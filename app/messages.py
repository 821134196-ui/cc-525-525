"""家长消息 —— 模拟适配器。

不真正外发：写入 messages 表并返回消息内容。真实环境可在此处
替换为短信/微信适配器，调用方（services）无需改动。
"""
from __future__ import annotations

import sqlite3

# 最近一次适配器返回（演示页“消息回执”用，仅本进程）
LAST_SENT: dict[str, dict] = {}


def send_message(conn: sqlite3.Connection, parent_phone: str, kind: str,
                 title: str, body: str, *, commit: bool = True) -> dict:
    cur = conn.execute(
        "INSERT INTO messages(parent_phone, channel, kind, title, body)"
        " VALUES (?, 'mock', ?, ?, ?)",
        (parent_phone, kind, title, body))
    if commit:
        conn.commit()
    receipt = {"id": cur.lastrowid, "to": parent_phone, "kind": kind,
               "title": title, "body": body, "channel": "mock-simulated"}
    LAST_SENT[parent_phone] = receipt
    return receipt


def list_messages(conn: sqlite3.Connection, phone: str | None = None,
                  limit: int = 50) -> list[sqlite3.Row]:
    if phone:
        return list(conn.execute(
            "SELECT * FROM messages WHERE parent_phone=? ORDER BY id DESC LIMIT ?",
            (phone, limit)))
    return list(conn.execute(
        "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)))
