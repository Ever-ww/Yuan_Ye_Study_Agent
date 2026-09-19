"""Sandboxed LaTeX compilation without exposing host paths to clients."""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from Agent import RuntimeConfig
from sandbox import PathMappingSnapshot, SandboxSessionProtocol, create_sandbox_session


class LatexEngineUnavailable(RuntimeError):
    pass


class LatexWorkspaceRevisionConflict(RuntimeError):
    pass


class LatexExecutionError(RuntimeError):
    def __init__(
        self, message: str, *, log: str,
        diagnostics: tuple[dict[str, object], ...], artifact_root: Path,
    ) -> None:
        super().__init__(message)
        self.log = log
        self.diagnostics = diagnostics
        self.artifact_root = artifact_root


@dataclass(slots=True)
class PreparedLatexCompilation:
    sandbox: SandboxSessionProtocol
    mapper: PathMappingSnapshot
    workspace_root: Path
    main_path: Path
    staging_path: Path
    engine: str


@dataclass(frozen=True, slots=True)
class LatexExecutionResult:
    log: str
    diagnostics: tuple[dict[str, object], ...]
    artifact_root: Path


class LatexCompilationService:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.artifact_root = config.agent_root / ".yy" / "web-latex"

    async def prepare(
        self,
        workspace_root: Path,
        compilation_id: str,
        main_logical_path: str,
        requested_engine: str,
    ) -> PreparedLatexCompilation:
        workspace = workspace_root.resolve()
        source_root = self.config.coding_source_root or Path(__file__).resolve().parents[1]
        mapper = PathMappingSnapshot(
            workspace_root=workspace,
            agent_source_root=source_root,
            trace_id=f"latex:{compilation_id}",
        )
        main_path = mapper.resolve_workspace_path(main_logical_path)
        if not main_path.is_file() or main_path.suffix.casefold() != ".tex":
            raise FileNotFoundError(main_logical_path)
        staging = workspace / ".yy-latex-build" / compilation_id
        if staging.exists():
            raise FileExistsError("LaTeX staging directory already exists")
        staging.mkdir(parents=True)
        sandbox: SandboxSessionProtocol | None = None
        try:
            sandbox = create_sandbox_session(
                self.config, project_root=workspace, path_mapping=mapper,
            )
            await sandbox.start(f"latex:{compilation_id}")
            engine = await self._select_engine(
                sandbox, mapper, staging, requested_engine,
            )
        except BaseException:
            if sandbox is not None:
                await sandbox.close()
            shutil.rmtree(staging, ignore_errors=True)
            if staging.parent.is_dir() and not any(staging.parent.iterdir()):
                staging.parent.rmdir()
            raise
        assert sandbox is not None
        return PreparedLatexCompilation(
            sandbox=sandbox,
            mapper=mapper,
            workspace_root=workspace,
            main_path=main_path,
            staging_path=staging,
            engine=engine,
        )

    async def execute(
        self, prepared: PreparedLatexCompilation, compilation_id: str,
    ) -> LatexExecutionResult:
        logical_main = prepared.mapper.to_logical_path(prepared.main_path)
        logical_staging = prepared.mapper.to_logical_path(prepared.staging_path)
        command = self._compile_command(
            prepared.sandbox.status.shell or "", prepared.engine,
            logical_main, logical_staging,
        )
        output = ""
        try:
            result = await prepared.sandbox.run_bash(
                command,
                120,
                writable_paths=(logical_staging,),
            )
            output = result.output
            pdf = prepared.staging_path / f"{prepared.main_path.stem}.pdf"
            if not pdf.is_file():
                raise RuntimeError("LaTeX engine completed without producing a PDF")
            destination = self.artifact_root / compilation_id
            destination.mkdir(parents=True, exist_ok=False)
            shutil.copy2(pdf, destination / "output.pdf")
            log_file = prepared.staging_path / f"{prepared.main_path.stem}.log"
            if log_file.is_file():
                output = f"{output}\n{log_file.read_text(encoding='utf-8', errors='replace')}"
            sanitized = prepared.mapper.sanitize_for_model(output)[-200_000:]
            (destination / "compile.log").write_text(sanitized, encoding="utf-8")
            return LatexExecutionResult(
                log=sanitized,
                diagnostics=self.parse_diagnostics(sanitized),
                artifact_root=destination,
            )
        except Exception as exc:
            message = prepared.mapper.sanitize_for_model(str(exc))
            destination = self.artifact_root / compilation_id
            destination.mkdir(parents=True, exist_ok=False)
            (destination / "compile.log").write_text(message, encoding="utf-8")
            raise LatexExecutionError(
                message,
                log=message,
                diagnostics=self.parse_diagnostics(message),
                artifact_root=destination,
            ) from exc
        finally:
            await self.discard(prepared)

    @staticmethod
    async def discard(prepared: PreparedLatexCompilation) -> None:
        """Release a prepared sandbox and its private staging directory."""
        await prepared.sandbox.close()
        shutil.rmtree(prepared.staging_path, ignore_errors=True)
        parent = prepared.staging_path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    async def _select_engine(
        self,
        sandbox: SandboxSessionProtocol,
        mapper: PathMappingSnapshot,
        staging: Path,
        requested: str,
    ) -> str:
        shell = (sandbox.status.shell or "").casefold()
        candidates = ("tectonic", "xelatex") if requested == "auto" else (requested,)
        logical_staging = mapper.to_logical_path(staging)
        for engine in candidates:
            if shell.endswith(("powershell.exe", "pwsh.exe")):
                command = (
                    f"if (Get-Command {engine} -ErrorAction SilentlyContinue) "
                    f"{{ Write-Output '{engine}' }} else {{ exit 17 }}"
                )
            else:
                command = f"command -v {shlex.quote(engine)} >/dev/null"
            try:
                await sandbox.run_bash(
                    command, 30, writable_paths=(logical_staging,),
                )
                return engine
            except RuntimeError:
                continue
        raise LatexEngineUnavailable(
            "No configured LaTeX engine is available in the OS sandbox; install Tectonic or configure XeLaTeX",
        )

    @staticmethod
    def _compile_command(shell: str, engine: str, main_path: str, output_path: str) -> str:
        powershell = shell.casefold().endswith(("powershell.exe", "pwsh.exe"))
        quote = (
            (lambda value: "'" + value.replace("'", "''") + "'")
            if powershell else shlex.quote
        )
        main, output = quote(main_path), quote(output_path)
        if engine == "tectonic":
            return f"tectonic --keep-logs --outdir {output} {main}"
        return f"xelatex -no-shell-escape -interaction=nonstopmode -halt-on-error -output-directory={output} {main}"

    @staticmethod
    def parse_diagnostics(log: str) -> tuple[dict[str, object], ...]:
        diagnostics: list[dict[str, object]] = []
        current_file: str | None = None
        for line in log.splitlines():
            file_match = re.search(r"((?:YYWorkspace:|/yy/workspace)[^\s:]*\.tex)", line)
            if file_match:
                current_file = file_match.group(1)
            matched = re.search(r"(?:^|:)(\d+):\s*(.+)$", line)
            if matched and ("error" in line.casefold() or line.lstrip().startswith("!")):
                diagnostics.append({
                    "file": current_file,
                    "line": int(matched.group(1)),
                    "severity": "error",
                    "message": matched.group(2).strip()[:1000],
                })
            elif line.lstrip().startswith("!"):
                diagnostics.append({
                    "file": current_file,
                    "line": None,
                    "severity": "error",
                    "message": line.lstrip("! ")[:1000],
                })
        return tuple(diagnostics[:200])
