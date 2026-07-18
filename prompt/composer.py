"""分层、可审计且无副作用的 Prompt 组合器。"""

from __future__ import annotations

from pathlib import Path

from memory import MemoryStore


class PromptComposer:
    """按固定顺序组合系统规则、项目指令与记忆。"""

    def __init__(self, project_root: Path, memory: MemoryStore) -> None:
        self.project_root, self.memory = project_root, memory

    def compose(self, task: str, session_id: str) -> list[dict[str, str]]:
        """生成模型消息，不在此层写入任何状态。"""
        instructions = self._read_instruction()
        profile = self.memory.profile_context()
        system = "你是严谨、透明的本地学习助手。工具调用必须遵守权限边界。"
        if instructions:
            system += f"\n\n项目指令：\n{instructions}"
        if profile:
            system += f"\n\n用户长期记忆：\n{profile[:6000]}"
        history = self.memory.restore_messages(session_id)
        return [{"role": "system", "content": system}, *history, {"role": "user", "content": task}]

    def _read_instruction(self) -> str:
        """仅读取项目根目录的正式指令文件。"""
        path = self.project_root / "AGENT.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""
