import { useEffect, useRef } from "react";
import { AlertTriangle, X } from "lucide-react";

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "确认",
  busy = false,
  onClose,
  onConfirm,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  busy?: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog ref={dialogRef} className="confirm-dialog" onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }} onClose={onClose}>
      <header>
        <AlertTriangle aria-hidden="true" />
        <h2>{title}</h2>
        <button type="button" className="icon-button" onClick={onClose} disabled={busy} aria-label="关闭" title="关闭"><X aria-hidden="true" /></button>
      </header>
      <div className="confirm-dialog-content"><p>{message}</p></div>
      <footer>
        <button type="button" className="secondary-button" onClick={onClose} disabled={busy}>取消</button>
        <button type="button" className="confirm-danger" onClick={onConfirm} disabled={busy}>{busy ? "处理中…" : confirmLabel}</button>
      </footer>
    </dialog>
  );
}
