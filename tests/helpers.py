"""
EthOS NAS – test helpers and constants.
"""

import os
import requests

BASE_URL = os.environ.get("ETHOS_BASE_URL", "http://localhost:9000")
USERNAME = os.environ.get("ETHOS_USER", "admin")
PASSWORD = os.environ.get("ETHOS_PASS", "")

DEFAULT_TIMEOUT = 15


def api_get(session, path, **kwargs):
    """GET helper that prepends the base_url."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return session.get(f"{session.base_url}{path}", **kwargs)


def api_post(session, path, **kwargs):
    """POST helper that prepends the base_url."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return session.post(f"{session.base_url}{path}", **kwargs)
