import { useState } from "react";
import { History, Lock, Play, RotateCcw, Unlock } from "lucide-react";
import type { JsonObject } from "../../types";

type Props = {
  dream: JsonObject;
  harness: JsonObject;
  busy: boolean;
  onRun: (date?: string) => Promise<void>;
  onBackfill: (start: string, end: string) => Promise<void>;
  onRollback: (runId?: string) => Promise<void>;
  onHarnessRun: (selected?: string) => Promise<void>;
  onHarnessFreeze: (frozen: boolean, reason?: string) => Promise<void>;
};

export function DreamControls(props: Props) {
  const [date, setDate] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [runId, setRunId] = useState("");
  const [selected, setSelected] = useState("");
  const [reason, setReason] = useState("operator freeze from Web");
  const frozen = Boolean(props.harness.frozen);
  return (
    <div className="operation-control-grid">
      <section className="operation-control">
        <header><div><span>Memory consolidation</span><h2>Daily Dream</h2></div><Status value={props.dream.running ? "运行中" : String(props.dream.last_status || "待命")} /></header>
        <label><span>指定日期（留空则处理待处理日期）</span><input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label>
        <button className="primary-button" disabled={props.busy} onClick={() => void props.onRun(date || undefined)}><Play aria-hidden="true" />运行 Dream</button>
        <details className="advanced-form"><summary>回填与回滚</summary><div className="compact-form-row"><label><span>开始</span><input type="date" value={start} onChange={(event) => setStart(event.target.value)} /></label><label><span>结束</span><input type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></label><button disabled={props.busy || !start || !end} onClick={() => void props.onBackfill(start, end)}><History aria-hidden="true" />回填</button></div><div className="compact-form-row"><label><span>Run ID（留空回滚最近一次）</span><input value={runId} onChange={(event) => setRunId(event.target.value)} /></label><button className="danger-outline" disabled={props.busy} onClick={() => void props.onRollback(runId || undefined)}><RotateCcw aria-hidden="true" />回滚</button></div></details>
      </section>
      <section className="operation-control">
        <header><div><span>Self evolution</span><h2>Harness Dream</h2></div><Status value={frozen ? "已冻结" : String(props.harness.enabled === false ? "已关闭" : "待命")} /></header>
        <label><span>日期或 Operation（留空处理默认日期）</span><input value={selected} onChange={(event) => setSelected(event.target.value)} /></label>
        <button className="primary-button" disabled={props.busy || props.harness.enabled === false || frozen} onClick={() => void props.onHarnessRun(selected || undefined)}><Play aria-hidden="true" />运行 Harness Dream</button>
        <details className="advanced-form"><summary>冻结控制</summary><label><span>冻结原因</span><textarea value={reason} onChange={(event) => setReason(event.target.value)} /></label><button disabled={props.busy} onClick={() => void props.onHarnessFreeze(!frozen, reason)}>{frozen ? <Unlock aria-hidden="true" /> : <Lock aria-hidden="true" />}{frozen ? "解除冻结" : "冻结 Harness Dream"}</button></details>
      </section>
    </div>
  );
}

function Status({ value }: { value: string }) { return <span className="control-status">{value}</span>; }
