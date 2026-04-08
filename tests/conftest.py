"""
EthOS NAS – shared pytest fixtures.
"""

import sys
import os
import time
import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))

from helpers import BASE_URL, USERNAME, PASSWORD

_cached_token = None


def _login_with_retry(base_url, retries=5, delay=3):
    """Login with retries to handle transient DB pool exhaustion.

    If ETHOS_TOKEN is set in the environment, it is used directly
    (useful for CI or dev environments where login credentials are unknown).
    """
    global _cached_token

    # Allow pre-injected token via env (e.g. generated directly in DB for dev)
    env_token = os.environ.get("ETHOS_TOKEN", "")
    if env_token:
        r = requests.get(
            f"{base_url}/api/auth/verify",
            headers={"Authorization": f"Bearer {env_token}"},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("valid"):
            _cached_token = env_token
            return env_token

    if _cached_token:
        r = requests.get(
            f"{base_url}/api/auth/verify",
            headers={"Authorization": f"Bearer {_cached_token}"},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("valid"):
            return _cached_token

    for attempt in range(retries):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("totp_required"):
                pytest.skip("2FA is enabled – cannot obtain token automatically")
            token = data.get("token")
            if token:
                _cached_token = token
                return token
        if resp.status_code == 429:
            pytest.skip("Rate limited by server")
        if attempt < retries - 1:
            time.sleep(delay)

    pytest.fail(
        f"Login failed after {retries} attempts: {resp.status_code} {resp.text[:200]}"
    )


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def auth_token(base_url):
    """Authenticate once and return a Bearer token for the whole test session."""
    return _login_with_retry(base_url)


@pytest.fixture(scope="session")
def api_session(base_url, auth_token):
    """requests.Session pre-configured with auth headers and base_url."""
    sess = requests.Session()
    sess.headers.update({
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    })
    sess.base_url = base_url
    return sess
