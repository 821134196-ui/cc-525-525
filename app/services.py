"""午睡袋业务服务层 —— 所有规则的唯一落点。

规则要点：
- 睡袋编号全库唯一；同一睡袋任一时刻只能有一条活动分配（DB 部分唯一索引兜底）。
- 老师必须已见实物（seen=True）才能标记送回；扫码编号与孩子当前睡袋不符即判定错拿。
- 代领授权有有效期；撤销后旧码立即失效；授权码使用一次后不可重复领取。
- 转班时睡袋若在家长家中，先挂起待落位，周一送回核验时自动落位新班新格位。
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 异常

class DomainError(Exception):
    """业务规则冲突（消息可直接展示给老师/家长）。"""


class NotFound(DomainError):
    pass


class AuthCodeError(DomainError):
    pass


class WrongBagError(DomainError):
    """送回核验时实物编号与孩子当前睡袋不符（错拿）。"""

    def __init__(self, message: str, *, scanned_bag_no: str, owner_code: str | None):
        super().__init__(message)
        self.scanned_bag_no = scanned_bag_no
        self.owner_code = owner_code


CLEAN_STATES = {"clean", "needs_wash", "dirty"}
CLEAN_LABELS = {"clean": "清洁合格", "needs_wash": "需补洗", "dirty": "污损不合格"}


def _now() -> datetime:
    return datetime.now()


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 基础登记

def create_class(conn: sqlite3.Connection, name: str) -> int:
    try:
        cur = conn.execute("INSERT INTO classes(name) VALUES (?)", (name,))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise DomainError(f"班级已存在：{name}") from e


def create_slot(conn: sqlite3.Connection, class_id: int, slot_no: str) -> int:
    try:
        cur = conn.execute(
            "INSERT INTO cubby_slots(class_id, slot_no) VALUES (?, ?)",
            (class_id, slot_no),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise DomainError(f"格位号已存在：{slot_no}") from e


def create_child(conn: sqlite3.Connection, *, code: str, name: str,
                 class_id: int, allergy_note: str = "") -> int:
    if not _class_exists(conn, class_id):
        raise NotFound("班级不存在")
    try:
        cur = conn.execute(
            "INSERT INTO children(code, name, class_id, allergy_note) VALUES (?,?,?,?)",
            (code, name, class_id, allergy_note),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise DomainError(f"儿童代号已存在：{code}") from e


def create_parent(conn: sqlite3.Connection, *, name: str, phone: str,
                  child_id: int, pickup_code: str | None = None) -> dict:
    pickup_code = pickup_code or f"P-{secrets.token_hex(3).upper()}"
    try:
        cur = conn.execute(
            "INSERT INTO parents(name, phone, pickup_code, child_id) VALUES (?,?,?,?)",
            (name, phone, pickup_code, child_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise DomainError(f"家长手机号或领回码重复：{phone}") from e
    return {"id": cur.lastrowid, "name": name, "phone": phone,
            "pickup_code": pickup_code, "child_id": child_id}


def register_bag(conn: sqlite3.Connection, *, bag_no: str, material: str = "",
                 allergy_note: str = "", spare: bool = False) -> int:
    """登记睡袋。编号全库唯一（重复抛 DomainError）。"""
    try:
        cur = conn.execute(
            "INSERT INTO sleeping_bags(bag_no, material, allergy_note, spare)"
            " VALUES (?,?,?,?)",
            (bag_no, material, allergy_note, 1 if spare else 0),
        )
        bag_id = cur.lastrowid
        if spare:
            conn.execute(
                "INSERT INTO spare_bags(bag_id, in_stock) VALUES (?,1)", (bag_id,))
        conn.execute(
            "INSERT INTO bag_events(bag_id, event_type, note) VALUES (?, 'registered', ?)",
            (bag_id, f"新袋登记，材质：{material or '未填'}"),
        )
        conn.commit()
        return bag_id
    except sqlite3.IntegrityError as e:
        raise DomainError(f"睡袋编号已存在：{bag_no}") from e


def assign_bag(conn: sqlite3.Connection, *, bag_no: str, child_code: str,
               slot_no: str | None = None, actor: str = "老师") -> None:
    bag = _require_bag(conn, bag_no)
    child = _require_child(conn, child_code)
    if bag["retired"]:
        raise DomainError(f"{bag_no} 已报废，不能再分配")
    if _active_assignment_for_bag(conn, bag["id"]):
        raise DomainError(f"{bag_no} 已分配给其他孩子，不能重复分配")
    if _active_assignment_for_child(conn, child["id"]):
        raise DomainError(f"{child_code} 已有在用睡袋，不能同时分配两个")
    slot_id = None
    if slot_no is not None:
        slot_id = _require_slot(conn, child["class_id"], slot_no)["id"]
    try:
        conn.execute(
            "INSERT INTO assignments(bag_id, child_id, slot_id, status)"
            " VALUES (?,?,?, 'at_school')",
            (bag["id"], child["id"], slot_id),
        )
        conn.execute(
            "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
            " VALUES (?,?, 'assigned', ?, ?)",
            (bag["id"], child["id"], actor,
             f"分配给 {child['code']}，格位 {slot_no or '未指定'}"),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise DomainError(f"分配失败（格位可能已被占用）：{e}") from e


# ---------------------------------------------------------------- 查询

STATUS_TEXT = {"at_school": "在园", "taken_home": "已领回", "spare_in_use": "备用袋使用中"}


def class_board(conn: sqlite3.Connection, class_id: int) -> list[sqlite3.Row]:
    """班级页：以儿童代号 + 格位号定位。"""
    return list(conn.execute(
        """
        SELECT ch.id AS child_id, ch.code AS child_code, ch.name AS child_name,
               ch.allergy_note AS child_allergy,
               cs.slot_no, b.bag_no, b.material, b.allergy_note AS bag_allergy,
               a.status, a.label_damaged, a.id AS assignment_id,
               cl.name AS class_name,
               a.pending_class_id, a.pending_slot_id
          FROM children ch
          JOIN classes cl ON cl.id = ch.class_id
          LEFT JOIN assignments a ON a.child_id = ch.id AND a.ended_at IS NULL
          LEFT JOIN sleeping_bags b ON b.id = a.bag_id
          LEFT JOIN cubby_slots cs ON cs.id = a.slot_id
         WHERE ch.class_id = ? AND ch.active = 1
         ORDER BY cs.slot_no IS NULL, cs.slot_no, ch.code
        """, (class_id,)))


def list_classes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM classes ORDER BY id"))


def list_slots(conn: sqlite3.Connection, class_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM cubby_slots WHERE class_id=? ORDER BY slot_no", (class_id,)))


def spare_stock(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT b.bag_no, b.material, s.in_stock, s.note,
               (SELECT ch.code FROM assignments a JOIN children ch ON ch.id=a.child_id
                 WHERE a.bag_id=b.id AND a.ended_at IS NULL) AS used_by_code
          FROM spare_bags s JOIN sleeping_bags b ON b.id = s.bag_id
         WHERE b.retired = 0
         ORDER BY b.bag_no
        """))


def bag_chain(conn: sqlite3.Connection, bag_no: str) -> dict:
    bag = _require_bag(conn, bag_no)
    events = list(conn.execute(
        """
        SELECT e.*, ch.code AS child_code FROM bag_events e
          LEFT JOIN children ch ON ch.id = e.child_id
         WHERE e.bag_id = ? ORDER BY e.id
        """, (bag["id"],)))
    photos = list(conn.execute(
        "SELECT * FROM label_photos WHERE bag_id=? ORDER BY id DESC", (bag["id"],)))
    return {"bag": bag, "events": events, "photos": photos}


def parent_dashboard(conn: sqlite3.Connection, phone: str) -> dict:
    """家长端：仅返回本人孩子的睡袋状态，不泄露其他孩子信息。"""
    parent = conn.execute(
        "SELECT * FROM parents WHERE phone = ?", (phone,)).fetchone()
    if not parent:
        raise NotFound("未找到该手机号对应的家长账户")
    row = conn.execute(
        """
        SELECT ch.code AS child_code, ch.name AS child_name, ch.allergy_note,
               cl.name AS class_name, cs.slot_no,
               b.bag_no, b.material, b.allergy_note AS bag_allergy, b.spare,
               a.status, a.label_damaged, a.id AS assignment_id
          FROM parents p
          JOIN children ch ON ch.id = p.child_id
          JOIN classes cl ON cl.id = ch.class_id
          LEFT JOIN assignments a ON a.child_id = ch.id AND a.ended_at IS NULL
          LEFT JOIN sleeping_bags b ON b.id = a.bag_id
          LEFT JOIN cubby_slots cs ON cs.id = a.slot_id
         WHERE p.id = ?
        """, (parent["id"],)).fetchone()
    auths = list(conn.execute(
        """
        SELECT pa.*, b.bag_no FROM pickup_auths pa
          JOIN sleeping_bags b ON b.id = pa.bag_id
         WHERE pa.child_id = ? ORDER BY pa.id DESC
        """, (parent["child_id"],)))
    return {"parent": parent, "view": row, "auths": auths}


# ---------------------------------------------------------------- 周末领回

def pickup(conn: sqlite3.Connection, *, code: str, actor: str = "") -> dict:
    """扫码领回：code 可以是家长本人领回码或代领授权码。"""
    parent = conn.execute(
        "SELECT * FROM parents WHERE pickup_code = ?", (code,)).fetchone()
    if parent:
        return _pickup_parent(conn, parent, actor=actor or f"家长 {parent['name']}")
    auth = conn.execute(
        "SELECT * FROM pickup_auths WHERE code = ?", (code,)).fetchone()
    if auth:
        return _pickup_proxy(conn, auth, actor=actor)
    raise AuthCodeError("领回码无效：不是家长码也不是有效代领码")


def _pickup_parent(conn, parent, *, actor: str) -> dict:
    a = _active_assignment_for_child(conn, parent["child_id"])
    if not a:
        raise DomainError("孩子当前没有在用睡袋，无法领回")
    return _mark_taken_home(conn, a, actor=actor, auth=None)


def _pickup_proxy(conn, auth, *, actor: str) -> dict:
    # 撤销 / 有效期 / 是否使用过 —— 逐一给出明确原因
    if auth["revoked_at"]:
        raise AuthCodeError("该代领授权已被家长撤销，旧码已失效")
    now = _now()
    if now < datetime.strptime(auth["valid_from"], "%Y-%m-%d %H:%M:%S"):
        raise AuthCodeError("代领授权尚未生效")
    if now > datetime.strptime(auth["valid_until"], "%Y-%m-%d %H:%M:%S"):
        raise AuthCodeError("代领授权已过有效期")
    if auth["used_at"]:
        raise AuthCodeError("该代领码已使用，不能重复领取")
    a = _active_assignment_for_child(conn, auth["child_id"])
    if not a:
        raise DomainError("孩子当前没有在用睡袋")
    if a["bag_id"] != auth["bag_id"]:
        raise AuthCodeError("授权睡袋与孩子当前在用睡袋不一致，请联系家长重新授权")
    actor = actor or f"代领人 {auth['proxy_name']}"
    result = _mark_taken_home(conn, a, actor=actor, auth=auth)
    conn.execute("UPDATE pickup_auths SET used_at=? WHERE id=?",
                 (_fmt(now), auth["id"]))
    conn.commit()
    return result


def _mark_taken_home(conn, a, *, actor: str, auth) -> dict:
    if a["status"] == "taken_home":
        raise DomainError("该睡袋已领回，不能重复领取")
    bag = conn.execute("SELECT * FROM sleeping_bags WHERE id=?",
                       (a["bag_id"],)).fetchone()
    child = conn.execute("SELECT * FROM children WHERE id=?",
                         (a["child_id"],)).fetchone()
    conn.execute("UPDATE assignments SET status='taken_home' WHERE id=?", (a["id"],))
    conn.execute(
        "INSERT INTO bag_events(bag_id, child_id, event_type, actor, auth_id, note)"
        " VALUES (?,?, 'taken_home', ?, ?, ?)",
        (bag["id"], child["id"], actor, auth["id"] if auth else None,
         "代领授权码领回" if auth else "家长本人码领回"))
    conn.commit()
    return {"bag_no": bag["bag_no"], "child_code": child["code"],
            "status": "taken_home", "actor": actor}


def grant_proxy(conn: sqlite3.Connection, *, parent_phone: str,
                proxy_name: str, proxy_phone: str = "",
                valid_from: datetime | None = None,
                valid_until: datetime | None = None,
                code: str | None = None) -> dict:
    parent = conn.execute("SELECT * FROM parents WHERE phone=?",
                          (parent_phone,)).fetchone()
    if not parent:
        raise NotFound("家长账户不存在")
    a = _active_assignment_for_child(conn, parent["child_id"])
    if not a:
        raise DomainError("孩子当前没有在用睡袋，无法授权代领")
    now = _now()
    valid_from = valid_from or now - timedelta(minutes=5)
    valid_until = valid_until or (now + timedelta(days=2))
    code = code or f"D-{secrets.token_hex(4).upper()}"
    try:
        cur = conn.execute(
            """
            INSERT INTO pickup_auths(child_id, bag_id, code, proxy_name, proxy_phone,
                                     granted_by, valid_from, valid_until)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (parent["child_id"], a["bag_id"], code, proxy_name, proxy_phone,
             parent["id"], _fmt(valid_from), _fmt(valid_until)))
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise DomainError(f"授权码重复：{code}") from e
    return {"id": cur.lastrowid, "code": code, "proxy_name": proxy_name,
            "valid_until": _fmt(valid_until)}


def revoke_proxy(conn: sqlite3.Connection, *, code: str,
                parent_phone: str | None = None) -> None:
    auth = conn.execute("SELECT * FROM pickup_auths WHERE code=?",
                        (code,)).fetchone()
    if not auth:
        raise NotFound("授权码不存在")
    if parent_phone is not None:
        parent = conn.execute("SELECT * FROM parents WHERE phone=?",
                              (parent_phone,)).fetchone()
        if not parent or parent["child_id"] != auth["child_id"]:
            raise DomainError("只能撤销本人孩子的代领授权")
    if auth["revoked_at"]:
        raise DomainError("该授权此前已撤销")
    conn.execute("UPDATE pickup_auths SET revoked_at=? WHERE id=?",
                 (_fmt(_now()), auth["id"]))
    conn.commit()


# ---------------------------------------------------------------- 周一送回核验

def verify_return(conn: sqlite3.Connection, *, bag_no: str, child_code: str,
                  clean_status: str, label_intact: bool, seen: bool,
                  actor: str = "值班老师") -> dict:
    # 硬规则：没见到实物一律不能标记送回
    if not seen:
        raise DomainError("未见实物，不能标记送回：请当面核对睡袋后再勾选")
    if clean_status not in CLEAN_STATES:
        raise DomainError("清洁状态取值不合法")
    bag = _require_bag(conn, bag_no)
    child = _require_child(conn, child_code)
    a = _active_assignment_for_child(conn, child["id"])
    if not a:
        raise DomainError("孩子没有活动的睡袋记录")
    if a["status"] != "taken_home":
        raise DomainError("睡袋当前不是已领回状态，无需送回核验")
    if a["bag_id"] != bag["id"]:
        owner = conn.execute(
            """
            SELECT ch.code FROM assignments x JOIN children ch ON ch.id=x.child_id
             WHERE x.bag_id=? AND x.ended_at IS NULL
            """, (bag["id"],)).fetchone()
        raise WrongBagError(
            f"实物编号 {bag_no} 与 {child_code} 当前登记睡袋不符，"
            f"疑似拿错（{bag_no} 登记属于 {owner['code'] if owner else '无人'}），"
            "请先换回本人的睡袋，不能标记送回",
            scanned_bag_no=bag_no,
            owner_code=owner["code"] if owner else None)

    warnings: list[str] = []
    landed_note = ""
    new_slot_id = a["slot_id"]
    if a["pending_slot_id"]:
        new_slot_id = a["pending_slot_id"]
        pslot = conn.execute("SELECT slot_no FROM cubby_slots WHERE id=?",
                             (new_slot_id,)).fetchone()
        landed_note = f"转班挂起落位：新班格位 {pslot['slot_no']}"
    try:
        conn.execute(
            """
            UPDATE assignments
               SET status='at_school', slot_id=?, label_damaged=?,
                   pending_class_id=NULL, pending_slot_id=NULL
             WHERE id=?
            """,
            (new_slot_id, 0 if label_intact else 1, a["id"]))
        conn.execute(
            """
            INSERT INTO bag_events(bag_id, child_id, event_type, actor,
                                   clean_status, note)
            VALUES (?,?, 'returned', ?, ?, ?)
            """,
            (bag["id"], child["id"], actor, clean_status,
             CLEAN_LABELS[clean_status] + (("；" + landed_note) if landed_note else "")))
        if not label_intact:
            conn.execute(
                """
                INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)
                VALUES (?,?, 'missing_label', ?, '送回时标签脱落，已挂待补贴警示')
                """, (bag["id"], child["id"], actor))
            warnings.append("标签脱落：已登记并在班级页挂警示，补贴后解除")
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise DomainError(f"落位失败（新格位可能被占用）：{e}") from e

    if clean_status != "clean":
        from .messages import send_message
        parent = conn.execute(
            "SELECT * FROM parents WHERE child_id=?", (child["id"],)).fetchone()
        if parent:
            send_message(conn, parent["phone"], "clean_warning",
                         "睡袋清洁状态提醒",
                         f"{child['code']} 的睡袋 {bag_no} 本次核验为"
                         f"“{CLEAN_LABELS[clean_status]}”，请本周末领回后按要求清洗。")
        warnings.append(f"清洁状态：{CLEAN_LABELS[clean_status]}，已发家长提醒")
    if landed_note:
        warnings.append(landed_note)
    return {"bag_no": bag_no, "child_code": child_code,
            "status": "at_school", "warnings": warnings}


def mark_not_returned(conn: sqlite3.Connection, *, child_code: str,
                      note: str = "", actor: str = "值班老师") -> dict:
    child = _require_child(conn, child_code)
    a = _active_assignment_for_child(conn, child["id"])
    if not a or a["status"] != "taken_home":
        raise DomainError("只有已领回未送回的睡袋才能登记未带回")
    bag = conn.execute("SELECT * FROM sleeping_bags WHERE id=?",
                       (a["bag_id"],)).fetchone()
    conn.execute(
        "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
        " VALUES (?,?, 'not_returned', ?, ?)",
        (bag["id"], child["id"], actor, note or "周一未带回"))
    conn.commit()
    from .messages import send_message
    parent = conn.execute("SELECT * FROM parents WHERE child_id=?",
                          (child["id"],)).fetchone()
    if parent:
        send_message(conn, parent["phone"], "not_returned",
                     "睡袋未送回提醒",
                     f"{child['code']} 的睡袋 {bag['bag_no']} 今天尚未送回，"
                     "请尽快带来；如已由他人代交请告知老师。")
    return {"bag_no": bag["bag_no"], "event": "not_returned"}


def record_swap_after_pickup(conn: sqlite3.Connection, *, code_a: str,
                             code_b: str, note: str = "") -> dict:
    """两家都已领回、事后发现实物互错（物品链上登记错拿，不改动归属）。"""
    ch_a = _require_child(conn, code_a)
    ch_b = _require_child(conn, code_b)
    a_a = _active_assignment_for_child(conn, ch_a["id"])
    a_b = _active_assignment_for_child(conn, ch_b["id"])
    if not a_a or a_a["status"] != "taken_home":
        raise DomainError(f"{code_a} 的睡袋须处于已领回状态")
    if not a_b or a_b["status"] != "taken_home":
        raise DomainError(f"{code_b} 的睡袋须处于已领回状态")
    bag_a, bag_b = a_a["bag_id"], a_b["bag_id"]
    conn.execute(
        "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
        " VALUES (?,?, 'wrong_swap', '值班老师', ?)",
        (bag_a, ch_a["id"], f"错拿事件：与 {code_b} 家实物互混，待换回。{note}"))
    conn.execute(
        "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
        " VALUES (?,?, 'wrong_swap', '值班老师', ?)",
        (bag_b, ch_b["id"], f"错拿事件：与 {code_a} 家实物互混，待换回。{note}"))
    conn.commit()
    from .messages import send_message
    for ch, other_ch, bag in ((ch_a, ch_b, bag_b), (ch_b, ch_a, bag_a)):
        parent = conn.execute("SELECT * FROM parents WHERE child_id=?",
                              (ch["id"],)).fetchone()
        other_bag = conn.execute("SELECT bag_no FROM sleeping_bags WHERE id=?",
                                 (bag,)).fetchone()["bag_no"]
        if parent:
            send_message(conn, parent["phone"], "wrong_swap",
                         "睡袋拿错提醒",
                         f"本周末领回时发现睡袋可能拿错：{ch['code']} 家手上的实物"
                         f"疑似编号 {other_bag}（属于 {other_ch['code']}），"
                         "请核对标签，周一先换回再做送回核验。")
    return {"swapped": [code_a, code_b]}


# ---------------------------------------------------------------- 污损 / 备用袋

def replace_with_spare(conn: sqlite3.Connection, *, child_code: str,
                       damaged_bag_no: str, spare_bag_no: str,
                       reason: str, actor: str = "值班老师") -> dict:
    child = _require_child(conn, child_code)
    damaged = _require_bag(conn, damaged_bag_no)
    spare = _require_bag(conn, spare_bag_no)
    if not spare["spare"]:
        raise DomainError(f"{spare_bag_no} 不是备用睡袋")
    stock = conn.execute("SELECT * FROM spare_bags WHERE bag_id=?",
                         (spare["id"],)).fetchone()
    if not stock or not stock["in_stock"]:
        raise DomainError(f"备用袋 {spare_bag_no} 当前不在库，无法启用")
    a = _active_assignment_for_child(conn, child["id"])
    slot_id = a["slot_id"] if a else None
    if a:
        if a["bag_id"] != damaged["id"]:
            raise DomainError(f"{child_code} 当前在用的不是 {damaged_bag_no}")
        conn.execute("UPDATE assignments SET ended_at=? WHERE id=?",
                     (_fmt(_now()), a["id"]))
    conn.execute(
        "UPDATE sleeping_bags SET retired=1, retire_reason=? WHERE id=?",
        (reason, damaged["id"]))
    conn.execute(
        "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
        " VALUES (?,?, 'damaged_replace', ?, ?)",
        (damaged["id"], child["id"], actor, f"污损报废更换：{reason}"))
    try:
        conn.execute(
            "INSERT INTO assignments(bag_id, child_id, slot_id, status)"
            " VALUES (?,?,?, 'spare_in_use')",
            (spare["id"], child["id"], slot_id))
    except sqlite3.IntegrityError as e:
        raise DomainError(f"备用袋启用失败：{e}") from e
    conn.execute("UPDATE spare_bags SET in_stock=0 WHERE bag_id=?", (spare["id"],))
    conn.execute(
        "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
        " VALUES (?,?, 'spare_issued', ?, ?)",
        (spare["id"], child["id"], actor,
         f"备用袋启用，替换污损袋 {damaged_bag_no}"))
    conn.commit()
    return {"retired": damaged_bag_no, "spare": spare_bag_no}


def return_spare(conn: sqlite3.Connection, *, child_code: str,
                 new_bag_no: str | None = None, actor: str = "值班老师") -> dict:
    child = _require_child(conn, child_code)
    a = _active_assignment_for_child(conn, child["id"])
    if not a:
        raise DomainError("孩子没有在用睡袋")
    spare = conn.execute("SELECT * FROM sleeping_bags WHERE id=?",
                         (a["bag_id"],)).fetchone()
    if not spare["spare"]:
        raise DomainError("当前在用的不是备用袋")
    slot_id = a["slot_id"]
    conn.execute("UPDATE assignments SET ended_at=? WHERE id=?",
                 (_fmt(_now()), a["id"]))
    conn.execute("UPDATE spare_bags SET in_stock=1 WHERE bag_id=?", (spare["id"],))
    conn.execute(
        "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
        " VALUES (?,?, 'spare_returned', ?, '备用袋归还入库')",
        (spare["id"], child["id"], actor))
    if new_bag_no:
        new_bag = _require_bag(conn, new_bag_no)
        if new_bag["retired"] or new_bag["spare"]:
            raise DomainError("新睡袋不能是报废袋或备用袋")
        try:
            conn.execute(
                "INSERT INTO assignments(bag_id, child_id, slot_id, status)"
                " VALUES (?,?,?, 'at_school')",
                (new_bag["id"], child["id"], slot_id))
        except sqlite3.IntegrityError as e:
            raise DomainError(f"新袋分配失败：{e}") from e
        conn.execute(
            "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
            " VALUES (?,?, 'assigned', ?, '污损更换后的新睡袋')",
            (new_bag["id"], child["id"], actor))
    conn.commit()
    return {"spare_returned": spare["bag_no"], "new_bag": new_bag_no}


# ---------------------------------------------------------------- 转班交接

def transfer_child(conn: sqlite3.Connection, *, child_code: str,
                   to_class_id: int, new_slot_no: str,
                   note: str = "", actor: str = "教务老师") -> dict:
    child = _require_child(conn, child_code)
    if not _class_exists(conn, to_class_id):
        raise NotFound("目标班级不存在")
    new_slot = _require_slot(conn, to_class_id, new_slot_no)
    a = _active_assignment_for_child(conn, child["id"])
    if not a:
        raise DomainError("孩子没有在用睡袋，无法做转班交接")
    bag = conn.execute("SELECT * FROM sleeping_bags WHERE id=?",
                       (a["bag_id"],)).fetchone()
    now = _fmt(_now())

    if a["status"] == "taken_home":
        # 睡袋在家长家：孩子与班级关系立刻转走，睡袋送回时落位新格位
        conn.execute("UPDATE children SET class_id=? WHERE id=?",
                     (to_class_id, child["id"]))
        conn.execute(
            "UPDATE assignments SET slot_id=NULL, pending_class_id=?, "
            "pending_slot_id=? WHERE id=?",
            (to_class_id, new_slot["id"], a["id"]))
        status = "bag_away_pending"
        handover_note = (f"转班至新班，睡袋在家长家中，送回核验时落位格位 "
                         f"{new_slot_no}。{note}")
    else:
        # 实物在园：当场交接，孩子 + 格位一起切走
        conn.execute("UPDATE children SET class_id=? WHERE id=?",
                     (to_class_id, child["id"]))
        try:
            conn.execute("UPDATE assignments SET slot_id=? WHERE id=?",
                         (new_slot["id"], a["id"]))
        except sqlite3.IntegrityError as e:
            raise DomainError(f"新格位 {new_slot_no} 已被占用：{e}") from e
        status = "completed"
        handover_note = f"实物在园当场交接，落位新班格位 {new_slot_no}。{note}"

    conn.execute(
        "INSERT INTO bag_events(bag_id, child_id, event_type, actor, note)"
        " VALUES (?,?, 'transfer_handover', ?, ?)",
        (bag["id"], child["id"], actor, handover_note))
    cur = conn.execute(
        "INSERT INTO transfers(child_id, bag_id, from_class, to_class,"
        " new_slot_id, status, note) VALUES (?,?,?,?,?,?,?)",
        (child["id"], bag["id"], child["class_id"], to_class_id,
         new_slot["id"], status, handover_note))
    conn.commit()
    return {"transfer_id": cur.lastrowid, "child_code": child_code,
            "bag_no": bag["bag_no"], "status": status}


# ---------------------------------------------------------------- 内部工具

def _class_exists(conn, class_id: int) -> bool:
    return conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone() is not None


def _require_bag(conn, bag_no: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sleeping_bags WHERE bag_no=?",
                       (bag_no,)).fetchone()
    if not row:
        raise NotFound(f"睡袋编号不存在：{bag_no}")
    return row


def _require_child(conn, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM children WHERE code=?",
                       (code,)).fetchone()
    if not row:
        raise NotFound(f"儿童代号不存在：{code}")
    return row


def _require_slot(conn, class_id: int, slot_no: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM cubby_slots WHERE class_id=? AND slot_no=?",
        (class_id, slot_no)).fetchone()
    if not row:
        raise NotFound(f"本班不存在格位 {slot_no}")
    return row


def _active_assignment_for_bag(conn, bag_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM assignments WHERE bag_id=? AND ended_at IS NULL",
        (bag_id,)).fetchone()


def _active_assignment_for_child(conn, child_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM assignments WHERE child_id=? AND ended_at IS NULL",
        (child_id,)).fetchone()
