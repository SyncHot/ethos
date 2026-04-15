"""Transfer manager — handles chunked uploads/downloads with throttling and progress."""

import logging
import os
import time
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ethos_drive.api.client import EthosAPIClient

log = logging.getLogger(__name__)


class TransferProgress:
    """Tracks progress of a single file transfer."""

    def __init__(self, path: str, total_bytes: int, direction: str):
        self.path = path
        self.total_bytes = total_bytes
        self.direction = direction  # 'upload' or 'download'
        self.transferred_bytes = 0
        self.start_time = time.time()
        self.last_update = self.start_time

    @property
    def percent(self) -> float:
        if self.total_bytes == 0:
            return 100.0
        return min(100.0, (self.transferred_bytes / self.total_bytes) * 100)

    @property
    def speed_bps(self) -> float:
        elapsed = time.time() - self.start_time
        return self.transferred_bytes / elapsed if elapsed > 0 else 0

    @property
    def eta_seconds(self) -> float:
        speed = self.speed_bps
        if speed <= 0:
            return 0
        remaining = self.total_bytes - self.transferred_bytes
        return remaining / speed

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "direction": self.direction,
            "total": self.total_bytes,
            "transferred": self.transferred_bytes,
            "percent": round(self.percent, 1),
            "speed_bps": int(self.speed_bps),
            "eta_seconds": int(self.eta_seconds),
        }


class TransferManager(QObject):
    """Manages file uploads and downloads with concurrency and throttling.

    Provides:
    - Concurrent transfers (configurable max)
    - Bandwidth throttling (separate upload/download limits)
    - Progress tracking with speed and ETA
    - Retry on transient failures
    - Cancel/pause support
    """

    progress_updated = Signal(dict)     # TransferProgress.as_dict()
    transfer_complete = Signal(dict)    # {path, direction, success, error}
    queue_empty = Signal()

    def __init__(self, api_client: EthosAPIClient,
                 max_concurrent: int = 3,
                 max_upload_kbps: int = 0,
                 max_download_kbps: int = 0,
                 chunk_size: int = 4 * 1024 * 1024,
                 max_retries: int = 3):
        super().__init__()
        self.api = api_client
        self.max_concurrent = max_concurrent
        self.max_upload_kbps = max_upload_kbps
        self.max_download_kbps = max_download_kbps
        self.chunk_size = chunk_size
        self.max_retries = max_retries

        self._queue: list[dict] = []
        self._active: dict[str, TransferProgress] = {}
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._paused = threading.Event()
        self._paused.set()  # Not paused by default
        self._sem = threading.Semaphore(max_concurrent)

    def enqueue_upload(self, local_path: str, remote_path: str):
        """Add a file to the upload queue."""
        with self._lock:
            self._queue.append({
                "direction": "upload",
                "local_path": local_path,
                "remote_path": remote_path,
            })
        self._process_queue()

    def enqueue_download(self, remote_path: str, local_path: str):
        """Add a file to the download queue."""
        with self._lock:
            self._queue.append({
                "direction": "download",
                "local_path": local_path,
                "remote_path": remote_path,
            })
        self._process_queue()

    def _process_queue(self):
        """Start transfers from the queue up to max concurrent limit."""
        with self._lock:
            while self._queue and len(self._active) < self.max_concurrent:
                item = self._queue.pop(0)
                t = threading.Thread(target=self._do_transfer, args=(item,), daemon=True)
                t.start()

    def _do_transfer(self, item: dict):
        """Execute a single transfer with retries and throttling."""
        direction = item["direction"]
        local_path = item["local_path"]
        remote_path = item["remote_path"]
        rel_path = remote_path

        self._sem.acquire()
        try:
            file_size = 0
            if direction == "upload" and os.path.exists(local_path):
                file_size = os.path.getsize(local_path)

            progress = TransferProgress(rel_path, file_size, direction)
            with self._lock:
                self._active[rel_path] = progress

            success = False
            error_msg = ""

            for attempt in range(1, self.max_retries + 1):
                if self._cancel.is_set():
                    break

                self._paused.wait()  # Block if paused

                try:
                    if direction == "upload":
                        self._throttled_upload(local_path, remote_path, progress)
                    else:
                        self._throttled_download(remote_path, local_path, progress)
                    success = True
                    break
                except Exception as e:
                    error_msg = str(e)
                    log.warning("Transfer attempt %d/%d failed for %s: %s",
                                attempt, self.max_retries, rel_path, e)
                    if attempt < self.max_retries:
                        time.sleep(2 ** attempt)  # Exponential backoff

            with self._lock:
                self._active.pop(rel_path, None)

            self.transfer_complete.emit({
                "path": rel_path,
                "direction": direction,
                "success": success,
                "error": error_msg if not success else "",
            })

        finally:
            self._sem.release()
            self._process_queue()

            # Check if queue is empty
            with self._lock:
                if not self._queue and not self._active:
                    self.queue_empty.emit()

    def _throttled_upload(self, local_path: str, remote_path: str,
                          progress: TransferProgress):
        """Upload with bandwidth throttling."""
        def on_progress(transferred, total):
            progress.transferred_bytes = transferred
            progress.total_bytes = total
            self.progress_updated.emit(progress.as_dict())
            self._throttle(transferred, progress.start_time, self.max_upload_kbps)

        self.api.upload_file(local_path, remote_path,
                             chunk_size=self.chunk_size,
                             progress_callback=on_progress)

    def _throttled_download(self, remote_path: str, local_path: str,
                            progress: TransferProgress):
        """Download with bandwidth throttling."""
        def on_progress(transferred, total):
            progress.transferred_bytes = transferred
            progress.total_bytes = total
            self.progress_updated.emit(progress.as_dict())
            self._throttle(transferred, progress.start_time, self.max_download_kbps)

        self.api.download_file(remote_path, local_path,
                               progress_callback=on_progress)

    def _throttle(self, bytes_transferred: int, start_time: float, limit_kbps: int):
        """Sleep to enforce bandwidth limit."""
        if limit_kbps <= 0:
            return
        limit_bps = limit_kbps * 1024
        elapsed = time.time() - start_time
        expected_time = bytes_transferred / limit_bps
        if expected_time > elapsed:
            time.sleep(expected_time - elapsed)

    def pause(self):
        self._paused.clear()

    def resume(self):
        self._paused.set()

    def cancel_all(self):
        self._cancel.set()
        with self._lock:
            self._queue.clear()

    @property
    def active_transfers(self) -> list[dict]:
        with self._lock:
            return [p.as_dict() for p in self._active.values()]

    @property
    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)
