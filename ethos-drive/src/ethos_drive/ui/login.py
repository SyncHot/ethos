"""Login and server connection dialog."""

import logging
from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QMessageBox, QProgressBar,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont

from ethos_drive.config import Config
from ethos_drive.api.client import EthosAPIClient, APIError

log = logging.getLogger(__name__)


class _LoginWorker(QThread):
    """Background thread for login to keep UI responsive."""
    finished = Signal(bool, str)  # success, token_or_error

    def __init__(self, server_url: str, username: str, password: str, verify_ssl: bool):
        super().__init__()
        self.server_url = server_url
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl

    def run(self):
        try:
            client = EthosAPIClient(self.server_url, verify_ssl=self.verify_ssl)
            token = client.login(self.username, self.password)
            if token:
                self.finished.emit(True, token)
            else:
                self.finished.emit(False, "Login failed — invalid credentials")
            client.close()
        except APIError as e:
            self.finished.emit(False, str(e))
        except Exception as e:
            self.finished.emit(False, f"Connection error: {e}")


class LoginDialog(QDialog):
    """Server connection and authentication dialog."""

    login_successful = Signal(str, str, str)  # server_url, username, token

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self._worker = None
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Connect to EthOS Server")
        self.setFixedSize(420, 340)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        # Title
        title = QLabel("EthOS Drive")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Connect to your EthOS server")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: #888; margin-bottom: 8px;")
        layout.addWidget(subtitle)

        # Server URL
        layout.addWidget(QLabel("Server Address:"))
        self._server_input = QLineEdit()
        self._server_input.setPlaceholderText("https://your-ethos-server:9000")
        self._server_input.setText(self.config.server_url)
        layout.addWidget(self._server_input)

        # Username
        layout.addWidget(QLabel("Username:"))
        self._username_input = QLineEdit()
        self._username_input.setPlaceholderText("admin")
        self._username_input.setText(self.config.username)
        layout.addWidget(self._username_input)

        # Password
        layout.addWidget(QLabel("Password:"))
        self._password_input = QLineEdit()
        self._password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_input.setPlaceholderText("••••••••")
        layout.addWidget(self._password_input)

        # SSL verification
        self._verify_ssl = QCheckBox("Verify SSL certificate")
        self._verify_ssl.setChecked(self.config.verify_ssl)
        layout.addWidget(self._verify_ssl)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # Indeterminate
        self._progress.hide()
        layout.addWidget(self._progress)

        # Buttons
        btn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setDefault(True)
        self._connect_btn.clicked.connect(self._on_connect)
        btn_row.addStretch()
        btn_row.addWidget(self._connect_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        # Enter key triggers connect
        self._password_input.returnPressed.connect(self._on_connect)

    def _on_connect(self):
        server = self._server_input.text().strip()
        username = self._username_input.text().strip()
        password = self._password_input.text()

        if not server:
            QMessageBox.warning(self, "Error", "Please enter a server address.")
            return
        if not username:
            QMessageBox.warning(self, "Error", "Please enter a username.")
            return
        if not password:
            QMessageBox.warning(self, "Error", "Please enter a password.")
            return

        # Ensure URL has scheme
        if not server.startswith(("http://", "https://")):
            server = "https://" + server

        self._connect_btn.setEnabled(False)
        self._progress.show()

        self._worker = _LoginWorker(server, username, password, self._verify_ssl.isChecked())
        self._worker.finished.connect(self._on_login_result)
        self._worker.start()

    def _on_login_result(self, success: bool, result: str):
        self._progress.hide()
        self._connect_btn.setEnabled(True)

        if success:
            server = self._server_input.text().strip()
            if not server.startswith(("http://", "https://")):
                server = "https://" + server
            username = self._username_input.text().strip()

            # Save credentials
            self.config.server_url = server
            self.config.username = username
            self.config.verify_ssl = self._verify_ssl.isChecked()
            self.config.save_credentials(username, self._password_input.text())
            self.config.save_token(result)
            self.config.save()

            self.login_successful.emit(server, username, result)
            self.accept()
        else:
            QMessageBox.critical(self, "Connection Failed", result)
