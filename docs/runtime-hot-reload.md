# Runtime 热插拔架构

YY Agent 的边界固定为：

```text
Stable Core
+
Hot-pluggable Capability Layer
```

Gateway 生命周期、StateController、SQLite migration、SessionStore、Canonical Memory
Store、ReAct Loop、HookExecutor/HookRegistry、Credential、WriteGate、Git 安全、Sandbox
底层执行器以及 Run/Operation/Attempt/Recovery 状态机都属于 Stable Core。普通 reload
不能替换这些组件。

## Generation、Snapshot 与 Bundle

所有可热更新能力先由 `RuntimeResourceProvider` 发现，并输出统一契约：

```text
RuntimePluginDescriptor
└─ RuntimePluginContribution
   ├─ TOOL
   ├─ SKILL
   ├─ STABLE_PROMPT
   ├─ HOOK
   ├─ EXTENSION
   └─ DYNAMIC_CONTEXT
```

- Generation：持久化、内容寻址、不可修改的完整资源版本。
- Snapshot：某个 Generation 经 Runtime Profile 隔离后形成的不可变视图。
- Bundle：从 Snapshot 建立的 Tool Registry、Skill Service、Prompt、Extension 和运行时适配器实例。

Runtime 只从 `.yy/runtime-plugins/generations/<generation>/` 的只读快照装配，绝不执行
正在编辑的源文件，也不使用 `importlib.reload()` 覆盖旧模块。`source_hash` 记录来源；
`semantic_hash` 忽略纯注释、换行与无语义格式变化。

## Hook 的职责

HookExecutor 和 HookRegistry 是不可热替换的生命周期总线。插件贡献的是回调或适配器，
然后由固定总线挂载；Hook 本身不是资源事实源，也不负责执行 Tool。

```text
Generation → Profile Snapshot → Bundle
                              ↓
                       fixed HookRegistry
                              ↓
             TRACE / TURN / MODEL / TOOL lifecycle
```

Memory Retrieval/Projection、Sandbox Policy/Context 和 Dynamic Context 都以窄适配器进入
Bundle。Canonical Memory Store 与 Sandbox Backend 仍属于 Core。Dynamic Context 回调在
`MODEL_BEFORE` 准备当前请求投影；ReAct Loop 只消费 Hook 产生的确定性请求操作。

Extension Hook 和其他插件 Hook 回调统一通过 `RuntimeHookCallbackContribution` 装入现有
HookExecutor。默认采用 `ISOLATE`，超时与异常可归因到具体插件版本；插件不能替换
HookExecutor 或修改已经冻结的 HookPlan。

## Reload 与持久审批

```text
discover
→ parse/hash
→ dependency/conflict/core-api validation
→ profile isolation validation
→ build immutable candidate
→ contract/smoke test
→ RuntimeReloadPlan
→ durable approval（能力面扩大时）
→ SQLite CAS activate
```

验证失败或审批缺失时，当前 active Generation 不变。Tool Schema、risk、权限契约、Hook
执行契约或 Profile 暴露范围扩大时必须批准精确 `plan_hash`。审批和完整 Plan 保存在
SQLite；Gateway 重启后只在源码仍生成相同 plan/generation hash 时复用。Skill 或稳定
Prompt 的非权限内容更新可以自动激活。

Watcher 只发现变化并调用同一 Reload Pipeline。它使用稳定窗口和构建锁；Backup、
Restore、Maintenance、WriteGate 非 RUNNING 状态以及 Gateway shutdown 都会阻止发现、
构建和激活。恢复维护后重新等待资源树稳定。

## Turn 边界与恢复

一个 Runtime Trace 内的 Snapshot 不可变：

```text
Turn N 使用 Generation 12
→ Generation 13 激活
→ Turn N 继续使用 12
→ 下一 Turn 才关闭旧 Runtime 并从原 Session 建立 Generation 13 Runtime
```

Run 在进入 Runtime 前会用 SQLite 引用绑定确切 `generation_id`。崩溃恢复只读取这个
绑定，不能默认选择最新 Generation。Cron Dispatch 还持有独立 Cron 引用；Harness
Invocation/Code Session 持有 Harness 引用。`/code` 的多 Turn 只在 Turn 边界切换版本，
并复用原 worktree 和 Harness Memory Session。

## Profile 隔离

- Interactive：主 Agent 资源；不含 Harness 内部资源。
- Cron：独立 Snapshot，再与 Dispatch 冻结的 Tool/Skill allowlist 取交集；不加载 Extension。
- Subagent：独立 Snapshot，Tool 必须是父 Runtime 已允许集合的子集。
- Compression：空能力面，不加载 Tool、Skill、Extension 或动态适配器。
- Maintenance：只保留维护所需 Core adapter，不加载 Tool/Skill/Extension。
- Harness：`common + current trigger`，四个 Profile 互相不可见。

Harness 资源固定为：

```text
harness-evolution/runtime/tools/common
harness-evolution/runtime/tools/<manual|error|capability|dream>
harness-evolution/runtime/skills/common
harness-evolution/runtime/skills/<manual|error|capability|dream>
```

最终授权始终是 Manifest 申请、Generation 绑定授权与 Runtime Profile Policy 的交集。
Reload 不会自动扩大原有权限。

## Quarantine、Rollback 与 GC

插件实现异常、插件超时、契约违规和加载失败会累计插件版本健康计数。业务失败、用户参数
错误、Provider 故障和 Policy Denial 只记审计，不计入坏版本阈值。成功回调只清零连续
失败 streak，不删除历史失败证据。达到阈值后发布一个新的完整 Generation，把故障成员
回退到最近健康版本；当前 Turn 不重跑，也不切换版本。

历史 Generation 不修改。默认保留 30 天；GC 仅删除已 retired/quarantined 且超过保留期
的快照目录。Run、Runtime、Operation、Cron、Harness 或 Recovery 的活动引用以及任一
Profile head 都会阻止删除。GC 先在 SQLite 写入删除 fencing，再删除目录；进程在两步间
崩溃时，下一次启动继续相同清理，不重新选择其他 Generation。Generation 元数据、Reload
审计、审批与健康证据仍保存在 SQLite。

## 同进程边界

热插拔只提供版本、权限、装配和恢复边界，不是恶意 Python 的强隔离。Extension Hook
仍与 Gateway 同进程；AST 检查、Capability Policy 和受控 Facade 只能约束受信任或
半受信任插件的正常执行路径。
