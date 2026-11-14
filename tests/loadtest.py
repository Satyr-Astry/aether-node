"""并发与长稳压测。

用 mock 外部端点作为上游，避免推理本身成为瓶颈——这样测的是代理层的
并发控制、账本写入与连接管理，而不是 GPU 吞吐。

两个场景：
  A. 并发远高于闸门 —— 验证超出的请求被快速拒绝（429），而不是排队拖死
  B. 闸门放宽 —— 测吞吐、延迟分布与账本条数是否与成功请求数一致
"""

import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-load-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from aether.config import get_settings, load_settings, save_settings  # noqa: E402

load_settings()

BASE = os.environ.get("LOAD_BASE", "http://127.0.0.1:19001")
MODEL = os.environ.get("LOAD_MODEL", "mock-model")

fails = []


def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        fails.append(name)


async def one(client: httpx.AsyncClient, idx: int) -> tuple[int, float]:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": f"ping {idx}"}],
        "max_tokens": 16,
        "stream": False,
    }
    started = time.monotonic()
    try:
        r = await client.post(f"{BASE}/v1/chat/completions", json=body, timeout=60)
        return r.status_code, time.monotonic() - started
    except Exception:
        return 0, time.monotonic() - started


async def burst(concurrency: int, total: int) -> tuple[dict, list[float]]:
    codes: dict[int, int] = {}
    latencies: list[float] = []
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=concurrency + 10)) as client:
        async def worker(i: int):
            async with sem:
                code, dt = await one(client, i)
                codes[code] = codes.get(code, 0) + 1
                latencies.append(dt)

        await asyncio.gather(*(worker(i) for i in range(total)))
    return codes, latencies


def ledger_count() -> int:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{BASE}/api/ledger/summary", timeout=10) as resp:
            import json

            return json.loads(resp.read())["summary"]["total_entries"]
    except Exception:
        return -1


async def set_concurrency(n: int) -> None:
    """改的是**服务端**的配置。

    早先版本直接调本地 save_settings，那只改了压测脚本自己进程内的配置对象，
    服务进程读的是它自己内存里的设置，根本没变——测出来的闸门行为全是错的。
    """
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{BASE}/api/settings", json={"api_max_concurrency": n})


async def main() -> int:
    # ---------- 场景 A：闸门收紧，验证快速失败 ----------
    await set_concurrency(2)
    print("== A. 闸门=2，并发 20，共 40 请求 ==")
    before = ledger_count()
    codes_a, lat_a = await burst(concurrency=20, total=40)
    after = ledger_count()
    print("   状态码分布:", dict(sorted(codes_a.items())))
    print(f"   账本增量: {after - before}")
    ok_a = codes_a.get(200, 0) + codes_a.get(429, 0)
    check("A. 全部请求都有明确结果", ok_a, 40)
    check("A. 出现 429（闸门生效）", codes_a.get(429, 0) > 0, True)
    check("A. 成功请求都被记账", after - before, codes_a.get(200, 0))

    # ---------- 场景 B：闸门放宽，测吞吐 ----------
    await set_concurrency(64)
    print("\n== B. 闸门=64，并发 20，共 200 请求 ==")
    before = ledger_count()
    t0 = time.monotonic()
    codes_b, lat_b = await burst(concurrency=20, total=200)
    elapsed = time.monotonic() - t0
    after = ledger_count()

    ok = codes_b.get(200, 0)
    lat_sorted = sorted(lat_b)
    p50 = statistics.median(lat_sorted)
    p95 = lat_sorted[int(len(lat_sorted) * 0.95) - 1]
    print(f"   状态码分布: {dict(sorted(codes_b.items()))}")
    print(f"   耗时 {elapsed:.1f}s，吞吐 {ok / elapsed:.1f} req/s")
    print(f"   延迟 p50={p50 * 1000:.0f}ms p95={p95 * 1000:.0f}ms max={lat_sorted[-1] * 1000:.0f}ms")
    print(f"   账本增量: {after - before}")

    check("B. 无 5xx", sum(v for k, v in codes_b.items() if k >= 500), 0)
    check("B. 全部成功", ok, 200)
    check("B. 账本与成功数一致", after - before, ok)
    check("B. p95 在合理范围（<5s）", p95 < 5.0, True)

    # ---------- 场景 C：顺序请求，验证数字稳定 ----------
    print("\n== C. 顺序 20 请求 ==")
    await set_concurrency(4)
    before = ledger_count()
    codes_c, lat_c = await burst(concurrency=1, total=20)
    after = ledger_count()
    print(f"   状态码分布: {dict(sorted(codes_c.items()))}，账本增量 {after - before}")
    check("C. 顺序请求全部成功", codes_c.get(200, 0), 20)
    check("C. 账本一致", after - before, 20)

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all load checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
