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
    return {"rule_accuracy": score("policy_explain"), "emotional_soothing": score("empathy_listen"),
            "solution_compliance": score("solution_negotiate")}


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


async def create_session(identity: Identity, seed: dict | None) -> dict:
    state = dict(session_id=str(uuid4()), tenant_id=identity.tenant_id, user_id=identity.user_id,
                 scenario_seed=DEFAULT_SEED.copy() if seed is None else seed,
                 stage=STAGES[0], follow_up_count=0, messages=[], summary="", report=None, status="ACTIVE")
    async with get_engine().begin() as conn:
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
        return await load(conn, session_id, identity)


async def chat(session_id: str, identity: Identity, message: str) -> tuple[dict, str, bool]:
    # Serialize simultaneous turns so no message or transition can be lost.
    async with get_engine().begin() as conn:
        state = await load(conn, session_id, identity, lock=True)
        stage = state["stage"]
        if state["status"] != "ACTIVE" or stage not in KEYWORDS:
            raise ApiError(422, "INVALID_STAGE", "training session has ended or has an invalid stage")
        state["messages"].append(dict(role="user", content=message, stage=stage))
        advanced = evaluate_answer(stage, message) or state["follow_up_count"] >= 2
        if advanced:
            state["stage"] = STAGES[STAGES.index(stage) + 1]
            state["follow_up_count"] = 0
        else:
            state["follow_up_count"] += 1
        reply = buyer_reply(state["scenario_seed"], state["stage"], state["follow_up_count"])
        state["messages"].append(dict(role="assistant", content=reply, stage=state["stage"]))
        if len(state["messages"]) % 20 == 0:
            state["summary"] = summarize(state["messages"])
        if state["stage"] == "finished":
            state["status"] = "FINISHED"
            state["report"] = make_report(state["messages"])
        await save(conn, state)
    return state, reply, advanced


def reply_segments(reply: str):
    return re.findall(r"[^，。！？；\n]{1,16}[，。！？；\n]?|[，。！？；\n]", reply)
