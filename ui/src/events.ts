import type { GatewayEvent } from "./types";

export const terminalEventTypes = new Set([
  "run_completed", "run_failed", "run_cancelled", "run_interrupted",
]);

/** Merge durable replay and live events without duplicating canonical events. */
export function mergeGatewayEvents(current: GatewayEvent[], incoming: GatewayEvent): GatewayEvent[] {
  if (current.some((item) => item.event_id === incoming.event_id)) return current;
  return [...current, incoming].sort((left, right) => left.sequence - right.sequence);
}

export function eventText(event: GatewayEvent): string {
  return String(event.payload.content ?? event.payload.answer ?? event.payload.message ?? "");
}

export function latestSequence(events: GatewayEvent[]): number {
  return events.reduce((latest, event) => Math.max(latest, event.sequence), 0);
}
