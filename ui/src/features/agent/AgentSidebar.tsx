import { MessageSquarePlus, Plus, Search } from "lucide-react";
import type { Project, Session } from "../../types";

export function AgentSidebar({
  projects, sessions, projectId, sessionId, onProject, onSession, onNewSession, onAddProject,
}: {
  projects: Project[];
  sessions: Session[];
  projectId?: string;
  sessionId?: string;
  onProject: (project: Project) => void;
  onSession: (sessionId: string) => void;
  onNewSession: () => void;
  onAddProject: () => void;
}) {
  return (
    <div className="sidebar-layout">
      <div className="sidebar-heading">
        <div><span>Agent</span><h2>会话</h2></div>
        <button type="button" className="icon-button" onClick={onNewSession} aria-label="开始新会话"><MessageSquarePlus aria-hidden="true" /></button>
      </div>

      <label className="compact-label" htmlFor="project-select">当前项目</label>
      <div className="project-switcher">
        <select id="project-select" value={projectId || ""} onChange={(event) => {
          const project = projects.find((item) => item.project_id === event.target.value);
          if (project) onProject(project);
        }}>
          {!projects.length && <option value="">尚未添加项目</option>}
          {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
        </select>
        <button type="button" className="icon-button" onClick={onAddProject} aria-label="添加项目"><Plus aria-hidden="true" /></button>
      </div>

      <div className="session-filter"><Search aria-hidden="true" /><span>最近会话</span></div>
      <nav className="session-list" aria-label="最近会话">
        <button type="button" className={!sessionId ? "session-item active" : "session-item"} onClick={onNewSession}>
          <strong>新会话</strong><small>从空白上下文开始</small>
        </button>
        {sessions.map((session) => (
          <button
            type="button"
            className={sessionId === session.session_id ? "session-item active" : "session-item"}
            key={session.session_id}
            onClick={() => onSession(session.session_id)}
          >
            <strong>{shortSession(session.session_id)}</strong>
            <small>{formatSessionDate(session.created_at)} · {session.message_count} 条记录</small>
          </button>
        ))}
      </nav>
    </div>
  );
}

function shortSession(value: string): string {
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-5)}` : value;
}

function formatSessionDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
