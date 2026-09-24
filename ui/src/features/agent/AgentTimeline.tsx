import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, CircleCheck, LoaderCircle, Wrench } from "lucide-react";
import { eventText, terminalEventTypes } from "../../events";
import type { GatewayEvent, SessionRecord } from "../../types";
import { Markdown } from "../../shared/ui/Markdown";
import {
  formatCacheRatio,
  formatTokenCount,
  modelCallsFromEvents,
  modelCallsFromRecords,
  summarizeModelUsage,
  type ModelUsageSummary,
} from "./agentUsage";

export function AgentTimeline({
  history, events, optimisticQuestion, running, onLoadToolResult,
}: {
  history: SessionRecord[];
  events: GatewayEvent[];
  optimisticQuestion: string;
  running: boolean;
  onLoadToolResult: (recordId: string) => Promise<string>;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [expandedProcess, setExpandedProcess] = useState(false);

  useEffect(() => {
    if (!follow.current) return;
    viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: "auto" });
  }, [history, events, optimisticQuestion]);

  const run = useMemo(() => projectRun(events), [events]);
  const historyGroups = useMemo(() => {
    const groups = groupHistory(history);
    const activeRunId = run.terminal?.run_id;
    const activeAnswer = normalizeAnswer(run.answer || run.text);
    const finishedAt = run.terminal ? new Date(run.terminal.timestamp).getTime() : NaN;
    if (!activeRunId && !run.terminal) return groups;
    return groups.filter((group) => {
      if (activeRunId && group.records.some((record) => record.run_id === activeRunId)) return false;
      // Some legacy JSONL records predate run_id. If the just-finished answer
      // is present there as well, suppress that exact near-in-time copy so the
      // live run remains the single canonical presentation.
      if (!activeAnswer || !Number.isFinite(finishedAt)) return true;
      return !group.records.some((record) => {
        if (record.role !== "assistant" || normalizeAnswer(record.content) !== activeAnswer) return false;
        const timestamp = record.timestamp ? new Date(record.timestamp).getTime() : NaN;
        return Number.isFinite(timestamp) && Math.abs(timestamp - finishedAt) < 120_000;
      });
    });
  }, [history, run.answer, run.terminal, run.text]);
  const empty = !history.length && !events.length && !optimisticQuestion && !running;

  return (
    <div
      ref={viewport}
      className="timeline"
      onScroll={(event) => {
        const target = event.currentTarget;
        follow.current = target.scrollHeight - target.scrollTop - target.clientHeight < 56;
      }}
    >
      <div className="timeline-inner">
        {empty && (
          <section className="agent-empty">
            <p className="eyebrow">Local research runtime</p>
            <h1>从问题开始，保留完整证据链。</h1>
            <p>询问研究问题、阅读项目内容，或让 Agent 在当前工作区中完成任务。工具执行和审批始终可追溯。</p>
          </section>
        )}

        {historyGroups.map((group, index) => group.runId || group.isTurn ? (
          <HistoryTurn records={group.records} key={`${group.runId || "history"}-${index}`} />
        ) : group.records.map((record, recordIndex) => (
          <HistoryRecord record={record} key={record.record_id || `${record.timestamp}-${index}-${recordIndex}`} onLoadToolResult={onLoadToolResult} />
        )))}

        {optimisticQuestion && <article className="message user-message"><div className="message-role">你</div><p>{optimisticQuestion}</p></article>}

        {(!!events.length || running) && (
          <section className="current-run" aria-live="polite">
            <div className="run-heading">
              <span className={running ? "run-spinner" : "run-finished"} aria-hidden="true">{running ? <LoaderCircle /> : <CircleCheck />}</span>
              <span>{running ? run.status : "本轮已完成"}</span>
            </div>

            <button className="process-toggle" type="button" aria-expanded={expandedProcess} onClick={() => setExpandedProcess((value) => !value)}>
              {expandedProcess ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
              用时 {formatDuration(run.durationMs)} · {expandedProcess ? "收起过程" : "查看过程"}
            </button>
            {expandedProcess && <RunProcess events={events} running={running} />}

            {!run.terminal && run.text && !events.some((event) => toolEventTypes.has(event.type)) && (
              <Markdown className="markdown assistant-live">{run.text}</Markdown>
            )}
            {run.terminal && <Markdown className="markdown final-answer">{run.answer || run.text || "任务已结束，但模型没有返回文字。"}</Markdown>}
          </section>
        )}
      </div>
    </div>
  );
}

function HistoryTurn({ records }: { records: SessionRecord[] }) {
  const [expanded, setExpanded] = useState(false);
  const question = records.find((record) => record.role === "user")?.content;
  const answer = [...records].reverse().find((record) => (
    record.role === "assistant"
    && !record.tool_calls?.length
    && typeof record.content === "string"
    && record.content.trim().length > 0
  ));
  const duration = [...records].reverse().find((record) => typeof record.task_latency_ms === "number")?.task_latency_ms
    ?? durationFromRecords(records);
  return (
    <section className="history-turn">
      {question && <article className="message user-message"><div className="message-role">你</div><p>{question}</p></article>}
      <button className="process-toggle" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
        {expanded ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
        用时 {formatDuration(duration)} · {expanded ? "收起过程" : "查看过程"}
      </button>
      {expanded && <HistoryProcess records={records} />}
      {answer && <article className="message assistant-message"><div className="message-role">YYAgent</div><Markdown>{answer.content || (answer.tool_calls?.length ? "模型请求了工具调用。" : "")}</Markdown>{answer.timestamp && <time>{formatTimestamp(answer.timestamp)}</time>}</article>}
    </section>
  );
}

function HistoryProcess({ records }: { records: SessionRecord[] }) {
  const usage = summarizeModelUsage(modelCallsFromRecords(records));
  return (
    <div className="process-panel">
      <ProcessSequence items={processItemsFromRecords(records)} running={false} />
      <ModelUsagePanel usage={usage} />
    </div>
  );
}

function HistoryRecord({ record, onLoadToolResult }: { record: SessionRecord; onLoadToolResult: (recordId: string) => Promise<string> }) {
  const [toolOutput, setToolOutput] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  if (record.role === "summary") {
    return <details className="history-summary"><summary>上下文摘要</summary><Markdown>{record.content || ""}</Markdown></details>;
  }
  if (record.role === "tool") {
    return (
      <details className="history-tool">
        <summary><Wrench aria-hidden="true" />工具 {record.name || "unknown"} · {record.status || "已记录"}</summary>
        {record.content && <pre>{record.content}</pre>}
        {record.record_id && !record.content && (
          <button type="button" disabled={loading} onClick={async () => {
            setLoading(true);
            try { setToolOutput(await onLoadToolResult(record.record_id as string)); }
            finally { setLoading(false); }
          }}>{loading ? "读取中…" : "读取完整结果"}</button>
        )}
        {toolOutput && <pre>{toolOutput}</pre>}
      </details>
    );
  }
  return (
    <article className={`message ${record.role === "user" ? "user-message" : "assistant-message"}`}>
      <div className="message-role">{record.role === "user" ? "你" : "YYAgent"}</div>
      {record.reasoning && <details className="reasoning"><summary>思考过程</summary><pre>{record.reasoning}</pre></details>}
      <Markdown>{record.content || (record.tool_calls?.length ? "模型请求了工具调用。" : "")}</Markdown>
      {record.timestamp && <time>{formatTimestamp(record.timestamp)}</time>}
    </article>
  );
}

function RunProcess({ events, running }: { events: GatewayEvent[]; running: boolean }) {
  const usage = summarizeModelUsage(modelCallsFromEvents(events));
  return (
    <div className="process-panel">
      <ProcessSequence items={processItemsFromEvents(events)} running={running} />
      <ModelUsagePanel usage={usage} />
    </div>
  );
}

type ProcessItem = {
  id: string;
  kind: "reasoning" | "tool" | "runtime" | "approval";
  label: string;
  content: string;
  payload?: Record<string, unknown>;
};

const toolEventTypes = new Set(["tool_requested", "tool_completed", "tool_batch_started", "tool_batch_completed"]);
const runtimeEventTypes = new Set([
  "compression_started", "context_compressed", "compression_fallback",
  "model_retry", "model_reconnected", "sandbox_fallback",
]);

function ProcessSequence({ items, running }: { items: ProcessItem[]; running: boolean }) {
  return (
    <section className="process-sequence-section">
      <h3>执行过程</h3>
      {!items.length && <p>{running ? "正在等待模型事件…" : "当前模型未提供可展示的思维链或工具记录。"}</p>}
      {!!items.length && <ol className="process-sequence">
        {items.map((item, index) => (
          <li key={item.id} className={`process-step process-step-${item.kind}`}>
            <div className="process-step-heading"><span className="process-step-index">{index + 1}</span><span>{item.label}</span></div>
            {item.kind === "tool" ? (
              <details className="tool-event">
                <summary>查看详细结果</summary>
                <pre>{item.content || "工具没有返回文本结果。"}</pre>
              </details>
            ) : item.content ? <pre>{item.content}</pre> : null}
          </li>
        ))}
      </ol>}
    </section>
  );
}

function processItemsFromEvents(events: GatewayEvent[]): ProcessItem[] {
  const items: ProcessItem[] = [];
  for (const [index, event] of events.entries()) {
    const content = eventText(event).trim();
    if (event.type === "text") {
      // Text emitted before a later tool call is intermediate model output.
      // The final text is rendered once below the process panel instead.
      if (content && events.slice(index + 1).some((item) => toolEventTypes.has(item.type))) {
        appendReasoningItem(items, event.event_id, eventText(event));
      }
      continue;
    }
    if (event.type === "reasoning") {
      const previous = items[items.length - 1];
      if (previous?.kind === "reasoning") previous.content += eventText(event);
      else items.push({ id: event.event_id, kind: "reasoning", label: "思考链", content: eventText(event) });
      continue;
    }
    if (toolEventTypes.has(event.type)) {
      items.push({
        id: event.event_id,
        kind: "tool",
        label: toolLabel(event),
        content: JSON.stringify(event.payload, null, 2),
        payload: event.payload,
      });
      continue;
    }
    if (event.type === "approval_requested") {
      items.push({ id: event.event_id, kind: "approval", label: "等待审批", content });
      continue;
    }
    if (runtimeEventTypes.has(event.type) && content) {
      items.push({ id: event.event_id, kind: "runtime", label: runtimeLabel(event.type), content });
    }
  }
  return items;
}

function processItemsFromRecords(records: SessionRecord[]): ProcessItem[] {
  const items: ProcessItem[] = [];
  let batchOpen = false;
  for (const record of records) {
    if (record.role === "assistant" && !record.tool_calls?.length && batchOpen) {
      items.push({ id: `${record.record_id || record.timestamp || items.length}-batch-complete`, kind: "runtime", label: "工具批次 · 完成", content: "" });
      batchOpen = false;
    }
    if (record.reasoning) {
      appendReasoningItem(items, `${record.record_id || record.timestamp || "reasoning"}-reasoning`, record.reasoning);
    }
    if (record.role === "assistant" && record.tool_calls?.length) {
      if (batchOpen) {
        items.push({ id: `${record.record_id || record.timestamp || items.length}-batch-complete`, kind: "runtime", label: "工具批次 · 完成", content: "" });
      }
      batchOpen = true;
      items.push({ id: `${record.record_id || record.timestamp || items.length}-batch-start`, kind: "runtime", label: "工具批次 · 开始", content: "" });
      if (record.content) {
        // Model content attached to a tool-call Turn is intermediate model
        // output, not the final answer. Keep it in the process timeline.
        appendReasoningItem(items, `${record.record_id || record.timestamp || items.length}-content`, record.content);
      }
      for (const [index, call] of record.tool_calls.entries()) {
        const functionValue = typeof call === "object" && call ? (call as Record<string, unknown>).function : null;
        const functionRecord = typeof functionValue === "object" && functionValue ? functionValue as Record<string, unknown> : {};
        const name = String(functionRecord.name || (call as Record<string, unknown>).name || "工具");
        items.push({
          id: `${record.record_id || record.timestamp || items.length}-request-${index}`,
          kind: "tool",
          label: `${name} · 已请求`,
          content: JSON.stringify(call, null, 2),
        });
      }
    } else if (record.role === "tool") {
      items.push({
        id: record.record_id || `${record.name || "tool"}-${record.timestamp || items.length}`,
        kind: "tool",
        label: `${record.name || "工具"} · ${record.status || "已记录"}`,
        content: record.content || (record.arguments ? JSON.stringify(record.arguments, null, 2) : "工具没有返回文本结果。"),
        payload: record.arguments,
      });
    }
  }
  if (batchOpen) items.push({ id: `batch-complete-${items.length}`, kind: "runtime", label: "工具批次 · 完成", content: "" });
  return items;
}

function appendReasoningItem(items: ProcessItem[], id: string, content: string): void {
  const previous = items[items.length - 1];
  if (previous?.kind === "reasoning") previous.content += `\n${content}`;
  else items.push({ id, kind: "reasoning", label: "思考链", content });
}

function ModelUsagePanel({ usage }: { usage: ModelUsageSummary }) {
  return (
    <section className="model-usage-panel">
      <h3>模型用量</h3>
      {!usage.calls ? <p>本轮未返回模型用量。</p> : <dl>
        <div><dt>模型调用</dt><dd>{usage.calls} 次</dd></div>
        <div><dt>输入 Tokens</dt><dd>{formatTokenCount(usage.inputTokens)}</dd></div>
        <div><dt>输出 Tokens</dt><dd>{formatTokenCount(usage.outputTokens)}</dd></div>
        <div><dt>总 Tokens</dt><dd>{formatTokenCount(usage.totalTokens)}</dd></div>
        <div><dt>前缀缓存命中</dt><dd>{formatTokenCount(usage.cachedTokens)}</dd></div>
        <div><dt>前缀缓存未命中</dt><dd>{formatTokenCount(usage.cacheMissTokens)}</dd></div>
        <div><dt>前缀缓存命中率</dt><dd>{formatCacheRatio(usage.cacheHitRatio)}</dd></div>
      </dl>}
    </section>
  );
}

function groupHistory(history: SessionRecord[]): Array<{ runId: string | null; turnId: string | null; records: SessionRecord[]; isTurn: boolean }> {
  const groups: Array<{ runId: string | null; turnId: string | null; records: SessionRecord[]; isTurn: boolean }> = [];
  for (const record of history) {
    const runId = record.run_id || null;
    const turnId = record.turn_id || null;
    const last = groups[groups.length - 1];
    // A few older records have no run_id but do retain the durable turn_id.
    // Match either identity so one multi-tool Turn remains one history row
    // after refresh instead of being split into isolated tool records.
    if (last && ((runId && last.runId === runId) || (turnId && last.turnId === turnId))) {
      last.records.push(record);
    } else if (!runId && last?.runId === null && last.isTurn && record.role !== "user") {
      // Older session records may not carry run_id. Keep their user message,
      // tool records and final answer together so refresh still has one
      // collapsible process view.
      last.records.push(record);
    } else {
      groups.push({ runId, turnId, records: [record], isTurn: Boolean(runId || turnId || record.role === "user") });
    }
  }
  return groups;
}

function durationFromRecords(records: SessionRecord[]): number {
  const timestamps = records.map((record) => record.timestamp ? new Date(record.timestamp).getTime() : NaN).filter(Number.isFinite);
  return timestamps.length > 1 ? Math.max(0, timestamps[timestamps.length - 1] - timestamps[0]) : 0;
}

function projectRun(events: GatewayEvent[]) {
  const text = events.filter((event) => event.type === "text").map(eventText).join("");
  const reasoning = events.filter((event) => event.type === "reasoning").map(eventText).join("");
  const terminal = [...events].reverse().find((event) => terminalEventTypes.has(event.type));
  const final = [...events].reverse().find((event) => event.type === "final");
  const fallback = terminal || events[events.length - 1];
  const answer = terminal?.type === "run_completed" ? eventText(terminal) : final ? eventText(final) : fallback ? eventText(fallback) : "";
  const start = events[0] ? new Date(events[0].timestamp).getTime() : Date.now();
  const end = terminal ? new Date(terminal.timestamp).getTime() : Date.now();
  const latest = events[events.length - 1];
  const status = latest?.type.startsWith("tool_") ? `正在执行工具：${String(latest.payload.name || latest.payload.tool_name || "")}` : "思考中";
  return { text, reasoning, terminal, answer, durationMs: Math.max(0, end - start), status };
}

function runtimeLabel(type: string): string {
  const labels: Record<string, string> = {
    compression_started: "开始压缩上下文",
    context_compressed: "上下文压缩完成",
    compression_fallback: "上下文压缩降级",
    model_retry: "模型请求重试",
    model_reconnected: "模型连接恢复",
    model_usage: "模型用量",
    sandbox_fallback: "Sandbox 降级",
  };
  return labels[type] || type;
}

function toolLabel(event: GatewayEvent): string {
  const name = String(event.payload.name || event.payload.tool_name || "工具");
  if (event.type === "tool_completed") return `${name} · 已完成`;
  if (event.type === "tool_requested") return `${name} · 已请求`;
  return event.type === "tool_batch_started" ? "工具批次 · 开始" : "工具批次 · 完成";
}

function formatDuration(value: number): string {
  const seconds = Math.max(0, Math.round(value / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = seconds % 60;
  return [hours ? `${hours}h` : "", minutes || hours ? `${minutes}m` : "", `${remaining}s`].filter(Boolean).join(" ");
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function normalizeAnswer(value: string | null | undefined): string {
  return String(value || "").replace(/\s+/g, " ").trim();
}
