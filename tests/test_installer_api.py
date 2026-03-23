"""Integration tests for installer Flask API."""

import sys
import os
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "installer", "preboot"))

from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_index_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"EthOS Installer" in r.data


def test_languages(client):
    r = client.get("/api/languages")
    codes = [l["code"] for l in r.get_json()]
    assert "pl" in codes and "en" in codes


def test_i18n_en(client):
    r = client.get("/api/i18n/en")
    assert r.get_json()["Dalej"] == "Next"


@patch("wifi_ops.scan")
def test_wifi_scan(mock_scan, client):
    mock_scan.return_value = [{"ssid": "TestNet", "signal": 80, "security": "WPA2", "connected": False}]
    r = client.get("/api/wifi/scan")
    assert len(r.get_json()["networks"]) == 1


@patch("wifi_ops.save_wifi_priority")
@patch("wifi_ops.connect")
def test_wifi_connect_ok(mock_connect, mock_prio, client):
    mock_connect.return_value = (True, "OK", "192.168.1.50")
    r = client.post("/api/wifi/connect", json={"ssid": "Net", "password": "p"})
    d = r.get_json()
    assert d["ok"] is True
    assert d["ip"] == "192.168.1.50"


def test_wifi_connect_no_ssid(client):
    r = client.post("/api/wifi/connect", json={"password": "p"})
    assert r.status_code == 400


@patch("wifi_ops.status")
def test_wifi_status(mock_s, client):
    mock_s.return_value = {"ethernet": True, "ethernet_ip": "10.0.0.1", "has_wifi": False, "wifi_ssid": None, "wifi_ip": None, "has_network": True}
    assert client.get("/api/wifi/status").get_json()["has_network"] is True


@patch("disk_ops.discover")
def test_disk_discover(mock_d, client):
    mock_d.return_value = {"disks": [{"name": "sda"}], "boot_device": "sdb"}
    assert len(client.get("/api/disks/discover").get_json()["disks"]) == 1


@patch("disk_ops.validate")
def test_disk_validate_ok(mock_v, client):
    mock_v.return_value = (True, [], [])
    r = client.post("/api/disks/validate", json={"os_disk": "sda", "data_disk": "same", "boot_device": "sdb"})
    assert r.get_json()["ok"] is True


def test_install_no_disk(client):
    r = client.post("/api/install/start", json={"username": "a", "password": "pppp", "confirmation": "INSTALUJ"})
    assert r.get_json()["ok"] is False


def test_install_bad_confirm(client):
    r = client.post("/api/install/start", json={"os_disk": "sda", "username": "a", "password": "pppp", "confirmation": "WRONG"})
    assert r.get_json()["ok"] is False


def test_install_short_pass(client):
    r = client.post("/api/install/start", json={"os_disk": "sda", "username": "a", "password": "ab", "confirmation": "INSTALUJ"})
    assert r.get_json()["ok"] is False


def test_progress_initial(client):
    r = client.get("/api/install/progress")
    assert "percent" in r.get_json()


def test_reboot_before_done(client):
    assert client.post("/api/install/reboot").status_code == 400
