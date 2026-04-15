"""Real-time SocketIO client for server change notifications."""

import logging
import threading
from typing import Optional

import socketio
from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class RealtimeClient(QObject):
    """SocketIO client for real-time events from EthOS server.

    Emits Qt signals when the server reports file changes, so the sync
    engine can react immediately without polling.
    """

    connected = Signal()
    disconnected = Signal()
    file_changed = Signal(dict)     # {action, path, size, mtime_ns}
    sync_conflict = Signal(dict)    # {path, server_version, ...}
    server_error = Signal(str)

    def __init__(self, server_url: str, token: str):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.token = token
        self._sio: Optional[socketio.Client] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def connect(self):
        """Connect to the server SocketIO endpoint."""
        if self._running:
            return

        self._sio = socketio.Client(
            reconnection=True,
            reconnection_attempts=0,  # Retry forever
            reconnection_delay=2,
            reconnection_delay_max=30,
            logger=False,
        )

        self._register_handlers()

        self._running = True
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._thread.start()

    def _connect_loop(self):
        """Background thread: connect and block on wait()."""
        try:
            self._sio.connect(
                self.server_url,
                auth={"token": self.token},
                transports=["websocket", "polling"],
                wait_timeout=10,
            )
            self._sio.wait()
        except Exception as e:
            log.error("SocketIO connection error: %s", e)
        finally:
            self._running = False

    def _register_handlers(self):
        """Register SocketIO event handlers."""

        @self._sio.event
        def connect():
            log.info("SocketIO connected to %s", self.server_url)
            # Subscribe to sync-drive channel
            self._sio.emit("sync_drive_subscribe", {"token": self.token})
            self.connected.emit()

        @self._sio.event
        def disconnect():
            log.warning("SocketIO disconnected")
            self.disconnected.emit()

        @self._sio.event
        def connect_error(data):
            log.error("SocketIO connection error: %s", data)

        # --- Sync Drive Events ---

        @self._sio.on("sync_drive_file_changed")
        def on_file_changed(data):
            """Server reports a file was created/modified/deleted."""
            log.debug("Remote change: %s %s", data.get("action"), data.get("path"))
            self.file_changed.emit(data)

        @self._sio.on("sync_drive_conflict")
        def on_conflict(data):
            """Server detected a sync conflict."""
            log.warning("Conflict: %s", data.get("path"))
            self.sync_conflict.emit(data)

        @self._sio.on("sync_drive_error")
        def on_error(data):
            """Server reports an error."""
            msg = data.get("message", "Unknown error")
            log.error("Server error: %s", msg)
            self.server_error.emit(msg)

    def disconnect(self):
        """Disconnect from server."""
        self._running = False
        if self._sio:
            try:
                self._sio.disconnect()
            except Exception:
                pass
            self._sio = None

    def emit(self, event: str, data: dict):
        """Send an event to the server."""
        if self._sio and self._sio.connected:
            self._sio.emit(event, data)

    @property
    def is_connected(self) -> bool:
        return self._sio is not None and self._sio.connected
