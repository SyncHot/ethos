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


# ─── API Routes ───


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

# ─── Load sub-modules (they register additional routes on downloads_bp) ───
from blueprints import downloads_config, downloads_debrid, downloads_extract, downloads_history, downloads_worker, downloads_torrent  # noqa: E402
