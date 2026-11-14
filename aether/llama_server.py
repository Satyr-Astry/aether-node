"""llama.cpp 运行时：二进制探测/下载、子进程生命周期、参数自动适配。

参数通过解析 `--help` 得到的支持列表做过滤，避免 llama.cpp 频繁改参时启动失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
import time
import zipfile
from pathlib import Path

import httpx

from aether.config import get_settings
from aether.events import bus
from aether.gguf import GGUFMeta
from aether.hardware import detect_gpus, recommend
from aether.library import LocalModel
from aether.net import tls_verify

GITHUB_REPO = "ggml-org/llama.cpp"
FLAG_RE = re.compile(r"--[a-z0-9][a-z0-9-]*")


class RuntimeError_(RuntimeError):
    pass


def _pick_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaRuntime:
    # 后端标识，InferenceBackend 契约要求
    name = "llama_cpp"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.proc: asyncio.subprocess.Process | None = None
        self.model: LocalModel | None = None
        self.port: int = 0
        self.state: str = "idle"  # idle | starting | ready | failed | stopping
        self.last_error: str = ""
        self.started_at: float = 0.0
        self._flags: set[str] | None = None
        self._log_task: asyncio.Task | None = None
        self._boot_log: list[str] = []
        self.params: dict = {}

    # ---------- 二进制 ----------
    def binary_path(self) -> Path | None:
        if self.settings.llama_bin:
            p = Path(self.settings.llama_bin)
            if p.exists():
                return p
        root = self.settings.runtime_path
        if not root.exists():
            return None
        for p in root.rglob("llama-server.exe"):
            return p
        for p in root.rglob("llama-server"):
            return p
        return None

    async def _latest_release(self, client: httpx.AsyncClient) -> tuple[str, list[dict]]:
        r = await client.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=10",
            headers={"User-Agent": "AetherNode/0.1"},
        )
        r.raise_for_status()
        for rel in r.json():
            assets = rel.get("assets") or []
            if any("win" in (a.get("name") or "") and a["name"].startswith("llama-") for a in assets):
                return rel["tag_name"], assets
        raise RuntimeError_("未找到含 Windows 二进制的 llama.cpp release")

    async def ensure_binary(self, use_cuda: bool = True) -> Path:
        existing = self.binary_path()
        if existing:
            return existing

        self.settings.runtime_path.mkdir(parents=True, exist_ok=True)
        dest = self.settings.runtime_path / "llama"
        dest.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=None),
            follow_redirects=True,
            verify=tls_verify(),
        ) as client:
            tag, assets = await self._latest_release(client)
            bus.publish("runtime.download", {"msg": f"准备下载 llama.cpp {tag}"})

            names = {a["name"]: a for a in assets}
            plan: list[str] = []
            if use_cuda:
                # 主包带版本号（llama-b12375-bin-win-cuda-12.4-x64.zip），
                # 但 CUDA 运行时包不带（cudart-llama-bin-win-cuda-12.4-x64.zip），
                # 所以不能靠字符串替换推导，得按候选名逐个匹配。
                for ver in ("12.4", "12.8", "13.5"):
                    main = f"llama-{tag}-bin-win-cuda-{ver}-x64.zip"
                    if main not in names:
                        continue
                    plan.append(main)
                    for cud in (f"cudart-llama-bin-win-cuda-{ver}-x64.zip", f"cudart-{main}"):
                        if cud in names:
                            plan.append(cud)
                            break
                    break
            if not plan:
                cand = f"llama-{tag}-bin-win-cpu-x64.zip"
                if cand not in names:
                    raise RuntimeError_("release 中既无 CUDA 也无 CPU 的 Windows 包")
                plan = [cand]

            for fname in plan:
                asset = names.get(fname)
                if not asset:
                    continue
                total = int(asset.get("size") or 0)
                target = dest.parent / fname
                bus.publish("runtime.download", {"msg": f"下载 {fname}", "total": total})
                done = 0
                t0 = time.monotonic()
                async with client.stream("GET", asset["browser_download_url"]) as resp:
                    resp.raise_for_status()
                    with target.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(1 << 20):
                            fh.write(chunk)
                            done += len(chunk)
                            if time.monotonic() - t0 > 0.5:
                                bus.publish("runtime.download", {
                                    "msg": f"下载 {fname}",
                                    "done": done, "total": total,
                                    "pct": round(done / total * 100, 1) if total else 0,
                                })
                                t0 = time.monotonic()
                with zipfile.ZipFile(target) as zf:
                    zf.extractall(dest)
                target.unlink(missing_ok=True)

        found = self.binary_path()
        if not found:
            raise RuntimeError_("解压后未找到 llama-server.exe")
        bus.publish("runtime.download", {"msg": f"运行时就绪：{found.name}", "done": 1, "total": 1, "pct": 100})
        return found

    # ---------- 能力探测 ----------
    async def list_devices(self, binary: Path) -> list[str]:
        """探测 llama.cpp 实际能用的加速后端。

        光有 ggml-cuda.dll 不够——缺 CUDA 运行时 DLL 时后端初始化会失败，
        此时 --list-devices 只返回 (none)。必须实测，否则会给出"已全量卸载到 GPU"
        这种错误结论。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                str(binary), "--list-devices",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        except Exception:
            return []
        devices: list[str] = []
        for raw in out.decode("utf-8", "replace").splitlines():
            line = raw.strip()
            if not line or line.lower().startswith(("available", "load_backend", "warning", "error")):
                continue
            if any(tag in line.upper() for tag in ("CUDA", "VULKAN", "SYCL", "HIP", "METAL", "OPENCL")):
                devices.append(line)
        return devices

    async def supported_flags(self, binary: Path) -> set[str]:
        if self._flags is not None:
            return self._flags
        try:
            proc = await asyncio.create_subprocess_exec(
                str(binary), "--help",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            self._flags = set(FLAG_RE.findall(out.decode("utf-8", "replace")))
        except Exception:
            self._flags = set()
        return self._flags

    def _build_args(self, binary: Path, model: LocalModel, ctx: int, ngl: int, port: int) -> list[str]:
        known = self._flags or set()
        threads = max(1, min(32, (os.cpu_count() or 8) // 2))

        args: list[str] = [str(binary), "-m", model.path]
        args += ["--host", "127.0.0.1", "--port", str(port)]
        args += ["-c", str(ctx)]
        args += ["-ngl", str(ngl)]
        args += ["-t", str(threads)]
        if self.settings.parallel_slots > 1 and "--parallel" in known:
            args += ["--parallel", str(self.settings.parallel_slots)]
        if "--alias" in known:
            args += ["--alias", model.display]
        for flag in ("--no-webui", "--jinja", "--cont-batching"):
            if flag in known:
                args.append(flag)
        # 不主动传 --flash-attn：新版要求带值（on|off|auto），旧版不带值，
        # 两种写法互不兼容，交给 llama.cpp 的默认值最稳。
        return args

    # ---------- 生命周期 ----------
    async def start(self, model: LocalModel, force_ctx: int | None = None) -> dict:
        if self.state in ("starting", "ready"):
            await self.stop()

        binary = await self.ensure_binary(use_cuda=bool(detect_gpus()))
        await self.supported_flags(binary)

        gpus = detect_gpus()
        gpu = gpus[0] if gpus else None
        meta = GGUFMeta(**model.meta) if model.meta else GGUFMeta()
        plan = recommend(model.size, meta, gpu, want_ctx=force_ctx or self.settings.context_limit)

        ctx = int(force_ctx or plan["ctx"])
        ngl = self.settings.gpu_layers if self.settings.gpu_layers >= 0 else int(plan["ngl"])
        port = _pick_port()

        # 光有 GPU 不够：缺 CUDA 运行时 DLL 时 ggml-cuda 后端初始化会失败，
        # 必须实测设备列表，否则会误报"已全量卸载到 GPU"而实际跑在 CPU 上。
        devices = await self.list_devices(binary)
        note = plan.get("note", "")
        if ngl != 0 and not devices:
            ngl = 0
            note = ("llama.cpp 未探测到可用 GPU 后端（通常是缺 CUDA 运行时 DLL），"
                    "已回落到 CPU 推理。可在「运行时」页点「安装 / 修复 llama.cpp」补齐")
        elif devices:
            note = f"{note} | 后端 {devices[0]}"

        args = self._build_args(binary, model, ctx, ngl, port)
        self.params = {
            "ctx": ctx,
            "ngl": ngl,
            "port": port,
            "note": note,
            "size_gb": round(model.size / (1 << 30), 2),
            "devices": devices[:2],
        }
        self.state = "starting"
        self.model = model
        self.port = port
        self.last_error = ""
        self._boot_log = []
        bus.publish("runtime.state", {"state": self.state, "model": model.display, **self.params})
        bus.publish("runtime.log", {"line": "$ " + " ".join(args), "stream": "cmd"})

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(binary.parent),
            )
        except OSError as exc:
            self.state = "failed"
            self.last_error = str(exc)
            bus.publish("runtime.state", {"state": self.state, "error": self.last_error})
            raise RuntimeError_(f"启动 llama-server 失败：{exc}") from exc

        self._log_task = asyncio.create_task(self._pump_logs())
        try:
            await self._wait_ready(timeout=240)
        except Exception as exc:
            self.last_error = str(exc)
            await self.stop(keep_error=True)
            # stop(keep_error) 不重置为 idle，这里必须显式落到 failed，
            # 否则状态机会永远停在 stopping
            self.state = "failed"
            bus.publish("runtime.state", {"state": "failed", "error": self.last_error})
            raise
        self.state = "ready"
        self.started_at = time.time()
        bus.publish("runtime.state", {"state": "ready", "model": model.display, **self.params})
        return self.status()

    async def _pump_logs(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self._boot_log.append(text)
            if len(self._boot_log) > 400:
                self._boot_log.pop(0)
            bus.publish("runtime.log", {"line": text, "stream": "out"})

    async def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.port}/v1/models"
        async with httpx.AsyncClient(timeout=3.0) as client:
            while time.monotonic() < deadline:
                if self.proc and self.proc.returncode is not None:
                    tail = "\n".join(self._boot_log[-8:])
                    raise RuntimeError_(f"llama-server 提前退出（code {self.proc.returncode}）\n{tail}")
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.7)
        raise RuntimeError_("llama-server 启动超时（240s）。可能是模型过大或显存不足，请查看运行日志。")

    async def stop(self, keep_error: bool = False) -> None:
        self.state = "stopping"
        proc = self.proc
        self.proc = None
        if self._log_task:
            self._log_task.cancel()
            self._log_task = None
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        self.model = None
        if not keep_error:
            self.state = "idle"
            bus.publish("runtime.state", {"state": "idle"})

    def status(self) -> dict:
        return {
            "state": self.state,
            "model": self.model.display if self.model else None,
            "model_key": self.model.key if self.model else None,
            "port": self.port,
            "uptime": round(time.time() - self.started_at, 1) if self.started_at and self.state == "ready" else 0,
            "params": self.params,
            "error": self.last_error,
            "binary": str(self.binary_path() or ""),
            "log_tail": self._boot_log[-30:],
        }

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}" if self.port else ""


runtime = LlamaRuntime()
