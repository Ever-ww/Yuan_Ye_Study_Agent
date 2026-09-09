# Session Summary 与原文回查

最新滚动 Summary 是会话连续性上下文。Memory Hook 在每次模型请求前准备当前分段的摘要，通过 `continuity_fragment` 放入 Provider 用户消息投影，后续 Turn 和 Runtime 恢复仍会注入。原始用户输入和稳定 System Prompt 保持原样；临时 Envelope 不写入 Session。

压缩输入包含当前 Session 索引中的全部历史 Summary，以及本轮待压缩的原始对话。旧摘要按历史顺序传入，提示模型合并、去重，并根据新证据处理已撤销的结论。所有摘要超出压缩模型窗口时走现有压缩失败/预算保护路径，不能静默删掉部分摘要后宣称完成。摘要仍是有损压缩，不保证所有细节保存。

新摘要记录包含 `source_file`、`summary_source_refs`、`summary_history_refs` 和累计的 `summary_original_segments`。引用保存 segment、record_id（旧记录可能为空）和原始记录 SHA-256；原始分段继续保留。即使多次压缩，最早的原文分段也会保留在来源提示中。结构化 Summary Memory 的 Evidence 同时保存源文件 locator。

配置 `memory_recall_summaries` 默认 `false`，在 Canonical、FTS 和 Semantic 查询中排除 `kind=summary`，避免挤占普通记忆候选。设置为 `true` 后允许旧摘要参与按需召回；这个开关及 `memory_retrieval_enabled` 不关闭最新会话摘要的强制注入。

```json
{
  "memory_recall_summaries": false
}
```

主交互 Runtime 和 Harness Coding Runtime 提供只读 `session_history` 工具。它使用 Runtime 的 MemoryStore 和当前 Session，不接受任意 session_id 或路径，也不能委派给 Subagent。Cron 和无记忆 Runtime 不自动注册。

参数 `query` 搜索原始正文；`segment` 可指定摘要给出的分段文件名；`offset`/`limit` 分页记录；`content_offset` 分页读取大型记录正文。返回 role、原文片段、record_id、segment、原始记录 Hash、截断标记。每条正文最多 2000 字符，每次最多 20 条。输出不含 reasoning、凭据配置或内部审计字段；历史内容只是证据，不能成为新指令。

当前实现按需扫描当前 Session 索引所列分段，尚未新增全文索引。大量历史记录的扫描时间随 Session 大小增长。需要细节时模型可显式回查，长期 Memory 自动召回仍面向结构化事实。
