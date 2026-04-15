"""Sync filter rules engine — decides which files to include/exclude."""

import fnmatch
import logging
import os
from pathlib import Path
from typing import Optional

from ethos_drive.config import FilterRule

log = logging.getLogger(__name__)

# Built-in exclusion patterns (always excluded)
ALWAYS_EXCLUDE = [
    "*.ethos-tmp",
    "*.ethos-conflict",
    "*.partial",
    ".ethos-sync",
    "desktop.ini",
    "Thumbs.db",
    ".DS_Store",
    "~$*",           # Office temp files
    "~*.tmp",
    ".~lock.*",      # LibreOffice locks
    "$RECYCLE.BIN",
    "System Volume Information",
]


class FilterEngine:
    """Evaluates filter rules against file paths."""

    def __init__(self, rules: list[FilterRule] = None,
                 sync_hidden: bool = False,
                 syncignore_path: Optional[str] = None):
        self.rules = rules or []
        self.sync_hidden = sync_hidden
        self._syncignore_patterns: list[str] = []

        if syncignore_path and os.path.isfile(syncignore_path):
            self._load_syncignore(syncignore_path)

    def _load_syncignore(self, path: str):
        """Load .syncignore file (gitignore-like syntax)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._syncignore_patterns.append(line)
            log.info("Loaded %d patterns from %s", len(self._syncignore_patterns), path)
        except Exception as e:
            log.warning("Cannot read .syncignore: %s", e)

    def should_exclude(self, rel_path: str, is_dir: bool = False,
                       file_size: int = 0) -> bool:
        """Check if a path should be excluded from sync.

        Returns True if the file should be EXCLUDED.
        """
        name = os.path.basename(rel_path)

        # Built-in exclusions
        for pattern in ALWAYS_EXCLUDE:
            if fnmatch.fnmatch(name, pattern):
                return True

        # Hidden files
        if not self.sync_hidden and name.startswith("."):
            return True

        # .syncignore patterns
        for pattern in self._syncignore_patterns:
            if pattern.endswith("/"):
                # Directory-only pattern
                if is_dir and fnmatch.fnmatch(name, pattern.rstrip("/")):
                    return True
            elif fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_path, pattern):
                return True

        # User filter rules
        for rule in self.rules:
            if rule.apply_to == "files" and is_dir:
                continue
            if rule.apply_to == "folders" and not is_dir:
                continue

            matched = False
            if rule.pattern:
                matched = fnmatch.fnmatch(name, rule.pattern) or fnmatch.fnmatch(rel_path, rule.pattern)

            if not matched and rule.max_size_mb and not is_dir:
                if file_size > rule.max_size_mb * 1024 * 1024:
                    matched = True

            if matched:
                if rule.type == "exclude":
                    return True
                elif rule.type == "include":
                    return False

        return False

    def filter_entries(self, entries: dict) -> dict:
        """Filter a dict of {rel_path: FileEntry}, returning only included entries."""
        return {
            path: entry for path, entry in entries.items()
            if not self.should_exclude(
                path,
                is_dir=getattr(entry, "is_dir", False),
                file_size=getattr(entry, "size", 0),
            )
        }
