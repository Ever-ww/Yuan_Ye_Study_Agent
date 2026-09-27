import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Blocks, PlugZap, RefreshCw, Shield, Sparkles, Wrench } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WorkbenchShell } from "../../app/WorkbenchShell";
import { useTheme } from "../../app/ThemeProvider";
import { useGatewayApi } from "../../shared/api/context";
import { CommandPalette } from "../../shared/ui/CommandPalette";
import { StatusMark } from "../../shared/ui/StatusMark";
import type { JsonObject } from "../../types";
import { CapabilityActions } from "./CapabilityActions";
import { extensionCapabilityLabel, extensionGrantLabel } from "./extensionLabels";

type View = "skills" | "tools" | "plugins" | "extensions";

export function CapabilitiesPage() {
  const api = useGatewayApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setPreference } = useTheme();
  const [params, setParams] = useSearchParams();
  const view = (["skills", "tools", "plugins", "extensions"].includes(params.get("view") || "") ? params.get("view") : "skills") as View;
  const [commandsOpen, setCommandsOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [selected, setSelected] = useState<JsonObject | null>(null);
  useEffect(() => setSelected(null), [view]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingPlanHash, setPendingPlanHash] = useState("");
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const projectId = params.get("project") || window.localStorage.getItem("yyagent.web.selected-project") || projects.data?.[0]?.project_id;
  const catalog = useQuery({ queryKey: ["capabilities", "catalog", projectId], queryFn: () => api.capabilities(projectId!), enabled: (view === "skills" || view === "tools") && Boolean(projectId) });
  const plugins = useQuery({ queryKey: ["capabilities", "plugins"], queryFn: () => api.runtimePluginStatus(), enabled: view === "plugins" });
  const extensions = useQuery({ queryKey: ["capabilities", "extensions"], queryFn: () => api.extensionStatus(), enabled: view === "extensions" });
  const rows = useMemo(() => view === "skills" ? catalog.data?.skills || [] : view === "tools" ? catalog.data?.tools || [] : normalizeRows(view === "plugins" ? plugins.data : extensions.data), [extensions.data, plugins.data, catalog.data, view]);
  const selectedItem = view === "plugins" ? rows.find((row) => row.plugin_id === selected?.plugin_id) || null : selected;
  const activeQuery = view === "skills" || view === "tools" ? catalog : view === "plugins" ? plugins : extensions;

  async function toggleCapability(row: JsonObject, enabled: boolean) {
    if (!projectId || !catalog.data || (view !== "skills" && view !== "tools")) return;
    const name = String(row.name);
    setBusy(true); setMessage("");
    try {
      await api.toggleCapability(projectId, view, name, enabled, catalog.data.revision);
      setMessage(`${name} 已${enabled ? "启用" : "停用"}，下一轮 Agent 任务生效。`);
      setSelected((current) => current?.name === name ? { ...current, enabled } : current);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      await queryClient.invalidateQueries({ queryKey: ["capabilities", "catalog", projectId] });
      setBusy(false);
    }
  }

  async function togglePlugin(row: JsonObject, enabled: boolean) {
    if (!plugins.data) return;
    setBusy(true); setMessage("");
    try {
      const result = await api.toggleRuntimePlugin(String(row.plugin_id), enabled, Number(plugins.data.revision));
      const reloadResult = result.reload as JsonObject;
      const pending = reloadResult.status === "awaiting_approval";
      setPendingPlanHash(pending ? String(reloadResult.plan_hash || "") : "");
      setMessage(pending
        ? `${String(row.display_name)}的变更需要审批；旧版本在批准前继续运行。`
        : `${String(row.display_name)}已${enabled ? "启用" : "停用"}，从下一轮任务生效。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      await queryClient.invalidateQueries({ queryKey: ["capabilities", "plugins"] });
      setBusy(false);
    }
  }

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
    sidebar={<div className="sidebar-layout"><div className="sidebar-heading"><div><span>Runtime</span><h2>能力</h2></div></div><nav className="section-nav"><button className={view === "skills" ? "active" : ""} onClick={() => setParams(projectId ? { view: "skills", project: projectId } : { view: "skills" })}><Sparkles /><span>Skills</span></button><button className={view === "tools" ? "active" : ""} onClick={() => setParams(projectId ? { view: "tools", project: projectId } : { view: "tools" })}><Wrench /><span>Tools</span></button><button className={view === "plugins" ? "active" : ""} onClick={() => setParams({ view: "plugins" })}><Blocks /><span>Plugins</span></button><button className={view === "extensions" ? "active" : ""} onClick={() => setParams({ view: "extensions" })}><PlugZap /><span>Extensions</span></button></nav></div>}
    inspector={<><CapabilityInspector item={selectedItem} view={view} rows={rows} /><CapabilityActions view={view} projectId={projectId} selected={selectedItem} busy={busy} onSkill={(value) => execute(() => api.manageSkill(value), "Skill 管理计划已提交；若权限扩大，仍需完成 Gateway 审批。")} onRollback={(pluginId, generationId) => execute(() => api.rollbackRuntimePlugin(pluginId, generationId), "已发布新的回滚 Generation；下一 Turn 生效。")} onExtension={(action, value) => execute(() => action === "reenable" ? api.reenableExtension({ hook_id: String(value.hook_id), stage: String(value.stage), source_hash: String(value.source_hash), expected_revision: Number(value.expected_revision), reason: String(value.reason) }) : api.changeExtensionGrant({ hook_id: String(value.hook_id), stage: String(value.stage), source_hash: String(value.source_hash), manifest_hash: String(value.manifest_hash), capabilities: value.capabilities as string[], tools: value.tools as string[], tool_contract_hashes: value.tool_contract_hashes as Record<string, string>, reason: String(value.reason) }, action === "revoke"), "Extension 持久决策已记录。")} /></>}
    footer={<><StatusMark state={activeQuery.isError ? "warning" : "ok"} label={activeQuery.isError ? "能力目录异常" : "权限边界有效"} /><span className="status-spacer" /><span>{rows.length} 项</span></>}
  >
    <div className="management-page"><header className="management-header"><div><p className="eyebrow">CAPABILITY LAYER</p><h1>{view === "skills" ? "Skills" : view === "tools" ? "Tools" : view === "plugins" ? "Runtime Plugins" : "Hook Extensions"}</h1><p>{view === "plugins" ? "插件由声明文件定义，启停会发布新的 Runtime 版本；当前任务保持原版本。" : "Skill 和 Tool 的开关按当前 Workspace 保存，对下一轮 Agent 任务生效；已有的权限审批仍然有效。"}</p></div>{view === "plugins" && <button className="secondary-button" disabled={busy} onClick={() => void reload()}><RefreshCw />验证并 Reload</button>}</header>{message && <div className="operation-message" role="status">{message}{pendingPlanHash && <button className="inline-approval" disabled={busy} onClick={() => void reload(pendingPlanHash)}>批准计划 {pendingPlanHash.slice(0, 12)}</button>}</div>}{activeQuery.isLoading && <div className="page-state">正在解析能力快照…</div>}{activeQuery.isError && <div className="page-state error">{activeQuery.error.message}</div>}<CapabilityTable rows={rows} view={view} busy={busy} onToggle={(item, enabled) => void (view === "plugins" ? togglePlugin(item, enabled) : toggleCapability(item, enabled))} onSelect={(item) => { setSelected(item); setInspectorOpen(true); }} /></div>
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

const hookStageLabels: Record<string, string> = {
  trace_start: "任务开始", trace_end: "任务结束", turn_start: "本轮开始", turn_end: "本轮结束",
  model_before: "模型请求前", model_during: "模型生成中", model_after: "模型返回后",
  tool_before: "工具执行前", tool_during: "工具执行中", tool_after: "工具执行后",
};

function extensionLabel(row: JsonObject): string {
  const stage = hookStageLabels[String(row.stage)] || "扩展";
  return `${stage} · ${String(row.name || "未命名").startsWith("default-") ? "默认示例" : String(row.name || "未命名")}`;
}

function CapabilityTable({ rows, view, busy, onSelect, onToggle }: { rows: JsonObject[]; view: View; busy: boolean; onSelect: (row: JsonObject) => void; onToggle: (row: JsonObject, enabled: boolean) => void }) {
  if (!rows.length) return <div className="page-state">当前范围没有已发布能力。</div>;
  const groups = view === "plugins"
    ? [
        { title: "可管理插件", description: "这些插件可以启停；变更发布后对下一轮任务生效。", items: rows.filter((row) => row.available && row.configurable === true) },
        { title: "运行基础组件", description: "由运行时统一管理，不提供单独开关。", items: rows.filter((row) => row.available && row.configurable !== true) },
        { title: "未安装", description: "目录中只有声明，尚缺对应代码或资源；补齐后可验证并 Reload。", items: rows.filter((row) => !row.available) },
      ]
    : [{ title: "", description: "", items: rows }];

  return <div className="capability-groups">{groups.map((group) => <section className="capability-group" key={group.title || view}>
    {group.title && <header className="capability-group-heading"><h2>{group.title}</h2><p>{group.description}</p></header>}
    {group.items.length ? <div className="capability-list">{group.items.map((row, index) => {
      const name = view === "extensions" ? extensionLabel(row) : String(row.display_name || row.name || row.plugin_id || row.hook_id || row.generation_id || `Capability ${index + 1}`);
      const description = view === "extensions"
        ? `在${hookStageLabels[String(row.stage)] || "指定阶段"}运行；${Array.isArray(row.requested_capabilities) && row.requested_capabilities.length ? "申请额外权限，需经授权" : "无需额外权限"}`
        : String(row.description || row.status || row.stage || row.profile || "已加载到当前能力目录");
      const switchable = view === "skills" || view === "tools" || (view === "plugins" && row.toggleable === true);
      const checked = view === "plugins" ? row.desired_enabled === true : row.enabled === true;
      const pending = view === "plugins" && row.configurable === true && row.enabled !== row.desired_enabled;
      const pluginStatus = !row.available ? row.enabled ? "资源目录已移除 · 当前版本仍运行" : "尚未提供代码或资源"
        : pending ? "等待发布或审批" : row.enabled ? "运行中" : row.configurable ? "已停用" : "当前未运行";
      return <div className="capability-row" key={String(row.plugin_id || row.name || row.hook_id || index)}>
        <button className="capability-row-detail" onClick={() => onSelect(row)}>
          <span className="capability-icon"><Blocks aria-hidden="true" /></span>
          <span><strong>{name}</strong><small>{description}</small>{view === "plugins" && <small>{pluginStatus}</small>}</span>
          {view !== "plugins" && view !== "extensions" && !switchable && <code>{String(row.version || row.semantic_hash || row.source_hash || "active").slice(0, 18)}</code>}
        </button>
        {switchable ? <label className="capability-switch"><input type="checkbox" role="switch" aria-label={`${checked ? "停用" : "启用"} ${name}`} checked={checked} disabled={busy} onChange={(event) => onToggle(row, event.target.checked)} /><span>{checked ? "已启用" : "已停用"}</span></label>
          : view === "plugins" && <span className="capability-fixed">{!row.available ? row.enabled ? "资源已移除" : "未安装" : "基础组件"}</span>}
      </div>;
    })}</div> : <p className="capability-group-empty">{group.title === "未安装" ? "没有待安装项目。" : "当前没有可管理插件。添加有效资源后，刷新目录即可显示。"}</p>}
  </section>)}</div>;
}

const profileLabels: Record<string, string> = { interactive: "Agent 对话", cron: "定时任务", dream: "Dream", subagent: "子任务", maintenance: "维护任务", "harness:manual": "Code 会话", "harness:error": "故障修复", "harness:capability": "能力演进", "harness:dream": "Dream Code" };

function CapabilityInspector({ item, view, rows }: { item: JsonObject | null; view: View; rows: JsonObject[] }) {
  if (view === "plugins") {
    const dependencies = Array.isArray(item?.requires_plugins) ? item.requires_plugins.map(String) : [];
    return <div className="inspector-content"><header><div><span>PLUGIN</span><h2>{item ? String(item.display_name || item.plugin_id) : "选择一个插件"}</h2></div><Shield aria-hidden="true" /></header>{item ? <><p>{String(item.description || "暂无说明")}</p><dl className="inspector-dl"><div><dt>当前状态</dt><dd>{!item.available ? item.enabled ? "资源目录已移除；当前版本仍运行" : "未安装：尚未提供代码或资源" : item.enabled ? "运行中" : "未运行"}{item.configurable && item.enabled !== item.desired_enabled ? " · 变更待发布" : ""}</dd></div><div><dt>适用任务</dt><dd>{Array.isArray(item.profiles) && item.profiles.length ? item.profiles.map((value) => profileLabels[String(value)] || String(value)).join("、") : "系统基础服务"}</dd></div><div><dt>依赖能力</dt><dd>{dependencies.length ? dependencies.map((id) => String(rows.find((row) => row.plugin_id === id)?.display_name || id)).join("、") : "无"}</dd></div><div><dt>管理方式</dt><dd>{item.toggleable ? "可启停；下一轮任务生效" : !item.available ? "补齐对应资源目录后才能操作" : "基础组件，由运行时统一管理"}</dd></div></dl></> : <p className="quiet-empty">选择插件后查看用途、适用任务和启停状态。</p>}</div>;
  }
  if (view === "extensions") {
    const grant = item?.grant && typeof item.grant === "object" ? item.grant as JsonObject : null;
    return <div className="inspector-content"><header><div><span>HOOK EXTENSION</span><h2>{item ? extensionLabel(item) : "选择一个扩展"}</h2></div><Shield aria-hidden="true" /></header>{item ? <><p>这个扩展在 Agent 的{hookStageLabels[String(item.stage)] || "指定阶段"}运行。它与插件开关不同，所需权限仍须单独授权。</p><dl className="inspector-dl"><div><dt>权限状态</dt><dd>{grant?.status ? extensionGrantLabel(String(grant.status)) : "未见持久授权记录"}</dd></div><div><dt>请求权限</dt><dd>{Array.isArray(item.requested_capabilities) && item.requested_capabilities.length ? item.requested_capabilities.map((value) => extensionCapabilityLabel(String(value))).join("、") : "无"}</dd></div><div><dt>可调用工具</dt><dd>{Array.isArray(item.requested_tools) && item.requested_tools.length ? item.requested_tools.map(String).join("、") : "无"}</dd></div></dl><details><summary>技术标识与校验信息</summary><dl className="inspector-dl"><div><dt>扩展 ID</dt><dd>{String(item.hook_id)}</dd></div><div><dt>源码指纹</dt><dd>{String(item.source_hash)}</dd></div></dl></details></> : <p className="quiet-empty">选择扩展后查看其运行阶段和申请的权限。</p>}</div>;
  }
  return <div className="inspector-content"><header><div><span>CAPABILITY</span><h2>{item ? String(item.name || item.plugin_id || item.hook_id || "能力详情") : "选择一项能力"}</h2></div><Shield aria-hidden="true" /></header>{item ? <dl className="inspector-dl">{Object.entries(item).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? "—")}</dd></div>)}</dl> : <p className="quiet-empty">选择 Skill、Tool、Plugin 或 Extension 查看能力状态和权限边界。</p>}</div>;
}
