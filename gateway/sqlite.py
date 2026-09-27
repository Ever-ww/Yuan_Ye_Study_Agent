"""SQLite connection with transaction semantics and deterministic handle cleanup."""

from __future__ import annotations

import sqlite3


class ClosingConnection(sqlite3.Connection):
    """sqlite3's default context manager commits but does not close the file."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()
