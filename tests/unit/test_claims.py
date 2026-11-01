import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from ecomops.core import config, db
from ecomops.core.auth import issue_token

INT = {"X-Internal-Token": "dev-internal-token"}


def payload(no="CLM_T1", amount=112000, days=5, reason="漏水打不着火", urls=("https://x.invalid/e.jpg",), tenant="SHOP_SELF_01"):
    return {"claim_no": no, "tenant_id": tenant, "user_id": "U1", "order_id": "O1", "order_item_id": "I1", "sku_id": "S1",
            "catalog_category_id": "HOME_APPLIANCE_HEATER", "item_payable_cents": amount, "max_refundable_cents": amount,
            "days_since_delivery": days, "claim_type": "QUALITY_REFUND", "buyer_reason_text": reason, "evidence_urls": list(urls)}


class FakePilot:
    """记录 refunds/execute 调用；首次 execute 可注入"已执行但响应超时"。"""
    def __init__(self):
        self.app = FastAPI()
        self.executed: dict[str, dict] = {}
        self.calls = 0
        self.drop_first = False
        self.results: list = []

        @self.app.get("/internal/v1/refunds/status")
        def status(refund_idempotency_key: str):
            r = self.executed.get(refund_idempotency_key)
            return {"refund_idempotency_key": refund_idempotency_key,
                    "execution_status": "SUCCESS" if r else "NOT_FOUND", "refunded_cents": r["approved_refund_cents"] if r else 0}

        @self.app.post("/internal/v1/refunds/execute")
        async def execute(req: Request):
            body = await req.json()
            self.calls += 1
            replay = body["refund_idempotency_key"] in self.executed
            self.executed.setdefault(body["refund_idempotency_key"], body)
            if self.drop_first and self.calls == 1:
                await asyncio.sleep(5)
            return {"execution_status": "SUCCESS", "idempotent_replayed": replay, "refunded_cents": body["approved_refund_cents"]}

        @self.app.post("/internal/v1/aftersales/{claim_no}/result")
        async def result(claim_no: str, req: Request):
            self.results.append((claim_no, (await req.json())["result"]))
            return {"ok": True}

    def start(self):
        s = socket_free()
        self.port = s
        self.server = uvicorn.Server(uvicorn.Config(self.app, port=s, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        return f"http://127.0.0.1:{s}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


def socket_free():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def env(db_name, monkeypatch):
    fp = FakePilot()
    url = fp.start()
    monkeypatch.setenv("ECOMOPS_PILOT_URL", url)
    monkeypatch.setenv("ECOMOPS_RECONCILE_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("ECOMOPS_OUTBOX_BACKOFF_SECONDS", "0.5")
    monkeypatch.setenv("ECOMOPS_REFUND_CALLBACK_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ECOMOPS_ENABLE_FAULTS", "1")
    config.get_settings.cache_clear()
    import importlib
    import ecomops.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c, fp
    fp.stop()
    config.get_settings.cache_clear()


def H(role="supervisor", tenant="SHOP_SELF_01", user="SUP1"):
    return {"Authorization": f"Bearer {issue_token(user, role, tenant)}"}


def detail(c, no, **kw):
    return c.get(f"/api/v1/claims/{no}", headers=H(**kw)).json()


def wait(c, no, targets, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        d = detail(c, no)
        if d["claim_status"] in targets:
            return d
        time.sleep(0.2)
    raise AssertionError(f"{no} stuck at {d['claim_status']} wanted {targets}")


def test_routing_matrix(env):
    c, fp = env
    r = c.post("/internal/v1/claims/submit", json=payload("A1", amount=4000, days=20, reason="不想要", urls=()), headers=INT)
    assert r.status_code == 202 and r.json()["status"] == "AUTO_REJECTED"
    r = c.post("/internal/v1/claims/submit", json=payload("A2", urls=()), headers=INT)
    assert r.json()["status"] == "NEED_MORE_EVIDENCE"
    r = c.post("/internal/v1/claims/submit", json=payload("A4"), headers=INT)
    d = detail(c, "A4")
    assert r.json()["status"] == "PENDING_SUPERVISOR_REVIEW" and d["risk_level"] == "HIGH"
    assert {t["track"] for t in d["tracks"]} == {"RULE", "EVIDENCE", "FEE"}
    r = c.post("/internal/v1/claims/submit", json=payload("A5", amount=3000, days=3, reason="感觉不太好", urls=()), headers=INT)
    assert r.json()["status"] == "PENDING_SUPERVISOR_REVIEW"
    r = c.post("/internal/v1/claims/submit", json=payload("A3", amount=4000, days=3, reason="坏了"), headers=INT)
    assert r.json()["status"] in ("APPROVED_PENDING_CALLBACK", "COMPLETED")
    d = wait(c, "A3", {"COMPLETED"})
    assert d["approvals"][0]["action"] == "AUTO_APPROVE" and d["risk_level"] == "LOW"
    assert list(fp.executed) == ["REFUND_A3_V1"]
    assert wait(c, "A1", {"AUTO_REJECTED"}) and ("A1", "REJECTED") in _wait_results(fp, 1)


def _wait_results(fp, n):
    end = time.time() + 10
    while time.time() < end and len(fp.results) < n:
        time.sleep(0.2)
    return fp.results


def test_submit_idempotent_and_fallback(env):
    c, _ = env
    a = c.post("/internal/v1/claims/submit", json=payload("B1"), headers=INT).json()
    b = c.post("/internal/v1/claims/submit", json=payload("B1"), headers=INT).json()
    assert a["idempotent_replayed"] is False and b["idempotent_replayed"] is True
    assert len(detail(c, "B1")["tracks"]) == 3
    assert c.post("/internal/v1/claims/submit", json=payload("B2"), headers={"X-Internal-Token": "bad"}).status_code == 401
    c.post("/internal/v1/faults", json={"llm_fail": ["evidence_track"]}, headers=INT)
    r = c.post("/internal/v1/claims/submit", json=payload("B3", amount=3000, days=2, reason="坏了"), headers=INT)
    d = detail(c, "B3")
    assert r.json()["status"] == "PENDING_SUPERVISOR_REVIEW" and d["fallback_used"] is True


def test_confirm_concurrent_approve_single_callback_with_timeout(env):
    c, fp = env
    fp.drop_first = True
    c.post("/internal/v1/claims/submit", json=payload("C1"), headers=INT)
    with ThreadPoolExecutor(2) as ex:
        rs = list(ex.map(lambda _: c.post("/api/v1/claims/C1/confirm", json={"action": "APPROVE"}, headers=H()), range(2)))
    assert [r.status_code for r in rs] == [200, 200], [r.text for r in rs]
    assert sorted(r.json()["idempotent_replayed"] for r in rs) == [False, True]
    d = wait(c, "C1", {"COMPLETED"}, timeout=30)
    assert len(d["approvals"]) == 1 and d["approved_refund_cents"] == 112000
    assert list(fp.executed) == ["REFUND_C1_V1"]   # 首次回调已执行但响应超时；对账先查 status=SUCCESS，不再重发
    assert fp.calls == 1
    assert d["outbox"]["status"] == "DONE"


def test_confirm_rules_and_rbac(env):
    c, _ = env
    c.post("/internal/v1/claims/submit", json=payload("D1"), headers=INT)
    assert c.post("/api/v1/claims/D1/confirm", json={"action": "APPROVE"}, headers=H("operator")).status_code == 403
    assert c.post("/api/v1/claims/D1/confirm", json={"action": "APPROVE"}).status_code == 401
    assert c.post("/api/v1/claims/D1/confirm", json={"action": "APPROVE"}, headers=H(tenant="OTHER")).status_code == 404
    assert c.get("/api/v1/claims", headers=H("cs_agent")).status_code == 403
    for amt in (0, 112001):
        r = c.post("/api/v1/claims/D1/confirm", json={"action": "MODIFY_AMOUNT", "amount_cents": amt}, headers=H())
        assert r.status_code == 422 and r.json()["error"]["code"] == "AMOUNT_EXCEEDS_LIMIT"
    r = c.post("/api/v1/claims/D1/confirm", json={"action": "MODIFY_AMOUNT", "amount_cents": 90000}, headers=H())
    assert r.status_code == 200
    d = wait(c, "D1", {"COMPLETED"})
    assert d["approved_refund_cents"] == 90000
    assert c.post("/api/v1/claims/D1/confirm", json={"action": "MODIFY_AMOUNT", "amount_cents": 90000}, headers=H()).json()["idempotent_replayed"] is True
    r = c.post("/api/v1/claims/D1/confirm", json={"action": "REJECT"}, headers=H())
    assert r.status_code == 409 and r.json()["error"]["code"] == "CLAIM_STATE_CONFLICT"


def test_reject_request_evidence_and_evidence_flow(env):
    c, fp = env
    c.post("/internal/v1/claims/submit", json=payload("E1"), headers=INT)
    assert c.post("/api/v1/claims/E1/confirm", json={"action": "REJECT", "reason": "无理由"}, headers=H()).json()["claim_status"] == "SUPERVISOR_REJECTED"
    assert ("E1", "REJECTED") in _wait_results(fp, 1)
    c.post("/internal/v1/claims/submit", json=payload("E2"), headers=INT)
    assert c.post("/api/v1/claims/E2/confirm", json={"action": "REQUEST_EVIDENCE"}, headers=H()).json()["claim_status"] == "NEED_MORE_EVIDENCE"
    r = c.post("/internal/v1/claims/E2/evidence", json={"evidence_urls": ["https://x.invalid/2.jpg"]}, headers=INT)
    assert r.status_code == 202 and r.json()["status"] == "PENDING_SUPERVISOR_REVIEW"
    assert detail(c, "E2")["audit_round"] == 2
    assert c.post("/internal/v1/claims/E1/evidence", json={"evidence_urls": ["u"]}, headers=INT).status_code == 409
    assert c.post("/internal/v1/claims/E2/withdraw", headers=INT).json()["claim_status"] == "WITHDRAWN"
    assert c.post("/internal/v1/claims/E1/withdraw", headers=INT).status_code == 409


def test_checkpoint_survives_app_restart(db_name, monkeypatch):
    """挂起后丢弃整个应用（新 graph、新连接池），仍可从 PG 检查点恢复并完成批准。"""
    import importlib
    monkeypatch.setenv("ECOMOPS_PILOT_URL", "http://127.0.0.1:1")
    config.get_settings.cache_clear()
    import ecomops.main as m
    importlib.reload(m)
    with TestClient(m.app) as c1:
        c1.post("/internal/v1/claims/submit", json=payload("R1"), headers=INT)
    importlib.reload(m)
    with TestClient(m.app) as c2:
        assert detail(c2, "R1")["claim_status"] == "PENDING_SUPERVISOR_REVIEW"
        r = c2.post("/api/v1/claims/R1/confirm", json={"action": "APPROVE"}, headers=H())
        assert r.status_code == 200 and r.json()["claim_status"] == "APPROVED_PENDING_CALLBACK"
    config.get_settings.cache_clear()


def test_internal_status_is_minimal_and_requires_token(env):
    c, _ = env
    c.post("/internal/v1/claims/submit", json=payload("ST1"), headers=INT)
    assert c.get("/internal/v1/claims/ST1/status").status_code == 401
    r = c.get("/internal/v1/claims/ST1/status", headers=INT)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"claim_no", "claim_status", "audit_round", "needs_evidence", "evidence_count"}
    assert body["needs_evidence"] is False
    c.post("/api/v1/claims/ST1/confirm", json={"action": "REQUEST_EVIDENCE"}, headers=H())
    assert c.get("/internal/v1/claims/ST1/status", headers=INT).json()["needs_evidence"] is True
    assert c.get("/internal/v1/claims/NOPE/status", headers=INT).status_code == 404
