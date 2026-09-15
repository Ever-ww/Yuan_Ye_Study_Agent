# Runtime 逻辑路径与沙箱边界

YY Agent 把“模型看到的路径”和“Gateway 实际访问的宿主机路径”分开。逻辑路径方便模型稳定地描述文件位置，但它本身不是安全机制；Tool Policy、Capability Policy 和 OS Sandbox 才负责授权。

## 命名空间

| 逻辑根 | 含义 | Windows Shell | Linux/macOS Shell |
|---|---|---|---|
| Workspace | 当前用户任务目录 | `YYWorkspace:\` | `/yy/workspace` |
| Agent Source | YY Agent 当前源码视图 | `YYAgentSource:\` | `/yy/agent-source` |
| Skills | Agent Source 内的 `skills` | `YYSkills:\` | `/yy/skills` |
| Hooks | Agent Source 内的 `extension/hook` | `YYHooks:\` | `/yy/hooks` |

`YYSkills` 与 `YYHooks` 不是独立存储，也不能独立配置。它们始终从同一份 `YYAgentSource` 映射派生。

## Runtime 冻结规则

每个 Runtime/Trace 创建一份不可变的 `PathMappingSnapshot`。并发中的 Interactive、Cron、Subagent 或 Harness Runtime 不共享可变的全局路径映射。

普通 Runtime 的 Agent Source 指向正式源码身份。Harness 或会修改代码的 Dream Runtime 创建 Git worktree 后，同时冻结：

```text
YYWorkspace    -> 当前 worktree
YYAgentSource  -> 当前 worktree
YYSkills       -> 当前 worktree/skills
YYHooks        -> 当前 worktree/extension/hook
```

因此 Coding Agent 无法通过 Skill/Hook 别名绕过 worktree 去写正式 checkout。验证和合并成功后，Runtime Resource 系统从正式合并结果构建新的不可变 Generation；旧 Runtime 关闭后才能回收 worktree。热更新不会修改正在执行 Turn 的路径或资源视图。

## 解析与授权

文件 Tool 统一通过 `ToolContext.resolve_workspace_path()` 解析。解析器拒绝：

- `..` 越界；
- 宿主机绝对路径、UNC 和 Windows device path；
- `$HOME`、`%USERPROFILE%`、`~` 等展开；
- 穿过 symlink、junction 或 reparse point 的现有路径。

解析成功只证明路径属于某个逻辑根。调用方仍必须通过该 Tool 的权限判断、文件锁、WriteGate、Checkpoint 和 Sandbox。Harness 仅在 Agent Source 与 Workspace 是同一个已授权 worktree 时，允许文件 Tool 使用 `YYAgentSource`、`YYSkills`、`YYHooks` 别名。

## 模型可见内容

动态 Runtime Context 只发布逻辑 Workspace。Tool Observation 在写入 Session 和发回模型前，把已知宿主机 Workspace/Agent Source/Skill/Hook 根转换为逻辑路径。真实路径仍可保留在 Gateway 内部审计证据中，但不应成为 Provider Prompt、Session Summary、Observer 或 Harness 决策的输入。

## 平台边界

- Windows：AppContainer 负责隔离，PowerShell drive 提供逻辑路径。
- Linux：bubblewrap 把 Workspace 挂载到 `/yy/workspace`，不把宿主机 Home 暴露给命令。
- macOS：Seatbelt 负责访问控制；由于 Seatbelt 不提供 mount namespace，Gateway 在进入 Shell 前翻译固定逻辑根，并在输出时反向投影。翻译不改变 Seatbelt 权限。
- Docker：是备用后端，Workspace 挂载到 `/yy/workspace`。

任何 OS Sandbox 初始化、映射或权限校验失败都必须关闭 Shell 能力，不能回退到未隔离的宿主机命令执行。
