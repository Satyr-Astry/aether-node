"""版本更新检查。

查 GitHub Releases API 与当前版本比对。**只做告知，不自动下载或替换**——
Windows 上自动替换正在运行的 exe 涉及文件占用与代码签名，误操作代价太高，
交给人决定更稳妥。

网络不可达、仓库不存在、限流都按「未知」处理，不影响主流程。
"""

from __future__ import annotations

import re
import time

import httpx

from aether.net import tls_verify

DEFAULT_REPO = "aether-node/aether-node"
CACHE_TTL = 3600.0
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple[int, int, int]:
    """从 'v0.3.1' / '0.3.1' / 'release-0.3.1' 里取出三段版本号。"""
    match = _VERSION_RE.search(text or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


def compare(a: str, b: str) -> int:
    """返回正数表示 a 比 b 新，负数表示旧，0 表示相同。"""
    va, vb = parse_version(a), parse_version(b)
    return (va > vb) - (va < vb)


class UpdateChecker:
    def __init__(self, repo: str = DEFAULT_REPO, current: str = "") -> None:
        self.repo = repo
        self.current = current
        self._cache: dict | None = None
        self._cached_at = 0.0
        self.last_error = ""

    async def check(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._cache and now - self._cached_at < CACHE_TTL:
            return {**self._cache, "cached": True}

        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        result: dict = {
            "current": self.current,
            "latest": "",
            "update_available": False,
            "url": "",
            "published_at": "",
            "cached": False,
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(12.0), verify=tls_verify()
            ) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "AetherNode", "Accept": "application/vnd.github+json"},
                )
            if resp.status_code == 404:
                result["status"] = "no_releases"
                self.last_error = "仓库没有 release 或不存在"
                return result
            if resp.status_code == 403:
                result["status"] = "rate_limited"
                self.last_error = "GitHub API 限流"
                return result
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            result["status"] = "unreachable"
            self.last_error = str(exc)[:200]
            return result

        latest = str(data.get("tag_name") or "")
        result.update({
            "latest": latest,
            "url": str(data.get("html_url") or ""),
            "published_at": str(data.get("published_at") or ""),
            "update_available": compare(latest, self.current) > 0,
            "status": "ok",
        })
        self._cache = result
        self._cached_at = now
        return result


_checker: UpdateChecker | None = None


def get_checker(current: str = "") -> UpdateChecker:
    global _checker
    if _checker is None:
        _checker = UpdateChecker(current=current)
    return _checker
