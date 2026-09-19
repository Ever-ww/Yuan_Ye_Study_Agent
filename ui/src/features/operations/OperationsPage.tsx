import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Bot, CalendarClock, CheckCheck, DatabaseBackup, Inbox, Pencil, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WorkbenchShell } from "../../app/WorkbenchShell";
import { useTheme } from "../../app/ThemeProvider";
import { useGatewayApi } from "../../shared/api/context";
import { CommandPalette } from "../../shared/ui/CommandPalette";
import { StatusMark } from "../../shared/ui/StatusMark";
import type { InboxItem, JsonObject } from "../../types";
import type { CronJobInput } from "../../types";
import { CronEditor } from "./CronEditor";
import { DreamControls } from "./DreamControls";

type Section = "inbox" | "cron" | "dream" | "backup" | "maintenance";

const sections: Array<{ id: Section; label: string; icon: typeof Inbox }> = [
  { id: "inbox", label: "Inbox", icon: Inbox },
  { id: "cron", label: "定时任务", icon: CalendarClock },
  { id: "dream", label: "Dream", icon: Bot },
  { id: "backup", label: "备份", icon: DatabaseBackup },
  { id: "maintenance", label: "维护状态", icon: ShieldCheck },
];

export function OperationsPage() {
  const api = useGatewayApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { setPreference } = useTheme();
  const [params, setParams] = useSearchParams();
  const selected = (sections.some((item) => item.id === params.get("section")) ? params.get("section") : "inbox") as Section;
  const [selectedInbox, setSelectedInbox] = useState<InboxItem | null>(null);
  const [selectedCron, setSelectedCron] = useState<JsonObject | null>(null);
  const [cronEditor, setCronEditor] = useState<JsonObject | null | undefined>(undefined);
  const [commandsOpen, setCommandsOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");

  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const inbox = useQuery({ queryKey: ["operations", "inbox"], queryFn: () => api.inbox(false), enabled: selected === "inbox" });
  const cron = useQuery({ queryKey: ["operations", "cron"], queryFn: async () => { const [status, jobs] = await Promise.all([api.cronStatus(), api.cronJobs()]); return { status, jobs }; }, enabled: selected === "cron" });
  const cronHistory = useQuery({ queryKey: ["operations", "cron-history", selectedCron?.job_id], queryFn: () => api.cronHistory(String(selectedCron!.job_id)), enabled: selected === "cron" && Boolean(selectedCron?.job_id) });
  const dream = useQuery({ queryKey: ["operations", "dream"], queryFn: async () => { const [dreamStatus, harness] = await Promise.all([api.dreamStatus(), api.harnessDreamStatus()]); return { dream: dreamStatus, harness }; }, enabled: selected === "dream" });
  const backup = useQuery({ queryKey: ["operations", "backup"], queryFn: async () => { const [status, items] = await Promise.all([api.backupStatus(), api.backups()]); return { status, items }; }, enabled: selected === "backup" });
  const maintenance = useQuery({ queryKey: ["operations", "maintenance"], queryFn: () => api.maintenanceStatus(), enabled: selected === "maintenance" });
  const activeQuery = { inbox, cron, dream, backup, maintenance }[selected];

  async function action(name: string, operation: () => Promise<unknown>): Promise<boolean> {
    setBusy(name); setMessage("");
    try {
      await operation();
      setMessage("操作已提交并由 Gateway 记录。");
      await queryClient.invalidateQueries({ queryKey: ["operations"] });
      return true;
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); return false; }
    finally { setBusy(""); }
  }

  async function saveCron(value: CronJobInput & { project_id: string }) {
    const ok = await action("save-cron", () => cronEditor
      ? api.editCron(String(cronEditor.job_id), value)
      : api.createCron(value));
    if (ok) setCronEditor(undefined);
  }

  const unread = useMemo(() => (inbox.data || []).filter((item) => !item.read).length, [inbox.data]);

  return (
    <WorkbenchShell
      projectName="Operations"
      modelControls={<span className="mode-chip">CONTROL PLANE</span>}
      inspectorOpen={inspectorOpen}
      onToggleInspector={() => setInspectorOpen((value) => !value)}
      onOpenCommands={() => setCommandsOpen(true)}
      sidebarLabel="运维模块"
      inspectorLabel="运维详情"
      sidebar={<div className="sidebar-layout"><div className="sidebar-heading"><div><span>工作台</span><h2>运维</h2></div></div><nav className="section-nav">{sections.map(({ id, label, icon: Icon }) => <button key={id} className={selected === id ? "active" : ""} onClick={() => setParams({ section: id })}><Icon aria-hidden="true" /><span>{label}</span>{id === "inbox" && unread > 0 && <b>{unread}</b>}</button>)}</nav></div>}
      inspector={<OperationsInspector section={selected} item={selectedInbox} cron={selectedCron} history={cronHistory.data || []} data={activeQuery.data} />}
      footer={<><StatusMark state={activeQuery.isError ? "warning" : "ok"} label={activeQuery.isError ? "数据加载失败" : "Gateway 管理接口"} /><span className="status-spacer" /><span>{projects.data?.length || 0} 个项目</span></>}
    >
      <div className="management-page">
        <header className="management-header"><div><p className="eyebrow">OPERATIONS</p><h1>{sections.find((item) => item.id === selected)?.label}</h1><p>查看持久状态并通过 Gateway 执行可审计操作。</p></div>{selected === "inbox" && <button className="secondary-button" disabled={!unread || busy === "read-all"} onClick={() => void action("read-all", () => api.markAllRead())}><CheckCheck aria-hidden="true" />全部已读</button>}{selected === "cron" && <button className="secondary-button" disabled={Boolean(busy)} onClick={() => setCronEditor(null)}><Plus aria-hidden="true" />新建任务</button>}{selected === "backup" && <button className="secondary-button" disabled={Boolean(busy)} onClick={() => void action("backup", () => api.createBackup())}><Archive aria-hidden="true" />创建备份</button>}</header>
        {message && <div className="operation-message" role="status">{message}</div>}
        {activeQuery.isLoading && <div className="page-state">正在读取持久状态…</div>}
        {activeQuery.isError && <div className="page-state error">{activeQuery.error.message}</div>}
        {selected === "inbox" && inbox.data && <InboxTable items={inbox.data} selected={selectedInbox?.item_id} onSelect={(item) => { setSelectedInbox(item); setInspectorOpen(true); if (!item.read) void action(`read-${item.item_id}`, () => api.markRead(item.item_id)); }} />}
        {selected === "cron" && cronEditor !== undefined && <CronEditor job={cronEditor} projects={projects.data || []} busy={Boolean(busy)} onClose={() => setCronEditor(undefined)} onSave={saveCron} />}
        {selected === "cron" && cron.data && <CronTable jobs={cron.data.jobs} selected={String(selectedCron?.job_id || "")} busy={busy} onSelect={(job) => { setSelectedCron(job); setInspectorOpen(true); }} onEdit={setCronEditor} onDelete={(job) => void action(`delete-${String(job.job_id)}`, () => api.removeCron(String(job.job_id))).then((ok) => { if (ok && selectedCron?.job_id === job.job_id) setSelectedCron(null); })} onAction={(id, kind) => void action(`${kind}-${id}`, () => kind === "run" ? api.runCron(id) : api.setCronPaused(id, kind === "pause"))} />}
        {selected === "dream" && dream.data && <><DreamControls dream={dream.data.dream} harness={dream.data.harness} busy={Boolean(busy)} onRun={(date) => action("dream", () => api.runDream(date)).then(() => undefined)} onBackfill={(start, end) => action("dream-backfill", () => api.backfillDream(start, end)).then(() => undefined)} onRollback={(runId) => action("dream-rollback", () => api.rollbackDream(runId)).then(() => undefined)} onHarnessRun={(value) => action("harness-dream", () => api.runHarnessDream(value)).then(() => undefined)} onHarnessFreeze={(frozen, reason) => action("harness-freeze", () => api.setHarnessDreamFrozen(frozen, reason)).then(() => undefined)} /><StatusGrid entries={[["Daily Dream", dream.data.dream], ["Harness Dream", dream.data.harness]]} /></>}
        {selected === "backup" && backup.data && <><StatusGrid entries={[["Backup Store", backup.data.status]]} /><RecordTable rows={backup.data.items} empty="还没有备份记录。" /></>}
        {selected === "maintenance" && maintenance.data && <StatusGrid entries={[["Maintenance Gate", maintenance.data]]} />}
      </div>
      <CommandPalette open={commandsOpen} onClose={() => setCommandsOpen(false)} onNewSession={() => navigate("/agent")} onAddProject={() => navigate("/agent")} onTheme={setPreference} />
    </WorkbenchShell>
  );
}

function InboxTable({ items, selected, onSelect }: { items: InboxItem[]; selected?: string; onSelect: (item: InboxItem) => void }) {
  if (!items.length) return <div className="page-state">Inbox 已清空。</div>;
  return <div className="data-table-wrap"><table className="data-table"><thead><tr><th>状态</th><th>任务</th><th>结果摘要</th><th>时间</th><th>ID</th></tr></thead><tbody>{items.map((item) => <tr key={item.item_id} className={`${selected === item.item_id ? "selected" : ""} ${item.read ? "read" : "unread"}`} onClick={() => onSelect(item)} tabIndex={0} onKeyDown={(event) => event.key === "Enter" && onSelect(item)}><td><span className={`state-text state-${item.status}`}>{item.status}</span></td><td>{item.title}</td><td className="summary-cell">{item.summary}</td><td className="mono-cell">{formatTime(item.created_at)}</td><td className="mono-cell">{item.item_id}</td></tr>)}</tbody></table></div>;
}

function CronTable({ jobs, selected, busy, onSelect, onEdit, onDelete, onAction }: { jobs: JsonObject[]; selected: string; busy: string; onSelect: (job: JsonObject) => void; onEdit: (job: JsonObject) => void; onDelete: (job: JsonObject) => void; onAction: (id: string, kind: "run" | "pause" | "resume") => void }) {
  if (!jobs.length) return <div className="page-state">尚未配置定时任务。</div>;
  return <div className="data-table-wrap"><table className="data-table"><thead><tr><th>名称</th><th>状态</th><th>下次运行</th><th>最近结果</th><th>操作</th></tr></thead><tbody>{jobs.map((job) => { const id = String(job.job_id || ""); const state = String(job.state || "unknown"); return <tr key={id} className={selected === id ? "selected" : ""} onClick={() => onSelect(job)}><td>{String(job.name || id)}</td><td>{state}</td><td className="mono-cell">{String(job.next_run_at || "—")}</td><td>{String(job.last_status || "尚未运行")}</td><td className="row-actions"><button disabled={Boolean(busy)} onClick={(event) => { event.stopPropagation(); onAction(id, "run"); }}>运行</button><button disabled={Boolean(busy)} onClick={(event) => { event.stopPropagation(); onAction(id, state === "paused" ? "resume" : "pause"); }}>{state === "paused" ? "恢复" : "暂停"}</button><button aria-label={`编辑 ${String(job.name || id)}`} disabled={Boolean(busy)} onClick={(event) => { event.stopPropagation(); onEdit(job); }}><Pencil aria-hidden="true" /></button><button aria-label={`删除 ${String(job.name || id)}`} disabled={Boolean(busy)} onClick={(event) => { event.stopPropagation(); if (window.confirm(`删除定时任务“${String(job.name || id)}”？`)) onDelete(job); }}><Trash2 aria-hidden="true" /></button></td></tr>; })}</tbody></table></div>;
}

function StatusGrid({ entries }: { entries: Array<[string, JsonObject]> }) { return <div className="status-grid">{entries.map(([label, value]) => <section key={label}><h2>{label}</h2><dl>{Object.entries(value).slice(0, 14).map(([key, item]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{display(item)}</dd></div>)}</dl></section>)}</div>; }
function RecordTable({ rows, empty }: { rows: JsonObject[]; empty: string }) { if (!rows.length) return <div className="page-state">{empty}</div>; const columns = Object.keys(rows[0]).filter((key) => !["path"].includes(key)).slice(0, 6); return <div className="data-table-wrap"><table className="data-table"><thead><tr>{columns.map((key) => <th key={key}>{key.replaceAll("_", " ")}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={String(row.id || row.backup_id || index)}>{columns.map((key) => <td key={key}>{display(row[key])}</td>)}</tr>)}</tbody></table></div>; }

function OperationsInspector({ section, item, cron, history, data }: { section: Section; item: InboxItem | null; cron: JsonObject | null; history: JsonObject[]; data: unknown }) { return <div className="inspector-content"><header><div><span>OPERATIONS</span><h2>{section === "inbox" && item ? item.title : section === "cron" && cron ? String(cron.name || "定时任务") : "状态详情"}</h2></div><ShieldCheck aria-hidden="true" /></header>{section === "inbox" && item ? <><section className="inspector-section"><h3>结果</h3><p>{item.summary}</p></section><section className="inspector-section"><h3>标识</h3><p className="mono-block">{item.item_id}<br />{item.run_id}</p></section></> : section === "cron" && cron ? <><dl className="inspector-dl">{Object.entries(cron).filter(([key]) => !["prompt", "runtime_profile"].includes(key)).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{display(value)}</dd></div>)}</dl><section className="inspector-section"><h3>执行历史</h3>{history.slice(0, 12).map((row, index) => <p className="history-line" key={String(row.dispatch_id || index)}><strong>{String(row.status || "unknown")}</strong><span>{String(row.completed_at || row.created_at || "—")}</span></p>)}{!history.length && <p>尚无执行记录。</p>}</section></> : <section className="inspector-section"><h3>当前视图</h3><p>选择表格行可在这里查看完整内容。管理页只展示 Gateway 已持久化的公开状态。</p><small className="muted-text">{data ? "数据已同步" : "等待数据"}</small></section>}</div>; }
function display(value: unknown): string { if (value === null || value === undefined || value === "") return "—"; if (typeof value === "object") return JSON.stringify(value); return String(value); }
function formatTime(value: string) { try { return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)); } catch { return value; } }
