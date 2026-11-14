"""节点身份：secp256k1 密钥对，与 Nostr 同构（x-only 公钥即节点 ID）。

用同一套曲线做两件事：给服务广告内容签名，以及直接签署 Nostr 事件上线。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from aether import secp256k1 as sk
from aether.config import get_settings

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_encode(hrp: str, data: bytes) -> str:
    """最小 bech32 实现，仅用于把公钥显示成 npub。"""
    acc, bits, out = 0, 0, []
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    values = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + out
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    checksum = [(chk >> (5 * (5 - i))) & 31 for i in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in out + checksum)


class Identity:
    def __init__(self, seckey_hex: str) -> None:
        self._seckey = bytes.fromhex(seckey_hex)
        if len(self._seckey) != 32:
            raise ValueError("私钥长度错误")
        self._pub = sk.pubkey_xonly(self._seckey)

    @classmethod
    def load_or_create(cls) -> "Identity":
        path = get_settings().home / "identity.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                key = data.get("seckey") or data.get("privkey")
                if key:
                    return cls(key)
            except Exception:
                pass
        seckey = os.urandom(32).hex()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seckey": seckey}, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return cls(seckey)

    # ---- 身份 ----
    @property
    def pubkey(self) -> str:
        return self._pub.hex()

    @property
    def npub(self) -> str:
        try:
            return _bech32_encode("npub", self._pub)
        except Exception:
            return self.pubkey

    # ---- EVM 身份 ----
    @property
    def evm_address(self) -> str:
        """把节点身份直接当作 EVM 账户：同一条 secp256k1 曲线，地址是公钥的 keccak 后缀。

        私钥不离开本对象——需要签名时调用 sign_evm_transaction。
        """
        from aether.evm.ecdsa import address as evm_address_of

        return "0x" + evm_address_of(self._seckey).hex()

    def sign_evm_transaction(self, **kwargs) -> dict:
        """用节点私钥签一笔 EIP-155 交易，返回可直接广播的原始交易。"""
        from aether.evm.tx import sign_transaction

        return sign_transaction(seckey=self._seckey, **kwargs)

    # ---- 签名 ----
    def sign_digest(self, digest32: bytes) -> bytes:
        """对 32 字节摘要直接做 Schnorr 签名（Nostr 事件用）。"""
        if len(digest32) != 32:
            raise ValueError("摘要必须是 32 字节")
        return sk.schnorr_sign(digest32, self._seckey)

    def sign_bytes(self, msg: bytes) -> bytes:
        digest = hashlib.sha256(msg).digest()
        return sk.schnorr_sign(digest, self._seckey)

    def sign_json(self, payload: dict) -> dict:
        body = {k: v for k, v in payload.items() if k != "sig"}
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        signed = dict(body)
        signed["sig"] = self.sign_bytes(raw.encode("utf-8")).hex()
        return signed

    @staticmethod
    def verify_bytes(pubkey_hex: str, msg: bytes, sig_hex: str) -> bool:
        try:
            digest = hashlib.sha256(msg).digest()
            return sk.schnorr_verify(digest, bytes.fromhex(pubkey_hex), bytes.fromhex(sig_hex))
        except Exception:
            return False

    @staticmethod
    def verify_json(pubkey_hex: str, payload: dict) -> bool:
        sig = payload.get("sig")
        if not sig:
            return False
        body = {k: v for k, v in payload.items() if k != "sig"}
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return Identity.verify_bytes(pubkey_hex, raw.encode("utf-8"), sig)


_identity: Identity | None = None


def get_identity() -> Identity:
    global _identity
    if _identity is None:
        _identity = Identity.load_or_create()
    return _identity
