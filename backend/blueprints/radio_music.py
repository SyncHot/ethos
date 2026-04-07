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
  GET  /api/radio-music/lyrics             - fetch song lyrics (?title=, ?artist=)
"""

import http.client
import json
import logging
import mimetypes
import os
import pathlib
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

from host import data_path, safe_path, q as shq

log = logging.getLogger('ethos.radio_music')

radio_music_bp = Blueprint('radio-music', __name__, url_prefix='/api/radio-music')

_DATA_DIR = data_path('radio_music')

_RADIO_API = 'https://de1.api.radio-browser.info'
_ITUNES_API = 'https://itunes.apple.com/search'

_MAX_HISTORY = 100

_AUDIO_EXTS = {'.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wav', '.wma', '.aac', '.webm', '.mp4', '.wv', '.ape'}
_DOWNLOAD_JOBS = {}  # job_id -> {status, progress, path, title, error}
_DOWNLOAD_LOCK = threading.Lock()

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


# ── Radio: search & browse ───────────────────────────────────

@radio_music_bp.route('/radio/search', methods=['GET'])
def radio_search():
    q_str = request.args.get('q', '').strip()
    country = request.args.get('country', '').strip()
    tag = request.args.get('tag', '').strip()
    limit = min(int(request.args.get('limit', 50)), 200)

    params = {
        'limit': limit * 3,  # fetch extra to aggregate duplicates
        'hidebroken': 'true',
        'order': 'clickcount',
        'reverse': 'true',
    }

    if q_str:
        params['name'] = q_str
    if country:
        params['countrycode'] = country.upper()
    if tag:
        params['tag'] = tag

    raw = _radio_api('/json/stations/search', params)
    items = _aggregate_stations(raw)
    return jsonify({'items': items[:limit]})


@radio_music_bp.route('/radio/countries', methods=['GET'])
def radio_countries():
    raw = _radio_api('/json/countrycodes', {'order': 'stationcount', 'reverse': 'true'})
    items = [{'code': c.get('name', ''), 'count': c.get('stationcount', 0)}
             for c in raw if c.get('stationcount', 0) > 0]
    return jsonify({'items': items})


@radio_music_bp.route('/radio/tags', methods=['GET'])
def radio_tags():
    raw = _radio_api('/json/tags', {'order': 'stationcount', 'reverse': 'true', 'limit': '80'})
    items = [{'name': t.get('name', ''), 'count': t.get('stationcount', 0)}
             for t in raw if t.get('stationcount', 0) > 50]
    return jsonify({'items': items})


@radio_music_bp.route('/radio/top', methods=['GET'])
def radio_top():
    limit = min(int(request.args.get('limit', 50)), 200)
    raw = _radio_api('/json/stations/topvote', {'limit': limit * 3, 'hidebroken': 'true'})
    items = _aggregate_stations(raw)
    return jsonify({'items': items[:limit]})


# ── Radio: favorites ─────────────────────────────────────────

@radio_music_bp.route('/radio/favorites', methods=['GET'])
def radio_favorites():
    return jsonify({'items': _load_json(_user_file('favorites.json'), [])})


@radio_music_bp.route('/radio/favorites', methods=['POST'])
def radio_favorites_edit():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    station = body.get('station')
    if not station or not station.get('uuid'):
        return jsonify({'error': 'Brak danych stacji.'}), 400

    favs = _load_json(_user_file('favorites.json'), [])

    if action == 'remove':
        favs = [f for f in favs if f.get('uuid') != station['uuid']]
    else:
        if not any(f.get('uuid') == station['uuid'] for f in favs):
            favs.insert(0, station)

    _save_json(_user_file('favorites.json'), favs)
    return jsonify({'ok': True, 'items': favs})


# ── Radio: stream URL resolver ───────────────────────────────

@radio_music_bp.route('/radio/stream-url', methods=['GET'])
def radio_stream_url():
    """Resolve a radio stream URL (follow redirects, return final URL)."""
    url = request.args.get('url', '')
    if not url:
        return jsonify({'error': 'Brak URL.'}), 400

    try:
        req = urllib.request.Request(url, method='HEAD', headers={
            'User-Agent': 'EthOS-RadioMusic/1.0',
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            final_url = resp.url
            content_type = resp.headers.get('Content-Type', '')
    except Exception:
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'EthOS-RadioMusic/1.0',
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                final_url = resp.url
                content_type = resp.headers.get('Content-Type', '')
        except Exception:
            final_url = url
            content_type = ''

    # Handle playlist files (M3U, PLS)
    if content_type and ('mpegurl' in content_type or 'x-scpls' in content_type):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                text = resp.read(8192).decode('utf-8', errors='replace')
            for line in text.splitlines():
                line = line.strip()
                if line.startswith('http'):
                    final_url = line
                    break
        except Exception:
            pass

    return jsonify({'url': final_url, 'content_type': content_type})


# ── Podcasts: search via iTunes ──────────────────────────────

# iTunes podcast genre IDs
_PODCAST_GENRES = {
    'all': 26,
    'arts': 1301, 'business': 1321, 'comedy': 1303, 'education': 1304,
    'fiction': 1483, 'health': 1512, 'history': 1487, 'kids': 1305,
    'leisure': 1502, 'music': 1310, 'news': 1489, 'religion': 1314,
    'science': 1533, 'society': 1324, 'sports': 1545, 'technology': 1318,
    'truecrime': 1488, 'tv': 1309,
}


@radio_music_bp.route('/podcasts/top', methods=['GET'])
def podcasts_top():
    """Top podcasts by genre and country via iTunes RSS."""
    country = request.args.get('country', 'pl').strip().lower()
    genre_key = request.args.get('genre', '').strip().lower()
    limit = min(int(request.args.get('limit', 30)), 100)
    genre_id = _PODCAST_GENRES.get(genre_key, 0)

    rss_url = f'https://itunes.apple.com/{country}/rss/toppodcasts/limit={limit}'
    if genre_id:
        rss_url += f'/genre={genre_id}'
    rss_url += '/json'

    try:
        req = urllib.request.Request(rss_url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log.debug('iTunes RSS top podcasts error: %s', e)
        return jsonify({'items': []})

    items = []
    for r in data.get('feed', {}).get('entry', []):
        apple_id = r.get('id', {}).get('attributes', {}).get('im:id', '')
        imgs = r.get('im:image', [])
        artwork = imgs[-1].get('label', '') if imgs else ''
        items.append({
            'id': apple_id,
            'name': r.get('im:name', {}).get('label', ''),
            'artist': r.get('im:artist', {}).get('label', ''),
            'artwork': artwork.replace('170x170', '600x600') if artwork else '',
            'genre': r.get('category', {}).get('attributes', {}).get('label', ''),
        })
    return jsonify({'items': items})


@radio_music_bp.route('/podcasts/lookup', methods=['GET'])
def podcasts_lookup():
    """Lookup a podcast by Apple ID to get its RSS feed URL (needed after top charts)."""
    apple_id = request.args.get('id', '').strip()
    if not apple_id:
        return jsonify({'error': 'Brak ID'}), 400
    lookup_url = f'https://itunes.apple.com/lookup?id={apple_id}&entity=podcast'
    try:
        req = urllib.request.Request(lookup_url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log.debug('iTunes lookup error: %s', e)
        return jsonify({'error': str(e)}), 502

    results = data.get('results', [])
    if not results:
        return jsonify({'error': 'Nie znaleziono'}), 404
    r = results[0]
    return jsonify({
        'id': r.get('collectionId', 0),
        'name': r.get('collectionName', ''),
        'artist': r.get('artistName', ''),
        'artwork': r.get('artworkUrl600') or r.get('artworkUrl100', ''),
        'feed_url': r.get('feedUrl', ''),
        'genre': r.get('primaryGenreName', ''),
        'count': r.get('trackCount', 0),
    })


@radio_music_bp.route('/podcasts/search', methods=['GET'])
def podcasts_search():
    q_str = request.args.get('q', '').strip()
    if not q_str:
        return jsonify({'items': []})

    params = {
        'term': q_str,
        'media': 'podcast',
        'limit': 30,
    }
    url = _ITUNES_API + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log.debug('iTunes search error: %s', e)
        return jsonify({'items': []})

    items = []
    for r in data.get('results', []):
        items.append({
            'id': r.get('collectionId', 0),
            'name': r.get('collectionName', ''),
            'artist': r.get('artistName', ''),
            'artwork': r.get('artworkUrl600') or r.get('artworkUrl100', ''),
            'feed_url': r.get('feedUrl', ''),
            'genre': r.get('primaryGenreName', ''),
            'count': r.get('trackCount', 0),
        })
    return jsonify({'items': items})


# ── Podcasts: parse RSS feed ─────────────────────────────────

@radio_music_bp.route('/podcasts/feed', methods=['GET'])
def podcasts_feed():
    feed_url = request.args.get('url', '').strip()
    if not feed_url:
        return jsonify({'error': 'Brak URL feedu.'}), 400

    req = urllib.request.Request(feed_url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_text = resp.read(10 * 1024 * 1024).decode('utf-8', errors='replace')
    except Exception as e:
        return jsonify({'error': 'Nie udało się pobrać feedu: ' + str(e)}), 502

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return jsonify({'error': 'Błąd parsowania RSS: ' + str(e)}), 502

    ns = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}
    channel = root.find('channel')
    if channel is None:
        return jsonify({'error': 'Nieprawidłowy feed RSS.'}), 400

    podcast = {
        'title': (channel.findtext('title') or '').strip(),
        'description': (channel.findtext('description') or '').strip(),
        'author': (channel.findtext('itunes:author', namespaces=ns) or '').strip(),
        'image': '',
    }
    img_el = channel.find('itunes:image', ns)
    if img_el is not None:
        podcast['image'] = img_el.get('href', '')
    elif channel.find('image') is not None:
        podcast['image'] = channel.findtext('image/url', '') or ''

    episodes = []
    for item in channel.findall('item'):
        enc = item.find('enclosure')
        audio_url = ''
        audio_type = ''
        if enc is not None:
            audio_url = enc.get('url', '')
            audio_type = enc.get('type', '')

        dur_text = item.findtext('itunes:duration', namespaces=ns) or ''
        duration = _parse_duration(dur_text)

        ep_img = ''
        ep_img_el = item.find('itunes:image', ns)
        if ep_img_el is not None:
            ep_img = ep_img_el.get('href', '')

        episodes.append({
            'title': (item.findtext('title') or '').strip(),
            'description': (item.findtext('description') or item.findtext('itunes:summary', namespaces=ns) or '').strip(),
            'pub_date': (item.findtext('pubDate') or '').strip(),
            'audio_url': audio_url,
            'audio_type': audio_type,
            'duration': duration,
            'duration_fmt': dur_text,
            'image': ep_img,
            'guid': (item.findtext('guid') or audio_url).strip(),
        })

    return jsonify({'podcast': podcast, 'episodes': episodes})


def _parse_duration(s):
    """Parse iTunes duration string (HH:MM:SS or seconds) to total seconds."""
    if not s:
        return 0
    s = s.strip()
    if ':' in s:
        parts = s.split(':')
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return 0
    try:
        return int(s)
    except ValueError:
        return 0


# ── Podcasts: subscriptions ──────────────────────────────────

@radio_music_bp.route('/podcasts/subscriptions', methods=['GET'])
def podcasts_subs():
    return jsonify({'items': _load_json(_user_file('subscriptions.json'), [])})


@radio_music_bp.route('/podcasts/subscribe', methods=['POST'])
def podcasts_subscribe():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    podcast = body.get('podcast')
    if not podcast or not podcast.get('feed_url'):
        return jsonify({'error': 'Brak danych podcastu.'}), 400

    subs = _load_json(_user_file('subscriptions.json'), [])

    if action == 'remove':
        subs = [s for s in subs if s.get('feed_url') != podcast['feed_url']]
    else:
        if not any(s.get('feed_url') == podcast['feed_url'] for s in subs):
            podcast['subscribed_at'] = time.time()
            subs.insert(0, podcast)

    _save_json(_user_file('subscriptions.json'), subs)
    return jsonify({'ok': True, 'items': subs})


# ── Play history ─────────────────────────────────────────────

def _fix_history_types(items):
    """Repair items whose type was incorrectly set to 'radio' by a past bug."""
    changed = False
    for it in items:
        url = it.get('url', '')
        if it.get('type') == 'radio':
            if '/local/stream' in url:
                it['type'] = 'local'
                changed = True
            elif 'youtube.com/' in url or 'youtu.be/' in url:
                it['type'] = 'music'
                changed = True
    return changed


@radio_music_bp.route('/history', methods=['GET'])
def history():
    hfile = _user_file('history.json')
    items = _load_json(hfile, [])
    if _fix_history_types(items):
        _save_json(hfile, items)
    return jsonify({'items': items})


@radio_music_bp.route('/history', methods=['POST'])
def history_add():
    body = request.get_json(force=True, silent=True) or {}
    item = body.get('item')
    if not item:
        return jsonify({'error': 'Brak danych.'}), 400

    # Normalize field aliases so history entries are always consistent
    if not item.get('name') and item.get('title'):
        item['name'] = item['title']
    if not item.get('image') and item.get('thumbnail'):
        item['image'] = item['thumbnail']
    if not item.get('meta') and item.get('channel'):
        item['meta'] = item['channel']

    item['played_at'] = time.time()
    hfile = _user_file('history.json')
    hist = _load_json(hfile, [])
    key = (item.get('name', ''), item.get('url', ''))
    existing = next((h for h in hist if (h.get('name', ''), h.get('url', '')) == key), None)
    if existing:
        item['play_count'] = existing.get('play_count', 1) + 1
        hist = [h for h in hist if (h.get('name', ''), h.get('url', '')) != key]
    else:
        item['play_count'] = 1
    hist.insert(0, item)
    hist = hist[:_MAX_HISTORY]

    _save_json(hfile, hist)
    return jsonify({'ok': True})


@radio_music_bp.route('/most-played', methods=['GET'])
def most_played():
    """Return history items sorted by play_count descending."""
    limit = min(int(request.args.get('limit', 30)), 100)
    hfile = _user_file('history.json')
    hist = _load_json(hfile, [])
    if _fix_history_types(hist):
        _save_json(hfile, hist)
    ranked = sorted(hist, key=lambda h: h.get('play_count', 1), reverse=True)
    return jsonify({'items': ranked[:limit]})


# ── Playback state (cross-device resume) ────────────────────

@radio_music_bp.route('/playback-state', methods=['GET'])
def get_playback_state():
    """Return saved playback state for cross-device resume."""
    pfile = _user_file('playback_state.json')
    state = _load_json(pfile, {})
    return jsonify(state)


@radio_music_bp.route('/playback-state', methods=['POST'])
def save_playback_state():
    """Save current playback state for cross-device resume."""
    data = request.get_json(silent=True) or {}
    if not data.get('playing'):
        return jsonify({'ok': True})
    # Cap queue to 200 items
    if 'queue' in data and len(data['queue']) > 200:
        data['queue'] = data['queue'][:200]
    pfile = _user_file('playback_state.json')
    _save_json(pfile, data)
    return jsonify({'ok': True})
def lyrics_search():
    """Fetch song lyrics from lrclib.net (free, no API key needed)."""
    title = request.args.get('title', '').strip()
    artist = request.args.get('artist', '').strip()
    if not title:
        return jsonify({'error': 'Brak tytułu.'}), 400

    def _clean_lyrics(text):
        lines = text.replace('\r\n', '\n').split('\n')
        cleaned = []
        for line in lines:
            line = re.sub(r'\[\d{2}:\d{2}\.\d{2,3}\]', '', line)
            if re.match(r'^\[(?:ti|ar|al|by|offset):.*\]$', line):
                continue
            cleaned.append(line.strip())
        return '\n'.join(cleaned).strip()

    def _search_lrclib(track, art):
        params = urllib.parse.urlencode({
            'track_name': track,
            'artist_name': art,
        })
        url = 'https://lrclib.net/api/search?' + params
        req = urllib.request.Request(url, headers={
            'User-Agent': 'EthOS-RadioMusic/1.0',
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            results = json.loads(resp.read().decode('utf-8'))
        if results and isinstance(results, list):
            best = results[0]
            plain = best.get('plainLyrics', '') or ''
            synced = best.get('syncedLyrics', '') or ''
            display = _clean_lyrics(plain) if plain else _clean_lyrics(synced)
            if display:
                return {
                    'ok': True, 'lyrics': display,
                    'title': best.get('trackName', track),
                    'artist': best.get('artistName', art),
                }
        return None

    try:
        # Primary search
        result = _search_lrclib(title, artist)
        if result:
            return jsonify(result)

        # Fallback: try splitting "Artist - Title" from the title field
        if ' - ' in title:
            parts = title.split(' - ', 1)
            fb_artist = parts[0].strip()
            fb_title = parts[1].strip()
            # Strip common YT suffixes
            fb_title = re.sub(
                r'\s*[\(\[](official\s*(video|audio|music\s*video|lyric\s*video|'
                r'visualizer)|lyrics?|teledysk|audio|video|clip|hd|hq|4k|'
                r'remastered|live)[\)\]]',
                '', fb_title, flags=re.IGNORECASE).strip()
            result = _search_lrclib(fb_title, fb_artist)
            if result:
                return jsonify(result)

        return jsonify({'ok': True, 'lyrics': '', 'not_found': True})
    except Exception as exc:
        log.warning('Lyrics fetch error: %s', exc)
        return jsonify({'ok': True, 'lyrics': '', 'not_found': True})


# ── Music folders config (per-user) ─────────────────────────

def _music_folders_file():
    return _user_file('music_folders.json')


def _default_music_dir():
    """User's home Music folder, always included."""
    username = getattr(g, 'username', None) or 'default'
    return os.path.join('/home', username, 'Music')


def _default_audiobooks_dir():
    """User's home Audiobooks folder."""
    username = getattr(g, 'username', None) or 'default'
    return os.path.join('/home', username, 'Audiobooks')


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


@radio_music_bp.route('/local/folders', methods=['GET'])
def local_folders_list():
    folders = _get_music_folders()
    result = []
    for f in folders:
        result.append({
            'path': f,
            'exists': os.path.isdir(f),
            'removable': f != _default_music_dir(),
        })
    return jsonify({'items': result})


@radio_music_bp.route('/local/folders', methods=['POST'])
def local_folders_update():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    folder = body.get('path', '').strip()
    if not folder:
        return jsonify({'error': 'Brak ścieżki.'}), 400
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return jsonify({'error': 'Folder nie istnieje.'}), 400

    folders = _load_json(_music_folders_file(), [])
    home_music = _default_music_dir()
    if action == 'add':
        if folder not in folders and folder != home_music:
            folders.append(folder)
    elif action == 'remove':
        if folder == home_music:
            return jsonify({'error': 'Nie można usunąć domyślnego folderu muzyki.'}), 400
        folders = [f for f in folders if f != folder]
    _save_json(_music_folders_file(), folders)
    return jsonify({'ok': True})


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


@radio_music_bp.route('/local/scan', methods=['GET'])
def local_scan():
    """Scan configured music/audiobook folders for audio files with metadata.
    ?scope=audiobooks → scan Audiobooks folder; default → Music folders."""
    scope = request.args.get('scope', 'music').strip()
    if scope == 'audiobooks':
        folders = _get_audiobook_folders()
    else:
        folders = _get_music_folders()
    items = []
    for base in folders:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _AUDIO_EXTS:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    stat = os.stat(fpath)
                except OSError:
                    continue
                rel = os.path.relpath(fpath, base)
                meta = _probe_audio(fpath)
                display_name = meta.get('title') or os.path.splitext(fname)[0]
                items.append({
                    'name': display_name,
                    'artist': meta.get('artist', ''),
                    'album': meta.get('album', ''),
                    'genre': meta.get('genre', ''),
                    'year': meta.get('year', ''),
                    'track': meta.get('track', ''),
                    'duration': meta.get('duration', 0),
                    'has_art': meta.get('has_art', False),
                    'filename': fname,
                    'path': fpath,
                    'folder': base,
                    'relative': rel,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'type': 'local',
                })
    items.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify({'items': items, 'folders': folders})


@radio_music_bp.route('/local/file', methods=['DELETE'])
def local_delete_file():
    """Delete a single local audio file."""
    body = request.get_json(force=True, silent=True) or {}
    fpath = (body.get('path') or '').strip()
    if not fpath:
        return jsonify({'error': 'Brak ścieżki'}), 400
    # Validate path is within a configured music folder
    music_folders = _get_music_folders() + _get_audiobook_folders()
    try:
        resolved = safe_path(fpath, '/')
    except ValueError:
        return jsonify({'error': 'Nieprawidłowa ścieżka'}), 400
    if not any(resolved.startswith(os.path.realpath(f) + os.sep) or resolved == os.path.realpath(f)
               for f in music_folders):
        return jsonify({'error': 'Plik poza folderem muzyki'}), 403
    if not os.path.isfile(resolved):
        return jsonify({'error': 'Plik nie istnieje'}), 404
    try:
        os.remove(resolved)
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@radio_music_bp.route('/local/folder', methods=['DELETE'])
def local_delete_folder():
    """Delete a local folder and all its audio contents."""
    body = request.get_json(force=True, silent=True) or {}
    fpath = (body.get('path') or '').strip()
    if not fpath:
        return jsonify({'error': 'Brak ścieżki'}), 400
    music_folders = _get_music_folders() + _get_audiobook_folders()
    try:
        resolved = safe_path(fpath, '/')
    except ValueError:
        return jsonify({'error': 'Nieprawidłowa ścieżka'}), 400
    real_music_folders = [os.path.realpath(f) for f in music_folders]
    # Must be within (but not equal to) a configured music folder
    if not any(resolved.startswith(rf + os.sep) for rf in real_music_folders):
        return jsonify({'error': 'Folder poza folderem muzyki lub jest głównym folderem'}), 403
    if not os.path.isdir(resolved):
        return jsonify({'error': 'Folder nie istnieje'}), 404
    try:
        shutil.rmtree(resolved)
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})



def local_stream():
    """Stream a local audio file."""
    fpath = request.args.get('path', '').lstrip()
    if not fpath:
        return jsonify({'error': 'Brak ścieżki'}), 400

    # Validate path is within one of the configured music or audiobook folders
    fpath = os.path.abspath(fpath)
    folders = _get_all_local_folders()
    allowed = False
    for base in folders:
        try:
            if fpath.startswith(os.path.realpath(base) + os.sep):
                allowed = True
                break
        except Exception:
            continue
    if not allowed:
        return jsonify({'error': 'Ścieżka poza dozwolonymi folderami'}), 403
    if not os.path.isfile(fpath):
        return jsonify({'error': 'Plik nie istnieje'}), 404

    ext = os.path.splitext(fpath)[1].lower()
    mime = mimetypes.guess_type(fpath)[0] or 'audio/mpeg'
    @after_this_request
    def _add_cors(resp):
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Range, Authorization'
        return resp
    return send_file(fpath, mimetype=mime, conditional=True)


@radio_music_bp.route('/local/artwork', methods=['GET'])
def local_artwork():
    """Extract embedded cover art from an audio file via ffmpeg."""
    fpath = request.args.get('path', '').lstrip()
    if not fpath:
        return jsonify({'error': 'Brak ścieżki'}), 400

    fpath = os.path.abspath(fpath)
    folders = _get_all_local_folders()
    allowed = False
    for base in folders:
        try:
            if fpath.startswith(os.path.realpath(base) + os.sep):
                allowed = True
                break
        except Exception:
            continue
    if not allowed:
        return jsonify({'error': 'Ścieżka poza dozwolonymi folderami'}), 403
    if not os.path.isfile(fpath):
        return jsonify({'error': 'Plik nie istnieje'}), 404

    try:
        r = subprocess.run(
            ['ffmpeg', '-i', fpath, '-an', '-vcodec', 'copy', '-f', 'image2pipe', '-'],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0 or not r.stdout:
            return Response(b'', status=204)
    except Exception:
        return Response(b'', status=204)

    # Detect MIME from magic bytes
    hdr = r.stdout[:4]
    if hdr[:2] == b'\xff\xd8':
        mime = 'image/jpeg'
    elif hdr[:4] == b'\x89PNG':
        mime = 'image/png'
    elif hdr[:4] == b'RIFF':
        mime = 'image/webp'
    else:
        mime = 'image/jpeg'

    return Response(r.stdout, mimetype=mime, headers={
        'Cache-Control': 'public, max-age=86400',
        'Access-Control-Allow-Origin': '*',
    })



# ── Chromecast helpers ──────────────────────────────────────

@radio_music_bp.route('/cast-info', methods=['GET'])
def cast_info():
    """Return NAS LAN IP(s) and origin for Chromecast URL building.

    Chromecast cannot use 127.0.0.1 or hostnames it doesn't know.
    This endpoint exposes the real LAN IP so the frontend can build
    absolute URLs that Chromecast can reach.
    """
    import socket
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # Include all non-loopback IPv4 addresses as fallback
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if '.' in addr and not addr.startswith('127.'):
                if addr not in ips:
                    ips.append(addr)
    except Exception:
        pass

    origin = request.host_url.rstrip('/')
    # Build a guaranteed-LAN origin using the primary LAN IP + port
    lan_origin = None
    if ips:
        try:
            port = int(request.host.split(':')[1]) if ':' in request.host else 9000
            lan_origin = f'http://{ips[0]}:{port}'
        except Exception:
            lan_origin = f'http://{ips[0]}:9000'

    return jsonify({'ips': ips, 'origin': origin, 'lan_origin': lan_origin or origin})


# ── Download (yt-dlp) ───────────────────────────────────────

_INTERMEDIATE_EXTS = {'.webm', '.webp', '.m4a', '.ogg', '.opus', '.part', '.ytdl'}

def _cleanup_intermediates(directory):
    """Remove leftover intermediate files that yt-dlp leaves after audio extraction."""
    import glob as _glob
    for ext in _INTERMEDIATE_EXTS:
        for fpath in _glob.glob(os.path.join(directory, '**', f'*{ext}'), recursive=True):
            mp3_sibling = fpath.rsplit('.', 1)[0] + '.mp3'
            if os.path.isfile(mp3_sibling):
                try:
                    os.remove(fpath)
                except OSError:
                    pass


def _music_download_dir():
    """Target directory for downloaded music. Creates if missing."""
    d = _default_music_dir()
    os.makedirs(d, exist_ok=True)
    return d


@radio_music_bp.route('/music/download', methods=['POST'])
def music_download():
    """Download a track to the user's music folder using yt-dlp."""
    body = request.get_json(force=True, silent=True) or {}
    url = body.get('url', '').strip()
    title = body.get('title', 'Unknown')
    folder = body.get('folder', '').strip()
    if not url:
        return jsonify({'error': 'Brak URL'}), 400

    ytdlp = _find_ytdlp()
    if not ytdlp:
        return jsonify({'error': 'yt-dlp nie jest zainstalowane'}), 503

    if folder:
        username = getattr(g, 'username', None) or 'default'
        dest = os.path.join('/home', username, folder)
    else:
        dest = _music_download_dir()
    os.makedirs(dest, exist_ok=True)
    job_id = str(int(time.time() * 1000))
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_JOBS[job_id] = {
            'status': 'downloading', 'progress': 0,
            'title': title, 'error': None, 'path': None,
        }

    from host import host_run

    def _do_download():
        try:
            # Organize: Artist/Album/Title.mp3
            # YouTube always has uploader; album may be missing
            out_tmpl = os.path.join(
                dest,
                '%(uploader|Unknown Artist)s',
                '%(album|Singles)s',
                '%(title)s.%(ext)s'
            )
            cmd = (
                f'{shq(ytdlp)} -f bestaudio -x --audio-format mp3 --audio-quality 0 '
                f'--embed-thumbnail --embed-metadata --no-playlist --no-warnings '
                f'--parse-metadata "%(uploader)s:%(meta_artist)s" '
                f'--parse-metadata "%(upload_date>%Y)s:%(meta_date)s" '
                f'--postprocessor-args "ffmpeg:-b:a 320k" '
                f'-o {shq(out_tmpl)} '
                f'{shq(url)}'
            )
            r = host_run(cmd, timeout=300)
            _cleanup_intermediates(dest)
            with _DOWNLOAD_LOCK:
                if r.returncode == 0:
                    out_file = None
                    if r.stdout:
                        for line in r.stdout.splitlines():
                            if 'Destination:' in line:
                                out_file = line.split('Destination:', 1)[1].strip()
                            elif '[ExtractAudio]' in line and 'Destination:' in line:
                                out_file = line.split('Destination:', 1)[1].strip()
                    _DOWNLOAD_JOBS[job_id]['status'] = 'done'
                    _DOWNLOAD_JOBS[job_id]['progress'] = 100
                    _DOWNLOAD_JOBS[job_id]['path'] = out_file or dest
                else:
                    _DOWNLOAD_JOBS[job_id]['status'] = 'error'
                    _DOWNLOAD_JOBS[job_id]['error'] = (r.stderr or 'Nieznany błąd')[:200]
        except Exception as e:
            with _DOWNLOAD_LOCK:
                _DOWNLOAD_JOBS[job_id]['status'] = 'error'
                _DOWNLOAD_JOBS[job_id]['error'] = str(e)[:200]

    gevent.spawn(_do_download)
    return jsonify({'ok': True, 'job_id': job_id})


@radio_music_bp.route('/music/download-playlist', methods=['POST'])
def music_download_playlist():
    """Download all tracks in a playlist."""
    body = request.get_json(force=True, silent=True) or {}
    tracks = body.get('tracks', [])
    playlist_name = body.get('name', 'Playlist')
    if not tracks:
        return jsonify({'error': 'Brak utworów'}), 400

    ytdlp = _find_ytdlp()
    if not ytdlp:
        return jsonify({'error': 'yt-dlp nie jest zainstalowane'}), 503

    dest = os.path.join(_music_download_dir(), playlist_name.replace('/', '_'))
    os.makedirs(dest, exist_ok=True)

    job_id = str(int(time.time() * 1000))
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_JOBS[job_id] = {
            'status': 'downloading', 'progress': 0,
            'title': playlist_name, 'error': None, 'path': dest,
            'total': len(tracks), 'done_count': 0,
        }

    from host import host_run

    def _do_batch():
        done = 0
        errors = []
        for track in tracks:
            turl = track.get('url', '').strip()
            if not turl:
                continue
            # Playlist downloads: PlaylistName/Artist - Title.mp3
            out_tmpl = os.path.join(
                dest, '%(uploader|Unknown)s - %(title)s.%(ext)s'
            )
            cmd = (
                f'{shq(ytdlp)} -f bestaudio -x --audio-format mp3 --audio-quality 0 '
                f'--embed-thumbnail --embed-metadata --no-playlist --no-warnings '
                f'--parse-metadata "%(uploader)s:%(meta_artist)s" '
                f'--parse-metadata "%(upload_date>%Y)s:%(meta_date)s" '
                f'--postprocessor-args "ffmpeg:-b:a 320k" '
                f'-o {shq(out_tmpl)} '
                f'{shq(turl)}'
            )
            r = host_run(cmd, timeout=300)
            done += 1
            with _DOWNLOAD_LOCK:
                _DOWNLOAD_JOBS[job_id]['done_count'] = done
                _DOWNLOAD_JOBS[job_id]['progress'] = int(done / len(tracks) * 100)
            if r.returncode != 0:
                errors.append(track.get('title', turl)[:40])

        with _DOWNLOAD_LOCK:
            _DOWNLOAD_JOBS[job_id]['status'] = 'done' if not errors else 'done_partial'
            _DOWNLOAD_JOBS[job_id]['progress'] = 100
            if errors:
                _DOWNLOAD_JOBS[job_id]['error'] = f'Błędy: {", ".join(errors[:5])}'
        _cleanup_intermediates(dest)

    gevent.spawn(_do_batch)
    return jsonify({'ok': True, 'job_id': job_id})


@radio_music_bp.route('/music/downloads', methods=['GET'])
def music_downloads_status():
    """Return status of active/recent download jobs."""
    with _DOWNLOAD_LOCK:
        # Clean up old completed jobs (>5 min)
        now = time.time()
        to_remove = [jid for jid, j in _DOWNLOAD_JOBS.items()
                     if j['status'] in ('done', 'done_partial', 'error')]
        jobs = dict(_DOWNLOAD_JOBS)
    return jsonify({'jobs': jobs})


# ── Playlists (per-user) ─────────────────────────────────────

def _playlists_file():
    return _user_file('playlists.json')


@radio_music_bp.route('/playlists', methods=['GET'])
def playlists_list():
    return jsonify({'items': _load_json(_playlists_file(), [])})


@radio_music_bp.route('/playlists', methods=['POST'])
def playlists_create():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Brak nazwy playlisty.'}), 400

    pls = _load_json(_playlists_file(), [])
    pl_id = str(int(time.time() * 1000))
    pl = {
        'id': pl_id,
        'name': name,
        'tracks': [],
        'created_at': time.time(),
        'updated_at': time.time(),
    }
    pls.insert(0, pl)
    _save_json(_playlists_file(), pls)
    return jsonify({'ok': True, 'playlist': pl, 'items': pls})


@radio_music_bp.route('/playlists/<pl_id>', methods=['GET'])
def playlists_get(pl_id):
    pls = _load_json(_playlists_file(), [])
    pl = next((p for p in pls if p['id'] == pl_id), None)
    if not pl:
        return jsonify({'error': 'Playlista nie znaleziona'}), 404
    return jsonify({'playlist': pl})


@radio_music_bp.route('/playlists/<pl_id>', methods=['PUT'])
def playlists_update(pl_id):
    body = request.get_json(force=True, silent=True) or {}
    pfile = _playlists_file()
    pls = _load_json(pfile, [])
    pl = next((p for p in pls if p['id'] == pl_id), None)
    if not pl:
        return jsonify({'error': 'Playlista nie znaleziona'}), 404

    if 'name' in body:
        pl['name'] = (body['name'] or '').strip() or pl['name']
    if 'tracks' in body:
        pl['tracks'] = body['tracks']
    pl['updated_at'] = time.time()
    _save_json(pfile, pls)
    return jsonify({'ok': True, 'playlist': pl})


@radio_music_bp.route('/playlists/<pl_id>', methods=['DELETE'])
def playlists_delete(pl_id):
    pfile = _playlists_file()
    pls = _load_json(pfile, [])
    pls = [p for p in pls if p['id'] != pl_id]
    _save_json(pfile, pls)
    return jsonify({'ok': True, 'items': pls})


@radio_music_bp.route('/playlists/<pl_id>/tracks', methods=['POST'])
def playlists_add_track(pl_id):
    """Add a track/station/podcast to a playlist."""
    body = request.get_json(force=True, silent=True) or {}
    track = body.get('track')
    if not track:
        return jsonify({'error': 'Brak danych utworu.'}), 400

    pfile = _playlists_file()
    pls = _load_json(pfile, [])
    pl = next((p for p in pls if p['id'] == pl_id), None)
    if not pl:
        return jsonify({'error': 'Playlista nie znaleziona'}), 404

    track['added_at'] = time.time()
    pl['tracks'].append(track)
    pl['updated_at'] = time.time()
    _save_json(pfile, pls)
    return jsonify({'ok': True, 'playlist': pl})


@radio_music_bp.route('/playlists/<pl_id>/tracks/<int:track_idx>', methods=['DELETE'])
def playlists_remove_track(pl_id, track_idx):
    pfile = _playlists_file()
    pls = _load_json(pfile, [])
    pl = next((p for p in pls if p['id'] == pl_id), None)
    if not pl:
        return jsonify({'error': 'Playlista nie znaleziona'}), 404
    if 0 <= track_idx < len(pl['tracks']):
        pl['tracks'].pop(track_idx)
        pl['updated_at'] = time.time()
        _save_json(pfile, pls)
    return jsonify({'ok': True, 'playlist': pl})


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


@radio_music_bp.route('/music/check-deps', methods=['GET'])
def music_check_deps():
    return jsonify({'ok': True, 'ready': bool(_find_ytdlp())})


@radio_music_bp.route('/music/install-deps', methods=['POST'])
def music_install_deps():
    from host import host_run
    ethos_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pip_bin = os.path.join(ethos_root, 'venv', 'bin', 'pip')

    r = host_run(f'{shq(pip_bin)} install --quiet yt-dlp', timeout=120)
    if r.returncode != 0:
        return jsonify({'error': r.stderr or 'Instalacja nie powiodła się'}), 500

    # Install deno if missing
    host_run('which deno >/dev/null 2>&1 || (curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh 2>/dev/null)', timeout=60)

    # Ensure yt-dlp config for EJS solver
    os.makedirs('/etc/yt-dlp', exist_ok=True)
    cfg_path = '/etc/yt-dlp/config'
    if not os.path.isfile(cfg_path):
        with open(cfg_path, 'w') as f:
            f.write('--remote-components ejs:github\n')

    global _YTDLP_BIN
    _YTDLP_BIN = None
    return jsonify({'ok': True, 'ready': bool(_find_ytdlp())})


@radio_music_bp.route('/music/search', methods=['GET'])
def music_search():
    """Search for music via yt-dlp (YouTube by default)."""
    q_str = request.args.get('q', '').strip()
    limit = min(int(request.args.get('limit', 20)), 50)
    if not q_str:
        return jsonify({'items': []})

    ytdlp = _find_ytdlp()
    if not ytdlp:
        return jsonify({'error': 'yt-dlp nie jest zainstalowane'}), 503

    from host import host_run
    search_arg = f'ytsearch{limit}:{q_str}'
    cmd = (f'{shq(ytdlp)} --dump-json --flat-playlist --no-warnings '
           f'--no-download {shq(search_arg)}')
    r = host_run(cmd, timeout=30)

    items = []
    if r.stdout:
        for line in r.stdout.strip().splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid_id = d.get('id', '')
            dur = d.get('duration') or 0
            items.append({
                'id': vid_id,
                'title': d.get('title', ''),
                'channel': d.get('channel', d.get('uploader', '')),
                'duration': dur,
                'duration_fmt': _fmt_secs(dur),
                'thumbnail': (d.get('thumbnails', [{}])[-1].get('url', '')
                              or f'https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg'),
                'url': (d.get('url', '') or d.get('webpage_url', '')
                        or f'https://www.youtube.com/watch?v={vid_id}'),
                'type': 'music',
                'source': 'youtube',
            })
    return jsonify({'items': items})


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


@radio_music_bp.route('/music/direct-url', methods=['GET'])
def music_direct_url():
    """Return the direct CDN audio URL (for Chromecast — bypasses proxy)."""
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Brak URL'}), 400
    audio_url, ct = _extract_audio_url(url)
    if not audio_url:
        return jsonify({'error': 'Extraction failed'}), 502
    return jsonify({'ok': True, 'audio_url': audio_url, 'content_type': ct or 'audio/mp4'})


@radio_music_bp.route('/music/stream', methods=['GET'])
def music_stream():
    """Stream audio from YouTube/other sources. Extracts URL via yt-dlp, caches, proxies."""
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Brak URL'}), 400

    audio_url, ct_hint = _extract_audio_url(url)
    if not audio_url:
        return jsonify({'error': 'Nie udało się wyodrębnić audio'}), 502

    # HLS live stream — redirect directly to m3u8 so browser can use hls.js or native HLS
    if ct_hint == 'application/x-mpegURL' or 'm3u8' in audio_url or 'manifest' in audio_url:
        return redirect(audio_url, code=302)

    range_header = request.headers.get('Range')
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    }
    if range_header:
        headers['Range'] = range_header

    def _open_audio(aurl):
        req = urllib.request.Request(aurl, headers=headers)
        return urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)

    try:
        resp = _open_audio(audio_url)
    except Exception:
        # URL may have expired — clear cache and re-extract
        _YTDLP_URL_CACHE.pop(url, None)
        audio_url, ct_hint = _extract_audio_url(url)
        if not audio_url:
            return jsonify({'error': 'Ekstrakcja nie powiodła się'}), 502
        try:
            resp = _open_audio(audio_url)
        except Exception as e:
            log.warning('Music stream error for %s: %s', url, e)
            return jsonify({'error': 'Strumień niedostępny'}), 502

    ct = resp.headers.get('Content-Type', ct_hint or 'audio/mp4')
    cl = resp.headers.get('Content-Length')
    cr = resp.headers.get('Content-Range')
    status = resp.status

    def generate():
        try:
            while True:
                chunk = resp.read(32768)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            pass
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    resp_headers = {
        'Cache-Control': 'no-cache',
        'Access-Control-Allow-Origin': '*',
    }
    if cl:
        resp_headers['Content-Length'] = cl
    if cr:
        resp_headers['Content-Range'] = cr
    resp_headers['Accept-Ranges'] = 'bytes' if cl else 'none'

    return Response(generate(), status=status, mimetype=ct, headers=resp_headers)


# ── Stream proxy (solves CORS, ICY, HLS issues) ─────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _open_icy_stream(url, timeout=10):
    """Open an ICY (SHOUTcast) audio stream via raw socket.
    Returns (stream_object, content_type) or raises on failure."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or 80
    # Reconstruct full path including params (e.g. /;.mp3) and query
    path = parsed.path or '/'
    if parsed.params:
        path += ';' + parsed.params
    if parsed.query:
        path += '?' + parsed.query

    sock = socket.create_connection((host, port), timeout=timeout)
    req_line = (
        f'GET {path} HTTP/1.0\r\n'
        f'Host: {host}\r\n'
        f'User-Agent: Mozilla/5.0\r\n'
        f'Icy-MetaData: 0\r\n'
        f'Connection: close\r\n'
        f'\r\n'
    )
    sock.sendall(req_line.encode('utf-8'))

    # Read the ICY status line + headers
    header_data = b''
    while b'\r\n\r\n' not in header_data:
        chunk = sock.recv(1024)
        if not chunk:
            break
        header_data += chunk
        if len(header_data) > 16384:
            break

    header_text, _, body_start = header_data.partition(b'\r\n\r\n')
    ct = 'audio/mpeg'
    for line in header_text.decode('utf-8', errors='replace').splitlines():
        if line.lower().startswith('content-type:'):
            ct = line.split(':', 1)[1].strip()
            break

    class IcyStream:
        """Minimal file-like wrapper over a raw socket with leftover data."""
        def __init__(self, sock, leftover):
            self._sock = sock
            self._leftover = leftover
        def read(self, size=16384):
            if self._leftover:
                data = self._leftover[:size]
                self._leftover = self._leftover[size:]
                return data
            try:
                return self._sock.recv(size)
            except Exception:
                return b''
        def close(self):
            try:
                self._sock.close()
            except Exception:
                pass

    return IcyStream(sock, body_start), ct


@radio_music_bp.route('/radio/proxy', methods=['GET'])
def radio_proxy():
    """Proxy audio streams/files through the server to avoid CORS/ICY issues.
    Supports both live radio (infinite streams) and podcasts (seekable files)."""
    url = request.args.get('url', '').strip()
    if not url or not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL'}), 400

    # For podcast files (finite), forward Range headers for seeking support
    range_header = request.headers.get('Range')
    extra_headers = {}
    if range_header:
        extra_headers['Range'] = range_header

    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                          'Chrome/146.0 Safari/537.36',
            'Icy-MetaData': '0',
            **extra_headers,
        })
        resp = urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)
        ct = resp.headers.get('Content-Type', 'audio/mpeg')
        cl = resp.headers.get('Content-Length')
        cr = resp.headers.get('Content-Range')
        ar = resp.headers.get('Accept-Ranges')
        status = resp.status
    except http.client.BadStatusLine:
        # ICY protocol — fall through to raw socket handler (radio only)
        try:
            resp, ct = _open_icy_stream(url)
        except Exception as e:
            log.warning('Stream proxy open error for %s: %s', url, e)
            return jsonify({'error': 'Nie udało się połączyć ze stacją'}), 502
        cl = None
        cr = None
        ar = None
        status = 200
    except Exception as e:
        log.warning('Stream proxy open error for %s: %s', url, e)
        return jsonify({'error': 'Nie udało się połączyć ze stacją'}), 502

    # Normalise common content-types
    if 'aacp' in ct or 'aac' in ct:
        ct = 'audio/aac'
    elif 'ogg' in ct:
        ct = 'audio/ogg'
    elif 'mp3' in ct or 'mpeg' in ct:
        ct = 'audio/mpeg'

    def generate():
        try:
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            pass
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    resp_headers = {
        'Cache-Control': 'no-cache, no-store',
        'Access-Control-Allow-Origin': '*',
    }
    # Seekable files (podcasts): forward Content-Length/Range info
    if cl:
        resp_headers['Content-Length'] = cl
    if cr:
        resp_headers['Content-Range'] = cr
    if cl or ar:
        resp_headers['Accept-Ranges'] = 'bytes'
    else:
        resp_headers['Accept-Ranges'] = 'none'

    return Response(
        generate(),
        status=status,
        mimetype=ct,
        headers=resp_headers,
    )

# ── Offline Archive (yt-dlp → permanent NAS copy) ──────────────────────────

@radio_music_bp.route('/archive/start', methods=['POST'])
def archive_start():
    """Start archiving a YouTube track to data/offline-archive/ using yt-dlp.
    Body: {url, title, artist, thumbnail}
    Returns: {ok, key, status}  key = md5(url)[:16]
    Emits: rm_archive_progress, rm_archive_done, rm_archive_error via SocketIO.
    """
    from host import host_run_stream
    body = request.get_json(force=True, silent=True) or {}
    url = body.get('url', '').strip()
    title = body.get('title', 'Unknown')
    artist = body.get('artist', '')
    thumbnail = body.get('thumbnail', '')
    if not url:
        return jsonify({'error': 'Brak URL'}), 400

    ytdlp = _find_ytdlp()
    if not ytdlp:
        return jsonify({'error': 'yt-dlp nie jest zainstalowane. Zainstaluj w sekcji Muzyka.'}), 503

    key = _archive_key(url)
    with _ARCHIVE_LOCK:
        db = _load_archive()
        existing = db.get(key, {})
        if existing.get('status') == 'done' and os.path.isfile(existing.get('nas_path', '')):
            return jsonify({'ok': True, 'key': key, 'status': 'done', 'already': True})
        if existing.get('status') == 'downloading':
            return jsonify({'ok': True, 'key': key, 'status': 'downloading', 'already': True})
        db[key] = {
            'key': key, 'yt_url': url, 'title': title, 'artist': artist,
            'thumbnail': thumbnail, 'status': 'downloading', 'progress': 0,
            'nas_path': None, 'size_bytes': 0, 'error': None,
            'created_at': time.time(),
        }
        _save_archive(db)

    # Capture username before spawning background task (g is not available in greenlets)
    req_username = getattr(g, 'username', None) or 'default'

    def _do_archive():
        sio = _sio()
        with _ARCHIVE_SEM:   # max 2 concurrent yt-dlp downloads
          try:
            dest_dir = _archive_dir()
            out_tmpl = os.path.join(dest_dir, key + '.%(ext)s')
            cmd = (
                f'{shq(ytdlp)} -f bestaudio -x --audio-format mp3 --audio-quality 0 '
                f'--embed-thumbnail --embed-metadata --no-playlist --no-warnings '
                f'--progress --newline '
                f'--parse-metadata "%(uploader)s:%(meta_artist)s" '
                f'-o {shq(out_tmpl)} '
                f'{shq(url)}'
            )
            stream = host_run_stream(cmd)
            rc = -1
            last_pct = -1
            for line in stream:
                if line.startswith('__EXIT_CODE__:'):
                    try:
                        rc = int(line.split(':')[1].strip())
                    except ValueError:
                        rc = -1
                    break
                m = re.search(r'\[download\]\s+(\d+\.?\d*)%', line)
                if m:
                    pct = min(99, int(float(m.group(1))))
                    if pct != last_pct:
                        last_pct = pct
                        with _ARCHIVE_LOCK:
                            db2 = _load_archive()
                            if key in db2:
                                db2[key]['progress'] = pct
                                _save_archive(db2)
                        if sio:
                            sio.emit('rm_archive_progress', {
                                'key': key, 'url': url, 'progress': pct, 'title': title
                            })

            nas_path = os.path.join(dest_dir, key + '.mp3')
            success = rc == 0 and os.path.isfile(nas_path)
            with _ARCHIVE_LOCK:
                db3 = _load_archive()
                if key in db3:
                    if success:
                        db3[key]['status'] = 'done'
                        db3[key]['progress'] = 100
                        db3[key]['nas_path'] = nas_path
                        db3[key]['size_bytes'] = os.path.getsize(nas_path)
                        # Also copy to ~/Music/RadioMusic/ for direct file access
                        try:
                            music_rm_dir = os.path.join('/home', req_username, 'Music', 'RadioMusic')
                            os.makedirs(music_rm_dir, exist_ok=True)
                            safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title or key)[:120]
                            music_dest = os.path.join(music_rm_dir, safe_title + '.mp3')
                            if not os.path.exists(music_dest):
                                shutil.copy2(nas_path, music_dest)
                            db3[key]['music_path'] = music_dest
                        except Exception:
                            pass
                    else:
                        db3[key]['status'] = 'error'
                        db3[key]['error'] = f'yt-dlp exited {rc}'
                    _save_archive(db3)
            if sio:
                if success:
                    sio.emit('rm_archive_done', {'key': key, 'url': url, 'title': title})
                else:
                    sio.emit('rm_archive_error', {
                        'key': key, 'url': url, 'title': title, 'error': f'yt-dlp exited {rc}'
                    })
          except Exception as exc:
            with _ARCHIVE_LOCK:
                db4 = _load_archive()
                if key in db4:
                    db4[key]['status'] = 'error'
                    db4[key]['error'] = str(exc)[:300]
                    _save_archive(db4)
            if sio:
                sio.emit('rm_archive_error', {'key': key, 'url': url, 'title': title, 'error': str(exc)[:200]})

    gevent.spawn(_do_archive)
    return jsonify({'ok': True, 'key': key, 'status': 'downloading'})


@radio_music_bp.route('/archive/batch', methods=['POST'])
def archive_batch():
    """Batch-query archive status for a list of YouTube URLs.
    Body: {urls: [...]}  (max 200)
    Returns: {results: {<url>: {key, status, progress, size_bytes}}}
    """
    body = request.get_json(force=True, silent=True) or {}
    urls = body.get('urls', [])
    if not isinstance(urls, list):
        return jsonify({'error': 'urls must be a list'}), 400
    urls = [u for u in urls if isinstance(u, str)][:200]
    with _ARCHIVE_LOCK:
        db = _load_archive()
    results = {}
    for url in urls:
        k = _archive_key(url)
        entry = db.get(k, {})
        results[url] = {
            'key': k,
            'status': entry.get('status', 'none'),
            'progress': entry.get('progress', 0),
            'size_bytes': entry.get('size_bytes', 0),
            'title': entry.get('title', ''),
        }
    return jsonify({'results': results})


@radio_music_bp.route('/archive/delete', methods=['POST'])
def archive_delete():
    """Delete an archived track. Body: {key}"""
    body = request.get_json(force=True, silent=True) or {}
    key = body.get('key', '').strip()
    if not key or not re.match(r'^[a-f0-9]{16}$', key):
        return jsonify({'error': 'Invalid key'}), 400
    with _ARCHIVE_LOCK:
        db = _load_archive()
        entry = db.pop(key, None)
        _save_archive(db)
    if entry and entry.get('nas_path') and os.path.isfile(entry['nas_path']):
        try:
            os.remove(entry['nas_path'])
        except OSError:
            pass
    return jsonify({'ok': True})


@radio_music_bp.route('/archive/quota', methods=['GET'])
def archive_quota():
    """Disk usage of offline archive."""
    d = _archive_dir()
    total_bytes = 0
    count = 0
    try:
        for fname in os.listdir(d):
            fp = os.path.join(d, fname)
            if os.path.isfile(fp):
                total_bytes += os.path.getsize(fp)
                count += 1
    except OSError:
        pass
    with _ARCHIVE_LOCK:
        db = _load_archive()
    return jsonify({'ok': True, 'total_bytes': total_bytes, 'count': count,
                    'tracked': len(db), 'dir': d})


@radio_music_bp.route('/archive/file/<key>', methods=['GET'])
def archive_file(key):
    """Stream an archived audio file. Range requests supported for seeking."""
    if not re.match(r'^[a-f0-9]{16}$', key):
        return jsonify({'error': 'Invalid key'}), 400
    with _ARCHIVE_LOCK:
        db = _load_archive()
    entry = db.get(key)
    if not entry or entry.get('status') != 'done':
        return jsonify({'error': 'Not found'}), 404
    nas_path = entry.get('nas_path')
    if not nas_path or not os.path.isfile(nas_path):
        return jsonify({'error': 'File missing on NAS — was it deleted?'}), 404
    resp = send_file(nas_path, mimetype='audio/mpeg', conditional=True)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@radio_music_bp.route('/archive/download/<key>', methods=['GET'])
def archive_download(key):
    """Force-download an archived file to the browser (Content-Disposition: attachment)."""
    if not re.match(r'^[a-f0-9]{16}$', key):
        return jsonify({'error': 'Invalid key'}), 400
    with _ARCHIVE_LOCK:
        db = _load_archive()
    entry = db.get(key)
    if not entry or entry.get('status') != 'done':
        return jsonify({'error': 'Not found or not yet downloaded'}), 404
    nas_path = entry.get('nas_path')
    if not nas_path or not os.path.isfile(nas_path):
        return jsonify({'error': 'File missing on NAS'}), 404
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', entry.get('title', key))[:120]
    download_name = safe_title + '.mp3'
    return send_file(nas_path, mimetype='audio/mpeg', as_attachment=True,
                     download_name=download_name)
