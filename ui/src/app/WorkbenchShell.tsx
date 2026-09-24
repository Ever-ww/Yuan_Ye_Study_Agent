import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { BookOpen, Bot, Braces, Command, FilePenLine, NotebookPen, PanelLeftClose, PanelLeftOpen, PanelRight, Search, Settings2 } from "lucide-react";
import { NavLink, useLocation } from "react-router-dom";

type WorkbenchShellProps = {
  projectName: string;
  modelControls: ReactNode;
  sidebar: ReactNode;
  children: ReactNode;
  inspector: ReactNode;
  inspectorEdgeControl?: ReactNode;
  footer: ReactNode;
  inspectorOpen: boolean;
  inspectorWidth?: number;
  onInspectorWidthChange?: (width: number) => void;
  showInspectorToggle?: boolean;
  sidebarOpen?: boolean;
  onToggleSidebar?: () => void;
  onToggleInspector: () => void;
  onOpenCommands: () => void;
  sidebarLabel?: string;
  inspectorLabel?: string;
};

export function WorkbenchShell(props: WorkbenchShellProps) {
  const location = useLocation();
  const selectedProject = new URLSearchParams(location.search).get("project");
  const modeTarget = (pathname: string) => ({
    pathname,
    search: selectedProject ? `?project=${encodeURIComponent(selectedProject)}` : "",
  });
  const [localSidebarOpen, setLocalSidebarOpen] = useState(
    () => window.localStorage.getItem("yyagent.web.sidebar") !== "closed",
  );
  const resizingInspector = useRef(false);
  const sidebarOpen = props.sidebarOpen ?? localSidebarOpen;
  const toggleSidebar = () => {
    if (props.onToggleSidebar) {
      props.onToggleSidebar();
      return;
    }
    setLocalSidebarOpen((value) => {
      window.localStorage.setItem("yyagent.web.sidebar", value ? "closed" : "open");
      return !value;
    });
  };
  useEffect(() => {
    const move = (event: PointerEvent) => {
      if (!resizingInspector.current || !props.onInspectorWidthChange) return;
      const width = Math.max(248, Math.min(560, window.innerWidth - event.clientX));
      props.onInspectorWidthChange(width);
    };
    const stop = () => {
      resizingInspector.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
  }, [props.onInspectorWidthChange]);
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
    <div className={`workbench ${props.inspectorOpen ? "inspector-visible" : ""} ${sidebarOpen ? "" : "sidebar-hidden"}`} style={{ "--inspector-width": `${props.inspectorWidth || 304}px` } as CSSProperties}>
      <a className="skip-link" href="#main-content">跳到主内容</a>
      <header className="topbar">
        <NavLink className="wordmark" to={modeTarget("/agent")} aria-label="YYAgent Agent 工作台">
          <span className="wordmark-glyph">YY</span>
          <span>YYAgent</span>
        </NavLink>
        <div className="project-title" title={props.projectName}>{props.projectName}</div>
        <div className="model-controls">{props.modelControls}</div>
        <button className="topbar-action search-action" type="button" onClick={props.onOpenCommands}>
          <Search aria-hidden="true" /><span>搜索与命令</span><kbd>Ctrl K</kbd>
        </button>
        {props.showInspectorToggle !== false && <button
          className="icon-button inspector-toggle"
          type="button"
          aria-label={props.inspectorOpen ? `关闭${props.inspectorLabel || "检查面板"}` : `打开${props.inspectorLabel || "检查面板"}`}
          title={props.inspectorOpen ? `关闭${props.inspectorLabel || "检查面板"}` : `打开${props.inspectorLabel || "检查面板"}`}
          aria-expanded={props.inspectorOpen}
          onClick={props.onToggleInspector}
        >
          <PanelRight aria-hidden="true" />
        </button>}
      </header>

      <nav className="primary-rail" aria-label="工作台模式">
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to={modeTarget("/agent")}>
          <Bot aria-hidden="true" /><span>Agent</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to={modeTarget("/code")}>
          <Braces aria-hidden="true" /><span>Code</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to={modeTarget("/read")}>
          <BookOpen aria-hidden="true" /><span>Read</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to={modeTarget("/write")}>
          <FilePenLine aria-hidden="true" /><span>Write</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to={modeTarget("/note")}>
          <NotebookPen aria-hidden="true" /><span>Note</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to={modeTarget("/operations")}>
          <Settings2 aria-hidden="true" /><span>运维</span>
        </NavLink>
        <NavLink className={({ isActive }) => `rail-item${isActive ? " active" : ""}`} to={modeTarget("/capabilities")}>
          <Command aria-hidden="true" /><span>能力</span>
        </NavLink>
        <button className="rail-command" type="button" onClick={props.onOpenCommands}>
          <Command aria-hidden="true" /><span>命令</span>
        </button>
      </nav>

      <aside className="context-sidebar" aria-label={props.sidebarLabel || "Agent 会话"}>
        {sidebarOpen && <button className="context-sidebar-toggle" type="button" aria-label="收起侧边栏" title="收起侧边栏" aria-expanded="true" onClick={toggleSidebar}><PanelLeftClose aria-hidden="true" /></button>}
        {props.sidebar}
      </aside>
      {!sidebarOpen && <button className="sidebar-reopen" type="button" aria-label="展开侧边栏" title="展开侧边栏" onClick={toggleSidebar}><PanelLeftOpen aria-hidden="true" /></button>}
      <main className="main-workspace" id="main-content" tabIndex={-1}>{props.children}</main>
      {props.inspectorOpen && props.onInspectorWidthChange && <div className="inspector-resizer" role="separator" aria-label="调整右侧面板宽度" aria-orientation="vertical" onPointerDown={(event) => { event.preventDefault(); resizingInspector.current = true; document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none"; }} />}
      {props.inspectorEdgeControl && <div className="inspector-edge-control">{props.inspectorEdgeControl}</div>}
      <aside className="inspector" aria-label={props.inspectorLabel || "Turn Observer"}>{props.inspector}</aside>
      <footer className="statusbar">{props.footer}</footer>
    </div>
  );
}
