"""对外的 Agent 实体。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from model_choice import ModelClient
from model_choice.settings import load_settings
from tools import Tool

from .react_agent import AgentResult, ReActAgent
from .tool_registry import ToolRegistry


@dataclass(frozen=True)
class AgentConfig:
    """单个 Agent 的运行配置，支持保留任意扩展参数。"""

    max_steps: int = 6
    temperature: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps 至少为 1")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature 必须介于 0 和 2 之间")

    @classmethod
    def from_dict(cls, values: Mapping[str, Any] | None = None) -> "AgentConfig":
        """从不定字段的字典创建配置，未知字段存入 ``extras``。"""
        data = dict(values or {})
        known = {key: data.pop(key) for key in ("max_steps", "temperature") if key in data}
        supplied_extras = data.pop("extras", {})
        if supplied_extras is not None and not isinstance(supplied_extras, Mapping):
            raise ValueError("extras 必须是字典类型")
        return cls(**known, extras={**dict(supplied_extras or {}), **data})

    def get(self, key: str, default: Any = None) -> Any:
        """获取扩展配置项；核心项也可用该方法读取。"""
        if key in {"max_steps", "temperature"}:
            return getattr(self, key)
        return self.extras.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        """将核心参数和扩展参数还原为普通字典。"""
        return {"max_steps": self.max_steps, "temperature": self.temperature, **self.extras}


class Agent:
    """可实例化的 ReAct Agent。

    参数 ``model`` 使用 model_choice 的别名或 ``provider:model`` 写法；传入
    ``None`` 时采用项目根目录 config.ini 的默认模型。
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
        """执行任务并返回最终答案和可审计的工具轨迹。"""
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
        combined_steps = primary_result.steps + [
            replace(step, index=len(primary_result.steps) + index)
            for index, step in enumerate(fallback_result.steps, start=1)
        ]
        return AgentResult(fallback_result.answer, combined_steps, fallback_result.completed)
