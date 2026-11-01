"""Deterministic buyer simulation and creator-scoped training state."""
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import text

from ecomops.core.auth import Identity
from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError

STAGES = ("verify_order", "empathy_listen", "policy_explain", "solution_negotiate", "finished")
KEYWORDS = {
    "verify_order": ("订单号", "手机尾号", "姓名", "核实"),
    "empathy_listen": ("抱歉", "理解", "着急"),
    "policy_explain": ("7天", "15天", "质量问题", "退换", "运费"),
    "solution_negotiate": ("退款", "换货", "补偿", "补发"),
}
DEFAULT_SEED = {"product": "热水器", "pain_point": "漏水"}


def evaluate_answer(stage: str, answer: str) -> bool:
    # Positive keyword matching deliberately accepts polite apologies.
    return any(word in answer for word in KEYWORDS.get(stage, ()))


def buyer_reply(seed: dict, stage: str, count: int) -> str:
    scenario = json.dumps(seed, ensure_ascii=False, sort_keys=True)
    prompts = {
        "verify_order": "先说清楚需要我提供什么订单信息。",
        "empathy_listen": "这件事让我很着急，你能理解我的感受吗？",
        "policy_explain": "请说清楚退换条款和运费由谁承担。",
        "solution_negotiate": "别再绕圈子，请给出具体处理方案。",
        "finished": "好，我会核对你承诺的处理方案，本次对练结束。",
    }
    pressure = ("请认真处理。", "我还没听到明确回答，请直接回应。", "这是第二次追问，请给我明确答复。")
    return f"我买的商品遇到这些问题：{scenario}。{prompts[stage]}{pressure[count]}"


def summarize(messages: list[dict]) -> str:
    return "客户对练摘要：" + "；".join(m["content"] for m in messages if m["role"] == "user")[:1000]


def make_report(messages: list[dict]) -> dict:
    answers = [m for m in messages if m["role"] == "user"]
    def score(stage):
        attempts = [m for m in answers if m["stage"] == stage]
        return max(0, 100 - 20 * (len(attempts) - 1)) if any(evaluate_answer(stage, m["content"]) for m in attempts) else 0
    mismatches = policy_summary(messages)["mismatches"]
    return {"rule_accuracy": max(0, score("policy_explain") - 20 * mismatches), "emotional_soothing": score("empathy_listen"),
            "solution_compliance": score("solution_negotiate")}


def policy_summary(messages: list[dict]) -> dict:
    """条款核对情况（会话查询接口返回；不放进 report，report 保持三项整数分）。"""
    answers = [m for m in messages if m["role"] == "user"]
    return {"checked": any(m.get("policy_checked", False) for m in answers),
            "mismatches": sum(bool(m.get("policy_mismatch")) for m in answers)}


async def save(conn, state: dict) -> None:
    params = {**state}
    for key in ("scenario_seed", "messages", "report"):
        params[key] = json.dumps(params[key], ensure_ascii=False)
    await conn.execute(text("""
        INSERT INTO training_sessions
          (session_id, tenant_id, user_id, scenario_seed, stage, follow_up_count, messages, summary, report, status)
        VALUES (:session_id, :tenant_id, :user_id, CAST(:scenario_seed AS jsonb), :stage,
                :follow_up_count, CAST(:messages AS jsonb), :summary, CAST(:report AS jsonb), :status)
        ON CONFLICT (session_id) DO UPDATE SET stage=EXCLUDED.stage,
          follow_up_count=EXCLUDED.follow_up_count, messages=EXCLUDED.messages,
          summary=EXCLUDED.summary, report=EXCLUDED.report, status=EXCLUDED.status, updated_at=now()
        WHERE training_sessions.tenant_id=EXCLUDED.tenant_id AND training_sessions.user_id=EXCLUDED.user_id
    """), params)


async def create_session(identity: Identity, seed: dict | None, from_listing_review_id: UUID | None = None) -> dict:
    if seed is not None and from_listing_review_id is not None:
        raise ApiError(400, "INVALID_REQUEST", "provide only one scenario source")
    state = dict(session_id=str(uuid4()), tenant_id=identity.tenant_id, user_id=identity.user_id,
                 scenario_seed=DEFAULT_SEED.copy() if seed is None else seed,
                 stage=STAGES[0], follow_up_count=0, messages=[], summary="", report=None, status="ACTIVE")
    async with get_engine().begin() as conn:
        if from_listing_review_id is not None:
            result = await conn.execute(text("SELECT scenario_seed FROM listing_reviews WHERE review_id=:review_id AND tenant_id=:tenant_id"),
                {"review_id": str(from_listing_review_id), "tenant_id": identity.tenant_id})
            seed = result.scalar_one_or_none()
            if not seed:
                raise ApiError(404, "NOT_FOUND", "scenario seed not found")
            state["scenario_seed"] = seed
        opening = state["scenario_seed"].get("opening_message")
        if isinstance(opening, str) and opening.strip():
            state["messages"] = [dict(role="assistant", content=opening, stage=STAGES[0])]
        await save(conn, state)
    return state


async def load(conn, session_id: str, identity: Identity, *, lock=False) -> dict:
    try:
        session_id = str(UUID(session_id))
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "training session not found") from None
    result = await conn.execute(text("""SELECT * FROM training_sessions
        WHERE session_id=:session_id AND tenant_id=:tenant_id AND user_id=:user_id"""
        + (" FOR UPDATE" if lock else "")),
        dict(session_id=session_id, tenant_id=identity.tenant_id, user_id=identity.user_id))
    row = result.mappings().first()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "training session not found")
    state = dict(row)
    state["session_id"] = str(state["session_id"])
    return state


async def get_session(session_id: str, identity: Identity) -> dict:
    async with get_engine().connect() as conn:
        state = await load(conn, session_id, identity)
    return {**state, "policy": policy_summary(state["messages"])}


async def chat(session_id: str, identity: Identity, message: str, graph=None) -> tuple[dict, str, bool]:
    from ecomops.training.graph import default_graph
    graph = graph if graph is not None else default_graph()
    async with get_engine().begin() as conn:
        result = await graph.ainvoke(
            dict(session_id=session_id, tenant_id=identity.tenant_id, user_id=identity.user_id, message=message),
            config={"configurable": {"thread_id": f"train:{identity.tenant_id}:{session_id}", "connection": conn}})
    return result["data"], result["reply"], result["advanced"]


def reply_segments(reply: str):
    return re.findall(r"[^，。！？；\n]{1,16}[，。！？；\n]?|[，。！？；\n]", reply)
