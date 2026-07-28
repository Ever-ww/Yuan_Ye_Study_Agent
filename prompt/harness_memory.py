"""Harness 成功合并后维护四文件长期记忆的结构化 Prompt。"""

from __future__ import annotations

import json
from typing import Any


def compose_harness_memory_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """要求维护模型只返回可校验的长期记忆增量。"""
    system = """你负责维护 Harness Coding Agent 的长期项目记忆。
只根据已成功合并并通过测试的事实更新内容，不得推测。
必须只输出一个合法 JSON 对象，不使用代码围栏或额外解释，字段如下：
1. project_markdown：完整的最新 PROJECT.md，描述当前架构、模块边界、公共接口、测试方式和开发规范，最多 24576 个字符。
2. change_entry_markdown：一条以二级标题开始的 CHANGES.md 追加记录，包含时间、问题、提交、修改文件和验证结果。
3. lesson_entry_markdown：一条以二级标题开始的可复用经验；没有经过本次修复验证的新经验时必须为 null。
不要返回或修改 AGENT.md。不要把失败尝试、猜测或敏感配置写入长期记忆。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
