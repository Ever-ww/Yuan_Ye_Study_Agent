"""Credential isolation helpers for backup and child processes."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4


SENSITIVE_ENV_NAMES = frozenset({
    "YY_BACKUP_PASSPHRASE",
    "YY_GATEWAY_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
})


class SystemManagedKeyUnavailable(RuntimeError):
    """The operating-system credential backend cannot provide the backup key."""


@dataclass(frozen=True)
class BackupSecret:
    """Resolved archive secret plus non-secret provenance metadata."""

    value: str
    mode: Literal["passphrase", "os_managed"]
    key_id: str | None = None


class SystemManagedBackupKeyStore:
    """Store one random backup key behind the current OS user identity.

    Windows uses DPAPI and stores only the protected blob outside ``.yy``.
    macOS and Linux use their native credential helpers when available. There
    is deliberately no plaintext-file fallback.
    """

    _SERVICE = "yy-agent.backup"

    def __init__(self, agent_root: Path) -> None:
        self.agent_root = agent_root.resolve()
        identity = os.path.normcase(str(self.agent_root)).encode("utf-8")
        self.key_id = hashlib.sha256(b"yy-agent-backup-key-v1\0" + identity).hexdigest()
        self.control_dir = self.agent_root / ".yy-backups" / "control" / "credentials"
        self.protected_path = self.control_dir / "backup-key.dpapi.json"
        self.lock_path = self.control_dir / "backup-key.lock"

    @property
    def provider_name(self) -> str:
        if os.name == "nt":
            return "windows-dpapi-current-user"
        if platform.system() == "Darwin":
            return "macos-keychain"
        return "linux-secret-service"

    def get(self, key_id: str | None = None) -> BackupSecret | None:
        if key_id is not None and key_id != self.key_id:
            raise SystemManagedKeyUnavailable(
                "该备份属于另一个 Agent Home 的系统托管密钥；请使用原系统账户或手动口令备份",
            )
        if os.name == "nt":
            value = self._windows_get()
        elif platform.system() == "Darwin":
            value = self._macos_get()
        else:
            value = self._linux_get()
        return BackupSecret(value, "os_managed", self.key_id) if value else None

    def get_or_create(self) -> BackupSecret:
        current = self.get()
        if current is not None:
            return current
        from .control import ExternalControlLock

        lock = ExternalControlLock(self.lock_path)
        lock.acquire()
        try:
            current = self.get()
            if current is not None:
                return current
            value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
            if os.name == "nt":
                self._windows_put(value)
            elif platform.system() == "Darwin":
                self._macos_put(value)
            else:
                self._linux_put(value)
            resolved = self.get()
            if resolved is None:
                raise SystemManagedKeyUnavailable("系统凭据后端写入后无法重新读取备份密钥")
            return resolved
        finally:
            lock.close()

    def status(self) -> dict[str, object]:
        supported = (
            os.name == "nt"
            or (platform.system() == "Darwin" and shutil.which("security") is not None)
            or (platform.system() != "Darwin" and shutil.which("secret-tool") is not None)
        )
        try:
            available = self.get() is not None if supported else False
            error = None
        except SystemManagedKeyUnavailable as exc:
            available = False
            error = str(exc)
        return {
            "provider": self.provider_name,
            "supported": supported,
            "key_available": available,
            "key_id": self.key_id,
            "error": error,
        }

    def _entropy(self) -> bytes:
        return hashlib.sha256(b"yy-agent-backup-dpapi-v1\0" + self.key_id.encode("ascii")).digest()

    def _windows_get(self) -> str | None:
        if not self.protected_path.is_file():
            return None
        try:
            payload = json.loads(self.protected_path.read_text(encoding="utf-8"))
            if (
                payload.get("version") != 1
                or payload.get("provider") != self.provider_name
                or payload.get("key_id") != self.key_id
            ):
                raise SystemManagedKeyUnavailable("系统托管备份密钥元数据不匹配")
            protected = base64.b64decode(payload["protected_key"], validate=True)
            return self._dpapi_unprotect(protected, self._entropy()).decode("utf-8")
        except SystemManagedKeyUnavailable:
            raise
        except Exception as exc:
            raise SystemManagedKeyUnavailable("Windows DPAPI 备份密钥损坏或当前用户无权解密") from exc

    def _windows_put(self, value: str) -> None:
        if self.protected_path.exists():
            return
        protected = self._dpapi_protect(value.encode("utf-8"), self._entropy())
        payload = {
            "version": 1,
            "provider": self.provider_name,
            "key_id": self.key_id,
            "protected_key": base64.b64encode(protected).decode("ascii"),
            "created_at": datetime.now().astimezone().isoformat(),
        }
        self._atomic_write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _dpapi_protect(value: bytes, entropy: bytes) -> bytes:
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        value_buffer = ctypes.create_string_buffer(value)
        entropy_buffer = ctypes.create_string_buffer(entropy)
        source = DataBlob(len(value), ctypes.cast(value_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        optional = DataBlob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = DataBlob()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source), "YY Agent backup key", ctypes.byref(optional),
            None, None, 0x1, ctypes.byref(target),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            ctypes.windll.kernel32.LocalFree(target.data)

    @staticmethod
    def _dpapi_unprotect(value: bytes, entropy: bytes) -> bytes:
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        value_buffer = ctypes.create_string_buffer(value)
        entropy_buffer = ctypes.create_string_buffer(entropy)
        source = DataBlob(len(value), ctypes.cast(value_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        optional = DataBlob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = DataBlob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, ctypes.byref(optional), None, None, 0x1, ctypes.byref(target),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            ctypes.windll.kernel32.LocalFree(target.data)

    def _macos_get(self) -> str | None:
        executable = shutil.which("security")
        if executable is None:
            raise SystemManagedKeyUnavailable("macOS Keychain 命令不可用")
        result = subprocess.run(
            [executable, "find-generic-password", "-s", self._SERVICE, "-a", self.key_id, "-w"],
            env=SensitiveEnvSanitizer.subprocess_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise SystemManagedKeyUnavailable("无法从 macOS Keychain 读取备份密钥")
        return result.stdout.rstrip("\r\n") or None

    def _macos_put(self, value: str) -> None:
        executable = shutil.which("security")
        if executable is None:
            raise SystemManagedKeyUnavailable("macOS Keychain 命令不可用")
        result = subprocess.run(
            [executable, "add-generic-password", "-U", "-s", self._SERVICE,
             "-a", self.key_id, "-w", value],
            env=SensitiveEnvSanitizer.subprocess_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            raise SystemManagedKeyUnavailable("无法写入 macOS Keychain 备份密钥")

    def _linux_get(self) -> str | None:
        executable = shutil.which("secret-tool")
        if executable is None:
            raise SystemManagedKeyUnavailable(
                "Linux Secret Service 不可用；请安装 secret-tool 或改用 passphrase 模式",
            )
        result = subprocess.run(
            [executable, "lookup", "service", self._SERVICE, "key-id", self.key_id],
            env=SensitiveEnvSanitizer.subprocess_env(
                allowed_names={"DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"},
            ),
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\r\n") or None

    def _linux_put(self, value: str) -> None:
        executable = shutil.which("secret-tool")
        if executable is None:
            raise SystemManagedKeyUnavailable(
                "Linux Secret Service 不可用；请安装 secret-tool 或改用 passphrase 模式",
            )
        result = subprocess.run(
            [executable, "store", "--label", "YY Agent automatic backup key",
             "service", self._SERVICE, "key-id", self.key_id],
            env=SensitiveEnvSanitizer.subprocess_env(
                allowed_names={"DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"},
            ),
            input=value, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            raise SystemManagedKeyUnavailable("无法写入 Linux Secret Service 备份密钥")

    def _atomic_write(self, text: str) -> None:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.protected_path.with_name(f".{self.protected_path.name}.{uuid4().hex}.partial")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.protected_path)
        finally:
            temporary.unlink(missing_ok=True)


class SensitiveEnvSanitizer:
    """Build a deliberately small environment for untrusted subprocesses."""

    BASE_ALLOWLIST = frozenset({
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "TMPDIR", "LANG", "LC_ALL", "TERM", "COLORTERM", "USERPROFILE", "HOME",
        "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    })

    @classmethod
    def subprocess_env(
        cls,
        additions: Mapping[str, str] | None = None,
        *,
        allowed_names: set[str] | frozenset[str] | None = None,
        trusted_sensitive_names: set[str] | frozenset[str] | None = None,
    ) -> dict[str, str]:
        names = cls.BASE_ALLOWLIST | frozenset(allowed_names or ())
        trusted = frozenset(name.upper() for name in (trusted_sensitive_names or ()))
        result = {
            key: value for key, value in os.environ.items()
            if key.upper() in names and (
                key.upper() not in SENSITIVE_ENV_NAMES or key.upper() in trusted
            )
        }
        for key, value in (additions or {}).items():
            if key.upper() in SENSITIVE_ENV_NAMES and key.upper() not in trusted:
                raise ValueError(f"不能向非可信子进程传递敏感变量：{key}")
            result[key] = value
        return result

    @staticmethod
    def consume_backup_passphrase() -> str | None:
        """Read once and remove it from the global process environment."""
        return os.environ.pop("YY_BACKUP_PASSPHRASE", None)


__all__ = [
    "BackupSecret",
    "SENSITIVE_ENV_NAMES",
    "SensitiveEnvSanitizer",
    "SystemManagedBackupKeyStore",
    "SystemManagedKeyUnavailable",
]
