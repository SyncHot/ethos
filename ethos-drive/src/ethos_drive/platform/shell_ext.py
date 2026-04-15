"""Windows Explorer shell extension stubs for overlay icons.

NOTE: Full shell extension requires a COM server (typically C++ or C# DLL).
This module provides the Python-side logic for managing overlay icon state.
A compiled DLL would need to be registered separately.

For now, this tracks which files are synced/syncing/error for potential
future shell extension integration.
"""

import logging
import os
import threading
from enum import Enum

log = logging.getLogger(__name__)


class OverlayStatus(Enum):
    """File sync overlay status."""
    NONE = 0
    SYNCED = 1        # Green checkmark
    SYNCING = 2       # Blue arrows
    PENDING = 3       # Clock icon
    ERROR = 4         # Red X
    CONFLICT = 5      # Yellow warning


class OverlayManager:
    """Tracks sync status for files to support Explorer overlay icons.

    In a full implementation, this would communicate with a registered
    Windows shell extension DLL via a named pipe or shared memory.
    """

    def __init__(self):
        self._statuses: dict[str, OverlayStatus] = {}
        self._lock = threading.Lock()

    def set_status(self, path: str, status: OverlayStatus):
        """Set the overlay status for a file or directory."""
        normalized = os.path.normpath(path).lower()
        with self._lock:
            if status == OverlayStatus.NONE:
                self._statuses.pop(normalized, None)
            else:
                self._statuses[normalized] = status

    def get_status(self, path: str) -> OverlayStatus:
        """Get the overlay status for a path."""
        normalized = os.path.normpath(path).lower()
        with self._lock:
            return self._statuses.get(normalized, OverlayStatus.NONE)

    def set_synced(self, path: str):
        self.set_status(path, OverlayStatus.SYNCED)

    def set_syncing(self, path: str):
        self.set_status(path, OverlayStatus.SYNCING)

    def set_error(self, path: str):
        self.set_status(path, OverlayStatus.ERROR)

    def set_conflict(self, path: str):
        self.set_status(path, OverlayStatus.CONFLICT)

    def clear(self, path: str):
        self.set_status(path, OverlayStatus.NONE)

    def clear_all(self):
        with self._lock:
            self._statuses.clear()
