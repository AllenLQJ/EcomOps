"""三轨审核：默认纯规则。配置了真实 LLM 时，EVIDENCE 轨多一个"是否质量问题"的判断，但只能转人工，不能触发自动通过。
LLM 调用失败/输出不合规 → fallback_used（与故障注入同一降级路径）。"""
import asyncio
import re

from ecomops.core import llm

QUALITY_WORDS = ("漏水", "打不着火", "不制热", "坏了", "坏", "破损", "故障", "质量", "变质", "漏电", "不能用")
WINDOW_DAYS = 15


_SENTENCE = re.compile(r"[。；;\n]")
_DAYS = re.compile(r"(\d{1,3})\s*天")
CLAUSE_MIN_SCORE = 0.75


def clause_window_days(policy: list[dict]) -> tuple[int, str] | None:
    """从检索到的高置信条款里解析"质量问题 N 天内可退换"。只认明确讲质量问题的句子：
    "非质量问题""7 天无理由"这类句子不能缩短质量问题的退换期；解析不到就返回 None，沿用默认窗口。"""
    for clause in sorted(policy, key=lambda c: -float(c.get("score", 0))):
        if float(clause.get("score", 0)) < CLAUSE_MIN_SCORE:
            continue
        for sentence in _SENTENCE.split(str(clause.get("text", ""))):
            if ("质量" in sentence and "非质量" not in sentence and "无理由" not in sentence
                    and any(w in sentence for w in ("退换", "退货", "退款"))):
                match = _DAYS.search(sentence)
                if match and 1 <= int(match.group(1)) <= 90:
                    return int(match.group(1)), str(clause.get("rule_source", ""))
    return None


def _cited(policy: list[dict]) -> list[dict]:
    return [{"rule_source": c.get("rule_source"), "version": c.get("version", 0), "score": c.get("score"),
             "is_demo_rule": bool(c.get("is_demo_rule", False))} for c in policy[:3]]


def rule_track(t: dict, window: tuple[int, str] | None = None, policy: list[dict] | None = None) -> dict:
    days = window[0] if window else WINDOW_DAYS
    ok = int(t["days_since_delivery"]) <= days
    detail = {"window_days": days, "days_since_delivery": int(t["days_since_delivery"]),
              "window_source": "clause" if window else "default"}
    if window:
        detail["window_clause"] = window[1]
    if policy:
        detail["policy_clauses"] = _cited(policy)
    return {"track": "RULE", "passed": ok, "confidence": 1.0, "fallback_used": False, "detail": detail}


AUTO_APPROVE_CONFIDENCE = 0.85      # 与 route() 的自动通过门槛一致
LLM_CAP = 0.84                      # LLM 参与且与规则不一致/低置信时的置信上限：只能转人工，不能自动通过
LLM_MIN_CONFIDENCE = 0.6


def evidence_track(t: dict, judged: dict | None = None, llm_error: bool = False) -> dict:
    """judged: 真实 LLM 的判断（None=未配置）；llm_error: LLM 已配置但调用/解析失败。"""
    text = t.get("buyer_reason_text") or ""
    quality = any(w in text for w in QUALITY_WORDS)
    urls = t.get("evidence_urls") or []
    evidence_ok = len(urls) >= 1
    if llm_error or llm.should_fail("evidence_track"):
        return {"track": "EVIDENCE", "passed": False, "confidence": 0.0, "fallback_used": True,
                "detail": {"quality_claim": quality, "evidence_ok": evidence_ok, "evidence_missing": False,
                           "degraded": True, **({"llm_error": True} if llm_error else {})}}
    detail_llm = None
    if judged is not None:
        detail_llm = {"quality_claim": judged["quality_claim"], "confidence": judged["confidence"], "reason": judged["reason"]}
        quality = quality or judged["quality_claim"]      # LLM 只能"多提示一个质量问题"，不能抹掉关键词命中
    conf = round(0.5 + 0.3 * quality + 0.2 * evidence_ok, 3)
    detail = {"quality_claim": quality, "evidence_ok": evidence_ok, "evidence_missing": quality and not evidence_ok}
    if detail_llm is not None:
        detail["llm"] = detail_llm
        keyword = any(w in text for w in QUALITY_WORDS)
        if judged["quality_claim"] != keyword or judged["confidence"] < LLM_MIN_CONFIDENCE:
            conf = min(conf, LLM_CAP)
            detail["llm_disagreement"] = judged["quality_claim"] != keyword
    return {"track": "EVIDENCE", "passed": quality and evidence_ok, "confidence": conf, "fallback_used": False,
            "detail": detail}


def fee_track(t: dict, quality: bool, policy: list[dict] | None = None) -> dict:
    suggested = min(int(t["item_payable_cents"]), int(t["max_refundable_cents"]))
    detail = {"suggested_refund_cents": suggested,
              "restock_policy": "DEFECTIVE_WAREHOUSE" if quality else "SELLABLE_WAREHOUSE"}
    if policy:
        detail["policy_clauses"] = _cited(policy)
    return {"track": "FEE", "passed": True, "confidence": 1.0, "fallback_used": False, "detail": detail}


async def run_three(t: dict, policy: list[dict] | None = None) -> dict:
    policy = policy or []

    async def _rule():
        return rule_track(t, clause_window_days(policy), policy)

    async def _evi():
        if not llm.configured():
            return evidence_track(t)
        try:
            return evidence_track(t, judged=await llm.judge_quality(t.get("buyer_reason_text") or ""))
        except llm.LlmError:
            return evidence_track(t, llm_error=True)

    rule, evi = await asyncio.gather(_rule(), _evi())
    fee = fee_track(t, bool(evi["detail"]["quality_claim"]), policy)
    return {"RULE": rule, "EVIDENCE": evi, "FEE": fee}


def route(t: dict, tracks: dict, auto_max_cents: int) -> tuple[str, str]:
    """返回 (route, reason)。route ∈ auto_reject | need_evidence | auto_approve | supervisor。"""
    rule, evi, fee = tracks["RULE"], tracks["EVIDENCE"], tracks["FEE"]
    d = evi["detail"]
    if not rule["passed"] and not (d["quality_claim"] and d["evidence_ok"]):
        return "auto_reject", "超出质量换退期且无质量故障证据"
    if d.get("evidence_missing"):
        return "need_evidence", "质量问题缺少凭证"
    suggested = fee["detail"]["suggested_refund_cents"]
    fallback = any(x["fallback_used"] for x in tracks.values())
    low_risk = (suggested <= auto_max_cents and all(x["passed"] for x in tracks.values())
                and evi["confidence"] >= 0.85 and not fallback)
    if low_risk:
        return "auto_approve", "小额且三轨通过、高置信"
    reasons = []
    if suggested > auto_max_cents:
        reasons.append(f"大额({suggested}分>{auto_max_cents}分)")
    if evi["confidence"] < 0.85:
        reasons.append(f"低置信({evi['confidence']})")
    if fallback:
        reasons.append("存在降级(fallback_used)")
    if not rule["passed"]:
        reasons.append("规则轨冲突")
    return "supervisor", "；".join(reasons) or "需人工复核"
