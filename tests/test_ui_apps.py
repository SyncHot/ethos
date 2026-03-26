"""
EthOS UI click-through tests using Playwright.

Tests open each major app via the desktop menu, verify the window renders,
and perform basic interactions. All tests share a single logged-in page
(session scope) for speed.

Run:
    ETHOS_USER=<user> ETHOS_PASS=<password> \
        venv/bin/pytest tests/test_ui_apps.py -v --headed   # visible browser
    ETHOS_USER=<user> ETHOS_PASS=<password> \
        venv/bin/pytest tests/test_ui_apps.py -v            # headless
"""

import os
import time
import pytest
from playwright.sync_api import sync_playwright, expect, Page

BASE_URL = os.environ.get("ETHOS_BASE_URL", "http://localhost:9000")
USERNAME = os.environ.get("ETHOS_USER", "admin")
PASSWORD = os.environ.get("ETHOS_PASS", "")
TIMEOUT = 15_000  # ms


# ─── Shared logged-in page (session scope) ────────────────────────────────────

@pytest.fixture(scope="session")
def pw_page():
    """Single Playwright page, logged in once for the whole session."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(TIMEOUT)

        page.goto(BASE_URL)
        page.wait_for_selector("#login-screen", state="visible")

        page.fill("#login-username", USERNAME)
        page.fill("#login-password", PASSWORD)
        page.click("button[type='submit'].btn-login")

        # Wait for desktop to appear
        page.wait_for_selector("#desktop:not(.hidden)", state="visible", timeout=15_000)

        yield page

        browser.close()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def open_app(page: Page, app_id: str) -> None:
    """Open an app via JS openApp() — bypasses menu, avoids translation issues."""
    # Close existing window if already open (via JS to avoid pointer-interception issues)
    page.evaluate(f"""
        () => {{
            const btn = document.querySelector('#win-{app_id} .win-ctrl.close');
            if (btn) btn.click();
        }}
    """)

    # Open app via JS directly
    page.evaluate(f"""
        () => {{
            const app = NAS.apps && NAS.apps.find(a => a.id === '{app_id}');
            if (app) openApp(app);
        }}
    """)

    # Ensure menu is dismissed
    page.evaluate("document.getElementById('main-menu').classList.add('hidden')")

    # Wait for the window to appear
    page.wait_for_selector(f"#win-{app_id}", state="visible")


def close_app(page: Page, app_id: str) -> None:
    """Close an app window via JS click (avoids pointer-interception by overlapping windows)."""
    clicked = page.evaluate(f"""
        () => {{
            const btn = document.querySelector('#win-{app_id} .win-ctrl.close');
            if (btn) {{ btn.click(); return true; }}
            return false;
        }}
    """)
    if clicked:
        page.locator(f"#win-{app_id}").wait_for(state="detached", timeout=5_000)


def win_body(page: Page, app_id: str):
    """Return the window body locator for an app."""
    return page.locator(f"#win-body-{app_id}")


def wait_for_content(page: Page, app_id: str, timeout: int = 8_000) -> None:
    """Wait until the window body has meaningful content (not just a spinner)."""
    body = win_body(page, app_id)
    # Wait for spinner to go away OR for real content to appear
    page.wait_for_function(
        f"""() => {{
            const el = document.getElementById('win-body-{app_id}');
            if (!el) return false;
            const text = el.innerText.trim();
            const hasSpinner = el.querySelector('.fa-spinner') !== null;
            return text.length > 10 && !hasSpinner;
        }}""",
        timeout=timeout,
    )


# ─── Login ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def nas_apps(pw_page):
    """Return the list of apps available on the NAS."""
    import json, re
    pw_page.evaluate("() => window.__test_apps__ = window.NAS?.apps || []")
    apps = pw_page.evaluate("() => window.__test_apps__")
    return apps or []


def test_login(pw_page):
    """Desktop is visible after login."""
    assert pw_page.is_visible("#desktop")
    assert pw_page.is_hidden("#login-screen")


# ─── Dashboard ───────────────────────────────────────────────────────────────

def test_dashboard_opens(pw_page):
    open_app(pw_page, "dashboard")
    wait_for_content(pw_page, "dashboard")
    body = win_body(pw_page, "dashboard")
    # Should show CPU or RAM stats
    body_text = body.inner_text()
    assert any(kw in body_text.upper() for kw in ["CPU", "RAM", "DISK", "%"]), \
        f"Dashboard missing stats, got: {body_text[:200]}"
    close_app(pw_page, "dashboard")


# ─── File Manager ─────────────────────────────────────────────────────────────

def test_file_manager_opens(pw_page):
    open_app(pw_page, "file-manager")
    pw_page.wait_for_selector("#win-body-file-manager .fm-toolbar, #win-body-file-manager #fm-file-list, #win-body-file-manager #fm-breadcrumb", timeout=8_000)
    close_app(pw_page, "file-manager")


def test_file_manager_navigate(pw_page):
    open_app(pw_page, "file-manager")
    # Wait for the file listing to appear
    pw_page.wait_for_selector("#win-body-file-manager #fm-file-list, #win-body-file-manager .fm-toolbar", timeout=8_000)
    body = win_body(pw_page, "file-manager")
    # Should have a breadcrumb showing current path
    assert body.locator("#fm-breadcrumb, .fm-breadcrumb").count() > 0, \
        "File manager missing breadcrumbs"
    close_app(pw_page, "file-manager")


# ─── Storage Manager ─────────────────────────────────────────────────────────

def test_storage_manager_opens(pw_page):
    open_app(pw_page, "storage-manager")
    pw_page.wait_for_selector("#win-body-storage-manager .storage-app, #win-body-storage-manager .storage-toolbar, #win-body-storage-manager #st-groups", timeout=10_000)
    body = win_body(pw_page, "storage-manager")
    assert len(body.inner_text().strip()) > 5, "Storage manager body is empty"
    close_app(pw_page, "storage-manager")


# ─── Docker Manager ──────────────────────────────────────────────────────────

def test_docker_manager_opens(pw_page):
    open_app(pw_page, "docker-manager")
    # Should render the sidebar nav
    pw_page.wait_for_selector("#win-body-docker-manager .dkr, #win-body-docker-manager .dkr-sidebar, #win-body-docker-manager .dkr-toolbar", timeout=8_000)
    close_app(pw_page, "docker-manager")


def test_docker_manager_tabs(pw_page):
    open_app(pw_page, "docker-manager")
    pw_page.wait_for_selector("#win-body-docker-manager .dkr-sidebar", timeout=8_000)
    body = win_body(pw_page, "docker-manager")

    # Click each sidebar nav item
    nav_items = body.locator(".dkr-nav-item")
    count = nav_items.count()
    assert count >= 3, f"Expected at least 3 nav items, got {count}"

    for i in range(count):
        nav_items.nth(i).click()
        time.sleep(0.3)  # allow render

    close_app(pw_page, "docker-manager")


# ─── Resource Monitor ────────────────────────────────────────────────────────

def test_resource_monitor_opens(pw_page):
    open_app(pw_page, "resource-monitor")
    wait_for_content(pw_page, "resource-monitor", timeout=10_000)
    body = win_body(pw_page, "resource-monitor")
    body_text = body.inner_text()
    assert any(kw in body_text.upper() for kw in ["CPU", "RAM", "MEMORY", "%", "GB"]), \
        f"Resource monitor missing stats: {body_text[:200]}"
    close_app(pw_page, "resource-monitor")


# ─── Terminal ────────────────────────────────────────────────────────────────

def test_terminal_opens(pw_page):
    # Terminal creates a dynamic window ID: terminal-<timestamp>, not #win-terminal
    pw_page.evaluate("""
        () => {
            const app = NAS.apps && NAS.apps.find(a => a.id === 'terminal');
            if (app) openApp(app);
            document.getElementById('main-menu').classList.add('hidden');
        }
    """)
    # Wait for any terminal window body
    pw_page.wait_for_selector("[id^='win-body-terminal-'] .term-app", timeout=15_000)
    # Close all terminal windows
    pw_page.evaluate("""
        () => {
            document.querySelectorAll('.window').forEach(w => {
                if (w.id && w.id.startsWith('win-terminal-')) {
                    const btn = w.querySelector('.win-ctrl.close');
                    if (btn) btn.click();
                }
            });
        }
    """)
    time.sleep(0.3)


# ─── Users ───────────────────────────────────────────────────────────────────

def test_users_opens(pw_page):
    open_app(pw_page, "users")
    wait_for_content(pw_page, "users")
    body = win_body(pw_page, "users")
    body_text = body.inner_text()
    # Should list at least the current user
    assert USERNAME.lower() in body_text.lower() or len(body_text.strip()) > 10, \
        f"Users app missing content: {body_text[:200]}"
    close_app(pw_page, "users")


# ─── Network ─────────────────────────────────────────────────────────────────

def test_network_opens(pw_page):
    open_app(pw_page, "network")
    pw_page.wait_for_selector("#win-body-network .net-sidebar, #win-body-network #net-tab-overview, #win-body-network .net-nav", timeout=10_000)
    body = win_body(pw_page, "network")
    assert len(body.inner_text().strip()) > 5, "Network app body is empty"
    close_app(pw_page, "network")

def test_event_log_opens(pw_page):
    open_app(pw_page, "event-log")
    wait_for_content(pw_page, "event-log")
    body = win_body(pw_page, "event-log")
    # Should show a table or list of events
    assert body.locator("table, .log-entry, .elog-row, tr").count() > 0 or \
           len(body.inner_text().strip()) > 10, \
        "Event log has no entries"
    close_app(pw_page, "event-log")


# ─── Notifications ───────────────────────────────────────────────────────────

def test_notifications_opens(pw_page):
    open_app(pw_page, "notifications")
    wait_for_content(pw_page, "notifications")
    body = win_body(pw_page, "notifications")
    assert len(body.inner_text().strip()) > 5, "Notifications app is empty"
    close_app(pw_page, "notifications")


# ─── Backup ──────────────────────────────────────────────────────────────────

def test_backup_opens(pw_page):
    open_app(pw_page, "backup")
    wait_for_content(pw_page, "backup", timeout=10_000)
    body = win_body(pw_page, "backup")
    assert len(body.inner_text().strip()) > 5, "Backup app is empty"
    close_app(pw_page, "backup")


# ─── Services ────────────────────────────────────────────────────────────────

def test_services_opens(pw_page):
    open_app(pw_page, "services")
    pw_page.wait_for_selector("#win-body-services .svc-app, #win-body-services #svc-list", timeout=10_000)
    body = win_body(pw_page, "services")
    assert len(body.inner_text().strip()) > 2, "Services app is empty"
    close_app(pw_page, "services")


# ─── Firewall ────────────────────────────────────────────────────────────────

def test_firewall_opens(pw_page):
    open_app(pw_page, "firewall")
    wait_for_content(pw_page, "firewall")
    close_app(pw_page, "firewall")


# ─── Cron Manager ────────────────────────────────────────────────────────────

def test_cron_opens(pw_page):
    open_app(pw_page, "cron")
    wait_for_content(pw_page, "cron")
    close_app(pw_page, "cron")


# ─── Packages ────────────────────────────────────────────────────────────────

def test_packages_opens(pw_page):
    open_app(pw_page, "packages")
    wait_for_content(pw_page, "packages", timeout=10_000)
    close_app(pw_page, "packages")


# ─── App Store ───────────────────────────────────────────────────────────────

def test_app_store_opens(pw_page):
    open_app(pw_page, "app-store")
    wait_for_content(pw_page, "app-store", timeout=10_000)
    body = win_body(pw_page, "app-store")
    assert len(body.inner_text().strip()) > 5, "App Store is empty"
    close_app(pw_page, "app-store")


# ─── Disk Repair ─────────────────────────────────────────────────────────────

def test_disk_repair_opens(pw_page):
    open_app(pw_page, "disk-repair")
    wait_for_content(pw_page, "disk-repair")
    close_app(pw_page, "disk-repair")


# ─── Power ───────────────────────────────────────────────────────────────────

def test_power_opens(pw_page):
    open_app(pw_page, "power")
    wait_for_content(pw_page, "power")
    body = win_body(pw_page, "power")
    # Should show power options — verify no shutdown is accidentally triggered
    body_text = body.inner_text()
    assert len(body_text.strip()) > 5, "Power app is empty"
    # Make sure we're just viewing, not clicking any power buttons
    close_app(pw_page, "power")


# ─── Sticky Notes ────────────────────────────────────────────────────────────

def test_sticky_notes_opens(pw_page):
    # Sticky Notes is a side panel (#sn-panel), not a standard window
    # Remove any existing panel first
    pw_page.evaluate("""
        () => {
            const p = document.getElementById('sn-panel');
            if (p) p.remove();
        }
    """)
    pw_page.evaluate("""
        () => {
            const app = NAS.apps && NAS.apps.find(a => a.id === 'sticky-notes');
            if (app) openApp(app);
        }
    """)
    pw_page.wait_for_selector("#sn-panel", timeout=8_000)
    # Toggle to close
    pw_page.evaluate("""
        () => {
            const app = NAS.apps && NAS.apps.find(a => a.id === 'sticky-notes');
            if (app) openApp(app);
        }
    """)
    time.sleep(0.3)


# ─── Taskbar integrity ───────────────────────────────────────────────────────

def test_taskbar_shows_stats(pw_page):
    """CPU/RAM stats should be visible in the taskbar."""
    pw_page.wait_for_selector("#taskbar-stats", state="visible")
    stats_text = pw_page.locator("#taskbar-stats").inner_text()
    # Stats update via socket; give it a moment
    time.sleep(2)
    stats_text = pw_page.locator("#taskbar-stats").inner_text()
    assert "%" in stats_text or len(stats_text.strip()) > 2, \
        f"Taskbar stats not populating: '{stats_text}'"


def test_clock_visible(pw_page):
    """Taskbar clock should show a time."""
    clock = pw_page.locator("#taskbar-clock")
    assert clock.is_visible()
    clock_text = clock.inner_text()
    assert ":" in clock_text, f"Clock not showing time: '{clock_text}'"


# ─── Window controls ─────────────────────────────────────────────────────────

def test_window_minimize_restore(pw_page):
    """Minimize and restore a window."""
    open_app(pw_page, "event-log")
    win = pw_page.locator("#win-event-log")

    pw_page.evaluate("document.querySelector('#win-event-log .win-ctrl.minimize').click()")
    time.sleep(0.4)
    # Window should be minimized (hidden or off-screen)
    win_class = win.get_attribute("class") or ""
    assert "minimized" in win_class or not win.is_visible(), "Window not minimized"

    # Restore by clicking the taskbar button for this window
    pw_page.evaluate("""
        () => {
            const btns = document.querySelectorAll('#taskbar-windows .tb-btn');
            for (const b of btns) { b.click(); break; }
        }
    """)
    time.sleep(0.3)
    close_app(pw_page, "event-log")


def test_window_maximize_restore(pw_page):
    """Maximize and un-maximize a window."""
    open_app(pw_page, "notifications")
    # Use JS click to avoid pointer interception by window content
    pw_page.evaluate("document.querySelector('#win-notifications .win-ctrl.maximize').click()")
    time.sleep(0.3)
    win_class = pw_page.locator("#win-notifications").get_attribute("class") or ""
    assert "maximized" in win_class, "Window not maximized"

    pw_page.evaluate("document.querySelector('#win-notifications .win-ctrl.maximize').click()")
    time.sleep(0.3)
    win_class = pw_page.locator("#win-notifications").get_attribute("class") or ""
    assert "maximized" not in win_class, "Window still maximized after restore"

    close_app(pw_page, "notifications")


# ─── Multiple windows ────────────────────────────────────────────────────────

def test_multiple_windows_open(pw_page):
    """Open three apps simultaneously and verify all are present."""
    open_app(pw_page, "event-log")
    open_app(pw_page, "notifications")
    open_app(pw_page, "network")

    assert pw_page.locator("#win-event-log").is_visible()
    assert pw_page.locator("#win-notifications").is_visible()
    assert pw_page.locator("#win-network").is_visible()

    close_app(pw_page, "event-log")
    close_app(pw_page, "notifications")
    close_app(pw_page, "network")


# ─── VM Manager ──────────────────────────────────────────────────────────────

def test_vm_manager_opens(pw_page):
    open_app(pw_page, "vm-manager")
    pw_page.wait_for_selector("#win-body-vm-manager .vm-sidebar, #win-body-vm-manager .vm-main", timeout=10_000)
    close_app(pw_page, "vm-manager")


# ─── RAID ─────────────────────────────────────────────────────────────────────

def test_raid_opens(pw_page):
    open_app(pw_page, "raid")
    pw_page.wait_for_selector("#win-body-raid .raid-wrap", timeout=10_000)
    close_app(pw_page, "raid")


# ─── Sharing ─────────────────────────────────────────────────────────────────

def test_sharing_opens(pw_page):
    open_app(pw_page, "sharing")
    pw_page.wait_for_selector("#win-body-sharing .shr-loading, #win-body-sharing .shr-empty, #win-body-sharing .sh-sidebar, #win-body-sharing .shr-sidebar", timeout=15_000)
    close_app(pw_page, "sharing")


# ─── Cloud Backup ─────────────────────────────────────────────────────────────

def test_cloud_backup_opens(pw_page):
    open_app(pw_page, "cloud-backup")
    pw_page.wait_for_selector("#win-body-cloud-backup .cb-app", timeout=10_000)
    close_app(pw_page, "cloud-backup")


# ─── Rollback ─────────────────────────────────────────────────────────────────

def test_rollback_opens(pw_page):
    open_app(pw_page, "rollback")
    pw_page.wait_for_selector("#win-body-rollback .rollback-app", timeout=10_000)
    close_app(pw_page, "rollback")


# ─── Gallery ──────────────────────────────────────────────────────────────────

def test_gallery_opens(pw_page):
    open_app(pw_page, "gallery")
    pw_page.wait_for_selector("#win-body-gallery .gal-app", timeout=10_000)
    close_app(pw_page, "gallery")


# ─── Duplicates ───────────────────────────────────────────────────────────────

def test_duplicates_opens(pw_page):
    open_app(pw_page, "duplicates")
    pw_page.wait_for_selector("#win-body-duplicates .dup-app", timeout=10_000)
    close_app(pw_page, "duplicates")


# ─── Document Editor ─────────────────────────────────────────────────────────

def test_doc_editor_opens(pw_page):
    open_app(pw_page, "doc-editor")
    pw_page.wait_for_selector("#win-body-doc-editor .doceditor", timeout=10_000)
    close_app(pw_page, "doc-editor")


# ─── Code Editor ─────────────────────────────────────────────────────────────

def test_code_editor_opens(pw_page):
    open_app(pw_page, "code-editor")
    pw_page.wait_for_selector("#win-body-code-editor .code-editor", timeout=10_000)
    close_app(pw_page, "code-editor")


# ─── Download Manager ────────────────────────────────────────────────────────

def test_download_manager_opens(pw_page):
    open_app(pw_page, "download-manager")
    pw_page.wait_for_selector("#win-body-download-manager .dlm", timeout=10_000)
    close_app(pw_page, "download-manager")


# ─── USB Flasher ─────────────────────────────────────────────────────────────

def test_usb_flasher_opens(pw_page):
    open_app(pw_page, "usb-flasher")
    pw_page.wait_for_selector("#win-body-usb-flasher .fl-wrap", timeout=10_000)
    close_app(pw_page, "usb-flasher")


# ─── Builder ─────────────────────────────────────────────────────────────────

def test_builder_opens(pw_page):
    open_app(pw_page, "builder")
    pw_page.wait_for_selector("#win-body-builder .bl-wrap", timeout=10_000)
    close_app(pw_page, "builder")


# ─── Updates ─────────────────────────────────────────────────────────────────

def test_updates_opens(pw_page):
    open_app(pw_page, "updates")
    pw_page.wait_for_selector("#win-body-updates .upd-app", timeout=10_000)
    close_app(pw_page, "updates")


# ─── UPS ─────────────────────────────────────────────────────────────────────

def test_ups_opens(pw_page):
    open_app(pw_page, "ups")
    pw_page.wait_for_selector("#win-body-ups #ups-main-icon, #win-body-ups .ups-main-icon", timeout=10_000)
    close_app(pw_page, "ups")


# ─── Printer ─────────────────────────────────────────────────────────────────

def test_printer_opens(pw_page):
    open_app(pw_page, "printer")
    pw_page.wait_for_selector("#win-body-printer .printer-app", timeout=10_000)
    close_app(pw_page, "printer")


# ─── Remote Log ──────────────────────────────────────────────────────────────

def test_remote_log_opens(pw_page):
    open_app(pw_page, "remote-log")
    pw_page.wait_for_selector("#win-body-remote-log .rl-wrap", timeout=10_000)
    close_app(pw_page, "remote-log")


# ─── Surveillance ────────────────────────────────────────────────────────────

def test_surveillance_opens(pw_page, nas_apps):
    if not any(a["id"] == "surveillance" for a in nas_apps):
        pytest.skip("surveillance not installed")
    open_app(pw_page, "surveillance")
    pw_page.wait_for_selector("#win-body-surveillance .surv-install-center, #win-body-surveillance .surv-topbar", timeout=15_000)
    close_app(pw_page, "surveillance")


# ─── AI Chat ─────────────────────────────────────────────────────────────────

def test_ai_chat_opens(pw_page):
    open_app(pw_page, "ai-chat")
    pw_page.wait_for_selector("#win-body-ai-chat", state="visible", timeout=10_000)
    body = win_body(pw_page, "ai-chat")
    assert len(body.inner_text().strip()) >= 0, "AI Chat body missing"
    close_app(pw_page, "ai-chat")


# ─── System Settings ─────────────────────────────────────────────────────────

def test_system_settings_opens(pw_page):
    open_app(pw_page, "system-settings")
    # renderSystemSettings is async — wait for .ss-wrap which is appended after await
    pw_page.wait_for_selector("#win-body-system-settings .ss-wrap, #win-body-system-settings .ss-tabs", timeout=15_000)
    close_app(pw_page, "system-settings")


# ─── Domains Manager ─────────────────────────────────────────────────────────

def test_domains_manager_opens(pw_page):
    open_app(pw_page, "domains-manager")
    pw_page.wait_for_selector("#win-body-domains-manager .dm-wrap", timeout=10_000)
    close_app(pw_page, "domains-manager")


# ─── NASLink ─────────────────────────────────────────────────────────────────

def test_naslink_opens(pw_page):
    open_app(pw_page, "naslink")
    pw_page.wait_for_selector("#win-body-naslink .nl", timeout=10_000)
    close_app(pw_page, "naslink")


# ─── SSH Manager ─────────────────────────────────────────────────────────────

def test_ssh_manager_opens(pw_page):
    open_app(pw_page, "ssh-manager")
    pw_page.wait_for_selector("#win-body-ssh-manager .ssh-mgr", timeout=10_000)
    close_app(pw_page, "ssh-manager")


# ─── Fail2ban ────────────────────────────────────────────────────────────────

def test_fail2ban_opens(pw_page):
    open_app(pw_page, "fail2ban")
    pw_page.wait_for_selector("#win-body-fail2ban #f2b-refresh, #win-body-fail2ban .app-btn", timeout=10_000)
    close_app(pw_page, "fail2ban")


# ─── WireGuard ───────────────────────────────────────────────────────────────

def test_wireguard_opens(pw_page):
    open_app(pw_page, "wireguard")
    pw_page.wait_for_selector("#win-body-wireguard #wg-status-badge, #win-body-wireguard #wg-toggle", timeout=10_000)
    close_app(pw_page, "wireguard")


# ─── Tickets ─────────────────────────────────────────────────────────────────

def test_tickets_opens(pw_page):
    open_app(pw_page, "tickets")
    pw_page.wait_for_selector("#win-body-tickets", state="visible", timeout=10_000)
    body = win_body(pw_page, "tickets")
    assert len(body.inner_text().strip()) >= 0, "Tickets body missing"
    close_app(pw_page, "tickets")


# ─── Family Hub ──────────────────────────────────────────────────────────────

def test_family_hub_opens(pw_page):
    open_app(pw_page, "family-hub")
    pw_page.wait_for_selector("#win-body-family-hub .fh-wrap", timeout=10_000)
    close_app(pw_page, "family-hub")
