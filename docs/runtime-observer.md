# Runtime Observer 架构

Runtime Observer 将“执行”与“观察”分开：Main/Harness Runtime 继续执行任务，Observer 只根据
用户已经能够看到的 Gateway Event 维护一份进度状态。Observer 的提示与结果校验契约是可热更新插件；事件
可见性、持久状态、offset、恢复、审批和传输均由不可热替换的 Gateway Core 负责。

## 数据流与边界

```text
Canonical Gateway Event
        ├─ Outbox异步投递 → CLI / Web
        └─ Run内有序后台队列
                    ↓
        VisibleEventProjection（Core allowlist + 字段裁剪）
                    ↓
          Observer Prompt Contract（Generation Plugin）
                    ↓
             最小化 AgentRuntime + LLM
                    ↓
             ObserverStateStore
             state + offset 同事务
                    ↓
         CLI / Web Observer Progress
```

Observer 可以看到普通界面已经展示的文本、Tool 名称/状态、审批等待提示和 Run 终态。它不能
看到 reasoning、System Prompt、Memory/动态 Context、完整 Tool 参数、Tool Result 或凭据。
`observer_progress`、纠偏事件和 Skill Candidate 事件被投影器明确排除，因而不会反馈给
Observer，也不会进入 Main/Harness Provider Context。

Observer 插件不获得 ToolRegistry、PromptRegistry、Harness、代码写入接口或 Approval 回调。
它只能组装递归状态请求并校验返回值。真正的递归更新通过 Core 创建的最小化
`AgentRuntime` 调用 LLM；该 Runtime 没有 Tool、Memory、Sandbox、Skill、Extension 或 Cron：

```text
Previous ObserverState + one visible milestone → New ObserverState
```

流式 `text` 仍保存为可见 Evidence，但不会逐 token 调用 Observer 模型。Observer 只在
`run_started`、Tool/压缩/审批等可见里程碑和 Run 终态递归更新状态。Main Event 提交后只唤醒
Outbox并投递到 Run 内有序后台 Observer 队列，不等待 Observer 模型或物理 Sink 完成，因此
Observer 延迟不会节流主回答。

插件异常由现有 Runtime Plugin 健康与 Quarantine 机制归因；Observer 实例标记失败，但 Main、
Harness、Cron Run 继续执行。

## Generation、Profile 与生命周期

`observer_plugins/` 由 `builtin.observer` Provider 发现并作为 `OBSERVER` Contribution 写入不可变
Generation。每个 Run 在调度前已经绑定确切 `generation_id`，Observer 从同一 Generation 的
Profile Snapshot 加载代码，不读取正在编辑的工作区版本。

支持的 Profile 为：

- `interactive`
- `cron`
- `dream`
- `harness:manual`
- `harness:error`
- `harness:capability`
- `harness:dream`

Evidence 同时保存 `runtime_role`、`agent_role`、`runtime_profile` 和 `trigger`。不同 Profile 的
Evidence 不会自动聚合；Harness 四个 trigger 也分别聚合。reload 只激活新的 Generation，已经
绑定的 Run 保持原版本。

Runtime 的第一个 durable Event 会创建独立 Observer Instance；不同 Run 不共享插件对象的可变
状态。Run 终止时 Instance 进入 `finalized` 并释放进程内插件实例；如果需要纠偏，
待决生命周期由独立的 Correction Proposal 记录表达，
Runtime 对象销毁不会删除 State 或 Evidence。

## 状态、offset 与崩溃恢复

Core 表包括：

- `observer_instances`：当前状态、Generation/Plugin/Schema 身份和 `last_event_offset`。
- `observer_visible_events`：每个已消费 Canonical Event 的裁剪投影或不可见标记。
- `observer_tool_loops`：纯规则产生的 loop、执行模式与 Tool 名称序列。
- `observer_correction_proposals`：持久纠偏建议、CAS revision 和一分钟期限。
- `observer_evidence`：Turn/Run 完成后的不可变 Evidence。
- `observer_skill_candidates`：Dream 聚合后的待审批 Skill Candidate。

单个 Event 的事务顺序固定为：

```text
BEGIN IMMEDIATE
→ 校验 Run / Generation / Plugin identity
→ INSERT observer_visible_events
→ 更新 ObserverState
→ CAS 推进 last_event_offset
→ COMMIT
```

不存在“先推进 offset、后写 State”的窗口。Gateway 重启后读取现有 State，从
`last_event_offset + 1` 继续读取 Canonical EventStore，不重新总结全部历史。Plugin 的
`state_schema_version` 变化时必须由该插件显式迁移；无法迁移会隔离 Observer，而不会猜测状态。

## Progress 与意图纠偏

运行中的 Progress Markdown 由 Core 的 `render_observer_progress()` 生成：

```text
## 用户问题
## 已完成
## 进行中
```

CLI 的会话级 Textual TUI 使用持久双栏 workspace 展示 Main Agent 时间线和当前 Turn Observer，完成前后
不会从左右结构重排为上下两个 Panel。成功且最终意图一致时，Observer 完成态收敛为
`✓ 任务已完成`，并保留最多 6 条经长度限制的已完成内容；失败、取消、无法确认或意图偏移使用
各自的紧凑终态。Observer Markdown 标题、段落和列表在该窄栏内使用紧凑间距，不再为“用户问题”
与进度之间保留大片空白。
Web 保持主聊天区域不变，在右侧显示同一状态。
查询接口为 `GET /api/v1/observer/runs/{run_id}`。

LLM 可将 `intent_alignment` 标为 `aligned`、`uncertain` 或 `drifted`。Turn 运行中只持久化状态并
更新进度展示，任何中间 `drifted` 都不会提示用户。只有处理 Run 终态事件后得到的最后一份 JSON
仍为 `drifted` 时，Core 才创建 Correction Proposal。用户可采用、编辑后采用或拒绝；决定使用
revision CAS，60 秒超时默认拒绝。Observer 不会自行修改 Prompt、计划或重新执行 Run。

## Tool Evidence

Tool Evidence 不调用模型。ReAct Loop 在 `tool_requested` 的公开元数据中增加 loop number、
`serial|parallel` execution 和原始 position。Observer 仅保存同一 loop 的 Tool 名称顺序：

```json
{"loop": 2, "execution": "parallel", "tools": ["code_search", "read_file"]}
```

不同 loop 分开，同一 loop 不去重；参数和结果不会进入 Observer 数据库。

## Evidence 与 Skill 发布

终态 Evidence 包含 Profile 身份、用户问题、完成项、可见执行摘要、用户纠正、最终对齐状态、
被采用的纠偏提示和 Tool loops。`completed_tasks` 与执行摘要由 Core 从已持久化的 Visible Event
及 Tool Metadata 按规则重新构造，不信任 LLM 状态中的完成项；LLM 只负责进度展示和最终意图判断。

Dream 完成阶段只在同一 `runtime_profile + trigger` 至少积累配置数量的 finalized Evidence 后
创建 Candidate，默认阈值为 3。Candidate 仍需人工批准、Skill 结构验证和现有 Runtime Plugin
reload；发布会产生新 Generation，只在下一 Turn 生效。Harness Candidate 写入对应 trigger 的
专用 Skill 根；Main、Cron 与 Dream Candidate 分别写入
`runtime-resources/<interactive|cron|dream>/skills/`。这些目录只进入对应 Profile Snapshot，
禁止自动跨 Profile 共享；仓库顶层 `skills/` 只保留显式共享/内置资源。

相关接口：

```text
GET  /api/v1/observer/skill-candidates
POST /api/v1/observer/skill-candidates/{candidate_id}/decision
POST /api/v1/observer/corrections/{proposal_id}/decision
```

## 当前安全边界

Observer 插件仍是与 Gateway 同进程运行的 Python 代码。不可变 Generation、输入裁剪、无执行
Facade 与 Quarantine 能限制正常插件路径和故障扩散，但不是针对恶意 Python 的 OS Sandbox。
因此 Observer 来源仍必须受信任，Core 的 VisibleEventProjection 和 StateStore 不能进入普通
热更新范围。

## 可靠性补充

- Core 使用 `observer_plugin_timeout_seconds` 限制插件的提示组装和 JSON 校验，默认 2 秒；
  `observer_model_timeout_seconds` 单独限制 LLM 状态更新，默认 60 秒。Observer 可通过
  `observer_model` 使用独立模型，未配置时复用主模型配置和同一 `AgentRuntime` 实现。
  插件回调超时归因到插件版本；模型超时、网络错误和非法模型输出归为 Provider Failure，
  不累计插件 Quarantine。两者都不会让 Main/Harness Run 失败。Python 无法安全终止已在执行的
  线程，因此回调使用一次性 daemon worker；旧回调可能一直存活到自行返回，但不会阻止 Gateway
  进程退出。
- Observer 状态写入接入 Agent Home `WriteGate`。Backup、Restore 与 Maintenance 会等待
  Observer 到达空闲边界，避免快照捕获只写了一半的 state/offset 事务。
- 终态 Event 与 finalized Evidence 是两个持久步骤。启动恢复发现 active instance 的 offset
  已覆盖终态 Event 时，会直接补齐 Evidence，不重新运行 LLM 状态更新。
- `adopt` 与 `edit` 会在原 Session 中幂等创建纠偏 Run；`reject` 不执行建议。对同一 Proposal
  重复提交冲突决定会被状态与 revision 契约拒绝。
- Skill Evolution 除满足最小 Evidence 数量外，同一步骤还必须得到至少两个不同 Run 的支持；
  不再因 Evidence 数量达标而生成通用兜底 Skill。
