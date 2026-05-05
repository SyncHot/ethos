"""
EthOS — Backup Snapshots Sub-module
Routes: /btrfs-snapshots, /btrfs-snapshot, /btrfs-snapshot/<id>,
        /btrfs-snapshot/<id>/rollback, /btrfs-info,
        /snapshots, /snapshots/status, /snapshots/reset,
        /snapshots/<id>/browse, /snapshots/space, /snapshots/<id>,
        /snapshots/<id>/restore, /snapshots/<id>/download,
        /snapshots/<id>/transfer, /snapshots/import,
        /snapshots/received, /snapshots/received/adopt,
        /snapshots/received/restore, /snapshots/remote, /snapshots/pull,
        /discover-nas
All snapshot worker functions and helpers.
"""

import os
import subprocess
import threading
import time
import json
import re
import secrets
import shutil
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from flask import jsonify, request

import blueprints.backup as _backup_mod
from blueprints.backup import (
    backup_bp, BACKUP_DIR, _emit, emit_log, load_ssh_configs, HAS_SSH,
    logger,
)
from host import q, host_run

try:
    from scp import SCPClient
except ImportError:
    SCPClient = None

from ssh_utils import (
    get_ssh_client as _get_ssh_client,
    ssh_resolve_home as _ssh_resolve_home,
    ssh_ensure_writable_dir as _ssh_ensure_writable_dir,
)

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import get_ethos_user


# ═══════════════════════════════════════════════════════════
#  SYSTEM SNAPSHOTS  — Punkty przywracania systemu
#  Backs up: EthOS config/data, Docker compose projects,
#  Docker volumes, container list, system configs.
# ═══════════════════════════════════════════════════════════

SNAPSHOTS_DIR = os.path.join(BACKUP_DIR, 'snapshots')
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

_snapshot_state = _backup_mod._snapshot_state


def _snap_update(status=None, percent=None, message=None, log=None):
    if status:
        _snapshot_state['status'] = status
    if percent is not None:
        _snapshot_state['percent'] = percent
    if message:
        _snapshot_state['message'] = message
    if log:
        _snapshot_state['log'].append(log)
        _emit('snapshot_log', {'message': log})
    _emit('snapshot_progress', {
        'status': _snapshot_state['status'],
        'percent': _snapshot_state['percent'],
        'message': _snapshot_state['message'],
    })


def _docker_cmd(args, timeout=120):
    """Run a docker command, return (stdout, returncode)."""
    try:
        r = subprocess.run(
            ['docker'] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return '', 1


def _docker_ok():
    _, rc = _docker_cmd(['info'], timeout=5)
    return rc == 0


# ── Btrfs native snapshots (instant CoW) ──
BTRFS_SNAPSHOTS_MOUNT = '/mnt/snapshots'
BTRFS_DATA_MOUNT = '/mnt/data'


def _btrfs_available():
    """Check if /mnt/data is a btrfs filesystem with snapshot support."""
    if not os.path.ismount(BTRFS_DATA_MOUNT):
        return False
    r = host_run(f'stat -f -c %T {q(BTRFS_DATA_MOUNT)}', timeout=5)
    return r.returncode == 0 and 'btrfs' in r.stdout.lower()


def _ensure_snapshots_mounted():
    """Mount @snapshots subvolume if not already mounted."""
    if os.path.ismount(BTRFS_SNAPSHOTS_MOUNT):
        return True
    os.makedirs(BTRFS_SNAPSHOTS_MOUNT, exist_ok=True)
    # Find the device for /mnt/data
    r = host_run(f"findmnt -n -o SOURCE {q(BTRFS_DATA_MOUNT)}", timeout=5)
    if r.returncode != 0 or not r.stdout.strip():
        return False
    dev = r.stdout.strip().split('[')[0]  # strip subvol suffix like [/@data]
    r = host_run(
        f"mount -o subvol=@snapshots,noatime,compress=zstd:3 {q(dev)} {q(BTRFS_SNAPSHOTS_MOUNT)}",
        timeout=15
    )
    return r.returncode == 0


@backup_bp.route('/btrfs-snapshots', methods=['GET'])
def list_btrfs_snapshots():
    """List native btrfs snapshots of the data partition."""
    if not _btrfs_available():
        return jsonify({'ok': False, 'error': 'Btrfs not available', 'snapshots': []})
    if not _ensure_snapshots_mounted():
        return jsonify({'ok': False, 'error': 'Cannot mount @snapshots', 'snapshots': []})

    snapshots = []
    try:
        for name in sorted(os.listdir(BTRFS_SNAPSHOTS_MOUNT), reverse=True):
            snap_path = os.path.join(BTRFS_SNAPSHOTS_MOUNT, name)
            if not os.path.isdir(snap_path):
                continue
            # Read metadata if available
            meta_file = os.path.join(snap_path, '.snap_meta.json')
            meta = {'id': name, 'type': 'btrfs'}
            if os.path.isfile(meta_file):
                try:
                    with open(meta_file) as f:
                        meta.update(json.load(f))
                except Exception:
                    pass
            # Get btrfs subvolume info for creation time
            r = host_run(f'btrfs subvolume show {q(snap_path)}', timeout=10)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if line.startswith('Creation time:'):
                        meta['btrfs_created'] = line.split(':', 1)[1].strip()
            if 'created' not in meta:
                meta['created'] = meta.get('btrfs_created', name)
            snapshots.append(meta)
    except Exception as e:
        logger.exception("Error listing btrfs snapshots")
        return jsonify({'ok': False, 'error': str(e), 'snapshots': []})

    return jsonify({'ok': True, 'snapshots': snapshots})


@backup_bp.route('/btrfs-snapshot', methods=['POST'])
def create_btrfs_snapshot():
    """Create an instant btrfs snapshot of the data partition (@data → @snapshots)."""
    if not _btrfs_available():
        return jsonify({'error': 'Btrfs not available on data partition'}), 400
    if not _ensure_snapshots_mounted():
        return jsonify({'error': 'Cannot mount @snapshots subvolume'}), 500

    data = request.json or {}
    label = data.get('label', '').strip()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    snap_name = f'snap_{ts}'
    snap_path = os.path.join(BTRFS_SNAPSHOTS_MOUNT, snap_name)

    # Create read-only btrfs snapshot (instant, CoW — O(1) operation)
    r = host_run(
        f'btrfs subvolume snapshot -r {q(BTRFS_DATA_MOUNT)} {q(snap_path)}',
        timeout=30
    )
    if r.returncode != 0:
        return jsonify({'error': f'Snapshot failed: {r.stderr[:200]}'}), 500

    # Write metadata
    meta = {
        'id': snap_name,
        'label': label or f'Snapshot {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        'created': datetime.now().isoformat(),
        'type': 'btrfs',
        'hostname': _read_file_safe('/etc/hostname', 'unknown').strip(),
    }
    try:
        with open(os.path.join(snap_path, '.snap_meta.json'), 'w') as f:
            json.dump(meta, f, indent=2)
    except OSError:
        pass  # read-only snapshot — metadata write may fail, that's OK

    _emit('snapshot_created', meta)
    logger.info("Btrfs snapshot created: %s", snap_name)
    return jsonify({'ok': True, 'snapshot': meta})


@backup_bp.route('/btrfs-snapshot/<snap_id>', methods=['DELETE'])
def delete_btrfs_snapshot(snap_id):
    """Delete a btrfs snapshot."""
    if not re.match(r'^snap_\d{8}_\d{6}$', snap_id):
        return jsonify({'error': 'Invalid snapshot ID'}), 400
    snap_path = os.path.join(BTRFS_SNAPSHOTS_MOUNT, snap_id)
    if not os.path.isdir(snap_path):
        return jsonify({'error': 'Snapshot not found'}), 404

    r = host_run(f'btrfs subvolume delete {q(snap_path)}', timeout=30)
    if r.returncode != 0:
        return jsonify({'error': f'Delete failed: {r.stderr[:200]}'}), 500

    logger.info("Btrfs snapshot deleted: %s", snap_id)
    return jsonify({'ok': True})


@backup_bp.route('/btrfs-snapshot/<snap_id>/rollback', methods=['POST'])
def rollback_btrfs_snapshot(snap_id):
    """Rollback data partition to a btrfs snapshot.

    This replaces @data with the snapshot content. The current @data
    is renamed to @data.replaced. Requires a service restart.
    """
    if not re.match(r'^snap_\d{8}_\d{6}$', snap_id):
        return jsonify({'error': 'Invalid snapshot ID'}), 400
    if not _ensure_snapshots_mounted():
        return jsonify({'error': 'Cannot mount @snapshots'}), 500

    snap_path = os.path.join(BTRFS_SNAPSHOTS_MOUNT, snap_id)
    if not os.path.isdir(snap_path):
        return jsonify({'error': 'Snapshot not found'}), 404

    # Find the btrfs device
    r = host_run(f"findmnt -n -o SOURCE {q(BTRFS_DATA_MOUNT)}", timeout=5)
    if r.returncode != 0:
        return jsonify({'error': 'Cannot find data device'}), 500
    dev = r.stdout.strip().split('[')[0]

    # Mount top-level btrfs (subvolid=5) to manipulate subvolumes
    top_mount = '/tmp/btrfs-rollback'
    os.makedirs(top_mount, exist_ok=True)
    try:
        host_run(f'umount {q(top_mount)} 2>/dev/null', timeout=10)
        r = host_run(f'mount -o subvolid=5 {q(dev)} {q(top_mount)}', timeout=15)
        if r.returncode != 0:
            return jsonify({'error': f'Cannot mount top-level btrfs: {r.stderr[:200]}'}), 500

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        data_old = os.path.join(top_mount, f'@data.replaced.{ts}')
        data_new = os.path.join(top_mount, '@data')
        snap_src = os.path.join(top_mount, f'@snapshots/{snap_id}')

        # Unmount @data before rename
        host_run(f'umount {q(BTRFS_DATA_MOUNT)} 2>/dev/null', timeout=15)

        # Rename current @data
        r = host_run(f'mv {q(data_new)} {q(data_old)}', timeout=15)
        if r.returncode != 0:
            # Try to remount and bail
            host_run(f'mount -o subvol=@data,noatime,compress=zstd:3 {q(dev)} {q(BTRFS_DATA_MOUNT)}', timeout=15)
            return jsonify({'error': f'Cannot rename @data: {r.stderr[:200]}'}), 500

        # Create writable snapshot from the read-only backup
        r = host_run(f'btrfs subvolume snapshot {q(snap_src)} {q(data_new)}', timeout=30)
        if r.returncode != 0:
            # Restore original
            host_run(f'mv {q(data_old)} {q(data_new)}', timeout=15)
            host_run(f'mount -o subvol=@data,noatime,compress=zstd:3 {q(dev)} {q(BTRFS_DATA_MOUNT)}', timeout=15)
            return jsonify({'error': f'Snapshot restore failed: {r.stderr[:200]}'}), 500

        # Remount the new @data
        r = host_run(f'mount -o subvol=@data,noatime,compress=zstd:3 {q(dev)} {q(BTRFS_DATA_MOUNT)}', timeout=15)

        # Clean up old @data (in background — may take time)
        host_run(f'btrfs subvolume delete {q(data_old)} 2>/dev/null', timeout=60)

        logger.info("Btrfs rollback to %s complete", snap_id)
        _emit('snapshot_rollback', {'snapshot': snap_id})

    finally:
        host_run(f'umount {q(top_mount)} 2>/dev/null', timeout=10)

    return jsonify({'ok': True, 'message': f'Rolled back to {snap_id}. Restart EthOS to apply.'})


@backup_bp.route('/btrfs-info', methods=['GET'])
def btrfs_info():
    """Return btrfs filesystem info for the data partition."""
    if not _btrfs_available():
        return jsonify({'ok': False, 'btrfs': False})
    r = host_run(f'btrfs filesystem usage -b {q(BTRFS_DATA_MOUNT)}', timeout=10)
    info = {'ok': True, 'btrfs': True, 'raw': r.stdout if r.returncode == 0 else ''}
    # Parse key metrics
    for line in (r.stdout or '').splitlines():
        line = line.strip()
        if line.startswith('Device size:'):
            info['device_size'] = int(''.join(c for c in line.split(':')[1] if c.isdigit()) or 0)
        elif line.startswith('Used:'):
            info['used'] = int(''.join(c for c in line.split(':')[1] if c.isdigit()) or 0)
        elif line.startswith('Free (estimated):'):
            val = line.split(':')[1].strip().split()[0]
            info['free'] = int(''.join(c for c in val if c.isdigit()) or 0)
    return jsonify(info)


@backup_bp.route('/snapshots', methods=['GET'])
def list_snapshots():
    """List all system snapshots (local + received from other NAS devices)."""
    snapshots = []
    if os.path.isdir(SNAPSHOTS_DIR):
        for name in sorted(os.listdir(SNAPSHOTS_DIR), reverse=True):
            snap_dir = os.path.join(SNAPSHOTS_DIR, name)
            if not os.path.isdir(snap_dir):
                continue
            meta_file = os.path.join(snap_dir, 'meta.json')
            if os.path.isfile(meta_file):
                try:
                    with open(meta_file, 'r') as f:
                        meta = json.load(f)
                    # Calculate total size
                    total_size = 0
                    for root, dirs, files in os.walk(snap_dir):
                        for fn in files:
                            total_size += os.path.getsize(os.path.join(root, fn))
                    meta['size'] = total_size
                    meta['dir'] = name
                    meta['received'] = False
                    snapshots.append(meta)
                except Exception:
                    pass

    # Also include received snapshots from other NAS devices
    try:
        received = _find_received_snapshot_dirs()
        for r in received:
            r['received'] = True
            snapshots.append(r)
    except Exception:
        logger.exception("Error scanning received snapshots for main list")

    # Sort all by creation date, newest first
    snapshots.sort(key=lambda s: s.get('created', ''), reverse=True)
    return jsonify({'snapshots': snapshots})


@backup_bp.route('/snapshots/status', methods=['GET'])
def snapshot_status():
    """Current snapshot operation status."""
    return jsonify(_snapshot_state)


@backup_bp.route('/snapshots/reset', methods=['POST'])
def reset_snapshot_state():
    """Manually reset a stuck snapshot operation state."""
    old_status = _snapshot_state['status']
    _snapshot_state['status'] = 'idle'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = ''
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = 0
    logger.info(f"Snapshot state manually reset from '{old_status}' to 'idle'")
    return jsonify({'ok': True, 'previous_status': old_status})


@backup_bp.route('/snapshots', methods=['POST'])
def create_snapshot():
    """Create a new system snapshot."""
    # Detect stuck state: if operation running for more than 30 minutes, auto-reset
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        elapsed = time.time() - _snapshot_state.get('_started', 0)
        if elapsed > 1800:  # 30 minutes
            logger.warning(f"Snapshot state '{_snapshot_state['status']}' stuck for {elapsed:.0f}s — auto-resetting")
            _snapshot_state['status'] = 'idle'
        else:
            return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    label = data.get('label', '').strip()
    include_docker = data.get('include_docker', True)
    include_volumes = data.get('include_volumes', True)
    include_ethos = data.get('include_ethos', True)
    include_system = data.get('include_system', True)
    include_userdirs = data.get('include_userdirs', True)
    include_vms = data.get('include_vms', False)
    include_models = data.get('include_models', False)
    dest_type = data.get('dest_type', 'local')  # 'local' or 'usb'
    dest_path = data.get('dest_path', '')

    _snapshot_state['status'] = 'creating'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam...'
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = time.time()

    if _backup_mod._socketio:
        _backup_mod._socketio.start_background_task(
            _create_snapshot_worker,
            label, include_docker, include_volumes, include_ethos, include_system,
            dest_type, dest_path, include_vms, include_models, include_userdirs
        )
    else:
        logger.error("SocketIO not initialized — cannot start snapshot worker")
        _snapshot_state['status'] = 'error'
        _snapshot_state['message'] = 'Internal error: SocketIO not initialized'
    return jsonify({'status': 'ok'})


@backup_bp.route('/snapshots/<snap_id>/browse', methods=['GET'])
def browse_snapshot(snap_id):
    """Browse the contents of a snapshot directory."""
    if not re.match(r'^snap_\d{8}_\d{6}$', snap_id):
        return jsonify({'error': 'Invalid snapshot identifier'}), 400
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404
    tree = []
    for root, dirs, files in os.walk(snap_dir):
        rel = os.path.relpath(root, snap_dir)
        if rel == '.':
            rel = ''
        for d in sorted(dirs):
            tree.append({'name': os.path.join(rel, d) if rel else d, 'type': 'dir', 'size': 0})
        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            tree.append({'name': os.path.join(rel, f) if rel else f, 'type': 'file', 'size': sz})
    return jsonify({'tree': tree, 'snap_id': snap_id})


@backup_bp.route('/snapshots/space', methods=['GET'])
def snapshot_space():
    """Get total space used by snapshots and disk free space."""
    total_size = 0
    count = 0
    if os.path.isdir(SNAPSHOTS_DIR):
        for name in os.listdir(SNAPSHOTS_DIR):
            d = os.path.join(SNAPSHOTS_DIR, name)
            if os.path.isdir(d):
                count += 1
                for root, dirs, files in os.walk(d):
                    for f in files:
                        try:
                            total_size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
    try:
        st = os.statvfs(SNAPSHOTS_DIR)
        disk_free = st.f_bavail * st.f_frsize
        disk_total = st.f_blocks * st.f_frsize
    except Exception:
        disk_free = 0
        disk_total = 0
    return jsonify({
        'snapshot_bytes': total_size,
        'snapshot_count': count,
        'disk_free': disk_free,
        'disk_total': disk_total,
    })


@backup_bp.route('/snapshots/<snap_id>', methods=['DELETE'])
def delete_snapshot(snap_id):
    """Delete a snapshot."""
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404
    shutil.rmtree(snap_dir, ignore_errors=True)
    return jsonify({'ok': True})


@backup_bp.route('/snapshots/<snap_id>/restore', methods=['POST'])
def restore_snapshot(snap_id):
    """Restore system from a snapshot."""
    if _snapshot_state['status'] in ('creating', 'restoring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404

    data = request.json or {}
    restore_docker = data.get('restore_docker', True)
    restore_volumes = data.get('restore_volumes', True)
    restore_ethos = data.get('restore_ethos', True)
    restore_system = data.get('restore_system', True)

    _snapshot_state['status'] = 'restoring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam przywracanie...'
    _snapshot_state['log'] = []

    if _backup_mod._socketio:
        _backup_mod._socketio.start_background_task(
            _restore_snapshot_worker,
            snap_dir, restore_docker, restore_volumes, restore_ethos, restore_system
        )
    return jsonify({'status': 'ok'})


@backup_bp.route('/snapshots/<snap_id>/download')
def download_snapshot(snap_id):
    """Download snapshot as tar.gz."""
    from flask import send_file as _send
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404
    archive = os.path.join(SNAPSHOTS_DIR, f'{snap_id}.tar.gz')
    if not os.path.isfile(archive):
        subprocess.run(['tar', '-czf', archive, '-C', SNAPSHOTS_DIR, snap_id],
                       capture_output=True, timeout=600)
    if os.path.isfile(archive):
        return _send(archive, as_attachment=True, download_name=f'snapshot-{snap_id}.tar.gz')
    return jsonify({'error': 'Failed to compress'}), 500


# ── NAS-to-NAS snapshot transfer ──

@backup_bp.route('/snapshots/<snap_id>/transfer', methods=['POST'])
def transfer_snapshot(snap_id):
    """Transfer (push) a snapshot to a remote NAS via SSH/SCP."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko/scp not installed'}), 500
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404

    data = request.json or {}
    server_id = data.get('server_id')
    if not server_id:
        return jsonify({'error': 'No server selected'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'SSH server not found'}), 404

    _snapshot_state['status'] = 'transferring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam transfer...'
    _snapshot_state['log'] = []

    if _backup_mod._socketio:
        _backup_mod._socketio.start_background_task(
            _transfer_snapshot_worker, snap_id, snap_dir, srv
        )
    return jsonify({'status': 'ok'})


def _ssh_resolve_remote_path(ssh, raw_path):
    """Resolve ~, $HOME, etc. in remote path and return absolute path."""
    try:
        # Let the remote shell expand ~ and $HOME
        _, so, _ = ssh.exec_command(f'eval echo {raw_path}')
        resolved = so.read().decode().strip()
        if resolved and resolved != '' and not resolved.startswith('$'):
            return resolved
    except Exception:
        pass
    # Fallback: resolve $HOME manually
    home = _ssh_resolve_home(ssh)
    if home:
        return raw_path.replace('~', home).replace('$HOME', home)
    return raw_path


def _transfer_snapshot_worker(snap_id, snap_dir, ssh_config):
    """Background: tar snapshot, SCP to remote, optionally extract."""
    try:
        _snap_update(percent=5, message='Packing snapshot...', log=f'Archiving {snap_id}...')

        archive = os.path.join(SNAPSHOTS_DIR, f'{snap_id}.tar.gz')
        if not os.path.isfile(archive):
            r = subprocess.run(
                ['tar', '-czf', archive, '-C', SNAPSHOTS_DIR, snap_id],
                capture_output=True, text=True, timeout=600
            )
            if r.returncode != 0:
                _snap_update(status='error', message=f'Archive error: {r.stderr[:200]}')
                return

        arc_size = os.path.getsize(archive)
        _snap_update(percent=15, message='Connecting to server...',
                     log=f'Archiwum: {arc_size / 1048576:.1f} MB')

        ssh = _get_ssh_client(ssh_config['host'], ssh_config.get('port', 22),
                              ssh_config['username'],
                              password=ssh_config.get('password'),
                              key_path=ssh_config.get('key_path'), timeout=30)
        _snap_update(percent=20, message='Connected, preparing...', log=f'SSH → {ssh_config["host"]}')

        # Resolve the remote path (handle ~, $HOME, relative paths)
        raw_remote_path = ssh_config.get('remote_path', '~/backups')
        remote_path = _ssh_resolve_remote_path(ssh, raw_remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'

        # Create remote directory and verify it's writable
        writable, _ = _ssh_ensure_writable_dir(ssh, remote_snap_dir)

        if not writable:
            _snap_update(log=f'No write permissions for {remote_snap_dir} — searching for alternative path...')
            # Try $HOME/backups/snapshots as fallback
            remote_home = _ssh_resolve_home(ssh)
            fallback_paths = []
            if remote_home:
                fallback_paths.append(f'{remote_home}/backups/snapshots')
            fallback_paths.append('/tmp/ethos-snapshots')

            found_writable = False
            for fb_path in fallback_paths:
                fb_ok, _ = _ssh_ensure_writable_dir(ssh, fb_path)
                if fb_ok:
                    remote_snap_dir = fb_path
                    _snap_update(log=f'Using path: {remote_snap_dir}')
                    found_writable = True
                    break

            if not found_writable:
                _snap_update(status='error',
                             message=f'No write permissions on remote server. Check path: {raw_remote_path}',
                             log=f'ERROR: Cannot write to {remote_snap_dir} or fallback paths')
                ssh.close()
                return

        _snap_update(percent=22, message='Uploading...', log=f'Target directory: {remote_snap_dir}')

        remote_file = f'{remote_snap_dir}/{snap_id}.tar.gz'

        def scp_progress(filename, size, sent):
            pct = int(22 + (sent / max(size, 1)) * 58)
            _snap_update(
                percent=pct,
                message=f'Uploading... {sent / 1048576:.0f}/{size / 1048576:.0f} MB'
            )

        with SCPClient(ssh.get_transport(), progress=scp_progress) as scp:
            scp.put(archive, remote_file)

        _snap_update(percent=85, message='Extracting on remote server...',
                     log='Transfer complete, extracting...')

        # Extract on remote
        cmd = f'cd {remote_snap_dir} && tar -xzf {snap_id}.tar.gz && rm -f {snap_id}.tar.gz'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=300)
        exit_code = stdout_.channel.recv_exit_status()
        if exit_code == 0:
            _snap_update(log='Extracted on remote server ✓')
        else:
            err_txt = stderr_.read().decode()[:200]
            _snap_update(log=f'Extraction: code {exit_code} — {err_txt}')

        ssh.close()

        _snap_update(status='done', percent=100,
                     message=f'Snapshot transferred to {ssh_config["host"]}:{remote_snap_dir}')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Transfer error: {e}',
                     log=f'EXCEPTION: {e}')
        logger.exception("Snapshot transfer failed")


@backup_bp.route('/snapshots/import', methods=['POST'])
def import_snapshot():
    """Import snapshot from uploaded tar.gz file."""
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file'}), 400

    if not f.filename.endswith('.tar.gz'):
        return jsonify({'error': 'Wymagany plik .tar.gz'}), 400

    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

    # Save uploaded file to temp location
    tmp_archive = os.path.join(SNAPSHOTS_DIR, f'_import_{secrets.token_hex(4)}.tar.gz')
    try:
        f.save(tmp_archive)
        _snap_update(status='creating', percent=30,
                     message='Extracting import...', log=['File received, extracting...'])

        # Extract
        r = subprocess.run(
            ['tar', '-xzf', tmp_archive, '-C', SNAPSHOTS_DIR],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            _snap_update(status='error', message=f'Extraction error: {r.stderr[:200]}')
            return jsonify({'error': f'Extraction error: {r.stderr[:200]}'}), 500

        # Find extracted snapshot (directory with meta.json)
        imported_name = None
        for entry in os.listdir(SNAPSHOTS_DIR):
            entry_path = os.path.join(SNAPSHOTS_DIR, entry)
            if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, 'meta.json')):
                # Check if this is new (not in our known list before import)
                meta_file = os.path.join(entry_path, 'meta.json')
                try:
                    with open(meta_file, 'r') as mf:
                        m = json.load(mf)
                    imported_name = entry
                except Exception:
                    pass

        if not imported_name:
            _snap_update(status='error', message='Snapshot not found in archive')
            return jsonify({'error': 'Snapshot not found in archive'}), 400

        _snap_update(status='done', percent=100,
                     message=f'Snapshot "{imported_name}" imported!')
        return jsonify({'ok': True, 'snapshot': imported_name})

    except Exception as e:
        _snap_update(status='error', message=f'Import error: {e}')
        return jsonify({'error': str(e)}), 500
    finally:
        # Cleanup temp archive
        if os.path.isfile(tmp_archive):
            os.remove(tmp_archive)


# ── Received snapshots (from other NAS devices) ──

def _find_received_snapshot_dirs():
    """
    Scan common directories where snapshots from other NAS devices
    might have been received via SSH transfer.
    Returns list of (base_dir, snap_name, meta) for directories
    that contain valid snapshots and are NOT in the local SNAPSHOTS_DIR.
    """
    local_real = os.path.realpath(SNAPSHOTS_DIR)
    scan_paths = set()

    # 1) User home + /backups/snapshots (default ~/backups target)
    home = os.path.expanduser('~')
    scan_paths.add(os.path.join(home, 'backups', 'snapshots'))

    # 2) /backups/snapshots (if someone used /backups as remote_path)
    scan_paths.add('/backups/snapshots')

    # 3) /tmp/ethos-snapshots (fallback path used by transfer code)
    scan_paths.add('/tmp/ethos-snapshots')

    # 4) Check all home directories for /backups/snapshots
    try:
        for entry in os.scandir('/home'):
            if entry.is_dir():
                p = os.path.join(entry.path, 'backups', 'snapshots')
                scan_paths.add(p)
    except Exception:
        pass

    # 5) Check EthOS data paths (common on EthOS installs)
    for base in ['/data', '/media']:
        try:
            for root, dirs, files in os.walk(base, topdown=True):
                # Look max 4 levels deep
                depth = root.replace(base, '').count(os.sep)
                if depth > 4:
                    dirs.clear()
                    continue
                if os.path.basename(root) == 'snapshots':
                    parent = os.path.dirname(root)
                    if os.path.basename(parent) == 'backups':
                        scan_paths.add(root)
                    dirs.clear()
        except Exception:
            pass

    results = []
    seen_snap_ids = set()

    for scan_dir in scan_paths:
        if not os.path.isdir(scan_dir):
            continue
        # Skip if this IS the local SNAPSHOTS_DIR
        if os.path.realpath(scan_dir) == local_real:
            continue

        try:
            for name in os.listdir(scan_dir):
                snap_path = os.path.join(scan_dir, name)
                if not os.path.isdir(snap_path):
                    continue
                meta_file = os.path.join(snap_path, 'meta.json')
                if not os.path.isfile(meta_file):
                    continue
                # Skip if same name already exists in local SNAPSHOTS_DIR
                if os.path.isdir(os.path.join(SNAPSHOTS_DIR, name)):
                    continue
                # Avoid duplicates across scan paths
                if name in seen_snap_ids:
                    continue
                seen_snap_ids.add(name)
                try:
                    with open(meta_file, 'r') as f:
                        meta = json.load(f)
                    # Calculate size
                    total_size = 0
                    for root, dirs, files in os.walk(snap_path):
                        for fn in files:
                            try:
                                total_size += os.path.getsize(os.path.join(root, fn))
                            except OSError:
                                pass
                    meta['size'] = total_size
                    meta['dir'] = name
                    meta['source_path'] = snap_path
                    meta['received'] = True
                    results.append(meta)
                except Exception:
                    pass
        except PermissionError:
            pass

    results.sort(key=lambda s: s.get('created', ''), reverse=True)
    return results


@backup_bp.route('/snapshots/received', methods=['GET'])
def list_received_snapshots():
    """List snapshots received from other NAS devices (not in local SNAPSHOTS_DIR)."""
    try:
        snaps = _find_received_snapshot_dirs()
        return jsonify({'snapshots': snaps})
    except Exception as e:
        logger.exception("Error scanning received snapshots")
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/received/adopt', methods=['POST'])
def adopt_received_snapshot():
    """Copy/move a received snapshot into local SNAPSHOTS_DIR so it appears in the normal list."""
    data = request.json or {}
    source_path = data.get('source_path')
    snap_dir_name = data.get('snap_id')
    if not source_path or not snap_dir_name:
        return jsonify({'error': 'source_path and snap_id required'}), 400

    # Validate the source path contains a valid snapshot
    meta_file = os.path.join(source_path, 'meta.json')
    if not os.path.isfile(meta_file):
        return jsonify({'error': 'Invalid snapshot path'}), 404

    local_dest = os.path.join(SNAPSHOTS_DIR, snap_dir_name)
    if os.path.exists(local_dest):
        return jsonify({'error': 'Snapshot with this name already exists locally'}), 409

    try:
        # Copy the snapshot directory into local SNAPSHOTS_DIR
        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        shutil.copytree(source_path, local_dest)
        return jsonify({'status': 'ok', 'snapshot': snap_dir_name})
    except Exception as e:
        logger.exception("Error adopting received snapshot")
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/received/restore', methods=['POST'])
def restore_received_snapshot():
    """Restore from a received snapshot (in-place, without copying to SNAPSHOTS_DIR first)."""
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    source_path = data.get('source_path')
    if not source_path:
        return jsonify({'error': 'source_path required'}), 400

    meta_file = os.path.join(source_path, 'meta.json')
    if not os.path.isfile(meta_file):
        return jsonify({'error': 'Invalid snapshot path'}), 404

    restore_ethos = data.get('restore_ethos', True)
    restore_system = data.get('restore_system', True)
    restore_docker = data.get('restore_docker', True)
    restore_volumes = data.get('restore_volumes', True)

    _snapshot_state['status'] = 'restoring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Restoring from received snapshot...'
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = time.time()

    if _backup_mod._socketio:
        _backup_mod._socketio.start_background_task(
            _restore_snapshot_worker, source_path,
            restore_docker, restore_volumes, restore_ethos, restore_system
        )
    return jsonify({'status': 'ok'})


@backup_bp.route('/snapshots/remote', methods=['POST'])
def list_remote_snapshots():
    """List snapshots on a remote NAS (via SSH)."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko not installed'}), 500

    data = request.json or {}
    server_id = data.get('server_id')
    if not server_id:
        return jsonify({'error': 'No server selected'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'SSH server not found'}), 404

    try:
        ssh = _get_ssh_client(srv['host'], srv.get('port', 22),
                              srv['username'],
                              password=srv.get('password'),
                              key_path=srv.get('key_path'), timeout=15)

        remote_path = srv.get('remote_path', '~/backups')
        # Resolve ~ and $HOME on the remote
        remote_path = _ssh_resolve_remote_path(ssh, remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'

        # Read meta.json from each snapshot directory
        cmd = f'for d in {remote_snap_dir}/snap_*/; do [ -f "$d/meta.json" ] && echo "---" && cat "$d/meta.json" && echo "|||$(basename $d)|||$(du -sb $d | cut -f1)"; done 2>/dev/null'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=30)
        stdout_.channel.recv_exit_status()
        output = stdout_.read().decode()
        ssh.close()

        snapshots = []
        for block in output.split('---'):
            block = block.strip()
            if not block:
                continue
            # Parse: JSON content then |||dirname|||size
            parts = block.rsplit('|||', 2)
            if len(parts) >= 3:
                json_str = parts[0].strip()
                snap_dirname = parts[1]
                snap_size = int(parts[2]) if parts[2].isdigit() else 0
                try:
                    meta = json.loads(json_str)
                    meta['dir'] = snap_dirname
                    meta['size'] = snap_size
                    meta['remote'] = True
                    snapshots.append(meta)
                except Exception:
                    pass

        snapshots.sort(key=lambda s: s.get('created', ''), reverse=True)
        return jsonify({'snapshots': snapshots, 'host': srv['host']})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/pull', methods=['POST'])
def pull_remote_snapshot():
    """Pull (download) a snapshot from remote NAS to local."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko/scp not installed'}), 500
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    server_id = data.get('server_id')
    remote_snap_id = data.get('snap_id')
    if not server_id or not remote_snap_id:
        return jsonify({'error': 'server_id and snap_id required'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'SSH server not found'}), 404

    _snapshot_state['status'] = 'transferring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Downloading from remote NAS...'
    _snapshot_state['log'] = []

    if _backup_mod._socketio:
        _backup_mod._socketio.start_background_task(
            _pull_snapshot_worker, remote_snap_id, srv
        )
    return jsonify({'status': 'ok'})


def _pull_snapshot_worker(snap_id, ssh_config):
    """Background: SCP snapshot from remote NAS to local."""
    tmp_archive = None
    try:
        _snap_update(percent=5, message='Connecting to server...', log=f'Connecting to {ssh_config["host"]}...')

        ssh = _get_ssh_client(ssh_config['host'], ssh_config.get('port', 22),
                              ssh_config['username'],
                              password=ssh_config.get('password'),
                              key_path=ssh_config.get('key_path'), timeout=30)

        remote_path = ssh_config.get('remote_path', '~/backups')
        # Resolve ~ and $HOME on the remote
        remote_path = _ssh_resolve_remote_path(ssh, remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'
        remote_archive = f'/tmp/_ethos_pull_{snap_id}.tar.gz'

        # Archive snapshot on remote
        _snap_update(percent=10, message='Packing on remote server...',
                     log='Archiving on remote...')
        cmd = f'tar -czf {remote_archive} -C {remote_snap_dir} {snap_id}'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=600)
        exit_code = stdout_.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr_.read().decode()[:200]
            _snap_update(status='error', message=f'Packing error: {err}')
            ssh.close()
            return

        _snap_update(percent=20, message='Downloading...',
                     log='Transfer from remote server...')

        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        tmp_archive = os.path.join(SNAPSHOTS_DIR, f'_pull_{snap_id}.tar.gz')

        def scp_progress(filename, size, sent):
            pct = int(20 + (sent / max(size, 1)) * 60)
            _snap_update(
                percent=pct,
                message=f'Downloading... {sent / 1048576:.0f}/{size / 1048576:.0f} MB'
            )

        with SCPClient(ssh.get_transport(), progress=scp_progress) as scp:
            scp.get(remote_archive, tmp_archive)

        # Clean up remote archive
        ssh.exec_command(f'rm -f {q(remote_archive)}')
        ssh.close()

        _snap_update(percent=85, message='Extracting...',
                     log='Extracting snapshot...')

        r = subprocess.run(
            ['tar', '-xzf', tmp_archive, '-C', SNAPSHOTS_DIR],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            _snap_update(status='error', message=f'Extraction error: {r.stderr[:200]}')
            return

        _snap_update(status='done', percent=100,
                     message=f'Snapshot {snap_id} downloaded from remote NAS!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Error: {e}',
                     log=f'EXCEPTION: {e}')
        logger.exception("Snapshot pull failed")
    finally:
        if tmp_archive and os.path.isfile(tmp_archive):
            os.remove(tmp_archive)


# ── NAS discovery on LAN ──

def _get_local_ip():
    """Best-effort: get this machine's LAN IP."""
    import socket as _sock
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _discover_via_avahi():
    """Use avahi-browse to find _ethos._tcp services on LAN."""
    devices = []
    try:
        r = subprocess.run(
            ['avahi-browse', '-ptr', '_ethos._tcp', 'local', '--resolve', '-l'],
            capture_output=True, text=True, timeout=8
        )
        # Parse avahi-browse output:
        # =;eth0;IPv4;NAS Name;_ethos._tcp;local;hostname.local;192.168.50.100;9000;"name=X" "version=Y"
        for line in r.stdout.splitlines():
            if not line.startswith('='):
                continue
            parts = line.split(';')
            if len(parts) < 9:
                continue
            name = parts[3]
            ip = parts[7]
            port = int(parts[8]) if parts[8].isdigit() else 9000
            # Parse TXT records
            txt = {}
            for tp in parts[9:]:
                tp = tp.strip().strip('"')
                if '=' in tp:
                    k, v = tp.split('=', 1)
                    txt[k] = v
            devices.append({
                'name': txt.get('name', name),
                'hostname': parts[6].rstrip('.'),
                'ip': ip,
                'port': port,
                'version': txt.get('version', '?'),
                'source': 'mdns',
            })
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass
    return devices


def _discover_via_scan(port=9000, timeout=2.0):
    """Scan local /24 subnet on given port for EthOS identify endpoint."""
    import socket as _sock
    from concurrent.futures import ThreadPoolExecutor, as_completed

    my_ip = _get_local_ip()
    if my_ip == '127.0.0.1':
        return []

    prefix = '.'.join(my_ip.split('.')[:3])
    devices = []

    def _probe(ip):
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(timeout)
            result = s.connect_ex((ip, port))
            s.close()
            if result != 0:
                return None
            # Port open — try identify endpoint
            import urllib.request
            url = f'http://{ip}:{port}/api/ethos/identify'
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read().decode())
            if data.get('ethos'):
                return {
                    'name': data.get('name', '?'),
                    'hostname': data.get('hostname', '?'),
                    'ip': ip,
                    'port': data.get('port', port),
                    'version': data.get('version', '?'),
                    'source': 'scan',
                }
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(_probe, f'{prefix}.{i}'): i for i in range(1, 255)}
        try:
            for fut in as_completed(futures, timeout=timeout + 5):
                try:
                    res = fut.result(timeout=0.5)
                    if res:
                        devices.append(res)
                except Exception:
                    pass
        except (TimeoutError, FuturesTimeoutError):
            # Some hosts didn't respond in time — that's OK
            pass

    return devices


def _discover_via_vms():
    """Discover EthOS instances running inside local QEMU VMs.

    Reads VM Manager state to find VMs with port forwards mapping to
    guest port 9000 (EthOS). Probes localhost:{host_port} to verify
    the instance is live.
    """
    import urllib.request
    vm_state_file = os.path.join(
        os.path.dirname(__file__), '..', '..', 'data', 'vm_state.json'
    )
    if not os.path.isfile(vm_state_file):
        return []

    try:
        with open(vm_state_file, 'r') as f:
            vms = json.load(f)
    except Exception:
        return []

    devices = []
    for vm_id, vm in vms.items():
        net = vm.get('network') or {}
        if net.get('net_type') not in ('user', None):
            continue
        for rule in net.get('port_forwards', []):
            if rule.get('guest') != 9000 or rule.get('proto', 'tcp') != 'tcp':
                continue
            host_port = rule.get('host', 0)
            if not host_port:
                continue
            try:
                url = f'http://127.0.0.1:{host_port}/api/ethos/identify'
                req = urllib.request.Request(url, headers={'Accept': 'application/json'})
                resp = urllib.request.urlopen(req, timeout=2)
                data = json.loads(resp.read().decode())
                if data.get('ethos'):
                    devices.append({
                        'name': data.get('name', vm.get('name', vm_id)),
                        'hostname': data.get('hostname', vm.get('name', vm_id)),
                        'ip': '127.0.0.1',
                        'port': host_port,
                        'version': data.get('version', '?'),
                        'source': 'vm',
                        'vm_id': vm_id,
                        'vm_name': vm.get('name', vm_id),
                    })
            except Exception:
                pass
    return devices


@backup_bp.route('/discover-nas', methods=['POST'])
def discover_nas():
    """Discover other EthOS instances on the local network."""
    my_ip = _get_local_ip()

    # Method 1: Avahi mDNS
    devices = _discover_via_avahi()

    # Method 2: Subnet scan fallback / supplement
    scanned = _discover_via_scan()

    # Method 3: Local QEMU VMs with port-forwarded EthOS
    vm_devices = _discover_via_vms()

    # Merge: deduplicate by IP:port
    known = {(d['ip'], d.get('port', 9000)) for d in devices}
    for sd in scanned:
        key = (sd['ip'], sd.get('port', 9000))
        if key not in known:
            devices.append(sd)
            known.add(key)
    for vd in vm_devices:
        key = (vd['ip'], vd.get('port', 9000))
        if key not in known:
            devices.append(vd)
            known.add(key)

    # Filter out self
    my_port = int(os.environ.get('ETHOS_PORT', 9000))
    devices = [d for d in devices
               if not (d['ip'] == my_ip and d.get('port', 9000) == my_port)]

    return jsonify({'devices': devices, 'my_ip': my_ip})


# ── Snapshot creation worker ──

def _create_snapshot_worker(label, include_docker, include_volumes,
                             include_ethos, include_system, dest_type, dest_path,
                             include_vms=False, include_models=False, include_userdirs=True):
    """Background task: creates a full system snapshot."""
    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        snap_name = f'snap_{ts}'
        snap_dir = os.path.join(SNAPSHOTS_DIR, snap_name)
        os.makedirs(snap_dir, exist_ok=True)

        meta = {
            'id': snap_name,
            'label': label or f'Snapshot {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            'created': datetime.now().isoformat(),
            'hostname': _read_file_safe('/etc/hostname', 'unknown').strip(),
            'includes': {
                'ethos': include_ethos,
                'docker': include_docker,
                'volumes': include_volumes,
                'system': include_system,
                'userdirs': include_userdirs,
                'vms': include_vms,
                'models': include_models,
            },
            'docker_projects': [],
            'docker_volumes': [],
            'docker_containers': 0,
        }

        steps_total = sum([include_ethos, include_system, include_userdirs, include_docker, include_volumes])
        step_i = 0

        # ── 1. EthOS config & data ──
        if include_ethos:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up EthOS...', log='Copying EthOS configuration...')

            ethos_root = os.environ.get('ETHOS_ROOT', f'/home/{get_ethos_user()}/docker/nasos')
            ethos_dir = os.path.join(snap_dir, 'ethos')
            os.makedirs(ethos_dir, exist_ok=True)

            # ethos.env
            env_file = os.path.join(ethos_root, 'ethos.env')
            if os.path.isfile(env_file):
                shutil.copy2(env_file, os.path.join(ethos_dir, 'ethos.env'))

            # data/ (settings, configs, profiles DB, etc.)
            # Optionally exclude large dirs based on user choices
            data_src = os.path.join(ethos_root, 'data')
            if os.path.isdir(data_src):
                exclude_dirs = ['novnc', 'updates']
                if not include_vms:
                    exclude_dirs.append('vms')
                if not include_models:
                    exclude_dirs.append('models')
                tar_cmd = ['tar', '-czf', os.path.join(ethos_dir, 'data.tar.gz'),
                           '-C', ethos_root]
                for ed in exclude_dirs:
                    tar_cmd.extend(['--exclude', f'data/{ed}'])
                tar_cmd.append('data')
                subprocess.run(tar_cmd, capture_output=True, timeout=3600)
                excluded_note = ', '.join(exclude_dirs) if exclude_dirs else 'none'
                _snap_update(log=f'EthOS data/ — excluding: {excluded_note}')

            # install.conf
            for extra in ['install.conf']:
                src = os.path.join(ethos_root, extra)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(ethos_dir, extra))

            _snap_update(log='EthOS — done')

        # ── 2. System configs ──
        if include_system:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up system configuration...', log='Copying system configuration...')

            sys_dir = os.path.join(snap_dir, 'system')
            os.makedirs(sys_dir, exist_ok=True)

            # Hostname, hosts, timezone, locale
            for f in ['/etc/hostname', '/etc/hosts', '/etc/timezone',
                       '/etc/default/locale', '/etc/resolv.conf']:
                if os.path.isfile(f):
                    dest = os.path.join(sys_dir, os.path.basename(f))
                    try:
                        shutil.copy2(f, dest)
                    except Exception:
                        pass

            # Timezone (symlink)
            try:
                tz = os.path.realpath('/etc/localtime')
                with open(os.path.join(sys_dir, 'timezone_link'), 'w') as tf:
                    tf.write(tz)
            except Exception:
                pass

            # NetworkManager connections
            nm_dir = '/etc/NetworkManager/system-connections'
            if os.path.isdir(nm_dir):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'nm-connections.tar.gz'),
                     '-C', '/etc/NetworkManager', 'system-connections'],
                    capture_output=True, timeout=30
                )
                _snap_update(log='NetworkManager connections saved')

            # Nginx sites
            nginx_avail = '/etc/nginx/sites-available'
            if os.path.isdir(nginx_avail):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'nginx-sites.tar.gz'),
                     '-C', '/etc/nginx', 'sites-available', 'sites-enabled'],
                    capture_output=True, timeout=30
                )
                _snap_update(log='Nginx sites saved')

            # SSL certs (Let's Encrypt)
            le_dir = '/etc/letsencrypt'
            if os.path.isdir(le_dir):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'letsencrypt.tar.gz'),
                     '-C', '/etc', 'letsencrypt'],
                    capture_output=True, timeout=60
                )
                _snap_update(log='Let\'s Encrypt certs saved')

            # Samba config
            smb_conf = '/etc/samba/smb.conf'
            if os.path.isfile(smb_conf):
                shutil.copy2(smb_conf, os.path.join(sys_dir, 'smb.conf'))
                _snap_update(log='Samba config saved')

            # fstab
            if os.path.isfile('/etc/fstab'):
                shutil.copy2('/etc/fstab', os.path.join(sys_dir, 'fstab'))

            # Crontab
            try:
                r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    with open(os.path.join(sys_dir, 'crontab'), 'w') as cf:
                        cf.write(r.stdout)
            except Exception:
                pass

            _snap_update(log='System config — done')

        # ── 2b. User home directories ──
        if include_userdirs:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up user directories...', log='Archiving /home...')

            users_dir = os.path.join(snap_dir, 'userdirs')
            os.makedirs(users_dir, exist_ok=True)

            try:
                home_entries = [d for d in os.listdir('/home')
                                if os.path.isdir(os.path.join('/home', d))
                                and not d.startswith('.')]
            except OSError:
                home_entries = []

            saved_users = []
            for uname in home_entries:
                user_home = os.path.join('/home', uname)
                _snap_update(log=f'  User: {uname}')
                tar_path = os.path.join(users_dir, f'{uname}.tar.gz')
                # Exclude caches and large temp dirs
                tar_cmd = [
                    'tar', '-czf', tar_path, '-C', '/home',
                    '--exclude', f'{uname}/.cache',
                    '--exclude', f'{uname}/.local/share/Trash',
                    '--exclude', f'{uname}/.npm',
                    '--exclude', f'{uname}/.venv',
                    '--exclude', f'{uname}/venv',
                    uname,
                ]
                try:
                    r = subprocess.run(tar_cmd, capture_output=True, timeout=3600)
                    if r.returncode == 0 or os.path.isfile(tar_path):
                        sz = os.path.getsize(tar_path) if os.path.isfile(tar_path) else 0
                        saved_users.append(uname)
                        _snap_update(log=f'  {uname}: {sz / 1048576:.1f} MB')
                except subprocess.TimeoutExpired:
                    _snap_update(log=f'  {uname}: TIMEOUT (skipped)')
                except Exception as e:
                    _snap_update(log=f'  {uname}: error — {e}')

            meta['user_dirs'] = saved_users
            _snap_update(log=f'User directories: {len(saved_users)} users archived')

        # ── 3. Docker compose projects ──
        if include_docker:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up Docker...', log='Saving Docker projects...')

            docker_dir = os.path.join(snap_dir, 'docker')
            os.makedirs(docker_dir, exist_ok=True)

            if _docker_ok():
                # Compose projects — save compose files + .env
                compose_root = os.environ.get('COMPOSE_ROOT', f'/home/{get_ethos_user()}/docker')
                projects = []
                if os.path.isdir(compose_root):
                    for entry in sorted(os.listdir(compose_root)):
                        proj_path = os.path.join(compose_root, entry)
                        if not os.path.isdir(proj_path):
                            continue
                        compose_file = None
                        for cf_name in ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml']:
                            candidate = os.path.join(proj_path, cf_name)
                            if os.path.isfile(candidate):
                                compose_file = candidate
                                break
                        if not compose_file:
                            continue

                        proj_bak = os.path.join(docker_dir, 'projects', entry)
                        os.makedirs(proj_bak, exist_ok=True)
                        shutil.copy2(compose_file, os.path.join(proj_bak, os.path.basename(compose_file)))
                        # .env file
                        env_f = os.path.join(proj_path, '.env')
                        if os.path.isfile(env_f):
                            shutil.copy2(env_f, os.path.join(proj_bak, '.env'))
                        # Additional env files referenced in compose
                        for extra_env in _find_env_files(compose_file):
                            extra_path = os.path.join(proj_path, extra_env)
                            if os.path.isfile(extra_path):
                                shutil.copy2(extra_path, os.path.join(proj_bak, extra_env))

                        projects.append(entry)
                        _snap_update(log=f'  Project: {entry}')

                meta['docker_projects'] = projects

                # Container list (for reference — what was running)
                out, rc = _docker_cmd(['ps', '-a', '--format', '{{json .}}', '--no-trunc'])
                if rc == 0:
                    containers = []
                    for line in out.split('\n'):
                        line = line.strip()
                        if line:
                            try:
                                containers.append(json.loads(line))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'containers.json'), 'w') as cf:
                        json.dump(containers, cf, indent=2)
                    meta['docker_containers'] = len(containers)
                    _snap_update(log=f'Containers: {len(containers)}')

                # Docker images list
                out, rc = _docker_cmd(['images', '--format', '{{json .}}'])
                if rc == 0:
                    images = []
                    for line in out.split('\n'):
                        line = line.strip()
                        if line:
                            try:
                                images.append(json.loads(line))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'images.json'), 'w') as cf:
                        json.dump(images, cf, indent=2)
                    _snap_update(log=f'Docker images: {len(images)}')

                # Networks
                out, rc = _docker_cmd(['network', 'ls', '--format', '{{json .}}'])
                if rc == 0:
                    networks = []
                    for line in out.split('\n'):
                        if line.strip():
                            try:
                                networks.append(json.loads(line.strip()))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'networks.json'), 'w') as cf:
                        json.dump(networks, cf, indent=2)

            else:
                _snap_update(log='Docker unavailable — skipping')

            _snap_update(log='Docker projects — done')

        # ── 4. Docker volumes ──
        if include_volumes and include_docker:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up Docker volumes...', log='Exporting Docker volumes...')

            vol_dir = os.path.join(snap_dir, 'docker', 'volumes')
            os.makedirs(vol_dir, exist_ok=True)

            if _docker_ok():
                out, rc = _docker_cmd(['volume', 'ls', '-q'])
                vol_names = [v.strip() for v in out.split('\n') if v.strip()] if rc == 0 else []
                saved_vols = []

                for vi, vol in enumerate(vol_names):
                    # Skip very large or internal volumes
                    inspect_out, irc = _docker_cmd(['volume', 'inspect', vol])
                    if irc != 0:
                        continue
                    try:
                        vinfo = json.loads(inspect_out)
                        if isinstance(vinfo, list) and vinfo:
                            vinfo = vinfo[0]
                        mount = vinfo.get('Mountpoint', '')
                    except Exception:
                        mount = ''

                    # Check size — skip if > 5GB
                    vol_size = 0
                    if mount and os.path.isdir(mount):
                        try:
                            r = subprocess.run(
                                ['du', '-sb', mount],
                                capture_output=True, text=True, timeout=10
                            )
                            if r.returncode == 0:
                                vol_size = int(r.stdout.split()[0])
                        except Exception:
                            pass

                    size_mb = vol_size / (1024 * 1024)
                    if vol_size > 5 * 1024 * 1024 * 1024:  # >5GB
                        _snap_update(log=f'  Skipping {vol} ({size_mb:.0f} MB — too large)')
                        continue

                    vol_pct = int(pct + (vi / max(len(vol_names), 1)) * (90 - pct))
                    _snap_update(
                        percent=vol_pct,
                        message=f'Eksport: {vol} ({size_mb:.1f} MB)',
                        log=f'  Volume: {vol} ({size_mb:.1f} MB)'
                    )

                    archive = os.path.join(vol_dir, f'{vol}.tar.gz')
                    # Export volume via docker run
                    result = subprocess.run(
                        ['docker', 'run', '--rm',
                         '-v', f'{vol}:/volume_data:ro',
                         '-v', f'{vol_dir}:/backup',
                         'alpine',
                         'tar', '-czf', f'/backup/{vol}.tar.gz', '-C', '/volume_data', '.'],
                        capture_output=True, text=True, timeout=600
                    )
                    if result.returncode == 0 and os.path.isfile(archive):
                        saved_vols.append(vol)
                    else:
                        _snap_update(log=f'  Export ERROR {vol}: {result.stderr[:200]}')

                meta['docker_volumes'] = saved_vols
                _snap_update(log=f'Volumes: {len(saved_vols)}/{len(vol_names)} exported')
            else:
                _snap_update(log='Docker unavailable — skipping volumes')

        # ── Save metadata ──
        _snap_update(percent=92, message='Saving metadata...')
        with open(os.path.join(snap_dir, 'meta.json'), 'w') as mf:
            json.dump(meta, mf, indent=2)

        # ── Copy to USB if requested ──
        if dest_type == 'usb' and dest_path:
            _snap_update(percent=93, message='Copying to USB...', log=f'USB transfer: {dest_path}')
            usb_snap_dir = os.path.join(dest_path, 'ethos-snapshots')
            os.makedirs(usb_snap_dir, exist_ok=True)
            archive_name = f'{snap_name}.tar.gz'
            archive_path = os.path.join(usb_snap_dir, archive_name)
            r = subprocess.run(
                ['tar', '-czf', archive_path, '-C', SNAPSHOTS_DIR, snap_name],
                capture_output=True, timeout=600
            )
            if r.returncode == 0:
                sz = os.path.getsize(archive_path) if os.path.isfile(archive_path) else 0
                _snap_update(log=f'USB: saved {archive_name} ({sz / 1048576:.1f} MB)')
            else:
                _snap_update(log=f'USB: write error — {r.stderr[:200]}')

        _snap_update(status='done', percent=100, message=f'Snapshot "{meta["label"]}" ready!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Error: {e}', log=f'EXCEPTION: {e}')
        logger.exception("Snapshot creation failed")


# ── Snapshot restore worker ──

def _restore_snapshot_worker(snap_dir, restore_docker, restore_volumes,
                              restore_ethos, restore_system):
    """Background task: restores system from a snapshot."""
    try:
        meta_file = os.path.join(snap_dir, 'meta.json')
        if not os.path.isfile(meta_file):
            _snap_update(status='error', message='No meta.json in snapshot')
            return

        with open(meta_file) as f:
            meta = json.load(f)

        _snap_update(percent=5, message='Restore started...', log=f'Restoring: {meta.get("label", "?")}')

        steps_total = sum([restore_ethos, restore_system, restore_docker, restore_volumes])
        step_i = 0

        # ── 1. EthOS config & data ──
        if restore_ethos:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring EthOS...', log='Restoring EthOS configuration...')

            ethos_root = os.environ.get('ETHOS_ROOT', f'/home/{get_ethos_user()}/docker/nasos')
            ethos_bak = os.path.join(snap_dir, 'ethos')

            if os.path.isdir(ethos_bak):
                # ethos.env
                env_src = os.path.join(ethos_bak, 'ethos.env')
                if os.path.isfile(env_src):
                    shutil.copy2(env_src, os.path.join(ethos_root, 'ethos.env'))
                    _snap_update(log='  ethos.env restored')

                # data/
                data_tar = os.path.join(ethos_bak, 'data.tar.gz')
                if os.path.isfile(data_tar):
                    # Backup current data before overwriting
                    data_current = os.path.join(ethos_root, 'data')
                    if os.path.isdir(data_current):
                        pre_restore = os.path.join(ethos_root, 'data.pre-restore')
                        if os.path.isdir(pre_restore):
                            shutil.rmtree(pre_restore, ignore_errors=True)
                        shutil.copytree(data_current, pre_restore)
                        _snap_update(log='  Current data/ copied to data.pre-restore/')
                    subprocess.run(
                        ['tar', '-xzf', data_tar, '-C', ethos_root],
                        capture_output=True, timeout=600
                    )
                    _snap_update(log='  data/ restored')

                # install.conf
                for extra in ['install.conf']:
                    src = os.path.join(ethos_bak, extra)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(ethos_root, extra))

                _snap_update(log='EthOS — restored')
            else:
                _snap_update(log='No EthOS data in snapshot')

        # ── 2. System configs ──
        if restore_system:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring system configuration...', log='Restoring system...')

            sys_bak = os.path.join(snap_dir, 'system')
            if os.path.isdir(sys_bak):
                # Hostname
                hostname_f = os.path.join(sys_bak, 'hostname')
                if os.path.isfile(hostname_f):
                    shutil.copy2(hostname_f, '/etc/hostname')
                    hostname = _read_file_safe(hostname_f, '').strip()
                    if hostname:
                        subprocess.run(['hostnamectl', 'set-hostname', hostname],
                                       capture_output=True, timeout=10)
                    _snap_update(log=f'  Hostname: {hostname}')

                # hosts
                hosts_f = os.path.join(sys_bak, 'hosts')
                if os.path.isfile(hosts_f):
                    shutil.copy2(hosts_f, '/etc/hosts')

                # Timezone
                tz_link = os.path.join(sys_bak, 'timezone_link')
                if os.path.isfile(tz_link):
                    tz_path = _read_file_safe(tz_link, '').strip()
                    if tz_path and os.path.isfile(tz_path):
                        try:
                            os.remove('/etc/localtime')
                        except Exception:
                            pass
                        os.symlink(tz_path, '/etc/localtime')
                        _snap_update(log=f'  Timezone: {tz_path}')

                # NetworkManager connections
                nm_tar = os.path.join(sys_bak, 'nm-connections.tar.gz')
                if os.path.isfile(nm_tar):
                    subprocess.run(
                        ['tar', '-xzf', nm_tar, '-C', '/etc/NetworkManager'],
                        capture_output=True, timeout=30
                    )
                    subprocess.run(['nmcli', 'connection', 'reload'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  NetworkManager connections restored')

                # Nginx sites
                nginx_tar = os.path.join(sys_bak, 'nginx-sites.tar.gz')
                if os.path.isfile(nginx_tar):
                    subprocess.run(
                        ['tar', '-xzf', nginx_tar, '-C', '/etc/nginx'],
                        capture_output=True, timeout=30
                    )
                    subprocess.run(['systemctl', 'reload', 'nginx'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Nginx sites restored')

                # Let's Encrypt
                le_tar = os.path.join(sys_bak, 'letsencrypt.tar.gz')
                if os.path.isfile(le_tar):
                    subprocess.run(
                        ['tar', '-xzf', le_tar, '-C', '/etc'],
                        capture_output=True, timeout=60
                    )
                    _snap_update(log='  Let\'s Encrypt certs restored')

                # Samba
                smb_f = os.path.join(sys_bak, 'smb.conf')
                if os.path.isfile(smb_f):
                    shutil.copy2(smb_f, '/etc/samba/smb.conf')
                    subprocess.run(['systemctl', 'restart', 'smbd'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Samba config restored')

                # fstab (copy but don't apply — user must reboot)
                fstab_f = os.path.join(sys_bak, 'fstab')
                if os.path.isfile(fstab_f):
                    shutil.copy2(fstab_f, '/etc/fstab')
                    _snap_update(log='  fstab restored (reboot required)')

                # Crontab
                cron_f = os.path.join(sys_bak, 'crontab')
                if os.path.isfile(cron_f):
                    subprocess.run(['crontab', cron_f],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Crontab restored')

                _snap_update(log='System config — restored')
            else:
                _snap_update(log='No system configuration in snapshot')

        # ── 2b. User home directories ──
        users_bak = os.path.join(snap_dir, 'userdirs')
        if os.path.isdir(users_bak):
            step_i += 1
            pct = int(step_i / steps_total * 85) if steps_total else 50
            _snap_update(percent=pct, message='Restoring user directories...', log='Restoring /home...')

            for tarfile_name in sorted(os.listdir(users_bak)):
                if not tarfile_name.endswith('.tar.gz'):
                    continue
                uname = tarfile_name.replace('.tar.gz', '')
                _snap_update(log=f'  Restoring user: {uname}')
                try:
                    subprocess.run(
                        ['tar', '-xzf', os.path.join(users_bak, tarfile_name), '-C', '/home'],
                        capture_output=True, timeout=3600
                    )
                    _snap_update(log=f'  {uname} — restored')
                except Exception as e:
                    _snap_update(log=f'  {uname} — error: {e}')

            _snap_update(log='User directories — restored')

        # ── 3. Docker compose projects ──
        if restore_docker:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring Docker...', log='Restoring Docker projects...')

            docker_bak = os.path.join(snap_dir, 'docker')
            proj_bak = os.path.join(docker_bak, 'projects')

            if os.path.isdir(proj_bak) and _docker_ok():
                compose_root = os.environ.get('COMPOSE_ROOT', f'/home/{get_ethos_user()}/docker')
                restored = 0

                for proj_name in sorted(os.listdir(proj_bak)):
                    proj_src = os.path.join(proj_bak, proj_name)
                    if not os.path.isdir(proj_src):
                        continue

                    proj_dest = os.path.join(compose_root, proj_name)
                    os.makedirs(proj_dest, exist_ok=True)

                    # Copy compose file + env files
                    for fn in os.listdir(proj_src):
                        src = os.path.join(proj_src, fn)
                        dst = os.path.join(proj_dest, fn)
                        if os.path.isfile(src):
                            shutil.copy2(src, dst)

                    # Pull images + start project
                    _snap_update(log=f'  Project {proj_name}: pulling images...')
                    subprocess.run(
                        ['docker', 'compose', 'pull'],
                        capture_output=True, timeout=300, cwd=proj_dest
                    )
                    r = subprocess.run(
                        ['docker', 'compose', 'up', '-d'],
                        capture_output=True, text=True, timeout=120, cwd=proj_dest
                    )
                    if r.returncode == 0:
                        _snap_update(log=f'  Project {proj_name}: started ✓')
                        restored += 1
                    else:
                        _snap_update(log=f'  Project {proj_name}: ERROR — {r.stderr[:200]}')

                _snap_update(log=f'Docker projects: {restored} restored')
            else:
                _snap_update(log='No Docker projects in snapshot or Docker unavailable')

        # ── 4. Docker volumes ──
        if restore_volumes and restore_docker:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring Docker volumes...', log='Importing Docker volumes...')

            vol_bak = os.path.join(snap_dir, 'docker', 'volumes')
            if os.path.isdir(vol_bak) and _docker_ok():
                restored_vols = 0
                vol_archives = [f for f in os.listdir(vol_bak) if f.endswith('.tar.gz')]

                for vi, vf in enumerate(vol_archives):
                    vol_name = vf.replace('.tar.gz', '')
                    vol_pct = int(pct + (vi / max(len(vol_archives), 1)) * (85 - pct))
                    _snap_update(
                        percent=vol_pct,
                        message=f'Import: {vol_name}',
                        log=f'  Volume: {vol_name}'
                    )

                    # Create volume if needed
                    _docker_cmd(['volume', 'create', vol_name])

                    # Stop containers using this volume before restoring
                    containers_using = _containers_using_volume(vol_name)
                    stopped = []
                    for cid in containers_using:
                        _docker_cmd(['stop', cid], timeout=30)
                        stopped.append(cid)
                        _snap_update(log=f'    Stopped container {cid[:12]}')

                    # Import volume data
                    archive_path = os.path.join(vol_bak, vf)
                    r = subprocess.run(
                        ['docker', 'run', '--rm',
                         '-v', f'{vol_name}:/volume_data',
                         '-v', f'{vol_bak}:/backup:ro',
                         'alpine', 'sh', '-c',
                         f'rm -rf /volume_data/* && tar -xzf /backup/{vf} -C /volume_data'],
                        capture_output=True, text=True, timeout=600
                    )
                    if r.returncode == 0:
                        restored_vols += 1
                    else:
                        _snap_update(log=f'    Import ERROR {vol_name}: {r.stderr[:200]}')

                    # Restart stopped containers
                    for cid in stopped:
                        _docker_cmd(['start', cid], timeout=30)
                        _snap_update(log=f'    Restarted {cid[:12]}')

                _snap_update(log=f'Volumes: {restored_vols}/{len(vol_archives)} restored')
            else:
                _snap_update(log='No volumes in snapshot or Docker unavailable')

        _snap_update(status='done', percent=100,
                     message=f'Restoring "{meta.get("label", "")}" completed!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Error: {e}', log=f'EXCEPTION: {e}')
        logger.exception("Snapshot restore failed")


# ── Helpers for snapshots ──

def _read_file_safe(path, default=''):
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return default


def _dir_size_str(path):
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for fn in files:
                total += os.path.getsize(os.path.join(root, fn))
    except Exception:
        pass
    if total > 1073741824:
        return f'{total / 1073741824:.1f} GB'
    if total > 1048576:
        return f'{total / 1048576:.1f} MB'
    return f'{total / 1024:.0f} KB'


def _find_env_files(compose_file):
    """Find extra .env files referenced in a compose file."""
    envs = []
    try:
        with open(compose_file, 'r') as f:
            content = f.read()
        for m in re.finditer(r'env_file:\s*\n\s*-\s*(.+)', content):
            fn = m.group(1).strip().strip('"').strip("'")
            if fn and fn != '.env':
                envs.append(fn)
        for m in re.finditer(r'env_file:\s+(.+)', content):
            fn = m.group(1).strip().strip('"').strip("'")
            if fn and fn != '.env' and not fn.startswith('-'):
                envs.append(fn)
    except Exception:
        pass
    return envs


def _containers_using_volume(vol_name):
    """Return list of container IDs using a given volume."""
    out, rc = _docker_cmd(['ps', '-a', '-q', '--filter', f'volume={vol_name}'])
    if rc == 0 and out:
        return [c.strip() for c in out.split('\n') if c.strip()]
    return []
