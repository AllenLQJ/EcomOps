"""KB retrieval and durable, idempotent publication."""
import hashlib
import re
import uuid
from contextlib import aclosing

import httpx
from sqlalchemy import text

from ecomops.core.config import get_settings, rag_headers
from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError
from ecomops.kb import faults


def classify_query(query: str) -> str:
    if any(w in query for w in ("你好", "谢谢", "天气", "您好", "早上好")):
        return "GENERAL"
    if any(w in query for w in ("以及", "还有", "顺便")) or len(re.findall(r"[?？]", query)) > 1:
        return "BROAD"
    if any(w in query for w in ("几天", "运费", "保修", "退换", "7天", "退货", "退款")):
        return "PRECISE"
    return "VAGUE"


_default_graph = None


def default_graph():
    global _default_graph
    if _default_graph is None:
        from ecomops.kb.graph import build_graph
        _default_graph = build_graph()
    return _default_graph


async def ask(body, identity, graph=None):
    sid = body.session_id or uuid.uuid4().hex
    state = await (graph if graph is not None else default_graph()).ainvoke(
        {"query": body.query, "catalog_category_id": body.catalog_category_id,
         "tenant_id": identity.tenant_id, "user_id": identity.user_id, "session_id": sid},
        config={"configurable": {"thread_id": f"kb:{identity.tenant_id}:{sid}:{uuid.uuid4().hex}"}})
    return {key: state[key] for key in (
        "answer", "query_type", "top1_score", "hits", "enqueued_queue_id", "session_id", "fallback_used")}


async def pending(tenant, status=None):
    async with get_engine().connect() as c:
        rows = await c.execute(text("SELECT * FROM knowledge_pending_queue WHERE tenant_id=:tenant" +
                                  (" AND publish_status=:status" if status else "") + " ORDER BY created_at,queue_id"), {"tenant": tenant, "status": status})
        return [dict(r) for r in rows.mappings()]


async def get_item(c, tenant, queue_id, locked=False):
    row = (await c.execute(text("SELECT * FROM knowledge_pending_queue WHERE tenant_id=:tenant AND queue_id=:id" +
                               (" FOR UPDATE" if locked else "")), {"tenant": tenant, "id": queue_id})).mappings().first()
    if not row:
        raise ApiError(404, "NOT_FOUND")
    return dict(row)


async def transition(tenant, queue_id, action, draft=None):
    params = {"tenant": tenant, "id": queue_id}
    async with get_engine().begin() as c:
        item = await get_item(c, tenant, queue_id, True)
        allowed = {"publish": ("PENDING_REVIEW", "PUBLISH_FAILED"), "retry": ("PUBLISHING", "PUBLISH_FAILED"), "discard": ("PENDING_REVIEW", "PUBLISH_FAILED")}
        if item["publish_status"] not in allowed[action]:
            raise ApiError(409, "PUBLISH_STATE_CONFLICT")
        if action == "discard":
            await c.execute(text("UPDATE knowledge_pending_queue SET publish_status='DISCARDED',updated_at=now() WHERE tenant_id=:tenant AND queue_id=:id"), params)
            return await get_item(c, tenant, queue_id)
        if action == "publish":
            version = item["version"] or 1
            chunk = hashlib.sha256(f"{queue_id}:{version}".encode()).hexdigest()
            await c.execute(text("""UPDATE knowledge_pending_queue SET standard_answer_draft=:standard_answer,
                catalog_category_id=:catalog_category_id,rule_source=:rule_source,effective_from=:effective_from,
                effective_to=:effective_to,version=:version,deterministic_chunk_id=:chunk
                WHERE tenant_id=:tenant AND queue_id=:id"""), params | draft.model_dump() | {"version": version, "chunk": chunk})
        await c.execute(text("UPDATE knowledge_pending_queue SET publish_status='PUBLISHING',last_error=NULL,updated_at=now() WHERE tenant_id=:tenant AND queue_id=:id"), params)
        item = await get_item(c, tenant, queue_id)
    error = None
    try:
        async with httpx.AsyncClient(timeout=get_settings().rag_upsert_timeout_s) as client:
            response = await client.post(get_settings().rag_url + "/internal/v1/kb/upsert", json={
                "chunk_id": item["deterministic_chunk_id"], "tenant_id": tenant,
                "catalog_category_id": item["catalog_category_id"],
                "text": item["question_text"] + "\n" + item["standard_answer_draft"],
                "rule_source": item["rule_source"], "version": item["version"],
                "effective_from": item["effective_from"], "effective_to": item["effective_to"], "is_demo_rule": False},
                headers=rag_headers(get_settings()))
            response.raise_for_status()
    except httpx.HTTPError as exc:
        error = str(exc)[:2000]
    if error is None and faults.state["publish_writeback_fail"]:
        return item
    async with get_engine().begin() as c:
        # A concurrent successful retry must never be overwritten by a failed attempt.
        await c.execute(text("""UPDATE knowledge_pending_queue SET publish_status=:status,last_error=:error,updated_at=now()
            WHERE tenant_id=:tenant AND queue_id=:id AND publish_status IN ('PUBLISHING','PUBLISH_FAILED')"""),
            params | {"status": "PUBLISH_FAILED" if error else "PUBLISHED", "error": error})
        return await get_item(c, tenant, queue_id)


async def check_session_access(body, identity):
    if body.session_id is None:
        return
    async with get_engine().connect() as conn:
        row = (await conn.execute(text("SELECT user_id FROM qa_sessions WHERE tenant_id=:tenant AND session_id=:sid"),
            {"tenant": identity.tenant_id, "sid": body.session_id})).mappings().first()
    # A new caller-supplied ID is allowed, as in save_memory.
    if row and row["user_id"] != identity.user_id:
        raise ApiError(404, "NOT_FOUND")


async def stream_ask(body, identity, graph=None):
    sid = body.session_id or uuid.uuid4().hex
    compiled = graph if graph is not None else default_graph()
    async with aclosing(compiled.astream(
        {"query": body.query, "catalog_category_id": body.catalog_category_id,
         "tenant_id": identity.tenant_id, "user_id": identity.user_id, "session_id": sid, "streaming": True},
        config={"configurable": {"thread_id": f"kb:{identity.tenant_id}:{sid}:{uuid.uuid4().hex}"}},
        stream_mode=["custom", "values"])) as events:
        async for mode, value in events:
            if mode == "custom":
                yield value
            else:
                final = value
    yield "done", {key: final[key] for key in ("answer", "fallback_used", "enqueued_queue_id")}
