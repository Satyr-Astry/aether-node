"""内置 Nostr relay：NIP-01 最小子集 + AETHER 隧道转发，内存存储。

存在意义：两台内网机器只要有一台对外可达（或其中一台作为汇聚点），
其余节点连它的 /relay 即可互相发现与服务调用，完全不依赖公网。

隧道转发解决的是「目标节点没有直达端点」的情形：
调用方把请求交给 relay，relay 按 node_id 找到目标的 WebSocket 连接转过去，
目标处理完再把结果按块回传。对称 NAT 也能穿透，代价是流量过 relay。

只接受本协议的广告 kind 与隧道消息，避免退化成公共垃圾 relay。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

from fastapi import WebSocket, WebSocketDisconnect

from aether.config import get_settings
from aether.events import bus
from aether.p2p.ad import KIND_AD, verify_event
from aether.p2p.tunnel import verify_hello, verify_request

MAX_EVENTS = 3000
MAX_AGE = 7200
REQ_TTL = 240.0


def _d_tag(evt: dict) -> str:
    for tag in evt.get("tags") or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "d":
            return str(tag[1])
    return ""


def _matches(evt: dict, flt: dict) -> bool:
    kinds = flt.get("kinds")
    if kinds and evt.get("kind") not in kinds:
        return False
    since = flt.get("since")
    if since is not None and int(evt.get("created_at") or 0) < int(since):
        return False
    until = flt.get("until")
    if until is not None and int(evt.get("created_at") or 0) > int(until):
        return False
    for key, wanted in flt.items():
        if not key.startswith("#") or not isinstance(wanted, list):
            continue
        tag_name = key[1:]
        values = {
            str(t[1])
            for t in (evt.get("tags") or [])
            if isinstance(t, list) and len(t) >= 2 and t[0] == tag_name
        }
        if not values & {str(w) for w in wanted}:
            return False
    return True


class MemoryRelay:
    def __init__(self) -> None:
        self._events: dict[str, dict] = {}
        self._subs: dict[WebSocket, dict[str, list[dict]]] = {}
        self._locks: dict[WebSocket, asyncio.Lock] = {}
        # 隧道：身份注册表与在途请求归属
        self._ws_node: dict[WebSocket, str] = {}
        self._by_node: dict[str, WebSocket] = {}
        self._req_owner: dict[str, tuple[WebSocket, float]] = {}
        # 中继带宽计量。
        # bandwidth 是按对端的流量明细（分析用）；relay_total_bytes 才是
        # 本 relay 提供者实际承载的字节数，也是代币激励里的带宽贡献。
        self.bandwidth: dict[str, dict[str, int]] = {}
        self.relay_total_bytes = 0
        # 防护：每节点速率窗口 + 已见请求 id 去重
        self._rate: dict[str, dict[str, float]] = {}
        self._seen: dict[str, float] = {}
        self.rate_limited = 0
        self.replay_blocked = 0
        self.events_accepted = 0
        self.events_rejected = 0
        self.clients_total = 0
        self.tunnel_relayed = 0
        self.tunnel_denied = 0
        self.started_at = time.time()

    # ---- 存储 ----
    def _store(self, evt: dict) -> None:
        key = f"{evt['pubkey']}:{evt['kind']}:{_d_tag(evt)}"
        prev = self._events.get(key)
        if prev and int(prev.get("created_at") or 0) >= int(evt.get("created_at") or 0):
            return  # 可替换事件只保留最新
        self._events[key] = evt
        self._prune()

    def _prune(self) -> None:
        if len(self._events) <= MAX_EVENTS:
            cutoff = time.time() - MAX_AGE
            stale = [k for k, v in self._events.items() if int(v.get("created_at") or 0) < cutoff]
            for k in stale:
                del self._events[k]
            return
        ordered = sorted(self._events.items(), key=lambda kv: int(kv[1].get("created_at") or 0))
        for k, _ in ordered[: len(self._events) - MAX_EVENTS]:
            del self._events[k]

    def _prune_requests(self) -> None:
        now = time.time()
        for req_id, (_, ts) in list(self._req_owner.items()):
            if now - ts > REQ_TTL:
                self._req_owner.pop(req_id, None)
        window = int(get_settings().relay_replay_window or 300)
        for req_id, ts in list(self._seen.items()):
            if now - ts > window:
                self._seen.pop(req_id, None)

    def _check_rate(self, node: str) -> str | None:
        """每节点每分钟的请求配额。返回拒绝原因，通过则返回 None。"""
        limit = int(get_settings().relay_rate_limit or 0)
        if limit <= 0:
            return None
        now = time.time()
        rec = self._rate.get(node)
        if rec is None or now - rec["start"] >= 60:
            self._rate[node] = {"start": now, "count": 1.0}
            return None
        rec["count"] += 1
        if rec["count"] > limit:
            return f"超出速率限制（{limit} 次/分钟）"
        return None

    def _meter_bw(self, node: str, field: str, size: int) -> None:
        size = max(0, int(size))
        self.relay_total_bytes += size
        if not node:
            return
        rec = self.bandwidth.setdefault(
            node, {"forwarded_out": 0, "forwarded_in": 0, "messages": 0}
        )
        rec[field] = rec.get(field, 0) + size
        if field == "forwarded_out":
            rec["messages"] += 1

    # ---- 发送 ----
    async def _send(self, ws: WebSocket, payload: list) -> None:
        lock = self._locks.setdefault(ws, asyncio.Lock())
        async with lock:
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def _broadcast(self, evt: dict) -> None:
        for ws, subs in list(self._subs.items()):
            for sub_id, filters in list(subs.items()):
                if any(_matches(evt, f) for f in filters):
                    await self._send(ws, ["EVENT", sub_id, evt])

    # ---- 隧道 ----
    async def _on_hello(self, ws: WebSocket, msg: list) -> None:
        if len(msg) < 4:
            await self._send(ws, ["AETHER_HELLO_FAIL", "参数不足"])
            return
        pubkey, ts, sig = str(msg[1]), int(msg[2] or 0), str(msg[3])
        if not verify_hello(pubkey, ts, sig):
            bus.publish("relay.hello_fail", {"node_id": pubkey[:12], "ts": ts})
            await self._send(ws, ["AETHER_HELLO_FAIL", "身份签名无效或已过期"])
            return
        self._ws_node[ws] = pubkey
        self._by_node[pubkey] = ws
        await self._send(ws, ["AETHER_HELLO_OK", pubkey])
        bus.publish("relay.hello", {"node_id": pubkey[:12], "clients": len(self._by_node)})

    async def _on_req(self, ws: WebSocket, msg: list) -> None:
        req_id = str(msg[1]) if len(msg) > 1 else ""
        caller = self._ws_node.get(ws)
        if not caller:
            self.tunnel_denied += 1
            await self._send(ws, ["AETHER_REQ_DENIED", req_id, "调用方未注册身份（先发 AETHER_HELLO）"])
            return
        if not verify_request(caller, msg):
            self.tunnel_denied += 1
            await self._send(ws, ["AETHER_REQ_DENIED", req_id, "请求签名无效"])
            return

        # 重放：同一 req_id 在窗口内只接受一次
        if req_id in self._seen:
            self.replay_blocked += 1
            await self._send(ws, ["AETHER_REQ_DENIED", req_id, "重复的请求 id（疑似重放）"])
            return

        limited = self._check_rate(caller)
        if limited:
            self.rate_limited += 1
            await self._send(ws, ["AETHER_REQ_DENIED", req_id, limited])
            return
        self._seen[req_id] = time.time()

        target = str(msg[2])
        target_ws = self._by_node.get(target)
        if target_ws is None or target == caller:
            self.tunnel_denied += 1
            reason = (
                "目标是自己"
                if target == caller
                else f"目标节点不在线 target={target[:12]} by_node={[k[:12] for k in self._by_node]} ws_node={[v[:12] for v in self._ws_node.values()]}"
            )
            await self._send(ws, ["AETHER_REQ_DENIED", req_id, reason])
            return
        self._prune_requests()
        self._req_owner[req_id] = (ws, time.time())
        self.tunnel_relayed += 1
        # biz_id 放在第 7 位随转发带过去，目标节点用它写账本，双方才能按同一键对账
        biz_id = str(msg[8]) if len(msg) > 8 else ""
        self._meter_bw(target, "forwarded_out", len(str(msg[5])))
        await self._send(
            target_ws,
            ["AETHER_FWD", req_id, caller, str(msg[3]), str(msg[4]), str(msg[5]), biz_id],
        )

    async def _on_res(self, ws: WebSocket, msg: list) -> None:
        if len(msg) < 5:
            return
        req_id = str(msg[1])
        owner = self._req_owner.get(req_id)
        if not owner:
            return
        caller_ws, _ = owner
        await self._send(caller_ws, msg)
        caller_node = self._ws_node.get(caller_ws) or ""
        self._meter_bw(caller_node, "forwarded_in", len(str(msg[3])))
        if bool(msg[4]):
            self._req_owner.pop(req_id, None)

    # ---- 连接处理 ----
    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        self._subs[ws] = {}
        self._locks[ws] = asyncio.Lock()
        self.clients_total += 1
        bus.publish("relay.client", {"clients": len(self._subs)})
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    await self._send(ws, ["NOTICE", "invalid json"])
                    continue
                if not isinstance(msg, list) or not msg:
                    continue
                verb = msg[0]

                if verb == "EVENT" and len(msg) >= 2 and isinstance(msg[1], dict):
                    evt = msg[1]
                    err = verify_event(evt)
                    if err or evt.get("kind") != KIND_AD:
                        self.events_rejected += 1
                        await self._send(
                            ws, ["OK", evt.get("id", ""), False, err or "unsupported kind"]
                        )
                        continue
                    self.events_accepted += 1
                    self._store(evt)
                    await self._send(ws, ["OK", evt["id"], True, ""])
                    await self._broadcast(evt)

                elif verb == "REQ" and len(msg) >= 3:
                    sub_id = str(msg[1])
                    filters = [f for f in msg[2:] if isinstance(f, dict)]
                    self._subs[ws][sub_id] = filters
                    for evt in list(self._events.values()):
                        if any(_matches(evt, f) for f in filters):
                            await self._send(ws, ["EVENT", sub_id, evt])
                    await self._send(ws, ["EOSE", sub_id])

                elif verb == "CLOSE" and len(msg) >= 2:
                    self._subs[ws].pop(str(msg[1]), None)

                elif verb == "AETHER_HELLO":
                    await self._on_hello(ws, msg)

                elif verb == "AETHER_REQ":
                    await self._on_req(ws, msg)

                elif verb == "AETHER_RES":
                    await self._on_res(ws, msg)

                elif verb == "PING":
                    await self._send(ws, ["PONG"])
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            bus.publish("relay.error", {"msg": str(exc)[:200]})
        finally:
            self._subs.pop(ws, None)
            self._locks.pop(ws, None)
            node = self._ws_node.pop(ws, None)
            bus.publish("relay.ws_closed", {
                "node": (node or "-")[:12],
                "by_node_before": [k[:12] for k in self._by_node],
                "same_ws": bool(node and self._by_node.get(node) is ws),
            })
            if node and self._by_node.get(node) is ws:
                del self._by_node[node]
            bus.publish("relay.client", {"clients": len(self._subs)})

    def stats(self) -> dict:
        return {
            "clients": len(self._subs),
            "clients_total": self.clients_total,
            "events_stored": len(self._events),
            "events_accepted": self.events_accepted,
            "events_rejected": self.events_rejected,
            "nodes_registered": len(self._by_node),
            "tunnel_relayed": self.tunnel_relayed,
            "tunnel_denied": self.tunnel_denied,
            "tunnel_pending": len(self._req_owner),
            "rate_limited": self.rate_limited,
            "replay_blocked": self.replay_blocked,
            "relay_total_bytes": self.relay_total_bytes,
            "bandwidth": {
                k: dict(v) for k, v in sorted(
                    self.bandwidth.items(), key=lambda kv: -(kv[1].get("forwarded_in", 0))
                )[:20]
            },
            "uptime": round(time.time() - self.started_at, 1),
        }


relay = MemoryRelay()
