"""
client.py — 客户端网络层

职责：
  1. 连接服务器 TCP
  2. 发送/接收消息（文本 + 文件）
  3. 监听 UDP 发现包（自动搜索房间）
  4. 通过回调通知 UI 更新
"""

import json
import socket
import threading
import time

from protocol import (
    DISCOVERY_PORT, CHAT_PORT,
    send_json, recv_json, send_file_chunk, recv_frame,
    MSG_TYPE_FILE,
    parse_file_chunk,
    parse_discovery_packet,
)


class ChatClient:
    def __init__(self):
        self.tcp_sock = None
        self.nick = ""
        self.connected = False
        self.recv_thread = None

        # 回调（由 UI 设置）
        self.on_message = None        # func(msg_dict)
        self.on_file_chunk = None     # func(file_id, chunk_bytes)
        self.on_disconnect = None     # func()
        self.on_users = None          # func(user_list)
        self.on_rename_ok = None      # func(new_nick)
        self.on_proxy_info = None     # func(proxy_port)

    # ── 连接 ──────────────────────────────────────────────

    def connect(self, host: str, nick: str) -> str:
        """
        连接到服务器并发送昵称
        成功返回 None，失败返回错误消息字符串
        """
        self.nick = nick
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8)
            sock.connect((host, CHAT_PORT))
            sock.settimeout(None)
            self.tcp_sock = sock

            # 发送昵称
            send_json(sock, {"type": "nick", "nick": nick})

            # 等待服务器响应（user 列表 或 error）
            resp = recv_json(sock)
            if resp is None:
                return "服务器无响应"

            if resp.get("type") == "error":
                return resp.get("text", "昵称被拒绝")

            if resp.get("type") != "users":
                return f"协议错误: 期望 user 列表，收到 {resp.get('type')}"

            self.connected = True
            if self.on_users:
                self.on_users(resp["users"])

            # 启动接收线程
            self.recv_thread = threading.Thread(
                target=self._recv_loop, daemon=True
            )
            self.recv_thread.start()
            return None  # 成功

        except socket.timeout:
            return "连接超时，请检查服务器是否开启"
        except ConnectionRefusedError:
            return "连接被拒绝，服务器可能未开启"
        except OSError as e:
            return f"连接失败: {e}"
        except Exception as e:
            return f"未知错误: {e}"

    def disconnect(self):
        self.connected = False
        if self.tcp_sock:
            try:
                self.tcp_sock.close()
            except OSError:
                pass

    # ── 发送 ──────────────────────────────────────────────

    def send_chat(self, text: str):
        """发送文字消息"""
        if not self.connected:
            return
        try:
            send_json(self.tcp_sock, {"type": "chat", "text": text})
        except OSError:
            self._handle_disconnect()

    def send_rename(self, new_nick: str):
        """发送改名请求"""
        if not self.connected:
            return
        try:
            send_json(self.tcp_sock, {"type": "rename", "new_nick": new_nick})
        except OSError:
            self._handle_disconnect()

    def send_private(self, to: str, text: str):
        """发送私聊消息"""
        if not self.connected:
            return
        try:
            send_json(self.tcp_sock, {"type": "private_chat", "to": to, "text": text})
        except OSError:
            self._handle_disconnect()

    def send_private_file_start(self, to: str, file_id: str, filename: str, size: int, file_hash: str = "", is_folder: bool = False):
        """通知服务器开始私发文件"""
        try:
            send_json(self.tcp_sock, {
                "type": "private_file_start",
                "to": to,
                "file_id": file_id,
                "filename": filename,
                "size": size,
                "hash": file_hash,
                "is_folder": is_folder,
            })
        except OSError:
            self._handle_disconnect()

    def send_private_file_end(self, to: str, file_id: str):
        """通知服务器私发文件完成"""
        try:
            send_json(self.tcp_sock, {
                "type": "private_file_end",
                "to": to,
                "file_id": file_id,
            })
        except OSError:
            self._handle_disconnect()

    def send_file_chunk(self, chunk: bytes):
        """发送文件二进制块"""
        if not self.connected:
            return
        try:
            send_file_chunk(self.tcp_sock, chunk)
        except OSError:
            self._handle_disconnect()

    def send_file_start(self, file_id: str, filename: str, size: int, file_hash: str = "", is_folder: bool = False):
        """通知服务器开始传文件，附带 SHA-256 哈希用于完整性校验"""
        try:
            send_json(self.tcp_sock, {
                "type": "file_start",
                "file_id": file_id,
                "filename": filename,
                "size": size,
                "hash": file_hash,
                "is_folder": is_folder,
            })
        except OSError:
            self._handle_disconnect()

    def send_file_end(self, file_id: str):
        """通知服务器文件传输完成"""
        try:
            send_json(self.tcp_sock, {
                "type": "file_end",
                "file_id": file_id,
            })
        except OSError:
            self._handle_disconnect()

    # ── 接收循环 ─────────────────────────────────────────

    def _recv_loop(self):
        while self.connected:
            try:
                msg_type, payload = recv_frame(self.tcp_sock)
            except (ConnectionError, OSError):
                self._handle_disconnect()
                return

            if msg_type is None:
                self._handle_disconnect()
                return

            if msg_type == MSG_TYPE_FILE:
                # 文件块 → 解析 file_id 后回调 UI
                try:
                    fid, chunk_data = parse_file_chunk(payload)
                    if self.on_file_chunk:
                        self.on_file_chunk(fid, chunk_data)
                except (ValueError, IndexError):
                    pass  # 忽略损坏的块
            else:
                # JSON 消息
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                msg_type_str = msg.get("type")

                if msg_type_str == "users":
                    if self.on_users:
                        self.on_users(msg["users"])
                    # 代理信息变化时通知 UI
                    proxy_port = msg.get("proxy_port", 0)
                    if self.on_proxy_info and proxy_port != getattr(self, "_last_proxy_port", -1):
                        self._last_proxy_port = proxy_port
                        self.on_proxy_info(proxy_port)

                if msg_type_str in ("chat", "system", "file_start", "file_end",
                                    "private_chat", "private_file_start", "private_file_end"):
                    if self.on_message:
                        self.on_message(msg)

                if msg_type_str == "rename_ok" and self.on_rename_ok:
                    self.on_rename_ok(msg["new_nick"])

    def _handle_disconnect(self):
        self.connected = False
        try:
            self.tcp_sock.close()
        except OSError:
            pass
        if self.on_disconnect:
            self.on_disconnect()


# ── UDP 自动发现 ────────────────────────────────────────

class DiscoveryListener:
    """
    监听 UDP 广播发现房间
    回调: on_room_found(ip: str, room_name: str)
          on_room_lost(ip: str)
    """

    def __init__(self):
        self.running = False
        self.sock = None
        self.thread = None
        self._seen = {}  # ip -> (time, proxy_port)

    def start(self, on_found, on_lost=None):
        """启动 UDP 监听线程"""
        self.running = True
        self.on_found = on_found
        self.on_lost = on_lost
        self._seen = {}
        self.thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def _listen_loop(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.settimeout(1)
            self.sock.bind(("", DISCOVERY_PORT))
        except OSError:
            return

        while self.running:
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                # 定期清理超时的房间（10 秒没收到广播视为下线）
                now = time.time()
                lost = [ip for ip, (t, _) in self._seen.items() if now - t > 10]
                for ip in lost:
                    del self._seen[ip]
                    if self.on_lost:
                        self.on_lost(ip)
                continue
            except OSError:
                break

            room_name, proxy_port = parse_discovery_packet(data)
            ip = addr[0]

            if ip not in self._seen:
                self._seen[ip] = (time.time(), proxy_port)
                if self.on_found:
                    self.on_found(ip, room_name, proxy_port)
            else:
                self._seen[ip] = (time.time(), proxy_port)  # 刷新时间戳和代理信息
