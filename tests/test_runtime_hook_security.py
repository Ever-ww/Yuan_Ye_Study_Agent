"""Runtime 与模型型 Hook 之间安全边界的确定性回归测试。

测试只使用临时项目、内存可控的假模型和假 Hook，不访问网络、Docker、用户配置或
真实模型。重点验证拒绝发生在权限询问和副作用之前，防止未来调整调用顺序时回归为
fail-open 行为。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Agent.config import RuntimeConfig
from Agent.hooks import HookEngine, HookOutcome
from Agent.permissions import ApprovalDecision, CapabilityGrant, plugin_capability_snapshot
from Agent.runtime import AgentRuntime
from Agent.storage import StateStore
from Agent.types import EventType, ModelOutput, RunEvent, ToolResult


class CountingProvider:
    """统计完整调用次数，并始终返回可结束 turn 的固定文本。"""

    def __init__(self) -> None:
        """创建尚未收到请求的模型替身。"""

        self.complete_calls = 0

    async def complete(self, messages, tools, *, temperature=0):
        """记录调用并返回无工具调用的确定性结果。"""

        del messages, tools, temperature
        self.complete_calls += 1
        return ModelOutput(content="完成", model="fake", provider="test")

    async def stream(self, messages, tools, *, temperature=0):
        """满足 ``ModelProvider`` 协议；本组测试不会消费流式接口。"""

        output = await self.complete(messages, tools, temperature=temperature)
        yield output.content


class FailingProvider(CountingProvider):
    """在模型请求边界抛出确定性异常，用于验证会话状态清理。"""

    async def complete(self, messages, tools, *, temperature=0):
        """记录调用后模拟 Provider 失败。"""

        del messages, tools, temperature
        self.complete_calls += 1
        raise RuntimeError("测试模型故障")


class RecordingTool:
    """带必填参数的无副作用测试工具，用计数器观察是否真正执行。"""

    name = "hook_security_tool"
    description = "Test that Hook-modified arguments are validated again."
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    risk = "high"
    sandboxed = False

    def __init__(self) -> None:
        """创建调用次数为零的工具替身。"""

        self.calls = 0

    async def run(self, arguments, context):
        """记录执行；若安全链正确，本测试中的非法改写永远不会到达这里。"""

        del arguments, context
        self.calls += 1
        return ToolResult("", self.name, "executed")


class ProjectedTool(RecordingTool):
    """用额外真实命令和配置哈希扩充审批上下文的测试工具。"""

    name = "projected_security_tool"

    def permission_arguments(self, arguments: dict) -> dict:
        """保留全部执行参数，并追加权限规则需要精确匹配的真实目标。"""

        return {**arguments, "command": "server --stdio", "config_hash": "fixed-hash"}


class NarrowingProjectionTool(RecordingTool):
    """故意从审批投影删除执行参数，用于验证 Runtime 会安全拒绝。"""

    name = "narrowing_security_tool"

    def permission_arguments(self, arguments: dict) -> dict:
        """返回危险的缩窄投影；生产工具绝不应采用这种实现。"""

        del arguments
        return {"command": "server --stdio"}


class RewriteArgumentsHook:
    """把原本合法的工具参数改写为缺少必填字段的对象。"""

    async def emit(self, event: str, payload: dict, *, depth: int = 0) -> HookOutcome:
        """仅在 ``PreToolUse`` 改写参数，其他生命周期事件原样允许。"""

        del depth
        if event == "PreToolUse":
            return HookOutcome(True, {**payload, "arguments": {}}, "测试非法参数改写")
        return HookOutcome(True, dict(payload))


class DenyCompactHook:
    """拒绝压缩并记录所有收到的生命周期事件。"""

    def __init__(self) -> None:
        """创建空事件轨迹。"""

        self.events: list[str] = []

    async def emit(self, event: str, payload: dict, *, depth: int = 0) -> HookOutcome:
        """拒绝 ``BeforeCompact``，其他事件保持允许。"""

        del depth
        self.events.append(event)
        if event == "BeforeCompact":
            return HookOutcome(False, dict(payload), "测试要求保留完整上下文")
        return HookOutcome(True, dict(payload))


class UnusedSandbox:
    """模型型 Hook 测试中不应被调用的沙箱替身。"""

    async def run(self, *args, **kwargs):
        """若测试意外进入命令型处理器，立即暴露错误。"""

        del args, kwargs
        raise AssertionError("模型型 Hook 不应执行沙箱命令")


class RuntimeHookSecurityTests(unittest.IsolatedAsyncioTestCase):
    """覆盖 Hook 参数改写、布尔决策和压缩拒绝三条安全不变量。"""

    def setUp(self) -> None:
        """创建隔离项目与用户状态目录，避免读取开发机器配置。"""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".git").mkdir()
        self.home = self.root / "home"
        self.environment = patch.dict(os.environ, {"YY_AGENT_HOME": str(self.home)}, clear=False)
        self.environment.start()

    def tearDown(self) -> None:
        """恢复环境变量并清理测试产生的 SQLite 文件。"""

        self.environment.stop()
        self.temporary.cleanup()

    def make_runtime(
        self,
        provider: CountingProvider,
        *,
        permission_mode: str = "risk-based",
        context_event_limit: int = 80,
        approval_callback=None,
    ) -> AgentRuntime:
        """以显式假依赖创建 Runtime，关闭与断言无关的自动记忆模型调用。"""

        config = RuntimeConfig(
            project_root=self.root,
            permission_mode=permission_mode,
            context_event_limit=context_event_limit,
            auto_memory=False,
        )
        store = StateStore(config.state_db)
        return AgentRuntime(config, provider=provider, store=store, approval_callback=approval_callback)

    async def test_hook_modified_arguments_are_revalidated_before_approval(self) -> None:
        """二次 Schema 校验失败时不得询问权限，更不得调用工具实现。"""

        approval_requests = []

        async def approve(request):
            """记录任何意外审批；安全实现不应调用该回调。"""

            approval_requests.append(request)
            return ApprovalDecision.ALLOW_ONCE

        runtime = self.make_runtime(CountingProvider(), permission_mode="review-all", approval_callback=approve)
        runtime.hooks = RewriteArgumentsHook()
        tool = RecordingTool()
        runtime.tools.register(tool)
        session = runtime.create_session(title="Hook 参数二次校验")

        result = await runtime._execute_tool(
            session,
            tool.name,
            "call-security",
            {"value": 1},
            None,
        )

        self.assertTrue(result.is_error)
        self.assertIn("Hook 修改后的工具参数校验失败", result.content)
        self.assertEqual(approval_requests, [])
        self.assertEqual(tool.calls, 0)

    async def test_model_hook_rejects_non_boolean_allow(self) -> None:
        """字符串 ``false`` 和数字真值都不能被宽松转换成放行决定。"""

        for invalid_allow in ("false", "true", 0, 1, None):
            with self.subTest(invalid_allow=invalid_allow):

                async def handler(prompt: str, payload: dict, value=invalid_allow) -> dict:
                    """返回带非法 allow 类型的模型型 Hook 结果。"""

                    del prompt, payload
                    return {"allow": value, "injected": "不应合并"}

                engine = HookEngine(self.root, UnusedSandbox(), prompt_handler=handler)
                engine.configs = [
                    {
                        "source": "test",
                        "hooks": {
                            "BeforeModel": [
                                {"matcher": "*", "hooks": [{"type": "prompt", "prompt": "检查请求"}]}
                            ]
                        },
                    }
                ]
                outcome = await engine.emit("BeforeModel", {"name": "fake-model"})

                self.assertFalse(outcome.allowed)
                self.assertIn("allow 必须是 JSON 布尔值", outcome.message)
                self.assertNotIn("injected", outcome.payload or {})

    async def test_permission_projection_is_used_and_cannot_hide_arguments(self) -> None:
        """审批应看到真实配置描述，同时拒绝任何删改最终执行参数的投影。"""

        approval_requests = []

        async def approve(request):
            """记录 Broker 收到的完整审批参数并允许本次调用。"""

            approval_requests.append(request)
            return ApprovalDecision.ALLOW_ONCE

        runtime = self.make_runtime(CountingProvider(), permission_mode="review-all", approval_callback=approve)
        projected = ProjectedTool()
        runtime.tools.register(projected)
        session = runtime.create_session(title="审批参数投影")
        result = await runtime._execute_tool(session, projected.name, "projected", {"value": 7}, None)
        self.assertFalse(result.is_error)
        self.assertEqual(approval_requests[0].arguments["value"], 7)
        self.assertEqual(approval_requests[0].arguments["command"], "server --stdio")
        self.assertEqual(approval_requests[0].arguments["config_hash"], "fixed-hash")

        narrowing = NarrowingProjectionTool()
        runtime.tools.register(narrowing)
        denied = await runtime._execute_tool(session, narrowing.name, "narrowing", {"value": 8}, None)
        self.assertTrue(denied.is_error)
        self.assertIn("不得删改最终执行参数", denied.content)
        self.assertEqual(len(approval_requests), 1)
        self.assertEqual(narrowing.calls, 0)

    async def test_before_compact_denial_skips_model_state_and_event(self) -> None:
        """压缩 Hook 拒绝后继续原上下文，且不得伪造任何压缩副作用。"""

        provider = CountingProvider()
        runtime = self.make_runtime(provider, context_event_limit=22)
        hooks = DenyCompactHook()
        runtime.hooks = hooks
        session = runtime.create_session(title="压缩拒绝")
        # 预置足够多的历史消息触发压缩，同时保持配置下限与算法保留窗口一致。
        for index in range(23):
            runtime.store.append_event(
                RunEvent(EventType.USER_MESSAGE, session.id, {"content": f"历史消息 {index}"})
            )

        events = [
            event
            async for event in runtime.run_turn("保留完整上下文", session_id=session.id)
        ]

        # 唯一模型调用应是正常回答；若仍执行摘要模型，这里会变成两次。
        self.assertEqual(provider.complete_calls, 1)
        self.assertNotIn("AfterCompact", hooks.events)
        self.assertFalse(any(event.type == EventType.COMPACTED for event in events))
        final = next(event for event in events if event.type == EventType.FINAL)
        session = runtime.store.get_session(final.session_id)
        self.assertIsNotNone(session)
        self.assertEqual(session.summary, "")

    async def test_error_and_plugin_pin_mismatch_restore_idle_session(self) -> None:
        """错误路径恢复会话状态，旧 plugin_versions 任务仍可校验并继续运行。"""

        failing = self.make_runtime(FailingProvider())
        session = failing.create_session(title="模型失败")
        events = [event async for event in failing.run_turn("触发错误", session_id=session.id)]
        self.assertTrue(any(event.type == EventType.ERROR for event in events))
        self.assertEqual(failing.store.get_session(session.id).status, "idle")

        provider = CountingProvider()
        pinned = self.make_runtime(provider)
        grant = CapabilityGrant(tools=("read_file",), plugin_versions={"missing@market": "old-hash"})
        blocked = [event async for event in pinned.run_turn("后台任务", capability_grant=grant)]
        error = next(event for event in blocked if event.type == EventType.ERROR)
        self.assertTrue(error.payload.get("needs_approval"))
        self.assertEqual(provider.complete_calls, 0)
        self.assertEqual(pinned.store.get_session(error.session_id).status, "idle")

        compatible_provider = CountingProvider()
        compatible = self.make_runtime(compatible_provider)

        def legacy_plugins(*, enabled_only: bool = False):
            """模拟旧任务创建时已经存在且内容未变化的插件。"""

            self.assertTrue(enabled_only)
            return [{"id": "legacy@market", "content_hash": "same-hash", "trusted_components_json": "[]"}]

        compatible.plugins.installed = legacy_plugins
        legacy_grant = CapabilityGrant(
            tools=("read_file",),
            plugin_versions={"legacy@market": "same-hash"},
        )
        legacy_result = await compatible.run("兼容旧任务", capability_grant=legacy_grant)
        self.assertTrue(legacy_result.completed)
        self.assertEqual(compatible_provider.complete_calls, 1)

    async def test_plugin_capability_snapshot_detects_set_and_trust_changes(self) -> None:
        """新增、禁用或改变插件信任组件都必须在模型和插件 Hook 执行前暂停后台任务。"""

        baseline_rows = [
            {
                "id": "study-tools@local",
                "content_hash": "hash-v1",
                "trusted_components_json": '["mcp"]',
            }
        ]
        changed_cases = {
            "空集合后新增插件": (
                [],
                [{"id": "new-hooks@local", "content_hash": "new-hash", "trusted_components_json": '["hooks"]'}],
            ),
            "已有集合新增插件": (
                baseline_rows,
                [
                    *baseline_rows,
                    {"id": "new-hooks@local", "content_hash": "new-hash", "trusted_components_json": '["hooks"]'},
                ],
            ),
            "禁用原插件": (baseline_rows, []),
            "改变信任组件": (
                baseline_rows,
                [{"id": "study-tools@local", "content_hash": "hash-v1", "trusted_components_json": '["hooks", "mcp"]'}],
            ),
        }

        for label, (creation_rows, current_rows) in changed_cases.items():
            with self.subTest(label=label):
                provider = CountingProvider()
                runtime = self.make_runtime(provider)
                grant = CapabilityGrant(
                    tools=("read_file",),
                    plugin_capability_snapshot=plugin_capability_snapshot(creation_rows),
                )

                def installed(*, enabled_only: bool = False, rows=current_rows):
                    """返回当前子场景的启用插件集合，模拟任务创建后的状态变化。"""

                    self.assertTrue(enabled_only)
                    return list(rows)

                runtime.plugins.installed = installed
                events = [event async for event in runtime.run_turn("后台任务", capability_grant=grant)]
                error = next(event for event in events if event.type == EventType.ERROR)
                self.assertTrue(error.payload.get("needs_approval"))
                self.assertIn("插件集合、内容或信任状态已变化", error.payload["error"])
                self.assertEqual(provider.complete_calls, 0)
                self.assertEqual(runtime.store.get_session(error.session_id).status, "idle")

    async def test_cron_create_tool_persists_complete_plugin_snapshot(self) -> None:
        """Runtime 注册的 Cron 工具参数名应一致，并把完整插件快照写入 capability_json。"""

        async def approve(_request):
            """批准本测试唯一一次高风险 Cron 创建。"""

            return ApprovalDecision.ALLOW_ONCE

        runtime = self.make_runtime(CountingProvider(), approval_callback=approve)
        cron_tool = runtime.tools.get("cron_create")
        self.assertIsNotNone(cron_tool)
        snapshot = plugin_capability_snapshot(
            [{"id": "pinned@local", "content_hash": "tree-hash", "trusted_components_json": '["mcp", "hooks"]'}]
        )
        cron_tool.plugin_capability_snapshot = snapshot
        session = runtime.create_session(title="Cron 快照")
        arguments = {
            "cron": "*/5 * * * *",
            "prompt": "检查状态",
            "timezone": "Asia/Shanghai",
            "recurring": True,
            "tools": ["read_file"],
            "paths": [str(self.root.resolve())],
            "domains": ["example.com"],
            "command_prefixes": ["git status"],
        }
        result = await runtime._execute_tool(session, "cron_create", "cron-call", arguments, None)
        self.assertFalse(result.is_error, result.content)
        row = runtime.scheduler.list_schedules()[0]
        persisted = json.loads(row["capability_json"])
        self.assertEqual(persisted["plugin_capability_snapshot"], snapshot)
        self.assertEqual(persisted["plugin_versions"], {})
        self.assertEqual(persisted["tools"], ["read_file"])
        self.assertEqual(persisted["paths"], [str(self.root.resolve())])
        self.assertEqual(persisted["domains"], ["example.com"])
        self.assertEqual(persisted["command_prefixes"], ["git status"])


if __name__ == "__main__":
    unittest.main()
