"""认证契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable


@dataclass
class AuthResult:
    ok: bool
    principal: str = ""
    reason: str = ""

    @classmethod
    def allow(cls, principal: str = "local") -> "AuthResult":
        return cls(True, principal, "")

    @classmethod
    def deny(cls, reason: str) -> "AuthResult":
        return cls(False, "", reason)


@runtime_checkable
class AuthProvider(Protocol):
    name: str

    def authenticate(self, headers: Mapping[str, str], client_host: str) -> AuthResult:
        """根据请求头与来源地址判定。实现必须是纯函数，不做 IO。"""
        ...
