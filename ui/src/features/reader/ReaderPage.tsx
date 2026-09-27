import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronLeft, ChevronRight, ChevronUp, Copy, Languages, LoaderCircle, MessageSquareText, Minus, NotebookPen, PanelRightClose, PanelRightOpen, Plus, RotateCcw, Search, Trash2, Upload } from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
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
import type { PaperInlineAnswer, PaperListItem } from "../../types";
import { PdfSearchPanel, type PdfSearchResult } from "./PdfSearch";
import { PdfThumbnails } from "./PdfThumbnails";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
type SelectionState = { text: string; nearby: string; locator: { page: number; x: number; y: number; width: number; height: number } };
type TranslationEngine = "baidu" | "google" | "youdao" | "360" | "llm";
type TranslationState = { status: "loading" | "completed" | "failed"; engine: TranslationEngine; text: string; error?: string };

export function ReaderPage() {
  const api = useGatewayApi(), navigate = useNavigate();
  const { setPreference } = useTheme();
  const [params, setParams] = useSearchParams();
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const projectId = params.get("project") || window.localStorage.getItem("yyagent.web.selected-project") || projects.data?.[0]?.project_id;
  const paperId = params.get("paper") || undefined;
  const [filter, setFilter] = useState(""), deferredFilter = useDeferredValue(filter);
  const [page, setPageState] = useState(1), [pages, setPages] = useState(0), [scale, setScale] = useState(1.05);
  const [pdfUrl, setPdfUrl] = useState(""), [pdfDocument, setPdfDocument] = useState<PDFDocumentProxy | null>(null);
  const [selection, setSelection] = useState<SelectionState | null>(null);
  const [translationEngine, setTranslationEngine] = useState<TranslationEngine>(() => {
    const saved = localStorage.getItem("yyagent.reader.translation-engine");
    return ["baidu", "google", "youdao", "360", "llm"].includes(saved || "") ? saved as TranslationEngine : "baidu";
  });
  const [translation, setTranslation] = useState<TranslationState | null>(null);
  const [translationFontSize, setTranslationFontSize] = useState(() => { const saved = Number(localStorage.getItem("yyagent.reader.translation-font-size")); return [12, 14, 16, 18].includes(saved) ? saved : 14; });
  const [autoTranslate, setAutoTranslate] = useState(() => localStorage.getItem("yyagent.reader.auto-translate") !== "false");
  const [inspectorOpen, setInspectorOpen] = useState(() => localStorage.getItem("yyagent.reader.translation-panel") !== "closed");
  const [inspectorWidth, setInspectorWidth] = useState(() => { const saved = Number(localStorage.getItem("yyagent.reader.translation-panel-width")); return Number.isFinite(saved) ? Math.max(248, Math.min(560, saved)) : 320; });
  const [message, setMessage] = useState(""), [searchOpen, setSearchOpen] = useState(false), [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<PdfSearchResult[]>([]), [searchProgress, setSearchProgress] = useState("");
  const [commandsOpen, setCommandsOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(() => localStorage.getItem("yyagent.reader.sidebar") !== "closed");
  const [summaryOpen, setSummaryOpen] = useState(true), [askOpen, setAskOpen] = useState(false), [question, setQuestion] = useState(""), [askBusy, setAskBusy] = useState(false), [selectionMenuOpen, setSelectionMenuOpen] = useState(false);
  const [importBusy, setImportBusy] = useState(false), [autoSummary, setAutoSummary] = useState(() => localStorage.getItem("yyagent.reader.auto-summary") !== "false");
  const pdfStageRef = useRef<HTMLDivElement>(null), pageRefs = useRef<Record<number, HTMLDivElement | null>>({}), importRef = useRef<HTMLInputElement>(null), translationRequest = useRef(0), translationAbort = useRef<AbortController | null>(null), pageNavigation = useRef(false);

  function setPage(value: number | ((current: number) => number)) {
    pageNavigation.current = true;
    setPageState(value);
  }

  const papers = useQuery({ queryKey: ["library", "papers", projectId], queryFn: () => api.papers(projectId!), enabled: Boolean(projectId) });
  const current = papers.data?.find((item) => item.paper_id === paperId);
  const summary = useQuery({ queryKey: ["library", "summary", projectId, paperId], queryFn: () => api.paperSummary(paperId!, projectId!), enabled: Boolean(projectId && paperId), refetchInterval: (query) => ["queued", "running"].includes(query.state.data?.status || "") ? 1200 : false });
  const inlineAnswers = useQuery({ queryKey: ["library", "inline", projectId, paperId], queryFn: () => api.paperInlineAnswers(paperId!, projectId!), enabled: Boolean(projectId && paperId), refetchInterval: (query) => query.state.data?.some((item) => item.status === "pending") ? 1200 : false });
  const visible = useMemo(() => { const query = deferredFilter.trim().toLocaleLowerCase(); return !query ? papers.data || [] : (papers.data || []).filter((item) => `${item.title} ${item.authors.map((author) => author.display_name).join(" ")} ${item.tags.join(" ")} ${item.publication_year || ""}`.toLocaleLowerCase().includes(query)); }, [deferredFilter, papers.data]);
  useEffect(() => {
    const onDeleted = (event: Event) => {
      const deletedId = (event as CustomEvent<{ paperId?: string }>).detail?.paperId;
      void papers.refetch();
      if (deletedId && deletedId === paperId && projectId) setParams({ project: projectId });
    };
    const onError = (event: Event) => setMessage(String((event as CustomEvent<string>).detail || "论文操作失败"));
    window.addEventListener("yy-paper-deleted", onDeleted);
    window.addEventListener("yy-paper-error", onError);
    return () => { window.removeEventListener("yy-paper-deleted", onDeleted); window.removeEventListener("yy-paper-error", onError); };
  }, [paperId, papers.refetch, projectId, setParams]);

  useEffect(() => { if (!projectId) return; if (!params.get("project")) setParams({ project: projectId, ...(paperId ? { paper: paperId } : {}) }, { replace: true }); else if (!paperId && visible[0]) setParams({ project: projectId, paper: visible[0].paper_id }, { replace: true }); }, [paperId, params, projectId, setParams, visible]);
  useEffect(() => {
    const saved = paperId ? readReaderPosition(paperId) : null;
    setPageState(saved?.page || 1); setScale(saved?.scale || 1.05); setPages(0); setPdfDocument(null); setSelection(null); setSelectionMenuOpen(false); setTranslation(null); translationRequest.current += 1; translationAbort.current?.abort(); translationAbort.current = null; setSearchResults([]); setSearchProgress(""); setAskOpen(false);
    if (!projectId || !paperId || !current?.has_pdf) { setPdfUrl(""); return; }
    let revoked = "", active = true;
    api.paperPdfUrl(paperId, projectId).then((url) => { if (!active) { if (url.startsWith("blob:")) URL.revokeObjectURL(url); return; } setPdfUrl(url); if (url.startsWith("blob:")) revoked = url; }).catch((error) => setMessage(error.message));
    return () => { active = false; if (revoked) URL.revokeObjectURL(revoked); };
  }, [api, current?.has_pdf, paperId, projectId]);
  useEffect(() => { if (paperId) localStorage.setItem(`yyagent.web.reader.${paperId}`, JSON.stringify({ page, scale })); }, [page, paperId, scale]);

  // Keep the extracted selection in React state, but let the browser/PDF.js
  // selection remain a temporary visual state. Any click outside the action
  // popover clears it without discarding the translation context.
  useEffect(() => {
    const clearNativeSelection = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest(".selection-popover")) return;
      window.getSelection()?.removeAllRanges();
      setSelectionMenuOpen(false);
    };
    document.addEventListener("mousedown", clearNativeSelection);
    return () => document.removeEventListener("mousedown", clearNativeSelection);
  }, []);

  useEffect(() => {
    if (!pageNavigation.current) return;
    pageNavigation.current = false;
    if (!pageRefs.current[page]) return;
    pageRefs.current[page]?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [page]);

  useEffect(() => {
    if (!autoTranslate || !selection?.text) return;
    void translateSelection();
    // The selected text and engine are the inputs to automatic translation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoTranslate, selection?.text, selection?.nearby, translationEngine, current?.content_hash]);

  function handlePdfScroll() {
    const container = pdfStageRef.current;
    if (!container) return;
    const top = container.getBoundingClientRect().top + 24;
    let closest = page;
    let distance = Number.POSITIVE_INFINITY;
    container.querySelectorAll<HTMLElement>("[data-pdf-page]").forEach((element) => {
      const distanceToTop = Math.abs(element.getBoundingClientRect().top - top);
      if (distanceToTop < distance) { distance = distanceToTop; closest = Number(element.dataset.pdfPage || page); }
    });
    if (closest !== page) setPageState(closest);
  }

  function captureSelection(pageNumber: number, pageElement: HTMLDivElement | null) {
    const selected = window.getSelection(), text = selected?.toString().trim().slice(0, 20_000) || "";
    if (!text || !selected?.rangeCount || !pageElement) return;
    if (selection?.text === text && selection.locator.page === pageNumber) { setSelectionMenuOpen(true); return; }
    const rect = selected.getRangeAt(0).getBoundingClientRect(), pageRect = pageElement.getBoundingClientRect(), pageText = pageElement.querySelector(".textLayer")?.textContent || "", index = pageText.indexOf(text);
    setPageState(pageNumber);
    translationRequest.current += 1;
    translationAbort.current?.abort();
    translationAbort.current = null;
    setSelection({ text, nearby: index >= 0 ? pageText.slice(Math.max(0, index - 1200), index + text.length + 1200) : pageText.slice(0, 2400), locator: { page: pageNumber, x: clamp((rect.left - pageRect.left) / pageRect.width), y: clamp((rect.top - pageRect.top) / pageRect.height), width: clamp(rect.width / pageRect.width), height: clamp(rect.height / pageRect.height) } });
    setSelectionMenuOpen(true); setTranslation(null); setAskOpen(false); setQuestion("");
  }

  async function askSelection() {
    if (!projectId || !current?.content_hash || !selection || !question.trim()) return;
    setAskBusy(true); setMessage("");
    try {
      const created = await api.startRun({ projectId, task: question.trim(), modelProfileId: "default", reasoningEffort: "low", uiContext: { source: "read", resource: { kind: "paper", paper_id: current.paper_id, logical_path: null, content_hash: current.content_hash }, selection: { selected_text: selection.text, page: selection.locator.page, start_line: null, end_line: null, nearby_context: selection.nearby, locator: selection.locator } } });
      await api.createPaperInlineQuestion(current.paper_id, projectId, { run_id: created.run_id, page: selection.locator.page, selected_text: selection.text, nearby_context: selection.nearby, locator: selection.locator, question: question.trim() });
      window.getSelection()?.removeAllRanges(); setAskOpen(false); setSelectionMenuOpen(false); setQuestion(""); await inlineAnswers.refetch();
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); } finally { setAskBusy(false); }
  }

  async function translateSelection(engine: TranslationEngine = translationEngine) {
    if (!projectId || !current?.content_hash || !selection?.text) return;
    const requestId = ++translationRequest.current;
    translationAbort.current?.abort();
    const abortController = new AbortController();
    translationAbort.current = abortController;
    setTranslation({ status: "loading", engine, text: "" });
    localStorage.setItem("yyagent.reader.translation-engine", engine);
    try {
      const text = engine === "llm"
        ? await api.translateLlm(projectId, selection.text, (partial) => { if (requestId === translationRequest.current) setTranslation({ status: "loading", engine, text: partial }); }, "zh-CN", "default", abortController.signal)
        : (await api.translate(selection.text, engine)).text;
      if (requestId === translationRequest.current) setTranslation({ status: "completed", engine, text });
    } catch (error) {
      if (abortController.signal.aborted) return;
      if (requestId === translationRequest.current) setTranslation({ status: "failed", engine, text: "", error: error instanceof Error ? error.message : String(error) });
    } finally {
      if (translationAbort.current === abortController) translationAbort.current = null;
    }
  }

  async function importFiles(files: File[]) {
    if (!projectId || !files.length) return;
    setImportBusy(true); setMessage("");
    try { let latest: PaperListItem | null = null; for (const file of files.filter((item) => item.type === "application/pdf" || item.name.toLowerCase().endsWith(".pdf"))) latest = (await api.importPaper(file, projectId, autoSummary, "default", "low")).paper; await papers.refetch(); if (latest) setParams({ project: projectId, paper: latest.paper_id }); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); } finally { setImportBusy(false); }
  }
  function toggleSidebar() { setSidebarOpen((value) => { localStorage.setItem("yyagent.reader.sidebar", value ? "closed" : "open"); return !value; }); }
  function toggleInspector() { setInspectorOpen((value) => { localStorage.setItem("yyagent.reader.translation-panel", value ? "closed" : "open"); return !value; }); }

  return <WorkbenchShell projectName={projects.data?.find((item) => item.project_id === projectId)?.name || "Read"} modelControls={<span className="mode-chip">PAPER LIBRARY</span>} sidebarOpen={sidebarOpen} onToggleSidebar={toggleSidebar} inspectorOpen={inspectorOpen} inspectorWidth={inspectorWidth} onInspectorWidthChange={(value) => { setInspectorWidth(value); localStorage.setItem("yyagent.reader.translation-panel-width", String(value)); }} onToggleInspector={toggleInspector} showInspectorToggle={false} inspectorEdgeControl={<button className="inspector-edge-button" type="button" onClick={toggleInspector} aria-label={inspectorOpen ? "收起翻译栏" : "展开翻译栏"} title={inspectorOpen ? "收起翻译栏" : "展开翻译栏"}>{inspectorOpen ? <PanelRightClose aria-hidden="true" /> : <PanelRightOpen aria-hidden="true" />}</button>} onOpenCommands={() => setCommandsOpen(true)} sidebarLabel="论文库" inspectorLabel="选区翻译" sidebar={<PaperSidebar papers={visible} current={paperId} filter={filter} autoSummary={autoSummary} importing={importBusy} onFilter={setFilter} onSelect={(id) => projectId && setParams({ project: projectId, paper: id })} onImport={() => importRef.current?.click()} onAutoSummary={(value) => { setAutoSummary(value); localStorage.setItem("yyagent.reader.auto-summary", String(value)); }} />} inspector={<ReaderInspector selection={selection} translation={translation} translationEngine={translationEngine} translationFontSize={translationFontSize} autoTranslate={autoTranslate} onAutoTranslate={(value) => { setAutoTranslate(value); localStorage.setItem("yyagent.reader.auto-translate", String(value)); }} onTranslationEngine={(value) => { setTranslationEngine(value); localStorage.setItem("yyagent.reader.translation-engine", value); }} onTranslationFontSize={(value) => { setTranslationFontSize(value); localStorage.setItem("yyagent.reader.translation-font-size", String(value)); }} onTranslate={() => { setMessage("正在重新翻译…"); void translateSelection(); }} />} footer={<><StatusMark state={papers.isError ? "warning" : "ok"} label={papers.isError ? "论文库不可用" : "Workspace Reference"} /><span className="status-spacer" /><span>{papers.data?.length || 0} 篇论文</span></>}>
    <input ref={importRef} hidden type="file" accept="application/pdf,.pdf" multiple onChange={(event) => { void importFiles(Array.from(event.target.files || [])); event.target.value = ""; }} />
    <div className="reader-workspace" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); void importFiles(Array.from(event.dataTransfer.files)); }}>
      <header className="reader-toolbar"><button aria-label="上一页" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}><ChevronLeft aria-hidden="true" /></button><label>第 <input aria-label="PDF 页码" type="number" min={1} max={pages || 1} value={page} onChange={(event) => setPage(Math.max(1, Math.min(pages || 1, Number(event.target.value))))} /> / {pages || "—"} 页</label><button aria-label="下一页" disabled={!pages || page >= pages} onClick={() => setPage((value) => value + 1)}><ChevronRight aria-hidden="true" /></button><button className={searchOpen ? "active" : ""} onClick={() => setSearchOpen((value) => !value)}><Search aria-hidden="true" />全文搜索</button><span className="toolbar-spacer" /><button aria-label="缩小" onClick={() => setScale((value) => Math.max(.6, value - .1))}><Minus aria-hidden="true" /></button><span>{Math.round(scale * 100)}%</span><button aria-label="放大" onClick={() => setScale((value) => Math.min(2.2, value + .1))}><Plus aria-hidden="true" /></button></header>
      {message && <div className="operation-message" role="status">{message}</div>}
      {searchOpen && <PdfSearchPanel document={pdfDocument} query={searchQuery} results={searchResults} progress={searchProgress} onQuery={setSearchQuery} onResults={setSearchResults} onProgress={setSearchProgress} onPage={(value) => { setPage(value); setSearchOpen(false); }} onClose={() => setSearchOpen(false)} />}
      {current?.has_pdf && pdfUrl ? <Document className="reader-document" file={pdfUrl} onLoadSuccess={(value) => { setPdfDocument(value); setPages(value.numPages); setPage((selected) => Math.min(selected, value.numPages)); }} loading={<div className="page-state">正在加载 PDF…</div>} error={<div className="page-state error">PDF 无法渲染，可能需要 OCR。</div>}><PdfThumbnails pages={pages} current={page} onPage={setPage} /><div className="pdf-stage" ref={pdfStageRef} onScroll={handlePdfScroll}><div className="pdf-page-stack">{Array.from({ length: pages }, (_, index) => { const pageNumber = index + 1; return <div className="pdf-page-wrap" data-pdf-page={pageNumber} ref={(element) => { pageRefs.current[pageNumber] = element; }} onMouseUp={(event) => captureSelection(pageNumber, event.currentTarget)} key={pageNumber}><Page pageNumber={pageNumber} scale={scale} />{selectionMenuOpen && selection?.locator.page === pageNumber && <SelectionMenu selection={selection} askOpen={askOpen} question={question} busy={askBusy} onClose={() => { window.getSelection()?.removeAllRanges(); setAskOpen(false); setSelectionMenuOpen(false); }} onNote={() => { window.getSelection()?.removeAllRanges(); setMessage("笔记功能暂未开放"); setSelectionMenuOpen(false); }} onQuestion={setQuestion} onAskOpen={setAskOpen} onAsk={() => void askSelection()} />}<InlineAnswerLayer answers={(inlineAnswers.data || []).filter((item) => item.page === pageNumber)} /></div>; })}</div></div><SummaryPanel value={summary.data} open={summaryOpen} onToggle={() => setSummaryOpen((value) => !value)} onStart={() => projectId && paperId && void api.startPaperSummary(paperId, projectId, "default", "low").then(() => summary.refetch())} /></Document> : <div className="pdf-stage"><div className="agent-empty"><p className="eyebrow">READ WORKSPACE</p><h1>{current ? "这篇论文还没有可读取的本地 PDF。" : "从论文库选择或导入一篇 PDF。"}</h1><p>只有从 Read 导入的文件会进入当前 Workspace 论文库。Agent 临时附件不会出现在这里。</p></div></div>}
    </div><CommandPalette open={commandsOpen} onClose={() => setCommandsOpen(false)} onNewSession={() => navigate("/agent")} onAddProject={() => navigate("/agent")} onTheme={setPreference} />
  </WorkbenchShell>;
}

function SelectionMenu({ selection, askOpen, question, busy, onClose, onNote, onQuestion, onAskOpen, onAsk }: { selection: SelectionState; askOpen: boolean; question: string; busy: boolean; onClose: () => void; onNote: () => void; onQuestion: (value: string) => void; onAskOpen: (value: boolean) => void; onAsk: () => void }) {
  const [copied, setCopied] = useState(false);
  const copySelection = async () => { try { await navigator.clipboard.writeText(selection.text); setCopied(true); window.setTimeout(() => setCopied(false), 1500); } catch { setCopied(false); } };
  const style = { left: `${Math.min(90, selection.locator.x * 100)}%`, top: `${Math.min(96, (selection.locator.y + selection.locator.height) * 100)}%` };
  return <div className="selection-popover" style={style} role="dialog" aria-label="PDF 选区操作">
    {askOpen ? <form onSubmit={(event) => { event.preventDefault(); onAsk(); }}><textarea autoFocus value={question} onChange={(event) => onQuestion(event.target.value)} placeholder="针对这段原文提问" /><footer><button type="button" onClick={onClose}>取消</button><button type="submit" disabled={!question.trim() || busy}>{busy ? "提交中…" : "询问"}</button></footer></form> : <>
      <div className="selection-actions"><button type="button" title="复制" aria-label="复制" onClick={() => { void copySelection(); onClose(); }}><Copy aria-hidden="true" />{copied ? "已复制" : "复制"}</button><button type="button" title="笔记" aria-label="笔记" onClick={onNote}><NotebookPen aria-hidden="true" />笔记</button><button type="button" title="询问" aria-label="询问" onClick={() => onAskOpen(true)}><MessageSquareText aria-hidden="true" />询问</button></div>
    </>}
  </div>;
}
function InlineAnswerLayer({ answers }: { answers: PaperInlineAnswer[] }) { const groups = useMemo(() => Object.entries(answers.reduce<Record<string, PaperInlineAnswer[]>>((result, item) => { const locator = item.locator as { x?: number; y?: number }; const key = `${item.selected_text_hash}:${Number(locator.x || 0).toFixed(4)}:${Number(locator.y || 0).toFixed(4)}`; (result[key] ||= []).push(item); return result; }, {})), [answers]); return <>{groups.map(([key, items]) => <InlineAnswerBubble key={key} items={items} />)}</>; }
function InlineAnswerBubble({ items }: { items: PaperInlineAnswer[] }) {
  const [open, setOpen] = useState(true), [index, setIndex] = useState(items.length - 1); useEffect(() => setIndex(items.length - 1), [items.length]);
  const item = items[Math.min(index, items.length - 1)], locator = item.locator as { x?: number; y?: number }, style = { left: `${Math.min(88, Number(locator.x || 0) * 100)}%`, top: `${Math.max(2, Number(locator.y || 0) * 100)}%` };
  if (!open) return <button className="inline-answer-pin" style={style} onClick={() => setOpen(true)} aria-label="展开原文问答"><MessageSquareText aria-hidden="true" /><span>{items.length}</span></button>;
  return <aside className="inline-answer-bubble" style={style}><header><strong>{item.status === "pending" ? "正在回答…" : "原文问答"}</strong><button onClick={() => setOpen(false)} aria-label="收起回答"><ChevronUp aria-hidden="true" /></button></header><p className="inline-question">{item.question}</p>{item.status === "pending" ? <p className="inline-pending"><LoaderCircle aria-hidden="true" />Agent 正在处理</p> : item.status === "failed" ? <p className="inline-failed">{item.error || "回答失败"}</p> : <div className="inline-answer-markdown"><Markdown remarkPlugins={[remarkGfm]}>{item.answer}</Markdown></div>}<footer><button disabled={index === 0} onClick={() => setIndex((value) => value - 1)}><ChevronLeft aria-hidden="true" />上一个</button><span>{index + 1} / {items.length}</span><button disabled={index >= items.length - 1} onClick={() => setIndex((value) => value + 1)}>下一个<ChevronRight aria-hidden="true" /></button></footer><time>{new Date(item.created_at).toLocaleString()}</time></aside>;
}
function SummaryPanel({ value, open, onToggle, onStart }: { value?: { status: string; content: string; error: string | null }; open: boolean; onToggle: () => void; onStart: () => void }) { return <aside className={`paper-summary ${open ? "open" : "closed"}`}><header><div>{open && <><span>SUMMARY</span><strong>论文总结</strong></>}</div><button onClick={onToggle} aria-label={open ? "收起总结栏" : "展开总结栏"} title={open ? "收起总结栏" : "展开总结栏"}>{open ? <ChevronRight aria-hidden="true" /> : <ChevronLeft aria-hidden="true" />}</button></header>{open && <div className="paper-summary-body">{["queued", "running"].includes(value?.status || "") ? <p className="summary-running"><LoaderCircle aria-hidden="true" />正在生成总结，PDF 可继续阅读。</p> : value?.status === "completed" ? <Markdown remarkPlugins={[remarkGfm]}>{value.content}</Markdown> : <><p>{value?.status === "failed" ? value.error || "总结生成失败" : "尚未生成总结"}</p><button onClick={onStart}>开始总结</button></>}</div>}</aside>; }
function ReaderInspector({ selection, translation, translationEngine, translationFontSize, autoTranslate, onAutoTranslate, onTranslationEngine, onTranslationFontSize, onTranslate }: { selection: SelectionState | null; translation: TranslationState | null; translationEngine: TranslationEngine; translationFontSize: number; autoTranslate: boolean; onAutoTranslate: (value: boolean) => void; onTranslationEngine: (value: TranslationEngine) => void; onTranslationFontSize: (value: number) => void; onTranslate: () => void }) {
  const [copied, setCopied] = useState<"source" | "translation" | null>(null);
  const sizePicker = (label: string) => <label className="translation-size"><span>{label}</span><select aria-label={label} value={translationFontSize} onChange={(event) => onTranslationFontSize(Number(event.target.value))}><option value={12}>小</option><option value={14}>中</option><option value={16}>大</option><option value={18}>特大</option></select></label>;
  const copy = async (kind: "source" | "translation", text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(kind);
      window.setTimeout(() => setCopied((current) => current === kind ? null : current), 1500);
    } catch {
      setCopied(null);
    }
  };
  return <div className="inspector-content translation-inspector"><header><div><span>SELECTION TRANSLATION</span><h2>选区翻译</h2></div><Languages aria-hidden="true" /></header><section className="inspector-section translation-settings"><label className="translation-toggle"><input type="checkbox" checked={autoTranslate} onChange={(event) => onAutoTranslate(event.target.checked)} /><span>选中文字后自动翻译</span></label><label className="translation-select"><span>翻译引擎</span><select aria-label="翻译引擎" value={translationEngine} onChange={(event) => onTranslationEngine(event.target.value as TranslationEngine)}><option value="baidu">百度翻译</option><option value="google">Google 翻译</option><option value="youdao">有道翻译</option><option value="360">360 翻译</option><option value="llm">LLM</option></select></label>{selection && !autoTranslate && <button className="secondary-button translation-manual" type="button" onClick={onTranslate}>翻译此选区</button>}</section>{!selection ? <section className="inspector-section translation-empty"><Languages aria-hidden="true" /><p>在 PDF 中选择文字，原文和译文会显示在这里。</p><small>默认自动翻译已{autoTranslate ? "开启" : "关闭"}。</small></section> : <div className="translation-content"><section className="translation-block"><header><h3>原文</h3><div className="translation-block-actions"><button className={`translation-copy ${copied === "source" ? "copied" : ""}`} title="复制原文" type="button" onClick={() => void copy("source", selection.text)}><Copy aria-hidden="true" />{copied === "source" ? "已复制" : "复制"}</button>{sizePicker("原文字号")}</div></header><p className="translation-source" style={{ fontSize: `${translationFontSize}px` }}>{selection.text}</p><small className="translation-location">第 {selection.locator.page} 页</small></section><section className="translation-block"><header><h3>中文译文</h3><div className="translation-block-actions">{translation?.status === "completed" && <button className={`translation-copy ${copied === "translation" ? "copied" : ""}`} title="复制译文" type="button" onClick={() => void copy("translation", translation.text)}><Copy aria-hidden="true" />{copied === "translation" ? "已复制" : "复制"}</button>}{sizePicker("译文字号")}</div></header>{!translation || translation.status === "loading" ? <p className="translation-pending"><LoaderCircle aria-hidden="true" />正在使用{translationLabel(translationEngine)}翻译…</p> : translation.status === "failed" ? <div className="translation-error"><p>{translation.error || "翻译失败"}</p><button className="translation-retry" title="重新翻译" type="button" onClick={onTranslate}><RotateCcw aria-hidden="true" />重试</button></div> : <p className="translation-output" style={{ fontSize: `${translationFontSize}px` }}>{translation.text}</p>}</section></div>}</div>;
}
function PaperSidebar({ papers, current, filter, autoSummary, importing, onFilter, onSelect, onImport, onAutoSummary }: { papers: PaperListItem[]; current?: string; filter: string; autoSummary: boolean; importing: boolean; onFilter: (value: string) => void; onSelect: (id: string) => void; onImport: () => void; onAutoSummary: (value: boolean) => void }) {
  const api = useGatewayApi();
  const parent = useRef<HTMLDivElement>(null), virtual = useVirtualizer({ count: papers.length, getScrollElement: () => parent.current, estimateSize: () => 72, overscan: 8 });
  const [deleteBusy, setDeleteBusy] = useState(false);
  async function trashCurrent() {
    const projectId = new URLSearchParams(window.location.search).get("project");
    if (!current || !projectId || deleteBusy || !window.confirm("将这篇论文移入回收站？7 天后自动彻底删除。")) return;
    setDeleteBusy(true);
    try {
      await api.deletePaper(current, projectId);
      window.dispatchEvent(new CustomEvent("yy-paper-deleted", { detail: { paperId: current } }));
    } catch (error) {
      window.dispatchEvent(new CustomEvent("yy-paper-error", { detail: error instanceof Error ? error.message : String(error) }));
    } finally { setDeleteBusy(false); }
  }
  return <div className="sidebar-layout"><div className="sidebar-heading"><div><span>Workspace Library</span><h2>论文</h2></div><div className="sidebar-heading-actions"><button className="sidebar-import" onClick={onImport} disabled={importing} aria-label={importing ? "正在导入论文" : "导入论文"} title={importing ? "正在导入论文" : "导入论文"}><Upload aria-hidden="true" /></button><button className="sidebar-delete" onClick={() => void trashCurrent()} disabled={!current || deleteBusy} title="移入回收站" aria-label="移入回收站"><Trash2 aria-hidden="true" /></button></div></div><label className="auto-summary-toggle"><input type="checkbox" checked={autoSummary} onChange={(event) => onAutoSummary(event.target.checked)} /><span>自动总结新论文</span></label><label className="paper-search"><Search aria-hidden="true" /><input aria-label="筛选论文" value={filter} onChange={(event) => onFilter(event.target.value)} placeholder="标题、作者、年份或标签" /></label><div className="paper-list" ref={parent}><div style={{ height: virtual.getTotalSize(), position: "relative" }}>{virtual.getVirtualItems().map((row) => { const paper = papers[row.index]; return <button key={paper.paper_id} className={current === paper.paper_id ? "active" : ""} style={{ position: "absolute", transform: `translateY(${row.start}px)`, height: row.size, width: "100%" }} onClick={() => onSelect(paper.paper_id)}><strong>▧ {paper.title}</strong><small>{paper.authors[0]?.display_name || "作者未知"} · {paper.publication_year || "—"}</small></button>; })}</div></div></div>;
}
function readReaderPosition(paperId: string): { page: number; scale: number } | null { try { const value = JSON.parse(localStorage.getItem(`yyagent.web.reader.${paperId}`) || "null") as { page?: unknown; scale?: unknown } | null; if (!value || !Number.isInteger(value.page) || typeof value.scale !== "number") return null; return { page: Math.max(1, Number(value.page)), scale: Math.max(.6, Math.min(2.2, value.scale)) }; } catch { return null; } }
function clamp(value: number) { return Math.max(0, Math.min(1, value)); }
function translationLabel(engine: TranslationEngine) { return engine === "baidu" ? "百度翻译" : engine === "google" ? "Google 翻译" : engine === "youdao" ? "有道翻译" : engine === "360" ? "360 翻译" : "LLM"; }
