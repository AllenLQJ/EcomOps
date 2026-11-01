"""三轨审核：默认纯规则。配置了真实 LLM 时，EVIDENCE 轨多一个"是否质量问题"的判断，但只能转人工，不能触发自动通过。
LLM 调用失败/输出不合规 → fallback_used（与故障注入同一降级路径）。"""
import asyncio

from ecomops.core import llm

QUALITY_WORDS = ("漏水", "打不着火", "不制热", "坏了", "坏", "破损", "故障", "质量", "变质", "漏电", "不能用")
WINDOW_DAYS = 15


def rule_track(t: dict) -> dict:
    ok = int(t["days_since_delivery"]) <= WINDOW_DAYS
    return {"track": "RULE", "passed": ok, "confidence": 1.0, "fallback_used": False,
            "detail": {"window_days": WINDOW_DAYS, "days_since_delivery": int(t["days_since_delivery"])}}


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


def fee_track(t: dict, quality: bool) -> dict:
    suggested = min(int(t["item_payable_cents"]), int(t["max_refundable_cents"]))
    return {"track": "FEE", "passed": True, "confidence": 1.0, "fallback_used": False,
            "detail": {"suggested_refund_cents": suggested,
                       "restock_policy": "DEFECTIVE_WAREHOUSE" if quality else "SELLABLE_WAREHOUSE"}}


async def run_three(t: dict) -> dict:
    async def _rule():
        return rule_track(t)

    async def _evi():
        if not llm.configured():
            return evidence_track(t)
        try:
            return evidence_track(t, judged=await llm.judge_quality(t.get("buyer_reason_text") or ""))
        except llm.LlmError:
            return evidence_track(t, llm_error=True)

    rule, evi = await asyncio.gather(_rule(), _evi())
    fee = fee_track(t, bool(evi["detail"]["quality_claim"]))
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
