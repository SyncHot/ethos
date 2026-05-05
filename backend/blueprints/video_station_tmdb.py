import time
import json
import urllib.request
import urllib.parse
import urllib.error
from flask import jsonify, request

from blueprints.video_station import (
    video_station_bp,
    _get_db,
    _load_tmdb_key, _save_tmdb_key,
    _tmdb_search, _tmdb_match_video,
    _fetch_tmdb_credits, _download_poster, _download_backdrop,
    _TMDB_BASE,
    _scan_state, _sio, _emit_progress,
    _parse_filename,
)
from blueprints.admin_required import admin_required

_last_tmdb_match_all = 0
@video_station_bp.route("/tmdb-config", methods=["GET"])
def tmdb_config_get():
    key = _load_tmdb_key()
    return jsonify({
        "has_key": bool(key),
        "key_preview": key[:4] + '***' + key[-4:] if len(key) > 8 else ('***' if key else ''),
    })


@video_station_bp.route("/tmdb-config", methods=["POST"])
@admin_required
def tmdb_config_save():
    key = (request.json or {}).get("api_key", "").strip()
    if not key:
        return jsonify({"error": "Brak klucza API."}), 400
    # Validate the key with a test request
    try:
        url = _TMDB_BASE + '/configuration?api_key=' + urllib.parse.quote(key)
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return jsonify({"error": "Klucz API nieprawidlowy."}), 400
    except urllib.error.HTTPError:
        return jsonify({"error": "Klucz API nieprawidlowy."}), 400
    except Exception as e:
        return jsonify({"error": "Blad weryfikacji: " + str(e)}), 500
    _save_tmdb_key(key)
    return jsonify({"ok": True})


@video_station_bp.route("/tmdb-match/<int:vid>", methods=["POST"])

def tmdb_match_one(vid):
    conn = _get_db()
    r = conn.execute("SELECT id, filename FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    api_key = _load_tmdb_key()
    if not api_key:
        conn.close()
        return jsonify({"error": "Brak klucza TMDb. Skonfiguruj w ustawieniach."}), 400
    result = _tmdb_match_video(conn, r["id"], r["filename"], api_key)
    conn.close()
    if not result:
        return jsonify({"error": "Nie znaleziono dopasowania w TMDb."}), 404
    return jsonify({"ok": True, "match": result})



@video_station_bp.route("/tmdb-match-all", methods=["POST"])
@admin_required
def tmdb_match_all():
    global _last_tmdb_match_all
    api_key = _load_tmdb_key()
    if not api_key:
        return jsonify({"error": "Brak klucza TMDb."}), 400
    if _scan_state["running"]:
        return jsonify({"error": "Skan juz trwa, poczekaj az sie skonczy."}), 409
    now = time.time()
    if now - _last_tmdb_match_all < 60:
        remaining = int(60 - (now - _last_tmdb_match_all))
        return jsonify({"error": "Poczekaj %ds przed kolejnym dopasowaniem." % remaining}), 429
    _last_tmdb_match_all = now
    import gevent

    def _do_match():
        _scan_state.update(running=True, stop_requested=False,
                           total=0, processed=0, current_file='')
        conn = _get_db()
        unmatched = conn.execute(
            "SELECT id, filename FROM videos "
            "WHERE (tmdb_id=0 OR tmdb_id IS NULL) AND COALESCE(hidden,0)=0"
        ).fetchall()
        _scan_state['total'] = len(unmatched)
        _emit_progress()
        for i, row in enumerate(unmatched):
            if _scan_state.get('stop_requested'):
                break
            _scan_state['current_file'] = row['filename']
            _scan_state['processed'] = i + 1
            try:
                _tmdb_match_video(conn, row['id'], row['filename'], api_key)
            except Exception as e:
                log.debug('TMDb match-all failed for %s: %s', row['filename'], e)
            if (i + 1) % 3 == 0:
                _emit_progress()
            gevent.sleep(0.3)
        conn.close()
        _scan_state.update(running=False, stop_requested=False, current_file='')
        _emit_progress()
        s = _sio()
        if s:
            s.emit('vs_scan_done', {
                'total_processed': _scan_state['processed'],
                'duration': 0, 'message': 'Dopasowanie TMDb zakonczone.'
            })

    gevent.spawn(_do_match)
    return jsonify({"ok": True})


@video_station_bp.route("/tmdb-search-list", methods=["GET"])

def tmdb_search_list():
    """Search TMDb and return a list of results for manual selection."""
    query = request.args.get("q", "").strip()
    year  = request.args.get("year", "").strip()
    if not query:
        return jsonify({"results": []})
    api_key = _load_tmdb_key()
    if not api_key:
        return jsonify({"error": "Brak klucza TMDb. Skonfiguruj w ustawieniach."}), 400
    results = []
    for endpoint, media_type in [('/search/movie', 'movie'), ('/search/tv', 'tv')]:
        try:
            params = {'api_key': api_key, 'query': query, 'language': 'pl-PL', 'page': 1}
            if year:
                params['year' if media_type == 'movie' else 'first_air_date_year'] = year
            url = _TMDB_BASE + endpoint + '?' + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            for r in (data.get('results') or [])[:5]:
                title = r.get('title' if media_type == 'movie' else 'name', '')
                date = r.get('release_date' if media_type == 'movie' else 'first_air_date', '') or ''
                results.append({
                    'tmdb_id': r.get('id', 0),
                    'title': title,
                    'year': date[:4],
                    'overview': (r.get('overview', '') or '')[:200],
                    'poster_path': r.get('poster_path', ''),
                    'rating': round(r.get('vote_average', 0), 1),
                    'media_type': media_type,
                })
        except Exception as e:
            log.debug('tmdb_search_list %s failed: %s', endpoint, e)
    results.sort(key=lambda x: x.get('rating', 0), reverse=True)
    return jsonify({"results": results[:10]})


@video_station_bp.route("/tmdb-apply/<int:vid>", methods=["POST"])

def tmdb_apply(vid):
    """Apply a specific TMDb result (chosen by user) to a video."""
    d = request.json or {}
    tmdb_id = d.get("tmdb_id")
    media_type = d.get("type", "movie")
    if not tmdb_id:
        return jsonify({"error": "Brak tmdb_id."}), 400
    if media_type not in ('movie', 'tv'):
        return jsonify({"error": "Nieprawidłowy typ."}), 400
    api_key = _load_tmdb_key()
    if not api_key:
        return jsonify({"error": "Brak klucza TMDb."}), 400
    conn = _get_db()
    r = conn.execute("SELECT id FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    try:
        url = '%s/%s/%d?api_key=%s&language=pl-PL' % (
            _TMDB_BASE, media_type, int(tmdb_id), urllib.parse.quote(api_key))
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            details = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        conn.close()
        return jsonify({"error": "Błąd pobierania z TMDb: " + str(e)}), 500
    title_key = 'title' if media_type == 'movie' else 'name'
    date_key = 'release_date' if media_type == 'movie' else 'first_air_date'
    tmdb_title = details.get(title_key, '')
    tmdb_year = (details.get(date_key, '') or '')[:4]
    tmdb_genres = ','.join(str(g['id']) for g in (details.get('genres') or []))
    poster_path = details.get('poster_path', '')
    backdrop_path = details.get('backdrop_path', '')
    rating = details.get('vote_average', 0)
    overview = details.get('overview', '')
    cast, director = _fetch_tmdb_credits(tmdb_id, media_type, api_key)
    conn.execute(
        'UPDATE videos SET tmdb_id=?, tmdb_title=?, tmdb_overview=?, '
        'tmdb_year=?, tmdb_rating=?, tmdb_genres=?, tmdb_poster_path=?, '
        'tmdb_backdrop_path=?, tmdb_cast=?, tmdb_director=?, tmdb_media_type=? WHERE id=?',
        (tmdb_id, tmdb_title, overview, tmdb_year, rating, tmdb_genres,
         poster_path, backdrop_path, cast, director, media_type, vid))
    conn.commit()
    if tmdb_title:
        display = tmdb_title + (' (' + tmdb_year + ')' if tmdb_year else '')
        conn.execute('UPDATE videos SET title=? WHERE id=?', (display, vid))
        conn.commit()
    poster_ok = False
    if poster_path:
        poster_ok = _download_poster(poster_path, vid)
        if poster_ok:
            conn.execute('UPDATE videos SET poster_ok=1 WHERE id=?', (vid,))
            conn.commit()
    backdrop_ok = False
    if backdrop_path:
        backdrop_ok = _download_backdrop(backdrop_path, vid)
        if backdrop_ok:
            conn.execute('UPDATE videos SET backdrop_ok=1 WHERE id=?', (vid,))
            conn.commit()
    conn.close()
    return jsonify({"ok": True, "title": tmdb_title, "year": tmdb_year,
                    "poster_ok": poster_ok, "backdrop_ok": backdrop_ok})
