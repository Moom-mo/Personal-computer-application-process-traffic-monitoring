"""前台监控界面。

三个页签：

- **实时监控**：按进程聚合的流量列表（主表）+ 选中进程的连接明细（从表），
  下方是配额设置面板。原实现的实时表格只有 PID / 进程名 / 端口，唯独没有
  「流量」——打开一个流量监控工具却看不到谁在吃带宽，等于没有监控。
- **历史分析**：按小时的分时流量趋势 + 按进程的 Top N 排行，
  以及当日远端地址 Top 10，并支持导出 CSV。
- **告警记录**：配额告警的历史列表。

所有耗时操作（网络请求）都在 ui.net_thread 的子线程中执行，主线程只做渲染。
"""
from PyQt5.QtCore import QDate, Qt, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox,
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSplitter, QStyle, QSystemTrayIcon, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.export import default_csv_name, export_rows
from core.utils import format_bytes, format_ts, mb_to_bytes
from ui.chart import HistoryCanvas
from ui.net_thread import NetWorkThread

TAB_REALTIME = 0
TAB_HISTORY = 1
TAB_ALERTS = 2

_COLOR_EXCEEDED_BG = QColor(255, 226, 226)
_COLOR_EXCEEDED_FG = QColor(176, 0, 0)
_COLOR_NORMAL_BG = QColor(255, 255, 255)


class MainWindow(QMainWindow):
    """主窗口。"""

    REFRESH_MS = 3000

    def __init__(self, config, logger):
        super().__init__()
        self._cfg = config
        self._log = logger

        self._host = config["server"]["host"]
        self._port = int(config["server"]["port"])
        self._timeout = float(config["server"].get("timeout", 5.0))

        self._threads = []              # 保持对运行中线程的引用，避免被 GC
        self._realtime_busy = False
        self._last_alert_id = 0
        self._unread_alerts = 0

        self._all_processes = []
        self._all_connections = []
        self._combo_names = []
        self._last_history = None

        self.setWindowTitle("个人电脑应用进程流量监控")
        self.setGeometry(120, 80, 1180, 780)

        self._build_ui()
        self._build_tray()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.fetch_realtime)
        self._timer.start(self.REFRESH_MS)

        self.fetch_realtime()
        self.query_history()

    # ================================================================== 界面搭建

    def _build_ui(self):
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_realtime_tab(), "实时监控")
        self.tabs.addTab(self._build_history_tab(), "历史分析")
        self.tabs.addTab(self._build_alerts_tab(), "告警记录")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        self.lbl_status = QLabel("正在连接采集服务…")
        self.statusBar().addWidget(self.lbl_status)

    def _build_realtime_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        # ---- 工具栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("过滤进程名："))
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText("输入关键字，例如 chrome")
        self.edit_filter.setClearButtonEnabled(True)
        self.edit_filter.textChanged.connect(self._fill_process_table)
        bar.addWidget(self.edit_filter, stretch=2)

        self.chk_auto = QCheckBox("自动刷新")
        self.chk_auto.setChecked(True)
        self.chk_auto.stateChanged.connect(self._toggle_auto)
        bar.addWidget(self.chk_auto)

        self.btn_refresh = QPushButton("立即刷新")
        self.btn_refresh.clicked.connect(self.fetch_realtime)
        bar.addWidget(self.btn_refresh)
        bar.addStretch()
        layout.addLayout(bar)

        # ---- 主从两个表
        splitter = QSplitter(Qt.Horizontal)

        self.table_proc = QTableWidget(0, 7)
        self.table_proc.setHorizontalHeaderLabels(
            ["PID", "进程名", "活跃连接", "↓ 接收", "↑ 发送", "合计", "配额使用"])
        self._setup_table(self.table_proc)
        self.table_proc.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table_proc.itemSelectionChanged.connect(self._fill_conn_table)
        splitter.addWidget(self.table_proc)

        conn_box = QWidget()
        conn_layout = QVBoxLayout(conn_box)
        conn_layout.setContentsMargins(0, 0, 0, 0)
        self.lbl_conn_title = QLabel("连接明细（选中左侧进程查看）")
        conn_layout.addWidget(self.lbl_conn_title)

        self.table_conn = QTableWidget(0, 4)
        self.table_conn.setHorizontalHeaderLabels(
            ["本地端口", "远端 IP", "远端端口", "状态"])
        self._setup_table(self.table_conn)
        conn_layout.addWidget(self.table_conn)
        splitter.addWidget(conn_box)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, stretch=1)

        layout.addLayout(self._build_quota_panel())
        return page

    def _build_quota_panel(self):
        panel = QHBoxLayout()
        panel.addWidget(QLabel("进程名："))
        self.combo_proc = QComboBox()
        self.combo_proc.setEditable(True)
        self.combo_proc.setMinimumWidth(190)
        self.combo_proc.setToolTip("可从下拉列表选择当前正在联网的进程，也可手动输入")
        panel.addWidget(self.combo_proc)

        panel.addWidget(QLabel("流量配额："))
        self.spin_quota = QDoubleSpinBox()
        self.spin_quota.setRange(0.1, 102400.0)
        self.spin_quota.setDecimals(1)
        self.spin_quota.setValue(100.0)
        self.spin_quota.setSuffix(" MB")
        panel.addWidget(self.spin_quota)

        panel.addWidget(QLabel("统计周期："))
        self.combo_period = QComboBox()
        self.combo_period.addItem("按天", "day")
        self.combo_period.addItem("累计", "total")
        panel.addWidget(self.combo_period)

        self.btn_set_quota = QPushButton("设置配额")
        self.btn_set_quota.clicked.connect(self.set_quota)
        panel.addWidget(self.btn_set_quota)

        self.btn_del_quota = QPushButton("删除配额")
        self.btn_del_quota.clicked.connect(self.delete_quota)
        panel.addWidget(self.btn_del_quota)

        panel.addStretch()
        return panel

    def _build_history_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("查询日期："))
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setCalendarPopup(True)
        bar.addWidget(self.date_edit)

        self.btn_query = QPushButton("查询并绘图")
        self.btn_query.clicked.connect(self.query_history)
        bar.addWidget(self.btn_query)

        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.clicked.connect(self.export_csv)
        bar.addWidget(self.btn_export)

        self.btn_today = QPushButton("回到今天")
        self.btn_today.clicked.connect(lambda: self.date_edit.setDate(QDate.currentDate()))
        bar.addWidget(self.btn_today)
        bar.addStretch()
        layout.addLayout(bar)

        self.lbl_summary = QLabel("尚未查询")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setStyleSheet("color:#333333; padding:2px 0;")
        layout.addWidget(self.lbl_summary)

        self.canvas = HistoryCanvas(self)
        layout.addWidget(self.canvas, stretch=3)

        layout.addWidget(QLabel("当日出现过的远端地址 Top 10"))
        self.table_remote = QTableWidget(0, 3)
        self.table_remote.setHorizontalHeaderLabels(["远端 IP", "出现次数", "涉及进程数"])
        self._setup_table(self.table_remote)
        self.table_remote.setMaximumHeight(190)
        layout.addWidget(self.table_remote, stretch=1)
        return page

    def _build_alerts_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        self.btn_alerts = QPushButton("刷新告警记录")
        self.btn_alerts.clicked.connect(self.fetch_alerts)
        bar.addWidget(self.btn_alerts)
        bar.addStretch()
        layout.addLayout(bar)

        self.table_alerts = QTableWidget(0, 6)
        self.table_alerts.setHorizontalHeaderLabels(
            ["时间", "进程名", "PID", "已用流量", "配额", "统计周期"])
        self._setup_table(self.table_alerts)
        # 各列按内容自适应，剩余空间留给末尾，避免时间列被拉伸得过宽
        self.table_alerts.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table_alerts)
        return page

    @staticmethod
    def _setup_table(table):
        """表格通用设置：只读、整行选中、隐藏行号。"""
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

    def _build_tray(self):
        """系统托盘图标，用于弹非模态的告警通知。"""
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._log.info("系统托盘不可用，告警只在界面内提示")
            return
        icon = self.style().standardIcon(QStyle.SP_MessageBoxWarning)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("个人电脑应用进程流量监控")
        self._tray.show()

    # ================================================================== 网络请求

    def _request(self, cmd, handler):
        """发起一次异步请求，结果通过 handler 回调到主线程。"""
        thread = NetWorkThread(cmd, self._host, self._port, self._timeout)
        thread.result_signal.connect(handler)
        thread.finished.connect(lambda bound=thread: self._release_thread(bound))
        self._threads.append(thread)
        thread.start()
        return thread

    def _release_thread(self, thread):
        if thread in self._threads:
            self._threads.remove(thread)

    # ================================================================== 实时监控

    def fetch_realtime(self):
        if self._realtime_busy:
            return
        self._realtime_busy = True
        self._request(
            {"cmd": "get_realtime", "since_alert_id": self._last_alert_id},
            self._on_realtime)

    def _on_realtime(self, result):
        self._realtime_busy = False

        if not result or not result.get("ok"):
            reason = (result or {}).get("error", "未知错误")
            self._set_status(
                f"⚠ 无法连接采集服务 {self._host}:{self._port}（{reason}）"
                f"，请先运行 python server.py", error=True)
            return

        response = result.get("data") or {}
        if response.get("code") != 0:
            self._set_status(f"⚠ 服务端返回错误：{response.get('msg')}", error=True)
            return

        payload = response["data"]
        self._all_processes = payload.get("processes", [])
        self._all_connections = payload.get("connections", [])

        self._fill_process_table()
        self._handle_alerts(payload.get("alerts", []), payload.get("last_alert_id", 0))

        skipped = payload.get("skipped", 0)
        skip_text = f"，{skipped} 个进程因权限不足未采集" if skipped else ""
        self._set_status(
            f"采集时间 {payload.get('ts_text', '-')}｜进程 {len(self._all_processes)} 个"
            f"｜活跃连接 {len(self._all_connections)} 条"
            f"｜系统套接字 {payload.get('total_conn', 0)} 个{skip_text}")

    def _fill_process_table(self):
        """重建进程表。重建前记住选中行，重建后恢复，避免刷新时选中状态丢失。"""
        selected_pid = self._selected_pid()

        keyword = self.edit_filter.text().strip().lower()
        if keyword:
            rows = [p for p in self._all_processes if keyword in (p.get("name") or "").lower()]
        else:
            rows = self._all_processes

        self.table_proc.setUpdatesEnabled(False)
        self.table_proc.setRowCount(len(rows))
        for row, proc in enumerate(rows):
            self._fill_process_row(row, proc)
        self.table_proc.setUpdatesEnabled(True)

        self._update_proc_combo()
        self._restore_selection(selected_pid)
        self._fill_conn_table()

    def _fill_process_row(self, row, proc):
        quota = proc.get("quota")
        total = proc.get("rx_delta", 0) + proc.get("tx_delta", 0)
        exceeded = bool(quota and quota.get("exceeded"))

        values = [
            str(proc.get("pid", "")),
            proc.get("name") or "",
            str(proc.get("conn_count", 0)),
            format_bytes(proc.get("rx_delta", 0)),
            format_bytes(proc.get("tx_delta", 0)),
            format_bytes(total),
            self._quota_text(quota),
        ]

        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            if column != 1:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if exceeded:
                item.setBackground(_COLOR_EXCEEDED_BG)
                item.setForeground(_COLOR_EXCEEDED_FG)
                item.setToolTip("该进程已超过流量配额")
            else:
                item.setBackground(_COLOR_NORMAL_BG)
            if column == 2:
                # 只监听、未与对端通信的本地绑定不计入活跃连接，
                # 但「这个进程占了哪些本地端口」仍然是有用信息，放进提示里
                ports = proc.get("local_ports") or []
                ports_text = ", ".join(str(p) for p in ports) if ports else "无"
                item.setToolTip(f"活跃连接 {proc.get('conn_count', 0)} 条\n"
                                f"本地端口（含监听）：{ports_text}")
            if column == 6 and quota:
                period = "累计" if quota.get("period") == "total" else "当日"
                item.setToolTip(f"{period}已用 {format_bytes(quota['used'])}，"
                                f"配额 {format_bytes(quota['limit'])}")
            self.table_proc.setItem(row, column, item)

    @staticmethod
    def _quota_text(quota):
        if not quota:
            return "-"
        return f"{format_bytes(quota['used'])} / {format_bytes(quota['limit'])}"

    def _fill_conn_table(self):
        pid = self._selected_pid()
        if pid is None:
            self.table_conn.setRowCount(0)
            self.lbl_conn_title.setText("连接明细（选中左侧进程查看）")
            return

        name = next((p.get("name") or "" for p in self._all_processes
                     if p.get("pid") == pid), "")
        conns = [c for c in self._all_connections if c.get("pid") == pid]
        self.table_conn.setRowCount(len(conns))
        for row, conn in enumerate(conns):
            values = [
                str(conn.get("local_port", "")),
                conn.get("remote_ip") or "-",
                str(conn.get("remote_port") or "-"),
                conn.get("status") or "-",
            ]
            for column, text in enumerate(values):
                self.table_conn.setItem(row, column, QTableWidgetItem(text))

        if conns:
            self.lbl_conn_title.setText(f"连接明细：{name}(PID={pid}) 共 {len(conns)} 条")
        else:
            # 进程可以「有流量但没有活跃连接」——连接传输完就关闭了，
            # 而 I/O 计数器仍在累计，这里要说清楚，否则用户会以为界面坏了
            self.lbl_conn_title.setText(
                f"连接明细：{name}(PID={pid}) 当前无活跃连接"
                f"（流量可能来自刚刚关闭的连接）")

    def _selected_pid(self):
        items = self.table_proc.selectedItems()
        if not items:
            return None
        pid_item = self.table_proc.item(items[0].row(), 0)
        if pid_item is None:
            return None
        try:
            return int(pid_item.text())
        except ValueError:
            return None

    def _restore_selection(self, pid):
        if pid is None:
            return
        for row in range(self.table_proc.rowCount()):
            item = self.table_proc.item(row, 0)
            if item is not None and item.text() == str(pid):
                self.table_proc.selectRow(row)
                return

    def _update_proc_combo(self):
        """把当前联网的进程名填进配额面板的下拉框。

        只在进程名集合变化时才重建，且保留用户已经输入的内容，
        否则每次刷新都会打断用户正在输入的过程名。
        """
        names = sorted({p.get("name") for p in self._all_processes if p.get("name")},
                       key=str.lower)
        if names == self._combo_names:
            return
        self._combo_names = names

        current = self.combo_proc.currentText()
        self.combo_proc.blockSignals(True)
        self.combo_proc.clear()
        self.combo_proc.addItems(names)
        self.combo_proc.setCurrentText(current)
        self.combo_proc.blockSignals(False)

    def _toggle_auto(self, state):
        if state:
            self._timer.start(self.REFRESH_MS)
            self.fetch_realtime()
        else:
            self._timer.stop()

    # ================================================================== 配额操作

    def set_quota(self):
        proc_name = self.combo_proc.currentText().strip()
        if not proc_name:
            QMessageBox.information(self, "设置配额", "请先选择或输入进程名。")
            return
        cmd = {
            "cmd": "set_quota",
            "proc_name": proc_name,
            "quota_bytes": mb_to_bytes(self.spin_quota.value()),
            "period": self.combo_period.currentData(),
        }
        self._request(cmd, self._on_quota_result)

    def delete_quota(self):
        proc_name = self.combo_proc.currentText().strip()
        if not proc_name:
            QMessageBox.information(self, "删除配额", "请先选择或输入进程名。")
            return
        self._request({"cmd": "delete_quota", "proc_name": proc_name},
                      self._on_quota_result)

    def _on_quota_result(self, result):
        if not result or not result.get("ok"):
            reason = (result or {}).get("error", "未知错误")
            QMessageBox.warning(self, "配额操作失败",
                                f"无法连接采集服务：{reason}\n请确认已运行 python server.py")
            return
        response = result.get("data") or {}
        message = str(response.get("msg", ""))
        if response.get("code") != 0:
            QMessageBox.warning(self, "配额操作失败", message)
        else:
            self._set_status(message)
            self.fetch_realtime()

    # ================================================================== 历史分析

    def query_history(self):
        day = self.date_edit.date().toString("yyyy-MM-dd")
        self._request({"cmd": "query_history", "day": day}, self._on_history)

    def _on_history(self, result):
        if not result or not result.get("ok"):
            reason = (result or {}).get("error", "未知错误")
            QMessageBox.warning(self, "查询失败",
                                f"无法连接采集服务：{reason}\n请确认已运行 python server.py")
            return
        response = result.get("data") or {}
        if response.get("code") != 0:
            QMessageBox.warning(self, "查询失败", str(response.get("msg")))
            return

        payload = response["data"]
        self._last_history = payload

        self.canvas.render(payload["day"], payload["trend"], payload["processes"])

        summary = payload.get("summary", {})
        peak = ""
        if summary.get("peak_hour") is not None:
            peak = (f"，流量高峰 {summary['peak_hour']:02d}:00"
                    f"（{format_bytes(summary.get('peak_bytes', 0))}）")
        self.lbl_summary.setText(
            f"【{payload['day']}】总流量 {format_bytes(summary.get('total', 0))}"
            f"｜活跃进程 {summary.get('proc_count', 0)} 个"
            f"｜采样 {summary.get('sample_count', 0)} 条{peak}")

        self._fill_remote_table(payload.get("remote_ips", []))

    def _fill_remote_table(self, rows):
        self.table_remote.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            values = [entry.get("remote_ip", "-"),
                      str(entry.get("hits", 0)),
                      str(entry.get("proc_count", 0))]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table_remote.setItem(row, column, item)

    def export_csv(self):
        """把当前查询到的当日数据导出成 CSV（额外任务：数据导出）。"""
        if not self._last_history:
            QMessageBox.information(self, "导出 CSV", "请先查询某一天的数据。")
            return

        day = self._last_history["day"]
        path, _ = QFileDialog.getSaveFileName(
            self, "导出当日流量数据", default_csv_name("traffic", day),
            "CSV 文件 (*.csv)")
        if not path:
            return

        rows = [[p.get("proc_name") or "", p.get("rx", 0), p.get("tx", 0),
                 p.get("total", 0)] for p in self._last_history.get("processes", [])]
        try:
            saved = export_rows(path, ["进程名", "接收字节", "发送字节", "合计字节"], rows)
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return

        self._set_status(f"已导出 {len(rows)} 条记录到 {saved}")
        QMessageBox.information(self, "导出完成",
                                f"已导出 {len(rows)} 条记录：\n{saved}")

    # ================================================================== 告警

    def fetch_alerts(self):
        self._request({"cmd": "list_alerts", "limit": 300}, self._on_alerts)

    def _on_alerts(self, result):
        if not result or not result.get("ok"):
            return
        response = result.get("data") or {}
        if response.get("code") != 0:
            return
        self.table_alerts.setRowCount(0)
        for alert in response.get("data", []):
            self._append_alert_row(alert)

    def _append_alert_row(self, alert):
        row = self.table_alerts.rowCount()
        self.table_alerts.insertRow(row)
        period = "累计" if alert.get("period") == "total" else "当日"
        values = [
            format_ts(alert.get("ts")),
            alert.get("proc_name") or "-",
            str(alert.get("pid") or "-"),
            format_bytes(alert.get("used_bytes", 0)),
            format_bytes(alert.get("quota_bytes", 0)),
            period,
        ]
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            if column >= 2:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            item.setBackground(_COLOR_EXCEEDED_BG)
            item.setForeground(_COLOR_EXCEEDED_FG)
            self.table_alerts.setItem(row, column, item)

    def _handle_alerts(self, alerts, last_alert_id):
        if last_alert_id:
            self._last_alert_id = max(self._last_alert_id, int(last_alert_id))
        if not alerts:
            return

        for alert in alerts:
            self._append_alert_row(alert)

        # 非模态提示：状态栏 + 页签角标 + 系统托盘气泡，避免弹窗打断操作
        self._unread_alerts += len(alerts)
        self.tabs.setTabText(TAB_ALERTS, f"告警记录 ({self._unread_alerts})")
        self._set_status(f"⚠ 新增 {len(alerts)} 条流量超配额告警", error=True)

        if self._tray:
            first = alerts[0]
            extra = f"，共 {len(alerts)} 条" if len(alerts) > 1 else ""
            self._tray.showMessage(
                "流量超配额告警",
                f"{first.get('proc_name')} 已用 {format_bytes(first.get('used_bytes', 0))}"
                f"，配额 {format_bytes(first.get('quota_bytes', 0))}{extra}",
                QSystemTrayIcon.Warning, 5000)

    def _on_tab_changed(self, index):
        if index == TAB_ALERTS:
            self._unread_alerts = 0
            self.tabs.setTabText(TAB_ALERTS, "告警记录")
            self.fetch_alerts()

    # ================================================================== 其他

    def _set_status(self, message, error=False):
        self.lbl_status.setText(message)
        self.lbl_status.setStyleSheet("color:#b00000;" if error else "color:#333333;")

    def closeEvent(self, event):
        self._timer.stop()
        for thread in list(self._threads):
            thread.wait(1500)
        if self._tray:
            self._tray.hide()
        super().closeEvent(event)
