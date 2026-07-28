"""Skill 来源获取、静态审核、安装事务和可信读取。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import ValidationError

from sandbox import WorkspaceLockManager

from .models import (
    InstalledSkillEntry,
    SkillAuditFinding,
    SkillAuditReport,
    SkillIndex,
    SkillInstallRequest,
    SkillInstallResult,
    SkillMetadata,
    SkillSource,
)
from .parser import catalog_xml, content_digest, parse_skill


Approval = Callable[[str, dict[str, Any]], Awaitable[bool]]
_MAX_FILES = 500
_MAX_TOTAL_BYTES = 20 * 1024 * 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SKILL_MD_BYTES = 512 * 1024
_MAX_READ_BYTES = 1024 * 1024
_MAX_CATALOG_CHARS = 64 * 1024
_CLONE_TIMEOUT_SECONDS = 120
_SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".ts"}
_LICENSE_NAMES = {
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "cc0-1.0",
    "gpl-2.0",
    "gpl-2.0-only",
    "gpl-2.0-or-later",
    "gpl-3.0",
    "gpl-3.0-only",
    "gpl-3.0-or-later",
    "isc",
    "lgpl-2.1",
    "lgpl-3.0",
    "mit",
    "mpl-2.0",
    "unlicense",
}
_PRIVATE_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
)
_REVIEW_PATTERNS = (
    (
        "network-command",
        re.compile(
            r"\b(?:curl|wget|Invoke-WebRequest|Invoke-RestMethod|git\s+clone|"
            r"requests\.(?:get|post)|httpx\.(?:get|post)|fetch\s*\(|npm\s+install)\b",
            re.I,
        ),
    ),
    (
        "environment-access",
        re.compile(r"(?:os\.environ|os\.getenv|process\.env|\$env:|\$\{[A-Z_][A-Z0-9_]*\})", re.I),
    ),
    ("privilege-escalation", re.compile(r"\b(?:sudo|runas|chmod\s+[467][0-7]{2})\b", re.I)),
    (
        "destructive-command",
        re.compile(r"\b(?:rm\s+-rf|Remove-Item\s+.*-Recurse|shutil\.rmtree|unlink\s*\(|format\s+[A-Z]:)\b", re.I),
    ),
    (
        "credential-access",
        re.compile(r"(?:\.ssh|\.aws|\.azure|\.kube|\.config[/\\]gcloud|credentials|api[_ -]?key|authorization)", re.I),
    ),
    (
        "dynamic-execution",
        re.compile(r"\b(?:eval|exec|compile\s*\(|base64\s+-d|FromBase64String|EncodedCommand|marshal\.loads)\b", re.I),
    ),
    ("instruction-override", re.compile(r"(?:ignore|override).{0,40}(?:system|previous).{0,20}instruction", re.I)),
)


class SkillService:
    """只让经过审核且摘要未变化的 Skill 进入 Agent。"""

    def __init__(
        self,
        agent_root: Path,
        workspace_root: Path,
        *,
        approval: Approval | None = None,
    ) -> None:
        self.agent_root = agent_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.approval = approval
        self.skills_root = self.agent_root / "skills"
        self.state_root = self.agent_root / ".yy" / "skills"
        self.review_root = self.state_root / "review"
        self.audit_root = self.state_root / "audit"
        self.backup_root = self.state_root / "backups"
        self.index_path = self.state_root / "index.json"
        self._locks = WorkspaceLockManager(self.agent_root, state_root=self.agent_root)
        self.initialize()

    def initialize(self) -> None:
        """创建本机 Skill 目录，并清理超过一天的中断审核副本。"""
        for path in (
            self.skills_root,
            self.review_root,
            self.audit_root,
            self.backup_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._write_index(SkillIndex())
        cutoff = datetime.now().astimezone() - timedelta(hours=24)
        for path in self.review_root.iterdir():
            if not path.is_dir():
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            if modified < cutoff:
                shutil.rmtree(path, ignore_errors=True)

    def catalog(self) -> tuple[SkillMetadata, ...]:
        """扫描目录并只返回索引摘要仍匹配的 Skill。"""
        index = self._read_index()
        values: list[SkillMetadata] = []
        for name, entry in sorted(index.skills.items()):
            root = self.skills_root / name
            if root.is_symlink():
                continue
            try:
                metadata = parse_skill(root)
            except (OSError, UnicodeError, ValueError):
                continue
            if metadata.content_digest != entry.content_digest:
                continue
            if metadata.name != entry.name or metadata.description != entry.description:
                continue
            values.append(metadata)
        return tuple(values)

    def catalog_xml(self) -> str:
        """返回 System Prompt 使用的完整发现层 XML。"""
        value = catalog_xml(self.catalog())
        if len(value.encode("utf-8")) > _MAX_CATALOG_CHARS:
            raise RuntimeError("Skill XML 目录超过 64 KiB，请移除部分 Skill")
        return value

    def read(self, name: str, path: str = "SKILL.md") -> str:
        """读取已审核 Skill 内的单个 UTF-8 文本文件。"""
        index = self._read_index()
        entry = index.skills.get(name)
        if entry is None:
            raise KeyError(f"未知或未审核 Skill：{name}")
        installed = self.skills_root / name
        if installed.is_symlink():
            raise RuntimeError(f"Skill {name} 内容已变化，必须重新审核")
        root = installed.resolve()
        if not root.is_dir() or content_digest(root) != entry.content_digest:
            raise RuntimeError(f"Skill {name} 内容已变化，必须重新审核")
        relative = PurePosixPath(path.replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise PermissionError("Skill 读取路径必须是目录内相对路径")
        candidate = root / Path(*relative.parts)
        cursor = candidate
        while cursor != root:
            if cursor.is_symlink():
                raise PermissionError("skill_read 不允许读取符号链接")
            cursor = cursor.parent
        target = candidate.resolve()
        if root != target and root not in target.parents:
            raise PermissionError("Skill 读取路径越界")
        if target.is_symlink() or not target.is_file():
            raise FileNotFoundError(f"Skill 文本文件不存在：{path}")
        size = target.stat().st_size
        if size > _MAX_READ_BYTES:
            raise ValueError("Skill 单次读取不能超过 1 MiB")
        raw = target.read_bytes()
        if b"\0" in raw:
            raise ValueError("skill_read 不支持二进制文件")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("skill_read 只支持 UTF-8 文本") from exc

    def audit_report(self, review_id: str) -> SkillAuditReport:
        """读取一份永久保留的审核报告。"""
        if not re.fullmatch(r"[0-9a-f]{64}", review_id):
            raise ValueError("review_id 必须是 64 位小写 SHA-256")
        path = self.audit_root / f"{review_id}.json"
        if not path.is_file():
            raise KeyError(f"未知审核记录：{review_id}")
        return SkillAuditReport.model_validate_json(path.read_text(encoding="utf-8"))

    async def install(self, request: SkillInstallRequest) -> SkillInstallResult:
        """下载到 `.yy`、审核并按策略安装或更新。"""
        if request.action == "update":
            return await self.update(request)
        return await self._run_install(request.model_copy(update={"action": "install"}))

    async def update(self, request: SkillInstallRequest) -> SkillInstallResult:
        """显式更新已有 Skill；更新永远需要审核后确认。"""
        if not request.name:
            return SkillInstallResult(status="error", message="更新必须指定已安装 Skill 名称")
        return await self._run_install(request.model_copy(update={"action": "update"}))

    async def _run_install(self, request: SkillInstallRequest) -> SkillInstallResult:
        review_id = hashlib.sha256(
            f"{datetime.now().astimezone().isoformat()}:{request.model_dump_json()}:{uuid4().hex}".encode("utf-8"),
        ).hexdigest()
        directory = self.review_root / review_id
        source_root = directory / "source"
        prepared_root = directory / "prepared"
        directory.mkdir(parents=True, exist_ok=False)
        report: SkillAuditReport | None = None
        try:
            source, checkout_root = await self._acquire_source(request, source_root)
            candidates = self._discover_candidates(checkout_root)
            selected = self._select_candidate(checkout_root, candidates, request.skill_path)
            if selected is None:
                return SkillInstallResult(
                    status="selection_required",
                    message="来源中包含多个 Skill，请指定 skill_path",
                    review_id=review_id,
                    candidates=tuple(candidates),
                )
            prepared = prepared_root / selected.name
            await asyncio.to_thread(
                shutil.copytree,
                selected,
                prepared,
                symlinks=True,
                ignore=_checkout_git_ignore(selected),
            )
            report = self._audit(review_id, source, prepared, selected, checkout_root)
            self._write_report(report)
            if report.status == "blocked":
                return self._result_from_report(report, "blocked", "Skill 审核被硬性阻断")
            assert report.skill is not None
            conflict = (self.skills_root / report.skill.name).exists()
            if request.action == "install" and conflict:
                return self._result_from_report(
                    report,
                    "conflict",
                    f"Skill 已存在：{report.skill.name}；请使用显式 update",
                )
            if request.action == "update":
                if request.name != report.skill.name:
                    return self._result_from_report(
                        report,
                        "blocked",
                        "更新目标名称与下载 Skill 名称不一致",
                    )
                if not conflict:
                    return self._result_from_report(report, "conflict", f"待更新 Skill 不存在：{request.name}")
            needs_review = report.status == "review_required" or request.action == "update"
            if needs_review and not await self._approve_report(report, request.action):
                declined = report.model_copy(update={"status": "declined"})
                self._write_report(declined)
                return self._result_from_report(declined, "declined", "用户拒绝安装审核结果")
            result = await self._commit_install(request, report, prepared)
            if result.status != "installed":
                return result
            installed = report.model_copy(update={"status": "installed"})
            self._write_report(installed)
            return result
        except Exception as exc:
            if report is not None:
                return self._result_from_report(
                    report,
                    "error",
                    f"Skill 安装失败：{str(exc) or type(exc).__name__}",
                )
            return SkillInstallResult(
                status="error",
                message=f"Skill 获取失败：{str(exc) or type(exc).__name__}",
                review_id=review_id,
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    async def _acquire_source(
        self,
        request: SkillInstallRequest,
        destination: Path,
    ) -> tuple[SkillSource, Path]:
        if _github_repository(request.source) is not None:
            repository = _github_repository(request.source)
            assert repository is not None
            destination.mkdir(parents=True, exist_ok=False)
            checkout = destination / Path(urlparse(repository).path).stem
            if request.ref:
                _validate_git_ref(request.ref)
            arguments = [
                "git",
                "-c",
                "credential.helper=",
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
            ]
            if request.ref:
                arguments.extend(["--branch", request.ref])
            arguments.extend([repository, str(checkout)])
            environment = dict(os.environ)
            environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
            await _run_command(arguments, cwd=self.agent_root, env=environment, timeout=_CLONE_TIMEOUT_SECONDS)
            commit = (
                await _run_command(
                    ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                    cwd=self.agent_root,
                    env=environment,
                    timeout=30,
                )
            ).strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
                raise RuntimeError("GitHub 来源未返回有效 commit")
            return SkillSource(
                kind="github",
                value=repository,
                ref=request.ref,
                skill_path=request.skill_path,
                commit=commit,
            ), checkout
        if "://" in request.source:
            raise ValueError("仅支持公开 GitHub HTTPS URL 或当前 workspace 内的本地目录")
        source = Path(request.source)
        if not source.is_absolute():
            source = self.workspace_root / source
        source = source.resolve()
        if self.workspace_root != source and self.workspace_root not in source.parents:
            raise PermissionError("本地 Skill 来源必须位于当前 workspace")
        if not source.is_dir():
            raise FileNotFoundError(f"本地 Skill 目录不存在：{source}")
        destination.mkdir(parents=True, exist_ok=False)
        checkout = destination / source.name
        await asyncio.to_thread(shutil.copytree, source, checkout, symlinks=True)
        return SkillSource(
            kind="local",
            value=str(source),
            ref=request.ref,
            skill_path=request.skill_path,
        ), checkout

    def _discover_candidates(self, root: Path) -> list[str]:
        candidates: list[str] = []
        for directory, names, files in os.walk(root, followlinks=False):
            base = Path(directory)
            names[:] = [
                name
                for name in names
                if name != ".git" and not (base / name).is_symlink()
            ]
            path = base / "SKILL.md"
            if "SKILL.md" in files and path.is_file() and not path.is_symlink():
                candidates.append(base.relative_to(root).as_posix() or ".")
        return sorted(set(candidates))

    @staticmethod
    def _select_candidate(root: Path, candidates: list[str], selected: str | None) -> Path | None:
        if selected:
            normalized = PurePosixPath(selected.replace("\\", "/"))
            if normalized.is_absolute() or ".." in normalized.parts:
                raise PermissionError("skill_path 必须是仓库内相对路径")
            value = normalized.as_posix().rstrip("/") or "."
            if value not in candidates:
                raise ValueError(f"未找到指定 Skill：{value}")
            return root if value == "." else root / Path(*normalized.parts)
        if len(candidates) == 1:
            return root if candidates[0] == "." else root / Path(*PurePosixPath(candidates[0]).parts)
        if not candidates:
            raise ValueError("来源中没有找到 SKILL.md")
        return None

    def _audit(
        self,
        review_id: str,
        source: SkillSource,
        root: Path,
        selected_source: Path,
        repository_root: Path,
    ) -> SkillAuditReport:
        findings: list[SkillAuditFinding] = []
        files: dict[str, str] = {}
        total_bytes = 0
        total_files = 0
        for directory, names, filenames in os.walk(root, followlinks=False):
            base = Path(directory)
            for name in list(names):
                path = base / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    findings.append(_finding("block", "symlink", "Skill 不允许包含符号链接", relative))
                    names.remove(name)
                elif name == ".git":
                    findings.append(_finding("block", "nested-git", "Skill 不允许包含 Git 元数据", relative))
                    names.remove(name)
            for name in filenames:
                path = base / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or not path.is_file():
                    findings.append(_finding("block", "special-file", "Skill 只允许普通文件", relative))
                    continue
                mode = path.stat().st_mode
                if not stat.S_ISREG(mode):
                    findings.append(_finding("block", "special-file", "Skill 只允许普通文件", relative))
                    continue
                size = path.stat().st_size
                total_files += 1
                total_bytes += size
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
                if size > _MAX_FILE_BYTES:
                    findings.append(_finding("block", "file-too-large", "单文件超过 2 MiB", relative))
                if relative == "SKILL.md" and size > _MAX_SKILL_MD_BYTES:
                    findings.append(_finding("block", "skill-md-too-large", "SKILL.md 超过 512 KiB", relative))
                if "scripts" in Path(relative).parts or path.suffix.lower() in _SCRIPT_SUFFIXES:
                    findings.append(_finding("review", "script", "Skill 包含不会自动执行的脚本", relative))
                if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                    findings.append(_finding("review", "executable", "Skill 包含可执行文件", relative))
                if size <= _MAX_FILE_BYTES:
                    self._scan_text(path, relative, findings)
        if total_files > _MAX_FILES:
            findings.append(_finding("block", "too-many-files", "Skill 文件数超过 500"))
        if total_bytes > _MAX_TOTAL_BYTES:
            findings.append(_finding("block", "skill-too-large", "Skill 总大小超过 20 MiB"))
        metadata: SkillMetadata | None = None
        try:
            metadata = parse_skill(root)
        except (OSError, UnicodeError, ValueError) as exc:
            findings.append(_finding("block", "invalid-skill", str(exc), "SKILL.md"))
        if metadata is not None:
            license_value = _detect_license(metadata, selected_source, repository_root)
            if license_value is None:
                findings.append(_finding("review", "license-unclear", "未找到可识别的开源许可证"))
            elif metadata.license is None:
                metadata = metadata.model_copy(update={"license": license_value})
            existing = [item for item in self.catalog() if item.name != metadata.name]
            xml = catalog_xml(tuple([*existing, metadata]))
            if len(xml.encode("utf-8")) > _MAX_CATALOG_CHARS:
                findings.append(_finding("block", "catalog-too-large", "安装后 Skill XML 目录将超过 64 KiB"))
        status = "clean"
        if any(item.severity == "block" for item in findings):
            status = "blocked"
        elif any(item.severity == "review" for item in findings):
            status = "review_required"
        report_path = self.audit_root / f"{review_id}.json"
        return SkillAuditReport(
            review_id=review_id,
            created_at=datetime.now().astimezone(),
            status=status,
            source=source,
            skill=metadata,
            findings=tuple(_deduplicate_findings(findings)),
            files=files,
            total_files=total_files,
            total_bytes=total_bytes,
            report_path=report_path,
        )

    @staticmethod
    def _scan_text(path: Path, relative: str, findings: list[SkillAuditFinding]) -> None:
        raw = path.read_bytes()
        if b"\0" in raw:
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return
        for pattern in _PRIVATE_SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(_finding("block", "embedded-secret", "检测到疑似私钥或访问令牌", relative))
        for code, pattern in _REVIEW_PATTERNS:
            if pattern.search(text):
                findings.append(_finding("review", code, "检测到需要人工复核的操作说明", relative))

    async def _approve_report(self, report: SkillAuditReport, action: str) -> bool:
        if self.approval is None:
            return False
        return await self.approval("skill_review", {
            "action": action,
            "name": report.skill.name if report.skill else "",
            "review_id": report.review_id,
            "report_path": str(report.report_path),
            "findings": [
                {"code": item.code, "message": item.message, "path": item.path}
                for item in report.findings
                if item.severity == "review"
            ],
        })

    async def _commit_install(
        self,
        request: SkillInstallRequest,
        report: SkillAuditReport,
        prepared: Path,
    ) -> SkillInstallResult:
        assert report.skill is not None
        name = report.skill.name
        target = self.skills_root / name
        backup: Path | None = None
        async with self._locks.workspace_exclusive():
            index = self._read_index()
            if request.action == "install" and (target.exists() or name in index.skills):
                return self._result_from_report(report, "conflict", f"Skill 已存在：{name}")
            if request.action == "update" and (not target.exists() or name not in index.skills):
                return self._result_from_report(report, "conflict", f"待更新 Skill 不存在：{name}")
            current = [item for item in self.catalog() if item.name != name]
            if len(catalog_xml(tuple([*current, report.skill])).encode("utf-8")) > _MAX_CATALOG_CHARS:
                return self._result_from_report(
                    report,
                    "blocked",
                    "并发安装后 Skill XML 目录将超过 64 KiB",
                )
            if request.action == "update":
                backup = self.backup_root / name / (
                    datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
                    + "-"
                    + index.skills[name].content_digest[:12]
                )
                backup.parent.mkdir(parents=True, exist_ok=True)
                target.replace(backup)
            try:
                prepared.replace(target)
                installed_metadata = parse_skill(target)
                entry = InstalledSkillEntry(
                    name=name,
                    content_digest=installed_metadata.content_digest,
                    description=installed_metadata.description,
                    source=report.source,
                    review_id=report.review_id,
                    installed_at=datetime.now().astimezone(),
                )
                index.skills[name] = entry
                self._write_index(index)
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                if backup is not None and backup.exists():
                    backup.replace(target)
                raise
            if backup is not None:
                self._trim_backups(name)
        return self._result_from_report(
            report,
            "installed",
            f"Skill {name} 已安装；需要时请调用 skill_read",
        )

    def _trim_backups(self, name: str) -> None:
        directory = self.backup_root / name
        values = sorted((path for path in directory.iterdir() if path.is_dir()), key=lambda path: path.name)
        for path in values[:-5]:
            shutil.rmtree(path, ignore_errors=True)

    def _read_index(self) -> SkillIndex:
        try:
            return SkillIndex.model_validate_json(self.index_path.read_text(encoding="utf-8"), strict=True)
        except ValidationError as exc:
            raise ValueError(f"Skill 安装索引损坏：{exc}") from exc

    def _write_index(self, index: SkillIndex) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_name(f".{self.index_path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(index.model_dump_json(indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.index_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_report(self, report: SkillAuditReport) -> None:
        self.audit_root.mkdir(parents=True, exist_ok=True)
        temporary = report.report_path.with_name(f".{report.report_path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
            temporary.replace(report.report_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _result_from_report(
        report: SkillAuditReport,
        status: str,
        message: str,
    ) -> SkillInstallResult:
        return SkillInstallResult(
            status=status,
            message=message,
            review_id=report.review_id,
            name=report.skill.name if report.skill else None,
            report_path=report.report_path,
        )


def _finding(severity: str, code: str, message: str, path: str | None = None) -> SkillAuditFinding:
    return SkillAuditFinding(severity=severity, code=code, message=message, path=path)


def _deduplicate_findings(values: list[SkillAuditFinding]) -> list[SkillAuditFinding]:
    result: list[SkillAuditFinding] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in values:
        key = (item.severity, item.code, item.path)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _github_repository(source: str) -> str | None:
    parsed = urlparse(source)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("GitHub URL 不允许凭据、查询参数或 fragment")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("GitHub 来源必须是仓库根 URL，并通过 ref/skill_path 指定版本和目录")
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub 仓库名称无效")
    return f"https://github.com/{owner}/{repository}.git"


def _validate_git_ref(value: str) -> None:
    if (
        value.startswith("-")
        or ".." in value
        or "@{" in value
        or value.endswith((".", "/"))
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
    ):
        raise ValueError("Git ref 格式无效")


def _detect_license(metadata: SkillMetadata, skill_root: Path, repository_root: Path) -> str | None:
    if metadata.license and _recognized_license(metadata.license):
        return metadata.license
    current = skill_root
    while True:
        for path in current.iterdir():
            if path.is_file() and path.name.lower().startswith(("license", "copying")):
                text = path.read_text(encoding="utf-8", errors="replace")[:20000]
                recognized = _license_from_text(path.name + "\n" + text)
                if recognized:
                    return recognized
        if current == repository_root or repository_root not in current.parents:
            break
        current = current.parent
    return None


def _recognized_license(value: str) -> bool:
    normalized = value.strip().lower().replace(" ", "-")
    return any(name in normalized for name in _LICENSE_NAMES)


def _license_from_text(value: str) -> str | None:
    lowered = value.lower()
    matches = (
        ("Apache-2.0", "apache license"),
        ("MIT", "mit license"),
        ("BSD-3-Clause", "redistribution and use in source and binary forms"),
        ("MPL-2.0", "mozilla public license"),
        ("GPL-3.0", "gnu general public license"),
        ("ISC", "isc license"),
        ("Unlicense", "unlicense"),
    )
    return next((name for name, marker in matches if marker in lowered), None)


def _checkout_git_ignore(checkout_root: Path) -> Callable[[str, list[str]], set[str]]:
    resolved_root = checkout_root.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        return {".git"} if Path(directory).resolve() == resolved_root and ".git" in names else set()

    return ignore


async def _run_command(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise RuntimeError(f"命令执行超时：{arguments[0]}") from exc
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"命令执行失败：{arguments[0]}")
    return stdout.decode("utf-8", errors="replace")
