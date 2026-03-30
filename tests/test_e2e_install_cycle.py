"""
EthOS NAS – Optional App Install/Uninstall Cycle Tests

Tests every optional app through a full cycle:
  1. Install  → wait for SocketIO 'done' event
  2. Verify   → /api/app-manager/<id>/status shows installed=True
  3. Uninstall → wait for SocketIO 'done' event
  4. Verify   → /api/app-manager/<id>/status shows installed=False
  5. Restore  → re-install if app was previously installed

If an app is already installed, the test uninstalls first then runs the full cycle.

Usage:
    ETHOS_USER=myuser ETHOS_PASS=mypass \\
    ETHOS_BASE_URL=http://localhost:9000 \\
    pytest tests/test_e2e_install_cycle.py -v --timeout=600

    # Single app:
    pytest tests/test_e2e_install_cycle.py -k "sharing-nfs" -v

    # Skip slow apps (libreoffice):
    pytest tests/test_e2e_install_cycle.py -k "not doc-editor and not printer" -v
"""

import os
import sys
import time
import threading
import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))
from helpers import BASE_URL, USERNAME, PASSWORD

# ---------------------------------------------------------------------------
# SocketIO-based progress waiter
# ---------------------------------------------------------------------------

try:
    import socketio as _sio_mod
    HAS_SOCKETIO = True
except ImportError:
    HAS_SOCKETIO = False


class ProgressWaiter:
    """Connect via SocketIO and block until app_manager_progress
    reports 'done' or 'error' for the given task_id."""

    def __init__(self, base_url, token, timeout=300):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self.events = []
        self._result = None
        self._done = threading.Event()

    def wait_for_task(self, task_id):
        """Block until the task completes. Returns last event dict."""
        if not HAS_SOCKETIO:
            return self._poll_fallback(task_id)

        sio = _sio_mod.Client(reconnection=False)

        @sio.on('app_manager_progress')
        def on_progress(data):
            if data.get('task_id') != task_id:
                return
            self.events.append(data)
            if data.get('status') in ('done', 'error'):
                self._result = data
                self._done.set()

        try:
            sio.connect(
                self.base_url,
                auth={'token': self.token},
                transports=['websocket'],
                wait_timeout=10,
            )
        except Exception:
            return self._poll_fallback(task_id)

        try:
            self._done.wait(timeout=self.timeout)
            return self._result
        finally:
            try:
                sio.disconnect()
            except Exception:
                pass

    def _poll_fallback(self, task_id):
        """Fallback: poll status endpoint if SocketIO unavailable."""
        time.sleep(3)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(2)
            # No per-task endpoint; just return None and let caller verify status
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_cached_token = None


def _get_token(base_url):
    global _cached_token
    if _cached_token:
        r = requests.get(
            f"{base_url}/api/auth/verify",
            headers={"Authorization": f"Bearer {_cached_token}"},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("valid"):
            return _cached_token

    for attempt in range(5):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=10,
        )
        if resp.status_code == 200:
            token = resp.json().get("token")
            if token:
                _cached_token = token
                return token
        if resp.status_code == 429:
            time.sleep(10)
            continue
        time.sleep(3)

    pytest.fail(f"Login failed: {resp.status_code} {resp.text[:200]}")


@pytest.fixture(scope="session")
def base_url():
    return os.environ.get("ETHOS_BASE_URL", BASE_URL)


@pytest.fixture(scope="session")
def token(base_url):
    return _get_token(base_url)


@pytest.fixture(scope="session")
def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="session")
def catalog(base_url, headers):
    """Fetch the full catalog once for the session."""
    r = requests.get(f"{base_url}/api/app-manager/catalog", headers=headers, timeout=15)
    assert r.status_code == 200, f"Catalog fetch failed: {r.status_code}"
    return r.json()


@pytest.fixture(scope="session")
def optional_apps(catalog):
    """Return dict of optional apps keyed by ID."""
    return {app['id']: app for app in catalog['optional']}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status(base_url, headers, app_id):
    r = requests.get(
        f"{base_url}/api/app-manager/{app_id}/status",
        headers=headers, timeout=15,
    )
    if r.status_code != 200:
        return {}
    return r.json()


def _install(base_url, headers, token, app_id, timeout=300):
    """Install an app and wait for completion. Returns (ok, events, error)."""
    r = requests.post(
        f"{base_url}/api/app-manager/{app_id}/install",
        headers=headers, timeout=30,
    )
    if r.status_code != 200:
        return False, [], f"Install request failed: {r.status_code} {r.text[:200]}"

    data = r.json()
    if not data.get('ok'):
        return False, [], f"Install not ok: {data}"

    task_id = data.get('task_id')
    if not task_id:
        return False, [], "No task_id returned"

    waiter = ProgressWaiter(base_url, token, timeout=timeout)
    result = waiter.wait_for_task(task_id)

    if result is None:
        # SocketIO didn't capture — poll status
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            st = _status(base_url, headers, app_id)
            if st.get('installed'):
                return True, waiter.events, None
        return False, waiter.events, "Timed out waiting for install"

    if result.get('status') == 'done':
        return True, waiter.events, None
    else:
        return False, waiter.events, result.get('message', 'Unknown error')


def _uninstall(base_url, headers, token, app_id, timeout=120):
    """Uninstall an app and wait for completion. Returns (ok, events, error)."""
    r = requests.post(
        f"{base_url}/api/app-manager/{app_id}/uninstall",
        headers=headers, timeout=30,
    )
    if r.status_code != 200:
        return False, [], f"Uninstall request failed: {r.status_code} {r.text[:200]}"

    data = r.json()
    if not data.get('ok'):
        return False, [], f"Uninstall not ok: {data}"

    task_id = data.get('task_id')
    if not task_id:
        return False, [], "No task_id returned"

    waiter = ProgressWaiter(base_url, token, timeout=timeout)
    result = waiter.wait_for_task(task_id)

    if result is None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            st = _status(base_url, headers, app_id)
            if not st.get('installed'):
                return True, waiter.events, None
        return False, waiter.events, "Timed out waiting for uninstall"

    if result.get('status') == 'done':
        return True, waiter.events, None
    else:
        return False, waiter.events, result.get('message', 'Unknown error')


# ---------------------------------------------------------------------------
# Timeout map — heavy deps need more time
# ---------------------------------------------------------------------------

INSTALL_TIMEOUTS = {
    'doc-editor': 600,      # libreoffice + pip deps
    'printer': 600,         # libreoffice + cups
    'vm-manager': 300,      # qemu packages
    'docker-manager': 300,  # docker.io
    'antivirus': 300,       # clamav + freshclam update
    'surveillance': 300,    # ffmpeg + onvif-zeep
    'builder': 240,         # squashfs + genisoimage
    'wireguard': 180,       # wireguard + qrencode
    'ai-chat': 180,         # pip: openai, anthropic, huggingface_hub
}

# Apps to skip entirely (need running daemon or break the system)
SKIP_APPS = {
    'docker-manager',  # Needs Docker daemon installed
}

# All optional app IDs — collected dynamically at module level is not possible,
# so we hardcode from BUILTIN_CATALOG. The test verifies against live catalog.
ALL_OPTIONAL = [
    'ai-chat', 'antivirus', 'builder', 'cloud-backup', 'code-editor',
    'cron', 'disk-repair', 'doc-editor', 'docker-manager', 'domains-manager',
    'download-manager', 'duplicates', 'family-hub', 'gallery', 'printer',
    'raid-lvm', 'remote-log', 'rollback', 'sharing-dlna', 'sharing-ftp',
    'sharing-nfs', 'sharing-samba', 'sharing-sftp', 'sharing-webdav',
    'sticky-notes', 'surveillance', 'tickets', 'ups', 'usb-flasher',
    'vm-manager', 'websites', 'wireguard',
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOptionalAppCatalog:
    """Verify the test list matches the live catalog."""

    def test_all_optional_apps_covered(self, optional_apps):
        """Our ALL_OPTIONAL list matches the server catalog."""
        live_ids = set(optional_apps.keys())
        test_ids = set(ALL_OPTIONAL)
        missing = live_ids - test_ids
        extra = test_ids - live_ids
        assert not missing, f"Apps in catalog but not in test list: {missing}"
        assert not extra, f"Apps in test list but not in catalog: {extra}"


@pytest.mark.parametrize("app_id", ALL_OPTIONAL)
class TestInstallUninstallCycle:
    """Full install → verify → uninstall → verify cycle for each optional app."""

    def test_install_uninstall(self, base_url, headers, token, optional_apps, app_id):
        if app_id in SKIP_APPS:
            pytest.skip(f"{app_id} skipped (needs external daemon)")

        if app_id not in optional_apps:
            pytest.skip(f"{app_id} not in catalog")

        app_def = optional_apps[app_id]
        timeout = INSTALL_TIMEOUTS.get(app_id, 120)
        was_installed = app_def.get('installed', False)

        # --- Phase 1: Ensure app is uninstalled ---
        if was_installed:
            ok, events, err = _uninstall(base_url, headers, token, app_id, timeout=60)
            if not ok:
                # Check if maybe already uninstalled
                st = _status(base_url, headers, app_id)
                if st.get('installed'):
                    pytest.fail(f"Pre-uninstall failed for {app_id}: {err}")

            st = _status(base_url, headers, app_id)
            assert not st.get('installed'), \
                f"{app_id} should be uninstalled before install test"

        # --- Phase 2: Install ---
        ok, events, err = _install(base_url, headers, token, app_id, timeout=timeout)
        assert ok, f"Install {app_id} failed: {err}"

        # Verify progress was monotonic
        if events:
            percents = [e.get('percent', 0) for e in events if e.get('percent', 0) > 0]
            for i in range(1, len(percents)):
                assert percents[i] >= percents[i - 1], \
                    f"{app_id} progress went backwards: {percents}"

        # Verify status
        st = _status(base_url, headers, app_id)
        assert st.get('installed'), f"{app_id} not marked installed after install"

        # --- Phase 3: Uninstall ---
        ok, events, err = _uninstall(base_url, headers, token, app_id, timeout=60)
        assert ok, f"Uninstall {app_id} failed: {err}"

        st = _status(base_url, headers, app_id)
        assert not st.get('installed'), \
            f"{app_id} still marked installed after uninstall"

        # --- Phase 4: Restore if was originally installed ---
        if was_installed:
            ok, _, err = _install(base_url, headers, token, app_id, timeout=timeout)
            assert ok, f"Restore {app_id} failed: {err}"


class TestProgressEvents:
    """Verify progress events have correct shape and monotonic percents."""

    def test_install_emits_progress(self, base_url, headers, token, optional_apps):
        """Pick a lightweight app and verify progress event structure."""
        # Pick an app with deps so install takes long enough to capture events
        # (frontend-only apps finish in ms, before SocketIO connects)
        candidates = [
            ('sharing-nfs', ['nfs-kernel-server']),
            ('sharing-ftp', ['vsftpd']),
            ('sharing-dlna', ['minidlna']),
            ('cloud-backup', ['rclone']),
            ('disk-repair', ['smartmontools', 'e2fsprogs']),
        ]
        target = None
        for aid, _ in candidates:
            if aid in optional_apps:
                target = aid
                break

        if target is None:
            pytest.skip("No suitable app available for progress test")

        was_installed = optional_apps[target].get('installed', False)

        # Ensure uninstalled first
        if was_installed:
            _uninstall(base_url, headers, token, target, timeout=60)

        ok, events, err = _install(base_url, headers, token, target, timeout=120)
        assert ok, f"Install {target} failed: {err}"

        # Verify events structure — need at least start + deps_apt + done
        assert len(events) >= 3, \
            f"Too few events ({len(events)}): {[e.get('stage') for e in events]}"

        for ev in events:
            assert 'stage' in ev, f"Missing stage: {ev}"
            assert 'percent' in ev, f"Missing percent: {ev}"
            assert 'status' in ev, f"Missing status: {ev}"
            assert 'app_id' in ev, f"Missing app_id: {ev}"
            assert ev['app_id'] == target

        # First event should be 'start' (may be missed if SocketIO connects late)
        stages = [e.get('stage') for e in events]
        assert 'done' in stages, f"No 'done' stage in events: {stages}"

        # Last event should be 'done'
        assert events[-1]['stage'] == 'done'
        assert events[-1]['percent'] == 100
        assert events[-1]['status'] == 'done'

        # Progress should be monotonically increasing
        percents = [e.get('percent', 0) for e in events if e.get('percent', 0) > 0]
        for i in range(1, len(percents)):
            assert percents[i] >= percents[i - 1], \
                f"Progress went backwards: {percents}"

        # Cleanup or restore
        if not was_installed:
            _uninstall(base_url, headers, token, target, timeout=60)
        else:
            pass  # already re-installed by this test
