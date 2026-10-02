"""标签照片本地存储：文件落到 data/photos/，库里只存相对路径与元信息。

支持两种来源：
- 上传文件（multipart 解析出的字节）；
- 测试/种子直接写字节（内置生成一个极小 PNG 占位）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import PHOTO_DIR

_ALLOWED = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def save_label_photo(conn: sqlite3.Connection, *, bag_no: str,
                     data: bytes, content_type: str = "image/jpeg") -> dict:
    row = conn.execute("SELECT id FROM sleeping_bags WHERE bag_no=?",
                       (bag_no,)).fetchone()
    if not row:
        from .services import NotFound
        raise NotFound(f"睡袋编号不存在：{bag_no}")
    suffix = _ALLOWED.get(content_type)
    if not suffix:
        raise ValueError(f"不支持的图片类型：{content_type}")
    existing = len(list(PHOTO_DIR.glob(f"bag-{row['id']}-*")))
    rel = f"bag-{row['id']}-{existing + 1}{suffix}"
    (PHOTO_DIR / rel).write_bytes(data)
    cur = conn.execute(
        "INSERT INTO label_photos(bag_id, rel_path, content_type)"
        " VALUES (?,?,?)",
        (row["id"], rel, content_type))
    conn.commit()
    return {"id": cur.lastrowid, "path": rel}


def photo_path(photo_id: int, conn: sqlite3.Connection) -> tuple[Path, str]:
    row = conn.execute("SELECT * FROM label_photos WHERE id=?",
                       (photo_id,)).fetchone()
    if not row:
        from .services import NotFound
        raise NotFound("照片不存在")
    return PHOTO_DIR / row["rel_path"], row["content_type"]


# 1x1 透明 PNG，种子与测试无需真实图片文件
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082")


def tiny_png() -> bytes:
    return _TINY_PNG
