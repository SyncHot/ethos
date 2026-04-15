"""Path normalization and safety utilities."""

import os
import re
from pathlib import Path, PurePosixPath


# Characters illegal in Windows filenames
_WINDOWS_ILLEGAL = re.compile(r'[<>:"|?*]')

# Reserved Windows device names
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def normalize_local(path: str) -> str:
    """Normalize a local filesystem path for the current OS."""
    return str(Path(path).resolve())


def local_to_remote(local_path: str, local_root: str) -> str:
    """Convert a local absolute path to a remote relative POSIX path."""
    rel = os.path.relpath(local_path, local_root)
    # Convert Windows backslashes to forward slashes
    return PurePosixPath(rel).as_posix()


def remote_to_local(remote_path: str, local_root: str) -> str:
    """Convert a remote POSIX relative path to a local absolute path."""
    parts = PurePosixPath(remote_path).parts
    return str(Path(local_root).joinpath(*parts))


def is_safe_relative(path: str) -> bool:
    """Check that a relative path doesn't escape its root via '..'."""
    parts = PurePosixPath(path).parts
    depth = 0
    for part in parts:
        if part == "..":
            depth -= 1
        elif part != ".":
            depth += 1
        if depth < 0:
            return False
    return True


def is_valid_windows_name(name: str) -> bool:
    """Check if a filename is valid on Windows."""
    if not name or name.endswith((" ", ".")):
        return False
    if _WINDOWS_ILLEGAL.search(name):
        return False
    stem = Path(name).stem.upper()
    if stem in _WINDOWS_RESERVED:
        return False
    return True


def sanitize_for_windows(name: str) -> str:
    """Make a filename safe for Windows by replacing illegal characters."""
    result = _WINDOWS_ILLEGAL.sub("_", name)
    if result.rstrip(". ") == "":
        result = "_" + result
    stem = Path(result).stem.upper()
    if stem in _WINDOWS_RESERVED:
        result = "_" + result
    return result


def is_hidden(path: str | Path) -> bool:
    """Check if a file is hidden (starts with dot or has Windows hidden attribute)."""
    name = Path(path).name
    if name.startswith("."):
        return True
    # Check Windows hidden attribute
    if os.name == "nt":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            return bool(attrs & 0x2)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass
    return False


def conflict_name(path: str, suffix: str = "conflict") -> str:
    """Generate a conflict filename: 'file.txt' -> 'file (conflict).txt'."""
    p = Path(path)
    stem = p.stem
    ext = p.suffix
    parent = p.parent
    return str(parent / f"{stem} ({suffix}){ext}")
