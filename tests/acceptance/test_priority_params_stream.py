"""C-PPS 验收：待审加急排序 / 品类必填参数 / 真流式 LLM（假 OpenAI 兼容服务）。

仅依据行为说明、contracts/ 与现有 acceptance 写法；不依赖 ecomops 实现细节。
A/B 使用无 LLM 配置的默认 ops_env / kb_env；C 放在独立测试类，经类内夹具注入 ECOMOPS_LLM_*。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import fitz
import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from tests.stack import free_port

_ACC = Path(__file__).resolve().parent
if str(_ACC) not in sys.path:
    sys.path.insert(0, str(_ACC))

pytest_plugins = ["conftest_kb"]

DEFAULT_TENANT = "SHOP_SELF_01"
PENDING = "PENDING_SUPERVISOR_REVIEW"
PRECISE_Q = "热水器质量问题几天内可以退换"
MISSING_PARAM_PREFIX = "缺少必填参数："

# ---------------------------------------------------------------------------
# 共用：SSE / PDF
# ---------------------------------------------------------------------------


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _parse_sse(response: httpx.Response) -> list[tuple[str, Any]]:
    """按空行分帧，解析 event: / data: 行。"""
    events: list[tuple[str, Any]] = []
    event_name: str | None = None
    data_lines: list[str] = []

    def _flush() -> None:
        nonlocal event_name, data_lines
        if event_name is None and not data_lines:
            return
        name = event_name or "message"
        raw = "\n".join(data_lines)
        payload: Any = raw
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
        events.append((name, payload))
        event_name = None
        data_lines = []

    for line in response.iter_lines():
        if line == "":
            _flush()
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith(":"):
            continue
    _flush()
    return events


def _pdf_bytes(text: str) -> bytes:
    """用 PyMuPDF 现场生成可抽取中文的 PDF。"""
    doc = fitz.open()
    try:
        page = doc.new_page(width=595, height=842)
        y = 72.0
        for line in text.splitlines():
            if y > 800:
                page = doc.new_page(width=595, height=842)
                y = 72.0
            if not line.strip():
                y += 16
                continue
            page.insert_text((40, y), line[:80], fontname="china-s", fontsize=12)
            y += 18
        return doc.tobytes()
    finally:
        doc.close()


def _blocking_messages(report: dict) -> list[str]:
    out: list[str] = []
    for item in report.get("blocking_violations") or []:
        if isinstance(item, dict):
            msg = item.get("message")
            if isinstance(msg, str):
                out.append(msg)
        elif isinstance(item, str):
            out.append(item)
    return out


def _missing_param_messages(report: dict) -> list[str]:
    return [m for m in _blocking_messages(report) if MISSING_PARAM_PREFIX in m]


# ===========================================================================
# A. 待审列表按加急标记优先排序
# ===========================================================================


def _submit_pending(
    ops_env,
    make_claim_payload: Callable[..., dict[str, Any]],
    *,
    claim_no: str,
    nlu_signal: dict[str, Any] | None,
    omit_nlu: bool = False,
) -> None:
    kwargs: dict[str, Any] = {
        "claim_no": claim_no,
        "tenant_id": DEFAULT_TENANT,
        "days_since_delivery": 5,
        "buyer_reason_text": "漏水",
        "evidence_urls": ["https://storage.example.invalid/evidence/e1.jpg"],
        "item_payable_cents": 112000,
        "max_refundable_cents": 112000,
    }
    if omit_nlu:
        payload = make_claim_payload(**kwargs)
        payload.pop("nlu_signal", None)
    else:
        assert nlu_signal is not None
        payload = make_claim_payload(**kwargs, nlu_signal=nlu_signal)
    r = ops_env.submit_claim(payload)
    assert r.status_code == 202, (
        f"submit {claim_no}: expected 202, got {r.status_code}: {r.text[:400]}"
    )
    assert r.json().get("status") == PENDING, (
        f"submit {claim_no}: expected {PENDING}, got {r.json()!r}"
    )


def _list_claims(ops_env, headers: dict[str, str], *, status: str | None = None) -> list[dict]:
    params = {"status": status} if status is not None else None
    r = httpx.get(
        f"{ops_env.url}/api/v1/claims",
        headers=headers,
        params=params,
        timeout=30.0,
    )
    assert r.status_code == 200, f"GET /api/v1/claims expected 200, got {r.status_code}: {r.text[:400]}"
    body = r.json()
    assert "claims" in body and isinstance(body["claims"], list), (
        f"GET /api/v1/claims: response must contain claims list, got {body!r}"
    )
    return body["claims"]


def test_claims_list_priority_boost_ordering(ops_env, make_claim_payload: Callable[..., dict[str, Any]]):
    """同一 PENDING 下：加急在前（新→旧），非加急再按创建时间倒序 → D,B,C,A。"""
    h = ops_env.ops_token("supervisor", "sup-pps-a", tenant_id=DEFAULT_TENANT)

    no_a = _uid("CLM_PPS_A")
    no_b = _uid("CLM_PPS_B")
    no_c = _uid("CLM_PPS_C")
    no_d = _uid("CLM_PPS_D")

    _submit_pending(
        ops_env,
        make_claim_payload,
        claim_no=no_a,
        nlu_signal={
            "predicted_label": "热水器",
            "category_confidence": 0.98,
            "sentiment": "NEGATIVE",
            "sentiment_score": 0.9,
            "priority_boost": False,
        },
    )
    time.sleep(1.1)
    _submit_pending(
        ops_env,
        make_claim_payload,
        claim_no=no_b,
        nlu_signal={
            "priority_boost": True,
            "sentiment": "NEGATIVE",
            "sentiment_score": 0.95,
        },
    )
    time.sleep(1.1)
    _submit_pending(ops_env, make_claim_payload, claim_no=no_c, nlu_signal=None, omit_nlu=True)
    time.sleep(1.1)
    _submit_pending(
        ops_env,
        make_claim_payload,
        claim_no=no_d,
        nlu_signal={"priority_boost": True},
    )

    wanted = {no_a, no_b, no_c, no_d}
    claims = [c for c in _list_claims(ops_env, h) if c.get("claim_no") in wanted]
    assert [c.get("claim_no") for c in claims] == [no_d, no_b, no_c, no_a], (
        f"priority list order expected D,B,C,A "
        f"(boost desc then created_at desc); got {[c.get('claim_no') for c in claims]}; "
        f"full={[ (c.get('claim_no'), c.get('priority_boost'), c.get('created_at')) for c in claims ]}"
    )

    expected_boost = {no_a: False, no_b: True, no_c: False, no_d: True}
    for c in claims:
        cn = c["claim_no"]
        assert "priority_boost" in c, f"list item {cn}: missing priority_boost: {c}"
        assert isinstance(c["priority_boost"], bool), (
            f"list item {cn}: priority_boost must be bool, "
            f"got {type(c['priority_boost']).__name__}: {c['priority_boost']!r}"
        )
        assert c["priority_boost"] is expected_boost[cn], (
            f"list item {cn}: priority_boost expected {expected_boost[cn]}, got {c['priority_boost']!r}"
        )

    filtered = [
        c for c in _list_claims(ops_env, h, status=PENDING) if c.get("claim_no") in wanted
    ]
    assert [c.get("claim_no") for c in filtered] == [no_d, no_b, no_c, no_a], (
        f"?status={PENDING} must keep D,B,C,A order; "
        f"got {[c.get('claim_no') for c in filtered]}"
    )

    for cn, boost in expected_boost.items():
        detail = ops_env.get_claim(h, cn)
        assert detail.status_code == 200, (
            f"GET detail {cn}: expected 200, got {detail.status_code}: {detail.text[:300]}"
        )
        body = detail.json()
        assert "priority_boost" in body, f"detail {cn}: missing priority_boost: {body}"
        assert isinstance(body["priority_boost"], bool), (
            f"detail {cn}: priority_boost must be bool, "
            f"got {type(body['priority_boost']).__name__}: {body['priority_boost']!r}"
        )
        assert body["priority_boost"] is boost, (
            f"detail {cn}: priority_boost expected {boost}, got {body['priority_boost']!r}"
        )

    other = ops_env.ops_token("supervisor", "sup-pps-other", tenant_id="OTHER_TENANT")
    other_list = _list_claims(ops_env, other)
    leaked = [c for c in other_list if c.get("claim_no") in wanted]
    assert leaked == [], (
        f"other tenant must not see these claims; list should be empty of ours, got {leaked!r}"
    )


# ===========================================================================
# B. 合规审查：按品类的必填参数表
# ===========================================================================

# 字段样例刻意避开极限词/违规功效词（最/第一/国家级/永久/根治/100%/防脱…）
_FIELD_VALUES: dict[str, str] = {
    "额定功率": "2000W",
    "容量": "60L",
    "能效等级": "二级",
    "3C": "2024010112345678",
    "净含量": "500ml",
    "保质期": "36个月",
    "备案号": "粤妆网备字2024123456",
    "品名": "示例商品",
    "规格": "标准规格",
    "屏幕尺寸": "11英寸",
    "存储容量": "256GB",
    "处理器": "八核处理器",
    "内存": "16GB",
    "书名": "示例读本",
    "ISBN": "9787111544937",
    "出版社": "示例出版社",
    "产地": "山东烟台",
    "生产许可证编号": "SC12345678901234",
    "面料成分": "棉与涤纶混纺",
    "尺码": "L码",
    "执行标准": "GB/T 2660",
    "酒店名称": "示例旅店",
    "地址": "示例市示例路1号",
    "入住时间": "14点后",
    "退订规则": "入住前一天可免费取消",
}

CATEGORY_REQUIRED: list[tuple[str, list[str]]] = [
    ("HOME_APPLIANCE_HEATER", ["额定功率", "容量", "能效等级", "3C"]),
    ("PERSONAL_SHAMPOO", ["净含量", "保质期", "备案号"]),
    ("DAILY_GENERAL", ["品名", "规格"]),
    ("DIGITAL_TABLET", ["品名", "屏幕尺寸", "存储容量", "3C"]),
    ("DIGITAL_PHONE", ["品名", "屏幕尺寸", "存储容量", "3C"]),
    ("DIGITAL_COMPUTER", ["品名", "处理器", "内存", "存储容量"]),
    ("BOOKS", ["书名", "ISBN", "出版社"]),
    ("FRESH_FRUIT", ["品名", "产地", "净含量", "保质期"]),
    ("DAIRY_MILK", ["品名", "净含量", "保质期", "生产许可证编号"]),
    ("APPAREL", ["品名", "面料成分", "尺码", "执行标准"]),
    ("TRAVEL_HOTEL", ["酒店名称", "地址", "入住时间", "退订规则"]),
    ("SOMETHING_ELSE", ["品名"]),
]


def _params_text(fields: list[str]) -> str:
    lines = [f"{f}：{_FIELD_VALUES[f]}" for f in fields]
    lines.append("产品卖点：安全可靠，日常家用")
    return "\n".join(lines) + "\n"


def _upload_listing(env, headers: dict[str, str], pdf: bytes, *, declared_category: str) -> httpx.Response:
    return httpx.post(
        f"{env.ops_url}/api/v1/listing/reviews",
        headers=headers,
        files={"file": ("listing.pdf", pdf, "application/pdf")},
        data={"declared_category": declared_category},
        timeout=60.0,
    )


@pytest.mark.parametrize(
    "category,fields",
    CATEGORY_REQUIRED,
    ids=[c for c, _ in CATEGORY_REQUIRED],
)
def test_listing_required_params_complete_not_blocking(kb_env, category: str, fields: list[str]):
    """字段齐全且无极限词/违规功效 → 不应出现「缺少必填参数」阻断项。"""
    h = kb_env.token("operator", f"list-pps-ok-{category[:12]}", "tenant-a")
    resp = _upload_listing(kb_env, h, _pdf_bytes(_params_text(fields)), declared_category=category)
    assert resp.status_code == 201, (
        f"{category} complete upload expected 201, got {resp.status_code}: {resp.text[:400]}"
    )
    data = resp.json()
    missing = _missing_param_messages(data)
    assert missing == [], (
        f"{category} with all required fields must not have blocking messages "
        f"containing {MISSING_PARAM_PREFIX!r}; verdict={data.get('verdict')!r} missing={missing!r} "
        f"blocking={data.get('blocking_violations')!r}"
    )


@pytest.mark.parametrize(
    "category,fields",
    CATEGORY_REQUIRED,
    ids=[c for c, _ in CATEGORY_REQUIRED],
)
def test_listing_required_params_drop_last_field(kb_env, category: str, fields: list[str]):
    """删掉最后一个必填字段 → REJECTED_BLOCKING，且恰好报缺该字段。"""
    h = kb_env.token("operator", f"list-pps-drop-{category[:12]}", "tenant-a")
    dropped = fields[-1]
    kept = fields[:-1]
    resp = _upload_listing(kb_env, h, _pdf_bytes(_params_text(kept)), declared_category=category)
    assert resp.status_code == 201, (
        f"{category} drop-last upload expected 201, got {resp.status_code}: {resp.text[:400]}"
    )
    data = resp.json()
    assert data.get("verdict") == "REJECTED_BLOCKING", (
        f"{category} missing {dropped!r}: verdict expected REJECTED_BLOCKING, "
        f"got {data.get('verdict')!r}; body={data}"
    )
    missing = _missing_param_messages(data)
    needle = f"{MISSING_PARAM_PREFIX}{dropped}"
    assert any(needle in m for m in missing), (
        f"{category}: expected a blocking message containing {needle!r}, got {missing!r}; "
        f"blocking={data.get('blocking_violations')!r}"
    )
    for f in kept:
        other = f"{MISSING_PARAM_PREFIX}{f}"
        assert not any(other in m for m in missing), (
            f"{category}: should only report missing {dropped!r}, but also found {other!r} "
            f"in {missing!r}"
        )


@pytest.mark.parametrize(
    "category,fields",
    CATEGORY_REQUIRED,
    ids=[c for c, _ in CATEGORY_REQUIRED],
)
def test_listing_required_params_all_missing(kb_env, category: str, fields: list[str]):
    """全缺必填字段 → REJECTED_BLOCKING，每个必填字段都有「缺少必填参数：…」。"""
    h = kb_env.token("operator", f"list-pps-none-{category[:12]}", "tenant-a")
    emptyish = "产品介绍：日常家用物资\n使用说明：按说明书操作\n"
    resp = _upload_listing(kb_env, h, _pdf_bytes(emptyish), declared_category=category)
    assert resp.status_code == 201, (
        f"{category} all-missing upload expected 201, got {resp.status_code}: {resp.text[:400]}"
    )
    data = resp.json()
    assert data.get("verdict") == "REJECTED_BLOCKING", (
        f"{category} all-missing: verdict expected REJECTED_BLOCKING, "
        f"got {data.get('verdict')!r}; body={data}"
    )
    missing = _missing_param_messages(data)
    for f in fields:
        needle = f"{MISSING_PARAM_PREFIX}{f}"
        assert any(needle in m for m in missing), (
            f"{category} all-missing: expected message containing {needle!r}, got {missing!r}; "
            f"blocking={data.get('blocking_violations')!r}"
        )


# ===========================================================================
# C. 真流式：假 OpenAI 兼容服务 + 独立 Ops（带 ECOMOPS_LLM_*）
# ===========================================================================


class _FakeLlmControl:
    """可编程假 LLM：mode / chunks，并记录请求体。"""

    def __init__(self) -> None:
        self.mode: str = "ok"  # ok | disconnect_after_n | http_500
        self.disconnect_after: int = 2
        self.chunks: list[str] = ["质量", "问题", "七天", "可退"]
        self.requests: list[dict[str, Any]] = []
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reset(self, *, mode: str = "ok", disconnect_after: int = 2, chunks: list[str] | None = None) -> None:
        self.mode = mode
        self.disconnect_after = disconnect_after
        if chunks is not None:
            self.chunks = list(chunks)
        self.requests.clear()


def _build_fake_llm_app(ctrl: _FakeLlmControl) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        ctrl.requests.append(body)

        if ctrl.mode == "http_500":
            return Response(content=b"upstream boom", status_code=500)

        if not body.get("stream"):
            content = "".join(ctrl.chunks)
            return JSONResponse({"choices": [{"message": {"content": content}}]})

        async def event_gen():
            for i, piece in enumerate(ctrl.chunks):
                if ctrl.mode == "disconnect_after_n" and i >= ctrl.disconnect_after:
                    return
                payload = {"choices": [{"delta": {"content": piece}}]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.05)
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_gen(), media_type="text/event-stream")

    return app


@pytest.fixture(scope="module")
def fake_llm_server():
    """模块级假 OpenAI 兼容服务（uvicorn 线程）；各用例通过 ctrl.reset 切换行为。"""
    ctrl = _FakeLlmControl()
    ctrl.port = free_port()
    app = _build_fake_llm_app(ctrl)
    config = uvicorn.Config(app, host="127.0.0.1", port=ctrl.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            r = httpx.get(f"{ctrl.base_url}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError(f"fake LLM server failed to start on port {ctrl.port}")

    yield ctrl

    server.should_exit = True
    thread.join(timeout=5.0)


class TestTrueStreamingWithFakeLlm:
    """配置真实流式 LLM 的验收；ECOMOPS_LLM_* 仅作用于本类夹具启动的 Ops。"""

    @pytest.fixture(autouse=True)
    def _llm_env_before_ops(self, fake_llm_server, monkeypatch):
        # 必须在 kb_env 起子进程前写入；monkeypatch 离开本类用例后还原，不泄漏到 A/B。
        monkeypatch.setenv("ECOMOPS_LLM_BASE_URL", f"{fake_llm_server.base_url}/v1")
        monkeypatch.setenv("ECOMOPS_LLM_API_KEY", "test-key")
        monkeypatch.setenv("ECOMOPS_LLM_MODEL", "fake")
        fake_llm_server.reset()
        yield

    @pytest.fixture
    def env(self, _llm_env_before_ops, kb_env):
        return kb_env

    @pytest.fixture
    def llm(self, fake_llm_server) -> _FakeLlmControl:
        return fake_llm_server

    @staticmethod
    def _ask_stream(env, headers: dict[str, str], query: str, *, session_id: str | None = None):
        payload: dict[str, Any] = {"query": query}
        if session_id is not None:
            payload["session_id"] = session_id
        with httpx.stream(
            "POST",
            f"{env.ops_url}/api/v1/kb/ask/stream",
            headers=headers,
            json=payload,
            timeout=60.0,
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                return resp, []
            return resp, _parse_sse(resp)

    @staticmethod
    def _chat_stream(env, headers: dict[str, str], session_id: str, message: str):
        with httpx.stream(
            "POST",
            f"{env.ops_url}/api/v1/training/sessions/{session_id}/chat/stream",
            headers=headers,
            json={"message": message},
            timeout=60.0,
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                return resp, []
            return resp, _parse_sse(resp)

    @staticmethod
    def _prompt_blob(body: dict[str, Any]) -> str:
        parts: list[str] = []
        for m in body.get("messages") or []:
            if isinstance(m, dict) and isinstance(m.get("content"), str):
                parts.append(m["content"])
        return "\n".join(parts)

    @staticmethod
    def _assert_fenced(blob: str, plain: str, *, ctx: str) -> None:
        assert plain in blob, f"{ctx}: prompt must contain original text {plain!r}; blob={blob[:500]!r}"
        # llm.untrusted → <label>\ntext\n</label>
        pat = re.compile(
            rf"<([A-Za-z0-9_]+)>\s*\n?{re.escape(plain)}\n?\s*</\1>",
            re.MULTILINE,
        )
        assert pat.search(blob), (
            f"{ctx}: original text must be wrapped in delimiter tags "
            f"(<label>...{plain}...</label>); blob={blob[:800]!r}"
        )

    @staticmethod
    def _token_payloads(events: list[tuple[str, Any]]) -> list[dict]:
        out: list[dict] = []
        for name, payload in events:
            if name == "token":
                assert isinstance(payload, dict), f"token data must be object, got {payload!r}"
                out.append(payload)
        return out

    def test_kb_stream_ok_token_sequence(self, env, llm: _FakeLlmControl):
        """假 LLM 正常 4 增量：meta → ≥4 token → done；拼接==answer；stream=true；fallback=false。"""
        chunks = ["质量", "问题", "七天", "可退"]
        llm.reset(mode="ok", chunks=chunks)
        h = env.token("operator", "pps-kb-ok", "tenant-a")
        resp, events = self._ask_stream(env, h, PRECISE_Q)
        assert resp.status_code == 200, f"kb stream ok: expected 200, got {resp.status_code}"
        ctype = (resp.headers.get("content-type") or "").lower()
        assert "text/event-stream" in ctype, f"kb stream ok: Content-Type SSE, got {ctype!r}"

        names = [n for n, _ in events]
        assert names, f"kb stream ok: expected SSE events, got none"
        assert names[0] == "meta", f"kb stream ok: first event must be meta, got {names[0]!r}; {events}"
        assert names[-1] == "done", f"kb stream ok: last event must be done, got {names[-1]!r}; {events}"
        assert "meta" in names and names.index("meta") < next(
            i for i, n in enumerate(names) if n == "token"
        ), f"kb stream ok: meta must precede first token; names={names}"

        tokens = self._token_payloads(events)
        assert len(tokens) >= 4, f"kb stream ok: need ≥4 token events, got {len(tokens)}: {tokens}"
        for i, piece in enumerate(chunks):
            assert tokens[i].get("text") == piece, (
                f"kb stream ok: token[{i}].text expected {piece!r}, got {tokens[i]!r}"
            )

        done = events[-1][1]
        assert isinstance(done, dict), f"kb stream ok: done payload must be object, got {done!r}"
        joined = "".join(str(t.get("text", "")) for t in tokens)
        assert joined == done.get("answer"), (
            f"kb stream ok: token concat must equal done.answer; "
            f"joined={joined!r} answer={done.get('answer')!r}"
        )
        assert done.get("fallback_used") is False, (
            f"kb stream ok: done.fallback_used expected False, got {done.get('fallback_used')!r}; {done}"
        )

        assert llm.requests, "kb stream ok: fake LLM must have received at least one request"
        last = llm.requests[-1]
        assert last.get("stream") is True, (
            f"kb stream ok: fake LLM request must have stream=true, got {last.get('stream')!r}; {last}"
        )
        self._assert_fenced(self._prompt_blob(last), PRECISE_Q, ctx="kb stream ok prompt")

    def test_kb_stream_midway_disconnect_reset_then_fallback(self, env, llm: _FakeLlmControl):
        """第 2 个增量后断开 → reset token → 降级 token；fallback_used；answer==reset 后拼接。"""
        chunks = ["甲", "乙", "丙", "丁"]
        llm.reset(mode="disconnect_after_n", disconnect_after=2, chunks=chunks)
        h = env.token("operator", "pps-kb-mid", "tenant-a")
        resp, events = self._ask_stream(env, h, PRECISE_Q)
        assert resp.status_code == 200, f"kb mid-fail: expected 200, got {resp.status_code}"

        tokens = self._token_payloads(events)
        reset_idxs = [i for i, t in enumerate(tokens) if t.get("reset") is True]
        assert reset_idxs, (
            f"kb mid-fail: expected a token event with data {{'reset': true}}, got tokens={tokens}"
        )
        reset_i = reset_idxs[0]
        after = tokens[reset_i + 1 :]
        assert after, f"kb mid-fail: expected fallback tokens after reset, got {tokens}"

        done = events[-1][1]
        assert isinstance(done, dict) and events[-1][0] == "done", (
            f"kb mid-fail: last event must be done object, got {events[-1]!r}"
        )
        assert done.get("fallback_used") is True, (
            f"kb mid-fail: done.fallback_used expected True, got {done.get('fallback_used')!r}; {done}"
        )
        joined_after = "".join(str(t.get("text", "")) for t in after)
        assert done.get("answer") == joined_after, (
            f"kb mid-fail: done.answer must equal token texts after reset; "
            f"answer={done.get('answer')!r} after={joined_after!r}"
        )

    def test_kb_stream_immediate_500_fallback_no_reset(self, env, llm: _FakeLlmControl):
        """假 LLM 直接 500：无 reset，直接降级 token，fallback_used=true。"""
        llm.reset(mode="http_500", chunks=["不会发出"])
        h = env.token("operator", "pps-kb-500", "tenant-a")
        resp, events = self._ask_stream(env, h, PRECISE_Q)
        assert resp.status_code == 200, f"kb 500: expected 200 SSE, got {resp.status_code}"

        tokens = self._token_payloads(events)
        assert tokens, f"kb 500: expected fallback token events, got {events}"
        assert all(t.get("reset") is not True for t in tokens), (
            f"kb 500: must not emit reset token on immediate failure, got {tokens}"
        )
        done = events[-1][1]
        assert events[-1][0] == "done" and isinstance(done, dict), (
            f"kb 500: last event must be done, got {events[-1]!r}"
        )
        assert done.get("fallback_used") is True, (
            f"kb 500: done.fallback_used expected True, got {done.get('fallback_used')!r}; {done}"
        )
        joined = "".join(str(t.get("text", "")) for t in tokens)
        assert done.get("answer") == joined, (
            f"kb 500: done.answer must equal all token texts; "
            f"answer={done.get('answer')!r} joined={joined!r}"
        )

    def test_kb_stream_foreign_session_http_404(self, env, llm: _FakeLlmControl):
        """用户 B 带用户 A 的 session_id 调 /ask/stream → HTTP 404（非 200 流内错误）。"""
        llm.reset(mode="ok", chunks=["沙", "盒", "答", "案"])
        h_a = env.token("operator", "pps-kb-owner", "tenant-a")
        h_b = env.token("operator", "pps-kb-other", "tenant-a")
        sid = f"pps-kb-sess-{uuid.uuid4().hex[:8]}"

        resp1, events1 = self._ask_stream(env, h_a, PRECISE_Q, session_id=sid)
        assert resp1.status_code == 200, (
            f"owner stream expected 200 to create session, got {resp1.status_code}"
        )
        assert events1, f"owner stream should yield SSE events"

        resp2, events2 = self._ask_stream(env, h_b, PRECISE_Q, session_id=sid)
        assert resp2.status_code == 404, (
            f"foreign session must be HTTP 404 (not 200+in-stream error), "
            f"got {resp2.status_code}; events={events2!r}"
        )
        assert events2 == [], (
            f"foreign session 404 must not return SSE event list, got {events2!r}"
        )

    def test_training_stream_ok_stage_before_tokens_and_persist(self, env, llm: _FakeLlmControl):
        """对练正常流：stage 在首个 token 前；落库买家消息 == token 拼接；请求 stream=true。"""
        chunks = ["好的", "请", "稍等", "核实"]
        llm.reset(mode="ok", chunks=chunks)
        h = env.token("cs_agent", "pps-train-ok", "tenant-a")
        created = httpx.post(
            f"{env.ops_url}/api/v1/training/sessions",
            headers=h,
            json={},
            timeout=30.0,
        )
        assert created.status_code == 201, (
            f"create training session expected 201, got {created.status_code}: {created.text[:300]}"
        )
        sid = created.json()["session_id"]
        agent_msg = "请提供订单号"

        resp, events = self._chat_stream(env, h, sid, agent_msg)
        assert resp.status_code == 200, f"training stream ok: expected 200, got {resp.status_code}"
        names = [n for n, _ in events]
        assert "stage" in names and "token" in names, (
            f"training stream ok: need stage and token events, got {names}"
        )
        assert names.index("stage") < names.index("token"), (
            f"training stream ok: stage must precede first token; names={names}"
        )
        tokens = self._token_payloads(events)
        assert len(tokens) >= len(chunks), (
            f"training stream ok: need ≥{len(chunks)} tokens, got {tokens}"
        )
        for i, piece in enumerate(chunks):
            assert tokens[i].get("text") == piece, (
                f"training stream ok: token[{i}] expected {piece!r}, got {tokens[i]!r}"
            )
        joined = "".join(str(t.get("text", "")) for t in tokens)

        detail = httpx.get(
            f"{env.ops_url}/api/v1/training/sessions/{sid}",
            headers=h,
            timeout=30.0,
        )
        assert detail.status_code == 200, (
            f"GET training session expected 200, got {detail.status_code}: {detail.text[:300]}"
        )
        messages = detail.json().get("messages") or []
        assert messages, f"training stream ok: session messages must be non-empty: {detail.json()}"
        buyer_msgs = [
            m for m in messages
            if isinstance(m, dict) and m.get("role") in ("buyer", "assistant")
        ]
        last_buyer = buyer_msgs[-1] if buyer_msgs else messages[-1]
        assert isinstance(last_buyer, dict), f"last message must be object: {last_buyer!r}"
        assert last_buyer.get("content") == joined, (
            f"training stream ok: last buyer message content must equal token concat; "
            f"content={last_buyer.get('content')!r} joined={joined!r}; messages={messages!r}"
        )

        assert llm.requests, "training stream ok: fake LLM must record the request"
        last = llm.requests[-1]
        assert last.get("stream") is True, (
            f"training stream ok: stream must be true, got {last.get('stream')!r}"
        )
        self._assert_fenced(self._prompt_blob(last), agent_msg, ctx="training stream ok prompt")

    def test_training_stream_midway_fail_reset_persists_suffix(self, env, llm: _FakeLlmControl):
        """对练中途断开：出现 reset token；落库买家消息 == reset 之后 token 拼接。"""
        chunks = ["前", "半", "后", "续"]
        llm.reset(mode="disconnect_after_n", disconnect_after=2, chunks=chunks)
        h = env.token("cs_agent", "pps-train-mid", "tenant-a")
        created = httpx.post(
            f"{env.ops_url}/api/v1/training/sessions",
            headers=h,
            json={},
            timeout=30.0,
        )
        assert created.status_code == 201, (
            f"create training session expected 201, got {created.status_code}: {created.text[:300]}"
        )
        sid = created.json()["session_id"]

        resp, events = self._chat_stream(env, h, sid, "请提供订单号")
        assert resp.status_code == 200, f"training mid-fail: expected 200, got {resp.status_code}"
        tokens = self._token_payloads(events)
        reset_idxs = [i for i, t in enumerate(tokens) if t.get("reset") is True]
        assert reset_idxs, (
            f"training mid-fail: expected reset token, got tokens={tokens}; events={events}"
        )
        after = tokens[reset_idxs[0] + 1 :]
        joined_after = "".join(str(t.get("text", "")) for t in after)

        detail = httpx.get(
            f"{env.ops_url}/api/v1/training/sessions/{sid}",
            headers=h,
            timeout=30.0,
        )
        assert detail.status_code == 200, (
            f"GET training session expected 200, got {detail.status_code}: {detail.text[:300]}"
        )
        messages = detail.json().get("messages") or []
        assert messages, f"training mid-fail: messages must be non-empty: {detail.json()}"
        buyer_msgs = [
            m for m in messages
            if isinstance(m, dict) and m.get("role") in ("buyer", "assistant")
        ]
        last_buyer = buyer_msgs[-1] if buyer_msgs else messages[-1]
        assert last_buyer.get("content") == joined_after, (
            f"training mid-fail: persisted buyer content must equal texts after reset; "
            f"content={last_buyer.get('content')!r} after={joined_after!r}; messages={messages!r}"
        )
