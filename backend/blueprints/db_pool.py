"""
EthOS — SQLite Connection Pool with WAL mode.
Shared by all blueprint DB modules for consistent, efficient database access.
"""

import sqlite3
import os
import threading

_init_lock = threading.Lock()
_initialized_dbs = set()

BUSY_TIMEOUT_MS = 5000
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024  # 64 MB


def _apply_pragmas(conn):
    """Apply WAL mode and performance pragmas to a connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")  # 8 MB cache
    return conn


def get_pooled_db(db_path):
    """Create a WAL-tuned SQLite connection. Caller must close when done."""
    with _init_lock:
        if db_path not in _initialized_dbs:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            _initialized_dbs.add(db_path)

    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def return_to_pool(db_path, conn):
    """Close connection (kept for API compat, no-op pool)."""
    try:
        conn.close()
    except Exception:
        pass
