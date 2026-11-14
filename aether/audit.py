"""抽样复算：让另一个提供同模型的节点重跑同一请求，比对输出。

账本对账只能发现「两边记录不一致」，审计才能发现「声称跑 70B 实际跑 7B」
这类作弊。这是给自述式计量补上一点实际约束力的最小手段。

判定分层的原因：逐字比对太严格。同一 GGUF 在不同硬件、不同后端（CPU/CUDA）、
不同 batch 大小下，temperature=0 的输出也可能有细微差异，所以退一步允许
「token 数一致 + 文本相似度达阈值」通过。相似度明显低于阈值才判可疑。

需要 temperature=0 与固定 seed，否则输出本身是随机的，比对没有意义。
"""

from __future__ import annotations

import difflib
import random
import time
import uuid

from aether.config import get_settings
from aether.events import bus
from aether.ledger import get_ledger
from aether.p2p import upstream

DETAIL_LIMIT = 120


def should_sample() -> bool:
    rate = float(get_settings().audit_rate or 0.0)
    return rate > 0 and random.random() < rate


def content(data: dict | None) -> str:
    try:
        return str((data or {})["choices"][0]["message"]["content"] or "")
    except Exception:
        return ""


_content = content  # 内部旧名保留


def _usage(data: dict | None) -> tuple[int, int]:
    u = (data or {}).get("usage") or {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def normalize_payload(payload: dict, seed: int) -> dict:
    """把请求变成可复现的形式：非流式、temperature=0、固定 seed。"""
    body = dict(payload)
    body["stream"] = False
    body["temperature"] = 0.0
    body["seed"] = int(seed)
    body.pop("stream_options", None)
    return body


def record(
    *,
    req_id: str,
    model: str,
    target_node: str,
    verifier: str,
    original: dict | None,
    verified: dict | None,
    verdict: str,
    detail: str = "",
    similarity_override: float | None = None,
) -> dict:
    settings = get_settings()
    o_in, o_out = _usage(original)
    v_in, v_out = _usage(verified)
    orig_text = _content(original)
    new_text = _content(verified)

    if similarity_override is not None:
        sim = similarity_override
    else:
        sim = similarity(orig_text, new_text)

    row = {
        "id": uuid.uuid4().hex,
        "req_id": req_id,
        "ts": time.time(),
        "model": model,
        "target_node": target_node,
        "verifier": verifier or "",
        "original_in": o_in,
        "original_out": o_out,
        "verified_in": v_in,
        "verified_out": v_out,
        "similarity": sim,
        "verdict": verdict,
        "detail": detail[:1000],
    }
    if not detail and original is not None and verified is not None:
        row["detail"] = (
            f"原: {orig_text[:DETAIL_LIMIT]!r} | 复算: {new_text[:DETAIL_LIMIT]!r}"
        )
    try:
        get_ledger().add_audit(row)
    except Exception as exc:
        bus.publish("audit.error", {"msg": str(exc)[:200]})
    bus.publish("audit.result", {
        "req_id": req_id, "verdict": verdict, "similarity": sim,
        "target_node": target_node, "model": model,
    })
    return row


def judge(original: dict | None, verified: dict | None) -> tuple[str, float]:
    settings = get_settings()
    orig_text = _content(original)
    new_text = _content(verified)
    sim = similarity(orig_text, new_text)
    o_in, o_out = _usage(original)
    v_in, v_out = _usage(verified)
    same_tokens = (o_in, o_out) == (v_in, v_out)

    if not orig_text or not new_text:
        # 两侧都空视为一致；只有一侧空说明复算失败
        return ("pass" if orig_text == new_text else "suspect"), sim

    if orig_text == new_text:
        # 文本逐字相同但 token 数不同，说明分词器或模型版本不一致，仍要警惕
        return ("pass" if same_tokens else "suspect"), sim

    if same_tokens and sim >= float(settings.audit_threshold):
        return "pass", sim
    return "suspect", sim


async def run_audit(router, tunnel, route, payload: dict, original: dict, req_id: str) -> dict:
    """对一次已完成的跨节点请求做复算审计。"""
    settings = get_settings()
    others = [c for c in router.candidates(route.model_id) if c.node_id != route.node_id]
    if not others:
        return record(
            req_id=req_id, model=route.model_id, target_node=route.node_id, verifier="",
            original=original, verified=None, verdict="skipped",
            detail="网络内没有第二个提供同模型的节点，无法复算",
        )

    verifier = others[0]
    body = normalize_payload(payload, settings.audit_seed)
    try:
        if verifier.kind == "relay":
            data = await upstream.chat_once_via_tunnel(
                tunnel, verifier.node_id, body, uuid.uuid4().hex
            )
        else:
            data = await upstream.chat(verifier.base_url, body)
    except Exception as exc:
        return record(
            req_id=req_id, model=route.model_id, target_node=route.node_id,
            verifier=verifier.node_id, original=original, verified=None,
            verdict="error", detail=f"复算请求失败: {exc}"[:300],
        )

    verdict, sim = judge(original, data)
    return record(
        req_id=req_id, model=route.model_id, target_node=route.node_id,
        verifier=verifier.node_id, original=original, verified=data,
        verdict=verdict, similarity_override=sim,
    )
