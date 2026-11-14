"""账本接口：流水、汇总、信用、批次、对账与凭证校验。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from aether import settlement
from aether.api.state import get_state

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


@router.get("/summary")
async def summary() -> dict:
    state = get_state()
    return {
        "summary": state.ledger.summary(),
        "credit": state.ledger.credit(),
        "db": state.ledger.path,
        "schema_version": state.ledger.version,
    }


@router.get("/entries")
async def entries(limit: int = 100, role: str | None = None) -> dict:
    state = get_state()
    return {"entries": state.ledger.entries(limit=limit, role=role)}


@router.get("/credit")
async def credit() -> dict:
    return {"credit": get_state().ledger.credit()}


@router.get("/batches")
async def batches(limit: int = 50) -> dict:
    return {"batches": get_state().ledger.batches(limit=limit)}


@router.post("/settle")
async def settle(payload: dict | None = None) -> dict:
    role = (payload or {}).get("role")
    try:
        return settlement.settle(role=role)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"结算失败: {exc}") from exc


@router.post("/reconcile")
async def reconcile(payload: dict | None = None) -> dict:
    """按 req_id 对账：本地 consume 与 serve 两侧的 token 数是否一致。"""
    state = get_state()
    ids = (payload or {}).get("req_ids")
    if not ids:
        ids = [e["req_id"] for e in state.ledger.entries(limit=500)]
    return state.ledger.reconcile(list(ids)[:500])


@router.post("/verify")
async def verify(payload: dict) -> dict:
    path = (payload or {}).get("path") or ""
    if not path:
        raise HTTPException(status_code=400, detail="需要 path")
    try:
        return settlement.verify_file(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"凭证不存在: {path}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"校验失败: {exc}") from exc


@router.get("/proof/{batch_id}")
async def proof(batch_id: str) -> dict:
    state = get_state()
    for b in state.ledger.batches(limit=200):
        if b["id"] == batch_id:
            path = Path(b["path"] or "")
            if not path.exists():
                raise HTTPException(status_code=404, detail="凭证文件已丢失")
            import json

            return json.loads(path.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="批次不存在")


# ---------------- 审计（抽样复算） ----------------

@router.get("/audits")
async def audits(limit: int = 50) -> dict:
    state = get_state()
    return {"audits": state.ledger.audits(limit=limit), "stats": state.ledger.audit_stats()}


@router.post("/audit/probe")
async def audit_probe(payload: dict | None = None) -> dict:
    """手动复算：对同一请求向所有提供者各跑一次，两两比对输出一致性。

    这是审计机制的自检入口——不用等抽样命中，也不用构造作弊节点，
    直接看同模型的两个提供者是否给出相同答案。
    """
    import uuid as _uuid

    from aether import audit
    from aether.config import get_settings
    from aether.p2p import upstream

    data = payload or {}
    model = data.get("model") or ""
    if not model:
        raise HTTPException(status_code=400, detail="需要 model")
    messages = data.get("messages") or [
        {"role": "user", "content": "1+1 等于几？只回答数字。"}
    ]
    state = get_state()
    routes = state.p2p.router.candidates(model)
    if len(routes) < 2:
        raise HTTPException(
            status_code=409,
            detail=f"只找到 {len(routes)} 个提供者，两两比对需要至少 2 个",
        )

    settings = get_settings()
    body = audit.normalize_payload(
        {"model": model, "messages": messages, "max_tokens": int(data.get("max_tokens") or 40)},
        settings.audit_seed,
    )
    providers: list[dict] = []
    for route in routes:
        try:
            if route.kind == "relay":
                reply = await upstream.chat_once_via_tunnel(
                    state.p2p.tunnel, route.node_id, body, _uuid.uuid4().hex
                )
            else:
                reply = await upstream.chat(route.base_url, body)
        except Exception as exc:
            providers.append({
                "node": route.node_name, "node_id": route.node_id,
                "kind": route.kind, "error": str(exc)[:200],
            })
            continue
        providers.append({
            "node": route.node_name,
            "node_id": route.node_id,
            "kind": route.kind,
            "content": audit.content(reply)[:300],
            "usage": reply.get("usage") or {},
        })

    ok = [p for p in providers if "content" in p]
    pairs: list[dict] = []
    for i in range(len(ok)):
        for j in range(i + 1, len(ok)):
            sim = audit.similarity(ok[i]["content"], ok[j]["content"])
            verdict = "pass" if sim >= float(settings.audit_threshold) else "suspect"
            pairs.append({
                "a": ok[i]["node"], "b": ok[j]["node"],
                "similarity": sim, "verdict": verdict,
            })
            audit.record(
                req_id=_uuid.uuid4().hex,
                model=model,
                target_node=ok[i]["node_id"],
                verifier=ok[j]["node_id"],
                original={"choices": [{"message": {"content": ok[i]["content"]}}], "usage": ok[i]["usage"]},
                verified={"choices": [{"message": {"content": ok[j]["content"]}}], "usage": ok[j]["usage"]},
                verdict=verdict,
                detail=f"手动复算：{ok[i]['node']} vs {ok[j]['node']}",
                similarity_override=sim,
            )

    return {
        "model": model,
        "providers": providers,
        "pairs": pairs,
        "threshold": float(settings.audit_threshold),
    }

# ---------------- 贡献度与代币发放 ----------------

@router.get("/emission/current")
async def emission_current(epoch: int | None = None) -> dict:
    """预览当前 epoch 的发放计划（不落盘）。"""
    from aether import emission
    from aether.identity import get_identity
    from aether.p2p.relay_server import relay as memory_relay

    provider = {
        "node": get_identity().pubkey,
        "bytes": memory_relay.relay_total_bytes,
        "messages": sum(v.get("messages", 0) for v in memory_relay.bandwidth.values()),
    }
    return emission.compute(epoch, provider)


@router.post("/emission/publish")
async def emission_publish(payload: dict | None = None) -> dict:
    """把当前 epoch 的发放计划落盘，产出可上链的 Merkle root。"""
    from aether import emission
    from aether.identity import get_identity
    from aether.p2p.relay_server import relay as memory_relay

    epoch = (payload or {}).get("epoch")
    provider = {
        "node": get_identity().pubkey,
        "bytes": memory_relay.relay_total_bytes,
        "messages": sum(v.get("messages", 0) for v in memory_relay.bandwidth.values()),
    }
    plan = emission.compute(epoch, provider)
    if not plan["allocations"]:
        return {"created": False, "reason": "当前 epoch 没有任何贡献记录"}
    path = emission.save(plan)
    plan["path"] = str(path)
    return {"created": True, "plan": plan}


@router.get("/emission/plans")
async def emission_plans(limit: int = 24) -> dict:
    from aether import emission

    return {"plans": emission.list_plans(limit)}


@router.post("/emission/verify")
async def emission_verify(payload: dict) -> dict:
    from aether import emission

    path = (payload or {}).get("path") or ""
    if not path:
        raise HTTPException(status_code=400, detail="需要 path")
    try:
        return emission.verify(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"清单不存在: {path}") from exc


@router.get("/bandwidth")
async def bandwidth() -> dict:
    """内置 relay 观察到的转发字节，代币激励的带宽贡献来源。"""
    from aether.p2p.relay_server import relay as memory_relay

    return {"bandwidth": memory_relay.bandwidth, "stats": memory_relay.stats()}

# ---------------- 链上结算载荷 ----------------

@router.get("/onchain/info")
async def onchain_info() -> dict:
    from aether.evm import abi
    from aether.identity import get_identity

    ident = get_identity()
    return {
        "node_id": ident.pubkey,
        "evm_address": ident.evm_address,
        "commit_signature": "commitBatch(bytes32,uint256,uint256)",
        "commit_selector": "0x" + abi.selector("commitBatch(bytes32,uint256,uint256)").hex(),
        "verify_signature": "verifyLeaf(bytes32,bytes32,bytes32[])",
        "note": "节点身份即 EVM 账户（同一条 secp256k1），私钥不出 Identity 对象",
    }


@router.post("/onchain/prepare")
async def onchain_prepare(payload: dict | None = None) -> dict:
    """为某一期结算构造 commitBatch 的 calldata（不签名）。"""
    from aether.evm import abi

    data = payload or {}
    root = data.get("root") or ""
    if not root:
        raise HTTPException(status_code=400, detail="需要 root")
    calldata = abi.encode_call(
        "commitBatch(bytes32,uint256,uint256)",
        abi.bytes32(root),
        abi.uint(int(data.get("epoch") or 0)),
        abi.uint(int(data.get("amount") or 0)),
    )
    return {
        "calldata": "0x" + calldata.hex(),
        "pretty": abi.pretty(calldata),
        "length": len(calldata),
    }


@router.post("/onchain/sign")
async def onchain_sign(payload: dict) -> dict:
    """用节点私钥签一笔提交交易，产出可直接广播的原始交易。"""
    from aether.evm import abi
    from aether.identity import get_identity

    data = payload or {}
    if not data.get("to"):
        raise HTTPException(status_code=400, detail="需要合约地址 to")
    root = data.get("root") or ""
    if not root:
        raise HTTPException(status_code=400, detail="需要 root")
    calldata = abi.encode_call(
        "commitBatch(bytes32,uint256,uint256)",
        abi.bytes32(root),
        abi.uint(int(data.get("epoch") or 0)),
        abi.uint(int(data.get("amount") or 0)),
    )
    try:
        tx_type = int(data.get("tx_type") or 0)
        common = dict(
            nonce=int(data.get("nonce") or 0),
            gas_limit=int(data.get("gas_limit") or 120_000),
            to=data["to"],
            value=0,
            data=calldata,
            chain_id=int(data.get("chain_id") or 1),
        )
        ident = get_identity()
        if tx_type == 2:
            # EIP-1559：显式给基础费与优先费，缺省时用保守值
            max_fee = int(data.get("max_fee") or 30_000_000_000)
            priority = int(data.get("max_priority_fee") or 1_500_000_000)
            signed = ident.sign_evm_transaction_1559(
                nonce=common["nonce"], max_priority_fee=priority, max_fee=max_fee,
                gas_limit=common["gas_limit"], to=common["to"], value=0,
                data=calldata, chain_id=common["chain_id"],
            )
        else:
            signed = ident.sign_evm_transaction(
                nonce=common["nonce"],
                gas_price=int(data.get("gas_price") or 1_000_000_000),
                gas_limit=common["gas_limit"], to=common["to"], value=0,
                data=calldata, chain_id=common["chain_id"],
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"签名失败: {exc}") from exc
    signed["calldata"] = "0x" + calldata.hex()
    return signed


@router.post("/onchain/broadcast")
async def onchain_broadcast(payload: dict) -> dict:
    """把已签名的原始交易发到指定 RPC。不传 rpc_url 就不会执行。"""
    import httpx

    from aether.net import tls_verify

    data = payload or {}
    rpc = data.get("rpc_url") or ""
    raw = data.get("raw") or ""
    if not rpc or not raw:
        raise HTTPException(status_code=400, detail="需要 rpc_url 与 raw")
    try:
        async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as client:
            resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_sendRawTransaction", "params": [raw],
            })
            body = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"RPC 失败: {exc}") from exc
    if isinstance(body, dict) and body.get("error"):
        raise HTTPException(status_code=400, detail=str(body["error"]))
    return {"tx_hash": (body or {}).get("result")}


@router.get("/onchain/artifact")
async def onchain_artifact() -> dict:
    """返回合约源码位置与说明，便于用任意 solc 编译部署。"""
    from pathlib import Path as _P

    sol = _P(__file__).resolve().parent.parent.parent / "contracts" / "AetherSettlement.sol"
    return {
        "path": str(sol),
        "exists": sol.exists(),
        "solidity": ">=0.8.20",
        "hash": "sha256(abi.encodePacked(a,b))，与 Python 侧 merkle 完全一致",
    }

# ---------------- 代币发放与领取 ----------------

def _addr_map() -> dict:
    """收集 {node_id: evm_address}：自己 + 广告里公布过 EVM 地址的 peer。"""
    from aether.identity import get_identity

    ident = get_identity()
    mapping = {ident.pubkey: ident.evm_address}
    try:
        for entry in get_state().p2p.peers.raw():
            ad = entry.get("ad") or {}
            evm = ad.get("evm") or ""
            if evm:
                mapping[entry["node_id"]] = evm
    except Exception:
        pass
    return mapping


@router.get("/payout/current")
async def payout_current(epoch: int | None = None) -> dict:
    """把本期贡献清单转成链上可领取的叶子、证明与 setRoot calldata。"""
    from aether import emission, payout
    from aether.identity import get_identity
    from aether.p2p.relay_server import relay as memory_relay

    provider = {
        "node": get_identity().pubkey,
        "bytes": memory_relay.relay_total_bytes,
        "messages": sum(v.get("messages", 0) for v in memory_relay.bandwidth.values()),
    }
    plan = emission.compute(epoch, provider)
    doc = payout.build(plan, _addr_map())
    doc["plan"] = {
        "emission": plan["emission"],
        "total_points": plan["total_points"],
        "allocations": len(plan["allocations"]),
    }
    return doc


@router.post("/payout/claim")
async def payout_claim(payload: dict) -> dict:
    """为某个节点生成 claim calldata（不签名）。"""
    from aether import payout

    data = payload or {}
    epoch = int(data.get("epoch") or 0)
    amount = int(data.get("amount_wei") or 0)
    proof = data.get("proof") or []
    if amount <= 0:
        raise HTTPException(status_code=400, detail="需要 amount_wei")
    return {
        "calldata": payout.claim_calldata(epoch, amount, list(proof)),
        "proof_len": len(proof),
    }


@router.post("/payout/verify")
async def payout_verify(payload: dict) -> dict:
    from aether import payout

    data = payload or {}
    root = data.get("root") or ""
    leaf = data.get("leaf") or ""
    proof = data.get("proof") or []
    if not root or not leaf:
        raise HTTPException(status_code=400, detail="需要 root 与 leaf")
    return {"ok": payout.verify_claim(root, leaf, list(proof))}


@router.get("/payout/contracts")
async def payout_contracts() -> dict:
    from pathlib import Path as _P

    base = _P(__file__).resolve().parent.parent.parent / "contracts"
    items = []
    for name in ("AetherToken.sol", "AetherDistributor.sol", "AetherSettlement.sol"):
        p = base / name
        items.append({"name": name, "path": str(p), "exists": p.exists()})
    return {"contracts": items, "token_decimals": 18}
