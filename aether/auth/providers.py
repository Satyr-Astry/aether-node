"""内置认证实现：开放、静态 Key、JWT(HS256)。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping

from aether.auth.base import AuthResult

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def is_local(host: str | None) -> bool:
    host = (host or "").strip()
    return host in LOCAL_HOSTS or host.startswith("127.")


def _presented(headers: Mapping[str, str]) -> str:
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for key in ("x-api-key", "X-Api-Key"):
        if headers.get(key):
            return str(headers[key]).strip()
    return ""


class OpenProvider:
    """全放行。只应在明确只监听回环时使用。"""

    name = "open"

    def authenticate(self, headers: Mapping[str, str], client_host: str) -> AuthResult:
        return AuthResult.allow("open")


class ApiKeyProvider:
    """静态 Bearer / X-Api-Key。

    本地回环是否免鉴权由 trust_local 决定：能读 settings.json 的人本来就有全部权限，
    强制本机 GUI 去读密钥只是麻烦。容器或反向代理场景应关掉它。
    """

    name = "api_key"

    def __init__(self, key: str, trust_local: bool = True) -> None:
        self._key = key or ""
        self._trust_local = trust_local

    def authenticate(self, headers: Mapping[str, str], client_host: str) -> AuthResult:
        if not self._key:
            return AuthResult.allow("no-key-configured")
        if self._trust_local and is_local(client_host):
            return AuthResult.allow("local")
        if hmac.compare_digest(_presented(headers), self._key):
            return AuthResult.allow("api-key")
        return AuthResult.deny("需要有效的 api_key")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class JwtHs256Provider:
    """HS256 JWT。零依赖实现，只校验签名与 exp/nbf。

    不支持 RS256/ES256——那需要非对称密钥管理与 JWKS 拉取，属于另一个量级的工程，
    留作接口扩展点而不是现在就假装支持。
    """

    name = "jwt"

    def __init__(self, secret: str, audience: str = "", trust_local: bool = False) -> None:
        self._secret = (secret or "").encode("utf-8")
        self._audience = audience
        self._trust_local = trust_local

    def authenticate(self, headers: Mapping[str, str], client_host: str) -> AuthResult:
        if not self._secret:
            return AuthResult.deny("未配置 JWT 密钥")
        if self._trust_local and is_local(client_host):
            return AuthResult.allow("local")

        token = _presented(headers)
        if not token or token.count(".") != 2:
            return AuthResult.deny("缺少或格式错误的 JWT")
        header_b64, payload_b64, sig_b64 = token.split(".")

        try:
            header = json.loads(_b64url_decode(header_b64))
        except Exception:
            return AuthResult.deny("JWT header 无法解析")
        if str(header.get("alg")) != "HS256":
            return AuthResult.deny(f"不支持的 alg: {header.get('alg')}")

        expected = hmac.new(
            self._secret, f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
        ).digest()
        try:
            actual = _b64url_decode(sig_b64)
        except Exception:
            return AuthResult.deny("JWT 签名无法解码")
        if not hmac.compare_digest(expected, actual):
            return AuthResult.deny("JWT 签名无效")

        try:
            claims = json.loads(_b64url_decode(payload_b64))
        except Exception:
            return AuthResult.deny("JWT payload 无法解析")

        now = time.time()
        exp = claims.get("exp")
        if exp is not None and float(exp) < now:
            return AuthResult.deny("JWT 已过期")
        nbf = claims.get("nbf")
        if nbf is not None and float(nbf) > now:
            return AuthResult.deny("JWT 尚未生效")
        if self._audience:
            aud = claims.get("aud")
            auds = aud if isinstance(aud, list) else [aud]
            if self._audience not in auds:
                return AuthResult.deny("JWT audience 不匹配")

        principal = str(claims.get("sub") or claims.get("iss") or "jwt")
        return AuthResult.allow(principal)


def build(settings) -> object:
    """按配置挑 provider。未知模式回退到 api_key，不静默放行。"""
    mode = (getattr(settings, "auth_mode", "") or "api_key").strip().lower()
    trust_local = bool(getattr(settings, "auth_trust_local", True))
    if mode == "open":
        return OpenProvider()
    if mode == "jwt":
        return JwtHs256Provider(
            secret=getattr(settings, "auth_jwt_secret", "") or "",
            audience=getattr(settings, "auth_jwt_audience", "") or "",
            trust_local=False,  # JWT 模式默认不信任本地，避免绕过
        )
    return ApiKeyProvider(
        key=getattr(settings, "api_key", "") or "",
        trust_local=trust_local,
    )
