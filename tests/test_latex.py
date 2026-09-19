from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from Agent.state import WorkloadKind
from gateway.latex import LatexCompilationService, LatexExecutionError
from gateway.state_controller import StateController
from gateway.store import GatewayStore


class FakeLatexSandbox:
    def __init__(
        self, workspace: Path, *, engine_available: bool = True,
        compile_error: bool = False,
    ) -> None:
        self.workspace = workspace
        self.engine_available = engine_available
        self.compile_error = compile_error
        self.status = SimpleNamespace(shell="pwsh.exe")
        self.commands: list[str] = []
        self.closed = False

    async def start(self, session_id: str):
        self.session_id = session_id
        return None

    async def run_bash(self, command: str, timeout_seconds: int = 30, *, writable_paths=None):
        del timeout_seconds, writable_paths
        self.commands.append(command)
        if "Get-Command" in command:
            if not self.engine_available:
                raise RuntimeError("engine missing")
            return SimpleNamespace(output="tectonic")
        if self.compile_error:
            raise RuntimeError(f"! Undefined control sequence in {self.workspace}")
        staging = next((self.workspace / ".yy-latex-build").iterdir())
        (staging / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (staging / "main.log").write_text("! Undefined control sequence", encoding="utf-8")
        return SimpleNamespace(output=f"compiled in {self.workspace}")

    async def close(self) -> None:
        self.closed = True


class LatexCompilationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_compilation_uses_sandbox_and_sanitizes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value).resolve()
            workspace = root / "workspace"
            source = root / "source"
            workspace.mkdir(); source.mkdir()
            (workspace / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
            config = SimpleNamespace(agent_root=root, coding_source_root=source)
            sandbox = FakeLatexSandbox(workspace)
            service = LatexCompilationService(config)
            with patch("gateway.latex.create_sandbox_session", return_value=sandbox):
                prepared = await service.prepare(
                    workspace, "compile-1", "YYWorkspace:\\main.tex", "auto",
                )
                result = await service.execute(prepared, "compile-1")

            self.assertTrue((result.artifact_root / "output.pdf").is_file())
            self.assertIn("YYWorkspace:", result.log)
            self.assertNotIn(str(workspace), result.log)
            self.assertTrue(sandbox.closed)
            self.assertFalse((workspace / ".yy-latex-build").exists())
            compile_command = sandbox.commands[-1]
            self.assertIn("tectonic", compile_command)
            self.assertNotIn("--untrusted", compile_command)

    async def test_sandbox_creation_failure_is_fail_closed_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value).resolve()
            workspace = root / "workspace"
            source = root / "source"
            workspace.mkdir(); source.mkdir()
            (workspace / "main.tex").write_text("test", encoding="utf-8")
            config = SimpleNamespace(agent_root=root, coding_source_root=source)
            service = LatexCompilationService(config)
            with (
                patch(
                    "gateway.latex.create_sandbox_session",
                    side_effect=RuntimeError("sandbox unavailable"),
                ),
                self.assertRaisesRegex(RuntimeError, "sandbox unavailable"),
            ):
                await service.prepare(
                    workspace, "compile-2", "YYWorkspace:\\main.tex", "auto",
                )
            self.assertFalse((workspace / ".yy-latex-build").exists())

    async def test_failed_compilation_keeps_sanitized_log(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value).resolve()
            workspace = root / "workspace"
            source = root / "source"
            workspace.mkdir(); source.mkdir()
            (workspace / "main.tex").write_text("broken", encoding="utf-8")
            service = LatexCompilationService(
                SimpleNamespace(agent_root=root, coding_source_root=source),
            )
            sandbox = FakeLatexSandbox(workspace, compile_error=True)
            with patch("gateway.latex.create_sandbox_session", return_value=sandbox):
                prepared = await service.prepare(
                    workspace, "compile-failed", "YYWorkspace:\\main.tex", "auto",
                )
                with self.assertRaises(LatexExecutionError) as failure:
                    await service.execute(prepared, "compile-failed")
            error = failure.exception
            saved = (error.artifact_root / "compile.log").read_text(encoding="utf-8")
            self.assertIn("YYWorkspace:", saved)
            self.assertNotIn(str(workspace), saved)
            self.assertTrue(error.diagnostics)

    async def test_xelatex_disables_shell_escape(self) -> None:
        command = LatexCompilationService._compile_command(
            "bash", "xelatex", "/yy/workspace/main.tex", "/yy/workspace/build",
        )
        self.assertIn("-no-shell-escape", command)

    async def test_restart_marks_active_compilation_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            controller = StateController(store.database_path, gateway_epoch="test")
            controller.create_run(
                run_id="run-1", workload_kind=WorkloadKind.LATEX_COMPILATION,
                project_id=project.project_id, client_id="test", task="compile",
                idempotency_key="latex-test", request_hash="0" * 64,
            )
            record = {
                "compilation_id": "compile-3",
                "project_id": project.project_id,
                "run_id": "run-1",
                "operation_id": "operation-1",
                "attempt_id": "attempt-1",
                "main_path": "YYWorkspace:\\main.tex",
                "engine": "tectonic",
                "status": "running",
                "diagnostics": [],
                "error": None,
                "created_at": "2026-09-19T12:00:00+08:00",
                "updated_at": "2026-09-19T12:00:00+08:00",
            }
            controller.create_latex_compilation(record)
            self.assertEqual(controller.interrupt_active_latex_compilations(), 1)
            restored, _ = controller.latex_compilation("compile-3")
            self.assertEqual(restored["status"], "interrupted")
            self.assertIn("restarted", restored["error"])


if __name__ == "__main__":
    unittest.main()
