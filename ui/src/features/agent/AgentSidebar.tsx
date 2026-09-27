import { MessageSquarePlus, Plus, Search, Trash2 } from "lucide-react";
import type { Project, Session } from "../../types";

export function AgentSidebar({
  projects,
  sessions,
  projectId,
  sessionId,
  draftSessionOpen,
  onProject,
  onSession,
  onNewSession,
  onDiscardDraft,
  onAddProject,
  onDeleteSession,
}: {
  projects: Project[];
  sessions: Session[];
  projectId?: string;
  sessionId?: string;
  draftSessionOpen: boolean;
  onProject: (project: Project) => void;
  onSession: (sessionId: string) => void;
  onNewSession: () => void;
  onDiscardDraft: () => void;
  onAddProject: () => void;
  onDeleteSession: (sessionId: string) => void;
}) {
  return (
    <div className="sidebar-layout">
      <div className="sidebar-heading">
        <div><span>Agent</span><h2>会话</h2></div>
        <button type="button" className="icon-button" onClick={onNewSession} aria-label="开始新会话" title="开始新会话"><MessageSquarePlus aria-hidden="true" /></button>
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
        <button type="button" className="icon-button" onClick={onAddProject} aria-label="添加项目" title="添加项目"><Plus aria-hidden="true" /></button>
      </div>

      <div className="session-filter"><Search aria-hidden="true" /><span>最近会话</span></div>
      <nav className="session-list" aria-label="最近会话">
        {draftSessionOpen && !sessionId && <div className="session-row active">
          <button type="button" className="session-item" title="新会话草稿" onClick={onNewSession}>
            <strong>新会话</strong><small>尚未发送消息</small>
          </button>
          <button type="button" className="session-delete" aria-label="删除空白会话" title="删除空白会话" onClick={onDiscardDraft}><Trash2 aria-hidden="true" /></button>
        </div>}
        {sessions.map((session) => {
          const question = session.first_question?.trim() || "未命名会话";
          const label = session.display_name?.trim() || shortQuestion(question);
          return (
            <div className={sessionId === session.session_id ? "session-row active" : "session-row"} key={session.session_id}>
              <button type="button" className="session-item" title={question} onClick={() => onSession(session.session_id)}>
                <strong>{label}</strong>
                <small>{session.display_name ? `${shortQuestion(question)} · ` : ""}{formatSessionDate(session.first_question_at || session.created_at)} · {session.message_count} 条记录</small>
              </button>
              <button type="button" className="session-delete" aria-label={`删除会话 ${label}`} title="删除会话" onClick={() => onDeleteSession(session.session_id)}>
                <Trash2 aria-hidden="true" />
              </button>
            </div>
          );
        })}
      </nav>
    </div>
  );
}

function shortQuestion(value: string): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > 44 ? `${compact.slice(0, 43)}…` : compact;
}

function formatSessionDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
