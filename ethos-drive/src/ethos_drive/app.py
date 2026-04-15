"""EthOS Drive application singleton — coordinates all components."""

import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal, QTimer

from ethos_drive.config import Config, SyncTask
from ethos_drive.sync.state import SyncStateDB
from ethos_drive.sync.engine import SyncEngine
from ethos_drive.sync.watcher import FileWatcher
from ethos_drive.api.client import EthosAPIClient
from ethos_drive.api.realtime import RealtimeClient
from ethos_drive.ui.tray import SystemTray

log = logging.getLogger(__name__)


class EthosDriveApp(QObject):
    """Main application controller."""

    status_changed = Signal(str)       # 'idle', 'syncing', 'paused', 'error', 'offline'
    sync_progress = Signal(dict)       # {task_id, file, progress, speed}
    conflict_detected = Signal(dict)   # {task_id, local_path, remote_path, details}

    def __init__(self):
        super().__init__()
        self.config = Config.load()
        self.state_db = SyncStateDB(self.config.data_dir / "sync_state.db")
        self.api_client: Optional[EthosAPIClient] = None
        self.realtime: Optional[RealtimeClient] = None
        self.engines: dict[str, SyncEngine] = {}
        self.watchers: dict[str, FileWatcher] = {}
        self.tray: Optional[SystemTray] = None
        self._status = "offline"
        self._paused = False

        self._sync_timer = QTimer()
        self._sync_timer.timeout.connect(self._periodic_sync)

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str):
        if self._status != value:
            self._status = value
            self.status_changed.emit(value)
            log.info("App status: %s", value)

    def start(self):
        """Initialize and start the application."""
        log.info("EthOS Drive v%s starting", "1.0.0")

        self.tray = SystemTray(self)
        self.tray.show()

        if self.config.server_url and self.config.has_credentials():
            self._connect()

        # Periodic full sync every 5 minutes as safety net
        self._sync_timer.start(5 * 60 * 1000)

    def _connect(self):
        """Connect to EthOS server."""
        try:
            self.api_client = EthosAPIClient(
                server_url=self.config.server_url,
                verify_ssl=self.config.verify_ssl,
            )

            token = self.config.get_token()
            if token:
                self.api_client.set_token(token)
            else:
                creds = self.config.get_credentials()
                if creds:
                    token = self.api_client.login(creds["username"], creds["password"])
                    if token:
                        self.config.save_token(token)

            if not self.api_client.token:
                self.status = "error"
                return

            # Start real-time connection
            self.realtime = RealtimeClient(
                server_url=self.config.server_url,
                token=self.api_client.token,
            )
            self.realtime.file_changed.connect(self._on_remote_change)
            self.realtime.connect()

            # Initialize sync engines for each task
            self._init_sync_tasks()

            self.status = "idle"
            log.info("Connected to %s", self.config.server_url)

            # Do initial full sync
            self.sync_all()

        except Exception as e:
            log.error("Connection failed: %s", e)
            self.status = "error"

    def _init_sync_tasks(self):
        """Create sync engines and watchers for all configured tasks."""
        for task in self.config.sync_tasks:
            if not task.enabled:
                continue
            self._start_task(task)

    def _start_task(self, task: SyncTask):
        """Start syncing a single task."""
        engine = SyncEngine(
            task=task,
            api_client=self.api_client,
            state_db=self.state_db,
        )
        engine.progress.connect(lambda p, tid=task.id: self.sync_progress.emit({**p, "task_id": tid}))
        engine.conflict.connect(lambda c, tid=task.id: self.conflict_detected.emit({**c, "task_id": tid}))
        self.engines[task.id] = engine

        watcher = FileWatcher(
            local_path=task.local_path,
            filters=task.filters,
        )
        watcher.changes_detected.connect(lambda changes, tid=task.id: self._on_local_changes(tid, changes))
        watcher.start()
        self.watchers[task.id] = watcher

        log.info("Started sync task: %s (%s <-> %s)", task.name, task.local_path, task.remote_path)

    def stop_task(self, task_id: str):
        """Stop a sync task."""
        if task_id in self.watchers:
            self.watchers[task_id].stop()
            del self.watchers[task_id]
        if task_id in self.engines:
            self.engines[task_id].cancel()
            del self.engines[task_id]

    def sync_all(self):
        """Trigger full sync for all tasks."""
        if self._paused or not self.api_client:
            return
        self.status = "syncing"
        for task_id, engine in self.engines.items():
            try:
                engine.full_sync()
            except Exception as e:
                log.error("Sync failed for task %s: %s", task_id, e)
        self.status = "idle"

    def sync_task(self, task_id: str):
        """Trigger sync for a specific task."""
        if self._paused or task_id not in self.engines:
            return
        self.status = "syncing"
        try:
            self.engines[task_id].full_sync()
        except Exception as e:
            log.error("Sync failed for task %s: %s", task_id, e)
        self.status = "idle"

    def pause(self):
        """Pause all sync operations."""
        self._paused = True
        for watcher in self.watchers.values():
            watcher.pause()
        self.status = "paused"

    def resume(self):
        """Resume sync operations."""
        self._paused = False
        for watcher in self.watchers.values():
            watcher.resume()
        self.status = "idle"
        self.sync_all()

    def _on_local_changes(self, task_id: str, changes: list[dict]):
        """Handle detected local file changes."""
        if self._paused or task_id not in self.engines:
            return
        self.status = "syncing"
        try:
            self.engines[task_id].process_local_changes(changes)
        except Exception as e:
            log.error("Error processing local changes for %s: %s", task_id, e)
        self.status = "idle"

    def _on_remote_change(self, data: dict):
        """Handle real-time remote change notification from server."""
        if self._paused:
            return
        remote_path = data.get("path", "")
        for task_id, engine in self.engines.items():
            task = engine.task
            if remote_path.startswith(task.remote_path):
                self.status = "syncing"
                try:
                    engine.process_remote_changes([data])
                except Exception as e:
                    log.error("Error processing remote change for %s: %s", task_id, e)
                self.status = "idle"

    def _periodic_sync(self):
        """Periodic full sync as reliability safety net."""
        if not self._paused:
            self.sync_all()

    def add_task(self, task: SyncTask):
        """Add and start a new sync task."""
        self.config.sync_tasks.append(task)
        self.config.save()
        self._start_task(task)

    def remove_task(self, task_id: str):
        """Remove a sync task."""
        self.stop_task(task_id)
        self.config.sync_tasks = [t for t in self.config.sync_tasks if t.id != task_id]
        self.config.save()
        self.state_db.clear_task(task_id)

    def disconnect(self):
        """Disconnect from server."""
        for task_id in list(self.engines.keys()):
            self.stop_task(task_id)
        if self.realtime:
            self.realtime.disconnect()
        self.api_client = None
        self.status = "offline"

    def quit(self):
        """Shut down the application."""
        log.info("EthOS Drive shutting down")
        self.disconnect()
        self.state_db.close()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
