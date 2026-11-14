"""推理后端抽象验证：契约、注册表、别名与内置实现。

重点不是测 llama.cpp 能不能跑（那由 e2e 脚本覆盖），而是验证
「换后端不需要改调用方」这个前提成立。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-be-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether import backends  # noqa: E402
from aether.backends import BackendError, InferenceBackend  # noqa: E402
from aether.config import load_settings  # noqa: E402

load_settings()

fails = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


# ---------------- 注册表 ----------------
check("llama_cpp registered", "llama_cpp" in backends.available(), True)
check("describe has one backend", len(backends.describe()), 1)
check("describe reports instantiated flag", backends.describe()[0]["instantiated"], False)

# 别名解析
for alias in ("llama.cpp", "llamacpp", "llama-server", "llama_cpp"):
    obj = backends.get(alias)
    check(f"alias {alias!r} resolves", obj is backends.get("llama_cpp"), True)

check("registry returns singleton", backends.get() is backends.get("llama_cpp"), True)
check("describe now instantiated", backends.describe()[0]["instantiated"], True)

# 未知后端应有明确错误
try:
    backends.create("no-such-backend")
    check("unknown backend raises", False, True)
except BackendError as exc:
    check("unknown backend raises BackendError", "未知后端" in str(exc), True)

# ---------------- 契约 ----------------
rt = backends.get("llama_cpp")
check("runtime satisfies Protocol", isinstance(rt, InferenceBackend), True)
for attr in ("name", "state", "base_url", "port"):
    check(f"runtime exposes {attr}", hasattr(rt, attr), True)
for method in ("start", "stop", "status"):
    check(f"runtime exposes {method}()", callable(getattr(rt, method, None)), True)

st = rt.status()
check("status is dict", isinstance(st, dict), True)
check("status has state", "state" in st, True)
check("idle by default", st.get("state"), "idle")
check("base_url empty when idle", rt.base_url, "")
check("port zero when idle", rt.port, 0)

# ---------------- 自定义后端可插拔 ----------------
class DummyBackend:
    name = "dummy"
    state = "ready"

    def __init__(self):
        self.port = 18080

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    async def start(self, model, force_ctx=None):
        self.state = "ready"
        return self.status()

    async def stop(self):
        self.state = "idle"

    def status(self):
        return {"state": self.state, "model": "dummy", "port": self.port, "error": "", "params": {}}


backends.register("dummy", DummyBackend)
check("custom backend registered", "dummy" in backends.available(), True)
dummy = backends.create("dummy")
check("custom backend satisfies Protocol", isinstance(dummy, InferenceBackend), True)
check("custom backend base_url", dummy.base_url, "http://127.0.0.1:18080")
check("custom backend is separate instance", backends.create("dummy") is not dummy, True)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all backend checks passed")
