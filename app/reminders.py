"""进程内提醒任务：后台守护线程轮询 reminders 表，到期即通过模拟适配器发家长消息。

不依赖外部队列/cron。连接由 conn_factory 在本线程内新建（SQLite 连接不跨线程共享）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from .messages import send_message

FMT = "%Y-%m-%d %H:%M:%S"


def schedule_reminder(conn: sqlite3.Connection, *, fire_at: datetime,
                      kind: str, ref_type: str = "",
                      ref_id: int | None = None,
                      payload: dict | None = None,
                      commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO reminders(fire_at, kind, ref_type, ref_id, payload)"
        " VALUES (?,?,?,?,?)",
        (fire_at.strftime(FMT), kind, ref_type, ref_id,
         json.dumps(payload or {}, ensure_ascii=False)))
    if commit:
        conn.commit()
    return cur.lastrowid


def cancel_reminder(conn: sqlite3.Connection, reminder_id: int) -> None:
    conn.execute(
        "UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'",
        (reminder_id,))
    conn.commit()


def schedule_weekend_pickups(conn: sqlite3.Connection,
                             fire_at: datetime | None = None) -> int:
    """周末领回提醒：给所有在园孩子的家长排一条。"""
    fire_at = fire_at or (datetime.now() + timedelta(days=1))
    rows = conn.execute(
        """
        SELECT DISTINCT p.phone, ch.code, b.bag_no
          FROM assignments a
          JOIN children ch ON ch.id = a.child_id
          JOIN parents p ON p.child_id = ch.id
          JOIN sleeping_bags b ON b.id = a.bag_id
         WHERE a.ended_at IS NULL AND a.status != 'taken_home'
        """).fetchall()
    n = 0
    for r in rows:
        schedule_reminder(conn, fire_at=fire_at, kind="pickup_reminder",
                          ref_type="child", ref_id=None,
                          payload={"phone": r["phone"], "child_code": r["code"],
                                   "bag_no": r["bag_no"],
                                   "title": "周末睡袋领回提醒",
                                   "body": f"{r['code']} 的睡袋 {r['bag_no']} "
                                           "今周可扫码领回清洗，请家长安排时间。"},
                          commit=False)
        n += 1
    conn.commit()
    return n


def schedule_monday_returns(conn: sqlite3.Connection,
                            fire_at: datetime | None = None) -> int:
    fire_at = fire_at or (datetime.now() + timedelta(days=3))
    rows = conn.execute(
        """
        SELECT p.phone, ch.code, b.bag_no
          FROM assignments a
          JOIN children ch ON ch.id = a.child_id
          JOIN parents p ON p.child_id = ch.id
          JOIN sleeping_bags b ON b.id = a.bag_id
         WHERE a.ended_at IS NULL AND a.status = 'taken_home'
        """).fetchall()
    n = 0
    for r in rows:
        schedule_reminder(conn, fire_at=fire_at, kind="return_reminder",
                          payload={"phone": r["phone"], "child_code": r["code"],
                                   "bag_no": r["bag_no"],
                                   "title": "周一睡袋送回提醒",
                                   "body": f"请明天把 {r['code']} 的睡袋 "
                                           f"{r['bag_no']} 清洗后带回，"
                                           "老师会当面核对标签与清洁状态。"},
                          commit=False)
        n += 1
    conn.commit()
    return n


class ReminderScheduler:
    """单进程后台调度器。到期任务经 messages 模拟适配器发出。"""

    def __init__(self, conn_factory, *, interval: float = 10.0,
                 on_fire=None):
        self._conn_factory = conn_factory
        self._interval = interval
        self._on_fire = on_fire or self._default_fire
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "ReminderScheduler":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reminders",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # 调度异常不能打死线程
                print(f"[reminders] tick error: {e}")
            self._stop.wait(self._interval)

    def tick(self) -> int:
        """取所有到期任务执行。返回本次处理条数。"""
        conn = self._conn_factory()
        try:
            now = datetime.now().strftime(FMT)
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status='pending' AND fire_at<=?"
                " ORDER BY fire_at LIMIT 10", (now,)).fetchall()
            for r in rows:
                self._on_fire(conn, r)
                conn.execute(
                    "UPDATE reminders SET status='done', done_at=? WHERE id=?",
                    (now, r["id"]))
                conn.commit()
            return len(rows)
        finally:
            conn.close()

    @staticmethod
    def _default_fire(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        payload = json.loads(row["payload"] or "{}")
        phone = payload.get("phone")
        title = payload.get("title", row["kind"])
        body = payload.get("body", "")
        if phone:
            receipt = send_message(conn, phone, row["kind"], title, body)
            print(f"[reminders] -> {phone}: {receipt['title']}")
