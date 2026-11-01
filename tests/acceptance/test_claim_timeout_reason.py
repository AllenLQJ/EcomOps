"""补材料超时自动撤回 + 自动驳回原因回写买家：真实 EcomPilot + 真实 EcomOps。

时间窗口假设（供统筹排查偶发失败）：
- ECOMOPS_EVIDENCE_DEADLINE_HOURS=0.0006 ≈ 2.16s；Ops 默认 reconcile 每 1s 扫一次，
  故 NEED_MORE_EVIDENCE → WITHDRAWN 预期约在提交后 3–5s 内出现；wait_status 上限 30s。
- 默认 deadline（72h）用例：进入 NEED_MORE_EVIDENCE 后固定 sleep 6s 仍应保持该状态；
  进入 PENDING_SUPERVISOR_REVIEW 后再 sleep 4s 仍不应被撤回。
- 自动驳回后 Pilot 经 CLAIM_RESULT 出箱回写：轮询订单 FULFILLED / claim REJECTED 上限 30s。
"""
from __future__ import annotations

import time

import httpx
import pytest

BUYER_TIMEOUT = "USR_TO_BUYER"
BUYER_DEFAULT = "USR_TO_DEF"
BUYER_REJECT = "USR_TO_REJ"
BUYER_PENDING = "USR_TO_PEND"
SUPERVISOR = "SUP_TO"


def _wait_order_status(ps, order_id: str, expected: str, *, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rows = ps.query("SELECT order_status FROM orders WHERE order_id=%s", order_id)
        last = rows[0]["order_status"] if rows else None
        if last == expected:
            return last
        time.sleep(0.25)
    raise AssertionError(
        f"order {order_id!r}: expected order_status={expected!r} within {timeout}s, last={last!r}"
    )


def _wait_pilot_claim(
    pilot: httpx.Client,
    headers: dict[str, str],
    claim_no: str,
    *,
    status: str | tuple[str, ...],
    timeout: float = 30.0,
) -> dict:
    wanted = (status,) if isinstance(status, str) else tuple(status)
    deadline = time.time() + timeout
    last_http = None
    last_body = None
    while time.time() < deadline:
        r = pilot.get(f"/api/v1/aftersales/{claim_no}", headers=headers)
        last_http = r.status_code
        if r.status_code == 200:
            last_body = r.json()
            if last_body.get("claim_status") in wanted:
                return last_body
        time.sleep(0.25)
    raise AssertionError(
        f"GET aftersales/{claim_no}: expected claim_status={status!r} within {timeout}s; "
        f"last_http={last_http}, last_body={last_body!r}"
    )


class TestEvidenceDeadlineTimeout:
    """短 deadline：买家超时未补材料 → Ops WITHDRAWN → Pilot 删申请、订单回 FULFILLED、可再申请。"""

    @pytest.fixture
    def short_deadline(self, monkeypatch):
        # ≈2.16s；必须在 ops 子进程启动前写入环境（由 env 夹具依赖链保证）
        monkeypatch.setenv("ECOMOPS_EVIDENCE_DEADLINE_HOURS", "0.0006")

    @pytest.fixture
    def env(self, short_deadline, ops_env):
        return ops_env

    @pytest.mark.parametrize("ops_env", ["pilot"], indirect=True)
    def test_buyer_timeout_without_evidence_auto_withdraws(self, env):
        ops_env = env
        ps = ops_env.pilot
        assert ps is not None, "timeout withdraw requires real Pilot"
        order = ps.make_fulfilled_order(BUYER_TIMEOUT)
        with httpx.Client(base_url=ps.pilot_url, timeout=60.0) as pilot:
            conf = pilot.post(
                "/api/v1/aftersales/confirm",
                headers=order["headers"],
                json={
                    "order_id": order["order_id"],
                    "order_item_id": order["order_item_id"],
                    "buyer_reason_text": "热水器漏水打不着火",
                    "evidence_urls": [],
                },
            )
            assert conf.status_code == 202, (
                f"confirm (no evidence) expected 202, got {conf.status_code}: {conf.text[:400]}"
            )
            claim_no = conf.json()["claim_no"]
            assert claim_no, f"confirm missing claim_no: {conf.json()!r}"

            sup = ops_env.ops_token("supervisor", SUPERVISOR)
            ops_env.wait_status(sup, claim_no, {"NEED_MORE_EVIDENCE"}, timeout=30.0)
            withdrawn = ops_env.wait_status(sup, claim_no, {"WITHDRAWN"}, timeout=30.0)
            assert withdrawn["claim_status"] == "WITHDRAWN", (
                f"after evidence deadline: Ops claim_status expected WITHDRAWN, got {withdrawn!r}"
            )

            _wait_order_status(ps, order["order_id"], "FULFILLED", timeout=30.0)
            gone = pilot.get(f"/api/v1/aftersales/{claim_no}", headers=order["headers"])
            assert gone.status_code == 404, (
                f"after WITHDRAWN writeback: GET aftersales/{claim_no} expected 404 "
                f"(claim deleted), got {gone.status_code}: {gone.text[:400]}"
            )

            again = pilot.post(
                "/api/v1/aftersales/confirm",
                headers=order["headers"],
                json={
                    "order_id": order["order_id"],
                    "order_item_id": order["order_item_id"],
                    "buyer_reason_text": "热水器漏水打不着火",
                    "evidence_urls": ["https://e.test/leak.jpg"],
                },
            )
            assert again.status_code == 202, (
                f"re-apply after timeout withdraw expected 202, "
                f"got {again.status_code}: {again.text[:400]}"
            )


class TestEvidenceDeadlineDefault:
    """默认 72h deadline：短等不撤回；补证后进主管复核，仍不被撤回。不设置 ECOMOPS_EVIDENCE_DEADLINE_HOURS。"""

    @pytest.mark.parametrize("ops_env", ["pilot"], indirect=True)
    def test_need_evidence_not_withdrawn_before_deadline_then_evidence_to_supervisor(self, ops_env):
        ps = ops_env.pilot
        assert ps is not None, "default deadline case requires real Pilot"
        order = ps.make_fulfilled_order(BUYER_DEFAULT)
        with httpx.Client(base_url=ps.pilot_url, timeout=60.0) as pilot:
            conf = pilot.post(
                "/api/v1/aftersales/confirm",
                headers=order["headers"],
                json={
                    "order_id": order["order_id"],
                    "order_item_id": order["order_item_id"],
                    "buyer_reason_text": "热水器漏水打不着火",
                    "evidence_urls": [],
                },
            )
            assert conf.status_code == 202, (
                f"confirm (no evidence) expected 202, got {conf.status_code}: {conf.text[:400]}"
            )
            claim_no = conf.json()["claim_no"]

            sup = ops_env.ops_token("supervisor", f"{SUPERVISOR}_DEF")
            ops_env.wait_status(sup, claim_no, {"NEED_MORE_EVIDENCE"}, timeout=30.0)

            time.sleep(6.0)
            still = ops_env.get_claim(sup, claim_no)
            assert still.status_code == 200, (
                f"after 6s under default deadline: GET claim expected 200, "
                f"got {still.status_code}: {still.text[:400]}"
            )
            assert still.json().get("claim_status") == "NEED_MORE_EVIDENCE", (
                f"after 6s under default deadline: expected still NEED_MORE_EVIDENCE "
                f"(not withdrawn), got {still.json()!r}"
            )

            ev = pilot.post(
                f"/api/v1/aftersales/{claim_no}/evidence",
                headers=order["headers"],
                json={"evidence_urls": ["https://e.test/leak.jpg"]},
            )
            assert ev.status_code == 202, (
                f"post evidence expected 202, got {ev.status_code}: {ev.text[:400]}"
            )
            review = ops_env.wait_status(
                sup, claim_no, {"PENDING_SUPERVISOR_REVIEW"}, timeout=30.0
            )
            assert review["claim_status"] == "PENDING_SUPERVISOR_REVIEW", (
                f"after evidence (amount>auto-approve): expected PENDING_SUPERVISOR_REVIEW, "
                f"got {review!r}"
            )

            time.sleep(4.0)
            still_review = ops_env.get_claim(sup, claim_no)
            assert still_review.status_code == 200, still_review.text[:400]
            assert still_review.json().get("claim_status") == "PENDING_SUPERVISOR_REVIEW", (
                f"after 4s in PENDING_SUPERVISOR_REVIEW: expected not withdrawn, "
                f"got {still_review.json()!r}"
            )


class TestAutoRejectResultReason:
    """超 15 天换退期且无质量故障证据 → AUTO_REJECTED；买家可见非空 result_reason，审核中为 null。"""

    @pytest.mark.parametrize("ops_env", ["pilot"], indirect=True)
    def test_auto_reject_notifies_buyer_with_result_reason(self, ops_env):
        ps = ops_env.pilot
        assert ps is not None, "auto-reject reason requires real Pilot"
        sup = ops_env.ops_token("supervisor", f"{SUPERVISOR}_REJ")

        # 对照：仍在审核中的申请，result_reason 应为 null
        pending_order = ps.make_fulfilled_order(BUYER_PENDING)
        with httpx.Client(base_url=ps.pilot_url, timeout=60.0) as pilot:
            pending_conf = pilot.post(
                "/api/v1/aftersales/confirm",
                headers=pending_order["headers"],
                json={
                    "order_id": pending_order["order_id"],
                    "order_item_id": pending_order["order_item_id"],
                    "buyer_reason_text": "热水器漏水打不着火",
                    "evidence_urls": ["https://e.test/leak.jpg"],
                },
            )
            assert pending_conf.status_code == 202, (
                f"pending confirm expected 202, got {pending_conf.status_code}: "
                f"{pending_conf.text[:400]}"
            )
            pending_no = pending_conf.json()["claim_no"]
            ops_env.wait_status(
                sup, pending_no, {"PENDING_SUPERVISOR_REVIEW"}, timeout=30.0
            )
            pending_get = pilot.get(
                f"/api/v1/aftersales/{pending_no}", headers=pending_order["headers"]
            )
            assert pending_get.status_code == 200, (
                f"pending GET expected 200, got {pending_get.status_code}: "
                f"{pending_get.text[:400]}"
            )
            pending_body = pending_get.json()
            assert pending_body.get("result_reason") is None, (
                f"in-review claim result_reason expected null, got {pending_body!r}"
            )

            # 主路径：签收 >15 天 + 非质量原因 + 无证据 → 自动驳回
            order = ps.make_fulfilled_order(BUYER_REJECT)
            with ps.mysql() as conn, conn.cursor() as cur:
                n = cur.execute(
                    "UPDATE orders SET fulfilled_at = NOW() - INTERVAL 20 DAY "
                    "WHERE order_id=%s",
                    (order["order_id"],),
                )
            assert n == 1, (
                f"fulfilled_at backdate: expected 1 row updated for {order['order_id']!r}, got {n}"
            )

            conf = pilot.post(
                "/api/v1/aftersales/confirm",
                headers=order["headers"],
                json={
                    "order_id": order["order_id"],
                    "order_item_id": order["order_item_id"],
                    "buyer_reason_text": "不想要了",
                    "evidence_urls": [],
                },
            )
            assert conf.status_code == 202, (
                f"out-of-window confirm expected 202, got {conf.status_code}: {conf.text[:400]}"
            )
            claim_no = conf.json()["claim_no"]
            ops_env.wait_status(sup, claim_no, {"AUTO_REJECTED"}, timeout=30.0)

            _wait_order_status(ps, order["order_id"], "FULFILLED", timeout=30.0)
            rejected = _wait_pilot_claim(
                pilot, order["headers"], claim_no, status=("REJECTED", "AUTO_REJECTED"), timeout=30.0
            )
            reason = rejected.get("result_reason")
            assert isinstance(reason, str) and reason.strip(), (
                f"AUTO_REJECTED writeback: result_reason must be non-empty str, "
                f"got {reason!r} in {rejected!r}"
            )
            # 进度接口返回审核端的细分状态（AUTO_REJECTED / SUPERVISOR_REJECTED）；本地兜底视图才是 REJECTED
            assert rejected.get("claim_status") in ("REJECTED", "AUTO_REJECTED"), (
                f"buyer claim_status expected a rejected status, got {rejected!r}"
            )

            again = pilot.post(
                "/api/v1/aftersales/confirm",
                headers=order["headers"],
                json={
                    "order_id": order["order_id"],
                    "order_item_id": order["order_item_id"],
                    "buyer_reason_text": "不想要了",
                    "evidence_urls": [],
                },
            )
            # 驳回是终态（撤回才允许重新申请，见决策记录第 7 条）：同一订单子项再次申请 -> 409
            assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_ORDER_STATE", (
                f"re-apply after AUTO_REJECTED expected 409 INVALID_ORDER_STATE, "
                f"got {again.status_code}: {again.text[:400]}"
            )
