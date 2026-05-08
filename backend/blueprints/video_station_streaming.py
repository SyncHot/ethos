import os
import json
import time
import shutil
import re
import subprocess
import tempfile
import threading
from flask import jsonify, request, Response, send_file

from host import host_run, host_run_stream, q
from blueprints.video_station import (
    video_station_bp,
    _get_db,
    _hls_sessions, _HLS_MAX_SESSIONS,
    _cleanup_hls, _cleanup_stale_hls,
    _detect_hw_encoder, _detect_vaapi_decode_codecs,
    _compute_vaapi_scale, _adaptive_qp, _adaptive_crf,
    _hw_health_check, _get_intel_media_driver_version,
    _intel_driver_needs_ppa_upgrade,
    _reset_hw_cache,
    RENDER_NODE,
    log,
)
from blueprints.admin_required import admin_required
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


def _drain_stderr(pipe, buf_list):
    """Read all lines from ffmpeg stderr into a thread-safe list."""
    try:
        for line in pipe:
            buf_list.append(line)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _wait_first_bytes(proc, stderr_buf, timeout=15):
    """Block until ffmpeg produces output or exits with an error.

    Reads and buffers the first chunk so it can be prepended by the generator.
    Returns (first_chunk, error_string).  On success: (bytes, None).
    On failure: (b'', "error message").
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is None:
            # Still running — try to read first chunk without blocking long
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, 'read1') else proc.stdout.read(65536)
            if chunk:
                return chunk, None
            # Check if any stderr accumulated (early error hint)
            time.sleep(0.3)
        else:
            # ffmpeg exited before producing output
            stderr_text = b''.join(stderr_buf).decode(errors='replace').strip()[:500]
            if stderr_text:
                return b'', f"exit {rc}: {stderr_text}"
            return b'', f"ffmpeg exited with code {rc}"
    # Timeout — ffmpeg is hanging without producing output
    proc.terminate()
    stderr_text = b''.join(stderr_buf).decode(errors='replace').strip()[:500]
    return b'', (f"timeout: {stderr_text}" if stderr_text else "timeout: no output")


def _split_hw_pre_and_enc(args):
    """Split a flat ffmpeg arg list into (hw_pre, video_enc_args).

    hw_pre are the pre-input args (hwaccel, vaapi_device).
    video_enc_args are the encoder args (-c:v, -vf, -qp, -crf, etc.).
    For simple cases like ['-c:v', 'copy'] there is no hw_pre.
    """
    # Heuristic: hw_pre args are known prefixes; everything after input is encoder
    hw_prefixes = {'-hwaccel', '-hwaccel_device', '-hwaccel_output_format', '-vaapi_device'}
    hw_pre = []
    enc_start = None
    collecting_hw = False
    for i, a in enumerate(args):
        if a in hw_prefixes:
            collecting_hw = True
            hw_pre.append(a)
            continue
        if collecting_hw:
            hw_pre.append(a)
            collecting_hw = False
            continue
        if a == '-c:v' and enc_start is None:
            enc_start = i
            break
    if enc_start is not None:
        return hw_pre, args[enc_start:]
    return hw_pre, args


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

    start_sec = max(0.0, request.args.get("start", 0, type=float))
    audio_idx = request.args.get("audio", None, type=int)

    meta = json.loads(r["metadata_json"] or "{}")

    # Validate audio track index
    if audio_idx is not None:
        try:
            tracks = meta.get("audio_tracks", [])
        except Exception:
            tracks = []
        valid_indices = {t.get("index") for t in tracks} if tracks else set()
        if valid_indices and audio_idx not in valid_indices:
            return jsonify({"error": "Nieprawidłowy indeks ścieżki audio."}), 400

    # Build video encoder args (with HW acceleration when available)
    hw_pre = []
    vid_w = int(meta.get('width') or 0)
    vid_h = int(meta.get('height') or 0)
    pix_fmt = (meta.get('pix_fmt') or '').lower()
    is_10bit = 'p010' in pix_fmt or 'yuv420p10' in pix_fmt or 'yuv444p10' in pix_fmt
    if vcopy:
        video_enc_args = ["-c:v", "copy"]
    else:
        hw_enc = _detect_hw_encoder()
        qp = _adaptive_qp()
        crf = _adaptive_crf()
        if hw_enc == 'h264_vaapi':
            src = vcodec.replace('h265', 'hevc')
            sw, sh = _compute_vaapi_scale(vid_w, vid_h)
            if src in _detect_vaapi_decode_codecs():
                # Full HW pipeline: GPU decode → GPU encode (zero-copy, VAAPI handles 10-bit natively)
                hw_pre = ['-hwaccel', 'vaapi', '-hwaccel_device', RENDER_NODE,
                          '-hwaccel_output_format', 'vaapi']
                if sw:
                    video_enc_args = ["-vf", "scale_vaapi=w=%d:h=%d" % (sw, sh),
                                      "-c:v", "h264_vaapi", "-qp", str(qp)]
                else:
                    video_enc_args = ["-c:v", "h264_vaapi", "-qp", str(qp)]
                log.info("transcode encode: %s → h264_vaapi (full HW%s, qp=%d)", vcodec,
                         " scaled %dx%d" % (sw, sh) if sw else "", qp)
            else:
                # CPU decode → GPU encode; use p010le for 10-bit sources
                hw_pre = ['-vaapi_device', RENDER_NODE]
                upload_fmt = 'p010le' if is_10bit else 'nv12'
                if sw:
                    video_enc_args = ["-vf", "scale=%d:%d,format=%s,hwupload" % (sw, sh, upload_fmt),
                                      "-c:v", "h264_vaapi", "-qp", str(qp)]
                else:
                    video_enc_args = ["-vf", "format=%s,hwupload" % upload_fmt,
                                      "-c:v", "h264_vaapi", "-qp", str(qp)]
                log.info("transcode encode: %s → h264_vaapi (CPU dec + GPU enc%s, qp=%d, fmt=%s)", vcodec,
                         " scaled %dx%d" % (sw, sh) if sw else "", qp, upload_fmt)
        elif hw_enc in ('h264_nvenc', 'h264_videotoolbox'):
            video_enc_args = ["-c:v", hw_enc, "-preset", "fast", "-cq", str(qp)]
            log.info("transcode encode: %s → %s (qp=%d)", vcodec, hw_enc, qp)
        else:
            video_enc_args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf)]
            log.info("transcode encode: %s → libx264 (CPU, crf=%d)", vcodec, crf)

    # Try primary encoder; if it is a HW encoder and fails, fall back to libx264
    attempts = [hw_pre + video_enc_args]
    # Build CPU fallback when HW encoder was selected
    if video_enc_args and video_enc_args[-3] not in ("libx264",):
        cpu_crf = _adaptive_crf()
        fallback = (["-c:v", "libx264", "-preset", "ultrafast", "-crf", str(cpu_crf)])
        attempts.append(fallback)
        log.debug("transcode vid=%d: prepared CPU fallback (libx264) in case HW fails", vid)

    for attempt_args in attempts:
        hw_pre, video_enc_args = _split_hw_pre_and_enc(attempt_args)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-probesize", "100M", "-analyzeduration", "10M"]
        cmd += hw_pre
        if start_sec > 0:
            cmd += ["-ss", str(start_sec)]
        cmd += ["-i", fp]
        cmd += ["-map", "0:v:0"]
        if audio_idx is not None:
            cmd += ["-map", "0:%d" % audio_idx]
        else:
            cmd += ["-map", "0:a:0?"]
        cmd += video_enc_args
        cmd += [
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            "-"
        ]

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr_buf = []
        _ = threading.Thread(target=_drain_stderr, args=(proc.stderr, stderr_buf), daemon=True)

        # Wait for first bytes to detect early failures before committing to this encoder
        first_chunk, early_err = _wait_first_bytes(proc, stderr_buf, timeout=15)
        if early_err:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, Exception):
                proc.kill()
                proc.wait()
            if attempt_args is attempts[-1]:
                log.error("transcode vid=%d: all encoder attempts failed: %s", vid, early_err)
                return jsonify({"error": "Transkodowanie nie powiodło się: " + early_err}), 500
            log.warning("transcode vid=%d: encoder failed (%s) — falling back to CPU", vid, early_err)
            continue

        def generate(_proc=proc, _first=first_chunk, _stderr=stderr_buf):
            try:
                if _first:
                    yield _first
                while True:
                    chunk = _proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            except (OSError, GeneratorExit):
                pass
            finally:
                _proc.stdout.close()
                try:
                    _proc.terminate()
                    _proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _proc.kill()
                    _proc.wait()
                rc = _proc.returncode
                stderr_text = b''.join(_stderr).decode(errors='replace')[:500]
                if rc and rc != -9 and stderr_text:
                    log.error("transcode vid=%d ffmpeg exit=%d: %s", vid, rc, stderr_text.strip())

        return Response(generate(), mimetype="video/mp4",
                       headers={
                           "Cache-Control": "no-cache",
                           "Accept-Ranges": "none",
                           "X-Content-Type-Options": "nosniff",
                       })

    # Should not reach here, but safety net
    return jsonify({"error": "Transkodowanie nie powiodło się."}), 500


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
    start_sec = max(0.0, float(data.get("start") or 0))
    audio_idx = data.get("audio", None)

    # Stop any existing HLS session for this video
    for sid in list(_hls_sessions):
        if _hls_sessions[sid].get("vid") == vid:
            _cleanup_hls(sid)

    # Enforce max concurrent sessions — kill the oldest non-matching session
    while len(_hls_sessions) >= _HLS_MAX_SESSIONS:
        oldest_sid = min(_hls_sessions, key=lambda s: _hls_sessions[s].get('created', 0))
        log.info("HLS session limit reached — killing oldest session %s", oldest_sid)
        _cleanup_hls(oldest_sid)

    vcodec = (r["codec"] or "").lower()
    vcopy = vcodec in ("h264", "vp8", "vp9")
    pre_input_args = ""
    vf_arg = ""
    v_enc_arg = ""

    # Get video dimensions for VAAPI resolution limit check (max 4096x4096 for h264_vaapi)
    meta = json.loads(r["metadata_json"] or '{}')
    vid_w = int(meta.get('width') or 0)
    vid_h = int(meta.get('height') or 0)
    pix_fmt = (meta.get('pix_fmt') or '').lower()
    is_10bit = 'p010' in pix_fmt or 'yuv420p10' in pix_fmt or 'yuv444p10' in pix_fmt

    if vcopy:
        v_enc_arg = "copy"
    else:
        hw_enc = _detect_hw_encoder()
        qp = _adaptive_qp()
        crf = _adaptive_crf()
        if hw_enc == 'h264_vaapi':
            src = vcodec.replace('h265', 'hevc')
            sw, sh = _compute_vaapi_scale(vid_w, vid_h)
            if src in _detect_vaapi_decode_codecs():
                # Full HW pipeline: GPU decode → GPU encode (zero-copy, VAAPI handles 10-bit natively)
                pre_input_args = "-hwaccel vaapi -hwaccel_device %s -hwaccel_output_format vaapi" % RENDER_NODE
                vf_arg = "-vf scale_vaapi=w=%d:h=%d" % (sw, sh) if sw else ""
                v_enc_arg = "h264_vaapi -qp %d" % qp
                log.info("HLS encode: %s → h264_vaapi (full HW%s, qp=%d)", vcodec,
                         " scaled %dx%d" % (sw, sh) if sw else "", qp)
            else:
                # CPU decode → GPU encode; use p010le for 10-bit sources
                pre_input_args = "-vaapi_device %s" % RENDER_NODE
                upload_fmt = 'p010le' if is_10bit else 'nv12'
                if sw:
                    vf_arg = "-vf scale=%d:%d,format=%s,hwupload" % (sw, sh, upload_fmt)
                else:
                    vf_arg = "-vf format=%s,hwupload" % upload_fmt
                v_enc_arg = "h264_vaapi -qp %d" % qp
                log.info("HLS encode: %s → h264_vaapi (CPU dec + GPU enc%s, qp=%d, fmt=%s)", vcodec,
                         " scaled %dx%d" % (sw, sh) if sw else "", qp, upload_fmt)
        elif hw_enc in ('h264_nvenc', 'h264_videotoolbox'):
            v_enc_arg = "%s -preset fast -cq %d" % (hw_enc, qp)
            log.info("HLS encode: %s → %s (qp=%d)", vcodec, hw_enc, qp)
        else:
            v_enc_arg = "libx264 -preset ultrafast -crf %d" % crf
            log.info("HLS encode: %s → libx264 (CPU, crf=%d)", vcodec, crf)

    session_id = "%d_%s" % (vid, os.urandom(4).hex())
    tmpdir = tempfile.mkdtemp(prefix="vs_hls_")
    cmd = "ffmpeg -hide_banner -loglevel error -probesize 100M -analyzeduration 10M"
    if pre_input_args:
        cmd += " %s" % pre_input_args
    if start_sec > 0:
        cmd += " -ss %s" % start_sec
    cmd += " -i %s" % q(fp)
    cmd += " -map 0:v:0"
    if audio_idx is not None:
        cmd += " -map 0:%d" % int(audio_idx)
    else:
        cmd += " -map 0:a:0?"
    if vf_arg:
        cmd += " %s" % vf_arg
    cmd += " -c:v %s -c:a aac -b:a 192k -ac 2" % v_enc_arg
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
        "last_heartbeat": time.time(),
        "client_pos": start_sec,
        "paused": False,
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

    return jsonify(ok=True, session_id=session_id, start_offset=start_sec,
                   sub_tracks=json.loads(r["metadata_json"] or '{}').get('sub_tracks', []))


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


@video_station_bp.route("/hls/<session_id>/heartbeat", methods=["POST"])
def hls_heartbeat(session_id):
    """Client keepalive — update last_heartbeat and current playback position.

    POST body: {pos: seconds}
    Must be called every ~8s while the player is active.
    If no heartbeat for _HLS_HEARTBEAT_TIMEOUT seconds, the watchdog kills ffmpeg.
    """
    sess = _hls_sessions.get(session_id)
    if not sess:
        return jsonify(ok=False, error="Session not found"), 404
    data = request.get_json(silent=True) or {}
    if isinstance(data, str):
        # Defensive: body was double-serialized by client (JSON.stringify in api())
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    sess['last_heartbeat'] = time.time()
    pos = float(data.get('pos', sess.get('client_pos', 0)))
    sess['client_pos'] = pos
    paused = sess.get('paused', False)
    return jsonify(ok=True, paused=paused, client_pos=pos)


@video_station_bp.route("/hls/encoder-info", methods=["GET"])
def hls_encoder_info():
    """Return current HW encoder in use (for player stats overlay)."""
    enc = _detect_hw_encoder()
    hw = enc != 'libx264'
    libva_driver_name = os.environ.get('LIBVA_DRIVER_NAME', '')
    return jsonify(
        encoder=enc,
        type='hw' if hw else 'sw',
        label='GPU (%s)' % enc if hw else 'CPU (libx264)',
        tooltip=('Intel QuickSync (iHD) — Aktywny' if 'vaapi' in enc else enc) if hw else 'libx264 (CPU) — akceleracja GPU niedostępna',
        libva_driver_name=libva_driver_name,
    )


@video_station_bp.route("/hw-health", methods=["GET"])
def hw_health():
    """Detailed HW acceleration health report.

    Returns status, diagnostic info, and step-by-step setup instructions
    when HW acceleration is not working. The frontend uses this to show
    a warning banner and setup wizard modal.
    """
    return jsonify(_hw_health_check())


@video_station_bp.route("/vainfo-test", methods=["GET"])
def vainfo_test():
    """Run vainfo against /dev/dri/renderD128 and return parsed result.

    Returns:
      ok                : bool
      output            : raw vainfo stdout+stderr
      profiles          : list of detected VAProfile/VAEntrypoint strings
      error_code        : "IHD_INIT_FAILED" | "PERMISSION_DENIED" | "MISSING_DRIVER" |
                          "NO_RENDER_NODE" | "MISSING_VAINFO" | "TIMEOUT" | ""
      error             : human-readable error string (only on failure)
      libva_driver_name : value of LIBVA_DRIVER_NAME env var
      libva_correct     : bool — LIBVA_DRIVER_NAME == 'ihd'
    """
    RENDER_NODE = '/dev/dri/renderD128'
    libva_driver_name = os.environ.get('LIBVA_DRIVER_NAME', '')
    libva_correct = libva_driver_name.lower() == 'ihd'

    if not shutil.which('vainfo'):
        return jsonify(ok=False, output='', profiles=[], error_code='MISSING_VAINFO',
                       error='Polecenie vainfo nie jest zainstalowane. '
                             'Uruchom: sudo apt install vainfo',
                       libva_driver_name=libva_driver_name, libva_correct=libva_correct)
    if not os.path.exists(RENDER_NODE):
        return jsonify(ok=False, output='', profiles=[], error_code='NO_RENDER_NODE',
                       error='Węzeł %s nie istnieje.' % RENDER_NODE,
                       libva_driver_name=libva_driver_name, libva_correct=libva_correct)
    try:
        r = subprocess.run(
            ['vainfo', '--display', 'drm', '--device', RENDER_NODE],
            capture_output=True, timeout=10, text=True
        )
        combined = (r.stdout or '') + (r.stderr or '')
        ok = r.returncode == 0 and 'VAEntrypoint' in combined

        # Parse supported profiles
        profiles = []
        for line in combined.splitlines():
            m = re.search(r'(VAProfile\w+)\s*/\s*(VAEntrypoint\w+)', line)
            if m:
                profiles.append('%s / %s' % (m.group(1), m.group(2)))

        if ok:
            return jsonify(ok=True, output=combined, profiles=profiles, error_code='', error='',
                           libva_driver_name=libva_driver_name, libva_correct=libva_correct)

        # Classify the failure
        if ('iHD_drv_video.so init failed' in combined
                or 'Failed to open the given device' in combined
                or ('init failed' in combined and 'iHD' in combined)):
            error_code = 'IHD_INIT_FAILED'
            error_msg = ('iHD_drv_video.so init failed — brakujące zależności lub LIBVA_DRIVER_NAME. '
                         'Uruchom: sudo apt install intel-media-va-driver-non-free libmfx1 libmfx-gen1 libva-drm2')
        elif 'Permission denied' in combined or 'permission denied' in combined:
            error_code = 'PERMISSION_DENIED'
            error_msg = ('Brak dostępu do /dev/dri/renderD128. '
                         'Uruchom: sudo usermod -aG render,video $USER')
        elif 'va_openDriver() returns -1' in combined:
            error_code = 'MISSING_DRIVER'
            error_msg = 'Brak sterownika VAAPI. Uruchom: sudo apt install intel-media-va-driver-non-free'
        else:
            err_m = re.search(r'(error|failed)[^\n]*', combined, re.I)
            error_code = 'UNKNOWN'
            error_msg = err_m.group(0) if err_m else ('vainfo błąd (kod %d)' % r.returncode)

        return jsonify(ok=False, output=combined, profiles=[], error_code=error_code, error=error_msg,
                       libva_driver_name=libva_driver_name, libva_correct=libva_correct)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, output='', profiles=[], error_code='TIMEOUT',
                       error='Timeout — vainfo nie odpowiedział w 10s.',
                       libva_driver_name=libva_driver_name, libva_correct=libva_correct)
    except Exception as exc:
        return jsonify(ok=False, output='', profiles=[], error_code='EXCEPTION',
                       error=str(exc), libva_driver_name=libva_driver_name, libva_correct=libva_correct)


@video_station_bp.route("/gpu-retest", methods=["POST"])
def gpu_retest():
    """Reset HW encoder cache and re-probe GPU capabilities via vainfo + ffmpeg.

    Returns:
      ok                : bool — vainfo succeeded
      hw_encoder        : str  — newly detected best encoder
      is_hw             : bool
      error_code        : str  — IHD_INIT_FAILED | PERMISSION_DENIED | etc.
      profiles          : list — VAProfile/VAEntrypoint strings
      codecs            : dict — {h264, hevc, vp9, av1}: bool
      libva_driver_name : str
      libva_correct     : bool
    """
    _reset_hw_cache()

    RENDER_NODE = '/dev/dri/renderD128'
    libva_driver_name = os.environ.get('LIBVA_DRIVER_NAME', '')
    libva_correct = libva_driver_name.lower() == 'ihd'

    # ── vainfo probe ────────────────────────────────────────────────────
    vainfo_ok = False
    profiles = []
    error_code = ''
    vainfo_output = ''

    if shutil.which('vainfo') and os.path.exists(RENDER_NODE):
        try:
            vr = subprocess.run(
                ['vainfo', '--display', 'drm', '--device', RENDER_NODE],
                capture_output=True, timeout=10, text=True
            )
            vainfo_output = (vr.stdout or '') + (vr.stderr or '')
            vainfo_ok = vr.returncode == 0 and 'VAEntrypoint' in vainfo_output
            for line in vainfo_output.splitlines():
                m = re.search(r'(VAProfile\w+)\s*/\s*(VAEntrypoint\w+)', line)
                if m:
                    profiles.append('%s / %s' % (m.group(1), m.group(2)))
            if not vainfo_ok:
                if ('iHD_drv_video.so init failed' in vainfo_output
                        or 'Failed to open the given device' in vainfo_output
                        or ('init failed' in vainfo_output and 'iHD' in vainfo_output)):
                    error_code = 'IHD_INIT_FAILED'
                elif 'Permission denied' in vainfo_output or 'permission denied' in vainfo_output:
                    error_code = 'PERMISSION_DENIED'
                elif 'va_openDriver() returns -1' in vainfo_output:
                    error_code = 'MISSING_DRIVER'
                else:
                    error_code = 'UNKNOWN'
        except subprocess.TimeoutExpired:
            error_code = 'TIMEOUT'
        except Exception:
            error_code = 'EXCEPTION'

    # ── per-codec ffmpeg probe (encode) ─────────────────────────────────
    codecs = {}
    if shutil.which('ffmpeg') and os.path.exists(RENDER_NODE):
        _base = ['-vaapi_device', RENDER_NODE, '-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.1',
                 '-vf', 'format=nv12,hwupload']
        codec_tests = [
            ('h264', _base + ['-c:v', 'h264_vaapi', '-f', 'null', '-']),
            ('hevc', _base + ['-c:v', 'hevc_vaapi', '-f', 'null', '-']),
            ('vp9',  _base + ['-c:v', 'vp9_vaapi',  '-f', 'null', '-']),
            ('av1',  _base + ['-c:v', 'av1_vaapi',  '-f', 'null', '-']),
        ]
        for codec_name, args in codec_tests:
            try:
                cr = subprocess.run(
                    ['ffmpeg', '-hide_banner', '-loglevel', 'error'] + args,
                    capture_output=True, timeout=6
                )
                codecs[codec_name] = cr.returncode == 0
            except Exception:
                codecs[codec_name] = False

    # ── decode capability (from vainfo VAEntrypointVLD profiles) ─────────
    dec_set = _detect_vaapi_decode_codecs()
    decode_codecs = {c: (c in dec_set) for c in ('h264', 'hevc', 'vp9', 'av1')}

    new_encoder = _detect_hw_encoder()
    return jsonify(
        ok=vainfo_ok,
        hw_encoder=new_encoder,
        is_hw=new_encoder != 'libx264',
        error_code=error_code,
        profiles=profiles,
        codecs=codecs,
        decode_codecs=decode_codecs,
        libva_driver_name=libva_driver_name,
        libva_correct=libva_correct,
        tooltip=('Intel QuickSync (iHD) — Aktywny' if vainfo_ok else
                 'libx264 (CPU) — akceleracja GPU niedostępna'),
    )



@video_station_bp.route("/hw-install", methods=["GET", "POST"])
@admin_required
def hw_install():
    """Auto-install VAAPI drivers for Intel GPUs, streamed as SSE.

    Streams progress lines as:
      data: {"line": "...", "done": false}
    Final event:
      data: {"done": true, "ok": true|false, "hw_encoder": "..."}
    """
    from flask import Response

    def _generate():

        def _send(line, done=False, **kw):
            import json
            payload = {"line": line, "done": done}
            payload.update(kw)
            return "data: %s\n\n" % json.dumps(payload)

        try:
            yield _send("🔍 Wykrywanie systemu...")
            # Check if we can use non-free
            sources = ""
            try:
                sources = open("/etc/apt/sources.list").read()
            except Exception:
                pass

            if "non-free" not in sources:
                yield _send("📦 Włączanie repozytorium non-free...")
                r = host_run(
                    "sed -i 's/main$/main contrib non-free non-free-firmware/g' /etc/apt/sources.list",
                    timeout=10
                )
                if r.returncode != 0:
                    yield _send("⚠️  Nie udało się edytować sources.list (kontynuuję...)")

            # Check if Intel media driver is too old for newer GPUs (N150, N200, etc.)
            installed_ver = _get_intel_media_driver_version()
            if _intel_driver_needs_ppa_upgrade(installed_ver):
                yield _send("⚠️  Wykryto stary sterownik Intel Media Driver (%s < 25.x)." % installed_ver)
                yield _send("📌 Dodawanie Intel Graphics PPA (kobuk-team) dla nowszych GPU...")

                ppa_key_url = (
                    "https://keyserver.ubuntu.com/pks/lookup"
                    "?op=get&search=0x0C0E6AF955CE463C03FC51574D098D70AFBE5E1F"
                )
                key_path = "/etc/apt/trusted.gpg.d/kobuk-intel-graphics.gpg"
                r = host_run(
                    "curl -fsSL %s | gpg --dearmor -o %s" % (q(ppa_key_url), q(key_path)),
                    timeout=20
                )
                if r.returncode != 0:
                    yield _send("⚠️  Nie udało się pobrać klucza GPG PPA (kontynuuję bez PPA...)")
                else:
                    ppa_line = (
                        "deb https://ppa.launchpadcontent.net/kobuk-team/intel-graphics/ubuntu noble main"
                    )
                    r = host_run(
                        "echo %s > /etc/apt/sources.list.d/intel-graphics.list" % q(ppa_line),
                        timeout=5
                    )
                    yield _send("✅ Dodano Intel Graphics PPA.")

            yield _send("🔄 Aktualizacja listy pakietów (apt update)...")
            for line in host_run_stream("apt-get update -qq 2>&1"):
                yield _send(line.rstrip())

            yield _send("📥 Instalacja sterowników VAAPI...")
            pkgs = "intel-media-va-driver-non-free libigdgmm12 libva2 libva-drm2 libvpl2 i965-va-driver vainfo"
            for line in host_run_stream(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends %s 2>&1" % pkgs
            ):
                yield _send(line.rstrip())

            # Add current user to render+video groups
            run_user = os.environ.get('SUDO_USER', '') or os.environ.get('USER', '') or 'ethos'
            yield _send("👤 Dodawanie użytkownika '%s' do grup render i video..." % run_user)
            host_run("usermod -aG render,video %s 2>&1 || true" % q(run_user), timeout=10)

            yield _send("✅ Sterowniki zainstalowane. Testuję VAAPI...")

            # Reset encoder cache and re-detect
            _reset_hw_cache()
            new_encoder = _detect_hw_encoder()
            if new_encoder != 'libx264':
                yield _send(
                    "🎉 Akceleracja sprzętowa aktywna! Enkoder: %s" % new_encoder,
                    done=True, ok=True, hw_encoder=new_encoder
                )
            else:
                yield _send(
                    "ℹ️  VAAPI zainstalowane — restart serwisu wymagany do aktywacji.",
                    done=True, ok=True, hw_encoder='libx264', restart_required=True
                )
        except Exception as exc:
            yield _send("❌ Błąd: %s" % str(exc), done=True, ok=False, hw_encoder='libx264')

    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

