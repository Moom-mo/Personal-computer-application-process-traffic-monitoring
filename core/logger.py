"""日志。

替代原先散落各处的 print：同时输出到控制台和带轮转的日志文件，
方便事后排查采集失败、客户端断连等问题。
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import resolve_path

_configured = set()


def _make_console_safe():
    """让控制台输出遇到无法编码的字符时替换而不是抛异常。

    中文版 Windows 的控制台默认是 GBK 编码，日志里出现 GBK 之外的字符
    （例如进程名中的特殊符号）会让 logging 直接抛 UnicodeEncodeError。
    这里只放宽错误处理，不改变原有编码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def get_logger(role, config):
    """获取（并在首次调用时配置）指定角色的日志器。

    role 同时用作日志文件名，例如 "server" / "client"。
    """
    logger = logging.getLogger(role)
    if role in _configured:
        return logger
    _configured.add(role)
    _make_console_safe()

    level = getattr(logging, str(config["log"].get("level", "INFO")).upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(threadName)s %(message)s", "%Y-%m-%d %H:%M:%S")

    log_dir = resolve_path(config, config["log"].get("dir", "logs"))
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, f"{role}.log"),
            maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        print(f"[logger] 无法创建日志目录 {log_dir}：{exc}")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger
