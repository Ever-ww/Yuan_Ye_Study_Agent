"""Consistent encrypted Agent Home backup and whole-home restore."""

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
from .security import SensitiveEnvSanitizer
from .scheduler import BackupScheduler
from .service import BackupService

__all__ = [
    "AgentHomeDurabilityCatalog",
    "AgentHomeMaintenanceCoordinator",
    "AgentHomeWriteGate",
    "ArchiveHeader",
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
    "UnsafeArchiveEntryError",
    "WriteScope",
    "assert_restore_inactive",
    "external_control_root",
    "read_restore_fence",
]
