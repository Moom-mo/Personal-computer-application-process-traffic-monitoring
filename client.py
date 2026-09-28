"""前台监控界面 —— 客户端进程入口。

职责：
    展示实时进程流量与端口映射、查询历史某天的流量并绘图、
    设置进程流量配额、查看告警记录。

本进程不采集数据、不访问数据库，全部数据都通过本地 TCP Socket
向 server.py 提供的后台采集服务索取，做到采集与展示的彻底分离。

启动（需先运行 server.py）：
    python client.py
"""
import sys

from PyQt5.QtWidgets import QApplication

from core.config import load_config
from core.logger import get_logger
from ui.main_window import MainWindow


def main():
    config = load_config()
    logger = get_logger("client", config)

    app = QApplication(sys.argv)
    window = MainWindow(config, logger)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
