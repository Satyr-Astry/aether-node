"""secp256k1 / BIP340 自洽性 + 已知向量校验。不依赖第三方库，系统 Python 直接可跑。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether import secp256k1 as sk  # noqa: E402

failures = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        failures.append(name)


# 1) 已知点：2G
g2 = sk._affine_mul((sk.GX, sk.GY), 2)
check(
    "2G.x",
    hex(g2[0]),
    "0xc6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5",
)
check(
    "2G.y",
    hex(g2[1]),
    "0x1ae168fea63dc339a3c58419466ceaeef7f632653266d0e1236431a950cfe52a",
)

# 2) BIP340 test vector 0 的公钥
seckey = bytes.fromhex("0" * 63 + "3")
check(
    "BIP340 vector0 pubkey",
    sk.pubkey_xonly(seckey).hex().upper(),
    "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
)

# 3) 签名 / 验签往返
msg = bytes.fromhex("11" * 32)
sig = sk.schnorr_sign(msg, seckey)
pub = sk.pubkey_xonly(seckey)
check("sign/verify roundtrip", sk.schnorr_verify(msg, pub, sig), True)

# 4) 篡改消息必须失败
check("verify wrong msg", sk.schnorr_verify(bytes.fromhex("22" * 32), pub, sig), False)

# 5) 篡改签名必须失败
bad = bytearray(sig)
bad[5] ^= 0xFF
check("verify tampered sig", sk.schnorr_verify(msg, pub, bytes(bad)), False)

# 6) 确定性 nonce（固定 aux）可复现
s1 = sk.schnorr_sign(msg, seckey, aux=b"\x00" * 32)
s2 = sk.schnorr_sign(msg, seckey, aux=b"\x00" * 32)
check("deterministic signing", s1 == s2, True)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all secp256k1 checks passed")
