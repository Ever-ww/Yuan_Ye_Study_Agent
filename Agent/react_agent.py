"""ReAct（Reason -> Act -> Observation）循环编排。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from model_choice import ModelClient

from .tool_registry import ToolRegistry

REACT_PROTOCOL_PROMPT = """你使用 ReAct 循环完成任务。
请在内部完成推理，且只能输出一个 JSON 对象，不要使用 Markdown 代码块或额外文本。
可用工具如下：
{tools}

当需要工具时输出：{{"action":"工具名","action_input":{{...}}}}
当已可回答时输出：{{"action":"final","final":"给用户的最终回答"}}
每次工具执行后，用户会发送 Observation。请基于它决定下一步。"""


@dataclass(frozen=True)
class Step:
    index: int
    action: str
    action_input: dict[str, Any]
    observation: str


@dataclass(frozen=True)
class AgentResult:
    answer: str
    steps: list[Step]
    completed: bool


class ReActAgent:
    """Agent 实体使用的内部 ReAct 执行器。"""

    def __init__(self, client: ModelClient, tools: ToolRegistry, *, system_prompt: str = "你是一个有帮助的助手。", max_steps: int = 6, temperature: float = 0) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 至少为 1")
        self.client = client
        self.tools = tools
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.temperature = temperature

    def run(self, task: str) -> AgentResult:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": f"{self.system_prompt}\n\n{REACT_PROTOCOL_PROMPT.format(tools=json.dumps(self.tools.prompt_schema(), ensure_ascii=False))}"},
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

            messages.extend([
                {"role": "assistant", "content": model_output},
                {"role": "user", "content": f"Observation: {observation}"},
            ])
        return AgentResult("已达到最大执行轮数，任务尚未完成。", steps, False)

    @staticmethod
    def _parse_decision(model_output: str) -> dict[str, Any]:
        text = model_output.strip()
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
        if match:
            text = match.group(1)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
