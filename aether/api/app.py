"""FastAPI 装配。"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from aether import APP_NAME, __version__
from aether.api import routes_ledger, routes_models, routes_openai, routes_p2p, routes_system
from aether.api.state import get_state
from aether.config import get_settings
from aether.events import bus
from aether.llama_server import runtime
from aether.p2p.relay_server import relay as memory_relay


def _gui_dir() -> Path:
    """PyInstaller 打包后静态资源在 _MEIPASS 下，源码运行时在包目录旁。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        bundled = base / "aether" / "gui"
        if bundled.exists():
            return bundled
        return Path(sys.executable).parent / "aether" / "gui"
    return Path(__file__).resolve().parent.parent / "gui"


GUI_DIR = _gui_dir()

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_local(host: str | None) -> bool:
    host = (host or "").strip()
    return host in LOCAL_HOSTS or host.startswith("127.")


def _presented_key(headers) -> str:
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-key") or ""


def _guard_protected(request) -> JSONResponse | None:
    """共享模式下，非本地访问 /api 与 /v1 必须带 api_key。

    本地回环访问免鉴权：GUI 与运维脚本都在本机，强行让它们读密钥文件只是麻烦，
    并不增加安全性——能读文件的人本来就能读 settings.json。
    """
    settings = get_settings()
    if not settings.share_enabled:
        return None
    if _is_local(request.client.host if request.client else None):
        return None
    if _presented_key(request.headers) == settings.api_key:
        return None
    return JSONResponse(
        {"detail": "共享模式下非本地访问需要 api_key（X-Api-Key 或 Bearer）"},
        status_code=401,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = get_state()
    settings = get_settings()
    # 事件总线同时落 JSONL，作为结构化日志
    bus.attach_log(settings.logs_path / "events.jsonl")
    bus.publish("app.start", {"version": __version__, "node_id": state.identity.pubkey})
    await state.p2p.start()
    try:
        yield
    finally:
        await state.p2p.stop()
        await runtime.stop()
        bus.publish("app.stop", {})


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _auth_guard(request, call_next):
        path = request.url.path
        if path.startswith(("/api/", "/v1/")) or path in ("/api", "/v1"):
            denied = _guard_protected(request)
            if denied is not None:
                return denied
        return await call_next(request)

    app.include_router(routes_system.router)
    app.include_router(routes_system.ops_router)
    app.include_router(routes_models.router)
    app.include_router(routes_p2p.router)
    app.include_router(routes_openai.router)
    app.include_router(routes_ledger.router)

    @app.websocket("/relay")
    async def _relay_endpoint(websocket: WebSocket):
        settings = get_settings()
        client_host = websocket.client.host if websocket.client else None
        # relay 是共享资源：非本地连接在共享模式下必须带 token，否则可以被随意占用
        if settings.share_enabled and not _is_local(client_host):
            token = websocket.query_params.get("token") or ""
            if token != settings.api_key:
                await websocket.close(code=1008, reason="relay 需要 token")
                return
        await memory_relay.handle(websocket)

    if GUI_DIR.exists():
        app.mount("/", StaticFiles(directory=str(GUI_DIR), html=True), name="gui")

    return app


app = create_app()
