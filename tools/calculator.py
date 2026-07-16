"""旧版同步 ReAct 循环使用的受限算术计算器。

实现不会将模型生成的表达式交给 ``eval``，而是先解析为
Python AST，再递归执行明确列入白名单的节点。这使属性访问、函数调用、
字符串和代码注入等节点无法被执行。
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from typing import Any

_OPERATORS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _evaluate(node: ast.AST) -> int | float:
    """递归计算一个已校验的算术 AST 节点。

    仅接受数字常量、白名单中的二元运算和一元正负号。布尔值
    在 Python 中是整数的子类，但对用户而言不应被当作 ``0/1``，
    因此需要显式排除。
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        right = _evaluate(node.right)
        # 限制指数可避免很小的输入在瞬间生成极大整数，拖垮 Agent 进程。
        if isinstance(node.op, ast.Pow) and abs(right) > 1_000:
            raise ValueError("幂的绝对值不能超过 1000")
        return _OPERATORS[type(node.op)](_evaluate(node.left), right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_evaluate(node.operand))
    raise ValueError("只支持数字和 + - * / // % ** 括号运算")


@dataclass
class CalculatorTool:
    """向旧同步 Agent 暴露基础算术计算能力。

    输入长度受限，且表达式最终由 :func:`_evaluate` 按 AST
    白名单执行，不具备访问 Python 运行时的能力。
    """

    name: str = "calculator"
    description: str = "计算一个基础数学表达式。"
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    })

    def run(self, arguments: dict[str, Any]) -> str:
        """校验并计算 ``expression``，以文本形式返回数值。"""

        expression = arguments["expression"]
        if not isinstance(expression, str) or len(expression) > 200:
            raise ValueError("expression 必须是不超过 200 个字符的字符串")
        return str(_evaluate(ast.parse(expression, mode="eval").body))
