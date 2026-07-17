"""旧同步 ``Agent`` 的高层组装入口。

本模块只保留历史同步 API 的组装职责：解析旧配置、创建同步工具注册表、驱动
:class:`Agent.react_agent.ReActAgent`，并在未完成时按旧约定切换备用模型。异步 Harness
请使用 :class:`Agent.runtime.AgentRuntime`；两条调用链刻意隔离，避免把同步重试语义带入
具有副作用的新工具执行流程。
"""

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


__all__ = ["Agent", "AgentConfig", "AgentResult", "ReActAgent", "ToolRegistry"]
