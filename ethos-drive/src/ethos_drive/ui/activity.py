"""Activity and sync history view."""

import logging
import time
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QComboBox, QPushButton, QHeaderView,
    QProgressBar,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor

if TYPE_CHECKING:
    from ethos_drive.app import EthosDriveApp

log = logging.getLogger(__name__)

ACTION_ICONS = {
    "upload": "↑",
    "download": "↓",
    "delete_local": "🗑 Local",
    "delete_remote": "🗑 Remote",
    "conflict": "⚠",
}


class ActivityWidget(QWidget):
    """Real-time sync activity log and transfer progress."""

    def __init__(self, app: "EthosDriveApp"):
        super().__init__()
        self.drive_app = app
        self._setup_ui()

        # Connect signals
        app.sync_progress.connect(self._on_progress)

        # Refresh timer
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._refresh_log)
        self._refresh_timer.start(5000)

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Active transfers section
        transfers_label = QLabel("Active Transfers")
        transfers_label.setStyleSheet("font-weight: bold; font-size: 14px; margin-top: 8px;")
        layout.addWidget(transfers_label)

        self._transfers_table = QTableWidget(0, 4)
        self._transfers_table.setHorizontalHeaderLabels(["File", "Direction", "Progress", "Speed"])
        self._transfers_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._transfers_table.setMaximumHeight(150)
        self._transfers_table.verticalHeader().hide()
        layout.addWidget(self._transfers_table)

        # Filter bar
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))
        self._action_filter = QComboBox()
        self._action_filter.addItems(["All", "Uploads", "Downloads", "Deletes", "Errors"])
        self._action_filter.currentTextChanged.connect(self._refresh_log)
        filter_row.addWidget(self._action_filter)

        self._task_filter = QComboBox()
        self._task_filter.addItem("All Tasks")
        for task in self.drive_app.config.sync_tasks:
            self._task_filter.addItem(task.name, task.id)
        self._task_filter.currentTextChanged.connect(self._refresh_log)
        filter_row.addWidget(self._task_filter)

        filter_row.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_log)
        filter_row.addWidget(refresh_btn)
        layout.addLayout(filter_row)

        # History table
        history_label = QLabel("Sync History")
        history_label.setStyleSheet("font-weight: bold; font-size: 14px; margin-top: 8px;")
        layout.addWidget(history_label)

        self._history_table = QTableWidget(0, 5)
        self._history_table.setHorizontalHeaderLabels(["Time", "Action", "File", "Task", "Status"])
        header = self._history_table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._history_table.verticalHeader().hide()
        self._history_table.setAlternatingRowColors(True)
        layout.addWidget(self._history_table)

    def _on_progress(self, data: dict):
        """Update active transfers table."""
        path = data.get("path", "")
        direction = data.get("direction", "")
        percent = data.get("percent", 0)
        speed = data.get("speed_bps", 0)

        # Find or add row
        found = False
        for row in range(self._transfers_table.rowCount()):
            if self._transfers_table.item(row, 0) and self._transfers_table.item(row, 0).text() == path:
                self._update_transfer_row(row, path, direction, percent, speed)
                found = True
                break

        if not found:
            row = self._transfers_table.rowCount()
            self._transfers_table.insertRow(row)
            self._update_transfer_row(row, path, direction, percent, speed)

        # Remove completed transfers
        if percent >= 100:
            QTimer.singleShot(2000, lambda: self._remove_transfer(path))

    def _update_transfer_row(self, row: int, path: str, direction: str,
                             percent: float, speed: int):
        self._transfers_table.setItem(row, 0, QTableWidgetItem(path))
        self._transfers_table.setItem(row, 1, QTableWidgetItem("↑" if direction == "upload" else "↓"))
        self._transfers_table.setItem(row, 2, QTableWidgetItem(f"{percent:.0f}%"))

        speed_str = self._format_speed(speed)
        self._transfers_table.setItem(row, 3, QTableWidgetItem(speed_str))

    def _remove_transfer(self, path: str):
        for row in range(self._transfers_table.rowCount()):
            item = self._transfers_table.item(row, 0)
            if item and item.text() == path:
                self._transfers_table.removeRow(row)
                break

    def _refresh_log(self):
        """Refresh the history table from the database."""
        self._history_table.setRowCount(0)

        for task in self.drive_app.config.sync_tasks:
            task_filter = self._task_filter.currentData()
            if task_filter and task_filter != task.id:
                continue

            entries = self.drive_app.state_db.get_recent_log(task.id, limit=200)

            action_filter = self._action_filter.currentText()
            for entry in entries:
                action = entry.get("action", "")
                if action_filter == "Uploads" and action != "upload":
                    continue
                if action_filter == "Downloads" and action != "download":
                    continue
                if action_filter == "Deletes" and "delete" not in action:
                    continue
                if action_filter == "Errors" and entry.get("success", 1):
                    continue

                row = self._history_table.rowCount()
                self._history_table.insertRow(row)

                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["timestamp"]))
                self._history_table.setItem(row, 0, QTableWidgetItem(ts))

                icon = ACTION_ICONS.get(action, "•")
                self._history_table.setItem(row, 1, QTableWidgetItem(f"{icon} {action}"))
                self._history_table.setItem(row, 2, QTableWidgetItem(entry.get("path", "")))
                self._history_table.setItem(row, 3, QTableWidgetItem(task.name))

                status = "✓" if entry.get("success") else "✗ " + entry.get("detail", "")
                item = QTableWidgetItem(status)
                if not entry.get("success"):
                    item.setForeground(QColor("#F44336"))
                self._history_table.setItem(row, 4, item)

    @staticmethod
    def _format_speed(bps: int) -> str:
        if bps < 1024:
            return f"{bps} B/s"
        elif bps < 1024 * 1024:
            return f"{bps / 1024:.1f} KB/s"
        else:
            return f"{bps / (1024 * 1024):.1f} MB/s"
