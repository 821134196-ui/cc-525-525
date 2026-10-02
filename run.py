"""一键启动：python3 run.py [--reset] [--port 8000]

初始化本地 SQLite（data/napbags.db）、标签照片目录（data/photos），
写入演示数据（含“错拿”样例），启动 Web 并在进程内开启提醒任务。
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime, timedelta

from napbags.messages import MockMessageAdapter
from napbags.photos import save_label_photo
from napbags.reminders import ReminderScheduler
from napbags.service import NapService, ServiceError
from napbags.web import serve

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "napbags.db")
PHOTO_DIR = os.path.join(DATA, "photos")


def seed(svc: NapService) -> dict:
    """初始化演示数据；幂等（已存在则直接查回令牌）。"""
    out = {}

    def ensure_class(name, rows, cols):
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT id FROM classes WHERE name=?", (name,)).fetchone()
        conn.close()
        return row[0] if row else svc.create_class(name, rows, cols)

    c1 = ensure_class("小一班", 3, 5)
    c2 = ensure_class("小二班", 3, 5)

    children = [
        ("SUN", c1, "R1C01", "B-1001", "纯棉", "尘螨过敏，避免羽绒"),
        ("LILY", c1, "R1C02", "B-1002", "六层纱布", "无"),
        ("TOM", c1, "R1C03", "B-1003", "纯棉", "无"),
        ("MEI", c1, "R1C04", "B-1004", "薄棉", "乳胶过敏"),
        ("KAI", c2, "R1C01", "B-2001", "纯棉", "无"),
    ]
    for code, cid, slot, bag_no, mat, allergy in children:
        try:
            svc.register_child(code, cid, slot, bag_no, mat, allergy,
                               photo_path=save_label_photo(bag_no, PHOTO_DIR))
        except ServiceError:
            pass

    for bag_no, mat, cid, slot in [
        ("SP-01", "纯棉（备用）", c1, "R3C01"),
        ("SP-02", "纱布（备用）", c1, "R3C02"),
        ("SP-03", "纯棉（备用）", c2, "R3C01"),
    ]:
        try:
            svc.register_spare(bag_no, mat, cid, slot)
        except ServiceError:
            pass

    tokens = {}
    import sqlite3
    for code, name in [("SUN", "孙女士"), ("LILY", "李女士"), ("TOM", "Tom 家长"),
                       ("MEI", "梅女士"), ("KAI", "凯爸")]:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT g.token FROM guardians g JOIN children c ON c.id=g.child_id"
            " WHERE c.code=?", (code,)).fetchone()
        conn.close()
        tokens[code] = row[0] if row else svc.add_guardian(code, name)
    out["tokens"] = tokens

    # ---- 错拿样例：LILY 家周末领回 B-1002，被 TOM 家拿错 ----
    if not svc.list_incidents():
        svc.pickup("B-1002")
        svc.record_mispick("B-1002", "TOM",
                           note="样例：周末领回时扫码发现拿错，待换回")
    # 一条正常的“已领回”样例
    try:
        svc.pickup("B-1004")
    except ServiceError:
        pass

    # 一条代领授权样例（有效），便于演示代领；已存在则复用
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT a.code FROM authorizations a JOIN children c ON c.id=a.child_id"
        " WHERE c.code='KAI' AND a.revoked_at IS NULL"
        " AND a.valid_until >= ? ORDER BY a.id LIMIT 1",
        (svc.now().isoformat(timespec="seconds"),)).fetchone()
    conn.close()
    if row:
        out["demo_auth"] = row[0]
    else:
        auth = svc.create_authorization(
            tokens["KAI"], "王阿姨", relation="奶奶", valid_days=7)
        out["demo_auth"] = auth["code"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="幼儿园午睡袋管理（本地启动）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="删除旧库重新播种")
    args = ap.parse_args(argv)

    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(PHOTO_DIR, exist_ok=True)

    adapter = MockMessageAdapter()
    svc = NapService(DB_PATH,
                     clock=datetime.now,
                     message_adapter=adapter)
    info = seed(svc)

    # 进程内提醒任务：每小时扫描“周末领回后超时未送回”
    scheduler = ReminderScheduler(svc.run_due_reminders, interval_seconds=3600)
    scheduler.start()

    httpd = serve(svc, PHOTO_DIR, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}/classes"
    print("=" * 64)
    print("幼儿园午睡袋管理已启动")
    print(f"  老师端（班级格位） : {url}")
    print(f"  备用袋库存         : http://{args.host}:{args.port}/spares")
    print(f"  特殊事项（含错拿） : http://{args.host}:{args.port}/incidents")
    print("  家长端令牌（仅看本人）:")
    for code, tok in info["tokens"].items():
        print(f"    {code:5s} -> http://{args.host}:{args.port}/guardian?token={tok}")
    if info.get("demo_auth"):
        print(f"  演示代领授权码（KAI）: {info['demo_auth']}（可在家长端撤销后试领）")
    print("-" * 64)
    print("  错拿样例：B-1002（LILY）被 TOM 家错拿，见「特殊事项」页处理换回")
    print("  重新播种：python3 run.py --reset")
    print("=" * 64)
    if os.environ.get("NAPBAGS_OPEN_BROWSER"):
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        scheduler.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
