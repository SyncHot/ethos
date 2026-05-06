"""
EthOS - App Manager File Operations Module

Handles file downloads, extraction, validation.
"""

import os
import json
import logging
import sys
import zipfile
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, host_run_stream, data_path, q

log = logging.getLogger('app_manager')

# ─── Path constants (set from app_manager) ──────────────────

_ETHOS_ROOT = None
_FRONTEND_APPS_DIR = None
_BLUEPRINTS_DIR = None


def set_paths(ethos_root, frontend_apps_dir, blueprints_dir):
    """Called from app_manager to set path constants."""
    global _ETHOS_ROOT, _FRONTEND_APPS_DIR, _BLUEPRINTS_DIR
    _ETHOS_ROOT = ethos_root
    _FRONTEND_APPS_DIR = frontend_apps_dir
    _BLUEPRINTS_DIR = blueprints_dir


# ─── File helpers ────────────────────────────────────────────

def _get_frontend_filenames(app_id):
    """Return list of ALL JS filenames (without .js) for an app.
    First element is the primary file; remaining are extras defined in
    _FRONTEND_EXTRA_FILES.  Returns [] when the app lives in apps.js (fn=None)."""
    primary = _get_frontend_filename(app_id)
    if primary is None:
        return []
    return [primary] + list(_FRONTEND_EXTRA_FILES.get(app_id, []))


def _get_backend_filenames(app_id):
    """Return list of ALL backend module names (without .py) for an app.
    First element is the primary module from _OPTIONAL_BLUEPRINTS; remaining
    are extras defined in _BACKEND_EXTRA_FILES.  Returns [] when the app has
    no optional blueprint."""
    bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
    if not bp_info:
        return []
    return [bp_info[0]] + list(_BACKEND_EXTRA_FILES.get(app_id, []))




    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
        tmp = dest_path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(content)
        os.replace(tmp, dest_path)
        return True
    except Exception as e:
        log.error('[app_manager] Download failed %s: %s', url, e)
        return False





def download_app_file(url, dest_path):
    """Download app file with error handling.
    
    Args:
        url: File URL
        dest_path: Destination path
        
    Returns:
        True on success, False on failure
    """
    return _download_file(url, dest_path)


def extract_app_files(zip_path, dest_dir):
    """Extract ZIP file to destination directory.
    
    Args:
        zip_path: Path to ZIP file
        dest_dir: Destination directory
        
    Returns:
        List of extracted file paths
        
    Raises:
        ValueError: If extraction fails
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
        extracted = []
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for info in zf.infolist():
                # Skip directories
                if info.filename.endswith('/'):
                    continue
                # Extract to dest_dir
                dest_file = zf.extract(info, dest_dir)
                extracted.append(dest_file)
        return extracted
    except Exception as e:
        raise ValueError(f"Failed to extract {zip_path}: {e}")


def validate_app_file_count(app_id, file_list):
    """Verify file count matches _BACKEND_EXTRA_FILES expectations.
    
    Args:
        app_id: Application ID
        file_list: List of files (names or paths)
        
    Returns:
        True if valid
        
    Raises:
        ValueError: If count doesn't match
    """
    # Import here to avoid circular import at module level
    try:
        from blueprints.app_manager import _BACKEND_EXTRA_FILES
    except ImportError:
        # If not available yet, accept anything (loading time)
        return True
    
    # Expected: 1 primary + N extras
    expected_count = 1 + len(_BACKEND_EXTRA_FILES.get(app_id, []))
    actual_count = len(file_list)
    
    if actual_count != expected_count:
        raise ValueError(
            f"{app_id}: expected {expected_count} files, got {actual_count}"
        )
    return True

