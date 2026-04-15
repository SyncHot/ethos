"""Directory scanner — walks local filesystem and builds file index with fingerprints."""

import logging
import os
import stat
from pathlib import Path
from typing import Optional

from ethos_drive.config import FilterRule
from ethos_drive.utils.crypto import content_fingerprint, quick_fingerprint
from ethos_drive.utils.paths import local_to_remote, is_hidden

log = logging.getLogger(__name__)


class FileEntry:
    """Represents a scanned file's metadata."""

    __slots__ = ("rel_path", "abs_path", "is_dir", "size", "mtime_ns", "xxhash")

    def __init__(self, rel_path: str, abs_path: str, is_dir: bool = False,
                 size: int = 0, mtime_ns: int = 0, xxhash: str = ""):
        self.rel_path = rel_path
        self.abs_path = abs_path
        self.is_dir = is_dir
        self.size = size
        self.mtime_ns = mtime_ns
        self.xxhash = xxhash

    def as_dict(self) -> dict:
        return {
            "path": self.rel_path,
            "is_dir": self.is_dir,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "xxhash": self.xxhash,
        }


class DirectoryScanner:
    """Scan a local directory tree, apply filters, compute fingerprints."""

    def __init__(self, root: str, filters: list[FilterRule] = None,
                 sync_hidden: bool = False,
                 selective_paths: Optional[list[str]] = None):
        self.root = os.path.normpath(root)
        self.filters = filters or []
        self.sync_hidden = sync_hidden
        self.selective_paths = selective_paths

    def scan(self, compute_hashes: bool = False) -> dict[str, FileEntry]:
        """Walk the directory tree and return a dict of rel_path -> FileEntry.

        Args:
            compute_hashes: If True, compute xxhash for every file (slow but thorough).
                           If False, only record size + mtime (fast for change detection).
        """
        result = {}
        try:
            self._scan_dir(self.root, result, compute_hashes)
        except Exception as e:
            log.error("Scan error at %s: %s", self.root, e)
        log.info("Scanned %s: %d entries", self.root, len(result))
        return result

    def _scan_dir(self, dir_path: str, result: dict, compute_hashes: bool):
        """Recursively scan a directory."""
        try:
            entries = list(os.scandir(dir_path))
        except PermissionError:
            log.warning("Permission denied: %s", dir_path)
            return
        except OSError as e:
            log.warning("Cannot scan %s: %s", dir_path, e)
            return

        for entry in entries:
            try:
                rel = local_to_remote(entry.path, self.root)

                # Skip hidden files unless configured
                if not self.sync_hidden and is_hidden(entry.path):
                    continue

                # Skip temp/partial files
                name = entry.name
                if name.endswith((".ethos-tmp", ".ethos-conflict", ".partial")):
                    continue

                # Apply selective sync filter
                if self.selective_paths is not None:
                    if not self._in_selective(rel):
                        continue

                # Apply user filter rules
                if self._is_filtered(rel, entry.is_dir()):
                    continue

                if entry.is_dir(follow_symlinks=False):
                    fe = FileEntry(
                        rel_path=rel,
                        abs_path=entry.path,
                        is_dir=True,
                    )
                    result[rel] = fe
                    self._scan_dir(entry.path, result, compute_hashes)

                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    fe = FileEntry(
                        rel_path=rel,
                        abs_path=entry.path,
                        is_dir=False,
                        size=st.st_size,
                        mtime_ns=st.st_mtime_ns,
                    )
                    if compute_hashes:
                        fp = content_fingerprint(entry.path)
                        if fp:
                            fe.xxhash = fp["xxhash"]
                    result[rel] = fe

            except (OSError, PermissionError) as e:
                log.warning("Skipping %s: %s", entry.path, e)

    def _in_selective(self, rel_path: str) -> bool:
        """Check if path is within any selective sync path."""
        for sp in self.selective_paths:
            if rel_path == sp or rel_path.startswith(sp + "/") or sp.startswith(rel_path + "/"):
                return True
        return False

    def _is_filtered(self, rel_path: str, is_dir: bool) -> bool:
        """Check if a path matches any filter rule."""
        from fnmatch import fnmatch
        name = os.path.basename(rel_path)

        for rule in self.filters:
            if rule.apply_to == "files" and is_dir:
                continue
            if rule.apply_to == "folders" and not is_dir:
                continue

            matched = False
            if rule.pattern and fnmatch(name, rule.pattern):
                matched = True
            if rule.pattern and fnmatch(rel_path, rule.pattern):
                matched = True

            if matched and rule.type == "exclude":
                return True
            if matched and rule.type == "include":
                return False

        return False

    def quick_scan(self) -> dict[str, str]:
        """Fast scan returning only quick fingerprints (mtime:size).

        Used for rapid change detection between full scans.
        """
        result = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self.root):
                # Filter hidden directories
                if not self.sync_hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]

                for fname in filenames:
                    if not self.sync_hidden and fname.startswith("."):
                        continue
                    if fname.endswith((".ethos-tmp", ".ethos-conflict", ".partial")):
                        continue

                    fpath = os.path.join(dirpath, fname)
                    rel = local_to_remote(fpath, self.root)

                    fp = quick_fingerprint(fpath)
                    if fp:
                        result[rel] = fp
        except Exception as e:
            log.error("Quick scan error: %s", e)

        return result
