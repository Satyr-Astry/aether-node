"""token 计量的共用工具。

llama.cpp 在 `stream=true` 时默认不返回 usage，需要请求方带
`stream_options.include_usage`；这里负责把 usage 从完整响应或 SSE 尾部捞出来。
两边（代理层与隧道目标端）共用同一套解析，避免各写一份导致口径不一致。
"""

from __future__ import annotations

import json

TAIL_BYTES = 16384


def from_json(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {}
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else {}


def from_sse(tail: bytes | str) -> dict:
    text = tail.decode("utf-8", "replace") if isinstance(tail, bytes) else tail
    last: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            last = obj["usage"]
    return last


def totals(usage: dict) -> tuple[int, int]:
    return (
        int((usage or {}).get("prompt_tokens") or 0),
        int((usage or {}).get("completion_tokens") or 0),
    )
