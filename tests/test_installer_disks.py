"""Tests for disk operations (mocked system commands)."""

import sys
import os
import json
import stat
import tempfile
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "installer", "preboot"))

import disk_ops


SAMPLE_LSBLK = json.dumps({
    "blockdevices": [
        {
            "name": "sda", "size": 128849018880, "type": "disk",
            "model": "Samsung SSD 860", "serial": "S3XXNX0K123456",
            "tran": "sata", "rota": False, "hotplug": False, "mountpoint": None,
            "children": [
                {"name": "sda1", "mountpoint": None, "size": 536870912, "type": "part"},
                {"name": "sda2", "mountpoint": None, "size": 128312147968, "type": "part"},
            ],
        },
        {
            "name": "sdb", "size": 15728640000, "type": "disk",
            "model": "USB Flash Drive", "serial": "USBXXX",
            "tran": "usb", "rota": False, "hotplug": True, "mountpoint": None,
            "children": [
                {"name": "sdb1", "mountpoint": "/boot/efi", "size": 268435456, "type": "part"},
                {"name": "sdb2", "mountpoint": "/", "size": 15460204544, "type": "part"},
            ],
        },
        {
            "name": "sdc", "size": 500107862016, "type": "disk",
            "model": "WD Blue 500GB", "serial": "WD-WXE123",
            "tran": "sata", "rota": True, "hotplug": False, "mountpoint": None,
            "children": [],
        },
        {
            "name": "mmcblk0", "size": 536870912, "type": "disk",
            "model": "", "serial": "", "tran": "", "rota": False,
            "hotplug": False, "mountpoint": None, "children": [],
        },
    ]
})


@patch("disk_ops._smart_temp", return_value=35)
@patch("disk_ops._smart_status", return_value="ok")
@patch("disk_ops._get_persistent_id", side_effect=lambda n: f"ata-{n}")
@patch("disk_ops._get_boot_device", return_value="sdb")
@patch("disk_ops._run")
def test_discover_finds_disks(mock_run, mock_boot, mock_pid, mock_smart, mock_temp):
    mock_run.return_value = (SAMPLE_LSBLK, "", 0)
    result = disk_ops.discover()
    assert result["boot_device"] == "sdb"
    disks = result["disks"]
    assert len(disks) == 3
    names = [d["name"] for d in disks]
    assert "mmcblk0" not in names
    boot = next(d for d in disks if d["name"] == "sdb")
    assert boot["is_boot"] is True
    ssd = next(d for d in disks if d["name"] == "sda")
    assert ssd["rotational"] is False
    assert ssd["size_gb"] == 120.0


@patch("disk_ops._smart_status", return_value="ok")
def test_validate_rejects_boot_disk(mock_smart):
    ok, errors, _ = disk_ops.validate("sdb", None, "sdb")
    assert ok is False
    assert any("boot" in e.lower() for e in errors)


@patch("disk_ops._smart_status", return_value="ok")
def test_validate_accepts_valid(mock_smart):
    ok, errors, _ = disk_ops.validate("sda", "same", "sdb")
    assert ok is True
    assert len(errors) == 0


@patch("disk_ops._smart_status", return_value="failed")
def test_validate_warns_smart(mock_smart):
    ok, _, warnings = disk_ops.validate("sda", "same", "sdb")
    assert ok is True
    assert any("SMART" in w for w in warnings)


@patch("disk_ops._smart_status", return_value="ok")
def test_validate_no_disk(mock_smart):
    ok, errors, _ = disk_ops.validate("", None, "sdb")
    assert ok is False


@patch("disk_ops._smart_status", return_value="ok")
def test_validate_data_is_boot(mock_smart):
    ok, errors, _ = disk_ops.validate("sda", "sdb", "sdb")
    assert ok is False


# USB as OS disk — previously blocked, now allowed with a warning

SAMPLE_LSBLK_WITH_USB_TARGET = json.dumps({
    "blockdevices": [
        {
            "name": "sda", "size": 128849018880, "type": "disk",
            "model": "Samsung SSD 860", "serial": "S3XXNX0K123456",
            "tran": "sata", "rota": False, "hotplug": False, "mountpoint": None,
            "children": [],
        },
        {
            "name": "sdb", "size": 15728640000, "type": "disk",
            "model": "USB Installer", "serial": "USBBOOT",
            "tran": "usb", "rota": False, "hotplug": True, "mountpoint": "/",
            "children": [
                {"name": "sdb1", "mountpoint": "/boot/efi", "size": 268435456, "type": "part"},
                {"name": "sdb2", "mountpoint": "/", "size": 15460204544, "type": "part"},
            ],
        },
        {
            "name": "sdc", "size": 32000000000, "type": "disk",
            "model": "USB Target Drive", "serial": "USBTGT",
            "tran": "usb", "rota": False, "hotplug": True, "mountpoint": None,
            "children": [],
        },
    ]
})


@patch("disk_ops._disk_size_bytes", return_value=32 * 1024**3)
@patch("disk_ops._has_ethos_data_on_disk", return_value=False)
@patch("disk_ops._smart_status", return_value="ok")
@patch("disk_ops._run")
def test_validate_usb_os_disk_allowed_with_warning(mock_run, mock_smart, mock_ethos, mock_size):
    """USB disk as OS target must be allowed (ok=True) but emit a warning."""
    mock_run.return_value = (SAMPLE_LSBLK_WITH_USB_TARGET, "", 0)
    ok, errors, warnings = disk_ops.validate("sdc", "same", "sdb")
    assert ok is True, f"Expected ok=True for USB OS disk, got errors: {errors}"
    assert len(errors) == 0
    assert any("USB" in w or "removable" in w.lower() for w in warnings), \
        f"Expected USB warning, got: {warnings}"


@patch("disk_ops._disk_size_bytes", return_value=32 * 1024**3)
@patch("disk_ops._has_ethos_data_on_disk", return_value=False)
@patch("disk_ops._smart_status", return_value="ok")
@patch("disk_ops._run")
def test_validate_usb_boot_still_rejected(mock_run, mock_smart, mock_ethos, mock_size):
    """Boot USB (installer itself) must still be rejected as OS disk."""
    mock_run.return_value = (SAMPLE_LSBLK_WITH_USB_TARGET, "", 0)
    ok, errors, _ = disk_ops.validate("sdb", None, "sdb")
    assert ok is False
    assert any("boot" in e.lower() for e in errors)


# ── _fixup_installed_system tests ──────────────────────────────────────────

def _make_ext4_tree(tmp):
    """Create a fake ext4 install tree with installer artefacts."""
    ethos = os.path.join(tmp, "opt/ethos")
    os.makedirs(ethos, exist_ok=True)
    os.makedirs(os.path.join(tmp, "etc/systemd/system/multi-user.target.wants"), exist_ok=True)

    # Installer artefacts that must be removed
    open(os.path.join(ethos, ".installer-mode"), "w").close()
    open(os.path.join(ethos, ".installed"), "w").close()

    # Preboot wants link that must be removed
    preboot = os.path.join(tmp, "etc/systemd/system/multi-user.target.wants/ethos-preboot.service")
    os.symlink("/etc/systemd/system/ethos-preboot.service", preboot)

    # Source firstboot script
    images_dir = os.path.join(ethos, "installer/images")
    os.makedirs(images_dir, exist_ok=True)
    fb = os.path.join(images_dir, "firstboot-v2.sh")
    with open(fb, "w") as f:
        f.write("#!/bin/bash\necho firstboot\n")
    return tmp


def _make_squashfs_tree(tmp):
    """Create a fake squashfs install tree (overlay structure)."""
    os.makedirs(os.path.join(tmp, "overlay/upper"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "overlay/work"), exist_ok=True)

    # Installer's own opt/ethos (on ext4, not the squashfs)
    ethos = os.path.join(tmp, "opt/ethos")
    os.makedirs(ethos, exist_ok=True)
    images_dir = os.path.join(ethos, "installer/images")
    os.makedirs(images_dir, exist_ok=True)
    with open(os.path.join(images_dir, "firstboot-v2.sh"), "w") as f:
        f.write("#!/bin/bash\necho firstboot\n")
    return tmp


def test_fixup_ext4_removes_flags():
    """ext4 mode: .installer-mode and .installed are deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_ext4_tree(tmp)
        disk_ops._fixup_installed_system(tmp, squashfs_mode=False)

        ethos = os.path.join(tmp, "opt/ethos")
        assert not os.path.exists(os.path.join(ethos, ".installer-mode"))
        assert not os.path.exists(os.path.join(ethos, ".installed"))


def test_fixup_ext4_writes_ethos_service():
    """ext4 mode: ethos.service is written to etc/systemd/system/."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_ext4_tree(tmp)
        disk_ops._fixup_installed_system(tmp, squashfs_mode=False)

        svc = os.path.join(tmp, "etc/systemd/system/ethos.service")
        assert os.path.isfile(svc)
        content = open(svc).read()
        assert "ExecStart=" in content
        assert "WantedBy=multi-user.target" in content


def test_fixup_ext4_enables_ethos_disables_preboot():
    """ext4 mode: ethos.service is enabled, preboot link is removed."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_ext4_tree(tmp)
        disk_ops._fixup_installed_system(tmp, squashfs_mode=False)

        wants = os.path.join(tmp, "etc/systemd/system/multi-user.target.wants")
        assert os.path.islink(os.path.join(wants, "ethos.service"))
        assert not os.path.exists(os.path.join(wants, "ethos-preboot.service"))


def test_fixup_ext4_deploys_firstboot():
    """ext4 mode: firstboot-v2.sh is deployed."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_ext4_tree(tmp)
        disk_ops._fixup_installed_system(tmp, squashfs_mode=False)

        fb = os.path.join(tmp, "opt/ethos-firstboot.sh")
        assert os.path.isfile(fb)
        assert os.access(fb, os.X_OK)


def test_fixup_squashfs_whiteouts_flags():
    """squashfs mode: .installer-mode and .installed become whiteout devices."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_squashfs_tree(tmp)
        disk_ops._fixup_installed_system(tmp, squashfs_mode=True)

        overlay_ethos = os.path.join(tmp, "overlay/upper/opt/ethos")
        for flag in (".installer-mode", ".installed"):
            path = os.path.join(overlay_ethos, flag)
            assert os.path.exists(path), f"whiteout missing: {flag}"
            s = os.stat(path)
            assert stat.S_ISCHR(s.st_mode), f"{flag} is not a char device"
            assert os.major(s.st_rdev) == 0 and os.minor(s.st_rdev) == 0


def test_fixup_squashfs_writes_service_to_overlay():
    """squashfs mode: ethos.service is written to overlay/upper/."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_squashfs_tree(tmp)
        disk_ops._fixup_installed_system(tmp, squashfs_mode=True)

        svc = os.path.join(tmp, "overlay/upper/etc/systemd/system/ethos.service")
        assert os.path.isfile(svc)
        content = open(svc).read()
        assert "ExecStart=" in content

        # Must NOT write outside overlay in squashfs mode
        wrong = os.path.join(tmp, "etc/systemd/system/ethos.service")
        assert not os.path.exists(wrong)


def test_fixup_squashfs_enables_ethos_disables_preboot_in_overlay():
    """squashfs mode: wants symlinks go into overlay/upper/."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_squashfs_tree(tmp)
        disk_ops._fixup_installed_system(tmp, squashfs_mode=True)

        wants = os.path.join(tmp, "overlay/upper/etc/systemd/system/multi-user.target.wants")
        assert os.path.islink(os.path.join(wants, "ethos.service"))
        preboot = os.path.join(wants, "ethos-preboot.service")
        # Either removed or whiteout-blocked
        if os.path.exists(preboot):
            s = os.stat(preboot)
            assert stat.S_ISCHR(s.st_mode)


def test_fixup_squashfs_deploys_firstboot_to_overlay():
    """squashfs mode: firstboot-v2.sh lands in overlay/upper/."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_squashfs_tree(tmp)
        disk_ops._fixup_installed_system(tmp, squashfs_mode=True)

        fb = os.path.join(tmp, "overlay/upper/opt/ethos-firstboot.sh")
        assert os.path.isfile(fb)
        assert os.access(fb, os.X_OK)
