"""kb_qa 验收：检索命中/入队、发布状态机、AC-KB-01、故障与租户隔离、会话摘要。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
import psycopg

from ecomops.core.db import conninfo

_ACC = Path(__file__).resolve().parent
if str(_ACC) not in sys.path:
    sys.path.insert(0, str(_ACC))

pytest_plugins = ["conftest_kb"]

HEX64 = re.compile(r"^[0-9a-f]{64}$")

HIT_Q = "热水器漏水怎么退换"
MISS_Q = "我想了解你们对跨境海淘税费的处理细则"


def _publish_body(question: str, rule_source: str = "sandbox_demo_publish") -> dict:
    """standard_answer 嵌入原问题关键词，确保发布后同问可高分命中。"""
    return {
        "standard_answer": (
            f"【标准答复】{question}：按沙盒演示规则处理，具体以平台公示税费细则为准，"
            f"质量问题 7 天内可退换。"
        ),
        "catalog_category_id": "HOME_APPLIANCE_HEATER",
        "rule_source": rule_source,
        "effective_from": 0,
        "effective_to": 4102444800,
    }


def _ask(env, headers: dict, query: str, session_id: str | None = None) -> dict:
    payload: dict = {"query": query}
    if session_id is not None:
        payload["session_id"] = session_id
    r = httpx.post(f"{env.ops_url}/api/v1/kb/ask", headers=headers, json=payload, timeout=60.0)
    assert r.status_code == 200, f"ask 期望 200，得 {r.status_code}: {r.text}"
    return r.json()


def _pending(env, headers: dict, status: str | None = None) -> list[dict]:
    params = {"status": status} if status else None
    r = httpx.get(f"{env.ops_url}/api/v1/kb/pending", headers=headers, params=params, timeout=30.0)
    assert r.status_code == 200, f"pending 列表失败 {r.status_code}: {r.text}"
    data = r.json()
    assert "items" in data, f"pending 响应缺 items: {data}"
    return data["items"]


def _pending_by_q(items: list[dict], question: str) -> list[dict]:
    return [i for i in items if i.get("question_text") == question]


def _publish(env, headers: dict, queue_id: str, body: dict) -> httpx.Response:
    return httpx.post(
        f"{env.ops_url}/api/v1/kb/pending/{queue_id}/publish",
        headers=headers,
        json=body,
        timeout=60.0,
    )


def _retry(env, headers: dict, queue_id: str) -> httpx.Response:
    return httpx.post(
        f"{env.ops_url}/api/v1/kb/pending/{queue_id}/retry",
        headers=headers,
        timeout=60.0,
    )


def _discard(env, headers: dict, queue_id: str) -> httpx.Response:
    return httpx.post(
        f"{env.ops_url}/api/v1/kb/pending/{queue_id}/discard",
        headers=headers,
        timeout=30.0,
    )


def _err_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("error", {}).get("code")
    except Exception:
        return None


def test_high_score_hit_no_enqueue(kb_env):
    h = kb_env.token("operator", "kb-op-1", "tenant-a")
    data = _ask(kb_env, h, HIT_Q)
    assert data.get("query_type") == "PRECISE", f"期望 PRECISE，得 {data.get('query_type')}: {data}"
    assert data.get("top1_score", 0) >= 0.75, f"沙箱演示规则应高分命中 top1_score>=0.75，得 {data.get('top1_score')}"
    assert isinstance(data.get("answer"), str) and data["answer"].strip(), f"高分命中答案应非空: {data}"
    assert data.get("enqueued_queue_id") in (None, ""), f"高分命中不应入队，enqueued_queue_id={data.get('enqueued_queue_id')}"
    items = _pending_by_q(_pending(kb_env, h), HIT_Q)
    assert items == [], f"高分命中后 pending 不应出现该问题，得 {items}"


def test_low_score_enqueue_dedup(kb_env):
    h = kb_env.token("operator", "kb-op-2", "tenant-a")
    first = _ask(kb_env, h, MISS_Q)
    assert first.get("top1_score", 1) < 0.75, f"未知问题应低分 top1_score<0.75，得 {first.get('top1_score')}"
    qid = first.get("enqueued_queue_id")
    assert qid, f"低分应入队，enqueued_queue_id 非空，得 {first}"

    second = _ask(kb_env, h, MISS_Q)
    assert second.get("enqueued_queue_id") == qid, (
        f"同问题再问应返回同一 queue_id，第一次={qid} 第二次={second.get('enqueued_queue_id')}"
    )
    items = _pending_by_q(_pending(kb_env, h), MISS_Q)
    assert len(items) == 1, f"pending 同问题仅 1 条，得 {len(items)}: {items}"


def test_publish_success_then_high_hit(kb_env):
    h = kb_env.token("supervisor", "kb-sup-1", "tenant-a")
    ask = _ask(kb_env, h, MISS_Q)
    qid = ask["enqueued_queue_id"]
    assert qid, f"需先入队: {ask}"

    resp = _publish(kb_env, h, qid, _publish_body(MISS_Q))
    assert resp.status_code == 200, f"publish 失败 {resp.status_code}: {resp.text}"
    item = resp.json()
    assert item.get("publish_status") == "PUBLISHED", f"发布成功应为 PUBLISHED，得 {item}"
    chunk_id = item.get("deterministic_chunk_id")
    assert isinstance(chunk_id, str) and HEX64.match(chunk_id), (
        f"deterministic_chunk_id 应为 64 位十六进制，得 {chunk_id!r}"
    )
    assert kb_env.rag_chunk_count(chunk_id) == 1, f"RAG 桩应有该 chunk count==1，chunk_id={chunk_id}"

    again = _ask(kb_env, h, MISS_Q)
    assert again.get("top1_score", 0) >= 0.75, f"发布后同问题应高分命中，得 {again.get('top1_score')}: {again}"
    assert again.get("enqueued_queue_id") in (None, ""), f"发布后不应再入队: {again}"


def test_ac_kb_01_writeback_fail_then_retry(kb_env):
    """AC-KB-01：回写失败停留 PUBLISHING，retry 成功且向量不重复。"""
    h = kb_env.token("supervisor", "kb-sup-ac01", "tenant-a")
    q = "关于跨境海淘税费补录条目 AC-KB-01 专用问题"
    ask = _ask(kb_env, h, q)
    qid = ask.get("enqueued_queue_id")
    assert qid, f"AC-KB-01 需先建待补录: {ask}"

    before_total = kb_env.rag_total_chunks()
    kb_env.faults({"publish_writeback_fail": True})
    resp = _publish(kb_env, h, qid, _publish_body(q, "ac_kb_01"))
    assert resp.status_code == 200, f"writeback 故障下 publish 仍应 200: {resp.status_code} {resp.text}"
    item = resp.json()
    assert item.get("publish_status") == "PUBLISHING", (
        f"回写失败应停留 PUBLISHING，得 {item.get('publish_status')}: {item}"
    )
    chunk_id = item.get("deterministic_chunk_id")
    version = item.get("version")
    assert chunk_id and HEX64.match(str(chunk_id)), f"PUBLISHING 时应已有 deterministic_chunk_id: {item}"
    assert kb_env.rag_chunk_count(str(chunk_id)) == 1, (
        f"回写失败时 RAG 桩应已写入 chunk count==1，chunk_id={chunk_id}"
    )

    kb_env.faults({})
    retry_resp = _retry(kb_env, h, qid)
    assert retry_resp.status_code == 200, f"retry 失败 {retry_resp.status_code}: {retry_resp.text}"
    final = retry_resp.json()
    assert final.get("publish_status") == "PUBLISHED", f"retry 后应为 PUBLISHED，得 {final}"
    assert final.get("deterministic_chunk_id") == chunk_id, (
        f"retry 前后 deterministic_chunk_id 应不变: before={chunk_id} after={final.get('deterministic_chunk_id')}"
    )
    assert final.get("version") == version, (
        f"retry 不递增 version: before={version} after={final.get('version')}"
    )
    assert kb_env.rag_chunk_count(str(chunk_id)) == 1, f"retry 后按 chunk_id 查询 count 仍应==1"
    after_total = kb_env.rag_total_chunks()
    assert after_total == before_total + 1, (
        f"total 比发布前只应 +1: before={before_total} after={after_total}"
    )


def test_rag_fail_before_write_then_retry(kb_env):
    h = kb_env.token("supervisor", "kb-sup-fbw", "tenant-a")
    q = "关于进口商品消费税细则的补录问题 fail_before_write"
    ask = _ask(kb_env, h, q)
    qid = ask.get("enqueued_queue_id")
    assert qid, f"需入队: {ask}"

    kb_env.rag_fault("fail_before_write")
    resp = _publish(kb_env, h, qid, _publish_body(q, "fail_before_write"))
    assert resp.status_code == 200, f"RAG fail_before_write 时 publish 应 200: {resp.status_code} {resp.text}"
    item = resp.json()
    assert item.get("publish_status") == "PUBLISH_FAILED", (
        f"fail_before_write 应为 PUBLISH_FAILED，得 {item.get('publish_status')}: {item}"
    )
    assert item.get("last_error"), f"PUBLISH_FAILED 时 last_error 应非空: {item}"

    kb_env.rag_fault("none")
    retry_resp = _retry(kb_env, h, qid)
    assert retry_resp.status_code == 200, f"清除故障后 retry 失败: {retry_resp.status_code} {retry_resp.text}"
    final = retry_resp.json()
    assert final.get("publish_status") == "PUBLISHED", f"retry 后应为 PUBLISHED，得 {final}"


def test_publish_state_conflict_and_tenant_isolation(kb_env):
    h_a = kb_env.token("supervisor", "kb-sup-a", "tenant-a")
    h_b = kb_env.token("supervisor", "kb-sup-b", "tenant-b")

    # 发布成功后再 publish → 409
    q_pub = "状态冲突测试：已发布条目不可再 publish"
    ask = _ask(kb_env, h_a, q_pub)
    qid_pub = ask["enqueued_queue_id"]
    pub = _publish(kb_env, h_a, qid_pub, _publish_body(q_pub, "conflict_pub"))
    assert pub.status_code == 200 and pub.json().get("publish_status") == "PUBLISHED", pub.text
    conflict = _publish(kb_env, h_a, qid_pub, _publish_body(q_pub, "conflict_pub_again"))
    assert conflict.status_code == 409, f"已 PUBLISHED 再 publish 应 409，得 {conflict.status_code}: {conflict.text}"
    assert _err_code(conflict) == "PUBLISH_STATE_CONFLICT", (
        f"错误码应为 PUBLISH_STATE_CONFLICT，得 {_err_code(conflict)}: {conflict.text}"
    )

    # discard → DISCARDED；再 discard → 409
    q_disc = "状态冲突测试：discard 条目"
    ask2 = _ask(kb_env, h_a, q_disc)
    qid_disc = ask2["enqueued_queue_id"]
    d1 = _discard(kb_env, h_a, qid_disc)
    assert d1.status_code == 200, f"discard 失败: {d1.status_code} {d1.text}"
    assert d1.json().get("publish_status") == "DISCARDED", f"discard 后应为 DISCARDED: {d1.json()}"
    d2 = _discard(kb_env, h_a, qid_disc)
    assert d2.status_code == 409, f"已 DISCARDED 再 discard 应 409，得 {d2.status_code}: {d2.text}"
    assert _err_code(d2) == "PUBLISH_STATE_CONFLICT", (
        f"再 discard 错误码应为 PUBLISH_STATE_CONFLICT，得 {_err_code(d2)}: {d2.text}"
    )

    # 他租户 404
    q_iso = "租户隔离测试问题他租户不可见"
    ask3 = _ask(kb_env, h_a, q_iso)
    qid_iso = ask3["enqueued_queue_id"]
    other = _discard(kb_env, h_b, qid_iso)
    assert other.status_code == 404, f"他租户 discard 应 404，得 {other.status_code}: {other.text}"
    assert _err_code(other) == "NOT_FOUND", f"他租户错误码应为 NOT_FOUND，得 {_err_code(other)}: {other.text}"
    other_pub = _publish(kb_env, h_b, qid_iso, _publish_body(q_iso, "tenant_iso"))
    assert other_pub.status_code == 404, f"他租户 publish 应 404，得 {other_pub.status_code}: {other_pub.text}"


def test_rag_down_ask_fallback_enqueue(kb_env):
    h = kb_env.token("operator", "kb-op-ragdown", "tenant-a")
    kb_env.rag.stop(sig_kill=True)
    q = "RAG 宕机时的兜底入队问题热水器保修多久"
    r = httpx.post(
        f"{kb_env.ops_url}/api/v1/kb/ask",
        headers=h,
        json={"query": q},
        timeout=60.0,
    )
    assert r.status_code == 200, f"RAG 不可用时 ask 应 200 兜底，不应 5xx，得 {r.status_code}: {r.text}"
    assert r.status_code < 500, f"不应出现 5xx: {r.status_code}"
    data = r.json()
    assert isinstance(data.get("answer"), str) and data["answer"].strip(), f"应有兜底答案: {data}"
    assert data.get("top1_score") == 0, f"RAG 不可用 top1_score 应为 0，得 {data.get('top1_score')}"
    assert data.get("enqueued_queue_id"), f"RAG 不可用仍应入队: {data}"


def test_qa_session_summary_at_20_messages(kb_env):
    h = kb_env.token("operator", "kb-op-sum", "tenant-a")
    session_id = "qa-session-summary-001"

    for i in range(9):
        _ask(kb_env, h, f"第{i+1}轮摘要测试询问保修政策吗", session_id=session_id)

    with psycopg.connect(conninfo(kb_env.db_name)) as conn:
        row = conn.execute(
            "SELECT messages, summary FROM qa_sessions WHERE session_id = %s AND tenant_id = %s",
            (session_id, "tenant-a"),
        ).fetchone()
    assert row is not None, "第 9 轮后 qa_sessions 应有记录"
    messages, summary = row[0], row[1]
    if isinstance(messages, str):
        messages = json.loads(messages)
    assert len(messages) == 18, f"第 9 轮后 messages 长度应为 18，得 {len(messages)}"
    assert not (summary or "").strip(), f"第 9 轮后 summary 应仍为空，得 {summary!r}"

    _ask(kb_env, h, "第10轮摘要测试询问退换运费谁承担", session_id=session_id)

    with psycopg.connect(conninfo(kb_env.db_name)) as conn:
        row = conn.execute(
            "SELECT messages, summary FROM qa_sessions WHERE session_id = %s AND tenant_id = %s",
            (session_id, "tenant-a"),
        ).fetchone()
    assert row is not None, "第 10 轮后 qa_sessions 应有记录"
    messages, summary = row[0], row[1]
    if isinstance(messages, str):
        messages = json.loads(messages)
    assert len(messages) == 20, f"10 轮后 messages 长度应==20，得 {len(messages)}"
    assert isinstance(summary, str) and summary.strip(), f"10 轮后 summary 应非空，得 {summary!r}"
