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
