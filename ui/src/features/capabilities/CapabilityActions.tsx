import { useEffect, useState } from "react";
import { RotateCcw, Save, ShieldCheck } from "lucide-react";
import type { JsonObject } from "../../types";

type Props = {
  view: "skills" | "plugins" | "extensions";
  projectId?: string;
  selected: JsonObject | null;
  busy: boolean;
  onSkill: (value: { project_id: string; action: "install" | "update"; source: string; ref?: string; skill_path?: string; name?: string; confirmed: boolean }) => Promise<void>;
  onRollback: (pluginId: string, generationId: string) => Promise<void>;
  onExtension: (action: "grant" | "revoke" | "reenable", value: JsonObject) => Promise<void>;
};

export function CapabilityActions(props: Props) {
  if (props.view === "skills") return <SkillForm {...props} />;
  if (props.view === "plugins") return <PluginForm {...props} />;
  return <ExtensionForm {...props} />;
}

function SkillForm(props: Props) {
  const [action, setAction] = useState<"install" | "update">("install");
  const [source, setSource] = useState("");
  const [ref, setRef] = useState("");
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  return <section className="capability-action"><header><div><span>Skill lifecycle</span><h2>安装或更新 Skill</h2></div><Save aria-hidden="true" /></header><form onSubmit={(event) => { event.preventDefault(); if (props.projectId && source.trim()) void props.onSkill({ project_id: props.projectId, action, source: source.trim(), ref: ref.trim() || undefined, skill_path: path.trim() || undefined, name: name.trim() || undefined, confirmed }); }}><div className="form-grid"><label><span>操作</span><select value={action} onChange={(event) => setAction(event.target.value as typeof action)}><option value="install">安装</option><option value="update">更新</option></select></label><label><span>名称（可选）</span><input value={name} onChange={(event) => setName(event.target.value)} /></label><label className="wide"><span>来源 URL 或受支持来源</span><input required value={source} onChange={(event) => setSource(event.target.value)} /></label><label><span>Git Ref（可选）</span><input value={ref} onChange={(event) => setRef(event.target.value)} /></label><label><span>来源内 Skill 路径（可选）</span><input value={path} onChange={(event) => setPath(event.target.value)} /></label></div><label className="check-field"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>确认执行已经过 Gateway 审核的安装计划</span></label><footer><button className="primary-button" disabled={props.busy || !props.projectId || !source.trim()} type="submit">{props.busy ? "提交中…" : `${action === "install" ? "安装" : "更新"} Skill`}</button></footer></form></section>;
}

function PluginForm(props: Props) {
  const pluginId = String(props.selected?.plugin_id || props.selected?.name || "");
  const generationId = String(props.selected?.generation_id || props.selected?.from_generation_id || "");
  return <section className="capability-action"><header><div><span>Immutable generation</span><h2>成员级回滚</h2></div><RotateCcw aria-hidden="true" /></header>{props.selected ? <><p>回滚只替换所选插件成员，并发布一个新的完整 Generation；历史版本不会被修改。</p><dl className="inline-metadata"><div><dt>Plugin</dt><dd>{pluginId || "当前记录没有插件标识"}</dd></div><div><dt>来源 Generation</dt><dd>{generationId || "当前记录没有 Generation 标识"}</dd></div></dl><button className="danger-outline" disabled={props.busy || !pluginId || !generationId} onClick={() => void props.onRollback(pluginId, generationId)}>生成回滚版本</button></> : <p className="quiet-empty">选择包含 plugin_id 与 generation_id 的隔离记录后才能回滚。</p>}</section>;
}

function ExtensionForm(props: Props) {
  const [action, setAction] = useState<"grant" | "revoke" | "reenable">("grant");
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const requestedCapabilities = stringList(props.selected?.requested_capabilities);
  const requestedTools = stringList(props.selected?.requested_tools);
  useEffect(() => {
    setCapabilities(stringList(props.selected?.requested_capabilities));
    setTools(stringList(props.selected?.requested_tools));
    setReason("");
  }, [props.selected]);
  const hookId = String(props.selected?.hook_id || "");
  const stage = String(props.selected?.stage || "");
  const sourceHash = String(props.selected?.source_hash || "");
  const manifestHash = String(props.selected?.manifest_hash || "");
  const grant = asObject(props.selected?.grant);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!props.selected || !reason.trim()) return;
    await props.onExtension(action, {
      hook_id: hookId, stage, source_hash: sourceHash, manifest_hash: manifestHash,
      capabilities, tools, tool_contract_hashes: asObject(props.selected.tool_contract_hashes),
      expected_revision: Number(grant.revision || props.selected.revision || 0), reason: reason.trim(),
    });
  }
  return <section className="capability-action"><header><div><span>Persistent grant</span><h2>Extension 授权</h2></div><ShieldCheck aria-hidden="true" /></header>{props.selected && hookId ? <form onSubmit={(event) => void submit(event)}><div className="form-grid"><label><span>操作</span><select value={action} onChange={(event) => setAction(event.target.value as typeof action)}><option value="grant">授予请求范围</option><option value="revoke">撤销全部授权</option><option value="reenable">重新启用已隔离 Hook</option></select></label><label><span>阶段</span><input readOnly value={stage} /></label></div>{action === "grant" && <><ChoiceGroup label="Capabilities" values={requestedCapabilities} selected={capabilities} onChange={setCapabilities} /><ChoiceGroup label="预批准 Tools" values={requestedTools} selected={tools} onChange={setTools} /></>}<label><span>理由</span><textarea required value={reason} onChange={(event) => setReason(event.target.value)} /></label><footer><button className="primary-button" disabled={props.busy || !reason.trim()} type="submit">{props.busy ? "提交中…" : "提交持久决策"}</button></footer></form> : <p className="quiet-empty">选择一个 Extension 查看 Manifest 申请并执行授权、撤销或重新启用。</p>}</section>;
}

function ChoiceGroup({ label, values, selected, onChange }: { label: string; values: string[]; selected: string[]; onChange: (value: string[]) => void }) {
  return <fieldset className="choice-group"><legend>{label}</legend>{values.length ? values.map((value) => <label key={value}><input type="checkbox" checked={selected.includes(value)} onChange={(event) => onChange(event.target.checked ? [...selected, value] : selected.filter((item) => item !== value))} /><span>{value}</span></label>) : <p>Manifest 未申请此类权限。</p>}</fieldset>;
}
function stringList(value: unknown): string[] { return Array.isArray(value) ? value.map(String) : []; }
function asObject(value: unknown): JsonObject { return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {}; }
