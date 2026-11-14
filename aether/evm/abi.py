"""Solidity ABI 编码：只实现结算用得到的部分。

不追求通用性——结算调用 `commitBatch(bytes32,uint256,uint256)` 全是静态参数，
拼 selectors 与 32 字节字就够。动态类型留了一个 `dynamic_bytes` 以备后用。
"""

from __future__ import annotations

from aether.evm.keccak import keccak256

WORD = 32


def selector(signature: str) -> bytes:
    return keccak256(signature.encode("utf-8"))[:4]


def uint(value: int) -> bytes:
    if value < 0:
        raise ValueError("uint256 不支持负数")
    if value >= 1 << 256:
        raise ValueError("超出 uint256 范围")
    return value.to_bytes(WORD, "big")


def address_bytes(value) -> bytes:
    if isinstance(value, str):
        raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raise TypeError("地址必须是字符串或 bytes")
    if len(raw) != 20:
        raise ValueError(f"地址应为 20 字节，实际 {len(raw)}")
    return b"\x00" * 12 + raw


def bytes32(value) -> bytes:
    if isinstance(value, str):
        raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raise TypeError("bytes32 必须是字符串或 bytes")
    if len(raw) != 32:
        raise ValueError(f"bytes32 应为 32 字节，实际 {len(raw)}")
    return raw


def dynamic_bytes(payload: bytes) -> bytes:
    """动态 bytes 的编码：长度字 + 右补零到 32 字节对齐。"""
    padded = payload + b"\x00" * ((WORD - len(payload) % WORD) % WORD)
    return uint(len(payload)) + padded


def encode_call(signature: str, *words: bytes) -> bytes:
    """静态参数直接拼在 selector 后面。"""
    head = b"".join(words)
    if len(head) % WORD != 0:
        raise ValueError("静态参数总长必须是 32 的整数倍")
    return selector(signature) + head


def bytes32_array(values: list[str]) -> bytes:
    """bytes32[] 的动态编码：长度字 + 逐元素 32 字节（不含 offset 头）。"""
    body = b"".join(bytes32(v) for v in values)
    return uint(len(values)) + body


def encode_claim(epoch: int, amount: int, proof: list[str]) -> bytes:
    """claim(uint256 epoch, uint256 amount, bytes32[] proof) 的 calldata。

    三个参数中前两个是静态槽，第三个动态，所以 head 里的 offset 是 3×32 = 0x60。
    """
    head = uint(epoch) + uint(amount) + uint(3 * WORD)
    return selector("claim(uint256,uint256,bytes32[])") + head + bytes32_array(proof)


def encode_set_root(epoch: int, root: str, total: int) -> bytes:
    """setRoot(uint256 epoch, bytes32 root, uint256 total) 的 calldata。"""
    return encode_call("setRoot(uint256,bytes32,uint256)", uint(epoch), bytes32(root), uint(total))


def pretty(data: bytes) -> str:
    """带 4 字节选择器分隔的可读形式，便于人工核对。"""
    body = data[4:]
    parts = [data[:4].hex()]
    for i in range(0, len(body), WORD):
        parts.append(body[i : i + WORD].hex())
    return "0x" + " | ".join(parts)
