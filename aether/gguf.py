"""GGUF 元数据轻量解析：只为拿到层数、原生上下文、注意力头数，用于精确算 GPU 层数。

只读文件头部若干 KB，遇到超大数组（tokenizer）即停止，不解析张量表。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"GGUF"

(UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL,
 STRING, ARRAY, UINT64, INT64, FLOAT64) = range(13)

_SCALAR_FMT = {
    UINT8: "<B", INT8: "<b", UINT16: "<H", INT16: "<h",
    UINT32: "<I", INT32: "<i", FLOAT32: "<f", BOOL: "<B",
    UINT64: "<Q", INT64: "<q", FLOAT64: "<d",
}

MAX_BYTES = 8 << 20  # 只读前 8MB


@dataclass
class GGUFMeta:
    architecture: str = ""
    block_count: int = 0
    context_length: int = 0
    head_count: int = 0
    head_count_kv: int = 0
    embedding_length: int = 0
    ok: bool = False

    def summary(self) -> dict:
        return {
            "ok": self.ok,
            "architecture": self.architecture,
            "block_count": self.block_count,
            "context_length": self.context_length,
            "head_count": self.head_count,
            "head_count_kv": self.head_count_kv,
            "embedding_length": self.embedding_length,
        }


class _Reader:
    def __init__(self, fh, limit: int) -> None:
        self.fh = fh
        self.limit = limit

    def _check(self) -> None:
        if self.fh.tell() > self.limit:
            raise EOFError("超出读取上限")

    def raw(self, n: int) -> bytes:
        self._check()
        b = self.fh.read(n)
        if len(b) != n:
            raise EOFError("文件截断")
        return b

    def u32(self) -> int:
        return struct.unpack("<I", self.raw(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.raw(8))[0]

    def string(self) -> str:
        n = self.u64()
        if n > 1 << 20:
            raise EOFError("字符串过长")
        return self.raw(n).decode("utf-8", "replace")

    def scalar(self, vtype: int):
        fmt = _SCALAR_FMT.get(vtype)
        if fmt is None:
            raise ValueError(f"未知标量类型 {vtype}")
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.raw(size))[0]

    def skip_value(self, vtype: int) -> None:
        if vtype == STRING:
            n = self.u64()
            self.raw(n)
        elif vtype == ARRAY:
            etype = self.u32()
            count = self.u64()
            if count > 1 << 24:
                raise EOFError("数组过大，跳过解析")
            if etype in _SCALAR_FMT:
                self.raw(struct.calcsize(_SCALAR_FMT[etype]) * count)
            elif etype == STRING:
                # 大词表直接放弃：后面没有我们需要的元数据了
                if count > 20000:
                    raise EOFError("词表过大，停止解析")
                for _ in range(count):
                    self.raw(self.u64())
            else:
                for _ in range(count):
                    self.skip_value(etype)
        else:
            self.scalar(vtype)


WANTED = ("block_count", "context_length", "head_count", "head_count_kv", "embedding_length")


def read_gguf_meta(path: str | Path) -> GGUFMeta:
    meta = GGUFMeta()
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != MAGIC:
                return meta
            version = struct.unpack("<I", fh.read(4))[0]
            if version < 2:
                return meta
            struct.unpack("<Q", fh.read(8))[0]  # tensor_count
            kv_count = struct.unpack("<Q", fh.read(8))[0]
            r = _Reader(fh, MAX_BYTES)
            for _ in range(min(kv_count, 512)):
                key = r.string()
                vtype = r.u32()
                if key == "general.architecture":
                    meta.architecture = r.string() if vtype == STRING else ""
                    continue
                arch = meta.architecture
                matched = next((w for w in WANTED if arch and key == f"{arch}.{w}"), None)
                if matched and vtype in _SCALAR_FMT:
                    setattr(meta, matched, int(r.scalar(vtype)))
                    continue
                r.skip_value(vtype)
                if meta.block_count and meta.context_length:
                    break
        meta.ok = meta.block_count > 0
    except (EOFError, ValueError, OSError, struct.error):
        meta.ok = meta.block_count > 0
    return meta
