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
  POST /api/builder/resume-image          -> resume an interrupted image build (SSE)
"""

import json
import logging
import os
import re
import threading
import time
import sys
from datetime import date
from flask import Blueprint, jsonify, request, Response, stream_with_context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_base, host_run_stream as _host_run_stream_base, \
    app_path, data_path, log_path, q as _q
from utils import load_json as _load_json, save_json as _save_json, fmt_bytes, register_pkg_routes, \
    require_tools
from blueprints.builder_spec import load_spec, save_spec, \
    spec_to_shell_vars, DEFAULT_SPEC
from blueprints.admin_required import admin_required

builder_bp = Blueprint('builder', __name__, url_prefix='/api/builder')

_socketio = None


def init_builder(socketio):
    global _socketio
    _socketio = socketio

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

def _get_published_app_ids():
    """Return set of app IDs that are published to GitHub (available for download).
    Only strip files from the image for apps that can be re-downloaded.
    Returns None if GitHub is unreachable (caller should keep all files)."""
    try:
        import importlib, urllib.request, sys as _sys
        _bp_dir = os.path.join(os.path.dirname(__file__))
        _sys.path.insert(0, os.path.join(_bp_dir, '..'))
        am = importlib.import_module('blueprints.app_manager')
        url = am.GITHUB_CATALOG_URL
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-Builder/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        apps = data if isinstance(data, list) else data.get('apps', [])
        return {a['id'] for a in apps if isinstance(a, dict) and 'id' in a}
    except Exception:
        return None  # if can't reach GitHub, don't strip anything

def _compute_optional_js():
    try:
        import importlib, sys as _sys
        _bp_dir = os.path.join(os.path.dirname(__file__))
        _sys.path.insert(0, os.path.join(_bp_dir, '..'))
        am = importlib.import_module('blueprints.app_manager')
        core_js = set()
        for aid in am.CORE_APPS:
            for fn in am._get_frontend_filenames(aid):
                core_js.add(fn + '.js')
        # Only strip apps that are published to GitHub (downloadable after install)
        published = _get_published_app_ids()
        if published is None:
            return []  # can't verify what's downloadable — keep all files
        optional = set()
        for app in am.BUILTIN_CATALOG:
            aid = app['id']
            if aid in am.CORE_APPS:
                continue
            if aid not in published:
                continue  # keep bundled — not yet published for download
            for fn in am._get_frontend_filenames(aid):
                if fn + '.js' not in core_js:
                    optional.add(fn + '.js')
        return sorted(optional)
    except Exception:
        return []

def _compute_optional_py():
    try:
        import importlib, sys as _sys
        _bp_dir = os.path.join(os.path.dirname(__file__))
        _sys.path.insert(0, os.path.join(_bp_dir, '..'))
        am = importlib.import_module('blueprints.app_manager')
        published = _get_published_app_ids()
        if published is None:
            return []  # can't verify what's downloadable — keep all files
        seen = set()
        result = []
        for app_id, (module_name, _, _, _) in am._OPTIONAL_BLUEPRINTS.items():
            if app_id in am.CORE_APPS:
                continue
            if app_id not in published:
                continue  # keep bundled — not yet published for download
            if module_name not in seen:
                seen.add(module_name)
                result.append(module_name + '.py')
        return sorted(result)
    except Exception:
        return []


def _get_optional_files():
    """Compute optional JS/PY lists at call time (not import time).
    This ensures GitHub reachability is checked when a build actually runs,
    not when the server starts."""
    js = _compute_optional_js()
    py = _compute_optional_py()
    return js, py


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
            _opt_js, _opt_py = _get_optional_files()
            optional_js = ' '.join(_opt_js)
            optional_py = ' '.join(_opt_py)

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
                    msg_text = parsed.get('message', '')
                    if 'personal access token' in msg_text or 'Resource not accessible' in msg_text:
                        parsed['_hint'] = (
                            'Fine-grained PAT nie ma uprawnienia do zapisu. '
                            'Wejdź w GitHub Settings → Fine-grained tokens → edytuj token → '
                            'Repository permissions → Contents: ustaw "Read and write". '
                            'Lub użyj classic PAT (ghp_...) ze scope "repo".'
                        )
                    else:
                        parsed['_hint'] = 'Brak uprawnień. Token musi mieć scope: repo (lub Contents: Read and write dla fine-grained PAT).'
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
    """Get local file paths for an optional app. Returns dict with 'backend' (list) and 'frontend' (list) paths."""
    import importlib
    am = importlib.import_module('blueprints.app_manager')

    files = {}
    bp_info = am._OPTIONAL_BLUEPRINTS.get(app_id)
    if bp_info:
        backend_paths = []
        for module_name in am._get_backend_filenames(app_id):
            bp_path = os.path.join(app_path(), 'backend', 'blueprints', module_name + '.py')
            if os.path.isfile(bp_path):
                backend_paths.append(bp_path)
        if backend_paths:
            files['backend'] = backend_paths

    frontend_paths = []
    for fn in am._get_frontend_filenames(app_id):
        js_path = os.path.join(app_path(), 'frontend', 'js', 'apps', fn + '.js')
        if os.path.isfile(js_path):
            frontend_paths.append(js_path)
    if frontend_paths:
        files['frontend'] = frontend_paths

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
        'release_repo': cfg.get('release_repo', ''),
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
    release_repo = data.get('release_repo', '').strip()
    if release_repo:
        cfg['release_repo'] = release_repo

    _save_publish_config(cfg)
    return jsonify({'ok': True})


@builder_bp.route('/github-release', methods=['POST'])
def github_release():
    """Commit, push and create a GitHub Release with update package.
    Uses token from publish-config (same PAT). Streams via SocketIO."""
    import urllib.request as _ur
    import urllib.error as _ue
    import tarfile as _tar

    data = request.json or {}
    bump = data.get('bump', 'patch')
    title = data.get('title', '').strip()
    changes = data.get('changes', [])
    if not title:
        return jsonify({'error': 'Tytuł zmiany jest wymagany'}), 400

    cfg = _load_publish_config()
    token = cfg.get('token', '').strip()
    release_repo = cfg.get('release_repo', '').strip()
    if not token:
        return jsonify({'error': 'GitHub Token nie skonfigurowany (tab Publikuj apki)'}), 400
    if not release_repo:
        return jsonify({'error': 'Release repo nie skonfigurowane (tab Publikuj apki → Repozytorium release)'}), 400

    def _emit_log(msg, error=False):
        if _socketio:
            _socketio.emit('builder_gh_log', {'message': msg, 'error': error})

    def _emit_done(ok, **kw):
        if _socketio:
            _socketio.emit('builder_gh_done', {'ok': ok, **kw})

    def _run():
        import re as _re
        nasos = _get_host_nasos_dir()
        git = f'git -C {_q(nasos)}'

        try:
            _emit_log('── GitHub Release ──')

            # 1. Read + bump version.json
            ver_file = os.path.join(nasos, 'backend', 'version.json')
            with open(ver_file) as f:
                ver_data = json.load(f)
            current = ver_data.get('version', '0.0.0')
            parts = current.split('.')
            maj, mi, pat = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
            if bump == 'major':
                new_ver = f'{maj+1}.0.0'
            elif bump == 'minor':
                new_ver = f'{maj}.{mi+1}.0'
            else:
                new_ver = f'{maj}.{mi}.{pat+1}'
            _emit_log(f'Wersja: {current} → {new_ver}')

            ver_data['version'] = new_ver
            ver_data['build_date'] = str(date.today())
            cl = ver_data.get('changelog', [])
            if not isinstance(cl, list):
                cl = []
            cl.insert(0, {
                'version': new_ver,
                'date': ver_data['build_date'],
                'title': title,
                'changes': changes if changes else [title],
            })
            ver_data['changelog'] = cl
            with open(ver_file, 'w') as f:
                json.dump(ver_data, f, indent=2, ensure_ascii=False)
            _emit_log(f'Zapisano version.json v{new_ver}')

            # 2. Git commit (with Co-authored-by trailer)
            r = _host_run(f'{git} add -A', timeout=15)
            if r.returncode != 0:
                raise RuntimeError(f'git add failed: {r.stderr}')
            commit_msg = _q(f'release: v{new_ver} — {title}\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>')
            r = _host_run(f'{git} commit -m {commit_msg}', timeout=15)
            if r.returncode != 0 and 'nothing to commit' not in (r.stdout + r.stderr):
                raise RuntimeError(f'git commit failed: {r.stderr}')
            _emit_log(f'git commit: release: v{new_ver}')

            # 3. Tag + push via HTTPS (ethos service runs as root — no SSH key for GitHub)
            r = _host_run(f'{git} tag {_q(f"v{new_ver}")}', timeout=10)
            if r.returncode != 0 and 'already exists' not in r.stderr:
                raise RuntimeError(f'git tag failed: {r.stderr}')
            push_url = _q(f'https://x-access-token:{token}@github.com/{release_repo}.git')
            r = _host_run(f'{git} rev-parse --abbrev-ref HEAD', timeout=5)
            branch = r.stdout.strip() or 'main'
            _emit_log('git push…')
            r = _host_run(f'{git} push {push_url} HEAD:refs/heads/{_q(branch)}', timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f'git push failed: {r.stderr}')
            r = _host_run(f'{git} push {push_url} {_q(f"v{new_ver}")}', timeout=30)
            if r.returncode != 0:
                raise RuntimeError(f'git push tag failed: {r.stderr}')
            _emit_log('Push OK')

            # 4. Build .tar.gz
            _emit_log('Budowanie paczki .tar.gz…')
            pkg_dir = data_path('updates')
            os.makedirs(pkg_dir, exist_ok=True)
            pkg_name = f'ethos-{new_ver}'
            pkg_filename = f'{pkg_name}.tar.gz'
            pkg_path = os.path.join(pkg_dir, pkg_filename)
            with _tar.open(pkg_path, 'w:gz') as tar:
                for subdir in ['backend', 'frontend']:
                    src = os.path.join(nasos, subdir)
                    if os.path.exists(src):
                        tar.add(src, arcname=f'{pkg_name}/{subdir}')
            size = os.path.getsize(pkg_path)
            _emit_log(f'Paczka: {pkg_filename} ({size // 1024} KB)')

            # 5. Create GitHub Release
            _emit_log('Tworzenie GitHub Release…')
            body_text = '\n'.join(f'- {c}' for c in (changes if changes else [title]))
            status, release = _github_api('POST', f'/repos/{release_repo}/releases', token, {
                'tag_name': f'v{new_ver}',
                'name': f'v{new_ver} — {title}',
                'body': body_text,
                'draft': False,
                'prerelease': False,
            })
            if status not in (200, 201):
                raise RuntimeError(f'GitHub API {status}: {release}')
            release_id = release['id']
            upload_url = _re.sub(r'\{.*\}', '', release.get('upload_url', ''))
            _emit_log(f'Release #{release_id} utworzony')

            # 6. Upload asset
            _emit_log(f'Upload {pkg_filename}…')
            with open(pkg_path, 'rb') as f:
                asset_data = f.read()
            upload_req = _ur.Request(
                f'{upload_url}?name={pkg_filename}',
                data=asset_data,
                headers={
                    'Authorization': f'token {token}',
                    'Content-Type': 'application/octet-stream',
                    'User-Agent': 'EthOS-Builder/1.0',
                },
                method='POST',
            )
            with _ur.urlopen(upload_req, timeout=120) as resp:
                asset = json.loads(resp.read().decode())
            _emit_log(f'Asset: {asset.get("browser_download_url", "")}')
            _emit_log(f'✓ GitHub Release v{new_ver} gotowy!')
            _emit_done(True, version=new_ver, url=release.get('html_url', ''))

        except _ue.HTTPError as e:
            body = e.read().decode()[:300]
            _logger.error('[github_release] HTTPError %s: %s', e.code, body)
            _emit_log(f'GitHub API error {e.code}: {body}', error=True)
            _emit_done(False, error=f'GitHub API {e.code}: {body}')
        except Exception as ex:
            _logger.exception('[github_release] %s', ex)
            _emit_log(f'Błąd: {ex}', error=True)
            _emit_done(False, error=str(ex))

    import gevent
    gevent.spawn(_run)
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
        for ftype, local_val in local_files.items():
            paths = local_val if isinstance(local_val, list) else [local_val]
            for file_idx, local_path in enumerate(paths):
                if ftype == 'backend':
                    fname = 'backend.py' if file_idx == 0 else f'backend_{file_idx + 1}.py'
                    remote_key = f'apps/{app_id}/{fname}'
                else:
                    remote_key = f'apps/{app_id}/frontend.js' if file_idx == 0 else f'apps/{app_id}/frontend_{file_idx + 1}.js'
                remote_sha = remote_tree.get(remote_key)

                # Compute git blob SHA for local file
                with open(local_path, 'rb') as f:
                    content = f.read()
                blob_header = f'blob {len(content)}\0'.encode()
                local_sha = hashlib.sha1(blob_header + content).hexdigest()

                if remote_sha is None:
                    changes.append({'file': remote_key.split('/')[-1], 'status': 'new'})
                elif local_sha != remote_sha:
                    changes.append({'file': remote_key.split('/')[-1], 'status': 'modified'})

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
                for ftype, local_val in local_files.items():
                    paths = local_val if isinstance(local_val, list) else [local_val]
                    for file_idx, local_path in enumerate(paths):
                        if ftype == 'backend':
                            fname = 'backend.py' if file_idx == 0 else f'backend_{file_idx + 1}.py'
                        else:
                            fname = 'frontend.js' if file_idx == 0 else f'frontend_{file_idx + 1}.js'
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
    _logger.info('[builder] Processes stopped (uninstall, wipe=%s)', wipe)


register_pkg_routes(
    builder_bp,
    install_message='Builder ready.',
    wipe_files=[_BUILD_STATE_FILE],
    wipe_dirs=[app_path('releases')],
    on_uninstall=_builder_on_uninstall,
)

from blueprints.builder_stages import _build_image_worker  # noqa: E402
