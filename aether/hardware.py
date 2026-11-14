"""硬件探测与推理参数推荐。"""

from __future__ import annotations

import ctypes
import subprocess
from dataclasses import dataclass

from aether.gguf import GGUFMeta

GB = 1 << 30


@dataclass
class GPU:
    index: int
    name: str
    vram_total: int  # MiB
    vram_free: int
    driver: str

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "vram_total_mb": self.vram_total,
            "vram_free_mb": self.vram_free,
            "driver": self.driver,
        }


def detect_gpus() -> list[GPU]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except Exception:
        return []
    gpus: list[GPU] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append(GPU(int(parts[0]), parts[1], int(parts[2]), int(parts[3]), parts[4]))
        except ValueError:
            continue
    return gpus


def total_ram_gb() -> float:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return round(st.ullAvailPhys / GB, 1)
    except Exception:
        return 0.0


def detect_hardware() -> dict:
    gpus = detect_gpus()
    return {
        "gpus": [g.to_dict() for g in gpus],
        "gpu_present": bool(gpus),
        "ram_available_gb": total_ram_gb(),
    }


def recommend(model_bytes: int, meta: GGUFMeta | None, gpu: GPU | None,
              want_ctx: int = 8192) -> dict:
    """给出 -ngl 与 ctx 的建议值，并标注能否全量卸载。"""
    if gpu is None:
        return {
            "ngl": 0, "ctx": min(want_ctx, 4096), "fits_vram": False,
            "note": "未检测到 NVIDIA GPU，将使用 CPU 推理（速度较慢）",
        }

    vram_free = gpu.vram_free * (1 << 20)
    overhead = int(1.4 * GB)  # 计算缓冲 + CUDA context

    layers = meta.block_count if meta and meta.block_count else 0
    if layers and meta is not None and meta.ok:
        per_layer = model_bytes / layers
        # KV cache：2（K+V）× ctx × kv_heads × head_dim × 2 bytes × layers
        head_dim = 128
        if meta.head_count and meta.embedding_length:
            head_dim = max(1, meta.embedding_length // meta.head_count)
        kv_heads = meta.head_count_kv or meta.head_count or 0
        kv_bytes = 2 * want_ctx * kv_heads * head_dim * 2 * layers
        ctx = want_ctx
        if overhead + kv_bytes > vram_free * 0.9:
            ctx = max(2048, int(want_ctx * 0.5))
            kv_bytes = 2 * ctx * kv_heads * head_dim * 2 * layers
        budget = vram_free - overhead - kv_bytes
        ngl = max(0, min(layers, int(budget / per_layer))) if per_layer > 0 else 0
        if ngl >= layers:
            return {"ngl": -1, "ctx": ctx, "fits_vram": True,
                    "note": f"全量卸载 {layers} 层到 GPU，上下文 {ctx}"}
        if ngl < layers * 0.5:
            return {"ngl": ngl, "ctx": min(ctx, 4096), "fits_vram": False,
                    "note": f"显存不足，仅 {ngl}/{layers} 层上 GPU。建议换更小的量化（Q4_K_M）或更小模型"}
        return {"ngl": ngl, "ctx": ctx, "fits_vram": False,
                "note": f"部分卸载 {ngl}/{layers} 层，速度会明显下降"}

    # 拿不到 GGUF 元数据时用粗估
    if model_bytes < vram_free * 0.75:
        return {"ngl": -1, "ctx": want_ctx, "fits_vram": True,
                "note": "按体积估算可全量卸载"}
    return {"ngl": 0, "ctx": 4096, "fits_vram": False,
            "note": "模型体积接近或超过显存，建议换更小量化"}
