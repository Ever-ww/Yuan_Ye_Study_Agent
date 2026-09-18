# Yuan Ye Study Agent

Yuan Ye Study Agent 是一个本地优先、单一异步 Runtime 驱动的学习与研究 Agent。正式入口始终是 `run.py`；常驻 Gateway 是 Runtime、Session、Sandbox、审批和事件流的唯一宿主，CLI、Web 与 Tauri 桌面工作台都是它的客户端。客户端关闭后任务仍可在后台继续，结果会进入 Inbox。

<p align="center">
  <img src="images/harness_evolution.png" alt="Harness 自进化：工坊中的马正在修整自己的挽具" width="760">
</p>

## Harness 自进化

模型像一匹拥有力量和方向感的马，Harness 则是让这种能力能够被稳定驾驭的整套挽具：Runtime 负责节奏，Prompt 提供方向，Tools 延伸行动能力，Hooks 留出新的连接点，Memory 和上下文压缩让它记住一路上真正重要的东西。

所谓 **Harness 自进化**，不是让模型不受控制地改写自己，而是建立一条可审计的成长闭环：传统框架依赖硬编码固定逻辑，面对长对话、Token 波动、复杂子任务、新增交互场景容易失效，只能靠人工改代码、重启服务来适配。本 Agent 在发现代码类缺陷并取得用户确认后，会在隔离环境中生成和校验代码补丁。这是对“身体”和“大脑”的共同进化，而不是只优化“大脑”（Skill）。

CLI chat 能保存完整错误现场，并在用户确认后创建隔离 Git worktree，再启动一个复用正式 `AgentRuntime` 类的 Coding Agent。Coding Runtime 的 `workspace_root`、ToolContext、沙箱工作目录和文件工具全部指向该 worktree；控制器会在运行前校验这一边界。Skill 则始终读取 Yuan Ye 主源码仓库的 `skills/`，不会改从临时 worktree 或 `~/.yy/skills/` 加载。它拥有专属 Memory、项目结构 Profile、上下文压缩和已审核 Skill，工具严格限定为 `read_file`、`search_workspace`、`edit`、`write`、`sandbox_rollback`、`web_fetch`、`skill_read`、`subagent`，配置 Brave Key 后增加 `web_search`，沙箱可用时增加 `bash`。论文、Reference、Cron、计算、时间和 Skill 安装均不会进入 Harness Schema。用户确认启动 Harness 后，隔离 worktree 内的既有 Coding 工具自动获批；变更仍必须通过固定测试，才会提交并本地快进合并。测试失败或无变更时删除 worktree，Harness 不会 stash 脏主工作区，也不会自动推送 GitHub。

Coding Agent 的记忆位于 Agent 根目录的 `.yy/harness-evolution/memory/`，与普通聊天 Memory 和目标 workspace 分离。每次 Harness 更新创建全新的 Session JSONL，不恢复上一项修复的临时对话；同一次更新发生压缩时，继续使用相同 Session 哈希生成 `_002.jsonl`、`_003.jsonl`。跨更新事实以该目录的 `memory.sqlite3` 为权威，并通过 Hook 按 source repository scope 召回；`profile/AGENT.md` 保留稳定规则，其余 Markdown 是可重建的人类可读投影。

`AGENT.md` 首次创建后只由用户维护；`PROJECT.md` 保存当前架构和 Tool/Hook 等开发规范；`CHANGES.md` 与 `LESSONS.md` 只追加已经通过测试并成功合并的事实。每个 Coding Session 都把 `AGENT.md`、`PROJECT.md` 全文和预算内的最新日志条目注入 System Prompt。合并成功后由无工具维护 Runtime 更新长期记忆，模型不可用时使用确定性项目扫描降级；失败、无变更或未合并的尝试只保留在 JSONL 和错误快照中。

> 本文以 Windows PowerShell 为例。项目要求 Python 3.10+；由 uv 管理项目 Python、`.venv` 和依赖，不需要手动使用 `pip` 或激活虚拟环境。

## 结构

```text
Agent/      模型适配、异步 ReAct、Runtime、Hook 协议与配置
backup/     Agent Home 写入屏障、协作冻结、流式加密快照与整体恢复
extension/  十阶段全局多文件 Hook Extension 与开发契约
gateway/    单实例后台进程、Runtime 池、本机 API、事件重放、审批与 Inbox
dream/      每日 Session 归档、长期记忆巩固、审计、调度与回滚
memory/     记忆领域 Python 服务
paper_library/ 全局论文索引、下载、去重、分页解析与总结持久化
context_process/ Token 阈值压缩、Profile 合并与失败裁剪
harness-evolution/ 错误快照、隔离 worktree、诊断与验证流水线
prompt/     单一 System Prompt、任务时间与上下文缓存组合
sandbox/    Trace 级 OS/Docker 沙箱、独立本地 Git checkpoint 与跨进程文件锁
skill/      Skill 获取、格式解析、静态审核、可信索引与安装事务
skills/     System Prompt 与 skill_read 唯一读取的正式 Skill 目录（由 Git 跟踪）
tool/       工具共用框架（不包含模型可直接调用的工具）
  contracts.py         AsyncTool 协议与 ToolContext
  registry.py          Schema 校验、注册与权限审批
  defaults.py          默认工具装配入口
  path_guard.py        工作区路径与敏感目录边界
tools/      仅保存模型可直接调用的受控工具实现
  bash.py              OS/Docker 内受限 Shell
  read_file.py         统一读取源码、文本、PDF 与 Office 文档
  edit.py              已有文件的精确多块替换并创建 checkpoint
  write.py             新建或整文件覆盖并创建 checkpoint
  sandbox_rollback.py  审批后恢复本地 checkpoint
  calculator.py        受限四则运算
  search_workspace.py  工作区文本搜索
  web_search.py        Brave API 网络搜索（仅返回结构化索引摘要）
  web_fetch.py         受 SSRF 边界保护的公开网页正文抓取
  download_paper.py    审批后下载公开论文 PDF 并创建 checkpoint
  profile_read.py      受控读取 Agent Home 长期 Profile
  paper_library.py     全局论文库查重、批量下载、分页读取与总结写入
  current_time.py      本地时间查询
  subagent.py          受父 Agent 限权的临时子 Agent
  skill_read.py        渐进读取已审核 Skill 文本
  skill_install.py     审批后获取、审核和安装 Skill
run_ui/     Textual 会话 TUI、Rich 兼容命令面、Web 路由与静态资源
ui/         Web 与 Tauri 共用的 React/TypeScript 工作台
desktop/    Tauri 2 原生窗口与 Gateway sidecar 启动外壳
packaging/  PyInstaller sidecar 构建与平台文件名准备
tests/      核心行为与 UI 安全测试
.yy/gateway/ SQLite、可重放 Run 事件与 Inbox（进程控制文件位于外部控制面）
.yy-backups/ 加密备份、Restore Journal/Fence、维护锁与 Gateway 进程控制（位于 Agent Home 外）
.yy/dream/  每日巩固状态、结构化记忆、运行审计、事务与 Profile 备份（不提交）
.yy/memory/ Agent Home 中的会话 JSONL、Canonical 长期 Memory、可重建索引与 Profile 投影（不提交）
.yy/papers/ 全局论文 PDF、中文总结与审计索引（不提交）
run.py      唯一源码树入口
```

仓库内的 `memory/` Python 包永远不保存用户数据。首次运行在用户 `~/.yy` 创建 `memory/`：会话消息按 workspace 隔离后写入 `session/` 下的 JSONL；长期事实、Evidence、Mutation 与 Projection Intent 写入 `memory.sqlite3`。`index.sqlite3` 和 `profile/` Markdown 均可由 Canonical Store 重建。启动 Agent 时所在的目录不会生成记忆或模型配置文件。

## 从零开始

### 1. 安装 uv

如果 PowerShell 中执行 `uv --version` 已能显示版本号，可跳过此步。Windows 推荐使用 WinGet：

```powershell
winget install --id=astral-sh.uv -e
uv --version
```

也可使用 uv 的官方安装器；安装方法、升级和其他平台命令以 [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/) 为准。安装完成后请重新打开 PowerShell，确保 `uv` 已进入 `PATH`。

### 2. 取得项目并进入目录

已有本项目文件夹时，直接进入它即可：

```powershell
cd D:\Ever_workspace\Yuan_Ye_Study_Agent
```

首次从 Git 克隆时：

```powershell
git clone https://github.com/Ever-ww/Yuan_Ye_Study_Agent.git
cd Yuan_Ye_Study_Agent
```

### 3. 由 uv 安装 Python 并创建项目环境

以下命令会安装项目可用的 Python 3.11、在项目根创建 `.venv`，并按照 `uv.lock`/`pyproject.toml` 同步依赖：

```powershell
uv python install 3.11
uv venv --python 3.11
uv sync
```

`uv sync` 会把项目以可编辑模式安装到 `.venv`；代码改动不需要重新安装。以后只需在项目根执行 `uv sync` 即可更新依赖环境。 `uv run` 在运行前也会自动检查并同步环境。详见 [uv 的 lock 与 sync 说明](https://docs.astral.sh/uv/concepts/projects/sync/)。

可选：确认解释器和已安装依赖。

```powershell
uv run python --version
uv tree
```

### 4. OS 沙箱默认启用，Docker 为可选后端

`bash` 默认使用 OS 沙箱：Linux/WSL2 使用 Bubblewrap，macOS 使用 Seatbelt，Windows 使用 AppContainer（默认 PowerShell，可配置 Git Bash）。Linux 需安装系统 bubblewrap 包；无法启动安全后端时进入 checkpoint-only，不执行无隔离 Shell。配置与边界见 [OS 沙箱说明](docs/os-sandbox.md)。如需备用 Docker 后端，在 Agent Home 的 `settings.local.json` 设置 `"sandbox_backend": "docker"`，安装并启动 Docker 后确认：

```powershell
docker version
docker info
```

选择 Docker 时，首次 Trace 会构建 `yy-agent-sandbox:local` 镜像，并使用原有无网络、只读容器根及资源限制。默认 OS 后端不依赖 Docker。后端不可用时，文件编辑与 Checkpoint 继续工作，Bash 从工具目录移除；不存在自动切换到无隔离宿主机 Shell 的路径。每个 Trace 固定所选后端，切换配置后重启 Gateway。

### 5. 首次启动并自动初始化 `.yy`

仓库不包含 `.yy/`。首次克隆后直接运行入口即可：

```powershell
uv run python run.py
```

第一次启动会在当前用户名下初始化唯一状态目录：Windows、macOS 和 Linux 均使用
`~/.yy/`（Windows 示例：`C:\Users\你的用户名\.yy`）。workspace、源码仓库和平台
AppData 不再创建新的运行期 `.yy`。

可在启动前通过 `YY_AGENT_HOME` 显式覆盖。初始化会创建 `.yy/settings.local.json`、Agent 身份与项目说明、Memory、Skill、Gateway 和 Sandbox 状态目录。后续启动只补齐缺失项，不会覆盖已有配置或记忆。

升级时会先从旧 `%LOCALAPPDATA%\YuanYeAgent\.yy`（macOS/Linux 对应旧平台目录）迁移，
再补充源码树中的旧 `.yy` 和 `skills`。已有目标文件不会被覆盖，旧目录不会自动删除；
迁移来源记录在 `~/.yy/agent-home-migration.json`。旧 Gateway 仍在持锁时暂不复制活跃
SQLite，继续使用旧位置；执行一次 `gateway stop` 后，下次启动自动完成迁移。旧的未分区
Session 会归入原 workspace 的哈希分区，全局 Profile 仍保持共享。

如果你误删了 `.yy` 中的必要文件，可手动修复初始化：

```powershell
uv run python run.py init
```

初始化和修复都不会覆盖已有配置或记忆。Agent Home 的 `.yy/` 不属于项目 Git。

### 6. 在其他 workspace 中运行

Agent Home 负责模型配置、记忆和本机状态；启动命令时的当前目录才是 Gateway 注册的项目，也是文件工具、沙箱工作目录和 checkpoint 要处理的 workspace。例如：

```powershell
$AgentRoot = "D:\Ever_workspace\Yuan_Ye_Study_Agent"
cd D:\Ever_workspace\My_Project
uv run --project $AgentRoot yy-agent chat
```

上述命令从用户 `~/.yy/settings.local.json` 读取模型配置，并把该 workspace 的会话写入 `~/.yy/memory/session/<workspace-hash>/`；`My_Project` 不会生成 `.yy`。其他 workspace 看不到也不能恢复这些 Session，但 USER、RESEARCH、OTHERS 和普通扩展 Profile 仍全局共享。`read_file`、`edit`、`write`、搜索、沙箱 Shell 和回溯只允许操作 `My_Project`，checkpoint 捕获的也是该目录。checkpoint 对象库按 workspace 隔离保存在 `~/.yy`，不会写进用户项目的 `.git`。

### 7. 先进行离线启动验证

仓库默认使用无需 API Key 的 `echo` Provider。它只回显输入，用于验证 CLI、UI 和 Runtime 是否正常；这不是实际的模型回答。

```powershell
uv run python run.py run "验证新版入口"
uv run python run.py chat
```

交互终端默认启动会话级 Textual TUI：左侧是可用鼠标滚轮、`PgUp`、`PgDn`
浏览的完整对话时间线，右侧是当前 Turn Observer，下方输入框在整个 Session
期间保持不变。模型 Token 会被合并到稳定帧率后刷新，避免每个 Token 重新解析
Markdown 而拖慢 Gateway 事件消费。时间线默认粘在最新内容；用户向上滚动后会暂停自动跟随，
只有回到真正底部才恢复；滚轮向上会在滚动偏移生效前立即暂停跟随，流式刷新不会把视图抢回底部。
当前 ReAct Loop 使用一个单色转圈折叠状态卡：模型生成阶段显示
“思考中...”，Tool 阶段显示该 Loop 正在执行的全部 Tool 名称；展开后可查看各 Tool 的参数、
结果和持久记录引用。Gateway 给出 Canonical Final 后，当前 Turn 的中间文字、草稿和 Tool
状态卡会被最终答案整体替换。在输入框中键入 `/` 会在上方显示可点击的命令面板，
并支持方向键选择和 `Tab` 补全。当终端不支持全屏 TUI，或需要管道/日志兼容输出时，使用：

```powershell
uv run python run.py chat --classic
```

在交互模式中输入 `/help` 查看帮助，输入 `/exit` 或 `/quit` 退出。

## 配置真实模型

### 1. 创建本机配置文件

Agent Home 中的 `.yy/settings.local.json` 是首次启动自动生成的本机模型配置文件，支持直接保存 `base_url` 与 `api_key`。即使从其他 workspace 启动，也始终读取这一份配置。如果文件被误删，可执行：

```powershell
uv run python run.py init
```

然后编辑 Agent Home 下的 `.yy/settings.local.json`。Windows PowerShell 可先定位文件：

```powershell
$AgentHome = if ($env:YY_AGENT_HOME) { $env:YY_AGENT_HOME } else { $HOME }
notepad (Join-Path $AgentHome ".yy\settings.local.json")
```

将 `api_key` 改为有效 Key。下面以 DeepSeek 为例：

```powershell
@'
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com",
  "api_key": "你的 API Key",
  "web_search_api_key": "你的 Brave Search API Key（不启用搜索时留空）",
  "web_search_timeout_seconds": 20,
  "web_fetch_timeout_seconds": 20,
  "web_fetch_max_bytes": 2000000,
  "web_fetch_max_chars": 30000,
  "paper_download_timeout_seconds": 60,
  "paper_download_max_bytes": 50000000,
  "use_system_proxy": false,
  "proxy_url": null,
  "coding_source_root": null,
  "stream": false,
  "max_steps": 8,
  "compression_threshold_tokens": 200000,
  "model_context_window_tokens": 262144,
  "compression_output_reserve_tokens": 16384,
  "compression_safety_margin_tokens": 8192,
  "compression_protect_last_n": 20,
  "compression_target_ratio": 0.2,
  "compression_hygiene_message_limit": 5000,
  "compression_micro_compact": false,
  "compression_provider": null,
  "compression_model": null,
  "compression_base_url": null,
  "compression_api_key": null,
  "compression_context_window_tokens": null,
  "tool_output_cjk_threshold_chars": 1000,
  "tool_output_english_threshold_words": 1000,
  "tool_output_max_chars": 10000,
  "tool_output_head_ratio": 0.2,
  "tool_output_tail_ratio": 0.2,
  "tool_output_preview_head_chars": 427,
  "tool_output_preview_tail_chars": 427,
  "tool_output_preview_head_words": 427,
  "tool_output_preview_tail_words": 427,
  "sandbox_checkpoint_limit": 17,
  "sandbox_checkpoint_merged_branch_retention_days": 30,
  "gateway_port": 8765,
  "gateway_max_concurrent_runs": 4,
  "gateway_runtime_idle_seconds": 3600,
  "approval_timeout_seconds": 30,
  "cron_heartbeat_seconds": 60,
  "dream_enabled": true,
  "dream_schedule": "0 3 * * *",
  "dream_timezone": "local",
  "dream_model": null,
  "dream_batch_tokens": 12000,
  "dream_model_timeout_seconds": 90.0,
  "dream_run_timeout_seconds": 900.0
}
'@ | Set-Content -Encoding utf8 (Join-Path $AgentHome ".yy\settings.local.json")
```

`dream_model_timeout_seconds` 限制每次 Dream 模型尝试（包括本地或自定义 runner）；
`dream_run_timeout_seconds` 限制整轮 Dream。超时会持久化为普通 FAILED 结果，源 Evidence
不会被消费，后续可安全重试，同时不会无限占用 Gateway 的维护租约。

`compression_protect_last_n` 与 `compression_target_ratio` 仅为旧配置兼容保留；自动压缩现在按完整 Turn 边界保护上下文，不再按原始消息条数截断。

可用 Provider：`openai`、`anthropic`、`deepseek`、`qwen`、`glm`、`kimi`。对应环境变量为：

| Provider | 环境变量 | 示例模型值 |
| --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `gpt-4.1-mini` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-5` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `qwen` | `DASHSCOPE_API_KEY` | 供应商支持的模型名 |
| `glm` | `ZHIPU_API_KEY` | 供应商支持的模型名 |
| `kimi` | `MOONSHOT_API_KEY` | 供应商支持的模型名 |

`base_url` 允许接入兼容 OpenAI 或 Anthropic 协议的企业网关；未填写时使用 Provider 内置官方地址。`api_key` 未填写时，程序才尝试读取下表所列环境变量。

可在同一份 `settings.local.json` 中配置多个可切换模型。顶部模型名可点击展开选择卡片，空闲时按 `Shift+Tab` 可轮换；切换只影响下一 Turn，不改变正在运行的请求。`api_key` 可按模型单独配置；同 Provider 未填写时继承主配置的 Key，不同 Provider 未填写时读取其环境变量：

```json
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "api_key": "主 DeepSeek Key",
  "model_profiles": [
    {
      "profile_id": "ds-flash",
      "provider": "deepseek",
      "model": "deepseek-flash",
      "base_url": "https://api.deepseek.com",
      "stream": true
    },
    {
      "profile_id": "openai-pro",
      "provider": "openai",
      "model": "gpt-5",
      "api_key": "对应 Provider Key",
      "stream": true,
      "context_window_tokens": 262144,
      "reasoning_effort": "high"
    }
  ]
}
```

`profile_id` 必须唯一；`default` 保留给顶部的主 `provider/model` 配置。Gateway 的模型列表接口只返回非敏感的模型与 reasoning 元数据，不会向 TUI 暴露 Key。顶部模型选择器右侧可选择 `none / low / medium / high / xhigh / max`。YYAgent 不维护模型档位映射：OpenAI-compatible 请求原样发送 `reasoning_effort`，Anthropic 请求转换为 `thinking` 与 `output_config.effort`，实际支持、映射或拒绝行为由对应 API 服务决定。

模型网络请求默认使用 `use_system_proxy=false`，即忽略 Python/HTTPX 可见的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等环境代理，直接连接模型服务。这可避免 Windows 系统代理或代理软件意外接管并中断 DeepSeek 等服务的 TLS 连接。修改代理配置后需要重启 Gateway，使新建的 Provider 生效。

如需显式复用系统环境代理，设置：

```json
{
  "use_system_proxy": true,
  "proxy_url": null
}
```

如需只给 Yuan Ye Agent 指定一个代理，而不读取系统代理环境变量，设置：

```json
{
  "use_system_proxy": false,
  "proxy_url": "http://127.0.0.1:7890"
}
```

`use_system_proxy=true` 与非空 `proxy_url` 互斥，程序会拒绝含糊配置。`proxy_url` 当前支持 `http://` 和 `https://` 代理地址；代理地址不会进入 Session 模型审计，审计只记录 `disabled`、`system` 或 `explicit` 模式。

### 1.1 可选：启用网络搜索

网络搜索使用 [Brave Search API](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)。先在 Brave Search API 控制台创建 Key，再把它写入 Agent Home 的 `.yy/settings.local.json`：

```json
{
  "web_search_api_key": "你的 Brave Search API Key",
  "web_search_timeout_seconds": 20
}
```

重启 Gateway 后，主 Agent、Harness Coding Agent 以及被明确授权该工具的 Subagent 会获得只读 `web_search` 工具。它支持查询词、1～10 条结果、国家、语言、Safe Search 和最近一天/一周/一月/一年筛选，返回稳定 JSON，其中包含标题、URL、摘要和可选发布时间。搜索工具本身只访问搜索 API，不会打开结果页面；结果会标记为不可信外部内容，并清理 HTML 与控制字符。

未配置或留空 `web_search_api_key` 时，`web_search` 不会出现在模型的工具 Schema 中，因此不会产生无 Key 的失败调用。搜索请求和模型请求共用 `use_system_proxy` / `proxy_url` 规则：默认忽略系统代理，只有显式配置后才使用代理。Key 只能保存在 `.yy/settings.local.json`，不能写入 `.yy/settings.json`，也不会进入错误快照。

独立的 `web_fetch` 不依赖 Brave Key，始终可用于抓取用户提供或搜索结果中的公开网页。它只接受标准端口的 HTTP(S)，逐次校验 DNS 和重定向目标，拒绝 localhost、局域网、链路本地/云元数据、保留地址、URL 凭据及二进制响应；不会携带 Cookie、执行 JavaScript 或下载附件。HTML 会转换为纯文本，默认最多读取 2,000,000 字节、返回 30,000 字符，响应中会标明是否被截断。可在本机配置中调整：

```json
{
  "web_fetch_timeout_seconds": 20,
  "web_fetch_max_bytes": 2000000,
  "web_fetch_max_chars": 30000
}
```

论文 PDF 使用独立的高风险 `download_paper` 工具。它接收公开 HTTP(S) PDF URL 和
workspace 内的 `.pdf` 保存路径，经用户审批后下载；DNS、重定向、实际连接地址和代理规则
沿用 `web_fetch` 的公开网络边界。响应必须是 PDF MIME（部分站点可使用
`application/octet-stream`）且内容具有 PDF 文件头，默认最大 50 MB。文件通过临时文件原子
替换，成功后创建 checkpoint，并返回 `path` 和 `next_tool=read_file`。ArXiv 的
`/abs/<id>` 摘要地址会自动转换为官方 `/pdf/<id>` 下载地址。可调整：

```json
{
  "paper_download_timeout_seconds": 60,
  "paper_download_max_bytes": 50000000
}
```

典型论文链路为 `web_search → web_fetch（阅读摘要/落地页）→ download_paper → read_file`。
Google Scholar 仍只是候选链接来源；工具不会绕过验证码、登录、订阅或出版商访问控制。

典型链路是 `web_search → web_fetch → 最终回答`。搜索结果 JSON 会明确返回 `next_tool=web_fetch` 和下一步说明；ReAct 循环把完整搜索结果作为 `role=tool` 保留给下一次模型调用，使模型可以从 `results` 选择 URL、继续抓取正文，再依据正文完成回答。抓取内容同样属于不可信外部输入，模型不得把网页中的提示文字视为系统指令。

`stream` 控制模型文本是否使用 SSE 实时输出，默认 `false`。设为 `true` 后，OpenAI-compatible Provider（包括 DeepSeek）会逐段显示生成文本；设为 `false` 时等待完整响应后再显示最终答案。Anthropic 当前仍采用完整响应模式。

`max_steps` 表示一次用户任务最多允许发起多少次模型 API 调用。

`coding_source_root` 指定 `/code` 要维护的 Yuan Ye Agent 源码 Git 仓库。默认不需要填写：
程序依次使用显式配置、`.yy/agent-home-migration.json` 中记录的 `source_root`，
最后才使用当前 Python 源码根目录。它与普通聊天所操作的 `workspace_root` 是两个独立边界。

`compression_threshold_tokens` 默认是 `200000`（200k tokens），现在表示“预计输入 + 输出预留”的总请求触发线。每次 `model_before` 都会统计稳定 System Prompt、历史消息、当前 Query、临时 Runtime Context、排序后的 Tool Schema，以及默认 `16384` tokens 输出预留；达到触发线时先压缩，再重载消息并执行最终硬限制复核。默认模型窗口为 `262144`，另保留 `8192` tokens 安全余量。

自动压缩按 Turn 边界保护近期原文：若在新 Turn 开始时触发，保留上一轮完整 Turn，并把新 Summary 放入当前 user prompt；若在 Turn 内触发，保留上一轮完整 Turn和当前 Turn 已产生的完整 user/assistant/tool trace，只压缩更早历史，并把 Summary 作为动态上下文挂到上一轮 user prompt。受保护消息通过 `record_id + segment + SHA-256` 引用原始 Session 记录，不在新 JSONL 分段复制。消息数达到 `5000` 时也会强制预检。Provider 返回真实 input usage 后，Runtime 会以保守系数校准后续估算。明确的 context-length 拒绝最多触发一次应急压缩和一次新 Provider Attempt；仍失败则返回结构化错误，不无限重试。

可通过 `compression_provider`、`compression_model`、`compression_base_url`、`compression_api_key` 和 `compression_context_window_tokens` 配置独立压缩模型；窗口不足或服务不可用时回退主模型。`compression_api_key` 只能位于本机 `settings.local.json`。`compression_micro_compact` 默认关闭，避免每 Turn 增加额外模型调用。将 `compression_threshold_tokens` 设为 `0` 可关闭自动压缩，但仍可手动使用 `/compress`；模型硬窗口检查仍然生效。

历史 Tool 输出裁剪由独立的 `MODEL_BEFORE` Hook 执行。历史结果超过 `tool_output_cjk_threshold_chars=1000` 个中日韩统一表意字符、超过 `tool_output_english_threshold_words=1000` 个英文单词，或超过防御性硬上限 `tool_output_max_chars=10000` 字符时，只缩短发给模型的消息副本。中文触发时头尾各保留 `427` 字符；英文触发时各保留 `427` 个完整单词，分别通过 `tool_output_preview_head_chars/tail_chars` 和 `tool_output_preview_head_words/tail_words` 配置。混合文本同时满足两个条件时采用中文字符规则，只有硬字符上限触发时也按字符处理；预览比原文更长或头尾重叠时保留原文。中间给出可直接调用的 `session_read` 参数。当前 Turn 与上一完整 Turn 的工具结果始终保留；Session JSONL 和 Memory 原文不变。三个阈值都设为 `0` 可关闭裁剪。

自动压缩通过当前 user 的持久化 `record_id` 判断是否处于 Turn 内，连续两次输入相同文字不会混淆边界。压缩失败后的整块兜底裁剪与 Tool 预览共用 Turn 保护边界；若保护内容本身超过模型硬窗口，会明确返回 `ContextBudgetExceeded`，不会继续删除当前任务或 Tool trace。

`sandbox_checkpoint_limit` 默认是 `17`，必须是大于等于 1 的整数。它只限制每个 Session 中可供用户精确回退的恢复点数量，基线也计入上限；超过后淘汰最老恢复点引用，但归档分支仍需要的提交继续由 branch ref 保护，不改写项目主仓库。

Gateway 默认监听 `127.0.0.1:8765`，不同 Session 最多同时运行 4 个任务，同一 Session 同时只允许一个任务。`gateway_runtime_idle_seconds=3600` 表示 Session Runtime 默认空闲 60 分钟后关闭 Trace 与沙箱后端；Session JSONL 不会删除，下一次请求仍可冷恢复。`chat --continue` 会选择当前 workspace 最近的 Session：Runtime 尚在时热复用，已经回收时从 Session 记录恢复。

Durable 重试参数分为 `model_retry_*`、`tool_retry_*` 和 `outbox_retry_*`。策略会在
Logical Operation 创建时固化，后续修改配置只影响新 Operation。Outbox 默认最多尝试
12 次，退避上限 900 秒；`outbox_dead_letter_enabled=true` 时，超限的单个 Sink 进入人工处理状态。

只允许将模型 Key 写在 `.yy/settings.local.json` 或对应环境变量中；Brave Search Key 只从本机配置读取。程序会拒绝 `.yy/settings.json` 中的 `api_key` 或 `web_search_api_key` 字段；整个 `.yy/` 均为本机目录且不会提交。

### 2. 可选：使用环境变量保存密钥

如不希望将 Key 写入本机 JSON，可删去 `api_key` 字段并在当前 PowerShell 会话设置密钥。以 DeepSeek 为例：

```powershell
$env:DEEPSEEK_API_KEY = "你的 API Key"
uv run python run.py chat
```

该环境变量只在当前 PowerShell 窗口有效。关闭窗口后需要重新设置；如需持久化，请使用你的系统凭据管理方案，并重新打开终端后再运行项目。配置文件中的 `api_key` 优先于环境变量，因此不要同时保存两个不同的 Key。

如果没有设置有效 Key，远程 Provider 会明确报出配置错误，不会静默退回网络请求或泄露密钥。

## 日常操作

### 0. Gateway 管理

`run`、`chat`、`serve-ui` 和桌面端会先探测 Gateway；未运行时自动启动单实例后台进程。客户端退出不会终止后台任务，也不会关闭 Gateway。

```powershell
uv run python run.py gateway start
uv run python run.py gateway status
uv run python run.py gateway logs --lines 100
uv run python run.py gateway stop
```

### Agent Home Backup / Restore

Backup 对统一的 `~/.yy` 创建一致性快照。Gateway 只在复制不可变 staging 时短暂进入 `QUIESCING → QUIESCED`；staging 原子发布后立即恢复服务。压缩、加密、内容去重、验证和 retention 在后台继续，不占用维护窗口。Runtime、Cron、Dream、Outbox、Embedding 与 Harness 仍须先到达可持久化边界；Outbox 不要求清空 backlog。

```powershell
uv run python run.py backup create
uv run python run.py backup list
uv run python run.py backup status
uv run python run.py backup verify D:\backups\backup-2026-09-17_040000_000000_ab12cd34ef56.manifest
uv run python run.py backup restore D:\backups\backup-2026-09-17_040000_000000_ab12cd34ef56.manifest
uv run python run.py backup recover
uv run python run.py backup rollback
```

`backup_key_mode` 默认是 `os_managed`：首次备份自动生成高熵随机密钥，Windows 使用当前用户 DPAPI、macOS 使用 Keychain、Linux 使用 Secret Service 托管。用户不需要设置或重复输入口令，`backup verify/restore` 会根据 Manifest 自动取回对应密钥。Windows 只在 `~/.yy-backups/control/credentials/` 保存 DPAPI 密文，明文密钥不写入代码、配置、Agent Home、日志或子进程环境。系统托管备份默认只适合在同一系统账户和 Agent Home 恢复；跨设备迁移应使用独立备份目录和手动口令。

兼容模式 `backup_key_mode=passphrase` 继续支持 `YY_BACKUP_PASSPHRASE`；Gateway 启动时读取一次并从全局环境移除。无论采用哪种模式都不会降级生成明文备份。自动备份默认每天本地时间 04:00 运行；自动成功只保留已读审计，连续失败只保留最新一条未读通知，普通聊天启动时仅提示待处理数量，详情由 `/inbox` 显式查看。

备份根目录由 `control/backup/backup_operations.sqlite3`、`objects/<前两位>/<sha256>` 和 `manifests/backup-<日期时间>-<id>.manifest` 组成。对象按明文 SHA-256 寻址，只写一次，并分别使用 zlib 与 AES-256-GCM 流式压缩、加密和认证；相邻快照中未变化的内容不会重复保存。Manifest 自身有 canonical hash，发布使用 `write → flush → fsync → atomic replace`。SQLite 使用 Backup API 生成干净快照，不归档 WAL/SHM。每个 Manifest 最多保留 27 天；删除过期 Manifest 后才回收没有任何剩余 Manifest 引用的对象。旧 `.yybackup` 读取器只用于兼容历史归档，不再生成新归档。

Operation FSM 保存在 Agent Home 外部：`PREPARING → SNAPSHOTTING → SNAPSHOT_READY → STORING_OBJECTS → VERIFYING → COMPLETED`。进程在 `SNAPSHOT_READY` 之后退出时，下一次 Gateway 启动会先恢复服务，再严格复用原 staging 和成员集合续跑；不会重新扫描当前 `.yy` 猜测原快照。

Restore 是整体替换而不是状态合并。破坏性替换前会显示 Backup ID、版本、大小、外部依赖和峰值空间，并要求输入短 ID；随后创建并验证救援备份。控制面位于 `~/.yy-backups/`，不随 `.yy` 一起替换：Fence 阻止普通 Gateway 启动，append-only 哈希链 Journal 对关键 rename 采用 `intent → filesystem action → committed`。如果强杀发生在 rename 与 commit 之间，`backup recover` 只依据记录的身份和指纹协调；无法证明时进入 `RECOVERY_REQUIRED`，不会猜测。

Harness worktree 本体和旧 `.git` 指针不会进入跨机器归档。维护冻结时只导出 repository identity、required commits、tracked/staged diff、untracked bundle、Session metadata 与验证证据；目标机器缺少仓库或 Commit 时会标记为 offline/incompatible，而不会按同名目录误关联。

Gateway 停止时先拒绝新任务，并给正在运行的任务一个短暂收尾窗口，随后通过
`CANCELLING → FINALIZING → CANCELLED` 关闭 Runtime 与沙箱后端。异常重启不会把全部未完成
任务粗暴改成 `interrupted`：可证明安全的任务从持久化 State 与 SafeCheckpoint 恢复，等待审批
的任务继续等待到 `expires_at`，外部副作用不确定时进入非终态 `RECOVERY_REQUIRED`。
只有数据库损坏、Checkpoint 丢失或版本不兼容等无法建立可信恢复路径的情况才进入永久终态
`INTERRUPTED`。

Windows 下 CLI 首次发现 Gateway 未运行时，会使用 `CREATE_NO_WINDOW` 在后台启动服务，不会另外打开空白 Windows Terminal；后台 stdout/stderr 统一写入 Agent Home 外部控制面的 `gateway.log`。

CLI、Web 和桌面端同时启动时会先竞争独立的 `startup.lock`，只有一个客户端创建后台
进程，其余客户端等待同一健康接口。正式实例通过 `instance.lock` 与保留的 PID 标识；
一次健康请求超时不会再删除 PID。`gateway stop` 使用绑定实例身份的结构化请求与 ACK，
排空超时会明确失败并保留现场，不再回退到强杀 PID。Windows 后台进程强制使用 UTF-8 日志，并过滤
h11 在连接已经关闭后重复发送 400 所产生的特定无害回调栈。

Gateway 的持久业务状态位于 Agent Home 的 `.yy/gateway/`；Token、PID、锁、停止请求和日志位于
`<agent_root>/.yy-backups/control/gateway/`，因此 Restore 替换 `.yy` 时不会丢失进程级隔离状态。
其中 `lifecycle.sqlite3` 是唯一维护状态权威：`RUNNING → QUIESCING → QUIESCED → RESUMING → RUNNING`；
离线 Restore 经过 `RESTORING`，失败保持 `FAILED`。`gateway quiesce --timeout 30` 排空已有任务并阻止新业务；
`gateway status` 查看 epoch/revision，`gateway resume --epoch <epoch> --revision <revision>` 检查健康后恢复。
维护中重启不会自动开放服务，完整 Restore 后也需要显式 resume。详见[维护状态机与恢复边界](docs/gateway-maintenance.md)。
SQLite 中的 `agent_states` 是当前 Runtime
State 的唯一权威来源；`state_transitions` 只保存真实 FSM 迁移，`operation_ledger` 保存模型、
工具、审批与收尾操作，`gateway_events + event_outbox` 负责可靠事件投递。EventBus 与
`runs/<run-id>.jsonl` 两个 Sink 分别确认，全部成功后 Outbox 才标记 delivered；JSONL 使用稳定
`event_id` 与文件级索引防重复。外部控制面的 `gateway.log` 最多保留当前文件与 5 份轮转日志。Session 对话
正文仍只由 Memory 写一份，不会复制到 Gateway 事件文件。

### Durable Runtime FSM

每个 Run 使用不可变 `AgentState`。外层 FSM 管理
`CREATED/QUEUED/STARTING/RUNNING/RECOVERING/RECOVERY_REQUIRED/CANCELLING/FINALIZING`
及四个终态；内层 FSM 管理 `THINKING/WAITING_HUMAN/ACTING/OBSERVING/FINISHED`。
`FINISHED` 只表示 Agent Loop 已结束，结果由 `SUCCESS/ERROR/CANCELLED/EXHAUSTED` 单独表达；
所有正常终止都必须经过可重入的 `FINALIZING`。

所有状态修改统一进入 `StateController.apply(command)`，同时执行 command 幂等、revision CAS、
Gateway epoch fencing、FSM guard、SQLite 事务、兼容 Run 投影、Gateway Event 和 Outbox。
Scheduler 统一调用 `is_runnable(state, operation, attempt)`；`WAITING_HUMAN`、`RECOVERY_REQUIRED` 和未知
副作用不会继续消耗模型 Token。

每个逻辑模型或工具动作由 `Logical Operation` 表示，真实请求则由不可改写的
`OperationAttempt` 记录。Operation 与 Attempt 1 在同一 SQLite 事务中创建；重试只能新建
Attempt，`stable_key/request_hash/retry_policy_snapshot` 在同一 Operation 中永久不变。Operation
的状态、结果和 `next_retry_at` 只由纯函数 `reduce_operation()` 生成。

外部工具固定执行 `Begin Attempt → running → completed/failed` 两阶段持久化边界。若进程在真实
副作用完成后、Attempt 结果提交前退出，Attempt 保持 `running/unknown`，系统先调用工具可选的
`reconcile()`；无法确认时绝不自动猜测或重放非幂等操作。Session JSONL 每条记录包含稳定
`record_id`，Gateway Run 下还会记录 `run_id/turn_id/operation_id`；恢复补写使用
`append_once()`。SafeCheckpoint 只会在 Ledger 结果与必需 Session 记录都已确定后建立。
所有正常终态只能由 `FinalizeTerminalCommand` 从 `FINALIZING` 提交；State、Run 投影、
Transition、terminal Event 和 Multi-Sink Outbox 共享一个最终事务。Outbox 按 Sink 独立退避，
达到上限后进入 dead-letter，不会无限高频重试。

`FINALIZING` 不再用字符串把 `memory/session_index/audit/inbox` 直接登记为完成。每次收尾使用
独立的 v2 generation，并为四个步骤建立真实 Logical Operation 与 OperationAttempt：Memory 和
Session Index 严格验证磁盘上的原始 Session JSONL 与分段索引，Audit 写入不可变 receipt，Inbox
核对确定性 SQLite 行。Evidence 先经严格 Pydantic 校验、脱敏和规范化 JSON 编码；Artifact 内容
Hash 与 Attempt Evidence Hash 各自只有一种含义。只有当前 generation 的四份 Evidence 相互引用且
仍然有效时，最终事务才允许进入终态。

同一 Session 的运行、压缩、Skill refresh、恢复和 Finalize 共用轻量
`SessionReservationRegistry`，不同 Session 仍可并发。文件型 Finalize Attempt 必须经历
`PREPARED → RUNNING → COMPLETED/FAILED/UNKNOWN`；Reducer 计算重试资格和 `next_retry_at`，轻量
Retry Driver 只负责在到期后通过标准命令创建下一 Attempt。Gateway 重启会从持久化时间继续等待。
若已完成 Evidence 后来与 Artifact 不一致，旧 Attempt 保持不可变，当前 generation 被持久化失效，
Run 进入 `RECOVERY_REQUIRED`；恢复时只能建立新的 generation，不能把旧 bookkeeping 包装成证据。

### 1. 创建新会话

```powershell
uv run python run.py chat
```

第一次发送消息后，CLI 会打印本次会话哈希，例如：

```text
会话哈希：60c2d464f820db43；下次可使用 chat --session 60c2d464f820db43 恢复
```

请保留这个哈希；它也是 JSONL 文件名中的会话标识。交互过程中：

- 直接输入任务并按 Enter 发送。
- `/help` 查看帮助；`/exit` 或 `/quit` 退出。
- `/inbox` 列出未读后台结果；`/inbox all` 列出全部结果；表格 ID 支持唯一前缀。
- `/inbox show <ID>` 查看完整结果；`/inbox read <ID>` 标记单条已读；`/inbox read-all` 标记全部已读。
- `/cron list|status|add|at|preview|edit|pause|resume|run|remove` 在聊天内管理后台定时任务，命令本身不会写入普通 Session。
- `/dream status|run|backfill|rollback` 查看、手动运行、历史回填或顺序回滚每日记忆巩固；命令本身不会写入普通 Session。

### 1.1 使用 `/code` 开发 Hook Extension

在普通 `chat` 中输入 `/code`，CLI 会通过 Gateway 为 Yuan Ye 源码仓库创建一个持续
Coding Session，提示符切换为 `Code >`。源码仓库必须处于正常分支且完全干净；
系统不会 stash 或覆盖现有未提交内容。

```text
你 > /code
Code > 在 tool_after 增加工具耗时统计
Code > 再为失败结果增加分类字段
Code > /exit
你 >
```

同一次 Coding 模式始终复用同一个隔离 Git worktree、Coding Runtime、短期 Memory、
上下文压缩、Skill、Subagent、OS/Docker 沙箱和 checkpoint。worktree 位于 Agent Home：

```text
.yy/harness-evolution/worktrees/<source-hash>/<code-session-id>/
```

每条需求都必须创建或修改 `extension/hook/**` 中的描述性扩展文件，并生成一个由控制器
指定的唯一 `tests/extensions/test_<name>_<hash>.py`。Coding Agent 会先主动测试，
控制器随后独立检查修改路径、Extension 契约并执行专项测试、全部 Extension 测试、
完整 pytest/unittest、compileall、`uv lock --check` 与 `git diff --check`。失败结果
会回送同一个 Coding Runtime，最多进行三轮自动修复；通过后才在临时分支创建一条提交。

`/exit` 不重新承担首次测试，只会合并已经验证的提交。只有 worktree 无未验证修改、
源码主工作区仍干净且 HEAD 未变化时，才执行 `git merge --ff-only` 并返回普通聊天。
未验证修改会拒绝退出合并；主 HEAD 变化时保留 worktree 和分支。输入 `/abort` 可在
再次确认后放弃整个隔离会话。合并后的扩展需要重启 Gateway 才会加载。
- 已经开始会话后输入 `/compress`，可立即压缩当前上下文；命令本身不会写入 JSONL。
- `/context refresh` 重新读取 Agent、Profile 与当前 Session 文件并刷新内存上下文；命令不会写入 JSONL。
- `/skill list`、`/skill install`、`/skill update`、`/skill audit` 和 `/skill refresh` 管理本机 Skill；安装或更新不会自动修改当前 Prompt，只有 `/skill refresh` 成功后才重新扫描并加载 Skill XML。
- `stream=true` 时，OpenAI-compatible Provider 会通过 SSE 逐段显示文本。
- 高风险工具会显示方向键审批菜单：使用 ↑/↓ 选择“允许本次 / 当前会话始终允许该工具 / 拒绝”，按 Enter 确认、Esc 取消，默认选中“允许本次”。30 秒内没有确认输入时仍会自动拒绝，避免危险操作悬挂；审批由发起任务的客户端优先处理，该客户端断开时也会立即拒绝。
- `bash` 只在当前 Trace 的 OS/Docker 无网络沙箱内执行，实际 shell 随平台和配置确定，并通过临时上下文提供给模型。后端不可用时移除 Schema，伪造调用也会在审批前拒绝。文件修改进入 workspace；一次成功调用最多创建一个 checkpoint，无变化不创建。
- `edit` 参考 PI Agent 的精确编辑语义：一次可提交多个 `{oldText,newText}`，每个 `oldText` 必须在原文件中唯一存在，所有定位都基于修改前的原文且不能重叠；工具保留 UTF-8 BOM 与原换行风格。它适合小范围修改已有文件。
- `write` 用于创建新文件或明确替换整个文件。`edit` 与 `write` 都在宿主机执行原子替换，每次实际变化后创建一个 checkpoint；内容未变化不会制造空快照。可让 Agent 调用高风险 `sandbox_rollback` 按步数恢复，执行前仍需审批。
- `read_file` 是唯一文件读取工具：源码和普通文本保持原始文本返回；PDF 论文、DOCX、PPTX、XLSX/XLSM、Jupyter Notebook 与 HTML 根据扩展名自动进入结构化解析。PDF 按页、PPTX 按幻灯片、XLSX 按工作表选择范围；长内容通过 `offset_chars` 和 `max_chars` 继续读取。它只提取文本，不执行宏、脚本、外部链接或嵌入对象。
- `enable_sandbox=False` 只用于没有写工具的压缩或诊断 Runtime。自定义 Runtime 未注入 checkpoint 上下文时，`edit`、`write`、`bash` 和 `sandbox_rollback` 都会明确拒绝，不存在无快照写入旁路。
- 文件工具使用 Agent 根目录 `.yy/sandbox/locks/` 下按 workspace 隔离的跨进程读写锁。同一文件写入时，其他读取和写入会等待到原子替换、checkpoint 或失败恢复全部结束；不同文件的普通读取不会被无关写入阻塞。
- CLI chat 的单次模型调用遇到临时网络错误时会保留同一份上下文、等待 2 秒后重试，总计最多 3 次；重连成功后继续当前任务，并显示“模型网络连接已恢复”。流式传输中断产生的不完整片段会被丢弃，避免与重试结果拼接；调用成功或进入工具结果后的下一次模型调用时重新计数。
- 模型或工具正在运行时按 `Ctrl+C` 只终止当前回答，Runtime 和 Session 保持打开，可以立即继续提问。流式模型已经生成并展示的文本会以 `status=cancelled` 的 assistant 消息完整写入 JSONL，并进入下一次模型调用的历史上下文；尚未完成的流式 `tool_call` 增量不会保存。若工具已经正式进入执行阶段，系统会补齐对应的取消结果，保证工具链合法。等待网络重试的 2 秒同样可以取消。CLI 空闲等待输入时按 `Ctrl+C` 则退出整个 chat。
- 只有内部代码缺陷或无法规范化的模型响应格式会在 `tests/error/<SHA-256>.jsonl` 保存完整本机复现快照，且不建立索引。网络、模型服务、配置、认证、权限和普通工具错误只在 CLI 显示，不生成快照。模型消息正文只保存一次，Session 时间戳和指标以 `session_audit` 补充；保存代码类缺陷后会询问是否启动 Harness，默认拒绝。

### 2. 查看已有会话

列出当前 workspace 中可恢复的会话：

```powershell
uv run python run.py session list
```

列表只读取当前 workspace 的 Session 分区，并显示会话哈希、创建时间、最新分段消息数和 JSONL 文件名。查看当前 workspace 中某个会话的带时间戳记录：

```powershell
uv run python run.py session show 60c2d464f820db43
```

### 3. 恢复并继续会话

从指定会话进入连续聊天：

```powershell
uv run python run.py chat --session 60c2d464f820db43
```

也可使用短参数：

```powershell
uv run python run.py chat -s 60c2d464f820db43
```

程序会从当前 workspace 对应的 `session/index.json` 找到该哈希的 `latest_file`，恢复其中的 `summary`、`user`、`assistant.tool_calls` 和 `tool` 消息，然后把新输入接在同一会话后面。`summary` 会合入唯一的 System Prompt，恢复后的消息列表不再额外插入 `system` 角色。即使另一个 workspace 中存在该哈希，当前 workspace 也会按“未找到会话”拒绝恢复。

### 4. 单次任务

创建新会话并运行一次：

```powershell
uv run python run.py run "总结当前项目的结构"
```

在已有会话中继续执行一次任务：

```powershell
uv run python run.py run "继续刚才的分析" --session 60c2d464f820db43
```

单次任务结束后同样会打印会话哈希。

### 5. 会话文件位置

```text
Agent 自身 workspace：
.yy/memory/session/index.json
.yy/memory/session/YYYY-MM-DD_<会话哈希>_001.jsonl

外部 workspace：
.yy/memory/session/<workspace-hash>/index.json
.yy/memory/session/<workspace-hash>/YYYY-MM-DD_<会话哈希>_001.jsonl
```

这些路径都位于 Agent 根目录的 `.yy/memory/`，不是外部 workspace。不要手工修改 `index.json`。恢复只读取当前 workspace 分区索引中的 `latest_file`；上下文压缩成功后，新分段保留同一哈希并使用 `_002.jsonl`、`_003.jsonl` 等编号，首行是结构化 `summary`。压缩只提交 Session continuity summary；长期事实必须通过结构化 MemoryWriter 进入 `memory.sqlite3`，不会再由压缩模型累积改写 Session Profile Markdown。

工具调用也使用接近模型输入的格式逐条保存：模型请求写为带 `tool_calls` 的 assistant 记录，工具成功或失败写为带相同 `tool_call_id` 的 tool 记录。这样恢复和压缩都能看到完整工具链。

每条助手消息还会记录本轮使用的 Provider、模型、`base_url`、流式设置、整轮时延及逐次模型调用指标。例如：

```json
{"role":"assistant","content":"你好！","timestamp":"2026-07-19 15:30:15","model":{"provider":"deepseek","name":"deepseek-chat","base_url":"https://api.deepseek.com/v1","stream":false},"model_calls":[{"latency_ms":842.31,"input_tokens":{"context_total":156,"current_question":3,"context_source":"provider","current_question_source":"estimated"},"output_tokens":12,"output_tokens_source":"provider"}],"task_latency_ms":843.02}
```

`context_total` 和 `output_tokens` 优先使用模型接口返回的精确 usage；接口不返回时使用本地估算并将对应 `source` 标记为 `estimated`。OpenAI-compatible 接口在流式模式下会请求返回 usage。由于常见模型接口不提供“当前问题”独立计数，`current_question` 始终是本地估算值。一次用户任务若因工具结果产生多个模型 Turn，`model_calls` 会逐次记录，避免把多次输出 Token 混成一个数字。记录中绝不会写入 API Key。

## Skill 获取、审核与渐进加载

Skill 遵循 [Agent Skills 规范](https://agentskills.io/specification)。一个 Skill 至少要有带 YAML frontmatter 的 `SKILL.md`，其中 `name` 必须与目录名一致。框架实现位于 `skill/`；源码仓库的 `skills/<name>/` 是 System Prompt 与 `skill_read` 的唯一正式来源，并由 Git 正常跟踪。`~/.yy/skills/` 只保存下载审核隔离区、报告、发布索引和更新备份，不参与模型上下文读取。

在 `chat` 中使用以下命令：

```text
/skill list
/skill install <source> [--ref REF] [--path SKILL_PATH]
/skill update <name> <source> [--ref REF] [--path SKILL_PATH]
/skill audit <review-id>
```

本地来源必须位于当前 workspace，例如：

```text
/skill install .\my-skills\research-helper
```

公共 GitHub 仓库只接受无凭据的仓库根 HTTPS URL。仓库包含多个 Skill 时先列出候选，再用 `--path` 指定：

```text
/skill install https://github.com/example/agent-skills --ref main --path skills/research-helper
```

普通安装不会覆盖同名 Skill。更新必须显式提供现有名称和新来源：

```text
/skill update research-helper https://github.com/example/agent-skills --ref v2 --path skills/research-helper
```

`/skill install` 表示用户已经主动授权获取来源；审核干净时会直接安装。若发现脚本、可执行文件、网络/凭据/提权等敏感说明，或者许可证无法识别，CLI 会显示审核摘要与 `.yy/skills/audit/<review-id>.json` 路径，并再次通过方向键菜单确认。路径越界、符号链接、嵌套 Git、特殊文件、私钥/高置信凭据、格式错误或体积超限属于硬阻断，不能人工覆盖。更新无论审核是否干净都要再次确认，并在 `.yy/skills/backups/` 保留最多 5 个旧版本。

也可以用自然语言让主 Agent 安装 Skill。此时模型调用高风险 `skill_install`，先确认下载意图；如果审核还发现可接受风险，再进行第二次确认。Subagent 不能获得 `skill_install`。

`/skill install|update` 会先把来源下载到 `~/.yy/skills/review/`，完成安全审核和必要的人工确认后，再原子发布到源码仓库 `skills/<name>/`。发布不会修改当前 Runtime。文件监听器会在资源树稳定后构建新的不可变 Runtime Generation；也可以显式执行 `/skill refresh` 或 `/reload`。已经开始的 Turn 继续使用旧 Generation，同一 Session 的下一 Turn 自动创建或切换到新 Runtime，并从原 Session 恢复上下文。目录语义未变化时不会创建空 Generation 或切换 Runtime。System Prompt 只加入如下发现信息，不加载正文：

```xml
<available_skills>
  <skill>
    <name>research-helper</name>
    <description>能力与触发条件</description>
    <location>skills/research-helper/SKILL.md</location>
  </skill>
</available_skills>
```

模型需要完整说明、`references/` 或脚本文本时调用只读 `skill_read`。该工具只读取当前 Session 快照中登记的仓库 Skill，不执行任何脚本，并拒绝绝对路径、`..`、符号链接、二进制和超大结果。Session 运行期间若仓库 Skill 摘要发生变化，读取会被拒绝并提示 `/skill refresh`。Skill 自带脚本若确有必要，只能在沙箱可用时通过 Bash/Shell 执行，仍受 Schema、审批、沙箱和 checkpoint 约束；checkpoint-only 模式不会执行这些脚本。

## Hook、Turn 与 Session

本项目把 Session 视为逻辑上的完整 Trace，不额外创建 Trace 数据模型。概念上，每次真实模型 API 调用对应一个模型 Turn；该响应请求的一个或多个工具在下一次模型调用前完成。Hook 中的 `TURN_START/TURN_END` 则刻意定义为一次完整用户任务的外层边界，任务内可以出现多次 `MODEL_*` 与 `TOOL_*`。两者都不创建实体、不编号，也不向事件或 Session JSONL 写入编号。

核心注册入口是 `Agent/hook.py`。Tool、Skill、稳定 Prompt 和 Extension 由 `Agent/resources.py` 统一发现、验证并发布到 `.yy/runtime-plugins/generations/`；Runtime 只读取当前 Profile 的不可变 Snapshot。Bundle 在 `TRACE_START` 经 Hook 生命周期总线挂载并冻结，但 Tool 查找和执行仍由 Tool Registry 负责。完整边界、Reload、Rollback 和 Profile 隔离见 [`docs/runtime-hot-reload.md`](docs/runtime-hot-reload.md)。全局源码扩展由 `Agent/extensions.py` 从当前 Generation 的 `extension/hook/` 扫描，以下十个阶段各自拥有目录，每个目录可包含任意数量、
名称不同的 Python 能力文件：

```text
trace_start  trace_end
turn_start   turn_end
model_before model_during model_after
tool_before  tool_during  tool_after
```

时序固定为 `trace_start → turn_start → (model_* → tool_* …)* → turn_end → … → trace_end`。第二个用户问题同样会触发新的 `turn_start`，并且发生在该问题进入上下文前。`model_before` 可修改 `event.data["messages"]` 和 `event.data["tools"]`；`tool_before` 可修改工具名称和参数，修改后的参数仍会重新执行 JSON Schema 校验。`during` 在进入真实 Provider 或工具函数前通知一次，不会按流式文本片段重复触发；`after` 同时覆盖成功与失败，并通过 `result/reply/error` 暴露结果。

默认 Runtime 在 `trace_start` 通过同一 Hook 注册器加载 Sandbox Policy、打开或复用 checkpoint 基线，但不会启动 OS 隔离进程。第一次真正调用 `bash` 时才扫描或复用 Workspace 安全缓存并创建 OS Sandbox；`trace_end` 释放短期权限 Lease，长期扫描缓存继续保留。显式 `sandbox_backend=docker` 使用 Docker。安全后端不可用时进入 checkpoint-only。Subagent 复用父 Runtime 的安全上下文；压缩 Runtime 不需要危险工具，显式禁用沙箱。Harness Coding Runtime 使用同一工厂与生命周期，但 workspace 和 checkpoint 始终绑定自己的隔离 Git worktree。

Checkpoint 不写入 workspace 自身的 `.git`。当 workspace 就是 Agent 根目录时，每个 Session 在 `.yy/sandbox/checkpoints/<session-id>/` 使用独立 Git 对象库；外部 workspace 则保存到 `.yy/sandbox/checkpoints/<workspace-hash>/<session-id>/`。对象库用无父 commit 保存 workspace 快照，因此 workspace 的 `git status`、当前分支和 `git push` 都不会包含这些 commit。回溯采用 hard-reset 语义恢复非忽略文件，workspace 中的 `.git`、`.yy`、`.env*` 等敏感或运行期路径不会进入快照。

文件锁同样不写入项目 `.git`。统一的 `read_file` 获取工作区共享锁和目标文件共享锁，并把锁持有到文本读取或文档解析完全结束；`edit` 与 `write` 按“工作区共享锁 → 写事务独占锁 → 目标文件独占锁”获取，并把锁持有到 checkpoint 完成。搜索会在读取每个文件前获取共享锁。Bash、回溯和 Trace 基线无法预先限定单一文件，因此使用工作区独占锁，期间所有遵循本项目协议的 Agent 文件操作都会等待。

### 文档与论文读取

`read_file` 读取结构化文档时默认单次返回最多 30000 字符。PDF、PPTX 和 XLSX 未指定范围时从第 1 个单元开始，默认最多读取 20 页/张/表；显式范围单次最多 50 个单元。文档结果 JSON 会提供总页数、当前范围、是否截断和 `next_offset_chars`，模型可以继续读取同一范围，而不需要把整篇论文一次塞入上下文。读取源码和普通文本时仍直接返回原文，保持 Coding Agent 的兼容性。

PDF 使用布局文本模式，尽量保留多栏论文的水平位置和页面边界。如果所选页面几乎没有文本，结果会设置 `ocr_required=true` 并提示可能是扫描版或图片页。首期不会静默调用外部 OCR，也不会把论文上传到第三方服务；需要 OCR 的文件后续可接入独立的本地 OCR 工具。

工具只允许读取当前 workspace，持有同一个跨进程共享文件锁，并限制源文件最大 100 MiB、OOXML 解压后最大 250 MiB。旧式 `.doc`、`.ppt`、`.xls` 需要先用 Office 或 LibreOffice 转换为 `.docx`、`.pptx`、`.xlsx`；图片、音频、视频及带密码 PDF 不会被假装解析成功，而是返回明确错误。

这些锁覆盖同一项目中的多个 Runtime、CLI 进程和 Subagent，并在进程异常退出时由操作系统自动释放。锁不会阻止普通编辑器、手工 PowerShell 或其他不使用本项目锁协议的程序修改文件。

记忆没有专用的 Memory Hook 类。会话创建、历史与 Profile 注入、用户输入和最终回答落盘均作为普通回调注册到上述阶段；Runtime 和 PromptComposer 不直接读写记忆。自定义 `HookRegistry` 时，调用方需要自行注册希望保留的记忆回调。

自动压缩检查位于每次真实 API 请求的 `model_before`。Memory 先恢复历史但暂缓写入当前问题；请求上下文超限时只压缩已经持久化的历史，成功切段并重载 summary 后才把当前问题落盘，因此不会重复记录或把新问题吞进摘要。工具 Turn 的 `assistant.tool_calls` 和全部 `role=tool` 结果先完整落盘，下一次模型调用前可以作为一个完整链路被压缩。压缩最多尝试三次；全部失败时不修改 Profile、不切换 JSONL，只在当前进程内按最旧完整对话块裁剪输入，原始审计记录继续保留。

默认 Runtime 还通过统一工具装配入口注册 `subagent`。父模型必须明确给出子任务、可选角色说明和工具名称子集；省略工具子集表示无工具，且子 Agent 永远不能再次调用 `subagent`。

`subagent` 的风险等级是 `dynamic`：无工具或只读子集解析为 `read`，包含写入能力时解析为 `write`，由同一个工具 Registry 决定是否审批。委派写能力和实际执行写入分别需要一次批准。临时子 Agent 复用父级完整 `ToolContext`，工作区必须与父 Runtime 一致；它使用空 Memory，不创建独立会话记录，最终输出只作为父 Agent 的普通 `tool` 结果保存。

Runtime Observer 作为热插拔 `OBSERVER` Contribution 随 Generation 发布，但状态、事件可见性、
offset、恢复和审批由 Gateway Core 管理。它只能消费用户可见 Event 投影，不能读取 reasoning、
隐藏上下文、Tool 参数或结果，也不能调用 Tool 或修改 Prompt。CLI 与 Web 展示同一份持久化
Progress，详细数据流见 [`docs/runtime-observer.md`](docs/runtime-observer.md)。

Hook 注册方式参考 [PI Agent Extensions](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md) 的事件订阅模式：单一入口、可变事件上下文、按注册顺序执行。为保持本项目的安全边界，工具参数被 Hook 修改后仍会重新校验 Schema，这一点比 PI Agent 当前默认行为更严格。

每个扩展文件导出唯一 `EXTENSION_NAME`、`-50..50` 的 `PRIORITY`，以及
`async def handle(event, context)`。同阶段按优先级、文件名稳定执行；文件名、路径、
符号链接、重复名称、签名和导入错误都会在启动扫描时明确拒绝。扩展只获得不含凭据的
`ExtensionContext`。完整字段、阶段数据和跨阶段示例见
[`extension/README.md`](extension/README.md)。

Runtime 的事件流入口是 `AgentRuntime.run_task()`，表示处理一次用户输入；`AgentRuntime.run()` 返回聚合结果。不要把一次用户任务与模型 Turn 混为一谈。

### 6. 启动本机 Web UI

```powershell
uv run python run.py serve-ui
```

该命令会确保 Gateway 已启动，然后打开带一次性启动码的本机浏览器地址。浏览器交换启动码后只保存 `HttpOnly + SameSite=Strict` Cookie；写请求还需要 CSRF，Gateway 只监听 `127.0.0.1`。关闭网页不会停止任务，最终结果会进入 Inbox；重连后客户端按事件序号补齐遗漏事件。`serve-ui` 不再创建第二套 Runtime。

共享 React 工作台提供项目侧栏、Session/任务线程、流式对话、工具时间线、审批卡片、取消运行、模型/Sandbox 状态和未读 Inbox。Tauri 2 桌面端使用同一套组件，并通过平台对应的 PyInstaller sidecar 启动 Gateway；正式安装包的使用者不需要预装 Python、uv、Node 或 Rust。

### 7. 桌面安装包

推送 `v*` 标签或手动运行 `desktop-release` GitHub Actions 后，会分别生成 Windows MSI/NSIS、macOS DMG、Linux AppImage/DEB、独立 CLI/Gateway 可执行文件及 SHA-256 文件。首期构建不签名、不公证，也不自动更新；下载安装包后应先用同一 Release 中的 `.sha256` 核对文件，再按系统提示手动允许运行。

源码开发桌面端需要 Node 22、Rust stable 和 Tauri 的平台构建依赖，但这些只属于开发/CI 环境。构建流程先生成共享 React 产物，再用 PyInstaller 构建 `yy-agent` sidecar，最后由 Tauri 打包。

### 8. 查看全部命令

```powershell
uv run python run.py --help
uv run python run.py session --help
uv run python run.py chat --help
```

### 9. 运行测试与检查

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m pytest -q
uv run python -m compileall -q Agent bootstrap context_process cron dream gateway memory prompt sandbox skill tool tools run_ui tests harness-evolution run.py
uv lock --check
npm --prefix ui install
npm --prefix ui run build
cargo check --manifest-path desktop/src-tauri/Cargo.toml
uv run python run.py --help
```

## Gateway Cron 与 Heartbeat

Gateway 启动后会同时启动默认 60 秒一次的 Heartbeat。任务定义和调度状态唯一保存在 Agent Home 的 `.yy/cron/jobs.json`；每次到期都会创建全新 Session，通过现有 RuntimePool、事件流和 Inbox 执行，不会恢复上一次定时任务的会话历史。系统停机期间错过多个周期时，重启 Gateway 后最多补跑一次；同一个 Job 的旧 Run 未结束时，新周期会记为 overlap 并跳过。

支持固定间隔、带时区的单次 ISO 8601 时间和标准五段 Cron。五段表达式支持通配符、列表、范围、步进、月份/星期英文名称与星期日 `0/7`，日期和星期同时受限时遵循 Vixie Cron 的 OR 语义；不支持秒字段、年份字段和 `@daily` 等别名。每个任务使用自己的 IANA 时区，未指定时采用本机时区。未来五次执行时间可在创建前预览：

```powershell
uv run python run.py cron preview "0 9 * * 1-5" --timezone Asia/Shanghai
uv run python run.py cron add --every 30m --name "项目巡检" --prompt "检查项目状态并总结异常"
uv run python run.py cron add --cron "0 9 * * 1-5" --timezone Asia/Shanghai --name "工作日简报" --prompt "搜索 AI 新闻并生成摘要"
uv run python run.py cron at "2026-08-10T14:30:00+08:00" --name "单次提醒" --prompt "总结当前研究进展"
uv run python run.py cron list
uv run python run.py cron status
uv run python run.py cron pause <job-id>
uv run python run.py cron resume <job-id>
uv run python run.py cron run <job-id>
uv run python run.py cron remove <job-id>
```

交互式 `chat` 中也支持 `/cron ...` 命令；自然语言请求可由主 Agent 调用动态风险工具 `cronjob`。读取、校验和预览无需审批，创建、编辑、暂停、恢复、立即运行和删除仍需审批。定时 Run 没有在线发起客户端，因此写入、Bash、回滚、Skill 安装等危险工具会自动拒绝；只读文件、已审核 Skill、网络搜索、网页抓取和 Subagent 仍可使用。Cron Runtime 不包含 `cronjob`，Subagent 也不能获得它，避免任务递归创建计划。

`cron_heartbeat_seconds` 默认是 `60`，控制 Gateway Heartbeat 的轮询间隔。JSON 损坏时只暂停 Cron 并把状态标记为 unhealthy，普通聊天不受影响；修复前程序不会覆盖损坏文件。停止 Gateway 会先停止 Heartbeat，再关闭运行池。本阶段不会安装系统登录自启服务，计算机重启后由下一次 CLI、Web 或桌面端启动 Gateway 时恢复调度。

## Dream 每日记忆巩固

Gateway 默认在本地时间每天凌晨 03:00 处理前一个完整自然日的对话。Dream 会遍历 `~/.yy/memory/session/` 下所有 workspace、Session 和 JSONL 分段，以 user 消息作为唯一长期事实证据；assistant 只用于理解语境，reasoning、summary、system、工具调用、工具输出、Cron 与维护 Session 均不会成为用户画像证据。进入模型前还会脱敏 API Key、Token 与 Authorization 等凭据。

Dream 使用独立、非流式且无 Tool/Skill/Memory/Sandbox/Extension 的临时 `AgentRuntime`，先按 Token 预算提取带证据哈希的候选，再与现有长期记忆合并。默认只更新 `USER.md`、`RESEARCH.md`、`OTHERS.md` 及用户新增的普通 Profile Markdown 中由以下标记包围的区域；标记外的手写内容保持原样：

```markdown
<!-- dream:managed:start -->
## Dream 长期记忆

- 用户偏好中文技术说明。 <!-- dream:id=mem_xxx -->
<!-- dream:managed:end -->
```

结构化记忆保存在 `~/.yy/dream/memories.json`，运行报告、模型错误、输入/输出 Token、证据计数与备份位于 `~/.yy/dream/runs/` 和 `~/.yy/dream/backups/`。每次真正执行还会在 `~/.yy/dream/executions/` 写入隐藏、append-only 的无记忆执行记录：它保存来源 Session/文件/行号、内容 Hash、模型输入输出 Hash和结构化候选，不复制完整私密对话或完整 Prompt，也不会出现在普通 Session 列表中。原始 Session 永不修改或删除；冲突记忆只标为 `superseded`，不做不可恢复删除。

自动 Dream 在调度时冻结最新截止日期，把截止日前所有尚未处理的用户 Evidence 合成一个增量 changeset，只启动一次无记忆 Runtime；不再按漏掉的自然日逐日创建 Run。若没有新 Evidence，只推进扫描游标，不创建 Gateway Run、Operation、Inbox、worktree或模型 Runtime。自动成功和 no-op 保持静默，失败只保留最新一条未读通知；普通聊天仅提示待处理数量。成功后，当前正在执行的 Turn 继续使用原 System Prompt，下一次用户输入才重建 Profile 上下文。

```text
/dream status
/dream run [YYYY-MM-DD]
/dream backfill <开始日期> <结束日期>
/dream rollback [run-id]
```

`run` 默认显式处理昨天；`backfill` 仍按日期从旧到新执行，单次最多 31 天；`rollback` 只能依次回滚最近一次成功运行，以免跳过后续依赖状态。自动 Scheduler 与显式 backfill 语义分离：Gateway 离线期间积累的未处理 Evidence 会在下一次到期时合并为一个增量执行。普通 Agent Run 尚未结束时 Dream 会等待。

## 配置、状态与安全

- 核心配置、Runtime 事件、模型回复、Hook 事件、工具上下文、压缩结果和 Harness 请求均使用 Pydantic v2 定义；不可变契约启用冻结语义。
- 配置文件会严格校验字段类型、数值范围和未知字段。拼错配置名会在启动时明确报错，不再被静默忽略。
- 工具 JSON Schema 会在注册时编译为严格 Pydantic 参数模型；Hook 改写后的最终参数会再次校验，拒绝类型偷换、未知字段和非法枚举。
- 上下文压缩模型的 JSON 输出由 Pydantic 校验：普通 Session 同时返回 `profile_markdown` 与 `context_summary_markdown`；Harness 只返回摘要，禁止压缩过程创建 Session 哈希 Profile。
- 用户主目录中的 `~/.yy/` 是唯一完整本机状态目录，由 `uv run python run.py init` 创建；外部 workspace 和源码仓库不会因此生成运行期 `.yy`。
- Skill 框架代码位于 `skill/`；正式内容位于源码仓库 `skills/`。`~/.yy/skills/` 只保存可信索引、审核报告、暂存和备份，旧 `installed/` 即使存在也不会进入 Prompt。
- Agent Home 的 `.yy/sandbox/checkpoints/` 保存按 workspace 隔离的独立本地 Git 对象库和 Pydantic 审计索引；它捕获 workspace，但不会修改 workspace 的 Git 分支，也不会被上传。
- Agent Home 的 `.yy/sandbox/locks/` 保存按 workspace 隔离的空锁载体文件；Windows 使用 `LockFileEx`，Linux/macOS 使用 `flock`，进程退出后不依赖删除文件即可释放锁。
- `tests/error/*.jsonl` 只保存代码类缺陷的完整上下文、请求与异常栈，可能包含隐私，只在本机保留并由 Git 忽略。
- 本机模型配置：`.yy/settings.local.json`，可放置 `provider`、`model`、`base_url` 与 `api_key`；初始化模板由源码中的 `bootstrap/templates/` 提供。
- 普通聊天记忆位于 Agent Home 的 `.yy/memory/`；Harness 专属记忆位于 `.yy/harness-evolution/memory/`。两者都不写入目标 workspace。普通 Session 按 workspace 路径哈希分区；Harness 每次更新使用独立 Session。两者的长期事实均以各自 `memory.sqlite3` 为权威，Markdown 仅承担稳定规则或可重建投影。
- Session JSONL、Session 索引和 Profile 索引读取时均经过 Pydantic 校验；非法角色、损坏的工具链关联或错误索引会明确失败，不会静默污染下一轮上下文。
- 首次运行自动创建 `profile/USER.md`、`profile/RESEARCH.md`、`profile/OTHERS.md` 和索引。普通命名的扩展 Profile 全局加载；16 位会话哈希命名的 Profile 只注入对应 Session，避免跨会话污染。
- 新模型实现 `Agent.contracts.ModelProvider`；新工具实现 `tools.AsyncTool`；新回调通过 `HookRegistry.register()` 或 `HookRegistry.on()` 注册。
- 文件读取、搜索和写入必须使用 Runtime 注入的跨进程锁；写文件、沙箱 Shell 和 checkpoint 回溯还必须通过审批回调。写入路径不能越出项目工作区，模型命令不允许在无隔离的宿主机 Shell 中执行。
- Gateway/Web 只监听 `127.0.0.1`；256 位以上访问令牌保存在 Agent Home，浏览器使用一次性启动码、HttpOnly Cookie、CSRF、Origin 白名单、安全响应头与禁止缓存。`ChannelAdapter` 只预留未来飞书等渠道所需的身份映射、收发、审批和路由协议，本阶段没有公网监听或飞书实现。

## 常见问题

### `uv` 不是命令

关闭并重新打开 PowerShell，再执行 `uv --version`。仍失败时按 [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/) 检查安装和 PATH。

### `uv sync` 或 `uv run` 无法访问缓存

确认当前 PowerShell 使用的是安装 uv 的同一 Windows 用户，并检查缓存位置：

```powershell
uv cache dir
```

不要为解决缓存权限问题而混用管理员与普通用户终端；先修复该用户对 uv 缓存目录的访问权限，再重新运行 `uv sync`。

### Agent 只显示“已收到：…”

这表示仍在使用 `echo` Provider。按“配置真实模型”创建 `.yy/settings.local.json`，设置对应 API Key，然后重新启动命令。

### DeepSeek 通过 Python 请求时 TLS/连接失败，但直连测试可到达服务

程序现在默认 `use_system_proxy=false`，模型请求不会自动使用系统代理。确认本机配置没有把
它改成 `true`，也没有填写不需要的 `proxy_url`，然后执行 `gateway stop` 并重新启动聊天。
使用无效 API Key 直连时收到 HTTP 401，说明网络与 TLS 已经到达服务端；它属于认证错误，
不是连接失败。确实需要代理时再按“配置真实模型”中的两种手动代理方式任选其一。

### 提示沙箱后端不可用

Agent 会显示一次降级提示并继续使用 checkpoint-only；`edit`、`write` 和回退仍可用。OS 后端请检查系统能力与 `sandbox_reason`；Docker 后端请确认 CLI/daemon。安装好后端或调整 `sandbox_backend` 后重启 Gateway。状态查询只发现 OS 后端，Trace 内还需通过真实隔离自检；不会静默回退到无隔离 Shell。

## 全局论文 Reference 资料库

首次初始化会在用户 `~/.yy` 创建全局资料库，而不是在当前 workspace 创建数据库：

```text
~/.yy/reference/reference.sqlite3
```

该 SQLite 数据库保存论文元数据、DOI/ArXiv 标识、作者、标签、本地 PDF 路径与 SHA-256、
可核验的论文原文摘录，以及用于写作的引用例句。普通文件关联可以指向原 workspace；
`search-summary-paper` 下载的 PDF 位于全局 `.yy/papers/`。PDF 本体都不会写入数据库。
原文摘录与写作例句分别保存，并通过支撑关系关联，避免把模型生成的表述误认为论文原文。
资料库在所有 workspace 之间共享，但文件、摘录和例句会记录来源 workspace 与 Session。

主 Agent 默认获得三个工具：`reference_search` 和 `reference_get` 是只读工具；
`reference_write` 可新增/更新论文、保存摘录和引用例句、关联 PDF、归档/恢复论文及重新生成向量，
普通调用必须通过现有审批界面确认；已批准的论文批次只允许对该 Session 和候选论文继续写入，
不会为同一批次的总结、摘录和引用重复提示。数据库只支持软归档，不向模型提供不可恢复的硬删除。

全文检索使用 SQLite FTS5。可选的语义检索使用 OpenAI-compatible `/embeddings`：

```json
{
  "reference_search_mode": "rrf",
  "reference_embedding_model": "text-embedding-3-small",
  "reference_embedding_base_url": "https://api.openai.com/v1",
  "reference_embedding_api_key": "你的 Embedding API Key",
  "reference_keyword_weight": 0.4,
  "reference_semantic_weight": 0.6
}
```

`reference_search_mode` 可设为 `rrf`、`weighted` 或 `separate`，默认是 RRF。
Embedding 专用地址和 Key 留空时回退聊天模型的 `base_url` 与 `api_key`，但模型名必须显式配置。
写入论文、摘录或引用例句后，Gateway 会持久化后台任务并自动生成向量；失败按
2、4、8、16、32 秒最多重试五次，正文不会回滚。Embedding 不可用时全文检索仍正常工作。
启用 Embedding 表示标题、摘要、原文摘录、引用例句和搜索查询会发送给所配置的外部接口。

`reference_embedding_api_key` 只能保存在 `.yy/settings.local.json`，共享设置、日志、Session
和错误快照均不会保存它。`.yy/reference/`、SQLite 主文件、WAL 和备份均由 Git 忽略。

## Search Summary Paper 与全局论文库

首次初始化会审核并登记仓库内置 `search-summary-paper` Skill，并创建：

```text
~/.yy/papers/index.json
~/.yy/papers/<安全化论文标题>/<安全化论文标题>.pdf
~/.yy/papers/<安全化论文标题>/<安全化论文标题>.md
```

当用户要求检索、下载或总结研究论文时，该 Skill 先用只读 `profile_read` 读取
`~/.yy/memory/profile/RESEARCH.md`。文件为空时会停止并提示填写，不根据当前聊天猜测研究
方向。默认筛选 5 篇，也可以在问题中指定数量、关键词或年份范围。

检索使用 `web_search` 与 `web_fetch` 核对 arXiv、Semantic Scholar、OpenAlex、Crossref、
PubMed 和公开出版商页面。Google Scholar 仅作为公开候选链接来源，不直接抓取，也不会绕过
登录、验证码或付费墙。模型先调用 `paper_library_lookup` 查重，再用
`paper_library_download` 一次提交整批候选，因此整批下载只出现一次审批。
批准后的批次授权按 Session 和论文 ID 集合写入 `~/.yy/papers/grants.json`；Gateway 重启、
Runtime 空闲回收或上下文切换后会安全恢复，不要求重复下载。旧版仅保存在内存中的授权会从
真实成功的 Session 工具记录迁移，并同时核验候选身份、论文索引、PDF 路径与 SHA-256。

`index.json` 同时记录成功、重复、不可访问、解析失败和 `ocr_required`；身份优先使用 DOI、
arXiv ID、规范 URL，最后才使用题名与年份。下载前查索引，下载后再用 PDF SHA-256 查重。
相同文件不会被覆盖，只补齐缺失总结或 Reference 数据。全局论文库使用 Agent Home 专用锁和
原子替换，不属于当前 workspace，也不进入 workspace checkpoint。

模型通过 `paper_library_read` 按页读取完整 PDF，并通过 `paper_library_save` 保存中文结构化
总结。扫描件没有可提取文字时只记录 `ocr_required`，不会伪造全文总结。准确原文 passage、
页码和模型生成的引用示例继续分别写入 `~/.yy/reference/reference.sqlite3`；全局 PDF 只能通过
受控 `paper_id` 关联，`reference_write` 不接受任意 Agent Home 绝对路径。

## Branch-based Sandbox Checkpoint

Sandbox checkpoint 使用 Agent Home 中的独立裸 Git 仓库，不修改项目自身的 HEAD、index 或
branch。执行 rollback 时，原活动分支会变成 `ARCHIVED` 并完整保留后续内容，系统从指定恢复点
创建新的活动分支；后续 `write`、`edit` 和 `bash` 只在新分支继续提交。`sandbox_rollback` 支持
按 `steps`、checkpoint `sequence` 或完整 SHA 精确选择恢复点，并可通过 `merge_eligible=false`
明确声明旧分支不应进入自动 Dream。

`sandbox_checkpoint_limit` 只限制每个 Session 对用户可见的恢复点数量。超限时淘汰最老恢复点
ref，但仍被活动或归档 branch ref 引用的 Git commit 不会被物理删除。每日 Dream 会对
`ARCHIVED + READY + merge_eligible` 分支执行无 Memory、无工具的价值判断；只有判断为有价值、
三方合并无冲突、workspace 精确位于活动 HEAD 且验证通过时才自动创建双父 merge checkpoint。
`BLOCKED/DEFERRED/UNKNOWN` 属于 merge attempt 结果，不会污染分支的
`ACTIVE/ARCHIVED/MERGED` 生命周期。已合并分支 ref 默认保留30天，可通过
`sandbox_checkpoint_merged_branch_retention_days` 调整，之后仅在合并历史稳定且没有恢复引用时GC。

交互式 Runtime 可用 `sandbox_checkpoint_history` 查看恢复点、分支生命周期和 Dream 合并记录；
`sandbox_checkpoint_branch` 是需要统一高风险审批的管理入口，可明确重新启用或禁用某个归档分支的
Dream 准入。两个入口都只操作隔离 Checkpoint 仓库，不修改项目 Git。

## Gateway Canonical Event Store

Gateway 的业务状态与事件历史统一以 SQLite 为 durable truth。所有领域命令都通过
`StateController` 在同一个 `BEGIN IMMEDIATE` 事务中提交当前 Snapshot、不可变
`gateway_events`、`event_outbox`、按 Sink 冻结的 `event_deliveries` 以及命令幂等证据。
`event_id` 由 `command_id + event_key + stream_id` 确定性派生；同一个 Command 可以用不同
`event_key` 产生多个同类型事件，而 `(command_id, event_key)` 和
`(stream_id, stream_sequence)` 都由数据库唯一约束保护。

`canonical_event_json` 是每个 Event 唯一的规范序列化。JSONL Sink 原样复制这段 UTF-8 JSON，
EventBus 从它反序列化后投递；API、WebSocket replay 和 Projection rebuild 都通过
`EventStore` 读取，不再把 JSONL 当作查询或 Recovery 后备。Delivery 创建时会冻结
`required`、`sink_config_version` 和 `sink_config_hash`，Outbox 完成条件只读取这些不可变快照。
Dispatcher 使用 Delivery revision CAS 创建唯一活跃 Attempt，物理投递失败不会重新执行业务命令。

超过热保留期且满足安全条件的终态 Stream 可进入 Verified Archive。`PREPARING` 事务会先冻结精确
member 集合；Archive Writer 只能处理这组成员。Archive 分别验证 Event 正文的
`events_content_hash` 和不包含自身 Hash 的 `manifest_hash`，成员位置使用 UTF-8 byte offset。
验证成功后只清空 SQLite 中的热正文，Event 索引行、Hash、Delivery 和 Attempt Evidence 永久保留。
普通 retention 不得删除未归档 Canonical Event。

Gateway 数据库增长采用“保留事实、压缩热负载”的策略。`processed_commands` 仍永久保存精确的
command hash 与可重放 `ApplyResult`，但超过阈值的结果使用带原文长度和 SHA-256 的 zlib 包装；
旧结果由后台任务分批转换，任何损坏都会在重放时明确失败，不会猜测或删除幂等证据。`/health`
使用短期统计缓存，不再在每次进程探活时扫描完整数据库；完整 `quick_check`、`PRAGMA optimize`
和小批量压缩在 Gateway 就绪后周期执行。新建数据库预先启用 incremental auto-vacuum，已有数据库
不会在启动路径被隐式执行昂贵的全量 `VACUUM`。Health 会报告数据库/WAL大小、可回收空间、压缩进度
以及 500 MB/1 GB 默认容量告警；这些阈值和维护周期均可通过 Runtime 配置调整。

权威层级固定为：Level 1 是 Run/AgentState/Operation/Attempt/Approval 与 Canonical Event；
Level 2 是 Outbox、Delivery、Delivery Attempt、Consumer Cursor 和 Archive Evidence；Level 3 是
JSONL、EventBus、WebSocket 与 UI Projection。Level 3 永远不能反向决定业务事实。
