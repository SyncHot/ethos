import os
import time
import tempfile
from flask import jsonify, request, Response, send_file

from host import q, host_run
from crypto_utils import hash_folder_password as _hash_pw, verify_folder_password as _verify_pw
from blueprints.video_station import (
    video_station_bp,
    _get_db,
    _hide_pw_is_set, _is_hidden_unlocked, _load_hide_pw, _save_hide_pw,
    _hide_lock, _hide_unlocked, _hide_attempts,
    _HIDE_MAX_ATTEMPTS, _HIDE_ATTEMPT_WINDOW, _HIDE_LOCKOUT_TIME,
    _get_token,
)
from blueprints.admin_required import admin_required
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
@admin_required
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
@admin_required
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


@video_station_bp.route("/embedded-subs/<int:vid>/<int:track>", methods=["GET"])
def embedded_subs(vid, track):
    """Extract an embedded subtitle stream from a video file to WebVTT on demand.

    Uses ffmpeg to extract the subtitle stream at the given stream index.
    Result is cached in /tmp for the session.
    """
    conn = _get_db()
    r = conn.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Nie znaleziono."}), 404
    fp = os.path.realpath(r["path"])
    if not os.path.isfile(fp):
        return jsonify({"error": "Plik nie istnieje."}), 404
    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg nie jest zainstalowany."}), 500

    cache_path = os.path.join(tempfile.gettempdir(), "vs_sub_%d_%d.vtt" % (vid, track))
    if not os.path.isfile(cache_path):
        try:
            cmd = "ffmpeg -hide_banner -loglevel error -i %s -map 0:%d -c:s webvtt -f webvtt %s -y" % (
                q(fp), track, q(cache_path))
            res = host_run(cmd, timeout=60)
            if res.returncode != 0 or not os.path.isfile(cache_path):
                return jsonify({"error": "Błąd ekstrakcji napisów."}), 500
        except Exception as e:
            return jsonify({"error": "Błąd: " + str(e)}), 500

    return send_file(cache_path, mimetype="text/vtt")

@video_station_bp.route("/rename/<int:vid>", methods=["POST"])
@admin_required
def rename_video(vid):
    """Rename a video file on disk and update DB path/filename."""
    d = request.json or {}
    new_name = (d.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "Brak nowej nazwy."}), 400
    conn = _get_db()
    r = conn.execute(
        "SELECT id, path, filename, folder FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Nie znaleziono."}), 404
    old_ext = os.path.splitext(r["filename"])[1].lower()
    new_base, new_ext = os.path.splitext(new_name)
    if not new_ext:
        new_name = new_name + old_ext
    elif new_ext.lower() != old_ext:
        conn.close()
        return jsonify({"error": "Nie można zmienić rozszerzenia pliku."}), 400
    folder = r["folder"]
    new_path = os.path.join(folder, new_name)
    try:
        new_path = safe_path(new_path, '/')
        old_path = safe_path(r["path"], '/')
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    if not os.path.isfile(old_path):
        conn.close()
        return jsonify({"error": "Plik nie istnieje na dysku."}), 404
    if os.path.exists(new_path) and new_path != old_path:
        conn.close()
        return jsonify({"error": "Plik o tej nazwie już istnieje."}), 400
    try:
        os.rename(old_path, new_path)
    except OSError as e:
        conn.close()
        return jsonify({"error": "Błąd zmiany nazwy: " + str(e)}), 500
    conn.execute(
        "UPDATE videos SET path=?, filename=? WHERE id=?",
        (new_path, new_name, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "new_path": new_path, "new_name": new_name})
