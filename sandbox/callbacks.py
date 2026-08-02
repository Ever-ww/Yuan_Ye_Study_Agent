"""把 Docker 沙箱生命周期注册到现有十阶段 Hook。"""

from __future__ import annotations

from Agent.hook import HookEvent, HookPoint, HookRegistry

from .docker import SandboxSessionProtocol, sandbox_status_of


def register_sandbox_callbacks(
    registry: HookRegistry,
    sandbox: SandboxSessionProtocol,
) -> None:
    """沙箱早于其他 Trace 初始化启动，并在其他结束回调后关闭。"""

    async def start_sandbox(event: HookEvent) -> None:
        checkpoint = await sandbox.start(event.session_id)
        event.data["sandbox_checkpoint"] = checkpoint.model_dump(mode="json")
        event.data["sandbox_status"] = sandbox_status_of(sandbox).model_dump(mode="json")

    async def close_sandbox(event: HookEvent) -> None:
        await sandbox.close()

    registry.register(HookPoint.TRACE_START, start_sandbox, priority=-300)
    registry.register(HookPoint.TRACE_END, close_sandbox, priority=300)
