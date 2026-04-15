"""Configuration management and credential storage."""

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import keyring
import platformdirs
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

APP_NAME = "EthOS Drive"
APP_AUTHOR = "EthOS"
KEYRING_SERVICE = "ethos-drive"


class FilterRule(BaseModel):
    """A sync filter rule."""
    type: str = "exclude"                # 'exclude' or 'include'
    pattern: str = ""                    # Glob pattern (e.g., '*.tmp')
    max_size_mb: Optional[float] = None  # Max file size in MB
    apply_to: str = "both"               # 'files', 'folders', 'both'


class SyncTask(BaseModel):
    """A sync task configuration — one local/remote folder pair."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "My Sync"
    enabled: bool = True
    local_path: str = ""
    remote_path: str = ""
    direction: str = "bidirectional"  # 'bidirectional', 'download_only', 'upload_only'
    conflict_strategy: str = "keep_newer"  # 'keep_newer', 'keep_server', 'keep_local', 'keep_both', 'ask'
    filters: list[FilterRule] = Field(default_factory=list)
    max_upload_kbps: int = 0       # 0 = unlimited
    max_download_kbps: int = 0     # 0 = unlimited
    sync_hidden: bool = False
    sync_interval_seconds: int = 300  # Periodic full-sync interval
    selective_paths: Optional[list[str]] = None  # If set, only sync these subpaths


class Config(BaseModel):
    """Application configuration."""
    server_url: str = ""
    username: str = ""
    verify_ssl: bool = True
    device_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    device_name: str = Field(default_factory=lambda: os.environ.get("COMPUTERNAME", "My PC"))
    sync_tasks: list[SyncTask] = Field(default_factory=list)
    show_notifications: bool = True
    start_minimized: bool = True
    auto_start: bool = True
    log_level: str = "INFO"
    max_concurrent_transfers: int = 3
    chunk_size_kb: int = 4096  # 4 MB chunks

    # Computed paths (not serialized)
    _config_dir: Path = Path()
    _data_dir: Path = Path()

    class Config:
        underscore_attrs_are_private = True

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from disk, or create default."""
        config_dir = Path(platformdirs.user_config_dir(APP_NAME, APP_AUTHOR))
        data_dir = Path(platformdirs.user_data_dir(APP_NAME, APP_AUTHOR))
        config_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        config_file = config_dir / "config.json"
        if config_file.exists():
            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
                cfg = cls(**data)
            except Exception as e:
                log.error("Failed to load config: %s, using defaults", e)
                cfg = cls()
        else:
            cfg = cls()

        cfg._config_dir = config_dir
        cfg._data_dir = data_dir

        log.info("Config dir: %s", config_dir)
        log.info("Data dir: %s", data_dir)
        return cfg

    def save(self):
        """Persist configuration to disk."""
        config_file = self._config_dir / "config.json"
        data = self.model_dump(exclude={"_config_dir", "_data_dir"})
        config_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.debug("Config saved to %s", config_file)

    def has_credentials(self) -> bool:
        """Check if credentials are stored."""
        return bool(self.username and self._get_keyring("token"))

    def get_credentials(self) -> Optional[dict]:
        """Retrieve stored credentials."""
        password = self._get_keyring("password")
        if self.username and password:
            return {"username": self.username, "password": password}
        return None

    def save_credentials(self, username: str, password: str):
        """Store credentials securely."""
        self.username = username
        self._set_keyring("password", password)
        self.save()

    def get_token(self) -> Optional[str]:
        """Get stored auth token."""
        return self._get_keyring("token")

    def save_token(self, token: str):
        """Store auth token securely."""
        self._set_keyring("token", token)

    def clear_credentials(self):
        """Remove all stored credentials."""
        self._del_keyring("password")
        self._del_keyring("token")
        self.username = ""
        self.save()

    def _get_keyring(self, key: str) -> Optional[str]:
        try:
            return keyring.get_password(KEYRING_SERVICE, f"{self.server_url}:{key}")
        except Exception:
            return None

    def _set_keyring(self, key: str, value: str):
        try:
            keyring.set_password(KEYRING_SERVICE, f"{self.server_url}:{key}", value)
        except Exception as e:
            log.error("Keyring store failed: %s", e)

    def _del_keyring(self, key: str):
        try:
            keyring.delete_password(KEYRING_SERVICE, f"{self.server_url}:{key}")
        except Exception:
            pass
