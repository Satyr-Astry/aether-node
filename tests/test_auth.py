"""认证抽象验证：三种 provider 的放行/拒绝路径，以及 JWT 的篡改与时效。

JWT 用纯标准库实现（HS256），这里也用标准库构造 token 来验，不引入 PyJWT。
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-auth-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether import auth  # noqa: E402
from aether.auth import providers as prov  # noqa: E402
from aether.config import load_settings  # noqa: E402

load_settings()

fails = []


def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_jwt(secret: str, claims: dict, alg: str = "HS256") -> str:
    header = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(sig)}"


# ---------- Open ----------
op = auth.OpenProvider()
check("open allows anything", op.authenticate({}, "203.0.113.9").ok, True)

# ---------- ApiKey ----------
kp = auth.ApiKeyProvider(key="s3cret-key-value", trust_local=True)
check("api key accepts local", kp.authenticate({}, "127.0.0.1").ok, True)
check("api key rejects remote without header", kp.authenticate({}, "203.0.113.9").ok, False)
check("api key accepts bearer",
      kp.authenticate({"authorization": "Bearer s3cret-key-value"}, "203.0.113.9").ok, True)
check("api key accepts x-api-key",
      kp.authenticate({"x-api-key": "s3cret-key-value"}, "203.0.113.9").ok, True)
check("api key rejects wrong value",
      kp.authenticate({"authorization": "Bearer nope"}, "203.0.113.9").ok, False)

strict = auth.ApiKeyProvider(key="k", trust_local=False)
check("strict mode rejects local too", strict.authenticate({}, "127.0.0.1").ok, False)
check("strict mode accepts with key",
      strict.authenticate({"x-api-key": "k"}, "127.0.0.1").ok, True)

empty = auth.ApiKeyProvider(key="", trust_local=True)
check("no key configured allows", empty.authenticate({}, "203.0.113.9").ok, True)

# ---------- JWT ----------
secret = "jwt-shared-secret"
jp = auth.JwtHs256Provider(secret=secret, audience="aether", trust_local=False)
now = int(time.time())

good = make_jwt(secret, {"sub": "node-a", "aud": "aether", "exp": now + 300})
r = jp.authenticate({"authorization": f"Bearer {good}"}, "203.0.113.9")
check("jwt valid token accepted", r.ok, True)
check("jwt principal from sub", r.principal, "node-a")

expired = make_jwt(secret, {"sub": "x", "aud": "aether", "exp": now - 10})
check("jwt expired rejected", jp.authenticate({"authorization": f"Bearer {expired}"}, "1.2.3.4").ok, False)

future = make_jwt(secret, {"sub": "x", "aud": "aether", "nbf": now + 300})
check("jwt nbf future rejected", jp.authenticate({"authorization": f"Bearer {future}"}, "1.2.3.4").ok, False)

wrong_aud = make_jwt(secret, {"sub": "x", "aud": "other", "exp": now + 300})
check("jwt wrong audience rejected",
      jp.authenticate({"authorization": f"Bearer {wrong_aud}"}, "1.2.3.4").ok, False)

bad_sig = make_jwt("different-secret", {"sub": "x", "aud": "aether", "exp": now + 300})
check("jwt bad signature rejected",
      jp.authenticate({"authorization": f"Bearer {bad_sig}"}, "1.2.3.4").ok, False)

alg_none = make_jwt(secret, {"sub": "x"}, alg="none")
check("jwt alg=none rejected",
      jp.authenticate({"authorization": f"Bearer {alg_none}"}, "1.2.3.4").ok, False)

check("jwt missing token rejected", jp.authenticate({}, "1.2.3.4").ok, False)
check("jwt malformed rejected", jp.authenticate({"authorization": "Bearer abc"}, "1.2.3.4").ok, False)

no_secret = auth.JwtHs256Provider(secret="")
check("jwt without secret rejects", no_secret.authenticate({"authorization": f"Bearer {good}"}, "1.2.3.4").ok, False)

# 篡改 payload 后签名失效
h, p, s = good.split(".")
tampered_payload = b64(json.dumps({"sub": "attacker", "aud": "aether", "exp": now + 300}).encode())
check("jwt tampered payload rejected",
      jp.authenticate({"authorization": f"Bearer {h}.{tampered_payload}.{s}"}, "1.2.3.4").ok, False)


# ---------- 工厂 ----------
class S:
    def __init__(self, **kw):
        self.api_key = ""
        self.auth_mode = "api_key"
        self.auth_trust_local = True
        self.auth_jwt_secret = ""
        self.auth_jwt_audience = ""
        for k, v in kw.items():
            setattr(self, k, v)


check("factory default -> api_key", auth.build(S()).name, "api_key")
check("factory jwt", auth.build(S(auth_mode="jwt", auth_jwt_secret="s")).name, "jwt")
check("factory open", auth.build(S(auth_mode="open")).name, "open")
check("factory unknown falls back to api_key", auth.build(S(auth_mode="martian")).name, "api_key")
check("jwt mode does not trust local", auth.build(S(auth_mode="jwt", auth_jwt_secret="s"))._trust_local, False)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all auth checks passed")
