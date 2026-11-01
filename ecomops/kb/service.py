"""KB retrieval and durable, idempotent publication."""
import hashlib
import json
import re
import uuid

import httpx
from sqlalchemy import text

from ecomops.core.config import get_settings
from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError
from ecomops.core import llm
from ecomops.kb import faults


def classify_query(query: str) -> str:
    if any(w in query for w in ("你好", "谢谢", "天气", "您好", "早上好")):
        return "GENERAL"
    if any(w in query for w in ("以及", "还有", "顺便")) or len(re.findall(r"[?？]", query)) > 1:
        return "BROAD"
    if any(w in query for w in ("几天", "运费", "保修", "退换", "7天", "退货", "退款")):
        return "PRECISE"
    return "VAGUE"


async def ask(body, identity):
    kind = classify_query(body.query)
    hits, degraded = [], False
    if kind != "GENERAL":
        queries = [body.query]
        if kind == "VAGUE":
            queries = [body.query + " 商品售后政策说明"]
        elif kind == "BROAD":
            queries = [q.strip() for q in re.split(r"以及|还有|顺便|[?？]", body.query) if q.strip()][:3]
        try:
            async with httpx.AsyncClient(timeout=get_settings().rag_search_timeout_s) as client:
                for query in queries:
                    r = await client.post(get_settings().rag_url + "/api/v1/rag/search", json={
                        "query": query, "tenant_id": identity.tenant_id,
                        "catalog_category_id": body.catalog_category_id,
                        "top_k": {"PRECISE": 8, "VAGUE": 10, "BROAD": 4}[kind]})
                    r.raise_for_status()
                    hits.extend(r.json()["hits"])
            hits = sorted({h["chunk_id"]: h for h in hits}.values(), key=lambda h: h["score"], reverse=True)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            hits, degraded = [], True
    score = hits[0]["score"] if hits else 0
    fallback = degraded or llm.should_fail("kb_answer")
    answer = "您好，请告诉我您需要咨询的商品或售后问题。"
    if kind != "GENERAL":
        answer = "暂未找到足够可靠的规则，已记录问题，等待人工补充确认。"
        if score >= .75:
            answer = "\n".join(f'{h["text"]} [{h["rule_source"]}/{"沙盒演示" if h["is_demo_rule"] else "正式规则"}]' for h in hits)
        else:
            fallback = True
    sid = body.session_id or uuid.uuid4().hex
    params = {"tenant": identity.tenant_id, "sid": sid, "user": identity.user_id}
    queue = None
    async with get_engine().begin() as c:
        # Serialize updates to a session, including its initial INSERT.
        await c.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": identity.tenant_id + ":" + sid})
        row = (await c.execute(text("SELECT * FROM qa_sessions WHERE tenant_id=:tenant AND session_id=:sid FOR UPDATE"), params)).mappings().first()
        if row and row["user_id"] != identity.user_id:
            raise ApiError(404, "NOT_FOUND")
        messages = list(row["messages"]) if row else []
        messages += [{"role": "user", "content": body.query}, {"role": "assistant", "content": answer}]
        summary = row["summary"] if row else ""
        if len(messages) % 20 == 0:
            summary = "；".join(m["content"] for m in messages if m["role"] == "user")[:1000]
        await c.execute(text("""INSERT INTO qa_sessions(session_id,tenant_id,user_id,messages,summary)
            VALUES (:sid,:tenant,:user,CAST(:messages AS jsonb),:summary)
            ON CONFLICT (tenant_id,session_id) DO UPDATE SET messages=EXCLUDED.messages,
            summary=EXCLUDED.summary, updated_at=now()"""), params | {"messages": json.dumps(messages), "summary": summary})
        if kind != "GENERAL" and score < .75:
            queue = (await c.execute(text("""INSERT INTO knowledge_pending_queue(queue_id,tenant_id,question_text,catalog_category_id)
                VALUES (:id,:tenant,:question,:category)
                ON CONFLICT (tenant_id,md5(question_text)) WHERE publish_status='PENDING_REVIEW'
                DO UPDATE SET question_text=EXCLUDED.question_text RETURNING queue_id"""), {
                    "id": uuid.uuid4(), "tenant": identity.tenant_id, "question": body.query, "category": body.catalog_category_id})).scalar_one()
    return dict(answer=answer, query_type=kind, top1_score=score, hits=hits,
                enqueued_queue_id=str(queue) if queue else None, session_id=sid, fallback_used=fallback)


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
                "effective_from": item["effective_from"], "effective_to": item["effective_to"], "is_demo_rule": False})
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
