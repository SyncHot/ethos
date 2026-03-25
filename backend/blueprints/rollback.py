"""
EthOS — System Rollback Blueprint
File-level snapshots of EthOS application files for safe rollback.
Snapshots are tar.gz archives stored in data/snapshots/ with JSON metadata.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, app_path, data_path, q
from utils import load_json as _load_json, save_json as _save_json, require_tools, check_tool

from blueprints.admin_required import admin_required

logger = logging.getLogger(__name__)

rollback_bp = Blueprint('rollback', __name__, url_prefix='/api/rollback')

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SNAPSHOTS_DIR = data_path('snapshots')
AUTO_CONFIG_FILE = data_path('rollback_auto.json')
ETHOS_ROOT = app_path()

# Directories / files included in each snapshot (relative to ETHOS_ROOT)
SNAPSHOT_INCLUDES = [
    'backend',
    'frontend',
    'frontend_dist',
    'install.conf',
    'ethos.env',
]

DEFAULT_MAX_SNAPSHOTS = 5
SIZE_WARN_BYTES = 500 * 1024 * 1024  # 500 MB

_operation_lock = threading.Lock()
_current_op = None  # None | 'create' | 'restore'

# Valid snapshot ID pattern: timestamp-based hex or alphanumeric
_SNAPSHOT_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def _ensure_snapshots_dir():
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)


def _validate_snapshot_id(snap_id):
    """Return True if snap_id is safe (no path traversal)."""
    if not snap_id or not _SNAPSHOT_ID_RE.match(snap_id):
        return False
    if '..' in snap_id or '/' in snap_id or '\\' in snap_id:
        return False
    return True


def _snapshot_meta_path(snap_id):
    return os.path.join(SNAPSHOTS_DIR, f'{snap_id}.json')


def _snapshot_archive_path(snap_id):
    return os.path.join(SNAPSHOTS_DIR, f'{snap_id}.tar.gz')


def _generate_snapshot_id():
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def _load_auto_config():
    defaults = {
        'enabled': False,
        'before_update': True,
        'daily': False,
        'max_snapshots': DEFAULT_MAX_SNAPSHOTS,
    }
    cfg = _load_json(AUTO_CONFIG_FILE, None)
    if cfg is None:
        return defaults
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg


def _save_auto_config(cfg):
    _save_json(AUTO_CONFIG_FILE, cfg)


def _list_snapshots():
    """Return list of snapshot metadata dicts, sorted newest first."""
    _ensure_snapshots_dir()
    snapshots = []
    for fname in os.listdir(SNAPSHOTS_DIR):
        if fname.endswith('.json'):
            meta_path = os.path.join(SNAPSHOTS_DIR, fname)
            try:
                meta = _load_json(meta_path, None)
                if meta and 'id' in meta:
                    archive = _snapshot_archive_path(meta['id'])
                    if os.path.isfile(archive):
                        meta['size'] = os.path.getsize(archive)
                    else:
                        meta['size'] = 0
                    snapshots.append(meta)
            except Exception:
                pass
    snapshots.sort(key=lambda s: s.get('created_at', ''), reverse=True)
    return snapshots


def _estimate_snapshot_size():
    """Estimate tar size by summing included paths."""
    total = 0
    for rel in SNAPSHOT_INCLUDES:
        full = os.path.join(ETHOS_ROOT, rel)
        if os.path.isfile(full):
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
        elif os.path.isdir(full):
            for dirpath, _dirs, files in os.walk(full):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
    return total


def _cleanup_old_snapshots(max_count):
    """Remove oldest snapshots beyond max_count."""
    snaps = _list_snapshots()
    if len(snaps) <= max_count:
        return
    to_remove = snaps[max_count:]
    for s in to_remove:
        sid = s['id']
        archive = _snapshot_archive_path(sid)
        meta = _snapshot_meta_path(sid)
        for p in (archive, meta):
            try:
                os.remove(p)
            except OSError:
                pass
        logger.info('Auto-cleanup removed snapshot %s', sid)


def _dir_total_size(path):
    """Total size of all files in a directory."""
    total = 0
    if not os.path.isdir(path):
        return 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@rollback_bp.route('/snapshots', methods=['GET'])
@admin_required
def list_snapshots():
    snapshots = _list_snapshots()
    storage_used = _dir_total_size(SNAPSHOTS_DIR)
    return jsonify({
        'snapshots': snapshots,
        'storage_used': storage_used,
    })


@rollback_bp.route('/snapshots', methods=['POST'])
@admin_required
def create_snapshot():
    err = require_tools('tar')
    if err:
        return err
    global _current_op

    with _operation_lock:
        if _current_op is not None:
            return jsonify({'error': f'Another operation in progress: {_current_op}'}), 409

    data = request.get_json(silent=True) or {}
    description = str(data.get('description', ''))[:200]

    estimated = _estimate_snapshot_size()
    size_warning = None
    if estimated > SIZE_WARN_BYTES:
        size_warning = f'Estimated size: {estimated / (1024*1024):.0f} MB (above 500 MB)'

    _ensure_snapshots_dir()
    snap_id = _generate_snapshot_id()

    # Ensure unique ID
    while os.path.exists(_snapshot_archive_path(snap_id)):
        snap_id = snap_id + '-1'

    archive_path = _snapshot_archive_path(snap_id)

    # Build tar command — include only existing paths
    include_args = []
    for rel in SNAPSHOT_INCLUDES:
        full = os.path.join(ETHOS_ROOT, rel)
        if os.path.exists(full):
            include_args.append(q(rel))

    if not include_args:
        return jsonify({'error': 'No files to archive'}), 400

    tar_cmd = (
        f'tar czf {q(archive_path)} '
        f'-C {q(ETHOS_ROOT)} '
        f'{" ".join(include_args)}'
    )

    with _operation_lock:
        if _current_op is not None:
            return jsonify({'error': f'Another operation in progress: {_current_op}'}), 409
        _current_op = 'create'

    try:
        result = host_run(tar_cmd, timeout=300)
        if result.returncode != 0:
            logger.error('Snapshot tar failed: %s', result.stderr)
            return jsonify({'error': 'Snapshot creation failed',
                            'details': result.stderr[:500]}), 500

        archive_size = os.path.getsize(archive_path)
        meta = {
            'id': snap_id,
            'description': description,
            'created_at': datetime.now().isoformat(),
            'includes': SNAPSHOT_INCLUDES,
            'size': archive_size,
        }
        _save_json(_snapshot_meta_path(snap_id), meta)

        # Auto-cleanup
        auto_cfg = _load_auto_config()
        max_snap = auto_cfg.get('max_snapshots', DEFAULT_MAX_SNAPSHOTS)
        _cleanup_old_snapshots(max_snap)

        resp = {'success': True, 'snapshot': meta}
        if size_warning:
            resp['warning'] = size_warning
        return jsonify(resp)

    except Exception as e:
        logger.exception('Error creating snapshot')
        # Clean up partial archive
        try:
            os.remove(archive_path)
        except OSError:
            pass
        return jsonify({'error': str(e)}), 500
    finally:
        with _operation_lock:
            _current_op = None


@rollback_bp.route('/snapshots/<snap_id>/restore', methods=['POST'])
@admin_required
def restore_snapshot(snap_id):
    err = require_tools('tar')
    if err:
        return err
    global _current_op

    if not _validate_snapshot_id(snap_id):
        return jsonify({'error': 'Invalid snapshot ID'}), 400

    meta_path = _snapshot_meta_path(snap_id)
    archive_path = _snapshot_archive_path(snap_id)

    if not os.path.isfile(meta_path) or not os.path.isfile(archive_path):
        return jsonify({'error': 'Snapshot not found'}), 404

    with _operation_lock:
        if _current_op is not None:
            return jsonify({'error': f'Another operation in progress: {_current_op}'}), 409
        _current_op = 'restore'

    try:
        # 1. Create a pre-restore backup
        pre_id = 'pre-restore-' + _generate_snapshot_id()
        pre_archive = _snapshot_archive_path(pre_id)
        _ensure_snapshots_dir()

        include_args = []
        for rel in SNAPSHOT_INCLUDES:
            full = os.path.join(ETHOS_ROOT, rel)
            if os.path.exists(full):
                include_args.append(q(rel))

        if include_args:
            pre_tar = (
                f'tar czf {q(pre_archive)} '
                f'-C {q(ETHOS_ROOT)} '
                f'{" ".join(include_args)}'
            )
            pre_result = host_run(pre_tar, timeout=300)
            if pre_result.returncode == 0:
                pre_meta = {
                    'id': pre_id,
                    'description': f'Automatic backup before restoring {snap_id}',
                    'created_at': datetime.now().isoformat(),
                    'includes': SNAPSHOT_INCLUDES,
                    'size': os.path.getsize(pre_archive),
                    'auto': True,
                }
                _save_json(_snapshot_meta_path(pre_id), pre_meta)
            else:
                logger.warning('Pre-restore backup failed: %s', pre_result.stderr)

        # 2. Extract snapshot over current files
        extract_cmd = (
            f'tar xzf {q(archive_path)} '
            f'-C {q(ETHOS_ROOT)}'
        )
        result = host_run(extract_cmd, timeout=300)
        if result.returncode != 0:
            logger.error('Snapshot restore failed: %s', result.stderr)
            return jsonify({'error': 'Restore failed',
                            'details': result.stderr[:500]}), 500

        # 3. Schedule a service restart (non-blocking)
        threading.Thread(target=_delayed_restart, daemon=True).start()

        return jsonify({
            'success': True,
            'message': 'Snapshot restored. System will restart.',
            'pre_restore_id': pre_id,
        })

    except Exception as e:
        logger.exception('Error restoring snapshot')
        return jsonify({'error': str(e)}), 500
    finally:
        with _operation_lock:
            _current_op = None


def _delayed_restart():
    """Wait a moment then restart the EthOS service."""
    time.sleep(3)
    try:
        host_run('sudo systemctl restart ethos 2>/dev/null || true', timeout=10)
    except Exception:
        pass


@rollback_bp.route('/snapshots/<snap_id>', methods=['DELETE'])
@admin_required
def delete_snapshot(snap_id):
    if not _validate_snapshot_id(snap_id):
        return jsonify({'error': 'Invalid snapshot ID'}), 400

    meta_path = _snapshot_meta_path(snap_id)
    archive_path = _snapshot_archive_path(snap_id)

    if not os.path.isfile(meta_path):
        return jsonify({'error': 'Snapshot not found'}), 404

    for p in (archive_path, meta_path):
        try:
            os.remove(p)
        except OSError:
            pass

    return jsonify({'success': True})


@rollback_bp.route('/auto', methods=['GET'])
@admin_required
def get_auto_config():
    return jsonify(_load_auto_config())


@rollback_bp.route('/auto', methods=['PUT'])
@admin_required
def set_auto_config():
    data = request.get_json(silent=True) or {}
    cfg = _load_auto_config()

    if 'enabled' in data:
        cfg['enabled'] = bool(data['enabled'])
    if 'before_update' in data:
        cfg['before_update'] = bool(data['before_update'])
    if 'daily' in data:
        cfg['daily'] = bool(data['daily'])
    if 'max_snapshots' in data:
        try:
            val = int(data['max_snapshots'])
            cfg['max_snapshots'] = max(1, min(val, 50))
        except (ValueError, TypeError):
            pass

    _save_auto_config(cfg)

    # Apply new retention limit immediately
    _cleanup_old_snapshots(cfg['max_snapshots'])

    return jsonify(cfg)
