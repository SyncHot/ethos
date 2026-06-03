"""EthOS — File Manager: core operation helpers, task persistence, progress/cancel routes."""
import os
import sys
import sys as _sys
import json
import time
import re
import zipfile
import tarfile
import shutil
import threading as _threading
import signal
import gevent
import pwd
from flask import Blueprint, request, jsonify
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
)
from utils import (
    load_json as _load_json,
    save_json as _save_json,
    safe_path as _safe_path_util,
    DATA_ROOT,
    ALLOWED_ROOTS as _ALLOWED_ROOTS,
)
from blueprints.eventlog import log as elog
from blueprints.admin_required import admin_required
from blueprints.auth import require_auth, get_current_user, _is_sudo_mode


def _main():
    """Return the main file_manager module."""
    return _sys.modules['blueprints.file_manager']


def _pending_downloads_getter():
    """Lazy accessor for _pending_downloads from file_manager_photos."""
    photos = _sys.modules.get('blueprints.file_manager_photos')
    if photos:
        return getattr(photos, '_pending_downloads', {})
    return {}


files_bp = _sys.modules['blueprints.file_manager'].files_bp

class _SocketioProxy:
    """Lazy proxy for socketio - avoids import-lock issues under gevent."""
    def __getattr__(self, name):
        sio = getattr(_sys.modules.get('app'), 'socketio', None)
        if sio is None:
            # Return a no-op function instead of raising an error
            def noop(*args, **kwargs):
                pass
            return noop
        return getattr(sio, name)
socketio = _SocketioProxy()

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

    _fm = _main()._fileop_channels['fm']
    with _main()._fileop_lock:
        if _fm['active']:
            return
        _fm['active'] = True
        _fm['operation'] = 'copy'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    cur_user = {'username': username} if username else None
    elog('files', 'info', f'Resuming copy after restart: {len(valid)} items → {dest_dir}')
    socketio.start_background_task(_main()._bg_copy, valid, dest_dir, total, on_conflict, cur_user)


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

    _fm = _main()._fileop_channels['fm']
    with _main()._fileop_lock:
        if _fm['active']:
            return
        _fm['active'] = True
        _fm['operation'] = 'move'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    cur_user = {'username': username} if username else None
    elog('files', 'info', f'Resuming move after restart: {len(valid)} items → {dest_dir}')
    socketio.start_background_task(_main()._bg_move, valid, dest_dir, total, on_conflict, dest_user_path, cur_user)


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

    with _main()._fileop_lock:
        if _main()._fileop_state['active']:
            return  # something else already running
        _main()._fileop_state['active'] = True
        _main()._fileop_state['operation'] = 'compress'
        _main()._fileop_state['progress'] = None
        _main()._fileop_state['cancel'] = False
        _main()._fileop_state['paused'] = False

    elog('files', 'info', f'Resuming compression after restart: {os.path.basename(archive_path)} ({total} files)')
    socketio.start_background_task(_main()._bg_compress, valid, archive_path, fmt, total, cur_user)

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

    with _main()._fileop_lock:
        if _main()._fileop_state['active']:
            return  # something else already running
        _main()._fileop_state['active'] = True
        _main()._fileop_state['operation'] = 'download'
        _main()._fileop_state['progress'] = None
        _main()._fileop_state['cancel'] = False
        _main()._fileop_state['paused'] = False

    elog('files', 'info', f'Resuming ZIP preparation after restart: {zip_name} ({total} files)')
    socketio.start_background_task(_main()._bg_download_zip, valid, tmp_path, zip_name, download_id, total)

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
            if _main()._listdir_cache_get(rpath) is not None:
                continue  # already warm
            _preload_sem.acquire()
            try:
                if _main()._listdir_cache_get(rpath) is not None:
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
                    _main()._listdir_cache_set(rpath, items_pre, mtime=_mtime)
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
    return _main()._OP_CHANNEL.get(operation, 'bg')

def _fileop_progress(operation, current_file, done, total, ch=None):
    if ch is None:
        ch = _ch_for(operation)
    pct = round(done / total * 100, 1) if total > 0 else 0
    prog = {'operation': operation, 'current_file': current_file,
            'done': done, 'total': total, 'percent': pct, 'channel': ch}
    with _main()._fileop_lock:
        _main()._fileop_channels[ch]['progress'] = prog
    _fileop_emit('fileop_progress', prog)

def _fileop_cancelled(ch='bg'):
    """Check if current file operation on *ch* was cancelled."""
    with _main()._fileop_lock:
        return _main()._fileop_channels[ch].get('cancel', False)

def _fileop_is_paused(ch='bg'):
    """Check if current file operation on *ch* is paused."""
    with _main()._fileop_lock:
        return _main()._fileop_channels[ch].get('paused', False)

def _fileop_finish(operation, success, message, ch=None):
    if ch is None:
        ch = _ch_for(operation)
    result = {'status': 'completed' if success else 'failed',
              'message': message, 'time': time.time(), 'operation': operation, 'channel': ch}
    slot = _main()._fileop_channels[ch]
    with _main()._fileop_lock:
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
    with _main()._fileop_lock:
        for _ch_key, slot in _main()._fileop_channels.items():
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
    with _main()._fileop_lock:
        for ch_key, slot in _main()._fileop_channels.items():
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
    _pending_downloads = _pending_downloads_getter()
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
        with _main()._fileop_lock:
            for ch_key in ('bg', 'fm'):
                if _main()._fileop_channels[ch_key]['active']:
                    ch = ch_key
                    break
    if not ch:
        return jsonify({'error': 'No active operation'}), 400
    slot = _main()._fileop_channels.get(ch)
    if not slot:
        return jsonify({'error': 'Unknown channel'}), 400
    with _main()._fileop_lock:
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
        with _main()._fileop_lock:
            for ch_key in ('bg', 'fm'):
                if _main()._fileop_channels[ch_key]['active']:
                    ch = ch_key
                    break
    if not ch:
        return jsonify({'error': 'No active operation'}), 400
    slot = _main()._fileop_channels.get(ch)
    if not slot:
        return jsonify({'error': 'Unknown channel'}), 400
    with _main()._fileop_lock:
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


