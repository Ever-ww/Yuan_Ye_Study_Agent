"""Agent 核心不变量与正常执行边界异常。"""


class AgentInvariantError(RuntimeError):
    """核心层或 Hook 破坏正式数据契约，属于可诊断代码缺陷。"""


class AgentExecutionLimitError(RuntimeError):
    """模型在允许的 ReAct 步数内没有完成，属于运行边界而非代码缺陷。"""
