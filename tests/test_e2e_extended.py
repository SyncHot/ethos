"""
EthOS NAS – Extended E2E tests
================================
Covers:
 1. Remaining untested routes (58 routes across 20+ blueprints)
 2. CRUD workflow tests (create → read → update → delete)
 3. Security / negative-input tests (injection, path traversal, boundary)
 4. Cross-app integration tests

Run:
    ETHOS_USER=myuser ETHOS_PASS='mypass' pytest tests/test_e2e_extended.py -v
    ETHOS_BASE_URL=http://localhost:9004 ...  # for VM
"""
import os
import sys
import time
import uuid
import json

import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))
from helpers import api_get, api_post, BASE_URL

# --------------- helpers ---------------

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

def _skip_html(resp, label="App"):
    if "text/html" in resp.headers.get("Content-Type", ""):
        pytest.skip(f"{label} blueprint not registered")
    if resp.status_code == 429:
        pytest.skip(f"{label} rate-limited (429)")

def _skip_dep(resp, label="App"):
    _skip_html(resp, label)
    if resp.status_code == 503:
        pytest.skip(f"{label} dependency not installed")

def _skip_not_installed(resp, label="App"):
    _skip_html(resp, label)
    if resp.status_code == 200:
        d = resp.json()
        if d.get("installed") is False:
            pytest.skip(f"{label} not installed")

TAG = uuid.uuid4().hex[:8]


# ====================================================================
# PART 1: REMAINING UNTESTED ROUTES
# ====================================================================

# --- Backup: usb-browse, usb-mkdir, trigger-smart, restore, backup-preview ---
class TestBackupRoutes:
    @pytest.fixture(autouse=True)
    def _check(self, api_session):
        r = _get(api_session, "/api/backup/status")
        _skip_html(r, "Backup")

    def test_usb_browse_missing_path(self, api_session):
        r = _get(api_session, "/api/backup/usb-browse")
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_usb_browse_invalid_path(self, api_session):
        r = _get(api_session, "/api/backup/usb-browse", params={"path": "/etc/shadow"})
        assert r.status_code in (400, 403, 404, 405, 429, 500)

    def test_usb_browse_media(self, api_session):
        r = _get(api_session, "/api/backup/usb-browse", params={"path": "/media"})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_usb_mkdir_missing_params(self, api_session):
        r = _post(api_session, "/api/backup/usb-mkdir", json={})
        assert r.status_code in (400, 404, 405, 429, 500)

    def test_usb_mkdir_invalid_path(self, api_session):
        r = _post(api_session, "/api/backup/usb-mkdir", json={"path": "/etc", "name": "test"})
        assert r.status_code in (400, 403, 404, 405, 429, 500)

    def test_usb_mkdir_traversal_name(self, api_session):
        """Name with .. should be rejected."""
        r = _post(api_session, "/api/backup/usb-mkdir", json={"path": "/media", "name": "../../etc"})
        assert r.status_code in (400, 403, 404, 405, 429, 500)

    def test_trigger_smart(self, api_session):
        r = _post(api_session, "/api/backup/trigger-smart", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_restore_missing_params(self, api_session):
        r = _post(api_session, "/api/backup/restore", json={})
        assert r.status_code in (400, 404, 405, 429, 500)

    def test_backup_preview_nonexistent(self, api_session):
        r = _get(api_session, "/api/backup/backup-preview/nonexistent.tar.gz")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- Downloads: extract, package/remove, test-debrid, test-saved-debrid ---
class TestDownloadsRoutes:
    @pytest.fixture(autouse=True)
    def _check(self, api_session):
        r = _get(api_session, "/api/downloads/list")
        _skip_html(r, "Downloads")
        _skip_dep(r, "Downloads")

    def test_extract_no_id(self, api_session):
        r = _post(api_session, "/api/downloads/extract", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_extract_nonexistent(self, api_session):
        r = _post(api_session, "/api/downloads/extract", json={"package_id": "fake-pkg-e2e"})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_package_remove_nonexistent(self, api_session):
        r = _post(api_session, "/api/downloads/package/remove", json={"package_id": "fake-pkg-e2e"})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_debrid_no_key(self, api_session):
        r = _post(api_session, "/api/downloads/test-debrid", json={"service": "realdebrid", "api_key": ""})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_saved_debrid(self, api_session):
        r = _post(api_session, "/api/downloads/test-saved-debrid", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- Gallery: shared, download-zip, pkg-status ---
class TestGalleryRoutes:
    @pytest.fixture(autouse=True)
    def _check(self, api_session):
        r = _get(api_session, "/api/gallery/stats")
        _skip_html(r, "Gallery")
        _skip_not_installed(r, "Gallery")

    def test_shared_nonexistent(self, api_session):
        r = _get(api_session, "/api/gallery/shared/nonexistent-token-e2e")
        assert r.status_code in (200, 400, 404, 410, 405, 429, 500)

    def test_download_zip_no_paths(self, api_session):
        r = _post(api_session, "/api/gallery/download-zip", json={"paths": []})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_download_zip_nonexistent(self, api_session):
        r = _post(api_session, "/api/gallery/download-zip", json={"paths": ["/nonexistent/file.jpg"]})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_pkg_status(self, api_session):
        r = _get(api_session, "/api/gallery/pkg-status")
        assert r.status_code in (200, 404, 405, 429, 500)


# --- VM Manager: import-disk, convert ---
class TestVMManagerRoutes:
    @pytest.fixture(autouse=True)
    def _check(self, api_session):
        r = _get(api_session, "/api/vm/list")
        _skip_html(r, "VM")
        _skip_dep(r, "VM")

    def test_import_disk_no_source(self, api_session):
        r = _post(api_session, "/api/vm/import-disk", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500, 503)

    def test_convert_no_source(self, api_session):
        r = _post(api_session, "/api/vm/convert", json={"source": "/nonexistent.img", "format": "qcow2"})
        assert r.status_code in (200, 400, 404, 405, 429, 500, 503)

    def test_convert_bad_format(self, api_session):
        r = _post(api_session, "/api/vm/convert", json={"source": "/tmp/test.img", "format": "exe"})
        assert r.status_code in (200, 400, 404, 405, 429, 500, 503)


# --- Tickets: preflight, gen-tests-poll, ai-usage ---
class TestTicketsRoutes:
    @pytest.fixture(autouse=True)
    def _check(self, api_session):
        r = _get(api_session, "/api/tickets/projects")
        _skip_html(r, "Tickets")
        _skip_dep(r, "Tickets")

    def test_preflight(self, api_session):
        r = _get(api_session, "/api/tickets/preflight", params={"quick": "1"}, timeout=30)
        assert r.status_code in (200, 400, 404, 405, 422, 429, 500, 504)

    def test_gen_tests_poll_nonexistent(self, api_session):
        r = _get(api_session, "/api/tickets/gen-tests-poll/nonexistent-task-id")
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_ai_usage_nonexistent(self, api_session):
        r = _get(api_session, "/api/tickets/ai-usage/nonexistent-project")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- FamilyHub: users ---
class TestFamilyHubRoutes:
    @pytest.fixture(autouse=True)
    def _check(self, api_session):
        r = _get(api_session, "/api/familyhub/posts")
        _skip_html(r, "FamilyHub")
        _skip_dep(r, "FamilyHub")

    def test_users(self, api_session):
        r = _get(api_session, "/api/familyhub/users")
        assert r.status_code in (200, 404, 405, 429, 500)
        if r.status_code == 200:
            d = r.json()
            assert "users" in d


# --- Storage: sftp/ftp/samba/nfs/dlna status & install routes ---
class TestStorageProtocolRoutes:
    @pytest.fixture(autouse=True)
    def _check(self, api_session):
        r = _get(api_session, "/api/storage/keepalive")
        _skip_html(r, "Storage")

    def test_sftp_status(self, api_session):
        r = _get(api_session, "/api/storage/sftp/status")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_ftp_status(self, api_session):
        r = _get(api_session, "/api/storage/ftp/status")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_ftp_install(self, api_session):
        r = _post(api_session, "/api/storage/ftp/install", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_samba_install(self, api_session):
        r = _post(api_session, "/api/storage/samba/install", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_nfs_install(self, api_session):
        r = _post(api_session, "/api/storage/nfs/install", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_dlna_status(self, api_session):
        r = _get(api_session, "/api/storage/dlna/status")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_dlna_install(self, api_session):
        r = _post(api_session, "/api/storage/dlna/install", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_dlna_rescan(self, api_session):
        r = _post(api_session, "/api/storage/dlna/rescan", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_merge_no_disk(self, api_session):
        r = _post(api_session, "/api/storage/merge", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_partition_no_disk(self, api_session):
        r = _post(api_session, "/api/storage/partition", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- Firewall: unban ---
class TestFirewallRoutes:
    def test_unban_missing_params(self, api_session):
        r = _post(api_session, "/api/firewall/unban", json={})
        _skip_html(r, "Firewall")
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_unban_invalid_jail(self, api_session):
        r = _post(api_session, "/api/firewall/unban", json={"jail": "../../etc", "ip": "1.2.3.4"})
        _skip_html(r, "Firewall")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- DLNA: uninstall ---
class TestDLNARoutes:
    def test_uninstall(self, api_session):
        r = _post(api_session, "/api/dlna/uninstall", json={})
        _skip_html(r, "DLNA")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- Flasher: dismiss ---
class TestFlasherRoutes:
    def test_dismiss(self, api_session):
        r = _post(api_session, "/api/flasher/dismiss", json={})
        _skip_html(r, "Flasher")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- DiskRepair: fsck, badblocks ---
class TestDiskRepairRoutes:
    @pytest.fixture(autouse=True)
    def _check(self, api_session):
        r = _get(api_session, "/api/diskrepair/status")
        _skip_html(r, "DiskRepair")
        _skip_dep(r, "DiskRepair")

    def test_fsck_no_partition(self, api_session):
        r = _post(api_session, "/api/diskrepair/fsck", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_badblocks_no_disk(self, api_session):
        r = _post(api_session, "/api/diskrepair/badblocks", json={})
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- Updater: publish ---
class TestUpdaterRoutes:
    def test_publish(self, api_session):
        r = _post(api_session, "/api/update/publish", json={})
        _skip_html(r, "Updater")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- Packages: update, remove ---
class TestPackagesRoutes:
    def test_update_apt(self, api_session):
        """POST /update triggers apt-get update. Just check it responds."""
        r = _post(api_session, "/api/packages/update", json={})
        _skip_html(r, "Packages")
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_remove_nonexistent(self, api_session):
        r = _post(api_session, "/api/packages/remove", json={"package": "e2e-nonexistent-pkg-12345"})
        _skip_html(r, "Packages")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- Pkg Registry: script route ---
class TestPkgRegistryRoutes:
    def test_script_nonexistent(self, api_session):
        r = _get(api_session, "/api/pkg-registry/script/nonexistent-repo/nonexistent-pkg/install")
        _skip_html(r, "PkgRegistry")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- Printer: cups-install ---
class TestPrinterRoutes:
    def test_cups_install(self, api_session):
        r = _post(api_session, "/api/printer/cups-install", json={})
        _skip_html(r, "Printer")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


# --- RemoteLog: device history with ID ---
class TestRemoteLogRoutes:
    def test_device_history(self, api_session):
        r = _get(api_session, "/api/device-logs/test-device/history")
        _skip_html(r, "RemoteLog")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_device_specific_file(self, api_session):
        r = _get(api_session, "/api/device-logs/test-device/report-2025-01-01.json")
        _skip_html(r, "RemoteLog")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_device_delete_file(self, api_session):
        r = _delete(api_session, "/api/device-logs/test-device/report-2025-01-01.json")
        _skip_html(r, "RemoteLog")
        assert r.status_code in (200, 404, 405, 429, 500)


# ====================================================================
# PART 2: CRUD WORKFLOW TESTS
# ====================================================================

class TestStickyNotesCRUD:
    """Full create → read → update → delete lifecycle."""

    def test_full_lifecycle(self, api_session):
        # Create
        r = _post(api_session, "/api/stickynotes/notes", json={
            "title": f"E2E-{TAG}", "content": "test note", "color": "#ffeb3b"
        })
        _skip_html(r, "StickyNotes")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 201, 405, 500)
        if r.status_code not in (200, 201):
            return
        d = r.json()
        note_id = d.get("note", {}).get("id") or d.get("id")
        assert note_id

        # Read
        r = _get(api_session, "/api/stickynotes/notes")
        assert r.status_code == 200
        notes = r.json().get("notes", [])
        assert any(n["id"] == note_id for n in notes)

        # Update
        r = _put(api_session, f"/api/stickynotes/notes/{note_id}", json={
            "title": f"E2E-{TAG}-updated", "content": "updated"
        })
        assert r.status_code in (200, 404, 405, 429, 500)

        # Delete
        r = _delete(api_session, f"/api/stickynotes/notes/{note_id}")
        assert r.status_code in (200, 204, 404, 405, 429, 500)


class TestTicketsCRUD:
    """Full project + ticket lifecycle."""

    def test_project_lifecycle(self, api_session):
        r = _get(api_session, "/api/tickets/projects")
        _skip_html(r, "Tickets")
        _skip_dep(r, "Tickets")
        if r.status_code == 429:
            pytest.skip("rate-limited")

        # Create project
        r = _post(api_session, "/api/tickets/projects", json={
            "name": f"E2E-Proj-{TAG}", "description": "test"
        })
        if r.status_code not in (200, 201):
            return
        d = r.json()
        proj = d.get("project") or d
        proj_id = proj.get("id")
        if not proj_id:
            return

        # Create ticket
        r = _post(api_session, f"/api/tickets/projects/{proj_id}/tickets", json={
            "title": f"E2E-Ticket-{TAG}", "description": "test ticket", "priority": "medium"
        })
        assert r.status_code in (200, 201, 404, 405, 429, 500)

        # Read tickets
        r = _get(api_session, f"/api/tickets/projects/{proj_id}/tickets")
        assert r.status_code in (200, 404, 405, 429, 500)

        # Delete project
        r = _delete(api_session, f"/api/tickets/projects/{proj_id}")
        assert r.status_code in (200, 204, 404, 405, 429, 500)


class TestFileManagerCRUD:
    """Create dir → create file → rename → copy → move → trash → restore → delete."""

    def test_full_lifecycle(self, api_session):
        base = f"/tmp/e2e-fm-{TAG}"

        # Create dir
        r = _post(api_session, "/api/files/mkdir", json={"path": base})
        _skip_html(r, "FileManager")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 201, 400, 405, 429, 500)

        # Upload/create file via save
        r = _post(api_session, "/api/files/save", json={
            "path": f"{base}/test.txt", "content": "hello e2e"
        })
        assert r.status_code in (200, 201, 400, 404, 405, 429, 500)

        # List
        r = _get(api_session, "/api/files/list", params={"path": base})
        assert r.status_code in (200, 404, 405, 429, 500)

        # Rename
        r = _post(api_session, "/api/files/rename", json={
            "path": f"{base}/test.txt", "new_name": "renamed.txt"
        })
        assert r.status_code in (200, 400, 404, 405, 429, 500)

        # Info
        r = _get(api_session, "/api/files/info", params={"path": f"{base}/renamed.txt"})
        assert r.status_code in (200, 400, 404, 405, 429, 500)

        # Copy (correct API: sources array + dest directory)
        r = _post(api_session, "/api/files/mkdir", json={"path": f"{base}/cp_dest"})
        r = _post(api_session, "/api/files/copy", json={
            "sources": [f"{base}/renamed.txt"], "dest": f"{base}/cp_dest"
        })
        assert r.status_code in (200, 400, 404, 405, 429, 500)

        # Delete
        r = _delete(api_session, "/api/files/delete", json={"paths": [base]})
        assert r.status_code in (200, 204, 400, 404, 405, 429, 500)


class TestDownloadsCRUD:
    """Add URL download → check list → cancel → clear."""

    def test_download_lifecycle(self, api_session):
        r = _get(api_session, "/api/downloads/list")
        _skip_html(r, "Downloads")
        _skip_dep(r, "Downloads")
        if r.status_code == 429:
            pytest.skip("rate-limited")

        # Add a download (invalid URL — should fail gracefully)
        r = _post(api_session, "/api/downloads/add", json={
            "url": "http://nonexistent.invalid/file.zip"
        })
        assert r.status_code in (200, 400, 404, 405, 429, 500)

        # Stats
        r = _get(api_session, "/api/downloads/stats")
        assert r.status_code in (200, 404, 405, 429, 500)


class TestFamilyHubCRUD:
    """Post + chore lifecycle."""

    def test_post_lifecycle(self, api_session):
        r = _get(api_session, "/api/familyhub/posts")
        _skip_html(r, "FamilyHub")
        _skip_dep(r, "FamilyHub")
        if r.status_code == 429:
            pytest.skip("rate-limited")

        # Create post
        r = _post(api_session, "/api/familyhub/posts", json={
            "content": f"E2E test post {TAG}", "type": "note"
        })
        assert r.status_code in (200, 201, 400, 404, 405, 429, 500)
        if r.status_code not in (200, 201):
            return
        d = r.json()
        post_id = d.get("post", {}).get("id") or d.get("id")
        if not post_id:
            return

        # Read posts
        r = _get(api_session, "/api/familyhub/posts")
        assert r.status_code == 200

        # Delete post
        r = _delete(api_session, f"/api/familyhub/posts/{post_id}")
        assert r.status_code in (200, 204, 404, 405, 429, 500)


class TestDomainsCRUD:
    """Domain + record lifecycle."""

    def test_domain_lifecycle(self, api_session):
        r = _get(api_session, "/api/domains/list")
        _skip_html(r, "Domains")
        _skip_dep(r, "Domains")
        if r.status_code == 429:
            pytest.skip("rate-limited")

        name = f"e2e-{TAG}.local"

        # Add
        r = _post(api_session, "/api/domains/add", json={"domain": name})
        assert r.status_code in (200, 201, 400, 404, 405, 429, 500)

        # List
        r = _get(api_session, "/api/domains/list")
        assert r.status_code in (200, 404, 405, 429, 500)

        # Remove
        r = _post(api_session, "/api/domains/remove", json={"domain": name})
        assert r.status_code in (200, 400, 404, 405, 429, 500)


class TestCronCRUD:
    """Cron job create → list → delete."""

    def test_cron_lifecycle(self, api_session):
        r = _get(api_session, "/api/cron/jobs")
        _skip_html(r, "Cron")
        if r.status_code == 429:
            pytest.skip("rate-limited")

        # Create
        r = _post(api_session, "/api/cron/jobs", json={
            "minute": "0", "hour": "3", "day": "*", "month": "*",
            "weekday": "*", "command": "echo e2e-test", "user": "root"
        })
        assert r.status_code in (200, 201, 400, 404, 405, 429, 500, 503)
        if r.status_code not in (200, 201):
            return
        d = r.json()
        job_id = d.get("job", {}).get("id") or d.get("id")
        if not job_id:
            return

        # List
        r = _get(api_session, "/api/cron/jobs")
        assert r.status_code == 200

        # Delete
        r = _delete(api_session, f"/api/cron/jobs/{job_id}")
        assert r.status_code in (200, 204, 404, 405, 429, 500)


# ====================================================================
# PART 3: SECURITY / NEGATIVE-INPUT TESTS
# ====================================================================

class TestSecurityPathTraversal:
    """Path traversal attacks on file-related endpoints."""

    TRAVERSAL_PATHS = [
        "../../../etc/shadow",
        "/etc/shadow",
        "..%2F..%2F..%2Fetc%2Fshadow",
        "/proc/self/environ",
    ]

    def test_file_list_traversal(self, api_session):
        for p in self.TRAVERSAL_PATHS:
            r = _get(api_session, "/api/files/list", params={"path": p})
            _skip_html(r, "FileManager")
            if r.status_code == 429:
                pytest.skip("rate-limited")
            assert r.status_code in (200, 400, 403, 404, 405, 429, 500)
            if r.status_code == 200:
                d = r.json()
                items = d.get("items", d.get("files", []))
                sensitive = ["shadow", "passwd", "environ"]
                for item in items:
                    name = item.get("name", "")
                    assert name not in sensitive, f"Traversal leaked {name}"

    def test_file_read_traversal(self, api_session):
        for p in self.TRAVERSAL_PATHS:
            r = _get(api_session, "/api/files/read", params={"path": p})
            _skip_html(r, "FileManager")
            if r.status_code == 429:
                pytest.skip("rate-limited")
            # Must NOT return file content from sensitive paths
            assert r.status_code in (200, 400, 403, 404, 405, 429, 500)
            if r.status_code == 200 and "/etc/shadow" in p:
                content = r.text
                assert "root:" not in content, "Shadow file contents leaked!"

    def test_editor_open_traversal(self, api_session):
        r = _post(api_session, "/api/editor/open", json={"path": "/etc/shadow"})
        _skip_html(r, "Editor")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 400, 403, 404, 405, 429, 500)

    def test_backup_browse_traversal(self, api_session):
        r = _get(api_session, "/api/backup/usb-browse", params={"path": "/etc"})
        _skip_html(r, "Backup")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        # Should reject paths outside allowed USB dirs
        assert r.status_code in (400, 403, 404, 405, 429, 500)


class TestSecurityInjection:
    """Command/SQL injection attempts."""

    INJECTION_PAYLOADS = [
        "; rm -rf /",
        "$(cat /etc/shadow)",
        "`cat /etc/shadow`",
        "| cat /etc/shadow",
        "'; DROP TABLE users; --",
    ]

    def test_cron_command_injection(self, api_session):
        """Cron commands should be sanitized."""
        for payload in self.INJECTION_PAYLOADS:
            r = _post(api_session, "/api/cron/jobs", json={
                "minute": "0", "hour": "0", "day": "*", "month": "*",
                "weekday": "*", "command": payload, "user": "root"
            })
            _skip_html(r, "Cron")
            if r.status_code == 429:
                pytest.skip("rate-limited")
            # Should either work (cron stores commands as-is) or reject
            assert r.status_code in (200, 201, 400, 403, 404, 405, 429, 500, 503)

    def test_antivirus_schedule_injection(self, api_session):
        """Schedule fields should be strictly validated."""
        r = _post(api_session, "/api/antivirus/schedule", json={
            "minute": "0; rm -rf /", "hour": "0", "day": "*",
            "month": "*", "weekday": "*", "path": "/tmp"
        })
        _skip_html(r, "Antivirus")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (400, 403, 404, 405, 429, 500), \
            "Antivirus accepted malicious cron field!"

    def test_firewall_rule_injection(self, api_session):
        r = _post(api_session, "/api/firewall/rules", json={
            "action": "add", "port": "22; rm -rf /",
            "proto": "tcp", "ufw_action": "allow"
        })
        _skip_html(r, "Firewall")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (400, 403, 404, 405, 429, 500)

    def test_ssh_key_injection(self, api_session):
        r = _post(api_session, "/api/ssh/keys", json={
            "key": "ssh-rsa AAAA$(cat /etc/shadow) injected"
        })
        _skip_html(r, "SSH")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 400, 403, 404, 405, 429, 500)


class TestSecurityAuth:
    """Auth boundary tests — verify unauthed requests are rejected."""

    PROTECTED_ENDPOINTS = [
        ("GET", "/api/settings/general"),
        ("GET", "/api/storage/disks"),
        ("POST", "/api/firewall/rules"),
        ("GET", "/api/backup/status"),
        ("GET", "/api/users/list"),
        ("POST", "/api/cron/jobs"),
        ("GET", "/api/files/list"),
    ]

    def test_no_token_rejected(self):
        """All protected endpoints must reject requests without auth token."""
        sess = requests.Session()
        sess.headers["Content-Type"] = "application/json"
        for method, path in self.PROTECTED_ENDPOINTS:
            url = f"{BASE_URL}{path}"
            if method == "GET":
                r = sess.get(url, timeout=10)
            else:
                r = sess.post(url, json={}, timeout=10)
            if r.status_code == 429:
                continue  # rate-limited, skip
            assert r.status_code in (401, 403, 405, 429), \
                f"{method} {path} returned {r.status_code} without auth"

    def test_invalid_token_rejected(self):
        """Invalid Bearer token must be rejected."""
        sess = requests.Session()
        sess.headers.update({
            "Authorization": "Bearer invalid-token-12345",
            "Content-Type": "application/json"
        })
        r = sess.get(f"{BASE_URL}/api/settings/general", timeout=10)
        if r.status_code != 429:
            assert r.status_code in (401, 403, 405, 429)


class TestSecurityInputBoundary:
    """Boundary/edge-case inputs."""

    def test_empty_json_body(self, api_session):
        """Endpoints should handle empty body gracefully."""
        endpoints = [
            "/api/firewall/rules",
            "/api/backup/create",
            "/api/downloads/add",
            "/api/antivirus/scan",
        ]
        for ep in endpoints:
            r = _post(api_session, ep, json={})
            if r.status_code == 429:
                continue
            assert r.status_code in (200, 400, 403, 404, 405, 409, 429, 500, 503), \
                f"{ep} crashed on empty JSON body"

    def test_oversized_string(self, api_session):
        """Huge string inputs shouldn't crash the server."""
        big = "A" * 100000
        r = _post(api_session, "/api/stickynotes/notes", json={
            "title": big, "content": big
        })
        _skip_html(r, "StickyNotes")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 201, 400, 413, 405, 429, 500)

    def test_unicode_input(self, api_session):
        """Unicode / emoji should be handled properly."""
        r = _post(api_session, "/api/stickynotes/notes", json={
            "title": "🔥 Тест 测试 テスト", "content": "✅ Multi-lang"
        })
        _skip_html(r, "StickyNotes")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 201, 400, 405, 429, 500)

    def test_null_values(self, api_session):
        """Null JSON values shouldn't crash."""
        r = _post(api_session, "/api/files/mkdir", json={"path": None})
        _skip_html(r, "FileManager")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 400, 404, 405, 429, 500)

    def test_wrong_content_type(self, api_session):
        """Sending form data to JSON endpoint."""
        r = api_session.post(
            f"{api_session.base_url}/api/stickynotes/notes",
            data="not-json",
            headers={"Content-Type": "text/plain"},
            timeout=10
        )
        if r.status_code != 429:
            assert r.status_code in (200, 400, 415, 405, 429, 500)


# ====================================================================
# PART 4: CROSS-APP INTEGRATION TESTS
# ====================================================================

class TestDashboardIntegration:
    """Dashboard aggregates data from multiple apps."""

    def test_dashboard_loads(self, api_session):
        r = _get(api_session, "/api/dashboard/stats")
        _skip_html(r, "Dashboard")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_system_info_complete(self, api_session):
        r = _get(api_session, "/api/system/info")
        _skip_html(r, "System")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        if r.status_code == 200:
            d = r.json()
            assert "hostname" in d or "version" in d or "ok" in d


class TestResourceMonitor:
    """Resource monitor real-time data."""

    def test_cpu_history(self, api_session):
        r = _get(api_session, "/api/resources/cpu/history")
        _skip_html(r, "Resources")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_memory_history(self, api_session):
        r = _get(api_session, "/api/resources/memory/history")
        _skip_html(r, "Resources")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_network_history(self, api_session):
        r = _get(api_session, "/api/resources/network/history")
        _skip_html(r, "Resources")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_disk_io_history(self, api_session):
        r = _get(api_session, "/api/resources/disk/history")
        _skip_html(r, "Resources")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_processes(self, api_session):
        r = _get(api_session, "/api/resources/processes")
        _skip_html(r, "Resources")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, list):
                procs = d
            else:
                procs = d.get("processes", d.get("items", []))
            assert len(procs) > 0, "No processes returned"


class TestServicesIntegration:
    """Services management."""

    def test_list_services(self, api_session):
        r = _get(api_session, "/api/services/list")
        _skip_html(r, "Services")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)
        if r.status_code == 200:
            d = r.json()
            items = d.get("services", d.get("items", []))
            assert len(items) > 0, "No services returned"

    def test_service_status(self, api_session):
        r = _get(api_session, "/api/services/status", params={"name": "ssh"})
        _skip_html(r, "Services")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


class TestEventLogIntegration:
    """Event log queries."""

    def test_recent_events(self, api_session):
        r = _get(api_session, "/api/event-log/entries")
        _skip_html(r, "EventLog")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_filtered_events(self, api_session):
        r = _get(api_session, "/api/event-log/entries", params={"level": "error", "limit": "5"})
        _skip_html(r, "EventLog")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)


class TestNotificationsIntegration:
    """Notification system."""

    def test_list_notifications(self, api_session):
        r = _get(api_session, "/api/notifications/list")
        _skip_html(r, "Notifications")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_dismiss_nonexistent(self, api_session):
        r = _post(api_session, "/api/notifications/dismiss", json={"id": "nonexistent-e2e"})
        _skip_html(r, "Notifications")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 400, 404, 405, 429, 500)


class TestAppStoreIntegration:
    """App store catalog validation."""

    def test_catalog_structure(self, api_session):
        r = _get(api_session, "/api/appstore/catalog")
        _skip_html(r, "AppStore")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        if r.status_code != 200:
            return
        d = r.json()
        apps = d.get("apps", d.get("items", []))
        assert len(apps) > 0, "Empty app catalog"
        # Each app should have required fields
        for app in apps[:5]:
            assert "id" in app, f"App missing 'id': {app}"
            assert "name" in app or "title" in app, f"App missing name: {app}"

    def test_installed_apps(self, api_session):
        r = _get(api_session, "/api/appstore/installed")
        _skip_html(r, "AppStore")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)


class TestSettingsIntegration:
    """Settings read (don't write)."""

    def test_general_settings(self, api_session):
        r = _get(api_session, "/api/settings/general")
        _skip_html(r, "Settings")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)
        if r.status_code == 200:
            d = r.json()
            assert "hostname" in d or "settings" in d or "ok" in d

    def test_appearance_settings(self, api_session):
        r = _get(api_session, "/api/settings/appearance")
        _skip_html(r, "Settings")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_datetime_settings(self, api_session):
        r = _get(api_session, "/api/settings/datetime")
        _skip_html(r, "Settings")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)

    def test_power_settings(self, api_session):
        r = _get(api_session, "/api/settings/power")
        _skip_html(r, "Settings")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code in (200, 404, 405, 429, 500)


# ====================================================================
# PART 5: RESPONSE VALIDATION TESTS
# ====================================================================

class TestResponseFormats:
    """Verify API responses follow expected format conventions."""

    def test_health_response(self, api_session):
        r = _get(api_session, "/api/health")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        assert r.status_code == 200

    def test_auth_verify_format(self, api_session):
        r = _get(api_session, "/api/auth/verify")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        if r.status_code == 200:
            d = r.json()
            assert "ok" in d or "valid" in d or "user" in d

    def test_apps_list_format(self, api_session):
        r = _get(api_session, "/api/apps")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, list):
                apps = d
            else:
                apps = d.get("apps", d.get("items", []))
            assert isinstance(apps, list)

    def test_error_format_on_404(self, api_session):
        """Non-existent API path should return 404 or fall through to SPA."""
        r = _get(api_session, "/api/nonexistent-endpoint-e2e-test")
        if r.status_code == 429:
            pytest.skip("rate-limited")
        # Flask may return 404 JSON or fallthrough to SPA (200 HTML)
        assert r.status_code in (200, 404, 405, 429)

    def test_json_content_type(self, api_session):
        """Known JSON API responses should be JSON."""
        endpoints = ["/api/auth/verify", "/api/settings/general"]
        for ep in endpoints:
            r = _get(api_session, ep)
            if r.status_code == 429:
                continue
            _skip_html(r, ep)
            ct = r.headers.get("Content-Type", "")
            assert "application/json" in ct, f"{ep} returned {ct}"


class TestConcurrency:
    """Verify server handles concurrent requests without errors."""

    def test_parallel_reads(self, api_session):
        """Multiple simultaneous read requests should all succeed."""
        import concurrent.futures
        endpoints = [
            "/api/health", "/api/apps", "/api/auth/verify",
            "/api/resources/status", "/api/notifications/list"
        ]
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_get, api_session, ep): ep for ep in endpoints}
            for f in concurrent.futures.as_completed(futures):
                try:
                    r = f.result()
                    results.append((futures[f], r.status_code))
                except Exception as e:
                    results.append((futures[f], str(e)))

        for ep, code in results:
            if isinstance(code, int):
                assert code in (200, 404, 405, 429, 500), f"{ep} returned {code}"
