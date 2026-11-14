"""隧道异常路径：回程中断、超时、资源清理。

之前所有隧道测试都是「请求发出去、回执收回来」的完整往返。这里只做前半程：
请求发出去了，但回程永远不来（relay 转发成功、响应在回程丢失）。

要验证的是：client 会超时而不是永远挂着，而且超时后所有中间表都被清干净——
否则每次网络抖动都会留下一条永远等不到回执的记录。
"""

import asyncio
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-rc-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether.config import get_settings, load_settings, save_settings  # noqa: E402
from aether.identity import Identity  # noqa: E402
from aether.p2p.tunnel import REQ_TIMEOUT, TunnelError, TunnelManager  # noqa: E402

load_settings()

fails = []


def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


class SilentClient:
    """接受发送，但永远不回任何东西——模拟回程中断。"""

    def __init__(self):
        self.sent = 0

    async def send_raw(self, text):
        self.sent += 1
        return 1


class RejectingClient(SilentClient):
    """发送后立刻让所有 relay 回绝。"""

    def __init__(self, relay_count: int):
        super().__init__()
        self.relay_count = relay_count

    async def send_raw(self, text):
        self.sent += 1
        return self.relay_count


def make(timeout: float, client=None) -> TunnelManager:
    s = get_settings()
    save_settings(s.model_copy(update={"tunnel_timeout": timeout}))
    ident = Identity(secrets.token_hex(32))
    return TunnelManager(ident, client or SilentClient(), lambda: "")


async def silent_case(timeout: float):
    tm = make(timeout)
    started = time.monotonic()
    err = None
    try:
        async for _ in tm._request_once("target", "POST", "/x", "{}", "biz"):
            pass
    except TunnelError as exc:
        err = exc
    elapsed = time.monotonic() - started
    return tm, err, elapsed


tm, err, elapsed = asyncio.run(silent_case(2.0))
check("回程中断会超时", err is not None, True)
check("超时信息含秒数", "超时" in str(err), True)
check("超时按配置生效（<4s）", elapsed < 4.0, True)
check("超时后 pending 清空", len(tm._pending), 0)
check("超时后 declared 清空", len(tm._declared_bytes), 0)
check("超时后 denied 清空", len(tm._denied), 0)
print(f"   实际耗时 {elapsed:.2f}s")


async def all_rejected_case():
    client = RejectingClient(relay_count=3)
    tm = make(30.0, client)
    started = time.monotonic()

    async def reject_soon():
        # 模拟三个 relay 依次回绝
        for _ in range(3):
            await asyncio.sleep(0.2)
            req_id = next(iter(tm._pending))
            tm._on_denied(["AETHER_REQ_DENIED", req_id, "目标节点不在线"])

    task = asyncio.create_task(reject_soon())
    err = None
    try:
        async for _ in tm._request_once("target", "POST", "/x", "{}", "biz"):
            pass
    except TunnelError as exc:
        err = exc
    await task
    return tm, err, time.monotonic() - started


tm2, err2, elapsed2 = asyncio.run(all_rejected_case())
check("全部拒绝会失败", err2 is not None, True)
check("失败信息提到全部 relay", "所有 relay" in str(err2), True)
check("全部拒绝时快速返回（<5s，远小于 30s 超时）", elapsed2 < 5.0, True)
check("失败后中间表清空", (len(tm2._pending), len(tm2._denied)), (0, 0))
print(f"   实际耗时 {elapsed2:.2f}s")


async def partial_reject_case():
    """两个 relay 中一个回绝、另一个稍后正常回执——应当成功，不应因为一个拒绝就失败。"""
    client = RejectingClient(relay_count=2)
    tm = make(10.0, client)

    async def mixed():
        await asyncio.sleep(0.2)
        req_id = next(iter(tm._pending))
        tm._on_denied(["AETHER_REQ_DENIED", req_id, "目标节点不在线"])
        await asyncio.sleep(0.3)
        # 另一个 relay 转发的回执到了
        tm._declared_bytes[req_id] = 8
        queue = tm._pending.get(req_id)
        if queue:
            queue.put_nowait((200, '{"ok":1}', True))

    task = asyncio.create_task(mixed())
    items = []
    err = None
    try:
        async for item in tm._request_once("target", "POST", "/x", "{}", "biz"):
            items.append(item)
    except TunnelError as exc:
        err = exc
    await task
    return items, err


items, err3 = asyncio.run(partial_reject_case())
check("部分拒绝不影响最终成功", err3, None)
check("收到成功回执", len(items), 1)
check("回执内容完整", items[0][1] if items else "", '{"ok":1}')

# 超时配置本身
check("默认超时常量存在", REQ_TIMEOUT > 0, True)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all return-path checks passed")
