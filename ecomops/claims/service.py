import asyncio
import json
from contextlib import asynccontextmanager

from langgraph.types import Command
from sqlalchemy import text

from ecomops.claims import repo
from ecomops.core.auth import Identity
from ecomops.core.config import get_settings
from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError

DECIDE_ACTIONS = ("APPROVE", "MODIFY_AMOUNT", "REJECT", "REQUEST_EVIDENCE")


def thread_id(claim_no: str, rnd: int) -> str:
    return f"{claim_no}:r{rnd}"


@asynccontextmanager
async def claim_lock(claim_no: str):
    """同一工单的状态变更串行化（会话级 advisory lock，占用一条专用连接）。"""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": f"claim:{claim_no}"})
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": f"claim:{claim_no}"})
            await conn.rollback()


async def run_graph(graph, claim_no: str, rnd: int) -> None:
    await graph.ainvoke({"claim_no": claim_no, "round": rnd},
                        {"configurable": {"thread_id": thread_id(claim_no, rnd)}})


async def status_of(claim_no: str) -> str:
    async with get_engine().connect() as c:
        return (await c.execute(text("SELECT claim_status FROM claim_tickets WHERE claim_no=:c"), {"c": claim_no})).scalar()


async def _resubmit_after_withdrawal(graph, p: dict) -> bool:
    """买家撤回/超时撤回后重新申请：claim_no 由订单子项确定性生成，所以会撞上已撤回的旧工单。
    这里把旧工单重置为新一轮审核（audit_round+1、清掉上一轮的路由/金额/风险），否则 Pilot 侧订单会卡在
    AFTER_SALE_OPEN，而 Ops 工单永远停在 WITHDRAWN。只有 WITHDRAWN 才会重开；其它状态都是幂等重放。"""
    async with claim_lock(p["claim_no"]):
        async with get_engine().begin() as c:
            t = await repo.get_ticket(c, p["claim_no"], for_update=True)
            if not t or t["claim_status"] != "WITHDRAWN":
                return False
            rnd = t["audit_round"] + 1
            await c.execute(text(
                "UPDATE claim_tickets SET item_payable_cents=:pay, max_refundable_cents=:mx, days_since_delivery=:days, "
                "buyer_reason_text=:reason, evidence_urls=CAST(:ev AS jsonb), nlu_signal=CAST(:nlu AS jsonb), "
                "claim_status='SUBMITTED', audit_round=:r, thread_id=:th, risk_level=NULL, route_reason=NULL, "
                "approved_refund_cents=NULL, suggested_refund_cents=NULL, fallback_used=FALSE, updated_at=now() "
                "WHERE claim_no=:c"),
                {"pay": p.get("item_payable_cents", p["max_refundable_cents"]), "mx": p["max_refundable_cents"],
                 "days": p.get("days_since_delivery", 0), "reason": p.get("buyer_reason_text"),
                 "ev": json.dumps(p.get("evidence_urls") or []), "nlu": json.dumps(p.get("nlu_signal")),
                 "r": rnd, "th": thread_id(p["claim_no"], rnd), "c": p["claim_no"]})
        await run_graph(graph, p["claim_no"], rnd)
    return True


async def submit(graph, p: dict) -> tuple[dict, bool]:
    """幂等提交：返回 ({claim_no,status}, replayed)。"""
    if await _resubmit_after_withdrawal(graph, p):
        return {"claim_no": p["claim_no"], "status": await status_of(p["claim_no"])}, False
    async with get_engine().begin() as c:
        r = await c.execute(text(
            "INSERT INTO claim_tickets (claim_no, tenant_id, user_id, order_id, order_item_id, sku_id, catalog_category_id, "
            "item_payable_cents, max_refundable_cents, days_since_delivery, claim_type, buyer_reason_text, evidence_urls, "
            "nlu_signal, claim_status, thread_id) VALUES (:claim_no,:tenant_id,:user_id,:order_id,:order_item_id,:sku_id,"
            ":catalog_category_id,:item_payable_cents,:max_refundable_cents,:days,:claim_type,:reason,CAST(:ev AS jsonb),"
            "CAST(:nlu AS jsonb),'SUBMITTED',:th) ON CONFLICT (claim_no) DO NOTHING"),
            {**{k: p.get(k) for k in ("claim_no", "tenant_id", "user_id", "order_id", "order_item_id", "sku_id",
                                      "catalog_category_id")},
             "item_payable_cents": p.get("item_payable_cents", p["max_refundable_cents"]),
             "max_refundable_cents": p["max_refundable_cents"], "days": p.get("days_since_delivery", 0),
             "claim_type": p.get("claim_type") or "QUALITY_REFUND", "reason": p.get("buyer_reason_text"),
             "ev": json.dumps(p.get("evidence_urls") or []), "nlu": json.dumps(p.get("nlu_signal")),
             "th": thread_id(p["claim_no"], 1)})
        created = r.rowcount == 1
    if created:
        async with claim_lock(p["claim_no"]):
            await run_graph(graph, p["claim_no"], 1)
    return {"claim_no": p["claim_no"], "status": await status_of(p["claim_no"])}, not created


async def confirm(graph, ident: Identity, claim_no: str, action: str, amount: int | None, reason: str | None) -> dict:
    async with get_engine().connect() as c:
        t = await repo.get_ticket(c, claim_no, ident.tenant_id)
    if not t:
        raise ApiError(404, "NOT_FOUND", "claim not found")
    if action not in DECIDE_ACTIONS:
        raise ApiError(400, "INVALID_REQUEST", "unknown action")
    if action == "MODIFY_AMOUNT" and (amount is None or amount <= 0 or amount > int(t["max_refundable_cents"])):
        raise ApiError(422, "AMOUNT_EXCEEDS_LIMIT", "amount must be in (0, max_refundable_cents]",
                       {"max_refundable_cents": int(t["max_refundable_cents"])})
    async with claim_lock(claim_no):
        async with get_engine().connect() as c:
            t = await repo.get_ticket(c, claim_no, ident.tenant_id)
            if t["claim_status"] != "PENDING_SUPERVISOR_REVIEW":
                last = (await c.execute(text(
                    "SELECT action, amount_cents FROM claim_approvals WHERE claim_no=:c AND action = ANY(:a) "
                    "ORDER BY id DESC LIMIT 1"), {"c": claim_no, "a": list(DECIDE_ACTIONS)})).first()
                same = last and last[0] == action and (action != "MODIFY_AMOUNT" or last[1] == amount)
                if same:
                    return {"claim_no": claim_no, "claim_status": t["claim_status"], "idempotent_replayed": True}
                raise ApiError(409, "CLAIM_STATE_CONFLICT", f"claim is {t['claim_status']}")
        amt = amount if action == "MODIFY_AMOUNT" else int(t["suggested_refund_cents"] or 0)
        if action in ("APPROVE", "MODIFY_AMOUNT") and amt <= 0:
            raise ApiError(422, "AMOUNT_EXCEEDS_LIMIT", "nothing to refund")
        await graph.ainvoke(Command(resume={"action": action, "amount_cents": amt if action in ("APPROVE", "MODIFY_AMOUNT") else None,
                                            "reason": reason, "user_id": ident.user_id}),
                            {"configurable": {"thread_id": thread_id(claim_no, t["audit_round"])}})
    return {"claim_no": claim_no, "claim_status": await status_of(claim_no), "idempotent_replayed": False}


async def add_evidence(graph, claim_no: str, urls: list[str]) -> dict:
    async with claim_lock(claim_no):
        async with get_engine().begin() as c:
            t = await repo.get_ticket(c, claim_no, for_update=True)
            if not t:
                raise ApiError(404, "NOT_FOUND", "claim not found")
            if t["claim_status"] != "NEED_MORE_EVIDENCE":
                raise ApiError(409, "CLAIM_STATE_CONFLICT", f"claim is {t['claim_status']}")
            rnd = t["audit_round"] + 1
            await c.execute(text("UPDATE claim_tickets SET evidence_urls = evidence_urls || CAST(:u AS jsonb), "
                                 "audit_round=:r, claim_status='SUBMITTED', updated_at=now() WHERE claim_no=:c"),
                            {"u": json.dumps(urls), "r": rnd, "c": claim_no})
        await run_graph(graph, claim_no, rnd)
    return {"claim_no": claim_no, "status": await status_of(claim_no)}


async def public_status(claim_no: str) -> dict:
    """买家侧可见的最小状态（不含审核细节）；供 EcomPilot 查询。"""
    async with get_engine().connect() as c:
        t = await repo.get_ticket(c, claim_no)
    if not t:
        raise ApiError(404, "NOT_FOUND", "claim not found")
    return {"claim_no": claim_no, "claim_status": t["claim_status"], "audit_round": t["audit_round"],
            "needs_evidence": t["claim_status"] == "NEED_MORE_EVIDENCE", "evidence_count": len(t["evidence_urls"] or [])}


async def withdraw_expired_evidence() -> int:
    """NEED_MORE_EVIDENCE 超过 evidence_deadline_hours 仍未补材料 → WITHDRAWN，并回写 Pilot 恢复订单。
    每张工单在 claim_lock 内复核状态与是否仍超期，避免和买家同时补材料/撤回竞争。"""
    hours = get_settings().evidence_deadline_hours
    async with get_engine().connect() as c:
        due = (await c.execute(text(
            "SELECT claim_no FROM claim_tickets WHERE claim_status='NEED_MORE_EVIDENCE' "
            "AND updated_at < now() - make_interval(secs => :s) ORDER BY updated_at LIMIT 100"),
            {"s": hours * 3600})).scalars().all()
    done = 0
    for claim_no in due:
        async with claim_lock(claim_no):
            async with get_engine().begin() as c:
                t = await repo.get_ticket(c, claim_no, for_update=True)
                if not t or t["claim_status"] != "NEED_MORE_EVIDENCE":
                    continue
                age = (await c.execute(text("SELECT extract(epoch FROM now() - updated_at) FROM claim_tickets WHERE claim_no=:c"),
                                       {"c": claim_no})).scalar()
                if age < hours * 3600:
                    continue
                await repo.set_status(c, claim_no, "WITHDRAWN", route_reason="买家超时未补充材料，已自动撤回")
                await repo.add_result_outbox(c, claim_no, "WITHDRAWN", reason="超时未补充材料，已自动撤回",
                                             rnd=t["audit_round"])
                done += 1
    return done


async def withdraw(claim_no: str) -> dict:
    async with claim_lock(claim_no):
        async with get_engine().begin() as c:
            t = await repo.get_ticket(c, claim_no, for_update=True)
            if not t:
                raise ApiError(404, "NOT_FOUND", "claim not found")
            if t["claim_status"] == "WITHDRAWN":
                return {"claim_no": claim_no, "claim_status": "WITHDRAWN", "idempotent_replayed": True}
            if t["claim_status"] not in ("NEED_MORE_EVIDENCE", "PENDING_SUPERVISOR_REVIEW"):
                raise ApiError(409, "CLAIM_STATE_CONFLICT", f"claim is {t['claim_status']}")
            await repo.set_status(c, claim_no, "WITHDRAWN")
            await repo.add_result_outbox(c, claim_no, "WITHDRAWN", rnd=t["audit_round"])
    return {"claim_no": claim_no, "claim_status": "WITHDRAWN", "idempotent_replayed": False}


async def list_claims(ident: Identity, status: str | None) -> list[dict]:
    # 加急（NLU 的 priority_boost）只是排序辅助信号：同一批里加急的排前面，其余按创建时间倒序；不改变任何审核结论
    sql = "SELECT * FROM claim_tickets WHERE tenant_id=:t" + (" AND claim_status=:s" if status else "") + \
          " ORDER BY COALESCE(nlu_signal->'priority_boost' = 'true'::jsonb, false) DESC, created_at DESC LIMIT 200"
    async with get_engine().connect() as c:
        rows = (await c.execute(text(sql), {"t": ident.tenant_id, "s": status})).mappings().all()
    return [repo.claim_view(dict(r)) for r in rows]


async def get_detail(ident: Identity, claim_no: str) -> dict:
    async with get_engine().connect() as c:
        t = await repo.get_ticket(c, claim_no, ident.tenant_id)
        if not t:
            raise ApiError(404, "NOT_FOUND", "claim not found")
        tr = (await c.execute(text("SELECT audit_round, track, passed, confidence, fallback_used, detail "
                                   "FROM claim_track_results WHERE claim_no=:c ORDER BY audit_round, id"), {"c": claim_no})).mappings().all()
        ap = (await c.execute(text("SELECT action, approver_user_id, amount_cents, reason FROM claim_approvals "
                                   "WHERE claim_no=:c ORDER BY id"), {"c": claim_no})).mappings().all()
        ob = (await c.execute(text("SELECT status, attempts, refund_idempotency_key, last_error FROM outbox_events "
                                   "WHERE claim_no=:c AND event_type='REFUND_CALLBACK'"), {"c": claim_no})).mappings().first()
    tracks = [{"audit_round": r["audit_round"], "track": r["track"], "passed": r["passed"],
               "confidence": float(r["confidence"]), "fallback_used": r["fallback_used"], "detail": r["detail"]} for r in tr]
    return repo.claim_view(t, tracks, [dict(r) for r in ap], dict(ob) if ob else None)
