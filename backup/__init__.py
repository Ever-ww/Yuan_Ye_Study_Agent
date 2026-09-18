"""Consistent Snapshot Manifest/Object Store backup and whole-home restore."""

from .archive import ArchiveHeader, EncryptedBackupArchive
from .catalog import AgentHomeDurabilityCatalog, UnsafeArchiveEntryError
from .control import (
    ExternalControlLock,
    RestoreFenceActiveError,
    RestoreJournal,
    assert_restore_inactive,
    external_control_root,
    read_restore_fence,
)
from .maintenance import (
    AgentHomeMaintenanceCoordinator,
    AgentHomeWriteGate,
    MaintenanceBlockedError,
    MaintenanceParticipant,
    WriteScope,
)
from .models import *
from .restore import RestoreConfirmationError, RestoreRecoveryRequired, RestoreService
from .security import (
    BackupSecret,
    SensitiveEnvSanitizer,
    SystemManagedBackupKeyStore,
    SystemManagedKeyUnavailable,
)
from .scheduler import BackupScheduler
from .service import BackupService
from .snapshot import read_manifest

__all__ = [
    "AgentHomeDurabilityCatalog",
    "AgentHomeMaintenanceCoordinator",
    "AgentHomeWriteGate",
    "ArchiveHeader",
    "BackupSecret",
    "BackupService",
    "BackupScheduler",
    "EncryptedBackupArchive",
    "ExternalControlLock",
    "MaintenanceBlockedError",
    "MaintenanceParticipant",
    "RestoreConfirmationError",
    "RestoreFenceActiveError",
    "RestoreJournal",
    "RestoreRecoveryRequired",
    "RestoreService",
    "SensitiveEnvSanitizer",
    "SystemManagedBackupKeyStore",
    "SystemManagedKeyUnavailable",
    "UnsafeArchiveEntryError",
    "WriteScope",
    "assert_restore_inactive",
    "external_control_root",
    "read_restore_fence",
    "read_manifest",
]
