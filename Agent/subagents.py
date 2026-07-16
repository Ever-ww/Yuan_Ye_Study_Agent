"""子代理定义的发现、解析与只读注册表。

子代理使用带 YAML 风格 frontmatter 的 Markdown 文件描述。注册表只负责把用户级、
兼容目录、项目原生目录以及已信任插件中的定义转换为 ``AgentDefinition``；真正的
模型调用、工具白名单执行和 worktree 隔离由 :class:`Agent.runtime.AgentRuntime`
负责。保持这一边界可以让“发现配置”本身始终是只读操作。

兼容目录 ``.claude/agents`` 会被读取为文本定义，但插件目录只有在上层插件管理器
确认其 ``agents`` 组件已受信任后才会通过 ``plugin_roots`` 传入。
"""

from __future__ import annotations

from pathlib import Path

from skills.registry import load_plugin_manifest, parse_frontmatter

from .types import AgentDefinition


class AgentRegistry:
    """按作用域发现并缓存 Markdown 子代理定义。

    同名定义遵循发现顺序的“后者覆盖前者”规则：用户级 → Claude 兼容项目目录 →
    ``.yy`` 原生项目目录 → 具名插件。插件名称会成为命名空间，避免多个插件之间
    发生无提示覆盖。
    """

    def __init__(self, project_root: Path, user_dir: Path) -> None:
        """保存已解析的项目和用户目录，不触发文件系统扫描。"""

        self.project_root = project_root
        self.user_dir = user_dir
        self._agents: dict[str, AgentDefinition] = {}

    def discover(self, plugin_roots: list[Path] | None = None) -> list[AgentDefinition]:
        """重新扫描全部定义来源，并返回本轮发现结果。

        单个定义格式错误时跳过该文件，避免一个损坏的第三方定义使整个 Harness
        无法启动。插件根目录由调用方完成信任过滤；本方法不会自行启用未知插件。
        """

        # 每次扫描都从空缓存开始，确保已删除或被禁用的定义不会残留。
        self._agents = {}
        roots: list[tuple[Path, str | None]] = [
            (self.user_dir / "agents", None),
            (self.project_root / ".claude" / "agents", None),
            (self.project_root / ".yy" / "agents", None),
        ]
        for plugin_root in plugin_roots or []:
            manifest = load_plugin_manifest(plugin_root)
            roots.append((plugin_root / "agents", str(manifest.get("name", plugin_root.name))))

        for root, namespace in roots:
            if not root.exists():
                continue
            for path in root.glob("*.md"):
                try:
                    metadata, prompt = parse_frontmatter(path.read_text(encoding="utf-8"))
                    raw_name = str(metadata.get("name", path.stem))
                    name = f"{namespace}:{raw_name}" if namespace else raw_name
                    definition = AgentDefinition(
                        name=name,
                        description=str(metadata.get("description", "")),
                        prompt=prompt.strip(),
                        model=metadata.get("model"),
                        # 同时接受 Claude 风格 camelCase 与本项目 snake_case。
                        max_turns=int(metadata.get("maxTurns", metadata.get("max_turns", 12))),
                        tools=tuple(metadata.get("tools", []) or []),
                        disallowed_tools=tuple(metadata.get("disallowedTools", []) or []),
                        skills=tuple(metadata.get("skills", []) or []),
                        memory=metadata.get("memory"),
                        background=bool(metadata.get("background", False)),
                        isolation=metadata.get("isolation"),
                    )
                    self._agents[name] = definition
                except (ValueError, TypeError):
                    # frontmatter 解析、数字转换或字段类型错误均视为该定义不可用。
                    continue
        return list(self._agents.values())

    def get(self, name: str) -> AgentDefinition | None:
        """按最终名称查询定义；插件定义需使用 ``插件名:代理名``。"""

        return self._agents.get(name)

    def all(self) -> list[AgentDefinition]:
        """返回缓存快照，避免调用方直接修改内部字典。"""

        return list(self._agents.values())


__all__ = ["AgentRegistry"]
