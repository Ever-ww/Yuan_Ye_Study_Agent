"""Bounded process capture and cancellation, used only AFTER sandbox wrapping."""
from __future__ import annotations

import asyncio
import os
import signal
from .session import CommandResult, SandboxRecoveryRequired


async def run_isolated(arguments: list[str], *, cwd: str, env: dict[str, str],
                       timeout: float) -> CommandResult:
    if os.name == "nt":
        raise RuntimeError("Windows commands require the AppContainer Job Object launcher")
    process = await asyncio.create_subprocess_exec(
        *arguments, cwd=cwd, env=env, start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def drain(reader):
        captured = bytearray()
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            if len(captured) < 1000000:
                captured.extend(chunk[:1000000 - len(captured)])
        return captured.decode("utf-8", errors="replace")

    outputs = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
    async def wait_for_root_exit():
        # Process.wait() can wait for pipe EOF after the root has already exited.
        # A background child holding stdout must not delay process-group cleanup.
        while process.returncode is None:
            await asyncio.sleep(0.01)
    try:
        await asyncio.wait_for(wait_for_root_exit(), timeout)
    finally:
        # Also clean up background children after a successful shell exit.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise SandboxRecoveryRequired("Cannot stop sandbox process group") from exc
        try:
            await asyncio.wait_for(process.wait(), 5)
            await asyncio.wait_for(asyncio.gather(*outputs), 5)
        except BaseException as exc:
            for task in outputs:
                task.cancel()
            await asyncio.gather(*outputs, return_exceptions=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SandboxRecoveryRequired("Sandbox child streams did not close after termination") from exc
    return CommandResult(returncode=process.returncode, stdout=outputs[0].result(), stderr=outputs[1].result())
