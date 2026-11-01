"""LangGraph 工作流验收：kb 流式问答、listing 对练种子卡、training 从审查开课。

不依赖真实 LLM / 联网；RAG 走 conftest_kb 桩。
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import fitz
import httpx

_ACC = Path(__file__).resolve().parent
if str(_ACC) not in sys.path:
    sys.path.insert(0, str(_ACC))

pytest_plugins = ["conftest_kb"]

HIT_Q = "热水器漏水怎么退换"
MISS_Q = "我想了解你们对跨境海淘税费的处理细则"

CATEGORY = "HOME_APPLIANCE_HEATER"

BASE_PARAMS = """商品名称：速热电热水器
额定功率：2000W
容量：60L
能效等级：一级
3C认证编号：2024010112345678
产品卖点：安全节能，品质可靠
"""

EXTREME_PARAMS = BASE_PARAMS + "宣传语：全网最低价、国家级品质保障、第一品牌\n"

DUBIOUS_CERT_PARAMS = """商品名称：速热电热水器
额定功率：2000W
容量：60L
能效等级：一级
3C认证编号：ABC
产品卖点：安全节能，品质可靠
"""

DISPUTE_KINDS = frozenset({"blocking", "manual"})


def _err_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("error", {}).get("code")
    except Exception:
        return None


def _parse_sse(response: httpx.Response) -> list[tuple[str, Any]]:
    """解析 text/event-stream：event: / data: 对。"""
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
    """用 PyMuPDF 现场生成可抽取中文的 PDF 字节。"""
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


def _assert_scenario_seed(seed: Any, *, declared_category: str, ctx: str) -> dict:
    assert isinstance(seed, dict), f"{ctx}: scenario_seed 应为对象，得 {type(seed).__name__}: {seed!r}"
    for key in ("product_title", "declared_category", "buyer_persona", "disputes", "opening_message"):
        assert key in seed, f"{ctx}: scenario_seed 缺字段 {key}: {seed}"

    assert isinstance(seed["product_title"], str), (
        f"{ctx}: product_title 应为 str，得 {type(seed['product_title']).__name__}: {seed}"
    )
    assert seed["declared_category"] == declared_category, (
        f"{ctx}: declared_category 应为 {declared_category!r}，得 {seed['declared_category']!r}"
    )
    assert isinstance(seed["buyer_persona"], str) and seed["buyer_persona"].strip(), (
        f"{ctx}: buyer_persona 应为非空字符串，得 {seed['buyer_persona']!r}"
    )
    assert isinstance(seed["opening_message"], str) and seed["opening_message"].strip(), (
        f"{ctx}: opening_message 应为非空字符串，得 {seed['opening_message']!r}"
    )
    disputes = seed["disputes"]
    assert isinstance(disputes, list), f"{ctx}: disputes 应为数组，得 {type(disputes).__name__}: {seed}"
    assert len(disputes) <= 5, f"{ctx}: disputes 长度应 ≤5，得 {len(disputes)}: {disputes}"
    for i, item in enumerate(disputes):
        assert isinstance(item, dict), f"{ctx}: disputes[{i}] 应为对象，得 {item!r}"
        for k in ("point", "kind", "detail"):
            assert k in item, f"{ctx}: disputes[{i}] 缺 {k}: {item}"
        assert item["kind"] in DISPUTE_KINDS, (
            f"{ctx}: disputes[{i}].kind 只能是 blocking|manual，得 {item['kind']!r}"
        )
    return seed


def _ask(env, headers: dict, query: str, session_id: str | None = None) -> dict:
    payload: dict[str, Any] = {"query": query}
    if session_id is not None:
        payload["session_id"] = session_id
    r = httpx.post(f"{env.ops_url}/api/v1/kb/ask", headers=headers, json=payload, timeout=60.0)
    assert r.status_code == 200, f"ask 期望 200，得 {r.status_code}: {r.text}"
    return r.json()


def _ask_stream(
    env,
    headers: dict | None,
    query: str,
    *,
    session_id: str | None = None,
    catalog_category_id: str | None = None,
) -> tuple[httpx.Response, list[tuple[str, Any]]]:
    payload: dict[str, Any] = {"query": query}
    if session_id is not None:
        payload["session_id"] = session_id
    if catalog_category_id is not None:
        payload["catalog_category_id"] = catalog_category_id
    req_headers = headers or {}
    with httpx.stream(
        "POST",
        f"{env.ops_url}/api/v1/kb/ask/stream",
        headers=req_headers,
        json=payload,
        timeout=60.0,
    ) as resp:
        if resp.status_code != 200:
            resp.read()
            return resp, []
        events = _parse_sse(resp)
        return resp, events


def _split_ask_stream_events(
    events: list[tuple[str, Any]], *, ctx: str
) -> tuple[dict, list[dict], dict]:
    assert events, f"{ctx}: 应至少有一个 SSE 事件"
    names = [n for n, _ in events]
    assert names[0] == "meta", f"{ctx}: 第一个事件应为 meta，得 {names[0]!r}；events={events}"
    assert names[-1] == "done", f"{ctx}: 最后一个事件应为 done，得 {names[-1]!r}；events={events}"

    meta = events[0][1]
    done = events[-1][1]
    assert isinstance(meta, dict), f"{ctx}: meta.data 应为 JSON 对象，得 {meta!r}"
    assert isinstance(done, dict), f"{ctx}: done.data 应为 JSON 对象，得 {done!r}"

    tokens: list[dict] = []
    for name, payload in events[1:-1]:
        assert name == "token", (
            f"{ctx}: meta 与 done 之间只应有 token，得 event={name!r} payload={payload!r}"
        )
        assert isinstance(payload, dict), f"{ctx}: token.data 应为对象，得 {payload!r}"
        tokens.append(payload)

    assert tokens, f"{ctx}: 应至少有一个 token 事件，events={events}"
    return meta, tokens, done


def _assert_ask_stream_ok(
    resp: httpx.Response,
    events: list[tuple[str, Any]],
    *,
    ctx: str,
) -> tuple[dict, list[dict], dict]:
    assert resp.status_code == 200, f"{ctx}: 流式 ask 应 200，得 {resp.status_code}"
    ctype = (resp.headers.get("content-type") or "").lower()
    assert "text/event-stream" in ctype, (
        f"{ctx}: Content-Type 应为 text/event-stream，得 {ctype!r}"
    )
    meta, tokens, done = _split_ask_stream_events(events, ctx=ctx)

    for key in ("query_type", "top1_score", "hits", "session_id"):
        assert key in meta, f"{ctx}: meta 缺 {key}: {meta}"
    for key in ("answer", "fallback_used", "enqueued_queue_id"):
        assert key in done, f"{ctx}: done 缺 {key}: {done}"

    joined = "".join(str(t.get("text", "")) for t in tokens)
    for i, t in enumerate(tokens):
        assert "text" in t, f"{ctx}: token[{i}] 缺 text: {t}"
        assert isinstance(t["text"], str), f"{ctx}: token[{i}].text 应为 str，得 {t}"

    assert joined == done.get("answer"), (
        f"{ctx}: 所有 token.text 拼接应等于 done.answer；"
        f"joined={joined!r} answer={done.get('answer')!r}"
    )
    return meta, tokens, done


def _pending_items(env, headers: dict) -> list[dict]:
    r = httpx.get(f"{env.ops_url}/api/v1/kb/pending", headers=headers, timeout=30.0)
    assert r.status_code == 200, f"pending 列表失败 {r.status_code}: {r.text}"
    data = r.json()
    assert "items" in data, f"pending 响应缺 items: {data}"
    return data["items"]


def _upload_listing(env, headers: dict, pdf: bytes, *, declared_category: str = CATEGORY) -> httpx.Response:
    return httpx.post(
        f"{env.ops_url}/api/v1/listing/reviews",
        headers=headers,
        files={"file": ("listing.pdf", pdf, "application/pdf")},
        data={"declared_category": declared_category},
        timeout=60.0,
    )


def _get_listing(env, headers: dict, review_id: str) -> httpx.Response:
    return httpx.get(
        f"{env.ops_url}/api/v1/listing/reviews/{review_id}",
        headers=headers,
        timeout=30.0,
    )


def _create_training(env, headers: dict, body: dict | None = None) -> httpx.Response:
    return httpx.post(
        f"{env.ops_url}/api/v1/training/sessions",
        headers=headers,
        json=body if body is not None else {},
        timeout=30.0,
    )


def _get_training(env, headers: dict, session_id: str) -> httpx.Response:
    return httpx.get(
        f"{env.ops_url}/api/v1/training/sessions/{session_id}",
        headers=headers,
        timeout=30.0,
    )


def _chat_stream(
    env, headers: dict, session_id: str, message: str
) -> tuple[httpx.Response, list[tuple[str, Any]]]:
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


# ---------------------------------------------------------------------------
# 1. kb 流式问答
# ---------------------------------------------------------------------------


def test_kb_ask_stream_sse_shape_and_token_concat(kb_env):
    """SSE：meta → token+ → done，且 token 拼接等于 done.answer。"""
    h = kb_env.token("operator", "wf-kb-stream-1", "tenant-a")
    resp, events = _ask_stream(kb_env, h, HIT_Q)
    _assert_ask_stream_ok(resp, events, ctx="高分流式问答")


def test_kb_ask_stream_requires_auth(kb_env):
    """无令牌访问 ask/stream → 401。"""
    resp, _ = _ask_stream(kb_env, None, HIT_Q)
    assert resp.status_code == 401, f"无令牌应 401，得 {resp.status_code}"


def test_kb_ask_stream_cs_agent_allowed(kb_env):
    """cs_agent 也可调用 ask/stream。"""
    h = kb_env.token("cs_agent", "wf-kb-cs-1", "tenant-a")
    resp, events = _ask_stream(kb_env, h, HIT_Q)
    _assert_ask_stream_ok(resp, events, ctx="cs_agent 流式问答")


def test_kb_ask_stream_parity_and_high_hit(kb_env):
    """同问题：stream 与 ask 的 query_type/top1_score 一致；沙盒命中不入队。"""
    h = kb_env.token("operator", "wf-kb-parity", "tenant-a")
    sync = _ask(kb_env, h, HIT_Q)
    resp, events = _ask_stream(kb_env, h, HIT_Q)
    meta, _tokens, done = _assert_ask_stream_ok(resp, events, ctx="沙盒命中流式")

    assert meta.get("query_type") == sync.get("query_type"), (
        f"stream.query_type 应与 ask 相同：stream={meta.get('query_type')!r} "
        f"ask={sync.get('query_type')!r}"
    )
    assert meta.get("top1_score") == sync.get("top1_score"), (
        f"stream.top1_score 应与 ask 相同：stream={meta.get('top1_score')!r} "
        f"ask={sync.get('top1_score')!r}"
    )
    assert meta.get("top1_score", 0) >= 0.75, (
        f"命中沙盒规则 top1_score 应 >= 0.75，得 {meta.get('top1_score')}"
    )
    assert done.get("enqueued_queue_id") in (None, ""), (
        f"高分命中 done.enqueued_queue_id 应为空，得 {done.get('enqueued_queue_id')!r}"
    )


def test_kb_ask_stream_low_confidence_enqueues(kb_env):
    """低置信问题：done.enqueued_queue_id 非空，且 pending 可查到。"""
    h = kb_env.token("operator", "wf-kb-miss", "tenant-a")
    sync = _ask(kb_env, h, MISS_Q)
    resp, events = _ask_stream(kb_env, h, MISS_Q)
    meta, _tokens, done = _assert_ask_stream_ok(resp, events, ctx="低置信流式")

    assert meta.get("query_type") == sync.get("query_type"), (
        f"低置信 stream/ask query_type 应一致：stream={meta.get('query_type')!r} "
        f"ask={sync.get('query_type')!r}"
    )
    assert meta.get("top1_score") == sync.get("top1_score"), (
        f"低置信 stream/ask top1_score 应一致：stream={meta.get('top1_score')!r} "
        f"ask={sync.get('top1_score')!r}"
    )
    assert meta.get("top1_score", 1) < 0.75, (
        f"未知问题 top1_score 应 < 0.75，得 {meta.get('top1_score')}"
    )

    qid = done.get("enqueued_queue_id")
    assert qid, f"低置信 done.enqueued_queue_id 应非空，得 {done}"
    pending = _pending_items(kb_env, h)
    items = [i for i in pending if i.get("question_text") == MISS_Q]
    assert items, f"pending 应能查到问题 {MISS_Q!r}，items={pending}"
    assert any(i.get("queue_id") == qid for i in items), (
        f"pending 应含 enqueued_queue_id={qid!r}，得 {items}"
    )


def test_kb_ask_stream_session_reuse_and_foreign_user_404(kb_env):
    """同一 session_id 连续两次 meta.session_id 一致；他用户复用 → 404。"""
    h1 = kb_env.token("operator", "wf-kb-sess-owner", "tenant-a")
    h2 = kb_env.token("operator", "wf-kb-sess-other", "tenant-a")
    sid = "wf-kb-stream-session-001"

    resp1, ev1 = _ask_stream(kb_env, h1, HIT_Q, session_id=sid)
    meta1, _, _ = _assert_ask_stream_ok(resp1, ev1, ctx="会话第1轮")
    assert meta1.get("session_id") == sid, (
        f"第1轮 meta.session_id 应保持为 {sid!r}，得 {meta1.get('session_id')!r}"
    )

    resp2, ev2 = _ask_stream(kb_env, h1, "热水器保修多久", session_id=sid)
    meta2, _, _ = _assert_ask_stream_ok(resp2, ev2, ctx="会话第2轮")
    assert meta2.get("session_id") == sid, (
        f"第2轮 meta.session_id 应仍为 {sid!r}，得 {meta2.get('session_id')!r}"
    )

    resp3, _ = _ask_stream(kb_env, h2, HIT_Q, session_id=sid)
    assert resp3.status_code == 404, (
        f"他用户使用他人 session_id 应 404，得 {resp3.status_code}"
    )


# ---------------------------------------------------------------------------
# 2. listing 合规审查 → scenario_seed
# ---------------------------------------------------------------------------


def test_listing_scenario_seed_blocking_extreme_words(kb_env):
    """含极限词 → REJECTED_BLOCKING，scenario_seed.disputes 含 blocking。"""
    h = kb_env.token("operator", "wf-list-block", "tenant-a")
    created = _upload_listing(kb_env, h, _pdf_bytes(EXTREME_PARAMS))
    assert created.status_code == 201, f"上传应 201，得 {created.status_code}: {created.text}"
    review_id = created.json().get("review_id")
    assert review_id, f"创建响应缺 review_id: {created.text}"

    detail = _get_listing(kb_env, h, review_id)
    assert detail.status_code == 200, f"详情应 200，得 {detail.status_code}: {detail.text}"
    data = detail.json()
    assert data.get("verdict") == "REJECTED_BLOCKING", (
        f"含「全网最低价/国家级/第一品牌」应为 REJECTED_BLOCKING，得 {data.get('verdict')!r}: {data}"
    )
    seed = _assert_scenario_seed(data.get("scenario_seed"), declared_category=CATEGORY, ctx="阻断种子卡")
    assert any(d.get("kind") == "blocking" for d in seed["disputes"]), (
        f"阻断结论下 disputes 至少一项 kind==blocking，得 {seed['disputes']}"
    )


def test_listing_scenario_seed_passed_or_manual_complete(kb_env):
    """PENDING_MANUAL_VERIFY / PASSED_WITH_SUGGESTIONS 仍有完整 scenario_seed；通过无争议时 disputes==[]。"""
    h = kb_env.token("operator", "wf-list-pass", "tenant-a")

    # 合规通过样例（与 test_listing.test_passed_with_suggestions 相同文案）
    created = _upload_listing(kb_env, h, _pdf_bytes(BASE_PARAMS))
    assert created.status_code == 201, f"上传应 201，得 {created.status_code}: {created.text}"
    review_id = created.json()["review_id"]
    detail = _get_listing(kb_env, h, review_id)
    assert detail.status_code == 200, f"详情应 200: {detail.status_code} {detail.text}"
    data = detail.json()
    assert data.get("verdict") == "PASSED_WITH_SUGGESTIONS", (
        f"参数齐全无违规应为 PASSED_WITH_SUGGESTIONS，得 {data.get('verdict')!r}: {data}"
    )
    seed = _assert_scenario_seed(data.get("scenario_seed"), declared_category=CATEGORY, ctx="通过种子卡")
    assert seed["disputes"] == [], (
        f"通过且无争议点时 disputes 应为 []，得 {seed['disputes']}"
    )

    # 证书存疑样例：结论 PENDING_MANUAL_VERIFY，种子卡字段仍齐全
    created2 = _upload_listing(kb_env, h, _pdf_bytes(DUBIOUS_CERT_PARAMS))
    assert created2.status_code == 201, f"存疑上传应 201，得 {created2.status_code}: {created2.text}"
    rid2 = created2.json()["review_id"]
    detail2 = _get_listing(kb_env, h, rid2)
    assert detail2.status_code == 200, detail2.text
    data2 = detail2.json()
    assert data2.get("verdict") == "PENDING_MANUAL_VERIFY", (
        f"证书格式存疑应为 PENDING_MANUAL_VERIFY，得 {data2.get('verdict')!r}: {data2}"
    )
    _assert_scenario_seed(data2.get("scenario_seed"), declared_category=CATEGORY, ctx="待核实种子卡")


# ---------------------------------------------------------------------------
# 3. training 用种子卡开课
# ---------------------------------------------------------------------------


def _blocking_review_id(env, headers: dict) -> tuple[str, dict]:
    created = _upload_listing(env, headers, _pdf_bytes(EXTREME_PARAMS))
    assert created.status_code == 201, f"阻断上传应 201: {created.status_code} {created.text}"
    review_id = created.json()["review_id"]
    detail = _get_listing(env, headers, review_id)
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data.get("verdict") == "REJECTED_BLOCKING", data
    seed = _assert_scenario_seed(data.get("scenario_seed"), declared_category=CATEGORY, ctx="开课用阻断审查")
    return review_id, seed


def test_training_create_from_listing_review_seed(kb_env):
    """from_listing_review_id=阻断审查 → 201，会话 scenario_seed 与报告一致。"""
    h_list = kb_env.token("operator", "wf-train-list-op", "tenant-a")
    h_train = kb_env.token("cs_agent", "wf-train-agent-1", "tenant-a")
    review_id, listing_seed = _blocking_review_id(kb_env, h_list)

    resp = _create_training(kb_env, h_train, {"from_listing_review_id": review_id})
    assert resp.status_code == 201, f"用审查开课应 201，得 {resp.status_code}: {resp.text}"
    body = resp.json()
    sid = body.get("session_id")
    assert sid, f"创建响应缺 session_id: {body}"

    detail = _get_training(kb_env, h_train, sid)
    assert detail.status_code == 200, f"GET 会话应 200: {detail.status_code} {detail.text}"
    sess = detail.json()
    assert sess.get("scenario_seed") == listing_seed, (
        f"会话 scenario_seed 应等于审查报告 scenario_seed；"
        f"session={sess.get('scenario_seed')!r} listing={listing_seed!r}"
    )


def test_training_from_listing_review_unknown_uuid_404(kb_env):
    """不存在的审查 UUID → 404。"""
    h = kb_env.token("cs_agent", "wf-train-missing", "tenant-a")
    missing = str(uuid.uuid4())
    resp = _create_training(kb_env, h, {"from_listing_review_id": missing})
    assert resp.status_code == 404, (
        f"不存在的 from_listing_review_id 应 404，得 {resp.status_code}: {resp.text}"
    )


def test_training_from_listing_review_other_tenant_404(kb_env):
    """他租户审查 id → 404。"""
    h_a = kb_env.token("operator", "wf-train-tenant-a", "tenant-a")
    h_b = kb_env.token("cs_agent", "wf-train-tenant-b", "tenant-b")
    review_id, _ = _blocking_review_id(kb_env, h_a)

    resp = _create_training(kb_env, h_b, {"from_listing_review_id": review_id})
    assert resp.status_code == 404, (
        f"他租户审查 id 开课应 404，得 {resp.status_code}: {resp.text}"
    )


def test_training_from_listing_and_scenario_seed_conflict_400(kb_env):
    """同时给 from_listing_review_id 与 scenario_seed → 400 INVALID_REQUEST。"""
    h_list = kb_env.token("operator", "wf-train-conflict-op", "tenant-a")
    h_train = kb_env.token("cs_agent", "wf-train-conflict", "tenant-a")
    review_id, listing_seed = _blocking_review_id(kb_env, h_list)

    resp = _create_training(
        kb_env,
        h_train,
        {"from_listing_review_id": review_id, "scenario_seed": listing_seed},
    )
    assert resp.status_code == 400, (
        f"两字段同时给出应 400，得 {resp.status_code}: {resp.text}"
    )
    assert _err_code(resp) == "INVALID_REQUEST", (
        f"错误码应为 INVALID_REQUEST，得 {_err_code(resp)}: {resp.text}"
    )


def test_training_create_without_seed_fields_still_201(kb_env):
    """不带 from_listing_review_id / scenario_seed → 仍 201（内置种子）。"""
    h = kb_env.token("cs_agent", "wf-train-default", "tenant-a")
    resp = _create_training(kb_env, h, {})
    assert resp.status_code == 201, f"空体创建应仍 201，得 {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("session_id"), f"缺 session_id: {data}"
    assert data.get("stage") == "verify_order", f"初始 stage 应为 verify_order，得 {data.get('stage')}"


def test_training_from_listing_seed_can_chat_stream(kb_env):
    """用审查种子开的会话可走 chat/stream，收到 SSE（含 stage，并以 done 收尾约定为准）。"""
    h_list = kb_env.token("operator", "wf-train-chat-op", "tenant-a")
    h_train = kb_env.token("cs_agent", "wf-train-chat", "tenant-a")
    review_id, _ = _blocking_review_id(kb_env, h_list)

    created = _create_training(kb_env, h_train, {"from_listing_review_id": review_id})
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]

    msg = "您好，请问您的订单号和手机尾号是多少？我帮您核实订单"
    resp, events = _chat_stream(kb_env, h_train, sid, msg)
    assert resp.status_code == 200, f"chat/stream 应 200，得 {resp.status_code}"
    ctype = (resp.headers.get("content-type") or "").lower()
    assert "text/event-stream" in ctype, f"Content-Type 应为 text/event-stream，得 {ctype!r}"
    assert events, f"应收到 SSE 事件，events={events}"

    stage_events = [p for n, p in events if n == "stage" and isinstance(p, dict)]
    assert stage_events, f"应有 stage 事件（同 test_training），events={events}"
    names = [n for n, _ in events]
    assert "done" in names, f"事件序列应含 done（同 test_training），得 {names}"
