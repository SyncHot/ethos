"""
EthOS — Builder Blueprint
Build releases and system images from the EthOS web panel.
All heavy operations run on the host and stream progress via SSE.
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
}
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
    stream = _host_run_stream_base(cmd)
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
#  API — Build Release (SSE)
# ═══════════════════════════════════════════════════════════

@builder_bp.route('/release', methods=['POST'])
def build_release():
    """Build a release package. Streams progress via SSE."""
    if _build_state['status'] == 'building':
        return jsonify({'error': 'Build already in progress. Wait for completion or cancel.'}), 409
    data = request.json or {}
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
OPTIONAL_JS="{' '.join(sorted(optional_js))}"
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


def _build_image_worker(nasos):
    """Background worker that runs the x86 image build."""
    try:
        wrapper = _x86_wrapper_script(nasos)

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
                    res = {
                        'success': True, 'message': msg,
                        'img': result_info.get('img_path', ''),
                    }
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
            elif line.startswith('LOG:'):
                _update_build(log=line[4:])
            elif line.strip():
                _update_build(log=line)
    except Exception as e:
        msg = f'Exception: {e}'
        _update_build(status='error', message=msg, result={'success': False, 'message': msg})



# ─────────────────────────────────────────────────────────
#  x86 wrapper script — debootstrap + GRUB
# ─────────────────────────────────────────────────────────

def _x86_wrapper_script(nasos: str) -> str:
    """Return bash wrapper script for building x86 image."""
    optional_js_list = ' '.join(_OPTIONAL_JS)
    optional_py_list = ' '.join(_OPTIONAL_PY)
    return f"""
set -e
set -o pipefail
export DEBIAN_FRONTEND=noninteractive

NASOS="{nasos}"

# Check dependencies
echo "STEP:2:Checking dependencies..."
for cmd in debootstrap parted mkfs.ext4 mkfs.vfat grub-install; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "STEP:3:Installing dependencies..."
        apt-get update -qq
        apt-get install -y -qq debootstrap parted dosfstools e2fsprogs \\
            grub-pc-bin grub-efi-amd64-bin grub-common grub2-common \\
            mtools xorriso isolinux debian-archive-keyring 2>/dev/null || true
        break
    fi
done

# Ensure debian-archive-keyring is present (needed on Ubuntu hosts)
if [ ! -f /usr/share/keyrings/debian-archive-keyring.gpg ]; then
    echo "LOG:Installing debian-archive-keyring..."
    apt-get update -qq 2>/dev/null
    apt-get install -y -qq debian-archive-keyring 2>/dev/null || true
fi

echo "STEP:5:Preparing environment..."

# Source config from the script but override with our values
VERSION=$(python3 -c "import json; print(json.load(open('$NASOS/backend/version.json'))['version'])" 2>/dev/null || echo '2.4.0')
BRAND_NAME=$(grep '^ETHOS_BRAND_NAME=' "$NASOS/install.conf" 2>/dev/null | cut -d'"' -f2)
BRAND_NAME=${{BRAND_NAME:-EthOS}}
FINAL_IMG="$NASOS/installer/images/ethos-x86.img"
WORK_DIR="/tmp/ethos-x86-build-web"
IMG_SIZE_GB=8
DEBIAN_RELEASE="bookworm"
DEFAULT_USER="nasadmin"
DEFAULT_HOSTNAME="ethos"
USER_PASS="ethos"
NAS_PORT="9000"

# ── Performance: use tmpfs (RAM) for build if enough memory ──
TOTAL_RAM_MB=$(awk '/MemAvailable/{{print int($2/1024)}}' /proc/meminfo 2>/dev/null || echo 0)
USE_TMPFS=0
if [ "$TOTAL_RAM_MB" -gt 10000 ]; then
    USE_TMPFS=1
    echo "LOG:Available RAM: ${{TOTAL_RAM_MB}}MB — building in tmpfs (RAM) for speed"
    mkdir -p "$WORK_DIR"
    mount -t tmpfs -o size=${{IMG_SIZE_GB}}G,nr_inodes=0 tmpfs "$WORK_DIR"
else
    echo "LOG:Available RAM: ${{TOTAL_RAM_MB}}MB — not enough for tmpfs, building on disk"
    mkdir -p "$WORK_DIR"
fi
OUTPUT_IMG="$WORK_DIR/ethos-x86.img"

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
    if [ "$USE_TMPFS" -eq 1 ]; then
        umount "$WORK_DIR" 2>/dev/null || \
            umount -l "$WORK_DIR" 2>/dev/null || true
    fi
    rm -rf "$WORK_DIR" 2>/dev/null || true
}}
trap cleanup EXIT

# ── Step 1: Create disk image ──
echo "STEP:8:Creating disk image (${{IMG_SIZE_GB}}GB)..."
mkdir -p "$WORK_DIR"/{{root,efi}}
rm -f "$OUTPUT_IMG" "$FINAL_IMG"
truncate -s "${{IMG_SIZE_GB}}G" "$OUTPUT_IMG"

LOOP_DEV=$(losetup --find --show --partscan "$OUTPUT_IMG")
echo "LOG:Loop device: $LOOP_DEV"

parted -s "$LOOP_DEV" mklabel gpt
parted -s "$LOOP_DEV" mkpart ESP fat32 1MiB 257MiB
parted -s "$LOOP_DEV" set 1 esp on
parted -s "$LOOP_DEV" mkpart primary 257MiB 258MiB
parted -s "$LOOP_DEV" set 2 bios_grub on
parted -s "$LOOP_DEV" mkpart primary ext4 258MiB 100%
partprobe "$LOOP_DEV"; sleep 1

mkfs.vfat -F32 "${{LOOP_DEV}}p1"
mkfs.ext4 -q -L "ethos-root" "${{LOOP_DEV}}p3"

mount "${{LOOP_DEV}}p3" "$WORK_DIR/root"
mkdir -p "$WORK_DIR/root/boot/efi"
mount "${{LOOP_DEV}}p1" "$WORK_DIR/root/boot/efi"

echo "STEP:14:Obraz dysku utworzony"

# ── Step 2: Debootstrap ──
echo "STEP:15:Debootstrap — minimal Debian install (this will take a few minutes)..."
PKG_COUNT=0
if [ -d "$DEBOOTSTRAP_CACHE" ] && [ "$(ls -A "$DEBOOTSTRAP_CACHE" 2>/dev/null)" ]; then
    echo "LOG:Using debootstrap cache ($(du -sh "$DEBOOTSTRAP_CACHE" | cut -f1))"
fi
debootstrap --cache-dir="$DEBOOTSTRAP_CACHE" --variant=minbase --include=\\
systemd,systemd-sysv,dbus,\\
linux-image-amd64,\\
grub-pc-bin,grub-efi-amd64-bin,grub-efi-amd64,grub-common,grub2-common,\\
efibootmgr,\\
sudo,openssh-server,curl,ca-certificates,gnupg,lsb-release,fail2ban,\\
iproute2,iputils-ping,wireguard-tools,qrencode,\\
bash,locales,console-setup,\\
python3,python3-minimal,\\
dosfstools,e2fsprogs,parted,util-linux,\\
rsync,smartmontools,ethtool,hdparm,cpufrequtils,\\
cryptsetup,\\
usbutils,pciutils,lm-sensors,nut,\\
avahi-daemon,libnss-mdns,\\
kmod,udev \\
    "$DEBIAN_RELEASE" "$WORK_DIR/root" http://deb.debian.org/debian 2>&1 | \\
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

# Verify debootstrap succeeded
if [ ! -d "$WORK_DIR/root/dev" ] || [ ! -d "$WORK_DIR/root/etc" ]; then
    echo "LOG:ERROR: debootstrap did not create rootfs — check logs"
    echo "STEP:45:Debootstrap failed"
    exit 1
fi

echo "STEP:45:Debian installed. Configuring system..."

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
ROOT_UUID=$(blkid -s UUID -o value "${{LOOP_DEV}}p3")
EFI_UUID=$(blkid -s UUID -o value "${{LOOP_DEV}}p1")

if [ -z "$ROOT_UUID" ]; then
    echo "LOG:ERROR: Failed to read root partition UUID (${{LOOP_DEV}}p3)"
    exit 1
fi
if [ -z "$EFI_UUID" ]; then
    echo "LOG:ERROR: Failed to read EFI partition UUID (${{LOOP_DEV}}p1)"
    exit 1
fi

cat > "$ROOT/etc/fstab" <<FSTAB
UUID=$ROOT_UUID  /          ext4  noatime,errors=remount-ro  0 1
UUID=$EFI_UUID   /boot/efi  vfat  umask=0077         0 1
/swapfile        none       swap  sw                 0 0
FSTAB

echo "$DEFAULT_HOSTNAME" > "$ROOT/etc/hostname"

# ── Swap file (4 GB) ──
echo "LOG:Creating swap file..."
fallocate -l 4G "$ROOT/swapfile"
chmod 600 "$ROOT/swapfile"
mkswap "$ROOT/swapfile" >/dev/null

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
ACTION=="add|change", KERNEL=="nvme*", ATTR{{queue/scheduler}}="none"
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

cat > "$ROOT/etc/apt/sources.list" <<APT
deb http://deb.debian.org/debian $DEBIAN_RELEASE main contrib non-free non-free-firmware
deb http://deb.debian.org/debian $DEBIAN_RELEASE-updates main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security $DEBIAN_RELEASE-security main contrib non-free non-free-firmware
deb http://deb.debian.org/debian $DEBIAN_RELEASE-backports main contrib non-free non-free-firmware
APT

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
# Enable UFW non-interactively
chroot "$ROOT" bash -c 'echo "y" | ufw enable' 2>/dev/null || true
chroot "$ROOT" systemctl enable ufw 2>/dev/null || true

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
ID_LIKE=debian
HOME_URL="https://ethos.local"
OSREL

cat > "$ROOT/etc/issue" <<ISSUE
$BRAND_NAME \\n \\l

ISSUE
echo "$BRAND_NAME" > "$ROOT/etc/issue.net"

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
echo "STEP:53:Installing GRUB (BIOS + UEFI)..."

echo "LOG:apt-get update in chroot..."
chroot "$ROOT" apt-get update -qq 2>&1 | tail -3 || true
echo "LOG:Installing GRUB packages..."
chroot "$ROOT" bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq grub-efi-amd64 grub-pc-bin grub-common efibootmgr' 2>&1 | tail -5 || true

echo "LOG:GRUB BIOS install..."
chroot "$ROOT" grub-install --target=i386-pc --boot-directory=/boot "$LOOP_DEV" 2>/dev/null || \\
    grub-install --target=i386-pc --boot-directory="$ROOT/boot" "$LOOP_DEV" 2>/dev/null || \\
    echo "LOG:BIOS grub-install warning (UEFI ok)"

mkdir -p "$ROOT/boot/efi/EFI/BOOT"
echo "LOG:GRUB UEFI install..."
chroot "$ROOT" grub-install --target=x86_64-efi --efi-directory=/boot/efi \\
    --boot-directory=/boot --removable --no-nvram 2>/dev/null || {{
    echo "STEP:0:ERROR: UEFI grub-install failed!"; exit 1;
}}

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
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro quiet net.ifnames=0 biosdevname=0 fsck.repair=preen
    initrd ${{INITRD}}
}}
menuentry "EthOS v${{VERSION}} (recovery)" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro single nomodeset fsck.repair=preen
    initrd ${{INITRD}}
}}
GRUBCFG

cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/EFI/BOOT/grub.cfg"

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
chroot "$ROOT" apt-get install -y -qq \
    firmware-atheros firmware-realtek firmware-brcm80211 \
    firmware-misc-nonfree firmware-linux-nonfree bluez firmware-intel-sound \
    2>&1 | tail -10 || echo "LOG:Some firmware skipped"

# All other packages (storage tools, sensors, printer, archives, etc.)
# are installed lazily by EthOS (ensure_dep) when user enables features.
# Builder tools are pre-installed so image creation works out of the box.
echo "LOG:Installing builder tools..."
chroot "$ROOT" apt-get install -y -qq \
    debootstrap squashfs-tools xorriso isolinux \
    parted dosfstools e2fsprogs mtools \
    2>&1 | tail -5 || echo "LOG:Some builder tools skipped"

echo "STEP:73:Installing kernel and firmware from backports..."

# First clean apt cache to free space before big installs
chroot "$ROOT" apt-get clean 2>/dev/null || true
echo "LOG:Disk usage before backports:"
df -h "$ROOT" 2>/dev/null | tail -1 || true

echo "LOG:Installing linux-image-amd64 from backports..."
chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports linux-image-amd64 2>&1 | tail -5 || echo "LOG:Backports kernel skipped"

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

echo "LOG:Installing firmware-iwlwifi from backports..."
chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-iwlwifi 2>&1 | tail -5 || echo "LOG:Backports iwlwifi skipped"
echo "LOG:Installing firmware-realtek from backports..."
chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-realtek 2>&1 | tail -5 || echo "LOG:Backports realtek skipped"
echo "LOG:Installing firmware-misc-nonfree from backports..."
chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-misc-nonfree 2>&1 | tail -5 || echo "LOG:Backports misc skipped"

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

# Rebuild initramfs with firmware (only for the new kernel)
echo "LOG:Przebudowa initramfs..."
if [[ -n "$NEW_KERN" ]]; then
    chroot "$ROOT" update-initramfs -u -k "$NEW_KERN" 2>&1 | tail -5 || echo "LOG:initramfs update failed"
else
    chroot "$ROOT" update-initramfs -u -k all 2>/dev/null || echo "LOG:initramfs update failed"
fi

echo "STEP:75:Dependencies installed"

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
ENVFILE
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
After=network.target
Wants=network.target
Conflicts=ethos-preboot.service

[Service]
Type=notify
NotifyAccess=all
WorkingDirectory=/opt/ethos
EnvironmentFile=/opt/ethos/ethos.env
ExecStartPre=/bin/mkdir -p /opt/ethos/data /opt/ethos/logs /opt/ethos/backups /opt/ethos/uploads
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

sync

for m in boot/efi run sys proc dev/shm dev/pts dev; do
    umount "$ROOT/$m" 2>/dev/null || \
        umount -l "$ROOT/$m" 2>/dev/null || true
done
sleep 1
umount "$ROOT" 2>/dev/null || \
    umount -l "$ROOT" 2>/dev/null || true

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

echo "STEP:100:Obraz gotowy!"
echo "RESULT_IMG:$OUTPUT_IMG:$IMG_SIZE"
"""


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
