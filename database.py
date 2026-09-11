"""SQLite connection boundary shared by persistence-oriented services."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def connect_database(
    path: Path,
    busy_timeout_ms: int,
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    connection.execute("PRAGMA synchronous = NORMAL")
    try:
        with connection:
            yield connection
    finally:
        connection.close()
