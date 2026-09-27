import type { WorkspaceEntry } from "../../types";

/** Notes live under the private workspace `notes` area and belong to Note mode. */
export function isNoteWorkspacePath(path: string): boolean {
  const normalized = path.replaceAll("/", "\\").replace(/\\+$/, "").toLocaleLowerCase();
  return normalized === "yyworkspace:\\notes" || normalized.startsWith("yyworkspace:\\notes\\");
}

export function isWriteEntry(entry: WorkspaceEntry): boolean {
  return !isNoteWorkspacePath(entry.path);
}

export function writeChanges<T extends { path: string }>(changes: T[]): T[] {
  return changes.filter((change) => !isNoteWorkspacePath(change.path));
}
