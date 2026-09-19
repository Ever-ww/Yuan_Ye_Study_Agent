"""FastAPI 本机 API、WebSocket 事件桥和共享 Web 入口。"""

import asyncio
import json
import mimetypes
import secrets
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from Agent import load_runtime_config
from cron import (
    CronJobCreateRequest,
    CronJobEditRequest,
    CronPaperResearchPresetRequest,
    CronPreviewRequest,
)
from dream import DreamBackfillRequest, DreamRollbackRequest, DreamRunRequest
from gateway.application import GatewayApplication, RunUIContextConflict
from gateway.models import (
    ApprovalDecision,
    BrowserExchangeRequest,
    CodeSessionCreateRequest,
    CodeTurnRequest,
    HarnessDreamDecisionRequest,
    HarnessDreamFreezeRequest,
    HarnessDreamRunRequest,
    HarnessDreamRevertRequest,
    HarnessEvolutionDecision,
    ProjectCreateRequest,
    PaperPatchRequest,
    RunCreateRequest,
    RecoveryDecisionRequest,
    SkillManageRequest,
    ExtensionGrantRequest,
    ExtensionReenableRequest,
    RuntimeReloadRequest,
    RuntimePluginRollbackRequest,
    ObserverCorrectionDecisionRequest,
    ObserverSkillCandidateDecisionRequest,
    WorkspaceEntryCreateRequest,
    WorkspaceFileWriteRequest,
    WorkspaceMoveRequest,
    LatexCompilationRequest,
)
from gateway.security import GatewayCredentials, bearer_value
from sandbox import probe_sandbox_status
from backup import BackupCreateRequest, MaintenanceBlockedError, external_control_root
from backup.models import MaintenanceQuiesceRequest, MaintenanceResumeRequest
from reference import PaperNoteCreate, PaperNoteUpdate, ReferenceSearchRequest
from gateway.workspace_files import WorkspaceFileConflict
from gateway.latex import LatexEngineUnavailable, LatexWorkspaceRevisionConflict


def create_gateway_api(
    application: GatewayApplication | None = None,
    *,
    access_token: str | None = None,
) -> Any:
    try:
        from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError("Gateway API 需要安装 fastapi") from exc

    config = application.config if application is not None else load_runtime_config()
    gateway = application or GatewayApplication(config)
    token = access_token or GatewayCredentials(
        external_control_root(config.agent_root) / "control" / "gateway",
    ).rotate()
    csrf_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        await gateway.start()
        try:
            yield
        finally:
            await gateway.close()

    app = FastAPI(lifespan=lifespan)
    app.state.gateway = gateway

    def error_body(code: str, message: str, *, recoverable: bool = False, details=None):
        return {
            "detail": message,
            "error": {
                "code": code,
                "message": message,
                "recoverable": recoverable,
                "details": details or {},
            },
            "correlation_id": uuid4().hex,
        }

    def public_code_payload(value):
        """Remove host paths from every Code API response and audit projection."""
        if hasattr(value, "model_dump"):
            raw = value.model_dump(mode="json")
        else:
            raw = value
        replacements: list[tuple[str, str]] = []

        def collect(item):
            if isinstance(item, dict):
                for key, selected in item.items():
                    if key == "worktree_path" and selected:
                        replacements.append((str(selected), "YYAgentSource:\\"))
                    elif key == "source_root" and selected:
                        replacements.append((str(selected), "YYAgentSource:\\"))
                    collect(selected)
            elif isinstance(item, list):
                for selected in item:
                    collect(selected)

        collect(raw)
        replacements.extend([
            (str(config.agent_root), "[Agent Home]"),
            (str(config.workspace_root), "YYWorkspace:\\"),
        ])
        replacements.sort(key=lambda item: len(item[0]), reverse=True)

        def clean(item):
            if isinstance(item, dict):
                return {
                    str(key): clean(selected)
                    for key, selected in item.items()
                    if key not in {"source_root", "worktree_path", "audit_path"}
                }
            if isinstance(item, list):
                return [clean(selected) for selected in item]
            if isinstance(item, str):
                result = item
                for host, logical in replacements:
                    for variant in {host, host.replace("\\", "/"), host.replace("/", "\\")}:
                        result = result.replace(variant, logical)
                return result
            return item

        result = clean(raw)
        if isinstance(result, dict) and "code_session_id" in result:
            result.setdefault("logical_roots", {
                "source": "YYAgentSource:\\",
                "skills": "YYSkills:\\",
                "hooks": "YYHooks:\\",
            })
        return result

    def public_paper(value):
        raw = value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)
        files = raw.pop("files", []) or []
        raw["files"] = [
            {
                "relative_path": item.get("relative_path"),
                "sha256": item.get("sha256"),
                "mime_type": item.get("mime_type"),
                "size_bytes": item.get("size_bytes"),
                "is_primary": item.get("is_primary"),
            }
            for item in files
        ]
        raw["has_pdf"] = any(
            item.get("mime_type") == "application/pdf" for item in raw["files"]
        )
        raw["content_hash"] = next(
            (item.get("sha256") for item in raw["files"] if item.get("is_primary")), None,
        )
        raw.pop("source_workspace", None)
        return raw

    def paper_file(paper_id: str) -> tuple[Path, dict[str, Any]]:
        paper = gateway.reference_store.get_paper(paper_id)
        selected = next((item for item in paper.files if item.is_primary), None)
        if selected is None:
            raise HTTPException(404, "Paper does not have a local PDF")
        path = Path(selected.absolute_path)
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Paper PDF is unavailable")
        resolved = path.resolve()
        allowed = [
            (config.agent_root / ".yy" / "papers").resolve(),
            *(Path(project.path).resolve() for project in gateway.store.list_projects()),
        ]
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
            raise PermissionError("Paper PDF is outside registered library roots")
        return resolved, {
            "sha256": selected.sha256,
            "mime_type": selected.mime_type,
            "size_bytes": selected.size_bytes,
        }

    @app.exception_handler(HTTPException)
    async def http_error_handler(request, exc):
        del request
        message = str(exc.detail)
        return _json_response(
            error_body(f"http_{exc.status_code}", message, recoverable=exc.status_code in {409, 429, 503}),
            exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(MaintenanceBlockedError)
    async def maintenance_blocked(request, exc):
        try:
            snapshot = gateway.maintenance.snapshot.model_dump(mode="json")
        except Exception:
            # A broken control store must not turn a refusal into an exception
            # handler recursion or a fabricated RUNNING/FAILED durable state.
            snapshot = None
        body = error_body(
            "maintenance_blocked", str(exc), recoverable=True,
            details={"maintenance": snapshot, "accepting_work": False},
        )
        body.update({"maintenance": snapshot, "accepting_work": False})
        return _json_response(body, 503)

    @app.exception_handler(KeyError)
    async def key_error_handler(request, exc):
        del request
        return _json_response(error_body("not_found", str(exc)), 404)

    @app.exception_handler(PermissionError)
    async def permission_error_handler(request, exc):
        del request
        return _json_response(error_body("permission_denied", str(exc)), 403)

    @app.exception_handler(ValueError)
    async def value_error_handler(request, exc):
        del request
        return _json_response(error_body("invalid_request", str(exc)), 400)

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(request, exc):
        del request
        return _json_response(error_body("runtime_conflict", str(exc), recoverable=True), 409)

    @app.exception_handler(WorkspaceFileConflict)
    async def workspace_file_conflict_handler(request, exc):
        del request
        return _json_response(error_body(
            "file_conflict", "文件已经被其他操作修改", recoverable=True,
            details={"current": exc.metadata},
        ), 409)

    @app.exception_handler(RunUIContextConflict)
    async def run_ui_context_conflict_handler(request, exc):
        del request
        return _json_response(error_body(
            "ui_context_conflict", str(exc), recoverable=True,
        ), 409)

    def authorize(
        authorization: str | None = Header(default=None),
        yy_gateway: str | None = Cookie(default=None),
    ) -> str:
        supplied = bearer_value(authorization) or yy_gateway
        if supplied is None or not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "Gateway 访问凭据无效")
        return supplied

    async def authorize_control(
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
        return supplied

    async def authorize_write(request: Request, supplied=Depends(authorize_control)):
        try:
            async with gateway.write_gate.operation(
                "gateway-api",
                f"{request.method}:{request.url.path}:{uuid4().hex}",
                kind="request",
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
                "tauri: ipc:; img-src 'self' data:; worker-src 'self'; frame-src 'self' blob:; "
                "object-src 'none'; base-uri 'self'; style-src 'self' 'unsafe-inline'"
            ),
        })
        return response

    @app.get("/api/v1/health")
    async def health():
        return {
            "status": "ok",
            "service": "yuan-ye-agent-gateway",
            "version": 1,
            **gateway.health(),
        }

    @app.get("/api/v1/maintenance", dependencies=[Depends(authorize)])
    async def maintenance_status():
        return gateway.maintenance.snapshot

    @app.post("/api/v1/maintenance/quiesce", dependencies=[Depends(authorize_control)])
    async def quiesce_gateway(request: MaintenanceQuiesceRequest):
        return await gateway.quiesce(request.timeout, request.reason)

    @app.post("/api/v1/maintenance/resume", dependencies=[Depends(authorize_control)])
    async def resume_gateway(request: MaintenanceResumeRequest):
        return await gateway.resume(request.maintenance_epoch, expected_revision=request.expected_revision)

    @app.post("/api/v1/backup/create", dependencies=[Depends(authorize_control)])
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
            "key_mode": config.backup_key_mode,
            "system_key": gateway.backup_service.key_status(),
            "storage": gateway.backup_service.storage_status(),
        }

    @app.get("/api/v1/status", dependencies=[Depends(authorize)])
    async def status():
        sandbox_status = await probe_sandbox_status(config)
        cron_status = await gateway.cron_status()
        dream_status = gateway.dream_status()
        return {
            "gateway": gateway.write_gate.state.value,
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
            "sandbox_backend": sandbox_status.backend,
            "sandbox_shell": sandbox_status.shell,
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

    @app.get("/api/v1/models", dependencies=[Depends(authorize)])
    async def model_options(
        session_id: str | None = None,
        project_id: str | None = None,
    ):
        return gateway.model_options(session_id, project_id)

    @app.get("/api/v1/extensions/status", dependencies=[Depends(authorize)])
    async def extension_status(hook_id: str | None = None):
        return gateway.extension_status(hook_id)

    @app.post("/api/v1/extensions/grant", dependencies=[Depends(authorize_write)])
    async def extension_grant(payload: ExtensionGrantRequest):
        return gateway.grant_extension(payload)

    @app.post("/api/v1/extensions/revoke", dependencies=[Depends(authorize_write)])
    async def extension_revoke(payload: ExtensionGrantRequest):
        return gateway.grant_extension(payload, revoke=True)

    @app.post("/api/v1/extensions/reenable", dependencies=[Depends(authorize_write)])
    async def extension_reenable(payload: ExtensionReenableRequest):
        return gateway.reenable_extension(payload)

    @app.get("/api/v1/runtime/plugins/status", dependencies=[Depends(authorize)])
    async def runtime_plugin_status():
        return gateway.runtime_plugin_status()

    @app.post("/api/v1/runtime/plugins/reload", dependencies=[Depends(authorize_write)])
    async def reload_runtime_plugins(payload: RuntimeReloadRequest):
        return gateway.reload_runtime_plugins(
            actor=payload.actor, approved_plan_hash=payload.approved_plan_hash,
        )

    @app.exception_handler(LatexEngineUnavailable)
    async def latex_engine_unavailable_handler(request, exc):
        del request
        return _json_response({
            "detail": str(exc),
            "error": {
                "code": "latex_engine_unavailable",
                "message": str(exc),
                "recoverable": True,
                "details": {},
            },
            "correlation_id": uuid4().hex,
        }, 409)

    @app.post("/api/v1/runtime/plugins/rollback", dependencies=[Depends(authorize_write)])
    async def rollback_runtime_plugin(payload: RuntimePluginRollbackRequest):
        return gateway.rollback_runtime_plugin(
            payload.plugin_id,
            from_generation_id=payload.from_generation_id,
            actor=payload.actor,
        )

    @app.exception_handler(LatexWorkspaceRevisionConflict)
    async def latex_workspace_revision_conflict_handler(request, exc):
        del request
        return _json_response(error_body(
            "workspace_revision_conflict", str(exc), recoverable=True,
        ), 409)

    @app.get("/api/v1/observer/runs/{run_id}", dependencies=[Depends(authorize)])
    async def observer_status(run_id: str):
        try:
            return gateway.observer_status(run_id)
        except KeyError as exc:
            # Observer creation is asynchronous relative to Main output. A
            # short-lived missing projection is an ordinary not-ready result,
            # not a Gateway internal error.
            raise HTTPException(404, "Observer state is not ready") from exc

    @app.post(
        "/api/v1/observer/corrections/{proposal_id}/decision",
        dependencies=[Depends(authorize_write)],
    )
    async def decide_observer_correction(
        proposal_id: str, payload: ObserverCorrectionDecisionRequest,
    ):
        return await gateway.decide_observer_correction(
            proposal_id, expected_revision=payload.expected_revision,
            action=payload.action, actor=payload.actor,
            edited_prompt=payload.edited_prompt, reason=payload.reason,
        )

    @app.get("/api/v1/observer/skill-candidates", dependencies=[Depends(authorize)])
    async def observer_skill_candidates():
        return gateway.observer_skill_candidates()

    @app.post(
        "/api/v1/observer/skill-candidates/{candidate_id}/decision",
        dependencies=[Depends(authorize_write)],
    )
    async def decide_observer_skill_candidate(
        candidate_id: str, payload: ObserverSkillCandidateDecisionRequest,
    ):
        return gateway.decide_observer_skill_candidate(
            candidate_id, expected_revision=payload.expected_revision,
            approved=payload.approved, actor=payload.actor,
        )

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

    @app.get("/api/v1/projects/{project_id}/workspace/tree", dependencies=[Depends(authorize)])
    async def workspace_tree(
        project_id: str, path: str = "YYWorkspace:\\",
        cursor: str | None = None, limit: int = 200,
    ):
        if limit < 1 or limit > 500:
            raise HTTPException(400, "limit must be between 1 and 500")
        project = gateway.store.project(project_id)
        try:
            return await gateway.workspace_files.tree(
                Path(project.path), path, cursor=cursor, limit=limit,
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HTTPException(404, "Workspace directory was not found") from exc

    @app.get("/api/v1/projects/{project_id}/workspace/files", dependencies=[Depends(authorize)])
    async def workspace_file(project_id: str, path: str):
        project = gateway.store.project(project_id)
        try:
            return await gateway.workspace_files.read(Path(project.path), path)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Workspace file was not found") from exc

    @app.get("/api/v1/projects/{project_id}/workspace/raw", dependencies=[Depends(authorize)])
    async def workspace_raw(project_id: str, path: str):
        project = gateway.store.project(project_id)
        try:
            selected, metadata = gateway.workspace_files.raw_path(Path(project.path), path)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Workspace file was not found") from exc
        return FileResponse(
            selected,
            media_type=mimetypes.guess_type(selected.name)[0] or "application/octet-stream",
            headers={"ETag": f'"{metadata["etag"]}"', "Accept-Ranges": "bytes"},
        )

    @app.get("/api/v1/projects/{project_id}/workspace/changes", dependencies=[Depends(authorize)])
    async def workspace_changes(project_id: str):
        project = gateway.store.project(project_id)
        return gateway.workspace_files.changes(Path(project.path))

    @app.get("/api/v1/projects/{project_id}/workspace/events", dependencies=[Depends(authorize)])
    async def workspace_events(project_id: str, after_sequence: int = 0):
        gateway.store.project(project_id)
        return gateway.stream_events(
            f"project:{project_id}:workspace", after_sequence,
        )

    @app.put("/api/v1/projects/{project_id}/workspace/files", dependencies=[Depends(authorize_write)])
    async def write_workspace_file(project_id: str, payload: WorkspaceFileWriteRequest):
        project = gateway.store.project(project_id)
        result = await gateway.workspace_files.write(
            Path(project.path), payload.path, payload.content,
            expected_etag=payload.expected_etag,
        )
        event = gateway.record_workspace_event(project_id, "workspace.file.changed", {
            "path": result["path"], "etag": result["etag"], "size": result["size"],
            "source": "web",
        })
        return {**result, "workspace_revision": event.stream_sequence}

    @app.post("/api/v1/projects/{project_id}/workspace/entries", dependencies=[Depends(authorize_write)])
    async def create_workspace_entry(project_id: str, payload: WorkspaceEntryCreateRequest):
        project = gateway.store.project(project_id)
        result = await gateway.workspace_files.create_entry(
            Path(project.path), payload.path, kind=payload.kind,
        )
        event = gateway.record_workspace_event(project_id, "workspace.file.created", {
            "path": result["path"], "kind": result["kind"], "source": "web",
        })
        return {**result, "workspace_revision": event.stream_sequence}

    @app.post("/api/v1/projects/{project_id}/workspace/move", dependencies=[Depends(authorize_write)])
    async def move_workspace_entry(project_id: str, payload: WorkspaceMoveRequest):
        project = gateway.store.project(project_id)
        result = await gateway.workspace_files.move(
            Path(project.path), payload.source, payload.destination,
        )
        event = gateway.record_workspace_event(project_id, "workspace.file.moved", {
            "source_path": payload.source,
            "path": result["entry"]["path"],
            "kind": result["entry"]["kind"],
            "source": "web",
        })
        return {**result, "workspace_revision": event.stream_sequence}

    @app.delete("/api/v1/projects/{project_id}/workspace/entries", dependencies=[Depends(authorize_write)])
    async def delete_workspace_entry(project_id: str, path: str):
        project = gateway.store.project(project_id)
        try:
            result = await gateway.workspace_files.delete(Path(project.path), path)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Workspace entry was not found") from exc
        event = gateway.record_workspace_event(project_id, "workspace.file.deleted", {
            "path": path,
            "checkpoint_id": result["checkpoint_id"],
            "source": "web",
        })
        return {**result, "workspace_revision": event.stream_sequence}

    @app.post(
        "/api/v1/projects/{project_id}/latex/compilations",
        dependencies=[Depends(authorize_write)],
    )
    async def start_latex_compilation(
        project_id: str,
        payload: LatexCompilationRequest,
        x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    ):
        try:
            return await gateway.start_latex_compilation(
                project_id, payload, client_id=x_client_id or "web:latex",
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "LaTeX main file was not found") from exc

    @app.get(
        "/api/v1/projects/{project_id}/latex/compilations/{compilation_id}",
        dependencies=[Depends(authorize)],
    )
    async def latex_compilation(project_id: str, compilation_id: str):
        record = gateway.latex_compilation(compilation_id)
        if record.project_id != project_id:
            raise HTTPException(404, "LaTeX compilation was not found")
        return record

    @app.post(
        "/api/v1/projects/{project_id}/latex/compilations/{compilation_id}/cancel",
        dependencies=[Depends(authorize_write)],
    )
    async def cancel_latex_compilation(project_id: str, compilation_id: str):
        record = gateway.latex_compilation(compilation_id)
        if record.project_id != project_id:
            raise HTTPException(404, "LaTeX compilation was not found")
        return await gateway.cancel_latex_compilation(compilation_id)

    @app.get(
        "/api/v1/projects/{project_id}/latex/compilations/{compilation_id}/log",
        dependencies=[Depends(authorize)],
    )
    async def latex_compilation_log(project_id: str, compilation_id: str):
        record = gateway.latex_compilation(compilation_id)
        if record.project_id != project_id:
            raise HTTPException(404, "LaTeX compilation was not found")
        path = gateway.latex_artifact(compilation_id, "log")
        return FileResponse(path, media_type="text/plain; charset=utf-8")

    @app.get(
        "/api/v1/projects/{project_id}/latex/compilations/{compilation_id}/pdf",
        dependencies=[Depends(authorize)],
    )
    async def latex_compilation_pdf(project_id: str, compilation_id: str):
        record = gateway.latex_compilation(compilation_id)
        if record.project_id != project_id:
            raise HTTPException(404, "LaTeX compilation was not found")
        path = gateway.latex_artifact(compilation_id, "pdf")
        return FileResponse(path, media_type="application/pdf", headers={"Accept-Ranges": "bytes"})

    @app.get("/api/v1/library/papers", dependencies=[Depends(authorize)])
    async def library_papers(
        response: Response, cursor: str | None = None, limit: int = 100,
        include_archived: bool = False,
    ):
        papers = gateway.reference_store.list_papers(
            include_archived=include_archived, cursor=cursor, limit=limit + 1,
        )
        page = papers[:limit]
        if len(papers) > limit and page:
            response.headers["X-Next-Cursor"] = page[-1].paper_id
        return [public_paper(paper) for paper in page]

    @app.get("/api/v1/library/papers/{paper_id}", dependencies=[Depends(authorize)])
    async def library_paper(paper_id: str):
        return public_paper(gateway.reference_store.get_paper(paper_id))

    @app.patch("/api/v1/library/papers/{paper_id}", dependencies=[Depends(authorize_write)])
    async def update_library_paper(paper_id: str, payload: PaperPatchRequest):
        return public_paper(gateway.reference_store.archive(
            paper_id, archived=payload.status == "archived",
        ))

    @app.get("/api/v1/library/papers/{paper_id}/pdf", dependencies=[Depends(authorize)])
    async def library_paper_pdf(paper_id: str):
        path, metadata = paper_file(paper_id)
        return FileResponse(
            path, media_type=metadata["mime_type"] or "application/pdf",
            headers={"ETag": f'"{metadata["sha256"]}"', "Accept-Ranges": "bytes"},
        )

    @app.get("/api/v1/library/papers/{paper_id}/text", dependencies=[Depends(authorize)])
    async def library_paper_text(
        paper_id: str, start_page: int | None = None, end_page: int | None = None,
        offset_chars: int = 0, max_chars: int = 30_000,
    ):
        if max_chars < 1 or max_chars > 100_000:
            raise HTTPException(400, "max_chars must be between 1 and 100000")
        path, metadata = paper_file(paper_id)
        from tools.read_file import DocumentReader
        content = await DocumentReader().read_path(
            path,
            display_path=f"paper:{paper_id}",
            arguments={
                "start_page": start_page, "end_page": end_page,
                "offset_chars": offset_chars, "max_chars": max_chars,
            },
        )
        return {"paper_id": paper_id, "content": content, "content_hash": metadata["sha256"]}

    @app.get("/api/v1/library/papers/{paper_id}/references", dependencies=[Depends(authorize)])
    async def library_paper_references(paper_id: str):
        bundle = gateway.reference_store.get_bundle("paper", paper_id)
        bundle["paper"] = public_paper(gateway.reference_store.get_paper(paper_id))
        return bundle

    @app.post("/api/v1/library/search", dependencies=[Depends(authorize)])
    async def library_search(payload: ReferenceSearchRequest):
        return await gateway.reference_service.search(payload)

    @app.get("/api/v1/library/papers/{paper_id}/notes", dependencies=[Depends(authorize)])
    async def paper_notes(paper_id: str):
        return gateway.reference_store.list_notes(paper_id)

    @app.post("/api/v1/library/papers/{paper_id}/notes", dependencies=[Depends(authorize_write)])
    async def create_paper_note(paper_id: str, payload: PaperNoteCreate):
        return gateway.reference_store.create_note(paper_id, payload)

    @app.patch(
        "/api/v1/library/papers/{paper_id}/notes/{note_id}",
        dependencies=[Depends(authorize_write)],
    )
    async def update_paper_note(paper_id: str, note_id: str, payload: PaperNoteUpdate):
        try:
            return gateway.reference_store.update_note(paper_id, note_id, payload)
        except RuntimeError as exc:
            if not str(exc).startswith("note_conflict:"):
                raise
            revision = int(str(exc).split(":", 1)[1])
            return _json_response(error_body(
                "note_conflict", "Paper note was modified by another client",
                recoverable=True, details={"current_revision": revision},
            ), 409)

    @app.delete(
        "/api/v1/library/papers/{paper_id}/notes/{note_id}",
        dependencies=[Depends(authorize_write)],
    )
    async def delete_paper_note(paper_id: str, note_id: str):
        return {"deleted": gateway.reference_store.delete_note(paper_id, note_id)}

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

    @app.post("/api/v1/cron/presets/paper-research", dependencies=[Depends(authorize_write)])
    async def initialize_paper_research_cron(payload: CronPaperResearchPresetRequest):
        return await gateway.initialize_paper_research_cron(payload)

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

    @app.post("/api/v1/cron/jobs/{job_id}/run-now", dependencies=[Depends(authorize_write)])
    async def run_cron_now(job_id: str):
        return await gateway.run_cron(job_id)

    @app.get("/api/v1/cron/jobs/{job_id}/history", dependencies=[Depends(authorize)])
    async def cron_history(job_id: str, limit: int = 100):
        return await gateway.cron_history(job_id, limit=limit)

    @app.post("/api/v1/cron/dispatches/{dispatch_id}/retry", dependencies=[Depends(authorize_write)])
    async def retry_cron_dispatch(dispatch_id: str):
        return await gateway.retry_cron_dispatch(dispatch_id)

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

    @app.get("/api/v1/harness/dream/status", dependencies=[Depends(authorize)])
    async def harness_dream_status():
        return gateway.harness_dream_status()

    @app.post("/api/v1/harness/dream/run", dependencies=[Depends(authorize_write)])
    async def run_harness_dream(payload: HarnessDreamRunRequest):
        if not payload.confirmed:
            raise HTTPException(400, "Explicit Harness Dream requires confirmed=true")
        return await gateway.run_harness_dream(
            payload.selected, automatic=False, actor=payload.client_id,
        )

    @app.post("/api/v1/harness/dream/freeze", dependencies=[Depends(authorize_write)])
    async def freeze_harness_dream(payload: HarnessDreamFreezeRequest):
        return gateway.freeze_harness_dream(actor=payload.client_id, reason=payload.reason)

    @app.post("/api/v1/harness/dream/unfreeze", dependencies=[Depends(authorize_write)])
    async def unfreeze_harness_dream():
        return gateway.unfreeze_harness_dream()

    @app.post(
        "/api/v1/harness/dream/{operation_id}/decision",
        dependencies=[Depends(authorize_write)],
    )
    async def decide_harness_dream(operation_id: str, payload: HarnessDreamDecisionRequest):
        return await gateway.decide_harness_dream(operation_id, payload)

    @app.post(
        "/api/v1/harness/dream/{operation_id}/revert",
        dependencies=[Depends(authorize_write)],
    )
    async def create_harness_dream_revert(
        operation_id: str, payload: HarnessDreamRevertRequest,
    ):
        if not payload.confirmed:
            raise HTTPException(400, "Harness Dream revert requires confirmed=true")
        return await gateway.create_harness_dream_revert(
            operation_id, actor=payload.client_id,
        )

    @app.post(
        "/api/v1/harness/dream/revert/{proposal_id}/decision",
        dependencies=[Depends(authorize_write)],
    )
    async def decide_harness_dream_revert(
        proposal_id: str, payload: HarnessDreamDecisionRequest,
    ):
        return await gateway.decide_harness_dream_revert(proposal_id, payload)

    @app.get("/api/v1/projects/{project_id}/sessions", dependencies=[Depends(authorize)])
    async def list_sessions(project_id: str):
        return gateway.sessions(project_id)

    @app.get("/api/v1/projects/{project_id}/sessions/{session_id}", dependencies=[Depends(authorize)])
    async def show_session(project_id: str, session_id: str):
        return gateway.session_records(project_id, session_id)

    @app.get("/api/v1/projects/{project_id}/sessions/{session_id}/tool-results", dependencies=[Depends(authorize)])
    async def session_tool_result(
        project_id: str, session_id: str, record_id: str | None = None,
        tool_call_id: str | None = None, run_id: str | None = None, content_offset: int = 0,
    ):
        return await gateway.session_tool_result(
            project_id, session_id, record_id=record_id, tool_call_id=tool_call_id,
            run_id=run_id, content_offset=content_offset,
        )

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
        return await gateway.recover_run(run_id, payload)

    @app.post("/api/v1/runs/{run_id}/cancel", dependencies=[Depends(authorize_control)])
    async def cancel_run(run_id: str):
        return {"cancelled": await gateway.cancel_run(run_id)}

    @app.get("/api/v1/code/sessions", dependencies=[Depends(authorize)])
    async def list_code_sessions(
        project_id: str | None = None, status: str | None = None,
    ):
        return public_code_payload(gateway.code_session_summaries(
            project_id=project_id, status=status,
        ))

    @app.get("/api/v1/code/sessions/{session_id}", dependencies=[Depends(authorize)])
    async def code_session_detail(session_id: str):
        return public_code_payload(gateway.code_session_summary(session_id))

    @app.post("/api/v1/code/sessions", dependencies=[Depends(authorize_write)])
    async def start_code_session(payload: CodeSessionCreateRequest):
        return public_code_payload(await gateway.start_code_session(payload))

    @app.post("/api/v1/code/sessions/{session_id}/turns", dependencies=[Depends(authorize_write)])
    async def run_code_turn(session_id: str, payload: CodeTurnRequest):
        return public_code_payload(await gateway.run_code_turn(session_id, payload))

    @app.post("/api/v1/code/sessions/{session_id}/finalize", dependencies=[Depends(authorize_write)])
    async def finalize_code_session(
        session_id: str, client_id: str, approved_plan_hash: str | None = None,
    ):
        return public_code_payload(await gateway.finalize_code_session(
            session_id, client_id, approved_plan_hash,
        ))

    @app.post("/api/v1/code/sessions/{session_id}/abort", dependencies=[Depends(authorize_write)])
    async def abort_code_session(session_id: str, client_id: str):
        return public_code_payload(await gateway.abort_code_session(session_id, client_id))

    @app.delete("/api/v1/code/sessions/{session_id}", dependencies=[Depends(authorize_write)])
    async def delete_code_session(session_id: str, client_id: str):
        return public_code_payload(await gateway.delete_code_session(session_id, client_id))

    @app.get("/api/v1/code/sessions/{session_id}/events", dependencies=[Depends(authorize)])
    async def code_session_events(session_id: str, after_sequence: int = 0):
        return public_code_payload(gateway.code_session_events(session_id, after_sequence))

    @app.post("/api/v1/harness/evolution/{proposal_id}/decision", dependencies=[Depends(authorize_write)])
    async def decide_harness_evolution(proposal_id: str, payload: HarnessEvolutionDecision):
        return await gateway.decide_harness_evolution(proposal_id, payload)

    @app.get("/api/v1/runs/{run_id}/events", dependencies=[Depends(authorize)])
    async def run_events(run_id: str, after_sequence: int = 0):
        return gateway.run_events(run_id, after_sequence)

    @app.get("/api/v1/approvals", dependencies=[Depends(authorize)])
    async def approvals(project_id: str | None = None, state: str = "pending"):
        if state != "pending":
            raise HTTPException(400, "Only pending approvals are exposed to interactive clients")
        return gateway.pending_approvals(project_id)

    @app.post("/api/v1/approvals/{approval_id}", dependencies=[Depends(authorize_control)])
    async def approval(approval_id: str, decision: ApprovalDecision):
        return {"approved": await gateway.decide_approval(approval_id, decision)}

    @app.get("/api/v1/inbox", dependencies=[Depends(authorize)])
    async def inbox(
        response: Response,
        unread_only: bool = False,
        cursor: str | None = None,
        limit: int = 100,
        read: bool | None = None,
        status: str | None = None,
        source: str | None = None,
        project_id: str | None = None,
    ):
        if limit < 1 or limit > 200:
            raise HTTPException(400, "Inbox limit must be between 1 and 200")
        try:
            items = gateway.store.list_inbox(
                unread_only=unread_only, cursor=cursor, limit=limit + 1,
                read=read, status=status, source=source, project_id=project_id,
            )
        except KeyError as exc:
            raise HTTPException(400, "Unknown Inbox cursor") from exc
        page = items[:limit]
        if len(items) > limit and page:
            response.headers["X-Next-Cursor"] = page[-1].item_id
        return page

    @app.post("/api/v1/inbox/read-all", dependencies=[Depends(authorize_write)])
    async def read_all_inbox(project_id: str | None = None):
        return {"updated": gateway.store.mark_all_inbox_read(project_id=project_id)}

    @app.get("/api/v1/inbox/{item_id}", dependencies=[Depends(authorize)])
    async def inbox_detail(item_id: str):
        return gateway.store.inbox_item(item_id)

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
            # Validity is fenced by the per-process Gateway token, not a wall
            # clock. The large cookie lifetime only keeps browser restarts from
            # forcing a new exchange while this Gateway instance is unchanged.
            max_age=2_147_483_647,
        )
        return {"csrf": csrf_token}

    @app.websocket("/api/v1/events")
    async def events(socket: WebSocket):
        supplied = socket.query_params.get("token") or socket.cookies.get("yy_gateway")
        client_id = socket.query_params.get("client_id") or ""
        run_id = socket.query_params.get("run_id") or None
        stream_ids = {
            item.strip()
            for item in (socket.query_params.get("stream_ids") or "").split(",")
            if item.strip()
        }
        if run_id:
            stream_ids.add(run_id)
        try:
            after_sequence = int(socket.query_params.get("after_sequence") or "0")
            raw_cursors = socket.query_params.get("after_sequences")
            parsed_cursors = json.loads(raw_cursors) if raw_cursors else {}
            if not isinstance(parsed_cursors, dict):
                raise ValueError("after_sequences must be an object")
            after_sequences = {
                str(key): int(value) for key, value in parsed_cursors.items()
            }
        except ValueError:
            await socket.close(code=1008)
            return
        except json.JSONDecodeError:
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
        subscription_id, queue = await gateway.events.subscribe(
            client_id, run_id, stream_ids=stream_ids,
        )
        terminal_events = {"run_completed", "run_failed", "run_cancelled", "run_interrupted"}

        def acknowledge_if_origin(event) -> None:
            if event.type not in terminal_events or not event.run_id:
                return
            try:
                run = gateway.store.run(event.run_id)
            except KeyError:
                return
            if run.client_id == client_id:
                gateway.store.mark_run_inbox_read(event.run_id)

        try:
            last_sent = {
                stream_id: after_sequences.get(
                    stream_id, after_sequence if stream_id == run_id else 0,
                )
                for stream_id in stream_ids
            }
            for stream_id in sorted(stream_ids):
                for event in gateway.stream_events(stream_id, last_sent[stream_id]):
                    await socket.send_text(event.model_dump_json())
                    acknowledge_if_origin(event)
                    sequence = event.stream_sequence or event.sequence
                    last_sent[stream_id] = max(last_sent[stream_id], sequence)
            # An idle subscription must also receive disconnects. Waiting only
            # on queue.get() strands ASGI tasks when clients leave or Uvicorn
            # closes sockets during graceful shutdown.
            async with aclosing(_subscription_events(socket, queue)) as delivery:
                async for event in delivery:
                    event_stream = event.stream_id or event.run_id
                    if event_stream is None:
                        continue
                    sequence = event.stream_sequence or event.sequence
                    previous = last_sent.get(event_stream, 0)
                    if event_stream in last_sent and sequence <= previous:
                        continue
                    if (
                        event_stream in last_sent
                        and sequence > previous + 1
                    ):
                        # A physical EventBus delivery may arrive after a retry
                        # or queue overflow. Fill the immutable SQLite sequence
                        # gap before forwarding the live item.
                        for missing in gateway.stream_events(event_stream, previous):
                            missing_sequence = missing.stream_sequence or missing.sequence
                            if missing_sequence >= sequence:
                                break
                            await socket.send_text(missing.model_dump_json())
                            acknowledge_if_origin(missing)
                            last_sent[event_stream] = missing_sequence
                    if sequence <= last_sent.get(event_stream, 0):
                        continue
                    await socket.send_text(event.model_dump_json())
                    acknowledge_if_origin(event)
                    last_sent[event_stream] = sequence
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await gateway.events.unsubscribe(subscription_id)
            if not await gateway.events.is_connected(client_id):
                gateway.store.client_disconnected(client_id)
                await gateway.disconnect_client(client_id)

    ui_dist = Path(__file__).resolve().parents[1] / "ui" / "dist"
    def frontend_index():
        path = ui_dist / "index.html"
        if path.is_file():
            return FileResponse(path, media_type="text/html")
        return _json_response({
            "detail": "YYAgent Web frontend has not been built",
            "error": {
                "code": "frontend_build_missing",
                "message": "YYAgent Web frontend has not been built; run npm run build in ui/",
                "recoverable": True,
                "details": {},
            },
            "correlation_id": uuid4().hex,
        }, 503)

    @app.get("/")
    async def index(bootstrap: str | None = None):
        del bootstrap
        return frontend_index()

    @app.get("/assets/{asset_path:path}")
    async def assets(asset_path: str):
        target = (ui_dist / "assets" / asset_path).resolve()
        assets_root = (ui_dist / "assets").resolve()
        if assets_root in target.parents and target.is_file():
            return FileResponse(target)
        raise HTTPException(404, "Frontend asset does not exist")

    @app.get("/{client_path:path}")
    async def client_route(client_path: str):
        if client_path.startswith("api/"):
            raise HTTPException(404, "API route does not exist")
        return frontend_index()

    app.state.gateway = gateway
    app.state.access_token = token
    app.state.csrf_token = csrf_token
    return app


async def _subscription_events(socket, queue):
    """Race delivery with peer disconnect; always reap both helper tasks."""
    async def disconnected():
        while True:
            if (await socket.receive())["type"] == "websocket.disconnect":
                return

    receiver = asyncio.create_task(disconnected())
    pending = None
    try:
        while True:
            pending = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait((receiver, pending), return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                await receiver
                return
            yield pending.result()
    finally:
        tasks = [receiver] + ([pending] if pending is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _json_response(value: dict[str, Any], status_code: int, *, headers=None):
    from fastapi.responses import JSONResponse
    return JSONResponse(value, status_code=status_code, headers=headers)
