"""System tray icon and menu — the always-visible entry point for EthOS Drive."""

import logging
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PySide6.QtCore import QTimer

if TYPE_CHECKING:
    from ethos_drive.app import EthosDriveApp

log = logging.getLogger(__name__)

# Status colors
STATUS_COLORS = {
    "idle":     "#4CAF50",   # Green
    "syncing":  "#2196F3",   # Blue
    "paused":   "#FF9800",   # Orange
    "error":    "#F44336",   # Red
    "offline":  "#9E9E9E",   # Gray
}

STATUS_LABELS = {
    "idle":     "Up to date",
    "syncing":  "Syncing...",
    "paused":   "Paused",
    "error":    "Error — click for details",
    "offline":  "Not connected",
}


def _create_status_icon(color_hex: str, size: int = 64) -> QIcon:
    """Generate a simple colored circle icon for the tray."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color_hex))
    painter.setPen(QColor(color_hex).darker(120))
    margin = size // 8
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.end()
    return QIcon(pixmap)


class SystemTray(QSystemTrayIcon):
    """System tray icon with context menu for EthOS Drive."""

    def __init__(self, app: "EthosDriveApp"):
        super().__init__()
        self.drive_app = app
        self._status_icons = {k: _create_status_icon(v) for k, v in STATUS_COLORS.items()}

        self._build_menu()
        self._update_status("offline")

        # Connect signals
        app.status_changed.connect(self._update_status)
        self.activated.connect(self._on_activated)

    def _build_menu(self):
        """Build the tray context menu."""
        menu = QMenu()

        self._status_label = QAction("EthOS Drive", menu)
        self._status_label.setEnabled(False)
        menu.addAction(self._status_label)
        menu.addSeparator()

        self._sync_now_action = QAction("Sync Now", menu)
        self._sync_now_action.triggered.connect(self.drive_app.sync_all)
        menu.addAction(self._sync_now_action)

        self._pause_action = QAction("Pause Syncing", menu)
        self._pause_action.triggered.connect(self._toggle_pause)
        menu.addAction(self._pause_action)

        menu.addSeparator()

        open_action = QAction("Open EthOS Drive", menu)
        open_action.triggered.connect(self._open_main_window)
        menu.addAction(open_action)

        activity_action = QAction("Recent Activity", menu)
        activity_action.triggered.connect(self._open_activity)
        menu.addAction(activity_action)

        menu.addSeparator()

        settings_action = QAction("Settings...", menu)
        settings_action.triggered.connect(self._open_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("Quit EthOS Drive", menu)
        quit_action.triggered.connect(self.drive_app.quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _update_status(self, status: str):
        """Update tray icon and tooltip based on sync status."""
        icon = self._status_icons.get(status, self._status_icons["offline"])
        self.setIcon(icon)

        label = STATUS_LABELS.get(status, status)
        self.setToolTip(f"EthOS Drive — {label}")
        self._status_label.setText(f"EthOS Drive — {label}")

        # Update pause button text
        if status == "paused":
            self._pause_action.setText("Resume Syncing")
        else:
            self._pause_action.setText("Pause Syncing")

    def _toggle_pause(self):
        if self.drive_app.status == "paused":
            self.drive_app.resume()
        else:
            self.drive_app.pause()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._open_main_window()

    def _open_main_window(self):
        from ethos_drive.ui.main_window import MainWindow
        if not hasattr(self, "_main_window") or self._main_window is None:
            self._main_window = MainWindow(self.drive_app)
        self._main_window.show()
        self._main_window.raise_()
        self._main_window.activateWindow()

    def _open_activity(self):
        self._open_main_window()
        if hasattr(self._main_window, "show_activity_tab"):
            self._main_window.show_activity_tab()

    def _open_settings(self):
        self._open_main_window()
        if hasattr(self._main_window, "show_settings_tab"):
            self._main_window.show_settings_tab()
