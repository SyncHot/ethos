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
  GET  /api/radio-music/history            - recently played items
  POST /api/radio-music/history            - add to history
"""

import http.client
import json
import logging
import os
import socket
import ssl
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

from flask import Blueprint, jsonify, request, Response

from host import data_path

log = logging.getLogger('ethos.radio_music')

radio_music_bp = Blueprint('radio-music', __name__, url_prefix='/api/radio-music')

_DATA_DIR = data_path('radio_music')
_FAV_FILE = os.path.join(_DATA_DIR, 'favorites.json')
_SUBS_FILE = os.path.join(_DATA_DIR, 'subscriptions.json')
_HISTORY_FILE = os.path.join(_DATA_DIR, 'history.json')

_RADIO_API = 'https://de1.api.radio-browser.info'
_ITUNES_API = 'https://itunes.apple.com/search'

_MAX_HISTORY = 100


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
    _ensure_dirs()
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
                    if picked['favicon'] and not existing['favicon']:
                        existing['favicon'] = picked['favicon']
                else:
                    existing['alt_urls'].append(url)
            # Merge votes (take max)
            if picked['votes'] > existing['votes']:
                existing['votes'] = picked['votes']
            if picked['favicon'] and not existing['favicon']:
                existing['favicon'] = picked['favicon']
    return list(groups.values())


# ── Package status (trivial — no system deps needed) ─────────

@radio_music_bp.route('/pkg-status', methods=['GET'])
def pkg_status():
    return jsonify({'ok': True, 'installed': True, 'ready': True})


@radio_music_bp.route('/install', methods=['POST'])
def install():
    _ensure_dirs()
    return jsonify({'ok': True})


@radio_music_bp.route('/uninstall', methods=['POST'])
def uninstall():
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
    return jsonify({'items': _load_json(_FAV_FILE, [])})


@radio_music_bp.route('/radio/favorites', methods=['POST'])
def radio_favorites_edit():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    station = body.get('station')
    if not station or not station.get('uuid'):
        return jsonify({'error': 'Brak danych stacji.'}), 400

    favs = _load_json(_FAV_FILE, [])

    if action == 'remove':
        favs = [f for f in favs if f.get('uuid') != station['uuid']]
    else:
        if not any(f.get('uuid') == station['uuid'] for f in favs):
            favs.insert(0, station)

    _save_json(_FAV_FILE, favs)
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_text = resp.read(2 * 1024 * 1024).decode('utf-8', errors='replace')
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
    return jsonify({'items': _load_json(_SUBS_FILE, [])})


@radio_music_bp.route('/podcasts/subscribe', methods=['POST'])
def podcasts_subscribe():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    podcast = body.get('podcast')
    if not podcast or not podcast.get('feed_url'):
        return jsonify({'error': 'Brak danych podcastu.'}), 400

    subs = _load_json(_SUBS_FILE, [])

    if action == 'remove':
        subs = [s for s in subs if s.get('feed_url') != podcast['feed_url']]
    else:
        if not any(s.get('feed_url') == podcast['feed_url'] for s in subs):
            podcast['subscribed_at'] = time.time()
            subs.insert(0, podcast)

    _save_json(_SUBS_FILE, subs)
    return jsonify({'ok': True, 'items': subs})


# ── Play history ─────────────────────────────────────────────

@radio_music_bp.route('/history', methods=['GET'])
def history():
    return jsonify({'items': _load_json(_HISTORY_FILE, [])})


@radio_music_bp.route('/history', methods=['POST'])
def history_add():
    body = request.get_json(force=True, silent=True) or {}
    item = body.get('item')
    if not item:
        return jsonify({'error': 'Brak danych.'}), 400

    item['played_at'] = time.time()
    hist = _load_json(_HISTORY_FILE, [])
    key = (item.get('name', ''), item.get('url', ''))
    hist = [h for h in hist if (h.get('name', ''), h.get('url', '')) != key]
    hist.insert(0, item)
    hist = hist[:_MAX_HISTORY]

    _save_json(_HISTORY_FILE, hist)
    return jsonify({'ok': True})


# ── Stream proxy (solves CORS, ICY, HLS issues) ─────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _open_stream(url, timeout=10):
    """Open an audio stream, handling both HTTP and ICY (SHOUTcast) protocols.
    Returns (response_object, content_type) or raises on failure."""
    # First try standard urllib (works for HTTP/HTTPS streams)
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                          'Chrome/146.0 Safari/537.36',
            'Icy-MetaData': '0',
        })
        resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        ct = resp.headers.get('Content-Type', 'audio/mpeg')
        return resp, ct
    except http.client.BadStatusLine:
        pass  # ICY protocol — fall through to raw socket
    except Exception:
        raise

    # ICY protocol: server responds "ICY 200 OK" which urllib can't parse.
    # Use a raw socket connection.
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
    """Proxy a radio stream through the server to avoid CORS/ICY issues."""
    url = request.args.get('url', '').strip()
    if not url or not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL'}), 400

    try:
        resp, ct = _open_stream(url)
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

    return Response(
        generate(),
        mimetype=ct,
        headers={
            'Cache-Control': 'no-cache, no-store',
            'Accept-Ranges': 'none',
            'Access-Control-Allow-Origin': '*',
        },
    )