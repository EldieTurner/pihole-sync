"""Persistent state (SQLite): per-replica applied snapshots + history."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""

HISTORY_LIMIT = 100


class StateStore:
    def __init__(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(p), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- generic key/value ------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )
        self._conn.commit()

    # -- helpers ----------------------------------------------------------
    def applied_snapshot(self, replica: str) -> Optional[dict]:
        return self.get(f"applied:{replica}")

    def set_applied_snapshot(self, replica: str, snapshot: dict) -> None:
        self.set(f"applied:{replica}", snapshot)

    def append_history(self, entry: dict) -> None:
        hist = self.get("history", [])
        hist.append(entry)
        self.set("history", hist[-HISTORY_LIMIT:])

    def close(self) -> None:
        self._conn.close()