"""旧 ``Agent.agent`` 导入路径的兼容转发模块。

真实实现已集中到 :mod:`Agent.legacy`。保留此文件是为了让
``from Agent.agent import Agent`` 等历史代码无需立即修改；新功能不应继续添加到这里。
"""

from .legacy import Agent, AgentConfig, AgentResult, ReActAgent, ToolRegistry

__all__ = ["Agent", "AgentConfig", "AgentResult", "ReActAgent", "ToolRegistry"]
