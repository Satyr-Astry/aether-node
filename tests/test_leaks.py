"""长期运行下的资源边界验证。

秒级压测发现不了慢速泄漏。这里针对三处「只增不减」的结构做定向验证：
  - 隧道请求声明字节表：超时/失败路径必须清理
  - relay 防重放表：低频场景下也要有确定上界
  - 下载任务列表：长期运行会一直累积已完成任务
"""

import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-leak-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether import downloads as dl  # noqa: E402
from aether.config import load_settings  # noqa: E402
from aether.identity import Identity  # noqa: E402
from aether.p2p.relay_server import MemoryRelay  # noqa: E402
from aether.p2p.tunnel import TunnelManager  # noqa: E402

load_settings()

fails = []


def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


# ---------- 1. 隧道声明字节表 ----------
class NullClient:
    async def send_raw(self, text):
        return 1


class Boom(Exception):
    pass


async def tunnel_case():
    ident = Identity(secrets.token_hex(32))
    tm = TunnelManager(ident, NullClient(), lambda: "")

    async def explode(text):
        raise Boom("send failed")

    tm.client.send_raw = explode

    # 连续 20 次失败请求：每次内部都会新建 req_id，finally 必须把它清掉，
    # 否则表会单调增长
    for _ in range(20):
        try:
            async for _ in tm._request_once("target", "POST", "/p", "body", "biz"):
                pass
        except Exception:
            pass

    return tm


tm = asyncio.run(tunnel_case())
check("pending table not growing after 20 failures", len(tm._pending), 0)
check("declared bytes not growing after 20 failures", len(tm._declared_bytes), 0)


# ---------- 2. relay 防重放表 ----------
r = MemoryRelay()
r._seen = {f"id{i}": 1_000_000 + i for i in range(6000)}
r._trim_seen()
check("seen trimmed to hard limit", len(r._seen), 5000)
# 保留的应是最新的一批
remaining = sorted(r._seen.values())
check("trim keeps newest", remaining[0], 1_000_000 + 1000)

# 速率窗口按小时清理
r2 = MemoryRelay()
r2._rate = {"stale": {"start": 0.0, "count": 5.0}, "fresh": {"start": 9e12, "count": 1.0}}
r2._prune_requests()
check("stale rate window dropped", "stale" in r2._rate, False)
check("fresh rate window kept", "fresh" in r2._rate, True)


# ---------- 3. 下载任务列表 ----------
class FakeLibrary:
    def register(self, repo, file, path):
        raise AssertionError("不应执行到注册")


async def downloads_case():
    mgr = dl.DownloadManager(FakeLibrary())
    mgr.MAX_TASKS = 10
    # 直接造 30 条已完成任务，再触发一次 prune
    for i in range(30):
        task = dl.DownloadTask(id=f"t{i}", repo="r", file=f"f{i}")
        task.status = "done"
        task.started_at = float(i)
        mgr.tasks[task.id] = task
    mgr._prune()
    return mgr


mgr = asyncio.run(downloads_case())
check("downloads pruned to cap", len(mgr.tasks), 10)
check("newest tasks retained", "t29" in mgr.tasks, True)
check("oldest tasks dropped", "t0" in mgr.tasks, False)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all leak-bound checks passed")
