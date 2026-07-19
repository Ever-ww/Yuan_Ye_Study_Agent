"""受限四则运算工具。"""

import ast
from typing import Any

from .contracts import ToolContext


class CalculatorTool:
    """仅计算由普通数字和四则运算符组成的表达式。"""

    name = "calculator"
    description = "计算数学表达式"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    }
    risk = "read"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        tree = ast.parse(arguments["expression"], mode="eval")
        allowed = (
            ast.Expression,
            ast.BinOp,
            ast.UnaryOp,
            ast.Constant,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.USub,
        )
        if any(not isinstance(node, allowed) for node in ast.walk(tree)):
            raise ValueError("表达式仅支持数字和四则运算")
        constants = (node.value for node in ast.walk(tree) if isinstance(node, ast.Constant))
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in constants):
            raise ValueError("表达式仅支持普通数字")
        return str(eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, {}))
