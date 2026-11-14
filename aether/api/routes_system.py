"""系统状态、事件流、设置。"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from aether import APP_NAME, __version__
from aether.api.state import get_state
from aether.config import get_settings, save_settings
from aether.events import bus
from aether.hardware import detect_hardware
from aether.llama_server import runtime
from aether.p2p.relay_server import relay as memory_relay

router = APIRouter(prefix="/api", tags=["system"])
# 探针与指标按惯例挂在根路径，不套 /api 前缀
ops_router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict:
    return {"ok": True, "app": APP_NAME, "version": __version__, "ts": time.time()}


@router.get("/system")
async def system_info() -> dict:
    state = get_state()
    s = get_settings()
    models = state.library.all()
    runtime_ready = bool(runtime.binary_path())
    # 首次启动判定：没有模型、没配外部端点、也没标记过向导完成
    first_run = (
        not models
        and not (s.external_providers or [])
        and not s.wizard_done
    )
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
        "onboarding": {
            "first_run": first_run,
            "has_model": bool(models),
            "has_runtime": runtime_ready,
            "has_provider": bool(s.external_providers),
            "share_enabled": s.share_enabled,
            "dismissed": s.wizard_done,
        },
    }


@router.post("/onboarding/dismiss")
async def dismiss_onboarding() -> dict:
    """标记引导已完成，之后不再显示。"""
    s = get_settings()
    save_settings(s.model_copy(update={"wizard_done": True}))
    bus.publish("settings.updated", {"keys": ["wizard_done"]})
    return {"ok": True}


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

# ---------------- 运维接口：健康检查与指标 ----------------

@ops_router.get("/healthz")
async def healthz() -> dict:
    """存活探针：进程能响应就返回 200。"""
    return {"ok": True, "ts": time.time()}


@ops_router.get("/readyz")
async def readyz() -> JSONResponse:
    """就绪探针：依赖真的可用才算 ready。

    relay 连不上、模型没加载都不影响进程存活，但此时不该接新流量，
    所以这里分开报告，由编排层决定是否摘除。
    """
    state = get_state()
    p2p = state.p2p
    rt = runtime.status()
    checks = {
        "library": len(state.library.all()) >= 0,
        "relay": p2p.client.connected_count > 0,
        "model_loaded": rt.get("state") == "ready",
        "ledger": state.ledger is not None,
    }
    required = ["ledger"]
    ok = all(checks[k] for k in required)
    return JSONResponse(
        {"ready": ok, "checks": checks, "state": rt.get("state")},
        status_code=200 if ok else 503,
    )


@ops_router.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus 文本格式。刻意不引 prometheus_client——指标项有限，
    手写文本比多一个依赖划算。"""
    state = get_state()
    rt = runtime.status()
    summary = state.ledger.summary()
    stats = memory_relay.stats()
    routing = state.p2p.router
    audit = state.ledger.audit_stats()

    lines: list[str] = []

    def emit(name: str, help_text: str, mtype: str, samples: list[tuple[str, float, dict | None]]):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for label_str, value, labels in samples:
            lstr = ""
            if labels:
                inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
                lstr = "{" + inner + "}"
            lines.append(f"{label_str if label_str else name}{lstr} {value}")

    emit("aether_runtime_ready", "模型是否已加载可服务", "gauge",
         [("aether_runtime_ready", 1 if rt.get("state") == "ready" else 0, None)])
    # 在途请求数是闸门是否泄漏的直接指标：压测后应当回到 0
    from aether.api import routes_openai

    emit("aether_inflight", "当前在途的 /v1 请求数", "gauge",
         [("aether_inflight", routes_openai._inflight, None)])
    emit("aether_relay_connected", "已连接的 relay 数", "gauge",
         [("aether_relay_connected", state.p2p.client.connected_count, None)])
    emit("aether_peers", "发现的网络节点数", "gauge",
         [("aether_peers", len(state.p2p.peers.peers()), None)])
    emit("aether_models_local", "本地模型数", "gauge",
         [("aether_models_local", len(state.library.all()), None)])

    for role in ("consume", "serve"):
        block = summary.get(role) or {}
        emit(f"aether_tokens_{role}_total", "已计量的 token 数", "counter",
             [(f"aether_tokens_{role}_total", block.get("in_tok", 0) + block.get("out_tok", 0), None)])
        emit(f"aether_amount_{role}", "已计量的金额（USD）", "gauge",
             [(f"aether_amount_{role}", block.get("amount", 0.0), None)])

    emit("aether_relay_bytes_total", "中继承载的总字节", "counter",
         [("aether_relay_bytes_total", stats.get("relay_total_bytes", 0), None)])
    emit("aether_relay_clients", "relay 当前连接数", "gauge",
         [("aether_relay_clients", stats.get("clients", 0), None)])
    emit("aether_tunnel_relayed_total", "隧道转发次数", "counter",
         [("aether_tunnel_relayed_total", stats.get("tunnel_relayed", 0), None)])
    emit("aether_rate_limited_total", "被速率限制拒绝的次数", "counter",
         [("aether_rate_limited_total", stats.get("rate_limited", 0), None)])
    emit("aether_replay_blocked_total", "被重放防护拒绝的次数", "counter",
         [("aether_replay_blocked_total", stats.get("replay_blocked", 0), None)])

    for verdict in ("pass", "suspect", "skipped", "error"):
        emit("aether_audit_total", "抽样复算结果计数", "counter",
             [("aether_audit_total", audit.get(verdict, 0), {"verdict": verdict})])

    return PlainTextResponse("\n".join(lines) + "\n")

# ---------------- 身份备份与恢复 ----------------

@router.get("/identity/info")
async def identity_info() -> dict:
    from aether.identity import get_identity

    ident = get_identity()
    return {
        "node_id": ident.pubkey,
        "npub": ident.npub,
        "evm_address": ident.evm_address,
        "secret_storage": "identity.json（明文私钥，仅在 data 目录内）",
        "backup_hint": "用 POST /api/identity/export 导出加密备份；丢失 identity.json 会永久失去节点身份",
    }


@router.post("/identity/export")
async def identity_export(payload: dict) -> dict:
    """用口令加密导出身份。返回的 blob 可直接存文件。"""
    from aether import identity_vault
    from aether.identity import get_identity

    password = str((payload or {}).get("password") or "")
    try:
        blob = identity_vault.export_backup(get_identity(), password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"backup": blob}


@router.post("/identity/import")
async def identity_import(payload: dict) -> dict:
    """从加密备份恢复身份。会覆盖现有 identity.json，需要重启生效。"""
    from aether import identity_vault
    from aether.config import get_settings
    from aether.identity import get_identity

    data = payload or {}
    blob = data.get("backup") or {}
    password = str(data.get("password") or "")
    if not isinstance(blob, dict) or not blob:
        raise HTTPException(status_code=400, detail="需要 backup 对象")
    try:
        recovered = identity_vault.decrypt_backup(blob, password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    current = get_identity()
    settings = get_settings()
    import json as _json

    path = settings.home / "identity.json"
    backup_of_old = path.with_suffix(".json.bak")
    if path.exists():
        try:
            backup_of_old.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    path.write_text(_json.dumps({"seckey": recovered["seckey"]}, indent=2), encoding="utf-8")
    return {
        "imported": True,
        "new_node_id": recovered.get("pubkey"),
        "previous_node_id": current.pubkey,
        "old_backup": str(backup_of_old),
        "restart_required": True,
        "warning": "账本与身份绑定：恢复后历史记录仍属于旧节点，需人工核对",
    }


@router.post("/identity/verify")
async def identity_verify(payload: dict) -> dict:
    """校验一份备份能否用给定口令解开，不落盘。"""
    from aether import identity_vault
    from aether.identity import get_identity

    data = payload or {}
    try:
        recovered = identity_vault.decrypt_backup(data.get("backup") or {}, str(data.get("password") or ""))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "pubkey": recovered.get("pubkey"),
        "matches_current": recovered.get("pubkey") == get_identity().pubkey,
    }

@router.get("/update/check")
async def update_check(force: bool = False) -> dict:
    """检查新版本。只告知，不自动下载或替换。"""
    from aether import __version__
    from aether.updater import get_checker

    checker = get_checker(__version__)
    result = await checker.check(force=force)
    result["error"] = checker.last_error
    return result
