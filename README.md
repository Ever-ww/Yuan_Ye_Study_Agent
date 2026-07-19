# Yuan Ye Study Agent

Yuan Ye Study Agent 是一个本地优先、单一异步 Runtime 驱动的学习与研究 Agent。正式入口始终是 `run.py`；CLI 与 Web UI 消费同一事件流，因此模型等待、工具执行、审批与错误都会即时可见。

> 本文以 Windows PowerShell 为例。项目要求 Python 3.10+；由 uv 管理项目 Python、`.venv` 和依赖，不需要手动使用 `pip` 或激活虚拟环境。

## 结构

```text
Agent/      模型适配、异步 ReAct、Runtime、Hook 协议与配置
memory/     记忆领域 Python 服务
prompt/     System Prompt 分层组合
tools/      异步工具协议、注册表和受控内置工具
run_ui/     Rich CLI、FastAPI 路由、模板和静态资源
tests/      核心行为与 UI 安全测试
.yy/memory/ 本机会话 JSONL、会话索引与长期 Profile（不提交）
run.py      唯一源码树入口
```

`memory/` 永远不保存用户数据。首次运行自动创建 `.yy/memory/`：会话消息写入 `session/` 下的 JSONL，长期 Profile 写入 `profile/` 下的 Markdown。

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

### 4. 首次启动并自动初始化 `.yy`

仓库不包含 `.yy/`。首次克隆后直接运行入口即可：

```powershell
uv run python run.py
```

第一次启动会先创建 `.yy/settings.local.json`、`.yy/.initialized.json`、`.yy/memory/session/index.json`，以及 `.yy/memory/profile/` 下的 `USER.md`、`RESEARCH.md` 和 `OTHERS.md`，然后显示命令帮助。后续执行 `run.py`、`chat`、`run` 或 `serve-ui` 时检测到初始化标记和必要文件齐全，就不会再次初始化。

如果你误删了 `.yy` 中的必要文件，可手动修复初始化：

```powershell
uv run python run.py init
```

初始化和修复都不会覆盖已有配置或记忆。整个 `.yy/` 都被 Git 忽略。

### 5. 先进行离线启动验证

仓库默认使用无需 API Key 的 `echo` Provider。它只回显输入，用于验证 CLI、UI 和 Runtime 是否正常；这不是实际的模型回答。

```powershell
uv run python run.py run "验证新版入口"
uv run python run.py chat
```

在交互模式中输入 `/help` 查看帮助，输入 `/exit` 或 `/quit` 退出。

## 配置真实模型

### 1. 创建本机配置文件

`.yy/settings.local.json` 是首次启动自动生成的本机模型配置文件，支持直接保存 `base_url` 与 `api_key`。如果文件被误删，可执行：

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
  "max_steps": 8
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
- `stream=true` 时，OpenAI-compatible Provider 会通过 SSE 逐段显示文本。
- 高风险写文件工具会请求一次性确认；输入 `n` 拒绝本次操作。

### 2. 查看已有会话

列出全部可恢复会话：

```powershell
uv run python run.py session list
```

列表会显示会话哈希、创建时间、最新分段消息数和 JSONL 文件名。查看某个会话的带时间戳记录：

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

程序会从 `session/index.json` 找到该哈希的 `latest_file`，读取最新 JSONL 分段中的历史 `user`/`assistant` 消息，然后把新输入接在同一会话后面。哈希不存在时会在调用模型前直接报错。

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
.yy/memory/session/index.json
.yy/memory/session/YYYY-MM-DD_<会话哈希>_001.jsonl
```

不要手工修改 `index.json`。当前恢复只读取索引中的 `latest_file`；未来发生上下文压缩后，新分段会保留同一哈希并使用 `_002.jsonl`、`_003.jsonl` 等编号。

每条助手消息还会记录本轮使用的 Provider、模型、`base_url`、流式设置、整轮时延及逐次模型调用指标。例如：

```json
{"role":"assistant","content":"你好！","timestamp":"2026-07-19 15:30:15","model":{"provider":"deepseek","name":"deepseek-chat","base_url":"https://api.deepseek.com/v1","stream":false},"model_calls":[{"latency_ms":842.31,"input_tokens":{"context_total":156,"current_question":3,"context_source":"provider","current_question_source":"estimated"},"output_tokens":12,"output_tokens_source":"provider"}],"task_latency_ms":843.02}
```

`context_total` 和 `output_tokens` 优先使用模型接口返回的精确 usage；接口不返回时使用本地估算并将对应 `source` 标记为 `estimated`。OpenAI-compatible 接口在流式模式下会请求返回 usage。由于常见模型接口不提供“当前问题”独立计数，`current_question` 始终是本地估算值。一次用户任务若因工具结果产生多个模型 Turn，`model_calls` 会逐次记录，避免把多次输出 Token 混成一个数字。记录中绝不会写入 API Key。

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

记忆没有专用的 Memory Hook 类。会话创建、历史与 Profile 注入、用户输入和最终回答落盘均作为普通回调注册到上述阶段；Runtime 和 PromptComposer 不直接读写记忆。自定义 `HookRegistry` 时，调用方需要自行注册希望保留的记忆回调。

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
uv run python -m compileall -q Agent bootstrap memory prompt tools run_ui tests run.py
uv run python run.py --help
uv lock --check
```

## 配置、状态与安全

- `.yy/` 是完整的本机目录，由 `uv run python run.py init` 创建，整个目录已被 Git 忽略。
- 本机模型配置：`.yy/settings.local.json`，可放置 `provider`、`model`、`base_url` 与 `api_key`；初始化模板由源码中的 `bootstrap/templates/` 提供。
- 全部记忆衍生物：`.yy/memory/`。`session/index.json` 指向每个会话最新 JSONL 分段；文件名为 `年月日_会话哈希_分段号.jsonl`。
- 首次运行自动创建 `profile/USER.md`、`profile/RESEARCH.md`、`profile/OTHERS.md`；Profile 加入新的 `.md` 文件后会自动被读取，不需要修改代码。
- 新模型实现 `Agent.contracts.ModelProvider`；新工具实现 `tools.AsyncTool`；新回调通过 `HookRegistry.register()` 或 `HookRegistry.on()` 注册。
- 写文件等高风险工具必须通过 Runtime 的审批回调，且文件路径不能越出项目工作区。
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
