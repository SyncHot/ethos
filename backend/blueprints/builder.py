"""
EthOS — Builder Blueprint
Build releases, system images, and publish optional apps to GitHub.
All heavy operations run on the host and stream progress via SSE.

Endpoints:
  GET  /api/builder/info                  -> version info, existing releases
  GET  /api/builder/status                -> current build status
  POST /api/builder/cancel                -> cancel running build
  POST /api/builder/dismiss               -> dismiss build notification
  GET  /api/builder/history               -> build history
  POST /api/builder/history/clear         -> clear history
  GET  /api/builder/cache                 -> cache info
  DELETE /api/builder/cache               -> clear cache
  GET/PUT/DELETE /api/builder/spec        -> build spec CRUD
  GET  /api/builder/spec/defaults         -> default spec
  POST /api/builder/release               -> build release (SSE)
  POST /api/builder/image                 -> build image (SSE)
  GET  /api/builder/publish-config        -> GitHub publish config (token masked)
  PUT  /api/builder/publish-config        -> save GitHub publish config
  GET  /api/builder/publish-diff          -> compare local apps with GitHub
  POST /api/builder/publish-apps          -> publish changed apps to GitHub (SSE)
  GET  /api/builder/logs                  -> build log
  POST /api/builder/logs/clear            -> clear log
  POST /api/builder/delete                -> delete artifact
  GET  /api/builder/download              -> download artifact
  GET  /api/builder/signing-key           -> builder public key (PEM)
  GET  /api/builder/manifest              -> verify & return manifest JSON
  POST /api/builder/beacon                -> receive "I AM ALIVE" from a freshly booted EthOS VM (no auth)
  GET  /api/builder/beacon                -> return last received beacon info
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
import traceback
import sys
from datetime import date, datetime
from flask import Blueprint, jsonify, request, Response, stream_with_context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_base, host_run_stream as _host_run_stream_base, \
    app_path, data_path, log_path, q as _q
from utils import load_json as _load_json, save_json as _save_json, fmt_bytes, register_pkg_routes, \
    require_tools, check_tool
from blueprints.builder_spec import load_spec, save_spec, generate_default_spec, \
    spec_to_shell_vars, DEFAULT_SPEC
from blueprints.admin_required import admin_required

builder_bp = Blueprint('builder', __name__, url_prefix='/api/builder')

# ── Logging ──
LOG_DIR = log_path()
os.makedirs(LOG_DIR, exist_ok=True)

_logger = logging.getLogger('builder')
_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(os.path.join(LOG_DIR, 'builder.log'), encoding='utf-8')
_fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
_logger.addHandler(_fh)

# ── Build State (persistent across SSE reconnects) ──
_BUILD_STATE_FILE = data_path('builder_state.json')
_BUILD_HISTORY_FILE = data_path('build_history.json')
_MAX_STATE_LOGS = 500
_MAX_HISTORY = 50

_build_state = {
    'status': 'idle',       # idle | building | done | error
    'build_type': '',       # 'release' | 'image'
    'percent': 0,
    'message': '',
    'logs': [],
    'start_time': 0,
    'pid': 0,               # host PID of nsenter process
    'result': None,         # {success, message, img, iso} on completion
    'resume_available': False,  # True when failed build can be resumed
    'build_dir': '',        # WORK_DIR path for resume
    'preflight_result': '',  # 'ok'|'fail'|'timeout'|'skipped'|'disabled'
    'last_beacon': None,    # Last "I AM ALIVE" beacon received from a booted VM
}

# Beacon ID is set at build completion; the booted VM must include it in its POST.
_BEACON_FILE = data_path('builder_beacon.json')
_build_lock = threading.Lock()


def _save_build_state():
    """Persist build state to disk for crash recovery."""
    try:
        _save_json(_BUILD_STATE_FILE, _build_state)
    except Exception:
        pass


def _save_to_history():
    """Append completed build to persistent history."""
    try:
        history = _load_json(_BUILD_HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        entry = {
            'build_type': _build_state.get('build_type', ''),
            'status': _build_state.get('status', ''),
            'message': _build_state.get('message', ''),
            'result': _build_state.get('result'),
            'start_time': _build_state.get('start_time', 0),
            'end_time': time.time(),
        }
        if entry['start_time']:
            entry['duration'] = int(entry['end_time'] - entry['start_time'])
        history.append(entry)
        if len(history) > _MAX_HISTORY:
            history = history[-_MAX_HISTORY:]
        _save_json(_BUILD_HISTORY_FILE, history)
    except Exception:
        pass


def _load_build_state():
    """Load build state from disk on startup."""
    global _build_state
    try:
        saved = _load_json(_BUILD_STATE_FILE, None)
        if saved is None:
            return
        if saved.get('status') == 'building':
            pid = saved.get('pid', 0)
            if pid and _is_pid_alive(pid):
                _build_state.update(saved)
            else:
                saved['status'] = 'error'
                saved['message'] = 'Build interrupted (process terminated)'
                saved['result'] = {'success': False, 'message': 'Build interrupted after restart'}
                _build_state.update(saved)
        else:
            _build_state.update(saved)
    except Exception:
        pass


def _is_pid_alive(pid):
    """Check if a PID is running on the host."""
    if not pid or pid <= 0:
        return False
    try:
        r = _host_run(f"kill -0 {pid} 2>/dev/null && echo alive", timeout=5)
        return 'alive' in r.stdout
    except Exception:
        return False


def _update_build(status=None, percent=None, message=None, log=None, result=None, pid=None):
    """Thread-safe update of build state."""
    with _build_lock:
        if status is not None:
            _build_state['status'] = status
        if percent is not None:
            _build_state['percent'] = percent
        if message is not None:
            _build_state['message'] = message
        if log is not None:
            _build_state['logs'].append(log)
            if len(_build_state['logs']) > _MAX_STATE_LOGS:
                _build_state['logs'] = _build_state['logs'][-_MAX_STATE_LOGS:]
            _logger.info(log)
        if result is not None:
            _build_state['result'] = result
        if pid is not None:
            _build_state['pid'] = pid
        if status:
            _logger.info('[%s] %s', status, message or '')
        _save_build_state()
        if status in ('done', 'error'):
            _save_to_history()


def _reset_build(build_type=''):
    """Reset build state for a new build."""
    with _build_lock:
        _build_state.update({
            'status': 'building',
            'build_type': build_type,
            'percent': 0,
            'message': 'Rozpoczynanie...',
            'logs': [],
            'start_time': time.time(),
            'pid': 0,
            'result': None,
            'resume_available': False,
            'build_dir': '',
            'preflight_result': '',
        })
        _save_build_state()


# Load saved state on import
_load_build_state()

# ── Paths ──
_HOST_NASOS_DIR = None

# ── Optional app JS files (excluded from base image; installed via Package Center) ──
def _compute_optional_js():
    try:
        import importlib, sys as _sys
        _bp_dir = os.path.join(os.path.dirname(__file__))
        _sys.path.insert(0, os.path.join(_bp_dir, '..'))
        am = importlib.import_module('blueprints.app_manager')
        core_js = set()
        for aid in am.CORE_APPS:
            fn = am._get_frontend_filename(aid)
            if fn:
                core_js.add(fn + '.js')
        optional = set()
        for app in am.BUILTIN_CATALOG:
            if app['id'] not in am.CORE_APPS:
                fn = am._get_frontend_filename(app['id'])
                if fn and fn + '.js' not in core_js:
                    optional.add(fn + '.js')
        return sorted(optional)
    except Exception:
        return []

_OPTIONAL_JS = _compute_optional_js()


def _compute_optional_py():
    try:
        import importlib, sys as _sys
        _bp_dir = os.path.join(os.path.dirname(__file__))
        _sys.path.insert(0, os.path.join(_bp_dir, '..'))
        am = importlib.import_module('blueprints.app_manager')
        seen = set()
        result = []
        for app_id, (module_name, _, _, _) in am._OPTIONAL_BLUEPRINTS.items():
            # Don't strip core app backends — they must stay on disk
            if app_id in am.CORE_APPS:
                continue
            if module_name not in seen:
                seen.add(module_name)
                result.append(module_name + '.py')
        return sorted(result)
    except Exception:
        return []

_OPTIONAL_PY = _compute_optional_py()


def _get_host_nasos_dir():
    """Get the host path to the nasos project directory."""
    global _HOST_NASOS_DIR
    if _HOST_NASOS_DIR:
        return _HOST_NASOS_DIR
    _HOST_NASOS_DIR = app_path()
    return _HOST_NASOS_DIR


# ── Host helpers ──

def _host_run(cmd, timeout=60):
    return _host_run_base(cmd, timeout=timeout)


def _host_run_stream(cmd, track_pid=False):
    stream = _host_run_stream_base(f"renice -n 10 $$ 2>/dev/null; {cmd}")
    if track_pid and hasattr(stream, 'pid') and stream.pid:
        _update_build(pid=stream.pid)
    for line in stream:
        yield line


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ═══════════════════════════════════════════════════════════
#  API — Get current status / info
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/info')
def builder_info():
    """Return version info, existing releases and images."""
    nasos = _get_host_nasos_dir()

    # Read version.json
    version_data = {}
    vf = app_path('backend/version.json')
    if os.path.isfile(vf):
        with open(vf) as f:
            version_data = json.load(f)

    # List existing releases
    releases = []
    releases_dir = data_path('releases')
    host_releases_dir = f"{nasos}/installer/releases"
    # Check both local and host paths
    for rdir in [releases_dir, app_path('installer/releases')]:
        pass  # we'll use host_run for this

    r = _host_run(f"ls -la {nasos}/installer/releases/*.tar.gz 2>/dev/null | awk '{{print $5, $9}}'")
    if r.returncode == 0 and r.stdout.strip():
        for line in r.stdout.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                size = int(parts[0]) if parts[0].isdigit() else 0
                path = parts[1]
                name = os.path.basename(path)
                releases.append({'name': name, 'size': size, 'path': path})

    # List latest.json
    latest = None
    r2 = _host_run(f"cat {nasos}/installer/releases/latest.json 2>/dev/null")
    if r2.returncode == 0 and r2.stdout.strip():
        try:
            latest = json.loads(r2.stdout)
        except Exception:
            pass

    # List existing images
    images = []
    r3 = _host_run(f"ls -la {nasos}/installer/images/ethos-*.img 2>/dev/null | awk '{{print $5, $6, $7, $8, $9}}'")
    if r3.returncode == 0 and r3.stdout.strip():
        for line in r3.stdout.strip().splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 5:
                size = int(parts[0]) if parts[0].isdigit() else 0
                path = parts[4]
                name = os.path.basename(path)
                images.append({'name': name, 'size': size, 'path': path})

    # x86 build is always available (wrapper script is embedded in Python)
    scripts = ['build-x86-image.sh']

    # Images directory path for file manager
    images_dir = f"{nasos}/installer/images"

    return jsonify({
        'version': version_data,
        'releases': releases,
        'latest': latest,
        'images': images,
        'scripts': scripts,
        'nasos_dir': nasos,
        'images_dir': images_dir,
    })


# ═══════════════════════════════════════════════════════════
#  API — Build Status (for reconnecting clients)
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/status')
def build_status():
    """Return current build state. Clients poll this to reconnect to running builds."""
    with _build_lock:
        # Check if 'building' process is still alive
        if _build_state['status'] == 'building' and _build_state['pid']:
            if not _is_pid_alive(_build_state['pid']):
                _build_state['status'] = 'error'
                _build_state['message'] = 'Build interrupted (process terminated)'
                _build_state['result'] = {'success': False, 'message': 'Build interrupted'}
                _save_build_state()
        elapsed = 0
        if _build_state['start_time'] and _build_state['status'] == 'building':
            elapsed = int(time.time() - _build_state['start_time'])
        since = request.args.get('since', 0, type=int)
        logs = _build_state['logs'][since:] if since < len(_build_state['logs']) else []
        return jsonify({
            'status': _build_state['status'],
            'build_type': _build_state['build_type'],
            'percent': _build_state['percent'],
            'message': _build_state['message'],
            'logs': logs,
            'log_total': len(_build_state['logs']),
            'elapsed': elapsed,
            'result': _build_state['result'],
            'resume_available': _build_state.get('resume_available', False),
            'build_dir': _build_state.get('build_dir', ''),
            'preflight_result': _build_state.get('preflight_result', ''),
        })


@builder_bp.route('/cancel', methods=['POST'])
def cancel_build():
    """Cancel a running build by killing its process tree."""
    with _build_lock:
        if _build_state['status'] != 'building':
            return jsonify({'error': 'No active build'}), 400
        pid = _build_state['pid']
    if pid:
        # Kill the whole process group
        _host_run(f"kill -TERM -{pid} 2>/dev/null; sleep 1; kill -KILL -{pid} 2>/dev/null || kill -KILL {pid} 2>/dev/null", timeout=10)
    _update_build(status='error', message='Cancelled by user',
                  result={'success': False, 'message': 'Build cancelled'})
    return jsonify({'ok': True})


@builder_bp.route('/dismiss', methods=['POST'])
def dismiss_build():
    """Reset build state back to idle (dismiss done/error result)."""
    with _build_lock:
        if _build_state['status'] == 'building':
            return jsonify({'error': 'Build in progress — cannot dismiss'}), 409
        _build_state.update({
            'status': 'idle',
            'build_type': '',
            'percent': 0,
            'message': '',
            'logs': [],
            'pid': 0,
            'result': None,
        })
        _save_build_state()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════
#  API — Boot Beacon  (Success Beacon for E2E validation)
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/beacon', methods=['POST'])
def receive_beacon():
    """Receive an 'I AM ALIVE' signal from a freshly booted EthOS VM.

    No authentication required — the newly installed system doesn't have a
    session token yet.  The caller must supply ``build_id`` matching the
    beacon_id embedded in the image at build time.

    Body (JSON):
        build_id  — matches the beacon_id stored in _build_state at completion
        hostname  — hostname of the booted system
        version   — EthOS version string from /etc/os-release
        timestamp — Unix epoch (seconds) of first boot
        extras    — optional dict of additional diagnostic fields
    """
    body = request.get_json(force=True, silent=True) or {}
    build_id = str(body.get('build_id', '')).strip()
    if not build_id:
        return jsonify({'error': 'build_id required'}), 400

    beacon = {
        'build_id': build_id,
        'hostname': str(body.get('hostname', ''))[:128],
        'version': str(body.get('version', ''))[:64],
        'timestamp': body.get('timestamp', int(time.time())),
        'received_at': int(time.time()),
        'remote_addr': request.remote_addr,
        'extras': body.get('extras') if isinstance(body.get('extras'), dict) else {},
    }

    with _build_lock:
        _build_state['last_beacon'] = beacon

    try:
        _save_json(_BEACON_FILE, beacon)
    except Exception as exc:
        _logger.warning('beacon: could not persist: %s', exc)

    _logger.info('beacon: received from %s (build_id=%s)', beacon['hostname'], build_id)
    return jsonify({'ok': True, 'acknowledged': True})


@builder_bp.route('/beacon', methods=['GET'])
@admin_required
def get_beacon():
    """Return the last boot beacon received from a built image (auth required)."""
    with _build_lock:
        beacon = _build_state.get('last_beacon')

    if beacon is None:
        # Try loading from disk (survives server restart)
        beacon = _load_json(_BEACON_FILE, None)

    if beacon is None:
        return jsonify({'ok': True, 'beacon': None,
                        'message': 'No beacon received yet'})

    expected_id = _build_state.get('beacon_id', '')
    matched = bool(expected_id and beacon.get('build_id') == expected_id)
    return jsonify({'ok': True, 'beacon': beacon, 'build_id_matched': matched})


@builder_bp.route('/history')
def build_history():
    """Return persistent build history (last N builds)."""
    history = _load_json(_BUILD_HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []
    # Return newest first
    return jsonify({'ok': True, 'items': list(reversed(history))})


@builder_bp.route('/history/clear', methods=['POST'])
def clear_history():
    """Clear build history."""
    _save_json(_BUILD_HISTORY_FILE, [])
    return jsonify({'ok': True})


@builder_bp.route('/cache', methods=['GET'])
def cache_info():
    """Get build cache size."""
    r = _host_run("du -sh /var/cache/ethos-builder/debootstrap /var/cache/ethos-builder/apt 2>/dev/null || echo '0\t-'")
    lines = r.stdout.strip().splitlines()
    sizes = {}
    for l in lines:
        parts = l.split('\t')
        if len(parts) == 2:
            key = 'debootstrap' if 'debootstrap' in parts[1] else ('apt' if 'apt' in parts[1] else parts[1])
            sizes[key] = parts[0]
    return jsonify({'cache': sizes})


@builder_bp.route('/cache', methods=['DELETE'])
def cache_clear():
    """Clear build cache."""
    _host_run("rm -rf /var/cache/ethos-builder/debootstrap/* /var/cache/ethos-builder/apt/*", timeout=30)
    return jsonify({'status': 'ok'})


# ═══════════════════════════════════════════════════════════
#  API — Build Spec (Declarative YAML configuration)
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/spec', methods=['GET'])
def get_build_spec():
    """Return current build spec (merged defaults + user overrides)."""
    spec = load_spec()
    return jsonify({'ok': True, 'spec': spec})


@builder_bp.route('/spec', methods=['PUT'])
def update_build_spec():
    """Update build spec with provided values."""
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'error': 'No spec data provided'}), 400
    try:
        # Load current, merge updates, save
        spec = load_spec()
        for section, values in data.items():
            if section in spec and isinstance(spec[section], dict) and isinstance(values, dict):
                spec[section].update(values)
            else:
                spec[section] = values
        save_spec(spec)
        return jsonify({'ok': True, 'spec': spec})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@builder_bp.route('/spec', methods=['DELETE'])
def reset_build_spec():
    """Reset build spec to defaults."""
    try:
        import os as _os
        path = data_path('build-spec.yaml')
        if _os.path.isfile(path):
            _os.unlink(path)
        return jsonify({'ok': True, 'spec': DEFAULT_SPEC})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@builder_bp.route('/spec/defaults', methods=['GET'])
def get_default_spec():
    """Return the default build spec (unmodified)."""
    return jsonify({'ok': True, 'spec': DEFAULT_SPEC})


# ═══════════════════════════════════════════════════════════
#  API — Build Release (SSE)
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/release', methods=['POST'])
def build_release():
    """Build a release package. Streams progress via SSE."""
    if _build_state['status'] == 'building':
        return jsonify({'error': 'Build already in progress. Wait for completion or cancel.'}), 409
    data = request.get_json(silent=True) or {}
    bump = data.get('bump', '')  # patch, minor, major or empty
    changelog_title = data.get('changelog_title', '').strip()
    changelog_changes = data.get('changelog_changes', [])

    nasos = _get_host_nasos_dir()
    _reset_build('release')

    def generate():
        try:
            yield _sse({'type': 'step', 'message': 'Reading version...', 'percent': 5})
            _update_build(percent=5, message='Reading version...')

            # Read current version
            r = _host_run(f"cat {nasos}/backend/version.json")
            if r.returncode != 0:
                _update_build(status='error', message='Cannot read version.json')
                yield _sse({'type': 'done', 'success': False, 'message': 'Cannot read version.json'})
                return

            try:
                ver_data = json.loads(r.stdout)
            except Exception:
                _update_build(status='error', message='Error parsing version.json')
                yield _sse({'type': 'done', 'success': False, 'message': 'Error parsing version.json'})
                return

            current = ver_data.get('version', '0.0.0')
            yield _sse({'type': 'log', 'message': f'Aktualna wersja: {current}'})

            # Bump version
            if bump in ('patch', 'minor', 'major'):
                parts = current.split('.')
                maj, mi, pat = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
                if bump == 'major':
                    new_ver = f"{maj+1}.0.0"
                elif bump == 'minor':
                    new_ver = f"{maj}.{mi+1}.0"
                else:
                    new_ver = f"{maj}.{mi}.{pat+1}"
            else:
                new_ver = current

            yield _sse({'type': 'step', 'message': f'Wersja release: {new_ver}', 'percent': 10})

            # Update version.json if changed
            if new_ver != current or changelog_title:
                yield _sse({'type': 'log', 'message': 'Updating version.json...'})

                ver_data['version'] = new_ver
                ver_data['build_date'] = str(date.today())

                if changelog_title:
                    entry = {
                        'version': new_ver,
                        'date': str(date.today()),
                        'title': changelog_title,
                        'changes': changelog_changes if changelog_changes else [changelog_title],
                    }
                    cl = ver_data.get('changelog', [])
                    cl.insert(0, entry)
                    ver_data['changelog'] = cl

                # Write updated version.json via host
                import base64 as _b64
                raw = json.dumps(ver_data, indent=2, ensure_ascii=False).encode('utf-8')
                b64 = _b64.b64encode(raw).decode('ascii')
                _host_run(f"echo '{b64}' | base64 -d > {_q(nasos + '/backend/version.json')}")
                yield _sse({'type': 'log', 'message': f'version.json → {new_ver}'})

            # Build release package
            yield _sse({'type': 'step', 'message': 'Building release package...', 'percent': 20})

            pkg_name = f"ethos-{new_ver}"
            build_dir = f"/tmp/ethos-release-web-$$"
            releases_dir = f"{nasos}/installer/releases"
            optional_js = ' '.join(_OPTIONAL_JS)
            optional_py = ' '.join(_OPTIONAL_PY)

            # The build-release.sh is interactive. We run equivalent steps directly.
            script = f"""
set -e
BUILD_DIR="/tmp/ethos-release-web-{int(time.time())}"
NASOS="{nasos}"
PKG="{pkg_name}"
RELEASES="{releases_dir}"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/$PKG"/{{backend/blueprints,backend/middleware,backend/i18n,frontend/css,frontend/js/apps,frontend/vendor,frontend/mobile}}
mkdir -p "$RELEASES"

echo "STEP:25:Copying backend..."
cp "$NASOS/backend/"*.py "$BUILD_DIR/$PKG/backend/"
cp "$NASOS/backend/version.json" "$BUILD_DIR/$PKG/backend/"
cp "$NASOS/backend/requirements.txt" "$BUILD_DIR/$PKG/backend/"
# Copy only CORE blueprints — optional ones are installed via Package Center
OPTIONAL_PY="{optional_py}"
for py in "$NASOS/backend/blueprints/"*.py; do
  fname=$(basename "$py")
  if echo "$OPTIONAL_PY" | grep -qw "$fname"; then
    continue
  fi
  cp "$py" "$BUILD_DIR/$PKG/backend/blueprints/"
done
touch "$BUILD_DIR/$PKG/backend/blueprints/__init__.py"
cp "$NASOS/backend/middleware/"*.py "$BUILD_DIR/$PKG/backend/middleware/" 2>/dev/null || true
cp -r "$NASOS/backend/i18n/"* "$BUILD_DIR/$PKG/backend/i18n/" 2>/dev/null || true

echo "STEP:40:Copying frontend..."
cp "$NASOS/frontend/index.html" "$BUILD_DIR/$PKG/frontend/"
cp "$NASOS/frontend/share.html" "$BUILD_DIR/$PKG/frontend/" 2>/dev/null || true
cp "$NASOS/frontend/manifest.json" "$BUILD_DIR/$PKG/frontend/" 2>/dev/null || true
cp "$NASOS/frontend/css/"*.css "$BUILD_DIR/$PKG/frontend/css/"
cp "$NASOS/frontend/js/"*.js "$BUILD_DIR/$PKG/frontend/js/"
# Copy only CORE app JS files — optional apps are installed via Package Center
OPTIONAL_JS="{optional_js}"
for js in "$NASOS/frontend/js/apps/"*.js; do
  fname=$(basename "$js")
  if echo "$OPTIONAL_JS" | grep -qw "$fname"; then
    continue
  fi
  cp "$js" "$BUILD_DIR/$PKG/frontend/js/apps/"
done
cp -r "$NASOS/frontend/vendor/"* "$BUILD_DIR/$PKG/frontend/vendor/" 2>/dev/null || true
cp -r "$NASOS/frontend/mobile/"* "$BUILD_DIR/$PKG/frontend/mobile/" 2>/dev/null || true
cp -r "$NASOS/frontend/img" "$BUILD_DIR/$PKG/frontend/" 2>/dev/null || true

echo "STEP:50:Copying files..."

echo "STEP:60:Cleaning cache..."
find "$BUILD_DIR" -type d -name "__pycache__" -exec rm -rf {{}} + 2>/dev/null || true
find "$BUILD_DIR" -name "*.pyc" -delete 2>/dev/null || true

echo "STEP:70:Creating tar.gz archive..."
cd "$BUILD_DIR"
tar -czf "$RELEASES/$PKG.tar.gz" "$PKG/"

echo "STEP:80:Generowanie manifest..."
CHECKSUM=$(sha256sum "$RELEASES/$PKG.tar.gz" | awk '{{print $1}}')
FILESIZE=$(stat -c%s "$RELEASES/$PKG.tar.gz")
FILE_COUNT=$(tar -tzf "$RELEASES/$PKG.tar.gz" | wc -l)

echo "STEP:90:Zapis latest.json..."
cat > "$RELEASES/latest.json" << MANIFEST_EOF
{{
  "version": "{new_ver}",
  "build_date": "$(date -I)",
  "filename": "$PKG.tar.gz",
  "size": $FILESIZE,
  "sha256": "$CHECKSUM",
  "min_version": "1.0.0"
}}
MANIFEST_EOF

echo "STEP:100:Gotowe!"
echo "RESULT_SIZE:$FILESIZE"
echo "RESULT_FILES:$FILE_COUNT"
echo "RESULT_SHA:$CHECKSUM"

rm -rf "$BUILD_DIR"
"""
            result_info = {}
            for line in _host_run_stream(script, track_pid=True):
                line = line.rstrip('\n')
                if line.startswith('__EXIT_CODE__:'):
                    code = int(line.split(':')[1])
                    if code == 0:
                        size_h = _human_size(int(result_info.get('size', 0)))
                        files = result_info.get('files', '?')
                        msg = f'Release {new_ver} built! ({size_h}, {files} files)'
                        res = {'success': True, 'message': msg, 'version': new_ver}
                        _update_build(status='done', percent=100, message=msg, result=res)
                        yield _sse({
                            'type': 'done', 'success': True, 'percent': 100,
                            'message': msg,
                            'version': new_ver,
                        })
                    else:
                        msg = f'Build error (code: {code})'
                        _update_build(status='error', message=msg, result={'success': False, 'message': msg})
                        yield _sse({'type': 'done', 'success': False, 'message': msg})
                elif line.startswith('STEP:'):
                    parts = line.split(':', 2)
                    pct = int(parts[1]) if len(parts) > 1 else 0
                    msg = parts[2] if len(parts) > 2 else ''
                    _update_build(percent=pct, message=msg)
                    yield _sse({'type': 'step', 'message': msg, 'percent': pct})
                elif line.startswith('RESULT_SIZE:'):
                    result_info['size'] = line.split(':')[1]
                elif line.startswith('RESULT_FILES:'):
                    result_info['files'] = line.split(':')[1]
                elif line.startswith('RESULT_SHA:'):
                    result_info['sha'] = line.split(':')[1]
                elif line.strip():
                    _update_build(log=line)
                    yield _sse({'type': 'log', 'message': line})
        except Exception as e:
            msg = f'Exception: {e}'
            _update_build(status='error', message=msg, result={'success': False, 'message': msg})
            yield _sse({'type': 'done', 'success': False, 'message': msg})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ═══════════════════════════════════════════════════════════
#  API — Build Image (SSE)
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/resume-image', methods=['POST'])
@admin_required
def resume_image():
    """Resume a failed image build from the last checkpoint."""
    if _build_state['status'] == 'building':
        return jsonify({'error': 'Build already in progress.'}), 409
    if not _build_state.get('resume_available'):
        return jsonify({'error': 'No resumable build found.'}), 400
    build_dir = _build_state.get('build_dir', '/tmp/ethos-x86-build-web')
    import os
    ckpt_dir = os.path.join(build_dir, '.ckpts')
    if not os.path.isdir(ckpt_dir):
        return jsonify({'error': f'Build directory not found: {build_dir}'}), 400

    nasos = _get_host_nasos_dir()
    _reset_build('image')
    _update_build(message=f'Wznawianie z checkpointa...')

    t = threading.Thread(
        target=_build_image_worker,
        args=(nasos,),
        kwargs={'resume': True},
        daemon=True,
    )
    t.start()
    return jsonify({'status': 'ok', 'resumed': True})


@builder_bp.route('/image', methods=['POST'])
def build_image():
    """Build a bootable system image in background thread."""
    err = require_tools('debootstrap')
    if err:
        return err
    if _build_state['status'] == 'building':
        return jsonify({'error': 'Build already in progress. Wait for completion or cancel.'}), 409
    nasos = _get_host_nasos_dir()

    _reset_build('image')

    # Launch build in background thread so it survives SSE disconnects
    t = threading.Thread(
        target=_build_image_worker,
        args=(nasos,),
        daemon=True,
    )
    t.start()

    return jsonify({'status': 'ok'})


def _build_image_worker(nasos, resume=False):
    """Background worker that runs the x86 image build."""
    from blueprints.builder_resources import enter_build_slice, leave_build_slice
    enter_build_slice()
    try:
        wrapper = _x86_wrapper_script(nasos)
        if resume:
            wrapper = 'export ETHOS_RESUME=1\n' + wrapper

        start_time = time.time()
        result_info = {}

        for line in _host_run_stream(wrapper, track_pid=True):
            line = line.rstrip('\n')
            if line.startswith('__EXIT_CODE__:'):
                code = int(line.split(':')[1])
                elapsed = time.time() - start_time
                elapsed_m = int(elapsed // 60)
                elapsed_s = int(elapsed % 60)
                if code == 0:
                    img_size = _human_size(int(result_info.get('img_size', 0)))
                    msg = f'Image ready! IMG: {img_size}'
                    msg += f' (czas: {elapsed_m}min {elapsed_s}s)'
                    beacon_id = f"build-{int(start_time)}"
                    res = {
                        'success': True, 'message': msg,
                        'img': result_info.get('img_path', ''),
                        'beacon_id': beacon_id,
                    }
                    with _build_lock:
                        _build_state['beacon_id'] = beacon_id
                    _update_build(status='done', percent=100, message=msg, result=res)
                else:
                    msg = f'Image build error (code: {code}, time: {elapsed_m}min {elapsed_s}s)'
                    _update_build(status='error', message=msg, result={'success': False, 'message': msg})
            elif line.startswith('STEP:'):
                parts = line.split(':', 2)
                pct = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                msg = parts[2] if len(parts) > 2 else ''
                _update_build(percent=pct, message=msg)
            elif line.startswith('RESULT_IMG:'):
                p = line.split(':')
                result_info['img_path'] = p[1] if len(p) > 1 else ''
                result_info['img_size'] = p[2] if len(p) > 2 else '0'
            elif line.startswith('LOG:RESUME_AVAILABLE:'):
                build_dir = line[len('LOG:RESUME_AVAILABLE:'):]
                with _build_lock:
                    _build_state['resume_available'] = True
                    _build_state['build_dir'] = build_dir.strip()
                    _save_build_state()
            elif line.startswith('PREFLIGHT_RESULT:'):
                pf_res = line[len('PREFLIGHT_RESULT:'):].strip()
                with _build_lock:
                    _build_state['preflight_result'] = pf_res
                    _save_build_state()
                _update_build(log=f'Pre-flight result: {pf_res}')
            elif line.startswith('LOG:'):
                msg = line[4:]
                _update_build(log=msg)
            elif line.strip():
                _update_build(log=line)
    except Exception as e:
        msg = f'Exception: {e}'
        _update_build(status='error', message=msg, result={'success': False, 'message': msg})
    finally:
        leave_build_slice()



# ─────────────────────────────────────────────────────────
#  x86 wrapper script — debootstrap + GRUB
# ─────────────────────────────────────────────────────────

def _x86_wrapper_script(nasos: str) -> str:
    """Return bash wrapper script for building x86 image."""
    optional_js_list = ' '.join(_OPTIONAL_JS)
    optional_py_list = ' '.join(_OPTIONAL_PY)

    # Load declarative build spec
    spec = load_spec()
    spec_vars = spec_to_shell_vars(spec)

    return f"""
set -e
set -o pipefail
export DEBIAN_FRONTEND=noninteractive

NASOS="{nasos}"

# ── Declarative build spec (from data/build-spec.yaml) ──
{spec_vars}

# Check dependencies
echo "STEP:2:Checking dependencies..."
for cmd in debootstrap parted mkfs.ext4 mkfs.vfat grub-install; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "STEP:3:Installing dependencies..."
        apt-get update -qq
        apt-get install -y -qq debootstrap parted dosfstools e2fsprogs \\
            grub-efi-amd64-bin grub-common grub2-common \\
            mtools xorriso isolinux debian-archive-keyring squashfs-tools zstd cryptsetup-bin 2>/dev/null || true
        break
    fi
done

# Ensure the appropriate archive keyring is present
if [ "$BASE_DISTRO" = "ubuntu" ]; then
    # Ubuntu debootstrap uses --no-check-gpg since ubuntu-keyring may be unavailable on Debian hosts
    echo "LOG:Ubuntu build — skipping keyring check (using --no-check-gpg for debootstrap)"
else
    if [ ! -f /usr/share/keyrings/debian-archive-keyring.gpg ]; then
        echo "LOG:Installing debian-archive-keyring..."
        apt-get update -qq 2>/dev/null
        apt-get install -y -qq debian-archive-keyring 2>/dev/null || true
    fi
fi

echo "STEP:5:Preparing environment..."

# Version from version.json (not overridden by spec)
VERSION=$(python3 -c "import json; print(json.load(open('$NASOS/backend/version.json'))['version'])" 2>/dev/null || echo '2.4.0')
FINAL_IMG="$NASOS/installer/images/ethos-x86.img"
WORK_DIR="/tmp/ethos-x86-build-web"

# ── Performance: use tmpfs (RAM) for build if enough memory ──
# VM-aware: subtract RAM already used by running QEMU processes.
MEM_AVAIL_MB=$(awk '/MemAvailable/{{print int($2/1024)}}' /proc/meminfo 2>/dev/null || echo 0)
VM_RAM_MB=$(ps -eo rss,comm --no-headers 2>/dev/null | awk '/qemu/{{s+=$1}} END{{print int(s/1024)}}')
VM_RAM_MB=${{VM_RAM_MB:-0}}
EFFECTIVE_RAM_MB=$(( MEM_AVAIL_MB - VM_RAM_MB - 512 ))
USE_TMPFS=0
if [ "$EFFECTIVE_RAM_MB" -gt "$TMPFS_MIN_RAM_MB" ]; then
    USE_TMPFS=1
    echo "LOG:RAM: avail=${{MEM_AVAIL_MB}}MB vms=${{VM_RAM_MB}}MB effective=${{EFFECTIVE_RAM_MB}}MB — building in tmpfs"
    mkdir -p "$WORK_DIR"
    mount -t tmpfs -o size=${{IMG_SIZE_GB}}G,nr_inodes=0 tmpfs "$WORK_DIR"
else
    echo "LOG:RAM: avail=${{MEM_AVAIL_MB}}MB vms=${{VM_RAM_MB}}MB effective=${{EFFECTIVE_RAM_MB}}MB — building on disk"
    mkdir -p "$WORK_DIR"
fi
OUTPUT_IMG="$WORK_DIR/ethos-x86.img"
CKPT_DIR="$WORK_DIR/.ckpts"
BUILD_DONE=0

# ── Stage checkpoint helpers (idempotent builds) ──
_ckpt_done() {{ [ -f "$CKPT_DIR/$1" ]; }}
_ckpt_set()  {{ mkdir -p "$CKPT_DIR"; touch "$CKPT_DIR/$1"; echo "LOG:✓ Stage checkpoint: $1"; }}

RESUME_MODE="${{ETHOS_RESUME:-0}}"
if [ "$RESUME_MODE" = "1" ] && [ -d "$CKPT_DIR" ]; then
    echo "LOG:Resume mode — existing checkpoints: $(ls "$CKPT_DIR/" 2>/dev/null | tr '\n' ' ')"
fi

# ── Build cache directories (persist across builds) ──
DEBOOTSTRAP_CACHE="/var/cache/ethos-builder/debootstrap"
APT_CACHE="/var/cache/ethos-builder/apt"
mkdir -p "$DEBOOTSTRAP_CACHE" "$APT_CACHE"

# Cleanup function
cleanup() {{
    sync 2>/dev/null || true
    # Unmount in reverse order, use lazy unmount for stubborn mounts
    for m in var/cache/apt/archives boot/efi run sys proc dev/shm dev/pts dev; do
        umount "$WORK_DIR/root/$m" 2>/dev/null || \
            umount -l "$WORK_DIR/root/$m" 2>/dev/null || true
    done
    sleep 1
    umount "$WORK_DIR/root" 2>/dev/null || \
        umount -l "$WORK_DIR/root" 2>/dev/null || true
    umount "$WORK_DIR/efi" 2>/dev/null || true
    sleep 1
    if [[ -n "${{LOOP_DEV:-}}" ]]; then
        losetup -d "$LOOP_DEV" 2>/dev/null || true
    fi
    if [ "$BUILD_DONE" = "1" ]; then
        # Success — clean up completely
        if [ "$USE_TMPFS" -eq 1 ] && mountpoint -q "$WORK_DIR" 2>/dev/null; then
            umount "$WORK_DIR" 2>/dev/null || \
                umount -l "$WORK_DIR" 2>/dev/null || true
        fi
        rm -rf "$WORK_DIR" 2>/dev/null || true
    else
        # Failure — preserve WORK_DIR for resume
        echo "LOG:RESUME_AVAILABLE:$WORK_DIR"
        if [ "$USE_TMPFS" -eq 1 ]; then
            echo "LOG:Note: Build dir is in tmpfs — checkpoints survive crash but not reboot"
        fi
    fi
}}
trap cleanup EXIT

# ── Re-mount helper (used when resuming from checkpoint) ──
_remount_for_resume() {{
    echo "LOG:Remounting build artifacts for resume..."
    mkdir -p "$WORK_DIR/root" "$WORK_DIR/efi"
    LOOP_DEV=$(losetup --find --show --partscan "$OUTPUT_IMG" 2>/dev/null) || {{
        echo "LOG:ERROR: Cannot attach loop device to $OUTPUT_IMG"; exit 1;
    }}
    echo "LOG:Loop device: $LOOP_DEV"
    mount "${{LOOP_DEV}}p2" "$WORK_DIR/root" || {{ echo "LOG:ERROR: Cannot mount root partition"; exit 1; }}
    mkdir -p "$WORK_DIR/root/boot/efi"
    mount "${{LOOP_DEV}}p1" "$WORK_DIR/root/boot/efi" 2>/dev/null || true
    echo "LOG:Disk remounted for resume"
}}

# ── Step 1: Create disk image ──
if _ckpt_done "01_disk"; then
    echo "STEP:8:Resuming — disk image exists"
    _remount_for_resume
    echo "STEP:14:Obraz dysku (z checkpointa)"
else
    echo "STEP:8:Creating disk image (${{IMG_SIZE_GB}}GB)..."
    mkdir -p "$WORK_DIR"/{{root,efi}}
    rm -f "$OUTPUT_IMG" "$FINAL_IMG"
    truncate -s "${{IMG_SIZE_GB}}G" "$OUTPUT_IMG"

    LOOP_DEV=$(losetup --find --show --partscan "$OUTPUT_IMG")
    echo "LOG:Loop device: $LOOP_DEV"

    parted -s "$LOOP_DEV" mklabel gpt
    parted -s "$LOOP_DEV" mkpart ESP fat32 1MiB ${{ESP_SIZE_MB}}MiB
    parted -s "$LOOP_DEV" set 1 esp on
    parted -s "$LOOP_DEV" mkpart primary ext4 ${{ESP_SIZE_MB}}MiB $((${{ESP_SIZE_MB}} + ${{ROOT_SIZE_MB}}))MiB
    partprobe "$LOOP_DEV"
    # Wait for partition devices to appear (up to 10s, 0.5s steps)
    for _pnum in 1 2; do
        _waited=0
        while [ ! -b "${{LOOP_DEV}}p${{_pnum}}" ] && [ $_waited -lt 20 ]; do
            sleep 0.5; _waited=$((_waited + 1))
        done
        if [ ! -b "${{LOOP_DEV}}p${{_pnum}}" ]; then
            echo "LOG:ERROR: Partition ${{LOOP_DEV}}p${{_pnum}} not ready after 10s"
            exit 1
        fi
    done

    mkfs.vfat -F32 "${{LOOP_DEV}}p1"
    mkfs.ext4 -q -L "ethos-root" "${{LOOP_DEV}}p2"

    mount "${{LOOP_DEV}}p2" "$WORK_DIR/root"
    mkdir -p "$WORK_DIR/root/boot/efi"
    mount "${{LOOP_DEV}}p1" "$WORK_DIR/root/boot/efi"

    echo "STEP:14:Obraz dysku utworzony"
    _ckpt_set "01_disk"
fi

# ── Step 2: Debootstrap ──
if _ckpt_done "02_debootstrap"; then
    echo "STEP:45:${{BASE_DISTRO^}} base system present (checkpoint — skipped)"
else
    if [ "$BASE_DISTRO" = "ubuntu" ]; then
        echo "STEP:15:Debootstrap — minimal Ubuntu install (this will take a few minutes)..."
        DEBOOTSTRAP_MIRROR="http://archive.ubuntu.com/ubuntu/"
        DEBOOTSTRAP_EXTRA_OPTS="--no-check-gpg --components=main,restricted,universe"
    else
        echo "STEP:15:Debootstrap — minimal Debian install (this will take a few minutes)..."
        DEBOOTSTRAP_MIRROR="http://deb.debian.org/debian"
        DEBOOTSTRAP_EXTRA_OPTS=""
    fi
    PKG_COUNT=0
    if [ -d "$DEBOOTSTRAP_CACHE" ] && [ "$(ls -A "$DEBOOTSTRAP_CACHE" 2>/dev/null)" ]; then
        echo "LOG:Using debootstrap cache ($(du -sh "$DEBOOTSTRAP_CACHE" | cut -f1))"
    fi
    DEBS_LOG="/tmp/ethos-debootstrap-$$.log"
    debootstrap --cache-dir="$DEBOOTSTRAP_CACHE" --variant=minbase $DEBOOTSTRAP_EXTRA_OPTS --include=\\
$DEBOOTSTRAP_INCLUDE \\
        "$DEBIAN_RELEASE" "$WORK_DIR/root" "$DEBOOTSTRAP_MIRROR" 2>&1 | \\
        tee "$DEBS_LOG" | \\
        while IFS= read -r line; do
            if echo "$line" | grep -qE "^I: Retrieving"; then
                PKG_COUNT=$((PKG_COUNT + 1))
                if (( PKG_COUNT % 20 == 0 )); then
                    echo "LOG:Downloading packages... ($PKG_COUNT downloaded)"
                fi
            elif echo "$line" | grep -qE "^I: Validating"; then
                echo "LOG:$line"
            elif echo "$line" | grep -qE "^I: Extracting"; then
                PKG_COUNT=$((PKG_COUNT + 1))
                if (( PKG_COUNT % 30 == 0 )); then
                    echo "LOG:Extracting... ($PKG_COUNT)"
                fi
            elif echo "$line" | grep -qE "^I: Unpacking|^I: Configuring"; then
                echo "LOG:$line"
            elif echo "$line" | grep -qE "^I: |^W: |^E: "; then
                echo "LOG:$line"
            fi
        done
    DEBS_RC=${{PIPESTATUS[0]}}
    if [ "$DEBS_RC" -ne 0 ]; then
        echo "LOG:ERROR: debootstrap failed (exit $DEBS_RC)"
        tail -5 "$DEBS_LOG" 2>/dev/null | while IFS= read -r _l; do echo "LOG:DEBS> $_l"; done
        rm -f "$DEBS_LOG"
        echo "STEP:45:Debootstrap failed"
        exit 1
    fi
    rm -f "$DEBS_LOG"

    # Verify debootstrap succeeded
    if [ ! -d "$WORK_DIR/root/dev" ] || [ ! -d "$WORK_DIR/root/etc" ]; then
        echo "LOG:ERROR: debootstrap did not create rootfs — check logs"
        echo "STEP:45:Debootstrap failed"
        exit 1
    fi

    echo "STEP:45:${{BASE_DISTRO^}} installed. Configuring system..."
    _ckpt_set "02_debootstrap"
fi

# ── Step 3: Configure system ──
ROOT="$WORK_DIR/root"
echo "LOG:Bind mount /dev, /proc, /sys, /run..."
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount --bind /dev/shm "$ROOT/dev/shm" 2>/dev/null || true
mount -t proc proc "$ROOT/proc"
mount -t sysfs sysfs "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/run"

# Bind-mount apt cache for faster rebuilds
mkdir -p "$ROOT/var/cache/apt/archives"
mount --bind "$APT_CACHE" "$ROOT/var/cache/apt/archives"
echo "LOG:Apt cache bind-mounted ($(du -sh "$APT_CACHE" 2>/dev/null | cut -f1) cached)"

# DNS for chroot — essential for apt-get
# Host resolv.conf may be systemd-resolved stub (127.0.0.53) which won't work in chroot
if [ -f /run/systemd/resolve/resolv.conf ]; then
    cp /run/systemd/resolve/resolv.conf "$ROOT/etc/resolv.conf"
    echo "LOG:DNS: copied resolv.conf from host"
else
    echo "nameserver 8.8.8.8" > "$ROOT/etc/resolv.conf"
    echo "nameserver 1.1.1.1" >> "$ROOT/etc/resolv.conf"
    echo "LOG:DNS: using 8.8.8.8 / 1.1.1.1"
fi

# Fix any broken packages left by debootstrap (polkitd etc.)
echo "LOG:Fixing packages after debootstrap..."
chroot "$ROOT" dpkg --configure -a 2>&1 | tail -3 || true
chroot "$ROOT" bash -c 'DEBIAN_FRONTEND=noninteractive apt --fix-broken install -y' 2>&1 | tail -3 || true
echo "LOG:Packages fixed"

# Install network-manager in chroot (needs systemd bind-mounts for polkitd)
echo "LOG:Installing network-manager in chroot..."
chroot "$ROOT" apt-get update -qq 2>&1 | tail -3 || true
chroot "$ROOT" bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq network-manager dbus-user-session' 2>&1 | tail -5 || echo "LOG:network-manager install issue"

echo "LOG:Creating fstab, hostname, locale..."
ROOT_UUID=$(blkid -s UUID -o value "${{LOOP_DEV}}p2")
EFI_UUID=$(blkid -s UUID -o value "${{LOOP_DEV}}p1")

if [ -z "$ROOT_UUID" ]; then
    echo "LOG:ERROR: Failed to read root partition UUID (${{LOOP_DEV}}p2)"
    exit 1
fi
if [ -z "$EFI_UUID" ]; then
    echo "LOG:ERROR: Failed to read EFI partition UUID (${{LOOP_DEV}}p1)"
    exit 1
fi

cat > "$ROOT/etc/fstab" <<FSTAB
UUID=$ROOT_UUID  /          ext4  noatime,errors=remount-ro  0 1
UUID=$EFI_UUID   /boot/efi  vfat  umask=0077         0 1
FSTAB

echo "$DEFAULT_HOSTNAME" > "$ROOT/etc/hostname"

# NOTE: Swap is intentionally NOT created in the build image.
# The installer generates its own fstab with swap on the data partition.
# Keeping the root partition small allows dd-based fast cloning.

# ── I/O tuning for low-power NAS hardware ──
echo "LOG:Konfiguracja I/O tuning..."
cat > "$ROOT/etc/sysctl.d/90-ethos-nas.conf" <<'IOTUNE'
# EthOS NAS Tuning
vm.swappiness = 10
vm.dirty_ratio = 40
vm.dirty_background_ratio = 10
vm.vfs_cache_pressure = 50
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
vm.min_free_kbytes = 65536
vm.dirty_expire_centisecs = 1500
vm.dirty_writeback_centisecs = 1500
kernel.nmi_watchdog = 0
net.ipv4.ip_forward = 1
IOTUNE

# ── Security hardening: sysctl ──
cat > "$ROOT/etc/sysctl.d/91-ethos-security.conf" <<'SECSYSCTL'
# Kernel pointer hardening
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
kernel.perf_event_paranoid = 3
kernel.unprivileged_bpf_disabled = 1
# Reverse-path filtering (spoofing protection)
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
# SYN flood protection
net.ipv4.tcp_syncookies = 1
# Disable ICMP redirects
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
# Disable source routing
net.ipv4.conf.all.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
SECSYSCTL

# ── Security: kernel module blacklist ──
cat > "$ROOT/etc/modprobe.d/ethos-security-blacklist.conf" <<'MODBLK'
# Disable DMA-capable bus interfaces (potential physical attack vectors)
blacklist firewire-core
blacklist thunderbolt
# Disable uncommon/legacy filesystems (attack surface reduction)
blacklist cramfs
blacklist freevxfs
blacklist jffs2
blacklist hfs
blacklist hfsplus
blacklist udf
install cramfs /bin/true
install freevxfs /bin/true
install jffs2 /bin/true
install hfs /bin/true
install hfsplus /bin/true
install udf /bin/true
# Disable uncommon network protocols
blacklist dccp
blacklist sctp
blacklist rds
blacklist tipc
install dccp /bin/true
install sctp /bin/true
install rds /bin/true
install tipc /bin/true
MODBLK

cat > "$ROOT/etc/udev/rules.d/99-ethos-power.rules" <<'UDEV_PWR'
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="1", RUN+="/sbin/hdparm -S 242 /dev/%k"
UDEV_PWR

cat > "$ROOT/etc/udev/rules.d/99-ethos-readahead.rules" <<'UDEV'
SUBSYSTEM=="block", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="1", RUN+="/sbin/blockdev --setra 4096 /dev/%k"
SUBSYSTEM=="block", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="0", RUN+="/sbin/blockdev --setra 256 /dev/%k"
SUBSYSTEM=="block", KERNEL=="nvme*", RUN+="/sbin/blockdev --setra 256 /dev/%k"
UDEV

# I/O scheduler: BFQ for HDD (better for mixed workloads), none for NVMe
cat > "$ROOT/etc/udev/rules.d/60-ethos-scheduler.rules" <<'UDEV_SCHED'
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="1", ATTR{{queue/scheduler}}="bfq"
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="0", ATTR{{queue/scheduler}}="none"
ACTION=="add|change", KERNEL=="nvme[0-9]*n[0-9]*", TEST=="queue/scheduler", ATTR{{queue/scheduler}}="none"
UDEV_SCHED

# Logrotate policy for EthOS logs
cat > "$ROOT/etc/logrotate.d/ethos" <<'LOGROTATE'
/opt/ethos/logs/*.log {{
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    maxsize 50M
}}

/opt/ethos/logs/copilot_tickets/*.log {{
    monthly
    rotate 2
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    maxsize 100M
}}
LOGROTATE
cat > "$ROOT/etc/hosts" <<HOSTS
127.0.0.1   localhost
127.0.1.1   $DEFAULT_HOSTNAME
::1         localhost ip6-localhost ip6-loopback
HOSTS

echo "en_US.UTF-8 UTF-8" > "$ROOT/etc/locale.gen"
echo "pl_PL.UTF-8 UTF-8" >> "$ROOT/etc/locale.gen"
chroot "$ROOT" locale-gen >/dev/null 2>&1
echo 'LANG=en_US.UTF-8' > "$ROOT/etc/default/locale"
ln -sf /usr/share/zoneinfo/Europe/Warsaw "$ROOT/etc/localtime"

if [ "$BASE_DISTRO" = "ubuntu" ]; then
cat > "$ROOT/etc/apt/sources.list" <<APT
deb http://archive.ubuntu.com/ubuntu $DEBIAN_RELEASE main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu $DEBIAN_RELEASE-updates main restricted universe multiverse
deb http://security.ubuntu.com/ubuntu $DEBIAN_RELEASE-security main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu $DEBIAN_RELEASE-backports main restricted universe multiverse
APT
else
cat > "$ROOT/etc/apt/sources.list" <<APT
deb http://deb.debian.org/debian $DEBIAN_RELEASE main contrib non-free non-free-firmware
deb http://deb.debian.org/debian $DEBIAN_RELEASE-updates main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security $DEBIAN_RELEASE-security main contrib non-free non-free-firmware
deb http://deb.debian.org/debian $DEBIAN_RELEASE-backports main contrib non-free non-free-firmware
APT
fi

echo "LOG:Creating user $DEFAULT_USER..."
PASS_HASH=$(openssl passwd -6 "$USER_PASS")
chroot "$ROOT" useradd -m -s /bin/bash -G sudo -p "$PASS_HASH" "$DEFAULT_USER"
ALLOWED_CMDS="/opt/ethos/tools/ethos-system-helper.sh, /opt/ethos/tools/ethos-power-*, /usr/bin/systemctl restart ethos, /usr/sbin/smartctl, /usr/bin/docker"
echo "${{DEFAULT_USER}} ALL=(ALL) NOPASSWD: ${{ALLOWED_CMDS}}" > "$ROOT/etc/sudoers.d/010_ethos"
chmod 440 "$ROOT/etc/sudoers.d/010_ethos"
chroot "$ROOT" groupadd -f ethos-admin
chroot "$ROOT" groupadd -f ethos-user
chroot "$ROOT" usermod -aG ethos-admin,ethos-user "$DEFAULT_USER"
chroot "$ROOT" systemctl enable ssh || true
chroot "$ROOT" systemctl enable smartmontools || true
chroot "$ROOT" systemctl enable nut-server || true
chroot "$ROOT" systemctl enable NetworkManager || true

# ── SMART Monitoring Configuration ──
echo "LOG:Konfiguracja SMART Monitoring..."
cat > "$ROOT/etc/smartd.conf" <<'EOF'
DEVICESCAN -a -o on -S on -n standby,q -s (S/../../7/02|L/../01/./03) -W 4,50,55 -R 199 -m root -M exec /etc/smartmontools/run.d/ethos-notify
EOF

mkdir -p "$ROOT/etc/smartmontools/run.d"
cat > "$ROOT/etc/smartmontools/run.d/ethos-notify" <<'EOF'
#!/bin/bash
# EthOS S.M.A.R.T. Alert Hook
# Triggered by smartd on disk issues

API_URL="http://localhost:9000/api"

# Log to EventLog
if [ -n "$SMARTD_MESSAGE" ]; then
    curl -s -X POST "$API_URL/eventlog" \
      -H "Content-Type: application/json" \
      -d "{{
        \"category\": \"storage\",
        \"level\": \"warning\",
        \"message\": \"SMART Alert: $SMARTD_DEVICE\",
        \"detail\": {{
            \"device\": \"$SMARTD_DEVICE\",
            \"message\": \"$SMARTD_MESSAGE\",
            \"failtype\": \"$SMARTD_FAILTYPE\",
            \"full_message\": \"$SMARTD_FULLMESSAGE\"
        }}
      }}"
fi

# Trigger backup on critical attributes
# Reallocated, Pending, Uncorrectable, or failure
DO_BACKUP=0

case "$SMARTD_MESSAGE" in
    *Reallocated_Sector_Ct*|*Current_Pending_Sector*|*Offline_Uncorrectable*)
        DO_BACKUP=1
        ;;
    *UDMA_CRC_Error_Count*)
        # Just log, don't trigger panic backup for cable errors
        ;;
esac

if [ -n "$SMARTD_FAILTYPE" ] && [ "$SMARTD_FAILTYPE" != "EmailTest" ]; then
    DO_BACKUP=1
fi

if [ "$DO_BACKUP" -eq 1 ]; then
    curl -s -X POST "$API_URL/backup/trigger-smart" \
      -H "Content-Type: application/json" \
      -d "{{}}"
fi
EOF
chmod +x "$ROOT/etc/smartmontools/run.d/ethos-notify"

chroot "$ROOT" systemctl disable networking 2>/dev/null || true
chroot "$ROOT" systemctl enable avahi-daemon 2>/dev/null || true
chroot "$ROOT" systemctl enable serial-getty@ttyS0.service 2>/dev/null || true

# ── Fail2Ban Configuration ──
echo "LOG:Konfiguracja Fail2Ban (SSH, Samba, Web)..."
cat > "$ROOT/etc/fail2ban/jail.local" <<'F2B'
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5
backend = systemd
ignoreip = 127.0.0.1/8 ::1 192.168.0.0/16 10.0.0.0/8
# Persistent database
dbfile = /opt/ethos/data/fail2ban.sqlite3
dbpurgeage = 86400
action = %(action_)s
         ethos-eventlog

[sshd]
enabled = true

[samba]
enabled = true
port = 139,445
filter = samba
logpath = /var/log/samba/log.*
backend = auto

[ethos-web]
enabled = true
port = 9000
filter = ethos-web
logpath = /opt/ethos/logs/access.log
backend = auto
F2B

mkdir -p "$ROOT/etc/fail2ban/action.d"
cat > "$ROOT/etc/fail2ban/action.d/ethos-eventlog.conf" <<'ACT'
[Definition]
actionban = /opt/ethos/tools/fail2ban_eventlog.py <name> <ip> <failures>
ACT

mkdir -p "$ROOT/etc/fail2ban/filter.d"
cat > "$ROOT/etc/fail2ban/filter.d/ethos-web.conf" <<'WEB'
[Definition]
failregex = ^<HOST> - - \[.*\] ".*" (401|403) .*$
ignoreregex =
WEB

chroot "$ROOT" systemctl enable fail2ban || true

# ── UFW Firewall ──
echo "LOG:Configuring UFW firewall..."
LAN="192.168.0.0/16"
chroot "$ROOT" bash -c 'command -v ufw &>/dev/null || apt-get install -y -qq ufw' 2>&1 | tail -3
chroot "$ROOT" ufw default deny incoming 2>/dev/null || true
chroot "$ROOT" ufw default allow outgoing 2>/dev/null || true
chroot "$ROOT" ufw allow from $LAN to any port 22 proto tcp comment 'SSH' 2>/dev/null || true
chroot "$ROOT" ufw allow from $LAN to any port 9000 proto tcp comment 'EthOS Web UI' 2>/dev/null || true
chroot "$ROOT" ufw allow from $LAN to any port 80,443 proto tcp comment 'HTTP / HTTPS' 2>/dev/null || true
# NOTE: Do NOT enable UFW here — during installer mode (USB boot) there is
# no firewall needed (open hotspot). UFW is enabled by the installer when
# it writes the system to the target disk (system_ops.configure_services).
# Enabling UFW in chroot can also produce broken iptables state.
echo "LOG:UFW rules configured (will be enabled after installation)"

# ── SSH Hardening ──
echo "LOG:SSH hardening..."
mkdir -p "$ROOT/etc/ssh/sshd_config.d"
cat > "$ROOT/etc/ssh/sshd_config.d/ethos-hardening.conf" <<'SSHH'
# EthOS SSH Hardening
PermitRootLogin no
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
PermitEmptyPasswords no
SSHH

# Gate SSH login until the default password is changed via the Web UI.
# ForceCommand runs check_password_changed.sh which blocks or exec's the shell.
cat >> "$ROOT/etc/ssh/sshd_config" <<'SSHGATE'

# EthOS: block SSH until default password changed via Web UI
Match User *
    ForceCommand /opt/ethos/tools/check_password_changed.sh
SSHGATE

# ── Force password change on first boot ──
rm -f "$ROOT/opt/ethos/.password_changed"

# ── USB automount (devmon/udevil) ──
echo "LOG:Konfiguracja devmon USB automount..."
chroot "$ROOT" bash -c 'id devmon &>/dev/null || useradd -r -s /usr/sbin/nologin -d /media/devmon devmon' 2>/dev/null || true
mkdir -p "$ROOT/media/devmon"
chroot "$ROOT" chown devmon:root /media/devmon
chroot "$ROOT" chmod 755 /media/devmon
cat > "$ROOT/etc/systemd/system/devmon@.service" <<'DEVMONSVC'
[Unit]
Description=devmon USB automounter for %i
After=local-fs.target

[Service]
Type=simple
User=%i
ExecStart=/usr/bin/devmon --no-gui
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
DEVMONSVC
ln -sf /etc/systemd/system/devmon@.service "$ROOT/etc/systemd/system/multi-user.target.wants/devmon@devmon.service"
echo "LOG:devmon USB automount OK"

# NetworkManager config for AP shared mode (dnsmasq) and WiFi scan
mkdir -p "$ROOT/etc/NetworkManager/conf.d"
cat > "$ROOT/etc/NetworkManager/conf.d/00-ethos.conf" <<'NMCFG'
[main]
dns=dnsmasq

[device]
wifi.scan-rand-mac-address=no
NMCFG
# Lock root account — random password + lock
ROOT_PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)
chroot "$ROOT" bash -c "echo \"root:${{ROOT_PASS}}\" | chpasswd"
chroot "$ROOT" passwd -l root

# ── Branding: /etc/os-release ──
cat > "$ROOT/etc/os-release" <<OSREL
PRETTY_NAME="$BRAND_NAME v${{VERSION}}"
NAME="$BRAND_NAME"
VERSION_ID="${{VERSION}}"
VERSION="${{VERSION}}"
ID=ethos
ID_LIKE=$BASE_DISTRO
HOME_URL="https://ethos.local"
OSREL

cat > "$ROOT/etc/issue" <<ISSUE
$BRAND_NAME \\n \\l

ISSUE
echo "$BRAND_NAME" > "$ROOT/etc/issue.net"

# ── Full Debian branding purge ──
# lsb-release
cat > "$ROOT/etc/lsb-release" <<LSBREL
DISTRIB_ID=EthOS
DISTRIB_RELEASE=${{VERSION}}
DISTRIB_CODENAME=ethos
DISTRIB_DESCRIPTION="$BRAND_NAME v${{VERSION}}"
LSBREL
# /usr/lib/os-release (canonical path, symlinked by many tools)
if [ -d "$ROOT/usr/lib" ]; then
    cp "$ROOT/etc/os-release" "$ROOT/usr/lib/os-release" 2>/dev/null || true
fi
# Neutralise /etc/debian_version (content doesn't matter for EthOS)
echo "ethos/${{VERSION}}" > "$ROOT/etc/debian_version" 2>/dev/null || true
# Replace dpkg origin so dpkg --version shows EthOS
if [ -d "$ROOT/etc/dpkg/origins" ]; then
    cat > "$ROOT/etc/dpkg/origins/ethos" <<DPKGORIG
Vendor: EthOS
Vendor-URL: https://ethos.local
Bugs: https://ethos.local/bugs
Parent: Debian
DPKGORIG
    ln -sf ethos "$ROOT/etc/dpkg/origins/default" 2>/dev/null || true
fi
# Remove Debian motd snippets that would reveal base distro
rm -f "$ROOT/etc/update-motd.d/10-uname" 2>/dev/null || true
cat > "$ROOT/etc/motd" <<MOTD

  Welcome to $BRAND_NAME v${{VERSION}}
  https://ethos.local

MOTD

# ── GRUB defaults (so update-grub keeps EthOS name) ──
cat > "$ROOT/etc/default/grub" <<GRUBDEF
GRUB_DEFAULT=0
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="EthOS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet net.ifnames=0 biosdevname=0"
GRUB_CMDLINE_LINUX=""
GRUBDEF

echo "STEP:52:System configured"

# ── Step 4: GRUB ──
echo "STEP:53:Installing GRUB (UEFI)..."

echo "LOG:apt-get update in chroot..."
chroot "$ROOT" apt-get update -qq 2>&1 | tail -3 || true
echo "LOG:Installing GRUB packages..."
if [ "$BASE_DISTRO" = "ubuntu" ]; then
    DEBIAN_FRONTEND=noninteractive chroot "$ROOT" apt-get install -y -qq grub-efi-amd64 grub-efi-amd64-bin grub-common grub2-common 2>&1 | tail -5 || true
else
    DEBIAN_FRONTEND=noninteractive chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports grub-efi-amd64 grub-efi-amd64-bin grub-common grub2-common 2>&1 | tail -5 || true
fi
DEBIAN_FRONTEND=noninteractive chroot "$ROOT" apt-get install -y -qq efibootmgr 2>&1 | tail -5 || true

mkdir -p "$ROOT/boot/efi/EFI/BOOT"
echo "LOG:GRUB UEFI install..."
chroot "$ROOT" grub-install --target=x86_64-efi --efi-directory=/boot/efi \\
    --boot-directory=/boot --removable --no-nvram 2>&1 | tail -5
GRUB_RC=${{PIPESTATUS[0]}}
GRUB_EFI_BIN="$ROOT/boot/efi/EFI/BOOT/BOOTX64.EFI"
if [ "$GRUB_RC" -ne 0 ] || [ ! -f "$GRUB_EFI_BIN" ]; then
    echo "LOG:ERROR: grub-install failed (rc=$GRUB_RC) or BOOTX64.EFI missing"
    ls -la "$ROOT/boot/efi/EFI/" 2>/dev/null | while IFS= read -r _l; do echo "LOG:EFI> $_l"; done
    echo "STEP:0:ERROR: UEFI grub-install failed!"
    exit 1
fi

KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||")
INITRD=$(ls "$ROOT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||")

mkdir -p "$ROOT/boot/grub"
cat > "$ROOT/boot/grub/grub.cfg" <<GRUBCFG
set timeout=3
set default=0
insmod part_gpt
insmod ext2
insmod gzio
menuentry "EthOS v${{VERSION}}" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro quiet net.ifnames=0 biosdevname=0 fsck.repair=preen console=ttyS0,115200n8
    initrd ${{INITRD}}
}}
menuentry "EthOS v${{VERSION}} (recovery)" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro single nomodeset fsck.repair=preen
    initrd ${{INITRD}}
}}
GRUBCFG

cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/EFI/BOOT/grub.cfg"

# Copy kernel + initrd to ESP recovery directory
mkdir -p "$ROOT/boot/efi/EFI/recovery"
cp "$ROOT/boot/${{KERN##*/}}" "$ROOT/boot/efi/EFI/recovery/vmlinuz" 2>/dev/null || true
cp "$ROOT/boot/${{INITRD##*/}}" "$ROOT/boot/efi/EFI/recovery/initrd.img" 2>/dev/null || true
echo "LOG:Recovery kernel copied to ESP"

echo "STEP:60:GRUB installed"

# From here on, individual failures should not abort the whole build
set +e

# ── Step 5: Install dependencies (native) ──
echo "STEP:61:Installing dependencies..."
echo "LOG:apt-get update in chroot..."
chroot "$ROOT" apt-get update -qq 2>&1 | tail -3 || echo "LOG:apt-get update failed but continuing"

echo "LOG:Installing minimal packages..."
chroot "$ROOT" apt-get install -y -qq \
    python3 python3-pip python3-venv \
    avahi-daemon \
    wpasupplicant dnsmasq rfkill \
    cloud-guest-utils \
    udevil udisks2 \
    zstd cron \
    gnupg age \
    2>&1 | tail -10 || echo "LOG:Some packages skipped"

echo "LOG:Installing firmware..."
if [ "$BASE_DISTRO" = "ubuntu" ]; then
    chroot "$ROOT" apt-get install -y -qq linux-firmware bluez 2>&1 | tail -5 || echo "LOG:Some firmware skipped"
else
    chroot "$ROOT" apt-get install -y -qq \
        firmware-atheros firmware-realtek firmware-brcm80211 \
        firmware-misc-nonfree firmware-linux-nonfree bluez firmware-intel-sound \
        2>&1 | tail -10 || echo "LOG:Some firmware skipped"
fi

# All other packages (storage tools, sensors, printer, archives, etc.)
# are installed lazily by EthOS (ensure_dep) when user enables features.
# Builder tools are pre-installed so image creation works out of the box.
echo "LOG:Installing builder tools..."
chroot "$ROOT" apt-get install -y -qq \
    debootstrap squashfs-tools xorriso isolinux \
    parted dosfstools e2fsprogs btrfs-progs mtools \
    2>&1 | tail -5 || echo "LOG:Some builder tools skipped"

echo "STEP:73:Installing kernel and firmware updates..."

# First clean apt cache to free space before big installs
chroot "$ROOT" apt-get clean 2>/dev/null || true
echo "LOG:Disk usage before kernel/firmware update:"
df -h "$ROOT" 2>/dev/null | tail -1 || true

if [ "$BASE_DISTRO" = "ubuntu" ]; then
    echo "LOG:Ubuntu: upgrading kernel and linux-firmware if newer available..."
    chroot "$ROOT" apt-get install -y -qq --only-upgrade linux-image-generic linux-firmware 2>&1 | tail -5 || echo "LOG:Kernel/firmware upgrade skipped"
else
    echo "LOG:Installing linux-image-amd64 from backports..."
    chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports linux-image-amd64 2>&1 | tail -5 || echo "LOG:Backports kernel skipped"
fi

# Remove OLD kernel to save ~200MB and avoid initramfs for 2 kernels
OLD_KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | head -1 | sed 's|.*/vmlinuz-||')
NEW_KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | sed 's|.*/vmlinuz-||')
if [[ -n "$OLD_KERN" && -n "$NEW_KERN" && "$OLD_KERN" != "$NEW_KERN" ]]; then
    echo "LOG:Removing old kernel $OLD_KERN (keeping $NEW_KERN)"
    chroot "$ROOT" apt-get remove -y --purge "linux-image-$OLD_KERN" 2>&1 | tail -3 || true
    rm -f "$ROOT/boot/vmlinuz-$OLD_KERN" "$ROOT/boot/initrd.img-$OLD_KERN" "$ROOT/boot/System.map-$OLD_KERN" "$ROOT/boot/config-$OLD_KERN" 2>/dev/null
    rm -rf "$ROOT/lib/modules/$OLD_KERN" 2>/dev/null
    echo "LOG:Old kernel removed"
fi

# Refresh grub.cfg + BOOTX64.EFI now that the latest kernel is active
KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||")
INITRD=$(ls "$ROOT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||")
echo "LOG:Refreshing GRUB config for kernel: $KERN"
cat > "$ROOT/boot/grub/grub.cfg" <<GRUBCFG
set timeout=3
set default=0
insmod part_gpt
insmod ext2
insmod gzio
menuentry "EthOS v${{VERSION}}" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro quiet net.ifnames=0 biosdevname=0 fsck.repair=preen console=ttyS0,115200n8
    initrd ${{INITRD}}
}}
menuentry "EthOS v${{VERSION}} (recovery)" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro single nomodeset fsck.repair=preen
    initrd ${{INITRD}}
}}
GRUBCFG
cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/EFI/BOOT/grub.cfg"
# Update recovery kernel on ESP
cp "$ROOT/boot/${{KERN##*/}}" "$ROOT/boot/efi/EFI/recovery/vmlinuz" 2>/dev/null || true
cp "$ROOT/boot/${{INITRD##*/}}" "$ROOT/boot/efi/EFI/recovery/initrd.img" 2>/dev/null || true
# Re-run grub-install to refresh BOOTX64.EFI modules
chroot "$ROOT" grub-install --target=x86_64-efi --efi-directory=/boot/efi \
    --boot-directory=/boot --removable --no-nvram 2>/dev/null || echo "LOG:grub-install refresh skipped"
echo "LOG:GRUB refreshed for latest kernel"

if [ "$BASE_DISTRO" = "ubuntu" ]; then
    echo "LOG:Ubuntu: linux-firmware already up-to-date from main repos"
else
    echo "LOG:Installing firmware-iwlwifi from backports..."
    chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-iwlwifi 2>&1 | tail -5 || echo "LOG:Backports iwlwifi skipped"
    echo "LOG:Installing firmware-realtek from backports..."
    chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-realtek 2>&1 | tail -5 || echo "LOG:Backports realtek skipped"
    echo "LOG:Installing firmware-misc-nonfree from backports..."
    chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-misc-nonfree 2>&1 | tail -5 || echo "LOG:Backports misc skipped"
fi

# Disable standalone dnsmasq (NM uses its own for AP mode)
chroot "$ROOT" systemctl disable dnsmasq 2>/dev/null || true
chroot "$ROOT" systemctl mask dnsmasq 2>/dev/null || true

# Install rfkill (needed for WiFi unblock)
chroot "$ROOT" apt-get install -y -qq rfkill 2>/dev/null || true

# Clean apt cache before initramfs rebuild to maximize free space
chroot "$ROOT" apt-get clean 2>/dev/null || true
rm -rf "$ROOT/var/lib/apt/lists/"* 2>/dev/null || true
echo "LOG:Disk usage before initramfs:"
df -h "$ROOT" 2>/dev/null | tail -1 || true

# ── SquashFS + OverlayFS initramfs hooks ──
# These hooks allow the installed system to boot from a read-only squashfs
# with a persistent overlay on the ext4 root partition.
echo "LOG:Adding SquashFS overlay boot support to initramfs..."
mkdir -p "$ROOT/etc/initramfs-tools/hooks"
mkdir -p "$ROOT/etc/initramfs-tools/scripts/local-bottom"

cat > "$ROOT/etc/initramfs-tools/hooks/ethos-overlay" <<'HOOKEOF'
#!/bin/sh
PREREQ=""
prereqs() {{ echo "$PREREQ"; }}
case "$1" in prereqs) prereqs; exit 0 ;; esac
. /usr/share/initramfs-tools/hook-functions
manual_add_modules squashfs
manual_add_modules overlay
manual_add_modules loop
manual_add_modules dm_verity
manual_add_modules dm_mod
manual_add_modules btrfs
copy_exec /sbin/losetup /sbin
copy_exec /sbin/blkid /sbin
# dm-verity support (optional — only if veritysetup is installed)
if [ -x /sbin/veritysetup ]; then
    copy_exec /sbin/veritysetup /sbin
fi
HOOKEOF
chmod +x "$ROOT/etc/initramfs-tools/hooks/ethos-overlay"

cat > "$ROOT/etc/initramfs-tools/scripts/local-bottom/ethos-overlay" <<'OVERLAYEOF'
#!/bin/sh
# EthOS SquashFS + OverlayFS boot script
# Lower layer: read-only squashfs on Root-A/B partition
# Upper layer: writable overlay — prefers EthOS-Data btrfs partition (like Synology),
#              falls back to Root-A/B ext4 partition if data disk unavailable.
# Activated by kernel cmdline: ethos.rootfs=squashfs
PREREQ=""
prereqs() {{ echo "$PREREQ"; }}
case "$1" in prereqs) prereqs; exit 0 ;; esac
grep -q "ethos.rootfs=squashfs" /proc/cmdline || exit 0
[ -f "${{rootmnt}}/root.sqsh" ] || exit 0
modprobe -q squashfs 2>/dev/null || true
modprobe -q overlay 2>/dev/null || true
modprobe -q loop 2>/dev/null || true
modprobe -q btrfs 2>/dev/null || true
mkdir -p /run/ethos-rootfs
mount --move "${{rootmnt}}" /run/ethos-rootfs

# Determine boot slot (a or b) from cmdline
SLOT="a"
for arg in $(cat /proc/cmdline); do
    case "$arg" in ethos.slot=*) SLOT="${{arg#ethos.slot=}}" ;; esac
done

# ── Try to use EthOS-Data btrfs partition for overlay (Synology-style) ──
DATA_UPPER=""
DATA_DEV=$(blkid -L EthOS-Data 2>/dev/null)
if [ -n "$DATA_DEV" ]; then
    DATA_MNT=/run/ethos-data
    mkdir -p "$DATA_MNT"
    if mount -t btrfs -o subvol=@data,noatime "$DATA_DEV" "$DATA_MNT" 2>/dev/null; then
        UPPER="$DATA_MNT/ethos/overlay/$SLOT/upper"
        WORK="$DATA_MNT/ethos/overlay/$SLOT/work"
        mkdir -p "$UPPER" "$WORK"
        DATA_UPPER=1
    fi
fi

# ── Fallback: use overlay dirs on Root-A/B partition ──
if [ -z "$DATA_UPPER" ]; then
    UPPER=/run/ethos-rootfs/overlay/upper
    WORK=/run/ethos-rootfs/overlay/work
    mkdir -p "$UPPER" "$WORK"
fi

# dm-verity integrity check (optional — runs if roothash and verity data exist)
VERITY_OK=0
ROOTHASH_FILE="/run/ethos-rootfs/boot/efi/EFI/ethos/roothash"
VERITY_FILE="/run/ethos-rootfs/root.sqsh.verity"
SQSH_FILE="/run/ethos-rootfs/root.sqsh"
if [ -f "$ROOTHASH_FILE" ] && [ -f "$VERITY_FILE" ] && command -v veritysetup >/dev/null 2>&1; then
    modprobe -q dm_verity 2>/dev/null || true
    modprobe -q dm_mod 2>/dev/null || true
    ROOTHASH=$(cat "$ROOTHASH_FILE")
    LOOP_DEV=$(losetup --find --show "$SQSH_FILE")
    HASH_DEV=$(losetup --find --show "$VERITY_FILE")
    if veritysetup open --hash-offset=0 "$LOOP_DEV" ethos-verity "$HASH_DEV" "$ROOTHASH" 2>/dev/null; then
        mkdir -p /run/ethos-sqsh
        if mount -t squashfs -o ro /dev/mapper/ethos-verity /run/ethos-sqsh 2>/dev/null; then
            VERITY_OK=1
        else
            veritysetup close ethos-verity 2>/dev/null
        fi
    fi
    if [ "$VERITY_OK" = "0" ]; then
        losetup -d "$LOOP_DEV" 2>/dev/null
        losetup -d "$HASH_DEV" 2>/dev/null
        echo "ethos-overlay: dm-verity verification FAILED — falling back to unverified mount"
    fi
fi

# Standard mount (no verity or verity unavailable)
if [ "$VERITY_OK" = "0" ]; then
    mkdir -p /run/ethos-sqsh
    if ! mount -t squashfs -o ro,loop "$SQSH_FILE" /run/ethos-sqsh 2>/dev/null; then
        mount --move /run/ethos-rootfs "${{rootmnt}}"
        exit 0
    fi
fi

if ! mount -t overlay overlay \
    -o "lowerdir=/run/ethos-sqsh,upperdir=$UPPER,workdir=$WORK" \
    "${{rootmnt}}" 2>/dev/null; then
    umount /run/ethos-sqsh 2>/dev/null
    [ "$VERITY_OK" = "1" ] && veritysetup close ethos-verity 2>/dev/null
    [ -n "$DATA_UPPER" ] && umount /run/ethos-data 2>/dev/null
    mount --move /run/ethos-rootfs "${{rootmnt}}"
    exit 0
fi
mkdir -p "${{rootmnt}}/.squashfs" "${{rootmnt}}/.rootfs"
mount --move /run/ethos-rootfs "${{rootmnt}}/.rootfs"
mount --move /run/ethos-sqsh "${{rootmnt}}/.squashfs"
# Expose data partition mount inside the new root for runtime use
if [ -n "$DATA_UPPER" ]; then
    mkdir -p "${{rootmnt}}/run/ethos-data"
    mount --move /run/ethos-data "${{rootmnt}}/run/ethos-data"
fi
OVERLAYEOF
chmod +x "$ROOT/etc/initramfs-tools/scripts/local-bottom/ethos-overlay"

# Rebuild initramfs with firmware + overlay hooks (only for the new kernel)
echo "LOG:Przebudowa initramfs..."
if [[ -n "$NEW_KERN" ]]; then
    chroot "$ROOT" update-initramfs -u -k "$NEW_KERN" 2>&1 | tail -5 || echo "LOG:initramfs update failed"
else
    chroot "$ROOT" update-initramfs -u -k all 2>/dev/null || echo "LOG:initramfs update failed"
fi

echo "STEP:75:Dependencies installed"
_ckpt_set "05_apt_deps"

# ── SBOM: generate Software Bill of Materials ──
echo "LOG:Generating SBOM (SPDX-2.3)..."
python3 - "$ROOT" "$VERSION" "$BRAND_NAME" "$WORK_DIR" <<'SBOMPY'
import sys, os
sys.path.insert(0, '/opt/ethos/backend/blueprints')
try:
    from builder_sbom import generate_sbom, write_sbom
    rootfs, version, brand, outdir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    sbom = generate_sbom(rootfs, version, brand)
    write_sbom(sbom, outdir)
    print(f"LOG:SBOM: {{len(sbom.get('packages', []))}} packages documented", flush=True)
except Exception as e:
    print(f"LOG:SBOM generation skipped: {{e}}", flush=True)
SBOMPY

# ── Step 6: Inject EthOS (full package) ──
echo "STEP:76:Injecting EthOS..."

ETHOS_DIR="$ROOT/opt/ethos"
mkdir -p "$ETHOS_DIR"/{{data,backups,logs,uploads,cups-config}}

# ── Copy entire backend/ ──
echo "LOG:Copying backend..."
cp -r "$NASOS/backend" "$ETHOS_DIR/"
rm -rf "$ETHOS_DIR/backend/__pycache__" "$ETHOS_DIR/backend/blueprints/__pycache__"
rm -f "$ETHOS_DIR/backend/blueprints/"*.bak 2>/dev/null || true

# ── License & compliance ──
for f in LICENSE NOTICE; do
  [ -f "$NASOS/$f" ] && cp "$NASOS/$f" "$ETHOS_DIR/"
done

# ── Copy entire frontend/ ──
echo "LOG:Copying frontend..."
cp -r "$NASOS/frontend" "$ETHOS_DIR/"
# Remove optional app JS files — they are installed via Package Center
OPTIONAL_JS="{optional_js_list}"
for fname in $OPTIONAL_JS; do
  rm -f "$ETHOS_DIR/frontend/js/apps/$fname"
  rm -f "$ETHOS_DIR/frontend_dist/js/apps/$fname" 2>/dev/null || true
done
echo "LOG:Optional app JS removed from base image ($(echo $OPTIONAL_JS | wc -w) files)"
# Remove optional blueprint .py files — installed via Package Center
OPTIONAL_PY="{optional_py_list}"
for fname in $OPTIONAL_PY; do
  rm -f "$ETHOS_DIR/backend/blueprints/$fname"
done

# ── Copy tools ──
echo "LOG:Copying tools..."
mkdir -p "$ETHOS_DIR/tools"
cp "$NASOS/tools/ethos-power-config.sh" "$ETHOS_DIR/tools/"
cp "$NASOS/tools/ethos-system-helper.sh" "$ETHOS_DIR/tools/"
cp "$NASOS/tools/ethos-power.service" "$ETHOS_DIR/tools/"
cp "$NASOS/tools/ethos-power-blacklist.conf" "$ETHOS_DIR/tools/"
# Security scripts — SSH password gate + fail2ban event logger
cp "$NASOS/tools/check_password_changed.sh" "$ETHOS_DIR/tools/" 2>/dev/null || echo "WARN:check_password_changed.sh not found"
cp "$NASOS/tools/fail2ban_eventlog.py"      "$ETHOS_DIR/tools/" 2>/dev/null || echo "WARN:fail2ban_eventlog.py not found"
chmod +x "$ETHOS_DIR/tools/ethos-power-config.sh"
chmod +x "$ETHOS_DIR/tools/ethos-system-helper.sh"
chmod +x "$ETHOS_DIR/tools/check_password_changed.sh" 2>/dev/null || true
chmod +x "$ETHOS_DIR/tools/fail2ban_eventlog.py" 2>/dev/null || true

# ── CUPS config ──
if [[ -d "$NASOS/cups-config" ]]; then
    cp -r "$NASOS/cups-config/"* "$ETHOS_DIR/cups-config/" 2>/dev/null || true
fi

# ── Installer scripts (for future updates) ──
mkdir -p "$ETHOS_DIR/installer/images"
cp "$NASOS/installer/"*.sh         "$ETHOS_DIR/installer/"     2>/dev/null || true
cp "$NASOS/installer/images/"*.sh     "$ETHOS_DIR/installer/images/" 2>/dev/null || true

# ── Flask-based preboot installer ──
echo "LOG:Copying Flask preboot installer..."
if [[ -d "$NASOS/installer/preboot" ]]; then
    cp -r "$NASOS/installer/preboot" "$ETHOS_DIR/installer/preboot"
    find "$ETHOS_DIR/installer/preboot" -type d -name "__pycache__" -exec rm -rf {{}} + 2>/dev/null || true
    echo "LOG:Flask preboot installer copied — $(du -sh "$ETHOS_DIR/installer/preboot" | awk '{{print $1}}')"
else
    echo "LOG:ERROR — Flask preboot installer not found at $NASOS/installer/preboot"
    exit 1
fi

# ── Clean cache from copied code ──
find "$ETHOS_DIR" -type d -name "__pycache__" -exec rm -rf {{}} + 2>/dev/null || true
find "$ETHOS_DIR" -name "*.pyc" -delete 2>/dev/null || true
rm -rf "$ETHOS_DIR/tests" "$ETHOS_DIR/logs" "$ETHOS_DIR/backups" 2>/dev/null || true

echo "LOG:Files copied — $(du -sh "$ETHOS_DIR" | awk '{{print $1}}')"

# ── Python venv + environment file ──
echo "LOG:Creating Python venv..."
# Install build deps needed by some pip packages (pyudev needs libudev-dev)
chroot "$ROOT" apt-get install -y -qq libudev-dev libffi-dev 2>&1 | tail -3 || echo "LOG:build deps issue"
chroot "$ROOT" python3 -m venv /opt/ethos/venv 2>&1 | tail -3 || echo "LOG:venv creation issue"
echo "LOG:pip install requirements..."
chroot "$ROOT" /opt/ethos/venv/bin/pip install --no-cache-dir -r /opt/ethos/backend/requirements.txt 2>&1 | tail -15 || echo "LOG:pip install issue"
# Verify critical imports work
chroot "$ROOT" /opt/ethos/venv/bin/python -c "import flask; import psutil; import gevent; import pyudev; print('OK: all imports')" 2>&1 || echo "LOG:WARNING: Some Python modules missing!"

# Remove build deps no longer needed (saves ~50MB)
chroot "$ROOT" apt-get remove -y --purge libudev-dev libffi-dev 2>&1 | tail -3 || true
chroot "$ROOT" apt-get autoremove -y -qq 2>&1 | tail -3 || true

cat > "$ETHOS_DIR/ethos.env" <<ENVFILE
NAS_NAME=EthOS
PORT=$NAS_PORT
ETHOS_ROOT=/opt/ethos
BACKUP_DIR=/opt/ethos/backups
ETHOS_BUILD_ID=build-$(date +%s)
ENVFILE
# Inject build host for QA beacon (only if ETHOS_QA_BUILD_HOST is set in the host env)
if [[ -n "${{ETHOS_QA_BUILD_HOST:-}}" ]]; then
    echo "ETHOS_BUILD_HOST=${{ETHOS_QA_BUILD_HOST}}" >> "$ETHOS_DIR/ethos.env"
fi
chmod 640 "$ETHOS_DIR/ethos.env"

cat > "$ETHOS_DIR/start.sh" <<'MGMT_STARTSH'
#!/bin/bash
sudo systemctl start ethos
echo "EthOS uruchomiony"
MGMT_STARTSH

cat > "$ETHOS_DIR/stop.sh" <<'MGMT_STOPSH'
#!/bin/bash
sudo systemctl stop ethos
echo "EthOS zatrzymany"
MGMT_STOPSH

cat > "$ETHOS_DIR/rebuild.sh" <<'MGMT_REBSH'
#!/bin/bash
cd "$(dirname "$0")"
./venv/bin/pip install --quiet --no-cache-dir -r backend/requirements.txt
sudo systemctl restart ethos
echo "EthOS przebudowany i uruchomiony"
MGMT_REBSH

chmod +x "$ETHOS_DIR"/{{start,stop,rebuild}}.sh

# ── install.conf ──
cat > "$ETHOS_DIR/install.conf" <<INSTCFG
ETHOS_USER="$DEFAULT_USER"
ETHOS_HOSTNAME="$DEFAULT_HOSTNAME"
ETHOS_NAS_NAME="$BRAND_NAME"
ETHOS_BRAND_NAME="$BRAND_NAME"
ETHOS_PORT=$NAS_PORT
ETHOS_SETUP_WIZARD=yes
INSTCFG

# ── Installer-mode marker: USB is an installer, not a live OS ──
touch "$ETHOS_DIR/.installer-mode"
echo "LOG:Installer-mode marker created"

# ── WiFi AP script ──
echo "LOG:Copying ethos-ap.sh..."
cp "$NASOS/installer/images/ethos-ap.sh" "$ROOT/usr/local/bin/ethos-ap"
chmod +x "$ROOT/usr/local/bin/ethos-ap"
if [[ ! -f "$ROOT/usr/local/bin/ethos-ap" ]]; then
    echo "LOG:ERROR — ethos-ap not copied!"
    ls -la "$NASOS/installer/images/ethos-ap.sh" 2>&1 || true
    exit 1
fi
echo "LOG:ethos-ap.sh OK"

# ── Firstboot script (copy from source — simplified v2) ──
echo "LOG:Copying firstboot-v2.sh..."
if [[ -f "$NASOS/installer/images/firstboot-v2.sh" ]]; then
    cp "$NASOS/installer/images/firstboot-v2.sh" "$ROOT/opt/ethos-firstboot.sh"
elif [[ -f "$NASOS/installer/images/firstboot.sh" ]]; then
    echo "LOG:WARNING — firstboot-v2.sh not found, falling back to firstboot.sh"
    cp "$NASOS/installer/images/firstboot.sh" "$ROOT/opt/ethos-firstboot.sh"
else
    echo "LOG:ERROR — no firstboot script found!"
    exit 1
fi
chmod +x "$ROOT/opt/ethos-firstboot.sh"
if [[ ! -f "$ROOT/opt/ethos-firstboot.sh" ]]; then
    echo "LOG:ERROR — firstboot.sh not copied!"
    exit 1
fi
echo "LOG:firstboot.sh OK"

# ── Diagnostic script ──
echo "LOG:Copying ethos-diag.sh..."
if [[ -f "$NASOS/installer/images/ethos-diag.sh" ]]; then
    cp "$NASOS/installer/images/ethos-diag.sh" "$ROOT/usr/local/bin/ethos-diag"
    chmod +x "$ROOT/usr/local/bin/ethos-diag"
    echo "LOG:ethos-diag OK"
else
    echo "LOG:WARNING — ethos-diag.sh not found (skipping)"
fi

# ── Firstboot systemd service ──
cat > "$ROOT/etc/systemd/system/ethos-firstboot.service" <<SVCUNIT
[Unit]
Description=EthOS First Boot Installer
After=network.target ethos-preboot.service
Wants=network.target
ConditionPathExists=/opt/ethos-firstboot.sh
ConditionPathExists=!/opt/ethos/.installed
ConditionPathExists=!/opt/ethos/.installer-mode
[Service]
Type=oneshot
ExecStart=/bin/bash /opt/ethos-firstboot.sh
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=1800
[Install]
WantedBy=multi-user.target
SVCUNIT
ln -sf /etc/systemd/system/ethos-firstboot.service "$ROOT/etc/systemd/system/multi-user.target.wants/ethos-firstboot.service"

# WiFi AP service
cat > "$ROOT/etc/systemd/system/ethos-ap.service" <<'APSVC'
[Unit]
Description=EthOS WiFi Hotspot (auto if no network)
After=NetworkManager.service
Wants=NetworkManager.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/ethos-ap auto
ExecStop=/usr/local/bin/ethos-ap stop
[Install]
WantedBy=multi-user.target
APSVC
ln -sf /etc/systemd/system/ethos-ap.service "$ROOT/etc/systemd/system/multi-user.target.wants/ethos-ap.service"

# Pre-boot setup server (Flask-based installer with i18n + offline fonts)
echo "LOG:Verifying Flask preboot installer in image..."
if [[ ! -f "$ROOT/opt/ethos/installer/preboot/app.py" ]]; then
    echo "LOG:CRITICAL ERROR — Flask preboot app.py does not exist in image!"
    exit 1
fi
echo "LOG:Flask preboot installer OK"

cat > "$ROOT/etc/systemd/system/ethos-preboot.service" <<'PREBOOT'
[Unit]
Description=EthOS Installer (pre-boot setup)
After=network.target NetworkManager.service
Wants=NetworkManager.service
Before=ethos-firstboot.service
Conflicts=ethos.service
ConditionPathExists=/opt/ethos/installer/preboot/app.py
ConditionPathExists=!/opt/ethos/.installed
StartLimitIntervalSec=60
StartLimitBurst=5
[Service]
Type=simple
WorkingDirectory=/opt/ethos/installer/preboot
ExecStart=/opt/ethos/venv/bin/python /opt/ethos/installer/preboot/app.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
PREBOOT
ln -sf /etc/systemd/system/ethos-preboot.service "$ROOT/etc/systemd/system/multi-user.target.wants/ethos-preboot.service"

# Set multi-user as default (headless — no kiosk, access via hotspot + browser)
mkdir -p "$ROOT/etc/systemd/system/multi-user.target.wants"
chroot "$ROOT" systemctl set-default multi-user.target 2>/dev/null || true

# ── ethos.service (pre-create — firstboot.sh enables + starts it after stopping preboot) ──
# NOTE: Do NOT add After=ethos-firstboot.service — it causes deadlock!
# (firstboot is Type=oneshot and calls systemctl restart ethos from within itself)
cat > "$ROOT/etc/systemd/system/ethos.service" <<SVCETHOS
[Unit]
Description=EthOS NAS
After=network.target local-fs.target
Wants=network.target
Conflicts=ethos-preboot.service
RequiresMountsFor=/mnt/data

[Service]
Type=notify
NotifyAccess=all
WorkingDirectory=/opt/ethos
EnvironmentFile=/opt/ethos/ethos.env
ExecStartPre=/bin/bash -c 'for d in data logs backups uploads venv; do p="/opt/ethos/\$d"; [ -L "\$p" ] && mkdir -p "\$(readlink "\$p")" || mkdir -p "\$p"; done'
Environment=PYTHONPATH=/opt/ethos/backend
ExecStart=/opt/ethos/venv/bin/python /opt/ethos/backend/app.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SVCETHOS
# NOTE: Do NOT enable here — firstboot.sh enables after stopping preboot (port 9000 conflict)

# ── Auto-login on tty1 as nasadmin (NOT root) during first boot ──
# After setup wizard completes, firstboot removes this override
mkdir -p "$ROOT/etc/systemd/system/getty@tty1.service.d"
cat > "$ROOT/etc/systemd/system/getty@tty1.service.d/override.conf" <<AUTOLOGIN
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $DEFAULT_USER --noclear %I \$TERM
AUTOLOGIN

# ── .bash_profile for nasadmin — show setup info on console ──
cat > "$ROOT/home/$DEFAULT_USER/.bash_profile" <<'USERPROFILE'
#!/bin/bash
# EthOS first-boot console banner
if [ ! -f /opt/ethos/.installed ]; then
    clear
    echo ""
    echo "  ======================================================="
    echo "              EthOS — Pierwszy start"
    echo "  ======================================================="
    echo ""
    echo "  System sie konfiguruje..."
    echo ""
    IP=$(hostname -I 2>/dev/null | awk '{{print $1}}')
    if [ -n "$IP" ]; then
    echo "  =>  http://${{IP}}:9000"
    else
    echo "  No network — connect to WiFi hotspot:"
    echo "    SSID:  ethos  (bez hasla)"
    echo "    Adres: http://192.168.42.1:9000"
    echo ""
    echo "  Lub podlacz kabel Ethernet."
    fi
    echo ""
    echo "  Kreator pomoze Ci ustawic:"
    echo "    - Polaczenie z siecia WiFi"
    echo "    - Konto administratora"
    echo "    - Dysk danych"
    echo ""
    echo "  ======================================================="
    echo ""
    while [ -z "$IP" ]; do
        sleep 15
        IP=$(hostname -I 2>/dev/null | awk '{{print $1}}')
        if [ -n "$IP" ]; then
            echo "  Siec dostepna: http://${{IP}}:9000"
            echo ""
        fi
    done
fi
USERPROFILE
chown $(chroot "$ROOT" id -u $DEFAULT_USER):$(chroot "$ROOT" id -g $DEFAULT_USER) "$ROOT/home/$DEFAULT_USER/.bash_profile"

echo "STEP:85:EthOS injected"
_ckpt_set "06_inject_ethos"

# ── Step 7: Cleanup & finalize ──
echo "STEP:86:Finalizing..."
# Unmount apt cache BEFORE cleaning (it's bind-mounted to host cache)
umount "$ROOT/var/cache/apt/archives" 2>/dev/null || true
chroot "$ROOT" apt-get clean 2>/dev/null || true
rm -rf "$ROOT/var/lib/apt/lists/"* 2>/dev/null || true
rm -rf "$ROOT/var/cache/apt/"*.bin 2>/dev/null || true
rm -rf "$ROOT/tmp/"* 2>/dev/null || true
rm -rf "$ROOT/var/tmp/"* 2>/dev/null || true
rm -rf "$ROOT/var/log/"*.gz "$ROOT/var/log/"*.1 2>/dev/null || true
# Clear pip cache that may have leaked
rm -rf "$ROOT/root/.cache" 2>/dev/null || true
# Only search /opt and /usr — skip mounted /proc, /sys, /dev
find "$ROOT/opt" "$ROOT/usr" -type d -name "__pycache__" -exec rm -rf {{}} + 2>/dev/null || true
# Remove .pyc files (regenerated on import)
find "$ROOT/opt" -name '*.pyc' -delete 2>/dev/null || true
truncate -s 0 "$ROOT/etc/machine-id" 2>/dev/null || true
rm -f "$ROOT/var/lib/dbus/machine-id"
# [DIST-SEC] Remove SSH host keys so they regenerate on first boot
rm -f "$ROOT/etc/ssh/ssh_host_"*
# Log final image usage
echo "LOG:Wykorzystanie dysku w obrazie:"
du -sh "$ROOT"/* 2>/dev/null | sort -rh | head -10 || true
df -h "$ROOT" 2>/dev/null || true

# ── Step 7a: Create SquashFS immutable root image ──
# SquashFS = golden image of the installed system (NOT the installer USB state).
# Data dirs are symlinked to /mnt/data/ethos/ for persistence across updates.
if command -v mksquashfs >/dev/null 2>&1; then
    echo "STEP:87:Creating SquashFS immutable root image..."

    ETHOS_DIR_SQ="$ROOT/opt/ethos"
    # Prepare clean installed-system state (squashfs should NOT contain installer artifacts)
    for d in data logs backups uploads venv; do
        rm -rf "$ETHOS_DIR_SQ/$d"
        ln -s "/mnt/data/ethos/$d" "$ETHOS_DIR_SQ/$d"
    done
    rm -f "$ETHOS_DIR_SQ/.installer-mode" "$ETHOS_DIR_SQ/.installed"
    mkdir -p "$ROOT/mnt/data"
    mkdir -p "$ROOT/mnt/snapshots"

    SQSH_OUT="$WORK_DIR/ethos-root.sqsh"
    mksquashfs "$ROOT" "$SQSH_OUT" \
        -comp zstd -Xcompression-level $SQSH_COMPRESSION_LEVEL \
        -noappend -no-progress \
        -e "$ROOT/proc" \
        -e "$ROOT/sys" \
        -e "$ROOT/dev" \
        -e "$ROOT/run" \
        -e "$ROOT/tmp" \
        -e "$ROOT/media" \
        -e "$ROOT/lost+found" \
        -e "$ROOT/swapfile" \
        -e "$ROOT/var/swap" \
        -e "$ROOT/opt/ethos/installer/images" \
        2>&1 | tail -10

    SQSH_SIZE=$(stat -c%s "$SQSH_OUT" 2>/dev/null || echo 0)
    echo "LOG:SquashFS image: $((SQSH_SIZE / 1048576))MB"

    # Generate dm-verity hash tree for integrity verification
    if command -v veritysetup >/dev/null 2>&1; then
        echo "LOG:Generating dm-verity hash tree..."
        VERITY_OUT="$WORK_DIR/ethos-root.sqsh.verity"
        ROOTHASH_OUT="$WORK_DIR/ethos-root.sqsh.roothash"
        veritysetup format "$SQSH_OUT" "$VERITY_OUT" 2>/dev/null | tee /tmp/verity-format.txt
        ROOTHASH=$(grep "Root hash:" /tmp/verity-format.txt | awk '{{print $NF}}')
        if [ -n "$ROOTHASH" ]; then
            echo "$ROOTHASH" > "$ROOTHASH_OUT"
            VERITY_SIZE=$(stat -c%s "$VERITY_OUT" 2>/dev/null || echo 0)
            echo "LOG:dm-verity: root hash=$ROOTHASH, hash tree=$((VERITY_SIZE / 1024))KB"
            # Sign artifact — produce ethos-manifest.json alongside .sqsh and .verity
            echo "LOG:Signing artifact (RSA-SHA256)..."
            python3 -c "
import sys
sys.path.insert(0, '$NASOS/backend')
from blueprints.builder_signing import sign_artifact, write_manifest
import json
try:
    ver = json.load(open('$NASOS/backend/version.json')).get('version','?')
except Exception:
    ver = '?'
m = sign_artifact('$SQSH_OUT', '$ROOTHASH', build_version=ver)
p = write_manifest(m, '$WORK_DIR')
print('LOG:Manifest written: ' + p if p else 'LOG:WARNING: manifest signing failed')
" 2>&1 | while IFS= read -r _l; do echo "LOG:$_l"; done || echo "LOG:WARNING: signing step failed (non-fatal)"
        else
            echo "LOG:WARNING: dm-verity format failed — skipping"
            rm -f "$VERITY_OUT" "$ROOTHASH_OUT"
        fi
        rm -f /tmp/verity-format.txt
    else
        echo "LOG:veritysetup not found — dm-verity disabled (install cryptsetup-bin for verified boot)"
    fi

    # Restore USB/installer state (so the USB can still boot the installer)
    for d in data logs backups uploads venv; do
        rm -f "$ETHOS_DIR_SQ/$d"
        mkdir -p "$ETHOS_DIR_SQ/$d"
    done
    touch "$ETHOS_DIR_SQ/.installer-mode"
else
    echo "LOG:WARNING: mksquashfs not found — SquashFS image will not be created"
fi

# ── Inject pre-flight health-check service (into installer rootfs AFTER squashfs creation) ──
# This service will NOT be in the installed system (squashfs is already baked).
# It runs once in the QEMU test, reports health, then self-destructs.
if [ "$PREFLIGHT_ENABLED" = "1" ] && mountpoint -q "$ROOT" 2>/dev/null; then
    echo "LOG:Injecting pre-flight health-check service into rootfs..."
    mkdir -p "$ROOT/usr/local/sbin"
    cat > "$ROOT/usr/local/sbin/ethos-preflight.sh" <<'PFSCRIPT'
#!/bin/bash
# EthOS pre-flight check — runs once in test VM, reports via serial console
exec 1>/dev/ttyS0 2>&1
echo "PREFLIGHT:START"
echo "PREFLIGHT:kernel=$(uname -r)"
SYSTEMD_STATE=$(systemctl is-system-running --wait --timeout=30 2>/dev/null || echo unknown)
echo "PREFLIGHT:SYSTEMD:$SYSTEMD_STATE"
# Check whichever EthOS service is expected to be active
# On installer images ethos-preboot.service runs; on installed systems ethos.service runs.
if systemctl is-active ethos.service >/dev/null 2>&1; then
    echo "PREFLIGHT:ETHOS:OK"
elif systemctl is-active ethos-preboot.service >/dev/null 2>&1; then
    echo "PREFLIGHT:ETHOS:PREBOOT_OK"
else
    echo "PREFLIGHT:ETHOS:FAIL"
fi
# Branding validation
if grep -q "ID=ethos" /etc/os-release 2>/dev/null; then
    echo "PREFLIGHT:BRANDING:OK"
else
    echo "PREFLIGHT:BRANDING:FAIL"
fi
# Hardening validation
if [ -f /etc/sysctl.d/91-ethos-security.conf ]; then
    echo "PREFLIGHT:HARDENING:OK"
else
    echo "PREFLIGHT:HARDENING:FAIL"
fi
# Flask available (check venv python, not system python)
if /opt/ethos/venv/bin/python -c "import flask" 2>/dev/null; then
    echo "PREFLIGHT:FLASK:OK"
else
    echo "PREFLIGHT:FLASK:FAIL"
fi
echo "PREFLIGHT:DONE"
systemctl disable ethos-preflight.service 2>/dev/null || true
rm -f /usr/local/sbin/ethos-preflight.sh /etc/systemd/system/ethos-preflight.service
systemctl daemon-reload 2>/dev/null || true
# Only power off in QA/beacon mode — when ETHOS_BUILD_HOST is set in ethos.env
# (injected by builder when ETHOS_QA_BUILD_HOST env var is set on the build host).
# Without it the image runs normally so the user can access the web UI.
if grep -q "^ETHOS_BUILD_HOST=" /opt/ethos/ethos.env 2>/dev/null; then
    shutdown -h now
fi
exit 0
PFSCRIPT
    chmod +x "$ROOT/usr/local/sbin/ethos-preflight.sh"
    cat > "$ROOT/etc/systemd/system/ethos-preflight.service" <<'PFSVC'
[Unit]
Description=EthOS Pre-flight Health Check
After=network.target ethos-preboot.service ethos.service
ConditionPathExists=/usr/local/sbin/ethos-preflight.sh

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ethos-preflight.sh
TimeoutStartSec=120
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
PFSVC
    chroot "$ROOT" systemctl enable ethos-preflight.service 2>/dev/null || true
    echo "LOG:Pre-flight service injected into rootfs"
fi

# ── Secure Boot MOK signing ──
echo "LOG:Attempting Secure Boot MOK signing..."
python3 - "$ROOT" "$BRAND_NAME" <<'SBSIGNPY'
import sys, os
sys.path.insert(0, '/opt/ethos/backend/blueprints')
try:
    from builder_secureboot import ensure_mok_keys, sign_rootfs_efi_binaries, install_mok_der_to_esp
    rootfs, brand = sys.argv[1], sys.argv[2]
    if ensure_mok_keys(brand):
        results = sign_rootfs_efi_binaries(rootfs, brand)
        signed = [k for k, v in results.items() if v]
        install_mok_der_to_esp(rootfs)
        if signed:
            print(f"LOG:Secure Boot: signed {{len(signed)}} EFI binaries", flush=True)
            print(f"LOG:Secure Boot: after install run: sudo mokutil --import /boot/efi/EFI/ethos/MOK.der", flush=True)
        else:
            print("LOG:Secure Boot: no EFI binaries signed (sbsign not installed or no targets found)", flush=True)
    else:
        print("LOG:Secure Boot: MOK key generation failed — skipping", flush=True)
except Exception as e:
    print(f"LOG:Secure Boot signing skipped: {{e}}", flush=True)
SBSIGNPY

sync

for m in boot/efi run sys proc dev/shm dev/pts dev; do
    umount "$ROOT/$m" 2>/dev/null || \
        umount -l "$ROOT/$m" 2>/dev/null || true
done
sleep 1
umount "$ROOT" 2>/dev/null || \
    umount -l "$ROOT" 2>/dev/null || true

# ── Step 7b: Inject install image(s) into filesystem ──
ROOT_PART="${{LOOP_DEV}}p2"

if [ -f "$WORK_DIR/ethos-root.sqsh" ]; then
    # SquashFS available — inject as primary install method
    echo "STEP:88:Injecting SquashFS image..."
    mount "$ROOT_PART" "$WORK_DIR/root"
    mkdir -p "$WORK_DIR/root/opt/ethos/installer/images"
    cp "$WORK_DIR/ethos-root.sqsh" "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.sqsh"
    SQSH_FINAL=$(stat -c%s "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.sqsh" 2>/dev/null || echo 0)
    echo "LOG:SquashFS image injected: $((SQSH_FINAL / 1048576))MB"
    rm -f "$WORK_DIR/ethos-root.sqsh"

    # Inject dm-verity data alongside squashfs
    if [ -f "$WORK_DIR/ethos-root.sqsh.verity" ] && [ -f "$WORK_DIR/ethos-root.sqsh.roothash" ]; then
        cp "$WORK_DIR/ethos-root.sqsh.verity" "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.sqsh.verity"
        cp "$WORK_DIR/ethos-root.sqsh.roothash" "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.sqsh.roothash"
        # Also copy to boot/efi for the installed system
        mkdir -p "$WORK_DIR/root/boot/efi/EFI/ethos"
        cp "$WORK_DIR/ethos-root.sqsh.roothash" "$WORK_DIR/root/boot/efi/EFI/ethos/roothash"
        echo "LOG:dm-verity data injected"
        rm -f "$WORK_DIR/ethos-root.sqsh.verity" "$WORK_DIR/ethos-root.sqsh.roothash"
        # Inject manifest (signing artifact)
        if [ -f "$WORK_DIR/ethos-manifest.json" ]; then
            cp "$WORK_DIR/ethos-manifest.json" "$WORK_DIR/root/opt/ethos/installer/images/ethos-manifest.json"
            echo "LOG:Manifest injected into image"
            rm -f "$WORK_DIR/ethos-manifest.json"
        fi
        # Inject SBOM alongside squashfs
        if [ -f "$WORK_DIR/ethos-sbom.json" ]; then
            cp "$WORK_DIR/ethos-sbom.json" "$WORK_DIR/root/opt/ethos/installer/images/ethos-sbom.json"
            echo "LOG:SBOM injected into image"
            rm -f "$WORK_DIR/ethos-sbom.json"
        fi
    fi
    sync
    umount "$WORK_DIR/root" 2>/dev/null || \
        umount -l "$WORK_DIR/root" 2>/dev/null || true
else
    # Fallback: create dd+zstd compressed root image for non-squashfs install
    echo "STEP:88:Creating compressed root image (fallback)..."
    COMPRESSED_IMG="$WORK_DIR/ethos-root.img.zst"

    echo "LOG:Running e2fsck on root partition..."
    e2fsck -f -y "$ROOT_PART" 2>&1 | tail -5 || true

    echo "LOG:Shrinking root filesystem to minimum size..."
    resize2fs -M "$ROOT_PART" 2>&1 | tail -5
    BLOCK_COUNT=$(dumpe2fs -h "$ROOT_PART" 2>/dev/null | awk '/Block count:/ {{print $3}}')
    BLOCK_SIZE=$(dumpe2fs -h "$ROOT_PART" 2>/dev/null | awk '/Block size:/ {{print $3}}')
    if [ -n "$BLOCK_COUNT" ] && [ -n "$BLOCK_SIZE" ]; then
        USED_BYTES=$((BLOCK_COUNT * BLOCK_SIZE))
        SAFE_BYTES=$(( (USED_BYTES * 105 / 100 + 4194303) / 4194304 * 4194304 ))
        DD_COUNT=$((SAFE_BYTES / 4194304))
        echo "LOG:Root partition minimized: ${{BLOCK_COUNT}} blocks x ${{BLOCK_SIZE}}B = $((USED_BYTES / 1048576))MB, dd count=$DD_COUNT"
        DECOMPRESSED_BYTES=$((DD_COUNT * 4194304))
        set -o pipefail
        dd if="$ROOT_PART" bs=4M count=$DD_COUNT status=none | zstd -3 -T0 -o "$COMPRESSED_IMG" 2>&1
        DD_RC=$?
        set +o pipefail
        if [ $DD_RC -ne 0 ]; then
            echo "LOG:WARNING: dd+zstd pipeline failed with code $DD_RC"
        fi
        if [ -f "$COMPRESSED_IMG" ]; then
            COMP_SIZE=$(stat -c%s "$COMPRESSED_IMG" 2>/dev/null || echo 0)
            echo "LOG:Compressed root image: $((COMP_SIZE / 1048576))MB (decompressed: $((DECOMPRESSED_BYTES / 1048576))MB)"
            # Validate the compressed image
            if ! zstd -t "$COMPRESSED_IMG" 2>/dev/null; then
                echo "LOG:WARNING: Compressed image failed integrity check — removing"
                rm -f "$COMPRESSED_IMG"
            fi
        fi
    fi

    echo "LOG:Expanding root filesystem back..."
    resize2fs "$ROOT_PART" 2>&1 | tail -3

    mount "$ROOT_PART" "$WORK_DIR/root"
    if [ -f "$COMPRESSED_IMG" ]; then
        mkdir -p "$WORK_DIR/root/opt/ethos/installer/images"
        cp "$COMPRESSED_IMG" "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.img.zst"
        echo "LOG:Compressed root image injected into filesystem"
        rm -f "$COMPRESSED_IMG"
    fi
    sync
    umount "$WORK_DIR/root" 2>/dev/null || \
        umount -l "$WORK_DIR/root" 2>/dev/null || true
fi

echo "STEP:90:Finalizacja obrazu IMG..."

losetup -d "$LOOP_DEV" 2>/dev/null || true
LOOP_DEV=""

# Move image from tmpfs to persistent storage
if [ "$USE_TMPFS" -eq 1 ] && [ -f "$OUTPUT_IMG" ]; then
    echo "LOG:Copying image from RAM to disk ($FINAL_IMG)..."
    cp "$OUTPUT_IMG" "$FINAL_IMG"
    rm -f "$OUTPUT_IMG"
    OUTPUT_IMG="$FINAL_IMG"
    echo "LOG:Obraz przeniesiony na dysk"
elif [ "$OUTPUT_IMG" != "$FINAL_IMG" ]; then
    mv "$OUTPUT_IMG" "$FINAL_IMG" 2>/dev/null || cp "$OUTPUT_IMG" "$FINAL_IMG"
    OUTPUT_IMG="$FINAL_IMG"
fi

# Results
IMG_SIZE=$(stat -c%s "$OUTPUT_IMG" 2>/dev/null || echo 0)

# ── Step 8: Pre-flight VM validation ──
if [ "$PREFLIGHT_ENABLED" = "1" ]; then
    echo "STEP:92:Pre-flight VM test — booting image in QEMU..."
    OVMF_FW=""
    for _p in /usr/share/OVMF/OVMF.fd /usr/share/ovmf/OVMF.fd /usr/share/qemu/OVMF.fd \\
              /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd; do
        if [ -f "$_p" ]; then OVMF_FW="$_p"; break; fi
    done
    if [ -z "$OVMF_FW" ]; then
        echo "LOG:Pre-flight: OVMF not found — install 'ovmf' package to enable VM test"
        echo "PREFLIGHT_RESULT:skipped"
    elif ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
        echo "LOG:Pre-flight: qemu-system-x86_64 not found — install 'qemu-system-x86' package"
        echo "PREFLIGHT_RESULT:skipped"
    else
        PFLOG="/tmp/ethos-preflight-$$.serial"
        KVM_OPTS=""
        if [ -e /dev/kvm ]; then
            KVM_OPTS="-enable-kvm -cpu host"
            echo "LOG:Pre-flight: KVM available — hardware acceleration enabled"
        else
            echo "LOG:Pre-flight: KVM not available — using software emulation (may be slow)"
        fi
        echo "LOG:Pre-flight: Starting QEMU (timeout: ${{PREFLIGHT_TIMEOUT}}s)..."
        qemu-system-x86_64 \\
            -bios "$OVMF_FW" \\
            -drive file="$OUTPUT_IMG",format=raw,if=virtio,readonly=on \\
            -m 1024 \\
            -smp 2 \\
            $KVM_OPTS \\
            -nographic \\
            -serial file:"$PFLOG" \\
            -no-reboot \\
            -display none \\
            2>/dev/null &
        QEMU_PID=$!
        PFLIGHT_OK=0
        PFLIGHT_FAIL=0
        _elapsed=0
        while [ $_elapsed -lt "${{PREFLIGHT_TIMEOUT}}" ]; do
            sleep 2
            _elapsed=$((_elapsed + 2))
            if [ -f "$PFLOG" ]; then
                if grep -q "^PREFLIGHT:DONE" "$PFLOG" 2>/dev/null; then
                    PFLIGHT_OK=1
                    break
                fi
                if grep -q "Kernel panic" "$PFLOG" 2>/dev/null; then
                    PFLIGHT_FAIL=1
                    break
                fi
            fi
        done
        kill $QEMU_PID 2>/dev/null
        wait $QEMU_PID 2>/dev/null || true
        if [ "$PFLIGHT_OK" = "1" ]; then
            echo "LOG:Pre-flight: PASSED — image booted and services verified"
            if [ -f "$PFLOG" ]; then
                grep "^PREFLIGHT:" "$PFLOG" 2>/dev/null | while IFS= read -r _line; do
                    echo "LOG:VM> $_line"
                done
            fi
            echo "PREFLIGHT_RESULT:ok"
        elif [ "$PFLIGHT_FAIL" = "1" ]; then
            echo "LOG:Pre-flight: FAILED — kernel panic detected"
            echo "PREFLIGHT_RESULT:fail"
        else
            echo "LOG:Pre-flight: TIMEOUT — VM did not report within ${{PREFLIGHT_TIMEOUT}}s"
            echo "PREFLIGHT_RESULT:timeout"
        fi
        rm -f "$PFLOG"
    fi
else
    echo "LOG:Pre-flight VM test disabled"
    echo "PREFLIGHT_RESULT:disabled"
fi

BUILD_DONE=1
echo "STEP:100:Obraz gotowy!"
echo "RESULT_IMG:$OUTPUT_IMG:$IMG_SIZE"
"""



# ═══════════════════════════════════════════════════════════
#  API — Artifact Signing
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/signing-key')
@admin_required
def get_signing_key():
    """Return the builder public key (PEM). Used for offline artifact verification."""
    from blueprints.builder_signing import ensure_signing_key, get_public_key_pem
    ensure_signing_key()
    pem = get_public_key_pem()
    if not pem:
        return jsonify({'error': 'Signing key not available'}), 503
    return jsonify({'ok': True, 'public_key': pem})


@builder_bp.route('/manifest')
@admin_required
def get_manifest():
    """
    Verify and return a build manifest.

    Query params:
      path  — path to ethos-manifest.json (must be inside installer/ dir)
      sqsh  — (optional) path to .sqsh for full verification
    """
    from blueprints.builder_signing import verify_artifact
    manifest_path = request.args.get('path', '').strip()
    sqsh_path     = request.args.get('sqsh', '').strip()

    if not manifest_path:
        return jsonify({'error': 'path param required'}), 400

    nasos        = _get_host_nasos_dir()
    allowed_root = os.path.realpath(os.path.join(nasos, 'installer'))
    real_path    = os.path.realpath(manifest_path)
    if not real_path.startswith(allowed_root + '/'):
        return jsonify({'error': 'Path not allowed'}), 403

    if not os.path.isfile(real_path):
        return jsonify({'error': 'Manifest not found'}), 404

    import json as _json
    try:
        manifest = _json.load(open(real_path))
    except Exception as exc:
        return jsonify({'error': f'Cannot read manifest: {exc}'}), 400

    verified   = None
    verify_msg = ''
    if sqsh_path:
        real_sqsh = os.path.realpath(sqsh_path)
        if real_sqsh.startswith(allowed_root + '/') and os.path.isfile(real_sqsh):
            verified, verify_msg = verify_artifact(real_sqsh, real_path)
        else:
            verify_msg = 'sqsh path not accessible'

    return jsonify({
        'ok':        True,
        'manifest':  manifest,
        'verified':  verified,
        'verify_msg': verify_msg,
    })


# ═══════════════════════════════════════════════════════════
#  API — Build Logs
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/logs')
def builder_logs():
    """Return build log contents (last N lines)."""
    lines_count = request.args.get('lines', 200, type=int)
    log_file = os.path.join(LOG_DIR, 'builder.log')

    if not os.path.isfile(log_file):
        return jsonify({'log': '', 'lines': 0, 'size': 0})

    size = os.path.getsize(log_file)
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        tail = all_lines[-lines_count:] if len(all_lines) > lines_count else all_lines
        return jsonify({
            'log': ''.join(tail),
            'lines': len(all_lines),
            'size': size,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@builder_bp.route('/logs/clear', methods=['POST'])
def clear_logs():
    """Clear the build log file."""
    log_file = os.path.join(LOG_DIR, 'builder.log')
    try:
        with open(log_file, 'w') as f:
            f.write('')
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ═══════════════════════════════════════════════════════════
#  API — Delete release/image
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/delete', methods=['POST'])
def delete_artifact():
    """Delete one or more release packages / image files."""
    data = request.json or {}
    # Support both single path and array of paths
    paths = data.get('paths') or []
    single = data.get('path', '').strip()
    if single and not paths:
        paths = [single]

    if not paths:
        return jsonify({'error': 'No paths provided'}), 400

    nasos = _get_host_nasos_dir()
    allowed_root = os.path.realpath(nasos + '/installer')

    deleted = []
    errors = []
    for p in paths:
        p = str(p).strip()
        if not p:
            continue
        real = os.path.realpath(p)
        if not real.startswith(allowed_root + '/'):
            errors.append(f'{os.path.basename(p)}: path not allowed')
            continue
        r = _host_run(f"rm -f {_q(real)}")
        if r.returncode == 0:
            deleted.append(os.path.basename(p))
        else:
            errors.append(f'{os.path.basename(p)}: failed to delete')

    return jsonify({'ok': True, 'deleted': deleted, 'errors': errors})


# ═══════════════════════════════════════════════════════════
#  API — Download image/release file
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/download')
def download_artifact():
    """Stream an image or release file for download."""
    from flask import send_file as _send
    path = request.args.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Path required'}), 400

    nasos = _get_host_nasos_dir()
    # Security: resolve symlinks/.. before checking prefix
    allowed_root = os.path.realpath(nasos + '/installer')
    real_check = os.path.realpath(path)
    if not real_check.startswith(allowed_root + '/'):
        return jsonify({'error': 'Path not allowed'}), 403

    # Try direct path first (native mode), then Docker container mapping
    if os.path.isfile(path):
        real_path = path
    else:
        real_path = path.replace('/home/', '/data/home/', 1)

    if not os.path.isfile(real_path):
        return jsonify({'error': 'File not found'}), 404

    filename = os.path.basename(path)
    return _send(real_path, as_attachment=True, download_name=filename)


def _human_size(b):
    return fmt_bytes(b)


# ═══════════════════════════════════════════════════════════
#  Publish Apps to GitHub
# ═══════════════════════════════════════════════════════════

_PUBLISH_CONFIG_FILE = data_path('builder_github.json')
_PUBLISH_REPO_DEFAULT = 'SyncHot/ethos-os-ethos-apps'


def _load_publish_config():
    try:
        if os.path.isfile(_PUBLISH_CONFIG_FILE):
            with open(_PUBLISH_CONFIG_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_publish_config(cfg):
    tmp = _PUBLISH_CONFIG_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, _PUBLISH_CONFIG_FILE)


def _github_api(method, path, token, body=None, timeout=30):
    """Call GitHub REST API. Returns (status_code, parsed_json)."""
    import urllib.request, urllib.error
    url = f'https://api.github.com{path}'
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', f'token {token}')
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('User-Agent', 'EthOS-Builder/1.0')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode()
            parsed = json.loads(body_text)
            # Enrich with human-readable context for common errors
            if e.code == 401:
                parsed['_hint'] = 'Nieprawidłowy token GitHub. Sprawdź token w konfiguracji Publishera.'
            elif e.code == 403:
                rate_remaining = e.headers.get('X-RateLimit-Remaining', '?')
                if rate_remaining == '0':
                    reset_ts = e.headers.get('X-RateLimit-Reset', '')
                    parsed['_hint'] = f'Przekroczono limit GitHub API. Poczekaj chwilę.'
                else:
                    parsed['_hint'] = 'Brak uprawnień. Token musi mieć scope: repo (lub contents:write).'
            elif e.code == 404:
                parsed['_hint'] = f'Nie znaleziono zasobu GitHub: {path}'
            elif e.code == 422:
                parsed['_hint'] = 'GitHub odrzucił żądanie (błąd walidacji). Sprawdź zawartość pliku.'
            return e.code, parsed
        except Exception:
            return e.code, {'message': str(e)}
    except Exception as e:
        return 0, {'message': f'Błąd sieci: {e}'}


def _bump_version(ver):
    """Bump patch version: 1.0.0 -> 1.0.1"""
    parts = ver.split('.')
    while len(parts) < 3:
        parts.append('0')
    parts[2] = str(int(parts[2]) + 1)
    return '.'.join(parts)


def _get_app_files(app_id):
    """Get local file paths for an optional app. Returns dict with 'backend' and 'frontend' paths."""
    import importlib
    am = importlib.import_module('blueprints.app_manager')

    files = {}
    bp_info = am._OPTIONAL_BLUEPRINTS.get(app_id)
    if bp_info:
        module_name = bp_info[0]
        bp_path = os.path.join(app_path(), 'backend', 'blueprints', module_name + '.py')
        if os.path.isfile(bp_path):
            files['backend'] = bp_path

    fn = am._get_frontend_filename(app_id)
    if fn:
        js_path = os.path.join(app_path(), 'frontend', 'js', 'apps', fn + '.js')
        if os.path.isfile(js_path):
            files['frontend'] = js_path

    return files


@builder_bp.route('/publish-config', methods=['GET'])
def get_publish_config():
    """Get GitHub publish config (token masked)."""
    cfg = _load_publish_config()
    token = cfg.get('token', '')
    masked = token[:4] + '***' + token[-4:] if len(token) > 8 else ('***' if token else '')
    return jsonify({
        'ok': True,
        'repo': cfg.get('repo', _PUBLISH_REPO_DEFAULT),
        'token': masked,
        'has_token': bool(token),
    })


@builder_bp.route('/publish-config', methods=['PUT'])
def set_publish_config():
    """Save GitHub publish config."""
    data = request.json or {}
    cfg = _load_publish_config()

    token = data.get('token', '').strip()
    if token and '***' not in token:
        cfg['token'] = token
    repo = data.get('repo', '').strip()
    if repo:
        cfg['repo'] = repo

    _save_publish_config(cfg)
    return jsonify({'ok': True})


@builder_bp.route('/publish-diff', methods=['GET'])
def publish_diff():
    """Compare local optional app files with GitHub. Returns list of changed apps."""
    import hashlib, base64, importlib, urllib.request, urllib.error

    am = importlib.import_module('blueprints.app_manager')
    cfg = _load_publish_config()
    token = cfg.get('token', '')
    repo = cfg.get('repo', _PUBLISH_REPO_DEFAULT)

    # Fetch remote catalog
    remote_catalog = {}
    try:
        catalog_url = f'https://raw.githubusercontent.com/{repo}/main/catalog.json'
        req = urllib.request.Request(catalog_url, headers={'User-Agent': 'EthOS-Builder/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            cat_data = json.loads(resp.read().decode())
        for a in (cat_data.get('apps', cat_data) if isinstance(cat_data, dict) else cat_data):
            remote_catalog[a['id']] = a
    except Exception:
        pass

    # Fetch remote tree to get file SHAs (for content comparison)
    remote_tree = {}
    if token:
        try:
            code, data = _github_api('GET', f'/repos/{repo}/git/trees/main?recursive=1', token)
            if code == 200:
                for item in data.get('tree', []):
                    remote_tree[item['path']] = item['sha']
        except Exception:
            pass

    results = []
    for app_entry in am.BUILTIN_CATALOG:
        app_id = app_entry['id']
        if app_id in am.CORE_APPS:
            continue

        local_files = _get_app_files(app_id)
        if not local_files:
            continue

        remote_ver = remote_catalog.get(app_id, {}).get('version', '—')
        local_ver = app_entry.get('version', '1.0.0')

        changes = []
        for ftype, local_path in local_files.items():
            remote_key = f'apps/{app_id}/{"backend.py" if ftype == "backend" else "frontend.js"}'
            remote_sha = remote_tree.get(remote_key)

            # Compute git blob SHA for local file
            with open(local_path, 'rb') as f:
                content = f.read()
            blob_header = f'blob {len(content)}\0'.encode()
            local_sha = hashlib.sha1(blob_header + content).hexdigest()

            if remote_sha is None:
                changes.append({'file': ftype, 'status': 'new'})
            elif local_sha != remote_sha:
                changes.append({'file': ftype, 'status': 'modified'})

        results.append({
            'id': app_id,
            'name': app_entry.get('name', app_id),
            'icon': app_entry.get('icon', 'fa-puzzle-piece'),
            'color': app_entry.get('color', '#6366f1'),
            'local_version': local_ver,
            'remote_version': remote_ver,
            'changes': changes,
            'changed': len(changes) > 0,
        })

    results.sort(key=lambda x: (not x['changed'], x['name']))
    return jsonify({'ok': True, 'apps': results, 'repo': repo, 'has_token': bool(token)})


@builder_bp.route('/publish-apps', methods=['POST'])
def publish_apps():
    """Publish changed optional apps to GitHub. Streams progress via SSE."""
    import hashlib, base64, importlib

    cfg = _load_publish_config()
    token = cfg.get('token', '')
    repo = cfg.get('repo', _PUBLISH_REPO_DEFAULT)

    if not token:
        return jsonify({'error': 'GitHub token nie skonfigurowany'}), 400

    data = request.json or {}
    app_ids = data.get('app_ids', [])
    if not app_ids:
        return jsonify({'error': 'Brak aplikacji do opublikowania'}), 400

    am = importlib.import_module('blueprints.app_manager')
    catalog_by_id = {a['id']: a for a in am.BUILTIN_CATALOG}

    def generate():
        try:
            yield _sse({'type': 'step', 'message': 'Pobieranie aktualnego stanu repozytorium...', 'percent': 5})

            # Get current main branch ref
            code, ref_data = _github_api('GET', f'/repos/{repo}/git/ref/heads/main', token)
            if code != 200:
                hint = ref_data.get('_hint', '')
                msg = ref_data.get('message', str(code))
                yield _sse({'type': 'done', 'success': False, 'message': f'Nie można pobrać ref main: {msg}' + (f' — {hint}' if hint else '')})
                return
            current_sha = ref_data['object']['sha']

            # Get current commit's tree
            code, commit_data = _github_api('GET', f'/repos/{repo}/git/commits/{current_sha}', token)
            if code != 200:
                yield _sse({'type': 'done', 'success': False, 'message': 'Nie można pobrać commita'})
                return
            base_tree_sha = commit_data['tree']['sha']

            # Fetch current catalog.json from repo
            yield _sse({'type': 'step', 'message': 'Pobieranie katalogu aplikacji...', 'percent': 10})
            import urllib.request, urllib.error
            remote_catalog_apps = []
            try:
                cat_url = f'https://raw.githubusercontent.com/{repo}/main/catalog.json'
                req = urllib.request.Request(cat_url, headers={'User-Agent': 'EthOS-Builder/1.0'})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    cat_data = json.loads(resp.read().decode())
                if isinstance(cat_data, dict) and 'apps' in cat_data:
                    remote_catalog_apps = cat_data['apps']
                elif isinstance(cat_data, list):
                    remote_catalog_apps = cat_data
            except Exception:
                pass
            remote_by_id = {a['id']: a for a in remote_catalog_apps}

            # Get remote tree for SHA comparison
            code, tree_data = _github_api('GET', f'/repos/{repo}/git/trees/main?recursive=1', token)
            remote_tree = {}
            if code == 200:
                for item in tree_data.get('tree', []):
                    remote_tree[item['path']] = item['sha']

            # Build list of blobs to create
            tree_items = []
            changed_apps = []
            total = len(app_ids)

            for idx, app_id in enumerate(app_ids):
                pct = 15 + int((idx / max(total, 1)) * 60)
                app_def = catalog_by_id.get(app_id)
                if not app_def:
                    yield _sse({'type': 'log', 'message': f'⚠ {app_id}: nie znaleziono w katalogu, pomijam'})
                    continue

                local_files = _get_app_files(app_id)
                if not local_files:
                    yield _sse({'type': 'log', 'message': f'⚠ {app_id}: brak plików lokalnych, pomijam'})
                    continue

                app_changed = False
                for ftype, local_path in local_files.items():
                    fname = 'backend.py' if ftype == 'backend' else 'frontend.js'
                    remote_key = f'apps/{app_id}/{fname}'

                    with open(local_path, 'rb') as f:
                        content = f.read()

                    # Compute git blob SHA
                    blob_header = f'blob {len(content)}\0'.encode()
                    local_sha = hashlib.sha1(blob_header + content).hexdigest()

                    if remote_tree.get(remote_key) == local_sha:
                        continue  # unchanged

                    app_changed = True
                    yield _sse({'type': 'log', 'message': f'📦 {app_id}/{fname} ({len(content)} bytes)'})

                    # Create blob
                    b64_content = base64.b64encode(content).decode('ascii')
                    code, blob_data = _github_api('POST', f'/repos/{repo}/git/blobs', token, {
                        'content': b64_content,
                        'encoding': 'base64',
                    })
                    if code != 201:
                        yield _sse({'type': 'done', 'success': False,
                                    'message': f'Błąd tworzenia blob {app_id}/{fname}: {blob_data.get("message", code)}'})
                        return

                    tree_items.append({
                        'path': remote_key,
                        'mode': '100644',
                        'type': 'blob',
                        'sha': blob_data['sha'],
                    })

                if app_changed:
                    changed_apps.append(app_id)

                yield _sse({'type': 'step', 'message': f'Przetwarzanie: {app_def["name"]}...', 'percent': pct})

            if not changed_apps:
                yield _sse({'type': 'done', 'success': True, 'message': 'Wszystkie aplikacje są aktualne — brak zmian do opublikowania.'})
                return

            # Update catalog.json with bumped versions for changed apps
            yield _sse({'type': 'step', 'message': 'Aktualizacja katalogu wersji...', 'percent': 78})

            updated_catalog = list(remote_catalog_apps)  # copy
            updated_by_id = {a['id']: a for a in updated_catalog}

            version_bumps = []
            for app_id in changed_apps:
                local_def = catalog_by_id.get(app_id, {})
                old_ver = remote_by_id.get(app_id, {}).get('version', '0.0.0')
                new_ver = _bump_version(old_ver)
                version_bumps.append(f'{app_id}: {old_ver} → {new_ver}')

                if app_id in updated_by_id:
                    # Update existing entry
                    entry = updated_by_id[app_id]
                    for k, v in local_def.items():
                        entry[k] = v
                    entry['version'] = new_ver
                else:
                    # Add new entry
                    new_entry = dict(local_def)
                    new_entry['version'] = new_ver
                    updated_catalog.append(new_entry)

            catalog_json = json.dumps(
                {'version': '1.0', 'apps': updated_catalog},
                indent=4, ensure_ascii=False,
            ).encode('utf-8')

            # Create blob for catalog.json
            b64_catalog = base64.b64encode(catalog_json).decode('ascii')
            code, cat_blob = _github_api('POST', f'/repos/{repo}/git/blobs', token, {
                'content': b64_catalog,
                'encoding': 'base64',
            })
            if code != 201:
                yield _sse({'type': 'done', 'success': False, 'message': 'Błąd tworzenia blob catalog.json'})
                return

            tree_items.append({
                'path': 'catalog.json',
                'mode': '100644',
                'type': 'blob',
                'sha': cat_blob['sha'],
            })

            # Create tree
            yield _sse({'type': 'step', 'message': 'Tworzenie commita...', 'percent': 85})
            code, new_tree = _github_api('POST', f'/repos/{repo}/git/trees', token, {
                'base_tree': base_tree_sha,
                'tree': tree_items,
            })
            if code != 201:
                yield _sse({'type': 'done', 'success': False,
                            'message': f'Błąd tworzenia drzewa: {new_tree.get("message", code)}'})
                return

            # Create commit
            app_names = ', '.join(changed_apps)
            commit_msg = f'chore: publish apps [{app_names}]\n\n' + '\n'.join(version_bumps)

            code, new_commit = _github_api('POST', f'/repos/{repo}/git/commits', token, {
                'message': commit_msg,
                'tree': new_tree['sha'],
                'parents': [current_sha],
            })
            if code != 201:
                yield _sse({'type': 'done', 'success': False,
                            'message': f'Błąd tworzenia commita: {new_commit.get("message", code)}'})
                return

            # Update ref
            yield _sse({'type': 'step', 'message': 'Pushowanie do GitHub...', 'percent': 92})
            code, _ = _github_api('PATCH', f'/repos/{repo}/git/refs/heads/main', token, {
                'sha': new_commit['sha'],
            })
            if code != 200:
                yield _sse({'type': 'done', 'success': False, 'message': 'Błąd aktualizacji brancha main'})
                return

            # Also update local BUILTIN_CATALOG versions in app_manager.py
            yield _sse({'type': 'step', 'message': 'Aktualizacja lokalnych wersji...', 'percent': 96})
            _update_local_catalog_versions(changed_apps, updated_by_id)

            yield _sse({'type': 'step', 'message': 'Gotowe!', 'percent': 100})
            summary = f'Opublikowano {len(changed_apps)} aplikacji: ' + ', '.join(version_bumps)
            yield _sse({'type': 'done', 'success': True, 'message': summary})

        except Exception as e:
            _logger.exception('publish_apps error')
            yield _sse({'type': 'done', 'success': False, 'message': f'Wyjątek: {e}'})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


def _update_local_catalog_versions(changed_app_ids, updated_by_id):
    """Update version strings in the local app_manager.py BUILTIN_CATALOG."""
    am_path = os.path.join(app_path(), 'backend', 'blueprints', 'app_manager.py')
    try:
        with open(am_path, 'r', encoding='utf-8') as f:
            content = f.read()

        for app_id in changed_app_ids:
            new_ver = updated_by_id.get(app_id, {}).get('version')
            if not new_ver:
                continue
            # Match: 'id': 'app-id', 'name': '...', 'version': 'X.Y.Z'
            pattern = re.compile(
                r"('id':\s*'" + re.escape(app_id) + r"'.*?'version':\s*')([^']+)(')",
                re.DOTALL,
            )
            content = pattern.sub(r'\g<1>' + new_ver + r'\3', content)

        with open(am_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        _logger.warning('Failed to update local catalog versions: %s', e)


# ── Package: install / uninstall / status ──

def _builder_on_uninstall(wipe):
    """Kill active build process on uninstall."""
    with _build_lock:
        if _build_state['status'] == 'building':
            pid = _build_state['pid']
            if pid:
                _host_run(f"kill -TERM -{pid} 2>/dev/null; sleep 1; kill -KILL -{pid} 2>/dev/null || kill -KILL {pid} 2>/dev/null", timeout=10)
            _build_state.update({
                'status': 'idle', 'build_type': '', 'percent': 0,
                'message': '', 'logs': [], 'pid': 0, 'result': None,
            })
            _save_build_state()
    log.info('[builder] Processes stopped (uninstall, wipe=%s)', wipe)


register_pkg_routes(
    builder_bp,
    install_message='Builder ready.',
    wipe_files=[_BUILD_STATE_FILE],
    wipe_dirs=[app_path('releases')],
    on_uninstall=_builder_on_uninstall,
)
