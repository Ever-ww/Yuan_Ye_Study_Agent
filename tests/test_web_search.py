"""网络搜索工具的配置、协议、安全和 Runtime 装配测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from Agent import AgentRuntime, load_runtime_config
from sandbox import sandbox_status_of
from tool import ToolContext, default_tools
from tools import (
    WebSearchNetworkError,
    WebSearchResponse,
    WebSearchResponseError,
    WebSearchServiceError,
    WebSearchTool,
)


class WebSearchTests(unittest.TestCase):
    def test_brave_request_and_response_are_normalized(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={
                "web": {
                    "results": [
                        {
                            "title": "<b>官方</b>\u0000文档",
                            "url": "https://example.com/docs",
                            "description": "公开 <em>摘要</em>",
                            "age": "2 hours ago",
                        },
                        {"title": "不安全协议", "url": "file:///secret"},
                    ],
                },
            })

        async def check(root: Path) -> WebSearchResponse:
            tool = WebSearchTool(
                "search-secret",
                transport=httpx.MockTransport(handler),
            )
            raw = await tool.run(
                {
                    "query": "Python agent",
                    "count": 2,
                    "freshness": "past_week",
                    "country": "cn",
                    "search_lang": "zh-hans",
                    "safesearch": "strict",
                },
                ToolContext(project_root=root),
            )
            return WebSearchResponse.model_validate_json(raw, strict=True)

        with tempfile.TemporaryDirectory() as value:
            result = asyncio.run(check(Path(value)))
        self.assertEqual(result.provider, "brave")
        self.assertTrue(result.untrusted_external_content)
        self.assertEqual(result.next_tool, "web_fetch")
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].title, "官方 文档")
        self.assertEqual(result.results[0].description, "公开 摘要")
        self.assertEqual(result.results[0].url, "https://example.com/docs")
        request = requests[0]
        self.assertEqual(request.headers["x-subscription-token"], "search-secret")
        self.assertIn("freshness=pw", str(request.url))
        self.assertIn("country=CN", str(request.url))
        self.assertIn("search_lang=zh-hans", str(request.url))

    def test_network_service_and_response_errors_do_not_expose_key(self) -> None:
        async def invoke(status: int | None, payload=None) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                if status is None:
                    raise httpx.ConnectError("offline", request=request)
                return httpx.Response(status, json=payload)

            tool = WebSearchTool("do-not-leak", transport=httpx.MockTransport(handler))
            await tool.run({"query": "test"}, ToolContext(project_root=Path.cwd()))

        with self.assertRaises(WebSearchNetworkError) as network:
            asyncio.run(invoke(None))
        self.assertNotIn("do-not-leak", str(network.exception))
        self.assertTrue(network.exception.retryable)
        with self.assertRaises(WebSearchServiceError) as service:
            asyncio.run(invoke(401, {"error": "unauthorized"}))
        self.assertNotIn("do-not-leak", str(service.exception))
        self.assertFalse(service.exception.retryable)
        with self.assertRaises(WebSearchResponseError) as response:
            asyncio.run(invoke(200, []))
        self.assertFalse(response.exception.retryable)

    def test_schema_and_argument_limits_are_enforced_before_network(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"web": {"results": []}})

        async def check(root: Path) -> None:
            tool = WebSearchTool("key", transport=httpx.MockTransport(handler))
            registry = default_tools(root, web_search_tool=tool)
            context = ToolContext(project_root=root)
            with self.assertRaisesRegex(ValueError, "参数校验失败"):
                await registry.execute("web_search", {"query": "x", "count": "5"}, context)
            with self.assertRaisesRegex(ValueError, "1 到 10"):
                await registry.execute("web_search", {"query": "x", "count": 11}, context)
            with self.assertRaisesRegex(ValueError, "国家代码"):
                await registry.execute("web_search", {"query": "x", "country": "china"}, context)

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))
        self.assertEqual(calls, 0)

    def test_runtime_only_exposes_search_when_local_key_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            disabled = AgentRuntime(
                load_runtime_config(root),
                enable_sandbox=False,
            )
            self.assertNotIn("web_search", disabled.tools.names(disabled.tool_context))
            self.assertIn("web_fetch", disabled.tools.names(disabled.tool_context))

            enabled = AgentRuntime(
                load_runtime_config(root, web_search_api_key="local-search-key"),
                enable_sandbox=False,
            )
            names = enabled.tools.names(enabled.tool_context)
            self.assertIn("web_search", names)
            schema = next(item for item in enabled.tools.schemas(enabled.tool_context) if item["name"] == "subagent")
            delegated = schema["parameters"]["properties"]["tools"]["items"]["enum"]
            self.assertIn("web_search", delegated)
            self.assertFalse(sandbox_status_of(enabled.sandbox).bash_available)

    def test_search_key_is_rejected_from_shared_settings(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".yy").mkdir()
            (root / ".yy" / "settings.json").write_text(
                '{"web_search_api_key":"must-be-local"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "settings.local.json"):
                load_runtime_config(root)


if __name__ == "__main__":
    unittest.main()
