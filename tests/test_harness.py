"""Harness 核心行为与安全边界的无外部依赖回归测试。

测试统一使用临时项目根和临时 ``YY_AGENT_HOME``，不会读取开发者真实配置、API Key 或
用户记忆。模型、时钟相关输入和审批通过 Fake/直接调用注入，因此普通 CI 不需要网络、
Docker 或真实模型账号。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from Agent.config import RuntimeConfig, load_runtime_config
from Agent.permissions import ApprovalDecision, CapabilityGrant, PermissionBroker
from Agent.runtime import AgentRuntime
from Agent.scheduler import SQLiteSchedulerStore, _cron_valid
from Agent.storage import StateStore
from Agent.teams import TeamStore
from Agent.types import ModelOutput, RunEvent, ToolCall
from memory.store import CorpusStore, SQLiteMemoryStore
from skills.registry import PluginManager, SkillRegistry, validate_skill
from tools.harness import PathPolicy, _enforce_domain_allowlist, _validate_public_url


class FakeProvider:
    """按预设顺序返回 ``ModelOutput`` 的确定性模型替身。"""

    def __init__(self, outputs):
        """复制输出序列，避免测试运行时修改调用方传入的列表。"""
        self.outputs = list(outputs)
        self.requests = []

    async def complete(self, messages, tools, *, temperature=0):
        """记录消息快照并弹出下一条结果，用于驱动工具循环和检查会话恢复。"""
        self.requests.append(list(messages))
        del tools, temperature
        return self.outputs.pop(0)

    async def stream(self, messages, tools, *, temperature=0):
        """满足 ModelProvider 协议；测试主循环当前不会消费这个方法。"""
        output = await self.complete(messages, tools, temperature=temperature)
        yield output.content


class HarnessTests(unittest.TestCase):
    """覆盖配置、权限、存储、Runtime、扩展安装和任务编排。"""

    def setUp(self):
        """为每个用例创建互不共享的仓库标记、用户目录和 SQLite。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # find_project_root 通过 .git 或 .yy 确定边界；空目录标记足以模拟仓库。
        (self.root / ".git").mkdir()
        self.home = self.root / "home"
        self.env = patch.dict(os.environ, {"YY_AGENT_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.config = RuntimeConfig(project_root=self.root)
        self.store = StateStore(self.config.state_db)

    def tearDown(self):
        """先恢复环境变量，再删除可能仍包含 SQLite 文件的临时目录。"""
        self.env.stop()
        self.temporary.cleanup()

    def test_merged_public_api_keeps_legacy_and_harness_types_distinct(self):
        """公共懒导出不得混淆新旧同名 Result 与 ToolRegistry。"""
        from Agent import Agent as LegacyAgent
        from Agent import AgentResult as LegacyAgentResult
        from Agent import AgentRuntime as PublicRuntime
        from Agent import LegacyAgentResult as LegacyResultAlias
        from Agent import RuntimeResult
        from Agent import ToolRegistry as LegacyToolRegistry
        from tools import AsyncToolRegistry

        self.assertIs(PublicRuntime, AgentRuntime)
        self.assertEqual(LegacyAgent.__module__, "Agent.legacy")
        self.assertIs(LegacyAgentResult, LegacyResultAlias)
        self.assertIsNot(LegacyAgentResult, RuntimeResult)
        self.assertEqual(AsyncToolRegistry.__module__, "tools.harness")
        self.assertEqual(LegacyToolRegistry.__module__, "Agent.legacy")

    def test_merged_packages_are_safe_in_cold_import_orders(self):
        """在全新解释器中验证高风险导入顺序不会产生部分初始化循环。"""
        root = Path(__file__).resolve().parents[1]
        orders = (
            ("tools.harness", "Agent.runtime", "Agent.legacy", "memory.store", "skills.registry", "model_choice.provider"),
            ("memory.store", "Agent.subagents", "skills.registry", "model_choice.provider", "tools.harness", "Agent.runtime"),
            ("skills.registry", "Agent.teams", "model_choice.provider", "tools.harness", "memory.store", "Agent.runtime"),
            ("model_choice.provider", "Agent.runtime", "Agent.legacy", "tools.harness", "skills.registry", "memory.store"),
        )
        for modules in orders:
            # 必须使用子进程；同一测试进程已经缓存模块，无法暴露冷启动循环依赖。
            code = "; ".join(f"import {module}" for module in modules)
            completed = subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_model_example_import_has_no_network_side_effect(self):
        """示例模块被文档或测试工具导入时，不得立即调用真实模型网络。"""

        root = Path(__file__).resolve().parents[1]
        # 在全新解释器里先封锁 urllib；旧实现若在 import 阶段调用 chat，子进程会立刻失败。
        code = (
            "from unittest.mock import patch; "
            "guard = patch('urllib.request.urlopen', side_effect=AssertionError('unexpected network')); "
            "guard.start(); import model_choice.example; guard.stop()"
        )
        completed = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_config_precedence(self):
        """调用方覆盖应高于项目配置，项目配置应高于用户默认。"""
        (self.home).mkdir(parents=True, exist_ok=True)
        (self.home / "settings.json").write_text('{"profile":"study","max_steps":3}', encoding="utf-8")
        (self.root / ".yy").mkdir()
        (self.root / ".yy" / "settings.json").write_text('{"max_steps":5}', encoding="utf-8")
        config = load_runtime_config(self.root, overrides={"max_steps": 7})
        self.assertEqual(config.profile, "study")
        self.assertEqual(config.max_steps, 7)

    def test_runtime_config_rejects_invalid_compaction_and_search_template(self):
        """直接 Python API 也必须拒绝无效上下文上限和缺少查询占位符的端点。"""

        with self.assertRaises(ValueError):
            RuntimeConfig(project_root=self.root, context_event_limit=21)
        with self.assertRaises(ValueError):
            RuntimeConfig(project_root=self.root, web_search_url="https://search.example/query")
        valid = RuntimeConfig(
            project_root=self.root,
            web_search_url="https://search.example/?q={query}",
        )
        self.assertIn("{query}", valid.web_search_url or "")

    def test_path_policy_blocks_escape_and_secrets(self):
        """文件路径和域名后缀匹配不能被常见前缀欺骗绕过。"""
        policy = PathPolicy(self.root)
        with self.assertRaises(PermissionError):
            policy.resolve("../escape.txt", for_write=True)
        with self.assertRaises(PermissionError):
            policy.resolve(".env", for_write=True)
        _enforce_domain_allowlist("https://docs.example.com/page", ("example.com",))
        with self.assertRaises(PermissionError):
            _enforce_domain_allowlist("https://example.com.attacker.test/", ("example.com",))

    def test_public_url_rejects_ip_literals_before_dns(self):
        """公网或私网 IP 字面量都不得绕过域名授权与 DNS 审计。"""

        with patch("tools.harness.socket.getaddrinfo") as resolver:
            with self.assertRaises(PermissionError):
                _validate_public_url("https://8.8.8.8/search")
            with self.assertRaises(PermissionError):
                _validate_public_url("http://127.0.0.1/")
            with self.assertRaises(PermissionError):
                _validate_public_url("https://134744072/")
        resolver.assert_not_called()

    def test_memory_and_corpus_are_separate(self):
        """长期记忆与资料库必须使用不同索引，避免资料文本污染用户事实。"""
        memory = SQLiteMemoryStore(self.store, self.config.state_dir / "memory")
        memory_id = memory.add("Always use focused unit tests", scope="project", source="test")
        self.assertEqual(memory.search("focused unit")[0]["id"], memory_id)
        document = self.root / "paper" / "notes.md"
        document.parent.mkdir()
        document.write_text("The orchard protocol uses spaced repetition.", encoding="utf-8")
        corpus = CorpusStore(self.store)
        indexed = corpus.index_path(document.parent)
        self.assertEqual(indexed["documents"], 1)
        self.assertIn("notes.md", corpus.search("orchard protocol")[0]["path"])
        self.assertFalse(memory.search("orchard protocol"))

    def test_skill_discovery_and_validation(self):
        """合法 frontmatter 能被发现，并且正文只在显式 load 时读取。"""
        path = self.root / ".yy" / "skills" / "test-skill"
        path.mkdir(parents=True)
        path.joinpath("SKILL.md").write_text("---\nname: test-skill\ndescription: Use for harness tests.\n---\nDo the test.\n", encoding="utf-8")
        self.assertEqual(validate_skill(path).name, "test-skill")
        registry = SkillRegistry(self.config)
        self.assertEqual(registry.discover()[0].load(), "Do the test.")

    def test_permission_modes_and_persistent_rule(self):
        """覆盖低风险自动允许、项目规则复用和后台能力包上限。"""
        async def allow(request):
            """模拟用户把一次写文件调用保存为项目级允许规则。"""
            self.assertEqual(request.tool, "write_file")
            return ApprovalDecision.ALLOW_PROJECT

        broker = PermissionBroker(self.store, self.root, "risk-based", allow)
        self.assertEqual(asyncio.run(broker.authorize("read_file", {"path": "a"}, risk="low", sandboxed=False))[0], True)
        self.assertEqual(asyncio.run(broker.authorize("write_file", {"path": "a"}, risk="medium", sandboxed=False))[0], True)
        plan = PermissionBroker(self.store, self.root, "plan", None)
        self.assertTrue(asyncio.run(plan.authorize("task_list", {}, risk="low", sandboxed=False))[0])
        # 已持久化的项目 allow 不能把 plan 从只读探索模式扩大为可写模式。
        self.assertFalse(asyncio.run(plan.authorize("write_file", {"path": "a"}, risk="medium", sandboxed=False))[0])
        # 创建/更新任务会写 SQLite，即使风险级别被误标为 low，也不能在只读 plan 模式放行。
        self.assertFalse(asyncio.run(plan.authorize("task_create", {"title": "x"}, risk="low", sandboxed=False))[0])
        broker2 = PermissionBroker(self.store, self.root, "review-all", None)
        # 前一个 broker 写入项目级 allow，新的 review-all broker 也应按确定性规则复用。
        self.assertEqual(asyncio.run(broker2.authorize("write_file", {"path": "a"}, risk="medium", sandboxed=False))[0], True)
        self.assertFalse(CapabilityGrant().allows("read_file", {"path": "a"}))
        grant = CapabilityGrant(tools=("write_file",), paths=(str((self.root / "a").resolve()),))
        unattended = PermissionBroker(self.store, self.root, "risk-based", None)
        self.assertTrue(asyncio.run(unattended.authorize("write_file", {"path": "a"}, risk="medium", sandboxed=False, grant=grant))[0])
        self.assertFalse(asyncio.run(unattended.authorize("shell", {"argv": ["rm", "-rf", "."], "writable": True}, risk="high", sandboxed=True, grant=CapabilityGrant(tools=("shell",))))[0])

    def test_permission_rule_priority_forces_ask_before_allow_and_mode(self):
        """同一调用命中多条规则时应遵循 deny → ask → allow，ask 不得被自动模式绕过。"""

        specifier = json.dumps({"path": "a"}, ensure_ascii=False, sort_keys=True)
        for rule_id, effect, created_at in (
            ("allow-rule", "allow", "2025-01-01T00:00:00+00:00"),
            ("ask-rule", "ask", "2025-01-02T00:00:00+00:00"),
        ):
            self.store.execute(
                "INSERT INTO permission_rules VALUES(?,?,?,?,?,?,?)",
                (rule_id, effect, "project", str(self.root.resolve()), "read_file", specifier, created_at),
            )

        requests = []

        async def approve(request):
            """记录是否真正进入审批回调，并批准本次调用。"""

            requests.append(request)
            return ApprovalDecision.ALLOW_ONCE

        broker = PermissionBroker(self.store, self.root, "risk-based", approve)
        allowed, _ = asyncio.run(broker.authorize("read_file", {"path": "a"}, risk="low", sandboxed=False))
        self.assertTrue(allowed)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].tool, "read_file")

        # 无交互前端时也不能退回 risk-based 的低风险自动允许，必须安全拒绝。
        unattended = PermissionBroker(self.store, self.root, "risk-based", None)
        unattended_allowed, _ = asyncio.run(
            unattended.authorize("read_file", {"path": "a"}, risk="low", sandboxed=False)
        )
        self.assertFalse(unattended_allowed)

        # 后加入 deny 后，它必须压过 ask；拒绝应直接完成，不得再次调用审批回调。
        self.store.execute(
            "INSERT INTO permission_rules VALUES(?,?,?,?,?,?,?)",
            (
                "deny-rule", "deny", "project", str(self.root.resolve()),
                "read_file", specifier, "2025-01-03T00:00:00+00:00",
            ),
        )
        denied, _ = asyncio.run(broker.authorize("read_file", {"path": "a"}, risk="low", sandboxed=False))
        self.assertFalse(denied)
        self.assertEqual(len(requests), 1)

    def test_critical_approval_cannot_be_persisted(self):
        """即使回调选择项目级允许，关键桌面动作也只能降级为单次批准。"""
        async def approve_project(_request):
            """故意请求过宽的项目授权，验证 Broker 会将其降级。"""
            return ApprovalDecision.ALLOW_PROJECT

        broker = PermissionBroker(self.store, self.root, "risk-based", approve_project)
        allowed, _ = asyncio.run(broker.authorize("desktop", {"operation": "list_windows"}, risk="critical", sandboxed=False))
        self.assertTrue(allowed)
        self.assertFalse(self.store.query("SELECT * FROM permission_rules WHERE tool='desktop'"))

    def test_runtime_tool_loop_and_event_persistence(self):
        """模型工具调用应被执行、反馈并完整写入事件表。"""
        provider = FakeProvider([
            ModelOutput(tool_calls=(ToolCall("call-1", "calculator", {"expression": "(25+17)*3"}),)),
            ModelOutput(content="126", model="fake", provider="test"),
        ])
        runtime = AgentRuntime(self.config, provider=provider, store=self.store)
        result = asyncio.run(runtime.run("calculate"))
        self.assertTrue(result.completed)
        self.assertEqual(result.answer, "126")
        event_types = [row["type"] for row in self.store.events(result.session_id)]
        streamed_types = [event.type.value if hasattr(event.type, "value") else event.type for event in result.events]
        self.assertIn("tool.requested", streamed_types)
        self.assertIn("approval.resolved", streamed_types)
        self.assertIn("tool.completed", event_types)

    def test_session_rebuild_restores_tool_action_and_observation(self):
        """跨 turn 重建上下文时，工具请求参数和 Observation 必须成对恢复。"""

        provider = FakeProvider(
            [
                ModelOutput(
                    tool_calls=(
                        ToolCall("call-1", "calculator", {"expression": "2+2"}),
                        ToolCall("call-2", "calculator", {"expression": "3+3"}),
                    )
                ),
                ModelOutput(content="4 和 6", model="fake", provider="test"),
                ModelOutput(content="继续完成", model="fake", provider="test"),
            ]
        )
        runtime = AgentRuntime(self.config, provider=provider, store=self.store)
        first = asyncio.run(runtime.run("calculate"))
        asyncio.run(runtime.run("continue", session_id=first.session_id))

        rebuilt = provider.requests[-1]
        contents = [message.content for message in rebuilt]
        action_indexes = [index for index, content in enumerate(contents) if '"action": "calculator"' in content]
        first_observation = contents.index("Observation: 4")
        second_observation = contents.index("Observation: 6")
        self.assertEqual(len(action_indexes), 2)
        self.assertLess(action_indexes[0], first_observation)
        self.assertLess(first_observation, action_indexes[1])
        self.assertLess(action_indexes[1], second_observation)

    def test_rewind_restores_only_agent_change(self):
        """文件未被用户再次修改时，rewind 应恢复 Agent 写入前的字节。"""
        target = self.root / "note.txt"
        target.write_text("before", encoding="utf-8")

        async def allow(_request):
            """允许本用例唯一一次内置写文件调用。"""
            return ApprovalDecision.ALLOW_ONCE

        provider = FakeProvider([
            ModelOutput(tool_calls=(ToolCall("call-1", "write_file", {"path": "note.txt", "content": "after"}),)),
            ModelOutput(content="done"),
        ])
        runtime = AgentRuntime(self.config, provider=provider, store=self.store, approval_callback=allow)
        result = asyncio.run(runtime.run("edit note"))
        self.assertEqual(target.read_text(), "after")
        outcome = asyncio.run(runtime.rewind(result.session_id, 1))
        self.assertTrue(outcome["ok"])
        self.assertEqual(target.read_text(), "before")

    def test_rewind_preflights_all_changes_before_restoring(self):
        """任一目标出现并发修改时必须整体停止，不能先回滚部分文件。"""
        session = self.store.create_session(str(self.root), "general")
        self.store.append_event(RunEvent("message.user", session.id, {"content": "edit"}))
        first, second = self.root / "first.txt", self.root / "second.txt"
        first.write_text("after-1", encoding="utf-8")
        second.write_text("after-2", encoding="utf-8")
        self.store.record_file_change(session.id, str(first), b"before-1", b"after-1")
        self.store.record_file_change(session.id, str(second), b"before-2", b"after-2")
        second.write_text("user-change", encoding="utf-8")
        # 第一文件仍匹配 after 哈希；若实现边检查边恢复，它会被错误提前改回。
        provider = FakeProvider([])
        runtime = AgentRuntime(self.config, provider=provider, store=self.store)
        outcome = asyncio.run(runtime.rewind(session.id, 0))
        self.assertFalse(outcome["ok"])
        self.assertEqual(first.read_text(), "after-1")

    def test_rewind_compensates_unexpected_write_failure(self):
        """预检后若某个文件写入失败，已恢复的其他文件和数据库标记必须被补偿。"""

        session = self.store.create_session(str(self.root), "general")
        self.store.append_event(RunEvent("message.user", session.id, {"content": "edit"}))
        first, second = self.root / "first.txt", self.root / "second.txt"
        first.write_bytes(b"after-1")
        second.write_bytes(b"after-2")
        self.store.record_file_change(session.id, str(first), b"before-1", b"after-1")
        self.store.record_file_change(session.id, str(second), b"before-2", b"after-2")
        runtime = AgentRuntime(self.config, provider=FakeProvider([]), store=self.store)
        replace_snapshot = runtime._replace_snapshot

        def fail_first(path, content):
            """让第二个目标恢复成功后在第一个目标处失败，以覆盖反向补偿路径。"""

            if path == first and content == b"before-1":
                raise OSError("simulated write failure")
            return replace_snapshot(path, content)

        with patch.object(runtime, "_replace_snapshot", side_effect=fail_first):
            with self.assertRaises(OSError):
                asyncio.run(runtime.rewind(session.id, 0))
        self.assertEqual(first.read_bytes(), b"after-1")
        self.assertEqual(second.read_bytes(), b"after-2")
        states = self.store.query(
            "SELECT reverted FROM file_changes WHERE session_id=? ORDER BY created_at",
            (session.id,),
        )
        self.assertEqual([row["reverted"] for row in states], [0, 0])

    def test_cron_and_team_dependency(self):
        """验证 Cron 错过策略以及 Team 任务依赖的领取门槛。"""
        self.assertTrue(_cron_valid("*/5 * * * *"))
        schedules = SQLiteSchedulerStore(self.store)
        schedule_id = schedules.add_schedule({"cron": "*/5 * * * *", "prompt": "check", "timezone": "Asia/Shanghai"})
        self.assertEqual(schedules.list_schedules()[0]["id"], schedule_id)
        one_shot = schedules.add_schedule({"cron": "*/5 * * * *", "prompt": "once", "timezone": "Asia/Shanghai", "recurring": False})
        self.store.execute("UPDATE schedules SET next_run=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), one_shot))
        schedules.due()
        # 过期较久的一次性任务不能静默补跑，应进入 needs_approval。
        self.assertEqual(self.store.query("SELECT status FROM schedules WHERE id=?", (one_shot,))[0]["status"], "needs_approval")
        teams = TeamStore(self.store)
        team = teams.create_team("test-team")
        first = teams.add_task(team, "first")
        second = teams.add_task(team, "second", dependencies=[first])
        self.assertFalse(teams.claim(team, second, "worker"))
        self.assertTrue(teams.claim(team, first, "worker"))
        teams.complete(team, first, "ok")
        self.assertTrue(teams.claim(team, second, "worker"))

    def test_local_plugin_marketplace_install_is_pinned_and_untrusted(self):
        """本地市场安装也必须生成内容哈希，且可执行组件初始为空信任。"""
        market = self.root / "market"
        plugin = market / "plugins" / "sample"
        (market / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / "skills" / "sample-skill").mkdir(parents=True)
        (market / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"plugins": [{"name": "sample", "source": "./plugins/sample", "version": "1.0.0"}]}), encoding="utf-8"
        )
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "sample", "version": "1.0.0"}), encoding="utf-8"
        )
        (plugin / "skills" / "sample-skill" / "SKILL.md").write_text(
            "---\nname: sample-skill\ndescription: Sample plugin skill.\n---\nUse it.\n", encoding="utf-8"
        )
        manager = PluginManager(self.config, self.store)
        manager.add_marketplace(str(market), "local-market")
        installed = manager.install("sample@local-market")
        self.assertEqual(installed["trusted_components"], [])
        self.assertTrue(Path(installed["path"]).exists())
        self.assertEqual(len(installed["hash"]), 64)


if __name__ == "__main__":
    unittest.main()
