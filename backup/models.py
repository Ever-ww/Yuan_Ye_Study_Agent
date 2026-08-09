"""Agent Home backup, maintenance, and restore contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MaintenanceState(str, Enum):
    RUNNING = "running"
    DRAINING = "draining"
    FROZEN = "frozen"
    RESUMING = "resuming"
    FAILED = "failed"


class DurabilityClass(str, Enum):
    CANONICAL = "canonical"
    REBUILDABLE = "rebuildable"
    TRANSIENT = "transient"
    EXTERNAL = "external"


class RestoreState(str, Enum):
    PREPARING = "preparing"
    PREPARED = "prepared"
    GATEWAY_STOPPED = "gateway_stopped"
    OLD_HOME_RENAMED = "old_home_renamed"
    NEW_HOME_INSTALLED = "new_home_installed"
    MIGRATED = "migrated"
    GATEWAY_STARTED = "gateway_started"
    HEALTH_VERIFIED = "health_verified"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


class QuiesceResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    participant: str
    maintenance_epoch: int = Field(ge=1)
    acknowledged: bool
    safe_boundary: str | None = None
    active_operations: tuple[str, ...] = ()
    failure_reason: str | None = None
    stale: bool = False


class MaintenanceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: MaintenanceState
    maintenance_epoch: int = Field(ge=0)
    reason: str | None = None
    started_at: datetime | None = None
    participant_status: dict[str, QuiesceResult] = Field(default_factory=dict)
    failure_reason: str | None = None


class ExternalDependency(BaseModel):
    model_config = ConfigDict(frozen=True)

    dependency_id: str
    kind: str
    path: str | None = None
    repository_identity: str | None = None
    required_commits: tuple[str, ...] = ()
    status: Literal["available", "offline", "mapped", "incompatible"] = "available"


class BackupFileRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    durability: DurabilityClass


class BackupManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    backup_id: str
    created_at: datetime
    kind: Literal["automatic", "manual", "rescue"]
    agent_version: str
    backup_format_version: int = 1
    schema_versions: dict[str, int | str] = Field(default_factory=dict)
    maintenance_epoch: int = Field(ge=1)
    source_platform: str
    source_timezone: str
    agent_home_logical_size: int = Field(ge=0)
    files: tuple[BackupFileRecord, ...]
    external_dependencies: tuple[ExternalDependency, ...] = ()
    skill_manifest_hashes: dict[str, str] = Field(default_factory=dict)
    harness_snapshots: tuple[str, ...] = ()


class BackupRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    backup_id: str
    path: Path
    kind: Literal["automatic", "manual", "rescue"]
    verification_status: Literal["pending", "verified", "failed"]
    size_bytes: int = Field(ge=0)
    created_at: datetime
    retention_class: str


class BackupCreateRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    passphrase: str = Field(min_length=1, max_length=4096)
    output: Path | None = None
    kind: Literal["automatic", "manual", "rescue"] = "manual"


class BackupVerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    backup_id: str | None = None
    valid: bool
    gcm_authenticated: bool
    manifest_valid: bool
    file_hashes_valid: bool
    sqlite_valid: bool
    indexes_valid: bool
    checkpoint_store_valid: bool
    external_dependency_status: dict[str, str] = Field(default_factory=dict)
    errors: tuple[str, ...] = ()


class RestoreFence(BaseModel):
    model_config = ConfigDict(frozen=True)

    restore_id: str
    journal_path: Path
    backup_format_version: int
    target_agent_root_identity: str
    created_at: datetime


class RestoreJournalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=1)
    previous_record_hash: str
    record_type: str
    action_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    record_hash: str
    timestamp: datetime


class RestoreTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    restore_id: str
    state: RestoreState
    archive_path: Path
    target_agent_root: Path
    staging_path: Path
    rollback_path: Path
    rescue_backup_path: Path | None = None
    last_action_id: str | None = None
    failure_reason: str | None = None


class RestorePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    backup_id: str
    archive_path: Path
    created_at: datetime
    agent_version: str
    schema_versions: dict[str, int | str]
    archive_size: int = Field(ge=0)
    logical_size: int = Field(ge=0)
    estimated_peak_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    external_dependencies: tuple[ExternalDependency, ...] = ()
    path_changes: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "BackupFileRecord",
    "BackupCreateRequest",
    "BackupManifest",
    "BackupRecord",
    "BackupVerificationResult",
    "DurabilityClass",
    "ExternalDependency",
    "MaintenanceSnapshot",
    "MaintenanceState",
    "QuiesceResult",
    "RestoreFence",
    "RestoreJournalRecord",
    "RestorePlan",
    "RestoreState",
    "RestoreTransaction",
]
