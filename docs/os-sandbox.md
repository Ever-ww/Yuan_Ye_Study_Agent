# OS 沙箱与 Docker 备用后端

主 Agent 与 `create_coding_runtime()` 均由 `AgentRuntime` 调用
`sandbox.factory.create_sandbox_session()`。默认 `sandbox_backend="os"`。
模型只能提交 command 和 timeout，不能选择后端、扩大目录或打开网络。
原有 Tool 高风险审批、Operation/Attempt、WorkspaceLock、分支 Checkpoint
与 Trace Hook 继续生效。

```text
Tool Registry / 原有高风险审批
→ BashTool
→ CheckpointSandboxSession（workspace 独占锁）
→ OS 后端或显式 Docker 后端
→ 停止命令进程
→ 成功创建 checkpoint；已确认失败恢复 checkpoint
```

## 配置

在 Agent Home 的 `.yy/settings.local.json` 中配置，重启 Gateway 后生效：

```json
{
  "sandbox_backend": "os",
  "sandbox_shell": null,
  "sandbox_readable_roots": []
}
```

| 系统 | OS 实现 | 默认 shell | 依赖 |
| --- | --- | --- | --- |
| Linux / WSL2 | Bubblewrap 的用户、PID、网络和挂载命名空间；drop capabilities，禁止进一步创建用户命名空间 | Bash | 系统 `/usr/bin/bwrap` 或 `/bin/bwrap`，支持对应 flags 的版本、可用的用户命名空间 |
| macOS | `/usr/bin/sandbox-exec` 加 Seatbelt deny-default policy | Bash | 系统 Seatbelt |
| Windows 原生 | AppContainer，无网络 capability；Job Object 管理整棵进程树 | Windows PowerShell | 支持 AppContainer 的 Windows、本地可授权的 NTFS workspace |

Linux 需要用户自行安装发行版的 bubblewrap 包；程序不会提权修改系统配置。
WSL 内运行 Gateway 时使用 Linux 后端；原生 Windows 不通过 `wsl.exe` 绕过
AppContainer。Windows 可将 `sandbox_shell` 设为绝对路径的 Git Bash 或 PowerShell 7；
Git Bash 依赖的完整安装目录应加入只读根。命令语法以当前 Trace 返回的 `shell` 为准，
已有工具名 `bash` 为兼容继续保留。

工具链位于系统目录或 workspace `.venv` 之外时，将其**专用安装目录**加入
`sandbox_readable_roots`，例如 Python 基础解释器或 Git Bash 的安装目录。
不要填整个用户主目录、磁盘根目录、Agent Home，或与 workspace 重叠的目录。
只读根是管理员的读取授权；其中不应存放 credential。Shell 的环境从 allowlist
构建，不继承 Provider Key、Proxy、`BASH_ENV`、`PYTHONPATH` 等环境。
依赖需要预先安装；OS 命令网络默认关闭，`.venv` 只读。

Docker 是显式备用选项：

```json
{ "sandbox_backend": "docker" }
```

此时使用原有无网络、只读容器根、资源限制和 workspace mount。
OS 后端失败不会自动启动 Docker，也不会自动改成无隔离宿主机 shell。
只有明确配置才能切换后端，避免执行环境和命令语法在任务中途变化。

## 生命周期与失败

`TRACE_START` 只验证静态 Policy、打开或复用 Checkpoint 基线，并把 OS 后端标记为
`os_lazy`；普通问答不会创建 AppContainer、ACL 或隔离进程。第一次真正调用 Bash
时才复用或重建 Workspace 安全扫描缓存、建立短期 Sandbox Lease 并执行受控探测。
启动能力不足时进入 `checkpoint_only`；Registry 移除 Bash Schema，也拒绝伪造调用。
读取、编辑、写入和 Checkpoint 仍可用。Checkpoint 本身失败仍向上传播。

`/api/v1/status` 对 OS 后端只报告发现结果 `pending` 与 `first_bash_probe_required`。
它不为了健康查询创建 Runtime、AppContainer、ACL、worktree 或 Checkpoint；
实际 Bash 执行能力由每个 Trace 的首次 Bash 隔离自检确定。主 Agent 和 Harness 在临时
Query Context 中获得 shell/backend 信息，不重建稳定 System Prompt。

命令失败或超时，在停止执行后沿用 Checkpoint 恢复；取消向上层传播。
若 Job 终止或 Windows ACL 清理无法确认，返回 `SandboxRecoveryRequired`，
禁用 Shell 并保留工作区及恢复证据，不在可能仍有写进程时恢复文件。

## 文件与进程边界

依赖缓存不视为可写源码：`.uv-cache` 完全隐藏；`.venv` 和
紧邻 `Cargo.toml` 的 `target` 目录只读。uv/Cargo 在这些目录内部生成的硬链接
不再导致整个 Workspace 禁用 Shell；可写源码里的硬链接、symlink、reparse point
仍拒绝执行，并在错误中显示具体相对路径。不会修改、删除缓存或全局放行硬链接。
如需重新构建 Rust，请显式将 `CARGO_TARGET_DIR` 指向 Workspace 内新的普通输出目录。
隔离自检必须真实通过；Docker 仍仅是显式备用选项，不回退到无隔离 Shell。

Windows 在创建 AppContainer 和写 ACL 前检查所有授权路径的
`READ_CONTROL/WRITE_DAC`。由其他账户（如 CodexSandboxOffline）创建的 `.venv`
可能只有普通 Modify 权限，不允许当前用户修改 DACL；这时会明确报告
`windows_acl_permission_denied` 及路径，不先写权限再误报未知清理状态。
应由用户在停止相关进程后，以自己的账户重建工具链，或由管理员明确修复目录
权限；Runtime 不自动接管所有权、不改全局 ACL，也不要求以管理员身份运行 Agent。

Linux 只挂载系统运行库、明确只读根与 workspace；不映射用户 Home 和主机 socket。
`.git`、`.yy`、`.yy-backups`、`.agents`、`.codex`、已有 `.env*` 等保护路径被遮蔽。
macOS 通过 Seatbelt 限定读写范围并显式拒绝网络和保护路径。
POSIX 启动使用独立进程组，超时、取消与普通完成后清理该进程组；
Linux 的 PID namespace 进一步约束后代进程。

Windows 每个 Trace 创建一个 package SID，并在该 Trace 内复用 AppContainer/ACL
Lease；Workspace 或授权范围变化后，下一次 Bash 会回收旧 Lease 并建立新版本。
目录授权按保护路径分割：
含保护子项的祖先不获得整树继承写权限；保护项不进入 package allowlist；
安全子树获得局部授权。AppContainer 本身另有系统允许的运行库/注册表权限，
其访问仍受当前机器 ACL 约束；它不提供 Linux mount namespace 那样的全盘视图。
命令进程以 suspended 状态创建，加入禁止 breakaway 的 Job 后才 Resume。
Job 关闭会终止后代；输出仅继承明确列出的标准管道句柄。Job 限制 256 个进程、
1 GiB 总进程内存；输出最多捕获每个流 1 MB，Tool 展示仍有自己的长度限制。

Windows 授权前在 `.yy/sandbox/native-leases/` 持久化精确 SID 与路径。
清理只移除该 SID 的 ACE，不用旧 DACL 覆盖用户修改。每份 lease 持有 OS 文件锁，
新执行只回收已经失去所有者锁的 lease。源码工作区可能临时出现 package SID ACL；
这是原生 Windows 后端的权限装配，不是项目 Git 内容变更。

Workspace 安全扫描和 Shell 权限是两层不同事实。扫描结果缓存在 Agent Home 的
`.yy/sandbox/scan-cache/`，以 Workspace identity、Git HEAD/dirty/untracked、目录
边界、Policy 和 backend version 校验；命中时只执行 quick check，变化时才重新扫描。
该缓存不是授权。Bash 可用 `writable_paths` 声明本次所需的现有相对目录；Linux 使用
只读 Workspace 加局部 bind，macOS 只为这些目录生成 write rule，Windows 只把这些
目录加入 package 写 ACL。省略该字段时为兼容旧调用保留整个 Workspace 写权限。
`.venv`、`node_modules`、`target`、`build` 等可继承的安全子树按目录边界处理，
不逐文件重复装配权限；保护路径仍会强制切断继承。

第一版拒绝 workspace 中可写区域的 symlink/reparse point、hardlink 和特殊文件，
防止别名绕过路径边界；只读 `.venv` 不作可写区域遍历。
跨 Runtime 的可变状态独立，Harness workspace 始终是自己的隔离 worktree。

## 范围与限制

本次隔离的是模型经 BashTool 发起的 shell/子进程命令。Gateway、Checkpoint Git
维护、Harness Controller 的 Git 合并与固定验证入口仍是宿主机可信控制面；
普通 Python Tool 与同进程 Extension Hook 不会因此自动被放进 OS 沙箱。
这不改变 Harness Target Scope 校验或 Tool 审批。

macOS 没有本实现中的 Job Object/PID namespace。脱离进程组的恶意 daemon
仍继承 Seatbelt，但进程组清理不能证明这类程序全部退出。因此这里不宣称完整的
恶意代码隔离或与三个 OS 相同的资源限制；需要容器级进程生命周期时可显式选 Docker。
任何 OS 后端都不承诺抵抗内核漏洞或同权限外部程序的并发文件修改。

## 验证入口

`tests/test_native_sandbox.py` 包含策略、Registry、Checkpoint 和真实隔离测试。
真实测试需显式 `YY_RUN_NATIVE_SANDBOX_TESTS=1`，会创建临时工作区与 Windows
临时 AppContainer，并检查工作区写入、越界拒绝、保护路径、网络及取消行为。
显式启用后后端不可用是失败，不以 skip 掩盖。

`.github/workflows/native-sandbox.yml` 在 Linux/macOS/Windows 上执行相同用例。
本机 Windows 的执行结果不代表已经运行过另外两个 OS 的内核集成测试。
