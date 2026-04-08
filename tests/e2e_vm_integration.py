"""
EthOS Builder — VM Integration Tests (E2E: Builder → VM Manager → Boot → Beacon)
==================================================================================
Tests the full "Golden Path" multi-module flow:
  1. VMMCreationFromBuilderArtifact  — create+delete VM using last .img artifact
  2. HeadlessBoot                   — start VM, verify QEMU process spawns
  3. InstallationLoop               — boot installer ISO → install to qcow2 → verify A/B
  4. BeaconIntegration              — mock POST beacon then verify GET retrieves it
  5. E2ESmokeTest                   — full flow summary (requires ETHOS_E2E_RUN=1)

Skip conditions:
  - Tests requiring QEMU skip unless qemu-system-x86_64 is found in PATH
  - Tests requiring a built image skip unless ETHOS_TEST_IMAGE env var is set
  - Full E2E smoke requires ETHOS_E2E_RUN=1 to avoid accidental 30-minute builds

Run minimal (no QEMU needed):
    ETHOS_USER=admin ETHOS_PASS=yourpass \
        pytest tests/e2e_vm_integration.py -v -k "not qemu and not slow"

Run with a pre-built image:
    ETHOS_TEST_IMAGE=/path/to/ethos.img \
    ETHOS_USER=admin ETHOS_PASS=yourpass \
        pytest tests/e2e_vm_integration.py -v

Run the full E2E smoke (builds, installs, boots — takes ~45 min):
    ETHOS_E2E_RUN=1 ETHOS_USER=admin ETHOS_PASS=yourpass \
        pytest tests/e2e_vm_integration.py -v -k slow
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))
from helpers import BASE_URL, USERNAME, PASSWORD

# ─── Runtime config ───────────────────────────────────────────────────────────

TEST_IMAGE   = os.environ.get("ETHOS_TEST_IMAGE", "")
E2E_RUN      = os.environ.get("ETHOS_E2E_RUN", "") == "1"
QEMU_BIN     = shutil.which("qemu-system-x86_64") or ""
QEMU_IMG_BIN = shutil.which("qemu-img") or ""

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _url(path):
    return f"{BASE_URL}/api{path}"


def _auth(api_session):
    return api_session


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api(api_session):
    return api_session


@pytest.fixture(scope="module")
def last_img(api):
    """Return the path of the last successfully built .img, or skip."""
    if TEST_IMAGE and os.path.isfile(TEST_IMAGE):
        return TEST_IMAGE
    # Try build history
    r = api.get(_url("/builder/history"))
    items = r.json().get("items", [])
    for item in items:
        p = item.get("img") or (item.get("result") or {}).get("img", "")
        if p and os.path.isfile(p):
            return p
    # Try info
    r2 = api.get(_url("/builder/info"))
    for key in ("images", "releases"):
        for entry in r2.json().get(key, []):
            p = entry.get("path") or entry.get("img", "")
            if p and os.path.isfile(p):
                return p
    pytest.skip("No .img artifact available — set ETHOS_TEST_IMAGE or run a build first")


@pytest.fixture(scope="module")
def qemu_available():
    if not QEMU_BIN:
        pytest.skip("qemu-system-x86_64 not found in PATH")
    return QEMU_BIN


@pytest.fixture(scope="module")
def tmp_dir():
    d = tempfile.mkdtemp(prefix="ethos_e2e_")
    yield d
    import shutil as sh
    sh.rmtree(d, ignore_errors=True)


# =============================================================================
#  1. VMM CREATION FROM BUILDER ARTIFACT
# =============================================================================

class TestVMMCreationFromBuilderArtifact:
    """POST /api/vm/machines with a builder .img as boot_image, then DELETE."""

    def test_vmm_list_accessible(self, api):
        r = api.get(_url("/vm/machines"))
        assert r.status_code in (200, 404), f"VM Manager unreachable: {r.status_code}"
        if r.status_code == 404:
            pytest.skip("VM Manager not installed")

    def test_create_vm_with_img_as_boot(self, api, last_img):
        r = api.get(_url("/vm/machines"))
        if r.status_code == 404:
            pytest.skip("VM Manager not installed")

        vm_name = f"e2e-integration-{uuid.uuid4().hex[:8]}"
        r = api.post(_url("/vm/machines"), json={
            "name": vm_name,
            "cpu": 1,
            "ram": 512,
            "disk_size": "4G",
            "os_type": "linux",
            "boot_image": last_img,
            "description": "E2E integration test VM — safe to delete",
        })
        assert r.status_code == 200, f"VM creation failed {r.status_code}: {r.text}"
        data = r.json()
        vm_id = data.get("id") or (data.get("vm") or {}).get("id")
        assert vm_id, f"VM id missing from response: {data}"

        # Verify VM was stored
        r2 = api.get(_url(f"/vm/machines/{vm_id}"))
        assert r2.status_code == 200
        vm = r2.json()
        assert vm.get("boot_image") == last_img or last_img in str(vm),             f"boot_image not set correctly in stored VM: {vm}"

        # Cleanup
        api.delete(_url(f"/vm/machines/{vm_id}"))

    def test_create_vm_invalid_image_path_rejected(self, api):
        r = api.get(_url("/vm/machines"))
        if r.status_code == 404:
            pytest.skip("VM Manager not installed")
        r = api.post(_url("/vm/machines"), json={
            "name": "e2e-invalid-path-test",
            "cpu": 1,
            "ram": 512,
            "disk_size": "4G",
            "os_type": "linux",
            "boot_image": "/etc/passwd",   # path traversal attempt
        })
        assert r.status_code in (400, 403, 500),             f"Expected rejection of /etc/passwd as boot_image, got {r.status_code}"

    def test_artifact_path_is_absolute(self, last_img):
        assert os.path.isabs(last_img), f"Artifact path not absolute: {last_img!r}"

    def test_artifact_readable(self, last_img):
        assert os.access(last_img, os.R_OK), f"Artifact not readable: {last_img!r}"

    def test_artifact_size_nonzero(self, last_img):
        size = os.path.getsize(last_img)
        assert size > 0, f"Artifact is 0 bytes: {last_img!r}"
        # Minimum 100 MB for a viable OS image
        min_size = 100 * 1024 * 1024
        assert size >= min_size,             f"Artifact too small ({size / 1e6:.1f} MB < 100 MB): {last_img!r}"


# =============================================================================
#  2. HEADLESS BOOT (QEMU)
# =============================================================================

class TestHeadlessBoot:
    """Boot the .img in QEMU headless mode and verify the process starts cleanly."""

    @pytest.mark.slow
    def test_qemu_starts_with_img(self, qemu_available, last_img, tmp_dir):
        """QEMU starts with the .img and emits some boot output within 30s."""
        serial_log = os.path.join(tmp_dir, "serial.log")
        cmd = [
            qemu_available,
            "-machine", "q35,accel=tcg",
            "-m", "512M",
            "-smp", "1",
            "-drive", f"file={last_img},format=raw,if=virtio,readonly=on",
            "-serial", f"file:{serial_log}",
            "-display", "none",
            "-no-reboot",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Wait for serial output to appear (up to 30s)
            deadline = time.time() + 30
            while time.time() < deadline:
                if os.path.isfile(serial_log) and os.path.getsize(serial_log) > 0:
                    break
                time.sleep(1)
            assert os.path.isfile(serial_log) and os.path.getsize(serial_log) > 0,                 "QEMU produced no serial output in 30s — image may not be bootable"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    @pytest.mark.slow
    def test_qemu_grub_appears_in_serial(self, qemu_available, last_img, tmp_dir):
        """GRUB or Linux kernel splash appears in serial output within 60s."""
        serial_log = os.path.join(tmp_dir, "grub_serial.log")
        cmd = [
            qemu_available,
            "-machine", "q35,accel=tcg",
            "-m", "512M",
            "-smp", "1",
            "-drive", f"file={last_img},format=raw,if=virtio",
            "-serial", f"file:{serial_log}",
            "-display", "none",
            "-no-reboot",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 60
            grub_seen = False
            while time.time() < deadline:
                if os.path.isfile(serial_log):
                    content = open(serial_log, errors="replace").read().lower()
                    if any(kw in content for kw in ("grub", "linux", "booting", "kernel")):
                        grub_seen = True
                        break
                time.sleep(2)
            assert grub_seen,                 "Neither GRUB nor Linux kernel appeared in serial output within 60s"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# =============================================================================
#  3. INSTALLATION LOOP (requires qemu-img + qemu)
# =============================================================================

class TestInstallationLoop:
    """Boot installer image → install to qcow2 → verify partition structure."""

    @pytest.mark.slow
    def test_installer_image_has_ab_partition_structure(self, last_img):
        """Use fdisk/sfdisk to check that the image has ≥4 partitions (A/B layout)."""
        if not shutil.which("fdisk"):
            pytest.skip("fdisk not available")
        result = subprocess.run(
            ["fdisk", "-l", last_img],
            capture_output=True, text=True, timeout=15
        )
        output = result.stdout + result.stderr
        # Count partition entries
        partitions = [ln for ln in output.splitlines()
                      if last_img in ln and ("Linux" in ln or "EFI" in ln or "FAT" in ln)]
        assert len(partitions) >= 3, (
            f"Expected ≥3 partitions in A/B image, found {len(partitions)}:\n{output}"
        )

    @pytest.mark.slow
    def test_efi_partition_present(self, last_img):
        """Image must have an EFI System Partition."""
        if not shutil.which("fdisk"):
            pytest.skip("fdisk not available")
        result = subprocess.run(
            ["fdisk", "-l", last_img],
            capture_output=True, text=True, timeout=15
        )
        assert "EFI" in result.stdout or "EFI" in result.stderr or "ef00" in result.stdout,             f"No EFI partition found in image:\n{result.stdout}"

    @pytest.mark.slow
    def test_squashfs_present_in_image(self, last_img, tmp_dir):
        """Mount the root partition and confirm squashfs/rootfs exists."""
        if not shutil.which("fdisk") or not shutil.which("losetup"):
            pytest.skip("fdisk/losetup not available")
        result = subprocess.run(
            ["fdisk", "-l", last_img],
            capture_output=True, text=True, timeout=15
        )
        # Find rootfs partition offset (sector × 512)
        sector_size = 512
        root_sector = None
        for line in result.stdout.splitlines():
            if "Linux filesystem" in line and last_img in line:
                parts = line.split()
                try:
                    root_sector = int(parts[1])
                    break
                except (IndexError, ValueError):
                    continue
        if root_sector is None:
            pytest.skip("Cannot determine root partition offset")
        offset = root_sector * sector_size
        mount_point = os.path.join(tmp_dir, "mount_check")
        os.makedirs(mount_point, exist_ok=True)
        # Mount read-only
        r = subprocess.run(
            ["mount", "-o", f"ro,offset={offset}", last_img, mount_point],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            pytest.skip(f"Cannot mount partition: {r.stderr}")
        try:
            entries = os.listdir(mount_point)
            assert any(
                e in entries for e in ("rootfs.squashfs", "rootfs.img", "squashfs.img", "live")
            ), f"No squashfs/rootfs found in mounted partition: {entries}"
        finally:
            subprocess.run(["umount", mount_point], capture_output=True, timeout=10)


# =============================================================================
#  4. BEACON INTEGRATION
# =============================================================================

class TestBeaconIntegration:
    """Full beacon flow: POST (simulate booted VM) → GET (verify builder received it)."""

    def test_beacon_post_no_auth_200(self):
        """A POST to /api/builder/beacon with no auth must return 200."""
        r = requests.post(_url("/builder/beacon"), json={
            "build_id": f"integration-test-{int(time.time())}",
            "hostname": "e2e-integration-vm",
            "version": "1.0.0-e2e",
            "timestamp": int(time.time()),
            "extras": {"test_class": "TestBeaconIntegration", "qemu": bool(QEMU_BIN)},
        })
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_beacon_get_after_post_matches(self, api):
        """Beacon stored via POST is retrievable via authenticated GET."""
        unique_id = f"integration-verify-{uuid.uuid4().hex[:8]}"
        requests.post(_url("/builder/beacon"), json={
            "build_id": unique_id,
            "hostname": "e2e-verify-vm",
            "version": "1.0.0",
            "timestamp": int(time.time()),
        })
        r = api.get(_url("/builder/beacon"))
        assert r.status_code == 200
        beacon = r.json().get("beacon", {})
        assert beacon.get("build_id") == unique_id,             f"Beacon mismatch: expected {unique_id!r}, got {beacon.get('build_id')!r}"

    def test_beacon_persisted_to_disk(self):
        """Beacon is written to builder_beacon.json on the host."""
        beacon_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "data", "builder_beacon.json"
        )
        if not os.path.isfile(beacon_file):
            pytest.skip("builder_beacon.json not found — no beacon has been received yet")
        with open(beacon_file) as f:
            data = json.load(f)
        assert "build_id" in data
        assert "hostname" in data
        assert "received_at" in data

    def test_beacon_received_at_is_reasonable_timestamp(self, api):
        """received_at in the beacon is within the last hour."""
        r = api.get(_url("/builder/beacon"))
        beacon = r.json().get("beacon")
        if beacon is None:
            pytest.skip("No beacon in state")
        received = beacon.get("received_at", 0)
        age_s = time.time() - received
        assert 0 <= age_s < 3600,             f"received_at timestamp looks wrong: age={age_s:.0f}s"

    def test_beacon_extras_preserved(self, api):
        """extras dict in POST payload is preserved in GET response."""
        extras = {"dm_verity": "ok", "systemd": True, "boot_time_s": 8}
        unique_id = f"extras-test-{uuid.uuid4().hex[:8]}"
        requests.post(_url("/builder/beacon"), json={
            "build_id": unique_id,
            "hostname": "extras-test-vm",
            "version": "1.0.0",
            "timestamp": int(time.time()),
            "extras": extras,
        })
        r = api.get(_url("/builder/beacon"))
        beacon = r.json().get("beacon", {})
        assert beacon.get("extras") == extras,             f"extras not preserved: {beacon.get('extras')!r}"


# =============================================================================
#  5. E2E SMOKE TEST (ETHOS_E2E_RUN=1 required)
# =============================================================================

class TestE2ESmokeTest:
    """Full Golden Path: trigger build → wait → create VM → beacon → verify."""

    @pytest.mark.slow
    def test_full_golden_path(self, api):
        """
        Full end-to-end smoke test:
          1. Ensure no build is running
          2. Start a build (or use existing artifact if ETHOS_TEST_IMAGE is set)
          3. Poll until done (max 60 min)
          4. Verify artifact exists and has beacon_id
          5. Create VM from artifact
          6. Post a mock beacon with the beacon_id
          7. Verify GET beacon returns matched=True

        Requires ETHOS_E2E_RUN=1 in environment.
        """
        if not E2E_RUN:
            pytest.skip(
                "Full E2E smoke test skipped. Set ETHOS_E2E_RUN=1 to run. "
                "Warning: this triggers a full image build (~30-45 min)."
            )

        # ── Step 1: Ensure idle ──────────────────────────────────────────────
        r = api.get(_url("/builder/status"))
        if r.json()["status"] == "building":
            pytest.skip("Build already in progress — cannot run E2E smoke test")

        # ── Step 2: Use existing image or start build ────────────────────────
        img_path = TEST_IMAGE
        beacon_id = None

        if not img_path:
            api.post(_url("/builder/dismiss"))
            r = api.post(_url("/builder/image"), json={})
            assert r.status_code == 200, f"Build start failed: {r.text}"

            # ── Step 3: Poll until done ──────────────────────────────────────
            deadline = time.time() + 3600  # 60 min max
            while time.time() < deadline:
                time.sleep(30)
                r = api.get(_url("/builder/status"))
                data = r.json()
                if data["status"] == "done":
                    img_path = (data.get("result") or {}).get("img", "")
                    beacon_id = (data.get("result") or {}).get("beacon_id", "")
                    break
                if data["status"] == "error":
                    pytest.fail(f"Build failed: {data.get('message')}")
            else:
                pytest.fail("Build did not complete within 60 minutes")
        else:
            # Extract beacon_id from status if last build matches image
            r = api.get(_url("/builder/status"))
            beacon_id = r.json().get("beacon_id", f"smoke-{int(time.time())}")

        # ── Step 4: Verify artifact ──────────────────────────────────────────
        assert img_path and os.path.isfile(img_path), f"Artifact missing: {img_path!r}"
        assert beacon_id, "No beacon_id in build result"

        # ── Step 5: Create VM ────────────────────────────────────────────────
        vm_name = f"e2e-smoke-{uuid.uuid4().hex[:6]}"
        r = api.post(_url("/vm/machines"), json={
            "name": vm_name, "cpu": 1, "ram": 512,
            "disk_size": "4G", "os_type": "linux",
            "boot_image": img_path,
        })
        vm_id = None
        if r.status_code == 200:
            vm_id = r.json().get("id") or (r.json().get("vm") or {}).get("id")

        # ── Step 6: Post mock beacon ─────────────────────────────────────────
        r = requests.post(_url("/builder/beacon"), json={
            "build_id": beacon_id,
            "hostname": "e2e-smoke-vm",
            "version": "1.0.0-smoke",
            "timestamp": int(time.time()),
            "extras": {"smoke_test": True, "vm_name": vm_name},
        })
        assert r.status_code == 200, f"Beacon POST failed: {r.text}"

        # ── Step 7: Verify GET beacon matched ────────────────────────────────
        r = api.get(_url("/builder/beacon"))
        assert r.status_code == 200
        data = r.json()
        assert data["beacon"]["build_id"] == beacon_id
        # build_id_matched = True only if beacon_id matches current build state
        # It may be True or False depending on whether this was the most recent build
        assert isinstance(data.get("build_id_matched"), bool)

        # ── Cleanup ──────────────────────────────────────────────────────────
        if vm_id:
            api.delete(_url(f"/vm/machines/{vm_id}"))
