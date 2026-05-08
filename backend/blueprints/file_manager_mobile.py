"""EthOS — File Manager: phone sync and trash."""
import os
import sys
import sys as _sys
import json
import time
import shutil
import secrets
import io
import gevent
from datetime import datetime
from PIL import Image
from flask import request, jsonify, send_file, g
from i18n import t
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from host import (
    data_path as _data_path,
    ETHOS_ROOT,
    host_run as _host_run,
    q,
    get_user_home as _get_user_home,
    user_data_path as _user_data_path,
)
from utils import (
    load_json as _load_json,
    save_json as _save_json,
    DATA_ROOT,
    ALLOWED_ROOTS as _ALLOWED_ROOTS,
    generate_thumbnail,
)
from blueprints.eventlog import log as elog
from blueprints.admin_required import admin_required
from blueprints.auth import require_auth, get_current_user


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
        dest_dir = _main().safe_path(os.path.join(dest_base, os.path.dirname(rel_path)))
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
    dest_dir = _main().safe_path(os.path.join(dest_base, os.path.dirname(rel_path)))
    if not dest_dir:
        return jsonify({'error': 'Invalid path'}), 400
    os.makedirs(dest_dir, exist_ok=True)
    _main()._chown_to_user(dest_dir)

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
    _main()._chown_to_user(dest_file)

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
    real_dest = _main().safe_path(dest)
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
            real = _main().safe_path(new_dest)
            if not real:
                return jsonify({'error': 'Invalid path'}), 400
            os.makedirs(real, exist_ok=True)
            _main()._chown_to_user(real)
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
    real_path = _main().safe_path(path)
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

        real_path = _main().safe_path(p)
        if not real_path:
            errors.append(f'Invalid path: {p}')
            continue
        if not os.path.exists(real_path):
            errors.append(f'Does not exist: {p}')
            continue
        try:
            _purge_thumb_cache(real_path)
            _main()._main()._dirsize_cache_invalidate(os.path.dirname(real_path))
            _main()._main()._listdir_cache_invalidate(p)

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

        original = _main().safe_path(item['original_path'])
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


