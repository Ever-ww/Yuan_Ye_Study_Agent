"""全局多文件 Hook Extension 的安全扫描、契约校验与注册。"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from Agent.hook import HookEvent, HookPoint, HookRegistry


_FILE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}\.py$")
_STAGES = tuple(point.value for point in HookPoint)


class ExtensionContext(BaseModel):
    """扩展可见的最小运行环境；刻意不暴露凭据、UI、数据库或模型客户端。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    agent_root: Path
    source_root: Path
    workspace_root: Path
    state_root: Path
    provider: str
    model: str
    sandbox_enabled: bool


class ExtensionModule(BaseModel):
    """一个通过静态契约检查的阶段扩展。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    stage: HookPoint
    name: str = Field(min_length=1, max_length=128)
    priority: int = Field(ge=-50, le=50)
    path: Path
    handle: Any


class ExtensionCatalog:
    """Gateway 启动时生成、运行期不可变的 Extension 快照。"""

    def __init__(self, modules: Mapping[HookPoint, tuple[ExtensionModule, ...]] | None = None) -> None:
        normalized = {point: tuple((modules or {}).get(point, ())) for point in HookPoint}
        self._modules = MappingProxyType(normalized)

    @property
    def modules(self) -> Mapping[HookPoint, tuple[ExtensionModule, ...]]:
        return self._modules

    def register(self, registry: HookRegistry, context: ExtensionContext) -> None:
        """把快照中的所有回调注册到 Runtime 的正式 HookRegistry。"""
        for point in HookPoint:
            for extension in self._modules[point]:
                async def callback(
                    event: HookEvent,
                    *,
                    _extension: ExtensionModule = extension,
                ) -> None:
                    try:
                        result = _extension.handle(event, context)
                        if inspect.isawaitable(result):
                            await result
                    except Exception as exc:
                        raise RuntimeError(
                            f"Extension {_extension.stage.value}/{_extension.path.name} "
                            f"({_extension.name}) 执行失败：{exc}"
                        ) from exc

                callback.__name__ = f"extension_{point.value}_{extension.path.stem}"
                registry.register(point, callback, priority=extension.priority)


class ExtensionLoader:
    """扫描源码仓库 `extension/hook/` 的直接 Python 子文件。"""

    def __init__(self, source_root: Path) -> None:
        self.source_root = source_root.resolve()
        self.hook_root = (self.source_root / "extension" / "hook").resolve()

    def scan(self) -> ExtensionCatalog:
        found: dict[HookPoint, tuple[ExtensionModule, ...]] = {}
        for point in HookPoint:
            found[point] = self._scan_stage(point)
        return ExtensionCatalog(found)

    def _scan_stage(self, point: HookPoint) -> tuple[ExtensionModule, ...]:
        stage_dir = self.hook_root / point.value
        if not stage_dir.exists():
            return ()
        if stage_dir.is_symlink() or not stage_dir.is_dir():
            raise ValueError(f"Extension 阶段目录非法：{stage_dir}")
        stage_dir = stage_dir.resolve()
        try:
            stage_dir.relative_to(self.hook_root)
        except ValueError as exc:
            raise ValueError(f"Extension 阶段路径越界：{stage_dir}") from exc

        modules: list[ExtensionModule] = []
        names: set[str] = set()
        for path in sorted(stage_dir.iterdir(), key=lambda item: item.name):
            if path.is_dir() or path.name.startswith(".") or path.name.startswith("~"):
                continue
            if path.suffix != ".py":
                continue
            if path.is_symlink() or not _FILE_NAME.fullmatch(path.name):
                raise ValueError(f"Extension 文件非法：{point.value}/{path.name}")
            resolved = path.resolve()
            try:
                resolved.relative_to(stage_dir)
            except ValueError as exc:
                raise ValueError(f"Extension 文件路径越界：{path}") from exc
            module = self._load_module(point, resolved)
            if module.name in names:
                raise ValueError(f"Extension 名称重复：{point.value}/{module.name}")
            names.add(module.name)
            modules.append(module)
        return tuple(sorted(modules, key=lambda item: (item.priority, item.path.name)))

    def _load_module(self, point: HookPoint, path: Path) -> ExtensionModule:
        module_name = f"_yuan_ye_extension_{point.value}_{path.stem}_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"无法创建 Extension 模块：{point.value}/{path.name}")
        module = importlib.util.module_from_spec(spec)
        try:
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            raise RuntimeError(f"Extension 导入失败：{point.value}/{path}：{exc}") from exc
        finally:
            sys.modules.pop(module_name, None)
        return self._validate_module(point, path, module)

    @staticmethod
    def _validate_module(point: HookPoint, path: Path, module: ModuleType) -> ExtensionModule:
        name = getattr(module, "EXTENSION_NAME", None)
        priority = getattr(module, "PRIORITY", None)
        handle = getattr(module, "handle", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Extension 缺少合法 EXTENSION_NAME：{point.value}/{path.name}")
        if isinstance(priority, bool) or not isinstance(priority, int) or not -50 <= priority <= 50:
            raise ValueError(f"Extension PRIORITY 必须位于 -50..50：{point.value}/{path.name}")
        if not inspect.iscoroutinefunction(handle):
            raise ValueError(f"Extension handle 必须是 async def：{point.value}/{path.name}")
        parameters = list(inspect.signature(handle).parameters.values())
        if len(parameters) != 2 or [item.name for item in parameters] != ["event", "context"]:
            raise ValueError(f"Extension handle 签名必须是 (event, context)：{point.value}/{path.name}")
        return ExtensionModule(
            stage=point,
            name=name.strip(),
            priority=priority,
            path=path,
            handle=handle,
        )


__all__ = [
    "ExtensionCatalog",
    "ExtensionContext",
    "ExtensionLoader",
    "ExtensionModule",
]
