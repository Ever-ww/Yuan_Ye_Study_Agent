import { useEffect, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Page } from "react-pdf";

export function PdfThumbnails({ pages, current, onPage }: { pages: number; current: number; onPage: (page: number) => void }) {
  const parent = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count: pages, getScrollElement: () => parent.current, estimateSize: () => 176, overscan: 2 });
  useEffect(() => { if (pages) virtual.scrollToIndex(current - 1, { align: "auto" }); }, [current, pages, virtual]);
  return <aside className="pdf-thumbnails" ref={parent} aria-label="PDF 页面缩略图"><div style={{ height: virtual.getTotalSize(), position: "relative" }}>{virtual.getVirtualItems().map((row) => { const page = row.index + 1; return <button key={page} className={current === page ? "active" : ""} style={{ position: "absolute", transform: `translateY(${row.start}px)`, height: row.size, width: "100%" }} onClick={() => onPage(page)} aria-label={`转到第 ${page} 页`}><Page pageNumber={page} width={112} renderTextLayer={false} renderAnnotationLayer={false} /><span>{page}</span></button>; })}</div></aside>;
}
