"""通用小工具。"""
import time

MB = 1024 * 1024


def format_bytes(num):
    """把字节数格式化成人类可读的字符串。"""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


def mb_to_bytes(mb):
    """MB -> 字节。"""
    return int(round(float(mb) * MB))


def bytes_to_mb(num):
    """字节 -> MB。"""
    return float(num) / MB


def today_str():
    """当前本地日期，格式 YYYY-MM-DD。"""
    return time.strftime("%Y-%m-%d")


def day_hour(ts=None):
    """把时间戳拆成本地日期字符串和小时。"""
    ts = time.time() if ts is None else ts
    local = time.localtime(ts)
    return time.strftime("%Y-%m-%d", local), int(time.strftime("%H", local))


def format_ts(ts, fmt="%Y-%m-%d %H:%M:%S"):
    """时间戳 -> 本地时间字符串。"""
    try:
        return time.strftime(fmt, time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "-"
