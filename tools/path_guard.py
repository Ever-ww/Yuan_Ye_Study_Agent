"""文件类工具共用的工作区路径安全边界。"""

from pathlib import Path


def safe_workspace_path(root: Path, requested: str) -> Path:
    """解析路径，并阻止越界或访问敏感配置文件。"""
    workspace = root.resolve()
    path = (workspace / requested).resolve()
    if workspace != path and workspace not in path.parents:
        raise PermissionError("路径必须位于项目工作区内")
    if path.name.startswith(".env"):
        raise PermissionError("禁止访问敏感配置文件")
    return path
