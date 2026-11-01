"""联网搜索（Web Search MCP 的最小实现）：Tavily → DuckDuckGo 回退。

默认关闭（ECOMOPS_WEB_SEARCH=off）；只在知识库低置信且开关为 on 时才会被调用。
搜索结果是不可信的外部文本，只作为"兜底回答的参考"，永远不会写进知识库。失败返回 []。"""
import html
import re

import httpx

from ecomops.core.config import get_settings

DDG_URL = "https://html.duckduckgo.com/html/"
TAVILY_URL = "https://api.tavily.com/search"


def enabled() -> bool:
    return get_settings().web_search.lower() == "on"


async def _tavily(client: httpx.AsyncClient, query: str, limit: int, key: str) -> list[dict]:
    r = await client.post(TAVILY_URL, json={"api_key": key, "query": query, "max_results": limit})
    r.raise_for_status()
    return [{"title": str(x.get("title", ""))[:200], "url": str(x.get("url", "")), "snippet": str(x.get("content", ""))[:400]}
            for x in r.json().get("results", [])[:limit] if x.get("url")]


async def _duckduckgo(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    r = await client.post(DDG_URL, data={"q": query}, headers={"User-Agent": "Mozilla/5.0 (EcomOps)"})
    r.raise_for_status()
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
                         r.text, re.S):
        clean = lambda t: html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
        out.append({"title": clean(m.group(2))[:200], "url": html.unescape(m.group(1)), "snippet": clean(m.group(3))[:400]})
        if len(out) >= limit:
            break
    return out


async def search(query: str, limit: int = 3) -> list[dict]:
    s = get_settings()
    if not enabled():
        return []
    async with httpx.AsyncClient(timeout=s.web_search_timeout_s) as client:
        if s.tavily_key:
            try:
                results = await _tavily(client, query, limit, s.tavily_key)
                if results:
                    return results
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
        try:
            return await _duckduckgo(client, query, limit)
        except (httpx.HTTPError, ValueError):
            return []
