"""买家补证据入口：真实 EcomPilot + 真实 EcomOps，缺凭证 -> 买家补传 -> 重新审核 -> 主管批准 -> 只退一次。"""
from __future__ import annotations

import httpx
import pytest

BUYER = "USR_EV_BUYER"
STRANGER = "USR_EV_STRANGER"
SUPERVISOR = "SUP_EV"


@pytest.mark.parametrize("ops_env", ["pilot"], indirect=True)
def test_buyer_supplies_evidence_then_refund_once(ops_env):
    ps = ops_env.pilot
    order = ps.make_fulfilled_order(BUYER)
    with httpx.Client(base_url=ps.pilot_url, timeout=60.0) as pilot:
        conf = pilot.post("/api/v1/aftersales/confirm", headers=order["headers"], json={
            "order_id": order["order_id"], "order_item_id": order["order_item_id"],
            "buyer_reason_text": "热水器漏水打不着火", "evidence_urls": []})
        assert conf.status_code == 202, conf.text
        claim_no = conf.json()["claim_no"]

        sup = ops_env.ops_token("supervisor", SUPERVISOR)
        ops_env.wait_status(sup, claim_no, {"NEED_MORE_EVIDENCE"}, timeout=30.0)

        status = pilot.get(f"/api/v1/aftersales/{claim_no}", headers=order["headers"])
        assert status.status_code == 200, status.text
        assert status.json()["claim_status"] == "NEED_MORE_EVIDENCE" and status.json()["needs_evidence"] is True
        assert status.json()["ops_available"] is True

        # 别人不能替我补，也看不到我的申请
        stranger = ps.dev_token(STRANGER)
        assert pilot.post(f"/api/v1/aftersales/{claim_no}/evidence", headers=stranger,
                          json={"evidence_urls": ["https://e.test/x.jpg"]}).status_code == 404
        assert pilot.get(f"/api/v1/aftersales/{claim_no}", headers=stranger).status_code == 404

        ok = pilot.post(f"/api/v1/aftersales/{claim_no}/evidence", headers=order["headers"],
                        json={"evidence_urls": ["https://e.test/leak.jpg"]})
        assert ok.status_code == 202, ok.text

        # 凭证齐了 -> 重新三轨审核 -> 金额超自动通过上限 -> 主管复核
        ops_env.wait_status(sup, claim_no, {"PENDING_SUPERVISOR_REVIEW"}, timeout=30.0)

        # 已不在待补材料状态：重复提交不能再触发一轮审核
        again = pilot.post(f"/api/v1/aftersales/{claim_no}/evidence", headers=order["headers"],
                           json={"evidence_urls": ["https://e.test/leak2.jpg"]})
        assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_ORDER_STATE"

    approve = ops_env.confirm(sup, claim_no, {"action": "APPROVE"})
    assert approve.status_code == 200, approve.text
    done = ops_env.wait_status(sup, claim_no, {"COMPLETED"}, timeout=60.0)
    assert done["claim_status"] == "COMPLETED"
    rows = ps.query("SELECT refunded_cents FROM refund_executions WHERE claim_no=%s", claim_no)
    assert len(rows) == 1 and rows[0]["refunded_cents"] == order["payable_cents"]
    assert ps.query("SELECT order_status FROM orders WHERE order_id=%s", order["order_id"])[0]["order_status"] == "REFUNDED_FULL"
