"""
Video Station -- video library manager with streaming.

Routes:
  GET  /api/video-station/pkg-status      - dependency & library status
  POST /api/video-station/install         - install ffmpeg
  POST /api/video-station/uninstall       - cleanup
  GET  /api/video-station/library         - list videos
  GET  /api/video-station/folders         - configured library folders
  POST /api/video-station/folders         - save library folders
  POST /api/video-station/scan            - start background library scan
  GET  /api/video-station/scan-status     - scan progress
  POST /api/video-station/scan-stop       - stop running scan
  GET  /api/video-station/info/<int:vid>  - detailed video metadata
  GET  /api/video-station/stream/<int:vid>- stream video file
  GET  /api/video-station/thumb/<int:vid> - video thumbnail
  GET  /api/video-station/poster/<int:vid>- TMDb poster image
  GET  /api/video-station/recent          - recently added videos
  GET  /api/video-station/collections     - auto-generated collections
  POST /api/video-station/watched/<int:vid> - mark as watched / update position
  GET  /api/video-station/tmdb-config     - get TMDb API key status
  POST /api/video-station/tmdb-config     - save TMDb API key
  POST /api/video-station/tmdb-match/<int:vid> - manually trigger TMDb match for a video
  POST /api/video-station/tmdb-match-all  - match all unmatched videos

SocketIO events emitted:
  vs_scan_progress  - {running, total, processed, current_file}
  vs_scan_done      - {total_processed, duration}
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import time
import urllib.request
import urllib.parse
import urllib.error

from flask import Blueprint, jsonify, request, Response, send_file

from host import host_run, q, data_path, app_path

try:
    from blueprints.admin_required import admin_required, require_auth
except ImportError:
    def admin_required(f): return f
    def require_auth(f): return f

log = logging.getLogger('ethos.video_station')

video_station_bp = Blueprint('video-station', __name__, url_prefix='/api/video-station')

_DB_PATH = data_path('video_station.db')
_THUMB_DIR = data_path('video_thumbs')
_POSTER_DIR = data_path('video_posters')
_TMDB_CONF = data_path('video_tmdb.json')

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
              '.mpg', '.mpeg', '.ts', '.3gp', '.ogv', '.vob'}

_scan_state = {
    'running': False, 'stop_requested': False,
    'total': 0, 'processed': 0, 'current_file': '',
}


# --- Database ---

def _get_db():
    conn = sqlite3.connect(_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def _init_db():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            folder TEXT,
            title TEXT,
            duration REAL DEFAULT 0,
            width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0,
            codec TEXT,
            audio_codec TEXT,
            bitrate INTEGER DEFAULT 0,
            file_size INTEGER DEFAULT 0,
            file_mtime REAL DEFAULT 0,
            added_at REAL DEFAULT 0,
            thumb_ok INTEGER DEFAULT 0,
            metadata_json TEXT
        );
        CREATE TABLE IF NOT EXISTS watch_state (
            video_id INTEGER PRIMARY KEY,
            watched INTEGER DEFAULT 0,
            position REAL DEFAULT 0,
            updated_at REAL DEFAULT 0,
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_videos_folder ON videos(folder);
        CREATE INDEX IF NOT EXISTS idx_videos_filename ON videos(filename);
    """)
    conn.commit()
    conn.close()


try:
    _init_db()
except Exception:
    pass


def _migrate_db():
    """Add TMDb columns if they don't exist."""
    try:
        conn = _get_db()
        cols = {r[1] for r in conn.execute('PRAGMA table_info(videos)').fetchall()}
        migrations = [
            ('tmdb_id', 'INTEGER DEFAULT 0'),
            ('tmdb_title', 'TEXT'),
            ('tmdb_overview', 'TEXT'),
            ('tmdb_year', 'TEXT'),
            ('tmdb_rating', 'REAL DEFAULT 0'),
            ('tmdb_genres', 'TEXT'),
            ('tmdb_poster_path', 'TEXT'),
            ('poster_ok', 'INTEGER DEFAULT 0'),
        ]
        for col_name, col_def in migrations:
            if col_name not in cols:
                conn.execute('ALTER TABLE videos ADD COLUMN %s %s' % (col_name, col_def))
        conn.commit()
        conn.close()
    except Exception:
        pass


_migrate_db()


# --- Helpers ---

def _sio():
    return getattr(video_station_bp, '_socketio', None)


def _emit_progress():
    s = _sio()
    if s:
        s.emit('vs_scan_progress', {
            'running': _scan_state['running'],
            'total': _scan_state['total'],
            'processed': _scan_state['processed'],
            'current_file': os.path.basename(_scan_state['current_file']),
        })


def _check_deps():
    return {
        'ffmpeg': shutil.which('ffmpeg') is not None,
        'ffprobe': shutil.which('ffprobe') is not None,
    }


def _all_deps_ok():
    d = _check_deps()
    return d['ffmpeg'] and d['ffprobe']


def _load_folders():
    p = data_path('video_folders.json')
    if os.path.isfile(p):
        try:
            return json.loads(open(p).read())
        except Exception:
            pass
    return []


def _save_folders(folders):
    p = data_path('video_folders.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w') as f:
        json.dump(folders, f)


def _collect_videos(folders):
    vids = []
    for folder in folders:
        fp = os.path.realpath(folder)
        if not os.path.isdir(fp):
            continue
        for root, _, files in os.walk(fp):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    vids.append(os.path.join(root, fn))
    return vids


def _probe_video(path):
    try:
        cmd = 'ffprobe -v quiet -print_format json -show_format -show_streams ' + q(path)
        out = host_run(cmd, timeout=30)
        data = json.loads(out)
        fmt = data.get('format', {})
        vstream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
        astream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), {})
        return {
            'duration': float(fmt.get('duration', 0)),
            'width': int(vstream.get('width', 0)),
            'height': int(vstream.get('height', 0)),
            'codec': vstream.get('codec_name', ''),
            'audio_codec': astream.get('codec_name', ''),
            'bitrate': int(fmt.get('bit_rate', 0)),
            'title': fmt.get('tags', {}).get('title', ''),
        }
    except Exception as e:
        log.debug('ffprobe failed for %s: %s', path, e)
        return {}


def _generate_thumb(path, video_id, duration=0):
    os.makedirs(_THUMB_DIR, exist_ok=True)
    thumb_path = os.path.join(_THUMB_DIR, str(video_id) + '.jpg')
    seek = min(duration * 0.1, 30) if duration > 10 else 2
    try:
        cmd = 'ffmpeg -y -ss %.1f -i %s -vframes 1 -vf scale=320:-1 -q:v 4 %s' % (seek, q(path), q(thumb_path))
        host_run(cmd, timeout=30)
        return os.path.isfile(thumb_path)
    except Exception:
        return False


def _format_duration(secs):
    if not secs:
        return '0:00'
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    if h:
        return '%d:%02d:%02d' % (h, m, s)
    return '%d:%02d' % (m, s)


# --- TMDb integration ---

_TMDB_BASE = 'https://api.themoviedb.org/3'
_TMDB_IMG_BASE = 'https://image.tmdb.org/t/p/w500'

# Common noise tokens stripped from filenames before TMDb search
_NOISE_RE = re.compile(
    r'\b('
    r'720p|1080p|1080i|2160p|4k|uhd|hdr|hdr10|dolby|atmos|dts|aac|ac3|'
    r'bluray|blu-ray|bdrip|brrip|dvdrip|webrip|web-dl|webdl|hdtv|hdrip|'
    r'x264|x265|h264|h265|hevc|avc|xvid|divx|'
    r'remastered|extended|directors.cut|unrated|theatrical|'
    r'proper|repack|internal|limited|'
    r'yts|yify|rarbg|eztv|ettv|sparks|geckos|fgt|'
    r'mkv|mp4|avi|mov'
    r')\b', re.IGNORECASE
)
_YEAR_RE = re.compile(r'[\(\[\.]?((?:19|20)\d{2})[\)\]\.]?')
_CLEAN_RE = re.compile(r'[\.\-_]+')
_MULTI_SPACE = re.compile(r'\s{2,}')


def _parse_filename(filename):
    """Extract title and year from a video filename."""
    name = os.path.splitext(filename)[0]
    # Try to find year first — everything before it is likely the title
    year_match = _YEAR_RE.search(name)
    year = ''
    if year_match:
        year = year_match.group(1)
        name = name[:year_match.start()]
    # Replace dots/dashes/underscores with spaces
    name = _CLEAN_RE.sub(' ', name)
    # Remove noise tokens
    name = _NOISE_RE.sub('', name)
    name = _MULTI_SPACE.sub(' ', name).strip()
    return name, year


def _load_tmdb_key():
    if os.path.isfile(_TMDB_CONF):
        try:
            return json.loads(open(_TMDB_CONF).read()).get('api_key', '')
        except Exception:
            pass
    return ''


def _save_tmdb_key(key):
    os.makedirs(os.path.dirname(_TMDB_CONF), exist_ok=True)
    with open(_TMDB_CONF, 'w') as f:
        json.dump({'api_key': key}, f)


def _tmdb_search(title, year='', api_key=''):
    """Search TMDb for a movie/TV show. Returns best match dict or None."""
    if not api_key:
        api_key = _load_tmdb_key()
    if not api_key:
        return None
    params = urllib.parse.urlencode({
        'api_key': api_key, 'query': title, 'language': 'pl-PL',
    })
    if year:
        params += '&year=' + str(year)

    # Try movie search first
    for endpoint in ['/search/movie', '/search/tv']:
        try:
            url = _TMDB_BASE + endpoint + '?' + params
            req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            results = data.get('results', [])
            if results:
                r = results[0]
                is_movie = endpoint == '/search/movie'
                return {
                    'tmdb_id': r.get('id', 0),
                    'tmdb_title': r.get('title' if is_movie else 'name', ''),
                    'tmdb_overview': r.get('overview', ''),
                    'tmdb_year': (r.get('release_date' if is_movie else 'first_air_date', '') or '')[:4],
                    'tmdb_rating': r.get('vote_average', 0),
                    'tmdb_genres': ','.join(str(g) for g in r.get('genre_ids', [])),
                    'tmdb_poster_path': r.get('poster_path', ''),
                    'media_type': 'movie' if is_movie else 'tv',
                }
        except Exception as e:
            log.debug('TMDb search failed (%s): %s', endpoint, e)
    return None


def _download_poster(poster_path, video_id):
    """Download TMDb poster and save locally."""
    if not poster_path:
        return False
    os.makedirs(_POSTER_DIR, exist_ok=True)
    local = os.path.join(_POSTER_DIR, str(video_id) + '.jpg')
    try:
        url = _TMDB_IMG_BASE + poster_path
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            with open(local, 'wb') as f:
                f.write(resp.read())
        return os.path.isfile(local)
    except Exception as e:
        log.debug('Poster download failed for vid %s: %s', video_id, e)
        return False


def _tmdb_match_video(conn, video_id, filename, api_key=''):
    """Parse filename, search TMDb, update DB, download poster."""
    title, year = _parse_filename(filename)
    if not title:
        return None
    result = _tmdb_search(title, year, api_key)
    if not result:
        return None
    conn.execute(
        'UPDATE videos SET tmdb_id=?, tmdb_title=?, tmdb_overview=?, '
        'tmdb_year=?, tmdb_rating=?, tmdb_genres=?, tmdb_poster_path=? WHERE id=?',
        (result['tmdb_id'], result['tmdb_title'], result['tmdb_overview'],
         result['tmdb_year'], result['tmdb_rating'], result['tmdb_genres'],
         result['tmdb_poster_path'], video_id))
    conn.commit()
    # Update display title to TMDb title
    if result['tmdb_title']:
        display = result['tmdb_title']
        if result['tmdb_year']:
            display += ' (' + result['tmdb_year'] + ')'
        conn.execute('UPDATE videos SET title=? WHERE id=?', (display, video_id))
        conn.commit()
    # Download poster
    if result['tmdb_poster_path']:
        ok = _download_poster(result['tmdb_poster_path'], video_id)
        if ok:
            conn.execute('UPDATE videos SET poster_ok=1 WHERE id=?', (video_id,))
            conn.commit()
    return result


# --- Scan worker ---

def _scan_worker(folders, use_tmdb=False):
    import gevent
    t0 = time.time()
    all_paths = _collect_videos(folders)
    conn = _get_db()
    existing = {r["path"] for r in conn.execute("SELECT path FROM videos").fetchall()}
    todo = []
    for p in all_paths:
        try:
            mt = os.path.getmtime(p)
            sz = os.path.getsize(p)
        except OSError:
            continue
        if p in existing:
            row = conn.execute("SELECT file_mtime FROM videos WHERE path=?", (p,)).fetchone()
            if row and abs(mt - row["file_mtime"]) < 1.0:
                continue
        todo.append((p, mt, sz))

    already = len(all_paths) - len(todo)
    _scan_state.update(total=len(all_paths), processed=already, current_file="")
    _emit_progress()

    all_set = set(all_paths)
    gone = [r["id"] for r in conn.execute("SELECT id, path FROM videos").fetchall() if r["path"] not in all_set]
    if gone:
        conn.executemany("DELETE FROM videos WHERE id=?", [(g,) for g in gone])
        conn.commit()

    if not todo:
        _scan_state["running"] = False
        _emit_progress()
        s = _sio()
        if s:
            s.emit("vs_scan_done", {"total_processed": 0, "duration": 0, "message": "Brak nowych filmow."})
        conn.close()
        return

    for i, (path, mt, sz) in enumerate(todo):
        if _scan_state.get("stop_requested"):
            break
        _scan_state["current_file"] = path
        gevent.sleep(0.1)
        meta = _probe_video(path)
        fn = os.path.basename(path)
        folder = os.path.dirname(path)
        title = meta.get("title") or os.path.splitext(fn)[0]
        conn.execute(
            "INSERT INTO videos (path,filename,folder,title,duration,width,height,"
            "codec,audio_codec,bitrate,file_size,file_mtime,added_at,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "title=excluded.title,duration=excluded.duration,width=excluded.width,"
            "height=excluded.height,codec=excluded.codec,audio_codec=excluded.audio_codec,"
            "bitrate=excluded.bitrate,file_size=excluded.file_size,"
            "file_mtime=excluded.file_mtime,metadata_json=excluded.metadata_json",
            (path, fn, folder, title, meta.get("duration", 0),
             meta.get("width", 0), meta.get("height", 0),
             meta.get("codec", ""), meta.get("audio_codec", ""),
             meta.get("bitrate", 0), sz, mt, time.time(), json.dumps(meta)))
        conn.commit()
        vid_row = conn.execute("SELECT id FROM videos WHERE path=?", (path,)).fetchone()
        if vid_row:
            ok = _generate_thumb(path, vid_row["id"], meta.get("duration", 0))
            if ok:
                conn.execute("UPDATE videos SET thumb_ok=1 WHERE id=?", (vid_row["id"],))
                conn.commit()
            # TMDb matching during scan
            if use_tmdb and _load_tmdb_key():
                try:
                    _tmdb_match_video(conn, vid_row["id"], fn)
                    gevent.sleep(0.3)  # respect TMDb rate limit (~40 req/10s)
                except Exception as e:
                    log.debug('TMDb match failed for %s: %s', fn, e)
        _scan_state["processed"] = already + i + 1
        if (i + 1) % 3 == 0 or i == 0:
            _emit_progress()
            gevent.sleep(0.05)

    conn.close()
    dur = time.time() - t0
    _scan_state.update(running=False, stop_requested=False, current_file="")
    _emit_progress()
    s = _sio()
    if s:
        s.emit("vs_scan_done", {"total_processed": _scan_state["processed"], "duration": round(dur, 1)})


# --- Routes ---

@video_station_bp.route("/pkg-status", methods=["GET"])
@require_auth
def pkg_status():
    deps = _check_deps()
    stats = {}
    if os.path.isfile(_DB_PATH):
        try:
            c = _get_db()
            stats = {
                "videos": c.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
                "total_size": c.execute("SELECT COALESCE(SUM(file_size),0) FROM videos").fetchone()[0],
                "watched": c.execute("SELECT COUNT(*) FROM watch_state WHERE watched=1").fetchone()[0],
            }
            c.close()
        except Exception:
            pass
    return jsonify({"installed": deps["ffmpeg"] and deps["ffprobe"], "deps": deps, "stats": stats, "scanning": _scan_state["running"]})


@video_station_bp.route("/install", methods=["POST"])
@admin_required
def install_deps():
    import gevent
    def _do():
        s = _sio()
        try:
            if s:
                s.emit("vs_install", {"stage": "start", "percent": 10, "message": "Instalowanie ffmpeg..."})
            host_run("apt-get update -qq && apt-get install -y -qq ffmpeg", timeout=300)
            if s:
                s.emit("vs_install", {"stage": "done", "percent": 100, "message": "Gotowe!"})
        except Exception as e:
            log.error("VS install failed: %s", e)
            if s:
                s.emit("vs_install", {"stage": "error", "percent": 0, "message": str(e)})
    gevent.spawn(_do)
    return jsonify({"ok": True, "message": "Instalacja w tle..."})


@video_station_bp.route("/uninstall", methods=["POST"])
@admin_required
def uninstall_deps():
    _scan_state["stop_requested"] = True
    wipe = (request.json or {}).get("wipe_data", False)
    if wipe:
        for p in [_DB_PATH, _THUMB_DIR, data_path("video_folders.json")]:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                elif os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
    return jsonify({"ok": True})


@video_station_bp.route("/folders", methods=["GET"])
@require_auth
def get_folders():
    return jsonify({"folders": _load_folders()})


@video_station_bp.route("/folders", methods=["POST"])
@require_auth
def save_folders():
    folders = (request.json or {}).get("folders", [])
    valid = []
    for f in folders:
        if not isinstance(f, str) or not f.startswith("/"):
            continue
        rp = os.path.realpath(f)
        if os.path.isdir(rp):
            valid.append(rp)
    _save_folders(valid)
    return jsonify({"ok": True, "folders": valid})


@video_station_bp.route("/scan", methods=["POST"])
@require_auth
def start_scan():
    if _scan_state["running"]:
        return jsonify({"error": "Skan juz trwa."}), 409
    if not _all_deps_ok():
        return jsonify({"error": "Brak ffmpeg/ffprobe."}), 400
    folders = _load_folders()
    if not folders:
        return jsonify({"error": "Brak skonfigurowanych folderow."}), 400
    _scan_state.update(running=True, stop_requested=False, total=0, processed=0, current_file="")
    use_tmdb = (request.json or {}).get("use_tmdb", False)
    import gevent
    gevent.spawn(_scan_worker, folders, use_tmdb)
    return jsonify({"ok": True})


@video_station_bp.route("/scan-stop", methods=["POST"])
@require_auth
def stop_scan():
    _scan_state["stop_requested"] = True
    return jsonify({"ok": True})


@video_station_bp.route("/scan-status", methods=["GET"])
@require_auth
def scan_status():
    return jsonify({
        "running": _scan_state["running"],
        "total": _scan_state["total"],
        "processed": _scan_state["processed"],
        "current_file": os.path.basename(_scan_state["current_file"]),
    })


@video_station_bp.route("/library", methods=["GET"])
@require_auth
def library():
    conn = _get_db()
    offset = int(request.args.get("offset", 0))
    limit = min(int(request.args.get("limit", 60)), 200)
    sort = request.args.get("sort", "added_desc")
    q_search = request.args.get("q", "").strip()
    folder_filter = request.args.get("folder", "")
    order_map = {
        "added_desc": "added_at DESC", "added_asc": "added_at ASC",
        "name_asc": "title ASC", "name_desc": "title DESC",
        "duration_desc": "duration DESC", "duration_asc": "duration ASC",
        "size_desc": "file_size DESC", "size_asc": "file_size ASC",
    }
    order = order_map.get(sort, "added_at DESC")
    where = []
    params = []
    if q_search:
        where.append("(title LIKE ? OR filename LIKE ?)")
        params += ["%" + q_search + "%", "%" + q_search + "%"]
    if folder_filter:
        where.append("folder=?")
        params.append(folder_filter)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) FROM videos " + where_sql, params).fetchone()[0]
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        + where_sql + " ORDER BY " + order + " LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "title": r["title"], "filename": r["filename"],
            "path": r["path"], "folder": r["folder"],
            "duration": r["duration"], "duration_fmt": _format_duration(r["duration"]),
            "width": r["width"], "height": r["height"],
            "codec": r["codec"], "file_size": r["file_size"],
            "thumb_ok": bool(r["thumb_ok"]),
            "poster_ok": bool(r["poster_ok"]),
            "tmdb_id": r["tmdb_id"] or 0,
            "tmdb_title": r["tmdb_title"] or "",
            "tmdb_year": r["tmdb_year"] or "",
            "tmdb_rating": r["tmdb_rating"] or 0,
            "tmdb_overview": r["tmdb_overview"] or "",
            "watched": bool(r["watched"]), "position": r["position"] or 0,
            "added_at": r["added_at"],
        })
    conn.close()
    return jsonify({"items": items, "total": total, "offset": offset, "limit": limit})


@video_station_bp.route("/recent", methods=["GET"])
@require_auth
def recent():
    conn = _get_db()
    limit = min(int(request.args.get("limit", 20)), 60)
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        "ORDER BY v.added_at DESC LIMIT ?", (limit,)).fetchall()
    items = [{
        "id": r["id"], "title": r["title"], "filename": r["filename"],
        "path": r["path"], "duration": r["duration"],
        "duration_fmt": _format_duration(r["duration"]),
        "width": r["width"], "height": r["height"],
        "thumb_ok": bool(r["thumb_ok"]),
        "poster_ok": bool(r["poster_ok"]),
        "tmdb_id": r["tmdb_id"] or 0,
        "tmdb_title": r["tmdb_title"] or "",
        "tmdb_year": r["tmdb_year"] or "",
        "tmdb_rating": r["tmdb_rating"] or 0,
        "watched": bool(r["watched"]), "position": r["position"] or 0,
    } for r in rows]
    conn.close()
    return jsonify({"items": items})


@video_station_bp.route("/collections", methods=["GET"])
@require_auth
def collections():
    conn = _get_db()
    rows = conn.execute(
        "SELECT folder, COUNT(*) as cnt, SUM(duration) as total_dur "
        "FROM videos GROUP BY folder ORDER BY cnt DESC LIMIT 50").fetchall()
    colls = []
    for r in rows:
        folder = r["folder"]
        name = os.path.basename(folder) or folder
        cover = conn.execute("SELECT id FROM videos WHERE folder=? AND thumb_ok=1 LIMIT 1", (folder,)).fetchone()
        colls.append({
            "folder": folder, "name": name, "count": r["cnt"],
            "total_duration": _format_duration(r["total_dur"] or 0),
            "cover_id": cover["id"] if cover else None,
        })
    conn.close()
    return jsonify({"collections": colls})


@video_station_bp.route("/info/<int:vid>", methods=["GET"])
@require_auth
def video_info(vid):
    conn = _get_db()
    r = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id WHERE v.id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    meta = {}
    try:
        meta = json.loads(r["metadata_json"] or "{}")
    except Exception:
        pass
    return jsonify({
        "id": r["id"], "title": r["title"], "filename": r["filename"],
        "path": r["path"], "folder": r["folder"],
        "duration": r["duration"], "duration_fmt": _format_duration(r["duration"]),
        "width": r["width"], "height": r["height"],
        "codec": r["codec"], "audio_codec": r["audio_codec"],
        "bitrate": r["bitrate"], "file_size": r["file_size"],
        "thumb_ok": bool(r["thumb_ok"]),
        "poster_ok": bool(r["poster_ok"]),
        "tmdb_id": r["tmdb_id"] or 0,
        "tmdb_title": r["tmdb_title"] or "",
        "tmdb_overview": r["tmdb_overview"] or "",
        "tmdb_year": r["tmdb_year"] or "",
        "tmdb_rating": r["tmdb_rating"] or 0,
        "tmdb_genres": r["tmdb_genres"] or "",
        "watched": bool(r["watched"]), "position": r["position"] or 0,
        "added_at": r["added_at"], "metadata": meta,
    })


@video_station_bp.route("/thumb/<int:vid>", methods=["GET"])
@require_auth
def thumb(vid):
    p = os.path.join(_THUMB_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak miniatury."}), 404


@video_station_bp.route("/poster/<int:vid>", methods=["GET"])
@require_auth
def poster(vid):
    p = os.path.join(_POSTER_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak plakatu."}), 404


@video_station_bp.route("/stream/<int:vid>", methods=["GET"])
@require_auth
def stream(vid):
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify({"error": "Plik nie istnieje."}), 404
    ext = os.path.splitext(fp)[1].lower()
    mime_map = {
        ".mp4": "video/mp4", ".webm": "video/webm",
        ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".mov": "video/quicktime", ".m4v": "video/mp4",
        ".ogv": "video/ogg", ".ts": "video/mp2t",
    }
    mime = mime_map.get(ext, "video/mp4")
    fsize = os.path.getsize(fp)
    range_header = request.headers.get("Range")
    if range_header:
        byte_start = 0
        byte_end = fsize - 1
        match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            byte_start = int(match.group(1))
            if match.group(2):
                byte_end = int(match.group(2))
        length = byte_end - byte_start + 1

        def gen():
            with open(fp, "rb") as f:
                f.seek(byte_start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        resp = Response(gen(), 206, mimetype=mime)
        resp.headers["Content-Range"] = "bytes %d-%d/%d" % (byte_start, byte_end, fsize)
        resp.headers["Content-Length"] = str(length)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp
    return send_file(fp, mimetype=mime)


@video_station_bp.route("/watched/<int:vid>", methods=["POST"])
@require_auth
def update_watched(vid):
    d = request.json or {}
    conn = _get_db()
    r = conn.execute("SELECT id FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    watched = 1 if d.get("watched", False) else 0
    position = float(d.get("position", 0))
    conn.execute(
        "INSERT INTO watch_state (video_id,watched,position,updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET "
        "watched=excluded.watched,position=excluded.position,updated_at=excluded.updated_at",
        (vid, watched, position, time.time()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --- TMDb config & matching routes ---

@video_station_bp.route("/tmdb-config", methods=["GET"])
@require_auth
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
@require_auth
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
@require_auth
def tmdb_match_all():
    api_key = _load_tmdb_key()
    if not api_key:
        return jsonify({"error": "Brak klucza TMDb."}), 400
    if _scan_state["running"]:
        return jsonify({"error": "Skan juz trwa, poczekaj az sie skonczy."}), 409
    import gevent

    def _do_match():
        _scan_state.update(running=True, stop_requested=False,
                           total=0, processed=0, current_file='')
        conn = _get_db()
        unmatched = conn.execute(
            "SELECT id, filename FROM videos WHERE tmdb_id=0 OR tmdb_id IS NULL"
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
