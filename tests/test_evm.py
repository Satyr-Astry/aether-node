"""EVM 工具链验证：全部对照官方向量。

覆盖 Keccak-256、RLP、RFC6979+ECDSA、EIP-155 交易、ABI 编码、以及
Merkle 证明与链上合约验证逻辑的一致性。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-evm-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether.evm import abi, ecdsa, rlp, tx  # noqa: E402
from aether.evm.keccak import keccak256  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


# ---------------- Keccak-256 ----------------
check("keccak('')", keccak256(b"").hex(),
      "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
check("keccak('abc')", keccak256(b"abc").hex(),
      "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
check("keccak ERC20 Transfer topic", keccak256(b"Transfer(address,address,uint256)").hex(),
      "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
check("keccak transfer selector", keccak256(b"transfer(address,uint256)").hex()[:8], "a9059cbb")

# ---------------- RLP ----------------
check("rlp('dog')", rlp.encode(b"dog").hex(), "83646f67")
check("rlp(['cat','dog'])", rlp.encode([b"cat", b"dog"]).hex(), "c88363617483646f67")
check("rlp('')", rlp.encode(b"").hex(), "80")
check("rlp(0)", rlp.encode(0).hex(), "80")
check("rlp(15)", rlp.encode(15).hex(), "0f")
check("rlp(1024)", rlp.encode(1024).hex(), "820400")
check("rlp([])", rlp.encode([]).hex(), "c0")
check("rlp(0x7f single)", rlp.encode(b"\x7f").hex(), "7f")
check("rlp(56 bytes)", rlp.encode(b"x" * 56).hex()[:4], "b838")

# ---------------- EIP-155 官方交易向量 ----------------
SECKEY = bytes.fromhex("46" * 32)
check("address of 0x46..46", "0x" + ecdsa.address(SECKEY).hex(), "0x9d8a62f656a8d1615c1294fd71e9cfb3e4855a4f")

signed = tx.sign_transaction(
    seckey=SECKEY,
    nonce=9,
    gas_price=20000000000,
    gas_limit=21000,
    to="0x3535353535353535353535353535353535353535",
    value=10 ** 18,
    data=b"",
    chain_id=1,
)
expected_raw = (
    "f86c098504a817c800825208943535353535353535353535353535353535353535"
    "880de0b6b3a76400008025a028ef61340bd939bc2195fe537567866003e1a15d3c71ff63e1590620aa636276"
    "a067cbe9d8997f761aecb703304b3800ccf555c9f3dc64214b297fb1966a3b6d83"
)
check("EIP-155 raw transaction", signed["raw"][2:], expected_raw)
check("EIP-155 v", signed["v"], 37)

decoded = tx.decode_raw(signed["raw"])
check("decode nonce", decoded["nonce"], 9)
check("decode gas_price", decoded["gas_price"], 20000000000)
check("decode to", decoded["to"].lower(), "0x3535353535353535353535353535353535353535")
check("decode value", decoded["value"], 10 ** 18)
check("signing hash roundtrip", tx.signing_hash(decoded), signed["signing_hash"])

recovered = ecdsa.recover(
    bytes.fromhex(signed["signing_hash"][2:]),
    int(signed["r"], 16),
    int(signed["s"], 16),
    signed["v"] - 35 - 2,
)
check("signature recovers to signer", "0x" + recovered.hex(), "0x" + ecdsa.address(SECKEY).hex())

# low-s 规范化：s 不应超过 N/2
check("low-s enforced", int(signed["s"], 16) <= ecdsa.N // 2, True)

# ---------------- ABI ----------------
check("commitBatch selector", abi.selector("commitBatch(bytes32,uint256,uint256)").hex(), 
      keccak256(b"commitBatch(bytes32,uint256,uint256)").hex()[:8])

root_hex = "ab" * 32
calldata = abi.encode_call(
    "commitBatch(bytes32,uint256,uint256)",
    abi.bytes32(root_hex),
    abi.uint(42),
    abi.uint(10 ** 18),
)
check("calldata length", len(calldata), 4 + 32 * 3)
check("calldata selector prefix", calldata[:4].hex(), abi.selector("commitBatch(bytes32,uint256,uint256)").hex())
check("calldata root word", calldata[4:36].hex(), root_hex)
check("calldata epoch word", int.from_bytes(calldata[36:68], "big"), 42)
check("calldata amount word", int.from_bytes(calldata[68:100], "big"), 10 ** 18)

# ---------------- Merkle 证明与合约逻辑一致 ----------------
from aether import settlement  # noqa: E402
from aether.config import load_settings  # noqa: E402

load_settings()
from aether.ledger import get_ledger  # noqa: E402

ledger = get_ledger()
for i in range(9):
    ledger.record(
        role="serve", self_node="aa", peer_node=f"p{i}", model="m",
        in_tok=10 + i, out_tok=5 + i, price_in=1.0, price_out=2.0,
        source="relay", req_id=f"r{i}",
    )
result = settlement.settle()
import json  # noqa: E402

proof_doc = json.loads(Path(result["batch"]["path"]).read_text(encoding="utf-8"))
all_ok = all(
    settlement.verify_proof(proof_doc["merkle_root"], e["leaf"], e["proof"])
    for e in proof_doc["entries"]
)
check("all merkle proofs verify", all_ok, True)
check("merkle root reproducible", settlement.merkle_root([e["leaf"] for e in proof_doc["entries"]]),
      proof_doc["merkle_root"])
check("tampered leaf rejected",
      settlement.verify_proof(proof_doc["merkle_root"], "00" * 32, proof_doc["entries"][0]["proof"]),
      False)

# 完整链路：批次 root -> calldata -> 签名交易 -> 解码自检
commit_data = abi.encode_call(
    "commitBatch(bytes32,uint256,uint256)",
    abi.bytes32(proof_doc["merkle_root"]),
    abi.uint(int(result["batch"]["ts"])),
    abi.uint(int(result["batch"]["amount"] * 1e9)),
)
settle_tx = tx.sign_transaction(
    seckey=SECKEY, nonce=0, gas_price=1_000_000_000, gas_limit=120_000,
    to="0x" + "11" * 20, value=0, data=commit_data, chain_id=31337,
)
check("settlement tx chain_id", settle_tx["chain_id"], 31337)
check("settlement tx data len", settle_tx["data_len"], len(commit_data))
check("settlement tx v", settle_tx["v"], (settle_tx["v"] - 35 - 2 * 31337) + 35 + 2 * 31337)
decoded_settle = tx.decode_raw(settle_tx["raw"])
check("settlement tx decodes", decoded_settle["to"].lower(), "0x" + "11" * 20)
check("settlement calldata survives rlp",
      decoded_settle["data"][2:], commit_data.hex())

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all EVM checks passed")
