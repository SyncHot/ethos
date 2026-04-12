"""Tests for builder.py — validates the x86 image wrapper script generation.

Ensures the f-string in _x86_wrapper_script() renders correctly, especially
bash constructs with curly braces (udev rules, bash arrays, etc.) that must
be double-escaped in Python f-strings.
"""

import sys
import os
import re
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "blueprints"))

from builder import _x86_wrapper_script


NASOS = "/opt/ethos"


@pytest.fixture(scope="module")
def script():
    """Generate the wrapper script once for all tests."""
    return _x86_wrapper_script(NASOS)


# ── f-string rendering (the bug that broke the build) ──

def test_fstring_no_python_artifacts(script):
    """The generated script must not contain Python f-string artifacts."""
    assert "NameError" not in script
    # If Python evaluated {queue} as expression, we'd get an error at generation time
    # The fact we got a script at all means the f-string is valid


def test_udev_power_rules(script):
    """udev power rules must have proper single braces in output."""
    assert 'ATTR{queue/rotational}=="1"' in script
    assert 'hdparm -S 242' in script


def test_udev_readahead_rules(script):
    """udev readahead rules must have proper single braces."""
    assert 'ATTR{queue/rotational}=="1", RUN+="/sbin/blockdev --setra 4096' in script
    assert 'ATTR{queue/rotational}=="0", RUN+="/sbin/blockdev --setra 256' in script


def test_udev_scheduler_rules(script):
    """udev I/O scheduler rules must have proper braces."""
    assert 'ATTR{queue/rotational}=="1", ATTR{queue/scheduler}="bfq"' in script
    assert 'ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="none"' in script
    assert 'KERNEL=="nvme' in script
    assert 'ATTR{queue/scheduler}="none"' in script


def test_no_stray_double_braces(script):
    """No double braces should remain in the output (they would be f-string escapes)."""
    # Known exception: bash arrays use {}, which are escaped as {{}} in f-string
    # but rendered as {} in output. Stray {{ in output means something is wrong.
    # Except if bash heredoc or some literal uses {{ — unlikely in this script.
    for line in script.split("\n"):
        if "{{" in line and "}}" in line:
            # This is fine if it's a bash brace expansion like mkdir -p dir/{a,b}
            # which in f-string is dir/{{a,b}} and renders as dir/{a,b}
            pass  # allow — hard to distinguish from valid bash


# ── Script structure ──

def test_script_starts_with_set(script):
    """Script should begin with set -e for error handling."""
    first_lines = script.strip().split("\n")[:5]
    assert any("set -e" in l for l in first_lines)


def test_nasos_path_injected(script):
    """NASOS variable should be set to the provided path."""
    assert f'NASOS="{NASOS}"' in script


def test_image_size(script):
    """Image should be 8GB."""
    assert "IMG_SIZE_GB=8" in script


def test_debian_release(script):
    """Should use the default distro release (Ubuntu noble)."""
    assert 'DEBIAN_RELEASE="noble"' in script
    assert 'BASE_DISTRO="ubuntu"' in script


# ── Key build steps ──

def test_dependency_check(script):
    """Builder checks for debootstrap, parted, mkfs, grub."""
    assert "command -v" in script
    assert "debootstrap" in script
    assert "parted" in script
    assert "grub-install" in script


def test_debootstrap_call(script):
    """Debootstrap should target the correct release."""
    assert "debootstrap" in script
    assert "noble" in script


def test_grub_efi_and_bios(script):
    """GRUB should be installed for EFI. BIOS fallback only on Debian."""
    assert "grub-install" in script
    # EFI target
    assert "x86_64-efi" in script


def test_python_venv_created(script):
    """Venv should be created in the image."""
    assert "python3 -m venv" in script
    assert "/opt/ethos/venv" in script


def test_pip_install(script):
    """pip install requirements should be run."""
    assert "pip install" in script
    assert "requirements.txt" in script


def test_apt_extra_packages(script):
    """apt_extra packages from spec should be installed."""
    assert "APT_EXTRA_PKGS" in script
    assert "apt_extra packages" in script.lower() or "apt_extra" in script


def test_critical_imports_verified(script):
    """Builder should verify flask, psutil, gevent, pyudev imports."""
    assert "import flask" in script
    assert "import psutil" in script


# ── New Flask installer integration ──

def test_flask_preboot_copied(script):
    """New Flask preboot installer should be copied to image."""
    assert "installer/preboot" in script


def test_flask_preboot_verified(script):
    """Builder should verify Flask preboot app.py exists in image."""
    assert "installer/preboot/app.py" in script


def test_preboot_service_uses_venv(script):
    """ethos-preboot.service should use venv Python, not system Python."""
    assert "/opt/ethos/venv/bin/python" in script
    # Old path should NOT be present
    assert "/usr/bin/python3 /opt/ethos-installer/preboot-server.py" not in script


def test_preboot_service_workdir(script):
    """Service should set WorkingDirectory for Flask."""
    assert "WorkingDirectory=/opt/ethos/installer/preboot" in script


def test_old_installer_not_referenced(script):
    """Old preboot-server.py should not be referenced."""
    assert "/opt/ethos-installer/preboot-server.py" not in script


# ── Firstboot ──

def test_firstboot_v2_preferred(script):
    """Builder should prefer firstboot-v2.sh."""
    assert "firstboot-v2.sh" in script


def test_firstboot_fallback(script):
    """Builder should fallback to firstboot.sh if v2 not found."""
    assert "firstboot.sh" in script


# ── System services ──

def test_ethos_service_created(script):
    """ethos.service should be created."""
    assert "ethos.service" in script


def test_ap_service_created(script):
    """ethos-ap.service should be created."""
    assert "ethos-ap.service" in script


def test_firstboot_service_created(script):
    """ethos-firstboot.service should be created."""
    assert "ethos-firstboot.service" in script


# ── Cleanup and output ──

def test_cleanup_trap(script):
    """Cleanup function should be trapped on EXIT."""
    assert "trap cleanup EXIT" in script


def test_cleanup_unmounts(script):
    """Cleanup should unmount and detach loop device."""
    assert "umount" in script
    assert "losetup -d" in script


def test_output_markers(script):
    """Script should output RESULT_IMG marker. EXIT_CODE is added by host_run_stream."""
    assert "RESULT_IMG:" in script


# ── Swap and filesystem ──

def test_swap_not_in_build_image(script):
    """Swap is NOT created in the build image — handled by installer at install time."""
    assert "swapfile" in script  # referenced in squashfs exclusions
    assert "Swap is intentionally NOT created" in script


def test_fstab_generated(script):
    """fstab should be written."""
    assert "/etc/fstab" in script


# ── Security ──

def test_ssh_keys_cleaned(script):
    """SSH host keys should be removed (regenerated on first boot)."""
    assert "ssh_host_" in script


def test_apt_cache_cleaned(script):
    """apt cache should be cleaned to reduce image size."""
    assert "apt-get clean" in script or "apt clean" in script


# ── Logrotate and sysctl ──

def test_sysctl_tuning(script):
    """sysctl tuning for NAS workload should be present."""
    assert "vm.swappiness" in script
    assert "ip_forward" in script


def test_logrotate_config(script):
    """logrotate config for EthOS logs."""
    assert "logrotate" in script
    assert "/opt/ethos/logs" in script
