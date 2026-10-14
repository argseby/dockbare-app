"""SQLite persistence.

A single file ``dockbare.db`` in the data directory holds everything that is
not a backup archive: settings, remotes, schedules, the backups index, upload
status per remote, the structured event log, sessions, login attempts, stored
reports and storage samples.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remotes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    volume TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS backups (
    path TEXT PRIMARY KEY,
    volume TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    encrypted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'backup',
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_backups_volume ON backups(volume, created_at);
CREATE TABLE IF NOT EXISTS uploads (
    path TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at REAL NOT NULL,
    error TEXT,
    PRIMARY KEY (path, remote_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    volume TEXT,
    remote_id TEXT,
    backup TEXT,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ip TEXT NOT NULL,
    username TEXT,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_ip ON login_attempts(ip, ts);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    generated_at REAL NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    subject TEXT NOT NULL,
    text TEXT NOT NULL,
    html TEXT NOT NULL,
    channels TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    token TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_checked_at REAL,
    last_ok_at REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    alert_active INTEGER NOT NULL DEFAULT 0,
    status TEXT
);
CREATE TABLE IF NOT EXISTS monitors (
    name TEXT PRIMARY KEY,
    config TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    detail TEXT,
    actions TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_container ON incidents(container, started_at);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT,
    services TEXT,
    status TEXT NOT NULL,
    severity TEXT,
    starts_at REAL NOT NULL,
    ends_at REAL,
    public INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS announcement_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    status TEXT NOT NULL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_updates ON announcement_updates(announcement_id, ts);
CREATE TABLE IF NOT EXISTS container_seen (
    name TEXT PRIMARY KEY,
    first_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    config TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    config TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS storage_samples (
    ts REAL NOT NULL,
    total_bytes INTEGER NOT NULL,
    backup_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_storage_ts ON storage_samples(ts);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)

    # --- low level -----------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- settings ------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def all_settings(self) -> dict[str, Any]:
        out = {}
        for row in self.query("SELECT key, value FROM settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                pass
        return out

    # --- events --------------------------------------------------------------
    def add_event(self, level: str, kind: str, message: str, *, volume: str | None = None,
                  remote_id: str | None = None, backup: str | None = None, data: dict | None = None,
                  ts: float | None = None) -> int:
        cur = self.execute(
            "INSERT INTO events(ts, level, kind, volume, remote_id, backup, message, data) VALUES(?,?,?,?,?,?,?,?)",
            (ts or time.time(), level, kind, volume, remote_id, backup, message, json.dumps(data) if data else None),
        )
        return int(cur.lastrowid)

    def prune_events(self, keep_days: int) -> int:
        cutoff = time.time() - keep_days * 86400
        cur = self.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return cur.rowcount

    # --- storage samples -----------------------------------------------------
    def add_storage_sample(self, total_bytes: int, backup_count: int) -> None:
        self.execute("INSERT INTO storage_samples(ts, total_bytes, backup_count) VALUES(?,?,?)",
                     (time.time(), total_bytes, backup_count))
        # keep at most ~1 year of hourly samples
        self.execute("DELETE FROM storage_samples WHERE ts < ?", (time.time() - 400 * 86400,))


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [row_to_dict(r) for r in rows]
