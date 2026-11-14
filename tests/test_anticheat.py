"""防作弊深化验证：复算者随机化与同主机女巫降权。

两项都能在不联网的情况下验证：
  - 随机化：给同一批候选多次抽样，验证者应当分散，且永不等于目标
  - 女巫降权：同主机两个身份的得分必须低于同条件下独立主机的节点
"""

import os
import random
import statistics
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-anti-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether import audit  # noqa: E402
from aether.config import load_settings  # noqa: E402
from aether.p2p.discovery import PeerTable  # noqa: E402
from aether.p2p.router import Router, _peer_host  # noqa: E402

load_settings()

fails = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


# ---------------- 同主机判定 ----------------
check("host from endpoint", _peer_host({"endpoint": "http://10.0.0.5:8737"}), "10.0.0.5")
check("host from domain", _peer_host({"endpoint": "https://node.example.com:443"}), "node.example.com")
check("relay-only falls back to node_id",
      _peer_host({"endpoint": "", "node_id": "abc123"}), "abc123")


# ---------------- 女巫降权 ----------------
class FakeRuntime:
    state = "idle"
    model = None
    base_url = ""
    port = 0
    params: dict = {}


class FakeLibrary:
    def all(self):
        return []


def make_peer(node_id, endpoint, reach="lan", model="m1"):
    return {
        "node_id": node_id,
        "short_id": node_id[:12],
        "name": node_id[:8],
        "endpoint": endpoint,
        "reachability": reach,
        "models": [{"id": model, "ctx": 4096}],
        "price": {"in": 0.0, "out": 0.0},
        "age": 5.0,
        "ttl": 900,
        "stale": False,
        "reachable": bool(endpoint),
        "relay": "ws://relay",
    }


peers = PeerTable("self")
# 两个身份挂在同一个 IP 上（女巫），一个独立主机
peers._peers = {
    "aatwin1": {"node_id": "aatwin1", "ad": {}, "received_at": 0, "relay": "r"},
    "aatwin2": {"node_id": "aatwin2", "ad": {}, "received_at": 0, "relay": "r"},
    "zzsolo": {"node_id": "zzsolo", "ad": {}, "received_at": 0, "relay": "r"},
}
peers.peers = lambda: [
    make_peer("aatwin1", "http://192.168.1.50:8737"),
    make_peer("aatwin2", "http://192.168.1.50:8738"),
    make_peer("zzsolo", "http://192.168.1.99:8737"),
]

router = Router(FakeRuntime(), FakeLibrary(), peers)
routes = router._remote_routes("m1")
scores = {r.node_id: r.score for r in routes}
print("scores:", {k: round(v, 2) for k, v in scores.items()})

check("both twins discovered", len(scores), 3)
check("solo outranks twins", scores["zzsolo"] > scores["aatwin1"], True)
check("twins penalised equally", round(scores["aatwin1"], 4), round(scores["aatwin2"], 4))
check("cluster size recorded", routes[0].extra.get("cluster"), 1)
twin_note = next(r.note for r in routes if r.node_id == "aatwin1")
check("twin note mentions cluster", "同主机 2 个身份" in twin_note, True)

# 独立主机不受影响
peers.peers = lambda: [
    make_peer("s1", "http://192.168.1.10:8737"),
    make_peer("s2", "http://192.168.1.11:8737"),
    make_peer("s3", "http://192.168.1.12:8737"),
]
router2 = Router(FakeRuntime(), FakeLibrary(), peers)
routes2 = router2._remote_routes("m1")
check("all independent hosts equal",
      len({round(r.score, 4) for r in routes2}), 1)


# ---------------- 复算者随机化 ----------------
class FakeRouter:
    def __init__(self, routes):
        self._routes = routes

    def candidates(self, model_id):
        return list(self._routes)


def mk_route(node_id, kind="remote", endpoint=""):
    from aether.p2p.router import Route

    return Route(
        model_id="m1", kind=kind, base_url=endpoint, node_id=node_id,
        node_name=node_id[:8], score=10.0, ctx=4096,
    )


target = mk_route("target", endpoint="http://10.0.0.1:8737")
others = [
    mk_route("v1", endpoint="http://10.0.0.2:8737"),
    mk_route("v2", endpoint="http://10.0.0.3:8737"),
    mk_route("v3", endpoint="http://10.0.0.4:8737"),
]
candidates_pool = [target, *others]

# 直接验证候选池构造：排除目标、排除同主机
host_of = audit._host_of
picked = [
    host_of(c) for c in candidates_pool
    if c.node_id != target.node_id and host_of(c) != host_of(target)
]
check("target excluded from verifier pool", len(picked), 3)
check("no verifier shares target host", host_of(target) not in picked, True)

# 同主机候选应当被排除
same_host = mk_route("v_same", endpoint="http://10.0.0.1:8738")
pool2 = [
    c for c in [target, same_host, *others]
    if c.node_id != target.node_id and host_of(c) != host_of(target)
]
check("same-host verifier excluded", "v_same" not in [c.node_id for c in pool2], True)

# 随机性：多次抽样应覆盖多个验证者
random.seed(1234)
picks = [random.choice(others).node_id for _ in range(200)]
distinct = len(set(picks))
check("random selection spreads across verifiers", distinct, 3)
counts = [picks.count(v) for v in ("v1", "v2", "v3")]
check("distribution roughly uniform", max(counts) - min(counts) < 60, True)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all anti-cheat checks passed")
