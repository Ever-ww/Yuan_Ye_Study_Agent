# 历史工具结果预览与精确回查

工具发布后的 Observation、Session JSONL、Operation/Attempt Evidence 和内存原始消息不因裁剪而改变。独立 `MODEL_BEFORE` Hook 在 Memory 完成消息恢复后修改本次 Provider 消息副本；Context Compression 不生成、缓存或二次修改 Tool 预览。没有 `session_read` 的 Runtime 不启用这种可恢复裁剪。

## 默认行为

- 当前 Turn（最近一次用户消息之后）新产生的工具结果全部保留。
- 缺少结果、调用 ID 非法、孤立 Tool 消息或来源不唯一时不猜测、不裁剪。
- 最近一次用户消息之后产生的 Tool 结果保持完整；更早的完整 Tool Call 组才可裁剪。
- 中文阈值为 1000 个中日韩统一表意字符，英文阈值为 1000 个单词；另有默认 10000 字符的防御性硬阈值用于代码、Base64和标点密集输出。
- 正文默认保留前 427、后 427 个 Python 字符，不是单词或 Token；中间只放确定性的 `session_read` 调用参数。
- 三个阈值均为 0 时关闭裁剪。旧 ratio/protect/diagnostic 配置只为兼容旧配置读取，不参与新规则。

```json
{
  "tool_output_cjk_threshold_chars": 1000,
  "tool_output_english_threshold_words": 1000,
  "tool_output_max_chars": 10000,
  "tool_output_preview_head_chars": 427,
  "tool_output_preview_tail_chars": 427
}
```

旧 `tool_output_head_ratio` / `tool_output_tail_ratio` 仍接受配置以兼容升级，但不再控制裁剪。稳定 System Prompt 和 Tool Schema 不随 Turn 改写，同一 Turn 不叠加重复裁剪。若当前对话本身超过模型硬限制，仍进入既有压缩/预算保护流程，不为了省 Token 强行截断当前 Tool 结果。

裁剪不再生成另一套结构化摘要，也不尝试猜测“重要的”中间行。需要中段错误、JSON字段或搜索结果时，模型必须用提示里的精确参数读取 Canonical Session 记录。历史观察不等于文件或外部服务的当前状态。

## 模型回查

`session_read` 只读取 Runtime 当前绑定 Session。`session_id` 参数是裁剪提示给出的 Session JSONL 文件名，`record_id` 是该行的稳定标识；`offset=0, limited=1` 默认只返回该记录，增大 offset/limited 可从锚点向后读取更多可见记录。文件名必须存在于当前 Session 索引，不能传任意路径。

较宽泛的历史搜索仍由 `session_history` 提供；它可按 query、role、segment、tool_call_id 和 Hash 分页检索，但不是裁剪提示的默认精确回读入口。

`session_read` 返回完整正文以及角色、工具名、状态、调用/运行身份和正文 Hash，不按字符二次裁剪。`limited` 最多为20；读取多条时只在指定文件中从锚点向后移动。若读取结果本身很大，它在当前 Turn 保持完整，进入后续历史后才可能再次被同一 Hook 裁剪。

## CLI 详情

```text
/tool-result <record_id 或 tool_call_id> [字符偏移]
```

通过鉴权 Gateway GET `/api/v1/projects/{project_id}/sessions/{session_id}/tool-results` 使用同一受控读取器。只读查询不会创建 Run、模型请求、Tool Attempt 或更改 Inbox。CLI 默认仍只显示工具状态；详情按需展开，显示的下一页命令可继续回查。

原工具可能已对输出分页或截断；这里只能回查当时保存的 Observation，并不能恢复从未保存的外部内容。当前读取器扫描该 Session 索引列出的分段，尚未新增数据库全文索引。跨 Session/项目或缺失证据时不会绕过边界寻找文件。
