# Provider 上下文历史与缓存边界

Yuan Ye 的用户消息分成两层：`content` 是用户原文；`provider_context` 是经过校验、带 Hash 的运行时投影。二者在同一条 Session JSONL 记录中原子追加，但只有前者属于对话事实、Memory、压缩输入和历史检索。

模型请求会从 `provider_context` 还原上一轮实际看到的 user message，因此后续 Turn 不会把旧动态区块删掉，也不会改变已有前缀。新 Turn 只把相对当前可见历史发生变化的 fragment 附在新 user message 后面。若没有变化，新消息就是用户原文。

Agent 时间是动态 fragment 的一部分。它以最近一次可见动态上下文的 `current_time` 为基线：未满两小时沿用旧值；满两小时在下一条 user message 中追加新快照。Memory、Summary、Sandbox 或压缩分段等变化若先产生了新上下文，则该次注入时间成为新的两小时基线。

压缩是显式历史窗口边界。若预检在新 Turn 的 user record 写入前触发，系统保留上一轮完整 Turn，只压缩更早历史，并把新 Summary 作为 `continuity_fragment` 放入本轮 user prompt。若压缩发生在 Turn 内（例如 Tool 返回后或 Provider 超限重试），系统同时保留上一轮完整 Turn和本轮截至当前的完整 user/assistant/tool trace，只压缩更早历史；新 Summary 通过 append-only `provider_context` amendment 挂到上一轮 user prompt，不改写原始记录。随后同一 Turn 的模型调用从新分段恢复这些原文和 amendment，继续执行。

这种位置选择有意让压缩后的请求保持“Summary → 上一完整 Turn → 当前 Turn trace”的时间顺序。新 Summary、边界类型和目标 record_id 都有持久化 Evidence；Gateway 恢复不依赖内存猜测。进入下一 Turn 后，最新 Summary 仍会作为该新 Turn 的连续性动态信息注入。

安全边界：`provider_context` 不进入压缩模型输入、长期 Memory、Session History Tool 正文、Failure Snapshot、Inbox 或 Tool 参数。Hash 只用于发现意外损坏，不构成针对本机恶意篡改的密码学信任根。

这一布局遵循 Prompt Cache 的核心约束：稳定内容尽量保持在请求前缀，变化内容追加在尾部。是否实际命中及命中多少仍由 Provider 的缓存规则、模型和保留时间决定，系统不伪造命中保证。
