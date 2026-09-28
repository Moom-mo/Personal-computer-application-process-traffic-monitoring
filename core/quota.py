"""流量配额规则与告警判定。

与原实现的差异：

- **规则以进程名为主键，而不是 PID**。PID 在进程退出后会被系统回收复用，
  按 PID 存规则会把配额错误地套用到毫不相干的新进程上。
- **用量在内存中累加**。原实现对每个进程每个采集周期都开一次数据库连接
  做 SUM 查询，这里改为启动时从库里播种、之后在内存累加。
- **增加告警静默期**。原实现超配额后每 3 秒打印一次，日志被刷爆且无法阅读。
- **告警进入队列并落库**，客户端可以增量拉取，不再只打印在服务端控制台。
"""
import collections
import time

from .utils import day_hour

# 内存中保留的最近告警条数，供客户端增量拉取
_RECENT_ALERT_LIMIT = 500


class QuotaManager:
    """维护配额规则、累计用量并判定告警。"""

    def __init__(self, db, config, logger=None):
        self._db = db
        self._log = logger
        self._cooldown = max(0, int(config["quota"].get("alert_cooldown", 60)))
        # alert = 仅告警（默认）；suspend = 告警并挂起进程。
        # 挂起属于对用户进程的侵入性操作，因此默认关闭，需要时在 config.json 中开启。
        self._action = str(config["quota"].get("alert_action", "alert")).lower()

        self._rules = {}            # 进程名小写 -> 规则字典
        self._day_usage = {}        # 进程名小写 -> 当日累计字节
        self._total_usage = {}      # 进程名小写 -> 历史累计字节
        self._last_alert = {}       # 进程名小写 -> 上次告警时间戳
        self._day = None
        self._recent_alerts = collections.deque(maxlen=_RECENT_ALERT_LIMIT)
        self._last_alert_id = 0
        self.reload()

    # ------------------------------------------------------------------ 规则

    def reload(self):
        """重新从数据库加载规则，并用库中的历史用量重新播种内存计数。"""
        self._rules = {}
        for row in self._db.list_quotas():
            if not row.get("enabled"):
                continue
            self._rules[str(row["proc_name"]).lower()] = row

        self._day = day_hour()[0]
        self._day_usage = self._db.usage_by_process(self._day)
        self._total_usage = self._db.usage_by_process(None)
        if self._log:
            self._log.info("已加载 %d 条配额规则", len(self._rules))

    # ------------------------------------------------------------------ 用量

    def record(self, proc_name, delta_bytes):
        """累加一个进程本周期产生的流量。"""
        if delta_bytes <= 0 or not proc_name:
            return
        key = proc_name.lower()
        self._day_usage[key] = self._day_usage.get(key, 0) + delta_bytes
        self._total_usage[key] = self._total_usage.get(key, 0) + delta_bytes

    def _roll_day(self, now):
        """跨天时清空当日用量计数。"""
        day = day_hour(now)[0]
        if day != self._day:
            self._day = day
            self._day_usage = {}
            if self._log:
                self._log.info("跨天，已重置当日配额用量计数（%s）", day)
        return day

    def _usage_of(self, key, period):
        if period == "total":
            return self._total_usage.get(key, 0)
        return self._day_usage.get(key, 0)

    # ------------------------------------------------------------------ 判定

    def evaluate(self, processes, now=None):
        """给每个进程附上配额状态，并返回本轮新产生的告警。

        会就地修改 ``processes`` 中每个字典，写入 ``quota`` 字段
        （无规则的进程为 None），供界面直接渲染。
        """
        now = time.time() if now is None else now
        day = self._roll_day(now)
        alerts = []

        for process in processes:
            name = process.get("name") or ""
            key = name.lower()
            rule = self._rules.get(key)
            if not rule:
                process["quota"] = None
                continue

            period = rule.get("period") or "day"
            quota = int(rule["quota_bytes"])
            used = self._usage_of(key, period)
            exceeded = quota > 0 and used > quota
            process["quota"] = {
                "limit": quota,
                "used": used,
                "period": period,
                "ratio": (used / quota) if quota > 0 else 0.0,
                "exceeded": exceeded,
            }

            if exceeded and self._cooldown_passed(key, now):
                alerts.append(self._raise(now, day, process, used, quota, period, name))

        return alerts

    def _cooldown_passed(self, key, now):
        last = self._last_alert.get(key)
        if last is not None and now - last < self._cooldown:
            return False
        self._last_alert[key] = now
        return True

    def _raise(self, now, day, process, used, quota, period, name):
        """生成一条告警：写入数据库、推入内存队列，必要时执行控制动作。"""
        period_text = "累计" if period == "total" else "当日"
        message = (f"进程 {name}(PID={process.get('pid')}) {period_text}流量 "
                   f"{used} 字节，已超过配额 {quota} 字节")

        alert_id = self._db.log_alert(
            now, day, process.get("pid"), name, used, quota, period, message)
        alert = {
            "id": alert_id,
            "ts": int(now),
            "day": day,
            "pid": process.get("pid"),
            "proc_name": name,
            "used_bytes": used,
            "quota_bytes": quota,
            "period": period,
            "message": message,
        }
        self._recent_alerts.append(alert)
        self._last_alert_id = max(self._last_alert_id, alert_id)

        if self._log:
            self._log.warning("【流量告警】%s", message)

        if self._action == "suspend":
            self._suspend(process.get("pid"), name)

        return alert

    def _suspend(self, pid, name):
        """挂起超配额进程（需在 config.json 中把 alert_action 设为 suspend）。"""
        try:
            import psutil
            psutil.Process(pid).suspend()
            if self._log:
                self._log.warning("已挂起超配额进程 %s(PID=%s)", name, pid)
        except Exception as exc:
            if self._log:
                self._log.error("挂起进程 %s(PID=%s) 失败：%s", name, pid, exc)

    # ------------------------------------------------------------- 告警查询

    def alerts_since(self, alert_id):
        """返回 id 大于 alert_id 的告警，供客户端增量拉取。"""
        return [a for a in self._recent_alerts if a["id"] > int(alert_id or 0)]

    def last_alert_id(self):
        return self._last_alert_id
