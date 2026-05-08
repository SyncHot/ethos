"""EthOS — File Manager: gallery favorites, file list/search/download, thumbnails, upload, mkdir."""
import os
import sys
import sys as _sys
import json
import time
import re
import zipfile
import shutil
import hashlib
import secrets
import errno
import threading as _threading
import gevent
import gevent.lock as _gevent_lock
from PIL import Image
from flask import request, jsonify, send_file, Response, g
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
    get_data_disk as _get_data_disk,
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
from blueprints.auth import require_auth, get_current_user, _is_sudo_mode, make_user_cache_key


def _main():
    """Return the main file_manager module."""
    return _sys.modules['blueprints.file_manager']


files_bp = _sys.modules['blueprints.file_manager'].files_bp

class _SocketioProxy:
    """Lazy proxy for socketio – avoids import-lock issues under gevent."""
    def __getattr__(self, name):
        sio = getattr(_sys.modules.get('app'), 'socketio', None)
        if sio is None:
            raise AttributeError(f'socketio not yet available ({name!r})')
        return getattr(sio, name)
socketio = _SocketioProxy()

def _try_wake_path_lazy(path, **kw):
    """Lazy wrapper for try_wake_path (avoids circular import with storage blueprint)."""
    mod = _sys.modules.get('blueprints.storage')
    fn = getattr(mod, 'try_wake_path', None) if mod else None
    if fn:
        return fn(path, **kw)

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
    real = _main().safe_path(path)
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
        real = _main().safe_path(fpath)
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
# cache decorator removed (listdir cache provides equivalent benefit)
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
                    _owner, _group = _main()._get_owner_group(st)
                    items.append({
                        'name': me,
                        'is_dir': True, 'is_link': False,
                        'size': 0, 'modified': st.st_mtime,
                        'permissions': oct(st.st_mode)[-3:],
                        'permissions_symbolic': _main()._mode_to_symbolic(st.st_mode),
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

    real_path = _main().safe_path(path)

    # If path looks like it's on a /media/devmon drive, try to wake / remount it
    if real_path and '/media/' in real_path:
        wake_path = real_path
        if wake_path:
            try:
                _fs_call(_try_wake_path_lazy, wake_path, timeout=25)
            except TimeoutError:
                return jsonify({'error': 'Disk not responding — try again shortly'}), 504

    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    # Check if attempting to list inside a protected folder
    blocked = _main()._require_folder_access(path)
    if blocked is not None:
        return blocked

    passwords = _main()._load_folder_passwords()
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
        cached_items = _main()._listdir_cache_get(_cache_key)
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
                owner, group = _main()._get_owner_group(stat)
                item_data = {
                    'name': entry.name,
                    'is_dir': entry.is_dir(),
                    'is_link': entry.is_symlink(),
                    'size': stat.st_size if not entry.is_dir() else 0,
                    'modified': stat.st_mtime,
                    'permissions': oct(stat.st_mode)[-3:],
                    'permissions_symbolic': _main()._mode_to_symbolic(stat.st_mode),
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
                        item_data['locked'] = not _main()._is_folder_unlocked(child_path)
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
        _main()._listdir_cache_set(_cache_key, items, mtime=_dir_mtime)

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
    real_path = _main().safe_path(path)
    if not real_path or not query:
        return jsonify({'items': []})

    # Cache passwords once per request (not per os.walk iteration)
    pw_folders = _main()._load_folder_passwords()
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
                if rel in pw_folders and not _main()._is_folder_unlocked(rel):
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
        cached = _main()._dirsize_cache_get(p)
        if cached is not None:
            results[p] = cached
            continue
        real = _main().safe_path(p)
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
            _main()._dirsize_cache_set(p, total)
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
        _main()._dirsize_cache_set(path, 0)
        with _main()._dirsize_bg_lock:
            if job_id in _main()._dirsize_bg_jobs:
                _main()._dirsize_bg_jobs[job_id].update({'size': 0, 'done': True, 'error': False, 'ts': time.monotonic()})
            _main()._dirsize_running_by_path.pop(path, None)
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
        _main()._dirsize_cache_set(path, total)
        with _main()._dirsize_bg_lock:
            if job_id in _main()._dirsize_bg_jobs:
                _main()._dirsize_bg_jobs[job_id].update({'size': total, 'done': True, 'error': False, 'ts': time.monotonic()})
            _main()._dirsize_running_by_path.pop(path, None)
    except Exception:
        with _main()._dirsize_bg_lock:
            if job_id in _main()._dirsize_bg_jobs:
                _main()._dirsize_bg_jobs[job_id].update({'size': 0, 'done': True, 'error': True, 'ts': time.monotonic()})
            _main()._dirsize_running_by_path.pop(path, None)


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
    # Prune stale completed jobs to keep _main()._dirsize_bg_jobs tidy
    _main()._dirsize_bg_jobs_prune()
    # Pseudo-filesystems and virtual paths that block gevent event loop
    _PSEUDO_FS_PATHS = {'/proc', '/sys', '/dev', '/run', '/snap', '/snap/bin'}

    for p in paths[:50]:
        # Skip pseudo-filesystems — scanning them would block the gevent event loop
        if p in _PSEUDO_FS_PATHS or p.startswith('/proc/') or p.startswith('/sys/') or p.startswith('/dev/'):
            cached[p] = 0
            continue
        # Return from cache immediately if fresh
        cv = _main()._dirsize_cache_get(p)
        if cv is not None:
            cached[p] = cv
            continue
        real = _main().safe_path(p)
        if not real or not os.path.isdir(real):
            cached[p] = 0
            continue
        # Reuse an existing running job for this path — avoids duplicate workers
        with _main()._dirsize_bg_lock:
            existing_jid = _main()._dirsize_running_by_path.get(p)
            if (existing_jid and existing_jid in _main()._dirsize_bg_jobs
                    and not _main()._dirsize_bg_jobs[existing_jid].get('done')):
                jobs[p] = existing_jid
                continue
        job_id = hashlib.md5(f'{p}:{time.monotonic()}'.encode()).hexdigest()[:12]
        with _main()._dirsize_bg_lock:
            _main()._dirsize_bg_jobs[job_id] = {'path': p, 'size': None, 'done': False, 'error': False, 'ts': time.monotonic()}
            _main()._dirsize_running_by_path[p] = job_id
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
    with _main()._dirsize_bg_lock:
        for path, job_id in jobs.items():
            entry = _main()._dirsize_bg_jobs.get(job_id)
            if entry is None:
                # Job expired/unknown — check cache
                cv = _main()._dirsize_cache_get(path)
                if cv is not None:
                    sizes[path] = cv
                else:
                    pending.append(path)
            elif entry['done']:
                sizes[path] = entry.get('size') or 0
                del _main()._dirsize_bg_jobs[job_id]
            else:
                pending.append(path)
    return jsonify({'sizes': sizes, 'pending': pending})


@files_bp.route('/api/files/download')
@require_auth
def files_download():
    path = request.args.get('path', '')
    real_path = _main().safe_path(path)
    if not real_path or not os.path.isfile(real_path):
        return jsonify({'error': 'File not found'}), 404
    # Enforce allowed roots even for admins — prevents reading /etc/shadow etc.
    if not any(real_path == r or real_path.startswith(r + '/') for r in _ALLOWED_ROOTS):
        return jsonify({'error': 'Access denied'}), 403
    # Block download from protected folders
    blocked = _main()._require_folder_access(path)
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
        rp = _main().safe_path(s)
        if rp and os.path.exists(rp):
            resolved.append(rp)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    total = _main()._count_items(resolved)

    # Determine archive filename
    if len(resolved) == 1:
        zip_name = os.path.splitext(os.path.basename(resolved[0]))[0] + '.zip'
    else:
        zip_name = 'download.zip'

    download_id = secrets.token_hex(16)
    tmp_path = os.path.join('/tmp', f'nasos_dl_{download_id}.zip')

    with _main()._fileop_lock:
        if _main()._fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _main()._fileop_state['active'] = True
        _main()._fileop_state['operation'] = 'download'
        _main()._fileop_state['progress'] = None
        _main()._fileop_state['cancel'] = False
        _main()._fileop_state['paused'] = False

    socketio.start_background_task(_bg_download_zip, resolved, tmp_path, zip_name, download_id, total)
    _main()._save_zip_task(resolved, tmp_path, zip_name, download_id, total)
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
        if _main()._fileop_cancelled():
            return True
        while _main()._fileop_is_paused():
            gevent.sleep(0.5)
            if _main()._fileop_cancelled():
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
                            _main()._fileop_progress('download', fn, done, total)
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
                    _main()._fileop_progress('download', os.path.basename(src), done, total)
                    gevent.sleep(0)

        if cancelled:
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass
            _main()._clear_zip_task()
            _main()._fileop_finish('download', False, 'Cancelled')
            return

        size_mb = round(os.path.getsize(tmp_path) / (1024*1024), 1)
        _pending_downloads[download_id] = {
            'path': tmp_path, 'name': zip_name, 'time': time.time()
        }
        _main()._clear_zip_task()
        _main()._fileop_finish('download', True, f'{zip_name} ({size_mb} MB)')
        # Emit download_id so frontend can trigger browser download
        _fileop_emit('fileop_download_ready', {'download_id': download_id, 'name': zip_name})
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass
        _main()._clear_zip_task()
        _main()._fileop_finish('download', False, str(e))


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
    real_path = _main().safe_path(path)
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
    real_path = _main().safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    def _preload_one(rpath):
        """Scan *rpath* and store result in the listing cache (semaphore-gated)."""
        if _main()._listdir_cache_get(rpath) is not None:
            return  # already fresh
        _preload_sem.acquire()
        try:
            # Re-check under semaphore — another greenlet may have just populated it
            if _main()._listdir_cache_get(rpath) is not None:
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
            _main()._listdir_cache_set(rpath, items_pre, mtime=_mtime)
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
    real_path = _main().safe_path(path)
    if not real_path or not os.path.isfile(real_path):
        return jsonify({'error': 'File not found'}), 404

    # Block preview from protected folders
    blocked = _main()._require_folder_access(path)
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
    real_path = _main().safe_path(path)
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
                    _main()._chown_to_user(cur)
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
        _main()._chown_to_user(filepath)
        uploaded.append(os.path.relpath(filepath, real_path))
    if uploaded:
        elog('files', 'info', f'Uploaded {len(uploaded)} file(s) to {path}', {'files': uploaded[:20]})
        _main()._listdir_cache_invalidate(path)
        _main()._dirsize_cache_invalidate(real_path)
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

    real_path = _main().safe_path(dest_path)
    if not real_path:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.isdir(real_path):
        if create_dir:
            try:
                os.makedirs(real_path, exist_ok=True)
                _main()._chown_to_user(real_path)
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
        _main()._chown_to_user(dest_file, username)
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
    _main()._listdir_cache_invalidate(dest_path)
    _main()._dirsize_cache_invalidate(real_path)
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
    real_path = _main().safe_path(data.get('path', ''))
    if not real_path:
        return jsonify({'error': 'Invalid path'}), 400

    blocked = _main()._require_folder_access(data.get('path', ''))
    if blocked is not None:
        return blocked

    try:
        os.makedirs(real_path, exist_ok=True)
        _main()._chown_to_user(real_path)
        _main()._dirsize_cache_invalidate(os.path.dirname(real_path))
        _main()._listdir_cache_invalidate(data.get('path', ''))
        elog('files', 'info', f'Created folder: {data.get("path", "")}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


