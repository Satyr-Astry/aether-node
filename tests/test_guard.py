"""防护验证：隧道速率限制、请求重放拦截、并发闸门。

用 TestClient 的 WebSocket 走真实协议路径，两个身份互发消息。
"""

import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-guard-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether.config import get_settings, load_settings, save_settings  # noqa: E402

load_settings()
save_settings(get_settings().model_copy(update={
    "relay_rate_limit": 3,
    "relay_replay_window": 300,
    "share_enabled": False,
}))

from fastapi.testclient import TestClient  # noqa: E402

from aether.api.app import create_app  # noqa: E402
from aether.identity import Identity  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


app = create_app()
client = TestClient(app)

caller = Identity(secrets.token_hex(32))
target = Identity(secrets.token_hex(32))


def hello_msg(ident: Identity) -> str:
    ts = int(time.time())
    sig = ident.sign_bytes(f"aether-hello:{ident.pubkey}:{ts}".encode()).hex()
    return json.dumps(["AETHER_HELLO", ident.pubkey, ts, sig])


def req_msg(caller_ident: Identity, req_id: str, target_pub: str, biz_id: str | None = None) -> str:
    method, path, body = "POST", "/v1/chat/completions", '{"model":"m"}'
    biz = biz_id or req_id
    sig = caller_ident.sign_bytes(
        f"{req_id}:{target_pub}:{method}:{path}:{body}:{biz}".encode()
    ).hex()
    return json.dumps(
        ["AETHER_REQ", req_id, target_pub, method, path, body, int(time.time()), sig, biz]
    )


with client.websocket_connect("/relay") as ws_caller, client.websocket_connect("/relay") as ws_target:
    ws_caller.send_text(hello_msg(caller))
    check("caller hello", json.loads(ws_caller.receive_text())[0], "AETHER_HELLO_OK")

    ws_target.send_text(hello_msg(target))
    check("target hello", json.loads(ws_target.receive_text())[0], "AETHER_HELLO_OK")

    # 1) 正常请求应被转发到目标
    rid = secrets.token_hex(8)
    ws_caller.send_text(req_msg(caller, rid, target.pubkey))
    fwd = json.loads(ws_target.receive_text())
    check("first request forwarded", fwd[0], "AETHER_FWD")
    check("forwarded biz id carried", fwd[6], rid)

    # 2) 同一 req_id 重放必须被拒
    ws_caller.send_text(req_msg(caller, rid, target.pubkey))
    denied = json.loads(ws_caller.receive_text())
    check("replay rejected", denied[0], "AETHER_REQ_DENIED")
    check("replay reason mentions replay", "重放" in denied[2], True)

    # 3) 速率限制：配额 3，已用 1（重放不计数），再发 2 次后第 4 次应被拒
    for _ in range(2):
        new_id = secrets.token_hex(8)
        ws_caller.send_text(req_msg(caller, new_id, target.pubkey))
        json.loads(ws_target.receive_text())  # 目标收到 FWD
    over_id = secrets.token_hex(8)
    ws_caller.send_text(req_msg(caller, over_id, target.pubkey))
    limited = json.loads(ws_caller.receive_text())
    check("rate limit kicks in", limited[0], "AETHER_REQ_DENIED")
    check("rate reason mentions limit", "速率" in limited[2], True)

    # 4) 签名错误的请求必须被拒
    bad = json.loads(req_msg(caller, secrets.token_hex(8), target.pubkey))
    bad[7] = "00" * 64
    ws_caller.send_text(json.dumps(bad))
    bad_resp = json.loads(ws_caller.receive_text())
    check("bad signature rejected", bad_resp[0], "AETHER_REQ_DENIED")
    check("bad signature reason", "签名" in bad_resp[2], True)

# 5) 未注册身份就发请求
with client.websocket_connect("/relay") as ws_anon:
    ws_anon.send_text(req_msg(caller, secrets.token_hex(8), target.pubkey))
    anon_resp = json.loads(ws_anon.receive_text())
    check("unregistered caller rejected", anon_resp[0], "AETHER_REQ_DENIED")
    check("unregistered reason mentions hello", "身份" in anon_resp[2], True)

# 6) 并发闸门
from aether.api import routes_openai  # noqa: E402

save_settings(get_settings().model_copy(update={"api_max_concurrency": 1}))
routes_openai._inflight = 0
check("gate admits first", routes_openai._enter(), True)
check("gate rejects second", routes_openai._enter(), False)
routes_openai._leave()
check("gate frees after leave", routes_openai._enter(), True)
routes_openai._leave()

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all guard checks passed")
