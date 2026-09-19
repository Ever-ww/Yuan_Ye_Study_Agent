import { useEffect, useRef } from "react";
import { EditorState, type Extension } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { bracketMatching, defaultHighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorView, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers } from "@codemirror/view";
import { markdown } from "@codemirror/lang-markdown";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";

export function CodeEditor({ path, value, readOnly, focusLine, onChange }: { path: string; value: string; readOnly: boolean; focusLine?: { line: number; token: number }; onChange: (value: string) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const changeHandler = useRef(onChange);
  useEffect(() => { changeHandler.current = onChange; }, [onChange]);
  useEffect(() => {
    if (!host.current) return;
    const extensions: Extension[] = [
      lineNumbers(), highlightActiveLineGutter(), history(), bracketMatching(),
      syntaxHighlighting(defaultHighlightStyle, { fallback: true }), highlightActiveLine(),
      EditorView.lineWrapping, EditorState.readOnly.of(readOnly),
      keymap.of([indentWithTab, ...defaultKeymap, ...historyKeymap]),
      EditorView.updateListener.of((update) => { if (update.docChanged) changeHandler.current(update.state.doc.toString()); }),
      EditorView.theme({
        "&": { height: "100%", backgroundColor: "var(--surface-raised)", color: "var(--text)" },
        ".cm-scroller": { fontFamily: "var(--mono)", fontSize: "13px", lineHeight: "1.65", overflow: "auto" },
        ".cm-gutters": { backgroundColor: "var(--surface)", color: "var(--text-faint)", borderRight: "1px solid var(--line)" },
        ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: "var(--surface-muted)" },
        ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": { backgroundColor: "var(--selection) !important" },
        "&.cm-focused": { outline: "none" },
      }),
      languageFor(path),
    ];
    view.current = new EditorView({ state: EditorState.create({ doc: value, extensions }), parent: host.current });
    return () => { view.current?.destroy(); view.current = null; };
  }, [path, readOnly]);
  useEffect(() => {
    const current = view.current;
    if (!current || current.state.doc.toString() === value) return;
    current.dispatch({ changes: { from: 0, to: current.state.doc.length, insert: value } });
  }, [value]);
  useEffect(() => {
    const current = view.current;
    if (!current || !focusLine) return;
    const line = current.state.doc.line(
      Math.max(1, Math.min(focusLine.line, current.state.doc.lines)),
    );
    current.dispatch({
      selection: { anchor: line.from },
      effects: EditorView.scrollIntoView(line.from, { y: "center" }),
    });
    current.focus();
  }, [focusLine]);
  return <div className="code-mirror-host" ref={host} />;
}

function languageFor(path: string): Extension {
  const extension = path.split(".").pop()?.toLocaleLowerCase();
  if (["md", "markdown"].includes(extension || "")) return markdown();
  if (extension === "json") return json();
  if (["py", "pyi"].includes(extension || "")) return python();
  if (["js", "jsx", "ts", "tsx", "mjs", "cjs"].includes(extension || "")) return javascript({ typescript: extension?.startsWith("ts"), jsx: extension?.endsWith("x") });
  if (["html", "htm"].includes(extension || "")) return html();
  if (["css", "scss"].includes(extension || "")) return css();
  return [];
}
