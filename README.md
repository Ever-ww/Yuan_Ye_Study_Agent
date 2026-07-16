# Yuan Ye Study Agent

Yuan Ye Study Agent 是一个 Windows 优先、核心能力可移植到 Linux/macOS 的本地 Agent
Harness。它把模型调用、工具审批、会话、记忆、学习资料、Skills/插件、Hooks、定时任务、
子代理和本地 Web UI 组织在同一套可审计运行时中。

当前版本为 `0.2.0`。项目保留原有同步 ReAct API，同时提供新的异步事件驱动 Runtime；
不包含 IDE 插件、远程 Worker 或云端多租户服务。

## 核心能力

- **Agent Runtime**：异步 `RunEvent` 事件流、原生工具调用、备用模型、自动上下文压缩、
  SQLite 会话溯源和冲突安全的文件回滚。
- **模型适配**：OpenAI Responses、Anthropic Messages 和 OpenAI-compatible Chat
  Completions；网关不支持工具调用时回退到严格 JSON 协议。
- **工具与安全**：工作区文件、`rg`、Git 只读操作、计算、时间、Web 抓取、Docker Shell、
  一次性浏览器采集和 Windows UI Automation，并统一经过风险审批。
- **知识与扩展**：长期记忆、PDF/Markdown/TXT/HTML 资料库、渐进加载的 Skills、插件市场、
  command/HTTP/prompt/agent Hooks、MCP 和 LSP。
- **任务编排**：会话 `/loop`、持久 Cron、后台能力包、子代理、Git worktree 隔离、
  团队任务 DAG 和邮箱。
- **交互界面**：Typer/Rich CLI，以及固定监听回环地址、带访问令牌与 CSRF 防护的 FastAPI Web UI。

## 项目结构

| 路径 | 职责 |
| --- | --- |
| `Agent/` | 同步 ReAct 兼容层、异步 Runtime、会话、审批、Hooks、Cron、沙箱与子代理 |
| `model_choice/` | 同步模型客户端、配置解析与异步 Provider 适配器 |
| `tools/` | 同步工具、Harness 工具注册表、内置工具与扩展工具 |
| `memory/` | 长期记忆和独立学习资料库 |
| `skills/` | Skill 发现、Git 安装、插件与市场管理 |
| `prompt/` | 分层 System Prompt 组合器 |
| `run_ui/` | 旧终端兼容层、正式 CLI 和本地 Web UI |
| `.yy/` | 可共享项目配置、Hook/Agent 定义与锁文件 |
| `paper/` | 默认学习资料目录 |
| `tests/` | 无真实 API Key 的核心行为与安全回归测试 |

更细的模块说明见 [Agent](Agent/README.md)、[模型适配](model_choice/README.md)、
[运行界面](run_ui/README.md) 和 [开发参考](DEVELOPMENT_REFERENCE.md)。

## 快速开始

### 1. 准备环境

需要 Python 3.10+ 和 Git。`rg` 用于文本检索；Docker 用于 Shell、命令型 Hook 和浏览器工具。
Docker 不可用时，这些不可信执行能力默认安全失败，而不是自动在主机执行。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
yy-agent doctor
```

开发环境：

```powershell
python -m pip install -e ".[dev]"
```

按需安装可选依赖：

| Extra | 用途 |
| --- | --- |
| `keyring` | 将 API Key 保存到操作系统凭据存储，并安全迁移旧配置中的密钥 |
| `mcp` | 运行 `yy-agent mcp serve` |
| `desktop` | Windows UI Automation、截图及相关桌面依赖 |
| `vector` | 预留 `sqlite-vec` 依赖；当前默认召回仍使用 SQLite FTS5 |

```powershell
python -m pip install -e ".[keyring,mcp,desktop]"
```

### 2. 配置模型

推荐把本机模型选择写入不会提交的 `.yy/settings.local.json`：

```json
{
  "model": "deepseek",
  "fallback_model": "qwen",
  "profile": "general",
  "permission_mode": "risk-based"
}
```

密钥优先使用环境变量：

```powershell
$env:DEEPSEEK_API_KEY = "..."
$env:DASHSCOPE_API_KEY = "..."
```

也可以安装 `keyring` extra 后写入系统凭据存储：

```powershell
yy-agent auth set deepseek
```

已有 `config.ini` 的项目可执行：

```powershell
yy-agent migrate
```

迁移结果写入 `.yy/settings.local.json`；如果旧文件含明文 API Key，迁移前必须安装
`keyring`，密钥不会写入可提交的 JSON 文件。

兼容说明：如果 Harness 没有显式设置 `model`，底层 `LegacyModelProvider` 仍会读取旧
`config.ini` 的默认模型；未提供 Harness provider 覆盖时也会复用其中的连接配置。完成迁移后建议在
`.yy/settings.local.json` 明确指定模型，减少两套配置并存造成的歧义。

### 3. 运行

```powershell
# 交互会话
yy-agent chat

# 单次任务
yy-agent run "总结这个项目，并指出测试缺口" --profile code

# 环境诊断
yy-agent doctor

# 仅本机 Web UI
yy-agent serve --port 8765
```

`yy-agent` 不带子命令时进入交互会话。源码树入口 `python run.py` 等价，并兼容原来的
快捷形式：

```powershell
python run.py "计算 (25 + 17) * 3"
```

## CLI 导航

| 命令组 | 主要用途 |
| --- | --- |
| `chat` / `run` / `serve` / `doctor` | 对话、单次任务、Web UI 和环境诊断 |
| `session` | 列出、查看和安全回滚会话 |
| `memory` / `corpus` | 管理长期记忆和学习资料索引 |
| `skill` / `plugin marketplace` / `plugin` | 安装 Skills，管理市场、插件和可执行组件信任 |
| `prompt inspect` / `hooks` / `sandbox` | 检查 Prompt 来源、Hook 和沙箱状态 |
| `cron` / `scheduler` | 创建持久任务并运行单实例调度器 |
| `agent` / `team` | 运行子代理和团队任务 DAG |
| `mcp` / `lsp` | 检查或调用本地集成 |
| `auth` / `migrate` | 管理系统凭据和迁移旧配置 |

交互会话支持：

- `/memory`、`/plugin`、`/cron`：查看当前状态。
- `/prompt`：检查本次分层 Prompt 的来源。
- `/rewind <seq>`：回滚指定事件序号之后由 Agent 记录的文件改动。
- `/loop 5m <prompt>`、`/loop list`、`/loop cancel <id>`：管理会话级循环任务。
- `/help`、`/exit`：显示帮助或退出。

完整参数以 `yy-agent --help` 和 `yy-agent <group> --help` 为准。

## 配置与状态

配置按后者覆盖前者的顺序合并：

1. 内置默认值。
2. `~/.yy/settings.json`：用户级配置。
3. `.yy/settings.json`：可提交的项目共享配置。
4. `.yy/settings.local.json`：本机项目配置，已加入 Git 忽略。
5. CLI 覆盖项。

常用字段见 [.yy/settings.json](.yy/settings.json) 和
[.yy/settings.schema.json](.yy/settings.schema.json)。`profile` 可为 `general`、`study`
或 `code`，默认权限模式为 `risk-based`。

System Prompt 还会发现用户级 `~/.yy/AGENT.md`，以及从项目根到当前目录的
`CLAUDE.md`、`AGENTS.md`、`AGENT.md` 和 `AGENT.local.md`。兼容文件只作为指令文本读取，
不会因此自动信任其中引用的可执行组件。

运行状态不写入仓库，而是保存在：

```text
~/.yy/projects/<project-id>/state.db
```

可通过环境变量 `YY_AGENT_HOME` 修改用户状态根目录。

## 权限与安全边界

| 模式 | 行为 |
| --- | --- |
| `plan` | 只允许被标记为被动读取的工具；既有持久 allow 也不能扩大该边界 |
| `review-all` | 每个工具调用都请求审批 |
| `risk-based` | 低风险被动读取自动允许，其余调用请求审批 |
| `accept-sandboxed` | 未命中硬拒绝规则的沙箱内调用自动允许，主机调用仍请求审批 |

审批可以只允许本次，也可以保存到当前会话、项目或用户作用域。`desktop`、可写 Shell
等关键调用只能单次批准，不能被持久规则或后台能力包静默放行。

其他边界：

- 文件工具解析真实路径并限制在项目根目录内，默认拒绝 `.env`、SSH 私钥和常见凭据文件。
- 写文件使用原子替换并记录前后内容；`rewind` 会先检查当前哈希，遇到用户并发修改就停止。
- Shell 使用 argv 数组，不解释 Shell 元字符；Docker 默认只读挂载、断网、清理凭据环境变量。
- Web 抓取和远程 MCP 仅允许经公网校验的 HTTP(S) 域名，拒绝 IP 字面量以及解析到内网、
  环回或保留地址的域名；Web 工具还支持项目域名 allowlist。
- 命令型 Hook 在 Docker 中执行；Hook 修改后的参数仍必须经过正常权限检查。
- Skill 文本可以被发现，但插件 scripts、Hooks、MCP、LSP 和 agents 需要显式信任。
- Web UI 始终绑定 `127.0.0.1`，不提供远程监听开关。

## Memory 与学习资料

```powershell
yy-agent memory add "测试必须使用临时目录" --scope project
yy-agent memory search "测试" --scope project
yy-agent memory list
yy-agent memory forget <memory-id>

yy-agent corpus index paper
yy-agent corpus search "间隔重复"
```

Memory 使用 SQLite + FTS5，并在项目的用户状态目录生成可审计 `MEMORY.md` 索引。
资料库与长期记忆使用不同的数据表；PDF 结果保留页码，Markdown/TXT/HTML 保留文件路径。

## Skills 与插件

Skill 安装支持 Git URL、固定 ref 和仓库子目录：

```powershell
yy-agent skill add "https://github.com/example/skills.git#skills/review" --ref v1.0.0 --scope project
yy-agent skill list
yy-agent skill update review --scope project
```

安装记录会保存来源、ref、提交 SHA 和内容哈希。`--scope project` 安装到 `.yy/skills/`，
`--scope user` 安装到 `~/.yy/skills/`。

插件通过市场安装：

```powershell
yy-agent plugin marketplace add owner/marketplace
yy-agent plugin install reviewer@marketplace
yy-agent plugin trust reviewer@marketplace hooks
yy-agent plugin update reviewer@marketplace
```

Marketplace catalog 必须位于市场根目录的 `.yy-plugin/marketplace.json` 或
`.claude-plugin/marketplace.json`；插件包 manifest 则使用 `.yy-plugin/plugin.json` 或
`.claude-plugin/plugin.json`。插件安装与更新都会在 staging 中执行最小 manifest/catalog
结构检查、整树路径隔离检查和内容哈希计算。内容哈希未变化时保留 `enabled` 和已有信任；
内容发生变化时清空可执行组件信任，需要重新执行 `plugin trust`。因此更新来源版本不会在
任何内容变化后静默沿用旧授权。

当前 Runtime 消费受信任的 hooks、agents、MCP 和 LSP 组件；`scripts` 可以记录信任状态，
但尚无通用插件脚本执行器，执行脚本仍必须经过受控 Shell/Hook 路径。

## Cron、子代理与集成

```powershell
# 创建持久任务，并在创建时固定能力边界
yy-agent cron create "*/15 * * * *" "检查测试状态" --tools read_file,git_status
yy-agent scheduler start

# Windows 登录自启
yy-agent scheduler install-autostart

# 子代理与团队
yy-agent agent list
yy-agent agent run reviewer "审查当前改动"
yy-agent team create --name review-team
```

创建 Cron 时会冻结工具、路径、域名、命令前缀，以及当前启用插件集合、内容哈希和受信任
组件。任一插件边界变化都会让后台任务进入 `needs_approval`，不会静默采用新能力。调度器
通过原子状态更新防止重复领取，并把本进程仍在运行的长任务排除在五分钟陈旧租约恢复之外。

子代理定义放在 `.yy/agents/*.md`；兼容读取 `.claude/agents/*.md`。定义可限制模型、轮数、
工具、记忆作用域和 `worktree` 隔离。团队执行受 `max_team_agents` 限制，并按任务依赖并发领取。

MCP 配置使用 `.mcp.json`，LSP 配置使用 `.lsp.json`：

```powershell
yy-agent mcp list
yy-agent mcp probe <server>
yy-agent lsp list
```

经模型触发的 `mcp_call` 会进入 Runtime 高风险审批链；`yy-agent mcp call` 是操作者显式
命令，目前直接调用管理器，不会再次弹出 PermissionBroker 审批。两条路径都会在加载 SDK
和连接前拒绝 localhost、IP 直连及解析到非公网地址的远程端点。

## Python API

推荐入口是 `AgentRuntime.run_turn()`：

```python
import asyncio

from Agent import AgentRuntime
from Agent.types import EventType


async def main() -> None:
    runtime = AgentRuntime()
    async for event in runtime.run_turn("分析当前项目的测试缺口"):
        if event.type in {EventType.TOOL_COMPLETED, EventType.TOOL_FAILED}:
            print(event.payload["name"], event.payload["content"])
        elif event.type == EventType.FINAL:
            print(event.payload["answer"])


asyncio.run(main())
```

如果只需要最终聚合结果：

```python
result = await AgentRuntime().run("总结 README")
print(result.answer)
```

为兼容原项目，`from Agent import Agent`、`AgentResult` 和 `ToolRegistry` 仍保持同步 ReAct
语义；异步 Harness 的结果和工具注册表分别导出为 `Agent.RuntimeResult` 和
`tools.AsyncToolRegistry`。

## 当前边界

- Provider API 已异步化，但当前网络请求完成后才产生模型文本事件，不是传输层逐 token 流式。
- 浏览器工具当前提供单次页面文本或截图；Windows 桌面工具提供窗口枚举、截图、控件点击和文本输入。
- Docker、Playwright 镜像、真实模型、MCP 服务和 Windows UI Automation 属于外部运行条件，
  普通测试不会自动下载或持有其凭据。
- `vector` extra 尚未接入召回流程；缺少向量适配器时始终使用 FTS5。
- 当前 Web 页面以聊天、审批和状态查询为主，不是全部 CLI 管理命令的等价面板。

## 开发与验证

```powershell
python -m unittest discover -s tests -v
python -m pytest
python -m compileall -q Agent model_choice tools memory skills prompt run_ui
```

CI 在 Windows 与 Linux、Python 3.10 与 3.12 上运行。提交改动前请保留用户工作区修改，
不要使用影响整个工作树的 `git reset --hard`。

更多实现细节、配置职责和扩展入口见 [DEVELOPMENT_REFERENCE.md](DEVELOPMENT_REFERENCE.md)。
