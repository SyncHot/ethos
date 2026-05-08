"""
EthOS — OTA Publish & GitHub Release Management  
Publishes updates, builds manifests, and manages GitHub releases.
"""

import os
import json
import hashlib
import tarfile
import subprocess
import re
import logging
from datetime import datetime
import urllib.request
import urllib.error

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, ETHOS_ROOT, app_path as _app_path, data_path as _data_path, q as _q
from utils import sio_emit
from flask import jsonify, request, send_from_directory, abort
from blueprints.admin_required import admin_required

log = logging.getLogger('ethos.updater')

# Import from main updater module
from updater import (
    update_bp, updates_public_bp, _emit, _get_current_version,
    INSTALL_DIR, VERSION_FILE, PUBLISH_DIR, RELEASES_DIR,
    _load_config, _save_config, _best_update_dir, _host_run
)

_socketio = None

def init_updater_publish(socketio):
    """Set SocketIO reference for async events."""
    global _socketio
    _socketio = socketio


# ════════════════════════════════════════════════════════════
#  Publish / Serve — make this instance an update server
# ════════════════════════════════════════════════════════════

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


def _bump_version(version_str, bump):
    """Bump semantic version. bump: 'patch'|'minor'|'major'."""
    parts = version_str.split('.')
    while len(parts) < 3:
        parts.append('0')
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    if bump == 'major':
        major += 1; minor = 0; patch = 0
    elif bump == 'minor':
        minor += 1; patch = 0
    else:
        patch += 1
    return f'{major}.{minor}.{patch}'


def _detect_github_repo():
    """Auto-detect GitHub repo from git remote URL."""
    try:
        r = _host_run(f'git -C {_q(INSTALL_DIR)} remote get-url origin', timeout=10)
        if r.returncode == 0:
            url = r.stdout.strip()
            # git@github.com:Owner/repo.git  or  https://github.com/Owner/repo.git
            m = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$', url)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ''


@update_bp.route('/github-release', methods=['POST'])
def github_release():
    """Commit, push, and create a GitHub Release with the update package."""
    import urllib.request as _urllib_req
    import urllib.error as _urllib_err

    data = request.get_json(force=True) or {}
    bump = data.get('bump', 'patch')
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'error': 'Changelog message is required'}), 400

    config = _load_config()
    token = config.get('github_token', '').strip()
    repo = config.get('github_repo', '').strip() or _detect_github_repo()

    if not token:
        return jsonify({'error': 'GitHub token not configured (Settings → GitHub)'}), 400
    if not repo:
        return jsonify({'error': 'GitHub repo not configured (Settings → GitHub)'}), 400

    def _run_release():
        try:
            _emit('update_log', {'message': '── GitHub Release ──'})

            # 1. Bump version
            old_ver = _get_current_version()
            new_ver = _bump_version(old_ver, bump)
            _emit('update_log', {'message': f'Wersja: {old_ver} → {new_ver}'})

            ver_file = os.path.join(INSTALL_DIR, 'backend', 'version.json')
            with open(ver_file) as f:
                ver_data = json.load(f)
            ver_data['version'] = new_ver
            ver_data['build_date'] = datetime.now().strftime('%Y-%m-%d')
            # Prepend new changelog entry
            changelog = ver_data.get('changelog', [])
            if not isinstance(changelog, list):
                changelog = []
            changelog.insert(0, {
                'version': new_ver,
                'date': ver_data['build_date'],
                'title': message,
                'changes': [message],
            })
            ver_data['changelog'] = changelog
            with open(ver_file, 'w') as f:
                json.dump(ver_data, f, indent=2, ensure_ascii=False)
            _emit('update_log', {'message': f'Zapisano version.json v{new_ver}'})

            # 2. Git commit
            git = f'git -C {_q(INSTALL_DIR)}'
            r = _host_run(f'{git} add -A', timeout=15)
            if r.returncode != 0:
                raise RuntimeError(f'git add failed: {r.stderr}')
            r = _host_run(f'{git} commit -m {_q(f"release: v{new_ver} — {message}")}', timeout=15)
            if r.returncode != 0 and 'nothing to commit' not in r.stdout + r.stderr:
                raise RuntimeError(f'git commit failed: {r.stderr}')
            _emit('update_log', {'message': f'git commit: release: v{new_ver}'})

            # 3. Git tag
            r = _host_run(f'{git} tag {_q(f"v{new_ver}")}', timeout=10)
            if r.returncode != 0 and 'already exists' not in r.stderr:
                raise RuntimeError(f'git tag failed: {r.stderr}')

            # 4. Push branch + tags
            _emit('update_log', {'message': 'git push…'})
            r = _host_run(f'{git} push origin HEAD', timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f'git push failed: {r.stderr}')
            r = _host_run(f'{git} push origin {_q(f"v{new_ver}")}', timeout=30)
            if r.returncode != 0:
                raise RuntimeError(f'git push tag failed: {r.stderr}')
            _emit('update_log', {'message': 'Push OK'})

            # 5. Build .tar.gz package
            _emit('update_log', {'message': 'Budowanie paczki .tar.gz…'})
            os.makedirs(PUBLISH_DIR, exist_ok=True)
            pkg_name = f'ethos-{new_ver}'
            pkg_filename = f'{pkg_name}.tar.gz'
            pkg_path = os.path.join(PUBLISH_DIR, pkg_filename)
            with tarfile.open(pkg_path, 'w:gz') as tar:
                for subdir in ['backend', 'frontend']:
                    src = os.path.join(INSTALL_DIR, subdir)
                    if os.path.exists(src):
                        tar.add(src, arcname=f'{pkg_name}/{subdir}')
            size = os.path.getsize(pkg_path)
            _emit('update_log', {'message': f'Paczka: {pkg_filename} ({size // 1024} KB)'})

            # 6. Create GitHub Release via API
            _emit('update_log', {'message': 'Tworzenie GitHub Release…'})
            release_body = {
                'tag_name': f'v{new_ver}',
                'name': f'v{new_ver} — {message}',
                'body': f'- {message}',
                'draft': False,
                'prerelease': False,
            }
            api_url = f'https://api.github.com/repos/{repo}/releases'
            req = _urllib_req.Request(
                api_url,
                data=json.dumps(release_body).encode(),
                headers={
                    'Authorization': f'Bearer {token}',
                    'Accept': 'application/vnd.github+json',
                    'Content-Type': 'application/json',
                    'User-Agent': 'EthOS-Updater',
                },
                method='POST',
            )
            with _urllib_req.urlopen(req, timeout=30) as resp:
                release = json.loads(resp.read().decode())
            release_id = release['id']
            upload_url = re.sub(r'\{.*\}', '', release.get('upload_url', ''))
            _emit('update_log', {'message': f'Release #{release_id} utworzony'})

            # 7. Upload asset
            _emit('update_log', {'message': f'Upload {pkg_filename}…'})
            with open(pkg_path, 'rb') as f:
                asset_data = f.read()
            upload_req = _urllib_req.Request(
                f'{upload_url}?name={pkg_filename}',
                data=asset_data,
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/octet-stream',
                    'User-Agent': 'EthOS-Updater',
                },
                method='POST',
            )
            with _urllib_req.urlopen(upload_req, timeout=120) as resp:
                asset = json.loads(resp.read().decode())
            _emit('update_log', {'message': f'Asset uploaded: {asset.get("browser_download_url", "")}'})
            _emit('update_log', {'message': f'✓ GitHub Release v{new_ver} gotowy!'})
            _emit('github_release_done', {'ok': True, 'version': new_ver, 'url': release.get('html_url', '')})

        except _urllib_err.HTTPError as e:
            body = e.read().decode()[:300]
            _emit('update_log', {'message': f'GitHub API error {e.code}: {body}', 'error': True})
            _emit('github_release_done', {'ok': False, 'error': f'GitHub API {e.code}: {body}'})
        except Exception as ex:
            _emit('update_log', {'message': f'Błąd: {ex}', 'error': True})
            _emit('github_release_done', {'ok': False, 'error': str(ex)})

    import gevent
    gevent.spawn(_run_release)
    return jsonify({'ok': True, 'message': 'Release w toku — sprawdź log'})


@update_bp.route('/github-detect-repo', methods=['GET'])
def github_detect_repo():
    """Auto-detect GitHub repo slug from git remote."""
    return jsonify({'repo': _detect_github_repo()})
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

