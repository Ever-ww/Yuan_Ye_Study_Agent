import { useEffect, useRef, useState, type ReactNode } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { TableKit } from "@tiptap/extension-table";
import Image from "@tiptap/extension-image";
import Placeholder from "@tiptap/extension-placeholder";
import {
  Bold, Braces, Code2, Copy, Image as ImageIcon, Italic, Link2, List,
  ListOrdered, Paperclip, Quote, Redo2, Search, Sigma, Sparkles,
  Strikethrough, Table2, Undo2,
} from "lucide-react";
import type { NoteNode } from "../../types";

export type NoteEditorAction = "ask" | "polish" | "rewrite" | "expand" | "shorten" | "summarize" | "translate" | "latex";
export type NoteEditorSuggestion = { action: NoteEditorAction; original: string; offset: number; answer: string; status: "running" | "ready" | "error"; error?: string };
type UploadedAsset = { name: string; markdown_path: string };

type Props = {
  value: string;
  mode: "document" | "markdown";
  onMode: (mode: "document" | "markdown") => void;
  onChange: (value: string) => void;
  knownNotes: NoteNode[];
  onOpenNote: (id: string) => void;
  onUpload: (file: File, kind: "image" | "attachment") => Promise<UploadedAsset | null>;
  onAgentAction: (action: NoteEditorAction, selectedText: string) => void;
  suggestion: NoteEditorSuggestion | null;
  onAcceptSuggestion: () => void;
  onRejectSuggestion: () => void;
  onRegenerateSuggestion: () => void;
};

/** A continuous WYSIWYG editor backed by Markdown files. Tables are real
 * ProseMirror tables, so cells can be edited directly and resized with the
 * table commands instead of editing a generated Markdown block. */
export function TiptapNoteEditor({ value, mode, onMode, onChange, onUpload, onAgentAction, suggestion, onAcceptSuggestion, onRejectSuggestion, onRegenerateSuggestion }: Props) {
  const [selectedText, setSelectedText] = useState("");
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState("");
  const [replaceQuery, setReplaceQuery] = useState("");
  const imageInput = useRef<HTMLInputElement>(null);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const editor = useEditor({
    extensions: [
      StarterKit,
      Markdown,
      TableKit.configure({ table: { resizable: true } }),
      Image.configure({ allowBase64: false }),
      Placeholder.configure({ placeholder: "开始写作…支持 Markdown、表格和快捷键" }),
    ],
    content: value,
    contentType: "markdown",
    immediatelyRender: false,
    editorProps: {
      attributes: { class: "tiptap-note-content", spellcheck: "true" },
      handlePaste: (_view, event) => {
        const item = Array.from(event.clipboardData?.items || []).find((candidate) => candidate.kind === "file" && candidate.type.startsWith("image/"));
        const file = item?.getAsFile();
        if (!file) return false;
        event.preventDefault();
        void uploadAndInsert(file, "image");
        return true;
      },
      handleDrop: (_view, event) => {
        const file = Array.from(event.dataTransfer?.files || [])[0];
        if (!file) return false;
        event.preventDefault();
        void uploadAndInsert(file, file.type.startsWith("image/") ? "image" : "attachment");
        return true;
      },
    },
    onUpdate: ({ editor: current }) => onChange(current.getMarkdown()),
  });

  useEffect(() => {
    if (!editor || mode !== "document") return;
    const current = editor.getMarkdown();
    if (value !== current && value.trim() !== current.trim()) editor.commands.setContent(value, { contentType: "markdown", emitUpdate: false });
  }, [editor, mode, value]);

  useEffect(() => {
    if (!editor) return;
    const updateSelection = () => {
      const { from, to } = editor.state.selection;
      const text = from === to ? "" : editor.state.doc.textBetween(from, to, "\n").trim();
      setSelectedText(text.slice(0, 20_000));
    };
    editor.on("selectionUpdate", updateSelection);
    return () => { editor.off("selectionUpdate", updateSelection); };
  }, [editor]);

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") { event.preventDefault(); setFindOpen(true); }
      if (event.key === "Escape" && findOpen) setFindOpen(false);
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [findOpen]);

  async function uploadAndInsert(file: File, kind: "image" | "attachment") {
    const asset = await onUpload(file, kind);
    if (!editor || !asset) return;
    if (kind === "image") editor.chain().focus().setImage({ src: asset.markdown_path, alt: file.name }).run();
    else editor.chain().focus().insertContent(`[${file.name}](${asset.markdown_path})`).run();
  }
  function insertLink() {
    const href = window.prompt("链接地址", "https://");
    if (href && editor) editor.chain().focus().setLink({ href }).run();
  }
  function replaceAll() {
    if (!editor || !findQuery) return;
    const next = editor.getMarkdown().split(findQuery).join(replaceQuery);
    editor.commands.setContent(next, { contentType: "markdown" });
  }
  const tableActive = Boolean(editor?.isActive("table"));
  const table = (command: string) => {
    if (!editor) return;
    const chain = editor.chain().focus();
    if (command === "addRowAfter") chain.addRowAfter().run();
    else if (command === "deleteRow") chain.deleteRow().run();
    else if (command === "addColumnAfter") chain.addColumnAfter().run();
    else if (command === "deleteColumn") chain.deleteColumn().run();
    else if (command === "toggleHeaderRow") chain.toggleHeaderRow().run();
    else if (command === "deleteTable") chain.deleteTable().run();
  };

  return <section className="note-doc-editor tiptap-note-editor">
    <header className="note-doc-toolbar">
      <div className="note-mode-switch" role="tablist" aria-label="笔记编辑模式">
        <button className={mode === "document" ? "active" : ""} onClick={() => onMode("document")} role="tab" aria-selected={mode === "document"}>文档</button>
        <button className={mode === "markdown" ? "active" : ""} onClick={() => onMode("markdown")} role="tab" aria-selected={mode === "markdown"}>Markdown</button>
      </div>
      <div className="note-format-actions">
        <ToolButton label="撤销" icon={<Undo2 />} onClick={() => editor?.chain().focus().undo().run()} />
        <ToolButton label="重做" icon={<Redo2 />} onClick={() => editor?.chain().focus().redo().run()} />
        <ToolButton label="查找与替换" icon={<Search />} onClick={() => setFindOpen((open) => !open)} />
        <span className="note-toolbar-divider" />
        <select className="note-heading-select" aria-label="标题级别" defaultValue="" onChange={(event) => { const level = Number(event.target.value); if (level && editor) editor.chain().focus().toggleHeading({ level: level as 1 | 2 | 3 | 4 | 5 | 6 }).run(); event.currentTarget.value = ""; }}><option value="" disabled>标题</option>{[1, 2, 3, 4, 5, 6].map((level) => <option key={level} value={level}>H{level}</option>)}</select>
        <ToolButton label="粗体" icon={<Bold />} onClick={() => editor?.chain().focus().toggleBold().run()} />
        <ToolButton label="斜体" icon={<Italic />} onClick={() => editor?.chain().focus().toggleItalic().run()} />
        <ToolButton label="删除线" icon={<Strikethrough />} onClick={() => editor?.chain().focus().toggleStrike().run()} />
        <ToolButton label="引用" icon={<Quote />} onClick={() => editor?.chain().focus().toggleBlockquote().run()} />
        <ToolButton label="代码" icon={<Code2 />} onClick={() => editor?.chain().focus().toggleCode().run()} />
        <ToolButton label="链接" icon={<Link2 />} onClick={insertLink} />
        <ToolButton label="项目列表" icon={<List />} onClick={() => editor?.chain().focus().toggleBulletList().run()} />
        <ToolButton label="有序列表" icon={<ListOrdered />} onClick={() => editor?.chain().focus().toggleOrderedList().run()} />
        <ToolButton label="表格" icon={<Table2 />} onClick={() => editor?.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()} />
        <ToolButton label="公式" icon={<Sigma />} onClick={() => editor?.chain().focus().insertContent("$$\n公式\n$$").run()} />
        <ToolButton label="图片" icon={<ImageIcon />} onClick={() => imageInput.current?.click()} />
        <ToolButton label="附件" icon={<Paperclip />} onClick={() => attachmentInput.current?.click()} />
        <ToolButton label="Mermaid" icon={<Braces />} onClick={() => editor?.chain().focus().toggleCodeBlock().run()} />
        <input ref={imageInput} className="visually-hidden" type="file" accept="image/*" onChange={(event) => { void uploadAndInsert(event.target.files?.[0] as File, "image"); event.currentTarget.value = ""; }} />
        <input ref={attachmentInput} className="visually-hidden" type="file" onChange={(event) => { void uploadAndInsert(event.target.files?.[0] as File, "attachment"); event.currentTarget.value = ""; }} />
      </div>
    </header>
    {findOpen && <div className="note-find-bar"><input autoFocus value={findQuery} onChange={(event) => setFindQuery(event.target.value)} placeholder="查找…" /><input value={replaceQuery} onChange={(event) => setReplaceQuery(event.target.value)} placeholder="替换为…" /><button className="secondary-button" onClick={replaceAll}>全部替换</button><button className="icon-button" onClick={() => setFindOpen(false)} aria-label="关闭查找">×</button></div>}
    {tableActive && <div className="tiptap-table-toolbar" role="toolbar" aria-label="表格操作"><span>当前表格</span><button onClick={() => table("addRowAfter")}>+ 行</button><button onClick={() => table("deleteRow")}>− 行</button><button onClick={() => table("addColumnAfter")}>+ 列</button><button onClick={() => table("deleteColumn")}>− 列</button><button onClick={() => table("toggleHeaderRow")}>切换表头</button><button className="danger-button" onClick={() => table("deleteTable")}>删除表格</button></div>}
    {suggestion && <SuggestionPanel suggestion={suggestion} onAccept={onAcceptSuggestion} onReject={onRejectSuggestion} onRegenerate={onRegenerateSuggestion} />}
    {mode === "markdown" ? <textarea className="note-source-editor" value={value} onChange={(event) => onChange(event.target.value)} aria-label="Markdown 源码" spellCheck={false} /> : <div className="tiptap-note-stage"><EditorContent editor={editor} />{selectedText && <div className="note-selection-actions" role="toolbar" aria-label="选中文字操作"><span>{selectedText.length > 72 ? `${selectedText.slice(0, 71)}…` : selectedText}</span><button type="button" onClick={() => void navigator.clipboard.writeText(selectedText)}><Copy />复制</button><button type="button" onClick={() => onAgentAction("ask", selectedText)}>询问 Agent</button><button type="button" onClick={() => onAgentAction("polish", selectedText)}><Sparkles />润色</button><button type="button" onClick={() => onAgentAction("rewrite", selectedText)}>重写</button><button type="button" onClick={() => onAgentAction("expand", selectedText)}>扩写</button><button type="button" onClick={() => onAgentAction("shorten", selectedText)}>缩写</button><button type="button" onClick={() => onAgentAction("summarize", selectedText)}>总结</button><button type="button" onClick={() => onAgentAction("translate", selectedText)}>翻译</button><button type="button" onClick={() => onAgentAction("latex", selectedText)}>转 LaTeX</button></div>}</div>}
  </section>;
}

function ToolButton({ label, icon, onClick }: { label: string; icon: ReactNode; onClick: () => void }) { return <button type="button" className="note-toolbar-button" title={label} aria-label={label} onClick={onClick}>{icon}</button>; }

function SuggestionPanel({ suggestion, onAccept, onReject, onRegenerate }: { suggestion: NoteEditorSuggestion; onAccept: () => void; onReject: () => void; onRegenerate: () => void }) {
  const labels: Record<NoteEditorAction, string> = { ask: "询问", polish: "润色", rewrite: "重写", expand: "扩写", shorten: "缩写", summarize: "总结", translate: "翻译", latex: "转 LaTeX" };
  const running = suggestion.status === "running";
  return <section className={`note-suggestion ${suggestion.status}`} aria-live="polite"><header className="note-suggestion-header"><div><Sparkles aria-hidden="true" /><strong>Agent 建议 · {labels[suggestion.action]}</strong></div><span>{running ? "正在生成" : suggestion.status === "ready" ? "待确认" : "生成失败"}</span></header><div className="note-suggestion-diff"><div className="note-suggestion-side removed"><span>原文</span><del>{suggestion.original}</del></div><div className="note-suggestion-side added"><span>建议结果</span>{suggestion.answer ? <ins>{suggestion.answer}</ins> : <p className="note-suggestion-placeholder">正在等待 Agent 输出…</p>}</div></div>{suggestion.error && <p className="note-suggestion-error">{suggestion.error}</p>}<footer className="note-suggestion-actions">{suggestion.status === "ready" && <button className="primary-action" type="button" onClick={onAccept}>接受修改</button>}{!running && <button className="secondary-button" type="button" onClick={onRegenerate}><span aria-hidden="true">↻</span>重新生成</button>}<button className="secondary-button" type="button" onClick={onReject}>{running ? "停止生成" : "拒绝"}</button></footer></section>;
}
