"""同步兼容 API 与异步 Harness 的统一公开入口。

所有符号都在首次访问时才导入。这样 ``tools``、``memory`` 等底层包可以引用
``Agent.types``，而不会因包初始化立即载入 ``Agent.runtime`` 再反向导入这些包，
从而保持多种冷启动导入顺序都不产生循环依赖。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    # 原有同步 API：为历史调用方保留，真实实现统一位于 legacy.py。
    "Agent": (".legacy", "Agent"),
    "AgentConfig": (".legacy", "AgentConfig"),
    "AgentResult": (".legacy", "AgentResult"),
    "LegacyAgentResult": (".legacy", "AgentResult"),
    "ReActAgent": (".legacy", "ReActAgent"),
    "Step": (".legacy", "Step"),
    "ToolRegistry": (".legacy", "ToolRegistry"),
    "Tool": ("tools", "Tool"),
    "CalculatorTool": ("tools", "CalculatorTool"),
    "CurrentTimeTool": ("tools", "CurrentTimeTool"),
    # 事件驱动 Harness API：新代码应优先使用这一组类型和运行时。
    "AgentRuntime": (".runtime", "AgentRuntime"),
    "RuntimeConfig": (".config", "RuntimeConfig"),
    "load_runtime_config": (".config", "load_runtime_config"),
    "ApprovalDecision": (".permissions", "ApprovalDecision"),
    "CapabilityGrant": (".permissions", "CapabilityGrant"),
    "PermissionMode": (".permissions", "PermissionMode"),
    "AgentDefinition": (".types", "AgentDefinition"),
    "RuntimeResult": (".types", "AgentResult"),
    "AsyncTool": (".types", "AsyncTool"),
    "HookHandler": (".types", "HookHandler"),
    "ImageContent": (".types", "ImageContent"),
    "MemoryStore": (".types", "MemoryStore"),
    "ModelProvider": (".types", "ModelProvider"),
    "RunEvent": (".types", "RunEvent"),
    "SandboxProvider": (".types", "SandboxProvider"),
    "SchedulerStore": (".types", "SchedulerStore"),
    "Session": (".types", "Session"),
    "ToolCall": (".types", "ToolCall"),
    "ToolResult": (".types", "ToolResult"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """解析、缓存并返回公开符号；未知名称遵循标准模块属性错误语义。"""

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    # 缓存到模块全局后，后续访问不会再次执行映射查询与 ``import_module``。
    globals()[name] = value
    return value
