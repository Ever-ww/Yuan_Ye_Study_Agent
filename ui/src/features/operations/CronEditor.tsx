import { useEffect, useMemo, useState } from "react";
import { Save, X } from "lucide-react";
import type { CronJobInput, JsonObject, Project } from "../../types";

type Props = {
  job: JsonObject | null;
  projects: Project[];
  busy: boolean;
  onClose: () => void;
  onSave: (value: CronJobInput & { project_id: string }) => Promise<void>;
};

const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

export function CronEditor({ job, projects, busy, onClose, onSave }: Props) {
  const schedule = asObject(job?.schedule);
  const profile = asObject(job?.runtime_profile);
  const limits = asObject(profile.limits);
  const [projectId, setProjectId] = useState(String(job?.project_id || projects[0]?.project_id || ""));
  const [name, setName] = useState(String(job?.name || ""));
  const [prompt, setPrompt] = useState(String(job?.prompt || ""));
  const [kind, setKind] = useState<"interval" | "once" | "cron">((schedule.kind as "interval" | "once" | "cron") || "cron");
  const [scheduleValue, setScheduleValue] = useState(scheduleInput(schedule));
  const [zone, setZone] = useState(String(schedule.timezone || timezone));
  const [misfire, setMisfire] = useState<"skip" | "fire_once" | "catch_up">((job?.misfire_policy as "skip" | "fire_once" | "catch_up") || "fire_once");
  const [allowedTools, setAllowedTools] = useState(joinList(profile.allowed_tools));
  const [allowedSkills, setAllowedSkills] = useState(joinList(profile.allowed_skills));
  const [preapprovedTools, setPreapprovedTools] = useState(joinList(profile.preapproved_tools));
  const [sandboxPolicy, setSandboxPolicy] = useState<"read_only" | "checkpointed_workspace">((profile.sandbox_policy as "read_only" | "checkpointed_workspace") || "read_only");
  const [memoryAccess, setMemoryAccess] = useState<"none" | "project">((profile.memory_access as "none" | "project") || "none");
  const [memoryKinds, setMemoryKinds] = useState(joinList(profile.allowed_memory_kinds));
  const [parallel, setParallel] = useState(numberValue(profile.max_parallel_tool_calls, 4));
  const [timeout, setTimeoutValue] = useState(numberValue(limits.timeout_seconds, 1800));
  const [maxTurns, setMaxTurns] = useState(numberValue(limits.max_turns, 1));
  const [maxModelCalls, setMaxModelCalls] = useState(numberValue(limits.max_model_calls, 8));
  const [maxToolCalls, setMaxToolCalls] = useState(numberValue(limits.max_tool_calls, 24));
  const [tokenBudget, setTokenBudget] = useState(numberValue(limits.token_budget, 200000));
  const [error, setError] = useState("");

  useEffect(() => {
    if (!projectId && projects[0]) setProjectId(projects[0].project_id);
  }, [projectId, projects]);

  const preapprovalError = useMemo(() => {
    const allowed = new Set(splitList(allowedTools));
    return splitList(preapprovedTools).find((tool) => !allowed.has(tool));
  }, [allowedTools, preapprovedTools]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    if (!projectId || !name.trim() || !prompt.trim() || !scheduleValue.trim()) {
      setError("请填写项目、名称、任务内容和调度值。");
      return;
    }
    if (preapprovalError) {
      setError(`预批准工具 ${preapprovalError} 不在允许工具列表中。`);
      return;
    }
    const scheduleInput = kind === "interval"
      ? { kind, interval_seconds: Number(scheduleValue), run_at: null, expression: null, timezone: zone }
      : kind === "once"
        ? { kind, interval_seconds: null, run_at: new Date(scheduleValue).toISOString(), expression: null, timezone: zone }
        : { kind, interval_seconds: null, run_at: null, expression: scheduleValue.trim(), timezone: zone };
    await onSave({
      project_id: projectId, name: name.trim(), prompt: prompt.trim(), schedule: scheduleInput,
      misfire_policy: misfire,
      runtime_profile: {
        allowed_tools: splitList(allowedTools), allowed_skills: splitList(allowedSkills),
        preapproved_tools: splitList(preapprovedTools), sandbox_policy: sandboxPolicy,
        max_parallel_tool_calls: parallel, memory_access: memoryAccess,
        allowed_memory_kinds: splitList(memoryKinds),
        limits: {
          timeout_seconds: timeout, max_turns: maxTurns, max_model_calls: maxModelCalls,
          max_tool_calls: maxToolCalls, token_budget: tokenBudget,
        },
      },
    });
  }

  return (
    <section className="management-editor" aria-labelledby="cron-editor-title">
      <header>
        <div><span>定时任务</span><h2 id="cron-editor-title">{job ? "编辑任务" : "新建任务"}</h2></div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="关闭定时任务表单"><X aria-hidden="true" /></button>
      </header>
      <form onSubmit={(event) => void submit(event)}>
        {error && <div className="form-error-summary" role="alert" tabIndex={-1}>{error}</div>}
        <div className="form-grid">
          <label><span>项目</span><select value={projectId} onChange={(event) => setProjectId(event.target.value)} disabled={Boolean(job)}>{projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}</select></label>
          <label><span>名称</span><input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label className="wide"><span>任务内容</span><textarea required maxLength={20000} value={prompt} onChange={(event) => setPrompt(event.target.value)} /></label>
          <label><span>调度类型</span><select value={kind} onChange={(event) => { const next = event.target.value as typeof kind; setKind(next); setScheduleValue(next === "cron" ? "0 9 * * 1" : next === "interval" ? "3600" : ""); }}><option value="cron">Cron 表达式</option><option value="interval">固定间隔</option><option value="once">单次执行</option></select></label>
          <label><span>{kind === "cron" ? "Cron 表达式" : kind === "interval" ? "间隔秒数" : "执行时间"}</span><input required type={kind === "once" ? "datetime-local" : kind === "interval" ? "number" : "text"} min={kind === "interval" ? 60 : undefined} value={scheduleValue} onChange={(event) => setScheduleValue(event.target.value)} /></label>
          <label><span>时区</span><input required value={zone} onChange={(event) => setZone(event.target.value)} /></label>
          <label><span>错过执行</span><select value={misfire} onChange={(event) => setMisfire(event.target.value as typeof misfire)}><option value="fire_once">补跑一次</option><option value="skip">跳过</option><option value="catch_up">逐次补跑</option></select></label>
        </div>
        <details className="advanced-form">
          <summary>运行权限与资源限制</summary>
          <div className="form-grid">
            <label className="wide"><span>允许工具（逗号或换行分隔）</span><textarea value={allowedTools} onChange={(event) => setAllowedTools(event.target.value)} /></label>
            <label className="wide"><span>允许 Skills</span><textarea value={allowedSkills} onChange={(event) => setAllowedSkills(event.target.value)} /></label>
            <label className="wide"><span>预批准工具（必须同时出现在允许工具中）</span><textarea value={preapprovedTools} onChange={(event) => setPreapprovedTools(event.target.value)} /></label>
            <label><span>沙箱策略</span><select value={sandboxPolicy} onChange={(event) => setSandboxPolicy(event.target.value as typeof sandboxPolicy)}><option value="read_only">只读</option><option value="checkpointed_workspace">可写且带 Checkpoint</option></select></label>
            <label><span>记忆访问</span><select value={memoryAccess} onChange={(event) => setMemoryAccess(event.target.value as typeof memoryAccess)}><option value="none">不访问</option><option value="project">项目记忆</option></select></label>
            <label className="wide"><span>允许记忆类型</span><input value={memoryKinds} onChange={(event) => setMemoryKinds(event.target.value)} /></label>
            <NumberField label="并行工具数" value={parallel} min={1} max={16} onChange={setParallel} />
            <NumberField label="超时（秒）" value={timeout} min={30} max={86400} onChange={setTimeoutValue} />
            <NumberField label="最大 Turns" value={maxTurns} min={1} max={32} onChange={setMaxTurns} />
            <NumberField label="最大模型调用" value={maxModelCalls} min={1} max={200} onChange={setMaxModelCalls} />
            <NumberField label="最大工具调用" value={maxToolCalls} min={0} max={500} onChange={setMaxToolCalls} />
            <NumberField label="Token 预算" value={tokenBudget} min={1000} max={10000000} onChange={setTokenBudget} />
          </div>
        </details>
        <footer><button type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy} type="submit"><Save aria-hidden="true" />{busy ? "保存中…" : "保存任务"}</button></footer>
      </form>
    </section>
  );
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return <label><span>{label}</span><input type="number" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} /></label>;
}
function asObject(value: unknown): JsonObject { return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {}; }
function joinList(value: unknown): string { return Array.isArray(value) ? value.join(", ") : ""; }
function splitList(value: string): string[] { return [...new Set(value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))]; }
function numberValue(value: unknown, fallback: number): number { const selected = Number(value); return Number.isFinite(selected) ? selected : fallback; }
function scheduleInput(schedule: JsonObject): string {
  if (schedule.kind === "interval") return String(schedule.interval_seconds || 3600);
  if (schedule.kind === "once") {
    const date = new Date(String(schedule.run_at || ""));
    if (!Number.isNaN(date.getTime())) return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    return "";
  }
  return String(schedule.expression || "0 9 * * 1");
}
