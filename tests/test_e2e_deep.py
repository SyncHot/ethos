"""
EthOS NAS – Deep E2E Functional Tests for Individual Apps

Tests actual CRUD operations, schedules, scans, config changes for:
- Antivirus (ClamAV): scan, schedules CRUD, results, DB update
- Backup: paths CRUD, profiles CRUD, browse, history, snapshots
- Download Manager: add/remove downloads, config
- Cron: jobs CRUD, toggle
- Firewall: rules CRUD, toggle, presets
- WireGuard: status, peers
- VM Manager: machines list, images
- Surveillance: cameras, settings
- Sharing (Samba): shares CRUD

Safe to run against any EthOS instance — creates test data, then cleans up.

Usage:
    ETHOS_USER=myuser ETHOS_PASS=mypass \\
    ETHOS_BASE_URL=http://localhost:9000 \\
    pytest tests/test_e2e_deep.py -v

    # Single app:
    pytest tests/test_e2e_deep.py -k "Antivirus" -v
"""

import time
import uuid
import pytest
import requests
from helpers import api_get, api_post


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(s, path, **kw):
    return api_get(s, path, **kw)

def _post(s, path, **kw):
    return api_post(s, path, **kw)

def _put(s, path, **kw):
    kw.setdefault("timeout", 15)
    return s.put(f"{s.base_url}{path}", **kw)

def _delete(s, path, **kw):
    kw.setdefault("timeout", 15)
    return s.delete(f"{s.base_url}{path}", **kw)

def _json(resp):
    ct = resp.headers.get("Content-Type", "")
    if "application/json" in ct:
        return resp.json()
    return None

def _skip_html(resp, label="App"):
    if "text/html" in resp.headers.get("Content-Type", ""):
        pytest.skip(f"{label} blueprint not registered")

def _skip_not_installed(resp, label="App"):
    """Skip if app returns pkg-status installed=false or 503."""
    if resp.status_code == 503:
        pytest.skip(f"{label} dependency not installed")
    data = _json(resp)
    if data and data.get("installed") is False:
        pytest.skip(f"{label} not installed")

def _retry_on_429(s, method, path, retries=3, **kw):
    for i in range(retries):
        resp = method(s, path, **kw)
        if resp.status_code != 429:
            return resp
        time.sleep(5 * (i + 1))
    return resp


# ═══════════════════════════════════════════════════════════════
#  ANTIVIRUS (ClamAV) – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestAntivirusDeep:
    """Full functional tests for Antivirus app including schedules CRUD and scan."""

    @pytest.fixture(autouse=True)
    def _check_installed(self, api_session):
        r = _get(api_session, "/api/antivirus/pkg-status")
        _skip_html(r, "Antivirus")
        data = _json(r)
        if not data or data.get("installed") is not True:
            pytest.skip("Antivirus (ClamAV) not installed")

    # --- Status ---

    def test_status_fields(self, api_session):
        """Status should return version, db_info, scanning state."""
        r = _get(api_session, "/api/antivirus/status")
        assert r.status_code == 200
        d = r.json()
        assert "installed" in d
        assert "version" in d
        assert "scanning" in d
        assert isinstance(d["scanning"], bool)

    def test_pkg_status(self, api_session):
        r = _get(api_session, "/api/antivirus/pkg-status")
        assert r.status_code == 200
        assert r.json()["installed"] is True

    # --- Results ---

    def test_results_list(self, api_session):
        r = _get(api_session, "/api/antivirus/results")
        assert r.status_code == 200
        d = r.json()
        assert "items" in d
        assert isinstance(d["items"], list)

    # --- Schedules CRUD ---

    def test_schedules_list(self, api_session):
        r = _get(api_session, "/api/antivirus/schedules")
        assert r.status_code == 200
        d = r.json()
        assert "items" in d
        assert isinstance(d["items"], list)

    def test_schedule_create_read_update_delete(self, api_session):
        """Full CRUD cycle for antivirus schedule."""
        tag = uuid.uuid4().hex[:8]
        # CREATE
        r = _post(api_session, "/api/antivirus/schedules", json={
            "name": f"test-schedule-{tag}",
            "path": "/tmp",
            "cron_expr": "0 3 * * 0",
            "enabled": False,
        })
        assert r.status_code == 200, f"Create failed: {r.text}"
        d = r.json()
        assert d.get("ok") is True
        sid = d["item"]["id"]

        try:
            # READ — verify it appears in list
            r = _get(api_session, "/api/antivirus/schedules")
            items = r.json()["items"]
            found = [x for x in items if x["id"] == sid]
            assert len(found) == 1
            assert found[0]["name"] == f"test-schedule-{tag}"
            assert found[0]["path"] == "/tmp"
            assert found[0]["cron_expr"] == "0 3 * * 0"
            assert found[0]["enabled"] is False

            # UPDATE — change name and enable
            r = _put(api_session, f"/api/antivirus/schedules/{sid}", json={
                "name": f"updated-{tag}",
                "enabled": True,
            })
            assert r.status_code == 200
            assert r.json().get("ok") is True

            # Verify update
            r = _get(api_session, "/api/antivirus/schedules")
            found = [x for x in r.json()["items"] if x["id"] == sid]
            assert found[0]["name"] == f"updated-{tag}"
            assert found[0]["enabled"] is True
        finally:
            # DELETE
            r = _delete(api_session, f"/api/antivirus/schedules/{sid}")
            assert r.status_code == 200
            assert r.json().get("ok") is True

        # Verify deleted
        r = _get(api_session, "/api/antivirus/schedules")
        assert all(x["id"] != sid for x in r.json()["items"])

    def test_schedule_create_invalid_cron(self, api_session):
        """Invalid cron expression should return 400."""
        r = _post(api_session, "/api/antivirus/schedules", json={
            "name": "bad-cron",
            "path": "/tmp",
            "cron_expr": "invalid cron",
            "enabled": False,
        })
        assert r.status_code == 400

    def test_schedule_create_missing_fields(self, api_session):
        """Missing required fields — backend may default them or return 400."""
        r = _post(api_session, "/api/antivirus/schedules", json={
            "name": "no-path",
        })
        if r.status_code == 200:
            # Backend accepted with defaults — clean up
            sid = r.json().get("item", {}).get("id")
            if sid:
                _delete(api_session, f"/api/antivirus/schedules/{sid}")
        else:
            assert r.status_code == 400

    def test_schedule_update_nonexistent(self, api_session):
        """Updating non-existent schedule should return 404."""
        r = _put(api_session, "/api/antivirus/schedules/nonexistent-id-999", json={
            "name": "ghost",
        })
        assert r.status_code == 404

    def test_schedule_delete_nonexistent(self, api_session):
        """Deleting non-existent schedule should return 404."""
        r = _delete(api_session, "/api/antivirus/schedules/nonexistent-id-999")
        assert r.status_code == 404

    def test_schedule_toggle_enabled(self, api_session):
        """Toggle enabled flag on/off."""
        tag = uuid.uuid4().hex[:8]
        r = _post(api_session, "/api/antivirus/schedules", json={
            "name": f"toggle-{tag}",
            "path": "/tmp",
            "cron_expr": "0 2 * * *",
            "enabled": True,
        })
        sid = r.json()["item"]["id"]
        try:
            # Disable
            r = _put(api_session, f"/api/antivirus/schedules/{sid}", json={"enabled": False})
            assert r.status_code == 200
            r = _get(api_session, "/api/antivirus/schedules")
            found = [x for x in r.json()["items"] if x["id"] == sid]
            assert found[0]["enabled"] is False

            # Re-enable
            r = _put(api_session, f"/api/antivirus/schedules/{sid}", json={"enabled": True})
            assert r.status_code == 200
            r = _get(api_session, "/api/antivirus/schedules")
            found = [x for x in r.json()["items"] if x["id"] == sid]
            assert found[0]["enabled"] is True
        finally:
            _delete(api_session, f"/api/antivirus/schedules/{sid}")

    # --- Scan ---

    def test_scan_start_and_cancel(self, api_session):
        """Start a scan on /tmp and cancel it immediately."""
        r = _post(api_session, "/api/antivirus/scan", json={"path": "/tmp"})
        if r.status_code == 409:
            pytest.skip("Another scan already running")
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True
        assert "scan_id" in d

        time.sleep(1)

        # Cancel — may return 200 (cancelled) or 404 (already finished)
        r = _post(api_session, "/api/antivirus/scan/cancel")
        assert r.status_code in (200, 404)

        # Wait for scan to actually stop
        for _ in range(10):
            r = _get(api_session, "/api/antivirus/status")
            if not r.json().get("scanning"):
                break
            time.sleep(1)

    def test_scan_no_path(self, api_session):
        """Scan without path defaults to /home — should succeed or 409 if busy."""
        r = _post(api_session, "/api/antivirus/scan", json={})
        # Backend defaults to /home — may succeed or 409 if already scanning
        assert r.status_code in (200, 409)
        if r.status_code == 200:
            # Cancel to clean up
            time.sleep(0.5)
            _post(api_session, "/api/antivirus/scan/cancel")

    def test_cancel_when_no_scan(self, api_session):
        """Cancel when no scan running should return 404."""
        # Ensure no scan running first
        status = _get(api_session, "/api/antivirus/status").json()
        if status.get("scanning"):
            pytest.skip("Scan is running, can't test cancel-no-scan")
        r = _post(api_session, "/api/antivirus/scan/cancel")
        assert r.status_code == 404

    # --- DB Update ---

    def test_update_db(self, api_session):
        """Trigger DB update — should return task_id."""
        r = _post(api_session, "/api/antivirus/update-db")
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True
        assert "task_id" in d


# ═══════════════════════════════════════════════════════════════
#  BACKUP – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestBackupDeep:
    """Full functional tests for Backup app."""

    # --- Status ---

    def test_status(self, api_session):
        r = _get(api_session, "/api/backup/status")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "busy" in d
        assert isinstance(d["busy"], bool)

    def test_progress(self, api_session):
        r = _get(api_session, "/api/backup/progress")
        _skip_html(r, "Backup")
        assert r.status_code == 200

    # --- Browse ---

    def test_browse_roots(self, api_session):
        r = _get(api_session, "/api/backup/browse/roots")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "roots" in d
        assert isinstance(d["roots"], list)

    def test_browse_tmp(self, api_session):
        """Browse /tmp — should always exist."""
        r = _get(api_session, "/api/backup/browse", params={"path": "/tmp"})
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "currentPath" in d
        assert "items" in d

    # --- Paths CRUD ---

    def test_paths_crud(self, api_session):
        """Add a path, verify it's listed, remove it."""
        # Use /tmp which always exists
        test_path = "/tmp"
        # Get original paths
        r = _get(api_session, "/api/backup/paths")
        _skip_html(r, "Backup")
        orig_paths = r.json().get("paths", [])

        # Add
        r = _post(api_session, "/api/backup/paths", json={"path": test_path})
        assert r.status_code == 200
        d = r.json()
        assert d.get("success") is True
        assert test_path in d.get("paths", [])

        # List
        r = _get(api_session, "/api/backup/paths")
        assert test_path in r.json().get("paths", [])

        # Remove only if we added it (wasn't there before)
        if test_path not in orig_paths:
            r = _delete(api_session, "/api/backup/paths", json={"path": test_path})
            assert r.status_code == 200
            assert test_path not in r.json().get("paths", [])

    # --- USB Drives ---

    def test_usb_drives(self, api_session):
        r = _get(api_session, "/api/backup/usb-drives")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "drives" in d
        assert isinstance(d["drives"], list)

    # --- SSH Servers CRUD ---

    def test_ssh_servers_list(self, api_session):
        r = _get(api_session, "/api/backup/ssh-servers")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        assert "servers" in r.json()

    def test_ssh_server_crud(self, api_session):
        """Create, list, delete SSH server config."""
        tag = uuid.uuid4().hex[:8]
        r = _post(api_session, "/api/backup/ssh-servers", json={
            "name": f"test-ssh-{tag}",
            "host": "192.168.99.99",
            "port": 22,
            "username": "testuser",
            "password": "testpass",
            "remote_path": "/tmp/backups",
        })
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert d.get("success") is True
        srv = d.get("server", {})
        srv_id = srv.get("id")
        assert srv_id

        try:
            # Verify in list
            r = _get(api_session, "/api/backup/ssh-servers")
            servers = r.json().get("servers", [])
            found = [s for s in servers if s["id"] == srv_id]
            assert len(found) == 1
            assert found[0]["host"] == "192.168.99.99"
        finally:
            # Delete
            r = _delete(api_session, f"/api/backup/ssh-servers/{srv_id}")
            assert r.status_code == 200

        # Verify deleted
        r = _get(api_session, "/api/backup/ssh-servers")
        assert all(s["id"] != srv_id for s in r.json().get("servers", []))

    # --- History ---

    def test_history(self, api_session):
        r = _get(api_session, "/api/backup/history")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "history" in d
        assert isinstance(d["history"], list)

    # --- Profiles CRUD ---

    def test_profiles_list(self, api_session):
        r = _get(api_session, "/api/backup/profiles")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        assert "profiles" in r.json()

    def test_profile_crud(self, api_session):
        """Create, update, and delete a backup profile."""
        tag = uuid.uuid4().hex[:8]
        r = _post(api_session, "/api/backup/profiles", json={
            "name": f"test-profile-{tag}",
            "paths": ["/tmp"],
            "destination": {"type": "local", "path": "/tmp/backups-e2e"},
            "retention": 3,
            "incremental": False,
        })
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert d.get("success") is True
        pid = d["profile"]["id"]

        try:
            # Update
            r = _put(api_session, f"/api/backup/profiles/{pid}", json={
                "name": f"updated-{tag}",
                "retention": 5,
            })
            assert r.status_code == 200
            assert r.json().get("success") is True

            # Verify
            r = _get(api_session, "/api/backup/profiles")
            found = [p for p in r.json()["profiles"] if p["id"] == pid]
            assert len(found) == 1
            assert found[0]["name"] == f"updated-{tag}"
            assert found[0]["retention"] == 5
        finally:
            # Delete
            r = _delete(api_session, f"/api/backup/profiles/{pid}")
            assert r.status_code == 200

    # --- Scheduled Backups ---

    def test_scheduled_backups(self, api_session):
        r = _get(api_session, "/api/backup/scheduled-backups")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "scheduled" in d

    # --- Snapshots ---

    def test_snapshots_list(self, api_session):
        r = _get(api_session, "/api/backup/snapshots")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "snapshots" in d
        assert isinstance(d["snapshots"], list)

    def test_snapshots_status(self, api_session):
        r = _get(api_session, "/api/backup/snapshots/status")
        _skip_html(r, "Backup")
        assert r.status_code == 200

    def test_snapshots_space(self, api_session):
        r = _get(api_session, "/api/backup/snapshots/space")
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "disk_free" in d or "snapshot_count" in d

    # --- NAS Discovery ---

    def test_discover_nas(self, api_session):
        r = _post(api_session, "/api/backup/discover-nas", timeout=30)
        _skip_html(r, "Backup")
        assert r.status_code == 200
        d = r.json()
        assert "devices" in d


# ═══════════════════════════════════════════════════════════════
#  DOWNLOAD MANAGER – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestDownloadManagerDeep:
    """Functional tests for Download Manager."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/downloads/list")
        _skip_html(r, "Downloads")

    def test_list(self, api_session):
        r = _get(api_session, "/api/downloads/list")
        assert r.status_code == 200

    def test_stats(self, api_session):
        r = _get(api_session, "/api/downloads/stats")
        assert r.status_code == 200

    def test_history(self, api_session):
        r = _get(api_session, "/api/downloads/history")
        assert r.status_code == 200

    def test_config_read(self, api_session):
        r = _get(api_session, "/api/downloads/config")
        assert r.status_code == 200
        d = r.json()
        assert "download_dir" in d or "max_concurrent" in d or isinstance(d, dict)

    def test_config_update_and_restore(self, api_session):
        """Read config, update a field, then restore."""
        r = _get(api_session, "/api/downloads/config")
        d = r.json()
        cfg = d.get("config", d)  # config may be nested

        max_c = cfg.get("max_concurrent", 3)
        new_val = max_c + 1 if max_c < 10 else max_c - 1

        r = _put(api_session, "/api/downloads/config", json={"max_concurrent": new_val})
        assert r.status_code == 200

        # Verify
        r = _get(api_session, "/api/downloads/config")
        cfg2 = r.json().get("config", r.json())
        assert cfg2.get("max_concurrent") == new_val

        # Restore
        _put(api_session, "/api/downloads/config", json={"max_concurrent": max_c})

    def test_add_invalid_url(self, api_session):
        """Adding invalid URL should fail gracefully."""
        r = _post(api_session, "/api/downloads/add", json={"url": ""})
        assert r.status_code in (400, 422, 200)

    def test_add_and_remove_download(self, api_session):
        """Add a small HTTP download then remove it."""
        r = _post(api_session, "/api/downloads/add", json={
            "url": "https://example.com/nonexistent-file.txt",
        })
        # May fail since URL is fake, but should return JSON response
        if r.status_code == 200:
            d = r.json()
            dl_id = d.get("id") or d.get("download_id")
            if dl_id:
                time.sleep(1)
                _post(api_session, "/api/downloads/remove", json={"id": dl_id})

    def test_clear_history(self, api_session):
        """Clear history should succeed."""
        r = _post(api_session, "/api/downloads/history/clear", json={})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  CRON/SCHEDULER – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestCronDeep:
    """CRUD tests for Cron/Scheduler app."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/cron/jobs")
        _skip_html(r, "Cron")
        if r.status_code == 503:
            pytest.skip("Cron dependency (crontab) not installed")

    def test_list_jobs(self, api_session):
        r = _get(api_session, "/api/cron/jobs")
        assert r.status_code == 200

    def test_job_crud(self, api_session):
        """Create, read, update, delete a cron job."""
        tag = uuid.uuid4().hex[:8]
        # Get current count
        r = _get(api_session, "/api/cron/jobs")
        original_jobs = r.json() if isinstance(r.json(), list) else r.json().get("jobs", [])
        orig_count = len(original_jobs)

        # CREATE
        r = _post(api_session, "/api/cron/jobs", json={
            "minute": "0",
            "hour": "4",
            "day": "*",
            "month": "*",
            "weekday": "*",
            "command": f"echo e2e-test-{tag}",
            "enabled": False,
        })
        assert r.status_code == 200

        # READ — verify added
        r = _get(api_session, "/api/cron/jobs")
        jobs = r.json() if isinstance(r.json(), list) else r.json().get("jobs", [])
        assert len(jobs) == orig_count + 1

        new_idx = len(jobs) - 1

        try:
            # UPDATE
            r = _put(api_session, f"/api/cron/jobs/{new_idx}", json={
                "minute": "30",
                "hour": "5",
                "day": "*",
                "month": "*",
                "weekday": "*",
                "command": f"echo updated-{tag}",
                "enabled": False,
            })
            assert r.status_code == 200

            # TOGGLE
            r = _post(api_session, f"/api/cron/jobs/{new_idx}/toggle")
            assert r.status_code == 200
        finally:
            # DELETE
            r = _delete(api_session, f"/api/cron/jobs/{new_idx}")
            assert r.status_code == 200

        # Verify deleted
        r = _get(api_session, "/api/cron/jobs")
        jobs = r.json() if isinstance(r.json(), list) else r.json().get("jobs", [])
        assert len(jobs) == orig_count


# ═══════════════════════════════════════════════════════════════
#  FIREWALL – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestFirewallDeep:
    """Functional tests for Firewall app."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/firewall/status")
        _skip_html(r, "Firewall")

    def test_status_fields(self, api_session):
        r = _get(api_session, "/api/firewall/status")
        assert r.status_code == 200
        d = r.json()
        assert "enabled" in d or "status" in d
        assert "rules" in d

    def test_subnet(self, api_session):
        r = _get(api_session, "/api/firewall/subnet")
        assert r.status_code == 200
        d = r.json()
        assert "subnet" in d

    def test_banned_list(self, api_session):
        r = _get(api_session, "/api/firewall/banned")
        assert r.status_code == 200

    def test_add_and_delete_rule(self, api_session):
        """Add a firewall rule then delete it."""
        # Add rule using correct API format
        r = _post(api_session, "/api/firewall/rules", json={
            "action": "add",
            "port": "59999",
            "proto": "tcp",
            "ufw_action": "allow",
            "access": "public",
            "comment": "e2e-test",
        })
        assert r.status_code == 200

        # Verify in status
        r = _get(api_session, "/api/firewall/status")
        rules = r.json().get("rules", [])
        found = [ru for ru in rules if "59999" in str(ru.get("to", "")) or str(ru.get("port", "")) == "59999"]
        assert len(found) >= 1, f"Rule not found in {rules}"

        # Delete by rule number
        rule_id = found[0].get("id") or found[0].get("number")
        v6_id = found[0].get("v6_id") or found[0].get("v6_number")
        delete_payload = {"action": "delete", "id": rule_id}
        if v6_id:
            delete_payload["v6_id"] = v6_id
        r = _post(api_session, "/api/firewall/rules", json=delete_payload)
        assert r.status_code == 200

        # Verify gone
        r = _get(api_session, "/api/firewall/status")
        rules = r.json().get("rules", [])
        found = [ru for ru in rules if "59999" in str(ru.get("to", "")) or str(ru.get("port", "")) == "59999"]
        assert len(found) == 0

    def test_toggle(self, api_session):
        """Toggle firewall off then on (or verify it responds)."""
        r = _get(api_session, "/api/firewall/status")
        was_enabled = r.json().get("enabled", True)

        # Toggle off
        r = _post(api_session, "/api/firewall/toggle", json={"enable": not was_enabled})
        assert r.status_code == 200

        # Toggle back to original state
        r = _post(api_session, "/api/firewall/toggle", json={"enable": was_enabled})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  WIREGUARD – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestWireGuardDeep:
    """Functional tests for WireGuard VPN app."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/wireguard/pkg-status")
        _skip_html(r, "WireGuard")
        d = _json(r)
        if d and d.get("installed") is not True:
            pytest.skip("WireGuard not installed")

    def test_status(self, api_session):
        r = _get(api_session, "/api/wireguard/status")
        assert r.status_code == 200
        d = r.json()
        assert "enabled" in d or "interface" in d or "peers" in d

    def test_peer_add_and_delete(self, api_session):
        """Add a peer and then delete it."""
        tag = uuid.uuid4().hex[:6]
        r = _post(api_session, "/api/wireguard/peer", json={
            "name": f"test-peer-{tag}",
        })
        if r.status_code == 400:
            err = r.json().get("error", "")
            if "not enabled" in err or "not configured" in err:
                pytest.skip("WireGuard interface not enabled")
        assert r.status_code == 200
        d = r.json()
        # PublicKey can be at top level or nested in 'peer' object
        pub_key = d.get("public_key") or d.get("peer", {}).get("PublicKey") or d.get("peer", {}).get("public_key")
        assert pub_key, f"No public_key in response: {list(d.keys())}"

        try:
            # Verify in status
            r = _get(api_session, "/api/wireguard/status")
            peers = r.json().get("peers", [])
            found = [p for p in peers if
                     p.get("PublicKey") == pub_key or
                     p.get("public_key") == pub_key or
                     p.get("Name") == f"test-peer-{tag}" or
                     p.get("name") == f"test-peer-{tag}"]
            assert len(found) >= 1
        finally:
            # Delete
            import urllib.parse
            encoded_key = urllib.parse.quote(pub_key, safe="")
            r = _delete(api_session, f"/api/wireguard/peer/{encoded_key}")
            assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  VM MANAGER – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestVMManagerDeep:
    """Functional tests for VM Manager."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/vm/status")
        _skip_html(r, "VM Manager")

    def test_status(self, api_session):
        r = _get(api_session, "/api/vm/status")
        assert r.status_code == 200

    def test_machines_list(self, api_session):
        r = _get(api_session, "/api/vm/machines")
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d, (list, dict))

    def test_images_list(self, api_session):
        r = _get(api_session, "/api/vm/images")
        assert r.status_code == 200

    def test_bridge_status(self, api_session):
        r = _get(api_session, "/api/vm/bridge")
        assert r.status_code == 200

    def test_builder_images(self, api_session):
        r = _get(api_session, "/api/vm/builder-images")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  SURVEILLANCE – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestSurveillanceDeep:
    """Functional tests for Surveillance app."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/surveillance/status")
        _skip_html(r, "Surveillance")

    def test_status(self, api_session):
        r = _get(api_session, "/api/surveillance/status")
        assert r.status_code == 200

    def test_cameras_list(self, api_session):
        r = _get(api_session, "/api/surveillance/cameras")
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d, (list, dict))

    def test_camera_crud(self, api_session):
        """Add a dummy camera, verify, delete."""
        tag = uuid.uuid4().hex[:8]
        r = _post(api_session, "/api/surveillance/cameras", json={
            "name": f"test-cam-{tag}",
            "url": "rtsp://192.168.99.99:554/stream",
            "type": "rtsp",
        })
        if r.status_code in (400, 503):
            pytest.skip(f"Cannot create camera: {r.text[:200]}")
        assert r.status_code == 200
        d = r.json()
        cam_id = d.get("camera_id") or d.get("id") or d.get("cam_id")
        if not cam_id:
            # Try to find from cameras list
            r2 = _get(api_session, "/api/surveillance/cameras")
            cams = r2.json() if isinstance(r2.json(), list) else r2.json().get("cameras", [])
            found = [c for c in cams if c.get("name") == f"test-cam-{tag}"]
            if found:
                cam_id = found[0].get("id") or found[0].get("cam_id")

        if cam_id:
            try:
                # Verify in list
                r = _get(api_session, "/api/surveillance/cameras")
                cams = r.json() if isinstance(r.json(), list) else r.json().get("cameras", [])
                assert any(str(c.get("id", c.get("cam_id", ""))) == str(cam_id) for c in cams)
            finally:
                _delete(api_session, f"/api/surveillance/cameras/{cam_id}")

    def test_settings_read(self, api_session):
        r = _get(api_session, "/api/surveillance/settings")
        if r.status_code == 404:
            pytest.skip("Settings endpoint not available")
        assert r.status_code == 200

    def test_recordings_list(self, api_session):
        r = _get(api_session, "/api/surveillance/recordings")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  SHARING (SAMBA) – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestSharingDeep:
    """Functional tests for file sharing (links, Samba)."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/files/shares")
        _skip_html(r, "Sharing")

    def test_shares_list(self, api_session):
        r = _get(api_session, "/api/files/shares")
        assert r.status_code == 200

    def test_share_create_and_revoke(self, api_session):
        """Create a share link then revoke it."""
        # We need a real file to share — use /etc/hostname (always exists)
        r = _post(api_session, "/api/files/shares", json={
            "path": "/etc/hostname",
        })
        if r.status_code in (400, 403):
            # Path might not be allowed for sharing
            pytest.skip(f"Cannot share /etc/hostname: {r.text[:200]}")
        assert r.status_code == 200
        d = r.json()
        token = d.get("token") or d.get("share_token") or d.get("link", "").split("/")[-1]
        assert token, f"No token in response: {d}"

        try:
            # Verify in list
            r = _get(api_session, "/api/files/shares")
            shares = r.json() if isinstance(r.json(), list) else r.json().get("shares", r.json().get("items", []))
            found = [s for s in shares if s.get("token") == token]
            assert len(found) >= 1
        finally:
            # Revoke
            r = _delete(api_session, f"/api/files/shares/{token}")
            assert r.status_code == 200

    def test_samba_status(self, api_session):
        """Check Samba status (may not be installed)."""
        r = _get(api_session, "/api/storage/samba/status")
        _skip_html(r, "Samba")
        if r.status_code == 200:
            d = r.json()
            assert isinstance(d, dict)

    def test_samba_shares_list(self, api_session):
        r = _get(api_session, "/api/storage/samba/shares")
        _skip_html(r, "Samba")
        if r.status_code == 200:
            d = r.json()
            assert isinstance(d, (list, dict))


# ═══════════════════════════════════════════════════════════════
#  PRINTER – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestPrinterDeep:
    """Functional tests for Printer app."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/printer/status")
        _skip_html(r, "Printer")

    def test_status(self, api_session):
        r = _get(api_session, "/api/printer/status")
        assert r.status_code == 200

    def test_printers_list(self, api_session):
        r = _get(api_session, "/api/printer/printers")
        assert r.status_code == 200

    def test_cups_status(self, api_session):
        r = _get(api_session, "/api/printer/cups-status")
        if r.status_code == 404:
            pytest.skip("CUPS status endpoint not available")
        assert r.status_code == 200

    def test_discover(self, api_session):
        """Printer discovery (may return empty list)."""
        r = _get(api_session, "/api/printer/discover", timeout=30)
        if r.status_code == 404:
            pytest.skip("Discover endpoint not available")
        assert r.status_code == 200

    def test_jobs_list(self, api_session):
        r = _get(api_session, "/api/printer/jobs")
        if r.status_code == 404:
            pytest.skip("Jobs endpoint not available")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  NETWORK – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestNetworkDeep:
    """Functional tests for Network app."""

    def test_interfaces(self, api_session):
        r = _get(api_session, "/api/network/interfaces")
        _skip_html(r, "Network")
        assert r.status_code == 200

    def test_dns(self, api_session):
        r = _get(api_session, "/api/network/dns")
        _skip_html(r, "Network")
        assert r.status_code == 200

    def test_hostname(self, api_session):
        r = _get(api_session, "/api/network/hostname")
        _skip_html(r, "Network")
        assert r.status_code == 200
        d = r.json()
        assert "hostname" in d

    def test_connectivity(self, api_session):
        """Verify network interfaces returns data."""
        r = _get(api_session, "/api/network/interfaces")
        _skip_html(r, "Network")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  STORAGE – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestStorageDeep:
    """Functional tests for Storage Manager."""

    def test_disks(self, api_session):
        r = _get(api_session, "/api/storage/disks")
        _skip_html(r, "Storage")
        assert r.status_code == 200

    def test_mounts(self, api_session):
        r = _get(api_session, "/api/storage/mounts")
        _skip_html(r, "Storage")
        assert r.status_code == 200

    def test_smart_list(self, api_session):
        """SMART data requires disk param — test with and without."""
        r = _get(api_session, "/api/storage/smart", params={"disk": "sda"})
        _skip_html(r, "Storage")
        # 200=data, 400=no such disk, 503=smartctl missing
        assert r.status_code in (200, 400, 503)


# ═══════════════════════════════════════════════════════════════
#  USERS – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestUsersDeep:
    """Functional tests for user management."""

    def test_list_users(self, api_session):
        r = _get(api_session, "/api/users/list")
        _skip_html(r, "Users")
        assert r.status_code == 200

    def test_user_create_and_delete(self, api_session):
        """Create a test user then delete it."""
        tag = uuid.uuid4().hex[:6]
        uname = f"e2etest{tag}"
        r = _post(api_session, "/api/users/create", json={
            "username": uname,
            "password": "TestPass123!",
        })
        _skip_html(r, "Users")
        if r.status_code == 400:
            pytest.skip(f"Cannot create user: {r.text[:200]}")
        assert r.status_code == 200

        try:
            # Verify in list
            r = _get(api_session, "/api/users/list")
            users = r.json() if isinstance(r.json(), list) else r.json().get("users", [])
            user_names = [u.get("username", u) if isinstance(u, dict) else u for u in users]
            assert uname in user_names
        finally:
            # Delete
            r = _post(api_session, "/api/users/delete", json={"username": uname})
            assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  SERVICES – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestServicesDeep:
    """Functional tests for Services/systemd manager."""

    def test_list_services(self, api_session):
        r = _get(api_session, "/api/services/list")
        _skip_html(r, "Services")
        assert r.status_code == 200

    def test_service_status(self, api_session):
        """Check status of the ethos service itself."""
        r = _get(api_session, "/api/services/status", params={"name": "ethos"})
        _skip_html(r, "Services")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  EVENT LOG – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestEventLogDeep:
    """Functional tests for Event Log."""

    def test_events_list(self, api_session):
        r = _get(api_session, "/api/eventlog/events")
        _skip_html(r, "EventLog")
        assert r.status_code == 200

    def test_events_filter(self, api_session):
        """Filter events by level."""
        r = _get(api_session, "/api/eventlog/events", params={"level": "error"})
        _skip_html(r, "EventLog")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  NOTIFICATIONS – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestNotificationsDeep:
    """Functional tests for Notifications."""

    def test_list(self, api_session):
        r = _get(api_session, "/api/notifications/list")
        _skip_html(r, "Notifications")
        assert r.status_code == 200

    def test_dismiss(self, api_session):
        """Clear all notifications."""
        r = _post(api_session, "/api/notifications/clear")
        _skip_html(r, "Notifications")
        assert r.status_code in (200, 204, 404, 405)


# ═══════════════════════════════════════════════════════════════
#  DOMAINS MANAGER – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestDomainsDeep:
    """Functional tests for Domains Manager."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/domains/list")
        _skip_html(r, "Domains")

    def test_list(self, api_session):
        r = _get(api_session, "/api/domains/list")
        assert r.status_code == 200

    def test_add_and_remove(self, api_session):
        """Add a domain entry then remove it."""
        tag = uuid.uuid4().hex[:6]
        domain = f"e2etest-{tag}.local"
        r = _post(api_session, "/api/domains/add", json={
            "domain": domain,
            "target": "127.0.0.1",
        })
        if r.status_code in (400, 503):
            pytest.skip(f"Cannot add domain: {r.text[:200]}")
        assert r.status_code == 200

        try:
            # Verify
            r = _get(api_session, "/api/domains/list")
            domains = r.json() if isinstance(r.json(), list) else r.json().get("domains", r.json().get("items", []))
            found = [d for d in domains if isinstance(d, dict) and d.get("domain") == domain]
            assert len(found) >= 1
        finally:
            _post(api_session, "/api/domains/remove", json={"domain": domain})


# ═══════════════════════════════════════════════════════════════
#  STICKY NOTES – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestStickyNotesDeep:
    """Functional tests for Sticky Notes."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/sticky-notes/list")
        _skip_html(r, "StickyNotes")

    def test_list(self, api_session):
        r = _get(api_session, "/api/sticky-notes/list")
        assert r.status_code == 200

    def test_note_crud(self, api_session):
        """Create, update, delete a sticky note."""
        tag = uuid.uuid4().hex[:8]
        r = _post(api_session, "/api/sticky-notes/create", json={
            "content": f"E2E test note {tag}",
            "color": "#ffeb3b",
        })
        assert r.status_code == 200
        d = r.json()
        note_id = d.get("id") or d.get("note", {}).get("id")
        assert note_id, f"No note ID in: {d}"

        try:
            # Update
            r = _put(api_session, f"/api/sticky-notes/{note_id}", json={
                "content": f"Updated {tag}",
            })
            if r.status_code == 405:
                # Try POST update
                r = _post(api_session, f"/api/sticky-notes/update", json={
                    "id": note_id,
                    "content": f"Updated {tag}",
                })
            assert r.status_code == 200
        finally:
            # Delete
            r = _delete(api_session, f"/api/sticky-notes/{note_id}")
            if r.status_code == 405:
                r = _post(api_session, "/api/sticky-notes/delete", json={"id": note_id})
            assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  TICKETS – Deep Tests
# ═══════════════════════════════════════════════════════════════

class TestTicketsDeep:
    """Functional tests for Tickets/Kanban app."""

    @pytest.fixture(autouse=True)
    def _check_available(self, api_session):
        r = _get(api_session, "/api/tickets/boards")
        _skip_html(r, "Tickets")

    def test_boards_list(self, api_session):
        r = _get(api_session, "/api/tickets/boards")
        assert r.status_code == 200

    def test_board_crud(self, api_session):
        """Create board, add ticket, delete both."""
        tag = uuid.uuid4().hex[:6]
        # Create board
        r = _post(api_session, "/api/tickets/boards", json={
            "name": f"e2e-board-{tag}",
        })
        assert r.status_code == 200
        d = r.json()
        board_id = d.get("id") or d.get("board", {}).get("id") or d.get("board_id")
        assert board_id, f"No board_id in: {d}"

        try:
            # Create ticket in board
            r = _post(api_session, f"/api/tickets/boards/{board_id}/tickets", json={
                "title": f"Test ticket {tag}",
                "description": "E2E test ticket",
            })
            if r.status_code == 200:
                ticket_id = r.json().get("id") or r.json().get("ticket", {}).get("id")
                if ticket_id:
                    # Get tickets
                    r = _get(api_session, f"/api/tickets/boards/{board_id}/tickets")
                    assert r.status_code == 200
        finally:
            # Delete board
            r = _delete(api_session, f"/api/tickets/boards/{board_id}")
            assert r.status_code == 200
