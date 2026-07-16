"""子代理 worktree 创建权限边界的回归测试。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Agent.config import RuntimeConfig
from Agent.permissions import CapabilityGrant
from Agent.runtime import AgentRuntime
from Agent.types import AgentDefinition, EventType, ModelOutput, ToolCall


class _UnusedProvider:
    """若权限边界失效才会被调用的离线模型替身。"""

    async def complete(self, messages, tools, *, temperature=0) -> ModelOutput:
        """返回固定文本；正常测试路径应在创建 worktree 前提前拒绝。"""

        del messages, tools, temperature
        return ModelOutput(content="不应执行")

    async def stream(self, messages, tools, *, temperature=0):
        """满足 Provider 协议但不产生真实网络流量。"""

        del messages, tools, temperature
        if False:
            yield ""


class _SequenceProvider:
    """按顺序返回离线模型结果，用于驱动子代理越权尝试。"""

    def __init__(self, outputs: list[ModelOutput]) -> None:
        """复制调用方结果，便于测试逐次弹出。"""

        self.outputs = list(outputs)

    async def complete(self, messages, tools, *, temperature=0) -> ModelOutput:
        """忽略请求正文并返回下一条预设结果。"""

        del messages, tools, temperature
        return self.outputs.pop(0)

    async def stream(self, messages, tools, *, temperature=0):
        """满足 Provider 协议；Runtime 当前不会消费该接口。"""

        output = await self.complete(messages, tools, temperature=temperature)
        yield output.content


class SubagentSecurityTests(unittest.TestCase):
    """验证公开子代理 API 不能绕过 Git 写操作审批。"""

    def test_direct_worktree_subagent_call_fails_closed(self) -> None:
        """没有审批回调时，直接调用 ``run_subagent`` 也不得创建 worktree。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath(".yy").mkdir()
            home = root / "home"
            with patch.dict(os.environ, {"YY_AGENT_HOME": str(home)}, clear=False):
                runtime = AgentRuntime(RuntimeConfig(project_root=root), provider=_UnusedProvider())
                runtime.agent_registry._agents["isolated"] = AgentDefinition(
                    name="isolated",
                    description="隔离测试",
                    prompt="检查文件",
                    isolation="worktree",
                )
                with patch.object(runtime, "_create_worktree") as create:
                    with self.assertRaises(PermissionError):
                        asyncio.run(runtime.run_subagent("isolated", "执行检查"))
                create.assert_not_called()

    def test_subagent_inherits_background_capability_grant(self) -> None:
        """恶意子模型调用能力包外工具时必须暂停，而不是使用自身默认模式放行。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath(".yy").mkdir()
            home = root / "home"
            provider = _SequenceProvider(
                [
                    ModelOutput(tool_calls=(ToolCall("child-call", "calculator", {"expression": "2+2"}),)),
                    ModelOutput(content="工具失败后仍试图宣称完成"),
                ]
            )
            config = RuntimeConfig(project_root=root, auto_memory=False)
            with patch.dict(os.environ, {"YY_AGENT_HOME": str(home)}, clear=False):
                runtime = AgentRuntime(config, provider=provider)
                runtime.agent_registry._agents["limited"] = AgentDefinition(
                    name="limited",
                    description="能力上限测试",
                    prompt="尝试计算",
                    tools=("calculator",),
                )
                grant = CapabilityGrant(tools=("agent_spawn",))
                with self.assertRaises(PermissionError):
                    asyncio.run(runtime.run_subagent("limited", "执行计算", grant))

                failed = [
                    row
                    for session in runtime.store.list_sessions()
                    for row in runtime.store.events(session["id"])
                    if row["type"] == EventType.TOOL_FAILED.value
                ]
                self.assertTrue(failed)
                self.assertTrue(failed[-1]["payload"]["metadata"]["needs_approval"])

    def test_agent_spawn_tool_propagates_grant_to_child_context(self) -> None:
        """经父 Runtime 工具链启动子代理时必须实际调用 runner 并传递同一个能力包。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath(".yy").mkdir()
            home = root / "home"
            provider = _SequenceProvider(
                [
                    ModelOutput(
                        tool_calls=(
                            ToolCall(
                                "parent-call",
                                "agent_spawn",
                                {"agent": "limited", "task": "执行受限检查"},
                            ),
                        )
                    ),
                    ModelOutput(content="父代理结束"),
                ]
            )
            config = RuntimeConfig(project_root=root, auto_memory=False)
            with patch.dict(os.environ, {"YY_AGENT_HOME": str(home)}, clear=False):
                runtime = AgentRuntime(config, provider=provider)
                grant = CapabilityGrant(tools=("agent_spawn",))
                received: list[tuple[str, str, CapabilityGrant | None]] = []

                async def recording_runner(agent: str, task: str, child_grant: CapabilityGrant | None) -> str:
                    """记录工具适配器交给真实子代理边界的三个参数。"""

                    received.append((agent, task, child_grant))
                    return "子代理完成"

                spawn_tool = runtime.tools.get("agent_spawn")
                self.assertIsNotNone(spawn_tool)
                spawn_tool.runner = recording_runner
                result = asyncio.run(
                    runtime.run(
                        "启动受限子代理",
                        capability_grant=grant,
                    )
                )

                self.assertTrue(result.completed)
                self.assertEqual(result.answer, "父代理结束")
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0][:2], ("limited", "执行受限检查"))
                self.assertIs(received[0][2], grant)
                self.assertFalse(any(event.type == EventType.TOOL_FAILED for event in result.events))
                self.assertFalse(provider.outputs)


if __name__ == "__main__":
    # 支持开发者单独运行该安全用例。
    unittest.main()
