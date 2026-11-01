"""cs_training 验收：四阶段合格流、追问满 2 次推进、结束态、租户/用户隔离、摘要。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import psycopg

from ecomops.core.db import conninfo

_ACC = Path(__file__).resolve().parent
if str(_ACC) not in sys.path:
    sys.path.insert(0, str(_ACC))

pytest_plugins = ["conftest_kb"]

STAGE_ANSWERS = [
    "您好，请问您的订单号和手机尾号是多少？我帮您核实订单",
    "非常抱歉让您着急了，我理解您的心情",
    "根据规则，质量问题 7 天内可以退换，运费由我们承担",
    "我们为您办理退款，也可以补发或换货，您看哪个方案合适",
]

# 合格回答推进后进入的下一阶段（含终态）
ADVANCED_STAGE_SEQ = [
    "empathy_listen",
    "policy_explain",
    "solution_negotiate",
    "finished",
]
PIPELINE = ["verify_order", *ADVANCED_STAGE_SEQ]


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


def _create_session(env, headers: dict) -> dict:
    r = httpx.post(f"{env.ops_url}/api/v1/training/sessions", headers=headers, json={}, timeout=30.0)
    assert r.status_code == 201, f"创建会话应 201，得 {r.status_code}: {r.text}"
    data = r.json()
    assert data.get("session_id"), f"创建响应缺 session_id: {data}"
    assert data.get("stage") == "verify_order", f"初始 stage 应为 verify_order，得 {data.get('stage')}"
    return data


def _chat_stream(env, headers: dict, session_id: str, message: str) -> tuple[httpx.Response, list[tuple[str, Any]]]:
    with httpx.stream(
        "POST",
        f"{env.ops_url}/api/v1/training/sessions/{session_id}/chat/stream",
        headers=headers,
        json={"message": message},
        timeout=60.0,
    ) as resp:
        # 非 SSE 错误（如 422/404）需读 body
        if resp.status_code != 200:
            body = resp.read()
            # 构造可检查的假 Response 属性
            resp._error_body = body  # type: ignore[attr-defined]
            return resp, []
        events = _parse_sse(resp)
        return resp, events


def _stage_events(events: list[tuple[str, Any]]) -> list[dict]:
    out = []
    for name, payload in events:
        if name == "stage" and isinstance(payload, dict):
            out.append(payload)
    return out


def _score_ints(report: dict) -> list[int]:
    vals = [v for v in report.values() if isinstance(v, int) and not isinstance(v, bool)]
    return vals


def test_happy_path_four_stages_and_report(kb_env):
    h = kb_env.token("cs_agent", "train-agent-1", "tenant-a")
    sess = _create_session(kb_env, h)
    sid = sess["session_id"]

    advanced_stages: list[str] = []
    report_payload: dict | None = None
    got_done = False

    for i, msg in enumerate(STAGE_ANSWERS):
        resp, events = _chat_stream(kb_env, h, sid, msg)
        assert resp.status_code == 200, f"第{i+1}阶段 SSE 应 200，得 {resp.status_code}"
        stages = _stage_events(events)
        assert stages, f"第{i+1}阶段应有 stage 事件，events={events}"
        for st in stages:
            if st.get("advanced"):
                advanced_stages.append(str(st.get("stage", "")))
        for n, p in events:
            if n == "report" and isinstance(p, dict):
                report_payload = p
            if n == "done":
                got_done = True

    assert len(advanced_stages) >= 4, f"四次合格回答应至少 4 次推进，得 {advanced_stages}"
    # 兼容两种语义：stage=推进后的新阶段，或 stage=刚完成的阶段名
    if advanced_stages[:4] == ADVANCED_STAGE_SEQ:
        pass
    elif advanced_stages[:4] == PIPELINE[:4]:
        pass
    else:
        # 有序子序列：沿 PIPELINE 单调前进且覆盖到 finished
        idxs = [PIPELINE.index(s) for s in advanced_stages if s in PIPELINE]
        assert idxs == sorted(idxs) and idxs, f"stage 应依次推进，得 {advanced_stages}"
        assert PIPELINE[-1] in advanced_stages or advanced_stages[-1] in (
            "finished",
            "solution_negotiate",
        ), f"最终应推进到 finished（或完成 solution_negotiate），得 {advanced_stages}"

    assert report_payload is not None, "最终应收到 report 事件"
    scores = _score_ints(report_payload)
    assert len(scores) >= 3, f"report 应含至少三项整数分数，得 {report_payload}"
    assert all(0 <= s <= 100 for s in scores), f"分数应为 0~100 整数，得 {report_payload}"
    assert got_done, "最终应收到 done 事件"

    detail = httpx.get(f"{kb_env.ops_url}/api/v1/training/sessions/{sid}", headers=h, timeout=30.0)
    assert detail.status_code == 200, f"GET 会话失败: {detail.status_code} {detail.text}"
    body = detail.json()
    assert body.get("status") == "FINISHED", f"会话 status 应为 FINISHED，得 {body}"


def test_follow_up_keeps_stage_until_third(kb_env):
    h = kb_env.token("cs_agent", "train-agent-2", "tenant-a")
    sid = _create_session(kb_env, h)["session_id"]
    off = "今天天气怎么样哈哈哈"

    # 第 1 次答非所问：保持 verify_order，follow_up_count 递增
    resp1, ev1 = _chat_stream(kb_env, h, sid, off)
    assert resp1.status_code == 200, resp1.status_code
    st1 = _stage_events(ev1)
    assert st1, f"应有 stage 事件: {ev1}"
    last1 = st1[-1]
    assert last1.get("stage") == "verify_order", f"第1次应保持 verify_order，得 {last1}"
    assert last1.get("advanced") in (False, None), f"第1次不应推进，得 {last1}"
    assert last1.get("follow_up_count") == 1, f"第1次 follow_up_count 应为 1，得 {last1}"

    # 第 2 次：仍保持
    resp2, ev2 = _chat_stream(kb_env, h, sid, off)
    assert resp2.status_code == 200
    st2 = _stage_events(ev2)
    last2 = st2[-1]
    assert last2.get("stage") == "verify_order", f"第2次应保持 verify_order，得 {last2}"
    assert last2.get("advanced") in (False, None), f"第2次不应推进，得 {last2}"
    assert last2.get("follow_up_count") == 2, f"第2次 follow_up_count 应为 2，得 {last2}"

    # 第 3 次：追问满 2 次，推进
    resp3, ev3 = _chat_stream(kb_env, h, sid, off)
    assert resp3.status_code == 200
    st3 = _stage_events(ev3)
    assert st3, f"第3次应有 stage: {ev3}"
    advanced = [s for s in st3 if s.get("advanced")]
    assert advanced, f"第3次应推进，stage 事件={st3}"
    assert advanced[-1].get("stage") == "empathy_listen", (
        f"第3次应推进到 empathy_listen，得 {advanced[-1]}"
    )


def test_finished_then_invalid_stage(kb_env):
    h = kb_env.token("cs_agent", "train-agent-3", "tenant-a")
    sid = _create_session(kb_env, h)["session_id"]
    for msg in STAGE_ANSWERS:
        resp, _ = _chat_stream(kb_env, h, sid, msg)
        assert resp.status_code == 200, f"合格回答应 200: {resp.status_code}"

    # 已结束再发：422 INVALID_STAGE（可能是 JSON 而非 SSE）
    r = httpx.post(
        f"{kb_env.ops_url}/api/v1/training/sessions/{sid}/chat/stream",
        headers=h,
        json={"message": "还想再问一句"},
        timeout=30.0,
    )
    assert r.status_code == 422, f"已结束再发应 422，得 {r.status_code}: {r.text}"
    assert _err_code(r) == "INVALID_STAGE", f"错误码应为 INVALID_STAGE，得 {_err_code(r)}: {r.text}"


def test_other_user_session_404(kb_env):
    h1 = kb_env.token("cs_agent", "train-owner", "tenant-a")
    h2 = kb_env.token("cs_agent", "train-other", "tenant-a")
    sid = _create_session(kb_env, h1)["session_id"]

    r = httpx.get(f"{kb_env.ops_url}/api/v1/training/sessions/{sid}", headers=h2, timeout=30.0)
    assert r.status_code == 404, f"其他 user 访问应 404，得 {r.status_code}: {r.text}"
    assert _err_code(r) == "NOT_FOUND", f"错误码应为 NOT_FOUND，得 {_err_code(r)}: {r.text}"

    r2 = httpx.post(
        f"{kb_env.ops_url}/api/v1/training/sessions/{sid}/chat/stream",
        headers=h2,
        json={"message": "您好"},
        timeout=30.0,
    )
    assert r2.status_code == 404, f"其他 user 发消息应 404，得 {r2.status_code}: {r2.text}"


def test_training_summary_at_20_messages(kb_env):
    h = kb_env.token("cs_agent", "train-sum", "tenant-a")
    sid = _create_session(kb_env, h)["session_id"]
    off = "随便聊聊今天午餐吃什么"

    # 10 轮答非所问：每阶段最多追问 2 次后第 3 次推进，10 轮后 messages==20 且仍可能 ACTIVE
    for i in range(10):
        resp, events = _chat_stream(kb_env, h, sid, f"{off} #{i+1}")
        assert resp.status_code == 200, f"第{i+1}轮应 200，得 {resp.status_code}"
        assert events, f"第{i+1}轮应有 SSE 事件"

    with psycopg.connect(conninfo(kb_env.db_name)) as conn:
        row = conn.execute(
            "SELECT messages, summary FROM training_sessions WHERE session_id = %s::uuid",
            (sid,),
        ).fetchone()
    assert row is not None, "training_sessions 应有记录"
    messages, summary = row[0], row[1]
    if isinstance(messages, str):
        messages = json.loads(messages)
    assert len(messages) == 20, f"10 轮后 messages 长度应==20，得 {len(messages)}"
    assert isinstance(summary, str) and summary.strip(), f"20 条消息后 summary 应非空，得 {summary!r}"
