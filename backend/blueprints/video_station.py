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


def _reset_hw_cache():
    global _HW_ENCODER, _VAAPI_DECODE_CODECS
    _HW_ENCODER = None
    _VAAPI_DECODE_CODECS = None


from blueprints import video_station_library, video_station_streaming, video_station_thumbnails, video_station_tmdb, video_station_extras
