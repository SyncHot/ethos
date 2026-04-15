"""File version tracking — maintains local record of file versions."""

import logging
import time
from typing import Optional

from ethos_drive.sync.state import SyncStateDB

log = logging.getLogger(__name__)


class VersionTracker:
    """Tracks file version history using the sync state database."""

    def __init__(self, state_db: SyncStateDB, task_id: str, max_versions: int = 32):
        self.db = state_db
        self.task_id = task_id
        self.max_versions = max_versions

    def record_version(self, path: str, size: int, xxhash: str,
                       mtime_ns: int, server_version_id: int = None):
        """Record a new version of a file before it gets overwritten."""
        versions = self.db.get_versions(self.task_id, path)

        # Get next version number
        if versions:
            next_num = max(v["version_num"] for v in versions) + 1
        else:
            next_num = 1

        self.db.add_version(
            task_id=self.task_id,
            path=path,
            version_num=next_num,
            size=size,
            xxhash=xxhash,
            mtime_ns=mtime_ns,
            server_version_id=server_version_id,
        )

        # Prune old versions beyond max
        if len(versions) >= self.max_versions:
            self._prune_old(path, keep=self.max_versions)

        log.debug("Recorded version %d for %s", next_num, path)
        return next_num

    def get_history(self, path: str) -> list[dict]:
        """Get version history for a file, newest first."""
        return self.db.get_versions(self.task_id, path)

    def _prune_old(self, path: str, keep: int):
        """Remove old versions beyond the keep limit."""
        versions = self.db.get_versions(self.task_id, path)
        if len(versions) > keep:
            to_delete = versions[keep:]
            for v in to_delete:
                self.db._conn.execute(
                    "DELETE FROM versions WHERE id = ?", (v["id"],)
                )
            self.db._conn.commit()
            log.debug("Pruned %d old versions for %s", len(to_delete), path)
