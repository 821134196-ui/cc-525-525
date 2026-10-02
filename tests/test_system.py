"""系统测试：编号唯一、授权撤销、转班交接、重复领取等核心规则。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import http.client
import os
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta

from napbags.messages import MockMessageAdapter
from napbags.photos import render_label_png
from napbags.reminders import ReminderScheduler
from napbags.service import NapService, ServiceError
from napbags.web import serve


class Clock:
    """可拨动的时钟，供授权有效期/提醒冷却测试。"""

    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)
        return self.t


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "nap.db")
        self.clock = Clock(datetime(2026, 10, 2, 8, 0))  # 周五
        self.adapter = MockMessageAdapter()
        self.svc = NapService(self.db, clock=self.clock,
                              message_adapter=self.adapter)
        self.c1 = self.svc.create_class("小一班", 3, 5)
        self.c2 = self.svc.create_class("小二班", 3, 5)

    def reg(self, code="SUN", cls=None, slot="R1C01", bag="B-0001", **kw):
        cls = cls or self.c1
        self.svc.register_child(code, cls, slot, bag, kw.get("material", "纯棉"),
                                kw.get("allergy", "无"))
        return self.svc.add_guardian(code, code + "家长")


class TestBagNumberUnique(ServiceTestBase):
    def test_same_bag_number_rejected(self):
        self.reg("SUN", bag="B-0001", slot="R1C01")
        with self.assertRaises(ServiceError) as cm:
            self.reg("LILY", bag="B-0001", slot="R1C02")
        self.assertEqual(cm.exception.code, "duplicate_bag")

    def test_spare_number_also_unique(self):
        self.reg("SUN", bag="B-0001")
        with self.assertRaises(ServiceError):
            self.svc.register_spare("B-0001", "棉", self.c2, "R3C01")

    def test_replacement_bag_cannot_reuse_number(self):
        self.reg("TOM", bag="B-0003", slot="R1C03")
        self.svc.register_spare("SP-1", "棉", self.c1, "R3C01")
        self.svc.damage_replace("B-0003", "脏污")
        self.svc.assign_spare("TOM", "SP-1")
        with self.assertRaises(ServiceError):
            self.svc.register_replacement("TOM", "B-0003", "纯棉")


class TestAuthorization(ServiceTestBase):
    def test_valid_proxy_code_allows_pickup(self):
        tok = self.reg("KAI", bag="B-2001", cls=self.c2, slot="R1C01")
        auth = self.svc.create_authorization(tok, "外婆", valid_days=7)
        r = self.svc.pickup("B-2001", proxy_code=auth["code"])
        self.assertEqual(r["proxy"], "外婆")
        inc = [i for i in self.svc.list_incidents() if i["kind"] == "proxy_pickup"]
        self.assertEqual(len(inc), 1)

    def test_revoked_code_fails_immediately(self):
        tok = self.reg("KAI", bag="B-2001", cls=self.c2, slot="R1C01")
        auth = self.svc.create_authorization(tok, "外婆")
        self.svc.revoke_authorization(auth["code"])
        # 撤销当下、同一时刻即失效
        with self.assertRaises(ServiceError) as cm:
            self.svc.pickup("B-2001", proxy_code=auth["code"])
        self.assertEqual(cm.exception.code, "proxy_revoked")
        # 旧码仍可领取的错误情况不应出现：家长本人扫码仍可正常领
        self.svc.pickup("B-2001")

    def test_expired_code_rejected(self):
        tok = self.reg("KAI", bag="B-2001", cls=self.c2, slot="R1C01")
        auth = self.svc.create_authorization(tok, "邻居", valid_days=7)
        self.clock.advance(days=8)
        with self.assertRaises(ServiceError) as cm:
            self.svc.pickup("B-2001", proxy_code=auth["code"])
        self.assertEqual(cm.exception.code, "proxy_expired")

    def test_code_bound_to_child(self):
        t1 = self.reg("SUN", bag="B-0001", slot="R1C01")
        self.reg("LILY", bag="B-0002", slot="R1C02")
        auth = self.svc.create_authorization(t1, "亲戚")
        with self.assertRaises(ServiceError) as cm:
            self.svc.pickup("B-0002", proxy_code=auth["code"])
        self.assertEqual(cm.exception.code, "bad_proxy")

    def test_double_revoke_idempotent_error(self):
        tok = self.reg("KAI", bag="B-2001", cls=self.c2, slot="R1C01")
        auth = self.svc.create_authorization(tok, "外婆")
        self.svc.revoke_authorization(auth["code"])
        with self.assertRaises(ServiceError) as cm:
            self.svc.revoke_authorization(auth["code"])
        self.assertEqual(cm.exception.code, "already_revoked")


class TestTransfer(ServiceTestBase):
    def test_transfer_moves_slot_bag_and_chain(self):
        self.reg("SUN", bag="B-0001", slot="R1C01")
        r = self.svc.transfer_child("SUN", self.c2, "R2C03")
        self.assertEqual(r["new_slot"], "R2C03")
        cls, slots = self.svc.class_board(self.c1)
        self.assertIsNone(next(s for s in slots if s["code"] == "R1C01")["assigned_child_id"])
        target = next(s for s in slots if False) if False else None
        cls2, slots2 = self.svc.class_board(self.c2)
        s = next(x for x in slots2 if x["code"] == "R2C03")
        self.assertEqual(s["child"]["code"], "SUN")
        self.assertEqual(s["child"]["bag_no"], "B-0001")
        chain = self.svc.bag_chain("B-0001")
        self.assertEqual(chain["events"][-1]["type"], "transfer")
        self.assertEqual(chain["bag"]["class_name"], "小二班")
        inc = [i for i in self.svc.list_incidents() if i["kind"] == "transfer"]
        self.assertEqual(len(inc), 1)
        self.assertIsNotNone(inc[0]["resolved_at"])

    def test_transfer_blocked_when_bag_not_at_school(self):
        self.reg("SUN", bag="B-0001")
        self.svc.pickup("B-0001")
        with self.assertRaises(ServiceError) as cm:
            self.svc.transfer_child("SUN", self.c2, "R2C03")
        self.assertEqual(cm.exception.code, "not_stored")

    def test_transfer_blocked_with_bad_label(self):
        self.reg("SUN", bag="B-0001")
        self.svc.mark_label_off("B-0001")
        with self.assertRaises(ServiceError) as cm:
            self.svc.transfer_child("SUN", self.c2, "R2C03")
        self.assertEqual(cm.exception.code, "label_bad")

    def test_transfer_blocked_when_on_spare(self):
        self.reg("SUN", bag="B-0001")
        self.svc.register_spare("SP-9", "棉", self.c1, "R3C02")
        self.svc.damage_replace("B-0001", "污损")
        self.svc.assign_spare("SUN", "SP-9")
        with self.assertRaises(ServiceError) as cm:
            self.svc.transfer_child("SUN", self.c2, "R2C03")
        self.assertEqual(cm.exception.code, "spare_in_use")

    def test_transfer_target_slot_taken(self):
        self.reg("SUN", bag="B-0001")
        self.reg("KAI", cls=self.c2, slot="R2C03", bag="B-2003")
        with self.assertRaises(ServiceError) as cm:
            self.svc.transfer_child("SUN", self.c2, "R2C03")
        self.assertEqual(cm.exception.code, "slot_taken")


class TestPickupReturn(ServiceTestBase):
    def test_duplicate_pickup_rejected(self):
        self.reg("SUN", bag="B-0001")
        self.svc.pickup("B-0001")
        with self.assertRaises(ServiceError) as cm:
            self.svc.pickup("B-0001")
        self.assertEqual(cm.exception.code, "duplicate_pickup")

    def test_return_requires_physical_seen(self):
        tok = self.reg("SUN", bag="B-0001")
        self.svc.pickup("B-0001")
        self.svc.request_return(tok)
        with self.assertRaises(ServiceError) as cm:
            self.svc.confirm_return("B-0001", physically_seen=False)
        self.assertEqual(cm.exception.code, "not_seen")

    def test_full_pickup_return_cycle(self):
        tok = self.reg("SUN", bag="B-0001")
        self.svc.pickup("B-0001")
        self.svc.request_return(tok, note="已清洗暴晒")
        r = self.svc.confirm_return("B-0001", physically_seen=True,
                                    label_ok=True, clean=True,
                                    spare_used=True, spare_note="用过 SP-01")
        self.assertEqual(r["status"], "stored")
        types = [e["type"] for e in self.svc.bag_chain("B-0001")["events"]]
        self.assertEqual(types, ["registered", "pickup", "return_request",
                                 "return_seen"])
        # 归位后可以再次领回（下一周）
        self.svc.pickup("B-0001")

    def test_confirm_without_request_also_allowed_when_seen(self):
        # 家长直接把袋子交到老师手里的情形：taken_home + 见实物
        self.reg("SUN", bag="B-0001")
        self.svc.pickup("B-0001")
        r = self.svc.confirm_return("B-0001", physically_seen=True)
        self.assertEqual(r["status"], "stored")

    def test_monday_missing_recorded_and_messaged(self):
        self.reg("SUN", bag="B-0001")
        self.svc.pickup("B-0001")
        self.clock.advance(days=3)
        self.svc.monday_missing("B-0001")
        inc = self.svc.list_incidents()
        self.assertTrue(any(i["kind"] == "monday_missing" for i in inc))
        self.assertTrue(any(m["kind"] == "monday_reminder"
                            for m in self.adapter.sent))


class TestMispick(ServiceTestBase):
    def test_mispick_lifecycle(self):
        self.reg("LILY", slot="R1C02", bag="B-0002")
        self.reg("TOM", slot="R1C03", bag="B-0003")
        self.svc.pickup("B-0002")
        inc_id = self.svc.record_mispick("B-1002".replace("1002", "0002"), "TOM")
        open_inc = [i for i in self.svc.list_incidents(open_only=True)
                    if i["kind"] == "mispick"]
        self.assertEqual(len(open_inc), 1)
        self.svc.resolve_mispick(inc_id, exchanged=True)
        self.assertFalse(any(i["kind"] == "mispick" and not i["resolved_at"]
                             for i in self.svc.list_incidents()))
        chain = [e["type"] for e in self.svc.bag_chain("B-0002")["events"]]
        self.assertIn("mispick", chain)
        self.assertIn("recheck", chain)

    def test_self_pickup_is_not_mispick(self):
        self.reg("LILY", slot="R1C02", bag="B-0002")
        self.svc.pickup("B-0002")
        with self.assertRaises(ServiceError) as cm:
            self.svc.record_mispick("B-0002", "LILY")
        self.assertEqual(cm.exception.code, "not_mispick")


class TestSpareInventory(ServiceTestBase):
    def test_spare_flow_and_single_owner(self):
        self.reg("TOM", slot="R1C03", bag="B-0003")
        self.svc.register_spare("SP-01", "纯棉", self.c1, "R3C01")
        self.svc.damage_replace("B-0003", "汤汁浸透")
        self.svc.assign_spare("TOM", "SP-01")
        # 同一备用袋不能同时分配给两个孩子
        self.reg("MEI", slot="R1C04", bag="B-0004")
        self.svc.damage_replace("B-0004", "破损")
        with self.assertRaises(ServiceError) as cm:
            self.svc.assign_spare("MEI", "SP-01")
        self.assertEqual(cm.exception.code, "spare_busy")
        # 新袋到位后备用袋收回，重新进入库存
        self.svc.register_replacement("TOM", "B-0030", "纯棉")
        self.assertIn("SP-01", [b["bag_no"] for b in self.svc.spare_stock()])
        self.svc.assign_spare("MEI", "SP-01")  # 现在可以给第二个孩子

    def test_spare_requires_child_without_good_bag(self):
        self.reg("TOM", slot="R1C03", bag="B-0003")
        self.svc.register_spare("SP-01", "纯棉", self.c1, "R3C01")
        with self.assertRaises(ServiceError) as cm:
            self.svc.assign_spare("TOM", "SP-01")
        self.assertEqual(cm.exception.code, "bad_state")


class TestGuardianIsolation(ServiceTestBase):
    def test_parent_sees_only_own_bag(self):
        t1 = self.reg("SUN", slot="R1C01", bag="B-0001")
        self.reg("LILY", slot="R1C02", bag="B-0002")
        view = self.svc.guardian_view(t1)
        self.assertEqual(view["bag"]["bag_no"], "B-0001")
        with self.assertRaises(ServiceError):
            self.svc.guardian_view("not-a-real-token")


class TestReminderScheduler(ServiceTestBase):
    def test_overdue_bag_gets_reminder_with_cooldown(self):
        self.reg("SUN", slot="R1C01", bag="B-0001")
        self.svc.pickup("B-0001")
        self.clock.advance(hours=21)
        sched = ReminderScheduler(self.svc.run_due_reminders, interval_seconds=1)
        sent = sched.tick()
        self.assertEqual(len(sent), 1)
        # 冷却期内不重复发
        self.clock.advance(hours=1)
        self.assertEqual(sched.tick(), [])
        # 冷却过后再提醒
        self.clock.advance(hours=12)
        self.assertEqual(len(sched.tick()), 1)

    def test_stored_bag_no_reminder(self):
        self.reg("SUN", slot="R1C01", bag="B-0001")
        self.clock.advance(days=2)
        self.assertEqual(self.svc.run_due_reminders(), [])


class TestLabelPhoto(unittest.TestCase):
    def test_png_signature_and_dimensions(self):
        import struct
        data = render_label_png("B-7777")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (640, 400))
        self.assertGreater(len(data), 1000)


class TestWebFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.db = os.path.join(cls.dir, "w.db")
        cls.svc = NapService(cls.db)
        cls.c1 = cls.svc.create_class("小一班", 2, 3)
        cls.c2 = cls.svc.create_class("小二班", 2, 3)
        cls.svc.register_child("SUN", cls.c1, "R1C01", "W-0001", "纯棉", "尘螨")
        cls.token = cls.svc.add_guardian("SUN", "孙女士")
        cls.svc.register_child("LILY", cls.c1, "R1C02", "W-0002", "纱布")
        cls.httpd = serve(cls.svc, os.path.join(cls.dir, "photos"),
                          host="127.0.0.1", port=0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = None
        headers = {}
        if form is not None:
            body = urllib.parse.urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        loc = resp.getheader("Location", "")
        conn.close()
        return resp.status, loc, data

    def test_pages_render(self):
        for path in ("/classes", f"/class?id={self.c1}", "/spares",
                     "/incidents", f"/bag?no=W-0001",
                     f"/guardian?token={self.token}"):
            status, _, data = self._req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("午睡袋", data)

    def test_guardian_requires_token(self):
        status, _, data = self._req("GET", "/guardian?token=wrong")
        self.assertEqual(status, 400)

    def test_label_photo_png(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/label_photo?bag_no=W-0001")
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_pickup_then_unseen_return_rejected_via_http(self):
        status, loc, _ = self._req("POST", "/action/pickup",
                                   {"bag_no": "W-0001", "back": f"/class?id={self.c1}"})
        self.assertEqual(status, 303)
        self.assertIn("msg=done", loc)
        # 重复领取
        status, loc, _ = self._req("POST", "/action/pickup", {"bag_no": "W-0001"})
        self.assertIn("err=", loc)
        self.assertIn("重复领取", urllib.parse.unquote(loc))
        # 未勾选“已见实物”
        status, loc, _ = self._req("POST", "/action/confirm_return",
                                   {"bag_no": "W-0001", "back": "/classes"})
        self.assertIn("err=", loc)
        self.assertIn("未见实物", urllib.parse.unquote(loc))
        # 家长送交 → 老师见实 → 归位
        self._req("POST", "/action/request_return", {"token": self.token})
        status, loc, _ = self._req("POST", "/action/confirm_return",
                                   {"bag_no": "W-0001", "physically_seen": "1",
                                    "label_ok": "1", "clean": "1"})
        self.assertIn("msg=done", loc)

    def test_icons_and_text_differ_for_pickup_and_return(self):
        # 先领回一个袋，使格位板上同时出现“领回清洗”和“见实归位”两种控件
        self._req("POST", "/action/pickup",
                  {"bag_no": "W-0002", "back": f"/class?id={self.c1}"})
        _, _, page = self._req("GET", f"/class?id={self.c1}")
        self.assertIn("领回清洗", page)
        self.assertIn("见实归位", page)
        # 两种 SVG 路径不同
        self.assertIn("M11 3h2v9.2", page)   # 下载/领回
        self.assertIn("M12 2l8 3v6", page)   # 盾牌/核验


if __name__ == "__main__":
    unittest.main()
