"""最小 Nostr 客户端：NIP-01 子集。

每个 relay 一条独立连接协程，自动重连与指数退避；发布走已连接的 relay 广播。
连不上任何公共 relay 时不影响本地功能——内置 relay 同样走这套协议。

除了 Nostr 事件，还承载 AETHER_* 前缀的中继隧道消息（见 p2p/tunnel.py）：
两类消息共用同一条 WebSocket 连接，靠消息前缀区分。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable

import websockets

from aether.events import bus
from aether.identity import Identity

RECONNECT_MAX = 30.0


class RelayState:
    def __init__(self, url: str) -> None:
        self.url = url
        self.status = "idle"  # idle | connecting | connected | error | stopped
        self.last_error = ""
        self.attempts = 0
        self.events_in = 0
        self.events_out = 0
        self.connected_at = 0.0
        self.ws = None
        self.builtin = url.startswith("ws://127.0.0.1")

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "status": self.status,
            "error": self.last_error,
            "attempts": self.attempts,
            "events_in": self.events_in,
            "events_out": self.events_out,
            "uptime": round(time.time() - self.connected_at, 1) if self.connected_at else 0,
            "builtin": self.builtin,
        }


class NostrClient:
    def __init__(
        self,
        identity: Identity,
        on_event: Callable[[dict, str], Awaitable[None]],
        sub_filter: dict | None = None,
        on_raw: Callable[[list, str], Awaitable[None]] | None = None,
        hello_provider: Callable[[], str] | None = None,
    ) -> None:
        self.identity = identity
        self.on_event = on_event
        self.on_raw = on_raw
        self.hello_provider = hello_provider
        self._conns: dict[str, RelayState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._sub_id = "aether-" + identity.pubkey[:12]
        self._filter = sub_filter or {}
        self._running = False

    # ---- 生命周期 ----
    async def start(self, urls: list[str]) -> None:
        self._running = True
        for url in urls:
            await self.add_relay(url)

    async def add_relay(self, url: str) -> None:
        url = url.strip()
        if not url or url in self._tasks:
            return
        st = RelayState(url)
        self._conns[url] = st
        self._tasks[url] = asyncio.create_task(self._run(st), name=f"relay:{url}")

    async def remove_relay(self, url: str) -> None:
        task = self._tasks.pop(url, None)
        self._conns.pop(url, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._conns.clear()

    # ---- 连接循环 ----
    async def _run(self, st: RelayState) -> None:
        backoff = 1.0
        while self._running:
            try:
                st.status = "connecting"
                bus.publish("relay.state", st.to_dict())
                async with websockets.connect(
                    st.url,
                    ping_interval=25,
                    ping_timeout=20,
                    open_timeout=15,
                    max_size=8 << 20,
                    close_timeout=5,
                ) as ws:
                    st.ws = ws
                    st.status = "connected"
                    st.last_error = ""
                    st.connected_at = time.time()
                    st.attempts = 0
                    backoff = 1.0
                    bus.publish("relay.state", st.to_dict())
                    # 先自报身份，relay 才能把别人发给我们的隧道请求转过来
                    if self.hello_provider:
                        try:
                            await ws.send(self.hello_provider())
                        except Exception as exc:
                            bus.publish(
                                "tunnel.error",
                                {"msg": f"HELLO 发送失败: {exc}", "relay": st.url},
                            )
                    if self._filter:
                        await ws.send(json.dumps(["REQ", self._sub_id, self._filter]))
                    async for raw in ws:
                        await self._handle(st, raw)
            except asyncio.CancelledError:
                st.status = "stopped"
                st.ws = None
                raise
            except Exception as exc:  # 网络类异常一律重连
                st.last_error = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                st.ws = None
                st.connected_at = 0.0
            if not self._running:
                break
            st.status = "error"
            st.attempts += 1
            bus.publish("relay.state", st.to_dict())
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _handle(self, st: RelayState, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, list) or not msg:
            return
        kind = msg[0]

        # 隧道消息：交给上层处理，不进 Nostr 分支
        if isinstance(kind, str) and kind.startswith("AETHER_"):
            if self.on_raw:
                try:
                    await self.on_raw(msg, st.url)
                except Exception as exc:
                    bus.publish("tunnel.error", {"msg": str(exc)[:200], "relay": st.url})
            return

        if kind == "EVENT" and len(msg) >= 3 and isinstance(msg[2], dict):
            st.events_in += 1
            try:
                await self.on_event(msg[2], st.url)
            except Exception as exc:
                bus.publish("p2p.error", {"msg": f"处理事件失败：{exc}", "relay": st.url})
        elif kind == "NOTICE" and len(msg) >= 2:
            st.last_error = str(msg[1])[:200]
        elif kind == "OK" and len(msg) >= 4 and msg[2] is False:
            st.last_error = f"relay 拒收事件：{msg[3]}"[:200]

    # ---- 发布 ----
    async def publish(self, event: dict) -> int:
        payload = json.dumps(["EVENT", event], ensure_ascii=False)
        return await self.send_raw(payload)

    async def send_raw(self, text: str) -> int:
        """把原始文本发给所有已连接的 relay，返回成功投递的连接数。"""
        sent = 0
        for st in list(self._conns.values()):
            ws = st.ws
            if ws is None:
                continue
            try:
                await ws.send(text)
                st.events_out += 1
                sent += 1
            except Exception as exc:
                st.last_error = str(exc)[:200]
        return sent

    # ---- 观测 ----
    def status(self) -> list[dict]:
        return [st.to_dict() for st in self._conns.values()]

    @property
    def connected_count(self) -> int:
        return sum(1 for st in self._conns.values() if st.status == "connected")
