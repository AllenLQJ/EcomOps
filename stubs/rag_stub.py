"""Deterministic in-memory RAG; run with uvicorn stubs.rag_stub:app --port 8002."""
import re
import threading

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal

app = FastAPI()
lock = threading.Lock()
chunks: dict[str, dict] = {}
state = {"mode": "none"}
DEMOS = [
    dict(chunk_id="demo-heater", text="热水器质量问题15天内可退换，质量问题退换运费由商家承担。", rule_source="沙盒热水器退换规则", is_demo_rule=True, catalog_category_id="HOME_APPLIANCE_HEATER", keywords=("热水器", "退换", "漏水", "质量", "运费", "退货", "15天")),
    dict(chunk_id="demo-seven", text="7天无理由退货需商品完好，非质量问题运费由买家承担。", rule_source="沙盒7天无理由规则", is_demo_rule=True, catalog_category_id=None, keywords=("7天", "无理由", "退货", "运费", "完好")),
]


class Search(BaseModel):
    query: str
    tenant_id: str | None = None
    catalog_category_id: str | None = None
    top_k: int = Field(default=8, ge=1, le=100)


class Chunk(BaseModel):
    chunk_id: str
    tenant_id: str
    catalog_category_id: str | None
    text: str
    rule_source: str
    is_demo_rule: bool = False
    effective_from: int | None = None
    effective_to: int | None = None
    version: int


def tokens(value):
    value = re.sub(r"\s+", "", value.lower())
    return set(value[i:i + 2] for i in range(max(1, len(value) - 1)))


@app.post("/api/v1/rag/search")
def search(body: Search):
    with lock:
        candidates = [dict(c) for c in chunks.values() if c["tenant_id"] == body.tenant_id] + [dict(c) for c in DEMOS]
    hits = []
    for c in candidates:
        if body.catalog_category_id and c.get("catalog_category_id") not in (None, body.catalog_category_id):
            continue
        query = body.query.removesuffix(" 商品售后政策说明")
        q = tokens(query)
        score = len(q & tokens(c["text"])) / max(1, len(q))
        if query and query in c["text"]:
            score = 0.95
        matched = sum(1 for k in c.get("keywords", ()) if k in query)
        if matched >= 2:   # 沙箱演示规则：命中 2 个以上关键词视为高置信
            score = max(score, min(0.95, 0.7 + 0.05 * matched))
        if score:
            hits.append({k: c[k] for k in ("chunk_id", "text", "rule_source", "is_demo_rule")} | {"score": round(score, 4)})
    return {"hits": sorted(hits, key=lambda h: (-h["score"], h["chunk_id"]))[:body.top_k]}


@app.post("/internal/v1/kb/upsert")
def upsert(body: Chunk):
    with lock:
        if state["mode"] == "fail_before_write":
            return JSONResponse({"error": {"code": "UPSTREAM_ERROR", "message": "before write"}}, status_code=500)
        chunks[body.chunk_id] = body.model_dump()
        if state["mode"] == "fail_after_write":
            return JSONResponse({"error": {"code": "UPSTREAM_ERROR", "message": "after write"}}, status_code=500)
    return {"chunk_id": body.chunk_id, "milvus_pk": body.chunk_id}


@app.get("/stub/admin/chunks/count")
def count():
    with lock:
        return {"total": len(chunks)}


@app.get("/stub/admin/chunks")
def chunk_count(chunk_id: str):
    with lock:
        return {"chunk_id": chunk_id, "count": int(chunk_id in chunks)}


class Fault(BaseModel):
    mode: Literal["none", "fail_after_write", "fail_before_write"]


@app.post("/stub/admin/fault")
def fault(body: Fault):
    with lock:
        state["mode"] = body.mode
    return {"mode": body.mode}


@app.get("/health")
def health():
    return {"status": "ok"}
