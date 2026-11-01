import shlex
import sys

import httpx
import pytest

from adapters import ecomrag_adapter as ad

SOURCES = [
    {"index": 2, "doc_id": "b" * 24, "title": "售后补材料", "snippet": "补充材料回到申请里提交", "score": 0.8},
    {"index": 1, "doc_id": "a" * 24, "title": "退款到账", "snippet": "原路退款 1–5 个工作日", "score": 0.99},
]


def _client_factory(handler):
    real = httpx.AsyncClient

    def make(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)
    return make


@pytest.fixture
def api():
    transport = httpx.ASGITransport(app=ad.app)
    return httpx.AsyncClient(transport=transport, base_url="http://adapter")


async def test_search_maps_sources_to_hits_sorted_and_limited(monkeypatch, api):
    seen = {}

    def handler(request):
        seen.update(request.read() and httpx.Response(200, content=request.content).json())
        return httpx.Response(200, json={"answer": "x", "route": "rag", "sources": SOURCES})
    monkeypatch.setattr(ad.httpx, "AsyncClient", _client_factory(handler))
    r = await api.post("/api/v1/rag/search", json={"query": "退款多久到账", "tenant_id": "T1", "top_k": 1})
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert [h["chunk_id"] for h in hits] == ["a" * 24]
    assert hits[0] == {"chunk_id": "a" * 24, "text": "原路退款 1–5 个工作日", "rule_source": "退款到账",
                       "is_demo_rule": False, "score": 0.99}
    assert seen["message"] == "退款多久到账" and seen["session_id"].startswith("ops-")


async def test_search_no_sources_is_empty_hits_not_error(monkeypatch, api):
    monkeypatch.setattr(ad.httpx, "AsyncClient", _client_factory(
        lambda req: httpx.Response(200, json={"answer": "没找到", "route": "fallback", "sources": []})))
    r = await api.post("/api/v1/rag/search", json={"query": "热水器几天退换"})
    assert r.status_code == 200 and r.json() == {"hits": []}


async def test_search_upstream_429_is_error_so_ops_degrades(monkeypatch, api):
    monkeypatch.setattr(ad.httpx, "AsyncClient", _client_factory(
        lambda req: httpx.Response(429, json={"error": {"code": "DAILY_RATE_LIMITED"}})))
    r = await api.post("/api/v1/rag/search", json={"query": "退款"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "UPSTREAM_ERROR"


async def test_search_upstream_unreachable(monkeypatch, api):
    def boom(req):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(ad.httpx, "AsyncClient", _client_factory(boom))
    r = await api.post("/api/v1/rag/search", json={"query": "退款"})
    assert r.status_code == 502


def _chunk(chunk_id="c" * 64):
    return {"chunk_id": chunk_id, "tenant_id": "T1", "catalog_category_id": None, "version": 1,
            "text": "热水器漏水能退吗？\n质量问题 15 天内可退换。", "rule_source": "沙盒热水器退换规则"}


def _fake_ingest(monkeypatch, tmp_path, script):
    (tmp_path / "root" / "backend").mkdir(parents=True)
    monkeypatch.setattr(ad, "ECOMRAG_ROOT", tmp_path / "root")
    monkeypatch.setattr(ad, "INGEST_CMD", " ".join(shlex.quote(x) for x in [sys.executable, "-c", script, "{dir}"]))


async def test_upsert_writes_stable_categorised_doc_and_runs_ingest(monkeypatch, tmp_path, api):
    capture = tmp_path / "cap"
    capture.mkdir()
    _fake_ingest(monkeypatch, tmp_path, f"import sys,pathlib,shutil;[shutil.copy(p, {str(capture)!r}) for p in pathlib.Path(sys.argv[1]).glob('*.md')]")
    r1 = await api.post("/internal/v1/kb/upsert", json=_chunk())
    r2 = await api.post("/internal/v1/kb/upsert", json=_chunk())
    assert r1.status_code == 200 and r1.json() == r2.json()
    files = list(capture.glob("*.md"))
    assert len(files) == 1 and files[0].name.startswith("after_sales_ecomops_")   # 前缀决定 EcomRAG 分类
    body = files[0].read_text(encoding="utf-8")
    assert body.startswith("# 热水器漏水能退吗？") and "质量问题 15 天内可退换。" in body and "沙盒热水器退换规则" in body
    import hashlib
    assert r1.json()["milvus_pk"] == hashlib.sha256(files[0].name.encode()).hexdigest()[:24]


async def test_upsert_different_chunks_get_different_docs(monkeypatch, tmp_path, api):
    capture = tmp_path / "cap"
    capture.mkdir()
    _fake_ingest(monkeypatch, tmp_path, f"import sys,pathlib,shutil;[shutil.copy(p, {str(capture)!r}) for p in pathlib.Path(sys.argv[1]).glob('*.md')]")
    a = await api.post("/internal/v1/kb/upsert", json=_chunk("a" * 64))
    b = await api.post("/internal/v1/kb/upsert", json=_chunk("b" * 64))
    assert a.json()["milvus_pk"] != b.json()["milvus_pk"] and len(list(capture.glob("*.md"))) == 2


async def test_upsert_ingest_failure_is_500(monkeypatch, tmp_path, api):
    _fake_ingest(monkeypatch, tmp_path, "import sys;print('milvus down');sys.exit(3)")
    r = await api.post("/internal/v1/kb/upsert", json=_chunk())
    assert r.status_code == 500 and "milvus down" in r.json()["error"]["message"]


async def test_upsert_missing_root_is_500(monkeypatch, tmp_path, api):
    monkeypatch.setattr(ad, "ECOMRAG_ROOT", tmp_path / "nope")
    r = await api.post("/internal/v1/kb/upsert", json=_chunk())
    assert r.status_code == 500
