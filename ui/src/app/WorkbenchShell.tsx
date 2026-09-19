import { useEffect, type ReactNode } from "react";
import { BookOpen, Bot, Braces, Command, FilePenLine, PanelRight, Search, Settings2 } from "lucide-react";
import { NavLink, useLocation } from "react-router-dom";

type WorkbenchShellProps = {
  projectName: string;
  modelControls: ReactNode;
  sidebar: ReactNode;
  children: ReactNode;
  inspector: ReactNode;
  footer: ReactNode;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  onOpenCommands: () => void;
  sidebarLabel?: string;
  inspectorLabel?: string;
};

export function WorkbenchShell(props: WorkbenchShellProps) {
  const location = useLocation();
  useEffect(() => {
    document.getElementById("main-content")?.focus({ preventScroll: true });
  }, [location.pathname]);
  useEffect(() => {
    const openCommands = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        props.onOpenCommands();
      }
    };
    window.addEventListener("keydown", openCommands);
    return () => window.removeEventListener("keydown", openCommands);
  }, [props.onOpenCommands]);
  return (
    <div className={`workbench ${props.inspectorOpen ? "inspector-visible" : ""}`}>
      <a className="skip-link" href="#main-content">跳到主内容</a>
      <header className="topbar">
        <NavLink className="wordmark" to="/agent" aria-label="YYAgent Agent 工作台">
          <span className="wordmark-glyph">YY</span>
          <span>YYAgent</span>
        </NavLink>
        <div className="project-title" title={props.projectName}>{props.projectName}</div>
        <div className="model-controls">{props.modelControls}</div>
        <button className="topbar-action search-action" type="button" onClick={props.onOpenCommands}>
          <Search aria-hidden="true" /><span>搜索与命令</span><kbd>Ctrl K</kbd>
        </button>
        <button
          className="icon-button inspector-toggle"
          type="button"
          aria-label={props.inspectorOpen ? "关闭检查面板" : "打开检查面板"}
          aria-expanded={props.inspectorOpen}
          onClick={props.onToggleInspector}
        >
          <PanelRight aria-hidden="true" />
        </button>
      </header>

      <nav className="primary-rail" aria-label="工作台模式">
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to="/agent">
          <Bot aria-hidden="true" /><span>Agent</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to="/code">
          <Braces aria-hidden="true" /><span>Code</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to="/read">
          <BookOpen aria-hidden="true" /><span>Read</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to="/write">
          <FilePenLine aria-hidden="true" /><span>Write</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to="/operations">
          <Settings2 aria-hidden="true" /><span>运维</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to="/capabilities">
          <Command aria-hidden="true" /><span>能力</span>
        </NavLink>
        <button className="rail-command" type="button" onClick={props.onOpenCommands}>
          <Command aria-hidden="true" /><span>命令</span>
        </button>
      </nav>

      <aside className="context-sidebar" aria-label={props.sidebarLabel || "Agent 会话"}>{props.sidebar}</aside>
      <main className="main-workspace" id="main-content" tabIndex={-1}>{props.children}</main>
      <aside className="inspector" aria-label={props.inspectorLabel || "Turn Observer"}>{props.inspector}</aside>
      <footer className="statusbar">{props.footer}</footer>
    </div>
  );
}
