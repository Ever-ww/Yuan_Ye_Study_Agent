import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayApi } from "./api";
import type { InboxItem, SessionRecord } from "./types";

const item = (runId: string, overrides: Partial<InboxItem> = {}): InboxItem => ({
  item_id: `item-${runId}`, run_id: runId, project_id: "project", session_id: "session",
  title: "result", summary: "done", status: "completed", created_at: "2026-09-07", read: false,
  ...overrides,
});

beforeEach(() => {
  vi.stubGlobal("location", { origin: "http://localhost" });
  vi.stubGlobal("crypto", { randomUUID: () => "client-id" });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("explicit displayed-result receipts", () => {
  it("acknowledges only the displayed Run and is idempotent", async () => {
    const api = new GatewayApi();
    const result = item("shown");
    vi.spyOn(api, "inbox").mockImplementation(async () => [result, item("background")]);
    const mark = vi.spyOn(api, "markRead").mockImplementation(async () => {
      result.read = true;
      return result;
    });
    await api.acknowledgeRunResult("shown");
    await api.acknowledgeRunResult("shown");
    expect(mark).toHaveBeenCalledExactlyOnceWith("item-shown");
  });

  it("restored history needs terminal identity and exact project/session", async () => {
    const api = new GatewayApi();
    const records: SessionRecord[] = [
      { role: "assistant", content: "done", run_id: "shown", record_id: "r1" },
      { role: "assistant", content: "thinking", run_id: "partial", record_id: "r2", tool_calls: [{}] },
      { role: "summary", content: "summary", run_id: "summary", record_id: "r3" },
      { role: "assistant", content: "legacy", run_id: "legacy" },
      { role: "assistant", content: "background", run_id: "cron", record_id: "r4", origin: "cron" },
    ];
    vi.spyOn(api, "inbox").mockResolvedValue([
      ...["shown", "partial", "summary", "legacy", "cron", "unseen"].map((run) => item(run)),
      item("shown", { project_id: "other", item_id: "other-project" }),
      item("shown", { session_id: "other", item_id: "other-session" }),
    ]);
    const mark = vi.spyOn(api, "markRead").mockResolvedValue(item("shown", { read: true }));
    await api.acknowledgeSessionHistory("project", "session", records);
    expect(mark).toHaveBeenCalledExactlyOnceWith("item-shown");
  });

  it("does not hide receipt failures or clear unrelated notifications", async () => {
    const api = new GatewayApi();
    vi.spyOn(api, "inbox").mockResolvedValue([item("shown"), item("background")]);
    const mark = vi.spyOn(api, "markRead").mockRejectedValue(new Error("offline"));
    await expect(api.acknowledgeRunResult("shown")).rejects.toThrow("offline");
    expect(mark).toHaveBeenCalledExactlyOnceWith("item-shown");
  });
});
