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


def _clean(items) -> list[dict]:
    """外部结果一律当不可信：只留三个字符串字段并截断。"""
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]:
            out.append({"title": str(item.get("title", ""))[:200], "url": item["url"][:500],
                        "snippet": str(item.get("snippet", ""))[:400]})
    return out


async def _via_mcp(url: str, query: str, limit: int) -> list[dict]:
    """经 MCP（streamable-http）调用联网搜索服务；任何失败都当作没有结果（不回退到进程内直接搜索，
    否则"把联网放进隔离的 MCP 服务"这个边界就被绕过了）。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    s = get_settings()
    try:
        async with streamablehttp_client(url, timeout=s.web_search_timeout_s) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("web_search", {"query": query, "limit": limit})
        if result.isError:
            return []
        data = result.structuredContent
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        if data is None:                           # 只带文本内容的服务端：取第一段文本按 JSON 解析
            import json
            data = json.loads(result.content[0].text)
        return _clean(data)[:limit]
    except Exception:    # noqa: BLE001 — MCP/网络/解析的任何失败都降级为"没有结果"
        return []


async def search(query: str, limit: int = 3) -> list[dict]:
    if not enabled():
        return []
    url = get_settings().web_search_mcp_url
    if url:
        return await _via_mcp(url, query, limit)
    return await search_backends(query, limit)


async def search_backends(query: str, limit: int = 3) -> list[dict]:
    """直接访问 Tavily → DuckDuckGo（MCP 服务端内部也调它）。不看总开关：开关属于调用方。"""
    s = get_settings()
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
