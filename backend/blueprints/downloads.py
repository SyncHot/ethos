"""
EthOS — Download Manager
Backend API for managing downloads with debrid service support
(AllDebrid, Real-Debrid, Premiumize, direct HTTP/FTP, torrents/magnets)
"""

from flask import Blueprint, request, jsonify, g
from flask_socketio import SocketIO
import os
import json
import time
import uuid
import threading
import re
import urllib.parse
import urllib.request
import ssl
import base64
import hashlib
import subprocess
import collections
import logging
import glob

import gevent
import gevent.threadpool

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path, user_data_path, NATIVE_MODE
from utils import safe_path as _safe_path_util, get_username as _utils_get_username, sio_emit, DATA_ROOT, register_pkg_routes, \
    require_tools, check_tool

log = logging.getLogger(__name__)

# Native OS thread pool for blocking file I/O on slow disks (HDD).
# gevent monkey-patches threading.Thread → greenlets, so f.write() in a
# download "thread" actually blocks the whole event loop when the kernel
# stalls on balance_dirty_pages.  Using a real OS thread pool keeps the
# main loop responsive.
_io_pool = gevent.threadpool.ThreadPool(4)

downloads_bp = Blueprint('downloads', __name__)

DATA_DIR = data_path()
DOWNLOADS_STATE_FILE = os.path.join(DATA_DIR, 'downloads_state.json')
DOWNLOADS_CONFIG_FILE = os.path.join(DATA_DIR, 'downloads_config.json')
DOWNLOADS_PACKAGES_FILE = os.path.join(DATA_DIR, 'downloads_packages.json')
DOWNLOADS_HISTORY_FILE = os.path.join(DATA_DIR, 'downloads_history.json')
TORRENT_CACHE_DIR = os.path.join(DATA_DIR, 'torrent_cache')
# DATA_ROOT imported from utils

MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds, exponential: 5, 10, 20
MAX_HISTORY = 1000  # keep last N history entries

# Transient error patterns worth retrying
_TRANSIENT_ERRORS = (
    'timeout', 'timed out', 'connection reset', 'connection refused',
    'broken pipe', 'network is unreachable', 'temporary failure',
    'urlopen error', 'eof occurred', 'incomplete read',
)


def _is_transient_error(error_str):
    """Check if an error is transient and worth retrying."""
    lower = error_str.lower()
    return any(pat in lower for pat in _TRANSIENT_ERRORS)

# Archive extensions for deep extract
ARCHIVE_EXTENSIONS = {
    '.zip', '.rar', '.7z', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2',
    '.tar.xz', '.txz', '.gz', '.bz2', '.xz', '.cab', '.iso',
}
# Multi-part rar patterns
RAR_PART_RE = re.compile(r'\.(part\d+\.rar|r\d+)$', re.IGNORECASE)

# In-memory state
_downloads = {}  # id -> download dict
_packages = {}   # package_id -> package dict
_lock = threading.Lock()
_socketio = None


_watch_thread = None
_watch_stop = threading.Event()

# Extraction queue — only one extraction at a time
_extract_queue = collections.deque()  # deque of package_id
_extract_running = threading.Event()   # set while an extraction is active
_extract_thread = None                 # the single extraction worker thread
_extract_start_lock = threading.Lock() # guards check-and-start of _extract_thread

# Stop event for state saver loop
_saver_stop = threading.Event()


def init_downloads(socketio_instance):
    """Initialize with socketio for real-time progress."""
    global _socketio
    _socketio = socketio_instance
    os.makedirs(TORRENT_CACHE_DIR, exist_ok=True)
    _load_state()
    _clean_torrent_cache()  # remove orphan torrent cache files
    _flush_state()  # persist any recovery corrections (e.g. stuck extracting reset)
    # Start background state saver (flushes dirty state every 5s)
    threading.Thread(target=_state_saver_loop, daemon=True).start()
    # Auto-resume any pending downloads after restart
    _active_threads.clear()  # stale refs from previous run
    _start_next()
    # Start watch folder monitor
    _start_watch_folder()


def _clean_torrent_cache():
    """Remove torrent cache files not referenced by any download."""
    try:
        with _lock:
            active_caches = {d.get('torrent_cache_path', '') for d in _downloads.values()}
        for fname in os.listdir(TORRENT_CACHE_DIR):
            fpath = os.path.join(TORRENT_CACHE_DIR, fname)
            if fpath not in active_caches:
                try:
                    os.remove(fpath)
                except OSError:
                    pass
    except Exception:
        pass


def _safe_path(user_path):
    try:
        sudo = getattr(g, 'sudo_mode', False)
    except RuntimeError:
        sudo = False          # background thread – no Flask request context
    return _safe_path_util(user_path, isolate_home=False, sudo_mode=sudo)


# ─── Config (per-user, cached in memory, invalidated on save) ───

_config_cache = {}  # username -> config dict
_config_defaults = {
    # Set default_dir and default_dir_torrent to user's localized Downloads folder on data drive
    'default_dir': None,  # Will be set dynamically
    'default_dir_torrent': None,  # Will be set dynamically
    'max_concurrent': 3,
    'debrid_service': 'none',
    'alldebrid_api_key': '',
    'realdebrid_api_key': '',
    'premiumize_api_key': '',
    'debridlink_api_key': '',
    'torbox_api_key': '',
    'watch_folder': '',
    'watch_folder_enabled': False,
    'overwrite_existing': False,
    'speed_limit': 0,
    'auto_categorize': True,
    'categories': [
        {'id': 'movies', 'name': 'Filmy', 'path': '', 'extensions': ['mp4', 'mkv', 'avi', 'mov', 'wmv', 'm4v']},
        {'id': 'music', 'name': 'Muzyka', 'path': '', 'extensions': ['mp3', 'flac', 'wav', 'aac', 'ogg', 'wma', 'm4a']},
        {'id': 'documents', 'name': 'Dokumenty', 'path': '', 'extensions': ['pdf', 'doc', 'docx', 'txt', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods']},
        {'id': 'software', 'name': 'Oprogramowanie', 'path': '', 'extensions': ['iso', 'exe', 'msi', 'deb', 'rpm', 'apk', 'sh', 'appimage', 'dmg']},
        {'id': 'images', 'name': 'Obrazy', 'path': '', 'extensions': ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'svg', 'webp']},
        {'id': 'archives', 'name': 'Archiwa', 'path': '', 'extensions': ['zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz']},
        {'id': 'other', 'name': 'Inne', 'path': '', 'extensions': []}
    ]
}


def _config_file(username=None):
    """Return per-user config file path. Falls back to global if no user."""
    if username:
        return user_data_path('downloads_config.json', username)
    return DOWNLOADS_CONFIG_FILE


def _get_username():
    """Get current username from Flask g (set by before_request)."""
    return _utils_get_username()


def _load_config(username=None):
    if username is None:
        username = _get_username()
    cache_key = username or '__global__'
    if cache_key in _config_cache:
        return dict(_config_cache[cache_key])
    cfg = dict(_config_defaults)
    # Dynamically set default_dir and default_dir_torrent if not present
    from host import get_user_home, get_default_folders
    user = username or _get_username()
    home = get_user_home(user) if user else '/home'
    folders = get_default_folders()
    downloads_folder = None
    for f in folders:
        if f.lower() in ('downloads', 'pobrane', 'descargas', 'téléchargements', 'dokumente', 'fotos', 'photos', 'videos', 'filmy', 'zdjęcia', 'vídeos', 'videa'):
            downloads_folder = f
            break
    if not downloads_folder:
        downloads_folder = folders[1] if len(folders) > 1 else 'Downloads'
    default_path = os.path.join(home, downloads_folder)
    cfg['default_dir'] = cfg.get('default_dir') or default_path
    cfg['default_dir_torrent'] = cfg.get('default_dir_torrent') or default_path
    cfg['watch_folder'] = cfg.get('watch_folder') or default_path
    cf = _config_file(username)
    if os.path.isfile(cf):
        try:
            with open(cf) as f:
                saved = json.load(f)
            cfg.update(saved)
        except Exception:
            pass
    # Fallback: if per-user file doesn't exist, try loading global config
    elif username and os.path.isfile(DOWNLOADS_CONFIG_FILE):
        try:
            with open(DOWNLOADS_CONFIG_FILE) as f:
                saved = json.load(f)
            cfg.update(saved)
        except Exception:
            pass
    _config_cache[cache_key] = cfg
    return dict(cfg)


def _save_config(cfg, username=None):
    if username is None:
        username = _get_username()
    cache_key = username or '__global__'
    cf = _config_file(username)
    with open(cf, 'w') as f:
        json.dump(cfg, f, indent=2)
    _config_cache[cache_key] = dict(cfg)


# ─── Watch folder ───

def _start_watch_folder():
    """Start/restart the watch folder monitoring thread."""
    global _watch_thread
    _watch_stop.set()
    if _watch_thread and _watch_thread.is_alive():
        _watch_thread.join(timeout=5)
    _watch_stop.clear()
    cfg = _load_config()
    if cfg.get('watch_folder_enabled') and cfg.get('watch_folder'):
        _watch_thread = threading.Thread(target=_watch_folder_loop, daemon=True)
        _watch_thread.start()


def _watch_folder_loop():
    """Poll watch folder for .torrent and .txt files every 10s."""
    while not _watch_stop.is_set():
        try:
            cfg = _load_config()
            folder = cfg.get('watch_folder', '')
            if not folder or not cfg.get('watch_folder_enabled'):
                break
            real_folder = _safe_path(folder)
            if not real_folder or not os.path.isdir(real_folder):
                _watch_stop.wait(10)
                continue
            for fname in os.listdir(real_folder):
                if _watch_stop.is_set():
                    break
                # Skip macOS resource forks and hidden files
                if fname.startswith('.') or fname.startswith('._'):
                    continue
                lower = fname.lower()
                fpath = os.path.join(real_folder, fname)
                if not os.path.isfile(fpath):
                    continue

                if lower.endswith('.torrent'):
                    _watch_handle_torrent(fpath, fname, cfg)
                elif lower.endswith('.txt'):
                    _watch_handle_txt(fpath, fname, cfg)
        except Exception:
            pass
        _watch_stop.wait(10)


def _watch_handle_torrent(fpath, fname, cfg):
    """Process a .torrent file from watch folder."""
    try:
        with open(fpath, 'rb') as _tf:
            torrent_data = _tf.read()
        if len(torrent_data) > 5 * 1024 * 1024:
            os.remove(fpath)
            return
        dl_id = str(uuid.uuid4())[:8]
        cache_path = os.path.join(TORRENT_CACHE_DIR, f"{dl_id}.torrent")
        with open(cache_path, 'wb') as tf:
            tf.write(torrent_data)
        # Extract torrent name
        torrent_name = fname.replace('.torrent', '')
        try:
            idx = torrent_data.find(b'4:name')
            if idx >= 0:
                rest = torrent_data[idx + 6:]
                if rest[0:1].isdigit():
                    colon = rest.index(b':')
                    length = int(rest[:colon])
                    torrent_name = rest[colon + 1:colon + 1 + length].decode('utf-8', errors='replace')
        except Exception:
            pass
        # Extract magnet URI for retry/resume support
        magnet_url = _extract_magnet_from_torrent(torrent_data)
        torrent_url = magnet_url or f'torrent://{torrent_name}'
        dest_dir = cfg.get('default_dir_torrent', '/home')
        dl = {
            'id': dl_id,
            'url': torrent_url,
            'filename': torrent_name,
            'filesize': 0,
            'downloaded': 0,
            'progress': 0,
            'speed': 0,
            'status': 'pending',
            'error': '',
            'debrid_error': '',
            'dest_dir': dest_dir,
            'dest_path': '',
            'use_debrid': True,
            'added_at': time.time(),
            'started_at': 0,
            'completed_at': 0,
            'is_torrent': True,
            'torrent_cache_path': cache_path,
            'watch_origin_name': fname,
        }
        with _lock:
            _downloads[dl_id] = dl
            _save_state()
        _emit('dl:update', _sanitize(dl))
        _start_next()
        # Move to processed/ (will be moved to error/ if it fails)
        _move_watch_torrent(fpath, fname, cfg, 'processed')
    except Exception:
        pass


def _move_watch_torrent(fpath, fname, cfg, subfolder):
    """Move a torrent file to processed/ or error/ subfolder inside watch folder."""
    try:
        watch_dir = _safe_path(cfg.get('watch_folder', ''))
        if not watch_dir:
            return
        target_dir = os.path.join(watch_dir, subfolder)
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, fname)
        # If already exists in target, overwrite
        if os.path.exists(target_path):
            os.remove(target_path)
        import shutil
        if os.path.exists(fpath):
            shutil.move(fpath, target_path)
    except Exception:
        # Fallback: just remove the file
        try:
            os.remove(fpath)
        except OSError:
            pass


def _move_torrent_on_finish(dl, success):
    """After torrent download completes or fails, move the original .torrent to processed/ or error/."""
    origin_name = dl.get('watch_origin_name')
    if not origin_name:
        return
    cfg = _load_config()
    watch_dir = _safe_path(cfg.get('watch_folder', ''))
    if not watch_dir:
        return
    # It should already be in processed/ from watch handler; if failed, move to error/
    if not success:
        src = os.path.join(watch_dir, 'processed', origin_name)
        if os.path.exists(src):
            _move_watch_torrent(src, origin_name, cfg, 'error')


def _watch_handle_txt(fpath, fname, cfg):
    """Process a .txt file with URLs (one per line) from watch folder."""
    try:
        with open(fpath, 'r', encoding='utf-8', errors='replace') as _tf:
            content = _tf.read()
        urls = [line.strip() for line in content.splitlines()
                if line.strip() and (line.strip().startswith('http://') or line.strip().startswith('https://') or line.strip().startswith('magnet:'))]
        if not urls:
            os.remove(fpath)
            return

        base_dir = cfg.get('default_dir', '/home')
        # Create package if multiple URLs
        package_id = ''
        pkg_folder = ''
        dl_ids = []
        if len(urls) > 1:
            pkg_name = os.path.splitext(fname)[0]
            package_id = 'pkg_' + str(uuid.uuid4())[:8]
            # Create subfolder for package
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', pkg_name)[:120]
            pkg_folder = os.path.join(base_dir, safe_name)
            real_pkg_folder = _safe_path(pkg_folder)
            if real_pkg_folder:
                os.makedirs(real_pkg_folder, exist_ok=True)
                pkg_folder = real_pkg_folder

        for u in urls:
            is_t = _is_torrent(u)
            dl_id = str(uuid.uuid4())[:8]
            _default_key = 'default_dir_torrent' if is_t else 'default_dir'
            dl_dest = pkg_folder if pkg_folder else cfg.get(_default_key, base_dir)
            dl = {
                'id': dl_id,
                'url': u,
                'filename': '',
                'filesize': 0,
                'downloaded': 0,
                'progress': 0,
                'speed': 0,
                'status': 'pending',
                'error': '',
                'debrid_error': '',
                'dest_dir': dl_dest,
                'dest_path': '',
                'use_debrid': True,
                'added_at': time.time(),
                'started_at': 0,
                'completed_at': 0,
                'is_torrent': is_t,
                'package_id': package_id,
            }
            with _lock:
                _downloads[dl_id] = dl
                _save_state()
            dl_ids.append(dl_id)
            _emit('dl:update', _sanitize(dl))

        # Create package entry
        if package_id and dl_ids:
            pkg = {
                'id': package_id,
                'name': os.path.splitext(fname)[0],
                'dl_ids': dl_ids,
                'dest_dir': pkg_folder or base_dir,
                'status': 'downloading',
                'auto_extract': False,
                'delete_after_extract': False,
                'extract_password': '',
                'extract_error': '',
                'created_at': time.time(),
                'has_archives': False,
            }
            with _lock:
                _packages[package_id] = pkg
                _save_state()
            _emit('dl:package_update', _sanitize_package(pkg))

        _start_next()
        os.remove(fpath)
    except Exception:
        pass


# Keys internal to runtime — never sent to clients but some are persisted
_INTERNAL_KEYS = {'_speed_samples'}
# Keys hidden from frontend but saved to state file
_PERSIST_HIDDEN = {'_actual_dest', 'torrent_cache_path'}


# ─── State persistence ───

def _load_json_safe(filepath):
    """Load JSON with fallback to .tmp file if main file is empty/corrupt."""
    for path in [filepath, filepath + '.tmp']:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, Exception):
                continue
    return []


def _load_state():
    global _downloads, _packages
    data = _load_json_safe(DOWNLOADS_STATE_FILE)
    for d in data:
        # Migrate legacy torrent:// URLs to magnet URIs where possible
        url = d.get('url', '')
        if url.startswith('torrent://'):
            cache = d.get('torrent_cache_path', '')
            if cache and os.path.isfile(cache):
                try:
                    with open(cache, 'rb') as f:
                        magnet = _extract_magnet_from_torrent(f.read())
                    if magnet:
                        d['url'] = magnet
                except Exception:
                    pass
        # Restore completed/failed/paused, skip active ones
        if d.get('status') in ('completed', 'failed', 'cancelled', 'paused'):
            _downloads[d['id']] = d
        elif d.get('status') in ('downloading', 'pending', 'resolving',
                                 'torrent_uploading', 'torrent_downloading'):
            d['status'] = 'pending'  # re-queue
            # Keep downloaded/progress/_actual_dest for HTTP Range resume
            d['speed'] = 0
            _downloads[d['id']] = d
    # Load packages
    pkgs = _load_json_safe(DOWNLOADS_PACKAGES_FILE)
    for p in pkgs:
        # Reset stuck extracting status from interrupted extraction
        if p.get('status') == 'extracting':
            p['status'] = 'completed'
            p['extract_error'] = 'Extraction interrupted by restart'
        _packages[p['id']] = p


def _atomic_write_json(filepath, data):
    """Write JSON atomically: write to temp file then rename to avoid corruption."""
    tmp = filepath + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, filepath)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


_state_dirty = False


def _save_state():
    """Mark state as dirty; background thread will persist within 5s."""
    global _state_dirty
    _state_dirty = True


def _flush_state():
    """Immediately persist state to disk. Safe to call outside _lock."""
    global _state_dirty
    _state_dirty = False
    with _lock:
        data_dl = [{k: v for k, v in d.items() if k not in _INTERNAL_KEYS}
                   for d in _downloads.values()]
        data_pkg = [dict(p) for p in _packages.values()]
    _atomic_write_json(DOWNLOADS_STATE_FILE, data_dl)
    _atomic_write_json(DOWNLOADS_PACKAGES_FILE, data_pkg)


# ─── Download history log ───

_history_lock = threading.Lock()


def _log_history(dl, event_type):
    """Append a download event to persistent history log (last MAX_HISTORY entries)."""
    entry = {
        'id': dl.get('id', ''),
        'url': dl.get('url', ''),
        'filename': dl.get('filename', ''),
        'filesize': dl.get('downloaded', 0) or dl.get('filesize', 0),
        'dest_dir': dl.get('dest_dir', ''),
        'is_torrent': dl.get('is_torrent', False),
        'use_debrid': dl.get('use_debrid', False),
        'event': event_type,
        'error': dl.get('error', '') if event_type == 'failed' else '',
        'timestamp': time.time(),
        'duration': round(time.time() - dl.get('started_at', time.time()), 1) if dl.get('started_at') else 0,
        'user': dl.get('user', ''),
    }
    try:
        with _history_lock:
            history = []
            if os.path.isfile(DOWNLOADS_HISTORY_FILE):
                try:
                    with open(DOWNLOADS_HISTORY_FILE) as f:
                        history = json.load(f)
                except Exception:
                    history = []
            history.append(entry)
            # Trim to last MAX_HISTORY
            if len(history) > MAX_HISTORY:
                history = history[-MAX_HISTORY:]
            _atomic_write_json(DOWNLOADS_HISTORY_FILE, history)
    except Exception:
        pass


def _state_saver_loop():
    """Background thread: flush dirty state to disk every 5s."""
    global _state_dirty
    while not _saver_stop.is_set():
        _saver_stop.wait(timeout=5)
        if _saver_stop.is_set():
            break
        if _state_dirty:
            try:
                _flush_state()
            except Exception:
                pass


def _load_history(limit=None, username=None):
    """Return persisted download history filtered by user."""
    try:
        with _history_lock:
            if os.path.isfile(DOWNLOADS_HISTORY_FILE):
                with open(DOWNLOADS_HISTORY_FILE) as f:
                    history = json.load(f)
            else:
                history = []
    except Exception:
        history = []
    if username:
        history = [h for h in history if h.get('user', '') == username or not h.get('user')]
    if limit:
        history = history[-limit:]
    return history


def _emit(event, data):
    sio_emit(_socketio, event, data, namespace='/')


# ─── Debrid API helpers ───

def _http_get_json(url, headers=None):
    """Simple HTTP GET returning JSON."""
    req = urllib.request.Request(url, headers=headers or {})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _http_post_json(url, data=None, headers=None):
    """Simple HTTP POST returning JSON."""
    if data:
        encoded = urllib.parse.urlencode(data).encode()
    else:
        encoded = None
    req = urllib.request.Request(url, data=encoded, headers=headers or {})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _resolve_alldebrid(url, api_key):
    """Resolve a link through AllDebrid API."""
    endpoint = f"https://api.alldebrid.com/v4/link/unlock?agent=EthOS&apikey={urllib.parse.quote(api_key)}&link={urllib.parse.quote(url)}"
    result = _http_get_json(endpoint)
    if result.get('status') == 'success' and result.get('data', {}).get('link'):
        data = result['data']
        return {
            'url': data['link'],
            'filename': data.get('filename', ''),
            'filesize': data.get('filesize', 0),
        }
    error = result.get('error', {}).get('message', 'Unknown AllDebrid error')
    raise Exception(f"AllDebrid: {error}")


def _resolve_realdebrid(url, api_key):
    """Resolve a link through Real-Debrid API."""
    # First, unrestrict the link
    endpoint = "https://api.real-debrid.com/rest/1.0/unrestrict/link"
    headers = {'Authorization': f'Bearer {api_key}'}
    data = {'link': url}
    req = urllib.request.Request(endpoint, data=urllib.parse.urlencode(data).encode(), headers=headers)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    if result.get('download'):
        return {
            'url': result['download'],
            'filename': result.get('filename', ''),
            'filesize': result.get('filesize', 0),
        }
    raise Exception("Real-Debrid: Could not unrestrict link")


def _resolve_premiumize(url, api_key):
    """Resolve a link through Premiumize.me API."""
    endpoint = "https://www.premiumize.me/api/transfer/directdl"
    data = {'apikey': api_key, 'src': url}
    result = _http_post_json(endpoint, data=data)
    if result.get('status') == 'success' and result.get('content'):
        content = result['content']
        if content:
            item = content[0]
            return {
                'url': item.get('link', ''),
                'filename': item.get('path', '').split('/')[-1] or '',
                'filesize': item.get('size', 0),
            }
    error = result.get('message', 'Unknown Premiumize error')
    raise Exception(f"Premiumize: {error}")



def _resolve_debridlink(url, api_key):
    """Resolve a link through Debrid-Link API."""
    endpoint = "https://debrid-link.com/api/v2/downloader/add"
    headers = {'Authorization': f'Bearer {api_key}'}
    data = {'url': url}
    req = urllib.request.Request(endpoint, data=urllib.parse.urlencode(data).encode(), headers=headers)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    if result.get('success') and result.get('value'):
        val = result['value']
        return {
            'url': val.get('downloadUrl', ''),
            'filename': val.get('name', ''),
            'filesize': val.get('size', 0),
        }
    raise Exception(f"Debrid-Link: {result.get('error', 'Unknown error')}")


def _resolve_torbox(url, api_key):
    """Resolve a link through TorBox API."""
    endpoint = "https://api.torbox.app/v1/api/webdl/createwebdownload"
    headers = {'Authorization': f'Bearer {api_key}'}
    data = json.dumps({'url': url}).encode()
    req = urllib.request.Request(endpoint, data=data, headers={**headers, 'Content-Type': 'application/json'})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    if result.get('success') and result.get('data'):
        d = result['data']
        return {
            'url': d.get('download_url', '') or d.get('cached_url', ''),
            'filename': d.get('name', ''),
            'filesize': d.get('size', 0),
        }
    raise Exception(f"TorBox: {result.get('detail', 'Unknown error')}")


def _resolve_debrid(url, config):
    """Try to resolve URL through configured debrid service."""
    service = config.get('debrid_service', 'none')
    if service == 'alldebrid' and config.get('alldebrid_api_key'):
        return _resolve_alldebrid(url, config['alldebrid_api_key'])
    elif service == 'realdebrid' and config.get('realdebrid_api_key'):
        return _resolve_realdebrid(url, config['realdebrid_api_key'])
    elif service == 'premiumize' and config.get('premiumize_api_key'):
        return _resolve_premiumize(url, config['premiumize_api_key'])
    elif service == 'debridlink' and config.get('debridlink_api_key'):
        return _resolve_debridlink(url, config['debridlink_api_key'])
    elif service == 'torbox' and config.get('torbox_api_key'):
        return _resolve_torbox(url, config['torbox_api_key'])
    return None  # no debrid configured


def _is_torrent(url):
    """Check if URL is a magnet link, .torrent URL, or torrent:// placeholder."""
    if not url:
        return False
    u = url.strip()
    return (u.startswith('magnet:') or u.startswith('torrent://')
            or u.lower().endswith('.torrent'))


def _extract_magnet_from_torrent(torrent_data):
    """Extract magnet URI from .torrent file bytes by computing info_hash."""
    import hashlib
    try:
        # Find the 'info' dictionary in bencode. Pattern: ...4:infod...
        idx = torrent_data.find(b'4:infod')
        if idx < 0:
            return None
        info_start = idx + 6  # start of the 'd' after '4:info'
        # Parse the bencoded info dict to find its end
        depth = 0
        i = info_start
        while i < len(torrent_data):
            c = torrent_data[i:i + 1]
            if c == b'd' or c == b'l':
                depth += 1
                i += 1
            elif c == b'e':
                depth -= 1
                i += 1
                if depth == 0:
                    break
            elif c == b'i':
                # Integer: i<number>e
                end = torrent_data.index(b'e', i + 1)
                i = end + 1
            elif c.isdigit():
                # String: <length>:<data>
                colon = torrent_data.index(b':', i)
                slen = int(torrent_data[i:colon])
                i = colon + 1 + slen
            else:
                i += 1
        info_bytes = torrent_data[info_start:i]
        info_hash = hashlib.sha1(info_bytes).hexdigest()
        # Extract name for display
        name = ''
        name_idx = info_bytes.find(b'4:name')
        if name_idx >= 0:
            rest = info_bytes[name_idx + 6:]
            if rest[0:1].isdigit():
                colon = rest.index(b':')
                slen = int(rest[:colon])
                name = rest[colon + 1:colon + 1 + slen].decode('utf-8', errors='replace')
        magnet = f'magnet:?xt=urn:btih:{info_hash}'
        if name:
            magnet += f'&dn={urllib.parse.quote(name)}'
        return magnet
    except Exception:
        return None


# Known file hoster domains that require debrid/premium for direct downloads.
# If debrid fails for these, the original URL serves an HTML page, not the file.
_FILE_HOSTER_DOMAINS = {
    'rapidgator', 'uploaded', 'nitroflare', 'turbobit', 'filefactory',
    'mega', 'mediafire', 'zippyshare', 'ddownload', 'katfile',
    'filejoker', 'keep2share', 'k2s', 'publish2', 'fboom',
    'tezfiles', 'hexupload', 'clicknupload', 'oboom', 'alfafile',
    'fileal', 'rosefile', 'filestore', 'mexa', 'wdupload',
    'ddl', 'ddlvalley', 'rapidrar', '1fichier', 'uptobox',
}


def _looks_like_direct_url(url):
    """Check if URL looks like a direct download (not a file hoster page).

    Returns True for CDN links, direct file URLs with media extensions, etc.
    Returns False for known file hoster domains that require premium/debrid.
    """
    try:
        parsed = urllib.parse.urlparse(url.strip())
        host = parsed.hostname or ''
        host_lower = host.lower()
        # Check against known file hosters
        for hoster in _FILE_HOSTER_DOMAINS:
            if hoster in host_lower:
                return False
        # Check if path ends with a common file extension → likely direct
        path_lower = parsed.path.lower()
        direct_exts = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v',
                       '.mp3', '.flac', '.wav', '.zip', '.rar', '.7z',
                       '.iso', '.exe', '.tar', '.gz', '.pdf', '.bin')
        if any(path_lower.endswith(ext) for ext in direct_exts):
            return True
        # URLs with no recognizable extension and on unknown domains —
        # assume direct to avoid false-positives blocking legitimate links
        return True
    except Exception:
        return True


# ─── Torrent via Debrid ───

def _torrent_alldebrid(magnet_or_url, api_key, torrent_file=None):
    """Add torrent/magnet to AllDebrid, wait for completion, return file links."""
    base = "https://api.alldebrid.com/v4.1"
    agent = "EthOS"

    if torrent_file:
        # Upload .torrent file — use magnet/upload/file with multipart
        boundary = uuid.uuid4().hex
        body = b''
        body += f'--{boundary}\r\n'.encode()
        body += b'Content-Disposition: form-data; name="files[]"; filename="upload.torrent"\r\n'
        body += b'Content-Type: application/x-bittorrent\r\n\r\n'
        body += torrent_file
        body += f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(
            f"{base}/magnet/upload/file?agent={agent}&apikey={urllib.parse.quote(api_key)}",
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            result = json.loads(resp.read().decode())

        if result.get('status') != 'success':
            err = result.get('error', {}).get('message', 'Upload failed')
            raise Exception(f"AllDebrid torrent: {err}")

        # /magnet/upload/file returns data.files[] with {id, name, ...}
        data = result.get('data', {})
        files = data.get('files', [])
        if files:
            magnet_id = files[0].get('id')
            if magnet_id:
                return {'service': 'alldebrid', 'torrent_id': magnet_id, 'api_key': api_key}
            err = files[0].get('error', {})
            if isinstance(err, dict):
                err = err.get('message', 'No magnet ID')
            raise Exception(f"AllDebrid: {err}")

        # Fallback: check if it returned magnets[] structure instead
        magnets = data.get('magnets', [])
        if magnets:
            magnet_id = magnets[0].get('id')
            if magnet_id:
                return {'service': 'alldebrid', 'torrent_id': magnet_id, 'api_key': api_key}

        raise Exception(f"AllDebrid: no data returned from file upload (keys: {list(data.keys())})")

    else:
        # Magnet link — use magnet/upload
        data_payload = urllib.parse.urlencode({'magnets[]': magnet_or_url}).encode()
        req = urllib.request.Request(
            f"{base}/magnet/upload?agent={agent}&apikey={urllib.parse.quote(api_key)}",
            data=data_payload,
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            result = json.loads(resp.read().decode())

        if result.get('status') != 'success':
            err = result.get('error', {}).get('message', 'Upload failed')
            raise Exception(f"AllDebrid torrent: {err}")

        magnets = result.get('data', {}).get('magnets', [])
        if not magnets:
            raise Exception(f"AllDebrid: no magnet data (keys: {list(result.get('data', {}).keys())})")

        magnet_id = magnets[0].get('id')
        if not magnet_id:
            err = magnets[0].get('error', {})
            if isinstance(err, dict):
                err = err.get('message', 'No magnet ID')
            raise Exception(f"AllDebrid: {err}")

        return {'service': 'alldebrid', 'torrent_id': magnet_id, 'api_key': api_key}


def _poll_alldebrid(torrent_id, api_key, dl):
    """Poll AllDebrid v4.1 until torrent is ready, return list of download links."""
    base = "https://api.alldebrid.com/v4.1"
    agent = "EthOS"

    for _ in range(600):  # up to ~30 min
        if dl.get('status') == 'cancelled':
            try:
                _http_get_json(f"{base}/magnet/delete?agent={agent}&apikey={urllib.parse.quote(api_key)}&id={torrent_id}")
            except Exception:
                pass
            return []

        result = _http_get_json(
            f"{base}/magnet/status?agent={agent}&apikey={urllib.parse.quote(api_key)}&id={torrent_id}"
        )
        if result.get('status') != 'success':
            time.sleep(3)
            continue

        # v4.1: data.magnets is an object (not a list)
        data = result.get('data', {}).get('magnets', {})
        if isinstance(data, list):
            data = data[0] if data else {}

        status_code = data.get('statusCode', 0)
        dl['torrent_status'] = data.get('status', '')
        dl['torrent_seeders'] = data.get('seeders', 0)
        dl['torrent_speed'] = data.get('downloadSpeed', 0)

        size = data.get('size', 0)
        downloaded = data.get('downloaded', 0)
        if size > 0:
            dl['progress'] = round(downloaded / size * 100, 1)
        dl['filesize'] = size
        _emit('dl:update', _sanitize(dl))

        if status_code == 4:  # Ready
            links = []

            # v4.1 files[] can be nested: folders have {n, e:[...]}, files have {n, s, l}
            def _extract_ad_files(items):
                """Recursively extract all files from AllDebrid nested structure."""
                for obj in items:
                    link = obj.get('l', '')
                    entries = obj.get('e')
                    if link:
                        # This is a file with a direct link
                        try:
                            resolved = _resolve_alldebrid(link, api_key)
                            links.append({
                                'url': resolved['url'],
                                'filename': resolved.get('filename') or obj.get('n', ''),
                                'filesize': resolved.get('filesize') or obj.get('s', 0),
                            })
                        except Exception:
                            links.append({'url': link, 'filename': obj.get('n', ''), 'filesize': obj.get('s', 0)})
                    elif entries and isinstance(entries, list):
                        # This is a folder — recurse into entries
                        _extract_ad_files(entries)

            _extract_ad_files(data.get('files', []))

            # Fallback: check old-style 'links' field too
            if not links:
                for link_obj in data.get('links', []):
                    link_url = link_obj.get('link', '') or link_obj.get('l', '')
                    fname = link_obj.get('filename', '') or link_obj.get('n', '')
                    fsize = link_obj.get('size', 0) or link_obj.get('s', 0)
                    if link_url:
                        try:
                            resolved = _resolve_alldebrid(link_url, api_key)
                            links.append(resolved)
                        except Exception:
                            links.append({'url': link_url, 'filename': fname, 'filesize': fsize})
            return links

        if status_code >= 5:  # Error
            raise Exception(f"AllDebrid torrent error: {data.get('status', 'unknown')}")

        time.sleep(3)

    raise Exception("AllDebrid: timeout waiting for torrent")


def _torrent_realdebrid(magnet_or_url, api_key, torrent_file=None):
    """Add torrent/magnet to Real-Debrid."""
    headers = {'Authorization': f'Bearer {api_key}'}
    ctx = ssl.create_default_context()

    if torrent_file:
        req = urllib.request.Request(
            "https://api.real-debrid.com/rest/1.0/torrents/addTorrent",
            data=torrent_file,
            headers={**headers, 'Content-Type': 'application/x-bittorrent'}
        )
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            result = json.loads(resp.read().decode())
    else:
        data = urllib.parse.urlencode({'magnet': magnet_or_url}).encode()
        req = urllib.request.Request(
            "https://api.real-debrid.com/rest/1.0/torrents/addMagnet",
            data=data, headers=headers
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            result = json.loads(resp.read().decode())

    torrent_id = result.get('id')
    if not torrent_id:
        raise Exception("Real-Debrid: no torrent ID returned")

    # Select all files
    data = urllib.parse.urlencode({'files': 'all'}).encode()
    req = urllib.request.Request(
        f"https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{torrent_id}",
        data=data, headers=headers
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        pass  # 204 No Content

    return {'service': 'realdebrid', 'torrent_id': torrent_id, 'api_key': api_key}


def _poll_realdebrid(torrent_id, api_key, dl):
    """Poll Real-Debrid until torrent is ready."""
    headers = {'Authorization': f'Bearer {api_key}'}

    for _ in range(600):
        if dl.get('status') == 'cancelled':
            try:
                req = urllib.request.Request(
                    f"https://api.real-debrid.com/rest/1.0/torrents/delete/{torrent_id}",
                    method='DELETE', headers=headers
                )
                ctx = ssl.create_default_context()
                urllib.request.urlopen(req, context=ctx, timeout=10)
            except Exception:
                pass
            return []

        result = _http_get_json(
            f"https://api.real-debrid.com/rest/1.0/torrents/info/{torrent_id}",
            headers=headers
        )

        status = result.get('status', '')
        dl['torrent_status'] = status
        dl['torrent_seeders'] = result.get('seeders', 0)
        dl['torrent_speed'] = result.get('speed', 0)

        progress = result.get('progress', 0)
        dl['progress'] = round(progress, 1)
        dl['filesize'] = result.get('bytes', 0)
        _emit('dl:update', _sanitize(dl))

        if status == 'downloaded':
            links = []
            for link_url in result.get('links', []):
                # Unrestrict each link
                try:
                    resolved = _resolve_realdebrid(link_url, api_key)
                    links.append(resolved)
                except Exception:
                    links.append({'url': link_url, 'filename': '', 'filesize': 0})
            return links

        if status in ('magnet_error', 'error', 'virus', 'dead'):
            raise Exception(f"Real-Debrid torrent: {status}")

        time.sleep(3)

    raise Exception("Real-Debrid: timeout waiting for torrent")


def _torrent_premiumize(magnet_or_url, api_key, torrent_file=None):
    """Add torrent/magnet to Premiumize."""
    if torrent_file:
        boundary = uuid.uuid4().hex
        body = b''
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="apikey"\r\n\r\n{api_key}\r\n'.encode()
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'.encode()
        body += torrent_file
        body += f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(
            "https://www.premiumize.me/api/transfer/create",
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            result = json.loads(resp.read().decode())
    else:
        result = _http_post_json(
            "https://www.premiumize.me/api/transfer/create",
            data={'apikey': api_key, 'src': magnet_or_url}
        )

    if result.get('status') != 'success':
        raise Exception(f"Premiumize: {result.get('message', 'failed')}")

    transfer_id = result.get('id')
    if not transfer_id:
        raise Exception("Premiumize: no transfer ID")

    return {'service': 'premiumize', 'torrent_id': transfer_id, 'api_key': api_key}


def _poll_premiumize(torrent_id, api_key, dl):
    """Poll Premiumize until torrent is ready."""
    for _ in range(600):
        if dl.get('status') == 'cancelled':
            try:
                _http_post_json("https://www.premiumize.me/api/transfer/delete",
                                data={'apikey': api_key, 'id': torrent_id})
            except Exception:
                pass
            return []

        result = _http_get_json(
            f"https://www.premiumize.me/api/transfer/list?apikey={urllib.parse.quote(api_key)}"
        )
        if result.get('status') != 'success':
            time.sleep(3)
            continue

        transfer = None
        for t in result.get('transfers', []):
            if str(t.get('id')) == str(torrent_id):
                transfer = t
                break

        if not transfer:
            # Transfer might be done, check folder
            # Try getting direct download links
            try:
                ddl = _http_post_json(
                    "https://www.premiumize.me/api/transfer/directdl",
                    data={'apikey': api_key, 'src': dl.get('url', '')}
                )
                if ddl.get('status') == 'success' and ddl.get('content'):
                    links = []
                    for item in ddl['content']:
                        links.append({
                            'url': item.get('link', ''),
                            'filename': item.get('path', '').split('/')[-1],
                            'filesize': item.get('size', 0),
                        })
                    return links
            except Exception:
                pass
            raise Exception("Premiumize: transfer disappeared")

        status = transfer.get('status', '')
        dl['torrent_status'] = transfer.get('message', status)
        progress = transfer.get('progress', 0)
        if isinstance(progress, (int, float)):
            dl['progress'] = round(progress * 100, 1)
        _emit('dl:update', _sanitize(dl))

        if status == 'finished':
            # Get file links
            folder_id = transfer.get('folder_id') or transfer.get('target_folder_id')
            if folder_id:
                try:
                    folder = _http_get_json(
                        f"https://www.premiumize.me/api/folder/list?apikey={urllib.parse.quote(api_key)}&id={folder_id}"
                    )
                    links = []
                    for item in folder.get('content', []):
                        if item.get('link'):
                            links.append({
                                'url': item['link'],
                                'filename': item.get('name', ''),
                                'filesize': item.get('size', 0),
                            })
                    return links
                except Exception:
                    pass

            # Fallback: direct download
            try:
                ddl = _http_post_json(
                    "https://www.premiumize.me/api/transfer/directdl",
                    data={'apikey': api_key, 'src': dl.get('url', '')}
                )
                if ddl.get('status') == 'success' and ddl.get('content'):
                    return [{'url': c.get('link', ''), 'filename': c.get('path', '').split('/')[-1], 'filesize': c.get('size', 0)} for c in ddl['content']]
            except Exception:
                pass
            raise Exception("Premiumize: could not get download links")

        if status == 'error':
            raise Exception(f"Premiumize torrent: {transfer.get('message', 'error')}")

        time.sleep(3)

    raise Exception("Premiumize: timeout waiting for torrent")


def _add_torrent_to_debrid(url, config, torrent_file=None):
    """Submit magnet/torrent to configured debrid service. Returns torrent info dict."""
    service = config.get('debrid_service', 'none')
    if service == 'alldebrid' and config.get('alldebrid_api_key'):
        return _torrent_alldebrid(url, config['alldebrid_api_key'], torrent_file)
    elif service == 'realdebrid' and config.get('realdebrid_api_key'):
        return _torrent_realdebrid(url, config['realdebrid_api_key'], torrent_file)
    elif service == 'premiumize' and config.get('premiumize_api_key'):
        return _torrent_premiumize(url, config['premiumize_api_key'], torrent_file)
    raise Exception("No debrid service configured for torrent handling")


def _poll_torrent(torrent_info, dl):
    """Poll until torrent is downloaded by debrid, return list of file links."""
    service = torrent_info['service']
    tid = torrent_info['torrent_id']
    key = torrent_info['api_key']
    if service == 'alldebrid':
        return _poll_alldebrid(tid, key, dl)
    elif service == 'realdebrid':
        return _poll_realdebrid(tid, key, dl)
    elif service == 'premiumize':
        return _poll_premiumize(tid, key, dl)
    return []


def _guess_filename(url):
    """Extract filename from URL."""
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    name = path.split('/')[-1] if path else ''
    if not name or '.' not in name:
        name = 'download_' + str(int(time.time()))
    # Sanitize
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    return name[:200]


# ─── Download worker ───

def _download_single_url(dl, download_url, filename, filesize, dest_dir, config):
    """Download a single file URL to dest_dir. Returns True on success.
    Supports HTTP Range resume from partial files.
    """
    resume_offset = 0
    resume_path = dl.get('_actual_dest')

    if resume_path and os.path.isfile(resume_path):
        # Resume from existing partial file
        resume_offset = os.path.getsize(resume_path)
        dest_path = resume_path
        filename = os.path.basename(dest_path)
    else:
        # New download — handle overwrite/rename
        dest_path = os.path.join(dest_dir, filename)
        if not config.get('overwrite_existing', False):
            base, ext = os.path.splitext(dest_path)
            counter = 1
            while os.path.exists(dest_path):
                dest_path = f"{base}_{counter}{ext}"
                counter += 1
        elif os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
    filename = os.path.basename(dest_path)

    dl['filename'] = filename
    dl['filesize'] = filesize
    dl['dest_path'] = dest_path  # actual filesystem path
    dl['_actual_dest'] = dest_path  # save for resume

    # Disk space pre-check (skip for resume — already partly on disk)
    if filesize and resume_offset == 0:
        try:
            st = os.statvfs(dest_dir)
            free_bytes = st.f_bavail * st.f_frsize
            # Need at least filesize + 100MB buffer
            if free_bytes < filesize + 100 * 1024 * 1024:
                free_gb = free_bytes / (1024 ** 3)
                need_gb = filesize / (1024 ** 3)
                raise Exception(
                    f"Not enough disk space: {free_gb:.1f} GB free, {need_gb:.1f} GB needed"
                )
        except OSError:
            pass  # can't check — proceed anyway

    try:
        headers = {'User-Agent': 'EthOS/1.0'}
        if resume_offset > 0:
            headers['Range'] = f'bytes={resume_offset}-'

        req = urllib.request.Request(download_url, headers=headers)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            status_code = getattr(resp, 'status', 200)
            content_length = int(resp.headers.get('content-length', 0))

            if resume_offset > 0 and status_code == 206:
                # Server supports Range — append to existing file
                total = resume_offset + content_length
                downloaded = resume_offset
                file_mode = 'ab'
            else:
                # No Range support or fresh download — start from scratch
                total = content_length or filesize
                downloaded = 0
                resume_offset = 0
                file_mode = 'wb'

            if total:
                dl['filesize'] = total

            chunk_size = 256 * 1024
            last_emit = 0
            # Speed limit (KB/s -> bytes/s), 0 = unlimited
            speed_limit = int(config.get('speed_limit', 0)) * 1024
            throttle_window = 0.25  # measure every 250ms
            window_bytes = 0
            window_start = time.time()

            with open(dest_path, file_mode) as f:
                while True:
                    if dl.get('status') == 'cancelled':
                        break
                    if dl.get('status') == 'paused':
                        # Break immediately — keep partial file for Range resume
                        break
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    # Write in a real OS thread so slow HDD I/O
                    # doesn't block gevent's event loop.
                    _io_pool.apply(f.write, (chunk,))
                    downloaded += len(chunk)
                    window_bytes += len(chunk)
                    dl['downloaded'] = downloaded
                    if total > 0:
                        dl['progress'] = round(downloaded / total * 100, 1)
                    else:
                        # Unknown total — use -1 to signal indeterminate progress
                        dl['progress'] = -1
                    dl['speed'] = _calc_speed(dl)
                    now = time.time()
                    if now - last_emit >= 0.5:
                        last_emit = now
                        _emit('dl:update', _sanitize(dl))
                    # Throttle if speed limit is set
                    if speed_limit > 0:
                        elapsed = now - window_start
                        if elapsed < throttle_window:
                            max_bytes = speed_limit * elapsed
                            if window_bytes >= max_bytes:
                                sleep_time = (window_bytes / speed_limit) - elapsed
                                if sleep_time > 0:
                                    time.sleep(sleep_time)
                        else:
                            window_bytes = 0
                            window_start = time.time()

        if dl.get('status') == 'cancelled':
            try:
                os.remove(dest_path)
            except OSError:
                pass
            dl.pop('_actual_dest', None)
            return False
        if dl.get('status') == 'paused':
            # Keep partial file, save progress for resume
            dl['downloaded'] = downloaded
            dl['_actual_dest'] = dest_path
            _save_state()
            return False
        dl['downloaded'] = os.path.getsize(dest_path) if os.path.exists(dest_path) else downloaded
        dl.pop('_actual_dest', None)  # cleanup — download complete
        return True
    except Exception as e:
        # Keep partial file for resume (store path)
        dl['_actual_dest'] = dest_path
        raise


def _wait_for_slot(dl):
    """Wait for a concurrency slot (limit from global config)."""
    while True:
        # Check if cancelled while waiting
        with _lock:
            if dl.get('status') in ('cancelled', 'failed', 'paused'):
                return False

            # Check global limit (system-wide)
            # We load global config to ensure we respect the AppStore setting
            config = _load_config(username=None)
            max_conc = config.get('max_concurrent', 3)

            # Count currently active downloads (excluding this one if it was already active,
            # but it shouldn't be as we are in 'torrent_downloading' or similar)
            active = sum(1 for d in _downloads.values()
                         if d['status'] in ('downloading', 'resolving'))

            if active < max_conc:
                # Slot available!
                return True

        # Wait before retrying
        time.sleep(2)


def _download_worker(dl_id):
    """Background thread that downloads a single file (or torrent)."""
    with _lock:
        dl = _downloads.get(dl_id)
        if not dl:
            return

    # Load per-user config for the download owner
    dl_owner = dl.get('user', '')
    config = _load_config(username=dl_owner or None)
    original_url = dl['url']
    is_torrent_dl = dl.get('is_torrent', False) or _is_torrent(dl.get('url', ''))
    default_key = 'default_dir_torrent' if is_torrent_dl else 'default_dir'
    dest_dir = _safe_path(dl.get('dest_dir') or config.get(default_key, '/home'))

    if not dest_dir or not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)

    is_torrent = dl.get('is_torrent', False) or _is_torrent(original_url)

    with _lock:
        dl['status'] = 'resolving'
        if is_torrent:
            dl['is_torrent'] = True
        _emit('dl:update', _sanitize(dl))

    # ─── Torrent/Magnet flow ───
    if is_torrent:
        try:
            # Get torrent file data if it was uploaded
            torrent_file_data = None
            torrent_cache = dl.get('torrent_cache_path')
            if torrent_cache and os.path.exists(torrent_cache):
                with open(torrent_cache, 'rb') as f:
                    torrent_file_data = f.read()

            # Submit to debrid
            with _lock:
                dl['status'] = 'torrent_uploading'
                _emit('dl:update', _sanitize(dl))

            torrent_info = _add_torrent_to_debrid(original_url, config, torrent_file_data)

            with _lock:
                dl['status'] = 'torrent_downloading'
                dl['torrent_id'] = torrent_info.get('torrent_id', '')
                dl['started_at'] = time.time()
                _save_state()
                _emit('dl:update', _sanitize(dl))

            # Poll until debrid has downloaded the torrent
            file_links = _poll_torrent(torrent_info, dl)

            if dl.get('status') == 'cancelled':
                _emit('dl:update', _sanitize(dl))
                return

            if not file_links:
                raise Exception("No files to download from torrent")

            # Try to auto-categorize torrent if using default path
            if config.get('auto_categorize', True):
                current_dest = dl.get('dest_dir')
                default_torrent = config.get('default_dir_torrent')
                # If current dest matches default, try to categorize
                if current_dest and default_torrent and os.path.normpath(current_dest) == os.path.normpath(default_torrent):
                    largest = max(file_links, key=lambda x: x.get('filesize', 0))
                    cat_id, cat_path = _get_category_for_file(largest.get('filename', ''), config)
                    if cat_id:
                        dest_dir = cat_path
                        with _lock:
                            dl['category_id'] = cat_id
                            dl['dest_dir'] = dest_dir
                            _save_state()
                        if not os.path.exists(dest_dir):
                            os.makedirs(dest_dir, exist_ok=True)

            # Download all resulting files
            # Wait for a slot before starting local download to respect global limit
            if not _wait_for_slot(dl):
                return

            with _lock:
                dl['status'] = 'downloading'
                dl['progress'] = 0
                dl['downloaded'] = 0
                dl['torrent_files_total'] = len(file_links)
                dl['torrent_files_done'] = 0
                _emit('dl:update', _sanitize(dl))

            # Create a subfolder for multi-file torrents
            if len(file_links) > 1:
                # Use magnet name or first file as folder name
                folder_name = dl.get('filename') or 'torrent_' + dl_id
                folder_name = re.sub(r'[<>:"/\\|?*]', '_', folder_name)[:100]
                torrent_dest = os.path.join(dest_dir, folder_name)
                os.makedirs(torrent_dest, exist_ok=True)
            else:
                torrent_dest = dest_dir

            total_size = sum(link.get('filesize', 0) for link in file_links)
            total_downloaded = 0

            for i, link in enumerate(file_links):
                if dl.get('status') in ('cancelled', 'paused'):
                    break

                link_url = link.get('url', '')
                link_filename = link.get('filename') or _guess_filename(link_url)
                link_filesize = link.get('filesize', 0)

                dl['torrent_files_done'] = i
                dl['filename'] = link_filename

                success = _download_single_url(dl, link_url, link_filename, link_filesize, torrent_dest, config)
                if not success:
                    break  # paused or cancelled
                total_downloaded += dl.get('downloaded', 0)

                dl['torrent_files_done'] = i + 1
                _emit('dl:update', _sanitize(dl))

            if dl.get('status') == 'cancelled':
                _emit('dl:update', _sanitize(dl))
                return

            if dl.get('status') == 'paused':
                _emit('dl:update', _sanitize(dl))
                return

            with _lock:
                dl['status'] = 'completed'
                dl['progress'] = 100
                dl['completed_at'] = time.time()
                dl['downloaded'] = total_downloaded
                dl['filesize'] = total_size or total_downloaded
                if len(file_links) > 1:
                    dl['filename'] = folder_name
                    dl['dest_path'] = torrent_dest
                _save_state()
            _emit('dl:update', _sanitize(dl))
            _emit('dl:completed', _dl_completed_payload(dl))
            _log_history(dl, 'completed')
            _flush_state()

            # Cleanup torrent cache
            if torrent_cache and os.path.exists(torrent_cache):
                try:
                    os.remove(torrent_cache)
                except OSError:
                    pass

            # Move watch torrent to processed/
            _move_torrent_on_finish(dl, True)

            return

        except Exception as e:
            with _lock:
                dl['status'] = 'failed'
                dl['error'] = str(e)[:500]
                _save_state()
            _emit('dl:update', _sanitize(dl))
            _log_history(dl, 'failed')
            _flush_state()
            # Move watch torrent to error/
            _move_torrent_on_finish(dl, False)
            return

    # ─── Regular download flow ───
    resolved = None
    debrid_was_requested = dl.get('use_debrid', True) and config.get('debrid_service', 'none') != 'none'
    if debrid_was_requested:
        try:
            resolved = _resolve_debrid(original_url, config)
        except Exception as e:
            dl['debrid_error'] = str(e)

    # If debrid was requested but failed and the URL doesn't look like a direct
    # download, fail immediately instead of downloading an HTML error page.
    if debrid_was_requested and not resolved:
        if not _looks_like_direct_url(original_url):
            with _lock:
                dl['status'] = 'failed'
                dl['error'] = f"Debrid resolution failed: {dl.get('debrid_error', 'unknown')}. " \
                              "Link is not a direct download URL."
                _save_state()
            _emit('dl:update', _sanitize(dl))
            _log_history(dl, 'failed')
            _flush_state()
            return

    download_url = resolved['url'] if resolved else original_url
    filename = dl.get('filename') or (resolved or {}).get('filename') or _guess_filename(download_url)
    filesize = (resolved or {}).get('filesize', 0)

    # Deduplicate: skip if same resolved URL or filename already downloaded in same package
    pkg_id = dl.get('package_id')
    if pkg_id and resolved:
        with _lock:
            for other in _downloads.values():
                if other.get('id') == dl_id or other.get('package_id') != pkg_id:
                    continue
                if other.get('status') != 'completed':
                    continue
                # Same debrid-resolved filename + similar size → duplicate
                other_fn = other.get('filename', '')
                other_sz = other.get('filesize', 0)
                if other_fn and other_fn == filename and (
                    not filesize or not other_sz or abs(filesize - other_sz) < 1024
                ):
                    dl['status'] = 'completed'
                    dl['progress'] = 100
                    dl['completed_at'] = time.time()
                    dl['filename'] = filename
                    dl['dest_path'] = other.get('dest_path', '')
                    dl['error'] = ''
                    dl['_dedup_of'] = other.get('id', '')
                    _save_state()
            if dl.get('_dedup_of'):
                logging.info('[downloads] Skipping duplicate %s (same as %s): %s',
                             dl_id, dl['_dedup_of'], filename)
                _emit('dl:update', _sanitize(dl))
                _log_history(dl, 'completed')
                _flush_state()
                return

    # Auto-categorize
    if config.get('auto_categorize', True):
        current_dest = dl.get('dest_dir')
        default_dir = config.get('default_dir')
        if current_dest and default_dir and os.path.normpath(current_dest) == os.path.normpath(default_dir):
            cat_id, cat_path = _get_category_for_file(filename, config)
            if cat_id:
                dest_dir = cat_path
                with _lock:
                    dl['category_id'] = cat_id
                    dl['dest_dir'] = dest_dir
                    _save_state()
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir, exist_ok=True)

    retries = dl.get('retry_count', 0)
    max_retries = MAX_RETRIES

    for attempt in range(max_retries + 1):
        with _lock:
            dl['status'] = 'downloading'
            dl['started_at'] = time.time()
            if attempt > 0:
                dl['retry_count'] = attempt
            _emit('dl:update', _sanitize(dl))

        try:
            success = _download_single_url(dl, download_url, filename, filesize, dest_dir, config)

            if dl.get('status') == 'cancelled':
                _emit('dl:update', _sanitize(dl))
                return
            elif dl.get('status') == 'paused':
                _emit('dl:update', _sanitize(dl))
                return
            elif success:
                # Validate downloaded file — detect bogus HTML error pages
                actual_size = dl.get('downloaded', 0)
                expected_size = filesize
                is_bogus = False
                if expected_size > 100_000 and actual_size < 50_000:
                    # Expected large file but got tiny → likely HTML error page
                    is_bogus = True
                elif actual_size < 1000 and not dl.get('is_torrent'):
                    # Sub-1KB "file" for a non-torrent download is suspicious
                    is_bogus = True

                if is_bogus:
                    # Remove the bogus file
                    dest = dl.get('dest_path', '')
                    if dest and os.path.isfile(dest):
                        try:
                            os.remove(dest)
                        except OSError:
                            pass
                    with _lock:
                        dl['status'] = 'failed'
                        dl['error'] = (
                            f'Downloaded file too small ({actual_size} bytes) — '
                            f'likely an error page, not the real file. '
                            f'Expected ~{expected_size} bytes.'
                            if expected_size
                            else f'Downloaded file too small ({actual_size} bytes) — '
                                 f'likely an error page.'
                        )
                        _save_state()
                    _emit('dl:update', _sanitize(dl))
                    _log_history(dl, 'failed')
                    _flush_state()
                    return

                with _lock:
                    dl['status'] = 'completed'
                    dl['progress'] = 100
                    dl['completed_at'] = time.time()
                    dl['retry_count'] = 0
                    _save_state()
                _emit('dl:update', _sanitize(dl))
                _emit('dl:completed', _dl_completed_payload(dl))
                _log_history(dl, 'completed')
                _flush_state()
                return

        except Exception as e:
            err_str = str(e)[:500]
            # Check if transient and retries remain
            if attempt < max_retries and _is_transient_error(err_str):
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                with _lock:
                    dl['status'] = 'resolving'  # visual: "retrying"
                    dl['error'] = f'Retry {attempt + 1}/{max_retries} in {delay}s: {err_str}'
                    dl['retry_count'] = attempt + 1
                _emit('dl:update', _sanitize(dl))
                time.sleep(delay)
                if dl.get('status') == 'cancelled':
                    _emit('dl:update', _sanitize(dl))
                    return
                continue
            # Non-transient or retries exhausted
            with _lock:
                dl['status'] = 'failed'
                dl['error'] = err_str
                _save_state()
            _emit('dl:update', _sanitize(dl))
            _log_history(dl, 'failed')
            _flush_state()
            return


def _calc_speed(dl):
    """Calculate download speed using sliding window (last ~5s) for responsiveness."""
    now = time.time()
    downloaded = dl.get('downloaded', 0)
    samples = dl.get('_speed_samples', [])
    samples.append((now, downloaded))
    # Keep last 10s of samples
    cutoff = now - 10
    samples = [(t, d) for t, d in samples if t >= cutoff]
    dl['_speed_samples'] = samples
    if len(samples) < 2:
        return 0
    # Use 5s window for calculation
    window = 5
    oldest = None
    for t, d in samples:
        if t >= now - window:
            oldest = (t, d)
            break
    if not oldest:
        oldest = samples[0]
    elapsed = now - oldest[0]
    if elapsed < 0.5:
        return 0
    return int((downloaded - oldest[1]) / elapsed)


def _calc_eta(dl):
    """Calculate ETA in seconds, or 0 if unknown."""
    speed = dl.get('speed', 0)
    if speed <= 0:
        return 0
    total = dl.get('filesize', 0)
    downloaded = dl.get('downloaded', 0)
    remaining = total - downloaded
    if remaining <= 0:
        return 0
    return int(remaining / speed)


def _get_category_for_file(filename, config):
    """Determine category and destination path based on file extension."""
    if not config.get('auto_categorize', True):
        return None, None
    
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    if not ext:
        return None, None
    
    categories = config.get('categories', [])
    for cat in categories:
        if ext in cat.get('extensions', []):
            # Found matching category
            # If cat path is absolute, use it. Else relative to default_dir
            cat_path = cat.get('path')
            if not cat_path:
                base_dir = config.get('default_dir', '/home')
                cat_path = os.path.join(base_dir, cat['name'])
            return cat['id'], _safe_path(cat_path)
            
    # No match found - use 'other' category if defined
    other = next((c for c in categories if c['id'] == 'other'), None)
    if other:
         cat_path = other.get('path')
         if not cat_path:
             base_dir = config.get('default_dir', '/home')
             cat_path = os.path.join(base_dir, other['name'])
         return other['id'], _safe_path(cat_path)

    return None, None


def _sanitize(dl):
    """Return a safe copy for JSON serialization."""
    d = {
        'id': dl.get('id'),
        'url': dl.get('url', ''),
        'filename': dl.get('filename', ''),
        'filesize': dl.get('filesize', 0),
        'downloaded': dl.get('downloaded', 0),
        'progress': dl.get('progress', 0),
        'speed': dl.get('speed', 0),
        'status': dl.get('status', 'pending'),
        'error': dl.get('error', ''),
        'debrid_error': dl.get('debrid_error', ''),
        'dest_dir': dl.get('dest_dir', ''),
        'dest_path': dl.get('dest_path', ''),
        'use_debrid': dl.get('use_debrid', True),
        'added_at': dl.get('added_at', 0),
        'started_at': dl.get('started_at', 0),
        'completed_at': dl.get('completed_at', 0),
        'is_torrent': dl.get('is_torrent', False),
        'priority': dl.get('priority', 0),
        'package_id': dl.get('package_id', ''),
        'eta': _calc_eta(dl),
        'retry_count': dl.get('retry_count', 0),
        'category_id': dl.get('category_id', ''),
    }
    if d['is_torrent']:
        d['torrent_status'] = dl.get('torrent_status', '')
        d['torrent_seeders'] = dl.get('torrent_seeders', 0)
        d['torrent_speed'] = dl.get('torrent_speed', 0)
        d['torrent_files_total'] = dl.get('torrent_files_total', 0)
        d['torrent_files_done'] = dl.get('torrent_files_done', 0)
    return d


def _dl_completed_payload(dl):
    """Build the payload sent with dl:completed events."""
    folder = dl.get('dest_dir') or dl.get('dest_path') or ''
    return {
        'id': dl.get('id'),
        'filename': dl.get('filename', ''),
        'filesize': dl.get('downloaded', 0),
        'folder': folder,
        'dest_dir': dl.get('dest_dir', ''),
        'dest_path': dl.get('dest_path', ''),
    }


# ─── Concurrent download manager ───

_active_threads = {}


def _start_next():
    """Start next pending download if under concurrency limit.
    Thread-safe: holds _lock while checking counts and claiming pending downloads.
    """
    # Use global config for max_concurrent (system-wide limit)
    config = _load_config(username=None)
    max_conc = config.get('max_concurrent', 3)

    with _lock:
        # Only count LOCAL downloads toward the concurrency limit.
        # Remote debrid operations (torrent_uploading, torrent_downloading)
        # happen on the debrid server and use no local bandwidth/disk,
        # so they should NOT block the queue.
        active = sum(1 for d in _downloads.values()
                     if d['status'] in ('downloading', 'resolving'))
        if active >= max_conc:
            return

        # Find next pending (highest priority first, then earliest added)
        pending = sorted(
            [d for d in _downloads.values() if d['status'] == 'pending'],
            key=lambda d: (-d.get('priority', 0), d.get('added_at', 0))
        )
        to_start = []
        for dl in pending:
            if active >= max_conc:
                break
            dl['status'] = 'resolving'  # claim immediately to prevent double-start
            to_start.append(dl['id'])
            active += 1

    # Start threads outside lock
    for dl_id in to_start:
        t = threading.Thread(target=_download_then_next, args=(dl_id,), daemon=True)
        _active_threads[dl_id] = t
        t.start()


def _download_then_next(dl_id):
    """Download, then trigger next queued download."""
    try:
        _download_worker(dl_id)
    except Exception as exc:
        # Safety net: mark download failed on unhandled crash
        logging.exception('[downloads] Worker crashed for %s', dl_id)
        with _lock:
            dl = _downloads.get(dl_id)
            if dl and dl['status'] not in ('completed', 'cancelled', 'failed'):
                dl['status'] = 'failed'
                dl['error'] = f'Unexpected error: {str(exc)[:300]}'
                _save_state()
        _emit('dl:update', _sanitize(dl) if dl else {})
    finally:
        with _lock:
            _active_threads.pop(dl_id, None)
        # Check if this completes a package
        _check_package_completion(dl_id)
        _start_next()


def _check_package_completion(dl_id):
    """Check if the completed download finishes a package, trigger auto-extract if needed."""
    with _lock:
        dl = _downloads.get(dl_id)
        if not dl or dl.get('status') != 'completed':
            return
        pkg_id = dl.get('package_id')
        if not pkg_id:
            return
        pkg = _packages.get(pkg_id)
        if not pkg or pkg.get('status') in ('extracting', 'extracted'):
            return
        # Check if ALL downloads in the package are completed
        all_done = all(
            _downloads.get(did, {}).get('status') == 'completed'
            for did in pkg.get('dl_ids', [])
        )
        if not all_done:
            return
        pkg['status'] = 'completed'
        _save_state()
    _emit('dl:package_update', _sanitize_package(pkg))
    # Auto-extract if enabled
    if pkg.get('auto_extract'):
        _enqueue_extraction(pkg_id)


def _sanitize_package(pkg):
    """Return a safe copy of package for JSON."""
    return {
        'id': pkg.get('id', ''),
        'name': pkg.get('name', ''),
        'dl_ids': pkg.get('dl_ids', []),
        'dest_dir': pkg.get('dest_dir', ''),
        'status': pkg.get('status', 'downloading'),
        'auto_extract': pkg.get('auto_extract', False),
        'delete_after_extract': pkg.get('delete_after_extract', False),
        'extract_password': '***' if pkg.get('extract_password') else '',
        'extract_error': pkg.get('extract_error', ''),
        'created_at': pkg.get('created_at', 0),
        'has_archives': pkg.get('has_archives', False),
    }


# ─── Deep extract ───

def _is_archive_file(filename):
    """Check if a filename looks like an archive."""
    fn_lower = filename.lower()
    for ext in ARCHIVE_EXTENSIONS:
        if fn_lower.endswith(ext):
            return True
    if RAR_PART_RE.search(fn_lower):
        return True
    return False


def _is_first_part(filepath):
    """For multi-part archives, return True only for the first part."""
    fn = os.path.basename(filepath).lower()
    # .part2.rar, .part3.rar → skip (not first)
    m = re.search(r'\.part(\d+)\.rar$', fn, re.IGNORECASE)
    if m:
        return int(m.group(1)) == 1
    # .r00, .r01 → skip, only .rar is first
    if re.search(r'\.r\d+$', fn, re.IGNORECASE):
        return False
    return True


def _extract_single(archive_path, dest_dir, password=''):
    """Extract a single archive. Returns (success, error_msg)."""
    fn_lower = archive_path.lower()
    cmd = None

    if fn_lower.endswith(('.rar',)) or RAR_PART_RE.search(fn_lower):
        # Use unrar or 7z for rar
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}']
        if password:
            cmd.append(f'-p{password}')
        else:
            cmd.append('-p-')  # no password, skip prompts
        cmd.append(archive_path)
    elif fn_lower.endswith('.7z'):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}']
        if password:
            cmd.append(f'-p{password}')
        cmd.append(archive_path)
    elif fn_lower.endswith('.zip'):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}']
        if password:
            cmd.append(f'-p{password}')
        cmd.append(archive_path)
    elif fn_lower.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar')):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}', archive_path]
    elif fn_lower.endswith(('.gz', '.bz2', '.xz')):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}', archive_path]
    elif fn_lower.endswith(('.cab', '.iso')):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}', archive_path]
    else:
        return False, f'Unsupported format: {os.path.basename(archive_path)}'

    try:
        # Auto-install 7z if missing
        from host import ensure_dep
        ok, msg = ensure_dep('7z', install=True)
        if not ok:
            return False, f'Missing 7z: {msg}'

        logging.info('[extract] cmd=%s', ' '.join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        logging.info('[extract] returncode=%d stdout=%.300s stderr=%.300s',
                     result.returncode, result.stdout.strip(), result.stderr.strip())
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            # Check for password errors
            if 'Wrong password' in err or 'incorrect password' in err.lower():
                return False, 'Invalid archive password'
            return False, err[:500]
        return True, ''
    except subprocess.TimeoutExpired:
        logging.error('[extract] Timeout extracting %s', archive_path)
        return False, 'Timeout — extraction took too long'
    except Exception as e:
        logging.exception('[extract] Error extracting %s', archive_path)
        return False, str(e)[:500]


def _deep_extract_dir(directory, password='', delete_after=False, max_depth=5):
    """Recursively extract all archives in directory.
    Returns (total_extracted, errors_list).
    """
    total_extracted = 0
    errors = []

    for depth in range(max_depth):
        # Find all archive files in directory tree
        archives_found = []
        for root, dirs, files in os.walk(directory):
            for fname in files:
                fpath = os.path.join(root, fname)
                if _is_archive_file(fname) and _is_first_part(fpath):
                    archives_found.append(fpath)

        if not archives_found:
            break  # No more archives

        extracted_this_round = 0
        for archive_path in archives_found:
            if not os.path.exists(archive_path):
                continue
            extract_dest = os.path.dirname(archive_path)
            ok, err = _extract_single(archive_path, extract_dest, password)
            if ok:
                extracted_this_round += 1
                total_extracted += 1
                if delete_after:
                    _delete_archive_parts(archive_path)
            else:
                errors.append(f'{os.path.basename(archive_path)}: {err}')

        if extracted_this_round == 0:
            break  # Nothing new extracted, stop recursion

    return total_extracted, errors


def _delete_archive_parts(archive_path):
    """Delete an archive and all its parts (for multi-part rar/zip)."""
    try:
        base = archive_path.lower()
        directory = os.path.dirname(archive_path)
        basename = os.path.basename(archive_path)

        # For partN.rar multi-part: delete all .partN.rar files
        m = re.match(r'(.*)\.part\d+\.rar$', basename, re.IGNORECASE)
        if m:
            prefix = m.group(1)
            for f in os.listdir(directory):
                if re.match(re.escape(prefix) + r'\.part\d+\.rar$', f, re.IGNORECASE):
                    try:
                        os.remove(os.path.join(directory, f))
                    except OSError:
                        pass
            return

        # For .rar + .r00, .r01 etc.
        if basename.lower().endswith('.rar'):
            stem = basename[:-4]
            for f in os.listdir(directory):
                if f.lower().startswith(stem.lower()) and (
                    f.lower().endswith('.rar') or re.search(r'\.r\d+$', f, re.IGNORECASE)
                ):
                    try:
                        os.remove(os.path.join(directory, f))
                    except OSError:
                        pass
            return

        # Single file archive
        try:
            os.remove(archive_path)
        except OSError:
            pass
    except Exception:
        pass


def _scan_for_archives(directory):
    """Check if a directory contains any archive files."""
    if not directory or not os.path.isdir(directory):
        return False
    for root, dirs, files in os.walk(directory):
        for fname in files:
            if _is_archive_file(fname):
                return True
    return False


def _extract_package_files(archive_paths, dest_dir, password='', delete_after=False):
    """Extract only the specified archive files.
    Returns (total_extracted, errors_list).
    """
    total_extracted = 0
    errors = []

    for archive_path in archive_paths:
        if not os.path.exists(archive_path):
            continue
        extract_dest = os.path.dirname(archive_path) or dest_dir
        ok, err = _extract_single(archive_path, extract_dest, password)
        if ok:
            total_extracted += 1
            if delete_after:
                _delete_archive_parts(archive_path)
        else:
            errors.append(f'{os.path.basename(archive_path)}: {err}')

    return total_extracted, errors


def _enqueue_extraction(package_id):
    """Add a package to the extraction queue and start the worker if not running."""
    global _extract_thread
    _extract_queue.append(package_id)
    logging.info('[extract] Enqueued %s (queue length: %d)', package_id, len(_extract_queue))
    # Start the extraction worker thread if not already running
    with _extract_start_lock:
        if _extract_thread is None or not _extract_thread.is_alive():
            _extract_thread = threading.Thread(target=_extraction_worker, daemon=True)
            _extract_thread.start()


def _extraction_worker():
    """Single worker thread that processes extraction queue sequentially."""
    while True:
        try:
            package_id = _extract_queue.popleft()
        except IndexError:
            logging.info('[extract] Queue empty, worker exiting')
            return
        logging.info('[extract] Starting extraction for %s', package_id)
        _extract_running.set()
        try:
            _run_package_extract(package_id)
        except Exception:
            logging.exception('[extract] Unhandled error extracting %s', package_id)
        finally:
            _extract_running.clear()


def _run_package_extract(package_id):
    """Run deep extraction for a package (called by _extraction_worker)."""
    with _lock:
        pkg = _packages.get(package_id)
        if not pkg:
            logging.warning('[extract] Package %s not found, skipping', package_id)
            return
        pkg['status'] = 'extracting'
        pkg['extract_error'] = ''
        _save_state()
        # Collect downloaded file paths belonging to this package
        pkg_files = []
        for did in pkg.get('dl_ids', []):
            dl = _downloads.get(did)
            if dl:
                fp = dl.get('dest_path') or dl.get('filepath') or ''
                if fp:
                    pkg_files.append(fp)
    _emit('dl:package_update', _sanitize_package(pkg))

    dest_dir = _safe_path(pkg.get('dest_dir', ''))
    password = pkg.get('extract_password', '')
    delete_after = pkg.get('delete_after_extract', False)

    logging.info('[extract] pkg=%s dest_dir=%s pkg_files=%s delete_after=%s',
                 package_id, dest_dir, pkg_files, delete_after)

    if not dest_dir or not os.path.isdir(dest_dir):
        err_msg = f'Target folder not found: {dest_dir}'
        logging.error('[extract] %s', err_msg)
        with _lock:
            pkg['status'] = 'extract_failed'
            pkg['extract_error'] = 'Target folder not found'
            _save_state()
        _emit('dl:package_update', _sanitize_package(pkg))
        return

    # Extract only archives from this package's files, not the whole directory
    pkg_archives = [f for f in pkg_files if os.path.isfile(f) and _is_archive_file(os.path.basename(f)) and _is_first_part(f)]
    logging.info('[extract] Found %d archives from pkg_files, fallback to deep=%s',
                 len(pkg_archives), not bool(pkg_archives))
    if pkg_archives:
        total, errors = _extract_package_files(pkg_archives, dest_dir, password, delete_after)
    else:
        total, errors = _deep_extract_dir(dest_dir, password, delete_after)

    logging.info('[extract] pkg=%s total=%d errors=%s', package_id, total, errors)

    with _lock:
        if errors and total == 0:
            pkg['status'] = 'extract_failed'
            pkg['extract_error'] = '; '.join(errors[:5])
        else:
            pkg['status'] = 'extracted'
            if errors:
                pkg['extract_error'] = f'Extracted {total}, errors: ' + '; '.join(errors[:3])
            else:
                pkg['extract_error'] = ''
        pkg['has_archives'] = _scan_for_archives(dest_dir)
        _save_state()
    _emit('dl:package_update', _sanitize_package(pkg))


# ─── API Routes ───

@downloads_bp.route('/api/downloads/stats')
def download_stats():
    """Return aggregate download statistics."""
    me = _get_username()
    with _lock:
        _my = [d for d in _downloads.values() if not me or d.get('user', '') == me or not d.get('user')]
        total = len(_my)
        active = sum(1 for d in _my
                     if d['status'] in ('downloading', 'resolving',
                                        'torrent_uploading', 'torrent_downloading'))
        completed = sum(1 for d in _my if d['status'] == 'completed')
        failed = sum(1 for d in _my if d['status'] == 'failed')
        paused = sum(1 for d in _my if d['status'] == 'paused')
        pending = sum(1 for d in _my if d['status'] == 'pending')
        total_bytes = sum(d.get('downloaded', 0) for d in _my
                         if d['status'] == 'completed')
        current_speed = 0
        for d in _my:
            if d['status'] == 'downloading':
                current_speed += d.get('speed', 0)
            elif d['status'] == 'torrent_downloading':
                current_speed += d.get('torrent_speed', 0)
        my_pkgs = sum(1 for p in _packages.values() if not me or p.get('user', '') == me or not p.get('user'))
    history = _load_history(username=me)
    now = time.time()
    periods = {
        'today': now - 86400,
        'week': now - 7 * 86400,
        'month': now - 30 * 86400,
    }
    bytes_by_period = {k: 0 for k in periods}
    counts = {'completed': 0, 'failed': 0, 'cancelled': 0}
    total_bytes_hist = 0
    total_duration = 0.0
    for h in history:
        event = h.get('event')
        ts = h.get('timestamp', 0) or 0
        size = h.get('filesize', 0) or 0
        duration = h.get('duration', 0) or 0
        if event == 'completed':
            counts['completed'] += 1
            total_bytes_hist += size
            if duration > 0:
                total_duration += duration
            for key, cutoff in periods.items():
                if ts >= cutoff:
                    bytes_by_period[key] += size
        elif event == 'failed':
            counts['failed'] += 1
        elif event == 'cancelled':
            counts['cancelled'] += 1
    avg_speed = int(total_bytes_hist / total_duration) if total_duration > 0 else 0
    return jsonify({
        'ok': True,
        'stats': {
            'total': total, 'active': active, 'completed': completed,
            'failed': failed, 'paused': paused, 'pending': pending,
            'total_bytes_downloaded': total_bytes,
            'current_speed': current_speed,
            'packages': my_pkgs,
        },
        'metrics': {
            'bytes': {**bytes_by_period, 'all_time': total_bytes_hist},
            'counts': counts,
            'average_speed': avg_speed,
            'history_entries': len(history),
        },
    })


@downloads_bp.route('/api/downloads/history')
def download_history():
    """Return download history log with search, filter, pagination."""
    me = _get_username()
    history = _load_history(username=me)
    # Return in reverse chronological order
    history.reverse()

    # Filtering
    q = request.args.get('q', '').lower()
    status = request.args.get('status', '')
    source = request.args.get('source', '')
    start_ts = request.args.get('start', type=float)
    end_ts = request.args.get('end', type=float)

    if q:
        history = [h for h in history if q in h.get('filename', '').lower() or q in h.get('url', '').lower()]

    if status:
        # status in history is 'event' (completed, failed, cancelled)
        history = [h for h in history if h.get('event') == status]

    if source:
        # source: torrent, direct. Debrid logic is complex (uses direct URL but originated from magnet/link)
        # simplistic check: is_torrent field
        if source == 'torrent':
            history = [h for h in history if h.get('is_torrent')]
        elif source == 'direct':
            history = [h for h in history if not h.get('is_torrent') and not h.get('use_debrid')]
        elif source == 'debrid':
            history = [h for h in history if h.get('use_debrid') and not h.get('is_torrent')]

    if start_ts:
        history = [h for h in history if h.get('timestamp', 0) >= start_ts]
    if end_ts:
        # end_ts is usually start of next day, so strictly less
        history = [h for h in history if h.get('timestamp', 0) < end_ts]

    total = len(history)
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    
    return jsonify({
        'ok': True, 
        'history': history[start_idx:end_idx],
        'total': total,
        'page': page,
        'limit': limit
    })


@downloads_bp.route('/api/downloads/history/clear', methods=['POST'])
def clear_history():
    """Clear download history for the current user."""
    me = _get_username()
    data = request.get_json(force=True)
    older_than_days = data.get('older_than_days')

    with _history_lock:
        if os.path.isfile(DOWNLOADS_HISTORY_FILE):
            try:
                with open(DOWNLOADS_HISTORY_FILE) as f:
                    history = json.load(f)
            except Exception:
                history = []
        else:
            history = []

        if older_than_days is not None:
            cutoff = time.time() - (int(older_than_days) * 86400)
            # Keep entries that belong to other users OR are newer than cutoff for current user
            history = [h for h in history if h.get('user') != me or h.get('timestamp', 0) > cutoff]
        else:
            # Remove only entries belonging to the current user
            history = [h for h in history if h.get('user') != me]

        _atomic_write_json(DOWNLOADS_HISTORY_FILE, history)

    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/history/retry', methods=['POST'])
def retry_history_download():
    """Retry a download from history."""
    data = request.get_json(force=True)
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    # Handle legacy torrent:// URLs — not retryable without original magnet/torrent
    if url.startswith('torrent://'):
        return jsonify({'error': 'Cannot retry: original torrent data no longer available. '
                        'Please re-add the magnet link or .torrent file.'}), 400

    dl_id = str(uuid.uuid4())[:8]
    is_t = _is_torrent(url)
    dest_dir = data.get('dest_dir')

    if not dest_dir:
        _cfg = _load_config()
        _default_key = 'default_dir_torrent' if is_t else 'default_dir'
        dest_dir = _cfg.get(_default_key, '/home')

    # Respect original debrid setting from history; default True for torrents
    use_debrid = data.get('use_debrid', True) if is_t else data.get('use_debrid', True)

    dl = {
        'id': dl_id,
        'url': url,
        'filename': data.get('filename', ''),
        'filesize': 0,
        'downloaded': 0,
        'progress': 0,
        'speed': 0,
        'status': 'pending',
        'error': '',
        'debrid_error': '',
        'dest_dir': dest_dir,
        'dest_path': '',
        'use_debrid': use_debrid,
        'added_at': time.time(),
        'started_at': 0,
        'completed_at': 0,
        'is_torrent': is_t,
        'package_id': '',
        'user': _get_username() or '',
    }

    with _lock:
        _downloads[dl_id] = dl
        _save_state()

    _emit('dl:update', _sanitize(dl))
    _start_next()
    return jsonify({'ok': True, 'id': dl_id})


@downloads_bp.route('/api/downloads/list')
def list_downloads():
    me = _get_username()
    # Sort: active first, then by priority (desc), then by added_at (desc)
    def _sort_key(d):
        status_order = {'downloading': 0, 'torrent_downloading': 0, 'torrent_uploading': 0,
                        'resolving': 0, 'paused': 1, 'pending': 2,
                        'completed': 3, 'failed': 4, 'cancelled': 5}
        return (status_order.get(d.get('status', ''), 9), -d.get('priority', 0), -d.get('added_at', 0))
    all_items = sorted(_downloads.values(), key=_sort_key)
    # Filter by user
    items = [d for d in all_items if not me or d.get('user', '') == me or not d.get('user')]
    pkgs = [_sanitize_package(p) for p in _packages.values()
            if not me or p.get('user', '') == me or not p.get('user')]
    return jsonify({'ok': True, 'items': [_sanitize(d) for d in items], 'packages': pkgs})


@downloads_bp.route('/api/downloads/add', methods=['POST'])
def add_download():
    data = request.get_json(force=True)
    urls = data.get('urls', [])
    url = data.get('url', '').strip()
    if url:
        urls = [url]
    if not urls:
        return jsonify({'error': 'No URL provided'}), 400

    # Validate URLs
    _valid_prefixes = ('http://', 'https://', 'ftp://', 'magnet:')
    invalid = [u for u in urls if not any(u.strip().lower().startswith(p) for p in _valid_prefixes)]
    if invalid:
        return jsonify({'error': f'Invalid link: {invalid[0][:80]}'}), 400

    dest_dir = data.get('dest_dir', '').strip()
    use_debrid = data.get('use_debrid', True)
    filename = data.get('filename', '').strip()

    # Package options (for multi-URL adds)
    package_name = data.get('package_name', '').strip()
    auto_extract = data.get('auto_extract', False)
    delete_after_extract = data.get('delete_after_extract', False)
    extract_password = data.get('extract_password', '')

    # Create package if multiple URLs
    package_id = ''
    pkg_folder = ''
    if len(urls) > 1 and package_name:
        package_id = 'pkg_' + str(uuid.uuid4())[:8]
        # Create subfolder for the package
        _cfg = _load_config()
        base_dir = dest_dir or _cfg.get('default_dir', '/home')
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', package_name)[:120]
        pkg_folder = os.path.join(base_dir, safe_name)
        real_pkg_folder = _safe_path(pkg_folder)
        if real_pkg_folder:
            os.makedirs(real_pkg_folder, exist_ok=True)
            pkg_folder = real_pkg_folder

    added = []
    dl_ids = []
    _cfg = _load_config()
    for u in urls:
        u = u.strip()
        if not u:
            continue
        dl_id = str(uuid.uuid4())[:8]
        is_t = _is_torrent(u)
        _default_key = 'default_dir_torrent' if is_t else 'default_dir'
        # Use package folder if available, otherwise default
        dl_dest = pkg_folder if pkg_folder else (dest_dir or _cfg.get(_default_key, '/home'))
        dl = {
            'id': dl_id,
            'url': u,
            'filename': filename if len(urls) == 1 else '',
            'filesize': 0,
            'downloaded': 0,
            'progress': 0,
            'speed': 0,
            'status': 'pending',
            'error': '',
            'debrid_error': '',
            'dest_dir': dl_dest,
            'dest_path': '',
            'use_debrid': use_debrid,
            'added_at': time.time(),
            'started_at': 0,
            'completed_at': 0,
            'is_torrent': is_t,
            'package_id': package_id,
            'user': _get_username() or '',
        }
        with _lock:
            _downloads[dl_id] = dl
        dl_ids.append(dl_id)
        added.append(_sanitize(dl))
    # Batch save after all downloads added
    with _lock:
        _save_state()

    # Create package entry
    pkg_data = None
    if package_id and dl_ids:
        pkg = {
            'id': package_id,
            'name': package_name,
            'dl_ids': dl_ids,
            'dest_dir': pkg_folder or dest_dir or _load_config().get('default_dir', '/home'),
            'status': 'downloading',
            'auto_extract': bool(auto_extract),
            'delete_after_extract': bool(delete_after_extract),
            'extract_password': extract_password,
            'extract_error': '',
            'created_at': time.time(),
            'has_archives': False,
            'user': _get_username() or '',
        }
        with _lock:
            _packages[package_id] = pkg
            _save_state()
        pkg_data = _sanitize_package(pkg)

    _start_next()
    return jsonify({'ok': True, 'added': added, 'package': pkg_data})


@downloads_bp.route('/api/downloads/check-processed', methods=['POST'])
def check_processed_torrents():
    """Check if torrent filenames exist in watch_folder/processed/."""
    data = request.get_json(force=True)
    filenames = data.get('filenames', [])
    cfg = _load_config()
    watch_dir = _safe_path(cfg.get('watch_folder', ''))
    found = []
    if watch_dir:
        processed_dir = os.path.join(watch_dir, 'processed')
        if os.path.isdir(processed_dir):
            existing = set(os.listdir(processed_dir))
            for fn in filenames:
                if fn in existing:
                    found.append(fn)
    return jsonify({'ok': True, 'processed': found})


@downloads_bp.route('/api/downloads/add-torrent', methods=['POST'])
def add_torrent_file():
    """Upload .torrent file and add to download queue."""
    if 'file' not in request.files:
        return jsonify({'error': 'No .torrent file'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No .torrent file'}), 400

    dest_dir = request.form.get('dest_dir', '').strip()
    torrent_data = f.read()
    if len(torrent_data) > 5 * 1024 * 1024:  # Max 5 MB
        return jsonify({'error': '.torrent file too large'}), 400

    dl_id = str(uuid.uuid4())[:8]

    # Save torrent to cache for the worker thread
    cache_path = os.path.join(TORRENT_CACHE_DIR, f"{dl_id}.torrent")
    with open(cache_path, 'wb') as tf:
        tf.write(torrent_data)

    # Try to extract name from torrent bencode
    torrent_name = f.filename.replace('.torrent', '')
    try:
        # Simple bencode parsing for 'name' field
        idx = torrent_data.find(b'4:name')
        if idx >= 0:
            rest = torrent_data[idx + 6:]
            if rest[0:1].isdigit():
                colon = rest.index(b':')
                length = int(rest[:colon])
                torrent_name = rest[colon + 1:colon + 1 + length].decode('utf-8', errors='replace')
    except Exception:
        pass

    # Extract magnet URI for retry/resume support
    magnet_url = _extract_magnet_from_torrent(torrent_data)
    torrent_url = magnet_url or f'torrent://{torrent_name}'

    dl = {
        'id': dl_id,
        'url': torrent_url,
        'filename': torrent_name,
        'filesize': 0,
        'downloaded': 0,
        'progress': 0,
        'speed': 0,
        'status': 'pending',
        'error': '',
        'debrid_error': '',
        'dest_dir': dest_dir or _load_config().get('default_dir_torrent', '/home'),
        'dest_path': '',
        'use_debrid': True,
        'added_at': time.time(),
        'started_at': 0,
        'completed_at': 0,
        'is_torrent': True,
        'torrent_cache_path': cache_path,
        'user': _get_username() or '',
    }
    with _lock:
        _downloads[dl_id] = dl
        _save_state()

    _start_next()
    return jsonify({'ok': True, 'added': [_sanitize(dl)]})


@downloads_bp.route('/api/downloads/cancel', methods=['POST'])
def cancel_download():
    data = request.get_json(force=True)
    dl_id = data.get('id', '')
    to_log = False
    with _lock:
        dl = _downloads.get(dl_id)
        if not dl:
            return jsonify({'error': 'Not found'}), 404
        if dl['status'] in ('downloading', 'resolving', 'pending', 'paused',
                            'torrent_uploading', 'torrent_downloading'):
            if dl['status'] != 'cancelled':
                to_log = True
            dl['status'] = 'cancelled'
            _save_state()
    if to_log and dl:
        _log_history(dl, 'cancelled')
    _emit('dl:update', _sanitize(dl))
    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/pause', methods=['POST'])
def pause_download():
    data = request.get_json(force=True)
    dl_id = data.get('id', '')
    with _lock:
        dl = _downloads.get(dl_id)
        if not dl:
            return jsonify({'error': 'Not found'}), 404
        if dl['status'] in ('downloading', 'pending', 'torrent_downloading'):
            dl['status'] = 'paused'
            dl['speed'] = 0
            _save_state()
    _emit('dl:update', _sanitize(dl))
    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/resume', methods=['POST'])
def resume_download():
    data = request.get_json(force=True)
    dl_id = data.get('id', '')
    with _lock:
        dl = _downloads.get(dl_id)
        if not dl:
            return jsonify({'error': 'Not found'}), 404
        if dl['status'] == 'paused':
            # Thread already exited (new pause design); set to pending for _start_next
            dl['status'] = 'pending'
            dl['started_at'] = time.time()  # reset speed calc
            dl['speed'] = 0
            # Keep downloaded/progress/_actual_dest for Range resume
            _save_state()
    _emit('dl:update', _sanitize(dl))
    _start_next()
    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/retry', methods=['POST'])
def retry_download():
    data = request.get_json(force=True)
    dl_id = data.get('id', '')
    with _lock:
        dl = _downloads.get(dl_id)
        if not dl:
            return jsonify({'error': 'Not found'}), 404
        if dl['status'] in ('failed', 'cancelled'):
            # For failed downloads, keep _actual_dest for Range resume
            # For cancelled downloads, partial file was already deleted
            if dl['status'] == 'cancelled':
                dl.pop('_actual_dest', None)
                dl['progress'] = 0
                dl['downloaded'] = 0
            # else: keep downloaded/progress/_actual_dest for Range resume
            dl['status'] = 'pending'
            dl['error'] = ''
            dl['debrid_error'] = ''
            dl['speed'] = 0
            dl.pop('_speed_samples', None)
            _save_state()
    _emit('dl:update', _sanitize(dl))
    _start_next()
    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/reorder', methods=['POST'])
def reorder_download():
    """Move a download up in priority or reorder multiple."""
    data = request.get_json(force=True)

    # Bulk reorder
    if 'ordered_ids' in data:
        ordered_ids = data['ordered_ids']
        if not isinstance(ordered_ids, list):
            return jsonify({'error': 'Invalid format'}), 400
        
        with _lock:
            # Assign priorities: top item gets highest priority
            total = len(ordered_ids)
            updates = []
            for i, dl_id in enumerate(ordered_ids):
                dl = _downloads.get(dl_id)
                if dl and dl['status'] in ('pending', 'paused'):
                    # Priority = total - index (so first item has 'total', last has 1)
                    new_prio = total - i
                    if dl.get('priority') != new_prio:
                        dl['priority'] = new_prio
                        updates.append(dl)
            
            if updates:
                _save_state()
                # Notify clients about changes
                for dl in updates:
                    _emit('dl:update', _sanitize(dl))
                    
        return jsonify({'ok': True})

    dl_id = data.get('id', '')
    direction = data.get('direction', 'up')  # 'up' = higher priority, 'down' = lower
    with _lock:
        dl = _downloads.get(dl_id)
        if not dl:
            return jsonify({'error': 'Not found'}), 404
        if dl['status'] not in ('pending', 'paused'):
            return jsonify({'error': 'Can only reorder pending items'}), 400
        current = dl.get('priority', 0)
        if direction == 'up':
            dl['priority'] = current + 1
        elif direction == 'top':
            max_p = max((d.get('priority', 0) for d in _downloads.values()), default=0)
            dl['priority'] = max_p + 1
        else:
            dl['priority'] = max(0, current - 1)
        _save_state()
    _emit('dl:update', _sanitize(dl))
    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/remove', methods=['POST'])
def remove_download():
    data = request.get_json(force=True)
    dl_id = data.get('id', '')
    to_log = False
    with _lock:
        dl = _downloads.pop(dl_id, None)
        if not dl:
            return jsonify({'error': 'Not found'}), 404
        if dl['status'] in ('downloading', 'resolving', 'paused',
                            'torrent_uploading', 'torrent_downloading'):
            if dl.get('status') != 'cancelled':
                to_log = True
            dl['status'] = 'cancelled'
        _save_state()
    if to_log and dl:
        _log_history(dl, 'cancelled')
    _emit('dl:removed', {'id': dl_id})
    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/clear', methods=['POST'])
def clear_downloads():
    """Clear completed downloads."""
    with _lock:
        to_remove = [k for k, v in _downloads.items() if v['status'] == 'completed']
        for k in to_remove:
            del _downloads[k]
        _save_state()
    return jsonify({'ok': True, 'removed': len(to_remove)})


@downloads_bp.route('/api/downloads/extract', methods=['POST'])
def extract_package():
    """Trigger deep extraction for a package or single download."""
    err = require_tools('7z')
    if err:
        return err
    data = request.get_json(force=True)
    package_id = data.get('package_id', '')
    password = data.get('password', '')
    delete_after = data.get('delete_after', False)

    if not package_id:
        # Single download extract — create ad-hoc "package"
        dl_id = data.get('id', '')
        with _lock:
            dl = _downloads.get(dl_id)
            if not dl:
                return jsonify({'error': 'Not found'}), 404
            if dl.get('status') != 'completed':
                return jsonify({'error': 'Download not completed'}), 400
            dest = dl.get('dest_dir', '')
            if not dest:
                return jsonify({'error': 'Target folder not found'}), 400
        # Create temporary package for this single download
        package_id = 'pkg_' + str(uuid.uuid4())[:8]
        pkg = {
            'id': package_id,
            'name': dl.get('filename', 'Extraction'),
            'dl_ids': [dl_id],
            'dest_dir': dest,
            'status': 'downloading',
            'auto_extract': False,
            'delete_after_extract': delete_after,
            'extract_password': password,
            'extract_error': '',
            'created_at': time.time(),
            'has_archives': True,
        }
        with _lock:
            dl['package_id'] = package_id
            _packages[package_id] = pkg
            _save_state()
        _emit('dl:package_update', _sanitize_package(pkg))

    with _lock:
        pkg = _packages.get(package_id)
        if not pkg:
            return jsonify({'error': 'Package not found'}), 404
        if pkg.get('status') == 'extracting':
            return jsonify({'error': 'Extraction already in progress'}), 400
        # Update password/delete if provided
        if password:
            pkg['extract_password'] = password
        if delete_after is not None:
            pkg['delete_after_extract'] = bool(delete_after)

    _enqueue_extraction(package_id)
    return jsonify({'ok': True, 'package_id': package_id})


@downloads_bp.route('/api/downloads/package/remove', methods=['POST'])
def remove_package():
    """Remove a package (not the downloads themselves)."""
    data = request.get_json(force=True)
    pkg_id = data.get('package_id', '')
    with _lock:
        pkg = _packages.pop(pkg_id, None)
        if not pkg:
            return jsonify({'error': 'Not found'}), 404
        # Clear package_id from all related downloads
        for dl_id in pkg.get('dl_ids', []):
            dl = _downloads.get(dl_id)
            if dl:
                dl['package_id'] = ''
        _save_state()
    _emit('dl:package_removed', {'id': pkg_id})
    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/config')
def get_config():
    cfg = _load_config()
    # Mask API keys
    safe = dict(cfg)
    for k in ('alldebrid_api_key', 'realdebrid_api_key', 'premiumize_api_key', 'debridlink_api_key', 'torbox_api_key'):
        if safe.get(k):
            safe[k] = safe[k][:4] + '***' + safe[k][-4:]
    return jsonify({'ok': True, 'config': safe})


@downloads_bp.route('/api/downloads/config', methods=['PUT'])
def set_config():
    data = request.get_json(force=True)
    cfg = _load_config()

    if 'default_dir' in data:
        cfg['default_dir'] = data['default_dir']
    if 'default_dir_torrent' in data:
        cfg['default_dir_torrent'] = data['default_dir_torrent']
    if 'watch_folder' in data:
        cfg['watch_folder'] = data['watch_folder']
    if 'watch_folder_enabled' in data:
        cfg['watch_folder_enabled'] = bool(data['watch_folder_enabled'])
    if 'max_concurrent' in data:
        cfg['max_concurrent'] = max(1, min(10, int(data['max_concurrent'])))
    if 'overwrite_existing' in data:
        cfg['overwrite_existing'] = bool(data['overwrite_existing'])
    if 'speed_limit' in data:
        cfg['speed_limit'] = max(0, int(data['speed_limit']))
    if 'debrid_service' in data:
        cfg['debrid_service'] = data['debrid_service']
    if 'auto_categorize' in data:
        cfg['auto_categorize'] = bool(data['auto_categorize'])
    if 'categories' in data:
        cfg['categories'] = data['categories']

    # Only update API keys if new value provided (not masked)
    for key in ('alldebrid_api_key', 'realdebrid_api_key', 'premiumize_api_key', 'debridlink_api_key', 'torbox_api_key'):
        if key in data and data[key] and '***' not in data[key]:
            cfg[key] = data[key]

    _save_config(cfg)
    # Restart watch folder if settings changed
    if 'watch_folder' in data or 'watch_folder_enabled' in data:
        _start_watch_folder()
    return jsonify({'ok': True})


@downloads_bp.route('/api/downloads/test-debrid', methods=['POST'])
def test_debrid():
    """Test debrid API key by checking account info."""
    data = request.get_json(force=True)
    service = data.get('service', '')
    api_key = data.get('api_key', '')

    if not api_key:
        return jsonify({'ok': False, 'error': 'API key missing'})

    return _do_test_debrid(service, api_key)


@downloads_bp.route('/api/downloads/test-saved-debrid', methods=['POST'])
def test_saved_debrid():
    """Test the already-saved debrid API key."""
    data = request.get_json(force=True)
    service = data.get('service', '')
    cfg = _load_config()
    api_key = cfg.get(f'{service}_api_key', '')
    if not api_key:
        return jsonify({'ok': False, 'error': 'No saved API key for this service'})
    return _do_test_debrid(service, api_key)


def _do_test_debrid(service, api_key):
    """Shared debrid test logic."""
    try:
        if service == 'alldebrid':
            result = _http_get_json(
                f"https://api.alldebrid.com/v4/user?agent=EthOS&apikey={urllib.parse.quote(api_key)}"
            )
            if result.get('status') == 'success':
                user = result.get('data', {}).get('user', {})
                return jsonify({'ok': True, 'info': f"User: {user.get('username', '?')}, Premium: {'Yes' if user.get('isPremium') else 'No'}"})
            return jsonify({'ok': False, 'error': result.get('error', {}).get('message', 'Error')})

        elif service == 'realdebrid':
            result = _http_get_json(
                "https://api.real-debrid.com/rest/1.0/user",
                headers={'Authorization': f'Bearer {api_key}'}
            )
            if result.get('username'):
                prem = 'Yes' if result.get('premium', 0) > 0 else 'No'
                return jsonify({'ok': True, 'info': f"User: {result['username']}, Premium: {prem}"})
            return jsonify({'ok': False, 'error': 'Invalid key'})

        elif service == 'premiumize':
            result = _http_get_json(
                f"https://www.premiumize.me/api/account/info?apikey={urllib.parse.quote(api_key)}"
            )
            if result.get('status') == 'success':
                return jsonify({'ok': True, 'info': f"User: {result.get('customer_id', '?')}, Premium: {'Yes' if result.get('premium_until') else 'No'}"})
            return jsonify({'ok': False, 'error': result.get('message', 'Error')})


        elif service == 'debridlink':
            result = _http_get_json(
                "https://debrid-link.com/api/v2/account/infos",
                headers={'Authorization': f'Bearer {api_key}'}
            )
            if result.get('success') and result.get('value'):
                val = result['value']
                prem = 'Yes' if val.get('premiumLeft', 0) > 0 else 'No'
                return jsonify({'ok': True, 'info': f"User: {val.get('pseudo', '?')}, Premium: {prem}"})
            return jsonify({'ok': False, 'error': result.get('error', 'Invalid key')})

        elif service == 'torbox':
            result = _http_get_json(
                "https://api.torbox.app/v1/api/user/me",
                headers={'Authorization': f'Bearer {api_key}'}
            )
            if result.get('success') and result.get('data'):
                d = result['data']
                prem = 'Yes' if d.get('plan', 0) > 0 else 'No'
                return jsonify({'ok': True, 'info': f"User: {d.get('email', '?')}, Premium: {prem}"})
            return jsonify({'ok': False, 'error': result.get('detail', 'Invalid key')})

        return jsonify({'ok': False, 'error': 'Unknown service'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]})


# ── Package: install / uninstall / status ──

def _downloads_on_uninstall(wipe):
    """Stop all download processes on uninstall."""
    # 1. Cancel all active/pending downloads
    with _lock:
        for dl in _downloads.values():
            if dl['status'] in ('downloading', 'resolving', 'pending',
                                'torrent_uploading', 'torrent_downloading'):
                dl['status'] = 'cancelled'
        _save_state()

    # 2. Stop watch folder monitor
    _watch_stop.set()

    # 3. Clear extraction queue
    _extract_queue.clear()

    # 4. Stop state saver loop
    _saver_stop.set()

    log.info('[downloads] All processes stopped (uninstall, wipe=%s)', wipe)


register_pkg_routes(
    downloads_bp,
    install_message='Download Manager ready.',
    wipe_files=[DOWNLOADS_STATE_FILE, DOWNLOADS_CONFIG_FILE,
                DOWNLOADS_PACKAGES_FILE, DOWNLOADS_HISTORY_FILE],
    wipe_dirs=[TORRENT_CACHE_DIR],
    status_extras=lambda: {'configured': os.path.isfile(DOWNLOADS_CONFIG_FILE)},
    url_prefix='/api/downloads',
    on_uninstall=_downloads_on_uninstall,
)
