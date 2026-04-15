"""HTTP API client for communicating with EthOS server."""

import logging
import os
import time
from pathlib import Path
from typing import IO, Optional

import httpx

log = logging.getLogger(__name__)

# API timeout defaults
TIMEOUT_DEFAULT = 30
TIMEOUT_UPLOAD = 300
TIMEOUT_DOWNLOAD = 600


class APIError(Exception):
    """API request failed."""

    def __init__(self, message: str, status_code: int = 0, response: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response or {}


class EthosAPIClient:
    """HTTP client for the EthOS sync-drive API.

    Handles authentication, request retries, and chunked transfers.
    """

    def __init__(self, server_url: str, verify_ssl: bool = True):
        self.server_url = server_url.rstrip("/")
        self.token: Optional[str] = None
        self._client = httpx.Client(
            base_url=self.server_url,
            verify=verify_ssl,
            timeout=TIMEOUT_DEFAULT,
            follow_redirects=True,
        )

    def set_token(self, token: str):
        """Set authentication token."""
        self.token = token
        self._client.headers["Authorization"] = f"Bearer {token}"

    def _headers(self) -> dict:
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make an authenticated API request. Returns parsed JSON."""
        kwargs.setdefault("headers", {}).update(self._headers())
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as e:
            raise APIError(f"Cannot connect to server: {e}") from e
        except httpx.TimeoutException as e:
            raise APIError(f"Request timed out: {e}") from e

        if resp.status_code == 401:
            raise APIError("Authentication failed", status_code=401)
        if resp.status_code == 403:
            raise APIError("Forbidden", status_code=403)

        try:
            data = resp.json()
        except Exception:
            if resp.status_code >= 400:
                raise APIError(f"HTTP {resp.status_code}: {resp.text}", status_code=resp.status_code)
            return {"ok": True}

        if resp.status_code >= 400:
            raise APIError(data.get("error", f"HTTP {resp.status_code}"), status_code=resp.status_code, response=data)

        return data

    # --- Authentication ---

    def login(self, username: str, password: str) -> Optional[str]:
        """Authenticate and get token."""
        data = self._request("POST", "/api/auth/login", json={
            "username": username,
            "password": password,
        })
        token = data.get("token")
        if token:
            self.set_token(token)
        return token

    def check_connection(self) -> bool:
        """Test if we can reach the server and our token is valid."""
        try:
            data = self._request("GET", "/api/sync-drive/ping")
            return data.get("ok", False)
        except APIError:
            return False

    # --- Device Registration ---

    def register_device(self, device_id: str, device_name: str) -> dict:
        """Register this client as a sync device."""
        return self._request("POST", "/api/sync-drive/devices/register", json={
            "device_id": device_id,
            "device_name": device_name,
        })

    def unregister_device(self, device_id: str) -> dict:
        """Remove device registration."""
        return self._request("POST", "/api/sync-drive/devices/unregister", json={
            "device_id": device_id,
        })

    # --- Sync State ---

    def get_remote_state(self, remote_path: str, recursive: bool = True) -> dict:
        """Get file listing with metadata for a remote directory.

        Returns: {files: [{path, size, mtime_ns, xxhash, is_dir}, ...]}
        """
        return self._request("POST", "/api/sync-drive/state", json={
            "path": remote_path,
            "recursive": recursive,
        })

    def get_changes_since(self, remote_path: str, since_version: int) -> dict:
        """Get changes since a specific sync version.

        Returns: {changes: [{action, path, ...}], current_version: int}
        """
        return self._request("POST", "/api/sync-drive/changes", json={
            "path": remote_path,
            "since_version": since_version,
        })

    # --- File Operations ---

    def download_file(self, remote_path: str, local_path: str,
                      progress_callback=None) -> bool:
        """Download a file from the server with progress tracking."""
        headers = self._headers()
        local_dir = os.path.dirname(local_path)
        os.makedirs(local_dir, exist_ok=True)

        # Use temp file for atomic write
        tmp_path = local_path + ".ethos-tmp"
        try:
            with self._client.stream("GET", "/api/sync-drive/download",
                                     params={"path": remote_path},
                                     headers=headers,
                                     timeout=TIMEOUT_DOWNLOAD) as resp:
                if resp.status_code != 200:
                    raise APIError(f"Download failed: HTTP {resp.status_code}",
                                   status_code=resp.status_code)

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total:
                            progress_callback(downloaded, total)

            # Atomic rename
            if os.path.exists(local_path):
                os.replace(tmp_path, local_path)
            else:
                os.rename(tmp_path, local_path)

            return True

        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def upload_file(self, local_path: str, remote_path: str,
                    chunk_size: int = 4 * 1024 * 1024,
                    progress_callback=None) -> dict:
        """Upload a file to the server using chunked upload."""
        file_size = os.path.getsize(local_path)

        # Small files: single upload
        if file_size <= chunk_size:
            with open(local_path, "rb") as f:
                return self._request("POST", "/api/sync-drive/upload", files={
                    "file": (os.path.basename(local_path), f),
                }, data={
                    "path": remote_path,
                })

        # Large files: chunked upload
        init_resp = self._request("POST", "/api/sync-drive/upload/init", json={
            "path": remote_path,
            "size": file_size,
            "filename": os.path.basename(local_path),
        })
        session_id = init_resp["session_id"]

        try:
            offset = 0
            chunk_index = 0
            with open(local_path, "rb") as f:
                while offset < file_size:
                    chunk_data = f.read(chunk_size)
                    if not chunk_data:
                        break

                    self._request("POST", "/api/sync-drive/upload/chunk",
                                  files={"chunk": ("chunk", chunk_data)},
                                  data={
                                      "session_id": session_id,
                                      "offset": str(offset),
                                      "index": str(chunk_index),
                                  },
                                  timeout=TIMEOUT_UPLOAD)

                    offset += len(chunk_data)
                    chunk_index += 1
                    if progress_callback:
                        progress_callback(offset, file_size)

            return self._request("POST", "/api/sync-drive/upload/complete", json={
                "session_id": session_id,
            })

        except Exception:
            # Abort on failure
            try:
                self._request("POST", "/api/sync-drive/upload/abort", json={
                    "session_id": session_id,
                })
            except Exception:
                pass
            raise

    def delete_remote(self, remote_path: str) -> dict:
        """Delete a file or directory on the server."""
        return self._request("POST", "/api/sync-drive/delete", json={
            "path": remote_path,
        })

    def create_remote_dir(self, remote_path: str) -> dict:
        """Create a directory on the server."""
        return self._request("POST", "/api/sync-drive/mkdir", json={
            "path": remote_path,
        })

    def move_remote(self, src_path: str, dst_path: str) -> dict:
        """Move/rename a file on the server."""
        return self._request("POST", "/api/sync-drive/move", json={
            "src": src_path,
            "dst": dst_path,
        })

    # --- Versioning ---

    def get_versions(self, remote_path: str) -> dict:
        """Get version history for a file."""
        return self._request("GET", "/api/sync-drive/versions", params={
            "path": remote_path,
        })

    def restore_version(self, remote_path: str, version_id: int) -> dict:
        """Restore a specific version of a file."""
        return self._request("POST", "/api/sync-drive/versions/restore", json={
            "path": remote_path,
            "version_id": version_id,
        })

    # --- Browse ---

    def browse(self, remote_path: str = "/") -> dict:
        """List directory contents for folder browsing in UI."""
        return self._request("GET", "/api/sync-drive/browse", params={
            "path": remote_path,
        })

    def close(self):
        """Close the HTTP client."""
        self._client.close()
