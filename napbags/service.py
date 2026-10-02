"""业务服务层。

所有写操作在单个 SQLite 事务内完成，失败抛 ServiceError 并回滚。
关键不变量：
  * bag_no 全局唯一（DB UNIQUE）；
  * 同一睡袋同时只有一个在园孩子占用（部分唯一索引）；
  * 老师必须“已见实物”才能确认送回（physically_seen=True）；
  * 代领授权码有有效期，撤销后旧码立即失效。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Optional

from .db import SCHEMA
from .messages import MockMessageAdapter


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class NapService:
    def __init__(self, db_path: str, clock: Optional[Callable[[], datetime]] = None,
                 message_adapter=None):
        self.db_path = db_path
        self._clock = clock or datetime.now
        self.messages = message_adapter or MockMessageAdapter()
        self.init_db()

    # ---------- 基础 ----------
    def init_db(self) -> None:
        with self._tx() as tx:
            tx.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    class _Tx:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            self.conn.close()
            return False

    def _tx(self):
        return NapService._Tx(self._connect())

    def now(self) -> datetime:
        return self._clock()

    def ts(self) -> str:
        return self.now().isoformat(timespec="seconds")

    # ---------- 班级 / 格位 ----------
    def create_class(self, name: str, rows: int, cols: int) -> int:
        if rows < 1 or cols < 1:
            raise ServiceError("格位行列数必须大于 0")
        with self._tx() as tx:
            try:
                cur = tx.execute(
                    "INSERT INTO classes(name, slot_rows, slot_cols) VALUES (?,?,?)",
                    (name, rows, cols))
            except sqlite3.IntegrityError:
                raise ServiceError(f"班级 {name} 已存在", "duplicate")
            class_id = cur.lastrowid
            for r in range(1, rows + 1):
                for c in range(1, cols + 1):
                    tx.execute(
                        'INSERT INTO slots(class_id, code, label, "row", col)'
                        " VALUES (?,?,?,?,?)",
                        (class_id, f"R{r}C{c:02d}", f"{r}-{c:02d}", r, c))
            return class_id

    def list_classes(self):
        with self._tx() as tx:
            return [dict(r) for r in tx.execute(
                "SELECT * FROM classes ORDER BY id")]

    def class_board(self, class_id: int):
        """班级页数据：格位 + 儿童代号 + 睡袋状态，按格位定位。"""
        with self._tx() as tx:
            cls = tx.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
            if not cls:
                raise ServiceError("班级不存在", "not_found")
            slots = [dict(r) for r in tx.execute(
                "SELECT * FROM slots WHERE class_id=? ORDER BY `row`, col", (class_id,))]
            for s in slots:
                if s["assigned_child_id"]:
                    ch = tx.execute(
                        "SELECT c.*, b.bag_no, b.material, b.allergy_note, b.status,"
                        " b.label_ok, b.is_spare, b.id AS bag_id"
                        " FROM children c LEFT JOIN sleeping_bags b"
                        " ON b.id = c.current_bag_id WHERE c.id=?",
                        (s["assigned_child_id"],)).fetchone()
                    s["child"] = dict(ch) if ch else None
                else:
                    spare = tx.execute(
                        "SELECT * FROM sleeping_bags WHERE slot_id=? AND is_spare=1",
                        (s["id"],)).fetchone()
                    s["spare"] = dict(spare) if spare else None
            return dict(cls), slots

    def _slot(self, tx, class_id: int, code: str) -> sqlite3.Row:
        row = tx.execute(
            "SELECT * FROM slots WHERE class_id=? AND code=?", (class_id, code)).fetchone()
        if not row:
            raise ServiceError(f"格位 {code} 不存在", "not_found")
        return row

    # ---------- 儿童 / 监护人 ----------
    def register_child(self, code: str, class_id: int, slot_code: str,
                       bag_no: str, material: str, allergy_note: str = "",
                       actor: str = "老师", photo_path: Optional[str] = None) -> int:
        ts = self.ts()
        with self._tx() as tx:
            slot = self._slot(tx, class_id, slot_code)
            if slot["assigned_child_id"]:
                raise ServiceError(f"格位 {slot_code} 已有儿童", "slot_taken")
            try:
                bag_cur = tx.execute(
                    "INSERT INTO sleeping_bags"
                    "(bag_no, material, allergy_note, is_spare, slot_id, status,"
                    " label_ok, created_at) VALUES (?,?,?,0,?,'stored',1,?)",
                    (bag_no, material, allergy_note, slot["id"], ts))
            except sqlite3.IntegrityError:
                raise ServiceError(f"睡袋编号 {bag_no} 已存在", "duplicate_bag")
            bag_id = bag_cur.lastrowid
            try:
                cur = tx.execute(
                    "INSERT INTO children(code, class_id, current_bag_id, active)"
                    " VALUES (?,?,?,1)", (code, class_id, bag_id))
            except sqlite3.IntegrityError:
                raise ServiceError(f"儿童代号 {code} 已存在", "duplicate_child")
            child_id = cur.lastrowid
            tx.execute("UPDATE slots SET assigned_child_id=? WHERE id=?",
                       (child_id, slot["id"]))
            self._event(tx, bag_id, child_id, "registered",
                        f"登记 {material}；格位 {slot['code']}", actor, ts)
            if photo_path:
                tx.execute(
                    "INSERT INTO label_photos(bag_id, path, event_type, ts)"
                    " VALUES (?,?, 'registered', ?)", (bag_id, photo_path, ts))
            return child_id

    def add_guardian(self, child_code: str, name: str,
                     token: Optional[str] = None) -> str:
        token = token or secrets.token_urlsafe(12)
        with self._tx() as tx:
            ch = self._child(tx, child_code)
            tx.execute(
                "INSERT INTO guardians(child_id, token, name) VALUES (?,?,?)",
                (ch["id"], token, name))
            return token

    def guardian_view(self, token: str) -> dict:
        """家长端：仅返回本人（令牌对应）孩子的睡袋状态。"""
        with self._tx() as tx:
            g = tx.execute("SELECT * FROM guardians WHERE token=?", (token,)).fetchone()
            if not g:
                raise ServiceError("家长令牌无效", "forbidden")
            ch = tx.execute("SELECT * FROM children WHERE id=?", (g["child_id"],)).fetchone()
            bag = None
            if ch["current_bag_id"]:
                bag = tx.execute("SELECT * FROM sleeping_bags WHERE id=?",
                                 (ch["current_bag_id"],)).fetchone()
            cls = tx.execute("SELECT * FROM classes WHERE id=?", (ch["class_id"],)).fetchone()
            events = []
            if bag:
                events = [dict(r) for r in tx.execute(
                    "SELECT * FROM events WHERE bag_id=? ORDER BY id DESC LIMIT 20",
                    (bag["id"],))]
            auths = [dict(r) for r in tx.execute(
                "SELECT * FROM authorizations WHERE child_id=? ORDER BY id DESC",
                (ch["id"],))]
            return {"guardian": dict(g), "child": dict(ch), "class": dict(cls),
                    "bag": dict(bag) if bag else None, "events": events,
                    "authorizations": auths}

    # ---------- 领回 / 送回 ----------
    def pickup(self, bag_no: str, proxy_code: Optional[str] = None,
               actor: str = "值班老师", note: str = "") -> dict:
        """家长扫码领回。重复领取、错拿状态下均拦截。"""
        ts = self.ts()
        with self._tx() as tx:
            bag, ch = self._bag_with_child(tx, bag_no)
            if bag["status"] == "taken_home":
                raise ServiceError(
                    f"{bag_no} 已被领回家，不能重复领取", "duplicate_pickup")
            if bag["status"] in ("damaged", "lost"):
                raise ServiceError(f"{bag_no} 已{bag['status']}，不可领取", "bad_state")
            if bag["status"] not in ("stored", "in_use"):
                raise ServiceError(f"{bag_no} 当前状态 {bag['status']}，不可领取",
                                   "bad_state")
            proxy = None
            if proxy_code:
                proxy = self._consume_proxy(tx, ch["id"], proxy_code, ts)
                tx.execute(
                    "INSERT INTO incidents(child_id, bag_id, kind, detail, created_at)"
                    " VALUES (?,?, 'proxy_pickup', ?, ?)",
                    (ch["id"], bag["id"],
                     f"代领人 {proxy['proxy_name']}（{proxy['relation']}）"
                     f"凭授权码领回", ts))
                self._event(tx, bag["id"], ch["id"], "authorization_used",
                            f"代领授权码 {proxy_code[:4]}… 已使用", actor, ts)
            tx.execute("UPDATE sleeping_bags SET status='taken_home' WHERE id=?",
                       (bag["id"],))
            self._event(tx, bag["id"], ch["id"], "pickup",
                        note or ("周末领回清洗" if not proxy_code
                                 else f"由代领人 {proxy['proxy_name']} 领回"),
                        actor, ts)
            msg = self._send(tx, ch["id"], "pickup",
                             f"【午睡袋】{ch['code']} 的睡袋 {bag_no} 已领回，"
                             "请周末清洗，周一送回。", ts)
            return {"bag": bag_no, "status": "taken_home",
                    "proxy": proxy["proxy_name"] if proxy else None,
                    "receipt": msg}

    def request_return(self, token: str, note: str = "",
                       cleaning_done: bool = True) -> dict:
        """家长送回登记：仅表示“已送到、请老师核验”，不等于已送回入账。"""
        ts = self.ts()
        with self._tx() as tx:
            g = tx.execute("SELECT * FROM guardians WHERE token=?", (token,)).fetchone()
            if not g:
                raise ServiceError("家长令牌无效", "forbidden")
            ch = tx.execute("SELECT * FROM children WHERE id=?", (g["child_id"],)).fetchone()
            bag = tx.execute("SELECT * FROM sleeping_bags WHERE id=?",
                             (ch["current_bag_id"],)).fetchone()
            if not bag:
                raise ServiceError("当前没有在管睡袋", "no_bag")
            if bag["status"] != "taken_home":
                raise ServiceError("只有已领回家的睡袋才能登记送回", "bad_state")
            tx.execute("UPDATE sleeping_bags SET status='return_pending' WHERE id=?",
                       (bag["id"],))
            self._event(tx, bag["id"], ch["id"], "return_request",
                        f"家长送交，清洁{'已完成' if cleaning_done else '待复查'}。{note}",
                        g["name"], ts)
            self._send(tx, ch["id"], "return_pending",
                       f"【午睡袋】{ch['code']} 的睡袋已送交，等待老师见实核验。", ts)
            return {"bag": bag["bag_no"], "status": "return_pending"}

    def confirm_return(self, bag_no: str, *, physically_seen: bool,
                       label_ok: bool = True, clean: bool = True,
                       spare_used: bool = False, spare_note: str = "",
                       actor: str = "值班老师", note: str = "") -> dict:
        """老师见到实物后核对标签与清洁状态，确认送回。

        physically_seen 必须为 True——未见实物不得标记送回。
        """
        if not physically_seen:
            raise ServiceError("未见实物，不能标记送回", "not_seen")
        ts = self.ts()
        with self._tx() as tx:
            bag, ch = self._bag_with_child(tx, bag_no)
            if bag["status"] not in ("return_pending", "taken_home"):
                raise ServiceError(
                    f"{bag_no} 当前为 {bag['status']}，无需核验送回", "bad_state")
            # 备用袋即便经历周末领回，仍在“使用中”，不能因此回到库存
            new_status = "in_use" if bag["is_spare"] else "stored"
            tx.execute(
                "UPDATE sleeping_bags SET status=?, label_ok=? WHERE id=?",
                (new_status, 1 if label_ok else 0, bag["id"]))
            detail = f"标签{'完好' if label_ok else '异常'}，清洁{'合格' if clean else '不合格'}"
            if spare_used:
                detail += f"；本周曾使用备用袋：{spare_note or '详见记录'}"
            self._event(tx, bag["id"], ch["id"], "return_seen",
                        f"{detail}。{note}", actor, ts)
            if not label_ok:
                tx.execute(
                    "INSERT INTO incidents(child_id, bag_id, kind, detail, created_at)"
                    " VALUES (?,?, 'label_off', ?, ?)",
                    (ch["id"], bag["id"], "送回核验时发现标签问题", ts))
            self._send(tx, ch["id"], "return_confirm",
                       f"【午睡袋】{ch['code']} 的睡袋 {bag_no} 已经老师核验归位。", ts)
            return {"bag": bag_no, "status": "stored", "label_ok": label_ok,
                    "clean": clean}

    def monday_missing(self, bag_no: str, actor: str = "值班老师") -> dict:
        """周一未带回：单独记录并提醒家长。"""
        ts = self.ts()
        with self._tx() as tx:
            bag, ch = self._bag_with_child(tx, bag_no)
            if bag["status"] != "taken_home":
                raise ServiceError("睡袋已在校，无需记周一未带回", "bad_state")
            self._event(tx, bag["id"], ch["id"], "monday_missing",
                        "周一未带回", actor, ts)
            tx.execute(
                "INSERT INTO incidents(child_id, bag_id, kind, detail, created_at)"
                " VALUES (?,?, 'monday_missing', '周一未带回', ?)",
                (ch["id"], bag["id"], ts))
            msg = self._send(tx, ch["id"], "monday_reminder",
                             f"【午睡袋】提醒：{ch['code']} 的睡袋 {bag_no} "
                             "周一尚未送回，请尽快带来。", ts)
            return {"receipt": msg}

    # ---------- 错拿 ----------
    def record_mispick(self, wrong_bag_no: str, holder_child_code: str,
                       note: str = "", actor: str = "值班老师") -> int:
        """登记错拿：holder 家庭领回了不属于自己的 wrong_bag_no。"""
        ts = self.ts()
        with self._tx() as tx:
            wbag, owner = self._bag_with_child(tx, wrong_bag_no)
            holder = self._child(tx, holder_child_code)
            if owner["id"] == holder["id"]:
                raise ServiceError("睡袋本就属于该儿童，不构成错拿", "not_mispick")
            holder_bag = tx.execute(
                "SELECT * FROM sleeping_bags WHERE id=?",
                (holder["current_bag_id"],)).fetchone()
            detail = json.dumps({
                "wrong_bag_no": wrong_bag_no,
                "owner_child_code": owner["code"],
                "holder_child_code": holder["code"],
                "holder_own_bag_no": holder_bag["bag_no"] if holder_bag else None,
            }, ensure_ascii=False)
            cur = tx.execute(
                "INSERT INTO incidents(child_id, bag_id, kind, detail, created_at)"
                " VALUES (?,?, 'mispick', ?, ?)",
                (holder["id"], wbag["id"], detail, ts))
            self._event(tx, wbag["id"], owner["id"], "mispick",
                        f"被 {holder['code']} 家庭错拿。{note}", actor, ts)
            self._send(tx, owner["id"], "mispick",
                       f"【午睡袋】您的睡袋 {wrong_bag_no} 疑似被 "
                       f"{holder['code']} 家庭错拿，老师正在协调换回。", ts)
            self._send(tx, holder["id"], "mispick",
                       f"【午睡袋】本周末领回的 {wrong_bag_no} 并非 "
                       f"{holder['code']} 的睡袋，请核对标签并联系老师换回。", ts)
            return cur.lastrowid

    def resolve_mispick(self, incident_id: int, *, exchanged: bool,
                        actor: str = "值班老师", note: str = "") -> None:
        ts = self.ts()
        with self._tx() as tx:
            inc = tx.execute("SELECT * FROM incidents WHERE id=? AND kind='mispick'",
                             (incident_id,)).fetchone()
            if not inc:
                raise ServiceError("错拿记录不存在", "not_found")
            if inc["resolved_at"]:
                raise ServiceError("该错拿记录已处理", "already_resolved")
            info = json.loads(inc["detail"])
            if exchanged:
                # 双方实物已换回并经老师核对：错拿袋归位
                tx.execute(
                    "UPDATE sleeping_bags SET status='stored' WHERE bag_no=? AND status='taken_home'",
                    (info["wrong_bag_no"],))
                self._event(tx, inc["bag_id"], inc["child_id"], "recheck",
                            f"错拿已换回，实物核对无误。{note}", actor, ts)
            tx.execute("UPDATE incidents SET resolved_at=? WHERE id=?", (ts, incident_id))

    # ---------- 污损更换 / 备用袋 ----------
    def register_spare(self, bag_no: str, material: str, class_id: int,
                       slot_code: str, allergy_note: str = "") -> int:
        ts = self.ts()
        with self._tx() as tx:
            slot = self._slot(tx, class_id, slot_code)
            if slot["assigned_child_id"]:
                raise ServiceError("备用袋必须放在未分配儿童的空格位", "slot_taken")
            if tx.execute("SELECT 1 FROM sleeping_bags WHERE slot_id=?",
                          (slot["id"],)).fetchone():
                raise ServiceError("该格位已有睡袋", "slot_taken")
            try:
                cur = tx.execute(
                    "INSERT INTO sleeping_bags"
                    "(bag_no, material, allergy_note, is_spare, slot_id, status,"
                    " label_ok, created_at) VALUES (?,?,?,1,?,'stored',1,?)",
                    (bag_no, material, allergy_note, slot["id"], ts))
            except sqlite3.IntegrityError:
                raise ServiceError(f"睡袋编号 {bag_no} 已存在", "duplicate_bag")
            tx.execute(
                "INSERT INTO spare_ledger(bag_id, direction, reason, child_id, ts)"
                " VALUES (?, 'in', '备用袋入库', NULL, ?)", (cur.lastrowid, ts))
            return cur.lastrowid

    def spare_stock(self, class_id: Optional[int] = None) -> list:
        q = ("SELECT b.*, s.class_id FROM sleeping_bags b JOIN slots s ON s.id=b.slot_id"
             " WHERE b.is_spare=1 AND b.status='stored'")
        args = ()
        if class_id:
            q += " AND s.class_id=?"
            args = (class_id,)
        q += " ORDER BY b.bag_no"
        with self._tx() as tx:
            return [dict(r) for r in tx.execute(q, args)]

    def damage_replace(self, bag_no: str, reason: str,
                       actor: str = "值班老师") -> None:
        """污损退役：原袋下架，孩子暂时无袋（随后可启用备用袋）。"""
        ts = self.ts()
        with self._tx() as tx:
            bag, ch = self._bag_with_child(tx, bag_no)
            # 退役袋离开格位，避免与随后启用的备用袋共用同一存放格
            tx.execute(
                "UPDATE sleeping_bags SET status='damaged', slot_id=NULL WHERE id=?",
                (bag["id"],))
            tx.execute("UPDATE children SET current_bag_id=NULL WHERE id=?", (ch["id"],))
            self._event(tx, bag["id"], ch["id"], "damaged",
                        f"污损退役：{reason}", actor, ts)
            tx.execute(
                "INSERT INTO incidents(child_id, bag_id, kind, detail, created_at)"
                " VALUES (?,?, 'damage_replace', ?, ?)",
                (ch["id"], bag["id"], f"污损：{reason}", ts))
            self._send(tx, ch["id"], "damage",
                       f"【午睡袋】{ch['code']} 的睡袋 {bag_no} 因{reason}已停用，"
                       "园方将启用备用袋。", ts)

    def assign_spare(self, child_code: str, spare_bag_no: str,
                     reason: str = "原袋污损/标签问题", actor: str = "值班老师") -> dict:
        ts = self.ts()
        with self._tx() as tx:
            ch = self._child(tx, child_code)
            spare = tx.execute("SELECT * FROM sleeping_bags WHERE bag_no=?",
                               (spare_bag_no,)).fetchone()
            if not spare or not spare["is_spare"]:
                raise ServiceError("备用袋不存在", "not_found")
            if spare["status"] != "stored" or self._owner_of(tx, spare["id"]):
                raise ServiceError("该备用袋已在使用，不能重复分配", "spare_busy")
            old = tx.execute("SELECT * FROM sleeping_bags WHERE id=?",
                             (ch["current_bag_id"],)).fetchone() if ch["current_bag_id"] else None
            if old and old["status"] not in ("damaged", "lost"):
                raise ServiceError("孩子仍有在用睡袋，不能启用备用袋", "bad_state")
            slot = tx.execute("SELECT * FROM slots WHERE assigned_child_id=?",
                              (ch["id"],)).fetchone()
            tx.execute("UPDATE sleeping_bags SET slot_id=?, status='in_use' WHERE id=?",
                       (slot["id"], spare["id"]))
            try:
                tx.execute("UPDATE children SET current_bag_id=? WHERE id=?",
                           (spare["id"], ch["id"]))
            except sqlite3.IntegrityError:
                raise ServiceError("该备用袋已分配给其他孩子", "spare_busy")
            tx.execute(
                "INSERT INTO spare_ledger(bag_id, direction, reason, child_id, ts)"
                " VALUES (?, 'out', ?, ?, ?)",
                (spare["id"], reason, ch["id"], ts))
            self._event(tx, spare["id"], ch["id"], "spare_used",
                        f"启用备用袋（{reason}）", actor, ts)
            return {"child": child_code, "spare": spare_bag_no}

    def return_spare(self, child_code: str, actor: str = "值班老师") -> dict:
        """备用袋收回库存（如替换新袋后）。"""
        ts = self.ts()
        with self._tx() as tx:
            ch = self._child(tx, child_code)
            bag = tx.execute("SELECT * FROM sleeping_bags WHERE id=?",
                             (ch["current_bag_id"],)).fetchone() if ch["current_bag_id"] else None
            if not bag or not bag["is_spare"]:
                raise ServiceError("当前使用的不是备用袋", "not_spare")
            if bag["status"] in ("taken_home", "return_pending"):
                raise ServiceError("备用袋尚在校外，先完成核验归位再收回", "bad_state")
            free = self._free_spare_slot(tx, ch["class_id"], bag["id"])
            tx.execute("UPDATE sleeping_bags SET slot_id=?, status='stored' WHERE id=?",
                       (free["id"], bag["id"]))
            tx.execute("UPDATE children SET current_bag_id=NULL WHERE id=?", (ch["id"],))
            tx.execute(
                "INSERT INTO spare_ledger(bag_id, direction, reason, child_id, ts)"
                " VALUES (?, 'in', '备用袋收回', ?, ?)", (bag["id"], ch["id"], ts))
            self._event(tx, bag["id"], ch["id"], "spare_back",
                        "备用袋收回库存", actor, ts)
            return {"spare": bag["bag_no"], "slot": free["code"]}

    def register_replacement(self, child_code: str, bag_no: str, material: str,
                             allergy_note: str = "", actor: str = "值班老师") -> int:
        """污损更换：登记孩子的新睡袋（若在用备用袋则同时收回）。"""
        ts = self.ts()
        with self._tx() as tx:
            ch = self._child(tx, child_code)
            slot = tx.execute("SELECT * FROM slots WHERE assigned_child_id=?",
                              (ch["id"],)).fetchone()
            current = tx.execute("SELECT * FROM sleeping_bags WHERE id=?",
                                 (ch["current_bag_id"],)).fetchone() if ch["current_bag_id"] else None
            if current:
                if current["is_spare"]:
                    if current["status"] in ("taken_home", "return_pending"):
                        raise ServiceError("备用袋尚在校外，先完成核验再换新袋",
                                           "bad_state")
                    free = self._free_spare_slot(tx, ch["class_id"], current["id"])
                    tx.execute(
                        "UPDATE sleeping_bags SET slot_id=?, status='stored' WHERE id=?",
                        (free["id"], current["id"]))
                    tx.execute(
                        "INSERT INTO spare_ledger(bag_id, direction, reason, child_id, ts)"
                        " VALUES (?, 'in', '新袋到位，备用袋收回', ?, ?)",
                        (current["id"], ch["id"], ts))
                    self._event(tx, current["id"], ch["id"], "spare_back",
                                "新袋到位，备用袋收回", actor, ts)
                else:
                    raise ServiceError("原有睡袋未退役，不能登记替换袋", "bad_state")
            try:
                cur = tx.execute(
                    "INSERT INTO sleeping_bags"
                    "(bag_no, material, allergy_note, is_spare, slot_id, status,"
                    " label_ok, created_at) VALUES (?,?,?,0,?,'stored',1,?)",
                    (bag_no, material, allergy_note, slot["id"], ts))
            except sqlite3.IntegrityError:
                raise ServiceError(f"睡袋编号 {bag_no} 已存在", "duplicate_bag")
            tx.execute("UPDATE children SET current_bag_id=? WHERE id=?",
                       (cur.lastrowid, ch["id"]))
            self._event(tx, cur.lastrowid, ch["id"], "swap_bag",
                        f"污损更换新袋：{bag_no}（{material}）", actor, ts)
            return cur.lastrowid

    # ---------- 标签脱落 ----------
    def mark_label_off(self, bag_no: str, actor: str = "值班老师",
                       note: str = "") -> None:
        ts = self.ts()
        with self._tx() as tx:
            bag, ch = self._bag_with_child(tx, bag_no)
            tx.execute("UPDATE sleeping_bags SET label_ok=0, status='lost' WHERE id=?",
                       (bag["id"],))
            self._event(tx, bag["id"], ch["id"], "label_off",
                        f"标签脱落待查。{note}", actor, ts)
            tx.execute(
                "INSERT INTO incidents(child_id, bag_id, kind, detail, created_at)"
                " VALUES (?,?, 'label_off', ?, ?)",
                (ch["id"], bag["id"], note or "标签脱落", ts))

    def recheck_label(self, bag_no: str, actor: str = "值班老师",
                      note: str = "", photo_path: Optional[str] = None) -> None:
        """标签复查/补贴后恢复。"""
        ts = self.ts()
        with self._tx() as tx:
            bag = tx.execute("SELECT * FROM sleeping_bags WHERE bag_no=?",
                             (bag_no,)).fetchone()
            if not bag:
                raise ServiceError("睡袋不存在", "not_found")
            tx.execute(
                "UPDATE sleeping_bags SET label_ok=1,"
                " status=CASE WHEN status='lost' THEN 'stored' ELSE status END"
                " WHERE id=?", (bag["id"],))
            self._event(tx, bag["id"], None, "recheck",
                        f"标签复查恢复。{note}", actor, ts)
            if photo_path:
                tx.execute(
                    "INSERT INTO label_photos(bag_id, path, event_type, ts)"
                    " VALUES (?,?,'recheck',?)", (bag["id"], photo_path, ts))

    # ---------- 转班 ----------
    def transfer_child(self, child_code: str, new_class_id: int,
                       new_slot_code: str, actor: str = "值班老师") -> dict:
        """孩子转班：睡袋实物必须在校、标签完好、非备用袋，一次性完成交接。"""
        ts = self.ts()
        with self._tx() as tx:
            ch = self._child(tx, child_code)
            bag = tx.execute("SELECT * FROM sleeping_bags WHERE id=?",
                             (ch["current_bag_id"],)).fetchone() if ch["current_bag_id"] else None
            if not bag:
                raise ServiceError("孩子没有在管睡袋，先完成补袋再转班", "no_bag")
            if not bag["label_ok"]:
                raise ServiceError("标签异常，处理完标签再转班", "label_bad")
            if bag["is_spare"]:
                raise ServiceError("使用备用袋期间不能转班，请先收回备用袋",
                                   "spare_in_use")
            if bag["status"] != "stored":
                raise ServiceError("睡袋不在校格位，无法完成实物交接", "not_stored")
            new_slot = self._slot(tx, new_class_id, new_slot_code)
            if new_slot["assigned_child_id"]:
                raise ServiceError(f"目标格位 {new_slot_code} 已有人", "slot_taken")
            old_slot = tx.execute("SELECT * FROM slots WHERE assigned_child_id=?",
                                  (ch["id"],)).fetchone()
            old_cls = tx.execute("SELECT * FROM classes WHERE id=?",
                                 (ch["class_id"],)).fetchone()
            new_cls = tx.execute("SELECT * FROM classes WHERE id=?",
                                 (new_class_id,)).fetchone()
            # 交接
            tx.execute("UPDATE slots SET assigned_child_id=NULL WHERE id=?",
                       (old_slot["id"],))
            tx.execute("UPDATE slots SET assigned_child_id=? WHERE id=?",
                       (ch["id"], new_slot["id"]))
            tx.execute("UPDATE children SET class_id=? WHERE id=?",
                       (new_class_id, ch["id"]))
            tx.execute("UPDATE sleeping_bags SET slot_id=? WHERE id=?",
                       (new_slot["id"], bag["id"]))
            self._event(tx, bag["id"], ch["id"], "transfer",
                        f"{old_cls['name']} {old_slot['code']} → "
                        f"{new_cls['name']} {new_slot['code']}，实物当面交接",
                        actor, ts)
            tx.execute(
                "INSERT INTO incidents(child_id, bag_id, kind, detail, resolved_at,"
                " created_at) VALUES (?,?, 'transfer', ?, ?, ?)",
                (ch["id"], bag["id"],
                 f"{old_cls['name']} → {new_cls['name']}（{old_slot['code']}→"
                 f"{new_slot['code']}）", ts, ts))
            return {"child": child_code, "new_class": new_cls["name"],
                    "new_slot": new_slot["code"]}

    # ---------- 代领授权 ----------
    def create_authorization(self, token: str, proxy_name: str,
                             valid_until: Optional[datetime] = None,
                             valid_days: int = 7, relation: str = "") -> dict:
        now = self.now()
        if valid_until is None:
            valid_until = now + timedelta(days=valid_days)
        with self._tx() as tx:
            g = tx.execute("SELECT * FROM guardians WHERE token=?", (token,)).fetchone()
            if not g:
                raise ServiceError("家长令牌无效", "forbidden")
            code = secrets.token_hex(5).upper()
            tx.execute(
                "INSERT INTO authorizations"
                "(code, child_id, proxy_name, relation, valid_from, valid_until,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (code, g["child_id"], proxy_name, relation,
                 now.isoformat(timespec="seconds"),
                 valid_until.isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds")))
            return {"code": code, "proxy_name": proxy_name,
                    "valid_until": valid_until.isoformat(timespec="seconds")}

    def revoke_authorization(self, code: str) -> None:
        """撤销：旧码立即失效（revoked_at 落库即拒）。"""
        ts = self.ts()
        with self._tx() as tx:
            row = tx.execute("SELECT * FROM authorizations WHERE code=?",
                             (code,)).fetchone()
            if not row:
                raise ServiceError("授权码不存在", "not_found")
            if row["revoked_at"]:
                raise ServiceError("授权码已撤销", "already_revoked")
            tx.execute("UPDATE authorizations SET revoked_at=? WHERE id=?",
                       (ts, row["id"]))

    def list_incidents(self, open_only: bool = False, class_id: Optional[int] = None):
        q = ("SELECT i.*, c.code AS child_code, b.bag_no FROM incidents i"
             " LEFT JOIN children c ON c.id=i.child_id"
             " LEFT JOIN sleeping_bags b ON b.id=i.bag_id")
        where = []
        args = []
        if open_only:
            where.append("i.resolved_at IS NULL")
        if class_id:
            where.append("c.class_id=?")
            args.append(class_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY i.id DESC"
        with self._tx() as tx:
            return [dict(r) for r in tx.execute(q, args)]

    def bag_chain(self, bag_no: str) -> dict:
        with self._tx() as tx:
            bag = tx.execute(
                "SELECT b.*, c.code AS child_code, s.code AS slot_code,"
                " cl.name AS class_name FROM sleeping_bags b"
                " LEFT JOIN children c ON c.current_bag_id=b.id"
                " LEFT JOIN slots s ON s.id=b.slot_id"
                " LEFT JOIN classes cl ON cl.id=s.class_id WHERE b.bag_no=?",
                (bag_no,)).fetchone()
            if not bag:
                raise ServiceError("睡袋不存在", "not_found")
            events = [dict(r) for r in tx.execute(
                "SELECT e.*, c.code AS child_code FROM events e"
                " LEFT JOIN children c ON c.id=e.child_id"
                " WHERE e.bag_id=? ORDER BY e.id", (bag["id"],))]
            photos = [dict(r) for r in tx.execute(
                "SELECT * FROM label_photos WHERE bag_id=? ORDER BY id",
                (bag["id"],))]
            return {"bag": dict(bag), "events": events, "photos": photos}

    def pending_returns(self):
        with self._tx() as tx:
            return [dict(r) for r in tx.execute(
                "SELECT b.bag_no, b.id AS bag_id, c.code AS child_code, cl.name AS class_name"
                " FROM sleeping_bags b JOIN children c ON c.current_bag_id=b.id"
                " JOIN classes cl ON cl.id=c.class_id"
                " WHERE b.status='return_pending' ORDER BY b.id")]

    # ---------- 内部工具 ----------
    def _child(self, tx, code: str) -> sqlite3.Row:
        row = tx.execute("SELECT * FROM children WHERE code=? AND active=1",
                         (code,)).fetchone()
        if not row:
            raise ServiceError(f"儿童 {code} 不存在", "not_found")
        return row

    def _bag_with_child(self, tx, bag_no: str):
        bag = tx.execute("SELECT * FROM sleeping_bags WHERE bag_no=?",
                         (bag_no,)).fetchone()
        if not bag:
            raise ServiceError(f"睡袋 {bag_no} 不存在", "not_found")
        ch = tx.execute(
            "SELECT c.* FROM children c WHERE c.current_bag_id=?",
            (bag["id"],)).fetchone()
        return bag, ch

    def _owner_of(self, tx, bag_id: int):
        return tx.execute(
            "SELECT * FROM children WHERE current_bag_id=? AND active=1",
            (bag_id,)).fetchone()

    def _free_spare_slot(self, tx, class_id: int, bag_id: int) -> sqlite3.Row:
        row = tx.execute(
            "SELECT s.* FROM slots s WHERE s.class_id=? AND s.assigned_child_id IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM sleeping_bags b"
            " WHERE b.slot_id=s.id AND b.id != ?)"
            " ORDER BY s.`row`, s.col LIMIT 1", (class_id, bag_id)).fetchone()
        if not row:
            raise ServiceError("班内没有空闲格位存放备用袋", "no_free_slot")
        return row

    def _consume_proxy(self, tx, child_id: int, code: str, ts: str) -> sqlite3.Row:
        row = tx.execute("SELECT * FROM authorizations WHERE code=?",
                         (code.strip().upper(),)).fetchone()
        if not row:
            raise ServiceError("代领授权码无效", "bad_proxy")
        if row["child_id"] != child_id:
            raise ServiceError("授权码与孩子不匹配", "bad_proxy")
        if row["revoked_at"]:
            raise ServiceError("授权码已被家长撤销", "proxy_revoked")
        if not (row["valid_from"] <= ts <= row["valid_until"]):
            raise ServiceError("授权码不在有效期内", "proxy_expired")
        tx.execute("UPDATE authorizations SET used_count=used_count+1 WHERE id=?",
                   (row["id"],))
        return row

    def _event(self, tx, bag_id, child_id, etype, note, actor, ts):
        tx.execute(
            "INSERT INTO events(bag_id, child_id, type, note, actor, ts)"
            " VALUES (?,?,?,?,?,?)",
            (bag_id, child_id, etype, note, actor, ts))

    def _send(self, tx, child_id, kind, body, ts) -> dict:
        """模拟消息适配器：返回送达回执，并落库消息记录。"""
        receipt = self.messages.send(child_id=child_id, kind=kind, body=body)
        tx.execute(
            "INSERT INTO messages(child_id, kind, body, adapter, delivered, ts)"
            " VALUES (?,?,?,?,?,?)",
            (child_id, kind, body, receipt.get("adapter", "mock"),
             1 if receipt.get("ok") else 0, ts))
        return receipt

    # ---------- 提醒 ----------
    def run_due_reminders(self, *, overdue_hours: float = 20,
                          cooldown_hours: float = 12) -> list:
        """进程内任务调用：对超时未送回的睡袋发提醒（冷却期内不重复）。"""
        now = self.now()
        cutoff = (now - timedelta(hours=overdue_hours)).isoformat(timespec="seconds")
        cool = (now - timedelta(hours=cooldown_hours)).isoformat(timespec="seconds")
        sent = []
        with self._tx() as tx:
            rows = tx.execute(
                "SELECT * FROM ("
                " SELECT b.id AS bag_id, b.bag_no, c.id AS child_id, c.code AS child_code,"
                " (SELECT MAX(ts) FROM events e WHERE e.bag_id=b.id AND e.type='pickup')"
                " AS last_pickup FROM sleeping_bags b"
                " JOIN children c ON c.current_bag_id=b.id"
                " WHERE b.status='taken_home')"
                " WHERE last_pickup IS NOT NULL AND last_pickup < ?"
                " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.child_id=child_id"
                " AND m.kind='monday_reminder' AND m.ts > ?)",
                (cutoff, cool)).fetchall()
            for r in rows:
                msg = self._send(
                    tx, r["child_id"], "monday_reminder",
                    f"【午睡袋】提醒：{r['child_code']} 的睡袋 {r['bag_no']} "
                    "尚未送回，请记得今天带来。",
                    now.isoformat(timespec="seconds"))
                sent.append({"bag": r["bag_no"], "child": r["child_code"],
                             "receipt": msg})
        return sent
