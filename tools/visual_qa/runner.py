"""
Test Runner — reads manual_tests from a ticket, executes steps via Playwright,
takes screenshots, runs pixel-diff and optional Vision QA.
"""

import asyncio
import json
import logging
import os
import re
import requests as http_requests

log = logging.getLogger("visual_qa.runner")

ETHOS_URL = os.environ.get("ETHOS_URL", "http://localhost:9000")
SCREENSHOTS_DIR = os.environ.get("VQA_SCREENSHOTS_DIR", "/opt/ethos/data/visual_qa/screenshots")
BASELINES_DIR = os.environ.get("VQA_BASELINES_DIR", "/opt/ethos/data/visual_qa/baselines")


def _get_auth_token():
    user = os.environ.get("ETHOS_COPILOT_USER", "nasadmin")
    pwd = os.environ.get("ETHOS_COPILOT_PASS", "ethos")
    try:
        resp = http_requests.post(
            f"{ETHOS_URL}/api/auth/login",
            json={"username": user, "password": pwd}, timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("token")
    except Exception as e:
        log.error("Auth failed: %s", e)
        return None


def _fetch_ticket_tests(ticket_id, token):
    try:
        resp = http_requests.get(
            f"{ETHOS_URL}/api/tickets/tickets/{ticket_id}",
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
        resp.raise_for_status()
        ticket = resp.json()
        return ticket.get("manual_tests", []), ticket.get("title", "")
    except Exception as e:
        log.error("Failed to fetch ticket %s: %s", ticket_id, e)
        return [], ""


async def run_visual_tests(ticket_id, use_vision=True, log_file=None):
    """
    Execute visual tests for a ticket.
    Returns dict: verdict (VIS_QA_PASS/VIS_QA_FAIL/VIS_QA_SKIP), steps, summary.
    """
    from visual_qa import interpret_action
    from visual_qa.pixel_diff import compare as pixel_diff_compare
    from visual_qa.vision_qa import evaluate_screenshot

    token = _get_auth_token()
    if not token:
        return {"verdict": "VIS_QA_SKIP", "steps": [], "summary": "Auth failed"}

    tests, ticket_title = _fetch_ticket_tests(ticket_id, token)
    if not tests:
        return {"verdict": "VIS_QA_SKIP", "steps": [], "summary": "No manual tests defined"}

    ticket_dir = os.path.join(SCREENSHOTS_DIR, ticket_id)
    os.makedirs(ticket_dir, exist_ok=True)
    step_results = []

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # Login
        log.info("Logging into EthOS at %s", ETHOS_URL)
        await page.goto(ETHOS_URL, wait_until="networkidle")
        await page.wait_for_timeout(1000)
        await page.evaluate(f"""() => {{
            localStorage.setItem('auth_token', '{token}');
            location.reload();
        }}""")
        await page.wait_for_timeout(3000)

        for step in tests:
            step_num = step.get("step", 0)
            action = step.get("action", "")
            expected = step.get("expected", "")
            take_screenshot = step.get("screenshot", False)

            _log_msg = f"Step {step_num}: {action}"
            log.info(_log_msg)
            if log_file:
                with open(log_file, "a") as f:
                    f.write(f"\n--- {_log_msg} ---\n")

            step_result = {
                "step": step_num, "action": action, "expected": expected,
                "status": "PASS", "screenshot": None, "vision": None,
                "pixel_diff": None, "error": None,
            }

            action_fn = interpret_action(action)
            if action_fn is None:
                step_result["status"] = "SKIP"
                step_result["error"] = f"Unknown action pattern: {action}"
                log.warning(step_result["error"])
            else:
                try:
                    await action_fn(page)
                except Exception as e:
                    step_result["status"] = "FAIL"
                    step_result["error"] = str(e)
                    log.error("Step %d failed: %s", step_num, e)

            if take_screenshot and step_result["status"] != "SKIP":
                ss_path = os.path.join(ticket_dir, f"step_{step_num}.png")
                try:
                    await page.screenshot(path=ss_path, full_page=False)
                    step_result["screenshot"] = ss_path
                    log.info("Screenshot saved: %s", ss_path)

                    baseline_name = _baseline_name(action)
                    baseline_path = os.path.join(BASELINES_DIR, f"{baseline_name}.png")
                    diff_path = os.path.join(ticket_dir, f"step_{step_num}_diff.png")

                    diff_result = pixel_diff_compare(ss_path, baseline_path, diff_path)
                    step_result["pixel_diff"] = diff_result

                    if use_vision and diff_result.get("changed", True):
                        log.info("Pixel diff: %.1f%% changed, running Vision QA...",
                                 diff_result.get("diff_pct", -1))
                        vision_result = evaluate_screenshot(ss_path, expected, ticket_title)
                        step_result["vision"] = vision_result
                        if vision_result.get("verdict") == "FAIL":
                            step_result["status"] = "FAIL"
                            step_result["error"] = vision_result.get("reason", "Vision QA failed")
                    elif not diff_result.get("changed", True):
                        log.info("No visual change — step PASS (skipping Vision)")

                except Exception as e:
                    log.error("Screenshot/eval error at step %d: %s", step_num, e)
                    step_result["error"] = str(e)

            step_results.append(step_result)

        await browser.close()

    failures = [s for s in step_results if s["status"] == "FAIL"]
    if failures:
        summary_lines = [f"VIS_QA_FAIL: {len(failures)}/{len(step_results)} kroków nie przeszło"]
        for f in failures:
            summary_lines.append(f"  ✗ Krok {f['step']}: {f['action']} — {f.get('error', 'failed')}")
        verdict = "VIS_QA_FAIL"
    else:
        summary_lines = [f"VIS_QA_PASS: {len(step_results)} kroków OK"]
        verdict = "VIS_QA_PASS"

    summary = "\n".join(summary_lines)
    log.info(summary)
    if log_file:
        with open(log_file, "a") as f:
            f.write(f"\n{summary}\n")

    return {"verdict": verdict, "steps": step_results, "summary": summary}


def _baseline_name(action):
    name = re.sub(r'[^a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ0-9]+', '_', action).strip('_').lower()
    return name[:80] if name else "unknown"


def run_visual_tests_sync(ticket_id, use_vision=True, log_file=None):
    """Synchronous wrapper for run_visual_tests."""
    return asyncio.run(run_visual_tests(ticket_id, use_vision, log_file))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m visual_qa.runner <ticket_id> [--no-vision]")
        sys.exit(1)
    tid = sys.argv[1]
    vision = "--no-vision" not in sys.argv
    result = run_visual_tests_sync(tid, use_vision=vision)
    print(json.dumps(result, indent=2, ensure_ascii=False))
