"""
EthOS NAS – Application Health Check Tests

Probes every app's key API endpoint to verify the blueprint is loaded
and responds with valid JSON. Safe to run against any EthOS instance
(dev server, remote machine, or fresh VM).

Usage:
    # Against local dev server
    ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/test_apps_health.py -v

    # Against a remote machine
    ETHOS_BASE_URL=http://host:port ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/test_apps_health.py -v

    # Quick smoke test (only core apps)
    pytest tests/test_apps_health.py -v -k core

    # Only optional apps
    pytest tests/test_apps_health.py -v -k optional
"""

import pytest
import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(base_url, token, path, **kw):
    kw.setdefault("timeout", 15)
    return requests.get(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        **kw,
    )

def _post(base_url, token, path, json=None, **kw):
    kw.setdefault("timeout", 15)
    return requests.post(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=json or {},
        **kw,
    )

def _is_json(resp):
    ct = resp.headers.get("Content-Type", "")
    return "application/json" in ct

def _check_endpoint(base_url, token, method, path, json_body=None):
    """Call endpoint and return (status_code, is_json, data_or_none)."""
    if method == "GET":
        r = _get(base_url, token, path)
    else:
        r = _post(base_url, token, path, json=json_body)
    is_j = _is_json(r)
    data = r.json() if is_j else None
    return r.status_code, is_j, data


# ═══════════════════════════════════════════════════════════════
#  CORE APPS — these must always work on every EthOS instance
# ═══════════════════════════════════════════════════════════════

class TestCoreAuth:
    """Auth & session management."""

    def test_verify_token(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/auth/verify")
        assert code == 200 and is_j
        assert data.get("valid") is True

    def test_setup_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/setup/status")
        assert code == 200 and is_j


class TestCoreSettings:
    """System settings."""

    def test_core_settings_read(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/settings/")
        assert code == 200 and is_j
        assert "hostname" in data

    def test_core_settings_timezones(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/settings/timezones")
        assert code == 200 and is_j


class TestCoreStorage:
    """Storage manager."""

    def test_core_storage_keepalive(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/storage/keepalive")
        assert code == 200 and is_j

    def test_core_storage_drives(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/storage/drives")
        assert code == 200 and is_j


class TestCoreFileManager:
    """File manager."""

    def test_core_files_list(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/files/list?path=/opt/ethos")
        assert code == 200 and is_j
        assert "items" in data

    def test_core_files_shares(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/files/shares")
        if not is_j:
            pytest.skip("Sharing blueprint not loaded (slim image)")
        assert code == 200


class TestCoreNetwork:
    """Network configuration."""

    def test_core_network_interfaces(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/network/interfaces")
        assert code == 200 and is_j


class TestCoreResources:
    """Resource monitor."""

    def test_core_resources_system(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/resources/system")
        assert code == 200 and is_j


class TestCoreUsers:
    """User management."""

    def test_core_users_list(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/users/list")
        assert code == 200 and is_j


class TestCoreBackup:
    """Backup app."""

    def test_core_backup_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/backup/status")
        assert code == 200 and is_j


class TestCorePower:
    """Power manager."""

    def test_core_power_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/power/status")
        assert code == 200 and is_j


class TestCoreServices:
    """System services."""

    def test_core_services_list(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/services/list")
        assert code == 200 and is_j


class TestCoreEventLog:
    """Event log."""

    def test_core_eventlog(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/eventlog")
        assert code == 200 and is_j


class TestCoreDashboard:
    """Dashboard."""

    def test_core_dashboard_summary(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/dashboard/summary")
        assert code == 200 and is_j


class TestCoreNotifications:
    """Notifications."""

    def test_core_notifications_config(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/notifications/config")
        assert code == 200 and is_j


class TestCoreUpdate:
    """Update system."""

    def test_core_update_check(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "POST", "/api/update/check")
        assert code == 200 and is_j
        assert "current_version" in data

    def test_core_update_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/update/status")
        assert code == 200 and is_j


class TestCoreSSH:
    """SSH manager."""

    def test_core_ssh_keys(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/ssh/keys")
        assert code == 200 and is_j


class TestCorePackages:
    """Package manager."""

    def test_core_packages_stats(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/packages/stats")
        assert code == 200 and is_j


class TestCoreAppManager:
    """App store / package center."""

    def test_core_appmanager_installed(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/app-manager/installed")
        assert code == 200 and is_j

    def test_core_appmanager_core(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/app-manager/core")
        assert code == 200 and is_j


class TestCoreApps:
    """Global app list."""

    def test_core_apps_list(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/apps")
        assert code == 200 and is_j


class TestCoreTOTP:
    """Two-factor authentication."""

    def test_core_totp_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/totp/status")
        assert code == 200 and is_j


# ═══════════════════════════════════════════════════════════════
#  CORE APPS WITH BACKEND IN _OPTIONAL_BLUEPRINTS
#  (firewall, fail2ban — core but backend may be missing on slim images)
# ═══════════════════════════════════════════════════════════════

class TestCoreFirewall:
    """Firewall (UFW) — core app."""

    def test_core_firewall_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/firewall/status")
        if not is_j:
            pytest.skip("Firewall backend not installed (slim image)")
        assert code == 200
        assert "status" in data or "ok" in data


class TestCoreFail2ban:
    """Intrusion protection — core app."""

    def test_core_fail2ban_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/fail2ban/status")
        if not is_j:
            pytest.skip("Fail2ban backend not installed (slim image)")
        # 500 = fail2ban service not running, still means blueprint works
        assert code in (200, 500)


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL APPS — skip gracefully if not installed
# ═══════════════════════════════════════════════════════════════

def _skip_if_not_installed(resp_is_json):
    if not resp_is_json:
        pytest.skip("App not installed (blueprint not registered)")


class TestOptionalDownloads:
    """Download Manager."""

    def test_optional_downloads_list(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/downloads/list")
        _skip_if_not_installed(is_j)
        assert code == 200

    def test_optional_downloads_history(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/downloads/history")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalVMManager:
    """VM Manager."""

    def test_optional_vm_machines(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/vm/machines")
        _skip_if_not_installed(is_j)
        assert code == 200

    def test_optional_vm_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/vm/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalDocker:
    """Docker Manager."""

    def test_optional_docker_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/docker/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalSurveillance:
    """Surveillance."""

    def test_optional_surveillance_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/surveillance/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalGallery:
    """Gallery."""

    def test_optional_gallery_albums(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/gallery/albums")
        _skip_if_not_installed(is_j)
        assert code == 200

    def test_optional_gallery_stats(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/gallery/stats")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalAIChat:
    """AI Assistant."""

    def test_optional_aichat_config(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/aichat/config")
        _skip_if_not_installed(is_j)
        assert code == 200

    def test_optional_aichat_conversations(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/aichat/conversations")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalPrinter:
    """Print Server."""

    def test_optional_printer_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/printer/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalSharing:
    """File Sharing (Samba)."""

    def test_optional_samba_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/storage/samba/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalDLNA:
    """DLNA."""

    def test_optional_dlna_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/dlna/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalWireguard:
    """VPN (WireGuard)."""

    def test_optional_wireguard_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/wireguard/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalAntivirus:
    """Antivirus (ClamAV)."""

    def test_optional_antivirus_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/antivirus/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalWebsites:
    """Websites."""

    def test_optional_websites_list(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/websites/")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalDDNS:
    """Dynamic DNS."""

    def test_optional_ddns_config(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/ddns/config")
        # DDNS is a core import but may fail — check JSON
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalCloudBackup:
    """Cloud Backup."""

    def test_optional_cloud_providers(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/cloud-backup/providers")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalRAID:
    """RAID / LVM."""

    def test_optional_raid_arrays(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/raid/arrays")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalCron:
    """Scheduler."""

    def test_optional_cron_jobs(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/cron/jobs")
        _skip_if_not_installed(is_j)
        # 503 = dependency not installed (crontab), still means blueprint works
        assert code in (200, 503)


class TestOptionalUPS:
    """UPS."""

    def test_optional_ups_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/ups/status")
        _skip_if_not_installed(is_j)
        # 503 = dependency not installed (nut-client), still means blueprint works
        assert code in (200, 503)


class TestOptionalBuilder:
    """Image Builder."""

    def test_optional_builder_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/builder/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalFlasher:
    """USB Creator."""

    def test_optional_flasher_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/flasher/status")
        _skip_if_not_installed(is_j)
        assert code == 200

    def test_optional_flasher_drives(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/flasher/drives")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalDiskRepair:
    """Disk Repair."""

    def test_optional_diskrepair_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/diskrepair/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalRollback:
    """Rollback."""

    def test_optional_rollback_snapshots(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/rollback/snapshots")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalEditor:
    """Document Editor."""

    def test_optional_editor_formats(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/editor/formats")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalRemoteLog:
    """Remote Logs."""

    def test_optional_remotelog_config(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/remote-log/config")
        _skip_if_not_installed(is_j)
        assert code == 200

    def test_optional_remotelog_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/remote-log/status")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalFamilyHub:
    """Family Hub."""

    def test_optional_familyhub_posts(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/familyhub/posts")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalStickyNotes:
    """Sticky Notes."""

    def test_optional_notes_list(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/notes/")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalTickets:
    """Tickets."""

    def test_optional_tickets_projects(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/tickets/projects")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalDomains:
    """Domains & SSL."""

    def test_optional_domains_list(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/domains-mgr/list")
        _skip_if_not_installed(is_j)
        assert code == 200


class TestOptionalInstaller:
    """System Installer."""

    def test_optional_installer_status(self, base_url, auth_token):
        code, is_j, data = _check_endpoint(base_url, auth_token, "GET", "/api/installer/status")
        _skip_if_not_installed(is_j)
        assert code == 200
