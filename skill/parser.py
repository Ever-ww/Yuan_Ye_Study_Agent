"""Agent Skills `SKILL.md` 格式解析、摘要与 XML 目录生成。"""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from .models import SkillMetadata


_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_STRING_MAP = TypeAdapter(dict[str, str])
_ALLOWED_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}


def parse_skill(skill_root: Path) -> SkillMetadata:
    """解析并严格校验一个 Skill 的发现层元数据。"""
    root = skill_root.resolve()
    path = root / "SKILL.md"
    if not path.is_file() or path.is_symlink():
        raise ValueError("Skill 必须包含普通文件 SKILL.md")
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(raw)
    if match is None:
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开头")
    try:
        value = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md frontmatter 不是合法 YAML：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("SKILL.md frontmatter 必须是对象")
    unknown = set(value).difference(_ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"SKILL.md 包含未知 frontmatter 字段：{sorted(unknown)[0]}")
    name = value.get("name")
    description = value.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        raise ValueError("SKILL.md 必须包含字符串 name 和 description")
    if name != root.name:
        raise ValueError("Skill name 必须与父目录名称一致")
    metadata_value = value.get("metadata", {})
    try:
        metadata = _STRING_MAP.validate_python(metadata_value, strict=True)
    except ValidationError as exc:
        raise ValueError(f"Skill metadata 必须是字符串键值映射：{exc}") from exc
    optional: dict[str, Any] = {}
    for key, target in (
        ("license", "license"),
        ("compatibility", "compatibility"),
        ("allowed-tools", "allowed_tools"),
    ):
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise ValueError(f"Skill {key} 必须是字符串")
        optional[target] = item.strip() if isinstance(item, str) else item
    return SkillMetadata(
        name=name,
        description=description.strip(),
        location=f"skills/{name}/SKILL.md",
        metadata=metadata,
        content_digest=content_digest(root),
        **optional,
    )


def content_digest(root: Path) -> str:
    """按相对路径和文件内容计算可复核的目录 SHA-256。"""
    digest = hashlib.sha256()
    for path in _regular_files(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def catalog_xml(skills: tuple[SkillMetadata, ...]) -> str:
    """生成只包含发现层元数据的安全 XML。"""
    root = ET.Element("available_skills")
    for metadata in sorted(skills, key=lambda item: item.name):
        item = ET.SubElement(root, "skill")
        ET.SubElement(item, "name").text = metadata.name
        ET.SubElement(item, "description").text = metadata.description
        ET.SubElement(item, "location").text = metadata.location
    return ET.tostring(root, encoding="unicode", short_empty_elements=False)


def _regular_files(root: Path) -> list[Path]:
    values: list[Path] = []
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        names.sort()
        files.sort()
        for name in names:
            path = base / name
            if path.is_symlink():
                raise ValueError(f"Skill 包含不受支持的符号链接：{path.relative_to(root)}")
        for name in files:
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Skill 包含不受支持的文件：{path.relative_to(root)}")
            values.append(path)
    return values
