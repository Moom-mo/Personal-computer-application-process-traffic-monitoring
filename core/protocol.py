"""客户端与服务端之间的报文协议。

帧格式：4 字节大端长度前缀 + UTF-8 编码的 JSON 正文。

相比原先「recv(4096) 单次接收 + \\r\\n###END###\\r\\n 文本标记」的做法：

- TCP 是字节流，recv 不保证一次收到完整报文，原实现只要发生分包就会
  把半个 JSON 丢给 json.loads 而报错；
- 文本结束标记在报文内容恰好包含该串时会误判；
- 长度前缀能精确知道要读多少字节，也不会与正文内容冲突。
"""
import json
import struct

_HEADER = struct.Struct("!I")

# 单条报文上限，防止对端发来畸形长度导致内存被撑爆
MAX_MESSAGE = 16 * 1024 * 1024


class ProtocolError(Exception):
    """报文格式非法。"""


def _recv_exact(sock, size):
    """从 socket 精确读取 size 字节；对端提前关闭时返回 None。"""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_message(sock, obj):
    """发送一条 JSON 报文。"""
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE:
        raise ProtocolError(f"报文过大：{len(payload)} 字节")
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def recv_message(sock):
    """接收一条 JSON 报文；对端正常关闭时返回 None。"""
    header = _recv_exact(sock, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length == 0:
        return {}
    if length > MAX_MESSAGE:
        raise ProtocolError(f"报文长度异常：{length} 字节")
    body = _recv_exact(sock, length)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"报文解析失败：{exc}") from exc
