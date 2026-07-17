"""旧同步 API 的兼容聚合出口。

早期版本把 ``Agent``、ReAct 循环、结果类型和同步工具注册表全部实现于本模块。为便于
维护，真实代码现已按职责回到 :mod:`Agent.agent`、:mod:`Agent.react_agent` 与
:mod:`Agent.tool_registry`；本文件只保留历史 ``Agent.legacy`` 导入路径及对象身份兼容，
不包含业务逻辑。
"""

from .agent import Agent, AgentConfig
from .react_agent import AgentResult, REACT_PROTOCOL_PROMPT, ReActAgent, Step
from .tool_registry import ToolRegistry

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentResult",
    "REACT_PROTOCOL_PROMPT",
    "ReActAgent",
    "Step",
    "ToolRegistry",
]
