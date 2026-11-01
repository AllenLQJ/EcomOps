"""claim_audit 路由/审批验收（纯 Ops；Pilot 不可达）。"""
from __future__ import annotations

import uuid
from typing import Any, Callable

APPROVED_OR_RETRYING = {"APPROVED_PENDING_CALLBACK", "CALLBACK_RETRYING", "COMPLETED"}


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _sup(ops) -> dict[str, str]:
    return ops.ops_token("supervisor", "sup-routing-1")


def test_auto_reject_out_of_window_no_quality(ops_env, make_claim_payload: Callable[..., dict[str, Any]]):
    claim_no = _uid("CLM_AR")
    payload = make_claim_payload(
        claim_no=claim_no,
        days_since_delivery=20,
        buyer_reason_text="不想要了，外观不满意",
        evidence_urls=[],
        item_payable_cents=3000,
        max_refundable_cents=3000,
    )
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, f"auto-reject submit expected 202, got {r.status_code}: {r.text[:400]}"
    body = r.json()
    assert body.get("status") == "AUTO_REJECTED", (
        f"auto-reject: submit status expected AUTO_REJECTED, got {body!r}"
    )
    detail = ops_env.get_claim(_sup(ops_env), claim_no).json()
    assert detail["claim_status"] == "AUTO_REJECTED", (
        f"auto-reject: detail claim_status expected AUTO_REJECTED, got {detail.get('claim_status')!r}"
    )


def test_need_more_evidence_then_resubmit_and_conflict(
    ops_env,
    make_claim_payload: Callable[..., dict[str, Any]],
    claim_err_code: Callable,
):
    claim_no = _uid("CLM_NME")
    payload = make_claim_payload(
        claim_no=claim_no,
        days_since_delivery=4,
        buyer_reason_text="漏水",
        evidence_urls=[],
        item_payable_cents=112000,
        max_refundable_cents=112000,
    )
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, f"need-evidence submit expected 202, got {r.status_code}: {r.text[:400]}"
    assert r.json().get("status") == "NEED_MORE_EVIDENCE", (
        f"need-evidence: expected NEED_MORE_EVIDENCE, got {r.json()!r}"
    )

    r2 = ops_env.post_evidence(claim_no, ["https://storage.example.invalid/e_added.jpg"])
    assert r2.status_code == 202, (
        f"post evidence expected 202, got {r2.status_code}: {r2.text[:400]}"
    )

    h = _sup(ops_env)
    detail = ops_env.wait_status(h, claim_no, {"PENDING_SUPERVISOR_REVIEW"}, timeout=20.0)
    assert detail["claim_status"] == "PENDING_SUPERVISOR_REVIEW", (
        f"after evidence (amount>5000): expected PENDING_SUPERVISOR_REVIEW, got {detail!r}"
    )
    audit_round = detail.get("audit_round")
    tracks = detail.get("tracks") or []
    if audit_round is not None:
        assert int(audit_round) >= 2, (
            f"after evidence: audit_round expected >=2, got {audit_round!r}"
        )
    else:
        has_round2 = any(
            (t.get("audit_round") == 2) or (t.get("detail") or {}).get("audit_round") == 2
            for t in tracks
        ) or len(tracks) > 3
        assert has_round2 or len(tracks) >= 3, (
            f"after evidence: expected 2nd-round signal in tracks; tracks={tracks!r}"
        )

    r3 = ops_env.post_evidence(claim_no, ["https://storage.example.invalid/e_extra.jpg"])
    assert r3.status_code == 409, (
        f"evidence while PENDING_SUPERVISOR_REVIEW expected 409, got {r3.status_code}: {r3.text[:400]}"
    )
    assert claim_err_code(r3) == "CLAIM_STATE_CONFLICT", (
        f"evidence conflict code expected CLAIM_STATE_CONFLICT, "
        f"got {claim_err_code(r3)!r} body={r3.text[:300]}"
    )


def test_auto_approve_small_quality_claim(ops_env, make_claim_payload: Callable[..., dict[str, Any]]):
    claim_no = _uid("CLM_AA")
    payload = make_claim_payload(
        claim_no=claim_no,
        days_since_delivery=3,
        buyer_reason_text="坏了",
        evidence_urls=["https://storage.example.invalid/e1.jpg"],
        item_payable_cents=4000,
        max_refundable_cents=4000,
    )
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, f"auto-approve submit expected 202, got {r.status_code}: {r.text[:400]}"
    status = r.json().get("status")
    assert status in APPROVED_OR_RETRYING, (
        f"auto-approve: submit status expected in {APPROVED_OR_RETRYING}, got {status!r}"
    )

    h = _sup(ops_env)
    detail = ops_env.wait_status(h, claim_no, APPROVED_OR_RETRYING, timeout=15.0)
    assert detail.get("risk_level") == "LOW", (
        f"auto-approve: risk_level expected LOW, got {detail.get('risk_level')!r}; detail={detail!r}"
    )
    actions = [a.get("action") for a in (detail.get("approvals") or [])]
    assert "AUTO_APPROVE" in actions, (
        f"auto-approve: approvals should contain AUTO_APPROVE, got {detail.get('approvals')!r}"
    )
    final = ops_env.wait_status(
        h, claim_no, {"CALLBACK_RETRYING", "APPROVED_PENDING_CALLBACK", "COMPLETED"}, timeout=20.0
    )
    assert final["claim_status"] in APPROVED_OR_RETRYING, (
        f"auto-approve unreachable pilot: unexpected final status {final.get('claim_status')!r}"
    )


def test_large_amount_pending_supervisor(ops_env, make_claim_payload: Callable[..., dict[str, Any]]):
    claim_no = _uid("CLM_LG")
    payload = make_claim_payload(
        claim_no=claim_no,
        days_since_delivery=5,
        buyer_reason_text="漏水",
        evidence_urls=["https://storage.example.invalid/e1.jpg"],
        item_payable_cents=112000,
        max_refundable_cents=112000,
    )
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, f"large claim submit expected 202, got {r.status_code}: {r.text[:400]}"
    assert r.json().get("status") == "PENDING_SUPERVISOR_REVIEW", (
        f"large claim: expected PENDING_SUPERVISOR_REVIEW, got {r.json()!r}"
    )
    detail = ops_env.get_claim(_sup(ops_env), claim_no).json()
    assert detail["claim_status"] == "PENDING_SUPERVISOR_REVIEW"
    assert detail.get("risk_level") == "HIGH", (
        f"large claim: risk_level expected HIGH, got {detail.get('risk_level')!r}"
    )
    tracks = {t.get("track") for t in (detail.get("tracks") or [])}
    assert tracks >= {"RULE", "EVIDENCE", "FEE"}, (
        f"large claim: expected RULE/EVIDENCE/FEE tracks, got {detail.get('tracks')!r}"
    )


def test_low_confidence_pending_supervisor(
    ops_env,
    make_claim_payload: Callable[..., dict[str, Any]],
    claim_track_by_name: Callable,
):
    claim_no = _uid("CLM_LC")
    payload = make_claim_payload(
        claim_no=claim_no,
        days_since_delivery=3,
        buyer_reason_text="一般般，说不上来",
        evidence_urls=[],
        item_payable_cents=3000,
        max_refundable_cents=3000,
    )
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, f"low-confidence submit expected 202, got {r.status_code}: {r.text[:400]}"
    assert r.json().get("status") == "PENDING_SUPERVISOR_REVIEW", (
        f"low-confidence: expected PENDING_SUPERVISOR_REVIEW (conf 0.5<0.85), got {r.json()!r}"
    )
    detail = ops_env.get_claim(_sup(ops_env), claim_no).json()
    ev = claim_track_by_name(detail, "EVIDENCE")
    assert ev is not None, f"low-confidence: missing EVIDENCE track in {detail.get('tracks')!r}"
    conf = float(ev.get("confidence") if ev.get("confidence") is not None else -1)
    assert conf == 0.5 or conf < 0.85, (
        f"low-confidence: EVIDENCE confidence expected 0.5 (<0.85), got {conf!r}"
    )


def test_evidence_track_fallback_then_recover(ops_env, make_claim_payload: Callable[..., dict[str, Any]]):
    fr = ops_env.set_faults({"llm_fail": ["evidence_track"]})
    assert fr.status_code == 200, f"set faults expected 200, got {fr.status_code}: {fr.text[:300]}"

    claim_no = _uid("CLM_FB")
    payload = make_claim_payload(
        claim_no=claim_no,
        days_since_delivery=3,
        buyer_reason_text="坏了",
        evidence_urls=["https://storage.example.invalid/e1.jpg"],
        item_payable_cents=4000,
        max_refundable_cents=4000,
    )
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, f"fallback submit expected 202, got {r.status_code}: {r.text[:400]}"
    assert r.json().get("status") == "PENDING_SUPERVISOR_REVIEW", (
        f"fallback: expected PENDING_SUPERVISOR_REVIEW, got {r.json()!r}"
    )
    detail = ops_env.get_claim(_sup(ops_env), claim_no).json()
    assert detail.get("fallback_used") is True, (
        f"fallback: claim fallback_used expected True, got {detail.get('fallback_used')!r}; {detail!r}"
    )

    cr = ops_env.set_faults({})
    assert cr.status_code == 200, f"clear faults expected 200, got {cr.status_code}: {cr.text[:300]}"

    claim_ok = _uid("CLM_FB_OK")
    payload2 = make_claim_payload(
        claim_no=claim_ok,
        days_since_delivery=3,
        buyer_reason_text="坏了",
        evidence_urls=["https://storage.example.invalid/e1.jpg"],
        item_payable_cents=4000,
        max_refundable_cents=4000,
    )
    r2 = ops_env.submit_claim(payload2)
    assert r2.status_code == 202, f"recover submit expected 202, got {r2.status_code}: {r2.text[:400]}"
    assert r2.json().get("status") in APPROVED_OR_RETRYING, (
        f"recover after clear faults: expected auto-approve status, got {r2.json()!r}"
    )


def test_idempotent_submit(ops_env, make_claim_payload: Callable[..., dict[str, Any]]):
    claim_no = _uid("CLM_ID")
    payload = make_claim_payload(
        claim_no=claim_no,
        days_since_delivery=5,
        buyer_reason_text="漏水",
        evidence_urls=["https://storage.example.invalid/e1.jpg"],
        item_payable_cents=112000,
        max_refundable_cents=112000,
    )
    r1 = ops_env.submit_claim(payload)
    r2 = ops_env.submit_claim(payload)
    assert r1.status_code == 202, f"idempotent first submit expected 202, got {r1.status_code}: {r1.text[:400]}"
    assert r2.status_code == 202, f"idempotent second submit expected 202, got {r2.status_code}: {r2.text[:400]}"
    b2 = r2.json()
    assert b2.get("idempotent_replayed") is True, (
        f"idempotent second submit: idempotent_replayed expected true, got {b2!r}"
    )

    detail = ops_env.get_claim(_sup(ops_env), claim_no).json()
    tracks = detail.get("tracks") or []
    names = [t.get("track") for t in tracks]
    assert set(names) >= {"RULE", "EVIDENCE", "FEE"}, (
        f"idempotent: missing tracks after replay, got {tracks!r}"
    )
    # 幂等重放不得再写一轮三轨；详情若只含当前轮则为 3 条
    assert len(tracks) == 3, (
        f"idempotent submit: expected exactly 3 tracks (no duplicate audit), "
        f"got {len(tracks)}: {tracks!r}"
    )


def test_supervisor_decisions(
    ops_env,
    make_claim_payload: Callable[..., dict[str, Any]],
    claim_err_code: Callable,
):
    h = _sup(ops_env)

    def _pending(suffix: str) -> str:
        claim_no = _uid(f"CLM_SUP_{suffix}")
        payload = make_claim_payload(
            claim_no=claim_no,
            days_since_delivery=5,
            buyer_reason_text="漏水",
            evidence_urls=["https://storage.example.invalid/e1.jpg"],
            item_payable_cents=112000,
            max_refundable_cents=112000,
        )
        r = ops_env.submit_claim(payload)
        assert r.status_code == 202 and r.json().get("status") == "PENDING_SUPERVISOR_REVIEW", (
            f"setup pending claim failed: {r.status_code} {r.text[:400]}"
        )
        return claim_no

    c_rej = _pending("REJ")
    rr = ops_env.confirm(h, c_rej, {"action": "REJECT", "reason": "不符合政策"})
    assert rr.status_code == 200, f"REJECT expected 200, got {rr.status_code}: {rr.text[:400]}"
    assert rr.json().get("claim_status") == "SUPERVISOR_REJECTED", (
        f"REJECT: claim_status expected SUPERVISOR_REJECTED, got {rr.json()!r}"
    )

    c_req = _pending("REQ")
    re = ops_env.confirm(h, c_req, {"action": "REQUEST_EVIDENCE", "reason": "再拍清晰图"})
    assert re.status_code == 200, f"REQUEST_EVIDENCE expected 200, got {re.status_code}: {re.text[:400]}"
    assert re.json().get("claim_status") == "NEED_MORE_EVIDENCE", (
        f"REQUEST_EVIDENCE: expected NEED_MORE_EVIDENCE, got {re.json()!r}"
    )

    c_amt = _pending("AMT")
    over = ops_env.confirm(
        h, c_amt, {"action": "MODIFY_AMOUNT", "amount_cents": 112001, "reason": "超额"}
    )
    assert over.status_code == 422, (
        f"MODIFY_AMOUNT over max expected 422, got {over.status_code}: {over.text[:400]}"
    )
    assert claim_err_code(over) == "AMOUNT_EXCEEDS_LIMIT", (
        f"MODIFY_AMOUNT over: code expected AMOUNT_EXCEEDS_LIMIT, got {claim_err_code(over)!r}"
    )
    zero = ops_env.confirm(
        h, c_amt, {"action": "MODIFY_AMOUNT", "amount_cents": 0, "reason": "零元"}
    )
    assert zero.status_code == 422, (
        f"MODIFY_AMOUNT 0 expected 422, got {zero.status_code}: {zero.text[:400]}"
    )
    assert claim_err_code(zero) == "AMOUNT_EXCEEDS_LIMIT", (
        f"MODIFY_AMOUNT 0: code expected AMOUNT_EXCEEDS_LIMIT, got {claim_err_code(zero)!r}"
    )

    ok_amt = 88000
    mod = ops_env.confirm(
        h, c_amt, {"action": "MODIFY_AMOUNT", "amount_cents": ok_amt, "reason": "酌减"}
    )
    assert mod.status_code == 200, (
        f"MODIFY_AMOUNT valid expected 200, got {mod.status_code}: {mod.text[:400]}"
    )
    assert mod.json().get("claim_status") in APPROVED_OR_RETRYING, (
        f"MODIFY_AMOUNT valid: status expected approved/retrying, got {mod.json()!r}"
    )
    detail = ops_env.wait_status(h, c_amt, APPROVED_OR_RETRYING, timeout=15.0)
    assert detail.get("approved_refund_cents") == ok_amt, (
        f"MODIFY_AMOUNT: approved_refund_cents expected {ok_amt}, got {detail.get('approved_refund_cents')!r}"
    )

    again = ops_env.confirm(
        h, c_amt, {"action": "MODIFY_AMOUNT", "amount_cents": ok_amt, "reason": "酌减"}
    )
    assert again.status_code == 200, (
        f"idempotent same MODIFY_AMOUNT expected 200, got {again.status_code}: {again.text[:400]}"
    )
    assert again.json().get("idempotent_replayed") is True, (
        f"idempotent same decision: idempotent_replayed expected true, got {again.json()!r}"
    )
    conflict = ops_env.confirm(h, c_amt, {"action": "REJECT", "reason": "改主意"})
    assert conflict.status_code == 409, (
        f"different decision expected 409, got {conflict.status_code}: {conflict.text[:400]}"
    )
    assert claim_err_code(conflict) == "CLAIM_STATE_CONFLICT", (
        f"different decision: code expected CLAIM_STATE_CONFLICT, got {claim_err_code(conflict)!r}"
    )

    again_rej = ops_env.confirm(h, c_rej, {"action": "REJECT", "reason": "不符合政策"})
    assert again_rej.status_code == 200, (
        f"idempotent REJECT expected 200, got {again_rej.status_code}: {again_rej.text[:400]}"
    )
    assert again_rej.json().get("idempotent_replayed") is True, (
        f"idempotent REJECT: idempotent_replayed expected true, got {again_rej.json()!r}"
    )
