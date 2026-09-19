import { CheckCircle2, CircleAlert, Eye, ListChecks } from "lucide-react";
import type { GatewayEvent, ObserverStatus } from "../../types";

export function ObserverInspector({ events, observer }: { events: GatewayEvent[]; observer: ObserverStatus | null }) {
  const state = observer?.state;
  const terminal = events.some((event) => ["run_completed", "run_failed", "run_cancelled", "run_interrupted"].includes(event.type));
  const progress = observer?.progress_markdown || latestProgress(events);
  const alignment = state?.intent_alignment.status;
  return (
    <div className="inspector-content">
      <header><div><span>Turn Observer</span><h2>{terminal ? "本轮状态" : "实时观察"}</h2></div><Eye aria-hidden="true" /></header>
      {terminal && alignment !== "drifted" ? (
        <section className="observer-complete"><CheckCircle2 aria-hidden="true" /><div><h3>任务已完成</h3><p>最终状态已核对。</p></div></section>
      ) : alignment === "drifted" ? (
        <section className="observer-warning"><CircleAlert aria-hidden="true" /><div><h3>检测到意图偏移</h3><p>{state?.intent_alignment.reason}</p></div></section>
      ) : (
        <section className="observer-current"><span className="observer-pulse" aria-hidden="true" /><div><h3>{state?.current_agent_action || "等待可见事件"}</h3><p>{state?.in_progress_task || "发送任务后开始本轮监控。"}</p></div></section>
      )}

      {state?.user_problem && <InspectorSection title="用户问题" items={[state.user_problem]} />}
      {!!state?.completed_tasks?.length && <InspectorSection title="已完成" items={state.completed_tasks} />}
      {!state && progress && <section className="observer-markdown"><ListChecks aria-hidden="true" /><pre>{progress}</pre></section>}
      {!events.length && !progress && <p className="quiet-empty">Observer 只处理本轮可见事件，不读取隐藏推理或系统提示。</p>}
    </div>
  );
}

function InspectorSection({ title, items }: { title: string; items: string[] }) {
  return <section className="inspector-section"><h3>{title}</h3><ul>{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></section>;
}

function latestProgress(events: GatewayEvent[]): string {
  const event = [...events].reverse().find((item) => item.type === "observer_progress");
  return event ? String(event.payload.progress_markdown || "") : "";
}
