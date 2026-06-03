import os
import sys
import tempfile
import shutil
from flask import jsonify, send_file
import gevent
from gevent import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from host import q

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


def _run_ffmpeg_async(cmd, timeout=30):
    """Run ffmpeg command asynchronously using gevent subprocess."""
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait with timeout using gevent
        timer = gevent.Timeout(timeout)
        timer.start()
        try:
            stdout, stderr = proc.communicate()
            return proc.returncode
        except gevent.Timeout:
            proc.kill()
            proc.wait()
            return -1
        finally:
            timer.cancel()
    except Exception as e:
        log.warning('FFmpeg async error: %s', e)
        return -1


def _generate_thumbstrip(vid, fp, duration, sprite_path):
    """Generate thumbnail sprite using fast keyframe seeks (background task).

    At most 2 generations run in parallel (controlled by _thumbstrip_sem)
    to avoid saturating the CPU with concurrent ffmpeg decode jobs.
    
    PERMANENT FIX: 
    - Uses gevent subprocess instead of blocking host_run()
    - Limits max frames to 100 (long videos capped at ~50min)
    - Proper async/await for all operations
    - Early bailout on errors
    """
    tmpdir = None
    _get_thumbstrip_sem().acquire()
    try:
        from PIL import Image
        os.makedirs(_THUMBSTRIP_DIR, exist_ok=True)
        
        interval = 30  # one thumb every 30 seconds
        MAX_FRAMES = 100  # Safety limit: max 100 frames (~50 min video)
        
        tmpdir = tempfile.mkdtemp(prefix="vs_ts_")
        positions = list(range(0, int(duration), interval))
        
        # Safety limit for long videos
        if len(positions) > MAX_FRAMES:
            log.warning('Thumbstrip: video %s too long (%d frames), limiting to %d', 
                       vid, len(positions), MAX_FRAMES)
            positions = positions[:MAX_FRAMES]
        
        if not positions:
            return

        # Extract individual thumbnails using fast seek (-ss before -i)
        thumb_w, thumb_h = 160, 90
        cols = 10
        frame_paths = []
        failed_count = 0
        
        for i, pos in enumerate(positions):
            # Yield to other greenlets every 5 frames
            if i % 5 == 0:
                gevent.sleep(0)
                
            frame_path = os.path.join(tmpdir, "f%04d.jpg" % i)
            cmd = 'ffmpeg -y -ss %d -i %s -vframes 1 -vf scale=%d:%d -q:v 6 %s 2>/dev/null' % (
                pos, q(fp), thumb_w, thumb_h, q(frame_path))
            
            returncode = _run_ffmpeg_async(cmd, timeout=15)  # Reduced timeout
            
            if returncode == 0 and os.path.isfile(frame_path):
                frame_paths.append(frame_path)
            else:
                frame_paths.append(None)  # placeholder
                failed_count += 1
                
                # Early bailout if too many failures
                if failed_count > 10 and i < 20:
                    log.warning('Thumbstrip: too many early failures for vid %s, aborting', vid)
                    return

        valid = [p for p in frame_paths if p]
        if not valid:
            log.warning('Thumbstrip: no frames extracted for vid %s', vid)
            return
        
        if len(valid) < len(positions) * 0.3:
            log.warning('Thumbstrip: too few valid frames for vid %s (%d/%d)', 
                       vid, len(valid), len(positions))
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
        log.info('Thumbstrip generated for vid %s: %d/%d frames, %s',
                 vid, len(valid), len(positions), sprite_path)
    except Exception as e:
        log.warning('Thumbstrip generation error for vid %s: %s', vid, e)
    finally:
        _thumbstrip_generating.discard(vid)
        _get_thumbstrip_sem().release()
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
