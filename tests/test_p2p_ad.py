"""P2P 广告层测试：签名、事件封装、验签、篡改检测、发现表。"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether.config import load_settings  # noqa: E402

load_settings()

from aether.identity import Identity, get_identity  # noqa: E402
from aether.p2p import ad  # noqa: E402
from aether.p2p.discovery import PeerTable  # noqa: E402

failures = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got!r}")
        print(f"       want = {want!r}")
        failures.append(name)


ident = get_identity()
print(f"node_id = {ident.pubkey}")
print(f"npub    = {ident.npub}")

# 1) 身份持久化：重新加载应得到同一公钥
again = Identity.load_or_create()
check("identity persisted", again.pubkey, ident.pubkey)

# 2) pubkey 长度
check("pubkey length", len(ident.pubkey), 64)

# 3) 广告构建与签名
payload = ad.build_ad(
    ident,
    name="test-node",
    endpoint="http://192.168.1.10:8737",
    reachability="lan",
    models=[{"id": "Qwen2.5-7B-Instruct-GGUF:Q4_K_M", "ctx": 8192, "size_gb": 4.7}],
    price={"in": 0.1, "out": 0.3, "unit": "USD/1M"},
    region="cn",
)
check("ad has sig", "sig" in payload and len(payload["sig"]) == 128, True)
check("ad node_id matches", payload["node_id"], ident.pubkey)

# 4) 内层签名验证
check("ad inner sig valid", Identity.verify_json(ident.pubkey, payload), True)

# 5) 事件封装
event = ad.build_event(ident, payload)
check("event kind", event["kind"], ad.KIND_AD)
check("event id length", len(event["id"]), 64)
check("event sig length", len(event["sig"]), 128)

# 6) 事件验签
check("verify_event ok", ad.verify_event(event), None)

# 7) 篡改 content 必须被拒
tampered = dict(event)
tampered["content"] = tampered["content"].replace("test-node", "evil-node")
err = ad.verify_event(tampered)
check("tampered content rejected", err is not None, True)
print(f"       reason: {err}")

# 8) 篡改 events 的 sig 必须被拒
tampered2 = dict(event)
tampered2["sig"] = ("00" * 64)
check("tampered sig rejected", ad.verify_event(tampered2) is not None, True)

# 9) parse_ad 正常路径
parsed, err = ad.parse_ad(event)
check("parse_ad ok", err, None)
check("parse_ad models", len(parsed["models"]), 1)

# 10) TTL 过期
old = dict(payload)
old["ts"] = int(time.time()) - 10000
old["sig"] = ""
old = ident.sign_json({k: v for k, v in old.items() if k != "sig"})
old_event = ad.build_event(ident, old)
old_event["created_at"] = int(time.time()) - 10000
# 重新计算 id 与签名以保持事件自洽
old_event["id"] = ad.event_id(
    ident.pubkey, old_event["created_at"], old_event["kind"], old_event["tags"], old_event["content"]
)
old_event["sig"] = ident.sign_digest(bytes.fromhex(old_event["id"])).hex()
parsed_old, err_old = ad.parse_ad(old_event)
check("expired ad rejected", err_old is not None, True)
print(f"       reason: {err_old}")

# 11) 发现表：接受他人广告
other = Identity(os.urandom(32).hex())
other_ad = ad.build_ad(
    other, name="peer-b", endpoint="http://10.0.0.5:8737", reachability="lan",
    models=[{"id": "m1", "ctx": 4096}],
)
table = PeerTable(ident.pubkey)
check("ingest peer ad", table.ingest(other_ad, "ws://relay"), True)
check("peer count", len(table.peers()), 1)
check("peer name", table.peers()[0]["name"], "peer-b")
check("peer reachable", table.peers()[0]["reachable"], True)

# 12) 同一条重复投递不重复计数
check("duplicate ingest ignored", table.ingest(other_ad, "ws://relay2"), False)

# 13) 自己的广告不入表
self_ad = ad.build_ad(ident, name="me", endpoint="http://127.0.0.1:8737",
                      reachability="relay-only", models=[])
check("self ad not ingested", table.ingest(self_ad, "ws://relay"), False)
check("own_ad captured", table.own_ad is not None, True)

# 14) 过期清理
table._peers[next(iter(table._peers))]["ad"]["ts"] = int(time.time()) - 5000
for p in table.peers():
    p["stale"] = True
check("prune removes stale peer", table.prune(), 1)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all p2p ad checks passed")
