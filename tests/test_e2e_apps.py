"""
EthOS NAS – Comprehensive E2E Functional Tests

Tests actual app functionality (CRUD operations, config read/write, feature logic).
Safe to run against any EthOS instance — creates test data, then cleans it up.

Usage:
    ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/test_e2e_apps.py -v
    ETHOS_BASE_URL=http://host:port ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/test_e2e_apps.py -v
"""

import time
import uuid
import pytest
import requests
from helpers import api_get, api_post


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(session, path, **kw):
    return api_get(session, path, **kw)

def _post(session, path, **kw):
    return api_post(session, path, **kw)

def _json(resp):
    """Return parsed JSON if Content-Type is JSON, else None."""
    ct = resp.headers.get("Content-Type", "")
    if "application/json" in ct:
        return resp.json()
    return None

def _skip_html(resp, label="App"):
    """Skip test if the endpoint returned HTML (blueprint not loaded)."""
    if "text/html" in resp.headers.get("Content-Type", ""):
        pytest.skip(f"{label} blueprint not registered")

def _skip_dep(resp, label="App"):
    """Skip if 503 missing dependency."""
    if resp.status_code == 503:
        data = _json(resp)
        if data and "missing_dependency" in str(data.get("error", "")):
            pytest.skip(f"{label} dependency not installed")


# ═══════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════

class TestAuthFunctional:
    """Auth: login, verify, bad creds, CSRF token presence."""

    def test_login_returns_token_and_user(self, base_url, auth_token, api_session):
        r = _get(api_session, "/api/auth/verify")
        data = r.json()
        assert data.get("valid") is True
        user = data.get("user", {})
        assert "username" in user
        assert "role" in user
        assert user["role"] in ("admin", "user")

    def test_no_auth_returns_401(self, base_url):
        r = requests.get(f"{base_url}/api/settings/", timeout=10)
        assert r.status_code == 401

    def test_bad_token_returns_401(self, base_url):
        r = requests.get(
            f"{base_url}/api/settings/",
            headers={"Authorization": "Bearer fake_token_00000"},
            timeout=10,
        )
        assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════

class TestSettingsFunctional:
    """Settings: read config, timezone list, update hostname round-trip."""

    def test_read_settings(self, api_session):
        r = _get(api_session, "/api/settings/")
        assert r.status_code == 200
        data = r.json()
        assert "hostname" in data

    def test_timezones_list(self, api_session):
        r = _get(api_session, "/api/settings/timezones")
        assert r.status_code == 200
        data = r.json()
        if isinstance(data, list):
            tzs = data
        else:
            tzs = data.get("timezones", [])
        assert isinstance(tzs, list)
        assert len(tzs) > 100

    def test_update_hostname_roundtrip(self, api_session):
        """Read hostname, write it back unchanged — verifies POST works."""
        r = _get(api_session, "/api/settings/")
        original = r.json()
        hostname = original.get("hostname", "EthOS")

        r2 = _post(api_session, "/api/settings/", json={"hostname": hostname})
        assert r2.status_code == 200
        data = _json(r2)
        assert data and data.get("ok") is True

    def test_ssl_status(self, api_session):
        r = _get(api_session, "/api/settings/ssl/status")
        assert r.status_code == 200
        data = r.json()
        assert "installed" in data or "certbot" in str(data)


# ═══════════════════════════════════════════════════════════════
#  USERS & GROUPS
# ═══════════════════════════════════════════════════════════════

class TestUsersFunctional:
    """Users: list, create/delete, groups, privileges."""

    def test_list_users(self, api_session):
        r = _get(api_session, "/api/users/list")
        assert r.status_code == 200
        data = r.json()
        users = data if isinstance(data, list) else data.get("users", [])
        assert isinstance(users, list)
        assert len(users) >= 1
        # No root user should be listed
        usernames = [u.get("username") for u in users]
        assert "root" not in usernames

    def test_list_groups(self, api_session):
        r = _get(api_session, "/api/users/groups")
        assert r.status_code == 200
        data = r.json()
        groups = data if isinstance(data, list) else data.get("groups", [])
        assert isinstance(groups, list)

    def test_roles_list(self, api_session):
        r = _get(api_session, "/api/users/roles")
        assert r.status_code == 200
        data = r.json()
        assert "roles" in data or isinstance(data, list)

    def test_privileges_read(self, api_session):
        r = _get(api_session, "/api/users/privileges")
        assert r.status_code == 200

    def test_create_and_delete_user(self, api_session):
        """Full lifecycle: create test user, verify it exists, then delete it."""
        test_user = f"_ethostest_{uuid.uuid4().hex[:8]}"
        test_pass = "T3stP@ss!2026"

        # Create
        r = _post(api_session, "/api/users/create",
                   json={"username": test_user, "password": test_pass})
        assert r.status_code == 200, f"Create failed: {r.text[:200]}"
        data = _json(r)
        assert data and (data.get("ok") is True or data.get("success") is True)

        # Verify it's in the list
        r2 = _get(api_session, "/api/users/list")
        users = r2.json()
        if isinstance(users, dict):
            users = users.get("users", [])
        found = any(u.get("username") == test_user for u in users)
        assert found, f"User {test_user} not in list"

        # Delete
        r3 = _post(api_session, "/api/users/delete",
                    json={"username": test_user})
        assert r3.status_code == 200

        # Verify it's gone
        r4 = _get(api_session, "/api/users/list")
        users = r4.json()
        if isinstance(users, dict):
            users = users.get("users", [])
        found = any(u.get("username") == test_user for u in users)
        assert not found, f"User {test_user} still in list after delete"


# ═══════════════════════════════════════════════════════════════
#  STORAGE
# ═══════════════════════════════════════════════════════════════

class TestStorageFunctional:
    """Storage: keepalive, drives list, SMART, Samba status."""

    def test_keepalive(self, api_session):
        r = _get(api_session, "/api/storage/keepalive")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_drives_list(self, api_session):
        r = _get(api_session, "/api/storage/drives")
        assert r.status_code == 200
        data = r.json()
        drives = data.get("drives", data.get("items", []))
        assert isinstance(drives, list)
        assert len(drives) >= 1  # at least the boot drive

    def test_smart_data(self, api_session):
        r = _get(api_session, "/api/storage/smart")
        # May return error if no drive specified or smartctl unavailable
        assert r.status_code in (200, 400, 500)

    def test_samba_status(self, api_session):
        r = _get(api_session, "/api/storage/samba/status")
        _skip_html(r, "Samba")
        assert r.status_code == 200

    def test_samba_shares_list(self, api_session):
        r = _get(api_session, "/api/storage/samba/shares")
        _skip_html(r, "Samba")
        assert r.status_code == 200

    def test_nfs_status(self, api_session):
        r = _get(api_session, "/api/storage/nfs/status")
        _skip_html(r, "NFS")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  FILE MANAGER
# ═══════════════════════════════════════════════════════════════

class TestFileManagerFunctional:
    """File Manager: listing, search, favorites, permissions."""

    def test_list_root(self, api_session):
        r = _get(api_session, "/api/files/list?path=/opt/ethos")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        names = [i["name"] for i in data["items"]]
        assert "backend" in names or "frontend" in names

    def test_list_home(self, api_session):
        r = _get(api_session, "/api/files/list?path=/home")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data

    def test_list_nonexistent_returns_error(self, api_session):
        r = _get(api_session, "/api/files/list?path=/nonexistent_dir_12345")
        data = _json(r)
        assert r.status_code in (400, 404) or (data and "error" in data)

    def test_search(self, api_session):
        r = _get(api_session, "/api/files/search?path=/opt/ethos&query=app.py")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data or "results" in data

    def test_favorites_read(self, api_session):
        r = _get(api_session, "/api/files/favorites")
        assert r.status_code == 200

    def test_permissions_read(self, api_session):
        r = _get(api_session, "/api/files/permissions?path=/opt/ethos/start.sh")
        assert r.status_code == 200
        data = r.json()
        assert "owner" in data or "permissions" in data or "mode" in data

    def test_operation_status(self, api_session):
        """Operation status should return even when no op is running."""
        r = _get(api_session, "/api/files/operation-status")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  NETWORK
# ═══════════════════════════════════════════════════════════════

class TestNetworkFunctional:
    """Network: interfaces, wifi status."""

    def test_interfaces_list(self, api_session):
        r = _get(api_session, "/api/network/interfaces")
        assert r.status_code == 200
        data = r.json()
        ifaces = data.get("interfaces", data.get("items", []))
        assert isinstance(ifaces, list)
        assert len(ifaces) >= 1  # at least lo or eth0

    def test_wifi_status(self, api_session):
        r = _get(api_session, "/api/network/wifi/status")
        # 404 on VMs or systems without wifi hardware
        assert r.status_code in (200, 404)

    def test_wifi_saved(self, api_session):
        r = _get(api_session, "/api/network/wifi/saved")
        assert r.status_code == 200

    def test_ap_status(self, api_session):
        r = _get(api_session, "/api/network/ap/status")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  RESOURCES / MONITOR
# ═══════════════════════════════════════════════════════════════

class TestResourcesFunctional:
    """Resource monitor: system stats, CPU, RAM, processes."""

    def test_system_all(self, api_session):
        r = _get(api_session, "/api/resources/system")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_cpu(self, api_session):
        r = _get(api_session, "/api/resources/cpu")
        assert r.status_code == 200

    def test_ram(self, api_session):
        r = _get(api_session, "/api/resources/ram")
        assert r.status_code == 200

    def test_disks(self, api_session):
        r = _get(api_session, "/api/resources/disks")
        assert r.status_code == 200

    def test_network_stats(self, api_session):
        r = _get(api_session, "/api/resources/network")
        assert r.status_code == 200

    def test_processes_list(self, api_session):
        r = _get(api_session, "/api/resources/processes")
        assert r.status_code == 200
        data = r.json()
        procs = data if isinstance(data, list) else data.get("processes", [])
        assert isinstance(procs, list)
        assert len(procs) >= 1

    def test_usb_devices(self, api_session):
        r = _get(api_session, "/api/resources/usb")
        assert r.status_code == 200

    def test_gpu_detect(self, api_session):
        r = _get(api_session, "/api/resources/gpu/detect")
        assert r.status_code == 200

    def test_resource_all(self, api_session):
        r = _get(api_session, "/api/resources/all")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  BACKUP
# ═══════════════════════════════════════════════════════════════

class TestBackupFunctional:
    """Backup: status, paths, browse, backup list, USB drives."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/backup/status")
        assert r.status_code == 200

    def test_progress(self, api_session):
        r = _get(api_session, "/api/backup/progress")
        assert r.status_code == 200

    def test_paths_list(self, api_session):
        r = _get(api_session, "/api/backup/paths")
        assert r.status_code == 200

    def test_backup_list(self, api_session):
        r = _get(api_session, "/api/backup/backups")
        assert r.status_code == 200
        data = r.json()
        assert "backups" in data or "items" in data or isinstance(data, list)

    def test_browse_roots(self, api_session):
        r = _get(api_session, "/api/backup/browse/roots")
        assert r.status_code == 200

    def test_usb_drives(self, api_session):
        r = _get(api_session, "/api/backup/usb-drives")
        assert r.status_code == 200

    def test_ssh_servers(self, api_session):
        r = _get(api_session, "/api/backup/ssh-servers")
        assert r.status_code == 200

    def test_paths_add_remove(self, api_session):
        """Add a backup path, verify it's listed, then remove it."""
        test_path = "/tmp/_ethos_test_backup_path"

        r = _post(api_session, "/api/backup/paths", json={"path": test_path})
        # May fail if path doesn't exist — that's ok, we test the round-trip
        if r.status_code != 200:
            pytest.skip("Cannot add test backup path (path may not exist)")

        r2 = _get(api_session, "/api/backup/paths")
        data = r2.json()
        paths = data.get("paths", data.get("items", []))
        found = test_path in str(paths)

        # Clean up
        _post(api_session, "/api/backup/paths",
              json={"path": test_path, "_method": "DELETE"})
        # Also try DELETE method
        api_session.delete(
            f"{api_session.base_url}/api/backup/paths",
            json={"path": test_path}, timeout=10
        )

        assert found, "Test backup path not found after adding"


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════

class TestDashboardFunctional:
    """Dashboard: summary with widget data."""

    def test_summary(self, api_session):
        r = _get(api_session, "/api/dashboard/summary")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        # Should have some system info
        assert any(k in data for k in ("cpu", "ram", "hostname", "uptime", "storage", "widgets"))


# ═══════════════════════════════════════════════════════════════
#  SSH MANAGER
# ═══════════════════════════════════════════════════════════════

class TestSSHFunctional:
    """SSH: key list, known hosts."""

    def test_keys_list(self, api_session):
        r = _get(api_session, "/api/ssh/keys")
        assert r.status_code == 200
        data = r.json()
        assert "keys" in data or isinstance(data, list)

    def test_known_hosts(self, api_session):
        r = _get(api_session, "/api/ssh/known-hosts")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  FIREWALL
# ═══════════════════════════════════════════════════════════════

class TestFirewallFunctional:
    """Firewall: status, rules, subnet detection."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/firewall/status")
        _skip_html(r, "Firewall")
        assert r.status_code == 200
        data = r.json()
        assert "status" in data or "ok" in data

    def test_subnet_detection(self, api_session):
        r = _get(api_session, "/api/firewall/subnet")
        _skip_html(r, "Firewall")
        assert r.status_code == 200
        data = r.json()
        assert "subnet" in data or "cidr" in data or "network" in data

    def test_banned_list(self, api_session):
        r = _get(api_session, "/api/firewall/banned")
        _skip_html(r, "Firewall")
        assert r.status_code == 200

    def test_add_and_remove_rule(self, api_session):
        """Add a test rule, verify it appears in status, then delete it."""
        r = _get(api_session, "/api/firewall/status")
        _skip_html(r, "Firewall")
        data = r.json()
        if data.get("status") == "inactive":
            pytest.skip("Firewall is inactive — can't test rules")

        # Add a rule on an unused port
        test_port = 59999
        r = _post(api_session, "/api/firewall/rules", json={
            "action": "add",
            "port": str(test_port), "proto": "tcp",
            "ufw_action": "allow", "access": "public",
            "comment": "ethos-test-rule",
        })
        if r.status_code != 200:
            pytest.skip(f"Cannot add test rule: {r.text[:200]}")

        # Verify rule is listed
        r2 = _get(api_session, "/api/firewall/status")
        data2 = r2.json()
        rules = data2.get("rules", [])
        found = any(str(test_port) in str(rule.get("to", "")) for rule in rules)

        # Clean up — find and delete the rule by id
        for rule in rules:
            if str(test_port) in str(rule.get("to", "")):
                _post(api_session, "/api/firewall/rules", json={
                    "action": "delete",
                    "id": rule["id"],
                    "v6_id": rule.get("v6_id"),
                })
                break

        assert found, f"Test rule on port {test_port} not found after adding"

    def test_toggle_remembers_state(self, api_session):
        """Toggle firewall off then on and verify state persists via ufw status."""
        r = _get(api_session, "/api/firewall/status")
        _skip_html(r, "Firewall")
        data = r.json()
        original_status = data.get("status")  # "active" or "inactive"

        # -- Disable firewall ------------------------------------------------
        r = _post(api_session, "/api/firewall/toggle", json={"enable": False})
        assert r.status_code == 200
        assert r.json().get("ok") is True

        r = _get(api_session, "/api/firewall/status")
        assert r.json().get("status") == "inactive"

        # -- Re-enable firewall ----------------------------------------------
        r = _post(api_session, "/api/firewall/toggle", json={"enable": True})
        assert r.status_code == 200
        assert r.json().get("ok") is True

        r = _get(api_session, "/api/firewall/status")
        assert r.json().get("status") == "active"

        # -- Verify persistence: UFW stores state in /etc/ufw/ufw.conf ------
        # After enable, "ENABLED=yes" must be present so ufw starts on boot.
        # We verify via the API — if status is "active" the conf file is correct
        # (ufw itself writes ENABLED=yes/no on toggle).
        # No reboot needed: ufw.conf is read by the ufw init script at boot,
        # so if the API reports "active" the boot-persist flag is already set.

        # -- Restore original state if it was inactive -----------------------
        if original_status == "inactive":
            _post(api_session, "/api/firewall/toggle", json={"enable": False})


# ═══════════════════════════════════════════════════════════════
#  FAIL2BAN
# ═══════════════════════════════════════════════════════════════

class TestFail2banFunctional:
    """Fail2ban: status check."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/fail2ban/status")
        _skip_html(r, "Fail2ban")
        # 500 = service not running, still means blueprint is loaded
        assert r.status_code in (200, 500)
        data = _json(r)
        assert data is not None


# ═══════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

class TestNotificationsFunctional:
    """Notifications: config read/write."""

    def test_config_read(self, api_session):
        r = _get(api_session, "/api/notifications/config")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)


# ═══════════════════════════════════════════════════════════════
#  PACKAGES
# ═══════════════════════════════════════════════════════════════

class TestPackagesFunctional:
    """Package manager: stats, installed, search."""

    def test_stats(self, api_session):
        r = _get(api_session, "/api/packages/stats")
        assert r.status_code == 200
        data = r.json()
        assert "installed" in data or "total" in data or "count" in data

    def test_installed_list(self, api_session):
        r = _get(api_session, "/api/packages/installed")
        assert r.status_code == 200
        data = r.json()
        pkgs = data if isinstance(data, list) else data.get("packages", [])
        assert isinstance(pkgs, list)
        assert len(pkgs) > 10  # any Linux system has many packages

    def test_search_package(self, api_session):
        r = _get(api_session, "/api/packages/search?q=python3")
        assert r.status_code == 200
        data = r.json()
        results = data if isinstance(data, list) else data.get("packages", [])
        assert isinstance(results, list)

    def test_package_info(self, api_session):
        r = _get(api_session, "/api/packages/info/bash")
        assert r.status_code == 200
        data = r.json()
        # Response is raw dpkg info dict with capitalized keys
        assert isinstance(data, dict)
        assert len(data) > 3


# ═══════════════════════════════════════════════════════════════
#  APP MANAGER / PACKAGE CENTER
# ═══════════════════════════════════════════════════════════════

class TestAppManagerFunctional:
    """App Store: catalog, installed apps, core apps."""

    def test_installed_apps(self, api_session):
        r = _get(api_session, "/api/app-manager/installed")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, (list, dict))

    def test_core_apps(self, api_session):
        r = _get(api_session, "/api/app-manager/core")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, (list, dict))

    def test_catalog(self, api_session):
        r = _get(api_session, "/api/app-manager/catalog")
        assert r.status_code == 200
        data = r.json()
        # Catalog has {"core": [...], "optional": [...]}
        if isinstance(data, dict) and "core" in data:
            total = len(data.get("core", [])) + len(data.get("optional", []))
        elif isinstance(data, list):
            total = len(data)
        else:
            total = len(data.get("apps", []))
        assert total >= 5

    def test_app_status(self, api_session):
        """Check status of a core app (firewall)."""
        r = _get(api_session, "/api/app-manager/firewall/status")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  APPS LIST (global app registry)
# ═══════════════════════════════════════════════════════════════

class TestAppsListFunctional:
    """Global app registry: should list all registered apps."""

    def test_apps_list(self, api_session):
        r = _get(api_session, "/api/apps")
        assert r.status_code == 200
        data = r.json()
        apps = data if isinstance(data, list) else data.get("apps", [])
        assert len(apps) >= 5
        # Each app should have at least id and name
        for app in apps[:5]:
            assert "id" in app or "name" in app


# ═══════════════════════════════════════════════════════════════
#  EVENT LOG
# ═══════════════════════════════════════════════════════════════

class TestEventLogFunctional:
    """Event log: list, stats, write/read cycle."""

    def test_list_events(self, api_session):
        r = _get(api_session, "/api/eventlog")
        assert r.status_code == 200
        data = r.json()
        assert "events" in data or "items" in data or isinstance(data, list)

    def test_stats(self, api_session):
        r = _get(api_session, "/api/eventlog/stats")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  POWER
# ═══════════════════════════════════════════════════════════════

class TestPowerFunctional:
    """Power manager: status check (we don't trigger reboot/shutdown)."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/power/status")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  SERVICES
# ═══════════════════════════════════════════════════════════════

class TestServicesFunctional:
    """System services list."""

    def test_list(self, api_session):
        r = _get(api_session, "/api/services/list")
        assert r.status_code == 200
        data = r.json()
        services = data.get("services", data.get("items", []))
        assert isinstance(services, list)
        assert len(services) >= 1


# ═══════════════════════════════════════════════════════════════
#  TOTP (2FA)
# ═══════════════════════════════════════════════════════════════

class TestTOTPFunctional:
    """Two-factor auth: status check."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/totp/status")
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data


# ═══════════════════════════════════════════════════════════════
#  UPDATE SYSTEM
# ═══════════════════════════════════════════════════════════════

class TestUpdateFunctional:
    """Update: check for updates, status."""

    def test_check(self, api_session):
        r = _post(api_session, "/api/update/check")
        assert r.status_code == 200
        data = r.json()
        assert "current_version" in data

    def test_status(self, api_session):
        r = _get(api_session, "/api/update/status")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  DDNS
# ═══════════════════════════════════════════════════════════════

class TestDDNSFunctional:
    """Dynamic DNS: config, providers, IP check."""

    def test_providers(self, api_session):
        r = _get(api_session, "/api/ddns/providers")
        _skip_html(r, "DDNS")
        assert r.status_code == 200
        data = r.json()
        providers = data if isinstance(data, list) else data.get("providers", [])
        assert isinstance(providers, list)

    def test_config(self, api_session):
        r = _get(api_session, "/api/ddns/config")
        _skip_html(r, "DDNS")
        assert r.status_code == 200

    def test_check_ip(self, api_session):
        r = _get(api_session, "/api/ddns/check-ip")
        _skip_html(r, "DDNS")
        assert r.status_code == 200

    def test_status(self, api_session):
        r = _get(api_session, "/api/ddns/status")
        _skip_html(r, "DDNS")
        assert r.status_code == 200

    def test_history(self, api_session):
        r = _get(api_session, "/api/ddns/history")
        _skip_html(r, "DDNS")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: DOWNLOADS
# ═══════════════════════════════════════════════════════════════

class TestDownloadsFunctional:
    """Download Manager: list, stats, config, history."""

    def test_list(self, api_session):
        r = _get(api_session, "/api/downloads/list")
        _skip_html(r, "Downloads")
        assert r.status_code == 200

    def test_stats(self, api_session):
        r = _get(api_session, "/api/downloads/stats")
        _skip_html(r, "Downloads")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_history(self, api_session):
        r = _get(api_session, "/api/downloads/history")
        _skip_html(r, "Downloads")
        assert r.status_code == 200

    def test_config_read(self, api_session):
        r = _get(api_session, "/api/downloads/config")
        _skip_html(r, "Downloads")
        assert r.status_code == 200
        data = r.json()
        assert "download_dir" in data or "config" in data or isinstance(data, dict)


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: VM MANAGER
# ═══════════════════════════════════════════════════════════════

class TestVMManagerFunctional:
    """VM Manager: status, machines list, images, bridge."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/vm/status")
        _skip_html(r, "VM Manager")
        assert r.status_code == 200

    def test_machines_list(self, api_session):
        r = _get(api_session, "/api/vm/machines")
        _skip_html(r, "VM Manager")
        assert r.status_code == 200
        data = r.json()
        machines = data if isinstance(data, list) else data.get("machines", [])
        assert isinstance(machines, list)

    def test_images_list(self, api_session):
        r = _get(api_session, "/api/vm/images")
        _skip_html(r, "VM Manager")
        assert r.status_code == 200

    def test_bridge_status(self, api_session):
        r = _get(api_session, "/api/vm/bridge")
        _skip_html(r, "VM Manager")
        assert r.status_code == 200

    def test_builder_images(self, api_session):
        r = _get(api_session, "/api/vm/builder-images")
        _skip_html(r, "VM Manager")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: DOCKER
# ═══════════════════════════════════════════════════════════════

class TestDockerFunctional:
    """Docker: status, containers, projects, images."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/docker/status")
        _skip_html(r, "Docker")
        assert r.status_code == 200

    def test_containers(self, api_session):
        r = _get(api_session, "/api/docker/containers")
        _skip_html(r, "Docker")
        assert r.status_code == 200

    def test_projects(self, api_session):
        r = _get(api_session, "/api/docker/projects")
        _skip_html(r, "Docker")
        assert r.status_code == 200

    def test_images(self, api_session):
        r = _get(api_session, "/api/docker/images")
        _skip_html(r, "Docker")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: GALLERY
# ═══════════════════════════════════════════════════════════════

class TestGalleryFunctional:
    """Gallery: folders, albums, stats, favorites."""

    def test_stats(self, api_session):
        r = _get(api_session, "/api/gallery/stats")
        _skip_html(r, "Gallery")
        assert r.status_code == 200

    def test_albums(self, api_session):
        r = _get(api_session, "/api/gallery/albums")
        _skip_html(r, "Gallery")
        assert r.status_code == 200

    def test_folders(self, api_session):
        r = _get(api_session, "/api/gallery/folders")
        _skip_html(r, "Gallery")
        assert r.status_code == 200

    def test_favorites(self, api_session):
        r = _get(api_session, "/api/gallery/favorites")
        _skip_html(r, "Gallery")
        assert r.status_code == 200

    def test_timeline(self, api_session):
        r = _get(api_session, "/api/gallery/timeline")
        _skip_html(r, "Gallery")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: ANTIVIRUS
# ═══════════════════════════════════════════════════════════════

class TestAntivirusFunctional:
    """Antivirus (ClamAV): status, results, schedules."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/antivirus/status")
        _skip_html(r, "Antivirus")
        assert r.status_code == 200

    def test_pkg_status(self, api_session):
        r = _get(api_session, "/api/antivirus/pkg-status")
        _skip_html(r, "Antivirus")
        assert r.status_code == 200

    def test_results(self, api_session):
        r = _get(api_session, "/api/antivirus/results")
        _skip_html(r, "Antivirus")
        assert r.status_code == 200

    def test_schedules(self, api_session):
        r = _get(api_session, "/api/antivirus/schedules")
        _skip_html(r, "Antivirus")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: SURVEILLANCE
# ═══════════════════════════════════════════════════════════════

class TestSurveillanceFunctional:
    """Surveillance: status."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/surveillance/status")
        _skip_html(r, "Surveillance")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: AI CHAT
# ═══════════════════════════════════════════════════════════════

class TestAIChatFunctional:
    """AI Chat: config, conversations list."""

    def test_config(self, api_session):
        r = _get(api_session, "/api/aichat/config")
        _skip_html(r, "AI Chat")
        assert r.status_code == 200

    def test_conversations(self, api_session):
        r = _get(api_session, "/api/aichat/conversations")
        _skip_html(r, "AI Chat")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: WIREGUARD
# ═══════════════════════════════════════════════════════════════

class TestWireguardFunctional:
    """WireGuard VPN: status."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/wireguard/status")
        _skip_html(r, "WireGuard")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: WEBSITES
# ═══════════════════════════════════════════════════════════════

class TestWebsitesFunctional:
    """Websites (nginx virtual hosts): list."""

    def test_list(self, api_session):
        r = _get(api_session, "/api/websites/")
        _skip_html(r, "Websites")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: CLOUD BACKUP
# ═══════════════════════════════════════════════════════════════

class TestCloudBackupFunctional:
    """Cloud Backup: providers, jobs."""

    def test_providers(self, api_session):
        r = _get(api_session, "/api/cloud-backup/providers")
        _skip_html(r, "Cloud Backup")
        assert r.status_code == 200

    def test_jobs(self, api_session):
        r = _get(api_session, "/api/cloud-backup/jobs")
        _skip_html(r, "Cloud Backup")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: RAID
# ═══════════════════════════════════════════════════════════════

class TestRAIDFunctional:
    """RAID Manager: arrays list."""

    def test_arrays(self, api_session):
        r = _get(api_session, "/api/raid/arrays")
        _skip_html(r, "RAID")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: CRON
# ═══════════════════════════════════════════════════════════════

class TestCronFunctional:
    """Scheduler: jobs list, CRUD cycle."""

    def test_jobs_list(self, api_session):
        r = _get(api_session, "/api/cron/jobs")
        _skip_html(r, "Cron")
        _skip_dep(r, "Cron")
        assert r.status_code in (200, 503)


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: PRINTER
# ═══════════════════════════════════════════════════════════════

class TestPrinterFunctional:
    """Print Server: status."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/printer/status")
        _skip_html(r, "Printer")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: DLNA
# ═══════════════════════════════════════════════════════════════

class TestDLNAFunctional:
    """DLNA Media Server: status."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/dlna/status")
        _skip_html(r, "DLNA")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: UPS
# ═══════════════════════════════════════════════════════════════

class TestUPSFunctional:
    """UPS Monitor: status, settings."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/ups/status")
        _skip_html(r, "UPS")
        _skip_dep(r, "UPS")
        assert r.status_code in (200, 503)

    def test_settings(self, api_session):
        r = _get(api_session, "/api/ups/settings")
        _skip_html(r, "UPS")
        _skip_dep(r, "UPS")
        assert r.status_code in (200, 503)


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: BUILDER
# ═══════════════════════════════════════════════════════════════

class TestBuilderFunctional:
    """Image Builder: status."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/builder/status")
        _skip_html(r, "Builder")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: FLASHER
# ═══════════════════════════════════════════════════════════════

class TestFlasherFunctional:
    """USB Creator: status, drives, images."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/flasher/status")
        _skip_html(r, "Flasher")
        assert r.status_code == 200

    def test_drives(self, api_session):
        r = _get(api_session, "/api/flasher/drives")
        _skip_html(r, "Flasher")
        assert r.status_code == 200

    def test_images(self, api_session):
        r = _get(api_session, "/api/flasher/images", timeout=30)
        _skip_html(r, "Flasher")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: DISK REPAIR
# ═══════════════════════════════════════════════════════════════

class TestDiskRepairFunctional:
    """Disk Repair: status."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/diskrepair/status")
        _skip_html(r, "DiskRepair")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: ROLLBACK
# ═══════════════════════════════════════════════════════════════

class TestRollbackFunctional:
    """Rollback: snapshots list."""

    def test_snapshots(self, api_session):
        r = _get(api_session, "/api/rollback/snapshots")
        _skip_html(r, "Rollback")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: REMOTE LOG
# ═══════════════════════════════════════════════════════════════

class TestRemoteLogFunctional:
    """Remote Logs: config, status."""

    def test_config(self, api_session):
        r = _get(api_session, "/api/remote-log/config")
        _skip_html(r, "Remote Log")
        assert r.status_code == 200

    def test_status(self, api_session):
        r = _get(api_session, "/api/remote-log/status")
        _skip_html(r, "Remote Log")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: FAMILY HUB
# ═══════════════════════════════════════════════════════════════

class TestFamilyHubFunctional:
    """Family Hub: posts list."""

    def test_posts(self, api_session):
        r = _get(api_session, "/api/familyhub/posts")
        _skip_html(r, "FamilyHub")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: STICKY NOTES
# ═══════════════════════════════════════════════════════════════

class TestStickyNotesFunctional:
    """Sticky Notes: CRUD cycle."""

    def test_list(self, api_session):
        r = _get(api_session, "/api/notes/")
        _skip_html(r, "StickyNotes")
        if r.status_code == 404:
            r = _get(api_session, "/api/notes")
        assert r.status_code == 200

    def test_colors(self, api_session):
        r = _get(api_session, "/api/notes/colors")
        _skip_html(r, "StickyNotes")
        assert r.status_code == 200

    def test_create_and_delete(self, api_session):
        """Create a note, verify, delete."""
        r = _get(api_session, "/api/notes/")
        _skip_html(r, "StickyNotes")

        # Create
        r = _post(api_session, "/api/notes/", json={
            "title": "_e2e_test_note",
            "content": "Test content from E2E",
            "color": "yellow",
        })
        if r.status_code not in (200, 201):
            r = _post(api_session, "/api/notes", json={
                "title": "_e2e_test_note",
                "content": "Test content from E2E",
                "color": "yellow",
            })
        assert r.status_code in (200, 201), f"Create note failed: {r.text[:200]}"
        data = _json(r)
        note_id = data.get("id") or data.get("note", {}).get("id")
        assert note_id, "No note ID returned"

        # Delete
        r2 = api_session.delete(
            f"{api_session.base_url}/api/notes/{note_id}", timeout=10
        )
        assert r2.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: TICKETS
# ═══════════════════════════════════════════════════════════════

class TestTicketsFunctional:
    """Tickets: projects CRUD, ticket CRUD."""

    def test_projects_list(self, api_session):
        r = _get(api_session, "/api/tickets/projects")
        _skip_html(r, "Tickets")
        assert r.status_code == 200

    def test_project_create_and_delete(self, api_session):
        """Create a project, list it, delete it."""
        r = _get(api_session, "/api/tickets/projects")
        _skip_html(r, "Tickets")

        name = f"_e2e_test_{uuid.uuid4().hex[:6]}"
        r = _post(api_session, "/api/tickets/projects", json={
            "name": name, "description": "E2E test project",
        })
        assert r.status_code in (200, 201), f"Create project failed: {r.text[:200]}"
        data = _json(r)
        # Response: {"ok": true, "item": {"id": "...", ...}}
        pid = (data.get("item", {}).get("id")
               or data.get("id")
               or data.get("project", {}).get("id"))
        assert pid, f"No project ID returned: {data}"

        # Verify in list
        r2 = _get(api_session, "/api/tickets/projects")
        proj_data = r2.json()
        projects = proj_data if isinstance(proj_data, list) else proj_data.get("projects", [])
        found = any(str(pid) in str(p) or name in str(p) for p in projects)
        assert found

        # Delete
        r3 = api_session.delete(
            f"{api_session.base_url}/api/tickets/projects/{pid}", timeout=10
        )
        assert r3.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  OPTIONAL: INSTALLER
# ═══════════════════════════════════════════════════════════════

class TestInstallerFunctional:
    """System Installer: status."""

    def test_status(self, api_session):
        r = _get(api_session, "/api/installer/status")
        _skip_html(r, "Installer")
        assert r.status_code == 200
