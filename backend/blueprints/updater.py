"""
EthOS — OTA Update Blueprint
Checks for updates from a configurable update server,
downloads and applies them automatically or on demand.

Update sources supported:
  - EthOS instance: just IP or hostname (e.g. "192.168.1.100", "ethos.local")
  - GitHub releases:  "github:user/repo"
  - Full URL:         "https://my-server.com/updates"

Public endpoints (no auth):
  GET /updates/apps.json                 -> manifest of optional apps with SHA256 hashes
  GET /updates/apps/<app_id>/backend.py  -> serve app backend .py file
  GET /updates/apps/<app_id>/frontend.js -> serve app frontend .js file
  GET /updates/latest.json               -> system update manifest
  GET /updates/<filename>                -> system update packages
"""

import os
import json
import hashlib
import shutil
import tarfile
import threading
import subprocess
import time
import re
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, send_from_directory, abort

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import ETHOS_ROOT, app_path as _app_path, data_path as _data_path, host_run as _host_run, q as _q
from utils import sio_emit
import logging
log = logging.getLogger('ethos.updater')

update_bp = Blueprint('update', __name__, url_prefix='/api/update')
updates_public_bp = Blueprint('updates_public', __name__, url_prefix='/updates')

# ── Config ──
INSTALL_DIR = ETHOS_ROOT
VERSION_FILE = _app_path('backend/version.json')
UPDATE_DIR = '/tmp/ethos-update'
PUBLISH_DIR = _data_path('updates')
RELEASES_DIR = _app_path('installer/releases')
_STATUS_FILE = _data_path('update_status.json')

_socketio = None
_update_lock = threading.Lock()

# All known grubenv locations — GRUB's $prefix varies by UEFI firmware
# and boot entry (EFI/BOOT, EFI/debian, or boot/grub on ESP).
GRUBENV_PATHS = (
    '/boot/efi/EFI/BOOT/grubenv',
    '/boot/efi/EFI/debian/grubenv',
    '/boot/efi/boot/grub/grubenv',
    '/boot/grub/grubenv',
)

_STATUS_DEFAULTS = {
    'checking': False,
    'downloading': False,
    'applying': False,
    'last_check': None,
    'available': None,
    'error': None,
    'progress': 0,
    'message': '',
}


def _read_status():
    """Read shared update status from disk."""
    try:
        with open(_STATUS_FILE) as f:
            s = json.load(f)
        # Merge with defaults for any missing keys
        merged = dict(_STATUS_DEFAULTS)
        merged.update(s)
        return merged
    except Exception:
        return dict(_STATUS_DEFAULTS)


def _write_status(status):
    """Write shared update status to disk atomically."""
    os.makedirs(os.path.dirname(_STATUS_FILE), exist_ok=True)
    tmp = _STATUS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(status, f, ensure_ascii=False)
    os.replace(tmp, _STATUS_FILE)


def init_update(socketio):
    global _socketio
    _socketio = socketio


def _emit(event, data):
    sio_emit(_socketio, event, data)


def _get_current_version():
    try:
        with open(VERSION_FILE) as f:
            return json.load(f).get('version', '0.0.0')
    except Exception:
        return '0.0.0'


def _get_current_build_date():
    try:
        with open(VERSION_FILE) as f:
            return json.load(f).get('build_date', '')
    except Exception:
        return ''


def _version_tuple(v):
    """Convert version string to comparable tuple."""
    try:
        return tuple(int(x) for x in v.split('.'))
    except Exception:
        return (0, 0, 0)


def _best_update_dir():
    """Return (directory, version) with the newest latest.json.

    Checks both PUBLISH_DIR (data/updates) and RELEASES_DIR
    (installer/releases) and returns whichever has the higher version.
    Returns (None, None) if neither contains a valid latest.json.
    """
    best_dir = None
    best_ver = (0, 0, 0)
    for d in [PUBLISH_DIR, RELEASES_DIR]:
        manifest = os.path.join(d, 'latest.json')
        if not os.path.isfile(manifest):
            continue
        try:
            with open(manifest) as f:
                ver_str = json.load(f).get('version', '0.0.0')
            ver = _version_tuple(ver_str)
            if ver >= best_ver:
                best_dir = d
                best_ver = ver
        except Exception:
            continue
    return best_dir, '.'.join(str(x) for x in best_ver) if best_dir else None


def _load_config():
    """Load update configuration."""
    defaults = {
        'update_url': 'https://nas.myserver.pl',
        'auto_check': True,         # Check automatically on boot
        'auto_check_interval': 86400,  # Check every 24h (seconds)
        'auto_apply': False,        # Auto-apply updates (dangerous)
        'last_check': None,
        'channel': 'stable',
    }
    try:
        cfg_path = _config_path()
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                saved = json.load(f)
            defaults.update(saved)
    except Exception:
        pass
    return defaults


def _save_config(config):
    cfg_path = _config_path()
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    tmp = cfg_path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp, cfg_path)


def _config_path():
    p = _data_path('update_config.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _resolve_update_url(raw):
    """
    Resolve a user-friendly update source to a concrete base URL.

    Accepted formats:
      "192.168.1.100"             → http://192.168.1.100:9000/updates
      "ethos.local"             → http://ethos.local:9000/updates
      "github:user/repo"          → special GitHub releases handling
      "https://nas.example.com"   → https://nas.example.com/updates
      "https://nas.example.com/updates" → used as-is
      "http://custom:8080/my/path"      → used as-is
    """
    if not raw:
        return '', 'plain'

    raw = raw.strip().rstrip('/')

    # GitHub releases: "github:user/repo"
    if raw.startswith('github:'):
        return raw, 'github'

    # Already a full URL
    if raw.startswith('http://') or raw.startswith('https://'):
        # If the URL doesn't already end with /updates or a custom path
        # (i.e. it looks like just a base domain), append /updates
        from urllib.parse import urlparse
        parsed = urlparse(raw)
        path = parsed.path.rstrip('/')
        if path == '' or path == '/':
            # Bare domain like https://nas.myserver.pl → add /updates
            return raw.rstrip('/') + '/updates', 'plain'
        return raw, 'plain'

    # Bare IP or hostname → assume another EthOS instance on port 9000
    return f'http://{raw}:9000/updates', 'plain'


def _github_check(repo_path):
    """Check GitHub releases for latest version. Returns (manifest_dict, base_url)."""
    import urllib.request
    api_url = f'https://api.github.com/repos/{repo_path}/releases/latest'
    req = urllib.request.Request(api_url, headers={
        'User-Agent': 'EthOS-Updater',
        'Accept': 'application/vnd.github+json'
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        release = json.loads(resp.read().decode())

    tag = release.get('tag_name', '').lstrip('v')
    body = release.get('body', '')
    assets = release.get('assets', [])

    # Find the .tar.gz asset
    pkg_asset = None
    for a in assets:
        if a['name'].endswith('.tar.gz'):
            pkg_asset = a
            break

    if not pkg_asset:
        raise ValueError('No .tar.gz package in latest GitHub release')

    manifest = {
        'version': tag,
        'filename': pkg_asset['name'],
        'size': pkg_asset.get('size', 0),
        'download_url': pkg_asset['browser_download_url'],
        'changelog': {
            'title': release.get('name', f'v{tag}'),
            'changes': [line.lstrip('- ') for line in body.split('\n')
                        if line.strip().startswith('-')]
        }
    }
    return manifest


# ═══════════════════════════════════════════════════════════
#  API Endpoints
# ═══════════════════════════════════════════════════════════

@update_bp.route('/config', methods=['GET'])
def get_config():
    config = _load_config()
    config['current_version'] = _get_current_version()
    return jsonify(config)


@update_bp.route('/config', methods=['PUT'])
def set_config():
    data = request.get_json(force=True)
    config = _load_config()

    if 'update_url' in data:
        url = data['update_url'].strip().rstrip('/')
        config['update_url'] = url
    if 'auto_check' in data:
        config['auto_check'] = bool(data['auto_check'])
    if 'auto_check_interval' in data:
        config['auto_check_interval'] = max(3600, int(data['auto_check_interval']))
    if 'auto_apply' in data:
        config['auto_apply'] = bool(data['auto_apply'])

    _save_config(config)
    return jsonify({'success': True, 'config': config})


@update_bp.route('/check', methods=['POST'])
def check_for_update():
    """Check update server for new version."""
    config = _load_config()
    raw_url = config.get('update_url', '')
    if not raw_url:
        return jsonify({'error': 'Update server not configured'}), 400

    _st = _read_status()
    _st['checking'] = True
    _st['error'] = None
    _write_status(_st)
    _emit('update_status', _st)

    try:
        resolved_url, source_type = _resolve_update_url(raw_url)
        current = _get_current_version()

        if source_type == 'github':
            repo_path = raw_url.split(':', 1)[1]
            manifest = _github_check(repo_path)
        else:
            import urllib.request
            manifest_url = resolved_url + '/latest.json'
            req = urllib.request.Request(manifest_url, headers={'User-Agent': 'EthOS-Updater'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                manifest = json.loads(resp.read().decode())

        remote = manifest.get('version', '0.0.0')

        config['last_check'] = datetime.now().isoformat()
        _save_config(config)
        _st = _read_status()
        _st['last_check'] = config['last_check']
        _write_status(_st)

        is_newer = _version_tuple(remote) > _version_tuple(current)

        # Downgrade protection: reject if remote build_date is older
        if is_newer:
            local_date = _get_current_build_date()
            remote_date = manifest.get('build_date', manifest.get('published', ''))
            if remote_date:
                remote_date = remote_date[:10]   # normalise to YYYY-MM-DD
            if local_date and remote_date and remote_date < local_date:
                is_newer = False  # stale build with higher version number

        if is_newer:
            # Store resolved URL for download phase
            manifest['_resolved_url'] = resolved_url
            manifest['_source_type'] = source_type
            _st = _read_status()
            _st['available'] = manifest
            _st['checking'] = False
            _write_status(_st)
            _emit('update_status', _st)
            return jsonify({
                'update_available': True,
                'current_version': current,
                'remote_version': remote,
                'manifest': manifest,
            })
        else:
            _st = _read_status()
            _st['available'] = None
            _st['checking'] = False
            _write_status(_st)
            _emit('update_status', _st)
            return jsonify({
                'update_available': False,
                'current_version': current,
                'remote_version': remote,
                'message': 'System is up to date',
            })

    except Exception as e:
        _st = _read_status()
        _st['checking'] = False
        _st['error'] = str(e)
        _write_status(_st)
        _emit('update_status', _st)
        return jsonify({'error': f'Check error: {e}'}), 500


@update_bp.route('/apply', methods=['POST'])
def apply_update():
    """Download and apply update."""
    if not _update_lock.acquire(blocking=False):
        return jsonify({'error': 'Update already in progress'}), 409

    try:
        manifest = _read_status().get('available')
        if not manifest:
            _update_lock.release()
            return jsonify({'error': 'No update available — check first'}), 400

        # Start background update
        import gevent
        gevent.spawn(_do_apply_update, manifest)
        return jsonify({'status': 'ok'})

    except Exception as e:
        _update_lock.release()
        return jsonify({'error': str(e)}), 500


@update_bp.route('/upload', methods=['POST'])
def upload_update():
    """Upload update package manually (alternative to OTA)."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400

    f = request.files['file']
    if not f.filename.endswith('.tar.gz'):
        return jsonify({'error': '.tar.gz file required'}), 400

    if not _update_lock.acquire(blocking=False):
        return jsonify({'error': 'Update already in progress'}), 409

    try:
        os.makedirs(UPDATE_DIR, exist_ok=True)
        pkg_path = os.path.join(UPDATE_DIR, f.filename)
        f.save(pkg_path)

        import gevent
        gevent.spawn(_do_apply_from_file, pkg_path)
        return jsonify({'status': 'ok'})
    except Exception as e:
        _update_lock.release()
        return jsonify({'error': str(e)}), 500


@update_bp.route('/status', methods=['GET'])
def update_status():
    """Return current update status."""
    result = _read_status()
    result['current_version'] = _get_current_version()
    # Include A/B slot info if available
    ab_file = _data_path('ab_slots.json')
    if os.path.isfile(ab_file):
        try:
            with open(ab_file) as f:
                result['ab_slots'] = json.load(f)
            result['active_slot'] = _get_active_slot()
        except Exception:
            pass
    return jsonify(result)


@update_bp.route('/slot-info', methods=['GET'])
def slot_info():
    """Return A/B slot information."""
    ab_file = _data_path('ab_slots.json')
    if not os.path.isfile(ab_file):
        return jsonify({'ok': False, 'error': 'A/B slots not configured (legacy install)'})
    try:
        with open(ab_file) as f:
            slots = json.load(f)
        active = _get_active_slot()
        inactive = 'b' if active == 'a' else 'a'
        return jsonify({
            'ok': True,
            'active_slot': active,
            'inactive_slot': inactive,
            'slots': slots,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@update_bp.route('/rollback', methods=['POST'])
def rollback_slot():
    """Switch to the other A/B root slot (manual rollback)."""
    ab_file = _data_path('ab_slots.json')
    if not os.path.isfile(ab_file):
        return jsonify({'error': 'A/B slots not configured'}), 400
    try:
        active = _get_active_slot()
        target = 'b' if active == 'a' else 'a'

        # Verify target slot has a filesystem
        with open(ab_file) as f:
            slots = json.load(f)
        target_part = slots[f'slot_{target}']['partition']
        check = subprocess.run(
            ['blkid', target_part], capture_output=True, timeout=10
        )
        if check.returncode != 0:
            return jsonify({'error': f'Slot {target.upper()} has no filesystem'}), 400

        # Flip grubenv
        for grubenv in GRUBENV_PATHS:
            if os.path.exists(grubenv):
                subprocess.run(['grub-editenv', grubenv, 'set', f'boot_slot={target}'],
                               capture_output=True, timeout=10)
                subprocess.run(['grub-editenv', grubenv, 'set', 'boot_success=0'],
                               capture_output=True, timeout=10)
                subprocess.run(['grub-editenv', grubenv, 'set', 'boot_counter=0'],
                               capture_output=True, timeout=10)

        # Update ab_slots.json active field
        try:
            with open(ab_file) as f:
                ab_data = json.load(f)
            ab_data['active'] = target
            with open(ab_file, 'w') as f:
                json.dump(ab_data, f, indent=2)
        except Exception:
            pass

        _emit('update_log', {'message': f'Rollback: switching from slot {active.upper()} to {target.upper()}'})

        # Schedule reboot
        subprocess.Popen(
            ['bash', '-c', 'sleep 3 && reboot'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return jsonify({'ok': True, 'message': f'Rolling back to slot {target.upper()}, rebooting...',
                        'from_slot': active, 'to_slot': target})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _is_squashfs_mode():
    """Check if running in SquashFS + OverlayFS immutable root mode."""
    return os.path.ismount('/.squashfs')


@update_bp.route('/rootfs-info', methods=['GET'])
def rootfs_info():
    """Return information about the root filesystem mode."""
    sqsh = _is_squashfs_mode()
    info = {
        'ok': True,
        'squashfs': sqsh,
        'mode': 'squashfs+overlay' if sqsh else 'ext4',
    }
    if sqsh:
        # Report overlay usage
        rootfs_path = '/.rootfs'
        if os.path.ismount(rootfs_path):
            try:
                st = os.statvfs(rootfs_path)
                total = st.f_blocks * st.f_frsize
                free = st.f_bfree * st.f_frsize
                info['overlay_total_mb'] = total // (1024 * 1024)
                info['overlay_used_mb'] = (total - free) // (1024 * 1024)
            except Exception:
                pass
        # Check dm-verity status
        r = _host_run('dmsetup status ethos-verity 2>/dev/null', timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info['verity'] = True
            info['verity_status'] = 'verified'
        else:
            info['verity'] = False
            info['verity_status'] = 'not active'
    return jsonify(info)


@update_bp.route('/factory-reset', methods=['POST'])
def factory_reset():
    """Clear the overlay upper directory to restore immutable root state.

    Only works in SquashFS mode. Preserves fstab and machine-id in the
    overlay so the system remains bootable with correct partition UUIDs.
    Requires reboot to take effect.
    """
    if not _is_squashfs_mode():
        return jsonify({'error': 'Factory reset only available in SquashFS mode'}), 400

    rootfs_path = '/.rootfs'
    if not os.path.ismount(rootfs_path):
        return jsonify({'error': 'Rootfs ext4 not mounted at /.rootfs'}), 500

    upper_dir = os.path.join(rootfs_path, 'overlay/upper')
    work_dir = os.path.join(rootfs_path, 'overlay/work')

    if not os.path.isdir(upper_dir):
        return jsonify({'error': 'Overlay upper directory not found'}), 500

    try:
        # Preserve essential per-install configs that differ from squashfs
        preserved = {}
        for rel_path in ('etc/fstab', 'etc/machine-id'):
            full = os.path.join(upper_dir, rel_path)
            if os.path.isfile(full):
                with open(full) as f:
                    preserved[rel_path] = f.read()

        # Wipe overlay
        shutil.rmtree(upper_dir)
        shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(upper_dir)
        os.makedirs(work_dir)

        # Restore preserved files
        for rel_path, content in preserved.items():
            full = os.path.join(upper_dir, rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w') as f:
                f.write(content)

        _emit('update_log', {'message': 'Factory reset: overlay cleared, rebooting...'})

        # Schedule reboot
        subprocess.Popen(
            ['bash', '-c', 'sleep 3 && reboot'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return jsonify({'ok': True, 'message': 'Factory reset complete. Rebooting...'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  Publish / Serve — make this instance an update server
# ═══════════════════════════════════════════════════════════

@update_bp.route('/publish', methods=['POST'])
def publish_update():
    """Package current installation as an update for other instances."""
    try:
        ver = _get_current_version()
        _emit('update_log', {'message': f'Creating update package v{ver}...'})

        os.makedirs(PUBLISH_DIR, exist_ok=True)
        pkg_name = f'ethos-{ver}'
        pkg_filename = f'{pkg_name}.tar.gz'
        pkg_path = os.path.join(PUBLISH_DIR, pkg_filename)

        # Build tar.gz with backend + frontend + version.json
        with tarfile.open(pkg_path, 'w:gz') as tar:
            for subdir in ['backend', 'frontend']:
                src = os.path.join(INSTALL_DIR, subdir)
                if os.path.exists(src):
                    tar.add(src, arcname=f'{pkg_name}/{subdir}')


        # Compute sha256
        hasher = hashlib.sha256()
        with open(pkg_path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        sha = hasher.hexdigest()
        size = os.path.getsize(pkg_path)

        # Read changelog from version.json
        changelog = {}
        ver_file = os.path.join(INSTALL_DIR, 'backend', 'version.json')
        if os.path.exists(ver_file):
            with open(ver_file) as vf:
                vdata = json.load(vf)
                changelog = vdata.get('changelog', {})

        # Write latest.json manifest
        manifest = {
            'version': ver,
            'filename': pkg_filename,
            'sha256': sha,
            'size': size,
            'published': datetime.now().isoformat(),
            'changelog': changelog,
        }
        with open(os.path.join(PUBLISH_DIR, 'latest.json'), 'w') as mf:
            json.dump(manifest, mf, indent=2)

        _emit('update_log', {'message': f'Published {pkg_filename} ({size // 1024} KB)'})
        return jsonify({'success': True, 'manifest': manifest})

    except Exception as e:
        return jsonify({'error': f'Publish error: {e}'}), 500


@update_bp.route('/serve/latest.json', methods=['GET'])
def serve_manifest():
    """Serve latest.json for other EthOS instances.
    Returns the newest version from data/updates or installer/releases."""
    best_dir, _ = _best_update_dir()
    if not best_dir:
        abort(404, description='No published update available')
    return send_from_directory(best_dir, 'latest.json', mimetype='application/json')


@update_bp.route('/serve/<path:filename>', methods=['GET'])
def serve_package(filename):
    """Serve published update package file."""
    if '..' in filename or filename.startswith('/'):
        abort(400)
    # Try both directories
    for d in [PUBLISH_DIR, RELEASES_DIR]:
        fp = os.path.join(d, filename)
        if os.path.isfile(fp):
            return send_from_directory(d, filename)
    abort(404, description='File does not exist')


@update_bp.route('/publish-delta', methods=['POST'])
def publish_delta():
    """Create an xdelta3 binary diff between old and new squashfs images.

    If both slots have root.sqsh, creates a delta from active to inactive
    (or from a provided source path). The delta is published alongside the
    regular update package so clients can download the smaller file.
    """
    if not _is_squashfs_mode():
        return jsonify({'error': 'Delta publish requires squashfs mode'}), 400

    try:
        active_sqsh = '/.rootfs/root.sqsh'
        if not os.path.isfile(active_sqsh):
            return jsonify({'error': 'Active root.sqsh not found'}), 404

        data = request.json or {}
        new_sqsh = data.get('new_sqsh_path', '')

        # If no new sqsh provided, check inactive slot
        if not new_sqsh:
            ab_file = _data_path('ab_slots.json')
            if os.path.isfile(ab_file):
                with open(ab_file) as f:
                    slots = json.load(f)
                active = _get_active_slot()
                inactive = 'b' if active == 'a' else 'a'
                inactive_part = slots[f'slot_{inactive}']['partition']
                tmp_mount = '/tmp/delta-mount'
                os.makedirs(tmp_mount, exist_ok=True)
                subprocess.run(['umount', tmp_mount], capture_output=True, timeout=15)
                rc = subprocess.run(['mount', '-o', 'ro', inactive_part, tmp_mount],
                                    capture_output=True, timeout=30)
                if rc.returncode == 0:
                    check_path = os.path.join(tmp_mount, 'root.sqsh')
                    if os.path.isfile(check_path):
                        new_sqsh = check_path
                    else:
                        subprocess.run(['umount', tmp_mount], capture_output=True, timeout=15)
                        return jsonify({'error': 'No root.sqsh on inactive slot'}), 404

        if not new_sqsh or not os.path.isfile(new_sqsh):
            return jsonify({'error': 'No new squashfs image available'}), 404

        ver = _get_current_version()
        os.makedirs(PUBLISH_DIR, exist_ok=True)
        delta_name = f'ethos-{ver}-root.sqsh.xdelta3'
        delta_path = os.path.join(PUBLISH_DIR, delta_name)

        _emit('update_log', {'message': f'Creating delta patch: {delta_name}…'})

        r = subprocess.run(
            ['xdelta3', '-e', '-s', active_sqsh, new_sqsh, delta_path],
            capture_output=True, timeout=1800  # 30 min max
        )

        # Clean up temp mount
        subprocess.run(['umount', '/tmp/delta-mount'], capture_output=True, timeout=15)

        if r.returncode != 0:
            return jsonify({'error': f'xdelta3 failed: {r.stderr.decode()[:200]}'}), 500

        size = os.path.getsize(delta_path)
        old_size = os.path.getsize(active_sqsh)
        ratio = (size / old_size * 100) if old_size else 0

        # Update manifest with delta info
        manifest_path = os.path.join(PUBLISH_DIR, 'latest.json')
        if os.path.isfile(manifest_path):
            with open(manifest_path) as f:
                manifest = json.load(f)
            manifest['delta'] = {
                'filename': delta_name,
                'size': size,
                'ratio_pct': round(ratio, 1),
            }
            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2)

        _emit('update_log', {'message': f'Delta: {size // 1024} KB ({ratio:.1f}% of full image)'})
        return jsonify({
            'ok': True,
            'delta_file': delta_name,
            'size': size,
            'ratio_pct': round(ratio, 1),
        })

    except Exception as e:
        subprocess.run(['umount', '/tmp/delta-mount'], capture_output=True, timeout=15)
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  Public /updates/ routes — user-friendly URLs
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  App-level update serving  (public, no auth)
# ═══════════════════════════════════════════════════════════

def _build_apps_manifest():
    """Build a manifest of all optional apps with SHA256 hashes of their files."""
    try:
        from blueprints.app_manager import (
            _OPTIONAL_BLUEPRINTS, _get_frontend_filename, CORE_APPS
        )
    except ImportError:
        return {}
    bp_dir = os.path.join(ETHOS_ROOT, 'backend', 'blueprints')
    fe_dir = os.path.join(ETHOS_ROOT, 'frontend', 'js', 'apps')
    apps = {}
    for app_id, (module_name, _, _, _) in _OPTIONAL_BLUEPRINTS.items():
        if app_id in CORE_APPS:
            continue
        entry = {'id': app_id}
        # Backend hash
        py_path = os.path.join(bp_dir, module_name + '.py')
        if os.path.isfile(py_path):
            entry['backend'] = module_name + '.py'
            entry['backend_sha256'] = _file_sha256(py_path)
        # Frontend hash
        fn = _get_frontend_filename(app_id)
        if fn:
            js_path = os.path.join(fe_dir, fn + '.js')
            if os.path.isfile(js_path):
                entry['frontend'] = fn + '.js'
                entry['frontend_sha256'] = _file_sha256(js_path)
        if 'backend_sha256' in entry or 'frontend_sha256' in entry:
            apps[app_id] = entry
    return apps


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


@updates_public_bp.route('/apps.json', methods=['GET'])
def public_serve_apps_manifest():
    """Serve manifest of all optional apps with SHA256 hashes."""
    manifest = _build_apps_manifest()
    return jsonify(manifest)


@updates_public_bp.route('/apps/<app_id>/backend.py', methods=['GET'])
def public_serve_app_backend(app_id):
    """Serve an optional app's backend .py file."""
    try:
        from blueprints.app_manager import _OPTIONAL_BLUEPRINTS, CORE_APPS
    except ImportError:
        abort(404)
    if app_id in CORE_APPS or app_id not in _OPTIONAL_BLUEPRINTS:
        abort(404)
    module_name = _OPTIONAL_BLUEPRINTS[app_id][0]
    bp_dir = os.path.join(ETHOS_ROOT, 'backend', 'blueprints')
    fp = os.path.join(bp_dir, module_name + '.py')
    if not os.path.isfile(fp):
        abort(404)
    return send_from_directory(bp_dir, module_name + '.py', mimetype='text/x-python')


@updates_public_bp.route('/apps/<app_id>/frontend.js', methods=['GET'])
def public_serve_app_frontend(app_id):
    """Serve an optional app's frontend .js file."""
    try:
        from blueprints.app_manager import (
            _OPTIONAL_BLUEPRINTS, _get_frontend_filename, CORE_APPS
        )
    except ImportError:
        abort(404)
    if app_id in CORE_APPS:
        abort(404)
    fn = _get_frontend_filename(app_id)
    if not fn:
        abort(404)
    fe_dir = os.path.join(ETHOS_ROOT, 'frontend', 'js', 'apps')
    fp = os.path.join(fe_dir, fn + '.js')
    if not os.path.isfile(fp):
        abort(404)
    return send_from_directory(fe_dir, fn + '.js', mimetype='application/javascript')


# ═══════════════════════════════════════════════════════════
#  System-level update serving  (public, no auth)
# ═══════════════════════════════════════════════════════════

@updates_public_bp.route('/latest.json', methods=['GET'])
def public_serve_manifest():
    """Serve latest.json at /updates/latest.json.
    Returns the newest version from data/updates or installer/releases."""
    best_dir, _ = _best_update_dir()
    if not best_dir:
        abort(404, description='No published update available')
    return send_from_directory(best_dir, 'latest.json', mimetype='application/json')


@updates_public_bp.route('/<path:filename>', methods=['GET'])
def public_serve_package(filename):
    """Serve update packages at /updates/<filename>."""
    if '..' in filename or filename.startswith('/'):
        abort(400)
    # Try both directories
    for d in [PUBLISH_DIR, RELEASES_DIR]:
        fp = os.path.join(d, filename)
        if os.path.isfile(fp):
            return send_from_directory(d, filename)
    abort(404, description='File does not exist')


# ═══════════════════════════════════════════════════════════
#  Background update logic
# ═══════════════════════════════════════════════════════════

def _do_apply_update(manifest):
    """Download and apply update in background."""
    try:
        _st = _read_status()
        _st['downloading'] = True
        _st['progress'] = 0
        _st['error'] = None
        _st['message'] = 'Preparing download…'
        _write_status(_st)
        _emit('update_status', _st)

        filename = manifest['filename']
        expected_sha = manifest.get('sha256', '')
        expected_size = manifest.get('size', 0)

        # Resolve download URL — GitHub stores it directly, plain uses base + filename
        if manifest.get('download_url'):
            download_url = manifest['download_url']
        else:
            resolved_url = manifest.get('_resolved_url', '')
            download_url = resolved_url + '/' + filename

        os.makedirs(UPDATE_DIR, exist_ok=True)
        pkg_path = os.path.join(UPDATE_DIR, filename)

        # Check if a delta is available and we're in squashfs mode
        delta_info = manifest.get('delta')
        delta_path = None
        if delta_info and _is_squashfs_mode():
            delta_filename = delta_info.get('filename', '')
            if delta_filename:
                try:
                    import urllib.request
                    delta_url = (manifest.get('_resolved_url', '') + '/' + delta_filename)
                    delta_path = os.path.join(UPDATE_DIR, delta_filename)
                    _emit('update_log', {'message': f'Delta available ({delta_info.get("ratio_pct", "?")}% of full). Downloading delta…'})
                    req = urllib.request.Request(delta_url, headers={'User-Agent': 'EthOS-Updater'})
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        with open(delta_path, 'wb') as out:
                            while True:
                                chunk = resp.read(65536)
                                if not chunk:
                                    break
                                out.write(chunk)
                    _emit('update_log', {'message': f'Delta downloaded: {os.path.getsize(delta_path) // 1024} KB'})
                except Exception as e:
                    _emit('update_log', {'message': f'Delta download failed: {e}. Falling back to full download.'})
                    delta_path = None

        # Download with progress
        import urllib.request
        _st = _read_status()
        _st['message'] = f'Downloading {filename}…'
        _write_status(_st)
        _emit('update_log', {'message': f'Downloading {filename}...'})
        _emit('update_status', _st)

        req = urllib.request.Request(download_url, headers={'User-Agent': 'EthOS-Updater'})
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get('Content-Length', expected_size) or expected_size)
            downloaded = 0
            hasher = hashlib.sha256()

            with open(pkg_path, 'wb') as out:
                last_pct = -1
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(downloaded * 50 / total)
                        if pct != last_pct:
                            last_pct = pct
                            dl_mb = downloaded / 1048576
                            tot_mb = total / 1048576
                            _st = _read_status()
                            _st['progress'] = pct
                            _st['message'] = f'Downloading… {dl_mb:.1f} / {tot_mb:.1f} MB'
                            _write_status(_st)
                            _emit('update_status', _st)

        _emit('update_log', {'message': f'Downloaded {downloaded} bytes'})

        # Verify checksum
        if expected_sha:
            _st = _read_status()
            _st['message'] = 'Verifying checksum…'
            _write_status(_st)
            _emit('update_status', _st)
            actual_sha = hasher.hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(f'Checksum error: expected {expected_sha[:16]}..., got {actual_sha[:16]}...')
            _emit('update_log', {'message': 'Checksum OK'})

        _st = _read_status()
        _st['downloading'] = False
        _write_status(_st)
        _do_apply_from_file(pkg_path)

    except Exception as e:
        _st = _read_status()
        _st['downloading'] = False
        _st['applying'] = False
        _st['error'] = str(e)
        _st['progress'] = 0
        _st['message'] = ''
        _write_status(_st)
        _emit('update_status', _st)
        _emit('update_log', {'message': f'ERROR: {e}', 'error': True})
        _update_lock.release()


def _do_apply_from_file(pkg_path):
    """Apply update from a local .tar.gz file.

    Supports two modes:
    - A/B slot update: if /opt/ethos/data/ab_slots.json exists, writes to
      inactive root partition, flips grubenv, and reboots.
    - Legacy in-place: replaces backend/ and frontend/ on the live system
      and restarts the service (backward compat for pre-A/B installs).
    """
    try:
        # ── Extract and verify package ──
        _st = _read_status()
        _st['applying'] = True
        _st['progress'] = 55
        _st['message'] = 'Extracting package…'
        _write_status(_st)
        _emit('update_status', _st)
        _emit('update_log', {'message': 'Extracting...'})

        extract_dir = os.path.join(UPDATE_DIR, 'extracted')
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        os.makedirs(extract_dir)

        with tarfile.open(pkg_path, 'r:gz') as tar:
            tar.extractall(extract_dir)

        subdirs = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
        if not subdirs:
            raise ValueError('Empty package — no directory inside')
        pkg_dir = os.path.join(extract_dir, subdirs[0])

        _st = _read_status()
        _st['progress'] = 60
        _st['message'] = 'Verifying package…'
        _write_status(_st)
        _emit('update_status', _st)

        for required in ['backend/app.py', 'backend/version.json', 'frontend/index.html']:
            if not os.path.exists(os.path.join(pkg_dir, required)):
                raise ValueError(f'Invalid package — missing {required}')

        _emit('update_log', {'message': 'Package verified'})

        with open(os.path.join(pkg_dir, 'backend', 'version.json')) as f:
            new_ver = json.load(f).get('version', '?')
        _emit('update_log', {'message': f'New version: {new_ver}'})

        # Copy any delta file from UPDATE_DIR into pkg_dir
        for f in os.listdir(UPDATE_DIR):
            if f.endswith('.xdelta3'):
                delta_src = os.path.join(UPDATE_DIR, f)
                shutil.copy2(delta_src, os.path.join(pkg_dir, 'root.sqsh.xdelta3'))
                _emit('update_log', {'message': f'Delta file available: {f}'})
                break

        # ── Decide: A/B slot or legacy? ──
        ab_slots_file = _data_path('ab_slots.json')
        if os.path.isfile(ab_slots_file):
            _do_ab_slot_update(pkg_dir, new_ver, ab_slots_file)
        else:
            _do_legacy_update(pkg_dir, new_ver)

        # Cleanup
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)

    except Exception as e:
        _st = _read_status()
        _st['applying'] = False
        _st['error'] = str(e)
        _st['progress'] = 0
        _st['message'] = ''
        _write_status(_st)
        _emit('update_status', _st)
        _emit('update_log', {'message': f'ERROR: {e}', 'error': True})
    finally:
        try:
            _update_lock.release()
        except RuntimeError:
            pass


# ── A/B Slot Update ──────────────────────────────────────────

_AB_MOUNT = '/mnt/ethos-update'


def _get_active_slot():
    """Return active slot ('a' or 'b') from kernel cmdline or data file."""
    try:
        with open('/proc/cmdline') as f:
            for param in f.read().split():
                if param.startswith('ethos.slot='):
                    return param.split('=', 1)[1].strip()
    except Exception:
        pass
    # Fallback: read from data file
    try:
        with open(_data_path('active_slot')) as f:
            return f.read().strip()
    except Exception:
        return 'a'


def _do_ab_slot_update_squashfs(pkg_dir, new_ver, ab_slots_file):
    """Apply update to inactive A/B slot in SquashFS mode.

    Instead of rsync-ing the entire root, we:
    1. Copy the current root.sqsh to the inactive slot
    2. Clear the overlay on the inactive slot
    3. Apply update files (backend/frontend) to the overlay upper
    4. Write fstab to overlay upper
    5. Extract kernel + initrd from squashfs
    6. Flip grubenv → reboot
    """
    import glob as _glob

    with open(ab_slots_file) as f:
        slots = json.load(f)

    active = _get_active_slot()
    inactive = 'b' if active == 'a' else 'a'
    inactive_info = slots[f'slot_{inactive}']
    inactive_part = inactive_info['partition']

    _emit('update_log', {'message': f'SquashFS A/B update: writing to slot {inactive} ({inactive_part})'})

    _st = _read_status()
    _st['progress'] = 65
    _st['message'] = f'Preparing slot {inactive.upper()}…'
    _write_status(_st)
    _emit('update_status', _st)

    os.makedirs(_AB_MOUNT, exist_ok=True)
    subprocess.run(['umount', _AB_MOUNT], capture_output=True, timeout=15)
    rc = subprocess.run(['mount', inactive_part, _AB_MOUNT], capture_output=True, timeout=30)
    if rc.returncode != 0:
        raise RuntimeError(f'Cannot mount {inactive_part}: {rc.stderr.decode()[:200]}')

    try:
        # 1) Copy or delta-patch root.sqsh to inactive slot
        _st['progress'] = 70
        _st['message'] = f'Copying system image to slot {inactive.upper()}…'
        _write_status(_st)
        _emit('update_status', _st)

        active_sqsh = '/.rootfs/root.sqsh'
        inactive_sqsh = os.path.join(_AB_MOUNT, 'root.sqsh')

        # Check for delta file in the update package (xdelta3 binary diff)
        delta_file = os.path.join(pkg_dir, 'root.sqsh.xdelta3')
        new_sqsh_file = os.path.join(pkg_dir, 'root.sqsh')

        if os.path.isfile(new_sqsh_file):
            # Full squashfs image in update package — direct copy
            shutil.copy2(new_sqsh_file, inactive_sqsh)
            _emit('update_log', {'message': 'Copied new root.sqsh from update package'})

        elif os.path.isfile(delta_file) and os.path.isfile(active_sqsh):
            # Delta update: apply xdelta3 patch
            _emit('update_log', {'message': 'Applying delta patch to root.sqsh (this may take a minute)…'})
            _st['message'] = f'Applying delta patch to slot {inactive.upper()}…'
            _write_status(_st)
            _emit('update_status', _st)
            r = subprocess.run(
                ['xdelta3', '-d', '-s', active_sqsh, delta_file, inactive_sqsh],
                capture_output=True, timeout=600
            )
            if r.returncode != 0:
                _emit('update_log', {'message': f'Delta patch failed: {r.stderr.decode()[:200]}, falling back to copy'})
                if os.path.isfile(active_sqsh):
                    shutil.copy2(active_sqsh, inactive_sqsh)
                    _emit('update_log', {'message': 'Fallback: copied active root.sqsh'})
                else:
                    raise RuntimeError('Delta patch failed and no active root.sqsh for fallback')
            else:
                _emit('update_log', {'message': 'Delta patch applied successfully'})

        elif os.path.isfile(active_sqsh):
            # No delta, no new sqsh — copy active to inactive (overlay-only update)
            shutil.copy2(active_sqsh, inactive_sqsh)
            _emit('update_log', {'message': 'Copied root.sqsh to inactive slot'})
        else:
            raise RuntimeError('No root.sqsh source available (no delta, no active sqsh)')

        # Copy dm-verity data if available in update package
        for ext in ('.verity', '.roothash'):
            verity_src = os.path.join(pkg_dir, f'root.sqsh{ext}')
            if os.path.isfile(verity_src):
                shutil.copy2(verity_src, os.path.join(_AB_MOUNT, f'root.sqsh{ext}'))
        # Also copy roothash to ESP
        roothash_src = os.path.join(pkg_dir, 'root.sqsh.roothash')
        if os.path.isfile(roothash_src):
            esp_ethos = '/boot/efi/EFI/ethos'
            os.makedirs(esp_ethos, exist_ok=True)
            shutil.copy2(roothash_src, os.path.join(esp_ethos, 'roothash'))
            _emit('update_log', {'message': 'dm-verity roothash updated on ESP'})

        # 2) Clear overlay on inactive slot
        _st['progress'] = 75
        _st['message'] = 'Clearing overlay on inactive slot…'
        _write_status(_st)
        _emit('update_status', _st)

        overlay_upper = os.path.join(_AB_MOUNT, 'overlay/upper')
        overlay_work = os.path.join(_AB_MOUNT, 'overlay/work')
        shutil.rmtree(overlay_upper, ignore_errors=True)
        shutil.rmtree(overlay_work, ignore_errors=True)
        os.makedirs(overlay_upper, exist_ok=True)
        os.makedirs(overlay_work, exist_ok=True)
        _emit('update_log', {'message': 'Overlay cleared'})

        # 2b) Restore critical system state from active (running) system
        # Without these, the inactive slot boots into installer/hotspot mode
        _emit('update_log', {'message': 'Restoring system config to overlay…'})

        # .installed marker — prevents preboot installer from starting
        if os.path.isfile('/opt/ethos/.installed'):
            _d = os.path.join(overlay_upper, 'opt/ethos/.installed')
            os.makedirs(os.path.dirname(_d), exist_ok=True)
            shutil.copy2('/opt/ethos/.installed', _d)

        # machine-id
        if os.path.isfile('/etc/machine-id'):
            _d = os.path.join(overlay_upper, 'etc/machine-id')
            os.makedirs(os.path.dirname(_d), exist_ok=True)
            shutil.copy2('/etc/machine-id', _d)

        # hostname
        if os.path.isfile('/etc/hostname'):
            _d = os.path.join(overlay_upper, 'etc/hostname')
            os.makedirs(os.path.dirname(_d), exist_ok=True)
            shutil.copy2('/etc/hostname', _d)

        # ethos.service + enable it (disable preboot)
        svc_src = '/etc/systemd/system/ethos.service'
        if os.path.isfile(svc_src):
            svc_dir = os.path.join(overlay_upper, 'etc/systemd/system')
            os.makedirs(svc_dir, exist_ok=True)
            shutil.copy2(svc_src, os.path.join(svc_dir, 'ethos.service'))
            wants = os.path.join(svc_dir, 'multi-user.target.wants')
            os.makedirs(wants, exist_ok=True)
            lnk = os.path.join(wants, 'ethos.service')
            if not os.path.exists(lnk):
                os.symlink('/etc/systemd/system/ethos.service', lnk)
            # Ensure preboot is NOT enabled on the updated slot
            pb = os.path.join(wants, 'ethos-preboot.service')
            if os.path.exists(pb):
                os.remove(pb)

        # NetworkManager connections (WiFi, Ethernet, etc.)
        nm_src = '/etc/NetworkManager/system-connections'
        if os.path.isdir(nm_src) and os.listdir(nm_src):
            nm_dst = os.path.join(overlay_upper, 'etc/NetworkManager/system-connections')
            shutil.copytree(nm_src, nm_dst, dirs_exist_ok=True)

        # ethos.env and install.conf
        for fname in ('ethos.env', 'install.conf'):
            _s = os.path.join('/opt/ethos', fname)
            if os.path.isfile(_s):
                _d = os.path.join(overlay_upper, 'opt/ethos', fname)
                os.makedirs(os.path.dirname(_d), exist_ok=True)
                shutil.copy2(_s, _d)

        _emit('update_log', {'message': 'System config restored'})

        # 3) Apply update files to overlay upper
        _st['progress'] = 80
        _st['message'] = 'Applying update to inactive slot…'
        _write_status(_st)
        _emit('update_status', _st)

        for d in ['backend', 'frontend']:
            src = os.path.join(pkg_dir, d)
            if os.path.exists(src):
                dst = os.path.join(overlay_upper, 'opt/ethos', d)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
        _emit('update_log', {'message': 'Update files applied to overlay'})

        # 4) Sync frontend_dist in overlay
        fe_src = os.path.join(overlay_upper, 'opt/ethos/frontend')
        fe_dst = os.path.join(overlay_upper, 'opt/ethos/frontend_dist')
        if os.path.isdir(fe_src):
            shutil.copytree(fe_src, fe_dst, dirs_exist_ok=True)
            _emit('update_log', {'message': 'frontend_dist synced on inactive slot'})

        # 5) Write fstab to overlay upper (copy from active overlay)
        _st['progress'] = 85
        _st['message'] = 'Configuring inactive slot…'
        _write_status(_st)
        _emit('update_status', _st)

        active_fstab = '/.rootfs/overlay/upper/etc/fstab'
        if os.path.isfile(active_fstab):
            inactive_fstab_dir = os.path.join(overlay_upper, 'etc')
            os.makedirs(inactive_fstab_dir, exist_ok=True)
            # Read active fstab and replace active root UUID with inactive root UUID
            with open(active_fstab) as f:
                fstab_content = f.read()
            active_info = slots[f'slot_{active}']
            fstab_content = fstab_content.replace(
                active_info.get('uuid', ''), inactive_info.get('uuid', '')
            )
            with open(os.path.join(inactive_fstab_dir, 'fstab'), 'w') as f:
                f.write(fstab_content)
            _emit('update_log', {'message': 'fstab written to overlay'})

        # 6) Extract kernel + initrd from squashfs to ext4 root
        sqsh_mount = '/tmp/sqsh-update'
        os.makedirs(sqsh_mount, exist_ok=True)
        rc = subprocess.run(
            ['mount', '-t', 'squashfs', '-o', 'ro,loop', inactive_sqsh, sqsh_mount],
            capture_output=True, timeout=30
        )
        if rc.returncode == 0:
            try:
                boot_dir = os.path.join(_AB_MOUNT, 'boot')
                os.makedirs(boot_dir, exist_ok=True)
                for pattern in ('vmlinuz-*', 'initrd.img-*'):
                    files = sorted(_glob.glob(os.path.join(sqsh_mount, 'boot', pattern)))
                    if files:
                        shutil.copy2(files[-1], os.path.join(boot_dir, os.path.basename(files[-1])))
                _emit('update_log', {'message': 'Kernel + initrd extracted'})
            finally:
                subprocess.run(['umount', sqsh_mount], capture_output=True, timeout=15)

        # 7) Copy ab_slots.json to inactive overlay with updated active field
        inactive_data_dir = os.path.join(overlay_upper, 'opt/ethos/data')
        os.makedirs(inactive_data_dir, exist_ok=True)
        dest_ab = os.path.join(inactive_data_dir, 'ab_slots.json')
        try:
            with open(ab_slots_file) as _f:
                ab_data = json.load(_f)
            ab_data['active'] = inactive
            if os.path.realpath(ab_slots_file) != os.path.realpath(dest_ab):
                with open(dest_ab, 'w') as _f:
                    json.dump(ab_data, _f, indent=2)
            with open(ab_slots_file, 'w') as _f:
                json.dump(ab_data, _f, indent=2)
        except Exception:
            pass
        with open(os.path.join(inactive_data_dir, 'active_slot'), 'w') as f:
            f.write(inactive)

    finally:
        subprocess.run(['umount', '-l', _AB_MOUNT], capture_output=True, timeout=30)

    # 8) Flip grubenv
    _st = _read_status()
    _st['progress'] = 92
    _st['message'] = 'Switching boot slot…'
    _write_status(_st)
    _emit('update_status', _st)

    for grubenv in GRUBENV_PATHS:
        if os.path.exists(grubenv):
            subprocess.run(['grub-editenv', grubenv, 'set', f'boot_slot={inactive}'],
                           capture_output=True, timeout=10)
            subprocess.run(['grub-editenv', grubenv, 'set', 'boot_success=0'],
                           capture_output=True, timeout=10)
            subprocess.run(['grub-editenv', grubenv, 'set', 'boot_counter=0'],
                           capture_output=True, timeout=10)

    _emit('update_log', {'message': f'Boot slot switched to {inactive.upper()}'})

    # 9) Mark complete and reboot
    _st = _read_status()
    _st['progress'] = 100
    _st['applying'] = False
    _st['available'] = None
    _st['message'] = f'Updated to {new_ver}! Rebooting to slot {inactive.upper()}…'
    _write_status(_st)
    _emit('update_status', _st)
    _emit('update_log', {'message': f'SquashFS update to {new_ver} complete! Rebooting...'})
    _emit('update_complete', {'version': new_ver, 'slot': inactive, 'reboot': True})

    subprocess.Popen(
        ['bash', '-c', 'sleep 3 && reboot'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )


def _auto_snapshot_before_update(new_ver):
    """Create an automatic btrfs snapshot before applying an update."""
    data_mount = '/mnt/data'
    snap_mount = '/mnt/snapshots'
    try:
        r = _host_run(f'stat -f -c %T {_q(data_mount)}', timeout=5)
        if r.returncode != 0 or 'btrfs' not in r.stdout.lower():
            return  # not btrfs — skip
        if not os.path.ismount(snap_mount):
            # Try to mount @snapshots
            r = _host_run(f"findmnt -n -o SOURCE {_q(data_mount)}", timeout=5)
            if r.returncode != 0:
                return
            dev = r.stdout.strip().split('[')[0]
            os.makedirs(snap_mount, exist_ok=True)
            r = _host_run(f"mount -o subvol=@snapshots,noatime,compress=zstd:3 {_q(dev)} {_q(snap_mount)}", timeout=15)
            if r.returncode != 0:
                return
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        snap_name = f'snap_{ts}_pre_update'
        snap_path = os.path.join(snap_mount, snap_name)
        r = _host_run(f'btrfs subvolume snapshot -r {_q(data_mount)} {_q(snap_path)}', timeout=30)
        if r.returncode == 0:
            _emit('update_log', {'message': f'Auto-snapshot created: {snap_name}'})
            log.info("Pre-update btrfs snapshot: %s", snap_name)
            # Write metadata (may fail on read-only snapshot)
            try:
                import json as _json
                with open(os.path.join(snap_path, '.snap_meta.json'), 'w') as f:
                    _json.dump({'id': snap_name, 'label': f'Pre-update to {new_ver}',
                                'created': datetime.now().isoformat(), 'type': 'btrfs',
                                'auto': True}, f)
            except OSError:
                pass
    except Exception as e:
        log.warning("Auto-snapshot skipped: %s", e)


def _do_ab_slot_update(pkg_dir, new_ver, ab_slots_file):
    """Apply update to inactive A/B slot, flip grubenv, reboot."""
    # Auto-snapshot data partition before any update
    _auto_snapshot_before_update(new_ver)

    if _is_squashfs_mode():
        return _do_ab_slot_update_squashfs(pkg_dir, new_ver, ab_slots_file)

    # ── Traditional ext4 update (rsync active → inactive) ──

    # 1) Load slot metadata
    with open(ab_slots_file) as f:
        slots = json.load(f)

    active = _get_active_slot()
    inactive = 'b' if active == 'a' else 'a'
    inactive_info = slots[f'slot_{inactive}']
    inactive_part = inactive_info['partition']
    inactive_uuid = inactive_info['uuid']

    _emit('update_log', {'message': f'A/B update: active={active}, writing to slot {inactive} ({inactive_part})'})

    # 2) Mount inactive slot
    _st = _read_status()
    _st['progress'] = 65
    _st['message'] = f'Preparing slot {inactive.upper()}…'
    _write_status(_st)
    _emit('update_status', _st)

    os.makedirs(_AB_MOUNT, exist_ok=True)
    subprocess.run(['umount', _AB_MOUNT], capture_output=True, timeout=15)
    rc = subprocess.run(['mount', inactive_part, _AB_MOUNT], capture_output=True, timeout=30)
    if rc.returncode != 0:
        raise RuntimeError(f'Cannot mount {inactive_part}: {rc.stderr.decode()[:200]}')
    _emit('update_log', {'message': f'Mounted {inactive_part} at {_AB_MOUNT}'})

    try:
        # 3) Sync active root → inactive root (full system copy)
        _st = _read_status()
        _st['progress'] = 70
        _st['message'] = f'Syncing system to slot {inactive.upper()} (this may take a while)…'
        _write_status(_st)
        _emit('update_status', _st)
        _emit('update_log', {'message': 'rsync active → inactive...'})

        rsync_result = subprocess.run(
            ['rsync', '-aAXH', '--delete',
             '--exclude=/proc', '--exclude=/sys', '--exclude=/dev',
             '--exclude=/run', '--exclude=/tmp', '--exclude=/mnt',
             '--exclude=/media', '--exclude=/lost+found',
             '--exclude=/swapfile', '--exclude=/var/swap',
             '--exclude=/opt/ethos/data/visual_qa',
             '--exclude=/opt/ethos/logs/copilot_tickets',
             '--exclude=/opt/ethos/installer/images/*.img',
             '--exclude=/opt/ethos/installer/images/*.sqsh',
             '--exclude=/opt/ethos/installer/images/*.zst',
             '/', _AB_MOUNT + '/'],
            capture_output=True, text=True, timeout=600
        )
        if rsync_result.returncode not in (0, 24):
            _emit('update_log', {'message': f'rsync warning (rc={rsync_result.returncode})'})

        # Recreate required mount points
        for d in ('proc', 'sys', 'dev', 'run', 'tmp', 'mnt', 'media'):
            os.makedirs(os.path.join(_AB_MOUNT, d), exist_ok=True)

        _emit('update_log', {'message': 'System synced'})

        # 4) Apply update files on inactive slot
        _st = _read_status()
        _st['progress'] = 80
        _st['message'] = 'Updating files on inactive slot…'
        _write_status(_st)
        _emit('update_status', _st)

        inactive_ethos = os.path.join(_AB_MOUNT, 'opt/ethos')
        for d in ['backend', 'frontend']:
            src = os.path.join(pkg_dir, d)
            dst = os.path.join(inactive_ethos, d)
            if os.path.exists(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
        _emit('update_log', {'message': 'Files updated on inactive slot'})

        # 5) Sync frontend_dist on inactive slot
        fe_src = os.path.join(inactive_ethos, 'frontend')
        fe_dst = os.path.join(inactive_ethos, 'frontend_dist')
        if os.path.isdir(fe_src):
            subprocess.run(
                ['rsync', '-a', '--delete', fe_src + '/', fe_dst + '/'],
                capture_output=True, timeout=60
            )
            _emit('update_log', {'message': 'frontend_dist synced on inactive slot'})

        # 6) Update pip deps if requirements changed (using chroot)
        new_reqs = os.path.join(inactive_ethos, 'backend', 'requirements.txt')
        old_reqs = os.path.join(INSTALL_DIR, 'backend', 'requirements.txt')
        if os.path.isfile(new_reqs):
            reqs_changed = True
            if os.path.isfile(old_reqs):
                with open(new_reqs) as f1, open(old_reqs) as f2:
                    reqs_changed = f1.read().strip() != f2.read().strip()
            if reqs_changed:
                _st['progress'] = 85
                _st['message'] = 'Installing Python dependencies…'
                _write_status(_st)
                _emit('update_status', _st)
                _emit('update_log', {'message': 'Updating pip deps on inactive slot...'})
                pip_result = subprocess.run(
                    [os.path.join(_AB_MOUNT, 'opt/ethos/venv/bin/pip'),
                     'install', '--no-cache-dir', '--root', _AB_MOUNT,
                     '--prefix', '/opt/ethos/venv',
                     '-r', new_reqs],
                    capture_output=True, text=True, timeout=300,
                    env={**os.environ, 'HOME': '/root'}
                )
                if pip_result.returncode != 0:
                    # Fallback: run pip install on boot instead
                    _emit('update_log', {
                        'message': f'pip --root install skipped, will update on boot'
                    })
                    # Leave a marker for firstboot to pick up
                    Path(os.path.join(inactive_ethos, '.pip_update_needed')).touch()
                else:
                    _emit('update_log', {'message': 'Dependencies updated'})

        # 7) Fix fstab on inactive slot (replace active UUID with inactive UUID)
        _st['progress'] = 88
        _st['message'] = 'Configuring inactive slot…'
        _write_status(_st)
        _emit('update_status', _st)

        active_info = slots[f'slot_{active}']
        fstab_path = os.path.join(_AB_MOUNT, 'etc/fstab')
        if os.path.isfile(fstab_path):
            with open(fstab_path) as f:
                fstab = f.read()
            # Replace the root partition UUID
            fstab = fstab.replace(active_info['uuid'], inactive_uuid)
            with open(fstab_path, 'w') as f:
                f.write(fstab)
            _emit('update_log', {'message': f'fstab updated (root UUID={inactive_uuid[:8]}…)'})

        # 8) Write active_slot marker on inactive for the app to read after boot
        inactive_data = os.path.join(inactive_ethos, 'data')
        os.makedirs(inactive_data, exist_ok=True)
        with open(os.path.join(inactive_data, 'active_slot'), 'w') as f:
            f.write(inactive)

        # Copy ab_slots.json to inactive with updated active field
        dest_ab = os.path.join(inactive_data, 'ab_slots.json')
        try:
            with open(ab_slots_file) as _f:
                ab_data = json.load(_f)
            ab_data['active'] = inactive
            if os.path.realpath(ab_slots_file) != os.path.realpath(dest_ab):
                with open(dest_ab, 'w') as _f:
                    json.dump(ab_data, _f, indent=2)
            # Update current copy so status endpoint reflects pending switch
            with open(ab_slots_file, 'w') as _f:
                json.dump(ab_data, _f, indent=2)
        except Exception:
            pass  # non-critical — shared data partition already has the file

    finally:
        # Always unmount inactive slot
        subprocess.run(['umount', '-l', _AB_MOUNT], capture_output=True, timeout=30)

    # 9) Flip grubenv to boot from inactive slot
    _st = _read_status()
    _st['progress'] = 92
    _st['message'] = 'Switching boot slot…'
    _write_status(_st)
    _emit('update_status', _st)

    for grubenv in GRUBENV_PATHS:
        if os.path.exists(grubenv):
            subprocess.run(['grub-editenv', grubenv, 'set', f'boot_slot={inactive}'],
                           capture_output=True, timeout=10)
            subprocess.run(['grub-editenv', grubenv, 'set', 'boot_success=0'],
                           capture_output=True, timeout=10)
            subprocess.run(['grub-editenv', grubenv, 'set', 'boot_counter=0'],
                           capture_output=True, timeout=10)

    _emit('update_log', {'message': f'Boot slot switched to {inactive.upper()}'})

    # 10) Mark complete and reboot
    _st = _read_status()
    _st['progress'] = 100
    _st['applying'] = False
    _st['available'] = None
    _st['message'] = f'Updated to {new_ver}! Rebooting to slot {inactive.upper()}…'
    _write_status(_st)
    _emit('update_status', _st)
    _emit('update_log', {'message': f'Update to {new_ver} complete! Rebooting...'})
    _emit('update_complete', {'version': new_ver, 'slot': inactive, 'reboot': True})

    # Reboot (not just restart service — need to boot into new root)
    subprocess.Popen(
        ['bash', '-c', 'sleep 3 && reboot'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )


# ── Legacy In-Place Update ───────────────────────────────────

def _do_legacy_update(pkg_dir, new_ver):
    """Apply update in-place on the live system (pre-A/B installs)."""

    # Backup current files
    _st = _read_status()
    _st['progress'] = 70
    _st['message'] = 'Creating backup…'
    _write_status(_st)
    _emit('update_status', _st)
    _emit('update_log', {'message': 'Creating backup...'})

    backup_dir = os.path.join(UPDATE_DIR, 'backup-' + datetime.now().strftime('%Y%m%d%H%M%S'))
    os.makedirs(backup_dir)

    for d in ['backend', 'frontend']:
        src = os.path.join(INSTALL_DIR, d)
        if os.path.exists(src):
            shutil.copytree(src, os.path.join(backup_dir, d))

    _emit('update_log', {'message': f'Backup at {backup_dir}'})

    # Apply update — replace backend, frontend
    _st = _read_status()
    _st['progress'] = 80
    _st['message'] = 'Updating files…'
    _write_status(_st)
    _emit('update_status', _st)
    _emit('update_log', {'message': 'Updating files...'})

    for d in ['backend', 'frontend']:
        src = os.path.join(pkg_dir, d)
        dst = os.path.join(INSTALL_DIR, d)
        if os.path.exists(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

    _st = _read_status()
    _st['progress'] = 85
    _st['message'] = 'Files updated'
    _write_status(_st)
    _emit('update_status', _st)
    _emit('update_log', {'message': 'Files updated'})

    # Sync frontend_dist
    frontend_src = os.path.join(INSTALL_DIR, 'frontend')
    frontend_dst = os.path.join(INSTALL_DIR, 'frontend_dist')
    if os.path.isdir(frontend_src):
        subprocess.run(
            ['rsync', '-a', '--delete', frontend_src + '/', frontend_dst + '/'],
            capture_output=True, timeout=60
        )
        _emit('update_log', {'message': 'frontend_dist synced'})

    # Update Python dependencies if requirements.txt changed
    new_reqs = os.path.join(INSTALL_DIR, 'backend', 'requirements.txt')
    old_reqs = os.path.join(backup_dir, 'backend', 'requirements.txt')
    venv_pip = os.path.join(INSTALL_DIR, 'venv', 'bin', 'pip')
    if os.path.isfile(new_reqs) and os.path.isfile(venv_pip):
        reqs_changed = True
        if os.path.isfile(old_reqs):
            with open(new_reqs) as f1, open(old_reqs) as f2:
                reqs_changed = f1.read().strip() != f2.read().strip()
        if reqs_changed:
            _st = _read_status()
            _st['progress'] = 88
            _st['message'] = 'Installing Python dependencies…'
            _write_status(_st)
            _emit('update_status', _st)
            _emit('update_log', {'message': 'Updating Python dependencies...'})
            pip_result = subprocess.run(
                [venv_pip, 'install', '--no-cache-dir', '-r', new_reqs],
                capture_output=True, text=True, timeout=300
            )
            if pip_result.returncode == 0:
                _emit('update_log', {'message': 'Dependencies updated'})
            else:
                _emit('update_log', {'message': f'pip install warning: {pip_result.stderr[-200:]}'})

    # Ensure core system packages are installed (older images may lack them)
    _CORE_SYSTEM_PKGS = ['ufw', 'fail2ban', 'rsync', 'cron', 'avahi-daemon', 'gnupg', 'age']
    try:
        _st = _read_status()
        _st['progress'] = 90
        _st['message'] = 'Checking core system packages…'
        _write_status(_st)
        _emit('update_status', _st)

        check = subprocess.run(
            ['dpkg', '-s'] + _CORE_SYSTEM_PKGS,
            capture_output=True, text=True, timeout=15
        )
        missing = []
        if check.returncode != 0:
            for line in check.stderr.splitlines():
                if 'is not installed' in line:
                    parts = line.split("'")
                    if len(parts) >= 2:
                        missing.append(parts[1])
        if missing:
            _emit('update_log', {'message': f'Installing missing packages: {", ".join(missing)}'})
            apt_result = subprocess.run(
                ['bash', '-c',
                 'DEBIAN_FRONTEND=noninteractive apt-get update -y -qq 2>/dev/null; '
                 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ' + ' '.join(missing)],
                capture_output=True, text=True, timeout=180
            )
            if apt_result.returncode == 0:
                _emit('update_log', {'message': 'Core packages installed'})
            else:
                _emit('update_log', {'message': f'apt warning: {apt_result.stderr[-200:]}'})

            for svc in ['ufw', 'fail2ban']:
                if svc in missing:
                    subprocess.run(['systemctl', 'enable', '--now', svc],
                                   capture_output=True, timeout=30)
            if 'ufw' in missing:
                subprocess.run(['ufw', '--force', 'enable'], capture_output=True, timeout=15)
                subprocess.run(['ufw', 'allow', '22/tcp'], capture_output=True, timeout=10)
                subprocess.run(['ufw', 'allow', '9000/tcp'], capture_output=True, timeout=10)
                _emit('update_log', {'message': 'Firewall enabled (SSH + EthOS allowed)'})
        else:
            _emit('update_log', {'message': 'Core packages OK'})
    except Exception as e:
        _emit('update_log', {'message': f'Core packages check note: {e}'})

    # Fix service file if still using gunicorn (pre-1.0.30 installs)
    svc_path = '/etc/systemd/system/ethos.service'
    try:
        with open(svc_path) as f:
            svc_content = f.read()
        if 'gunicorn' in svc_content:
            _emit('update_log', {'message': 'Migrating service from gunicorn to python app.py...'})
            new_svc = """[Unit]
Description=EthOS NAS
After=network.target
Wants=network.target

[Service]
Type=notify
NotifyAccess=all
WorkingDirectory=/opt/ethos
EnvironmentFile=/opt/ethos/ethos.env
ExecStartPre=/bin/bash -c 'for d in data logs backups uploads venv; do p="/opt/ethos/$d"; [ -L "$p" ] && mkdir -p "$(readlink "$p")" || mkdir -p "$p"; done'
Environment=PYTHONPATH=/opt/ethos/backend
ExecStart=/opt/ethos/venv/bin/python /opt/ethos/backend/app.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""
            with open(svc_path, 'w') as f:
                f.write(new_svc)
            subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, timeout=15)
            _emit('update_log', {'message': 'Service migrated to python app.py'})
    except Exception as e:
        _emit('update_log', {'message': f'Service migration note: {e}'})

    # Mark complete and restart
    _st = _read_status()
    _st['progress'] = 100
    _st['applying'] = False
    _st['available'] = None
    _st['message'] = f'Updated to {new_ver}!'
    _write_status(_st)
    _emit('update_status', _st)
    _emit('update_log', {'message': f'Update to {new_ver} complete! Restarting...'})

    subprocess.Popen(
        ['bash', '-c', 'sleep 2 && systemctl restart ethos.service'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    _emit('update_complete', {'version': new_ver})


# ═══════════════════════════════════════════════════════════
#  Background auto-check (called from app.py on startup)
# ═══════════════════════════════════════════════════════════

def update_auto_check_loop():
    """Background loop that periodically checks for updates."""
    import gevent
    gevent.sleep(60)  # Wait 1 min after startup

    while True:
        try:
            config = _load_config()
            if config.get('auto_check') and config.get('update_url'):
                interval = config.get('auto_check_interval', 86400)
                last = config.get('last_check')

                should_check = True
                if last:
                    try:
                        last_dt = datetime.fromisoformat(last)
                        elapsed = (datetime.now() - last_dt).total_seconds()
                        should_check = elapsed >= interval
                    except Exception:
                        pass

                if should_check:
                    raw_url = config['update_url']
                    resolved_url, source_type = _resolve_update_url(raw_url)
                    current = _get_current_version()

                    if source_type == 'github':
                        repo_path = raw_url.split(':', 1)[1]
                        manifest = _github_check(repo_path)
                    else:
                        import urllib.request
                        url = resolved_url + '/latest.json'
                        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-Updater'})
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            manifest = json.loads(resp.read().decode())

                    remote = manifest.get('version', '0.0.0')

                    config['last_check'] = datetime.now().isoformat()
                    _save_config(config)
                    _st = _read_status()
                    _st['last_check'] = config['last_check']
                    _write_status(_st)

                    if _version_tuple(remote) > _version_tuple(current):
                        manifest['_resolved_url'] = resolved_url
                        manifest['_source_type'] = source_type
                        _st = _read_status()
                        _st['available'] = manifest
                        _write_status(_st)
                        _emit('update_available', {
                            'current': current,
                            'remote': remote,
                            'changelog': manifest.get('changelog', {})
                        })

                        if config.get('auto_apply'):
                            if _update_lock.acquire(blocking=False):
                                import gevent as g2
                                g2.spawn(_do_apply_update, manifest)
        except Exception:
            pass

        import gevent
        gevent.sleep(3600)  # Recheck every hour
