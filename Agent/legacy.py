"""旧版同步 ReAct Agent 的集中兼容实现。

本模块保留项目早期公开的同步调用方式，供已有脚本继续使用。它与
:class:`Agent.runtime.AgentRuntime` 的异步事件驱动架构相互独立：前者依赖模型输出
严格的 JSON ``action`` 协议，后者优先使用模型原生工具调用、权限审批和事件存储。

将旧实现集中在一个模块有两个目的：

* 明确兼容边界，避免同步 ``ToolRegistry`` 与异步 ``tools.harness.ToolRegistry``
  因同名而被误用；
* 保持 ``Agent.agent``、``Agent.react_agent`` 和 ``Agent.tool_registry`` 原导入路径
  可用，同时消除三个实现模块之间不必要的相互依赖。

这里不会主动升级旧 API 的行为。尤其需要注意，备用模型会从头重新执行一次任务，
因此旧接口不适合包含不可重复副作用的工具；需要严格防重、审批、回滚或会话恢复时，
应改用异步 ``AgentRuntime``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from model_choice import ModelClient
from model_choice.settings import load_settings
from tools import Tool


# 旧同步循环要求模型每轮只返回一个 JSON 对象。双花括号是 ``str.format`` 的
# 转义写法，最终发送给模型时会还原成普通的 JSON 花括号。
REACT_PROTOCOL_PROMPT = """你使用 ReAct 循环完成任务。
请在内部完成推理，且只能输出一个 JSON 对象，不要使用 Markdown 代码块或额外文本。
可用工具如下：
{tools}

当需要工具时输出：{{"action":"工具名","action_input":{{...}}}}
当已可回答时输出：{{"action":"final","final":"给用户的最终回答"}}
每次工具执行后，用户会发送 Observation。请基于它决定下一步。"""


@dataclass(frozen=True)
class AgentConfig:
    """旧同步 Agent 的运行配置。

    ``max_steps`` 和 ``temperature`` 是旧执行器直接消费的稳定字段；其他字段会被
    原样保存在 ``extras`` 中，以兼容历史配置及调用方自定义扩展。数据类被冻结，
    避免运行途中被意外修改而导致主模型与备用模型采用不同参数。
    """

    max_steps: int = 6
    temperature: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """在配置创建时尽早拒绝模型接口无法接受的取值。"""

        if self.max_steps < 1:
            raise ValueError("max_steps 至少为 1")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature 必须介于 0 和 2 之间")

    @classmethod
    def from_dict(cls, values: Mapping[str, Any] | None = None) -> "AgentConfig":
        """从宽松映射创建配置，并把未知键合并到 ``extras``。

        显式 ``extras`` 与顶层未知字段同时出现时，顶层字段优先。这一规则与旧版
        行为保持一致，也便于调用方用单个顶层参数临时覆盖已打包的扩展配置。
        """

        data = dict(values or {})
        known = {key: data.pop(key) for key in ("max_steps", "temperature") if key in data}
        supplied_extras = data.pop("extras", {})
        if supplied_extras is not None and not isinstance(supplied_extras, Mapping):
            raise ValueError("extras 必须是字典类型")
        return cls(**known, extras={**dict(supplied_extras or {}), **data})

    def get(self, key: str, default: Any = None) -> Any:
        """以类似字典的方式读取核心字段或扩展字段。"""

        if key in {"max_steps", "temperature"}:
            return getattr(self, key)
        return self.extras.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        """导出适合序列化的扁平字典；扩展字段保持原值。"""

        return {"max_steps": self.max_steps, "temperature": self.temperature, **self.extras}


class ToolRegistry:
    """旧同步 :class:`tools.base.Tool` 的名称注册表和安全调用边界。

    该类只用于 ``ReActAgent``。异步 Harness 工具带有风险级别、沙箱标记和
    ``ToolContext``，应使用 ``tools.harness.ToolRegistry``，不能注册到这里。
    """

    def __init__(self, tools: list[Tool] | None = None) -> None:
        """按传入顺序注册工具；重复名称立即报错而不是静默覆盖。"""

        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """注册单个同步工具，并维持“名称全局唯一”的不变量。"""

        if tool.name in self._tools:
            raise ValueError(f"工具已注册：{tool.name}")
        self._tools[tool.name] = tool

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        """执行工具并把常见参数错误转换为可反馈给模型的 Observation。

        未知工具以及 ``KeyError``、``TypeError``、``ValueError`` 属于模型可修正的
        输入问题，因此返回文本而不是中断 ReAct 循环。其他异常不在这里吞掉，避免
        隐藏程序缺陷或不可恢复的系统错误。
        """

        tool = self._tools.get(name)
        if not tool:
            return f"工具不存在：{name}。可用工具：{', '.join(self._tools)}"
        try:
            return tool.run(arguments)
        except (KeyError, TypeError, ValueError) as exc:
            return f"工具 {name} 执行失败：{exc}"

    def prompt_schema(self) -> list[dict[str, Any]]:
        """生成嵌入旧 ReAct System Prompt 的轻量工具描述。"""

        return [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in self._tools.values()
        ]


@dataclass(frozen=True)
class Step:
    """一次旧式工具决策及其 Observation 的不可变审计记录。"""

    index: int
    action: str
    action_input: dict[str, Any]
    observation: str


@dataclass(frozen=True)
class AgentResult:
    """旧同步调用结果；不要与异步运行时的 ``types.AgentResult`` 混淆。"""

    answer: str
    steps: list[Step]
    completed: bool


class ReActAgent:
    """执行旧版 Reason → Act → Observation 同步循环。

    每轮把模型输出解析为单个决策：``final`` 直接结束，其他 ``action`` 则调用工具
    并将 Observation 追加到消息历史。达到 ``max_steps`` 后返回未完成结果，调用方
    可据此决定是否启用备用模型。
    """

    def __init__(
        self,
        client: ModelClient,
        tools: ToolRegistry,
        *,
        system_prompt: str = "你是一个有帮助的助手。",
        max_steps: int = 6,
        temperature: float = 0,
    ) -> None:
        """保存模型和工具依赖，并校验循环至少允许执行一轮。"""

        if max_steps < 1:
            raise ValueError("max_steps 至少为 1")
        self.client = client
        self.tools = tools
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.temperature = temperature

    def run(self, task: str) -> AgentResult:
        """同步运行任务，返回最终答案和完整工具步骤。"""

        # 工具 Schema 固定在本次任务的首条系统消息中，使模型每轮看到相同协议。
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"{self.system_prompt}\n\n"
                    f"{REACT_PROTOCOL_PROMPT.format(tools=json.dumps(self.tools.prompt_schema(), ensure_ascii=False))}"
                ),
            },
            {"role": "user", "content": f"任务：{task}"},
        ]
        steps: list[Step] = []
        for index in range(1, self.max_steps + 1):
            model_output = self.client.chat(messages, temperature=self.temperature).content
            decision = self._parse_decision(model_output)
            action = decision.get("action")
            if action == "final":
                final = decision.get("final")
                if isinstance(final, str) and final.strip():
                    return AgentResult(final, steps, True)
                # 协议错误作为 Observation 回传，让模型有机会在下一轮自行修正。
                observation = "final 必须是非空字符串，请重新按 JSON 协议输出。"
            elif isinstance(action, str):
                arguments = decision.get("action_input", {})
                if not isinstance(arguments, dict):
                    observation = "action_input 必须是 JSON 对象。"
                    arguments = {}
                else:
                    observation = self.tools.run(action, arguments)
                steps.append(Step(index, action, arguments, observation))
            else:
                observation = "缺少 action 字段，请按协议输出 JSON。"

            # 保留原始模型输出，避免模型看不到自己上一轮的格式错误。
            messages.extend(
                [
                    {"role": "assistant", "content": model_output},
                    {"role": "user", "content": f"Observation: {observation}"},
                ]
            )
        return AgentResult("已达到最大执行轮数，任务尚未完成。", steps, False)

    @staticmethod
    def _parse_decision(model_output: str) -> dict[str, Any]:
        """容错解析模型决策，兼容偶尔出现的 Markdown JSON 代码块。

        解析失败或顶层不是对象时返回空字典，由主循环生成协议纠错 Observation；
        这比在此抛异常更适合可自我修正的 ReAct 对话。
        """

        text = model_output.strip()
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
        if match:
            text = match.group(1)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


class Agent:
    """旧同步 ReAct Agent 的高层组装入口。

    ``model`` 接受 ``model_choice`` 中的别名或 ``provider:model`` 写法；为 ``None``
    时读取旧 ``config.ini`` 的默认模型。新代码通常应使用 ``AgentRuntime``，本类
    主要用于保持早期脚本和教程可以继续运行。
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        tools: list[Tool],
        model: str | None = None,
        config: AgentConfig | Mapping[str, Any] | None = None,
        client: ModelClient | None = None,
        fallback_client: ModelClient | None = None,
    ) -> None:
        """创建主模型执行器，并延迟构造可能用不到的备用模型客户端。"""

        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        self.system_prompt = system_prompt
        self.model = model
        self.config = config if isinstance(config, AgentConfig) else AgentConfig.from_dict(config)
        self.tools = ToolRegistry(tools)
        self.client = client or ModelClient.from_config(model)
        settings = load_settings()
        self.fallback_model = self.config.get("fallback_model", settings.fallback_model)
        self._fallback_client = fallback_client
        self._runner = ReActAgent(
            self.client,
            self.tools,
            system_prompt=system_prompt,
            max_steps=self.config.max_steps,
            temperature=self.config.temperature,
        )

    def run(self, task: str) -> AgentResult:
        """执行任务；主模型未完成时可从头使用备用模型重试。

        两次执行的步骤索引会合并为连续序列，便于旧调用方统一审计。由于备用模型
        会重新开始任务，此兼容行为可能重复调用有副作用工具，调用方应自行避免。
        """

        if not task.strip():
            raise ValueError("task 不能为空")
        primary_result = self._runner.run(task)
        if primary_result.completed or not self.fallback_model or self.fallback_model == self.model:
            return primary_result

        fallback_client = self._fallback_client or ModelClient.from_config(self.fallback_model)
        fallback_runner = ReActAgent(
            fallback_client,
            self.tools,
            system_prompt=self.system_prompt,
            max_steps=self.config.max_steps,
            temperature=self.config.temperature,
        )
        fallback_result = fallback_runner.run(task)
        # ``replace`` 保留每个不可变 Step 的内容，只调整备用执行轨迹的全局序号。
        combined_steps = primary_result.steps + [
            replace(step, index=len(primary_result.steps) + index)
            for index, step in enumerate(fallback_result.steps, start=1)
        ]
        return AgentResult(fallback_result.answer, combined_steps, fallback_result.completed)


__all__ = [
    "Agent",
    "AgentConfig",
    "AgentResult",
    "REACT_PROTOCOL_PROMPT",
    "ReActAgent",
    "Step",
    "ToolRegistry",
]
