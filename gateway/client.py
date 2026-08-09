"""CLI、Web 启动器和 Tauri sidecar 共用的 Gateway 客户端。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from cron import (
    CronJob,
    CronJobCreateRequest,
    CronJobEditRequest,
    CronPreview,
    CronPreviewRequest,
    CronSchedule,
    CronStatus,
)
from dream import (
    DreamBackfillRequest,
    DreamRollbackRequest,
    DreamRollbackResult,
    DreamRunRequest,
    DreamRunResult,
    DreamStatus,
)
from backup import BackupCreateRequest, BackupRecord

from gateway.models import (
    ApprovalDecision,
    CodeFinalizeResult,
    CodeSessionCreateRequest,
    CodeSessionRecord,
    CodeTurnRequest,
    CodeTurnResult,
    GatewayEventEnvelope,
    ProjectCreateRequest,
    RunCreateRequest,
    RecoveryDecisionRequest,
    RunRecord,
)
from gateway.process import GatewayProcessManager


class GatewayClient:
    def __init__(
        self,
        agent_root: Path,
        *,
        port: int = 8765,
        client_id: str | None = None,
        auto_start: bool = True,
    ) -> None:
        self.manager = GatewayProcessManager(agent_root, port)
        if auto_start:
            self.manager.ensure_running()
        self.base_url = self.manager.base_url
        self.token = self.manager.token()
        self.client_id = client_id or f"client_{uuid4().hex}"
        self._headers = {"Authorization": f"Bearer {self.token}"}

    async def connect(self) -> dict[str, Any]:
        async with httpx.AsyncClient(headers=self._headers, timeout=10, trust_env=False) as client:
            response = await client.get(f"{self.base_url}/api/v1/status")
            response.raise_for_status()
            return dict(response.json())

    async def create_backup(self, passphrase: str, output: Path | None = None) -> BackupRecord:
        request = BackupCreateRequest(passphrase=passphrase, output=output)
        value = await self._request(
            "POST", "/api/v1/backup/create",
            json=request.model_dump(mode="json"), timeout=3600,
        )
        return BackupRecord.model_validate(value)

    async def backups(self) -> tuple[BackupRecord, ...]:
        values = await self._request("GET", "/api/v1/backup/list")
        return tuple(BackupRecord.model_validate(value) for value in values)

    async def backup_status(self) -> dict[str, Any]:
        return dict(await self._request("GET", "/api/v1/backup/status"))

    async def register_project(self, path: Path, name: str | None = None) -> dict[str, Any]:
        payload = ProjectCreateRequest(path=str(path.resolve()), name=name)
        return await self._request("POST", "/api/v1/projects", json=payload.model_dump(mode="json"))

    async def projects(self) -> list[dict[str, Any]]:
        return list(await self._request("GET", "/api/v1/projects"))

    async def cron_jobs(self, project_id: str | None = None) -> tuple[CronJob, ...]:
        params = {"project_id": project_id} if project_id else None
        values = await self._request("GET", "/api/v1/cron/jobs", params=params)
        return tuple(CronJob.model_validate(value) for value in values)

    async def cron_status(self) -> CronStatus:
        return CronStatus.model_validate(await self._request("GET", "/api/v1/cron/status"))

    async def cron_preview(self, schedule: CronSchedule, count: int = 5) -> CronPreview:
        payload = CronPreviewRequest(schedule=schedule, count=count)
        value = await self._request(
            "POST", "/api/v1/cron/preview", json=payload.model_dump(mode="json"),
        )
        return CronPreview.model_validate(value)

    async def create_cron(self, request: CronJobCreateRequest) -> CronJob:
        value = await self._request(
            "POST", "/api/v1/cron/jobs", json=request.model_dump(mode="json"),
        )
        return CronJob.model_validate(value)

    async def edit_cron(self, job_id: str, request: CronJobEditRequest) -> CronJob:
        value = await self._request(
            "PATCH", f"/api/v1/cron/jobs/{job_id}", json=request.model_dump(mode="json"),
        )
        return CronJob.model_validate(value)

    async def pause_cron(self, job_id: str) -> CronJob:
        return await self._cron_action(job_id, "pause")

    async def resume_cron(self, job_id: str) -> CronJob:
        return await self._cron_action(job_id, "resume")

    async def run_cron(self, job_id: str) -> CronJob:
        return await self._cron_action(job_id, "run")

    async def remove_cron(self, job_id: str) -> CronJob:
        value = await self._request("DELETE", f"/api/v1/cron/jobs/{job_id}")
        return CronJob.model_validate(value)

    async def _cron_action(self, job_id: str, action: str) -> CronJob:
        value = await self._request("POST", f"/api/v1/cron/jobs/{job_id}/{action}")
        return CronJob.model_validate(value)

    async def dream_status(self) -> DreamStatus:
        return DreamStatus.model_validate(await self._request("GET", "/api/v1/dream/status"))

    async def run_dream(self, selected_date: str | None = None) -> DreamRunResult:
        request = DreamRunRequest(date=selected_date)
        value = await self._request(
            "POST", "/api/v1/dream/run", json=request.model_dump(mode="json"),
        )
        return DreamRunResult.model_validate(value)

    async def backfill_dream(self, start: str, end: str) -> tuple[DreamRunResult, ...]:
        request = DreamBackfillRequest(start=start, end=end)
        values = await self._request(
            "POST", "/api/v1/dream/backfill", json=request.model_dump(mode="json"),
        )
        return tuple(DreamRunResult.model_validate(value) for value in values)

    async def rollback_dream(self, run_id: str | None = None) -> DreamRollbackResult:
        request = DreamRollbackRequest(run_id=run_id)
        value = await self._request(
            "POST", "/api/v1/dream/rollback", json=request.model_dump(mode="json"),
        )
        return DreamRollbackResult.model_validate(value)

    async def sessions(self, project_id: str) -> list[dict[str, Any]]:
        return list(await self._request("GET", f"/api/v1/projects/{project_id}/sessions"))

    async def session(self, project_id: str, session_id: str) -> list[dict[str, Any]]:
        return list(await self._request("GET", f"/api/v1/projects/{project_id}/sessions/{session_id}"))

    async def start_run(
        self,
        project_id: str,
        task: str,
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunRecord:
        payload = RunCreateRequest(
            project_id=project_id,
            client_id=self.client_id,
            task=task,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )
        value = await self._request("POST", "/api/v1/runs", json=payload.model_dump(mode="json"))
        return RunRecord.model_validate(value)

    async def cancel_run(self, run_id: str) -> bool:
        value = await self._request("POST", f"/api/v1/runs/{run_id}/cancel")
        return bool(value.get("cancelled"))

    async def run(self, run_id: str) -> RunRecord:
        value = await self._request("GET", f"/api/v1/runs/{run_id}")
        return RunRecord.model_validate(value)

    async def run_state(self, run_id: str) -> dict[str, Any]:
        return dict(await self._request("GET", f"/api/v1/runs/{run_id}/state"))

    async def run_operations(self, run_id: str) -> list[dict[str, Any]]:
        return list(await self._request("GET", f"/api/v1/runs/{run_id}/operations"))

    async def recover_run(self, run_id: str, request: RecoveryDecisionRequest) -> dict[str, Any]:
        return dict(await self._request(
            "POST", f"/api/v1/runs/{run_id}/recovery",
            json=request.model_dump(mode="json"),
        ))

    async def respond_approval(self, approval_id: str, approved: bool) -> bool:
        decision = ApprovalDecision(client_id=self.client_id, approved=approved)
        value = await self._request(
            "POST",
            f"/api/v1/approvals/{approval_id}",
            json=decision.model_dump(mode="json"),
        )
        return bool(value.get("approved"))

    async def inbox(self, unread_only: bool = False) -> list[dict[str, Any]]:
        return list(await self._request(
            "GET",
            "/api/v1/inbox",
            params={"unread_only": str(unread_only).lower()},
        ))

    async def mark_inbox_read(self, item_id: str) -> dict[str, Any]:
        """将一条后台结果标记为已读。"""
        return dict(await self._request("POST", f"/api/v1/inbox/{item_id}/read"))

    async def browser_url(self) -> str:
        value = await self._request("POST", "/api/v1/browser/code")
        return str(value["url"])

    async def start_code_session(self, project_id: str) -> CodeSessionRecord:
        payload = CodeSessionCreateRequest(project_id=project_id, client_id=self.client_id)
        value = await self._request(
            "POST", "/api/v1/code/sessions", json=payload.model_dump(mode="json"),
        )
        return CodeSessionRecord.model_validate(value)

    async def run_code_turn(self, session_id: str, task: str) -> CodeTurnResult:
        payload = CodeTurnRequest(client_id=self.client_id, task=task)
        value = await self._request(
            "POST", f"/api/v1/code/sessions/{session_id}/turns",
            json=payload.model_dump(mode="json"),
            timeout=3600,
        )
        return CodeTurnResult.model_validate(value)

    async def finalize_code_session(self, session_id: str) -> CodeFinalizeResult:
        value = await self._request(
            "POST", f"/api/v1/code/sessions/{session_id}/finalize",
            params={"client_id": self.client_id},
        )
        return CodeFinalizeResult.model_validate(value)

    async def abort_code_session(self, session_id: str) -> CodeFinalizeResult:
        value = await self._request(
            "POST", f"/api/v1/code/sessions/{session_id}/abort",
            params={"client_id": self.client_id},
        )
        return CodeFinalizeResult.model_validate(value)

    async def code_session_events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        return list(await self._request(
            "GET",
            f"/api/v1/code/sessions/{session_id}/events",
            params={"after_sequence": after_sequence},
        ))

    async def skills(self, project_id: str) -> list[dict[str, Any]]:
        return list(await self._request("GET", f"/api/v1/projects/{project_id}/skills"))

    async def refresh_skills(self, project_id: str, session_id: str) -> dict[str, Any]:
        return dict(await self._request(
            "POST",
            f"/api/v1/projects/{project_id}/sessions/{session_id}/skills/refresh",
            # 刷新可能需要恢复 Runtime，并在切换 JSONL 分段前调用模型压缩历史；
            # 不能沿用普通控制面请求的 30 秒读取超时。
            timeout=3600,
        ))

    async def skill_audit(self, project_id: str, review_id: str) -> dict[str, Any]:
        return dict(await self._request(
            "GET",
            f"/api/v1/projects/{project_id}/skills/audit/{review_id}",
        ))

    async def manage_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._request("POST", "/api/v1/skills/manage", json=payload))

    async def events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[GatewayEventEnvelope]:
        try:
            from websockets.asyncio.client import connect
        except ModuleNotFoundError as exc:
            raise RuntimeError("GatewayClient 需要安装 websockets") from exc
        ws_url = self.base_url.replace("http://", "ws://")
        query = httpx.QueryParams({
            "token": self.token,
            "client_id": self.client_id,
            "run_id": run_id,
            "after_sequence": str(after_sequence),
        })
        # Gateway 固定为本机回环服务，不允许系统代理劫持 WebSocket。本机事件流
        # 不需要后台 ping；禁用它可避免任务取消时 keepalive 与关闭握手并发写连接。
        async with connect(
            f"{ws_url}/api/v1/events?{query}",
            proxy=None,
            ping_interval=None,
            close_timeout=2,
        ) as socket:
            async for raw in socket:
                value = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                yield GatewayEventEnvelope.model_validate_json(value, strict=True)

    async def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[GatewayEventEnvelope]:
        sequence = after_sequence
        terminal = {"run_completed", "run_failed", "run_cancelled", "run_interrupted"}
        while True:
            try:
                # async for 在消费者提前返回时不会替任意异步生成器保证 aclose。
                # 显式 aclosing 让 WebSocket 在当前任务内按顺序完成关闭握手。
                async with contextlib.aclosing(
                    self.events(run_id, after_sequence=sequence),
                ) as event_stream:
                    async for event in event_stream:
                        if event.sequence <= sequence:
                            continue
                        sequence = event.sequence
                        yield event
                        if event.type in terminal:
                            return
            except asyncio.CancelledError:
                raise
            except Exception:
                current = await self.run(run_id)
                replay = await self._request(
                    "GET",
                    f"/api/v1/runs/{run_id}/events",
                    params={"after_sequence": sequence},
                )
                for value in replay:
                    event = GatewayEventEnvelope.model_validate(value)
                    if event.sequence <= sequence:
                        continue
                    sequence = event.sequence
                    yield event
                    if event.type in terminal:
                        return
                if current.status in {"completed", "failed", "cancelled", "interrupted"}:
                    return
                await asyncio.sleep(0.5)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", 30)
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=timeout,
            trust_env=False,
        ) as client:
            response = await client.request(method, f"{self.base_url}{path}", **kwargs)
            if response.status_code == 409:
                raise RuntimeError(response.json().get("detail", "Gateway 状态冲突"))
            response.raise_for_status()
            return response.json()
