"""进程内事件总线：后台任务把状态变化推给 SSE 订阅者（GUI）。

同时可以把事件按 JSONL 追加到磁盘，作为结构化日志——事件本来就带
kind / ts / payload，天然是可查询的记录，不需要另造一套日志格式。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

MAX_QUEUE = 512
HISTORY = 400


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self._seq = 0
        self._log_path: Path | None = None
        self._log_lock = threading.Lock()
        self.dropped = 0

    def attach_log(self, path: Path) -> None:
        """把事件同时写入 JSONL。失败不影响主流程。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_path = path
        except OSError:
            self._log_path = None

    def _write_log(self, evt: dict[str, Any]) -> None:
        if self._log_path is None:
            return
        try:
            line = json.dumps(evt, ensure_ascii=False, default=str)
            with self._log_lock:
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            self._log_path = None  # 写失败就停掉，避免拖累主流程

    def publish(self, kind: str, payload: dict | None = None) -> dict[str, Any]:
        self._seq += 1
        evt = {"seq": self._seq, "kind": kind, "ts": time.time(), "payload": payload or {}}
        self._history.append(evt)
        self._write_log(evt)
        for q in list(self._subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                self.dropped += 1  # 慢消费者直接丢事件，不阻塞发布方
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
