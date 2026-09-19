import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { FileSearch, Search, X } from "lucide-react";
import type { PDFDocumentProxy } from "pdfjs-dist";

export type PdfSearchResult = { page: number; snippet: string; matches: number };

type Props = {
  document: PDFDocumentProxy | null;
  query: string;
  results: PdfSearchResult[];
  progress: string;
  onQuery: (value: string) => void;
  onResults: (value: PdfSearchResult[]) => void;
  onProgress: (value: string) => void;
  onPage: (page: number) => void;
  onClose: () => void;
};

export function PdfSearchPanel(props: Props) {
  const parent = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count: props.results.length, getScrollElement: () => parent.current, estimateSize: () => 78, overscan: 6 });
  async function search(event: React.FormEvent) {
    event.preventDefault();
    const needle = props.query.trim().toLocaleLowerCase();
    if (!needle || !props.document) return;
    props.onResults([]);
    const found: PdfSearchResult[] = [];
    for (let pageNumber = 1; pageNumber <= props.document.numPages; pageNumber += 1) {
      props.onProgress(`正在搜索第 ${pageNumber} / ${props.document.numPages} 页`);
      const page = await props.document.getPage(pageNumber);
      const content = await page.getTextContent();
      const text = content.items.map((item) => "str" in item ? item.str : "").join(" ").replace(/\s+/g, " ");
      const lower = text.toLocaleLowerCase();
      let cursor = 0; let matches = 0; let first = -1;
      while ((cursor = lower.indexOf(needle, cursor)) >= 0) { if (first < 0) first = cursor; matches += 1; cursor += Math.max(needle.length, 1); }
      if (matches) found.push({ page: pageNumber, matches, snippet: text.slice(Math.max(0, first - 70), Math.min(text.length, first + needle.length + 110)) });
    }
    props.onResults(found);
    props.onProgress(found.length ? `${found.length} 页包含匹配内容` : "未找到匹配内容");
  }
  return <section className="pdf-search-panel" aria-label="PDF 全文搜索">
    <header><form onSubmit={(event) => void search(event)}><Search aria-hidden="true" /><input autoFocus aria-label="搜索 PDF 全文" value={props.query} onChange={(event) => props.onQuery(event.target.value)} placeholder="搜索当前 PDF" /><button disabled={!props.query.trim() || !props.document} type="submit">搜索</button></form><button className="icon-button" aria-label="关闭全文搜索" onClick={props.onClose}><X aria-hidden="true" /></button></header>
    <p className="search-progress" role="status">{props.progress || "输入关键词后搜索 PDF 文本层。"}</p>
    <div className="pdf-search-results" ref={parent}><div style={{ height: virtual.getTotalSize(), position: "relative" }}>{virtual.getVirtualItems().map((row) => { const item = props.results[row.index]; return <button key={item.page} style={{ position: "absolute", transform: `translateY(${row.start}px)`, height: row.size, width: "100%" }} onClick={() => props.onPage(item.page)}><FileSearch aria-hidden="true" /><span><strong>第 {item.page} 页 · {item.matches} 处</strong><small>{item.snippet}</small></span></button>; })}</div></div>
  </section>;
}
