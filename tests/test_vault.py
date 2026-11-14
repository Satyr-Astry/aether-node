"""身份加密备份验证。

重点：
  - 导出的 blob 里不能出现明文私钥
  - 口令错误要抛异常，而不是解出一段垃圾
  - 篡改密文要被 GCM 认证标签拒绝
  - 往返后公钥、EVM 地址、npub 全部一致
"""

import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-vault-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether import identity_vault as vault  # noqa: E402
from aether.config import load_settings  # noqa: E402
from aether.identity import Identity  # noqa: E402

load_settings()

fails = []


def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


ident = Identity(secrets.token_hex(32))
password = "correct horse battery staple"

blob = vault.export_backup(ident, password)
serialized = json.dumps(blob)

# ---------- 不泄漏明文 ----------
secret_hex = ident.export_secret()
check("blob has no plaintext secret", secret_hex not in serialized, True)
check("blob has no base64 secret",
      __import__("base64").b64encode(bytes.fromhex(secret_hex)).decode() not in serialized, True)
check("blob carries public key", blob["pubkey"], ident.pubkey)
check("blob carries evm address", blob["evm_address"], ident.evm_address)
check("kdf recorded", blob["kdf"], "pbkdf2-sha256")
check("iterations recorded", blob["iterations"], 200_000)

# ---------- 正确口令恢复 ----------
recovered = vault.decrypt_backup(blob, password)
check("recovered secret matches", recovered["seckey"], secret_hex)
check("recovered pubkey matches", recovered["pubkey"], ident.pubkey)
check("restored identity is same node", Identity(recovered["seckey"]).pubkey, ident.pubkey)
check("restored evm address matches", Identity(recovered["seckey"]).evm_address, ident.evm_address)
check("restored npub matches", Identity(recovered["seckey"]).npub, ident.npub)

# ---------- 错误口令 ----------
try:
    vault.decrypt_backup(blob, "wrong password")
    check("wrong password rejected", False, True)
except ValueError as exc:
    check("wrong password rejected", "口令错误" in str(exc) or "损坏" in str(exc), True)

# ---------- 篡改检测 ----------
import base64  # noqa: E402

tampered = dict(blob)
raw = bytearray(base64.b64decode(tampered["ciphertext"]))
raw[5] ^= 0xFF
tampered["ciphertext"] = base64.b64encode(bytes(raw)).decode()
try:
    vault.decrypt_backup(tampered, password)
    check("tampered ciphertext rejected", False, True)
except ValueError:
    check("tampered ciphertext rejected", True, True)

# ---------- 短口令被拒 ----------
try:
    vault.export_backup(ident, "short")
    check("short password rejected", False, True)
except ValueError:
    check("short password rejected", True, True)

# ---------- 版本与字段校验 ----------
bad_version = dict(blob)
bad_version["v"] = 99
try:
    vault.decrypt_backup(bad_version, password)
    check("unsupported version rejected", False, True)
except ValueError as exc:
    check("unsupported version rejected", "版本" in str(exc), True)

missing = {k: v for k, v in blob.items() if k != "salt"}
try:
    vault.decrypt_backup(missing, password)
    check("missing field rejected", False, True)
except ValueError as exc:
    check("missing field rejected", "缺少字段" in str(exc), True)

# ---------- 两次导出使用不同盐 ----------
blob2 = vault.export_backup(ident, password)
check("salt is random per export", blob["salt"] != blob2["salt"], True)
check("ciphertext differs per export", blob["ciphertext"] != blob2["ciphertext"], True)
check("both decrypt fine",
      vault.decrypt_backup(blob2, password)["seckey"], secret_hex)

# ---------- 自检函数 ----------
check("roundtrip self-check", vault.verify_roundtrip(ident, password), True)
# 短口令在导出阶段就该被拒，verify_roundtrip 应把异常透出来
try:
    vault.verify_roundtrip(ident, "nope")
    check("roundtrip rejects short password", False, True)
except ValueError:
    check("roundtrip rejects short password", True, True)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all vault checks passed")
