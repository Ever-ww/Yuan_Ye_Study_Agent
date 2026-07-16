"""验证 MCP/LSP 的 Docker 强隔离、环境清理和无副作用授权描述。

测试只使用临时 JSON 配置与内存 mock，不要求本机安装 Docker、MCP SDK 或 Language
Server。这样 Windows/Linux CI 都能验证“Docker 缺失时安全失败”以及 SDK 最终收到的
确实是容器命令，而不会意外启动第三方进程。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, Mock, patch

from Agent.integrations import LSPManager, MCPManager
from Agent.sandbox import DockerSandbox, SandboxUnavailable
from tools.extensions import LSPTool, MCPCallTool


class IntegrationSecurityTests(unittest.IsolatedAsyncioTestCase):
    """覆盖外部协议进程在执行前必须满足的安全不变量。"""

    def setUp(self) -> None:
        """为每个用例创建互不共享的项目与用户配置目录。"""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.user = self.root / "user"
        self.project.mkdir()
        self.user.mkdir()
        (self.project / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "local": {
                            "type": "stdio",
                            "command": "python",
                            "args": ["server.py"],
                            "env": {"MCP_TOKEN": "explicit-mcp-secret"},
                        },
                        "remote": {
                            "type": "streamable-http",
                            "url": "https://mcp.example.test/v1",
                            "headers": {"Authorization": "explicit-http-secret"},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.project / ".lsp.json").write_text(
            json.dumps(
                {
                    "lspServers": {
                        "python": {
                            "command": "pylsp",
                            "args": ["--stdio"],
                            "env": {"LSP_TOKEN": "explicit-lsp-secret"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        """删除用例创建的临时配置和目录。"""

        self.temporary.cleanup()

    def test_minimal_environment_and_wrapper_hide_unapproved_secrets(self) -> None:
        """宿主秘密不得继承，显式 env 值也不得直接出现在 Docker argv。"""

        sandbox = DockerSandbox("example/image:fixed")
        host = {"PATH": "safe-path", "OPENAI_API_KEY": "unapproved-secret"}
        with patch.dict(os.environ, host, clear=True), patch("Agent.sandbox.shutil.which", return_value="docker"):
            environment = sandbox.minimal_environment({"MCP_TOKEN": "approved-secret"})
            wrapped = sandbox.wrap_argv(
                ["python", "server.py"],
                cwd=str(self.project),
                environment={"MCP_TOKEN": "approved-secret"},
            )

        self.assertEqual(environment, {"PATH": "safe-path", "MCP_TOKEN": "approved-secret"})
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(wrapped[0:2], ["docker", "run"])
        self.assertIn("example/image:fixed", wrapped)
        self.assertIn("MCP_TOKEN", wrapped)
        self.assertNotIn("approved-secret", wrapped)
        self.assertNotIn("unapproved-secret", wrapped)

    def test_authorization_descriptors_are_stable_and_side_effect_free(self) -> None:
        """审批参数应暴露真实目标、隐藏 env 值，并随配置内容变化。"""

        sandbox = DockerSandbox("example/image:fixed")
        mcp = MCPManager(self.project, self.user, sandbox=sandbox)
        lsp = LSPManager(self.project, self.user, sandbox=sandbox)

        mcp_arguments = MCPCallTool(manager=mcp).permission_arguments(
            {"server": "local", "tool": "lookup", "arguments": {"query": "x"}}
        )
        lsp_arguments = LSPTool(manager=lsp).permission_arguments(
            {"server": "python", "operation": "hover", "uri": "file:///workspace/a.py"}
        )

        self.assertEqual(mcp_arguments["transport"], "stdio")
        self.assertEqual(mcp_arguments["command"], "python")
        self.assertEqual(mcp_arguments["args"], ["server.py"])
        self.assertEqual(mcp_arguments["environment_keys"], ["MCP_TOKEN"])
        self.assertNotIn("explicit-mcp-secret", json.dumps(mcp_arguments))
        self.assertEqual(lsp_arguments["command"], "pylsp")
        self.assertEqual(lsp_arguments["sandbox_image"], "example/image:fixed")
        remote = mcp.authorization_descriptor("remote")
        self.assertEqual(remote["transport"], "streamable-http")
        self.assertEqual(remote["url"], "https://mcp.example.test/v1")
        self.assertNotIn("explicit-http-secret", json.dumps(remote))

        previous_hash = mcp_arguments["config_hash"]
        mcp.servers["local"]["env"]["MCP_TOKEN"] = "rotated-secret"
        self.assertNotEqual(previous_hash, mcp.authorization_descriptor("local")["config_hash"])

    async def test_existing_run_api_reuses_the_same_wrapper(self) -> None:
        """原有 ``run`` 应继续返回三元组，并复用新的容器边界。"""

        sandbox = DockerSandbox("example/image:fixed")
        process = AsyncMock(return_value=(0, "ok", ""))
        with patch("Agent.sandbox.shutil.which", return_value="docker"), patch(
            "Agent.sandbox._run_process", process
        ):
            result = await sandbox.run(
                ["python", "-V"], cwd=str(self.project), writable=True, network=True, timeout=15
            )

        self.assertEqual(result, (0, "ok", ""))
        command = process.await_args.args[0]
        self.assertEqual(command[0:2], ["docker", "run"])
        self.assertIn("rw", command[command.index("--mount") + 1])
        self.assertEqual(command[command.index("--network") + 1], "bridge")
        self.assertEqual(process.await_args.kwargs["timeout"], 15)
        self.assertTrue(process.await_args.kwargs["clean_environment"])

    async def test_stdio_mcp_fails_closed_before_optional_sdk_import(self) -> None:
        """Docker 缺失时应先拒绝，不因 MCP SDK 缺失而尝试原始主机命令。"""

        manager = MCPManager(self.project, self.user)
        with patch("Agent.sandbox.shutil.which", return_value=None):
            with self.assertRaises(SandboxUnavailable):
                await manager.call_tool("local", "lookup", {})

    async def test_remote_mcp_rejects_unsafe_urls_before_sdk_connection(self) -> None:
        """HTTP/SSE transport 必须在创建 SDK 连接前拒绝 IP、localhost 和内网域名。"""

        streamable_client = Mock(side_effect=AssertionError("不得创建 Streamable HTTP 连接"))
        sse_client = Mock(side_effect=AssertionError("不得创建 SSE 连接"))
        mcp_module = types.ModuleType("mcp")
        mcp_module.__path__ = []  # type: ignore[attr-defined]
        # 这些占位符使测试不依赖真实 MCP SDK；URL 校验正确时不会实例化任何一个对象。
        mcp_module.ClientSession = Mock(name="ClientSession")  # type: ignore[attr-defined]
        mcp_module.StdioServerParameters = Mock(name="StdioServerParameters")  # type: ignore[attr-defined]
        client_module = types.ModuleType("mcp.client")
        client_module.__path__ = []  # type: ignore[attr-defined]
        streamable_module = types.ModuleType("mcp.client.streamable_http")
        streamable_module.streamablehttp_client = streamable_client  # type: ignore[attr-defined]
        sse_module = types.ModuleType("mcp.client.sse")
        sse_module.sse_client = sse_client  # type: ignore[attr-defined]
        modules = {
            "mcp": mcp_module,
            "mcp.client": client_module,
            "mcp.client.streamable_http": streamable_module,
            "mcp.client.sse": sse_module,
        }
        cases = (
            ("http", "http://127.0.0.1:8080/mcp"),
            ("streamable-http", "http://localhost:8080/mcp"),
            ("sse", "https://mcp.internal.test/events"),
        )
        manager = MCPManager(self.project, self.user)
        private_resolution = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", 443))
        ]

        with patch.dict(sys.modules, modules), patch(
            "tools.harness.socket.getaddrinfo", return_value=private_resolution
        ):
            for transport, url in cases:
                with self.subTest(transport=transport, url=url):
                    manager.servers["blocked"] = {"type": transport, "url": url}
                    with self.assertRaises(PermissionError):
                        await manager.call_tool("blocked", "lookup", {})

        streamable_client.assert_not_called()
        sse_client.assert_not_called()

    async def test_lsp_fails_closed_without_starting_host_process(self) -> None:
        """Docker 缺失时 LSP 不得调用 ``create_subprocess_exec``。"""

        manager = LSPManager(self.project, self.user)
        spawn = AsyncMock()
        with patch("Agent.sandbox.shutil.which", return_value=None), patch(
            "Agent.integrations.asyncio.create_subprocess_exec", spawn
        ):
            with self.assertRaises(SandboxUnavailable):
                await manager.start("python")
        spawn.assert_not_awaited()

    async def test_stdio_mcp_sdk_receives_docker_and_clean_environment(self) -> None:
        """伪 SDK 应收到 Docker 命令以及白名单加显式配置形成的环境。"""

        captured: dict[str, Any] = {}

        class Parameters:
            """记录 MCP SDK 收到的 stdio 启动参数。"""

            def __init__(self, *, command: str, args: list[str], env: dict[str, str]) -> None:
                """保存命令、参数和环境，供断言检查。"""

                self.command = command
                self.args = args
                self.env = env

        class ClientSession:
            """不访问网络的最小异步 MCP ClientSession 假实现。"""

            def __init__(self, read_stream: object, write_stream: object) -> None:
                """接受 SDK 传输流但不消费它们。"""

                del read_stream, write_stream

            async def __aenter__(self) -> "ClientSession":
                """进入异步上下文并返回当前会话。"""

                return self

            async def __aexit__(self, *exc_info: object) -> None:
                """退出异步上下文，无需释放真实资源。"""

                del exc_info

            async def initialize(self) -> None:
                """模拟成功的 MCP 初始化握手。"""

            async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                """返回调用参数，证明代码已走到 SDK 会话层。"""

                return {"tool": tool, "arguments": arguments}

        @asynccontextmanager
        async def stdio_client(parameters: Parameters) -> AsyncIterator[tuple[object, object]]:
            """捕获启动参数并提供两个占位传输流。"""

            captured["parameters"] = parameters
            yield object(), object()

        mcp_module = types.ModuleType("mcp")
        mcp_module.__path__ = []  # type: ignore[attr-defined]
        mcp_module.ClientSession = ClientSession  # type: ignore[attr-defined]
        mcp_module.StdioServerParameters = Parameters  # type: ignore[attr-defined]
        client_module = types.ModuleType("mcp.client")
        client_module.__path__ = []  # type: ignore[attr-defined]
        stdio_module = types.ModuleType("mcp.client.stdio")
        stdio_module.stdio_client = stdio_client  # type: ignore[attr-defined]

        manager = MCPManager(self.project, self.user, sandbox=DockerSandbox("example/image:fixed"))
        modules = {"mcp": mcp_module, "mcp.client": client_module, "mcp.client.stdio": stdio_module}
        host = {"PATH": "safe-path", "AWS_SECRET_ACCESS_KEY": "must-not-leak"}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, host, clear=True), patch(
            "Agent.sandbox.shutil.which", return_value="docker"
        ):
            result = await manager.call_tool("local", "lookup", {"query": "x"})

        parameters = captured["parameters"]
        self.assertEqual(result, {"tool": "lookup", "arguments": {"query": "x"}})
        self.assertEqual(parameters.command, "docker")
        self.assertIn("python", parameters.args)
        self.assertIn("server.py", parameters.args)
        self.assertEqual(parameters.env["MCP_TOKEN"], "explicit-mcp-secret")
        self.assertEqual(parameters.env["PATH"], "safe-path")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", parameters.env)
        self.assertNotIn("explicit-mcp-secret", parameters.args)

    async def test_lsp_starts_wrapped_process_with_clean_environment(self) -> None:
        """LSP 初始化必须启动 Docker argv，并使用容器内工作区 URI。"""

        class FakeProcess:
            """满足 ``LSPProcess`` 保存需求的空进程对象。"""

        process = FakeProcess()
        manager = LSPManager(self.project, self.user, sandbox=DockerSandbox("example/image:fixed"))
        manager.request = AsyncMock(return_value={})  # type: ignore[method-assign]
        spawn = AsyncMock(return_value=process)
        host = {"PATH": "safe-path", "GITHUB_TOKEN": "must-not-leak"}
        with patch.dict(os.environ, host, clear=True), patch(
            "Agent.sandbox.shutil.which", return_value="docker"
        ), patch("Agent.integrations.asyncio.create_subprocess_exec", spawn):
            await manager.start("python")

        positional, keywords = spawn.call_args
        self.assertEqual(positional[0:2], ("docker", "run"))
        self.assertIn("pylsp", positional)
        self.assertIn("--stdio", positional)
        self.assertEqual(keywords["env"]["LSP_TOKEN"], "explicit-lsp-secret")
        self.assertNotIn("GITHUB_TOKEN", keywords["env"])
        manager.request.assert_awaited_once_with(
            "python", "initialize", {"processId": None, "rootUri": "file:///workspace", "capabilities": {}}
        )


if __name__ == "__main__":
    unittest.main()
