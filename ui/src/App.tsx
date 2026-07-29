import { useEffect, useMemo, useState } from "react";
import { GatewayApi } from "./api";
import { mergeGatewayEvents } from "./events";
import type { GatewayEvent, GatewayStatus, InboxItem, Project, Session, SessionRecord } from "./types";
import "./styles.css";

const terminalEvents = new Set(["run_completed", "run_failed", "run_cancelled", "run_interrupted"]);

export default function App() {
  const api = useMemo(() => new GatewayApi(), []);
  const [status, setStatus] = useState<GatewayStatus | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [history, setHistory] = useState<SessionRecord[]>([]);
  const [inbox, setInbox] = useState<InboxItem[]>([]);
  const [events, setEvents] = useState<GatewayEvent[]>([]);
  const [task, setTask] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [handledApprovals, setHandledApprovals] = useState<Set<string>>(new Set());

  async function refresh() {
    const [nextStatus, nextProjects, nextInbox] = await Promise.all([
      api.status(), api.projects(), api.inbox()
    ]);
    setStatus(nextStatus);
    setProjects(nextProjects);
    setInbox(nextInbox);
    const active = project
      ? nextProjects.find((item) => item.project_id === project.project_id) || null
      : nextProjects[0] || null;
    setProject(active);
    if (active) setSessions(await api.sessions(active.project_id));
  }

  useEffect(() => {
    api.initialize().then(refresh).catch((reason) => setError(String(reason)));
  }, []);

  async function addProject() {
    try {
      let path = "";
      if ("__TAURI_INTERNALS__" in window) {
        const { open } = await import("@tauri-apps/plugin-dialog");
        const selected = await open({ directory: true, multiple: false });
        if (typeof selected === "string") path = selected;
      } else {
        path = prompt("输入 workspace 的绝对路径") || "";
      }
      if (!path) return;
      const created = await api.registerProject(path);
      await refresh();
      setProject(created);
      setSessions(await api.sessions(created.project_id));
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function send() {
    if (!project || !task.trim() || runId) return;
    setError("");
    setEvents([]);
    setHandledApprovals(new Set());
    const submitted = task.trim();
    setHistory((current) => [
      ...current,
      { role: "user", content: submitted, timestamp: new Date().toISOString() }
    ]);
    const created = await api.startRun(project.project_id, submitted, sessionId);
    setRunId(created.run_id);
    setTask("");
    let lastSequence = 0;
    let terminal = false;
    const connect = () => {
      const socket = api.subscribe(created.run_id, lastSequence, async (event) => {
        if (event.sequence <= lastSequence) return;
        lastSequence = event.sequence;
        setEvents((current) => mergeGatewayEvents(current, event));
        if (event.session_id) setSessionId(event.session_id);
        if (terminalEvents.has(event.type)) {
          terminal = true;
          socket.close();
          setRunId(null);
          await notifyDesktop(
            event.type === "run_completed" ? "任务已完成" : "任务已结束",
            String(event.payload.answer || event.payload.message || project.name)
          );
          await refresh();
        }
      });
      socket.onerror = () => setError("事件连接已断开，正在重连；运行仍会在 Gateway 后台继续");
      socket.onopen = () => setError("");
      socket.onclose = () => {
        if (!terminal) window.setTimeout(connect, 1000);
      };
    };
    connect();
  }

  const approval = [...events].reverse().find((item) =>
    item.type === "approval_requested"
    && !handledApprovals.has(String(item.payload.approval_id))
  );

  async function decideApproval(approvalId: string, approved: boolean) {
    try {
      await api.respondApproval(approvalId, approved);
      setHandledApprovals((current) => new Set(current).add(approvalId));
    } catch (reason) {
      setError(String(reason));
    }
  }

  return (
    <main className="shell">
      <aside className="projects">
        <div className="brand"><span>YY</span><div>Yuan Ye<small>Agent Gateway</small></div></div>
        <button className="new-project" onClick={addProject}>＋ 添加项目</button>
        <nav>
          {projects.map((item) => (
            <button
              className={project?.project_id === item.project_id ? "active" : ""}
              key={item.project_id}
              onClick={async () => {
                setProject(item); setSessionId(undefined); setHistory([]);
                setSessions(await api.sessions(item.project_id));
              }}
            >
              <strong>{item.name}</strong><small>{item.path}</small>
            </button>
          ))}
        </nav>
        <div className="model">
          <i className={status ? "online" : ""} />
          <div>{status?.model || "连接中"}<small>{status?.provider} · {status?.stream ? "流式" : "非流式"}</small></div>
        </div>
      </aside>

      <aside className="threads">
        <header><h2>任务线程</h2><button onClick={() => { setSessionId(undefined); setHistory([]); setEvents([]); }}>＋</button></header>
        {sessions.map((item) => (
          <button
            className={sessionId === item.session_id ? "active" : ""}
            key={item.session_id}
            onClick={async () => {
              setSessionId(item.session_id || undefined);
              setHistory(await api.session(String(project?.project_id), item.session_id));
              setEvents([]);
            }}
          >
            <strong>{item.session_id}</strong>
            <small>{item.created_at} · {item.message_count} 条</small>
          </button>
        ))}
        <h3>Inbox <span>{inbox.filter((item) => !item.read).length}</span></h3>
        {inbox.slice(0, 8).map((item) => (
          <button className={`inbox ${item.read ? "" : "unread"}`} key={item.item_id}
            onClick={async () => {
              await api.markRead(item.item_id);
              setSessionId(item.session_id);
              if (item.session_id) setHistory(await api.session(item.project_id, item.session_id));
              await refresh();
            }}>
            <strong>{item.title}</strong><small>{item.status} · {item.created_at}</small>
          </button>
        ))}
      </aside>

      <section className="workspace">
        <header>
          <div><h1>{project?.name || "选择一个项目"}</h1><p>{sessionId || "新任务"}</p></div>
          <div className="pills"><span>Local</span><span>{status?.sandbox ? "Sandbox" : "No sandbox"}</span></div>
        </header>
        <div className="timeline">
          {history.length === 0 && events.length === 0 && <div className="empty"><b>开始一项工作</b><p>提出问题、研究主题或让 Agent 在 workspace 中完成任务。</p></div>}
          {history.map((record, index) => (
            <article className={`history ${record.role}`} key={`${record.timestamp || ""}-${index}`}>
              <span>{record.role}</span>
              <p>{record.content ?? (record.tool_calls ? JSON.stringify(record.tool_calls, null, 2) : "")}</p>
              {record.timestamp && <time>{record.timestamp}</time>}
            </article>
          ))}
          {events.map((event) => <EventCard event={event} key={event.event_id} />)}
        </div>
        {approval && runId && (
          <div className="approval">
            <div><b>需要批准：{String(approval.payload.tool_name)}</b><pre>{JSON.stringify(approval.payload.arguments, null, 2)}</pre></div>
            <button onClick={() => void decideApproval(String(approval.payload.approval_id), false)}>拒绝</button>
            <button className="allow" onClick={() => void decideApproval(String(approval.payload.approval_id), true)}>允许</button>
          </div>
        )}
        {error && <div className="error">{error}</div>}
        <div className="composer">
          <textarea value={task} onChange={(event) => setTask(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }}
            placeholder={project ? "告诉 Yuan Ye Agent 要做什么…" : "请先添加项目"} disabled={!project || Boolean(runId)} />
          {runId
            ? <button className="stop" onClick={() => api.cancelRun(runId)}>■</button>
            : <button onClick={send} disabled={!project || !task.trim()}>↑</button>}
        </div>
      </section>
    </main>
  );
}

async function notifyDesktop(title: string, body: string) {
  if (!("__TAURI_INTERNALS__" in window)) return;
  try {
    const { isPermissionGranted, requestPermission, sendNotification } =
      await import("@tauri-apps/plugin-notification");
    let granted = await isPermissionGranted();
    if (!granted) granted = (await requestPermission()) === "granted";
    if (granted) sendNotification({ title, body: body.slice(0, 240) });
  } catch {
    // 通知不可用不影响 Run 结果与 Inbox。
  }
}

function EventCard({ event }: { event: GatewayEvent }) {
  if (event.type === "text") return <article className="answer">{String(event.payload.content || "")}</article>;
  if (event.type === "approval_requested") return null;
  const labels: Record<string, string> = {
    run_queued: "已排队", run_started: "开始运行", tool_requested: "工具请求",
    tool_completed: "工具完成", model_retry: "网络重试", model_reconnected: "网络恢复",
    compression_started: "上下文压缩", context_compressed: "压缩完成",
    run_completed: "运行完成", run_failed: "运行失败", run_cancelled: "已取消",
    run_interrupted: "Gateway 异常中断"
  };
  return (
    <article className={`event ${event.type}`}>
      <span>{labels[event.type] || event.type}</span>
      <p>{String(event.payload.message || event.payload.name || event.payload.answer || "")}</p>
      <time>{new Date(event.timestamp).toLocaleTimeString()}</time>
    </article>
  );
}
