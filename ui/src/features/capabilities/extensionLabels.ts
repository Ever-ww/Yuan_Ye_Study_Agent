export const extensionCapabilityLabels: Record<string, string> = {
  "session.read": "读取会话", "state.read": "读取任务状态",
  "logger.write": "写入运行日志", "workspace.read": "读取工作区",
  "memory.read": "读取记忆", "memory.append": "写入记忆",
  "model.request.modify": "修改模型请求", "tool.request.modify": "修改工具请求",
  "tool.invoke": "调用指定工具",
};

export function extensionCapabilityLabel(value: string): string {
  return extensionCapabilityLabels[value] || value;
}

export function extensionGrantLabel(value: string): string {
  return ({ granted: "已授权", revoked: "已撤销", pending: "等待授权", denied: "已拒绝" } as Record<string, string>)[value] || value;
}
