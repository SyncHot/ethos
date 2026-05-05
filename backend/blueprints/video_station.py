"""
Video Station -- video library manager with streaming.

Routes:
  GET  /api/video-station/pkg-status      - dependency & library status
  POST /api/video-station/install         - install ffmpeg
  POST /api/video-station/uninstall       - cleanup
  GET  /api/video-station/library         - list videos (?watched=0|1 filter)
  GET  /api/video-station/continue-watching - in-progress videos (position>0, not watched)
  GET  /api/video-station/folders         - configured library folders
  POST /api/video-station/folders         - save library folders
  POST /api/video-station/scan            - start background library scan
  GET  /api/video-station/scan-status     - scan progress
  POST /api/video-station/scan-stop       - stop running scan
  GET  /api/video-station/info/<int:vid>  - detailed video metadata (with TMDb credits)
  GET  /api/video-station/stream/<int:vid>- stream video file (raw)
  GET  /api/video-station/transcode/<int:vid> - transcode video (?audio=N, ?start=S)
  POST /api/video-station/hls/<int:vid>/start - start HLS transcoding session
  GET  /api/video-station/hls/<sid>/playlist.m3u8 - HLS playlist
  GET  /api/video-station/hls/<sid>/<segment>  - HLS segment (.ts)
  POST /api/video-station/hls/<sid>/stop       - stop HLS session
  GET  /api/video-station/thumb/<int:vid> - video thumbnail
  GET  /api/video-station/poster/<int:vid>- TMDb poster image
  GET  /api/video-station/backdrop/<int:vid> - TMDb backdrop image
  GET  /api/video-station/thumbstrip/<int:vid> - seekbar thumbnail sprite (VTT+image)
  GET  /api/video-station/recent          - recently added videos
  GET  /api/video-station/collections     - auto-generated collections
  POST /api/video-station/watched/<int:vid> - mark as watched / update position
  POST /api/video-station/rescan-metadata - re-probe videos with empty codec info
  POST /api/video-station/remove/<int:vid> - remove video from library (keeps file)
  POST /api/video-station/delete/<int:vid> - delete video from library AND from disk
  GET  /api/video-station/parse-title/<int:vid> - parse filename into title+year for TMDb
  POST /api/video-station/batch           - batch operations (watched/unwatched/remove/hide/unhide)
  GET  /api/video-station/hide-status     - hide password status & session unlock state
  POST /api/video-station/hide-password   - set/change hide password
  DELETE /api/video-station/hide-password - remove hide password & unhide all
  POST /api/video-station/hide-unlock     - unlock hidden videos for session
  POST /api/video-station/hide-lock       - re-lock hidden videos for session
  GET  /api/video-station/subtitles/<int:vid> - find subtitle files next to video
  GET  /api/video-station/subtitle-file/<int:vid>/<filename> - serve subtitle (srt→vtt)
  GET  /api/video-station/tmdb-config     - get TMDb API key status
  POST /api/video-station/tmdb-config     - save TMDb API key
  POST /api/video-station/tmdb-match/<int:vid> - manually trigger TMDb match for a video
  POST /api/video-station/tmdb-match-all  - match all unmatched NON-HIDDEN videos
  GET  /api/video-station/tmdb-search-list - search TMDb, return list (?q=query)
  POST /api/video-station/tmdb-apply/<int:vid> - apply a specific TMDb result (by tmdb_id)
  POST /api/video-station/rename/<int:vid> - rename video file on disk and update DB
  GET  /api/video-station/hw-health       - detailed HW acceleration health report
  GET  /api/video-station/vainfo-test     - run vainfo against /dev/dri/renderD128, return profiles
  POST /api/video-station/gpu-retest      - reset encoder cache, re-probe all VAAPI codecs (H264/HEVC/VP9/AV1)

SocketIO events emitted:
  vs_scan_progress  - {running, total, processed, current_file}
  vs_scan_done      - {total_processed, duration}
"""

import grp
import json
import logging
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import urllib.error

from flask import Blueprint, jsonify, request, Response, send_file

from host import host_run, host_run_stream, q, data_path, app_path
from crypto_utils import hash_folder_password as _hash_pw, verify_folder_password as _verify_pw

from blueprints.admin_required import admin_required

log = logging.getLogger('ethos.video_station')

video_station_bp = Blueprint('video-station', __name__, url_prefix='/api/video-station')

_DB_PATH = data_path('video_station.db')
_THUMB_DIR = data_path('video_thumbs')
_POSTER_DIR = data_path('video_posters')
_BACKDROP_DIR = data_path('video_backdrops')
_THUMBSTRIP_DIR = data_path('video_thumbstrips')
_TMDB_CONF = data_path('video_tmdb.json')
_HIDE_PW_FILE = data_path('vs_hide_password.json')

# ── hide-password session state ────────────────────────────────
_hide_unlocked = {}          # token → True
_hide_lock = threading.Lock()
_hide_attempts = {}          # token → {'count': int, 'first': float, 'locked_until': float}
_HIDE_MAX_ATTEMPTS = 5
_HIDE_ATTEMPT_WINDOW = 120   # seconds
_HIDE_LOCKOUT_TIME = 300     # seconds

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
              '.mpg', '.mpeg', '.ts', '.3gp', '.ogv', '.vob'}

# Video codecs that browsers can natively decode inside <video>
_BROWSER_VIDEO_CODECS = {'h264', 'avc1', 'vp8', 'vp9', 'theora', 'av1'}

# Audio codecs that browsers can natively decode inside <video>
_BROWSER_AUDIO_CODECS = {'aac', 'mp3', 'opus', 'vorbis', 'flac'}

# Containers that browsers can play natively in <video>
_BROWSER_CONTAINERS = {'.mp4', '.webm', '.m4v', '.ogg', '.ogv', '.mov'}

_scan_state = {
    'running': False, 'stop_requested': False,
    'total': 0, 'processed': 0, 'current_file': '',
}

# ── HLS transcoding sessions ──────────────────────────────────
_hls_sessions = {}   # session_id → {proc, tmpdir, vid, created, last_heartbeat, client_pos, paused}
_HLS_MAX_AGE = 2 * 3600   # auto-cleanup after 2 hours
_HLS_ORPHAN_PREFIX = "vs_hls_"
_HLS_HEARTBEAT_TIMEOUT = 20   # seconds — kill session if no heartbeat
_HLS_THROTTLE_AHEAD = 90      # seconds — pause ffmpeg when this far ahead of playback
_HLS_THROTTLE_RESUME = 30     # seconds — resume ffmpeg when buffer drops below this
_HLS_MAX_SESSIONS = 3         # kill oldest session when this many are active

# File-watcher state: folder_path → last-seen mtime
_watcher_state = {}           # populated by _start_hls_cleanup_loop

# VAAPI render node (Intel/AMD iGPU)
RENDER_NODE = '/dev/dri/renderD128'

# Hardware encoder detection (run once at import)
_HW_ENCODER = None  # 'h264_nvenc' | 'h264_vaapi' | 'h264_videotoolbox' | 'libx264'

# VAAPI decodable codecs (parsed from vainfo VAEntrypointVLD profiles)
_VAAPI_DECODE_CODECS = None  # set of codec names: {'h264', 'hevc', 'vp9', 'av1'}


def _detect_hw_encoder():
    """Probe available HW H.264 encoders. Returns best available encoder name."""
    global _HW_ENCODER
    if _HW_ENCODER is not None:
        return _HW_ENCODER
    if not shutil.which('ffmpeg'):
        _HW_ENCODER = 'libx264'
        return _HW_ENCODER
    candidates = [
        ('h264_nvenc',
         ['-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.1', '-c:v', 'h264_nvenc', '-f', 'null', '-']),
        ('h264_vaapi',
         ['-vaapi_device', RENDER_NODE, '-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.1',
          '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-f', 'null', '-']),
        ('h264_videotoolbox',
         ['-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.1', '-c:v', 'h264_videotoolbox', '-f', 'null', '-']),
    ]
    for name, args in candidates:
        try:
            r = subprocess.run(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error'] + args,
                capture_output=True, timeout=5)
            if r.returncode == 0:
                _HW_ENCODER = name
                log.info("HLS HW encoder: %s", name)
                return _HW_ENCODER
        except Exception:
            pass
    _HW_ENCODER = 'libx264'
    log.info("HLS HW encoder: libx264 (no HW accel)")
    return _HW_ENCODER


def _detect_vaapi_decode_codecs():
    """Return set of codec names decodable via VAAPI (from vainfo VAEntrypointVLD profiles).

    Uses vainfo to parse supported decode profiles. VAEntrypointVLD = hardware decode.
    Result is cached at module level to avoid repeated subprocess calls.
    Returns a set of strings from {'h264', 'hevc', 'vp9', 'av1'}.
    """
    global _VAAPI_DECODE_CODECS
    if _VAAPI_DECODE_CODECS is not None:
        return _VAAPI_DECODE_CODECS
    _VAAPI_DECODE_CODECS = set()
    if not shutil.which('vainfo') or not os.path.exists(RENDER_NODE):
        return _VAAPI_DECODE_CODECS
    try:
        r = subprocess.run(
            ['vainfo', '--display', 'drm', '--device', RENDER_NODE],
            capture_output=True, timeout=8, text=True)
        output = (r.stdout or '') + (r.stderr or '')
        if r.returncode != 0 or 'VAEntrypoint' not in output:
            return _VAAPI_DECODE_CODECS
        profile_map = {
            'VAProfileH264':  'h264',
            'VAProfileHEVC':  'hevc',
            'VAProfileVP9':   'vp9',
            'VAProfileAV1':   'av1',
        }
        for line in output.splitlines():
            if 'VAEntrypointVLD' in line:
                for prefix, codec in profile_map.items():
                    if prefix in line:
                        _VAAPI_DECODE_CODECS.add(codec)
        log.info("VAAPI decodable codecs: %s", _VAAPI_DECODE_CODECS)
    except Exception:
        pass
    return _VAAPI_DECODE_CODECS


def _detect_docker():
    """Return True if the current process is running inside a Docker container."""
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup') as f:
            content = f.read()
        if 'docker' in content or 'kubepods' in content or 'containerd' in content:
            return True
    except Exception:
        pass
    return False


def _check_iHD_driver():
    """Return (present: bool, path: str) for iHD_drv_video.so (Intel Media Driver)."""
    search_paths = [
        '/usr/lib/x86_64-linux-gnu/dri/iHD_drv_video.so',
        '/usr/lib/dri/iHD_drv_video.so',
        '/usr/lib64/dri/iHD_drv_video.so',
        '/usr/local/lib/dri/iHD_drv_video.so',
    ]
    for p in search_paths:
        if os.path.exists(p):
            return True, p
    return False, ''


def _render_node_group_info(render_node):
    """Return (in_group: bool, group_name: str, process_user: str) for render node access.

    Checks actual supplemental group membership of the running process, which
    is more accurate than os.access() for setuid scenarios.
    """
    try:
        st = os.stat(render_node)
        gid = st.st_gid
        try:
            grp_name = grp.getgrgid(gid).gr_name
        except KeyError:
            grp_name = str(gid)

        # Also get the video group gid for secondary check
        try:
            video_gid = grp.getgrnam('video').gr_gid
        except KeyError:
            video_gid = None

        proc_gids = os.getgroups()
        proc_egid = os.getegid()
        in_group = (gid in proc_gids or gid == proc_egid
                    or (video_gid is not None and video_gid in proc_gids))

        try:
            process_user = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            process_user = str(os.geteuid())

        return in_group, grp_name, process_user
    except Exception:
        # Fallback: simple access check
        return os.access(render_node, os.R_OK), 'render', ''


def _compute_vaapi_scale(w, h, max_w=4096, max_h=4096):
    """Return (scale_w, scale_h) to fit (w,h) within VAAPI encoder limits.

    Returns (0, 0) if no scaling is needed or dimensions are unknown.
    Output dimensions are divisible by 2 (required by h264_vaapi).
    """
    if not w or not h or (w <= max_w and h <= max_h):
        return 0, 0
    ratio = min(max_w / w, max_h / h)
    sw = int(w * ratio) & ~1
    sh = int(h * ratio) & ~1
    return max(sw, 2), max(sh, 2)


def _get_cpu_usage_pct():
    """Return current system-wide CPU usage percentage (0–100) via /proc/stat.

    Reads two snapshots 200 ms apart for an accurate instantaneous reading.
    Returns 0.0 on any error.
    """
    def _read_stat():
        try:
            with open('/proc/stat') as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:8]))
            idle = vals[3] + vals[4]
            total = sum(vals)
            return idle, total
        except Exception:
            return 0, 1

    i1, t1 = _read_stat()
    time.sleep(0.2)
    i2, t2 = _read_stat()
    dt = t2 - t1
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1.0 - (i2 - i1) / dt)))


def _adaptive_qp():
    """Return a QP value for VAAPI/NVENC encoding based on current CPU load.

    Lower QP → better quality (but more CPU/GPU work).
    Intel N150 can comfortably run at qp=22 when idle; backs off to qp=28
    under load so the encode pipeline doesn't starve other processes.

      CPU load   QP   notes
      < 35 %     22   high quality, plenty of headroom
      35–60 %    24   balanced
      60–80 %    26   light quality reduction
      > 80 %     28   fastest possible — avoid frame drops
    """
    try:
        load = _get_cpu_usage_pct()
    except Exception:
        return 23  # safe default
    if load < 35:
        return 22
    if load < 60:
        return 24
    if load < 80:
        return 26
    return 28


def _adaptive_crf():
    """Return a CRF value for libx264/libx265 encoding based on current CPU load."""
    try:
        load = _get_cpu_usage_pct()
    except Exception:
        return 23
    if load < 35:
        return 21
    if load < 60:
        return 23
    if load < 80:
        return 26
    return 28


def _get_intel_media_driver_version():
    """Return installed version of intel-media-va-driver-non-free, or '' if not installed."""
    try:
        r = subprocess.run(
            ['dpkg-query', '-W', '-f=${Version}', 'intel-media-va-driver-non-free'],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            # e.g. "24.1.0+ds1-1" → major int 24
            return r.stdout.strip().split('+')[0].split('~')[0]
    except Exception:
        pass
    return ''


def _intel_driver_needs_ppa_upgrade(version_str):
    """Return True if installed Intel media driver is too old for newer Intel GPUs (< 25.x)."""
    if not version_str:
        return False
    try:
        major = int(version_str.split('.')[0])
        return major < 25
    except (ValueError, IndexError):
        return False


def _hw_health_check():
    """Return detailed HW acceleration health report for Intel/NVIDIA/AMD GPUs.

    Returns a dict with:
      status        : "ok" | "no_render_node" | "missing_driver" | "permission_denied" | "cpu_only"
      hw_encoder    : detected encoder name
      is_hw         : bool — True when real HW acceleration is active
      render_node   : bool — /dev/dri/renderD128 exists
      driver_ok     : bool — VAAPI driver responds
      in_render_grp : bool — current process can access the render node
      render_grp    : str  — OS group owning the render node (e.g. "render")
      process_user  : str  — username of the running process
      iHD_present   : bool — iHD_drv_video.so (Intel Media Driver) found on disk
      iHD_path      : str  — full path to iHD_drv_video.so or ''
      in_docker     : bool — process is running inside a Docker container
      cpu_model     : str  — from /proc/cpuinfo
      setup_steps   : list of {title, commands: [str]} — install instructions
      message       : human-readable diagnosis
    """
    RENDER_NODE = '/dev/dri/renderD128'
    encoder = _detect_hw_encoder()
    is_hw = encoder != 'libx264'
    in_docker = _detect_docker()
    iHD_present, iHD_path = _check_iHD_driver()

    # CPU model
    cpu_model = ''
    try:
        for line in open('/proc/cpuinfo').readlines():
            if 'model name' in line:
                cpu_model = line.split(':', 1)[1].strip()
                break
    except Exception:
        pass

    # Is it Intel (N-series Alder Lake-N etc.)?
    _cm = cpu_model.lower()
    is_intel = ('intel' in _cm or 'n100' in _cm or 'n95' in _cm
                or 'n150' in _cm or 'n200' in _cm or 'n305' in _cm)
    is_amd   = 'amd' in cpu_model.lower()
    is_nvidia = encoder == 'h264_nvenc'

    if is_hw:
        return {
            'status': 'ok',
            'hw_encoder': encoder,
            'is_hw': True,
            'render_node': os.path.exists(RENDER_NODE),
            'driver_ok': True,
            'in_render_grp': True,
            'render_grp': '',
            'process_user': '',
            'iHD_present': iHD_present,
            'iHD_path': iHD_path,
            'in_docker': in_docker,
            'cpu_model': cpu_model,
            'setup_steps': [],
            'message': 'Akceleracja sprzętowa aktywna (%s).' % encoder,
        }

    # Not using HW — diagnose why
    render_node_exists = os.path.exists(RENDER_NODE)
    in_render_grp, render_grp, process_user = False, 'render', ''
    if render_node_exists:
        in_render_grp, render_grp, process_user = _render_node_group_info(RENDER_NODE)

    driver_ok = False
    if render_node_exists and in_render_grp:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-vaapi_device', RENDER_NODE,
             '-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.1',
             '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-f', 'null', '-'],
            capture_output=True, timeout=8
        )
        driver_ok = r.returncode == 0

    # Build setup steps for Intel N-series (N100/N95 = Alder Lake / Twin Lake / GMKtec G3 Plus)
    pkg_main  = 'intel-media-va-driver-non-free'    # Xe iGPU (N100/N95/N200)
    pkg_legacy = 'i965-va-driver'                   # older Intel (Haswell–Ice Lake)
    run_user = process_user or os.environ.get('USER', '') or os.environ.get('SUDO_USER', '') or 'ethos'

    # LIBVA_DRIVER_NAME env var check — important for iHD init
    libva_driver_name = os.environ.get('LIBVA_DRIVER_NAME', '')
    libva_correct = libva_driver_name.lower() == 'ihd'

    # Docker-specific step (shown when any permission/driver issue + running in container)
    docker_step = {
        'title': '🐳 Docker: przekaż urządzenie GPU do kontenera',
        'commands': [
            'docker run --device /dev/dri:/dev/dri ...',
            '# lub w docker-compose.yml:',
            'devices:\n  - /dev/dri:/dev/dri',
        ],
    } if in_docker else None

    if not render_node_exists:
        status = 'no_render_node'
        message = ('Brak węzła renderowania GPU (/dev/dri/renderD128). '
                   'Sprawdź, czy GPU jest obsługiwane przez jądro systemu.')
        steps = [
            {'title': '1. Sprawdź dostępne urządzenia DRI',
             'commands': ['ls -la /dev/dri/', 'lspci | grep -i vga']},
            {'title': '2. Zainstaluj sterownik Intel (N100/N95 — GMKtec G3 Plus)',
             'commands': [
                 'sudo apt update',
                 'sudo apt install -y %s %s vainfo intel-gpu-tools' % (pkg_main, pkg_legacy),
             ]},
            {'title': '3. Przeładuj moduł i915',
             'commands': ['sudo modprobe i915', 'ls /dev/dri/']},
            {'title': '4. Uruchom ponownie serwer EthOS',
             'commands': ['sudo systemctl restart ethos']},
        ]
        if docker_step:
            steps.append(docker_step)
    elif not in_render_grp:
        status = 'permission_denied'
        message = ('Błąd akceleracji: Brak uprawnień do procesora graficznego Intel N100. '
                   'Użytkownik "%s" musi być w grupie "%s" i "video".' % (run_user, render_grp))
        steps = [
            {'title': '1. Nadaj uprawnienia QuickSync — dodaj użytkownika do grup %s i video' % render_grp,
             'commands': [
                 'sudo usermod -aG video,%s %s' % (render_grp, run_user),
                 'groups %s  # weryfikacja — na liście powinno być: video %s' % (run_user, render_grp),
             ]},
            {'title': '2. Uruchom ponownie usługę lub zaloguj się ponownie',
             'commands': [
                 'sudo systemctl restart ethos',
                 '# Jeśli uruchamiasz lokalnie: wyloguj się i zaloguj ponownie',
             ]},
            {'title': '3. Weryfikacja dostępu do GPU (GMKtec G3 Plus — Intel N100)',
             'commands': [
                 'ls -la /dev/dri/renderD128',
                 'vainfo --display drm --device /dev/dri/renderD128',
             ]},
        ]
        if docker_step:
            steps.insert(0, docker_step)
    elif not driver_ok:
        # Run vainfo to get precise iHD failure info
        ihd_init_failed = False
        if iHD_present and shutil.which('vainfo'):
            try:
                vr = subprocess.run(
                    ['vainfo', '--display', 'drm', '--device', RENDER_NODE],
                    capture_output=True, timeout=8, text=True
                )
                vc = (vr.stdout or '') + (vr.stderr or '')
                if ('iHD_drv_video.so init failed' in vc
                        or 'Failed to open the given device' in vc
                        or 'init failed' in vc):
                    ihd_init_failed = True
            except Exception:
                pass

        if ihd_init_failed:
            installed_ver = _get_intel_media_driver_version()
            needs_ppa = _intel_driver_needs_ppa_upgrade(installed_ver)
            if needs_ppa:
                status = 'driver_too_old'
                message = (
                    'Sterownik Intel iHD nie inicjalizuje się — zainstalowana wersja %s jest za stara '
                    'dla tego GPU. Wymagana aktualizacja z Intel Graphics PPA (≥ 25.x).' % installed_ver
                )
                steps = [
                    {'title': '1. Zaktualizuj sterownik Intel z Intel Graphics PPA',
                     'commands': [
                         '# Automatycznie przez Video Station — kliknij "Zainstaluj sterowniki"',
                         '# lub ręcznie:',
                         'curl -sL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x0C0E6AF955CE463C03FC51574D098D70AFBE5E1F" | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/kobuk-intel-graphics.gpg',
                         'echo "deb https://ppa.launchpadcontent.net/kobuk-team/intel-graphics/ubuntu noble main" | sudo tee /etc/apt/sources.list.d/intel-graphics.list',
                         'sudo apt update',
                         'sudo apt install -y intel-media-va-driver-non-free libigdgmm12 libva2 libva-drm2 libvpl2',
                     ]},
                    {'title': '2. Zweryfikuj instalację',
                     'commands': [
                         'vainfo --display drm --device /dev/dri/renderD128',
                     ]},
                    {'title': '3. Uruchom ponownie EthOS i odśwież enkoder',
                     'commands': ['sudo systemctl restart ethos']},
                ]
            else:
                status = 'ihd_init_failed'
                message = ('Wykryto procesor Intel N100, ale sterownik iHD nie może wystartować. '
                           'iHD_drv_video.so init failed — brakujące zależności lub LIBVA_DRIVER_NAME.')
                steps = [
                    {'title': '1. Zainstaluj brakujące zależności (Intel N100 — GMKtec G3 Plus)',
                     'commands': [
                         'sudo apt install -y intel-media-va-driver-non-free libmfx1 libmfx-gen1 libva-drm2',
                     ]},
                    {'title': '2. Wymuś sterownik iHD (LIBVA_DRIVER_NAME=ihd)',
                     'commands': [
                         'export LIBVA_DRIVER_NAME=ihd  # tymczasowo w bieżącej sesji',
                         '# Trwale — dodaj do /etc/environment:',
                         'echo "LIBVA_DRIVER_NAME=ihd" | sudo tee -a /etc/environment',
                         '# Dla usługi EthOS — edytuj /etc/default/ethos (lub /etc/systemd/system/ethos.service):',
                         'sudo sed -i \'/^\\[Service\\]/a Environment=LIBVA_DRIVER_NAME=ihd\' /etc/systemd/system/ethos.service',
                         'sudo systemctl daemon-reload',
                     ]},
                    {'title': '3. Dodaj użytkownika do grup render i video',
                     'commands': [
                         'sudo usermod -aG render,video %s' % run_user,
                         'sudo systemctl restart ethos',
                     ]},
                ]
        elif not iHD_present:
            status = 'missing_driver'
            message = ('Węzeł GPU dostępny, ale brak sterownika Intel Media Driver (iHD_drv_video.so). '
                       'Wymagany pakiet: intel-media-va-driver-non-free (Intel N100/N95).')
            steps = [
                {'title': '1. Zainstaluj sterownik VAAPI dla Intel N100/N95 (GMKtec G3 Plus)',
                 'commands': [
                     'sudo apt update',
                     'sudo apt install -y %s %s vainfo' % (pkg_main, pkg_legacy),
                 ]},
                {'title': '2. Włącz repozytorium non-free (jeśli potrzebne)',
                 'commands': [
                     "sudo sed -i 's/main$/main contrib non-free non-free-firmware/g' /etc/apt/sources.list",
                     'sudo apt update',
                     'sudo apt install -y %s' % pkg_main,
                 ]},
                {'title': '3. Sprawdź działanie VAAPI',
                 'commands': [
                     'vainfo --display drm --device /dev/dri/renderD128',
                     'ffmpeg -hide_banner -vaapi_device /dev/dri/renderD128 -f lavfi -i nullsrc=s=64x64:d=0.1 -vf format=nv12,hwupload -c:v h264_vaapi -f null -',
                 ]},
                {'title': '4. Uruchom ponownie EthOS i odśwież enkoder',
                 'commands': ['sudo systemctl restart ethos']},
            ]
        else:
            status = 'missing_driver'
            message = ('Węzeł GPU istnieje i masz do niego dostęp, ale sterownik VAAPI nie odpowiada. '
                       'Zainstaluj intel-media-va-driver-non-free (Intel N100/N95).')
            steps = [
                {'title': '1. Zainstaluj sterownik VAAPI dla Intel N100/N95 (GMKtec G3 Plus)',
                 'commands': [
                     'sudo apt update',
                     'sudo apt install -y %s %s vainfo' % (pkg_main, pkg_legacy),
                 ]},
                {'title': '2. Włącz repozytorium non-free (jeśli potrzebne)',
                 'commands': [
                     "sudo sed -i 's/main$/main contrib non-free non-free-firmware/g' /etc/apt/sources.list",
                     'sudo apt update',
                     'sudo apt install -y %s' % pkg_main,
                 ]},
                {'title': '3. Sprawdź działanie VAAPI',
                 'commands': [
                     'vainfo --display drm --device /dev/dri/renderD128',
                     'ffmpeg -hide_banner -vaapi_device /dev/dri/renderD128 -f lavfi -i nullsrc=s=64x64:d=0.1 -vf format=nv12,hwupload -c:v h264_vaapi -f null -',
                 ]},
                {'title': '4. Uruchom ponownie EthOS i odśwież enkoder',
                 'commands': ['sudo systemctl restart ethos']},
            ]
        if docker_step:
            steps.append(docker_step)
    else:
        # render node ok, driver ok, but encoder detection still returned libx264
        status = 'cpu_only'
        message = ('Sprzęt nie obsługuje akceleracji H.264 przez VAAPI lub NVENC. '
                   'Używany jest enkoder programowy libx264 (CPU).')
        steps = []

    return {
        'status': status,
        'hw_encoder': encoder,
        'is_hw': False,
        'render_node': render_node_exists,
        'driver_ok': driver_ok,
        'in_render_grp': in_render_grp,
        'render_grp': render_grp,
        'process_user': run_user,
        'iHD_present': iHD_present,
        'iHD_path': iHD_path,
        'in_docker': in_docker,
        'libva_driver_name': libva_driver_name,
        'libva_correct': libva_correct,
        'cpu_model': cpu_model,
        'is_intel': is_intel,
        'is_amd': is_amd,
        'is_nvidia': is_nvidia,
        'driver_version': _get_intel_media_driver_version(),
        'setup_steps': steps,
        'message': message,
    }


def _cleanup_hls(session_id):
    """Stop ffmpeg and remove temp dir for an HLS session."""
    sess = _hls_sessions.pop(session_id, None)
    if not sess:
        return
    proc = sess.get('proc')
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except Exception:
            pass
    tmpdir = sess.get('tmpdir')
    if tmpdir and os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)


def _cleanup_stale_hls():
    """Remove HLS sessions older than _HLS_MAX_AGE and orphaned temp dirs."""
    now = time.time()
    for sid in list(_hls_sessions):
        if now - _hls_sessions[sid].get('created', 0) > _HLS_MAX_AGE:
            _cleanup_hls(sid)
    # Scan /tmp for orphaned vs_hls_* dirs not tracked in _hls_sessions
    _cleanup_orphaned_hls_dirs()


def _cleanup_orphaned_hls_dirs():
    """Remove vs_hls_* temp dirs in /tmp that aren't tracked by any session."""
    tracked_dirs = {s.get('tmpdir') for s in _hls_sessions.values()}
    try:
        for name in os.listdir(tempfile.gettempdir()):
            if not name.startswith(_HLS_ORPHAN_PREFIX):
                continue
            dirpath = os.path.join(tempfile.gettempdir(), name)
            if not os.path.isdir(dirpath):
                continue
            if dirpath in tracked_dirs:
                continue
            log.info("HLS cleanup: removing orphaned dir %s", dirpath)
            shutil.rmtree(dirpath, ignore_errors=True)
    except OSError:
        pass


# Clean up orphaned HLS dirs left from before server restart
_cleanup_orphaned_hls_dirs()


def _start_hls_cleanup_loop(socketio_instance=None):
    """Start background greenlets: stale session cleanup + heartbeat watchdog + throttle."""
    import gevent

    def _cleanup_loop():
        while True:
            gevent.sleep(30 * 60)
            try:
                _cleanup_stale_hls()
            except Exception:
                pass
            try:
                _evict_tmp_if_low()
            except Exception:
                pass

    def _heartbeat_watchdog():
        """Kill sessions whose client hasn't sent a heartbeat for _HLS_HEARTBEAT_TIMEOUT s.
        Also proactively cleanup sessions where ffmpeg has already crashed."""
        while True:
            gevent.sleep(5)
            now = time.time()
            for sid in list(_hls_sessions):
                sess = _hls_sessions.get(sid)
                if not sess:
                    continue
                proc = sess.get('proc')
                # Proactively cleanup crashed ffmpeg (OOM, signal, etc.)
                if proc and proc.poll() is not None:
                    log.warning("HLS ffmpeg crashed sid=%s (exit %s) — cleaning up",
                                sid, proc.returncode)
                    _cleanup_hls(sid)
                    continue
                last = sess.get('last_heartbeat', sess.get('created', now))
                if now - last > _HLS_HEARTBEAT_TIMEOUT:
                    log.info("HLS heartbeat timeout sid=%s — killing ffmpeg", sid)
                    _cleanup_hls(sid)

    def _throttle_loop():
        """Pause/resume ffmpeg based on how far ahead of client the buffer is."""
        while True:
            gevent.sleep(2)
            for sid, sess in list(_hls_sessions.items()):
                proc = sess.get('proc')
                if not proc or proc.poll() is not None:
                    continue
                client_pos = sess.get('client_pos', 0)
                start_offset = sess.get('start_offset', 0)
                seg_dur = 4  # seconds per HLS segment
                tmpdir = sess.get('tmpdir', '')
                try:
                    segs = sorted(f for f in os.listdir(tmpdir) if f.endswith('.ts'))
                except OSError:
                    continue
                if not segs:
                    continue
                # Estimate how many seconds are buffered ahead of client
                seg_count = len(segs)
                buffered_end = start_offset + seg_count * seg_dur
                ahead = buffered_end - client_pos
                paused = sess.get('paused', False)
                if ahead > _HLS_THROTTLE_AHEAD and not paused:
                    try:
                        os.kill(proc.pid, 19)  # SIGSTOP
                        sess['paused'] = True
                        log.debug("HLS throttle STOP sid=%s ahead=%.0fs", sid, ahead)
                    except OSError:
                        pass
                elif ahead < _HLS_THROTTLE_RESUME and paused:
                    try:
                        os.kill(proc.pid, 18)  # SIGCONT
                        sess['paused'] = False
                        log.debug("HLS throttle CONT sid=%s ahead=%.0fs", sid, ahead)
                    except OSError:
                        pass

    gevent.spawn(_cleanup_loop)
    gevent.spawn(_heartbeat_watchdog)
    gevent.spawn(_throttle_loop)
    gevent.spawn(_file_watcher_loop)


def _file_watcher_loop():
    """Watch library folders for new video files.

    Tries inotifywait first (inotify-tools package) for instant detection.
    Falls back to mtime polling every 60 s if inotifywait is not available.
    Only events CREATE and MOVED_TO are acted on; a lightweight ffprobe is
    run on the new file before triggering a full scan so the scan sees
    correct metadata without blocking the I/O poll loop.
    """
    import gevent
    gevent.sleep(30)  # initial delay — let server finish starting

    if shutil.which('inotifywait'):
        _inotify_watcher_loop()
    else:
        log.info("inotifywait not found — using mtime polling (install inotify-tools for instant detection)")
        _mtime_polling_loop()


def _inotify_watcher_loop():
    """Inotify-based watcher: instant detection via inotifywait subprocess."""
    import gevent
    while True:
        folders = _load_folders()
        valid = [f for f in folders if os.path.isdir(f)]
        if not valid:
            gevent.sleep(30)
            continue

        cmd = ['inotifywait', '-m', '-r',
               '-e', 'create,moved_to',
               '--format', '%w%f',
               '--'] + valid
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
            log.info("inotify watcher started on %s", valid)
            _pending_paths = []   # batch: accumulate rapid bursts
            _batch_deadline = [0.0]

            def _flush_batch():
                """Trigger scan after a short quiet period."""
                gevent.sleep(3)  # wait for burst to settle
                if _pending_paths and not _scan_state.get('running'):
                    log.info("inotify: %d new file(s) detected — triggering scan", len(_pending_paths))
                    _pending_paths.clear()
                    _scan_state.update(running=True, stop_requested=False,
                                       total=0, processed=0, current_file='')
                    gevent.spawn(_scan_worker, _load_folders(), False)

            flusher = [None]  # greenlet handle

            while True:
                line = proc.stdout.readline()
                if not line:
                    break  # process exited — restart loop
                path = line.rstrip('\n')
                ext = os.path.splitext(path)[1].lower()
                if ext not in VIDEO_EXTS:
                    continue
                if not os.path.isfile(path):
                    continue
                log.debug("inotify: new video %s", path)
                _pending_paths.append(path)
                if flusher[0] is None or flusher[0].dead:
                    flusher[0] = gevent.spawn(_flush_batch)
        except Exception as e:
            log.debug("inotify watcher error: %s", e)
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass

        # Restart after a short delay (e.g. folder list changed)
        gevent.sleep(15)
        # Refresh folder list in case new folders were added while we were watching
        new_folders = _load_folders()
        if set(new_folders) != set(valid):
            log.info("inotify: folder list changed (%s → %s), restarting watcher", valid, new_folders)


def _mtime_polling_loop():
    """Fallback: poll library folders every 60 s; auto-scan if any folder mtime changed."""
    import gevent
    while True:
        try:
            folders = _load_folders()
            changed = []
            for f in folders:
                if not os.path.isdir(f):
                    continue
                try:
                    mt = os.path.getmtime(f)
                except OSError:
                    continue
                prev = _watcher_state.get(f)
                _watcher_state[f] = mt
                if prev is not None and mt != prev:
                    changed.append(f)
            if changed and not _scan_state.get('running'):
                log.info("File watcher (poll): changes in %s — triggering scan", changed)
                _scan_state.update(running=True, stop_requested=False,
                                   total=0, processed=0, current_file='')
                gevent.spawn(_scan_worker, folders, False)
        except Exception as e:
            log.debug("File watcher error: %s", e)
        gevent.sleep(60)


def _evict_tmp_if_low():
    """If /tmp is more than 80% full, remove all vs_hls_* dirs older than 10 min."""
    try:
        st = os.statvfs('/tmp')
        used_pct = 100 * (1 - st.f_bavail / st.f_blocks) if st.f_blocks else 0
        if used_pct < 80:
            return
        tmp = tempfile.gettempdir()
        now = time.time()
        for name in os.listdir(tmp):
            if not name.startswith(_HLS_ORPHAN_PREFIX):
                continue
            dirpath = os.path.join(tmp, name)
            if not os.path.isdir(dirpath):
                continue
            age = now - os.path.getmtime(dirpath)
            if age > 600:  # older than 10 minutes
                shutil.rmtree(dirpath, ignore_errors=True)
    except OSError:
        pass


# --- Database ---

def _get_db():
    conn = sqlite3.connect(_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _init_db():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            folder TEXT,
            title TEXT,
            duration REAL DEFAULT 0,
            width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0,
            codec TEXT,
            audio_codec TEXT,
            bitrate INTEGER DEFAULT 0,
            file_size INTEGER DEFAULT 0,
            file_mtime REAL DEFAULT 0,
            added_at REAL DEFAULT 0,
            thumb_ok INTEGER DEFAULT 0,
            metadata_json TEXT
        );
        CREATE TABLE IF NOT EXISTS watch_state (
            video_id INTEGER PRIMARY KEY,
            watched INTEGER DEFAULT 0,
            position REAL DEFAULT 0,
            updated_at REAL DEFAULT 0,
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_videos_folder ON videos(folder);
        CREATE INDEX IF NOT EXISTS idx_videos_filename ON videos(filename);
        CREATE INDEX IF NOT EXISTS idx_videos_added_at ON videos(added_at);
    """)
    conn.commit()
    conn.close()


try:
    _init_db()
except Exception:
    pass


def _migrate_db():
    """Add TMDb columns if they don't exist."""
    try:
        conn = _get_db()
        cols = {r[1] for r in conn.execute('PRAGMA table_info(videos)').fetchall()}
        migrations = [
            ('tmdb_id', 'INTEGER DEFAULT 0'),
            ('tmdb_title', 'TEXT'),
            ('tmdb_overview', 'TEXT'),
            ('tmdb_year', 'TEXT'),
            ('tmdb_rating', 'REAL DEFAULT 0'),
            ('tmdb_genres', 'TEXT'),
            ('tmdb_poster_path', 'TEXT'),
            ('poster_ok', 'INTEGER DEFAULT 0'),
            ('tmdb_backdrop_path', 'TEXT'),
            ('backdrop_ok', 'INTEGER DEFAULT 0'),
            ('tmdb_cast', 'TEXT'),
            ('tmdb_director', 'TEXT'),
            ('tmdb_media_type', 'TEXT'),
            ('hidden', 'INTEGER DEFAULT 0'),
        ]
        for col_name, col_def in migrations:
            if col_name not in cols:
                conn.execute('ALTER TABLE videos ADD COLUMN %s %s' % (col_name, col_def))
        # watch_state migrations
        ws_cols = {r[1] for r in conn.execute('PRAGMA table_info(watch_state)').fetchall()}
        if 'last_watched_at' not in ws_cols:
            conn.execute('ALTER TABLE watch_state ADD COLUMN last_watched_at REAL DEFAULT 0')
        conn.commit()
        conn.close()
    except Exception:
        pass


_migrate_db()


# --- Helpers ---

def _sio():
    return getattr(video_station_bp, '_socketio', None)


def _get_token():
    """Return current auth token from request."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    qt = request.args.get('token', '')
    if qt:
        return qt
    return request.cookies.get('nas_token', '')


# ── hide-password helpers ──────────────────────────────────────
def _load_hide_pw():
    try:
        with open(_HIDE_PW_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_hide_pw(data):
    with open(_HIDE_PW_FILE, 'w') as f:
        json.dump(data, f)


def _hide_pw_is_set():
    return bool(_load_hide_pw().get('hash'))


def _is_hidden_unlocked():
    """Check if hidden videos are unlocked for the current session token."""
    token = _get_token()
    with _hide_lock:
        return _hide_unlocked.get(token, False)


def _emit_progress():
    s = _sio()
    if s:
        s.emit('vs_scan_progress', {
            'running': _scan_state['running'],
            'total': _scan_state['total'],
            'processed': _scan_state['processed'],
            'current_file': os.path.basename(_scan_state['current_file']),
        })


def _check_deps():
    return {
        'ffmpeg': shutil.which('ffmpeg') is not None,
        'ffprobe': shutil.which('ffprobe') is not None,
        'inotifywait': shutil.which('inotifywait') is not None,
    }


def _all_deps_ok():
    d = _check_deps()
    return d['ffmpeg'] and d['ffprobe']


def _load_folders():
    p = data_path('video_folders.json')
    if os.path.isfile(p):
        try:
            with open(p) as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []


def _save_folders(folders):
    p = data_path('video_folders.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w') as f:
        json.dump(folders, f)


def _collect_videos(folders):
    vids = []
    for folder in folders:
        fp = os.path.realpath(folder)
        if not os.path.isdir(fp):
            continue
        for root, _, files in os.walk(fp):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    vids.append(os.path.join(root, fn))
    return vids


def _probe_video(path):
    try:
        # Scale timeout by file size: small files 30s, large files (>4GB) up to 120s
        try:
            fsize = os.path.getsize(path)
            timeout = 120 if fsize > 4 * 1024**3 else 60
        except OSError:
            timeout = 60
        cmd = 'ffprobe -v quiet -print_format json -show_format -show_streams ' + q(path)
        result = host_run(cmd, timeout=timeout)
        if result.returncode != 0:
            log.debug('ffprobe non-zero exit for %s: %s', path, result.stderr)
            return {}
        data = json.loads(result.stdout)
        fmt = data.get('format', {})
        vstream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
        astreams = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
        sstreams = [s for s in data.get('streams', []) if s.get('codec_type') == 'subtitle']
        first_audio = astreams[0] if astreams else {}
        audio_tracks = []
        for i, a in enumerate(astreams):
            tags = a.get('tags', {})
            audio_tracks.append({
                'index': a.get('index', i),
                'codec': a.get('codec_name', ''),
                'channels': a.get('channels', 0),
                'language': tags.get('language', ''),
                'title': tags.get('title', ''),
            })
        sub_tracks = []
        for s in sstreams:
            tags = s.get('tags', {})
            sub_tracks.append({
                'index': s.get('index'),
                'codec': s.get('codec_name', ''),
                'language': tags.get('language', ''),
                'title': tags.get('title', ''),
            })
        return {
            'duration': float(fmt.get('duration', 0)),
            'width': int(vstream.get('width', 0)),
            'height': int(vstream.get('height', 0)),
            'codec': vstream.get('codec_name', ''),
            'audio_codec': first_audio.get('codec_name', ''),
            'bitrate': int(fmt.get('bit_rate', 0)),
            'title': fmt.get('tags', {}).get('title', ''),
            'pix_fmt': vstream.get('pix_fmt', ''),
            'audio_tracks': audio_tracks,
            'sub_tracks': sub_tracks,
        }
    except Exception as e:
        log.debug('ffprobe failed for %s: %s', path, e)
        return {}


def _generate_thumb(path, video_id, duration=0):
    os.makedirs(_THUMB_DIR, exist_ok=True)
    thumb_path = os.path.join(_THUMB_DIR, str(video_id) + '.jpg')
    seek = min(duration * 0.1, 30) if duration > 10 else 2
    try:
        cmd = 'ffmpeg -y -ss %.1f -i %s -vframes 1 -vf scale=320:-1 -q:v 4 %s' % (seek, q(path), q(thumb_path))
        result = host_run(cmd, timeout=30)
        return result.returncode == 0 and os.path.isfile(thumb_path)
    except Exception:
        return False


def _format_duration(secs):
    if not secs:
        return '0:00'
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    if h:
        return '%d:%02d:%02d' % (h, m, s)
    return '%d:%02d' % (m, s)


# --- TMDb integration ---

_TMDB_BASE = 'https://api.themoviedb.org/3'
_TMDB_IMG_BASE = 'https://image.tmdb.org/t/p/w500'
_TMDB_BACKDROP_BASE = 'https://image.tmdb.org/t/p/w1280'

# Common noise tokens stripped from filenames before TMDb search
_NOISE_RE = re.compile(
    r'\b('
    # Resolution / quality
    r'480p|576p|720p|1080p|1080i|2160p|4k|uhd|fhd|hd|'
    r'hdr|hdr10|hdr10plus|dv|dolbyvision|sdr|'
    # Audio codecs
    r'dolby|atmos|dts|dts-hd|truehd|aac|ac3|eac3|ddp5?[\.\s]?1|'
    r'flac|mp3|opus|vorbis|'
    # Video codecs / containers
    r'x264|x265|h264|h265|hevc|avc|xvid|divx|av1|vp9|'
    r'mkv|mp4|avi|mov|wmv|ts|m2ts|'
    # Source / rip type
    r'bluray|blu-ray|bdrip|brrip|bdremux|remux|'
    r'dvdrip|dvdscr|dvd|'
    r'webrip|web-dl|webdl|web|'
    r'hdtv|hdrip|pdtv|dsr|'
    r'hc|hq|cam|ts|scr|'
    # Streaming sources
    r'nf|amzn|dsnp|hmax|hulu|atvp|pcok|sho|'
    # Edition/cut tags
    r'remastered|extended|extended-cut|directors?.cut|unrated|theatrical|'
    r'open.matte|imax|proper|repack|internal|limited|retail|'
    # Language tags
    r'multi|dual|pl|eng|ger|fra|por|spa|ita|rus|cze|'
    r'dubbed|lektor|napisy|subs|subbed|'
    # Common release groups / site prefixes
    r'yts|yify|rarbg|eztv|ettv|sparks|geckos|fgt|'
    r'nodlabs|rzero-?x|poke|cmrg|ntg|fum|wbr|nhanc3|'
    r'evo|galadriel|axxo|vxt|mzabi|tigole|qxr|flux|'
    # Bracket/paren noise
    r'sample|trailer|featurette|extra'
    r')\b', re.IGNORECASE
)
# Strip site-prefix patterns like "DDLValley.me_83_" or "SiteTag_NNN_"
_SITE_PREFIX_RE = re.compile(r'^[A-Za-z0-9]+\.[a-z]{2,4}[_\-\s]\d*[_\-\s]', re.IGNORECASE)
# TV episode pattern: S01E01, S01, E01, 1x01
_EPISODE_RE = re.compile(r'\bS\d{1,2}E\d{1,2}\b|\bS\d{1,2}\b|\bE\d{1,2}\b|\b\d{1,2}x\d{2}\b', re.IGNORECASE)
_YEAR_RE = re.compile(r'[\(\[\.]?((?:19|20)\d{2})[\)\]\.]?')
_CLEAN_RE = re.compile(r'[\.\-_]+')
_MULTI_SPACE = re.compile(r'\s{2,}')


def _parse_filename(filename):
    """Extract clean title and year from a video filename.

    Handles:
      - Site prefixes: DDLValley.me_83_scream.7.2026 → Scream 7
      - Quality/codec noise: 1080p, x265, BluRay, HEVC, AMZN, etc.
      - Release groups: NodLabs, POKE, RZeroX, etc.
      - TV episode tags: S01E01, 2x04, etc. (stripped for TMDb search)
    """
    name = os.path.splitext(filename)[0]
    # Strip leading site-prefix (e.g. "DDLValley.me_83_")
    name = _SITE_PREFIX_RE.sub('', name)
    # Try to find year first — everything before it is likely the title
    year_match = _YEAR_RE.search(name)
    year = ''
    if year_match:
        year = year_match.group(1)
        name = name[:year_match.start()]
    else:
        # Cut off at first episode marker if no year
        ep_match = _EPISODE_RE.search(name)
        if ep_match:
            name = name[:ep_match.start()]
    # Replace dots/dashes/underscores with spaces
    name = _CLEAN_RE.sub(' ', name)
    # Remove noise tokens
    name = _NOISE_RE.sub('', name)
    # Remove leftover episode tags (e.g. "S01E01" after space normalisation)
    name = _EPISODE_RE.sub('', name)
    name = _MULTI_SPACE.sub(' ', name).strip()
    return name, year


def _load_tmdb_key():
    if os.path.isfile(_TMDB_CONF):
        try:
            with open(_TMDB_CONF) as f:
                return json.loads(f.read()).get('api_key', '')
        except Exception:
            pass
    return ''


def _save_tmdb_key(key):
    os.makedirs(os.path.dirname(_TMDB_CONF), exist_ok=True)
    with open(_TMDB_CONF, 'w') as f:
        json.dump({'api_key': key}, f)


def _tmdb_search(title, year='', api_key=''):
    """Search TMDb for a movie/TV show. Returns best match dict or None."""
    if not api_key:
        api_key = _load_tmdb_key()
    if not api_key:
        return None
    params = urllib.parse.urlencode({
        'api_key': api_key, 'query': title, 'language': 'pl-PL',
    })
    if year:
        params += '&year=' + str(year)

    # Try movie search first
    for endpoint in ['/search/movie', '/search/tv']:
        try:
            url = _TMDB_BASE + endpoint + '?' + params
            req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            results = data.get('results', [])
            if results:
                r = results[0]
                is_movie = endpoint == '/search/movie'
                return {
                    'tmdb_id': r.get('id', 0),
                    'tmdb_title': r.get('title' if is_movie else 'name', ''),
                    'tmdb_overview': r.get('overview', ''),
                    'tmdb_year': (r.get('release_date' if is_movie else 'first_air_date', '') or '')[:4],
                    'tmdb_rating': r.get('vote_average', 0),
                    'tmdb_genres': ','.join(str(g) for g in r.get('genre_ids', [])),
                    'tmdb_poster_path': r.get('poster_path', ''),
                    'tmdb_backdrop_path': r.get('backdrop_path', ''),
                    'media_type': 'movie' if is_movie else 'tv',
                }
        except Exception as e:
            log.debug('TMDb search failed (%s): %s', endpoint, e)
    return None


def _download_poster(poster_path, video_id):
    """Download TMDb poster and save locally."""
    if not poster_path:
        return False
    os.makedirs(_POSTER_DIR, exist_ok=True)
    local = os.path.join(_POSTER_DIR, str(video_id) + '.jpg')
    try:
        url = _TMDB_IMG_BASE + poster_path
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            with open(local, 'wb') as f:
                f.write(resp.read())
        return os.path.isfile(local)
    except Exception as e:
        log.debug('Poster download failed for vid %s: %s', video_id, e)
        return False


def _download_backdrop(backdrop_path, video_id):
    """Download TMDb backdrop and save locally."""
    if not backdrop_path:
        return False
    os.makedirs(_BACKDROP_DIR, exist_ok=True)
    local = os.path.join(_BACKDROP_DIR, str(video_id) + '.jpg')
    try:
        url = _TMDB_BACKDROP_BASE + backdrop_path
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            with open(local, 'wb') as f:
                f.write(resp.read())
        return os.path.isfile(local)
    except Exception as e:
        log.debug('Backdrop download failed for vid %s: %s', video_id, e)
        return False


def _fetch_tmdb_credits(tmdb_id, media_type, api_key):
    """Fetch cast + director from TMDb credits API."""
    if not tmdb_id or not api_key:
        return '', ''
    try:
        url = '%s/%s/%d/credits?api_key=%s' % (
            _TMDB_BASE, media_type, tmdb_id, urllib.parse.quote(api_key))
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        cast_names = [c['name'] for c in (data.get('cast') or [])[:10]]
        directors = [c['name'] for c in (data.get('crew') or [])
                     if c.get('job') == 'Director']
        return ', '.join(cast_names), ', '.join(directors)
    except Exception as e:
        log.debug('TMDb credits fetch failed for %s/%s: %s', media_type, tmdb_id, e)
        return '', ''


def _tmdb_match_video(conn, video_id, filename, api_key=''):
    """Parse filename, search TMDb, update DB, download poster & backdrop, fetch credits."""
    title, year = _parse_filename(filename)
    if not title:
        return None
    result = _tmdb_search(title, year, api_key)
    if not result:
        return None

    # Fetch credits (cast + director)
    media_type = result.get('media_type', 'movie')
    cast, director = _fetch_tmdb_credits(result['tmdb_id'], media_type, api_key)

    conn.execute(
        'UPDATE videos SET tmdb_id=?, tmdb_title=?, tmdb_overview=?, '
        'tmdb_year=?, tmdb_rating=?, tmdb_genres=?, tmdb_poster_path=?, '
        'tmdb_backdrop_path=?, tmdb_cast=?, tmdb_director=?, tmdb_media_type=? WHERE id=?',
        (result['tmdb_id'], result['tmdb_title'], result['tmdb_overview'],
         result['tmdb_year'], result['tmdb_rating'], result['tmdb_genres'],
         result['tmdb_poster_path'], result.get('tmdb_backdrop_path', ''),
         cast, director, media_type, video_id))
    conn.commit()
    # Update display title to TMDb title
    if result['tmdb_title']:
        display = result['tmdb_title']
        if result['tmdb_year']:
            display += ' (' + result['tmdb_year'] + ')'
        conn.execute('UPDATE videos SET title=? WHERE id=?', (display, video_id))
        conn.commit()
    # Download poster
    if result['tmdb_poster_path']:
        ok = _download_poster(result['tmdb_poster_path'], video_id)
        if ok:
            conn.execute('UPDATE videos SET poster_ok=1 WHERE id=?', (video_id,))
            conn.commit()
    # Download backdrop
    if result.get('tmdb_backdrop_path'):
        ok = _download_backdrop(result['tmdb_backdrop_path'], video_id)
        if ok:
            conn.execute('UPDATE videos SET backdrop_ok=1 WHERE id=?', (video_id,))
            conn.commit()
    return result


# --- Scan worker ---

def _scan_worker(folders, use_tmdb=False):
    import gevent
    t0 = time.time()
    all_paths = _collect_videos(folders)
    conn = _get_db()
    existing = {r["path"] for r in conn.execute("SELECT path FROM videos").fetchall()}
    todo = []
    for p in all_paths:
        try:
            mt = os.path.getmtime(p)
            sz = os.path.getsize(p)
        except OSError:
            continue
        if p in existing:
            row = conn.execute("SELECT file_mtime FROM videos WHERE path=?", (p,)).fetchone()
            if row and abs(mt - row["file_mtime"]) < 1.0:
                continue
        todo.append((p, mt, sz))

    already = len(all_paths) - len(todo)
    _scan_state.update(total=len(all_paths), processed=already, current_file="")
    _emit_progress()

    all_set = set(all_paths)
    gone = [r["id"] for r in conn.execute("SELECT id, path FROM videos").fetchall() if r["path"] not in all_set]
    if gone:
        conn.executemany("DELETE FROM videos WHERE id=?", [(g,) for g in gone])
        conn.commit()

    if not todo:
        _scan_state["running"] = False
        _emit_progress()
        s = _sio()
        if s:
            s.emit("vs_scan_done", {"total_processed": 0, "duration": 0, "message": "Brak nowych filmow."})
        conn.close()
        return

    for i, (path, mt, sz) in enumerate(todo):
        if _scan_state.get("stop_requested"):
            break
        _scan_state["current_file"] = path
        gevent.sleep(0.1)
        meta = _probe_video(path)
        fn = os.path.basename(path)
        folder = os.path.dirname(path)
        title = meta.get("title") or os.path.splitext(fn)[0]
        conn.execute(
            "INSERT INTO videos (path,filename,folder,title,duration,width,height,"
            "codec,audio_codec,bitrate,file_size,file_mtime,added_at,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "title=excluded.title,duration=excluded.duration,width=excluded.width,"
            "height=excluded.height,codec=excluded.codec,audio_codec=excluded.audio_codec,"
            "bitrate=excluded.bitrate,file_size=excluded.file_size,"
            "file_mtime=excluded.file_mtime,metadata_json=excluded.metadata_json",
            (path, fn, folder, title, meta.get("duration", 0),
             meta.get("width", 0), meta.get("height", 0),
             meta.get("codec", ""), meta.get("audio_codec", ""),
             meta.get("bitrate", 0), sz, mt, time.time(), json.dumps(meta)))
        conn.commit()
        vid_row = conn.execute("SELECT id FROM videos WHERE path=?", (path,)).fetchone()
        if vid_row:
            ok = _generate_thumb(path, vid_row["id"], meta.get("duration", 0))
            if ok:
                conn.execute("UPDATE videos SET thumb_ok=1 WHERE id=?", (vid_row["id"],))
                conn.commit()
            # Background thumbstrip generation (sprite for seek preview)
            sprite_path = os.path.join(_THUMBSTRIP_DIR, str(vid_row["id"]) + ".jpg")
            if not os.path.isfile(sprite_path) and meta.get("duration", 0) > 30:
                vid_id = vid_row["id"]
                dur = meta.get("duration", 0)
                gevent.spawn(_generate_thumbstrip, vid_id, path, dur, sprite_path)
            # TMDb matching during scan
            if use_tmdb and _load_tmdb_key():
                try:
                    _tmdb_match_video(conn, vid_row["id"], fn)
                    gevent.sleep(0.3)  # respect TMDb rate limit (~40 req/10s)
                except Exception as e:
                    log.debug('TMDb match failed for %s: %s', fn, e)
        _scan_state["processed"] = already + i + 1
        if (i + 1) % 3 == 0 or i == 0:
            _emit_progress()
            gevent.sleep(0.05)

    conn.close()
    dur = time.time() - t0
    _scan_state.update(running=False, stop_requested=False, current_file="")
    _emit_progress()
    s = _sio()
    if s:
        s.emit("vs_scan_done", {"total_processed": _scan_state["processed"], "duration": round(dur, 1)})


# --- Routes ---

@video_station_bp.route("/pkg-status", methods=["GET"])

def pkg_status():
    deps = _check_deps()
    stats = {}
    if os.path.isfile(_DB_PATH):
        c = None
        try:
            c = _get_db()
            stats = {
                "videos": c.execute("SELECT COUNT(*) FROM videos WHERE COALESCE(hidden,0)=0").fetchone()[0],
                "total_videos": c.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
                "total_size": c.execute("SELECT COALESCE(SUM(file_size),0) FROM videos").fetchone()[0],
                "watched": c.execute("SELECT COUNT(*) FROM watch_state WHERE watched=1").fetchone()[0],
                "hidden": c.execute("SELECT COUNT(*) FROM videos WHERE COALESCE(hidden,0)=1").fetchone()[0],
            }
        except Exception:
            pass
        finally:
            if c:
                c.close()
    return jsonify({
        "installed": deps["ffmpeg"] and deps["ffprobe"], "deps": deps,
        "stats": stats, "scanning": _scan_state["running"],
        "hide_password_set": _hide_pw_is_set(),
        "hide_unlocked": _is_hidden_unlocked(),
    })


@video_station_bp.route("/install", methods=["POST"])
@admin_required
def install_deps():
    import gevent
    def _do():
        s = _sio()
        try:
            if s:
                s.emit("vs_install", {"stage": "start", "percent": 10, "message": "Instalowanie ffmpeg..."})
            host_run("apt-get update -qq && apt-get install -y -qq ffmpeg inotify-tools && apt-get clean", timeout=300)
            if s:
                s.emit("vs_install", {"stage": "done", "percent": 100, "message": "Gotowe!"})
        except Exception as e:
            log.error("VS install failed: %s", e)
            if s:
                s.emit("vs_install", {"stage": "error", "percent": 0, "message": str(e)})
    gevent.spawn(_do)
    return jsonify({"ok": True, "message": "Instalacja w tle..."})


@video_station_bp.route("/uninstall", methods=["POST"])
@admin_required
def uninstall_deps():
    _scan_state["stop_requested"] = True
    wipe = (request.json or {}).get("wipe_data", False)
    if wipe:
        for p in [_DB_PATH, _THUMB_DIR, data_path("video_folders.json")]:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                elif os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
    return jsonify({"ok": True})


@video_station_bp.route("/folders", methods=["GET"])

def get_folders():
    return jsonify({"folders": _load_folders()})


# System directories that should never be added as library folders
_SYSTEM_PATHS = {'/', '/etc', '/sys', '/proc', '/dev', '/boot', '/root', '/run', '/tmp', '/var', '/snap'}
_SYSTEM_PREFIXES = ('/etc/', '/sys/', '/proc/', '/dev/', '/boot/', '/root/', '/run/', '/snap/')

def _is_safe_library_path(folder):
    """Reject system directories and paths inside protected system trees."""
    rp = os.path.realpath(folder)
    if rp in _SYSTEM_PATHS:
        return False
    if rp.startswith(_SYSTEM_PREFIXES):
        return False
    return True


@video_station_bp.route("/folders", methods=["POST"])
@admin_required
def save_folders():
    folders = (request.json or {}).get("folders", [])
    valid = []
    for f in folders:
        if not isinstance(f, str) or not f.startswith("/"):
            continue
        rp = os.path.realpath(f)
        if os.path.isdir(rp) and _is_safe_library_path(rp):
            valid.append(rp)
    _save_folders(valid)
    return jsonify({"ok": True, "folders": valid})


@video_station_bp.route("/scan", methods=["POST"])
@admin_required
def start_scan():
    if _scan_state["running"]:
        return jsonify({"error": "Skan juz trwa."}), 409
    if not _all_deps_ok():
        return jsonify({"error": "Brak ffmpeg/ffprobe."}), 400
    folders = _load_folders()
    if not folders:
        return jsonify({"error": "Brak skonfigurowanych folderow."}), 400
    _scan_state.update(running=True, stop_requested=False, total=0, processed=0, current_file="")
    use_tmdb = (request.json or {}).get("use_tmdb", False)
    import gevent
    gevent.spawn(_scan_worker, folders, use_tmdb)
    return jsonify({"ok": True})


@video_station_bp.route("/scan-stop", methods=["POST"])
@admin_required
def stop_scan():
    _scan_state["stop_requested"] = True
    return jsonify({"ok": True})


@video_station_bp.route("/scan-status", methods=["GET"])

def scan_status():
    return jsonify({
        "running": _scan_state["running"],
        "total": _scan_state["total"],
        "processed": _scan_state["processed"],
        "current_file": os.path.basename(_scan_state["current_file"]),
    })


@video_station_bp.route("/continue-watching", methods=["GET"])

def continue_watching():
    """Return videos with saved position > 0 that are not yet marked as watched."""
    conn = _get_db()
    limit = min(int(request.args.get("limit", 20)), 60)
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE ws.position > 0 AND (ws.watched=0 OR ws.watched IS NULL) "
        "AND COALESCE(v.hidden,0)=0 "
        "ORDER BY ws.updated_at DESC LIMIT ?", (limit,)).fetchall()
    items = [{
        "id": r["id"], "title": r["title"], "filename": r["filename"],
        "path": r["path"], "duration": r["duration"],
        "duration_fmt": _format_duration(r["duration"]),
        "width": r["width"], "height": r["height"],
        "thumb_ok": bool(r["thumb_ok"]),
        "poster_ok": bool(r["poster_ok"]),
        "tmdb_id": r["tmdb_id"] or 0,
        "tmdb_title": r["tmdb_title"] or "",
        "tmdb_year": r["tmdb_year"] or "",
        "tmdb_rating": r["tmdb_rating"] or 0,
        "watched": False, "position": r["position"] or 0,
    } for r in rows]
    conn.close()
    return jsonify({"items": items})


@video_station_bp.route("/history", methods=["GET"])

def watch_history():
    """Return recently watched videos (fully watched), sorted by last_watched_at desc."""
    conn = _get_db()
    limit = min(int(request.args.get("limit", 40)), 100)
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position, ws.last_watched_at FROM videos v "
        "JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE ws.watched=1 AND COALESCE(v.hidden,0)=0 "
        "ORDER BY COALESCE(ws.last_watched_at, ws.updated_at) DESC LIMIT ?",
        (limit,)).fetchall()
    items = [{
        "id": r["id"], "title": r["title"], "filename": r["filename"],
        "path": r["path"], "duration": r["duration"],
        "duration_fmt": _format_duration(r["duration"]),
        "width": r["width"], "height": r["height"],
        "thumb_ok": bool(r["thumb_ok"]),
        "poster_ok": bool(r["poster_ok"]),
        "tmdb_id": r["tmdb_id"] or 0,
        "tmdb_title": r["tmdb_title"] or "",
        "tmdb_year": r["tmdb_year"] or "",
        "tmdb_rating": r["tmdb_rating"] or 0,
        "tmdb_genres": r["tmdb_genres"] or "",
        "watched": True,
        "position": r["position"] or 0,
        "last_watched_at": r["last_watched_at"] or r["updated_at"] or 0,
    } for r in rows]
    conn.close()
    return jsonify({"items": items})


@video_station_bp.route("/library", methods=["GET"])

def library():
    conn = _get_db()
    offset = int(request.args.get("offset", 0))
    limit = min(int(request.args.get("limit", 60)), 200)
    sort = request.args.get("sort", "added_desc")
    q_search = request.args.get("q", "").strip()
    folder_filter = request.args.get("folder", "")
    watched_filter = request.args.get("watched", "")
    codec_filter = request.args.get("codec", "")      # "hevc" | "av1" | "h264" | "transcode"
    res_filter = request.args.get("res", "")           # "4k" | "1080p" | "720p"
    show_hidden = request.args.get("show_hidden", "") == "1"
    order_map = {
        "added_desc": "added_at DESC", "added_asc": "added_at ASC",
        "name_asc": "title ASC", "name_desc": "title DESC",
        "duration_desc": "duration DESC", "duration_asc": "duration ASC",
        "size_desc": "file_size DESC", "size_asc": "file_size ASC",
    }
    order = order_map.get(sort, "added_at DESC")
    where = []
    params = []
    if show_hidden and _is_hidden_unlocked():
        where.append("COALESCE(v.hidden,0)=1")
    else:
        where.append("COALESCE(v.hidden,0)=0")
    if q_search:
        where.append("(title LIKE ? OR filename LIKE ?)")
        params += ["%" + q_search + "%", "%" + q_search + "%"]
    if folder_filter:
        where.append("folder=?")
        params.append(folder_filter)
    if watched_filter == '1':
        where.append("COALESCE(ws.watched, 0)=1")
    elif watched_filter == '0':
        where.append("COALESCE(ws.watched, 0)=0")
    if codec_filter == 'transcode':
        where.append("(LOWER(v.codec) NOT IN ('h264','avc1','vp8','vp9','theora','av1')"
                     " OR LOWER(v.audio_codec) NOT IN ('aac','mp3','opus','vorbis','flac'))")
    elif codec_filter in ('hevc', 'av1', 'h264'):
        where.append("LOWER(v.codec)=?")
        params.append(codec_filter)
    if res_filter == '4k':
        where.append("v.width >= 3840")
    elif res_filter == '1080p':
        where.append("v.width >= 1920 AND v.width < 3840")
    elif res_filter == '720p':
        where.append("v.width >= 1280 AND v.width < 1920")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM videos v LEFT JOIN watch_state ws ON ws.video_id=v.id "
        + where_sql, params).fetchone()[0]
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        + where_sql + " ORDER BY " + order + " LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "title": r["title"], "filename": r["filename"],
            "path": r["path"], "folder": r["folder"],
            "duration": r["duration"], "duration_fmt": _format_duration(r["duration"]),
            "width": r["width"], "height": r["height"],
            "codec": r["codec"], "file_size": r["file_size"],
            "thumb_ok": bool(r["thumb_ok"]),
            "poster_ok": bool(r["poster_ok"]),
            "tmdb_id": r["tmdb_id"] or 0,
            "tmdb_title": r["tmdb_title"] or "",
            "tmdb_year": r["tmdb_year"] or "",
            "tmdb_rating": r["tmdb_rating"] or 0,
            "tmdb_overview": r["tmdb_overview"] or "",
            "tmdb_genres": r["tmdb_genres"] or "",
            "watched": bool(r["watched"]), "position": r["position"] or 0,
            "added_at": r["added_at"],
            "hidden": bool(r["hidden"]),
        })
    conn.close()
    return jsonify({"items": items, "total": total, "offset": offset, "limit": limit})


def _row_items(rows):
    """Convert DB rows to lean dicts for Netflix home rows."""
    items = []
    for r in rows:
        d = dict(r)
        items.append({
            "id": d["id"], "title": d["title"], "filename": d["filename"],
            "duration": d["duration"], "duration_fmt": _format_duration(d["duration"]),
            "thumb_ok": bool(d.get("thumb_ok")), "poster_ok": bool(d.get("poster_ok")),
            "backdrop_ok": bool(d.get("backdrop_ok", 0)),
            "tmdb_id": d.get("tmdb_id") or 0,
            "tmdb_title": d.get("tmdb_title") or "",
            "tmdb_year": d.get("tmdb_year") or "",
            "tmdb_rating": d.get("tmdb_rating") or 0,
            "tmdb_overview": d.get("tmdb_overview") or "",
            "tmdb_genres": d.get("tmdb_genres") or "",
            "tmdb_media_type": d.get("tmdb_media_type") or "movie",
            "watched": bool(d.get("watched")), "position": d.get("position") or 0,
            "added_at": d.get("added_at"),
        })
    return items


@video_station_bp.route("/home", methods=["GET"])
def home():
    """Netflix-style home rows: hero, continue watching, recently added, per-genre rows."""
    conn = _get_db()
    hidden_clause = "COALESCE(v.hidden,0)=0"

    # Hero: last watched with backdrop, else last added with backdrop
    hero_row = conn.execute(
        "SELECT v.*, ws.watched, ws.position, ws.last_watched_at FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE " + hidden_clause + " AND v.backdrop_ok=1 "
        "ORDER BY COALESCE(ws.last_watched_at, v.added_at) DESC LIMIT 1"
    ).fetchone()
    hero = None
    if hero_row:
        hero = _row_items([hero_row])[0]
        hero["last_watched_at"] = dict(hero_row).get("last_watched_at")

    # Continue watching — has position > 5% of duration
    cw_rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE " + hidden_clause + " AND ws.watched=0 "
        "AND v.duration > 0 AND ws.position > (v.duration * 0.05) "
        "ORDER BY ws.updated_at DESC LIMIT 20"
    ).fetchall()

    # Recently added
    recent_rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE " + hidden_clause + " ORDER BY v.added_at DESC LIMIT 20"
    ).fetchall()

    # Genre rows — collect top genres present in library
    genre_rows = conn.execute(
        "SELECT tmdb_genres FROM videos WHERE COALESCE(hidden,0)=0"
        " AND tmdb_genres IS NOT NULL AND tmdb_genres != '' LIMIT 500"
    ).fetchall()
    # Count genre IDs
    from collections import Counter
    genre_counter = Counter()
    for g in genre_rows:
        for gid in (g["tmdb_genres"] or "").split(","):
            gid = gid.strip()
            if gid:
                genre_counter[gid] += 1
    # Top 6 genres
    top_genres = [gid for gid, _ in genre_counter.most_common(6)]

    genre_sections = []
    for gid in top_genres:
        g_rows = conn.execute(
            "SELECT v.*, ws.watched, ws.position FROM videos v "
            "LEFT JOIN watch_state ws ON ws.video_id=v.id "
            "WHERE " + hidden_clause + " AND (',' || v.tmdb_genres || ',') LIKE ? "
            "ORDER BY v.tmdb_rating DESC LIMIT 20",
            ('%,' + gid + ',%',)
        ).fetchall()
        if g_rows:
            genre_sections.append({"genre_id": int(gid), "items": _row_items(g_rows)})

    conn.close()
    return jsonify({
        "hero": hero,
        "continue_watching": _row_items(cw_rows),
        "recently_added": _row_items(recent_rows),
        "genres": genre_sections,
    })


@video_station_bp.route("/recent", methods=["GET"])

def recent():
    conn = _get_db()
    limit = min(int(request.args.get("limit", 20)), 60)
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE COALESCE(v.hidden,0)=0 "
        "ORDER BY v.added_at DESC LIMIT ?", (limit,)).fetchall()
    items = [{
        "id": r["id"], "title": r["title"], "filename": r["filename"],
        "path": r["path"], "duration": r["duration"],
        "duration_fmt": _format_duration(r["duration"]),
        "width": r["width"], "height": r["height"],
        "thumb_ok": bool(r["thumb_ok"]),
        "poster_ok": bool(r["poster_ok"]),
        "tmdb_id": r["tmdb_id"] or 0,
        "tmdb_title": r["tmdb_title"] or "",
        "tmdb_year": r["tmdb_year"] or "",
        "tmdb_rating": r["tmdb_rating"] or 0,
        "watched": bool(r["watched"]), "position": r["position"] or 0,
    } for r in rows]
    conn.close()
    return jsonify({"items": items})


@video_station_bp.route("/collections", methods=["GET"])

def collections():
    conn = _get_db()
    rows = conn.execute(
        "SELECT folder, COUNT(*) as cnt, SUM(duration) as total_dur, "
        "(SELECT id FROM videos v2 WHERE v2.folder=v.folder AND v2.thumb_ok=1 AND COALESCE(v2.hidden,0)=0 LIMIT 1) as cover_id "
        "FROM videos v WHERE COALESCE(v.hidden,0)=0 GROUP BY folder ORDER BY cnt DESC LIMIT 50").fetchall()
    colls = []
    for r in rows:
        folder = r["folder"]
        name = os.path.basename(folder) or folder
        colls.append({
            "folder": folder, "name": name, "count": r["cnt"],
            "total_duration": _format_duration(r["total_dur"] or 0),
            "cover_id": r["cover_id"],
        })
    conn.close()
    return jsonify({"collections": colls})


@video_station_bp.route("/info/<int:vid>", methods=["GET"])

def video_info(vid):
    conn = _get_db()
    r = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id WHERE v.id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    meta = {}
    try:
        meta = json.loads(r["metadata_json"] or "{}")
    except Exception:
        pass
    audio_codec = (r["audio_codec"] or "").lower()
    video_codec = (r["codec"] or "").lower()
    ext = os.path.splitext(r["path"])[1].lower()
    needs_tc = (bool(video_codec) and video_codec not in _BROWSER_VIDEO_CODECS) or \
               (bool(audio_codec) and audio_codec not in _BROWSER_AUDIO_CODECS) or \
               (ext not in _BROWSER_CONTAINERS)
    audio_tracks = meta.get("audio_tracks", [])

    # Genre ID → name mapping (TMDb standard)
    _GENRE_MAP = {
        28: 'Akcja', 12: 'Przygodowy', 16: 'Animacja', 35: 'Komedia', 80: 'Kryminał',
        99: 'Dokumentalny', 18: 'Dramat', 10751: 'Familijny', 14: 'Fantasy',
        36: 'Historyczny', 27: 'Horror', 10402: 'Muzyczny', 9648: 'Tajemnica',
        10749: 'Romans', 878: 'Sci-Fi', 10770: 'Film TV', 53: 'Thriller',
        10752: 'Wojenny', 37: 'Western',
        10759: 'Akcja i Przygoda', 10762: 'Dla dzieci', 10763: 'Informacyjny',
        10764: 'Reality', 10765: 'Sci-Fi & Fantasy', 10766: 'Telenowela',
        10767: 'Talk-show', 10768: 'Wojenny i Polityczny',
    }
    genre_ids = (r["tmdb_genres"] or "").split(",")
    genre_names = [_GENRE_MAP.get(int(g.strip()), '') for g in genre_ids if g.strip().isdigit()]
    genre_names = [g for g in genre_names if g]

    return jsonify({
        "id": r["id"], "title": r["title"], "filename": r["filename"],
        "path": r["path"], "folder": r["folder"],
        "duration": r["duration"], "duration_fmt": _format_duration(r["duration"]),
        "width": r["width"], "height": r["height"],
        "codec": r["codec"], "audio_codec": r["audio_codec"],
        "bitrate": r["bitrate"], "file_size": r["file_size"],
        "thumb_ok": bool(r["thumb_ok"]),
        "poster_ok": bool(r["poster_ok"]),
        "backdrop_ok": bool(r["backdrop_ok"]) if "backdrop_ok" in r.keys() else False,
        "tmdb_id": r["tmdb_id"] or 0,
        "tmdb_title": r["tmdb_title"] or "",
        "tmdb_overview": r["tmdb_overview"] or "",
        "tmdb_year": r["tmdb_year"] or "",
        "tmdb_rating": r["tmdb_rating"] or 0,
        "tmdb_genres": r["tmdb_genres"] or "",
        "genre_names": genre_names,
        "tmdb_cast": r["tmdb_cast"] or "" if "tmdb_cast" in r.keys() else "",
        "tmdb_director": r["tmdb_director"] or "" if "tmdb_director" in r.keys() else "",
        "watched": bool(r["watched"]), "position": r["position"] or 0,
        "added_at": r["added_at"], "metadata": meta,
        "needs_transcode": needs_tc,
        "audio_tracks": audio_tracks,
    })


@video_station_bp.route("/thumb/<int:vid>", methods=["GET"])

def thumb(vid):
    p = os.path.join(_THUMB_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak miniatury."}), 404


@video_station_bp.route("/poster/<int:vid>", methods=["GET"])

def poster(vid):
    p = os.path.join(_POSTER_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak plakatu."}), 404


@video_station_bp.route("/backdrop/<int:vid>", methods=["GET"])

def backdrop(vid):
    p = os.path.join(_BACKDROP_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak tła."}), 404


_thumbstrip_generating = set()  # video IDs currently being generated


@video_station_bp.route("/thumbstrip/<int:vid>", methods=["GET"])

def thumbstrip(vid):
    """Serve seekbar thumbnail sprite image.

    Returns the sprite image (JPEG) if cached. If not cached, kicks off
    background generation and returns 202 — client should retry later.
    Sprite: 160x90 thumbnails every 30s, tiled 10 columns.
    Uses fast keyframe-seek per thumbnail instead of decoding all frames.
    """
    conn = _get_db()
    r = conn.execute("SELECT path, duration FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404

    sprite_path = os.path.join(_THUMBSTRIP_DIR, str(vid) + ".jpg")

    # Serve cached sprite
    if os.path.isfile(sprite_path):
        resp = send_file(sprite_path, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=604800"
        return resp

    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify({"error": "Plik nie istnieje."}), 404

    duration = r["duration"] or 0
    if duration < 30:
        return jsonify({"error": "Film za krótki."}), 400

    if vid in _thumbstrip_generating:
        return jsonify({"status": "generating"}), 202

    # Start background generation
    _thumbstrip_generating.add(vid)
    import gevent
    gevent.spawn(_generate_thumbstrip, vid, fp, duration, sprite_path)
    return jsonify({"status": "generating"}), 202


def _generate_thumbstrip(vid, fp, duration, sprite_path):
    """Generate thumbnail sprite using fast keyframe seeks (background task)."""
    tmpdir = None
    try:
        from PIL import Image
        os.makedirs(_THUMBSTRIP_DIR, exist_ok=True)
        interval = 30  # one thumb every 30 seconds
        tmpdir = tempfile.mkdtemp(prefix="vs_ts_")
        positions = list(range(0, int(duration), interval))
        if not positions:
            return

        # Extract individual thumbnails using fast seek (-ss before -i)
        thumb_w, thumb_h = 160, 90
        cols = 10
        frame_paths = []
        for i, pos in enumerate(positions):
            frame_path = os.path.join(tmpdir, "f%04d.jpg" % i)
            cmd = 'ffmpeg -y -ss %d -i %s -vframes 1 -vf scale=%d:%d -q:v 6 %s' % (
                pos, q(fp), thumb_w, thumb_h, q(frame_path))
            result = host_run(cmd, timeout=30)
            if result.returncode == 0 and os.path.isfile(frame_path):
                frame_paths.append(frame_path)
            else:
                frame_paths.append(None)  # placeholder

        valid = [p for p in frame_paths if p]
        if not valid:
            log.warning('Thumbstrip: no frames extracted for vid %s', vid)
            return

        # Tile into sprite using PIL (simple and reliable)
        rows = (len(frame_paths) + cols - 1) // cols
        sprite = Image.new('RGB', (cols * thumb_w, rows * thumb_h), (0, 0, 0))
        for i, fpath in enumerate(frame_paths):
            if fpath and os.path.isfile(fpath):
                try:
                    img = Image.open(fpath)
                    sprite.paste(img, ((i % cols) * thumb_w, (i // cols) * thumb_h))
                    img.close()
                except Exception:
                    pass  # black placeholder for failed frames
        sprite.save(sprite_path, 'JPEG', quality=70)
        log.info('Thumbstrip generated for vid %s: %d frames, %s',
                 vid, len(valid), sprite_path)
    except Exception as e:
        log.warning('Thumbstrip generation error for vid %s: %s', vid, e)
    finally:
        _thumbstrip_generating.discard(vid)
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


@video_station_bp.route("/stream/<int:vid>", methods=["GET"])

def stream(vid):
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify({"error": "Plik nie istnieje."}), 404
    ext = os.path.splitext(fp)[1].lower()
    mime_map = {
        ".mp4": "video/mp4", ".webm": "video/webm",
        ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".mov": "video/quicktime", ".m4v": "video/mp4",
        ".ogv": "video/ogg", ".ts": "video/mp2t",
    }
    mime = mime_map.get(ext, "video/mp4")
    fsize = os.path.getsize(fp)
    range_header = request.headers.get("Range")
    if range_header:
        byte_start = 0
        byte_end = fsize - 1
        match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            byte_start = int(match.group(1))
            if match.group(2):
                byte_end = int(match.group(2))
        length = byte_end - byte_start + 1

        def gen():
            with open(fp, "rb") as f:
                f.seek(byte_start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        resp = Response(gen(), 206, mimetype=mime)
        resp.headers["Content-Range"] = "bytes %d-%d/%d" % (byte_start, byte_end, fsize)
        resp.headers["Content-Length"] = str(length)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp
    return send_file(fp, mimetype=mime)


@video_station_bp.route("/transcode/<int:vid>", methods=["GET"])

def transcode(vid):
    """Stream video with audio re-encoded to AAC for browser compatibility.

    Uses ffmpeg to copy the video stream (when h264) and transcode audio
    to AAC, outputting fragmented MP4 suitable for progressive HTTP streaming.

    Query params:
        start  - seek to position in seconds before encoding
        audio  - ffmpeg stream index for audio track (default: first audio)
    """
    conn = _get_db()
    r = conn.execute("SELECT path, codec, audio_codec, metadata_json FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify({"error": "Plik nie istnieje."}), 404
    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg nie jest zainstalowany."}), 500

    vcodec = (r["codec"] or "").lower()
    vcopy = vcodec in ("h264", "vp8", "vp9")

    start_sec = max(0.0, request.args.get("start", 0, type=float))
    audio_idx = request.args.get("audio", None, type=int)

    meta = json.loads(r["metadata_json"] or "{}")

    # Validate audio track index
    if audio_idx is not None:
        try:
            tracks = meta.get("audio_tracks", [])
        except Exception:
            tracks = []
        valid_indices = {t.get("index") for t in tracks} if tracks else set()
        if valid_indices and audio_idx not in valid_indices:
            return jsonify({"error": "Nieprawidłowy indeks ścieżki audio."}), 400

    # Build video encoder args (with HW acceleration when available)
    hw_pre = []
    vid_w = int(meta.get('width') or 0)
    vid_h = int(meta.get('height') or 0)
    pix_fmt = (meta.get('pix_fmt') or '').lower()
    is_10bit = 'p010' in pix_fmt or 'yuv420p10' in pix_fmt or 'yuv444p10' in pix_fmt
    if vcopy:
        video_enc_args = ["-c:v", "copy"]
    else:
        hw_enc = _detect_hw_encoder()
        qp = _adaptive_qp()
        crf = _adaptive_crf()
        if hw_enc == 'h264_vaapi':
            src = vcodec.replace('h265', 'hevc')
            sw, sh = _compute_vaapi_scale(vid_w, vid_h)
            if src in _detect_vaapi_decode_codecs():
                # Full HW pipeline: GPU decode → GPU encode (zero-copy, VAAPI handles 10-bit natively)
                hw_pre = ['-hwaccel', 'vaapi', '-hwaccel_device', RENDER_NODE,
                          '-hwaccel_output_format', 'vaapi']
                if sw:
                    video_enc_args = ["-vf", "scale_vaapi=w=%d:h=%d" % (sw, sh),
                                      "-c:v", "h264_vaapi", "-qp", str(qp)]
                else:
                    video_enc_args = ["-c:v", "h264_vaapi", "-qp", str(qp)]
                log.info("transcode encode: %s → h264_vaapi (full HW%s, qp=%d)", vcodec,
                         " scaled %dx%d" % (sw, sh) if sw else "", qp)
            else:
                # CPU decode → GPU encode; use p010le for 10-bit sources
                hw_pre = ['-vaapi_device', RENDER_NODE]
                upload_fmt = 'p010le' if is_10bit else 'nv12'
                if sw:
                    video_enc_args = ["-vf", "scale=%d:%d,format=%s,hwupload" % (sw, sh, upload_fmt),
                                      "-c:v", "h264_vaapi", "-qp", str(qp)]
                else:
                    video_enc_args = ["-vf", "format=%s,hwupload" % upload_fmt,
                                      "-c:v", "h264_vaapi", "-qp", str(qp)]
                log.info("transcode encode: %s → h264_vaapi (CPU dec + GPU enc%s, qp=%d, fmt=%s)", vcodec,
                         " scaled %dx%d" % (sw, sh) if sw else "", qp, upload_fmt)
        elif hw_enc in ('h264_nvenc', 'h264_videotoolbox'):
            video_enc_args = ["-c:v", hw_enc, "-preset", "fast", "-cq", str(qp)]
            log.info("transcode encode: %s → %s (qp=%d)", vcodec, hw_enc, qp)
        else:
            video_enc_args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf)]
            log.info("transcode encode: %s → libx264 (CPU, crf=%d)", vcodec, crf)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    cmd += hw_pre
    if start_sec > 0:
        cmd += ["-ss", str(start_sec)]
    cmd += ["-i", fp]
    # Explicitly map first video + chosen audio to avoid subtitle stream issues
    cmd += ["-map", "0:v:0"]
    if audio_idx is not None:
        cmd += ["-map", "0:%d" % audio_idx]
    else:
        cmd += ["-map", "0:a:0?"]
    cmd += [
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "-"
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        except (OSError, GeneratorExit):
            pass
        finally:
            proc.stdout.close()
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except Exception:
                pass

    return Response(generate(), mimetype="video/mp4",
                    headers={
                        "Cache-Control": "no-cache",
                        "Accept-Ranges": "none",
                        "X-Content-Type-Options": "nosniff",
                    })


# ── HLS endpoints ─────────────────────────────────────────────

@video_station_bp.route("/hls/<int:vid>/start", methods=["POST"])
def hls_start(vid):
    """Start an HLS transcoding session.

    POST body (JSON): {start: seconds, audio: track_index}
    Returns: {ok, session_id}
    """
    _cleanup_stale_hls()

    conn = _get_db()
    r = conn.execute(
        "SELECT path, codec, audio_codec, duration, metadata_json FROM videos WHERE id=?",
        (vid,),
    ).fetchone()
    conn.close()
    if not r:
        return jsonify(error="Nie znaleziono."), 404
    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify(error="Plik nie istnieje."), 404
    if not shutil.which("ffmpeg"):
        return jsonify(error="ffmpeg nie jest zainstalowany."), 500

    data = request.get_json(silent=True) or {}
    start_sec = max(0.0, float(data.get("start") or 0))
    audio_idx = data.get("audio", None)

    # Stop any existing HLS session for this video
    for sid in list(_hls_sessions):
        if _hls_sessions[sid].get("vid") == vid:
            _cleanup_hls(sid)

    # Enforce max concurrent sessions — kill the oldest non-matching session
    while len(_hls_sessions) >= _HLS_MAX_SESSIONS:
        oldest_sid = min(_hls_sessions, key=lambda s: _hls_sessions[s].get('created', 0))
        log.info("HLS session limit reached — killing oldest session %s", oldest_sid)
        _cleanup_hls(oldest_sid)

    vcodec = (r["codec"] or "").lower()
    vcopy = vcodec in ("h264", "vp8", "vp9")
    pre_input_args = ""
    vf_arg = ""
    v_enc_arg = ""

    # Get video dimensions for VAAPI resolution limit check (max 4096x4096 for h264_vaapi)
    meta = json.loads(r["metadata_json"] or '{}')
    vid_w = int(meta.get('width') or 0)
    vid_h = int(meta.get('height') or 0)
    pix_fmt = (meta.get('pix_fmt') or '').lower()
    is_10bit = 'p010' in pix_fmt or 'yuv420p10' in pix_fmt or 'yuv444p10' in pix_fmt

    if vcopy:
        v_enc_arg = "copy"
    else:
        hw_enc = _detect_hw_encoder()
        qp = _adaptive_qp()
        crf = _adaptive_crf()
        if hw_enc == 'h264_vaapi':
            src = vcodec.replace('h265', 'hevc')
            sw, sh = _compute_vaapi_scale(vid_w, vid_h)
            if src in _detect_vaapi_decode_codecs():
                # Full HW pipeline: GPU decode → GPU encode (zero-copy, VAAPI handles 10-bit natively)
                pre_input_args = "-hwaccel vaapi -hwaccel_device %s -hwaccel_output_format vaapi" % RENDER_NODE
                vf_arg = "-vf scale_vaapi=w=%d:h=%d" % (sw, sh) if sw else ""
                v_enc_arg = "h264_vaapi -qp %d" % qp
                log.info("HLS encode: %s → h264_vaapi (full HW%s, qp=%d)", vcodec,
                         " scaled %dx%d" % (sw, sh) if sw else "", qp)
            else:
                # CPU decode → GPU encode; use p010le for 10-bit sources
                pre_input_args = "-vaapi_device %s" % RENDER_NODE
                upload_fmt = 'p010le' if is_10bit else 'nv12'
                if sw:
                    vf_arg = "-vf scale=%d:%d,format=%s,hwupload" % (sw, sh, upload_fmt)
                else:
                    vf_arg = "-vf format=%s,hwupload" % upload_fmt
                v_enc_arg = "h264_vaapi -qp %d" % qp
                log.info("HLS encode: %s → h264_vaapi (CPU dec + GPU enc%s, qp=%d, fmt=%s)", vcodec,
                         " scaled %dx%d" % (sw, sh) if sw else "", qp, upload_fmt)
        elif hw_enc in ('h264_nvenc', 'h264_videotoolbox'):
            v_enc_arg = "%s -preset fast -cq %d" % (hw_enc, qp)
            log.info("HLS encode: %s → %s (qp=%d)", vcodec, hw_enc, qp)
        else:
            v_enc_arg = "libx264 -preset ultrafast -crf %d" % crf
            log.info("HLS encode: %s → libx264 (CPU, crf=%d)", vcodec, crf)

    session_id = "%d_%s" % (vid, os.urandom(4).hex())
    tmpdir = tempfile.mkdtemp(prefix="vs_hls_")

    cmd = "ffmpeg -hide_banner -loglevel error"
    if pre_input_args:
        cmd += " %s" % pre_input_args
    if start_sec > 0:
        cmd += " -ss %s" % start_sec
    cmd += " -i %s" % q(fp)
    cmd += " -map 0:v:0"
    if audio_idx is not None:
        cmd += " -map 0:%d" % int(audio_idx)
    else:
        cmd += " -map 0:a:0?"
    if vf_arg:
        cmd += " %s" % vf_arg
    cmd += " -c:v %s -c:a aac -b:a 192k -ac 2" % v_enc_arg
    cmd += " -f hls -hls_time 4 -hls_list_size 40"
    cmd += " -hls_segment_type mpegts"
    cmd += " -hls_segment_filename %s" % q(os.path.join(tmpdir, "seg%05d.ts"))
    cmd += " -hls_flags delete_segments+append_list"
    cmd += " -y %s" % q(os.path.join(tmpdir, "playlist.m3u8"))

    log.info("HLS start vid=%d ss=%.1f cmd=%s", vid, start_sec, cmd[:200])

    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    _hls_sessions[session_id] = {
        "proc": proc,
        "tmpdir": tmpdir,
        "vid": vid,
        "start_offset": start_sec,
        "duration": r["duration"] or 0,
        "created": time.time(),
        "last_heartbeat": time.time(),
        "client_pos": start_sec,
        "paused": False,
    }

    # Wait for first segment (up to 10s)
    playlist_path = os.path.join(tmpdir, "playlist.m3u8")
    for _ in range(100):
        if os.path.exists(playlist_path) and os.path.getsize(playlist_path) > 20:
            break
        # Check if ffmpeg crashed
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace")[:500] if proc.stderr else ""
            _cleanup_hls(session_id)
            log.error("HLS ffmpeg crashed: %s", stderr)
            return jsonify(error="Transkodowanie nie powiodło się: " + stderr), 500
        time.sleep(0.1)

    return jsonify(ok=True, session_id=session_id, start_offset=start_sec,
                   sub_tracks=json.loads(r["metadata_json"] or '{}').get('sub_tracks', []))


@video_station_bp.route("/hls/<session_id>/playlist.m3u8")
def hls_playlist(session_id):
    """Serve the live HLS playlist written by ffmpeg.

    Relays ffmpeg's sliding-window playlist directly.
    When ffmpeg finishes, appends #EXT-X-ENDLIST so hls.js
    knows the stream ended.
    """
    sess = _hls_sessions.get(session_id)
    if not sess:
        return "", 404
    path = os.path.join(sess["tmpdir"], "playlist.m3u8")
    if not os.path.exists(path):
        return "", 404

    with open(path, "r") as f:
        raw = f.read()

    proc = sess.get("proc")
    finished = proc is None or proc.poll() is not None

    if finished and "#EXT-X-ENDLIST" not in raw:
        raw = raw.rstrip() + "\n#EXT-X-ENDLIST\n"

    resp = Response(raw, mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


@video_station_bp.route("/hls/<session_id>/<filename>")
def hls_segment(session_id, filename):
    """Serve an HLS segment (.ts file).

    With sliding-window playlist, segments are deleted by ffmpeg once they
    fall outside the window.  Only a short wait for the next segment being
    produced right now; old/deleted segments return 404 immediately.
    """
    sess = _hls_sessions.get(session_id)
    if not sess:
        return "", 404

    # Security: only .ts files, no path traversal
    if not filename.endswith(".ts") or "/" in filename or ".." in filename:
        return "", 400

    path = os.path.join(sess["tmpdir"], filename)

    # Short wait for segment being produced right now (up to 15s)
    if not os.path.exists(path):
        proc = sess.get("proc")
        for _ in range(150):
            if os.path.exists(path):
                break
            if proc and proc.poll() is not None:
                return "", 404
            time.sleep(0.1)
        else:
            return "", 404
        time.sleep(0.05)

    resp = send_file(path, mimetype="video/MP2T")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@video_station_bp.route("/hls/<session_id>/stop", methods=["POST"])
def hls_stop_session(session_id):
    """Stop an HLS transcoding session and clean up."""
    _cleanup_hls(session_id)
    return jsonify(ok=True)


@video_station_bp.route("/hls/<session_id>/heartbeat", methods=["POST"])
def hls_heartbeat(session_id):
    """Client keepalive — update last_heartbeat and current playback position.

    POST body: {pos: seconds}
    Must be called every ~8s while the player is active.
    If no heartbeat for _HLS_HEARTBEAT_TIMEOUT seconds, the watchdog kills ffmpeg.
    """
    sess = _hls_sessions.get(session_id)
    if not sess:
        return jsonify(ok=False, error="Session not found"), 404
    data = request.get_json(silent=True) or {}
    if isinstance(data, str):
        # Defensive: body was double-serialized by client (JSON.stringify in api())
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    sess['last_heartbeat'] = time.time()
    pos = float(data.get('pos', sess.get('client_pos', 0)))
    sess['client_pos'] = pos
    paused = sess.get('paused', False)
    return jsonify(ok=True, paused=paused, client_pos=pos)


@video_station_bp.route("/hls/encoder-info", methods=["GET"])
def hls_encoder_info():
    """Return current HW encoder in use (for player stats overlay)."""
    enc = _detect_hw_encoder()
    hw = enc != 'libx264'
    libva_driver_name = os.environ.get('LIBVA_DRIVER_NAME', '')
    return jsonify(
        encoder=enc,
        type='hw' if hw else 'sw',
        label='GPU (%s)' % enc if hw else 'CPU (libx264)',
        tooltip=('Intel QuickSync (iHD) — Aktywny' if 'vaapi' in enc else enc) if hw else 'libx264 (CPU) — akceleracja GPU niedostępna',
        libva_driver_name=libva_driver_name,
    )


@video_station_bp.route("/hw-health", methods=["GET"])
def hw_health():
    """Detailed HW acceleration health report.

    Returns status, diagnostic info, and step-by-step setup instructions
    when HW acceleration is not working. The frontend uses this to show
    a warning banner and setup wizard modal.
    """
    return jsonify(_hw_health_check())


@video_station_bp.route("/vainfo-test", methods=["GET"])
def vainfo_test():
    """Run vainfo against /dev/dri/renderD128 and return parsed result.

    Returns:
      ok                : bool
      output            : raw vainfo stdout+stderr
      profiles          : list of detected VAProfile/VAEntrypoint strings
      error_code        : "IHD_INIT_FAILED" | "PERMISSION_DENIED" | "MISSING_DRIVER" |
                          "NO_RENDER_NODE" | "MISSING_VAINFO" | "TIMEOUT" | ""
      error             : human-readable error string (only on failure)
      libva_driver_name : value of LIBVA_DRIVER_NAME env var
      libva_correct     : bool — LIBVA_DRIVER_NAME == 'ihd'
    """
    RENDER_NODE = '/dev/dri/renderD128'
    libva_driver_name = os.environ.get('LIBVA_DRIVER_NAME', '')
    libva_correct = libva_driver_name.lower() == 'ihd'

    if not shutil.which('vainfo'):
        return jsonify(ok=False, output='', profiles=[], error_code='MISSING_VAINFO',
                       error='Polecenie vainfo nie jest zainstalowane. '
                             'Uruchom: sudo apt install vainfo',
                       libva_driver_name=libva_driver_name, libva_correct=libva_correct)
    if not os.path.exists(RENDER_NODE):
        return jsonify(ok=False, output='', profiles=[], error_code='NO_RENDER_NODE',
                       error='Węzeł %s nie istnieje.' % RENDER_NODE,
                       libva_driver_name=libva_driver_name, libva_correct=libva_correct)
    try:
        r = subprocess.run(
            ['vainfo', '--display', 'drm', '--device', RENDER_NODE],
            capture_output=True, timeout=10, text=True
        )
        combined = (r.stdout or '') + (r.stderr or '')
        ok = r.returncode == 0 and 'VAEntrypoint' in combined

        # Parse supported profiles
        profiles = []
        for line in combined.splitlines():
            m = re.search(r'(VAProfile\w+)\s*/\s*(VAEntrypoint\w+)', line)
            if m:
                profiles.append('%s / %s' % (m.group(1), m.group(2)))

        if ok:
            return jsonify(ok=True, output=combined, profiles=profiles, error_code='', error='',
                           libva_driver_name=libva_driver_name, libva_correct=libva_correct)

        # Classify the failure
        if ('iHD_drv_video.so init failed' in combined
                or 'Failed to open the given device' in combined
                or ('init failed' in combined and 'iHD' in combined)):
            error_code = 'IHD_INIT_FAILED'
            error_msg = ('iHD_drv_video.so init failed — brakujące zależności lub LIBVA_DRIVER_NAME. '
                         'Uruchom: sudo apt install intel-media-va-driver-non-free libmfx1 libmfx-gen1 libva-drm2')
        elif 'Permission denied' in combined or 'permission denied' in combined:
            error_code = 'PERMISSION_DENIED'
            error_msg = ('Brak dostępu do /dev/dri/renderD128. '
                         'Uruchom: sudo usermod -aG render,video $USER')
        elif 'va_openDriver() returns -1' in combined:
            error_code = 'MISSING_DRIVER'
            error_msg = 'Brak sterownika VAAPI. Uruchom: sudo apt install intel-media-va-driver-non-free'
        else:
            err_m = re.search(r'(error|failed)[^\n]*', combined, re.I)
            error_code = 'UNKNOWN'
            error_msg = err_m.group(0) if err_m else ('vainfo błąd (kod %d)' % r.returncode)

        return jsonify(ok=False, output=combined, profiles=[], error_code=error_code, error=error_msg,
                       libva_driver_name=libva_driver_name, libva_correct=libva_correct)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, output='', profiles=[], error_code='TIMEOUT',
                       error='Timeout — vainfo nie odpowiedział w 10s.',
                       libva_driver_name=libva_driver_name, libva_correct=libva_correct)
    except Exception as exc:
        return jsonify(ok=False, output='', profiles=[], error_code='EXCEPTION',
                       error=str(exc), libva_driver_name=libva_driver_name, libva_correct=libva_correct)


@video_station_bp.route("/gpu-retest", methods=["POST"])
def gpu_retest():
    """Reset HW encoder cache and re-probe GPU capabilities via vainfo + ffmpeg.

    Returns:
      ok                : bool — vainfo succeeded
      hw_encoder        : str  — newly detected best encoder
      is_hw             : bool
      error_code        : str  — IHD_INIT_FAILED | PERMISSION_DENIED | etc.
      profiles          : list — VAProfile/VAEntrypoint strings
      codecs            : dict — {h264, hevc, vp9, av1}: bool
      libva_driver_name : str
      libva_correct     : bool
    """
    global _HW_ENCODER, _VAAPI_DECODE_CODECS
    _HW_ENCODER = None          # reset encoder cache to force re-detection
    _VAAPI_DECODE_CODECS = None  # reset decoder cache to force re-detection

    RENDER_NODE = '/dev/dri/renderD128'
    libva_driver_name = os.environ.get('LIBVA_DRIVER_NAME', '')
    libva_correct = libva_driver_name.lower() == 'ihd'

    # ── vainfo probe ────────────────────────────────────────────────────
    vainfo_ok = False
    profiles = []
    error_code = ''
    vainfo_output = ''

    if shutil.which('vainfo') and os.path.exists(RENDER_NODE):
        try:
            vr = subprocess.run(
                ['vainfo', '--display', 'drm', '--device', RENDER_NODE],
                capture_output=True, timeout=10, text=True
            )
            vainfo_output = (vr.stdout or '') + (vr.stderr or '')
            vainfo_ok = vr.returncode == 0 and 'VAEntrypoint' in vainfo_output
            for line in vainfo_output.splitlines():
                m = re.search(r'(VAProfile\w+)\s*/\s*(VAEntrypoint\w+)', line)
                if m:
                    profiles.append('%s / %s' % (m.group(1), m.group(2)))
            if not vainfo_ok:
                if ('iHD_drv_video.so init failed' in vainfo_output
                        or 'Failed to open the given device' in vainfo_output
                        or ('init failed' in vainfo_output and 'iHD' in vainfo_output)):
                    error_code = 'IHD_INIT_FAILED'
                elif 'Permission denied' in vainfo_output or 'permission denied' in vainfo_output:
                    error_code = 'PERMISSION_DENIED'
                elif 'va_openDriver() returns -1' in vainfo_output:
                    error_code = 'MISSING_DRIVER'
                else:
                    error_code = 'UNKNOWN'
        except subprocess.TimeoutExpired:
            error_code = 'TIMEOUT'
        except Exception:
            error_code = 'EXCEPTION'

    # ── per-codec ffmpeg probe (encode) ─────────────────────────────────
    codecs = {}
    if shutil.which('ffmpeg') and os.path.exists(RENDER_NODE):
        _base = ['-vaapi_device', RENDER_NODE, '-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.1',
                 '-vf', 'format=nv12,hwupload']
        codec_tests = [
            ('h264', _base + ['-c:v', 'h264_vaapi', '-f', 'null', '-']),
            ('hevc', _base + ['-c:v', 'hevc_vaapi', '-f', 'null', '-']),
            ('vp9',  _base + ['-c:v', 'vp9_vaapi',  '-f', 'null', '-']),
            ('av1',  _base + ['-c:v', 'av1_vaapi',  '-f', 'null', '-']),
        ]
        for codec_name, args in codec_tests:
            try:
                cr = subprocess.run(
                    ['ffmpeg', '-hide_banner', '-loglevel', 'error'] + args,
                    capture_output=True, timeout=6
                )
                codecs[codec_name] = cr.returncode == 0
            except Exception:
                codecs[codec_name] = False

    # ── decode capability (from vainfo VAEntrypointVLD profiles) ─────────
    dec_set = _detect_vaapi_decode_codecs()
    decode_codecs = {c: (c in dec_set) for c in ('h264', 'hevc', 'vp9', 'av1')}

    new_encoder = _detect_hw_encoder()
    return jsonify(
        ok=vainfo_ok,
        hw_encoder=new_encoder,
        is_hw=new_encoder != 'libx264',
        error_code=error_code,
        profiles=profiles,
        codecs=codecs,
        decode_codecs=decode_codecs,
        libva_driver_name=libva_driver_name,
        libva_correct=libva_correct,
        tooltip=('Intel QuickSync (iHD) — Aktywny' if vainfo_ok else
                 'libx264 (CPU) — akceleracja GPU niedostępna'),
    )



@video_station_bp.route("/hw-install", methods=["GET", "POST"])
@admin_required
def hw_install():
    """Auto-install VAAPI drivers for Intel GPUs, streamed as SSE.

    Streams progress lines as:
      data: {"line": "...", "done": false}
    Final event:
      data: {"done": true, "ok": true|false, "hw_encoder": "..."}
    """
    from flask import Response

    def _generate():
        global _HW_ENCODER

        def _send(line, done=False, **kw):
            import json
            payload = {"line": line, "done": done}
            payload.update(kw)
            return "data: %s\n\n" % json.dumps(payload)

        try:
            yield _send("🔍 Wykrywanie systemu...")
            # Check if we can use non-free
            sources = ""
            try:
                sources = open("/etc/apt/sources.list").read()
            except Exception:
                pass

            if "non-free" not in sources:
                yield _send("📦 Włączanie repozytorium non-free...")
                r = host_run(
                    "sed -i 's/main$/main contrib non-free non-free-firmware/g' /etc/apt/sources.list",
                    timeout=10
                )
                if r.returncode != 0:
                    yield _send("⚠️  Nie udało się edytować sources.list (kontynuuję...)")

            # Check if Intel media driver is too old for newer GPUs (N150, N200, etc.)
            installed_ver = _get_intel_media_driver_version()
            if _intel_driver_needs_ppa_upgrade(installed_ver):
                yield _send("⚠️  Wykryto stary sterownik Intel Media Driver (%s < 25.x)." % installed_ver)
                yield _send("📌 Dodawanie Intel Graphics PPA (kobuk-team) dla nowszych GPU...")

                ppa_key_url = (
                    "https://keyserver.ubuntu.com/pks/lookup"
                    "?op=get&search=0x0C0E6AF955CE463C03FC51574D098D70AFBE5E1F"
                )
                key_path = "/etc/apt/trusted.gpg.d/kobuk-intel-graphics.gpg"
                r = host_run(
                    "curl -fsSL %s | gpg --dearmor -o %s" % (q(ppa_key_url), q(key_path)),
                    timeout=20
                )
                if r.returncode != 0:
                    yield _send("⚠️  Nie udało się pobrać klucza GPG PPA (kontynuuję bez PPA...)")
                else:
                    ppa_line = (
                        "deb https://ppa.launchpadcontent.net/kobuk-team/intel-graphics/ubuntu noble main"
                    )
                    r = host_run(
                        "echo %s > /etc/apt/sources.list.d/intel-graphics.list" % q(ppa_line),
                        timeout=5
                    )
                    yield _send("✅ Dodano Intel Graphics PPA.")

            yield _send("🔄 Aktualizacja listy pakietów (apt update)...")
            for line in host_run_stream("apt-get update -qq 2>&1"):
                yield _send(line.rstrip())

            yield _send("📥 Instalacja sterowników VAAPI...")
            pkgs = "intel-media-va-driver-non-free libigdgmm12 libva2 libva-drm2 libvpl2 i965-va-driver vainfo"
            for line in host_run_stream(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends %s 2>&1" % pkgs
            ):
                yield _send(line.rstrip())

            # Add current user to render+video groups
            run_user = os.environ.get('SUDO_USER', '') or os.environ.get('USER', '') or 'ethos'
            yield _send("👤 Dodawanie użytkownika '%s' do grup render i video..." % run_user)
            host_run("usermod -aG render,video %s 2>&1 || true" % q(run_user), timeout=10)

            yield _send("✅ Sterowniki zainstalowane. Testuję VAAPI...")

            # Reset encoder cache and re-detect
            _HW_ENCODER = None
            new_encoder = _detect_hw_encoder()
            if new_encoder != 'libx264':
                yield _send(
                    "🎉 Akceleracja sprzętowa aktywna! Enkoder: %s" % new_encoder,
                    done=True, ok=True, hw_encoder=new_encoder
                )
            else:
                yield _send(
                    "ℹ️  VAAPI zainstalowane — restart serwisu wymagany do aktywacji.",
                    done=True, ok=True, hw_encoder='libx264', restart_required=True
                )
        except Exception as exc:
            yield _send("❌ Błąd: %s" % str(exc), done=True, ok=False, hw_encoder='libx264')

    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@video_station_bp.route("/watched/<int:vid>", methods=["POST"])
def update_watched(vid):
    d = request.json or {}
    conn = _get_db()
    r = conn.execute("SELECT id FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    watched = 1 if d.get("watched", False) else 0
    position = float(d.get("position", 0))
    now = time.time()
    last_watched = now if watched else 0
    conn.execute(
        "INSERT INTO watch_state (video_id,watched,position,updated_at,last_watched_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET "
        "watched=excluded.watched,position=excluded.position,updated_at=excluded.updated_at,"
        "last_watched_at=CASE WHEN excluded.watched=1 THEN excluded.last_watched_at "
        "ELSE watch_state.last_watched_at END",
        (vid, watched, position, now, last_watched))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@video_station_bp.route("/rescan-metadata", methods=["POST"])
@admin_required
def rescan_metadata():
    """Re-probe all videos with empty codec metadata (fixes broken scans)."""
    if not _all_deps_ok():
        return jsonify({"error": "Brak ffmpeg/ffprobe."}), 400
    force_all = request.json and request.json.get("all", False)
    conn = _get_db()
    if force_all:
        rows = conn.execute("SELECT id, path FROM videos").fetchall()
    else:
        rows = conn.execute(
            "SELECT id, path FROM videos WHERE codec IS NULL OR codec = ''").fetchall()
    updated = 0
    for r in rows:
        path = r["path"]
        if not os.path.isfile(path):
            continue
        meta = _probe_video(path)
        if meta.get("codec"):
            conn.execute(
                "UPDATE videos SET codec=?, audio_codec=?, duration=?, width=?, "
                "height=?, bitrate=?, metadata_json=? WHERE id=?",
                (meta.get("codec", ""), meta.get("audio_codec", ""),
                 meta.get("duration", 0), meta.get("width", 0),
                 meta.get("height", 0), meta.get("bitrate", 0),
                 json.dumps(meta), r["id"]))
            updated += 1
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "updated": updated, "total": len(rows)})


@video_station_bp.route("/remove/<int:vid>", methods=["POST"])
@admin_required
def remove_from_library(vid):
    """Remove video from library without deleting the file."""
    conn = _get_db()
    r = conn.execute("SELECT id FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    conn.execute("DELETE FROM watch_state WHERE video_id=?", (vid,))
    conn.execute("DELETE FROM videos WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    # Remove thumbnail and poster
    for d in (_THUMB_DIR, _POSTER_DIR):
        p = os.path.join(d, str(vid) + '.jpg')
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return jsonify({"ok": True})


@video_station_bp.route("/delete/<int:vid>", methods=["POST"])
@admin_required
def delete_from_disk(vid):
    """Remove video from library AND permanently delete the file from disk."""
    conn = _get_db()
    r = conn.execute("SELECT id, path FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    file_path = r["path"]
    conn.execute("DELETE FROM watch_state WHERE video_id=?", (vid,))
    conn.execute("DELETE FROM videos WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    for d in (_THUMB_DIR, _POSTER_DIR, _BACKDROP_DIR):
        p = os.path.join(d, str(vid) + '.jpg')
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    deleted = False
    if file_path and os.path.isfile(file_path):
        try:
            os.remove(file_path)
            deleted = True
        except OSError as e:
            return jsonify({"error": "Usunięto z bazy, ale nie można usunąć pliku: " + str(e)}), 500
    return jsonify({"ok": True, "deleted": deleted, "path": file_path})


@video_station_bp.route("/parse-title/<int:vid>", methods=["GET"])
@admin_required
def parse_title(vid):
    """Parse video filename into a clean title and year for TMDb search."""
    conn = _get_db()
    r = conn.execute("SELECT filename FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    title, year = _parse_filename(r["filename"])
    return jsonify({"title": title, "year": year, "filename": r["filename"]})


# ── batch operations ───────────────────────────────────────────
@video_station_bp.route("/batch", methods=["POST"])
@admin_required
def batch_action():
    """Batch operations on multiple videos: watched, unwatched, remove, hide, unhide."""
    d = request.json or {}
    ids = d.get("ids", [])
    action = d.get("action", "")
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "Brak wybranych filmów."}), 400
    if action not in ("watched", "unwatched", "remove", "hide", "unhide"):
        return jsonify({"error": "Nieznana akcja."}), 400
    conn = _get_db()
    placeholders = ",".join("?" * len(ids))
    if action == "watched":
        now = time.time()
        for vid in ids:
            conn.execute(
                "INSERT INTO watch_state (video_id,watched,position,updated_at) "
                "VALUES (?,1,0,?) ON CONFLICT(video_id) DO UPDATE SET "
                "watched=1, updated_at=?", (vid, now, now))
    elif action == "unwatched":
        now = time.time()
        for vid in ids:
            conn.execute(
                "INSERT INTO watch_state (video_id,watched,position,updated_at) "
                "VALUES (?,0,0,?) ON CONFLICT(video_id) DO UPDATE SET "
                "watched=0, position=0, updated_at=?", (vid, now, now))
    elif action == "remove":
        rows = conn.execute("SELECT id FROM videos WHERE id IN (%s)" % placeholders, ids).fetchall()
        for r in rows:
            conn.execute("DELETE FROM watch_state WHERE video_id=?", (r["id"],))
            conn.execute("DELETE FROM videos WHERE id=?", (r["id"],))
            for d_dir in (_THUMB_DIR, _POSTER_DIR):
                p = os.path.join(d_dir, str(r["id"]) + '.jpg')
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
    elif action == "hide":
        conn.execute("UPDATE videos SET hidden=1 WHERE id IN (%s)" % placeholders, ids)
    elif action == "unhide":
        conn.execute("UPDATE videos SET hidden=0 WHERE id IN (%s)" % placeholders, ids)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "count": len(ids)})


# ── hide password management ──────────────────────────────────
@video_station_bp.route("/hide-status", methods=["GET"])
def hide_status():
    """Check if hide password is set and if current session is unlocked."""
    return jsonify({
        "password_set": _hide_pw_is_set(),
        "unlocked": _is_hidden_unlocked(),
        "hidden_count": _count_hidden(),
    })


def _count_hidden():
    try:
        conn = _get_db()
        n = conn.execute("SELECT COUNT(*) FROM videos WHERE COALESCE(hidden,0)=1").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


@video_station_bp.route("/hide-password", methods=["POST"])
@admin_required
def hide_password_set():
    """Set or change the hide password."""
    d = request.json or {}
    password = d.get("password", "")
    old_password = d.get("old_password", "")
    if not password or len(password) < 4:
        return jsonify({"error": "Hasło musi mieć co najmniej 4 znaki."}), 400
    pw_data = _load_hide_pw()
    if pw_data.get("hash"):
        if not old_password or not _verify_pw(old_password, pw_data["hash"]):
            return jsonify({"error": "Nieprawidłowe obecne hasło."}), 403
    pw_data["hash"] = _hash_pw(password)
    _save_hide_pw(pw_data)
    return jsonify({"ok": True})


@video_station_bp.route("/hide-password", methods=["DELETE"])
@admin_required
def hide_password_remove():
    """Remove hide password and unhide all videos."""
    d = request.json or {}
    password = d.get("password", "")
    pw_data = _load_hide_pw()
    if not pw_data.get("hash"):
        return jsonify({"error": "Hasło nie jest ustawione."}), 400
    if not _verify_pw(password, pw_data["hash"]):
        return jsonify({"error": "Nieprawidłowe hasło."}), 403
    _save_hide_pw({})
    conn = _get_db()
    conn.execute("UPDATE videos SET hidden=0 WHERE hidden=1")
    conn.commit()
    conn.close()
    with _hide_lock:
        _hide_unlocked.clear()
    return jsonify({"ok": True})


@video_station_bp.route("/hide-unlock", methods=["POST"])
def hide_unlock():
    """Unlock hidden videos for the current session."""
    d = request.json or {}
    password = d.get("password", "")
    pw_data = _load_hide_pw()
    if not pw_data.get("hash"):
        return jsonify({"error": "Hasło nie jest ustawione."}), 400

    token = _get_token()
    now = time.time()

    # Brute-force protection
    with _hide_lock:
        att = _hide_attempts.get(token)
        if att and now < att.get("locked_until", 0):
            remaining = int(att["locked_until"] - now)
            return jsonify({"error": "Zbyt wiele prób. Odczekaj %ds." % remaining}), 429
        if att and now - att.get("first", now) > _HIDE_ATTEMPT_WINDOW:
            _hide_attempts.pop(token, None)

    if not _verify_pw(password, pw_data["hash"]):
        with _hide_lock:
            att = _hide_attempts.get(token, {"count": 0, "first": now, "locked_until": 0})
            att["count"] += 1
            if att["count"] >= _HIDE_MAX_ATTEMPTS:
                att["locked_until"] = now + _HIDE_LOCKOUT_TIME
                att["count"] = 0
            _hide_attempts[token] = att
        return jsonify({"error": "Nieprawidłowe hasło."}), 403

    with _hide_lock:
        _hide_attempts.pop(token, None)
        _hide_unlocked[token] = True
    return jsonify({"ok": True})


@video_station_bp.route("/hide-lock", methods=["POST"])
def hide_lock_session():
    """Re-lock hidden videos for the current session."""
    token = _get_token()
    with _hide_lock:
        _hide_unlocked.pop(token, None)
    return jsonify({"ok": True})


@video_station_bp.route("/subtitles/<int:vid>", methods=["GET"])

def subtitles(vid):
    """Find subtitle files (.srt, .ass, .ssa, .vtt) next to the video file."""
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    video_path = r["path"]
    base = os.path.splitext(video_path)[0]
    video_dir = os.path.dirname(video_path)
    video_stem = os.path.splitext(os.path.basename(video_path))[0]
    sub_exts = {'.srt', '.ass', '.ssa', '.vtt'}
    subs = []
    if os.path.isdir(video_dir):
        for fn in os.listdir(video_dir):
            fext = os.path.splitext(fn)[1].lower()
            if fext in sub_exts and fn.lower().startswith(video_stem.lower()):
                # Extract language tag from filename like "movie.en.srt"
                parts = os.path.splitext(fn)[0].split('.')
                lang = parts[-1] if len(parts) > 1 and len(parts[-1]) <= 3 else ''
                subs.append({
                    "filename": fn,
                    "path": os.path.join(video_dir, fn),
                    "language": lang,
                    "format": fext[1:],
                })
    return jsonify({"ok": True, "subtitles": subs})


@video_station_bp.route("/subtitle-file/<int:vid>/<path:filename>", methods=["GET"])

def subtitle_file(vid, filename):
    """Serve a subtitle file. Converts SRT to VTT for browser compatibility."""
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    video_dir = os.path.dirname(r["path"])
    sub_path = os.path.realpath(os.path.join(video_dir, filename))
    # Ensure path is within the video directory
    if not sub_path.startswith(os.path.realpath(video_dir) + os.sep):
        return jsonify({"error": "Niedozwolona ścieżka."}), 403
    if not os.path.isfile(sub_path):
        return jsonify({"error": "Plik napisów nie istnieje."}), 404

    ext = os.path.splitext(sub_path)[1].lower()
    if ext == '.vtt':
        return send_file(sub_path, mimetype="text/vtt")

    # Convert SRT → VTT on-the-fly
    if ext == '.srt':
        try:
            with open(sub_path, 'r', encoding='utf-8', errors='replace') as f:
                srt_content = f.read()
            vtt = "WEBVTT\n\n" + srt_content.replace(',', '.')
            return Response(vtt, mimetype="text/vtt")
        except Exception:
            return jsonify({"error": "Błąd odczytu napisów."}), 500

    return send_file(sub_path, mimetype="text/plain")


@video_station_bp.route("/embedded-subs/<int:vid>/<int:track>", methods=["GET"])
def embedded_subs(vid, track):
    """Extract an embedded subtitle stream from a video file to WebVTT on demand.

    Uses ffmpeg to extract the subtitle stream at the given stream index.
    Result is cached in /tmp for the session.
    """
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify({"error": "Plik nie istnieje."}), 404
    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg nie jest zainstalowany."}), 500

    cache_path = os.path.join(tempfile.gettempdir(), "vs_sub_%d_%d.vtt" % (vid, track))
    if not os.path.isfile(cache_path):
        try:
            cmd = "ffmpeg -hide_banner -loglevel error -i %s -map 0:%d -c:s webvtt -f webvtt %s -y" % (
                q(fp), track, q(cache_path))
            res = host_run(cmd, timeout=60)
            if res.returncode != 0 or not os.path.isfile(cache_path):
                return jsonify({"error": "Błąd ekstrakcji napisów."}), 500
        except Exception as e:
            return jsonify({"error": "Błąd: " + str(e)}), 500

    return send_file(cache_path, mimetype="text/vtt")


@video_station_bp.route("/watcher-status", methods=["GET"])
def watcher_status():
    """Return file-watcher state (watched folders and their last-seen mtimes)."""
    return jsonify({
        "ok": True,
        "watched": [
            {"folder": f, "last_mtime": mt}
            for f, mt in _watcher_state.items()
        ],
        "scanning": _scan_state.get("running", False),
    })


@video_station_bp.route("/scan-folder", methods=["POST"])
@admin_required
def scan_folder():
    """Trigger an incremental scan of a specific folder path.

    POST body: {folder: "/path/to/folder", use_tmdb: bool}
    """
    import gevent
    data = request.get_json(silent=True) or {}
    folder = data.get("folder", "").strip()
    if not folder:
        return jsonify({"error": "Brak parametru folder."}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": "Folder nie istnieje."}), 404
    if _scan_state.get("running"):
        return jsonify({"error": "Skanowanie już w toku."}), 409
    use_tmdb = bool(data.get("use_tmdb", False))
    _scan_state.update(running=True, stop_requested=False,
                       total=0, processed=0, current_file="")
    gevent.spawn(_scan_worker, [folder], use_tmdb)
    return jsonify({"ok": True, "message": "Skanowanie folderu w tle..."})




@video_station_bp.route("/tmdb-config", methods=["GET"])
def tmdb_config_get():
    key = _load_tmdb_key()
    return jsonify({
        "has_key": bool(key),
        "key_preview": key[:4] + '***' + key[-4:] if len(key) > 8 else ('***' if key else ''),
    })


@video_station_bp.route("/tmdb-config", methods=["POST"])
@admin_required
def tmdb_config_save():
    key = (request.json or {}).get("api_key", "").strip()
    if not key:
        return jsonify({"error": "Brak klucza API."}), 400
    # Validate the key with a test request
    try:
        url = _TMDB_BASE + '/configuration?api_key=' + urllib.parse.quote(key)
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return jsonify({"error": "Klucz API nieprawidlowy."}), 400
    except urllib.error.HTTPError:
        return jsonify({"error": "Klucz API nieprawidlowy."}), 400
    except Exception as e:
        return jsonify({"error": "Blad weryfikacji: " + str(e)}), 500
    _save_tmdb_key(key)
    return jsonify({"ok": True})


@video_station_bp.route("/tmdb-match/<int:vid>", methods=["POST"])

def tmdb_match_one(vid):
    conn = _get_db()
    r = conn.execute("SELECT id, filename FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    api_key = _load_tmdb_key()
    if not api_key:
        conn.close()
        return jsonify({"error": "Brak klucza TMDb. Skonfiguruj w ustawieniach."}), 400
    result = _tmdb_match_video(conn, r["id"], r["filename"], api_key)
    conn.close()
    if not result:
        return jsonify({"error": "Nie znaleziono dopasowania w TMDb."}), 404
    return jsonify({"ok": True, "match": result})


# Rate limit for tmdb-match-all (once per 60s)
_last_tmdb_match_all = 0


@video_station_bp.route("/tmdb-match-all", methods=["POST"])
@admin_required
def tmdb_match_all():
    global _last_tmdb_match_all
    api_key = _load_tmdb_key()
    if not api_key:
        return jsonify({"error": "Brak klucza TMDb."}), 400
    if _scan_state["running"]:
        return jsonify({"error": "Skan juz trwa, poczekaj az sie skonczy."}), 409
    now = time.time()
    if now - _last_tmdb_match_all < 60:
        remaining = int(60 - (now - _last_tmdb_match_all))
        return jsonify({"error": "Poczekaj %ds przed kolejnym dopasowaniem." % remaining}), 429
    _last_tmdb_match_all = now
    import gevent

    def _do_match():
        _scan_state.update(running=True, stop_requested=False,
                           total=0, processed=0, current_file='')
        conn = _get_db()
        unmatched = conn.execute(
            "SELECT id, filename FROM videos "
            "WHERE (tmdb_id=0 OR tmdb_id IS NULL) AND COALESCE(hidden,0)=0"
        ).fetchall()
        _scan_state['total'] = len(unmatched)
        _emit_progress()
        for i, row in enumerate(unmatched):
            if _scan_state.get('stop_requested'):
                break
            _scan_state['current_file'] = row['filename']
            _scan_state['processed'] = i + 1
            try:
                _tmdb_match_video(conn, row['id'], row['filename'], api_key)
            except Exception as e:
                log.debug('TMDb match-all failed for %s: %s', row['filename'], e)
            if (i + 1) % 3 == 0:
                _emit_progress()
            gevent.sleep(0.3)
        conn.close()
        _scan_state.update(running=False, stop_requested=False, current_file='')
        _emit_progress()
        s = _sio()
        if s:
            s.emit('vs_scan_done', {
                'total_processed': _scan_state['processed'],
                'duration': 0, 'message': 'Dopasowanie TMDb zakonczone.'
            })

    gevent.spawn(_do_match)
    return jsonify({"ok": True})


@video_station_bp.route("/tmdb-search-list", methods=["GET"])

def tmdb_search_list():
    """Search TMDb and return a list of results for manual selection."""
    query = request.args.get("q", "").strip()
    year  = request.args.get("year", "").strip()
    if not query:
        return jsonify({"results": []})
    api_key = _load_tmdb_key()
    if not api_key:
        return jsonify({"error": "Brak klucza TMDb. Skonfiguruj w ustawieniach."}), 400
    results = []
    for endpoint, media_type in [('/search/movie', 'movie'), ('/search/tv', 'tv')]:
        try:
            params = {'api_key': api_key, 'query': query, 'language': 'pl-PL', 'page': 1}
            if year:
                params['year' if media_type == 'movie' else 'first_air_date_year'] = year
            url = _TMDB_BASE + endpoint + '?' + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            for r in (data.get('results') or [])[:5]:
                title = r.get('title' if media_type == 'movie' else 'name', '')
                date = r.get('release_date' if media_type == 'movie' else 'first_air_date', '') or ''
                results.append({
                    'tmdb_id': r.get('id', 0),
                    'title': title,
                    'year': date[:4],
                    'overview': (r.get('overview', '') or '')[:200],
                    'poster_path': r.get('poster_path', ''),
                    'rating': round(r.get('vote_average', 0), 1),
                    'media_type': media_type,
                })
        except Exception as e:
            log.debug('tmdb_search_list %s failed: %s', endpoint, e)
    results.sort(key=lambda x: x.get('rating', 0), reverse=True)
    return jsonify({"results": results[:10]})


@video_station_bp.route("/tmdb-apply/<int:vid>", methods=["POST"])

def tmdb_apply(vid):
    """Apply a specific TMDb result (chosen by user) to a video."""
    d = request.json or {}
    tmdb_id = d.get("tmdb_id")
    media_type = d.get("type", "movie")
    if not tmdb_id:
        return jsonify({"error": "Brak tmdb_id."}), 400
    if media_type not in ('movie', 'tv'):
        return jsonify({"error": "Nieprawidłowy typ."}), 400
    api_key = _load_tmdb_key()
    if not api_key:
        return jsonify({"error": "Brak klucza TMDb."}), 400
    conn = _get_db()
    r = conn.execute("SELECT id FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    try:
        url = '%s/%s/%d?api_key=%s&language=pl-PL' % (
            _TMDB_BASE, media_type, int(tmdb_id), urllib.parse.quote(api_key))
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            details = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        conn.close()
        return jsonify({"error": "Błąd pobierania z TMDb: " + str(e)}), 500
    title_key = 'title' if media_type == 'movie' else 'name'
    date_key = 'release_date' if media_type == 'movie' else 'first_air_date'
    tmdb_title = details.get(title_key, '')
    tmdb_year = (details.get(date_key, '') or '')[:4]
    tmdb_genres = ','.join(str(g['id']) for g in (details.get('genres') or []))
    poster_path = details.get('poster_path', '')
    backdrop_path = details.get('backdrop_path', '')
    rating = details.get('vote_average', 0)
    overview = details.get('overview', '')
    cast, director = _fetch_tmdb_credits(tmdb_id, media_type, api_key)
    conn.execute(
        'UPDATE videos SET tmdb_id=?, tmdb_title=?, tmdb_overview=?, '
        'tmdb_year=?, tmdb_rating=?, tmdb_genres=?, tmdb_poster_path=?, '
        'tmdb_backdrop_path=?, tmdb_cast=?, tmdb_director=?, tmdb_media_type=? WHERE id=?',
        (tmdb_id, tmdb_title, overview, tmdb_year, rating, tmdb_genres,
         poster_path, backdrop_path, cast, director, media_type, vid))
    conn.commit()
    if tmdb_title:
        display = tmdb_title + (' (' + tmdb_year + ')' if tmdb_year else '')
        conn.execute('UPDATE videos SET title=? WHERE id=?', (display, vid))
        conn.commit()
    poster_ok = False
    if poster_path:
        poster_ok = _download_poster(poster_path, vid)
        if poster_ok:
            conn.execute('UPDATE videos SET poster_ok=1 WHERE id=?', (vid,))
            conn.commit()
    backdrop_ok = False
    if backdrop_path:
        backdrop_ok = _download_backdrop(backdrop_path, vid)
        if backdrop_ok:
            conn.execute('UPDATE videos SET backdrop_ok=1 WHERE id=?', (vid,))
            conn.commit()
    conn.close()
    return jsonify({"ok": True, "title": tmdb_title, "year": tmdb_year,
                    "poster_ok": poster_ok, "backdrop_ok": backdrop_ok})


@video_station_bp.route("/rename/<int:vid>", methods=["POST"])
@admin_required
def rename_video(vid):
    """Rename a video file on disk and update DB path/filename."""
    d = request.json or {}
    new_name = (d.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "Brak nowej nazwy."}), 400
    conn = _get_db()
    r = conn.execute(
        "SELECT id, path, filename, folder FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    old_ext = os.path.splitext(r["filename"])[1].lower()
    new_base, new_ext = os.path.splitext(new_name)
    if not new_ext:
        new_name = new_name + old_ext
    elif new_ext.lower() != old_ext:
        conn.close()
        return jsonify({"error": "Nie można zmienić rozszerzenia pliku."}), 400
    folder = r["folder"]
    new_path = os.path.join(folder, new_name)
    try:
        new_path = safe_path(new_path, '/')
        old_path = safe_path(r["path"], '/')
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    if not os.path.isfile(old_path):
        conn.close()
        return jsonify({"error": "Plik nie istnieje na dysku."}), 404
    if os.path.exists(new_path) and new_path != old_path:
        conn.close()
        return jsonify({"error": "Plik o tej nazwie już istnieje."}), 400
    try:
        os.rename(old_path, new_path)
    except OSError as e:
        conn.close()
        return jsonify({"error": "Błąd zmiany nazwy: " + str(e)}), 500
    conn.execute(
        "UPDATE videos SET path=?, filename=? WHERE id=?",
        (new_path, new_name, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "new_path": new_path, "new_name": new_name})
