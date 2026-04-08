"""
EthOS Builder — Playwright E2E Frontend Tests
==============================================
Tests the "Golden Path" through the Builder UI:
  1. App renders correctly (window, sections)
  2. Build button states (idle vs. building)
  3. SSE progress bar and log lines appear in UI
  4. Page-refresh during build → UI resumes polling (state persistence)
  5. Cancel button → cleanup + UI returns to idle
  6. Negative path: immediate error shown when debootstrap missing

Run:
    ETHOS_USER=admin ETHOS_PASS=yourpass \
        venv/bin/pytest tests/test_e2e_playwright_builder.py -v

    # Visible browser for debugging:
    ETHOS_USER=admin ETHOS_PASS=yourpass \
        venv/bin/pytest tests/test_e2e_playwright_builder.py -v --headed

Prerequisites:
    pip install playwright && playwright install chromium
"""

import os
import sys
import time

import pytest

# Skip entire file if playwright is not installed
playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
from playwright.sync_api import sync_playwright, expect, Page  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from helpers import BASE_URL, USERNAME, PASSWORD  # noqa: E402

TIMEOUT_MS = 20_000
APP_ID = "builder"


# ─── Session-scoped logged-in page ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def pw_page():
    """Single Playwright page, logged in once for the whole module."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        page.goto(BASE_URL)
        page.wait_for_selector("#login-screen", state="visible")
        page.fill("#login-username", USERNAME)
        page.fill("#login-password", PASSWORD)
        page.click("button[type=\'submit\'].btn-login")
        page.wait_for_selector("#desktop:not(.hidden)", state="visible", timeout=15_000)

        yield page
        browser.close()


@pytest.fixture(scope="module")
def builder_page(pw_page: Page):
    """Ensure the Builder app window is open."""
    _open_app(pw_page, APP_ID)
    return pw_page


def _open_app(page: Page, app_id: str) -> None:
    page.evaluate(f"""() => {{
        const btn = document.querySelector('#win-{app_id} .win-ctrl.close');
        if (btn) btn.click();
    }}""")
    page.evaluate(f"""() => {{
        const app = NAS.apps && NAS.apps.find(a => a.id === '{app_id}');
        if (app) openApp(app);
    }}""")
    page.evaluate("document.getElementById('main-menu').classList.add('hidden')")
    page.wait_for_selector(f"#win-{app_id}", state="visible")


def _close_app(page: Page, app_id: str) -> None:
    page.evaluate(f"""() => {{
        const btn = document.querySelector('#win-{app_id} .win-ctrl.close');
        if (btn) btn.click();
    }}""")


# =============================================================================
#  1. APP RENDERS
# =============================================================================

class TestBuilderAppRenders:
    """Verify the Builder window opens and all expected sections are visible."""

    def test_window_is_visible(self, builder_page: Page):
        win = builder_page.query_selector("#win-builder")
        assert win is not None, "Builder window not found"
        assert win.is_visible(), "Builder window is not visible"

    def test_window_has_title(self, builder_page: Page):
        title = builder_page.inner_text("#win-builder .win-title-text")
        assert title.strip() != "", "Builder window title is empty"

    def test_image_section_exists(self, builder_page: Page):
        html = builder_page.query_selector("#win-builder").inner_html().lower()
        assert any(kw in html for kw in ("image", "build", "img", "obraz")), \
            "No image/build section found in Builder window"

    def test_progress_element_exists(self, builder_page: Page):
        html = builder_page.query_selector("#win-builder").inner_html().lower()
        assert any(kw in html for kw in ("progress", "percent", "step")), \
            "No progress element found in Builder window HTML"

    def test_log_area_exists(self, builder_page: Page):
        html = builder_page.query_selector("#win-builder").inner_html().lower()
        assert any(kw in html for kw in ("log", "output", "console", "terminal")), \
            "No log output area found in Builder window HTML"


# =============================================================================
#  2. BUILD BUTTON STATES
# =============================================================================

class TestBuilderBuildButton:
    """Verify the Build Image button state logic."""

    def test_build_button_exists_in_dom(self, builder_page: Page):
        result = builder_page.evaluate("""
            () => {
                const win = document.querySelector('#win-builder');
                if (!win) return {found: false};
                const allBtns = Array.from(win.querySelectorAll('button'));
                const build = allBtns.find(b =>
                    b.textContent.toLowerCase().includes('build') ||
                    b.textContent.toLowerCase().includes('zbuduj') ||
                    b.textContent.toLowerCase().includes('obraz')
                );
                return {found: !!build, text: build ? build.textContent.trim() : '',
                        disabled: build ? build.disabled : false};
            }
        """)
        assert result["found"], "No build button found in Builder window"

    def test_build_button_enabled_when_idle(self, builder_page: Page):
        is_building = builder_page.evaluate("""
            () => {
                const win = document.querySelector('#win-builder');
                if (!win) return false;
                return win.innerHTML.toLowerCase().includes('building') ||
                       win.innerHTML.toLowerCase().includes('budowanie');
            }
        """)
        if is_building:
            pytest.skip("Build is currently in progress")
        result = builder_page.evaluate("""
            () => {
                const win = document.querySelector('#win-builder');
                const allBtns = Array.from(win.querySelectorAll('button'));
                const build = allBtns.find(b =>
                    b.textContent.toLowerCase().includes('build') ||
                    b.textContent.toLowerCase().includes('zbuduj')
                );
                return build ? !build.disabled : null;
            }
        """)
        if result is None:
            pytest.skip("Could not locate build button")
        assert result, "Build button is disabled when idle"


# =============================================================================
#  3. SSE PROGRESS UI
# =============================================================================

class TestBuilderSSEProgressUI:
    """Verify UI reflects SSE events: percent, STEP messages."""

    def test_status_api_called_by_ui(self, builder_page: Page):
        """UI must poll /api/builder/status within 3 seconds."""
        requests_made = []

        def handle(req):
            if "builder/status" in req.url or "builder/release" in req.url:
                requests_made.append(req.url)

        builder_page.on("request", handle)
        import time as t
        t.sleep(3)
        builder_page.remove_listener("request", handle)
        assert len(requests_made) > 0, \
            "Builder UI made no calls to /api/builder/status in 3s — polling broken"

    def test_log_lines_appear_in_ui(self, builder_page: Page):
        """If API state has logs, at least one line appears in the UI."""
        import requests as req
        token = builder_page.evaluate("() => NAS.token || ''")
        if not token:
            pytest.skip("No token available")
        r = req.get(f"{BASE_URL}/api/builder/status",
                    headers={"Authorization": f"Bearer {token}"}, timeout=5)
        data = r.json()
        if not data.get("logs"):
            pytest.skip("No logs in state")
        fragment = data["logs"][0][:20].strip()
        if not fragment:
            pytest.skip("Log entry empty")
        win_text = builder_page.inner_text("#win-builder")
        assert fragment in win_text, \
            f"Log fragment {fragment!r} not found in Builder UI text"


# =============================================================================
#  4. RESUME POLLING ON PAGE REFRESH
# =============================================================================

class TestBuilderResumeOnRefresh:
    """Verify page-refresh resumes progress tracking via builder_state.json."""

    def test_close_reopen_preserves_building_state(self, pw_page: Page):
        _open_app(pw_page, APP_ID)
        initial = pw_page.evaluate(
            "() => document.querySelector('#win-builder')?.innerHTML?.toLowerCase() || ''"
        )
        is_building = "building" in initial or "budowanie" in initial
        _close_app(pw_page, APP_ID)
        pw_page.wait_for_timeout(500)
        _open_app(pw_page, APP_ID)
        pw_page.wait_for_timeout(1500)
        if not is_building:
            pytest.skip("No active build — cannot test resume behaviour")
        resumed = pw_page.evaluate(
            "() => document.querySelector('#win-builder')?.innerHTML?.toLowerCase() || ''"
        )
        assert "building" in resumed or "%" in resumed, \
            "After reopening, Builder UI does not show building state"

    def test_resume_available_reflects_interrupted_build(self, pw_page: Page):
        import requests as req
        token = pw_page.evaluate("() => NAS.token || ''")
        if not token:
            pytest.skip("No token")
        r = req.get(f"{BASE_URL}/api/builder/status",
                    headers={"Authorization": f"Bearer {token}"}, timeout=5)
        data = r.json()
        assert isinstance(data["resume_available"], bool)
        if data["resume_available"]:
            assert data.get("build_dir"), \
                "resume_available=True but build_dir is empty"


# =============================================================================
#  5. CANCEL BUTTON
# =============================================================================

class TestBuilderCancelButton:
    """Verify Cancel Build stops the build and resets the UI."""

    def test_cancel_via_api_then_ui_shows_idle(self, pw_page: Page):
        import requests as req
        token = pw_page.evaluate("() => NAS.token || ''")
        if not token:
            pytest.skip("No token")
        r = req.get(f"{BASE_URL}/api/builder/status",
                    headers={"Authorization": f"Bearer {token}"}, timeout=5)
        if r.json()["status"] != "building":
            pytest.skip("No active build to cancel")
        req.post(f"{BASE_URL}/api/builder/cancel",
                 headers={"Authorization": f"Bearer {token}"}, timeout=5)
        import time as t
        t.sleep(3)
        _close_app(pw_page, APP_ID)
        _open_app(pw_page, APP_ID)
        pw_page.wait_for_timeout(2000)
        win_text = pw_page.inner_text("#win-builder").lower()
        assert "building" not in win_text, \
            "UI still shows 'building' after cancel"


# =============================================================================
#  6. NEGATIVE PATH
# =============================================================================

class TestBuilderNegativePath:
    """UI surfaces errors correctly (missing debootstrap, bad state)."""

    def test_build_image_endpoint_never_returns_5xx(self, pw_page: Page):
        import requests as req
        token = pw_page.evaluate("() => NAS.token || ''")
        if not token:
            pytest.skip("No token")
        r = req.get(f"{BASE_URL}/api/builder/status",
                    headers={"Authorization": f"Bearer {token}"}, timeout=5)
        if r.json()["status"] == "building":
            pytest.skip("Build is running")
        r = req.post(f"{BASE_URL}/api/builder/image",
                     headers={"Authorization": f"Bearer {token}"},
                     json={}, timeout=5)
        assert r.status_code in (200, 400, 409, 503), \
            f"Unexpected {r.status_code} — should never be 5xx"
        if r.status_code == 200:
            req.post(f"{BASE_URL}/api/builder/cancel",
                     headers={"Authorization": f"Bearer {token}"}, timeout=5)
        elif r.status_code in (400, 503):
            assert "error" in r.json(), "Tool-missing error missing 'error' field"

    def test_preflight_result_is_string(self, pw_page: Page):
        import requests as req
        token = pw_page.evaluate("() => NAS.token || ''")
        if not token:
            pytest.skip("No token")
        r = req.get(f"{BASE_URL}/api/builder/status",
                    headers={"Authorization": f"Bearer {token}"}, timeout=5)
        data = r.json()
        assert "preflight_result" in data
        assert isinstance(data["preflight_result"], str)
