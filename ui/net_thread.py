"""GUI 侧的网络请求线程。

所有 Socket 请求都放在子线程里执行，避免阻塞 Qt 主线程导致界面卡死。

修正了原实现的两个问题：

1. **没有超时，会把界面永久锁死**。原实现用 ``recv(4096)`` 循环收数据且未设置
   socket 超时，一旦服务端不响应，线程永久阻塞 → ``finished`` 信号不触发 →
   界面的互斥锁永不解锁 → 实时刷新彻底停摆且没有任何提示。这里统一设置超时。
2. **没有处理 TCP 分包**。原实现靠 ``###END###`` 文本标记判断报文结束，
   收到半个 JSON 时直接解析失败。这里改用 core.protocol 的长度前缀帧格式。
"""
import socket

from PyQt5.QtCore import QThread, pyqtSignal

from core import protocol


class NetWorkThread(QThread):
    """执行一次「发一条请求、收一条响应」的短连接交互。

    无论成功失败都会发出 ``result_signal``：

    - 成功：``{"ok": True,  "data": <服务端响应字典>}``
    - 失败：``{"ok": False, "error": "<原因>"}``
    """

    result_signal = pyqtSignal(object)

    def __init__(self, cmd_dict, host, port, timeout=5.0, parent=None):
        super().__init__(parent)
        self._cmd = cmd_dict
        self._host = host
        self._port = port
        self._timeout = timeout

    def run(self):
        sock = None
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
            sock.settimeout(self._timeout)
            protocol.send_message(sock, self._cmd)
            response = protocol.recv_message(sock)
            if response is None:
                self.result_signal.emit({"ok": False, "error": "服务端未返回数据"})
            else:
                self.result_signal.emit({"ok": True, "data": response})
        except socket.timeout:
            self.result_signal.emit({"ok": False, "error": f"请求超时（{self._timeout} 秒）"})
        except ConnectionRefusedError:
            self.result_signal.emit({"ok": False, "error": "连接被拒绝"})
        except (OSError, protocol.ProtocolError) as exc:
            self.result_signal.emit({"ok": False, "error": str(exc)})
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
