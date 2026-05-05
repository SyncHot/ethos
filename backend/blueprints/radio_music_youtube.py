"""YouTube / yt-dlp music and archive routes for the Radio & Music blueprint."""

import json
import os
import re
import shutil
import time

import gevent
import socket
import urllib.request

from flask import g, jsonify, request, Response, send_file, redirect

from host import get_user_home, q as shq
from blueprints.radio_music import (
    radio_music_bp, log, _sio,
    _find_ytdlp, _fmt_secs, _extract_audio_url,
    _archive_dir, _archive_key, _load_archive, _save_archive,
    _ARCHIVE_LOCK, _ARCHIVE_SEM,
    _safe_int, _deezer_get, _get_deezer_similar_artists,
    _user_file, _load_json,
    _DOWNLOAD_JOBS, _DOWNLOAD_LOCK, _DATA_DIR,
    _YTDLP_URL_CACHE, _VARIOUS_ARTISTS_PLAYLIST, _SSL_CTX,
    _default_music_dir,
)


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


def _add_to_playlist_by_name(track_info, username, playlist_name):
    """Auto-add a downloaded track to a named playlist (create if missing)."""
    try:
        user_dir = os.path.join(_DATA_DIR, 'users', username)
        os.makedirs(user_dir, exist_ok=True)
        pfile = os.path.join(user_dir, 'playlists.json')
        from blueprints.radio_music import _load_json, _save_json
        pls = _load_json(pfile, [])
        pl = next((p for p in pls if p.get('name') == playlist_name), None)
        if not pl:
            pl = {
                'id': str(int(time.time() * 1000)) + '_' + os.urandom(3).hex(),
                'name': playlist_name,
                'tracks': [],
                'created_at': time.time(),
                'updated_at': time.time(),
            }
            pls.append(pl)
        # Skip if track URL already in this playlist
        if any(t.get('url') == track_info.get('url') for t in pl.get('tracks', [])):
            return
        track_info['added_at'] = time.time()
        pl['tracks'].append(track_info)
        pl['updated_at'] = time.time()
        _save_json(pfile, pls)
    except Exception:
        pass


# ── Chromecast helpers ──────────────────────────────────────

@radio_music_bp.route('/cast-info', methods=['GET'])
def cast_info():
    """Return NAS LAN IP(s) and origin for Chromecast URL building.

    Chromecast cannot use 127.0.0.1 or hostnames it doesn't know.
    This endpoint exposes the real LAN IP so the frontend can build
    absolute URLs that Chromecast can reach.
    """
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

@radio_music_bp.route('/music/download', methods=['POST'])
def music_download():
    """Download a track to the user's music folder using yt-dlp."""
    body = request.get_json(force=True, silent=True) or {}
    url = body.get('url', '').strip()
    title = body.get('title', 'Unknown')
    folder = body.get('folder', '').strip()
    playlist = body.get('playlist', '').strip()
    track_meta = {
        'type': body.get('type', 'music'),
        'url': url,
        'title': title,
        'artist': body.get('artist', ''),
        'thumbnail': body.get('thumbnail', ''),
        'duration': body.get('duration', 0),
        'source': body.get('source', 'youtube'),
    }
    if not url:
        return jsonify({'error': 'Brak URL'}), 400

    ytdlp = _find_ytdlp()
    if not ytdlp:
        return jsonify({'error': 'yt-dlp nie jest zainstalowane'}), 503

    username = getattr(g, 'username', None) or 'default'
    if folder:
        dest = os.path.join(get_user_home(username), folder)
    else:
        dest = _music_download_dir()
    os.makedirs(dest, exist_ok=True)
    job_id = str(int(time.time() * 1000)) + '_' + os.urandom(3).hex()
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_JOBS[job_id] = {
            'status': 'downloading', 'progress': 0,
            'title': title, 'error': None, 'path': None,
        }

    target_playlist = playlist or _VARIOUS_ARTISTS_PLAYLIST

    from host import host_run

    def _do_download():
        try:
            if not playlist:
                # Single track → flat file in Various Artists folder
                out_tmpl = os.path.join(
                    dest, _VARIOUS_ARTISTS_PLAYLIST,
                    '%(title)s.%(ext)s'
                )
            else:
                # Playlist context → Artist/Album/Title.mp3
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
                    _DOWNLOAD_JOBS[job_id]['finished_at'] = time.time()
                    success = True
                else:
                    _DOWNLOAD_JOBS[job_id]['status'] = 'error'
                    _DOWNLOAD_JOBS[job_id]['error'] = (r.stderr or 'Nieznany błąd')[:200]
                    _DOWNLOAD_JOBS[job_id]['finished_at'] = time.time()
                    success = False
            if success:
                _add_to_playlist_by_name(track_meta, username, target_playlist)
        except Exception as e:
            with _DOWNLOAD_LOCK:
                _DOWNLOAD_JOBS[job_id]['status'] = 'error'
                _DOWNLOAD_JOBS[job_id]['error'] = str(e)[:200]
                _DOWNLOAD_JOBS[job_id]['finished_at'] = time.time()

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

    job_id = str(int(time.time() * 1000)) + '_' + os.urandom(3).hex()
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
            _DOWNLOAD_JOBS[job_id]['finished_at'] = time.time()
            if errors:
                _DOWNLOAD_JOBS[job_id]['error'] = f'Błędy: {", ".join(errors[:5])}'
        _cleanup_intermediates(dest)

    gevent.spawn(_do_batch)
    return jsonify({'ok': True, 'job_id': job_id})


@radio_music_bp.route('/music/downloads', methods=['GET'])
def music_downloads_status():
    """Return status of active/recent download jobs."""
    with _DOWNLOAD_LOCK:
        # Clean up completed jobs older than 5 minutes
        now = time.time()
        to_remove = [jid for jid, j in _DOWNLOAD_JOBS.items()
                     if j['status'] in ('done', 'done_partial', 'error')
                     and now - j.get('finished_at', now) > 300]
        for jid in to_remove:
            del _DOWNLOAD_JOBS[jid]
        jobs = dict(_DOWNLOAD_JOBS)
    return jsonify({'jobs': jobs})


# ── Music: YouTube / multi-source (via yt-dlp) ──────────────

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

    from blueprints import radio_music as _rm
    _rm._YTDLP_BIN = None
    return jsonify({'ok': True, 'ready': bool(_find_ytdlp())})


@radio_music_bp.route('/music/search', methods=['GET'])
def music_search():
    """Search for music via yt-dlp (YouTube by default)."""
    q_str = request.args.get('q', '').strip()
    limit = _safe_int(request.args.get('limit', 20), 20, hi=50)
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


@radio_music_bp.route('/ai-dj/next', methods=['GET'])
def ai_dj_next():
    """Generate next batch of AI DJ tracks from user history + Deezer similarity."""
    count = _safe_int(request.args.get('count', 10), 10, hi=30)
    artist = request.args.get('artist', '').strip()
    exclude_raw = request.args.get('exclude', '')
    exclude_set = set(u for u in exclude_raw.split(',') if u)
    disliked_raw = request.args.get('disliked_artists', '')
    disliked_artists = set(a.strip().lower() for a in disliked_raw.split(',') if a.strip())
    # Also merge with stored per-user preferences
    prefs = _load_json(_user_file('ai_dj_prefs.json'), {'liked_urls': [], 'disliked_urls': [], 'disliked_artists': []})
    disliked_artists.update(prefs.get('disliked_artists', []))
    disliked_urls_stored = set(prefs.get('disliked_urls', []))

    hfile = _user_file('history.json')
    hist = _load_json(hfile, [])

    # Extract top artists from history (same logic as /recommendations)
    artist_counts = {}
    for h in hist:
        art = (h.get('meta') or h.get('channel') or '').strip()
        if art and h.get('type') in ('music', 'local'):
            artist_counts[art] = artist_counts.get(art, 0) + h.get('play_count', 1)
    top_artists = sorted(artist_counts, key=artist_counts.get, reverse=True)[:5]

    # Use ONE seed artist: currently playing (from param) OR top-1 from history.
    # Using a single seed keeps the playlist stylistically coherent.
    seed_artist = artist or (top_artists[0] if top_artists else None)

    # Build YouTube search queries from similar artists of the single seed
    queries = []

    if seed_artist:
        similar = [a['name'] for a in _get_deezer_similar_artists(seed_artist, limit=6)]
        # Seed itself comes first so the playlist anchors around it
        if seed_artist.lower() not in disliked_artists:
            queries.append('%s best songs' % seed_artist)
        for s in similar:
            if s.lower() not in disliked_artists:
                queries.append('%s music' % s)

    # If no Deezer results, fall back to tags from history/favorites
    if not queries:
        favs = _load_json(_user_file('favorites.json'), [])
        tag_counts = {}
        for fav in favs:
            for tag in (fav.get('tags') or '').split(','):
                tag = tag.strip().lower()
                if tag and len(tag) > 1:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
        top_tags = sorted(tag_counts, key=tag_counts.get, reverse=True)[:5]
        for tag in top_tags:
            queries.append('%s music 2025' % tag)

    # Absolute fallback
    if not queries:
        queries.append('popular music hits 2025')

    # Search YouTube via yt-dlp in parallel per query
    ytdlp = _find_ytdlp()
    if not ytdlp:
        return jsonify({'items': [], 'error': 'yt-dlp not installed'}), 503

    from host import host_run

    def _search_query(q, limit_per_q):
        search_arg = f'ytsearch{limit_per_q}:{q}'
        cmd = (f'{shq(ytdlp)} --dump-json --flat-playlist --no-warnings '
               f'--no-download {shq(search_arg)}')
        r = host_run(cmd, timeout=20)
        results = []
        if r.stdout:
            for line in r.stdout.strip().splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                vid_id = d.get('id', '')
                dur = d.get('duration') or 0
                url = d.get('url', '') or d.get('webpage_url', '') or f'https://www.youtube.com/watch?v={vid_id}'
                if url in exclude_set:
                    continue
                results.append({
                    'id': vid_id,
                    'title': d.get('title', ''),
                    'channel': d.get('channel', d.get('uploader', '')),
                    'duration': dur,
                    'duration_fmt': _fmt_secs(dur),
                    'thumbnail': (d.get('thumbnails', [{}])[-1].get('url', '')
                                  or f'https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg'),
                    'url': url,
                    'type': 'music',
                    'source': 'youtube',
                })
        return results

    items = []
    seen_urls = set(exclude_set)
    per_query = max(3, count // max(1, len(queries)))
    threads = [gevent.spawn(lambda q=q: _search_query(q, per_query)) for q in queries[:6]]
    gevent.joinall(threads, timeout=25)

    for t in threads:
        if t.value:
            for it in t.value:
                if it['url'] not in seen_urls and it['url'] not in disliked_urls_stored and (it.get('channel', '') or '').lower() not in disliked_artists:
                    seen_urls.add(it['url'])
                    items.append(it)

    # Keep order: seed artist tracks first, then similar artists in sequence
    return jsonify({'items': items[:count]})


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
                            music_rm_dir = os.path.join(get_user_home(req_username), 'Music', 'RadioMusic')
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
        changed = False
        for url in urls:
            k = _archive_key(url)
            entry = db.get(k, {})
            status = entry.get('status', 'none')
            if status == 'done':
                nas_path = entry.get('nas_path', '')
                if not nas_path or not os.path.isfile(nas_path):
                    status = 'none'
                    db.pop(k, None)
                    changed = True
            results[url] = {
                'key': k,
                'status': status,
                'progress': entry.get('progress', 0),
                'size_bytes': entry.get('size_bytes', 0),
                'title': entry.get('title', ''),
            }
        if changed:
            _save_archive(db)
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
        # File deleted from disk — purge stale DB entry so UI resets to "not archived"
        with _ARCHIVE_LOCK:
            db2 = _load_archive()
            db2.pop(key, None)
            _save_archive(db2)
        return jsonify({'error': 'File missing on NAS — re-archive to download again'}), 404
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
