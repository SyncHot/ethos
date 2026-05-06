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


def _migrate_old_subscriptions():
    """
    PHASE 2: Migrate old subscriptions.json to unified favorites.json format.
    Converts podcast subscriptions from subscriptions.json to favorites.json with source='podcast'.
    Idempotent: safe to call multiple times.
    Returns: (migrated_count, errors_list)
    """
    sub_file = _user_file('subscriptions.json')
    fav_file = _user_file('favorites.json')
    old_sub_backup = _user_file('old_subscriptions.json')
    
    # Check if old subscriptions.json exists and hasn't been migrated
    if not os.path.exists(sub_file):
        return (0, [])  # Already migrated or never existed
    
    try:
        # Load old subscriptions
        subs = _load_json(sub_file, [])
        if not subs:
            return (0, [])  # Empty, nothing to migrate
        
        # Load existing favorites
        favs = _load_json(fav_file, [])
        
        # Track existing podcast feed URLs to avoid duplicates
        existing_feeds = {f.get('feed_url') for f in favs if f.get('source') == 'podcast'}
        
        migrated = 0
        for sub in subs:
            feed_url = sub.get('feed_url', '')
            if not feed_url or feed_url in existing_feeds:
                continue
            
            # Convert subscription to unified favorites entry
            fav_entry = {
                'id': 'fav-podcast-' + str(hash(feed_url))[-10:].lstrip('-'),
                'title': sub.get('name') or sub.get('title', 'Untitled'),
                'source': 'podcast',
                'type': 'subscription',
                'url': feed_url,
                'feed_url': feed_url,
                'image': sub.get('artwork') or sub.get('image', ''),
                'meta': sub.get('artist') or sub.get('author', ''),
                'added_at': sub.get('subscribed_at', time.time()),
                'genre': sub.get('genre', ''),
                'explicit': sub.get('explicit', False),
                'language': sub.get('language', ''),
            }
            favs.insert(0, fav_entry)
            existing_feeds.add(feed_url)
            migrated += 1
        
        # Save merged favorites
        if migrated > 0:
            _save_json(fav_file, favs)
            
            # Backup old subscriptions.json before deleting
            try:
                subs_backup = _load_json(sub_file, [])
                _save_json(old_sub_backup, subs_backup)
            except Exception:
                pass
            
            # Delete old subscriptions.json
            try:
                os.remove(sub_file)
            except OSError:
                pass
        
        return (migrated, [])
    except Exception as e:
        return (0, [str(e)])


def _normalize_entry(item, source_hint=None):
    """
    PHASE 2: Normalize a queue/history/favorites entry to unified format.
    - Auto-generates ID if missing
    - Infers source from URL patterns if not provided
    - Normalizes field aliases (name→title, favicon→image, etc.)
    - Ensures required fields are present
    Returns: normalized entry dict
    """
    if not item:
        return {}
    
    # Work with a copy to avoid mutations
    entry = dict(item)
    
    # Normalize title (from name, title, or default)
    if 'title' not in entry:
        entry['title'] = entry.get('name', entry.get('title', 'Untitled'))
    entry['name'] = entry['title']  # Keep both for compatibility
    
    # Normalize image (from image, favicon, artwork, thumbnail, etc.)
    if 'image' not in entry:
        entry['image'] = (entry.get('favicon') or entry.get('artwork') 
                         or entry.get('thumbnail') or '')
    
    # Normalize meta (from meta, channel, author, artist)
    if 'meta' not in entry:
        entry['meta'] = (entry.get('channel') or entry.get('author') 
                        or entry.get('artist') or '')
    
    # Infer source if not provided
    if 'source' not in entry and source_hint:
        entry['source'] = source_hint
    elif 'source' not in entry:
        url = entry.get('url', '')
        if '/local/stream' in url:
            entry['source'] = 'local'
        elif 'youtube.com' in url or 'youtu.be' in url:
            entry['source'] = 'youtube'
        elif entry.get('feed_url'):
            entry['source'] = 'podcast'
        elif entry.get('uuid'):
            entry['source'] = 'radio'
        else:
            entry['source'] = 'local'  # Default
    
    # Generate ID if missing (format: item-{source}-{hash})
    if 'id' not in entry:
        title = entry.get('title', '')
        url = entry.get('url', '')
        key = (title + '|' + url).encode()
        hash_val = str(hash(key))[-10:].lstrip('-')
        entry['id'] = 'item-' + entry.get('source', 'unknown') + '-' + hash_val
    
    return entry


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


# ── Play history ─────────────────────────────────────────────

@radio_music_bp.route('/history', methods=['GET'])
def history():
    """
    PHASE 2: Unified history endpoint.
    Supports sorting by date (default) or play_count (most-played).
    Query params:
      - sort=plays → sort by play_count descending (most-played)
      - sort=date → sort by played_at descending (default)
      - limit=N → return top N items
    """
    hfile = _user_file('history.json')
    items = _load_json(hfile, [])
    if _fix_history_types(items):
        _save_json(hfile, items)
    
    # Get sort and limit parameters
    sort = request.args.get('sort', 'date').lower()
    limit = _safe_int(request.args.get('limit', 1000), 1000, hi=10000)
    
    # Sort items
    if sort == 'plays':
        # Sort by play_count descending (most-played)
        items = sorted(items, key=lambda h: h.get('play_count', 1), reverse=True)
    else:
        # Sort by played_at descending (most recent)
        items = sorted(items, key=lambda h: h.get('played_at', 0), reverse=True)
    
    return jsonify({'items': items[:limit]})


@radio_music_bp.route('/history', methods=['POST'])
def history_add():
    body = request.get_json(force=True, silent=True) or {}
    item = body.get('item')
    if not item:
        return jsonify({'error': 'Brak danych.'}), 400

    # Normalize using Phase 2 helper
    item = _normalize_entry(item)

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
    """
    PHASE 2: Deprecated endpoint - redirects to /history?sort=plays
    Kept for backwards compatibility.
    """
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
    """Find similar artists (deprecated: Deezer API removed)."""
    return jsonify({'items': []})


@radio_music_bp.route('/recommendations', methods=['GET'])
def recommendations():
    """
    PHASE 2: Build personalized recommendations from user's history and favorites.
    Uses unified favorites.json (radio + podcasts).
    Automatically migrates old subscriptions.json if present.
    """
    # Migrate old subscriptions if present (Phase 2)
    _migrate_old_subscriptions()
    
    hfile = _user_file('history.json')
    hist = _load_json(hfile, [])
    favs = _load_json(_user_file('favorites.json'), [])

    # ── Extract top tags from radio favorites (only source='radio') ──
    tag_counts = {}
    for fav in favs:
        if fav.get('source') == 'radio':
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

    # ── Extract podcast genres from podcast favorites (source='podcast') ──
    pod_genres = set()
    for fav in favs:
        if fav.get('source') == 'podcast':
            g = (fav.get('genre') or fav.get('category') or '').strip().lower()
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
        # Exclude stations already in favorites (check only radio favorites)
        fav_uuids = {f.get('uuid') for f in favs if f.get('source') == 'radio'}
        items = [s for s in items if s.get('uuid') not in fav_uuids]
        tag_radios[tag] = items[:6]

    threads = [gevent.spawn(_fetch_tag_radio, tag) for tag in top_tags[:3]]
    gevent.joinall(threads, timeout=12)

    return jsonify({
        'top_tags': top_tags,
        'tag_radios': tag_radios,
        'top_artists': top_artists,
        'artist_recs': [],
        'pod_genres': list(pod_genres),
        'has_data': bool(top_tags or top_artists or pod_genres),
    })


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
