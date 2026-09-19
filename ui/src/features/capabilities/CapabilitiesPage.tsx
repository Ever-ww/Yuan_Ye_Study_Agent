import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Blocks, PlugZap, RefreshCw, Shield, Sparkles } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WorkbenchShell } from "../../app/WorkbenchShell";
import { useTheme } from "../../app/ThemeProvider";
import { useGatewayApi } from "../../shared/api/context";
import { CommandPalette } from "../../shared/ui/CommandPalette";
import { StatusMark } from "../../shared/ui/StatusMark";
import type { JsonObject } from "../../types";
import { CapabilityActions } from "./CapabilityActions";

type View = "skills" | "plugins" | "extensions";

export function CapabilitiesPage() {
  const api = useGatewayApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setPreference } = useTheme();
  const [params, setParams] = useSearchParams();
  const view = (["skills", "plugins", "extensions"].includes(params.get("view") || "") ? params.get("view") : "skills") as View;
  const [commandsOpen, setCommandsOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [selected, setSelected] = useState<JsonObject | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingPlanHash, setPendingPlanHash] = useState("");
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const projectId = params.get("project") || projects.data?.[0]?.project_id;
  const skills = useQuery({ queryKey: ["capabilities", "skills", projectId], queryFn: () => api.skills(projectId!), enabled: view === "skills" && Boolean(projectId) });
  const plugins = useQuery({ queryKey: ["capabilities", "plugins"], queryFn: () => api.runtimePluginStatus(), enabled: view === "plugins" });
  const extensions = useQuery({ queryKey: ["capabilities", "extensions"], queryFn: () => api.extensionStatus(), enabled: view === "extensions" });
  const rows = useMemo(() => view === "skills" ? skills.data || [] : normalizeRows(view === "plugins" ? plugins.data : extensions.data), [extensions.data, plugins.data, skills.data, view]);
  const activeQuery = view === "skills" ? skills : view === "plugins" ? plugins : extensions;

  async function execute(operation: () => Promise<unknown>, success: string) {
    setBusy(true); setMessage("");
    try {
      await operation();
      setMessage(success);
      await queryClient.invalidateQueries({ queryKey: ["capabilities"] });
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }

  async function reload(approvedPlanHash?: string) {
    setBusy(true);
    setMessage("正在验证新的 Runtime Generation…");
    try {
      const result = await api.reloadRuntimePlugins(approvedPlanHash);
      const planHash = String(result.plan_hash || "");
      setPendingPlanHash(String(result.status) === "awaiting_approval" ? planHash : "");
      setMessage(`Reload ${String(result.status || "已提交")}；当前 Turn 不会切换版本。`);
      await queryClient.invalidateQueries({ queryKey: ["capabilities"] });
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }

  return <WorkbenchShell
    projectName="Capabilities"
    modelControls={<span className="mode-chip">RUNTIME GENERATION</span>}
    inspectorOpen={inspectorOpen}
    onToggleInspector={() => setInspectorOpen((value) => !value)}
    onOpenCommands={() => setCommandsOpen(true)}
    sidebarLabel="能力目录"
    inspectorLabel="能力详情"
    sidebar={<div className="sidebar-layout"><div className="sidebar-heading"><div><span>Runtime</span><h2>能力</h2></div></div><nav className="section-nav"><button className={view === "skills" ? "active" : ""} onClick={() => setParams(projectId ? { view: "skills", project: projectId } : { view: "skills" })}><Sparkles /><span>Skills</span></button><button className={view === "plugins" ? "active" : ""} onClick={() => setParams({ view: "plugins" })}><Blocks /><span>Plugins</span></button><button className={view === "extensions" ? "active" : ""} onClick={() => setParams({ view: "extensions" })}><PlugZap /><span>Extensions</span></button></nav>{view === "skills" && <label className="sidebar-select"><span>项目范围</span><select value={projectId || ""} onChange={(event) => setParams({ view: "skills", project: event.target.value })}>{(projects.data || []).map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}</select></label>}</div>}
    inspector={<><CapabilityInspector item={selected} /><CapabilityActions view={view} projectId={projectId} selected={selected} busy={busy} onSkill={(value) => execute(() => api.manageSkill(value), "Skill 管理计划已提交；若权限扩大，仍需完成 Gateway 审批。")} onRollback={(pluginId, generationId) => execute(() => api.rollbackRuntimePlugin(pluginId, generationId), "已发布新的回滚 Generation；下一 Turn 生效。")} onExtension={(action, value) => execute(() => action === "reenable" ? api.reenableExtension({ hook_id: String(value.hook_id), stage: String(value.stage), source_hash: String(value.source_hash), expected_revision: Number(value.expected_revision), reason: String(value.reason) }) : api.changeExtensionGrant({ hook_id: String(value.hook_id), stage: String(value.stage), source_hash: String(value.source_hash), manifest_hash: String(value.manifest_hash), capabilities: value.capabilities as string[], tools: value.tools as string[], tool_contract_hashes: value.tool_contract_hashes as Record<string, string>, reason: String(value.reason) }, action === "revoke"), "Extension 持久决策已记录。")} /></>}
    footer={<><StatusMark state={activeQuery.isError ? "warning" : "ok"} label={activeQuery.isError ? "能力目录异常" : "权限边界有效"} /><span className="status-spacer" /><span>{rows.length} 项</span></>}
  >
    <div className="management-page"><header className="management-header"><div><p className="eyebrow">CAPABILITY LAYER</p><h1>{view === "skills" ? "Skills" : view === "plugins" ? "Runtime Plugins" : "Hook Extensions"}</h1><p>这里展示不可变 Generation 中的能力与持久授权，不修改 Stable Core。</p></div>{view === "plugins" && <button className="secondary-button" disabled={busy} onClick={() => void reload()}><RefreshCw />验证并 Reload</button>}</header>{message && <div className="operation-message" role="status">{message}{pendingPlanHash && <button className="inline-approval" disabled={busy} onClick={() => void reload(pendingPlanHash)}>批准计划 {pendingPlanHash.slice(0, 12)}</button>}</div>}{activeQuery.isLoading && <div className="page-state">正在解析能力快照…</div>}{activeQuery.isError && <div className="page-state error">{activeQuery.error.message}</div>}<CapabilityTable rows={rows} onSelect={(item) => { setSelected(item); setInspectorOpen(true); }} /></div>
    <CommandPalette open={commandsOpen} onClose={() => setCommandsOpen(false)} onNewSession={() => navigate("/agent")} onAddProject={() => navigate("/agent")} onTheme={setPreference} />
  </WorkbenchShell>;
}

function normalizeRows(value?: JsonObject): JsonObject[] {
  if (!value) return [];
  for (const key of ["extensions", "plugins", "members", "runtime_states", "generations"]) {
    const selected = value[key];
    if (Array.isArray(selected)) return selected.filter((item): item is JsonObject => Boolean(item) && typeof item === "object");
    if (selected && typeof selected === "object") return Object.entries(selected).map(([name, item]) => ({ name, ...(typeof item === "object" && item ? item as JsonObject : { value: item }) }));
  }
  const runtimeRows: JsonObject[] = [];
  for (const key of ["heads", "quarantined_plugins", "recent_attempts", "active_references", "approvals"]) {
    const items = value[key];
    if (Array.isArray(items)) runtimeRows.push(...items.filter((item): item is JsonObject => Boolean(item) && typeof item === "object").map((item) => ({ category: key, ...item })));
  }
  if (runtimeRows.length) return runtimeRows;
  return [value];
}

function CapabilityTable({ rows, onSelect }: { rows: JsonObject[]; onSelect: (row: JsonObject) => void }) {
  if (!rows.length) return <div className="page-state">当前范围没有已发布能力。</div>;
  return <div className="capability-list">{rows.map((row, index) => { const name = String(row.name || row.plugin_id || row.hook_id || row.generation_id || `Capability ${index + 1}`); const description = String(row.description || row.status || row.stage || row.profile || "已加载到当前能力目录"); return <button key={`${name}-${index}`} onClick={() => onSelect(row)}><span className="capability-icon"><Blocks aria-hidden="true" /></span><span><strong>{name}</strong><small>{description}</small></span><code>{String(row.version || row.semantic_hash || row.source_hash || "active").slice(0, 18)}</code></button>; })}</div>;
}

function CapabilityInspector({ item }: { item: JsonObject | null }) { return <div className="inspector-content"><header><div><span>CAPABILITY</span><h2>{item ? String(item.name || item.plugin_id || item.hook_id || "能力详情") : "选择一项能力"}</h2></div><Shield aria-hidden="true" /></header>{item ? <dl className="inspector-dl">{Object.entries(item).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? "—")}</dd></div>)}</dl> : <p className="quiet-empty">选择 Skill、Plugin 或 Extension 查看版本、Profile、Grant 和隔离状态。</p>}</div>; }
