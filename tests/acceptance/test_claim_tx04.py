"""AC-TX-04 完整版：真实 EcomPilot + 故障代理 + SIGKILL 重启 + 并发批准。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import pytest

HEATER_CENTS = 112000
BUYER = "USR_TX04_BUYER"
SUPERVISOR = "SUP_TX04"


@pytest.mark.parametrize("ops_env", ["proxy"], indirect=True)
def test_ac_tx04_restart_concurrent_approve_single_refund(ops_env):
    assert ops_env.pilot is not None, "AC-TX-04 requires real Pilot stack"
    assert ops_env.proxy is not None, "AC-TX-04 requires fault proxy"
    ps = ops_env.pilot

    order = ps.make_fulfilled_order(BUYER, sku_id="SKU_HEATER_003")
    assert order["payable_cents"] == HEATER_CENTS, (
        f"heater payable expected {HEATER_CENTS}, got {order.get('payable_cents')!r}"
    )

    with httpx.Client(base_url=ps.pilot_url, timeout=60.0) as c:
        conf = c.post(
            "/api/v1/aftersales/confirm",
            headers=order["headers"],
            json={
                "order_id": order["order_id"],
                "order_item_id": order["order_item_id"],
                "buyer_reason_text": "漏水打不着火",
                "evidence_urls": ["https://storage.example.invalid/e1.jpg"],
            },
        )
    assert conf.status_code == 202, (
        f"Pilot aftersales/confirm expected 202, got {conf.status_code}: {conf.text[:500]}"
    )
    claim_no = conf.json()["claim_no"]
    assert claim_no, f"confirm response missing claim_no: {conf.json()!r}"

    h = ops_env.ops_token("supervisor", SUPERVISOR)
    detail = ops_env.wait_status(h, claim_no, {"PENDING_SUPERVISOR_REVIEW"}, timeout=30.0)
    assert detail["claim_status"] == "PENDING_SUPERVISOR_REVIEW", (
        f"after Pilot submit: expected PENDING_SUPERVISOR_REVIEW, got {detail!r}"
    )

    # SIGKILL 模拟崩溃重启；检查点应持久
    ops_env.ops.restart(sig_kill=True)
    detail2 = ops_env.get_claim(h, claim_no)
    assert detail2.status_code == 200, (
        f"after SIGKILL restart: GET claim expected 200, got {detail2.status_code}: {detail2.text[:400]}"
    )
    assert detail2.json().get("claim_status") == "PENDING_SUPERVISOR_REVIEW", (
        f"after SIGKILL restart: checkpoint should remain PENDING_SUPERVISOR_REVIEW, "
        f"got {detail2.json()!r}"
    )

    ops_env.proxy_set_mode({"drop_response_first_n": 1, "sleep_seconds": 5})

    results: list[httpx.Response] = []

    def _approve_once() -> httpx.Response:
        # 每线程独立 client，避免共享连接状态
        return httpx.post(
            f"{ops_env.url}/api/v1/claims/{claim_no}/confirm",
            headers=h,
            json={"action": "APPROVE"},
            timeout=90.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_approve_once) for _ in range(2)]
        for fut in as_completed(futs):
            results.append(fut.result())

    assert len(results) == 2, f"expected 2 concurrent confirm responses, got {len(results)}"
    for i, resp in enumerate(results):
        assert resp.status_code == 200, (
            f"concurrent APPROVE[{i}] expected 200, got {resp.status_code}: {resp.text[:400]}"
        )

    bodies = [r.json() for r in results]
    replay_flags = [b.get("idempotent_replayed") for b in bodies]
    fresh = [f for f in replay_flags if f is False]
    replayed = [f for f in replay_flags if f is True]

    detail_after = ops_env.get_claim(h, claim_no).json()
    approvals = detail_after.get("approvals") or []
    # 恰好一条真正生效；允许用 approvals 长度兜底
    if not (len(fresh) == 1 and len(replayed) == 1):
        assert len(approvals) == 1, (
            f"concurrent APPROVE: expected exactly one non-replayed OR approvals==1; "
            f"idempotent_replayed={replay_flags!r}, approvals={approvals!r}, bodies={bodies!r}"
        )
    else:
        assert len(approvals) == 1, (
            f"concurrent APPROVE: approvals length expected 1, got {len(approvals)}: {approvals!r}"
        )

    completed = ops_env.wait_status(h, claim_no, {"COMPLETED"}, timeout=40.0)
    assert completed["claim_status"] == "COMPLETED", (
        f"AC-TX-04: expected COMPLETED within 40s, got {completed!r}"
    )

    rows = ps.query(
        "SELECT refund_execution_id, refunded_cents, claim_no FROM refund_executions WHERE claim_no=%s",
        claim_no,
    )
    assert len(rows) == 1, (
        f"AC-TX-04: refund_executions for {claim_no!r} expected 1 row, got {len(rows)}: {rows!r}"
    )
    assert int(rows[0]["refunded_cents"]) == HEATER_CENTS, (
        f"AC-TX-04: refunded_cents expected {HEATER_CENTS}, got {rows[0].get('refunded_cents')!r}"
    )

    orders = ps.query(
        "SELECT order_status, refunded_cents FROM orders WHERE order_id=%s",
        order["order_id"],
    )
    assert len(orders) == 1, f"order missing after refund: {order['order_id']!r}"
    assert orders[0]["order_status"] == "REFUNDED_FULL", (
        f"AC-TX-04: order_status expected REFUNDED_FULL, got {orders[0]!r}"
    )
    assert int(orders[0]["refunded_cents"]) == HEATER_CENTS, (
        f"AC-TX-04: orders.refunded_cents expected {HEATER_CENTS}, got {orders[0].get('refunded_cents')!r}"
    )

    mode = ops_env.proxy_get_mode()
    assert int(mode.get("dropped", -1)) == 1, (
        f"AC-TX-04: proxy dropped expected 1, got {mode!r}"
    )


@pytest.mark.parametrize("ops_env", ["pilot"], indirect=True)
def test_approve_completes_quickly_without_proxy(ops_env):
    """无故障代理：主管批准后应快速 COMPLETED。"""
    assert ops_env.pilot is not None
    ps = ops_env.pilot

    order = ps.make_fulfilled_order("USR_TX04_FAST", sku_id="SKU_HEATER_003")
    with httpx.Client(base_url=ps.pilot_url, timeout=60.0) as c:
        conf = c.post(
            "/api/v1/aftersales/confirm",
            headers=order["headers"],
            json={
                "order_id": order["order_id"],
                "order_item_id": order["order_item_id"],
                "buyer_reason_text": "漏水打不着火",
                "evidence_urls": ["https://storage.example.invalid/e1.jpg"],
            },
        )
    assert conf.status_code == 202, (
        f"Pilot confirm expected 202, got {conf.status_code}: {conf.text[:400]}"
    )
    claim_no = conf.json()["claim_no"]

    h = ops_env.ops_token("supervisor", "SUP_TX04_FAST")
    ops_env.wait_status(h, claim_no, {"PENDING_SUPERVISOR_REVIEW"}, timeout=30.0)

    appr = ops_env.confirm(h, claim_no, {"action": "APPROVE"})
    assert appr.status_code == 200, f"APPROVE expected 200, got {appr.status_code}: {appr.text[:400]}"

    done = ops_env.wait_status(h, claim_no, {"COMPLETED"}, timeout=20.0)
    assert done["claim_status"] == "COMPLETED", (
        f"happy-path: expected COMPLETED quickly, got {done!r}"
    )

    rows = ps.query(
        "SELECT refunded_cents FROM refund_executions WHERE claim_no=%s",
        claim_no,
    )
    assert len(rows) == 1 and int(rows[0]["refunded_cents"]) == HEATER_CENTS, (
        f"happy-path: expected single refund of {HEATER_CENTS}, got {rows!r}"
    )
