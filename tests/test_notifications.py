"""
EthOS NAS – Notifications endpoint tests.
"""

import requests
from helpers import api_get


class TestNotifications:
    def test_get_notification_config(self, api_session):
        resp = api_get(api_session, "/api/notifications/config")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_get_notification_history(self, api_session):
        resp = api_get(api_session, "/api/notifications/history")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))

    def test_notifications_config_unauthenticated(self, base_url):
        resp = requests.get(f"{base_url}/api/notifications/config", timeout=10)
        assert resp.status_code in (200, 401)

    def test_notifications_history_unauthenticated(self, base_url):
        resp = requests.get(f"{base_url}/api/notifications/history", timeout=10)
        assert resp.status_code in (200, 401)
