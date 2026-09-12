# Runtime Observer 架构

Runtime Observer 将“执行”与“观察”分开：Main/Harness Runtime 继续执行任务，Observer 只根据
用户已经能够看到的 Gateway Event 维护一份进度状态。Observer reducer 是可热更新插件；事件
可见性、持久状态、offset、恢复、审批和传输均由不可热替换的 Gateway Core 负责。

## 数据流与边界

```text
Canonical Gateway Event
        ↓
VisibleEventProjection（Core allowlist + 字段裁剪）
        ├─ 正常 EventBus / CLI / Web
        └─ Observer reducer（Generation Plugin）
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
它只能实现纯状态转换：

```text
Previous ObserverState + one VisibleObserverEvent → New ObserverState
```

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

统一 Progress Markdown 由 Core 的 `render_observer_progress()` 生成：

```text
## 用户问题
## 已完成
## 进行中
```

CLI 使用左右两列展示主 Agent 和 Observer；Web 保持主聊天区域不变，在右侧显示同一状态。
查询接口为 `GET /api/v1/observer/runs/{run_id}`。

插件可将 `intent_alignment` 标为 `aligned`、`uncertain` 或 `drifted`。Turn 运行中只持久化状态。
终态仍为 `drifted` 时，Core 创建 Correction Proposal。用户可采用、编辑后采用或拒绝；决定使用
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
被采用的纠偏提示和 Tool loops。摘要只来自 Visible Event 与 Tool Metadata。

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
