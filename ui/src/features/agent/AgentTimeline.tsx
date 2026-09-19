import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, CircleCheck, LoaderCircle, Wrench } from "lucide-react";
import { eventText, terminalEventTypes } from "../../events";
import type { GatewayEvent, SessionRecord } from "../../types";
import { Markdown } from "../../shared/ui/Markdown";

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
  const empty = !history.length && !events.length && !optimisticQuestion;

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

        {history.map((record, index) => (
          <HistoryRecord record={record} key={record.record_id || `${record.timestamp}-${index}`} onLoadToolResult={onLoadToolResult} />
        ))}

        {optimisticQuestion && <article className="message user-message"><div className="message-role">你</div><p>{optimisticQuestion}</p></article>}

        {!!events.length && (
          <section className="current-run" aria-live="polite">
            <div className="run-heading">
              <span className={running ? "run-spinner" : "run-finished"} aria-hidden="true">{running ? <LoaderCircle /> : <CircleCheck />}</span>
              <span>{running ? run.status : "本轮已完成"}</span>
            </div>

            {!run.terminal && run.text && <Markdown className="markdown assistant-live">{run.text}</Markdown>}

            {run.terminal && (
              <>
                <button className="process-toggle" type="button" aria-expanded={expandedProcess} onClick={() => setExpandedProcess((value) => !value)}>
                  {expandedProcess ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
                  用时 {formatDuration(run.durationMs)} · {expandedProcess ? "收起过程" : "查看过程"}
                </button>
                {expandedProcess && <RunProcess events={events} reasoning={run.reasoning} />}
                <Markdown className="markdown final-answer">{run.answer || run.text || "任务已结束，但模型没有返回文字。"}</Markdown>
              </>
            )}
          </section>
        )}
      </div>
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

function RunProcess({ events, reasoning }: { events: GatewayEvent[]; reasoning: string }) {
  const toolEvents = events.filter((event) => ["tool_requested", "tool_completed", "tool_batch_started", "tool_batch_completed"].includes(event.type));
  const runtimeEvents = events.filter((event) => [
    "compression_started", "context_compressed", "compression_fallback",
    "model_retry", "model_reconnected", "model_usage", "sandbox_fallback",
  ].includes(event.type));
  return (
    <div className="process-panel">
      <section><h3>思考链</h3>{reasoning ? <pre>{reasoning}</pre> : <p>当前模型未提供可展示的思维链。</p>}</section>
      <section>
        <h3>本轮工具</h3>
        {!toolEvents.length && <p>本轮未调用工具。</p>}
        {toolEvents.map((event) => (
          <details key={event.event_id} className="tool-event">
            <summary>{toolLabel(event)}</summary>
            <pre>{JSON.stringify(event.payload, null, 2)}</pre>
          </details>
        ))}
      </section>
      {!!runtimeEvents.length && (
        <section>
          <h3>运行记录</h3>
          <ul className="runtime-event-list">
            {runtimeEvents.map((event) => <li key={event.event_id}><span>{runtimeLabel(event.type)}</span><small>{eventText(event)}</small></li>)}
          </ul>
        </section>
      )}
    </div>
  );
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
