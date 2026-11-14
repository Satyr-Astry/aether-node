"""安全基线验证：默认只监听回环、共享模式强制鉴权、老配置迁移。

用 TestClient 的 client.host 是 "testclient"，不是回环地址，
所以它天然扮演"外部访问者"，正好用来验证鉴权分支。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-sec-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether.config import get_settings, load_settings, save_settings  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


# ---------------- 配置层 ----------------
s = load_settings()
check("default binds loopback only", s.effective_host(), "127.0.0.1")
check("default share disabled", s.share_enabled, False)
check("default has no api key", s.api_key, "")

shared = s.model_copy(update={"share_enabled": True})
shared = shared.model_copy(update={"api_key": shared.ensure_api_key()})
save_settings(shared)
check("share enabled binds all interfaces", shared.effective_host(), "0.0.0.0")
check("share enabled auto-generates key", len(shared.api_key) >= 24, True)

# 老配置迁移
home2 = tempfile.mkdtemp(prefix="aether-mig-")
Path(home2, "settings.json").write_text(
    json.dumps({"api_host": "0.0.0.0", "api_port": 8737}), encoding="utf-8"
)
os.environ["AETHER_HOME"] = home2
from aether import config as cfg  # noqa: E402

cfg._settings = None
migrated = cfg.load_settings()
check("legacy 0.0.0.0 migrates to share", migrated.share_enabled, True)
check("legacy host field normalised", migrated.api_host, "127.0.0.1")
check("legacy still binds all interfaces", migrated.effective_host(), "0.0.0.0")
check("legacy gets a key", len(migrated.api_key) >= 24, True)
check("legacy key persisted",
      bool(json.loads(Path(home2, "settings.json").read_text(encoding="utf-8"))["api_key"]), True)

# ---------------- HTTP 鉴权层 ----------------
os.environ["AETHER_HOME"] = tempfile.mkdtemp(prefix="aether-http-")
cfg._settings = None
cfg.load_settings()

from fastapi.testclient import TestClient  # noqa: E402

from aether.api.app import create_app  # noqa: E402

app = create_app()
client = TestClient(app, raise_server_exceptions=False)

# 默认（未开启共享）：外部访问也放行，因为只监听回环，外部根本连不上
r = client.get("/api/health")
check("local-only mode allows requests", r.status_code, 200)

# 开启共享后：无 key 被拒
cur = cfg.get_settings()
cfg.save_settings(cur.model_copy(update={"share_enabled": True, "api_key": "test-key-1234567890"}))
r = client.get("/api/models")
check("shared mode rejects without key", r.status_code, 401)

r = client.get("/api/models", headers={"X-Api-Key": "wrong"})
check("shared mode rejects wrong key", r.status_code, 401)

r = client.get("/api/models", headers={"X-Api-Key": "test-key-1234567890"})
check("shared mode accepts correct key", r.status_code, 200)

r = client.get("/api/models", headers={"Authorization": "Bearer test-key-1234567890"})
check("shared mode accepts bearer", r.status_code, 200)

# /v1 同样受保护
r = client.get("/v1/models")
check("shared mode protects /v1", r.status_code, 401)
r = client.get("/v1/models", headers={"X-Api-Key": "test-key-1234567890"})
check("shared mode allows /v1 with key", r.status_code, 200)

# 静态资源不受影响（GUI 本身公开）
r = client.get("/")
check("static gui still served", r.status_code, 200)

# 关闭共享后恢复放行
cur = cfg.get_settings()
cfg.save_settings(cur.model_copy(update={"share_enabled": False}))
r = client.get("/api/models")
check("disabling share restores access", r.status_code, 200)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all security checks passed")
