"""KB question workflow; session persistence and pending queue share one transaction."""
import asyncio
import json
import re
import uuid
from contextlib import aclosing
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from ecomops.core import llm, rag, websearch
from ecomops.core.config import get_settings
from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError
from ecomops.kb import service


class State(TypedDict, total=False):
    streaming: bool
    query: str
    catalog_category_id: str | None
    tenant_id: str
    user_id: str
    session_id: str
    query_type: str
    queries: list[str]
    hits: list[dict]
    top1_score: float
    degraded: bool
    fallback_used: bool
    answer: str
    web_results: list[dict]
    pending: bool
    enqueued_queue_id: str | None


def refined_query_type(query: str) -> str:
    """规则先判；规则只给出兜底的 VAGUE 时，才让训练出来的分类器细分（规则明确判出的类型永远不被覆盖）。"""
    kind = service.classify_query(query)
    if get_settings().query_classifier.lower() == "on":
        from ecomops.kb import classifier
        kind, _source = classifier.refine(kind, query)
    return kind


async def classify_query(state):
    return {"query_type": refined_query_type(state["query"]), "queries": [state["query"]],
            "hits": [], "top1_score": 0, "degraded": False,
            "fallback_used": llm.should_fail("kb_answer"), "enqueued_queue_id": None}


def model_enabled(state):
    return not llm.should_fail("kb_answer") and llm.configured()


async def hyde_generate(state):
    if model_enabled(state):
        try:
            result = await llm.complete("生成一段假设性售后条款，仅用于检索，不作为事实。", llm.untrusted("query", state["query"]))
            if not isinstance(result, str) or not result.strip():
                raise llm.LlmError("empty hypothesis")
            return {"queries": [result]}
        except llm.LlmError:
            return {"queries": [state["query"] + " 商品售后政策说明"], "fallback_used": True}
    return {"queries": [state["query"] + " 商品售后政策说明"]}


async def multi_query_rewrite(state):
    fallback = [q.strip() for q in re.split(r"以及|还有|顺便|[?？]", state["query"]) if q.strip()][:3]
    if model_enabled(state):
        try:
            result = await llm.complete('拆分为至多三个检索问题，输出 JSON {"queries": [...]}。',
                                        llm.untrusted("query", state["query"]), json_object=True)
            queries = result.get("queries") if isinstance(result, dict) else None
            if not isinstance(queries, list) or not 1 <= len(queries) <= 3 or not all(isinstance(q, str) and q.strip() for q in queries):
                raise llm.LlmError("invalid queries")
            return {"queries": [q.strip() for q in queries]}
        except llm.LlmError:
            return {"queries": fallback, "fallback_used": True}
    return {"queries": fallback}


async def call_rag(state):
    results = await asyncio.gather(*(rag.search(q, tenant_id=state["tenant_id"],
        catalog_category_id=state.get("catalog_category_id"),
        top_k={"PRECISE": 8, "VAGUE": 10, "BROAD": 4}[state["query_type"]]) for q in state["queries"][:3]))
    if any(result is None for result in results):
        return {"hits": [], "degraded": True, "fallback_used": True}
    unique = {}
    for result in results:
        for hit in result:
            if hit["chunk_id"] not in unique or hit["score"] > unique[hit["chunk_id"]]["score"]:
                unique[hit["chunk_id"]] = hit
    return {"hits": sorted(unique.values(), key=lambda h: h["score"], reverse=True)}


async def score_check(state):
    score = state["hits"][0]["score"] if state["hits"] else 0
    if state.get("streaming"):
        emit_meta(state | {"top1_score": score})
    return {"top1_score": score}


def emit_meta(state):
    get_stream_writer()(("meta", {key: state[key] for key in
        ("query_type", "top1_score", "hits", "session_id")}))


def emit_chunks(answer):
    for offset in range(0, len(answer), 40):
        get_stream_writer()(("token", {"text": answer[offset:offset + 40]}))


async def stream_answer(system, user, fallback, suffix="", prefix=""):
    writer = get_stream_writer()
    answer = ""
    try:
        async with aclosing(llm.stream(system, user)) as deltas:
            async for delta in deltas:
                if not delta:
                    continue
                if not answer and prefix:
                    writer(("token", {"text": prefix}))
                answer += delta
                writer(("token", {"text": delta}))
        if not answer.strip():
            raise llm.LlmError("empty answer")
    except llm.LlmError:
        if answer:
            writer(("token", {"reset": True}))
        emit_chunks(fallback)
        return {"answer": fallback, "fallback_used": True}
    if suffix:
        writer(("token", {"text": suffix}))
    return {"answer": prefix + answer + suffix}


async def generate_general(state):
    answer = "您好，请告诉我您需要咨询的商品或售后问题。"
    if state.get("streaming"):
        emit_meta(state)
        emit_chunks(answer)
    return {"answer": answer}


def citation(hit):
    return f'[{hit["rule_source"]}/{"沙盒演示" if hit["is_demo_rule"] else "正式规则"}]'


async def generate_rag(state):
    extract = "\n".join(f'{h["text"]} {citation(h)}' for h in state["hits"])
    if state.get("streaming"):
        if model_enabled(state):
            return await stream_answer("仅依据命中片段回答，不添加外部事实。",
                llm.untrusted("query", state["query"]) + "\n" + llm.untrusted("evidence", extract, limit=12000),
                extract, "\n" + " ".join(dict.fromkeys(citation(h) for h in state["hits"])))
        emit_chunks(extract)
        return {"answer": extract}
    if model_enabled(state):
        try:
            answer = await llm.complete("仅依据命中片段回答，不添加外部事实。", llm.untrusted("query", state["query"]) + "\n" + llm.untrusted("evidence", extract, limit=12000))
            if not isinstance(answer, str) or not answer.strip():
                raise llm.LlmError("empty answer")
            return {"answer": answer.rstrip() + "\n" + " ".join(dict.fromkeys(citation(h) for h in state["hits"]))}
        except llm.LlmError:
            return {"answer": extract, "fallback_used": True}
    return {"answer": extract}


async def web_search(state):
    if not websearch.enabled():
        return {"web_results": []}
    try:
        results = await websearch.search(state["query"])
        return {"web_results": results, **({"fallback_used": True} if not results else {})}
    except Exception:
        # Do not expose provider exceptions (which may include URLs or credentials).
        return {"web_results": [], "fallback_used": True}


async def generate_low(state):
    answer = "暂未找到足够可靠的规则，已记录问题，等待人工补充确认。"
    if state.get("streaming"):
        if state.get("web_results") and model_enabled(state):
            result = await stream_answer("依据搜索摘要生成一句参考说明，不把搜索内容当作正式规则。",
                llm.untrusted("query", state["query"]) + "\n" + llm.untrusted("web", json.dumps(state["web_results"], ensure_ascii=False)),
                answer, prefix=answer + "\n来自联网搜索，未经核实：")
            return result | {"fallback_used": True}
        emit_chunks(answer)
        return {"answer": answer, "fallback_used": True}
    if state.get("web_results") and model_enabled(state):
        try:
            extra = await llm.complete("依据搜索摘要生成一句参考说明，不把搜索内容当作正式规则。", llm.untrusted("query", state["query"]) + "\n" + llm.untrusted("web", json.dumps(state["web_results"], ensure_ascii=False)))
            if isinstance(extra, str) and extra.strip():
                answer += "\n来自联网搜索，未经核实：" + extra
        except llm.LlmError:
            pass
    return {"answer": answer, "fallback_used": True}


async def enqueue_pending(state):
    # Commit the pending INSERT with session ownership validation and memory UPSERT.
    # Keeping the original transaction avoids orphan queues on rejected sessions.
    return {"pending": True}


async def save_memory(state):
    sid, answer, kind, score = state["session_id"], state["answer"], state["query_type"], state["top1_score"]
    params = {"tenant": state["tenant_id"], "sid": sid, "user": state["user_id"]}
    queue = None
    async with get_engine().begin() as c:
        # Serialize updates to a session, including its initial INSERT.
        await c.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": state["tenant_id"] + ":" + sid})
        row = (await c.execute(text("SELECT * FROM qa_sessions WHERE tenant_id=:tenant AND session_id=:sid FOR UPDATE"), params)).mappings().first()
        if row and row["user_id"] != state["user_id"]:
            raise ApiError(404, "NOT_FOUND")
        messages = list(row["messages"]) if row else []
        messages += [{"role": "user", "content": state["query"]}, {"role": "assistant", "content": answer}]
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
                    "id": uuid.uuid4(), "tenant": state["tenant_id"], "question": state["query"], "category": state.get("catalog_category_id")})).scalar_one()
    return {"enqueued_queue_id": str(queue) if queue else None}


def build_graph(checkpointer=None):
    graph = StateGraph(State)
    for node in (classify_query, generate_general, hyde_generate, multi_query_rewrite, call_rag,
                 score_check, generate_rag, web_search, generate_low, enqueue_pending, save_memory):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, "classify_query")
    graph.add_conditional_edges("classify_query", lambda s: s["query_type"], {
        "GENERAL": "generate_general", "PRECISE": "call_rag", "VAGUE": "hyde_generate", "BROAD": "multi_query_rewrite"})
    for source in ("hyde_generate", "multi_query_rewrite"):
        graph.add_edge(source, "call_rag")
    graph.add_edge("call_rag", "score_check")
    graph.add_conditional_edges("score_check", lambda s: "high" if s["top1_score"] >= .75 else "low",
                                {"high": "generate_rag", "low": "web_search"})
    graph.add_edge("web_search", "generate_low")
    graph.add_edge("generate_low", "enqueue_pending")
    for source in ("generate_general", "generate_rag", "enqueue_pending"):
        graph.add_edge(source, "save_memory")
    graph.add_edge("save_memory", END)
    return graph.compile(checkpointer=checkpointer)
