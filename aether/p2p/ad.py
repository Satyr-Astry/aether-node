"""服务广告：构建、签名、验证。

双层签名：
  外层 —— Nostr 事件签名（对事件 id 做 Schnorr），让任意标准 relay 都能接收与转发；
  内层 —— 对广告正文的规范化 JSON 签名，使广告在离开 relay 后仍可独立验真。
"""

from __future__ import annotations

import hashlib
import json
import time

from aether import secp256k1 as sk
from aether.identity import Identity

KIND_AD = 30333
TOPIC = "aether"
PROTOCOL_VERSION = 1
DEFAULT_TTL = 900  # 广告有效期（秒）


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_id(pubkey: str, created_at: int, kind: int, tags: list, content: str) -> str:
    serialized = json.dumps(
        [0, pubkey, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_ad(
    identity: Identity,
    *,
    name: str,
    endpoint: str,
    reachability: str,
    models: list[dict],
    price: dict | None = None,
    region: str = "",
    ttl: int = DEFAULT_TTL,
    extra: dict | None = None,
) -> dict:
    """构造并签名广告正文。"""
    ad = {
        "v": PROTOCOL_VERSION,
        "proto": "aether.service",
        "node_id": identity.pubkey,
        "name": name,
        "endpoint": endpoint,
        "reachability": reachability,
        "models": models,
        "price": price or {"in": 0.0, "out": 0.0, "unit": "USD/1M"},
        "region": region,
        "ts": int(time.time()),
        "ttl": ttl,
        "npub": identity.npub,
    }
    if extra:
        ad.update(extra)
    return identity.sign_json(ad)


def build_event(identity: Identity, ad: dict) -> dict:
    """把广告正文包成 Nostr 可替换事件（kind 30333，d tag = node_id）。"""
    content = json.dumps(ad, separators=(",", ":"), ensure_ascii=False)
    tags = [["t", TOPIC], ["d", identity.pubkey]]
    created_at = int(time.time())
    evt = {
        "pubkey": identity.pubkey,
        "created_at": created_at,
        "kind": KIND_AD,
        "tags": tags,
        "content": content,
    }
    eid = event_id(identity.pubkey, created_at, KIND_AD, tags, content)
    evt["id"] = eid
    evt["sig"] = identity.sign_digest(bytes.fromhex(eid)).hex()
    return evt


def verify_event(evt: dict) -> str | None:
    """校验事件完整性。返回 None 表示通过，否则返回错误原因。"""
    try:
        pubkey = evt["pubkey"]
        created_at = int(evt["created_at"])
        kind = int(evt["kind"])
        tags = evt["tags"]
        content = evt["content"]
        eid = evt["id"]
        sig = evt["sig"]
    except (KeyError, TypeError, ValueError):
        return "字段缺失或类型错误"
    if kind != KIND_AD:
        return "kind 不匹配"
    if len(pubkey) != 64 or len(sig) != 128:
        return "pubkey/sig 长度错误"
    expected = event_id(pubkey, created_at, kind, tags, content)
    if expected != eid:
        return "事件 id 不匹配"
    if not sk.schnorr_verify(bytes.fromhex(eid), bytes.fromhex(pubkey), bytes.fromhex(sig)):
        return "事件签名无效"
    return None


def parse_ad(evt: dict) -> tuple[dict | None, str | None]:
    """事件 → 广告正文，并验证内层签名与时效。"""
    err = verify_event(evt)
    if err:
        return None, err
    try:
        ad = json.loads(evt["content"])
    except Exception:
        return None, "content 不是合法 JSON"
    if not isinstance(ad, dict):
        return None, "content 不是对象"
    if ad.get("proto") != "aether.service":
        return None, "协议标识不匹配"
    if ad.get("node_id") != evt["pubkey"]:
        return None, "node_id 与事件发布者不一致"
    if not Identity.verify_json(evt["pubkey"], ad):
        return None, "广告内层签名无效"
    age = time.time() - int(ad.get("ts") or 0)
    ttl = int(ad.get("ttl") or DEFAULT_TTL)
    if age > ttl:
        return None, f"广告已过期（{int(age)}s > {ttl}s）"
    if age < -300:
        return None, "时间戳来自未来"
    return ad, None
