"""llama.cpp 后端的注册。

LlamaRuntime 是在引入 Protocol 之前写的，它的接口已经符合契约，所以这里
只做注册，不改它的实现——重构等到真有第二个后端时再做，那时才知道
抽象边界划在哪里才合适。
"""

from __future__ import annotations

from aether.backends.registry import register


def _factory():
    from aether.llama_server import runtime

    return runtime


register("llama_cpp", _factory)
