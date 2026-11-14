"""P2P 观测与控制：广播开关、peers、统一模型目录、relay 管理。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from aether.api.state import get_state
from aether.config import get_settings, save_settings
from aether.p2p import upstream

router = APIRouter(prefix="/api/p2p", tags=["p2p"])


@router.get("/status")
async def status() -> dict:
    state = get_state()
    return state.p2p.status()


@router.get("/peers")
async def peers() -> dict:
    state = get_state()
    return {"peers": state.p2p.peers.peers(), "stats": state.p2p.peers.stats()}


@router.get("/catalog")
async def catalog() -> dict:
    state = get_state()
    return {"models": state.p2p.router.catalog(), "last_route": state.p2p.router.last_route}


@router.post("/advertise")
async def set_advertise(payload: dict) -> dict:
    state = get_state()
    enabled = bool(payload.get("enabled"))
    state.p2p.set_advertise(enabled)
    s = get_settings()
    save_settings(s.model_copy(update={"advertise": enabled}))
    sent = 0
    if enabled:
        await state.p2p.ensure_relay(15.0)
        # 开广播前先确保 relay 那边知道我们的身份，否则对方拿不到隧道地址
        await state.p2p.tunnel.announce()
        await asyncio.sleep(0.3)
        sent = await state.p2p.publish_ad()
    return {
        "advertise": enabled,
        "relays": sent,
        "relay_connected": state.p2p.client.connected_count,
        "models": (state.p2p.peers.own_ad or {}).get("models", []),
    }


@router.post("/publish")
async def publish_now() -> dict:
    state = get_state()
    if not state.p2p.advertise:
        raise HTTPException(status_code=409, detail="未开启广播，请先 POST /api/p2p/advertise")
    sent = await state.p2p.publish_ad()
    return {"relays": sent, "ad": state.p2p.peers.own_ad}


@router.get("/own-ad")
async def own_ad() -> dict:
    state = get_state()
    return {"ad": state.p2p.peers.own_ad, "preview": state.p2p.build_payload()}


@router.post("/relays")
async def manage_relays(payload: dict) -> dict:
    action = payload.get("action") or "add"
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="需要 url")
    state = get_state()
    s = get_settings()
    if action == "add":
        if url not in s.extra_relays:
            save_settings(s.model_copy(update={"extra_relays": [*s.extra_relays, url]}))
        await state.p2p.client.add_relay(url)
    elif action == "remove":
        save_settings(s.model_copy(update={"extra_relays": [u for u in s.extra_relays if u != url]}))
        await state.p2p.client.remove_relay(url)
    else:
        raise HTTPException(status_code=400, detail="action 只能是 add 或 remove")
    return {"relays": state.p2p.client.status()}


@router.post("/probe")
async def probe(payload: dict) -> dict:
    """探测某个 peer 的端点是否真的在服务。"""
    node_id = payload.get("node_id") or ""
    state = get_state()
    peer = state.p2p.peers.get(node_id)
    if not peer:
        raise HTTPException(status_code=404, detail="未知节点")
    endpoint = peer["ad"].get("endpoint") or ""
    if not endpoint:
        return {"ok": False, "reason": "该节点未公布可达端点（需要中继）"}
    ok = await upstream.health(endpoint)
    models: list[dict] = []
    if ok:
        try:
            models = await upstream.list_models(endpoint)
        except Exception:
            models = []
    return {"ok": ok, "endpoint": endpoint, "models": models}


@router.get("/lan")
async def lan_info() -> dict:
    state = get_state()
    s = get_settings()
    from aether.config import get_runtime_host, get_runtime_port

    port = get_runtime_port()
    host = get_runtime_host()
    ip = state.p2p.status()["lan_ip"]
    return {
        "lan_ip": ip,
        "api": {"host": host, "port": port},
        "local_relay": s.local_relay_url(),
        "share_hint": f"其他节点可用 relay：ws://{ip}:{port}/relay",
        "openai_hint": f"http://{ip}:{port}/v1",
        "reachable": host not in ("127.0.0.1", "localhost"),
    }
