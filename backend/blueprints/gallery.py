"""
EthOS — Gallery backend blueprint.
Manages gallery folders, scans media recursively, serves EXIF data & video thumbnails.
"""

import os
import json
import hashlib
import time
import re
import subprocess
import secrets
import zipfile
import tempfile
import threading
import shutil
from flask import Blueprint, request, jsonify, send_file, g
from functools import wraps

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import app_path, data_path, user_data_path, NATIVE_MODE, ensure_dep, get_user_home, \
    get_photo_folders, get_all_photo_folder_variants
from utils import load_json as _load_json, save_json as _save_json, \
    safe_path as _safe_path_util, get_username as _get_username, DATA_ROOT, \
    ALLOWED_ROOTS, register_pkg_routes, require_tools, check_tool
from blueprints.admin_required import admin_required

gallery_bp = Blueprint('gallery', __name__, url_prefix='/api/gallery')

_GALLERY_CONFIG_GLOBAL = data_path('gallery_folders.json')
GALLERY_CACHE = data_path('gallery_cache.json')
_FAVORITES_GLOBAL = data_path('gallery_favorites.json')
FOLDER_PASSWORDS_FILE = data_path('folder_passwords.json')
THUMB_CACHE_DIR = data_path('.thumb_cache')
VIDEO_THUMB_DIR = data_path('.thumb_cache/video')


def _gallery_config_file():
    u = _get_username()
    if u:
        return user_data_path('gallery_folders.json', u)
    return _GALLERY_CONFIG_GLOBAL


def _favorites_file():
    u = _get_username()
    if u:
        return user_data_path('gallery_favorites.json', u)
    return _FAVORITES_GLOBAL

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.heic', '.heif', '.avif'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.3gp', '.wmv', '.flv', '.ts'}
RAW_EXTS   = {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.raf'}

os.makedirs(VIDEO_THUMB_DIR, exist_ok=True)

_gallery_lock = threading.Lock()
_scan_cache = {}  # key -> {'data': ..., 'time': float}
_SCAN_CACHE_TTL = 30  # seconds

# ─── Helpers ──────────────────────────────────────────────

def _safe_path(user_path):
    if not user_path:
        return None
    # Support absolute paths without forcing them under DATA_ROOT
    if os.path.isabs(user_path):
        target = os.path.realpath(user_path)
    else:
        base = os.path.realpath(DATA_ROOT)
        target = os.path.realpath(os.path.join(base, user_path))

    # Ensure path is within allowed roots
    if any(target == r or target.startswith(r + '/') for r in ALLOWED_ROOTS):
        return target
    return None


def _load_gallery_folders():
    fp = _gallery_config_file()
    if os.path.isfile(fp):
        try:
            with open(fp) as f:
                return json.load(f)
        except Exception:
            pass
    # Config doesn't exist yet — seed with default home folders
    return _seed_default_folders(fp)


def _seed_default_folders(config_path):
    """Auto-add photo/video home folders as gallery sources for a new user.

    Detects all localized variants (Zdjęcia, Photos, Fotos, …) so the gallery
    works regardless of the language that was active when the user was created.
    Prefers the current-language folder but falls back to any known variant.
    """
    u = _get_username()
    if not u:
        return []
    home = get_user_home(u)
    all_photos, all_videos = get_all_photo_folder_variants()
    cur_photos, cur_videos = get_photo_folders()

    defaults = []
    # Photos folder — prefer current language, fallback to any variant
    for candidate in [cur_photos] + sorted(all_photos - {cur_photos}):
        p = os.path.join(home, candidate)
        if os.path.isdir(p):
            defaults.append({'path': p, 'label': candidate, 'added': time.time()})
            break
    # Videos folder — same logic
    for candidate in [cur_videos] + sorted(all_videos - {cur_videos}):
        p = os.path.join(home, candidate)
        if os.path.isdir(p):
            defaults.append({'path': p, 'label': candidate, 'added': time.time()})
            break

    if defaults:
        try:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                json.dump(defaults, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return defaults


def _save_gallery_folders(folders):
    fp = _gallery_config_file()
    with open(fp, 'w') as f:
        json.dump(folders, f, ensure_ascii=False, indent=2)


def _load_favorites():
    fp = _favorites_file()
    if os.path.isfile(fp):
        try:
            with open(fp) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_favorites(favs):
    fp = _favorites_file()
    with open(fp, 'w') as f:
        json.dump(favs, f, ensure_ascii=False, indent=2)


def _is_media(name):
    ext = os.path.splitext(name)[1].lower()
    return ext in IMAGE_EXTS or ext in VIDEO_EXTS or ext in RAW_EXTS


def _media_type(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in RAW_EXTS:
        return 'raw'
    return 'image'


def _get_exif(real_path):
    """Extract EXIF data from image using Pillow."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
        img = Image.open(real_path)
        exif_data = img._getexif()
        if not exif_data:
            return {'width': img.width, 'height': img.height}
        result = {'width': img.width, 'height': img.height}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if isinstance(tag, str):
                # Only include useful string/numeric tags
                if isinstance(value, (str, int, float)):
                    result[tag] = value
                elif isinstance(value, bytes):
                    try:
                        result[tag] = value.decode('utf-8', errors='ignore')
                    except Exception:
                        pass
                elif isinstance(value, tuple) and len(value) <= 6:
                    result[tag] = [float(v) if hasattr(v, 'numerator') else v for v in value]
        # Parse GPS
        gps_info = exif_data.get(34853) or {}
        if gps_info:
            gps = {}
            for key in gps_info:
                decode = GPSTAGS.get(key, key)
                gps[decode] = gps_info[key]
            if 'GPSLatitude' in gps and 'GPSLongitude' in gps:
                try:
                    lat = _gps_to_decimal(gps['GPSLatitude'], gps.get('GPSLatitudeRef', 'N'))
                    lon = _gps_to_decimal(gps['GPSLongitude'], gps.get('GPSLongitudeRef', 'E'))
                    result['gps'] = {'lat': lat, 'lon': lon}
                except Exception:
                    pass
        return result
    except Exception:
        return {}


def _gps_to_decimal(coords, ref):
    d = float(coords[0])
    m = float(coords[1])
    s = float(coords[2])
    result = d + m / 60 + s / 3600
    if ref in ('S', 'W'):
        result = -result
    return round(result, 6)


THUMBS_DIR_NAME = '.thumbs'


def _video_thumbnail(real_path, cache_key):
    """Generate a thumbnail for a video file using ffmpeg.
    Tries to store in local .thumbs/ dir first, falls back to centralized cache.
    """
    # Check local .thumbs/ first
    parent = os.path.dirname(real_path)
    basename = os.path.basename(real_path)
    name, _ = os.path.splitext(basename)
    local_thumbs = os.path.join(parent, THUMBS_DIR_NAME)
    local_path = os.path.join(local_thumbs, name + '_video.webp')

    if os.path.isfile(local_path):
        return local_path

    # Check centralized cache
    out_path = os.path.join(VIDEO_THUMB_DIR, cache_key + '.webp')
    if os.path.isfile(out_path):
        return out_path

    try:
        # Ensure ffmpeg is available
        ensure_dep('ffmpeg', install=True)

        # Generate thumbnail – try local first
        try:
            os.makedirs(local_thumbs, exist_ok=True)
            target_path = local_path
        except OSError:
            target_path = out_path

        subprocess.run([
            'ffmpeg', '-y', '-i', real_path,
            '-vf', 'thumbnail,scale=640:-2',
            '-frames:v', '1',
            '-q:v', '8',
            target_path
        ], capture_output=True, timeout=15)
        if os.path.isfile(target_path):
            return target_path
    except Exception:
        pass
    return None


def _video_duration(real_path):
    """Get video duration in seconds using ffprobe."""
    try:
        ensure_dep('ffprobe', install=True)
        result = subprocess.run([
            'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', real_path
        ], capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except Exception:
        return None


# ─── Gallery Folder Management ──────────────────────────────

@gallery_bp.route('/folders')
def gallery_folders_list():
    """List all gallery folders."""
    folders = _load_gallery_folders()
    result = []
    for f in folders:
        real = _safe_path(f['path'])
        exists = real is not None and os.path.isdir(real)
        count = 0
        if exists:
            try:
                count = sum(1 for _ in _iter_media(real, max_depth=10))
            except Exception:
                pass
        result.append({**f, 'exists': exists, 'media_count': count})
    return jsonify(result)


@gallery_bp.route('/folders', methods=['POST'])
def gallery_folders_add():
    """Add a folder to the gallery."""
    data = request.get_json(force=True)
    path = data.get('path', '').strip().rstrip('/')
    label = data.get('label', '') or os.path.basename(path) or path
    if not path:
        return jsonify({'error': 'Path required'}), 400
    real = _safe_path(path)
    if not real or not os.path.isdir(real):
        return jsonify({'error': 'Folder not found'}), 404
    folders = _load_gallery_folders()
    if any(f['path'] == path for f in folders):
        return jsonify({'error': 'Folder already added'}), 409
    with _gallery_lock:
        folders = _load_gallery_folders()
        if any(f['path'] == path for f in folders):
            return jsonify({'error': 'Folder already added'}), 409
        folders.append({'path': path, 'label': label, 'added': time.time()})
        _save_gallery_folders(folders)
    return jsonify({'ok': True, 'folders': folders})


@gallery_bp.route('/folders', methods=['DELETE'])
def gallery_folders_remove():
    """Remove a folder from the gallery."""
    data = request.get_json(force=True)
    path = data.get('path', '').strip().rstrip('/')
    with _gallery_lock:
        folders = _load_gallery_folders()
        folders = [f for f in folders if f['path'] != path]
        _save_gallery_folders(folders)
    return jsonify({'ok': True, 'folders': folders})


# ─── Media Scanning ─────────────────────────────────────────

def _load_folder_passwords_gallery():
    """Load password-protected folder paths (shared with app.py)."""
    return set(_load_json(FOLDER_PASSWORDS_FILE, {}).keys())


def _is_path_protected(real_path, protected_set):
    """Check if a real path falls under a password-protected folder."""
    # Use data_path() to determine if we are inside internal storage
    base_real = os.path.realpath(data_path())
    
    # If path is inside internal storage, make it relative (e.g. /photos)
    # to match how passwords are likely stored for internal folders.
    if real_path == base_real or real_path.startswith(base_real + '/'):
        rel = '/' + os.path.relpath(real_path, base_real)
    else:
        # For external paths (or anything outside data_path), use the absolute path.
        rel = real_path

    check = rel.rstrip('/')
    while check and check != '/':
        if check in protected_set:
            return True
        check = os.path.dirname(check)
    return '/' in protected_set


def _iter_media(real_dir, max_depth=10, _depth=0, _protected=None):
    """Recursively yield media file paths, skipping protected folders."""
    if _depth > max_depth:
        return
    if _protected is None:
        _protected = _load_folder_passwords_gallery()
    try:
        for entry in os.scandir(real_dir):
            if entry.name.startswith('.'):
                continue
            if entry.is_dir(follow_symlinks=False):
                # Skip password-protected directories
                if _protected and _is_path_protected(entry.path, _protected):
                    continue
                yield from _iter_media(entry.path, max_depth, _depth + 1, _protected)
            elif entry.is_file() and _is_media(entry.name):
                yield entry
    except PermissionError:
        pass


@gallery_bp.route('/scan')
def gallery_scan():
    """Scan all gallery folders and return media items sorted by date.
    Query params:
        offset    (int): pagination offset
        limit     (int): items per page (default 100, max 500)
        folder    (str): filter to a specific gallery source folder path
        subfolder (str): filter to a specific subfolder (album path, e.g. /home/x/Photos/vacation)
        type      (str): 'image', 'video', 'all' (default: 'all')
        sort      (str): 'date_desc', 'date_asc', 'name', 'size' (default: date_desc)
        q         (str): search query (filename substring)
        month     (str): filter by year-month, e.g. '2024-03'
    """
    offset = request.args.get('offset', 0, type=int)
    limit = min(request.args.get('limit', 100, type=int), 500)
    folder_filter = request.args.get('folder', '')
    subfolder_filter = request.args.get('subfolder', '').rstrip('/')
    type_filter = request.args.get('type', 'all')
    sort_by = request.args.get('sort', 'date_desc')
    query = request.args.get('q', '').lower().strip()
    month_filter = request.args.get('month', '').strip()

    # Check scan cache
    cache_key = f"{folder_filter}|{subfolder_filter}|{type_filter}|{sort_by}|{query}|{month_filter}"
    cached = _scan_cache.get(cache_key)
    if cached and time.time() - cached['time'] < _SCAN_CACHE_TTL:
        all_items = cached['data']
        total = len(all_items)
        page = all_items[offset:offset + limit]
        return jsonify({'total': total, 'offset': offset, 'limit': limit, 'items': page})

    folders = _load_gallery_folders()
    if folder_filter:
        folders = [f for f in folders if f['path'] == folder_filter]

    # If subfolder requested, scan only that specific directory
    if subfolder_filter:
        real_sub = _safe_path(subfolder_filter)
        if real_sub and os.path.isdir(real_sub):
            folders = [{'path': subfolder_filter, 'label': os.path.basename(subfolder_filter)}]

    items = []
    protected = _load_folder_passwords_gallery()
    for folder_def in folders:
        real = _safe_path(folder_def['path'])
        if not real or not os.path.isdir(real):
            continue
        # Skip entire gallery source if it is password-protected
        if protected and _is_path_protected(real, protected):
            continue
        base_real = os.path.realpath(os.path.join(DATA_ROOT, ''))
        for entry in _iter_media(real, _protected=protected):
            try:
                stat = entry.stat()
                mtype = _media_type(entry.name)
                if type_filter != 'all' and mtype != type_filter:
                    continue
                if query and query not in entry.name.lower():
                    continue
                # Filter by year-month
                if month_filter:
                    t = time.localtime(stat.st_mtime)
                    item_month = f'{t.tm_year}-{t.tm_mon:02d}'
                    if item_month != month_filter:
                        continue
                # Build user-visible path
                rel = os.path.relpath(entry.path, base_real)
                items.append({
                    'name': entry.name,
                    'path': '/' + rel,
                    'folder': folder_def['path'],
                    'folder_label': folder_def.get('label', ''),
                    'type': mtype,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'album': os.path.basename(os.path.dirname(entry.path)),
                })
            except Exception:
                continue

    # Sort
    if sort_by == 'date_asc':
        items.sort(key=lambda x: x['modified'])
    elif sort_by == 'name':
        items.sort(key=lambda x: x['name'].lower())
    elif sort_by == 'size':
        items.sort(key=lambda x: -x['size'])
    else:  # date_desc
        items.sort(key=lambda x: -x['modified'])

    total = len(items)
    # Store in scan cache
    _scan_cache[cache_key] = {'data': items, 'time': time.time()}
    page = items[offset:offset + limit]
    return jsonify({'total': total, 'offset': offset, 'limit': limit, 'items': page})


@gallery_bp.route('/albums')
def gallery_albums():
    """Return albums (subdirectories) grouped from gallery folders."""
    folders = _load_gallery_folders()
    albums = {}
    protected = _load_folder_passwords_gallery()
    for folder_def in folders:
        real = _safe_path(folder_def['path'])
        if not real or not os.path.isdir(real):
            continue
        if protected and _is_path_protected(real, protected):
            continue
        base_real = os.path.realpath(os.path.join(DATA_ROOT, ''))
        for entry in _iter_media(real, _protected=protected):
            try:
                parent_real = os.path.dirname(entry.path)
                album_name = os.path.basename(parent_real)
                parent_rel = '/' + os.path.relpath(parent_real, base_real)
                key = parent_rel
                if key not in albums:
                    stat = entry.stat()
                    rel = '/' + os.path.relpath(entry.path, base_real)
                    albums[key] = {
                        'name': album_name,
                        'path': parent_rel,
                        'folder': folder_def['path'],
                        'count': 0,
                        'cover': rel,
                        'latest': stat.st_mtime,
                    }
                albums[key]['count'] += 1
                stat = entry.stat()
                if stat.st_mtime > albums[key]['latest']:
                    albums[key]['latest'] = stat.st_mtime
                    rel = '/' + os.path.relpath(entry.path, base_real)
                    albums[key]['cover'] = rel
            except Exception:
                continue

    result = sorted(albums.values(), key=lambda x: -x['latest'])
    return jsonify(result)


@gallery_bp.route('/exif')
def gallery_exif():
    """Return EXIF data for a specific file."""
    path = request.args.get('path', '')
    real = _safe_path(path)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'File not found'}), 404
    return jsonify(_get_exif(real))


@gallery_bp.route('/video-thumb')
def gallery_video_thumb():
    """Return a thumbnail for a video file."""
    err = require_tools('ffmpeg', 'ffprobe')
    if err:
        return err
    path = request.args.get('path', '')
    real = _safe_path(path)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'File not found'}), 404
    ext = os.path.splitext(real)[1].lower()
    if ext not in VIDEO_EXTS:
        return jsonify({'error': 'Not a video file'}), 400

    mtime = os.path.getmtime(real)
    cache_key = hashlib.md5(f'{real}:{mtime}'.encode()).hexdigest()

    thumb_path = _video_thumbnail(real, cache_key)
    if thumb_path and os.path.isfile(thumb_path):
        return send_file(thumb_path, mimetype='image/webp', max_age=86400)

    # Fallback: return a placeholder
    return jsonify({'error': 'Failed to generate thumbnail'}), 500


@gallery_bp.route('/video-info')
def gallery_video_info():
    """Return video metadata (duration, etc.)."""
    err = require_tools('ffmpeg', 'ffprobe')
    if err:
        return err
    path = request.args.get('path', '')
    real = _safe_path(path)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'File not found'}), 404
    duration = _video_duration(real)
    size = os.path.getsize(real)
    return jsonify({'duration': duration, 'size': size, 'path': path})


@gallery_bp.route('/timeline')
def gallery_timeline():
    """Return media grouped by date (year-month)."""
    folders = _load_gallery_folders()
    groups = {}
    base_real = os.path.realpath(os.path.join(DATA_ROOT, ''))
    protected = _load_folder_passwords_gallery()

    for folder_def in folders:
        real = _safe_path(folder_def['path'])
        if not real or not os.path.isdir(real):
            continue
        if protected and _is_path_protected(real, protected):
            continue
        for entry in _iter_media(real, _protected=protected):
            try:
                stat = entry.stat()
                t = time.localtime(stat.st_mtime)
                key = f'{t.tm_year}-{t.tm_mon:02d}'
                rel = '/' + os.path.relpath(entry.path, base_real)
                if key not in groups:
                    groups[key] = {'key': key, 'year': t.tm_year, 'month': t.tm_mon, 'count': 0, 'cover': rel}
                groups[key]['count'] += 1
            except Exception:
                continue

    result = sorted(groups.values(), key=lambda x: x['key'], reverse=True)
    return jsonify(result)


@gallery_bp.route('/browse')
def gallery_browse_folders():
    """Browse available folders on the NAS for adding to gallery."""
    path = request.args.get('path', '/')
    real = _safe_path(path)
    if not real or not os.path.isdir(real):
        return jsonify({'error': 'Invalid path'}), 400

    dirs = []
    media_count = 0
    try:
        for entry in sorted(os.scandir(real), key=lambda e: e.name.lower()):
            if entry.name.startswith('.'):
                continue
            if entry.is_dir(follow_symlinks=False):
                dirs.append(entry.name)
            elif entry.is_file() and _is_media(entry.name):
                media_count += 1
    except PermissionError:
        return jsonify({'error': 'Access denied'}), 403

    return jsonify({
        'path': path,
        'folders': dirs,
        'media_count': media_count,
        'is_gallery': any(f['path'] == path.rstrip('/') for f in _load_gallery_folders())
    })


# ─── Favorites ───────────────────────────────────────────────

@gallery_bp.route('/favorites')
def gallery_favorites_list():
    """Return list of favorite media paths with metadata."""
    favs = _load_favorites()
    base_real = os.path.realpath(os.path.join(DATA_ROOT, ''))
    items = []
    for fav in favs:
        real = _safe_path(fav['path'])
        if not real or not os.path.isfile(real):
            continue
        try:
            stat = os.stat(real)
            items.append({
                'name': os.path.basename(fav['path']),
                'path': fav['path'],
                'type': _media_type(fav['path']),
                'size': stat.st_size,
                'modified': stat.st_mtime,
                'added': fav.get('added', 0),
                'album': os.path.basename(os.path.dirname(fav['path'])),
            })
        except Exception:
            continue
    return jsonify({'items': items, 'total': len(items)})


@gallery_bp.route('/favorites', methods=['POST'])
def gallery_favorites_add():
    """Add a media file to favorites."""
    data = request.get_json(force=True)
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Path required'}), 400
    real = _safe_path(path)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'File not found'}), 404
    favs = _load_favorites()
    if any(f['path'] == path for f in favs):
        return jsonify({'ok': True, 'already': True})
    favs.insert(0, {'path': path, 'added': time.time()})
    _save_favorites(favs)
    return jsonify({'ok': True})


@gallery_bp.route('/favorites', methods=['DELETE'])
def gallery_favorites_remove():
    """Remove a media file from favorites."""
    data = request.get_json(force=True)
    path = data.get('path', '').strip()
    favs = _load_favorites()
    favs = [f for f in favs if f['path'] != path]
    _save_favorites(favs)
    return jsonify({'ok': True})


@gallery_bp.route('/favorites/check')
def gallery_favorites_check():
    """Check if a path is in favorites."""
    path = request.args.get('path', '').strip()
    favs = _load_favorites()
    is_fav = any(f['path'] == path for f in favs)
    return jsonify({'favorite': is_fav})


# ─── Statistics ──────────────────────────────────────────────

@gallery_bp.route('/stats')
def gallery_stats():
    """Scan all gallery folders and return aggregate statistics."""
    folders = _load_gallery_folders()
    protected = _load_folder_passwords_gallery()
    total_images = 0
    total_videos = 0
    total_raw = 0
    total_size = 0
    earliest = None
    latest = None
    formats = {}

    for folder_def in folders:
        real = _safe_path(folder_def['path'])
        if not real or not os.path.isdir(real):
            continue
        if protected and _is_path_protected(real, protected):
            continue
        for entry in _iter_media(real, _protected=protected):
            try:
                stat = entry.stat()
                mtype = _media_type(entry.name)
                if mtype == 'image':
                    total_images += 1
                elif mtype == 'video':
                    total_videos += 1
                elif mtype == 'raw':
                    total_raw += 1
                total_size += stat.st_size
                mtime = stat.st_mtime
                if earliest is None or mtime < earliest:
                    earliest = mtime
                if latest is None or mtime > latest:
                    latest = mtime
                ext = os.path.splitext(entry.name)[1].lower()
                formats[ext] = formats.get(ext, 0) + 1
            except Exception:
                continue

    total_files = total_images + total_videos + total_raw
    return jsonify({
        'total_images': total_images,
        'total_videos': total_videos,
        'total_raw': total_raw,
        'total_files': total_files,
        'total_size': total_size,
        'earliest': earliest,
        'latest': latest,
        'formats': formats,
    })


# ─── Duplicate Detection ────────────────────────────────────

@gallery_bp.route('/duplicates')
def gallery_duplicates():
    """Find duplicate files by size + name across gallery folders."""
    folders = _load_gallery_folders()
    protected = _load_folder_passwords_gallery()
    base_real = os.path.realpath(os.path.join(DATA_ROOT, ''))
    buckets = {}

    for folder_def in folders:
        real = _safe_path(folder_def['path'])
        if not real or not os.path.isdir(real):
            continue
        if protected and _is_path_protected(real, protected):
            continue
        for entry in _iter_media(real, _protected=protected):
            try:
                stat = entry.stat()
                if stat.st_size < 1024:
                    continue
                key = (entry.name.lower(), stat.st_size)
                rel = '/' + os.path.relpath(entry.path, base_real)
                buckets.setdefault(key, []).append(rel)
            except Exception:
                continue

    groups = []
    for (name, size), paths in buckets.items():
        if len(paths) > 1:
            groups.append({'name': name, 'size': size, 'paths': paths})
    groups.sort(key=lambda g: -g['size'])
    return jsonify(groups[:100])


# ─── Map Data (GPS Photos) ──────────────────────────────────

@gallery_bp.route('/map')
def gallery_map():
    """Return photos with GPS EXIF data for map display."""
    folders = _load_gallery_folders()
    protected = _load_folder_passwords_gallery()
    base_real = os.path.realpath(os.path.join(DATA_ROOT, ''))
    results = []

    for folder_def in folders:
        real = _safe_path(folder_def['path'])
        if not real or not os.path.isdir(real):
            continue
        if protected and _is_path_protected(real, protected):
            continue
        for entry in _iter_media(real, _protected=protected):
            if len(results) >= 500:
                break
            try:
                if _media_type(entry.name) != 'image':
                    continue
                exif = _get_exif(entry.path)
                gps = exif.get('gps')
                if not gps:
                    continue
                rel = '/' + os.path.relpath(entry.path, base_real)
                results.append({
                    'path': rel,
                    'name': entry.name,
                    'lat': gps['lat'],
                    'lon': gps['lon'],
                    'thumb_url': f'/api/gallery/file?path={rel}',
                })
            except Exception:
                continue
        if len(results) >= 500:
            break

    return jsonify(results)


# ─── Image Rotation ──────────────────────────────────────────

@gallery_bp.route('/rotate', methods=['POST'])
def gallery_rotate():
    """Rotate an image file by 90, 180, or 270 degrees."""
    data = request.get_json(force=True)
    path = data.get('path', '').strip()
    angle = data.get('angle', 0)
    if angle not in (90, 180, 270):
        return jsonify({'error': 'Angle must be 90, 180, or 270'}), 400
    real = _safe_path(path)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'File not found'}), 404
    if _media_type(os.path.basename(real)) != 'image':
        return jsonify({'error': 'Not an image file'}), 400
    try:
        from PIL import Image
        img = Image.open(real)
        rotated = img.rotate(-angle, expand=True)
        rotated.save(real)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── Public Sharing ──────────────────────────────────────────

SHARES_FILE = data_path('gallery_shares.json')


def _load_shares():
    return _load_json(SHARES_FILE, [])


def _save_shares(shares):
    _save_json(SHARES_FILE, shares)


@gallery_bp.route('/share', methods=['POST'])
def gallery_share_create():
    """Create a public share link for one or more files."""
    data = request.get_json(force=True)
    paths = data.get('paths') or []
    single = data.get('path', '').strip()
    if single and not paths:
        paths = [single]
    if not paths:
        return jsonify({'error': 'No paths provided'}), 400
    for p in paths:
        real = _safe_path(p)
        if not real or not os.path.isfile(real):
            return jsonify({'error': f'File not found: {p}'}), 404

    # Optional: share with specific users
    shared_with = data.get('shared_with', [])
    if isinstance(shared_with, list):
        shared_with = [u.strip() for u in shared_with if isinstance(u, str) and u.strip()]
    else:
        shared_with = []

    token = secrets.token_urlsafe(16)
    shares = _load_shares()
    shares.append({
        'token': token,
        'paths': paths,
        'created': time.time(),
        'expires': time.time() + 7 * 86400,
        'creator': _get_username() or '',
        'shared_with': shared_with,
    })
    _save_shares(shares)
    return jsonify({'ok': True, 'token': token, 'url': f'/api/gallery/shared/{token}'})


# NOTE: shared endpoint exempt from auth — handled in app.py
@gallery_bp.route('/shared/<token>')
def gallery_shared_serve(token):
    """Serve shared content by token. Auth required for user-targeted shares."""
    shares = _load_shares()
    share = next((s for s in shares if s['token'] == token), None)
    if not share:
        return jsonify({'error': 'Share not found'}), 404
    if time.time() > share.get('expires', 0):
        return jsonify({'error': 'Share expired'}), 410

    # If share is user-targeted, verify auth
    if share.get('shared_with'):
        me = _get_username()
        if not me:
            return jsonify({'error': 'Login required'}), 401
        if me not in share['shared_with'] and me != share.get('creator', ''):
            return jsonify({'error': 'Access denied'}), 403

    paths = share['paths']
    if len(paths) == 1:
        real = _safe_path(paths[0])
        if not real or not os.path.isfile(real):
            return jsonify({'error': 'File not found'}), 404
        return send_file(real)
    else:
        items = []
        for p in paths:
            real = _safe_path(p)
            if real and os.path.isfile(real):
                items.append({'path': p, 'name': os.path.basename(p)})
        return jsonify(items)


@gallery_bp.route('/share', methods=['DELETE'])
def gallery_share_delete():
    """Remove a share by token."""
    data = request.get_json(force=True)
    token = data.get('token', '').strip()
    if not token:
        return jsonify({'error': 'No token provided'}), 400
    shares = _load_shares()
    shares = [s for s in shares if s['token'] != token]
    _save_shares(shares)
    return jsonify({'ok': True})


@gallery_bp.route('/shares')
def gallery_shares_list():
    """List all active (non-expired) shares created by current user."""
    me = _get_username()
    shares = _load_shares()
    now = time.time()
    active = [s for s in shares if s.get('expires', 0) > now
              and (not me or s.get('creator', '') == me or not s.get('creator'))]
    return jsonify(active)


@gallery_bp.route('/shares/received')
def gallery_shares_received():
    """List gallery shares that other users shared with the current user."""
    me = _get_username()
    if not me:
        return jsonify([]), 200
    shares = _load_shares()
    now = time.time()
    result = []
    for s in shares:
        if s.get('expires', 0) <= now:
            continue
        if me not in s.get('shared_with', []):
            continue
        if s.get('creator', '') == me:
            continue
        result.append(s)
    return jsonify(result)


# ─── Custom Albums (per-user) ────────────────────────────────

_ALBUMS_GLOBAL = data_path('gallery_albums.json')


def _albums_file():
    u = _get_username()
    if u:
        return user_data_path('gallery_albums.json', u)
    return _ALBUMS_GLOBAL


def _load_albums():
    return _load_json(_albums_file(), [])


def _save_albums(albums):
    _save_json(_albums_file(), albums)


@gallery_bp.route('/custom-albums')
def gallery_custom_albums_list():
    """List all custom albums."""
    albums = _load_albums()
    return jsonify(albums)


@gallery_bp.route('/custom-albums', methods=['POST'])
def gallery_custom_albums_create():
    """Create a new custom album."""
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    description = data.get('description', '').strip()
    album_id = secrets.token_hex(8)
    albums = _load_albums()
    albums.append({
        'id': album_id,
        'name': name,
        'description': description,
        'paths': [],
        'created': time.time(),
    })
    _save_albums(albums)
    return jsonify({'ok': True, 'id': album_id})


@gallery_bp.route('/custom-albums', methods=['DELETE'])
def gallery_custom_albums_delete():
    """Delete a custom album by id."""
    data = request.get_json(force=True)
    album_id = data.get('id', '').strip()
    if not album_id:
        return jsonify({'error': 'Album id required'}), 400
    albums = _load_albums()
    albums = [a for a in albums if a['id'] != album_id]
    _save_albums(albums)
    return jsonify({'ok': True})


@gallery_bp.route('/custom-albums/add', methods=['POST'])
def gallery_custom_albums_add_items():
    """Add items to a custom album."""
    data = request.get_json(force=True)
    album_id = data.get('album_id', '').strip()
    paths = data.get('paths', [])
    if not album_id or not paths:
        return jsonify({'error': 'album_id and paths required'}), 400
    albums = _load_albums()
    album = next((a for a in albums if a['id'] == album_id), None)
    if not album:
        return jsonify({'error': 'Album not found'}), 404
    existing = set(album['paths'])
    for p in paths:
        if p not in existing:
            album['paths'].append(p)
            existing.add(p)
    _save_albums(albums)
    return jsonify({'ok': True})


@gallery_bp.route('/custom-albums/remove', methods=['POST'])
def gallery_custom_albums_remove_items():
    """Remove items from a custom album."""
    data = request.get_json(force=True)
    album_id = data.get('album_id', '').strip()
    paths = data.get('paths', [])
    if not album_id or not paths:
        return jsonify({'error': 'album_id and paths required'}), 400
    albums = _load_albums()
    album = next((a for a in albums if a['id'] == album_id), None)
    if not album:
        return jsonify({'error': 'Album not found'}), 404
    remove_set = set(paths)
    album['paths'] = [p for p in album['paths'] if p not in remove_set]
    _save_albums(albums)
    return jsonify({'ok': True})


@gallery_bp.route('/custom-albums/<album_id>')
def gallery_custom_album_get(album_id):
    """Get album items with metadata."""
    albums = _load_albums()
    album = next((a for a in albums if a['id'] == album_id), None)
    if not album:
        return jsonify({'error': 'Album not found'}), 404
    items = []
    for p in album['paths']:
        real = _safe_path(p)
        if not real or not os.path.isfile(real):
            continue
        try:
            stat = os.stat(real)
            items.append({
                'name': os.path.basename(p),
                'path': p,
                'type': _media_type(p),
                'size': stat.st_size,
                'modified': stat.st_mtime,
            })
        except Exception:
            continue
    return jsonify({
        'id': album['id'],
        'name': album['name'],
        'description': album.get('description', ''),
        'created': album.get('created', 0),
        'items': items,
        'total': len(items),
    })


# ─── Upload ──────────────────────────────────────────────────

@gallery_bp.route('/upload', methods=['POST'])
def gallery_upload():
    """Upload files to a gallery folder."""
    folder = request.form.get('folder', '').strip().rstrip('/')
    if not folder:
        return jsonify({'error': 'Target folder required'}), 400
    folders = _load_gallery_folders()
    if not any(f['path'] == folder for f in folders):
        return jsonify({'error': 'Not a gallery folder'}), 400
    real_folder = _safe_path(folder)
    if not real_folder or not os.path.isdir(real_folder):
        return jsonify({'error': 'Folder not found'}), 404
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files provided'}), 400
    uploaded = 0
    for f in files:
        if f.filename:
            safe_name = os.path.basename(f.filename)
            dest = os.path.join(real_folder, safe_name)
            f.save(dest)
            uploaded += 1
    return jsonify({'ok': True, 'uploaded': uploaded})


# ─── Batch Download (ZIP) ───────────────────────────────────

@gallery_bp.route('/download-zip', methods=['POST'])
def gallery_download_zip():
    """Download multiple files as a ZIP archive."""
    data = request.get_json(force=True)
    paths = data.get('paths', [])
    if not paths:
        return jsonify({'error': 'No paths provided'}), 400
    real_paths = []
    for p in paths:
        real = _safe_path(p)
        if not real or not os.path.isfile(real):
            return jsonify({'error': f'File not found: {p}'}), 404
        real_paths.append((p, real))

    tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
            seen_names = {}
            for user_path, real_path in real_paths:
                arcname = os.path.basename(user_path)
                count = seen_names.get(arcname, 0)
                if count > 0:
                    name, ext = os.path.splitext(arcname)
                    arcname = f'{name}_{count}{ext}'
                seen_names[os.path.basename(user_path)] = count + 1
                zf.write(real_path, arcname)
        tmp.close()
        return send_file(
            tmp.name,
            mimetype='application/zip',
            as_attachment=True,
            download_name='gallery_download.zip',
        )
    finally:
        # Schedule cleanup after response
        try:
            if os.path.isfile(tmp.name):
                os.unlink(tmp.name)
        except Exception:
            pass


# ── Package: install / uninstall / status ──

def _gallery_on_uninstall(wipe):
    if wipe:
        import glob as _g
        for pattern in ('gallery_folders_*.json', 'gallery_favorites_*.json'):
            for f in _g.glob(os.path.join(data_path(), pattern)):
                try:
                    os.remove(f)
                except Exception:
                    pass

@gallery_bp.route('/install', methods=['POST'])
@admin_required
def gallery_install():
    # Gallery has no system dependencies to install
    return jsonify({'status': 'ok'})


@gallery_bp.route('/uninstall', methods=['POST'])
@admin_required
def gallery_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    
    # 1. Custom uninstall logic
    _gallery_on_uninstall(wipe)
    
    # 2. Wipe files/dirs if requested
    if wipe:
        # Files
        for f in [GALLERY_CACHE, _GALLERY_CONFIG_GLOBAL, _FAVORITES_GLOBAL, FOLDER_PASSWORDS_FILE]:
            try:
                if os.path.isfile(f):
                    os.remove(f)
            except Exception:
                pass
        # Dirs
        for d in [THUMB_CACHE_DIR]:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                
    return jsonify({'ok': True})


@gallery_bp.route('/pkg-status', methods=['GET'])
def gallery_pkg_status():
    return jsonify({
        'installed': True,
        'configured': os.path.isfile(_GALLERY_CONFIG_GLOBAL),
        'ffmpeg': check_tool('ffmpeg'),
        'ffprobe': check_tool('ffprobe'),
    })
