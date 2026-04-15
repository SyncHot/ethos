"""Hashing and checksum utilities for file fingerprinting."""

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

import xxhash

log = logging.getLogger(__name__)

# Read buffer size for hashing — 1 MB
HASH_BUFFER_SIZE = 1024 * 1024


def file_xxhash(path: str | Path, *, chunk_size: int = HASH_BUFFER_SIZE) -> Optional[str]:
    """Compute xxHash64 of a file. Fast — ~6 GB/s on modern hardware."""
    try:
        h = xxhash.xxh64()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError) as e:
        log.warning("Cannot hash %s: %s", path, e)
        return None


def file_sha256(path: str | Path, *, chunk_size: int = HASH_BUFFER_SIZE) -> Optional[str]:
    """Compute SHA-256 hash of a file. Slower but cryptographically secure."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError) as e:
        log.warning("Cannot hash %s: %s", path, e)
        return None


def quick_fingerprint(path: str | Path) -> Optional[str]:
    """Fast file fingerprint using mtime + size. No disk I/O beyond stat().

    Returns a string like "1710000000.123456:4096" (mtime_ns:size).
    Used for quick change detection — if this changes, compute full hash.
    """
    try:
        st = os.stat(path)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except (OSError, PermissionError):
        return None


def content_fingerprint(path: str | Path) -> Optional[dict]:
    """Full content fingerprint: stat + xxhash."""
    try:
        st = os.stat(path)
        xxh = file_xxhash(path)
        if xxh is None:
            return None
        return {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "xxhash": xxh,
        }
    except (OSError, PermissionError) as e:
        log.warning("Cannot fingerprint %s: %s", path, e)
        return None
