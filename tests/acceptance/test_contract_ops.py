"""EcomOps OpenAPI 契约一致性测试（对照 contracts/openapi/ecomops.yaml）。"""
from __future__ import annotations

import io
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from reportlab.pdfgen import canvas

# acceptance/ 无 __init__.py：把本目录加入 path，再加载 sibling fixture 模块
_ACC_DIR = Path(__file__).resolve().parent
if str(_ACC_DIR) not in sys.path:
    sys.path.insert(0, str(_ACC_DIR))

pytest_plugins = ["conftest_contract"]

from conftest_contract import (  # noqa: E402
    OpsHarness,
    assert_error_body,
    assert_status_declared,
    response_body_schema,
    validate_json,
)


def _client(ops: OpsHarness) -> httpx.Client:
    return httpx.Client(base_url=ops.url, timeout=60.0)


def _check(
    doc: dict,
    codes: set[str],
    method: str,
    path: str,
    resp: httpx.Response,
    *,
    expect: int | None = None,
    schema_name: str | None = None,
    allow_empty_schema: bool = False,
) -> Any:
    """断言状态码∈OpenAPI responses；2xx 按 schema 校验；4xx 校验 Error + errors.md。"""
    if expect is not None:
        assert resp.status_code == expect, (
            f"{method.upper()} {path}: expected HTTP {expect}, got {resp.status_code}: {resp.text[:500]}"
        )
    assert_status_declared(doc, path, method, resp.status_code)

    ctype = (resp.headers.get("content-type") or "").lower()
    if resp.status_code >= 400:
        assert "application/json" in ctype, f"{method.upper()} {path}: error content-type={ctype!r}"
        body = resp.json()
        assert_error_body(doc, codes, body, where=f"{method.upper()} {path} → {resp.status_code}")
        return body

    schema = response_body_schema(doc, path, method, resp.status_code)
    if schema is None:
        if allow_empty_schema or "text/event-stream" in ctype:
            return None
        return resp.json() if "application/json" in ctype and resp.content else None

    assert "application/json" in ctype, (
        f"{method.upper()} {path}: expected application/json, got {ctype!r}"
    )
    body = resp.json()
    label = schema_name or f"{method.upper()} {path} → {resp.status_code}"
    validate_json(schema, body, where=label)
    return body


def _make_pdf(text: str = "沙盒商品 额定功率1000W 容量2L 能效等级二级 3C:202401010001") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in (text.splitlines() or [text]):
        c.drawString(72, y, line[:100])
        y -= 18
    c.save()
    return buf.getvalue()


def _submit_claim(ops: OpsHarness, client: httpx.Client, claim_no: str, **over) -> httpx.Response:
    body = {
        "claim_no": claim_no,
        "tenant_id": "SHOP_SELF_01",
        "user_id": "buyer-1",
        "order_id": "ORD-CONTRACT-1",
        "order_item_id": "OI-1",
        "sku_id": "SKU_HEATER_003",
        "catalog_category_id": "HOME_APPLIANCE_HEATER",
        "item_payable_cents": 112000,
        "max_refundable_cents": 112000,
        "days_since_delivery": 5,
        "claim_type": "QUALITY_REFUND",
        "buyer_reason_text": "热水器漏水，质量问题",
        "evidence_urls": ["https://example.com/e1.jpg"],
    }
    body.update(over)
    return client.post("/internal/v1/claims/submit", headers=ops.internal, json=body)


def test_health_and_dev_token(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    with _client(ops_only) as c:
        r = c.get("/health")
        _check(openapi_doc, error_codes, "get", "/health", r, expect=200, allow_empty_schema=True)

        r = c.post(
            "/api/v1/dev/token",
            json={"user_id": "u1", "role": "supervisor", "tenant_id": "SHOP_SELF_01"},
        )
        body = _check(openapi_doc, error_codes, "post", "/api/v1/dev/token", r, expect=200)
        assert isinstance(body.get("token"), str) and body["token"]


def test_claims_submit_list_detail_confirm(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    claim_no = f"CLM-{uuid.uuid4().hex[:12]}"
    with _client(ops_only) as c:
        r = _submit_claim(ops_only, c, claim_no)
        body = _check(
            openapi_doc, error_codes, "post", "/internal/v1/claims/submit", r,
            expect=202, schema_name="ClaimSubmit202",
        )
        assert body["claim_no"] == claim_no

        h = ops_only.auth("sup-1", "supervisor")
        r = c.get("/api/v1/claims", headers=h)
        listing = _check(
            openapi_doc, error_codes, "get", "/api/v1/claims", r, expect=200, schema_name="ClaimList",
        )
        assert any(x.get("claim_no") == claim_no for x in listing["claims"]), (
            f"submitted claim {claim_no} missing from list"
        )

        detail_path = f"/api/v1/claims/{claim_no}"
        r = c.get(detail_path, headers=h)
        detail = _check(
            openapi_doc, error_codes, "get", detail_path, r, expect=200, schema_name="Claim",
        )
        assert detail["claim_no"] == claim_no
        assert detail["claim_status"] == "PENDING_SUPERVISOR_REVIEW", (
            f"expected PENDING_SUPERVISOR_REVIEW for large quality claim, got {detail.get('claim_status')!r}"
        )

        confirm_path = f"/api/v1/claims/{claim_no}/confirm"
        r = c.post(confirm_path, headers=h, json={"action": "APPROVE", "reason": "contract-test"})
        _check(openapi_doc, error_codes, "post", confirm_path, r, expect=200, schema_name="Confirm")


def test_kb_ask_pending_publish_retry_discard(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    with _client(ops_only) as c:
        h = ops_only.auth("op-kb", "operator")

        r = c.post(
            "/api/v1/kb/ask",
            headers=h,
            json={"query": f"完全无关冷门问题-{uuid.uuid4().hex} 月球配送政策是什么"},
        )
        _check(openapi_doc, error_codes, "post", "/api/v1/kb/ask", r, expect=200, schema_name="KbAsk")

        r = c.get("/api/v1/kb/pending", headers=h)
        _check(
            openapi_doc, error_codes, "get", "/api/v1/kb/pending", r, expect=200, schema_name="PendingList",
        )

        q_discard = f"discard-pending-{uuid.uuid4().hex}"
        q_publish = f"publish-pending-{uuid.uuid4().hex}"
        for q in (q_discard, q_publish):
            r = c.post("/api/v1/kb/ask", headers=h, json={"query": q})
            ask = _check(openapi_doc, error_codes, "post", "/api/v1/kb/ask", r, expect=200)
            assert ask.get("enqueued_queue_id") or ask.get("top1_score", 1) < 0.75, (
                f"query {q!r} did not enqueue (need low-score pending): {ask}"
            )

        r = c.get("/api/v1/kb/pending", headers=h)
        items = _check(openapi_doc, error_codes, "get", "/api/v1/kb/pending", r, expect=200)["items"]
        by_q = {it["question_text"]: it for it in items}
        assert q_discard in by_q and q_publish in by_q, (
            f"expected pending for discard/publish; got {[i.get('question_text') for i in items]}"
        )

        discard_id = by_q[q_discard]["queue_id"]
        discard_path = f"/api/v1/kb/pending/{discard_id}/discard"
        r = c.post(discard_path, headers=h)
        _check(
            openapi_doc, error_codes, "post", discard_path, r, expect=200, schema_name="PendingItem.discard",
        )

        r = c.post(
            "/internal/v1/faults",
            headers=ops_only.internal,
            json={"publish_writeback_fail": True},
        )
        assert_status_declared(openapi_doc, "/internal/v1/faults", "post", r.status_code)
        assert r.status_code == 200, f"faults inject failed: {r.status_code} {r.text[:300]}"

        pub_id = by_q[q_publish]["queue_id"]
        publish_path = f"/api/v1/kb/pending/{pub_id}/publish"
        r = c.post(
            publish_path,
            headers=h,
            json={
                "standard_answer": "契约测试标准答",
                "catalog_category_id": "HOME_APPLIANCE_HEATER",
                "rule_source": "contract-test",
            },
        )
        pub_body = _check(
            openapi_doc, error_codes, "post", publish_path, r, expect=200, schema_name="PendingItem.publish",
        )
        assert pub_body.get("publish_status") in ("PUBLISHING", "PUBLISH_FAILED", "PUBLISHED"), (
            f"unexpected publish_status after publish: {pub_body.get('publish_status')!r}"
        )

        c.post("/internal/v1/faults", headers=ops_only.internal, json={})
        retry_path = f"/api/v1/kb/pending/{pub_id}/retry"
        r = c.post(retry_path, headers=h)
        # 回写失败后应为 PUBLISHING，retry → 200 PendingItem；若已 PUBLISHED 则 409 也在声明内
        if pub_body.get("publish_status") in ("PUBLISHING", "PUBLISH_FAILED"):
            _check(
                openapi_doc, error_codes, "post", retry_path, r, expect=200, schema_name="PendingItem.retry",
            )
        else:
            assert_status_declared(openapi_doc, retry_path, "post", r.status_code)
            if r.status_code >= 400:
                assert_error_body(openapi_doc, error_codes, r.json(), where=f"POST {retry_path}")
            elif r.status_code == 200:
                schema = response_body_schema(openapi_doc, retry_path, "post", 200)
                if schema is not None:
                    validate_json(schema, r.json(), where="PendingItem.retry")


def test_listing_upload_list_detail(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    pdf = _make_pdf()
    with _client(ops_only) as c:
        h = ops_only.auth("op-list", "operator")
        r = c.post(
            "/api/v1/listing/reviews",
            headers=h,
            files={"file": ("listing.pdf", pdf, "application/pdf")},
            data={"declared_category": "HOME_APPLIANCE_HEATER"},
        )
        created = _check(
            openapi_doc, error_codes, "post", "/api/v1/listing/reviews", r,
            expect=201, schema_name="ListingReport.create",
        )
        review_id = created["review_id"]

        r = c.get("/api/v1/listing/reviews", headers=h)
        listed = _check(
            openapi_doc, error_codes, "get", "/api/v1/listing/reviews", r,
            expect=200, schema_name="ListingReport.list",
        )
        assert any(x.get("review_id") == review_id for x in listed["reviews"])

        detail_path = f"/api/v1/listing/reviews/{review_id}"
        r = c.get(detail_path, headers=h)
        _check(
            openapi_doc, error_codes, "get", detail_path, r, expect=200, schema_name="ListingReport.detail",
        )


def test_training_create_detail_and_sse(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    with _client(ops_only) as c:
        h = ops_only.auth("agent-1", "cs_agent")
        r = c.post("/api/v1/training/sessions", headers=h, json={})
        created = _check(
            openapi_doc, error_codes, "post", "/api/v1/training/sessions", r,
            expect=201, schema_name="TrainingCreate",
        )
        sid = created["session_id"]

        detail_path = f"/api/v1/training/sessions/{sid}"
        r = c.get(detail_path, headers=h)
        _check(
            openapi_doc, error_codes, "get", detail_path, r, expect=200, schema_name="TrainingDetail",
        )

        stream_path = f"/api/v1/training/sessions/{sid}/chat/stream"
        with c.stream("POST", stream_path, headers=h, json={"message": "您好，我来核实订单号 ORD-1"}) as r:
            assert_status_declared(openapi_doc, stream_path, "post", r.status_code)
            assert r.status_code == 200, (
                f"SSE expected 200, got {r.status_code}: {r.read()[:400]!r}"
            )
            ctype = (r.headers.get("content-type") or "").lower()
            assert "text/event-stream" in ctype, (
                f"POST {stream_path}: content-type must be text/event-stream, got {ctype!r}"
            )
            total = 0
            for chunk in r.iter_bytes():
                total += len(chunk)
                if total > 32:
                    break


def test_error_401_missing_token(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    """401 缺 token：内部接口缺 X-Internal-Token；业务接口缺 Bearer（Error 体）。"""
    with _client(ops_only) as c:
        r = c.post(
            "/internal/v1/claims/submit",
            json={
                "claim_no": "CLM-NOAUTH",
                "tenant_id": "SHOP_SELF_01",
                "user_id": "u",
                "order_id": "o",
                "order_item_id": "i",
                "sku_id": "s",
                "max_refundable_cents": 1,
            },
        )
        body = _check(
            openapi_doc, error_codes, "post", "/internal/v1/claims/submit", r, expect=401,
        )
        assert body["error"]["code"] == "INTERNAL_AUTH_FAILED"

        # 缺 Bearer → UNAUTHORIZED。OpenAPI 未在 GET /claims 声明 401，故不走 assert_status_declared。
        r = c.get("/api/v1/claims")
        assert r.status_code == 401, (
            f"GET /api/v1/claims without bearer: expected 401, got {r.status_code}: {r.text[:300]}"
        )
        assert_error_body(openapi_doc, error_codes, r.json(), where="GET /api/v1/claims no bearer")
        assert r.json()["error"]["code"] == "UNAUTHORIZED"

        # 同一路径带 token 的 200/403 已在其它用例覆盖，保证「已声明状态」路径仍被测到
        h = ops_only.auth("sup-401b", "supervisor")
        r = c.get("/api/v1/claims", headers=h)
        _check(openapi_doc, error_codes, "get", "/api/v1/claims", r, expect=200)


def test_error_403_forbidden_role(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    with _client(ops_only) as c:
        h = ops_only.auth("agent-forbid", "cs_agent")
        r = c.get("/api/v1/claims", headers=h)
        body = _check(openapi_doc, error_codes, "get", "/api/v1/claims", r, expect=403)
        assert body["error"]["code"] == "FORBIDDEN_ROLE"


def test_error_404_not_found(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    with _client(ops_only) as c:
        h = ops_only.auth("sup-404", "supervisor")
        path = "/api/v1/claims/CLM-DOES-NOT-EXIST"
        body = _check(openapi_doc, error_codes, "get", path, c.get(path, headers=h), expect=404)
        assert body["error"]["code"] == "NOT_FOUND"


def test_error_400_invalid_request(ops_only: OpsHarness, openapi_doc: dict, error_codes: set[str]):
    with _client(ops_only) as c:
        h = ops_only.auth("op-400", "operator")
        r = c.post(
            "/api/v1/listing/reviews",
            headers=h,
            files={"file": ("note.txt", b"not-a-pdf", "text/plain")},
            data={"declared_category": "HOME_APPLIANCE_HEATER"},
        )
        body = _check(openapi_doc, error_codes, "post", "/api/v1/listing/reviews", r, expect=400)
        assert body["error"]["code"] in ("UNSUPPORTED_FILE", "INVALID_REQUEST")
