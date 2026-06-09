"""
main.py — 局域网聊天室入口

双击此文件或运行:
    python main.py

打包成单个 exe:
    pip install pyinstaller
    pyinstaller --onefile --windowed main.py
"""

import sys
import os

# 确保当前目录在路径中（用于 PyInstaller 打包）
if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))
else:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

from ui import App


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
