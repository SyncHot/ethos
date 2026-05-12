import os
import sys
import tempfile
import shutil
from flask import jsonify, send_file
import gevent

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from host import host_run, q

from blueprints.video_station import (
    video_station_bp,
    _get_db,
    _THUMB_DIR, _POSTER_DIR, _BACKDROP_DIR, _THUMBSTRIP_DIR,
    log,
)

# Thumbstrip generation state (local to this module)
_thumbstrip_generating = set()  # video IDs currently being generated
_thumbstrip_sem = None          # gevent.Semaphore(2) — set in init_video_station


def _get_thumbstrip_sem():
    """Return the thumbstrip semaphore (initialized on first call)."""
    global _thumbstrip_sem
    if _thumbstrip_sem is None:
        _thumbstrip_sem = gevent.lock.Semaphore(2)
    return _thumbstrip_sem



def thumb(vid):
    p = os.path.join(_THUMB_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak miniatury."}), 404


def poster(vid):
    p = os.path.join(_POSTER_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak plakatu."}), 404


def backdrop(vid):
    p = os.path.join(_BACKDROP_DIR, str(vid) + ".jpg")
    if os.path.isfile(p):
        return send_file(p, mimetype="image/jpeg")
    return jsonify({"error": "Brak tła."}), 404


def thumbstrip(vid):
    db = _get_db()
    row = db.execute(
        "SELECT path, duration FROM videos WHERE id = ?", (vid,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Wideo nie istnieje."}), 404

    fp, duration = row
    sprite_path = os.path.join(_THUMBSTRIP_DIR, str(vid) + ".jpg")

    # Return existing sprite if present (unless browser bypasses cache with t=... param)
    if os.path.isfile(sprite_path):
        return send_file(sprite_path, mimetype="image/jpeg")

    # Avoid duplicate generation
    if vid in _thumbstrip_generating:
        return jsonify({"status": "generating"}), 202

    # Start background generation (at most 2 in parallel)
    _thumbstrip_generating.add(vid)
    gevent.spawn(_generate_thumbstrip, vid, fp, duration, sprite_path)
    return jsonify({"status": "generating"}), 202


def get_thumbstrip_meta(vid):
    """Return thumbstrip metadata (used by frontend for hover scrubbing)."""
    db = _get_db()
    row = db.execute(
        "SELECT path, duration FROM videos WHERE id = ?", (vid,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Wideo nie istnieje."}), 404

    fp, duration = row
    sprite_path = os.path.join(_THUMBSTRIP_DIR, str(vid) + ".jpg")

    if os.path.isfile(sprite_path):
        # Same hardcoded layout as generator
        interval = 30
        thumb_w, thumb_h = 160, 90
        cols = 10
        num_thumbs = int(duration) // interval
        if int(duration) % interval > 0:
            num_thumbs += 1
        return jsonify({
            "url": f"/api/video-station/thumbstrip/{vid}",
            "thumbWidth": thumb_w,
            "thumbHeight": thumb_h,
            "cols": cols,
            "interval": interval,
            "numThumbs": num_thumbs,
        })
    return jsonify({"error": "Thumbstrip niedostępny."}), 404



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

