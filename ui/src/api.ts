import type { GatewayEvent, GatewayStatus, InboxItem, Project, Session, SessionRecord } from "./types";

type Connection = { baseUrl: string; token?: string; csrf?: string };

export class GatewayApi {
  private connection: Connection = { baseUrl: location.origin };
  readonly clientId = `workbench_${crypto.randomUUID().replaceAll("-", "")}`;

  async initialize(): Promise<void> {
    if ("__TAURI_INTERNALS__" in window) {
      const { invoke } = await import("@tauri-apps/api/core");
      const value = await invoke<{ base_url: string; token: string }>("gateway_connection");
      this.connection = { baseUrl: value.base_url, token: value.token };
      return;
    }
    const code = new URLSearchParams(location.search).get("bootstrap");
    if (code) {
      const response = await fetch("/api/v1/browser/exchange", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code })
      });
      if (!response.ok) throw new Error("浏览器启动凭据无效或已过期");
      const value = await response.json();
      this.connection.csrf = value.csrf;
      history.replaceState({}, "", "/");
    } else {
      const response = await fetch("/api/v1/bootstrap", { credentials: "include" });
      if (!response.ok) throw new Error("请通过 `run.py serve-ui` 打开本机工作台");
      const value = await response.json();
      this.connection.csrf = value.csrf;
    }
  }

  status(): Promise<GatewayStatus> { return this.request("/api/v1/status"); }
  projects(): Promise<Project[]> { return this.request("/api/v1/projects"); }
  sessions(projectId: string): Promise<Session[]> {
    return this.request(`/api/v1/projects/${projectId}/sessions`);
  }
  session(projectId: string, sessionId: string): Promise<SessionRecord[]> {
    return this.request(`/api/v1/projects/${projectId}/sessions/${sessionId}`);
  }
  inbox(): Promise<InboxItem[]> { return this.request("/api/v1/inbox"); }
  markRead(itemId: string): Promise<InboxItem> {
    return this.request(`/api/v1/inbox/${itemId}/read`, { method: "POST" });
  }
  registerProject(path: string): Promise<Project> {
    return this.request("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify({ path })
    });
  }
  startRun(projectId: string, task: string, sessionId?: string): Promise<{ run_id: string }> {
    return this.request("/api/v1/runs", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        client_id: this.clientId,
        task,
        session_id: sessionId || null
      })
    });
  }
  cancelRun(runId: string): Promise<{ cancelled: boolean }> {
    return this.request(`/api/v1/runs/${runId}/cancel`, { method: "POST" });
  }
  respondApproval(approvalId: string, approved: boolean): Promise<{ approved: boolean }> {
    return this.request(`/api/v1/approvals/${approvalId}`, {
      method: "POST",
      body: JSON.stringify({ client_id: this.clientId, approved })
    });
  }

  subscribe(runId: string, afterSequence: number, onEvent: (event: GatewayEvent) => void): WebSocket {
    const base = this.connection.baseUrl.replace(/^http/, "ws");
    const query = new URLSearchParams({
      client_id: this.clientId,
      run_id: runId,
      after_sequence: String(afterSequence)
    });
    if (this.connection.token) query.set("token", this.connection.token);
    const socket = new WebSocket(`${base}/api/v1/events?${query}`);
    socket.onmessage = (message) => onEvent(JSON.parse(message.data) as GatewayEvent);
    return socket;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    if (this.connection.token) headers.set("Authorization", `Bearer ${this.connection.token}`);
    if (this.connection.csrf) headers.set("X-CSRF-Token", this.connection.csrf);
    const response = await fetch(`${this.connection.baseUrl}${path}`, {
      ...init,
      headers,
      credentials: "include"
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(body.detail || `Gateway HTTP ${response.status}`);
    }
    return response.json() as Promise<T>;
  }
}
