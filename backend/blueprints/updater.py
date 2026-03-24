"""
EthOS — OTA Update Blueprint
Checks for updates from a configurable update server,
downloads and applies them automatically or on demand.

Update sources supported:
  - EthOS instance: just IP or hostname (e.g. "192.168.1.100", "ethos.local")
  - GitHub releases:  "github:user/repo"
  - Full URL:         "https://my-server.com/updates"
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
from host import ETHOS_ROOT, app_path as _app_path, data_path as _data_path
from utils import sio_emit

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
        'update_url': '',           # e.g. http://192.168.1.100:8888
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
    return jsonify(result)


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


# ═══════════════════════════════════════════════════════════
#  Public /updates/ routes — user-friendly URLs
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
    """Apply update from a local .tar.gz file."""
    try:
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

        # Find the package directory (ethos-X.Y.Z/)
        subdirs = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
        if not subdirs:
            raise ValueError('Empty package — no directory inside')
        pkg_dir = os.path.join(extract_dir, subdirs[0])

        _st = _read_status()
        _st['progress'] = 60
        _st['message'] = 'Verifying package…'
        _write_status(_st)
        _emit('update_status', _st)

        # Verify package has required structure
        for required in ['backend/app.py', 'backend/version.json', 'frontend/index.html']:
            if not os.path.exists(os.path.join(pkg_dir, required)):
                raise ValueError(f'Invalid package — missing {required}')

        _emit('update_log', {'message': 'Package verified'})

        # Read new version
        with open(os.path.join(pkg_dir, 'backend', 'version.json')) as f:
            new_ver = json.load(f).get('version', '?')
        _emit('update_log', {'message': f'New version: {new_ver}'})

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

        # Restart service
        _st = _read_status()
        _st['progress'] = 95
        _st['message'] = 'Restarting service…'
        _write_status(_st)
        _emit('update_status', _st)
        _emit('update_log', {'message': 'Restarting EthOS service...'})
        result = subprocess.run(
            ['systemctl', 'restart', 'ethos.service'],
            capture_output=True, text=True, timeout=60
        )

        if result.returncode != 0:
            _emit('update_log', {'message': f'Restart stderr: {result.stderr[-500:]}'})
            # Try to rollback
            _emit('update_log', {'message': 'ERROR — attempting restore...', 'error': True})
            for d in ['backend', 'frontend']:
                bak = os.path.join(backup_dir, d)
                dst = os.path.join(INSTALL_DIR, d)
                if os.path.exists(bak):
                    if os.path.exists(dst):
                        shutil.rmtree(dst)
                    shutil.copytree(bak, dst)
            subprocess.run(['systemctl', 'restart', 'ethos.service'], timeout=60)
            raise RuntimeError(f'Restart failed: {result.stderr[-200:]}')

        _st = _read_status()
        _st['progress'] = 100
        _st['applying'] = False
        _st['available'] = None
        _st['message'] = 'Done!'
        _write_status(_st)
        _emit('update_status', _st)
        _emit('update_log', {'message': f'Update to {new_ver} complete! System restarting...'})
        _emit('update_complete', {'version': new_ver})

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
