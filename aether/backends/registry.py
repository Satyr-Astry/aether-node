"""后端注册表：按名字创建实例。

注册是惰性的——导入注册表不会拉起 llama.cpp 运行时，只有真正 create 时才构造。
"""

from __future__ import annotations

from collections.abc import Callable

from aether.backends.base import BackendError, InferenceBackend

_factories: dict[str, Callable[[], InferenceBackend]] = {}
_instances: dict[str, InferenceBackend] = {}
_aliases: dict[str, str] = {"llama.cpp": "llama_cpp", "llama-server": "llama_cpp", "llamacpp": "llama_cpp"}


def register(name: str, factory: Callable[[], InferenceBackend]) -> None:
    _factories[name] = factory


def _canonical(name: str) -> str:
    key = (name or "").strip().lower().replace("-", "_")
    return _aliases.get(name, _aliases.get(key, key))


def available() -> list[str]:
    return sorted(_factories)


def create(name: str = "llama_cpp") -> InferenceBackend:
    key = _canonical(name)
    if key not in _factories:
        raise BackendError(f"未知后端 {name!r}，可用：{', '.join(available()) or '无'}")
    return _factories[key]()


def get(name: str = "llama_cpp") -> InferenceBackend:
    """取单例，便于 API 层与路由层共享同一后端实例。"""
    key = _canonical(name)
    if key not in _instances:
        _instances[key] = create(key)
    return _instances[key]


def describe() -> list[dict]:
    return [{"name": n, "instantiated": n in _instances} for n in available()]
