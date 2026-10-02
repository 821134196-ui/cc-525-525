"""业务规则测试 —— 标准库 unittest，零依赖。

覆盖题目点名的四项：编号唯一、授权撤销、转班交接、重复领取；
另含：未见实物不能送回、错拿拦截、家长越权撤销、格位/睡袋互斥、
备用袋库存流转、进程内提醒触发、家长端仅见本人。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import services as svc
from app.db import connect, init_db
from app.messages import list_messages
from app.photos import tiny_png, save_label_photo
from app.reminders import ReminderScheduler, schedule_reminder


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        self.conn = connect(self.db)
        init_db(self.conn)
        self.c1 = svc.create_class(self.conn, "小一班")
        self.c2 = svc.create_class(self.conn, "小二班")
        for i in range(1, 5):
            svc.create_slot(self.conn, self.c1, f"A-0{i}")
            svc.create_slot(self.conn, self.c2, f"B-0{i}")
        svc.create_child(self.conn, code="C1", name="甲", class_id=self.c1)
        svc.create_child(self.conn, code="C2", name="乙", class_id=self.c1)
        svc.create_child(self.conn, code="C3", name="丙", class_id=self.c2)
        svc.create_parent(self.conn, name="甲妈", phone="1301",
                          pickup_code="P-1", child_id=self._cid("C1"))
        svc.create_parent(self.conn, name="乙妈", phone="1302",
                          pickup_code="P-2", child_id=self._cid("C2"))
        svc.create_parent(self.conn, name="丙妈", phone="1303",
                          pickup_code="P-3", child_id=self._cid("C3"))
        svc.register_bag(self.conn, bag_no="N-1")
        svc.register_bag(self.conn, bag_no="N-2")
        svc.register_bag(self.conn, bag_no="N-3")
        svc.register_bag(self.conn, bag_no="S-9", material="棉", spare=True)
        svc.assign_bag(self.conn, bag_no="N-1", child_code="C1", slot_no="A-01")
        svc.assign_bag(self.conn, bag_no="N-2", child_code="C2", slot_no="A-02")
        svc.assign_bag(self.conn, bag_no="N-3", child_code="C3", slot_no="B-01")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _cid(self, code):
        return self.conn.execute(
            "SELECT id FROM children WHERE code=?", (code,)).fetchone()["id"]

    # ---------------------------------------------------------- 1. 编号唯一
    def test_bag_no_unique(self):
        with self.assertRaises(svc.DomainError):
            svc.register_bag(self.conn, bag_no="N-1")  # 重复编号
        # 同编号即便标记为备用袋也不行
        with self.assertRaises(svc.DomainError):
            svc.register_bag(self.conn, bag_no="N-1", spare=True)
        # 不同编号可登记
        new_id = svc.register_bag(self.conn, bag_no="N-100")
        self.assertGreater(new_id, 0)

    def test_one_bag_cannot_assign_two_children(self):
        # N-1 已分给 C1，再分给 C2 必须失败
        with self.assertRaises(svc.DomainError):
            svc.assign_bag(self.conn, bag_no="N-1", child_code="C2",
                           slot_no="A-03")
        # 直接走 DB 唯一索引也应被挡
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO assignments(bag_id, child_id, status)"
                " SELECT id, 2, 'at_school' FROM sleeping_bags WHERE bag_no='N-1'")

    def test_slot_exclusive(self):
        svc.register_bag(self.conn, bag_no="N-50")
        # A-01 已被 C1 占用
        with self.assertRaises(svc.DomainError):
            svc.assign_bag(self.conn, bag_no="N-50", child_code="C3",
                           slot_no="A-01")

    # ---------------------------------------------------------- 2. 授权撤销
    def test_proxy_grant_pickup_revoke(self):
        r = svc.grant_proxy(self.conn, parent_phone="1301",
                            proxy_name="王阿姨", code="D-W1",
                            valid_from=datetime.now() - timedelta(hours=1),
                            valid_until=datetime.now() + timedelta(days=1))
        self.assertEqual(r["code"], "D-W1")
        # 代领成功
        res = svc.pickup(self.conn, code="D-W1")
        self.assertEqual(res["status"], "taken_home")
        self.assertEqual(res["bag_no"], "N-1")

    def test_revoked_code_fails_immediately(self):
        svc.grant_proxy(self.conn, parent_phone="1301", proxy_name="旧代领",
                        code="D-OLD",
                        valid_from=datetime.now() - timedelta(hours=1),
                        valid_until=datetime.now() + timedelta(days=1))
        svc.revoke_proxy(self.conn, code="D-OLD", parent_phone="1301")
        with self.assertRaises(svc.AuthCodeError) as cm:
            svc.pickup(self.conn, code="D-OLD")
        self.assertIn("撤销", str(cm.exception))
        # 重复撤销报错
        with self.assertRaises(svc.DomainError):
            svc.revoke_proxy(self.conn, code="D-OLD", parent_phone="1301")

    def test_expired_and_used_code_fails(self):
        svc.grant_proxy(self.conn, parent_phone="1302", proxy_name="过期人",
                        code="D-EXP",
                        valid_from=datetime.now() - timedelta(days=5),
                        valid_until=datetime.now() - timedelta(days=1))
        with self.assertRaises(svc.AuthCodeError):
            svc.pickup(self.conn, code="D-EXP")
        # 未生效
        svc.grant_proxy(self.conn, parent_phone="1302", proxy_name="未来人",
                        code="D-FUT",
                        valid_from=datetime.now() + timedelta(days=1),
                        valid_until=datetime.now() + timedelta(days=2))
        with self.assertRaises(svc.AuthCodeError):
            svc.pickup(self.conn, code="D-FUT")

    def test_parent_cannot_revoke_others_auth(self):
        svc.grant_proxy(self.conn, parent_phone="1301", proxy_name="阿姨",
                        code="D-MINE",
                        valid_until=datetime.now() + timedelta(days=1))
        # C2 的家长不能撤销 C1 的授权
        with self.assertRaises(svc.DomainError):
            svc.revoke_proxy(self.conn, code="D-MINE", parent_phone="1302")
        # 本人可以撤销
        svc.revoke_proxy(self.conn, code="D-MINE", parent_phone="1301")

    # ---------------------------------------------------------- 3. 转班交接
    def test_transfer_with_bag_at_school(self):
        # 实物在园：当场落位新班格位
        r = svc.transfer_child(self.conn, child_code="C3",
                               to_class_id=self.c1, new_slot_no="A-03")
        self.assertEqual(r["status"], "completed")
        board = {x["child_code"]: x for x in svc.class_board(self.conn, self.c1)}
        self.assertEqual(board["C3"]["slot_no"], "A-03")
        row = board["C3"]
        self.assertIsNone(row["pending_class_id"])
        # 原班不再有该孩子
        board2 = svc.class_board(self.conn, self.c2)
        self.assertNotIn("C3", {x["child_code"] for x in board2})

    def test_transfer_while_bag_away_then_return_lands(self):
        # 睡袋先被家长领回家，再转班 → 挂起
        svc.pickup(self.conn, code="P-3")
        r = svc.transfer_child(self.conn, child_code="C3",
                               to_class_id=self.c1, new_slot_no="A-03")
        self.assertEqual(r["status"], "bag_away_pending")
        # 转班后孩子已在新班，但尚未送回；家长端/新班看板可见挂起
        board = {x["child_code"]: x for x in svc.class_board(self.conn, self.c1)}
        self.assertIn("C3", board)
        self.assertEqual(board["C3"]["status"], "taken_home")
        self.assertEqual(board["C3"]["pending_slot_id"],
                         self.conn.execute(
                             "SELECT id FROM cubby_slots WHERE class_id=? AND slot_no='A-03'",
                             (self.c1,)).fetchone()["id"])
        # 未送回时不能落在别人占用的格位：先占住 A-03 的另一条路径
        # （此时挂起不占格位，其他孩子可暂用；送回落位若冲突应报错）
        # 周一见实物送回 → 自动落位 A-03
        res = svc.verify_return(
            self.conn, bag_no="N-3", child_code="C3",
            clean_status="clean", label_intact=True, seen=True)
        self.assertEqual(res["status"], "at_school")
        board = {x["child_code"]: x for x in svc.class_board(self.conn, self.c1)}
        self.assertEqual(board["C3"]["slot_no"], "A-03")
        self.assertIsNone(board["C3"]["pending_slot_id"])
        chain = svc.bag_chain(self.conn, "N-3")
        kinds = [e["event_type"] for e in chain["events"]]
        self.assertIn("transfer_handover", kinds)
        self.assertIn("returned", kinds)

    # ---------------------------------------------------------- 4. 重复领取
    def test_duplicate_pickup_blocked(self):
        svc.pickup(self.conn, code="P-1")
        with self.assertRaises(svc.DomainError) as cm:
            svc.pickup(self.conn, code="P-1")
        self.assertIn("重复领取", str(cm.exception))

    def test_proxy_code_single_use(self):
        svc.grant_proxy(self.conn, parent_phone="1302", proxy_name="阿姨",
                        code="D-ONCE",
                        valid_from=datetime.now() - timedelta(minutes=5),
                        valid_until=datetime.now() + timedelta(days=1))
        svc.pickup(self.conn, code="D-ONCE")
        with self.assertRaises(svc.AuthCodeError) as cm:
            svc.pickup(self.conn, code="D-ONCE")
        self.assertIn("已使用", str(cm.exception))

    # ---------------------------------------------------------- 送回核验
    def test_return_requires_seen(self):
        svc.pickup(self.conn, code="P-1")
        # 未见实物 → 禁止标记送回
        with self.assertRaises(svc.DomainError) as cm:
            svc.verify_return(self.conn, bag_no="N-1", child_code="C1",
                              clean_status="clean", label_intact=True, seen=False)
        self.assertIn("未见实物", str(cm.exception))

    def test_return_wrong_bag_intercepted(self):
        # C1 领走 N-1，周一手上却是 N-2（拿错）
        svc.pickup(self.conn, code="P-1")
        svc.pickup(self.conn, code="P-2")
        with self.assertRaises(svc.WrongBagError) as cm:
            svc.verify_return(self.conn, bag_no="N-2", child_code="C1",
                              clean_status="clean", label_intact=True, seen=True)
        self.assertEqual(cm.exception.scanned_bag_no, "N-2")
        self.assertEqual(cm.exception.owner_code, "C2")
        # 被拦后仍是已领回状态，没有误判送回
        a = self.conn.execute(
            "SELECT status FROM assignments a JOIN children ch ON ch.id=a.child_id"
            " WHERE ch.code='C1' AND a.ended_at IS NULL").fetchone()
        self.assertEqual(a["status"], "taken_home")
        # 换回本人编号、见实物后才能送回
        svc.verify_return(self.conn, bag_no="N-1", child_code="C1",
                          clean_status="clean", label_intact=True, seen=True)

    def test_return_label_damage_and_dirty_notify(self):
        svc.pickup(self.conn, code="P-1")
        res = svc.verify_return(
            self.conn, bag_no="N-1", child_code="C1",
            clean_status="needs_wash", label_intact=False, seen=True)
        self.assertTrue(any("标签脱落" in w for w in res["warnings"]))
        self.assertTrue(any("清洁" in w for w in res["warnings"]))
        a = self.conn.execute(
            "SELECT label_damaged FROM assignments a JOIN children ch ON ch.id=a.child_id"
            " WHERE ch.code='C1' AND a.ended_at IS NULL").fetchone()
        self.assertEqual(a["label_damaged"], 1)
        msgs = list_messages(self.conn, phone="1301")
        self.assertTrue(any("清洁" in m["title"] or "清洁" in m["body"]
                            for m in msgs))

    def test_swap_after_pickup_records_and_notifies(self):
        svc.pickup(self.conn, code="P-1")
        svc.pickup(self.conn, code="P-2")
        svc.record_swap_after_pickup(self.conn, code_a="C1", code_b="C2")
        for bag, owner in (("N-1", "C1"), ("N-2", "C2")):
            chain = svc.bag_chain(self.conn, bag)
            self.assertTrue(any(e["event_type"] == "wrong_swap"
                                for e in chain["events"]))
        self.assertTrue(any(m["kind"] == "wrong_swap"
                            for m in list_messages(self.conn, "1301")))
        self.assertTrue(any(m["kind"] == "wrong_swap"
                            for m in list_messages(self.conn, "1302")))
        # 错拿登记不改归属：物品链里 N-1 的活动孩子仍是 C1
        owner = self.conn.execute(
            "SELECT ch.code FROM assignments a JOIN sleeping_bags b ON b.id=a.bag_id"
            " JOIN children ch ON ch.id=a.child_id"
            " WHERE b.bag_no='N-1' AND a.ended_at IS NULL").fetchone()["code"]
        self.assertEqual(owner, "C1")

    # ---------------------------------------------------------- 污损/备用袋
    def test_damage_replace_and_spare_stock(self):
        stock = {r["bag_no"]: r["in_stock"]
                 for r in svc.spare_stock(self.conn)}
        self.assertEqual(stock["S-9"], 1)
        svc.replace_with_spare(self.conn, child_code="C1",
                               damaged_bag_no="N-1", spare_bag_no="S-9",
                               reason="破损")
        bag = self.conn.execute(
            "SELECT retired, retire_reason FROM sleeping_bags WHERE bag_no='N-1'").fetchone()
        self.assertEqual(bag["retired"], 1)
        stock = {r["bag_no"]: r for r in svc.spare_stock(self.conn)}
        self.assertEqual(stock["S-9"]["in_stock"], 0)
        self.assertEqual(stock["S-9"]["used_by_code"], "C1")
        # 已报废袋不能再分配
        with self.assertRaises(svc.DomainError):
            svc.assign_bag(self.conn, bag_no="N-1", child_code="C2")
        # 备用袋归还并换新袋
        svc.register_bag(self.conn, bag_no="N-77", material="棉")
        svc.return_spare(self.conn, child_code="C1", new_bag_no="N-77")
        stock = {r["bag_no"]: r for r in svc.spare_stock(self.conn)}
        self.assertEqual(stock["S-9"]["in_stock"], 1)
        cur = self.conn.execute(
            "SELECT b.bag_no FROM assignments a JOIN sleeping_bags b ON b.id=a.bag_id"
            " JOIN children ch ON ch.id=a.child_id"
            " WHERE ch.code='C1' AND a.ended_at IS NULL").fetchone()["bag_no"]
        self.assertEqual(cur, "N-77")

    # ---------------------------------------------------------- 家长端隔离
    def test_parent_dashboard_scoped(self):
        d1 = svc.parent_dashboard(self.conn, "1301")
        self.assertEqual(d1["view"]["child_code"], "C1")
        self.assertEqual(d1["view"]["bag_no"], "N-1")
        d2 = svc.parent_dashboard(self.conn, "1302")
        self.assertNotIn("N-1", [d2["view"]["bag_no"]])
        self.assertEqual(d2["view"]["bag_no"], "N-2")
        with self.assertRaises(svc.NotFound):
            svc.parent_dashboard(self.conn, "0000")

    def test_not_returned_only_when_away(self):
        with self.assertRaises(svc.DomainError):
            svc.mark_not_returned(self.conn, child_code="C1")
        svc.pickup(self.conn, code="P-1")
        svc.mark_not_returned(self.conn, child_code="C1")
        self.assertTrue(any(m["kind"] == "not_returned"
                            for m in list_messages(self.conn, "1301")))

    # ---------------------------------------------------------- 照片 & 提醒
    def test_label_photo_local(self):
        r = save_label_photo(self.conn, bag_no="N-1", data=tiny_png(),
                             content_type="image/png")
        chain = svc.bag_chain(self.conn, "N-1")
        self.assertEqual(len(chain["photos"]), 1)
        self.assertTrue(Path(
            __import__("app.db", fromlist=["PHOTO_DIR"]).PHOTO_DIR
            / r["path"]).exists())

    def test_reminder_scheduler_fires_due(self):
        schedule_reminder(
            self.conn, fire_at=datetime.now() - timedelta(seconds=2),
            kind="pickup_reminder",
            payload={"phone": "1301", "title": "该领回了", "body": "x"})
        scheduler = ReminderScheduler(lambda: connect(self.db), interval=1)
        n = scheduler.tick()
        self.assertEqual(n, 1)
        msgs = list_messages(self.conn, "1301")
        self.assertTrue(any(m["title"] == "该领回了" for m in msgs))
        # 幂等：再 tick 不重复发
        self.assertEqual(scheduler.tick(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
