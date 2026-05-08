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
# and boot entry (EFI/BOOT, EFI/debian, EFI/ubuntu, or boot/grub on ESP).
GRUBENV_PATHS = (
    '/boot/efi/EFI/BOOT/grubenv',
    '/boot/efi/EFI/debian/grubenv',
    '/boot/efi/EFI/ubuntu/grubenv',
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

    # Clean up stale status from previous boot (e.g. after reboot post-update)
    try:
        st = _read_status()
        dirty = False
        if st.get('downloading') or st.get('applying'):
            st['downloading'] = False
            st['applying'] = False
            st['error'] = None
            dirty = True
        if st.get('progress', 0) >= 100:
            st['progress'] = 0
            st['message'] = ''
            st['available'] = None
            dirty = True
        if dirty:
            _write_status(st)
    except Exception:
        pass
    
    # Initialize sub-modules with SocketIO reference
    try:
        from blueprints.updater_apply import init_updater_apply
        from blueprints.updater_publish import init_updater_publish
        init_updater_apply(_socketio)
        init_updater_publish(_socketio)
    except Exception as e:
        log.warning('Could not initialize updater sub-modules: %s', e)


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
        'github_token': '',
        'github_repo': '',
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
    # Mask token — never expose plaintext to frontend
    if config.get('github_token'):
        config['github_token_set'] = True
        config['github_token'] = ''
    else:
        config['github_token_set'] = False
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
    if 'github_repo' in data:
        config['github_repo'] = data['github_repo'].strip()
    if 'github_token' in data and data['github_token'].strip():
        config['github_token'] = data['github_token'].strip()

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
        active_slot = _get_active_slot()
        # Overlay upper: prefer data partition, fall back to root partition
        data_overlay = os.path.join('/mnt/data/ethos/overlay', active_slot, 'upper')
        root_overlay = '/.rootfs/overlay/upper'
        if os.path.isdir(data_overlay) and os.path.ismount('/mnt/data'):
            info['overlay_upper'] = data_overlay
            info['overlay_on_data'] = True
        else:
            info['overlay_upper'] = root_overlay
            info['overlay_on_data'] = False

        # Overlay disk usage
        upper_path = info['overlay_upper']
        if os.path.isdir(upper_path):
            try:
                r = _host_run(f'du -sb {upper_path}', timeout=10)
                if r.returncode == 0:
                    info['overlay_used_bytes'] = int(r.stdout.split()[0])
            except Exception:
                pass

        # Root partition (/.rootfs) stats
        rootfs_path = '/.rootfs'
        if os.path.ismount(rootfs_path):
            try:
                st = os.statvfs(rootfs_path)
                total = st.f_blocks * st.f_frsize
                free = st.f_bfree * st.f_frsize
                info['root_partition_total_mb'] = total // (1024 * 1024)
                info['root_partition_free_mb'] = free // (1024 * 1024)
                info['root_partition_used_mb'] = (total - free) // (1024 * 1024)
            except Exception:
                pass

        # Data partition stats
        if os.path.ismount('/mnt/data'):
            try:
                st = os.statvfs('/mnt/data')
                total = st.f_blocks * st.f_frsize
                free = st.f_bfree * st.f_frsize
                info['data_total_mb'] = total // (1024 * 1024)
                info['data_free_mb'] = free // (1024 * 1024)
                info['data_used_mb'] = (total - free) // (1024 * 1024)
            except Exception:
                pass

        # Docker data-root location
        daemon_cfg = '/etc/docker/daemon.json'
        if os.path.isfile(daemon_cfg):
            try:
                import json as _json
                with open(daemon_cfg) as _f:
                    dcfg = _json.load(_f)
                info['docker_data_root'] = dcfg.get('data-root', '/var/lib/docker')
            except Exception:
                info['docker_data_root'] = '/var/lib/docker'
        else:
            info['docker_data_root'] = '/var/lib/docker'

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

    active_slot = _get_active_slot()
    upper_dir, work_dir = _get_overlay_dirs(active_slot)

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



# ════════════════════════════════════════════════════════════
#  Sub-module Imports (Bottom-Import Pattern)
#  Prevents circular imports by importing after all module code is defined
# ════════════════════════════════════════════════════════════
# Sub-modules (updater_apply.py and updater_publish.py) are imported and
# initialized in init_update() above to ensure blueprints and helpers are
# fully defined before sub-modules import them.
