"""Static-only checks for the no-build management page; no browser required."""
from html.parser import HTMLParser
from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "static" / "index.html"


class Structure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.ids = set()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "script" and "src" in attrs:
            self.scripts.append(attrs["src"])


def test_html_structure_and_pages():
    assert INDEX.is_file()
    source = INDEX.read_text(encoding="utf-8")
    parser = Structure()
    parser.feed(source)
    assert source.lower().startswith("<!doctype html>")
    for tag in ("html", "head", "title", "body", "nav", "main"):
        assert tag in parser.tags
        assert f"</{tag}>" in source
    assert {"app", "claims-page", "kb-page", "listing-page", "training-page", "alerts-page"} <= parser.ids
    for title in ("售后审批", "知识待补充", "文案审查", "客服对练", "告警"):
        assert title in source
    assert parser.scripts == ["https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js"]
    assert "innerHTML" not in source
    assert "v-html" not in source


def test_key_api_paths_and_controls():
    source = INDEX.read_text(encoding="utf-8")
    for path in (
        "/api/v1/dev/token", "/api/v1/claims?status=", "/api/v1/claims/",
        "/confirm", "/api/v1/kb/ask", "/api/v1/kb/pending?status=",
        "/api/v1/kb/pending/", "/discard", "/publish", "/retry",
        "/api/v1/listing/reviews", "/api/v1/training/sessions",
        "/chat/stream", "/api/v1/alerts", "/resolve",
    ):
        assert path in source
    for marker in (
        "ALERT_STATE_CONFLICT", "document.hidden", "当前角色无权访问",
        "暂无告警", "已提交重试", "由系统自动重试，无需人工操作",
        "Authorization", "Bearer ", "localStorage", "FormData", "getReader",
        "TextDecoder", "PENDING_SUPERVISOR_REVIEW", "MODIFY_AMOUNT",
        "REQUEST_EVIDENCE", "idempotent_replayed", "max_refundable_cents",
        "已处理，无重复操作", "已进入待补录队列", "5000",
        "blocking_violations", "manual_verify_items", "advisory_suggestions",
        "rule_accuracy", "emotional_soothing", "solution_compliance",
    ):
        assert marker in source


def test_error_codes_and_role_guards():
    source = INDEX.read_text(encoding="utf-8")
    for code in (
        "INVALID_REQUEST", "UNSUPPORTED_FILE", "UNAUTHORIZED",
        "INTERNAL_AUTH_FAILED", "FORBIDDEN_ROLE", "NOT_FOUND",
        "ALERT_STATE_CONFLICT", "CLAIM_STATE_CONFLICT",
        "PUBLISH_STATE_CONFLICT", "AMOUNT_EXCEEDS_LIMIT", "INVALID_STAGE",
        "UPSTREAM_ERROR", "UPSTREAM_TIMEOUT",
    ):
        assert f"{code}:" in source
    for marker in ("visiblePages", "canAccess", "canWriteKB", "decodeIdentity",
                   "expireLogin", "localStorage.removeItem", "stuck:",
                   "Array.from(resolveReason.trim()).length<3"):
        assert marker in source
