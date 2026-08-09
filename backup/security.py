"""Credential isolation helpers for backup and child processes."""

from __future__ import annotations

import os
from collections.abc import Mapping


SENSITIVE_ENV_NAMES = frozenset({
    "YY_BACKUP_PASSPHRASE",
    "YY_GATEWAY_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
})


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


__all__ = ["SENSITIVE_ENV_NAMES", "SensitiveEnvSanitizer"]
