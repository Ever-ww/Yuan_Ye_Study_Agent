# 历史工具结果预览与精确回查

工具发布后的 Observation、Session JSONL、Operation/Attempt Evidence 和内存原始消息不因预览而改变。`TURN_START` Hook 冻结本轮历史证据，`MemoryStore.restore_messages()` 在消息副本上生成预览；预算复核使用同一个 `ToolOutputProjector`，不再维护第二套首尾裁剪算法。没有 `session_history` 的 Runtime 不自动启用这种预览。

## 默认行为

- 当前 Turn 新产生的工具结果保留；恢复中的同一 Durable Run 结果也保留。
- 最近一个完整 assistant/tool 调用组保留，包括同一批的全部并行结果。
- 缺少结果、调用 ID 非法、孤立 Tool 消息或来源不唯一时不猜测、不裁剪。
- 更早且正文超过 `tool_output_max_chars=10000` 的结果可生成预览。小结果保持原样；预览比原文更长时不替换。
- 正文预览前 25、后 25 个 Python 字符，不是单词或 Token。元数据和诊断摘要不包含在这 50 个字符内。
- `tool_output_max_chars=0` 关闭预览；提高 `tool_output_protect_recent_groups` 可保留更多最近调用组。

```json
{
  "tool_output_max_chars": 10000,
  "tool_output_preview_head_chars": 25,
  "tool_output_preview_tail_chars": 25,
  "tool_output_protect_recent_groups": 1,
  "tool_output_diagnostic_max_chars": 600
}
```

旧 `tool_output_head_ratio` / `tool_output_tail_ratio` 仍接受配置以兼容升级，但不再控制预览。稳定 System Prompt 和 Tool Schema 不随 Turn 改写，同一 Turn 不叠加重复预览。若保护内容本身超过模型硬限制，仍进入既有压缩/预算保护流程，不为了省 Token 强行截断当前结果。

## 结构化与失败摘要

预览包含 name、status、tool_call_id、record_id、run_id、原始字符数、完整正文 SHA-256 和精确回查参数。结构化文件结果保留路径、格式、范围、分页/截断信息；JSON 保留有限的键、计数及状态；搜索结果保留少量命中；日志抽取 FAILED、AssertionError、异常、退出码等诊断行（默认最多 600 字符）。这是确定性摘录，不调用额外模型，不保证保留全部错误。它不复制完整参数或隐藏审计字段。历史观察不等于文件或外部服务的当前状态。

## 模型回查

`session_history` 可组合使用 `record_id`、`tool_call_id`、`run_id`、`segment`、`role`、`query` 和 `expected_content_hash`，只查询绑定的当前 Session。优先使用预览中的 record_id 和内容 Hash。重复的旧 tool_call_id 返回候选身份而不是猜测；精确记录的 Hash 不符会拒绝读取。

结果提供工具名、状态、调用/运行身份、分段、完整记录 Hash、正文 Hash、字符偏移和下一页偏移。`sha256` 是完整 Session Record 的 Hash，`content_sha256` 是 Observation 正文 Hash，二者不能混用。单页正文最多 2000 字符；利用 `next_content_offset` 可以拼回完整已保存 Observation，无需重新执行原工具。

## CLI 详情

```text
/tool-result <record_id 或 tool_call_id> [字符偏移]
```

通过鉴权 Gateway GET `/api/v1/projects/{project_id}/sessions/{session_id}/tool-results` 使用同一受控读取器。只读查询不会创建 Run、模型请求、Tool Attempt 或更改 Inbox。CLI 默认仍只显示工具状态；详情按需展开，显示的下一页命令可继续回查。

原工具可能已对输出分页或截断；这里只能回查当时保存的 Observation，并不能恢复从未保存的外部内容。当前读取器扫描该 Session 索引列出的分段，尚未新增数据库全文索引。跨 Session/项目或缺失证据时不会绕过边界寻找文件。
