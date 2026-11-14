"""系统状态、事件流、设置。"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from aether import APP_NAME, __version__
from aether.api.state import get_state
from aether.config import get_settings, save_settings
from aether.events import bus
from aether.hardware import detect_hardware
from aether.llama_server import runtime
from aether.p2p.relay_server import relay as memory_relay

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"ok": True, "app": APP_NAME, "version": __version__, "ts": time.time()}


@router.get("/system")
async def system_info() -> dict:
    state = get_state()
    s = get_settings()
    return {
        "app": APP_NAME,
        "version": __version__,
        "node_id": state.identity.pubkey,
        "npub": state.identity.npub,
        "hardware": detect_hardware(),
        "paths": {
            "home": str(s.home),
            "models": str(s.models_path),
            "runtime": str(s.runtime_path),
            "downloads": str(s.downloads_path),
        },
        "runtime": runtime.status(),
        "relay": memory_relay.stats(),
        "api": {"host": s.api_host, "port": s.api_port, "key_required": bool(s.api_key)},
    }


@router.get("/settings")
async def read_settings() -> dict:
    return {"settings": get_settings().model_dump(mode="json")}


@router.post("/settings")
async def write_settings(payload: dict) -> dict:
    s = get_settings()
    allowed = set(type(s).model_fields.keys()) - {"home", "install_id"}
    patch = {k: v for k, v in payload.items() if k in allowed}
    updated = s.model_copy(update=patch)
    save_settings(updated)
    bus.publish("settings.updated", {"keys": sorted(patch.keys())})
    return {"ok": True, "settings": updated.model_dump(mode="json"), "changed": sorted(patch.keys())}


@router.post("/share")
async def toggle_share(payload: dict | None = None) -> dict:
    """开启/关闭对外共享。

    开启时会强制生成 api_key——把 GPU 暴露到网络必须带鉴权，没有"裸端口"这个选项。
    绑定地址变化需要重启进程才生效。
    """
    s = get_settings()
    enabled = bool((payload or {}).get("enabled"))
    previous_host = s.effective_host()
    updated = s.model_copy(update={"share_enabled": enabled})
    if enabled:
        key = updated.ensure_api_key()
        updated = updated.model_copy(update={"api_key": key})
    save_settings(updated)
    bus.publish("settings.updated", {"keys": ["share_enabled", "api_key"]})
    return {
        "share_enabled": updated.share_enabled,
        "api_key": updated.api_key,
        "bind": updated.effective_host(),
        "restart_required": previous_host != updated.effective_host(),
    }


@router.get("/events")
async def event_stream(request: Request) -> StreamingResponse:
    queue = bus.subscribe(replay=80)

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/events/history")
async def event_history(limit: int = 120) -> dict:
    return {"events": bus.history(limit)}

# ---------------- 自有外部 API ----------------

def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-4:] if len(value) > 10 else "…"


@router.get("/providers")
async def list_providers() -> dict:
    """列出已配置的外部端点。api_key 一律脱敏返回。"""
    s = get_settings()
    items = []
    for p in s.external_providers or []:
        items.append({
            "name": p.get("name") or "",
            "endpoint": p.get("endpoint") or "",
            "models": p.get("models") or [],
            "price": p.get("price") or {},
            "api_key_masked": _mask(str(p.get("api_key") or "")),
            "has_key": bool(p.get("api_key")),
        })
    return {"providers": items, "score": s.external_score}


@router.post("/providers")
async def manage_providers(payload: dict) -> dict:
    """add / remove / test。add 时若同名已存在则覆盖。"""
    from aether.p2p import upstream

    s = get_settings()
    data = payload or {}
    action = data.get("action") or "add"
    items = [dict(p) for p in (s.external_providers or [])]
    name = str(data.get("name") or "").strip()

    if action == "add":
        endpoint = str(data.get("endpoint") or "").strip().rstrip("/")
        if not name or not endpoint:
            raise HTTPException(status_code=400, detail="需要 name 与 endpoint")
        entry = {
            "name": name,
            "endpoint": endpoint,
            "api_key": str(data.get("api_key") or ""),
            "models": [str(m) for m in (data.get("models") or []) if m],
            "price": data.get("price") or {"in": 0.0, "out": 0.0},
        }
        items = [p for p in items if p.get("name") != name] + [entry]
    elif action == "remove":
        if not name:
            raise HTTPException(status_code=400, detail="需要 name")
        items = [p for p in items if p.get("name") != name]
    elif action == "test":
        target = next((p for p in items if p.get("name") == name), None)
        if target is None:
            raise HTTPException(status_code=404, detail="端点不存在")
        try:
            models = await upstream.list_models(target.get("endpoint") or "")
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}
        return {"ok": True, "models": [m.get("id") for m in models][:20]}
    else:
        raise HTTPException(status_code=400, detail="action 只能是 add / remove / test")

    save_settings(s.model_copy(update={"external_providers": items}))
    bus.publish("settings.updated", {"keys": ["external_providers"]})
    return {"ok": True, "count": len(items)}
