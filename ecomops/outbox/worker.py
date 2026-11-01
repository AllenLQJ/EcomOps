"""Outbox：回调 EcomPilot 执行退款 + 超时对账。先查 status，SUCCESS 不再重发；NOT_FOUND/首次才 POST execute。"""
import asyncio
import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import text

from ecomops.alerts import service as alerts
from ecomops.core.config import get_settings
from ecomops.core.db import get_engine

_LEASE_EXTRA = 10.0


async def _claim_one() -> dict | None:
    s = get_settings()
    lease = datetime.now().astimezone() + timedelta(seconds=s.refund_callback_timeout_seconds * 2 + _LEASE_EXTRA)
    async with get_engine().begin() as c:
        r = (await c.execute(text(
            "UPDATE outbox_events SET next_retry_at=:lease, updated_at=now() WHERE event_id = ("
            "SELECT event_id FROM outbox_events WHERE status IN ('PENDING','PENDING_RETRY') AND next_retry_at <= now() "
            "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *"), {"lease": lease})).mappings().first()
    return dict(r) if r else None


async def _finish(ev: dict, status: str, error: str | None = None, ticket_status: str | None = None,
                  bump_attempt: bool = False) -> None:
    s = get_settings()
    nxt = datetime.now().astimezone() + timedelta(seconds=s.outbox_backoff_seconds)
    async with get_engine().begin() as c:
        await c.execute(text("UPDATE outbox_events SET status=:s, last_error=:e, attempts=attempts+:b, next_retry_at=:n, "
                             "updated_at=now() WHERE event_id=:i"),
                        {"s": status, "e": error, "b": 1 if bump_attempt else 0, "n": nxt, "i": ev["event_id"]})
        if ticket_status:
            await c.execute(text("UPDATE claim_tickets SET claim_status=:t, updated_at=now() WHERE claim_no=:c "
                                 "AND claim_status IN ('APPROVED_PENDING_CALLBACK','CALLBACK_RETRYING')"),
                            {"t": ticket_status, "c": ev["claim_no"]})
        tenant = (await c.execute(text("SELECT tenant_id FROM claim_tickets WHERE claim_no=:c"), {"c": ev["claim_no"]})).scalar() \
            if status == "FAILED_MANUAL" else None
    if status == "FAILED_MANUAL":
        await alerts.notify(ev, error, tenant)


async def _handle_refund(ev: dict) -> None:
    s = get_settings()
    base, hdr = s.pilot_url.rstrip("/"), {"X-Internal-Token": s.pilot_internal_token}
    key, payload = ev["refund_idempotency_key"], ev["payload"]
    attempts = ev["attempts"] + 1
    try:
        async with httpx.AsyncClient(timeout=s.refund_callback_timeout_seconds) as client:
            st = await client.get(f"{base}/internal/v1/refunds/status", params={"refund_idempotency_key": key}, headers=hdr)
            if st.status_code == 200 and st.json().get("execution_status") == "SUCCESS":
                return await _finish(ev, "DONE", ticket_status="COMPLETED")
            if st.status_code >= 500:
                raise httpx.HTTPError(f"status http {st.status_code}")
            r = await client.post(f"{base}/internal/v1/refunds/execute", json=payload, headers=hdr)
    except httpx.HTTPError as e:
        err = f"{type(e).__name__}: {e}"[:300]
        if attempts >= s.outbox_max_attempts:
            return await _finish(ev, "FAILED_MANUAL", err, "CALLBACK_FAILED_MANUAL", True)
        return await _finish(ev, "PENDING_RETRY", err, "CALLBACK_RETRYING", True)
    if r.status_code == 200 and r.json().get("execution_status") == "SUCCESS":
        return await _finish(ev, "DONE", ticket_status="COMPLETED")
    if r.status_code >= 500:
        err = f"pilot http {r.status_code}"
        if attempts >= s.outbox_max_attempts:
            return await _finish(ev, "FAILED_MANUAL", err, "CALLBACK_FAILED_MANUAL", True)
        return await _finish(ev, "PENDING_RETRY", err, "CALLBACK_RETRYING", True)
    # 4xx 业务拒绝：不再自动重试，转人工
    await _finish(ev, "FAILED_MANUAL", f"pilot http {r.status_code}: {r.text[:200]}", "CALLBACK_FAILED_MANUAL", True)


async def _handle_result(ev: dict) -> None:
    s = get_settings()
    attempts = ev["attempts"] + 1
    try:
        async with httpx.AsyncClient(timeout=s.refund_callback_timeout_seconds) as client:
            r = await client.post(f"{s.pilot_url.rstrip('/')}/internal/v1/aftersales/{ev['claim_no']}/result",
                                  json=ev["payload"], headers={"X-Internal-Token": s.pilot_internal_token})
    except httpx.HTTPError as e:
        status = "FAILED_MANUAL" if attempts >= s.outbox_max_attempts else "PENDING_RETRY"
        return await _finish(ev, status, f"{type(e).__name__}: {e}"[:300], bump_attempt=True)
    if r.status_code < 300 or r.status_code in (404, 409):   # 404/409：对端无此工单或已处于终态，视为已送达
        return await _finish(ev, "DONE", None if r.status_code < 300 else f"pilot http {r.status_code}")
    status = "FAILED_MANUAL" if attempts >= s.outbox_max_attempts or r.status_code < 500 else "PENDING_RETRY"
    await _finish(ev, status, f"pilot http {r.status_code}", bump_attempt=True)


async def process_due(limit: int = 50) -> int:
    n = 0
    for _ in range(limit):
        ev = await _claim_one()
        if not ev:
            break
        if isinstance(ev["payload"], str):
            ev["payload"] = json.loads(ev["payload"])
        await (_handle_refund(ev) if ev["event_type"] == "REFUND_CALLBACK" else _handle_result(ev))
        n += 1
    return n


async def run_loop() -> None:
    while True:
        await asyncio.sleep(get_settings().reconcile_interval_seconds)
        try:
            await process_due()
        except Exception:
            pass
