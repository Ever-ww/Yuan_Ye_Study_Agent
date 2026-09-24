import type { GatewayEvent, ModelCallMetric, SessionRecord } from "../../types";

export type ModelUsageSummary = {
  calls: number;
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  cachedTokens: number | null;
  cacheMissTokens: number | null;
  cacheHitRatio: number | null;
};

export function modelCallsFromEvents(events: GatewayEvent[]): ModelCallMetric[] {
  return events
    .filter((event) => event.type === "model_usage")
    .map((event) => event.payload.model_call)
    .filter(isModelCallMetric);
}

export function modelCallsFromRecords(records: SessionRecord[]): ModelCallMetric[] {
  // The final assistant record persists the complete model_calls list for the
  // turn, while intermediate tool-call records persist one model_call each.
  // Prefer the aggregate when it exists so refreshes do not double-count a run.
  const aggregates = records.flatMap((record) => Array.isArray(record.model_calls)
    ? record.model_calls.filter(isModelCallMetric)
    : []);
  if (aggregates.length) return aggregates;
  return records.flatMap((record) => record.model_call && isModelCallMetric(record.model_call)
    ? [record.model_call]
    : []);
}

export function latestModelCallsFromRecords(records: SessionRecord[]): ModelCallMetric[] {
  const lastUserIndex = records.reduce((latest, record, index) => record.role === "user" ? index : latest, -1);
  const latestTurn = records.slice(Math.max(0, lastUserIndex));
  for (let index = latestTurn.length - 1; index >= 0; index -= 1) {
    const record = latestTurn[index];
    if (Array.isArray(record.model_calls)) {
      const calls = record.model_calls.filter(isModelCallMetric);
      if (calls.length) return calls;
    }
  }
  const calls: ModelCallMetric[] = [];
  for (let index = latestTurn.length - 1; index >= 0; index -= 1) {
    const record = latestTurn[index];
    if (record.model_call && isModelCallMetric(record.model_call)) calls.unshift(record.model_call);
  }
  return calls;
}

export function summarizeModelUsage(calls: ModelCallMetric[]): ModelUsageSummary {
  const inputValues = calls.map((call) => numberValue(call.input_tokens?.context_total ?? call.prefix_cache?.total_tokens));
  const outputValues = calls.map((call) => numberValue(call.output_tokens));
  const cachedValues = calls.map((call) => numberValue(call.input_tokens?.cached ?? call.prefix_cache?.hit_tokens));
  const missValues = calls.map((call) => numberValue(
    call.input_tokens?.cache_miss ?? call.prefix_cache?.miss_tokens,
  ));
  const inputTokens = sumKnown(inputValues);
  const outputTokens = sumKnown(outputValues);
  const cachedTokens = sumKnown(cachedValues);
  const explicitMissTokens = sumKnown(missValues);
  const cacheMissTokens = explicitMissTokens ?? (
    inputTokens !== null && cachedTokens !== null ? Math.max(0, inputTokens - cachedTokens) : null
  );
  const totalTokens = inputTokens === null && outputTokens === null ? null : (inputTokens || 0) + (outputTokens || 0);
  const cacheDenominator = cachedTokens !== null && cacheMissTokens !== null
    ? cachedTokens + cacheMissTokens
    : null;
  const reportedRatio = calls.map((call) => numberValue(call.input_tokens?.cache_hit_ratio ?? call.prefix_cache?.hit_ratio)).find((value) => value !== null) ?? null;
  return {
    calls: calls.length,
    inputTokens,
    outputTokens,
    totalTokens,
    cachedTokens,
    cacheMissTokens,
    cacheHitRatio: cacheDenominator && cacheDenominator > 0
      ? cachedTokens! / cacheDenominator
      : reportedRatio,
  };
}

export function formatTokenCount(value: number | null): string {
  return value === null ? "—" : new Intl.NumberFormat("en-US").format(value);
}

export function formatCacheRatio(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(2)}%`;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function sumKnown(values: Array<number | null>): number | null {
  const known = values.filter((value): value is number => value !== null);
  return known.length ? known.reduce((total, value) => total + value, 0) : null;
}

function isModelCallMetric(value: unknown): value is ModelCallMetric {
  return Boolean(value && typeof value === "object");
}
