"""claim_audit 鉴权 / RBAC / 租户隔离验收。"""
from __future__ import annotations

import uuid
from typing import Any, Callable

import httpx

from tests.stack import INTERNAL_TOKEN


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _submit_owned(ops_env, make_claim_payload, *, tenant_id: str = "SHOP_SELF_01") -> str:
    claim_no = _uid("CLM_SEC")
    payload = make_claim_payload(
        claim_no=claim_no,
        tenant_id=tenant_id,
        days_since_delivery=5,
        buyer_reason_text="漏水",
        evidence_urls=["https://storage.example.invalid/e1.jpg"],
        item_payable_cents=112000,
        max_refundable_cents=112000,
    )
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, f"security setup submit expected 202, got {r.status_code}: {r.text[:400]}"
    assert r.json().get("status") == "PENDING_SUPERVISOR_REVIEW", (
        f"security setup: expected PENDING_SUPERVISOR_REVIEW, got {r.json()!r}"
    )
    return claim_no


def test_operator_confirm_forbidden(
    ops_env, make_claim_payload: Callable[..., dict[str, Any]], claim_err_code: Callable
):
    claim_no = _submit_owned(ops_env, make_claim_payload)
    op = ops_env.ops_token("operator", "op-sec-1")
    r = ops_env.confirm(op, claim_no, {"action": "APPROVE"})
    assert r.status_code == 403, (
        f"operator confirm expected 403, got {r.status_code}: {r.text[:400]}"
    )
    assert claim_err_code(r) == "FORBIDDEN_ROLE", (
        f"operator confirm: code expected FORBIDDEN_ROLE, got {claim_err_code(r)!r}"
    )


def test_missing_token_unauthorized(ops_env, make_claim_payload: Callable[..., dict[str, Any]], claim_err_code: Callable):
    claim_no = _submit_owned(ops_env, make_claim_payload)
    r = httpx.get(f"{ops_env.url}/api/v1/claims/{claim_no}", timeout=30.0)
    assert r.status_code == 401, f"missing token GET expected 401, got {r.status_code}: {r.text[:400]}"
    assert claim_err_code(r) == "UNAUTHORIZED", (
        f"missing token: code expected UNAUTHORIZED, got {claim_err_code(r)!r}"
    )
    r2 = httpx.post(
        f"{ops_env.url}/api/v1/claims/{claim_no}/confirm",
        json={"action": "APPROVE"},
        timeout=30.0,
    )
    assert r2.status_code == 401, (
        f"missing token confirm expected 401, got {r2.status_code}: {r2.text[:400]}"
    )
    assert claim_err_code(r2) == "UNAUTHORIZED", (
        f"missing token confirm: code expected UNAUTHORIZED, got {claim_err_code(r2)!r}"
    )


def test_bad_internal_token_on_submit(
    ops_env, make_claim_payload: Callable[..., dict[str, Any]], claim_err_code: Callable
):
    payload = make_claim_payload(
        claim_no=_uid("CLM_BADINT"),
        days_since_delivery=5,
        buyer_reason_text="漏水",
        evidence_urls=["https://storage.example.invalid/e1.jpg"],
    )
    r = httpx.post(
        f"{ops_env.url}/internal/v1/claims/submit",
        headers={"X-Internal-Token": "wrong-internal-token"},
        json=payload,
        timeout=30.0,
    )
    assert r.status_code == 401, (
        f"bad internal token expected 401, got {r.status_code}: {r.text[:400]}"
    )
    assert claim_err_code(r) == "INTERNAL_AUTH_FAILED", (
        f"bad internal token: code expected INTERNAL_AUTH_FAILED, got {claim_err_code(r)!r}"
    )
    # 正确令牌仍可用（对照）
    ok = httpx.post(
        f"{ops_env.url}/internal/v1/claims/submit",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        json=payload,
        timeout=60.0,
    )
    assert ok.status_code == 202, (
        f"valid internal token submit expected 202, got {ok.status_code}: {ok.text[:400]}"
    )


def test_other_tenant_supervisor_cannot_see_or_confirm(
    ops_env, make_claim_payload: Callable[..., dict[str, Any]], claim_err_code: Callable
):
    # 内部提交按 payload.tenant_id 归属，与 JWT 无关
    claim_no = _submit_owned(ops_env, make_claim_payload, tenant_id="SHOP_SELF_01")

    other = ops_env.ops_token("supervisor", "sup-other", tenant_id="OTHER")
    g = ops_env.get_claim(other, claim_no)
    assert g.status_code == 404, (
        f"OTHER tenant GET expected 404, got {g.status_code}: {g.text[:400]}"
    )
    assert claim_err_code(g) == "NOT_FOUND", (
        f"OTHER tenant GET: code expected NOT_FOUND, got {claim_err_code(g)!r}"
    )

    c = ops_env.confirm(other, claim_no, {"action": "APPROVE"})
    assert c.status_code == 404, (
        f"OTHER tenant confirm expected 404, got {c.status_code}: {c.text[:400]}"
    )
    assert claim_err_code(c) == "NOT_FOUND", (
        f"OTHER tenant confirm: code expected NOT_FOUND, got {claim_err_code(c)!r}"
    )

    # 本租户主管可见
    own = ops_env.ops_token("supervisor", "sup-home", tenant_id="SHOP_SELF_01")
    ok = ops_env.get_claim(own, claim_no)
    assert ok.status_code == 200, (
        f"home tenant GET expected 200, got {ok.status_code}: {ok.text[:400]}"
    )
    assert ok.json().get("claim_no") == claim_no


def test_cs_agent_list_forbidden(ops_env, claim_err_code: Callable):
    agent = ops_env.ops_token("cs_agent", "cs-1")
    r = httpx.get(f"{ops_env.url}/api/v1/claims", headers=agent, timeout=30.0)
    assert r.status_code == 403, (
        f"cs_agent list expected 403, got {r.status_code}: {r.text[:400]}"
    )
    assert claim_err_code(r) == "FORBIDDEN_ROLE", (
        f"cs_agent list: code expected FORBIDDEN_ROLE, got {claim_err_code(r)!r}"
    )


def test_payload_tenant_drives_visibility_not_caller_token(
    ops_env, make_claim_payload: Callable[..., dict[str, Any]], claim_err_code: Callable
):
    """内部提交无 JWT；工单归属 payload.tenant_id。"""
    claim_other = _uid("CLM_TEN_B")
    payload = make_claim_payload(
        claim_no=claim_other,
        tenant_id="OTHER",
        days_since_delivery=5,
        buyer_reason_text="漏水",
        evidence_urls=["https://storage.example.invalid/e1.jpg"],
        item_payable_cents=112000,
        max_refundable_cents=112000,
    )
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, f"submit OTHER tenant claim expected 202, got {r.status_code}: {r.text[:400]}"

    home_sup = ops_env.ops_token("supervisor", "sup-home-2", tenant_id="SHOP_SELF_01")
    miss = ops_env.get_claim(home_sup, claim_other)
    assert miss.status_code == 404, (
        f"SHOP_SELF_01 supervisor must not see OTHER claim: got {miss.status_code}: {miss.text[:400]}"
    )
    assert claim_err_code(miss) == "NOT_FOUND"

    other_sup = ops_env.ops_token("supervisor", "sup-other-2", tenant_id="OTHER")
    hit = ops_env.get_claim(other_sup, claim_other)
    assert hit.status_code == 200, (
        f"OTHER supervisor should see own-tenant claim: got {hit.status_code}: {hit.text[:400]}"
    )
    assert hit.json().get("claim_no") == claim_other
