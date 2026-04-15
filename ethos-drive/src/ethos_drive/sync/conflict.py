"""Conflict detection and resolution strategies."""

import logging
import os
import shutil
import time
from typing import Optional

from ethos_drive.utils.paths import conflict_name

log = logging.getLogger(__name__)


class ConflictInfo:
    """Details about a sync conflict."""

    def __init__(self, path: str, local_fp: dict, remote_fp: dict):
        self.path = path
        self.local_fp = local_fp    # {size, mtime_ns, xxhash}
        self.remote_fp = remote_fp  # {size, mtime_ns, xxhash}
        self.detected_at = time.time()

    @property
    def local_newer(self) -> bool:
        return (self.local_fp.get("mtime_ns", 0) > self.remote_fp.get("mtime_ns", 0))

    def __repr__(self):
        return f"Conflict({self.path}, local={self.local_fp}, remote={self.remote_fp})"


# Conflict resolution strategies
STRATEGY_KEEP_NEWER = "keep_newer"
STRATEGY_KEEP_SERVER = "keep_server"
STRATEGY_KEEP_LOCAL = "keep_local"
STRATEGY_KEEP_BOTH = "keep_both"
STRATEGY_ASK = "ask"


def detect_conflict(path: str, local_fp: dict, remote_fp: dict,
                    last_synced_fp: dict) -> Optional[ConflictInfo]:
    """Detect if both local and remote changed since last sync.

    A conflict occurs when:
    - local file changed (fingerprint differs from last synced local)
    - remote file changed (fingerprint differs from last synced remote)

    No conflict if only one side changed.
    """
    local_changed = (
        local_fp.get("xxhash") != last_synced_fp.get("local_xxhash") or
        local_fp.get("size") != last_synced_fp.get("local_size")
    )
    remote_changed = (
        remote_fp.get("xxhash") != last_synced_fp.get("remote_xxhash") or
        remote_fp.get("size") != last_synced_fp.get("remote_size")
    )

    if local_changed and remote_changed:
        return ConflictInfo(path, local_fp, remote_fp)

    return None


def resolve_conflict(conflict: ConflictInfo, strategy: str,
                     local_root: str) -> dict:
    """Apply a conflict resolution strategy.

    Returns: {action: 'upload'|'download'|'keep_both', ...}
    """
    if strategy == STRATEGY_KEEP_NEWER:
        if conflict.local_newer:
            return {"action": "upload", "reason": "local is newer"}
        else:
            return {"action": "download", "reason": "remote is newer"}

    elif strategy == STRATEGY_KEEP_SERVER:
        return {"action": "download", "reason": "keep server version"}

    elif strategy == STRATEGY_KEEP_LOCAL:
        return {"action": "upload", "reason": "keep local version"}

    elif strategy == STRATEGY_KEEP_BOTH:
        # Rename local copy with conflict suffix
        local_abs = os.path.join(local_root, conflict.path.replace("/", os.sep))
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        hostname = os.environ.get("COMPUTERNAME", "local")
        suffix = f"conflict-{hostname}-{timestamp}"
        new_name = conflict_name(local_abs, suffix)

        # Rename local file
        if os.path.exists(local_abs):
            try:
                os.rename(local_abs, new_name)
                log.info("Conflict: renamed local %s -> %s", local_abs, new_name)
            except OSError as e:
                log.error("Cannot rename for conflict: %s", e)
                return {"action": "skip", "reason": f"rename failed: {e}"}

        return {
            "action": "download",
            "reason": "keep both — local renamed",
            "renamed_local": new_name,
        }

    elif strategy == STRATEGY_ASK:
        return {"action": "ask", "reason": "user decision required"}

    else:
        log.warning("Unknown conflict strategy: %s, defaulting to keep_newer", strategy)
        return resolve_conflict(conflict, STRATEGY_KEEP_NEWER, local_root)
