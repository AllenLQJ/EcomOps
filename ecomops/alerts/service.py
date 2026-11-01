"""告警控制台：出箱事件走到 FAILED_MANUAL（回调/回写多次失败）与长时间卡住的退款回调，需要主管人工处理。

告警由 outbox_events 与 claim_tickets 派生，不另建告警表；人工动作写入 alert_actions 留痕。
重试不直接调 Pilot：把事件放回 PENDING，由 outbox worker 走既有路径（先查 refunds/status，SUCCESS 不再重发），因此不会重复退款。
"""
import json

import httpx
from sqlalchemy import text

from ecomops.core.auth import Identity
from ecomops.core.config import get_settings
from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError

ENSURE_SQL = [
    """CREATE TABLE IF NOT EXISTS alert_actions (
      id BIGSERIAL PRIMARY KEY, event_id VARCHAR(64) NOT NULL, claim_no VARCHAR(64) NOT NULL,
      tenant_id VARCHAR(64) NOT NULL, action VARCHAR(16) NOT NULL, actor_user_id VARCHAR(64) NOT NULL,
      reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_alert_actions_event ON alert_actions (event_id)",
]
TYPES = {"REFUND_CALLBACK": "CALLBACK_FAILED_MANUAL", "CLAIM_RESULT": "RESULT_NOTIFY_FAILED"}


async def ensure_schema() -> None:
    async with get_engine().begin() as c:
        for stmt in ENSURE_SQL:
            await c.execute(text(stmt))


def _view(row: dict, state: str) -> dict:
    return {"alert_id": row["alert_id"], "type": row["type"], "state": state, "claim_no": row["claim_no"],
            "claim_status": row["claim_status"], "attempts": row.get("attempts"), "last_error": row.get("last_error"),
            "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}


async def list_alerts(ident: Identity, state: str | None) -> list[dict]:
    """state: OPEN | RESOLVED | None(全部)。OPEN = 需要人工的出箱事件 + 卡住的回调。"""
    stuck_minutes = get_settings().alert_stuck_minutes
    out: list[dict] = []
    async with get_engine().connect() as c:
        rows = (await c.execute(text(
            "SELECT o.event_id AS alert_id, o.event_type, o.status, o.attempts, o.last_error, o.created_at, o.updated_at, "
            "o.claim_no, t.claim_status FROM outbox_events o JOIN claim_tickets t ON t.claim_no=o.claim_no "
            "WHERE t.tenant_id=:t AND o.status IN ('FAILED_MANUAL','RESOLVED_MANUAL') "
            "ORDER BY o.updated_at DESC LIMIT 200"), {"t": ident.tenant_id})).mappings().all()
        for r in rows:
            row = dict(r)
            row["type"] = TYPES.get(row["event_type"], row["event_type"])
            out.append(_view(row, "OPEN" if row["status"] == "FAILED_MANUAL" else "RESOLVED"))
        stuck = (await c.execute(text(
            "SELECT claim_no, claim_status, created_at, updated_at FROM claim_tickets WHERE tenant_id=:t "
            "AND claim_status IN ('APPROVED_PENDING_CALLBACK','CALLBACK_RETRYING') "
            "AND updated_at < now() - make_interval(mins => :m) ORDER BY updated_at LIMIT 200"),
            {"t": ident.tenant_id, "m": stuck_minutes})).mappings().all()
        for r in stuck:
            row = dict(r)
            row.update(alert_id="stuck:" + row["claim_no"], type="CALLBACK_STUCK", attempts=None,
                       last_error=f"退款回调超过 {stuck_minutes} 分钟未完成")
            out.append(_view(row, "OPEN"))
    if state:
        out = [a for a in out if a["state"] == state]
    return out


async def _own_event(c, alert_id: str, tenant_id: str, for_update: bool = False) -> dict:
    row = (await c.execute(text(
        "SELECT o.*, t.tenant_id FROM outbox_events o JOIN claim_tickets t ON t.claim_no=o.claim_no "
        "WHERE o.event_id=:i AND t.tenant_id=:t" + (" FOR UPDATE OF o" if for_update else "")),
        {"i": alert_id, "t": tenant_id})).mappings().first()
    if not row:
        raise ApiError(404, "NOT_FOUND", "alert not found")
    return dict(row)


async def retry(ident: Identity, alert_id: str, reason: str | None) -> dict:
    async with get_engine().begin() as c:
        ev = await _own_event(c, alert_id, ident.tenant_id, for_update=True)
        if ev["status"] != "FAILED_MANUAL":
            raise ApiError(409, "ALERT_STATE_CONFLICT", f"alert is {ev['status']}")
        await c.execute(text("UPDATE outbox_events SET status='PENDING', attempts=0, next_retry_at=now(), updated_at=now() "
                             "WHERE event_id=:i"), {"i": alert_id})
        if ev["event_type"] == "REFUND_CALLBACK":
            # worker 只在这两个状态下才把工单推进到 COMPLETED
            await c.execute(text("UPDATE claim_tickets SET claim_status='CALLBACK_RETRYING', updated_at=now() "
                                 "WHERE claim_no=:c AND claim_status='CALLBACK_FAILED_MANUAL'"), {"c": ev["claim_no"]})
        await c.execute(text("INSERT INTO alert_actions (event_id, claim_no, tenant_id, action, actor_user_id, reason) "
                             "VALUES (:i,:c,:t,'RETRY',:u,:r)"),
                        {"i": alert_id, "c": ev["claim_no"], "t": ident.tenant_id, "u": ident.user_id, "r": reason})
    return {"alert_id": alert_id, "state": "RETRYING"}


async def resolve(ident: Identity, alert_id: str, reason: str) -> dict:
    async with get_engine().begin() as c:
        ev = await _own_event(c, alert_id, ident.tenant_id, for_update=True)
        if ev["status"] != "FAILED_MANUAL":
            raise ApiError(409, "ALERT_STATE_CONFLICT", f"alert is {ev['status']}")
        await c.execute(text("UPDATE outbox_events SET status='RESOLVED_MANUAL', updated_at=now() WHERE event_id=:i"),
                        {"i": alert_id})
        await c.execute(text("INSERT INTO alert_actions (event_id, claim_no, tenant_id, action, actor_user_id, reason) "
                             "VALUES (:i,:c,:t,'RESOLVE',:u,:r)"),
                        {"i": alert_id, "c": ev["claim_no"], "t": ident.tenant_id, "u": ident.user_id, "r": reason})
    return {"alert_id": alert_id, "state": "RESOLVED"}


async def notify(event: dict, error: str | None, tenant_id: str | None) -> None:
    """新进入 FAILED_MANUAL 的告警推送到 webhook；任何失败都吞掉（告警通道故障不能拖垮出箱处理）。"""
    url = get_settings().alert_webhook_url
    if not url:
        return
    body = {"alert_id": event["event_id"], "type": TYPES.get(event.get("event_type", "REFUND_CALLBACK"), "OUTBOX_FAILED"),
            "claim_no": event["claim_no"], "tenant_id": tenant_id, "last_error": (error or "")[:500]}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(url, content=json.dumps(body, ensure_ascii=False).encode(),
                              headers={"content-type": "application/json"})
    except Exception:    # noqa: BLE001 — 见 docstring
        pass
