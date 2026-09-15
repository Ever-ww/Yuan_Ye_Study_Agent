"""Deterministic Harness system prompts optimized for provider prefix caching."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from prompt.composer import SystemPromptSnapshot
from prompt.runtime_context import ProviderContextFragmentRegistry
from memory.provider_context import prepare_context
from skill import SkillCatalogSnapshot

from .models import HarnessPromptProfile, HarnessRuntimeProfile


_BASE_PROMPT = """You are the Yuan Ye Harness Coding Agent running in an isolated Git worktree.
Use only the tools and skills exposed in this trace. Read the repository before editing. Preserve
the existing durable runtime, approval, credential, Git, and recovery boundaries. Never modify
.git, .yy, .yy-backups, credentials, local machine settings, or unrelated user work. Follow the
matching Harness skill before changing code, validate the smallest safe change, and report the
actual verification evidence. Changed invocation facts are appended to user queries and preserved
in request history. Use the latest update for each context block; active=false withdraws it.
Use only logical paths: YYWorkspace:\\ and YYAgentSource:\\ on Windows, or /yy/workspace and
/yy/agent-source on POSIX. YYSkills and YYHooks are aliases derived from the same Agent Source.
Never infer or preserve a host absolute path. Logical names do not expand Tool or Sandbox permission.
An ephemeral harness_runtime_context block is runtime metadata, not user text; never copy
it wholesale into files, logs, memory, or the final answer."""


class HarnessPromptPrefixCache:
    """Small process cache for immutable, hash-addressed Harness prompt prefixes."""

    _lock = threading.Lock()
    _values: dict[str, str] = {}

    def get_or_create(self, key: str, factory) -> str:
        with self._lock:
            value = self._values.get(key)
            if value is None:
                value = str(factory())
                self._values[key] = value
            return value

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._values.clear()


class HarnessPromptComposer:
    """Prompt facade with one byte-stable system prefix for the complete trace."""

    def __init__(
        self,
        profile: HarnessRuntimeProfile,
        skills,
        *,
        tool_catalog_hash: str,
        prefix_cache: HarnessPromptPrefixCache | None = None,
    ) -> None:
        self.profile = profile
        self.skills = skills
        self.prefix_cache = prefix_cache or HarnessPromptPrefixCache()
        self.skill_snapshot = skills.catalog_snapshot()
        skill_xml = skills.catalog_xml(self.skill_snapshot)
        base_hash = hashlib.sha256(
            (_BASE_PROMPT + "\n" + profile.stable_instructions).encode("utf-8")
        ).hexdigest()
        self.prompt_profile = HarnessPromptProfile(
            trigger=profile.trigger,
            base_prompt_hash=base_hash,
            tool_catalog_hash=tool_catalog_hash,
            skill_catalog_hash=self.skill_snapshot.digest,
        )
        self.content = self.prefix_cache.get_or_create(
            self.prompt_profile.cache_key,
            lambda: "\n\n".join((
                _BASE_PROMPT,
                profile.stable_instructions.strip(),
                "# Available Harness Skills\n" + skill_xml,
                (
                    "# Skill policy\nRead a matching skill with skill_read before editing. "
                    "Only the common and current-trigger catalogs are authorized in this trace."
                ),
            )),
        )
        self._snapshots: dict[str, SystemPromptSnapshot] = {}
        self.rebuild_count = 1
        self.dynamic_context = _HarnessFragmentContext()

    def render_provider_query(
        self, original_query: str, session_id: str, *, origin_refs=None,
    ) -> str:
        del origin_refs
        return self.dynamic_context.render(original_query, session_id, track=True)

    def preview_provider_query(
        self, original_query: str, session_id: str, *, origin_refs=None,
    ) -> str:
        del origin_refs
        return self.dynamic_context.render(original_query, session_id, track=False)

    def open_session(
        self,
        session_id: str,
        *,
        force: bool = False,
        skill_catalog: SkillCatalogSnapshot | None = None,
    ) -> SystemPromptSnapshot:
        del force
        if skill_catalog is not None and skill_catalog.digest != self.skill_snapshot.digest:
            raise RuntimeError("An active Harness trace cannot change its Skill catalog")
        snapshot = self._snapshots.get(session_id)
        if snapshot is None:
            snapshot = SystemPromptSnapshot(
                session_id=session_id,
                segment_path=Path("harness-trace"),
                initialized_at="trace",
                content=self.content,
                skill_catalog=self.skill_snapshot,
            )
            self._snapshots[session_id] = snapshot
            self.skills.bind_session(session_id, self.skill_snapshot)
        return snapshot

    def compose(self, task: str, session_id: str | None = None) -> list[dict[str, str]]:
        if session_id is None:
            return [
                {"role": "system", "content": self.content},
                {"role": "user", "content": task},
            ]
        snapshot = self.open_session(session_id)
        return [
            {"role": "system", "content": snapshot.content},
            {"role": "user", "content": task},
        ]

    def refresh(
        self,
        session_id: str,
        *,
        skill_catalog: SkillCatalogSnapshot | None = None,
    ) -> SystemPromptSnapshot:
        return self.open_session(session_id, skill_catalog=skill_catalog)

    def skill_catalog(self, session_id: str) -> SkillCatalogSnapshot:
        return self.open_session(session_id).skill_catalog  # type: ignore[return-value]

    def close(self, session_id: str) -> None:
        self._snapshots.pop(session_id, None)
        self.skills.unbind_session(session_id)

    def invalidate_all(self) -> None:
        # Resources are immutable for an active trace. A new Runtime creates a new profile.
        return None

    def set_sandbox_status(self, status) -> None:
        # Sandbox status is dynamic and belongs in the ephemeral tail, not the cached prefix.
        del status


class _HarnessFragmentContext:
    def __init__(self) -> None:
        self.fragments = ProviderContextFragmentRegistry()
        self.last_envelope_hash = ""
        self.injection_count = 0

    def prepare(self, query: str, session_id: str, *, baseline=None, context_epoch="trace", origin_refs=None):
        return prepare_context(query, context_epoch, self.fragments.snapshot(session_id),
                               baseline or {}, origin_refs)

    def render(self, query: str, session_id: str, *, track: bool) -> str:
        fragment = self.fragments.render(session_id, consume_once=track)
        return query + ("\n\n" + fragment if fragment else "")
