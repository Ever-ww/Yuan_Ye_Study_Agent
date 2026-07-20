"""上下文压缩 Agent 的结构化 Prompt。"""

from __future__ import annotations

import json
from typing import Any


def compose_compression_messages(
    records: list[dict[str, Any]],
    existing_profile: str,
    validation_error: str = "",
) -> list[dict[str, str]]:
    """要求模型同时返回会话 Profile 与下一分段上下文摘要。"""
    system = """你是上下文压缩 Agent。请只根据提供的历史整理信息，不推测未出现的事实。
必须只输出一个合法 JSON 对象，不要使用 Markdown 代码围栏，也不要附加解释。JSON 必须包含两个非空字符串字段：
1. profile_markdown：完整的合并后 Markdown，只包含用户特征、偏好、研究方向和稳定关键事实，不包含临时未完成任务。
2. context_summary_markdown：供下一段会话继续工作的结构化 Markdown，至少包含“用户目标”“已完成任务”“未完成任务”“关键决策”“必要工具结论”五个标题；没有内容时写“无”。
已有 Profile 代表先前分段的结果，profile_markdown 必须把它与本次新增事实去重合并，而不是简单追加。"""
    payload = {
        "existing_profile_markdown": existing_profile,
        "session_records": records,
    }
    if validation_error:
        payload["previous_output_error"] = validation_error
        system += "\n上一次输出未通过校验，请严格修正格式。"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
