import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bold,
  ArchiveRestore,
  Bot,
  Braces,
  ChevronDown,
  Code2,
  Copy,
  Download,
  FilePlus2,
  Folder,
  FolderPlus,
  Image as ImageIcon,
  Italic,
  Link2,
  List,
  ListOrdered,
  NotebookPen,
  Paperclip,
  Pencil,
  Quote,
  Redo2,
  RefreshCw,
  Search,
  Sigma,
  Sparkles,
  Strikethrough,
  Table2,
  Trash2,
  Undo2,
  Upload,
  Printer,
} from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WorkbenchShell } from "../../app/WorkbenchShell";
import { useTheme } from "../../app/ThemeProvider";
import { eventText } from "../../events";
import { useRunStream } from "../agent/useRunStream";
import { useGatewayApi } from "../../shared/api/context";
import { CommandPalette } from "../../shared/ui/CommandPalette";
import { Markdown } from "../../shared/ui/Markdown";
import { StatusMark } from "../../shared/ui/StatusMark";
import type { GatewayEvent, NoteNode, NoteTrashItem, ReasoningEffort, RunCreate } from "../../types";
import { TiptapNoteEditor } from "./TiptapNoteEditor";

type EditorMode = "document" | "markdown";
type NoteRevision = { revision_id: string; created_at: string };
type UploadedAsset = { name: string; markdown_path: string };
type AgentAction = "ask" | "polish" | "rewrite" | "expand" | "shorten" | "summarize" | "translate" | "latex";
type NoteSuggestion = {
  action: AgentAction;
  original: string;
  offset: number;
  answer: string;
  status: "running" | "ready" | "error";
  error?: string;
};

export function NotePage() {
  const api = useGatewayApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { setPreference } = useTheme();
  const [params, setParams] = useSearchParams();
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const projectId = params.get("project")
    || window.localStorage.getItem("yyagent.web.selected-project")
    || projects.data?.[0]?.project_id;
  const selectedId = params.get("note") || undefined;
  const [query, setQuery] = useState("");
  const [commandsOpen, setCommandsOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [mode, setMode] = useState<EditorMode>("document");
  const [document, setDocument] = useState<NoteNode | null>(null);
  const [saveState, setSaveState] = useState<"saved" | "saving" | "error">("saved");
  const [folderTarget, setFolderTarget] = useState<string | null>(null);
  const [noteRunId, setNoteRunId] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<NoteSuggestion | null>(null);
  const saveTimer = useRef<number | null>(null);
  const saveVersion = useRef(0);
  const saveInFlight = useRef(false);
  const saveQueued = useRef(false);
  const draftRef = useRef<NoteNode | null>(null);
  const workspaceCursor = useRef(0);

  const notes = useQuery({
    queryKey: ["notes", projectId, query],
    queryFn: () => api.notes(projectId!, query),
    enabled: Boolean(projectId),
  });
  useEffect(() => {
    if (!projectId) return;
    workspaceCursor.current = 0;
    const streamId = `project:${projectId}:workspace`;
    const socket = api.subscribeStreams({ [streamId]: workspaceCursor.current }, (event) => {
      if (event.stream_id !== streamId) return;
      workspaceCursor.current = event.stream_sequence || event.sequence;
      if (String(event.payload?.path || "").includes("\\notes\\") || String(event.payload?.path || "").includes("/notes/")) {
        void queryClient.invalidateQueries({ queryKey: ["notes", projectId] });
        void queryClient.invalidateQueries({ queryKey: ["note-trash", projectId] });
        if (selectedId) void queryClient.invalidateQueries({ queryKey: ["note", projectId, selectedId] });
      }
    });
    return () => socket.close();
  }, [api, projectId, queryClient, selectedId]);
  const revisions = useQuery({
    queryKey: ["note-revisions", projectId, selectedId],
    queryFn: () => api.noteRevisions(projectId!, selectedId!),
    enabled: Boolean(projectId && selectedId),
  });
  const models = useQuery({
    queryKey: ["models", projectId, "note"],
    queryFn: () => api.models(projectId),
    enabled: Boolean(projectId),
  });
  const trash = useQuery({
    queryKey: ["note-trash", projectId],
    queryFn: () => api.noteTrash(projectId!),
    enabled: Boolean(projectId),
  });
  const selected = notes.data?.find((item) => item.note_id === selectedId && item.kind === "note");
  const onSuggestionTerminal = useCallback((event: GatewayEvent) => {
    setNoteRunId(null);
    setSuggestion((current) => current ? {
      ...current,
      answer: eventText(event) || current.answer,
      status: event.type === "run_completed" ? "ready" : "error",
      error: event.type === "run_completed" ? undefined : eventText(event) || "Agent 未能生成建议",
    } : current);
    if (event.run_id) void api.acknowledgeRunResult(event.run_id).catch(() => undefined);
  }, [api]);
  const suggestionStream = useRunStream(api, noteRunId, onSuggestionTerminal);

  useEffect(() => {
    const streamed = suggestionStream.events.filter((event) => event.type === "text").map(eventText).join("");
    if (!streamed) return;
    setSuggestion((current) => current?.status === "running" ? { ...current, answer: streamed } : current);
  }, [suggestionStream.events]);

  const previousNoteId = useRef(selectedId);
  useEffect(() => {
    if (previousNoteId.current === selectedId) return;
    previousNoteId.current = selectedId;
    if (noteRunId) void api.cancelRun(noteRunId).catch(() => undefined);
    setNoteRunId(null);
    setSuggestion(null);
    suggestionStream.reset();
  }, [api, noteRunId, selectedId, suggestionStream.reset]);

  useEffect(() => {
    if (projectId && !params.get("project")) {
      setParams({ project: projectId, ...(selectedId ? { note: selectedId } : {}) }, { replace: true });
    }
  }, [params, projectId, selectedId, setParams]);

  useEffect(() => {
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = null;
    saveVersion.current += 1;
    setSaveState("saved");
    if (!projectId || !selectedId) {
      draftRef.current = null;
      setDocument(null);
      return;
    }
    let active = true;
    void api.note(projectId, selectedId).then((value) => {
      if (!active) return;
      draftRef.current = value;
      setDocument(value);
    }).catch((error) => {
      if (active) setMessage(errorMessage(error));
    });
    return () => { active = false; };
  }, [api, projectId, selectedId]);

  useEffect(() => {
    if (selected) setFolderTarget(selected.parent_id);
  }, [selected?.note_id, selected?.parent_id]);

  useEffect(() => {
    if (!projectId || !selectedId || query || !notes.isSuccess) return;
    if (notes.data.some((item) => item.note_id === selectedId)) return;
    const next = notes.data.find((item) => item.kind === "note");
    setParams({ project: projectId, ...(next ? { note: next.note_id } : {}) }, { replace: true });
  }, [notes.data, notes.isSuccess, projectId, query, selectedId, setParams]);

  useEffect(() => () => {
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
  }, []);

  useEffect(() => {
    if (!projectId || !selectedId || saveState !== "saved") return;
    const timer = window.setInterval(() => {
      void api.note(projectId, selectedId).then((value) => {
        if (draftRef.current && value.etag !== draftRef.current.etag) {
          draftRef.current = value;
          setDocument(value);
          setMessage("已从外部 Markdown 文件刷新");
        }
      }).catch(() => undefined);
    }, 4_000);
    return () => window.clearInterval(timer);
  }, [api, projectId, selectedId, saveState]);

  function scheduleSave(next: Partial<NoteNode>) {
    if (!projectId || !draftRef.current) return;
    const pending = { ...draftRef.current, ...next };
    ++saveVersion.current;
    draftRef.current = pending;
    setDocument(pending);
    setSaveState("saving");
    setMessage("");
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => void flushSave(), 650);
  }

  async function flushSave() {
    if (!projectId || !draftRef.current) return;
    if (saveInFlight.current) {
      saveQueued.current = true;
      return;
    }
    const pending = draftRef.current;
    const version = saveVersion.current;
    saveInFlight.current = true;
    try {
      const value = await api.updateNote(projectId, pending.note_id, {
        name: pending.name,
        content: pending.content,
        tags: pending.tags,
        expected_etag: pending.etag,
      });
      if (version === saveVersion.current) {
        draftRef.current = value;
        setDocument(value);
        setSaveState("saved");
        void notes.refetch();
      } else if (draftRef.current?.note_id === pending.note_id) {
        draftRef.current = {
          ...draftRef.current,
          etag: value.etag,
          file_path: value.file_path,
        };
        setDocument(draftRef.current);
        saveQueued.current = true;
      }
    } catch (error) {
      setSaveState("error");
      setMessage(errorMessage(error));
      saveQueued.current = false;
    } finally {
      saveInFlight.current = false;
      if (saveQueued.current) {
        saveQueued.current = false;
        saveTimer.current = window.setTimeout(() => void flushSave(), 0);
      }
    }
  }

  async function create(kind: "note" | "folder") {
    if (!projectId) return;
    const name = kind === "note" ? "未命名笔记" : window.prompt("文件夹名称", "新文件夹");
    if (!name?.trim()) return;
    try {
      const value = kind === "note"
        ? await api.createNote(projectId, name.trim(), null)
        : await api.createNoteFolder(projectId, name.trim(), null);
      await notes.refetch();
      if (kind === "note") setParams({ project: projectId, note: value.note_id });
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  async function move(noteId: string, parentId: string | null) {
    if (!projectId) return;
    try {
      const value = await api.moveNote(projectId, noteId, parentId);
      if (noteId === document?.note_id) {
        draftRef.current = value;
        setDocument(value);
      }
      await notes.refetch();
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  async function rename(node: NoteNode) {
    if (!projectId) return;
    const name = window.prompt("重命名", node.name);
    if (!name?.trim() || name.trim() === node.name) return;
    try {
      const value = await api.updateNote(projectId, node.note_id, { name: name.trim() });
      if (node.note_id === document?.note_id) {
        draftRef.current = value;
        setDocument(value);
      }
      await notes.refetch();
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  async function remove(node: NoteNode) {
    if (!projectId || !window.confirm(`确定删除“${node.name}”及其中内容吗？`)) return;
    try {
      await api.deleteNote(projectId, node.note_id);
      const [refreshed] = await Promise.all([notes.refetch(), trash.refetch()]);
      if (selectedId && !refreshed.data?.some((item) => item.note_id === selectedId)) {
        const next = refreshed.data?.find((item) => item.kind === "note");
        setParams({ project: projectId, ...(next ? { note: next.note_id } : {}) });
      }
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  async function restoreTrash(trashId: string) {
    if (!projectId) return;
    try {
      const restored = await api.restoreTrashedNote(projectId, trashId);
      await Promise.all([notes.refetch(), trash.refetch()]);
      if (restored.kind === "note") setParams({ project: projectId, note: restored.note_id });
      setMessage(`已恢复“${restored.name}”`);
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  async function reindex() {
    if (!projectId) return;
    try {
      const result = await api.reindexNotes(projectId);
      await notes.refetch();
      setMessage(`已重新扫描 ${result.notes} 篇 Markdown 笔记`);
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  async function restoreRevision(revisionId: string) {
    if (!projectId || !document || !window.confirm("恢复这个历史版本吗？当前内容会先保存到历史记录。")) return;
    try {
      const value = await api.restoreNoteRevision(projectId, document.note_id, revisionId);
      draftRef.current = value;
      setDocument(value);
      await revisions.refetch();
      setMessage("已恢复历史版本");
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  async function uploadAsset(file: File, kind: "image" | "attachment") {
    if (!projectId || !document) return null;
    try {
      const result = await api.uploadNoteAsset(projectId, document.note_id, file, kind);
      setMessage(`${file.name} 已保存到 Workspace assets`);
      return result;
    } catch (error) {
      setMessage(errorMessage(error));
      return null;
    }
  }

  async function importMarkdown(file: File) {
    if (!projectId) return;
    if (file.size > 2 * 1024 * 1024) {
      setMessage("Markdown 文件不能超过 2 MiB");
      return;
    }
    try {
      const value = await api.createNote(projectId, file.name.replace(/\.md$/i, "") || "导入笔记", null, await file.text());
      await notes.refetch();
      setParams({ project: projectId, note: value.note_id });
      setMessage(`已导入 ${file.name}`);
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  async function exportMarkdown() {
    if (!projectId || !document) return;
    try {
      const url = await api.noteRawUrl(projectId, document.note_id);
      const anchor = window.document.createElement("a");
      anchor.href = url;
      anchor.download = `${document.name}.md`;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
      setMessage("已导出 Markdown");
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  const loadAsset = useCallback((path: string) => {
    if (!projectId) return Promise.reject(new Error("尚未选择 Workspace"));
    return api.noteAssetUrl(projectId, path);
  }, [api, projectId]);

  async function sendSelectionToAgent(action: AgentAction, selectedText: string) {
    if (!projectId || !document || !selectedText.trim() || noteRunId || suggestion?.status === "running") return;
    const prompts: Record<AgentAction, string> = {
      ask: "请解释这段笔记内容，并指出其中可能需要补充或核验的地方。",
      polish: "请润色这段笔记，保持原意并提高表达清晰度。",
      rewrite: "请重写这段笔记，使结构更清楚、语言更准确。",
      expand: "请扩写这段笔记，补充必要细节，但不要引入未经说明的事实。",
      shorten: "请缩写这段笔记，保留关键信息并删除重复表达。",
      summarize: "请总结这段笔记的核心观点。",
      translate: "请翻译这段笔记；自动识别语言并翻译为中文或英文，只输出译文。",
      latex: "请把这段笔记转换为清晰、可直接写入 Markdown 的 LaTeX 表达。",
    };
    const logicalPath = `YYWorkspace:\\${(document.file_path || `notes/${document.name}.md`).replaceAll("/", "\\")}`;
    const textOffset = document.content.indexOf(selectedText);
    const startLine = textOffset >= 0 ? document.content.slice(0, textOffset).split("\n").length : null;
    const endLine = startLine === null ? null : startLine + selectedText.split("\n").length - 1;
    const nearbyContext = textOffset < 0 ? "" : document.content.slice(
      Math.max(0, textOffset - 1_500),
      Math.min(document.content.length, textOffset + selectedText.length + 1_500),
    );
    const context: RunCreate["uiContext"] = {
      source: "note",
      resource: {
        kind: "workspace_file",
        paper_id: null,
        logical_path: logicalPath,
        content_hash: document.etag,
      },
      selection: {
        selected_text: selectedText.slice(0, 20_000),
        page: null,
        start_line: startLine,
        end_line: endLine,
        nearby_context: nearbyContext,
        locator: { note_id: document.note_id, text_offset: textOffset >= 0 ? textOffset : null },
      },
    };
    if (action === "ask") {
      window.sessionStorage.setItem("yyagent.web.agent-prefill", prompts[action]);
      window.sessionStorage.setItem("yyagent.web.agent-context", JSON.stringify(context));
      navigate(`/agent?project=${encodeURIComponent(projectId)}`);
      return;
    }
    const selectedModel = models.data?.find((item) => item.selected) || models.data?.[0];
    if (!selectedModel) {
      setMessage("当前没有可用模型");
      return;
    }
    const savedEffort = window.localStorage.getItem("yyagent.web.reasoning-effort");
    const effort: ReasoningEffort = action === "translate"
      ? "none"
      : isReasoningEffort(savedEffort) ? savedEffort : "low";
    suggestionStream.reset();
    setSuggestion({ action, original: selectedText, offset: textOffset, answer: "", status: "running" });
    try {
      const created = await api.startRun({
        projectId,
        task: `${prompts[action]}\n\n不要调用工具，不要解释过程，不要添加 Markdown 代码围栏，只返回可以替换原文的最终文本。`,
        modelProfileId: selectedModel.profile_id,
        reasoningEffort: effort,
        uiContext: context,
      });
      setNoteRunId(created.run_id);
    } catch (error) {
      setSuggestion((current) => current ? { ...current, status: "error", error: errorMessage(error) } : current);
    }
  }

  function acceptSuggestion() {
    if (!suggestion || !document || suggestion.status !== "ready") return;
    if (!suggestion.answer.trim()) {
      setMessage("Agent 没有返回可应用的建议");
      return;
    }
    if (suggestion.offset < 0 || document.content.slice(suggestion.offset, suggestion.offset + suggestion.original.length) !== suggestion.original) {
      setMessage("原文在生成建议期间已经变化，请重新选择后再试");
      return;
    }
    scheduleSave({
      content: document.content.slice(0, suggestion.offset)
        + suggestion.answer
        + document.content.slice(suggestion.offset + suggestion.original.length),
    });
    setSuggestion(null);
    suggestionStream.reset();
  }

  async function rejectSuggestion() {
    if (noteRunId) await api.cancelRun(noteRunId).catch(() => undefined);
    setNoteRunId(null);
    setSuggestion(null);
    suggestionStream.reset();
  }

  const tree = notes.data || [];
  const folders = tree.filter((item) => item.kind === "folder");
  const knownNotes = tree.filter((item) => item.kind === "note");
  const openNote = (id: string) => projectId && setParams({ project: projectId, note: id });

  return (
    <WorkbenchShell
      projectName="Note"
      modelControls={<span className="mode-chip">WORKSPACE VAULT</span>}
      sidebarLabel="Workspace 笔记"
      inspectorLabel="笔记信息"
      inspectorOpen={inspectorOpen}
      onToggleInspector={() => setInspectorOpen((value) => !value)}
      onOpenCommands={() => setCommandsOpen(true)}
      sidebar={<NoteSidebar tree={tree} selectedId={selectedId} query={query} onQuery={setQuery} onSelect={openNote} onRename={(node) => void rename(node)} onDelete={(node) => void remove(node)} onMove={(id, parent) => void move(id, parent)} onCreateNote={() => void create("note")} onCreateFolder={() => void create("folder")} onImport={(file) => void importMarkdown(file)} />}
      inspector={<NoteInspector document={document} folders={folders} revisions={revisions.data || []} trash={trash.data || []} onRestoreTrash={(id) => void restoreTrash(id)} onRestore={(id) => void restoreRevision(id)} onFolderTarget={setFolderTarget} folderTarget={folderTarget} onMove={() => document && void move(document.note_id, folderTarget)} onDelete={() => document && void remove(document)} onExport={() => void exportMarkdown()} onPrint={() => window.print()} onReindex={() => void reindex()} onOpenNote={openNote} onOutline={() => window.document.querySelector(".tiptap-note-content")?.scrollIntoView({ behavior: "smooth", block: "start" })} onTags={(tags) => scheduleSave({ tags })} />}
      footer={<><StatusMark state={notes.isError || saveState === "error" ? "warning" : "ok"} label={saveState === "saving" ? "正在保存" : saveState === "error" ? "保存失败" : "已保存"} /><span className="status-spacer" /><span>{tree.filter((item) => item.kind === "note").length} 条笔记</span>{message && <span className="status-message">{message}</span>}</>}
    >
      <div className="note-workspace">
        {document ? <><input className="note-document-title" value={document.name} onChange={(event) => scheduleSave({ name: event.target.value })} aria-label="笔记标题" /><TiptapNoteEditor value={document.content} mode={mode} onMode={setMode} onChange={(content) => scheduleSave({ content })} knownNotes={knownNotes} onOpenNote={openNote} onUpload={uploadAsset} onAgentAction={(action, text) => void sendSelectionToAgent(action, text)} suggestion={suggestion} onAcceptSuggestion={acceptSuggestion} onRejectSuggestion={() => void rejectSuggestion()} onRegenerateSuggestion={() => suggestion && void sendSelectionToAgent(suggestion.action, suggestion.original)} /></> : <div className="agent-empty"><p className="eyebrow">WORKSPACE VAULT</p><h1>把研究和想法留在当前 Workspace。</h1><p>Markdown 是真实文件，笔记、附件和关系都属于当前知识库。</p><button className="primary-action" onClick={() => void create("note")}><FilePlus2 />新建笔记</button></div>}
      </div>
      <CommandPalette open={commandsOpen} onClose={() => setCommandsOpen(false)} onNewSession={() => navigate("/agent")} onAddProject={() => navigate("/agent")} onTheme={setPreference} />
    </WorkbenchShell>
  );
}

function NoteDocumentEditor({ value, mode, onMode, onChange, knownNotes, onOpenNote, onUpload, resolveAsset, loadAsset, onAgentAction, suggestion, onAcceptSuggestion, onRejectSuggestion, onRegenerateSuggestion }: {
  value: string;
  mode: EditorMode;
  onMode: (mode: EditorMode) => void;
  onChange: (value: string) => void;
  knownNotes: NoteNode[];
  onOpenNote: (id: string) => void;
  onUpload: (file: File, kind: "image" | "attachment") => Promise<UploadedAsset | null>;
  resolveAsset: (source: string) => string | null;
  loadAsset: (path: string) => Promise<string>;
  onAgentAction: (action: AgentAction, selectedText: string) => void;
  suggestion: NoteSuggestion | null;
  onAcceptSuggestion: () => void;
  onRejectSuggestion: () => void;
  onRegenerateSuggestion: () => void;
}) {
  const [activeBlock, setActiveBlock] = useState<number | null>(null);
  const [selectedText, setSelectedText] = useState("");
  const [tableBuilderOpen, setTableBuilderOpen] = useState(false);
  const [tableRows, setTableRows] = useState(3);
  const [tableColumns, setTableColumns] = useState(2);
  const [collapsedHeadings, setCollapsedHeadings] = useState<Set<number>>(new Set());
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState("");
  const [replaceQuery, setReplaceQuery] = useState("");
  const blocks = useMemo(() => splitBlocks(value), [value]);
  const imageInput = useRef<HTMLInputElement>(null);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const undoStack = useRef<string[]>([]);
  const redoStack = useRef<string[]>([]);
  const historyGroupAt = useRef(0);
  const lastLocalValue = useRef(value);

  useEffect(() => {
    if (value !== lastLocalValue.current) {
      undoStack.current = [];
      redoStack.current = [];
      historyGroupAt.current = 0;
    }
    lastLocalValue.current = value;
  }, [value]);

  const commit = (next: string) => {
    if (next === value) return;
    const now = Date.now();
    if (now - historyGroupAt.current > 750) {
      undoStack.current = [...undoStack.current.slice(-99), value];
    }
    historyGroupAt.current = now;
    redoStack.current = [];
    lastLocalValue.current = next;
    onChange(next);
  };
  const undo = () => {
    const previous = undoStack.current.pop();
    if (previous === undefined) return;
    redoStack.current.push(value);
    historyGroupAt.current = 0;
    lastLocalValue.current = previous;
    onChange(previous);
  };
  const redo = () => {
    const next = redoStack.current.pop();
    if (next === undefined) return;
    undoStack.current.push(value);
    historyGroupAt.current = 0;
    lastLocalValue.current = next;
    onChange(next);
  };

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
        event.preventDefault();
        setFindOpen(true);
      }
      if (event.key === "Escape" && findOpen) setFindOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [findOpen]);

  const update = (index: number, content: string) => {
    const next = index < blocks.length ? blocks.map((item, position) => position === index ? content : item) : [...blocks, content];
    commit(joinBlocks(next));
  };
  const insert = (snippet: string) => {
    if (activeBlock === null || activeBlock >= blocks.length) commit(joinBlocks([...blocks, snippet]));
    else update(activeBlock, `${blocks[activeBlock]}${blocks[activeBlock] ? "\n" : ""}${snippet}`);
  };
  const insertTable = () => {
    const rows = Math.min(20, Math.max(1, tableRows));
    const columns = Math.min(12, Math.max(1, tableColumns));
    const line = `| ${Array.from({ length: columns }, (_, index) => `列 ${index + 1}`).join(" | ")} |`;
    const divider = `| ${Array.from({ length: columns }, () => "---").join(" | ")} |`;
    const body = Array.from({ length: Math.max(0, rows - 1) }, () => `| ${Array.from({ length: columns }, () => "内容").join(" | ")} |`);
    insert([line, divider, ...body].join("\n"));
    setTableBuilderOpen(false);
  };
  const replaceAll = () => {
    if (!findQuery) return;
    commit(value.split(findQuery).join(replaceQuery));
  };
  const headingLevels = useMemo(() => blocks.map((block) => headingLevel(block)), [blocks]);
  const hiddenBlocks = useMemo(() => {
    const hidden = new Set<number>();
    headingLevels.forEach((level, index) => {
      if (!level || !collapsedHeadings.has(index)) return;
      for (let cursor = index + 1; cursor < headingLevels.length; cursor += 1) {
        const nextLevel = headingLevels[cursor];
        if (nextLevel && nextLevel <= level) break;
        hidden.add(cursor);
      }
    });
    return hidden;
  }, [collapsedHeadings, headingLevels]);
  const toggleHeading = (index: number) => setCollapsedHeadings((current) => {
    const next = new Set(current);
    next.has(index) ? next.delete(index) : next.add(index);
    return next;
  });
  const chooseAsset = async (file: File | undefined, kind: "image" | "attachment") => {
    if (!file) return;
    const result = await onUpload(file, kind);
    if (result) insert(kind === "image" ? `![${file.name}](${result.markdown_path})` : `[${file.name}](${result.markdown_path})`);
  };
  const pasteAsset = async (event: ClipboardEvent<HTMLDivElement>) => {
    const item = Array.from(event.clipboardData.items).find((candidate) => candidate.kind === "file" && candidate.type.startsWith("image/"));
    const file = item?.getAsFile();
    if (!file) return;
    event.preventDefault();
    await chooseAsset(file, "image");
  };
  const dropAsset = async (event: DragEvent<HTMLDivElement>) => {
    const file = Array.from(event.dataTransfer.files)[0];
    if (!file) return;
    event.preventDefault();
    await chooseAsset(file, file.type.startsWith("image/") ? "image" : "attachment");
  };
  const captureSelection = () => {
    const text = window.getSelection()?.toString().trim() || "";
    if (text) setSelectedText(text.slice(0, 20_000));
  };
  const render = (block: string) => <Markdown components={{
    a: ({ href, children, ...props }) => {
      if (href?.startsWith("#yy-note-")) return <button type="button" className="note-wiki-link" onClick={() => { const note = knownNotes.find((item) => item.note_id === href.slice(9)); if (note) onOpenNote(note.note_id); }}>{children}</button>;
      const assetPath = resolveAsset(href || "");
      return assetPath ? <NoteAssetLink path={assetPath} load={loadAsset}>{children}</NoteAssetLink> : <a href={href} target="_blank" rel="noreferrer" {...props}>{children}</a>;
    },
    img: ({ src, alt }) => { const path = resolveAsset(src || ""); return path ? <NoteAssetImage alt={alt || "笔记图片"} path={path} load={loadAsset} /> : <span>{alt || "图片"}</span>; },
  }}>{linkifyWiki(block, knownNotes)}</Markdown>;

  return <section className="note-doc-editor">
    <header className="note-doc-toolbar">
      <div className="note-mode-switch" role="tablist" aria-label="笔记编辑模式"><button className={mode === "document" ? "active" : ""} onClick={() => onMode("document")} role="tab" aria-selected={mode === "document"}>文档</button><button className={mode === "markdown" ? "active" : ""} onClick={() => onMode("markdown")} role="tab" aria-selected={mode === "markdown"}>Markdown</button></div>
      <div className="note-format-actions"><ToolButton label="撤销" icon={<Undo2 />} onClick={undo} /><ToolButton label="重做" icon={<Redo2 />} onClick={redo} /><ToolButton label="查找与替换" icon={<Search />} onClick={() => setFindOpen((open) => !open)} /><span className="note-toolbar-divider" /><select className="note-heading-select" aria-label="插入标题" defaultValue="" onChange={(event) => { if (event.target.value) insert(`${event.target.value} 标题`); event.currentTarget.value = ""; }}><option value="" disabled>标题</option>{[1, 2, 3, 4, 5, 6].map((level) => <option key={level} value={"#".repeat(level)}>H{level}</option>)}</select><ToolButton label="粗体" icon={<Bold />} onClick={() => insert("**粗体**")} /><ToolButton label="斜体" icon={<Italic />} onClick={() => insert("*斜体*")} /><ToolButton label="删除线" icon={<Strikethrough />} onClick={() => insert("~~删除线~~")} /><ToolButton label="引用" icon={<Quote />} onClick={() => insert("> 引用内容")} /><ToolButton label="代码" icon={<Code2 />} onClick={() => insert("`代码`")} /><ToolButton label="链接" icon={<Link2 />} onClick={() => insert("[链接文字](https://example.com)")} /><ToolButton label="列表" icon={<List />} onClick={() => insert("- 列表项")} /><ToolButton label="有序列表" icon={<ListOrdered />} onClick={() => insert("1. 列表项")} /><ToolButton label="公式" icon={<Sigma />} onClick={() => insert("$$\n公式\n$$")} /><ToolButton label="表格" icon={<Table2 />} onClick={() => setTableBuilderOpen((open) => !open)} /><ToolButton label="图片" icon={<ImageIcon />} onClick={() => imageInput.current?.click()} /><ToolButton label="附件" icon={<Paperclip />} onClick={() => attachmentInput.current?.click()} /><ToolButton label="Mermaid" icon={<Braces />} onClick={() => insert("```mermaid\nflowchart LR\n  A --> B\n```")} /><input ref={imageInput} className="visually-hidden" type="file" accept="image/*" onChange={(event) => { void chooseAsset(event.target.files?.[0], "image"); event.currentTarget.value = ""; }} /><input ref={attachmentInput} className="visually-hidden" type="file" onChange={(event) => { void chooseAsset(event.target.files?.[0], "attachment"); event.currentTarget.value = ""; }} /></div>
    </header>
    {findOpen && <div className="note-find-bar" role="search"><input autoFocus value={findQuery} onChange={(event) => setFindQuery(event.target.value)} placeholder="查找…" aria-label="查找文字" /><span>{findQuery ? `${value.split(findQuery).length - 1} 处` : ""}</span><input value={replaceQuery} onChange={(event) => setReplaceQuery(event.target.value)} placeholder="替换为…" aria-label="替换为" /><button className="secondary-button" type="button" onClick={replaceAll} disabled={!findQuery}>全部替换</button><button className="icon-button" type="button" onClick={() => setFindOpen(false)} aria-label="关闭查找">×</button></div>}
    {tableBuilderOpen && <div className="note-table-builder" role="dialog" aria-label="创建 Markdown 表格"><label>行数<input type="number" min={2} max={20} value={tableRows} onChange={(event) => setTableRows(Number(event.target.value) || 2)} /></label><label>列数<input type="number" min={1} max={12} value={tableColumns} onChange={(event) => setTableColumns(Number(event.target.value) || 1)} /></label><button className="primary-action" type="button" onClick={insertTable}>插入表格</button><button className="secondary-button" type="button" onClick={() => setTableBuilderOpen(false)}>取消</button></div>}
    {suggestion && <NoteSuggestionPanel suggestion={suggestion} onAccept={onAcceptSuggestion} onReject={onRejectSuggestion} onRegenerate={onRegenerateSuggestion} />}
    {mode === "markdown" ? <textarea className="note-source-editor" value={value} onChange={(event) => commit(event.target.value)} aria-label="Markdown 源码" spellCheck={false} /> : <div className="note-editor-stage">{selectedText && <div className="note-selection-actions" role="toolbar" aria-label="选中文字操作"><span>{truncate(selectedText, 72)}</span><ToolButton label="复制" icon={<Copy />} onClick={() => void navigator.clipboard.writeText(selectedText)} /><ToolButton label="询问 Agent" icon={<Bot />} onClick={() => onAgentAction("ask", selectedText)} /><ToolButton label="润色" icon={<Sparkles />} onClick={() => onAgentAction("polish", selectedText)} /><button type="button" onClick={() => onAgentAction("rewrite", selectedText)}>重写</button><button type="button" onClick={() => onAgentAction("expand", selectedText)}>扩写</button><button type="button" onClick={() => onAgentAction("shorten", selectedText)}>缩写</button><button type="button" onClick={() => onAgentAction("summarize", selectedText)}>总结</button><button type="button" onClick={() => onAgentAction("translate", selectedText)}>翻译</button><button type="button" onClick={() => onAgentAction("latex", selectedText)}>转 LaTeX</button><button type="button" onClick={() => setSelectedText("")} aria-label="关闭选区操作">×</button></div>}<div className="note-rich-editor" role="document" aria-label="笔记文档编辑器" onMouseUp={captureSelection} onPaste={(event) => { void pasteAsset(event); }} onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); }} onDrop={(event) => { void dropAsset(event); }}>{blocks.map((block, index) => hiddenBlocks.has(index) ? null : <div className="note-rich-block" data-block={index} key={`${index}-${block.slice(0, 24)}`}>{activeBlock === index ? <BlockEditor value={block} index={index} onChange={update} onDone={() => setActiveBlock(null)} /> : <div className={headingLevels[index] ? "note-heading-row" : ""}><RenderedBlock onEdit={() => setActiveBlock(index)}>{render(block)}</RenderedBlock>{headingLevels[index] && <button type="button" className={`note-heading-toggle ${collapsedHeadings.has(index) ? "collapsed" : ""}`} onClick={() => toggleHeading(index)} aria-label={collapsedHeadings.has(index) ? "展开标题内容" : "收起标题内容"}><ChevronDown /></button>}</div>}</div>)}{!blocks.length && (activeBlock === 0 ? <div className="note-rich-block"><BlockEditor value="" index={0} onChange={update} onDone={() => setActiveBlock(null)} /></div> : <button className="note-rendered-block note-empty-block" onClick={() => setActiveBlock(0)}>点击开始写作…</button>)}</div></div>}
  </section>;
}

function RenderedBlock({ children, onEdit }: { children: ReactNode; onEdit: () => void }) {
  const activate = (event: { target: EventTarget | null }) => { const target = event.target instanceof Element ? event.target : null; if (!window.getSelection()?.toString().trim() && !target?.closest("a, button")) onEdit(); };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onEdit(); } };
  return <div className="note-rendered-block" role="button" tabIndex={0} onClick={activate} onKeyDown={onKeyDown}>{children}</div>;
}

function NoteSuggestionPanel({ suggestion, onAccept, onReject, onRegenerate }: { suggestion: NoteSuggestion; onAccept: () => void; onReject: () => void; onRegenerate: () => void }) {
  const labels: Record<AgentAction, string> = {
    ask: "询问",
    polish: "润色",
    rewrite: "重写",
    expand: "扩写",
    shorten: "缩写",
    summarize: "总结",
    translate: "翻译",
    latex: "转 LaTeX",
  };
  const running = suggestion.status === "running";
  return <section className={`note-suggestion ${suggestion.status}`} aria-live="polite">
    <header className="note-suggestion-header">
      <div><Sparkles aria-hidden="true" /><strong>Agent 建议 · {labels[suggestion.action]}</strong></div>
      <span>{running ? "正在生成" : suggestion.status === "ready" ? "待确认" : "生成失败"}</span>
    </header>
    <div className="note-suggestion-diff">
      <div className="note-suggestion-side removed"><span>原文</span><del>{suggestion.original}</del></div>
      <div className="note-suggestion-side added"><span>建议结果</span>{suggestion.answer ? <ins>{suggestion.answer}</ins> : <p className="note-suggestion-placeholder">正在等待 Agent 输出…</p>}</div>
    </div>
    {suggestion.error && <p className="note-suggestion-error">{suggestion.error}</p>}
    <footer className="note-suggestion-actions">
      {suggestion.status === "ready" && <button className="primary-action" type="button" onClick={onAccept}>接受修改</button>}
      {!running && <button className="secondary-button" type="button" onClick={onRegenerate}><RefreshCw />重新生成</button>}
      <button className="secondary-button" type="button" onClick={onReject}>{running ? "停止生成" : "拒绝"}</button>
    </footer>
  </section>;
}

const slashCommands = [["标题", "## 标题"], ["列表", "- 列表项"], ["Checklist", "- [ ] 待办事项"], ["引用", "> 引用内容"], ["代码块", "```text\n代码\n```"], ["公式", "$$\n公式\n$$"], ["表格", "| 列 1 | 列 2 |\n| --- | --- |\n| 内容 | 内容 |"], ["Mermaid", "```mermaid\nflowchart LR\n  A --> B\n```"], ["分割线", "---"]] as const;
function BlockEditor({ value, index, onChange, onDone }: { value: string; index: number; onChange: (index: number, value: string) => void; onDone: () => void }) { const slashOpen = value.trimEnd().endsWith("/"); const apply = (snippet: string) => onChange(index, value.replace(/\/?\s*$/, "") + snippet); return <div className="note-block-editor"><textarea autoFocus value={value} onChange={(event) => onChange(index, event.target.value)} onBlur={(event) => { if (!event.currentTarget.parentElement?.contains(event.relatedTarget)) onDone(); }} onKeyDown={(event) => { if (event.key === "Escape") onDone(); }} aria-label={`编辑第 ${index + 1} 个内容块`} />{slashOpen && <div className="note-slash-menu" role="menu" aria-label="插入内容块">{slashCommands.map(([label, snippet]) => <button type="button" role="menuitem" key={label} onMouseDown={(event) => event.preventDefault()} onClick={() => apply(snippet)}>{label}</button>)}</div>}</div>; }

function NoteAssetImage({ path, alt, load }: { path: string; alt: string; load: (path: string) => Promise<string> }) { const [url, setUrl] = useState(""); useEffect(() => { let active = true, objectUrl = ""; void load(path).then((value) => { objectUrl = value; if (active) setUrl(value); }).catch(() => undefined); return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); }; }, [load, path]); return url ? <img src={url} alt={alt} /> : <span className="note-asset-loading">正在加载图片…</span>; }
function NoteAssetLink({ path, load, children }: { path: string; load: (path: string) => Promise<string>; children: ReactNode }) { const open = async () => { const url = await load(path); window.open(url, "_blank", "noopener,noreferrer"); window.setTimeout(() => URL.revokeObjectURL(url), 60_000); }; return <button type="button" className="note-asset-link" onClick={() => void open()}><Paperclip />{children}</button>; }
function ToolButton({ label, icon, onClick }: { label: string; icon: ReactNode; onClick: () => void }) { return <button type="button" className="note-toolbar-button" title={label} aria-label={label} onClick={onClick}>{icon}</button>; }

function NoteSidebar({ tree, selectedId, query, onQuery, onSelect, onRename, onDelete, onMove, onCreateNote, onCreateFolder, onImport }: { tree: NoteNode[]; selectedId?: string; query: string; onQuery: (value: string) => void; onSelect: (id: string) => void; onRename: (node: NoteNode) => void; onDelete: (node: NoteNode) => void; onMove: (id: string, parentId: string | null) => void; onCreateNote: () => void; onCreateFolder: () => void; onImport: (file: File) => void }) { const input = useRef<HTMLInputElement>(null); return <div className="sidebar-layout note-sidebar"><div className="sidebar-heading"><div><span>Workspace Vault</span><h2>Note</h2></div><div className="sidebar-heading-actions"><button className="icon-button" onClick={onCreateNote} aria-label="新建笔记" title="新建笔记"><FilePlus2 /></button><button className="icon-button" onClick={onCreateFolder} aria-label="新建文件夹" title="新建文件夹"><FolderPlus /></button><button className="icon-button" onClick={() => input.current?.click()} aria-label="导入 Markdown" title="导入 Markdown"><Upload /></button><input ref={input} className="visually-hidden" type="file" accept=".md,text/markdown,text/plain" onChange={(event) => { const file = event.target.files?.[0]; if (file) onImport(file); event.currentTarget.value = ""; }} /></div></div><label className="paper-search"><NotebookPen aria-hidden="true" /><input value={query} onChange={(event) => onQuery(event.target.value)} aria-label="搜索笔记标题、正文或标签" placeholder="搜索标题、正文或标签…" /></label><div className="note-tree">{tree.length ? <NoteTree nodes={tree} selectedId={selectedId} onSelect={onSelect} onRename={onRename} onDelete={onDelete} onMove={onMove} /> : <p className="quiet-empty">还没有笔记，先创建一条。</p>}</div></div>; }
function NoteTree({ nodes, selectedId, onSelect, onRename, onDelete, onMove }: { nodes: NoteNode[]; selectedId?: string; onSelect: (id: string) => void; onRename: (node: NoteNode) => void; onDelete: (node: NoteNode) => void; onMove: (id: string, parentId: string | null) => void }) { const [expanded, setExpanded] = useState<Set<string>>(new Set()); const children = useMemo(() => { const value = new Map<string | null, NoteNode[]>(); nodes.forEach((node) => value.set(node.parent_id, [...(value.get(node.parent_id) || []), node])); return value; }, [nodes]); const render = (parentId: string | null, depth: number): ReactNode => (children.get(parentId) || []).map((node) => <div key={node.note_id}><NoteTreeItem node={node} depth={depth} selectedId={selectedId} expanded={expanded.has(node.note_id)} onSelect={onSelect} onRename={() => onRename(node)} onDelete={() => onDelete(node)} onMove={onMove} onToggle={() => setExpanded((current) => { const next = new Set(current); next.has(node.note_id) ? next.delete(node.note_id) : next.add(node.note_id); return next; })} />{node.kind === "folder" && expanded.has(node.note_id) && render(node.note_id, depth + 1)}</div>); return <>{render(null, 0)}</>; }
function NoteTreeItem({ node, depth, selectedId, expanded, onSelect, onRename, onDelete, onMove, onToggle }: { node: NoteNode; depth: number; selectedId?: string; expanded: boolean; onSelect: (id: string) => void; onRename: () => void; onDelete: () => void; onMove: (id: string, parentId: string | null) => void; onToggle: () => void }) { return <div className={`note-tree-item ${node.kind} ${selectedId === node.note_id ? "active" : ""}`} style={{ paddingLeft: `${8 + depth * 16}px` }} draggable={node.kind === "note"} onDragStart={(event) => event.dataTransfer.setData("text/yy-note", node.note_id)} onDragOver={(event) => { if (node.kind === "folder") event.preventDefault(); }} onDrop={(event) => { if (node.kind !== "folder") return; event.preventDefault(); const id = event.dataTransfer.getData("text/yy-note"); if (id && id !== node.note_id) onMove(id, node.note_id); }}><button className="note-tree-main" onClick={() => node.kind === "folder" ? onToggle() : onSelect(node.note_id)} aria-expanded={node.kind === "folder" ? expanded : undefined}><span className="note-tree-icon">{node.kind === "folder" ? <Folder aria-hidden="true" /> : <NotebookPen aria-hidden="true" />}</span><span>{node.name}</span>{node.kind === "folder" && <ChevronDown className={expanded ? "expanded" : ""} aria-hidden="true" />}</button><div className="note-tree-actions"><button onClick={onRename} aria-label={`重命名 ${node.name}`} title="重命名"><Pencil /></button><button onClick={onDelete} aria-label={`删除 ${node.name}`} title="删除"><Trash2 /></button></div></div>; }

function NoteInspector({ document, folders, revisions, trash, folderTarget, onFolderTarget, onMove, onDelete, onExport, onPrint, onReindex, onOpenNote, onOutline, onTags, onRestore, onRestoreTrash }: { document: NoteNode | null; folders: NoteNode[]; revisions: NoteRevision[]; trash: NoteTrashItem[]; folderTarget: string | null; onFolderTarget: (value: string | null) => void; onMove: () => void; onDelete: () => void; onExport: () => void; onPrint: () => void; onReindex: () => void; onOpenNote: (id: string) => void; onOutline: (block: number) => void; onTags: (tags: string[]) => void; onRestore: (revisionId: string) => void; onRestoreTrash: (trashId: string) => void }) { return <div className="inspector-content"><header><div><span>WORKSPACE VAULT</span><h2>{document?.name || "未选择笔记"}</h2></div><NotebookPen aria-hidden="true" /></header>{document ? <><section className="inspector-section"><h3>文档信息</h3><p className="mono-block">{document.file_path || document.path}</p><label className="note-tag-input"><span>标签</span><input key={`${document.note_id}-${document.tags?.join(",")}`} defaultValue={document.tags?.join(", ")} placeholder="research, experiment" onBlur={(event) => onTags(event.target.value.split(",").map((tag) => tag.trim()).filter(Boolean))} /></label><label className="note-move"><span>移动到文件夹</span><select value={folderTarget || ""} onChange={(event) => onFolderTarget(event.target.value || null)}><option value="">根目录</option>{folders.filter((item) => item.note_id !== document.note_id).map((folder) => <option key={folder.note_id} value={folder.note_id}>{folder.path}</option>)}</select><button className="secondary-button" onClick={onMove}><Folder />移动</button></label></section><section className="inspector-section"><h3>目录</h3><NoteOutline content={document.content} onSelect={onOutline} /></section><section className="inspector-section"><h3>链接</h3>{document.links?.length ? document.links.map((link, index) => <button className="note-relation" key={`${link.target}-${index}`} disabled={!link.note_id} onClick={() => link.note_id && onOpenNote(link.note_id)}><Link2 />{link.name || link.target}</button>) : <p className="quiet-empty">本文档没有 WikiLink</p>}{!!document.backlinks?.length && <><h3 className="note-subheading">反向链接</h3>{document.backlinks.map((link) => <button className="note-relation" key={link.note_id} onClick={() => onOpenNote(link.note_id)}><Link2 />{link.name}</button>)}</>}</section><section className="inspector-section"><h3>版本历史</h3>{revisions.length ? <div className="note-revisions">{revisions.slice(0, 8).map((revision) => <button key={revision.revision_id} onClick={() => onRestore(revision.revision_id)}><span>{formatRevisionTime(revision.created_at)}</span><RefreshCw /></button>)}</div> : <p className="quiet-empty">编辑后会自动保存历史版本</p>}</section><section className="inspector-section"><h3>操作</h3><button className="secondary-button" onClick={onExport}><Download />导出 Markdown</button><button className="secondary-button" onClick={onPrint}><Printer />打印 / 导出 PDF</button><button className="secondary-button" onClick={onReindex}><RefreshCw />重新扫描 Markdown</button><button className="danger-button" onClick={onDelete}><Trash2 />移到回收站</button></section></> : <><p className="quiet-empty">从左侧选择一条笔记开始编辑。</p><button className="secondary-button" onClick={onReindex}><RefreshCw />重新扫描 Workspace</button></>}{!!trash.length && <section className="inspector-section"><h3>回收站</h3><div className="note-revisions">{trash.slice(0, 8).map((item) => <button key={item.trash_id} onClick={() => onRestoreTrash(item.trash_id)} title={item.original_file_path}><span>{item.name}</span><ArchiveRestore /></button>)}</div></section>}</div>; }
function NoteOutline({ content, onSelect }: { content: string; onSelect: (block: number) => void }) { const headings = splitBlocks(content).map((block, index) => { const match = /^(#{1,6})\s+(.+)/.exec(block); return match ? { level: match[1].length, title: match[2].split("\n", 1)[0].replace(/[*_`]/g, ""), index } : null; }).filter(Boolean) as Array<{ level: number; title: string; index: number }>; return headings.length ? <ul className="note-outline">{headings.map((heading) => <li key={`${heading.index}-${heading.title}`} style={{ paddingLeft: `${(heading.level - 1) * 10}px` }}><button type="button" onClick={() => onSelect(heading.index)}>{heading.title}</button></li>)}</ul> : <p className="quiet-empty">添加标题后会生成目录</p>; }
function splitBlocks(value: string): string[] { const lines = value.replace(/\r\n/g, "\n").split("\n"), blocks: string[] = [], current: string[] = []; let fenced = false; for (const line of lines) { if (/^\s*(```|~~~)/.test(line)) fenced = !fenced; if (!fenced && !line.trim() && current.length) { blocks.push(current.join("\n")); current.length = 0; } else current.push(line); } if (current.some((line) => line.trim())) blocks.push(current.join("\n")); return blocks; }
function headingLevel(block: string): number | null { const match = /^(#{1,6})\s+/.exec(block.trimStart()); return match ? match[1].length : null; }
function joinBlocks(blocks: string[]): string { const content = blocks.map((block) => block.trimEnd()).filter(Boolean).join("\n\n"); return content ? `${content}\n` : ""; }
function linkifyWiki(content: string, notes: NoteNode[]): string { return content.replace(/\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]/g, (_match, target: string, label?: string) => { const note = notes.find((item) => item.name.toLocaleLowerCase() === target.trim().toLocaleLowerCase()); return `[${label || target.trim()}](#yy-note-${note?.note_id || encodeURIComponent(target.trim())})`; }); }
function formatRevisionTime(value: string): string { const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/.exec(value); return match ? `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]}:${match[6]} UTC` : value; }
function resolveNoteAssetPath(noteFilePath: string, source: string): string | null { if (!source || /^(?:https?:|data:|blob:)/i.test(source)) return null; try { const resolved = new URL(source, `https://workspace.local/${noteFilePath}`).pathname.replace(/^\//, ""); return resolved.startsWith("assets/") ? decodeURIComponent(resolved) : null; } catch { return null; } }
function truncate(value: string, length: number): string { return value.length > length ? `${value.slice(0, length - 1)}…` : value; }
function errorMessage(value: unknown): string { return value instanceof Error ? value.message : String(value); }
function isReasoningEffort(value: string | null): value is ReasoningEffort { return value === "none" || value === "low" || value === "medium" || value === "high" || value === "xhigh" || value === "max"; }
