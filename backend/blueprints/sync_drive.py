"""
EthOS Sync Drive Blueprint -- Server-side sync API for desktop clients.

Endpoints:
    GET   /api/sync-drive/ping                  - Connection test
    POST  /api/sync-drive/devices/register      - Register a sync device
    POST  /api/sync-drive/devices/unregister    - Unregister a device
    GET   /api/sync-drive/devices               - List registered devices
    POST  /api/sync-drive/state                 - Get file state (listing + checksums)
    POST  /api/sync-drive/changes               - Get changes since version
    GET   /api/sync-drive/download              - Download a file
    POST  /api/sync-drive/upload                - Upload a file (single)
    POST  /api/sync-drive/upload/init           - Init chunked upload
    POST  /api/sync-drive/upload/chunk          - Upload chunk
    POST  /api/sync-drive/upload/complete       - Complete chunked upload
    POST  /api/sync-drive/upload/abort          - Abort chunked upload
    POST  /api/sync-drive/delete                - Delete file/dir
    POST  /api/sync-drive/mkdir                 - Create directory
    POST  /api/sync-drive/move                  - Move/rename
    GET   /api/sync-drive/browse                - Browse directory tree
    GET   /api/sync-drive/roots                 - List available root locations
    GET   /api/sync-drive/versions              - Get file version history
    POST  /api/sync-drive/versions/restore      - Restore a version

SocketIO events:
    sync_drive_subscribe    - Client subscribes to change notifications
    sync_drive_file_changed - Server notifies of file changes
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

log = logging.getLogger(__name__)

sync_drive_bp = Blueprint('sync_drive', __name__)

_socketio = None
_db_lock = threading.Lock()
_upload_sessions = {}
_VERSION_DB = None
_VERSIONS_DIR = None


def init_sync_drive(socketio, data_dir=None):
    """Initialize the sync drive blueprint."""
    global _socketio, _VERSION_DB, _VERSIONS_DIR
    _socketio = socketio
    if data_dir is None:
        from host import data_path
        data_dir = data_path('sync_drive')
    os.makedirs(data_dir, exist_ok=True)
    _VERSION_DB = os.path.join(data_dir, 'sync_drive.db')
    _VERSIONS_DIR = os.path.join(data_dir, 'versions')
    os.makedirs(_VERSIONS_DIR, exist_ok=True)
    _init_db()
    _register_socketio_handlers(socketio)
    log.info("Sync Drive initialized: %s", data_dir)


def _init_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY, device_name TEXT, username TEXT,
            registered_at REAL, last_seen REAL, ip_address TEXT
        );
        CREATE TABLE IF NOT EXISTS change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
            username TEXT NOT NULL, action TEXT NOT NULL, path TEXT NOT NULL,
            size INTEGER, xxhash TEXT, device_id TEXT
        );
        CREATE TABLE IF NOT EXISTS file_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
            version_num INTEGER NOT NULL, size INTEGER, xxhash TEXT,
            mtime REAL, created_at REAL NOT NULL, stored_path TEXT, username TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_changelog_path ON change_log(path, timestamp);
        CREATE INDEX IF NOT EXISTS idx_changelog_time ON change_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_versions_path ON file_versions(path, version_num);
    """)
    conn.commit()
    conn.close()


def _get_db():
    conn = sqlite3.connect(_VERSION_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _register_socketio_handlers(socketio):
    @socketio.on('sync_drive_subscribe')
    def on_subscribe(data):
        from flask import request as req
        sid = req.sid
        log.info("Sync drive client subscribed: %s", sid)
        socketio.server.enter_room(sid, 'sync_drive', namespace='/')


def _emit_change(action, path, username='', **extra):
    if _socketio:
        data = {'action': action, 'path': path, 'username': username, **extra}
        _socketio.emit('sync_drive_file_changed', data, room='sync_drive')


def _log_change(username, action, path, size=0, xxhash='', device_id=''):
    conn = _get_db()
    conn.execute(
        "INSERT INTO change_log (timestamp, username, action, path, size, xxhash, device_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), username, action, path, size, xxhash, device_id))
    conn.commit()
    conn.close()


def _get_current_user():
    try:
        # Prefer g.username set by app.py auth guard (most reliable)
        from flask import g as _g
        if getattr(_g, 'username', None):
            return _g.username
        # Fallback to token-based lookup
        from app import get_current_user
        user = get_current_user()
        return user.get('username', '') if user else ''
    except Exception:
        return ''


def _get_user_root():
    """Get the home directory of the currently authenticated user."""
    username = _get_current_user()
    if not username:
        return None
    try:
        from host import get_user_home
        home = get_user_home(username)
        if home and os.path.isdir(home):
            return home
    except Exception:
        pass
    return None


def _get_allowed_roots():
    """Return list of root paths the current user may browse/sync.

    Always includes the user's home directory.
    Admin users also get storage pool mount points and network mounts.
    """
    roots = []

    # User's home — always available
    home = _get_user_root()
    if home:
        roots.append({'id': 'home', 'name': 'Home', 'path': home})

    # Storage pools and network mounts — admin only
    try:
        from flask import g as _g
        role = getattr(_g, 'role', None) or 'user'
    except Exception:
        role = 'user'

    if role == 'admin':
        try:
            from host import host_run, data_path, Q
            # Storage pools: /mnt/data, /mnt/pool*
            df_r = host_run("df -B1 --output=target 2>/dev/null", timeout=5)
            if df_r.returncode == 0:
                for line in df_r.stdout.strip().split('\n')[1:]:
                    mp = line.strip()
                    if mp == '/mnt/data':
                        roots.append({'id': 'volume1', 'name': 'Volume 1', 'path': mp})
                    elif mp.startswith('/mnt/pool'):
                        pool_name = os.path.basename(mp)
                        roots.append({'id': pool_name, 'name': pool_name.replace('pool', 'Pool '), 'path': mp})

            # Network mounts from /mnt/network/*
            net_dir = '/mnt/network'
            if os.path.isdir(net_dir):
                for entry in sorted(os.scandir(net_dir), key=lambda e: e.name.lower()):
                    if entry.is_dir():
                        roots.append({
                            'id': f'net_{entry.name}',
                            'name': f'Network: {entry.name}',
                            'path': entry.path,
                        })
        except Exception:
            pass

    return roots


def _safe_path(path):
    """Resolve a sync path to a real filesystem path.

    Supports two formats:
      - '/'              → user home root
      - '/some/folder'   → relative to user home
      - '/__root/<id>/…' → path relative to a named allowed root
    Prevents traversal outside allowed roots.
    """
    # Named root format: /__root/<root_id>/sub/path
    if path.startswith('/__root/'):
        parts = path[len('/__root/'):].split('/', 1)
        root_id = parts[0]
        sub_path = parts[1] if len(parts) > 1 else ''

        allowed = {r['id']: r['path'] for r in _get_allowed_roots()}
        base = allowed.get(root_id)
        if not base:
            return None
        if not sub_path or sub_path == '.':
            return base
        resolved = os.path.normpath(os.path.join(base, sub_path))
        if not resolved.startswith(base):
            return None
        return resolved

    # Default: relative to user home (backward compatible)
    user_root = _get_user_root()
    if not user_root:
        return None
    rel = path.lstrip('/').lstrip('\\')
    if not rel or rel == '.':
        return user_root
    resolved = os.path.normpath(os.path.join(user_root, rel))
    if not resolved.startswith(user_root):
        return None
    return resolved


def _compute_xxhash(filepath, chunk_size=1024*1024):
    try:
        import xxhash
        h = xxhash.xxh64()
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except ImportError:
        h = hashlib.sha256()
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return 'sha256:' + h.hexdigest()


def _save_version(filepath, username=''):
    if not _VERSIONS_DIR or not os.path.isfile(filepath):
        return
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT MAX(version_num) as max_v FROM file_versions WHERE path = ?",
            (filepath,)).fetchone()
        next_ver = (row['max_v'] or 0) + 1
        version_name = f"{uuid.uuid4().hex}_{next_ver}"
        version_path = os.path.join(_VERSIONS_DIR, version_name)
        shutil.copy2(filepath, version_path)
        st = os.stat(filepath)
        xxh = _compute_xxhash(filepath)
        conn.execute(
            "INSERT INTO file_versions (path, version_num, size, xxhash, mtime, created_at, stored_path, username) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (filepath, next_ver, st.st_size, xxh, st.st_mtime, time.time(), version_path, username))
        conn.commit()
        conn.close()
        _prune_versions(filepath, keep=32)
    except Exception as e:
        log.error("Failed to save version for %s: %s", filepath, e)


def _prune_versions(path, keep=32):
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, stored_path FROM file_versions WHERE path = ? ORDER BY version_num DESC",
        (path,)).fetchall()
    if len(rows) > keep:
        for row in rows[keep:]:
            stored = row['stored_path']
            if stored and os.path.exists(stored):
                os.unlink(stored)
            conn.execute("DELETE FROM file_versions WHERE id = ?", (row['id'],))
        conn.commit()
    conn.close()


# --- Endpoints ---

@sync_drive_bp.route('/api/sync-drive/ping', methods=['GET'])
def ping():
    return jsonify(ok=True, version='1.0.0', server='ethos')


@sync_drive_bp.route('/api/sync-drive/devices/register', methods=['POST'])
def register_device():
    data = request.get_json(force=True)
    device_id = data.get('device_id', '')
    device_name = data.get('device_name', 'Unknown')
    username = _get_current_user()
    if not device_id:
        return jsonify(error='device_id required'), 400
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO devices (device_id, device_name, username, registered_at, last_seen, ip_address) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (device_id, device_name, username, time.time(), time.time(), request.remote_addr))
    conn.commit()
    conn.close()
    log.info("Device registered: %s (%s) by %s", device_id, device_name, username)
    return jsonify(ok=True, device_id=device_id)


@sync_drive_bp.route('/api/sync-drive/devices/unregister', methods=['POST'])
def unregister_device():
    data = request.get_json(force=True)
    device_id = data.get('device_id', '')
    conn = _get_db()
    conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@sync_drive_bp.route('/api/sync-drive/devices', methods=['GET'])
def list_devices():
    conn = _get_db()
    rows = conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
    conn.close()
    return jsonify(ok=True, devices=[dict(r) for r in rows])


@sync_drive_bp.route('/api/sync-drive/state', methods=['POST'])
def get_state():
    data = request.get_json(force=True)
    remote_path = data.get('path', '')
    recursive = data.get('recursive', True)
    resolved = _safe_path(remote_path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    if not os.path.isdir(resolved):
        return jsonify(error='Directory not found'), 404
    files = []
    try:
        if recursive:
            for dirpath, dirnames, filenames in os.walk(resolved):
                dirnames[:] = [d for d in dirnames if not d.startswith('.')]
                rel_dir = os.path.relpath(dirpath, resolved)
                if rel_dir != '.':
                    files.append({
                        'path': rel_dir.replace('\\', '/'), 'is_dir': True,
                        'size': 0, 'mtime_ns': int(os.path.getmtime(dirpath) * 1_000_000_000),
                    })
                for fname in filenames:
                    if fname.startswith('.'):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    rel = os.path.relpath(fpath, resolved).replace('\\', '/')
                    try:
                        st = os.stat(fpath)
                        entry = {
                            'path': rel, 'is_dir': False,
                            'size': st.st_size, 'mtime_ns': int(st.st_mtime_ns),
                        }
                        if st.st_size < 100 * 1024 * 1024:
                            entry['xxhash'] = _compute_xxhash(fpath)
                        files.append(entry)
                    except (OSError, PermissionError):
                        continue
        else:
            for entry in os.scandir(resolved):
                try:
                    if entry.name.startswith('.'):
                        continue
                    st = entry.stat()
                    files.append({
                        'path': entry.name, 'is_dir': entry.is_dir(),
                        'size': st.st_size if not entry.is_dir() else 0,
                        'mtime_ns': int(st.st_mtime_ns),
                    })
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError) as e:
        return jsonify(error=f'Cannot read directory: {e}'), 500
    return jsonify(ok=True, files=files, count=len(files))


@sync_drive_bp.route('/api/sync-drive/changes', methods=['POST'])
def get_changes():
    data = request.get_json(force=True)
    path_prefix = data.get('path', '')
    since_version = data.get('since_version', 0)
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM change_log WHERE timestamp > ? AND path LIKE ? ORDER BY timestamp",
        (since_version, path_prefix + '%')).fetchall()
    conn.close()
    return jsonify(ok=True, changes=[dict(r) for r in rows], current_version=time.time())


@sync_drive_bp.route('/api/sync-drive/download', methods=['GET'])
def download_file():
    remote_path = request.args.get('path', '')
    resolved = _safe_path(remote_path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    if not os.path.isfile(resolved):
        return jsonify(error='File not found'), 404
    return send_file(resolved, as_attachment=True, download_name=os.path.basename(resolved))


@sync_drive_bp.route('/api/sync-drive/upload', methods=['POST'])
def upload_file():
    remote_path = request.form.get('path', '')
    file = request.files.get('file')
    if not file or not remote_path:
        return jsonify(error='file and path required'), 400
    resolved = _safe_path(remote_path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    username = _get_current_user()
    if os.path.isfile(resolved):
        _save_version(resolved, username)
    parent = os.path.dirname(resolved)
    os.makedirs(parent, exist_ok=True)
    tmp_path = resolved + '.ethos-tmp'
    try:
        file.save(tmp_path)
        os.replace(tmp_path, resolved)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    size = os.path.getsize(resolved)
    xxhash = _compute_xxhash(resolved)
    _log_change(username, 'modified', remote_path, size=size, xxhash=xxhash)
    _emit_change('modified', remote_path, username=username,
                 size=size, mtime_ns=int(os.path.getmtime(resolved) * 1e9))
    return jsonify(ok=True, size=size, xxhash=xxhash)


@sync_drive_bp.route('/api/sync-drive/upload/init', methods=['POST'])
def upload_init():
    data = request.get_json(force=True)
    remote_path = data.get('path', '')
    file_size = data.get('size', 0)
    filename = data.get('filename', '')
    if not remote_path:
        return jsonify(error='path required'), 400
    resolved = _safe_path(remote_path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    session_id = secrets.token_hex(16)
    chunks_dir = tempfile.mkdtemp(prefix='ethos_sync_')
    _upload_sessions[session_id] = {
        'path': resolved, 'remote_path': remote_path, 'size': file_size,
        'filename': filename, 'chunks_dir': chunks_dir, 'chunks': {},
        'created_at': time.time(),
    }
    return jsonify(ok=True, session_id=session_id)


@sync_drive_bp.route('/api/sync-drive/upload/chunk', methods=['POST'])
def upload_chunk():
    session_id = request.form.get('session_id', '')
    offset = int(request.form.get('offset', 0))
    index = int(request.form.get('index', 0))
    chunk = request.files.get('chunk')
    if session_id not in _upload_sessions:
        return jsonify(error='Invalid session'), 400
    if not chunk:
        return jsonify(error='No chunk data'), 400
    session = _upload_sessions[session_id]
    chunk_path = os.path.join(session['chunks_dir'], f'chunk_{index:06d}')
    chunk.save(chunk_path)
    session['chunks'][index] = {'path': chunk_path, 'offset': offset}
    return jsonify(ok=True, index=index)


@sync_drive_bp.route('/api/sync-drive/upload/complete', methods=['POST'])
def upload_complete():
    data = request.get_json(force=True)
    session_id = data.get('session_id', '')
    if session_id not in _upload_sessions:
        return jsonify(error='Invalid session'), 400
    session = _upload_sessions.pop(session_id)
    resolved = session['path']
    remote_path = session['remote_path']
    username = _get_current_user()
    if os.path.isfile(resolved):
        _save_version(resolved, username)
    parent = os.path.dirname(resolved)
    os.makedirs(parent, exist_ok=True)
    tmp_path = resolved + '.ethos-tmp'
    try:
        sorted_chunks = sorted(session['chunks'].values(), key=lambda c: c['offset'])
        with open(tmp_path, 'wb') as out:
            for chunk_info in sorted_chunks:
                with open(chunk_info['path'], 'rb') as chunk_f:
                    shutil.copyfileobj(chunk_f, out)
        os.replace(tmp_path, resolved)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return jsonify(error=f'Assembly failed: {e}'), 500
    finally:
        shutil.rmtree(session['chunks_dir'], ignore_errors=True)
    size = os.path.getsize(resolved)
    xxhash = _compute_xxhash(resolved)
    _log_change(username, 'modified', remote_path, size=size, xxhash=xxhash)
    _emit_change('modified', remote_path, username=username,
                 size=size, mtime_ns=int(os.path.getmtime(resolved) * 1e9))
    return jsonify(ok=True, size=size, xxhash=xxhash)


@sync_drive_bp.route('/api/sync-drive/upload/abort', methods=['POST'])
def upload_abort():
    data = request.get_json(force=True)
    session_id = data.get('session_id', '')
    session = _upload_sessions.pop(session_id, None)
    if session:
        shutil.rmtree(session['chunks_dir'], ignore_errors=True)
    return jsonify(ok=True)


@sync_drive_bp.route('/api/sync-drive/delete', methods=['POST'])
def delete_file():
    data = request.get_json(force=True)
    path = data.get('path', '')
    resolved = _safe_path(path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    username = _get_current_user()
    if os.path.isfile(resolved):
        _save_version(resolved, username)
    if os.path.isdir(resolved):
        shutil.rmtree(resolved)
    elif os.path.isfile(resolved):
        os.unlink(resolved)
    else:
        return jsonify(error='Path not found'), 404
    _log_change(username, 'deleted', path)
    _emit_change('deleted', path, username=username)
    return jsonify(ok=True)


@sync_drive_bp.route('/api/sync-drive/mkdir', methods=['POST'])
def mkdir():
    data = request.get_json(force=True)
    path = data.get('path', '')
    resolved = _safe_path(path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    os.makedirs(resolved, exist_ok=True)
    _log_change(_get_current_user(), 'created', path)
    return jsonify(ok=True)


@sync_drive_bp.route('/api/sync-drive/move', methods=['POST'])
def move_file():
    data = request.get_json(force=True)
    src = data.get('src', '')
    dst = data.get('dst', '')
    src_resolved = _safe_path(src)
    dst_resolved = _safe_path(dst)
    if not src_resolved or not dst_resolved:
        return jsonify(error='Invalid path'), 400
    if not os.path.exists(src_resolved):
        return jsonify(error='Source not found'), 404
    dst_parent = os.path.dirname(dst_resolved)
    os.makedirs(dst_parent, exist_ok=True)
    shutil.move(src_resolved, dst_resolved)
    username = _get_current_user()
    _log_change(username, 'moved', f'{src} -> {dst}')
    _emit_change('moved', dst, username=username, src_path=src)
    return jsonify(ok=True)


@sync_drive_bp.route('/api/sync-drive/roots', methods=['GET'])
def list_roots():
    """List available root locations the user can browse/sync."""
    username = _get_current_user()
    if not username:
        return jsonify(error='Not authenticated'), 401
    roots = _get_allowed_roots()
    # Return safe info — don't expose server filesystem paths to the client
    result = []
    for r in roots:
        result.append({
            'id': r['id'],
            'name': r['name'],
            'path': f"/__root/{r['id']}",
        })
    return jsonify(ok=True, roots=result)


@sync_drive_bp.route('/api/sync-drive/browse', methods=['GET'])
def browse():
    username = _get_current_user()
    if not username:
        return jsonify(error='Not authenticated'), 401
    path = request.args.get('path', '/')
    resolved = _safe_path(path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    if not os.path.isdir(resolved):
        return jsonify(error=f'Directory not found: {path}'), 404
    entries = []
    try:
        for entry in sorted(os.scandir(resolved), key=lambda e: e.name.lower()):
            if entry.name.startswith('.'):
                continue
            try:
                entries.append({
                    'name': entry.name,
                    'path': os.path.join(path, entry.name).replace('\\', '/'),
                    'is_dir': entry.is_dir(),
                    'size': entry.stat().st_size if entry.is_file() else 0,
                })
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError) as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True, entries=entries, path=path)


@sync_drive_bp.route('/api/sync-drive/versions', methods=['GET'])
def get_versions():
    path = request.args.get('path', '')
    if not path:
        return jsonify(error='path required'), 400
    resolved = _safe_path(path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, path, version_num, size, xxhash, mtime, created_at, username "
        "FROM file_versions WHERE path = ? ORDER BY version_num DESC",
        (resolved,)).fetchall()
    conn.close()
    versions = [{
        'id': r['id'], 'version': r['version_num'], 'size': r['size'],
        'xxhash': r['xxhash'], 'mtime': r['mtime'],
        'created_at': r['created_at'], 'username': r['username'],
    } for r in rows]
    return jsonify(ok=True, versions=versions, path=path)


@sync_drive_bp.route('/api/sync-drive/versions/restore', methods=['POST'])
def restore_version():
    data = request.get_json(force=True)
    path = data.get('path', '')
    version_id = data.get('version_id', 0)
    resolved = _safe_path(path)
    if not resolved:
        return jsonify(error='Invalid path'), 400
    conn = _get_db()
    row = conn.execute("SELECT * FROM file_versions WHERE id = ?", (version_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify(error='Version not found'), 404
    stored_path = row['stored_path']
    if not stored_path or not os.path.exists(stored_path):
        return jsonify(error='Version file missing'), 404
    username = _get_current_user()
    if os.path.isfile(resolved):
        _save_version(resolved, username)
    shutil.copy2(stored_path, resolved)
    _log_change(username, 'restored', path, size=os.path.getsize(resolved))
    _emit_change('modified', path, username=username)
    return jsonify(ok=True, restored_version=row['version_num'])
