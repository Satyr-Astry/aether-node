"""P2P 服务编排：relay 连接、广告发布与定时刷新、发现表维护。"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import time

from aether.config import get_runtime_host, get_runtime_port, get_settings
from aether.events import bus
from aether.identity import get_identity
from aether.library import ModelLibrary
from aether.llama_server import LlamaRuntime
from aether.p2p.ad import DEFAULT_TTL, KIND_AD, TOPIC, build_ad, build_event, parse_ad
from aether.p2p.discovery import PeerTable
from aether.p2p.nostr import NostrClient
from aether.p2p.router import Router
from aether.p2p.tunnel import TunnelManager

REFRESH_INTERVAL = 120.0  # 广告重发周期，需远小于 TTL
PRUNE_INTERVAL = 60.0


def lan_ip() -> str:
    """取本机在默认路由上的地址。UDP connect 只做路由查找，不产生任何流量。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1，保留地址
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def classify_reachability(host: str) -> str:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "public"  # 域名按可达处理
    if addr.is_loopback:
        return "relay-only"
    if addr.is_private:
        return "lan"
    return "public"


class P2PService:
    def __init__(self, library: ModelLibrary, runtime: LlamaRuntime) -> None:
        self.settings = get_settings()
        self.identity = get_identity()
        self.library = library
        self.runtime = runtime
        self.peers = PeerTable(self.identity.pubkey)
        self.router = Router(runtime, library, self.peers)
        self.client = NostrClient(
            self.identity,
            self._on_event,
            {"kinds": [KIND_AD], "#t": [TOPIC]},
            on_raw=self._on_raw,
            hello_provider=self._hello,
        )
        # 隧道：让 relay-only 节点也能被调用。
        # 关闭自身回环——不给自己转发（否则单节点会调用自己两次）。
        self.tunnel = TunnelManager(
            self.identity,
            self.client,
            lambda: (self.runtime.base_url if self.runtime.state == "ready" and self.runtime.port else ""),
        )
        self.advertise = bool(self.settings.advertise)
        self.started = False
        self.last_publish_at = 0.0
        self.last_publish_relays = 0
        self.last_error = ""
        self._refresh_task: asyncio.Task | None = None
        self._prune_task: asyncio.Task | None = None
        self._hello_task: asyncio.Task | None = None

    # ---------- relay 列表 ----------
    def relay_urls(self) -> list[str]:
        urls: list[str] = []
        if self.settings.local_relay_enabled:
            urls.append(self.settings.local_relay_url())
        urls.extend(self.settings.all_relays())
        seen: list[str] = []
        for u in urls:
            if u and u not in seen:
                seen.append(u)
        return seen

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        await self.client.start(self.relay_urls())
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="ad-refresh")
        self._prune_task = asyncio.create_task(self._prune_loop(), name="peer-prune")
        self._hello_task = asyncio.create_task(self._hello_loop(), name="tunnel-hello")
        bus.publish("p2p.started", {"relays": self.relay_urls(), "node_id": self.identity.pubkey})
        if self.advertise:
            await self.publish_ad()

    async def _hello_loop(self) -> None:
        """周期性重新注册隧道身份。

        连接刚建立那一刻发一次 HELLO 并不可靠（握手完成的时序不受控），
        靠这个循环保证 relay 侧始终知道我们在线。
        """
        while True:
            try:
                await asyncio.sleep(20)
                if self.client.connected_count:
                    await self.tunnel.announce()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]

    async def stop(self) -> None:
        self.started = False
        for task in (self._refresh_task, self._prune_task, self._hello_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._refresh_task = self._prune_task = self._hello_task = None
        await self.client.stop()
        bus.publish("p2p.stopped", {})

    # ---------- 广告 ----------
    def build_payload(self) -> dict:
        models: list[dict] = []
        if self.runtime.state == "ready" and self.runtime.model:
            m = self.runtime.model
            models.append({
                "id": m.display,
                "ctx": int(self.runtime.params.get("ctx") or 0),
                "size_gb": round(m.size / (1 << 30), 2),
                "quant": m.quant,
                "arch": (m.meta or {}).get("architecture", ""),
            })
        host = get_runtime_host()
        port = get_runtime_port()
        if self.settings.public_endpoint:
            endpoint = self.settings.public_endpoint.rstrip("/")
            reach = "public"
        elif host in ("0.0.0.0", "::"):
            ip = lan_ip()
            endpoint = f"http://{ip}:{port}"
            reach = classify_reachability(ip)
        else:
            endpoint = f"http://{host}:{port}"
            reach = classify_reachability(host)
            if reach == "relay-only":
                # 只监听回环意味着外部无法直达，别公布一个没用的地址；
                # 让调用方走 relay 隧道。
                endpoint = ""
        return build_ad(
            self.identity,
            name=self.settings.node_name,
            endpoint=endpoint,
            reachability=reach,
            models=models,
            price={"in": self.settings.price_in, "out": self.settings.price_out, "unit": "USD/1M"},
            region=self.settings.region,
            ttl=DEFAULT_TTL,
            # EVM 地址必须自己公布：节点 ID 是 x-only 公钥，推不出唯一的 EVM 地址
            extra={"evm": self.identity.evm_address},
        )

    async def publish_ad(self) -> int:
        ad = self.build_payload()
        event = build_event(self.identity, ad)
        sent = await self.client.publish(event)
        self.last_publish_at = time.time()
        self.last_publish_relays = sent
        self.peers.own_ad = ad
        bus.publish("ad.published", {
            "models": [m.get("id") for m in ad.get("models") or []],
            "relays": sent,
            "endpoint": ad.get("endpoint"),
            "reachability": ad.get("reachability"),
        })
        return sent

    def set_advertise(self, value: bool) -> None:
        self.advertise = bool(value)

    async def ensure_relay(self, timeout: float = 15.0) -> int:
        """等待至少一个 relay 连上，避免"点了广播但一条也没发出去"。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.client.connected_count > 0:
                return self.client.connected_count
            await asyncio.sleep(0.4)
        return self.client.connected_count

    # ---------- 隧道 ----------
    def _hello(self) -> str:
        return json.dumps(self.tunnel.hello_message())

    async def _on_raw(self, msg: list, relay: str) -> None:
        await self.tunnel.handle_raw(msg, relay)

    # ---------- 事件 ----------
    async def _on_event(self, evt: dict, relay: str) -> None:
        ad, err = parse_ad(evt)
        if err or ad is None:
            self.peers.reject(err or "广告解析为空", relay)
            return
        self.peers.ingest(ad, relay)

    # ---------- 后台循环 ----------
    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(REFRESH_INTERVAL)
                if self.advertise:
                    await self.publish_ad()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]
                bus.publish("p2p.error", {"msg": self.last_error})

    async def _prune_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(PRUNE_INTERVAL)
                removed = self.peers.prune()
                if removed:
                    bus.publish("peer.pruned", {"count": removed})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    # ---------- 观测 ----------
    def status(self) -> dict:
        return {
            "started": self.started,
            "advertise": self.advertise,
            "node_id": self.identity.pubkey,
            "npub": self.identity.npub,
            "node_name": self.settings.node_name,
            "relays": self.client.status(),
            "relay_connected": self.client.connected_count,
            "peers": self.peers.peers(),
            "peer_stats": self.peers.stats(),
            "tunnel": self.tunnel.stats,
            "own_ad": self.peers.own_ad,
            "last_publish_at": self.last_publish_at,
            "last_publish_relays": self.last_publish_relays,
            "last_error": self.last_error,
            "local_relay_url": self.settings.local_relay_url(),
            "lan_ip": lan_ip(),
        }
