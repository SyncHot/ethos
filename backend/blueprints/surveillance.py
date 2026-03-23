"""
EthOS Surveillance Station — camera discovery, live streams, recording & playback.

Installable package: on first open the UI checks /api/surveillance/status and
offers to install dependencies (ffmpeg, python3-pip → onvif-zeep, etc.).

Architecture:
  * cameras.json — persisted camera list (id, name, url, onvif creds, etc.)
  * recordings stored under DATA_DIR/surveillance/YYYY-MM-DD/<cam_id>/
  * ffmpeg subprocesses handle RTSP→HLS transcoding (live) and recording segments
  * ONVIF + network scan for camera auto-discovery
"""
from __future__ import annotations

import json, logging, os, re, secrets, shutil, signal, socket, subprocess, threading, time, glob
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, send_file, Response

from host import data_path, NATIVE_MODE
from utils import load_json as _load_json, save_json as _save_json

log = logging.getLogger('surveillance')
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[%(name)s] %(message)s'))
    log.addHandler(_h)
log.setLevel(logging.INFO)

surveillance_bp = Blueprint('surveillance', __name__, url_prefix='/api/surveillance')

# ─── Paths ────────────────────────────────────────────────────
DATA_DIR = data_path('surveillance')
CAMERAS_FILE = data_path('surveillance_cameras.json')
SETTINGS_FILE = data_path('surveillance_settings.json')
HLS_DIR = os.path.join(DATA_DIR, 'hls')           # transient HLS chunks
RECORDINGS_DIR = os.path.join(DATA_DIR, 'recordings')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(RECORDINGS_DIR, exist_ok=True)

_socketio = None

# Per-camera error/status tracking: cam_id -> { 'last_error': str, 'last_error_time': str, 'retries': int }
_cam_errors = {}

# Locks for thread-safe access to global mutable state
_state_lock = threading.Lock()
_json_lock = threading.Lock()

def init_surveillance(sio):
    global _socketio
    _socketio = sio


# ═══════════════════════════════════════════════════════════════
# Helpers — persistence
# ═══════════════════════════════════════════════════════════════

def _load_cameras():
    with _json_lock:
        return _load_json(CAMERAS_FILE, [])

def _save_cameras(cams):
    with _json_lock:
        _save_json(CAMERAS_FILE, cams)

def _load_settings():
    defaults = {
        'recording_enabled': False,
        'recording_mode': 'continuous',      # 'continuous' | 'events'
        'segment_minutes': 15,
        'retention_days': 30,
        'recordings_path': RECORDINGS_DIR,
        'motion_detection': False,
        'motion_sensitivity': 50,
        'event_pre_seconds': 5,               # pre-event buffer
        'event_post_seconds': 15,             # post-event recording
        'event_cooldown': 10,                 # min seconds between events
        'auto_discovery': False,
        'auto_discovery_interval': 300,
    }
    with _json_lock:
        saved = _load_json(SETTINGS_FILE, None)
    if saved is not None:
        defaults.update(saved)
    return defaults

def _save_settings(s):
    with _json_lock:
        _save_json(SETTINGS_FILE, s)


# ═══════════════════════════════════════════════════════════════
# RTSP URL probing & friendly error messages
# ═══════════════════════════════════════════════════════════════

def _probe_rtsp_url(url, timeout=7):
    """Test RTSP URL with ffprobe. Returns (ok: bool, message: str, details: dict)."""
    if not shutil.which('ffprobe'):
        return True, 'ffprobe unavailable — skipping validation', {}
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
             '-show_entries', 'stream=codec_name,width,height',
             '-of', 'json', url],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            info = {}
            try:
                data = json.loads(r.stdout)
                streams = data.get('streams', [])
                if streams:
                    info = {'codec': streams[0].get('codec_name', ''),
                            'width': streams[0].get('width'), 'height': streams[0].get('height')}
            except Exception:
                pass
            return True, 'Connection OK', info
        stderr = r.stderr.strip()
        return False, _friendly_rtsp_error(stderr, url), {'raw': stderr[-300:]}
    except subprocess.TimeoutExpired:
        return False, 'Timeout — camera not responding within {}s'.format(timeout), {}
    except Exception as e:
        return False, str(e), {}


def _friendly_rtsp_error(stderr, url=''):
    """Translate common ffmpeg/ffprobe RTSP errors into user-friendly messages."""
    s = stderr.lower()
    # Extract host:port from URL for diagnostic hints
    host_port = ''
    m = re.search(r'rtsp://(?:[^@]+@)?([^/]+)', url)
    if m:
        host_port = m.group(1)

    if 'connection refused' in s:
        msg = f'Connection refused ({host_port}). RTSP service not listening on this port.'
        # Check if this might be a go2rtc port mismatch (only for local URLs)
        host_only = host_port.split(':')[0] if host_port else ''
        is_local = host_only in ('127.0.0.1', 'localhost', _get_local_ip())
        if is_local:
            go2rtc_info = _detect_native_go2rtc_port()
            if go2rtc_info and host_port:
                try:
                    port_in_url = int(host_port.split(':')[1]) if ':' in host_port else 554
                    if port_in_url != go2rtc_info['rtsp_port']:
                        msg += f' go2rtc detected on port {go2rtc_info["rtsp_port"]} (URL has port {port_in_url}).'
                except (ValueError, IndexError):
                    pass
        return msg
    if '401' in s or 'unauthorized' in s:
        return 'Authorization error (401). Check username and password in RTSP URL.'
    if '404' in s or 'not found' in s:
        return 'Stream not found (404). Check the path in URL (e.g. /live0, /stream).'
    if '403' in s or 'forbidden' in s:
        return 'Access forbidden (403). Camera refuses connection.'
    if 'no route to host' in s:
        return f'Host unreachable ({host_port}). Check IP address and network.'
    if 'connection timed out' in s or 'timed out' in s:
        return f'Connection timeout to {host_port}. Camera not responding.'
    if 'invalid data' in s:
        return 'Invalid data — camera responds but format is unknown.'
    return stderr[:200] if stderr else 'Unknown connection error'


def _detect_native_go2rtc_port():
    """Detect if go2rtc is running natively and return its RTSP port.
    Returns dict {'pid': int, 'rtsp_port': int, 'bind': str} or None."""
    try:
        r = subprocess.run(['pgrep', '-a', 'go2rtc'], capture_output=True, text=True, timeout=3)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        # Parse first match: "PID /bin/go2rtc -c /path/to/config.yaml"
        line = r.stdout.strip().splitlines()[0]
        parts = line.split()
        pid = int(parts[0])

        # Try to find RTSP listen port from /proc/PID/net/tcp (look for LISTEN sockets)
        rtsp_port = None
        try:
            with open(f'/proc/{pid}/net/tcp') as f:
                for tcp_line in f:
                    fields = tcp_line.strip().split()
                    if len(fields) < 4:
                        continue
                    # st=0A means LISTEN
                    if fields[3] != '0A':
                        continue
                    local = fields[1]
                    hex_port = local.split(':')[1]
                    port = int(hex_port, 16)
                    # go2rtc RTSP is typically 8554 or 18554
                    if port in (8554, 18554, 8555, 18555) or (8000 < port < 20000):
                        # RTSP ports are usually in 8554/18554 range; WebRTC/API on 1984
                        bind_addr = local.split(':')[0]
                        # Decode bind address
                        if bind_addr == '00000000':
                            bind_str = '0.0.0.0'
                        elif bind_addr == '0100007F':
                            bind_str = '127.0.0.1'
                        else:
                            # Little-endian hex to IP
                            b = bytes.fromhex(bind_addr)
                            bind_str = f'{b[3]}.{b[2]}.{b[1]}.{b[0]}'
                        if rtsp_port is None or port in (8554, 18554):
                            rtsp_port = port
                            bind = bind_str
        except Exception:
            pass

        if rtsp_port:
            return {'pid': pid, 'rtsp_port': rtsp_port, 'bind': bind}
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# Dependency management (installable package)
# ═══════════════════════════════════════════════════════════════

_REQUIRED_BINS = ['ffmpeg', 'ffprobe']

def _check_installed():
    """Return dict of which dependencies are available."""
    result = {
        'ffmpeg': shutil.which('ffmpeg') is not None,
        'ffprobe': shutil.which('ffprobe') is not None,
        'onvif': False,
    }
    try:
        import onvif  # noqa
        result['onvif'] = True
    except ImportError:
        pass
    return result

def _all_deps_ok():
    d = _check_installed()
    return d['ffmpeg'] and d['ffprobe']


@surveillance_bp.route('/status')
def status():
    """Check if surveillance deps are installed and return overall status."""
    deps = _check_installed()
    installed = deps['ffmpeg'] and deps['ffprobe']
    return jsonify({
        'installed': installed,
        'deps': deps,
        'cameras': len(_load_cameras()),
        'streams_active': len(_streams),
        'recording': _load_settings().get('recording_enabled', False),
    })


@surveillance_bp.route('/install', methods=['POST'])
def install_deps():
    """Install ffmpeg + onvif library. Returns immediately; progress via SocketIO."""
    task_id = secrets.token_hex(8)

    def _bg():
        def _emit(stage, pct, msg):
            if _socketio:
                _socketio.emit('surveillance_install', {
                    'task_id': task_id, 'stage': stage, 'percent': pct, 'message': msg
                })

        _emit('start', 0, 'Installing ffmpeg…')
        try:
            r = subprocess.run(
                ['apt-get', 'install', '-y', 'ffmpeg'],
                capture_output=True, text=True, timeout=300
            )
            if r.returncode != 0:
                _emit('error', 0, f'ffmpeg installation error: {r.stderr[:300]}')
                return
            _emit('progress', 50, 'ffmpeg installed. Installing python-onvif…')

            # Install ONVIF library
            pip = shutil.which('pip3') or shutil.which('pip')
            if pip:
                r2 = subprocess.run(
                    [pip, 'install', 'onvif-zeep'],
                    capture_output=True, text=True, timeout=120
                )
                if r2.returncode != 0:
                    _emit('progress', 80, 'python-onvif not installed (optional)')
                else:
                    _emit('progress', 90, 'python-onvif installed')

            _emit('done', 100, 'Surveillance Station gotowe!')
        except Exception as e:
            _emit('error', 0, str(e))

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return jsonify({'ok': True, 'task_id': task_id})


@surveillance_bp.route('/uninstall', methods=['POST'])
def uninstall_deps():
    """Stop all surveillance processes. Optionally wipe data."""
    wipe = (request.json or {}).get('wipe_data', False)

    # Stop everything
    for cam_id in list(_streams.keys()):
        _stop_stream(cam_id)
    for cam_id in list(_recorders.keys()):
        _stop_recorder(cam_id)
    for cam_id in list(_event_monitors.keys()):
        _stop_event_monitor(cam_id)
    for cam_id in list(_event_clips.keys()):
        _stop_event_clip(cam_id)

    # Stop health monitor thread
    _health_stop.set()

    # Stop auto-discovery thread
    global _auto_discovery_thread
    _auto_discovery_stop.set()
    if _auto_discovery_thread and _auto_discovery_thread.is_alive():
        _auto_discovery_thread.join(timeout=5)
    _auto_discovery_thread = None

    # Reset settings
    settings = _load_settings()
    settings['recording_enabled'] = False
    settings['auto_discovery'] = False
    _save_settings(settings)

    _cam_errors.clear()

    if wipe:
        # Remove recordings + HLS data
        if os.path.isdir(RECORDINGS_DIR):
            shutil.rmtree(RECORDINGS_DIR, ignore_errors=True)
            os.makedirs(RECORDINGS_DIR, exist_ok=True)
        if os.path.isdir(HLS_DIR):
            shutil.rmtree(HLS_DIR, ignore_errors=True)
            os.makedirs(HLS_DIR, exist_ok=True)

    log.info('[api] Surveillance uninstalled (wipe_data=%s)', wipe)
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════
# Camera CRUD
# ═══════════════════════════════════════════════════════════════

@surveillance_bp.route('/cameras')
def list_cameras():
    cams = _load_cameras()
    _cleanup_dead_procs()
    with _state_lock:
        streams_snap = set(_streams)
        recorders_snap = set(_recorders)
        errors_snap = {k: dict(v) for k, v in _cam_errors.items()}
    for c in cams:
        cid = c['id']
        c['streaming'] = cid in streams_snap
        c['recording'] = cid in recorders_snap
        err_info = errors_snap.get(cid, {})
        c['last_error'] = err_info.get('last_error', '')
        c['last_error_time'] = err_info.get('last_error_time', '')
        c['retries'] = err_info.get('retries', 0)
    return jsonify(cams)

def _cleanup_dead_procs():
    """Remove entries whose ffmpeg process has exited and log the reason."""
    to_remove = []
    with _state_lock:
        for label, d in (('stream', _streams), ('recorder', _recorders)):
            for k, v in list(d.items()):
                if v['proc'].poll() is not None:
                    to_remove.append((label, d, k, v))

    for label, d, k, info in to_remove:
        code = info['proc'].returncode
        stderr = ''
        try:
            stderr = info['proc'].stderr.read().decode(errors='replace')[-500:]
        except Exception:
            pass
        finally:
            if info['proc'].stderr:
                try:
                    info['proc'].stderr.close()
                except Exception:
                    pass
        cam_url = ''
        try:
            cams = _load_cameras()
            cam = next((c for c in cams if c['id'] == k), None)
            if cam:
                cam_url = cam.get('substream_url') or cam.get('url', '')
        except Exception:
            pass
        friendly = _friendly_rtsp_error(stderr, cam_url) if stderr.strip() else f'ffmpeg exited with code {code}'
        log.warning('[%s:%s] ffmpeg exited code=%s: %s', label, k, code, friendly)
        with _state_lock:
            d.pop(k, None)
            _cam_errors[k] = {
                'last_error': friendly,
                'last_error_time': datetime.now().isoformat(),
                'retries': _cam_errors.get(k, {}).get('retries', 0),
            }


@surveillance_bp.route('/cameras', methods=['POST'])
def add_camera():
    data = request.json or {}
    name = data.get('name', '').strip()
    url = data.get('url', '').strip()         # RTSP URL
    if not name or not url:
        return jsonify({'error': 'Name and URL required'}), 400

    # Probe RTSP URL (non-blocking warning)
    probe_ok, probe_msg, probe_info = _probe_rtsp_url(url)

    cam = {
        'id': secrets.token_hex(6),
        'name': name,
        'url': url,
        'onvif_host': data.get('onvif_host', ''),
        'onvif_port': int(data.get('onvif_port', 80)),
        'onvif_user': data.get('onvif_user', ''),
        'onvif_pass': data.get('onvif_pass', ''),
        'enabled': True,
        'record': data.get('record', False),
        'recording_mode': data.get('recording_mode', 'continuous'),
        'substream_url': data.get('substream_url', ''),
        'added': datetime.now().isoformat(),
    }
    cams = _load_cameras()
    cams.append(cam)
    _save_cameras(cams)

    result = dict(cam)
    if not probe_ok:
        result['warning'] = probe_msg
        with _state_lock:
            _cam_errors[cam['id']] = {
                'last_error': probe_msg,
                'last_error_time': datetime.now().isoformat(),
                'retries': 0,
            }
    elif probe_info:
        result['stream_info'] = probe_info
    return jsonify(result)


@surveillance_bp.route('/cameras/<cam_id>', methods=['PUT'])
def update_camera(cam_id):
    data = request.json or {}
    cams = _load_cameras()
    cam = next((c for c in cams if c['id'] == cam_id), None)
    if not cam:
        return jsonify({'error': 'Camera not found'}), 404
    for k in ('name', 'url', 'onvif_host', 'onvif_port', 'onvif_user', 'onvif_pass',
              'enabled', 'record', 'substream_url', 'recording_mode'):
        if k in data:
            cam[k] = data[k]
    _save_cameras(cams)
    # Restart stream if active
    with _state_lock:
        stream_active = cam_id in _streams
    if stream_active:
        _stop_stream(cam_id)
        if cam['enabled']:
            _start_stream(cam)
    # Restart recorder if recording_mode changed or record toggled while recording active
    settings = _load_settings()
    if settings.get('recording_enabled') and cam.get('record'):
        with _state_lock:
            has_recorder = cam_id in _recorders
            has_event_mon = cam_id in _event_monitors
        old_mode = 'continuous'
        if has_recorder:
            old_mode = 'continuous'
        elif has_event_mon:
            old_mode = 'events'
        new_mode = cam.get('recording_mode', 'continuous')
        if old_mode != new_mode or ('recording_mode' in data):
            _stop_recorder(cam_id)
            _stop_event_monitor(cam_id)
            _start_recorder(cam, settings)
    return jsonify(cam)


@surveillance_bp.route('/cameras/<cam_id>', methods=['DELETE'])
def delete_camera(cam_id):
    _stop_stream(cam_id)
    _stop_recorder(cam_id)
    with _state_lock:
        _cam_errors.pop(cam_id, None)
    cams = _load_cameras()
    cams = [c for c in cams if c['id'] != cam_id]
    _save_cameras(cams)
    # Clean up HLS dir
    hls_path = os.path.join(HLS_DIR, cam_id)
    if os.path.isdir(hls_path):
        shutil.rmtree(hls_path, ignore_errors=True)
    return jsonify({'ok': True})


@surveillance_bp.route('/cameras/<cam_id>/clear_error', methods=['POST'])
def clear_camera_error(cam_id):
    """Reset error counter so health monitor retries this camera."""
    with _state_lock:
        _cam_errors.pop(cam_id, None)
    log.info('[api] Cleared error state for camera %s', cam_id)
    return jsonify({'ok': True})


@surveillance_bp.route('/cameras/<cam_id>/diagnose', methods=['POST'])
def diagnose_camera(cam_id):
    """Run full connectivity diagnostics for a camera."""
    cams = _load_cameras()
    cam = next((c for c in cams if c['id'] == cam_id), None)
    if not cam:
        return jsonify({'error': 'Camera not found'}), 404

    url = cam.get('substream_url') or cam['url']
    result = {'camera': cam['name'], 'url_masked': re.sub(r'://[^@]+@', '://***@', url), 'checks': []}

    # 1. Parse host:port from RTSP URL
    m = re.search(r'rtsp://(?:[^@]+@)?([^/:]+)(?::(\d+))?', url)
    if not m:
        result['checks'].append({'name': 'URL', 'ok': False, 'msg': 'Invalid RTSP URL format'})
        return jsonify(result)

    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else 554

    # 2. Ping test
    try:
        r = subprocess.run(['ping', '-c', '1', '-W', '2', host],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            # Extract latency
            latency = ''
            lm = re.search(r'time=(\S+)', r.stdout)
            if lm:
                latency = lm.group(1) + 'ms'
            result['checks'].append({'name': 'Ping', 'ok': True, 'msg': f'Host {host} reachable ({latency})'})
        else:
            result['checks'].append({'name': 'Ping', 'ok': False, 'msg': f'Host {host} unreachable'})
    except Exception:
        result['checks'].append({'name': 'Ping', 'ok': False, 'msg': 'Ping failed'})

    # 3. TCP port test
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((host, port))
        s.close()
        result['checks'].append({'name': 'Port', 'ok': True, 'msg': f'Port {port} open'})
    except socket.timeout:
        result['checks'].append({'name': 'Port', 'ok': False, 'msg': f'Port {port} — timeout'})
    except ConnectionRefusedError:
        result['checks'].append({'name': 'Port', 'ok': False, 'msg': f'Port {port} — connection refused'})
    except Exception as e:
        result['checks'].append({'name': 'Port', 'ok': False, 'msg': f'Port {port} — {e}'})

    # 4. RTSP probe
    probe_ok, probe_msg, probe_info = _probe_rtsp_url(url)
    check = {'name': 'RTSP', 'ok': probe_ok, 'msg': probe_msg}
    if probe_info and not probe_info.get('raw'):
        check['info'] = probe_info
    result['checks'].append(check)

    # 5. go2rtc detection (if URL points to localhost/local IP)
    local_ips = set()
    try:
        for addr_info in socket.getaddrinfo(socket.gethostname(), None):
            local_ips.add(addr_info[4][0])
        local_ips.update({'127.0.0.1', 'localhost'})
        # Also add LAN IP
        lip = _get_local_ip()
        if lip:
            local_ips.add(lip)
    except Exception:
        pass

    if host in local_ips or host == 'localhost':
        go2rtc_info = _detect_native_go2rtc_port()
        if go2rtc_info:
            actual_port = go2rtc_info['rtsp_port']
            bind = go2rtc_info.get('bind', '?')
            if actual_port != port:
                result['checks'].append({
                    'name': 'go2rtc',
                    'ok': False,
                    'msg': f'go2rtc listening on port {actual_port} ({bind}), but camera URL has port {port}',
                    'suggestion': re.sub(r':\d+/', f':{actual_port}/', url) if f':{port}/' in url else None,
                })
            else:
                result['checks'].append({
                    'name': 'go2rtc',
                    'ok': True,
                    'msg': f'go2rtc detected on port {actual_port} ({bind}) — matches URL'
                })
        else:
            result['checks'].append({
                'name': 'go2rtc',
                'ok': False,
                'msg': 'URL points to localhost, but go2rtc is not running'
            })

    # 6. Summary
    all_ok = all(c['ok'] for c in result['checks'])
    result['status'] = 'ok' if all_ok else 'error'
    result['summary'] = 'All tests passed' if all_ok else 'Connection issues detected'

    return jsonify(result)


@surveillance_bp.route('/cameras/<cam_id>/snapshot')
def camera_snapshot(cam_id):
    """Grab a single JPEG frame from the camera RTSP stream."""
    cams = _load_cameras()
    cam = next((c for c in cams if c['id'] == cam_id), None)
    if not cam:
        return jsonify({'error': 'Camera not found'}), 404

    if not _all_deps_ok():
        return jsonify({'error': 'ffmpeg not installed'}), 503

    try:
        r = subprocess.run([
            'ffmpeg', '-y', '-rtsp_transport', 'tcp',
            '-i', cam['url'],
            '-frames:v', '1', '-f', 'image2', '-'
        ], capture_output=True, timeout=10)
        if r.returncode != 0 or not r.stdout:
            return jsonify({'error': 'Failed to capture frame'}), 502
        return Response(r.stdout, mimetype='image/jpeg')
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Camera connection timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# Live Streaming (RTSP → HLS via ffmpeg)
# ═══════════════════════════════════════════════════════════════

_streams = {}   # cam_id -> { 'proc': subprocess.Popen, 'started': float }

def _start_stream(cam):
    cam_id = cam['id']
    with _state_lock:
        if cam_id in _streams:
            return  # already running

    hls_path = os.path.join(HLS_DIR, cam_id)
    os.makedirs(hls_path, exist_ok=True)
    playlist = os.path.join(hls_path, 'stream.m3u8')

    url = cam.get('substream_url') or cam['url']
    log.info('[stream:%s] Starting ffmpeg for %s → %s', cam_id, cam.get('name', '?'), url[:80])

    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'warning',
        '-rtsp_transport', 'tcp',
        '-i', url,
        '-c:v', 'copy', '-c:a', 'aac',
        '-f', 'hls',
        '-hls_time', '2',
        '-hls_list_size', '6',
        '-hls_flags', 'delete_segments+append_list',
        '-hls_segment_filename', os.path.join(hls_path, 'seg_%03d.ts'),
        playlist
    ]

    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                preexec_fn=lambda: os.nice(10))
        with _state_lock:
            _streams[cam_id] = {'proc': proc, 'started': time.time(), 'error': None}
        # Wait briefly to detect immediate failures
        time.sleep(3)
        if proc.poll() is not None:
            with _state_lock:
                _streams.pop(cam_id, None)
            stderr = ''
            try:
                stderr = proc.stderr.read().decode(errors='replace')[-500:]
            except Exception:
                pass
            err_msg = _friendly_rtsp_error(stderr, url) if stderr else f'ffmpeg exited with code {proc.returncode}'
            log.error('[stream:%s] ffmpeg failed immediately: %s', cam_id, err_msg)
            with _state_lock:
                _cam_errors[cam_id] = {
                    'last_error': err_msg,
                    'last_error_time': datetime.now().isoformat(),
                    'retries': _cam_errors.get(cam_id, {}).get('retries', 0) + 1,
                }
            return err_msg
        log.info('[stream:%s] ffmpeg started OK (pid=%d)', cam_id, proc.pid)
        # Clear error on success
        with _state_lock:
            _cam_errors.pop(cam_id, None)
    except Exception as e:
        log.error('[stream:%s] Failed to spawn ffmpeg: %s', cam_id, e)
        with _state_lock:
            _cam_errors[cam_id] = {
                'last_error': str(e),
                'last_error_time': datetime.now().isoformat(),
                'retries': _cam_errors.get(cam_id, {}).get('retries', 0) + 1,
            }
        return str(e)
    finally:
        if proc and proc.stderr:
            try:
                proc.stderr.close()
            except Exception:
                pass
    return None


def _stop_stream(cam_id):
    with _state_lock:
        info = _streams.pop(cam_id, None)
    if info and info['proc'].poll() is None:
        try:
            info['proc'].send_signal(signal.SIGTERM)
            info['proc'].wait(timeout=5)
        except Exception:
            try:
                info['proc'].kill()
            except Exception:
                pass


@surveillance_bp.route('/stream/<cam_id>/start', methods=['POST'])
def stream_start(cam_id):
    if not _all_deps_ok():
        return jsonify({'error': 'ffmpeg not installed'}), 503
    cams = _load_cameras()
    cam = next((c for c in cams if c['id'] == cam_id), None)
    if not cam:
        return jsonify({'error': 'Camera not found'}), 404
    err = _start_stream(cam)
    if err:
        return jsonify({'ok': False, 'error': f'Cannot start stream: {err}'}), 502
    return jsonify({'ok': True, 'hls': f'/api/surveillance/stream/{cam_id}/hls/stream.m3u8'})


@surveillance_bp.route('/stream/<cam_id>/stop', methods=['POST'])
def stream_stop(cam_id):
    _stop_stream(cam_id)
    return jsonify({'ok': True})


@surveillance_bp.route('/stream/<cam_id>/hls/<path:filename>')
def stream_hls(cam_id, filename):
    """Serve HLS playlist and segments."""
    hls_path = os.path.join(HLS_DIR, cam_id)
    fpath = os.path.join(hls_path, filename)
    if not os.path.isfile(fpath):
        return '', 404
    mime = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/MP2T'
    return send_file(fpath, mimetype=mime, max_age=1)


# ═══════════════════════════════════════════════════════════════
# Recording engine (segment-based + event-based)
# ═══════════════════════════════════════════════════════════════

_recorders = {}       # cam_id -> { 'proc': Popen, 'started': float }
_event_monitors = {}  # cam_id -> { 'thread': Thread, 'stop': Event }
_event_clips = {}     # cam_id -> { 'proc': Popen, 'started': float, 'file': str }

def _rec_dir_for_cam(cam_id, settings=None):
    if not settings:
        settings = _load_settings()
    rec_base = settings.get('recordings_path', RECORDINGS_DIR)
    today = datetime.now().strftime('%Y-%m-%d')
    d = os.path.join(rec_base, today, cam_id)
    os.makedirs(d, exist_ok=True)
    return d


def _start_recorder(cam, settings=None):
    """Start continuous or event recording depending on per-camera mode."""
    cam_id = cam['id']
    if not settings:
        settings = _load_settings()

    # Per-camera recording_mode; fall back to global setting
    mode = cam.get('recording_mode', settings.get('recording_mode', 'continuous'))
    if mode == 'events':
        _start_event_monitor(cam, settings)
    else:
        _start_continuous_recorder(cam, settings)


def _start_continuous_recorder(cam, settings=None):
    cam_id = cam['id']
    with _state_lock:
        if cam_id in _recorders:
            return
    if not settings:
        settings = _load_settings()

    rec_dir = _rec_dir_for_cam(cam_id, settings)
    seg_min = settings.get('segment_minutes', 15)
    seg_sec = seg_min * 60

    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'warning',
        '-rtsp_transport', 'tcp',
        '-i', cam['url'],
        '-c:v', 'copy', '-c:a', 'aac',
        '-f', 'segment',
        '-segment_time', str(seg_sec),
        '-segment_format', 'mp4',
        '-strftime', '1',
        '-reset_timestamps', '1',
        os.path.join(rec_dir, '%Y%m%d_%H%M%S.mp4')
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=lambda: os.nice(10))
        with _state_lock:
            _recorders[cam_id] = {'proc': proc, 'started': time.time(), 'dir': rec_dir, 'mode': 'continuous'}
        log.info('[recorder:%s] Continuous recording started → %s', cam_id, rec_dir)
    except Exception as e:
        log.error('[recorder:%s] Failed to start recorder: %s', cam_id, e)


# ── Event / motion-based recording ──────────────────────────

def _start_event_monitor(cam, settings=None):
    """Start a motion detector thread that records clips on events."""
    cam_id = cam['id']
    with _state_lock:
        if cam_id in _event_monitors:
            return
    if not settings:
        settings = _load_settings()

    stop_ev = threading.Event()
    t = threading.Thread(target=_event_monitor_loop, args=(cam, settings, stop_ev), daemon=True)
    t.start()
    with _state_lock:
        _event_monitors[cam_id] = {'thread': t, 'stop': stop_ev}
    log.info('[event:%s] Motion event monitor started for %s', cam_id, cam.get('name'))


def _stop_event_monitor(cam_id):
    with _state_lock:
        info = _event_monitors.pop(cam_id, None)
    if info:
        info['stop'].set()
        if 'thread' in info and info['thread'].is_alive():
            info['thread'].join(timeout=10)
    # Also stop any running event clip
    _stop_event_clip(cam_id)


def _stop_event_clip(cam_id):
    with _state_lock:
        info = _event_clips.pop(cam_id, None)
    if info and info['proc'].poll() is None:
        try:
            info['proc'].send_signal(signal.SIGTERM)
            info['proc'].wait(timeout=5)
        except Exception:
            try: info['proc'].kill()
            except Exception: pass


def _start_event_clip(cam, duration, settings=None):
    """Record a single clip of given duration (seconds)."""
    cam_id = cam['id']
    # If already recording a clip, let it finish
    with _state_lock:
        if cam_id in _event_clips and _event_clips[cam_id]['proc'].poll() is None:
            return

    if not settings:
        settings = _load_settings()

    rec_dir = _rec_dir_for_cam(cam_id, settings)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'event_{ts}.mp4'
    filepath = os.path.join(rec_dir, filename)

    pre_sec = settings.get('event_pre_seconds', 5)

    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'warning',
        '-rtsp_transport', 'tcp',
        '-i', cam['url'],
        '-c:v', 'copy', '-c:a', 'aac',
        '-t', str(pre_sec + duration),
        '-movflags', '+faststart',
        '-y', filepath
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=lambda: os.nice(10))
        with _state_lock:
            _event_clips[cam_id] = {'proc': proc, 'started': time.time(), 'file': filepath}
        log.info('[event:%s] Recording event clip (%ds) → %s', cam_id, pre_sec + duration, filename)
    except Exception as e:
        log.error('[event:%s] Failed to start event clip: %s', cam_id, e)


def _event_monitor_loop(cam, settings, stop_event):
    """Monitor RTSP stream for motion using ffmpeg lavfi scene detection.

    How it works:
    - ffmpeg decodes the stream and applies the 'select' filter with
      scene change detection  (scene > threshold).
    - When scene change is detected, 'showinfo' outputs a line with pts_time.
    - We parse stderr for 'Parsed_showinfo' lines = motion event.
    - On event → spawn a short recording clip.
    """
    cam_id = cam['id']
    sensitivity = settings.get('motion_sensitivity', 50)
    threshold = max(0.01, min(0.99, 1.0 - sensitivity / 100.0))  # 50→0.5, 80→0.2, 20→0.8
    post_sec = settings.get('event_post_seconds', 15)
    cooldown = settings.get('event_cooldown', 10)
    url = cam.get('substream_url') or cam['url']

    log.info('[event:%s] Monitor loop started (threshold=%.2f, post=%ds, cooldown=%ds)',
             cam_id, threshold, post_sec, cooldown)

    while not stop_event.is_set():
        # Launch ffmpeg motion detector
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'info',
            '-rtsp_transport', 'tcp',
            '-i', url,
            '-vf', f'select=gt(scene\\,{threshold}),showinfo',
            '-f', 'null', '-'
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as e:
            log.error('[event:%s] Cannot start motion detector: %s', cam_id, e)
            stop_event.wait(timeout=30)
            continue

        last_event_time = 0
        try:
            while not stop_event.is_set():
                line = proc.stderr.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                decoded = line.decode(errors='replace')
                # Scene change detected → showinfo outputs pkt_dts_time etc.
                if 'Parsed_showinfo' in decoded or 'showinfo' in decoded.lower():
                    now = time.time()
                    if now - last_event_time < cooldown:
                        continue  # too soon
                    last_event_time = now
                    log.info('[event:%s] Motion detected! Recording clip...', cam_id)
                    _start_event_clip(cam, post_sec, settings)
                    # Notify frontend
                    if _socketio:
                        _socketio.emit('surveillance_motion', {
                            'camera_id': cam_id,
                            'camera_name': cam.get('name', ''),
                            'time': datetime.now().isoformat(),
                        })
        except Exception as e:
            log.error('[event:%s] Motion monitor error: %s', cam_id, e)
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

        if not stop_event.is_set():
            log.warning('[event:%s] Motion detector ffmpeg exited, restarting in 5s...', cam_id)
            stop_event.wait(timeout=5)

    log.info('[event:%s] Monitor loop stopped', cam_id)


def _stop_recorder(cam_id):
    # Stop continuous recorder
    with _state_lock:
        info = _recorders.pop(cam_id, None)
    if info and info['proc'].poll() is None:
        try:
            info['proc'].send_signal(signal.SIGTERM)
            info['proc'].wait(timeout=10)
        except Exception:
            try:
                info['proc'].kill()
            except Exception:
                pass
    # Stop event monitor and clips
    _stop_event_monitor(cam_id)


@surveillance_bp.route('/recording/start', methods=['POST'])
def recording_start():
    """Start recording all cameras that have record=True."""
    if not _all_deps_ok():
        return jsonify({'error': 'ffmpeg not installed'}), 503
    settings = _load_settings()
    settings['recording_enabled'] = True
    _save_settings(settings)
    cams = _load_cameras()
    started = 0
    for cam in cams:
        if cam.get('enabled') and cam.get('record'):
            _start_recorder(cam, settings)
            started += 1
    log.info('[recording] Started %d cameras', started)
    return jsonify({'ok': True, 'started': started})


@surveillance_bp.route('/recording/stop', methods=['POST'])
def recording_stop():
    settings = _load_settings()
    settings['recording_enabled'] = False
    _save_settings(settings)
    for cam_id in list(_recorders.keys()):
        _stop_recorder(cam_id)
    for cam_id in list(_event_monitors.keys()):
        _stop_event_monitor(cam_id)
    for cam_id in list(_event_clips.keys()):
        _stop_event_clip(cam_id)
    return jsonify({'ok': True})


@surveillance_bp.route('/cameras/<cam_id>/trigger_event', methods=['POST'])
def trigger_event(cam_id):
    """Manually trigger a recording event for a camera."""
    cams = _load_cameras()
    cam = next((c for c in cams if c['id'] == cam_id), None)
    if not cam:
        return jsonify({'error': 'Camera not found'}), 404
    settings = _load_settings()
    duration = (request.json or {}).get('duration', settings.get('event_post_seconds', 15))
    _start_event_clip(cam, int(duration), settings)
    log.info('[event:%s] Manual event triggered (%ds)', cam_id, duration)
    return jsonify({'ok': True, 'duration': duration})


# ═══════════════════════════════════════════════════════════════
# Recordings browser & playback
# ═══════════════════════════════════════════════════════════════

@surveillance_bp.route('/recordings')
def list_recordings():
    """List recordings grouped by date and camera."""
    cam_id = request.args.get('camera', '')
    date = request.args.get('date', '')
    settings = _load_settings()
    rec_base = settings.get('recordings_path', RECORDINGS_DIR)
    seg_duration = settings.get('segment_minutes', 15) * 60

    if not os.path.isdir(rec_base):
        return jsonify({'dates': [], 'files': []})

    # List available dates
    dates = sorted([d for d in os.listdir(rec_base)
                    if os.path.isdir(os.path.join(rec_base, d)) and re.match(r'\d{4}-\d{2}-\d{2}', d)],
                   reverse=True)

    if not date and dates:
        date = dates[0]

    files = []
    if date:
        date_dir = os.path.join(rec_base, date)
        if os.path.isdir(date_dir):
            cams_in_dir = os.listdir(date_dir) if not cam_id else [cam_id]
            all_cams = {c['id']: c['name'] for c in _load_cameras()}
            for cid in cams_in_dir:
                cam_dir = os.path.join(date_dir, cid)
                if not os.path.isdir(cam_dir):
                    continue
                for fn in sorted(os.listdir(cam_dir)):
                    if fn.endswith('.mp4'):
                        fpath = os.path.join(cam_dir, fn)
                        try:
                            st = os.stat(fpath)
                            files.append({
                                'camera_id': cid,
                                'camera_name': all_cams.get(cid, cid),
                                'filename': fn,
                                'date': date,
                                'size': st.st_size,
                                'duration_guess': seg_duration,
                                'path': f'{date}/{cid}/{fn}',
                            })
                        except OSError:
                            pass

    return jsonify({'dates': dates, 'date': date, 'files': files})


@surveillance_bp.route('/recordings/play/<path:rec_path>')
def play_recording(rec_path):
    """Stream a recorded file."""
    settings = _load_settings()
    rec_base = settings.get('recordings_path', RECORDINGS_DIR)
    fpath = os.path.realpath(os.path.join(rec_base, rec_path))
    if not fpath.startswith(os.path.realpath(rec_base)):
        return jsonify({'error': 'Invalid path'}), 403
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    return send_file(fpath, mimetype='video/mp4')


@surveillance_bp.route('/recordings/delete', methods=['DELETE'])
def delete_recording():
    data = request.json or {}
    paths = data.get('paths', [])
    settings = _load_settings()
    rec_base = os.path.realpath(settings.get('recordings_path', RECORDINGS_DIR))
    deleted = 0
    for p in paths:
        fpath = os.path.realpath(os.path.join(rec_base, p))
        if fpath.startswith(rec_base) and os.path.isfile(fpath):
            try:
                os.remove(fpath)
                deleted += 1
            except OSError:
                pass
    return jsonify({'ok': True, 'deleted': deleted})


# ═══════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════

@surveillance_bp.route('/settings')
def get_settings():
    return jsonify(_load_settings())


@surveillance_bp.route('/settings', methods=['PUT'])
def update_settings():
    data = request.json or {}
    s = _load_settings()
    for k in ('recording_enabled', 'recording_mode', 'segment_minutes', 'retention_days',
              'recordings_path', 'motion_detection', 'motion_sensitivity',
              'event_pre_seconds', 'event_post_seconds', 'event_cooldown',
              'auto_discovery', 'auto_discovery_interval'):
        if k in data:
            s[k] = data[k]
    _save_settings(s)
    # Restart or stop auto-discovery thread if setting changed
    _restart_auto_discovery()
    return jsonify(s)


# ═══════════════════════════════════════════════════════════════
# Camera auto-discovery (ONVIF + network scan)
# ═══════════════════════════════════════════════════════════════

_discovery_results = []
_discovery_running = False

@surveillance_bp.route('/discover', methods=['POST'])
def discover_cameras():
    """Start camera discovery scan in background."""
    global _discovery_running, _discovery_results
    if _discovery_running:
        return jsonify({'ok': True, 'status': 'already_running'})

    subnet = request.json.get('subnet', '') if request.json else ''
    _discovery_results = []
    _discovery_running = True

    def _bg():
        global _discovery_running, _discovery_results
        found = []
        try:
            # Method 1: ONVIF WS-Discovery (broadcast)
            onvif_cameras = _onvif_ws_discovery()
            found.extend(onvif_cameras)

            # Method 2: Network scan for common RTSP ports
            if subnet:
                scan_results = _scan_rtsp_ports(subnet)
                # Deduplicate by IP
                known_ips = {c.get('ip') for c in found}
                for s in scan_results:
                    if s.get('ip') not in known_ips:
                        found.append(s)
                        known_ips.add(s.get('ip'))

            # Method 3: Docker container inspection (ring-mqtt, go2rtc, frigate, etc.)
            docker_cams = _discover_docker_streams()
            known_urls = {c.get('url', '') for c in found}
            for dc in docker_cams:
                if dc.get('url') and dc['url'] not in known_urls:
                    found.append(dc)
                    known_urls.add(dc['url'])

            # Method 4: Native go2rtc (e.g. under Home Assistant)
            native_go2rtc = _discover_native_go2rtc()
            for nc in native_go2rtc:
                if nc.get('url') and nc['url'] not in known_urls:
                    found.append(nc)
                    known_urls.add(nc['url'])

            _discovery_results = found
            if _socketio:
                _socketio.emit('surveillance_discovery', {'status': 'done', 'cameras': found})
        except Exception as e:
            if _socketio:
                _socketio.emit('surveillance_discovery', {'status': 'error', 'message': str(e)})
        finally:
            _discovery_running = False

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return jsonify({'ok': True, 'status': 'started'})


@surveillance_bp.route('/discover')
def discover_results():
    return jsonify({
        'running': _discovery_running,
        'cameras': _discovery_results,
    })


def _onvif_ws_discovery():
    """Use WS-Discovery to find ONVIF cameras on the LAN."""
    found = []
    try:
        from onvif import ONVIFCamera
        # WSDiscovery
        from wsdiscovery import WSDiscovery
        wsd = WSDiscovery()
        wsd.start()
        services = wsd.searchServices(timeout=5)
        for svc in services:
            scopes = svc.getScopes()
            xaddrs = svc.getXAddrs()
            # Check if it's a camera (NVT scope)
            is_camera = any('onvif://www.onvif.org/type/video_encoder' in str(s)
                           or 'onvif://www.onvif.org/type/NetworkVideoTransmitter' in str(s)
                           for s in scopes)
            if is_camera and xaddrs:
                url = xaddrs[0]
                # Parse IP from URL
                import urllib.parse
                parsed = urllib.parse.urlparse(url)
                ip = parsed.hostname or ''
                name_scope = next((str(s).split('/')[-1] for s in scopes
                                   if 'onvif://www.onvif.org/name/' in str(s)), '')
                found.append({
                    'ip': ip,
                    'port': parsed.port or 80,
                    'name': urllib.parse.unquote(name_scope) or f'ONVIF Camera ({ip})',
                    'onvif_url': url,
                    'method': 'onvif',
                    'url': '',  # RTSP URL needs to be probed via ONVIF GetStreamUri
                })
        wsd.stop()
    except ImportError:
        # onvif/wsdiscovery not installed — fall back to port scan only
        pass
    except Exception:
        pass
    return found


def _scan_rtsp_ports(subnet):
    """Scan common RTSP ports on subnet to find cameras."""
    import socket
    from concurrent.futures import ThreadPoolExecutor, as_completed
    found = []
    # Parse subnet like 192.168.50.0/24
    base_match = re.match(r'(\d+\.\d+\.\d+)\.\d+(?:/\d+)?', subnet)
    if not base_match:
        return found
    base = base_match.group(1)

    rtsp_ports = [554, 8554, 8080]

    def _check_host(ip):
        hits = []
        for port in rtsp_ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.0)
                result = s.connect_ex((ip, port))
                s.close()
                if result == 0:
                    hits.append({'ip': ip, 'port': port, 'name': f'Camera ({ip}:{port})',
                                 'url': f'rtsp://{ip}:{port}/', 'method': 'scan'})
            except Exception:
                pass
        return hits

    with ThreadPoolExecutor(max_workers=64) as pool:
        futures = {pool.submit(_check_host, f'{base}.{i}'): i for i in range(1, 255)}
        for fut in as_completed(futures, timeout=30):
            try:
                hits = fut.result()
                if hits:
                    found.extend(hits)
            except Exception:
                pass

    return found


# ─── ONVIF stream URI probe ──────────────────────────────────

@surveillance_bp.route('/onvif/probe', methods=['POST'])
def onvif_probe():
    """Try to get RTSP stream URI from an ONVIF camera."""
    data = request.json or {}
    host = data.get('host', '').strip()
    port = int(data.get('port', 80))
    user = data.get('user', '')
    password = data.get('password', '')

    if not host:
        return jsonify({'error': 'Host required'}), 400

    try:
        from onvif import ONVIFCamera
        cam = ONVIFCamera(host, port, user, password)
        media = cam.create_media_service()
        profiles = media.GetProfiles()
        streams = []
        for p in profiles:
            try:
                uri_req = media.create_type('GetStreamUri')
                uri_req.ProfileToken = p.token
                uri_req.StreamSetup = {'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}}
                uri = media.GetStreamUri(uri_req)
                streams.append({
                    'profile': p.Name,
                    'token': p.token,
                    'url': str(uri.Uri),
                    'resolution': f'{p.VideoEncoderConfiguration.Resolution.Width}x{p.VideoEncoderConfiguration.Resolution.Height}'
                    if hasattr(p, 'VideoEncoderConfiguration') and p.VideoEncoderConfiguration else '',
                })
            except Exception:
                pass
        return jsonify({'ok': True, 'streams': streams, 'profiles': len(profiles)})
    except ImportError:
        return jsonify({'error': 'python-onvif not installed. Enter RTSP URL manually.'}), 503
    except Exception as e:
        return jsonify({'error': f'ONVIF error: {str(e)[:200]}'}), 502


# ═══════════════════════════════════════════════════════════════
# Docker container stream discovery (ring-mqtt, go2rtc, frigate)
# ═══════════════════════════════════════════════════════════════

def _discover_docker_streams():
    """Detect RTSP streams exposed by Docker containers (ring-mqtt/go2rtc, frigate, etc.)."""
    found = []
    try:
        # Get host IP for RTSP URLs
        host_ip = _get_local_ip()
        if not host_ip:
            host_ip = '127.0.0.1'

        # List running containers
        r = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return found
        containers = r.stdout.strip().splitlines()

        for name in containers:
            # ── ring-mqtt (uses go2rtc internally) ──
            if 'ring' in name.lower():
                cams = _discover_go2rtc_in_container(name, host_ip)
                found.extend(cams)

            # ── standalone go2rtc ──
            elif 'go2rtc' in name.lower():
                cams = _discover_go2rtc_in_container(name, host_ip)
                found.extend(cams)

            # ── frigate ──
            elif 'frigate' in name.lower():
                cams = _discover_frigate_streams(name, host_ip)
                found.extend(cams)

    except Exception:
        pass
    return found


def _discover_native_go2rtc():
    """Detect go2rtc running natively (e.g. under Home Assistant) and list its streams."""
    found = []
    try:
        go2rtc_info = _detect_native_go2rtc_port()
        if not go2rtc_info:
            return found

        rtsp_port = go2rtc_info['rtsp_port']
        bind = go2rtc_info.get('bind', '127.0.0.1')
        host_ip = _get_local_ip() or '127.0.0.1'
        connect_ip = '127.0.0.1' if bind in ('127.0.0.1', '0.0.0.0') else bind

        # Try to read go2rtc config from process cmdline
        pid = go2rtc_info['pid']
        config_path = None
        try:
            cmdline = open(f'/proc/{pid}/cmdline').read().split('\0')
            for i, arg in enumerate(cmdline):
                if arg == '-c' and i + 1 < len(cmdline):
                    config_path = cmdline[i + 1]
                    break
        except Exception:
            pass

        streams = {}
        if config_path and os.path.isfile(config_path):
            try:
                with open(config_path) as f:
                    config_text = f.read()
                try:
                    import yaml
                    config = yaml.safe_load(config_text)
                except Exception:
                    config = _simple_yaml_parse(config_text)
                streams = config.get('streams', {})
            except Exception:
                pass

        if isinstance(streams, dict) and streams:
            for stream_name, stream_src in streams.items():
                if '_event' in stream_name:
                    continue
                display_name = stream_name.replace('_live', '').replace('_', ' ')
                rtsp_host = host_ip if bind == '0.0.0.0' else connect_ip
                found.append({
                    'ip': rtsp_host,
                    'port': rtsp_port,
                    'name': f'go2rtc: {display_name}',
                    'url': f'rtsp://{rtsp_host}:{rtsp_port}/{stream_name}',
                    'method': 'native_go2rtc',
                    'source': f'go2rtc (pid {pid})',
                })
        else:
            # No config readable — report go2rtc as available
            found.append({
                'ip': host_ip,
                'port': rtsp_port,
                'name': 'go2rtc (native)',
                'url': f'rtsp://{host_ip}:{rtsp_port}/',
                'method': 'native_go2rtc',
                'source': f'go2rtc (pid {pid})',
                'note': 'go2rtc detected but failed to read stream configuration',
            })
    except Exception:
        pass
    return found


def _get_local_ip():
    """Get local LAN IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''


def _discover_go2rtc_in_container(container_name, host_ip):
    """Parse go2rtc.yaml inside a container to find RTSP streams."""
    found = []
    try:
        # Read go2rtc config
        r = subprocess.run(['docker', 'exec', container_name, 'cat', '/data/go2rtc.yaml'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            # Try alternate path
            r = subprocess.run(['docker', 'exec', container_name, 'cat', '/config/go2rtc.yaml'],
                               capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return found

        # Parse YAML (simple: find rtsp listen port + stream names)
        config_text = r.stdout
        import yaml
        try:
            config = yaml.safe_load(config_text)
        except Exception:
            # Fallback: manual parse
            config = _simple_yaml_parse(config_text)

        rtsp_port = 8554  # default
        rtsp_section = config.get('rtsp', {})
        if isinstance(rtsp_section, dict):
            listen = rtsp_section.get('listen', ':8554')
            if ':' in str(listen):
                try:
                    rtsp_port = int(str(listen).rsplit(':', 1)[1])
                except ValueError:
                    pass

        # Find host-mapped port
        mapped_port = _get_docker_host_port(container_name, rtsp_port)
        if not mapped_port:
            mapped_port = rtsp_port

        streams = config.get('streams', {})
        if isinstance(streams, dict):
            for stream_name, stream_src in streams.items():
                # Skip event streams, only show live
                if '_event' in stream_name:
                    continue
                # Derive a human-readable name
                display_name = stream_name.replace('_live', '').replace('_', ' ')
                # Try to get camera name from container logs (ring-mqtt publishes name)
                friendly_name = _get_ring_camera_name(container_name, stream_name)

                found.append({
                    'ip': host_ip,
                    'port': mapped_port,
                    'name': friendly_name or f'Ring Camera ({display_name})',
                    'url': f'rtsp://{host_ip}:{mapped_port}/{stream_name}',
                    'method': 'docker',
                    'source': container_name,
                })

    except Exception:
        pass
    return found


def _simple_yaml_parse(text):
    """Very basic YAML parser for go2rtc config (handles only top-level keys and simple nesting)."""
    result = {}
    current_key = None
    current_dict = None
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith('#'):
            continue
        if not line.startswith(' ') and not line.startswith('\t') and ':' in line:
            key, _, val = line.partition(':')
            key = key.strip()
            val = val.strip()
            if val:
                result[key] = val
            else:
                current_key = key
                current_dict = {}
                result[key] = current_dict
        elif current_dict is not None and ':' in line:
            key, _, val = line.strip().partition(':')
            current_dict[key.strip()] = val.strip()
        else:
            current_key = None
            current_dict = None
    return result


def _get_docker_host_port(container_name, container_port):
    """Get the host-mapped port for a container port."""
    try:
        r = subprocess.run(['docker', 'port', container_name, str(container_port)],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            # Output like "0.0.0.0:8554"
            for line in r.stdout.strip().splitlines():
                parts = line.strip().rsplit(':', 1)
                if len(parts) == 2:
                    return int(parts[1])
    except Exception:
        pass
    return None


def _get_ring_camera_name(container_name, stream_name):
    """Try to extract friendly camera name from ring-mqtt logs."""
    try:
        cam_id = stream_name.replace('_live', '').replace('_event', '')
        r = subprocess.run(
            ['docker', 'logs', '--tail', '200', container_name],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            # ring-mqtt logs contain lines like: [Front Door] ring/...camera/083a882645ca/...
            import re as _re
            _ansi_re = _re.compile(r'\x1b\[[0-9;]*m')
            pattern = _re.compile(r'\[([^\]]+)\].*camera/' + _re.escape(cam_id))
            for line in r.stdout.splitlines() + r.stderr.splitlines():
                clean = _ansi_re.sub('', line)
                m = pattern.search(clean)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ''


def _discover_frigate_streams(container_name, host_ip):
    """Detect cameras from Frigate config."""
    found = []
    try:
        # Frigate exposes RTSP re-streams on port 8554 by default
        r = subprocess.run(['docker', 'exec', container_name, 'cat', '/config/config.yml'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return found
        try:
            import yaml
            config = yaml.safe_load(r.stdout)
        except Exception:
            return found

        mapped_port = _get_docker_host_port(container_name, 8554) or 8554
        cameras = config.get('cameras', {})
        for cam_name, cam_cfg in cameras.items():
            found.append({
                'ip': host_ip,
                'port': mapped_port,
                'name': f'Frigate: {cam_name}',
                'url': f'rtsp://{host_ip}:{mapped_port}/{cam_name}',
                'method': 'docker',
                'source': container_name,
            })
    except Exception:
        pass
    return found


# ═══════════════════════════════════════════════════════════════
# Automatic periodic camera discovery
# ═══════════════════════════════════════════════════════════════

_auto_discovery_stop = threading.Event()
_auto_discovery_thread = None

def _get_local_subnet():
    """Best-effort: get the local LAN subnet like 192.168.50.0/24."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split('.')
        return '.'.join(parts[:3]) + '.0/24'
    except Exception:
        return ''

def _auto_discovery_loop():
    """Run periodic camera discovery, emit events for NEW cameras found."""
    while not _auto_discovery_stop.is_set():
        settings = _load_settings()
        if not settings.get('auto_discovery'):
            break
        interval = max(int(settings.get('auto_discovery_interval', 300)), 60)

        try:
            known_cams = _load_cameras()
            known_ips = {c.get('ip', '') or _ip_from_url(c.get('url', '')) for c in known_cams}
            known_ips.discard('')

            found = []
            # ONVIF
            onvif_cams = _onvif_ws_discovery()
            found.extend(onvif_cams)

            # Port scan local subnet
            subnet = _get_local_subnet()
            if subnet:
                scan = _scan_rtsp_ports(subnet)
                seen = {c.get('ip') for c in found}
                for s in scan:
                    if s.get('ip') not in seen:
                        found.append(s)
                        seen.add(s.get('ip'))

            # Docker containers (ring-mqtt, go2rtc, frigate)
            docker_cams = _discover_docker_streams()
            known_urls_found = {c.get('url', '') for c in found}
            for dc in docker_cams:
                if dc.get('url') and dc['url'] not in known_urls_found:
                    found.append(dc)
                    known_urls_found.add(dc['url'])

            # Filter only genuinely NEW cameras (by IP or by URL)
            known_urls = {c.get('url', '') for c in known_cams}
            new_cameras = [c for c in found
                           if (c.get('ip') and c['ip'] not in known_ips)
                           or (c.get('url') and c['url'] not in known_urls)]

            if new_cameras and _socketio:
                _socketio.emit('surveillance_new_cameras', {
                    'cameras': new_cameras,
                    'count': len(new_cameras),
                })
        except Exception:
            pass

        # Wait for interval or stop signal
        _auto_discovery_stop.wait(timeout=interval)


def _ip_from_url(url):
    """Extract IP from rtsp://user:pass@IP:port/path."""
    m = re.search(r'://(?:[^@]+@)?([\d.]+)', url or '')
    return m.group(1) if m else ''


def _restart_auto_discovery():
    """Stop existing auto-discovery thread and start a new one if enabled."""
    global _auto_discovery_thread
    _auto_discovery_stop.set()
    if _auto_discovery_thread and _auto_discovery_thread.is_alive():
        _auto_discovery_thread.join(timeout=5)
    _auto_discovery_stop.clear()

    settings = _load_settings()
    if settings.get('auto_discovery'):
        _auto_discovery_thread = threading.Thread(target=_auto_discovery_loop, daemon=True)
        _auto_discovery_thread.start()


# ═══════════════════════════════════════════════════════════════
# Retention cleanup (called from scheduler)
# ═══════════════════════════════════════════════════════════════

def cleanup_old_recordings():
    """Delete recordings older than retention_days. Call from scheduler."""
    settings = _load_settings()
    days = settings.get('retention_days', 30)
    rec_base = settings.get('recordings_path', RECORDINGS_DIR)
    if not os.path.isdir(rec_base):
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    removed = 0
    for d in os.listdir(rec_base):
        if re.match(r'\d{4}-\d{2}-\d{2}', d) and d < cutoff:
            dpath = os.path.join(rec_base, d)
            try:
                shutil.rmtree(dpath)
                removed += 1
            except OSError:
                pass
    return removed


# ═══════════════════════════════════════════════════════════════
# Startup: resume recording if it was enabled
# ═══════════════════════════════════════════════════════════════

def _auto_resume():
    """Resume streams + recording after restart for enabled cameras."""
    if not _all_deps_ok():
        log.warning('[startup] ffmpeg not available, skipping auto-resume')
        return
    cams = _load_cameras()
    settings = _load_settings()
    started_streams = 0
    started_recs = 0
    for cam in cams:
        if not cam.get('enabled'):
            continue
        # Always auto-start live stream for enabled cameras
        err = _start_stream(cam)
        if err:
            log.warning('[startup] Cannot start stream for %s: %s', cam.get('name'), err)
        else:
            started_streams += 1
        # Start recorder if recording was enabled
        if settings.get('recording_enabled') and cam.get('record'):
            _start_recorder(cam, settings)
            started_recs += 1
    log.info('[startup] Auto-resume done: %d streams, %d recorders started (%d cameras total)',
             started_streams, started_recs, len(cams))


# ═══════════════════════════════════════════════════════════════
# Health monitor — watchdog that restarts dead ffmpeg processes
# ═══════════════════════════════════════════════════════════════

_health_stop = threading.Event()
HEALTH_CHECK_INTERVAL = 30   # seconds
MAX_RETRIES = 10              # stop retrying after this many consecutive failures
RETRY_BACKOFF_BASE = 10       # seconds; actual = base * retries (max 300s)

def _health_monitor_loop():
    """Periodically check ffmpeg processes, restart dead ones."""
    log.info('[health] Stream health monitor started (interval=%ds)', HEALTH_CHECK_INTERVAL)
    while not _health_stop.is_set():
        _health_stop.wait(timeout=HEALTH_CHECK_INTERVAL)
        if _health_stop.is_set():
            break
        try:
            _cleanup_dead_procs()
            cams = _load_cameras()
            settings = _load_settings()
            for cam in cams:
                cid = cam['id']
                if not cam.get('enabled'):
                    continue

                # Check stream
                with _state_lock:
                    has_stream = cid in _streams
                    err_info = _cam_errors.get(cid, {})
                    retries = err_info.get('retries', 0)
                    last_err_time = err_info.get('last_error_time', '')
                if not has_stream:
                    if retries >= MAX_RETRIES:
                        continue  # give up on this camera
                    # Backoff: wait longer after repeated failures
                    backoff = min(RETRY_BACKOFF_BASE * (retries + 1), 300)
                    if last_err_time:
                        try:
                            elapsed = (datetime.now() - datetime.fromisoformat(last_err_time)).total_seconds()
                            if elapsed < backoff:
                                continue  # too soon to retry
                        except Exception:
                            pass

                    log.info('[health] Restarting stream for %s (retry #%d)', cam.get('name'), retries + 1)
                    err = _start_stream(cam)
                    if err:
                        log.warning('[health] Stream restart failed for %s: %s', cam.get('name'), err[:150])

                # Check recorder (per-camera mode: continuous or events)
                if settings.get('recording_enabled') and cam.get('record'):
                    cam_mode = cam.get('recording_mode', settings.get('recording_mode', 'continuous'))
                    with _state_lock:
                        has_recorder = cid in _recorders
                        has_event_mon = cid in _event_monitors
                    if cam_mode == 'continuous' and not has_recorder:
                        log.info('[health] Restarting continuous recorder for %s', cam.get('name'))
                        _start_continuous_recorder(cam, settings)
                    elif cam_mode == 'events' and not has_event_mon:
                        log.info('[health] Restarting event monitor for %s', cam.get('name'))
                        _start_event_monitor(cam, settings)
        except Exception as e:
            log.error('[health] Health check error: %s', e)


# Run auto-resume + auto-discovery + health monitor in background after a short delay
def _delayed_startup():
    time.sleep(10)
    # Ensure settings file exists with defaults
    if not os.path.isfile(SETTINGS_FILE):
        _save_settings(_load_settings())
        log.info('[startup] Created default settings file: %s', SETTINGS_FILE)
    _auto_resume()
    _restart_auto_discovery()
    # Start health monitor
    hm = threading.Thread(target=_health_monitor_loop, daemon=True)
    hm.start()

_startup_thread = threading.Thread(target=_delayed_startup, daemon=True)
_startup_thread.start()
