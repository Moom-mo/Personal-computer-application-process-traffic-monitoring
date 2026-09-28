"""进程网络连接与流量的采集器。

修正了原实现的三处根本性错误：

1. **字段名错误（致命）**。原代码写的是::

       io = p.io_counters()
       rx = io.bytes_recv     # AttributeError!
       tx = io.bytes_sent     # AttributeError!

   ``bytes_recv`` / ``bytes_sent`` 是 ``psutil.net_io_counters()``（系统级）的
   字段名，进程级 ``Process.io_counters()`` 返回的是 ``read_bytes`` /
   ``write_bytes``。这两个属性访问必然抛 AttributeError，而外层
   ``except Exception: continue`` 把它静默吞掉，结果是**数据库里一条流量
   记录都没有写入过**，历史图表永远为空、配额告警永远不触发。

2. **未按 PID 聚合**。原实现按「连接」循环，一个进程有 30 个连接就对同一个
   PID 读 30 次 io_counters，写入 30 行完全相同的累计值。

3. **未计算增量**。io_counters 是进程启动至今的累计值，直接入库无法分析。
   这里保存上次快照并计算差值，同时用 create_time 识别 PID 被复用的情况。

另外用 ``psutil.net_connections()`` 取代 ``netstat -ano`` 子进程 + 正则解析：
后者漏掉全部 IPv6 和 UDP 连接、每次调用 fork 一个子进程、还要硬编码 GBK 解码。
"""
import os
import time
from collections import defaultdict

import psutil

# 进程长时间不再出现后，清理其缓存的间隔（秒）
_STALE_AFTER = 300


class Collector:
    """采集本机各进程的网络连接与流量增量。

    :param exclude_self: 是否把采集服务自身排除在统计之外。默认开启：
        服务端每轮都要写 SQLite，这些写操作会被 io_counters 计入，
        导致监控工具自己常年占据流量榜首，掩盖真正占用带宽的进程。
    """

    def __init__(self, logger=None, exclude_self=True):
        self._log = logger
        self._self_pid = os.getpid() if exclude_self else None
        self._proc_cache = {}   # pid -> psutil.Process
        self._last_io = {}      # pid -> (create_time, rx_total, tx_total, last_seen)

    # ------------------------------------------------------------------ 内部

    def _get_process(self, pid):
        process = self._proc_cache.get(pid)
        if process is None:
            process = psutil.Process(pid)
            self._proc_cache[pid] = process
        return process

    def _prune(self, now):
        """清理长时间未出现（进程已退出）的缓存项。

        注意不能简单地按「本轮没出现」来清理：一个进程可能这轮刚好没有活跃
        连接、下轮又有，直接删缓存会让它丢掉基线，少统计一个周期的流量。
        """
        for pid in [p for p, v in self._last_io.items() if now - v[3] > _STALE_AFTER]:
            self._last_io.pop(pid, None)
            self._proc_cache.pop(pid, None)

    # ------------------------------------------------------------------ 接口

    def collect(self):
        """采集一轮，返回快照字典。

        :returns: ``{"ts", "processes", "connections", "skipped", "total_conn"}``

        - ``processes``   按进程聚合：PID、进程名、连接数、本周期收发增量、累计值
        - ``connections`` 逐条连接：本地端口、远端地址、状态
        - ``skipped``     因权限等原因读取失败的进程数（原实现静默丢弃，无从得知）
        """
        now = time.time()
        try:
            raw_connections = psutil.net_connections(kind="inet")
        except psutil.Error as exc:
            if self._log:
                self._log.warning("读取网络连接失败：%s", exc)
            raw_connections = []

        by_pid = defaultdict(list)
        for conn in raw_connections:
            if conn.pid:
                by_pid[conn.pid].append(conn)

        processes = []
        connections = []
        skipped = 0

        for pid, conns in by_pid.items():
            if pid == self._self_pid:
                continue
            try:
                process = self._get_process(pid)
                name = process.name()
                create_time = process.create_time()
                io = process.io_counters()
            except psutil.Error:
                # 进程已退出，或权限不足（系统进程普遍如此）
                self._proc_cache.pop(pid, None)
                self._last_io.pop(pid, None)
                skipped += 1
                continue

            rx_total = int(io.read_bytes)
            tx_total = int(io.write_bytes)

            previous = self._last_io.get(pid)
            if previous is not None and previous[0] == create_time:
                # 正常情况：用差值得到本周期流量
                rx_delta = max(0, rx_total - previous[1])
                tx_delta = max(0, tx_total - previous[2])
            else:
                # 进程首次出现，或 PID 被系统回收给了新进程（create_time 变化），
                # 没有可比基线，本周期记 0，下个周期起才是有效数据
                rx_delta = tx_delta = 0
            self._last_io[pid] = (create_time, rx_total, tx_total, now)

            remote_ips = []
            local_ports = set()
            active_count = 0
            for conn in conns:
                local_port = int(conn.laddr.port) if conn.laddr else 0
                local_ports.add(local_port)

                if not conn.raddr:
                    # 只监听、未与任何对端通信的本地绑定不进入连接列表：
                    # 一是它们谈不上「端口映射关系」，二是 Windows 上
                    # IPv4/IPv6 双栈套接字、多网卡 UDP 绑定会让同一个服务
                    # 重复出现十几条（如 0.0.0.0:5353 与 [::]:5353），
                    # 造成连接明细里满屏一模一样的行。
                    continue

                remote_ip = conn.raddr.ip
                remote_port = int(conn.raddr.port)
                active_count += 1
                if remote_ip not in remote_ips:
                    remote_ips.append(remote_ip)
                connections.append({
                    "pid": pid,
                    "name": name,
                    "local_port": local_port,
                    "remote_ip": remote_ip,
                    "remote_port": remote_port,
                    "status": conn.status or "",
                })

            processes.append({
                "pid": pid,
                "name": name,
                "conn_count": active_count,
                "local_ports": sorted(local_ports),
                "remote_ips": remote_ips,
                "rx_delta": rx_delta,
                "tx_delta": tx_delta,
                "rx_total": rx_total,
                "tx_total": tx_total,
            })

        self._prune(now)

        # 按本周期流量降序，界面直接拿来展示就是「谁在吃带宽」
        processes.sort(key=lambda p: p["rx_delta"] + p["tx_delta"], reverse=True)
        connections.sort(key=lambda c: (c["name"].lower(), c["local_port"], c["remote_port"]))

        return {
            "ts": now,
            "processes": processes,
            "connections": connections,
            "skipped": skipped,
            "total_conn": len(raw_connections),
        }

    @staticmethod
    def connection_key(connections):
        """把连接列表压成一个可比较的集合，用于判断连接拓扑是否发生变化。"""
        return frozenset(
            (c["pid"], c["local_port"], c["remote_ip"], c["remote_port"], c["status"])
            for c in connections)
