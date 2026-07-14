# ReAct Agent

`Agent` 是可复用的包实体。初始化时传入角色提示词、可调用工具、模型和运行配置：

```python
from Agent import Agent, AgentConfig
from tools import CalculatorTool, CurrentTimeTool

agent = Agent(
    system_prompt="你是一名严谨的学习助手。",
    tools=[CalculatorTool(), CurrentTimeTool()],
    model="deepseek",  # 也可以是 qwen:qwen-plus；None 表示使用 config.ini 默认值
    config=AgentConfig(max_steps=8, temperature=0),
)
result = agent.run("计算 (25 + 17) * 3")
print(result.answer)
```

配置可先用字段数量不固定的字典声明，再交给 `AgentConfig` 解析；`Agent` 也可直接接收该字典。核心字段会被校验，未知字段保留在 `extras`，方便以后增加配置而不改初始化接口：

```python
config = {
    "max_steps": 8,
    "temperature": 0,
    "trace_enabled": True,       # 扩展项
    "memory_window": 12,         # 扩展项
}

agent = Agent(
    system_prompt="你是一名学习助手。",
    tools=[CalculatorTool()],
    model="deepseek",
    config=config,
)

assert agent.config.get("trace_enabled") is True
assert agent.config.extras["memory_window"] == 12
```

也可以显式解析：`agent_config = AgentConfig.from_dict(config)`。`result.steps` 保留每一次工具调用与 Observation，方便调试和审计。

可在项目根目录 `config.ini` 配置备用模型：

```ini
[model_choice]
default_model = deepseek
fallback_model = qwen
```

主模型在 `max_steps` 内未返回最终答案时，Agent 会用备用模型重新执行任务；未设置 `fallback_model` 则直接结束。也可在传给 `Agent` 的配置字典中设置 `fallback_model` 覆盖文件配置。

运行入口在项目根目录：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
python run.py "计算 (25 + 17) * 3，然后告诉我上海现在几点"
```

运行流程：模型决定 Action → `ToolRegistry` 执行受控工具 → Observation 回填给模型 → 模型继续行动或返回 Final。最大执行轮数由 `config.ini` 外的启动参数控制：`python run.py "任务" --max-steps 8`。

所有具体工具位于项目根目录的 `tools/`。添加工具时实现 `tools.Tool` 协议，并在 `run.py` 注册：

```python
tools = ToolRegistry([CalculatorTool(), MyTool()])
```

不要把任意 shell 命令或任意文件读写直接暴露为工具；应为每个工具约束输入、权限和可访问路径。
