"""代币发放验证：叶子定义、Merkle 证明、setRoot / claim 的 calldata 编码。

叶子必须与 AetherDistributor 合约严格一致：
    sha256(address(20 字节) || amount(uint256 大端))
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-payout-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether import payout  # noqa: E402
from aether.evm import abi  # noqa: E402
from aether.evm.keccak import keccak256  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


# ---------------- 叶子定义 ----------------
ADDR = "0x9f8e09464c76e4fb8d0466cf443319551b3527a3"
AMOUNT = 1_500_000_000_000_000_000  # 1.5 AETH

# 手工按合约定义算一遍
raw = bytes.fromhex(ADDR[2:]) + AMOUNT.to_bytes(32, "big")
manual = hashlib.sha256(raw).hexdigest()
check("leaf matches contract definition", payout.leaf_of(ADDR, AMOUNT), manual)
check("leaf is 32 bytes", len(payout.leaf_of(ADDR, AMOUNT)), 64)

# 金额变则叶子变
check("amount change changes leaf",
      payout.leaf_of(ADDR, AMOUNT) != payout.leaf_of(ADDR, AMOUNT + 1), True)

# 地址变则叶子变
check("address change changes leaf",
      payout.leaf_of(ADDR, AMOUNT) != payout.leaf_of("0x" + "11" * 20, AMOUNT), True)

# ---------------- 构造发放清单 ----------------
plan = {
    "epoch": 498515,
    "emission": 10000.0,
    "total_points": 3500.0,
    "allocations": [
        {"node": "nodeA" + "0" * 59, "points": 3000.0, "amount": 8571.428571429},
        {"node": "nodeB" + "0" * 59, "points": 500.0, "amount": 1428.571428571},
        {"node": "nodeC" + "0" * 59, "points": 0.0, "amount": 0.0},
    ],
}
addr_of = {
    "nodeA" + "0" * 59: ADDR,
    "nodeB" + "0" * 59: "0x" + "22" * 20,
    # nodeC 故意不给地址，验证跳过而不是静默丢账
}

doc = payout.build(plan, addr_of)
check("two claims built", len(doc["claims"]), 2)
check("skipped recorded", len(doc["skipped"]), 1)
check("skipped reason mentions address", "EVM" in doc["skipped"][0]["reason"], True)
check("total is sum of claims",
      doc["total_wei"], sum(c["amount_wei"] for c in doc["claims"]))

# ---------------- 证明验证 ----------------
for c in doc["claims"]:
    ok = payout.verify_claim(doc["root"], c["leaf"], c["proof"])
    check(f"proof verifies for {c['evm'][:10]}", ok, True)

# 篡改金额后叶子不符 -> 证明失败
tampered = payout.leaf_of(doc["claims"][0]["evm"], doc["claims"][0]["amount_wei"] + 1)
check("tampered amount fails proof",
      payout.verify_claim(doc["root"], tampered, doc["claims"][0]["proof"]), False)

# 换别人的证明也不行
if len(doc["claims"]) > 1:
    check("cross-claim proof rejected",
          payout.verify_claim(doc["root"], doc["claims"][0]["leaf"], doc["claims"][1]["proof"]),
          len(doc["claims"]) == 1)

# ---------------- calldata 编码 ----------------
set_root = bytes.fromhex(doc["set_root_calldata"][2:])
check("setRoot selector", set_root[:4].hex(),
      keccak256(b"setRoot(uint256,bytes32,uint256)").hex()[:8])
check("setRoot length", len(set_root), 4 + 32 * 3)
check("setRoot epoch word", int.from_bytes(set_root[4:36], "big"), 498515)
check("setRoot root word", set_root[36:68].hex(), doc["root"])
check("setRoot total word", int.from_bytes(set_root[68:100], "big"), doc["total_wei"])

claim_hex = payout.claim_calldata(498515, doc["claims"][0]["amount_wei"], doc["claims"][0]["proof"])
claim = bytes.fromhex(claim_hex[2:])
check("claim selector", claim[:4].hex(), keccak256(b"claim(uint256,uint256,bytes32[])").hex()[:8])
check("claim epoch word", int.from_bytes(claim[4:36], "big"), 498515)
check("claim amount word", int.from_bytes(claim[36:68], "big"), doc["claims"][0]["amount_wei"])
check("claim array offset", int.from_bytes(claim[68:100], "big"), 96)  # 3 * 32
check("claim proof length", int.from_bytes(claim[100:132], "big"), len(doc["claims"][0]["proof"]))
check("claim total length",
      len(claim), 4 + 32 * 3 + 32 + 32 * len(doc["claims"][0]["proof"]))

# proof 元素逐个核对
proof = doc["claims"][0]["proof"]
offset = 132
for i, sibling in enumerate(proof):
    seg = claim[offset + i * 32 : offset + (i + 1) * 32]
    if seg.hex() != sibling:
        fails.append(f"claim proof element {i}")
        print(f"[FAIL] claim proof element {i}")
        break
else:
    print("[PASS] claim proof elements")

# ---------------- 空清单与边界 ----------------
empty = payout.build({"epoch": 1, "allocations": []}, {})
check("empty plan root is deterministic", len(empty["root"]), 64)
check("empty plan has no claims", len(empty["claims"]), 0)

# 单节点：proof 应为空数组但 root 仍然是该叶子的哈希
single = payout.build(
    {"epoch": 2, "allocations": [{"node": "n1", "points": 1.0, "amount": 1.0}]},
    {"n1": ADDR},
)
check("single claim proof empty", single["claims"][0]["proof"], [])
check("single claim verify", payout.verify_claim(
    single["root"], single["claims"][0]["leaf"], []), True)
check("single claim root equals leaf", single["root"], single["claims"][0]["leaf"])

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all payout checks passed")
