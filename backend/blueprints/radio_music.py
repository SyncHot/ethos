"""
Radio & Music -- internet radio, podcasts, and music player.

Routes:
  GET  /api/radio-music/pkg-status         - dependency status
  POST /api/radio-music/install            - install dependencies
  POST /api/radio-music/uninstall          - cleanup
  GET  /api/radio-music/radio/search       - search radio stations (?q=, ?country=, ?tag=, ?limit=)
  GET  /api/radio-music/radio/countries    - list countries with station counts
  GET  /api/radio-music/radio/tags         - popular genre tags
  GET  /api/radio-music/radio/top          - top voted stations (?limit=)
  GET  /api/radio-music/radio/favorites    - user's saved stations
  POST /api/radio-music/radio/favorites    - add/remove favorite station
  GET  /api/radio-music/radio/stream-url   - resolve stream URL (?url=)
  GET  /api/radio-music/radio/proxy        - proxy stream through server (?url=)
  GET  /api/radio-music/podcasts/search    - search podcasts via iTunes (?q=)
  GET  /api/radio-music/podcasts/feed      - parse podcast RSS feed (?url=)
  GET  /api/radio-music/podcasts/subscriptions - user's subscribed podcasts
  POST /api/radio-music/podcasts/subscribe - subscribe/unsubscribe
  GET  /api/radio-music/music/check-deps   - check if yt-dlp is installed
  POST /api/radio-music/music/install-deps - install yt-dlp
  GET  /api/radio-music/music/search       - search YouTube music (?q=, ?limit=)
  GET  /api/radio-music/music/direct-url   - get direct CDN audio URL for Chromecast (?url=)
  GET  /api/radio-music/music/stream       - proxy audio from YouTube (?url=)
  POST /api/radio-music/music/download     - download track to music folder
  POST /api/radio-music/music/download-playlist - download all tracks in a playlist
  GET  /api/radio-music/music/downloads    - list active/recent downloads
  GET  /api/radio-music/local/folders      - list configured music folders
  POST /api/radio-music/archive/start      - start archiving a YT track to NAS offline-archive/
  POST /api/radio-music/archive/batch      - batch status for a list of YT URLs
  POST /api/radio-music/archive/delete     - delete archived track from NAS
  GET  /api/radio-music/archive/quota      - disk usage of offline archive
  GET  /api/radio-music/archive/file/<key> - stream archived audio file
  POST /api/radio-music/local/folders      - add/remove music folder
  GET  /api/radio-music/local/scan         - scan folders for audio files
  GET  /api/radio-music/local/stream       - stream local audio file (?path=)
  DELETE /api/radio-music/local/file       - delete a single local audio file
  DELETE /api/radio-music/local/folder     - delete a local folder and its contents
  GET  /api/radio-music/playlists           - list user's playlists
  POST /api/radio-music/playlists           - create playlist
  GET  /api/radio-music/playlists/<id>      - get playlist
  PUT  /api/radio-music/playlists/<id>      - update playlist (name, tracks)
  DELETE /api/radio-music/playlists/<id>    - delete playlist
  POST /api/radio-music/playlists/<id>/tracks      - add track to playlist
  DELETE /api/radio-music/playlists/<id>/tracks/<i> - remove track from playlist
  GET  /api/radio-music/history            - recently played items
  POST /api/radio-music/history            - add to history
  GET  /api/radio-music/most-played        - most played items by count
  GET  /api/radio-music/playback-state     - get saved playback state (cross-device resume)
  POST /api/radio-music/playback-state     - save playback state
  GET  /api/radio-music/music/liked       - user's liked songs
  POST /api/radio-music/music/liked       - add/remove liked song
  GET  /api/radio-music/similar-artists   - similar artists via Deezer (?artist=, ?limit=)
  GET  /api/radio-music/recommendations   - personalized recs from history/favorites/subs
  GET  /api/radio-music/lyrics             - fetch song lyrics (?title=, ?artist=)
  GET  /api/radio-music/search/all         - unified search across radio+podcasts+local (?q=)
  GET  /api/radio-music/playlists/<id>/export - export playlist as M3U8
  POST /api/radio-music/playlists/import   - import M3U/M3U8 playlist
  GET  /api/radio-music/podcasts/autodownload - get auto-download settings
  POST /api/radio-music/podcasts/autodownload - toggle auto-download for a feed
"""

import http.client
import json
import logging
import mimetypes
import os
import pathlib
import random
import re
import shutil
import hashlib
import socket
import ssl
import subprocess
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

import gevent
from gevent.lock import BoundedSemaphore as _GeventBoundedSemaphore

from flask import Blueprint, g, jsonify, request, Response, send_file, after_this_request, redirect

from host import data_path, safe_path, q as shq, get_user_home

log = logging.getLogger('ethos.radio_music')

radio_music_bp = Blueprint('radio-music', __name__, url_prefix='/api/radio-music')

_DATA_DIR = data_path('radio_music')

_RADIO_API = 'https://de1.api.radio-browser.info'
_ITUNES_API = 'https://itunes.apple.com/search'

_MAX_HISTORY = 100

_AUDIO_EXTS = {'.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wav', '.wma', '.aac', '.webm', '.mp4', '.wv', '.ape'}
_DOWNLOAD_JOBS = {}  # job_id -> {status, progress, path, title, error, finished_at}
_DOWNLOAD_LOCK = threading.Lock()

# Metadata cache: avoids re-running ffprobe for unchanged files
_meta_cache = {}  # {path: {mtime: float, meta: dict}}
_meta_cache_file = None
_meta_cache_dirty = False
_meta_cache_lock = threading.Lock()


def _meta_cache_path():
    global _meta_cache_file
    if not _meta_cache_file:
        _meta_cache_file = os.path.join(data_path('radio_music'), 'meta_cache.json')
        os.makedirs(os.path.dirname(_meta_cache_file), exist_ok=True)
    return _meta_cache_file


def _load_meta_cache():
    global _meta_cache
    with _meta_cache_lock:
        try:
            with open(_meta_cache_path(), 'r') as f:
                _meta_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _meta_cache = {}


def _save_meta_cache():
    global _meta_cache_dirty
    with _meta_cache_lock:
        if not _meta_cache_dirty:
            return
        try:
            with open(_meta_cache_path(), 'w') as f:
                json.dump(_meta_cache, f)
            _meta_cache_dirty = False
        except OSError:
            pass


def _probe_audio_cached(fpath, mtime):
    """Return cached ffprobe result if file unchanged, else probe and cache."""
    global _meta_cache_dirty
    with _meta_cache_lock:
        cached = _meta_cache.get(fpath)
        if cached and abs(cached.get('mtime', 0) - mtime) < 0.01:
            return cached['meta']
    meta = _probe_audio(fpath)
    with _meta_cache_lock:
        _meta_cache[fpath] = {'mtime': mtime, 'meta': meta}
        _meta_cache_dirty = True
    return meta


def _safe_int(val, default, lo=1, hi=200):
    """Parse an integer from a request arg, clamping to [lo, hi]."""
    try:
        return max(lo, min(int(val), hi))
    except (ValueError, TypeError):
        return default

# ── Offline Archive ──────────────────────────────────────────
_ARCHIVE_LOCK = threading.Lock()
_ARCHIVE_SEM = _GeventBoundedSemaphore(2)   # max 2 concurrent yt-dlp downloads


def _archive_dir():
    d = data_path('offline-archive')
    os.makedirs(d, exist_ok=True)
    return d


def _archive_db_path():
    return data_path('rm_archive.json')


def _load_archive():
    try:
        with open(_archive_db_path()) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_archive(db):
    tmp = _archive_db_path() + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(db, f)
    os.replace(tmp, _archive_db_path())


def _archive_key(url):
    """16-char hex key derived from URL — stable across restarts."""
    return hashlib.md5(url.encode('utf-8')).hexdigest()[:16]


def _sio():
    """Return SocketIO instance wired by app_manager, or None."""
    return getattr(radio_music_bp, '_socketio', None)


def _user_dir():
    """Per-user data directory: data/radio_music/users/{username}/"""
    username = getattr(g, 'username', None) or 'default'
    d = os.path.join(_DATA_DIR, 'users', username)
    os.makedirs(d, exist_ok=True)
    return d


def _user_file(name):
    """Path to a per-user JSON file."""
    return os.path.join(_user_dir(), name)


def _ensure_dirs():
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load_json(path, default=None):
    if default is None:
        default = []
    try:
        if os.path.isfile(path):
            with open(path, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _radio_api(endpoint, params=None, timeout=10):
    """Call Radio Browser API. Returns parsed JSON or empty list on error."""
    url = _RADIO_API + endpoint
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'EthOS-RadioMusic/1.0',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log.debug('Radio API error %s: %s', endpoint, e)
        return []


def _pick_station(s):
    """Extract useful fields from a Radio Browser station object."""
    return {
        'uuid': s.get('stationuuid', ''),
        'name': s.get('name', '').strip(),
        'url': s.get('url_resolved') or s.get('url', ''),
        'favicon': s.get('favicon', ''),
        'country': s.get('country', ''),
        'countrycode': s.get('countrycode', ''),
        'language': s.get('language', ''),
        'tags': s.get('tags', ''),
        'bitrate': s.get('bitrate', 0),
        'codec': s.get('codec', ''),
        'votes': s.get('votes', 0),
        'homepage': s.get('homepage', ''),
        'hls': s.get('hls', 0),
    }


def _aggregate_stations(raw_list):
    """Merge duplicates by normalised name, collecting alt URLs as fallbacks.
    Prefer the entry with the highest votes/bitrate as the primary."""
    import re
    groups = {}
    for s in raw_list:
        picked = _pick_station(s)
        url = picked['url']
        if not url:
            continue
        # Normalise: lowercase, strip whitespace, collapse spaces,
        # remove trailing frequency-like suffixes (e.g. "102.5")
        key = re.sub(r'\s+', ' ', picked['name'].lower().strip())
        key = re.sub(r'\s*\d{2,3}[.,]\d.*$', '', key)  # "eska wrocław 102.5"
        key = key.rstrip()
        if not key:
            continue
        if key not in groups:
            groups[key] = picked
            groups[key]['alt_urls'] = []
        else:
            existing = groups[key]
            # Collect unique alt URL
            all_urls = [existing['url']] + existing.get('alt_urls', [])
            if url not in all_urls:
                # If new entry is better (higher bitrate), swap
                if picked['bitrate'] > existing['bitrate']:
                    existing['alt_urls'].append(existing['url'])
                    existing['url'] = url
                    existing['bitrate'] = picked['bitrate']
                    existing['codec'] = picked['codec']
                else:
                    existing['alt_urls'].append(url)
            # Merge votes (take max)
            if picked['votes'] > existing['votes']:
                existing['votes'] = picked['votes']
            # Always prefer a non-empty favicon/homepage
            if picked['favicon'] and not existing['favicon']:
                existing['favicon'] = picked['favicon']
            if picked['homepage'] and not existing['homepage']:
                existing['homepage'] = picked['homepage']
    return list(groups.values())


# ── Package status (trivial — no system deps needed) ─────────

@radio_music_bp.route('/pkg-status', methods=['GET'])
def pkg_status():
    return jsonify({'ok': True, 'installed': True, 'ready': True})


@radio_music_bp.route('/install', methods=['POST'])
def install():
    """Called by App Manager after apt_deps/pip_deps are already installed."""
    from host import host_run
    _ensure_dirs()

    # Install deno (JS runtime required by yt-dlp for YouTube) — not available via apt/pip
    host_run('which deno >/dev/null 2>&1 || (curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh 2>/dev/null)', timeout=60)

    # Configure yt-dlp to use EJS solver (required for YouTube extraction)
    os.makedirs('/etc/yt-dlp', exist_ok=True)
    cfg_path = '/etc/yt-dlp/config'
    if not os.path.isfile(cfg_path):
        with open(cfg_path, 'w') as f:
            f.write('--remote-components ejs:github\n')

    global _YTDLP_BIN
    _YTDLP_BIN = None
    return jsonify({'ok': True})


@radio_music_bp.route('/uninstall', methods=['POST'])
def uninstall():
    from host import host_run
    ethos_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pip_bin = os.path.join(ethos_root, 'venv', 'bin', 'pip')

    # Remove yt-dlp (keep ffmpeg/deno as other apps may use them)
    host_run(f'{shq(pip_bin)} uninstall -y yt-dlp', timeout=60)

    # Clean yt-dlp config
    cfg_path = '/etc/yt-dlp/config'
    if os.path.isfile(cfg_path):
        os.remove(cfg_path)

    # Clean yt-dlp cache
    cache_dir = os.path.expanduser('~/.cache/yt-dlp')
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)

    global _YTDLP_BIN
    _YTDLP_BIN = None
    return jsonify({'ok': True})


def _ensure_meta_cache():
    """Ensure meta cache is loaded. Used by sub-modules to avoid stale binding."""
    if not _meta_cache:
        _load_meta_cache()

# ── Deezer-based recommendations (free, no API key) ─────────

_DEEZER_API = 'https://api.deezer.com'


def _deezer_get(path, params=None):
    """GET request to Deezer API, returns parsed JSON or empty dict."""
    url = _DEEZER_API + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log.debug('Deezer API error for %s: %s', path, e)
        return {}


def _get_deezer_similar_artists(art_name, limit=4):
    """Return list of similar artists [{name, picture}] for art_name via Deezer."""
    search = _deezer_get('/search/artist', {'q': art_name, 'limit': 1})
    results = search.get('data', [])
    if not results:
        return []
    aid = results[0].get('id')
    related = _deezer_get(f'/artist/{aid}/related', {'limit': limit})
    return [{'name': a.get('name', ''), 'picture': a.get('picture_medium', '')}
            for a in related.get('data', [])]

# ── Music folders config (per-user) ─────────────────────────

def _music_folders_file():
    return _user_file('music_folders.json')


def _default_music_dir():
    """User's home Music folder, always included."""
    username = getattr(g, 'username', None) or 'default'
    home = get_user_home(username)
    return os.path.join(home, 'Music')


def _default_audiobooks_dir():
    """User's home Audiobooks folder."""
    username = getattr(g, 'username', None) or 'default'
    home = get_user_home(username)
    return os.path.join(home, 'Audiobooks')


def _get_music_folders():
    """Return list of configured music folders + user home Music (always)."""
    folders = _load_json(_music_folders_file(), [])
    home_music = _default_music_dir()
    # Ensure home Music dir always present
    if home_music not in folders:
        folders.insert(0, home_music)
    return folders


def _get_audiobook_folders():
    """Return the audiobook folder list (just the default for now)."""
    d = _default_audiobooks_dir()
    os.makedirs(d, exist_ok=True)
    return [d]


def _get_all_local_folders():
    """All allowed local folders (music + audiobooks) for path validation."""
    return _get_music_folders() + _get_audiobook_folders()


def _probe_audio(fpath):
    """Extract metadata + cover-art presence from an audio file via ffprobe."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', fpath],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return {}
        info = json.loads(r.stdout)
    except Exception:
        return {}
    tags = (info.get('format') or {}).get('tags') or {}
    # Normalize tag keys to lowercase for case-insensitive lookup
    ltags = {k.lower(): v for k, v in tags.items()}
    dur = 0.0
    try:
        dur = float(info['format'].get('duration', 0))
    except (ValueError, TypeError, KeyError):
        pass
    has_art = any(
        s.get('codec_type') == 'video' or s.get('codec_name') in ('mjpeg', 'png')
        for s in info.get('streams', [])
    )
    # Extract year from date tag (yt-dlp writes YYYYMMDD, standard is YYYY or YYYY-MM-DD)
    raw_date = ltags.get('date', '') or ltags.get('year', '')
    year = raw_date[:4] if raw_date and raw_date[:4].isdigit() else ''
    return {
        'title': ltags.get('title', ''),
        'artist': ltags.get('artist', '') or ltags.get('album_artist', ''),
        'album': ltags.get('album', ''),
        'genre': ltags.get('genre', ''),
        'year': year,
        'track': ltags.get('track', ''),
        'duration': round(dur, 1),
        'has_art': has_art,
    }

_VARIOUS_ARTISTS_PLAYLIST = 'Various Artists'

# ── Music: YouTube / multi-source (via yt-dlp) ──────────────

_YTDLP_BIN = None
_YTDLP_URL_CACHE = {}   # {video_url: (audio_url, ct_hint, expiry)}
_YTDLP_CACHE_TTL = 3600  # 1 hour (YouTube URLs last ~6 hours)


def _find_ytdlp():
    """Locate yt-dlp binary (venv first, then system PATH)."""
    global _YTDLP_BIN
    if _YTDLP_BIN and os.path.isfile(_YTDLP_BIN):
        return _YTDLP_BIN
    # backend/blueprints/ → backend/ → /opt/ethos/
    ethos_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    venv_bin = os.path.join(ethos_root, 'venv', 'bin', 'yt-dlp')
    if os.path.isfile(venv_bin):
        _YTDLP_BIN = venv_bin
        return venv_bin
    sys_bin = shutil.which('yt-dlp')
    if sys_bin:
        _YTDLP_BIN = sys_bin
        return sys_bin
    return None

def _fmt_secs(s):
    """Format seconds to H:MM:SS or M:SS."""
    if not s:
        return ''
    s = int(s)
    if s >= 3600:
        return f'{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}'
    return f'{s // 60}:{s % 60:02d}'


def _extract_audio_url(video_url):
    """Extract direct audio URL from a video page via yt-dlp. Results are cached.
    Falls back to HLS m3u8 for live streams / HLS-only videos."""
    now = time.time()
    if video_url in _YTDLP_URL_CACHE:
        audio_url, ct_hint, exp = _YTDLP_URL_CACHE[video_url]
        if now < exp:
            return audio_url, ct_hint

    ytdlp = _find_ytdlp()
    if not ytdlp:
        return None, None

    from host import host_run
    # First try: prefer audio-only formats (no HLS, no live stream overhead)
    cmd = (f'{shq(ytdlp)} -f "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio[protocol!=m3u8]" '
           f'-g --no-warnings --no-playlist {shq(video_url)}')
    r = host_run(cmd, timeout=30)

    audio_url = r.stdout.strip().splitlines()[0] if r.stdout and r.stdout.strip() else ''

    # Fallback: HLS streams (live broadcasts, some regional content)
    if not audio_url:
        cmd2 = (f'{shq(ytdlp)} -f "91/92/93/bestaudio/best[height<=480]" '
                f'-g --no-warnings --no-playlist {shq(video_url)}')
        r2 = host_run(cmd2, timeout=30)
        audio_url = r2.stdout.strip().splitlines()[0] if r2.stdout and r2.stdout.strip() else ''
        if audio_url:
            log.info('yt-dlp HLS fallback for %s', video_url)
        else:
            log.warning('yt-dlp extraction failed for %s: %s', video_url, (r.stderr or r2.stderr or '')[:200])
            return None, None

    if 'm3u8' in audio_url or 'manifest' in audio_url:
        ct = 'application/x-mpegURL'
    elif 'm4a' in audio_url or 'mime=audio%2Fmp4' in audio_url:
        ct = 'audio/mp4'
    else:
        ct = 'audio/webm'

    _YTDLP_URL_CACHE[video_url] = (audio_url, ct, now + _YTDLP_CACHE_TTL)

    # Evict expired entries
    for k in list(_YTDLP_URL_CACHE):
        if _YTDLP_URL_CACHE[k][2] < now:
            del _YTDLP_URL_CACHE[k]
    return audio_url, ct

# ── Stream proxy (solves CORS, ICY, HLS issues) ─────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE



# ── Sub-module route registration ───────────────────────────
# Import sub-modules last so they can import from this module without
# circular import issues. The imports trigger @radio_music_bp.route
# decorators in each sub-module, registering routes on this blueprint.
from blueprints import radio_music_radio      # noqa: E402, F401
from blueprints import radio_music_podcasts   # noqa: E402, F401
from blueprints import radio_music_youtube    # noqa: E402, F401
from blueprints import radio_music_local      # noqa: E402, F401
from blueprints import radio_music_playlist   # noqa: E402, F401
