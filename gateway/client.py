"""CLI、Web 启动器和 Tauri sidecar 共用的 Gateway 客户端。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

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

    async def register_project(self, path: Path, name: str | None = None) -> dict[str, Any]:
        payload = ProjectCreateRequest(path=str(path.resolve()), name=name)
        return await self._request("POST", "/api/v1/projects", json=payload.model_dump(mode="json"))

    async def projects(self) -> list[dict[str, Any]]:
        return list(await self._request("GET", "/api/v1/projects"))

    async def sessions(self, project_id: str) -> list[dict[str, Any]]:
        return list(await self._request("GET", f"/api/v1/projects/{project_id}/sessions"))

    async def session(self, project_id: str, session_id: str) -> list[dict[str, Any]]:
        return list(await self._request("GET", f"/api/v1/projects/{project_id}/sessions/{session_id}"))

    async def start_run(
        self,
        project_id: str,
        task: str,
        session_id: str | None = None,
    ) -> RunRecord:
        payload = RunCreateRequest(
            project_id=project_id,
            client_id=self.client_id,
            task=task,
            session_id=session_id,
        )
        value = await self._request("POST", "/api/v1/runs", json=payload.model_dump(mode="json"))
        return RunRecord.model_validate(value)

    async def cancel_run(self, run_id: str) -> bool:
        value = await self._request("POST", f"/api/v1/runs/{run_id}/cancel")
        return bool(value.get("cancelled"))

    async def run(self, run_id: str) -> RunRecord:
        value = await self._request("GET", f"/api/v1/runs/{run_id}")
        return RunRecord.model_validate(value)

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

    async def refresh_skills(self, project_id: str) -> int:
        value = await self._request("POST", f"/api/v1/projects/{project_id}/skills/refresh")
        return int(value["count"])

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
        # Gateway 固定为本机回环服务，不允许系统代理劫持 WebSocket。
        async with connect(f"{ws_url}/api/v1/events?{query}", proxy=None) as socket:
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
                async for event in self.events(run_id, after_sequence=sequence):
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
