"""代币发放：把贡献清单转成链上可领取的叶子与证明。

叶子定义与 AetherDistributor 合约严格一致：

    leaf = sha256( address(20 字节) || amount(uint256 大端) )

刻意不含点数与 epoch——点数是链下记账口径，浮点在链上没有表示，一旦进叶子就无法
用合约重算。链上只认「这个地址这一期能领多少」，争议时拿链下清单举证。

EVM 地址必须由节点自己公布：节点 ID 是 x-only 公钥（32 字节，缺 y），
从它推不出唯一的 EVM 地址（y 有奇偶两种可能）。所以广告里带 `evm` 字段。
"""

from __future__ import annotations

import hashlib

from aether import settlement
from aether.evm import abi

TOKEN_DECIMALS = 18
WEI = 10 ** TOKEN_DECIMALS


def leaf_of(evm_address: str, amount_wei: int) -> str:
    raw = bytes.fromhex(evm_address[2:] if evm_address.startswith("0x") else evm_address)
    if len(raw) != 20:
        raise ValueError(f"EVM 地址应为 20 字节，实际 {len(raw)}")
    if amount_wei < 0:
        raise ValueError("金额不能为负")
    return hashlib.sha256(raw + amount_wei.to_bytes(32, "big")).hexdigest()


def build(plan: dict, addr_of: dict[str, str]) -> dict:
    """plan 来自 emission.compute；addr_of 是 {node_id: evm_address}。

    缺少 EVM 地址的节点会被跳过并记录原因，不会静默丢账。
    """
    claims: list[dict] = []
    leaves: list[str] = []
    skipped: list[dict] = []

    for alloc in plan.get("allocations") or []:
        node = str(alloc.get("node") or "")
        evm = addr_of.get(node) or ""
        if not evm:
            skipped.append({"node": node, "reason": "该节点未在广告里公布 EVM 地址"})
            continue
        amount_wei = int(round(float(alloc.get("amount") or 0) * WEI))
        if amount_wei <= 0:
            skipped.append({"node": node, "reason": "本期分配额为零"})
            continue
        leaf = leaf_of(evm, amount_wei)
        claims.append({
            "node": node,
            "evm": evm,
            "amount_wei": amount_wei,
            "points": alloc.get("points"),
            "leaf": leaf,
        })
        leaves.append(leaf)

    root = settlement.merkle_root(leaves)
    total_wei = sum(c["amount_wei"] for c in claims)
    for claim in claims:
        claim["proof"] = settlement.merkle_proof(leaves, claim["leaf"])

    return {
        "epoch": plan.get("epoch"),
        "root": root,
        "total_wei": total_wei,
        "count": len(claims),
        "claims": claims,
        "skipped": skipped,
        "set_root_calldata": "0x" + abi.encode_set_root(
            int(plan.get("epoch") or 0), root, total_wei
        ).hex(),
    }


def claim_calldata(epoch: int, amount_wei: int, proof: list[str]) -> str:
    return "0x" + abi.encode_claim(epoch, amount_wei, proof).hex()


def verify_claim(root: str, leaf: str, proof: list[str]) -> bool:
    """与合约 verifyLeaf 等价的本地校验。"""
    return settlement.verify_proof(root, leaf, proof)
