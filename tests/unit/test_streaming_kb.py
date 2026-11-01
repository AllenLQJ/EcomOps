"""Live graph/SSE contracts without a database or model server."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from ecomops.core import llm
from ecomops.core.auth import Identity
from ecomops.core.errors import ApiError, install_error_handlers
from ecomops.kb import graph, service
from ecomops.kb.router import Ask, ask_stream, read, router

IDENTITY = Identity(user_id='u', tenant_id='t', role='operator')
HIT = dict(chunk_id='one', text='七天可退', score=.9, rule_source='手册', is_demo_rule=False)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(llm, 'configured', lambda: True)
    monkeypatch.setattr(llm, 'should_fail', lambda _: False)
    monkeypatch.setattr(graph.rag, 'search', AsyncMock(return_value=[HIT]))
    monkeypatch.setattr(graph.websearch, 'enabled', lambda: False)
    saved = []

    async def save_memory(state):
        saved.append(state.copy())
        return {'enqueued_queue_id': None}

    monkeypatch.setattr(graph, 'save_memory', save_memory)
    return saved


def joined(events):
    text = ''
    for name, data in events:
        if name == 'token':
            text = '' if data.get('reset') else text + data['text']
    return text


@pytest.mark.parametrize('fail_after', [None, 0, 1, 2])
async def test_live_order_reset_and_persistence(setup, monkeypatch, fail_after):
    async def model(*args, **kwargs):
        for index, delta in enumerate(['依据', '条款 ']):
            if index == fail_after:
                raise llm.LlmError('https://secret/key')
            yield delta
        if fail_after == 2:
            raise llm.LlmError('https://secret/key')

    monkeypatch.setattr(llm, 'stream', model)
    events = [item async for item in service.stream_ask(Ask(query='退货几天'), IDENTITY,
                                                      graph.build_graph(InMemorySaver()))]
    assert events[0][0] == 'meta' and events[-1][0] == 'done'
    assert all(name == 'token' for name, _ in events[1:-1])
    assert joined(events) == events[-1][1]['answer'] == setup[0]['answer']
    resets = [data for name, data in events if data.get('reset')]
    assert bool(resets) == (fail_after is not None and fail_after > 0)
    assert events[-1][1]['fallback_used'] == (fail_after is not None)
    if fail_after is None:
        assert [d['text'] for n, d in events if n == 'token'] == ['依据', '条款 ', '\n[手册/正式规则]']
    assert 'secret' not in str(events)


@pytest.mark.parametrize('query', ['你好', '退货几天', '退款'])
async def test_unconfigured_exact_legacy(setup, monkeypatch, query):
    monkeypatch.setattr(llm, 'configured', lambda: False)
    if query == '退款':
        graph.rag.search.return_value = []
    compiled = graph.build_graph()
    body = Ask(query=query)
    expected = await service.ask(body, IDENTITY, compiled)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(kb_graph=compiled)))
    response = await ask_stream(body, request, IDENTITY)
    frames = [part async for part in response.body_iterator]
    events = [(f.splitlines()[0][7:], json.loads(f.splitlines()[1][6:])) for f in frames]
    assert events[0][0] == 'meta' and events[-1][0] == 'done'
    assert joined(events) == expected['answer']
    assert all(len(d['text']) <= 40 for n, d in events if n == 'token')
    assert events[-1][1] == {key: expected[key] for key in ('answer', 'fallback_used', 'enqueued_queue_id')}


async def test_disconnect_closes_model_and_prevents_save(setup, monkeypatch):
    closed = asyncio.Event()

    async def model(*args, **kwargs):
        try:
            yield '部分'
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(llm, 'stream', model)
    monkeypatch.setattr(service, 'check_session_access', AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(kb_graph=graph.build_graph())))
    response = await ask_stream(Ask(query='退货几天'), request, IDENTITY)
    iterator = response.body_iterator
    assert 'event: meta' in await anext(iterator)
    assert 'event: token' in await anext(iterator)
    await iterator.aclose()
    await asyncio.wait_for(closed.wait(), 1)
    assert not setup


def test_preflight_404_and_sanitized_error(setup, monkeypatch):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[read] = lambda: IDENTITY
    monkeypatch.setattr(service, 'check_session_access', AsyncMock(side_effect=ApiError(404, 'NOT_FOUND')))
    with TestClient(app) as client:
        response = client.post('/api/v1/kb/ask/stream', json={'query': '退货', 'session_id': 'foreign'})
        assert response.status_code == 404
        monkeypatch.setattr(service, 'check_session_access', AsyncMock())

        async def broken(*args, **kwargs):
            yield 'meta', {}
            raise RuntimeError('https://secret/key')

        monkeypatch.setattr(service, 'stream_ask', broken)
        response = client.post('/api/v1/kb/ask/stream', json={'query': '退货'})
    assert 'event: error\ndata: {"code": "UPSTREAM_ERROR"}' in response.text
    assert 'secret' not in response.text and 'event: done' not in response.text


@pytest.mark.parametrize('owner', ['u', 'other', None])
async def test_access_query_without_writes(monkeypatch, owner):
    conn = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        mappings=lambda: SimpleNamespace(first=lambda: {'user_id': owner} if owner else None))))

    @asynccontextmanager
    async def connect():
        yield conn

    monkeypatch.setattr(service, 'get_engine', lambda: SimpleNamespace(connect=connect))
    if owner == 'other':
        with pytest.raises(ApiError) as error:
            await service.check_session_access(Ask(query='退货', session_id='s'), IDENTITY)
        assert error.value.status == 404
    else:
        await service.check_session_access(Ask(query='退货', session_id='s'), IDENTITY)
    assert conn.execute.await_count == 1
    assert 'SELECT user_id' in str(conn.execute.call_args.args[0])


@pytest.mark.parametrize('fail_after', [None, 0, 1])
async def test_low_web_stream_fallback(setup, monkeypatch, fail_after):
    graph.rag.search.return_value = []
    monkeypatch.setattr(graph.websearch, 'enabled', lambda: True)
    monkeypatch.setattr(graph.websearch, 'search', AsyncMock(return_value=[{'snippet': '搜索摘要'}]))

    async def model(system, user, **kwargs):
        assert '<web>' in user
        if fail_after == 0:
            raise llm.LlmError('secret')
        yield '参考说明'
        if fail_after == 1:
            raise llm.LlmError('secret')

    monkeypatch.setattr(llm, 'stream', model)
    events = [e async for e in service.stream_ask(Ask(query='退货'), IDENTITY, graph.build_graph())]
    assert events[0][0] == 'meta' and events[0][1]['top1_score'] == 0
    assert joined(events) == events[-1][1]['answer'] == setup[0]['answer']
    assert events[-1][1]['fallback_used']
    assert bool([d for _, d in events if d.get('reset')]) == (fail_after == 1)
    assert ('来自联网搜索，未经核实：参考说明' in joined(events)) == (fail_after is None)
    assert setup[0]['pending']


@pytest.mark.parametrize('query,injected', [('你好', False), ('退货', True), ('退货', False)])
async def test_configured_without_model_output(setup, monkeypatch, query, injected):
    monkeypatch.setattr(llm, 'should_fail', lambda _: injected)

    async def empty(*args, **kwargs):
        if False:
            yield ''

    monkeypatch.setattr(llm, 'stream', empty)
    events = [e async for e in service.stream_ask(Ask(query=query), IDENTITY, graph.build_graph())]
    assert events[0][0] == 'meta' and events[-1][0] == 'done'
    assert joined(events) == setup[0]['answer'] == events[-1][1]['answer']
    assert not any(d.get('reset') for _, d in events)
    if query != '你好':
        assert events[-1][1]['fallback_used']
