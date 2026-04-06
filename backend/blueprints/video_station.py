"""
Video Station -- video library manager with streaming.

Routes:
  GET  /api/video-station/pkg-status      - dependency & library status
  POST /api/video-station/install         - install ffmpeg
  POST /api/video-station/uninstall       - cleanup
  GET  /api/video-station/library         - list videos (?watched=0|1 filter)
  GET  /api/video-station/continue-watching - in-progress videos (position>0, not watched)
  GET  /api/video-station/folders         - configured library folders
  POST /api/video-station/folders         - save library folders
  POST /api/video-station/scan            - start background library scan
  GET  /api/video-station/scan-status     - scan progress
  POST /api/video-station/scan-stop       - stop running scan
  GET  /api/video-station/info/<int:vid>  - detailed video metadata (with TMDb credits)
  GET  /api/video-station/stream/<int:vid>- stream video file (raw)
  GET  /api/video-station/transcode/<int:vid> - transcode video (?audio=N, ?start=S)
  POST /api/video-station/hls/<int:vid>/start - start HLS transcoding session
  GET  /api/video-station/hls/<sid>/playlist.m3u8 - HLS playlist
  GET  /api/video-station/hls/<sid>/<segment>  - HLS segment (.ts)
  POST /api/video-station/hls/<sid>/stop       - stop HLS session
  GET  /api/video-station/thumb/<int:vid> - video thumbnail
  GET  /api/video-station/poster/<int:vid>- TMDb poster image
  GET  /api/video-station/backdrop/<int:vid> - TMDb backdrop image
  GET  /api/video-station/thumbstrip/<int:vid> - seekbar thumbnail sprite (VTT+image)
  GET  /api/video-station/recent          - recently added videos
  GET  /api/video-station/collections     - auto-generated collections
  POST /api/video-station/watched/<int:vid> - mark as watched / update position
  POST /api/video-station/rescan-metadata - re-probe videos with empty codec info
  POST /api/video-station/remove/<int:vid> - remove video from library (keeps file)
  POST /api/video-station/batch           - batch operations (watched/unwatched/remove/hide/unhide)
  GET  /api/video-station/hide-status     - hide password status & session unlock state
  POST /api/video-station/hide-password   - set/change hide password
  DELETE /api/video-station/hide-password - remove hide password & unhide all
  POST /api/video-station/hide-unlock     - unlock hidden videos for session
  POST /api/video-station/hide-lock       - re-lock hidden videos for session
  GET  /api/video-station/subtitles/<int:vid> - find subtitle files next to video
  GET  /api/video-station/subtitle-file/<int:vid>/<filename> - serve subtitle (srt→vtt)
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
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import urllib.error

from flask import Blueprint, jsonify, request, Response, send_file

from host import host_run, q, data_path, app_path
from crypto_utils import hash_folder_password as _hash_pw, verify_folder_password as _verify_pw

from blueprints.admin_required import admin_required

log = logging.getLogger('ethos.video_station')

video_station_bp = Blueprint('video-station', __name__, url_prefix='/api/video-station')

_DB_PATH = data_path('video_station.db')
_THUMB_DIR = data_path('video_thumbs')
_POSTER_DIR = data_path('video_posters')
_BACKDROP_DIR = data_path('video_backdrops')
_THUMBSTRIP_DIR = data_path('video_thumbstrips')
_TMDB_CONF = data_path('video_tmdb.json')
_HIDE_PW_FILE = data_path('vs_hide_password.json')

# ── hide-password session state ────────────────────────────────
_hide_unlocked = {}          # token → True
_hide_lock = threading.Lock()
_hide_attempts = {}          # token → {'count': int, 'first': float, 'locked_until': float}
_HIDE_MAX_ATTEMPTS = 5
_HIDE_ATTEMPT_WINDOW = 120   # seconds
_HIDE_LOCKOUT_TIME = 300     # seconds

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
              '.mpg', '.mpeg', '.ts', '.3gp', '.ogv', '.vob'}

# Audio codecs that browsers can natively decode inside <video>
_BROWSER_AUDIO_CODECS = {'aac', 'mp3', 'opus', 'vorbis', 'flac'}

# Containers that browsers can play natively in <video>
_BROWSER_CONTAINERS = {'.mp4', '.webm', '.m4v', '.ogg', '.ogv', '.mov'}

_scan_state = {
    'running': False, 'stop_requested': False,
    'total': 0, 'processed': 0, 'current_file': '',
}

# ── HLS transcoding sessions ──────────────────────────────────
_hls_sessions = {}   # session_id → {proc, tmpdir, vid, created}
_HLS_MAX_AGE = 2 * 3600   # auto-cleanup after 2 hours
_HLS_ORPHAN_PREFIX = "vs_hls_"


def _cleanup_hls(session_id):
    """Stop ffmpeg and remove temp dir for an HLS session."""
    sess = _hls_sessions.pop(session_id, None)
    if not sess:
        return
    proc = sess.get('proc')
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except Exception:
            pass
    tmpdir = sess.get('tmpdir')
    if tmpdir and os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)


def _cleanup_stale_hls():
    """Remove HLS sessions older than _HLS_MAX_AGE and orphaned temp dirs."""
    now = time.time()
    for sid in list(_hls_sessions):
        if now - _hls_sessions[sid].get('created', 0) > _HLS_MAX_AGE:
            _cleanup_hls(sid)
    # Scan /tmp for orphaned vs_hls_* dirs not tracked in _hls_sessions
    _cleanup_orphaned_hls_dirs()


def _cleanup_orphaned_hls_dirs():
    """Remove vs_hls_* temp dirs in /tmp that aren't tracked by any session."""
    tracked_dirs = {s.get('tmpdir') for s in _hls_sessions.values()}
    try:
        for name in os.listdir(tempfile.gettempdir()):
            if not name.startswith(_HLS_ORPHAN_PREFIX):
                continue
            dirpath = os.path.join(tempfile.gettempdir(), name)
            if not os.path.isdir(dirpath):
                continue
            if dirpath in tracked_dirs:
                continue
            log.info("HLS cleanup: removing orphaned dir %s", dirpath)
            shutil.rmtree(dirpath, ignore_errors=True)
    except OSError:
        pass


# Clean up orphaned HLS dirs left from before server restart
_cleanup_orphaned_hls_dirs()


# --- Database ---

def _get_db():
    conn = sqlite3.connect(_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
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
        CREATE INDEX IF NOT EXISTS idx_videos_added_at ON videos(added_at);
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
            ('tmdb_backdrop_path', 'TEXT'),
            ('backdrop_ok', 'INTEGER DEFAULT 0'),
            ('tmdb_cast', 'TEXT'),
            ('tmdb_director', 'TEXT'),
            ('tmdb_media_type', 'TEXT'),
            ('hidden', 'INTEGER DEFAULT 0'),
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


def _get_token():
    """Return current auth token from request."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    qt = request.args.get('token', '')
    if qt:
        return qt
    return request.cookies.get('nas_token', '')


# ── hide-password helpers ──────────────────────────────────────
def _load_hide_pw():
    try:
        with open(_HIDE_PW_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_hide_pw(data):
    with open(_HIDE_PW_FILE, 'w') as f:
        json.dump(data, f)


def _hide_pw_is_set():
    return bool(_load_hide_pw().get('hash'))


def _is_hidden_unlocked():
    """Check if hidden videos are unlocked for the current session token."""
    token = _get_token()
    with _hide_lock:
        return _hide_unlocked.get(token, False)


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
            with open(p) as f:
                return json.loads(f.read())
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
        result = host_run(cmd, timeout=30)
        if result.returncode != 0:
            log.debug('ffprobe non-zero exit for %s: %s', path, result.stderr)
            return {}
        data = json.loads(result.stdout)
        fmt = data.get('format', {})
        vstream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
        astreams = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
        first_audio = astreams[0] if astreams else {}
        audio_tracks = []
        for i, a in enumerate(astreams):
            tags = a.get('tags', {})
            audio_tracks.append({
                'index': a.get('index', i),
                'codec': a.get('codec_name', ''),
                'channels': a.get('channels', 0),
                'language': tags.get('language', ''),
                'title': tags.get('title', ''),
            })
        return {
            'duration': float(fmt.get('duration', 0)),
            'width': int(vstream.get('width', 0)),
            'height': int(vstream.get('height', 0)),
            'codec': vstream.get('codec_name', ''),
            'audio_codec': first_audio.get('codec_name', ''),
            'bitrate': int(fmt.get('bit_rate', 0)),
            'title': fmt.get('tags', {}).get('title', ''),
            'audio_tracks': audio_tracks,
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
        result = host_run(cmd, timeout=30)
        return result.returncode == 0 and os.path.isfile(thumb_path)
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
_TMDB_BACKDROP_BASE = 'https://image.tmdb.org/t/p/w1280'

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
            with open(_TMDB_CONF) as f:
                return json.loads(f.read()).get('api_key', '')
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
                    'tmdb_backdrop_path': r.get('backdrop_path', ''),
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


def _download_backdrop(backdrop_path, video_id):
    """Download TMDb backdrop and save locally."""
    if not backdrop_path:
        return False
    os.makedirs(_BACKDROP_DIR, exist_ok=True)
    local = os.path.join(_BACKDROP_DIR, str(video_id) + '.jpg')
    try:
        url = _TMDB_BACKDROP_BASE + backdrop_path
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            with open(local, 'wb') as f:
                f.write(resp.read())
        return os.path.isfile(local)
    except Exception as e:
        log.debug('Backdrop download failed for vid %s: %s', video_id, e)
        return False


def _fetch_tmdb_credits(tmdb_id, media_type, api_key):
    """Fetch cast + director from TMDb credits API."""
    if not tmdb_id or not api_key:
        return '', ''
    try:
        url = '%s/%s/%d/credits?api_key=%s' % (
            _TMDB_BASE, media_type, tmdb_id, urllib.parse.quote(api_key))
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        cast_names = [c['name'] for c in (data.get('cast') or [])[:10]]
        directors = [c['name'] for c in (data.get('crew') or [])
                     if c.get('job') == 'Director']
        return ', '.join(cast_names), ', '.join(directors)
    except Exception as e:
        log.debug('TMDb credits fetch failed for %s/%s: %s', media_type, tmdb_id, e)
        return '', ''


def _tmdb_match_video(conn, video_id, filename, api_key=''):
    """Parse filename, search TMDb, update DB, download poster & backdrop, fetch credits."""
    title, year = _parse_filename(filename)
    if not title:
        return None
    result = _tmdb_search(title, year, api_key)
    if not result:
        return None

    # Fetch credits (cast + director)
    media_type = result.get('media_type', 'movie')
    cast, director = _fetch_tmdb_credits(result['tmdb_id'], media_type, api_key)

    conn.execute(
        'UPDATE videos SET tmdb_id=?, tmdb_title=?, tmdb_overview=?, '
        'tmdb_year=?, tmdb_rating=?, tmdb_genres=?, tmdb_poster_path=?, '
        'tmdb_backdrop_path=?, tmdb_cast=?, tmdb_director=?, tmdb_media_type=? WHERE id=?',
        (result['tmdb_id'], result['tmdb_title'], result['tmdb_overview'],
         result['tmdb_year'], result['tmdb_rating'], result['tmdb_genres'],
         result['tmdb_poster_path'], result.get('tmdb_backdrop_path', ''),
         cast, director, media_type, video_id))
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
    # Download backdrop
    if result.get('tmdb_backdrop_path'):
        ok = _download_backdrop(result['tmdb_backdrop_path'], video_id)
        if ok:
            conn.execute('UPDATE videos SET backdrop_ok=1 WHERE id=?', (video_id,))
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

def pkg_status():
    deps = _check_deps()
    stats = {}
    if os.path.isfile(_DB_PATH):
        c = None
        try:
            c = _get_db()
            stats = {
                "videos": c.execute("SELECT COUNT(*) FROM videos WHERE COALESCE(hidden,0)=0").fetchone()[0],
                "total_videos": c.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
                "total_size": c.execute("SELECT COALESCE(SUM(file_size),0) FROM videos").fetchone()[0],
                "watched": c.execute("SELECT COUNT(*) FROM watch_state WHERE watched=1").fetchone()[0],
                "hidden": c.execute("SELECT COUNT(*) FROM videos WHERE COALESCE(hidden,0)=1").fetchone()[0],
            }
        except Exception:
            pass
        finally:
            if c:
                c.close()
    return jsonify({
        "installed": deps["ffmpeg"] and deps["ffprobe"], "deps": deps,
        "stats": stats, "scanning": _scan_state["running"],
        "hide_password_set": _hide_pw_is_set(),
        "hide_unlocked": _is_hidden_unlocked(),
    })


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

def get_folders():
    return jsonify({"folders": _load_folders()})


@video_station_bp.route("/folders", methods=["POST"])

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

def stop_scan():
    _scan_state["stop_requested"] = True
    return jsonify({"ok": True})


@video_station_bp.route("/scan-status", methods=["GET"])

def scan_status():
    return jsonify({
        "running": _scan_state["running"],
        "total": _scan_state["total"],
        "processed": _scan_state["processed"],
        "current_file": os.path.basename(_scan_state["current_file"]),
    })


@video_station_bp.route("/continue-watching", methods=["GET"])

def continue_watching():
    """Return videos with saved position > 0 that are not yet marked as watched."""
    conn = _get_db()
    limit = min(int(request.args.get("limit", 20)), 60)
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE ws.position > 0 AND (ws.watched=0 OR ws.watched IS NULL) "
        "AND COALESCE(v.hidden,0)=0 "
        "ORDER BY ws.updated_at DESC LIMIT ?", (limit,)).fetchall()
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
        "watched": False, "position": r["position"] or 0,
    } for r in rows]
    conn.close()
    return jsonify({"items": items})


@video_station_bp.route("/library", methods=["GET"])

def library():
    conn = _get_db()
    offset = int(request.args.get("offset", 0))
    limit = min(int(request.args.get("limit", 60)), 200)
    sort = request.args.get("sort", "added_desc")
    q_search = request.args.get("q", "").strip()
    folder_filter = request.args.get("folder", "")
    watched_filter = request.args.get("watched", "")
    show_hidden = request.args.get("show_hidden", "") == "1"
    order_map = {
        "added_desc": "added_at DESC", "added_asc": "added_at ASC",
        "name_asc": "title ASC", "name_desc": "title DESC",
        "duration_desc": "duration DESC", "duration_asc": "duration ASC",
        "size_desc": "file_size DESC", "size_asc": "file_size ASC",
    }
    order = order_map.get(sort, "added_at DESC")
    where = []
    params = []
    if show_hidden and _is_hidden_unlocked():
        where.append("COALESCE(v.hidden,0)=1")
    else:
        where.append("COALESCE(v.hidden,0)=0")
    if q_search:
        where.append("(title LIKE ? OR filename LIKE ?)")
        params += ["%" + q_search + "%", "%" + q_search + "%"]
    if folder_filter:
        where.append("folder=?")
        params.append(folder_filter)
    if watched_filter == '1':
        where.append("COALESCE(ws.watched, 0)=1")
    elif watched_filter == '0':
        where.append("COALESCE(ws.watched, 0)=0")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM videos v LEFT JOIN watch_state ws ON ws.video_id=v.id "
        + where_sql, params).fetchone()[0]
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
            "hidden": bool(r["hidden"]),
        })
    conn.close()
    return jsonify({"items": items, "total": total, "offset": offset, "limit": limit})


@video_station_bp.route("/recent", methods=["GET"])

def recent():
    conn = _get_db()
    limit = min(int(request.args.get("limit", 20)), 60)
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE COALESCE(v.hidden,0)=0 "
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

def collections():
    conn = _get_db()
    rows = conn.execute(
        "SELECT folder, COUNT(*) as cnt, SUM(duration) as total_dur, "
        "(SELECT id FROM videos v2 WHERE v2.folder=v.folder AND v2.thumb_ok=1 AND COALESCE(v2.hidden,0)=0 LIMIT 1) as cover_id "
        "FROM videos v WHERE COALESCE(v.hidden,0)=0 GROUP BY folder ORDER BY cnt DESC LIMIT 50").fetchall()
    colls = []
    for r in rows:
        folder = r["folder"]
        name = os.path.basename(folder) or folder
        colls.append({
            "folder": folder, "name": name, "count": r["cnt"],
            "total_duration": _format_duration(r["total_dur"] or 0),
            "cover_id": r["cover_id"],
        })
    conn.close()
    return jsonify({"collections": colls})


@video_station_bp.route("/info/<int:vid>", methods=["GET"])

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
    audio_codec = (r["audio_codec"] or "").lower()
    ext = os.path.splitext(r["path"])[1].lower()
    needs_tc = (bool(audio_codec) and audio_codec not in _BROWSER_AUDIO_CODECS) or \
               (ext not in _BROWSER_CONTAINERS)
    audio_tracks = meta.get("audio_tracks", [])

    # Genre ID → name mapping (TMDb standard)
    _GENRE_MAP = {
        28: 'Akcja', 12: 'Przygodowy', 16: 'Animacja', 35: 'Komedia', 80: 'Kryminał',
        99: 'Dokumentalny', 18: 'Dramat', 10751: 'Familijny', 14: 'Fantasy',
        36: 'Historyczny', 27: 'Horror', 10402: 'Muzyczny', 9648: 'Tajemnica',
        10749: 'Romans', 878: 'Sci-Fi', 10770: 'Film TV', 53: 'Thriller',
        10752: 'Wojenny', 37: 'Western',
        10759: 'Akcja i Przygoda', 10762: 'Dla dzieci', 10763: 'Informacyjny',
        10764: 'Reality', 10765: 'Sci-Fi & Fantasy', 10766: 'Telenowela',
        10767: 'Talk-show', 10768: 'Wojenny i Polityczny',
    }
    genre_ids = (r["tmdb_genres"] or "").split(",")
    genre_names = [_GENRE_MAP.get(int(g.strip()), '') for g in genre_ids if g.strip().isdigit()]
    genre_names = [g for g in genre_names if g]

    return jsonify({
        "id": r["id"], "title": r["title"], "filename": r["filename"],
        "path": r["path"], "folder": r["folder"],
        "duration": r["duration"], "duration_fmt": _format_duration(r["duration"]),
        "width": r["width"], "height": r["height"],
        "codec": r["codec"], "audio_codec": r["audio_codec"],
        "bitrate": r["bitrate"], "file_size": r["file_size"],
        "thumb_ok": bool(r["thumb_ok"]),
        "poster_ok": bool(r["poster_ok"]),
        "backdrop_ok": bool(r["backdrop_ok"]) if "backdrop_ok" in r.keys() else False,
        "tmdb_id": r["tmdb_id"] or 0,
        "tmdb_title": r["tmdb_title"] or "",
        "tmdb_overview": r["tmdb_overview"] or "",
        "tmdb_year": r["tmdb_year"] or "",
        "tmdb_rating": r["tmdb_rating"] or 0,
        "tmdb_genres": r["tmdb_genres"] or "",
        "genre_names": genre_names,
        "tmdb_cast": r["tmdb_cast"] or "" if "tmdb_cast" in r.keys() else "",
        "tmdb_director": r["tmdb_director"] or "" if "tmdb_director" in r.keys() else "",
        "watched": bool(r["watched"]), "position": r["position"] or 0,
        "added_at": r["added_at"], "metadata": meta,
        "needs_transcode": needs_tc,
        "audio_tracks": audio_tracks,
    })


@video_station_bp.route("/thumb/<int:vid>", methods=["GET"])

def thumb(vid):
    p = os.path.join(_THUMB_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak miniatury."}), 404


@video_station_bp.route("/poster/<int:vid>", methods=["GET"])

def poster(vid):
    p = os.path.join(_POSTER_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak plakatu."}), 404


@video_station_bp.route("/backdrop/<int:vid>", methods=["GET"])

def backdrop(vid):
    p = os.path.join(_BACKDROP_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak tła."}), 404


_thumbstrip_generating = set()  # video IDs currently being generated


@video_station_bp.route("/thumbstrip/<int:vid>", methods=["GET"])

def thumbstrip(vid):
    """Serve seekbar thumbnail sprite image.

    Returns the sprite image (JPEG) if cached. If not cached, kicks off
    background generation and returns 202 — client should retry later.
    Sprite: 160x90 thumbnails every 30s, tiled 10 columns.
    Uses fast keyframe-seek per thumbnail instead of decoding all frames.
    """
    conn = _get_db()
    r = conn.execute("SELECT path, duration FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404

    sprite_path = os.path.join(_THUMBSTRIP_DIR, str(vid) + ".jpg")

    # Serve cached sprite
    if os.path.isfile(sprite_path):
        resp = send_file(sprite_path, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=604800"
        return resp

    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify({"error": "Plik nie istnieje."}), 404

    duration = r["duration"] or 0
    if duration < 30:
        return jsonify({"error": "Film za krótki."}), 400

    if vid in _thumbstrip_generating:
        return jsonify({"status": "generating"}), 202

    # Start background generation
    _thumbstrip_generating.add(vid)
    import gevent
    gevent.spawn(_generate_thumbstrip, vid, fp, duration, sprite_path)
    return jsonify({"status": "generating"}), 202


def _generate_thumbstrip(vid, fp, duration, sprite_path):
    """Generate thumbnail sprite using fast keyframe seeks (background task)."""
    tmpdir = None
    try:
        from PIL import Image
        os.makedirs(_THUMBSTRIP_DIR, exist_ok=True)
        interval = 30  # one thumb every 30 seconds
        tmpdir = tempfile.mkdtemp(prefix="vs_ts_")
        positions = list(range(0, int(duration), interval))
        if not positions:
            return

        # Extract individual thumbnails using fast seek (-ss before -i)
        thumb_w, thumb_h = 160, 90
        cols = 10
        frame_paths = []
        for i, pos in enumerate(positions):
            frame_path = os.path.join(tmpdir, "f%04d.jpg" % i)
            cmd = 'ffmpeg -y -ss %d -i %s -vframes 1 -vf scale=%d:%d -q:v 6 %s' % (
                pos, q(fp), thumb_w, thumb_h, q(frame_path))
            result = host_run(cmd, timeout=30)
            if result.returncode == 0 and os.path.isfile(frame_path):
                frame_paths.append(frame_path)
            else:
                frame_paths.append(None)  # placeholder

        valid = [p for p in frame_paths if p]
        if not valid:
            log.warning('Thumbstrip: no frames extracted for vid %s', vid)
            return

        # Tile into sprite using PIL (simple and reliable)
        rows = (len(frame_paths) + cols - 1) // cols
        sprite = Image.new('RGB', (cols * thumb_w, rows * thumb_h), (0, 0, 0))
        for i, fpath in enumerate(frame_paths):
            if fpath and os.path.isfile(fpath):
                try:
                    img = Image.open(fpath)
                    sprite.paste(img, ((i % cols) * thumb_w, (i // cols) * thumb_h))
                    img.close()
                except Exception:
                    pass  # black placeholder for failed frames
        sprite.save(sprite_path, 'JPEG', quality=70)
        log.info('Thumbstrip generated for vid %s: %d frames, %s',
                 vid, len(valid), sprite_path)
    except Exception as e:
        log.warning('Thumbstrip generation error for vid %s: %s', vid, e)
    finally:
        _thumbstrip_generating.discard(vid)
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


@video_station_bp.route("/stream/<int:vid>", methods=["GET"])

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


@video_station_bp.route("/transcode/<int:vid>", methods=["GET"])

def transcode(vid):
    """Stream video with audio re-encoded to AAC for browser compatibility.

    Uses ffmpeg to copy the video stream (when h264) and transcode audio
    to AAC, outputting fragmented MP4 suitable for progressive HTTP streaming.

    Query params:
        start  - seek to position in seconds before encoding
        audio  - ffmpeg stream index for audio track (default: first audio)
    """
    conn = _get_db()
    r = conn.execute("SELECT path, codec, audio_codec, metadata_json FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify({"error": "Plik nie istnieje."}), 404
    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg nie jest zainstalowany."}), 500

    vcodec = (r["codec"] or "").lower()
    vcopy = vcodec in ("h264", "vp8", "vp9")
    v_arg = "copy" if vcopy else "libx264"

    start_sec = max(0.0, request.args.get("start", 0, type=float))
    audio_idx = request.args.get("audio", None, type=int)

    # Validate audio track index
    if audio_idx is not None:
        try:
            tracks = json.loads(r["metadata_json"] or "{}").get("audio_tracks", [])
        except Exception:
            tracks = []
        valid_indices = {t.get("index") for t in tracks} if tracks else set()
        if valid_indices and audio_idx not in valid_indices:
            return jsonify({"error": "Nieprawidłowy indeks ścieżki audio."}), 400

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start_sec > 0:
        cmd += ["-ss", str(start_sec)]
    cmd += ["-i", fp]
    # Explicitly map first video + chosen audio to avoid subtitle stream issues
    cmd += ["-map", "0:v:0"]
    if audio_idx is not None:
        cmd += ["-map", "0:%d" % audio_idx]
    else:
        cmd += ["-map", "0:a:0"]
    cmd += [
        "-c:v", v_arg,
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "-"
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        except (OSError, GeneratorExit):
            pass
        finally:
            proc.stdout.close()
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except Exception:
                pass

    return Response(generate(), mimetype="video/mp4",
                    headers={
                        "Cache-Control": "no-cache",
                        "Accept-Ranges": "none",
                        "X-Content-Type-Options": "nosniff",
                    })


# ── HLS endpoints ─────────────────────────────────────────────

@video_station_bp.route("/hls/<int:vid>/start", methods=["POST"])
def hls_start(vid):
    """Start an HLS transcoding session.

    POST body (JSON): {start: seconds, audio: track_index}
    Returns: {ok, session_id}
    """
    _cleanup_stale_hls()

    conn = _get_db()
    r = conn.execute(
        "SELECT path, codec, audio_codec, duration, metadata_json FROM videos WHERE id=?",
        (vid,),
    ).fetchone()
    conn.close()
    if not r:
        return jsonify(error="Nie znaleziono."), 404
    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify(error="Plik nie istnieje."), 404
    if not shutil.which("ffmpeg"):
        return jsonify(error="ffmpeg nie jest zainstalowany."), 500

    data = request.get_json(silent=True) or {}
    start_sec = max(0.0, float(data.get("start", 0)))
    audio_idx = data.get("audio", None)

    # Stop any existing HLS session for this video
    for sid in list(_hls_sessions):
        if _hls_sessions[sid].get("vid") == vid:
            _cleanup_hls(sid)

    vcodec = (r["codec"] or "").lower()
    vcopy = vcodec in ("h264", "vp8", "vp9")
    v_arg = "copy" if vcopy else "libx264 -preset ultrafast -crf 23"

    session_id = "%d_%s" % (vid, os.urandom(4).hex())
    tmpdir = tempfile.mkdtemp(prefix="vs_hls_")

    cmd = "ffmpeg -hide_banner -loglevel error"
    if start_sec > 0:
        cmd += " -ss %s" % start_sec
    cmd += " -i %s" % q(fp)
    cmd += " -map 0:v:0"
    if audio_idx is not None:
        cmd += " -map 0:%d" % int(audio_idx)
    else:
        cmd += " -map 0:a:0"
    cmd += " -c:v %s -c:a aac -b:a 192k -ac 2" % v_arg
    cmd += " -f hls -hls_time 4 -hls_list_size 40"
    cmd += " -hls_segment_type mpegts"
    cmd += " -hls_segment_filename %s" % q(os.path.join(tmpdir, "seg%05d.ts"))
    cmd += " -hls_flags delete_segments+append_list"
    cmd += " -y %s" % q(os.path.join(tmpdir, "playlist.m3u8"))

    log.info("HLS start vid=%d ss=%.1f cmd=%s", vid, start_sec, cmd[:200])

    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    _hls_sessions[session_id] = {
        "proc": proc,
        "tmpdir": tmpdir,
        "vid": vid,
        "start_offset": start_sec,
        "duration": r["duration"] or 0,
        "created": time.time(),
    }

    # Wait for first segment (up to 10s)
    playlist_path = os.path.join(tmpdir, "playlist.m3u8")
    for _ in range(100):
        if os.path.exists(playlist_path) and os.path.getsize(playlist_path) > 20:
            break
        # Check if ffmpeg crashed
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace")[:500] if proc.stderr else ""
            _cleanup_hls(session_id)
            log.error("HLS ffmpeg crashed: %s", stderr)
            return jsonify(error="Transkodowanie nie powiodło się: " + stderr), 500
        time.sleep(0.1)

    return jsonify(ok=True, session_id=session_id, start_offset=start_sec)


@video_station_bp.route("/hls/<session_id>/playlist.m3u8")
def hls_playlist(session_id):
    """Serve the live HLS playlist written by ffmpeg.

    Relays ffmpeg's sliding-window playlist directly.
    When ffmpeg finishes, appends #EXT-X-ENDLIST so hls.js
    knows the stream ended.
    """
    sess = _hls_sessions.get(session_id)
    if not sess:
        return "", 404
    path = os.path.join(sess["tmpdir"], "playlist.m3u8")
    if not os.path.exists(path):
        return "", 404

    with open(path, "r") as f:
        raw = f.read()

    proc = sess.get("proc")
    finished = proc is None or proc.poll() is not None

    if finished and "#EXT-X-ENDLIST" not in raw:
        raw = raw.rstrip() + "\n#EXT-X-ENDLIST\n"

    resp = Response(raw, mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


@video_station_bp.route("/hls/<session_id>/<filename>")
def hls_segment(session_id, filename):
    """Serve an HLS segment (.ts file).

    With sliding-window playlist, segments are deleted by ffmpeg once they
    fall outside the window.  Only a short wait for the next segment being
    produced right now; old/deleted segments return 404 immediately.
    """
    sess = _hls_sessions.get(session_id)
    if not sess:
        return "", 404

    # Security: only .ts files, no path traversal
    if not filename.endswith(".ts") or "/" in filename or ".." in filename:
        return "", 400

    path = os.path.join(sess["tmpdir"], filename)

    # Short wait for segment being produced right now (up to 15s)
    if not os.path.exists(path):
        proc = sess.get("proc")
        for _ in range(150):
            if os.path.exists(path):
                break
            if proc and proc.poll() is not None:
                return "", 404
            time.sleep(0.1)
        else:
            return "", 404
        time.sleep(0.05)

    resp = send_file(path, mimetype="video/MP2T")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@video_station_bp.route("/hls/<session_id>/stop", methods=["POST"])
def hls_stop_session(session_id):
    """Stop an HLS transcoding session and clean up."""
    _cleanup_hls(session_id)
    return jsonify(ok=True)


@video_station_bp.route("/watched/<int:vid>", methods=["POST"])

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


@video_station_bp.route("/rescan-metadata", methods=["POST"])
@admin_required
def rescan_metadata():
    """Re-probe all videos with empty codec metadata (fixes broken scans)."""
    if not _all_deps_ok():
        return jsonify({"error": "Brak ffmpeg/ffprobe."}), 400
    force_all = request.json and request.json.get("all", False)
    conn = _get_db()
    if force_all:
        rows = conn.execute("SELECT id, path FROM videos").fetchall()
    else:
        rows = conn.execute(
            "SELECT id, path FROM videos WHERE codec IS NULL OR codec = ''").fetchall()
    updated = 0
    for r in rows:
        path = r["path"]
        if not os.path.isfile(path):
            continue
        meta = _probe_video(path)
        if meta.get("codec"):
            conn.execute(
                "UPDATE videos SET codec=?, audio_codec=?, duration=?, width=?, "
                "height=?, bitrate=?, metadata_json=? WHERE id=?",
                (meta.get("codec", ""), meta.get("audio_codec", ""),
                 meta.get("duration", 0), meta.get("width", 0),
                 meta.get("height", 0), meta.get("bitrate", 0),
                 json.dumps(meta), r["id"]))
            updated += 1
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "updated": updated, "total": len(rows)})


@video_station_bp.route("/remove/<int:vid>", methods=["POST"])

def remove_from_library(vid):
    """Remove video from library without deleting the file."""
    conn = _get_db()
    r = conn.execute("SELECT id FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    conn.execute("DELETE FROM watch_state WHERE video_id=?", (vid,))
    conn.execute("DELETE FROM videos WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    # Remove thumbnail and poster
    for d in (_THUMB_DIR, _POSTER_DIR):
        p = os.path.join(d, str(vid) + '.jpg')
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return jsonify({"ok": True})


# ── batch operations ───────────────────────────────────────────
@video_station_bp.route("/batch", methods=["POST"])
def batch_action():
    """Batch operations on multiple videos: watched, unwatched, remove, hide, unhide."""
    d = request.json or {}
    ids = d.get("ids", [])
    action = d.get("action", "")
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "Brak wybranych filmów."}), 400
    if action not in ("watched", "unwatched", "remove", "hide", "unhide"):
        return jsonify({"error": "Nieznana akcja."}), 400
    conn = _get_db()
    placeholders = ",".join("?" * len(ids))
    if action == "watched":
        now = time.time()
        for vid in ids:
            conn.execute(
                "INSERT INTO watch_state (video_id,watched,position,updated_at) "
                "VALUES (?,1,0,?) ON CONFLICT(video_id) DO UPDATE SET "
                "watched=1, updated_at=?", (vid, now, now))
    elif action == "unwatched":
        now = time.time()
        for vid in ids:
            conn.execute(
                "INSERT INTO watch_state (video_id,watched,position,updated_at) "
                "VALUES (?,0,0,?) ON CONFLICT(video_id) DO UPDATE SET "
                "watched=0, position=0, updated_at=?", (vid, now, now))
    elif action == "remove":
        rows = conn.execute("SELECT id FROM videos WHERE id IN (%s)" % placeholders, ids).fetchall()
        for r in rows:
            conn.execute("DELETE FROM watch_state WHERE video_id=?", (r["id"],))
            conn.execute("DELETE FROM videos WHERE id=?", (r["id"],))
            for d_dir in (_THUMB_DIR, _POSTER_DIR):
                p = os.path.join(d_dir, str(r["id"]) + '.jpg')
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
    elif action == "hide":
        conn.execute("UPDATE videos SET hidden=1 WHERE id IN (%s)" % placeholders, ids)
    elif action == "unhide":
        conn.execute("UPDATE videos SET hidden=0 WHERE id IN (%s)" % placeholders, ids)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "count": len(ids)})


# ── hide password management ──────────────────────────────────
@video_station_bp.route("/hide-status", methods=["GET"])
def hide_status():
    """Check if hide password is set and if current session is unlocked."""
    return jsonify({
        "password_set": _hide_pw_is_set(),
        "unlocked": _is_hidden_unlocked(),
        "hidden_count": _count_hidden(),
    })


def _count_hidden():
    try:
        conn = _get_db()
        n = conn.execute("SELECT COUNT(*) FROM videos WHERE COALESCE(hidden,0)=1").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


@video_station_bp.route("/hide-password", methods=["POST"])
def hide_password_set():
    """Set or change the hide password."""
    d = request.json or {}
    password = d.get("password", "")
    old_password = d.get("old_password", "")
    if not password or len(password) < 4:
        return jsonify({"error": "Hasło musi mieć co najmniej 4 znaki."}), 400
    pw_data = _load_hide_pw()
    if pw_data.get("hash"):
        if not old_password or not _verify_pw(old_password, pw_data["hash"]):
            return jsonify({"error": "Nieprawidłowe obecne hasło."}), 403
    pw_data["hash"] = _hash_pw(password)
    _save_hide_pw(pw_data)
    return jsonify({"ok": True})


@video_station_bp.route("/hide-password", methods=["DELETE"])
def hide_password_remove():
    """Remove hide password and unhide all videos."""
    d = request.json or {}
    password = d.get("password", "")
    pw_data = _load_hide_pw()
    if not pw_data.get("hash"):
        return jsonify({"error": "Hasło nie jest ustawione."}), 400
    if not _verify_pw(password, pw_data["hash"]):
        return jsonify({"error": "Nieprawidłowe hasło."}), 403
    _save_hide_pw({})
    conn = _get_db()
    conn.execute("UPDATE videos SET hidden=0 WHERE hidden=1")
    conn.commit()
    conn.close()
    with _hide_lock:
        _hide_unlocked.clear()
    return jsonify({"ok": True})


@video_station_bp.route("/hide-unlock", methods=["POST"])
def hide_unlock():
    """Unlock hidden videos for the current session."""
    d = request.json or {}
    password = d.get("password", "")
    pw_data = _load_hide_pw()
    if not pw_data.get("hash"):
        return jsonify({"error": "Hasło nie jest ustawione."}), 400

    token = _get_token()
    now = time.time()

    # Brute-force protection
    with _hide_lock:
        att = _hide_attempts.get(token)
        if att and now < att.get("locked_until", 0):
            remaining = int(att["locked_until"] - now)
            return jsonify({"error": "Zbyt wiele prób. Odczekaj %ds." % remaining}), 429
        if att and now - att.get("first", now) > _HIDE_ATTEMPT_WINDOW:
            _hide_attempts.pop(token, None)

    if not _verify_pw(password, pw_data["hash"]):
        with _hide_lock:
            att = _hide_attempts.get(token, {"count": 0, "first": now, "locked_until": 0})
            att["count"] += 1
            if att["count"] >= _HIDE_MAX_ATTEMPTS:
                att["locked_until"] = now + _HIDE_LOCKOUT_TIME
                att["count"] = 0
            _hide_attempts[token] = att
        return jsonify({"error": "Nieprawidłowe hasło."}), 403

    with _hide_lock:
        _hide_attempts.pop(token, None)
        _hide_unlocked[token] = True
    return jsonify({"ok": True})


@video_station_bp.route("/hide-lock", methods=["POST"])
def hide_lock_session():
    """Re-lock hidden videos for the current session."""
    token = _get_token()
    with _hide_lock:
        _hide_unlocked.pop(token, None)
    return jsonify({"ok": True})


@video_station_bp.route("/subtitles/<int:vid>", methods=["GET"])

def subtitles(vid):
    """Find subtitle files (.srt, .ass, .ssa, .vtt) next to the video file."""
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    video_path = r["path"]
    base = os.path.splitext(video_path)[0]
    video_dir = os.path.dirname(video_path)
    video_stem = os.path.splitext(os.path.basename(video_path))[0]
    sub_exts = {'.srt', '.ass', '.ssa', '.vtt'}
    subs = []
    if os.path.isdir(video_dir):
        for fn in os.listdir(video_dir):
            fext = os.path.splitext(fn)[1].lower()
            if fext in sub_exts and fn.lower().startswith(video_stem.lower()):
                # Extract language tag from filename like "movie.en.srt"
                parts = os.path.splitext(fn)[0].split('.')
                lang = parts[-1] if len(parts) > 1 and len(parts[-1]) <= 3 else ''
                subs.append({
                    "filename": fn,
                    "path": os.path.join(video_dir, fn),
                    "language": lang,
                    "format": fext[1:],
                })
    return jsonify({"ok": True, "subtitles": subs})


@video_station_bp.route("/subtitle-file/<int:vid>/<path:filename>", methods=["GET"])

def subtitle_file(vid, filename):
    """Serve a subtitle file. Converts SRT to VTT for browser compatibility."""
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    video_dir = os.path.dirname(r["path"])
    sub_path = os.path.realpath(os.path.join(video_dir, filename))
    # Ensure path is within the video directory
    if not sub_path.startswith(os.path.realpath(video_dir) + os.sep):
        return jsonify({"error": "Niedozwolona ścieżka."}), 403
    if not os.path.isfile(sub_path):
        return jsonify({"error": "Plik napisów nie istnieje."}), 404

    ext = os.path.splitext(sub_path)[1].lower()
    if ext == '.vtt':
        return send_file(sub_path, mimetype="text/vtt")

    # Convert SRT → VTT on-the-fly
    if ext == '.srt':
        try:
            with open(sub_path, 'r', encoding='utf-8', errors='replace') as f:
                srt_content = f.read()
            vtt = "WEBVTT\n\n" + srt_content.replace(',', '.')
            return Response(vtt, mimetype="text/vtt")
        except Exception:
            return jsonify({"error": "Błąd odczytu napisów."}), 500

    return send_file(sub_path, mimetype="text/plain")

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
