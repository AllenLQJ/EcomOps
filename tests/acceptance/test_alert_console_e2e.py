"""告警控制台：真实 EcomPilot 宕机 -> 回调耗尽重试进入人工处理 -> Pilot 恢复 -> 主管在控制台重试 -> 完成且只退一次。"""
from __future__ import annotations


import httpx
import pytest


@pytest.fixture(autouse=True)
def _fast_outbox(monkeypatch):
    # 必须早于 ops_env 生效：子进程继承当前环境
    monkeypatch.setenv("ECOMOPS_OUTBOX_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("ECOMOPS_OUTBOX_BACKOFF_SECONDS", "1")


@pytest.mark.parametrize("ops_env", ["pilot"], indirect=True)
def test_pilot_down_alert_then_console_retry_refunds_once(ops_env):
    ps = ops_env.pilot
    order = ps.make_fulfilled_order("USR_AL_BUYER")
    with httpx.Client(base_url=ps.pilot_url, timeout=60.0) as pilot:
        conf = pilot.post("/api/v1/aftersales/confirm", headers=order["headers"], json={
            "order_id": order["order_id"], "order_item_id": order["order_item_id"],
            "buyer_reason_text": "热水器漏水打不着火", "evidence_urls": ["https://e.test/leak.jpg"]})
        assert conf.status_code == 202, conf.text
    claim_no = conf.json()["claim_no"]
    sup = ops_env.ops_token("supervisor", "SUP_AL")
    ops_env.wait_status(sup, claim_no, {"PENDING_SUPERVISOR_REVIEW"}, timeout=30.0)

    ps.pilot.stop()                                   # 批准时 Pilot 不可用
    assert ops_env.confirm(sup, claim_no, {"action": "APPROVE"}).status_code == 200
    ops_env.wait_status(sup, claim_no, {"CALLBACK_FAILED_MANUAL"}, timeout=60.0)

    alerts = httpx.get(f"{ops_env.url}/api/v1/alerts?state=OPEN", headers=sup).json()["alerts"]
    assert [(a["type"], a["claim_no"]) for a in alerts] == [("CALLBACK_FAILED_MANUAL", claim_no)]

    ps.pilot.start()                                  # Pilot 恢复（同端口、同库）
    r = httpx.post(f"{ops_env.url}/api/v1/alerts/{alerts[0]['alert_id']}/retry", headers=sup, json={"reason": "Pilot 已恢复"})
    assert r.status_code == 202, r.text
    ops_env.wait_status(sup, claim_no, {"COMPLETED"}, timeout=60.0)

    rows = ps.query("SELECT refunded_cents FROM refund_executions WHERE claim_no=%s", claim_no)
    assert len(rows) == 1 and rows[0]["refunded_cents"] == order["payable_cents"]
    assert httpx.get(f"{ops_env.url}/api/v1/alerts?state=OPEN", headers=sup).json()["alerts"] == []
