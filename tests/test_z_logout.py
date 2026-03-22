"""
EthOS NAS – Logout test (runs last to avoid invalidating the session token).
"""

import requests
import pytest
from helpers import BASE_URL, USERNAME, PASSWORD


class TestLogout:
    def test_logout(self, base_url, auth_token):
        """Logout using the session token, then verify it's invalidated.
        This test runs last (z_ prefix) because it invalidates the token.
        """
        resp = requests.post(
            f"{base_url}/api/auth/logout",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=10,
        )
        assert resp.status_code == 200

        verify = requests.get(
            f"{base_url}/api/auth/verify",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=10,
        )
        assert verify.status_code == 401
