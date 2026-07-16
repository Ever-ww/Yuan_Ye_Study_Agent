"""运行界面的兼容公开入口。

顶层只导出旧同步终端组件，避免仅执行 ``import run_ui`` 就强制加载 Typer、Rich 和
FastAPI。正式 Harness CLI 位于 :mod:`run_ui.cli`，可通过 ``yy-agent`` 或
``python -m run_ui`` 启动。
"""

from .console import DynamicCLI, run_with_spinner

__all__ = ["DynamicCLI", "run_with_spinner"]
