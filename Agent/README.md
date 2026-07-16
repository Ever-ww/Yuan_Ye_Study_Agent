# Agent 核心包

`Agent/` 是 Yuan Ye Study Agent 的编排层，负责把模型、工具、Prompt、权限、会话存储、Hooks、沙箱、子代理和调度组件连接成一次可审计的 Agent 运行。

该目录同时保留两套 API，但真实实现已经按职责重新整理：

- `Agent`：原有的同步 ReAct API，真实实现集中在 `legacy.py`，仅用于兼容已有调用方。
- `AgentRuntime`：新的异步、事件驱动 Harness，新功能应优先基于它开发。

模型适配器和工具实现并不全部位于本目录。它们分别由 `model_choice/`、`tools/`、`memory/`、`skills/` 和 `prompt/` 提供，`AgentRuntime` 负责组装这些组件。

## 选择哪套 API

新代码应使用 `AgentRuntime`。它支持持久会话、事件记录、权限审批、Hooks、上下文压缩和受控工具执行。

只有在维护旧的同步调用时才使用 `Agent`。旧 ReAct 循环依赖严格 JSON 决策协议，并且备用模型会从头重新执行整个任务；如果自定义工具包含副作用，模型回退可能导致重复执行，因此不要把它用于新的文件编辑、Shell 或联网工作流。

## 异步 Harness 快速开始

先按照项目根目录文档配置模型及 API Key。下面的示例创建一个持久会话，并逐项消费 `run_turn()` 产生的事件：

```python
import asyncio

from Agent import AgentRuntime, load_runtime_config


async def main() -> None:
    config = load_runtime_config(
        overrides={
            "profile": "code",
            "permission_mode": "plan",
        }
    )
    runtime = AgentRuntime(config)
    session = runtime.create_session(title="检查项目结构")

    async for event in runtime.run_turn(
        "概括这个项目的主要 Python 包，不要修改文件。",
        session_id=session.id,
    ):
        data = event.to_dict()
        if data["type"] == "model.delta":
            print(data["payload"]["delta"], end="", flush=True)
        elif data["type"] == "run.final":
            print("\n\n最终结果：", data["payload"]["answer"])


asyncio.run(main())
```

`run_turn()` 是异步事件生成器，适合 CLI、WebSocket/SSE 或自定义前端。当前默认模型适配器先完成一次模型请求，再将返回文本拆成 `model.delta` 事件；这不等同于 Provider 传输层的逐 token 流式响应。

如果只需要最终结果，可以使用聚合接口：

```python
result = await runtime.run("总结当前项目", session_id=session.id)
print(result.answer, result.completed)
```

这里返回的是顶层导出的 `RuntimeResult`。顶层名称 `AgentResult` 保留给旧同步 API，二者字段不同。

### 审批回调

需要审批的工具调用会交给 `approval_callback`。没有提供回调时，运行时会安全拒绝该调用，而不是默认放行：

```python
from Agent import AgentRuntime, ApprovalDecision


async def approve(request):
    print(request.tool, request.risk, request.arguments)
    return ApprovalDecision.ALLOW_ONCE


runtime = AgentRuntime(approval_callback=approve)
```

关键风险操作只能单次允许或拒绝，不能保存为长期放行规则。后台任务使用的 `CapabilityGrant` 也不能授权关键主机或桌面操作。

## 同步兼容 API

同步 API 继续支持原来的 `Agent`、`AgentConfig`、`ReActAgent`、`ToolRegistry` 和基础工具。
推荐从包顶层导入：

```python
from Agent import Agent, AgentConfig
from tools import CalculatorTool, CurrentTimeTool

agent = Agent(
    system_prompt="你是一名严谨的学习助手。",
    tools=[CalculatorTool(), CurrentTimeTool()],
    model="deepseek",
    config=AgentConfig(max_steps=8, temperature=0),
)

result = agent.run("计算 (25 + 17) * 3")
print(result.answer)
for step in result.steps:
    print(step.action, step.observation)
```

`AgentConfig.from_dict()` 会校验 `max_steps` 和 `temperature`，未知字段保存在 `extras` 中：

```python
config = AgentConfig.from_dict(
    {
        "max_steps": 8,
        "temperature": 0,
        "fallback_model": "qwen",
        "trace_enabled": True,
    }
)

assert config.get("trace_enabled") is True
```

注意两组同名概念：

- `Agent.ToolRegistry` 是旧同步注册表，执行实现 `tools.Tool` 协议的工具。
- `tools.AsyncToolRegistry` 是新 Harness 注册表，执行异步工具。
- `Agent.AgentResult` 是旧同步结果。
- `Agent.RuntimeResult` 是新异步运行结果。

### 旧模块路径如何兼容

早期代码把组装入口、ReAct 循环和同步工具表分别放在三个文件。重构后，真实代码统一
位于 `legacy.py`，原文件仅转发公开符号，因此以下导入仍然有效：

```python
from Agent.agent import Agent, AgentConfig
from Agent.react_agent import AgentResult, ReActAgent, Step
from Agent.tool_registry import ToolRegistry
```

兼容转发模块不会创建模型或执行工具，导入没有额外副作用。新功能不要继续写入这三个
文件；若确实需要维护旧同步行为，请修改 `legacy.py` 并同步补充兼容测试。

同理，原 `Agent.agents` 曾同时包含子代理发现和团队持久化。现在真实实现分别位于：

```python
from Agent.subagents import AgentRegistry
from Agent.teams import TeamStore
```

`from Agent.agents import AgentRegistry, TeamStore` 仍可使用，但仅作为迁移期兼容路径。

## 配置、会话与安全边界

### 配置

`load_runtime_config()` 按“默认值 → 用户配置 → 项目共享配置 → 项目本地配置 → 调用方覆盖”的顺序合并设置，后者覆盖前者：

- 用户配置：`~/.yy/settings.json`，也可通过 `YY_AGENT_HOME` 更改用户目录。
- 项目共享配置：`.yy/settings.json`。
- 项目本地配置：`.yy/settings.local.json`。
- 调用方覆盖：`load_runtime_config(overrides={...})`。

`profile` 当前只接受 `general`、`study` 或 `code`。运行状态不写入仓库配置目录，而是存放在 `~/.yy/projects/<project-id>/state.db`。

### 会话与回滚

`StateStore` 使用 SQLite 保存会话、事件、工具轨迹、权限规则、文件变更、记忆、Cron 和团队任务。可将 `session_id` 再次传给 `run_turn()` 或 `run()` 继续会话；同一个会话不能同时运行两个 turn。

`context_event_limit` 最小为 22；超过上限时，运行时会调用模型生成摘要并保留最近 20 条消息。`rewind(session_id, to_seq)` 只回滚通过内置 `write_file`/`apply_patch` 或显式调用 `record_file_change()` 记录的变更，并在当前文件哈希与记录不一致时报告冲突。Shell、浏览器截图、Python、第三方工具或自定义工具写文件不会自动进入回滚日志。

### 权限模式

`PermissionBroker` 实现四种模式：

- `plan`：只允许内置只读/被动工具集合；既有项目/用户 allow 和后台 grant 都不能把它扩大
  为写模式。
- `review-all`：除硬拒绝规则外，每次调用都进入审批。
- `risk-based`：低风险只读工具自动允许，其余调用按风险审批；这是默认模式。
- `accept-sandboxed`：未命中关键风险规则的沙箱内工具自动允许，沙箱外工具仍需审批。

权限匹配会优先执行拒绝规则。工具声明的 `risk` 和 `sandboxed` 只是策略输入，不能绕过硬拒绝、持久拒绝或后台能力包限制。新建 Cron 的 `CapabilityGrant` 还会冻结启用插件集合、整树内容哈希和受信任组件；任一变化都会在模型调用前暂停后台任务。旧 `plugin_versions` 任务保持只读兼容。

### Hooks

`HookEngine` 自动加载项目原生 `.yy/hooks.json`，以及已经信任 `hooks` 组件的插件 Hook 文件。它支持 `command`、`http`、`prompt` 和 `agent` 处理器。可配置的事件名称涵盖 Session、Model、Permission、Tool、Compact、Memory、Subagent、Team、Cron 和 Stop；当前 `AgentRuntime` 会直接触发 SessionStart、UserPromptSubmit、Model、Tool、Compact、MemoryWrite 和 Subagent 相关事件，其余名称留给调度器或前端集成，尚不会由主循环自动触发。

Hook 可以拒绝调用、收窄或改写参数、追加 observation，但不能写入 `approved` 或 `permission` 来提升权限。命令型 Hook 通过 Docker 沙箱运行；HTTP Hook 会检查公网地址和域名 allowlist。兼容来源不会因为被发现就自动执行。

### 沙箱

`DockerSandbox` 默认以只读根文件系统、最小 Linux capabilities、资源限制和清理后的环境变量运行命令。工作区挂载默认只读，网络默认关闭；写挂载和网络都必须在工具参数及权限策略中明确出现。

当前 Shell 和命令型 Hook 在 Docker 不可用时会直接失败。`HostProcessRunner` 只是供显式审批后的调用方使用的底层适配器，`AgentRuntime` 不会自动降级到主机执行。

## 关键模块

| 模块 | 职责 |
| --- | --- |
| `runtime.py` | 主运行循环、工具调用、上下文压缩、自动记忆、回滚、子代理与 Team 编排 |
| `types.py` | Provider 无关的事件、会话、模型、工具和扩展协议 |
| `config.py` | `.yy` 分层配置、运行目录计算和旧 `config.ini` 迁移 |
| `storage.py` | SQLite Schema、事件溯源和文件变更日志 |
| `permissions.py` | 权限模式、审批决策、持久规则和 `CapabilityGrant` |
| `hooks.py` | 生命周期 Hook 的发现、匹配、执行与权限约束 |
| `sandbox.py` | Docker 沙箱和显式主机进程适配接口 |
| `subagents.py` | 发现并解析 Markdown 子代理定义；不负责执行模型 |
| `teams.py` | 团队任务 DAG、原子领取、结果状态和一次性交付邮箱 |
| `scheduler.py` | 五字段 Cron 解析、SQLite 调度记录和守护循环 |
| `integrations.py` | 可选 MCP 客户端和 LSP 子进程管理 |
| `legacy.py` | 原有同步 Agent、ReAct 循环、结果类型和同步工具注册表的真实实现 |
| `agent.py`、`react_agent.py`、`tool_registry.py` | 对 `legacy.py` 的旧导入路径兼容转发 |
| `agents.py` | 对 `subagents.py` 和 `teams.py` 的旧聚合路径兼容转发 |

依赖方向可以概括为：

```text
AgentRuntime (runtime.py)
├── AgentRegistry (subagents.py)
├── TeamStore (teams.py)
├── 权限 / Hook / 沙箱 / 调度 / 存储
└── model_choice、tools、memory、skills、prompt

旧 Agent (legacy.py)
├── 旧 ReActAgent
└── 旧同步 ToolRegistry → tools.Tool
```

两条执行链不互相调用。不要为了复用名称把异步 Harness 工具注册到旧同步注册表，也
不要让旧备用模型重试承担具有不可重复副作用的新任务。

`Agent/__init__.py` 使用懒加载导出公开 API，避免 `Agent`、`tools` 和 `model_choice`
初始化时产生循环依赖。旧顶层符号直接解析到 `legacy.py`，不会额外穿过兼容 shim；
调用方仍应优先从 `Agent` 导入公开类型，而不是依赖内部模块的加载顺序。

## 扩展注意事项

- 自定义模型应实现 `ModelProvider` 的 `complete()` 与 `stream()` 协议；当前主循环调用 `complete()`，不要假设 `stream()` 已被自动消费。
- 自定义 Harness 工具应实现 `AsyncTool`，或继承 `tools.BaseTool`；必须提供准确的 JSON Schema、`risk` 和 `sandboxed`，并返回 `ToolResult`。
- 注册异步工具使用 `runtime.tools.register(tool)`。不要把异步工具交给旧的 `Agent.ToolRegistry`。
- 工具参数会先经过 Schema 校验，再经过 PreToolUse Hook；Hook 改写后的完整参数会在权限审批前进行第二次 Schema 校验。非法改写会安全拒绝，工具实现仍应自行检查运行时边界。
- 自定义文件工具应复用 `tools.PathPolicy`，并在写入时记录变更；否则工作区约束和精确回滚不会自动生效。
- 不要在工具、Hook、MCP 或 LSP 配置中嵌入 API Key。密钥应放在环境变量或受支持的凭据存储中。
- `MCPManager` 依赖可选的 `mcp` 包；LSP 服务器也必须由本机另行安装。配置存在不代表外部服务已经可用或已被信任。
- 兼容目录中的 Agent、Skill、MCP 和 LSP 配置可能会被发现，但可执行组件不应仅因兼容发现而获得信任。

## 相关文档

- [项目安装、CLI 与功能总览](../README.md)
- [模型配置和 Provider](../model_choice/README.md)
- [CLI 与本地 Web 界面](../run_ui/README.md)
- [架构现状与开发参考](../DEVELOPMENT_REFERENCE.md)
