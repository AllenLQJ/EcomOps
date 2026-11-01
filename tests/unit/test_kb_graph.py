"""KB workflow contract tests, with external calls and SQL connections replaced."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecomops.core.auth import Identity
from ecomops.core.errors import ApiError
from ecomops.kb import graph, service
from ecomops.kb.router import Ask, read, router


HIT = dict(chunk_id="one", text="七天可退", rule_source="手册", is_demo_rule=False, score=.9)
LOW = "暂未找到足够可靠的规则，已记录问题，等待人工补充确认。"


def state(**kwargs):
    return dict(query="商品异常", tenant_id="a", user_id="u", session_id="s", catalog_category_id=None,
                query_type="VAGUE", queries=["商品异常"], hits=[], top1_score=0,
                fallback_used=False, answer=LOW, enqueued_queue_id=None) | kwargs


@pytest.fixture(autouse=True)
def external(monkeypatch):
    monkeypatch.setattr(graph.llm, "configured", Mock(return_value=False))
    monkeypatch.setattr(graph.llm, "should_fail", Mock(return_value=False))
    monkeypatch.setattr(graph.llm, "complete", AsyncMock())
    monkeypatch.setattr(graph.websearch, "enabled", Mock(return_value=False))
    monkeypatch.setattr(graph.websearch, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph.rag, "search", AsyncMock(return_value=[HIT.copy()]))


@pytest.mark.asyncio
@pytest.mark.parametrize("query,kind,top_k", [("你好", "GENERAL", None), ("退换几天", "PRECISE", 8),
    ("商品异常", "VAGUE", 10), ("运费？还有保修？", "BROAD", 4), ("未知", "VAGUE", 10)])
async def test_workflow_branches(monkeypatch, query, kind, top_k):
    if query == "未知":
        graph.rag.search.return_value = []
    saved = []
    async def save_memory(s):
        saved.append(s.copy())
        return {"enqueued_queue_id": "queue" if s.get("pending") else None}
    monkeypatch.setattr(graph, "save_memory", save_memory)
    result = await graph.build_graph().ainvoke(state(query=query))
    assert result["query_type"] == kind and len(saved) == 1
    if top_k is None:
        graph.rag.search.assert_not_awaited()
        assert result["answer"] == "您好，请告诉我您需要咨询的商品或售后问题。"
    else:
        assert all(c.kwargs["top_k"] == top_k for c in graph.rag.search.await_args_list)
        assert result["answer"] == (LOW if query == "未知" else "七天可退 [手册/正式规则]")
    assert result["enqueued_queue_id"] == ("queue" if query == "未知" else None)
    graph.websearch.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("node,success,fallback", [
    (graph.hyde_generate, "假设条款", ["商品异常 商品售后政策说明"]),
    (graph.multi_query_rewrite, {"queries": ["甲", "乙"]}, ["商品异常"]),
])
@pytest.mark.parametrize("mode", ["success", "failure", "unconfigured", "invalid"])
async def test_rewrite_modes(node, success, fallback, mode):
    graph.llm.configured.return_value = mode != "unconfigured"
    graph.llm.complete.return_value = success if mode == "success" else {"queries": [1]}
    if mode == "failure":
        graph.llm.complete.side_effect = graph.llm.LlmError("failed")
    result = await node(state())
    expected = ([success] if isinstance(success, str) else success["queries"]) if mode == "success" else fallback
    assert result["queries"] == expected
    if mode == "unconfigured":
        graph.llm.complete.assert_not_awaited()
    else:
        assert "<query>" in graph.llm.complete.call_args.args[1]
    if mode in ("failure", "invalid"):
        assert result["fallback_used"]


@pytest.mark.asyncio
async def test_broad_parallel_dedup_and_degradation():
    active = 0
    peak = 0
    async def search(query, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return [HIT | {"score": float(query)}]
    graph.rag.search.side_effect = search
    result = await graph.call_rag(state(query_type="BROAD", queries=["0.8", "0.9", "0.7"]))
    assert peak == 3 and result["hits"] == [HIT]
    graph.rag.search.side_effect = None
    graph.rag.search.return_value = None
    assert await graph.call_rag(state()) == {"hits": [], "degraded": True, "fallback_used": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,score", [(False, .2), (True, .2), (True, .9)])
async def test_web_only_low_enabled(monkeypatch, enabled, score):
    graph.websearch.enabled.return_value = enabled
    graph.websearch.search.return_value = [{"snippet": "联网摘要"}]
    graph.rag.search.return_value = [HIT | {"score": score}]
    graph.llm.configured.return_value = True
    graph.llm.complete.return_value = "参考说明"
    saved = []
    async def save_memory(s):
        saved.append(s)
        return {"enqueued_queue_id": "queue" if s.get("pending") else None}
    monkeypatch.setattr(graph, "save_memory", save_memory)
    result = await graph.build_graph().ainvoke(state(query="退换几天"))
    assert graph.websearch.search.await_count == int(enabled and score < .75)
    assert result["enqueued_queue_id"] == ("queue" if score < .75 else None)
    assert result["hits"] == [HIT | {"score": score}]
    if enabled and score < .75:
        assert "来自联网搜索，未经核实" in result["answer"]
        assert "<web>" in graph.llm.complete.call_args.args[1]
    else:
        assert "联网摘要" not in result["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "failure", "unconfigured", "injected"])
async def test_rag_generation(mode):
    graph.llm.configured.return_value = mode != "unconfigured"
    graph.llm.should_fail.return_value = mode == "injected"
    graph.llm.complete.return_value = "依据规则回答"
    if mode == "failure":
        graph.llm.complete.side_effect = graph.llm.LlmError("offline")
    s = state(hits=[HIT, HIT | {"is_demo_rule": True}])
    result = await graph.generate_rag(s)
    assert result["answer"].endswith("[手册/沙盒演示]")
    if mode == "success":
        assert "<evidence>" in graph.llm.complete.call_args.args[1]
        assert "<query>" in graph.llm.complete.call_args.args[1]
    else:
        assert result["answer"] == "七天可退 [手册/正式规则]\n七天可退 [手册/沙盒演示]"
    if mode == "failure":
        assert result["fallback_used"]
    if mode == "injected":
        assert (await graph.classify_query(s))["fallback_used"]
        graph.llm.complete.assert_not_awaited()


class Connection:
    def __init__(self, row):
        self.row, self.calls = row, []

    async def execute(self, sql, params):
        sql = str(sql)
        self.calls.append((sql, params))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: self.row), scalar_one=lambda: "queue")


def fake_engine(monkeypatch, row):
    conn = Connection(row)
    @asynccontextmanager
    async def begin():
        yield conn
    monkeypatch.setattr(graph, "get_engine", lambda: SimpleNamespace(begin=begin))
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize("final_length", [19, 20, 21])
async def test_save_memory_summary_boundary_and_upsert(monkeypatch, final_length):
    messages = [{"role": "user", "content": "历史问题"} for _ in range(final_length - 2)]
    conn = fake_engine(monkeypatch, {"user_id": "u", "messages": messages, "summary": "旧摘要"})
    await graph.save_memory(state(query_type="GENERAL"))
    writes = [(sql, params) for sql, params in conn.calls if "INSERT INTO qa_sessions" in sql]
    assert len(writes) == 1
    sql, params = writes[0]
    assert "ON CONFLICT" in sql
    saved = json.loads(params["messages"])
    assert len(saved) == final_length and saved[-1]["role"] == "assistant"
    assert (params["summary"] != "旧摘要") == (final_length == 20)
    assert "pg_advisory_xact_lock" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_cross_user_session_before_any_write(monkeypatch):
    conn = fake_engine(monkeypatch, {"user_id": "other", "messages": [], "summary": ""})
    with pytest.raises(ApiError) as exc:
        await graph.save_memory(state())
    assert exc.value.status == 404
    assert not any("INSERT" in sql for sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_pending_sql_only_question_not_web_results(monkeypatch):
    conn = fake_engine(monkeypatch, None)
    result = await graph.save_memory(state(web_results=[{"snippet": "不得入库"}], pending=True))
    assert result == {"enqueued_queue_id": "queue"}
    sql, params = conn.calls[-1]
    assert "knowledge_pending_queue" in sql and "ON CONFLICT" in sql
    assert params["question"] == "商品异常"
    assert "不得入库" not in json.dumps([p for _, p in conn.calls], default=str, ensure_ascii=False)


@pytest.mark.asyncio
async def test_service_thread_and_response():
    result = state()
    mock_graph = SimpleNamespace(ainvoke=AsyncMock(return_value=result))
    identity = Identity(user_id="u", tenant_id="a", role="operator")
    await service.ask(Ask(query="你好", session_id="s"), identity, mock_graph)
    await service.ask(Ask(query="你好", session_id="s"), identity, mock_graph)
    ids = [c.kwargs["config"]["configurable"]["thread_id"] for c in mock_graph.ainvoke.await_args_list]
    assert all(t.startswith("kb:a:s:") for t in ids) and ids[0] != ids[1]


@pytest.mark.parametrize("failed", [False, True])
def test_sse_sequence(monkeypatch, failed):
    app = FastAPI()
    app.include_router(router)
    app.state.kb_graph = object()
    app.dependency_overrides[read] = lambda: Identity(user_id="u", tenant_id="a", role="operator")
    result = state(answer="回答" * 51)
    ask = AsyncMock(return_value=result, side_effect=RuntimeError("secret URL") if failed else None)
    monkeypatch.setattr(service, "ask", ask)
    with TestClient(app) as client:
        response = client.post("/api/v1/kb/ask/stream", json={"query": "你好"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    frames = [part.splitlines() for part in response.text.strip().split("\n\n")]
    names = [f[0].removeprefix("event: ") for f in frames]
    if failed:
        assert names == ["error"] and "secret" not in response.text
    else:
        assert names == ["meta", "token", "token", "token", "done"]
        data = [json.loads(f[1].removeprefix("data: ")) for f in frames]
        assert set(data[0]) == {"query_type", "top1_score", "hits", "session_id"}
        assert all(len(d["text"]) <= 40 for d in data[1:-1])
        assert "".join(d["text"] for d in data[1:-1]) == result["answer"]
        assert set(data[-1]) == {"answer", "fallback_used", "enqueued_queue_id"}
    assert ask.call_args.kwargs["graph"] is app.state.kb_graph


@pytest.mark.asyncio
async def test_broad_rule_split_preserves_legacy_limit():
    result = await graph.multi_query_rewrite(state(query="甲以及乙还有丙顺便丁？"))
    assert result["queries"] == ["甲", "乙", "丙"]
    assert (await graph.multi_query_rewrite(state(query="？？")))["queries"] == []
