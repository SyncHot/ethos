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
  GET  /api/video-station/recent          - recently added videos
  GET  /api/video-station/collections     - auto-generated collections
  POST /api/video-station/watched/<int:vid> - mark as watched / update position

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

from flask import Blueprint, jsonify, request, Response, send_file

from host import host_run, q, safe_path, data_path, app_path

try:
    from blueprints.admin_required import admin_required, require_auth
except ImportError:
    def admin_required(f): return f
    def require_auth(f): return f

log = logging.getLogger('ethos.video_station')

video_station_bp = Blueprint('video-station', __name__, url_prefix='/api/video-station')

_DB_PATH = data_path('video_station.db')
_THUMB_DIR = data_path('video_thumbs')

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
        fp = safe_path(folder)
        if not fp or not os.path.isdir(fp):
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


# --- Scan worker ---

def _scan_worker(folders):
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
        fp = safe_path(f)
        if fp and os.path.isdir(fp):
            valid.append(f)
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
    import gevent
    gevent.spawn(_scan_worker, folders)
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


@video_station_bp.route("/stream/<int:vid>", methods=["GET"])
@require_auth
def stream(vid):
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    fp = safe_path(r["path"])
    if not fp or not os.path.isfile(fp):
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
