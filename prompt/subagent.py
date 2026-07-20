"""无持久化子 Agent 的专用 Prompt。"""


def compose_subagent_messages(task: str, instructions: str = "") -> list[dict[str, str]]:
    """组合受父 Agent 委派但不继承其会话历史的消息。"""
    system = (
        "你是主 Agent 临时创建的子 Agent。只完成给定任务，遵守工具权限边界，"
        "不要假设你拥有父会话中未提供的信息，最后返回可直接交给父 Agent 使用的结果。"
    )
    if instructions.strip():
        system += f"\n\n角色说明：\n{instructions.strip()}"
    return [{"role": "system", "content": system}, {"role": "user", "content": task}]
