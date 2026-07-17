"""旧 ``Agent.agents`` 聚合导入路径的兼容转发模块。

为消除“子代理定义”和“团队任务存储”两个概念混在单文件中的歧义，实现现分别位于
:mod:`Agent.subagents` 与 :mod:`Agent.teams`。已有导入仍可通过本模块继续工作。
"""

from .subagents import AgentRegistry
from .teams import TeamStore
 
__all__ = ["AgentRegistry", "TeamStore"]
