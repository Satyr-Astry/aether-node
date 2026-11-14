"""路由：把本地已加载的模型与网络内其他节点的模型统一成一份目录并择优。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from aether.config import get_settings
from aether.llama_server import LlamaRuntime
from aether.library import ModelLibrary
from aether.p2p.discovery import PeerTable

REACH_BONUS = {"public": 40.0, "lan": 32.0, "relay-only": -18.0, "unknown": 0.0}


@dataclass
class Route:
    model_id: str
    kind: str  # local | remote | relay
    base_url: str
    node_id: str
    node_name: str
    score: float = 0.0
    ctx: int = 0
    note: str = ""
    price: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "kind": self.kind,
            "base_url": self.base_url,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "score": round(self.score, 2),
            "ctx": self.ctx,
            "note": self.note,
        }


def _peer_host(peer: dict) -> str:
    """同主机判定用的标识。

    有端点就看主机名；没有端点（relay-only）退回 node_id——那种情况本来就
    只能走隧道，聚在一起也不构成刷分优势。
    """
    endpoint = str(peer.get("endpoint") or "")
    if endpoint:
        try:
            from urllib.parse import urlparse

            host = urlparse(endpoint).hostname or ""
            if host:
                return host
        except Exception:
            pass
    return str(peer.get("node_id") or "")


class Router:
    def __init__(self, runtime: LlamaRuntime, library: ModelLibrary, peers: PeerTable) -> None:
        self.runtime = runtime
        self.library = library
        self.peers = peers
        self._last_route: dict | None = None

    @property
    def settings(self):
        """每次动态取全局设置。

        构造时缓存会在 save_settings 之后失效——外部端点、优先级等改动
        必须立刻生效，否则用户改完配置发现路由还是老样子。
        """
        return get_settings()

    # ---- 本地 ----
    def _local_routes(self, loaded_only: bool = True) -> list[Route]:
        routes: list[Route] = []
        if self.runtime.state != "ready" or not self.runtime.model:
            return routes
        m = self.runtime.model
        routes.append(Route(
            model_id=m.display,
            kind="local",
            base_url=self.runtime.base_url,
            node_id="local",
            node_name="local",
            score=1000.0,
            ctx=int(self.runtime.params.get("ctx") or 0),
            note=f"{self.runtime.params.get('ngl')} 层 / ctx {self.runtime.params.get('ctx')}",
            price={"in": 0.0, "out": 0.0},
        ))
        return routes

    # ---- 远端 ----
    def _remote_routes(self, model_id: str) -> list[Route]:
        routes: list[Route] = []
        peers = self.peers.peers()
        # 女巫防护：一个 IP 后面挂多个身份是最常见的刷分形态。
        # 按簇大小降权，让独立主机排在前面，但不禁用——同机多实例是合理用法。
        host_counts: dict[str, int] = {}
        for p in peers:
            host = _peer_host(p)
            host_counts[host] = host_counts.get(host, 0) + 1

        for peer in peers:
            endpoint = peer.get("endpoint") or ""
            reach = peer.get("reachability") or "unknown"
            cluster = host_counts.get(_peer_host(peer), 1)
            sybil_penalty = -6.0 * (cluster - 1)
            for model in peer.get("models") or []:
                if str(model.get("id")) != model_id:
                    continue
                score = REACH_BONUS.get(reach, 0.0)
                age = float(peer.get("age") or 0)
                score += max(0.0, 30.0 - age / 30.0)
                price = peer.get("price") or {}
                score -= float(price.get("out") or 0) * 5
                score += sybil_penalty
                extra = {"size_gb": model.get("size_gb"), "gpu": model.get("gpu"), "cluster": cluster}
                suffix = f" | 同主机 {cluster} 个身份" if cluster > 1 else ""

                if endpoint and reach != "relay-only":
                    routes.append(Route(
                        model_id=model_id,
                        kind="remote",
                        base_url=endpoint.rstrip("/"),
                        node_id=peer["node_id"],
                        node_name=peer.get("name") or peer["short_id"],
                        score=score,
                        ctx=int(model.get("ctx") or 0),
                        note=f"{reach} / 广告龄 {age:.0f}s{suffix}",
                        price=price,
                        # 目标若要求鉴权，只有预先交换过 key 的节点才能直连；
                        # 没配 key 时会拿到 401，调用方应改用隧道
                        extra={
                            **extra,
                            "api_key": (self.settings.peer_keys or {}).get(peer["node_id"], ""),
                        },
                    ))
                elif reach == "relay-only":
                    # 无直达端点：走应用层 relay 隧道，流量过 relay 所以扣分
                    routes.append(Route(
                        model_id=model_id,
                        kind="relay",
                        base_url="",
                        node_id=peer["node_id"],
                        node_name=peer.get("name") or peer["short_id"],
                        score=score,
                        ctx=int(model.get("ctx") or 0),
                        note=f"经 relay 隧道 / 广告龄 {age:.0f}s{suffix}",
                        price=price,
                        extra=extra,
                    ))
        routes.sort(key=lambda r: r.score, reverse=True)
        return routes

    # ---- 决策 ----
    def resolve(self, model_id: str | None) -> Route | None:
        """按优先级返回一条可用路由：本机已加载 > 本机其它 > 网络内最优。"""
        local = self._local_routes()
        if model_id == "" or model_id is None:
            if local and self.settings.prefer_local:
                return local[0]
            candidates: list[Route] = []
            for peer in self.peers.peers():
                for model in peer.get("models") or []:
                    candidates.extend(self._remote_routes(str(model.get("id"))))
            return candidates[0] if candidates else (local[0] if local else None)

        if self.settings.prefer_local:
            for r in local:
                if r.model_id == model_id:
                    return r
        remote = self._remote_routes(model_id)
        if remote and (self.settings.auto_route or not local):
            return remote[0]
        for r in local:
            if r.model_id == model_id:
                return r
        return remote[0] if remote else None

    def _external_routes(self, model_id: str) -> list[Route]:
        """自有外部 API 端点。它们不是网络节点，没有广告与可达性问题，
        但同样参与打分与计量——这样「我的 API」和「别人的节点」是同一个入口。"""
        routes: list[Route] = []
        base_score = float(self.settings.external_score or 38.0)
        for provider in self.settings.external_providers or []:
            endpoint = str(provider.get("endpoint") or "").rstrip("/")
            if not endpoint:
                continue
            models = [str(m) for m in (provider.get("models") or []) if m]
            if model_id and model_id not in models:
                continue
            price = provider.get("price") or {}
            penalty = float(price.get("out") or 0) * 2
            for name in models or [model_id]:
                routes.append(Route(
                    model_id=name,
                    kind="external",
                    base_url=endpoint,
                    node_id="external:" + str(provider.get("name") or "api"),
                    node_name=str(provider.get("name") or "外部 API"),
                    score=base_score - penalty,
                    price=price,
                    note="自有外部端点",
                    extra={"api_key": provider.get("api_key") or ""},
                ))
        routes.sort(key=lambda r: r.score, reverse=True)
        return routes

    def candidates(self, model_id: str) -> list[Route]:
        """按优先级返回所有可用候选，供调用方逐个重试。

        顺序：本机已加载 > 自有外部端点 > 网络直连 > relay 隧道。
        外部端点排在本机之后、网络之前，因为它由自己配置、可用性最可控。
        """
        out: list[Route] = []
        for r in self._local_routes():
            if not model_id or r.model_id == model_id:
                out.append(r)
        out.extend(self._external_routes(model_id))
        out.extend(self._remote_routes(model_id))
        return out

    def catalog(self) -> list[dict]:
        """GUI 用的统一模型目录，标注来源与可用性。"""
        seen: dict[str, dict] = {}
        for m in self.library.all():
            seen[m.display] = {
                "id": m.display,
                "kind": "local",
                "ctx": int((m.meta or {}).get("context_length") or 0),
                "size_gb": round(m.size / (1 << 30), 2),
                "loaded": m.loaded and self.runtime.state == "ready",
                "key": m.key,
                "sources": 1,
                "note": "本机模型库",
            }
        for peer in self.peers.peers():
            reach = peer.get("reachability") or "unknown"
            if not peer.get("endpoint") and reach != "relay-only":
                continue
            for model in peer.get("models") or []:
                mid = str(model.get("id"))
                if not mid:
                    continue
                if mid in seen:
                    seen[mid]["sources"] = int(seen[mid].get("sources") or 1) + 1
                    continue
                seen[mid] = {
                    "id": mid,
                    "kind": "remote",
                    "ctx": int(model.get("ctx") or 0),
                    "size_gb": model.get("size_gb"),
                    "loaded": True,
                    "key": None,
                    "sources": 1,
                    "note": (
                        f"来自 {peer.get('name')}（经 relay 隧道）"
                        if reach == "relay-only"
                        else f"来自 {peer.get('name')}"
                    ),
                }
        return sorted(seen.values(), key=lambda x: (x["kind"] != "local", x["id"]))

    def record(self, route: Route) -> None:
        self._last_route = route.to_dict()

    @property
    def last_route(self) -> dict | None:
        return self._last_route
