"""节点身份的加密备份与恢复。

`identity.json` 是节点的全部身份：丢了它就丢了网络身份、账本归属、隧道地址与
EVM 账户。这里用口令派生密钥加密导出，避免私钥以明文落地或贴进聊天窗口。

用标准 PBKDF2-HMAC-SHA256（20 万次迭代）+ AES-256-GCM。选 GCM 是因为它带认证标签：
口令错了会解密失败，而不是解出一段垃圾再被当成私钥用。
"""

from __future__ import annotations

import base64
import json
import os

from aether.identity import Identity

VERSION = 1
ITERATIONS = 200_000
MIN_PASSWORD = 8


def _derive(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS
    )
    return kdf.derive(password.encode("utf-8"))


def export_backup(identity: Identity, password: str) -> dict:
    """导出加密备份。返回的字典可直接存文件或抄下来。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(password) < MIN_PASSWORD:
        raise ValueError(f"口令至少 {MIN_PASSWORD} 位")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive(password, salt)
    payload = json.dumps(
        {"seckey": identity.export_secret(), "pubkey": identity.pubkey},
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, payload, None)
    return {
        "v": VERSION,
        "kdf": "pbkdf2-sha256",
        "iterations": ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        # 明文公钥仅用于恢复后核对，不构成泄漏（公钥本来就是公开的）
        "pubkey": identity.pubkey,
        "evm_address": identity.evm_address,
        "npub": identity.npub,
    }


def decrypt_backup(blob: dict, password: str) -> dict:
    """解密备份，返回 {seckey, pubkey}。口令错误会抛异常而不是返回垃圾。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if int(blob.get("v") or 0) != VERSION:
        raise ValueError("备份版本不支持")
    try:
        salt = base64.b64decode(blob["salt"])
        nonce = base64.b64decode(blob["nonce"])
        ciphertext = base64.b64decode(blob["ciphertext"])
    except KeyError as exc:
        raise ValueError(f"备份缺少字段: {exc}") from exc

    key = _derive(password, salt)
    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ValueError("口令错误或备份已损坏") from exc

    data = json.loads(plain.decode("utf-8"))
    expected = blob.get("pubkey") or ""
    if expected and data.get("pubkey") != expected:
        raise ValueError("恢复出的公钥与备份记录不一致")
    return data


def verify_roundtrip(identity: Identity, password: str) -> bool:
    """自检：导出再解密，公钥必须对得上。"""
    blob = export_backup(identity, password)
    data = decrypt_backup(blob, password)
    return data.get("pubkey") == identity.pubkey
