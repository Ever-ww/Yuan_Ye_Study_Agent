"""公开网页抓取工具的正文提取、SSRF 边界和 Runtime 装配测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from Agent import AgentRuntime, load_runtime_config
from Agent.contracts import EventType, ModelReply, ToolCall
from Agent.hook import HookRegistry
from Agent.react import ReactLoop
from tool import AsyncToolRegistry, ToolContext
from tools import (
    WebFetchResponse,
    WebFetchSecurityError,
    WebFetchServiceError,
    WebFetchTool,
    WebSearchTool,
)


async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return ("93.184.216.34",)


class WebFetchTests(unittest.TestCase):
    def test_fetch_accepts_arxiv_atom_feed_and_extracts_readable_text(self) -> None:
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>arXiv Query: tabular deep learning</title>
          <entry>
            <title>Deep Learning for Tabular Data</title>
            <summary>A benchmark of neural networks for tables.</summary>
            <id>http://arxiv.org/abs/2601.00001v1</id>
          </entry>
        </feed>"""

        async def invoke() -> WebFetchResponse:
            tool = WebFetchTool(
                transport=httpx.MockTransport(lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/atom+xml; charset=utf-8"},
                    text=feed,
                )),
                resolver=_public_resolver,
            )
            raw = await tool.run(
                {"url": "https://export.arxiv.org/api/query?search_query=all:tabular"},
                ToolContext(project_root=Path.cwd()),
            )
            return WebFetchResponse.model_validate_json(raw, strict=True)

        result = asyncio.run(invoke())
        self.assertEqual(result.title, "arXiv Query: tabular deep learning")
        self.assertIn("Deep Learning for Tabular Data", result.content)
        self.assertIn("A benchmark of neural networks for tables.", result.content)
        self.assertIn("http://arxiv.org/abs/2601.00001v1", result.content)
        self.assertNotIn("<entry>", result.content)

    def test_fetch_follows_safe_redirect_and_extracts_html(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "/article#ignored"})
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=(
                    "<html><head><title>示例 页面</title><style>secret</style></head>"
                    "<body><h1>标题</h1><script>ignore()</script><p>第一段</p>"
                    "<p>第二段</p></body></html>"
                ),
            )

        async def invoke(root: Path) -> WebFetchResponse:
            tool = WebFetchTool(
                transport=httpx.MockTransport(handler),
                resolver=_public_resolver,
            )
            raw = await tool.run(
                {"url": "https://example.com/start", "max_chars": 5000},
                ToolContext(project_root=root),
            )
            return WebFetchResponse.model_validate_json(raw, strict=True)

        with tempfile.TemporaryDirectory() as value:
            result = asyncio.run(invoke(Path(value)))
        self.assertEqual(result.final_url, "https://example.com/article")
        self.assertEqual(result.title, "示例 页面")
        self.assertIn("标题", result.content)
        self.assertIn("第一段", result.content)
        self.assertNotIn("ignore", result.content)
        self.assertNotIn("secret", result.content)
        self.assertTrue(result.untrusted_external_content)
        self.assertEqual(len(requests), 2)
        self.assertIn("YuanYeAgent", requests[0].headers["user-agent"])

    def test_private_targets_and_private_dns_answers_are_blocked_before_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="never")

        async def mixed_resolver(host: str, port: int) -> tuple[str, ...]:
            del host, port
            return ("93.184.216.34", "10.0.0.5")

        async def invoke(url: str, resolver=_public_resolver) -> None:
            tool = WebFetchTool(
                transport=httpx.MockTransport(handler),
                resolver=resolver,
            )
            await tool.run({"url": url}, ToolContext(project_root=Path.cwd()))

        blocked = (
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://localhost/",
            "http://169.254.169.254/latest/meta-data/",
            "https://user:password@example.com/",
            "https://example.com:8443/",
        )
        for url in blocked:
            with self.subTest(url=url), self.assertRaises(WebFetchSecurityError):
                asyncio.run(invoke(url))
        with self.assertRaises(WebFetchSecurityError):
            asyncio.run(invoke("https://example.com/", mixed_resolver))
        self.assertEqual(calls, 0)

    def test_redirect_to_private_address_is_blocked(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

        async def invoke() -> None:
            tool = WebFetchTool(
                transport=httpx.MockTransport(handler),
                resolver=_public_resolver,
            )
            await tool.run(
                {"url": "https://example.com/start"},
                ToolContext(project_root=Path.cwd()),
            )

        with self.assertRaises(WebFetchSecurityError):
            asyncio.run(invoke())

    def test_binary_oversized_and_truncated_responses_are_controlled(self) -> None:
        async def invoke(response: httpx.Response, *, max_bytes=100_000, max_chars=1000):
            tool = WebFetchTool(
                max_bytes=max_bytes,
                max_chars=max_chars,
                transport=httpx.MockTransport(lambda request: response),
                resolver=_public_resolver,
            )
            return await tool.run(
                {"url": "https://example.com/data"},
                ToolContext(project_root=Path.cwd()),
            )

        with self.assertRaisesRegex(WebFetchServiceError, "内容类型"):
            asyncio.run(invoke(httpx.Response(200, headers={"content-type": "application/pdf"})))
        with self.assertRaisesRegex(WebFetchServiceError, "超过"):
            asyncio.run(invoke(httpx.Response(
                200,
                headers={"content-type": "text/plain", "content-length": "100001"},
            )))
        raw = asyncio.run(invoke(httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="A" * 1500,
        )))
        result = WebFetchResponse.model_validate_json(raw, strict=True)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.content), 1000)

    def test_runtime_always_exposes_fetch_and_subagent_can_receive_it(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            runtime = AgentRuntime(load_runtime_config(root), enable_sandbox=False)
            self.assertIn("web_fetch", runtime.tools.names(runtime.tool_context))
            schema = next(
                item for item in runtime.tools.schemas(runtime.tool_context)
                if item["name"] == "subagent"
            )
            delegated = schema["parameters"]["properties"]["tools"]["items"]["enum"]
            self.assertIn("web_fetch", delegated)

    def test_react_chains_search_then_fetch_then_final_answer(self) -> None:
        class ChainedProvider:
            streaming = False

            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                tool_names = {item["name"] for item in tools}
                self.assert_tools(tool_names)
                tool_messages = [message for message in messages if message["role"] == "tool"]
                if self.calls == 1:
                    return ModelReply(tool_calls=(ToolCall(
                        name="web_search",
                        arguments={"query": "Yuan Ye Agent"},
                    ),))
                if self.calls == 2:
                    search_result = tool_messages[-1]["content"]
                    self.assert_search_result(search_result)
                    return ModelReply(tool_calls=(ToolCall(
                        name="web_fetch",
                        arguments={"url": "https://example.com/article"},
                    ),))
                self.assert_fetch_result(tool_messages[-1]["content"])
                return ModelReply(text="已根据抓取正文完成回答")

            @staticmethod
            def assert_tools(names: set[str]) -> None:
                if not {"web_search", "web_fetch"}.issubset(names):
                    raise AssertionError(f"模型没有同时看到串联工具：{names}")

            @staticmethod
            def assert_search_result(value: str) -> None:
                if '"next_tool":"web_fetch"' not in value or "https://example.com/article" not in value:
                    raise AssertionError("搜索结果没有提供抓取入口")

            @staticmethod
            def assert_fetch_result(value: str) -> None:
                if "可核验的页面正文" not in value:
                    raise AssertionError("抓取正文没有进入下一次模型调用")

        def search_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "web": {"results": [{
                    "title": "候选页面",
                    "url": "https://example.com/article",
                    "description": "搜索摘要",
                }]},
            })

        def fetch_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><body><main>可核验的页面正文</main></body></html>",
            )

        async def invoke(root: Path) -> tuple[list[str], int]:
            provider = ChainedProvider()
            registry = AsyncToolRegistry((
                WebSearchTool("key", transport=httpx.MockTransport(search_handler)),
                WebFetchTool(
                    transport=httpx.MockTransport(fetch_handler),
                    resolver=_public_resolver,
                ),
            ))
            loop = ReactLoop(provider, registry, HookRegistry(), max_steps=4)
            events = [
                event
                async for event in loop.run(
                    [
                        {"role": "system", "content": "测试"},
                        {"role": "user", "content": "搜索并读取页面"},
                    ],
                    ToolContext(project_root=root),
                    task="搜索并读取页面",
                    session_id="session_chain",
                    model={"provider": "test", "model": "test"},
                )
            ]
            requested = [
                str(event.payload["name"])
                for event in events
                if event.type is EventType.TOOL_REQUESTED
            ]
            final = next(event for event in events if event.type is EventType.FINAL)
            self.assertEqual(final.payload["answer"], "已根据抓取正文完成回答")
            return requested, provider.calls

        with tempfile.TemporaryDirectory() as value:
            requested, model_calls = asyncio.run(invoke(Path(value)))
        self.assertEqual(requested, ["web_search", "web_fetch"])
        self.assertEqual(model_calls, 3)


if __name__ == "__main__":
    unittest.main()
