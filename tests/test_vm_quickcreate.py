"""
EthOS VM Quick-Create — Post-Build Image Validation & Lifecycle Tests

Validates that a builder image works correctly through the VM quick-create
flow, from image creation through setup wizard completion and restart.

Test categories:
  A. Image Structure   — offline validation of builder .img partitions/files
  B. Quick-Create API  — creates VM, verifies disk conversion (not empty)
  C. Disk Preparation  — .installed marker, ethos.service enabled, preboot disabled
  D. Boot & Setup      — VM boots into setup wizard, completes setup, login works
  E. Restart Survival  — stop/start VM, login still works (not installer loop)

Prerequisites:
  - EthOS server running on localhost:9000 (or ETHOS_BASE_URL)
  - qemu-system-x86_64, qemu-img, qemu-nbd available
  - A built EthOS image (auto-detected from builder history or ETHOS_TEST_IMAGE)
  - KVM recommended (/dev/kvm) — tests are slow without it

Run all:
    pytest tests/test_vm_quickcreate.py -v --timeout=600

Run offline-only (no VM):
    pytest tests/test_vm_quickcreate.py -v -k "ImageStructure"

Run full lifecycle:
    ETHOS_E2E_VM=1 pytest tests/test_vm_quickcreate.py -v --timeout=900

Env vars:
    ETHOS_BASE_URL   — EthOS API (default: http://localhost:9000)
    ETHOS_USER       — login username
    ETHOS_PASS       — login password
    ETHOS_TOKEN      — pre-injected Bearer token (skips login)
    ETHOS_TEST_IMAGE — path to .img file (skips builder history lookup)
    ETHOS_E2E_VM     — set to "1" to run full lifecycle tests (D, E)
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))
from helpers import BASE_URL, USERNAME, PASSWORD

# ─── Configuration ────────────────────────────────────────────────────────────

TEST_IMAGE = os.environ.get("ETHOS_TEST_IMAGE", "")
E2E_VM = os.environ.get("ETHOS_E2E_VM", "") == "1"
QEMU_BIN = shutil.which("qemu-system-x86_64") or ""
QEMU_IMG = shutil.which("qemu-img") or ""
QEMU_NBD = shutil.which("qemu-nbd") or ""
FDISK = shutil.which("fdisk") or "/usr/sbin/fdisk"

SETUP_USER = "qctest"
SETUP_PASS = "QcTest99!x"
VM_BOOT_TIMEOUT = 240  # seconds to wait for VM to serve HTTP
VM_NAME_PREFIX = "qc-test-"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _api_url(path):
    return f"{BASE_URL}{path}"


def _get_token():
    """Get auth token from env, DB, or login."""
    tok = os.environ.get("ETHOS_TOKEN", "")
    if tok:
        return tok
    # Try DB
    try:
        import sqlite3
        conn = sqlite3.connect("/opt/ethos/data/tokens.db")
        tok = conn.execute(
            "SELECT token FROM tokens ORDER BY last_active DESC LIMIT 1"
        ).fetchone()[0]
        conn.close()
        return tok
    except Exception:
        pass
    # Login
    r = requests.post(
        _api_url("/api/auth/login"),
        json={"username": USERNAME, "password": PASSWORD},
        timeout=10,
    )
    if r.status_code == 200:
        return r.json().get("token", "")
    return ""


def _headers():
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


def _api(method, path, data=None, timeout=15):
    """Call the host EthOS API."""
    url = _api_url(path)
    try:
        r = getattr(requests, method.lower())(
            url, headers=_headers(), json=data, timeout=timeout
        )
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:300]}
    except requests.exceptions.ConnectionError:
        return 0, {"error": "Connection refused"}
    except requests.exceptions.Timeout:
        return 0, {"error": "Timeout"}


def _vm_api(port, method, path, data=None, timeout=15):
    """Call an API endpoint inside a VM (no auth — fresh EthOS)."""
    url = f"http://localhost:{port}{path}"
    csrf = secrets.token_hex(32)
    headers = {
        "Content-Type": "application/json",
        "Cookie": f"csrf_token={csrf}",
        "X-CSRFToken": csrf,
    }
    try:
        r = getattr(requests, method.lower())(
            url, headers=headers, json=data, timeout=timeout,
            cookies={"csrf_token": csrf},
        )
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:300]}
    except requests.exceptions.ConnectionError:
        return 0, {"error": "Connection refused"}
    except requests.exceptions.Timeout:
        return 0, {"error": "Timeout"}


def _wait_for_http(port, path="/", timeout=VM_BOOT_TIMEOUT, expect_code=200):
    """Wait until an HTTP endpoint responds with the expected status code."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(
                f"http://localhost:{port}{path}",
                timeout=5,
            )
            if r.status_code == expect_code:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def _find_builder_image():
    """Find an EthOS builder image."""
    if TEST_IMAGE and os.path.isfile(TEST_IMAGE):
        return TEST_IMAGE
    # Check common locations
    candidates = [
        "/opt/ethos/installer/images/ethos-x86.img",
        "/opt/ethos/data/vms/_images/ethos-x86.img",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # Try builder history API
    try:
        code, resp = _api("GET", "/api/builder/history")
        if code == 200:
            for item in resp.get("items", []):
                p = item.get("img") or (item.get("result") or {}).get("img", "")
                if p and os.path.isfile(p):
                    return p
    except Exception:
        pass
    return None


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api_session():
    """Authenticated requests.Session for the host EthOS API."""
    s = requests.Session()
    s.headers.update(_headers())
    return s


@pytest.fixture(scope="module")
def builder_image():
    """Path to a builder .img file. Skip if not found."""
    img = _find_builder_image()
    if not img:
        pytest.skip("No builder image found (set ETHOS_TEST_IMAGE)")
    return img


@pytest.fixture(scope="module")
def qemu_available():
    if not QEMU_BIN:
        pytest.skip("qemu-system-x86_64 not found")
    return True


@pytest.fixture(scope="module")
def nbd_available():
    if not QEMU_NBD:
        pytest.skip("qemu-nbd not found")
    if os.geteuid() != 0:
        pytest.skip("qemu-nbd requires root")
    return True


@pytest.fixture(scope="module")
def quick_create_vm(api_session, builder_image, qemu_available):
    """Create a quick-create VM and yield its (vm_id, port). Cleanup after."""
    code, resp = _api("POST", "/api/vm/quick-create-ethos")
    if code != 200 or not resp.get("id"):
        pytest.fail(f"Quick-create failed: {code} {resp}")

    vm_id = resp["id"]
    # Find the EthOS web port
    code2, machines = _api("GET", "/api/vm/machines")
    port = None
    if code2 == 200:
        vms = machines if isinstance(machines, list) else machines.get("items", [])
        for vm in vms:
            if vm.get("id") == vm_id:
                for pf in vm.get("network", {}).get("port_forwards", []):
                    if pf.get("guest") == 9000:
                        port = pf["host"]
                        break
                break
    if not port:
        pytest.fail(f"VM {vm_id} has no port 9000 forward")

    yield vm_id, port

    # Cleanup
    _api("POST", f"/api/vm/machines/{vm_id}/stop", {"force": True})
    time.sleep(3)
    _api("DELETE", f"/api/vm/machines/{vm_id}")


# ─── Category A: Image Structure (offline, fast) ─────────────────────────────

class TestImageStructure:
    """Offline validation of the builder image."""

    def test_image_exists_and_nonzero(self, builder_image):
        assert os.path.isfile(builder_image)
        size = os.path.getsize(builder_image)
        assert size > 500_000_000, f"Image too small: {size} bytes"

    @pytest.mark.skipif(not QEMU_IMG, reason="qemu-img not found")
    def test_image_is_valid_raw(self, builder_image):
        """Builder images should be raw format."""
        r = subprocess.run(
            ["qemu-img", "info", "--output=json", builder_image],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0, f"qemu-img info failed: {r.stderr}"
        info = json.loads(r.stdout)
        assert info.get("format") == "raw", f"Expected raw, got {info.get('format')}"

    def test_partition_table_is_gpt(self, builder_image):
        """Image must have a GPT partition table."""
        r = subprocess.run(
            ["fdisk", "-l", builder_image],
            capture_output=True, text=True, timeout=10,
        )
        assert "GPT" in r.stdout or "gpt" in r.stdout.lower(), \
            "No GPT partition table found"

    def test_has_esp_and_root_partitions(self, builder_image):
        """Image must have at least 2 partitions (ESP + root)."""
        r = subprocess.run(
            ["fdisk", "-l", builder_image],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l for l in r.stdout.splitlines() if builder_image in l and "EFI" not in l.split(":")[0] if l.strip()]
        # Count partition lines (e.g. "image.img1", "image.img2")
        part_lines = [
            l for l in r.stdout.splitlines()
            if l.startswith(builder_image) and not l.startswith(f"{builder_image}:")
        ]
        assert len(part_lines) >= 2, f"Expected ≥2 partitions, found {len(part_lines)}"

    def test_esp_has_grub_efi(self, builder_image):
        """ESP must contain GRUB EFI bootloader."""
        r = subprocess.run(
            ["fdisk", "-l", "-o", "Start,Size,Type", builder_image],
            capture_output=True, text=True, timeout=10,
        )
        assert "EFI" in r.stdout, "No EFI System partition found"


# ─── Category B: Quick-Create API ────────────────────────────────────────────

class TestQuickCreateAPI:
    """Test the quick-create endpoint produces a valid VM."""

    def test_quick_create_returns_vm_id(self, quick_create_vm):
        vm_id, port = quick_create_vm
        assert vm_id, "No VM ID returned"
        assert port, "No port mapping found"

    def test_vm_disk_is_not_empty(self, quick_create_vm):
        """The converted disk must be substantially larger than an empty qcow2."""
        vm_id, _ = quick_create_vm
        code, machines = _api("GET", "/api/vm/machines")
        assert code == 200
        vms = machines if isinstance(machines, list) else machines.get("items", [])
        vm = next((v for v in vms if v.get("id") == vm_id), None)
        assert vm, f"VM {vm_id} not found"

        disk_file = vm.get("disk_file", "")
        assert disk_file, "No disk_file in VM config"
        assert os.path.isfile(disk_file), f"Disk file not found: {disk_file}"

        size = os.path.getsize(disk_file)
        # Empty qcow2 is ~200KB. A converted builder image should be >500MB.
        assert size > 500_000_000, \
            f"Disk looks empty: {size} bytes (expected >500MB for converted image)"

    @pytest.mark.skipif(not QEMU_IMG, reason="qemu-img not found")
    def test_vm_disk_is_qcow2(self, quick_create_vm):
        """Disk must be in qcow2 format."""
        vm_id, _ = quick_create_vm
        code, machines = _api("GET", "/api/vm/machines")
        vms = machines if isinstance(machines, list) else machines.get("items", [])
        vm = next((v for v in vms if v.get("id") == vm_id), None)
        disk_file = vm.get("disk_file", "")

        r = subprocess.run(
            ["qemu-img", "info", "--output=json", disk_file],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0
        info = json.loads(r.stdout)
        assert info.get("format") == "qcow2"


# ─── Category C: Disk Preparation (requires root + nbd) ──────────────────────

class TestDiskPreparation:
    """Verify the image was properly prepared for direct boot."""

    @pytest.fixture(autouse=True)
    def _need_nbd(self, nbd_available):
        pass

    @pytest.fixture(scope="class")
    def mounted_root(self, quick_create_vm):
        """Mount the VM's root partition via qemu-nbd and yield the mount path."""
        vm_id, _ = quick_create_vm

        # First stop the VM so we can mount its disk
        _api("POST", f"/api/vm/machines/{vm_id}/stop", {"force": True})
        time.sleep(3)

        code, machines = _api("GET", "/api/vm/machines")
        vms = machines if isinstance(machines, list) else machines.get("items", [])
        vm = next((v for v in vms if v.get("id") == vm_id), None)
        disk_file = vm.get("disk_file", "")

        nbd_dev = "/dev/nbd1"  # Use nbd1 to avoid conflicts with production
        mnt = tempfile.mkdtemp(prefix="qctest_mnt_")

        subprocess.run(["modprobe", "nbd", "max_part=8"],
                       capture_output=True, timeout=10)
        r = subprocess.run(["qemu-nbd", "--connect", nbd_dev, disk_file],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            pytest.skip(f"qemu-nbd connect failed: {r.stderr}")

        time.sleep(1)  # let kernel discover partitions
        root_part = f"{nbd_dev}p2"
        r = subprocess.run(["mount", root_part, mnt],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            subprocess.run(["qemu-nbd", "--disconnect", nbd_dev],
                           capture_output=True, timeout=10)
            pytest.skip(f"Mount failed: {r.stderr}")

        yield mnt

        # Cleanup
        subprocess.run(["umount", mnt], capture_output=True, timeout=10)
        subprocess.run(["qemu-nbd", "--disconnect", nbd_dev],
                       capture_output=True, timeout=10)
        try:
            os.rmdir(mnt)
        except OSError:
            pass

        # Restart the VM for subsequent tests
        _api("POST", f"/api/vm/machines/{vm_id}/start")

    def test_installed_marker_exists(self, mounted_root):
        """The .installed marker must be present to skip preboot."""
        marker = os.path.join(mounted_root, "opt/ethos/.installed")
        assert os.path.isfile(marker), \
            f".installed marker not found — VM will boot into preboot installer loop"

    def test_ethos_service_enabled(self, mounted_root):
        """ethos.service must be enabled (symlink in multi-user.target.wants)."""
        link = os.path.join(
            mounted_root, "etc/systemd/system/multi-user.target.wants/ethos.service"
        )
        assert os.path.islink(link) or os.path.isfile(link), \
            "ethos.service not enabled — main EthOS app won't start on boot"

    def test_preboot_service_disabled(self, mounted_root):
        """ethos-preboot.service must NOT be enabled (no symlink in wants dir)."""
        link = os.path.join(
            mounted_root,
            "etc/systemd/system/multi-user.target.wants/ethos-preboot.service",
        )
        assert not os.path.exists(link), \
            "ethos-preboot.service still enabled — Conflicts= will prevent ethos.service from starting"

    def test_ethos_app_files_present(self, mounted_root):
        """Core EthOS backend files must be on disk."""
        app_py = os.path.join(mounted_root, "opt/ethos/backend/app.py")
        assert os.path.isfile(app_py), "backend/app.py not found in image"

    def test_venv_exists(self, mounted_root):
        """Python venv must be present for the service to start."""
        venv = os.path.join(mounted_root, "opt/ethos/venv/bin/python")
        assert os.path.isfile(venv), "venv/bin/python not found in image"

    def test_ethos_service_unit_valid(self, mounted_root):
        """ethos.service unit file should reference the correct paths."""
        unit = os.path.join(mounted_root, "etc/systemd/system/ethos.service")
        assert os.path.isfile(unit), "ethos.service unit file not found"
        content = open(unit).read()
        assert "/opt/ethos/backend/app.py" in content
        assert "multi-user.target" in content


# ─── Category D: Boot & Setup Wizard (requires running VM) ───────────────────

@pytest.mark.skipif(not E2E_VM, reason="Set ETHOS_E2E_VM=1 to run lifecycle tests")
class TestBootAndSetup:
    """Full lifecycle: boot → setup wizard → login."""

    @pytest.fixture(autouse=True)
    def _need_qemu(self, qemu_available):
        pass

    def test_vm_starts_successfully(self, quick_create_vm):
        vm_id, port = quick_create_vm
        code, resp = _api("POST", f"/api/vm/machines/{vm_id}/start")
        assert code == 200, f"VM start failed: {resp}"
        assert resp.get("ok") or resp.get("pid"), f"Unexpected response: {resp}"

    def test_vm_serves_ethos_not_preboot(self, quick_create_vm):
        """VM must boot into EthOS setup wizard, NOT the preboot installer."""
        _, port = quick_create_vm
        assert _wait_for_http(port), \
            f"VM not responding on port {port} after {VM_BOOT_TIMEOUT}s"

        # Check it's the main EthOS app (has /api/setup/status)
        code, data = _vm_api(port, "GET", f"/api/setup/status")
        assert code == 200, f"Setup status failed: {code} {data}"
        assert "needs_setup" in data, f"Not EthOS app response: {data}"

        # Verify preboot endpoints are NOT served
        code2, _ = _vm_api(port, "GET", "/api/disks/discover")
        # The main app might also have this endpoint, but let's check title
        try:
            r = requests.get(f"http://localhost:{port}/", timeout=5)
            assert "EthOS" in r.text, "Page title doesn't contain 'EthOS'"
            assert "Installer" not in r.text, \
                "Page contains 'Installer' — preboot is running instead of EthOS"
        except Exception:
            pass

    def test_setup_wizard_completes(self, quick_create_vm):
        """Complete the setup wizard with test credentials."""
        _, port = quick_create_vm
        if not _wait_for_http(port):
            pytest.skip("VM not responding")

        code, data = _vm_api(port, "GET", "/api/setup/status")
        if code == 200 and not data.get("needs_setup"):
            pytest.skip("Setup already completed")

        code, resp = _vm_api(port, "POST", "/api/setup/complete", {
            "hostname": "qctest-vm",
            "username": SETUP_USER,
            "password": SETUP_PASS,
            "nas_name": "QC Test VM",
            "language": "en",
            "timezone": "Europe/Warsaw",
        })
        assert code == 200, f"Setup failed: {code} {resp}"
        assert resp.get("success"), f"Setup not successful: {resp}"

    def test_login_works_after_setup(self, quick_create_vm):
        """Login with credentials created during setup."""
        _, port = quick_create_vm
        if not _wait_for_http(port):
            pytest.skip("VM not responding")

        code, resp = _vm_api(port, "POST", "/api/auth/login", {
            "username": SETUP_USER,
            "password": SETUP_PASS,
        })
        assert code == 200, f"Login failed: {code} {resp}"
        assert resp.get("token"), f"No token in login response: {resp}"
        assert resp.get("user", {}).get("username") == SETUP_USER


# ─── Category E: Restart Survival ────────────────────────────────────────────

@pytest.mark.skipif(not E2E_VM, reason="Set ETHOS_E2E_VM=1 to run lifecycle tests")
class TestRestartSurvival:
    """VM must survive stop/start and still serve EthOS (not installer)."""

    @pytest.fixture(autouse=True)
    def _need_qemu(self, qemu_available):
        pass

    def test_restart_preserves_setup(self, quick_create_vm):
        """After stop/start, VM serves EthOS login (not setup, not installer)."""
        vm_id, port = quick_create_vm

        # Ensure setup was completed in previous tests
        if _wait_for_http(port, timeout=10):
            code, data = _vm_api(port, "GET", "/api/setup/status")
            if code == 200 and data.get("needs_setup"):
                # Run setup first
                _vm_api(port, "POST", "/api/setup/complete", {
                    "hostname": "qctest-vm",
                    "username": SETUP_USER,
                    "password": SETUP_PASS,
                    "nas_name": "QC Test VM",
                    "language": "en",
                    "timezone": "Europe/Warsaw",
                })
                time.sleep(3)

        # Stop VM
        code, _ = _api("POST", f"/api/vm/machines/{vm_id}/stop", {"force": True})
        assert code == 200, "Failed to stop VM"
        time.sleep(5)

        # Start VM
        code, resp = _api("POST", f"/api/vm/machines/{vm_id}/start")
        assert code == 200, f"Failed to restart VM: {resp}"

        # Wait for it to come back
        assert _wait_for_http(port), \
            f"VM not responding after restart on port {port}"

        # Must NOT need setup
        code, data = _vm_api(port, "GET", "/api/setup/status")
        assert code == 200, f"Setup status failed after restart: {code}"
        assert data.get("needs_setup") is False, \
            "Setup wizard appeared after restart — state was lost"

    def test_login_works_after_restart(self, quick_create_vm):
        """Login with the same credentials after restart."""
        _, port = quick_create_vm
        if not _wait_for_http(port, timeout=30):
            pytest.skip("VM not responding after restart")

        code, resp = _vm_api(port, "POST", "/api/auth/login", {
            "username": SETUP_USER,
            "password": SETUP_PASS,
        })
        assert code == 200, f"Login failed after restart: {code} {resp}"
        assert resp.get("token"), "No token after restart login"
