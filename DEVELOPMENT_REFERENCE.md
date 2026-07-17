# Yuan Ye Study Agent 开发参考

本文记录当前项目的实现状态，供后续迭代时快速了解入口、配置和扩展点。

## 运行方式

```powershell
python run.py
```

启动交互式 CLI 后可连续输入任务；使用 `/help` 查看帮助、`/exit` 退出。也可以直接执行单个任务：

```powershell
python run.py "计算 (25 + 17) * 3"
```

正式 Harness 推荐在 `.yy/settings.local.json` 选择模型，并通过供应商环境变量或系统
Credential Store 提供 API Key。根目录 `config.ini` 主要服务旧同步 API 和迁移流程；但
Harness 未显式设置 model 时，`LegacyModelProvider` 仍会读取其中的默认模型；未提供
Harness provider 覆盖时还会复用 INI 连接配置。因此它不是推荐配置，却仍可能影响缺省
启动行为。

## 目录职责

```text
Agent/          同步 ReAct 兼容层、异步 Runtime、会话、审批、Hooks、Cron 与沙箱
model_choice/   原模型客户端、配置加载器与异步 Provider 适配层
tools/          同步工具、Harness 工具注册表及内置/扩展工具
memory/         长期记忆与学习资料库
skills/         Skill 注册、Git 下载、插件与市场管理
prompt/         分层 System Prompt 组合器
run_ui/         旧终端兼容层、Typer/Rich CLI 与 FastAPI Web UI
config.ini      本机模型配置（Git 忽略）
config.ini.example  可提交的配置模板
run.py          项目启动入口
```

### Agent 包内部边界

为避免“同步 ReAct、异步 Harness、子代理定义、团队任务”继续混在同一模块，`Agent/`
现在按职责拆分：

- `agent.py`：旧同步 `Agent` 的高层组装、`AgentConfig` 与备用模型重试。
- `react_agent.py`：旧同步 `ReActAgent`、协议、`Step` 与旧 `AgentResult`。
- `tool_registry.py`：旧同步 `ToolRegistry` 的注册与调用边界。
- `legacy.py`：仅兼容聚合上述同步符号，保留 `Agent.legacy` 历史导入路径。
- `runtime.py`：新异步 `AgentRuntime` 主循环，只依赖正式的 Harness 组件。
- `subagents.py`：发现和解析 Markdown 子代理定义。
- `teams.py`：团队任务 DAG、领取状态与邮箱持久化。
- `agents.py`：保留子代理注册表与团队存储的历史聚合导入路径。

旧代码无需立即迁移；`Agent.legacy` 保持兼容，而维护同步代码时应直接修改
`Agent.agent`、`Agent.react_agent` 或 `Agent.tool_registry`。包顶层 `from Agent import Agent`
仍表示旧同步 API，而推荐的新入口是 `AgentRuntime.run_turn()`。

## 旧同步模型配置

根目录 `config.ini` 负责旧同步 `Agent`/`ModelClient`，以及新 Harness 未显式选择模型时的
兼容回退配置：

```ini
[model_choice]
default_model = deepseek
fallback_model = qwen
timeout_seconds = 60

[providers.deepseek]
base_url = https://api.deepseek.com/v1
api_key =
api_key_env = DEEPSEEK_API_KEY
```

- `default_model`：默认使用的模型别名或 `provider:model`。
- `fallback_model`：主模型达到最大步骤数但未完成时使用；留空则不启用。
- `api_key`：本机密钥，优先级高于环境变量。由于 `config.ini` 已在 `.gitignore` 中，可本地填写；更推荐留空并使用 `api_key_env`。
- `base_url`：对应供应商 API 地址，也可改为企业网关地址。

已适配：GPT/OpenAI、Claude/Anthropic、DeepSeek、GLM、Qwen、Kimi。

新 Harness 的配置链、凭据读取和迁移方式见下文“构建、安装与配置体系”。

## 创建旧同步 Agent

`Agent` 是可实例化的 ReAct Agent，负责模型决策、工具调用与 Observation 回填：

```python
from Agent import Agent, AgentConfig
from tools import CalculatorTool, CurrentTimeTool

config = {
    "max_steps": 8,
    "temperature": 0,
    "trace_enabled": True,  # 任意扩展参数
}

agent = Agent(
    system_prompt="你是一名严谨的学习助手。",
    tools=[CalculatorTool(), CurrentTimeTool()],
    model="deepseek",       # None 时读取 config.ini 的 default_model
    config=config,
)
result = agent.run("计算 (25 + 17) * 3")
print(result.answer)
```

`config` 会通过 `AgentConfig.from_dict()` 解析：

- `max_steps`、`temperature` 为当前内置且会校验的字段。
- 其他字段保留在 `agent.config.extras`，可通过 `agent.config.get("字段名")` 读取。
- 配置字典中的 `fallback_model` 可覆盖 `config.ini` 的备用模型。

## 旧同步 ReAct 执行流程

1. `Agent` 创建 `ModelClient`、`ToolRegistry` 和内部 `ReActAgent`。
2. 模型根据系统提示与工具 Schema 输出 JSON Action。
3. `ToolRegistry` 调用指定工具，得到 Observation。
4. Observation 回填给模型，循环直到返回 `final` 或达到 `max_steps`。
5. 未完成且配置了 `fallback_model` 时，使用备用模型重新执行任务。

模型输出协议：

```json
{"action": "calculator", "action_input": {"expression": "2 + 2"}}
```

或：

```json
{"action": "final", "final": "最终回答"}
```

## 添加工具

同步兼容工具放在根目录 `tools/`，实现 `tools.Tool` 协议：`name`、`description`、
`parameters` 和 `run(arguments)`。

示例：

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class MyTool:
    name: str = "my_tool"
    description: str = "工具功能说明"
    parameters: dict[str, Any] = None

    def run(self, arguments: dict[str, Any]) -> str:
        return "工具结果"
```

同步调用方在创建旧 `Agent` 时显式传入：

```python
agent = Agent(
    system_prompt="你是一名学习助手。",
    tools=[CalculatorTool(), CurrentTimeTool(), MyTool()],
)
```

新 Harness 工具应实现 `AsyncTool`，或继承 `tools.BaseTool`，声明 JSON Schema、`risk`、
`sandboxed` 与异步 `run()`，再通过 `runtime.tools.register(tool)` 注册。不要把异步工具交给
旧同步 `ToolRegistry`。新增工具必须限制输入、权限和访问范围；文件工具还应复用
`PathPolicy` 并记录变更，否则工作区边界和 rewind 不会自动生效。

## 旧动态 CLI

`run_ui/console.py` 使用线程在后台运行 Agent，并在主线程显示状态动画，结束后输出最终回答和工具轨迹。运行界面不依赖第三方库，兼容 Windows 默认终端编码。
它只保留同步 API 兼容，不再是 `run.py` 的默认实现；正式入口是 `run_ui/cli.py` 中的
Typer/Rich CLI。

## Harness 0.2 实现状态

新 Harness 已按职责合并到原有的 `Agent/`、`model_choice/`、`tools/`、`memory/`、`skills/`、`prompt/` 与 `run_ui/`，通过 `yy-agent` 或 `python run.py` 启动。原有
`Agent(...)` 同步 API 保留兼容；新任务可使用 `AgentRuntime.run_turn()` 异步事件流。

当前已接入：

- `.yy/settings.json`、`.yy/settings.local.json`、用户配置和 CLI 覆盖组成的分层配置；
  `AGENT.md` 为正式项目指令，同时只读兼容 `AGENTS.md`、`CLAUDE.md` 和 `.claude` 组件。
- OpenAI Responses、Anthropic Messages 和 OpenAI-compatible Chat Completions 原生工具调用，
  不兼容网关自动回退严格 JSON Action 协议。
- SQLite 事件会话、Prompt 来源检查、上下文压缩、文件变更日志和冲突安全的 `/rewind`。
- 文件/Git/Web/浏览器/Windows 桌面等内置工具、四种权限模式、持久审批规则和 Docker
  fail-closed 沙箱；桌面与关键主机动作不能被永久自动批准。
- SQLite/FTS5 长期记忆、`MEMORY.md` 索引，以及 PDF/Markdown/TXT/HTML 学习资料库。
- Open Agent Skills 注册与 Git 安装，`.yy-plugin`/`.claude-plugin` 市场、版本哈希、组件信任、
  更新与卸载。
- command、HTTP、prompt、agent Hooks；持久 Cron 守护进程和会话 `/loop`。
- MCP client/server、通用 LSP JSON-RPC、子代理定义、worktree 隔离、团队任务 DAG 与邮箱。
- Typer/Rich CLI 和固定绑定 `127.0.0.1`、带 token/CSRF 与审批队列的 FastAPI Web UI。

开发验证使用：

```powershell
python -m unittest discover -s tests -v
```

完整 CLI/Web 测试需要先安装 `pyproject.toml` 中的依赖；Docker、Playwright、MCP 和 Windows
UI Automation 属于可选 smoke test，不影响无 API Key 的核心测试。

---

## 2026-07-17 全量合并、重构与注释记录

本节是从原同步 ReAct 项目升级到 `0.2.0` 本地 Harness 后的完整变更记录。它既记录新增
能力，也记录兼容层、安全加固、测试结果和当前仍受外部环境限制的部分。后续维护时应以
本节、各模块 README 和实际测试共同作为基线，不能仅依据早期同步 Agent 的说明判断行为。

### 交付范围与兼容策略

- 正式 Python 版本要求调整为 Python 3.10+，分发名称为 `yy-agent`。
- 正式命令入口为 `yy-agent = run_ui.cli:app`；`python run.py` 保留为源码树便捷入口。
- 原同步 `Agent(...)`、`AgentConfig`、`AgentResult`、`ReActAgent` 和同步 `ToolRegistry`
  继续可用，但定位为旧代码兼容 API。
- 新代码推荐使用 `AgentRuntime.run_turn()` 获取异步 `RunEvent` 流，或使用
  `AgentRuntime.run()` 获取聚合后的 `RuntimeResult`。
- 同名类型保持明确隔离：包顶层 `AgentResult` 表示旧同步结果，异步结果导出为
  `RuntimeResult`；包顶层 `ToolRegistry` 表示旧同步注册表，异步注册表导出为
  `tools.AsyncToolRegistry`。
- 不包含 IDE 插件、跨机器 Worker、云端多租户服务和影响整个 Git 工作区的快照回滚。

### 文件级变更总表

| 范围 | 文件 | 本次职责或修改 |
| --- | --- | --- |
| 构建与入口 | `pyproject.toml`、`run.py` | 增加 PEP 517/621 构建、依赖分组、`yy-agent` 命令和兼容启动器 |
| 项目配置 | `.yy/settings.json`、`.yy/settings.schema.json` | 增加可共享项目配置、JSON Schema、profile、权限和沙箱设置 |
| 项目指令 | `AGENT.md`、`.yy/agents/reviewer.md` | 增加正式项目指令和只读审查子代理示例 |
| Hook 示例 | `.yy/hooks.example.json` | 增加无需外部脚本的 `PreToolUse` prompt Hook 配置示例；示例文件不会自动启用 |
| Git 与 CI | `.gitignore`、`.gitattributes`、`.github/workflows/tests.yml` | 排除凭据/数据库/构建状态，统一 LF；增加最小权限的 Windows、Ubuntu 与 Python 3.10/3.12 CI |
| 同步高层入口 | `Agent/agent.py` | 旧 Agent 组装、AgentConfig 与备用模型回退 |
| 同步 ReAct | `Agent/react_agent.py` | ReAct 协议、同步循环、Step 与旧 AgentResult |
| 同步工具表 | `Agent/tool_registry.py` | 同步 Tool 协议的注册、查找与错误转换 |
| 同步兼容聚合 | `Agent/legacy.py` | 只转发同步符号，兼容 `Agent.legacy`，不承载业务逻辑 |
| 兼容转发 | `Agent/agents.py` | 转发子代理注册表与团队存储的旧聚合导入 |
| 公共 API | `Agent/__init__.py`、`Agent/types.py` | 懒加载公开符号；定义事件、会话、模型、工具和扩展协议 |
| Runtime | `Agent/runtime.py` | 异步主循环、事件流、审批、压缩、恢复、rewind、子代理与团队调度 |
| 配置与存储 | `Agent/config.py`、`Agent/storage.py` | 分层配置、运行目录、旧配置迁移、SQLite Schema 和事件溯源 |
| 权限与 Hook | `Agent/permissions.py`、`Agent/hooks.py` | 权限模式、持久规则、能力包和四类 Hook 处理器 |
| 隔离与集成 | `Agent/sandbox.py`、`Agent/integrations.py` | Docker fail-closed、最小环境、MCP 与 LSP 管理 |
| 后台任务 | `Agent/scheduler.py` | 五字段 Cron、时区、重试、单实例守护进程和崩溃恢复 |
| 多 Agent | `Agent/subagents.py`、`Agent/teams.py` | Markdown 子代理定义、团队任务 DAG、原子领取和邮箱 |
| 模型层 | `model_choice/*.py` | 保留同步客户端；新增异步 Provider、原生工具调用和备用模型包装 |
| 工具层 | `tools/harness.py`、`tools/extensions.py` | 异步注册表、文件/Git/Shell/Web/浏览器/桌面及 Harness 扩展工具 |
| 原工具 | `tools/base.py`、`calculator.py`、`current_time.py` | 保留同步 Tool 协议并补充校验和详细中文说明 |
| Prompt | `prompt/composer.py` | 固定顺序组合安全规则、profile、环境、指令、记忆、Skills 与摘要 |
| Memory | `memory/store.py` | SQLite/FTS5 长期记忆、`MEMORY.md` 索引和独立资料库 |
| Skills/插件 | `skills/registry.py` | Skill 发现/Git 安装、市场、插件、信任、哈希、更新与安全回滚 |
| CLI/Web | `run_ui/cli.py`、`run_ui/web.py`、`run_ui/console.py` | Typer/Rich CLI、本机 FastAPI UI 和旧同步终端兼容层 |
| 测试 | `tests/test_*.py` | 核心 Harness、Hook、集成、调度器、Skill/插件和子代理安全回归 |
| 文档 | 根 README、`Agent/README.md`、`model_choice/README.md`、`run_ui/README.md` | 安装、命令、API、安全边界和扩展开发说明 |

### 构建、安装与配置体系

新增 `pyproject.toml` 后，项目可执行：

```powershell
python -m pip install -e .
yy-agent doctor
```

开发环境使用：

```powershell
python -m pip install -e ".[dev]"
```

可选依赖组：

| Extra | 内容 |
| --- | --- |
| `desktop` | Pillow、Playwright、Windows `pywinauto` |
| `mcp` | 官方 MCP Python SDK |
| `vector` | 预留 `sqlite-vec` 适配器依赖 |
| `keyring` | 操作系统凭据存储 |
| `dev` | pytest、pytest-asyncio、pytest-cov |

Runtime 配置按以下顺序从低到高覆盖：

1. 内置默认值。
2. `~/.yy/settings.json` 用户配置。
3. 项目 `.yy/settings.json` 共享配置。
4. 项目 `.yy/settings.local.json` 本机配置。
5. CLI 或 Python 调用显式覆盖。

`.yy/settings.schema.json` 已同步 Runtime 当前识别的 model/fallback/vision、temperature、
context event limit、Web 搜索 URL、Provider、profile、权限、团队和沙箱字段；未知字段仍通过
`additionalProperties` 保留前向兼容性。

`.yy/settings.local.json`、`.yy/.state/`、本地插件锁、`AGENT.local.md`、通用 `.env*`、
数据库、虚拟环境和构建产物均被 Git 忽略；`.env.example` 可显式提交。可共享的
`.yy/settings.json`、Schema、Agent 定义与 Hook 示例允许提交。`.gitattributes` 将项目文本
统一规范为 LF，降低 Windows/Linux CI 之间的行尾噪声。

运行状态默认位于：

```text
~/.yy/projects/<project-id>/state.db
```

可使用 `YY_AGENT_HOME` 修改用户状态根目录。旧 `config.ini` 仍可能由底层兼容 Provider
读取；`yy-agent migrate` 可将普通配置迁移到 `.yy/settings.local.json`，明文 API Key 只会
写入安装好的系统 keyring。未安装 keyring 时迁移会先失败并提示用户手工改用环境变量，
不会由程序设置环境变量，也不会把密钥写入可提交 JSON。

### Agent 包结构重构

旧同步实现按职责拆回清晰模块，避免所有修改都堆积到 `legacy.py`：

- `agent.py` 是同步 `Agent`、`AgentConfig`、模型客户端组装与备用模型回退的唯一实现位置。
- `react_agent.py` 是 `ReActAgent`、`Step`、旧 `AgentResult` 和 ReAct JSON 协议的唯一实现位置。
- `tool_registry.py` 是同步 `ToolRegistry` 的唯一实现位置。
- `legacy.py` 只重导出以上对象；历史 `from Agent.legacy import ...` 与新模块导入保持对象身份相同。
- `subagents.py` 只负责发现、解析和缓存 Markdown 子代理定义。
- `teams.py` 只负责团队、任务 DAG、领取、完成状态和邮箱持久化。
- `agents.py` 仅兼容原聚合导入路径，并转发 `AgentRegistry` 与 `TeamStore`。
- `Agent/__init__.py` 使用映射式惰性导入；底层 `tools`、`memory`、`skills` 可以安全引用
  `Agent.types`，不会因包初始化提前载入 `Agent.runtime` 而形成循环依赖。

以下旧代码继续成立：

```python
from Agent import Agent, AgentConfig, AgentResult, ToolRegistry
from Agent.agent import Agent
from Agent.react_agent import ReActAgent
from Agent.tool_registry import ToolRegistry
from Agent.legacy import Agent as LegacyAgent  # 旧路径仍可用
from Agent.agents import AgentRegistry, TeamStore
```

新代码推荐：

```python
from Agent import AgentRuntime, RuntimeResult
from Agent.subagents import AgentRegistry
from Agent.teams import TeamStore
from tools import AsyncToolRegistry
```

### 异步 Runtime 与事件溯源

`AgentRuntime.run_turn()` 是正式异步入口，负责：

1. 创建或恢复 `Session`，防止同一会话并发执行两个 turn。
2. 触发 Session/Prompt Hook 并持久化用户消息。
3. 按当前项目状态重新组合 System Prompt。
4. 调用 Provider，持久化模型开始、文本增量、完整输出和 token 使用量。
5. 处理原生 `ToolCall`，经过参数校验、Hook、审批、工具执行和后置观察。
6. 在已识别错误路径产生 `ERROR` 事件，并在所有正常、错误、异常或取消出口的 `finally`
   中把 Session 状态恢复为 `idle`，同时释放并发标记。

SQLite 保存会话、事件、工具轨迹、审批结果、文件变更、记忆、Cron、团队任务、邮箱和插件
状态。CLI 与 Web UI 消费同一套事件，而不是各自维护不可审计状态。

会话恢复现已保留：

- 用户消息与模型文本。
- 工具名称、`call_id`、参数和 Observation。
- 多工具调用的 `action → observation` 原始顺序。
- 工具返回图片的媒体类型和 Base64 数据。
- 最后一次压缩摘要之后的原始事件。

工具内部产生的 `TOOL_REQUESTED` 与 `APPROVAL_RESOLVED` 不再只写数据库，也会进入
`run_turn()` 对外事件流。

### Prompt 组合与上下文压缩

`PromptComposer` 按固定顺序组合：

1. 基础安全规则。
2. `general`、`study` 或 `code` profile。
3. 当前运行环境与项目根目录信息。
4. 用户级和目录级指令文件。
5. Memory 人工可审计索引。
6. Skill 名称与描述目录。
7. 已持久化的会话压缩摘要。

正式指令文件是 `AGENT.md` 与 `AGENT.local.md`；同时只读兼容 `AGENTS.md`、
`CLAUDE.md`、`.claude/skills` 和 `.claude/agents`。兼容来源只作为文本或候选组件发现，
不会自动信任可执行内容。

当上下文事件超过上限时，Runtime 调用零温度摘要并保留最近消息。`BeforeCompact` Hook
可以拒绝压缩；拒绝后不会调用摘要模型、更新 Session、触发 `AfterCompact` 或伪造
`COMPACTED` 事件。

### 文件变更记录与安全 rewind

内置 `write_file` 及委托它的 `apply_patch` 会记录：

- 绝对规范路径。
- 写入前后字节快照。
- 写入前后 SHA-256。
- 所属 Session 和最近事件序号。

rewind 不调用 `git reset`，其流程为：

1. 第一遍只读取并验证全部目标的当前哈希。
2. 任意路径发现用户或外部进程同期修改时整体停止，不先修改其他文件。
3. 同一路径多次修改按“新到旧”计算最终恢复快照。
4. 文件通过同目录临时文件、`fsync` 与 `os.replace` 做单文件原子替换。
5. SQLite 的变更标记、事件截断、摘要恢复和 `REWOUND` 审计事件在同一事务提交。
6. 文件写入或数据库提交异常时，使用预检阶段保存的原字节补偿已经处理的路径。

因此 rewind 只影响明确进入 `record_file_change()` 的变更，并在可检测冲突或执行异常时
安全停止。可写 Shell、浏览器截图和第三方/自定义工具产生的文件不会自动纳入 rewind。

### 模型适配层

`model_choice` 现在包含两条清晰路径：

- `ModelClient`：供旧同步 Agent 和简单脚本使用。
- `LegacyModelProvider`/`FallbackModelProvider`：供异步 Runtime 使用。

支持的默认 Provider：

| 别名 | API 风格 | 默认密钥环境变量 |
| --- | --- | --- |
| `gpt` | OpenAI Responses | `OPENAI_API_KEY` |
| `claude` | Anthropic Messages | `ANTHROPIC_API_KEY` |
| `deepseek` | OpenAI-compatible Chat Completions | `DEEPSEEK_API_KEY` |
| `glm` | OpenAI-compatible Chat Completions | `ZHIPU_API_KEY` |
| `qwen` | OpenAI-compatible Chat Completions | `DASHSCOPE_API_KEY` |
| `kimi` | OpenAI-compatible Chat Completions | `MOONSHOT_API_KEY` |

Provider 优先使用原生工具调用；不兼容的网关回退到严格 JSON Action。备用 Provider 在当前
消息状态切换，不要求 Runtime 从头重复已经完成的有副作用工具。`model_choice/example.py`
已改为显式 `main()`，普通 import 不再意外发起真实网络请求或消耗 API 配额。

### 工具注册与内置能力

异步 `ToolRegistry` 统一管理工具名称、JSON Schema、风险级别和沙箱声明。默认工具覆盖：

- 工作区文件分段读取、目录列举、`rg` 文本搜索、原子写入和补丁。
- Git status、diff、log 等只读操作。
- 计算器、当前时间、用户提问和会话任务列表。
- Web 搜索/抓取、一次性 Playwright 页面文本或截图。
- Docker Shell。
- Windows UI Automation 窗口列举、控件点击、文本输入和截图。
- Memory、Corpus、Skill、Cron、Subagent、MCP 与 LSP 扩展工具。

文件策略解析真实路径并拒绝符号链接逃逸、工作区外访问、敏感文件名和超大读取；写入采用
同目录临时文件原子替换，并登记 rewind 快照。Shell 始终使用 argv 数组，不使用
`shell=True` 解释元字符。

Web 抓取只接受 HTTP(S)，检查域名 allowlist、DNS 解析、重定向目标和最终 IP，拒绝环回、
私网、链路本地、保留地址以及标准/旧式 IP 字面量。浏览器和桌面能力属于更高风险边界。

### 权限、审批与能力包

四种权限模式：

| 模式 | 实际行为 |
| --- | --- |
| `plan` | 仅允许被动读取、检索和计算；写 SQLite 的 task create/update 也不会自动放行 |
| `review-all` | 每个工具调用都请求审批 |
| `risk-based` | 低风险被动读取自动允许，其余请求审批 |
| `accept-sandboxed` | 沙箱内且未命中硬拒绝/关键规则的调用自动允许；主机调用仍审批 |

规则按固定的 deny → ask → allow 顺序匹配，并支持本次、Session、项目和用户作用域。规则
采用工具名与参数精确子集匹配，不允许模型输出自行授予权限。桌面动作、可写 Shell 等
critical 调用不能通过持久 allow 或后台能力包跳过最终确认。

`CapabilityGrant` 固定后台任务可使用的工具、路径、域名、命令前缀，以及创建时的完整
插件能力快照。新快照同时冻结启用插件 ID 集合、每个插件的整树内容哈希和规范化后的信任
组件集合；新增、禁用、内容变化或信任变化中的任一种都会在 Runtime 调用模型或插件 Hook
之前产生 `needs_approval`。旧任务的 `plugin_versions` 字段仍可读取，但只能执行旧格式能够
表达的哈希校验；新 Cron 一律写入完整快照。能力包继续传入子代理并与子代理工具白名单
求交，后台调用越界时只能停止或进入待审批状态，不能扩大自身能力。

Git worktree 创建现在拥有独立的 `git_worktree_add` 高风险审批；即使 Python 调用方直接
调用 `run_subagent()`，没有审批回调也会 fail-closed，不会绕过外层工具权限。

### Hook 安全链

支持 command、HTTP、prompt、agent 四类 Hook。可配置事件名称覆盖 Session、Prompt、Model、
Tool、Permission、Compact、Memory、Subagent、Team、Cron 与 Stop；当前 Runtime 主循环实际
触发 SessionStart、UserPromptSubmit、Model、Tool、Compact、MemoryWrite 和 Subagent 相关
事件，其余名称是供调度器或后续集成使用的扩展表面，尚不会全部由主循环自动触发。

工具执行顺序固定为：

```text
首次 Schema 校验
→ PreToolUse
→ Hook 改写后的二次 Schema 校验
→ 审批参数投影
→ PermissionBroker
→ 工具执行
→ PostToolUse
```

Hook 不能提升权限。`PreToolUse` 改写后的参数必须仍为 JSON 对象并再次通过完整 Schema；
审批参数投影只能补充真实 command、URL 和配置哈希，不能删改最终执行参数。prompt/agent
Hook 的 `allow` 只接受真正 JSON boolean，字符串 `"false"`、数字或 `null` 均 fail-closed。

命令 Hook 默认进入 Docker；HTTP Hook 受允许域名、超时和输出大小限制；所有 Hook 有递归
深度限制。当前真正把 `allowed=False` 作为前置拒绝的主循环事件是 UserPromptSubmit、
BeforeModel、PreToolUse、BeforeCompact、MemoryWrite 和 SubagentStart；PostToolUse 只追加
Observation，SessionStart、AfterModel、SubagentStop 等后置结果不改变既有行为。HTTP Hook
当前把返回 JSON 当作 payload 合并，不把其中的 `allow` 当拒绝决定。每个处理器结果尚未
单独持久化为 `EventType.HOOK` 审计事件。

### Docker、MCP 与 LSP 隔离

`DockerSandbox` 统一构造：

- `--rm --init --read-only` 临时容器。
- 删除 Linux capabilities，启用 `no-new-privileges`。
- 限制 PID、内存、CPU 和临时目录。
- 工作区默认只读挂载。
- 默认 `--network none`。
- 仅传递 PATH、SystemRoot、临时目录、locale 等白名单环境变量。

Docker 不可用时抛出 `SandboxUnavailable`，不会静默在主机执行原命令。

stdio MCP 和 LSP 现在强制由 Docker 包装启动：

- MCP SDK 收到的命令是 `docker run`，不是原始第三方命令。
- LSP 进程同样通过容器启动，初始化根 URI 使用 `/workspace`。
- 显式组件环境变量只把键名放入 argv，值通过最小宿主子进程环境传递。
- 未授权的 API Key、云凭据和用户环境变量不会继承给第三方进程。
- 每个 MCP/LSP 配置生成稳定授权描述，包含 transport、真实 command/URL、args、镜像、
  能力标志和完整配置哈希，但不在审批事件中暴露 secret/header 值。
- 同名服务器配置发生变化后，`config_hash` 改变，旧精确 allow 不能静默复用。

通过 Runtime 的 `mcp_call` 工具调用远程 MCP HTTP/SSE 时会经过高风险审批。所有远程
HTTP、Streamable HTTP 和 SSE 调用（包括直接使用 CLI/Python 管理器）都会在导入 MCP SDK
和创建连接前复用 Web 公网 URL 校验，拒绝 localhost、标准/旧式 IP 字面量，以及解析到
环回、私网、链路本地或保留地址的域名。直接执行 `yy-agent mcp call` 或 Python
`MCPManager.call_tool()` 仍被视为操作者显式调用，不再经过 PermissionBroker；用户/项目
`.mcp.json` 也尚无独立来源信任记录。插件 MCP 仍只有在组件受信任后才会载入。MCP SDK
属于可选依赖。

### Memory 与学习资料库

长期 Memory 与 Corpus 使用不同表和不同检索入口，避免学习资料正文污染用户事实：

- Memory 保存 scope、正文、来源、置信度、创建/更新时间和 active 状态。
- StateStore 初始化要求 SQLite 支持 FTS5；缺少扩展时会明确失败。Memory 的 MATCH/bm25
  查询在执行阶段异常时有受控 LIKE 降级，Corpus 当前没有该降级。
- `MEMORY.md` 是可人工查看的索引，详细内容保存在本机项目状态目录。
- 支持 search、show、edit、forget、export；forget 使用可审计软删除。
- Runtime 完成任务后可产生候选记忆，并进行规范化与去重。
- Corpus 独立索引 PDF、Markdown、TXT 和 HTML。
- 文档使用文件哈希避免重复，分块保留路径、标题、页码和章节。
- PDF 搜索结果携带页码；HTML 先做受控文本抽取。

`sqlite-vec` 当前只是可选适配器预留；默认召回路径仍为 FTS5。

### Skills、插件与市场

Skill 注册器发现：

- 用户目录 `~/.yy/skills/`。
- 项目目录 `.yy/skills/`。
- 已安装且允许发现文本的插件目录。
- 只读兼容 `.claude/skills/`。

发现阶段只常驻 name、description、路径和来源；选中后才加载 `SKILL.md` 正文。当前注册器
不会自动加载 references/assets，也不会执行 Skill scripts；需要这些资源的调用方必须在
选中 Skill 后显式、安全地读取或经受控工具执行。

Git 安装接口支持：

```powershell
yy-agent skill add "<git-url>#<subdir>" --ref <tag-or-sha> --scope project
yy-agent skill update <name> --scope project
yy-agent skill remove <name> --scope project
```

Marketplace 自身支持 local、GitHub shorthand 和 Git URL；catalog 固定查找市场根目录的
`.yy-plugin/marketplace.json` 或 `.claude-plugin/marketplace.json`。catalog 内的插件 source
另支持 local、GitHub、Git URL、git-subdir 和 npm，npm 获取禁止运行安装脚本。插件包
manifest 固定为 `.yy-plugin/plugin.json` 或 `.claude-plugin/plugin.json`；可发现组件位置为
`skills/`、`agents/`、`hooks/hooks.json`、`.mcp.json` 和 `.lsp.json`，scripts 当前只记录信任
而没有通用执行器。

安装/更新安全修复：

- 在复制前使用 no-follow 扫描拒绝任意 symlink、Windows junction/reparse point 和真实路径逃逸。
- `copytree` 显式保留链接，再在复制后复检，避免默认解引用把外部目标“洗白”为普通文件。
- Skill、Marketplace 和 Plugin 都先物化到目标同级随机 staging。
- 完成最小 manifest/catalog 结构检查、路径树隔离和内容哈希计算后才进行可回滚目录交换；
  当前不是完整正式插件 Schema 或组件清单校验器。
- lock、市场注册表或 SQLite 状态写入失败时恢复旧安装，不留下数据库指向缺失目录。
- Marketplace 更新从登记来源重新 staging，不再对现有缓存原地 `git pull`。
- 安装记录保存来源、ref、commit SHA、版本和内容哈希；内容哈希排除易变 `.git` 元数据。
- 插件内容不变时保留 enabled 与 trusted components，并返回 `trust_reset=False`。
- 整棵插件内容哈希变化时清空已有信任（包括 README/Skill 文本变化）；只有实际撤销非空
  信任时才报告 `trust_reset=True`。
- scripts、Hooks、MCP、LSP 和 agents 不会因安装自动信任，必须显式执行 `plugin trust`。

### Cron 与 Scheduler

调度存储支持标准五字段 Cron、IANA 时区、单次/周期任务、有限重试、有限单任务超时和
能力包。守护进程默认给每次运行 900 秒，可通过 `scheduler start --job-timeout-seconds`
调整，但拒绝零、负数、NaN 和无穷值。
周期任务默认七天到期，除非创建时明确长期有效。过期较久的一次性任务进入
`needs_approval`，不会在机器重新启动后突然补跑。

单任务使用条件 UPDATE 从 `ready` 原子切换到 `running`，防止多守护进程重复领取。守护
进程还维护当前进程的活跃任务 ID 集合；后续轮询会把这些 ID 排除在陈旧租约恢复之外，
因此运行超过五分钟但仍受 900 秒任务超时约束的合法长任务不会被误判为崩溃残留并重叠
执行。任务成功、异常、取消或状态落库失败后都会在 `finally` 清理活跃集合。

Scheduler 单实例修复：

- PID 文件通过 `O_CREAT | O_EXCL` 原子取得，不再使用“exists 后 write”的竞态流程。
- 只能确认旧 PID 已死亡时才删除旧锁；权限不足或未知系统错误均保留锁并 fail-closed。
- POSIX 使用 signal 0 探测；Windows 使用 `OpenProcess`/`GetExitCodeProcess`，避免
  `os.kill(pid, 0)` 在 Windows 上潜在误杀目标进程。
- 退出时只删除内容仍属于当前 PID 的锁文件。
- 守护进程崩溃后遗留的 `running` 任务在五分钟租约到期时转为 `needs_approval`，不会因
  无法判断上次副作用是否完成而自动重跑。
- 只有不属于当前守护进程活跃集合的陈旧任务才会被恢复；所有完成回调也只更新仍为
  `running` 的记录，不能覆盖管理员已经写入的 `needs_approval` 等外部状态。
- CLI runner 使用 `AgentRuntime.run()` 检查完整事件与 `completed`；Runtime ERROR、未完成
  结果和超时会进入一次性失败或周期有限重试，能力越界/插件能力快照变化则通过明确类型
  直接转入 `needs_approval`，不会按普通故障自动重试。

### 子代理与 Agent Teams

子代理采用 Markdown frontmatter，可定义：

- `name`、`description`、独立 prompt。
- model、max turns。
- 工具 allow/deny。
- Skills、Memory scope、background。
- `worktree` isolation。

子代理使用独立 Runtime/Session，向父调用返回其最终答案文本。它继承父权限模式、会话规则、
审批回调和外层 `CapabilityGrant`；工具集合先应用定义 allow/deny，再与后台 grant 求交，
不能替用户批准。worktree 创建经过单独 Git 写审批，并继续受外层 grant 限制，命令使用参数
数组而非 Shell 字符串。

Teams 支持 lead/teammate 概念所需的共享任务表、依赖 DAG、原子 claim、完成结果、邮箱发送
与一次性 receive。并发数受 `max_team_agents` 限制，默认最多四个。没有就绪任务时返回当前
状态，不无限轮询依赖失败的 DAG。

### CLI 与本地 Web UI

Typer/Rich CLI 已覆盖：

| 命令组 | 功能 |
| --- | --- |
| `chat`、`run`、`serve`、`doctor` | 对话、单次任务、本地 Web UI、环境诊断 |
| `session` | 会话列表、事件查看、安全 rewind |
| `memory`、`corpus` | 长期记忆和资料索引 |
| `skill`、`plugin marketplace`、`plugin` | Skill、市场、插件和组件信任 |
| `prompt inspect`、`hooks`、`sandbox` | Prompt 来源、Hook 与隔离状态 |
| `cron`、`scheduler` | 定时任务和单实例守护进程 |
| `agent`、`team` | 子代理和团队 DAG |
| `mcp`、`lsp` | 外部协议配置与受控调用 |
| `auth`、`migrate` | 系统凭据和旧配置迁移 |

交互模式支持 `/memory`、`/plugin`、`/cron`、`/prompt`、`/rewind`、`/loop`、`/help`
和 `/exit`。

Web UI：

- 永远绑定 `127.0.0.1`，不提供远程监听开关。
- 启动时生成访问 token 和独立 CSRF token。
- HTTP 使用 token 校验，状态改变请求还必须携带 CSRF header。
- 使用安全响应头和禁止缓存策略。
- WebSocket 传输与 CLI 相同的 `RunEvent`。
- 提供聊天、运行状态、Memory/Corpus 查询、Session 事件、审批队列和用户问题队列接口。

当前页面是轻量本地聊天与审批界面；Skills、插件、Hooks 和 Cron 只通过状态接口展示，尚未
提供与全部 CLI 命令等价的写管理面板。内置页面按钮只提供本次允许/拒绝；底层审批 POST API
接受完整 `ApprovalDecision` 枚举，持有 token 与 CSRF 的自定义本机客户端可以提交持久决定。

`run_ui/console.py` 继续保留无第三方 UI 依赖的旧同步 Agent 动画界面。

### 中文注释与文档覆盖

本轮不仅注释 `Agent/`，而是覆盖全部一方 Python 代码：

- 50 个 Python 文件。
- 131 个类。
- 528 个函数、方法和嵌套函数。
- 所有模块、类和函数均有包含中文的 docstring。
- 复杂安全、事务、状态迁移、协议适配和平台差异均补充中文行内注释。
- AST 审计结果：缺失 docstring 为 0，非中文 docstring 为 0，非中文业务注释为 0。
- TOML、GitHub Actions YAML、INI 模板和 `.gitignore` 的说明性注释也已中文化。
- 标准 JSON 不支持注释，因此 `.yy/*.json` 保持合法 JSON，通过 Schema、README 和代码说明
  字段语义，没有写入会破坏解析的伪注释。

### 安全审计后追加的实现修复

| 原问题 | 修复结果 |
| --- | --- |
| Skill/插件复制后才检查 symlink，可能已被 `copytree` 解引用 | 复制前后 no-follow 检查、拒绝 junction，并采用 staging |
| 插件更新失败可能先删除旧缓存 | 完整验证和状态写入成功后交换；失败恢复旧目录 |
| 插件内容未变却错误清空信任或误报 `trust_reset` | 内容不变保留状态；内容变化才按实际信任撤销结果报告 |
| Hook 改写参数后未再次做 Schema 校验 | 审批前二次完整校验，非法改写不触发审批或工具 |
| Hook 对字符串 `"false"` 使用 `bool()` 后变为允许 | `allow` 仅接受真正 JSON boolean，其他类型 fail-closed |
| `BeforeCompact` deny 被忽略 | 拒绝后不摘要、不写状态、不触发后置 Hook/事件 |
| stdio MCP 继承完整 `os.environ` | Docker 启动，宿主环境白名单化，仅加入显式配置键值 |
| LSP 直接启动主机进程 | 强制 Docker 包装，Docker 不可用时拒绝 |
| MCP/LSP 审批只显示逻辑 server 名 | 审批加入真实 command/URL/args/config hash，配置变化使旧授权失效 |
| Scheduler PID “检查后写”可竞态 | 使用独占原子创建和跨平台无副作用 PID 探测 |
| 崩溃遗留 `running` Cron 永久卡住 | 租约过期转 `needs_approval`，避免静默重复副作用 |
| rewind 多文件写入异常可能部分完成 | 单文件原子替换、反向补偿和单事务状态提交 |
| 会话恢复丢失工具参数和图片 | 持久化并恢复 call/action/observation/image 完整上下文 |
| 内部请求/审批事件只入库不进入事件流 | `run_turn()` 同步发布已持久化内部事件 |
| 直接调用 `run_subagent()` 可绕过 worktree 审批 | worktree 使用独立高风险授权，缺少回调时安全拒绝 |
| 示例模块 import 即联网 | 网络调用移动到显式 `main()` 入口 |
| task create/update 被误列为被动只读 | 从 plan/risk-based 自动放行列表移除 |
| `plan` 可被既有持久 `allow` 扩大为写权限 | 在匹配 allow/grant 之前执行不可扩大的只读模式上限 |
| 持久 `ask` 规则可能继续落入低风险自动允许 | `ask` 先于模式规则并强制调用审批；无回调时 fail-closed |
| Web 允许公网 IP 字面量绕过域名策略 | 拒绝标准及旧式 IPv4/IPv6 字面量，只允许经校验域名 |
| 远程 MCP 可绕过 Web 地址安全检查访问本机/内网 | SDK 导入和连接前统一执行公网 URL、DNS 与 IP 校验 |
| Web token/CSRF 页面可被浏览器缓存 | 所有 HTTP 响应加入 `no-store`、Pragma 与 Expires |
| Runtime 错误/取消后 Session 状态未统一清理 | 所有出口在 `finally` 恢复 `idle` 并释放会话锁 |
| 子代理未继承外层后台能力包 | 传递会话规则和 CapabilityGrant，工具白名单取交集，越权上报待审批 |
| Cron 只消费事件流却把 ERROR/未完成误判为成功 | 聚合并检查结果，增加 900 秒默认超时和类型化待审批状态 |
| 长 Cron 超过五分钟后可能被租约恢复并重叠执行 | Daemon 传递活跃 ID 快照；陈旧恢复和迟到完成均使用条件状态更新 |
| CapabilityGrant 的插件固定字段未完整执行 | 冻结启用集合、整树哈希和信任组件，任何差异都在模型调用前暂停 |

### 测试与验收结果

当前确定性测试文件：

| 文件 | 覆盖重点 |
| --- | --- |
| `tests/test_harness.py` | 配置、同步 API 分层/兼容与 ReAct 行为、冷导入、权限优先级、URL/IP、Memory/Corpus、Runtime、rewind、Cron、Teams |
| `tests/test_runtime_hook_security.py` | Hook 二次校验、boolean fail-closed、压缩拒绝、审批投影、错误清理和插件能力快照 |
| `tests/test_integration_security.py` | Docker 包装、最小环境、MCP/LSP fail-closed、远程 MCP 公网 URL 和授权描述 |
| `tests/test_scheduler_security.py` | PID 锁、活跃/陈旧任务区分、状态竞态、有限超时、Runtime 成败判定和待审批暂停 |
| `tests/test_skill_registry_security.py` | symlink/junction、staging 回滚、信任保留/重置和状态写入失败 |
| `tests/test_subagent_security.py` | 直接调用不能绕过 worktree 审批，子代理继承后台 CapabilityGrant |

已执行：

```powershell
python -m compileall -q Agent model_choice tools memory skills prompt run_ui tests run.py
python -B -m unittest discover -s tests -v
```

结果：

- 共发现并运行 59 项测试：58 项通过，1 项按条件跳过。
- 跳过项是 Git 原生 symlink 测试；当前 Windows 账户缺少“创建符号链接”权限。
- Windows junction/reparse point 逃逸测试实际通过。
- 新旧 API 别名对象身份测试通过。
- 多组全新解释器冷启动导入顺序测试通过。
- 39 个不依赖可选 UI 包的核心模块冷导入通过。
- TOML、JSON、INI 解析通过。
- `git diff --check` 通过，全部项目文本尾随空白为 0。

当前机器尚未安装 `pytest`、Typer/Rich/FastAPI 完整依赖，也未安装 Docker，因此本地未运行
pytest runner、真实 CLI/Web、真实容器、Playwright、MCP Server 和 Windows UI Automation
smoke test。CI 会执行 `pip install -e ".[dev]"` 后在 Windows/Ubuntu、Python 3.10/3.12
运行 pytest，并把 `GITHUB_TOKEN` 权限收紧为只读仓库内容；真实外部集成仍应在具备对应
软件和凭据的环境单独验证。

### 当前已知边界

- Provider API 已异步包装，但当前网络请求完成后才生成文本事件；`MODEL_DELTA` 是固定块前端
  增量，不是传输层逐 token 流。
- 交互 CLI 没有手动 `/compact`；当前只会按 `context_event_limit` 自动压缩。
- Hook 名称全集是扩展接口，不代表主循环已经触发全部生命周期；Hook 处理器结果也尚无独立
  的持久审计事件类型。
- 浏览器工具当前是单次页面文本或截图，不是长期交互式浏览器会话。
- Web 与远程 MCP 的 DNS 校验能拒绝已解析的私网和 Web 重定向目标，但底层客户端实际连接
  时可能再次解析 DNS；应用层检查不能等同于受控代理提供的强 DNS rebinding 隔离。
- Windows 桌面工具支持窗口/控件级操作，但真实 UI Automation 依赖可选 `desktop` extra。
- `sqlite-vec` 尚未接入默认召回流程，缺失时始终使用 FTS5。
- `AgentDefinition.skills` 和 `background` 当前完成了解析但尚未驱动子代理执行；worktree 尚无
  自动清理、合并、提交或推送。Teams 也尚未实现创建前确认和默认并行 worktree。
- 插件 scripts 可记录信任状态，但 Runtime 目前只消费受信任 hooks、agents、MCP 和 LSP，
  没有通用插件脚本执行器。
- `SandboxConfig.enabled`、`fail_if_unavailable` 与 `deny_read` 仍有未完全接入的声明字段；当前
  有效保护主要来自 Docker fail-closed、最小环境和工具自身的 `PathPolicy`。
- Corpus 的 Markdown/HTML 章节解析、整次索引事务和向量召回仍待完善。
- Web UI 尚不是 CLI 的全部功能等价面板；`hooks`/`sandbox`/`lsp` CLI 目前也分别以状态查看、
  沙箱诊断和 LSP 列举为主。
- Docker、Playwright、真实模型、MCP 服务和 Language Server 均属于外部运行条件；核心测试
  不会自动下载它们或持有用户凭据。
- 本次上传只包含源码、共享配置、Schema、示例、测试和文档；`config.ini`、本地状态数据库、
  `.yy/settings.local.json`、缓存和虚拟环境不会提交到 GitHub。

### GitHub 提交范围

本轮提交目标为：

```text
remote: https://github.com/Ever-ww/Yuan_Ye_Study_Agent.git
branch: main
version: 0.2.0
date: 2026-07-17
```

上传前必须再次执行敏感文件检查、全量测试和 `git diff --check`。推送成功后的实际 commit
SHA 以 Git 历史为准；本文不写入“包含自身内容的提交哈希”，避免产生无法自洽的循环修改。
