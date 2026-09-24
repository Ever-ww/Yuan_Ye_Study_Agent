import { describe, expect, it } from "vitest";
import { eventText, latestSequence, mergeGatewayEvents } from "./events";
import type { GatewayEvent } from "./types";

function event(eventId: string, sequence: number): GatewayEvent {
  return {
    version: 1,
    event_id: eventId,
    sequence,
    timestamp: "2026-07-29T12:00:00+08:00",
    project_id: "project",
    session_id: "session",
    run_id: "run",
    type: "text",
    payload: { content: eventId }
  };
}

describe("mergeGatewayEvents", () => {
  it("orders replayed events and removes duplicates", () => {
    const later = event("later", 2);
    const earlier = event("earlier", 1);
    const merged = mergeGatewayEvents(
      mergeGatewayEvents([later], earlier),
      later
    );
    expect(merged.map((item) => item.event_id)).toEqual(["earlier", "later"]);
  });

  it("extracts terminal payload text and the replay cursor", () => {
    const answer = { ...event("answer", 4), payload: { answer: "done" } };
    const failure = { ...event("failure", 7), payload: { message: "offline" } };
    expect(eventText(answer)).toBe("done");
    expect(eventText(failure)).toBe("offline");
    expect(latestSequence([answer, failure])).toBe(7);
  });
});
