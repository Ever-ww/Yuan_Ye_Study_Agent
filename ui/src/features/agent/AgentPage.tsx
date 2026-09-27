import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CloudOff, PlugZap } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { WorkbenchShell } from "../../app/WorkbenchShell";
import { useTheme } from "../../app/ThemeProvider";
import { terminalEventTypes } from "../../events";
import type { GatewayEvent, ObserverStatus, PendingApproval, Project, ReasoningEffort, RunCreate, Session, TemporaryAttachment } from "../../types";
import { useGatewayApi } from "../../shared/api/context";
import { CommandPalette } from "../../shared/ui/CommandPalette";
import { ConfirmDialog } from "../../shared/ui/ConfirmDialog";
import { ProjectDialog } from "../../shared/ui/ProjectDialog";
import { StatusMark } from "../../shared/ui/StatusMark";
import { AgentComposer } from "./AgentComposer";
import { AgentSidebar } from "./AgentSidebar";
import { AgentTimeline } from "./AgentTimeline";
import { ModelControls } from "./ModelControls";
import { ObserverInspector } from "./ObserverInspector";
import { useRunStream } from "./useRunStream";
import { latestModelCallsFromRecords, modelCallsFromEvents, summarizeModelUsage } from "./agentUsage";

export function AgentPage() {
  const api = useGatewayApi();
  const queryClient = useQueryClient();
  const { setPreference } = useTheme();
  const [searchParams, setSearchParams] = useSearchParams();
  const projectId = searchParams.get("project") || window.localStorage.getItem("yyagent.web.selected-project") || undefined;
  const sessionId = searchParams.get("session") || undefined;
  const [runId, setRunId] = useState<string | null>(null);
  const [runSessionId, setRunSessionId] = useState<string | null>(null);
  const activeRunRef = useRef<string | null>(null);
  const ignoredRunsRef = useRef(new Set<string>());
  const [displayRunId, setDisplayRunId] = useState<string | null>(null);
  const [optimisticQuestion, setOptimisticQuestion] = useState("");
  const [task, setTask] = useState("");
  const [uiContext, setUiContext] = useState<RunCreate["uiContext"]>({ source: "agent" });
  const [attachments, setAttachments] = useState<TemporaryAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [observer, setObserver] = useState<ObserverStatus | null>(null);
  const [handledApprovals, setHandledApprovals] = useState(() => new Set<string>());
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [commandsOpen, setCommandsOpen] = useState(false);
  const [draftSessionOpen, setDraftSessionOpen] = useState(false);
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null);
  const [deletingSessionId, setDeletingSessionId] = useState<string | null>(null);
  const [profileId, setProfileId] = useState("default");
  const [effort, setEffortState] = useState<ReasoningEffort>(() => {
    const saved = window.localStorage.getItem("yyagent.web.reasoning-effort");
    return isEffort(saved) ? saved : "low";
  });

  const statusQuery = useQuery({ queryKey: ["status"], queryFn: () => api.status(), refetchInterval: 15_000 });
  const projectsQuery = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const selectedProject = projectsQuery.data?.find((item) => item.project_id === projectId) || projectsQuery.data?.[0];
  const sessionsQuery = useQuery({
    queryKey: ["sessions", selectedProject?.project_id],
    queryFn: () => api.sessions(selectedProject!.project_id),
    enabled: Boolean(selectedProject),
  });
  const historyQuery = useQuery({
    queryKey: ["session", selectedProject?.project_id, sessionId],
    queryFn: () => api.session(selectedProject!.project_id, sessionId as string),
    enabled: Boolean(selectedProject && sessionId),
  });
  const modelsQuery = useQuery({
    queryKey: ["models", selectedProject?.project_id, sessionId],
    queryFn: () => api.models(selectedProject?.project_id, sessionId),
  });
  const approvalsQuery = useQuery({
    queryKey: ["approvals", selectedProject?.project_id],
    queryFn: () => api.approvals(selectedProject?.project_id),
    refetchInterval: runId ? 2_000 : 10_000,
  });

  useEffect(() => {
    if (!projectId && selectedProject) setSearchParams({ project: selectedProject.project_id }, { replace: true });
  }, [projectId, selectedProject, setSearchParams]);

  useEffect(() => {
    const selected = modelsQuery.data?.find((item) => item.selected) || modelsQuery.data?.[0];
    if (selected && !modelsQuery.data?.some((item) => item.profile_id === profileId)) setProfileId(selected.profile_id);
  }, [modelsQuery.data, profileId]);

  const terminalCallback = useCallback(async (event: GatewayEvent) => {
    if (!event.run_id) return;
    if (ignoredRunsRef.current.has(event.run_id)) return;
    if (activeRunRef.current && activeRunRef.current !== event.run_id) return;
    activeRunRef.current = null;
    setRunId(null);
    setRunSessionId(null);
    if (event.session_id && selectedProject) {
      setDraftSessionOpen(false);
      setSearchParams({ project: selectedProject.project_id, session: event.session_id }, { replace: true });
    }
    try { setObserver(await api.observerStatus(event.run_id)); } catch { setObserver(null); }
    try { await api.acknowledgeRunResult(event.run_id); } catch { /* Receipt failure is non-fatal. */ }
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["sessions", selectedProject?.project_id] }),
      queryClient.invalidateQueries({ queryKey: ["session", selectedProject?.project_id] }),
      queryClient.invalidateQueries({ queryKey: ["approvals", selectedProject?.project_id] }),
    ]);
    void notifyDesktop(event.type === "run_completed" ? "任务已完成" : "任务已结束", String(event.payload.answer || event.payload.message || selectedProject?.name || "YYAgent"));
  }, [api, queryClient, selectedProject, setSearchParams]);

  const stream = useRunStream(api, runId, terminalCallback);

  useEffect(() => {
    const terminal = [...stream.events].reverse().find((event) => terminalEventTypes.has(event.type));
    if (!terminal?.run_id || observer?.run_id === terminal.run_id) return;
    api.observerStatus(terminal.run_id).then(setObserver).catch(() => undefined);
  }, [api, observer?.run_id, stream.events]);

  useEffect(() => {
    // Observer state is durable, but the React state is not. On refresh or
    // session navigation, restore the most recent Run's Observer projection
    // from the Session history instead of starting with an empty inspector.
    if (runId || displayRunId || !selectedProject || !sessionId || !historyQuery.data?.length) return;
    const latestRunId = [...historyQuery.data].reverse().find((record) => record.run_id)?.run_id;
    if (!latestRunId || observer?.run_id === latestRunId) return;
    api.observerStatus(latestRunId).then((value) => {
      setObserver((current) => current?.run_id === latestRunId ? current : value);
    }).catch(() => undefined);
  }, [api, displayRunId, historyQuery.data, observer?.run_id, runId, selectedProject, sessionId]);

  useEffect(() => {
    const key = draftKey(selectedProject?.project_id, sessionId);
    const prefill = window.sessionStorage.getItem("yyagent.web.agent-prefill");
    if (prefill) {
      window.sessionStorage.removeItem("yyagent.web.agent-prefill");
      setTask(prefill);
      const rawContext = window.sessionStorage.getItem("yyagent.web.agent-context");
      window.sessionStorage.removeItem("yyagent.web.agent-context");
      try { setUiContext(rawContext ? JSON.parse(rawContext) as RunCreate["uiContext"] : { source: "agent" }); }
      catch { setUiContext({ source: "agent" }); }
    } else {
      setTask(window.sessionStorage.getItem(key) || "");
      setUiContext({ source: "agent" });
    }
  }, [selectedProject?.project_id, sessionId]);

  useEffect(() => {
    window.sessionStorage.setItem(draftKey(selectedProject?.project_id, sessionId), task);
  }, [selectedProject?.project_id, sessionId, task]);

  useEffect(() => {
    if (!selectedProject || !sessionId || !historyQuery.data?.length) return;
    api.acknowledgeSessionHistory(selectedProject.project_id, sessionId, historyQuery.data).catch(() => undefined);
  }, [api, historyQuery.data, selectedProject, sessionId]);

  const eventApproval = useMemo(() => {
    const event = [...stream.events].reverse().find((item) => item.type === "approval_requested" && !handledApprovals.has(String(item.payload.approval_id || "")));
    return event ? approvalFromEvent(event) : null;
  }, [handledApprovals, stream.events]);
  const durableApproval = approvalsQuery.data?.find((item) => item.client_id === api.clientId && !handledApprovals.has(item.approval_id)) || null;
  const approval = eventApproval || durableApproval;

  function setEffort(value: ReasoningEffort) {
    setEffortState(value);
    window.localStorage.setItem("yyagent.web.reasoning-effort", value);
  }

  function selectProject(project: Project) {
    window.localStorage.setItem("yyagent.web.selected-project", project.project_id);
    setDraftSessionOpen(false);
    setSearchParams({ project: project.project_id });
    clearDisplayedRun();
    void api.openProject(project.project_id).then(() => projectsQuery.refetch());
  }

  function selectSession(nextSessionId?: string) {
    if (!selectedProject) return;
    setDraftSessionOpen(false);
    setSearchParams(nextSessionId ? { project: selectedProject.project_id, session: nextSessionId } : { project: selectedProject.project_id });
    clearDisplayedRun();
  }

  function beginNewSession() {
    if (!selectedProject) return;
    window.sessionStorage.removeItem(draftKey(selectedProject.project_id));
    setTask("");
    setAttachments([]);
    selectSession();
    setDraftSessionOpen(true);
  }

  function discardDraftSession() {
    if (runId) return;
    selectSession();
    setTask("");
    setAttachments([]);
    window.sessionStorage.removeItem(draftKey(selectedProject?.project_id));
  }

  function clearDisplayedRun() {
    stream.reset(); setDisplayRunId(null); setObserver(null); setError(""); setOptimisticQuestion(""); setRunSessionId(null);
  }

  async function startQuestion(question: string, targetSessionId?: string) {
    if (!selectedProject || !question.trim()) return;
    setError(""); clearDisplayedRun(); setOptimisticQuestion(question); setTask("");
    try {
      const created = await api.startRun({
        projectId: selectedProject.project_id,
        task: question,
        sessionId: targetSessionId,
        modelProfileId: profileId,
        reasoningEffort: effort,
        uiContext: uiContext?.source === "agent" ? { source: "agent", attachments } : uiContext,
      });
      setUiContext({ source: "agent" });
      setAttachments([]);
      setDisplayRunId(created.run_id);
      setRunId(created.run_id);
      activeRunRef.current = created.run_id;
      setRunSessionId(created.session_id || targetSessionId || null);
    } catch (reason) {
      setOptimisticQuestion("");
      setTask(question);
      setError(errorMessage(reason));
    }
  }

  async function send() {
    if (!selectedProject || !task.trim()) return;
    const question = task.trim();
    const targetSessionId = runSessionId || sessionId;
    if (runId) {
      try {
        ignoredRunsRef.current.add(runId);
        activeRunRef.current = null;
        await api.cancelRun(runId);
        setRunId(null);
      } catch (reason) {
        setError(errorMessage(reason));
        return;
      }
    }
    await startQuestion(question, targetSessionId);
  }

  async function editLastQuestion(question: string) {
    if (!selectedProject || !sessionId || runId) return;
    try {
      await api.replaceLastTurn(selectedProject.project_id, sessionId);
      await queryClient.invalidateQueries({ queryKey: ["session", selectedProject.project_id, sessionId] });
      await startQuestion(question, sessionId);
    } catch (reason) { setError(errorMessage(reason)); }
  }

  async function branchToNewChat(cutoffRecordId: string | null) {
    if (!selectedProject || !sessionId) return;
    try {
      const branch = await api.branchSession(selectedProject.project_id, sessionId, cutoffRecordId);
      await sessionsQuery.refetch();
      setSearchParams({ project: selectedProject.project_id, session: branch.session_id });
      clearDisplayedRun();
    } catch (reason) { setError(errorMessage(reason)); }
  }

  async function uploadAttachments(files: File[]) {
    if (!selectedProject || !files.length) return;
    setUploading(true); setError("");
    try {
      for (const file of files) {
        const uploaded = await api.uploadAgentAttachment(selectedProject.project_id, file);
        setAttachments((current) => current.some((item) => item.content_hash === uploaded.content_hash)
          ? current : [...current, uploaded]);
      }
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setUploading(false); }
  }

  async function decideApproval(approvalId: string, approved: boolean) {
    try {
      await api.respondApproval(approvalId, approved);
      setHandledApprovals((current) => new Set(current).add(approvalId));
      await approvalsQuery.refetch();
    } catch (reason) { setError(errorMessage(reason)); }
  }

  async function addProject(path: string, name: string) {
    const project = await api.registerProject(path, name || undefined);
    await projectsQuery.refetch();
    selectProject(project);
  }

  function beginAddProject() {
    setProjectDialogOpen(true);
  }

  function requestDeleteSession(targetSessionId: string) {
    const session = sessionsQuery.data?.find((item) => item.session_id === targetSessionId);
    if (session) setDeleteTarget(session);
  }

  async function deleteSession() {
    if (!selectedProject || !deleteTarget) return;
    const target = deleteTarget;
    setDeletingSessionId(target.session_id);
    try {
      await api.deleteSession(selectedProject.project_id, target.session_id);
      if (sessionId === target.session_id) selectSession();
      await sessionsQuery.refetch();
      setDeleteTarget(null);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setDeletingSessionId(null);
    }
  }

  const visibleHistory = (historyQuery.data || []).filter((record) => !displayRunId || record.run_id !== displayRunId);
  const modelUsage = useMemo(() => summarizeModelUsage(
    stream.events.length
      ? modelCallsFromEvents(stream.events)
      : latestModelCallsFromRecords(historyQuery.data || []),
  ), [historyQuery.data, stream.events]);
  const status = statusQuery.data;

  return (
    <>
      <WorkbenchShell
        projectName={selectedProject?.name || "选择项目"}
        inspectorOpen={inspectorOpen}
        onToggleInspector={() => setInspectorOpen((value) => !value)}
        onOpenCommands={() => setCommandsOpen(true)}
        modelControls={<ModelControls models={modelsQuery.data || []} profileId={profileId} effort={effort} disabled={Boolean(runId)} onProfile={setProfileId} onEffort={setEffort} />}
        sidebar={<AgentSidebar projects={projectsQuery.data || []} sessions={sessionsQuery.data || []} projectId={selectedProject?.project_id} sessionId={sessionId} draftSessionOpen={draftSessionOpen} onProject={selectProject} onSession={selectSession} onNewSession={beginNewSession} onDiscardDraft={discardDraftSession} onAddProject={beginAddProject} onDeleteSession={requestDeleteSession} />}
        inspector={<ObserverInspector events={stream.events} observer={observer} />}
        footer={<><StatusMark state={status ? "ok" : "idle"} label={status ? "Gateway 已连接" : "正在连接 Gateway"} /><StatusMark state={status?.bash_available ? "ok" : status ? "warning" : "idle"} label={sandboxLabel(status?.sandbox_mode)} /><StatusMark state={stream.connection === "reconnecting" ? "warning" : "ok"} label={stream.connection === "reconnecting" ? "事件流重连中" : "事件流就绪"} /><span className="status-spacer" /><span className="generation-state"><PlugZap aria-hidden="true" />Local runtime</span></>}
      >
        <AgentTimeline
          history={visibleHistory}
          events={stream.events}
          optimisticQuestion={optimisticQuestion}
          running={Boolean(runId)}
          onEditLastQuestion={(question) => void editLastQuestion(question)}
          onBranch={(recordId) => void branchToNewChat(recordId)}
          onLoadToolResult={async (recordId) => {
            if (!selectedProject || !sessionId) return "无法定位 Session。";
            const value = await api.toolResult(selectedProject.project_id, sessionId, recordId);
            return typeof value.content === "string" ? value.content : JSON.stringify(value, null, 2);
          }}
        />
        <AgentComposer value={task} disabled={!selectedProject} running={Boolean(runId)} approval={approval} error={error || queryError(projectsQuery.error || statusQuery.error)} attachments={attachments} uploading={uploading} usage={modelUsage} onChange={setTask} onSend={() => void send()} onCancel={() => runId && void api.cancelRun(runId).catch((reason) => setError(errorMessage(reason)))} onApproval={(id, approved) => void decideApproval(id, approved)} onFiles={(files) => void uploadAttachments(files)} onRemoveAttachment={(id) => setAttachments((current) => current.filter((item) => item.attachment_id !== id))} />
        {stream.connection === "reconnecting" && <div className="connection-banner"><CloudOff aria-hidden="true" />连接暂时中断。任务继续在 Gateway 运行，正在补齐事件。</div>}
      </WorkbenchShell>
      <CommandPalette open={commandsOpen} onClose={() => setCommandsOpen(false)} onNewSession={beginNewSession} onAddProject={() => void beginAddProject()} onTheme={setPreference} />
      <ProjectDialog open={projectDialogOpen} onClose={() => setProjectDialogOpen(false)} onSubmit={addProject} />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除会话"
        message={deleteTarget ? `确定删除“${(deleteTarget.first_question || "未命名会话").trim().slice(0, 80)}”吗？会话记录将从当前 Workspace 中永久删除。` : ""}
        confirmLabel="删除会话"
        busy={Boolean(deletingSessionId)}
        onClose={() => { if (!deletingSessionId) setDeleteTarget(null); }}
        onConfirm={() => void deleteSession()}
      />
    </>
  );
}

function isEffort(value: string | null): value is ReasoningEffort {
  return ["none", "low", "medium", "high", "xhigh", "max"].includes(value || "");
}

function draftKey(projectId?: string, sessionId?: string) { return `yyagent.web.draft.${projectId || "none"}.${sessionId || "new"}`; }
function errorMessage(reason: unknown) { return reason instanceof Error ? reason.message : String(reason); }
function queryError(reason: Error | null) { return reason ? reason.message : ""; }
function sandboxLabel(mode?: string) {
  if (!mode) return "Sandbox 检测中";
  if (mode === "os" || mode === "os_lazy") return "OS Sandbox";
  if (mode === "docker") return "Docker Sandbox";
  return "Checkpoint only";
}

function approvalFromEvent(event: GatewayEvent): PendingApproval {
  return {
    approval_id: String(event.payload.approval_id || ""),
    run_id: event.run_id || String(event.payload.run_id || ""),
    client_id: String(event.payload.client_id || ""),
    tool_name: String(event.payload.tool_name || "unknown"),
    arguments: typeof event.payload.arguments === "object" && event.payload.arguments ? event.payload.arguments as Record<string, unknown> : {},
    state: "pending",
    created_at: String(event.payload.created_at || event.timestamp),
    expires_at: String(event.payload.expires_at || ""),
  };
}

async function notifyDesktop(title: string, body: string) {
  if (!("__TAURI_INTERNALS__" in window)) return;
  try {
    const { isPermissionGranted, requestPermission, sendNotification } = await import("@tauri-apps/plugin-notification");
    let granted = await isPermissionGranted();
    if (!granted) granted = (await requestPermission()) === "granted";
    if (granted) sendNotification({ title, body: body.slice(0, 240) });
  } catch { /* Desktop notification is an optional enhancement. */ }
}
