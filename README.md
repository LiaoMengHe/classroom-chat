# 局域网聊天室

纯 Python 局域网聊天室，零外部依赖，支持文字聊天、文件传输、图片预览、私聊、SOCKS5 网络共享。

## 功能

- 💬 **文字聊天** — 支持消息历史持久化（sqlite3）、消息时间戳、URL 自动识别可点击
- 📎 **文件传输** — 任意文件传输、文件夹自动压缩/解压、图片缩略图预览（支持 JPEG/GDI+）
- 🔒 **私聊 + 私发文件** — 用户列表右键菜单，消息精确转发
- 🌐 **SOCKS5 网络共享** — 房主开启共享，加入者一键设系统代理，浏览器自动走共享网络
- 👥 **用户管理** — 在线用户列表、房间内改名
- 🔔 **系统通知** — 窗口最小化时任务栏闪烁 + Windows 原生 Toast 通知
- 📋 **聊天记录持久化** — 退出重进历史可见

## 快速开始

```bash
pip install pyinstaller
python main.py
```

打包为单文件 exe：

```bash
pyinstaller --onefile --windowed --name "局域网聊天室" main.py
```

## 协议

纯 Python 标准库实现，零外部依赖：
- `tkinter` — GUI
- `socket` + `threading` — 网络通信
- `sqlite3` — 聊天记录存储
- `ctypes` — Windows 系统托盘、系统代理设置、GDI+ 图片解码
