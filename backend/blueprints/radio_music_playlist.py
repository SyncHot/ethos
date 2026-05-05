"""Playlist, history, liked songs, recommendations, and unified search routes."""

import json
import os
import re
import time
import urllib.parse
import urllib.request

import gevent
from flask import jsonify, request, Response

from blueprints.radio_music import (
    radio_music_bp, log,
    _user_file, _load_json, _save_json, _safe_int,
    _MAX_HISTORY, _radio_api, _aggregate_stations,
    _deezer_get, _get_deezer_similar_artists,
    _get_music_folders, _probe_audio_cached, _ensure_meta_cache,
    _AUDIO_EXTS, _find_ytdlp, _fmt_secs, _ITUNES_API,
)


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


def _playlists_file():
    return _user_file('playlists.json')


# ── Music: liked songs ───────────────────────────────────────

@radio_music_bp.route('/music/liked', methods=['GET'])
def music_liked():
    return jsonify({'items': _load_json(_user_file('liked_songs.json'), [])})


@radio_music_bp.route('/music/liked', methods=['POST'])
def music_liked_edit():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    track = body.get('track')
    if not track or not track.get('url'):
        return jsonify({'error': 'Brak danych utworu.'}), 400

    liked = _load_json(_user_file('liked_songs.json'), [])

    if action == 'remove':
        liked = [s for s in liked if s.get('url') != track['url']]
    else:
        if not any(s.get('url') == track['url'] for s in liked):
            liked.insert(0, track)

    _save_json(_user_file('liked_songs.json'), liked)
    return jsonify({'ok': True, 'items': liked})


@radio_music_bp.route('/ai-dj/preferences', methods=['GET'])
def ai_dj_preferences_get():
    prefs = _load_json(_user_file('ai_dj_prefs.json'), {'liked_urls': [], 'disliked_urls': [], 'disliked_artists': []})
    return jsonify(prefs)


@radio_music_bp.route('/ai-dj/preferences', methods=['POST'])
def ai_dj_preferences_edit():
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    url = data.get('url', '').strip()
    artist = (data.get('artist') or data.get('name') or '').strip().lower()

    prefs = _load_json(_user_file('ai_dj_prefs.json'), {'liked_urls': [], 'disliked_urls': [], 'disliked_artists': []})

    if action == 'like_url' and url:
        if url not in prefs['liked_urls']:
            prefs['liked_urls'].insert(0, url)
        prefs['disliked_urls'] = [u for u in prefs['disliked_urls'] if u != url]
    elif action == 'unlike_url' and url:
        prefs['liked_urls'] = [u for u in prefs['liked_urls'] if u != url]
    elif action == 'dislike_url' and url:
        if url not in prefs['disliked_urls']:
            prefs['disliked_urls'].append(url)
        prefs['liked_urls'] = [u for u in prefs['liked_urls'] if u != url]
    elif action == 'undislike_url' and url:
        prefs['disliked_urls'] = [u for u in prefs['disliked_urls'] if u != url]
    elif action == 'dislike_artist' and artist:
        if artist not in prefs['disliked_artists']:
            prefs['disliked_artists'].append(artist)
    elif action == 'undislike_artist' and artist:
        prefs['disliked_artists'] = [a for a in prefs['disliked_artists'] if a != artist]
    elif action == 'clear_all':
        prefs = {'liked_urls': [], 'disliked_urls': [], 'disliked_artists': []}
    else:
        return jsonify({'error': 'Unknown action'}), 400

    _save_json(_user_file('ai_dj_prefs.json'), prefs)
    return jsonify({'ok': True, 'prefs': prefs})


@radio_music_bp.route('/ai-dj/seeds', methods=['GET'])
def ai_dj_seeds():
    """Return top seed artists from user's music history (no yt-dlp, fast)."""
    count = _safe_int(request.args.get('count', 10), 10, hi=20)
    hist = _load_json(_user_file('history.json'), [])
    artist_counts = {}
    for h in hist:
        art = (h.get('meta') or h.get('channel') or '').strip()
        if art and h.get('type') in ('music', 'local'):
            artist_counts[art] = artist_counts.get(art, 0) + h.get('play_count', 1)
    top_artists = sorted(artist_counts, key=artist_counts.get, reverse=True)[:count]
    return jsonify({'artists': top_artists})


# ── Play history ─────────────────────────────────────────────

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
    limit = _safe_int(request.args.get('limit', 30), 30, hi=100)
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


# ── Deezer-based recommendations (free, no API key) ─────────

@radio_music_bp.route('/similar-artists', methods=['GET'])
def similar_artists():
    """Find similar artists via Deezer API (free, no key).
    Returns similar artists with their top tracks."""
    artist = request.args.get('artist', '').strip()
    limit = _safe_int(request.args.get('limit', 8), 8, hi=25)
    if not artist:
        return jsonify({'items': []})

    # 1. Find artist on Deezer
    search = _deezer_get('/search/artist', {'q': artist, 'limit': 1})
    results = search.get('data', [])
    if not results:
        return jsonify({'items': []})

    artist_id = results[0].get('id')
    artist_name = results[0].get('name', artist)
    artist_picture = results[0].get('picture_medium', '')

    # 2. Get related artists
    related = _deezer_get(f'/artist/{artist_id}/related', {'limit': limit})
    items = []
    for a in related.get('data', []):
        items.append({
            'id': a.get('id'),
            'name': a.get('name', ''),
            'picture': a.get('picture_medium', ''),
            'fans': a.get('nb_fan', 0),
        })

    return jsonify({
        'source': {'id': artist_id, 'name': artist_name, 'picture': artist_picture},
        'items': items[:limit],
    })


@radio_music_bp.route('/recommendations', methods=['GET'])
def recommendations():
    """Build personalized recommendations from user's history, favorites and subscriptions."""
    hfile = _user_file('history.json')
    hist = _load_json(hfile, [])
    favs = _load_json(_user_file('favorites.json'), [])
    subs = _load_json(_user_file('subscriptions.json'), [])

    # ── Extract top tags from favorites (radio stations have 'tags' field) ──
    tag_counts = {}
    for fav in favs:
        for tag in (fav.get('tags') or '').split(','):
            tag = tag.strip().lower()
            if tag and len(tag) > 1:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = sorted(tag_counts, key=tag_counts.get, reverse=True)[:5]

    # ── Extract top artists from history ──
    artist_counts = {}
    for h in hist:
        art = (h.get('meta') or h.get('channel') or '').strip()
        if art and h.get('type') in ('music', 'local'):
            artist_counts[art] = artist_counts.get(art, 0) + h.get('play_count', 1)
    top_artists = sorted(artist_counts, key=artist_counts.get, reverse=True)[:5]

    # ── Extract podcast genres from subscriptions ──
    pod_genres = set()
    for sub in subs:
        g = (sub.get('genre') or sub.get('category') or '').strip().lower()
        if g:
            pod_genres.add(g)

    # ── Build tag-based radio recommendations (parallel) ──
    tag_radios = {}
    country = request.args.get('country', '').strip().upper() or 'PL'

    def _fetch_tag_radio(tag):
        data = _radio_api('/json/stations/search', {
            'tag': tag, 'limit': 24, 'hidebroken': 'true',
            'order': 'clickcount', 'reverse': 'true',
            'countrycode': country,
        })
        items = _aggregate_stations(data)
        # Exclude stations already in favorites
        fav_uuids = {f.get('uuid') for f in favs}
        items = [s for s in items if s.get('uuid') not in fav_uuids]
        tag_radios[tag] = items[:6]

    threads = [gevent.spawn(_fetch_tag_radio, tag) for tag in top_tags[:3]]
    gevent.joinall(threads, timeout=12)

    # ── Build artist-based music recommendations ──
    artist_recs = []
    if top_artists:
        # Pick top 2 artists, find similar via Deezer
        for art_name in top_artists[:2]:
            for a in _get_deezer_similar_artists(art_name, limit=4):
                artist_recs.append({
                    'name': a['name'],
                    'picture': a['picture'],
                    'because': art_name,
                })

    return jsonify({
        'top_tags': top_tags,
        'tag_radios': tag_radios,
        'top_artists': top_artists,
        'artist_recs': artist_recs,
        'pod_genres': list(pod_genres),
        'has_data': bool(top_tags or top_artists or pod_genres),
    })


@radio_music_bp.route('/lyrics', methods=['GET'])
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
                    'syncedLyrics': synced,
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


# ── Playlists (per-user) ─────────────────────────────────────

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
    pl_id = str(int(time.time() * 1000)) + '_' + os.urandom(3).hex()
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


@radio_music_bp.route('/playlists/<pl_id>/export', methods=['GET'])
def playlists_export(pl_id):
    """Export playlist as M3U8."""
    pls = _load_json(_playlists_file(), [])
    pl = next((p for p in pls if p['id'] == pl_id), None)
    if not pl:
        return jsonify({'error': 'Playlista nie znaleziona'}), 404
    lines = ['#EXTM3U', '# Playlist: ' + pl.get('name', 'Untitled')]
    for tr in pl.get('tracks', []):
        dur = int(tr.get('duration', 0) or 0) if tr.get('duration') else -1
        title = tr.get('name') or tr.get('title') or ''
        artist = tr.get('meta') or tr.get('channel') or ''
        label = (artist + ' - ' + title) if artist else title
        lines.append('#EXTINF:%d,%s' % (dur, label))
        url = tr.get('url') or tr.get('path') or ''
        lines.append(url)
    body = '\n'.join(lines) + '\n'
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', pl.get('name', 'playlist'))[:80]
    return Response(body, mimetype='audio/x-mpegurl', headers={
        'Content-Disposition': 'attachment; filename="%s.m3u8"' % safe_name,
    })


@radio_music_bp.route('/playlists/import', methods=['POST'])
def playlists_import():
    """Import an M3U/M3U8 file as a new playlist."""
    if 'file' not in request.files:
        return jsonify({'error': 'Brak pliku'}), 400
    f = request.files['file']
    chunk = f.read(10 * 1024 * 1024 + 1)  # 10 MB limit
    if len(chunk) > 10 * 1024 * 1024:
        return jsonify({'error': 'Plik M3U jest zbyt duży (max 10 MB)'}), 413
    text = chunk.decode('utf-8', errors='replace')
    lines = text.splitlines()
    name = os.path.splitext(f.filename or 'Import')[0]
    tracks = []
    pending_info = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('#EXTINF:'):
            parts = line.split(',', 1)
            pending_info = {'title': parts[1].strip() if len(parts) > 1 else ''}
        elif line.startswith('#'):
            continue
        else:
            track = {
                'name': pending_info.get('title') or os.path.basename(line),
                'url': line,
                'type': 'local' if line.startswith('/') else 'music',
                'added_at': time.time(),
            }
            if line.startswith('/'):
                track['path'] = line
            tracks.append(track)
            pending_info = {}
    pls = _load_json(_playlists_file(), [])
    pl_id = str(int(time.time() * 1000)) + '_' + os.urandom(3).hex()
    pl = {
        'id': pl_id, 'name': name, 'tracks': tracks,
        'created_at': time.time(), 'updated_at': time.time(),
    }
    pls.insert(0, pl)
    _save_json(_playlists_file(), pls)
    return jsonify({'ok': True, 'playlist': pl, 'items': pls})


# ── Unified Search ───────────────────────────────────────────

@radio_music_bp.route('/search/all', methods=['GET'])
def search_all():
    """Search across radio, podcasts, and local library in parallel."""
    q_str = request.args.get('q', '').strip()
    if not q_str:
        return jsonify({'error': 'Brak zapytania'}), 400
    limit = _safe_int(request.args.get('limit', 10), 10, hi=30)
    results = {'radio': [], 'podcasts': [], 'local': [], 'music': []}

    def _search_radio():
        try:
            raw = _radio_api('/json/stations/search', {
                'name': q_str, 'limit': limit * 2, 'hidebroken': 'true',
                'order': 'clickcount', 'reverse': 'true',
            })
            results['radio'] = _aggregate_stations(raw)[:limit]
        except Exception:
            pass

    def _search_podcasts():
        try:
            enc = urllib.parse.quote(q_str)
            url = '%s?term=%s&media=podcast&limit=%d' % (_ITUNES_API, enc, limit)
            req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
            resp = urllib.request.urlopen(req, timeout=8)
            data = json.loads(resp.read())
            items = []
            for r in data.get('results', []):
                items.append({
                    'name': r.get('collectionName', ''),
                    'artist': r.get('artistName', ''),
                    'artwork': r.get('artworkUrl100', ''),
                    'feed_url': r.get('feedUrl', ''),
                    'genre': r.get('primaryGenreName', ''),
                })
            results['podcasts'] = items[:limit]
        except Exception:
            pass

    def _search_local():
        try:
            q_low = q_str.lower()
            _ensure_meta_cache()
            folders = _get_music_folders()
            matched = []
            for base in folders:
                if not os.path.isdir(base):
                    continue
                for root, _dirs, files in os.walk(base):
                    for fname in files:
                        ext = os.path.splitext(fname)[1].lower()
                        if ext not in _AUDIO_EXTS:
                            continue
                        fpath = os.path.join(root, fname)
                        try:
                            stat = os.stat(fpath)
                        except OSError:
                            continue
                        meta = _probe_audio_cached(fpath, stat.st_mtime)
                        display = meta.get('title') or os.path.splitext(fname)[0]
                        if q_low in display.lower() or q_low in (meta.get('artist') or '').lower() \
                                or q_low in (meta.get('album') or '').lower() or q_low in fname.lower():
                            matched.append({
                                'name': display, 'artist': meta.get('artist', ''),
                                'album': meta.get('album', ''), 'path': fpath,
                                'has_art': meta.get('has_art', False),
                                'duration': meta.get('duration', 0),
                                'folder': base, 'filename': fname, 'type': 'local',
                                'modified': stat.st_mtime,
                            })
                            if len(matched) >= limit:
                                break
                    if len(matched) >= limit:
                        break
                if len(matched) >= limit:
                    break
            results['local'] = matched
        except Exception:
            pass

    def _search_music():
        try:
            ytdlp = _find_ytdlp()
            if not ytdlp:
                return
            from host import host_run, q as shq
            search_arg = 'ytsearch8:' + q_str
            cmd = (shq(ytdlp) + ' --dump-json --flat-playlist --no-warnings '
                   '--no-download ' + shq(search_arg))
            r = host_run(cmd, timeout=15)
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
                                      or 'https://i.ytimg.com/vi/%s/hqdefault.jpg' % vid_id),
                        'url': (d.get('url', '') or d.get('webpage_url', '')
                                or 'https://www.youtube.com/watch?v=' + vid_id),
                        'type': 'music',
                        'source': 'youtube',
                    })
            results['music'] = items
        except Exception:
            pass

    jobs = [gevent.spawn(_search_radio), gevent.spawn(_search_podcasts),
            gevent.spawn(_search_local), gevent.spawn(_search_music)]
    gevent.joinall(jobs, timeout=15)
    return jsonify(results)
