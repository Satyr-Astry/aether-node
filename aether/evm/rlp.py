"""RLP 编码（以太坊黄皮书附录 B）。"""

from __future__ import annotations

from typing import Any


def _length_prefix(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(raw)]) + raw


def _encode_bytes(payload: bytes) -> bytes:
    if len(payload) == 1 and payload[0] < 0x80:
        return payload
    return _length_prefix(len(payload), 0x80) + payload


def _encode_int(value: int) -> bytes:
    if value == 0:
        return b"\x80"
    if value < 0:
        raise ValueError("RLP 不支持负数")
    return _encode_bytes(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def encode(item: Any) -> bytes:
    if isinstance(item, (bytes, bytearray)):
        return _encode_bytes(bytes(item))
    if isinstance(item, bool):
        return _encode_int(1 if item else 0)
    if isinstance(item, int):
        return _encode_int(item)
    if isinstance(item, str):
        return _encode_bytes(item.encode("utf-8"))
    if isinstance(item, (list, tuple)):
        payload = b"".join(encode(x) for x in item)
        return _length_prefix(len(payload), 0xC0) + payload
    raise TypeError(f"无法 RLP 编码的类型: {type(item)!r}")
