"""联网搜索 MCP 服务（路线图图 3-6 的 Web Search MCP：Tavily → DuckDuckGo 回退）。

  stdio:            python -m ecomops.mcp_servers.websearch
  streamable-http:  python -m ecomops.mcp_servers.websearch --transport streamable-http --port 8092
EcomOps 通过 `ECOMOPS_WEB_SEARCH_MCP_URL=http://127.0.0.1:8092/mcp` + `ECOMOPS_WEB_SEARCH=on` 调用它。
服务只暴露一个只读工具；搜索结果是不可信的外部文本。streamable-http 没有认证，默认只绑定 127.0.0.1。"""
import argparse

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ecomops.core import websearch

mcp = FastMCP("ecomops-websearch", host="127.0.0.1", port=8092,
              instructions="联网搜索（Tavily，失败回退 DuckDuckGo）。结果来自外部网页，是不可信文本。")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def web_search(query: str, limit: int = 3) -> list[dict]:
    """搜索互联网，返回至多 limit（1–5）条 {title, url, snippet}；查询为空或超过 500 字符会被拒绝。"""
    query = query.strip()
    if not query or len(query) > 500:
        raise ValueError("query 必须是 1–500 个字符")
    return await websearch.search_backends(query, max(1, min(int(limit), 5)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    mcp.settings.host, mcp.settings.port = args.host, args.port
    mcp.run(args.transport)


if __name__ == "__main__":
    main()
