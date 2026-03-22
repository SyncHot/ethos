"""
EthOS — SQLite Connection Pool with WAL mode.
Shared by all blueprint DB modules for consistent, efficient database access.
"""

import sqlite3
import os
import threading
from queue import Queue, Empty

_pools = {}
_pools_lock = threading.Lock()

# Default pool settings
POOL_SIZE = 5
BUSY_TIMEOUT_MS = 5000
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024  # 64 MB


class _ConnectionPool:
    """Thread-safe SQLite connection pool with WAL and tuned pragmas."""

    def __init__(self, db_path, pool_size=POOL_SIZE):
        self._db_path = db_path
        self._pool = Queue(maxsize=pool_size)
        self._pool_size = pool_size
        self._created = 0
        self._lock = threading.Lock()

    def _make_conn(self):
        conn = sqlite3.connect(self._db_path, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8 MB cache
        return conn

    def get(self):
        try:
            return self._pool.get_nowait()
        except Empty:
            with self._lock:
                if self._created < self._pool_size:
                    self._created += 1
                    return self._make_conn()
            # All slots taken, wait for a return
            return self._pool.get(timeout=BUSY_TIMEOUT_MS / 1000)

    def put(self, conn):
        try:
            self._pool.put_nowait(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def close_all(self):
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Exception:
                pass


def get_pooled_db(db_path):
    """Get a connection from the pool for the given database path."""
    with _pools_lock:
        if db_path not in _pools:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            _pools[db_path] = _ConnectionPool(db_path)
    pool = _pools[db_path]
    return pool.get()


def return_to_pool(db_path, conn):
    """Return a connection back to its pool."""
    with _pools_lock:
        pool = _pools.get(db_path)
    if pool:
        pool.put(conn)
    else:
        try:
            conn.close()
        except Exception:
            pass
