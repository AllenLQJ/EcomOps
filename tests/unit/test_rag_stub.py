import pytest
from fastapi.testclient import TestClient
from stubs import rag_stub as rag


@pytest.fixture()
def rag_client():
    with rag.lock:
        rag.chunks.clear()
        rag.state['mode'] = 'none'
    with TestClient(rag.app) as client:
        yield client
    with rag.lock:
        rag.chunks.clear()
        rag.state['mode'] = 'none'


def test_upsert_faults_and_tenant_search(rag_client):
    c = rag_client
    chunk = dict(chunk_id='one', tenant_id='a', catalog_category_id='cat', text='特殊问题的答案', rule_source='手册', version=1)
    c.post('/stub/admin/fault', json={'mode': 'fail_before_write'})
    assert c.post('/internal/v1/kb/upsert', json=chunk).status_code == 500
    assert c.get('/stub/admin/chunks/count').json() == {'total': 0}
    c.post('/stub/admin/fault', json={'mode': 'fail_after_write'})
    assert c.post('/internal/v1/kb/upsert', json=chunk).status_code == 500
    c.post('/stub/admin/fault', json={'mode': 'none'})
    assert c.post('/internal/v1/kb/upsert', json=chunk).status_code == 200
    assert c.get('/stub/admin/chunks', params={'chunk_id': 'one'}).json()['count'] == 1
    assert c.get('/stub/admin/chunks/count').json()['total'] == 1
    assert c.post('/api/v1/rag/search', json={'query': '特殊问题', 'tenant_id': 'a'}).json()['hits'][0]['score'] >= .9
    assert all(h['chunk_id'] != 'one' for h in c.post('/api/v1/rag/search', json={'query': '特殊问题', 'tenant_id': 'b'}).json()['hits'])
    assert c.get('/health').status_code == 200


def test_demo_and_validation(rag_client):
    hits = rag_client.post('/api/v1/rag/search', json={'query': '7天无理由', 'top_k': 1}).json()['hits']
    assert len(hits) == 1 and hits[0]['is_demo_rule'] and hits[0]['score'] >= .75
    assert rag_client.post('/stub/admin/fault', json={'mode': 'unknown'}).status_code == 422
