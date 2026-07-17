"""旧同步 ReAct 循环、协议与结果类型。

模块只负责 Reason → Act → Observation 的同步循环，不负责选择备用模型或读取项目配置；
这些组装职责放在 :mod:`Agent.agent`。同步工具名称查找与异常转换则委托给
:mod:`Agent.tool_registry`，使三个旧 API 文件各自只维护一种职责。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from model_choice import ModelClient

from .tool_registry import ToolRegistry


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


__all__ = ["AgentResult", "REACT_PROTOCOL_PROMPT", "ReActAgent", "Step", "ToolRegistry"]
