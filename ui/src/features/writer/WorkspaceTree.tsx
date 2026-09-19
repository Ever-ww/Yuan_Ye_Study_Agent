import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, File, FileWarning, Folder, FolderOpen } from "lucide-react";
import { useGatewayApi } from "../../shared/api/context";
import type { WorkspaceEntry } from "../../types";

export function WorkspaceTree({ projectId, selected, revision, onSelect }: { projectId: string; selected?: string; revision: number; onSelect: (entry: WorkspaceEntry) => void }) {
  const root = useDirectory(projectId, "YYWorkspace:\\", revision, true);
  if (root.isLoading) return <p className="quiet-empty">正在读取文件树…</p>;
  if (root.isError) return <p className="quiet-empty">{root.error.message}</p>;
  const entries = root.data?.pages.flatMap((page) => page.entries) || [];
  return <div className="workspace-tree" role="tree" aria-label="Workspace 文件树">{entries.map((entry) => <TreeEntry key={entry.path} projectId={projectId} entry={entry} selected={selected} revision={revision} depth={0} onSelect={onSelect} />)}{root.hasNextPage && <LoadMore busy={root.isFetchingNextPage} onClick={() => void root.fetchNextPage()} />}</div>;
}

function TreeEntry({ projectId, entry, selected, revision, depth, onSelect }: { projectId: string; entry: WorkspaceEntry; selected?: string; revision: number; depth: number; onSelect: (entry: WorkspaceEntry) => void }) {
  const [open, setOpen] = useState(false);
  const directory = entry.kind === "directory";
  const children = useDirectory(projectId, entry.path, revision, directory && open);
  const Icon = entry.kind === "blocked_link" ? FileWarning : directory ? open ? FolderOpen : Folder : File;
  return <div role="treeitem" aria-expanded={directory ? open : undefined}>
    <button className={`tree-row ${selected === entry.path ? "selected" : ""}`} style={{ paddingLeft: `${8 + depth * 14}px` }} disabled={entry.blocked} onClick={() => { if (directory) setOpen((value) => !value); onSelect(entry); }}>
      {directory ? open ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" /> : <span className="tree-spacer" />}
      <Icon aria-hidden="true" /><span>{entry.name}</span>
    </button>
    {directory && open && <div role="group">{children.isLoading && <span className="tree-loading" style={{ paddingLeft: `${28 + depth * 14}px` }}>加载中…</span>}{(children.data?.pages.flatMap((page) => page.entries) || []).map((child) => <TreeEntry key={child.path} projectId={projectId} entry={child} selected={selected} revision={revision} depth={depth + 1} onSelect={onSelect} />)}{children.hasNextPage && <LoadMore busy={children.isFetchingNextPage} depth={depth + 1} onClick={() => void children.fetchNextPage()} />}</div>}
  </div>;
}

function useDirectory(projectId: string, path: string, revision: number, enabled: boolean) {
  const api = useGatewayApi();
  return useInfiniteQuery({
    queryKey: ["workspace", "tree", projectId, path, revision],
    initialPageParam: "",
    queryFn: ({ pageParam }) => api.workspaceTree(projectId, path, pageParam || undefined),
    getNextPageParam: (page) => page.next_cursor || undefined,
    enabled,
  });
}

function LoadMore({ busy, depth = 0, onClick }: { busy: boolean; depth?: number; onClick: () => void }) {
  return <button className="tree-load-more" style={{ paddingLeft: `${28 + depth * 14}px` }} disabled={busy} onClick={onClick}>{busy ? "正在加载…" : "加载更多"}</button>;
}
