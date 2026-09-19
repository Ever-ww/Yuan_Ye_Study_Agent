import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { BookOpen, ChevronLeft, ChevronRight, Copy, Languages, MessageSquareText, Minus, NotebookPen, Plus, Search } from "lucide-react";
import { Document, Page, pdfjs } from "react-pdf";
import type { PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WorkbenchShell } from "../../app/WorkbenchShell";
import { useTheme } from "../../app/ThemeProvider";
import { useGatewayApi } from "../../shared/api/context";
import { CommandPalette } from "../../shared/ui/CommandPalette";
import { StatusMark } from "../../shared/ui/StatusMark";
import type { PaperListItem, PaperNote } from "../../types";
import { PaperNotes } from "./PaperNotes";
import { PdfSearchPanel, type PdfSearchResult } from "./PdfSearch";
import { PdfThumbnails } from "./PdfThumbnails";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

export function ReaderPage() {
  const api = useGatewayApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setPreference } = useTheme();
  const [params, setParams] = useSearchParams();
  const paperId = params.get("paper") || undefined;
  const [filter, setFilter] = useState("");
  const deferredFilter = useDeferredValue(filter);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(0);
  const [scale, setScale] = useState(1.05);
  const [pdfUrl, setPdfUrl] = useState("");
  const [pdfDocument, setPdfDocument] = useState<PDFDocumentProxy | null>(null);
  const [selectedText, setSelectedText] = useState("");
  const [note, setNote] = useState("");
  const [noteBusy, setNoteBusy] = useState("");
  const [message, setMessage] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<PdfSearchResult[]>([]);
  const [searchProgress, setSearchProgress] = useState("");
  const [commandsOpen, setCommandsOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const papers = useQuery({ queryKey: ["library", "papers"], queryFn: () => api.papers() });
  const current = papers.data?.find((item) => item.paper_id === paperId);
  const notes = useQuery({ queryKey: ["library", "notes", paperId], queryFn: () => api.paperNotes(paperId!), enabled: Boolean(paperId) });
  const visible = useMemo(() => {
    const query = deferredFilter.trim().toLocaleLowerCase();
    if (!query) return papers.data || [];
    return (papers.data || []).filter((item) => `${item.title} ${item.authors.map((author) => author.display_name).join(" ")} ${item.tags.join(" ")} ${item.publication_year || ""}`.toLocaleLowerCase().includes(query));
  }, [deferredFilter, papers.data]);

  useEffect(() => { if (!paperId && visible[0]) setParams({ paper: visible[0].paper_id }, { replace: true }); }, [paperId, setParams, visible]);
  useEffect(() => {
    const saved = paperId ? readReaderPosition(paperId) : null;
    setPage(saved?.page || 1); setScale(saved?.scale || 1.05); setPages(0); setPdfDocument(null); setSelectedText(""); setNote(""); setSearchResults([]); setSearchProgress("");
    if (!paperId || !current?.has_pdf) { setPdfUrl(""); return; }
    let revoked = ""; let active = true;
    api.paperPdfUrl(paperId).then((url) => {
      if (!active) { if (url.startsWith("blob:")) URL.revokeObjectURL(url); return; }
      setPdfUrl(url); if (url.startsWith("blob:")) revoked = url;
    }).catch((error) => setMessage(error.message));
    return () => { active = false; if (revoked) URL.revokeObjectURL(revoked); };
  }, [api, current?.has_pdf, paperId]);
  useEffect(() => {
    if (paperId) window.localStorage.setItem(
      `yyagent.web.reader.${paperId}`,
      JSON.stringify({ page, scale }),
    );
  }, [page, paperId, scale]);

  function captureSelection() { const text = window.getSelection()?.toString().trim().slice(0, 20_000) || ""; setSelectedText(text); if (text) setInspectorOpen(true); }
  function handoff(kind: "ask" | "translate") {
    if (!current || !current.content_hash || !selectedText) return;
    const prompt = kind === "translate" ? `请翻译并解释下面这段论文内容（来源：${current.title}，第 ${page} 页）：\n\n${selectedText}` : `请基于下面的论文选区回答我的问题（来源：${current.title}，第 ${page} 页）：\n\n${selectedText}\n\n我的问题：`;
    window.sessionStorage.setItem("yyagent.web.agent-prefill", prompt);
    window.sessionStorage.setItem("yyagent.web.agent-context", JSON.stringify({ source: "read", resource: { kind: "paper", paper_id: current.paper_id, logical_path: null, content_hash: current.content_hash }, selection: { selected_text: selectedText, page, start_line: null, end_line: null } }));
    navigate("/agent");
  }
  async function saveNote() {
    if (!paperId || !note.trim()) return;
    setNoteBusy("new");
    try { await api.createPaperNote(paperId, { page, selected_text: selectedText, locator: { page }, note_markdown: note.trim() }); setNote(""); setMessage("笔记已保存到 Reference 数据库；不会自动进入长期 Memory。"); await refreshNotes(); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setNoteBusy(""); }
  }
  async function updateNote(value: PaperNote) {
    if (!paperId) return; setNoteBusy(value.note_id);
    try { await api.updatePaperNote(paperId, value); setMessage("笔记已更新。"); await refreshNotes(); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); throw error; }
    finally { setNoteBusy(""); }
  }
  async function deleteNote(value: PaperNote) {
    if (!paperId) return; setNoteBusy(value.note_id);
    try { await api.deletePaperNote(paperId, value.note_id); setMessage("笔记已删除。"); await refreshNotes(); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setNoteBusy(""); }
  }
  function refreshNotes() { return queryClient.invalidateQueries({ queryKey: ["library", "notes", paperId] }); }

  return <WorkbenchShell projectName="Read" modelControls={<span className="mode-chip">PAPER LIBRARY</span>} inspectorOpen={inspectorOpen} onToggleInspector={() => setInspectorOpen((value) => !value)} onOpenCommands={() => setCommandsOpen(true)} sidebarLabel="论文库" inspectorLabel="论文与笔记" sidebar={<PaperSidebar papers={visible} current={paperId} filter={filter} onFilter={setFilter} onSelect={(id) => setParams({ paper: id })} />} inspector={<ReaderInspector current={current} page={page} selectedText={selectedText} note={note} noteBusy={noteBusy} notes={notes.data || []} onNote={setNote} onSaveNote={saveNote} onUpdateNote={updateNote} onDeleteNote={deleteNote} onPage={setPage} onHandoff={handoff} />} footer={<><StatusMark state={papers.isError ? "warning" : "ok"} label={papers.isError ? "论文库不可用" : "Reference Store"} /><span className="status-spacer" /><span>{papers.data?.length || 0} 篇论文</span></>}>
    <div className="reader-workspace">
      <header className="reader-toolbar"><button aria-label="上一页" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}><ChevronLeft aria-hidden="true" /></button><label>第 <input aria-label="PDF 页码" type="number" min={1} max={pages || 1} value={page} onChange={(event) => setPage(Math.max(1, Math.min(pages || 1, Number(event.target.value))))} /> / {pages || "—"} 页</label><button aria-label="下一页" disabled={!pages || page >= pages} onClick={() => setPage((value) => value + 1)}><ChevronRight aria-hidden="true" /></button><button className={searchOpen ? "active" : ""} onClick={() => setSearchOpen((value) => !value)}><Search aria-hidden="true" />全文搜索</button><span className="toolbar-spacer" /><button aria-label="缩小" onClick={() => setScale((value) => Math.max(.6, value - .1))}><Minus aria-hidden="true" /></button><span>{Math.round(scale * 100)}%</span><button aria-label="放大" onClick={() => setScale((value) => Math.min(2.2, value + .1))}><Plus aria-hidden="true" /></button><button className="selection-button" onClick={captureSelection}>使用选区</button></header>
      {message && <div className="operation-message" role="status">{message}</div>}
      {searchOpen && <PdfSearchPanel document={pdfDocument} query={searchQuery} results={searchResults} progress={searchProgress} onQuery={setSearchQuery} onResults={setSearchResults} onProgress={setSearchProgress} onPage={(value) => { setPage(value); setSearchOpen(false); }} onClose={() => setSearchOpen(false)} />}
      {current?.has_pdf && pdfUrl ? <Document className="reader-document" file={pdfUrl} onLoadSuccess={(value) => { setPdfDocument(value); setPages(value.numPages); setPage((selected) => Math.min(selected, value.numPages)); }} loading={<div className="page-state">正在加载 PDF…</div>} error={<div className="page-state error">PDF 无法渲染。请检查文件是否存在或是否需要 OCR。</div>}><PdfThumbnails pages={pages} current={page} onPage={setPage} /><div className="pdf-stage" onMouseUp={captureSelection}><Page pageNumber={page} scale={scale} /></div></Document> : <div className="pdf-stage"><div className="agent-empty"><p className="eyebrow">READ WORKSPACE</p><h1>{current ? "这篇论文还没有可读取的本地 PDF。" : "从论文库选择一篇文献。"}</h1><p>PDF 由 Gateway 按范围请求提供，宿主机路径不会暴露给浏览器。</p></div></div>}
    </div>
    <CommandPalette open={commandsOpen} onClose={() => setCommandsOpen(false)} onNewSession={() => navigate("/agent")} onAddProject={() => navigate("/agent")} onTheme={setPreference} />
  </WorkbenchShell>;
}

function ReaderInspector({ current, page, selectedText, note, noteBusy, notes, onNote, onSaveNote, onUpdateNote, onDeleteNote, onPage, onHandoff }: { current?: PaperListItem; page: number; selectedText: string; note: string; noteBusy: string; notes: PaperNote[]; onNote: (value: string) => void; onSaveNote: () => Promise<void>; onUpdateNote: (note: PaperNote) => Promise<void>; onDeleteNote: (note: PaperNote) => Promise<void>; onPage: (page: number) => void; onHandoff: (kind: "ask" | "translate") => void }) {
  return <div className="inspector-content"><header><div><span>REFERENCE</span><h2>{current?.title || "选择论文"}</h2></div><BookOpen aria-hidden="true" /></header>{current && <><section className="inspector-section"><h3>元数据</h3><p>{current.authors.map((author) => author.display_name).join(" · ") || "作者未知"}</p><p>{current.publication_year || "年份未知"} · {current.venue || "未记录期刊"}</p></section><section className="inspector-section"><h3>当前选区</h3><p>{selectedText || "在 PDF 文本层中选择内容，然后点击“使用选区”。"}</p>{selectedText && <div className="reader-actions"><button onClick={() => onHandoff("ask")}><MessageSquareText aria-hidden="true" />Ask Agent</button><button onClick={() => onHandoff("translate")}><Languages aria-hidden="true" />Translate</button><button onClick={() => void navigator.clipboard.writeText(selectedText)}><Copy aria-hidden="true" />复制</button></div>}</section><section className="inspector-section"><h3>保存笔记</h3><textarea value={note} onChange={(event) => onNote(event.target.value)} placeholder="记录判断、引用用途或待验证问题" /><button className="secondary-button" disabled={!note.trim() || noteBusy === "new"} onClick={() => void onSaveNote()}><NotebookPen aria-hidden="true" />{noteBusy === "new" ? "保存中…" : "保存笔记"}</button></section><section className="inspector-section"><h3>已有笔记</h3><PaperNotes notes={notes} busy={noteBusy} onUpdate={onUpdateNote} onDelete={onDeleteNote} onPage={onPage} /></section></>}</div>;
}

function PaperSidebar({ papers, current, filter, onFilter, onSelect }: { papers: PaperListItem[]; current?: string; filter: string; onFilter: (value: string) => void; onSelect: (id: string) => void }) {
  const parent = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count: papers.length, getScrollElement: () => parent.current, estimateSize: () => 72, overscan: 8 });
  return <div className="sidebar-layout"><div className="sidebar-heading"><div><span>Reference Store</span><h2>论文</h2></div></div><label className="paper-search"><Search aria-hidden="true" /><input aria-label="筛选论文" value={filter} onChange={(event) => onFilter(event.target.value)} placeholder="标题、作者、年份或标签" /></label><div className="paper-list" ref={parent}><div style={{ height: virtual.getTotalSize(), position: "relative" }}>{virtual.getVirtualItems().map((row) => { const paper = papers[row.index]; return <button key={paper.paper_id} className={current === paper.paper_id ? "active" : ""} style={{ position: "absolute", transform: `translateY(${row.start}px)`, height: row.size, width: "100%" }} onClick={() => onSelect(paper.paper_id)}><strong>{paper.title}</strong><small>{paper.authors[0]?.display_name || "作者未知"} · {paper.publication_year || "—"}</small></button>; })}</div></div></div>;
}

function readReaderPosition(paperId: string): { page: number; scale: number } | null {
  try {
    const value = JSON.parse(window.localStorage.getItem(`yyagent.web.reader.${paperId}`) || "null") as { page?: unknown; scale?: unknown } | null;
    if (!value || !Number.isInteger(value.page) || typeof value.scale !== "number") return null;
    return {
      page: Math.max(1, Number(value.page)),
      scale: Math.max(.6, Math.min(2.2, value.scale)),
    };
  } catch { return null; }
}
