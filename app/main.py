"""一条本地启动命令的入口：python3 -m app.main

初始化库 → 首次启动播种演示数据（含错拿样例）→ 启动 Web 与进程内提醒。
"""
from __future__ import annotations

import os

from .db import DEFAULT_DB, init_db, connect
from .reminders import ReminderScheduler
from .seed import seed
from .web import serve


BANNER = r"""
============================================================
 幼儿园午睡袋管理 · 本地已启动
------------------------------------------------------------
 教师端（班级页）        http://127.0.0.1:8000/
 备用袋库存              http://127.0.0.1:8000/spares
 家长消息（模拟回执）    http://127.0.0.1:8000/messages
 家长端                  http://127.0.0.1:8000/parent

 家长端试用手机号：13800000001（C101 朵朵） … 13800000004（C104 米米）

 错拿样例：小一班 C103 豆豆 / C104 米米 均已领回且登记了错拿，
           双方家长消息已在“消息回执”；周一须先换回再做送回核验。
           在送回框扫入对方编号（如给 C103 扫 N-004）会被直接拦下。
 转班样例：C202 安安 已转小二班，睡袋在家长家，送回时自动落位 B-02。
 授权样例：C101 有有效码 D-WANG01；已撤销码 D-OLD000 扫码立即失效。
 备用袋：  N-007 已污损报废，C201 正在使用备用袋 S-01。
 重置数据：删除 data/nap.db 后重新启动。
============================================================
"""


def main() -> None:
    host = os.environ.get("NAP_HOST", "127.0.0.1")
    port = int(os.environ.get("NAP_PORT", "8000"))
    db_path = os.environ.get("NAP_DB") or str(DEFAULT_DB)

    conn = connect(db_path)
    init_db(conn)
    seed(conn)
    conn.close()

    print(BANNER)
    httpd = serve(host=host, port=port, db_path=db_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
