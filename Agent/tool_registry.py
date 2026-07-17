"""旧同步 ReAct API 使用的工具注册表。

它只接受实现 :class:`tools.base.Tool` 协议的同步工具。新 Harness 的异步工具包含风险、
沙箱与 ``ToolContext``，必须使用 :class:`tools.harness.ToolRegistry`，两者不能混用。
"""

from __future__ import annotations

from typing import Any

from tools import Tool


class ToolRegistry:
    """旧同步工具的名称注册表和可反馈给模型的调用边界。"""

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


__all__ = ["ToolRegistry"]
