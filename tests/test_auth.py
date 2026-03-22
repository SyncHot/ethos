"""
EthOS NAS – Authentication tests.
"""

import requests
import pytest
from helpers import BASE_URL, USERNAME, PASSWORD


class TestLogin:
    """Login endpoint tests."""

    def test_login_success(self, base_url, auth_token):
        """Verify login works — the session-scoped auth_token proves it."""
        assert auth_token and len(auth_token) > 10

    def test_login_returns_user_info(self, base_url, auth_token):
        """Verify the token's user info via /verify (avoids extra login)."""
        resp = requests.get(
            f"{base_url}/api/auth/verify",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("valid") is True
        user = data.get("user", {})
        assert user.get("username") == USERNAME
        assert user.get("role") in ("admin", "user")

    def test_login_wrong_password(self, base_url):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": USERNAME, "password": "wrong_password_xyz"},
            timeout=10,
        )
        assert resp.status_code == 401
        assert "error" in resp.json()

    def test_login_empty_body(self, base_url):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={},
            timeout=10,
        )
        assert resp.status_code in (400, 401, 500)

    def test_login_no_json(self, base_url):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            data="not json",
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        assert resp.status_code in (400, 401, 415)


class TestVerify:
    def test_verify_valid_token(self, base_url, auth_token):
        resp = requests.get(
            f"{base_url}/api/auth/verify",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("valid") is True
        assert data["user"]["username"] == USERNAME

    def test_verify_invalid_token(self, base_url):
        resp = requests.get(
            f"{base_url}/api/auth/verify",
            headers={"Authorization": "Bearer invalid_token_abc123"},
            timeout=10,
        )
        assert resp.status_code == 401
        assert resp.json().get("valid") is False
