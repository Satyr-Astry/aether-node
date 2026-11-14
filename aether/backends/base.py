"""推理后端抽象。

动机：现在整条链路硬绑 llama.cpp。要接 vLLM / SGLang / 远程端点，或者换成
别的本地运行时，需要先有一个稳定的契约——否则每换一次后端都要改路由、
计量、隧道、GUI 四处。

这里只做契约与注册表，**不重构现有调用点**：LlamaRuntime 已经满足这个 Protocol，
注册表把它们统一成「按名字取后端」的工厂。真正的替换发生在需要时，而不是现在。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class InferenceBackend(Protocol):
    """一个能加载模型并暴露 OpenAI 兼容 HTTP 接口的后端。"""

    #: 后端标识，用于选择与展示
    name: str

    #: idle | starting | ready | failed | stopping
    state: str

    #: 就绪后可直接拼 /v1/... 的基地址；未就绪时为空串
    @property
    def base_url(self) -> str: ...

    #: 实际监听端口
    @property
    def port(self) -> int: ...

    async def start(self, model: Any, force_ctx: int | None = None) -> dict:
        """加载并启动。返回状态字典，失败应抛异常并把 state 落到 failed。"""
        ...

    async def stop(self) -> None:
        """停止并释放资源。重复调用必须安全。"""
        ...

    def status(self) -> dict:
        """可序列化的状态，供 API 与 GUI 使用。"""
        ...


class BackendError(RuntimeError):
    """后端层统一异常，便于路由层做统一降级。"""
