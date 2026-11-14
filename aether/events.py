"""进程内事件总线：后台任务把状态变化推给 SSE 订阅者（GUI）。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

MAX_QUEUE = 512
HISTORY = 400


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self._seq = 0

    def publish(self, kind: str, payload: dict | None = None) -> dict[str, Any]:
        self._seq += 1
        evt = {"seq": self._seq, "kind": kind, "ts": time.time(), "payload": payload or {}}
        self._history.append(evt)
        for q in list(self._subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass  # 慢消费者直接丢事件，不阻塞发布方
        return evt

    def subscribe(self, replay: int = 100) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subs.add(q)
        for evt in list(self._history)[-replay:]:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                break
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(self._history)[-limit:]


bus = EventBus()
