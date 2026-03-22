"""
EthOS NAS – Docker endpoint tests.
"""

import requests
from helpers import api_get


class TestDocker:
    def test_list_containers(self, api_session):
        resp = api_get(api_session, "/api/docker/containers")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))

    def test_list_images(self, api_session):
        resp = api_get(api_session, "/api/docker/images")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))

    def test_containers_unauthenticated(self, base_url):
        resp = requests.get(f"{base_url}/api/docker/containers", timeout=10)
        assert resp.status_code == 401

    def test_images_unauthenticated(self, base_url):
        resp = requests.get(f"{base_url}/api/docker/images", timeout=10)
        assert resp.status_code == 401
