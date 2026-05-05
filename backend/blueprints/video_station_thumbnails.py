import os
from flask import jsonify, send_file
import gevent

from blueprints.video_station import (
    video_station_bp,
    _get_db,
    _THUMB_DIR, _POSTER_DIR, _BACKDROP_DIR, _THUMBSTRIP_DIR,
    _thumbstrip_generating, _get_thumbstrip_sem, _generate_thumbstrip,
)
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
_thumbstrip_sem = None          # gevent.Semaphore(2) — set in init_video_station


def _get_thumbstrip_sem():
    global _thumbstrip_sem
    if _thumbstrip_sem is None:
        import gevent.lock
        _thumbstrip_sem = gevent.lock.Semaphore(2)
    return _thumbstrip_sem


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
    """Generate thumbnail sprite using fast keyframe seeks (background task).

    At most 2 generations run in parallel (controlled by _thumbstrip_sem)
    to avoid saturating the CPU with concurrent ffmpeg decode jobs.
    """
    tmpdir = None
    _get_thumbstrip_sem().acquire()
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
        _get_thumbstrip_sem().release()
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)

