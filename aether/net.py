"""统一 TLS 配置。

httpx 0.28 起 `verify=True` 走的是系统证书库，Windows 上该路径常常是空的
（`C:\\Program Files\\Common Files\\SSL/cert.pem` 不存在），表现为
`CERTIFICATE_VERIFY_FAILED`。这里显式绑定 certifi 的 CA 包，避免依赖宿主环境。
"""

from __future__ import annotations

import os
from pathlib import Path


def ca_bundle() -> str | bool:
    override = os.environ.get("AETHER_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if override and Path(override).exists():
        return override
    try:
        import certifi

        path = certifi.where()
        if Path(path).exists():
            return path
    except Exception:
        pass
    return True


def tls_verify() -> str | bool:
    """TLS 校验策略。

    本机若有 TLS 中间设备（企业代理/安全网关），其自签 CA 不在公共信任库里，
    所有对公网的 https 请求都会 CERTIFICATE_VERIFY_FAILED。这种情况需要用户
    显式设置 AETHER_INSECURE_TLS=1 才降级——绝不做静默放行。
    """
    if os.environ.get("AETHER_INSECURE_TLS", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    return ca_bundle()
