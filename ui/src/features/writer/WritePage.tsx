import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FilePlus2, FileText, FolderPlus, GitCompare, Play, Save, Send, Square, Trash2, X } from "lucide-react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { useNavigate, useSearchParams } from "react-router-dom";
import { GatewayRequestError } from "../../api";
import { WorkbenchShell } from "../../app/WorkbenchShell";
import { useTheme } from "../../app/ThemeProvider";
import { useGatewayApi } from "../../shared/api/context";
import { CommandPalette } from "../../shared/ui/CommandPalette";
import { StatusMark } from "../../shared/ui/StatusMark";
import type { LatexCompilation, LatexDiagnostic, WorkspaceDocument, WorkspaceEntry } from "../../types";
import { CodeEditor } from "./CodeEditor";
import { WorkspaceTree } from "./WorkspaceTree";

type OpenDocument = WorkspaceDocument & { draft: string; dirty: boolean };

export function WritePage() {
  const api = useGatewayApi(); const navigate = useNavigate(); const queryClient = useQueryClient(); const { setPreference } = useTheme();
  const [params, setParams] = useSearchParams();
  const [commandsOpen, setCommandsOpen] = useState(false); const [inspectorOpen, setInspectorOpen] = useState(true);
  const [documents, setDocuments] = useState<OpenDocument[]>([]); const [activePath, setActivePath] = useState("");
  const [selected, setSelected] = useState<WorkspaceEntry | null>(null); const [treeRevision, setTreeRevision] = useState(0);
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState(""); const [conflict, setConflict] = useState<OpenDocument | null>(null);
  const [entryName, setEntryName] = useState(""); const [entryKind, setEntryKind] = useState<"file" | "directory">("file");
  const [movePath, setMovePath] = useState("");
  const compilationId = params.get("compilation") || ""; const [pdfUrl, setPdfUrl] = useState(""); const [compileLog, setCompileLog] = useState("");
  const [focusLine, setFocusLine] = useState<{ line: number; token: number }>();
  const workspaceCursor = useRef(0);
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const projectId = params.get("project") || projects.data?.[0]?.project_id;
  const filePath = params.get("file") || "";
  const changes = useQuery({ queryKey: ["workspace", "changes", projectId, treeRevision], queryFn: () => api.workspaceChanges(projectId!), enabled: Boolean(projectId) });
  const compilation = useQuery({
    queryKey: ["latex", projectId, compilationId],
    queryFn: () => api.latexCompilation(projectId!, compilationId),
    enabled: Boolean(projectId && compilationId),
    refetchInterval: (query) => ["queued", "running"].includes(query.state.data?.status || "") ? 1000 : false,
  });
  const active = documents.find((item) => item.path === activePath);

  useEffect(() => {
    if (!projectId || !filePath) return;
    const existing = documents.find((item) => item.path === filePath);
    if (existing) { if (activePath !== existing.path) setActivePath(existing.path); return; }
    let stale = false;
    api.workspaceFile(projectId, filePath).then((loaded) => {
      if (stale) return;
      setDocuments((items) => items.some((item) => item.path === loaded.path) ? items : [...items, { ...loaded, draft: loaded.content, dirty: false }]);
      setActivePath(loaded.path);
    }).catch((error) => { if (!stale) setMessage(error instanceof Error ? error.message : String(error)); });
    return () => { stale = true; };
  }, [activePath, api, documents, filePath, projectId]);

  useEffect(() => {
    let stale = false; let selectedUrl = "";
    if (projectId && compilation.data?.status === "completed") {
      api.latexPdfUrl(projectId, compilation.data.compilation_id).then((url) => {
        if (stale) { if (url.startsWith("blob:")) URL.revokeObjectURL(url); return; }
        selectedUrl = url; setPdfUrl(url);
      }).catch((error) => setMessage(error instanceof Error ? error.message : String(error)));
    } else setPdfUrl("");
    return () => { stale = true; if (selectedUrl.startsWith("blob:")) URL.revokeObjectURL(selectedUrl); };
  }, [api, compilation.data?.compilation_id, compilation.data?.status, projectId]);
  useEffect(() => {
    let stale = false;
    const terminal = compilation.data && ["completed", "failed"].includes(compilation.data.status);
    if (!projectId || !terminal) { setCompileLog(""); return; }
    api.latexLog(projectId, compilation.data!.compilation_id)
      .then((value) => { if (!stale) setCompileLog(value); })
      .catch(() => { if (!stale) setCompileLog(""); });
    return () => { stale = true; };
  }, [api, compilation.data?.compilation_id, compilation.data?.status, projectId]);

  const refreshTree = useCallback(() => { setTreeRevision((value) => value + 1); void queryClient.invalidateQueries({ queryKey: ["workspace", "tree", projectId] }); }, [projectId, queryClient]);
  useEffect(() => {
    if (!projectId) return;
    workspaceCursor.current = 0;
    const streamId = `project:${projectId}:workspace`;
    const socket = api.subscribeStreams({ [streamId]: workspaceCursor.current }, (event) => {
      if (event.stream_id !== streamId) return;
      workspaceCursor.current = event.stream_sequence || event.sequence;
      refreshTree();
      void queryClient.invalidateQueries({ queryKey: ["workspace", "changes", projectId] });
    });
    return () => socket.close();
  }, [api, projectId, queryClient, refreshTree]);
  async function open(entry: WorkspaceEntry) {
    setSelected(entry); setMovePath(entry.path); setInspectorOpen(true);
    if (!projectId || entry.kind !== "file") return;
    setParams((current) => {
      const next = new URLSearchParams(current); next.set("project", projectId); next.set("file", entry.path); return next;
    });
    const existing = documents.find((item) => item.path === entry.path);
    if (existing) { setActivePath(existing.path); return; }
    setBusy(true); setMessage("");
    try { const document = await api.workspaceFile(projectId, entry.path); setDocuments((items) => [...items, { ...document, draft: document.content, dirty: false }]); setActivePath(entry.path); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  const updateDraft = useCallback((value: string) => setDocuments((items) => items.map((item) => item.path === activePath ? { ...item, draft: value, dirty: value !== item.content } : item)), [activePath]);
  const save = useCallback(async (document = active) => {
    if (!projectId || !document) return false;
    if (!document.dirty) return true;
    setBusy(true); setMessage(""); setConflict(null);
    try {
      const saved = await api.saveWorkspaceFile(projectId, document.path, document.draft, document.etag);
      setDocuments((items) => items.map((item) => item.path === document.path ? { ...item, content: document.draft, draft: document.draft, etag: saved.etag, dirty: false } : item));
      setMessage("文件已原子保存。"); refreshTree();
      return true;
    } catch (error) {
      if (error instanceof GatewayRequestError && error.code === "file_conflict") setConflict(document);
      setMessage(error instanceof Error ? error.message : String(error));
      return false;
    } finally { setBusy(false); }
  }, [active, api, projectId, refreshTree]);
  useEffect(() => {
    function keyboard(event: KeyboardEvent) { if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "s") { event.preventDefault(); void save(); } }
    window.addEventListener("keydown", keyboard); return () => window.removeEventListener("keydown", keyboard);
  }, [save]);

  async function createEntry() {
    if (!projectId || !entryName.trim()) return;
    const parent = selected?.kind === "directory" ? selected.path : selected?.path.includes("\\") ? selected.path.slice(0, selected.path.lastIndexOf("\\")) : "YYWorkspace:\\";
    const path = `${(parent || "YYWorkspace:\\").replace(/\\?$/, "\\")}${entryName.trim()}`;
    setBusy(true); setMessage("");
    try { const created = await api.createWorkspaceEntry(projectId, path, entryKind); setEntryName(""); refreshTree(); if (created.kind === "file") await open(created); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  async function removeSelected() {
    if (!projectId || !selected || !window.confirm(`删除 ${selected.path}？将先创建可恢复 Checkpoint。`)) return;
    setBusy(true);
    try { const result = await api.deleteWorkspaceEntry(projectId, selected.path); setDocuments((items) => items.filter((item) => item.path !== selected.path && !item.path.startsWith(`${selected.path}\\`))); if (activePath === selected.path || activePath.startsWith(`${selected.path}\\`)) { setActivePath(""); setParams((current) => { const next = new URLSearchParams(current); next.delete("file"); return next; }); } setSelected(null); setMessage(`已删除；Checkpoint ${String(result.checkpoint_id || "已记录")}`); refreshTree(); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  async function moveSelected() {
    if (!projectId || !selected || !movePath.trim() || movePath.trim() === selected.path) return;
    setBusy(true); setMessage("");
    try {
      const result = await api.moveWorkspaceEntry(projectId, selected.path, movePath.trim());
      const moved = result.entry as WorkspaceEntry;
      const source = selected.path;
      const destination = moved.path;
      setDocuments((items) => items.map((item) => {
        if (item.path !== source && !item.path.startsWith(`${source}\\`)) return item;
        const path = `${destination}${item.path.slice(source.length)}`;
        return { ...item, path, name: path.split("\\").at(-1) || item.name };
      }));
      if (activePath === source || activePath.startsWith(`${source}\\`)) {
        const movedActive = `${destination}${activePath.slice(source.length)}`;
        setActivePath(movedActive);
        setParams((current) => { const next = new URLSearchParams(current); next.set("file", movedActive); return next; });
      }
      setSelected(moved); setMovePath(destination); setMessage("路径已移动并写入 Workspace 事件流。"); refreshTree();
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  async function compileLatex() {
    if (!projectId || !active || !active.path.toLocaleLowerCase().endsWith(".tex")) return;
    if (active.dirty && !await save(active)) return;
    setBusy(true); setMessage(""); setPdfUrl("");
    try {
      setCompileLog("");
      const started = await api.startLatexCompilation(
        projectId, active.path, workspaceCursor.current,
      );
      setParams((current) => {
        const next = new URLSearchParams(current);
        next.set("project", projectId);
        next.set("compilation", started.compilation_id);
        return next;
      });
      setMessage(`LaTeX ${started.engine} 编译已进入隔离队列。`);
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  async function reloadConflict() {
    if (!projectId || !conflict) return;
    const latest = await api.workspaceFile(projectId, conflict.path);
    setDocuments((items) => items.map((item) => item.path === conflict.path ? { ...latest, draft: latest.content, dirty: false } : item));
    setConflict(null); setMessage("已加载磁盘上的最新版本。");
  }
  async function revealDiagnostic(diagnostic: LatexDiagnostic) {
    if (!projectId) return;
    const requestedPath = diagnostic.file || compilation.data?.main_path;
    if (!requestedPath) return;
    setBusy(true); setMessage("");
    try {
      let document = documents.find((item) => item.path === requestedPath);
      if (!document) {
        const loaded = await api.workspaceFile(projectId, requestedPath);
        document = { ...loaded, draft: loaded.content, dirty: false };
        setDocuments((items) => [...items, document!]);
      }
      setActivePath(document.path);
      setParams((current) => { const next = new URLSearchParams(current); next.set("file", document!.path); return next; });
      if (diagnostic.line) setFocusLine({ line: diagnostic.line, token: Date.now() });
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  function close(path: string) {
    const item = documents.find((value) => value.path === path);
    if (item?.dirty && !window.confirm("此标签有未保存修改，仍要关闭？")) return;
    const remaining = documents.filter((value) => value.path !== path); setDocuments(remaining); if (activePath === path) { const nextPath = remaining.at(-1)?.path || ""; setActivePath(nextPath); setParams((current) => { const next = new URLSearchParams(current); if (nextPath) next.set("file", nextPath); else next.delete("file"); return next; }); }
  }

  return <WorkbenchShell projectName="Write" modelControls={<span className="mode-chip">YYWorkspace:\</span>} inspectorOpen={inspectorOpen} onToggleInspector={() => setInspectorOpen((value) => !value)} onOpenCommands={() => setCommandsOpen(true)} sidebarLabel="Workspace 文件" inspectorLabel="文件与变更" sidebar={<div className="sidebar-layout"><div className="sidebar-heading"><div><span>逻辑工作区</span><h2>Write</h2></div></div><label className="sidebar-select writer-project"><span>项目</span><select value={projectId || ""} onChange={(event) => { setParams({ project: event.target.value }); setDocuments([]); setActivePath(""); }}>{(projects.data || []).map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}</select></label>{projectId && <WorkspaceTree projectId={projectId} selected={selected?.path} revision={treeRevision} onSelect={(entry) => void open(entry)} />}</div>} inspector={<WriterInspector selected={selected} changes={changes.data?.changes || []} entryName={entryName} entryKind={entryKind} movePath={movePath} busy={busy} onName={setEntryName} onKind={setEntryKind} onMovePath={setMovePath} onCreate={() => void createEntry()} onMove={() => void moveSelected()} onDelete={() => void removeSelected()} onSend={() => active && handoffToAgent(active, navigate)} />} footer={<><StatusMark state={message && conflict ? "warning" : "ok"} label={conflict ? "保存冲突" : "Workspace CAS"} /><span className="status-spacer" /><span>{changes.data?.changes.length || 0} 项 Git 变更</span></>}>
    <div className="writer-workspace">
      <div className="editor-tabs" role="tablist">{documents.map((document) => <button role="tab" aria-selected={document.path === activePath} className={document.path === activePath ? "active" : ""} key={document.path} onClick={() => setActivePath(document.path)}><FileText aria-hidden="true" /><span>{document.name}</span>{document.dirty && <i aria-label="未保存">●</i>}<span className="tab-close" role="button" aria-label={`关闭 ${document.name}`} onClick={(event) => { event.stopPropagation(); close(document.path); }}><X aria-hidden="true" /></span></button>)}</div>
      {message && <div className={`operation-message ${conflict ? "conflict" : ""}`} role="status">{message}{conflict && <span className="conflict-actions"><button onClick={() => void reloadConflict()}>重新加载</button><button onClick={() => setConflict(null)}>保留草稿</button></span>}</div>}
      {active ? <Group className="writer-panels" orientation="vertical"><Panel id="editor" minSize="40%" defaultSize="70%"><div className="editor-pane"><header><code>{active.path}</code><div className="editor-actions">{active.path.toLocaleLowerCase().endsWith(".tex") && <button disabled={busy || ["queued", "running"].includes(compilation.data?.status || "")} onClick={() => void compileLatex()}><Play aria-hidden="true" />编译 PDF</button>}<button disabled={!active.dirty || busy} onClick={() => void save()}><Save aria-hidden="true" />{busy ? "保存中…" : "保存"}</button></div></header><CodeEditor path={active.path} value={active.draft} readOnly={busy} focusLine={focusLine} onChange={updateDraft} /></div></Panel><Separator className="panel-separator" /><Panel id="changes" minSize="18%" defaultSize="30%" collapsible><div className="writer-output-grid"><WorkspaceChanges changes={changes.data?.changes || []} />{active.path.toLocaleLowerCase().endsWith(".tex") && <LatexPanel compilation={compilation.data} pdfUrl={pdfUrl} log={compileLog} onDiagnostic={(diagnostic) => void revealDiagnostic(diagnostic)} onCancel={() => projectId && compilationId && void api.cancelLatexCompilation(projectId, compilationId).then(() => compilation.refetch())} />}</div></Panel></Group> : <div className="agent-empty"><p className="eyebrow">WRITE WORKSPACE</p><h1>从文件树打开一个文本文件。</h1><p>浏览器只使用逻辑路径。保存通过 ETag 比较和原子替换完成，不会静默覆盖 Agent 或其他窗口的修改。</p></div>}
    </div>
    <CommandPalette open={commandsOpen} onClose={() => setCommandsOpen(false)} onNewSession={() => navigate("/agent")} onAddProject={() => navigate("/agent")} onTheme={setPreference} />
  </WorkbenchShell>;
}

function WriterInspector({ selected, changes, entryName, entryKind, movePath, busy, onName, onKind, onMovePath, onCreate, onMove, onDelete, onSend }: { selected: WorkspaceEntry | null; changes: Array<{ status: string; path: string }>; entryName: string; entryKind: "file" | "directory"; movePath: string; busy: boolean; onName: (value: string) => void; onKind: (value: "file" | "directory") => void; onMovePath: (value: string) => void; onCreate: () => void; onMove: () => void; onDelete: () => void; onSend: () => void }) {
  return <div className="inspector-content"><header><div><span>WORKSPACE</span><h2>{selected?.name || "文件操作"}</h2></div><FileText aria-hidden="true" /></header><section className="inspector-section"><h3>新建</h3><div className="compact-create"><select value={entryKind} onChange={(event) => onKind(event.target.value as typeof entryKind)}><option value="file">文件</option><option value="directory">目录</option></select><input value={entryName} onChange={(event) => onName(event.target.value)} placeholder="名称" /><button disabled={busy || !entryName.trim()} onClick={onCreate}>{entryKind === "file" ? <FilePlus2 aria-hidden="true" /> : <FolderPlus aria-hidden="true" />}新建</button></div></section>{selected && <><section className="inspector-section"><h3>逻辑路径</h3><p className="mono-block">{selected.path}</p><label className="workspace-move"><span>移动或重命名</span><input value={movePath} onChange={(event) => onMovePath(event.target.value)} /><button disabled={busy || !movePath.trim() || movePath.trim() === selected.path} onClick={onMove}>应用路径</button></label><div className="reader-actions"><button disabled={selected.kind !== "file"} onClick={onSend}><Send aria-hidden="true" />发送给 Agent</button><button className="danger-outline" disabled={busy} onClick={onDelete}><Trash2 aria-hidden="true" />删除</button></div></section></>}<section className="inspector-section"><h3>当前 Git 变更</h3><p>{changes.length ? `${changes.length} 个路径有变化。` : "工作区没有 Git 变更。"}</p></section></div>;
}
function WorkspaceChanges({ changes }: { changes: Array<{ status: string; path: string }> }) { return <section className="workspace-changes"><header><GitCompare aria-hidden="true" /><h2>Workspace Changes</h2><span>{changes.length}</span></header><div>{changes.map((change) => <p key={`${change.status}-${change.path}`}><code>{change.status}</code><span>{change.path}</span></p>)}{!changes.length && <p className="quiet-empty">没有 Git 变更。</p>}</div></section>; }
function LatexPanel({ compilation, pdfUrl, log, onDiagnostic, onCancel }: { compilation?: LatexCompilation; pdfUrl: string; log: string; onDiagnostic: (diagnostic: LatexDiagnostic) => void; onCancel: () => void }) { return <section className="latex-panel"><header><h2>LaTeX Preview</h2><span>{compilation?.status || "尚未编译"}</span>{compilation && ["queued", "running"].includes(compilation.status) && <button onClick={onCancel}><Square aria-hidden="true" />取消</button>}</header>{compilation?.error && <p className="latex-error">{compilation.error}</p>}{compilation?.diagnostics.map((diagnostic, index) => <button className="latex-diagnostic" key={`${diagnostic.line}-${index}`} onClick={() => onDiagnostic(diagnostic)}><strong>{diagnostic.severity}</strong><span>{diagnostic.file || compilation.main_path}{diagnostic.line ? `:${diagnostic.line}` : ""}</span><small>{diagnostic.message}</small></button>)}{log && <details className="latex-log"><summary>原始编译日志</summary><pre>{log}</pre></details>}{pdfUrl ? <iframe title="LaTeX PDF preview" src={pdfUrl} /> : <p className="quiet-empty">编译成功后在这里显示 PDF；错误会按文件和行号列出。</p>}</section>; }
function handoffToAgent(document: OpenDocument, navigate: ReturnType<typeof useNavigate>) { window.sessionStorage.setItem("yyagent.web.agent-prefill", `请查看并协助处理文件 ${document.path}。`); window.sessionStorage.setItem("yyagent.web.agent-context", JSON.stringify({ source: "write", resource: { kind: "workspace_file", logical_path: document.path, content_hash: document.etag }, selection: { selected_text: "", page: null, start_line: null, end_line: null } })); navigate("/agent"); }
