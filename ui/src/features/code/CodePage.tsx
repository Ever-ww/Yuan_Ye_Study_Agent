import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Braces, GitMerge, GitPullRequest, Play, ShieldAlert, Trash2 } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WorkbenchShell } from "../../app/WorkbenchShell";
import { useTheme } from "../../app/ThemeProvider";
import { useGatewayApi } from "../../shared/api/context";
import { CommandPalette } from "../../shared/ui/CommandPalette";
import { StatusMark } from "../../shared/ui/StatusMark";
import type { CodeSessionEvent, CodeSessionSummary, ReasoningEffort } from "../../types";
import { ModelControls } from "../agent/ModelControls";

export function CodePage() {
  const api = useGatewayApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setPreference } = useTheme();
  const [params, setParams] = useSearchParams();
  const [commandsOpen, setCommandsOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [confirm, setConfirm] = useState<"finalize" | "delete" | null>(null);
  const [profileId, setProfileId] = useState("default");
  const [effort, setEffort] = useState<ReasoningEffort>(() => {
    const saved = window.localStorage.getItem("yyagent.web.code.reasoning-effort");
    return isEffort(saved) ? saved : "low";
  });
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const projectId = params.get("project") || projects.data?.[0]?.project_id;
  const sessionId = params.get("session") || undefined;
  const sessions = useQuery({ queryKey: ["code", "sessions", projectId], queryFn: () => api.codeSessions(projectId), enabled: Boolean(projectId), refetchInterval: 5_000 });
  const session = sessions.data?.find((item) => item.code_session_id === sessionId);
  const events = useQuery({ queryKey: ["code", "events", sessionId], queryFn: () => api.codeEvents(sessionId!), enabled: Boolean(sessionId), refetchInterval: session && ["active", "verified", "unverified"].includes(session.status) ? 2_000 : false });
  const models = useQuery({ queryKey: ["models", "code", projectId, sessionId], queryFn: () => api.models(projectId), enabled: Boolean(projectId) });

  useEffect(() => {
    const selected = models.data?.find((item) => item.selected) || models.data?.[0];
    if (selected && !models.data?.some((item) => item.profile_id === profileId)) setProfileId(selected.profile_id);
  }, [models.data, profileId]);

  async function run(operation: () => Promise<unknown>, success: string) {
    setBusy(true); setMessage("");
    try {
      await operation();
      setMessage(success);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["code", "sessions"] }),
        queryClient.invalidateQueries({ queryKey: ["code", "events"] }),
      ]);
      return true;
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); return false; }
    finally { setBusy(false); setConfirm(null); }
  }

  async function start() {
    if (!projectId) return;
    setBusy(true); setMessage("");
    try {
      const created = await api.startCodeSession(projectId);
      setParams({ project: projectId, session: created.code_session_id });
      await sessions.refetch();
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }

  async function send() {
    if (!sessionId || !task.trim()) return;
    const request = task.trim(); setTask("");
    await run(() => api.runCodeTurn(sessionId, request, profileId, effort), "Coding Turn 已完成验证；请检查过程和变更状态。");
  }

  async function remove(sessionToDelete: string) {
    const deleted = await run(() => api.deleteCodeSession(sessionToDelete), "Coding Session 已删除。");
    if (deleted && sessionToDelete === sessionId) {
      setParams(projectId ? { project: projectId } : {});
    }
  }

  return <WorkbenchShell
    projectName="YY Code"
    modelControls={<ModelControls models={models.data || []} profileId={profileId} effort={effort} disabled={busy} onProfile={setProfileId} onEffort={(value) => { setEffort(value); window.localStorage.setItem("yyagent.web.code.reasoning-effort", value); }} />}
    inspectorOpen={inspectorOpen}
    onToggleInspector={() => setInspectorOpen((value) => !value)}
    onOpenCommands={() => setCommandsOpen(true)}
    sidebarLabel="Coding Sessions"
    inspectorLabel="Code Session 状态"
    sidebar={<CodeSidebar projects={projects.data || []} sessions={sessions.data || []} projectId={projectId} sessionId={sessionId} busy={busy} onProject={(id) => setParams({ project: id })} onSession={(id) => setParams(projectId ? { project: projectId, session: id } : { session: id })} onDelete={(id) => { if (window.confirm("删除这个独立 Coding Session、候选 worktree 和本地审计记录？")) void remove(id); }} onNew={() => void start()} />}
    inspector={<CodeInspector session={session} events={events.data || []} />}
    footer={<><StatusMark state={sessions.isError ? "warning" : "ok"} label={sessions.isError ? "Code 状态不可用" : "Harness worktree 隔离"} /><StatusMark state="ok" label="独立 Session / Cache" /><span className="status-spacer" /><span>Cache {cacheLabel(events.data || [])}</span></>}
  >
    <div className="code-workspace">
      {!session ? <div className="agent-empty"><p className="eyebrow">ISOLATED CODING RUNTIME</p><h1>在独立 worktree 中修改 YYAgent。</h1><p>每个 Code、Dream 和能力补全任务使用独立 Session；新 Session 只读取其他 Coding Observer 的脱敏摘要，不共享原始消息或缓存。</p><button className="primary-action" disabled={!projectId || busy} onClick={() => void start()}><Braces />创建 Coding Session</button></div> : <><header className="code-header"><div><p className="eyebrow">CODE SESSION</p><h1>{session.branch}</h1><p><code>{session.code_session_id}</code> · {session.status} · 已验证 {session.verified_turns} 轮</p></div><div className="header-actions"><button onClick={() => setConfirm("delete")} disabled={busy}><Trash2 />删除</button><button className="primary-button" onClick={() => setConfirm("finalize")} disabled={busy}><GitMerge />Finalize</button></div></header>{confirm && <div className="decision-strip" role="alert"><ShieldAlert /><div><strong>{confirm === "finalize" ? "确认合并已验证变更？" : "确认删除当前 Coding Session？"}</strong><small>{confirm === "finalize" ? "若主仓库 HEAD 或文件冲突，Gateway 会拒绝并保留 worktree。" : "候选 worktree 与本地审计记录会一并删除，正式源码不会受影响。"}</small></div><button onClick={() => setConfirm(null)}>取消</button><button className="danger-confirm" onClick={() => void (confirm === "finalize" ? run(() => api.finalizeCodeSession(session.code_session_id), "Finalize 已完成。") : remove(session.code_session_id))}>确认</button></div>} {message && <div className="operation-message" role="status">{message}</div>}<CodeEventList events={events.data || []} loading={events.isLoading} /><div className="code-composer"><textarea value={task} onChange={(event) => setTask(event.target.value)} placeholder="描述要实现或修复的源码任务…" disabled={busy} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void send(); } }} /><button disabled={busy || !task.trim()} onClick={() => void send()}>{busy ? "运行中" : <><Play />执行 Turn</>}</button></div></>}
    </div>
    <CommandPalette open={commandsOpen} onClose={() => setCommandsOpen(false)} onNewSession={() => navigate("/agent")} onAddProject={() => navigate("/agent")} onTheme={setPreference} />
  </WorkbenchShell>;
}

function CodeSidebar({ projects, sessions, projectId, sessionId, busy, onProject, onSession, onDelete, onNew }: { projects: Array<{ project_id: string; name: string }>; sessions: CodeSessionSummary[]; projectId?: string; sessionId?: string; busy: boolean; onProject: (id: string) => void; onSession: (id: string) => void; onDelete: (id: string) => void; onNew: () => void }) { return <div className="sidebar-layout"><div className="sidebar-heading"><div><span>隔离 Runtime</span><h2>YY Code</h2></div><button className="icon-button" onClick={onNew} disabled={busy} aria-label="新建 Coding Session"><GitPullRequest /></button></div><label className="sidebar-select"><span>关联项目</span><select value={projectId || ""} onChange={(event) => onProject(event.target.value)}>{projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}</select></label><div className="session-filter"><span>CODING SESSIONS</span></div><div className="session-list">{sessions.map((session) => <div className={`session-row ${sessionId === session.code_session_id ? "active" : ""}`} key={session.code_session_id}><button className="session-item" onClick={() => onSession(session.code_session_id)}><strong>{session.branch || session.code_session_id.slice(0, 12)}</strong><small>{session.status} · {session.updated_at || session.created_at}</small></button><button className="session-delete" disabled={busy} aria-label={`删除 ${session.branch || session.code_session_id}`} onClick={() => onDelete(session.code_session_id)}><Trash2 aria-hidden="true" /></button></div>)}{!sessions.length && <p className="quiet-empty">当前项目没有 Coding Session。</p>}</div></div>; }
function CodeEventList({ events, loading }: { events: CodeSessionEvent[]; loading: boolean }) { if (loading) return <div className="page-state">正在读取独立审计流…</div>; if (!events.length) return <div className="page-state">Session 已建立，等待第一个 Turn。</div>; return <div className="code-events">{events.map((event) => <details key={event.sequence} open={event.record_type.includes("turn")}><summary><span>{event.sequence}</span><strong>{event.record_type.replaceAll("_", " ")}</strong><time>{event.timestamp}</time></summary><pre>{JSON.stringify(Object.fromEntries(Object.entries(event).filter(([key]) => !["version", "sequence", "record_type", "timestamp"].includes(key))), null, 2)}</pre></details>)}</div>; }
function CodeInspector({ session, events }: { session?: CodeSessionSummary; events: CodeSessionEvent[] }) { return <div className="inspector-content"><header><div><span>CODE RUNTIME</span><h2>{session ? session.status : "未选择 Session"}</h2></div><Braces /></header>{session ? <><section className="inspector-section"><h3>逻辑路径</h3><p className="mono-block">{session.logical_roots.source}<br />{session.logical_roots.skills}<br />{session.logical_roots.hooks}</p></section><section className="inspector-section"><h3>隔离状态</h3><p>独立 Coding Session · {events.length} 条审计事件</p><p>真实 worktree 路径不会返回 Web。</p></section><section className="inspector-section"><h3>基线</h3><p className="mono-block">{session.base_commit || "—"}</p></section></> : <p className="quiet-empty">创建或选择 Coding Session 后查看隔离和验证状态。</p>}</div>; }
function cacheLabel(events: CodeSessionEvent[]): string { const calls = [...events].reverse().find((event) => Array.isArray(event.model_calls))?.model_calls as Array<Record<string, unknown>> | undefined; if (!calls?.length) return "—"; const input = calls[calls.length - 1]?.input_tokens as Record<string, unknown> | undefined; const ratio = Number(input?.cache_hit_ratio); return Number.isFinite(ratio) ? `${Math.round(ratio * 100)}%` : "—"; }
function isEffort(value: string | null): value is ReasoningEffort { return ["none", "low", "medium", "high", "xhigh", "max"].includes(value || ""); }
