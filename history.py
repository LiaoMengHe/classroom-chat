"""
history.py — 聊天记录持久化（sqlite3，零外部依赖）

作用：
  1. 保存聊天记录到 SQLite 文件
  2. 进入房间时加载最近的 N 条历史
"""

import sqlite3
import threading
import time
from pathlib import Path


# 数据库文件路径（用户目录下）
DB_PATH = str(Path.home() / ".chatroom_history.db")

# 加载历史的最大条数
MAX_LOAD = 200

# 每个房间最大保存条数
MAX_PER_ROOM = 2000

_local = threading.local()  # 线程本地存储


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接（线程本地，自动创建）"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _init_db(_local.conn)
    return _local.conn


def _init_db(conn: sqlite3.Connection):
    """初始化表结构"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            room        TEXT    NOT NULL,
            sender      TEXT    NOT NULL,
            text        TEXT    NOT NULL,
            msg_type    TEXT    NOT NULL DEFAULT 'chat',
            timestamp   TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_room
        ON messages(room, id)
    """)
    conn.commit()


def save_message(room: str, sender: str, text: str, msg_type: str = "chat"):
    """保存一条消息到数据库"""
    try:
        conn = _get_conn()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO messages (room, sender, text, msg_type, timestamp) VALUES (?, ?, ?, ?, ?)",
            (room, sender, text, msg_type, ts),
        )
        conn.commit()
        # 定期清理：房间消息太多时删除最旧的
        _prune_room(conn, room)
    except Exception:
        pass  # 持久化失败不影响聊天


def load_history(room: str) -> list[dict]:
    """加载指定房间最近的历史消息"""
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT sender, text, msg_type, timestamp FROM messages "
            "WHERE room=? ORDER BY id DESC LIMIT ?",
            (room, MAX_LOAD),
        ).fetchall()
        # 按时间正序返回
        rows.reverse()
        return [
            {"sender": r[0], "text": r[1], "msg_type": r[2], "timestamp": r[3]}
            for r in rows
        ]
    except Exception:
        return []


def _prune_room(conn: sqlite3.Connection, room: str):
    """房间消息超过上限时删除最旧的"""
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE room=?", (room,)
        ).fetchone()[0]
        if count > MAX_PER_ROOM:
            conn.execute(
                "DELETE FROM messages WHERE id IN ("
                "  SELECT id FROM messages WHERE room=? ORDER BY id LIMIT ?"
                ")",
                (room, count - MAX_PER_ROOM),
            )
            conn.commit()
    except Exception:
        pass
