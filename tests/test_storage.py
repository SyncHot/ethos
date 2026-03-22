"""
EthOS NAS – Storage endpoint tests.
"""

import requests
from helpers import api_get


class TestStorage:
    def test_list_drives(self, api_session):
        resp = api_get(api_session, "/api/storage/drives")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "drives" in data

    def test_list_shares(self, api_session):
        resp = api_get(api_session, "/api/storage/samba/shares")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))

    def test_drives_unauthenticated(self, base_url):
        resp = requests.get(f"{base_url}/api/storage/drives", timeout=10)
        assert resp.status_code == 401

    def test_shares_unauthenticated(self, base_url):
        resp = requests.get(f"{base_url}/api/storage/samba/shares", timeout=10)
        assert resp.status_code == 401
