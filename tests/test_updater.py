"""版本比较与更新检查的离线验证。

网络部分用 mock 覆盖，「不可达 / 限流 / 无 release」这些分支不该依赖真实 GitHub。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-upd-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether.config import load_settings  # noqa: E402
from aether.updater import UpdateChecker, compare, parse_version  # noqa: E402

load_settings()

fails = []


def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


# ---------- 版本解析 ----------
check("parse plain", parse_version("0.2.0"), (0, 2, 0))
check("parse with v", parse_version("v1.12.3"), (1, 12, 3))
check("parse with prefix", parse_version("release-0.3.1"), (0, 3, 1))
check("parse garbage", parse_version("no version here"), (0, 0, 0))
check("parse empty", parse_version(""), (0, 0, 0))
check("parse prerelease suffix", parse_version("v0.3.0-beta.1"), (0, 3, 0))

# ---------- 比较 ----------
check("newer patch", compare("0.2.1", "0.2.0") > 0, True)
check("older patch", compare("0.2.0", "0.2.1") < 0, True)
check("same", compare("0.2.0", "v0.2.0"), 0)
check("major wins", compare("1.0.0", "0.9.9") > 0, True)
check("minor wins", compare("0.10.0", "0.9.0") > 0, True)
check("double digit", compare("0.10.0", "0.9.0") > 0, True)

# ---------- 各种失败分支 ----------
import asyncio  # noqa: E402


async def case(status: int | None, payload: dict | None = None, raises: bool = False) -> dict:
    c = UpdateChecker(repo="x/y", current="0.2.0")

    class FakeResp:
        def __init__(self):
            self.status_code = status or 0
            self._payload = payload or {}

        def raise_for_status(self):
            if status and status >= 400:
                raise RuntimeError(f"HTTP {status}")

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            if raises:
                raise RuntimeError("network down")
            return FakeResp()

    import aether.updater as up

    original = up.httpx.AsyncClient
    up.httpx.AsyncClient = lambda **kw: FakeClient()
    try:
        return await c.check()
    finally:
        up.httpx.AsyncClient = original


res = asyncio.run(case(404))
check("404 -> no_releases", res["status"], "no_releases")
check("404 -> not available", res["update_available"], False)

res = asyncio.run(case(403))
check("403 -> rate_limited", res["status"], "rate_limited")

res = asyncio.run(case(0, raises=True))
check("network error -> unreachable", res["status"], "unreachable")

res = asyncio.run(case(200, {"tag_name": "v0.3.0", "html_url": "http://x", "published_at": "2026-01-01"}))
check("200 newer -> available", res["update_available"], True)
check("200 sets latest", res["latest"], "v0.3.0")
check("200 status ok", res["status"], "ok")

res = asyncio.run(case(200, {"tag_name": "v0.2.0"}))
check("same version -> not available", res["update_available"], False)

res = asyncio.run(case(200, {"tag_name": "v0.1.9"}))
check("older release -> not available", res["update_available"], False)

# ---------- 缓存 ----------
async def cache_case():
    c = UpdateChecker(repo="x/y", current="0.2.0")
    c._cache = {"status": "ok", "latest": "v0.3.0", "update_available": True}
    c._cached_at = __import__("time").time()
    return await c.check()


res = asyncio.run(cache_case())
check("cache hit avoids network", res.get("cached"), True)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all updater checks passed")
