"""Shared checkpoint lifecycle; backends only implement process isolation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Protocol
from pydantic import BaseModel, ConfigDict
from .checkpoint import CheckpointStore
from .locks import WorkspaceLockManager
from .models import (BashResult, CheckpointBranchRecord, CheckpointMergeAttempt,
                     CheckpointRecord, RollbackResult, SandboxStatus)

class SandboxUnavailableError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code

class BashUnavailableError(RuntimeError):
    """当前 Trace 没有可安全执行命令的隔离后端。"""


class SandboxRecoveryRequired(RuntimeError):
    """A process/permission cleanup cannot be proven; preserve workspace evidence."""


class CommandResult(BaseModel):
    """外部命令执行结果，便于测试注入而不依赖真实 Docker。"""

    model_config = ConfigDict(frozen=True, strict=True)

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str], float | None], Awaitable[CommandResult]]


class SandboxSessionProtocol(Protocol):
    """工具与 Runtime 之间共享的最小沙箱契约。"""

    file_locks: WorkspaceLockManager

    @property
    def status(self) -> SandboxStatus: ...
    @property
    def bash_available(self) -> bool: ...

    async def start(self, session_id: str) -> CheckpointRecord: ...
    async def close(self) -> None: ...
    async def run_bash(self, command: str, timeout_seconds: int = 30) -> BashResult: ...
    async def checkpoint_write(self, path: str) -> CheckpointRecord | None: ...
    async def checkpoint_edit(self, path: str) -> CheckpointRecord | None: ...
    async def restore_current(self) -> CheckpointRecord: ...
    async def rollback(
        self, steps: int | None = None, *, sequence: int | None = None,
        checkpoint_sha: str | None = None, merge_eligible: bool = True,
        archive_reason: str = "user_rollback",
    ) -> RollbackResult: ...
    def list_checkpoints(self) -> tuple[CheckpointRecord, ...]: ...
    def list_checkpoint_branches(self) -> tuple[CheckpointBranchRecord, ...]: ...
    def list_checkpoint_merge_attempts(self) -> tuple[CheckpointMergeAttempt, ...]: ...
    async def set_checkpoint_branch_merge_eligibility(
        self, branch_id: str, eligible: bool, reason: str,
    ) -> CheckpointBranchRecord: ...


class CheckpointSandboxSession:
    def __init__(
        self,
        project_root: Path,
        *,
        state_root: Path | None = None,
        checkpoint_limit: int = 17,
        checkpoint_store: CheckpointStore | None = None,
        file_locks: WorkspaceLockManager | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.state_root = (state_root or project_root).resolve()
        self.file_locks = file_locks or WorkspaceLockManager(
            self.project_root,
            state_root=self.state_root,
        )
        self.checkpoints = checkpoint_store or CheckpointStore(
            self.project_root,
            state_root=self.state_root,
            limit=checkpoint_limit,
        )
        self._operation_lock = asyncio.Lock()
        self._status = SandboxStatus(
            mode="pending",
            bash_available=False,
            message="沙箱尚未探测",
        )

    @property
    def active(self) -> bool:
        return self._status.mode in {"os", "docker", "checkpoint_only"}

    @property
    def status(self) -> SandboxStatus:
        return self._status

    @property
    def bash_available(self) -> bool:
        return self._status.bash_available

    async def start(self, session_id: str) -> CheckpointRecord:
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                if self.active:
                    return self.checkpoints.list()[-1]
                try:
                    self._status = await self._start_backend(session_id)
                except SandboxUnavailableError as exc:
                    self._status = SandboxStatus(
                        mode="checkpoint_only", bash_available=False,
                        reason_code=exc.reason_code,
                        message=f"{exc}；Bash/Shell 已禁用，文件编辑与 checkpoint 仍可用",
                    )
                try:
                    return await self._open_checkpoint_baseline(session_id)
                except BaseException:
                    await self._close_unlocked()
                    raise

    async def close(self) -> None:
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        try:
            await self._close_backend()
        finally:
            self._status = SandboxStatus(
                mode="closed", bash_available=False, message="沙箱已关闭，checkpoint 已保留",
            )

    async def _start_backend(self, session_id: str) -> SandboxStatus:
        raise NotImplementedError

    async def _close_backend(self) -> None:
        raise NotImplementedError

    async def _execute_shell(self, command: str, timeout_seconds: int) -> CommandResult:
        raise NotImplementedError

    async def run_bash(self, command: str, timeout_seconds: int = 30) -> BashResult:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command 不能为空")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 120:
            raise ValueError("Bash timeout_seconds 必须位于 1 到 120 之间")
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                if not self.bash_available:
                    raise BashUnavailableError("未运行安全沙箱；禁止回退到无隔离的宿主机 Shell")
                try:
                    result = await self._execute_shell(command, timeout_seconds)
                    output = "\n".join(p.strip() for p in (result.stdout, result.stderr) if p.strip())
                    output = output[:20000]
                    if result.returncode:
                        raise RuntimeError(f"Sandbox Bash 执行失败（exit={result.returncode}）：{output or '无输出'}")
                    checkpoint = await asyncio.to_thread(
                        self.checkpoints.create, "bash",
                        {"command": command[:1000], "timeout_seconds": timeout_seconds},
                    )
                except SandboxRecoveryRequired:
                    self._status = SandboxStatus(
                        mode="checkpoint_only", bash_available=False,
                        reason_code="sandbox_recovery_required",
                        message="Sandbox cleanup is unconfirmed; shell disabled and workspace preserved",
                    )
                    raise
                except BaseException:
                    # Backends must stop the entire process tree before returning/cancelling.
                    # Only then can the shared checkpoint safely restore the workspace.
                    await asyncio.to_thread(self.checkpoints.restore_current)
                    raise
                return BashResult(exit_code=0, output=output, checkpoint=checkpoint)

    async def checkpoint_write(self, path: str) -> CheckpointRecord | None:
        """为宿主机 write 已完成的实际修改创建一次 checkpoint。"""
        async with self._operation_lock:
            self._require_checkpoint_session()
            return await asyncio.to_thread(
                self.checkpoints.create,
                "write",
                {"path": path},
            )

    async def checkpoint_edit(self, path: str) -> CheckpointRecord | None:
        """为宿主机 edit 已完成的实际修改创建一次独立审计 checkpoint。"""
        async with self._operation_lock:
            self._require_checkpoint_session()
            return await asyncio.to_thread(
                self.checkpoints.create,
                "edit",
                {"path": path},
            )

    async def restore_current(self) -> CheckpointRecord:
        async with self._operation_lock:
            self._require_checkpoint_session()
            return await asyncio.to_thread(self.checkpoints.restore_current)

    async def rollback(
        self,
        steps: int | None = None,
        *,
        sequence: int | None = None,
        checkpoint_sha: str | None = None,
        merge_eligible: bool = True,
        archive_reason: str = "user_rollback",
    ) -> RollbackResult:
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                self._require_checkpoint_session()
                if (
                    steps is not None and sequence is None and checkpoint_sha is None
                    and merge_eligible and archive_reason == "user_rollback"
                ):
                    # 保持旧测试替身和第三方Sandbox适配器的单参数调用兼容性。
                    return await asyncio.to_thread(self.checkpoints.rollback, steps)
                return await asyncio.to_thread(
                    self.checkpoints.rollback,
                    steps,
                    sequence=sequence,
                    checkpoint_sha=checkpoint_sha,
                    merge_eligible=merge_eligible,
                    archive_reason=archive_reason,
                )

    def list_checkpoints(self) -> tuple[CheckpointRecord, ...]:
        return self.checkpoints.list()

    def list_checkpoint_branches(self) -> tuple[CheckpointBranchRecord, ...]:
        return self.checkpoints.list_branches()

    def list_checkpoint_merge_attempts(self) -> tuple[CheckpointMergeAttempt, ...]:
        """返回只读的 Dream 合并历史，供状态查询和人工恢复判断使用。"""
        return self.checkpoints.list_merge_attempts()

    async def set_checkpoint_branch_merge_eligibility(
        self,
        branch_id: str,
        eligible: bool,
        reason: str,
    ) -> CheckpointBranchRecord:
        """串行修改归档分支的 Dream 准入；不触碰工作区或项目 Git。"""
        async with self._operation_lock:
            self._require_checkpoint_session()
            return await asyncio.to_thread(
                self.checkpoints.set_merge_eligibility,
                branch_id,
                eligible,
                reason,
            )

    async def _open_checkpoint_baseline(self, session_id: str) -> CheckpointRecord:
        """初始化独立 Git 对象库；失败属于数据安全问题，必须向上抛出。"""
        self.checkpoints.open(session_id)
        baseline = await asyncio.to_thread(
            self.checkpoints.create,
            "trace_start",
            {"kind": "baseline"},
            force=True,
        )
        if baseline is None:
            raise RuntimeError("创建 Trace 基线 checkpoint 失败")
        return baseline

    def _require_checkpoint_session(self) -> None:
        if not self.active:
            raise RuntimeError("Checkpoint 会话尚未启动或已经关闭")
