"""家长消息：模拟适配器。

不接入真实短信/公众号，send() 直接返回一条送达回执；
所有消息同时由 service 落库 messages 表，页面可查看。
"""
from __future__ import annotations

from itertools import count
from typing import Optional


class MockMessageAdapter:
    name = "mock"

    def __init__(self):
        self._seq = count(1)
        self.sent: list[dict] = []  # 内存侧也留一份，方便测试断言

    def send(self, *, child_id: int, kind: str, body: str) -> dict:
        receipt = {
            "id": next(self._seq),
            "adapter": self.name,
            "ok": True,
            "child_id": child_id,
            "kind": kind,
            "body": body,
        }
        self.sent.append(receipt)
        return receipt

    def messages_for(self, child_id: int) -> list[dict]:
        return [m for m in self.sent if m["child_id"] == child_id]
