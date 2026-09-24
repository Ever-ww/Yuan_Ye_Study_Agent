import { describe, expect, it } from "vitest";
import { formatCacheRatio, formatTokenCount, latestModelCallsFromRecords, modelCallsFromRecords, summarizeModelUsage } from "./features/agent/agentUsage";

describe("agent model usage", () => {
  it("separates input/output totals and prefix-cache hit/miss tokens", () => {
    const usage = summarizeModelUsage([
      {
        input_tokens: { context_total: 1000, cached: 800, cache_miss: 200 },
        output_tokens: 120,
      },
      {
        input_tokens: { context_total: 500, cached: 300, cache_miss: 200 },
        output_tokens: 80,
      },
    ]);

    expect(usage.calls).toBe(2);
    expect(usage.inputTokens).toBe(1500);
    expect(usage.outputTokens).toBe(200);
    expect(usage.totalTokens).toBe(1700);
    expect(usage.cachedTokens).toBe(1100);
    expect(usage.cacheMissTokens).toBe(400);
    expect(usage.cacheHitRatio).toBeCloseTo(1100 / 1500);
    expect(formatTokenCount(1700)).toBe("1,700");
    expect(formatCacheRatio(usage.cacheHitRatio)).toBe("73.33%");
  });

  it("derives cache misses when the provider only reports total and hits", () => {
    const usage = summarizeModelUsage([
      { prefix_cache: { total_tokens: 100, hit_tokens: 90 }, output_tokens: 1 },
    ]);

    expect(usage.inputTokens).toBe(100);
    expect(usage.cachedTokens).toBe(90);
    expect(usage.cacheMissTokens).toBe(10);
    expect(usage.cacheHitRatio).toBe(0.9);
  });

  it("keeps unavailable provider metrics explicit", () => {
    const usage = summarizeModelUsage([{ output_tokens: 12 }]);

    expect(usage.calls).toBe(1);
    expect(usage.inputTokens).toBeNull();
    expect(usage.cachedTokens).toBeNull();
    expect(usage.cacheMissTokens).toBeNull();
    expect(formatTokenCount(null)).toBe("—");
    expect(formatCacheRatio(null)).toBe("—");
  });

  it("uses a persisted aggregate instead of counting intermediate calls twice", () => {
    const calls = [
      { model_call: { input_tokens: { context_total: 100 }, output_tokens: 10 } },
      { model_calls: [{ input_tokens: { context_total: 220 }, output_tokens: 20 }] },
    ];

    const metrics = modelCallsFromRecords(calls as never);
    expect(metrics).toHaveLength(1);
    expect(summarizeModelUsage(metrics).inputTokens).toBe(220);
    expect(summarizeModelUsage(metrics).outputTokens).toBe(20);
  });

  it("selects only the latest persisted turn for the composer badge", () => {
    const calls = latestModelCallsFromRecords([
      { role: "user", content: "first" },
      { role: "assistant", content: "first answer", model_calls: [{ output_tokens: 5 }] },
      { role: "user", content: "second" },
      { role: "assistant", content: "second answer", model_calls: [{ output_tokens: 8 }] },
    ] as never);

    expect(calls).toHaveLength(1);
    expect(calls[0].output_tokens).toBe(8);
  });
});
