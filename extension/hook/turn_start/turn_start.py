"""turn_start 阶段的空扩展示例。"""

from Agent.extensions import ExtensionContext
from Agent.hook import HookEvent

EXTENSION_NAME = "default-turn-start"
PRIORITY = 0


async def handle(event: HookEvent, context: ExtensionContext) -> None:
    """保留阶段结构；在描述性新文件中实现实际能力。"""
