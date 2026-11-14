"""按 token 计量的本地账本（SQLite）。

双视角记录：作为调用方消费时记 role=consume，作为提供方服务时记 role=serve。
同一条跨节点请求会在两边各产生一条记录，靠 req_id 对齐，供对账与结算使用。

v1 的计量是「双方自述 + 按 req_id 对账」，能发现记录不一致，
但无法证明提供方真的跑了声明的模型——那需要抽样复算或 TEE，不在本层。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from aether.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id          TEXT PRIMARY KEY,
    req_id      TEXT NOT NULL,
    ts          REAL NOT NULL,
    role        TEXT NOT NULL,
    self_node   TEXT NOT NULL,
    peer_node   TEXT,
    model       TEXT NOT NULL,
    in_tok      INTEGER NOT NULL DEFAULT 0,
    out_tok     INTEGER NOT NULL DEFAULT 0,
    price_in    REAL NOT NULL DEFAULT 0,
    price_out   REAL NOT NULL DEFAULT 0,
    amount      REAL NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,
    estimated   INTEGER NOT NULL DEFAULT 0,
    receipt     TEXT,
    batch_id    TEXT,
    status      TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_requests_req   ON requests(req_id);
CREATE INDEX IF NOT EXISTS idx_requests_ts    ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_batch ON requests(batch_id);
CREATE INDEX IF NOT EXISTS idx_requests_role  ON requests(role);

CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    from_ts     REAL NOT NULL,
    to_ts       REAL NOT NULL,
    count       INTEGER NOT NULL,
    total_in    INTEGER NOT NULL,
    total_out   INTEGER NOT NULL,
    amount      REAL NOT NULL,
    merkle_root TEXT NOT NULL,
    path        TEXT
);

CREATE TABLE IF NOT EXISTS peer_credit (
    node_id     TEXT PRIMARY KEY,
    receivable  REAL NOT NULL DEFAULT 0,
    payable     REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audits (
    id          TEXT PRIMARY KEY,
    req_id      TEXT NOT NULL,
    ts          REAL NOT NULL,
    model       TEXT NOT NULL,
    target_node TEXT NOT NULL,
    verifier    TEXT,
    original_in INTEGER NOT NULL DEFAULT 0,
    original_out INTEGER NOT NULL DEFAULT 0,
    verified_in INTEGER NOT NULL DEFAULT 0,
    verified_out INTEGER NOT NULL DEFAULT 0,
    similarity  REAL NOT NULL DEFAULT 0,
    verdict     TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audits_ts ON audits(ts);
CREATE INDEX IF NOT EXISTS idx_audits_target ON audits(target_node);
"""


def compute_amount(in_tok: int, out_tok: int, price_in: float, price_out: float) -> float:
    """按每百万 token 的单价折算。"""
    return round(in_tok / 1_000_000 * price_in + out_tok / 1_000_000 * price_out, 9)


class Ledger:
    def __init__(self, db_path: Path | None = None) -> None:
        settings = get_settings()
        self._path = db_path or (settings.home / "ledger.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def path(self) -> str:
        return str(self._path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 写入 ----------
    def record(
        self,
        *,
        role: str,
        self_node: str,
        peer_node: str | None,
        model: str,
        in_tok: int,
        out_tok: int,
        price_in: float = 0.0,
        price_out: float = 0.0,
        source: str = "local",
        estimated: bool = False,
        receipt: str | None = None,
        req_id: str | None = None,
        status: str = "ok",
        ts: float | None = None,
    ) -> dict:
        rid = uuid.uuid4().hex
        req = req_id or uuid.uuid4().hex
        stamp = ts if ts is not None else time.time()
        amount = compute_amount(in_tok, out_tok, price_in, price_out)
        row = (
            rid, req, stamp, role, self_node, peer_node, model, int(in_tok), int(out_tok),
            float(price_in), float(price_out), amount, source, 1 if estimated else 0,
            receipt, None, status,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests (id, req_id, ts, role, self_node, peer_node, model,"
                " in_tok, out_tok, price_in, price_out, amount, source, estimated,"
                " receipt, batch_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            if peer_node and not peer_node.startswith("external:"):
                # role=serve 表示我提供服务，对方欠我；role=consume 反之。
                # 外部 API 不是网络节点，只记账不产生互相债务。
                if role == "serve":
                    self._conn.execute(
                        "INSERT INTO peer_credit (node_id, receivable, payable, updated_at)"
                        " VALUES (?, ?, 0, ?) ON CONFLICT(node_id) DO UPDATE SET"
                        " receivable = receivable + ?, updated_at = ?",
                        (peer_node, amount, stamp, amount, stamp),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO peer_credit (node_id, receivable, payable, updated_at)"
                        " VALUES (?, 0, ?, ?) ON CONFLICT(node_id) DO UPDATE SET"
                        " payable = payable + ?, updated_at = ?",
                        (peer_node, amount, stamp, amount, stamp),
                    )
            self._conn.commit()
        return {
            "id": rid, "req_id": req, "ts": stamp, "role": role, "model": model,
            "in_tok": int(in_tok), "out_tok": int(out_tok), "amount": amount,
            "source": source, "estimated": estimated, "status": status,
        }

    # ---------- 读取 ----------
    def entries(
        self, limit: int = 100, role: str | None = None, since: float | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM requests WHERE 1=1"
        args: list = []
        if role:
            sql += " AND role = ?"
            args.append(role)
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(int(limit), 2000)))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "req_id": r["req_id"], "ts": r["ts"], "role": r["role"],
            "peer_node": r["peer_node"], "model": r["model"], "in_tok": r["in_tok"],
            "out_tok": r["out_tok"], "amount": r["amount"], "source": r["source"],
            "estimated": bool(r["estimated"]), "receipt": r["receipt"],
            "batch_id": r["batch_id"], "status": r["status"],
        }

    def summary(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, COUNT(*) n, SUM(in_tok) i, SUM(out_tok) o, SUM(amount) a,"
                " SUM(estimated) est FROM requests GROUP BY role"
            ).fetchall()
            total = self._conn.execute("SELECT COUNT(*) n FROM requests").fetchone()["n"]
            unsettled = self._conn.execute(
                "SELECT COUNT(*) n FROM requests WHERE batch_id IS NULL"
            ).fetchone()["n"]
        out = {
            "total_entries": total,
            "unsettled": unsettled,
            "consume": {"count": 0, "in_tok": 0, "out_tok": 0, "amount": 0.0, "estimated": 0},
            "serve": {"count": 0, "in_tok": 0, "out_tok": 0, "amount": 0.0, "estimated": 0},
        }
        for r in rows:
            out[r["role"]] = {
                "count": r["n"] or 0,
                "in_tok": r["i"] or 0,
                "out_tok": r["o"] or 0,
                "amount": round(r["a"] or 0.0, 6),
                "estimated": r["est"] or 0,
            }
        return out

    def credit(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM peer_credit ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "node_id": r["node_id"],
                "receivable": round(r["receivable"], 6),
                "payable": round(r["payable"], 6),
                "net": round(r["receivable"] - r["payable"], 6),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def unsettled(self, limit: int = 5000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE batch_id IS NULL ORDER BY ts ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(r) for r in rows]

    # ---------- 对账 ----------
    def reconcile(self, req_ids: list[str]) -> dict:
        """按 req_id 对比两侧记录。

        只对跨节点记录对账：本地自服务没有对手方，天然只有一条，不参与比对。
        """
        if not req_ids:
            return {"checked": 0, "matched": 0, "mismatched": [], "missing": []}
        marks = ",".join("?" * len(req_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE req_id IN ({marks})"
                " AND peer_node IS NOT NULL",
                req_ids,
            ).fetchall()
        grouped: dict[str, dict[str, dict]] = {}
        for r in rows:
            grouped.setdefault(r["req_id"], {})[r["role"]] = self._row(r)
        matched = 0
        mismatched: list[dict] = []
        missing: list[dict] = []
        for req_id, pair in grouped.items():
            consume = pair.get("consume")
            serve = pair.get("serve")
            if not consume or not serve:
                missing.append({"req_id": req_id, "has": sorted(pair.keys())})
                continue
            same = (
                consume["in_tok"] == serve["in_tok"]
                and consume["out_tok"] == serve["out_tok"]
            )
            if same:
                matched += 1
            else:
                mismatched.append({
                    "req_id": req_id,
                    "consume": [consume["in_tok"], consume["out_tok"]],
                    "serve": [serve["in_tok"], serve["out_tok"]],
                })
        return {
            "checked": len(grouped),
            "matched": matched,
            "mismatched": mismatched[:20],
            "missing": missing[:20],
        }

    # ---------- 内部 ----------
    def attach_batch(self, entry_ids: list[str], batch_id: str) -> None:
        if not entry_ids:
            return
        marks = ",".join("?" * len(entry_ids))
        with self._lock:
            self._conn.execute(
                f"UPDATE requests SET batch_id = ? WHERE id IN ({marks})",
                [batch_id, *entry_ids],
            )
            self._conn.commit()

    def batches(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batches ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": r["id"], "ts": r["ts"], "from_ts": r["from_ts"], "to_ts": r["to_ts"],
                "count": r["count"], "total_in": r["total_in"], "total_out": r["total_out"],
                "amount": r["amount"], "merkle_root": r["merkle_root"], "path": r["path"],
            }
            for r in rows
        ]

    def save_batch(self, batch: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO batches (id, ts, from_ts, to_ts, count, total_in, total_out,"
                " amount, merkle_root, path) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    batch["id"], batch["ts"], batch["from_ts"], batch["to_ts"],
                    batch["count"], batch["total_in"], batch["total_out"],
                    batch["amount"], batch["merkle_root"], batch["path"],
                ),
            )
            self._conn.commit()

    # ---------- 审计 ----------
    def add_audit(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audits (id, req_id, ts, model, target_node, verifier,"
                " original_in, original_out, verified_in, verified_out, similarity,"
                " verdict, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["req_id"], row["ts"], row["model"], row["target_node"],
                    row.get("verifier") or "", int(row.get("original_in") or 0),
                    int(row.get("original_out") or 0), int(row.get("verified_in") or 0),
                    int(row.get("verified_out") or 0), float(row.get("similarity") or 0),
                    row["verdict"], (row.get("detail") or "")[:1000],
                ),
            )
            self._conn.commit()

    def audits(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audits ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [
            {
                "id": r["id"], "req_id": r["req_id"], "ts": r["ts"], "model": r["model"],
                "target_node": r["target_node"], "verifier": r["verifier"],
                "original_in": r["original_in"], "original_out": r["original_out"],
                "verified_in": r["verified_in"], "verified_out": r["verified_out"],
                "similarity": r["similarity"], "verdict": r["verdict"], "detail": r["detail"],
            }
            for r in rows
        ]

    def audit_stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT verdict, COUNT(*) n FROM audits GROUP BY verdict"
            ).fetchall()
        out = {"pass": 0, "suspect": 0, "skipped": 0, "error": 0, "total": 0}
        for r in rows:
            out[r["verdict"]] = r["n"]
            out["total"] += r["n"]
        return out


_ledger: Ledger | None = None


def get_ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger()
    return _ledger
