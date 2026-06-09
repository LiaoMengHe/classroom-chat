"""
proxy.py — SOCKS5 代理服务器（纯 Python socket，零外部依赖）

用法:
    proxy = Socks5Proxy(host="0.0.0.0", port=10800)
    proxy.start()
    ...
    proxy.stop()

支持: CONNECT (TCP 转发)，无认证
"""

import socket
import threading

from protocol import SOCKS5_PORT


class Socks5Proxy:
    def __init__(self, host="0.0.0.0", port=SOCKS5_PORT):
        self.host = host
        self.port = port
        self._sock = None
        self._running = False
        self._thread = None

    def start(self):
        """启动 SOCKS5 代理服务器（非阻塞）"""
        if self._running:
            return
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(32)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="SOCKS5-Accept")
        self._thread.start()
        return True

    def stop(self):
        """停止代理服务器"""
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def running(self):
        return self._running

    def _accept_loop(self):
        while self._running:
            try:
                client, addr = self._sock.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(client,),
                    daemon=True,
                ).start()
            except OSError:
                break

    def _handle_client(self, client: socket.socket):
        """处理单个 SOCKS5 客户端连接"""
        try:
            # 1. 协商：客户端发 VER+NMETHODS+METHODS
            data = client.recv(2)
            if len(data) < 2:
                return
            ver, nmethods = data[0], data[1]
            if ver != 5:
                return
            # 读取 methods
            methods = client.recv(nmethods)
            if not methods:
                return
            # 回复：无认证 (0x00)
            client.sendall(b"\x05\x00")

            # 2. 请求：VER+CMD+RSV+ATYP+DST.ADDR+DST.PORT
            header = client.recv(4)
            if len(header) < 4:
                return
            ver, cmd, rsv, atyp = header[0], header[1], header[2], header[3]
            if ver != 5 or cmd != 0x01:  # 只支持 CONNECT
                client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)  # 不支持的命令
                return

            # 解析目标地址
            if atyp == 0x01:  # IPv4
                addr_bytes = client.recv(4)
                host = socket.inet_ntoa(addr_bytes)
            elif atyp == 0x03:  # 域名
                name_len = client.recv(1)[0]
                host = client.recv(name_len).decode("utf-8")
            elif atyp == 0x04:  # IPv6
                host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
            else:
                client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                return

            port_bytes = client.recv(2)
            port = (port_bytes[0] << 8) | port_bytes[1]

            # 3. 连接目标
            try:
                remote = socket.socket(socket.AF_INET6 if atyp == 0x04 else socket.AF_INET, socket.SOCK_STREAM)
                remote.settimeout(15)
                remote.connect((host, port))
                remote.settimeout(None)
            except Exception:
                client.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)  # 连接拒绝
                return

            # 4. 回复成功
            bind_addr = client.getsockname()
            if isinstance(bind_addr[0], bytes):
                bind_ip = bind_addr[0]
            else:
                bind_ip = socket.inet_aton(bind_addr[0])
            resp = b"\x05\x00\x00\x01" + bind_ip + bytes([bind_addr[1] >> 8, bind_addr[1] & 0xFF])
            client.sendall(resp)

            # 5. 双向数据转发
            self._relay(client, remote)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _relay(self, client: socket.socket, remote: socket.socket):
        """双向转发数据（隧道模式）"""
        closed = [False]

        def forward(src, dst, name):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                if not closed[0]:
                    closed[0] = True
                    try:
                        client.close()
                    except Exception:
                        pass
                    try:
                        remote.close()
                    except Exception:
                        pass

        t1 = threading.Thread(target=forward, args=(client, remote, "C→R"), daemon=True)
        t2 = threading.Thread(target=forward, args=(remote, client, "R→C"), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
