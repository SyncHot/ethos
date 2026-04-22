"""Tests for disk operations (mocked system commands)."""

import sys
import os
import json
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
