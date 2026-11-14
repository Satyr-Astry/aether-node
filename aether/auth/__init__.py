"""认证抽象。

动机：现在只有单一 `api_key`，接 JWT / OAuth / 网关注入身份都得改中间件本身。
这里把「怎么认证」抽成可插拔的 provider，中间件只负责「拿结果做决定」。

保持零依赖：JWT 只用标准库的 hmac + base64 实现 HS256，不引 PyJWT。
"""

from __future__ import annotations

from aether.auth.base import AuthProvider, AuthResult
from aether.auth.providers import ApiKeyProvider, JwtHs256Provider, OpenProvider, build

__all__ = [
    "AuthProvider",
    "AuthResult",
    "ApiKeyProvider",
    "JwtHs256Provider",
    "OpenProvider",
    "build",
]
