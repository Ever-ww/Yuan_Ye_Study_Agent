from __future__ import annotations

import importlib.util
import asyncio
import sys
from pathlib import Path

import pytest

from Agent import EventType, load_runtime_config
from Agent.contracts import ModelReply
from Agent.models.providers import _openai_usage
from harness_runtime import (
    HarnessRuntimeResourceLoader,
    HarnessRuntimeTrigger,
    SessionPersistenceProjection,
)
from skill import SkillService


ROOT = Path(__file__).parents[1]


def _harness():
    path = ROOT / "harness-evolution" / "harness.py"
    name = "yy_harness_runtime_isolation_test"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CapturingProvider:
    streaming = False

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def complete(self, messages, tools):
        self.calls.append(([dict(item) for item in messages], [dict(item) for item in tools]))
        return ModelReply(text="completed")


def test_main_skill_catalog_cannot_discover_harness_resources(tmp_path: Path) -> None:
    service = SkillService(tmp_path, ROOT, ROOT)
    names = {item.name for item in service.catalog()}
    assert not any(name.startswith("harness-") for name in names)
    assert "repository-safety" not in names
    assert "tool-capability-evolution" not in names


def test_trigger_skill_roots_are_exact_and_cross_trigger_hidden(tmp_path: Path) -> None:
    loader = HarnessRuntimeResourceLoader(ROOT / "harness-evolution" / "runtime")
    expected = {
        HarnessRuntimeTrigger.MANUAL: {"repository-safety", "validated-repair", "hook-evolution"},
        HarnessRuntimeTrigger.ERROR: {"repository-safety", "validated-repair", "runtime-failure-repair"},
        HarnessRuntimeTrigger.CAPABILITY: {"repository-safety", "validated-repair", "tool-capability-evolution"},
        HarnessRuntimeTrigger.DREAM: {"repository-safety", "validated-repair", "conservative-dream-review"},
    }
    for trigger, names in expected.items():
        profile = loader.profile(trigger)
        skills = loader.build_skills(profile, agent_root=tmp_path, workspace_root=tmp_path)
        assert {item.name for item in skills.catalog()} == names


def test_ephemeral_context_is_provider_only_and_prompt_prefix_is_reused(tmp_path: Path) -> None:
    async def check() -> None:
        harness = _harness()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: isolated\n", encoding="utf-8")
        config = load_runtime_config(tmp_path, coding_source_root=ROOT)
        runtime = harness.create_coding_runtime(config, worktree)
        provider = CapturingProvider()
        runtime.provider = provider
        runtime.harness_dynamic_context.update(
            origin_refs={"origin_run_id": "run-1"},
            git_state={"head": "abc"},
            current_attempt=1,
            source_hash="a" * 64,
        )

        first = [event async for event in runtime.run_task("first query", runtime.coding_session_id)]
        assert first[-1].type is EventType.FINAL
        runtime.harness_dynamic_context.update(
            git_state={"head": "def"},
            current_attempt=2,
            source_hash="b" * 64,
        )
        second = [event async for event in runtime.run_task("second query", runtime.coding_session_id)]
        assert second[-1].type is EventType.FINAL

        first_messages, first_tools = provider.calls[0]
        second_messages, second_tools = provider.calls[1]
        assert first_messages[0] == second_messages[0]
        assert first_tools == second_tools
        assert "harness_runtime_context" in first_messages[-1]["content"]
        assert '"head":"abc"' in first_messages[-1]["content"]
        assert '"head":"def"' in second_messages[-1]["content"]
        assert "abc" not in second_messages[-2].get("content", "")
        assert runtime.prompts.rebuild_count == 1

        records = runtime.memory.session_records(runtime.coding_session_id)
        assert [item["content"] for item in records if item["role"] == "user"] == [
            "first query",
            "second query",
        ]
        assert "harness_runtime_context" not in str(records)
        await runtime.close()

    asyncio.run(check())


def test_session_projection_rejects_partial_or_full_envelope() -> None:
    with pytest.raises(ValueError, match="never be persisted"):
        SessionPersistenceProjection.assert_persistable(
            '<harness_runtime_context ephemeral="true">{}'
        )


def test_failure_projection_removes_provider_only_envelope() -> None:
    projected = SessionPersistenceProjection.from_runtime_messages([{
        "role": "user",
        "content": (
            "<user_query>\noriginal\n</user_query>\n\n"
            '<harness_runtime_context ephemeral="true">\n{}\n</harness_runtime_context>'
        ),
    }])
    assert projected == [{"role": "user", "content": "original"}]


def test_provider_usage_exposes_cached_input_tokens() -> None:
    usage = _openai_usage({
        "prompt_tokens": 1200,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 960},
    })
    assert usage is not None
    assert usage.input_tokens == 1200
    assert usage.cached_input_tokens == 960


def test_model_metric_reports_cache_hit_ratio() -> None:
    from Agent.contracts import ModelReply, TokenUsage
    from Agent.react.loop import _model_call_metric

    metric = _model_call_metric(
        1.0,
        1200,
        20,
        ModelReply(text="done", usage=TokenUsage(input_tokens=1200, cached_input_tokens=960)),
        ephemeral_context_tokens=80,
    )

    assert metric["input_tokens"]["cache_hit_ratio"] == 0.8
    assert metric["input_tokens"]["ephemeral_context"] == 80
