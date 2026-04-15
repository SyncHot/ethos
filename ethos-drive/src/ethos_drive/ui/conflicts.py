"""Conflict resolution UI — shows pending conflicts and lets user resolve them."""

import logging
import time
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QPushButton, QHeaderView, QMessageBox,
    QComboBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

if TYPE_CHECKING:
    from ethos_drive.app import EthosDriveApp

log = logging.getLogger(__name__)


class ConflictsWidget(QWidget):
    """Shows pending sync conflicts and provides resolution controls."""

    def __init__(self, app: "EthosDriveApp"):
        super().__init__()
        self.drive_app = app
        self._setup_ui()

        app.conflict_detected.connect(self._on_conflict)

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel("Sync Conflicts")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        header.addWidget(title)
        header.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        info = QLabel(
            "These files were modified on both your PC and the server since the last sync. "
            "Choose how to resolve each conflict."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #888; margin-bottom: 8px;")
        layout.addWidget(info)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels([
            "File", "Local Size", "Remote Size",
            "Local Modified", "Remote Modified", "Resolution"
        ])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().hide()
        self._table.setAlternatingRowColors(True)
        layout.addWidget(self._table)

        # Bulk actions
        actions = QHBoxLayout()

        keep_server_btn = QPushButton("Keep All Server")
        keep_server_btn.setStyleSheet("color: #2196F3;")
        keep_server_btn.clicked.connect(lambda: self._resolve_all("keep_server"))
        actions.addWidget(keep_server_btn)

        keep_local_btn = QPushButton("Keep All Local")
        keep_local_btn.setStyleSheet("color: #4CAF50;")
        keep_local_btn.clicked.connect(lambda: self._resolve_all("keep_local"))
        actions.addWidget(keep_local_btn)

        keep_newer_btn = QPushButton("Keep All Newer")
        keep_newer_btn.clicked.connect(lambda: self._resolve_all("keep_newer"))
        actions.addWidget(keep_newer_btn)

        keep_both_btn = QPushButton("Keep All Both")
        keep_both_btn.clicked.connect(lambda: self._resolve_all("keep_both"))
        actions.addWidget(keep_both_btn)

        actions.addStretch()
        layout.addLayout(actions)

    def _on_conflict(self, data: dict):
        """Handle newly detected conflict."""
        self._refresh()
        # Show notification
        if self.drive_app.config.show_notifications and hasattr(self.drive_app, 'tray'):
            self.drive_app.tray.showMessage(
                "Sync Conflict",
                f"Conflict detected: {data.get('path', 'unknown file')}",
                self.drive_app.tray.MessageIcon.Warning,
                5000,
            )

    def _refresh(self):
        """Reload conflicts from database."""
        self._table.setRowCount(0)

        for task in self.drive_app.config.sync_tasks:
            conflicts = self.drive_app.state_db.get_pending_conflicts(task.id)
            for c in conflicts:
                row = self._table.rowCount()
                self._table.insertRow(row)

                self._table.setItem(row, 0, QTableWidgetItem(c["path"]))
                self._table.setItem(row, 1, QTableWidgetItem(self._fmt_size(c.get("local_size", 0))))
                self._table.setItem(row, 2, QTableWidgetItem(self._fmt_size(c.get("remote_size", 0))))

                local_time = self._fmt_time(c.get("local_mtime_ns", 0))
                remote_time = self._fmt_time(c.get("remote_mtime_ns", 0))
                self._table.setItem(row, 3, QTableWidgetItem(local_time))
                self._table.setItem(row, 4, QTableWidgetItem(remote_time))

                # Resolution combo
                combo = QComboBox()
                combo.addItems(["Keep Newer", "Keep Server", "Keep Local", "Keep Both"])
                combo.setProperty("conflict_id", c["id"])
                combo.setProperty("task_id", task.id)
                self._table.setCellWidget(row, 5, combo)

    def _resolve_all(self, strategy: str):
        """Resolve all visible conflicts with the same strategy."""
        count = self._table.rowCount()
        if count == 0:
            return

        reply = QMessageBox.question(
            self, "Resolve Conflicts",
            f"Resolve all {count} conflicts with '{strategy.replace('_', ' ')}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for row in range(count):
            combo = self._table.cellWidget(row, 5)
            if combo:
                conflict_id = combo.property("conflict_id")
                self.drive_app.state_db.resolve_conflict(conflict_id, strategy)

        self._refresh()
        log.info("Resolved %d conflicts with strategy: %s", count, strategy)

    @staticmethod
    def _fmt_size(size: int) -> str:
        if not size:
            return "—"
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        else:
            return f"{size / (1024 * 1024 * 1024):.1f} GB"

    @staticmethod
    def _fmt_time(mtime_ns: int) -> str:
        if not mtime_ns:
            return "—"
        ts = mtime_ns / 1_000_000_000
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
