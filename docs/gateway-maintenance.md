# Gateway 维护状态机与恢复边界

## 1. 解决的问题

旧实现已有 WriteGate 与 Participant，但 Gate 状态和 Coordinator Snapshot 只在内存中保存；
维护失败会尝试自动恢复服务，Participant resume 错误被 gather 吞掉。
RuntimePool 的 drain gather 被 timeout 取消时可能连带取消实际 Run。
HTTP 入口检查不能覆盖内部 dispatch，也不能保护“检查通过后才开始写事务”的线程竞态。
旧 stop.request 只表示停止意图，无法证明当前实例已经排空；超时还会回退到进程信号。

本次沿用 AgentHomeWriteGate / AgentHomeMaintenanceCoordinator，不另建 Run 或 Tool 状态机。

## 2. 唯一权威与状态

维护事实保存在 `<agent_root>/.yy-backups/control/gateway/lifecycle.sqlite3`：

- `lifecycle_state`：唯一 singleton snapshot，含 revision、epoch、operation_id、原因与时间。
- `lifecycle_transitions`：append-only 迁移 Evidence，与 snapshot CAS 同一 SQLite 事务提交。
- 同步级别 FULL；连接每次操作后关闭，不跨恢复目录替换持有连接。

这是外部维护控制领域的权威，不是另一套 Gateway Run Event Store。普通业务审计仍走
StateController → gateway_events → Outbox。维护记录必须在 `.yy` 不可写、Outbox 暂停或
目录已被替换时仍能提交，故不把 Gateway Event Delivery 当作维护状态提交条件。

`instance.json` 只记录进程发现信息；stop.request 是命令，stop.ack 是处理结果；它们均不决定准入。
进程内 active scopes 仅记录当前存活工作，不作为崩溃后的 Run 重放依据。

```text
RUNNING → QUIESCING → QUIESCED → RESUMING → RUNNING
                         ↓
                      RESTORING → RESUMING

关键步骤失败 → FAILED
FAILED → 显式重新 quiesce 或显式 resume（仍须检查）
```

序列化使用小写值。旧 DRAINING/FROZEN Python 枚举名仅是 QUIESCING/QUIESCED 别名，
不是两种额外状态。epoch 是维护代数，revision 是每次状态迁移的 CAS 版本。

## 3. 准入、排空与并发

`AgentHomeWriteGate.work()/operation()` 在同一 threading.RLock 内完成状态读取与 scope 注册。
迁移也使用该锁，因此不存在检查 RUNNING 后、进入 QUIESCING 才注册新工作的窗口。
ContextVar 保存当前 scope；每次使用都会确认它仍存在于活跃表，继承了已结束 scope 的子任务不能绕过屏障。

只有 RUNNING 可以接受新 workflow。已有被准入的 Run 在 QUIESCING/FAILED 可以完成其模型、
普通 Tool、收尾和持久化，但不能启动新的 Subagent、Cron 管理或 Harness workflow。
已知在 Tool body 执行前拒绝的调用记为 NOT_EXECUTED/SKIPPED，不伪造未知源码副作用。
运行中的 repair loop 是已有 Invocation 的延续，仍受原有预算和超时限制。

覆盖 API 与应用服务、RuntimePool、CodeSession、ERROR/CAPABILITY/DREAM、Cron/Dream tick、
Subagent Tool、Embedding workers、Event Archive 和 idle-runtime reaper。维护控制接口不获取普通
业务 lease，否则 quiesce 会等待自身。取消/审批只允许针对已存在的 Run/Approval；维护期不会因此调度新 Run。

StateController 的同步 connection/transaction 与迁移共享锁，并在 SQLite authorizer 中重新检查写准入。
已有业务事务先完成，再进入 QUIESCING；没有有效 scope 的新写入被拒绝。控制启动所需 schema 初始化
单独处理，不把它当作 Run 执行许可。

`get_lifecycle_state()` 提供 active_runs、active_tool_calls、active_background_jobs、
active_db_transactions 和每个 scope 的身份。DB 数量计数覆盖 StateController 事务；其他领域的
短连接事务受外层 Run/worker scope 保护，不声称逐一计量所有第三方 SQLite 连接。

排空链路：

```text
API / CLI / Backup
→ Coordinator.quiesce
→ 外部 maintenance.lock + durable QUIESCING
→ 拒绝新工作，等待已准入 scopes 归零
→ Participant quiesce ACK
→ Trace end / Runtime 释放完成（仍在受控 drain scope 内）
→ 数据库 quick_check / foreign_key_check / WAL checkpoint
→ 再检查 idle
→ durable QUIESCED
```

Outbox backlog 只要求已持久化，不要求离线客户端收到所有事件。
普通 idle Runtime 在排空阶段释放；MANUAL 保留 Runtime 对象、worktree 和 Memory Session，
关闭当前 Trace，resume 后的下一 Turn 重新开启 Trace。进程退出不会在 QUIESCED 后追加 CodeSession 审计。
Timeout、取消或 Participant 错误都会留下 FAILED 与未退出 scope Evidence；不会自动 resume 或强杀 Run。
RuntimePool 用 shield 等待 Run；SQLite flush 线程开始后等待其完成才能释放维护所有权，
因此不可中断的底层 I/O 可能使 timeout 的返回晚于配置秒数，但不会假称已安全排空。

## 4. Resume 与启动恢复

```text
检查 epoch/revision + Restore Fence
→ durable RESUMING
→ 再次暂停全部 Participant（兼容之前只排空一部分的失败）
→ SQLite integrity/FK/schema
→ 释放 idle Runtime 缓存
→ Outbox / Archive reconcile
→ Memory watermark、Tool Schema、Skill Catalog 检查
→ Participant resume 全部成功
→ durable RUNNING
→ 若是控制模式启动，启动通常的 Recovery / worker / scheduler
```

任何检查失败保持 FAILED。控制模式开启普通服务失败也立即重新封闭，不能报告恢复成功。
恢复既有 Run 仍由原 RecoveryCoordinator / Operation / Attempt Evidence 决定，维护状态机不重放 Tool。
健康检查不发送模型请求，不代替 Provider 可用性探测。

进程重启读取外部状态：

| 持久状态 | 启动行为 |
| --- | --- |
| RUNNING | 正常 Recovery 和调度 |
| QUIESCING / RESUMING | 写 interrupted 失败事实，转 FAILED，只启动控制面 |
| QUIESCED / RESTORING / FAILED | 保留状态，只启动控制面，等待显式恢复 |

不从 PID、请求文件存在性或旧备份猜测能否运行。运行期控制存储写失败会封闭本进程的新工作；
若磁盘无法写入，不会假装 FAILED 已 durable 提交。应修复存储后重启检查。

## 5. Backup、Restore 与 stop

成功备份可以自动 resume；失败备份保留 FAILED、导出 Evidence，等待显式处理。
整体 `.yy` Restore **仍然要求 Gateway 停止**：这是已有 instance lock + Restore Fence 的更强边界，
不为实现在线维护而允许进程持有旧 DB/cache 时替换目录。

Restore 在外部状态进入 RESTORING 后才准备替换目录。成功保持 RESTORING，失败保留 FAILED；
Journal/Fence 继续负责 rename 崩溃恢复。先解决未完成 Restore，再启动 Gateway 控制面并显式 resume。
恢复旧 `.yy` 不会覆盖 lifecycle DB、instance_id、锁、请求或当前维护 operation_id。

stop.request 的严格模型含 version、request_id、instance_id、action、reason、requested_at、requested_by、timeout。
Gateway 完成 lifespan 初始化后才处理请求，验证 instance_id，真正 quiesce 后原子写 stop.ack。
ACK 含请求 hash、状态、revision、完成时间和结构化错误。同请求重复处理幂等，旧实例请求不会停止新实例。
官方 stop 不再 SIGTERM 回退；失败时保留现场。Harness 自动重启仅能自动恢复自己 request_id 对应的
QUIESCED 维护，不会解锁管理员发起的维护。

普通 `gateway stop --timeout 30` 将任务 drain 与连接退出分开等待：默认 drain
30 秒，再给 ASGI 退出 10 秒。WebSocket 同时等待事件和客户端断开，空闲连接
不会把停机永远挂住；Uvicorn 的连接收尾最多等待 5 秒。没有成功 quiesce 时
不会通过超时强杀 Run。失败 ACK 会立即反馈 CLI，不再展示内部 traceback。

正常 operator stop 完成整个 ASGI/lifespan 退出后才写 `stopped.json`。
下一进程只有当它与 Canonical lifecycle 的 epoch、revision、operation_id
全部匹配，且状态为 `QUIESCED / operator stop` 时，才通过现有健康检查与 CAS
resume 恢复准入。崩溃、FAILED、Backup/Restore 或不匹配证据仍是 control-only。
旧版本遗留的 QUIESCED 实例没有该完成证据，需要检查后显式 `gateway resume`；
不删状态库、instance lock、stop.ack 或恢复证据来伪造“已恢复”。

## 6. 运维入口

```text
uv run python run.py gateway status
uv run python run.py gateway quiesce --timeout 30 --reason maintenance
uv run python run.py gateway resume --epoch <epoch> --revision <revision>
```

HTTP：`GET /api/v1/maintenance`、`POST /api/v1/maintenance/quiesce`、
`POST /api/v1/maintenance/resume`。沿用现有鉴权，resume 请求携带 maintenance_epoch 和 expected_revision。
维护期新 mutation 返回 503，查询接口仍可用；需要创建或恢复可变资源的“查询”不伪装成纯只读。
Health 的进程存活与业务 readiness 分离：应读取 accepting_work 和 maintenance，而不是仅看 HTTP 200。

## 7. 修改区域与验证

- `backup/{models,lifecycle_store,maintenance,service,restore,scheduler}.py`：状态、CAS、排空、恢复与备份。
- `gateway/{application,api,client,process,restart}.py`：所有入口、检查、协议与控制模式启动。
- `gateway/{state_controller,store,runtime_pool,durable_execution,code_sessions,harness_evolution}.py`：事务、Run 与 Invocation 准入。
- `gateway/{outbox,event_store}.py`：暂停 Delivery/Archive，等待实际任务完成。
- `cron/scheduler.py`、`memory/embeddings.py`、`reference/embeddings.py`、`tools/subagent.py`：内部任务屏障。
- `run_ui/cli.py`：状态与显式维护命令。
- `tests/test_gateway_maintenance.py`、`tests/test_backup.py`：状态、线程竞态、真实 Run drain、子任务拒绝、
  timeout、resume 失败、磁盘写失败、HTTP、ACK、离线 Restore，以及 os._exit 强制退出窗口。

本次验证（Windows，2026-09-10）：

- 完整 pytest：420 passed，3 skipped。
- unittest discover：321 tests，OK，3 skipped。
- 维护专项：17 项通过；包含 Trace end 在 QUIESCING 完成及丢失 snapshot 不会自动初始化的断言。
- compileall、`uv lock --check`、`git diff --check` 通过。
- 跳过项为两项显式 opt-in 的真实 OS/ACL 集成测试和一项真实 Docker 集成测试。
  未在 Linux/macOS 实机上验证，也没有为了测试停止用户正在使用的 Gateway。

## 8. 限制

- single Gateway；SQLite CAS 和进程锁不是多主集群协议。未知外部进程直接写 Agent Home 不受协作 Gate 控制。
- 这是受信任代码的协作准入，不是 OS sandbox。新增业务 writer/worker 必须接入同一 Gate。
- 活跃 scope 是内存存活性；崩溃后不能据其计数推断 Tool 成败，仍以 Durable Run Ledger 为准。
- 数据库损坏到无法构造应用时，控制 HTTP 也可能无法启动；外部 lifecycle 状态仍保留，需要离线 Recovery。
- 不支持热替换 `.yy`、热升级 Python Tool，也不因 drain timeout 强杀用户工作。
