"""后台采集服务 —— 服务端进程入口。

职责：
    定时采集本机各进程的网络连接与流量增量，按 PID 聚合后写入 SQLite，
    判定配额告警，并通过本地 TCP Socket 向前台 GUI 提供查询接口。

本进程不包含任何界面代码，可独立于 GUI 运行（也可以注册成开机自启的后台服务）。

启动：
    python server.py
"""
import sys

from core.config import load_config, resolve_path
from core.db import Database
from core.logger import get_logger
from core.service import MonitorService


def main():
    config = load_config()
    logger = get_logger("server", config)

    db = Database(resolve_path(config, config["database"]["file"]), logger)
    service = MonitorService(config, db, logger)

    try:
        service.start()
    except KeyboardInterrupt:
        logger.info("收到中断信号，服务退出")
    except OSError:
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
