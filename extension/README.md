# Hook Extension 开发指南

`extension/hook/` 是 Yuan Ye Gateway 启动时加载的进程内扩展目录。源码变更后需要重启 Gateway；纯管理员 Grant 变更只影响下一条 Trace。

## 模块结构

每个阶段目录只接受直接子级的 `[a-z][a-z0-9_]{0,63}.py` 文件，不跟随符号链接。模块必须静态声明：

```python
EXTENSION_NAME = "request-audit"
PRIORITY = 0
EXTENSION_MANIFEST = {
    "schema_version": 1,
    "capabilities": ["logger.write"],
    "allowed_tools": [],
    "timeout_seconds": 5,
}

async def handle(event, context):
    context.log("request observed")
```

Manifest 必须是 AST 可解析的字面量。Loader 会在导入模块前完成路径、AST、Manifest 和静态安全检查；导入后的 Manifest 必须与 AST 结果完全一致。普通 Extension 默认超时 5 秒，上限 30 秒。长任务不得阻塞 Hook，应交给已有 Durable Operation；没有合适的 Durable 能力时应拒绝实现。

## Capability 与 Grant

Manifest 只是权限申请，不是授权。有效权限始终是：

```text
Manifest 申请 ∩ 当前 source/manifest 的持久 Grant ∩ Runtime Policy
```

Capability 分级：

- SAFE：`session.read`、`state.read`、`logger.write`、`workspace.read`
- CONTROLLED：`memory.read`、`memory.append`、`model.request.modify`、`tool.request.modify`
- PRIVILEGED：`tool.invoke`

正常 `/code` 流程会在候选合并前生成 Grant Plan。SAFE 自动授权；CONTROLLED 和 PRIVILEGED 随最终 Candidate 一次性确认。Grant 绑定精确的 `source_hash`、`manifest_hash`、Tool contract hash 和单调 `grant_version`。

运行中的 Trace 使用创建时冻结的 Grant 快照。管理员 `grant/revoke` 不会热更新当前 Trace，只影响下一条 Trace。Extension 在运行期请求未授权能力时只会得到 `DENIED` 并写审计，普通 Agent 不进入 `WAITING_HUMAN`。

## Tool 预授权

`tool.invoke` 必须同时声明精确 `allowed_tools`；不允许 `*`、glob 或模式匹配。目标 Tool 还必须由核心 Tool contract 显式设置 `extension_preapproval = True`。

调用时仍会执行 Schema、动态风险、Operation/Attempt、幂等和 Recovery 检查。未授权或 contract hash 已变化时，Ledger 写入确定性的 `SKIPPED` Attempt，Hook 被隔离，绝不会转入在线人工审批。产生 UNKNOWN 外部副作用时仍进入既有 Durable Recovery。

## Event 修改

Extension 接收只读 `ExtensionEventView`，只能通过 `ExtensionContext` 写入本次 Hook 的 `ExtensionMutationBuffer`：

- `model_before` 可替换 `messages`、`tools`
- `tool_before` 可替换 `name`、`arguments`

Hook 成功后才重新校验并原子提交 Patch。异常、超时、权限拒绝或契约错误会丢弃整个 Buffer，不会留下半修改 Event。

## 故障隔离与 Quarantine

Core Hook 为 `FAIL_CLOSED`；Extension Hook 为 `ISOLATE`。Extension 异常或超时不会阻止后续 Hook和普通 Agent Runtime，`asyncio.CancelledError`仍向上传播。

状态按 `(hook_id, stage, source_hash)` 保存：

- exception/timeout 增加 runtime failure 总数与连续 streak；连续 3 次 quarantine 当前源码版本
- 成功执行清零 runtime failure streak
- capability denied、Tool 未预授权、Grant revoked 只增加 policy denial，不参与 quarantine
- Contract violation 单独计数

新 `/code` 候选产生新的 `source_hash`，不会继承旧版本的 quarantine。`/extension reenable`仅用于重新启用同一个源码版本。

## `/code` 边界

Extension 模式只允许修改：

```text
extension/hook/**
tests/extensions/**
extension/README.md
```

验证顺序为源码范围、AST、Manifest、Capability 分类、Tool allowlist/contract、Grant Plan、Contract/Loader/HookExecutor 测试和回归测试。通过后才形成 verified candidate；需要确认的权限必须绑定精确 `plan_hash` 才能 fast-forward merge。

## 安全边界

AST 扫描、Facade 和 Capability Policy 是受控执行机制，不是恶意 Python 代码的强安全隔离。Hook 仍与 Gateway 位于同一个 Python 进程；一旦代码绕过静态检查，就具有同进程权限。本版本不提供独立进程、容器、seccomp、网络 namespace 或 Python 强 Sandbox。
