"""把真实 EcomRAG 适配成 EcomOps 约定的 RAG 契约（替代 stubs/rag_stub.py，同样监听 :8002）。

  POST /api/v1/rag/search      -> EcomRAG POST /api/v1/chat，取其 sources 作为命中片段
  POST /internal/v1/kb/upsert  -> 把条目写成 md 文档，调 EcomRAG 的 `ingest.run --yes`（就地 upsert，稳定主键）

EcomRAG 本身不提供检索与写入的 HTTP 接口，也没有租户与类目过滤：tenant_id / catalog_category_id 在这里被忽略。
"""
import asyncio
import hashlib
import os
import re
import shlex
import tempfile
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

ECOMRAG_URL = os.environ.get("ECOMRAG_URL", "http://127.0.0.1:8010").rstrip("/")
ECOMRAG_ROOT = Path(os.environ.get("ECOMRAG_ROOT", "../EcomRAG")).expanduser()
CHAT_TIMEOUT_S = float(os.environ.get("ECOMRAG_CHAT_TIMEOUT_S", "60"))
INGEST_TIMEOUT_S = float(os.environ.get("ECOMRAG_INGEST_TIMEOUT_S", "300"))
# {dir} 会被替换成只含本次文档的临时目录；文件名以 after_sales_ 开头，入库时据此归类。
INGEST_CMD = os.environ.get("ECOMRAG_INGEST_CMD", "uv run python -m ingest.run --dir {dir} --yes")

app = FastAPI()
_ingest_lock = asyncio.Lock()   # EcomRAG 入库自带发布锁；这里串行化，避免并发起多个加载 bge-m3 的进程


class Search(BaseModel):
    query: str
    tenant_id: str | None = None
    catalog_category_id: str | None = None
    top_k: int = Field(default=8, ge=1, le=100)


class Chunk(BaseModel):
    chunk_id: str
    tenant_id: str
    catalog_category_id: str | None = None
    text: str
    rule_source: str
    is_demo_rule: bool = False
    effective_from: int | None = None
    effective_to: int | None = None
    version: int


def _error(code: str, message: str, status: int = 502):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@app.post("/api/v1/rag/search")
async def search(body: Search):
    payload = {"session_id": "ops-" + uuid.uuid4().hex[:16], "message": body.query.strip()[:500] or "?"}
    try:
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
            r = await client.post(ECOMRAG_URL + "/api/v1/chat", json=payload)
    except httpx.HTTPError as exc:
        return _error("UPSTREAM_ERROR", f"EcomRAG unreachable: {exc}"[:300])
    if r.status_code != 200:
        # 含 429（游客每日 5 次配额）：让 EcomOps 走降级，而不是把"没配额"当成"知识库没有答案"
        return _error("UPSTREAM_ERROR", f"EcomRAG status {r.status_code}: {r.text[:200]}")
    data = r.json()
    hits = [{"chunk_id": s["doc_id"], "text": s.get("snippet", ""), "rule_source": s.get("title", ""),
             "is_demo_rule": False, "score": float(s["score"])} for s in data.get("sources", [])]
    hits.sort(key=lambda h: (-h["score"], h["chunk_id"]))
    return {"hits": hits[:body.top_k]}


def _filename(chunk_id: str) -> str:
    return f"after_sales_ecomops_{re.sub(r'[^0-9A-Za-z]', '', chunk_id)[:40]}.md"


@app.post("/internal/v1/kb/upsert")
async def upsert(body: Chunk):
    name = _filename(body.chunk_id)
    doc_id = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]   # 与 ingest.loaders 的 doc_id 规则一致
    first, _, rest = body.text.partition("\n")
    content = f"# {first.strip()[:80] or body.rule_source}\n\n{(rest or first).strip()}\n\n来源：{body.rule_source}\n"
    backend = ECOMRAG_ROOT / "backend"
    if not backend.is_dir():
        return _error("UPSTREAM_ERROR", f"ECOMRAG_ROOT 不存在: {backend}", 500)
    async with _ingest_lock:
        with tempfile.TemporaryDirectory(prefix="ecomops-kb-") as tmp:
            (Path(tmp) / name).write_text(content, encoding="utf-8")
            cmd = [part.replace("{dir}", tmp) for part in shlex.split(INGEST_CMD)]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(backend), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(proc.communicate(), INGEST_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                return _error("UPSTREAM_ERROR", "ingest timeout", 504)
            except OSError as exc:
                return _error("UPSTREAM_ERROR", f"ingest cannot start: {exc}", 500)
    if proc.returncode != 0:
        return _error("UPSTREAM_ERROR", f"ingest exit {proc.returncode}: {out.decode(errors='replace')[-300:]}", 500)
    return {"chunk_id": body.chunk_id, "milvus_pk": doc_id}


@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(ECOMRAG_URL + "/health")
        return {"status": "ok" if r.status_code == 200 else "degraded", "ecomrag": r.json() if r.status_code == 200 else None}
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"status": "degraded", "ecomrag": None}, status_code=503)
