import type { GatewayEvent } from "./types";

/** 合并实时与重放事件，并按 Run 序号稳定去重。 */
export function mergeGatewayEvents(
  current: GatewayEvent[],
  incoming: GatewayEvent
): GatewayEvent[] {
  if (current.some((item) => item.event_id === incoming.event_id)) return current;
  const next = [...current, incoming];
  next.sort((left, right) => left.sequence - right.sequence);
  return next;
}
