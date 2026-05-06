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
from blueprints.auth import require_auth, get_current_user
from crypto_utils import hash_folder_password as _hash_folder_password_new, verify_folder_password as _verify_folder_password

files_bp = Blueprint('files', __name__)

# ═════════════════════════════════════════════════════════════════════════════



# ─────────────────────────── File Manager ───────────────────────────

import zipfile
import tarfile
import threading as _threading

_fileop_lock = _threading.RLock()

# ── Dir-size in-memory cache ────────────────────────────────
# Maps path → {'size': int, 'expires': monotonic_time}
# OrderedDict preserves insertion order for O(1) FIFO eviction.
from collections import OrderedDict as _ODict
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

# ── Persistent transfer resume ──
TRANSFER_RESUME_FILE = _data_path('transfer_resume.json')

def _save_transfer_task(resolved, server_id, remote_dest, requesting_user):
    """Persist transfer task to disk so it survives server restarts."""
    task = {
        'paths': resolved,
        'server_id': server_id,
        'remote_dest': remote_dest,
        'requesting_user': requesting_user,
        'started': time.time(),
    }
    _save_json(TRANSFER_RESUME_FILE, task)

def _clear_transfer_task():
    """Remove persisted transfer task (transfer completed or cancelled)."""
    try:
        if os.path.exists(TRANSFER_RESUME_FILE):
            os.remove(TRANSFER_RESUME_FILE)
    except OSError:
        pass

# ── Persistent ZIP download resume ──
ZIP_RESUME_FILE = _data_path('zip_resume.json')

def _save_zip_task(resolved, tmp_path, zip_name, download_id, total):
    """Persist ZIP download task to disk so it can restart after server reboot."""
    task = {
        'resolved': resolved,
        'tmp_path': tmp_path,
        'zip_name': zip_name,
        'download_id': download_id,
        'total': total,
        'started': time.time(),
    }
    _save_json(ZIP_RESUME_FILE, task)

def _clear_zip_task():
    """Remove persisted ZIP task (completed, cancelled, or failed)."""
    try:
        if os.path.exists(ZIP_RESUME_FILE):
            os.remove(ZIP_RESUME_FILE)
    except OSError:
        pass

# ── Persistent copy resume ──
COPY_RESUME_FILE = _data_path('copy_resume.json')

def _save_copy_task(resolved, dest_dir, total, on_conflict, username):
    """Persist copy task to disk so it can restart after server reboot."""
    task = {
        'resolved': resolved,
        'dest_dir': dest_dir,
        'total': total,
        'on_conflict': on_conflict,
        'username': username,
        'started': time.time(),
    }
    _save_json(COPY_RESUME_FILE, task)

def _clear_copy_task():
    """Remove persisted copy task (completed, cancelled, or failed)."""
    try:
        if os.path.exists(COPY_RESUME_FILE):
            os.remove(COPY_RESUME_FILE)
    except OSError:
        pass

def _safe_exists(path, timeout=3):
    """os.path.exists via thread pool – won't D-state on sleeping drives."""
    try:
        return _fs_call(os.path.exists, path, timeout=timeout)
    except (TimeoutError, Exception):
        return False

def _safe_isdir(path, timeout=3):
    """os.path.isdir via thread pool – won't D-state on sleeping drives."""
    try:
        return _fs_call(os.path.isdir, path, timeout=timeout)
    except (TimeoutError, Exception):
        return False

def _resume_interrupted_copy():
    """Check for a persisted copy task and restart it after server reboot."""
    task = _load_json(COPY_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('resolved', [])
    dest_dir = task.get('dest_dir', '')
    on_conflict = task.get('on_conflict', 'rename')
    username = task.get('username')
    started = task.get('started', 0)

    if time.time() - started > 86400:
        _clear_copy_task()
        return
    if not resolved or not dest_dir or not _safe_isdir(dest_dir):
        _clear_copy_task()
        return

    valid = [p for p in resolved if _safe_exists(p)]
    if not valid:
        _clear_copy_task()
        return

    total = _count_items(valid)
    if total == 0:
        _clear_copy_task()
        return

    _fm = _fileop_channels['fm']
    with _fileop_lock:
        if _fm['active']:
            return
        _fm['active'] = True
        _fm['operation'] = 'copy'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    cur_user = {'username': username} if username else None
    elog('files', 'info', f'Resuming copy after restart: {len(valid)} items → {dest_dir}')
    socketio.start_background_task(_bg_copy, valid, dest_dir, total, on_conflict, cur_user)


# ── Persistent move resume ──
MOVE_RESUME_FILE = _data_path('move_resume.json')

def _save_move_task(resolved, dest_dir, total, on_conflict, dest_user_path, username):
    """Persist move task to disk so it can restart after server reboot."""
    task = {
        'resolved': resolved,
        'dest_dir': dest_dir,
        'total': total,
        'on_conflict': on_conflict,
        'dest_user_path': dest_user_path,
        'username': username,
        'started': time.time(),
    }
    _save_json(MOVE_RESUME_FILE, task)

def _clear_move_task():
    """Remove persisted move task (completed, cancelled, or failed)."""
    try:
        if os.path.exists(MOVE_RESUME_FILE):
            os.remove(MOVE_RESUME_FILE)
    except OSError:
        pass

def _resume_interrupted_move():
    """Check for a persisted move task and restart it after server reboot."""
    task = _load_json(MOVE_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('resolved', [])
    dest_dir = task.get('dest_dir', '')
    on_conflict = task.get('on_conflict', 'rename')
    dest_user_path = task.get('dest_user_path', '')
    username = task.get('username')
    started = task.get('started', 0)

    if time.time() - started > 86400:
        _clear_move_task()
        return
    if not resolved or not dest_dir or not _safe_isdir(dest_dir):
        _clear_move_task()
        return

    # For moves, only resume sources that still exist (not yet moved)
    valid = [p for p in resolved if _safe_exists(p)]
    if not valid:
        _clear_move_task()
        return

    total = _count_items(valid)
    if total == 0:
        _clear_move_task()
        return

    _fm = _fileop_channels['fm']
    with _fileop_lock:
        if _fm['active']:
            return
        _fm['active'] = True
        _fm['operation'] = 'move'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    cur_user = {'username': username} if username else None
    elog('files', 'info', f'Resuming move after restart: {len(valid)} items → {dest_dir}')
    socketio.start_background_task(_bg_move, valid, dest_dir, total, on_conflict, dest_user_path, cur_user)


# ── Persistent compress (in-place) resume ──
COMPRESS_RESUME_FILE = _data_path('compress_resume.json')

def _save_compress_task(resolved, archive_path, fmt, total, username):
    """Persist compress task to disk so it can restart after server reboot."""
    task = {
        'resolved': resolved,
        'archive_path': archive_path,
        'format': fmt,
        'total': total,
        'username': username,
        'started': time.time(),
    }
    _save_json(COMPRESS_RESUME_FILE, task)

def _clear_compress_task():
    """Remove persisted compress task (completed, cancelled, or failed)."""
    try:
        if os.path.exists(COMPRESS_RESUME_FILE):
            os.remove(COMPRESS_RESUME_FILE)
    except OSError:
        pass

def _resume_interrupted_compress():
    """Check for a persisted compress task and restart it after server reboot."""
    task = _load_json(COMPRESS_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('resolved', [])
    archive_path = task.get('archive_path', '')
    fmt = task.get('format', 'zip')
    total = task.get('total', 0)
    username = task.get('username')
    started = task.get('started', 0)

    # Ignore tasks older than 1 day (stale)
    if time.time() - started > 86400:
        _clear_compress_task()
        return

    if not resolved or not archive_path:
        _clear_compress_task()
        return

    # Verify at least one source still exists
    valid = [p for p in resolved if _safe_exists(p)]
    if not valid:
        _clear_compress_task()
        return

    # Recount items (files may have changed)
    total = _count_items(valid)
    if total == 0:
        _clear_compress_task()
        return

    # Remove partial archives from previous attempt (both final and temp paths)
    for p in (archive_path, archive_path + '.ethos_archive_tmp'):
        if _safe_exists(p):
            try: os.remove(p)
            except OSError: pass

    cur_user = {'username': username} if username else None

    with _fileop_lock:
        if _fileop_state['active']:
            return  # something else already running
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'compress'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False

    elog('files', 'info', f'Resuming compression after restart: {os.path.basename(archive_path)} ({total} files)')
    socketio.start_background_task(_bg_compress, valid, archive_path, fmt, total, cur_user)

def _resume_interrupted_zip():
    """Check for a persisted ZIP task and restart it after server reboot."""
    task = _load_json(ZIP_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('resolved', [])
    zip_name = task.get('zip_name', 'download.zip')
    download_id = task.get('download_id')
    total = task.get('total', 0)
    started = task.get('started', 0)

    # Ignore tasks older than 1 day (stale)
    if time.time() - started > 86400:
        _clear_zip_task()
        return

    if not resolved or not download_id:
        _clear_zip_task()
        return

    # Verify at least one source still exists
    valid = [p for p in resolved if _safe_exists(p)]
    if not valid:
        _clear_zip_task()
        return

    # Recount items (files may have changed)
    total = _count_items(valid)
    if total == 0:
        _clear_zip_task()
        return

    # Generate fresh tmp_path (old partial file is gone after restart)
    tmp_path = os.path.join('/tmp', f'nasos_dl_{download_id}.zip')

    with _fileop_lock:
        if _fileop_state['active']:
            return  # something else already running
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'download'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False

    elog('files', 'info', f'Resuming ZIP preparation after restart: {zip_name} ({total} files)')
    socketio.start_background_task(_bg_download_zip, valid, tmp_path, zip_name, download_id, total)

def _cleanup_stale_ethos_tmp(data_root=None):
    """Remove leftover .ethos_tmp partial files from previous crash/abort.

    Scans user home directories for .ethos_tmp files older than 1 hour and
    removes them.  Also clears stale upload chunk directories.

    Only scans /home and data-disk home — external mounts (/media, /mnt)
    are skipped because walking large FUSE-mounted drives (e.g. NTFS)
    blocks the gevent event loop and .ethos_tmp files only exist on
    paths managed by EthOS file operations.
    """
    cutoff = time.time() - 3600  # 1 hour

    # Determine scan roots: user home directories only
    scan_roots = []
    try:
        scan_roots.append('/home')
        try:
            dd = _get_data_disk()
            if dd:
                home_on_dd = os.path.join(dd, 'home')
                if _safe_isdir(home_on_dd) and home_on_dd not in scan_roots:
                    scan_roots.append(home_on_dd)
        except Exception:
            pass
    except Exception:
        pass

    _SKIP_DIRS = {'.trash', '.thumbs', '.Trash-1000', '.Trash-0', 'preview', '.snapshots'}

    def _scan_dir(start_path):
        try:
            for dirpath, dirs, files in os.walk(start_path):
                # Prune directories that should never be walked for temp cleanup
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                for fn in files:
                    if fn.endswith('.ethos_tmp') or fn.endswith('.ethos_upload_tmp') or fn.endswith('.ethos_mv_tmp'):
                        fp = os.path.join(dirpath, fn)
                        try:
                            if os.path.getmtime(fp) < cutoff:
                                os.remove(fp)
                        except OSError:
                            pass
                # Clean up stale .ethos_tmp_dir and .ethos_mv_tmp_dir partial directory copies
                for dn in list(dirs):
                    if dn.endswith('.ethos_tmp_dir') or dn.endswith('.ethos_mv_tmp_dir'):
                        dp = os.path.join(dirpath, dn)
                        try:
                            if os.path.getmtime(dp) < cutoff:
                                shutil.rmtree(dp, ignore_errors=True)
                                dirs.remove(dn)
                        except OSError:
                            pass
        except Exception:
            pass

    for root in scan_roots:
        try:
            _fs_call(_scan_dir, root, timeout=30)
        except TimeoutError:
            print(f'[cleanup] Skipping hung path: {root}')
        except Exception:
            pass

    # Also prune stale upload chunk dirs
    try:
        if os.path.isdir(_UPLOAD_TMP_DIR):
            chunk_cutoff = time.time() - _UPLOAD_SESSION_TTL
            for entry in os.scandir(_UPLOAD_TMP_DIR):
                if entry.is_dir():
                    try:
                        if entry.stat().st_mtime < chunk_cutoff:
                            shutil.rmtree(entry.path, ignore_errors=True)
                    except OSError:
                        pass
    except Exception:
        pass


def _bg_prewarm_home_listing():
    """Pre-warm the listing cache for all user home directories at startup.

    Runs 12 s after server boot so the first FM open for any user is instant.
    Uses the preload semaphore to avoid competing with real requests.
    """
    try:
        home_roots = ['/home']
        dd = _get_data_disk()
        if dd:
            home_roots.append(os.path.join(dd, 'home'))

        dirs_to_warm = []
        for root in home_roots:
            if not os.path.isdir(root):
                continue
            try:
                for entry in os.scandir(root):
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'):
                        dirs_to_warm.append(entry.path)
            except (PermissionError, OSError):
                pass

        for rpath in dirs_to_warm:
            if _listdir_cache_get(rpath) is not None:
                continue  # already warm
            _preload_sem.acquire()
            try:
                if _listdir_cache_get(rpath) is not None:
                    continue
                items_pre = []
                _mtime = None
                try:
                    _mtime = os.path.getmtime(rpath)
                except OSError:
                    pass
                try:
                    for entry in sorted(os.scandir(rpath),
                                        key=lambda e: (not e.is_dir(), e.name.lower())):
                        if entry.name in ('.trash', '.thumbs') and entry.is_dir():
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                            items_pre.append({
                                'name': entry.name,
                                'is_dir': entry.is_dir(),
                                'is_link': entry.is_symlink(),
                                'size': st.st_size if not entry.is_dir() else 0,
                                'modified': st.st_mtime,
                                'permissions': oct(st.st_mode)[-3:],
                            })
                        except (PermissionError, OSError):
                            pass
                    _listdir_cache_set(rpath, items_pre, mtime=_mtime)
                except (PermissionError, OSError):
                    pass
            finally:
                _preload_sem.release()
            gevent.sleep(0.05)  # yield between home dirs to stay low-priority
    except Exception:
        pass


def _fileop_emit(event, data):
    socketio.emit(event, data)

def _ch_for(operation):
    """Return channel key ('bg' or 'fm') for a given operation name."""
    return _OP_CHANNEL.get(operation, 'bg')

def _fileop_progress(operation, current_file, done, total, ch=None):
    if ch is None:
        ch = _ch_for(operation)
    pct = round(done / total * 100, 1) if total > 0 else 0
    prog = {'operation': operation, 'current_file': current_file,
            'done': done, 'total': total, 'percent': pct, 'channel': ch}
    with _fileop_lock:
        _fileop_channels[ch]['progress'] = prog
    _fileop_emit('fileop_progress', prog)

def _fileop_cancelled(ch='bg'):
    """Check if current file operation on *ch* was cancelled."""
    with _fileop_lock:
        return _fileop_channels[ch].get('cancel', False)

def _fileop_is_paused(ch='bg'):
    """Check if current file operation on *ch* is paused."""
    with _fileop_lock:
        return _fileop_channels[ch].get('paused', False)

def _fileop_finish(operation, success, message, ch=None):
    if ch is None:
        ch = _ch_for(operation)
    result = {'status': 'completed' if success else 'failed',
              'message': message, 'time': time.time(), 'operation': operation, 'channel': ch}
    slot = _fileop_channels[ch]
    with _fileop_lock:
        slot['active'] = False
        slot['operation'] = None
        slot['progress'] = None
        slot['result'] = result
        slot['cancel'] = False
        slot['paused'] = False
        slot['_proc'] = None
    event = 'fileop_complete' if success else 'fileop_error'
    _fileop_emit(event, result)
    # Log to event log
    op_labels = {'copy': 'Copying', 'move': 'Moving',
                 'compress': 'Compressing', 'extract': 'Extracting',
                 'download': 'ZIP Download', 'transfer': 'NAS Transfer'}
    label = op_labels.get(operation, operation)
    lvl = 'info' if success else 'error'
    elog('files', lvl, f'{label}: {message}')

def get_fileop_notifications():
    notifs = []
    op_labels = {'copy': 'Copying', 'move': 'Moving',
                 'compress': 'Compressing', 'extract': 'Extracting',
                 'download': 'ZIP Download', 'transfer': 'NAS Transfer'}
    results = []
    with _fileop_lock:
        for _ch_key, slot in _fileop_channels.items():
            if slot['active'] and slot['progress']:
                p = slot['progress']
                label = op_labels.get(p.get('operation', ''), 'Operation')
                notifs.append({
                    'type': 'progress',
                    'title': f'{label} files',
                    'message': f'{p["done"]}/{p["total"]} — {p["percent"]}%',
                    'time': time.time(),
                    'action': {'app': 'file-manager', 'tab': ''}
                })
            r = slot.get('result')
            if r:
                results.append(r)
    for r in results:
        if r.get('time') and (time.time() - r['time']) < 300:
            label = op_labels.get(r.get('operation', ''), 'Operation')
            if r['status'] == 'completed':
                notifs.append({
                    'type': 'success', 'title': f'{label} completed',
                    'message': r['message'], 'time': r['time'],
                    'action': {'app': 'file-manager', 'tab': ''}
                })
            else:
                notifs.append({
                    'type': 'error', 'title': f'{label} — error',
                    'message': r['message'], 'time': r['time'],
                    'action': {'app': 'file-manager', 'tab': ''}
                })
    return notifs


@files_bp.route('/api/files/operation-status', methods=['GET'])
@require_auth
def fileop_status():
    """Return current file operation status (both channels)."""
    channels = {}
    with _fileop_lock:
        for ch_key, slot in _fileop_channels.items():
            channels[ch_key] = {
                'active': slot['active'],
                'operation': slot['operation'],
                'progress': slot['progress'],
                'result': slot.get('result'),
                'meta': slot.get('meta'),
                'paused': slot.get('paused', False),
            }
    # Backward compat: top-level fields from first active channel (or bg)
    primary = channels.get('bg', {})
    for ch_key in ('bg', 'fm'):
        if channels.get(ch_key, {}).get('active'):
            primary = channels[ch_key]
            break
    st = dict(primary)
    st['channels'] = channels
    # Include pending downloads that haven't been picked up yet
    pending = []
    for did, info in list(_pending_downloads.items()):
        if os.path.isfile(info.get('path', '')):
            pending.append({'download_id': did, 'name': info.get('name', 'download.zip')})
    st['pending_downloads'] = pending
    return jsonify(st)


@files_bp.route('/api/files/cancel-operation', methods=['POST'])
@require_auth
def cancel_fileop():
    """Cancel the currently active file operation on a specific channel."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    ch = data.get('channel', None)
    # Auto-detect channel if not provided
    if not ch:
        with _fileop_lock:
            for ch_key in ('bg', 'fm'):
                if _fileop_channels[ch_key]['active']:
                    ch = ch_key
                    break
    if not ch:
        return jsonify({'error': 'No active operation'}), 400
    slot = _fileop_channels.get(ch)
    if not slot:
        return jsonify({'error': 'Unknown channel'}), 400
    with _fileop_lock:
        if not slot['active']:
            return jsonify({'error': 'No active operation on this channel'}), 400
        slot['cancel'] = True
        slot['paused'] = False
        proc = slot.get('_proc')
    # Resume first if paused, then kill subprocess (rsync)
    if proc:
        try:
            proc.send_signal(signal.SIGCONT)
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
    return jsonify({'cancelled': True, 'channel': ch, 'message': 'Cancellation in progress…'})


@files_bp.route('/api/files/pause-operation', methods=['POST'])
@require_auth
def pause_fileop():
    """Pause or resume the currently active file operation on a specific channel."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    ch = data.get('channel', None)
    # Auto-detect channel if not provided
    if not ch:
        with _fileop_lock:
            for ch_key in ('bg', 'fm'):
                if _fileop_channels[ch_key]['active']:
                    ch = ch_key
                    break
    if not ch:
        return jsonify({'error': 'No active operation'}), 400
    slot = _fileop_channels.get(ch)
    if not slot:
        return jsonify({'error': 'Unknown channel'}), 400
    with _fileop_lock:
        if not slot['active']:
            return jsonify({'error': 'No active operation on this channel'}), 400
        operation = slot.get('operation')
        if operation not in ('transfer', 'download', 'compress', 'copy', 'move'):
            return jsonify({'error': 'Pause is not supported for this operation'}), 400
        currently_paused = slot.get('paused', False)
        new_paused = not currently_paused
        slot['paused'] = new_paused
        proc = slot.get('_proc')

    # For transfer operations with rsync subprocess, use SIGSTOP/SIGCONT
    if proc:
        try:
            if new_paused:
                proc.send_signal(signal.SIGSTOP)
            else:
                proc.send_signal(signal.SIGCONT)
        except Exception:
            pass

    _fileop_emit('fileop_paused', {'paused': new_paused, 'channel': ch})
    return jsonify({'paused': new_paused, 'channel': ch, 'message': 'Wstrzymano' if new_paused else 'Wznowiono'})


def _count_items(paths):
    """Count total files/dirs recursively for progress."""
    total = 0
    for p in paths:
        if os.path.isdir(p):
            for _, dirs, files in os.walk(p):
                total += len(files) + len(dirs)
        else:
            total += 1
    return total

def _chown_to_user(path, username=None):
    """Change ownership of a single path to the given (or current) user."""
    if not username:
        user = get_current_user()
        username = user['username'] if user else None
    if not username:
        return
    try:
        pw = pwd.getpwnam(username)
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass

def _chown_recursive(path, username=None):
    """Recursively chown a path (file or dir) to the given (or current) user."""
    if not username:
        user = get_current_user()
        username = user['username'] if user else None
    if not username:
        return
    try:
        pw = pwd.getpwnam(username)
        uid, gid = pw.pw_uid, pw.pw_gid
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                os.chown(root, uid, gid)
                for d in dirs:
                    os.chown(os.path.join(root, d), uid, gid)
                for f in files:
                    os.chown(os.path.join(root, f), uid, gid)
        else:
            os.chown(path, uid, gid)
    except (KeyError, OSError):
        pass

def _copy_with_progress(real_src, target, op_label, done_ref, total, gevent_yield=True, username=None, cancel_ref=None, pause_fn=None, partial_files=None):
    """Copy file or directory tree with progress updates.

    cancel_ref: a mutable list [False] — set to True to abort.
    pause_fn:   callable returning True while paused.
    partial_files: list to track files being written (for cleanup on cancel).
    """
    def _check():
        """Return True if cancelled; honour pause."""
        if cancel_ref is not None and cancel_ref[0]:
            return True
        if pause_fn is not None:
            while pause_fn():
                if cancel_ref is not None and cancel_ref[0]:
                    return True
                gevent.sleep(0.2)
        return False

    if os.path.isdir(real_src):
        os.makedirs(target, exist_ok=True)
        _chown_to_user(target, username)
        for item in os.listdir(real_src):
            if _check():
                raise InterruptedError('Copy cancelled')
            s = os.path.join(real_src, item)
            d = os.path.join(target, item)
            if os.path.isdir(s):
                _copy_with_progress(s, d, op_label, done_ref, total, gevent_yield, username, cancel_ref, pause_fn, partial_files)
            else:
                tmp_d = d + '.ethos_tmp'
                if partial_files is not None:
                    partial_files.append(tmp_d)
                try:
                    _fs_call(shutil.copy2, s, tmp_d, timeout=300)
                    os.replace(tmp_d, d)
                    if partial_files is not None and tmp_d in partial_files:
                        partial_files.remove(tmp_d)
                except Exception:
                    try: os.remove(tmp_d)
                    except OSError: pass
                    raise
                _chown_to_user(d, username)
                done_ref[0] += 1
                if done_ref[0] % 3 == 0 or done_ref[0] == total:
                    _fileop_progress(op_label, item, done_ref[0], total)
                    if gevent_yield:
                        gevent.sleep(0)
        done_ref[0] += 1  # count the dir itself
    else:
        tmp_target = target + '.ethos_tmp'
        if partial_files is not None:
            partial_files.append(tmp_target)
        try:
            _fs_call(shutil.copy2, real_src, tmp_target, timeout=300)
            os.replace(tmp_target, target)
            if partial_files is not None and tmp_target in partial_files:
                partial_files.remove(tmp_target)
        except Exception:
            try: os.remove(tmp_target)
            except OSError: pass
            raise
        _chown_to_user(target, username)
        done_ref[0] += 1
        _fileop_progress(op_label, os.path.basename(real_src), done_ref[0], total)
        if gevent_yield:
            gevent.sleep(0)

def safe_path(user_path):
    """Resolve user path with role-aware sandboxing.

    - Normal mode: restricted to allowed roots and own home scope.
    - Sudo mode (admin): full filesystem access, except other users' home dirs.
    """
    cur = get_current_user()
    sudo_mode = bool(cur and cur.get('role') == 'admin' and _is_sudo_mode())
    result = _safe_path_util(
        user_path, isolate_home=True,
        current_user=cur['username'] if cur else None,
        sudo_mode=sudo_mode,
    )
    # Non-sudo users stay strictly in their own home directory.
    if result and cur and not sudo_mode:
        user_home = _get_user_home(cur['username'])
        if not (result == user_home or result.startswith(user_home + '/')):
            return None
    return result


# ─── Folder Passwords ────────────────────────────────────────
FOLDER_PASSWORDS_FILE = _data_path('folder_passwords.json')
_unlocked_folders = {}  # token -> set of unlocked folder paths
_uf_lock = _threading.Lock()

# Brute-force protection for folder unlock (per token, keyed by token+path)
_folder_unlock_attempts = {}  # key -> {'count': int, 'first': float, 'locked_until': float}
_folder_unlock_lock = _threading.Lock()
_FU_MAX_ATTEMPTS = 5
_FU_ATTEMPT_WINDOW = 120   # 2 minutes
_FU_LOCKOUT_TIME = 300     # 5 minute lockout

FOLDER_PASSWORD_MIN_LENGTH = 8


def _mode_to_symbolic(mode):
    """Convert numeric stat mode to symbolic string like 'rwxr-xr-x'."""
    flags = [
        (_stat_mod.S_IRUSR, 'r'), (_stat_mod.S_IWUSR, 'w'), (_stat_mod.S_IXUSR, 'x'),
        (_stat_mod.S_IRGRP, 'r'), (_stat_mod.S_IWGRP, 'w'), (_stat_mod.S_IXGRP, 'x'),
        (_stat_mod.S_IROTH, 'r'), (_stat_mod.S_IWOTH, 'w'), (_stat_mod.S_IXOTH, 'x'),
    ]
    return ''.join(c if mode & f else '-' for f, c in flags)


def _get_owner_group(st):
    """Return (owner_name, group_name) strings from a stat result."""
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, AttributeError):
        owner = str(st.st_uid)
    try:
        group = _grp.getgrgid(st.st_gid).gr_name
    except (KeyError, AttributeError):
        group = str(st.st_gid)
    return owner, group

def _load_folder_passwords():
    return _load_json(FOLDER_PASSWORDS_FILE, {})

def _save_folder_passwords(data):
    _save_json(FOLDER_PASSWORDS_FILE, data)

def _hash_folder_password(pw):
    return _hash_folder_password_new(pw)

def _is_folder_protected(user_path):
    """Check if user_path or any of its parents is password-protected."""
    passwords = _load_folder_passwords()
    if not passwords:
        return None
    # Normalize
    check = user_path.rstrip('/')
    while check and check != '/':
        if check in passwords:
            return check
        check = os.path.dirname(check)
    if '/' in passwords:
        return '/'
    return None

def _is_folder_unlocked(user_path):
    """Check if a protected folder is unlocked for the current session."""
    token = get_token()
    with _uf_lock:
        unlocked = _unlocked_folders.get(token, set())
    protected_path = _is_folder_protected(user_path)
    if protected_path is None:
        return True  # not protected
    return protected_path in unlocked

def _require_folder_access(user_path):
    """Returns None if access is allowed, or a Flask response tuple if blocked."""
    protected_path = _is_folder_protected(user_path)
    if protected_path is None:
        return None
    if _is_folder_unlocked(user_path):
        return None
    return jsonify({'error': 'Folder protected by password', 'locked': True, 'protected_path': protected_path}), 403


def _migrate_folder_passwords(old_user_path, new_user_path):
    """Update folder_passwords.json and _unlocked_folders when a folder is renamed/moved."""
    old_user_path = old_user_path.rstrip('/')
    new_user_path = new_user_path.rstrip('/')
    passwords = _load_folder_passwords()
    updated = {}
    changed = False
    for pw_path, pw_hash in passwords.items():
        if pw_path == old_user_path:
            updated[new_user_path] = pw_hash
            changed = True
        elif pw_path.startswith(old_user_path + '/'):
            updated[new_user_path + pw_path[len(old_user_path):]] = pw_hash
            changed = True
        else:
            updated[pw_path] = pw_hash
    if changed:
        _save_folder_passwords(updated)
        with _uf_lock:
            for token_set in list(_unlocked_folders.values()):
                to_add = set()
                to_remove = set()
                for p in token_set:
                    if p == old_user_path:
                        to_remove.add(p)
                        to_add.add(new_user_path)
                    elif p.startswith(old_user_path + '/'):
                        to_remove.add(p)
                        to_add.add(new_user_path + p[len(old_user_path):])
                token_set -= to_remove
                token_set |= to_add


@files_bp.route('/api/files/folder-password', methods=['GET'])
@require_auth
def folder_password_list():
    """List all password-protected folders."""
    passwords = _load_folder_passwords()
    return jsonify({'folders': list(passwords.keys())})


@files_bp.route('/api/files/folder-password', methods=['POST'])
@require_auth
def folder_password_set():
    """Set password for a folder."""
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/') or '/'
    password = data.get('password', '')
    if not password or len(password) < FOLDER_PASSWORD_MIN_LENGTH:
        return jsonify({'error': f'Password must be at least {FOLDER_PASSWORD_MIN_LENGTH} characters'}), 400
    # Complexity check: at least one letter and one number
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return jsonify({'error': 'Password must contain letters and numbers'}), 400
    real = safe_path(path)
    if not real or not os.path.isdir(real):
        return jsonify({'error': 'Folder does not exist'}), 404
    cur = get_current_user()
    username = cur['username'] if cur else 'unknown'
    passwords = _load_folder_passwords()
    is_update = path in passwords
    passwords[path] = _hash_folder_password(password)
    _save_folder_passwords(passwords)
    action = 'Changed folder password' if is_update else 'Set folder password'
    elog('security', 'info', f'{action}: {path}', {'user': username, 'path': path})
    return jsonify({'ok': True})


@files_bp.route('/api/files/folder-password', methods=['DELETE'])
@require_auth
def folder_password_remove():
    """Remove password from a folder."""
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/') or '/'
    password = data.get('password', '')
    passwords = _load_folder_passwords()
    if path not in passwords:
        return jsonify({'error': 'Folder is not protected'}), 404
    cur = get_current_user()
    username = cur['username'] if cur else 'unknown'
    # Verify current password (unless admin override)
    is_admin = cur and cur.get('role') == 'admin'
    force = data.get('force', False)

    if not _verify_folder_password(password, passwords[path]):
        # Allow admin to force remove without correct password
        if is_admin and force:
            elog('security', 'warning', f'Forced removal of folder password by admin: {path}',
                 {'user': username, 'path': path})
        else:
            elog('security', 'warning', f'Failed attempt to remove folder password: {path}',
                 {'user': username, 'path': path})
            return jsonify({'error': t('auth.invalid_password')}), 403
    del passwords[path]
    _save_folder_passwords(passwords)
    # Remove from all unlock sessions
    with _uf_lock:
        for s in _unlocked_folders.values():
            s.discard(path)
    elog('security', 'info', f'Removed folder password: {path}', {'user': username, 'path': path})
    return jsonify({'ok': True})


@files_bp.route('/api/files/folder-unlock', methods=['POST'])
@require_auth
def folder_unlock():
    """Unlock a password-protected folder for the current session."""
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/') or '/'
    password = data.get('password', '')
    passwords = _load_folder_passwords()
    if path not in passwords:
        return jsonify({'error': 'Folder is not protected'}), 404

    token = get_token()
    cur = get_current_user()
    username = cur['username'] if cur else 'unknown'
    fu_key = f'{token}:{path}'
    now = time.time()

    # Check brute-force lockout
    with _folder_unlock_lock:
        attempt = _folder_unlock_attempts.get(fu_key)
        if attempt:
            if now < attempt.get('locked_until', 0):
                remaining = int(attempt['locked_until'] - now)
                elog('security', 'warning',
                     f'Folder locked against brute-force attack: {path}',
                     {'user': username, 'path': path, 'remaining_s': remaining})
                return jsonify({'error': f'Too many attempts. Wait {remaining}s'}), 429
            if now - attempt.get('first', now) > _FU_ATTEMPT_WINDOW:
                _folder_unlock_attempts.pop(fu_key, None)

    if not _verify_folder_password(password, passwords[path]):
        with _folder_unlock_lock:
            attempt = _folder_unlock_attempts.get(fu_key, {'count': 0, 'first': now, 'locked_until': 0})
            attempt['count'] += 1
            if attempt['count'] >= _FU_MAX_ATTEMPTS:
                attempt['locked_until'] = now + _FU_LOCKOUT_TIME
                attempt['count'] = 0
            _folder_unlock_attempts[fu_key] = attempt
        elog('security', 'warning', f'Nieudane odblokowanie folderu: {path}',
             {'user': username, 'path': path})
        return jsonify({'error': t('auth.invalid_password')}), 403

    # Successful unlock — clear attempts and record
    with _folder_unlock_lock:
        _folder_unlock_attempts.pop(fu_key, None)
    with _uf_lock:
        _unlocked_folders.setdefault(token, set()).add(path)
    elog('security', 'info', f'Odblokowano folder: {path}', {'user': username, 'path': path})
    return jsonify({'ok': True})


@files_bp.route('/api/files/folder-lock', methods=['POST'])
@require_auth
def folder_lock():
    """Re-lock a folder for the current session."""
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/') or '/'
    token = get_token()
    cur = get_current_user()
    username = cur['username'] if cur else 'unknown'
    with _uf_lock:
        if token in _unlocked_folders:
            _unlocked_folders[token].discard(path)
    elog('security', 'info', f'Zablokowano folder: {path}', {'user': username, 'path': path})
    return jsonify({'ok': True})


# ─── File Permissions ─────────────────────────────────────────

@files_bp.route('/api/files/permissions')
@require_auth
def files_get_permissions():
    """GET /api/files/permissions?path=<path>
    Returns detailed permissions info: symbolic mode, octal, owner, group.
    """
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    real = safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404
    try:
        st = os.stat(real)
        owner, group = _get_owner_group(st)
        return jsonify({
            'path': path,
            'permissions': oct(st.st_mode)[-3:],
            'permissions_symbolic': _mode_to_symbolic(st.st_mode),
            'permissions_octal': '0' + oct(st.st_mode & 0o7777)[2:],
            'owner': owner,
            'group': group,
            'uid': st.st_uid,
            'gid': st.st_gid,
            'is_dir': os.path.isdir(real),
        })
    except OSError as e:
        return jsonify({'error': str(e)}), 500


@files_bp.route('/api/files/chmod', methods=['POST'])
@require_auth
def files_chmod():
    """POST /api/files/chmod — Change file/folder permissions.
    Body: { path, mode }  where mode is an octal string like '755' or '644'.
    Admin only.
    """
    cur = get_current_user()
    if not cur or cur.get('role') != 'admin':
        return jsonify({'error': 'Administrator privileges required'}), 403
    data = request.get_json(force=True)
    path = data.get('path', '')
    mode_str = data.get('mode', '')
    if not path or not mode_str:
        return jsonify({'error': 'Path and mode are required'}), 400

    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    # Validate mode: must be 3-4 octal digits
    if not re.match(r'^[0-7]{3,4}$', mode_str):
        return jsonify({'error': 'Invalid permissions mode (e.g. 755, 644)'}), 400
    real = safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404
    try:
        new_mode = int(mode_str, 8)
        os.chmod(real, new_mode)
        st = os.stat(real)
        owner, group = _get_owner_group(st)
        username = cur['username']
        elog('security', 'info', f'Changed permissions: {path} → {mode_str}',
             {'user': username, 'path': path, 'mode': mode_str})
        return jsonify({
            'ok': True,
            'permissions': oct(st.st_mode)[-3:],
            'permissions_symbolic': _mode_to_symbolic(st.st_mode),
            'owner': owner,
            'group': group,
        })
    except PermissionError:
        return jsonify({'error': t('auth.no_permission')}), 403
    except OSError as e:
        return jsonify({'error': str(e)}), 500
_FAVORITES_GLOBAL = _data_path('favorites.json')  # legacy, used for migration

def _favorites_file():
    """Return per-user favorites file path."""
    cur = get_current_user()
    username = cur['username'] if cur else None
    if username:
        return _user_data_path('favorites.json', username)
    return _FAVORITES_GLOBAL

def _load_favorites():
    return _load_json(_favorites_file(), [])

def _save_favorites(favs):
    _save_json(_favorites_file(), favs)


@files_bp.route('/api/files/chown', methods=['POST'])
@require_auth
def files_chown():
    """POST /api/files/chown — Change file/folder owner/group.
    Body: { path, owner, group }
    Admin only.
    """
    cur = get_current_user()
    if not cur or cur.get('role') != 'admin':
        return jsonify({'error': 'Administrator privileges required'}), 403
    data = request.get_json(force=True)
    path = data.get('path', '')
    owner = data.get('owner', '')
    group = data.get('group', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    if not owner and not group:
        return jsonify({'error': 'Owner or group is required'}), 400

    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    real = safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404

    try:
        uid = -1
        gid = -1
        if owner:
            try:
                uid = pwd.getpwnam(owner).pw_uid
            except KeyError:
                return jsonify({'error': f'User {owner} does not exist'}), 400
        if group:
            try:
                gid = _grp.getgrnam(group).gr_gid
            except KeyError:
                return jsonify({'error': f'Group {group} does not exist'}), 400

        os.chown(real, uid, gid)
        st = os.stat(real)
        new_owner, new_group = _get_owner_group(st)
        username = cur['username']
        elog('security', 'info', f'Changed owner: {path} → {new_owner}:{new_group}',
             {'user': username, 'path': path, 'owner': new_owner, 'group': new_group})
        return jsonify({
            'ok': True,
            'owner': new_owner,
            'group': new_group
        })
    except PermissionError:
        return jsonify({'error': t('auth.no_permission')}), 403
    except OSError as e:
        return jsonify({'error': str(e)}), 500


# ── Per-folder POSIX ACL management ──────────────────────────────────────────
import shlex as _shlex_acl

def _parse_getfacl(output):
    """Parse getfacl output into structured ACL dict.
    Returns: { 'users': { 'name': 'rwx', ... }, 'groups': { ... },
               'owner': 'rwx', 'group': 'rwx', 'other': 'rwx',
               'default_users': { ... }, 'default_groups': { ... },
               'default_owner': 'rwx', 'default_group': 'rwx', 'default_other': 'rwx' }
    """
    acl = {
        'users': {}, 'groups': {},
        'owner': '', 'group': '', 'other': '',
        'default_users': {}, 'default_groups': {},
        'default_owner': '', 'default_group': '', 'default_other': '',
        'mask': '', 'default_mask': '',
    }
    for line in output.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(':')
        if len(parts) < 3:
            continue
        # Standard entries: user::rwx, user:bob:r-x, group::r-x, etc.
        # Default entries: default:user::rwx, default:user:bob:r-x, etc.
        is_default = parts[0] == 'default'
        if is_default:
            parts = parts[1:]
        if len(parts) < 3:
            continue
        etype = parts[0]   # user, group, mask, other
        ename = parts[1]   # empty for owner/group/other, or username/groupname
        eperm = parts[2]   # rwx, r-x, etc.
        prefix = 'default_' if is_default else ''
        if etype == 'user':
            if ename:
                acl[prefix + 'users'][ename] = eperm
            else:
                acl[prefix + 'owner'] = eperm
        elif etype == 'group':
            if ename:
                acl[prefix + 'groups'][ename] = eperm
            else:
                acl[prefix + 'group'] = eperm
        elif etype == 'other':
            acl[prefix + 'other'] = eperm
        elif etype == 'mask':
            acl[prefix + 'mask'] = eperm
    return acl


def _perm_to_simple(perm_str):
    """Convert rwx string to simple label: 'rw', 'ro', or 'none'."""
    if not perm_str or perm_str == '---':
        return 'none'
    has_w = 'w' in perm_str
    has_r = 'r' in perm_str
    if has_w:
        return 'rw'
    if has_r:
        return 'ro'
    return 'none'


def _simple_to_perm(simple, is_dir=True):
    """Convert simple label to rwx string."""
    if simple == 'rw':
        return 'rwx' if is_dir else 'rw-'
    elif simple == 'ro':
        return 'r-x' if is_dir else 'r--'
    return '---'


@files_bp.route('/api/files/acl')
@require_auth
def files_get_acl():
    """GET /api/files/acl?path=<path>
    Returns POSIX ACLs for the given path using getfacl.
    """
    cur = get_current_user()
    if not cur or cur.get('role') != 'admin':
        return jsonify({'error': 'Administrator privileges required'}), 403
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    real = safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404

    r = _host_run_base(f"getfacl -p {_shlex_acl.quote(real)} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({'error': 'Could not read ACL (filesystem may not support ACLs)'}), 500

    acl = _parse_getfacl(r.stdout)
    # Convert rwx strings to simple labels for the UI
    entries = []
    for user, perm in acl['users'].items():
        entries.append({'type': 'user', 'name': user, 'access': _perm_to_simple(perm)})
    for grp, perm in acl['groups'].items():
        entries.append({'type': 'group', 'name': grp, 'access': _perm_to_simple(perm)})

    defaults = []
    for user, perm in acl['default_users'].items():
        defaults.append({'type': 'user', 'name': user, 'access': _perm_to_simple(perm)})
    for grp, perm in acl['default_groups'].items():
        defaults.append({'type': 'group', 'name': grp, 'access': _perm_to_simple(perm)})

    return jsonify({
        'ok': True,
        'path': path,
        'is_dir': os.path.isdir(real),
        'entries': entries,
        'defaults': defaults,
        'base': {
            'owner': acl['owner'],
            'group': acl['group'],
            'other': acl['other'],
        }
    })


@files_bp.route('/api/files/acl', methods=['PUT'])
@require_auth
def files_set_acl():
    """PUT /api/files/acl — Set POSIX ACLs on a file/folder.
    Body: {
        path: "/some/folder",
        entries: [ { type: "user"|"group", name: "bob", access: "rw"|"ro"|"none" }, ... ],
        inherit: true|false,     // if true, set default ACLs too (dirs only)
        recursive: true|false    // if true, apply -R recursively
    }
    """
    cur = get_current_user()
    if not cur or cur.get('role') != 'admin':
        return jsonify({'error': 'Administrator privileges required'}), 403

    data = request.get_json(force=True)
    path = data.get('path', '')
    entries = data.get('entries', [])
    inherit = data.get('inherit', False)
    recursive = data.get('recursive', False)

    if not path:
        return jsonify({'error': 'Path is required'}), 400

    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    real = safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404

    is_dir = os.path.isdir(real)
    rflag = '-R ' if recursive else ''
    username = cur['username']

    # Build setfacl commands
    cmds = []
    for entry in entries:
        etype = entry.get('type', '')
        ename = entry.get('name', '')
        access = entry.get('access', 'none')
        if etype not in ('user', 'group') or not ename:
            continue

        prefix = 'u' if etype == 'user' else 'g'
        perm = _simple_to_perm(access, is_dir)
        _sq = _shlex_acl.quote

        if access == 'none':
            cmds.append(f"setfacl {rflag}-x {prefix}:{_sq(ename)} {_sq(real)} 2>/dev/null; true")
            if inherit and is_dir:
                cmds.append(f"setfacl {rflag}-x d:{prefix}:{_sq(ename)} {_sq(real)} 2>/dev/null; true")
        else:
            cmds.append(f"setfacl {rflag}-m {prefix}:{_sq(ename)}:{perm} {_sq(real)}")
            if inherit and is_dir:
                cmds.append(f"setfacl {rflag}-m d:{prefix}:{_sq(ename)}:{perm} {_sq(real)}")

    if not cmds:
        return jsonify({'ok': True, 'message': 'No changes to apply'})

    errors = []
    for cmd in cmds:
        r = _host_run_base(cmd, timeout=60)
        if r.returncode != 0 and 'true' not in cmd:
            errors.append(r.stderr.strip() if r.stderr else f'Command failed: {cmd}')

    if errors:
        return jsonify({'error': '; '.join(errors)}), 500

    elog('security', 'info', f'ACL updated: {path}' + (' (recursive)' if recursive else ''),
         {'user': username, 'path': path, 'entries': len(entries), 'inherit': inherit, 'recursive': recursive})

    return jsonify({'ok': True})


@files_bp.route('/api/files/favorites')
@require_auth
def files_favorites_list():
    return jsonify(_load_favorites())


@files_bp.route('/api/files/favorites', methods=['POST'])
@require_auth
def files_favorites_add():
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/')
    label = data.get('label', '') or os.path.basename(path) or path
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    favs = _load_favorites()
    if any(f['path'] == path for f in favs):
        return jsonify({'error': 'Already in favorites'}), 409
    favs.append({'path': path, 'label': label})
    _save_favorites(favs)
    return jsonify({'ok': True, 'favorites': favs})


@files_bp.route('/api/files/favorites', methods=['DELETE'])
@require_auth
def files_favorites_remove():
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    favs = _load_favorites()
    favs = [f for f in favs if f['path'] != path]
    _save_favorites(favs)
    return jsonify({'ok': True, 'favorites': favs})


# ─── Photo Favorites (per-user, shared with Gallery) ─────────────────
_GALLERY_FAVS_GLOBAL = _data_path('gallery_favorites.json')  # legacy
_OLD_PHOTO_FAVS_FILE = _data_path('photo_favorites.json')

def _gallery_favs_file():
    cur = get_current_user()
    username = cur['username'] if cur else None
    if username:
        return _user_data_path('gallery_favorites.json', username)
    return _GALLERY_FAVS_GLOBAL

def _migrate_photo_favs():
    """One-time migration: merge old photo_favorites.json into gallery_favorites.json."""
    if not os.path.isfile(_OLD_PHOTO_FAVS_FILE):
        return
    try:
        with open(_OLD_PHOTO_FAVS_FILE, 'r') as f:
            old = json.load(f)
        if not isinstance(old, list) or not old:
            os.rename(_OLD_PHOTO_FAVS_FILE, _OLD_PHOTO_FAVS_FILE + '.bak')
            return
        # Migrate into global file (will be further migrated per-user on first access)
        favs = []
        if os.path.isfile(_GALLERY_FAVS_GLOBAL):
            try:
                with open(_GALLERY_FAVS_GLOBAL, 'r') as f:
                    favs = json.load(f)
            except Exception:
                pass
        existing = {f['path'] for f in favs}
        for path in old:
            if isinstance(path, str) and path not in existing:
                favs.append({'path': path, 'added': time.time()})
                existing.add(path)
        with open(_GALLERY_FAVS_GLOBAL, 'w') as f:
            json.dump(favs, f, ensure_ascii=False, indent=2)
        os.rename(_OLD_PHOTO_FAVS_FILE, _OLD_PHOTO_FAVS_FILE + '.bak')
    except Exception:
        pass

_migrate_photo_favs()

def _load_gallery_favs():
    return _load_json(_gallery_favs_file(), [])

def _save_gallery_favs(favs):
    _save_json(_gallery_favs_file(), favs)


@files_bp.route('/api/photos/favorites')
@require_auth
def photo_favorites_list():
    """Return flat list of paths (compat with file manager)."""
    favs = _load_gallery_favs()
    return jsonify([f['path'] for f in favs if 'path' in f])


@files_bp.route('/api/photos/favorites', methods=['POST'])
@require_auth
def photo_favorites_add():
    data = request.get_json(force=True)
    path = data.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    real = safe_path(path)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'File does not exist'}), 404
    favs = _load_gallery_favs()
    if not any(f['path'] == path for f in favs):
        favs.insert(0, {'path': path, 'added': time.time()})
        _save_gallery_favs(favs)
    paths = [f['path'] for f in favs]
    return jsonify({'ok': True, 'favorites': paths})


@files_bp.route('/api/photos/favorites', methods=['DELETE'])
@require_auth
def photo_favorites_remove():
    data = request.get_json(force=True)
    path = data.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    favs = _load_gallery_favs()
    favs = [f for f in favs if f['path'] != path]
    _save_gallery_favs(favs)
    paths = [f['path'] for f in favs]
    return jsonify({'ok': True, 'favorites': paths})


@files_bp.route('/api/photos/favorites/files')
@require_auth
def photo_favorites_files():
    """Return file info for all favorited photos (that still exist)."""
    favs = _load_gallery_favs()
    items = []
    cleaned = []
    for fav in favs:
        fpath = fav.get('path', '')
        real = safe_path(fpath)
        if not real or not os.path.isfile(real):
            continue  # skip deleted
        cleaned.append(fav)
        try:
            stat = os.stat(real)
            items.append({
                'name': os.path.basename(fpath),
                'path': fpath,
                'is_dir': False,
                'is_link': os.path.islink(real),
                'size': stat.st_size,
                'modified': stat.st_mtime,
                'permissions': oct(stat.st_mode)[-3:]
            })
        except Exception:
            pass
    # Clean up stale entries
    if len(cleaned) != len(favs):
        _save_gallery_favs(cleaned)
    return jsonify({'path': '/__photo_favorites__', 'items': items})


@files_bp.route('/api/files/list')
@require_auth
@cache.cached(timeout=10, key_prefix=make_user_cache_key)
def files_list():
    path = request.args.get('path', '/')
    # Normalize double slashes
    while '//' in path:
        path = path.replace('//', '/')

    _cur = get_current_user()
    _sudo_mode = bool(_cur and _cur.get('role') == 'admin' and _is_sudo_mode())

    # Show virtual root with allowed directories (normal mode only).
    # In sudo mode, '/' is the real filesystem root.
    if (path == '/' or path == '') and not _sudo_mode:
        me = _cur['username'] if _cur else None

        # ── Normal mode: virtual root with allowed dirs ──
        items = []
        # Show user's own home directory (resolves to data disk if configured)
        if me:
            home_dir = _get_user_home(me)
            if os.path.isdir(home_dir):
                try:
                    st = os.stat(home_dir)
                    _owner, _group = _get_owner_group(st)
                    items.append({
                        'name': me,
                        'is_dir': True, 'is_link': False,
                        'size': 0, 'modified': st.st_mtime,
                        'permissions': oct(st.st_mode)[-3:],
                        'permissions_symbolic': _mode_to_symbolic(st.st_mode),
                        'can_read': os.access(home_dir, os.R_OK),
                        'can_write': os.access(home_dir, os.W_OK),
                        'owner': _owner,
                        'group': _group,
                        'home': True,
                        'home_path': home_dir,
                    })
                except OSError:
                    pass
        return jsonify({'items': items, 'path': '/'})

    real_path = safe_path(path)

    # If path looks like it's on a /media/devmon drive, try to wake / remount it
    if real_path and '/media/' in real_path:
        wake_path = real_path
        if wake_path:
            try:
                _fs_call(try_wake_path, wake_path, timeout=25)
            except TimeoutError:
                return jsonify({'error': 'Disk not responding — try again shortly'}), 504

    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    # Check if attempting to list inside a protected folder
    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    passwords = _load_folder_passwords()
    # ── Home isolation: when listing /home or {data_disk}/home, show only own dir ──
    _home_only_user = None
    _home_dirs = ['/home']
    _dd = _get_data_disk()
    if _dd:
        _home_dirs.append(os.path.join(_dd, 'home'))
    if real_path in _home_dirs:
        _cur = get_current_user()
        if _cur:
            _home_only_user = _cur['username']

    # ── Short-lived listing cache (skip for home-isolation and password-protected) ──
    _cache_key = real_path if not _home_only_user else None
    if _cache_key:
        cached_items = _listdir_cache_get(_cache_key)
        if cached_items is not None:
            # ETag based on directory mtime — allows 304 responses on repeat polls
            try:
                dir_mtime = os.path.getmtime(real_path)
                etag = hashlib.md5(f'{real_path}:{dir_mtime}'.encode()).hexdigest()[:16]
                if request.headers.get('If-None-Match') == etag:
                    return '', 304
                resp = jsonify({'path': path, 'items': cached_items})
                resp.headers['ETag'] = etag
                resp.headers['Cache-Control'] = 'no-cache'
                return resp
            except OSError:
                pass
            return jsonify({'path': path, 'items': cached_items})

    # ETag from directory mtime (computed once, reused below)
    _etag = None
    _dir_mtime = None
    if _cache_key:
        try:
            _dir_mtime = os.path.getmtime(real_path)
            _etag = hashlib.md5(f'{real_path}:{_dir_mtime}'.encode()).hexdigest()[:16]
        except OSError:
            pass

    items = []
    _on_external = '/media/' in real_path or '/mnt/' in real_path

    def _do_scandir():
        return sorted(os.scandir(real_path), key=lambda e: (not e.is_dir(), e.name.lower()))

    try:
        if _on_external:
            dir_entries = _fs_call(_do_scandir, timeout=30)
        else:
            dir_entries = _do_scandir()
        for entry in dir_entries:
            # Hide per-drive trash directories and .thumbs directories
            if entry.name in ('.trash', '.thumbs') and entry.is_dir():
                continue
            # Home isolation – skip other users' directories
            if _home_only_user and entry.name != _home_only_user:
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
                owner, group = _get_owner_group(stat)
                item_data = {
                    'name': entry.name,
                    'is_dir': entry.is_dir(),
                    'is_link': entry.is_symlink(),
                    'size': stat.st_size if not entry.is_dir() else 0,
                    'modified': stat.st_mtime,
                    'permissions': oct(stat.st_mode)[-3:],
                    'permissions_symbolic': _mode_to_symbolic(stat.st_mode),
                    'permissions_octal': '0' + oct(stat.st_mode & 0o7777)[2:],
                    'can_read': os.access(entry.path, os.R_OK),
                    'can_write': os.access(entry.path, os.W_OK),
                    'owner': owner,
                    'group': group,
                }
                # Mark protected dirs with locked flag
                if entry.is_dir():
                    child_path = (path.rstrip('/') + '/' + entry.name) if path != '/' else ('/' + entry.name)
                    if child_path in passwords:
                        item_data['locked'] = not _is_folder_unlocked(child_path)
                        item_data['protected'] = True
                items.append(item_data)
            except (PermissionError, OSError):
                items.append({
                    'name': entry.name,
                    'is_dir': entry.is_dir(),
                    'is_link': False,
                    'size': 0,
                    'modified': 0,
                    'permissions': '---',
                    'permissions_symbolic': '---------',
                })
    except PermissionError:
        return jsonify({'error': t('auth.no_permission')}), 403
    except TimeoutError:
        return jsonify({'error': 'Disk not responding — try again shortly'}), 504

    if _cache_key:
        _listdir_cache_set(_cache_key, items, mtime=_dir_mtime)

    resp = jsonify({'path': path, 'items': items})
    if _etag:
        resp.headers['ETag'] = _etag
        resp.headers['Cache-Control'] = 'no-cache'
    return resp


@files_bp.route('/api/files/search')
@require_auth
def files_search():
    path = request.args.get('path', '/')
    query = request.args.get('q', '').lower()
    real_path = safe_path(path)
    if not real_path or not query:
        return jsonify({'items': []})

    # Cache passwords once per request (not per os.walk iteration)
    pw_folders = _load_folder_passwords()
    data_real = os.path.realpath(DATA_ROOT)
    try:
        max_depth = int(request.args.get('depth', 6))
    except (ValueError, TypeError):
        max_depth = 6
    base_depth = real_path.rstrip('/').count('/')

    import time as _time
    deadline = _time.monotonic() + 5  # 5 second timeout

    # ── Home isolation for search: never descend into other users' homes ──
    _search_home_user = None
    _home_dirs = ['/home']
    _dd = _get_data_disk()
    if _dd:
        _home_dirs.append(os.path.join(_dd, 'home'))
    _scur = get_current_user()
    if _scur:
        _search_home_user = _scur['username']

    results = []
    truncated = False
    try:
        for root, dirs, files in os.walk(real_path):
            # Enforce depth limit
            cur_depth = root.rstrip('/').count('/') - base_depth
            if cur_depth >= max_depth:
                dirs[:] = []
                continue
            # Enforce time limit
            if _time.monotonic() > deadline:
                truncated = True
                break
            # Skip .trash, .thumbs, and password-protected directories
            def _dir_allowed(d):
                if d in ('.trash', '.thumbs'):
                    return False
                rel = '/' + os.path.relpath(os.path.join(root, d), data_real)
                if rel in pw_folders and not _is_folder_unlocked(rel):
                    return False
                # Home isolation – when walking a home dir, only descend into own dir
                if _search_home_user and root in _home_dirs and d != _search_home_user:
                    return False
                return True
            dirs[:] = [d for d in dirs if _dir_allowed(d)]
            for name in dirs + files:
                if query in name.lower():
                    rel = os.path.relpath(os.path.join(root, name), data_real)
                    full = os.path.join(root, name)
                    try:
                        stat = os.stat(full)
                        results.append({
                            'name': name,
                            'path': '/' + rel,
                            'is_dir': os.path.isdir(full),
                            'size': stat.st_size,
                            'modified': stat.st_mtime
                        })
                    except Exception:
                        pass
                    if len(results) >= 200:
                        truncated = True
                        break
            if truncated:
                break
    except Exception:
        pass
    return jsonify({'items': results, 'truncated': truncated})


@files_bp.route('/api/files/dir-sizes', methods=['POST'])
@require_auth
def files_dir_sizes():
    """Calculate total size for a list of directories (with TTL cache)."""
    data = request.json or {}
    paths = data.get('paths', [])
    if not paths or not isinstance(paths, list):
        return jsonify({'error': 'Provide a list of paths'}), 400

    deadline = time.monotonic() + 10  # 10s total timeout

    results = {}
    for p in paths[:50]:  # limit to 50 dirs per request
        # Return cached result if available and fresh
        cached = _dirsize_cache_get(p)
        if cached is not None:
            results[p] = cached
            continue
        real = safe_path(p)
        if not real or not os.path.isdir(real):
            results[p] = 0
            continue
        total = 0
        timed_out = False
        try:
            stack = [real]
            while stack:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                cur = stack.pop()
                try:
                    for entry in os.scandir(cur):
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in ('.trash', '.thumbs'):
                                    stack.append(entry.path)
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
                except (PermissionError, OSError):
                    pass
        except (PermissionError, OSError):
            pass
        if not timed_out:
            _dirsize_cache_set(p, total)
        results[p] = total
        if timed_out:
            break
    return jsonify({'sizes': results})


def _calc_dir_size_worker(job_id, path, real):
    """Background greenlet: calculate dir size and store in job + cache.

    Uses an iterative os.scandir stack for better performance on large trees
    (avoids repeated string joins from os.walk and leverages DirEntry caching).
    """
    _PSEUDO_FS = {'/proc', '/sys', '/dev', '/run'}
    # Skip pseudo-filesystems early
    if real in _PSEUDO_FS or any(real.startswith(p + '/') for p in _PSEUDO_FS):
        _dirsize_cache_set(path, 0)
        with _dirsize_bg_lock:
            if job_id in _dirsize_bg_jobs:
                _dirsize_bg_jobs[job_id].update({'size': 0, 'done': True, 'error': False, 'ts': time.monotonic()})
            _dirsize_running_by_path.pop(path, None)
        return

    total = 0
    _scan_count = 0
    try:
        stack = [real]
        while stack:
            cur = stack.pop()
            try:
                for entry in os.scandir(cur):
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in ('.trash', '.thumbs'):
                                stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
                    _scan_count += 1
                    # Yield to gevent event loop every 500 entries to avoid blocking
                    if _scan_count % 500 == 0:
                        gevent.sleep(0)
            except (PermissionError, OSError):
                pass
        _dirsize_cache_set(path, total)
        with _dirsize_bg_lock:
            if job_id in _dirsize_bg_jobs:
                _dirsize_bg_jobs[job_id].update({'size': total, 'done': True, 'error': False, 'ts': time.monotonic()})
            _dirsize_running_by_path.pop(path, None)
    except Exception:
        with _dirsize_bg_lock:
            if job_id in _dirsize_bg_jobs:
                _dirsize_bg_jobs[job_id].update({'size': 0, 'done': True, 'error': True, 'ts': time.monotonic()})
            _dirsize_running_by_path.pop(path, None)


@files_bp.route('/api/files/dir-sizes-start', methods=['POST'])
@require_auth
def files_dir_sizes_start():
    """Start background dir-size calculations, returning job IDs.

    Request body: { "paths": ["/path/to/dir", ...] }
    Response: { "jobs": { "/path": "job_id", ... }, "cached": { "/path": size, ... } }
    """
    data = request.json or {}
    paths = data.get('paths', [])
    if not paths or not isinstance(paths, list):
        return jsonify({'error': 'Provide a list of paths'}), 400

    jobs = {}
    cached = {}
    # Prune stale completed jobs to keep _dirsize_bg_jobs tidy
    _dirsize_bg_jobs_prune()
    # Pseudo-filesystems and virtual paths that block gevent event loop
    _PSEUDO_FS_PATHS = {'/proc', '/sys', '/dev', '/run', '/snap', '/snap/bin'}

    for p in paths[:50]:
        # Skip pseudo-filesystems — scanning them would block the gevent event loop
        if p in _PSEUDO_FS_PATHS or p.startswith('/proc/') or p.startswith('/sys/') or p.startswith('/dev/'):
            cached[p] = 0
            continue
        # Return from cache immediately if fresh
        cv = _dirsize_cache_get(p)
        if cv is not None:
            cached[p] = cv
            continue
        real = safe_path(p)
        if not real or not os.path.isdir(real):
            cached[p] = 0
            continue
        # Reuse an existing running job for this path — avoids duplicate workers
        with _dirsize_bg_lock:
            existing_jid = _dirsize_running_by_path.get(p)
            if (existing_jid and existing_jid in _dirsize_bg_jobs
                    and not _dirsize_bg_jobs[existing_jid].get('done')):
                jobs[p] = existing_jid
                continue
        job_id = hashlib.md5(f'{p}:{time.monotonic()}'.encode()).hexdigest()[:12]
        with _dirsize_bg_lock:
            _dirsize_bg_jobs[job_id] = {'path': p, 'size': None, 'done': False, 'error': False, 'ts': time.monotonic()}
            _dirsize_running_by_path[p] = job_id
        gevent.spawn(_calc_dir_size_worker, job_id, p, real)
        jobs[p] = job_id
    return jsonify({'jobs': jobs, 'cached': cached})


@files_bp.route('/api/files/dir-sizes-result', methods=['POST'])
@require_auth
def files_dir_sizes_result():
    """Poll background dir-size jobs.

    Request body: { "jobs": { "/path": "job_id", ... } }
    Response: { "sizes": { "/path": size }, "pending": ["/path", ...] }
    """
    data = request.json or {}
    jobs = data.get('jobs', {})
    sizes = {}
    pending = []
    with _dirsize_bg_lock:
        for path, job_id in jobs.items():
            entry = _dirsize_bg_jobs.get(job_id)
            if entry is None:
                # Job expired/unknown — check cache
                cv = _dirsize_cache_get(path)
                if cv is not None:
                    sizes[path] = cv
                else:
                    pending.append(path)
            elif entry['done']:
                sizes[path] = entry.get('size') or 0
                del _dirsize_bg_jobs[job_id]
            else:
                pending.append(path)
    return jsonify({'sizes': sizes, 'pending': pending})


@files_bp.route('/api/files/download')
@require_auth
def files_download():
    path = request.args.get('path', '')
    real_path = safe_path(path)
    if not real_path or not os.path.isfile(real_path):
        return jsonify({'error': 'File not found'}), 404
    # Enforce allowed roots even for admins — prevents reading /etc/shadow etc.
    if not any(real_path == r or real_path.startswith(r + '/') for r in _ALLOWED_ROOTS):
        return jsonify({'error': 'Access denied'}), 403
    # Block download from protected folders
    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked
    resp = send_file(real_path, as_attachment=True, conditional=True)
    # Advertise byte-range support so browsers can resume interrupted downloads (HTTP 206)
    resp.headers['Accept-Ranges'] = 'bytes'

    # Log download start (only for full files, not partial ranges to avoid spam)
    if 'Range' not in request.headers:
        cur = get_current_user()
        username = cur['username'] if cur else 'unknown'
        elog('files', 'info', f'File download: {path}', {'user': username, 'path': path, 'size': os.path.getsize(real_path)})

    return resp


@files_bp.route('/api/files/download-zip', methods=['POST'])
@require_auth
def files_download_zip():
    """Create a ZIP from selected files/folders with progress, return download ID."""
    data = request.json or {}
    sources = data.get('sources', [])
    if not sources:
        return jsonify({'error': 'No files to download'}), 400

    resolved = []
    for s in sources:
        rp = safe_path(s)
        if rp and os.path.exists(rp):
            resolved.append(rp)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    total = _count_items(resolved)

    # Determine archive filename
    if len(resolved) == 1:
        zip_name = os.path.splitext(os.path.basename(resolved[0]))[0] + '.zip'
    else:
        zip_name = 'download.zip'

    download_id = secrets.token_hex(16)
    tmp_path = os.path.join('/tmp', f'nasos_dl_{download_id}.zip')

    with _fileop_lock:
        if _fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'download'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False

    socketio.start_background_task(_bg_download_zip, resolved, tmp_path, zip_name, download_id, total)
    _save_zip_task(resolved, tmp_path, zip_name, download_id, total)
    elog('files', 'info', f'Started ZIP preparation: {zip_name} ({total} files)')
    return jsonify({'async': True, 'download_id': download_id,
                    'message': f'Preparing {zip_name} ({total} files)…'})


# Pending download temp files: id -> {path, name, time}
_pending_downloads = {}


def _bg_download_zip(resolved, tmp_path, zip_name, download_id, total):
    """Background ZIP creation for download with progress."""
    done = 0
    cancelled = False
    CHUNK_SIZE = 1 * 1024 * 1024       # 1 MB read chunks
    LARGE_FILE_THRESHOLD = 50 * 1024 * 1024  # 50 MB — store without compression

    def _check_cancel_pause():
        """Check cancel flag & honour pause; return True if cancelled."""
        if _fileop_cancelled():
            return True
        while _fileop_is_paused():
            gevent.sleep(0.5)
            if _fileop_cancelled():
                return True
        return False

    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for src in resolved:
                if _check_cancel_pause():
                    cancelled = True; break
                base_dir = os.path.dirname(src)
                if os.path.isdir(src):
                    for root, dirs, files in os.walk(src):
                        if _check_cancel_pause():
                            cancelled = True; break
                        rel = os.path.relpath(root, base_dir)
                        if rel != '.':
                            try:
                                zf.write(root, rel)
                            except Exception:
                                pass
                        for fn in files:
                            if _check_cancel_pause():
                                cancelled = True; break
                            full = os.path.join(root, fn)
                            arcname = os.path.relpath(full, base_dir)
                            try:
                                fsize = os.path.getsize(full)
                                if fsize > LARGE_FILE_THRESHOLD:
                                    # Large file — store without compression, write in chunks
                                    info = zipfile.ZipInfo.from_file(full, arcname)
                                    info.compress_type = zipfile.ZIP_STORED
                                    with zf.open(info, 'w') as dest:
                                        with open(full, 'rb') as fsrc:
                                            while True:
                                                if _check_cancel_pause():
                                                    cancelled = True; break
                                                chunk = fsrc.read(CHUNK_SIZE)
                                                if not chunk:
                                                    break
                                                dest.write(chunk)
                                                gevent.sleep(0)
                                else:
                                    zf.write(full, arcname)
                            except (PermissionError, OSError):
                                pass  # skip unreadable files
                            if cancelled:
                                break
                            done += 1
                            _fileop_progress('download', fn, done, total)
                            gevent.sleep(0)
                        if cancelled:
                            break
                        done += len(dirs)
                        gevent.sleep(0)
                else:
                    try:
                        fsize = os.path.getsize(src)
                        bname = os.path.basename(src)
                        if fsize > LARGE_FILE_THRESHOLD:
                            info = zipfile.ZipInfo.from_file(src, bname)
                            info.compress_type = zipfile.ZIP_STORED
                            with zf.open(info, 'w') as dest:
                                with open(src, 'rb') as fsrc:
                                    while True:
                                        if _check_cancel_pause():
                                            cancelled = True; break
                                        chunk = fsrc.read(CHUNK_SIZE)
                                        if not chunk:
                                            break
                                        dest.write(chunk)
                                        gevent.sleep(0)
                        else:
                            zf.write(src, bname)
                    except (PermissionError, OSError):
                        pass
                    if cancelled:
                        break
                    done += 1
                    _fileop_progress('download', os.path.basename(src), done, total)
                    gevent.sleep(0)

        if cancelled:
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass
            _clear_zip_task()
            _fileop_finish('download', False, 'Cancelled')
            return

        size_mb = round(os.path.getsize(tmp_path) / (1024*1024), 1)
        _pending_downloads[download_id] = {
            'path': tmp_path, 'name': zip_name, 'time': time.time()
        }
        _clear_zip_task()
        _fileop_finish('download', True, f'{zip_name} ({size_mb} MB)')
        # Emit download_id so frontend can trigger browser download
        _fileop_emit('fileop_download_ready', {'download_id': download_id, 'name': zip_name})
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass
        _clear_zip_task()
        _fileop_finish('download', False, str(e))


@files_bp.route('/api/files/download-zip/<download_id>/status')
@require_auth
def files_download_zip_status(download_id):
    """Check if a prepared ZIP is ready for download."""
    info = _pending_downloads.get(download_id)
    if info and os.path.isfile(info['path']):
        return jsonify({'ready': True, 'name': info.get('name', 'download.zip')})
    return jsonify({'ready': False})


@files_bp.route('/api/files/download-zip/<download_id>')
@require_auth
def files_download_zip_file(download_id):
    """Serve a prepared ZIP and clean up."""
    info = _pending_downloads.pop(download_id, None)
    if not info or not os.path.isfile(info['path']):
        return jsonify({'error': 'File not found or expired'}), 404

    resp = send_file(info['path'], as_attachment=True,
                     download_name=info['name'], mimetype='application/zip',
                     conditional=False)
    @resp.call_on_close
    def _cleanup():
        try:
            if os.path.exists(info['path']):
                os.remove(info['path'])
        except OSError:
            pass
    return resp


# THUMB_CACHE_DIR, THUMBS_DIR_NAME, _thumb_cache_key, _local_thumb_path — imported from utils


def _purge_thumb_cache(real_path):
    """Remove cached thumbnails for a file or all files under a directory."""
    target = os.path.realpath(real_path)
    # Purge from centralized cache
    try:
        for fn in os.listdir(THUMB_CACHE_DIR):
            if not fn.endswith('.meta'):
                continue
            meta_fp = os.path.join(THUMB_CACHE_DIR, fn)
            try:
                with open(meta_fp) as f:
                    stored_path = f.read().strip()
            except Exception:
                continue
            if stored_path == target or stored_path.startswith(target + '/'):
                base = fn[:-5]  # remove .meta
                webp_fp = os.path.join(THUMB_CACHE_DIR, base + '.webp')
                for fp in (meta_fp, webp_fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
    except Exception:
        pass
    # Purge from local .thumbs/ directory
    try:
        if os.path.isfile(target):
            parent = os.path.dirname(target)
            basename = os.path.basename(target)
            name, _ = os.path.splitext(basename)
            thumbs_dir = os.path.join(parent, THUMBS_DIR_NAME)
            if os.path.isdir(thumbs_dir):
                for fn in os.listdir(thumbs_dir):
                    if fn.startswith(name + '_') and fn.endswith('.webp'):
                        try:
                            os.remove(os.path.join(thumbs_dir, fn))
                        except OSError:
                            pass
        elif os.path.isdir(target):
            thumbs_dir = os.path.join(target, THUMBS_DIR_NAME)
            if os.path.isdir(thumbs_dir):
                import shutil
                shutil.rmtree(thumbs_dir, ignore_errors=True)
    except Exception:
        pass


# Set of supported image extensions for thumbnail pre-generation
_THUMB_IMAGE_EXTS = frozenset(('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif'))

# Global semaphore: limits simultaneous thumbnail generation greenlets.
# Prevents memory overload when multiple users / large directories trigger
# pregeneration of hundreds of thumbnails concurrently.
import gevent.lock as _gevent_lock
_PREGENERATE_CONCURRENCY = 8
_pregenerate_sem = _gevent_lock.BoundedSemaphore(_PREGENERATE_CONCURRENCY)

# Global semaphore: limits simultaneous listing-preload greenlets.
_PRELOAD_CONCURRENCY = 12
_preload_sem = _gevent_lock.BoundedSemaphore(_PRELOAD_CONCURRENCY)


@files_bp.route('/api/files/pregenerate-thumbs', methods=['POST'])
@require_auth
def files_pregenerate_thumbs():
    """Pre-generate WebP thumbnails for all images in a directory (background greenlets).

    Called by the frontend when entering thumb view so that thumbnails are warm
    before the user scrolls to them.  Returns immediately; generation runs async.
    """
    data = request.json or {}
    path = data.get('path', '')
    w = min(int(data.get('w', 120)), 400)
    h = min(int(data.get('h', 120)), 400)
    real_path = safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    def _generate_one(fpath):
        try:
            thumbs_dir, local_path = _local_thumb_path(fpath, w, h)
            if os.path.isfile(local_path):
                mtime = os.path.getmtime(fpath)
                if os.path.getmtime(local_path) >= mtime:
                    return  # already fresh — skip
            # Acquire global semaphore before generating to limit memory pressure
            _pregenerate_sem.acquire()
            try:
                generate_thumbnail(fpath, w, h)
            finally:
                _pregenerate_sem.release()
        except Exception:
            pass

    queued = 0
    try:
        for entry in os.scandir(real_path):
            if not entry.is_file(follow_symlinks=False):
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in _THUMB_IMAGE_EXTS:
                gevent.spawn(_generate_one, entry.path)
                queued += 1
                if queued >= 200:  # cap to avoid run-away greenlet creation
                    break
    except (PermissionError, OSError):
        pass
    return jsonify({'ok': True, 'queued': queued})


@files_bp.route('/api/files/preload-cache', methods=['POST'])
@require_auth
def files_preload_cache():
    """Pre-populate the listing cache for a directory and its immediate subdirs.

    Called by the frontend on hover/prefetch so that navigating into a folder
    is instant (cache hit in files_list).  Returns immediately.
    """
    data = request.json or {}
    path = data.get('path', '')
    real_path = safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    def _preload_one(rpath):
        """Scan *rpath* and store result in the listing cache (semaphore-gated)."""
        if _listdir_cache_get(rpath) is not None:
            return  # already fresh
        _preload_sem.acquire()
        try:
            # Re-check under semaphore — another greenlet may have just populated it
            if _listdir_cache_get(rpath) is not None:
                return
            items_pre = []
            _mtime = None
            try:
                _mtime = os.path.getmtime(rpath)
            except OSError:
                pass
            for entry in sorted(os.scandir(rpath), key=lambda e: (not e.is_dir(), e.name.lower())):
                if entry.name in ('.trash', '.thumbs') and entry.is_dir():
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    items_pre.append({
                        'name': entry.name,
                        'is_dir': entry.is_dir(),
                        'is_link': entry.is_symlink(),
                        'size': st.st_size if not entry.is_dir() else 0,
                        'modified': st.st_mtime,
                        'permissions': oct(st.st_mode)[-3:],
                    })
                except (PermissionError, OSError):
                    pass
            _listdir_cache_set(rpath, items_pre, mtime=_mtime)
        except (PermissionError, OSError):
            pass
        finally:
            _preload_sem.release()

    # Preload the directory itself
    gevent.spawn(_preload_one, real_path)
    queued = 1
    # Preload immediate subdirectories (limit to 20 to avoid overload)
    try:
        for entry in os.scandir(real_path):
            if entry.is_dir(follow_symlinks=False) and entry.name not in ('.trash', '.thumbs'):
                gevent.spawn(_preload_one, entry.path)
                queued += 1
                if queued >= 21:
                    break
    except (PermissionError, OSError):
        pass
    return jsonify({'ok': True, 'queued': queued})


@files_bp.route('/api/files/preview')
@require_auth
def files_preview():
    path = request.args.get('path', '')
    real_path = safe_path(path)
    if not real_path or not os.path.isfile(real_path):
        return jsonify({'error': 'File not found'}), 404

    # Block preview from protected folders
    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    w = request.args.get('w', type=int)
    h = request.args.get('h', type=int)

    # If thumbnail size requested, serve cached thumbnail with ETag for 304 support
    if w and h:
        try:
            mtime = os.path.getmtime(real_path)
            etag = _thumb_cache_key(real_path, mtime, w, h)
            if request.headers.get('If-None-Match') == etag:
                return '', 304
            resp = generate_thumbnail(real_path, w, h)
            resp.headers['ETag'] = etag
            return resp
        except Exception:
            return generate_thumbnail(real_path, w, h)
    return send_file(real_path)


def _sanitize_filename(filename):
    """Strip path separators and dangerous characters from an uploaded filename."""
    # Take only the basename — remove any directory components
    name = os.path.basename(filename)
    # Collapse .. and strip control chars / null bytes
    name = name.replace('\x00', '').replace('..', '_')
    # Remove shell-dangerous characters
    name = re.sub(r'[<>:"|?*\\]', '_', name)
    # Strip leading dots (hidden files attack) — optional, keep at least one char
    name = name.lstrip('.')
    if not name:
        name = 'upload_' + str(int(time.time()))
    return name[:255]


@files_bp.route('/api/files/upload', methods=['POST'])
@require_auth
def files_upload():
    path = request.form.get('path', '/')
    real_path = safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    files = request.files.getlist('files')
    rel_paths = request.form.getlist('rel_paths')  # optional: relative paths for folder uploads

    uploaded = []
    errors = []
    for idx, f in enumerate(files):
        # Use relative path if provided (folder upload), else just filename
        if rel_paths and idx < len(rel_paths) and rel_paths[idx]:
            rel = rel_paths[idx]
            # Sanitize each path component
            parts = [_sanitize_filename(p) for p in rel.replace('\\', '/').split('/') if p and p != '..']
            if not parts:
                parts = [_sanitize_filename(f.filename)]
            # Create intermediate directories
            if len(parts) > 1:
                subdir = os.path.join(real_path, *parts[:-1])
                try:
                    os.makedirs(subdir, exist_ok=True)
                except PermissionError:
                    errors.append({'name': f.filename, 'error': 'No write permission'})
                    continue
                # chown intermediate dirs
                cur = real_path
                for p in parts[:-1]:
                    cur = os.path.join(cur, p)
                    _chown_to_user(cur)
            filepath = os.path.join(real_path, *parts)
        else:
            safe_name = _sanitize_filename(f.filename)
            filepath = os.path.join(real_path, safe_name)
        # Atomic write: save to temp then rename to avoid partial files
        tmp_filepath = filepath + '.ethos_upload_tmp'
        try:
            f.save(tmp_filepath)
            os.replace(tmp_filepath, filepath)
        except PermissionError:
            try: os.remove(tmp_filepath)
            except OSError: pass
            errors.append({'name': f.filename, 'error': 'No write permission'})
            continue
        except OSError as e:
            try: os.remove(tmp_filepath)
            except OSError: pass
            if e.errno == errno.ENOSPC:
                errors.append({'name': f.filename, 'error': 'No disk space available'})
            else:
                errors.append({'name': f.filename, 'error': f'Write error: {e.strerror}'})
            continue
        _chown_to_user(filepath)
        uploaded.append(os.path.relpath(filepath, real_path))
    if uploaded:
        elog('files', 'info', f'Uploaded {len(uploaded)} file(s) to {path}', {'files': uploaded[:20]})
        _listdir_cache_invalidate(path)
        _dirsize_cache_invalidate(real_path)
    if errors and not uploaded:
        # All files failed — return error status
        first_err = errors[0]['error']
        status = 403 if first_err == 'No write permission' else 507 if 'disk space' in first_err else 500
        return jsonify({'error': first_err, 'errors': errors}), status
    return jsonify({'uploaded': uploaded, 'errors': errors})


# ── Chunked / resumable upload ──────────────────────────────────────────────
# For large files: client splits into chunks, sends them one by one with session ID.
# Server assembles chunks and writes final file atomically.

_upload_sessions = {}          # session_id → session_dict
_upload_sessions_lock = _threading.Lock()
_UPLOAD_SESSION_TTL = 3600     # 1 hour — stale sessions are cleaned up
# Use persistent storage so sessions survive server restarts (/tmp is tmpfs and cleared on reboot)
_UPLOAD_TMP_DIR = '/opt/ethos/uploads/chunks'

def _upload_session_tmpdir(session_id):
    return os.path.join(_UPLOAD_TMP_DIR, session_id)

def _upload_sessions_prune():
    """Remove stale upload sessions and their temp dirs."""
    now = time.monotonic()
    with _upload_sessions_lock:
        stale = [sid for sid, s in _upload_sessions.items()
                 if now - s.get('created_at', now) > _UPLOAD_SESSION_TTL]
    for sid in stale:
        tmpdir = _upload_session_tmpdir(sid)
        try: shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception: pass
        with _upload_sessions_lock:
            _upload_sessions.pop(sid, None)

def _persist_upload_session(session):
    """Save upload session metadata to disk so it survives app restarts."""
    tmpdir = session.get('tmpdir', '')
    if not tmpdir:
        return
    meta = {k: v for k, v in session.items() if k != 'uploaded_chunks'}
    try:
        _save_json(os.path.join(tmpdir, 'session.json'), meta)
    except Exception:
        pass

def _restore_upload_sessions():
    """Scan upload tmpdir for persisted sessions and restore them into memory.

    Rebuilds uploaded_chunks by checking which chunk files actually exist on disk.
    Called once at startup so clients can resume interrupted chunked uploads.
    """
    if not os.path.isdir(_UPLOAD_TMP_DIR):
        return
    restored = 0
    now = time.monotonic()
    try:
        for entry in os.scandir(_UPLOAD_TMP_DIR):
            if not entry.is_dir():
                continue
            session_file = os.path.join(entry.path, 'session.json')
            if not os.path.isfile(session_file):
                continue
            try:
                session = _load_json(session_file, None)
                if not session or not isinstance(session, dict):
                    continue
                session_id = session.get('session_id', '')
                if not session_id:
                    continue
                # Skip sessions older than TTL (created_at is monotonic so use mtime as fallback)
                created_at = session.get('created_at', 0)
                mtime = entry.stat().st_mtime
                age = time.time() - mtime
                if age > _UPLOAD_SESSION_TTL:
                    continue
                # Rebuild uploaded_chunks from actual chunk files on disk
                num_chunks = session.get('num_chunks', 0)
                tmpdir = session.get('tmpdir', entry.path)
                existing_chunks = [
                    i for i in range(num_chunks)
                    if os.path.isfile(os.path.join(tmpdir, f'chunk_{i:06d}'))
                ]
                session['uploaded_chunks'] = existing_chunks
                session['tmpdir'] = tmpdir
                # created_at in restored sessions is wall-clock offset; set to now minus age
                session['created_at'] = now - age
                with _upload_sessions_lock:
                    if session_id not in _upload_sessions:
                        _upload_sessions[session_id] = session
                        restored += 1
            except Exception:
                pass
    except Exception:
        pass
    if restored:
        elog('files', 'info', f'Restored {restored} upload sessions after restart')


@files_bp.route('/api/files/upload-chunk-init', methods=['POST'])
@require_auth
def files_upload_chunk_init():
    """Initialize a resumable chunked upload session.

    Body: {path, filename, size, chunk_size, create_dir}
    create_dir=true: create destination directory if it doesn't exist (needed for folder uploads).
    Returns: {session_id, uploaded_chunks: [], chunk_size}
    """
    data = request.json or {}
    dest_path = data.get('path', '/')
    filename = data.get('filename', '')
    total_size = int(data.get('size', 0))
    chunk_size = int(data.get('chunk_size', 5 * 1024 * 1024))  # default 5 MB
    create_dir = bool(data.get('create_dir', False))

    real_path = safe_path(dest_path)
    if not real_path:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.isdir(real_path):
        if create_dir:
            try:
                os.makedirs(real_path, exist_ok=True)
                _chown_to_user(real_path)
            except Exception as e:
                return jsonify({'error': f'Cannot create folder: {e}'}), 400
        else:
            return jsonify({'error': 'Invalid path'}), 400
    if not filename:
        return jsonify({'error': 'Filename is required'}), 400
    if total_size <= 0:
        return jsonify({'error': 'Invalid file size'}), 400

    safe_name = _sanitize_filename(filename)
    session_id = secrets.token_hex(16)
    tmpdir = _upload_session_tmpdir(session_id)
    os.makedirs(tmpdir, exist_ok=True)

    num_chunks = max(1, (total_size + chunk_size - 1) // chunk_size)
    session = {
        'session_id': session_id,
        'dest_path': dest_path,
        'real_path': real_path,
        'filename': safe_name,
        'total_size': total_size,
        'chunk_size': chunk_size,
        'num_chunks': num_chunks,
        'uploaded_chunks': [],
        'created_at': time.monotonic(),
        'tmpdir': tmpdir,
        'username': (get_current_user() or {}).get('username'),
    }
    with _upload_sessions_lock:
        _upload_sessions[session_id] = session

    # Persist session metadata to disk so it survives app restarts
    _persist_upload_session(session)

    return jsonify({'session_id': session_id, 'uploaded_chunks': [], 'num_chunks': num_chunks})


@files_bp.route('/api/files/upload-chunk', methods=['POST'])
@require_auth
def files_upload_chunk():
    """Upload a single chunk for a resumable upload session.

    Form fields: session_id, chunk_index, checksum (optional SHA-256 hex of this chunk)
    File: 'chunk' — the binary data
    Returns: {ok: true, uploaded_chunks: [...]}
    """
    session_id = request.form.get('session_id', '')
    chunk_index = request.form.get('chunk_index', type=int)
    chunk_file = request.files.get('chunk')
    expected_checksum = request.form.get('checksum', '')  # optional SHA-256 of this chunk

    if chunk_index is None or not chunk_file:
        return jsonify({'error': 'Missing data'}), 400

    with _upload_sessions_lock:
        session = _upload_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'Unknown upload session', 'expired': True}), 404

    if chunk_index < 0 or chunk_index >= session['num_chunks']:
        return jsonify({'error': 'Invalid chunk index'}), 400

    chunk_path = os.path.join(session['tmpdir'], f'chunk_{chunk_index:06d}')
    tmp_chunk = chunk_path + '.tmp'
    try:
        chunk_file.save(tmp_chunk)
        # Verify SHA-256 checksum if provided
        if expected_checksum:
            import hashlib as _hashlib
            sha = _hashlib.sha256()
            with open(tmp_chunk, 'rb') as cf:
                for buf in iter(lambda: cf.read(65536), b''):
                    sha.update(buf)
            actual = sha.hexdigest()
            if actual != expected_checksum.lower():
                try: os.remove(tmp_chunk)
                except OSError: pass
                return jsonify({'error': f'Chunk {chunk_index} integrity error: checksum mismatch'}), 400
        os.replace(tmp_chunk, chunk_path)
    except Exception as e:
        try: os.remove(tmp_chunk)
        except OSError: pass
        return jsonify({'error': f'Chunk write error: {e}'}), 500

    with _upload_sessions_lock:
        if chunk_index not in session['uploaded_chunks']:
            session['uploaded_chunks'].append(chunk_index)
        uploaded = list(session['uploaded_chunks'])

    return jsonify({'ok': True, 'uploaded_chunks': uploaded})


@files_bp.route('/api/files/upload-status/<session_id>', methods=['GET'])
@require_auth
def files_upload_status(session_id):
    """Return current upload session status."""
    with _upload_sessions_lock:
        session = _upload_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'Nieznana sesja', 'expired': True}), 404
    return jsonify({
        'session_id': session_id,
        'uploaded_chunks': list(session['uploaded_chunks']),
        'num_chunks': session['num_chunks'],
        'total_size': session['total_size'],
    })


@files_bp.route('/api/files/upload-complete', methods=['POST'])
@require_auth
def files_upload_complete():
    """Finalize a chunked upload: assemble chunks into the destination file.

    Body: {session_id}
    Returns: {ok: true, filename, sha256 (if verify_checksum was requested)}
    """
    data = request.json or {}
    session_id = data.get('session_id', '')
    verify_checksum = bool(data.get('verify_checksum', False))  # optional: compute SHA-256 of assembled file

    with _upload_sessions_lock:
        session = _upload_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'Unknown upload session', 'expired': True}), 404

    num_chunks = session['num_chunks']
    uploaded = set(session['uploaded_chunks'])
    missing = [i for i in range(num_chunks) if i not in uploaded]
    if missing:
        return jsonify({'error': f'Missing chunks: {missing[:10]}', 'missing_chunks': missing}), 400

    real_path = session['real_path']
    filename = session['filename']
    tmpdir = session['tmpdir']
    username = session.get('username')

    if not os.path.isdir(real_path):
        return jsonify({'error': 'Destination folder does not exist'}), 400

    dest_file = os.path.join(real_path, filename)
    tmp_dest = dest_file + '.ethos_upload_tmp'
    file_sha256 = None

    try:
        import hashlib as _hashlib
        sha = _hashlib.sha256() if verify_checksum else None
        with open(tmp_dest, 'wb') as out:
            for i in range(num_chunks):
                chunk_path = os.path.join(tmpdir, f'chunk_{i:06d}')
                with open(chunk_path, 'rb') as cf:
                    while True:
                        buf = cf.read(65536)
                        if not buf:
                            break
                        out.write(buf)
                        if sha:
                            sha.update(buf)
        if sha:
            file_sha256 = sha.hexdigest()
        os.replace(tmp_dest, dest_file)
        _chown_to_user(dest_file, username)
    except Exception as e:
        try: os.remove(tmp_dest)
        except OSError: pass
        return jsonify({'error': f'File assembly error: {e}'}), 500
    finally:
        # Clean up temp chunks regardless of outcome
        try: shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception: pass
        with _upload_sessions_lock:
            _upload_sessions.pop(session_id, None)

    dest_path = session['dest_path']
    elog('files', 'info', f'Uploaded (chunked) {filename} to {dest_path}')
    _listdir_cache_invalidate(dest_path)
    _dirsize_cache_invalidate(real_path)
    result = {'ok': True, 'filename': filename}
    if file_sha256:
        result['sha256'] = file_sha256
    return jsonify(result)


@files_bp.route('/api/files/upload-abort', methods=['POST'])
@require_auth
def files_upload_abort():
    """Abort a chunked upload session and clean up temp files.

    Body: {session_id}
    """
    data = request.json or {}
    session_id = data.get('session_id', '')

    with _upload_sessions_lock:
        session = _upload_sessions.pop(session_id, None)

    if session:
        tmpdir = session.get('tmpdir', '')
        if tmpdir:
            try: shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception: pass
    return jsonify({'ok': True})


@files_bp.route('/api/files/mkdir', methods=['POST'])
@require_auth
def files_mkdir():
    data = request.json or {}
    real_path = safe_path(data.get('path', ''))
    if not real_path:
        return jsonify({'error': 'Invalid path'}), 400

    blocked = _require_folder_access(data.get('path', ''))
    if blocked is not None:
        return blocked

    try:
        os.makedirs(real_path, exist_ok=True)
        _chown_to_user(real_path)
        _dirsize_cache_invalidate(os.path.dirname(real_path))
        _listdir_cache_invalidate(data.get('path', ''))
        elog('files', 'info', f'Created folder: {data.get("path", "")}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────── Phone Sync ───────────────────────────

PHONE_SYNC_META_DIR = _data_path('phone_sync')
os.makedirs(PHONE_SYNC_META_DIR, exist_ok=True)

def _get_sync_username():
    """Get username from the current auth token."""
    user = get_current_user()
    return user['username'] if user else 'admin'

def _sync_meta_path(username=None):
    if not username:
        username = _get_sync_username()
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '', username)
    return os.path.join(PHONE_SYNC_META_DIR, f'{safe}.json')

def _default_dest(username=None):
    if not username:
        username = _get_sync_username()
    photos_folder, _ = _get_photo_folders()
    return f'home/{username}/{photos_folder}/Phone'

def _load_sync_meta(username=None):
    data = _load_json(_sync_meta_path(username), None)
    if data is not None:
        return data
    return {'synced': {}, 'dest': _default_dest(username)}

def _save_sync_meta(meta, username=None):
    _save_json(_sync_meta_path(username), meta)


@files_bp.route('/api/sync/check', methods=['POST'])
@require_auth
def sync_check():
    """Check which files are already synced. Checks both metadata AND actual files on disk."""
    data = request.json or {}
    files = data.get('files', [])
    meta = _load_sync_meta()
    synced = meta.get('synced', {})
    dest_base = meta.get('dest', _default_dest())
    needed = []
    already = []
    for f in files:
        key = f'{f["name"]}_{f["size"]}'
        # Check metadata first
        if key in synced:
            already.append(f['name'])
            continue
        # Also check if file physically exists on disk with same name and size
        rel_path = f.get('rel_path', f['name'])
        dest_dir = safe_path(os.path.join(dest_base, os.path.dirname(rel_path)))
        if dest_dir:
            dest_file = os.path.join(dest_dir, os.path.basename(f['name']))
            if os.path.isfile(dest_file):
                try:
                    existing_size = os.path.getsize(dest_file)
                    if existing_size == int(f.get('size', 0)):
                        # File exists on disk with same size — mark as synced
                        synced[key] = {'ts': time.time(), 'path': rel_path}
                        already.append(f['name'])
                        continue
                except OSError:
                    pass
        needed.append(f['name'])
    # Save any newly discovered existing files
    if len(already) > len([k for k in synced if synced.get(k, {}).get('ts', 0) == 0]):
        meta['synced'] = synced
        _save_sync_meta(meta)
    return jsonify({'needed': needed, 'already': already})


@files_bp.route('/api/sync/upload', methods=['POST'])
@require_auth
def sync_upload():
    """Upload a single photo/video from phone. Preserves subfolder structure."""
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file'}), 400

    rel_path = request.form.get('rel_path', _sanitize_filename(f.filename))
    try:
        file_size = int(request.form.get('size', 0))
    except (ValueError, TypeError):
        file_size = 0
    mtime_ms = request.form.get('mtime_ms', '')

    meta = _load_sync_meta()
    dest_base = meta.get('dest', _default_dest())

    # Build destination path
    dest_dir = safe_path(os.path.join(dest_base, os.path.dirname(rel_path)))
    if not dest_dir:
        return jsonify({'error': 'Invalid path'}), 400
    os.makedirs(dest_dir, exist_ok=True)
    _chown_to_user(dest_dir)

    dest_file = os.path.join(dest_dir, os.path.basename(rel_path))

    # If file exists with same size, skip
    if os.path.isfile(dest_file) and os.path.getsize(dest_file) == file_size and file_size > 0:
        key = f'{os.path.basename(rel_path)}_{file_size}'
        meta['synced'][key] = {'ts': time.time(), 'path': rel_path}
        _save_sync_meta(meta)
        return jsonify({'status': 'exists', 'name': os.path.basename(rel_path)})

    # Atomic write: save to temp then rename to avoid partial files on interrupt
    tmp_dest = dest_file + '.ethos_upload_tmp'
    try:
        f.save(tmp_dest)
        os.replace(tmp_dest, dest_file)
    except Exception:
        try: os.remove(tmp_dest)
        except OSError: pass
        raise
    _chown_to_user(dest_file)

    # Record in sync meta
    actual_size = os.path.getsize(dest_file)
    key = f'{os.path.basename(rel_path)}_{actual_size}'
    meta['synced'][key] = {'ts': time.time(), 'path': rel_path}
    _save_sync_meta(meta)

    elog('sync', 'info', f'Zsynchronizowano z telefonu: {rel_path}')
    return jsonify({'status': 'uploaded', 'name': os.path.basename(rel_path)})


@files_bp.route('/api/sync/status')
@require_auth
def sync_status():
    """Return sync statistics."""
    meta = _load_sync_meta()
    dest = meta.get('dest', _default_dest())
    real_dest = safe_path(dest)
    total_files = 0
    total_size = 0
    if real_dest and os.path.isdir(real_dest):
        for root, dirs, files in os.walk(real_dest):
            for fn in files:
                fp = os.path.join(root, fn)
                total_files += 1
                try:
                    total_size += os.path.getsize(fp)
                except OSError:
                    pass
    return jsonify({
        'synced_count': len(meta.get('synced', {})),
        'dest': dest,
        'total_files': total_files,
        'total_size': total_size,
    })


@files_bp.route('/api/sync/config', methods=['GET', 'POST'])
@require_auth
def sync_config():
    """Get or set sync destination folder."""
    meta = _load_sync_meta()
    if request.method == 'POST':
        data = request.json or {}
        new_dest = data.get('dest', '').strip('/')
        if new_dest:
            real = safe_path(new_dest)
            if not real:
                return jsonify({'error': 'Invalid path'}), 400
            os.makedirs(real, exist_ok=True)
            _chown_to_user(real)
            meta['dest'] = new_dest
            _save_sync_meta(meta)
    return jsonify({'dest': meta.get('dest', _default_dest())})


@files_bp.route('/api/sync/reset', methods=['POST'])
@require_auth
def sync_reset():
    """Reset sync tracking (doesn't delete files)."""
    meta = _load_sync_meta()
    meta['synced'] = {}
    _save_sync_meta(meta)
    return jsonify({'ok': True})


@files_bp.route('/api/sync/folders')
@require_auth
def sync_folders():
    """Browse folders for sync destination picker. Returns only directories."""
    path = request.args.get('path', '/')
    real_path = safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    items, err = _list_dir(real_path, dirs_only=True)
    if err:
        return jsonify({'error': err}), 403
    folders = [i['name'] for i in items]
    return jsonify({'path': path, 'folders': folders})


@files_bp.route('/api/sync/qr-code')
def sync_qr_code():
    """Generate QR code PNG for the given URL."""
    import segno
    import io
    url = request.args.get('url', '')
    if not url:
        return 'Missing url parameter', 400
    qr = segno.make(url, error='L')
    buf = io.BytesIO()
    qr.save(buf, kind='png', scale=6, border=4)
    buf.seek(0)
    return send_file(buf, mimetype='image/png', download_name='qr.png')


# ─────────────────────────── Trash (Kosz) ───────────────────────────

TRASH_DIR = _data_path('.trash')               # centralized fallback
TRASH_META_FILE = _data_path('trash_meta.json')
TRASH_RETENTION_DAYS = 30

os.makedirs(TRASH_DIR, exist_ok=True)


def _get_trash_dir_for_path(real_path):
    """Return the .trash directory on the same filesystem as real_path.

    Finds the mount-point root by walking up until st_dev changes,
    then places .trash there.  Falls back to TRASH_DIR on error.
    """
    try:
        target_dev = os.stat(real_path).st_dev
        cur = os.path.dirname(os.path.realpath(real_path))
        while True:
            parent = os.path.dirname(cur)
            if parent == cur:
                # reached /
                break
            if os.stat(parent).st_dev != target_dev:
                # cur is the mount root
                break
            cur = parent
        trash = os.path.join(cur, '.trash')
        os.makedirs(trash, exist_ok=True)
        return trash
    except Exception:
        os.makedirs(TRASH_DIR, exist_ok=True)
        return TRASH_DIR


def _resolve_trash_path(item):
    """Return the full path to a trashed item, handling both old and new entries."""
    td = item.get('trash_dir', TRASH_DIR)
    return os.path.join(td, item['trash_id'])


def _load_trash_meta():
    return _load_json(TRASH_META_FILE, [])

def _save_trash_meta(meta):
    _save_json(TRASH_META_FILE, meta)


def _trash_cleanup():
    """Remove trash items older than TRASH_RETENTION_DAYS."""
    meta = _load_trash_meta()
    now = time.time()
    cutoff = now - TRASH_RETENTION_DAYS * 86400
    remaining = []
    removed = 0
    for item in meta:
        if item.get('deleted_at', 0) < cutoff:
            trash_path = _resolve_trash_path(item)
            try:
                if os.path.isdir(trash_path):
                    shutil.rmtree(trash_path)
                elif os.path.exists(trash_path):
                    os.remove(trash_path)
                removed += 1
            except Exception:
                pass
        else:
            remaining.append(item)
    if removed > 0:
        _save_trash_meta(remaining)
        elog('files', 'info', f'Trash: automatically removed {removed} items older than {TRASH_RETENTION_DAYS} days')


def _start_trash_scheduler():
    """Run trash cleanup every 6 hours."""
    def _loop():
        while True:
            gevent.sleep(6 * 3600)
            try:
                _trash_cleanup()
            except Exception:
                pass
    gevent.spawn(_loop)
    # Also run once on startup (delayed)
    gevent.spawn_later(30, _trash_cleanup)


@files_bp.route('/api/files/delete', methods=['DELETE'])
@require_auth
def files_delete():
    """Move files to trash instead of permanent delete."""
    data = request.json or {}
    paths = data.get('paths', [])
    permanent = data.get('permanent', False)
    if isinstance(data.get('path'), str):
        paths = [data['path']]

    deleted = []
    errors = []
    meta = _load_trash_meta()

    # Track deleted items for logging
    deleted_log = []

    for p in paths:
        # Security check
        blocked = _require_folder_access(p)
        if blocked is not None:
            errors.append(f'Access denied: {p} (protected folder)')
            continue

        real_path = safe_path(p)
        if not real_path:
            errors.append(f'Invalid path: {p}')
            continue
        if not os.path.exists(real_path):
            errors.append(f'Does not exist: {p}')
            continue
        try:
            _purge_thumb_cache(real_path)
            _dirsize_cache_invalidate(os.path.dirname(real_path))
            _listdir_cache_invalidate(p)

            if permanent:
                # Permanent delete (from trash empty or explicit)
                if os.path.isdir(real_path):
                    shutil.rmtree(real_path)
                else:
                    os.remove(real_path)
            else:
                # Move to trash — on the same filesystem for instant rename
                local_trash = _get_trash_dir_for_path(real_path)
                trash_id = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}"
                trash_dest = os.path.join(local_trash, trash_id)
                shutil.move(real_path, trash_dest)

                # Compute size
                if os.path.isdir(trash_dest):
                    size = sum(
                        os.path.getsize(os.path.join(dp, f))
                        for dp, _, fns in os.walk(trash_dest)
                        for f in fns
                    )
                else:
                    size = os.path.getsize(trash_dest)

                meta.append({
                    'trash_id': trash_id,
                    'trash_dir': local_trash,
                    'original_path': p,
                    'name': os.path.basename(real_path),
                    'is_dir': os.path.isdir(trash_dest),
                    'size': size,
                    'deleted_at': time.time(),
                })
            deleted.append(p)
        except Exception as e:
            errors.append(str(e))

    if not permanent:
        _save_trash_meta(meta)

    if deleted:
        label = 'Permanently deleted' if permanent else 'Moved to trash'
        elog('files', 'info', f'{label} {len(deleted)} items', {'paths': deleted})
    if errors:
        elog('files', 'error', f'Deletion errors: {len(errors)}', {'errors': errors})
    return jsonify({'deleted': deleted, 'errors': errors})


@files_bp.route('/api/files/trash')
@require_auth
def files_trash_list():
    """List items in trash."""
    meta = _load_trash_meta()
    now = time.time()
    items = []
    for item in meta:
        days_left = max(0, TRASH_RETENTION_DAYS - int((now - item.get('deleted_at', 0)) / 86400))
        items.append({
            **item,
            'days_left': days_left,
            'deleted_date': datetime.fromtimestamp(item.get('deleted_at', 0)).strftime('%Y-%m-%d %H:%M'),
        })
    items.sort(key=lambda x: x.get('deleted_at', 0), reverse=True)
    return jsonify({'items': items, 'retention_days': TRASH_RETENTION_DAYS})


@files_bp.route('/api/files/trash/restore', methods=['POST'])
@require_auth
def files_trash_restore():
    """Restore items from trash to original location."""
    data = request.json or {}
    trash_ids = data.get('trash_ids', [])
    if isinstance(data.get('trash_id'), str):
        trash_ids = [data['trash_id']]

    meta = _load_trash_meta()
    restored = []
    errors = []

    for tid in trash_ids:
        item = next((m for m in meta if m['trash_id'] == tid), None)
        if not item:
            errors.append(f'Not found in trash: {tid}')
            continue

        trash_path = _resolve_trash_path(item)
        if not os.path.exists(trash_path):
            meta = [m for m in meta if m['trash_id'] != tid]
            errors.append(f'File disappeared from trash: {item["name"]}')
            continue

        original = safe_path(item['original_path'])
        if not original:
            errors.append(f'Invalid original path: {item["original_path"]}')
            continue

        try:
            # Ensure parent directory exists
            parent = os.path.dirname(original)
            os.makedirs(parent, exist_ok=True)

            # Handle name conflict
            dest = original
            if os.path.exists(dest):
                base, ext = os.path.splitext(dest)
                dest = f"{base}_restored{ext}"
                counter = 1
                while os.path.exists(dest):
                    dest = f"{base}_restored_{counter}{ext}"
                    counter += 1

            shutil.move(trash_path, dest)
            _chown_recursive(dest)
            meta = [m for m in meta if m['trash_id'] != tid]
            restored.append(item['name'])
        except Exception as e:
            errors.append(f'{item["name"]}: {str(e)}')

    _save_trash_meta(meta)
    if restored:
        elog('files', 'info', f'Restored {len(restored)} items from trash', {'names': restored})
    return jsonify({'restored': restored, 'errors': errors})


@files_bp.route('/api/files/trash/empty', methods=['POST'])
@require_auth
def files_trash_empty():
    """Permanently delete all items in trash."""
    meta = _load_trash_meta()
    removed = 0
    for item in meta:
        trash_path = _resolve_trash_path(item)
        try:
            if os.path.isdir(trash_path):
                shutil.rmtree(trash_path)
            elif os.path.exists(trash_path):
                os.remove(trash_path)
            removed += 1
        except Exception:
            pass
    _save_trash_meta([])
    elog('files', 'info', f'Trash emptied: {removed} items removed')
    return jsonify({'removed': removed})


@files_bp.route('/api/files/trash/delete', methods=['DELETE'])
@require_auth
def files_trash_delete_permanent():
    """Permanently delete specific items from trash."""
    data = request.json or {}
    trash_ids = data.get('trash_ids', [])
    if isinstance(data.get('trash_id'), str):
        trash_ids = [data['trash_id']]

    meta = _load_trash_meta()
    removed = []

    for tid in trash_ids:
        item = next((m for m in meta if m['trash_id'] == tid), None)
        if not item:
            continue
        trash_path = _resolve_trash_path(item)
        try:
            if os.path.isdir(trash_path):
                shutil.rmtree(trash_path)
            elif os.path.exists(trash_path):
                os.remove(trash_path)
        except Exception:
            pass
        meta = [m for m in meta if m['trash_id'] != tid]
        removed.append(item['name'])

    _save_trash_meta(meta)
    if removed:
        elog('files', 'info', f'Permanently deleted from trash: {len(removed)} items')
    return jsonify({'removed': removed})


@files_bp.route('/api/files/trash/preview')
@require_auth
def files_trash_preview():
    """Serve preview / thumbnail for a file in trash."""
    trash_id = request.args.get('id', '')
    # Optional sub-path inside trashed directory
    sub = request.args.get('sub', '')
    w = request.args.get('w', type=int)
    h = request.args.get('h', type=int)

    if not trash_id:
        return jsonify({'error': 'ID is required'}), 400

    # Look up the item in meta to find its trash_dir
    meta = _load_trash_meta()
    item = next((m for m in meta if m['trash_id'] == trash_id), None)
    item_trash_dir = item.get('trash_dir', TRASH_DIR) if item else TRASH_DIR

    trash_path = os.path.join(item_trash_dir, trash_id)
    if sub:
        trash_path = os.path.join(trash_path, sub)
    # Security: ensure stays inside the item's trash dir
    trash_path = os.path.realpath(trash_path)
    if not trash_path.startswith(os.path.realpath(item_trash_dir)):
        return jsonify({'error': 'Invalid path'}), 403

    if not os.path.isfile(trash_path):
        return jsonify({'error': 'Not found'}), 404

    if w and h:
        try:
            img = Image.open(trash_path)
            img.thumbnail((w * 2, h * 2), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='WEBP', quality=75)
            buf.seek(0)
            return send_file(buf, mimetype='image/webp', max_age=3600)
        except Exception:
            return send_file(trash_path)
    return send_file(trash_path)


# ─────────────────── Duplicate Photo Finder ───────────────────────

_dup_scan = {
    'running': False,
    'cancel': False,
    'scan_id': None,
    'phase': '',
    'scanned': 0,
    'total': 0,
    'found_groups': 0,
    'results': [],         # list of groups — populated INCREMENTALLY
    'error': None,
}

# ── Hash cache — avoids re-computing SHA256 / dhash on unchanged files ──
DUP_HASH_CACHE_FILE = _data_path('dup_hash_cache.json')

def _load_hash_cache():
    return _load_json(DUP_HASH_CACHE_FILE, {})

def _save_hash_cache(cache):
    try:
        _save_json(DUP_HASH_CACHE_FILE, cache)
    except Exception:
        pass

_MAX_HASH_CACHE_ENTRIES = 50000

def _prune_hash_cache(cache):
    """Remove oldest entries if cache exceeds max size."""
    if len(cache) <= _MAX_HASH_CACHE_ENTRIES:
        return cache
    # Keep newest entries by mtime
    sorted_keys = sorted(cache.keys(), key=lambda k: cache[k].get('mtime', 0), reverse=True)
    pruned = {k: cache[k] for k in sorted_keys[:_MAX_HASH_CACHE_ENTRIES]}
    return pruned

# ── Ignored duplicate groups ──
DUP_IGNORED_FILE = _data_path('dup_ignored.json')


def _dup_group_key(group):
    """Stable key for a dup group based on sorted file paths."""
    paths = sorted(f['path'] for f in group.get('items', []))
    return hashlib.md5('|'.join(paths).encode()).hexdigest()


def _load_dup_ignored():
    return _load_json(DUP_IGNORED_FILE, {})

def _save_dup_ignored(data):
    _save_json(DUP_IGNORED_FILE, data)

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.heic', '.heif'}


def _dhash(img, hash_size=10):
    """Compute difference hash (dhash) — perceptual hash for visual similarity."""
    img = img.convert('L').resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    w = hash_size + 1
    bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            bits.append(1 if pixels[row * w + col] < pixels[row * w + col + 1] else 0)
    return int(''.join(str(b) for b in bits), 2)


def _hamming(h1, h2):
    """Hamming distance between two integer hashes."""
    return bin(h1 ^ h2).count('1')


def _emit_new_group(group):
    """Send a newly found group to the frontend in real-time."""
    socketio.emit('dup_new_group', {
        'group': group,
        'total_groups': _dup_scan['found_groups'],
    })


def _scan_duplicates(scan_paths, mode, threshold):
    """Background task: find duplicate images — emits groups incrementally."""
    global _dup_scan
    try:
        _dup_scan['phase'] = 'Searching for images…'
        _dup_scan['scanned'] = 0
        socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': 0})

        # Load hash cache for faster re-scans
        hash_cache = _load_hash_cache()
        cache_hits = 0

        # Collect all image files from all scan paths
        image_files = []
        seen_real = set()
        _file_count = 0
        for scan_path in scan_paths:
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            for dirpath, _dirs, filenames in os.walk(scan_path):
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                # Skip hidden dirs and trash
                if '/.trash' in dirpath or '/.' in dirpath.split(scan_path)[-1]:
                    continue
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in IMAGE_EXTS:
                        fp = os.path.join(dirpath, fn)
                        real = os.path.realpath(fp)
                        if real in seen_real:
                            continue
                        seen_real.add(real)
                        try:
                            st = os.stat(fp)
                            image_files.append({
                                'path': fp.replace(DATA_ROOT, '', 1),
                                'real_path': fp,
                                'size': st.st_size,
                                'modified': st.st_mtime,
                                'name': fn,
                            })
                        except OSError:
                            pass
                    _file_count += 1
                    if _file_count % 5 == 0:
                        gevent.sleep(0)
                gevent.sleep(0)  # yield per directory

        total = len(image_files)
        _dup_scan['total'] = total
        _dup_scan['phase'] = f'Found {total} images, analyzing…'
        socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

        if total == 0:
            _dup_scan['running'] = False
            _dup_scan['phase'] = 'Completed'
            socketio.emit('dup_complete', {'groups': 0, 'duplicates': 0, 'size': 0})
            return

        exact_path_sets = set()  # track exact groups to avoid duplication in similar phase

        if mode in ('exact', 'both'):
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            # ── Phase: exact duplicates by SHA256 ──
            _dup_scan['phase'] = 'Comparing checksums (SHA256)…'
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

            # Pre-filter: group by size
            by_size = {}
            for f in image_files:
                by_size.setdefault(f['size'], []).append(f)
            size_groups = {k: v for k, v in by_size.items() if len(v) > 1}

            # Hash only size-matched files
            hash_map = {}
            scanned = 0
            to_hash = [f for grp in size_groups.values() for f in grp]
            for f in to_hash:
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                try:
                    cache_key = f['real_path']
                    cached = hash_cache.get(cache_key)
                    if cached and cached.get('mtime') == f['modified'] and cached.get('size') == f['size'] and cached.get('sha256'):
                        digest = cached['sha256']
                        cache_hits += 1
                    else:
                        h = hashlib.sha256()
                        with open(f['real_path'], 'rb') as fh:
                            _chunks = 0
                            while True:
                                chunk = fh.read(65536)
                                if not chunk:
                                    break
                                h.update(chunk)
                                _chunks += 1
                                if _chunks % 16 == 0:  # yield every ~1MB
                                    gevent.sleep(0)
                        digest = h.hexdigest()
                        # Update cache
                        if cache_key not in hash_cache:
                            hash_cache[cache_key] = {}
                        hash_cache[cache_key]['sha256'] = digest
                        hash_cache[cache_key]['mtime'] = f['modified']
                        hash_cache[cache_key]['size'] = f['size']
                    hash_map.setdefault(digest, []).append(f)
                except Exception:
                    pass
                scanned += 1
                gevent.sleep(0)  # yield after every file hash
                if scanned % 20 == 0:
                    _dup_scan['scanned'] = scanned
                    socketio.emit('dup_progress', {
                        'phase': f'SHA256: {scanned}/{len(to_hash)}',
                        'scanned': scanned, 'total': len(to_hash)
                    })

            # Emit exact duplicate groups incrementally
            for digest, files in hash_map.items():
                if len(files) > 1:
                    items = sorted(files, key=lambda x: x['modified'])
                    # Strip real_path before sending
                    items_clean = [{k: v for k, v in it.items() if k != 'real_path'} for it in items]
                    group = {
                        'hash': digest[:16],
                        'type': 'exact',
                        'items': items_clean,
                    }
                    _dup_scan['results'].append(group)
                    _dup_scan['found_groups'] = len(_dup_scan['results'])
                    _emit_new_group(group)
                    exact_path_sets.add(tuple(sorted(f['path'] for f in items)))
                    gevent.sleep(0)

        if mode in ('similar', 'both'):
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            # ── Phase: visually similar by dhash ──
            _dup_scan['phase'] = 'Analiza wizualna (perceptual hash)…'
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

            phash_list = []  # [(file_info, hash_int)]
            scanned = 0
            for f in image_files:
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                try:
                    cache_key = f['real_path']
                    cached = hash_cache.get(cache_key)
                    if cached and cached.get('mtime') == f['modified'] and cached.get('size') == f['size'] and cached.get('dhash') is not None:
                        h = cached['dhash']
                        cache_hits += 1
                    else:
                        img = Image.open(f['real_path'])
                        h = _dhash(img)
                        # Update cache
                        if cache_key not in hash_cache:
                            hash_cache[cache_key] = {}
                        hash_cache[cache_key]['dhash'] = h
                        hash_cache[cache_key]['mtime'] = f['modified']
                        hash_cache[cache_key]['size'] = f['size']
                    phash_list.append((f, h))
                except Exception:
                    pass
                scanned += 1
                gevent.sleep(0)  # yield after every image
                if scanned % 20 == 0:
                    _dup_scan['scanned'] = scanned
                    socketio.emit('dup_progress', {
                        'phase': f'Perceptual hash: {scanned}/{total}',
                        'scanned': scanned, 'total': total
                    })

            # ── Phase: clustering ──
            # Use bucket approach to avoid full O(n²): bucket by coarse hash chunks
            _dup_scan['phase'] = 'Grouping similar images…'
            n = len(phash_list)
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': n})

            parent = list(range(n))

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

            # Bucket by portions of the hash to reduce comparisons
            HASH_BITS = 100  # 10x10 dhash = 100 bits
            BUCKET_BITS = 25  # split into 4 bands
            num_bands = HASH_BITS // BUCKET_BITS

            buckets = [{} for _ in range(num_bands)]
            for i, (f, h) in enumerate(phash_list):
                for band_idx in range(num_bands):
                    band_val = (h >> (band_idx * BUCKET_BITS)) & ((1 << BUCKET_BITS) - 1)
                    buckets[band_idx].setdefault(band_val, []).append(i)

            compared = set()
            comparisons_done = 0
            for band_idx in range(num_bands):
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                for candidates in buckets[band_idx].values():
                    if len(candidates) < 2:
                        continue
                    for ci in range(len(candidates)):
                        for cj in range(ci + 1, len(candidates)):
                            i, j = candidates[ci], candidates[cj]
                            pair = (min(i, j), max(i, j))
                            if pair in compared:
                                continue
                            compared.add(pair)
                            if _hamming(phash_list[i][1], phash_list[j][1]) <= threshold:
                                union(i, j)
                            comparisons_done += 1
                            if comparisons_done % 50 == 0:
                                gevent.sleep(0)  # yield frequently during comparisons
                            if comparisons_done % 500 == 0:
                                socketio.emit('dup_progress', {
                                    'phase': f'Comparing: {comparisons_done} pairs',
                                    'scanned': comparisons_done, 'total': comparisons_done
                                })

            # For high thresholds, also do a limited brute-force on remaining
            if threshold >= 10 and n <= 5000:
                _dup_scan['phase'] = 'Additional comparisons…'
                socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': n})
                _bf_ops = 0
                for i in range(n):
                    if _dup_scan.get('cancel'):
                        raise InterruptedError('Cancelled')
                    for j in range(i + 1, n):
                        pair = (i, j)
                        if pair in compared:
                            continue
                        if _hamming(phash_list[i][1], phash_list[j][1]) <= threshold:
                            union(i, j)
                        _bf_ops += 1
                        if _bf_ops % 50 == 0:
                            gevent.sleep(0)
                    if i % 50 == 0:
                        socketio.emit('dup_progress', {
                            'phase': f'Brute-force: {i}/{n}',
                            'scanned': i, 'total': n
                        })
                        gevent.sleep(0)

            # Collect clusters & emit incrementally
            clusters = {}
            for i in range(n):
                root = find(i)
                clusters.setdefault(root, []).append(i)

            for idxs in clusters.values():
                if len(idxs) < 2:
                    continue
                items = [phash_list[i][0] for i in idxs]
                paths = tuple(sorted(f['path'] for f in items))
                if paths in exact_path_sets:
                    continue
                items_sorted = sorted(items, key=lambda x: x['modified'])
                items_clean = [{k: v for k, v in it.items() if k != 'real_path'} for it in items_sorted]
                group = {
                    'hash': f'phash_{idxs[0]}',
                    'type': 'similar',
                    'items': items_clean,
                }
                _dup_scan['results'].append(group)
                _dup_scan['found_groups'] = len(_dup_scan['results'])
                _emit_new_group(group)
                gevent.sleep(0)

        # Sort results by total size (descending)
        _dup_scan['results'].sort(key=lambda g: sum(f['size'] for f in g['items']), reverse=True)

        _dup_scan['running'] = False
        _dup_scan['phase'] = 'Completed'
        groups = _dup_scan['results']
        total_dups = sum(len(g['items']) - 1 for g in groups)
        dup_size = sum(sum(f['size'] for f in g['items'][1:]) for g in groups)
        socketio.emit('dup_complete', {
            'groups': len(groups),
            'duplicates': total_dups,
            'size': dup_size,
        })
        elog('files', 'info',
             f'Duplicate scan completed: {len(groups)} groups, '
             f'{total_dups} duplicates ({dup_size} bytes), '
             f'{cache_hits} cache hits')
        # Save hash cache for future scans
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)

    except InterruptedError:
        _dup_scan['running'] = False
        _dup_scan['cancel'] = False
        _dup_scan['phase'] = 'Cancelled'
        socketio.emit('dup_cancelled', {
            'groups': _dup_scan['found_groups']
        })
        elog('files', 'info', f'Duplicate scan cancelled ({_dup_scan["found_groups"]} groups found)')
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)
    except Exception as e:
        _dup_scan['running'] = False
        _dup_scan['error'] = str(e)
        _dup_scan['phase'] = 'Error'
        socketio.emit('dup_error', {'error': str(e)})
        elog('files', 'error', f'Duplicate scan error: {str(e)}')
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)


def get_dupscan_notifications():
    """Return notification items for the duplicate scan."""
    notifs = []
    if _dup_scan['running']:
        phase = _dup_scan.get('phase', 'Scanning…')
        found = _dup_scan.get('found_groups', 0)
        scanned = _dup_scan.get('scanned', 0)
        total = _dup_scan.get('total', 0)
        pct = (round(scanned / total * 100) if total > 0 else 0)
        notifs.append({
            'type': 'progress',
            'title': 'Finding duplicates',
            'message': f'{phase} — {found} grup, {pct}%',
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    elif _dup_scan.get('phase') == 'Completed' and _dup_scan.get('results'):
        groups = _dup_scan['results']
        total_dups = sum(len(g['items']) - 1 for g in groups)
        notifs.append({
            'type': 'success',
            'title': 'Duplicates found',
            'message': f'{len(groups)} groups, {total_dups} redundant files',
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    elif _dup_scan.get('phase') == 'Cancelled' and _dup_scan.get('results'):
        found = len(_dup_scan['results'])
        if found > 0:
            notifs.append({
                'type': 'warning',
                'title': 'Scan cancelled',
                'message': f'{found} groups found before cancellation',
                'time': time.time(),
                'action': {'app': 'duplicates'}
            })
    elif _dup_scan.get('error'):
        notifs.append({
            'type': 'error',
            'title': 'Duplicate scan error',
            'message': str(_dup_scan['error']),
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    return notifs


@files_bp.route('/api/files/duplicates/scan', methods=['POST'])
@require_auth
def files_duplicates_scan():
    """Start a duplicate photo scan."""
    if _dup_scan['running']:
        return jsonify({'error': 'Scan already in progress'}), 409

    data = request.json or {}
    # Support both single path and multiple paths
    paths_rel = data.get('paths', [])
    if not paths_rel and data.get('path'):
        paths_rel = [data['path']]
    if not paths_rel:
        paths_rel = ['/home']
    mode = data.get('mode', 'both')        # exact | similar | both
    threshold = data.get('threshold', 8)     # hamming distance for similar

    scan_paths = []
    for p in paths_rel:
        rp = safe_path(p)
        if rp and os.path.isdir(rp):
            scan_paths.append(rp)
    if not scan_paths:
        return jsonify({'error': 'No valid paths'}), 400

    _dup_scan['running'] = True
    _dup_scan['cancel'] = False
    _dup_scan['scan_id'] = secrets.token_hex(4)
    _dup_scan['phase'] = 'Starting…'
    _dup_scan['scanned'] = 0
    _dup_scan['total'] = 0
    _dup_scan['found_groups'] = 0
    _dup_scan['results'] = []
    _dup_scan['error'] = None

    socketio.start_background_task(_scan_duplicates, scan_paths, mode, threshold)
    return jsonify({'scan_id': _dup_scan['scan_id'], 'status': 'started'})


@files_bp.route('/api/files/duplicates/cancel', methods=['POST'])
@require_auth
def files_duplicates_cancel():
    """Cancel a running scan."""
    if not _dup_scan['running']:
        return jsonify({'error': 'No active scan'}), 400
    _dup_scan['cancel'] = True
    return jsonify({'ok': True, 'message': 'Cancelling scan…'})


@files_bp.route('/api/files/duplicates/status')
@require_auth
def files_duplicates_status():
    """Get current scan progress."""
    return jsonify({
        'running': _dup_scan['running'],
        'phase': _dup_scan['phase'],
        'scanned': _dup_scan['scanned'],
        'total': _dup_scan['total'],
        'found_groups': _dup_scan['found_groups'],
        'error': _dup_scan['error'],
    })


@files_bp.route('/api/files/duplicates/results')
@require_auth
def files_duplicates_results():
    """Get scan results — groups of duplicate images (works during scan too).
    Filters out ignored groups unless ?include_ignored=1."""
    results = _dup_scan.get('results', [])
    include_ignored = request.args.get('include_ignored', '0') == '1'
    if not include_ignored:
        ignored = _load_dup_ignored()
        results = [g for g in results if _dup_group_key(g) not in ignored]
    return jsonify({
        'groups': results,
        'ready': not _dup_scan['running'],
        'total_groups': len(results),
    })


@files_bp.route('/api/files/duplicates/ignore', methods=['POST'])
@require_auth
def files_duplicates_ignore():
    """Add groups to the ignore list."""
    data = request.json or {}
    groups = data.get('groups', [])  # list of group objects with items
    if not groups:
        return jsonify({'error': 'No groups provided'}), 400
    ignored = _load_dup_ignored()
    added = 0
    for g in groups:
        key = _dup_group_key(g)
        if key not in ignored:
            ignored[key] = {
                'key': key,
                'paths': sorted(f['path'] for f in g.get('items', [])),
                'type': g.get('type', 'unknown'),
                'ignored_at': time.time(),
            }
            added += 1
    _save_dup_ignored(ignored)
    return jsonify({'ok': True, 'added': added, 'total_ignored': len(ignored)})


@files_bp.route('/api/files/duplicates/unignore', methods=['POST'])
@require_auth
def files_duplicates_unignore():
    """Remove groups from the ignore list."""
    data = request.json or {}
    keys = data.get('keys', [])  # list of group keys
    if not keys:
        return jsonify({'error': 'No keys provided'}), 400
    ignored = _load_dup_ignored()
    removed = 0
    for k in keys:
        if k in ignored:
            del ignored[k]
            removed += 1
    _save_dup_ignored(ignored)
    return jsonify({'ok': True, 'removed': removed, 'total_ignored': len(ignored)})


@files_bp.route('/api/files/duplicates/ignored')
@require_auth
def files_duplicates_ignored():
    """List all ignored dup groups — returns stored info + matching scan results if available."""
    ignored = _load_dup_ignored()
    # Try to enrich with actual group data from last scan results
    results = _dup_scan.get('results', [])
    result_by_key = {_dup_group_key(g): g for g in results}
    items = []
    for key, info in ignored.items():
        entry = {**info}
        if key in result_by_key:
            entry['group'] = result_by_key[key]
        items.append(entry)
    items.sort(key=lambda x: x.get('ignored_at', 0), reverse=True)
    return jsonify({'items': items, 'total': len(items)})


# -- duplicates package routes (no blueprint, lives in app.py) --
@files_bp.route('/api/files/duplicates/install', methods=['POST'])
@require_auth
def duplicates_pkg_install():
    return jsonify({'ok': True})

@files_bp.route('/api/files/duplicates/uninstall', methods=['POST'])
@require_auth
def duplicates_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    if wipe:
        _dup_scan['results'] = []
        _dup_scan['progress'] = 0
        _hash_cache_path = _data_path('dup_hash_cache.json')
        if os.path.isfile(_hash_cache_path):
            os.remove(_hash_cache_path)
        _ignored_path = _data_path('dup_ignored.json')
        if os.path.isfile(_ignored_path):
            os.remove(_ignored_path)
    return jsonify({'ok': True})

@files_bp.route('/api/files/duplicates/pkg-status')
@require_auth
def duplicates_pkg_status():
    return jsonify({'status': 'ready'})


# -- code-editor package routes (frontend-only, noop backend) --
@files_bp.route('/api/code-editor/install', methods=['POST'])
@require_auth
@admin_required
def code_editor_pkg_install():
    return jsonify({'ok': True})

@files_bp.route('/api/code-editor/uninstall', methods=['POST'])
@require_auth
@admin_required
def code_editor_pkg_uninstall():
    return jsonify({'ok': True})

@files_bp.route('/api/code-editor/pkg-status')
@require_auth
def code_editor_pkg_status():
    return jsonify({'status': 'ready'})


@files_bp.route('/api/files/rename', methods=['POST'])
@require_auth
def files_rename():
    data = request.json or {}
    old = safe_path(data.get('path', ''))
    new_name = data.get('new_name', '')
    if not old or not new_name or '/' in new_name:
        return jsonify({'error': 'Invalid parameters'}), 400

    blocked = _require_folder_access(data.get('path', ''))
    if blocked is not None:
        return blocked

    new_path = os.path.join(os.path.dirname(old), new_name)
    try:
        # Compute user-visible paths for folder password migration
        old_user_path = data.get('path', '').rstrip('/')
        parent = os.path.dirname(old_user_path)
        new_user_path = (parent + '/' + new_name) if parent != '/' else ('/' + new_name)

        os.rename(old, new_path)

        # Migrate folder passwords
        _migrate_folder_passwords(old_user_path, new_user_path)
        _dirsize_cache_invalidate(os.path.dirname(old))
        _listdir_cache_invalidate(data.get('path', ''))

        elog('files', 'info', f'Renamed: {os.path.basename(old)} → {new_name}')
        return jsonify({'ok': True})
    except Exception as e:
        elog('files', 'error', f'Rename error: {str(e)}')
        return jsonify({'error': str(e)}), 500


def _atomic_move(src, target, username=None):
    """Move *src* to *target* atomically.

    For same-filesystem moves, uses os.rename which is atomic.
    For cross-filesystem moves, copies to a temp file first, then atomically
    replaces the target, and finally removes the source.  This ensures that
    a crash between any step leaves data intact (source still exists or target
    is already written).

    *username* is used to chown the destination on cross-device moves.
    It must be captured from the request context before entering the thread pool.
    """
    try:
        os.rename(src, target)
    except OSError as e:
        import errno as _errno
        if e.errno != _errno.EXDEV:
            raise
        # Cross-device move: copy → atomic replace → remove source
        if os.path.isdir(src):
            tmp_target = target + '.ethos_mv_tmp_dir'
            try:
                shutil.copytree(src, tmp_target)
                _chown_recursive(tmp_target, username)
                os.rename(tmp_target, target)
                shutil.rmtree(src)
            except Exception:
                shutil.rmtree(tmp_target, ignore_errors=True)
                raise
        else:
            tmp_target = target + '.ethos_mv_tmp'
            try:
                shutil.copy2(src, tmp_target)
                _chown_to_user(tmp_target, username)
                os.replace(tmp_target, target)
                os.remove(src)
            except Exception:
                try:
                    os.remove(tmp_target)
                except OSError:
                    pass
                raise


@files_bp.route('/api/files/move', methods=['POST'])
@require_auth
def files_move():
    data = request.json or {}
    src = safe_path(data.get('src', ''))
    dest = safe_path(data.get('dest', ''))
    if not src or not dest:
        return jsonify({'error': 'Invalid parameters'}), 400

    blocked = _require_folder_access(data.get('src', ''))
    if blocked is not None:
        return blocked
    blocked = _require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked

    try:
        old_user_path = data.get('src', '').rstrip('/')
        dest_user = data.get('dest', '').rstrip('/')
        new_user_path = dest_user + '/' + os.path.basename(old_user_path)

        target = os.path.join(dest, os.path.basename(src))
        # Capture username in request context before entering thread pool
        _cur_user = get_current_user()
        _username = _cur_user['username'] if _cur_user else None
        # Run in thread pool to avoid blocking gevent on cross-device moves
        _fs_call(_atomic_move, src, target, _username, timeout=300)

        # Migrate folder passwords for moved folder
        _migrate_folder_passwords(old_user_path, new_user_path)
        _dirsize_cache_invalidate(os.path.dirname(src))
        _dirsize_cache_invalidate(dest)
        _listdir_cache_invalidate(data.get('src', ''))
        _listdir_cache_invalidate(data.get('dest', ''))

        elog('files', 'info', f'Moved: {os.path.basename(src)} → {data.get("dest", "")}')
        return jsonify({'ok': True})
    except Exception as e:
        elog('files', 'error', f'Move error: {str(e)}')
        return jsonify({'error': str(e)}), 500


@files_bp.route('/api/files/check-conflicts', methods=['POST'])
@require_auth
def files_check_conflicts():
    """Check which sources already exist in dest."""
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = safe_path(data.get('dest', ''))
    if not sources or not dest_dir:
        return jsonify({'conflicts': []})
    conflicts = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            continue
        base_name = os.path.basename(real_src)
        target = os.path.join(dest_dir, base_name)
        if os.path.exists(target):
            conflicts.append({
                'name': base_name,
                'source': src_path,
                'is_dir': os.path.isdir(real_src),
                'target_is_dir': os.path.isdir(target),
            })
    return jsonify({'conflicts': conflicts})


def _resolve_target(real_src, dest_dir, on_conflict):
    """Resolve target path based on conflict strategy. Returns target path or None to skip."""
    base_name = os.path.basename(real_src)
    target = os.path.join(dest_dir, base_name)
    if os.path.exists(target):
        if on_conflict == 'overwrite':
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            return target
        elif on_conflict == 'skip':
            return None
        else:  # rename (default)
            name, ext = os.path.splitext(base_name)
            counter = 1
            while os.path.exists(target):
                target = os.path.join(dest_dir, f"{name}_kopia{counter}{ext}")
                counter += 1
            return target
    return target


@files_bp.route('/api/files/copy', methods=['POST'])
@require_auth
def files_copy():
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = safe_path(data.get('dest', ''))
    on_conflict = data.get('on_conflict', 'rename')  # overwrite | skip | rename
    if not sources or not dest_dir:
        return jsonify({'error': 'Invalid parameters'}), 400
    if not os.path.isdir(dest_dir):
        return jsonify({'error': 'Destination is not a folder'}), 400

    # Security check: check destination and all sources
    blocked = _require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked
    for s in sources:
        blocked = _require_folder_access(s)
        if blocked is not None:
            return blocked

    # Resolve sources first
    resolved = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            continue
        resolved.append(real_src)

    total = _count_items(resolved)
    use_bg = total > 2  # background task for >2 items

    if use_bg:
        _fm = _fileop_channels['fm']
        with _fileop_lock:
            if _fm['active']:
                return jsonify({'error': 'Another file operation is in progress'}), 400
            _fm['active'] = True
            _fm['operation'] = 'copy'
            _fm['progress'] = None
            _fm['cancel'] = False
            _fm['paused'] = False
        cur_user = get_current_user()
        _save_copy_task(resolved, dest_dir, total, on_conflict, (cur_user or {}).get('username'))
        socketio.start_background_task(_bg_copy, resolved, dest_dir, total, on_conflict, cur_user)
        return jsonify({'async': True, 'message': f'Copying {len(resolved)} items ({total} files) in background'})

    # Small operation — synchronous but run I/O in thread pool
    # to avoid blocking gevent on slow disks (USB, cross-device)
    copied = []
    skipped = []
    errors = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            errors.append(f'Not found: {src_path}')
            continue
        base_name = os.path.basename(real_src)
        target = _resolve_target(real_src, dest_dir, on_conflict)
        if target is None:
            skipped.append(base_name)
            continue
        try:
            if os.path.isdir(real_src):
                tmp_target = target + '.ethos_tmp_dir'
                def _do_copy_dir(_src=real_src, _tmp=tmp_target, _tgt=target):
                    try:
                        shutil.copytree(_src, _tmp)
                        os.rename(_tmp, _tgt)
                    except Exception:
                        shutil.rmtree(_tmp, ignore_errors=True)
                        raise
                _fs_call(_do_copy_dir, timeout=300)
                _chown_recursive(target)
            else:
                tmp_target = target + '.ethos_tmp'
                def _do_copy_file(_src=real_src, _tmp=tmp_target, _tgt=target):
                    shutil.copy2(_src, _tmp)
                    os.replace(_tmp, _tgt)
                _fs_call(_do_copy_file, timeout=300)
                _chown_to_user(target)
            copied.append(base_name)
        except Exception as e:
            errors.append(f'{base_name}: {str(e)}')

    _listdir_cache_invalidate(data.get('dest', ''))
    _dirsize_cache_invalidate(dest_dir)

    if copied:
        cur = get_current_user()
        username = cur['username'] if cur else 'unknown'
        elog('files', 'info', f'Copied {len(copied)} files to {data.get("dest", "")}', {'user': username, 'files': copied})

    return jsonify({'copied': copied, 'skipped': skipped, 'errors': errors})


def _bg_copy(resolved_sources, dest_dir, total, on_conflict='rename', cur_user=None):
    """Background copy with progress, cancel/pause support, and cleanup on abort."""
    _bg_username = cur_user['username'] if cur_user else None
    copied = []
    skipped = []
    errors = []
    done_ref = [0]
    partial_files = []  # tracks .ethos_tmp files written so far (for cleanup on cancel)
    cancel_ref = [False]
    _fm = _fileop_channels['fm']

    def _check_cancel():
        with _fileop_lock:
            cancel_ref[0] = _fm.get('cancel', False)
        return cancel_ref[0]

    def _is_paused():
        with _fileop_lock:
            return _fm.get('paused', False)

    _fileop_progress('copy', '', 0, total, 'fm')
    cancelled = False
    try:
        for real_src in resolved_sources:
            if _check_cancel():
                cancelled = True
                break
            if not real_src or not os.path.exists(real_src):
                errors.append(f'Not found: {real_src}')
                continue
            base_name = os.path.basename(real_src)
            target = _resolve_target(real_src, dest_dir, on_conflict)
            if target is None:
                skipped.append(base_name)
                done_ref[0] += _count_items([real_src])
                _fileop_progress('copy', f'{base_name} (skipped)', done_ref[0], total, 'fm')
                gevent.sleep(0)
                continue
            try:
                _copy_with_progress(real_src, target, 'copy', done_ref, total, username=_bg_username,
                                    cancel_ref=cancel_ref, pause_fn=_is_paused, partial_files=partial_files)
                copied.append(base_name)
            except InterruptedError:
                cancelled = True
                break
            except Exception as e:
                errors.append(f'{base_name}: {str(e)}')

        if cancelled:
            # Clean up partial .ethos_tmp files left behind
            for pf in partial_files:
                try: os.remove(pf)
                except OSError: pass
            _clear_copy_task()
            _fileop_finish('copy', False, 'Cancelled', 'fm')
            return

        _clear_copy_task()
        msg = f'Copied {len(copied)} items'
        if skipped:
            msg += f', skipped {len(skipped)}'
        if errors:
            msg += f' ({len(errors)} errors)'
        _fileop_finish('copy', len(copied) > 0 or len(skipped) > 0, msg, 'fm')
    except Exception as e:
        # Clean up partial files on unexpected error
        for pf in partial_files:
            try: os.remove(pf)
            except OSError: pass
        _clear_copy_task()
        _fileop_finish('copy', False, str(e), 'fm')


@files_bp.route('/api/files/move-multi', methods=['POST'])
@require_auth
def files_move_multi():
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = safe_path(data.get('dest', ''))
    if not sources or not dest_dir:
        return jsonify({'error': 'Invalid parameters'}), 400
    if not os.path.isdir(dest_dir):
        return jsonify({'error': 'Destination is not a folder'}), 400

    # Security check: check destination and all sources
    blocked = _require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked
    for s in sources:
        blocked = _require_folder_access(s)
        if blocked is not None:
            return blocked

    # Resolve sources
    resolved = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if real_src and os.path.exists(real_src):
            resolved.append(real_src)

    total = _count_items(resolved)
    use_bg = total > 2  # background task for >2 items

    on_conflict = data.get('on_conflict', 'rename')  # overwrite | skip | rename

    if use_bg:
        _fm = _fileop_channels['fm']
        with _fileop_lock:
            if _fm['active']:
                return jsonify({'error': 'Another file operation is in progress'}), 400
            _fm['active'] = True
            _fm['operation'] = 'move'
            _fm['progress'] = None
            _fm['cancel'] = False
            _fm['paused'] = False
        cur_user = get_current_user()
        _save_move_task(resolved, dest_dir, total, on_conflict, data.get('dest', ''), (cur_user or {}).get('username'))
        socketio.start_background_task(_bg_move, resolved, dest_dir, total, on_conflict, data.get('dest', ''), cur_user)
        return jsonify({'async': True, 'message': f'Moving {len(resolved)} items in background'})

    # Small — synchronous
    moved = []
    skipped = []
    errors = []
    _cur_user = get_current_user()
    _username = (_cur_user or {}).get('username')
    for src_path in sources:
        real_src = safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            errors.append(f'Not found: {src_path}')
            continue
        base_name = os.path.basename(real_src)
        target = _resolve_target(real_src, dest_dir, on_conflict)
        if target is None:
            skipped.append(base_name)
            continue
        try:
            # Run in thread pool to avoid blocking gevent on cross-device moves
            _fs_call(_atomic_move, real_src, target, _username, timeout=300)
            _chown_recursive(target, _username)
            moved.append(base_name)
            # Migrate folder passwords
            dest_user = data.get('dest', '').rstrip('/')
            _migrate_folder_passwords(src_path.rstrip('/'), dest_user + '/' + base_name)
        except Exception as e:
            errors.append(f'{base_name}: {str(e)}')

    _listdir_cache_invalidate(data.get('dest', ''))
    _dirsize_cache_invalidate(dest_dir)
    for src_path in sources:
        _listdir_cache_invalidate(src_path)
        _dirsize_cache_invalidate(os.path.dirname(safe_path(src_path) or ''))

    if moved:
        cur = get_current_user()
        username = cur['username'] if cur else 'unknown'
        elog('files', 'info', f'Moved {len(moved)} files to {data.get("dest", "")}', {'user': username, 'files': moved})

    return jsonify({'moved': moved, 'skipped': skipped, 'errors': errors})


def _bg_move(resolved_sources, dest_dir, total, on_conflict='rename', dest_user_path='', cur_user=None):
    """Background move with progress, cancel/pause support."""
    _bg_username = cur_user['username'] if cur_user else None
    moved = []
    skipped = []
    errors = []
    done = 0
    dest_user = dest_user_path.rstrip('/')
    cancel_ref = [False]
    _fm = _fileop_channels['fm']

    def _check_cancel():
        with _fileop_lock:
            cancel_ref[0] = _fm.get('cancel', False)
        return cancel_ref[0]

    def _is_paused():
        with _fileop_lock:
            return _fm.get('paused', False)

    def _wait_if_paused():
        while _is_paused():
            if _check_cancel():
                return True
            gevent.sleep(0.2)
        return _check_cancel()

    _fileop_progress('move', '', 0, total, 'fm')
    cancelled = False
    try:
        for real_src in resolved_sources:
            if _check_cancel():
                cancelled = True
                break
            # Honour pause between items
            if _wait_if_paused():
                cancelled = True
                break
            if not real_src or not os.path.exists(real_src):
                errors.append(f'Not found: {real_src}')
                continue
            base_name = os.path.basename(real_src)
            item_count = _count_items([real_src])
            target = _resolve_target(real_src, dest_dir, on_conflict)
            if target is None:
                skipped.append(base_name)
                done += item_count
                _fileop_progress('move', f'{base_name} (skipped)', done, total, 'fm')
                gevent.sleep(0)
                continue
            try:
                # Run in thread pool to avoid blocking gevent on cross-device moves
                _fs_call(_atomic_move, real_src, target, _bg_username, timeout=600)
                _chown_recursive(target, _bg_username)
                done += item_count
                moved.append(base_name)
                # Migrate folder passwords
                if dest_user:
                    _migrate_folder_passwords(real_src.rstrip('/'), dest_user + '/' + base_name)
                _fileop_progress('move', base_name, done, total, 'fm')
                gevent.sleep(0)
            except Exception as e:
                errors.append(f'{base_name}: {str(e)}')

        if cancelled:
            _clear_move_task()
            _fileop_finish('move', False, 'Cancelled', 'fm')
            return

        _clear_move_task()
        msg = f'Moved {len(moved)} items'
        if skipped:
            msg += f', skipped {len(skipped)}'
        if errors:
            msg += f' ({len(errors)} errors)'
        _fileop_finish('move', len(moved) > 0 or len(skipped) > 0, msg, 'fm')
    except Exception as e:
        _clear_move_task()
        _fileop_finish('move', False, str(e), 'fm')


# ── Archive / Extract ──

@files_bp.route('/api/files/compress', methods=['POST'])
@require_auth
def files_compress():
    """Create a zip or tar.gz from selected files/folders."""
    data = request.json or {}
    sources = data.get('sources', [])
    fmt = data.get('format', 'zip')  # 'zip' or 'tar.gz'
    archive_name = data.get('name', '')
    if not sources:
        return jsonify({'error': 'No files to compress'}), 400

    # Determine output dir = same dir as first source
    first_src = safe_path(sources[0])
    if not first_src:
        return jsonify({'error': 'Invalid path'}), 400
    out_dir = os.path.dirname(first_src)

    if not archive_name:
        if len(sources) == 1:
            archive_name = os.path.splitext(os.path.basename(first_src))[0]
        else:
            archive_name = 'archive'

    ext = '.zip' if fmt == 'zip' else '.tar.gz'
    archive_path = os.path.join(out_dir, archive_name + ext)
    # Avoid overwrite
    counter = 1
    while os.path.exists(archive_path):
        archive_path = os.path.join(out_dir, f"{archive_name}_{counter}{ext}")
        counter += 1

    resolved = []
    for s in sources:
        rs = safe_path(s)
        if rs and os.path.exists(rs):
            resolved.append(rs)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    total = _count_items(resolved)

    with _fileop_lock:
        if _fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'compress'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False

    cur_user = get_current_user()
    _bg_username = cur_user['username'] if cur_user else None
    _save_compress_task(resolved, archive_path, fmt, total, _bg_username)
    socketio.start_background_task(_bg_compress, resolved, archive_path, fmt, total, cur_user)
    return jsonify({'async': True, 'message': f'Compressing {len(resolved)} items to {os.path.basename(archive_path)}'})


def _bg_compress(resolved, archive_path, fmt, total, cur_user=None):
    """Background archive creation with progress. Writes to a temp file first,
    then atomically renames to the final path on success (prevents corrupt archives)."""
    _bg_username = cur_user['username'] if cur_user else None
    done = 0
    cancelled = False
    tmp_archive_path = archive_path + '.ethos_archive_tmp'

    def _check_cancel_pause():
        if _fileop_cancelled():
            return True
        while _fileop_is_paused():
            gevent.sleep(0.5)
            if _fileop_cancelled():
                return True
        return False

    try:
        if fmt == 'zip':
            with zipfile.ZipFile(tmp_archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for src in resolved:
                    if _check_cancel_pause():
                        cancelled = True; break
                    base_dir = os.path.dirname(src)
                    if os.path.isdir(src):
                        for root, dirs, files in os.walk(src):
                            if _check_cancel_pause():
                                cancelled = True; break
                            for fn in files:
                                if _check_cancel_pause():
                                    cancelled = True; break
                                full = os.path.join(root, fn)
                                arcname = os.path.relpath(full, base_dir)
                                zf.write(full, arcname)
                                done += 1
                                _fileop_progress('compress', fn, done, total)
                                gevent.sleep(0)
                            if cancelled:
                                break
                            done += len(dirs)
                            gevent.sleep(0)
                    else:
                        zf.write(src, os.path.basename(src))
                        done += 1
                        _fileop_progress('compress', os.path.basename(src), done, total)
                        gevent.sleep(0)
        else:
            with tarfile.open(tmp_archive_path, 'w:gz') as tf:
                for src in resolved:
                    if _check_cancel_pause():
                        cancelled = True; break
                    base_dir = os.path.dirname(src)
                    if os.path.isdir(src):
                        for root, dirs, files in os.walk(src):
                            if _check_cancel_pause():
                                cancelled = True; break
                            for fn in files:
                                if _check_cancel_pause():
                                    cancelled = True; break
                                full = os.path.join(root, fn)
                                arcname = os.path.relpath(full, base_dir)
                                tf.add(full, arcname)
                                done += 1
                                _fileop_progress('compress', fn, done, total)
                                gevent.sleep(0)
                            if cancelled:
                                break
                            done += len(dirs)
                            gevent.sleep(0)
                    else:
                        tf.add(src, os.path.basename(src))
                        done += 1
                        _fileop_progress('compress', os.path.basename(src), done, total)
                        gevent.sleep(0)

        if cancelled:
            for p in (tmp_archive_path,):
                if os.path.exists(p):
                    try: os.remove(p)
                    except OSError: pass
            _clear_compress_task()
            _fileop_finish('compress', False, 'Cancelled')
            return

        # Atomically move temp archive to final path
        os.replace(tmp_archive_path, archive_path)
        _chown_to_user(archive_path, _bg_username)
        size_mb = round(os.path.getsize(archive_path) / (1024*1024), 1)
        _clear_compress_task()
        _fileop_finish('compress', True, f'{os.path.basename(archive_path)} ({size_mb} MB)')
    except Exception as e:
        for p in (tmp_archive_path, archive_path):
            if os.path.exists(p):
                try: os.remove(p)
                except OSError: pass
        _clear_compress_task()
        _fileop_finish('compress', False, str(e))


_EXTRACT_COMPOUND_EXTS = ('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')
_EXTRACT_SIMPLE_EXTS = ('.tar', '.zip')
_EXTRACT_7Z_EXTS = ('.gz', '.bz2', '.xz', '.rar', '.7z', '.cab', '.iso')
_EXTRACT_ALL_EXTS = _EXTRACT_COMPOUND_EXTS + _EXTRACT_SIMPLE_EXTS + _EXTRACT_7Z_EXTS


def _extract_folder_name(basename):
    """Derive output folder name from archive filename."""
    low = basename.lower()
    for ext in _EXTRACT_COMPOUND_EXTS:
        if low.endswith(ext):
            return basename[:len(basename) - len(ext)]
    for ext in _EXTRACT_SIMPLE_EXTS + _EXTRACT_7Z_EXTS:
        if low.endswith(ext):
            return basename[:len(basename) - len(ext)]
    return basename + '_extracted'


def _needs_7z(basename):
    """Return True if this archive format requires 7z CLI rather than Python stdlib."""
    low = basename.lower()
    return any(low.endswith(ext) for ext in _EXTRACT_7Z_EXTS)


@files_bp.route('/api/files/extract', methods=['POST'])
@require_auth
def files_extract():
    """Extract an archive (zip, tar.*, gz, rar, 7z, bz2, xz, cab, iso)."""
    data = request.json or {}
    archive = safe_path(data.get('path', ''))
    if not archive or not os.path.isfile(archive):
        return jsonify({'error': 'Archive file does not exist'}), 400

    basename = os.path.basename(archive)
    low = basename.lower()

    if not any(low.endswith(ext) for ext in _EXTRACT_ALL_EXTS):
        return jsonify({'error': 'Unsupported archive format'}), 400

    folder_name = _extract_folder_name(basename)
    extract_to = os.path.join(os.path.dirname(archive), folder_name)
    counter = 1
    base_extract = extract_to
    while os.path.exists(extract_to):
        extract_to = f"{base_extract}_{counter}"
        counter += 1

    # For 7z-only formats, verify or install 7z first (before going async)
    use_7z = _needs_7z(basename)
    if use_7z:
        from host import ensure_dep
        ok, msg = ensure_dep('7z', install=True)
        if not ok:
            return jsonify({'error': f'Cannot extract: {msg}'}), 400

    os.makedirs(extract_to, exist_ok=True)

    # Count members (only for Python-handled formats)
    total = 0
    if not use_7z:
        try:
            if low.endswith('.zip'):
                with zipfile.ZipFile(archive, 'r') as zf:
                    total = len(zf.namelist())
            elif low.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar')):
                with tarfile.open(archive, 'r:*') as tf:
                    total = len(tf.getnames())
        except Exception:
            total = 0

    with _fileop_lock:
        _fm = _fileop_channels['fm']
        if _fm['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fm['active'] = True
        _fm['operation'] = 'extract'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    socketio.start_background_task(_bg_extract, archive, extract_to, total, use_7z, get_current_user())
    return jsonify({'async': True, 'message': f'Extracting {basename} to {folder_name}/'})


def _bg_extract(archive, extract_to, total, use_7z, cur_user=None):
    """Background archive extraction with progress and cancel support."""
    _bg_username = cur_user['username'] if cur_user else None
    done = 0
    basename = os.path.basename(archive)
    _fm = _fileop_channels['fm']

    def _check_cancel():
        with _fileop_lock:
            return _fm.get('cancel', False)

    try:
        if use_7z:
            # Use 7z CLI for formats Python can't handle (.gz, .rar, .7z, .bz2, .xz, .cab, .iso)
            import subprocess as _sp
            cmd = ['7z', 'x', '-y', f'-o{extract_to}', archive]
            _fileop_progress('extract', basename, 0, 1)
            proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, bufsize=1)
            for line in iter(proc.stdout.readline, ''):
                if _check_cancel():
                    proc.kill()
                    try: shutil.rmtree(extract_to, ignore_errors=True)
                    except Exception: pass
                    _fileop_finish('extract', False, 'Cancelled')
                    return
                line = line.strip()
                if line.startswith('- ') or line.startswith('Extracting '):
                    fname = line.split(' ', 1)[-1] if ' ' in line else line
                    done += 1
                    _fileop_progress('extract', fname[:80], done, max(done, 1))
                    gevent.sleep(0)
            proc.wait()
            if proc.returncode != 0:
                _fileop_finish('extract', False, f'7z exited with code {proc.returncode}')
                return
        elif basename.lower().endswith('.zip'):
            with zipfile.ZipFile(archive, 'r') as zf:
                for member in zf.namelist():
                    if _check_cancel():
                        try: shutil.rmtree(extract_to, ignore_errors=True)
                        except Exception: pass
                        _fileop_finish('extract', False, 'Cancelled')
                        return
                    zf.extract(member, extract_to)
                    done += 1
                    if done % 20 == 0 or done == total:
                        _fileop_progress('extract', member.split('/')[-1] or member, done, total)
                        gevent.sleep(0)
        else:
            # tar variants
            with tarfile.open(archive, 'r:*') as tf:
                for member in tf:
                    if _check_cancel():
                        try: shutil.rmtree(extract_to, ignore_errors=True)
                        except Exception: pass
                        _fileop_finish('extract', False, 'Cancelled')
                        return
                    tf.extract(member, extract_to, filter='data')
                    done += 1
                    if done % 20 == 0 or done == total:
                        name = member.name.split('/')[-1] or member.name
                        _fileop_progress('extract', name, done, total)
                        gevent.sleep(0)

        _chown_recursive(extract_to, _bg_username)
        _fileop_finish('extract', True, f'Extracted to {os.path.basename(extract_to)}/ ({done} files)')
    except Exception as e:
        _fileop_finish('extract', False, str(e))


# ── Transfer to remote NAS ──

@files_bp.route('/api/files/remote-servers', methods=['GET'])
@require_auth
def files_remote_servers():
    """Return available SSH/NAS servers (from backup config) for file transfer."""
    try:
        from blueprints.backup import load_ssh_configs
        configs = load_ssh_configs()
        safe = []
        for c in configs:
            safe.append({
                'id': c.get('id'), 'name': c.get('name', ''),
                'host': c.get('host', ''), 'port': c.get('port', 22),
                'username': c.get('username', ''),
                'remote_path': c.get('remote_path', '~/'),
                'has_password': bool(c.get('password')),
                'has_key': bool(c.get('key_path'))
            })
        return jsonify({'servers': safe})
    except Exception as e:
        return jsonify({'servers': [], 'error': str(e)})


@files_bp.route('/api/files/transfer-remote', methods=['POST'])
@require_auth
def files_transfer_remote():
    """Transfer files/folders to a remote NAS via SCP with progress."""
    data = request.json or {}
    server_id = data.get('server_id')
    paths = data.get('paths', [])
    remote_dest = data.get('remote_path', '')

    if not server_id:
        return jsonify({'error': 'No server selected'}), 400
    if not paths:
        return jsonify({'error': 'No files to transfer'}), 400

    # Get SSH config
    from blueprints.backup import load_ssh_configs
    configs = load_ssh_configs()
    server = next((c for c in configs if c.get('id') == server_id), None)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    # Quick connectivity check before starting background transfer
    import socket as _socket
    host = server.get('host', '')
    port = server.get('port', 22)
    try:
        s = _socket.create_connection((host, port), timeout=5)
        s.close()
    except Exception:
        return jsonify({'error': f'Server {server.get("name",host)} ({host}:{port}) is unreachable. Check that it is powered on and connected to the network.'}), 502

    # Resolve local paths
    resolved = []
    for p in paths:
        rp = safe_path(p)
        if rp and os.path.exists(rp):
            resolved.append(rp)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    # Compute total bytes
    total_bytes = 0
    total_files = 0
    for rp in resolved:
        if os.path.isdir(rp):
            for root, dirs, files in os.walk(rp):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except OSError:
                        pass
        else:
            try:
                total_bytes += os.path.getsize(rp)
                total_files += 1
            except OSError:
                pass

    with _fileop_lock:
        if _fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'transfer'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False
        _fileop_state['meta'] = {
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paths': [os.path.basename(p) for p in resolved[:10]],
            'total_bytes': total_bytes,
            'total_files': total_files,
            'started': time.time(),
        }

    dest = remote_dest or server.get('remote_path', '~/')
    # Capture requesting user for per-user known_hosts
    requesting_user = getattr(g, 'username', None) or 'root'

    # Persist transfer task so it can resume after server restart
    _save_transfer_task(resolved, server_id, dest, requesting_user)

    socketio.start_background_task(_bg_transfer_remote, resolved, server, dest, total_bytes, total_files, requesting_user)

    size_str = _fmt_bytes(total_bytes)
    return jsonify({
        'async': True,
        'cancellable': True,
        'message': f'Transferring {len(resolved)} items ({size_str}) to {server["name"]}'
    })


def _fmt_bytes(b):
    """Format bytes to human-readable — delegates to utils.fmt_bytes."""
    return fmt_bytes(b)


def _bg_transfer_remote(resolved, server, remote_dest, total_bytes, total_files, requesting_user='root'):
    """Background rsync transfer with real-time progress, resume and cancel support."""
    import subprocess, re as _re, shlex as _shlex, pty, select as _select

    host = server['host']
    port = server.get('port', 22)
    user = server['username']
    password = server.get('password', '')
    key_path = server.get('key_path', '')

    # Resolve the requesting user's home directory
    _user_home = _get_user_home(requesting_user)
    user_kh = os.path.join(_user_home, '.ssh', 'known_hosts')

    # Ensure .ssh dir exists for the user
    ssh_dir = os.path.join(_user_home, '.ssh')
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    try:
        import pwd
        pw = pwd.getpwnam(requesting_user)
        os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass

    # Build SSH command for rsync — use user's known_hosts file
    # Quote paths that may contain spaces
    ssh_cmd_parts = ['ssh', '-p', str(port), '-o', 'StrictHostKeyChecking=accept-new',
                     '-o', 'UserKnownHostsFile=' + _shlex.quote(user_kh),
                     '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=10',
                     '-o', 'ServerAliveCountMax=3']
    if key_path and os.path.exists(key_path):
        ssh_cmd_parts += ['-i', _shlex.quote(key_path)]
    ssh_cmd_str = ' '.join(ssh_cmd_parts)

    # Resolve remote dest (expand ~)
    dest = remote_dest
    if '~' in dest:
        try:
            from ssh_utils import get_ssh_client as _gsc, ssh_resolve_home as _srh
            ssh = _gsc(host, port, user, password=password, key_path=key_path, timeout=15)
            home = _srh(ssh)
            if home:
                dest = dest.replace('~', home)
            ssh.exec_command(f'mkdir -p {_shlex.quote(dest)}')
            gevent.sleep(0.3)
            ssh.close()
        except Exception:
            pass

    # Pre-compute per-item sizes for accurate progress
    item_sizes = []
    for rp in resolved:
        if os.path.isfile(rp):
            try:
                item_sizes.append(os.path.getsize(rp))
            except OSError:
                item_sizes.append(0)
        else:
            sz = 0
            for rt, _, fns in os.walk(rp):
                for fn in fns:
                    try:
                        sz += os.path.getsize(os.path.join(rt, fn))
                    except OSError:
                        pass
            item_sizes.append(sz)

    bytes_sent_total = [0]
    files_done = [0]
    current_file = ['']
    last_emit = [0]

    def emit_progress(forced=False):
        now = time.time()
        if not forced and now - last_emit[0] < 0.3:
            return
        last_emit[0] = now
        pct = round(bytes_sent_total[0] / total_bytes * 100, 1) if total_bytes > 0 else 0

        # Store byte-based progress in state (so /operation-status returns accurate %)
        detail = {
            'bytes_sent': bytes_sent_total[0], 'total_bytes': total_bytes,
            'files_sent': files_done[0], 'total_files': total_files,
            'current_file': current_file[0], 'percent': pct,
            'sent_fmt': _fmt_bytes(bytes_sent_total[0]), 'total_fmt': _fmt_bytes(total_bytes),
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paused': _fileop_is_paused(),
        }
        prog = {'operation': 'transfer', 'current_file': current_file[0],
                'done': files_done[0], 'total': total_files, 'percent': pct}
        with _fileop_lock:
            _fileop_state['progress'] = prog
        _fileop_emit('fileop_progress', prog)
        socketio.emit('fileop_transfer_detail', detail)

    def _run_rsync(rsync_cmd, env, idx, local_path, name):
        """Run rsync with PTY for real-time progress. Returns (returncode, stderr_text)."""
        master_fd, slave_fd = pty.openpty()
        err_r, err_w = os.pipe()
        proc = subprocess.Popen(
            rsync_cmd, stdout=slave_fd, stderr=err_w,
            env=env, close_fds=True
        )
        os.close(slave_fd)
        os.close(err_w)
        with _fileop_lock:
            _fileop_state['_proc'] = proc

        patt = _re.compile(r'([\d,]+)\s+(\d+)%\s+([\d.]+\w+/s)')
        buf = ''
        completed_bytes = sum(item_sizes[:idx])

        try:
            while True:
                if _fileop_cancelled():
                    # Resume first if paused (stopped process ignores SIGTERM)
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                    os.close(master_fd)
                    os.close(err_r)
                    with _fileop_lock:
                        _fileop_state['_proc'] = None
                    return (-999, 'cancelled')

                rlist = _select.select([master_fd], [], [], 0.5)[0]
                if rlist:
                    try:
                        data = os.read(master_fd, 4096).decode('utf-8', errors='replace')
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data
                    parts = _re.split(r'[\r\n]+', buf)
                    buf = parts[-1]
                    for p in parts[:-1]:
                        m = patt.search(p)
                        if m:
                            file_pct = int(m.group(2))
                            bytes_sent_total[0] = completed_bytes + int(item_sizes[idx] * file_pct / 100)
                            current_file[0] = name
                            emit_progress()
                    gevent.sleep(0)
                elif proc.poll() is not None:
                    break
        except (OSError, IOError) as exc:
            print(f'  [transfer] PTY read error for {name}: {exc}')
        except Exception as exc:
            print(f'  [transfer] Unexpected error in rsync read loop for {name}: {exc}')

        os.close(master_fd)
        # Use timeout to prevent permanent hang on stuck rsync
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print(f'  [transfer] rsync process for {name} did not exit in 30s, killing…')
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        stderr_text = ''
        try:
            stderr_text = os.read(err_r, 65536).decode('utf-8', errors='replace')
        except Exception:
            pass
        os.close(err_r)
        with _fileop_lock:
            _fileop_state['_proc'] = None
        return (proc.returncode, stderr_text)

    try:
        for idx, local_path in enumerate(resolved):
            if _fileop_cancelled():
                _clear_transfer_task()
                _fileop_finish('transfer', False, 'Transfer cancelled by user')
                return

            # Wait while paused (between items)
            while _fileop_is_paused():
                if _fileop_cancelled():
                    _clear_transfer_task()
                    _fileop_finish('transfer', False, 'Transfer cancelled by user')
                    return
                gevent.sleep(0.5)

            name = os.path.basename(local_path)
            current_file[0] = name
            remote_target = f'{user}@{host}:{_shlex.quote(dest)}/'

            rsync_cmd = [
                'rsync', '-rlt', '--partial', '--inplace',
                '--info=progress2', '--no-inc-recursive',
                '-e', ssh_cmd_str
            ]
            src = local_path.rstrip('/') + '/' if os.path.isdir(local_path) else local_path
            if os.path.isdir(local_path):
                remote_target = f'{user}@{host}:{_shlex.quote(dest + "/" + name)}/'
            rsync_cmd += [src, remote_target]

            env = os.environ.copy()
            if password and not (key_path and os.path.exists(key_path)):
                rsync_cmd = ['sshpass', '-e'] + rsync_cmd
                env['SSHPASS'] = password

            rc, stderr_out = _run_rsync(rsync_cmd, env, idx, local_path, name)

            if rc == -999:  # cancelled
                _clear_transfer_task()
                _fileop_finish('transfer', False, 'Transfer cancelled by user')
                return

            if rc not in (0, 24):
                # On network/IO error, retry up to 3 times (rsync resumes from partial)
                retryable_codes = (1, 5, 10, 11, 12, 23, 30, 35, 255)
                if rc in retryable_codes:
                    max_retries = 3
                    retry_ok = False
                    for attempt in range(1, max_retries + 1):
                        delay = min(5 * attempt, 15)  # 5s, 10s, 15s
                        _fileop_progress('transfer', f'Retrying ({attempt}/{max_retries}): {name}…', files_done[0], total_files)
                        print(f'  [transfer] rsync {name} failed (rc={rc}), retry {attempt}/{max_retries} in {delay}s')
                        gevent.sleep(delay)
                        if _fileop_cancelled():
                            _clear_transfer_task()
                            _fileop_finish('transfer', False, 'Transfer cancelled by user')
                            return
                        rc2, stderr2 = _run_rsync(rsync_cmd, env, idx, local_path, name)
                        if rc2 == -999:
                            _clear_transfer_task()
                            _fileop_finish('transfer', False, 'Transfer cancelled by user')
                            return
                        if rc2 in (0, 24):
                            retry_ok = True
                            break
                        rc = rc2
                        stderr_out = stderr2
                    if not retry_ok:
                        raise RuntimeError(f'rsync {name} (rc={rc}): {stderr_out.strip()}')
                else:
                    raise RuntimeError(f'rsync {name} (rc={rc}): {stderr_out.strip()}')

            files_done[0] = idx + 1
            bytes_sent_total[0] = sum(item_sizes[:idx + 1])
            emit_progress(forced=True)

        with _fileop_lock:
            _fileop_state['_proc'] = None
        _clear_transfer_task()
        size_str = _fmt_bytes(total_bytes)
        _fileop_finish('transfer', True,
                       f'Transferred {files_done[0]} files ({size_str}) to {server["name"]}')
    except Exception as e:
        import traceback
        print(f'  [transfer] EXCEPTION: {e}')
        traceback.print_exc()
        with _fileop_lock:
            _fileop_state['_proc'] = None
        # On crash / network error keep resume file so startup can retry.
        # Only clear on explicit cancel or permanent failure (we keep the file
        # for retryable errors so the next startup picks it up).
        err_msg = str(e)
        retryable_keywords = ('No route to host', 'Connection refused', 'timed out',
                              'Connection reset', 'Broken pipe', 'Network is unreachable',
                              'Connection timed out', 'Name or service not known')
        is_retryable = any(kw in err_msg for kw in retryable_keywords)
        if not is_retryable:
            _clear_transfer_task()
        # User-friendly error messages for common network errors
        for kw in ('No route to host', 'Connection refused', 'timed out', 'Connection reset',
                   'Network is unreachable', 'Name or service not known'):
            if kw.lower() in err_msg.lower():
                err_msg = f'Cannot connect to {server["name"]} ({server["host"]}). Check that the server is powered on and connected to the network.'
                break
        _fileop_finish('transfer', False, err_msg)


def _resume_interrupted_transfer():
    """Check for a persisted transfer task and resume it after server restart."""
    task = _load_json(TRANSFER_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('paths', [])
    server_id = task.get('server_id')
    remote_dest = task.get('remote_dest', '~/')
    requesting_user = task.get('requesting_user', 'root')
    started = task.get('started', 0)

    # Ignore tasks older than 7 days (stale)
    if time.time() - started > 7 * 86400:
        _clear_transfer_task()
        return

    if not resolved or not server_id:
        _clear_transfer_task()
        return

    # Verify at least one source still exists and validate paths
    valid = []
    for p in resolved:
        # Only allow absolute paths and ensure they actually exist
        rp = os.path.realpath(p)
        if rp and os.path.isabs(rp):
            try:
                exists = _fs_call(os.path.exists, rp, timeout=3)
            except TimeoutError:
                exists = False
            if exists:
                valid.append(rp)
    if not valid:
        _clear_transfer_task()
        return

    # Load server config
    try:
        from blueprints.backup import load_ssh_configs
        configs = load_ssh_configs()
        server = next((c for c in configs if c.get('id') == server_id), None)
    except Exception:
        server = None
    if not server:
        _clear_transfer_task()
        return

    # Quick connectivity check before resuming
    import socket as _socket
    try:
        s = _socket.create_connection((server.get('host', ''), server.get('port', 22)), timeout=10)
        s.close()
    except Exception:
        print(f'  [transfer-resume] Server {server.get("name", server.get("host", ""))} is unreachable, skipping resume for now')
        # Don't clear the task — it will be retried on next restart
        return

    # Compute totals
    total_bytes = 0
    total_files = 0
    for rp in valid:
        if os.path.isdir(rp):
            for root, dirs, files in os.walk(rp):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except OSError:
                        pass
        else:
            try:
                total_bytes += os.path.getsize(rp)
                total_files += 1
            except OSError:
                pass

    with _fileop_lock:
        if _fileop_state['active']:
            return  # something else already running
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'transfer'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['meta'] = {
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paths': [os.path.basename(p) for p in valid[:10]],
            'total_bytes': total_bytes,
            'total_files': total_files,
            'started': started,
            'resumed': True,
        }

    dest = remote_dest or server.get('remote_path', '~/')
    print(f'  [transfer-resume] Resuming transfer of {len(valid)} items to {server.get("name", server.get("host", ""))}…')
    elog('files', 'info', f'Resuming interrupted transfer to {server.get("name", server.get("host", ""))} ({len(valid)} items)')
    socketio.start_background_task(_bg_transfer_remote, valid, server, dest, total_bytes, total_files, requesting_user)


