"""模型检索、下载、加载。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from aether.api.state import get_state
from aether.config import get_settings
from aether.events import bus
from aether.hub import CURATED, HubClient
from aether.llama_server import runtime

router = APIRouter(prefix="/api", tags=["models"])


def _hub() -> HubClient:
    s = get_settings()
    return HubClient(s.hf_endpoint, s.hf_token)


@router.get("/hub/curated")
async def curated() -> dict:
    return {"items": CURATED}


@router.get("/hub/search")
async def hub_search(q: str, limit: int = 30) -> dict:
    if not q.strip():
        return {"results": [], "hint": "输入关键词，例如 qwen2.5 gguf"}
    hub = _hub()
    try:
        repos = await hub.search(q.strip(), limit=max(1, min(limit, 60)))
        return {"results": [r.to_dict() for r in repos], "endpoint": hub.endpoint}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await hub.aclose()


@router.get("/hub/files")
async def hub_files(repo: str) -> dict:
    hub = _hub()
    try:
        files = await hub.list_gguf_files(repo)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await hub.aclose()
    return {
        "repo": repo,
        "files": [
            {"path": f.path, "fname": f.fname, "size": f.size,
             "size_gb": round(f.size / (1 << 30), 2), "quant": f.quant}
            for f in files
        ],
    }


@router.post("/models/download")
async def start_download(payload: dict) -> dict:
    repo = (payload.get("repo") or "").strip()
    file = (payload.get("file") or "").strip()
    if not repo or not file:
        raise HTTPException(status_code=400, detail="需要 repo 和 file")
    state = get_state()
    task = await state.downloads.create(repo, file)
    return {"task": task.to_dict()}


@router.get("/downloads")
async def list_downloads() -> dict:
    return {"tasks": get_state().downloads.list()}


@router.delete("/downloads/{task_id}")
async def cancel_download(task_id: str) -> dict:
    ok = get_state().downloads.cancel(task_id)
    return {"ok": ok}


@router.post("/downloads/clear")
async def clear_downloads() -> dict:
    return {"cleared": get_state().downloads.clear_finished()}


@router.get("/models")
async def list_models() -> dict:
    state = get_state()
    state.library.rescan()
    return {
        "models": [m.to_dict() for m in state.library.all()],
        "loaded": runtime.status().get("model"),
    }


@router.post("/models/load")
async def load_model(payload: dict) -> dict:
    key = payload.get("key") or ""
    ctx = payload.get("ctx")
    state = get_state()
    model = state.library.get(key) or state.library.by_model_id(key)
    if not model:
        raise HTTPException(status_code=404, detail=f"模型不存在：{key}")

    async def run() -> None:
        try:
            await runtime.start(model, force_ctx=int(ctx) if ctx else None)
            state.library.mark_loaded(model.key)
            await state.p2p.publish_ad() if state.p2p.advertise else None
        except Exception as exc:
            bus.publish("runtime.state", {"state": "failed", "error": str(exc)[:300]})

    asyncio.create_task(run(), name=f"load:{model.key}")
    bus.publish("runtime.state", {"state": "starting", "model": model.display})
    return {"accepted": True, "model": model.to_dict()}


@router.post("/models/unload")
async def unload_model() -> dict:
    state = get_state()
    await runtime.stop()
    state.library.mark_loaded(None)
    return {"ok": True}


@router.delete("/models/{key:path}")
async def delete_model(key: str, delete_file: bool = True) -> dict:
    state = get_state()
    if runtime.model and runtime.model.key == key:
        await runtime.stop()
    ok = state.library.remove(key, delete_file=delete_file)
    return {"ok": ok}


@router.get("/runtime")
async def runtime_status() -> dict:
    return runtime.status()


@router.post("/runtime/install")
async def install_runtime() -> dict:
    async def run() -> None:
        try:
            path = await runtime.ensure_binary(use_cuda=True)
            bus.publish("runtime.binary", {"path": str(path), "ok": True})
        except Exception as exc:
            bus.publish("runtime.binary", {"ok": False, "error": str(exc)[:300]})

    asyncio.create_task(run(), name="install-runtime")
    return {"accepted": True}
