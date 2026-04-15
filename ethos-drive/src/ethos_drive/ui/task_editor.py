"""Sync task editor dialog — configure folder pairs, filters, and sync options."""

import logging
from typing import Optional, TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QComboBox, QSpinBox, QGroupBox,
    QFormLayout, QFileDialog, QTreeWidget, QTreeWidgetItem,
    QTabWidget, QWidget, QListWidget, QListWidgetItem, QMessageBox,
)
from PySide6.QtCore import Qt

from ethos_drive.config import SyncTask, FilterRule
from ethos_drive.api.client import EthosAPIClient

log = logging.getLogger(__name__)


class TaskEditorDialog(QDialog):
    """Dialog for creating or editing a sync task."""

    def __init__(self, task: SyncTask, api_client: Optional[EthosAPIClient] = None,
                 parent=None):
        super().__init__(parent)
        self.task = task
        self.api = api_client
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Sync Task" if self.task.name == "My Sync" else f"Edit: {self.task.name}")
        self.setMinimumSize(550, 480)

        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # Tab 1: General
        tabs.addTab(self._create_general_tab(), "General")

        # Tab 2: Filters
        tabs.addTab(self._create_filters_tab(), "Filters")

        # Tab 3: Advanced
        tabs.addTab(self._create_advanced_tab(), "Advanced")

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _create_general_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        form.setSpacing(10)

        # Task name
        self._name_input = QLineEdit(self.task.name)
        form.addRow("Task Name:", self._name_input)

        # Local folder
        local_row = QHBoxLayout()
        self._local_input = QLineEdit(self.task.local_path)
        self._local_input.setPlaceholderText("C:\\Users\\you\\EthOS Drive")
        local_row.addWidget(self._local_input)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_local)
        local_row.addWidget(browse_btn)
        form.addRow("Local Folder:", local_row)

        # Remote folder
        remote_row = QHBoxLayout()
        self._remote_input = QLineEdit(self.task.remote_path)
        self._remote_input.setPlaceholderText("/home/admin/Documents")
        remote_row.addWidget(self._remote_input)
        if self.api:
            browse_remote_btn = QPushButton("Browse...")
            browse_remote_btn.clicked.connect(self._browse_remote)
            remote_row.addWidget(browse_remote_btn)
        form.addRow("Remote Folder:", remote_row)

        # Sync direction
        self._direction = QComboBox()
        self._direction.addItems([
            "Two-way (Bidirectional)",
            "Download Only (Server → PC)",
            "Upload Only (PC → Server)",
        ])
        dir_map = {"bidirectional": 0, "download_only": 1, "upload_only": 2}
        self._direction.setCurrentIndex(dir_map.get(self.task.direction, 0))
        form.addRow("Sync Direction:", self._direction)

        # Conflict strategy
        self._conflict = QComboBox()
        self._conflict.addItems([
            "Keep Newer File",
            "Keep Server Version",
            "Keep Local Version",
            "Keep Both (rename local)",
            "Ask Me Each Time",
        ])
        strat_map = {"keep_newer": 0, "keep_server": 1, "keep_local": 2, "keep_both": 3, "ask": 4}
        self._conflict.setCurrentIndex(strat_map.get(self.task.conflict_strategy, 0))
        form.addRow("On Conflict:", self._conflict)

        # Enabled
        self._enabled = QCheckBox("Enable this sync task")
        self._enabled.setChecked(self.task.enabled)
        form.addRow("", self._enabled)

        return widget

    def _create_filters_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        layout.addWidget(QLabel("Exclude files matching these patterns:"))

        self._filter_list = QListWidget()
        for rule in self.task.filters:
            item = QListWidgetItem(f"{rule.type}: {rule.pattern}")
            if rule.max_size_mb:
                item.setText(f"{rule.type}: > {rule.max_size_mb} MB")
            item.setData(Qt.UserRole, rule)
            self._filter_list.addItem(item)
        layout.addWidget(self._filter_list)

        # Add filter controls
        add_row = QHBoxLayout()
        self._filter_pattern = QLineEdit()
        self._filter_pattern.setPlaceholderText("*.tmp, *.log, node_modules/")
        add_row.addWidget(self._filter_pattern)

        add_btn = QPushButton("Add Exclude")
        add_btn.clicked.connect(self._add_filter)
        add_row.addWidget(add_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda: self._filter_list.takeItem(self._filter_list.currentRow()))
        add_row.addWidget(remove_btn)
        layout.addLayout(add_row)

        # Max file size
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Skip files larger than:"))
        self._max_size = QSpinBox()
        self._max_size.setRange(0, 100000)
        self._max_size.setSuffix(" MB")
        self._max_size.setSpecialValueText("No limit")
        layout.addLayout(size_row)
        size_row.addWidget(self._max_size)

        # Sync hidden files
        self._sync_hidden = QCheckBox("Sync hidden files (dotfiles)")
        self._sync_hidden.setChecked(self.task.sync_hidden)
        layout.addWidget(self._sync_hidden)

        return widget

    def _create_advanced_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        # Bandwidth limits
        form.addRow(QLabel("Bandwidth Limits:"))

        self._upload_limit = QSpinBox()
        self._upload_limit.setRange(0, 1000000)
        self._upload_limit.setSuffix(" KB/s")
        self._upload_limit.setSpecialValueText("Unlimited")
        self._upload_limit.setValue(self.task.max_upload_kbps)
        form.addRow("Max Upload Speed:", self._upload_limit)

        self._download_limit = QSpinBox()
        self._download_limit.setRange(0, 1000000)
        self._download_limit.setSuffix(" KB/s")
        self._download_limit.setSpecialValueText("Unlimited")
        self._download_limit.setValue(self.task.max_download_kbps)
        form.addRow("Max Download Speed:", self._download_limit)

        # Sync interval
        self._sync_interval = QSpinBox()
        self._sync_interval.setRange(30, 3600)
        self._sync_interval.setSuffix(" seconds")
        self._sync_interval.setValue(self.task.sync_interval_seconds)
        form.addRow("Full Sync Interval:", self._sync_interval)

        return widget

    def _browse_local(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Local Folder",
                                                   self._local_input.text())
        if folder:
            self._local_input.setText(folder)

    def _browse_remote(self):
        """Simple remote folder browser using the API."""
        if not self.api:
            return
        # For now, just let user type the path
        # TODO: implement tree browser dialog
        pass

    def _add_filter(self):
        pattern = self._filter_pattern.text().strip()
        if pattern:
            rule = FilterRule(type="exclude", pattern=pattern)
            item = QListWidgetItem(f"exclude: {pattern}")
            item.setData(Qt.UserRole, rule)
            self._filter_list.addItem(item)
            self._filter_pattern.clear()

    def _save(self):
        name = self._name_input.text().strip()
        local_path = self._local_input.text().strip()
        remote_path = self._remote_input.text().strip()

        if not name:
            QMessageBox.warning(self, "Error", "Please enter a task name.")
            return
        if not local_path:
            QMessageBox.warning(self, "Error", "Please select a local folder.")
            return
        if not remote_path:
            QMessageBox.warning(self, "Error", "Please enter a remote folder path.")
            return

        self.task.name = name
        self.task.local_path = local_path
        self.task.remote_path = remote_path
        self.task.enabled = self._enabled.isChecked()

        dir_map = {0: "bidirectional", 1: "download_only", 2: "upload_only"}
        self.task.direction = dir_map.get(self._direction.currentIndex(), "bidirectional")

        strat_map = {0: "keep_newer", 1: "keep_server", 2: "keep_local", 3: "keep_both", 4: "ask"}
        self.task.conflict_strategy = strat_map.get(self._conflict.currentIndex(), "keep_newer")

        # Collect filters
        self.task.filters = []
        for i in range(self._filter_list.count()):
            rule = self._filter_list.item(i).data(Qt.UserRole)
            if isinstance(rule, FilterRule):
                self.task.filters.append(rule)

        if self._max_size.value() > 0:
            self.task.filters.append(FilterRule(
                type="exclude", max_size_mb=self._max_size.value(), apply_to="files"
            ))

        self.task.sync_hidden = self._sync_hidden.isChecked()
        self.task.max_upload_kbps = self._upload_limit.value()
        self.task.max_download_kbps = self._download_limit.value()
        self.task.sync_interval_seconds = self._sync_interval.value()

        self.accept()
