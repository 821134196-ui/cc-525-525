"""演示种子数据 —— 含一个完整的“错拿”样例。

场景：
- 小一班 C103 豆豆 与 C104 米米 周末都已领回，老师事后发现两家实物互混，
  已登记错拿事件（物品链），双方家长均收到模拟提醒，周一须先换回再送回核验。
- C202 安安 的睡袋在家长家时转班到小二班（转班挂起，送回核验时落位 B-02）。
- C201 乐乐 的 N-007 污损报废，已启用备用袋 S-01（库存可查）。
- C101 朵朵 有一条有效代领授权，另有一条已撤销授权（演示旧码立即失效）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import services as svc
from .db import connect, init_db
from .photos import save_label_photo, tiny_png
from .reminders import schedule_reminder


def seed(conn) -> None:
    if svc.list_classes(conn):
        return  # 已有数据，不重复播种

    c1 = svc.create_class(conn, name="小一班")
    c2 = svc.create_class(conn, name="小二班")
    for no in ("A-01", "A-02", "A-03", "A-04", "A-05", "A-06"):
        svc.create_slot(conn, c1, no)
    for no in ("B-01", "B-02", "B-03", "B-04", "B-05", "B-06"):
        svc.create_slot(conn, c2, no)

    children = {
        "C101": dict(name="朵朵", cls=c1, allergy="对羽绒过敏，睡袋仅限纯棉或抓绒"),
        "C102": dict(name="阳阳", cls=c1, allergy=""),
        "C103": dict(name="豆豆", cls=c1, allergy=""),
        "C104": dict(name="米米", cls=c1, allergy="皮肤敏感，需使用温和洗涤剂"),
        "C201": dict(name="乐乐", cls=c2, allergy=""),
        "C202": dict(name="安安", cls=c1, allergy=""),   # 稍后转去小二班
    }
    child_ids = {}
    for code, d in children.items():
        child_ids[code] = svc.create_child(
            conn, code=code, name=d["name"], class_id=d["cls"],
            allergy_note=d["allergy"])

    parents = [
        ("朵妈", "13800000001", "P-101001", "C101"),
        ("阳阳爸", "13800000002", "P-102002", "C102"),
        ("豆妈", "13800000003", "P-103003", "C103"),
        ("米米妈", "13800000004", "P-104004", "C104"),
        ("乐妈", "13800000005", "P-201005", "C201"),
        ("安妈", "13800000006", "P-202006", "C202"),
    ]
    parent_phone: dict[str, str] = {}
    for name, phone, pcode, ccode in parents:
        svc.create_parent(conn, name=name, phone=phone, pickup_code=pcode,
                          child_id=child_ids[ccode])
        parent_phone[ccode] = phone

    # 常规睡袋 + 备用袋
    bags = [
        ("N-001", "纯棉", "无"), ("N-002", "抓绒", "无"),
        ("N-003", "纯棉", "无"), ("N-004", "纯棉", "无"),
        ("N-006", "纯棉", "无"), ("N-007", "纯棉", "无"),
    ]
    for bag_no, mat, note in bags:
        svc.register_bag(conn, bag_no=bag_no, material=mat, allergy_note=note)
    svc.register_bag(conn, bag_no="S-01", material="纯棉（备用）", spare=True)
    svc.register_bag(conn, bag_no="S-02", material="纯棉（备用）", spare=True)

    svc.assign_bag(conn, bag_no="N-001", child_code="C101", slot_no="A-01")
    svc.assign_bag(conn, bag_no="N-002", child_code="C102", slot_no="A-02")
    svc.assign_bag(conn, bag_no="N-003", child_code="C103", slot_no="A-03")
    svc.assign_bag(conn, bag_no="N-004", child_code="C104", slot_no="A-04")
    svc.assign_bag(conn, bag_no="N-007", child_code="C201", slot_no="B-01")
    svc.assign_bag(conn, bag_no="N-006", child_code="C202", slot_no="A-05")

    # 标签照片（本地）
    save_label_photo(conn, bag_no="N-001", data=tiny_png(),
                     content_type="image/png")
    save_label_photo(conn, bag_no="N-003", data=tiny_png(),
                     content_type="image/png")

    # C201 污损更换：N-007 报废，启用备用袋 S-01
    svc.replace_with_spare(
        conn, child_code="C201", damaged_bag_no="N-007",
        spare_bag_no="S-01", reason="正面大片污渍且内衬破损，无法洗净")

    # C202 睡袋先领回、随后转班到小二班（挂起，送回时落位 B-02）
    svc.pickup(conn, code="P-202006")
    svc.transfer_child(conn, child_code="C202", to_class_id=c2,
                       new_slot_no="B-02", note="家长工作调动转班")

    # —— 错拿样例：C103 / C104 都已领回，发现实物互混 ——
    svc.pickup(conn, code="P-103003")
    svc.pickup(conn, code="P-104004")
    svc.record_swap_after_pickup(
        conn, code_a="C103", code_b="C104",
        note="两家袋子同色同码段，周末交接时拿混，已电话沟通周一先换回")

    # C101 代领授权：一条有效、一条已撤销（旧码失效演示）
    svc.grant_proxy(conn, parent_phone=parent_phone["C101"],
                    proxy_name="王阿姨", proxy_phone="13900001111",
                    code="D-WANG01",
                    valid_from=datetime.now() - timedelta(hours=1),
                    valid_until=datetime.now() + timedelta(days=2))
    svc.grant_proxy(conn, parent_phone=parent_phone["C101"],
                    proxy_name="前钟点工", proxy_phone="13900002222",
                    code="D-OLD000",
                    valid_from=datetime.now() - timedelta(days=3),
                    valid_until=datetime.now() + timedelta(days=3))
    svc.revoke_proxy(conn, code="D-OLD000",
                     parent_phone=parent_phone["C101"])

    # 一条已到期提醒：服务启动后由进程内任务立即发出（模拟适配器）
    schedule_reminder(
        conn, fire_at=datetime.now() - timedelta(minutes=1),
        kind="pickup_reminder",
        payload={"phone": parent_phone["C102"], "child_code": "C102",
                 "bag_no": "N-002",
                 "title": "周末睡袋领回提醒",
                 "body": "C102 的睡袋 N-002 今周可扫码领回清洗，"
                         "请家长安排时间（示例：启动即触发的进程内提醒）。"})


def seed_if_empty(db_path: str | None = None) -> None:
    conn = connect(db_path)
    init_db(conn)
    try:
        seed(conn)
    finally:
        conn.close()
