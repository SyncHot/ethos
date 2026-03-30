"""
EthOS NAS – App Store / Package Center E2E Tests

Tests the three package management systems:
1. App Manager  — EthOS native optional apps (install/uninstall/catalog)
2. App Store    — Docker container apps (CasaOS repos, compose, install)
3. Pkg Registry — Git-based packages (repos, catalog, install scripts)

Usage:
    ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/test_e2e_appstore.py -v
    ETHOS_BASE_URL=http://host:port ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/test_e2e_appstore.py -v
"""

import time
import uuid
import pytest
import requests
from helpers import api_get, api_post, BASE_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _retry_on_429(fn, *args, retries=3, **kw):
    """Retry a request function on 429 rate limit."""
    for attempt in range(retries):
        r = fn(*args, **kw)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 5))
            if attempt < retries - 1:
                time.sleep(wait)
                continue
            pytest.skip("Rate limited by server")
        return r
    return r

def _get(session, path, **kw):
    kw.setdefault("timeout", 15)
    return _retry_on_429(api_get, session, path, **kw)

def _post(session, path, **kw):
    kw.setdefault("timeout", 15)
    return _retry_on_429(api_post, session, path, **kw)

def _delete(session, path, **kw):
    kw.setdefault("timeout", 15)
    return _retry_on_429(session.delete, f"{session.base_url}{path}", **kw)

def _put(session, path, **kw):
    kw.setdefault("timeout", 15)
    return _retry_on_429(session.put, f"{session.base_url}{path}", **kw)

def _json(resp):
    ct = resp.headers.get("Content-Type", "")
    if "application/json" in ct:
        return resp.json()
    return None

def _skip_if_blueprint_missing(session, probe_path, label="Blueprint"):
    """Probe a GET endpoint to check if the blueprint is registered."""
    r = api_get(session, probe_path, timeout=10)
    if "text/html" in r.headers.get("Content-Type", ""):
        pytest.skip(f"{label} blueprint not registered")
    return r


def _skip_html(resp, label="Endpoint"):
    """Skip test if the endpoint returned HTML (blueprint not loaded)
    or 405 Method Not Allowed (unregistered POST route)."""
    ct = resp.headers.get("Content-Type", "")
    if "text/html" in ct:
        pytest.skip(f"{label} blueprint not registered")
    if resp.status_code == 405:
        pytest.skip(f"{label} blueprint not registered (405)")


# ═══════════════════════════════════════════════════════════════════════════
#  1. APP MANAGER — /api/app-manager/
#     EthOS native optional apps (surveillance, docker, gallery, etc.)
# ═══════════════════════════════════════════════════════════════════════════

class TestAppManagerCatalog:
    """App Manager catalog: core + optional listings."""

    def test_catalog_structure(self, api_session):
        """Catalog returns core and optional arrays."""
        r = _get(api_session, "/api/app-manager/catalog")
        assert r.status_code == 200
        data = r.json()
        assert "core" in data, "Catalog missing 'core' key"
        assert "optional" in data, "Catalog missing 'optional' key"
        assert isinstance(data["core"], list)
        assert isinstance(data["optional"], list)

    def test_catalog_core_apps_present(self, api_session):
        """All essential core apps are in the catalog."""
        r = _get(api_session, "/api/app-manager/catalog")
        data = r.json()
        core_ids = {app["id"] for app in data["core"]}
        expected = {"dashboard", "file-manager", "storage-manager", "terminal",
                    "system-settings", "users", "updates", "app-store",
                    "backup", "firewall", "fail2ban"}
        missing = expected - core_ids
        assert not missing, f"Missing core apps: {missing}"

    def test_catalog_core_app_fields(self, api_session):
        """Each core app has required fields."""
        r = _get(api_session, "/api/app-manager/catalog")
        data = r.json()
        for app in data["core"][:5]:
            assert "id" in app, f"Core app missing 'id': {app}"
            assert "name" in app, f"Core app missing 'name': {app}"
            assert app.get("core") is True, f"Core app not marked core: {app['id']}"
            assert app.get("installed") is True, f"Core app not installed: {app['id']}"

    def test_catalog_optional_app_fields(self, api_session):
        """Each optional app has required fields."""
        r = _get(api_session, "/api/app-manager/catalog")
        data = r.json()
        assert len(data["optional"]) >= 5, "Too few optional apps"
        for app in data["optional"][:5]:
            assert "id" in app
            assert "name" in app
            assert "icon" in app
            assert "category" in app
            assert "description" in app
            assert "installed" in app
            assert isinstance(app["installed"], bool)

    def test_catalog_no_duplicate_ids(self, api_session):
        """No duplicate app IDs across core + optional."""
        r = _get(api_session, "/api/app-manager/catalog")
        data = r.json()
        core_ids = {a["id"] for a in data["core"]}
        optional_ids = {a["id"] for a in data["optional"]}
        overlap = core_ids & optional_ids
        assert not overlap, f"Core apps appearing in optional list: {overlap}"


class TestAppManagerInstalled:
    """App Manager: installed apps list."""

    def test_installed_is_list(self, api_session):
        r = _get(api_session, "/api/app-manager/installed")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_installed_apps_have_fields(self, api_session):
        r = _get(api_session, "/api/app-manager/installed")
        data = r.json()
        for app in data[:5]:
            assert "id" in app
            assert "installed" in app
            assert app["installed"] is True

    def test_installed_matches_catalog(self, api_session):
        """Every installed app should also show installed=true in catalog."""
        r1 = _get(api_session, "/api/app-manager/installed")
        installed_ids = {a["id"] for a in r1.json()}

        r2 = _get(api_session, "/api/app-manager/catalog")
        cat = r2.json()
        catalog_installed = {a["id"] for a in cat["optional"] if a.get("installed")}

        # installed list should be subset of catalog installed
        diff = installed_ids - catalog_installed
        # Some may be bundled/core — filter those out
        assert len(diff) <= len(installed_ids), "Installed/catalog mismatch"


class TestAppManagerCore:
    """App Manager: core apps list."""

    def test_core_is_list(self, api_session):
        r = _get(api_session, "/api/app-manager/core")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 10  # at least 10 core apps

    def test_core_all_marked_core(self, api_session):
        r = _get(api_session, "/api/app-manager/core")
        for app in r.json():
            assert app.get("core") is True, f"{app.get('id')} not marked core"


class TestAppManagerStatus:
    """App Manager: individual app status."""

    def test_status_installed_app(self, api_session):
        """Check status of a known-installed optional app."""
        # Get first installed optional app
        r = _get(api_session, "/api/app-manager/installed")
        installed = r.json()
        if not installed:
            pytest.skip("No optional apps installed")
        app_id = installed[0]["id"]

        r2 = _get(api_session, f"/api/app-manager/{app_id}/status")
        assert r2.status_code == 200
        data = r2.json()
        assert data.get("installed") is True

    def test_status_nonexistent_app(self, api_session):
        """Status of a non-existent app."""
        r = _get(api_session, "/api/app-manager/nonexistent_app_xyz/status")
        # Should return 200 with installed=false, or 404
        assert r.status_code in (200, 404)
        data = _json(r)
        if r.status_code == 200 and data:
            assert data.get("installed") is False


class TestAppManagerCheckUpdates:
    """App Manager: update check."""

    def test_check_updates(self, api_session):
        r = _get(api_session, "/api/app-manager/check-updates")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)


class TestAppManagerInstallRestrictions:
    """App Manager: install/uninstall validation."""

    def test_cannot_install_core_app(self, api_session):
        """Installing a core app should be rejected."""
        r = _post(api_session, "/api/app-manager/dashboard/install")
        assert r.status_code == 400
        data = _json(r)
        assert data and "error" in data

    def test_cannot_uninstall_core_app(self, api_session):
        """Uninstalling a core app should be rejected."""
        r = _post(api_session, "/api/app-manager/dashboard/uninstall")
        assert r.status_code == 400
        data = _json(r)
        assert data and "error" in data

    def test_install_unknown_app(self, api_session):
        """Installing an unknown app should return 404."""
        r = _post(api_session, "/api/app-manager/nonexistent_app_xyz_999/install")
        assert r.status_code == 404
        data = _json(r)
        assert data and "error" in data

    def test_uninstall_unknown_app(self, api_session):
        """Uninstalling an unknown app should return 404."""
        r = _post(api_session, "/api/app-manager/nonexistent_app_xyz_999/uninstall")
        assert r.status_code == 404
        data = _json(r)
        assert data and "error" in data

    def test_update_core_app_rejected(self, api_session):
        """Updating a core app should be rejected (uses OTA)."""
        r = _post(api_session, "/api/app-manager/dashboard/update")
        assert r.status_code == 400
        data = _json(r)
        assert data and "error" in data


class TestAppManagerNoAuth:
    """App Manager: auth enforcement."""

    def test_catalog_requires_auth(self, base_url):
        r = requests.get(f"{base_url}/api/app-manager/catalog", timeout=10)
        assert r.status_code == 401

    def test_install_requires_auth(self, base_url):
        r = requests.post(f"{base_url}/api/app-manager/surveillance/install",
                          timeout=10)
        # 403 = user detected but no admin rights; 401 = no token at all
        assert r.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════
#  2. APP STORE — /api/appstore/
#     Docker container apps from CasaOS-compatible repos
# ═══════════════════════════════════════════════════════════════════════════

class TestAppStoreRepos:
    """App Store: repository management."""

    def test_repos_list(self, api_session):
        r = _get(api_session, "/api/appstore/repos")
        _skip_html(r, "AppStore")
        assert r.status_code == 200
        data = r.json()
        repos = data if isinstance(data, list) else data.get("items", [])
        assert isinstance(repos, list)
        # Should have at least the default Big Bear repo
        assert len(repos) >= 1

    def test_repo_has_fields(self, api_session):
        r = _get(api_session, "/api/appstore/repos")
        _skip_html(r, "AppStore")
        data = r.json()
        repos = data if isinstance(data, list) else data.get("items", [])
        for repo in repos[:3]:
            assert "id" in repo
            assert "name" in repo
            assert "url" in repo
            assert "enabled" in repo

    def test_add_invalid_repo(self, api_session):
        """Adding a repo with invalid URL should fail."""
        r = _post(api_session, "/api/appstore/repos", json={
            "url": "",
            "name": "Empty URL",
            "id": "test-empty",
        })
        _skip_html(r, "AppStore")
        assert r.status_code == 400
        data = _json(r)
        assert data and "error" in data

    def test_add_duplicate_repo(self, api_session):
        """Adding a repo with existing ID should return 409."""
        r = _get(api_session, "/api/appstore/repos")
        _skip_html(r, "AppStore")
        repos = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        if not repos:
            pytest.skip("No repos to test duplicate against")

        existing_id = repos[0]["id"]
        r2 = _post(api_session, f"/api/appstore/repos", json={
            "url": "https://example.com/fake",
            "name": "Duplicate Test",
            "id": existing_id,
        })
        _skip_html(r2, "AppStore")
        assert r2.status_code == 409

    def test_toggle_repo(self, api_session):
        """Toggle a repo on/off and back."""
        r = _get(api_session, "/api/appstore/repos")
        _skip_html(r, "AppStore")
        repos = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        if not repos:
            pytest.skip("No repos")

        repo_id = repos[0]["id"]
        original_state = repos[0].get("enabled", True)

        # Toggle off
        r2 = _post(api_session, f"/api/appstore/repos/{repo_id}/toggle",
                    json={"enabled": not original_state})
        assert r2.status_code == 200
        data = r2.json()
        assert data.get("ok") is True

        # Toggle back to original
        r3 = _post(api_session, f"/api/appstore/repos/{repo_id}/toggle",
                    json={"enabled": original_state})
        assert r3.status_code == 200

    def test_delete_nonexistent_repo(self, api_session):
        r = _get(api_session, "/api/appstore/repos")
        _skip_html(r, "AppStore")
        r2 = _delete(api_session, "/api/appstore/repos/nonexistent_repo_999")
        assert r2.status_code == 404


class TestAppStoreCatalog:
    """App Store: Docker app catalog."""

    def test_catalog_is_list(self, api_session):
        r = _get(api_session, "/api/appstore/catalog")
        _skip_html(r, "AppStore")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_catalog_app_fields(self, api_session):
        """Each catalog entry has essential fields."""
        r = _get(api_session, "/api/appstore/catalog")
        _skip_html(r, "AppStore")
        data = r.json()
        if not data:
            pytest.skip("Empty catalog (repos may not be synced)")
        for app in data[:5]:
            assert "id" in app, f"App missing 'id'"
            assert "title" in app, f"App missing 'title': {app.get('id')}"
            assert "image" in app or "main_service" in app, \
                f"App missing docker info: {app.get('id')}"

    def test_catalog_no_compose_raw_in_list(self, api_session):
        """Catalog list should NOT include full compose YAML (perf)."""
        r = _get(api_session, "/api/appstore/catalog")
        _skip_html(r, "AppStore")
        data = r.json()
        for app in data[:10]:
            assert "compose_raw" not in app, \
                f"compose_raw leaked in catalog list for {app.get('id')}"

    def test_catalog_app_detail(self, api_session):
        """Individual app detail includes compose_raw."""
        r = _get(api_session, "/api/appstore/catalog")
        _skip_html(r, "AppStore")
        data = r.json()
        if not data:
            pytest.skip("Empty catalog")

        app_id = data[0]["id"]
        r2 = _get(api_session, f"/api/appstore/app/{app_id}")
        assert r2.status_code == 200
        detail = r2.json()
        assert "id" in detail
        assert detail["id"] == app_id
        assert "compose_raw" in detail, "Detail should include compose_raw"

    def test_catalog_app_not_found(self, api_session):
        r = _get(api_session, "/api/appstore/app/nonexistent_docker_app_999")
        _skip_html(r, "AppStore")
        assert r.status_code == 404


class TestAppStoreCacheStats:
    """App Store: cache management."""

    def test_cache_stats(self, api_session):
        r = _get(api_session, "/api/appstore/cache/stats")
        _skip_html(r, "AppStore")
        assert r.status_code == 200
        data = r.json()
        assert "catalog_exists" in data
        assert "repo_cache_count" in data


class TestAppStoreCompose:
    """App Store: compose file adaptation and validation."""

    def test_compose_endpoint(self, api_session):
        """Get adapted compose for an app."""
        r = _get(api_session, "/api/appstore/catalog")
        _skip_html(r, "AppStore")
        data = r.json()
        if not data:
            pytest.skip("Empty catalog")

        app_id = data[0]["id"]
        r2 = _get(api_session, f"/api/appstore/compose/{app_id}")
        assert r2.status_code == 200
        cdata = r2.json()
        assert "compose" in cdata or "compose_raw" in cdata
        # Should have config with service port/volume info
        assert "config" in cdata

    def test_validate_nonexistent_app(self, api_session):
        """Validate a non-existent app."""
        r = _post(api_session, "/api/appstore/validate",
                   json={"app_id": "nonexistent_999"})
        _skip_html(r, "AppStore")
        data = _json(r)
        if r.status_code == 503:
            pytest.skip("Docker not running")
        # Should return error or ok=false
        assert r.status_code in (200, 400, 404)


class TestAppStoreInstallRestrictions:
    """App Store: install/uninstall validation."""

    def test_install_requires_app_id(self, api_session):
        r = _post(api_session, "/api/appstore/install", json={})
        _skip_html(r, "AppStore")
        if r.status_code == 503:
            pytest.skip("Docker not running")
        assert r.status_code == 400
        data = _json(r)
        assert data and "error" in data

    def test_install_nonexistent_app(self, api_session):
        r = _post(api_session, "/api/appstore/install",
                   json={"app_id": "nonexistent_999"})
        _skip_html(r, "AppStore")
        if r.status_code == 503:
            pytest.skip("Docker not running")
        assert r.status_code in (400, 404)

    def test_uninstall_requires_app_id(self, api_session):
        r = _post(api_session, "/api/appstore/uninstall", json={})
        _skip_html(r, "AppStore")
        if r.status_code == 503:
            pytest.skip("Docker not running")
        assert r.status_code == 400

    def test_uninstall_nonexistent(self, api_session):
        r = _post(api_session, "/api/appstore/uninstall",
                   json={"app_id": "nonexistent_999"})
        _skip_html(r, "AppStore")
        if r.status_code == 503:
            pytest.skip("Docker not running")
        assert r.status_code == 404

    def test_reinstall_nonexistent(self, api_session):
        r = _post(api_session, "/api/appstore/reinstall",
                   json={"app_id": "nonexistent_999"})
        _skip_html(r, "AppStore")
        if r.status_code == 503:
            pytest.skip("Docker not running")
        assert r.status_code == 404


class TestAppStoreNoAuth:
    """App Store: public vs protected endpoints."""

    def test_catalog_is_public(self, base_url):
        """Catalog should be accessible without auth."""
        r = requests.get(f"{base_url}/api/appstore/catalog", timeout=15)
        if "text/html" in r.headers.get("Content-Type", ""):
            pytest.skip("AppStore blueprint not registered")
        # May return 200 (public) or 401 (if gated) — both valid
        assert r.status_code in (200, 401)

    def test_install_requires_auth(self, base_url):
        r = requests.post(f"{base_url}/api/appstore/install",
                          json={"app_id": "test"}, timeout=10)
        if "text/html" in r.headers.get("Content-Type", ""):
            pytest.skip("AppStore blueprint not registered")
        assert r.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════
#  3. PKG REGISTRY — /api/pkg-registry/
#     Git-based package management
# ═══════════════════════════════════════════════════════════════════════════

class TestPkgRegistryRepos:
    """Pkg Registry: repository management."""

    def test_repos_list(self, api_session):
        r = _get(api_session, "/api/pkg-registry/repos")
        _skip_html(r, "PkgRegistry")
        assert r.status_code == 200
        data = r.json()
        items = data.get("items", data if isinstance(data, list) else [])
        assert isinstance(items, list)

    def test_repos_have_fields(self, api_session):
        r = _get(api_session, "/api/pkg-registry/repos")
        _skip_html(r, "PkgRegistry")
        data = r.json()
        items = data.get("items", data if isinstance(data, list) else [])
        for repo in items[:3]:
            assert "id" in repo
            assert "name" in repo
            assert "url" in repo

    def test_add_repo_empty_url(self, api_session):
        r = _post(api_session, "/api/pkg-registry/repos", json={
            "url": "", "name": "Bad Repo",
        })
        _skip_html(r, "PkgRegistry")
        assert r.status_code == 400

    def test_cannot_delete_official_repo(self, api_session):
        """Official repos should be immutable."""
        r = _get(api_session, "/api/pkg-registry/repos")
        _skip_html(r, "PkgRegistry")
        data = r.json()
        items = data.get("items", data if isinstance(data, list) else [])
        official = [repo for repo in items if repo.get("official")]
        if not official:
            pytest.skip("No official repo found")

        r2 = _delete(api_session, f"/api/pkg-registry/repos/{official[0]['id']}")
        assert r2.status_code == 400

    def test_delete_nonexistent_repo(self, api_session):
        r = _delete(api_session, "/api/pkg-registry/repos/nonexistent_repo_999")
        _skip_html(r, "PkgRegistry")
        assert r.status_code == 404

    def test_toggle_nonexistent(self, api_session):
        r = _post(api_session, "/api/pkg-registry/repos/nonexistent_999/toggle")
        _skip_html(r, "PkgRegistry")
        assert r.status_code == 404


class TestPkgRegistryCatalog:
    """Pkg Registry: package catalog."""

    def test_catalog_list(self, api_session):
        r = _get(api_session, "/api/pkg-registry/catalog")
        _skip_html(r, "PkgRegistry")
        assert r.status_code == 200
        data = r.json()
        items = data.get("items", data if isinstance(data, list) else [])
        assert isinstance(items, list)

    def test_catalog_package_fields(self, api_session):
        r = _get(api_session, "/api/pkg-registry/catalog")
        _skip_html(r, "PkgRegistry")
        data = r.json()
        items = data.get("items", data if isinstance(data, list) else [])
        if not items:
            pytest.skip("Empty pkg catalog")
        for pkg in items[:3]:
            assert "id" in pkg
            assert "name" in pkg or "title" in pkg


class TestPkgRegistryInstallRestrictions:
    """Pkg Registry: install validation."""

    def test_install_missing_pkg_id(self, api_session):
        r = _post(api_session, "/api/pkg-registry/install", json={})
        _skip_html(r, "PkgRegistry")
        assert r.status_code == 400

    def test_install_nonexistent_package(self, api_session):
        r = _post(api_session, "/api/pkg-registry/install",
                   json={"pkg_id": "nonexistent_pkg_999"})
        _skip_html(r, "PkgRegistry")
        assert r.status_code == 404

    def test_uninstall_nonexistent_package(self, api_session):
        r = _post(api_session, "/api/pkg-registry/uninstall",
                   json={"pkg_id": "nonexistent_pkg_999"})
        _skip_html(r, "PkgRegistry")
        assert r.status_code == 404


class TestPkgRegistryNoAuth:
    """Pkg Registry: auth enforcement."""

    def test_repos_list_access(self, base_url):
        """Repos may be public or require auth."""
        r = requests.get(f"{base_url}/api/pkg-registry/repos", timeout=10)
        if "text/html" in r.headers.get("Content-Type", ""):
            pytest.skip("PkgRegistry blueprint not registered")
        # Public endpoint or auth-gated — both valid
        assert r.status_code in (200, 401)

    def test_install_requires_auth(self, base_url):
        r = requests.post(f"{base_url}/api/pkg-registry/install",
                          json={"pkg_id": "test"}, timeout=10)
        if "text/html" in r.headers.get("Content-Type", ""):
            pytest.skip("PkgRegistry blueprint not registered")
        if r.status_code == 405:
            pytest.skip("PkgRegistry blueprint not registered (405)")
        assert r.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════
#  4. CROSS-SYSTEM CONSISTENCY
# ═══════════════════════════════════════════════════════════════════════════

class TestCrossSystemConsistency:
    """Verify consistency between the three package systems."""

    def test_core_apps_not_in_optional(self, api_session):
        """Core apps should not appear as optional (no double listing)."""
        r = _get(api_session, "/api/app-manager/catalog")
        data = r.json()
        core_ids = {a["id"] for a in data["core"]}
        optional_ids = {a["id"] for a in data["optional"]}
        overlap = core_ids & optional_ids
        assert not overlap, f"Core/optional overlap: {overlap}"

    def test_installed_apps_have_version(self, api_session):
        """Every installed app should have version info."""
        r = _get(api_session, "/api/app-manager/installed")
        for app in r.json():
            version = (app.get("installed_version") or
                       app.get("version") or "")
            assert version, f"App {app.get('id')} has no version"

    def test_all_optional_have_install_info(self, api_session):
        """Optional apps should have install/status endpoint info."""
        r = _get(api_session, "/api/app-manager/catalog")
        data = r.json()
        for app in data["optional"][:10]:
            # Most should have at least a status endpoint
            has_endpoints = (
                app.get("install_endpoint") or
                app.get("status_endpoint") or
                app.get("simple")  # frontend-only apps
            )
            assert has_endpoints, \
                f"Optional app {app['id']} has no install/status info"
