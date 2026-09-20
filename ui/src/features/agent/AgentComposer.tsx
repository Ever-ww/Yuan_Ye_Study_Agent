import { ArrowUp, Paperclip, Square, X } from "lucide-react";
import type { PendingApproval, TemporaryAttachment } from "../../types";

export function AgentComposer({
  value, disabled, running, approval, error, attachments, uploading,
  onChange, onSend, onCancel, onApproval, onFiles, onRemoveAttachment,
}: {
  value: string;
  disabled: boolean;
  running: boolean;
  approval: PendingApproval | null;
  error: string;
  attachments: TemporaryAttachment[];
  uploading: boolean;
  onChange: (value: string) => void;
  onSend: () => void;
  onCancel: () => void;
  onApproval: (approvalId: string, approved: boolean) => void;
  onFiles: (files: File[]) => void;
  onRemoveAttachment: (attachmentId: string) => void;
}) {
  const acceptFiles = (files: FileList | null) => {
    if (!files?.length) return;
    onFiles(Array.from(files).filter((file) =>
      file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"),
    ));
  };
  return (
    <div className="composer-region" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); acceptFiles(event.dataTransfer.files); }}>
      {approval && (
        <section className="approval-bar" aria-label="工具审批">
          <div><strong>允许执行工具 “{approval.tool_name}” 吗？</strong><small>{compactArguments(approval.arguments)}</small></div>
          <button type="button" onClick={() => onApproval(approval.approval_id, false)}>拒绝</button>
          <button className="approval-allow" type="button" onClick={() => onApproval(approval.approval_id, true)}>允许</button>
        </section>
      )}
      {error && <div className="inline-error" role="alert">{error}</div>}
      {attachments.length > 0 && <div className="attachment-strip" aria-label="临时对话附件">{attachments.map((attachment) => <span key={attachment.attachment_id}><Paperclip aria-hidden="true" />{attachment.filename}<button type="button" aria-label={`移除 ${attachment.filename}`} onClick={() => onRemoveAttachment(attachment.attachment_id)}><X aria-hidden="true" /></button></span>)}</div>}
      <div className="composer">
        <label className="sr-only" htmlFor="agent-prompt">发送给 YYAgent</label>
        <textarea
          id="agent-prompt"
          value={value}
          disabled={disabled || running}
          placeholder={disabled ? "请先添加或选择一个项目" : "给 YYAgent 一项任务…"}
          rows={2}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSend();
            }
          }}
        />
        <div className="composer-actions">
          <label className="attachment-button" aria-label="上传 PDF 作为当前对话临时上下文">
            <Paperclip aria-hidden="true" />
            <input type="file" accept="application/pdf,.pdf" multiple disabled={disabled || running || uploading} onChange={(event) => { acceptFiles(event.target.files); event.target.value = ""; }} />
          </label>
          <span className="composer-help">Enter 发送 · Shift Enter 换行 · Ctrl K 打开命令</span>
          {running ? (
            <button className="send-button stop-button" type="button" onClick={onCancel} aria-label="停止当前运行"><Square aria-hidden="true" /></button>
          ) : (
            <button className="send-button" type="button" disabled={disabled || !value.trim()} onClick={onSend} aria-label="发送"><ArrowUp aria-hidden="true" /></button>
          )}
        </div>
      </div>
    </div>
  );
}

function compactArguments(argumentsValue: Record<string, unknown>): string {
  const text = JSON.stringify(argumentsValue);
  return text.length > 180 ? `${text.slice(0, 177)}…` : text;
}
