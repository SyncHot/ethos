import os
import json
import time
import shutil
from collections import Counter
from flask import jsonify, request, send_file

from blueprints.video_station import (
    video_station_bp,
    _get_db, _scan_state, _watcher_state,
    _load_folders, _save_folders, _scan_worker,
    _format_duration, _all_deps_ok, _is_hidden_unlocked,
    _sio, _emit_progress, _probe_video,
    _THUMB_DIR, _POSTER_DIR, _BACKDROP_DIR,
    _THUMBSTRIP_DIR,
    _BROWSER_VIDEO_CODECS, _BROWSER_AUDIO_CODECS, _BROWSER_CONTAINERS,
    _tmdb_match_video, _load_tmdb_key,
)
from blueprints.admin_required import admin_required


@video_station_bp.route("/folders", methods=["GET"])

def get_folders():
    return jsonify({"folders": _load_folders()})


# System directories that should never be added as library folders
_SYSTEM_PATHS = {'/', '/etc', '/sys', '/proc', '/dev', '/boot', '/root', '/run', '/tmp', '/var', '/snap'}
_SYSTEM_PREFIXES = ('/etc/', '/sys/', '/proc/', '/dev/', '/boot/', '/root/', '/run/', '/snap/')

def _is_safe_library_path(folder):
    """Reject system directories and paths inside protected system trees."""
    rp = os.path.realpath(folder)
    if rp in _SYSTEM_PATHS:
        return False
    if rp.startswith(_SYSTEM_PREFIXES):
        return False
    return True


@video_station_bp.route("/folders", methods=["POST"])
@admin_required
def save_folders():
    folders = (request.json or {}).get("folders", [])
    valid = []
    for f in folders:
        if not isinstance(f, str) or not f.startswith("/"):
            continue
        rp = os.path.realpath(f)
        if os.path.isdir(rp) and _is_safe_library_path(rp):
            valid.append(rp)
    _save_folders(valid)
    return jsonify({"ok": True, "folders": valid})


@video_station_bp.route("/scan", methods=["POST"])
@admin_required
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
@admin_required
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


@video_station_bp.route("/history", methods=["GET"])

def watch_history():
    """Return recently watched videos (fully watched), sorted by last_watched_at desc."""
    conn = _get_db()
    limit = min(int(request.args.get("limit", 40)), 100)
    rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position, ws.last_watched_at FROM videos v "
        "JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE ws.watched=1 AND COALESCE(v.hidden,0)=0 "
        "ORDER BY COALESCE(ws.last_watched_at, ws.updated_at) DESC LIMIT ?",
        (limit,)).fetchall()
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
        "tmdb_genres": r["tmdb_genres"] or "",
        "watched": True,
        "position": r["position"] or 0,
        "last_watched_at": r["last_watched_at"] or r["updated_at"] or 0,
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
    codec_filter = request.args.get("codec", "")      # "hevc" | "av1" | "h264" | "transcode"
    res_filter = request.args.get("res", "")           # "4k" | "1080p" | "720p"
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
    if codec_filter == 'transcode':
        where.append("(LOWER(v.codec) NOT IN ('h264','avc1','vp8','vp9','theora','av1')"
                     " OR LOWER(v.audio_codec) NOT IN ('aac','mp3','opus','vorbis','flac'))")
    elif codec_filter in ('hevc', 'av1', 'h264'):
        where.append("LOWER(v.codec)=?")
        params.append(codec_filter)
    if res_filter == '4k':
        where.append("v.width >= 3840")
    elif res_filter == '1080p':
        where.append("v.width >= 1920 AND v.width < 3840")
    elif res_filter == '720p':
        where.append("v.width >= 1280 AND v.width < 1920")
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
            "tmdb_genres": r["tmdb_genres"] or "",
            "watched": bool(r["watched"]), "position": r["position"] or 0,
            "added_at": r["added_at"],
            "hidden": bool(r["hidden"]),
        })
    conn.close()
    return jsonify({"items": items, "total": total, "offset": offset, "limit": limit})


def _row_items(rows):
    """Convert DB rows to lean dicts for Netflix home rows."""
    items = []
    for r in rows:
        d = dict(r)
        items.append({
            "id": d["id"], "title": d["title"], "filename": d["filename"],
            "duration": d["duration"], "duration_fmt": _format_duration(d["duration"]),
            "thumb_ok": bool(d.get("thumb_ok")), "poster_ok": bool(d.get("poster_ok")),
            "backdrop_ok": bool(d.get("backdrop_ok", 0)),
            "tmdb_id": d.get("tmdb_id") or 0,
            "tmdb_title": d.get("tmdb_title") or "",
            "tmdb_year": d.get("tmdb_year") or "",
            "tmdb_rating": d.get("tmdb_rating") or 0,
            "tmdb_overview": d.get("tmdb_overview") or "",
            "tmdb_genres": d.get("tmdb_genres") or "",
            "tmdb_media_type": d.get("tmdb_media_type") or "movie",
            "watched": bool(d.get("watched")), "position": d.get("position") or 0,
            "added_at": d.get("added_at"),
        })
    return items


@video_station_bp.route("/home", methods=["GET"])
def home():
    """Netflix-style home rows: hero, continue watching, recently added, per-genre rows."""
    conn = _get_db()
    hidden_clause = "COALESCE(v.hidden,0)=0"

    # Hero: last watched with backdrop, else last added with backdrop
    hero_row = conn.execute(
        "SELECT v.*, ws.watched, ws.position, ws.last_watched_at FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE " + hidden_clause + " AND v.backdrop_ok=1 "
        "ORDER BY COALESCE(ws.last_watched_at, v.added_at) DESC LIMIT 1"
    ).fetchone()
    hero = None
    if hero_row:
        hero = _row_items([hero_row])[0]
        hero["last_watched_at"] = dict(hero_row).get("last_watched_at")

    # Continue watching — has position > 5% of duration
    cw_rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE " + hidden_clause + " AND ws.watched=0 "
        "AND v.duration > 0 AND ws.position > (v.duration * 0.05) "
        "ORDER BY ws.updated_at DESC LIMIT 20"
    ).fetchall()

    # Recently added
    recent_rows = conn.execute(
        "SELECT v.*, ws.watched, ws.position FROM videos v "
        "LEFT JOIN watch_state ws ON ws.video_id=v.id "
        "WHERE " + hidden_clause + " ORDER BY v.added_at DESC LIMIT 20"
    ).fetchall()

    # Genre rows — collect top genres present in library
    genre_rows = conn.execute(
        "SELECT tmdb_genres FROM videos WHERE COALESCE(hidden,0)=0"
        " AND tmdb_genres IS NOT NULL AND tmdb_genres != '' LIMIT 500"
    ).fetchall()
    # Count genre IDs
    from collections import Counter
    genre_counter = Counter()
    for g in genre_rows:
        for gid in (g["tmdb_genres"] or "").split(","):
            gid = gid.strip()
            if gid:
                genre_counter[gid] += 1
    # Top 6 genres
    top_genres = [gid for gid, _ in genre_counter.most_common(6)]

    genre_sections = []
    for gid in top_genres:
        g_rows = conn.execute(
            "SELECT v.*, ws.watched, ws.position FROM videos v "
            "LEFT JOIN watch_state ws ON ws.video_id=v.id "
            "WHERE " + hidden_clause + " AND (',' || v.tmdb_genres || ',') LIKE ? "
            "ORDER BY v.tmdb_rating DESC LIMIT 20",
            ('%,' + gid + ',%',)
        ).fetchall()
        if g_rows:
            genre_sections.append({"genre_id": int(gid), "items": _row_items(g_rows)})

    conn.close()
    return jsonify({
        "hero": hero,
        "continue_watching": _row_items(cw_rows),
        "recently_added": _row_items(recent_rows),
        "genres": genre_sections,
    })


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
    video_codec = (r["codec"] or "").lower()
    ext = os.path.splitext(r["path"])[1].lower()
    needs_tc = (bool(video_codec) and video_codec not in _BROWSER_VIDEO_CODECS) or \
               (bool(audio_codec) and audio_codec not in _BROWSER_AUDIO_CODECS) or \
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
    now = time.time()
    last_watched = now if watched else 0
    conn.execute(
        "INSERT INTO watch_state (video_id,watched,position,updated_at,last_watched_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET "
        "watched=excluded.watched,position=excluded.position,updated_at=excluded.updated_at,"
        "last_watched_at=CASE WHEN excluded.watched=1 THEN excluded.last_watched_at "
        "ELSE watch_state.last_watched_at END",
        (vid, watched, position, now, last_watched))
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
@admin_required
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


@video_station_bp.route("/delete/<int:vid>", methods=["POST"])
@admin_required
def delete_from_disk(vid):
    """Remove video from library AND permanently delete the file from disk."""
    conn = _get_db()
    r = conn.execute("SELECT id, path FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    file_path = r["path"]
    conn.execute("DELETE FROM watch_state WHERE video_id=?", (vid,))
    conn.execute("DELETE FROM videos WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    for d in (_THUMB_DIR, _POSTER_DIR, _BACKDROP_DIR):
        p = os.path.join(d, str(vid) + '.jpg')
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    deleted = False
    if file_path and os.path.isfile(file_path):
        try:
            os.remove(file_path)
            deleted = True
        except OSError as e:
            return jsonify({"error": "Usunięto z bazy, ale nie można usunąć pliku: " + str(e)}), 500
    return jsonify({"ok": True, "deleted": deleted, "path": file_path})


@video_station_bp.route("/parse-title/<int:vid>", methods=["GET"])
@admin_required
def parse_title(vid):
    """Parse video filename into a clean title and year for TMDb search."""
    conn = _get_db()
    r = conn.execute("SELECT filename FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    title, year = _parse_filename(r["filename"])
    return jsonify({"title": title, "year": year, "filename": r["filename"]})


# ── batch operations ───────────────────────────────────────────
@video_station_bp.route("/batch", methods=["POST"])
@admin_required
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

@video_station_bp.route("/watcher-status", methods=["GET"])
def watcher_status():
    """Return file-watcher state (watched folders and their last-seen mtimes)."""
    return jsonify({
        "ok": True,
        "watched": [
            {"folder": f, "last_mtime": mt}
            for f, mt in _watcher_state.items()
        ],
        "scanning": _scan_state.get("running", False),
    })


@video_station_bp.route("/scan-folder", methods=["POST"])
@admin_required
def scan_folder():
    """Trigger an incremental scan of a specific folder path.

    POST body: {folder: "/path/to/folder", use_tmdb: bool}
    """
    import gevent
    data = request.get_json(silent=True) or {}
    folder = data.get("folder", "").strip()
    if not folder:
        return jsonify({"error": "Brak parametru folder."}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": "Folder nie istnieje."}), 404
    if _scan_state.get("running"):
        return jsonify({"error": "Skanowanie już w toku."}), 409
    use_tmdb = bool(data.get("use_tmdb", False))
    _scan_state.update(running=True, stop_requested=False,
                       total=0, processed=0, current_file="")
    gevent.spawn(_scan_worker, [folder], use_tmdb)
    return jsonify({"ok": True, "message": "Skanowanie folderu w tle..."})
