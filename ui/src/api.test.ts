import { beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayApi } from "./api";

const values = new Map<string, string>();

beforeEach(() => {
  values.clear();
  vi.restoreAllMocks();
  vi.stubGlobal("location", {
    origin: "http://gateway.test",
    href: "http://gateway.test/agent",
  });
  vi.stubGlobal("window", {
    location: globalThis.location,
    localStorage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
  vi.stubGlobal("crypto", { randomUUID: () => "11111111-2222-3333-4444-555555555555" });
});

describe("GatewayApi", () => {
  it("persists a stable browser client id for approval recovery", () => {
    const first = new GatewayApi();
    const second = new GatewayApi();
    expect(first.clientId).toBe("workbench_11111111222233334444555555555555");
    expect(second.clientId).toBe(first.clientId);
  });

  it("freezes phase-one agent context into every run request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ run_id: "run-1" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new GatewayApi();

    await api.startRun({
      projectId: "project",
      sessionId: "session",
      task: "Explain the result",
      modelProfileId: "default",
      reasoningEffort: "high",
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body));
    expect(url).toBe("http://gateway.test/api/v1/runs");
    expect(body).toMatchObject({
      project_id: "project",
      session_id: "session",
      task: "Explain the result",
      model_profile_id: "default",
      reasoning_effort: "high",
      ui_context: { source: "agent" },
    });
    expect(body.idempotency_key).toBe("11111111222233334444555555555555");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe(body.idempotency_key);
  });

  it("normalizes structured Gateway errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: {
        code: "run_conflict",
        message: "Another run is active",
        recoverable: true,
        details: {},
      },
      correlation_id: "corr-1",
    }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    })));
    const api = new GatewayApi();

    await expect(api.status()).rejects.toMatchObject({
      name: "GatewayRequestError",
      status: 409,
      code: "run_conflict",
      recoverable: true,
      correlationId: "corr-1",
    });
  });

  it("deletes a Code session through the durable Gateway endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ deleted: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new GatewayApi();

    await api.deleteCodeSession("code-session");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/v1/code/sessions/code-session?client_id=");
    expect(init.method).toBe("DELETE");
  });
});
