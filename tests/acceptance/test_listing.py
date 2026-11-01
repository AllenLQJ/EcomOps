"""listing 合规审查验收：三层判定、降级、非 PDF、列表/详情与租户隔离。"""
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

import httpx
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

_ACC = Path(__file__).resolve().parent
if str(_ACC) not in sys.path:
    sys.path.insert(0, str(_ACC))

pytest_plugins = ["conftest_kb"]

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

CATEGORY = "HOME_APPLIANCE_HEATER"

BASE_PARAMS = """商品名称：速热电热水器
额定功率：2000W
容量：60L
能效等级：一级
3C认证编号：2024010112345678
产品卖点：安全节能，品质可靠
"""

LISTING_REQUIRED = (
    "review_id",
    "verdict",
    "blocking_violations",
    "manual_verify_items",
    "advisory_suggestions",
    "fallback_used",
)


def _pdf_bytes(text: str) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.setFont("STSong-Light", 12)
    y = 800
    for line in text.splitlines():
        if not line.strip():
            y -= 16
            continue
        c.drawString(40, y, line[:80])
        y -= 18
        if y < 40:
            c.showPage()
            c.setFont("STSong-Light", 12)
            y = 800
    c.save()
    return buf.getvalue()


def _assert_listing_report(data: dict, *, ctx: str) -> None:
    for key in LISTING_REQUIRED:
        assert key in data, f"{ctx}: ListingReport 缺必填字段 {key}: {data}"
    assert data["verdict"] in (
        "REJECTED_BLOCKING",
        "PENDING_MANUAL_VERIFY",
        "PASSED_WITH_SUGGESTIONS",
    ), f"{ctx}: verdict 非法 {data['verdict']!r}"
    assert isinstance(data["blocking_violations"], list), f"{ctx}: blocking_violations 应为 list"
    assert isinstance(data["manual_verify_items"], list), f"{ctx}: manual_verify_items 应为 list"
    assert isinstance(data["advisory_suggestions"], list), f"{ctx}: advisory_suggestions 应为 list"
    assert isinstance(data["fallback_used"], bool), f"{ctx}: fallback_used 应为 bool"


def _upload(env, headers: dict, pdf: bytes, filename: str = "listing.pdf") -> httpx.Response:
    return httpx.post(
        f"{env.ops_url}/api/v1/listing/reviews",
        headers=headers,
        files={"file": (filename, pdf, "application/pdf")},
        data={"declared_category": CATEGORY},
        timeout=60.0,
    )


def _err_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("error", {}).get("code")
    except Exception:
        return None


def test_extreme_words_rejected(kb_env):
    h = kb_env.token("operator", "list-op-1", "tenant-a")
    text = BASE_PARAMS + "宣传语：全网最低价、国家级品质保障\n"
    resp = _upload(kb_env, h, _pdf_bytes(text))
    assert resp.status_code == 201, f"上传应 201，得 {resp.status_code}: {resp.text}"
    data = resp.json()
    _assert_listing_report(data, ctx="极限词")
    assert data["verdict"] == "REJECTED_BLOCKING", f"含「全网最低价、国家级」应为 REJECTED_BLOCKING，得 {data}"
    assert data["blocking_violations"], f"blocking_violations 应非空，得 {data}"


def test_medical_claims_rejected(kb_env):
    h = kb_env.token("operator", "list-op-2", "tenant-a")
    text = BASE_PARAMS + "功效宣称：防脱生发，头皮护理\n"
    resp = _upload(kb_env, h, _pdf_bytes(text))
    assert resp.status_code == 201, f"上传应 201，得 {resp.status_code}: {resp.text}"
    data = resp.json()
    _assert_listing_report(data, ctx="医疗功效")
    assert data["verdict"] == "REJECTED_BLOCKING", f"含「防脱生发」应为 REJECTED_BLOCKING，得 {data}"
    assert data["blocking_violations"], f"blocking_violations 应非空，得 {data}"


def test_missing_required_param_rejected(kb_env):
    h = kb_env.token("operator", "list-op-3", "tenant-a")
    text = """商品名称：速热电热水器
额定功率：2000W
容量：60L
3C认证编号：2024010112345678
产品卖点：安全节能
"""
    resp = _upload(kb_env, h, _pdf_bytes(text))
    assert resp.status_code == 201, f"上传应 201，得 {resp.status_code}: {resp.text}"
    data = resp.json()
    _assert_listing_report(data, ctx="缺能效等级")
    assert data["verdict"] == "REJECTED_BLOCKING", (
        f"declared_category=HOME_APPLIANCE_HEATER 缺「能效等级」应为 REJECTED_BLOCKING，得 {data}"
    )
    assert data["blocking_violations"], f"缺必填参数时 blocking_violations 应非空，得 {data}"


def test_dubious_cert_pending_manual(kb_env):
    h = kb_env.token("operator", "list-op-4", "tenant-a")
    text = """商品名称：速热电热水器
额定功率：2000W
容量：60L
能效等级：一级
3C认证编号：ABC
产品卖点：安全节能，品质可靠
"""
    resp = _upload(kb_env, h, _pdf_bytes(text))
    assert resp.status_code == 201, f"上传应 201，得 {resp.status_code}: {resp.text}"
    data = resp.json()
    _assert_listing_report(data, ctx="证书存疑")
    assert data["verdict"] == "PENDING_MANUAL_VERIFY", (
        f"参数齐全但证书格式存疑应为 PENDING_MANUAL_VERIFY，得 {data}"
    )
    assert data["manual_verify_items"], f"manual_verify_items 应非空，得 {data}"


def test_passed_with_suggestions(kb_env):
    h = kb_env.token("operator", "list-op-5", "tenant-a")
    resp = _upload(kb_env, h, _pdf_bytes(BASE_PARAMS))
    assert resp.status_code == 201, f"上传应 201，得 {resp.status_code}: {resp.text}"
    data = resp.json()
    _assert_listing_report(data, ctx="合规通过")
    assert data["verdict"] == "PASSED_WITH_SUGGESTIONS", (
        f"参数齐全无违规无证书存疑应为 PASSED_WITH_SUGGESTIONS，得 {data}"
    )
    assert data["advisory_suggestions"], f"advisory_suggestions 应非空，得 {data}"
    assert data["fallback_used"] is False, f"无降级时 fallback_used 应为 false，得 {data}"


def test_cert_extract_llm_fail_fallback(kb_env):
    h = kb_env.token("operator", "list-op-6", "tenant-a")
    kb_env.faults({"llm_fail": ["cert_extract"]})
    try:
        resp = _upload(kb_env, h, _pdf_bytes(BASE_PARAMS))
        assert resp.status_code == 201, f"上传应 201，得 {resp.status_code}: {resp.text}"
        data = resp.json()
        _assert_listing_report(data, ctx="cert_extract 降级")
        assert data["verdict"] == "PENDING_MANUAL_VERIFY", (
            f"cert_extract 降级应为 PENDING_MANUAL_VERIFY，得 {data}"
        )
        assert data["fallback_used"] is True, f"降级时应 fallback_used=true，得 {data}"
    finally:
        kb_env.faults({})


def test_non_pdf_unsupported(kb_env):
    h = kb_env.token("operator", "list-op-7", "tenant-a")
    resp = httpx.post(
        f"{kb_env.ops_url}/api/v1/listing/reviews",
        headers=h,
        files={"file": ("notes.txt", b"not a pdf", "text/plain")},
        data={"declared_category": CATEGORY},
        timeout=30.0,
    )
    assert resp.status_code == 400, f"非 PDF 应 400，得 {resp.status_code}: {resp.text}"
    assert _err_code(resp) == "UNSUPPORTED_FILE", (
        f"错误码应为 UNSUPPORTED_FILE，得 {_err_code(resp)}: {resp.text}"
    )


def test_list_detail_and_tenant_isolation(kb_env):
    h_a = kb_env.token("operator", "list-op-a", "tenant-a")
    h_b = kb_env.token("operator", "list-op-b", "tenant-b")

    created = _upload(kb_env, h_a, _pdf_bytes(BASE_PARAMS))
    assert created.status_code == 201, created.text
    report = created.json()
    _assert_listing_report(report, ctx="创建")
    review_id = report["review_id"]

    lst = httpx.get(f"{kb_env.ops_url}/api/v1/listing/reviews", headers=h_a, timeout=30.0)
    assert lst.status_code == 200, f"列表应 200: {lst.status_code} {lst.text}"
    body = lst.json()
    assert "reviews" in body, f"列表应含 reviews: {body}"
    assert any(r.get("review_id") == review_id for r in body["reviews"]), (
        f"本租户列表应含刚创建的 review_id={review_id}: {body}"
    )
    for r in body["reviews"]:
        _assert_listing_report(r, ctx="列表项")

    detail = httpx.get(f"{kb_env.ops_url}/api/v1/listing/reviews/{review_id}", headers=h_a, timeout=30.0)
    assert detail.status_code == 200, f"详情应 200: {detail.status_code} {detail.text}"
    _assert_listing_report(detail.json(), ctx="详情")

    other = httpx.get(f"{kb_env.ops_url}/api/v1/listing/reviews/{review_id}", headers=h_b, timeout=30.0)
    assert other.status_code == 404, f"他租户详情应 404，得 {other.status_code}: {other.text}"
    assert _err_code(other) == "NOT_FOUND", f"他租户错误码应为 NOT_FOUND，得 {_err_code(other)}"
