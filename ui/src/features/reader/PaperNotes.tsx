import { useEffect, useState } from "react";
import { Check, Pencil, Trash2, X } from "lucide-react";
import type { PaperNote } from "../../types";

type Props = {
  notes: PaperNote[];
  busy: string;
  onUpdate: (note: PaperNote) => Promise<void>;
  onDelete: (note: PaperNote) => Promise<void>;
  onPage: (page: number) => void;
};

export function PaperNotes({ notes, busy, onUpdate, onDelete, onPage }: Props) {
  if (!notes.length) return <p className="quiet-empty">暂无笔记。</p>;
  return <div className="paper-notes">{notes.map((note) => <NoteCard key={note.note_id} note={note} busy={busy === note.note_id} onUpdate={onUpdate} onDelete={onDelete} onPage={onPage} />)}</div>;
}

function NoteCard({ note, busy, onUpdate, onDelete, onPage }: { note: PaperNote; busy: boolean; onUpdate: Props["onUpdate"]; onDelete: Props["onDelete"]; onPage: Props["onPage"] }) {
  const [editing, setEditing] = useState(false);
  const [markdown, setMarkdown] = useState(note.note_markdown);
  useEffect(() => setMarkdown(note.note_markdown), [note.note_markdown]);
  async function save() {
    if (!markdown.trim()) return;
    await onUpdate({ ...note, note_markdown: markdown.trim() });
    setEditing(false);
  }
  return <article className="paper-note">
    <header><button className="note-page" type="button" disabled={!note.page} onClick={() => note.page && onPage(note.page)}>第 {note.page || "?"} 页</button><span>rev {note.revision}</span><span className="note-actions"><button type="button" aria-label="编辑笔记" onClick={() => setEditing(true)}><Pencil aria-hidden="true" /></button><button type="button" aria-label="删除笔记" onClick={() => window.confirm("删除这条论文笔记？") && void onDelete(note)}><Trash2 aria-hidden="true" /></button></span></header>
    {editing ? <><textarea autoFocus value={markdown} onChange={(event) => setMarkdown(event.target.value)} /><div className="note-edit-actions"><button type="button" onClick={() => { setMarkdown(note.note_markdown); setEditing(false); }}><X aria-hidden="true" />取消</button><button type="button" disabled={busy || !markdown.trim()} onClick={() => void save()}><Check aria-hidden="true" />保存</button></div></> : <p>{note.note_markdown}</p>}
    {note.selected_text && <details><summary>查看原文选区</summary><blockquote>{note.selected_text}</blockquote></details>}
  </article>;
}
