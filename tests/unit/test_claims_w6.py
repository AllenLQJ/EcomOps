"""条款参与三轨、驳回原因回写、超时未补材料自动撤回、撤回后重新申请。"""
import importlib
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ecomops.claims import tracks
from ecomops.core import config
from ecomops.core.auth import issue_token

INT = {"X-Internal-Token": "dev-internal-token"}


def H(role="supervisor", tenant="SHOP_SELF_01", user="SUP1"):
    return {"Authorization": f"Bearer {issue_token(user, role, tenant)}"}


def ticket(days=5, amount=112000, reason="漏水打不着火", urls=("https://x.invalid/e.jpg",)):
    return {"days_since_delivery": days, "item_payable_cents": amount, "max_refundable_cents": amount,
            "buyer_reason_text": reason, "evidence_urls": list(urls)}


def payload(no, **kw):
    t = ticket(**kw)
    return {"claim_no": no, "tenant_id": "SHOP_SELF_01", "user_id": "U1", "order_id": "O" + no, "order_item_id": "I1",
            "sku_id": "S1", "catalog_category_id": "HOME_APPLIANCE_HEATER", "claim_type": "QUALITY_REFUND",
            "evidence_urls": t["evidence_urls"], **{k: v for k, v in t.items() if k != "evidence_urls"}}


# ---------------- 条款解析（纯函数）
def clause(text, score=0.9, source="沙盒规则", **kw):
    return {"text": text, "score": score, "rule_source": source, **kw}


@pytest.mark.parametrize("policy,expected", [
    ([clause("热水器质量问题30天内可退换，运费由商家承担。")], (30, "沙盒规则")),
    ([clause("热水器质量问题15天内可退换，质量问题退换运费由商家承担。")], (15, "沙盒规则")),
    ([clause("7天无理由退货需商品完好，非质量问题运费由买家承担。")], None),            # 无理由/非质量问题不能缩短质量问题窗口
    ([clause("非质量问题7天内可退货。")], None),
    ([clause("质量问题30天内可退换。", score=0.6)], None),                               # 低置信条款不采信
    ([clause("质量问题120天内可退换。")], None),                                         # 超出合理范围
    ([clause("质量问题的处理流程如下，请联系客服。")], None),                               # 没有天数
    ([clause("质量问题7天内可退换。", score=0.8, source="低分"), clause("质量问题45天内可退换。", score=0.95, source="高分")], (45, "高分")),
    ([], None),
])
def test_clause_window_days(policy, expected):
    assert tracks.clause_window_days(policy) == expected


def test_rule_track_uses_clause_window_and_records_citations():
    policy = [clause("质量问题30天内可退换。", version=3, is_demo_rule=True)]
    within = tracks.rule_track(ticket(days=20), tracks.clause_window_days(policy), policy)
    assert within["passed"] is True and within["detail"]["window_days"] == 30
    assert within["detail"]["window_source"] == "clause" and within["detail"]["window_clause"] == "沙盒规则"
    assert within["detail"]["policy_clauses"] == [{"rule_source": "沙盒规则", "version": 3, "score": 0.9, "is_demo_rule": True}]
    assert tracks.rule_track(ticket(days=20))["passed"] is False                       # 默认 15 天窗口时同一单会被判超期
    assert tracks.rule_track(ticket(days=20))["detail"]["window_source"] == "default"


async def test_run_three_cites_clauses_in_rule_and_fee_tracks():
    policy = [clause("质量问题15天内可退换。")]
    out = await tracks.run_three(ticket(), policy)
    assert out["RULE"]["detail"]["policy_clauses"][0]["rule_source"] == "沙盒规则"
    assert out["FEE"]["detail"]["policy_clauses"][0]["rule_source"] == "沙盒规则"
    plain = await tracks.run_three(ticket())
    assert "policy_clauses" not in plain["RULE"]["detail"]                              # 没检索到条款时与旧输出一致


# ---------------- 带 Pilot 回写记录的集成夹具
class FakePilot:
    def __init__(self):
        self.app = FastAPI()
        self.results: list[tuple[str, dict]] = []

        @self.app.post("/internal/v1/aftersales/{claim_no}/result")
        async def result(claim_no: str, req: Request):
            self.results.append((claim_no, await req.json()))
            return {"ok": True}

        @self.app.get("/internal/v1/refunds/status")
        def status(refund_idempotency_key: str):
            return {"execution_status": "NOT_FOUND"}

    def start(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        return f"http://127.0.0.1:{port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


def make_env(monkeypatch, db_name, **env):
    fp = FakePilot()
    monkeypatch.setenv("ECOMOPS_PILOT_URL", fp.start())
    monkeypatch.setenv("ECOMOPS_RECONCILE_INTERVAL_SECONDS", "0.3")
    monkeypatch.setenv("ECOMOPS_OUTBOX_BACKOFF_SECONDS", "0.3")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    import ecomops.main as m
    importlib.reload(m)
    return fp, TestClient(m.app)


@pytest.fixture()
def env(db_name, monkeypatch):
    fp, tc = make_env(monkeypatch, db_name)
    with tc as c:
        yield c, fp
    fp.stop()
    config.get_settings.cache_clear()


@pytest.fixture()
def fast_expiry_env(db_name, monkeypatch):
    fp, tc = make_env(monkeypatch, db_name, ECOMOPS_EVIDENCE_DEADLINE_HOURS="0.0004")    # 约 1.4 秒
    with tc as c:
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


def detail(c, no):
    return c.get(f"/api/v1/claims/{no}", headers=H()).json()


def test_auto_reject_reason_is_sent_to_pilot(env):
    c, fp = env
    r = c.post("/internal/v1/claims/submit", json=payload("RJ1", days=20, reason="不想要了", urls=()), headers=INT)
    assert r.json()["status"] == "AUTO_REJECTED"
    claim, body = wait_for(lambda: fp.results[0] if fp.results else None)
    assert claim == "RJ1" and body["result"] == "REJECTED" and body["reason"] and "换退期" in body["reason"]


def test_supervisor_reject_reason_is_sent_to_pilot(env):
    c, fp = env
    c.post("/internal/v1/claims/submit", json=payload("RJ2"), headers=INT)
    c.post("/api/v1/claims/RJ2/confirm", json={"action": "REJECT", "reason": "凭证与描述不符"}, headers=H())
    _, body = wait_for(lambda: fp.results[0] if fp.results else None)
    assert body == {"result": "REJECTED", "reason": "凭证与描述不符"}


def test_expired_evidence_request_is_withdrawn_and_pilot_notified(fast_expiry_env):
    c, fp = fast_expiry_env
    assert c.post("/internal/v1/claims/submit", json=payload("EX1", urls=()), headers=INT).json()["status"] == "NEED_MORE_EVIDENCE"
    wait_for(lambda: detail(c, "EX1")["claim_status"] == "WITHDRAWN")
    claim, body = wait_for(lambda: fp.results[0] if fp.results else None)
    assert claim == "EX1" and body["result"] == "WITHDRAWN" and "超时" in body["reason"]
    assert "超时" in detail(c, "EX1")["route_reason"]


def test_default_deadline_does_not_withdraw_and_other_states_are_untouched(env):
    c, fp = env
    c.post("/internal/v1/claims/submit", json=payload("KEEP1", urls=()), headers=INT)       # 待补材料
    c.post("/internal/v1/claims/submit", json=payload("KEEP2"), headers=INT)                # 待主管复核
    time.sleep(1.5)
    assert detail(c, "KEEP1")["claim_status"] == "NEED_MORE_EVIDENCE"
    assert detail(c, "KEEP2")["claim_status"] == "PENDING_SUPERVISOR_REVIEW" and fp.results == []


def test_evidence_before_deadline_prevents_withdrawal(fast_expiry_env):
    c, fp = fast_expiry_env
    c.post("/internal/v1/claims/submit", json=payload("EX2", urls=()), headers=INT)
    assert c.post("/internal/v1/claims/EX2/evidence", json={"evidence_urls": ["https://x.invalid/a.jpg"]}, headers=INT).status_code == 202
    time.sleep(2.5)                                                                          # 超过 deadline，但已进入主管复核
    assert detail(c, "EX2")["claim_status"] == "PENDING_SUPERVISOR_REVIEW" and fp.results == []


def test_reapplication_after_withdrawal_starts_a_new_round_and_second_withdrawal_still_notifies(env):
    c, fp = env
    c.post("/internal/v1/claims/submit", json=payload("RE1", urls=()), headers=INT)
    assert c.post("/internal/v1/claims/RE1/withdraw", headers=INT).json()["claim_status"] == "WITHDRAWN"
    wait_for(lambda: len(fp.results) == 1)
    # Pilot 里撤回后可以重新申请；同一订单子项生成同一个 claim_no
    again = c.post("/internal/v1/claims/submit", json=payload("RE1", reason="热水器漏水", urls=("https://x.invalid/e.jpg",)), headers=INT)
    assert again.status_code == 202 and again.json()["idempotent_replayed"] is False
    assert again.json()["status"] == "PENDING_SUPERVISOR_REVIEW"
    d = detail(c, "RE1")
    assert d["audit_round"] == 2 and d["approved_refund_cents"] is None and d["approvals"] == []
    assert {t["audit_round"] for t in d["tracks"]} == {1, 2}
    # 第二次撤回的回写不能被第一次的幂等键吞掉
    assert c.post("/internal/v1/claims/RE1/withdraw", headers=INT).json()["claim_status"] == "WITHDRAWN"
    wait_for(lambda: len(fp.results) == 2)
    assert [r[1]["result"] for r in fp.results] == ["WITHDRAWN", "WITHDRAWN"]


def test_resubmitting_a_live_claim_is_still_an_idempotent_replay(env):
    c, _ = env
    c.post("/internal/v1/claims/submit", json=payload("RE2"), headers=INT)
    again = c.post("/internal/v1/claims/submit", json=payload("RE2"), headers=INT)
    assert again.json()["idempotent_replayed"] is True and detail(c, "RE2")["audit_round"] == 1
