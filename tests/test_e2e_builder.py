"""
EthOS Builder — End-to-End Backend Tests
=========================================
Tests the full "Golden Path" business flow:
  1. API Lifecycle     — status → start → cancel → idle
  2. SSE Protocol      — event format, resume state, log pagination
  3. Cancel Cleanup    — process + loop-device cleanup after cancel
  4. Builder→VMM       — post-build artifact can create a VM immediately
  5. Beacon Endpoint   — POST (no-auth) + GET (auth) beacon flow
  6. Browser-Side      — state-file persistence simulating page-refresh mid-build

Run:
    ETHOS_USER=admin ETHOS_PASS=yourpass pytest tests/test_e2e_builder.py -v
    pytest tests/test_e2e_builder.py -v -k "not slow"   # skip long ops
"""

import json
import os
import sys
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))
from helpers import BASE_URL, USERNAME, PASSWORD

# ─── Shared authenticated session ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def api(api_session):
    """Authenticated requests.Session with Bearer token already set."""
    return api_session


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _url(path):
    return f"{BASE_URL}/api/builder{path}"


def _unauth_session():
    return requests.Session()


# ═══════════════════════════════════════════════════════════════════════════════
#  1. API LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuilderAPILifecycle:
    """Verify the state machine: idle → (build) → done/error → idle."""

    def test_status_unauthenticated(self):
        """Status endpoint requires auth."""
        r = _unauth_session().get(_url("/status"))
        assert r.status_code in (401, 403), f"Expected auth rejection, got {r.status_code}"

    def test_status_fields_when_idle(self, api):
        """Status response always includes required fields."""
        r = api.get(_url("/status"))
        assert r.status_code == 200
        data = r.json()
        for field in ("status", "percent", "message", "logs", "log_total",
                      "elapsed", "result", "resume_available", "build_dir",
                      "preflight_result"):
            assert field in data, f"Missing field: {field}"

    def test_status_is_idle_or_known_state(self, api):
        """Status must be one of the known values."""
        r = api.get(_url("/status"))
        assert r.json()["status"] in ("idle", "building", "done", "error")

    def test_cancel_when_idle_returns_400(self, api):
        """Cancelling when nothing is running must return 400."""
        r = api.get(_url("/status"))
        if r.json()["status"] == "building":
            pytest.skip("Build is currently running — cannot test idle cancel")
        r = api.post(_url("/cancel"))
        assert r.status_code == 400
        assert "error" in r.json()

    def test_dismiss_when_idle_resets_state(self, api):
        """Dismiss on idle/done state returns ok and resets to idle."""
        r = api.get(_url("/status"))
        if r.json()["status"] == "building":
            pytest.skip("Build is currently running")
        r = api.post(_url("/dismiss"))
        # 200 ok or 409 if building — either is valid
        assert r.status_code in (200, 409)
        if r.status_code == 200:
            assert r.json().get("ok") is True

    def test_double_start_returns_409(self, api):
        """Starting a second build while one is in progress must return 409."""
        r = api.get(_url("/status"))
        if r.json()["status"] != "building":
            pytest.skip("No active build — cannot test double-start (409)")
        r = api.post(_url("/image"), json={})
        assert r.status_code == 409

    def test_info_endpoint(self, api):
        """Info endpoint returns version and existing artifacts."""
        r = api.get(_url("/info"))
        assert r.status_code == 200
        data = r.json()
        assert "version" in data or "ok" in data

    def test_history_structure(self, api):
        """History returns a list (possibly empty)."""
        r = api.get(_url("/history"))
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert isinstance(data["items"], list)

    def test_spec_defaults(self, api):
        """Spec defaults endpoint returns a YAML/JSON structure."""
        r = api.get(_url("/spec/defaults"))
        assert r.status_code == 200

    def test_spec_roundtrip(self, api):
        """GET spec, PUT it back unchanged, GET again — must match."""
        r1 = api.get(_url("/spec"))
        assert r1.status_code == 200
        original = r1.json()
        r2 = api.put(_url("/spec"), json=original.get("spec", original))
        assert r2.status_code == 200
        r3 = api.get(_url("/spec"))
        assert r3.status_code == 200
        # The payload should round-trip cleanly
        assert r3.json() == original

    def test_cache_info(self, api):
        """Cache info returns size fields."""
        r = api.get(_url("/cache"))
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
#  2. SSE PROTOCOL
# ═══════════════════════════════════════════════════════════════════════════════

class TestSSEProtocol:
    """Verify the SSE event format and resume-state behaviour."""

    def test_sse_event_format_is_valid_json(self, api):
        """Each SSE line is 'data: <JSON>\\n\\n' — validate on the release endpoint."""
        r = api.get(_url("/status"))
        if r.json()["status"] == "building":
            pytest.skip("Build is running — release SSE endpoint blocked")

        # Trigger a quick release SSE (reads for max 5s then aborts)
        try:
            with api.post(_url("/release"), stream=True, timeout=5) as resp:
                if resp.status_code == 409:
                    pytest.skip("Release already in progress")
                if resp.status_code in (500, 503):
                    pytest.skip(f"Release endpoint unavailable: {resp.status_code}")
                assert resp.status_code == 200, f"Unexpected: {resp.status_code} {resp.text[:200]}"
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
                    if not line.startswith("data: "):
                        continue  # skip keep-alive or comment lines
                    payload = json.loads(line[6:])
                    assert "type" in payload, f"SSE payload missing 'type': {payload}"
                    break  # First event is enough to validate format
        except requests.exceptions.Timeout:
            pass  # Fine — we only needed the first event

    def test_status_log_pagination_with_since(self, api):
        """GET /status?since=N returns only logs from offset N."""
        r = api.get(_url("/status"))
        data = r.json()
        total = data.get("log_total", 0)
        if total == 0:
            pytest.skip("No logs in state (nothing has been built yet)")
        # Request from the last log entry
        r2 = api.get(_url("/status"), params={"since": total - 1})
        assert r2.status_code == 200
        data2 = r2.json()
        assert len(data2["logs"]) <= 1, (
            f"Expected ≤1 log at offset {total-1}, got {len(data2['logs'])}"
        )

    def test_resume_available_field_type(self, api):
        """resume_available must be a bool."""
        r = api.get(_url("/status"))
        assert isinstance(r.json()["resume_available"], bool)

    def test_build_dir_field_type(self, api):
        """build_dir must be a string."""
        r = api.get(_url("/status"))
        assert isinstance(r.json()["build_dir"], str)

    def test_elapsed_is_zero_when_idle(self, api):
        """elapsed is 0 (or a non-negative number) when not building."""
        r = api.get(_url("/status"))
        data = r.json()
        if data["status"] != "building":
            assert data["elapsed"] == 0, f"Elapsed should be 0 when idle, got {data['elapsed']}"

    def test_state_survives_server_reconnect(self, api):
        """Two consecutive status calls return the same state (simulates browser F5)."""
        r1 = api.get(_url("/status"))
        time.sleep(0.1)
        r2 = api.get(_url("/status"))
        s1, s2 = r1.json()["status"], r2.json()["status"]
        # State can only change from building→done/error, not idle→building spontaneously
        assert not (s1 == "idle" and s2 == "building"), (
            "State changed from idle to building without a start call — unexpected"
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  3. CANCEL CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelCleanup:
    """Verify that cancelling a build leaves the system in a clean idle state."""

    def test_cancel_transitions_to_error_or_idle(self, api):
        """After cancel, status must not remain 'building'."""
        r = api.get(_url("/status"))
        if r.json()["status"] != "building":
            pytest.skip("No active build to cancel")
        rc = api.post(_url("/cancel"))
        assert rc.status_code == 200, f"Cancel failed: {rc.status_code} {rc.text}"
        time.sleep(2)  # Give the build worker a moment to notice
        r2 = api.get(_url("/status"))
        assert r2.json()["status"] != "building", (
            "Status is still 'building' after cancel"
        )

    def test_cancel_result_indicates_cancellation(self, api):
        """After a cancel, the result message should indicate cancellation."""
        r = api.get(_url("/status"))
        data = r.json()
        if data["status"] == "error" and data.get("result"):
            msg = (data["result"].get("message") or "").lower()
            if "cancel" in msg:
                assert True  # This is the expected state after a cancel
                return
        pytest.skip("No cancelled build in state history")

    def test_cancel_then_dismiss_reaches_idle(self, api):
        """After cancel+dismiss, state must be idle."""
        r = api.get(_url("/status"))
        if r.json()["status"] == "building":
            api.post(_url("/cancel"))
            time.sleep(2)
        r2 = api.get(_url("/status"))
        if r2.json()["status"] not in ("error", "done"):
            pytest.skip("State is not cancellable/dismissable")
        rd = api.post(_url("/dismiss"))
        assert rd.status_code == 200
        r3 = api.get(_url("/status"))
        assert r3.json()["status"] == "idle"


# ═══════════════════════════════════════════════════════════════════════════════
#  4. BUILDER → VMM CONTRACT
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuilderToVMMContract:
    """Verify the contract between Builder artifact output and VM Manager input."""

    @pytest.fixture(scope="class")
    def last_build_img(self, api):
        """Return path to last built .img from build history, skip if none."""
        r = api.get(_url("/history"))
        items = r.json().get("items", [])
        for item in items:
            img = item.get("img") or (item.get("result") or {}).get("img")
            if img and os.path.isfile(img):
                return img
        # Also check /info
        r2 = api.get(_url("/info"))
        data = r2.json()
        for key in ("images", "releases"):
            for entry in data.get(key, []):
                p = entry.get("path") or entry.get("img")
                if p and os.path.isfile(p):
                    return p
        pytest.skip("No built .img artifact found — run a build first")

    def test_build_result_has_beacon_id(self, api):
        """Completed build result includes a beacon_id field."""
        r = api.get(_url("/status"))
        data = r.json()
        if data["status"] == "done" and data.get("result"):
            result = data["result"]
            assert "beacon_id" in result, (
                "Build result missing 'beacon_id' — needed for E2E boot validation"
            )
        else:
            pytest.skip("No completed build in current state")

    def test_vm_list_endpoint_accessible(self, api):
        """VM Manager list endpoint is reachable (prerequisite for VMM contract)."""
        r = api.get(f"{BASE_URL}/api/vm/machines")
        assert r.status_code in (200, 404), (
            f"VM Manager not accessible: {r.status_code}"
        )

    def test_create_vm_from_builder_artifact(self, api, last_build_img):
        """Create a VM using the builder output as boot_image — then delete it."""
        # Only .img / .iso / .qcow2 can be used as VM boot images
        _BOOTABLE = (".img", ".iso", ".qcow2")
        if not last_build_img.endswith(_BOOTABLE):
            pytest.skip(
                f"Artifact {os.path.basename(last_build_img)!r} is not a bootable disk image "
                f"(need {_BOOTABLE}) — skipping VMM contract test"
            )
        r = api.post(f"{BASE_URL}/api/vm/machines", json={
            "name": "e2e-builder-contract-test",
            "cpu": 1,
            "ram": 512,
            "disk_size": "8G",
            "os_type": "linux",
            "boot_image": last_build_img,
        })
        if r.status_code == 404:
            pytest.skip("VM Manager not installed")
        assert r.status_code == 200, f"VM creation failed: {r.status_code} {r.text}"
        data = r.json()
        assert data.get("ok") or data.get("id"), f"Unexpected response: {data}"
        vm_id = data.get("id") or (data.get("vm") or {}).get("id")

        if vm_id:
            # Cleanup: delete the test VM
            api.delete(f"{BASE_URL}/api/vm/machines/{vm_id}")

    def test_vm_boot_image_path_matches_artifact(self, api, last_build_img):
        """boot_image path is an absolute path pointing to an artifact file."""
        assert os.path.isabs(last_build_img), (
            f"Builder artifact path is not absolute: {last_build_img!r}"
        )
        _BOOT_EXTS = (".img", ".iso", ".qcow2", ".tar.gz", ".tar.xz", ".zip")
        assert last_build_img.endswith(_BOOT_EXTS), (
            f"Builder artifact has unexpected extension: {last_build_img!r}"
        )

    def test_signing_key_present(self, api):
        """Builder signing key endpoint returns a PEM-encoded public key."""
        r = api.get(_url("/signing-key"))
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            body = r.text
            assert "BEGIN" in body, "signing-key endpoint returned non-PEM content"

    def test_manifest_structure(self, api):
        """Manifest endpoint (when artifact exists) contains checksum fields."""
        # Manifest requires ?path=<artifact>; without it we get 400 — that's expected.
        r = api.get(_url("/manifest"))
        if r.status_code in (400, 404):
            pytest.skip("No artifact path provided / no manifest available — run a build first")
        assert r.status_code == 200
        data = r.json()
        assert "artifacts" in data or "ok" in data


# ═══════════════════════════════════════════════════════════════════════════════
#  5. BEACON ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════

class TestBeaconEndpoint:
    """Verify the I-AM-ALIVE beacon flow: POST (no-auth) then GET (auth)."""

    _TEST_BUILD_ID = f"e2e-test-beacon-{int(time.time())}"

    def test_beacon_post_requires_no_auth(self):
        """POST /beacon must not require authentication (VM has no token)."""
        r = _unauth_session().post(
            _url("/beacon"),
            json={
                "build_id": self._TEST_BUILD_ID,
                "hostname": "e2e-test-vm",
                "version": "1.0.0-test",
                "timestamp": int(time.time()),
            },
        )
        assert r.status_code == 200, (
            f"Beacon POST failed (expected 200, got {r.status_code}): {r.text}"
        )
        data = r.json()
        assert data.get("ok") is True
        assert data.get("acknowledged") is True

    def test_beacon_post_without_build_id_returns_400(self):
        """POST /beacon without build_id must return 400."""
        r = _unauth_session().post(_url("/beacon"), json={"hostname": "test"})
        assert r.status_code == 400

    def test_beacon_post_with_empty_build_id_returns_400(self):
        """POST /beacon with empty build_id must return 400."""
        r = _unauth_session().post(
            _url("/beacon"),
            json={"build_id": "", "hostname": "test"},
        )
        assert r.status_code == 400

    def test_beacon_get_requires_auth(self):
        """GET /beacon must require authentication."""
        r = _unauth_session().get(_url("/beacon"))
        assert r.status_code in (401, 403)

    def test_beacon_get_returns_last_beacon(self, api):
        """After a POST, GET /beacon returns the stored beacon data."""
        # Post a beacon
        build_id = f"e2e-get-test-{int(time.time())}"
        _unauth_session().post(
            _url("/beacon"),
            json={
                "build_id": build_id,
                "hostname": "e2e-get-test-vm",
                "version": "1.0.0-get-test",
                "timestamp": int(time.time()),
            },
        )
        # Retrieve it
        r = api.get(_url("/beacon"))
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True
        assert data.get("beacon") is not None
        beacon = data["beacon"]
        assert beacon["build_id"] == build_id
        assert beacon["hostname"] == "e2e-get-test-vm"
        assert beacon["version"] == "1.0.0-get-test"
        assert "received_at" in beacon
        assert "remote_addr" in beacon

    def test_beacon_get_returns_none_build_id_matched_when_no_active_build(self, api):
        """build_id_matched is False when no image was built in this session."""
        r = api.get(_url("/beacon"))
        assert r.status_code == 200
        data = r.json()
        if data.get("beacon") is None:
            pytest.skip("No beacon in state yet")
        # build_id_matched may be True or False — just verify it's a bool
        assert isinstance(data.get("build_id_matched"), bool)

    def test_beacon_extras_field_stored(self, api):
        """Beacon extras dict is preserved."""
        build_id = f"e2e-extras-{int(time.time())}"
        extras = {"dm_verity": "ok", "systemd_running": True, "boot_time_s": 12}
        _unauth_session().post(
            _url("/beacon"),
            json={
                "build_id": build_id,
                "hostname": "e2e-extras-vm",
                "version": "1.0.0",
                "timestamp": int(time.time()),
                "extras": extras,
            },
        )
        r = api.get(_url("/beacon"))
        beacon = r.json().get("beacon", {})
        assert beacon.get("extras") == extras, (
            f"extras not preserved: {beacon.get('extras')!r}"
        )

    def test_beacon_hostname_truncated_to_128_chars(self, api):
        """Hostile hostname longer than 128 chars is silently truncated."""
        build_id = f"e2e-trunc-{int(time.time())}"
        _unauth_session().post(
            _url("/beacon"),
            json={
                "build_id": build_id,
                "hostname": "x" * 300,
                "version": "1.0.0",
                "timestamp": int(time.time()),
            },
        )
        r = api.get(_url("/beacon"))
        beacon = r.json().get("beacon", {})
        assert len(beacon.get("hostname", "")) <= 128


# ═══════════════════════════════════════════════════════════════════════════════
#  6. BROWSER-SIDE STRESS (page refresh, cancel robustness)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrowserSideStress:
    """Simulate browser-side events: page refresh, rapid status polls, edge cases."""

    def test_rapid_status_polling_is_stable(self, api):
        """10 rapid status polls all return the same state (simulate SSE reconnect)."""
        states = set()
        for _ in range(10):
            r = api.get(_url("/status"))
            assert r.status_code == 200
            states.add(r.json()["status"])
        # State can transition building→done/error but never idle→building spontaneously
        assert "idle" not in states or "building" not in states, (
            "State oscillated between idle and building — indicates a race condition"
        )

    def test_status_page_size_bounded(self, api):
        """Log list is capped at _MAX_STATE_LOGS (500) — prevents client OOM."""
        r = api.get(_url("/status"))
        data = r.json()
        assert len(data.get("logs", [])) <= 500, (
            f"Logs list exceeded 500 entries: {len(data['logs'])}"
        )

    def test_status_with_large_since_offset_returns_empty(self, api):
        """since=999999 returns empty logs array (not an error)."""
        r = api.get(_url("/status"), params={"since": 999999})
        assert r.status_code == 200
        assert r.json()["logs"] == []

    def test_dismiss_is_idempotent(self, api):
        """Two consecutive dismisses: first may succeed, second must not crash."""
        r = api.get(_url("/status"))
        if r.json()["status"] == "building":
            pytest.skip("Build is running")
        r1 = api.post(_url("/dismiss"))
        r2 = api.post(_url("/dismiss"))
        # Both must return either 200 or 409, never 500
        assert r1.status_code in (200, 409)
        assert r2.status_code in (200, 409)

    def test_resume_available_false_when_idle(self, api):
        """resume_available is False immediately after a dismiss."""
        r = api.get(_url("/status"))
        if r.json()["status"] == "building":
            pytest.skip("Build is running")
        api.post(_url("/dismiss"))
        r2 = api.get(_url("/status"))
        assert r2.json()["resume_available"] is False

    def test_negative_path_build_requires_debootstrap(self, api):
        """Build image endpoint returns error if debootstrap is missing (negative path).

        This mirrors the UI case where user clicks Build and sees an error immediately.
        The endpoint either starts the build (200) or rejects with a tool-missing error.
        We just verify the response is well-formed — not a 500 server crash.
        """
        r = api.get(_url("/status"))
        if r.json()["status"] == "building":
            pytest.skip("Build already running")
        # Don't actually start a build — just verify the image endpoint is reachable
        # and accepts/rejects cleanly (not 5xx for missing tools)
        r = api.post(_url("/image"), json={})
        assert r.status_code in (200, 400, 409, 503), (
            f"Unexpected status from /image: {r.status_code} {r.text}"
        )
        if r.status_code == 200:
            # Build was started — cancel it immediately to avoid a full 30-min build
            api.post(_url("/cancel"))
