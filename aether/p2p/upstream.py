"""上游转发：把请求发到网络内其他节点，并原样透传（含 SSE 流式）。"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from aether.config import get_settings
from aether.net import tls_verify


class UpstreamError(RuntimeError):
    pass


def _client() -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(s.route_timeout, connect=10.0, read=None),
        follow_redirects=False,
        verify=tls_verify(),
    )


async def list_models(base_url: str) -> list[dict]:
    async with _client() as client:
        try:
            r = await client.get(f"{base_url.rstrip('/')}/v1/models")
            r.raise_for_status()
            data = r.json()
            return data.get("data") or []
        except Exception as exc:
            raise UpstreamError(f"{base_url} 不可达：{exc}") from exc


def _auth_headers(api_key: str | None) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def chat(base_url: str, payload: dict, api_key: str | None = None) -> dict:
    async with _client() as client:
        try:
            r = await client.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                json=payload,
                headers=_auth_headers(api_key),
            )
        except Exception as exc:
            raise UpstreamError(f"{base_url} 请求失败：{exc}") from exc
        if r.status_code >= 400:
            raise UpstreamError(f"上游返回 {r.status_code}：{r.text[:300]}")
        return r.json()


async def chat_stream(base_url: str, payload: dict, api_key: str | None = None) -> AsyncIterator[bytes]:
    """逐块转发 SSE，不做解析——保持上游的原始字节流与格式。"""
    body = dict(payload)
    body["stream"] = True
    async with _client() as client:
        try:
            async with client.stream(
                "POST",
                f"{base_url.rstrip('/')}/v1/chat/completions",
                json=body,
                headers=_auth_headers(api_key),
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise UpstreamError(f"上游返回 {resp.status_code}：{detail}")
                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise UpstreamError(f"{base_url} 流式转发中断：{exc}") from exc


async def chat_via_tunnel(tunnel, target: str, payload: dict, req_id: str | None = None) -> AsyncIterator[bytes]:
    """经 relay 隧道转发 chat 请求（流式）。目标节点内部仍走它自己的 llama-server。"""
    body = dict(payload)
    body["stream"] = True
    text = json.dumps(body, ensure_ascii=False)
    async for status, chunk, done in tunnel.request(
        target, "POST", "/v1/chat/completions", text, biz_id=req_id
    ):
        if status >= 400:
            yield sse_error(chunk[:300] or f"目标节点返回 {status}")
            return
        if chunk:
            yield chunk.encode("utf-8")
        if done:
            return


async def chat_once_via_tunnel(tunnel, target: str, payload: dict, req_id: str | None = None) -> dict:
    """经 relay 隧道转发 chat 请求（非流式）。"""
    text = json.dumps(payload, ensure_ascii=False)
    parts: list[str] = []
    status = 0
    async for st, chunk, done in tunnel.request(
        target, "POST", "/v1/chat/completions", text, biz_id=req_id
    ):
        status = st
        parts.append(chunk)
        if done:
            break
    raw = "".join(parts)
    if status >= 400:
        raise UpstreamError(f"隧道上游返回 {status}：{raw[:300]}")
    try:
        return json.loads(raw)
    except Exception as exc:
        raise UpstreamError(f"隧道响应不是合法 JSON：{raw[:200]}") from exc


async def health(base_url: str) -> bool:
    try:
        async with _client() as client:
            r = await client.get(f"{base_url.rstrip('/')}/v1/models", timeout=5.0)
            return r.status_code == 200
    except Exception:
        return False


def sse_error(message: str) -> bytes:
    """上游失败时也返回合法 SSE，避免客户端解析崩溃。"""
    chunk = {
        "id": "aether-error",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "error",
        "choices": [{"index": 0, "delta": {"content": f"[上游错误] {message}"}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
