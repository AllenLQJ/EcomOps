import importlib
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ecomops.core import config
from ecomops.core.auth import issue_token

INT = {"X-Internal-Token": "dev-internal-token"}


def H(role="supervisor", tenant="SHOP_SELF_01", user="SUP1"):
    return {"Authorization": f"Bearer {issue_token(user, role, tenant)}"}


class FakePilot:
    """execute 可切换为 500；同时充当告警 webhook 接收端。"""
    def __init__(self):
        self.app = FastAPI()
        self.executed: dict[str, dict] = {}
        self.healthy = False
        self.hooks: list[dict] = []

        @self.app.get("/internal/v1/refunds/status")
        def status(refund_idempotency_key: str):
            r = self.executed.get(refund_idempotency_key)
            return {"refund_idempotency_key": refund_idempotency_key,
                    "execution_status": "SUCCESS" if r else "NOT_FOUND", "refunded_cents": r["approved_refund_cents"] if r else 0}

        @self.app.post("/internal/v1/refunds/execute")
        async def execute(req: Request):
            from fastapi.responses import JSONResponse
            body = await req.json()
            if not self.healthy:
                return JSONResponse({"error": {"code": "BOOM"}}, status_code=500)
            self.executed.setdefault(body["refund_idempotency_key"], body)
            return {"execution_status": "SUCCESS", "idempotent_replayed": False, "refunded_cents": body["approved_refund_cents"]}

        @self.app.post("/hook")
        async def hook(req: Request):
            self.hooks.append(await req.json())
            return {"ok": True}

        @self.app.post("/internal/v1/aftersales/{claim_no}/result")
        async def result(claim_no: str):
            return {"ok": True}

    def start(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture()
def env(db_name, monkeypatch):
    fp = FakePilot()
    url = fp.start()
    monkeypatch.setenv("ECOMOPS_PILOT_URL", url)
    monkeypatch.setenv("ECOMOPS_RECONCILE_INTERVAL_SECONDS", "0.3")
    monkeypatch.setenv("ECOMOPS_OUTBOX_BACKOFF_SECONDS", "0.2")
    monkeypatch.setenv("ECOMOPS_OUTBOX_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("ECOMOPS_REFUND_CALLBACK_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ECOMOPS_ALERT_WEBHOOK_URL", url + "/hook")
    config.get_settings.cache_clear()
    import ecomops.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c, fp
    fp.stop()
    config.get_settings.cache_clear()


def wait_for(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("timeout")


def claim_status(c, no):
    return c.get(f"/api/v1/claims/{no}", headers=H()).json()["claim_status"]


def alerts(c, state=None, **kw):
    q = f"?state={state}" if state else ""
    return c.get("/api/v1/alerts" + q, headers=H(**kw)).json()["alerts"]


def failed_claim(c, no="AL1"):
    """提交 -> 主管批准 -> Pilot 持续 500 -> 重试耗尽 -> CALLBACK_FAILED_MANUAL。"""
    body = {"claim_no": no, "tenant_id": "SHOP_SELF_01", "user_id": "U1", "order_id": "O" + no, "order_item_id": "I1",
            "sku_id": "S1", "catalog_category_id": "HOME_APPLIANCE_HEATER", "item_payable_cents": 112000,
            "max_refundable_cents": 112000, "days_since_delivery": 5, "claim_type": "QUALITY_REFUND",
            "buyer_reason_text": "漏水打不着火", "evidence_urls": ["https://x.invalid/e.jpg"]}
    assert c.post("/internal/v1/claims/submit", json=body, headers=INT).status_code == 202
    assert c.post(f"/api/v1/claims/{no}/confirm", json={"action": "APPROVE"}, headers=H()).status_code == 200
    wait_for(lambda: claim_status(c, no) == "CALLBACK_FAILED_MANUAL")
    return no


def test_failed_callback_shows_as_open_alert_and_pushes_webhook(env):
    c, fp = env
    no = failed_claim(c)
    open_alerts = alerts(c, "OPEN")
    assert len(open_alerts) == 1
    a = open_alerts[0]
    assert (a["type"], a["claim_no"], a["state"]) == ("CALLBACK_FAILED_MANUAL", no, "OPEN")
    assert "500" in a["last_error"] and a["attempts"] == 2
    wait_for(lambda: fp.hooks)
    assert fp.hooks[0]["alert_id"] == a["alert_id"] and fp.hooks[0]["claim_no"] == no
    assert fp.hooks[0]["tenant_id"] == "SHOP_SELF_01"


def test_retry_after_pilot_recovers_completes_with_single_refund(env):
    c, fp = env
    no = failed_claim(c)
    aid = alerts(c, "OPEN")[0]["alert_id"]
    fp.healthy = True
    r = c.post(f"/api/v1/alerts/{aid}/retry", json={"reason": "Pilot 已恢复"}, headers=H())
    assert r.status_code == 202 and r.json()["state"] == "RETRYING"
    wait_for(lambda: claim_status(c, no) == "COMPLETED")
    assert list(fp.executed) == [f"REFUND_{no}_V1"]
    assert alerts(c, "OPEN") == []
    again = c.post(f"/api/v1/alerts/{aid}/retry", headers=H())
    assert again.status_code == 409 and again.json()["error"]["code"] == "ALERT_STATE_CONFLICT"


def test_double_click_retry_only_one_wins_and_refund_once(env):
    c, fp = env
    no = failed_claim(c)
    aid = alerts(c, "OPEN")[0]["alert_id"]
    fp.healthy = True
    with ThreadPoolExecutor(2) as pool:
        codes = sorted(f.result().status_code for f in [pool.submit(lambda: c.post(f"/api/v1/alerts/{aid}/retry", headers=H())) for _ in range(2)])
    assert codes == [202, 409]
    wait_for(lambda: claim_status(c, no) == "COMPLETED")
    assert len(fp.executed) == 1


def test_resolve_requires_reason_and_closes_alert_without_touching_pilot(env):
    c, fp = env
    no = failed_claim(c)
    aid = alerts(c, "OPEN")[0]["alert_id"]
    assert c.post(f"/api/v1/alerts/{aid}/resolve", json={"reason": "x"}, headers=H()).status_code == 400
    r = c.post(f"/api/v1/alerts/{aid}/resolve", json={"reason": "已线下退款并核对"}, headers=H())
    assert r.status_code == 200 and r.json()["state"] == "RESOLVED"
    assert alerts(c, "OPEN") == [] and [a["alert_id"] for a in alerts(c, "RESOLVED")] == [aid]
    assert claim_status(c, no) == "CALLBACK_FAILED_MANUAL" and fp.executed == {}
    assert c.post(f"/api/v1/alerts/{aid}/retry", headers=H()).status_code == 409
    assert c.post(f"/api/v1/alerts/{aid}/resolve", json={"reason": "再来一次"}, headers=H()).status_code == 409


def test_rbac_and_tenant_isolation(env):
    c, _ = env
    failed_claim(c)
    aid = alerts(c, "OPEN")[0]["alert_id"]
    assert c.get("/api/v1/alerts", headers=H(role="operator")).status_code == 403
    assert c.get("/api/v1/alerts", headers=H(role="cs_agent")).status_code == 403
    assert c.get("/api/v1/alerts").status_code == 401
    assert alerts(c, tenant="OTHER") == []
    assert c.post(f"/api/v1/alerts/{aid}/retry", headers=H(tenant="OTHER")).status_code == 404
    assert c.post(f"/api/v1/alerts/{aid}/resolve", json={"reason": "越权尝试"}, headers=H(tenant="OTHER")).status_code == 404
    assert c.post("/api/v1/alerts/stuck:AL1/retry", headers=H()).status_code == 404   # 卡住告警不可手动重试


def test_unreachable_webhook_does_not_break_outbox(env, monkeypatch):
    c, fp = env
    monkeypatch.setenv("ECOMOPS_ALERT_WEBHOOK_URL", "http://127.0.0.1:9/hook")
    config.get_settings.cache_clear()
    no = failed_claim(c, "AL2")
    assert claim_status(c, no) == "CALLBACK_FAILED_MANUAL" and len(alerts(c, "OPEN")) == 1


def test_stuck_callback_listed_but_not_actionable(env, monkeypatch):
    c, fp = env
    monkeypatch.setenv("ECOMOPS_OUTBOX_MAX_ATTEMPTS", "1000")
    monkeypatch.setenv("ECOMOPS_ALERT_STUCK_MINUTES", "0")
    monkeypatch.setenv("ECOMOPS_ALERT_WEBHOOK_URL", "")
    config.get_settings.cache_clear()
    body = {"claim_no": "ST1", "tenant_id": "SHOP_SELF_01", "user_id": "U1", "order_id": "OST", "order_item_id": "I1",
            "sku_id": "S1", "catalog_category_id": "HOME_APPLIANCE_HEATER", "item_payable_cents": 112000,
            "max_refundable_cents": 112000, "days_since_delivery": 5, "claim_type": "QUALITY_REFUND",
            "buyer_reason_text": "漏水打不着火", "evidence_urls": ["https://x.invalid/e.jpg"]}
    c.post("/internal/v1/claims/submit", json=body, headers=INT)
    c.post("/api/v1/claims/ST1/confirm", json={"action": "APPROVE"}, headers=H())
    stuck = wait_for(lambda: [a for a in alerts(c, "OPEN") if a["type"] == "CALLBACK_STUCK"])
    assert stuck[0]["alert_id"] == "stuck:ST1" and stuck[0]["claim_no"] == "ST1"
    assert c.post("/api/v1/alerts/stuck:ST1/resolve", json={"reason": "尝试处理卡住项"}, headers=H()).status_code == 404


def test_metrics_expose_outbox_backlog_and_open_alerts(env):
    c, _ = env
    failed_claim(c)
    text = c.get("/metrics").text
    assert 'ecomops_outbox_events{status="FAILED_MANUAL"} 1.0' in text
    assert "ecomops_open_alerts 1.0" in text
