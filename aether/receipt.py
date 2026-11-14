"""收据：提供方对一次服务签发的凭据。

用途是让调用方事后能证明「对方承认服务过这一次、承认这个 token 数」，
从而在结算争议里有一份带签名的对手方自述。

已知限制：流式请求在响应头发出时还不知道 token 数，因此流式不带收据，
只能双方各记一笔、按 req_id 事后对账。字段里用 receipt=None 明确标记。
"""

from __future__ import annotations

import base64
import json
import time

from aether.identity import Identity

VERSION = 1


def build(
    identity: Identity,
    *,
    req_id: str,
    client: str,
    model: str,
    in_tok: int,
    out_tok: int,
    price_in: float = 0.0,
    price_out: float = 0.0,
    source: str = "local",
) -> dict:
    body = {
        "v": VERSION,
        "req_id": req_id,
        "provider": identity.pubkey,
        "client": client,
        "model": model,
        "in": int(in_tok),
        "out": int(out_tok),
        "price_in": float(price_in),
        "price_out": float(price_out),
        "source": source,
        "ts": int(time.time()),
    }
    return identity.sign_json(body)


def verify(pubkey: str, receipt: dict) -> bool:
    if not isinstance(receipt, dict):
        return False
    if receipt.get("v") != VERSION:
        return False
    if receipt.get("provider") != pubkey:
        return False
    return Identity.verify_json(pubkey, receipt)


def encode(receipt: dict) -> str:
    raw = json.dumps(receipt, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode(token: str) -> dict | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None
