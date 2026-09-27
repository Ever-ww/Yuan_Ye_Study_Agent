import { describe, expect, it } from "vitest";
import { isNoteWorkspacePath, writeChanges } from "./features/writer/workspaceScope";

describe("Write workspace scope", () => {
  it("hides the workspace notes area without hiding similarly named project paths", () => {
    expect(isNoteWorkspacePath("YYWorkspace:\\notes")).toBe(true);
    expect(isNoteWorkspacePath("YYWorkspace:\\notes\\review.md")).toBe(true);
    expect(isNoteWorkspacePath("YYWorkspace:\\notebook\\review.md")).toBe(false);
    expect(isNoteWorkspacePath("YYWorkspace:\\paper\\notes\\review.md")).toBe(false);
  });

  it("filters Note paths from the Write change list", () => {
    const changes = [
      { path: "YYWorkspace:\\notes\\daily.md", status: "M" },
      { path: "YYWorkspace:\\paper\\main.tex", status: "M" },
    ];
    expect(writeChanges(changes)).toEqual([{ path: "YYWorkspace:\\paper\\main.tex", status: "M" }]);
  });
});
