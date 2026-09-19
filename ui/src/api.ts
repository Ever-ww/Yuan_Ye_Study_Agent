import type {
  GatewayErrorBody, GatewayEvent, GatewayStatus, InboxItem, ModelOption,
  CodeSessionEvent, CodeSessionSummary, JsonObject, ObserverStatus, PendingApproval,
  CronJobInput, PaperListItem, PaperNote, Project, ReasoningEffort, RunCreate, Session, SessionRecord, ToolResult,
  WorkspaceDocument, WorkspaceEntry, WorkspaceTree, LatexCompilation,
} from "./types";

type Connection = { baseUrl: string; token?: string; csrf?: string };

export class GatewayRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code = "gateway_error",
    readonly recoverable = false,
    readonly correlationId?: string,
    readonly details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "GatewayRequestError";
  }
}

function stableClientId(): string {
  const key = "yyagent.web.client-id";
  try {
    const current = window.localStorage.getItem(key);
    if (current) return current;
    const created = `workbench_${crypto.randomUUID().replaceAll("-", "")}`;
    window.localStorage.setItem(key, created);
    return created;
  } catch {
    return `workbench_${crypto.randomUUID().replaceAll("-", "")}`;
  }
}

export class GatewayApi {
  private connection: Connection = { baseUrl: location.origin };
  readonly clientId = stableClientId();

  async initialize(): Promise<void> {
    if ("__TAURI_INTERNALS__" in window) {
      const { invoke } = await import("@tauri-apps/api/core");
      const value = await invoke<{ base_url: string; token: string }>("gateway_connection");
      this.connection = { baseUrl: value.base_url, token: value.token };
      return;
    }
    const url = new URL(window.location.href);
    const code = url.searchParams.get("bootstrap");
    if (code) {
      const response = await fetch("/api/v1/browser/exchange", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (!response.ok) throw new Error("浏览器启动凭据无效或已经过期");
      const value = await response.json() as { csrf: string };
      this.connection.csrf = value.csrf;
      url.searchParams.delete("bootstrap");
      history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
      return;
    }
    const response = await fetch("/api/v1/bootstrap", { credentials: "include" });
    if (!response.ok) throw new Error("请通过 `run.py serve-ui` 打开本地工作台");
    const value = await response.json() as { csrf: string };
    this.connection.csrf = value.csrf;
  }

  status(): Promise<GatewayStatus> { return this.request("/api/v1/status"); }
  projects(): Promise<Project[]> { return this.request("/api/v1/projects"); }
  models(projectId?: string, sessionId?: string): Promise<ModelOption[]> {
    const query = new URLSearchParams();
    if (projectId) query.set("project_id", projectId);
    if (sessionId) query.set("session_id", sessionId);
    return this.request(`/api/v1/models${query.size ? `?${query}` : ""}`);
  }
  sessions(projectId: string): Promise<Session[]> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/sessions`);
  }
  session(projectId: string, sessionId: string): Promise<SessionRecord[]> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}`);
  }
  toolResult(projectId: string, sessionId: string, recordId: string): Promise<ToolResult> {
    const query = new URLSearchParams({ record_id: recordId });
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/tool-results?${query}`);
  }
  inbox(unreadOnly = false, projectId?: string): Promise<InboxItem[]> {
    const query = new URLSearchParams({ unread_only: String(unreadOnly), limit: "200" });
    if (projectId) query.set("project_id", projectId);
    return this.request(`/api/v1/inbox?${query}`);
  }
  inboxItem(itemId: string): Promise<InboxItem> {
    return this.request(`/api/v1/inbox/${encodeURIComponent(itemId)}`);
  }
  markRead(itemId: string): Promise<InboxItem> {
    return this.request(`/api/v1/inbox/${encodeURIComponent(itemId)}/read`, { method: "POST" });
  }
  markAllRead(projectId?: string): Promise<{ updated: number }> {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return this.request(`/api/v1/inbox/read-all${query}`, { method: "POST" });
  }
  cronJobs(projectId?: string): Promise<JsonObject[]> {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return this.request(`/api/v1/cron/jobs${query}`);
  }
  cronStatus(): Promise<JsonObject> { return this.request("/api/v1/cron/status"); }
  createCron(input: CronJobInput & { project_id: string }): Promise<JsonObject> {
    return this.request("/api/v1/cron/jobs", { method: "POST", body: JSON.stringify(input) });
  }
  editCron(jobId: string, input: CronJobInput): Promise<JsonObject> {
    const { project_id: _projectId, ...payload } = input;
    return this.request(`/api/v1/cron/jobs/${encodeURIComponent(jobId)}`, {
      method: "PATCH", body: JSON.stringify(payload),
    });
  }
  removeCron(jobId: string): Promise<JsonObject> {
    return this.request(`/api/v1/cron/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
  }
  cronHistory(jobId: string): Promise<JsonObject[]> {
    return this.request(`/api/v1/cron/jobs/${encodeURIComponent(jobId)}/history?limit=100`);
  }
  runCron(jobId: string): Promise<JsonObject> {
    return this.request(`/api/v1/cron/jobs/${encodeURIComponent(jobId)}/run-now`, { method: "POST" });
  }
  setCronPaused(jobId: string, paused: boolean): Promise<JsonObject> {
    return this.request(`/api/v1/cron/jobs/${encodeURIComponent(jobId)}/${paused ? "pause" : "resume"}`, { method: "POST" });
  }
  dreamStatus(): Promise<JsonObject> { return this.request("/api/v1/dream/status"); }
  runDream(date?: string): Promise<JsonObject> {
    return this.request("/api/v1/dream/run", {
      method: "POST", body: JSON.stringify({ date: date || null }),
    });
  }
  backfillDream(start: string, end: string): Promise<JsonObject> {
    return this.request("/api/v1/dream/backfill", {
      method: "POST", body: JSON.stringify({ start, end }),
    });
  }
  rollbackDream(runId?: string): Promise<JsonObject> {
    return this.request("/api/v1/dream/rollback", {
      method: "POST", body: JSON.stringify({ run_id: runId || null }),
    });
  }
  harnessDreamStatus(): Promise<JsonObject> { return this.request("/api/v1/harness/dream/status"); }
  runHarnessDream(selected?: string): Promise<JsonObject> {
    return this.request("/api/v1/harness/dream/run", {
      method: "POST",
      body: JSON.stringify({ client_id: this.clientId, selected: selected || null, confirmed: true }),
    });
  }
  setHarnessDreamFrozen(frozen: boolean, reason = "operator freeze from Web"): Promise<JsonObject> {
    return this.request(`/api/v1/harness/dream/${frozen ? "freeze" : "unfreeze"}`, {
      method: "POST",
      ...(frozen ? { body: JSON.stringify({ client_id: this.clientId, reason }) } : {}),
    });
  }
  backupStatus(): Promise<JsonObject> { return this.request("/api/v1/backup/status"); }
  backups(): Promise<JsonObject[]> { return this.request("/api/v1/backup/list"); }
  createBackup(): Promise<JsonObject> {
    return this.request("/api/v1/backup/create", {
      method: "POST", body: JSON.stringify({ passphrase: null, output: null, kind: "manual" }),
    });
  }
  maintenanceStatus(): Promise<JsonObject> { return this.request("/api/v1/maintenance"); }
  runtimePluginStatus(): Promise<JsonObject> { return this.request("/api/v1/runtime/plugins/status"); }
  extensionStatus(): Promise<JsonObject> { return this.request("/api/v1/extensions/status"); }
  skills(projectId: string): Promise<JsonObject[]> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/skills`);
  }
  manageSkill(input: {
    project_id: string; action: "install" | "update"; source: string; ref?: string;
    skill_path?: string; name?: string; confirmed: boolean;
  }): Promise<JsonObject> {
    return this.request("/api/v1/skills/manage", { method: "POST", body: JSON.stringify(input) });
  }
  reloadRuntimePlugins(approvedPlanHash?: string): Promise<JsonObject> {
    return this.request("/api/v1/runtime/plugins/reload", {
      method: "POST",
      body: JSON.stringify({ actor: this.clientId, approved_plan_hash: approvedPlanHash || null }),
    });
  }
  rollbackRuntimePlugin(pluginId: string, fromGenerationId: string): Promise<JsonObject> {
    return this.request("/api/v1/runtime/plugins/rollback", {
      method: "POST",
      body: JSON.stringify({
        plugin_id: pluginId, from_generation_id: fromGenerationId, actor: this.clientId,
      }),
    });
  }
  changeExtensionGrant(input: {
    hook_id: string; stage: string; source_hash: string; manifest_hash: string;
    capabilities: string[]; tools: string[]; tool_contract_hashes: Record<string, string>;
    reason: string;
  }, revoke = false): Promise<JsonObject> {
    return this.request(`/api/v1/extensions/${revoke ? "revoke" : "grant"}`, {
      method: "POST", body: JSON.stringify({ ...input, actor: this.clientId }),
    });
  }
  reenableExtension(input: {
    hook_id: string; stage: string; source_hash: string; expected_revision: number; reason: string;
  }): Promise<JsonObject> {
    return this.request("/api/v1/extensions/reenable", {
      method: "POST", body: JSON.stringify({ ...input, actor: this.clientId }),
    });
  }
  codeSessions(projectId?: string, status?: string): Promise<CodeSessionSummary[]> {
    const query = new URLSearchParams();
    if (projectId) query.set("project_id", projectId);
    if (status) query.set("status", status);
    return this.request(`/api/v1/code/sessions${query.size ? `?${query}` : ""}`);
  }
  codeSession(sessionId: string): Promise<CodeSessionSummary> {
    return this.request(`/api/v1/code/sessions/${encodeURIComponent(sessionId)}`);
  }
  codeEvents(sessionId: string, afterSequence = 0): Promise<CodeSessionEvent[]> {
    return this.request(`/api/v1/code/sessions/${encodeURIComponent(sessionId)}/events?after_sequence=${afterSequence}`);
  }
  startCodeSession(projectId: string, originSessionId?: string): Promise<CodeSessionSummary> {
    return this.request("/api/v1/code/sessions", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        client_id: this.clientId,
        origin_session_id: originSessionId || null,
      }),
    });
  }
  runCodeTurn(
    sessionId: string, task: string, modelProfileId: string, reasoningEffort: ReasoningEffort,
  ): Promise<JsonObject> {
    return this.request(`/api/v1/code/sessions/${encodeURIComponent(sessionId)}/turns`, {
      method: "POST",
      body: JSON.stringify({
        client_id: this.clientId,
        task,
        model_profile_id: modelProfileId,
        reasoning_effort: reasoningEffort,
      }),
    });
  }
  finalizeCodeSession(sessionId: string, approvedPlanHash?: string): Promise<JsonObject> {
    const query = new URLSearchParams({ client_id: this.clientId });
    if (approvedPlanHash) query.set("approved_plan_hash", approvedPlanHash);
    return this.request(`/api/v1/code/sessions/${encodeURIComponent(sessionId)}/finalize?${query}`, { method: "POST" });
  }
  abortCodeSession(sessionId: string): Promise<JsonObject> {
    return this.request(`/api/v1/code/sessions/${encodeURIComponent(sessionId)}/abort?client_id=${encodeURIComponent(this.clientId)}`, { method: "POST" });
  }
  deleteCodeSession(sessionId: string): Promise<JsonObject> {
    return this.request(`/api/v1/code/sessions/${encodeURIComponent(sessionId)}?client_id=${encodeURIComponent(this.clientId)}`, { method: "DELETE" });
  }
  papers(): Promise<PaperListItem[]> { return this.request("/api/v1/library/papers?limit=200"); }
  paper(paperId: string): Promise<PaperListItem & JsonObject> {
    return this.request(`/api/v1/library/papers/${encodeURIComponent(paperId)}`);
  }
  async paperPdfUrl(paperId: string): Promise<string> {
    const url = `${this.connection.baseUrl}/api/v1/library/papers/${encodeURIComponent(paperId)}/pdf`;
    if (!this.connection.token) return url;
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${this.connection.token}` },
      credentials: "include",
    });
    if (!response.ok) throw new GatewayRequestError("PDF could not be loaded", response.status);
    return URL.createObjectURL(await response.blob());
  }
  paperNotes(paperId: string): Promise<PaperNote[]> {
    return this.request(`/api/v1/library/papers/${encodeURIComponent(paperId)}/notes`);
  }
  deletePaperNote(paperId: string, noteId: string): Promise<{ deleted: boolean }> {
    return this.request(`/api/v1/library/papers/${encodeURIComponent(paperId)}/notes/${encodeURIComponent(noteId)}`, {
      method: "DELETE",
    });
  }
  createPaperNote(paperId: string, value: { page: number | null; selected_text: string; locator: JsonObject; note_markdown: string }): Promise<PaperNote> {
    return this.request(`/api/v1/library/papers/${encodeURIComponent(paperId)}/notes`, {
      method: "POST", body: JSON.stringify(value),
    });
  }
  updatePaperNote(paperId: string, note: PaperNote): Promise<PaperNote> {
    return this.request(`/api/v1/library/papers/${encodeURIComponent(paperId)}/notes/${encodeURIComponent(note.note_id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: note.revision,
        page: note.page,
        selected_text: note.selected_text,
        locator: note.locator,
        note_markdown: note.note_markdown,
      }),
    });
  }
  searchLibrary(query: string): Promise<JsonObject> {
    return this.request("/api/v1/library/search", {
      method: "POST",
      body: JSON.stringify({ query, mode: "rrf", entity_types: [], top_k: 50 }),
    });
  }
  workspaceTree(projectId: string, path = "YYWorkspace:\\", cursor?: string): Promise<WorkspaceTree> {
    const query = new URLSearchParams({ path, limit: "300" });
    if (cursor) query.set("cursor", cursor);
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/workspace/tree?${query}`);
  }
  workspaceFile(projectId: string, path: string): Promise<WorkspaceDocument> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/workspace/files?${new URLSearchParams({ path })}`);
  }
  saveWorkspaceFile(projectId: string, path: string, content: string, expectedEtag: string | null): Promise<WorkspaceEntry & { etag: string }> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/workspace/files`, {
      method: "PUT", body: JSON.stringify({ path, content, expected_etag: expectedEtag }),
    });
  }
  createWorkspaceEntry(projectId: string, path: string, kind: "file" | "directory"): Promise<WorkspaceEntry> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/workspace/entries`, {
      method: "POST", body: JSON.stringify({ path, kind }),
    });
  }
  moveWorkspaceEntry(projectId: string, source: string, destination: string): Promise<JsonObject> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/workspace/move`, {
      method: "POST", body: JSON.stringify({ source, destination }),
    });
  }
  deleteWorkspaceEntry(projectId: string, path: string): Promise<JsonObject> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/workspace/entries?${new URLSearchParams({ path })}`, { method: "DELETE" });
  }
  workspaceChanges(projectId: string): Promise<{ available: boolean; changes: Array<{ status: string; path: string }> }> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/workspace/changes`);
  }
  startLatexCompilation(projectId: string, mainPath: string, expectedTreeRevision: number): Promise<LatexCompilation> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/latex/compilations`, {
      method: "POST",
      headers: { "X-Client-Id": this.clientId },
      body: JSON.stringify({
        main_path: mainPath,
        engine: "auto",
        expected_tree_revision: expectedTreeRevision,
      }),
    });
  }
  latexCompilation(projectId: string, compilationId: string): Promise<LatexCompilation> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/latex/compilations/${encodeURIComponent(compilationId)}`);
  }
  cancelLatexCompilation(projectId: string, compilationId: string): Promise<LatexCompilation> {
    return this.request(`/api/v1/projects/${encodeURIComponent(projectId)}/latex/compilations/${encodeURIComponent(compilationId)}/cancel`, { method: "POST" });
  }
  async latexPdfUrl(projectId: string, compilationId: string): Promise<string> {
    const url = `${this.connection.baseUrl}/api/v1/projects/${encodeURIComponent(projectId)}/latex/compilations/${encodeURIComponent(compilationId)}/pdf`;
    if (!this.connection.token) return url;
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${this.connection.token}` }, credentials: "include",
    });
    if (!response.ok) throw new GatewayRequestError("Compiled PDF could not be loaded", response.status);
    return URL.createObjectURL(await response.blob());
  }
  async latexLog(projectId: string, compilationId: string): Promise<string> {
    const url = `${this.connection.baseUrl}/api/v1/projects/${encodeURIComponent(projectId)}/latex/compilations/${encodeURIComponent(compilationId)}/log`;
    const headers = new Headers();
    if (this.connection.token) headers.set("Authorization", `Bearer ${this.connection.token}`);
    const response = await fetch(url, { headers, credentials: "include" });
    if (!response.ok) throw new GatewayRequestError("LaTeX log could not be loaded", response.status);
    return response.text();
  }
  workspaceEvents(projectId: string, afterSequence = 0): Promise<GatewayEvent[]> {
    return this.request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/workspace/events?after_sequence=${afterSequence}`,
    );
  }
  approvals(projectId?: string): Promise<PendingApproval[]> {
    const query = new URLSearchParams({ state: "pending" });
    if (projectId) query.set("project_id", projectId);
    return this.request(`/api/v1/approvals?${query}`);
  }
  async acknowledgeRunResult(runId: string): Promise<void> {
    for (const item of await this.inbox()) {
      if (!item.read && item.run_id === runId) await this.markRead(item.item_id);
    }
  }
  async acknowledgeSessionHistory(projectId: string, sessionId: string, records: SessionRecord[]): Promise<void> {
    const displayed = new Set(records.filter((record) =>
      record.run_id && record.record_id && record.role === "assistant"
      && !record.tool_calls?.length && record.content?.trim()
      && !["cron", "maintenance", "extension"].includes(record.origin || "")
    ).map((record) => record.run_id as string));
    if (!displayed.size) return;
    for (const item of await this.inbox()) {
      if (!item.read && item.project_id === projectId && item.session_id === sessionId
          && displayed.has(item.run_id)) await this.markRead(item.item_id);
    }
  }
  registerProject(path: string, name?: string): Promise<Project> {
    return this.request("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify({ path, name: name || null }),
    });
  }
  startRun(input: RunCreate): Promise<{ run_id: string; session_id?: string | null }> {
    const idempotencyKey = crypto.randomUUID().replaceAll("-", "");
    return this.request("/api/v1/runs", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        project_id: input.projectId,
        client_id: this.clientId,
        task: input.task,
        session_id: input.sessionId || null,
        idempotency_key: idempotencyKey,
        model_profile_id: input.modelProfileId,
        reasoning_effort: input.reasoningEffort,
        ui_context: input.uiContext || { source: "agent" },
      }),
    });
  }
  run(runId: string): Promise<Record<string, unknown>> {
    return this.request(`/api/v1/runs/${encodeURIComponent(runId)}`);
  }
  runOperations(runId: string): Promise<Array<Record<string, unknown>>> {
    return this.request(`/api/v1/runs/${encodeURIComponent(runId)}/operations`);
  }
  cancelRun(runId: string): Promise<{ cancelled: boolean }> {
    return this.request(`/api/v1/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  }
  respondApproval(approvalId: string, approved: boolean): Promise<{ approved: boolean }> {
    return this.request(`/api/v1/approvals/${encodeURIComponent(approvalId)}`, {
      method: "POST",
      body: JSON.stringify({ client_id: this.clientId, approved }),
    });
  }
  observerStatus(runId: string): Promise<ObserverStatus> {
    return this.request(`/api/v1/observer/runs/${encodeURIComponent(runId)}`);
  }
  decideObserverCorrection(
    proposalId: string, expectedRevision: number, action: "adopt" | "edit" | "reject",
    editedPrompt?: string,
  ): Promise<Record<string, unknown>> {
    return this.request(`/api/v1/observer/corrections/${encodeURIComponent(proposalId)}/decision`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: expectedRevision,
        action,
        actor: this.clientId,
        edited_prompt: editedPrompt || null,
        reason: action === "reject" ? "user_rejected" : "user_approved",
      }),
    });
  }

  subscribe(runId: string, afterSequence: number, onEvent: (event: GatewayEvent) => void): WebSocket {
    const base = this.connection.baseUrl.replace(/^http/, "ws");
    const query = new URLSearchParams({
      client_id: this.clientId,
      run_id: runId,
      after_sequence: String(afterSequence),
    });
    if (this.connection.token) query.set("token", this.connection.token);
    const socket = new WebSocket(`${base}/api/v1/events?${query}`);
    socket.onmessage = (message) => onEvent(JSON.parse(message.data) as GatewayEvent);
    return socket;
  }

  subscribeStreams(
    streams: Record<string, number>,
    onEvent: (event: GatewayEvent) => void,
  ): WebSocket {
    const base = this.connection.baseUrl.replace(/^http/, "ws");
    const query = new URLSearchParams({
      client_id: this.clientId,
      stream_ids: Object.keys(streams).join(","),
      after_sequences: JSON.stringify(streams),
    });
    if (this.connection.token) query.set("token", this.connection.token);
    const socket = new WebSocket(`${base}/api/v1/events?${query}`);
    socket.onmessage = (message) => onEvent(JSON.parse(message.data) as GatewayEvent);
    return socket;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body) headers.set("Content-Type", "application/json");
    if (this.connection.token) headers.set("Authorization", `Bearer ${this.connection.token}`);
    if (this.connection.csrf) headers.set("X-CSRF-Token", this.connection.csrf);
    const response = await fetch(`${this.connection.baseUrl}${path}`, {
      ...init,
      headers,
      credentials: "include",
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({ detail: response.statusText })) as GatewayErrorBody;
      throw new GatewayRequestError(
        body.error?.message || body.detail || `Gateway HTTP ${response.status}`,
        response.status,
        body.error?.code,
        body.error?.recoverable,
        body.correlation_id,
        body.error?.details,
      );
    }
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }
}
