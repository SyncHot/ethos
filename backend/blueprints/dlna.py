"""
EthOS — DLNA / UPnP Blueprint
MiniDLNA media server management: install, configure, start/stop, rescan.
"""

import os
import re
import shlex
import json
from flask import Blueprint, request, jsonify

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run
from utils import require_tools, check_tool
from blueprints.admin_required import admin_required

dlna_bp = Blueprint('dlna', __name__, url_prefix='/api/dlna')

MINIDLNA_CONF = '/etc/minidlna.conf'
MINIDLNA_DB_DIR = '/var/lib/minidlna'

# =====================================================================
# Helpers
# =====================================================================

def _is_installed():
    """Check if minidlna package is installed."""
    r = host_run("dpkg -s minidlna 2>/dev/null | grep -q 'Status: install ok installed'", timeout=10)
    return r.returncode == 0


def _is_running():
    """Check if minidlna service is active."""
    r = host_run("systemctl is-active minidlna 2>/dev/null", timeout=10)
    return r.stdout.strip() == 'active'


def _get_file_count():
    """Get indexed file count from minidlna database."""
    # Try the status URL first (minidlna serves a status page on its port)
    r = host_run("find /var/lib/minidlna -name '*.db' -size +0 2>/dev/null | head -1", timeout=5)
    if r.returncode == 0 and r.stdout.strip():
        # Count from DB using sqlite if available
        db_path = r.stdout.strip()
        cr = host_run(
            f"sqlite3 {shlex.quote(db_path)} \"SELECT COUNT(*) FROM DETAILS\" 2>/dev/null",
            timeout=5,
        )
        if cr.returncode == 0 and cr.stdout.strip().isdigit():
            return int(cr.stdout.strip())
    # Fallback: count art_cache thumbnails as a rough indicator
    r2 = host_run("find /tmp/minidlna /var/cache/minidlna /var/lib/minidlna -type f 2>/dev/null | wc -l", timeout=10)
    if r2.returncode == 0 and r2.stdout.strip().isdigit():
        return int(r2.stdout.strip())
    return 0


def _get_mounted_drives():
    """Return list of mounted drive paths (real mount points, not rootfs)."""
    r = host_run(
        "lsblk -J -o NAME,MOUNTPOINT,TYPE,FSTYPE 2>/dev/null",
        timeout=10,
    )
    mounts = []
    if r.returncode != 0:
        return mounts
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return mounts

    def _collect(devices):
        for dev in devices:
            mp = dev.get('mountpoint')
            dtype = dev.get('type', '')
            if mp and mp != '/' and mp != '[SWAP]' and dtype in ('part', 'disk', 'lvm'):
                mounts.append(mp)
            for child in dev.get('children', []):
                cmp = child.get('mountpoint')
                ctype = child.get('type', '')
                if cmp and cmp != '/' and cmp != '[SWAP]' and ctype in ('part', 'disk', 'lvm'):
                    mounts.append(cmp)
                # Handle deeper nesting (e.g., LUKS)
                for gc in child.get('children', []):
                    gmp = gc.get('mountpoint')
                    if gmp and gmp != '/' and gmp != '[SWAP]':
                        mounts.append(gmp)

    _collect(data.get('blockdevices', []))
    return sorted(set(mounts))


def _parse_config():
    """Parse /etc/minidlna.conf into a dict."""
    config = {
        'friendly_name': 'EthOS Media Server',
        'port': 8200,
        'media_dirs': [],
        'inotify': True,
        'log_level': 'warn',
    }
    if not os.path.exists(MINIDLNA_CONF):
        return config
    try:
        with open(MINIDLNA_CONF, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key = key.strip()
                val = val.strip()
                if key == 'friendly_name':
                    config['friendly_name'] = val
                elif key == 'port':
                    try:
                        config['port'] = int(val)
                    except ValueError:
                        pass
                elif key == 'media_dir':
                    config['media_dirs'].append(val)
                elif key == 'inotify':
                    config['inotify'] = val.lower() == 'yes'
                elif key == 'log_level':
                    config['log_level'] = val
    except (IOError, OSError):
        pass
    return config


def _write_config(friendly_name, port, media_dirs, inotify):
    """Write /etc/minidlna.conf with validated params."""
    lines = [
        '# EthOS MiniDLNA configuration',
        '# Managed by EthOS — manual edits may be overwritten',
        '',
        f'friendly_name={friendly_name}',
        f'port={port}',
        f'db_dir={MINIDLNA_DB_DIR}',
        'log_dir=/var/log',
        f'inotify={"yes" if inotify else "no"}',
        'album_art_names=Cover.jpg/cover.jpg/AlbumArtSmall.jpg/albumartsmall.jpg',
        'album_art_names=AlbumArt.jpg/albumart.jpg/Folder.jpg/folder.jpg',
        'album_art_names=Thumb.jpg/thumb.jpg',
        '',
    ]
    for md in media_dirs:
        lines.append(f'media_dir={md}')
    lines.append('')
    conf_content = '\n'.join(lines)
    tmp_path = '/tmp/minidlna.conf.tmp'
    try:
        with open(tmp_path, 'w') as f:
            f.write(conf_content)
        r = host_run(f"sudo cp {shlex.quote(tmp_path)} {shlex.quote(MINIDLNA_CONF)}", timeout=10)
        os.unlink(tmp_path)
        return r.returncode == 0
    except (IOError, OSError):
        return False


def _validate_mount_path(path):
    """Check that a path (after stripping media type prefix) is a real mount point or subdirectory of one."""
    # Strip media type prefix like A,/path or V,/path
    clean = re.sub(r'^[AVP],', '', path)
    if not os.path.isabs(clean):
        return False
    mounted = _get_mounted_drives()
    for mp in mounted:
        if clean == mp or clean.startswith(mp + '/'):
            return True
    return False


# =====================================================================
# API Routes
# =====================================================================

@dlna_bp.route('/status')
@admin_required
def get_status():
    """Check if minidlna is installed, running, and get stats."""
    installed = _is_installed()
    running = _is_running() if installed else False
    file_count = _get_file_count() if running else 0
    config = _parse_config() if installed else {}
    return jsonify({
        'installed': installed,
        'running': running,
        'file_count': file_count,
        'port': config.get('port', 8200),
        'friendly_name': config.get('friendly_name', ''),
    })


@dlna_bp.route('/install', methods=['POST'])
@admin_required
def install_minidlna():
    """Install minidlna package via apt."""
    if _is_installed():
        return jsonify({'status': 'ok', 'installed': True})
    r = host_run(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y minidlna && apt-get clean",
        timeout=120,
    )
    if r.returncode != 0:
        return jsonify({'success': False, 'error': r.stderr.strip() or 'Installation failed'}), 500
    # Stop the auto-started service so user can configure first
    host_run("sudo systemctl stop minidlna 2>/dev/null", timeout=10)
    host_run("sudo systemctl disable minidlna 2>/dev/null", timeout=10)
    return jsonify({'status': 'ok'})


@dlna_bp.route('/config')
@admin_required
def get_config():
    """Get current minidlna configuration and available drives."""
    config = _parse_config()
    config['available_drives'] = _get_mounted_drives()
    return jsonify(config)


@dlna_bp.route('/config', methods=['PUT'])
@admin_required
def update_config():
    """Update minidlna configuration."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    friendly_name = str(data.get('friendly_name', 'EthOS Media Server')).strip()
    if not friendly_name or len(friendly_name) > 64:
        return jsonify({'error': 'Invalid friendly name'}), 400

    try:
        port = int(data.get('port', 8200))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid port'}), 400
    if not 1024 <= port <= 65535:
        return jsonify({'error': 'Port must be 1024-65535'}), 400

    media_dirs = data.get('media_dirs', [])
    if not isinstance(media_dirs, list):
        return jsonify({'error': 'media_dirs must be a list'}), 400

    # Validate each media dir path
    for md in media_dirs:
        if not isinstance(md, str) or not md.strip():
            return jsonify({'error': f'Invalid media directory: {md}'}), 400
        if not _validate_mount_path(md.strip()):
            return jsonify({'error': f'Path is not on a mounted drive: {md}'}), 400

    inotify = bool(data.get('inotify', True))

    clean_dirs = [md.strip() for md in media_dirs]
    ok = _write_config(friendly_name, port, clean_dirs, inotify)
    if not ok:
        return jsonify({'error': 'Failed to write configuration'}), 500

    # Reload if running
    if _is_running():
        host_run("sudo systemctl restart minidlna", timeout=15)

    return jsonify({'success': True})


@dlna_bp.route('/start', methods=['POST'])
@admin_required
def start_service():
    """Start minidlna service."""
    err = require_tools('minidlnad')
    if err:
        return err
    if not _is_installed():
        return jsonify({'error': 'minidlna is not installed'}), 400
    r = host_run("sudo systemctl start minidlna", timeout=15)
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to start'}), 500
    host_run("sudo systemctl enable minidlna 2>/dev/null", timeout=10)
    return jsonify({'success': True})


@dlna_bp.route('/stop', methods=['POST'])
@admin_required
def stop_service():
    """Stop minidlna service."""
    r = host_run("sudo systemctl stop minidlna", timeout=15)
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to stop'}), 500
    host_run("sudo systemctl disable minidlna 2>/dev/null", timeout=10)
    return jsonify({'success': True})


@dlna_bp.route('/rescan', methods=['POST'])
@admin_required
def rescan_library():
    """Force a full rescan of media library."""
    err = require_tools('minidlnad')
    if err:
        return err
    if not _is_installed():
        return jsonify({'error': 'minidlna is not installed'}), 400
    # Stop, clear DB, restart with fresh scan
    host_run("sudo systemctl stop minidlna 2>/dev/null", timeout=10)
    host_run(f"sudo rm -rf {shlex.quote(MINIDLNA_DB_DIR)}/files.db", timeout=10)
    r = host_run("sudo systemctl start minidlna", timeout=15)
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Rescan failed'}), 500
    return jsonify({'status': 'ok'})


@dlna_bp.route('/pkg-status')
@admin_required
def pkg_status():
    """Package status for AppStore integration."""
    installed = _is_installed()
    return jsonify({'installed': installed, 'status': 'active' if installed else 'not_installed'})


@dlna_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall_minidlna():
    """Uninstall minidlna."""
    from host import host_run
    host_run('systemctl stop minidlna 2>/dev/null; apt-get remove -y minidlna 2>/dev/null || true', timeout=60)
    return jsonify({'ok': True})
