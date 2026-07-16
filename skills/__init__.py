"""技能发现、固定版本安装与插件市场管理的公共导出。

具体实现放在 :mod:`skills.registry`，该包只暴露运行时组装和
CLI 所需的稳定接口，避免把安装内部辅助函数变成公开 API。
"""

from .registry import PluginManager, SkillInstaller, SkillRegistry, validate_skill

__all__ = ["PluginManager", "SkillInstaller", "SkillRegistry", "validate_skill"]
