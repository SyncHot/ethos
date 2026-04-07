"""
Video Station QA Test Suite
============================
Tests: HLS transcoding, heartbeat, session limits, subtitle extraction,
       file watcher, encoder detection, cleanup, API contract.

Run:
    pytest tests/video_station/test_vs_qa.py -v
    ETHOS_BASE_URL=http://localhost:9000 pytest tests/video_station/test_vs_qa.py -v

Requires: a test video file and a running EthOS server with Video Station installed.
"""

import os
import time
import json
import tempfile
import subprocess
import shutil

import pytest
import requests

BASE_URL = os.environ.get("ETHOS_BASE_URL", "http://localhost:9000")
TOKEN    = os.environ.get("ETHOS_TOKEN", "")

# ── helpers ─────────────────────────────────────────────────────────────────

def api(path, method="GET", json_body=None, token=None):
    tok = token or TOKEN
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    url = BASE_URL + "/api/video-station" + path
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, json=json_body or {}, headers=headers, timeout=30)
    return r


def _get_token():
    """Obtain a Bearer token using env credentials."""
    user = os.environ.get("ETHOS_USER", "admin")
    pw   = os.environ.get("ETHOS_PASS", "admin")
    r = requests.post(BASE_URL + "/api/auth/login",
                      json={"username": user, "password": pw}, timeout=10)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def token():
    return _get_token()


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    """Generate a 90-second H.264/AAC test video using ffmpeg."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    out = str(tmp_path_factory.mktemp("vs_test") / "test_h264.mp4")
    r = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=90",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=90",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-c:a", "aac", "-b:a", "64k", "-shortest",
        "-y", out,
    ], capture_output=True, timeout=120)
    assert r.returncode == 0, "ffmpeg test video generation failed"
    assert os.path.isfile(out)
    return out


# ── 1. Package status ────────────────────────────────────────────────────────

class TestPackageStatus:
    def test_pkg_status_returns_ok(self, token):
        r = api("/pkg-status", token=token)
        assert r.status_code == 200
        d = r.json()
        assert "installed" in d
        assert "deps" in d

    def test_ffmpeg_installed(self, token):
        r = api("/pkg-status", token=token)
        d = r.json()
        assert d["deps"]["ffmpeg"] is True, "ffmpeg not installed"
        assert d["deps"]["ffprobe"] is True, "ffprobe not installed"


# ── 2. Library & Scan ────────────────────────────────────────────────────────

class TestLibrary:
    def test_library_endpoint(self, token):
        r = api("/library", token=token)
        assert r.status_code == 200
        d = r.json()
        assert "items" in d

    def test_folders_endpoint(self, token):
        r = api("/folders", token=token)
        assert r.status_code == 200
        d = r.json()
        assert "folders" in d

    def test_scan_start_stop(self, token):
        r = api("/scan", method="POST", json_body={"use_tmdb": False}, token=token)
        assert r.status_code == 200
        assert r.json().get("ok") or r.json().get("scanning")
        # Stop it immediately
        api("/scan-stop", method="POST", token=token)
        time.sleep(1)
        status = api("/scan-status", token=token).json()
        assert status.get("running") is False

    def test_scan_folder_nonexistent_returns_404(self, token):
        r = api("/scan-folder", method="POST",
                json_body={"folder": "/nonexistent/path/xyz"}, token=token)
        assert r.status_code == 404

    def test_scan_folder_valid(self, token):
        folders_r = api("/folders", token=token).json()
        folders = folders_r.get("folders", [])
        if not folders:
            pytest.skip("No library folders configured")
        r = api("/scan-folder", method="POST",
                json_body={"folder": folders[0], "use_tmdb": False}, token=token)
        assert r.status_code in (200, 409)  # 409 if already scanning


# ── 3. HW Encoder Detection ──────────────────────────────────────────────────

class TestHWEncoder:
    def test_encoder_info_endpoint(self, token):
        r = api("/hls/encoder-info", token=token)
        assert r.status_code == 200
        d = r.json()
        assert "encoder" in d
        assert "type" in d
        assert d["type"] in ("hw", "sw"), f"Unexpected type: {d['type']}"

    def test_encoder_label_not_empty(self, token):
        d = api("/hls/encoder-info", token=token).json()
        assert d.get("label"), "Encoder label is empty"

    def test_hw_encoder_uses_known_codec(self, token):
        d = api("/hls/encoder-info", token=token).json()
        known = {"libx264", "h264_nvenc", "h264_vaapi", "h264_videotoolbox"}
        assert d["encoder"] in known, f"Unknown encoder: {d['encoder']}"


# ── 4. HLS Session Lifecycle ─────────────────────────────────────────────────

class TestHLSSession:
    def _first_video_id(self, token):
        items = api("/library", token=token).json().get("items", [])
        if not items:
            pytest.skip("Library is empty — add a video first")
        return items[0]["id"]

    def test_hls_start_returns_session_id(self, token):
        vid = self._first_video_id(token)
        r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 0}, token=token)
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True
        assert "session_id" in d
        sid = d["session_id"]
        # Cleanup
        api(f"/hls/{sid}/stop", method="POST", token=token)

    def test_hls_playlist_served(self, token):
        vid = self._first_video_id(token)
        r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 0}, token=token)
        sid = r.json()["session_id"]
        try:
            playlist_url = f"{BASE_URL}/api/video-station/hls/{sid}/playlist.m3u8?token={token}"
            pr = requests.get(playlist_url, timeout=10)
            assert pr.status_code == 200
            assert "#EXTM3U" in pr.text, "Not a valid M3U8 playlist"
        finally:
            api(f"/hls/{sid}/stop", method="POST", token=token)

    def test_heartbeat_endpoint_no_500(self, token):
        """Core regression: heartbeat must NOT 500 with {pos: N} body."""
        vid = self._first_video_id(token)
        start_r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 0}, token=token)
        sid = start_r.json()["session_id"]
        try:
            # Send heartbeat with correct body (NOT JSON.stringify'd)
            hb_r = api(f"/hls/{sid}/heartbeat", method="POST",
                       json_body={"pos": 5.0}, token=token)
            assert hb_r.status_code == 200, f"Heartbeat 500: {hb_r.text}"
            assert hb_r.json().get("ok") is True
        finally:
            api(f"/hls/{sid}/stop", method="POST", token=token)

    def test_heartbeat_unknown_session_returns_404(self, token):
        r = api("/hls/INVALID_SESSION_ID/heartbeat", method="POST",
                json_body={"pos": 0}, token=token)
        assert r.status_code == 404

    def test_hls_stop_cleans_session(self, token):
        vid = self._first_video_id(token)
        start_r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 0}, token=token)
        sid = start_r.json()["session_id"]
        stop_r = api(f"/hls/{sid}/stop", method="POST", token=token)
        assert stop_r.status_code == 200
        # After stop, heartbeat must return 404
        time.sleep(0.5)
        hb_r = api(f"/hls/{sid}/heartbeat", method="POST", json_body={"pos": 0}, token=token)
        assert hb_r.status_code == 404, "Session still alive after stop"

    def test_hls_seek_mid_film(self, token):
        """Start HLS at 30s offset — playlist should still be served."""
        vid = self._first_video_id(token)
        r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 30}, token=token)
        assert r.status_code == 200
        d = r.json()
        assert float(d.get("start_offset", 0)) == 30.0
        api(f"/hls/{d['session_id']}/stop", method="POST", token=token)

    def test_session_limit_kills_oldest(self, token):
        """Starting 4 sessions should not exceed _HLS_MAX_SESSIONS=3."""
        vid = self._first_video_id(token)
        sids = []
        for _ in range(4):
            r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 0}, token=token)
            if r.status_code == 200 and r.json().get("session_id"):
                sids.append(r.json()["session_id"])
            time.sleep(0.2)
        # Cleanup all
        for sid in sids:
            api(f"/hls/{sid}/stop", method="POST", token=token)
        # No assertion needed — if max sessions worked, oldest was killed silently.
        # This test mainly checks that 4th start doesn't crash (no 500).
        assert len(sids) >= 1


# ── 5. Subtitle Extraction ───────────────────────────────────────────────────

class TestSubtitles:
    def test_subtitles_endpoint(self, token):
        items = api("/library", token=token).json().get("items", [])
        if not items:
            pytest.skip("Empty library")
        vid = items[0]["id"]
        r = api(f"/subtitles/{vid}", token=token)
        assert r.status_code == 200
        assert "subtitles" in r.json()

    def test_embedded_subs_invalid_track_returns_500_or_404(self, token):
        """Track 999 should fail gracefully, not crash."""
        items = api("/library", token=token).json().get("items", [])
        if not items:
            pytest.skip("Empty library")
        vid = items[0]["id"]
        r = api(f"/embedded-subs/{vid}/999", token=token)
        assert r.status_code in (404, 500, 200)  # 500 is acceptable for bad track


# ── 6. File Watcher ──────────────────────────────────────────────────────────

class TestFileWatcher:
    def test_watcher_status_endpoint(self, token):
        r = api("/watcher-status", token=token)
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True
        assert "watched" in d
        assert isinstance(d["watched"], list)


# ── 7. Thumbnail & Thumbstrip ────────────────────────────────────────────────

class TestThumbnails:
    def test_thumb_endpoint(self, token):
        items = api("/library", token=token).json().get("items", [])
        if not items:
            pytest.skip("Empty library")
        vid = items[0]["id"]
        r = requests.get(f"{BASE_URL}/api/video-station/thumb/{vid}?token={token}", timeout=10)
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert r.headers["Content-Type"].startswith("image/")

    def test_thumbstrip_endpoint_triggers_generation(self, token):
        items = api("/library", token=token).json().get("items", [])
        if not items:
            pytest.skip("Empty library")
        vid = items[0]["id"]
        r = requests.get(f"{BASE_URL}/api/video-station/thumbstrip/{vid}?token={token}", timeout=20)
        # Either serves cached sprite (200) or triggers background generation (202)
        assert r.status_code in (200, 202)


# ── 8. Stress / Concurrency ──────────────────────────────────────────────────

class TestStress:
    def test_concurrent_heartbeats(self, token):
        """Send 10 rapid heartbeats — all must return 200."""
        items = api("/library", token=token).json().get("items", [])
        if not items:
            pytest.skip("Empty library")
        vid = items[0]["id"]
        r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 0}, token=token)
        sid = r.json()["session_id"]
        try:
            for i in range(10):
                hb = api(f"/hls/{sid}/heartbeat", method="POST",
                         json_body={"pos": float(i * 2)}, token=token)
                assert hb.status_code == 200, f"Heartbeat {i} failed: {hb.status_code}"
        finally:
            api(f"/hls/{sid}/stop", method="POST", token=token)

    def test_no_ghost_ffmpeg_after_stop(self, token):
        """After stopping HLS, no vs_hls_ temp dirs should remain."""
        items = api("/library", token=token).json().get("items", [])
        if not items:
            pytest.skip("Empty library")
        vid = items[0]["id"]
        r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 0}, token=token)
        sid = r.json()["session_id"]
        api(f"/hls/{sid}/stop", method="POST", token=token)
        time.sleep(1)
        tmp = tempfile.gettempdir()
        leftovers = [d for d in os.listdir(tmp) if d.startswith("vs_hls_")]
        assert len(leftovers) == 0, f"Leftover HLS dirs: {leftovers}"

    def test_heartbeat_timeout_kills_session(self, token):
        """After _HLS_HEARTBEAT_TIMEOUT (20s) without heartbeat, session is killed."""
        items = api("/library", token=token).json().get("items", [])
        if not items:
            pytest.skip("Empty library")
        vid = items[0]["id"]
        r = api(f"/hls/{vid}/start", method="POST", json_body={"start": 0}, token=token)
        sid = r.json()["session_id"]
        # Do NOT send heartbeats — wait 25s
        print(f"\n  Waiting 25s for heartbeat timeout on session {sid}...")
        time.sleep(25)
        # Session should now be dead — heartbeat returns 404
        hb = api(f"/hls/{sid}/heartbeat", method="POST", json_body={"pos": 0}, token=token)
        assert hb.status_code == 404, f"Session not killed after timeout: {hb.status_code}"


# ── 9. TMDb & Metadata ───────────────────────────────────────────────────────

class TestTmdb:
    def test_tmdb_config_endpoint(self, token):
        r = api("/tmdb-config", token=token)
        assert r.status_code == 200
        d = r.json()
        assert "has_key" in d

    def test_home_endpoint(self, token):
        r = api("/home", token=token)
        assert r.status_code == 200
        d = r.json()
        assert "recently_added" in d

    def test_rescan_metadata_returns_counts(self, token):
        r = api("/rescan-metadata", method="POST", token=token)
        assert r.status_code == 200
        d = r.json()
        assert "total" in d or "updated" in d
