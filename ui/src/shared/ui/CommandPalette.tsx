import { useEffect, useMemo, useRef, useState, type ComponentType } from "react";
import {
  BookOpen, Bot, Braces, Command, FilePenLine, Moon, Plus,
  Search, Settings2, ShieldCheck, Sun, X,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import type { ThemePreference } from "../../app/ThemeProvider";

type CommandPaletteProps = {
  open: boolean;
  onClose: () => void;
  onNewSession: () => void;
  onAddProject: () => void;
  onTheme: (theme: ThemePreference) => void;
};

type PaletteCommand = {
  id: string;
  label: string;
  detail: string;
  keywords: string;
  icon: ComponentType<{ "aria-hidden"?: boolean }>;
  run: () => void;
};

export function CommandPalette(props: CommandPaletteProps) {
  const navigate = useNavigate();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const commands = useMemo<PaletteCommand[]>(() => [
    { id: "agent", label: "Agent", detail: "对话、工具与 Observer", keywords: "chat 会话 对话", icon: Bot, run: () => navigate("/agent") },
    { id: "code", label: "Code", detail: "独立 Coding Session", keywords: "代码 coding worktree", icon: Braces, run: () => navigate("/code") },
    { id: "read", label: "Read", detail: "论文库与 PDF 阅读", keywords: "paper pdf 论文 阅读", icon: BookOpen, run: () => navigate("/read") },
    { id: "write", label: "Write", detail: "Workspace 文件与 LaTeX", keywords: "文件 编辑 latex workspace", icon: FilePenLine, run: () => navigate("/write") },
    { id: "operations", label: "运维", detail: "Inbox、Cron、Dream 与 Backup", keywords: "operations inbox cron dream backup 备份", icon: Settings2, run: () => navigate("/operations") },
    { id: "capabilities", label: "能力", detail: "Skill、Plugin 与 Runtime Generation", keywords: "capabilities skill plugin extension", icon: ShieldCheck, run: () => navigate("/capabilities") },
    { id: "new-session", label: "开始新会话", detail: "清除当前 Agent Session 选择", keywords: "new session 新建", icon: Plus, run: props.onNewSession },
    { id: "add-project", label: "添加项目", detail: "注册本地工作区", keywords: "project workspace 项目", icon: Plus, run: props.onAddProject },
    { id: "theme-light", label: "浅色主题", detail: "切换当前客户端外观", keywords: "theme light 主题", icon: Sun, run: () => props.onTheme("light") },
    { id: "theme-dark", label: "深色主题", detail: "切换当前客户端外观", keywords: "theme dark 主题", icon: Moon, run: () => props.onTheme("dark") },
  ], [navigate, props.onAddProject, props.onNewSession, props.onTheme]);
  const results = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return commands;
    return commands.filter((item) =>
      `${item.label} ${item.detail} ${item.keywords}`.toLocaleLowerCase().includes(needle),
    );
  }, [commands, query]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (props.open && !dialog.open) {
      dialog.showModal();
      setQuery("");
      setSelected(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
    if (!props.open && dialog.open) dialog.close();
  }, [props.open]);

  function execute(command: PaletteCommand | undefined) {
    if (!command) return;
    command.run();
    props.onClose();
  }

  return (
    <dialog ref={dialogRef} className="command-dialog" onClose={props.onClose}>
      <header>
        <Command aria-hidden="true" />
        <h2>搜索与命令</h2>
        <button type="button" onClick={props.onClose} aria-label="关闭"><X aria-hidden="true" /></button>
      </header>
      <label className="command-search">
        <Search aria-hidden="true" />
        <input
          ref={inputRef}
          type="search"
          value={query}
          placeholder="搜索页面或命令…"
          aria-label="搜索页面或命令"
          onChange={(event) => { setQuery(event.target.value); setSelected(0); }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") { event.preventDefault(); setSelected((value) => Math.min(value + 1, results.length - 1)); }
            if (event.key === "ArrowUp") { event.preventDefault(); setSelected((value) => Math.max(value - 1, 0)); }
            if (event.key === "Enter") { event.preventDefault(); execute(results[selected]); }
          }}
        />
      </label>
      <div className="command-list" role="listbox" aria-label="搜索结果">
        {results.map((item, index) => {
          const Icon = item.icon;
          return <button
            type="button"
            role="option"
            aria-selected={index === selected}
            className={index === selected ? "selected" : ""}
            key={item.id}
            onMouseEnter={() => setSelected(index)}
            onClick={() => execute(item)}
          ><Icon aria-hidden={true} /><span><b>{item.label}</b><small>{item.detail}</small></span></button>;
        })}
        {!results.length && <p className="command-empty">没有匹配的页面或命令。</p>}
      </div>
    </dialog>
  );
}
