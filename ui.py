"""
ui.py — 所有界面（tkinter，零外部依赖）

页面：
  1. StartPage  — 创建/加入房间 + 设置下载目录
  2. ChatPage   — 聊天主界面（用户列表 + 消息 + 输入 + 拖拽）
"""

import os
import re
import socket
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
import zipfile
import platform
import uuid
import ctypes
from ctypes import wintypes

# ── Win32 API 函数类型声明（64 位兼容） ──────────────────
# 不声明 argtypes 时 ctypes 默认用 c_int（32位），64 位指针会被截断导致崩溃
_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32

# GetCursorPos: BOOL GetCursorPos(LPPOINT)
_user32.GetCursorPos.argtypes = [ctypes.c_void_p]
_user32.GetCursorPos.restype = wintypes.BOOL

# GetForegroundWindow: HWND GetForegroundWindow()
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND

# FlashWindow: BOOL FlashWindow(HWND, BOOL)
_user32.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
_user32.FlashWindow.restype = wintypes.BOOL

# FlashWindowEx: BOOL FlashWindowEx(PFLASHWINFO)
_user32.FlashWindowEx.argtypes = [ctypes.c_void_p]
_user32.FlashWindowEx.restype = wintypes.BOOL

# DragQueryFileW: UINT DragQueryFileW(HDROP, UINT, LPWSTR, UINT)
_shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
_shell32.DragQueryFileW.restype = wintypes.UINT

from client import ChatClient, DiscoveryListener
from server import ChatServer
from protocol import make_file_chunk, compute_file_hash, SOCKS5_PORT
from proxy import Socks5Proxy, HttpConnectProxy, HTTP_PROXY_PORT
import history


# ── 配色（浅色简洁） ─────────────────────────────────────
COLOR_BG = "#F5F5F5"
COLOR_WHITE = "#FFFFFF"
COLOR_ACCENT = "#4A90D9"
COLOR_ACCENT_HOVER = "#357ABD"
COLOR_TEXT = "#333333"
COLOR_TEXT_LIGHT = "#888888"
COLOR_BORDER = "#E0E0E0"
COLOR_SYSTEM = "#E8E8E8"
COLOR_FILE_TAG = "#E3F2FD"
COLOR_FILE_TAG_BORDER = "#BBDEFB"
COLOR_MY_MSG = "#DCF8C6"
COLOR_OTHER_MSG = "#FFFFFF"


class App(tk.Tk):
    """主应用窗口"""

    def __init__(self):
        super().__init__()
        self.title("局域网聊天室")
        self.geometry("820x580")
        self.minsize(700, 500)
        self.configure(bg=COLOR_BG)

        # 设置（下载目录）
        self.download_dir = str(Path.home() / "Downloads" / "ChatRoom")
        os.makedirs(self.download_dir, exist_ok=True)

        # 服务器 + 客户端引用
        self.server: ChatServer = None
        self.client: ChatClient = None
        self.discovery: DiscoveryListener = None

        # 在 ChatPage 创建之前收到的用户列表（用于修复首次进入不显示的问题）
        self._pending_users: list = None

        # SOCKS5 代理
        self.proxy: Socks5Proxy = None
        self.proxy_enabled = False

        # 当前页面引用
        self.current_page = None
        # 记住上次输入的房间名和昵称（离开后保留）
        self._last_room = socket.gethostname()
        self._last_nick = f"同学{os.environ.get('COMPUTERNAME', '')[-4:]}"
        self._show_start_page()

        # 点击关闭按钮时彻底退出
        self.protocol("WM_DELETE_WINDOW", self._on_close)


    # ── 接收拖拽文件（由 Ctrl+V 回调） ────────────────────

    def _on_files_dropped(self, paths: list):
        """从剪贴板粘贴接收文件（支持文件和文件夹）"""
        page = self.current_page
        if not isinstance(page, ChatPage):
            return

        files_added = False
        for path in paths:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                if path not in [pf for pf, _, _ in page.pending_files]:
                    page.pending_files.append((path, os.path.basename(path), False))
                    files_added = True
            elif os.path.isdir(path):
                # 文件夹需要压缩后发送
                folder_name = os.path.basename(path)
                page._add_system_msg(f"📁 正在压缩文件夹「{folder_name}」...")
                threading.Thread(
                    target=page._compress_folder_background,
                    args=(path, folder_name),
                    daemon=True,
                ).start()

        if files_added:
            page._refresh_file_tags()

    # ── 页面切换 ─────────────────────────────────────────

    def _clear_page(self):
        if self.current_page:
            self.current_page.destroy()
            self.current_page = None

    def _show_start_page(self):
        self._clear_page()
        self.current_page = StartPage(self, self._on_create_room, self._on_join_room)
        self.current_page.pack(fill="both", expand=True)

    def _show_chat_page(self, room_name: str, nick: str, proxy_ip=None, proxy_port=0, server_ip=None):
        self._clear_page()
        self.current_page = ChatPage(self, room_name, nick, self._on_leave_room,
                                      proxy_ip=proxy_ip, proxy_port=proxy_port,
                                      server_ip=server_ip)
        self.current_page.pack(fill="both", expand=True)
        # 应用在 ChatPage 创建之前缓存的用户列表
        if self._pending_users is not None:
            self.current_page.on_users_updated(self._pending_users)
            self._pending_users = None
        # 加载聊天历史
        self.after(100, self.current_page.load_history)

    # ── 房间操作 ─────────────────────────────────────────

    def _on_create_room(self, room_name: str, nick: str):
        """用户点击「创建房间」后调用"""
        # 1. 启动服务器
        self.server = ChatServer(room_name)
        self.server.start()

        # 2. 连接客户端到本地
        self.client = ChatClient()
        self.client.on_message = self._on_client_message
        self.client.on_file_chunk = self._on_file_chunk
        self.client.on_users = self._on_users_update
        self.client.on_disconnect = self._on_client_disconnect
        self.client.on_rename_ok = self._on_rename_ok
        self.client.on_proxy_info = self._on_proxy_info

        err = self.client.connect("127.0.0.1", nick)
        if err:
            messagebox.showerror("连接失败", err)
            self.server.stop()
            self.server = None
            self.client = None
            return

        # 3. 切换到聊天界面
        self._show_chat_page(room_name, nick, server_ip="127.0.0.1")

    def _on_join_room(self, ip: str, room_name: str, nick: str, proxy_port: int = 0):
        """用户选择了一个房间加入"""
        self.client = ChatClient()
        self.client.on_message = self._on_client_message
        self.client.on_file_chunk = self._on_file_chunk
        self.client.on_users = self._on_users_update
        self.client.on_disconnect = self._on_client_disconnect
        self.client.on_rename_ok = self._on_rename_ok
        self.client.on_proxy_info = self._on_proxy_info

        err = self.client.connect(ip, nick)
        if err:
            messagebox.showerror("加入失败", err)
            if self.discovery:
                self.discovery.stop()
                self.discovery = None
            self.client = None
            return

        self._show_chat_page(room_name, nick, proxy_ip=ip if proxy_port else None, proxy_port=proxy_port, server_ip=ip)

    def _on_leave_room(self):
        """离开房间"""
        if self.client:
            self.client.disconnect()
            self.client = None
        if self.server:
            self.server.stop()
            self.server = None
        if self.discovery:
            self.discovery.stop()
            self.discovery = None
        # 关闭代理
        self._stop_proxy()
        # 关闭系统代理
        self._disable_system_proxy()
        # 清理当前页面残留的临时文件
        if isinstance(self.current_page, ChatPage):
            self.current_page._cleanup_temp_zips(self.download_dir)
        self._show_start_page()

    # ── 客户端回调 ───────────────────────────────────────

    def _toggle_proxy(self):
        """开启/关闭 SOCKS5 网络共享"""
        if self.proxy_enabled:
            self._stop_proxy()
        else:
            self._start_proxy()

    def _start_proxy(self):
        """启动 SOCKS5 + HTTP CONNECT 代理并通知服务器广播"""
        try:
            socks5 = Socks5Proxy(port=SOCKS5_PORT)
            socks5_ok = socks5.start()
            http_proxy = HttpConnectProxy(port=HTTP_PROXY_PORT)
            http_ok = http_proxy.start()
            if socks5_ok or http_ok:
                self.proxy = socks5
                self.http_proxy = http_proxy
                self.proxy_enabled = True
                if self.server:
                    # UDP 广播用 HTTP 代理端口（浏览器直接支持）
                    self.server.set_proxy_port(HTTP_PROXY_PORT)
                if isinstance(self.current_page, ChatPage):
                    self.current_page.on_proxy_status(True, HTTP_PROXY_PORT)
                    self.current_page._add_system_msg("🌐 已开启网络共享 (HTTP :10801 / SOCKS5 :10800)")
        except Exception as e:
            if isinstance(self.current_page, ChatPage):
                self.current_page._add_system_msg(f"❌ 开启网络共享失败: {e}")

    def _stop_proxy(self):
        """停止所有代理"""
        if hasattr(self, 'http_proxy') and self.http_proxy:
            self.http_proxy.stop()
            self.http_proxy = None
        if self.proxy:
            self.proxy.stop()
            self.proxy = None
        self.proxy_enabled = False
        if self.server:
            self.server.set_proxy_port(0)
        if isinstance(self.current_page, ChatPage):
            self.current_page.on_proxy_status(False, 0)
            self.current_page._add_system_msg("🌐 已关闭网络共享")

    def _enable_system_proxy(self, ip: str, port: int):
        """设置 Windows 系统代理为 HTTP（浏览器 100% 生效）"""
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                proxy_server = f"{ip}:{port}"
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_server)
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            if isinstance(self.current_page, ChatPage):
                self.current_page._add_system_msg(f"🌐 已启用系统代理 ({ip}:{port})")
                self.current_page._add_system_msg("💡 浏览器已自动走共享网络，再次点击可关闭")
        except Exception as e:
            if isinstance(self.current_page, ChatPage):
                self.current_page._add_system_msg(f"❌ 设置系统代理失败: {e}")

    def _disable_system_proxy(self):
        """关闭 Windows 系统代理"""
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            if isinstance(self.current_page, ChatPage):
                self.current_page._add_system_msg("🌐 已关闭系统代理")
        except Exception:
            pass

    def _on_client_message(self, msg: dict):
        """收到聊天消息/系统通知/文件通知（后台线程调用，通过 after 调度到主线程）"""
        if isinstance(self.current_page, ChatPage):
            # 来信提示：别人发的聊天消息且窗口不在前台
            if msg.get("type") == "chat" and msg.get("from", "") != self.current_page.nick:
                sender = msg.get("from", "")
                text = msg.get("text", "")
                self.after(0, self._notify_incoming, sender, text)
            self.after(0, self.current_page.on_message_received, msg)

    def _notify_incoming(self, sender="", text=""):
        """来信提示：蜂鸣 + 窗口闪烁 + 系统通知"""
        # 1. 蜂鸣
        try:
            self.bell()
        except Exception:
            pass

        try:
            state = self.state()  # "normal" / "iconic" / "withdrawn"
        except Exception:
            state = "normal"

        # 2. 窗口闪烁（仅 Windows）
        try:
            import ctypes
            from ctypes import wintypes

            if state == "iconic":
                # 最小化 → FlashWindowEx 闪烁任务栏 + 系统通知
                hwnd = wintypes.HWND(self.winfo_id())
                class FLASHWINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.UINT),
                        ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD),
                    ]
                FLASHW_TRAY = 0x00000002
                FLASHW_TIMERNOFG = 0x0000000C
                info = FLASHWINFO()
                info.cbSize = ctypes.sizeof(FLASHWINFO)
                info.hwnd = hwnd
                info.dwFlags = FLASHW_TRAY | FLASHW_TIMERNOFG
                info.uCount = 0
                info.dwTimeout = 0
                ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
                if sender:
                    self._show_toast(sender, text)
            elif state == "normal":
                # 前台窗口不闪烁，非前台时也闪一下标题栏
                foreground = ctypes.windll.user32.GetForegroundWindow()
                if foreground and foreground != self.winfo_id():
                    ctypes.windll.user32.FlashWindow(self.winfo_id(), True)
        except Exception:
            pass

    def _show_toast(self, sender: str, text: str):
        """用 PowerShell 弹 Windows 原生系统通知"""
        import subprocess
        try:
            display_text = (text[:64] + "…") if len(text) > 64 else text
            # 转义 PowerShell 字符串中的特殊字符
            safe_sender = sender.replace("`", "``").replace("$", "`$").replace('"', '`"')
            safe_text = display_text.replace("`", "``").replace("$", "`$").replace('"', '`"')
            ps = (
                'Add-Type -AssemblyName System.Windows.Forms;'
                '$n=New-Object System.Windows.Forms.NotifyIcon;'
                '$n.Icon=[System.Drawing.SystemIcons]::Information;'
                '$n.BalloonTipIcon="Info";'
                '$n.BalloonTipTitle="' + safe_sender + '";'
                '$n.BalloonTipText="' + safe_text + '";'
                '$n.Visible=$true;'
                '$n.ShowBalloonTip(5000);'
                'Start-Sleep 5;'
                '$n.Dispose()'
            )
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                creationflags=0x08000000,
            )
        except Exception:
            pass

    def _on_file_chunk(self, file_id: str, chunk: bytes):
        """收到文件二进制数据（后台线程调用，调度到主线程以避免竞态）"""
        if isinstance(self.current_page, ChatPage):
            self.after(0, self.current_page.on_file_chunk_received, file_id, chunk)

    def _on_users_update(self, users: list):
        """用户列表更新（后台线程调用，调度到主线程）"""
        if isinstance(self.current_page, ChatPage):
            self.after(0, self.current_page.on_users_updated, users)
        else:
            # ChatPage 尚未创建，缓存列表供稍后使用
            self._pending_users = users

    def _on_client_disconnect(self):
        """连接断开"""
        if isinstance(self.current_page, ChatPage):
            self.after(200, lambda: self._handle_disconnect())

    def _on_rename_ok(self, new_nick: str):
        """改名成功（后台线程调用）"""
        if isinstance(self.current_page, ChatPage):
            self.after(0, self.current_page.on_rename_ok, new_nick)

    def _on_proxy_info(self, proxy_port: int):
        """房间代理信息更新（后台线程调用）"""
        if isinstance(self.current_page, ChatPage):
            self.after(0, self.current_page._update_proxy_info, proxy_port)

    def _handle_disconnect(self):
        if self.current_page and isinstance(self.current_page, ChatPage):
            messagebox.showinfo("连接断开", "房间已关闭或网络断开")
        self._on_leave_room()

    def _on_close(self):
        """程序关闭 — 询问最小化或退出"""
        dialog = tk.Toplevel(self)
        dialog.title("提示")
        dialog.geometry("380x150")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg=COLOR_WHITE)
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 380) // 2
        y = self.winfo_y() + (self.winfo_height() - 150) // 2
        dialog.geometry(f"+{x}+{y}")

        tk.Label(dialog, text="要做什么？",
                 font=("微软雅黑", 12, "bold"),
                 fg=COLOR_TEXT, bg=COLOR_WHITE).pack(pady=(18, 12))

        btn_frame = tk.Frame(dialog, bg=COLOR_WHITE)
        btn_frame.pack()

        tk.Button(btn_frame, text="—  最小化窗口",
                  font=("微软雅黑", 10),
                  bg=COLOR_ACCENT, fg="white",
                  activebackground=COLOR_ACCENT_HOVER,
                  bd=0, padx=14, pady=6, cursor="hand2",
                  command=lambda: self._do_minimize(dialog)
                  ).pack(side="left", padx=(0, 10))

        tk.Button(btn_frame, text="✕ 退出程序",
                  font=("微软雅黑", 10),
                  bg="#E74C3C", fg="white",
                  activebackground="#C0392B",
                  bd=0, padx=14, pady=6, cursor="hand2",
                  command=lambda: self._do_exit(dialog)
                  ).pack(side="left")

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    def _do_minimize(self, dialog=None):
        """最小化到任务栏"""
        if dialog:
            dialog.destroy()
        self.iconify()

    def _do_exit(self, dialog=None):
        """退出程序"""
        if dialog:
            dialog.destroy()
        self._on_leave_room()
        ChatPage._cleanup_temp_zips(self.download_dir)
        self.destroy()


# ═══════════════════════════════════════════════════════════
#  StartPage — 创建/加入房间
# ═══════════════════════════════════════════════════════════

class StartPage(tk.Frame):
    def __init__(self, app: App, on_create, on_join):
        super().__init__(app, bg=COLOR_BG)
        self.app = app
        self.on_create = on_create
        self.on_join = on_join

        # 发现到的房间列表 (ip -> room_name)
        self.found_rooms = {}
        self.discovery = DiscoveryListener()

        # 获取本机所有局域网 IP（用于过滤自己的广播）
        self.local_ips = self._get_local_ips()

        # 搜索状态
        self.is_searching = False

        self._build_ui()

    def destroy(self):
        """页面销毁时清理 UDP 监听器"""
        self.discovery.stop()
        super().destroy()

    def _build_ui(self):
        # 居中容器
        container = tk.Frame(self, bg=COLOR_WHITE, bd=1, relief="solid",
                             highlightbackground=COLOR_BORDER, highlightthickness=1)
        container.place(relx=0.5, rely=0.45, anchor="center")
        container_inner = tk.Frame(container, bg=COLOR_WHITE, padx=50, pady=30)
        container_inner.pack()

        # 标题
        tk.Label(container_inner, text="📡 局域网聊天室",
                 font=("微软雅黑", 22, "bold"),
                 fg=COLOR_ACCENT, bg=COLOR_WHITE).pack(pady=(0, 25))

        # ── 创建房间 ──
        tk.Label(container_inner, text="房间名:", font=("微软雅黑", 11),
                 fg=COLOR_TEXT, bg=COLOR_WHITE, anchor="w").pack(fill="x")
        self.room_entry = tk.Entry(container_inner, font=("微软雅黑", 11),
                                    bd=1, relief="solid", highlightthickness=0)
        self.room_entry.insert(0, self.app._last_room)
        self.room_entry.pack(fill="x", pady=(2, 10), ipady=4)

        tk.Label(container_inner, text="你的昵称:", font=("微软雅黑", 11),
                 fg=COLOR_TEXT, bg=COLOR_WHITE, anchor="w").pack(fill="x")
        self.nick_entry = tk.Entry(container_inner, font=("微软雅黑", 11),
                                    bd=1, relief="solid", highlightthickness=0)
        self.nick_entry.insert(0, self.app._last_nick)
        self.nick_entry.pack(fill="x", pady=(2, 15), ipady=4)

        self.create_btn = tk.Button(container_inner, text="🎮  创建房间",
                                     font=("微软雅黑", 12, "bold"),
                                     bg=COLOR_ACCENT, fg="white",
                                     activebackground=COLOR_ACCENT_HOVER,
                                     activeforeground="white",
                                     bd=0, padx=10, pady=6,
                                     cursor="hand2",
                                     command=self._do_create)
        self.create_btn.pack(fill="x", pady=(0, 20))

        # ── 分隔线 ──
        sep_frame = tk.Frame(container_inner, bg=COLOR_BORDER, height=1)
        sep_frame.pack(fill="x", pady=5)
        tk.Label(container_inner, text="— 或 —", font=("微软雅黑", 10),
                 fg=COLOR_TEXT_LIGHT, bg=COLOR_WHITE).pack()

        # ── 加入房间 ──
        self.join_btn = tk.Button(container_inner, text="🔍  加入房间",
                                   font=("微软雅黑", 12, "bold"),
                                   bg=COLOR_ACCENT, fg="white",
                                   activebackground=COLOR_ACCENT_HOVER,
                                   activeforeground="white",
                                   bd=0, padx=10, pady=6,
                                   cursor="hand2",
                                   command=self._start_search)
        self.join_btn.pack(fill="x", pady=(10, 5))

        # 房间列表
        self.room_list_frame = tk.Frame(container_inner, bg=COLOR_WHITE)
        self.room_list_frame.pack(fill="x", pady=(5, 0))

        # 搜索状态标签
        self.status_label = tk.Label(container_inner,
                                      text="点击「加入房间」搜索局域网",
                                      font=("微软雅黑", 9),
                                      fg=COLOR_TEXT_LIGHT, bg=COLOR_WHITE)
        self.status_label.pack(pady=(3, 0))

        # ── 底部设置 ──
        settings_frame = tk.Frame(container_inner, bg=COLOR_WHITE)
        settings_frame.pack(fill="x", pady=(15, 0))

        tk.Label(settings_frame, text="⚙ 下载目录:",
                 font=("微软雅黑", 9), fg=COLOR_TEXT_LIGHT,
                 bg=COLOR_WHITE).pack(side="left")

        dir_short = self._shorten_path(self.app.download_dir, 28)
        self.dir_label = tk.Label(settings_frame, text=dir_short,
                                   font=("微软雅黑", 9),
                                   fg=COLOR_ACCENT, bg=COLOR_WHITE,
                                   cursor="hand2")
        self.dir_label.pack(side="left", padx=(5, 0))
        self.dir_label.bind("<Button-1>", lambda e: self._change_dir())

    # ── 创建房间 ──────────────────────────────────────────

    def _do_create(self):
        room = self.room_entry.get().strip() or socket.gethostname()
        nick = self.nick_entry.get().strip()
        if not nick:
            messagebox.showwarning("提示", "请输入你的昵称")
            return
        # 记住输入值
        self.app._last_room = room
        self.app._last_nick = nick
        # 停止搜索并重置状态
        self.discovery.stop()
        self.is_searching = False
        self.join_btn.config(state="normal", text="🔍  加入房间")
        self.on_create(room, nick)

    # ── 搜索房间 ──────────────────────────────────────────

    def _start_search(self):
        if self.is_searching:
            return

        self.is_searching = True
        self.join_btn.config(state="disabled", text="🔍  正在搜索...")
        self.status_label.config(text="正在搜索局域网房间...")
        self.found_rooms.clear()

        # 清空旧列表
        for w in self.room_list_frame.winfo_children():
            w.destroy()

        # 启动 UDP 监听
        self.discovery.start(
            on_found=self._on_room_found,
            on_lost=self._on_room_lost,
        )

        # 5 秒后自动停止搜索
        self.after(5000, self._stop_search)

    def _stop_search(self):
        if not self.is_searching:
            return
        self.is_searching = False
        self.discovery.stop()
        self.join_btn.config(state="normal", text="🔄  刷新")

        count = len(self.found_rooms)
        if count == 0:
            self.status_label.config(text="未发现房间，点击「刷新」重试")
        else:
            self.status_label.config(text=f"发现 {count} 个房间，点击加入")

    def _on_room_found(self, ip: str, room_name: str, proxy_port: int = 0):
        """UDP 发现新房间（在后台线程调用）"""
        # 过滤自己的广播
        if ip in self.local_ips:
            return
        self.after(0, self._add_room_ui, ip, room_name, proxy_port)

    def _on_room_lost(self, ip: str):
        """房间消失"""
        self.after(0, self._remove_room_ui, ip)

    def _add_room_ui(self, ip: str, room_name: str, proxy_port: int = 0):
        if ip in self.found_rooms:
            return
        self.found_rooms[ip] = room_name

        frame = tk.Frame(self.room_list_frame, bg=COLOR_WHITE,
                         highlightbackground=COLOR_BORDER,
                         highlightthickness=1, bd=0, pady=2, padx=8)
        frame.pack(fill="x", pady=2)

        # 绿点 + 房间名
        dot = tk.Label(frame, text="🟢", font=("微软雅黑", 10), bg=COLOR_WHITE)
        dot.pack(side="left", padx=(0, 5))

        name_label = tk.Label(frame, text=room_name,
                               font=("微软雅黑", 11, "bold"),
                               fg=COLOR_TEXT, bg=COLOR_WHITE)
        name_label.pack(side="left")

        ip_label = tk.Label(frame, text=f"({ip})",
                             font=("微软雅黑", 9),
                             fg=COLOR_TEXT_LIGHT, bg=COLOR_WHITE)
        ip_label.pack(side="left", padx=(8, 0))

        # 如果有网络共享标记
        if proxy_port:
            proxy_label = tk.Label(frame, text="🌐共享",
                                   font=("微软雅黑", 9, "bold"),
                                   fg="#27AE60", bg="#E8F8E8",
                                   padx=4, pady=1)
            proxy_label.pack(side="left", padx=(6, 0))

        join_small_btn = tk.Button(frame, text="加入",
                                    font=("微软雅黑", 9),
                                    bg=COLOR_ACCENT, fg="white",
                                    bd=0, padx=8, pady=1,
                                    cursor="hand2",
                                    command=lambda i=ip, r=room_name: self._confirm_join(i, r))
        join_small_btn.pack(side="right", padx=(5, 0))

        # 存储引用以便移除
        frame.ip = ip
        frame._proxy_port = proxy_port

    def _remove_room_ui(self, ip: str):
        if ip in self.found_rooms:
            del self.found_rooms[ip]
        for w in self.room_list_frame.winfo_children():
            if getattr(w, "ip", None) == ip:
                w.destroy()

    def _confirm_join(self, ip: str, room_name: str):
        nick = self.nick_entry.get().strip()
        if not nick:
            messagebox.showwarning("提示", "请输入你的昵称")
            return
        # 获取该房间的代理端口
        proxy_port = 0
        for di in self.room_list_frame.winfo_children():
            if getattr(di, "ip", None) == ip and hasattr(di, "_proxy_port"):
                proxy_port = di._proxy_port
                break
        self.app._last_nick = nick
        self.app._last_room = room_name
        self.discovery.stop()
        self.is_searching = False
        self.on_join(ip, room_name, nick, proxy_port)

    # ── 设置 ──────────────────────────────────────────────

    def _change_dir(self):
        path = filedialog.askdirectory(initialdir=self.app.download_dir,
                                        title="选择文件下载目录")
        if path:
            self.app.download_dir = path
            os.makedirs(path, exist_ok=True)
            self.dir_label.config(text=self._shorten_path(path, 28))

    @staticmethod
    def _shorten_path(path: str, max_len: int) -> str:
        if len(path) <= max_len:
            return path
        return "..." + path[-(max_len - 3):]

    @staticmethod
    def _get_local_ips() -> set:
        """获取本机所有局域网 IP"""
        ips = {"127.0.0.1", "0.0.0.0"}
        try:
            hostname = socket.gethostname()
            ips.add(socket.gethostbyname(hostname))
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        # 枚举所有网卡
        try:
            addrs = socket.gethostbyname_ex(socket.gethostname())[2]
            ips.update(addrs)
        except Exception:
            pass
        return ips


# ═══════════════════════════════════════════════════════════
#  ChatPage — 聊天主界面
# ═══════════════════════════════════════════════════════════

class ChatPage(tk.Frame):
    def __init__(self, app: App, room_name: str, nick: str, on_leave, proxy_ip=None, proxy_port=0, server_ip=None):
        super().__init__(app, bg=COLOR_BG)
        self.app = app
        self.room_name = room_name
        self.nick = nick
        self.on_leave = on_leave

        # 代理信息
        self._proxy_ip = proxy_ip
        self._proxy_port = proxy_port
        self._server_ip = server_ip or proxy_ip  # 服务器IP，后开共享时使用
        self._proxy_active = False  # 系统代理开关状态

        # 在线用户列表
        self.users = []

        # 待发送文件列表 [(path, filename, is_folder)]
        self.pending_files = []

        # 文件接收状态 {file_id: {filename, size, received, path, fp}}
        self.file_recv = {}

        # Emoji 面板状态
        self._emoji_visible = False

        # 标记页面是否存活（用于全局滚轮绑定检查）
        self._alive = True

        self._build_ui()

        # 如果有代理信息，显示"使用共享"按钮
        if self._proxy_ip and self._proxy_port:
            self.use_proxy_btn.pack(side="left", padx=(8, 0))

    def destroy(self):
        """页面销毁时标记失效（让全局滚轮绑定静默）"""
        self._alive = False
        super().destroy()

    def _build_ui(self):
        # ── 顶部栏 ──
        top = tk.Frame(self, bg=COLOR_WHITE, bd=0,
                       highlightbackground=COLOR_BORDER, highlightthickness=1)
        top.pack(fill="x")

        tk.Label(top, text=f"📡  {self.room_name}",
                 font=("微软雅黑", 13, "bold"),
                 fg=COLOR_ACCENT, bg=COLOR_WHITE).pack(side="left", padx=12, pady=8)

        # 用户计数
        self.user_count_label = tk.Label(top, text="成员 (1)",
                                          font=("微软雅黑", 9),
                                          fg=COLOR_TEXT_LIGHT, bg=COLOR_WHITE)
        self.user_count_label.pack(side="left", padx=(8, 0))

        # 改名按钮
        rename_btn = tk.Button(top, text="✏️ 改名",
                               font=("微软雅黑", 9), bd=0,
                               bg=COLOR_WHITE, fg=COLOR_ACCENT,
                               activebackground=COLOR_BG,
                               cursor="hand2",
                               command=self._do_rename)
        rename_btn.pack(side="left", padx=(8, 0))

        # 网络共享按钮（仅房主显示）
        self.proxy_share_btn = tk.Button(top, text="🌐 共享网络",
                                         font=("微软雅黑", 9), bd=0,
                                         bg=COLOR_WHITE, fg="#27AE60",
                                         activebackground=COLOR_BG,
                                         cursor="hand2",
                                         command=self._toggle_proxy)
        # 默认隐藏，有 server 时才显示
        if self.app.server:
            self.proxy_share_btn.pack(side="left", padx=(8, 0))

        # 使用代理按钮（加入者点击启用系统代理）
        self.use_proxy_btn = tk.Button(top, text="🌐 使用共享",
                                       font=("微软雅黑", 9), bd=0,
                                       bg=COLOR_WHITE, fg=COLOR_ACCENT,
                                       activebackground=COLOR_BG,
                                       cursor="hand2",
                                       command=self._use_proxy)
        # 默认隐藏，发现房间有代理时显示
        self._proxy_room_ip = None
        self._proxy_room_port = 0

        # 打开下载目录
        dir_btn = tk.Button(top, text="📁 下载目录",
                            font=("微软雅黑", 9), bd=0,
                            bg=COLOR_WHITE, fg=COLOR_ACCENT,
                            activebackground=COLOR_BG,
                            cursor="hand2",
                            command=self._open_download_dir)
        dir_btn.pack(side="right", padx=(0, 5), pady=5)

        # 离开按钮
        leave_btn = tk.Button(top, text="✕  离开",
                              font=("微软雅黑", 9), bd=0,
                              bg=COLOR_WHITE, fg=COLOR_TEXT_LIGHT,
                              activebackground="#FFEBEE",
                              cursor="hand2",
                              command=self._do_leave)
        leave_btn.pack(side="right", padx=(0, 12), pady=5)

        # ── 主区域（用户列表 + 聊天区） ──
        main = tk.Frame(self, bg=COLOR_BG)
        main.pack(fill="both", expand=True, padx=8, pady=(6, 6))

        # 在线用户列表（左侧）
        self.user_frame = tk.Frame(main, bg=COLOR_WHITE, width=160,
                                    highlightbackground=COLOR_BORDER,
                                    highlightthickness=1)
        self.user_frame.pack(side="left", fill="y")
        self.user_frame.pack_propagate(False)

        tk.Label(self.user_frame, text="  在线用户",
                 font=("微软雅黑", 10, "bold"),
                 fg=COLOR_TEXT, bg=COLOR_WHITE,
                 anchor="w").pack(fill="x", padx=8, pady=(8, 4))

        # 用户列表（listbox）
        self.user_listbox = tk.Listbox(self.user_frame,
                                        font=("微软雅黑", 10),
                                        bg=COLOR_WHITE, fg=COLOR_TEXT,
                                        bd=0, highlightthickness=0,
                                        selectbackground="#E3F2FD",
                                        selectforeground=COLOR_TEXT,
                                        activestyle="none")
        self.user_listbox.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # 用户列表右键菜单（私聊）
        self._user_menu = tk.Menu(self.user_listbox, tearoff=0, bg=COLOR_WHITE, fg=COLOR_TEXT)
        self.user_listbox.bind("<Button-3>", self._on_user_right_click)

        # ── 右侧聊天区域 ──
        right = tk.Frame(main, bg=COLOR_WHITE,
                         highlightbackground=COLOR_BORDER,
                         highlightthickness=1)
        right.pack(side="right", fill="both", expand=True, padx=(6, 0))

        # 消息显示区域（带滚动）
        self.msg_frame = tk.Frame(right, bg=COLOR_WHITE)
        self.msg_frame.pack(fill="both", expand=True, padx=2, pady=(2, 0))

        self.msg_canvas = tk.Canvas(self.msg_frame, bg=COLOR_WHITE,
                                     highlightthickness=0, bd=0)
        self.msg_scroll = tk.Scrollbar(self.msg_frame, orient="vertical",
                                        command=self.msg_canvas.yview)
        self.msg_canvas.configure(yscrollcommand=self.msg_scroll.set)

        self.msg_scroll.pack(side="right", fill="y")
        self.msg_canvas.pack(side="left", fill="both", expand=True)

        # 消息容器（放在 canvas 内）
        self.msg_container = tk.Frame(self.msg_canvas, bg=COLOR_WHITE)
        self.msg_canvas.create_window((0, 0), window=self.msg_container,
                                       anchor="nw", tags="inner")

        # 绑定消息容器尺寸变化 -> 更新 scrollregion
        def _configure_container(event):
            self.msg_canvas.configure(scrollregion=self.msg_canvas.bbox("all"))
        self.msg_container.bind("<Configure>", _configure_container)

        # canvas 宽度变化 -> 同步给内部 frame
        def _configure_canvas(event):
            self.msg_canvas.itemconfig("inner", width=event.width)
        self.msg_canvas.bind("<Configure>", _configure_canvas)

        # ── 鼠标滚轮支持（全局捕获，任何子控件上悬停都能滚动） ──
        def _on_mousewheel(event):
            """Windows / macOS 滚轮"""
            if not self._alive:
                return
            self.msg_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        def _on_mousewheel_up(event):
            """Linux 滚轮上滚"""
            if not self._alive:
                return
            self.msg_canvas.yview_scroll(-3, "units")
            return "break"
        def _on_mousewheel_down(event):
            """Linux 滚轮下滚"""
            if not self._alive:
                return
            self.msg_canvas.yview_scroll(3, "units")
            return "break"

        # 用 bind_all 绑定到整个应用（不受子控件拦截），靠 _alive 标志防泄漏
        self.bind_all("<MouseWheel>", _on_mousewheel, add="+")
        self.bind_all("<Button-4>", _on_mousewheel_up, add="+")
        self.bind_all("<Button-5>", _on_mousewheel_down, add="+")
        # 也直接绑定到 canvas 和滚动条（兜底）
        self.msg_canvas.bind("<MouseWheel>", _on_mousewheel)
        self.msg_canvas.bind("<Button-4>", _on_mousewheel_up)
        self.msg_canvas.bind("<Button-5>", _on_mousewheel_down)
        self.msg_scroll.bind("<MouseWheel>", _on_mousewheel)
        self.msg_scroll.bind("<Button-4>", _on_mousewheel_up)
        self.msg_scroll.bind("<Button-5>", _on_mousewheel_down)

        # ── 文件标签区域（拖拽显示） ──
        self.file_tag_frame = tk.Frame(right, bg=COLOR_WHITE,
                                        highlightbackground=COLOR_BORDER,
                                        highlightthickness=0)
        # 默认隐藏
        self.file_tag_frame.pack_forget()

        self.file_tags_inner = tk.Frame(self.file_tag_frame, bg=COLOR_WHITE)
        self.file_tags_inner.pack(fill="x", padx=4, pady=2)

        # ── Emoji 快捷面板 ──
        self.emoji_frame = tk.Frame(right, bg=COLOR_WHITE)
        # 默认隐藏，点击按钮展开
        self.emoji_bar = tk.Frame(self.emoji_frame, bg=COLOR_WHITE)
        self.emoji_bar.pack(fill="x", padx=4, pady=1)
        EMOJIS = ["😊","😂","👍","❤️","🎉","🔥","🙌","😍","🤔","😭","✨","🥰","👏","💪","😤","🤣","🥺","😎"]
        for em in EMOJIS:
            btn = tk.Button(self.emoji_bar, text=em,
                            font=("微软雅黑", 12),
                            bd=0, bg=COLOR_WHITE, fg=COLOR_TEXT,
                            activebackground=COLOR_BG,
                            cursor="hand2",
                            width=2, height=1,
                            command=lambda e=em: self._insert_emoji(e))
            btn.pack(side="left", padx=1)

        # ── 底部输入区域 ──
        bottom = tk.Frame(right, bg=COLOR_WHITE,
                          highlightbackground=COLOR_BORDER,
                          highlightthickness=1)
        bottom.pack(fill="x")

        # 文件按钮
        file_btn = tk.Button(bottom, text="📎",
                              font=("微软雅黑", 14),
                              bd=0, bg=COLOR_WHITE, fg=COLOR_TEXT_LIGHT,
                              activebackground=COLOR_BG,
                              cursor="hand2",
                              command=self._pick_files)
        file_btn.pack(side="left", padx=(4, 2), pady=4)

        # 文件夹按钮
        folder_btn = tk.Button(bottom, text="📁",
                                font=("微软雅黑", 14),
                                bd=0, bg=COLOR_WHITE, fg=COLOR_TEXT_LIGHT,
                                activebackground=COLOR_BG,
                                cursor="hand2",
                                command=self._pick_folder)
        folder_btn.pack(side="left", padx=(0, 2), pady=4)

        # ★ 右侧按钮区域 — 先 pack，确保它始终可见 ★
        right_btn_frame = tk.Frame(bottom, bg=COLOR_WHITE)
        right_btn_frame.pack(side="right", padx=(0, 6))

        # 发送按钮
        self.send_btn = tk.Button(right_btn_frame, text="发送",
                                   font=("微软雅黑", 10, "bold"),
                                   bg=COLOR_ACCENT, fg="white",
                                   activebackground=COLOR_ACCENT_HOVER,
                                   activeforeground="white",
                                   bd=0, padx=12, pady=4,
                                   cursor="hand2",
                                   command=self._do_send)
        self.send_btn.pack(side="left")

        # 提示文字
        hint = tk.Label(right_btn_frame, text="Ctrl+V | 📁 发文件夹",
                        font=("微软雅黑", 8), fg=COLOR_TEXT_LIGHT,
                        bg=COLOR_WHITE)
        hint.pack(side="left", padx=(6, 0))

        # ★ 输入框 — 后 pack，只占据剩余空间 ★
        self.input_entry = tk.Text(bottom, height=2,
                                    font=("微软雅黑", 10),
                                    bd=1, relief="solid",
                                    highlightthickness=0,
                                    wrap="word",
                                    padx=6, pady=4)
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 4), pady=4)

        # Emoji 切换按钮（放在发送按钮左边）
        emoji_toggle = tk.Button(right_btn_frame, text="😊",
                                  font=("微软雅黑", 11),
                                  bd=0, bg=COLOR_WHITE, fg=COLOR_TEXT,
                                  activebackground=COLOR_BG,
                                  cursor="hand2",
                                  command=self._toggle_emoji)
        emoji_toggle.pack(side="left", padx=(0, 4))

        # 绑定 Enter 发送（Shift+Enter 换行）
        self.input_entry.bind("<Return>", self._on_enter_send)
        self.input_entry.bind("<Shift-Return>", self._on_shift_enter)
        # Ctrl+V 粘贴文件
        self.input_entry.bind("<Control-v>", self._on_ctrl_v)
        self.input_entry.bind("<Control-V>", self._on_ctrl_v)


    def _pick_files(self):
        """点击📎选择文件"""
        paths = filedialog.askopenfilenames(title="选择要发送的文件")
        for path in paths:
            if path not in [pf for pf, _, _ in self.pending_files]:
                self.pending_files.append((path, os.path.basename(path), False))
        self._refresh_file_tags()

    def _toggle_emoji(self):
        """切换显示/隐藏 emoji 面板"""
        if self._emoji_visible:
            self.emoji_frame.pack_forget()
            self._emoji_visible = False
        else:
            # 在 bottom 之前插入
            self.emoji_frame.pack(fill="x", before=self.emoji_frame.master.winfo_children()[-1])
            self._emoji_visible = True

    def _insert_emoji(self, em: str):
        """在输入框光标位置插入 emoji"""
        self.input_entry.insert("insert", em)
        self.input_entry.focus_set()

    def _pick_folder(self):
        """点击📁选择文件夹，后台压缩为 zip 后加入发送列表"""
        folder = filedialog.askdirectory(title="选择要发送的文件夹")
        if not folder:
            return
        folder_name = os.path.basename(folder)
        if not folder_name:
            return

        # 检查是否已添加（按文件夹名去重）
        for _, fn, is_f in self.pending_files:
            if is_f and fn == f"{folder_name}.zip":
                return

        self._add_system_msg(f"📁 正在压缩文件夹「{folder_name}」...")
        threading.Thread(
            target=self._compress_folder_background,
            args=(folder, folder_name),
            daemon=True,
        ).start()

    def _compress_folder_background(self, folder: str, folder_name: str):
        """在后台线程压缩文件夹，完成后添加到发送列表"""
        try:
            download_dir = self.app.download_dir
            os.makedirs(download_dir, exist_ok=True)
            zip_path = os.path.join(download_dir, f"_temp_{folder_name}_{int(time.time())}.zip")

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root_dir, dirs, files in os.walk(folder):
                    for file in files:
                        file_path = os.path.join(root_dir, file)
                        # 以 folder 自身为基准，避免解压时双重嵌套
                        arcname = os.path.relpath(file_path, folder)
                        zf.write(file_path, arcname)

            # 回到主线程更新 UI
            self.after(0, self._on_folder_compressed, zip_path, folder_name)
        except Exception as e:
            self.after(0, lambda: self._add_system_msg(f"❌ 文件夹压缩失败: {folder_name} - {e}"))

    def _on_folder_compressed(self, zip_path: str, folder_name: str):
        """文件夹压缩完成，添加到发送列表"""
        self.pending_files.append((zip_path, f"{folder_name}.zip", True))
        self._refresh_file_tags()
        self._add_system_msg(f"✅ 文件夹「{folder_name}」压缩完成")


    def _refresh_file_tags(self):
        """刷新文件标签栏"""
        for w in self.file_tags_inner.winfo_children():
            w.destroy()

        if not self.pending_files:
            self.file_tag_frame.pack_forget()
            return

        self.file_tag_frame.pack(fill="x", after=self.msg_frame)

        for idx, (fullpath, filename, is_folder) in enumerate(self.pending_files):
            tag = tk.Frame(self.file_tags_inner, bg=COLOR_FILE_TAG,
                           highlightbackground=COLOR_FILE_TAG_BORDER,
                           highlightthickness=1, bd=0, padx=6, pady=1)
            tag.pack(side="left", padx=(0, 4), pady=2)

            size = os.path.getsize(fullpath)
            size_str = self._format_size(size)

            icon = "📁" if is_folder else "📎"
            display_name = filename.rstrip(".zip") if is_folder else filename

            tk.Label(tag, text=f"{icon} {display_name} ({size_str})",
                     font=("微软雅黑", 9),
                     fg=COLOR_TEXT, bg=COLOR_FILE_TAG).pack(side="left")

            close_btn = tk.Label(tag, text=" ×",
                                  font=("微软雅黑", 9, "bold"),
                                  fg=COLOR_TEXT_LIGHT, bg=COLOR_FILE_TAG,
                                  cursor="hand2")
            close_btn.pack(side="left", padx=(4, 0))
            close_btn.bind("<Button-1>", lambda e, i=idx: self._remove_file(i))

    @staticmethod
    def _cleanup_temp_zips(custom_dir: str = None):
        """清理所有临时 zip / tmpzip 文件"""
        paths_to_rm = []
        dirs_to_check = []
        if custom_dir:
            dirs_to_check.append(custom_dir)
        default_dir = str(Path.home() / "Downloads" / "ChatRoom")
        if default_dir not in dirs_to_check:
            dirs_to_check.append(default_dir)

        for dl_dir in dirs_to_check:
            if os.path.isdir(dl_dir):
                for f in os.listdir(dl_dir):
                    # 发送端临时 zip（_temp_*.zip）和接收端临时 zip（*.tmpzip）
                    if (f.startswith("_temp_") and f.endswith(".zip")) or f.endswith(".tmpzip"):
                        paths_to_rm.append(os.path.join(dl_dir, f))

        for p in paths_to_rm:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass

    def _remove_file(self, idx: int):
        """从发送列表移除文件，同时删除对应的临时 zip"""
        if 0 <= idx < len(self.pending_files):
            fullpath, _, is_folder = self.pending_files.pop(idx)
            if is_folder:
                # 删除临时 zip
                try:
                    os.remove(fullpath)
                except OSError:
                    pass
        self._refresh_file_tags()

    # ── 发送 ──────────────────────────────────────────────

    def _on_enter_send(self, event):
        """Enter 发送，Shift+Enter 换行"""
        if not event.state & 0x0001:  # 没有 Shift
            self._do_send()
            return "break"  # 阻止换行
        return None

    def _on_shift_enter(self, event):
        """Shift+Enter 换行"""
        return None

    def _on_ctrl_v(self, event):
        """Ctrl+V：检测剪贴板是否有文件，有则添加"""
        # 仅 Windows 支持从剪贴板读取文件路径
        if platform.system() != "Windows":
            return None
        try:
            import ctypes.wintypes
            user32 = ctypes.windll.user32
            CF_HDROP = 15
            if user32.OpenClipboard(None):
                try:
                    if user32.IsClipboardFormatAvailable(CF_HDROP):
                        hdrop = user32.GetClipboardData(CF_HDROP)
                        if hdrop:
                            count = ctypes.windll.shell32.DragQueryFileW(hdrop, -1, None, 0)
                            paths = []
                            buf = ctypes.create_unicode_buffer(260)
                            for i in range(count):
                                ctypes.windll.shell32.DragQueryFileW(hdrop, i, buf, 260)
                                paths.append(buf.value)
                            if paths:
                                self.app._on_files_dropped(paths)
                                return "break"
                    return None
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass
        return None

    def _do_send(self):
        """发送文字 + 文件"""
        text = self.input_entry.get("1.0", "end-1c").strip()
        has_files = len(self.pending_files) > 0

        if not text and not has_files:
            return

        client = self.app.client
        if not client or not client.connected:
            messagebox.showwarning("提示", "已断开连接")
            return

        # 发送文字（如果有）
        if text:
            client.send_chat(text)

        # 发送文件（在后台线程进行，不阻塞 UI）
        if has_files:
            files_to_send = list(self.pending_files)
            self.pending_files.clear()
            self._refresh_file_tags()
            self._add_system_msg(f"📤 正在发送 {len(files_to_send)} 个文件...")
            threading.Thread(
                target=self._send_files_background,
                args=(client, files_to_send),
                daemon=True,
            ).start()

        # 清空输入框
        self.input_entry.delete("1.0", "end")

    def _send_files_background(self, client: ChatClient, files: list):
        """在后台线程发送多个文件"""
        for fullpath, filename, is_folder in files:
            try:
                self._send_file(client, fullpath, filename, is_folder)
                # 发送成功后，在主线程显示已发送消息（含图片缩略图预览）
                if not is_folder:
                    self.after(0, lambda fp=fullpath, fn=filename: self._add_sent_file_msg(fn, fp))
                else:
                    # 文件夹发送成功后清理临时 zip
                    try:
                        os.remove(fullpath)
                    except OSError:
                        pass
            except Exception as e:
                err_fn = filename
                self.after(0, lambda fn=err_fn: self._add_system_msg(
                    f"❌ 文件发送失败: {fn} - {e}"))
        self.after(0, lambda: self._add_system_msg("✅ 文件发送完成"))

    def _send_file(self, client: ChatClient, fullpath: str, filename: str, is_folder: bool = False, target_nick: str = None):
        """发送单个文件（分块），支持群发和私发"""
        file_id = str(uuid.uuid4())
        size = os.path.getsize(fullpath)
        file_hash = compute_file_hash(fullpath)
        chunk_size = 64 * 1024

        if target_nick:
            client.send_private_file_start(target_nick, file_id, filename, size, file_hash, is_folder=is_folder)
            with open(fullpath, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    client.send_file_chunk(make_file_chunk(file_id, chunk))
            client.send_private_file_end(target_nick, file_id)
        else:
            client.send_file_start(file_id, filename, size, file_hash, is_folder=is_folder)
            with open(fullpath, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    client.send_file_chunk(make_file_chunk(file_id, chunk))
            client.send_file_end(file_id)

    # ── 消息接收 ─────────────────────────────────────────

    def load_history(self):
        """加载聊天历史到界面"""
        msgs = history.load_history(self.room_name)
        if not msgs:
            self._add_system_msg("💬 欢迎加入聊天室")
            return
        self._add_system_msg(f"📋 加载了 {len(msgs)} 条历史记录")
        for m in msgs:
            if m["msg_type"] == "chat":
                self._add_chat_msg(m["sender"], m["text"])
            elif m["msg_type"] == "system":
                self._add_system_msg(m["text"])

    def on_message_received(self, msg: dict):
        """收到消息（在主线程调用）"""
        msg_type = msg.get("type")

        if msg_type == "chat":
            self._add_chat_msg(msg.get("from", ""), msg.get("text", ""))
            # 保存到历史
            history.save_message(self.room_name, msg.get("from", ""), msg.get("text", ""), "chat")

        elif msg_type == "system":
            self._add_system_msg(msg.get("text", ""))
            # 系统消息也持久化到历史
            history.save_message(self.room_name, "", msg.get("text", ""), "system")

        elif msg_type == "file_start":
            self._on_file_start(msg)

        elif msg_type == "file_end":
            self._on_file_end(msg)

        elif msg_type == "private_chat":
            sender = msg.get("from", "")
            self._add_chat_msg(sender, msg.get("text", ""), is_private=True)
            history.save_message(self.room_name, sender, msg.get("text", ""), "private_chat")

        elif msg_type == "private_file_start":
            msg["_private"] = True
            self._on_file_start(msg)

        elif msg_type == "private_file_end":
            msg["_private"] = True
            self._on_file_end(msg)

    def on_file_chunk_received(self, file_id: str, chunk: bytes):
        """收到文件二进制块，按 file_id 分派给对应的接收流"""
        state = self.file_recv.get(file_id)
        if state and state.get("status") == "receiving":
            try:
                state["fp"].write(chunk)
                state["received"] += len(chunk)
            except Exception:
                pass

    def on_users_updated(self, users: list):
        """用户列表更新"""
        self.users = users
        self.user_count_label.config(text=f"成员 ({len(users)})")
        self.user_listbox.delete(0, "end")
        for u in users:
            prefix = "▶ " if u == self.nick else "  "
            self.user_listbox.insert("end", f"{prefix}{u}")
            if u == self.nick:
                self.user_listbox.itemconfig("end", fg=COLOR_ACCENT)

    def on_proxy_status(self, active: bool, port: int):
        """更新代理共享按钮状态"""
        if active:
            self.proxy_share_btn.config(text="🌐 共享中", fg="#27AE60")
        else:
            self.proxy_share_btn.config(text="🌐 共享网络", fg="#27AE60")

    def _toggle_proxy(self):
        """切换网络共享"""
        self.app._toggle_proxy()

    def _use_proxy(self):
        """切换使用/关闭对方共享的网络"""
        if hasattr(self, "_proxy_active") and self._proxy_active:
            # 已启用 → 关闭
            self.app._disable_system_proxy()
            self.use_proxy_btn.config(text="🌐 使用共享", fg=COLOR_ACCENT, state="normal")
            self._proxy_active = False
            self._add_system_msg("🌐 已关闭系统代理")
        elif self._server_ip and self._proxy_port:
            # 未启用 → 开启
            self.app._enable_system_proxy(self._server_ip, self._proxy_port)
            self.use_proxy_btn.config(text="✅ 关闭共享", fg="#E74C3C", state="normal")
            self._proxy_active = True
        else:
            self._add_system_msg("❌ 未发现可用的网络共享")

    def _update_proxy_info(self, proxy_port: int):
        """房间代理信息更新（房主开启/关闭共享时由服务器广播触发）"""
        self._proxy_port = proxy_port
        if proxy_port > 0 and self._server_ip:
            # 显示"使用共享"按钮（如果还没显示）
            if hasattr(self, 'use_proxy_btn'):
                if not self.use_proxy_btn.winfo_manager():
                    self.use_proxy_btn.pack(side="left", padx=(8, 0))
                self.use_proxy_btn.config(text="🌐 使用共享", fg=COLOR_ACCENT, state="normal")
                self._add_system_msg(f"🌐 房间已开启网络共享 (:{proxy_port})")
        elif proxy_port > 0:
            # 有代理端口但不知道 IP（不太可能，兜底）
            pass
        else:
            # 代理已关闭
            if hasattr(self, 'use_proxy_btn') and self.use_proxy_btn.winfo_manager():
                self.use_proxy_btn.pack_forget()
            self._add_system_msg("🌐 房间已关闭网络共享")

    IMAGE_EXTENSIONS = {".png", ".gif", ".ppm", ".pgm", ".bmp", ".ico", ".xbm", ".jpg", ".jpeg"}

    def _show_image_thumbnail(self, save_path: str, frame: tk.Frame):
        """在聊天消息中嵌入图片缩略图"""
        ext = os.path.splitext(save_path)[1].lower()
        if ext not in self.IMAGE_EXTENSIONS:
            return
        try:
            img = tk.PhotoImage(file=save_path)
        except Exception:
            # tkinter 原生不支持的格式（如 JPEG）→ Windows GDI+ 解码
            if ext in (".jpg", ".jpeg") and platform.system() == "Windows":
                img = self._load_jpeg_gdi(save_path)
                if img is None:
                    return
            else:
                return
        try:
            # 缩略图最大 200x200
            max_w, max_h = 200, 200
            if img.width() > max_w or img.height() > max_h:
                ratio = min(max_w / img.width(), max_h / img.height())
                new_w = int(img.width() * ratio)
                new_h = int(img.height() * ratio)
                img = img.subsample(
                    max(1, img.width() // new_w),
                    max(1, img.height() // new_h),
                )
            label = tk.Label(frame, image=img, bg=COLOR_WHITE,
                             cursor="hand2")
            label.image = img  # 保持引用
            label.pack(pady=(4, 0))
            label.bind("<Button-1>", lambda e: self._open_file(save_path))
        except Exception:
            pass

    @staticmethod
    def _load_jpeg_gdi(filepath: str):
        """用 Windows GDI+ 解码 JPEG → PPM，再让 tkinter 读取"""
        try:
            import ctypes
            from ctypes import wintypes

            gdiplus = ctypes.windll.gdiplus

            # 1. 初始化 GDI+
            class GdiplusStartupInput(ctypes.Structure):
                _fields_ = [
                    ("GdiplusVersion", wintypes.UINT),
                    ("DebugEventCallback", ctypes.c_void_p),
                    ("SuppressBackgroundThread", wintypes.BOOL),
                    ("SuppressExternalCodecs", wintypes.BOOL),
                ]
            startup_input = GdiplusStartupInput()
            startup_input.GdiplusVersion = 1
            token = ctypes.c_ulonglong()
            ret = gdiplus.GdiplusStartup(
                ctypes.byref(token),
                ctypes.byref(startup_input),
                None,
            )
            if ret != 0:
                return None

            # 2. 从文件创建 Bitmap
            bitmap = ctypes.c_void_p()
            ret = gdiplus.GdipCreateBitmapFromFile(
                wintypes.LPCWSTR(filepath),
                ctypes.byref(bitmap),
            )
            if ret != 0 or not bitmap:
                gdiplus.GdiplusShutdown(token)
                return None

            # 3. 获取宽高
            w, h = wintypes.UINT(), wintypes.UINT()
            gdiplus.GdipGetImageWidth(bitmap, ctypes.byref(w))
            gdiplus.GdipGetImageHeight(bitmap, ctypes.byref(h))
            w, h = w.value, h.value
            if w == 0 or h == 0:
                gdiplus.GdipDisposeImage(bitmap)
                gdiplus.GdiplusShutdown(token)
                return None

            # 4. 锁定 bitmap — 定义 BitmapData 结构
            class BitmapData(ctypes.Structure):
                _fields_ = [
                    ("Width", wintypes.UINT),
                    ("Height", wintypes.UINT),
                    ("Stride", wintypes.INT),
                    ("PixelFormat", wintypes.UINT),
                    ("Scan0", ctypes.c_void_p),
                    ("Reserved", ctypes.c_void_p),
                ]
            bmpdata = BitmapData()
            rect = (wintypes.INT * 4)(0, 0, w, h)  # GpRect
            # PixelFormat32bppARGB = 0x0026200a — 兼容所有图片格式（JPEG/BMP/PNG 等）
            fmt_32bpp_argb = 0x0026200a
            gdiplus.GdipBitmapLockBits(
                bitmap, rect, 0x0001, fmt_32bpp_argb,  # ImageLockModeRead = 0x0001
                ctypes.byref(bmpdata),
            )
            stride, scan0 = bmpdata.Stride, bmpdata.Scan0
            if not scan0:
                gdiplus.GdipDisposeImage(bitmap)
                gdiplus.GdiplusShutdown(token)
                return None
            # c_void_p 值可能是 c_void_p 对象或裸整数，统一为整数地址
            scan0_addr = scan0.value if hasattr(scan0, 'value') else scan0

            try:
                # 5. 读取像素并转为 PPM（BGRA→RGB，每像素 4 字节，跳过 Alpha）
                header = f"P6\n{w} {h}\n255\n".encode("ascii")
                ppm_data = bytearray(header)
                raw = (ctypes.c_ubyte * (abs(stride) * h)).from_address(scan0_addr)
                for y in range(h):
                    row_start = y * stride
                    for x in range(w):
                        idx = row_start + x * 4  # 32bpp = 4 bytes/pixel
                        ppm_data.extend(bytes([raw[idx+2], raw[idx+1], raw[idx+0]]))  # BGRA→RGB

                # 6. tkinter 读取 PPM
                photo = tk.PhotoImage(data=bytes(ppm_data))
                return photo
            finally:
                # 确保 GDI+ 资源无论异常与否都被释放
                try:
                    gdiplus.GdipBitmapUnlockBits(bitmap, ctypes.byref(bmpdata))
                except Exception:
                    pass
                try:
                    gdiplus.GdipDisposeImage(bitmap)
                except Exception:
                    pass
                try:
                    gdiplus.GdiplusShutdown(token)
                except Exception:
                    pass
        except Exception:
            return None

    # ── 文件接收 ─────────────────────────────────────────

    def _on_file_start(self, msg: dict):
        """开始接收文件"""
        file_id = msg["file_id"]
        filename = msg["filename"]
        size = msg["size"]
        expected_hash = msg.get("hash", "")
        is_folder = msg.get("is_folder", False)

        # 如果是文件夹，解压到同名目录而不是保存 zip 文件
        if is_folder:
            folder_name = filename.replace(".zip", "")
            save_dir = os.path.join(self.app.download_dir, folder_name)
            base = folder_name
            counter = 1
            while os.path.exists(save_dir):
                save_dir = os.path.join(self.app.download_dir, f"{base}_{counter}")
                counter += 1
            os.makedirs(save_dir, exist_ok=True)
            # 临时 zip 路径
            save_path = save_dir + ".tmpzip"
        else:
            save_path = os.path.join(self.app.download_dir, filename)
            # 重名处理
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(save_path):
                save_path = os.path.join(self.app.download_dir, f"{base}_{counter}{ext}")
                counter += 1

        try:
            fp = open(save_path, "wb")
            self.file_recv[file_id] = {
                "filename": filename,
                "save_path": save_path,
                "sender": msg.get("from", ""),
                "is_folder": is_folder,
                "size": size,
                "received": 0,
                "expected_hash": expected_hash,
                "status": "receiving",
                "fp": fp,
            }
            # 系统提示开始接收
            if is_folder:
                self._add_system_msg(f"📥 正在接收文件夹: {folder_name} ({self._format_size(size)})")
            else:
                self._add_system_msg(f"📥 正在接收文件: {filename} ({self._format_size(size)})")
        except OSError as e:
            self._add_system_msg(f"❌ 文件保存失败: {filename} - {e}")

    def _on_file_end(self, msg: dict):
        """文件接收完成 — 校验完整性和哈希"""
        file_id = msg["file_id"]
        state = self.file_recv.get(file_id)
        if not state:
            return

        if state.get("fp"):
            try:
                state["fp"].close()
            except OSError:
                pass

        state["status"] = "done"
        self.file_recv.pop(file_id, None)

        filename = state["filename"]
        save_path = state["save_path"]
        sender = state.get("sender", "")
        is_folder = state.get("is_folder", False)
        received = state["received"]
        expected_size = state["size"]
        expected_hash = state.get("expected_hash", "")

        # 1. 检查大小是否匹配
        size_ok = (received == expected_size)

        # 2. 检查哈希是否匹配（如果有的话）
        hash_ok = True
        if expected_hash and size_ok:
            try:
                actual_hash = compute_file_hash(save_path)
                hash_ok = (actual_hash == expected_hash)
            except Exception:
                hash_ok = False

        if not size_ok:
            self._add_system_msg(f"❌ 文件接收不完整: {filename} "
                                 f"(收到 {received}/{expected_size} 字节)")
            try:
                os.remove(save_path)
            except OSError:
                pass
        elif not hash_ok:
            self._add_system_msg(f"❌ 文件校验失败(哈希不匹配): {filename}")
            try:
                os.remove(save_path)
            except OSError:
                pass
        elif is_folder:
            # 自动解压文件夹
            try:
                extract_to = save_path.replace(".tmpzip", "")
                os.makedirs(extract_to, exist_ok=True)
                with zipfile.ZipFile(save_path, "r") as zf:
                    zf.extractall(extract_to)
                os.remove(save_path)
                folder_name = os.path.basename(extract_to)
                self._add_system_msg(f"📁 文件夹已接收: {folder_name}")
                self._add_file_received_msg(folder_name + "/", extract_to, sender)
            except Exception as e:
                self._add_system_msg(f"❌ 文件夹解压失败: {filename} - {e}")
                # 解压失败也清理临时 zip 文件
                try:
                    os.remove(save_path)
                except OSError:
                    pass
        else:
            self._add_file_received_msg(filename, save_path, sender)
            if expected_hash:
                self._add_system_msg(f"🔒 文件校验通过: {filename}")

    def _add_file_received_msg(self, filename: str, save_path: str, sender: str = ""):
        """在聊天区添加文件已接收的消息"""
        is_folder = filename.endswith("/")
        display_name = filename.rstrip("/")
        icon = "📁" if is_folder else "📎"

        frame = tk.Frame(self.msg_container, bg=COLOR_WHITE)
        frame.pack(fill="x", padx=8, pady=2, anchor="w")

        # 显示发送者
        if sender and sender != self.nick:
            name_label = tk.Label(frame, text=sender,
                                   font=("微软雅黑", 9, "bold"),
                                   fg=COLOR_ACCENT, bg=COLOR_WHITE)
            name_label.pack(anchor="w")

        bubble = tk.Frame(frame, bg=COLOR_SYSTEM, bd=0, padx=10, pady=6)
        bubble.pack(side="left")

        label = tk.Label(bubble,
                          text=f"{icon} {display_name}  ✓ 已接收",
                          font=("微软雅黑", 9),
                          fg=COLOR_TEXT, bg=COLOR_SYSTEM,
                          cursor="hand2")
        label.pack()
        label.bind("<Button-1>", lambda e: self._open_file(save_path))

        # 如果是图片文件，嵌入缩略图预览
        self._show_image_thumbnail(save_path, bubble)

        # 自动滚动到底部
        self._scroll_to_bottom()
        self._prune_messages()

    def _add_sent_file_msg(self, filename: str, fullpath: str):
        """在聊天区添加文件已发送的消息（发送者预览）"""
        frame = tk.Frame(self.msg_container, bg=COLOR_WHITE)
        frame.pack(fill="x", padx=8, pady=2, anchor="e")

        bubble = tk.Frame(frame, bg=COLOR_MY_MSG, bd=0, padx=10, pady=6,
                          highlightbackground=COLOR_BORDER,
                          highlightthickness=1)
        bubble.pack(side="right")

        label = tk.Label(bubble,
                          text=f"📎 {filename}  ✓ 已发送",
                          font=("微软雅黑", 9),
                          fg=COLOR_TEXT, bg=COLOR_MY_MSG,
                          cursor="hand2")
        label.pack()
        label.bind("<Button-1>", lambda e: self._open_file(fullpath))

        # 如果是图片文件，嵌入缩略图预览
        self._show_image_thumbnail(fullpath, bubble)

        self._scroll_to_bottom()
        self._prune_messages()

    # ── UI 辅助 ──────────────────────────────────────────

    def _add_chat_msg(self, sender: str, text: str, is_private: bool = False):
        """添加聊天消息气泡"""
        is_me = sender == self.nick
        now_str = time.strftime("%H:%M")
        frame = tk.Frame(self.msg_container, bg=COLOR_WHITE)
        frame.pack(fill="x", padx=8, pady=3, anchor="e" if is_me else "w")

        if not is_me:
            name_label = tk.Label(frame, text=sender,
                                   font=("微软雅黑", 9, "bold"),
                                   fg=COLOR_ACCENT, bg=COLOR_WHITE)
            name_label.pack(anchor="w")

        bubble_color = COLOR_MY_MSG if is_me else COLOR_OTHER_MSG
        # 私聊气泡用蓝色边框标记
        if is_private:
            border_color = "#E74C3C" if is_me else "#3498DB"
            bubble = tk.Frame(frame, bg=bubble_color, bd=0, padx=10, pady=5,
                              highlightbackground=border_color,
                              highlightthickness=2)
            # 添加私聊标记文字
            marker = "🔒 私聊"
            marker_label = tk.Label(frame, text=marker,
                                     font=("微软雅黑", 8),
                                     fg="#E74C3C", bg=COLOR_WHITE)
            marker_label.pack(anchor="e" if is_me else "w")
        else:
            bubble = tk.Frame(frame, bg=bubble_color, bd=0, padx=10, pady=5,
                              highlightbackground=COLOR_BORDER,
                              highlightthickness=1)
        bubble.pack(anchor="e" if is_me else "w")

        # 统一用 tk.Text 显示消息（支持鼠标选中 + Ctrl+C 复制）
        msg_text = tk.Text(bubble, font=("微软雅黑", 10),
                           fg=COLOR_TEXT, bg=bubble_color,
                           wrap="word", width=40, height=1,
                           bd=0, highlightthickness=0,
                           padx=0, pady=0)
        msg_text.insert("1.0", text)
        msg_text.configure(state="disabled", takefocus=0)
        # 标记 URL 为蓝色可点击
        url_pattern = r'https?://[^\s]+|www\.[^\s]+'
        for m in re.finditer(url_pattern, text):
            start = f"1.0+{m.start()}c"
            end = f"1.0+{m.end()}c"
            url = m.group()
            msg_text.tag_add("url", start, end)
            msg_text.tag_config("url", foreground="#0563C1", underline=1)
            msg_text.tag_bind("url", "<Button-1>",
                              lambda e, u=url: self._open_url(u))
        msg_text.pack(side="left")
        # 让 Text 自适应内容高度
        line_count = text.count("\n") + 1
        # 每 40 字符大约一行（中文字算 2 个宽度，按平均 380px ÷ 字体宽度 ≈ 30 个字）
        approx_chars_per_line = max(1, 380 // 10)
        visible_lines = max(1, (len(text) + approx_chars_per_line - 1) // approx_chars_per_line, line_count)
        msg_text.configure(height=min(visible_lines, 12))

        # 时间戳
        tk.Label(bubble, text=now_str,
                 font=("微软雅黑", 8),
                 fg=COLOR_TEXT_LIGHT, bg=bubble_color).pack(
                 side="right", padx=(6, 0))

        self._scroll_to_bottom()
        self._prune_messages()

    def _add_system_msg(self, text: str):
        """添加系统通知"""
        frame = tk.Frame(self.msg_container, bg=COLOR_WHITE)
        frame.pack(fill="x", padx=20, pady=2)

        label = tk.Label(frame, text=f"  💬 {text}  ",
                          font=("微软雅黑", 9),
                          fg=COLOR_TEXT_LIGHT, bg=COLOR_SYSTEM,
                          padx=8, pady=2)
        label.pack()
        self._scroll_to_bottom()
        self._prune_messages()

    MAX_MESSAGES = 500

    def _prune_messages(self):
        """超过 MAX_MESSAGES 条消息时删除最早的 100 条，防止内存泄漏"""
        children = self.msg_container.winfo_children()
        if len(children) > self.MAX_MESSAGES:
            for child in children[:100]:
                child.destroy()

    def _scroll_to_bottom(self):
        """滚动消息到底部"""
        self.msg_container.update_idletasks()
        self.msg_canvas.yview_moveto(1.0)

    def _open_file(self, path: str):
        """打开文件或文件夹"""
        try:
            os.startfile(path)
        except Exception:
            pass

    @staticmethod
    def _open_url(url: str):
        """用默认浏览器打开 URL"""
        try:
            import webbrowser
            # www. 开头补 https://
            if url.startswith("www."):
                url = "https://" + url
            webbrowser.open(url)
        except Exception:
            pass

    def _open_download_dir(self):
        """打开下载目录"""
        try:
            os.startfile(self.app.download_dir)
        except Exception:
            pass

    def _on_user_right_click(self, event):
        """用户列表右键菜单"""
        try:
            idx = self.user_listbox.nearest(event.y)
            if idx < 0:
                return
            selected = self.users[idx]
            if selected == self.nick:
                return
            self._user_menu.delete(0, "end")
            self._user_menu.add_command(
                label=f"💬 私聊 {selected}",
                command=lambda u=selected: self._start_private_chat(u))
            self._user_menu.add_command(
                label=f"📎 私发文件给 {selected}",
                command=lambda u=selected: self._start_private_file(u))
            self._user_menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass
        finally:
            self._user_menu.grab_release()

    def _start_private_chat(self, target: str):
        """弹出私聊输入框"""
        dialog = tk.Toplevel(self)
        dialog.title(f"💬 私聊 {target}")
        dialog.geometry("380x140")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg=COLOR_WHITE)
        tk.Label(dialog, text=f"对 {target} 说:",
                 font=("微软雅黑", 11), fg=COLOR_TEXT, bg=COLOR_WHITE).pack(pady=(12, 5))
        entry = tk.Text(dialog, font=("微软雅黑", 10), height=2,
                        bd=1, relief="solid", wrap="word")
        entry.pack(fill="x", padx=20, ipady=2)
        entry.focus_set()
        def do_send():
            text = entry.get("1.0", "end-1c").strip()
            if text:
                self.app.client.send_private(target, text)
                self._add_chat_msg(self.nick, text, is_private=True)
                self._add_system_msg(f"🔒 已私聊发送给 {target}")
            dialog.destroy()
        entry.bind("<Return>", lambda e: do_send() if not (e.state & 0x0001) else None)
        entry.bind("<Shift-Return>", lambda e: None)
        tk.Button(dialog, text="发送", font=("微软雅黑", 10, "bold"),
                  bg=COLOR_ACCENT, fg="white", bd=0, padx=12, pady=4,
                  cursor="hand2", command=do_send).pack(pady=(8, 0))

    def _start_private_file(self, target: str):
        """选择文件私发给指定用户"""
        paths = filedialog.askopenfilenames(title=f"选择要私发给 {target} 的文件")
        if not paths:
            return
        client = self.app.client
        for path in paths:
            filename = os.path.basename(path)
            self._add_system_msg(f"📤 正在私发文件给 {target}: {filename}")
            threading.Thread(
                target=self._send_file,
                args=(client, path, filename, False, target),
                daemon=True,
            ).start()

    def _do_leave(self):
        if messagebox.askyesno("离开房间", "确定离开房间吗？"):
            self.on_leave()

    def _do_rename(self):
        """弹出改名对话框"""
        dialog = tk.Toplevel(self)
        dialog.title("修改昵称")
        dialog.geometry("320x140")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg=COLOR_WHITE)

        tk.Label(dialog, text="新的昵称:", font=("微软雅黑", 11),
                 fg=COLOR_TEXT, bg=COLOR_WHITE).pack(pady=(15, 5))

        entry = tk.Entry(dialog, font=("微软雅黑", 11),
                         bd=1, relief="solid")
        entry.insert(0, self.nick)
        entry.pack(fill="x", padx=30, ipady=4)
        entry.focus_set()
        entry.select_range(0, "end")

        def do_rename():
            new_nick = entry.get().strip()
            if not new_nick:
                messagebox.showwarning("提示", "昵称不能为空", parent=dialog)
                return
            if new_nick == self.nick:
                dialog.destroy()
                return
            self.app.client.send_rename(new_nick)
            dialog.destroy()

        tk.Button(dialog, text="确认改名", font=("微软雅黑", 10, "bold"),
                  bg=COLOR_ACCENT, fg="white", bd=0, padx=12, pady=4,
                  cursor="hand2", command=do_rename).pack(pady=(10, 0))
        entry.bind("<Return>", lambda e: do_rename())

    def on_rename_ok(self, new_nick: str):
        """服务器确认改名成功"""
        old_nick = self.nick
        self.nick = new_nick
        # 更新标题栏（通过 app 的标题）
        self.app.title(f"局域网聊天室 - {self.nick}")
        # 刷新用户列表中的标记
        self.on_users_updated(self.users)
        self._add_system_msg(f"✅ 你已改名为 {new_nick}")

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes}B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f}MB"
