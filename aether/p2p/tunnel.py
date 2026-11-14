"""中继隧道：让没有直达端点的节点也能被调用。

NAT 后的节点虽然无法被直接 HTTP 访问，但它一定连着某个 relay。
这里在 relay 上做应用层转发：调用方把请求交给 relay，relay 按 node_id
转给目标节点，目标调用本地推理后把结果流式回传。

相比 UDP 打洞（DCUtR）的优势：不关心 NAT 类型，对称 NAT 一样能用；复用已有的
WebSocket 长连接，不需要额外端口；缺点是全部流量过 relay，带宽成本在 relay 一侧。

协议（与 Nostr 的 NIP-01 消息共存，靠前缀区分）：
  HELLO  客户端 -> relay       ["AETHER_HELLO", pubkey, ts, sig]
  REQ    调用方 -> relay       ["AETHER_REQ", req_id, target, method, path, body, ts, sig]
  FWD    relay  -> 目标        ["AETHER_FWD", req_id, from, method, path, body]
  RES    目标   -> relay       ["AETHER_RES", req_id, status, body, done]
  RES    relay  -> 调用方      ["AETHER_RES", req_id, status, body, done]
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import AsyncIterator, Callable

import httpx

from aether import metering
from aether.config import get_settings
from aether.events import bus
from aether.identity import Identity
from aether.ledger import get_ledger
from aether.p2p.upstream import UpstreamError

REQ_TIMEOUT = 180.0
HELLO_TTL = 600.0
CHUNK_FLUSH = 1024


class TunnelError(UpstreamError):
    """隧道失败。继承 UpstreamError，好让路由层与直连失败走同一套降级逻辑。"""


class TunnelManager:
    """两端通用：既能发起隧道请求，也能响应别人发来的请求。"""

    def __init__(
        self,
        identity: Identity,
        client,
        local_base_url: Callable[[], str],
    ) -> None:
        self.identity = identity
        self.client = client
        self.local_base_url = local_base_url
        self._pending: dict[str, asyncio.Queue] = {}
        self.stats = {"forwarded_in": 0, "requests_out": 0, "served": 0, "errors": 0, "overloaded": 0}
        self.last_error = ""
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        """目标端并发闸门。满就快速失败，让调用方换节点或重试，
        比把请求排进队列更能保护本机——排队只会把超时压力往后传。"""
        if self._sem is None:
            limit = max(1, int(get_settings().tunnel_max_concurrency or 4))
            self._sem = asyncio.Semaphore(limit)
        return self._sem

    # ---------- 注册 ----------
    def hello_message(self) -> list:
        ts = int(time.time())
        signed = self.identity.sign_bytes(f"aether-hello:{self.identity.pubkey}:{ts}".encode())
        return ["AETHER_HELLO", self.identity.pubkey, ts, signed.hex()]

    def verify_hello(self, pubkey: str, ts: int, sig_hex: str) -> bool:
        if abs(time.time() - ts) > HELLO_TTL:
            return False
        return Identity.verify_bytes(pubkey, f"aether-hello:{pubkey}:{ts}".encode(), sig_hex)

    async def announce(self) -> int:
        return await self.client.send_raw(json.dumps(self.hello_message()))

    # ---------- 消息入口 ----------
    async def handle_raw(self, msg: list, relay: str) -> None:
        if not msg:
            return
        verb = msg[0]
        try:
            if verb == "AETHER_FWD":
                await self._on_forward(msg)
            elif verb == "AETHER_RES":
                self._on_result(msg)
            elif verb == "AETHER_REQ_DENIED":
                self._on_denied(msg)
        except Exception as exc:
            self.stats["errors"] += 1
            self.last_error = f"{verb}: {exc}"[:200]
            bus.publish("tunnel.error", {"msg": self.last_error, "relay": relay})

    # ---------- 目标端：响应转发 ----------
    async def _on_forward(self, msg: list) -> None:
        if len(msg) < 6:
            return
        _, req_id, from_node, method, path, body = msg[:6]
        # relay 在 FWD 第 7 位带上业务请求 id；缺失时退回隧道消息 id
        biz_id = str(msg[6]) if len(msg) > 6 and msg[6] else req_id
        self.stats["forwarded_in"] += 1

        sem = self._semaphore()
        if sem.locked():
            self.stats["overloaded"] += 1
            await self._reply(
                req_id, 429,
                json.dumps({"error": "本节点隧道并发已满，请稍后重试或换节点"}),
                True, from_node,
            )
            self._record_serve(biz_id, from_node, body, {}, status=429)
            return

        async with sem:
            await self._forward_inner(req_id, biz_id, from_node, method, path, body)

    async def _forward_inner(
        self, req_id: str, biz_id: str, from_node: str, method: str, path: str, body: str
    ) -> None:
        base = self.local_base_url()
        if not base:
            await self._reply(req_id, 503, json.dumps({"error": "目标节点没有已加载的模型"}), True, from_node)
            return

        url = f"{base.rstrip('/')}{path}"
        self.stats["served"] += 1
        headers = {"Content-Type": "application/json"}

        # 非流式请求要整体回传，否则调用方拿到的是被拆碎的 SSE 文本
        try:
            want_stream = bool(json.loads(body).get("stream"))
        except Exception:
            want_stream = False

        usage: dict = {}
        status = 0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
                if not want_stream:
                    resp = await client.post(url, content=body.encode("utf-8"), headers=headers)
                    status = resp.status_code
                    text = resp.text
                    self.stats["last_status"] = status
                    self.stats["last_body"] = text[:200]
                    if status < 400:
                        try:
                            usage = metering.from_json(json.loads(text))
                        except Exception:
                            usage = {}
                    await self._reply(req_id, status, text, True, from_node)
                else:
                    tail = bytearray()
                    async with client.stream(
                        method, url, content=body.encode("utf-8"), headers=headers
                    ) as resp:
                        status = resp.status_code
                        self.stats["last_status"] = status
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes(4096):
                            buf.extend(chunk)
                            tail.extend(chunk)
                            if len(tail) > metering.TAIL_BYTES:
                                del tail[: len(tail) - metering.TAIL_BYTES]
                            if len(buf) >= CHUNK_FLUSH:
                                await self._reply(
                                    req_id, status, buf.decode("utf-8", "replace"), False, from_node
                                )
                                buf.clear()
                        await self._reply(
                            req_id, status, buf.decode("utf-8", "replace"), True, from_node
                        )
                        self.stats["last_body"] = bytes(tail[-160:]).decode("utf-8", "replace")
                    if status < 400:
                        usage = metering.from_sse(bytes(tail))
        except Exception as exc:
            self.stats["errors"] += 1
            self.last_error = str(exc)[:200]
            await self._reply(req_id, 502, json.dumps({"error": f"目标节点处理失败: {exc}"}), True, from_node)
            self._record_serve(biz_id, from_node, body, {}, status=502)
            return

        self._record_serve(biz_id, from_node, body, usage, status=status or 200)

    def _record_serve(self, req_id: str, client_node: str, body: str, usage: dict, status: int) -> None:
        """提供方视角记账：谁调用的、用了多少 token、按自己的报价算金额。"""
        try:
            model = str(json.loads(body).get("model") or "")
        except Exception:
            model = ""
        settings = get_settings()
        try:
            get_ledger().record(
                role="serve",
                self_node=self.identity.pubkey,
                peer_node=client_node,
                model=model,
                in_tok=int((usage or {}).get("prompt_tokens") or 0),
                out_tok=int((usage or {}).get("completion_tokens") or 0),
                price_in=float(settings.price_in),
                price_out=float(settings.price_out),
                source="relay",
                estimated=not usage,
                req_id=req_id,
                status="ok" if status < 400 else "error",
            )
        except Exception as exc:
            self.stats["errors"] += 1
            self.last_error = f"记账失败: {exc}"[:200]

    async def _reply(self, req_id: str, status: int, body: str, done: bool, target: str) -> None:
        payload = json.dumps(["AETHER_RES", req_id, int(status), body, bool(done)], ensure_ascii=False)
        await self.client.send_raw(payload)

    # ---------- 调用端 ----------
    def _on_result(self, msg: list) -> None:
        if len(msg) < 5:
            return
        req_id = msg[1]
        queue = self._pending.get(req_id)
        if queue is None:
            return
        try:
            queue.put_nowait((int(msg[2]), str(msg[3]), bool(msg[4])))
        except asyncio.QueueFull:
            pass

    def _on_denied(self, msg: list) -> None:
        if len(msg) < 3:
            return
        queue = self._pending.get(msg[1])
        if queue is not None:
            try:
                queue.put_nowait((404, json.dumps({"error": msg[2]}), True))
            except asyncio.QueueFull:
                pass

    async def _request_once(
        self, target: str, method: str, path: str, body: str, biz_id: str
    ) -> AsyncIterator[tuple[int, str, bool]]:
        req_id = secrets.token_hex(8)
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._pending[req_id] = queue
        self.stats["requests_out"] += 1
        try:
            # 签名覆盖业务请求 id，双方记账才能按同一个键对齐
            signed = self.identity.sign_bytes(
                f"{req_id}:{target}:{method}:{path}:{body}:{biz_id}".encode("utf-8")
            )
            payload = json.dumps(
                ["AETHER_REQ", req_id, target, method, path, body, int(time.time()), signed.hex(), biz_id],
                ensure_ascii=False,
            )
            sent = await self.client.send_raw(payload)
            if not sent:
                raise TunnelError("没有可用的 relay 连接，无法走隧道")

            deadline = time.monotonic() + REQ_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TunnelError(f"隧道请求超时（{REQ_TIMEOUT:.0f}s）")
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TunnelError(f"隧道请求超时（{REQ_TIMEOUT:.0f}s）") from None
                yield item
                if item[2]:
                    break
        finally:
            self._pending.pop(req_id, None)

    async def request(
        self, target: str, method: str, path: str, body: str,
        retries: int = 3, biz_id: str | None = None
    ) -> AsyncIterator[tuple[int, str, bool]]:
        """向目标节点发起隧道请求。

        目标刚上线时可能还没在 relay 完成身份注册，relay 会回 404「不在线」。
        这类失败按短退避重试，避免把注册时序竞争暴露成用户可见的错误。

        biz_id 是业务层面的请求标识：调用方的账本记录与目标方的账本记录
        都使用它，是双方对账的唯一键。
        """
        biz = biz_id or secrets.token_hex(8)
        last_error = ""
        for attempt in range(retries):
            pending: list[tuple[int, str, bool]] = []
            retry_needed = False
            try:
                async for item in self._request_once(target, method, path, body, biz):
                    # relay 明确回 404（目标还没注册）时不产出，直接重试
                    if not pending and item[0] == 404:
                        retry_needed = True
                        last_error = item[1]
                        break
                    pending.append(item)
                    yield item
                    if item[2]:
                        return
                if not retry_needed:
                    return
            except TunnelError as exc:
                last_error = str(exc)
                retry_needed = True
            if retry_needed and attempt < retries - 1:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            if retry_needed:
                raise TunnelError(f"隧道请求失败（已重试 {retries} 次）：{last_error}")
            return


def verify_hello(pubkey: str, ts: int, sig_hex: str) -> bool:
    """relay 侧校验客户端的身份注册消息。"""
    if abs(time.time() - ts) > HELLO_TTL:
        return False
    return Identity.verify_bytes(pubkey, f"aether-hello:{pubkey}:{ts}".encode(), sig_hex)


def verify_request(pubkey: str, msg: list) -> bool:
    """relay 侧校验调用方签名的请求。签名覆盖业务请求 id。"""
    try:
        _, req_id, target, method, path, body, ts, sig = msg[:8]
    except (ValueError, IndexError):
        return False
    biz_id = str(msg[8]) if len(msg) > 8 else ""
    if abs(time.time() - ts) > HELLO_TTL:
        return False
    return Identity.verify_bytes(
        pubkey, f"{req_id}:{target}:{method}:{path}:{body}:{biz_id}".encode("utf-8"), sig
    )
