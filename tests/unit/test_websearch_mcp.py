"""联网搜索经 MCP：服务端工具、真实 streamable-http 往返、以及客户端的降级行为。全部不联网。"""
import socket
import threading
import time

import pytest
import uvicorn

from ecomops.core import config, websearch
from ecomops.mcp_servers import websearch as server

RESULTS = [{"title": "标题", "url": "https://e.test/1", "snippet": "摘要"}]


@pytest.fixture
def env(monkeypatch):
    def setter(**values):
        for key, value in values.items():
            monkeypatch.setenv("ECOMOPS_" + key.upper(), value)
        config.get_settings.cache_clear()
    yield setter
    config.get_settings.cache_clear()


@pytest.fixture
def mcp_http(monkeypatch):
    """起真实的 streamable-http MCP 服务（后端搜索被替换成假结果），返回 /mcp 地址与调用记录。"""
    calls = []

    async def fake_backends(query, limit=3):
        calls.append((query, limit))
        return RESULTS
    monkeypatch.setattr(websearch, "search_backends", fake_backends)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = server.mcp.streamable_http_app()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", calls
    srv.should_exit = True
    thread.join(5)


async def test_client_goes_through_mcp_when_url_configured_and_switch_on(env, mcp_http):
    url, calls = mcp_http
    env(web_search="on", web_search_mcp_url=url)
    assert await websearch.search("海淘税费", limit=2) == RESULTS
    assert calls == [("海淘税费", 2)]


async def test_switch_off_never_connects(env, monkeypatch):
    env(web_search="off", web_search_mcp_url="http://127.0.0.1:9/mcp")
    monkeypatch.setattr(websearch, "_via_mcp", lambda *a: (_ for _ in ()).throw(AssertionError("must not connect")))
    assert await websearch.search("x") == []


async def test_unreachable_mcp_server_degrades_to_empty_and_does_not_bypass_to_direct(env, monkeypatch):
    env(web_search="on", web_search_mcp_url="http://127.0.0.1:9/mcp")
    monkeypatch.setattr(websearch, "search_backends", lambda *a: (_ for _ in ()).throw(AssertionError("MCP 边界不能被绕过")))
    assert await websearch.search("x") == []


async def test_no_mcp_url_uses_in_process_backends(env, monkeypatch):
    env(web_search="on")

    async def fake(query, limit=3):
        return RESULTS
    monkeypatch.setattr(websearch, "search_backends", fake)
    assert await websearch.search("x") == RESULTS


async def test_server_tool_validates_input_and_clamps_limit(monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session
    seen = []

    async def fake(query, limit=3):
        seen.append((query, limit))
        return RESULTS
    monkeypatch.setattr(websearch, "search_backends", fake)
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
        tools = (await session.list_tools()).tools
        assert [t.name for t in tools] == ["web_search"] and tools[0].annotations.readOnlyHint is True
        await session.call_tool("web_search", {"query": " 退款 ", "limit": 99})
        assert seen == [("退款", 5)]
        for bad in ("", "  ", "x" * 501):
            assert (await session.call_tool("web_search", {"query": bad})).isError is True
        assert len(seen) == 1                                           # 非法查询没有到达后端


def test_clean_keeps_only_safe_string_fields():
    dirty = [{"title": "t" * 300, "url": "https://e.test", "snippet": "s" * 900, "evil": "<script>"},
             {"title": "no url"}, "string", {"url": ""}, None]
    cleaned = websearch._clean(dirty)
    assert len(cleaned) == 1 and set(cleaned[0]) == {"title", "url", "snippet"}
    assert len(cleaned[0]["title"]) == 200 and len(cleaned[0]["snippet"]) == 400
    assert websearch._clean("not a list") == []
