"""
EthOS — File Manager Blueprint
Extracted from app.py.

Routes:
  /api/files/* — file operations (list, delete, rename, etc.)
  /api/files/folder-password/* — folder password protection
  /api/files/*/permissions — file permissions
  /api/files/duplicates/* — duplicate file finder
  /api/phone-sync/* — phone sync
  /api/trash/* — trash/recycle bin
"""

import os
import sys
import json
import time
import re
import zipfile
import tarfile
import shutil
import threading as _threading
from collections import OrderedDict as _ODict
from flask import Blueprint, request, jsonify, send_file, Response
from i18n import t

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from host import (
    data_path as _data_path,
    app_path as _app_path,
    ETHOS_ROOT,
    host_run as _host_run,
    q,
    get_user_home as _get_user_home,
    user_data_path as _user_data_path,
    fs_call_with_timeout as _fs_call,
    get_photo_folders as _get_photo_folders,
    ensure_user_home_structure as _ensure_user_home_structure,
)
from utils import (
    load_json as _load_json,
    save_json as _save_json,
    safe_path as _safe_path_util,
    fmt_bytes,
    DATA_ROOT,
    ALLOWED_ROOTS as _ALLOWED_ROOTS,
    generate_thumbnail,
    THUMB_CACHE_DIR,
    THUMBS_DIR_NAME,
    _thumb_cache_key,
    _local_thumb_path,
    list_directory as _list_dir,
)
from blueprints.eventlog import log as elog
from blueprints.admin_required import admin_required
from blueprints.auth import require_auth, get_current_user, get_token, _is_sudo_mode, make_user_cache_key
from crypto_utils import hash_folder_password as _hash_folder_password_new, verify_folder_password as _verify_folder_password
import gevent
import signal
import errno
import hashlib
import secrets
import stat as _stat_mod
import grp as _grp
import pwd
from datetime import datetime
import io
from PIL import Image
from flask import g
from host import get_data_disk as _get_data_disk

files_bp = Blueprint('files', __name__)

_host_run_base = _host_run  # alias — same function, different name from app.py era


class _SocketioProxy:
    """Lazy proxy for socketio — avoids import-lock issues under gevent."""
    def __getattr__(self, name):
        sio = getattr(_sys.modules.get('app'), 'socketio', None)
        if sio is None:
            raise AttributeError(f'socketio not yet available ({name!r})')
        return getattr(sio, name)


socketio = _SocketioProxy()


class _CacheProxy:
    """Lazy proxy for Flask-Caching cache instance."""
    def cached(self, *args, **kwargs):
        c = getattr(_sys.modules.get('app'), 'cache', None)
        if c:
            return c.cached(*args, **kwargs)
        def _noop(f):
            return f
        return _noop

    def __getattr__(self, name):
        c = getattr(_sys.modules.get('app'), 'cache', None)
        if c is None:
            raise AttributeError(f'cache not yet available ({name!r})')
        return getattr(c, name)


cache = _CacheProxy()


class _TryWakeProxy:
    """Lazy proxy for try_wake_path (avoids circular import with storage blueprint)."""
    def __call__(self, *args, **kwargs):
        mod = _sys.modules.get('blueprints.storage')
        fn = getattr(mod, 'try_wake_path', None) if mod else None
        if fn:
            return fn(*args, **kwargs)


try_wake_path = _TryWakeProxy()

_fileop_lock = _threading.RLock()

# ── Dir-size in-memory cache ────────────────────────────────
# Maps path → {'size': int, 'expires': monotonic_time}
# OrderedDict preserves insertion order for O(1) FIFO eviction.
_dirsize_cache = _ODict()
_dirsize_cache_lock = _threading.Lock()
_DIRSIZE_CACHE_TTL = 600   # seconds
_DIRSIZE_CACHE_MAX = 1000  # max entries — increased for better hit rate
_DIRSIZE_CACHE_PERSIST_FILE = _data_path('.dirsize_cache.json')
_DIRSIZE_CACHE_SAVE_INTERVAL = 60  # minimum seconds between disk saves
_dirsize_cache_last_save = 0.0

def _dirsize_cache_get(path):
    """Return cached size for *path* or None if missing/expired."""
    with _dirsize_cache_lock:
        entry = _dirsize_cache.get(path)
        if entry:
            if time.monotonic() < entry['expires']:
                return entry['size']
            del _dirsize_cache[path]  # evict expired entry eagerly
        return None

def _dirsize_cache_set(path, size):
    """Store *size* for *path* with TTL; O(1) FIFO eviction when over limit."""
    global _dirsize_cache_last_save
    with _dirsize_cache_lock:
        # Move-to-end on update keeps most-recently-set at the back
        if path in _dirsize_cache:
            _dirsize_cache.move_to_end(path)
        _dirsize_cache[path] = {'size': size, 'expires': time.monotonic() + _DIRSIZE_CACHE_TTL}
        if len(_dirsize_cache) > _DIRSIZE_CACHE_MAX:
            # Evict oldest insertion (O(1) with OrderedDict)
            _dirsize_cache.popitem(last=False)
    # Persist to disk (debounced — at most once per _DIRSIZE_CACHE_SAVE_INTERVAL)
    now = time.monotonic()
    if now - _dirsize_cache_last_save > _DIRSIZE_CACHE_SAVE_INTERVAL:
        _dirsize_cache_last_save = now
        gevent.spawn(_dirsize_cache_persist)

def _dirsize_cache_invalidate(path):
    """Evict *path* (and any sub-paths) from the dir-size cache."""
    with _dirsize_cache_lock:
        keys = [k for k in _dirsize_cache if k == path or k.startswith(path + '/')]
        for k in keys:
            del _dirsize_cache[k]

def _dirsize_cache_persist():
    """Write current in-memory dir-size cache to disk (runs in a background greenlet)."""
    now = time.monotonic()
    try:
        snapshot = {}
        with _dirsize_cache_lock:
            for path, entry in list(_dirsize_cache.items()):
                remaining = entry['expires'] - now
                if remaining > 0:
                    snapshot[path] = {'size': entry['size'], 'remaining': round(remaining, 1)}
        tmp = _DIRSIZE_CACHE_PERSIST_FILE + '.tmp'
        with open(tmp, 'w') as _f:
            json.dump(snapshot, _f)
        os.replace(tmp, _DIRSIZE_CACHE_PERSIST_FILE)
    except Exception:
        pass

def _dirsize_cache_restore():
    """Load persisted dir-size cache from disk at startup."""
    try:
        with open(_DIRSIZE_CACHE_PERSIST_FILE) as _f:
            raw = json.load(_f)
        now = time.monotonic()
        loaded = 0
        with _dirsize_cache_lock:
            for path, entry in raw.items():
                remaining = entry.get('remaining', 0)
                if remaining > 0 and loaded < _DIRSIZE_CACHE_MAX:
                    _dirsize_cache[path] = {'size': entry['size'], 'expires': now + remaining}
                    loaded += 1
    except Exception:
        pass

# Restore persisted cache at import time
_dirsize_cache_restore()

# ── Short-lived listing cache ────────────────────────────────
# Maps real_path → {'items': list, 'expires': monotonic_time}
# OrderedDict preserves insertion order for O(1) FIFO eviction.
_listdir_cache = _ODict()
_listdir_cache_lock = _threading.Lock()
_LISTDIR_CACHE_TTL = 45   # seconds — slightly longer for better hit rate
_LISTDIR_CACHE_MAX = 400  # max entries — doubled for large deployments

def _listdir_cache_get(real_path):
    """Return cached item list for *real_path* or None if missing/expired/stale.

    On every hit we do a single os.path.getmtime() syscall (~1-2 µs) so that
    any write to the directory immediately invalidates the cached listing
    instead of waiting up to the full TTL.
    """
    with _listdir_cache_lock:
        entry = _listdir_cache.get(real_path)
        if not entry:
            return None
        if time.monotonic() >= entry['expires']:
            del _listdir_cache[real_path]  # evict expired entry eagerly
            return None
        # Invalidate if directory mtime changed since we cached it
        _cached_mtime = entry.get('mtime')
        if _cached_mtime is not None:
            try:
                if os.path.getmtime(real_path) != _cached_mtime:
                    del _listdir_cache[real_path]
                    return None
            except OSError:
                del _listdir_cache[real_path]
                return None
        return entry['items']

def _listdir_cache_set(real_path, items, mtime=None):
    """Store *items* for *real_path* with short TTL; O(1) FIFO eviction.

    Pass *mtime* (os.path.getmtime result) so the cache can self-invalidate
    the moment the directory changes rather than waiting for TTL expiry.
    """
    with _listdir_cache_lock:
        if real_path in _listdir_cache:
            _listdir_cache.move_to_end(real_path)
        _listdir_cache[real_path] = {
            'items': items,
            'expires': time.monotonic() + _LISTDIR_CACHE_TTL,
            'mtime': mtime,
        }
        if len(_listdir_cache) > _LISTDIR_CACHE_MAX:
            _listdir_cache.popitem(last=False)  # O(1) FIFO eviction

def _listdir_cache_invalidate(path):
    """Evict *path* and its parent from the listing cache."""
    real = safe_path(path) if path else None
    parent_real = os.path.dirname(real) if real else None
    with _listdir_cache_lock:
        for key in list(_listdir_cache.keys()):
            if key == real or key == parent_real or (real and key.startswith(real + '/')):
                del _listdir_cache[key]


# ── Background dir-size jobs ────────────────────────────────
# job_id → { path: str, size: int|None, done: bool, error: bool, ts: float }
_dirsize_bg_jobs = {}
_dirsize_bg_lock = _threading.Lock()
_DIRSIZE_JOB_EXPIRY = 120  # seconds — stale completed jobs are cleaned up
# path → job_id for currently running (not-yet-done) jobs — prevents duplicate spawning
_dirsize_running_by_path = {}

def _dirsize_bg_jobs_prune():
    """Remove completed/stale jobs older than _DIRSIZE_JOB_EXPIRY seconds."""
    now = time.monotonic()
    with _dirsize_bg_lock:
        stale = [jid for jid, j in _dirsize_bg_jobs.items()
                 if j.get('done') and (now - j.get('ts', now)) > _DIRSIZE_JOB_EXPIRY]
        for jid in stale:
            del _dirsize_bg_jobs[jid]

# Two independent operation channels so e.g. compress doesn't block copy/move.
# Channel 'bg' = heavy background ops (compress, download-zip, transfer)
# Channel 'fm' = file-management ops (copy, move, extract)
_OP_CHANNEL = {
    'compress': 'bg', 'download': 'bg', 'transfer': 'bg',
    'copy': 'fm', 'move': 'fm', 'extract': 'fm',
}

def _new_channel_state():
    return {
        'active': False,
        'operation': None,
        'progress': None,
        'result': None,
        'cancel': False,
        'paused': False,
        '_proc': None,
        'meta': None,
    }

_fileop_channels = {
    'bg': _new_channel_state(),
    'fm': _new_channel_state(),
}

# ── Backward-compat alias ── (used in a few places that touch _proc / progress directly)
_fileop_state = _fileop_channels['bg']


# ── Sub-module bottom-imports ─────────────────────────────────────────────────
# These must stay at the end of this file — after all shared state is defined.
# Importing each sub-module triggers @files_bp.route() decorator registration.
import blueprints.file_manager_ops          # noqa: F401
import blueprints.file_manager_permissions  # noqa: F401
import blueprints.file_manager_photos       # noqa: F401
import blueprints.file_manager_mobile       # noqa: F401
import blueprints.file_manager_duplicates   # noqa: F401
import blueprints.file_manager_archive      # noqa: F401

# Re-expose key helpers onto this module so other sub-modules can reach them
# via _sys.modules['blueprints.file_manager'].<name>
from blueprints.file_manager_ops import (           # noqa: F401
    safe_path,
    _count_items,
    _chown_to_user,
    _chown_recursive,
    _copy_with_progress,
    _fileop_emit,
    _ch_for,
    _fileop_progress,
    _fileop_cancelled,
    _fileop_is_paused,
    _fileop_finish,
    _clear_copy_task,
    _clear_move_task,
    _clear_compress_task,
    _clear_zip_task,
    _clear_transfer_task,
    _save_transfer_task,
    _save_zip_task,
    _save_copy_task,
    _save_move_task,
    _save_compress_task,
    _safe_exists,
    _safe_isdir,
    _resume_interrupted_copy,
    _resume_interrupted_move,
    _resume_interrupted_compress,
    _resume_interrupted_zip,
    _cleanup_stale_ethos_tmp,
    _bg_prewarm_home_listing,
    get_fileop_notifications,
)
from blueprints.file_manager_permissions import (   # noqa: F401
    _require_folder_access,
    _load_folder_passwords,
    _is_folder_unlocked,
    _is_folder_protected,
    _migrate_folder_passwords,
    _get_owner_group,
    _mode_to_symbolic,
)
from blueprints.file_manager_photos import (        # noqa: F401
    _bg_download_zip,
    _purge_thumb_cache,
)
from blueprints.file_manager_duplicates import (    # noqa: F401
    _bg_copy,
    _bg_move,
    _atomic_move,
)
from blueprints.file_manager_archive import (       # noqa: F401
    _bg_compress,
    _bg_extract,
    _bg_transfer_remote,
    _resume_interrupted_transfer,
    _fmt_bytes,
)
