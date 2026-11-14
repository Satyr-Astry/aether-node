"""SQLite schema 迁移验证。

覆盖三条路径：
  全新库      —— 一次建到最新版本
  老库升级    —— 只有 v1 基础结构，增量补齐后续表与列
  残缺库自愈  —— 被人为删表后，基础结构幂等重建
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AETHER_HOME", tempfile.mkdtemp(prefix="aether-mig-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aether.config import load_settings  # noqa: E402

load_settings()

from aether.ledger import SCHEMA_VERSION, Ledger  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got  = {got}")
        print(f"       want = {want}")
        fails.append(name)


def tables(db: Path) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return sorted(n for (n,) in rows if not n.startswith("sqlite"))
    finally:
        conn.close()


def columns(db: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


# ---------- 1. 全新库 ----------
fresh = Path(tempfile.mkdtemp()) / "fresh.db"
L = Ledger(fresh)
check("fresh schema version", L.version, SCHEMA_VERSION)
check("fresh has all tables",
      tables(fresh), ["audits", "batches", "peer_credit", "requests", "schema_version"])
check("fresh requests has relay_bytes", "relay_bytes" in columns(fresh, "requests"), True)

# ---------- 2. 老库升级 ----------
old = Path(tempfile.mkdtemp()) / "old.db"
conn = sqlite3.connect(str(old))
conn.executescript("""
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (1);
CREATE TABLE requests (
  id TEXT PRIMARY KEY, req_id TEXT NOT NULL, ts REAL NOT NULL, role TEXT NOT NULL,
  self_node TEXT NOT NULL, peer_node TEXT, model TEXT NOT NULL,
  in_tok INTEGER NOT NULL DEFAULT 0, out_tok INTEGER NOT NULL DEFAULT 0,
  price_in REAL NOT NULL DEFAULT 0, price_out REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0, source TEXT NOT NULL,
  estimated INTEGER NOT NULL DEFAULT 0, receipt TEXT, batch_id TEXT,
  status TEXT NOT NULL DEFAULT 'ok');
""")
conn.commit()
conn.close()

check("old db starts at v1", "relay_bytes" not in columns(old, "requests"), True)

L2 = Ledger(old)
check("old db upgraded version", L2.version, SCHEMA_VERSION)
check("old db gained audits table", "audits" in tables(old), True)
check("old db gained peer_credit", "peer_credit" in tables(old), True)
check("old db gained relay_bytes", "relay_bytes" in columns(old, "requests"), True)

entry = L2.record(
    role="serve", self_node="a", peer_node="b", model="m",
    in_tok=3, out_tok=4, req_id="r1",
)
check("record works after upgrade", (entry["in_tok"], entry["out_tok"]), (3, 4))
check("summary works after upgrade", L2.summary()["total_entries"], 1)
L2.close()

# ---------- 3. 重复打开不重复迁移 ----------
L3 = Ledger(old)
check("reopen keeps version", L3.version, SCHEMA_VERSION)
check("reopen keeps data", L3.summary()["total_entries"], 1)
L3.close()

# ---------- 4. 残缺库自愈 ----------
broken = Path(tempfile.mkdtemp()) / "broken.db"
conn = sqlite3.connect(str(broken))
conn.executescript("""
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (3);
""")
conn.commit()
conn.close()

L4 = Ledger(broken)
check("broken db heals base tables", "requests" in tables(broken), True)
check("broken db keeps version", L4.version, SCHEMA_VERSION)
check("broken db usable", L4.summary()["total_entries"], 0)
L4.close()

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all migration checks passed")
