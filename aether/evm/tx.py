"""EIP-155 交易签名与原始交易序列化。

产出的 raw 十六进制可直接用任意 EVM 节点的 eth_sendRawTransaction 广播，
不依赖 web3.py / eth-account。
"""

from __future__ import annotations

from typing import Any

from aether.evm import ecdsa, rlp
from aether.evm.keccak import keccak256


def _to_bytes_addr(to: Any) -> bytes:
    if to in (None, "", b""):
        return b""
    if isinstance(to, (bytes, bytearray)):
        raw = bytes(to)
    elif isinstance(to, str):
        raw = bytes.fromhex(to[2:] if to.startswith("0x") else to)
    else:
        raise TypeError("to 必须是地址字符串或 bytes")
    if len(raw) != 20:
        raise ValueError(f"地址长度应为 20 字节，实际 {len(raw)}")
    return raw


def sign_transaction(
    *,
    seckey: bytes,
    nonce: int,
    gas_price: int,
    gas_limit: int,
    to: Any = "",
    value: int = 0,
    data: bytes = b"",
    chain_id: int = 1,
) -> dict:
    """构造并签名一笔 EIP-155 交易。"""
    target = _to_bytes_addr(to)
    fields: list[Any] = [nonce, gas_price, gas_limit, target, value, data]

    # EIP-155：签名哈希包含 chain_id, 0, 0
    signing_payload = rlp.encode(fields + [chain_id, 0, 0])
    digest = keccak256(signing_payload)
    r, s, recid = ecdsa.sign(digest, seckey)
    v = recid + 35 + 2 * chain_id

    raw = rlp.encode(fields + [v, r, s])
    return {
        "raw": "0x" + raw.hex(),
        "hash": "0x" + keccak256(raw).hex(),
        "from": "0x" + ecdsa.address(seckey).hex(),
        "to": ("0x" + target.hex()) if target else "",
        "nonce": nonce,
        "gas_price": gas_price,
        "gas_limit": gas_limit,
        "value": value,
        "chain_id": chain_id,
        "v": v,
        "r": hex(r),
        "s": hex(s),
        "data_len": len(data),
        "signing_hash": "0x" + digest.hex(),
    }


def decode_raw(raw_hex: str) -> dict:
    """解析原始交易（仅 legacy EIP-155），用于自检。"""
    raw = bytes.fromhex(raw_hex[2:] if raw_hex.startswith("0x") else raw_hex)

    def _parse(buf: bytes, pos: int = 0) -> tuple[Any, int]:
        if pos >= len(buf):
            raise ValueError("RLP 截断")
        first = buf[pos]
        if first < 0x80:
            return bytes([first]), pos + 1
        if first < 0xB8:
            length = first - 0x80
            return buf[pos + 1 : pos + 1 + length], pos + 1 + length
        if first < 0xC0:
            lenlen = first - 0xB7
            length = int.from_bytes(buf[pos + 1 : pos + 1 + lenlen], "big")
            start = pos + 1 + lenlen
            return buf[start : start + length], start + length
        if first < 0xF8:
            length = first - 0xC0
            start = pos + 1
        else:
            lenlen = first - 0xF7
            length = int.from_bytes(buf[pos + 1 : pos + 1 + lenlen], "big")
            start = pos + 1 + lenlen
        items = []
        cursor = start
        end = start + length
        while cursor < end:
            item, cursor = _parse(buf, cursor)
            items.append(item)
        return items, end

    items, _ = _parse(raw)
    if not isinstance(items, list) or len(items) != 9:
        raise ValueError("不是 9 字段的 legacy 交易")
    names = ["nonce", "gas_price", "gas_limit", "to", "value", "data", "v", "r", "s"]
    out: dict = {}
    for name, value in zip(names, items):
        if isinstance(value, list):
            out[name] = value
        elif name in ("to", "data"):
            out[name] = "0x" + value.hex()
        else:
            out[name] = int.from_bytes(value, "big") if value else 0
    return out


def signing_hash(decoded: dict) -> str:
    """从已解析交易重算签名哈希，验证 EIP-155 字段拼装是否正确。"""
    chain_id = (decoded["v"] - 35) // 2
    fields = [
        decoded["nonce"], decoded["gas_price"], decoded["gas_limit"],
        bytes.fromhex(decoded["to"][2:]) if decoded["to"] not in ("0x", "") else b"",
        decoded["value"], bytes.fromhex(decoded["data"][2:]) if decoded["data"] != "0x" else b"",
    ]
    return "0x" + keccak256(rlp.encode(fields + [chain_id, 0, 0])).hex()
