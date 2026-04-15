"""File system watcher — monitors local directories for real-time changes."""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent,
    FileDeletedEvent, FileMovedEvent, DirCreatedEvent, DirDeletedEvent,
    DirMovedEvent,
)
from PySide6.QtCore import QObject, Signal

from ethos_drive.config import FilterRule
from ethos_drive.utils.paths import local_to_remote, is_hidden

log = logging.getLogger(__name__)

# Debounce window — coalesce rapid changes to same file
DEBOUNCE_SECONDS = 2.0


class _EventCollector(FileSystemEventHandler):
    """Collects filesystem events and debounces them."""

    def __init__(self, root: str, sync_hidden: bool, filters: list[FilterRule]):
        super().__init__()
        self.root = root
        self.sync_hidden = sync_hidden
        self.filters = filters
        self._pending: dict[str, dict] = {}  # rel_path -> {action, time, ...}
        self._lock = threading.Lock()

    def on_created(self, event):
        self._record(event.src_path, "created", event.is_directory)

    def on_modified(self, event):
        if not event.is_directory:
            self._record(event.src_path, "modified", False)

    def on_deleted(self, event):
        self._record(event.src_path, "deleted", event.is_directory)

    def on_moved(self, event):
        self._record(event.src_path, "deleted", event.is_directory)
        self._record(event.dest_path, "created", event.is_directory)

    def _record(self, abs_path: str, action: str, is_dir: bool):
        """Record a change with debouncing."""
        name = os.path.basename(abs_path)

        # Skip hidden
        if not self.sync_hidden and is_hidden(abs_path):
            return

        # Skip temp files
        if name.endswith((".ethos-tmp", ".ethos-conflict", ".partial")):
            return

        rel = local_to_remote(abs_path, self.root)

        with self._lock:
            self._pending[rel] = {
                "action": action,
                "path": rel,
                "abs_path": abs_path,
                "is_dir": is_dir,
                "time": time.time(),
            }

    def flush(self) -> list[dict]:
        """Return all changes that have been stable for DEBOUNCE_SECONDS."""
        now = time.time()
        ready = []
        with self._lock:
            expired = []
            for rel, change in self._pending.items():
                if now - change["time"] >= DEBOUNCE_SECONDS:
                    ready.append(change)
                    expired.append(rel)
            for rel in expired:
                del self._pending[rel]
        return ready


class FileWatcher(QObject):
    """Watches a local directory for changes and emits signals.

    Uses watchdog for cross-platform file system monitoring with
    debouncing to coalesce rapid changes.
    """

    changes_detected = Signal(list)  # list[dict] of change records

    def __init__(self, local_path: str, filters: list[FilterRule] = None,
                 sync_hidden: bool = False):
        super().__init__()
        self.local_path = local_path
        self.filters = filters or []
        self.sync_hidden = sync_hidden
        self._observer: Optional[Observer] = None
        self._collector: Optional[_EventCollector] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._paused = False

    def start(self):
        """Start watching the directory."""
        if self._running:
            return

        if not os.path.isdir(self.local_path):
            log.error("Watch path does not exist: %s", self.local_path)
            return

        self._collector = _EventCollector(self.local_path, self.sync_hidden, self.filters)
        self._observer = Observer()
        self._observer.schedule(self._collector, self.local_path, recursive=True)
        self._observer.start()
        self._running = True

        # Start flush poll thread
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        log.info("Watching: %s", self.local_path)

    def _poll_loop(self):
        """Periodically flush debounced changes and emit signal."""
        while self._running:
            time.sleep(1.0)
            if self._paused or not self._collector:
                continue
            changes = self._collector.flush()
            if changes:
                log.debug("Detected %d local changes", len(changes))
                self.changes_detected.emit(changes)

    def stop(self):
        """Stop watching."""
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        self._collector = None
        log.info("Stopped watching: %s", self.local_path)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False
