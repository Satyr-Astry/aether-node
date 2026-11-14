"""OpenAI 兼容入口：把本机与网络内其他节点的模型统一成 /v1。

路由规则：本机已加载 > 网络内打分最高的节点。非流式请求失败会自动换下一个候选。
每次成功请求都进账本：本地自服务记 role=serve（不计外部债务），
网络请求记 role=consume（形成对某个 peer 的应付）。

流式响应默认不返回 usage，这里会注入 stream_options.include_usage，
再从末块提取；拿不到就标记 estimated，不把估算当真值。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aether import audit, auth
from aether import receipt as receipts
from aether.api.state import get_state
from aether.config import get_settings
from aether.events import bus
from aether.identity import get_identity
from aether.metering import from_sse
from aether.p2p import upstream
from aether.p2p.router import Route

router = APIRouter(tags=["openai"])
TAIL_BYTES = 16384

# 并发闸门：超过上限直接 429，而不是排队。排队会让每个请求都变慢，
# 最后一起超时；快速失败能把压力交还给调用方（它会换节点或重试）。
_inflight = 0


def _enter() -> bool:
    global _inflight
    limit = int(get_settings().api_max_concurrency or 0)
    if limit > 0 and _inflight >= limit:
        return False
    _inflight += 1
    return True


def _leave() -> None:
    global _inflight
    _inflight = max(0, _inflight - 1)


def _check_key(request: Request) -> None:
    """与中间件保持同一套判定。

    早先这里只看"有没有配 key"，导致本地回环调用也被要求带 key——
    而中间件的规则是"仅共享模式下的非本地访问"才校验。两套规则冲突，
    表现为模型加载成功、推理却 401。
    """
    settings = get_settings()
    if not settings.share_enabled:
        return
    provider = auth.build(settings)
    result = provider.authenticate(dict(request.headers), request.client.host if request.client else "")
    if not result.ok:
        raise HTTPException(status_code=401, detail=result.reason or "未通过认证")


def _ascii_header(value) -> str:
    """HTTP 头只能装 latin-1；节点名可能是中文，转义后再放进头里。"""
    text = str(value)
    return text if text.isascii() else quote(text, safe="")


def _route_headers(route: Route) -> dict:
    return {
        "X-Aether-Route": _ascii_header(route.kind),
        "X-Aether-Node": _ascii_header(route.node_name),
        "X-Aether-Model": _ascii_header(route.model_id),
        "X-Aether-Score": f"{route.score:.2f}",
    }


def extract_usage(tail: bytes) -> dict:
    """兼容旧调用点：SSE 尾部提取 usage。"""
    return from_sse(tail)


def _meter(
    route: Route,
    req_id: str,
    usage: dict,
    *,
    streaming: bool,
    receipt_token: str | None = None,
    status: str = "ok",
) -> dict:
    state = get_state()
    in_tok = int((usage or {}).get("prompt_tokens") or 0)
    out_tok = int((usage or {}).get("completion_tokens") or 0)
    estimated = not usage
    price = route.price or {}
    price_in = float(price.get("in") or 0.0)
    price_out = float(price.get("out") or 0.0)
    if route.kind == "local":
        role, peer = "serve", None
    else:
        role, peer = "consume", route.node_id
    entry = state.ledger.record(
        role=role,
        self_node=get_identity().pubkey,
        peer_node=peer,
        model=route.model_id,
        in_tok=in_tok,
        out_tok=out_tok,
        price_in=price_in,
        price_out=price_out,
        source=route.kind,
        estimated=estimated,
        receipt=receipt_token,
        req_id=req_id,
        status=status,
    )
    entry["streaming"] = streaming
    bus.publish("ledger.entry", entry)
    return entry


@router.get("/v1/models")
async def list_models(request: Request) -> dict:
    _check_key(request)
    state = get_state()
    now = int(time.time())
    catalog = state.p2p.router.catalog()
    data = [
        {
            "id": m["id"],
            "object": "model",
            "created": now,
            "owned_by": "aether-local" if m.get("kind") == "local" else "aether-remote",
        }
        for m in catalog
    ]
    if not data:
        data = [{"id": "auto", "object": "model", "created": now, "owned_by": "aether"}]
    return {"object": "list", "data": data}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _check_key(request)
    payload = await request.json()
    state = get_state()
    model = str(payload.get("model") or "")
    candidates = state.p2p.router.candidates(model)
    if not candidates:
        raise HTTPException(
            status_code=503,
            detail="没有可用模型：本机未加载，网络内也没有节点广播该模型",
        )

    req_id = uuid.uuid4().hex
    stream = bool(payload.get("stream"))

    if not _enter():
        raise HTTPException(status_code=429, detail="本节点并发已满，请稍后重试或换节点")

    if stream:
        route = candidates[0]
        state.p2p.router.record(route)
        body = dict(payload)
        opts = dict(body.get("stream_options") or {})
        opts["include_usage"] = True
        body["stream_options"] = opts
        # 流式拿不到预先的 usage，收据在流结束后也无法回填响应头
        receipt_token = None
        tunnel = state.p2p.tunnel

        async def gen():
            tail = bytearray()
            try:
                source = (
                    upstream.chat_via_tunnel(tunnel, route.node_id, body, req_id)
                    if route.kind == "relay"
                    else upstream.chat_stream(
                        route.base_url, body, (route.extra or {}).get("api_key")
                    )
                )
                async for chunk in source:
                    tail.extend(chunk)
                    if len(tail) > TAIL_BYTES:
                        del tail[: len(tail) - TAIL_BYTES]
                    yield chunk
            except Exception as exc:
                yield upstream.sse_error(str(exc)[:200])
            finally:
                usage = extract_usage(bytes(tail))
                _meter(route, req_id, usage, streaming=True, receipt_token=receipt_token)
                _leave()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={**_route_headers(route), "X-Aether-Req": req_id},
        )

    errors: list[str] = []
    try:
        for route in candidates:
            try:
                if route.kind == "relay":
                    data = await upstream.chat_once_via_tunnel(
                        state.p2p.tunnel, route.node_id, payload, req_id
                    )
                else:
                    data = await upstream.chat(
                        route.base_url, payload, (route.extra or {}).get("api_key")
                    )
            except upstream.UpstreamError as exc:
                errors.append(f"{route.node_name}({route.kind}): {exc}")
                continue

            state.p2p.router.record(route)
            usage = (data or {}).get("usage") or {}
            receipt_token = None
            if route.kind != "local" and usage:
                # 远端/隧道请求让提供方签收据，作为结算争议时的对手方自述
                token = receipts.build(
                    get_identity(),
                    req_id=req_id,
                    client=route.node_id,
                    model=route.model_id,
                    in_tok=int(usage.get("prompt_tokens") or 0),
                    out_tok=int(usage.get("completion_tokens") or 0),
                    price_in=float((route.price or {}).get("in") or 0),
                    price_out=float((route.price or {}).get("out") or 0),
                    source=route.kind,
                )
                receipt_token = receipts.encode(token)

            _meter(route, req_id, usage, streaming=False, receipt_token=receipt_token)
            # 抽样复算：只对跨节点非流式请求，后台跑，不阻塞本次响应
            if route.kind != "local" and audit.should_sample():
                asyncio.create_task(
                    audit.run_audit(state.p2p.router, state.p2p.tunnel, route, payload, data, req_id),
                    name=f"audit:{req_id[:8]}",
                )
            headers = {**_route_headers(route), "X-Aether-Req": req_id}
            if receipt_token:
                headers["X-Aether-Receipt"] = receipt_token
            return JSONResponse(content=data, headers=headers)
    finally:
        _leave()

    _meter(candidates[0], req_id, {}, streaming=False, status="error")
    raise HTTPException(status_code=502, detail="所有候选节点均失败：" + " | ".join(errors[-3:]))


@router.post("/v1/completions")
async def completions(request: Request):
    _check_key(request)
    payload = await request.json()
    state = get_state()
    model = str(payload.get("model") or "")
    candidates = state.p2p.router.candidates(model)
    if not candidates:
        raise HTTPException(status_code=503, detail="没有可用模型")
    route = candidates[0]
    if route.kind == "relay":
        raise HTTPException(status_code=501, detail="completions 暂不支持经隧道转发")
    state.p2p.router.record(route)

    import httpx

    if payload.get("stream"):
        async def gen():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{route.base_url}/v1/completions", json=payload
                ) as resp:
                    async for chunk in resp.aiter_raw():
                        yield chunk

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers=_route_headers(route)
        )

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(f"{route.base_url}/v1/completions", json=payload)
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=resp.text[:300])
        data = resp.json()
    usage = data.get("usage") or {}
    _meter(route, uuid.uuid4().hex, usage, streaming=False)
    return JSONResponse(content=data, headers=_route_headers(route))


@router.post("/api/route/preview")
async def route_preview(payload: dict) -> dict:
    state = get_state()
    model = str(payload.get("model") or "")
    candidates = state.p2p.router.candidates(model)
    return {
        "model": model,
        "candidates": [c.to_dict() for c in candidates],
        "chosen": candidates[0].to_dict() if candidates else None,
    }
