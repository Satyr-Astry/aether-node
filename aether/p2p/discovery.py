"""发现表：维护网络内其他节点的服务广告。"""

from __future__ import annotations

import time
from collections import deque

from aether.events import bus


class PeerTable:
    def __init__(self, self_pubkey: str) -> None:
        self.self_pubkey = self_pubkey
        self._peers: dict[str, dict] = {}
        self.own_ad: dict | None = None
        self.accepted = 0
        self.rejected = 0
        self.reject_log: deque = deque(maxlen=40)

    # ---- 写入 ----
    def ingest(self, ad: dict, relay: str) -> bool:
        node_id = str(ad.get("node_id") or "")
        if not node_id:
            self.reject("缺少 node_id", relay)
            return False
        if node_id == self.self_pubkey:
            self.own_ad = ad
            return False
        ts = int(ad.get("ts") or 0)
        current = self._peers.get(node_id)
        if current and int(current["ad"].get("ts") or 0) >= ts:
            return False  # 已有更新的版本，忽略回声
        is_new = current is None
        self._peers[node_id] = {
            "node_id": node_id,
            "ad": ad,
            "received_at": time.time(),
            "relay": relay,
        }
        self.accepted += 1
        bus.publish("peer.update", {
            "node_id": node_id,
            "name": ad.get("name") or node_id[:8],
            "models": [m.get("id") for m in ad.get("models") or []],
            "new": is_new,
            "relay": relay,
        })
        return True

    def reject(self, reason: str, relay: str) -> None:
        self.rejected += 1
        self.reject_log.append({"reason": reason, "relay": relay, "ts": time.time()})
        bus.publish("peer.reject", {"reason": reason, "relay": relay})

    def forget(self, node_id: str) -> None:
        self._peers.pop(node_id, None)

    def prune(self) -> int:
        now = time.time()
        removed = 0
        for node_id, entry in list(self._peers.items()):
            ttl = int(entry["ad"].get("ttl") or 900)
            age = now - int(entry["ad"].get("ts") or 0)
            if age > ttl * 2:
                del self._peers[node_id]
                removed += 1
        return removed

    # ---- 读取 ----
    def raw(self) -> list[dict]:
        return list(self._peers.values())

    def get(self, node_id: str) -> dict | None:
        return self._peers.get(node_id)

    def peers(self) -> list[dict]:
        now = time.time()
        out: list[dict] = []
        for entry in self._peers.values():
            ad = entry["ad"]
            age = round(now - int(ad.get("ts") or 0), 1)
            out.append({
                "node_id": entry["node_id"],
                "short_id": entry["node_id"][:12],
                "name": ad.get("name") or entry["node_id"][:8],
                "npub": ad.get("npub") or "",
                "endpoint": ad.get("endpoint") or "",
                "reachability": ad.get("reachability") or "unknown",
                "region": ad.get("region") or "",
                "models": ad.get("models") or [],
                "price": ad.get("price") or {},
                "age": age,
                "ttl": int(ad.get("ttl") or 900),
                "relay": entry["relay"],
                "stale": age > int(ad.get("ttl") or 900),
                "reachable": bool(ad.get("endpoint")),
            })
        out.sort(key=lambda p: (p["stale"], p["age"]))
        return out

    def stats(self) -> dict:
        return {
            "peers": len(self._peers),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "recent_rejects": list(self.reject_log)[-8:],
        }
