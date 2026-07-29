"""Gateway 本机凭据与浏览器会话辅助。"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


class GatewayCredentials:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.token_path = self.directory / "token"
        self.directory.mkdir(parents=True, exist_ok=True)

    def load_or_create(self) -> str:
        if self.token_path.exists():
            token = self.token_path.read_text(encoding="utf-8").strip()
            if len(token) >= 32:
                return token
        token = secrets.token_urlsafe(48)
        temporary = self.token_path.with_suffix(".tmp")
        temporary.write_text(token + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.token_path)
        return token


def bearer_value(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.casefold() != "bearer" or not value:
        return None
    return value
