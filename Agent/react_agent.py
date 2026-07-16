"""旧 ``Agent.react_agent`` 导入路径的兼容转发模块。

ReAct 协议、步骤类型与同步执行器的实现现位于 :mod:`Agent.legacy`。此处仅转发
历史公开符号，不在导入时创建模型客户端或执行任何代码。
"""

from .legacy import AgentResult, REACT_PROTOCOL_PROMPT, ReActAgent, Step, ToolRegistry

# ``ToolRegistry`` 原本作为该模块的导入依赖也可被外部访问，因此一并保留兼容。
__all__ = ["AgentResult", "REACT_PROTOCOL_PROMPT", "ReActAgent", "Step", "ToolRegistry"]
