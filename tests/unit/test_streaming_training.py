"""Training live streaming against mocked persistence."""
import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from ecomops.core import llm
from ecomops.core.auth import Identity
from ecomops.core.errors import ApiError, install_error_handlers
from ecomops.training import graph, service
from ecomops.training.router import ChatMessage, chat_stream, identity_dependency, router

IDENTITY = Identity(user_id='u', tenant_id='t', role='cs_agent')


@pytest.fixture
def setup(monkeypatch):
    data = dict(session_id=str(uuid4()), tenant_id='t', user_id='u', scenario_seed={'product': '杯子'},
                stage='verify_order', follow_up_count=0, messages=[], summary='', report=None, status='ACTIVE')
    saves = []

    async def load(*args, **kwargs):
        return deepcopy(data)

    async def save(conn, state):
        saves.append(deepcopy(state))

    @asynccontextmanager
    async def connection():
        yield None

    monkeypatch.setattr(service, 'load', load)
    monkeypatch.setattr(service, 'save', save)
    monkeypatch.setattr(service, 'get_engine', lambda: SimpleNamespace(begin=connection, connect=connection))
    monkeypatch.setattr(llm, 'configured', lambda: True)
    return data, saves


def joined(events):
    answer = ''
    for name, data in events:
        if name == 'token':
            answer = '' if data.get('reset') else answer + data['text']
    return answer


@pytest.mark.parametrize('fail_after', [None, 0, 1, 2])
@pytest.mark.parametrize('finished', [False, True])
async def test_stage_stream_reset_report_and_saved_reply(setup, monkeypatch, fail_after, finished):
    data, saves = setup
    if finished:
        data['stage'] = 'solution_negotiate'

    async def model(*args, **kwargs):
        for index, delta in enumerate(['请', '解释 ']):
            if index == fail_after:
                raise llm.LlmError('secret URL')
            yield delta
        if fail_after == 2:
            raise llm.LlmError('secret URL')

    monkeypatch.setattr(llm, 'stream', model)
    events = [item async for item in service.stream_chat(data['session_id'], IDENTITY,
        '退款' if finished else '订单号', graph.build_graph(InMemorySaver()))]
    assert events[0][0] == 'stage' and events[-1] == ('done', {'session_id': data['session_id']})
    assert bool([d for n, d in events if d.get('reset')]) == (fail_after is not None and fail_after > 0)
    assert joined(events) == saves[0]['messages'][-1]['content']
    assert bool([n for n, _ in events if n == 'report']) == finished
    if finished:
        assert [n for n, _ in events][-2:] == ['report', 'done']
        assert saves[0]['status'] == 'FINISHED'
    if fail_after is None:
        assert joined(events) == '请解释 '
    else:
        assert joined(events) == service.buyer_reply(data['scenario_seed'], saves[0]['stage'], 0)
    assert 'secret' not in str(events)


async def test_disconnect_cancels_and_does_not_save(setup, monkeypatch):
    data, saves = setup
    closed = asyncio.Event()

    async def model(*args, **kwargs):
        try:
            yield '部分'
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(llm, 'stream', model)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(training_graph=graph.build_graph())))
    response = await chat_stream(data['session_id'], ChatMessage(message='订单号'), request, IDENTITY)
    iterator = response.body_iterator
    assert 'event: stage' in await anext(iterator)
    assert 'event: token' in await anext(iterator)
    await iterator.aclose()
    await asyncio.wait_for(closed.wait(), 1)
    assert not saves


async def test_unconfigured_legacy_segments_and_order(setup, monkeypatch):
    data, saves = setup
    monkeypatch.setattr(llm, 'configured', lambda: False)
    compiled = graph.build_graph()
    state, reply, advanced = await service.chat(data['session_id'], IDENTITY, '订单号', compiled)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(training_graph=compiled)))
    response = await chat_stream(data['session_id'], ChatMessage(message='订单号'), request, IDENTITY)
    frames = [part async for part in response.body_iterator]
    events = [(f.splitlines()[0][7:], json.loads(f.splitlines()[1][6:])) for f in frames]
    assert events[:-2] == [('token', {'text': segment}) for segment in service.reply_segments(reply)]
    assert events[-2] == ('stage', {'stage': state['stage'], 'advanced': advanced, 'follow_up_count': state['follow_up_count']})
    assert events[-1] == ('done', {'session_id': data['session_id']})
    assert joined(events) == saves[-1]['messages'][-1]['content']


def test_preflight_finished_422_and_sanitized_error(setup, monkeypatch):
    data, saves = setup
    data.update(status='FINISHED', stage='finished')
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[identity_dependency] = lambda: IDENTITY
    with TestClient(app) as client:
        url = '/api/v1/training/sessions/' + data['session_id'] + '/chat/stream'
        response = client.post(url, json={'message': '继续'})
        assert response.status_code == 422 and response.json()['error']['code'] == 'INVALID_STAGE'
        assert not saves
        data.update(status='ACTIVE', stage='verify_order')

        async def broken(*args, **kwargs):
            yield 'stage', {}
            raise RuntimeError('secret URL')

        monkeypatch.setattr(service, 'stream_chat', broken)
        response = client.post(url, json={'message': '继续'})
    assert 'event: error\ndata: {"code": "UPSTREAM_ERROR"}' in response.text
    assert 'secret' not in response.text and 'event: done' not in response.text


async def test_foreign_session_preflight_404(setup, monkeypatch):
    monkeypatch.setattr(service, 'load', AsyncMock(side_effect=ApiError(404, 'NOT_FOUND')))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(ApiError) as error:
        await chat_stream(setup[0]['session_id'], ChatMessage(message='继续'), request, IDENTITY)
    assert error.value.status == 404


@pytest.mark.parametrize('mode', ['success', 'empty', 'whitespace', 'failure'])
async def test_policy_challenge_is_in_final_saved_text(setup, monkeypatch, mode):
    data, saves = setup
    data['stage'] = 'policy_explain'
    monkeypatch.setattr(graph.rag, 'search', AsyncMock(return_value=[{'text': '7天退换'}]))

    async def model(*args, **kwargs):
        if mode == 'success':
            yield '请核对'
        elif mode == 'whitespace':
            yield '  '
        elif mode == 'failure':
            yield '半截'
            raise llm.LlmError('secret')

    monkeypatch.setattr(llm, 'stream', model)
    events = [e async for e in service.stream_chat(data['session_id'], IDENTITY, '15天退换', graph.build_graph())]
    assert events[0] == ('stage', {'stage': 'policy_explain', 'advanced': False, 'follow_up_count': 1})
    assert joined(events) == saves[0]['messages'][-1]['content']
    assert joined(events).endswith('你引用的天数、金额或比例与条款不一致，请核对条款后解释。')
    assert bool([d for _, d in events if d.get('reset')]) == (mode in ('whitespace', 'failure'))


async def test_checkpoint_does_not_retain_streaming_mode(setup, monkeypatch):
    data, saves = setup

    async def model(*args, **kwargs):
        yield '流式'

    monkeypatch.setattr(llm, 'stream', model)
    monkeypatch.setattr(llm, 'complete', AsyncMock(return_value='完整回复'))
    compiled = graph.build_graph(InMemorySaver())
    _ = [e async for e in service.stream_chat(data['session_id'], IDENTITY, '订单号', compiled)]
    _, reply, _ = await service.chat(data['session_id'], IDENTITY, '订单号', compiled)
    assert reply == '完整回复'
    llm.complete.assert_awaited_once()
