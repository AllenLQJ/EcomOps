"""claim_audit LangGraph：load → 三轨并发 → 聚合路由 → {自动驳回 | 补材料 | 自动批准 | 通知主管→interrupt→应用决定}。"""
import asyncio
import json
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import text

from ecomops.claims import repo, tracks
from ecomops.core import pilot, rag
from ecomops.core.config import get_settings
from ecomops.core.db import get_engine


class State(TypedDict, total=False):
    claim_no: str
    round: int
    policy: list
    tracks: dict
    route: str
    reason: str
    decision: dict


async def load_order_and_policy(state: State) -> dict:
    """加载订单快照与可退上限（已随提交入库），并尽力检索售后条款（失败只记录，不降级放行）。
    有 Pilot 商品快照时把品类名拼进检索词；条款（规则来源/版本/分数/文本）交给三轨引用。"""
    async with get_engine().connect() as c:
        t = await repo.get_ticket(c, state["claim_no"])
    snapshot = await pilot.sku_snapshot(t["tenant_id"], t["sku_id"])
    query = ((snapshot["category_name"] + " ") if snapshot else "") + (t["buyer_reason_text"] or "售后退换")
    hits = await rag.search(query[:500], tenant_id=t["tenant_id"], catalog_category_id=t["catalog_category_id"],
                            top_k=3, timeout=get_settings().rag_claim_timeout_s) or []
    policy = [{"rule_source": h.get("rule_source"), "text": str(h.get("text", ""))[:400], "score": float(h["score"]),
               "version": h.get("version", 0), "is_demo_rule": bool(h.get("is_demo_rule", False))} for h in hits[:3]]
    return {"policy": policy}


async def run_three_tracks(state: State) -> dict:
    claim_no, rnd = state["claim_no"], state["round"]
    async with get_engine().connect() as c:
        t = await repo.get_ticket(c, claim_no)
    res = await tracks.run_three(t, state.get("policy") or [])
    fb = any(x["fallback_used"] for x in res.values())
    async with get_engine().begin() as c:
        for name, x in res.items():
            await c.execute(text(
                "INSERT INTO claim_track_results (claim_no, audit_round, track, passed, confidence, fallback_used, detail) "
                "VALUES (:c,:r,:t,:p,:cf,:fb,CAST(:d AS jsonb)) ON CONFLICT (claim_no, audit_round, track) DO NOTHING"),
                {"c": claim_no, "r": rnd, "t": name, "p": x["passed"], "cf": x["confidence"], "fb": x["fallback_used"],
                 "d": json.dumps(x["detail"])})
        await c.execute(text("UPDATE claim_tickets SET suggested_refund_cents=:s, restock_policy=:rp, fallback_used=:fb, "
                             "audit_round=:r, updated_at=now() WHERE claim_no=:c"),
                        {"s": res["FEE"]["detail"]["suggested_refund_cents"], "rp": res["FEE"]["detail"]["restock_policy"],
                         "fb": fb, "r": rnd, "c": claim_no})
    return {"tracks": res}


async def aggregate_and_route(state: State) -> dict:
    async with get_engine().connect() as c:
        t = await repo.get_ticket(c, state["claim_no"])
    route, reason = tracks.route(t, state["tracks"], get_settings().auto_approve_max_cents)
    return {"route": route, "reason": reason}


def route_edge(state: State) -> str:
    return state["route"]


async def auto_reject(state: State) -> dict:
    cn = state["claim_no"]
    async with get_engine().begin() as c:
        if await repo.once(c, f"{cn}:r{state['round']}:auto_reject", cn, "auto_reject"):
            await repo.set_status(c, cn, "AUTO_REJECTED", risk_level="LOW", route_reason=state["reason"])
            await repo.add_result_outbox(c, cn, "AUTO_REJECTED", reason=state.get("reason"), rnd=state["round"])
    return {}


async def request_more_evidence(state: State) -> dict:
    cn = state["claim_no"]
    async with get_engine().begin() as c:
        if await repo.once(c, f"{cn}:r{state['round']}:request_more_evidence", cn, "request_more_evidence"):
            await repo.set_status(c, cn, "NEED_MORE_EVIDENCE", risk_level="LOW", route_reason=state["reason"])
    return {}


async def auto_approve(state: State) -> dict:
    cn = state["claim_no"]
    async with get_engine().begin() as c:
        if await repo.once(c, f"{cn}:r{state['round']}:auto_approve", cn, "auto_approve"):
            t = await repo.get_ticket(c, cn, for_update=True)
            amt = int(t["suggested_refund_cents"])
            await repo.add_approval(c, cn, "AUTO_APPROVE", "SYSTEM", amt, state["reason"])
            await repo.add_refund_outbox(c, t, amt, "AUTO_APPROVED", "SYSTEM")
            await repo.set_status(c, cn, "APPROVED_PENDING_CALLBACK", risk_level="LOW",
                                  route_reason=state["reason"], approved_refund_cents=amt)
    return {}


async def execute_refund_callback(state: State) -> dict:
    """路线图图 3-4 的 execute_refund_callback 节点。

    节点本身不同步调用 Pilot：Pilot 提交接口有 3 秒超时，同步回调超时会触发它的补偿回滚（决策记录第 5 条）。
    所以这里只做两件事：以 step_execution_id 防重入地"登记并唤醒"——退款回调事件已在批准节点里和审批结论同一个 PG 事务写进
    outbox_events；真正的 HTTP 调用、超时后先查 refunds/status 再决定是否补发、重试到上限转人工，都由 outbox worker 完成。"""
    cn = state["claim_no"]
    async with get_engine().begin() as c:
        first = await repo.once(c, f"{cn}:r{state['round']}:execute_refund_callback", cn, "execute_refund_callback")
    if first:
        from ecomops.outbox import worker     # 延迟导入：worker 反过来被 claims.service 的扫描用到
        asyncio.get_running_loop().create_task(worker.process_due())
    return {}


def after_decision(state: State) -> str:
    """主管决定之后：批准类（工单已进入 APPROVED_PENDING_CALLBACK）才走回调节点，驳回/退回补材料直接结束。"""
    return "execute_refund_callback" if state["decision"]["action"] in ("APPROVE", "MODIFY_AMOUNT") else END


async def notify_supervisor(state: State) -> dict:
    cn = state["claim_no"]
    async with get_engine().begin() as c:
        await repo.set_status(c, cn, "PENDING_SUPERVISOR_REVIEW", risk_level="HIGH", route_reason=state["reason"])
    return {}


async def supervisor_review(state: State) -> dict:
    """挂起：状态快照由 AsyncPostgresSaver 持久化。恢复值由 service.confirm 以 Command(resume=...) 注入。"""
    decision = interrupt({"claim_no": state["claim_no"], "reason": state["reason"]})
    return {"decision": decision}


async def apply_supervisor_decision(state: State) -> dict:
    cn, d = state["claim_no"], state["decision"]
    action = d["action"]
    async with get_engine().begin() as c:
        if not await repo.once(c, f"{cn}:r{state['round']}:apply:{action}", cn, "apply_supervisor_decision"):
            return {}
        t = await repo.get_ticket(c, cn, for_update=True)
        if t["claim_status"] != "PENDING_SUPERVISOR_REVIEW":
            return {}
        await repo.add_approval(c, cn, action, d["user_id"], d.get("amount_cents"), d.get("reason"))
        if action in ("APPROVE", "MODIFY_AMOUNT"):
            amt = int(d["amount_cents"])
            await repo.add_refund_outbox(c, t, amt, "SUPERVISOR_CONFIRMED", d["user_id"])
            await repo.set_status(c, cn, "APPROVED_PENDING_CALLBACK", approved_refund_cents=amt)
        elif action == "REJECT":
            await repo.set_status(c, cn, "SUPERVISOR_REJECTED")
            await repo.add_result_outbox(c, cn, "SUPERVISOR_REJECTED", reason=d.get("reason") or "主管驳回", rnd=state["round"])
        else:  # REQUEST_EVIDENCE
            await repo.set_status(c, cn, "NEED_MORE_EVIDENCE", route_reason=d.get("reason") or "主管退回补材料")
    return {}


def build_graph(checkpointer):
    g = StateGraph(State)
    for name, fn in [("load_order_and_policy", load_order_and_policy), ("run_three_tracks", run_three_tracks),
                     ("aggregate_and_route", aggregate_and_route), ("auto_reject", auto_reject),
                     ("request_more_evidence", request_more_evidence), ("auto_approve", auto_approve),
                     ("notify_supervisor", notify_supervisor), ("supervisor_review", supervisor_review),
                     ("apply_supervisor_decision", apply_supervisor_decision),
                     ("execute_refund_callback", execute_refund_callback)]:
        g.add_node(name, fn)
    g.add_edge(START, "load_order_and_policy")
    g.add_edge("load_order_and_policy", "run_three_tracks")
    g.add_edge("run_three_tracks", "aggregate_and_route")
    g.add_conditional_edges("aggregate_and_route", route_edge, {
        "auto_reject": "auto_reject", "need_evidence": "request_more_evidence",
        "auto_approve": "auto_approve", "supervisor": "notify_supervisor"})
    g.add_edge("notify_supervisor", "supervisor_review")
    g.add_edge("supervisor_review", "apply_supervisor_decision")
    g.add_edge("auto_approve", "execute_refund_callback")
    g.add_conditional_edges("apply_supervisor_decision", after_decision,
                            {"execute_refund_callback": "execute_refund_callback", END: END})
    g.add_edge("execute_refund_callback", END)
    for end in ("auto_reject", "request_more_evidence"):
        g.add_edge(end, END)
    return g.compile(checkpointer=checkpointer)
