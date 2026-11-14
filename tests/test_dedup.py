"""隧道重复转发去重验证。

场景：调用方把 AETHER_REQ 广播给所有 relay。若两个 relay 都认识目标节点，
同一个请求会被转发两遍——目标节点会重复烧算力。这里验证目标端按 req_id 去重，
并且过期条目会被清理，不会变成无界增长。
"""

import asyncio
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-dup-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether.config import load_settings  # noqa: E402
from aether.identity import Identity  # noqa: E402
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


class CountingClient:
    def __init__(self):
        self.sent = []

    async def send_raw(self, text):
        self.sent.append(text)
        return 1


async def case() -> TunnelManager:
    ident = Identity(secrets.token_hex(32))
    client = CountingClient()
    tm = TunnelManager(ident, client, lambda: "")  # 没有本地后端，走 503 分支

    fwd = ["AETHER_FWD", "req-abc", "caller-node", "POST", "/v1/chat/completions", "{}", "biz-1"]
    await tm._on_forward(list(fwd))
    await tm._on_forward(list(fwd))  # 第二个 relay 转发的同一请求
    await tm._on_forward(list(fwd))
    return tm


tm = asyncio.run(case())
check("only first forward counted", tm.stats["forwarded_in"], 1)
check("duplicates counted separately", tm.stats["duplicate_forward"], 2)
check("handled table has one entry", len(tm._handled), 1)
check("no extra replies sent", len(tm.client.sent), 1)  # 只有第一次回了 503


async def different_ids() -> TunnelManager:
    ident = Identity(secrets.token_hex(32))
    tm = TunnelManager(ident, CountingClient(), lambda: "")
    for i in range(3):
        fwd = ["AETHER_FWD", f"req-{i}", "caller", "POST", "/x", "{}", f"biz-{i}"]
        await tm._on_forward(list(fwd))
    return tm


tm2 = asyncio.run(different_ids())
check("different req_ids all processed", tm2.stats["forwarded_in"], 3)
check("no false duplicates", tm2.stats["duplicate_forward"], 0)


async def expiry() -> TunnelManager:
    ident = Identity(secrets.token_hex(32))
    tm = TunnelManager(ident, CountingClient(), lambda: "")
    fwd = ["AETHER_FWD", "old-req", "caller", "POST", "/x", "{}", "biz"]
    await tm._on_forward(list(fwd))
    # 手工把记录时间推到 10 分钟前
    tm._handled["old-req"] = time.time() - 600
    fwd2 = ["AETHER_FWD", "new-req", "caller", "POST", "/x", "{}", "biz2"]
    await tm._on_forward(list(fwd2))
    return tm


tm3 = asyncio.run(expiry())
check("expired entries pruned", "old-req" not in tm3._handled, True)
check("fresh entry kept", "new-req" in tm3._handled, True)


# 模拟转发两遍时只有第一次产生回执
async def replies() -> CountingClient:
    ident = Identity(secrets.token_hex(32))
    client = CountingClient()
    tm = TunnelManager(ident, client, lambda: "")
    fwd = ["AETHER_FWD", "r-1", "caller", "POST", "/x", "{}", "b"]
    await tm._on_forward(list(fwd))
    first = len(client.sent)
    await tm._on_forward(list(fwd))
    second = len(client.sent)
    print(f"   回执数: 第一次 {first}，重复转发后 {second}")
    return client


client = asyncio.run(replies())
check("duplicate did not send again", len(client.sent), 1)

# 回执内容应是一次 503（没有本地后端）
payload = json.loads(client.sent[0])
check("reply is AETHER_RES", payload[0], "AETHER_RES")
check("reply is 503", payload[2], 503)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all dedup checks passed")
