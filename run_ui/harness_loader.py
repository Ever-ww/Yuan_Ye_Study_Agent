"""从准确命名的 `harness-evolution/` 目录加载独立 Harness 模块。"""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType


@lru_cache(maxsize=1)
def load_harness_module() -> ModuleType:
    """按源码路径加载 Harness，不在 Agent 核心复制其实现。"""
    candidates = (
        Path(__file__).parents[1] / "harness-evolution" / "harness.py",
        Path(sys.prefix) / "harness-evolution" / "harness.py",
    )
    path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    name = "yy_harness_evolution"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 Harness 模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
