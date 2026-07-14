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

执行模型前，需要在根目录 `config.ini` 填写对应模型的 API Key，或设置该供应商的环境变量。

## 目录职责

```text
Agent/          Agent 实体、ReAct 循环与工具注册表
model_choice/   多模型统一适配层和配置加载器
tools/          所有可供 Agent 调用的具体工具
run_ui/         动态命令行运行界面
config.ini      本机模型配置（Git 忽略）
config.ini.example  可提交的配置模板
run.py          项目启动入口
```

## 模型配置

根目录 `config.ini` 负责选择模型及供应商连接信息：

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

## 创建 Agent

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

## ReAct 执行流程

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

所有具体工具必须放在根目录 `tools/`。工具需实现 `tools.Tool` 协议：`name`、`description`、`parameters` 和 `run(arguments)`。

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

然后在 `run.py` 的 `create_agent()` 中注册：

```python
tools=[CalculatorTool(), CurrentTimeTool(), MyTool()]
```

新增工具时应限制输入、权限和访问范围；不要直接暴露任意 Shell 命令或任意文件读写能力。

## 动态 CLI

`run_ui/console.py` 使用线程在后台运行 Agent，并在主线程显示状态动画，结束后输出最终回答和工具轨迹。运行界面不依赖第三方库，兼容 Windows 默认终端编码。

## 后续迭代建议

- 新增工具：放入 `tools/`，并在 `run.py` 注册。
- 新增模型供应商：在 `model_choice/config.py` 增加供应商默认配置和 API 适配逻辑。
- 增加 Agent 配置：先从 `AgentConfig.from_dict()` 的 `extras` 读取；稳定后再提升为经过校验的核心字段。
- 引入记忆、检索或任务规划时：保持 `Agent` 的对外初始化接口不变，将实现拆分为独立模块。
