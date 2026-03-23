"""Tests for WiFi operations (mocked nmcli)."""

import sys
import os
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "installer", "preboot"))

import wifi_ops


@patch("wifi_ops._run")
def test_scan_returns_sorted_networks(mock_run):
    mock_run.side_effect = [
        ("", 0),  # rescan
        (
            "HomeNet:85:WPA2:*\n"
            "CafeWiFi:42::\n"
            "Office:71:WPA2:\n"
            "HomeNet:60:WPA2:\n",
            0,
        ),
    ]
    result = wifi_ops.scan()
    assert len(result) == 3
    assert result[0]["ssid"] == "HomeNet"
    assert result[0]["signal"] == 85
    assert result[0]["connected"] is True
    assert result[1]["ssid"] == "Office"
    assert result[2]["ssid"] == "CafeWiFi"


@patch("wifi_ops._run")
def test_scan_empty(mock_run):
    mock_run.side_effect = [("", 0), ("", 0)]
    assert wifi_ops.scan() == []


@patch("wifi_ops._run")
def test_connect_success(mock_run):
    mock_run.side_effect = [
        ("", 0),  # delete old
        ("Device 'wlan0' connected.", 0),  # connect
    ]
    with patch("wifi_ops._get_wifi_ip", return_value="192.168.1.50"):
        ok, msg, ip = wifi_ops.connect("MyNet", "pass123")
    assert ok is True
    assert ip == "192.168.1.50"


@patch("wifi_ops._run")
def test_connect_failure(mock_run):
    mock_run.side_effect = [
        ("", 0),  # delete old
        ("Error: No network with SSID", 1),  # connect fail
    ]
    ok, msg, ip = wifi_ops.connect("BadNet", "wrong")
    assert ok is False


@patch("wifi_ops._run")
def test_has_ethernet_true(mock_run):
    mock_run.return_value = ("ethernet:connected\nwifi:disconnected", 0)
    assert wifi_ops.has_ethernet() is True


@patch("wifi_ops._run")
def test_has_ethernet_false(mock_run):
    mock_run.return_value = ("wifi:connected", 0)
    assert wifi_ops.has_ethernet() is False


@patch("wifi_ops._run")
def test_has_wifi_device(mock_run):
    mock_run.return_value = ("wifi", 0)
    assert wifi_ops.has_wifi_device() is True
    mock_run.return_value = ("ethernet", 0)
    assert wifi_ops.has_wifi_device() is False


@patch("wifi_ops._run")
def test_status_ethernet(mock_run):
    def side_effect(cmd, **kw):
        if "TYPE,STATE" in cmd:
            return "ethernet:connected", 0
        if "ip -4" in cmd:
            return "192.168.1.10", 0
        if "TYPE device" in cmd:
            return "wifi", 0
        if "connection show --active" in cmd:
            return "", 0
        return "", 0

    mock_run.side_effect = side_effect
    s = wifi_ops.status()
    assert s["ethernet"] is True
    assert s["has_network"] is True


def test_save_wifi_config(tmp_path):
    """save_wifi_config writes a valid NM connection file."""
    ok = wifi_ops.save_wifi_config("MyNet", "secret123", str(tmp_path))
    assert ok is True
    nm_dir = tmp_path / "etc" / "NetworkManager" / "system-connections"
    files = list(nm_dir.glob("*.nmconnection"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "ssid=MyNet" in content
    assert "psk=secret123" in content
    assert "key-mgmt=wpa-psk" in content
    assert "autoconnect=true" in content
    # File permissions: 0o600
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"


def test_save_wifi_config_special_chars(tmp_path):
    """WiFi SSID with special characters gets safe filename."""
    wifi_ops.save_wifi_config("My/Net@Home!", "pass", str(tmp_path))
    nm_dir = tmp_path / "etc" / "NetworkManager" / "system-connections"
    files = list(nm_dir.glob("*.nmconnection"))
    assert len(files) == 1
    # SSID in file content should be exact
    assert "ssid=My/Net@Home!" in files[0].read_text()
    # Filename should be sanitized
    assert "/" not in files[0].name
