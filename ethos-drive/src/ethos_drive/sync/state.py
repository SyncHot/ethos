"""Local sync state database — tracks per-file sync metadata in SQLite."""

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class SyncStateDB:
    """SQLite database tracking the sync state of every file.

    Each file record holds:
    - path (relative POSIX path within the sync root)
    - local fingerprint (size, mtime_ns, xxhash at last sync)
    - remote fingerprint (size, mtime_ns, xxhash at last sync)
    - sync_version (monotonic counter incremented on each sync)
    - status ('synced', 'pending_upload', 'pending_download', 'conflict', 'deleted')
    """

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        """Per-thread connection (SQLite is not thread-safe by default)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, timeout=30)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS file_state (
                task_id     TEXT NOT NULL,
                path        TEXT NOT NULL,
                is_dir      INTEGER DEFAULT 0,
                local_size  INTEGER,
                local_mtime_ns INTEGER,
                local_xxhash TEXT,
                remote_size INTEGER,
                remote_mtime_ns INTEGER,
                remote_xxhash TEXT,
                sync_version INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'synced',
                last_synced REAL,
                PRIMARY KEY (task_id, path)
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT NOT NULL,
                timestamp   REAL NOT NULL,
                action      TEXT NOT NULL,
                path        TEXT NOT NULL,
                detail      TEXT,
                success     INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS conflicts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT NOT NULL,
                path        TEXT NOT NULL,
                local_size  INTEGER,
                local_mtime_ns INTEGER,
                local_xxhash TEXT,
                remote_size INTEGER,
                remote_mtime_ns INTEGER,
                remote_xxhash TEXT,
                detected_at REAL NOT NULL,
                resolved_at REAL,
                resolution  TEXT,
                status      TEXT DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT NOT NULL,
                path        TEXT NOT NULL,
                version_num INTEGER NOT NULL,
                size        INTEGER,
                xxhash      TEXT,
                mtime_ns    INTEGER,
                created_at  REAL NOT NULL,
                server_version_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS task_state (
                task_id         TEXT PRIMARY KEY,
                last_full_sync  REAL,
                sync_version    INTEGER DEFAULT 0,
                files_synced    INTEGER DEFAULT 0,
                total_bytes     INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_file_state_status ON file_state(task_id, status);
            CREATE INDEX IF NOT EXISTS idx_sync_log_task ON sync_log(task_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_conflicts_task ON conflicts(task_id, status);
            CREATE INDEX IF NOT EXISTS idx_versions_path ON versions(task_id, path);
        """)
        conn.commit()

    # --- File State ---

    def get_file(self, task_id: str, path: str) -> Optional[dict]:
        """Get state for a single file."""
        row = self._conn.execute(
            "SELECT * FROM file_state WHERE task_id = ? AND path = ?",
            (task_id, path)
        ).fetchone()
        return dict(row) if row else None

    def get_all_files(self, task_id: str) -> list[dict]:
        """Get all file states for a task."""
        rows = self._conn.execute(
            "SELECT * FROM file_state WHERE task_id = ?", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_files_by_status(self, task_id: str, status: str) -> list[dict]:
        """Get files with a specific status."""
        rows = self._conn.execute(
            "SELECT * FROM file_state WHERE task_id = ? AND status = ?",
            (task_id, status)
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_file(self, task_id: str, path: str, **kwargs):
        """Insert or update a file state record."""
        existing = self.get_file(task_id, path)
        now = time.time()

        if existing:
            sets = []
            vals = []
            for k, v in kwargs.items():
                sets.append(f"{k} = ?")
                vals.append(v)
            if sets:
                vals.extend([task_id, path])
                self._conn.execute(
                    f"UPDATE file_state SET {', '.join(sets)} WHERE task_id = ? AND path = ?",
                    vals
                )
        else:
            kwargs["task_id"] = task_id
            kwargs["path"] = path
            kwargs.setdefault("last_synced", now)
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            self._conn.execute(
                f"INSERT INTO file_state ({cols}) VALUES ({placeholders})",
                list(kwargs.values())
            )
        self._conn.commit()

    def mark_synced(self, task_id: str, path: str,
                    local_size: int, local_mtime_ns: int, local_xxhash: str,
                    remote_size: int, remote_mtime_ns: int, remote_xxhash: str):
        """Mark a file as successfully synced."""
        self.upsert_file(
            task_id, path,
            local_size=local_size,
            local_mtime_ns=local_mtime_ns,
            local_xxhash=local_xxhash,
            remote_size=remote_size,
            remote_mtime_ns=remote_mtime_ns,
            remote_xxhash=remote_xxhash,
            status="synced",
            last_synced=time.time(),
        )

    def mark_deleted(self, task_id: str, path: str):
        """Remove a file from tracking."""
        self._conn.execute(
            "DELETE FROM file_state WHERE task_id = ? AND path = ?",
            (task_id, path)
        )
        self._conn.commit()

    def clear_task(self, task_id: str):
        """Remove all state for a task."""
        for table in ("file_state", "sync_log", "conflicts", "versions", "task_state"):
            self._conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
        self._conn.commit()

    # --- Sync Log ---

    def log_action(self, task_id: str, action: str, path: str,
                   detail: str = "", success: bool = True):
        """Record a sync action in the log."""
        self._conn.execute(
            "INSERT INTO sync_log (task_id, timestamp, action, path, detail, success) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, time.time(), action, path, detail, int(success))
        )
        self._conn.commit()

    def get_recent_log(self, task_id: str, limit: int = 100) -> list[dict]:
        """Get recent sync log entries."""
        rows = self._conn.execute(
            "SELECT * FROM sync_log WHERE task_id = ? ORDER BY timestamp DESC LIMIT ?",
            (task_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_log_since(self, task_id: str, since: float) -> list[dict]:
        """Get log entries since a timestamp."""
        rows = self._conn.execute(
            "SELECT * FROM sync_log WHERE task_id = ? AND timestamp > ? ORDER BY timestamp",
            (task_id, since)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Conflicts ---

    def add_conflict(self, task_id: str, path: str,
                     local_fp: dict, remote_fp: dict) -> int:
        """Record a sync conflict. Returns conflict ID."""
        cur = self._conn.execute(
            "INSERT INTO conflicts (task_id, path, local_size, local_mtime_ns, local_xxhash, "
            "remote_size, remote_mtime_ns, remote_xxhash, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, path,
             local_fp.get("size"), local_fp.get("mtime_ns"), local_fp.get("xxhash"),
             remote_fp.get("size"), remote_fp.get("mtime_ns"), remote_fp.get("xxhash"),
             time.time())
        )
        self._conn.commit()
        return cur.lastrowid

    def resolve_conflict(self, conflict_id: int, resolution: str):
        """Mark a conflict as resolved."""
        self._conn.execute(
            "UPDATE conflicts SET status = 'resolved', resolved_at = ?, resolution = ? WHERE id = ?",
            (time.time(), resolution, conflict_id)
        )
        self._conn.commit()

    def get_pending_conflicts(self, task_id: str) -> list[dict]:
        """Get unresolved conflicts for a task."""
        rows = self._conn.execute(
            "SELECT * FROM conflicts WHERE task_id = ? AND status = 'pending' ORDER BY detected_at",
            (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Versions ---

    def add_version(self, task_id: str, path: str, version_num: int,
                    size: int, xxhash: str, mtime_ns: int,
                    server_version_id: int = None):
        """Record a file version."""
        self._conn.execute(
            "INSERT INTO versions (task_id, path, version_num, size, xxhash, mtime_ns, "
            "created_at, server_version_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, path, version_num, size, xxhash, mtime_ns, time.time(), server_version_id)
        )
        self._conn.commit()

    def get_versions(self, task_id: str, path: str) -> list[dict]:
        """Get version history for a file."""
        rows = self._conn.execute(
            "SELECT * FROM versions WHERE task_id = ? AND path = ? ORDER BY version_num DESC",
            (task_id, path)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Task State ---

    def get_task_state(self, task_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM task_state WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_task_state(self, task_id: str, **kwargs):
        existing = self.get_task_state(task_id)
        if existing:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            self._conn.execute(
                f"UPDATE task_state SET {sets} WHERE task_id = ?",
                [*kwargs.values(), task_id]
            )
        else:
            kwargs["task_id"] = task_id
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            self._conn.execute(
                f"INSERT INTO task_state ({cols}) VALUES ({placeholders})",
                list(kwargs.values())
            )
        self._conn.commit()

    # --- Housekeeping ---

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
