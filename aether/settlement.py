"""结算批次：把未结算记录聚合成批次、算 Merkle root、导出可离线校验的凭证。

本轮不生成链上交易，只产出「一份带根的账单文件 + 每个叶子的证明」，
这样后续接 x402 / 状态通道 / 乐观结算时，链上只需要存这个 root。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from aether.config import get_settings
from aether.ledger import get_ledger

VERSION = 1


def _canonical(entry: dict) -> str:
    """叶子的规范化串。字段顺序与取值口径必须固定，否则 root 无法复现。"""
    return "|".join([
        str(entry["req_id"]),
        str(entry.get("peer_node") or ""),
        str(entry["model"]),
        str(int(entry["in_tok"])),
        str(int(entry["out_tok"])),
        f"{float(entry['amount']):.9f}",
    ])


def leaf_hash(entry: dict) -> str:
    return hashlib.sha256(_canonical(entry).encode("utf-8")).hexdigest()


def _pair(a: str, b: str) -> str:
    """内部节点哈希。每对先按字典序排序再拼接——构建与验证必须用同一规则，
    否则同一片叶子的证明会时而成立时而不成立。"""
    lo, hi = (a, b) if a < b else (b, a)
    return hashlib.sha256(bytes.fromhex(lo) + bytes.fromhex(hi)).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return hashlib.sha256(b"aether-empty").hexdigest()
    layer = sorted(leaves)  # 排序保证同一集合的 root 与顺序无关
    while len(layer) > 1:
        nxt: list[str] = []
        for i in range(0, len(layer), 2):
            a = layer[i]
            b = layer[i + 1] if i + 1 < len(layer) else a
            nxt.append(_pair(a, b))
        layer = nxt
    return layer[0]


def merkle_proof(leaves: list[str], target: str) -> list[str]:
    """生成某片叶子的兄弟路径。合约里的 verifyLeaf 可直接消费。"""
    layer = sorted(leaves)
    if target not in layer:
        raise ValueError("叶子不在集合中")
    idx = layer.index(target)
    proof: list[str] = []
    while len(layer) > 1:
        sibling_idx = idx + 1 if idx % 2 == 0 else idx - 1
        if sibling_idx >= len(layer):
            sibling_idx = idx  # 奇数层最后一个节点与自己配对
        proof.append(layer[sibling_idx])
        nxt: list[str] = []
        for i in range(0, len(layer), 2):
            a = layer[i]
            b = layer[i + 1] if i + 1 < len(layer) else a
            nxt.append(_pair(a, b))
        layer = nxt
        idx //= 2
    return proof


def verify_proof(root: str, leaf: str, proof: list[str]) -> bool:
    """与合约 verifyLeaf 完全相同的验证逻辑（本地等价实现）。"""
    h = leaf
    for sibling in proof:
        h = _pair(h, sibling)
    return h == root


def settle(role: str | None = None) -> dict:
    ledger = get_ledger()
    entries = ledger.unsettled()
    if role:
        entries = [e for e in entries if e["role"] == role]
    if not entries:
        return {"created": False, "reason": "没有待结算记录"}

    leaves = [leaf_hash(e) for e in entries]
    root = merkle_root(leaves)
    batch_id = uuid.uuid4().hex[:16]
    stamp = time.time()
    total_in = sum(int(e["in_tok"]) for e in entries)
    total_out = sum(int(e["out_tok"]) for e in entries)
    amount = round(sum(float(e["amount"]) for e in entries), 9)

    path = get_settings().home / "settlements" / f"{batch_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    proof = {
        "v": VERSION,
        "batch_id": batch_id,
        "created_at": stamp,
        "merkle_root": root,
        "count": len(entries),
        "totals": {"in": total_in, "out": total_out, "amount": amount},
        "entries": [
            {
                "req_id": e["req_id"],
                "role": e["role"],
                "peer": e["peer_node"],
                "model": e["model"],
                "in": e["in_tok"],
                "out": e["out_tok"],
                "amount": e["amount"],
                "estimated": e["estimated"],
                "receipt": e["receipt"],
                "ts": e["ts"],
                "leaf": leaf,
                "proof": merkle_proof(leaves, leaf),
            }
            for e, leaf in zip(entries, leaves, strict=True)
        ],
    }
    path.write_text(json.dumps(proof, indent=2, ensure_ascii=False), encoding="utf-8")

    batch = {
        "id": batch_id,
        "ts": stamp,
        "from_ts": min(float(e["ts"]) for e in entries),
        "to_ts": max(float(e["ts"]) for e in entries),
        "count": len(entries),
        "total_in": total_in,
        "total_out": total_out,
        "amount": amount,
        "merkle_root": root,
        "path": str(path),
    }
    ledger.save_batch(batch)
    ledger.attach_batch([e["id"] for e in entries], batch_id)
    return {"created": True, "batch": batch}


def verify_file(path: str | Path) -> dict:
    """独立校验一份结算凭证：重算每片叶子与整棵 Merkle root。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    bad: list[str] = []
    leaves: list[str] = []
    for e in entries:
        expected = leaf_hash({
            "req_id": e.get("req_id"),
            "peer_node": e.get("peer"),
            "model": e.get("model"),
            "in_tok": e.get("in"),
            "out_tok": e.get("out"),
            "amount": e.get("amount"),
        })
        leaves.append(e.get("leaf") or "")
        if expected != e.get("leaf"):
            bad.append(str(e.get("req_id")))
    recomputed = merkle_root(leaves)
    return {
        "ok": recomputed == data.get("merkle_root") and not bad,
        "expected_root": data.get("merkle_root"),
        "recomputed_root": recomputed,
        "entry_count": len(entries),
        "bad_leaves": bad[:10],
    }
