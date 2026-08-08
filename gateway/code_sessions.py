"""Gateway 托管的持续 `/code` 会话管理器。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

from Agent import RuntimeConfig
from gateway.models import CodeFinalizeResult, CodeSessionRecord, CodeTurnResult


def _load_harness(source_root: Path) -> ModuleType:
    path = source_root.resolve() / "harness-evolution" / "harness.py"
    if not path.is_file():
        raise RuntimeError(f"找不到 Harness 实现：{path}")
    name = "yy_harness_evolution"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 Harness：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CodeSessionManager:
    """确保每个 Yuan Ye 源码仓库同时只有一个可变 Coding Session。"""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.source_root = (
            config.coding_source_root or Path(__file__).resolve().parents[1]
        ).resolve()
        self.module = _load_harness(self.source_root)
        self._sessions: dict[str, object] = {}
        self._sources: dict[Path, str] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def start(self, project_id: str, client_id: str) -> CodeSessionRecord:
        async with self._lock:
            if self.source_root in self._sources:
                raise RuntimeError("这个 Yuan Ye 源码仓库已经有活动的 Coding Session")
            controller = self.module.CodeSessionController(self.config)
            raw = await controller.start(self.source_root)
            session_id = raw.code_session_id
            self._sessions[session_id] = controller
            self._sources[self.source_root] = session_id
            self._owners[session_id] = (project_id, client_id)
            self._turn_locks[session_id] = asyncio.Lock()
            return self._record(raw, project_id, client_id)

    async def run_turn(self, session_id: str, client_id: str, task: str) -> CodeTurnResult:
        controller = self._owned(session_id, client_id)
        lock = self._turn_locks[session_id]
        if lock.locked():
            raise RuntimeError("同一个 Coding Session 同时只能运行一条需求")
        async with lock:
            raw = await controller.run_turn(task)
        return CodeTurnResult.model_validate(raw.model_dump(mode="json"))

    async def finalize(self, session_id: str, client_id: str) -> CodeFinalizeResult:
        controller = self._owned(session_id, client_id)
        lock = self._turn_locks[session_id]
        if lock.locked():
            raise RuntimeError("Coding Turn 正在运行，暂时不能退出或合并")
        async with lock:
            raw = await controller.finalize()
        result = CodeFinalizeResult.model_validate(raw.model_dump(mode="json"))
        if not result.stay_in_code_mode:
            self._forget(session_id)
        return result

    async def abort(self, session_id: str, client_id: str) -> CodeFinalizeResult:
        controller = self._owned(session_id, client_id)
        lock = self._turn_locks[session_id]
        if lock.locked():
            raise RuntimeError("Coding Turn 正在运行，暂时不能放弃会话")
        async with lock:
            raw = await controller.abort()
        result = CodeFinalizeResult.model_validate(raw.model_dump(mode="json"))
        self._forget(session_id)
        return result

    async def close(self) -> None:
        """关闭 Runtime/Docker，但保留分支与 worktree，绝不在 Gateway 退出时合并。"""
        for session_id, controller in tuple(self._sessions.items()):
            runtime = getattr(controller, "runtime", None)
            record = getattr(controller, "record", None)
            if runtime is not None:
                await runtime.close()
                controller.runtime = None
            if record is not None:
                controller.audit.append_event(
                    record.audit_path,
                    "code_session_interrupted",
                    message="Gateway 已关闭；worktree 和临时分支已保留，未自动合并。",
                )
            self._forget(session_id)

    def events(self, session_id: str, after_sequence: int = 0) -> list[dict]:
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise ValueError("Coding Session ID 非法")
        path = (
            self.config.agent_root / ".yy" / "harness-evolution" / "code" /
            f"{session_id}.jsonl"
        ).resolve()
        expected = (
            self.config.agent_root / ".yy" / "harness-evolution" / "code"
        ).resolve()
        if path.parent != expected or not path.is_file():
            raise KeyError(f"未知 Coding Session：{session_id}")
        records: list[dict] = []
        for position, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # 写入中的最后一行留给下一次轮询，避免短暂部分写影响主任务。
                continue
            sequence = int(value.get("sequence", position))
            if sequence > after_sequence:
                value["sequence"] = sequence
                records.append(value)
        return records

    def owner(self, session_id: str) -> tuple[str, str]:
        owner = self._owners.get(session_id)
        if owner is None:
            raise KeyError(f"未知 Coding Session：{session_id}")
        return owner

    def _owned(self, session_id: str, client_id: str):
        controller = self._sessions.get(session_id)
        owner = self._owners.get(session_id)
        if controller is None or owner is None:
            raise KeyError(f"未知 Coding Session：{session_id}")
        if owner[1] != client_id:
            raise PermissionError("只有创建 Coding Session 的客户端可以操作它")
        return controller

    def _forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._owners.pop(session_id, None)
        self._turn_locks.pop(session_id, None)
        if self._sources.get(self.source_root) == session_id:
            self._sources.pop(self.source_root, None)

    @staticmethod
    def _record(raw, project_id: str, client_id: str) -> CodeSessionRecord:
        return CodeSessionRecord(
            code_session_id=raw.code_session_id,
            project_id=project_id,
            client_id=client_id,
            source_root=str(raw.source_root),
            worktree_path=str(raw.worktree_path),
            branch=raw.branch,
            base_commit=raw.base_commit,
            status=raw.status,
            verified_turns=raw.verified_turns,
        )
