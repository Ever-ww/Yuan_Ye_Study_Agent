# `run_ui`：CLI 与本地 Web 界面

`run_ui` 是 Yuan Ye Study Agent 的交互层。业务状态、权限判断和工具执行仍由
`AgentRuntime` 负责，这里只提供三种入口：

- `cli.py`：正式的 Typer/Rich 命令行界面。
- `web.py`：复用同一运行时的 FastAPI 本地 Web 界面。
- `console.py`：为原同步 `Agent` API 保留的轻量终端兼容层。

项目安装、配置和安全模型请先阅读[根目录 README](../README.md)；内部模块说明见
[开发参考](../DEVELOPMENT_REFERENCE.md)。

## 安装与启动

在项目根目录使用 Python 3.10+ 安装：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
yy-agent doctor
```

以下入口均启动同一套新 Harness：

```powershell
yy-agent                         # 默认进入交互会话
yy-agent chat                    # 显式进入交互会话
yy-agent run "检查当前项目的测试缺口"
python -m run_ui run "检查当前项目的测试缺口"
python run.py "检查当前项目的测试缺口"  # 旧的一次性任务快捷写法
```

`python run.py` 与 `python -m run_ui` 不带子命令时都会进入交互会话。只有
`run.py` 会把未识别的第一个参数自动解释为 `run` 的任务；使用 `yy-agent` 或
`python -m run_ui` 时应显式写出 `run`。

运行时可覆盖模型、Prompt profile 和权限模式：

```powershell
yy-agent run "审查这次修改" --model deepseek --profile code --permission-mode risk-based
```

## 常用命令

下面的名称与 `cli.py` 当前注册的命令一致；参数详情以
`yy-agent <命令> --help` 为准。

| 范围 | 命令 |
| --- | --- |
| 运行 | `chat`、`run`、`serve`、`doctor`、`migrate` |
| 凭据 | `auth set`、`auth delete` |
| 会话 | `session list`、`session show`、`session rewind` |
| 记忆 | `memory list`、`search`、`add`、`show`、`edit`、`forget`、`export` |
| 学习资料 | `corpus index`、`corpus search` |
| Skills | `skill list`、`add`、`update`、`remove` |
| 插件 | `plugin list`、`install`、`update`、`enable`、`disable`、`trust`、`uninstall` |
| 插件市场 | `plugin marketplace list`、`add`、`update`、`remove` |
| Prompt 与执行策略 | `prompt inspect`、`hooks`、`sandbox` |
| 定时任务 | `cron list`、`create`、`delete`、`approve-missed` |
| 调度器 | `scheduler start`、`status`、`stop`、`install-autostart` |
| 子代理 | `agent list`、`agent run` |
| 团队 | `team create`、`tasks`、`add-task`、`run`、`send`、`receive` |
| 外部协议 | `mcp list`、`probe`、`call`、`serve`；`lsp list` |

典型操作示例：

```powershell
yy-agent session list
yy-agent memory search "测试约定" --scope project
yy-agent corpus index paper
yy-agent skill add "https://github.com/example/skills.git#skills/review" --ref v1.0.0
yy-agent plugin marketplace list
yy-agent prompt inspect --render
yy-agent cron create "*/30 * * * *" "检查项目状态" --timezone Asia/Shanghai
yy-agent scheduler status
```

### 交互会话命令

`yy-agent chat` 中支持：

- `/help`：显示交互命令。
- `/exit`、`/quit`：结束会话，并取消当前会话创建的 Loop。
- `/memory`、`/plugin`、`/cron`：查看相应的当前状态。
- `/prompt`：查看分层 Prompt 的来源和估算 token 数。
- `/rewind <seq>`：将当前会话回滚到指定事件序号；发生并发修改冲突时会停止。
- `/loop <间隔> <prompt>`：创建仅在当前进程存活的重复任务，间隔支持 `s`、`m`、`h`。
- `/loop list`、`/loop cancel <id>`：查看或取消会话级 Loop。

会话级 `/loop` 不会写入持久 Cron。需要重启后继续运行的任务，应使用
`cron create` 和 `scheduler start`。持久任务会保存创建时批准的能力边界与插件能力快照；
调度器不会把本进程仍在执行的长任务当成五分钟租约过期的崩溃残留，真正无法确认结果的
陈旧任务会停在 `needs_approval`。

## Web UI

```powershell
yy-agent serve --port 8765
```

启动后终端会打印带随机 token 的 URL。当前浏览器页面提供聊天事件流、项目与沙箱状态、
审批队列和 Agent 提问队列。聊天事件通过 WebSocket 传输；经过 token 校验的本地 API
还提供会话事件、Memory、资料搜索，以及 Skills、插件、Hooks 和 Cron 状态。

安全边界：

- 服务最终固定绑定 `127.0.0.1`；CLI 不提供远程监听能力。
- HTTP API 和 WebSocket 都校验启动时生成的随机 token。
- 审批决定和问题回答还必须通过 CSRF 校验。
- 所有 HTTP 响应设置 `Cache-Control: no-store` 等缓存禁用头，避免 token/CSRF 落入缓存。
- 响应设置 CSP、`X-Frame-Options`、`X-Content-Type-Options` 和无引用来源策略。
- Web UI 不会绕过运行时的权限、Capability Grant 或沙箱规则。

这个界面面向单用户、本机使用，不应通过端口转发或反向代理暴露到局域网或互联网。
终端打印的访问 URL 含有 bearer token，请勿粘贴到日志、Issue 或聊天记录中。

## 旧同步终端兼容层

`console.py` 的 `DynamicCLI` 和 `run_with_spinner` 继续服务原有同步
`Agent.run()`/`AgentResult` API。它本身只使用 Python 标准库，提供旋转状态提示、
`/help`、`/exit` 和工具轨迹输出，但不包含新 CLI 的持久会话、审批面板、Memory、Cron
和团队命令。

它不再是 `run.py` 的默认实现。新代码应优先使用 `yy-agent`，或直接通过
`AgentRuntime.run_turn()` 消费异步事件流；只有维护旧同步集成时才需要引用
`run_ui.console`。

## 依赖与开发扩展

- Typer 与 Rich 支撑正式 CLI；FastAPI 与 Uvicorn 支撑 Web UI，均属于基础依赖。
- `auth set/delete` 需要额外安装 `.[keyring]`。
- `mcp probe` 可用于报告 SDK 是否缺失；实际 `mcp call/serve` 需要额外安装 `.[mcp]`。
- Docker、Playwright 和 Windows UI Automation 属于运行时工具能力，不由 UI 层放宽权限。

扩展命令行时，在 `cli.py` 注册命令并通过 `make_runtime()` 获取统一配置和审批回调；
异步操作应由入口显式驱动。扩展 Web API 时，每个读取端点都必须校验 token，改变状态的
端点还必须校验 CSRF。不要在 UI 层直接执行工具、写会话数据库或复制权限逻辑。

修改后至少运行：

```powershell
python -m pytest
yy-agent --help
yy-agent doctor
```
