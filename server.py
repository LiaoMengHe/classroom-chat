"""
server.py — 聊天室服务器

职责：
  1. 接受 TCP 客户端连接
  2. 管理在线用户列表
  3. 广播聊天消息、系统通知
  4. 中继文件传输（分块转发）
  5. UDP 广播自己的存在（自动发现）
"""

import json
import socket
import threading
import time

from protocol import (
    DISCOVERY_PORT, CHAT_PORT, BROADCAST_IP,
    send_json, recv_json, send_file_chunk, recv_frame,
    MSG_TYPE_FILE, make_discovery_packet, parse_discovery_packet,
)


class ChatServer:
    def __init__(self, room_name: str, host: str = "0.0.0.0"):
        self.room_name = room_name
        self.host = host

        # TCP 聊天服务器
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind((host, CHAT_PORT))
        self.tcp_sock.listen(32)

        # 客户端连接表 {sock: nick}
        self.clients: dict[socket.socket, str] = {}
        self.clients_lock = threading.Lock()

        # UDP 发现广播
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.udp_sock.settimeout(1)

        self.running = True
        self._proxy_port = 0  # SOCKS5 代理端口，0=未开启

        # 私聊文件路由表 {file_id: target_sock}
        self._private_files = {}
        self._private_files_lock = threading.Lock()


    def set_proxy_port(self, port: int):
        """设置 SOCKS5 代理端口（影响 UDP 广播内容）"""
        self._proxy_port = port

    # ── 启动 ──────────────────────────────────────────────

    def start(self):
        """启动服务器线程（不阻塞）"""
        threading.Thread(target=self._accept_loop, daemon=True, name="TCP-Accept").start()
        threading.Thread(target=self._discovery_loop, daemon=True, name="UDP-Discovery").start()
        print(f"[服务器] 房间「{self.room_name}」已创建")
        print(f"[服务器] TCP 监听 :{CHAT_PORT}")
        print(f"[服务器] UDP 广播 :{DISCOVERY_PORT}")

    def stop(self):
        self.running = False
        for sock in list(self.clients):
            try:
                sock.close()
            except OSError:
                pass
        try:
            self.tcp_sock.close()
        except OSError:
            pass
        try:
            self.udp_sock.close()
        except OSError:
            pass

    # ── 接受连接 ──────────────────────────────────────────

    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self.tcp_sock.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(sock, addr),
                    daemon=True,
                    name=f"Client-{addr[0]}",
                ).start()
            except OSError:
                break

    # ── 客户端处理 ────────────────────────────────────────

    def _handle_client(self, sock: socket.socket, addr):
        """处理单个客户端：等待昵称 → 加入 → 循环处理消息"""
        # 第一步：接收昵称
        nick_msg = recv_json(sock)
        if nick_msg is None or nick_msg.get("type") != "nick":
            sock.close()
            return
        nick = nick_msg["nick"]

        # 第二步：注册
        with self.clients_lock:
            # 检查昵称是否已存在
            existing_nicks = set(self.clients.values())
            if nick in existing_nicks:
                try:
                    send_json(sock, {"type": "error", "text": f"昵称「{nick}」已被使用"})
                except OSError:
                    pass
                sock.close()
                return
            self.clients[sock] = nick

        # ★ 先单独给新客户端发送用户列表（保证它收到的第一条消息是 users）
        try:
            send_json(sock, {"type": "users", "users": list(self.clients.values())})
        except OSError:
            pass

        # 再广播给所有人（包含新用户的系统通知 + 更新后的列表）
        self._broadcast({"type": "system", "text": f"{nick} 加入了房间"}, exclude=sock)
        self._broadcast_users()
        print(f"[连接] {nick} ({addr[0]}) 加入了房间")

        # 第三步：消息循环
        try:
            while self.running:
                msg_type, payload = recv_frame(sock)
                if msg_type is None:
                    break

                if msg_type == MSG_TYPE_FILE:
                    # 文件块 → 判断是否为私聊文件
                    fid = payload[:36].decode("ascii", errors="replace")
                    with self._private_files_lock:
                        target = self._private_files.get(fid)
                    if target:
                        try:
                            send_file_chunk(target, payload)
                        except OSError:
                            pass
                    else:
                        self._broadcast_file_chunk(payload, sender=sock)
                else:
                    # JSON 消息
                    msg = json.loads(payload.decode("utf-8"))
                    self._handle_json(msg, sock, nick)
                    # 改名后刷新本地 nick，防止后续消息使用旧昵称
                    if msg.get("type") == "rename":
                        with self.clients_lock:
                            nick = self.clients.get(sock, nick)
        except (ConnectionError, OSError, json.JSONDecodeError):
            pass
        finally:
            self._remove_client(sock, nick)

    def _handle_json(self, msg: dict, sock: socket.socket, nick: str):
        msg_type = msg.get("type")

        if msg_type == "chat":
            self._broadcast({
                "type": "chat",
                "from": nick,
                "text": msg["text"],
            })

        elif msg_type == "rename":
            new_nick = msg["new_nick"].strip()
            if not new_nick or new_nick == nick:
                return
            # 检查新昵称是否已被使用
            with self.clients_lock:
                if new_nick in set(self.clients.values()):
                    try:
                        send_json(sock, {"type": "error", "text": f"昵称「{new_nick}」已被使用"})
                    except OSError:
                        pass
                    return
                # 更新昵称
                self.clients[sock] = new_nick
            # 广播改名通知
            self._broadcast({"type": "system", "text": f"{nick} 改名为 {new_nick}"})
            self._broadcast_users()
            # 单独通知改名者确认成功
            try:
                send_json(sock, {"type": "rename_ok", "new_nick": new_nick})
            except OSError:
                pass
            print(f"[改名] {nick} → {new_nick}")

        elif msg_type == "file_start":
            # 中继文件开始通知（包含哈希校验值和文件夹标记）
            file_id = msg["file_id"]
            self._broadcast({
                "type": "file_start",
                "from": nick,
                "file_id": file_id,
                "filename": msg["filename"],
                "size": msg["size"],
                "hash": msg.get("hash", ""),
                "is_folder": msg.get("is_folder", False),
            }, exclude=sock)

        elif msg_type == "file_end":
            self._broadcast({
                "type": "file_end",
                "from": nick,
                "file_id": msg["file_id"],
            }, exclude=sock)

        elif msg_type == "private_chat":
            # 私聊：查找目标用户 socket 直接发送
            target_nick = msg.get("to", "")
            if not target_nick:
                return
            target_sock = None
            with self.clients_lock:
                for s, n in self.clients.items():
                    if n == target_nick:
                        target_sock = s
                        break
            if target_sock:
                try:
                    send_json(target_sock, {
                        "type": "private_chat",
                        "from": nick,
                        "text": msg["text"],
                    })
                except OSError:
                    pass

        elif msg_type == "private_file_start":
            target_nick = msg.get("to", "")
            if not target_nick:
                return
            target_sock = None
            with self.clients_lock:
                for s, n in self.clients.items():
                    if n == target_nick:
                        target_sock = s
                        break
            if target_sock:
                file_id = msg["file_id"]
                with self._private_files_lock:
                    self._private_files[file_id] = target_sock
                try:
                    send_json(target_sock, {
                        "type": "private_file_start",
                        "from": nick,
                        "file_id": file_id,
                        "filename": msg["filename"],
                        "size": msg["size"],
                        "hash": msg.get("hash", ""),
                        "is_folder": msg.get("is_folder", False),
                    })
                except OSError:
                    pass

        elif msg_type == "private_file_end":
            target_nick = msg.get("to", "")
            if not target_nick:
                return
            target_sock = None
            with self.clients_lock:
                for s, n in self.clients.items():
                    if n == target_nick:
                        target_sock = s
                        break
            if target_sock:
                file_id = msg["file_id"]
                with self._private_files_lock:
                    self._private_files.pop(file_id, None)
                try:
                    send_json(target_sock, {
                        "type": "private_file_end",
                        "from": nick,
                        "file_id": file_id,
                    })
                except OSError:
                    pass

    def _remove_client(self, sock: socket.socket, nick: str):
        with self.clients_lock:
            self.clients.pop(sock, None)
        try:
            sock.close()
        except OSError:
            pass
        self._broadcast({"type": "system", "text": f"{nick} 离开了房间"})
        self._broadcast_users()
        print(f"[断开] {nick} 离开了房间")

    # ── 广播 ──────────────────────────────────────────────

    def _broadcast(self, msg: dict, exclude: socket.socket = None):
        """广播 JSON 消息给所有客户端（可选排除发送者）"""
        dead = []
        with self.clients_lock:
            for sock in self.clients:
                if sock is exclude:
                    continue
                try:
                    send_json(sock, msg)
                except OSError:
                    dead.append(sock)
            # 清理死 socket
            for sock in dead:
                nick = self.clients.pop(sock, None)
                try:
                    sock.close()
                except OSError:
                    pass
                if nick:
                    print(f"[断开] {nick} 离开了房间 (send 失败)")
        # 如果有死 socket，通知其他客户端用户已离开
        if dead:
            self._broadcast_users()

    def _broadcast_file_chunk(self, chunk: bytes, sender: socket.socket):
        """广播文件二进制块"""
        dead = []
        with self.clients_lock:
            for sock in self.clients:
                if sock is sender:
                    continue
                try:
                    send_file_chunk(sock, chunk)
                except OSError:
                    dead.append(sock)
            # 清理死 socket
            for sock in dead:
                nick = self.clients.pop(sock, None)
                try:
                    sock.close()
                except OSError:
                    pass
                if nick:
                    print(f"[断开] {nick} 离开了房间 (send 失败)")
        if dead:
            self._broadcast_users()

    def _broadcast_users(self):
        """发送当前在线用户列表给所有客户端"""
        with self.clients_lock:
            users = list(self.clients.values())
        self._broadcast({"type": "users", "users": users})

    # ── UDP 发现广播 ──────────────────────────────────────

    def _discovery_loop(self):
        """每隔 2 秒广播房间信息（含代理端口）"""
        while self.running:
            packet = make_discovery_packet(self.room_name, self._proxy_port)
            try:
                self.udp_sock.sendto(packet, (BROADCAST_IP, DISCOVERY_PORT))
            except OSError:
                pass
            time.sleep(2)

# 单独运行测试
if __name__ == "__main__":
    srv = ChatServer("测试房间")
    srv.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
