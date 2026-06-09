"""
protocol.py — 网络协议定义

UDP 发现协议：服务器每隔 2 秒广播房间名，客户端监听发现房间
TCP 消息协议：1 字节类型 + 4 字节长度 + 数据
  类型 0x00 = JSON 控制消息
  类型 0x01 = 文件二进制块
"""

import json
import struct
import socket

import hashlib


# ── 端口常量 ──────────────────────────────────────────────
DISCOVERY_PORT = 24600  # UDP 发现端口
CHAT_PORT      = 24601  # TCP 聊天端口
SOCKS5_PORT    = 10800  # SOCKS5 代理默认端口
BROADCAST_IP   = "255.255.255.255"

# ── 文件块前缀：每个文件数据块前附加 36 字节的 file_id (UUID) ──
FILE_ID_LEN = 36  # UUID 字符串长度


# ── 工具函数 ──────────────────────────────────────────────

def compute_file_hash(filepath: str) -> str:
    """计算文件的 SHA-256 哈希值（十六进制字符串）"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(128 * 1024)  # 128KB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ── UDP 发现 ──────────────────────────────────────────────

def make_discovery_packet(room_name: str, proxy_port: int = 0) -> bytes:
    """构造 UDP 发现广播包（房间名 + 可选代理信息）"""
    if proxy_port:
        return f"{room_name}|proxy:{proxy_port}".encode("utf-8")
    return room_name.encode("utf-8")


def parse_discovery_packet(data: bytes):
    """解析 UDP 发现包，返回 (room_name, proxy_port|0)"""
    text = data.decode("utf-8")
    proxy_port = 0
    room_name = text
    if "|proxy:" in text:
        parts = text.split("|proxy:", 1)
        room_name = parts[0]
        try:
            proxy_port = int(parts[1])
        except ValueError:
            pass
    return room_name, proxy_port


# ── TCP 消息帧 ────────────────────────────────────────────

MSG_TYPE_JSON = 0x00   # JSON 控制消息
MSG_TYPE_FILE = 0x01   # 文件二进制块


# ── 文件块编码 ────────────────────────────────────────────

def make_file_chunk(file_id: str, chunk: bytes) -> bytes:
    """将 file_id 和二进制块打包成一帧数据"""
    return file_id.encode("ascii") + chunk


def parse_file_chunk(payload: bytes):
    """从帧数据中解析出 (file_id, chunk_data)"""
    if len(payload) < FILE_ID_LEN:
        raise ValueError(f"文件块太短 ({len(payload)} < {FILE_ID_LEN})")
    fid = payload[:FILE_ID_LEN].decode("ascii")
    chunk = payload[FILE_ID_LEN:]
    return fid, chunk


def send_json(sock: socket.socket, msg: dict):
    """发送一条 JSON 控制消息"""
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    _send_frame(sock, MSG_TYPE_JSON, payload)


def send_file_chunk(sock: socket.socket, chunk: bytes):
    """发送一段文件二进制数据"""
    _send_frame(sock, MSG_TYPE_FILE, chunk)


def recv_frame(sock: socket.socket):
    """
    接收一帧数据，返回 (msg_type, payload)
    阻塞读取；连接关闭时返回 (None, None)
    """
    try:
        header = _recv_exact(sock, 5)
    except (ConnectionError, OSError):
        return None, None
    if not header:
        return None, None
    msg_type, length = struct.unpack("!BI", header)
    payload = _recv_exact(sock, length)
    if payload is None:
        return None, None
    return msg_type, payload


def recv_json(sock: socket.socket):
    """接收一条 JSON 消息，返回 dict；连接关闭返回 None"""
    msg_type, payload = recv_frame(sock)
    if msg_type is None:
        return None
    if msg_type != MSG_TYPE_JSON:
        raise ValueError(f"期望 JSON 帧，收到类型 {msg_type}")
    return json.loads(payload.decode("utf-8"))


# ── 内部辅助 ──────────────────────────────────────────────

def _send_frame(sock: socket.socket, msg_type: int, payload: bytes):
    header = struct.pack("!BI", msg_type, len(payload))
    sock.sendall(header + payload)


def _recv_exact(sock: socket.socket, n: int):
    """精确读取 n 字节，连接关闭返回 None（决不返回残缺数据）"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
