# Yuan Ye Study Agent

Yuan Ye Study Agent 是一个本地优先、单一异步 Runtime 驱动的学习与研究 Agent。正式入口始终是 `run.py`；CLI 与 Web UI 消费同一事件流，因此模型等待、工具执行、审批与错误都会即时可见。

<p align="center">
  <img src="images/harness_evolution.png" alt="Harness 自进化：工坊中的马正在修整自己的挽具" width="760">
</p>

## Harness 自进化

模型像一匹拥有力量和方向感的马，Harness 则是让这种能力能够被稳定驾驭的整套挽具：Runtime 负责节奏，Prompt 提供方向，Tools 延伸行动能力，Hooks 留出新的连接点，Memory 和上下文压缩让它记住一路上真正重要的东西。

所谓 **Harness 自进化**，不是让模型不受控制地改写自己，而是建立一条可审计的成长闭环：传统框架依赖硬编码固定逻辑，面对长对话、Token 波动、复杂子任务、新增交互场景容易失效，只能靠人工改代码、重启服务来适配。本 Agent 在发现代码类缺陷并取得用户确认后，会在隔离环境中生成和校验代码补丁。这是对“身体”和“大脑”的共同进化，而不是只优化“大脑”（Skill）。

CLI chat 能保存完整错误现场，并在用户确认后创建隔离 Git worktree，再启动一个复用正式 `AgentRuntime` 类的 Coding Agent。Coding Runtime 的 `workspace_root`、ToolContext、Docker 挂载和文件工具全部指向该 worktree；控制器会在运行前校验这一边界。它拥有专属 Memory、项目结构 Profile、上下文压缩、已审核 Skill、`skill_read`、文件读写/搜索、Docker Bash、checkpoint 回溯和 Subagent，但不提供 `skill_install`。用户确认启动 Harness 后，隔离 worktree 内的既有 Coding 工具自动获批；变更仍必须通过固定测试，才会提交并本地快进合并。测试失败或无变更时删除 worktree，Harness 不会 stash 脏主工作区，也不会自动推送 GitHub。

Coding Agent 的记忆位于 Agent 根目录的 `.yy/harness-evolution/memory/`，与普通聊天 Memory 和目标 workspace 分离。每次 Harness 更新创建全新的 Session JSONL，不恢复上一项修复的临时对话；同一次更新发生压缩时，继续使用相同 Session 哈希生成 `_002.jsonl`、`_003.jsonl`。跨更新只共享 `profile/AGENT.md`、`PROJECT.md`、`CHANGES.md` 和 `LESSONS.md` 四个长期文件。

`AGENT.md` 首次创建后只由用户维护；`PROJECT.md` 保存当前架构和 Tool/Hook 等开发规范；`CHANGES.md` 与 `LESSONS.md` 只追加已经通过测试并成功合并的事实。每个 Coding Session 都把 `AGENT.md`、`PROJECT.md` 全文和预算内的最新日志条目注入 System Prompt。合并成功后由无工具维护 Runtime 更新长期记忆，模型不可用时使用确定性项目扫描降级；失败、无变更或未合并的尝试只保留在 JSONL 和错误快照中。

> 本文以 Windows PowerShell 为例。项目要求 Python 3.10+；由 uv 管理项目 Python、`.venv` 和依赖，不需要手动使用 `pip` 或激活虚拟环境。

## 结构

```text
Agent/      模型适配、异步 ReAct、Runtime、Hook 协议与配置
memory/     记忆领域 Python 服务
context_process/ Token 阈值压缩、Profile 合并与失败裁剪
harness-evolution/ 错误快照、隔离 worktree、诊断与验证流水线
prompt/     System Prompt 分层组合
sandbox/    Trace 级 Docker、独立本地 Git checkpoint 与跨进程文件锁
skill/      Skill 获取、格式解析、静态审核、可信索引与安装事务
skills/     本机已安装的第三方 Skill 内容（仅提交 .gitkeep）
tools/      异步工具协议、注册表和受控内置工具
  contracts.py         AsyncTool 协议与 ToolContext
  registry.py          Schema 校验、注册与权限审批
  defaults.py          默认工具装配入口
  bash.py              Docker 内受限 Bash
  read_file.py         受控文件读取
  write_file.py        审批后原子写入并创建 checkpoint
  sandbox_rollback.py  审批后恢复本地 checkpoint
  calculator.py        受限四则运算
  search_workspace.py  工作区文本搜索
  current_time.py      本地时间查询
  subagent.py          受父 Agent 限权的临时子 Agent
  skill_read.py        渐进读取已审核 Skill 文本
  skill_install.py     审批后获取、审核和安装 Skill
run_ui/     Rich CLI、FastAPI 路由、模板和静态资源
tests/      核心行为与 UI 安全测试
.yy/memory/ Agent 根目录中的会话 JSONL、会话索引与长期 Profile（不提交）
run.py      唯一源码树入口
```

`memory/` 永远不保存用户数据。首次运行在 Agent 安装/源码根目录创建 `.yy/memory/`：会话消息按 workspace 隔离后写入 `session/` 下的 JSONL，长期 Profile 写入所有 workspace 共享的 `profile/` Markdown。启动 Agent 时所在的目录不会生成记忆或模型配置文件。

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

### 4. 安装并启动 Docker Desktop

正式 Runtime 会在 `trace_start` 创建 Docker 沙箱，即使当前问题最终没有调用 Bash，也需要 Docker 服务可用。安装 Docker Desktop 后先启动它，再确认客户端和服务端都能响应：

```powershell
docker version
docker info
```

首次 Trace 会自动构建本项目的 `yy-agent-sandbox:local` 镜像。容器运行时无网络、移除 Linux capabilities、限制 CPU/内存/进程数，并把容器根文件系统设为只读。若 Docker CLI 或服务不可用，Trace 会明确失败，不会把 Bash 降级到宿主机执行。

### 5. 首次启动并自动初始化 `.yy`

仓库不包含 `.yy/`。首次克隆后直接运行入口即可：

```powershell
uv run python run.py
```

第一次启动会在 Agent 根目录创建 `.yy/settings.local.json`、`.yy/.initialized.json`、`.yy/memory/session/index.json`，以及 `.yy/memory/profile/` 下的 `index.json`、`USER.md`、`RESEARCH.md` 和 `OTHERS.md`。同时会初始化 `.yy/skills/index.json`、审核/备份目录和 Agent 根目录的 `skills/`。后续执行 `run.py`、`chat`、`run` 或 `serve-ui` 时检测到初始化标记和必要文件齐全，就不会再次初始化。

如果你误删了 `.yy` 中的必要文件，可手动修复初始化：

```powershell
uv run python run.py init
```

初始化和修复都不会覆盖已有配置或记忆。整个 `.yy/` 都被 Git 忽略。

### 6. 在其他 workspace 中运行

Agent 根目录只负责代码、模型配置、记忆和本地沙箱状态；启动命令时的当前目录才是文件工具、Docker 挂载和 checkpoint 要处理的 workspace。例如：

```powershell
$AgentRoot = "D:\Ever_workspace\Yuan_Ye_Study_Agent"
cd D:\Ever_workspace\My_Project
uv run --project $AgentRoot yy-agent chat
```

上述命令仍从 `$AgentRoot\.yy\settings.local.json` 读取模型配置，并把该 workspace 的会话写入 `$AgentRoot\.yy\memory\session\<workspace-hash>\`；`My_Project` 不会生成 `.yy`。其他 workspace 看不到也不能恢复这些 Session，但 USER、RESEARCH、OTHERS 和普通扩展 Profile 仍全局共享。`read_file`、`write_file`、搜索、Docker Bash 和回溯只允许操作 `My_Project`，checkpoint 捕获的也是该目录。外部 workspace 的 checkpoint 对象库按相同 workspace 边界隔离后保存在 `$AgentRoot\.yy\sandbox\checkpoints\`，不会写进用户项目的 `.git`。

### 7. 先进行离线启动验证

仓库默认使用无需 API Key 的 `echo` Provider。它只回显输入，用于验证 CLI、UI 和 Runtime 是否正常；这不是实际的模型回答。

```powershell
uv run python run.py run "验证新版入口"
uv run python run.py chat
```

在交互模式中输入 `/help` 查看帮助，输入 `/exit` 或 `/quit` 退出。

## 配置真实模型

### 1. 创建本机配置文件

Agent 根目录中的 `.yy/settings.local.json` 是首次启动自动生成的本机模型配置文件，支持直接保存 `base_url` 与 `api_key`。即使从其他 workspace 启动，也始终读取这一份配置。如果文件被误删，可在 Agent 根目录执行：

```powershell
uv run python run.py init
```

然后编辑 `.yy/settings.local.json`，将 `api_key` 改为你刚轮换后的有效 Key。下面以 DeepSeek 为例：

```powershell
@'
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com",
  "api_key": "你的 API Key",
  "stream": false,
  "max_steps": 8,
  "compression_threshold_tokens": 20000,
  "sandbox_checkpoint_limit": 17
}
'@ | Set-Content -Encoding utf8 .yy/settings.local.json
```

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

`stream` 控制模型文本是否使用 SSE 实时输出，默认 `false`。设为 `true` 后，OpenAI-compatible Provider（包括 DeepSeek）会逐段显示生成文本；设为 `false` 时等待完整响应后再显示最终答案。Anthropic 当前仍采用完整响应模式。

`max_steps` 表示一次用户任务最多允许发起多少次模型 API 调用。

`compression_threshold_tokens` 默认是 `20000`。所有具备持久化 Memory 的 Runtime 会在每次 `model_before` 估算即将发送的完整 messages 与 Tool Schema；达到阈值时先压缩已经落盘的历史，再把当前新问题作为独立 `user` 消息写入新分段并调用模型。工具调用及结果会在下一次模型请求前一起参与检查。设为 `0` 可关闭自动压缩，但仍可手动使用 `/compress`。

`sandbox_checkpoint_limit` 默认是 `17`，必须是大于等于 1 的整数。它限制每个 Session 在 `.yy/sandbox/checkpoints/` 中保留的本地快照数量，基线也计入上限；超过后会删除最旧引用，并只清理独立 checkpoint 对象库，不改写项目主仓库。

只允许将 Key 写在 `.yy/settings.local.json` 或环境变量中。程序会拒绝 `.yy/settings.json` 中的 `api_key` 字段；整个 `.yy/` 均为本机目录且不会提交。

### 2. 可选：使用环境变量保存密钥

如不希望将 Key 写入本机 JSON，可删去 `api_key` 字段并在当前 PowerShell 会话设置密钥。以 DeepSeek 为例：

```powershell
$env:DEEPSEEK_API_KEY = "你的 API Key"
uv run python run.py chat
```

该环境变量只在当前 PowerShell 窗口有效。关闭窗口后需要重新设置；如需持久化，请使用你的系统凭据管理方案，并重新打开终端后再运行项目。配置文件中的 `api_key` 优先于环境变量，因此不要同时保存两个不同的 Key。

如果没有设置有效 Key，远程 Provider 会明确报出配置错误，不会静默退回网络请求或泄露密钥。

## 日常操作

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
- 已经开始会话后输入 `/compress`，可立即压缩当前上下文；命令本身不会写入 JSONL。
- `/skill list`、`/skill install`、`/skill update` 和 `/skill audit` 管理本机 Skill；这些命令不会写入 Session JSONL。
- `stream=true` 时，OpenAI-compatible Provider 会通过 SSE 逐段显示文本。
- 高风险工具会显示方向键审批菜单：使用 ↑/↓ 选择“允许本次 / 当前会话始终允许该工具 / 拒绝”，按 Enter 确认、Esc 取消，默认选中拒绝。会话授权在退出当前 Runtime 后自动失效。
- `bash` 只在当前 Trace 的无网络 Docker 容器中运行。容器读写挂载启动时的 workspace，因此命令造成的文件变化会立即出现在宿主机；一次成功 Bash 调用无论修改多少文件都只创建一个 checkpoint，没有变化则不创建。
- `write_file` 仍由宿主机执行原子写入，每次实际内容变化后创建一个 checkpoint；重复写入相同内容不会制造空快照。可让 Agent 调用高风险 `sandbox_rollback` 按步数恢复，执行前仍需审批。
- `enable_sandbox=False` 只用于没有写工具的压缩或诊断 Runtime。自定义 Runtime 未注入 checkpoint 上下文时，`write_file`、`bash` 和 `sandbox_rollback` 都会明确拒绝，不存在无快照写入旁路。
- 文件工具使用 Agent 根目录 `.yy/sandbox/locks/` 下按 workspace 隔离的跨进程读写锁。同一文件写入时，其他读取和写入会等待到原子替换、checkpoint 或失败恢复全部结束；不同文件的普通读取不会被无关写入阻塞。
- CLI chat 的单次模型调用遇到临时网络错误时会等待 2 秒后重试，总计最多 3 次；调用成功或进入工具结果后的下一次模型调用时重新计数。
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

程序会从当前 workspace 对应的 `session/index.json` 找到该哈希的 `latest_file`，恢复其中的 `summary`、`user`、`assistant.tool_calls` 和 `tool` 消息，然后把新输入接在同一会话后面。存储角色 `summary` 在发送模型前会转换为 `system`。即使另一个 workspace 中存在该哈希，当前 workspace 也会按“未找到会话”拒绝恢复。

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

这些路径都位于 Agent 根目录的 `.yy/memory/`，不是外部 workspace。不要手工修改 `index.json`。恢复只读取当前 workspace 分区索引中的 `latest_file`；上下文压缩成功后，新分段保留同一哈希并使用 `_002.jsonl`、`_003.jsonl` 等编号，首行是结构化 `summary`。压缩会同时更新全局 `profile/<会话哈希>.md` 和 `profile/index.json`，USER、RESEARCH、OTHERS 与普通扩展 Profile 可供所有 workspace 使用。

工具调用也使用接近模型输入的格式逐条保存：模型请求写为带 `tool_calls` 的 assistant 记录，工具成功或失败写为带相同 `tool_call_id` 的 tool 记录。这样恢复和压缩都能看到完整工具链。

每条助手消息还会记录本轮使用的 Provider、模型、`base_url`、流式设置、整轮时延及逐次模型调用指标。例如：

```json
{"role":"assistant","content":"你好！","timestamp":"2026-07-19 15:30:15","model":{"provider":"deepseek","name":"deepseek-chat","base_url":"https://api.deepseek.com/v1","stream":false},"model_calls":[{"latency_ms":842.31,"input_tokens":{"context_total":156,"current_question":3,"context_source":"provider","current_question_source":"estimated"},"output_tokens":12,"output_tokens_source":"provider"}],"task_latency_ms":843.02}
```

`context_total` 和 `output_tokens` 优先使用模型接口返回的精确 usage；接口不返回时使用本地估算并将对应 `source` 标记为 `estimated`。OpenAI-compatible 接口在流式模式下会请求返回 usage。由于常见模型接口不提供“当前问题”独立计数，`current_question` 始终是本地估算值。一次用户任务若因工具结果产生多个模型 Turn，`model_calls` 会逐次记录，避免把多次输出 Token 混成一个数字。记录中绝不会写入 API Key。

## Skill 获取、审核与渐进加载

Skill 遵循 [Agent Skills 规范](https://agentskills.io/specification)。一个 Skill 至少要有带 YAML frontmatter 的 `SKILL.md`，其中 `name` 必须与目录名一致。框架实现位于 `skill/`；安装后的第三方内容位于 Agent 根目录 `skills/<name>/`。`skills/` 和 `.yy/skills/` 都不会上传 Git，仓库只保留 `skills/.gitkeep`。

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

安装成功后，每个新用户任务都会重新扫描可信索引。System Prompt 只加入如下发现信息，不加载正文：

```xml
<available_skills>
  <skill>
    <name>research-helper</name>
    <description>能力与触发条件</description>
    <location>skills/research-helper/SKILL.md</location>
  </skill>
</available_skills>
```

模型需要完整说明、`references/` 或脚本文本时调用只读 `skill_read`。该工具不执行任何脚本，并拒绝绝对路径、`..`、符号链接、二进制和超大结果。Skill 自带脚本若确有必要，只能继续通过现有 workspace 工具与 Docker Bash 执行，仍受 Schema、审批、沙箱和 checkpoint 约束。手工复制到 `skills/`、安装后被修改或内容摘要不匹配的 Skill 都不会进入 Prompt，也不能读取；必须重新走审核安装。

## Hook、Turn 与 Session

本项目把 Session 视为逻辑上的完整 Trace，不额外创建 Trace 数据模型。每次真实模型 API 调用严格对应一个 Turn；该模型响应请求的一个或多个工具都在当前 Turn 内执行，工具结果需要再次发送给模型时才开始下一个 Turn。Turn 只表达生命周期边界，不创建实体、不编号，也不向事件或 Session JSONL 写入编号。

统一注册入口是 `Agent/hook.py`，包含以下十个可直接填写代码的异步回调：

```text
trace_start  trace_end
turn_start   turn_end
model_before model_during model_after
tool_before  tool_during  tool_after
```

时序固定为 `trace_start → turn_start → model_* → tool_*（可重复）→ turn_end → … → trace_end`。`model_before` 可修改 `event.data["messages"]` 和 `event.data["tools"]`；`tool_before` 可修改工具名称和参数，修改后的参数仍会重新执行 JSON Schema 校验。`during` 在进入真实 Provider 或工具函数前通知一次，不会按流式文本片段重复触发；`after` 同时覆盖成功与失败，并通过 `result/reply/error` 暴露结果。

默认 Runtime 在 `trace_start` 通过同一 Hook 注册器启动 Docker 沙箱并创建基线 checkpoint，在 `trace_end` 直接删除容器。Subagent 复用父 Runtime 的同一个沙箱上下文，不会提前关闭容器；上下文压缩 Runtime 没有危险工具，因此显式禁用沙箱。Harness Coding Runtime 则为隔离 Git worktree 启动自己的 Docker 和 checkpoint，绝不复用主聊天 workspace 的容器。

Checkpoint 不写入 workspace 自身的 `.git`。当 workspace 就是 Agent 根目录时，每个 Session 在 `.yy/sandbox/checkpoints/<session-id>/` 使用独立 Git 对象库；外部 workspace 则保存到 `.yy/sandbox/checkpoints/<workspace-hash>/<session-id>/`。对象库用无父 commit 保存 workspace 快照，因此 workspace 的 `git status`、当前分支和 `git push` 都不会包含这些 commit。回溯采用 hard-reset 语义恢复非忽略文件，workspace 中的 `.git`、`.yy`、`.env*` 等敏感或运行期路径不会进入快照。

文件锁同样不写入项目 `.git`。`read_file` 获取工作区共享锁和目标文件共享锁；`write_file` 按“工作区共享锁 → 写事务独占锁 → 目标文件独占锁”获取，并把锁持有到 checkpoint 完成。搜索会在读取每个文件前获取共享锁。Bash、回溯和 Trace 基线无法预先限定单一文件，因此使用工作区独占锁，期间所有遵循本项目协议的 Agent 文件操作都会等待。

这些锁覆盖同一项目中的多个 Runtime、CLI 进程和 Subagent，并在进程异常退出时由操作系统自动释放。锁不会阻止普通编辑器、手工 PowerShell 或其他不使用本项目锁协议的程序修改文件。

记忆没有专用的 Memory Hook 类。会话创建、历史与 Profile 注入、用户输入和最终回答落盘均作为普通回调注册到上述阶段；Runtime 和 PromptComposer 不直接读写记忆。自定义 `HookRegistry` 时，调用方需要自行注册希望保留的记忆回调。

自动压缩检查位于每次真实 API 请求的 `model_before`。Memory 先恢复历史但暂缓写入当前问题；请求上下文超限时只压缩已经持久化的历史，成功切段并重载 summary 后才把当前问题落盘，因此不会重复记录或把新问题吞进摘要。工具 Turn 的 `assistant.tool_calls` 和全部 `role=tool` 结果先完整落盘，下一次模型调用前可以作为一个完整链路被压缩。压缩最多尝试三次；全部失败时不修改 Profile、不切换 JSONL，只在当前进程内按最旧完整对话块裁剪输入，原始审计记录继续保留。

默认 Runtime 还通过统一工具装配入口注册 `subagent`。父模型必须明确给出子任务、可选角色说明和工具名称子集；省略工具子集表示无工具，且子 Agent 永远不能再次调用 `subagent`。

`subagent` 的风险等级是 `dynamic`：无工具或只读子集解析为 `read`，包含写入能力时解析为 `write`，由同一个工具 Registry 决定是否审批。委派写能力和实际执行写入分别需要一次批准。临时子 Agent 复用父级完整 `ToolContext`，工作区必须与父 Runtime 一致；它使用空 Memory，不创建独立会话记录，最终输出只作为父 Agent 的普通 `tool` 结果保存。

Hook 注册方式参考 [PI Agent Extensions](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md) 的事件订阅模式：单一入口、可变事件上下文、按注册顺序执行。为保持本项目的安全边界，工具参数被 Hook 修改后仍会重新校验 Schema，这一点比 PI Agent 当前默认行为更严格。

Runtime 的事件流入口是 `AgentRuntime.run_task()`，表示处理一次用户输入；`AgentRuntime.run()` 返回聚合结果。不要把一次用户任务与模型 Turn 混为一谈。

### 6. 启动本机 Web UI

```powershell
uv run python run.py serve-ui --port 8765
```

终端会输出包含随机 token 的本机地址。复制该地址到同一台电脑的浏览器访问；服务只监听 `127.0.0.1`，不要把带 token 的地址发布到聊天、Issue 或日志中。按 `Ctrl + C` 停止服务。

### 7. 查看全部命令

```powershell
uv run python run.py --help
uv run python run.py session --help
uv run python run.py chat --help
```

### 8. 运行测试与检查

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m pytest -q
uv run python -m compileall -q Agent bootstrap context_process memory prompt sandbox skill tools run_ui tests harness-evolution run.py
uv run python run.py --help
uv lock --check
```

## 配置、状态与安全

- 核心配置、Runtime 事件、模型回复、Hook 事件、工具上下文、压缩结果和 Harness 请求均使用 Pydantic v2 定义；不可变契约启用冻结语义。
- 配置文件会严格校验字段类型、数值范围和未知字段。拼错配置名会在启动时明确报错，不再被静默忽略。
- 工具 JSON Schema 会在注册时编译为严格 Pydantic 参数模型；Hook 改写后的最终参数会再次校验，拒绝类型偷换、未知字段和非法枚举。
- 上下文压缩模型的 JSON 输出由 Pydantic 校验：普通 Session 同时返回 `profile_markdown` 与 `context_summary_markdown`；Harness 只返回摘要，禁止压缩过程创建 Session 哈希 Profile。
- Agent 根目录的 `.yy/` 是完整的本机状态目录，由 `uv run python run.py init` 创建并被 Git 忽略；外部 workspace 不会因此生成 `.yy`。
- Skill 框架代码位于 `skill/`；可信索引、审核报告、暂存和备份位于 Agent 根目录 `.yy/skills/`，已安装内容位于 Agent 根目录 `skills/`。两处运行内容均被 Git 忽略。
- Agent 根目录的 `.yy/sandbox/checkpoints/` 保存按 workspace 隔离的独立本地 Git 对象库和 Pydantic 审计索引；它捕获 workspace，但不会修改 workspace 的 Git 分支，也不会被上传。
- Agent 根目录的 `.yy/sandbox/locks/` 保存按 workspace 隔离的空锁载体文件；Windows 使用 `LockFileEx`，Linux/macOS 使用 `flock`，进程退出后不依赖删除文件即可释放锁。
- `tests/error/*.jsonl` 只保存代码类缺陷的完整上下文、请求与异常栈，可能包含隐私，只在本机保留并由 Git 忽略。
- 本机模型配置：`.yy/settings.local.json`，可放置 `provider`、`model`、`base_url` 与 `api_key`；初始化模板由源码中的 `bootstrap/templates/` 提供。
- 普通聊天记忆位于 Agent 根目录 `.yy/memory/`；Harness 专属记忆位于 `.yy/harness-evolution/memory/`。两者都不写入目标 workspace。普通 Session 按 workspace 路径哈希分区；Harness 每次更新使用独立 Session，并只跨更新共享四个固定 Markdown。
- Session JSONL、Session 索引和 Profile 索引读取时均经过 Pydantic 校验；非法角色、损坏的工具链关联或错误索引会明确失败，不会静默污染下一轮上下文。
- 首次运行自动创建 `profile/USER.md`、`profile/RESEARCH.md`、`profile/OTHERS.md` 和索引。普通命名的扩展 Profile 全局加载；16 位会话哈希命名的 Profile 只注入对应 Session，避免跨会话污染。
- 新模型实现 `Agent.contracts.ModelProvider`；新工具实现 `tools.AsyncTool`；新回调通过 `HookRegistry.register()` 或 `HookRegistry.on()` 注册。
- 文件读取、搜索和写入必须使用 Runtime 注入的跨进程锁；写文件、Docker Bash 和 checkpoint 回溯还必须通过审批回调。写入路径不能越出项目工作区，Bash 不允许在宿主机执行。
- Web 只监听 `127.0.0.1`，访问令牌随机生成，所有响应禁止缓存。

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

### 提示 Docker CLI 或服务不可用

先启动 Docker Desktop，再分别执行 `docker version` 和 `docker info`。两条命令都成功后重新运行 Agent。系统不会因为 Docker 不可用而把 Bash 改到 PowerShell、CMD 或宿主机 Bash 中执行。
