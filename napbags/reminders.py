"""进程内提醒任务：后台守护线程周期性扫描超时未送回的睡袋。

不依赖 cron / celery；调用 start() 即在本进程内运行，stop() 退出。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


class ReminderScheduler:
    def __init__(self, run_fn: Callable[[], list], interval_seconds: float = 3600.0):
        self.run_fn = run_fn
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.history: list[dict] = []
        self._lock = threading.Lock()

    def tick(self) -> list:
        sent = self.run_fn()
        if sent:
            with self._lock:
                self.history.append(sent)
        return sent

    def _loop(self):
        # 启动后先等一个周期，避免与启动样例在同一时刻抢跑
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # 提醒任务不能拖垮主进程
                self.history.append([{"error": repr(exc)}])

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nap-reminder",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
