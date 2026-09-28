"""数据持久化层（SQLite）。

相对原始实现的关键修正：

1. **只写增量，不写累计值**。psutil 的 ``Process.io_counters()`` 返回的是进程
   启动至今的累计字节数，原实现把这个累计值原样入库，得到的是单调递增的快照，
   既不能求和也不能画趋势。
2. **按 PID 聚合后写入**。原实现按「连接」循环，同一个进程的累计值被重复写入
   N 份（Chrome 开 30 个连接就写 30 行），导致数据冗余、告警重复触发。
3. **用 day / hour 列 + 索引替代 LIKE 前缀匹配**。原查询
   ``record_time LIKE '2026-09-23%'`` 无法命中索引，只能全表扫描。
4. **开启 WAL 日志模式**，采集线程写入时不会阻塞前台 GUI 的查询。

表结构：

- ``traffic_delta``    按进程的流量增量，分析的主力表
- ``conn_snapshot``    连接/端口映射快照，按变化增量写入
- ``quota_rule``       配额规则，**以进程名为主键**
- ``alert_log``        告警历史
"""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic_delta (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    day       TEXT    NOT NULL,
    hour      INTEGER NOT NULL,
    pid       INTEGER NOT NULL,
    proc_name TEXT,
    rx_bytes  INTEGER NOT NULL DEFAULT 0,
    tx_bytes  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_delta_day      ON traffic_delta(day);
CREATE INDEX IF NOT EXISTS idx_delta_ts       ON traffic_delta(ts);
CREATE INDEX IF NOT EXISTS idx_delta_name_day ON traffic_delta(proc_name, day);

CREATE TABLE IF NOT EXISTS conn_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    pid         INTEGER,
    proc_name   TEXT,
    local_port  INTEGER,
    remote_ip   TEXT,
    remote_port INTEGER,
    status      TEXT
);
CREATE INDEX IF NOT EXISTS idx_conn_day      ON conn_snapshot(day);
CREATE INDEX IF NOT EXISTS idx_conn_day_ip   ON conn_snapshot(day, remote_ip);

CREATE TABLE IF NOT EXISTS quota_rule (
    proc_name   TEXT PRIMARY KEY,
    quota_bytes INTEGER NOT NULL,
    period      TEXT    NOT NULL DEFAULT 'day',
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS alert_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    pid         INTEGER,
    proc_name   TEXT,
    used_bytes  INTEGER,
    quota_bytes INTEGER,
    period      TEXT,
    message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_day ON alert_log(day);
"""


class Database:
    """SQLite 访问封装。

    使用单个连接 + 可重入锁：sqlite3 的连接对象默认不允许跨线程使用，
    而采集线程与多个客户端处理线程都会访问数据库，因此显式加锁串行化。
    WAL 模式下读操作本身不阻塞写，锁的持有时间很短，不构成瓶颈。
    """

    def __init__(self, path, logger=None):
        self._path = path
        self._log = logger
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        if self._log:
            self._log.info("数据库就绪：%s", path)

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 内部

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql, params=()):
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    # -------------------------------------------------------------- 写入接口

    def insert_deltas(self, rows):
        """批量写入流量增量。

        rows: [(ts, day, hour, pid, proc_name, rx_bytes, tx_bytes), ...]
        """
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO traffic_delta"
                "(ts, day, hour, pid, proc_name, rx_bytes, tx_bytes)"
                " VALUES (?,?,?,?,?,?,?)", rows)
            self._conn.commit()
        return len(rows)

    def insert_conn_snapshot(self, rows):
        """批量写入连接快照。

        rows: [(ts, day, pid, proc_name, local_port, remote_ip, remote_port, status), ...]
        """
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO conn_snapshot"
                "(ts, day, pid, proc_name, local_port, remote_ip, remote_port, status)"
                " VALUES (?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()
        return len(rows)

    def log_alert(self, ts, day, pid, proc_name, used, quota, period, message):
        """记录一条告警，返回自增 id。"""
        cursor = self._execute(
            "INSERT INTO alert_log"
            "(ts, day, pid, proc_name, used_bytes, quota_bytes, period, message)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (int(ts), day, pid, proc_name, int(used), int(quota), period, message))
        return cursor.lastrowid

    # -------------------------------------------------------------- 查询接口

    def day_trend(self, day):
        """当日 24 小时的分时流量，返回长度 24 的列表（单位：字节）。"""
        rows = self._query(
            "SELECT hour, SUM(rx_bytes + tx_bytes) AS total FROM traffic_delta"
            " WHERE day = ? GROUP BY hour", (day,))
        buckets = [0] * 24
        for row in rows:
            hour = int(row["hour"])
            if 0 <= hour < 24:
                buckets[hour] = int(row["total"] or 0)
        return buckets

    def day_by_process(self, day, limit=None):
        """当日按进程聚合的流量，按合计降序。"""
        sql = ("SELECT proc_name,"
               "       SUM(rx_bytes) AS rx,"
               "       SUM(tx_bytes) AS tx,"
               "       SUM(rx_bytes + tx_bytes) AS total,"
               "       COUNT(DISTINCT pid) AS pid_count"
               " FROM traffic_delta WHERE day = ?"
               " GROUP BY proc_name"
               " ORDER BY total DESC")
        params = [day]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(row) for row in self._query(sql, params)]

    def day_summary(self, day):
        """当日汇总：总流量、活跃进程数、采样条数、流量最高的时段。"""
        row = self._query(
            "SELECT COALESCE(SUM(rx_bytes + tx_bytes), 0) AS total,"
            "       COUNT(DISTINCT proc_name) AS proc_count,"
            "       COUNT(*) AS sample_count"
            " FROM traffic_delta WHERE day = ?", (day,))[0]
        trend = self.day_trend(day)
        peak_hour = max(range(24), key=lambda h: trend[h]) if any(trend) else None
        return {
            "total": int(row["total"]),
            "proc_count": int(row["proc_count"]),
            "sample_count": int(row["sample_count"]),
            "peak_hour": peak_hour,
            "peak_bytes": trend[peak_hour] if peak_hour is not None else 0,
        }

    def remote_ip_top(self, day, limit=10):
        """当日出现次数最多的远端地址。"""
        rows = self._query(
            "SELECT remote_ip,"
            "       COUNT(*) AS hits,"
            "       COUNT(DISTINCT pid) AS proc_count"
            " FROM conn_snapshot"
            " WHERE day = ? AND remote_ip IS NOT NULL AND remote_ip <> ''"
            " GROUP BY remote_ip ORDER BY hits DESC LIMIT ?",
            (day, int(limit)))
        return [dict(row) for row in rows]

    def usage_by_process(self, day=None):
        """按进程统计累计用量，返回 {进程名小写: 字节数}。

        day 为 None 时统计全部历史（对应配额周期 "total"）。
        """
        if day is None:
            rows = self._query(
                "SELECT proc_name, SUM(rx_bytes + tx_bytes) AS used"
                " FROM traffic_delta GROUP BY proc_name")
        else:
            rows = self._query(
                "SELECT proc_name, SUM(rx_bytes + tx_bytes) AS used"
                " FROM traffic_delta WHERE day = ? GROUP BY proc_name", (day,))
        return {str(row["proc_name"]).lower(): int(row["used"] or 0)
                for row in rows if row["proc_name"]}

    # ---------------------------------------------------------------- 配额

    def upsert_quota(self, proc_name, quota_bytes, period="day"):
        self._execute(
            "REPLACE INTO quota_rule(proc_name, quota_bytes, period, enabled, updated_at)"
            " VALUES (?,?,?,1,?)",
            (proc_name, int(quota_bytes), period, int(time.time())))

    def delete_quota(self, proc_name):
        self._execute("DELETE FROM quota_rule WHERE proc_name = ?", (proc_name,))

    def list_quotas(self):
        rows = self._query(
            "SELECT proc_name, quota_bytes, period, enabled FROM quota_rule"
            " ORDER BY proc_name")
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- 告警

    def list_alerts(self, day=None, limit=200):
        if day:
            rows = self._query(
                "SELECT * FROM alert_log WHERE day = ? ORDER BY id DESC LIMIT ?",
                (day, int(limit)))
        else:
            rows = self._query(
                "SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- 维护

    def cleanup(self, retention_days):
        """删除超过保留期的历史数据，返回删除的行数。"""
        cutoff = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - int(retention_days) * 86400))
        removed = 0
        for table in ("traffic_delta", "conn_snapshot", "alert_log"):
            cursor = self._execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
            removed += cursor.rowcount or 0
        if removed and self._log:
            self._log.info("清理 %s 之前的历史数据，共删除 %d 行", cutoff, removed)
        return removed
