"""EcomRAG 检索客户端：kb 问答、售后条款检索、对练条款核对共用。

失败（网络、超时、非 2xx、响应形状不对）一律返回 None，由调用方决定降级，绝不抛出。"""
import httpx

from ecomops.core.config import get_settings, rag_headers


async def search(query: str, *, tenant_id: str | None = None, catalog_category_id: str | None = None,
                 top_k: int = 5, timeout: float | None = None) -> list[dict] | None:
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=timeout or s.rag_search_timeout_s) as client:
            r = await client.post(s.rag_url.rstrip("/") + "/api/v1/rag/search", headers=rag_headers(s), json={
                "query": query, "tenant_id": tenant_id, "catalog_category_id": catalog_category_id, "top_k": top_k})
            r.raise_for_status()
            hits = r.json()["hits"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(hits, list) or not all(isinstance(h, dict) and "chunk_id" in h and "score" in h for h in hits):
        return None
    return hits
