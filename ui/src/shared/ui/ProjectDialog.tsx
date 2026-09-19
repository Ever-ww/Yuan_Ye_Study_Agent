import { useEffect, useRef, useState } from "react";
import { FolderPlus, X } from "lucide-react";

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

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!path.trim()) return setError("请输入工作区绝对路径。使用 Tauri 时也可以从系统目录选择器添加项目。");
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
        <header><FolderPlus aria-hidden="true" /><h2>添加项目</h2><button type="button" onClick={onClose} aria-label="关闭"><X aria-hidden="true" /></button></header>
        <label htmlFor="project-path">工作区绝对路径</label>
        <input ref={inputRef} id="project-path" value={path} onChange={(event) => setPath(event.target.value)} autoComplete="off" />
        <label htmlFor="project-name">显示名称 <span>可选</span></label>
        <input id="project-name" value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" />
        {error && <p className="field-error" role="alert">{error}</p>}
        <footer><button type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy} type="submit">{busy ? "添加中…" : "添加项目"}</button></footer>
      </form>
    </dialog>
  );
}
