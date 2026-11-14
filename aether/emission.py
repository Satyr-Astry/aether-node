"""贡献度与代币发放计划（链下）。

把两类贡献换算成统一的 points：

- **推理服务** —— 账本里 `role=serve` 的 token 当量，每个 token 记 1 点
- **中继带宽** —— relay 转发的字节，每 MB 记 500 点

带宽权重远高于算力，是因为它更稀缺也更难替代：算力可以闲时白嫖，
NAT 后的节点却没有别的方式获得可直达的入口。这也是为什么代币激励
应当从带宽起步——它比算力容易验证得多（字节数不可伪造，而推理是否
真的跑了某个模型，抽样复算也只能抓明显作弊）。

排放按天衰减，每个 epoch 产出一份发放清单与 Merkle root。
链上只需要存这个 root，叶子明细留在链下供争议举证。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from aether.config import get_settings
from aether.identity import get_identity
from aether.ledger import get_ledger

EPOCH_SECONDS = 3600
BASE_EMISSION = 10000.0     # 第一个 epoch 的总发放量
DAILY_DECAY = 0.95          # 每过一天，排放乘以这个系数

WEIGHT_SERVE_TOKEN = 1.0
WEIGHT_RELAY_MB = 500.0


def epoch_of(ts: float | None = None) -> int:
    return int((ts if ts is not None else time.time()) // EPOCH_SECONDS)


def epoch_window(epoch: int) -> tuple[float, float]:
    start = epoch * EPOCH_SECONDS
    return start, start + EPOCH_SECONDS


def _genesis() -> int:
    """网络创世 epoch：首次计算时落盘，之后衰减以它为基准。

    不能拿绝对 epoch 做指数底数——epoch 从 1970 起算，0.95^n 会立刻下溢成 0。
    """
    path = get_settings().home / "emissions" / ".genesis"
    if path.exists():
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = epoch_of()
    try:
        path.write_text(str(stamp), encoding="utf-8")
    except Exception:
        pass
    return stamp


def emission_of(epoch: int) -> float:
    days = max(0.0, (epoch - _genesis()) / 24.0)
    return round(BASE_EMISSION * (DAILY_DECAY ** days), 6)


def _leaf(node: str, points: float, amount: float) -> str:
    raw = f"{node}|{points:.6f}|{amount:.9f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pair(a: str, b: str) -> str:
    lo, hi = (a, b) if a < b else (b, a)
    return hashlib.sha256(bytes.fromhex(lo) + bytes.fromhex(hi)).hexdigest()


def _merkle(leaves: list[str]) -> str:
    if not leaves:
        return hashlib.sha256(b"aether-emission-empty").hexdigest()
    layer = sorted(leaves)
    while len(layer) > 1:
        nxt = []
        for i in range(0, len(layer), 2):
            a = layer[i]
            b = layer[i + 1] if i + 1 < len(layer) else a
            nxt.append(_pair(a, b))
        layer = nxt
    return layer[0]


def compute(epoch: int | None = None, relay_provider: dict | None = None) -> dict:
    """计算某个 epoch 的贡献度与发放清单。

    relay_provider 形如 {"node": <pubkey>, "bytes": <本 relay 承载的总字节>}。
    带宽贡献归 relay 提供者，而不是按"谁的流量"分摊——付出中转成本的是
    relay 本身，被中转的双方只是在消费这项服务。
    """
    ledger = get_ledger()
    self_pub = get_identity().pubkey
    target_epoch = epoch if epoch is not None else epoch_of()
    start, end = epoch_window(target_epoch)

    contributions: dict[str, dict] = {}

    def acc(node: str, field: str, value: float) -> None:
        if not node:
            return
        rec = contributions.setdefault(
            node, {"serve_tokens": 0.0, "relay_bytes": 0.0, "served_requests": 0, "relayed_messages": 0}
        )
        rec[field] = rec.get(field, 0) + value

    # 推理贡献：本节点 serve 出去的 token（别人调用我）
    for entry in ledger.entries(limit=5000):
        if entry["role"] != "serve":
            continue
        if not (start <= float(entry["ts"]) < end):
            continue
        acc(self_pub, "serve_tokens", entry["in_tok"] + entry["out_tok"])
        acc(self_pub, "served_requests", 1)

    # 中继贡献：relay 提供者按承载的总字节计
    if relay_provider:
        node = str(relay_provider.get("node") or "")
        acc(node, "relay_bytes", float(relay_provider.get("bytes") or 0))
        acc(node, "relayed_messages", float(relay_provider.get("messages") or 0))

    rows: list[dict] = []
    total_points = 0.0
    for node, c in contributions.items():
        points = (
            c["serve_tokens"] * WEIGHT_SERVE_TOKEN
            + (c["relay_bytes"] / (1 << 20)) * WEIGHT_RELAY_MB
        )
        if points <= 0:
            continue
        rows.append({
            "node": node,
            "points": round(points, 6),
            "serve_tokens": int(c["serve_tokens"]),
            "relay_bytes": int(c["relay_bytes"]),
            "served_requests": c["served_requests"],
            "relayed_messages": c["relayed_messages"],
        })
        total_points += points

    emission = emission_of(target_epoch)
    allocations: list[dict] = []
    leaves: list[str] = []
    for row in sorted(rows, key=lambda r: -r["points"]):
        share = row["points"] / total_points if total_points > 0 else 0.0
        amount = round(emission * share, 9)
        leaf = _leaf(row["node"], row["points"], amount)
        leaves.append(leaf)
        allocations.append({**row, "share": round(share, 6), "amount": amount, "leaf": leaf})

    plan = {
        "v": 1,
        "epoch": target_epoch,
        "window": {"start": start, "end": end},
        "emission": emission,
        "total_points": round(total_points, 6),
        "weights": {"serve_token": WEIGHT_SERVE_TOKEN, "relay_mb": WEIGHT_RELAY_MB},
        "merkle_root": _merkle(leaves),
        "allocations": allocations,
        "generated_at": time.time(),
    }
    return plan


def save(plan: dict) -> Path:
    path = get_settings().home / "emissions" / f"{plan['epoch']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def list_plans(limit: int = 24) -> list[dict]:
    root = get_settings().home / "emissions"
    if not root.exists():
        return []
    files = sorted(root.glob("*.json"), key=lambda p: p.stem, reverse=True)[:limit]
    out: list[dict] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "epoch": data.get("epoch"),
            "emission": data.get("emission"),
            "total_points": data.get("total_points"),
            "merkle_root": data.get("merkle_root"),
            "allocations": len(data.get("allocations") or []),
            "path": str(f),
        })
    return out


def verify(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    leaves = [a.get("leaf") or "" for a in data.get("allocations") or []]
    recomputed = _merkle(leaves)
    bad = []
    for a in data.get("allocations") or []:
        if _leaf(a["node"], float(a["points"]), float(a["amount"])) != a.get("leaf"):
            bad.append(a["node"])
    return {
        "ok": recomputed == data.get("merkle_root") and not bad,
        "expected_root": data.get("merkle_root"),
        "recomputed_root": recomputed,
        "allocations": len(leaves),
        "bad_nodes": bad[:10],
    }
