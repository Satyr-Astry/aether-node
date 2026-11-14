"""推理后端抽象与注册。

导入本包会注册内置后端，随后可用 `registry.get("llama_cpp")` 取实例。
目前只有 llama.cpp 一个实现，但契约已就位：接第二个后端时只需实现
InferenceBackend 并 register，路由/计量/隧道/GUI 都不必改。
"""

from aether.backends import llama_cpp as _llama_cpp  # noqa: F401  触发注册
from aether.backends.base import BackendError, InferenceBackend
from aether.backends.registry import available, create, describe, get, register

__all__ = [
    "InferenceBackend",
    "BackendError",
    "available",
    "create",
    "describe",
    "get",
    "register",
]
