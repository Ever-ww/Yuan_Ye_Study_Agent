"""FastAPI 本机 API、WebSocket 事件桥和共享 Web 入口。"""

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from Agent import load_runtime_config
from cron import CronJobCreateRequest, CronJobEditRequest, CronPreviewRequest
from dream import DreamBackfillRequest, DreamRollbackRequest, DreamRunRequest
from gateway.application import GatewayApplication
from gateway.models import (
    ApprovalDecision,
    BrowserExchangeRequest,
    CodeSessionCreateRequest,
    CodeTurnRequest,
    ProjectCreateRequest,
    RunCreateRequest,
    RecoveryDecisionRequest,
    SkillManageRequest,
)
from gateway.security import GatewayCredentials, bearer_value
from sandbox import probe_docker_status
from backup import BackupCreateRequest, MaintenanceBlockedError, external_control_root


def create_gateway_api(
    application: GatewayApplication | None = None,
    *,
    access_token: str | None = None,
) -> Any:
    try:
        from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse, HTMLResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError("Gateway API 需要安装 fastapi") from exc

    config = application.config if application is not None else load_runtime_config()
    gateway = application or GatewayApplication(config)
    token = access_token or GatewayCredentials(
        external_control_root(config.agent_root) / "control" / "gateway",
    ).load_or_create()
    csrf_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        await gateway.start()
        try:
            yield
        finally:
            await gateway.close()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(KeyError)
    async def key_error_handler(request, exc):
        del request
        return _json_response({"detail": str(exc)}, 404)

    @app.exception_handler(PermissionError)
    async def permission_error_handler(request, exc):
        del request
        return _json_response({"detail": str(exc)}, 403)

    @app.exception_handler(ValueError)
    async def value_error_handler(request, exc):
        del request
        return _json_response({"detail": str(exc)}, 400)

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(request, exc):
        del request
        return _json_response({"detail": str(exc)}, 409)

    def authorize(
        authorization: str | None = Header(default=None),
        yy_gateway: str | None = Cookie(default=None),
    ) -> str:
        supplied = bearer_value(authorization) or yy_gateway
        if supplied is None or not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "Gateway 访问凭据无效")
        return supplied

    async def authorize_write(
        request: Request,
        authorization: str | None = Header(default=None),
        yy_gateway: str | None = Cookie(default=None),
        x_csrf_token: str | None = Header(default=None),
    ):
        supplied = authorize(authorization, yy_gateway)
        if bearer_value(authorization) is None and not secrets.compare_digest(x_csrf_token or "", csrf_token):
            raise HTTPException(403, "CSRF 校验失败")
        origin = request.headers.get("origin")
        if origin and origin not in {
            f"http://127.0.0.1:{config.gateway_port}",
            f"http://localhost:{config.gateway_port}",
            "tauri://localhost",
            "http://tauri.localhost",
        }:
            raise HTTPException(403, "Origin 不在本机客户端白名单")
        try:
            async with gateway.write_gate.operation(
                "gateway-api",
                f"{request.method}:{request.url.path}:{uuid4().hex}",
            ):
                yield supplied
        except MaintenanceBlockedError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:* "
                "tauri: ipc:; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
            ),
        })
        return response

    @app.get("/api/v1/health")
    async def health():
        return {
            "status": "ok",
            "service": "yuan-ye-agent-gateway",
            "version": 1,
            **gateway.state_controller.health(),
        }

    @app.post("/api/v1/backup/create", dependencies=[Depends(authorize)])
    async def create_backup(request: BackupCreateRequest):
        # This endpoint deliberately does not acquire a normal write scope: it is
        # the maintenance initiator and must transition the gate to DRAINING.
        return await gateway.create_backup(request.passphrase, request.output)

    @app.get("/api/v1/backup/list", dependencies=[Depends(authorize)])
    async def list_backups():
        return gateway.backup_service.list()

    @app.get("/api/v1/backup/status", dependencies=[Depends(authorize)])
    async def backup_status():
        return {
            "maintenance": gateway.maintenance.snapshot.model_dump(mode="json"),
            **gateway.backup_scheduler.status(),
            "backup_directory": str(gateway.backup_service.backup_directory),
        }

    @app.get("/api/v1/status", dependencies=[Depends(authorize)])
    async def status():
        sandbox_status = await probe_docker_status()
        cron_status = await gateway.cron_status()
        dream_status = gateway.dream_status()
        return {
            "gateway": "running",
            "version": 1,
            "provider": config.provider,
            "model": config.model,
            "stream": config.stream,
            "proxy_mode": (
                "explicit" if config.proxy_url
                else "system" if config.use_system_proxy
                else "disabled"
            ),
            "sandbox": sandbox_status.bash_available,
            "sandbox_mode": sandbox_status.mode,
            "bash_available": sandbox_status.bash_available,
            "sandbox_reason": (
                sandbox_status.message
                if sandbox_status.mode == "checkpoint_only"
                else None
            ),
            "max_concurrent_runs": config.gateway_max_concurrent_runs,
            "cron": cron_status.model_dump(mode="json"),
            "dream": dream_status.model_dump(mode="json"),
        }

    @app.get("/api/v1/bootstrap", dependencies=[Depends(authorize)])
    async def bootstrap():
        return {"csrf": csrf_token}

    @app.post("/api/v1/projects", dependencies=[Depends(authorize_write)])
    async def create_project(payload: ProjectCreateRequest):
        return gateway.register_project(Path(payload.path), payload.name)

    @app.get("/api/v1/projects", dependencies=[Depends(authorize)])
    async def list_projects():
        return gateway.store.list_projects()

    @app.delete("/api/v1/projects/{project_id}", dependencies=[Depends(authorize_write)])
    async def delete_project(project_id: str):
        await gateway.remove_project(project_id)
        return {"removed": True}

    @app.get("/api/v1/cron/jobs", dependencies=[Depends(authorize)])
    async def list_cron(project_id: str | None = None):
        return await gateway.cron_service.list(project_id)

    @app.get("/api/v1/cron/status", dependencies=[Depends(authorize)])
    async def cron_status():
        return await gateway.cron_status()

    @app.post("/api/v1/cron/preview", dependencies=[Depends(authorize)])
    async def cron_preview(payload: CronPreviewRequest):
        return gateway.cron_preview(payload.schedule, payload.count)

    @app.post("/api/v1/cron/jobs", dependencies=[Depends(authorize_write)])
    async def create_cron(payload: CronJobCreateRequest):
        return await gateway.create_cron(payload)

    @app.patch("/api/v1/cron/jobs/{job_id}", dependencies=[Depends(authorize_write)])
    async def edit_cron(job_id: str, payload: CronJobEditRequest):
        return await gateway.edit_cron(job_id, payload)

    @app.post("/api/v1/cron/jobs/{job_id}/pause", dependencies=[Depends(authorize_write)])
    async def pause_cron(job_id: str):
        return await gateway.pause_cron(job_id)

    @app.post("/api/v1/cron/jobs/{job_id}/resume", dependencies=[Depends(authorize_write)])
    async def resume_cron(job_id: str):
        return await gateway.resume_cron(job_id)

    @app.post("/api/v1/cron/jobs/{job_id}/run", dependencies=[Depends(authorize_write)])
    async def run_cron(job_id: str):
        return await gateway.run_cron(job_id)

    @app.delete("/api/v1/cron/jobs/{job_id}", dependencies=[Depends(authorize_write)])
    async def remove_cron(job_id: str):
        return await gateway.remove_cron(job_id)

    @app.get("/api/v1/dream/status", dependencies=[Depends(authorize)])
    async def dream_status():
        return gateway.dream_status()

    @app.post("/api/v1/dream/run", dependencies=[Depends(authorize_write)])
    async def run_dream(payload: DreamRunRequest):
        return await gateway.run_dream(payload.date)

    @app.post("/api/v1/dream/backfill", dependencies=[Depends(authorize_write)])
    async def backfill_dream(payload: DreamBackfillRequest):
        return await gateway.backfill_dream(payload.start, payload.end)

    @app.post("/api/v1/dream/rollback", dependencies=[Depends(authorize_write)])
    async def rollback_dream(payload: DreamRollbackRequest):
        return await gateway.rollback_dream(payload.run_id)

    @app.get("/api/v1/projects/{project_id}/sessions", dependencies=[Depends(authorize)])
    async def list_sessions(project_id: str):
        return gateway.sessions(project_id)

    @app.get("/api/v1/projects/{project_id}/sessions/{session_id}", dependencies=[Depends(authorize)])
    async def show_session(project_id: str, session_id: str):
        return gateway.session_records(project_id, session_id)

    @app.post("/api/v1/runs", dependencies=[Depends(authorize_write)])
    async def start_run(
        payload: RunCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        if (
            idempotency_key is not None
            and payload.idempotency_key is not None
            and idempotency_key != payload.idempotency_key
        ):
            raise HTTPException(
                status_code=400,
                detail="Header 与 body 的 Idempotency-Key 不一致",
            )
        selected = idempotency_key or payload.idempotency_key
        return await gateway.start_run(payload.model_copy(update={"idempotency_key": selected}))

    @app.get("/api/v1/runs", dependencies=[Depends(authorize)])
    async def list_runs(project_id: str | None = None):
        return gateway.store.list_runs(project_id)

    @app.get("/api/v1/runs/{run_id}", dependencies=[Depends(authorize)])
    async def get_run(run_id: str):
        return gateway.store.run(run_id)

    @app.get("/api/v1/runs/{run_id}/state", dependencies=[Depends(authorize)])
    async def get_run_state(run_id: str):
        return gateway.state_controller.state(run_id)

    @app.get("/api/v1/runs/{run_id}/operations", dependencies=[Depends(authorize)])
    async def get_run_operations(run_id: str):
        return [
            {
                "operation": operation,
                "attempts": gateway.state_controller.operation_attempts(operation.operation_id),
            }
            for operation in gateway.state_controller.operations(run_id)
        ]

    @app.get("/api/v1/runs/{run_id}/transitions", dependencies=[Depends(authorize)])
    async def get_run_transitions(run_id: str):
        return gateway.state_controller.transitions(run_id)

    @app.get("/api/v1/runs/{run_id}/recovery-decisions", dependencies=[Depends(authorize)])
    async def get_recovery_decisions(run_id: str):
        return gateway.state_controller.recovery_decisions(run_id)

    @app.post("/api/v1/runs/{run_id}/recovery", dependencies=[Depends(authorize_write)])
    async def recover_run(run_id: str, payload: RecoveryDecisionRequest):
        return gateway.recover_run(run_id, payload)

    @app.post("/api/v1/runs/{run_id}/cancel", dependencies=[Depends(authorize_write)])
    async def cancel_run(run_id: str):
        return {"cancelled": await gateway.cancel_run(run_id)}

    @app.post("/api/v1/code/sessions", dependencies=[Depends(authorize_write)])
    async def start_code_session(payload: CodeSessionCreateRequest):
        return await gateway.start_code_session(payload)

    @app.post("/api/v1/code/sessions/{session_id}/turns", dependencies=[Depends(authorize_write)])
    async def run_code_turn(session_id: str, payload: CodeTurnRequest):
        return await gateway.run_code_turn(session_id, payload)

    @app.post("/api/v1/code/sessions/{session_id}/finalize", dependencies=[Depends(authorize_write)])
    async def finalize_code_session(session_id: str, client_id: str):
        return await gateway.finalize_code_session(session_id, client_id)

    @app.post("/api/v1/code/sessions/{session_id}/abort", dependencies=[Depends(authorize_write)])
    async def abort_code_session(session_id: str, client_id: str):
        return await gateway.abort_code_session(session_id, client_id)

    @app.get("/api/v1/code/sessions/{session_id}/events", dependencies=[Depends(authorize)])
    async def code_session_events(session_id: str, after_sequence: int = 0):
        return gateway.code_session_events(session_id, after_sequence)

    @app.get("/api/v1/runs/{run_id}/events", dependencies=[Depends(authorize)])
    async def run_events(run_id: str, after_sequence: int = 0):
        return gateway.run_events(run_id, after_sequence)

    @app.post("/api/v1/approvals/{approval_id}", dependencies=[Depends(authorize_write)])
    async def approval(approval_id: str, decision: ApprovalDecision):
        return {"approved": await gateway.decide_approval(approval_id, decision)}

    @app.get("/api/v1/inbox", dependencies=[Depends(authorize)])
    async def inbox(unread_only: bool = False):
        return gateway.store.list_inbox(unread_only=unread_only)

    @app.post("/api/v1/inbox/{item_id}/read", dependencies=[Depends(authorize_write)])
    async def read_inbox(item_id: str):
        return gateway.store.mark_inbox_read(item_id)

    @app.get("/api/v1/projects/{project_id}/skills", dependencies=[Depends(authorize)])
    async def skills(project_id: str):
        return gateway.skills(project_id).catalog()

    @app.post(
        "/api/v1/projects/{project_id}/sessions/{session_id}/skills/refresh",
        dependencies=[Depends(authorize_write)],
    )
    async def refresh_skills(project_id: str, session_id: str):
        return await gateway.pool.refresh_skills(project_id, session_id)

    @app.get("/api/v1/projects/{project_id}/skills/audit/{review_id}", dependencies=[Depends(authorize)])
    async def audit_skill(project_id: str, review_id: str):
        return gateway.skills(project_id).audit_report(review_id)

    @app.post("/api/v1/skills/manage", dependencies=[Depends(authorize_write)])
    async def manage_skill(payload: SkillManageRequest):
        return await gateway.manage_skill(payload)

    @app.post("/api/v1/browser/code", dependencies=[Depends(authorize_write)])
    async def browser_code():
        code = gateway.issue_browser_code()
        return {"url": f"http://127.0.0.1:{config.gateway_port}/?bootstrap={code}"}

    @app.post("/api/v1/browser/exchange")
    async def browser_exchange(payload: BrowserExchangeRequest, response: Response):
        if not gateway.consume_browser_code(payload.code):
            raise HTTPException(401, "浏览器启动码无效或已过期")
        response.set_cookie(
            "yy_gateway",
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=86400,
        )
        return {"csrf": csrf_token}

    @app.websocket("/api/v1/events")
    async def events(socket: WebSocket):
        supplied = socket.query_params.get("token") or socket.cookies.get("yy_gateway")
        client_id = socket.query_params.get("client_id") or ""
        run_id = socket.query_params.get("run_id") or None
        try:
            after_sequence = int(socket.query_params.get("after_sequence") or "0")
        except ValueError:
            await socket.close(code=1008)
            return
        if not client_id or not supplied or not secrets.compare_digest(supplied, token):
            await socket.close(code=1008)
            return
        origin = socket.headers.get("origin")
        if origin and origin not in {
            f"http://127.0.0.1:{config.gateway_port}",
            f"http://localhost:{config.gateway_port}",
            "tauri://localhost",
            "http://tauri.localhost",
        }:
            await socket.close(code=1008)
            return
        await socket.accept()
        gateway.store.client_connected(client_id)
        subscription_id, queue = await gateway.events.subscribe(client_id, run_id)
        terminal_events = {"run_completed", "run_failed", "run_cancelled", "run_interrupted"}

        def acknowledge_if_origin(event) -> None:
            if event.type not in terminal_events:
                return
            try:
                run = gateway.store.run(event.run_id)
            except KeyError:
                return
            if run.client_id == client_id:
                gateway.store.mark_run_inbox_read(event.run_id)

        try:
            last_sent = after_sequence
            if run_id:
                for event in gateway.run_events(run_id, after_sequence):
                    await socket.send_text(event.model_dump_json())
                    acknowledge_if_origin(event)
                    last_sent = max(last_sent, event.sequence)
            while True:
                event = await queue.get()
                if event.run_id == run_id and event.sequence <= last_sent:
                    continue
                await socket.send_text(event.model_dump_json())
                acknowledge_if_origin(event)
                if event.run_id == run_id:
                    last_sent = event.sequence
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await gateway.events.unsubscribe(subscription_id)
            if not await gateway.events.is_connected(client_id):
                gateway.store.client_disconnected(client_id)
                await gateway.disconnect_client(client_id)

    ui_dist = Path(__file__).resolve().parents[1] / "ui" / "dist"
    legacy = Path(__file__).resolve().parents[1] / "run_ui"

    @app.get("/")
    async def index(bootstrap: str | None = None):
        del bootstrap
        path = ui_dist / "index.html"
        if path.exists():
            return FileResponse(path, media_type="text/html")
        fallback = legacy / "templates" / "index.html"
        if fallback.exists():
            return FileResponse(fallback, media_type="text/html")
        return HTMLResponse("<h1>Yuan Ye Gateway</h1><p>前端尚未构建。</p>")

    @app.get("/assets/{asset_path:path}")
    async def assets(asset_path: str):
        target = (ui_dist / "assets" / asset_path).resolve()
        assets_root = (ui_dist / "assets").resolve()
        if assets_root in target.parents and target.is_file():
            return FileResponse(target)
        fallback = (legacy / "static" / asset_path).resolve()
        fallback_root = (legacy / "static").resolve()
        if fallback_root not in fallback.parents or not fallback.is_file():
            raise HTTPException(404, "资源不存在")
        return FileResponse(fallback)

    app.state.gateway = gateway
    app.state.access_token = token
    app.state.csrf_token = csrf_token
    return app


def _json_response(value: dict[str, Any], status_code: int):
    from fastapi.responses import JSONResponse
    return JSONResponse(value, status_code=status_code)
