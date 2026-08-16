from __future__ import annotations

import asyncio
from pathlib import Path

from Agent import AgentRuntime, load_runtime_config
from Agent.contracts import ModelReply
from memory import MemoryStore
from memory.persistence import SessionPersistenceProjection
from skill import SkillService
from tool import AsyncToolRegistry, ToolContext
from tools.skill_read import SkillReadTool


class CapturingProvider:
    streaming = False

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def complete(self, messages, tools):
        self.calls.append(([dict(item) for item in messages], [dict(item) for item in tools]))
        return ModelReply(text="done")


def test_main_agent_uses_stable_prefix_and_provider_only_dynamic_tail(tmp_path: Path) -> None:
    async def check() -> None:
        config = load_runtime_config(tmp_path)
        memory = MemoryStore(config.memory_dir)
        provider = CapturingProvider()
        runtime = AgentRuntime(
            config,
            provider=provider,
            memory=memory,
            tools=AsyncToolRegistry(),
            enable_sandbox=False,
            enable_subagent=False,
            enable_references=False,
            enable_paper_library=False,
        )

        first = await runtime.run("first query")
        memory.runtime_notice = "dynamic notice"
        await runtime.run("second query", first.session_id)
        third = await runtime.run("third query")

        first_messages, first_tools = provider.calls[0]
        second_messages, second_tools = provider.calls[1]
        third_messages, third_tools = provider.calls[2]
        assert first_messages[0] == second_messages[0] == third_messages[0]
        assert first_tools == second_tools == third_tools == []
        assert "Session ID：" not in first_messages[0]["content"]
        assert "分段绝对路径：" not in first_messages[0]["content"]
        assert str(config.workspace_root) not in first_messages[0]["content"]

        assert first_messages[-1]["content"].startswith("<user_query>\nfirst query\n</user_query>")
        assert '<agent_runtime_context ephemeral="true">' in first_messages[-1]["content"]
        assert f'"session_id":"{first.session_id}"' in first_messages[-1]["content"]
        assert f'"session_id":"{third.session_id}"' in third_messages[-1]["content"]
        assert '"runtime_notice":"dynamic notice"' in second_messages[-1]["content"]
        assert "dynamic notice" not in second_messages[0]["content"]
        assert "agent_runtime_context" not in second_messages[-2].get("content", "")

        records = memory.session_records(first.session_id)
        assert [item["content"] for item in records if item["role"] == "user"] == [
            "first query",
            "second query",
        ]
        assert "agent_runtime_context" not in str(records)
        # run() closes each Trace; rebuilt snapshots remain byte-identical across all three traces.
        assert runtime.prompts.system.rebuild_count == 3
        await runtime.close()

    asyncio.run(check())


def test_main_skill_read_returns_stable_reference_after_first_load(tmp_path: Path) -> None:
    async def check() -> None:
        source_root = tmp_path / "source"
        workspace = tmp_path / "workspace"
        skill_root = source_root / "skills" / "cache-demo"
        skill_root.mkdir(parents=True)
        workspace.mkdir()
        (skill_root / "SKILL.md").write_text(
            "---\nname: cache-demo\ndescription: cache test skill\nlicense: MIT\n---\n\nbody\n",
            encoding="utf-8",
        )
        service = SkillService(tmp_path, workspace, source_root)
        snapshot = service.catalog_snapshot()
        service.bind_session("session", snapshot)
        tool = SkillReadTool(service)
        context = ToolContext(project_root=workspace, session_id="session")

        first = await tool.run({"name": "cache-demo"}, context)
        second = await tool.run({"name": "cache-demo"}, context)

        assert "body" in first
        assert second.startswith("skill-ref:cache-demo:SKILL.md:")
        assert tool.cache_misses == 1
        assert tool.cache_hits == 1

    asyncio.run(check())


def test_ephemeral_context_is_rejected_inside_tool_arguments() -> None:
    import pytest

    with pytest.raises(ValueError, match="must never be persisted"):
        SessionPersistenceProjection.assert_no_ephemeral({
            "nested": ['<agent_runtime_context ephemeral="true">'],
        })
