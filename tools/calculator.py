"""仅支持基础算术的安全计算器工具。"""

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
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1_000:
            raise ValueError("幂的绝对值不能超过 1000")
        return _OPERATORS[type(node.op)](_evaluate(node.left), right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_evaluate(node.operand))
    raise ValueError("只支持数字和 + - * / // % ** 括号运算")


@dataclass
class CalculatorTool:
    name: str = "calculator"
    description: str = "计算一个基础数学表达式。"
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    })

    def run(self, arguments: dict[str, Any]) -> str:
        expression = arguments["expression"]
        if not isinstance(expression, str) or len(expression) > 200:
            raise ValueError("expression 必须是不超过 200 个字符的字符串")
        return str(_evaluate(ast.parse(expression, mode="eval").body))
