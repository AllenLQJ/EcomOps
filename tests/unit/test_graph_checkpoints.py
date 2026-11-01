"""四条工作流共用同一个 AsyncPostgresSaver：跑完请求后，检查点表里要有各自约定的 thread_id（见 module_interfaces 第 8 节）。"""
import importlib

import psycopg
import pymupdf
import pytest
from fastapi.testclient import TestClient

from ecomops.core import config
from ecomops.core.auth import issue_token
from ecomops.core.db import conninfo


@pytest.fixture()
def app_client(db_name, monkeypatch):
    monkeypatch.setenv("ECOMOPS_RAG_URL", "http://127.0.0.1:9")          # 不可达：kb 走降级，不影响检查点
    config.get_settings.cache_clear()
    import ecomops.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        c.headers["Authorization"] = "Bearer " + issue_token("OP1", "operator", "T1")
        yield c
    config.get_settings.cache_clear()


def threads(db_name):
    with psycopg.connect(conninfo(db_name)) as conn:
        return {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}


def pdf_bytes(text):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def test_kb_listing_and_training_leave_checkpoints_under_documented_thread_ids(app_client, db_name):
    assert app_client.post("/api/v1/kb/ask", json={"query": "退换几天", "session_id": "S1"}).status_code == 200
    review = app_client.post("/api/v1/listing/reviews", data={"declared_category": "HOME_APPLIANCE_HEATER"},
                             files={"file": ("a.pdf", pdf_bytes("海尔热水器 全网最低价 国家级"), "application/pdf")})
    assert review.status_code == 201, review.text
    session = app_client.post("/api/v1/training/sessions", json={}).json()
    chat = app_client.post(f"/api/v1/training/sessions/{session['session_id']}/chat/stream", json={"message": "请提供订单号"})
    assert chat.status_code == 200
    found = threads(db_name)
    assert any(t.startswith("kb:T1:S1:") for t in found), found
    assert f"listing:{review.json()['review_id']}" in found, found
    assert f"train:T1:{session['session_id']}" in found, found
