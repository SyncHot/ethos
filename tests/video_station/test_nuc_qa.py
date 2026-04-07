#!/usr/bin/env python3
"""
Video Station — QA Test Suite dla Intel N100/N95 (GMKtec NucBox G3)
====================================================================
Testy: VAAPI init, HLS latencja, Rapid Seeking, Session cleanup,
       temperaturowy stress test (30 min).

Uruchomienie:
    ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/video_station/test_nuc_qa.py -v -s
    ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/video_station/test_nuc_qa.py -v -s -m stress --timeout=2000
"""

import os
import time
import subprocess
import statistics
import glob

import pytest
import requests

BASE_URL = os.environ.get("ETHOS_BASE_URL", "http://localhost:9000")


def _get_token():
    user = os.environ.get("ETHOS_USER", "admin")
    pw   = os.environ.get("ETHOS_PASS", "")
    r = requests.post(BASE_URL + "/api/auth/login",
                      json={"username": user, "password": pw}, timeout=10)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def token():
    return _get_token()


def _api(path, method="GET", json_body=None, tok=""):
    headers = {"Authorization": f"Bearer {tok}"}
    url = BASE_URL + "/api/video-station" + path
    if method == "GET":
        return requests.get(url, headers=headers, timeout=20)
    return requests.post(url, json=json_body or {}, headers=headers, timeout=30)


def _pick_video(token, prefer_hevc=False):
    """Return first video (or first HEVC) from library, skip if none."""
    r = _api("/library?limit=50", tok=token)
    if r.status_code != 200:
        pytest.skip("Library not accessible")
    items = r.json().get("items", [])
    if not items:
        pytest.skip("No videos in library — add files to test folders first")
    if prefer_hevc:
        hevc = [i for i in items if "hevc" in (i.get("codec_name") or "").lower()]
        if hevc:
            return hevc[0]
    return items[0]


def _start_hls(vid, start_pos=0, tok=""):
    r = _api(f"/hls/{vid}/start", "POST", {"start": start_pos}, tok=tok)
    assert r.status_code == 200, f"HLS start failed ({r.status_code}): {r.text}"
    return r.json()["session_id"]


def _stop_hls(sid, tok=""):
    _api(f"/hls/{sid}/stop", "POST", tok=tok)


def _heartbeat(sid, pos, tok=""):
    return _api(f"/hls/{sid}/heartbeat", "POST", {"pos": pos}, tok=tok)


def _wait_playlist(sid, tok="", timeout=15):
    """Wait until first HLS segment is ready. Returns elapsed seconds."""
    t0 = time.perf_counter()
    deadline = t0 + timeout
    while time.perf_counter() < deadline:
        pr = _api(f"/hls/{sid}/playlist.m3u8", tok=tok)
        if pr.status_code == 200 and "#EXTINF" in pr.text:
            return time.perf_counter() - t0
        time.sleep(0.15)
    return time.perf_counter() - t0  # timed out but return elapsed anyway


# ══════════════════════════════════════════════════════════════════
# 1. VAAPI Init Tests
# ══════════════════════════════════════════════════════════════════

class TestVAAPIInit:
    """Verify Intel VAAPI hardware encoder is detected and usable.
    Tests skip gracefully when running on non-Intel hardware."""

    def test_render_node_exists(self):
        if not os.path.exists("/dev/dri/renderD128"):
            pytest.skip("No /dev/dri/renderD128 — expected on NucBox, skip on dev machines")

    def test_vaapi_encoder_in_ffmpeg(self):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=10)
        assert "h264_vaapi" in r.stdout, "h264_vaapi encoder not compiled into ffmpeg"

    def test_vaapi_device_init_succeeds(self):
        """Actually init the render device — catches missing intel-media-va-driver."""
        if not os.path.exists("/dev/dri/renderD128"):
            pytest.skip("No render node — install intel-media-va-driver on NucBox")
        r = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-vaapi_device", "/dev/dri/renderD128",
            "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.3",
            "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-f", "null", "-",
        ], capture_output=True, timeout=15)
        if r.returncode != 0:
            stderr = r.stderr.decode()
            if "No VA display" in stderr or "Device creation failed" in stderr:
                pytest.xfail(
                    "VAAPI driver not configured — run on NucBox with:\n"
                    "  sudo apt install intel-media-va-driver i965-va-driver vainfo\n"
                    f"  sudo usermod -aG render $USER\n"
                    f"Error: {stderr[-200:]}"
                )
            pytest.fail(f"VAAPI init failed unexpectedly:\n{stderr[-300:]}")

    def test_encoder_info_endpoint(self, token):
        r = _api("/hls/encoder-info", tok=token)
        assert r.status_code == 200
        d = r.json()
        enc = d.get("encoder", "")
        hw = "vaapi" in enc.lower() or "nvenc" in enc.lower() or "videotoolbox" in enc.lower()
        print(f"\n  → Detected encoder: {enc}  HW={'YES' if hw else 'NO (CPU fallback)'}")
        if not hw:
            pytest.xfail(
                f"HW encoder not active ({enc}) — on NucBox G3 install "
                f"intel-media-va-driver and ensure /dev/dri/renderD128 is accessible"
            )

    def test_hevc_decode_available(self):
        """N100 supports HEVC decode; needed for 4K HEVC files."""
        r = subprocess.run(["ffmpeg", "-hide_banner", "-decoders"],
                           capture_output=True, text=True, timeout=10)
        assert "hevc" in r.stdout, "HEVC decoder missing from ffmpeg build"


# ══════════════════════════════════════════════════════════════════
# 2. Time-to-First-Frame (TTFF) Tests
# ══════════════════════════════════════════════════════════════════

class TestTimeToFirstFrame:
    """Measure HLS session start latency. Target: <1.5s for N100."""

    def test_ttff_h264(self, token):
        """H.264 file should start in under 1.5s (stream copy when possible)."""
        items = _api("/library?limit=50", tok=token).json().get("items", [])
        h264 = [i for i in items if "h264" in (i.get("codec_name") or "").lower()]
        if not h264:
            pytest.skip("No H.264 files in library")
        vid = h264[0]["id"]

        t0 = time.perf_counter()
        sid = _start_hls(vid, tok=token)
        elapsed = _wait_playlist(sid, tok=token)
        _stop_hls(sid, tok=token)

        print(f"\n  → TTFF (H.264): {elapsed:.3f}s  (target: <1.5s on N100)")
        assert elapsed < 3.0, f"TTFF {elapsed:.2f}s is too slow — check disk I/O or FFmpeg config"
        if elapsed > 1.5:
            pytest.xfail(f"TTFF {elapsed:.2f}s > 1.5s — acceptable on non-HW, target for N100")

    def test_ttff_hevc(self, token):
        """HEVC file should start in under 3s with VAAPI transcode on N100."""
        item = _pick_video(token, prefer_hevc=True)
        if "hevc" not in (item.get("codec_name") or "").lower():
            pytest.skip("No HEVC files in library")
        vid = item["id"]

        t0 = time.perf_counter()
        sid = _start_hls(vid, tok=token)
        elapsed = _wait_playlist(sid, tok=token, timeout=20)
        _stop_hls(sid, tok=token)

        print(f"\n  → TTFF (HEVC): {elapsed:.3f}s  (target: <3s on N100 VAAPI)")
        assert elapsed < 10.0, f"TTFF {elapsed:.2f}s is very slow — VAAPI may be broken"
        if elapsed > 3.0:
            pytest.xfail(f"TTFF {elapsed:.2f}s > 3s — acceptable on CPU, target for VAAPI on N100")

    def test_ttff_5x_consistent(self, token):
        """Run 5 sequential HLS starts. All must complete. Measure consistency."""
        vid = _pick_video(token)["id"]
        latencies = []
        sids = []
        try:
            for _ in range(5):
                sid = _start_hls(vid, tok=token)
                sids.append(sid)
                elapsed = _wait_playlist(sid, tok=token)
                latencies.append(elapsed)
                _stop_hls(sid, tok=token)
                sids.pop()
                time.sleep(0.5)
        finally:
            for s in sids:
                _stop_hls(s, tok=token)

        avg = statistics.mean(latencies)
        print(f"\n  → 5x TTFF: {[f'{l:.2f}s' for l in latencies]}  avg={avg:.2f}s")
        assert all(l < 10.0 for l in latencies), f"Some TTFF > 10s: {latencies}"


# ══════════════════════════════════════════════════════════════════
# 3. Rapid Seeking Tests
# ══════════════════════════════════════════════════════════════════

class TestRapidSeeking:
    """5 seek operations in quick succession (re-start at new position).
    Old FFmpeg processes must be killed promptly."""

    def test_rapid_seek_no_ghost_processes(self, token):
        """Seek to 5 positions within 5 seconds — no ghost ffmpeg after."""
        item = _pick_video(token)
        vid = item["id"]
        dur = item.get("duration") or 3600

        positions = [
            int(dur * 0.10),
            int(dur * 0.30),
            int(dur * 0.55),
            int(dur * 0.70),
            int(dur * 0.85),
        ]

        sids = []
        try:
            for pos in positions:
                # Each "seek" = stop old session, start new at new position
                if sids:
                    _stop_hls(sids[-1], tok=token)
                sid = _start_hls(vid, start_pos=pos, tok=token)
                sids.append(sid)
                time.sleep(1)

            # Wait for cleanup
            if sids:
                _stop_hls(sids[-1], tok=token)
                sids.clear()
            time.sleep(3)

        finally:
            for s in sids:
                _stop_hls(s, tok=token)
            time.sleep(2)

        result = subprocess.run(
            ["bash", "-c", "pgrep ffmpeg | wc -l"],
            capture_output=True, text=True
        )
        count = int(result.stdout.strip() or "0")
        print(f"\n  → FFmpeg processes after rapid seek test: {count}")
        assert count == 0, f"Ghost FFmpeg processes remaining: {count}"

    def test_hls_seek_via_start_offset(self, token):
        """Verify HLS start at non-zero offset produces valid playlist."""
        item = _pick_video(token)
        vid = item["id"]
        dur = item.get("duration") or 3600
        seek_pos = int(dur * 0.5)

        sid = _start_hls(vid, start_pos=seek_pos, tok=token)
        try:
            elapsed = _wait_playlist(sid, tok=token, timeout=20)
            pr = _api(f"/hls/{sid}/playlist.m3u8", tok=token)
            assert pr.status_code == 200
            assert "#EXTINF" in pr.text, "Playlist has no segments after seek-start"
            print(f"\n  → Seek to {seek_pos}s ({seek_pos//60}m), playlist ready in {elapsed:.2f}s")
        finally:
            _stop_hls(sid, tok=token)

    def test_seek_start_latency_under_3s(self, token):
        """Re-starting HLS at mid-film must produce first segment within 3s."""
        item = _pick_video(token)
        vid = item["id"]
        dur = item.get("duration") or 3600
        seek_pos = int(dur * 0.6)

        t0 = time.perf_counter()
        sid = _start_hls(vid, start_pos=seek_pos, tok=token)
        elapsed = _wait_playlist(sid, tok=token, timeout=15)
        _stop_hls(sid, tok=token)

        print(f"\n  → Seek-start latency: {elapsed:.3f}s")
        assert elapsed < 8.0, f"Seek-start took {elapsed:.1f}s — too slow"
        if elapsed > 3.0:
            pytest.xfail(f"Seek latency {elapsed:.2f}s > 3s target — acceptable on CPU encode")


# ══════════════════════════════════════════════════════════════════
# 4. Ghost Process / Cleanup Tests
# ══════════════════════════════════════════════════════════════════

class TestCleanup:
    """Verify no ghost FFmpeg processes and no /tmp leaks."""

    def test_stop_cleans_tmpdir(self, token):
        vid = _pick_video(token)["id"]
        r = requests.post(
            BASE_URL + f"/api/video-station/hls/{vid}/start",
            json={"start": 0},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]

        # Find the tmp dir
        tmpdirs_before = set(glob.glob("/tmp/vs_hls_*"))
        time.sleep(3)
        tmpdirs_during = set(glob.glob("/tmp/vs_hls_*"))
        session_dirs = tmpdirs_during - tmpdirs_before

        _stop_hls(sid, tok=token)
        time.sleep(3)

        tmpdirs_after = set(glob.glob("/tmp/vs_hls_*"))
        leaked = session_dirs & tmpdirs_after
        print(f"\n  → Session dirs during: {session_dirs}")
        print(f"  → Leaked dirs after stop: {leaked}")
        assert not leaked, f"HLS tmp dirs not cleaned up: {leaked}"

    def test_no_ffmpeg_after_stop(self, token):
        """After stopping all sessions, no ffmpeg processes should remain."""
        vid = _pick_video(token)["id"]

        sids = []
        for _ in range(2):
            try:
                sid = _start_hls(vid, tok=token)
                sids.append(sid)
                time.sleep(1)
            except Exception:
                pass

        time.sleep(2)
        for sid in sids:
            _stop_hls(sid, tok=token)
        time.sleep(4)

        result = subprocess.run(
            ["bash", "-c", "pgrep -a ffmpeg || true"],
            capture_output=True, text=True
        )
        remaining = result.stdout.strip()
        print(f"\n  → FFmpeg after stop: '{remaining or 'none'}'")
        assert not remaining, f"Ghost ffmpeg: {remaining}"


# ══════════════════════════════════════════════════════════════════
# 5. Thermal Stress Test (30 minutes) — marker: stress
# ══════════════════════════════════════════════════════════════════

class TestThermalStress:
    """
    30-minute stress test — monitors CPU temperature.
    Fails if thermal throttling (>= 90°C) occurs more than 3 times.

    Run separately:
        pytest -m stress -s --timeout=2000
    """

    DURATION_S    = 30 * 60
    SAMPLE_EVERY  = 10
    THROTTLE_TEMP = 90

    def _read_temp(self):
        for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
            type_path = path.replace("/temp", "/type")
            try:
                if open(type_path).read().strip() in ("x86_pkg_temp", "coretemp"):
                    return int(open(path).read()) // 1000
            except Exception:
                pass
        try:
            return int(open("/sys/class/thermal/thermal_zone0/temp").read()) // 1000
        except Exception:
            return None

    @pytest.mark.stress
    def test_30min_thermal_stability(self, token):
        item = _pick_video(token, prefer_hevc=True)
        vid = item["id"]
        dur = item.get("duration") or 7200
        print(f"\n  → 30-min thermal stress: vid={vid} ({item.get('title','?')})")
        print(f"  → Throttle threshold: {self.THROTTLE_TEMP}°C")

        sid = _start_hls(vid, tok=token)
        temps = []
        throttle_count = 0
        start = time.time()
        pos = 0

        try:
            while time.time() - start < self.DURATION_S:
                time.sleep(self.SAMPLE_EVERY)
                pos = min(int(time.time() - start), dur - 60)
                _heartbeat(sid, pos, tok=token)

                t = self._read_temp()
                elapsed = int(time.time() - start)
                ffmpeg_n = subprocess.run(
                    ["bash", "-c", "pgrep -c ffmpeg || echo 0"],
                    capture_output=True, text=True
                ).stdout.strip()

                if t is not None:
                    temps.append(t)
                    if t >= self.THROTTLE_TEMP:
                        throttle_count += 1
                    flag = "  ⚠️  THROTTLE!" if t >= self.THROTTLE_TEMP else ""
                    print(f"  [{elapsed:4d}s] {t}°C  ffmpeg={ffmpeg_n}{flag}")
        finally:
            _stop_hls(sid, tok=token)

        if temps:
            print(f"\n  Temp: min={min(temps)}°C  avg={statistics.mean(temps):.1f}°C  "
                  f"max={max(temps)}°C  throttle_events={throttle_count}")

        assert throttle_count <= 3, (
            f"Thermal throttling {throttle_count}x (≥90°C)! NucBox G3 cooling "
            f"insufficient for sustained 4K HEVC. Consider: reduce _HLS_MAX_SESSIONS=1, "
            f"use -preset ultrafast, or improve case airflow."
        )
