"""Gateway 托管的持续 `/code` 会话管理器。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType

from Agent import RuntimeConfig
from backup import QuiesceResult, SensitiveEnvSanitizer
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

    def __init__(self, config: RuntimeConfig, *, grant_backend=None) -> None:
        self.config = config
        self.source_root = (
            config.coding_source_root or Path(__file__).resolve().parents[1]
        ).resolve()
        self.module = _load_harness(self.source_root)
        self.grant_backend = grant_backend
        self._sessions: dict[str, object] = {}
        self._sources: dict[Path, str] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self._maintenance_epoch: int | None = None

    async def start(
        self, project_id: str, client_id: str, *,
        origin_session_id: str | None = None, origin_run_id: str,
        origin_context: dict | None = None,
    ) -> CodeSessionRecord:
        async with self._lock:
            self._require_available()
            if self.source_root in self._sources:
                raise RuntimeError("这个 Yuan Ye 源码仓库已经有活动的 Coding Session")
            try:
                controller = self.module.CodeSessionController(
                    self.config, grant_backend=self.grant_backend,
                )
            except TypeError as exc:
                if "grant_backend" not in str(exc):
                    raise
                controller = self.module.CodeSessionController(self.config)
            origin = self.module.HarnessOriginContext.model_validate(
                origin_context or {
                    "origin_project_id": project_id,
                    "origin_session_id": origin_session_id,
                    "origin_run_id": origin_run_id,
                    "context_summary": "Gateway /code control session",
                    "trigger_evidence": {"code_session_start_run_id": origin_run_id},
                },
                strict=True,
            )
            try:
                raw = await controller.start(self.source_root, origin=origin)
            except TypeError as exc:
                # Compatibility for injected test/session facades that predate origin context.
                if "origin" not in str(exc):
                    raise
                raw = await controller.start(self.source_root)
            session_id = raw.code_session_id
            self._sessions[session_id] = controller
            self._sources[self.source_root] = session_id
            self._owners[session_id] = (project_id, client_id)
            self._turn_locks[session_id] = asyncio.Lock()
            return self._record(raw, project_id, client_id)

    async def run_turn(self, session_id: str, client_id: str, task: str) -> CodeTurnResult:
        self._require_available()
        controller = self._owned(session_id, client_id)
        lock = self._turn_locks[session_id]
        if lock.locked():
            raise RuntimeError("同一个 Coding Session 同时只能运行一条需求")
        async with lock:
            self._require_available()
            raw = await controller.run_turn(task)
        return CodeTurnResult.model_validate(raw.model_dump(mode="json"))

    async def finalize(
        self, session_id: str, client_id: str, *, approved_plan_hash: str | None = None,
        run_id: str | None = None,
    ) -> CodeFinalizeResult:
        self._require_available()
        controller = self._owned(session_id, client_id)
        lock = self._turn_locks[session_id]
        if lock.locked():
            raise RuntimeError("Coding Turn 正在运行，暂时不能退出或合并")
        async with lock:
            self._require_available()
            try:
                raw = await controller.finalize(
                    approved_plan_hash=approved_plan_hash,
                    decision_actor=client_id,
                    run_id=run_id,
                )
            except TypeError as exc:
                if not any(name in str(exc) for name in ("approved_plan_hash", "run_id")):
                    raise
                raw = await controller.finalize()
        result = CodeFinalizeResult.model_validate(raw.model_dump(mode="json"))
        if not result.stay_in_code_mode:
            self._forget(session_id)
        return result

    async def abort(self, session_id: str, client_id: str) -> CodeFinalizeResult:
        self._require_available()
        controller = self._owned(session_id, client_id)
        lock = self._turn_locks[session_id]
        if lock.locked():
            raise RuntimeError("Coding Turn 正在运行，暂时不能放弃会话")
        async with lock:
            self._require_available()
            raw = await controller.abort()
        result = CodeFinalizeResult.model_validate(raw.model_dump(mode="json"))
        self._forget(session_id)
        return result

    async def quiesce(self, maintenance_epoch: int) -> QuiesceResult:
        async with self._lock:
            if self._maintenance_epoch is not None and maintenance_epoch <= self._maintenance_epoch:
                return QuiesceResult(
                    participant="harness",
                    maintenance_epoch=maintenance_epoch,
                    acknowledged=maintenance_epoch == self._maintenance_epoch,
                    stale=maintenance_epoch < self._maintenance_epoch,
                )
            self._maintenance_epoch = maintenance_epoch
        # Do not cancel a Coding Turn. Wait for each worktree to reach a stable Git boundary.
        for lock in tuple(self._turn_locks.values()):
            async with lock:
                pass
        destination = (
            self.config.agent_root / ".yy-backups" / "maintenance" /
            str(maintenance_epoch) / "participants" / "harness"
        )
        destination.mkdir(parents=True, exist_ok=True)
        active: list[str] = []
        for session_id, controller in tuple(self._sessions.items()):
            await self._export_candidate(session_id, controller, destination / session_id)
            active.append(session_id)
        return QuiesceResult(
            participant="harness",
            maintenance_epoch=maintenance_epoch,
            acknowledged=True,
            safe_boundary="git_candidate_snapshot_exported",
            active_operations=tuple(active),
        )

    async def resume(self, maintenance_epoch: int) -> None:
        if self._maintenance_epoch == maintenance_epoch:
            self._maintenance_epoch = None

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

    def _require_available(self) -> None:
        if self._maintenance_epoch is not None:
            raise RuntimeError("Agent Home 正在维护，Coding Session 暂停接收新操作")

    async def _export_candidate(self, session_id: str, controller, destination: Path) -> None:
        record = getattr(controller, "record", None)
        if record is None:
            return
        destination.mkdir(parents=True, exist_ok=True)
        worktree = Path(record.worktree_path).resolve()
        source = Path(record.source_root).resolve()
        head = await self._git(worktree, "rev-parse", "HEAD")
        tracked = await self._git(worktree, "diff", "--binary")
        staged = await self._git(worktree, "diff", "--cached", "--binary")
        untracked = await self._git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
        tracked_path = destination / "tracked.diff"
        staged_path = destination / "staged.diff"
        tracked_path.write_text(tracked, encoding="utf-8")
        staged_path.write_text(staged, encoding="utf-8")
        bundle_path = destination / "untracked.zip"
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
            for name in (item for item in untracked.split("\x00") if item):
                candidate = (worktree / name).resolve()
                if worktree not in candidate.parents or candidate.is_symlink() or not candidate.is_file():
                    raise RuntimeError(f"Harness untracked 文件不安全：{name}")
                bundle.write(candidate, arcname=Path(name).as_posix())
        origin = await self._git(source, "remote", "get-url", "origin", check=False)
        metadata = {
            "version": 1,
            "code_session_id": session_id,
            "source_repo_path": str(source),
            "repository_identity": hashlib.sha256(
                (origin.strip() or str(source)).encode("utf-8"),
            ).hexdigest(),
            "base_commit": record.base_commit,
            "candidate_branch": record.branch,
            "candidate_commit": record.last_verified_commit,
            "head_commit": head.strip(),
            "tracked_diff": tracked_path.name,
            "staged_diff": staged_path.name,
            "untracked_file_bundle": bundle_path.name,
            "session_metadata": record.model_dump(mode="json"),
            "verification_evidence": str(record.audit_path),
            "required_commits": sorted({record.base_commit, record.last_verified_commit, head.strip()}),
        }
        content_hash = hashlib.sha256()
        for path in (tracked_path, staged_path, bundle_path):
            content_hash.update(path.read_bytes())
        metadata["content_hash"] = content_hash.hexdigest()
        (destination / "candidate.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    async def _git(root: Path, *arguments: str, check: bool = True) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(root), *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=SensitiveEnvSanitizer.subprocess_env({
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
            }),
        )
        stdout, stderr = await process.communicate()
        text = stdout.decode("utf-8", errors="replace")
        if check and process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[-4000:])
        return text

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
            origin_session_id=raw.origin.origin_session_id if raw.origin else None,
            origin_run_id=raw.origin.origin_run_id if raw.origin else None,
        )
