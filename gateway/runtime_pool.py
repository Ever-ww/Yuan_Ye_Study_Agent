"""Gateway 托管的 Runtime 缓存、并发队列和取消控制。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from Agent import AgentRuntime, EventType, ExtensionCatalog, ModelRetryPolicy, load_runtime_config
from gateway.approval import GatewayApprovalBroker
from gateway.events import GatewayEventBus
from gateway.models import ApprovalRequest, RunRecord, now_iso
from gateway.store import GatewayStore


RuntimeFactory = Callable[[Path, GatewayApprovalBroker], AgentRuntime]


@dataclass
class RuntimeEntry:
    runtime: AgentRuntime
    last_used: float


class RuntimePool:
    """每个 Session 缓存一个 Runtime，不同 Session 按全局上限并发。"""

    def __init__(
        self,
        *,
        agent_root: Path,
        store: GatewayStore,
        events: GatewayEventBus,
        max_concurrent_runs: int = 4,
        idle_timeout_seconds: int = 900,
        runtime_factory: RuntimeFactory | None = None,
        extensions: ExtensionCatalog | None = None,
        cron_service=None,
    ) -> None:
        self.agent_root = agent_root.resolve()
        self.store = store
        self.events = events
        self.max_concurrent_runs = max_concurrent_runs
        self.idle_timeout_seconds = idle_timeout_seconds
        self.runtime_factory = runtime_factory
        self.extensions = extensions
        self.cron_service = cron_service
        self.approvals = GatewayApprovalBroker(
            store,
            self._publish_approval,
            self.events.wait_connected,
        )
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._runtimes: dict[tuple[str, str], RuntimeEntry] = {}
        self._busy_sessions: set[tuple[str, str]] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle(), name="gateway-runtime-reaper")

    async def submit(self, run: RunRecord) -> None:
        if self._closing:
            raise RuntimeError("Gateway 正在关闭，不能接收新任务")
        if run.session_id:
            key = (run.project_id, run.session_id)
            async with self._lock:
                if key in self._busy_sessions:
                    raise RuntimeError("同一个 Session 同时只能运行一个任务")
                self._busy_sessions.add(key)
        task = asyncio.create_task(self._execute(run), name=f"gateway-run-{run.run_id}")
        self._tasks[run.run_id] = task

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self, grace_seconds: float = 5.0) -> None:
        self._closing = True
        await self.approvals.deny_all()
        active = [task for task in self._tasks.values() if not task.done()]
        if active:
            done, pending = await asyncio.wait(active, timeout=grace_seconds)
            del done
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        entries = list(self._runtimes.values())
        self._runtimes.clear()
        for entry in entries:
            await entry.runtime.close()

    def refresh_skills(self, project_id: str) -> int:
        """刷新指定项目当前缓存 Runtime 的 Skill Prompt。"""
        counts = [
            entry.runtime.refresh_skills()
            for (selected_project, _), entry in self._runtimes.items()
            if selected_project == project_id
        ]
        return max(counts, default=0)

    async def _execute(self, original: RunRecord) -> None:
        runtime: AgentRuntime | None = None
        session_key: tuple[str, str] | None = (
            (original.project_id, original.session_id) if original.session_id else None
        )
        try:
            await self._emit(original, "run_queued", {"message": "任务已进入 Gateway 队列"})
            async with self._semaphore:
                current = self.store.update_run(
                    original.run_id,
                    status="running",
                    started_at=now_iso(),
                )
                await self._emit(current, "run_started", {"message": "任务开始运行"})
                project = self.store.project(current.project_id)
                runtime = await self._runtime_for(current, Path(project.path))
                token = self.approvals.bind_run(current.run_id, current.client_id)
                try:
                    async for event in runtime.run_task(current.task, current.session_id):
                        if event.type is EventType.STARTED:
                            session_id = str(event.payload["session_id"])
                            current = self.store.update_run(current.run_id, session_id=session_id)
                            new_key = (current.project_id, session_id)
                            if session_key is None:
                                async with self._lock:
                                    if new_key in self._busy_sessions:
                                        raise RuntimeError("新 Session 标识与正在运行的 Session 冲突")
                                    self._busy_sessions.add(new_key)
                                session_key = new_key
                                self._runtimes[new_key] = RuntimeEntry(runtime, monotonic())
                        await self._emit(
                            current,
                            event.type.value,
                            dict(event.payload),
                        )
                    current = self.store.run(current.run_id)
                    final_events = self.store.read_events(current.run_id)
                    answer = next(
                        (
                            str(item.payload.get("answer", ""))
                            for item in reversed(final_events)
                            if item.type == EventType.FINAL.value
                        ),
                        "",
                    )
                    current = self.store.update_run(
                        current.run_id,
                        status="completed",
                        answer=answer,
                        finished_at=now_iso(),
                    )
                    await self._emit(current, "run_completed", {"answer": answer})
                finally:
                    self.approvals.reset_run(token)
        except asyncio.CancelledError:
            current = self.store.update_run(
                original.run_id,
                status="cancelled",
                finished_at=now_iso(),
            )
            await self._emit(current, "run_cancelled", {"message": "当前运行已取消"})
        except Exception as exc:
            current = self.store.update_run(
                original.run_id,
                status="failed",
                error=str(exc) or type(exc).__name__,
                finished_at=now_iso(),
            )
            await self._emit(current, "run_failed", {"message": current.error or "运行失败"})
        finally:
            current = self.store.run(original.run_id)
            if current.status in {"completed", "failed", "cancelled", "interrupted"}:
                item = self.store.create_inbox(current)
                await self._emit(current, "inbox_created", item.model_dump(mode="json"))
            if runtime is not None and session_key is not None:
                entry = self._runtimes.get(session_key)
                if entry is not None:
                    entry.last_used = monotonic()
            if session_key is not None:
                async with self._lock:
                    self._busy_sessions.discard(session_key)
            self._tasks.pop(original.run_id, None)

    async def _runtime_for(self, run: RunRecord, workspace: Path) -> AgentRuntime:
        if run.session_id:
            entry = self._runtimes.get((run.project_id, run.session_id))
            if entry is not None:
                entry.last_used = monotonic()
                return entry.runtime
        if self.runtime_factory is not None:
            return self.runtime_factory(workspace, self.approvals)
        return self._default_runtime(workspace, self.approvals, run)

    def _default_runtime(
        self,
        workspace: Path,
        approvals: GatewayApprovalBroker,
        run: RunRecord,
    ) -> AgentRuntime:
        config = load_runtime_config(self.agent_root, workspace_root=workspace)
        scheduled = run.client_id.startswith("cron:")
        return AgentRuntime(
            config,
            approval=approvals,
            retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=2),
            raise_errors=True,
            extensions=self.extensions,
            cron=self.cron_service if not scheduled else None,
            cron_project_id=run.project_id,
            enable_cron=not scheduled,
        )

    async def _emit(self, run: RunRecord, event_type: str, payload: dict) -> None:
        envelope = self.store.append_event(
            run.run_id,
            run.project_id,
            run.session_id,
            event_type,
            payload,
        )
        await self.events.publish(envelope)

    async def _publish_approval(self, request: ApprovalRequest) -> None:
        run = self.store.run(request.run_id)
        await self._emit(run, "approval_requested", request.model_dump(mode="json"))

    async def _reap_idle(self) -> None:
        interval = max(5.0, min(60.0, self.idle_timeout_seconds / 2))
        while True:
            await asyncio.sleep(interval)
            now = monotonic()
            selected: list[RuntimeEntry] = []
            async with self._lock:
                for key, entry in tuple(self._runtimes.items()):
                    if key not in self._busy_sessions and now - entry.last_used >= self.idle_timeout_seconds:
                        selected.append(entry)
                        self._runtimes.pop(key, None)
            for entry in selected:
                await entry.runtime.close()
