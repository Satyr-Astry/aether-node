"""HuggingFace Hub 客户端：GGUF 检索、文件树、断点续传下载。

刻意不依赖 huggingface_hub：直接走 REST API。镜像切换只需换 endpoint，
断点续传与进度完全由本方控制，也不会被上游库的大版本变更打断。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from aether.net import tls_verify

QUANT_RE = re.compile(r"(IQ\d+_[A-Z0-9]+|Q\d+_[A-Z0-9_]+|Q\d+_\d+|Q\d+|BF16|F16|F32)", re.I)
CHUNK = 1 << 20  # 1 MiB


@dataclass
class GGUFFile:
    path: str
    size: int

    @property
    def quant(self) -> str:
        m = QUANT_RE.search(self.path)
        return m.group(1).upper() if m else "?"

    @property
    def fname(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass
class RepoInfo:
    id: str
    downloads: int = 0
    likes: int = 0
    tags: list[str] = field(default_factory=list)
    last_modified: str = ""
    gguf_files: list[GGUFFile] = field(default_factory=list)
    siblings_loaded: bool = False

    @property
    def display(self) -> str:
        return self.id.rsplit("/", 1)[-1]

    @property
    def param_hint(self) -> str:
        """从仓库名里推断参数量，仅用于界面提示。"""
        m = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])", self.id)
        return f"{m.group(1)}B" if m else "?"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display": self.display,
            "downloads": self.downloads,
            "likes": self.likes,
            "tags": self.tags[:8],
            "last_modified": self.last_modified,
            "param_hint": self.param_hint,
            "siblings_loaded": self.siblings_loaded,
            "files": [
                {"path": f.path, "fname": f.fname, "size": f.size, "quant": f.quant}
                for f in self.gguf_files
            ],
        }


class HubError(RuntimeError):
    pass


class HubClient:
    def __init__(self, endpoint: str = "https://huggingface.co", token: str = "") -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=None),
            follow_redirects=True,
            verify=tls_verify(),
            headers={"User-Agent": "AetherNode/0.1"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- 内部 ----
    def _headers(self, extra: dict | None = None) -> dict:
        h: dict[str, str] = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def _raise(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code == 401:
            raise HubError(f"{what}: 需要访问令牌（401）。gated 模型请在设置里填写 HF token。")
        if resp.status_code == 404:
            raise HubError(f"{what}: 仓库或文件不存在（404）。")
        if resp.status_code >= 400:
            raise HubError(f"{what}: HTTP {resp.status_code} — {resp.text[:200]}")

    # ---- 检索 ----
    async def search(self, query: str, limit: int = 30, gguf_only: bool = True) -> list[RepoInfo]:
        params: dict[str, str | int] = {
            "search": query,
            "sort": "downloads",
            "direction": -1,
            "limit": limit,
        }
        if gguf_only:
            params["filter"] = "gguf"
        resp = await self._client.get(
            f"{self.endpoint}/api/models", params=params, headers=self._headers()
        )
        self._raise(resp, "模型搜索失败")
        out: list[RepoInfo] = []
        for item in resp.json():
            repo_id = item.get("id") or item.get("modelId") or ""
            if not repo_id:
                continue
            out.append(
                RepoInfo(
                    id=repo_id,
                    downloads=int(item.get("downloads") or 0),
                    likes=int(item.get("likes") or 0),
                    tags=list(item.get("tags") or []),
                    last_modified=str(item.get("lastModified") or ""),
                )
            )
        return out

    async def list_gguf_files(self, repo_id: str, revision: str = "main") -> list[GGUFFile]:
        url = f"{self.endpoint}/api/models/{repo_id}/tree/{revision}"
        # HF 的分页 limit 上限是 1000，超过会返回 400 Invalid limit
        resp = await self._client.get(
            url, params={"recursive": "true", "limit": 1000}, headers=self._headers()
        )
        self._raise(resp, f"读取 {repo_id} 文件树失败")
        files: list[GGUFFile] = []
        for item in resp.json():
            if item.get("type") != "file":
                continue
            path = str(item.get("path") or "")
            size = int(item.get("size") or 0)
            if path.lower().endswith(".gguf"):
                files.append(GGUFFile(path=path, size=size))
        files.sort(key=lambda f: f.size)
        return files

    async def enrich(self, repo: RepoInfo) -> RepoInfo:
        if repo.siblings_loaded:
            return repo
        repo.gguf_files = await self.list_gguf_files(repo.id)
        repo.siblings_loaded = True
        return repo

    # ---- 下载 ----
    async def download(
        self,
        repo_id: str,
        filename: str,
        dest_root: Path,
        on_progress=None,
        cancel: asyncio.Event | None = None,
    ) -> Path:
        """断点续传下载单个文件。on_progress(done, total, speed) 会被高频调用。"""
        dest_dir = dest_root / repo_id.replace("/", "__")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename.replace("/", "__")
        url = f"{self.endpoint}/{repo_id}/resolve/main/{quote(filename, safe='/')}"

        pos = dest.stat().st_size if dest.exists() else 0
        headers = self._headers({"Range": f"bytes={pos}-"} if pos else None)

        mode = "ab" if pos else "wb"
        async with self._client.stream(
            "GET", url, headers=headers, timeout=httpx.Timeout(30.0, read=None)
        ) as resp:
            if resp.status_code == 416:
                return dest  # 已完整
            if resp.status_code == 200 and pos:
                # 服务端忽略 Range，只能从头来
                pos, mode = 0, "wb"
            if resp.status_code >= 400:
                # 流式响应必须先 aread() 才能取 body
                detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise HubError(f"下载 {filename} 失败: HTTP {resp.status_code} — {detail}")

            total = pos + int(resp.headers.get("Content-Length") or 0)
            done = pos
            t0 = time.monotonic()
            last_emit = 0.0
            with dest.open(mode) as fh:
                async for chunk in resp.aiter_bytes(CHUNK):
                    if cancel is not None and cancel.is_set():
                        raise asyncio.CancelledError("下载已取消")
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if on_progress and (now - last_emit > 0.2 or done >= total):
                        speed = done / max(now - t0, 1e-6)
                        on_progress(done, total, speed)
                        last_emit = now
        return dest


# 无搜索结果时的兜底推荐，避免新用户面对空列表
CURATED: list[dict] = [
    {"id": "Qwen/Qwen2.5-1.5B-Instruct-GGUF", "note": "小体积，CPU 也能跑，适合首次验证"},
    {"id": "Qwen/Qwen2.5-7B-Instruct-GGUF", "note": "通用对话，12GB 显存可跑 Q4"},
    {"id": "bartowski/Qwen2.5-7B-Instruct-GGUF", "note": "量化齐全，Q4_K_M 质量最好"},
    {"id": "bartowski/Llama-3.2-3B-Instruct-GGUF", "note": "轻量多语言"},
    {"id": "microsoft/Phi-3.5-mini-instruct-gguf", "note": "微软官方 GGUF"},
    {"id": "bartowski/gemma-2-9b-it-GGUF", "note": "9B 需要 Q4 以下量化"},
]
