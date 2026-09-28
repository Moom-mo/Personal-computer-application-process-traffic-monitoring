"""后台采集服务：采集线程 + Socket 服务的组合体。

与原实现的差异：

- ``get_realtime`` 直接返回采集线程维护的**内存快照**，不再在请求线程里
  重新跑一遍 netstat。原实现每来一个客户端刷新请求就 fork 一个 netstat
  子进程，多客户端并发时服务端会雪崩。
- 采集循环用「周期减去已耗时」的方式休眠，避免采集本身耗时导致周期漂移。
- 单次采集异常不会终止整个循环。
- 所有指令通过统一的 dispatch 分发，未知指令返回明确错误码。
"""
import socket
import threading
import time

from . import protocol
from .collector import Collector
from .quota import QuotaManager
from .utils import day_hour, format_ts

# 清理历史数据的间隔（秒）
_CLEANUP_INTERVAL = 6 * 3600

# 客户端连接空闲超时（秒）
_CLIENT_TIMEOUT = 60.0


class MonitorService:
    """把采集、持久化、配额判定和对外查询接口组装成一个后台服务。"""

    def __init__(self, config, db, logger):
        self._cfg = config
        self._db = db
        self._log = logger

        collect_cfg = config["collect"]
        self._interval = max(1, int(collect_cfg.get("interval", 3)))
        self._min_delta = int(collect_cfg.get("min_delta_bytes", 1))
        self._conn_min_gap = int(collect_cfg.get("conn_snapshot_min_gap", 30))
        self._conn_heartbeat = int(collect_cfg.get("conn_snapshot_heartbeat", 900))
        self._retention = int(config["database"].get("retention_days", 14))

        self._host = config["server"]["host"]
        self._port = int(config["server"]["port"])

        self._collector = Collector(
            logger, exclude_self=bool(collect_cfg.get("exclude_self", True)))
        self._quota = QuotaManager(db, config, logger)

        self._snapshot = {
            "ts": 0.0, "processes": [], "connections": [],
            "skipped": 0, "total_conn": 0,
        }
        self._snapshot_lock = threading.Lock()

        self._last_conn_key = None
        self._last_conn_ts = 0.0
        self._last_cleanup = 0.0

    # -------------------------------------------------------------- 采集循环

    def start(self):
        """启动采集线程并阻塞在 Socket 服务上。"""
        threading.Thread(target=self._collect_loop, name="collector", daemon=True).start()
        self._serve_forever()

    def _collect_loop(self):
        while True:
            started = time.time()
            try:
                self._collect_once()
            except Exception:
                # 单轮采集失败不能拖垮整个服务
                self._log.exception("采集循环发生异常")
            elapsed = time.time() - started
            time.sleep(max(0.2, self._interval - elapsed))

    def _collect_once(self):
        snapshot = self._collector.collect()
        now = snapshot["ts"]
        day, hour = day_hour(now)

        # 1. 流量增量入库（只写有流量的，数据量比原实现低一个数量级）
        rows = []
        for process in snapshot["processes"]:
            delta = process["rx_delta"] + process["tx_delta"]
            if delta >= self._min_delta:
                rows.append((int(now), day, hour, process["pid"], process["name"],
                             process["rx_delta"], process["tx_delta"]))
                self._quota.record(process["name"], delta)
        self._db.insert_deltas(rows)

        # 2. 配额判定（会就地给每个进程补上 quota 字段）
        alerts = self._quota.evaluate(snapshot["processes"], now)
        if alerts:
            self._log.warning("本轮产生 %d 条流量告警", len(alerts))

        # 3. 连接快照按变化增量写入
        self._maybe_snapshot_connections(snapshot, now, day)

        # 4. 发布快照
        with self._snapshot_lock:
            self._snapshot = snapshot

        # 5. 定期清理过期数据
        self._maybe_cleanup(now)

    def _maybe_snapshot_connections(self, snapshot, now, day):
        """连接拓扑变化时写入快照，长时间不变则写一次心跳。

        原实现没有历史连接数据；这里保留端口映射关系的历史，
        用于「当日远端地址 Top N」这类分析。写入频率受两个阈值约束，
        避免浏览器频繁开关标签页时把数据库刷爆。
        """
        if now - self._last_conn_ts < self._conn_min_gap:
            return

        key = Collector.connection_key(snapshot["connections"])
        changed = key != self._last_conn_key
        heartbeat_due = (now - self._last_conn_ts) >= self._conn_heartbeat
        if not (changed or heartbeat_due):
            return

        rows = [(int(now), day, c["pid"], c["name"], c["local_port"],
                 c["remote_ip"], c["remote_port"], c["status"])
                for c in snapshot["connections"]]
        self._db.insert_conn_snapshot(rows)
        self._last_conn_key = key
        self._last_conn_ts = now

    def _maybe_cleanup(self, now):
        if now - self._last_cleanup < _CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        try:
            self._db.cleanup(self._retention)
        except Exception:
            self._log.exception("清理历史数据失败")

    # -------------------------------------------------------------- Socket 服务

    def _serve_forever(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self._host, self._port))
        except OSError as exc:
            self._log.error("端口 %s:%d 绑定失败：%s（是否已有服务实例在运行？）",
                            self._host, self._port, exc)
            raise
        server.listen(8)
        self._log.info("后台采集服务已启动，监听 %s:%d，采集周期 %d 秒",
                       self._host, self._port, self._interval)

        while True:
            try:
                conn, addr = server.accept()
            except OSError:
                self._log.exception("accept 失败")
                continue
            threading.Thread(target=self._handle_client, args=(conn, addr),
                             name=f"client-{addr[1]}", daemon=True).start()

    def _handle_client(self, conn, addr):
        conn.settimeout(_CLIENT_TIMEOUT)
        try:
            while True:
                try:
                    request = protocol.recv_message(conn)
                except protocol.ProtocolError as exc:
                    self._log.warning("来自 %s 的报文非法：%s", addr, exc)
                    break
                if request is None:
                    break
                protocol.send_message(conn, self._dispatch(request))
        except (OSError, socket.timeout) as exc:
            self._log.debug("客户端 %s 连接结束：%s", addr, exc)
        except Exception:
            self._log.exception("处理客户端 %s 请求时发生异常", addr)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # -------------------------------------------------------------- 指令分发

    def _dispatch(self, request):
        cmd = request.get("cmd")
        try:
            if cmd == "get_realtime":
                return self._ok(self._realtime_payload(request))
            if cmd == "query_history":
                return self._ok(self._history_payload(request.get("day")))
            if cmd == "set_quota":
                return self._set_quota(request)
            if cmd == "delete_quota":
                return self._delete_quota(request)
            if cmd == "list_quota":
                return self._ok(self._db.list_quotas())
            if cmd == "list_alerts":
                return self._ok(self._db.list_alerts(
                    request.get("day"), int(request.get("limit", 200))))
            if cmd == "ping":
                return self._ok({"ts": time.time()})
            return {"code": 400, "msg": f"未知指令：{cmd}", "data": None}
        except Exception as exc:
            self._log.exception("执行指令 %s 失败", cmd)
            return {"code": 500, "msg": f"服务端处理失败：{exc}", "data": None}

    @staticmethod
    def _ok(data, msg="ok"):
        return {"code": 0, "msg": msg, "data": data}

    def _realtime_payload(self, request):
        with self._snapshot_lock:
            snapshot = self._snapshot
        since = int(request.get("since_alert_id") or 0)
        return {
            "ts": snapshot["ts"],
            "ts_text": format_ts(snapshot["ts"]) if snapshot["ts"] else "-",
            "processes": snapshot["processes"],
            "connections": snapshot["connections"],
            "skipped": snapshot["skipped"],
            "total_conn": snapshot["total_conn"],
            # 客户端每次刷新顺带把新告警取走，省去一次往返
            "alerts": self._quota.alerts_since(since),
            "last_alert_id": self._quota.last_alert_id(),
        }

    def _history_payload(self, day):
        if not day:
            day = day_hour()[0]
        return {
            "day": day,
            "trend": self._db.day_trend(day),
            "processes": self._db.day_by_process(day),
            "remote_ips": self._db.remote_ip_top(day, limit=10),
            "summary": self._db.day_summary(day),
        }

    def _set_quota(self, request):
        proc_name = str(request.get("proc_name") or "").strip()
        if not proc_name:
            return {"code": 400, "msg": "进程名不能为空", "data": None}
        try:
            quota_bytes = int(request.get("quota_bytes") or 0)
        except (TypeError, ValueError):
            return {"code": 400, "msg": "配额必须是整数（字节）", "data": None}
        if quota_bytes <= 0:
            return {"code": 400, "msg": "配额必须大于 0", "data": None}

        period = str(request.get("period") or "day")
        if period not in ("day", "total"):
            period = "day"

        self._db.upsert_quota(proc_name, quota_bytes, period)
        # 规则变化后用库中数据重新播种用量，保证立即生效
        self._quota.reload()
        self._log.info("已为 %s 设置配额 %d 字节（周期 %s）", proc_name, quota_bytes, period)
        return self._ok(None, f"已为 {proc_name} 设置配额")

    def _delete_quota(self, request):
        proc_name = str(request.get("proc_name") or "").strip()
        if not proc_name:
            return {"code": 400, "msg": "进程名不能为空", "data": None}
        self._db.delete_quota(proc_name)
        self._quota.reload()
        self._log.info("已删除 %s 的配额规则", proc_name)
        return self._ok(None, f"已删除 {proc_name} 的配额")
