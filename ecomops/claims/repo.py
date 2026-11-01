"""工单 SQL 辅助。所有写操作都在调用方给定的连接/事务内完成。"""
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from ecomops.core.config import get_settings


def new_event_id() -> str:
    return "EV_" + uuid.uuid4().hex[:16]


def refund_key(claim_no: str) -> str:
    return f"REFUND_{claim_no}_V1"


def iso(v) -> str | None:
    return v.isoformat() if v else None


async def get_ticket(conn, claim_no: str, tenant_id: str | None = None, for_update: bool = False) -> dict | None:
    sql = "SELECT * FROM claim_tickets WHERE claim_no=:c" + (" AND tenant_id=:t" if tenant_id else "")
    if for_update:
        sql += " FOR UPDATE"
    r = (await conn.execute(text(sql), {"c": claim_no, "t": tenant_id})).mappings().first()
    return dict(r) if r else None


async def once(conn, step_id: str, claim_no: str, node: str) -> bool:
    """节点副作用重入幂等：同一 step_execution_id 只有第一次返回 True。须与副作用同事务。"""
    r = await conn.execute(text("INSERT INTO step_executions (step_execution_id, claim_no, node) VALUES (:s,:c,:n) "
                                "ON CONFLICT DO NOTHING"), {"s": step_id, "c": claim_no, "n": node})
    return r.rowcount == 1


async def set_status(conn, claim_no: str, status: str, **fields) -> None:
    sets = ["claim_status=:st", "updated_at=now()"] + [f"{k}=:{k}" for k in fields]
    await conn.execute(text(f"UPDATE claim_tickets SET {', '.join(sets)} WHERE claim_no=:c"),
                       {"st": status, "c": claim_no, **fields})


async def add_approval(conn, claim_no: str, action: str, user: str, amount: int | None, reason: str | None) -> None:
    await conn.execute(text("INSERT INTO claim_approvals (claim_no, action, approver_user_id, amount_cents, reason) "
                            "VALUES (:c,:a,:u,:m,:r)"), {"c": claim_no, "a": action, "u": user, "m": amount, "r": reason})


async def add_refund_outbox(conn, t: dict, amount: int, mode: str, supervisor: str) -> None:
    key = refund_key(t["claim_no"])
    payload = {"refund_idempotency_key": key, "claim_no": t["claim_no"], "order_id": t["order_id"],
               "order_item_id": t["order_item_id"], "approved_refund_cents": int(amount),
               "restock_policy": t.get("restock_policy") or "DEFECTIVE_WAREHOUSE", "approval_mode": mode,
               "supervisor_user_id": supervisor, "approved_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
    await conn.execute(text("INSERT INTO outbox_events (event_id, claim_no, event_type, refund_idempotency_key, payload) "
                            "VALUES (:e,:c,'REFUND_CALLBACK',:k,CAST(:p AS jsonb)) ON CONFLICT (refund_idempotency_key) DO NOTHING"),
                       {"e": new_event_id(), "c": t["claim_no"], "k": key, "p": json.dumps(payload)})


async def add_result_outbox(conn, claim_no: str, status: str) -> None:
    result = "WITHDRAWN" if status == "WITHDRAWN" else "REJECTED"
    key = f"RESULT_{claim_no}_{status}"
    await conn.execute(text("INSERT INTO outbox_events (event_id, claim_no, event_type, refund_idempotency_key, payload) "
                            "VALUES (:e,:c,'CLAIM_RESULT',:k,CAST(:p AS jsonb)) ON CONFLICT (refund_idempotency_key) DO NOTHING"),
                       {"e": new_event_id(), "c": claim_no, "k": key, "p": json.dumps({"result": result})})


def claim_view(t: dict, tracks=None, approvals=None, outbox=None) -> dict:
    v = {"claim_no": t["claim_no"], "claim_status": t["claim_status"], "order_id": t["order_id"],
         "order_item_id": t["order_item_id"], "user_id": t["user_id"],
         "max_refundable_cents": int(t["max_refundable_cents"]),
         "suggested_refund_cents": None if t["suggested_refund_cents"] is None else int(t["suggested_refund_cents"]),
         "approved_refund_cents": None if t["approved_refund_cents"] is None else int(t["approved_refund_cents"]),
         "risk_level": t["risk_level"], "route_reason": t["route_reason"], "fallback_used": t["fallback_used"],
         "audit_round": t["audit_round"], "created_at": iso(t["created_at"])}
    if tracks is not None:
        v["tracks"] = tracks
    if approvals is not None:
        v["approvals"] = approvals
    if outbox is not None or tracks is not None:
        v["outbox"] = outbox
    return v
