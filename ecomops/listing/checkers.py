"""确定性抽取与五项检查；不使用加权分覆盖阻断项。"""
import asyncio
import re

import pymupdf

from ecomops.core import llm

REQUIRED_PARAMS = {
    "HOME_APPLIANCE_HEATER": ("额定功率", "容量", "能效等级", "3C"),
    "PERSONAL_SHAMPOO": ("净含量", "保质期", "备案号"),
    "DAILY_GENERAL": ("品名", "规格"),
}
CERT_LABELS = r"3C(?:认证)?(?:证书)?(?:编号|号)?|备案号|质检报告号"


def extract_pdf(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = []
        for page in doc:
            blocks = [b for b in page.get_text("blocks") if b[6] == 0]
            # 先按左边界聚类为列（容忍 24pt 缩进），列内按 y 阅读。
            columns = []
            for block in sorted(blocks, key=lambda b: b[0]):
                if not columns or block[0] - columns[-1][0][0] > 24:
                    columns.append([])
                columns[-1].append(block)
            pages.append("\n".join(b[4] for col in columns for b in sorted(col, key=lambda b: b[1])))
        return "\n".join(pages)


def extract_structure(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    params = {}
    for line in lines:
        for match in re.finditer(r"([^：:；;]+)[：:]\s*([^；;]+)", line):
            params[match[1].strip()] = match[2].strip()
    certificates = [
        {"kind": m[1], "number": m[2]}
        for m in re.finditer(rf"({CERT_LABELS})[ \t]*[：:]?[ \t]*([^\s；;，,]+)", text)
    ]
    return {"title": lines[0] if lines else "", "parameters": params,
            "claims": [line for line in lines if re.search("功效|防脱|生发|治疗|治愈", line)],
            "certificates": certificates, "text": text}


def check(name: str, document: dict, category: str) -> dict:
    result = {"checker": name, "fallback_used": llm.should_fail(name),
              "blocking_violations": [], "manual_verify_items": [], "advisory_suggestions": []}
    text = document["text"]
    if name in ("extreme_words", "medical_claims"):
        words = ("最", "第一", "国家级", "100%", "永久", "根治") if name == "extreme_words" else ("防脱", "生发", "治疗", "治愈")
        for word in words:
            if word in text:
                result["blocking_violations"].append({"checker": name, "term": word, "message": f"命中违规宣称：{word}"})
    elif name == "required_params":
        for field in REQUIRED_PARAMS.get(category, ("品名",)):
            # 标签必须有非空值；仅在宣传句中出现参数名不算参数表。
            if not re.search(rf"{re.escape(field)}(?:认证)?(?:证书)?(?:编号|号)?[ \t]*[：:][ \t]*[^\s；;，,]+", text):
                result["blocking_violations"].append({"checker": name, "field": field, "message": f"缺少必填参数：{field}"})
    elif name == "cert_extract":
        for cert in document["certificates"]:
            kind = cert["kind"]
            # 简化格式：3C 编号为 16 位数字；备案号为省份前缀+妆网备字+10位数字；质检报告号为6~32位字母数字/横线。
            pattern = r"\d{16}" if kind.startswith("3C") else (r"[一-龥]{1,3}妆网备字\d{10}" if kind == "备案号" else r"[A-Za-z0-9-]{6,32}")
            if not re.fullmatch(pattern, cert["number"]):
                result["manual_verify_items"].append({"checker": name, **cert, "message": "编号格式存疑，请核验证书原件"})
    else:
        result["advisory_suggestions"].append({"checker": name, "message": "建议分段展示商品参数，并提供证书原件及适用范围以便消费者核验。"})
    if result["fallback_used"]:
        result["manual_verify_items"].append({"checker": name, "message": "检查器降级，需人工核实", "fallback_used": True})
    return result


async def review_document(document: dict, category: str) -> dict:
    results = await asyncio.gather(*(asyncio.to_thread(check, name, document, category) for name in
                                    ("extreme_words", "required_params", "medical_claims", "cert_extract", "readability")))
    report = {key: [item for result in results for item in result[key]] for key in
              ("blocking_violations", "manual_verify_items", "advisory_suggestions")}
    report["fallback_used"] = any(r["fallback_used"] for r in results)
    report["verdict"] = ("REJECTED_BLOCKING" if report["blocking_violations"] else
                         "PENDING_MANUAL_VERIFY" if report["manual_verify_items"] or report["fallback_used"] else
                         "PASSED_WITH_SUGGESTIONS")
    report["checks"] = results
    return report
