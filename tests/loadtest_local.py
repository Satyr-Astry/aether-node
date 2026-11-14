"""真实推理压测。

与 loadtest.py 的区别：上游是真正的 llama-server，而不是 mock。要验证的是
代理层在真实推理延迟（数百毫秒到数秒）下是否仍然行为正确——闸门、账本、
流式透传、inflight 归零。

并发刻意压得很低：单张 GPU、parallel_slots=1 时，高并发只会让请求在
llama-server 内部排队，测出来的是它的排队策略，不是代理层的问题。
"""

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("AETHER_HOME", os.environ.get("AETHER_HOME", ""))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

BASE = os.environ.get("LOAD_BASE", "http://127.0.0.1:19011")
MODEL = os.environ.get("LOAD_MODEL", "")
CONCURRENCY = int(os.environ.get("LOAD_CONCURRENCY", "4"))
TOTAL = int(os.environ.get("LOAD_TOTAL", "12"))

fails = []


def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        fails.append(name)


def _get(path: str):
    import json
    import urllib.request

    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as resp:
        return json.loads(resp.read())


async def _post_settings(client: httpx.AsyncClient, patch: dict) -> None:
    await client.post(f"{BASE}/api/settings", json=patch, timeout=10)


async def one(client: httpx.AsyncClient, idx: int, stream: bool) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": f"第 {idx} 问：用一句话回答。"}],
        "max_tokens": 24,
        "stream": stream,
    }
    started = time.monotonic()
    try:
        if stream:
            chunks = 0
            async with client.stream(
                "POST", f"{BASE}/v1/chat/completions", json=body, timeout=180
            ) as resp:
                code = resp.status_code
                async for _ in resp.aiter_raw():
                    chunks += 1
            return {
                "code": code,
                "dt": time.monotonic() - started,
                "stream": True,
                "chunks": chunks,
            }
        r = await client.post(f"{BASE}/v1/chat/completions", json=body, timeout=180)
        data = r.json() if r.status_code == 200 else {}
        return {
            "code": r.status_code,
            "dt": time.monotonic() - started,
            "stream": False,
            "usage": (data.get("usage") or {}),
            "route": r.headers.get("X-Aether-Route", ""),
        }
    except Exception as exc:
        return {"code": 0, "dt": time.monotonic() - started, "error": str(exc)[:120]}


async def main() -> int:
    summary = _get("/api/ledger/summary")
    print(f"起始账本条目: {summary['summary']['total_entries']}")
    rt = _get("/api/runtime")
    print(f"运行时状态: {rt.get('state')} 模型={rt.get('model')} 参数={rt.get('params')}")
    if rt.get("state") != "ready":
        print("模型尚未就绪，跳过")
        return 2

    before = summary["summary"]["total_entries"]
    sem = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=CONCURRENCY + 4)) as client:
        await _post_settings(client, {"api_max_concurrency": CONCURRENCY})

        async def worker(i: int, stream: bool):
            async with sem:
                return await one(client, i, stream)

        print(f"\n== 非流式：并发 {CONCURRENCY}，共 {TOTAL} 请求 ==")
        t0 = time.monotonic()
        results = await asyncio.gather(*(worker(i, False) for i in range(TOTAL)))
        elapsed = time.monotonic() - t0

        ok = [r for r in results if r["code"] == 200]
        lat = sorted(r["dt"] for r in ok)
        if lat:
            print(f"   成功 {len(ok)}/{TOTAL}，耗时 {elapsed:.1f}s，吞吐 {len(ok) / elapsed:.1f} req/s")
            print(
                f"   延迟 p50={statistics.median(lat):.2f}s "
                f"p95={lat[int(len(lat) * 0.95) - 1]:.2f}s max={lat[-1]:.2f}s"
            )
            tokens_in = sum(r["usage"].get("prompt_tokens", 0) for r in ok)
            tokens_out = sum(r["usage"].get("completion_tokens", 0) for r in ok)
            print(f"   token 合计: {tokens_in}+{tokens_out}")
        check("非流式全部成功", len(ok), TOTAL)
        check("全部路由到本机", {r["route"] for r in ok}, {"local"})

        after = _get("/api/ledger/summary")["summary"]["total_entries"]
        check("账本增量等于成功数", after - before, len(ok))

        print(f"\n== 流式：并发 {CONCURRENCY}，共 {max(4, TOTAL // 2)} 请求 ==")
        before2 = after
        n_stream = max(4, TOTAL // 2)
        results2 = await asyncio.gather(*(worker(i, True) for i in range(n_stream)))
        ok2 = [r for r in results2 if r["code"] == 200]
        for r in ok2:
            if r["chunks"] < 2:
                fails.append("流式分块过少")
                print(f"[FAIL] 流式分块过少: {r['chunks']}")
                break
        else:
            print(f"[PASS] 流式分块正常（最少 {min((r['chunks'] for r in ok2), default=0)} 块）")
        check("流式全部成功", len(ok2), n_stream)

        await asyncio.sleep(2)  # 等流式请求的收尾记账落库
        after2 = _get("/api/ledger/summary")["summary"]["total_entries"]
        check("流式请求也被记账", after2 - before2, len(ok2))

    # inflight 必须归零：非零说明有请求没走到释放路径
    import json
    import urllib.request

    with urllib.request.urlopen(f"{BASE}/metrics", timeout=10) as resp:
        text = resp.read().decode("utf-8")
    inflight = [
        line.split()[-1] for line in text.splitlines()
        if line.startswith("aether_inflight ")
    ]
    check("inflight 归零", inflight[0] if inflight else "missing", "0")

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all local-inference load checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
