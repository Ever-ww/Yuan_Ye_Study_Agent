import { ArrowUp, Square } from "lucide-react";
import type { PendingApproval } from "../../types";

export function AgentComposer({
  value, disabled, running, approval, error, onChange, onSend, onCancel, onApproval,
}: {
  value: string;
  disabled: boolean;
  running: boolean;
  approval: PendingApproval | null;
  error: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onCancel: () => void;
  onApproval: (approvalId: string, approved: boolean) => void;
}) {
  return (
    <div className="composer-region">
      {approval && (
        <section className="approval-bar" aria-label="工具审批">
          <div><strong>允许执行工具 “{approval.tool_name}” 吗？</strong><small>{compactArguments(approval.arguments)}</small></div>
          <button type="button" onClick={() => onApproval(approval.approval_id, false)}>拒绝</button>
          <button className="approval-allow" type="button" onClick={() => onApproval(approval.approval_id, true)}>允许</button>
        </section>
      )}
      {error && <div className="inline-error" role="alert">{error}</div>}
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
        {running ? (
          <button className="send-button stop-button" type="button" onClick={onCancel} aria-label="停止当前运行"><Square aria-hidden="true" /></button>
        ) : (
          <button className="send-button" type="button" disabled={disabled || !value.trim()} onClick={onSend} aria-label="发送"><ArrowUp aria-hidden="true" /></button>
        )}
      </div>
      <p className="composer-help">Enter 发送 · Shift Enter 换行 · Ctrl K 打开命令</p>
    </div>
  );
}

function compactArguments(argumentsValue: Record<string, unknown>): string {
  const text = JSON.stringify(argumentsValue);
  return text.length > 180 ? `${text.slice(0, 177)}…` : text;
}
