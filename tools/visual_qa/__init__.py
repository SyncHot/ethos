"""
Action Interpreter — translates manual test step actions into Playwright commands.
Supports Polish and English action patterns.
"""

import re
import logging

log = logging.getLogger("visual_qa.actions")

# ── AppRegistry ID normalization ──
_APP_ALIASES = {
    "dashboard": "dashboard",
    "pulpit": "dashboard",
    "file manager": "file-manager",
    "menedżer plików": "file-manager",
    "pliki": "file-manager",
    "tickets": "tickets",
    "tickety": "tickets",
    "zadania": "tickets",
    "kanban": "tickets",
    "storage": "storage",
    "dyski": "storage",
    "ai chat": "ai-chat",
    "aichat": "ai-chat",
    "czat ai": "ai-chat",
    "backup": "backup",
    "kopia zapasowa": "backup",
    "gallery": "gallery",
    "galeria": "gallery",
    "event log": "event-log",
    "logi": "event-log",
    "docker": "docker-manager",
    "network": "network",
    "sieć": "network",
    "settings": "system-settings",
    "ustawienia": "system-settings",
    "code editor": "code-editor",
    "edytor kodu": "code-editor",
    "surveillance": "surveillance",
    "monitoring": "surveillance",
    "app store": "app-store",
    "sklep": "app-store",
}


def _resolve_app_id(name):
    key = name.strip().lower()
    if key in _APP_ALIASES:
        return _APP_ALIASES[key]
    return re.sub(r'\s+', '-', key)


def _match_open_app(action):
    m = re.match(r'(?:otwórz\s+apkę?|open\s+app)\s+(.+)', action, re.IGNORECASE)
    if not m:
        return None
    app_name = m.group(1).strip().strip('"\'')
    app_id = _resolve_app_id(app_name)

    async def run(page):
        log.info("Opening app: %s", app_id)
        await page.evaluate(f"openApp('{app_id}')")
        await page.wait_for_timeout(2000)
    return run


def _match_click(action):
    m = re.match(r'(?:kliknij|click)\s+(.+)', action, re.IGNORECASE)
    if not m:
        return None
    target = m.group(1).strip().strip('"\'')

    async def run(page):
        log.info("Clicking: %s", target)
        try:
            el = page.get_by_text(target, exact=False).first
            if await el.is_visible():
                await el.click()
                await page.wait_for_timeout(1000)
                return
        except Exception:
            pass
        try:
            await page.click(target, timeout=5000)
            await page.wait_for_timeout(1000)
        except Exception:
            try:
                await page.get_by_role("button", name=target).first.click()
                await page.wait_for_timeout(1000)
            except Exception as e:
                log.warning("Could not click '%s': %s", target, e)
    return run


def _match_fill(action):
    m = re.match(
        r'(?:wpisz|type|wpisać)\s+"([^"]+)"\s+(?:w\s+pole|in\s+field|w)\s+(.+)',
        action, re.IGNORECASE
    )
    if not m:
        return None
    text = m.group(1)
    field = m.group(2).strip().strip('"\'')

    async def run(page):
        log.info("Filling '%s' with '%s'", field, text)
        try:
            await page.get_by_placeholder(field).first.fill(text)
        except Exception:
            try:
                await page.get_by_label(field).first.fill(text)
            except Exception:
                try:
                    await page.fill(field, text)
                except Exception as e:
                    log.warning("Could not fill '%s': %s", field, e)
        await page.wait_for_timeout(500)
    return run


def _match_wait(action):
    m = re.match(r'(?:czekaj|wait|poczekaj)\s+(\d+)\s*(?:s|sek|sekund|seconds?)?', action, re.IGNORECASE)
    if not m:
        return None
    seconds = int(m.group(1))

    async def run(page):
        log.info("Waiting %ds", seconds)
        await page.wait_for_timeout(seconds * 1000)
    return run


def _match_check_visible(action):
    m = re.match(r'(?:sprawdź|check|verify)[:\s]+(.+?)(?:\s+(?:jest|is)\s+(?:widoczn[yae]|visible))?$', action, re.IGNORECASE)
    if not m:
        return None
    target = m.group(1).strip().strip('"\'')

    async def run(page):
        log.info("Checking visible: %s", target)
        try:
            el = page.get_by_text(target, exact=False).first
            await el.wait_for(state='visible', timeout=10000)
        except Exception:
            try:
                await page.wait_for_selector(target, state='visible', timeout=10000)
            except Exception as e:
                log.warning("Element not visible: '%s': %s", target, e)
                raise
    return run


def _match_scroll(action):
    m = re.match(r'(?:przewiń|scroll)\s+(?:w\s+dół|down|w\s+górę|up)', action, re.IGNORECASE)
    if not m:
        return None
    direction = -500 if ('górę' in action.lower() or 'up' in action.lower()) else 500

    async def run(page):
        log.info("Scrolling %s", 'up' if direction < 0 else 'down')
        await page.mouse.wheel(0, direction)
        await page.wait_for_timeout(500)
    return run


def _match_screenshot(action):
    m = re.match(r'screenshot[:\s]+(.+)', action, re.IGNORECASE)
    if not m:
        return None

    async def run(page):
        pass  # Screenshot taken by runner
    return run


_MATCHERS = [
    _match_open_app,
    _match_fill,
    _match_click,
    _match_wait,
    _match_check_visible,
    _match_scroll,
    _match_screenshot,
]


def interpret_action(action_text):
    """Parse an action string and return an async callable(page), or None."""
    action_text = action_text.strip()
    if not action_text:
        return None
    for matcher in _MATCHERS:
        result = matcher(action_text)
        if result is not None:
            return result
    log.warning("No pattern matched action: '%s'", action_text)
    return None
