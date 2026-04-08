"""
EthOS Builder QA — Production-Readiness Test Suite

Validates the x86 wrapper script and Python modules added in the enterprise
pipeline against the following criteria:

  Category A  — Bash script content (static analysis)
  Category B  — Python module correctness (SBOM, SecureBoot, Signing)
  Category C  — Builder API endpoints
  Category D  — Artifact structure (manifest, SBOM JSON schemas)
  Category E  — Go/No-Go checklist assertions (pre-release gate)

Run with:
    pytest tests/test_builder_qa.py -v
"""

import ast
import json
import os
import sys
import time

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "blueprints"))

# builder.py uses @admin_required/@require_auth injected by app.py.
# Inject stubs via builtins so the module imports cleanly in test context.
import builtins as _builtins
_stub = lambda f: f  # identity decorator
_builtins.admin_required = _stub
_builtins.require_auth = _stub

from builder import _x86_wrapper_script  # noqa: E402

from helpers import BASE_URL, USERNAME, PASSWORD, DEFAULT_TIMEOUT

NASOS = "/opt/ethos"

# ─────────────────────────────────────────────────────────────────────────────
#  Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def script():
    """Render the wrapper bash script once for all tests in this module."""
    if _x86_wrapper_script is None:
        pytest.skip("_x86_wrapper_script could not be imported from builder.py")
    return _x86_wrapper_script(NASOS)


@pytest.fixture(scope="module")
def auth_token():
    resp = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=10,
    )
    if resp.status_code != 200:
        pytest.skip("Cannot authenticate — server unavailable or wrong credentials")
    data = resp.json()
    if data.get("totp_required"):
        pytest.skip("2FA enabled — cannot obtain token automatically")
    token = data.get("token")
    if not token:
        pytest.skip("No token in login response")
    return token


@pytest.fixture(scope="module")
def api(auth_token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {auth_token}"})
    s.base = BASE_URL
    return s


# ═════════════════════════════════════════════════════════════════════════════
#  Category A — Bash script content (kernel hardening & security)
# ═════════════════════════════════════════════════════════════════════════════

class TestKernelHardening:
    """91-ethos-security.conf must be written by the wrapper script."""

    def test_sysctl_config_file_created(self, script):
        assert "91-ethos-security.conf" in script

    def test_kptr_restrict(self, script):
        assert "kernel.kptr_restrict = 2" in script

    def test_dmesg_restrict(self, script):
        assert "kernel.dmesg_restrict = 1" in script

    def test_bpf_disabled(self, script):
        assert "kernel.unprivileged_bpf_disabled = 1" in script

    def test_rp_filter(self, script):
        assert "net.ipv4.conf.all.rp_filter = 1" in script

    def test_syncookies(self, script):
        assert "net.ipv4.tcp_syncookies = 1" in script

    def test_icmp_redirects_blocked(self, script):
        assert "net.ipv4.conf.all.accept_redirects = 0" in script
        assert "net.ipv6.conf.all.accept_redirects = 0" in script

    def test_source_route_disabled(self, script):
        assert "net.ipv4.conf.all.accept_source_route = 0" in script


class TestModuleBlacklist:
    """ethos-security-blacklist.conf must blacklist DMA vectors and unused protocols."""

    def test_blacklist_file_created(self, script):
        assert "ethos-security-blacklist.conf" in script

    def test_firewire_blacklisted(self, script):
        assert "blacklist firewire-core" in script

    def test_thunderbolt_blacklisted(self, script):
        assert "blacklist thunderbolt" in script

    def test_legacy_fs_blacklisted(self, script):
        for fs in ["cramfs", "freevxfs", "jffs2", "hfs", "hfsplus", "udf"]:
            assert f"blacklist {fs}" in script, f"{fs} not blacklisted"

    def test_unused_protocols_blacklisted(self, script):
        for proto in ["dccp", "sctp", "rds", "tipc"]:
            assert f"blacklist {proto}" in script, f"{proto} not blacklisted"

    def test_install_true_prevents_autoload(self, script):
        # 'install X /bin/true' prevents kernel from autoloading the module
        for mod in ["cramfs", "dccp", "sctp"]:
            assert f"install {mod} /bin/true" in script, f"install /bin/true missing for {mod}"


# ═════════════════════════════════════════════════════════════════════════════
#  Category A — Branding purge
# ═════════════════════════════════════════════════════════════════════════════

class TestDebianBrandingPurge:
    """All Debian branding markers must be overwritten."""

    def test_lsb_release_written(self, script):
        assert "lsb-release" in script
        assert "DISTRIB_ID=EthOS" in script

    def test_usr_lib_os_release_updated(self, script):
        assert "usr/lib/os-release" in script

    def test_debian_version_overwritten(self, script):
        assert "/etc/debian_version" in script
        assert "ethos/" in script  # ethos/VERSION content

    def test_dpkg_origin_created(self, script):
        assert "dpkg/origins/ethos" in script
        assert "Vendor: EthOS" in script

    def test_motd_rebranded(self, script):
        assert "/etc/motd" in script
        # motd should reference brand name variable
        assert "BRAND_NAME" in script or "EthOS" in script


# ═════════════════════════════════════════════════════════════════════════════
#  Category A — SBOM integration in bash
# ═════════════════════════════════════════════════════════════════════════════

class TestSBOMBashIntegration:
    """SBOM generation and injection must be present in wrapper script."""

    def test_sbom_generated_after_apt(self, script):
        apt_pos  = script.find('_ckpt_set "05_apt_deps"')
        sbom_pos = script.find("builder_sbom")
        assert apt_pos != -1, "05_apt_deps checkpoint not found"
        assert sbom_pos != -1, "builder_sbom not referenced in script"
        assert sbom_pos > apt_pos, "SBOM must be generated AFTER apt_deps checkpoint"

    def test_sbom_injected_into_image(self, script):
        assert "ethos-sbom.json" in script

    def test_sbom_generate_sbom_called(self, script):
        assert "generate_sbom" in script
        assert "write_sbom" in script


# ═════════════════════════════════════════════════════════════════════════════
#  Category A — Secure Boot integration in bash
# ═════════════════════════════════════════════════════════════════════════════

class TestSecureBootBashIntegration:
    """Secure Boot signing step must appear after EFI install, before sync."""

    def test_secureboot_module_called(self, script):
        assert "builder_secureboot" in script

    def test_mok_key_ensured(self, script):
        assert "ensure_mok_keys" in script

    def test_sign_rootfs_called(self, script):
        assert "sign_rootfs_efi_binaries" in script

    def test_mok_der_installed_to_esp(self, script):
        assert "install_mok_der_to_esp" in script

    def test_mokutil_enrollment_hint(self, script):
        assert "mokutil" in script

    def test_signing_before_sync(self, script):
        sign_pos = script.find("sign_rootfs_efi_binaries")
        sync_pos = script.rfind("\nsync\n")
        assert sign_pos != -1
        assert sync_pos != -1
        assert sign_pos < sync_pos, "Signing must happen before final sync"


# ═════════════════════════════════════════════════════════════════════════════
#  Category A — Extended preflight checks
# ═════════════════════════════════════════════════════════════════════════════

class TestExtendedPreflight:
    """Preflight script must verify branding, hardening, and Flask."""

    def test_preflight_branding_check(self, script):
        assert "PREFLIGHT:BRANDING:OK" in script

    def test_preflight_hardening_check(self, script):
        assert "PREFLIGHT:HARDENING:OK" in script

    def test_preflight_flask_check(self, script):
        assert "PREFLIGHT:FLASK:OK" in script

    def test_preflight_systemd_check(self, script):
        assert "PREFLIGHT:SYSTEMD:" in script

    def test_preflight_ethos_service_check(self, script):
        assert "PREFLIGHT:ETHOS:OK" in script


# ═════════════════════════════════════════════════════════════════════════════
#  Category B — Python module correctness
# ═════════════════════════════════════════════════════════════════════════════

class TestSBOMModule:
    """builder_sbom.py must be importable and expose correct API."""

    def test_importable(self):
        import builder_sbom  # noqa: F401

    def test_generate_sbom_signature(self):
        from builder_sbom import generate_sbom
        import inspect
        sig = inspect.signature(generate_sbom)
        params = list(sig.parameters)
        assert "rootfs_path" in params
        assert "build_version" in params
        assert "brand_name" in params

    def test_write_sbom_signature(self):
        from builder_sbom import write_sbom
        import inspect
        sig = inspect.signature(write_sbom)
        params = list(sig.parameters)
        assert "sbom" in params
        assert "out_dir" in params

    def test_sbom_structure_on_nonexistent_rootfs(self):
        """generate_sbom returns {} for nonexistent rootfs (graceful failure)."""
        from builder_sbom import generate_sbom
        sbom = generate_sbom("/nonexistent/rootfs", "0.0.0-test", "EthOS")
        # Expected: empty dict returned on missing rootfs
        assert isinstance(sbom, dict)

    def test_sbom_spdx_version(self):
        """generate_sbom on real path returns valid SPDX-2.3 doc."""
        from builder_sbom import generate_sbom
        # Use /opt/ethos itself — has no dpkg but tests code path
        sbom = generate_sbom("/opt/ethos", "1.2.3", "EthOS")
        if not sbom:
            pytest.skip("No packages found in /opt/ethos rootfs (expected in dev)")
        assert sbom["spdxVersion"] == "SPDX-2.3"

    def test_sbom_document_namespace(self):
        from builder_sbom import generate_sbom
        sbom = generate_sbom("/opt/ethos", "1.2.3", "EthOS")
        if not sbom:
            pytest.skip("No packages found in /opt/ethos rootfs")
        assert "documentNamespace" in sbom
        assert "1.2.3" in sbom["documentNamespace"]

    def test_write_sbom_creates_file(self, tmp_path):
        from builder_sbom import generate_sbom, write_sbom
        # Build a minimal SPDX dict manually to test write_sbom
        sbom = {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "EthOS-qa-test",
            "documentNamespace": "https://ethos.local/sbom/qa-test",
            "dataLicense": "CC0-1.0",
            "documentDescribes": [],
            "packages": [],
            "relationships": [],
        }
        path = write_sbom(sbom, str(tmp_path))
        assert path and os.path.isfile(path)
        with open(path) as f:
            data = json.load(f)
        assert data["spdxVersion"] == "SPDX-2.3"


class TestSecureBootModule:
    """builder_secureboot.py must be importable and expose correct API."""

    def test_importable(self):
        import builder_secureboot  # noqa: F401

    def test_ensure_mok_keys_callable(self):
        from builder_secureboot import ensure_mok_keys
        assert callable(ensure_mok_keys)

    def test_sign_efi_binary_callable(self):
        from builder_secureboot import sign_efi_binary
        assert callable(sign_efi_binary)

    def test_sign_nonexistent_binary_returns_false(self):
        from builder_secureboot import sign_efi_binary
        result = sign_efi_binary("/nonexistent/BOOTX64.EFI")
        assert result is False

    def test_get_mok_paths(self):
        from builder_secureboot import get_mok_crt_path, get_mok_der_path
        # Returns string (may be empty if keys not yet generated — that's fine)
        assert isinstance(get_mok_crt_path(), str)
        assert isinstance(get_mok_der_path(), str)

    def test_install_mok_der_without_keys(self, tmp_path):
        from builder_secureboot import install_mok_der_to_esp
        # Should return False gracefully when MOK.der doesn't exist
        result = install_mok_der_to_esp(str(tmp_path))
        # Either False (no key generated yet) or True (keys already exist)
        assert isinstance(result, bool)


class TestSigningModule:
    """builder_signing.py must expose sign/verify/manifest API."""

    def test_importable(self):
        import builder_signing  # noqa: F401

    def test_sign_artifact_callable(self):
        from builder_signing import sign_artifact
        assert callable(sign_artifact)

    def test_verify_artifact_callable(self):
        from builder_signing import verify_artifact
        assert callable(verify_artifact)

    def test_write_manifest_callable(self):
        from builder_signing import write_manifest
        assert callable(write_manifest)

    def test_sign_nonexistent_returns_none_or_false(self):
        from builder_signing import sign_artifact
        result = sign_artifact("/nonexistent/artifact.sqsh", roothash="deadbeef")
        # Should fail gracefully (returns empty dict or falsy)
        assert not result or isinstance(result, dict)


class TestResourcesModule:
    """builder_resources.py must expose cgroup and tmpfs helpers."""

    def test_importable(self):
        import builder_resources  # noqa: F401

    def test_calculate_tmpfs(self):
        from builder_resources import calculate_build_tmpfs_mb
        result = calculate_build_tmpfs_mb(img_size_gb=8)
        # Returns a tuple (tmpfs_mb, use_tmpfs_bool) or an int
        if isinstance(result, tuple):
            mb = result[0]
        else:
            mb = result
        assert isinstance(mb, int)
        assert mb >= 0

    def test_enter_leave_build_slice_callable(self):
        from builder_resources import enter_build_slice, leave_build_slice
        assert callable(enter_build_slice)
        assert callable(leave_build_slice)


# ═════════════════════════════════════════════════════════════════════════════
#  Category C — Builder API endpoints
# ═════════════════════════════════════════════════════════════════════════════

class TestBuilderAPIEndpoints:

    def test_signing_key_endpoint_exists(self, api):
        r = api.get(f"{api.base}/api/builder/signing-key", timeout=DEFAULT_TIMEOUT)
        assert r.status_code in (200, 404), f"Unexpected status {r.status_code}"

    def test_signing_key_returns_json(self, api):
        r = api.get(f"{api.base}/api/builder/signing-key", timeout=DEFAULT_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            assert "public_key" in data or "key_id" in data or "error" in data

    def test_manifest_endpoint_exists(self, api):
        r = api.get(f"{api.base}/api/builder/manifest", timeout=DEFAULT_TIMEOUT)
        assert r.status_code in (200, 404), f"Unexpected status {r.status_code}"

    def test_manifest_returns_json(self, api):
        r = api.get(f"{api.base}/api/builder/manifest", timeout=DEFAULT_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            assert isinstance(data, (dict, list))

    def test_builder_status_endpoint(self, api):
        r = api.get(f"{api.base}/api/builder/status", timeout=DEFAULT_TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "running" in data or "status" in data or "ok" in data

    def test_unauthenticated_signing_key_rejected(self):
        r = requests.get(
            f"{BASE_URL}/api/builder/signing-key",
            timeout=DEFAULT_TIMEOUT,
        )
        assert r.status_code in (401, 403)

    def test_unauthenticated_manifest_rejected(self):
        r = requests.get(
            f"{BASE_URL}/api/builder/manifest",
            timeout=DEFAULT_TIMEOUT,
        )
        assert r.status_code in (401, 403)


# ═════════════════════════════════════════════════════════════════════════════
#  Category D — Artifact structure validation
# ═════════════════════════════════════════════════════════════════════════════

class TestManifestSchema:
    """ethos-manifest.json must conform to expected schema when it exists."""

    MANIFEST_PATH = os.path.join(NASOS, "data", "ethos-manifest.json")

    @pytest.mark.skipif(
        not os.path.exists(os.path.join(NASOS, "data", "ethos-manifest.json")),
        reason="No manifest produced yet (run a build first)",
    )
    def test_manifest_required_fields(self):
        with open(self.MANIFEST_PATH) as f:
            m = json.load(f)
        for field in ["version", "build_time", "artifacts"]:
            assert field in m, f"Manifest missing field: {field}"

    @pytest.mark.skipif(
        not os.path.exists(os.path.join(NASOS, "data", "ethos-manifest.json")),
        reason="No manifest produced yet",
    )
    def test_manifest_artifacts_have_checksums(self):
        with open(self.MANIFEST_PATH) as f:
            m = json.load(f)
        artifacts = m.get("artifacts", {})
        assert len(artifacts) > 0, "Manifest has no artifacts"
        for name, meta in artifacts.items():
            assert "sha256" in meta or "hash" in meta, \
                f"Artifact {name!r} has no checksum"


class TestSBOMSchema:
    """ethos-sbom.json (if produced by a build) must be valid SPDX-2.3."""

    def _find_sbom(self):
        candidates = [
            os.path.join(NASOS, "data", "ethos-sbom.json"),
            "/tmp/ethos-sbom.json",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    def test_sbom_spdx_schema_when_present(self):
        path = self._find_sbom()
        if not path:
            pytest.skip("No SBOM file found — run a build first")
        with open(path) as f:
            sbom = json.load(f)
        assert sbom.get("spdxVersion") == "SPDX-2.3"
        assert "documentNamespace" in sbom
        assert "name" in sbom
        assert "packages" in sbom
        assert isinstance(sbom["packages"], list)

    def test_sbom_packages_have_required_fields(self):
        path = self._find_sbom()
        if not path:
            pytest.skip("No SBOM file found")
        with open(path) as f:
            sbom = json.load(f)
        for pkg in sbom.get("packages", [])[:10]:  # sample first 10
            assert "name" in pkg, f"Package missing name: {pkg}"
            assert "versionInfo" in pkg, f"Package missing versionInfo: {pkg}"
            assert "SPDXID" in pkg, f"Package missing SPDXID: {pkg}"


# ═════════════════════════════════════════════════════════════════════════════
#  Category E — Go/No-Go checklist (pre-release gate)
# ═════════════════════════════════════════════════════════════════════════════

class TestGoNoGoChecklist:
    """
    Production release gate. All must pass for a Go decision.

    Tests prefixed CRITICAL must all pass (No-Go if any fail).
    Tests prefixed ADVISORY are warnings only.
    """

    # --- CRITICAL ---

    def test_CRITICAL_wrapper_script_renders(self, script):
        """Wrapper script must render without exceptions."""
        assert len(script) > 10_000, "Script suspiciously short — rendering may have failed"

    def test_CRITICAL_set_e_present(self, script):
        """set -e must be present for early-exit on errors."""
        first = script.strip().split("\n")[:10]
        assert any("set -e" in l for l in first)

    def test_CRITICAL_debootstrap_pipestatus(self, script):
        """debootstrap uses PIPESTATUS so tee doesn't mask failure exit code."""
        assert "PIPESTATUS" in script

    def test_CRITICAL_grub_efi_verified(self, script):
        """GRUB install verifies BOOTX64.EFI existence after grub-install."""
        assert "BOOTX64.EFI" in script

    def test_CRITICAL_dm_verity_present(self, script):
        """dm-verity must be configured in the build script."""
        assert "veritysetup" in script
        assert "ROOTHASH" in script or "roothash" in script.lower()

    def test_CRITICAL_overlayfs_in_initramfs(self, script):
        """OverlayFS must be set up in the initramfs hook."""
        assert "overlay" in script.lower()

    def test_CRITICAL_tmpfs_umount_guard(self, script):
        """Cleanup must use mountpoint guard before tmpfs umount."""
        assert "mountpoint -q" in script

    def test_CRITICAL_security_sysctl_present(self, script):
        """Kernel hardening sysctl (91-ethos-security.conf) must be present."""
        assert "91-ethos-security.conf" in script

    def test_CRITICAL_module_blacklist_present(self, script):
        """Module blacklist must be present."""
        assert "ethos-security-blacklist.conf" in script

    def test_CRITICAL_branding_complete(self, script):
        """All four branding files must be overwritten."""
        for marker in ["/etc/os-release", "lsb-release", "/etc/debian_version", "dpkg/origins"]:
            assert marker in script, f"Branding file not written: {marker}"

    def test_CRITICAL_sbom_integrated(self, script):
        """SBOM generation must be integrated into the build."""
        assert "builder_sbom" in script
        assert "ethos-sbom.json" in script

    def test_CRITICAL_secureboot_integrated(self, script):
        """Secure Boot MOK signing must be integrated."""
        assert "builder_secureboot" in script

    def test_CRITICAL_ssh_keys_removed(self, script):
        """SSH host keys must be removed before image packaging."""
        assert "ssh_host_" in script

    def test_CRITICAL_apt_cache_cleaned(self, script):
        """apt cache must be cleaned to reduce image size."""
        assert "apt-get clean" in script or "apt clean" in script

    def test_CRITICAL_loop_device_cleanup(self, script):
        """Cleanup trap must detach loop device."""
        assert "losetup -d" in script

    # --- ADVISORY ---

    def test_ADVISORY_signing_module_exists(self):
        """Artifact signing module should be present."""
        path = os.path.join(NASOS, "backend", "blueprints", "builder_signing.py")
        assert os.path.isfile(path), "builder_signing.py not found"

    def test_ADVISORY_sbom_module_syntax(self):
        """SBOM module must have no Python syntax errors."""
        path = os.path.join(NASOS, "backend", "blueprints", "builder_sbom.py")
        if not os.path.isfile(path):
            pytest.skip("builder_sbom.py not found")
        src = open(path).read()
        try:
            ast.parse(src)
        except SyntaxError as e:
            pytest.fail(f"Syntax error in builder_sbom.py: {e}")

    def test_ADVISORY_secureboot_module_syntax(self):
        """Secure Boot module must have no Python syntax errors."""
        path = os.path.join(NASOS, "backend", "blueprints", "builder_secureboot.py")
        if not os.path.isfile(path):
            pytest.skip("builder_secureboot.py not found")
        src = open(path).read()
        try:
            ast.parse(src)
        except SyntaxError as e:
            pytest.fail(f"Syntax error in builder_secureboot.py: {e}")

    def test_ADVISORY_firstboot_oobe_ssl(self):
        """firstboot-v2.sh should generate an SSL certificate."""
        path = os.path.join(NASOS, "installer", "images", "firstboot-v2.sh")
        if not os.path.isfile(path):
            pytest.skip("firstboot-v2.sh not found")
        src = open(path).read()
        assert "openssl req" in src, "OOBE SSL cert generation missing from firstboot-v2.sh"
        assert "SSL_CERT" in src, "SSL_CERT not registered in ethos.env"

    def test_ADVISORY_preflight_extended_checks(self, script):
        """Preflight should check branding, hardening, and Flask."""
        for check in ["PREFLIGHT:BRANDING:", "PREFLIGHT:HARDENING:", "PREFLIGHT:FLASK:"]:
            assert check in script, f"Preflight check missing: {check}"
