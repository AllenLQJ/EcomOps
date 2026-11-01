import hashlib

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecomops.core.auth import issue_token
from ecomops.core.db import conninfo
from ecomops.core.errors import install_error_handlers
from ecomops.kb import faults, service
from ecomops.kb.router import router
from stubs import rag_stub as rag


@pytest.mark.parametrize('query,expected', [('你好', 'GENERAL'), ('退换几天', 'PRECISE'), ('商品异常', 'VAGUE'), ('运费？以及保修？', 'BROAD')])
def test_kb_classification(query, expected):
    assert service.classify_query(query) == expected


@pytest.fixture()
def client(db_name, monkeypatch):
    original = httpx.AsyncClient
    def local_client(**kwargs):
        return original(transport=httpx.ASGITransport(app=rag.app), **kwargs)
    monkeypatch.setattr(service.httpx, 'AsyncClient', local_client)
    with rag.lock:
        rag.chunks.clear()
        rag.state['mode'] = 'none'
    faults.state['publish_writeback_fail'] = False
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    with TestClient(app) as c:
        c.headers['Authorization'] = 'Bearer ' + issue_token('u', 'operator', 'a')
        yield c
    faults.state['publish_writeback_fail'] = False


def ask(c, query='陨石商品异常', **kwargs):
    r = c.post('/api/v1/kb/ask', json={'query': query, **kwargs})
    assert r.status_code == 200, r.text
    return r.json()


def queued(c):
    return ask(c)['enqueued_queue_id']


DRAFT = dict(standard_answer='请联系客服确认处理。', catalog_category_id='cat', rule_source='正式手册')


def test_kb_retrieval_and_duplicate(client):
    assert ask(client, '你好')['enqueued_queue_id'] is None
    high = ask(client, '7天无理由退货')
    assert high['top1_score'] >= .75 and high['enqueued_queue_id'] is None
    assert '沙盒演示' in high['answer']
    assert queued(client) == queued(client)
    assert len(client.get('/api/v1/kb/pending').json()['items']) == 1


def test_kb_unavailable(client, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError('offline')
    original = service.httpx.AsyncClient
    def offline_client(**kwargs):
        c = original(**kwargs)
        c.post = unavailable
        return c
    monkeypatch.setattr(service.httpx, 'AsyncClient', offline_client)
    answer = ask(client)
    assert answer['top1_score'] == 0 and answer['enqueued_queue_id'] and answer['fallback_used']


@pytest.mark.parametrize('mode', ['none', 'fail_before_write', 'fail_after_write', 'writeback'])
def test_kb_publish_retry(client, mode):
    qid = queued(client)
    if mode == 'writeback':
        faults.state['publish_writeback_fail'] = True
    else:
        rag.state['mode'] = mode
    path = f'/api/v1/kb/pending/{qid}'
    response = client.post(path + '/publish', json=DRAFT)
    assert response.status_code == 200, response.text
    item = response.json()
    assert item['publish_status'] == ('PUBLISHED' if mode == 'none' else 'PUBLISHING' if mode == 'writeback' else 'PUBLISH_FAILED')
    chunk = hashlib.sha256(f'{qid}:1'.encode()).hexdigest()
    assert item['deterministic_chunk_id'] == chunk
    if mode != 'none':
        rag.state['mode'] = 'none'
        faults.state['publish_writeback_fail'] = False
        item = client.post(path + '/retry').json()
        assert item['publish_status'] == 'PUBLISHED' and item['version'] == 1
        assert item['deterministic_chunk_id'] == chunk
    assert len(rag.chunks) == 1 and chunk in rag.chunks
    assert client.post(path + '/publish', json=DRAFT).status_code == 409
    assert client.post(path + '/retry').status_code == 409
    assert ask(client)['top1_score'] >= .9


def test_kb_tenant_roles_and_discard(client):
    qid = queued(client)
    path = f'/api/v1/kb/pending/{qid}'
    client.headers['Authorization'] = 'Bearer ' + issue_token('u', 'operator', 'b')
    assert client.get('/api/v1/kb/pending').json()['items'] == []
    for action in ['publish', 'retry', 'discard']:
        assert client.post(path + '/' + action, json=DRAFT).status_code == 404
    client.headers['Authorization'] = 'Bearer ' + issue_token('u', 'cs_agent', 'a')
    assert ask(client, '谢谢')['query_type'] == 'GENERAL'
    for action in ['publish', 'retry', 'discard']:
        assert client.post(path + '/' + action, json=DRAFT).status_code == 403
    client.headers['Authorization'] = 'Bearer ' + issue_token('u', 'supervisor', 'a')
    assert client.post(path + '/discard').json()['publish_status'] == 'DISCARDED'
    assert client.post(path + '/publish', json=DRAFT).status_code == 409
    client.headers.clear()
    assert client.post('/api/v1/kb/ask', json={'query': '你好'}).status_code == 401


def test_kb_session_upsert_and_summary(client, db_name):
    sid = 'memory'
    for turn in range(1, 12):
        ask(client, f'你好{turn}', session_id=sid)
        with psycopg.connect(conninfo(db_name)) as c:
            rows = c.execute('SELECT messages,summary FROM qa_sessions WHERE tenant_id=%s AND session_id=%s', ('a', sid)).fetchall()
        assert len(rows) == 1
        messages, summary = rows[0]
        assert len(messages) == 2 * turn and messages[-1]['role'] == 'assistant'
        assert bool(summary) == (turn >= 10)
        if turn >= 10:
            assert '你好10' in summary and '你好11' not in summary


def test_rag_internal_token_is_sent_on_search_and_publish(client, monkeypatch):
    """接真实 EcomRAG 时每次调用都带 X-Internal-Token；没配置则不发（桩不校验）。"""
    from ecomops.core import config
    seen = []
    from httpx import _client
    original = _client.AsyncClient          # 夹具已把 httpx.AsyncClient 换成走桩的版本，这里取真正的类

    def spy_client(**kwargs):
        def handler(request):
            seen.append((request.url.path, request.headers.get("x-internal-token")))
            return httpx.Response(200, json={"hits": [], "chunk_id": "x", "milvus_pk": "y"})
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(service.httpx, 'AsyncClient', spy_client)
    monkeypatch.setenv("ECOMOPS_RAG_INTERNAL_TOKEN", "rag-secret-token")
    config.get_settings.cache_clear()
    try:
        r = client.post('/api/v1/kb/ask', json={'query': '退换几天'})
        assert r.status_code == 200
        assert ("/api/v1/rag/search", "rag-secret-token") in seen
        monkeypatch.delenv("ECOMOPS_RAG_INTERNAL_TOKEN")
        config.get_settings.cache_clear()
        seen.clear()
        client.post('/api/v1/kb/ask', json={'query': '退换几天'})
        assert seen and all(token is None for _, token in seen)
    finally:
        config.get_settings.cache_clear()
