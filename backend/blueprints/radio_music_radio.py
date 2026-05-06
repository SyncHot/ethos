"""Radio routes (search, favorites, proxy) for the Radio & Music blueprint."""

import http.client
import urllib.parse
import urllib.request
import socket

from flask import jsonify, request, Response

from blueprints.radio_music import (
    radio_music_bp, log, _safe_int, _radio_api, _aggregate_stations,
    _user_file, _load_json, _save_json, _SSL_CTX,
)
from blueprints.radio_music_playlist import _migrate_old_subscriptions


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


# ── Radio: search & browse ───────────────────────────────────

@radio_music_bp.route('/radio/search', methods=['GET'])
def radio_search():
    q_str = request.args.get('q', '').strip()
    country = request.args.get('country', '').strip()
    tag = request.args.get('tag', '').strip()
    limit = _safe_int(request.args.get('limit', 50), 50, hi=200)
    offset = _safe_int(request.args.get('offset', 0), 0, lo=0, hi=10000)

    params = {
        'limit': limit * 3,  # fetch extra to aggregate duplicates
        'offset': offset * 3,
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
    return jsonify({'items': items[:limit], 'hasMore': len(items) >= limit})


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
    limit = _safe_int(request.args.get('limit', 50), 50, hi=200)
    raw = _radio_api('/json/stations/topvote', {'limit': limit * 3, 'hidebroken': 'true'})
    items = _aggregate_stations(raw)
    return jsonify({'items': items[:limit]})


# ── Radio: favorites ─────────────────────────────────────────

@radio_music_bp.route('/radio/favorites', methods=['GET'])
def radio_favorites():
    """
    PHASE 2: Get radio favorites (radio stations only).
    Automatically migrates old subscriptions.json if present.
    Returns only items with source='radio' from unified favorites.json.
    """
    _migrate_old_subscriptions()
    all_favs = _load_json(_user_file('favorites.json'), [])
    # Filter only radio stations (source='radio')
    radio_favs = [f for f in all_favs if f.get('source') == 'radio' or not f.get('source')]
    return jsonify({'items': radio_favs})


@radio_music_bp.route('/radio/favorites', methods=['POST'])
def radio_favorites_edit():
    """
    PHASE 2: Add/remove radio station from unified favorites.json.
    Automatically adds source='radio' field if not present.
    """
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    station = body.get('station')
    if not station or not station.get('uuid'):
        return jsonify({'error': 'Brak danych stacji.'}), 400

    _migrate_old_subscriptions()
    favs = _load_json(_user_file('favorites.json'), [])

    if action == 'remove':
        favs = [f for f in favs if f.get('uuid') != station['uuid']]
    else:
        if not any(f.get('uuid') == station['uuid'] for f in favs):
            # Ensure source='radio' is set
            station = dict(station)
            station.setdefault('source', 'radio')
            station.setdefault('type', 'station')
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


# ── Stream proxy (solves CORS, ICY, HLS issues) ─────────────

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
