import json

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecomops.core.auth import issue_token
from ecomops.core.db import conninfo
from ecomops.core.errors import install_error_handlers
from ecomops.training import service
from ecomops.training.router import router


@pytest.fixture
def client(db_name):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    with TestClient(app) as client:
        client.headers.update(headers())
        yield client


def headers(user="trainee", tenant="shop", role="cs_agent"):
    return {"Authorization": "Bearer " + issue_token(user, role, tenant)}


def create(client, seed=None):
    response = client.post("/api/v1/training/sessions", json={} if seed is None else {"scenario_seed": seed})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def path(sid):
    return f"/api/v1/training/sessions/{sid}"


def turn(client, sid, message):
    response = client.post(path(sid) + "/chat/stream", json={"message": message})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for frame in response.text.split("\n\n"):
        if not frame:
            continue
        event_line, data_line = frame.split("\n")
        assert event_line.startswith("event: ") and data_line.startswith("data: ")
        events.append((event_line[7:], json.loads(data_line[6:])))
    names = [name for name, _ in events]
    assert names.count("stage") == 1
    token_count = names.index("stage")
    assert token_count > 1
    assert names[:token_count] == ["token"] * token_count
    assert names[token_count:] in (["stage", "done"], ["stage", "report", "done"])
    state = client.get(path(sid)).json()
    assert "".join(data["text"] for name, data in events if name == "token") == state["messages"][-1]["content"]
    return next(data for name, data in events if name == "stage"), events


def stored(db_name, sid):
    with psycopg.connect(conninfo(db_name)) as conn:
        return conn.execute("SELECT messages, summary, stage, status, report FROM training_sessions WHERE session_id=%s", (sid,)).fetchone()


def test_full_training_and_report(client, db_name):
    sid = create(client)
    for answer, expected in zip(("请提供订单号", "很抱歉让您着急", "质量问题15天退换，运费我们承担", "为您换货"), service.STAGES[1:]):
        stage, events = turn(client, sid, answer)
        assert stage == {"stage": expected, "advanced": True, "follow_up_count": 0}
        assert ("report" in [name for name, _ in events]) == (expected == "finished")
    state = client.get(path(sid)).json()
    assert state["status"] == "FINISHED"
    assert len(state["messages"]) == 8
    assert set(state["report"]) == {"rule_accuracy", "emotional_soothing", "solution_compliance"}
    assert all(type(score) is int and 0 <= score <= 100 for score in state["report"].values())
    assert stored(db_name, sid)[2:] == ("finished", "FINISHED", state["report"])
    response = client.post(path(sid) + "/chat/stream", json={"message": "退款"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_STAGE"
    assert len(stored(db_name, sid)[0]) == 8


def test_two_followups_then_forced_advance(client):
    sid = create(client, {"product": "洗衣机", "pain_point": "噪音刺耳"})
    replies = []
    for count in (1, 2):
        stage, events = turn(client, sid, "今天天气晴朗")
        assert stage == {"stage": "verify_order", "advanced": False, "follow_up_count": count}
        replies.append("".join(data["text"] for name, data in events if name == "token"))
    assert replies[0] != replies[1]
    assert all("洗衣机" in reply and "噪音刺耳" in reply for reply in replies)
    stage, _ = turn(client, sid, "今天天气晴朗")
    assert stage == {"stage": "empathy_listen", "advanced": True, "follow_up_count": 0}
    stage, _ = turn(client, sid, "很抱歉让您着急")
    assert stage == {"stage": "policy_explain", "advanced": True, "follow_up_count": 0}


@pytest.mark.parametrize("other_headers", [headers("other"), headers(tenant="other"), headers("boss", role="supervisor")])
def test_creator_only(client, other_headers):
    sid = create(client)
    for method, url, kwargs in (("get", path(sid), {}), ("post", path(sid) + "/chat/stream", {"json": {"message": "订单号"}})):
        response = getattr(client, method)(url, headers=other_headers, **kwargs)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
    assert client.get(path(sid)).json()["messages"] == []


def test_summary_after_ai_and_upsert_every_turn(client, db_name, monkeypatch):
    calls = []
    summaries = []
    original_save, original_summary = service.save, service.summarize

    async def track_save(conn, state):
        calls.append(len(state["messages"]))
        await original_save(conn, state)

    def track_summary(messages):
        summaries.append(len(messages))
        assert messages[-1]["role"] == "assistant"
        return original_summary(messages)

    monkeypatch.setattr(service, "save", track_save)
    monkeypatch.setattr(service, "summarize", track_summary)
    sid = create(client)
    for round_number in range(1, 11):
        turn(client, sid, "我不知道该说什么")
        messages, summary, *_ = stored(db_name, sid)
        assert len(messages) == round_number * 2
        assert bool(summary) == (round_number == 10)
    assert summaries == [20]
    assert calls == list(range(0, 21, 2))
    turn(client, sid, "请给您退款")
    assert len(stored(db_name, sid)[0]) == 22
    assert stored(db_name, sid)[1]
    assert calls[-1] == 22


@pytest.mark.parametrize("stage,answer", [("verify_order", "核实手机尾号"), ("empathy_listen", "很抱歉让您着急"), ("empathy_listen", "理解您的感受"), ("policy_explain", "7天退换"), ("solution_negotiate", "为您补发")])
def test_evaluate_answer(stage, answer):
    assert service.evaluate_answer(stage, answer)
    assert not service.evaluate_answer(stage, "天气晴朗")


def test_template_is_deterministic_and_segments_are_lossless():
    seed = {"product": "热水器", "pain_point": "漏水"}
    reply = service.buyer_reply(seed, "verify_order", 1)
    assert reply == service.buyer_reply(seed, "verify_order", 1)
    assert "漏水" in reply
    assert "".join(service.reply_segments(reply)) == reply
