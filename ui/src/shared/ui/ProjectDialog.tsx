import { useEffect, useRef, useState } from "react";
import { FolderOpen, FolderPlus, X } from "lucide-react";

type DirectoryPickerWindow = Window & {
  showDirectoryPicker?: () => Promise<{ name: string; path?: string }>;
};

export function ProjectDialog({
  open, onClose, onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (path: string, name: string) => Promise<void>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
    if (!open && dialog.open) dialog.close();
  }, [open]);

  async function choosePath() {
    setError("");
    if ("__TAURI_INTERNALS__" in window) {
      try {
        const { open } = await import("@tauri-apps/plugin-dialog");
        const selected = await open({ directory: true, multiple: false });
        if (typeof selected === "string") setPath(selected);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
      return;
    }
    const picker = (window as DirectoryPickerWindow).showDirectoryPicker;
    if (!picker) {
      setError("当前浏览器不支持目录选择，请直接输入绝对路径，或使用 Tauri 工作台。");
      return;
    }
    try {
      const handle = await picker();
      if (handle.path) setPath(handle.path);
      else setError("浏览器出于安全限制不会返回绝对路径，请直接输入路径，或使用 Tauri 工作台选择目录。");
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!path.trim()) return setError("请输入工作区绝对路径。可使用右侧目录按钮选择。");
    setBusy(true);
    setError("");
    try {
      await onSubmit(path.trim(), name.trim());
      setPath(""); setName(""); onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialogRef} className="form-dialog" onClose={onClose}>
      <form onSubmit={submit}>
        <header><FolderPlus aria-hidden="true" /><h2>添加项目</h2><button type="button" onClick={onClose} aria-label="关闭" title="关闭"><X aria-hidden="true" /></button></header>
        <label htmlFor="project-path">工作区绝对路径</label>
        <div className="path-picker">
          <input ref={inputRef} id="project-path" value={path} onChange={(event) => setPath(event.target.value)} autoComplete="off" />
          <button type="button" className="icon-button" onClick={() => void choosePath()} aria-label="选择工作区目录" title="选择工作区目录"><FolderOpen aria-hidden="true" /></button>
        </div>
        <label htmlFor="project-name">显示名称 <span>可选</span></label>
        <input id="project-name" value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" />
        {error && <p className="field-error" role="alert">{error}</p>}
        <footer><button type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy} type="submit">{busy ? "添加中…" : "添加项目"}</button></footer>
      </form>
    </dialog>
  );
}
