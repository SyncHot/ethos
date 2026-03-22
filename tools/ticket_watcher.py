#!/usr/bin/env python3
"""
EthOS Ticket Watcher — polls copilot queue and auto-processes tickets.

Usage:
    python3 ticket_watcher.py [--interval 15] [--auto]

With --auto: picks highest priority ticket, assigns to copilot, moves to W trakcie,
outputs EXECUTE instruction with model selection based on ticket complexity.

Complexity → Model mapping:
    complex  → gpt-5.1-codex-max (premium, deep reasoning)
    medium   → gemini-3-pro-preview (balanced quality/speed)
    simple   → gpt-5.1-codex-mini (fast, cost-effective)
"""

import sys, time, json, os, argparse, requests, urllib3, subprocess, shlex, signal, threading, hashlib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Codebase map generator — dynamic architecture reference for AI agents
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codebase_map import generate as generate_codebase_map

# Backend imports for local AI execution
sys.path.insert(0, "/opt/ethos/backend")
try:
    from model_library import get_library as _get_ml
    from utils import get_ethos_user
except Exception:
    _get_ml = None
    def get_ethos_user(): return "nasadmin"

BASE = "http://localhost:9000/api"
TOKEN_FILE = "/tmp/.ethos_orchestrator_token"
LOCK_FILE = "/tmp/.ethos_watcher_executing"
DOCS_DIR = "/opt/ethos/docs"
API_FAIL_THRESHOLD = 3  # consecutive API failures before considering outage
API_TIMEOUT = (5, 15)   # (connect, read) seconds

# ── Polling interval tuning ──────────────────────────────────────────────
POLL_IDLE = 30           # seconds between polls when queue empty & nothing running
POLL_ACTIVE = 10         # seconds between polls when executing or queue non-empty

# ── Persistent session with connection pooling & auto-retry ──────────────
_session = requests.Session()
_retry_strategy = Retry(
    total=3,
    backoff_factor=1,            # 0s, 1s, 2s between retries
    status_forcelist=[502, 503, 504],
    allowed_methods=["GET", "POST", "PUT"],
    raise_on_status=False,
)
_adapter = HTTPAdapter(
    max_retries=_retry_strategy,
    pool_connections=2,
    pool_maxsize=4,
)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)
_session.verify = False

# ── Free Model mapping (Copilot CLI with free/low-cost models) ──────────
FREE_MODEL_MAP = {
    "complex": {
        "model": "gpt-4.1",
        "label": "GPT-4.1 (Free)",
        "reason": "Best free model for complex tasks",
    },
    "medium": {
        "model": "gpt-4.1",
        "label": "GPT-4.1 (Free)",
        "reason": "Strong free model for medium tasks",
    },
    "simple": {
        "model": "gpt-5-mini",
        "label": "GPT-5 Mini (Free)",
        "reason": "Fast free model for simple tasks",
    },
}
FREE_MODEL_ROUTING = {
    "complex": ["gpt-4.1", "gpt-5-mini", "gpt-5.1-codex-mini"],
    "medium":  ["gpt-4.1", "gpt-5-mini", "gpt-5.1-codex-mini"],
    "simple":  ["gpt-5-mini", "gpt-4.1", "gpt-5.1-codex-mini"],
}

# ── Complexity → AI Model mapping ────────────────────────────────────────
# Maps to Copilot CLI --model flag values
COMPLEXITY_MODEL_MAP = {
    "complex": {
        "model": "claude-sonnet-4.6",
        "label": "Claude Sonnet 4.6 (Deep Reasoning)",
        "reason": "Best at multi-file architecture, security audits, and cross-cutting changes",
    },
    "medium": {
        "model": "gemini-3-pro-preview",
        "label": "Gemini 3 Pro (Reliable Implementer)",
        "reason": "Strong tool use, reliable for typical 2-3 file feature tickets",
    },
    "simple": {
        "model": "gpt-5.1-codex-mini",
        "label": "Codex Mini (Fast & Efficient)",
        "reason": "Quick single-file edits, CSS tweaks, config changes",
    },
}

# ── Model routing: ordered fallback chains per complexity ─────────────────
MODEL_ROUTING = {
    "complex": ["claude-sonnet-4.6", "gemini-3-pro-preview", "gpt-5.1-codex", "gpt-4.1"],
    "medium":  ["gemini-3-pro-preview", "claude-sonnet-4.6", "gpt-5.1-codex-mini", "gpt-4.1"],
    "simple":  ["gpt-5.1-codex-mini", "gpt-5-mini", "gpt-4.1"],
}

# All available Copilot CLI models, ranked by capability (best first).
# Used as last-resort fallback pool when the routing chain is exhausted.
ALL_AVAILABLE_MODELS = [
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.4",
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "gemini-3-pro-preview",
    "gpt-5.2",
    "gpt-5.1-codex",
    "gpt-5.1",
    "claude-opus-4.5",
    "claude-sonnet-4",
    "claude-haiku-4.5",
    "gpt-5.4-mini",
    "gpt-5.1-codex-mini",
    "gpt-5-mini",
    "gpt-4.1",
]

RATE_LIMIT_MARKERS = [
    "rate limit",
    "rate-limit",
    "too many requests",
    "429",
    "quota exceeded",
    "usage limit",
    "try again later",
]

TRANSIENT_ERROR_MARKERS = [
    "transient api error",
    "transient error",
    "request failed due to a transient",
    "no active shell sessions",
    "invalid shell id",
]

SERVER_ERROR_MARKERS = [
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "server error",
    "internal error",
    "overloaded",
]

QUOTA_EXHAUSTED_MARKERS = [
    "you have no quota",
    "402 you have no quota",
]

MAX_RETRIES_SAME_MODEL = 2      # retry same model N times with backoff before switching
MAX_BACKOFF_SECS = 180          # cap exponential backoff at 3 min
BACKOFF_BASE_SECS = 15          # starting backoff for rate limits (15→30→60→120→180)
INTER_TICKET_DELAY = 5          # base gap between tickets (only cosmetic, real protection is per-model cooldown)
MODEL_COOLDOWN_SECS = 120      # don't retry a model that got 429 for 2 min (providers reset in ~60s)
LIVE_LOG_CHECK_INTERVAL = 10   # seconds between checking running process log for rate limits
QA_RATE_LIMIT_COOLDOWN = 30    # extra pause only if QA would use same provider that just got 429
TRANSIENT_ERROR_KILL_THRESHOLD = 5  # kill process after N transient API errors (they waste autopilot turns)
TRANSIENT_SOFT_COOLDOWN_THRESHOLD = 3  # after exit 0, if this many transient errors → soft cooldown on model

# ── Global attempt cap — prevents infinite retry loops on stuck tickets ───
MAX_TOTAL_ATTEMPTS = 10  # max total dev runs per ticket before auto-shelving
MAX_STRANDED_REQUEUES = 3  # max consecutive stranded detections before auto-shelving
_ticket_attempt_counts = {}  # {ticket_id: total_attempts_across_all_cycles}
_stranded_counts = {}  # {ticket_id: consecutive stranded detections}

def _count_existing_attempts(tid):
    """Count how many dev run logs exist on disk for a ticket (persists across restarts)."""
    count = 0
    for log_dir in ("/opt/ethos/logs/copilot_tickets", "/opt/ethos/logs/localai_tickets",
                     "/opt/ethos/logs/copilot_tickets/archive", "/opt/ethos/logs/localai_tickets/archive"):
        if os.path.isdir(log_dir):
            for fname in os.listdir(log_dir):
                # Match dev logs like t_xxx_1234567890.log but NOT qa/prompt files
                if fname.startswith(tid + "_") and fname.endswith(".log") \
                        and "_qa_" not in fname and "_prompt" not in fname:
                    count += 1
    return count

def _record_attempt(tid):
    """Record a dev attempt for a ticket. Returns (allowed, count).
    Combines in-memory session count with on-disk history for restart persistence."""
    _ticket_attempt_counts[tid] = _ticket_attempt_counts.get(tid, 0) + 1
    # On first call after restart, seed from disk to survive restarts
    if _ticket_attempt_counts[tid] == 1:
        disk_count = _count_existing_attempts(tid)
        # disk_count includes the log about to be created, so use it directly
        if disk_count > 1:
            _ticket_attempt_counts[tid] = disk_count
    count = _ticket_attempt_counts[tid]
    return count <= MAX_TOTAL_ATTEMPTS, count

def _is_ticket_shelved(tid):
    """Check if ticket has exhausted total attempts."""
    mem_count = _ticket_attempt_counts.get(tid, 0)
    if mem_count >= MAX_TOTAL_ATTEMPTS:
        return True
    # Also check disk for restart persistence
    if mem_count == 0:
        return _count_existing_attempts(tid) >= MAX_TOTAL_ATTEMPTS
    return False

# ── Prompt deduplication — skip rewriting identical prompts ──────────────
_last_prompt_hashes = {}  # {ticket_id: (hash, prompt_file_path)}

def _dedup_prompt(tid, prompt, log_dir):
    """Return prompt_file path, reusing previous file if prompt hash matches."""
    h = hashlib.md5(prompt.encode()).hexdigest()[:12]
    prev = _last_prompt_hashes.get(tid)
    if prev and prev[0] == h and os.path.exists(prev[1]):
        return prev[1], True  # reuse
    prompt_file = os.path.join(log_dir, f"{tid}_{int(time.time())}_prompt.txt")
    with open(prompt_file, "w") as pf:
        pf.write(prompt)
    _last_prompt_hashes[tid] = (h, prompt_file)
    return prompt_file, False

# ── Global model cooldown tracker ─────────────────────────────────────────
# {model_name: timestamp_when_cooldown_expires}
_model_cooldowns = {}
_timeout_counts = {}  # {ticket_id: number_of_timeouts}

# ── Provider grouping: rate limits are usually per-provider ──────────────
# When one model from a provider gets 429'd, sibling models likely share the quota.
MODEL_PROVIDER = {}
_PROVIDERS = {
    "anthropic": [m for m in ALL_AVAILABLE_MODELS if m.startswith("claude")],
    "openai":    [m for m in ALL_AVAILABLE_MODELS if m.startswith("gpt")],
    "google":    [m for m in ALL_AVAILABLE_MODELS if m.startswith("gemini")],
}
for _prov, _models in _PROVIDERS.items():
    for _m in _models:
        MODEL_PROVIDER[_m] = _prov

def _is_model_cooled_down(model_name):
    """Check if a model is in rate-limit cooldown."""
    expires = _model_cooldowns.get(model_name, 0)
    return time.time() < expires

def _set_model_cooldown(model_name, duration=None):
    """Put a model in cooldown after a rate limit hit.
    Also applies a shorter cooldown to sibling models from the same provider
    (rate limits are typically per-provider, not per-model)."""
    if duration is None:
        duration = MODEL_COOLDOWN_SECS
    expires = time.time() + duration
    _model_cooldowns[model_name] = expires
    print(f"MODEL_COOLDOWN | {model_name} | cooled down for {duration}s (until {time.strftime('%H:%M:%S', time.localtime(expires))})", flush=True)

    # Cascade: apply 60% cooldown to sibling models from the same provider
    provider = MODEL_PROVIDER.get(model_name)
    if provider:
        sibling_duration = int(duration * 0.6)
        sibling_expires = time.time() + sibling_duration
        for sibling in _PROVIDERS.get(provider, []):
            if sibling != model_name:
                # Only extend, never shorten an existing cooldown
                if _model_cooldowns.get(sibling, 0) < sibling_expires:
                    _model_cooldowns[sibling] = sibling_expires
                    print(f"  SIBLING_COOLDOWN | {sibling} | {sibling_duration}s (provider={provider})", flush=True)

def _get_available_model(complexity, tried_models=None):
    """Get best available model for complexity, skipping cooled-down ones."""
    tried = set(tried_models or [])
    chain = MODEL_ROUTING.get(complexity, MODEL_ROUTING.get("medium", []))
    # First try routing chain
    for m in chain:
        if m not in tried and not _is_model_cooled_down(m):
            return {"model": m, "label": f"{m} (routing)", "reason": "Primary routing choice"}
    # Then all available models
    for m in ALL_AVAILABLE_MODELS:
        if m not in tried and not _is_model_cooled_down(m):
            return {"model": m, "label": f"{m} (global fallback)", "reason": "All routing models unavailable"}
    # Everything is cooled down — pick the one with soonest expiry
    soonest_model = None
    soonest_time = float('inf')
    for m in chain + ALL_AVAILABLE_MODELS:
        if m not in tried:
            exp = _model_cooldowns.get(m, 0)
            if exp < soonest_time:
                soonest_time = exp
                soonest_model = m
    if soonest_model:
        return {"model": soonest_model, "label": f"{soonest_model} (soonest available)", "reason": f"All models in cooldown, soonest expires at {time.strftime('%H:%M:%S', time.localtime(soonest_time))}"}
    return None

# ── Inter-ticket pacing ──────────────────────────────────────────────────
_last_ticket_finished = 0       # timestamp of last ticket completion
_last_rate_limit_hit = 0        # timestamp of last rate limit encounter

# ── Interruptible sleep (checked by signal handler) ──────────────────────
_shutdown_event = None  # will be set to threading.Event() in main()

def _interruptible_sleep(seconds):
    """Sleep in 1-second increments so signals and shutdown can interrupt."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _shutdown_event and _shutdown_event.is_set():
            break
        time.sleep(min(1.0, deadline - time.time()))

# ── Execution metrics ────────────────────────────────────────────────────
# Tracks per-model success/failure/rate-limit counts and total execution time.
# Used to inform routing decisions and printed periodically.
_model_metrics = {}  # {model: {"ok": int, "fail": int, "rate_limit": int, "transient": int, "total_secs": float}}

def _record_metric(model, outcome, duration_secs=0):
    """Record execution outcome for a model. outcome: 'ok', 'fail', 'rate_limit', 'server_error', 'transient'."""
    if model not in _model_metrics:
        _model_metrics[model] = {"ok": 0, "fail": 0, "rate_limit": 0, "server_error": 0, "transient": 0, "total_secs": 0.0}
    m = _model_metrics[model]
    if outcome in m:
        m[outcome] += 1
    m["total_secs"] += duration_secs

def _print_metrics_summary():
    """Print a one-line summary of model metrics."""
    if not _model_metrics:
        return
    parts = []
    for model, m in sorted(_model_metrics.items()):
        total = m["ok"] + m["fail"] + m["rate_limit"] + m["server_error"] + m["transient"]
        if total == 0:
            continue
        rate = f"{m['ok']/total*100:.0f}%" if total > 0 else "—"
        parts.append(f"{model}: {rate} ok ({m['ok']}/{total}, {m['rate_limit']}×429, {m['total_secs']/60:.0f}min)")
    if parts:
        print(f"METRICS | {' | '.join(parts)}", flush=True)

def _should_wait_before_next_ticket(next_model=None):
    """Calculate how long to wait before starting the next ticket.
    Skips QA cooldown if the next model is from a different provider."""
    now = time.time()
    # Minimal base gap (always)
    since_last = now - _last_ticket_finished
    if since_last < INTER_TICKET_DELAY:
        return INTER_TICKET_DELAY - since_last
    # Extra cooldown only if the next model itself is still in cooldown
    if next_model and _is_model_cooled_down(next_model):
        remaining = _model_cooldowns.get(next_model, 0) - now
        return max(remaining, 0)
    return 0

# ── Agent type → lessons & skills ────────────────────────────────────────
AGENT_MAP = {
    "FE/UX":   {"lessons": "FE_LESSONS_LEARNED.md",     "skills": "frontend, CSS, JS, UX",
                 "docs": ["UX_LESSONS_LEARNED.md", "UX_UI_DESIGN_SYSTEM.md", "DEV_STANDARDS.md"]},
    "Backend": {"lessons": "BE_LESSONS_LEARNED.md",      "skills": "Python, Flask, API",
                 "docs": ["BE_LESSONS_LEARNED.md", "ARCH_CORE_SYSTEM.md", "DEV_STANDARDS.md"]},
    "DevOps":  {"lessons": "DEVOPS_LESSONS_LEARNED.md",  "skills": "systemd, Docker, deployment",
                 "docs": ["DEVOPS_LESSONS_LEARNED.md", "QA_FAILOVER_PROTOCOLS.md", "DEV_STANDARDS.md"]},
    "Security":{"lessons": "BE_LESSONS_LEARNED.md",      "skills": "security, auth, encryption",
                 "docs": ["ETHOS_MANIFESTO.md", "QA_FAILOVER_PROTOCOLS.md", "DEV_STANDARDS.md"]},
    "Docs":    {"lessons": "DEVOPS_LESSONS_LEARNED.md",  "skills": "documentation",
                 "docs": ["APP_DEVELOPMENT_GUIDE.md", "DEV_STANDARDS.md"]},
    "QA":      {"lessons": "FE_LESSONS_LEARNED.md",      "skills": "testing, QA",
                 "docs": ["QA_FAILOVER_PROTOCOLS.md", "DEV_STANDARDS.md"]},
    "General": {"lessons": "FE_LESSONS_LEARNED.md",      "skills": "general development",
                 "docs": ["DEV_STANDARDS.md", "APP_DEVELOPMENT_GUIDE.md"]},
}

# ── Auth & API helpers ───────────────────────────────────────────────────

def login():
    username = os.environ.get("ETHOS_COPILOT_USER")
    password = os.environ.get("ETHOS_COPILOT_PASS")
    if not username or not password:
        raise RuntimeError("Missing ETHOS_COPILOT_USER or ETHOS_COPILOT_PASS env vars")
    r = _session.post(f"{BASE}/auth/login",
                      json={"username": username, "password": password},
                      timeout=API_TIMEOUT)
    r.raise_for_status()
    token = r.json().get("token", "")
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    _session.headers.update({"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"})
    return token

def get_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                _session.headers.update({"Authorization": f"Bearer {t}",
                                         "Content-Type": "application/json"})
                return t
    return login()

_last_auth_refresh = 0
_AUTH_REFRESH_COOLDOWN = 300  # seconds between empty-queue auth retries

def _ensure_auth():
    """Make sure session headers have a valid token."""
    if "Authorization" not in _session.headers:
        get_token()

def api(method, path, body=None):
    global _last_auth_refresh
    _ensure_auth()
    fn = _session.get if method == "GET" else (_session.post if method == "POST" else _session.put)
    kw = {"timeout": API_TIMEOUT}
    if body: kw["json"] = body
    r = fn(f"{BASE}{path}", **kw)
    if r.status_code == 401:
        login()
        r = fn(f"{BASE}{path}", **kw)
    r.raise_for_status()
    data = r.json()
    # Detect silent auth failure: retry only if cooldown has elapsed
    if (path == "/tickets/copilot/queue" and data.get("total", -1) == 0
            and time.time() - _last_auth_refresh > _AUTH_REFRESH_COOLDOWN):
        _last_auth_refresh = time.time()
        login()
        r = fn(f"{BASE}{path}", **kw)
        r.raise_for_status()
        retry_data = r.json()
        if retry_data.get("total", 0) > 0:
            print("AUTH_REFRESH | Stale token detected (empty queue), re-logged in", flush=True)
            return retry_data
    return data

def poll_queue():
    return api("GET", "/tickets/copilot/queue")

def assign_ticket(tid, user="copilot"):
    api("PUT", f"/tickets/tickets/{tid}", {"assignee": user})

def move_ticket(tid, col):
    api("PUT", f"/tickets/tickets/{tid}/move", {"column": col, "order": 0})

def add_comment(tid, text):
    api("POST", f"/tickets/tickets/{tid}/comments", {"text": text})

# ── Execution lock ───────────────────────────────────────────────────────

def is_executing():
    return os.path.exists(LOCK_FILE)

def set_executing(tid, model_info=None, retry_count=0, tried_models=None, qa_cycle=0, agent="copilot", pid=None, log_file=None):
    with open(LOCK_FILE, "w") as f:
        payload = {
            "ticket_id": tid,
            "started": time.time(),
            "model": model_info,
            "retry_count": retry_count,
            "tried_models": tried_models or [],
            "qa_cycle": qa_cycle,
            "agent": agent,
        }
        if pid:
            payload["copilot_pid"] = pid
        if log_file:
            payload["log_file"] = log_file
        json.dump(payload, f)

def clear_executing():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

def get_executing():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return None
    return None

def pid_alive(pid):
    """Check if a process with given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False

def cleanup_stale_lock():
    """On startup, clean lock file if the PID inside is dead."""
    executing = get_executing()
    if not executing:
        return
    pid = executing.get("copilot_pid")
    tid = executing.get("ticket_id", "?")
    if pid and pid_alive(pid):
        print(f"STALE_CHECK | {tid} | PID {pid} still alive — killing orphan", flush=True)
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    print(f"STALE_LOCK_CLEARED | {tid} | PID {pid}", flush=True)
    clear_executing()


def resume_in_progress_tickets():
    """On startup (auto mode), move 'W trakcie' tickets back to 'Do zrobienia' so they get re-queued."""
    try:
        data = poll_queue()
        queue = data.get("queue", [])
        in_progress = [t for t in queue if t["column"] == "W trakcie"]
        if not in_progress:
            return
        print(f"RESUME | Found {len(in_progress)} ticket(s) stuck in 'W trakcie' — re-queuing", flush=True)
        for ticket in in_progress:
            tid = ticket["id"]
            try:
                move_ticket(tid, "Do zrobienia")
                add_comment(tid, "[system] Watcher zrestartowany — ticket wraca do kolejki do ponownego wykonania.")
                print(f"RESUME | {tid} | '{ticket['title']}' moved back to 'Do zrobienia'", flush=True)
            except Exception as e:
                print(f"RESUME_ERROR | {tid} | {e}", flush=True)
    except Exception as e:
        print(f"RESUME_POLL_ERROR | {e}", flush=True)


def _log_indicates_rate_limit(log_file):
    """Best-effort detection of provider/API rate limit (429) from Copilot log output."""
    return _log_contains_markers(log_file, RATE_LIMIT_MARKERS)


def _log_indicates_server_error(log_file):
    """Best-effort detection of server errors (5xx) from Copilot log output."""
    return _log_contains_markers(log_file, SERVER_ERROR_MARKERS)


def _log_contains_markers(log_file, markers):
    """Check if log file tail contains any of the given marker strings."""
    if not log_file or log_file == "?" or not os.path.isfile(log_file):
        return False
    try:
        size = os.path.getsize(log_file)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            if size > 20000:
                f.seek(max(0, size - 20000))
            content = f.read().lower()
        return any(marker in content for marker in markers)
    except Exception:
        return False


def _log_indicates_transient_error(log_file):
    """Best-effort detection of transient API errors from Copilot log output."""
    return _log_contains_markers(log_file, TRANSIENT_ERROR_MARKERS)


def _count_transient_errors(log_file):
    """Count how many transient API error lines appear in the log."""
    if not log_file or log_file == "?" or not os.path.isfile(log_file):
        return 0
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read().lower()
        return sum(content.count(marker) for marker in TRANSIENT_ERROR_MARKERS)
    except Exception:
        return 0


def _log_indicates_quota_exhausted(log_file):
    """Detect 402 quota exhausted errors from Copilot log output."""
    return _log_contains_markers(log_file, QUOTA_EXHAUSTED_MARKERS)


def _classify_failure(log_file):
    """Classify failure type from log output. Returns 'quota_exhausted', 'rate_limit', 'server_error', 'transient', or 'unknown'."""
    if _log_indicates_quota_exhausted(log_file):
        return "quota_exhausted"
    if _log_indicates_rate_limit(log_file):
        return "rate_limit"
    if _log_indicates_server_error(log_file):
        return "server_error"
    if _log_indicates_transient_error(log_file):
        return "transient"
    return "unknown"


def _should_fallback_from_sonnet(model_info, log_file):
    """Check if the current model failed due to rate limit, server error, or transient errors."""
    if not isinstance(model_info, dict):
        return False
    failure = _classify_failure(log_file)
    return failure in ("rate_limit", "server_error", "transient")

# ── Post-commit health gate — detect server crashes before QA ─────────────
HEALTH_CHECK_URL = "http://localhost:9000/api/auth/verify"
HEALTH_CHECK_TIMEOUT = 5        # seconds per HTTP check
HEALTH_CHECK_RETRIES = 3        # attempts before declaring dead
HEALTH_CHECK_RETRY_DELAY = 3    # seconds between health retries
LIVE_HEALTH_CHECK_INTERVAL = 30 # seconds between live health checks during execution
_last_live_health_check = 0
_live_health_failures = 0

def _check_ethos_health():
    """Check if ethos service is running and API responds.
    Returns (healthy: bool, detail: str)."""
    # 1. Check systemd service
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", "ethos"],
            timeout=5, capture_output=True)
        if r.returncode != 0:
            return False, "ethos service not active"
    except Exception as e:
        return False, f"systemctl check failed: {e}"

    # 2. Check HTTP health
    for attempt in range(HEALTH_CHECK_RETRIES):
        try:
            resp = requests.get(HEALTH_CHECK_URL, timeout=HEALTH_CHECK_TIMEOUT)
            if resp.status_code in (200, 401):
                return True, f"API responding (HTTP {resp.status_code})"
        except Exception:
            pass
        if attempt < HEALTH_CHECK_RETRIES - 1:
            time.sleep(HEALTH_CHECK_RETRY_DELAY)

    return False, "API not responding after retries"


def _get_last_agent_commit(ticket_id):
    """Find the most recent commit made by the copilot agent for this ticket.
    Returns (sha, message) or (None, None)."""
    try:
        r = subprocess.run(
            ["git", "--no-pager", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=10, cwd="/opt/ethos")
        if r.returncode != 0:
            return None, None
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            sha = line.split()[0]
            msg = line[len(sha):].strip()
            # Agent commits typically contain ticket ID or Co-authored-by Copilot
            if ticket_id in msg:
                return sha, msg
            # Also check full commit for co-author trailer
            full = subprocess.run(
                ["git", "--no-pager", "show", "--quiet", sha],
                capture_output=True, text=True, timeout=10, cwd="/opt/ethos")
            if "Co-authored-by: Copilot" in full.stdout:
                return sha, msg
        return None, None
    except Exception:
        return None, None


def _auto_revert_and_recover(ticket_id):
    """Revert last agent commit and restart ethos.
    Returns (success: bool, detail: str)."""
    sha, msg = _get_last_agent_commit(ticket_id)
    if not sha:
        return False, "could not identify agent commit to revert"

    print(f"AUTO_REVERT | {ticket_id} | reverting {sha}: {msg}", flush=True)

    try:
        # Revert the commit
        r = subprocess.run(
            ["git", "revert", "--no-edit", sha],
            capture_output=True, text=True, timeout=30, cwd="/opt/ethos")
        if r.returncode != 0:
            # Try harder: reset if revert has conflicts
            subprocess.run(["git", "revert", "--abort"],
                capture_output=True, timeout=10, cwd="/opt/ethos")
            subprocess.run(["git", "reset", "--hard", f"{sha}~1"],
                capture_output=True, timeout=10, cwd="/opt/ethos")
            print(f"AUTO_REVERT | {ticket_id} | revert had conflicts, used hard reset to {sha}~1", flush=True)
    except Exception as e:
        return False, f"git revert failed: {e}"

    # Restart ethos
    try:
        subprocess.run(["sudo", "systemctl", "restart", "ethos"],
            capture_output=True, timeout=30)
        time.sleep(5)
    except Exception as e:
        return False, f"restart failed after revert: {e}"

    # Verify recovery
    healthy, detail = _check_ethos_health()
    if healthy:
        # Push the revert
        try:
            subprocess.run(["git", "push"],
                capture_output=True, timeout=30, cwd="/opt/ethos")
        except Exception:
            pass
        return True, f"reverted {sha}, server recovered"
    else:
        return False, f"reverted {sha} but server still unhealthy: {detail}"




# ── Live log monitoring — detect rate limits during execution ─────────────
_last_live_check = 0
_last_log_size = 0
_live_transient_count = 0  # accumulates transient errors across checks

def check_running_process_for_rate_limit(proc, log_file):
    """Check log of a running process for rate limit / transient error markers.
    Returns 'rate_limit', 'server_error', 'transient', or None if clean."""
    global _last_live_check, _last_log_size, _live_transient_count
    now = time.time()
    if now - _last_live_check < LIVE_LOG_CHECK_INTERVAL:
        return None
    _last_live_check = now

    if not log_file or not os.path.isfile(log_file):
        return None
    try:
        size = os.path.getsize(log_file)
        if size <= _last_log_size:
            return None  # no new content
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            # Only read the new part since last check
            f.seek(max(0, _last_log_size))
            new_content = f.read().lower()
        _last_log_size = size

        if any(marker in new_content for marker in RATE_LIMIT_MARKERS):
            return "rate_limit"
        if any(marker in new_content for marker in SERVER_ERROR_MARKERS):
            return "server_error"

        # Count transient errors in new content — they accumulate
        new_transient = sum(new_content.count(marker) for marker in TRANSIENT_ERROR_MARKERS)
        if new_transient > 0:
            _live_transient_count += new_transient
            print(f"TRANSIENT_ERRORS | {new_transient} new ({_live_transient_count} total, kill at {TRANSIENT_ERROR_KILL_THRESHOLD})", flush=True)
            if _live_transient_count >= TRANSIENT_ERROR_KILL_THRESHOLD:
                return "transient"
    except Exception:
        pass
    return None


def _get_next_fallback(model_info, complexity, tried_models=None):
    """Return the next model to try, respecting cooldowns. First walks the routing chain, then ALL_AVAILABLE_MODELS."""
    if not isinstance(model_info, dict):
        return None
    current = model_info.get("model", "")
    tried = set(tried_models or [])
    tried.add(current)

    # For freemodel agent, stay within free model pool
    executing = get_executing() or {}
    if executing.get("agent") == "freemodel":
        chain = FREE_MODEL_ROUTING.get(complexity, FREE_MODEL_ROUTING.get("medium", []))
        for m in chain:
            if m not in tried and not _is_model_cooled_down(m):
                return {
                    "model": m,
                    "label": f"{m} (free fallback)",
                    "reason": f"Free model failover from {current}",
                }
        return None

    # 1) Try the routing chain for this complexity (skip cooled-down)
    chain = MODEL_ROUTING.get(complexity, MODEL_ROUTING.get("medium", []))
    try:
        idx = chain.index(current)
    except ValueError:
        idx = -1
    for i in range(idx + 1, len(chain)):
        if chain[i] not in tried and not _is_model_cooled_down(chain[i]):
            return {
                "model": chain[i],
                "label": f"{chain[i]} (routing fallback)",
                "reason": f"Failover from {current}",
            }

    # 2) Routing chain exhausted — scan all available models (skip cooled-down)
    for m in ALL_AVAILABLE_MODELS:
        if m not in tried and not _is_model_cooled_down(m):
            return {
                "model": m,
                "label": f"{m} (global fallback)",
                "reason": f"All routing models exhausted, best available fallback from {current}",
            }

    # 3) All available are cooled down — pick the one with soonest expiry
    for m in chain + ALL_AVAILABLE_MODELS:
        if m not in tried:
            return {
                "model": m,
                "label": f"{m} (waiting for cooldown)",
                "reason": f"All models cooled down, soonest available",
            }

    return None

# ── Agent detection ──────────────────────────────────────────────────────

def detect_agent(title, labels):
    t = title.lower()
    l = [x.lower() for x in labels]
    # Title-based detection
    if any(x in t for x in ['[fe]','frontend','ui','ux','css','design','gallery','thumbnail']): return "FE/UX"
    if any(x in t for x in ['[be]','backend','api','endpoint','flask','route']): return "Backend"
    if any(x in t for x in ['[devops]','deploy','build','docker','systemd','service','ops']): return "DevOps"
    if any(x in t for x in ['[sec]','security','auth','permission','encrypt']): return "Security"
    if any(x in t for x in ['[docs]','dokumentacja','documentation']): return "Docs"
    if any(x in t for x in ['[qa]','test','qa']): return "QA"
    # Label-based detection
    if any(x in l for x in ['frontend','fe','ui','file-manager','gallery']): return "FE/UX"
    if any(x in l for x in ['backend','be','api']): return "Backend"
    if any(x in l for x in ['devops','ops','infra']): return "DevOps"
    if any(x in l for x in ['security','sec']): return "Security"
    # Description keywords (title has abbreviated names like "FM:")
    if any(x in t for x in ['fm:','file manager','file operations','listing','upload','download']): return "FE/UX"
    if any(x in t for x in ['cache','index','optim','perf','scalab']): return "Backend"
    return "General"

# ── Model selection based on complexity ──────────────────────────────────

def _maybe_upgrade_complexity(ticket):
    """Auto-upgrade complexity based on description/title heuristics.
    Catches misclassified 'simple' tickets that are actually harder."""
    current = ticket.get("complexity", "medium")
    title = (ticket.get("title", "") + " " + ticket.get("description", "")).lower()
    desc = ticket.get("description", "")

    # Count hints of multi-file/complex work
    complex_keywords = ["refactor", "migration", "security audit", "architektur",
                        "restructur", "rewrite", "multi-file", "cross-cutting"]
    medium_keywords = ["endpoint", "blueprint", "component", "feature",
                       "integration", "socket", "websocket", "middleware"]

    if current == "simple":
        # Upgrade simple→medium if description is substantial or has medium keywords
        word_count = len(desc.split()) if desc else 0
        if word_count > 80:
            return "medium"
        if any(kw in title for kw in medium_keywords + complex_keywords):
            return "medium"
    if current in ("simple", "medium"):
        # Upgrade to complex if strong complexity signals
        if any(kw in title for kw in complex_keywords):
            return "complex"
    return current


def select_model(ticket, tried_models=None):
    """Select AI model based on ticket complexity, respecting cooldowns.
    Auto-upgrades complexity if heuristics detect misclassification."""
    complexity = _maybe_upgrade_complexity(ticket)
    orig = ticket.get("complexity", "medium")
    if complexity != orig:
        print(f"COMPLEXITY_UPGRADE | {ticket.get('id','?')} | {orig} → {complexity}", flush=True)
        ticket["complexity"] = complexity  # persist for downstream (autopilot turns, timeouts)
    if complexity not in COMPLEXITY_MODEL_MAP:
        complexity = "medium"
    # Use cooldown-aware selection instead of static mapping
    available = _get_available_model(complexity, tried_models)
    if available:
        return available
    # Fallback to static mapping if everything is somehow unavailable
    return COMPLEXITY_MODEL_MAP[complexity]

# ── Load relevant docs for agent ─────────────────────────────────────────

_docs_cache = {}  # {doc_name: content}
_docs_cache_time = 0
_DOCS_CACHE_TTL = 600  # refresh docs from disk every 10 minutes

# Codebase map cache — regenerated every 10 minutes or when stale
_codebase_map_cache = {}  # {agent_or_None: map_string}
_codebase_map_time = 0

def get_codebase_map(agent=None):
    """Get the codebase map (optionally filtered by agent), regenerating if stale."""
    global _codebase_map_cache, _codebase_map_time
    now = time.time()
    if now - _codebase_map_time > _DOCS_CACHE_TTL:
        _codebase_map_cache = {}
        _codebase_map_time = now
    key = agent or "_full"
    if key not in _codebase_map_cache:
        try:
            _codebase_map_cache[key] = generate_codebase_map(agent=agent)
        except Exception as e:
            print(f"CODEBASE_MAP_ERROR | {e}", flush=True)
            _codebase_map_cache[key] = ""
    return _codebase_map_cache[key]

def _summarize_doc(content, max_lines=40):
    """Extract key bullet points from a doc — headings + first sentence of each section."""
    lines = content.splitlines()
    summary = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Keep headings
        if stripped.startswith("#"):
            summary.append(stripped)
        # Keep bullet points and numbered items
        elif stripped.startswith(("- ", "* ", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
            summary.append(stripped[:150])
        # Keep lines right after headings (first paragraph sentence)
        elif i > 0 and lines[i-1].strip().startswith("#") and stripped:
            summary.append(stripped[:150])
        if len(summary) >= max_lines:
            break
    return "\n".join(summary)


def load_docs_context(agent):
    """Load summarized docs for the agent type. Full docs are referenced by path for the agent to read if needed."""
    global _docs_cache, _docs_cache_time
    info = AGENT_MAP.get(agent, AGENT_MAP["General"])
    doc_files = info.get("docs", [])

    # Invalidate cache if stale
    now = time.time()
    if now - _docs_cache_time > _DOCS_CACHE_TTL:
        _docs_cache = {}
        _docs_cache_time = now

    context_parts = []
    for doc_name in doc_files:
        if doc_name in _docs_cache:
            context_parts.append(_docs_cache[doc_name])
            continue
        doc_path = os.path.join(DOCS_DIR, doc_name)
        if os.path.exists(doc_path):
            try:
                with open(doc_path, "r", encoding="utf-8") as f:
                    content = f.read()
                line_count = content.count("\n")
                # Short docs (<80 lines): include full content
                if line_count < 80:
                    entry = f"--- {doc_name} ---\n{content}"
                else:
                    # Long docs: include summary + tell agent where to find full version
                    summary = _summarize_doc(content)
                    entry = (f"--- {doc_name} (summary, {line_count} lines) ---\n"
                             f"{summary}\n\n"
                             f"[Full doc: /opt/ethos/docs/{doc_name} — read with cat if you need details]")
                _docs_cache[doc_name] = entry
                context_parts.append(entry)
            except Exception:
                pass

    return "\n\n".join(context_parts)

# ── Execute ticket via Copilot CLI ────────────────────────────────────────

COPILOT_BIN = os.environ.get("COPILOT_BIN_PATH", f"/home/{get_ethos_user()}/.local/bin/copilot")
COPILOT_LOG_DIR = "/opt/ethos/logs/copilot_tickets"
LOCALAI_LOG_DIR = "/opt/ethos/logs/localai_tickets"
MAX_AUTOPILOT = {
    "complex": 25,
    "medium": 15,
    "simple": 8,
}
MAX_EXECUTION_SECS = {
    "complex": 2400,   # 40 min
    "medium": 1200,    # 20 min
    "simple": 900,     # 15 min
}
MAX_TIMEOUT_RETRIES = 2  # max times a ticket can timeout before being shelved
MAX_QA_CYCLES = 5  # max dev→QA round-trips before giving up

# Per-complexity QA settings — simple tickets get static-only, complex get full review
QA_DEPTH = {
    "complex": {"autopilot": 8, "static_only": False, "max_cycles": 3},
    "medium":  {"autopilot": 6, "static_only": False, "max_cycles": 3},
    "simple":  {"autopilot": 0, "static_only": True,  "max_cycles": 2},
}

def _pre_qa_static_check(tid):
    """Run fast local checks before expensive model QA.
    Returns (passed: bool, errors: list[str]).
    Scoped to committed changes only (HEAD~1..HEAD) to avoid false positives
    from uncommitted work left by other agents.
    """
    errors = []
    try:
        # Use HEAD~1..HEAD to check ONLY the last commit, not dirty working tree
        changed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
        )
        for f in changed.stdout.strip().splitlines():
            fpath = os.path.join("/opt/ethos", f)
            if not os.path.isfile(fpath):
                continue
            if f.endswith(".py"):
                try:
                    with open(fpath, "r", encoding="utf-8") as pf:
                        source = pf.read()
                    compile(source, fpath, "exec")
                except SyntaxError as se:
                    errors.append(f"Syntax error in {f}: {se}")
            elif f.endswith(".js"):
                r = subprocess.run(
                    ["node", "-c", fpath],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode != 0:
                    errors.append(f"JS error in {f}: {r.stderr.strip()[:200]}")
        # Whitespace errors / conflict markers — committed changes only
        r = subprocess.run(
            ["git", "diff", "--check", "HEAD~1", "HEAD"],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
        )
        if r.returncode != 0:
            errors.append(f"git diff --check: {r.stdout.strip()[:300]}")
    except Exception as e:
        # Static checks failed to run — don't block QA
        print(f"PRE_QA_STATIC_ERROR | {tid} | {e}", flush=True)
        return True, []
    return len(errors) == 0, errors


def _pre_fetch_relevant_files(ticket):
    """Use ripgrep to find files relevant to the ticket, saving agent exploration turns."""
    title = ticket.get("title", "")
    desc = ticket.get("description", "")
    # Extract meaningful keywords (skip short/common words)
    words = set()
    for text in (title, desc):
        for w in text.split():
            w = w.strip("[](){}:,.;!?\"'").lower()
            if len(w) >= 4 and w not in ("this", "that", "with", "from", "should", "would",
                                          "could", "ticket", "ethos", "need", "when", "have",
                                          "make", "will", "been", "then", "also", "into"):
                words.add(w)
    if not words:
        return ""
    # Limit to 6 keywords to keep rg fast
    keywords = list(words)[:6]
    pattern = "|".join(keywords)
    try:
        r = subprocess.run(
            ["rg", "-l", "-i", "--max-count=1", "--max-depth=4",
             "--glob=*.py", "--glob=*.js", "--glob=*.html",
             pattern],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            files = r.stdout.strip().splitlines()[:15]  # cap at 15 files
            return "\nLIKELY RELEVANT FILES (from keyword search — check these first):\n" + \
                "\n".join(f"  - {f}" for f in files) + "\n"
    except Exception:
        pass
    return ""


def build_qa_prompt(ticket):
    """Build a QA review prompt — includes the actual diff so QA knows exactly what to review."""
    tid = ticket["id"]
    title = ticket["title"]
    desc = ticket.get("description", "")
    lc = ticket.get("last_comment")

    # Get the actual changes for this ticket so QA can review them directly
    diff_context = _get_ticket_diff_context(tid)

    prompt = f"""You are an EthOS QA Expert agent. Your job is to verify that the ticket was implemented correctly.

TICKET: {tid}
Title: {title}
{f'Description: {desc}' if desc else ''}
{f'Latest comment: {lc["text"][:500]}' if lc else ''}
"""

    if diff_context:
        prompt += f"""
📁 CHANGES TO REVIEW:
{diff_context}
"""

    prompt += f"""
YOUR TASK:
1. Read the ticket requirements (title + description above)
2. Review the CHANGES TO REVIEW diff above — these are the actual code changes
3. Read the relevant EthOS docs to understand standards:
   - /opt/ethos/docs/DEV_STANDARDS.md
   - /opt/ethos/docs/QA_FAILOVER_PROTOCOLS.md
4. Verify the changes meet the ticket requirements
5. Test the functionality if possible (curl API endpoints, check if server responds, etc.)
6. Check if the code follows EthOS coding standards from the docs

VERDICT — you MUST output exactly one of these lines at the END of your response:
  QA_PASS: <brief reason why it passes>
  QA_FAIL: <specific issues found that need fixing — include file names and line numbers>

When reporting QA_FAIL:
- List EACH issue with the specific file path and what's wrong
- Be concrete: "in backend/app.py line 123, the error handler is missing X" not "error handling needs improvement"
- Only report real bugs or requirement gaps, not style preferences
Focus on: correctness, requirements met, docs compliance, no regressions."""

    return prompt


# ── QA model selection with fallback (free/low-cost models) ──────────────
QA_MODEL_CHAIN = ["claude-sonnet-4.6", "gpt-5.1-codex-mini", "gpt-5-mini", "gpt-4.1"]

def _select_qa_model():
    """Select QA model, skipping cooled-down ones. Returns a dict like select_model()."""
    for m in QA_MODEL_CHAIN:
        if not _is_model_cooled_down(m):
            return {"model": m, "label": f"{m} (QA)", "reason": "QA review agent"}
    # All cooled down — pick soonest available
    soonest = min(QA_MODEL_CHAIN, key=lambda m: _model_cooldowns.get(m, 0))
    wait = max(0, _model_cooldowns.get(soonest, 0) - time.time())
    if wait > 0:
        print(f"QA_WAIT | all QA models in cooldown, waiting {wait:.0f}s for {soonest}", flush=True)
        _interruptible_sleep(wait)
    return {"model": soonest, "label": f"{soonest} (QA)", "reason": "QA review agent (post-cooldown)"}


def run_qa_check(ticket):
    """Launch Copilot CLI as QA agent with fallback model selection.

    For simple tickets: static checks only (no model call).
    For medium/complex: static checks first, then model QA if static passes.
    """
    tid = ticket["id"]
    complexity = ticket.get("complexity", "medium")
    qa_cfg = QA_DEPTH.get(complexity, QA_DEPTH["medium"])

    # --- Pre-QA static checks (free, instant) ---
    static_ok, static_errors = _pre_qa_static_check(tid)
    if not static_ok:
        # Static checks caught real errors — fast-fail without model call
        os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
        log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_qa_{int(time.time())}.log")
        error_text = "\n".join(static_errors)
        with open(log_file, "w") as f:
            f.write(f"=== QA Review: {tid} | {ticket['title']} ===\n")
            f.write(f"=== Static pre-check — no model needed ===\n\n")
            f.write(f"QA_FAIL: Static checks failed:\n{error_text}\n")
        print(f"QA_STATIC_FAIL | {tid} | {len(static_errors)} error(s) | skipped model QA", flush=True)
        # Return a fake proc-like object so the caller can parse the log
        class _StaticResult:
            def __init__(self, lf):
                self._log_file = lf
                self._qa_mode = True
                self.pid = 0
                self.returncode = 1
            def poll(self):
                return self.returncode
        return _StaticResult(log_file)

    # --- Docs-only changes: skip model QA entirely ---
    try:
        _changed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
        )
        _changed_files = [f for f in _changed.stdout.strip().splitlines() if f.strip()]
        if _changed_files and all(f.endswith(".md") for f in _changed_files):
            os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
            log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_qa_{int(time.time())}.log")
            with open(log_file, "w") as f:
                f.write(f"=== QA Review: {tid} | {ticket['title']} ===\n")
                f.write(f"=== Docs-only change — no model QA needed ===\n\n")
                f.write(f"QA_PASS: Only documentation files changed ({', '.join(_changed_files)}). No code QA required.\n")
            print(f"QA_DOCS_SKIP | {tid} | docs-only change ({len(_changed_files)} .md files)", flush=True)
            class _DocsPass:
                def __init__(self, lf):
                    self._log_file = lf
                    self._qa_mode = True
                    self.pid = 0
                    self.returncode = 0
                def poll(self):
                    return self.returncode
            return _DocsPass(log_file)
    except Exception:
        pass

    # --- Simple tickets: static-only QA (no model call at all) ---
    if qa_cfg["static_only"]:
        os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
        log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_qa_{int(time.time())}.log")
        with open(log_file, "w") as f:
            f.write(f"=== QA Review: {tid} | {ticket['title']} ===\n")
            f.write(f"=== Static-only QA (simple ticket) — no model cost ===\n\n")
            f.write(f"QA_PASS: Static checks passed (syntax OK, no conflict markers). Simple ticket — model QA skipped.\n")
        print(f"QA_STATIC_PASS | {tid} | simple ticket, no model QA needed", flush=True)
        class _StaticPass:
            def __init__(self, lf):
                self._log_file = lf
                self._qa_mode = True
                self.pid = 0
                self.returncode = 0
            def poll(self):
                return self.returncode
        return _StaticPass(log_file)

    # --- Medium/Complex: full model QA ---
    model_info = _select_qa_model()
    model = model_info["model"]
    autopilot_turns = qa_cfg["autopilot"]

    os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
    log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_qa_{int(time.time())}.log")

    prompt = build_qa_prompt(ticket)

    cmd = [
        COPILOT_BIN,
        "-p", prompt,
        "--model", model,
        "--autopilot",
        "--allow-all",
        "--max-autopilot-continues", str(autopilot_turns),
    ]

    print(f"QA_START | {tid} | model={model} | autopilot={autopilot_turns} | log={log_file}", flush=True)

    try:
        lf = open(log_file, "w")
        lf.write(f"=== QA Review: {tid} | {ticket['title']} ===\n")
        lf.write(f"=== Model: {model} | Started: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")
        lf.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd="/opt/ethos",
            env={**os.environ, "TERM": "dumb"},
        )

        proc._log_fh = lf
        proc._log_file = log_file
        proc._qa_mode = True

        print(f"QA_RUNNING | {tid} | PID={proc.pid}", flush=True)
        return proc

    except Exception as e:
        print(f"QA_ERROR | {tid} | {e}", flush=True)
        add_comment(tid, f"[qa] Błąd uruchamiania QA: {e}")
        return None


def parse_qa_verdict(log_file):
    """Read QA log and extract QA_PASS or QA_FAIL verdict."""
    try:
        with open(log_file, "r") as f:
            content = f.read()
        # Search from the end for the verdict
        for line in reversed(content.splitlines()):
            line = line.strip()
            if line.startswith("QA_PASS:"):
                return "pass", line[8:].strip()
            if line.startswith("QA_FAIL:"):
                return "fail", line[8:].strip()
        # If no explicit verdict, check for keywords
        if "QA_PASS" in content:
            return "pass", "Implicit pass found in output"
        if "QA_FAIL" in content:
            return "fail", "Implicit fail found in output"
        return "unknown", "No QA verdict found in output"
    except Exception as e:
        return "error", str(e)

def build_copilot_prompt(ticket, agent, info, model_info, docs_context):
    """Build a focused prompt for Copilot CLI to execute a ticket."""
    tid = ticket["id"]
    title = ticket["title"]
    desc = ticket.get("description", "")
    priority = ticket.get("priority", "medium")
    complexity = ticket.get("complexity", "medium")
    lc = ticket.get("last_comment")
    feedback = f"\nUser feedback: {lc['text']}" if lc else ""

    doc_list = '\n'.join(f'   - /opt/ethos/docs/{d}' for d in info.get('docs', []))

    # Generate dynamic codebase map filtered for this agent type
    codebase_map = get_codebase_map(agent=agent)

    map_section = ""
    if codebase_map:
        map_section = f"\nCODEBASE MAP (use this to locate files — do NOT explore from scratch):\n{codebase_map}\n"

    # Pre-fetch relevant files via ripgrep to save agent exploration turns
    relevant_files = _pre_fetch_relevant_files(ticket)

    prompt = f"""You are an EthOS {agent} agent. Execute this ticket efficiently.

TICKET: {tid}
Title: {title}
{f'Description: {desc}' if desc else ''}
{feedback}

PROJECT: /opt/ethos/ (Flask backend + vanilla JS frontend, port 9000)
{map_section}{relevant_files}REFERENCE DOCS (summaries below — read full doc with cat only if needed):
{doc_list}

CRITICAL FACTS:
- Server runs on port 9000 (NOT 5000). API base: http://localhost:9000/api
- Files may be owned by root — if EACCES on write, use: sudo tee <file> or sudo cp
- Git push: sudo -u ${ETHOS_USER} git push

HELPER TOOLS (use these instead of manual exploration — saves time and tokens):
  bash /opt/ethos/tools/agent_helpers/find_route.sh <pattern>       — find API routes by keyword
  bash /opt/ethos/tools/agent_helpers/find_function.sh <name> [scope] — find function/class defs (scope: backend|frontend|all)
  bash /opt/ethos/tools/agent_helpers/file_overview.sh <file>        — get file structure (funcs, classes, routes, imports)
  bash /opt/ethos/tools/agent_helpers/check_syntax.sh [files...]     — validate Python/JS syntax
  bash /opt/ethos/tools/agent_helpers/safe_restart.sh {tid} <reason> — preflight + log + restart + verify (one command)
  bash /opt/ethos/tools/agent_helpers/get_api_token.sh               — get valid Bearer token for curl testing
  bash /opt/ethos/tools/agent_helpers/git_summary.sh [{tid}]         — git status + recent commits + diffs
  bash /opt/ethos/tools/agent_helpers/project_info.sh                — ports, paths, blueprints, ownership info

WORKFLOW:
1. Use the codebase map above to locate the relevant files — open them directly
2. Implement the solution
3. Validate: bash /opt/ethos/tools/agent_helpers/check_syntax.sh <changed files>
4. If backend changes: bash /opt/ethos/tools/agent_helpers/safe_restart.sh {tid} "<reason>"
5. Commit: git add <files> && git commit -m "[{tid}] <description>"
6. Push: sudo -u ${ETHOS_USER} git push

Be focused and efficient. Use the helper tools above instead of manual exploration."""

    # Inject context from previous attempts (survives watcher restarts)
    prior_parts = []
    git_ctx = _get_recent_git_context(tid)
    if git_ctx:
        prior_parts.append(git_ctx)
    log_ctx = _get_previous_log_summary(tid)
    if log_ctx:
        prior_parts.append(log_ctx)
    if prior_parts:
        prompt += "\n\n⚠️ PREVIOUS ATTEMPT CONTEXT (a prior run was interrupted — do NOT start from scratch):\n"
        prompt += "\n".join(prior_parts)

    return prompt


def _get_ticket_diff_context(tid):
    """Get a concise diff of all changes made for this ticket (committed and uncommitted)."""
    parts = []
    try:
        # Find the commit range for this ticket
        result = subprocess.run(
            ["git", "--no-pager", "log", "--oneline", "-30"],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            ticket_hashes = [l.split()[0] for l in lines if tid in l]
            if ticket_hashes:
                # Diff from just before the oldest ticket commit to HEAD
                oldest = ticket_hashes[-1]
                diff = subprocess.run(
                    ["git", "--no-pager", "diff", f"{oldest}~1..HEAD", "--stat"],
                    capture_output=True, text=True, cwd="/opt/ethos", timeout=5,
                )
                if diff.returncode == 0 and diff.stdout.strip():
                    parts.append(f"Files changed by this ticket (committed):\n{diff.stdout.strip()}")

                # Also get the actual diff (limited) so agent sees what was changed
                full_diff = subprocess.run(
                    ["git", "--no-pager", "diff", f"{oldest}~1..HEAD"],
                    capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
                )
                if full_diff.returncode == 0 and full_diff.stdout.strip():
                    diff_text = full_diff.stdout.strip()
                    if len(diff_text) > 8000:
                        diff_text = diff_text[:8000] + "\n... (diff truncated, use git diff to see full changes)"
                    parts.append(f"\nActual diff:\n```diff\n{diff_text}\n```")

        # Uncommitted changes
        uncommitted = subprocess.run(
            ["git", "--no-pager", "diff"],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
        )
        if uncommitted.returncode == 0 and uncommitted.stdout.strip():
            ud = uncommitted.stdout.strip()
            if len(ud) > 4000:
                ud = ud[:4000] + "\n... (truncated)"
            parts.append(f"\nUncommitted changes:\n```diff\n{ud}\n```")

    except Exception:
        pass
    return "\n".join(parts) if parts else ""


def _get_qa_log_details(tid):
    """Get the full QA failure analysis from the most recent QA log."""
    try:
        log_dir = COPILOT_LOG_DIR
        if not os.path.isdir(log_dir):
            return ""
        # Find QA logs for this ticket
        qa_logs = sorted(
            [f for f in os.listdir(log_dir)
             if f.startswith(tid) and "_qa_" in f and f.endswith(".log")],
            reverse=True
        )
        if not qa_logs:
            return ""
        log_path = os.path.join(log_dir, qa_logs[0])
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Extract the verdict section and any specific issue mentions
        # QA logs typically end with a structured verdict — get the last meaningful chunk
        lines = content.strip().splitlines()
        # Find where the actual analysis starts (after the header)
        analysis_start = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("YOUR TASK") or "1." in line or "Requirements" in line.lower():
                analysis_start = i
                break
        # Get from analysis to end, capped
        analysis = "\n".join(lines[analysis_start:])
        if len(analysis) > 5000:
            analysis = analysis[-5000:]
        return analysis
    except Exception:
        pass
    return ""


def build_rework_prompt(ticket, agent, info, model_info, docs_context, qa_reason, qa_cycle):
    """Build a prompt for dev agent to fix QA issues — includes actual diff and QA details."""
    tid = ticket["id"]
    title = ticket["title"]
    desc = ticket.get("description", "")
    doc_list = '\n'.join(f'   - /opt/ethos/docs/{d}' for d in info.get('docs', []))

    # Gather concrete context for the rework agent
    diff_context = _get_ticket_diff_context(tid)
    qa_log_details = _get_qa_log_details(tid)

    prompt = f"""You are an EthOS {agent} agent. A QA review found issues with your previous implementation. Fix them.

TICKET: {tid}
Title: {title}
{f'Description: {desc}' if desc else ''}

QA CYCLE: {qa_cycle}/{MAX_QA_CYCLES}

❌ QA FAILURE REASON:
{qa_reason}
"""

    if qa_log_details:
        prompt += f"""
📋 FULL QA ANALYSIS (from QA agent log):
{qa_log_details}
"""

    if diff_context:
        prompt += f"""
📁 YOUR PREVIOUS CHANGES (this is what you implemented — QA found issues in these):
{diff_context}
"""

    prompt += f"""
PROJECT: /opt/ethos/ (Flask backend + vanilla JS frontend, port 9000)
REFERENCE DOCS (consult only when relevant):
{doc_list}

CRITICAL FACTS:
- Server runs on port 9000 (NOT 5000). API base: http://localhost:9000/api
- Files may be owned by root — if EACCES on write, use: sudo tee <file> or sudo cp

HELPER TOOLS:
  bash /opt/ethos/tools/agent_helpers/check_syntax.sh [files...]     — validate syntax before restart
  bash /opt/ethos/tools/agent_helpers/safe_restart.sh {tid} <reason> — preflight + log + restart + verify
  bash /opt/ethos/tools/agent_helpers/find_function.sh <name>        — find function/class definitions
  bash /opt/ethos/tools/agent_helpers/file_overview.sh <file>        — get file structure overview

WORKFLOW:
1. Read the QA failure reason and full QA analysis above — they tell you EXACTLY what's wrong
2. The diff above shows your previous changes — locate the specific issues QA reported
3. Fix ONLY the issues identified by QA — do not restructure or rewrite working code
4. Validate: bash /opt/ethos/tools/agent_helpers/check_syntax.sh <changed files>
5. If backend changes: bash /opt/ethos/tools/agent_helpers/safe_restart.sh {tid} "QA fix"
6. Commit: git add <files> && git commit -m "[{tid}] fix: QA cycle {qa_cycle} — <description>"
7. Push: sudo -u ${ETHOS_USER} git push

IMPORTANT: You have the diff and QA analysis above. Go directly to fixing the issues.
Do NOT explore the codebase from scratch — start from the specific files mentioned in the QA feedback."""

    return prompt


def _get_recent_git_context(tid):
    """Get rich git context for a ticket: commits, changed files, uncommitted work, and diff summary."""
    parts = []
    try:
        # 1) Commits by this ticket
        result = subprocess.run(
            ["git", "--no-pager", "log", "--oneline", "-20"],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            ticket_commits = [l for l in lines if tid in l]
            if ticket_commits:
                parts.append("COMMITTED WORK (already done — do NOT redo):")
                for c in ticket_commits:
                    parts.append(f"  {c}")

                # 2) Diff stat for the most recent ticket commit (what files changed)
                latest_hash = ticket_commits[0].split()[0]
                diff_stat = subprocess.run(
                    ["git", "--no-pager", "diff", "--stat", f"{latest_hash}~1..{latest_hash}"],
                    capture_output=True, text=True, cwd="/opt/ethos", timeout=5,
                )
                if diff_stat.returncode == 0 and diff_stat.stdout.strip():
                    parts.append(f"\nFiles changed in latest commit ({latest_hash}):")
                    parts.append(diff_stat.stdout.strip())

        # 3) Uncommitted changes (staged + unstaged)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=5,
        )
        if status.returncode == 0 and status.stdout.strip():
            changed = status.stdout.strip().splitlines()
            parts.append(f"\nUNCOMMITTED CHANGES ({len(changed)} files — previous agent was interrupted mid-work):")
            for line in changed[:20]:  # cap at 20 files
                parts.append(f"  {line}")
            if len(changed) > 20:
                parts.append(f"  ... and {len(changed) - 20} more files")

            # 4) Brief diff of uncommitted work (limited to 3000 chars to avoid huge prompts)
            diff = subprocess.run(
                ["git", "--no-pager", "diff", "--stat"],
                capture_output=True, text=True, cwd="/opt/ethos", timeout=5,
            )
            if diff.returncode == 0 and diff.stdout.strip():
                parts.append(f"\nUncommitted diff summary:")
                parts.append(diff.stdout.strip()[:2000])

    except Exception:
        pass

    if not parts:
        return ""
    return "\n\nPREVIOUS WORK from prior attempt (interrupted by model error):\n" + "\n".join(parts) + \
        "\n\nIMPORTANT: Review committed and uncommitted changes above. Continue from where the previous agent stopped. " \
        "If uncommitted changes look correct, commit them. If they need fixes, fix and commit. Do NOT start over."


def _get_previous_log_summary(tid, max_chars=3000):
    """Extract key actions from the most recent copilot log for this ticket."""
    try:
        log_dir = COPILOT_LOG_DIR
        if not os.path.isdir(log_dir):
            return ""
        # Find logs for this ticket (exclude QA logs and prompt files)
        logs = sorted(
            [f for f in os.listdir(log_dir)
             if f.startswith(tid) and f.endswith(".log") and "_qa_" not in f and "_prompt" not in f],
            reverse=True
        )
        if not logs:
            return ""
        log_path = os.path.join(log_dir, logs[0])
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Extract just the last section which usually has the conclusion / summary
        if len(content) > max_chars:
            content = content[-max_chars:]
        # Filter to lines that look like actions (tool calls, decisions, errors)
        action_lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Keep lines with file paths, commands, errors, or decisions
            if any(kw in stripped.lower() for kw in [
                '/opt/ethos/', 'file:', 'error', 'commit', 'created', 'modified',
                'replaced', 'added', 'removed', 'def ', 'class ', 'function',
                'restart', 'curl', 'git ', '✓', '✗', '→', 'done', 'fixed',
            ]):
                action_lines.append(stripped)
        if action_lines:
            summary = "\n".join(action_lines[-40:])  # last 40 action lines
            return f"\nPrevious agent's actions (from log {logs[0]}):\n{summary}"
    except Exception:
        pass
    return ""

# ── Local AI helper (via model_library) ────────────────────────────────────
LOCAL_MAX_TOKENS = {
    "complex": 1024,
    "medium": 768,
    "simple": 512,
}
LOCAL_CTX_WINDOW = 2048         # must match model_library ctx_size
LOCAL_SYSTEM_OVERHEAD = 180     # rough token count for system prompt
LOCAL_MAX_RETRIES = 2           # max attempts per ticket before parking
_local_fail_counts = {}         # {ticket_id: fail_count}

def _build_local_system_prompt(model_name=None):
    """Build system prompt with the actual active model name."""
    label = model_name or "lokalny model"
    return (
        f"Jesteś lokalnym agentem deweloperskim EthOS ({label}). "
        "Realizujesz tickety z tablicy Kanban. Twoje zmiany zostaną automatycznie zaaplikowane przez system. "
        "OBOWIĄZKOWO generuj KOMPLETNY unified diff w blokach ```diff ... ```. "
        "Każdy diff musi zaczynać się od --- a/ścieżka i +++ b/ścieżka (względem /opt/ethos/). "
        "Podaj pełny kontekst (min. 3 linie przed i po zmianie). "
        "Na końcu dodaj sekcję TESTY z 2-3 szybkimi krokami weryfikacji. "
        "Odpowiadaj po polsku. Bądź zwięzły — liczy się poprawny diff."
    )


def _check_local_model_ready():
    """Check if a local model is available and ready. Returns (active_info, error_str)."""
    if _get_ml is None:
        return None, "Biblioteka modeli lokalnych niedostępna"
    try:
        lib = _get_ml()
        active = lib.get_active_model()
        if not active:
            return None, "Brak aktywnego modelu lokalnego. Włącz model w Bibliotece modeli."
        return active, None
    except Exception as e:
        return None, str(e)

def _run_local_model(prompt, max_tokens=900, temperature=0.15, log_file=None):
    """Execute local chat completion using active model_library model.

    When *log_file* is provided, tokens are streamed and appended to it in
    real-time so users can ``tail -f`` the file during inference.
    """
    active, err = _check_local_model_ready()
    if err:
        return None, active, err
    try:
        lib = _get_ml()
        llm, err = lib.load_model()
        if err:
            return None, active, err
        lib.touch_model()
        model_name = active.get("name") or active.get("id", "lokalny model")
        system_prompt = _build_local_system_prompt(model_name)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        # Stream tokens for real-time log visibility
        stream = llm.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        chunks = []
        log_fh = None
        try:
            if log_file:
                log_fh = open(log_file, "a")
                log_fh.write("### Response ###\n")
                log_fh.flush()
            for chunk in stream:
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                token = delta.get("content", "")
                if token:
                    chunks.append(token)
                    if log_fh:
                        log_fh.write(token)
                        log_fh.flush()
        finally:
            if log_fh:
                log_fh.write("\n")
                log_fh.close()

        content = "".join(chunks).strip()
        return content, active, None
    except Exception as e:
        return None, active, str(e)


def _truncate_to_tokens(text, max_tokens):
    """Rough truncation: ~3 chars per token for mixed PL/EN/code."""
    max_chars = max_tokens * 3
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...obcięto...]"


def build_local_plan_prompt(ticket, agent, info, max_response_tokens=768):
    """Construct a concise instruction for the local model (no autopilot).

    Prompt is trimmed so that prompt_tokens + max_response_tokens < LOCAL_CTX_WINDOW.
    """
    tid = ticket["id"]
    title = ticket["title"]
    desc = ticket.get("description", "")
    complexity = ticket.get("complexity", "medium")
    priority = ticket.get("priority", "medium")
    lc = ticket.get("last_comment")
    feedback = f"Ostatni komentarz: [{lc['author']}] {lc['text'][:200]}" if lc else ""
    doc_list = '\n'.join(f"- /opt/ethos/docs/{d}" for d in info.get("docs", []))
    codebase_map = get_codebase_map()

    # Budget: context_window - response_tokens - system_overhead
    prompt_budget = LOCAL_CTX_WINDOW - max_response_tokens - LOCAL_SYSTEM_OVERHEAD

    # Fixed parts of prompt (~200 tokens)
    header = f"""TICKET: {tid} — {title}
Priorytet: {priority} | Złożoność: {complexity}
{f'Opis: {desc[:300]}' if desc else ''}
{feedback}

Zasoby:
{doc_list if doc_list else '- brak dodatkowych dokumentów'}"""

    task = """\nZADANIE:
- Przygotuj plan wykonania (2–5 kroków).
- Wygeneruj KOMPLETNY unified diff dla KAŻDEGO pliku do zmiany.
- Format diffa MUSI być poprawny (--- a/path, +++ b/path, @@ hunki).
- Ścieżki podawaj względem /opt/ethos/, np. --- a/backend/blueprints/ddns.py
- Podaj min. 3 linie kontekstu przed i po każdej zmianie.
- Na końcu dodaj sekcję TESTY: 2–3 kroki weryfikacji.
"""

    # Budget for codebase map = total budget - header - task
    header_tokens = len(header) // 3
    task_tokens = len(task) // 3
    map_budget = max(100, prompt_budget - header_tokens - task_tokens)

    if codebase_map:
        codebase_map = _truncate_to_tokens(codebase_map, map_budget)

    prompt = f"""{header}

CODEBASE MAP (skrócony):
{codebase_map or 'brak mapy'}
{task}"""
    return prompt


def _parse_diffs(text):
    """Extract unified diff blocks from model output.

    Returns list of diff strings ready for ``git apply``.
    """
    diffs = []
    in_block = False
    current = []
    for line in text.splitlines():
        if line.strip().startswith("```diff"):
            in_block = True
            current = []
            continue
        if in_block and line.strip().startswith("```"):
            in_block = False
            if current:
                diffs.append("\n".join(current) + "\n")
            current = []
            continue
        if in_block:
            current.append(line)
    # Also try to catch inline diffs without fences (--- a/ ... +++ b/ pattern)
    if not diffs:
        current = []
        for line in text.splitlines():
            if line.startswith("--- a/") or line.startswith("diff --git"):
                if current:
                    diffs.append("\n".join(current) + "\n")
                current = [line]
            elif current:
                current.append(line)
        if current:
            diffs.append("\n".join(current) + "\n")
    return diffs


def _apply_and_commit(diffs, tid, title, model_label, log_file):
    """Apply parsed diffs via git apply, then commit.

    Returns (success: bool, message: str, files_changed: list).
    """
    if not diffs:
        return False, "Brak diffów do zaaplikowania", []

    applied_files = []
    failed_patches = []

    for i, diff_text in enumerate(diffs):
        patch_path = os.path.join(LOCALAI_LOG_DIR, f"{tid}_patch_{i}.diff")
        with open(patch_path, "w") as pf:
            pf.write(diff_text)

        # Try to apply
        result = subprocess.run(
            ["git", "apply", "--check", patch_path],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
        )
        if result.returncode != 0:
            # Try with --3way for fuzzy matching
            result = subprocess.run(
                ["git", "apply", "--check", "--3way", patch_path],
                capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
            )
        if result.returncode != 0:
            failed_patches.append((i, result.stderr.strip()))
            try:
                os.remove(patch_path)
            except OSError:
                pass
            continue

        # Apply for real
        apply_result = subprocess.run(
            ["git", "apply", patch_path],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
        )
        if apply_result.returncode != 0:
            # Retry with --3way
            apply_result = subprocess.run(
                ["git", "apply", "--3way", patch_path],
                capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
            )
        if apply_result.returncode == 0:
            # Extract filenames from diff
            for line in diff_text.splitlines():
                if line.startswith("+++ b/"):
                    applied_files.append(line[6:])
        else:
            failed_patches.append((i, apply_result.stderr.strip()))

        try:
            os.remove(patch_path)
        except OSError:
            pass

    if not applied_files:
        fail_reasons = "; ".join(f"patch {i}: {err[:100]}" for i, err in failed_patches)
        return False, f"Nie udało się zaaplikować żadnego patcha. {fail_reasons}", []

    # Stage and commit
    try:
        subprocess.run(
            ["git", "add"] + applied_files,
            capture_output=True, text=True, cwd="/opt/ethos", timeout=10,
        )
        short_title = title[:60].replace('"', "'")
        commit_msg = f"[{tid}] {short_title}\n\nLocal AI ({model_label})\nLog: {os.path.basename(log_file)}"
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True, text=True, cwd="/opt/ethos", timeout=15,
        )
        if commit_result.returncode != 0:
            return False, f"git commit failed: {commit_result.stderr.strip()[:200]}", applied_files
    except Exception as e:
        return False, f"git error: {e}", applied_files

    msg = f"Zaaplikowano {len(applied_files)} plik(ów): {', '.join(applied_files[:5])}"
    if failed_patches:
        msg += f" ({len(failed_patches)} patch(y) nie przeszło)"
    return True, msg, applied_files


def process_local_ticket(ticket):
    """Handle a ticket with the local model: generate diff, apply, commit, QA.

    Flow mirrors Copilot: edit files → git commit → move to QA.
    If diff application fails, falls back to plan-only mode (comment + Review).
    """
    tid = ticket["id"]
    title = ticket["title"]
    agent = detect_agent(title, ticket.get("labels", []))
    info = AGENT_MAP.get(agent, AGENT_MAP["General"])
    complexity = ticket.get("complexity", "medium")

    # Retry guard: don't re-attempt tickets that permanently fail
    fail_count = _local_fail_counts.get(tid, 0)
    if fail_count >= LOCAL_MAX_RETRIES:
        print(f"LOCAL_PARKED | {tid} | max retries ({LOCAL_MAX_RETRIES}) reached, skipping", flush=True)
        return

    # Pre-check: is a local model actually available?
    active_pre, pre_err = _check_local_model_ready()
    if pre_err:
        print(f"LOCAL_SKIP | {tid} | {pre_err}", flush=True)
        add_comment(tid, f"[localai] {pre_err}")
        return

    model_name = active_pre.get("name") or active_pre.get("id", "Local AI")
    os.makedirs(LOCALAI_LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOCALAI_LOG_DIR, f"{tid}_local_{int(time.time())}.log")

    set_executing(tid, {"model": "local", "label": model_name}, agent="localai", pid=os.getpid(), log_file=log_file)

    try:
        assign_ticket(tid, "localai")
        move_ticket(tid, "W trakcie")
    except Exception as e:
        print(f"LOCAL_ASSIGN_ERROR | {tid} | {e}", flush=True)

    max_tok = LOCAL_MAX_TOKENS.get(complexity, 768)
    prompt = build_local_plan_prompt(ticket, agent, info, max_response_tokens=max_tok)

    # Write log header + prompt BEFORE inference so users can tail -f
    with open(log_file, "w") as f:
        f.write(f"=== Local AI Ticket ===\n")
        f.write(f"Ticket: {tid} | {title}\n")
        f.write(f"Model: {model_name}\n")
        f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("### Prompt ###\n")
        f.write(prompt + "\n\n")

    # Streaming inference — tokens are appended to log_file in real-time
    result, active_model, err = _run_local_model(prompt, max_tokens=max_tok, log_file=log_file)

    if err:
        with open(log_file, "a") as f:
            f.write(f"\nERROR: {err}\n")

    if err:
        _local_fail_counts[tid] = fail_count + 1
        remaining = LOCAL_MAX_RETRIES - fail_count - 1
        if remaining > 0:
            add_comment(tid, f"[localai] Błąd: {err}. Ponowię próbę ({remaining} pozostało).")
        else:
            add_comment(tid, f"[localai] Błąd: {err}. Ticket wymaga interwencji (wyczerpano {LOCAL_MAX_RETRIES} prób).")
        try:
            move_ticket(tid, "Do zrobienia")
        except Exception:
            pass
        clear_executing()
        return

    # Success — clear fail counter
    _local_fail_counts.pop(tid, None)

    model_label = active_model.get("name") or active_model.get("id") if active_model else "local"

    # --- Try to apply diffs and commit (like Copilot) ---
    diffs = _parse_diffs(result or "")
    applied = False
    apply_msg = ""
    files_changed = []
    if diffs:
        applied, apply_msg, files_changed = _apply_and_commit(
            diffs, tid, title, model_label, log_file)
        with open(log_file, "a") as f:
            f.write(f"\n### Apply Result ###\n")
            f.write(f"Applied: {applied} | {apply_msg}\n")
            if files_changed:
                f.write(f"Files: {', '.join(files_changed)}\n")
        print(f"LOCAL_APPLY | {tid} | applied={applied} | {apply_msg}", flush=True)

    summary = (result or "")[:1500]
    if applied:
        # Full flow — files changed, committed, go to QA
        add_comment(
            tid,
            f"[localai] Zmiany zaaplikowane i scommitowane (model: {model_label}).\n"
            f"{apply_msg}\n"
            f"Log: {os.path.basename(log_file)}\n\n{summary}"
        )
        try:
            move_ticket(tid, "QA")
            print(f"MOVED_TO_QA | {tid} | local", flush=True)
        except Exception as e:
            print(f"LOCAL_MOVE_ERROR | {tid} | {e}", flush=True)
    else:
        # Fallback — plan only, no file changes
        fallback_note = f"\n⚠️ Patch nie przeszedł: {apply_msg}" if diffs else ""
        add_comment(
            tid,
            f"[localai] Plan i propozycje zmian (model: {model_label}).{fallback_note}\n"
            f"Log: {os.path.basename(log_file)}\n\n{summary}"
        )
        try:
            move_ticket(tid, "Review")
        except Exception as e:
            print(f"LOCAL_MOVE_ERROR | {tid} | {e}", flush=True)
    clear_executing()

def build_retry_prompt(ticket, agent, info, model_info, docs_context, prev_model, failure_type):
    """Build a prompt for retry/failover that includes rich context from the previous attempt."""
    base_prompt = build_copilot_prompt(ticket, agent, info, model_info, docs_context)
    tid = ticket["id"]

    # build_copilot_prompt already injects git context and log summary if available.
    # Only add retry-specific note about model switch.
    if "PREVIOUS ATTEMPT CONTEXT" not in base_prompt:
        # No prior context was found — add a fallback hint
        base_prompt += (
            f"\n\nNOTE: A previous attempt with model {prev_model} was interrupted ({failure_type}). "
            f"Run: git --no-pager log --oneline -10 and git status to see if any partial work exists."
        )

    return base_prompt


def execute_via_copilot(ticket, agent, info, model_info, docs_context, override_prompt=None):
    """Launch Copilot CLI in non-interactive mode to solve the ticket."""
    tid = ticket["id"]
    model = model_info["model"]

    # Ensure log dir exists
    os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
    log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_{int(time.time())}.log")

    prompt = override_prompt or build_copilot_prompt(ticket, agent, info, model_info, docs_context)

    # Prompt dedup: reuse prompt file if content hasn't changed
    prompt_file, reused = _dedup_prompt(tid, prompt, COPILOT_LOG_DIR)
    if reused:
        print(f"PROMPT_REUSED | {tid} | same prompt hash, skipping rewrite", flush=True)

    cmd = [
        COPILOT_BIN,
        "-p", prompt,
        "--model", model,
        "--autopilot",
        "--allow-all",
        "--max-autopilot-continues", str(MAX_AUTOPILOT.get(ticket.get("complexity", "medium"), 15)),
    ]
    reasoning_effort = model_info.get("reasoning_effort")
    if reasoning_effort:
        cmd.extend(["--reasoning-effort", str(reasoning_effort)])

    print(f"COPILOT_START | {tid} | model={model} | log={log_file}", flush=True)

    try:
        # Open log file persistently (not in with-block) so subprocess can write
        lf = open(log_file, "w")
        lf.write(f"=== Ticket: {tid} | {ticket['title']} ===\n")
        lf.write(f"=== Model: {model} | Agent: {agent} ===\n")
        lf.write(f"=== Started: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")
        lf.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd="/opt/ethos",
            env={**os.environ, "TERM": "dumb"},
        )

        # Attach file handle to proc so it stays open until process ends
        # Reset live log monitor position and transient error counter for this new process
        global _last_log_size, _live_transient_count
        _last_log_size = 0
        _live_transient_count = 0

        proc._log_fh = lf
        proc._log_file = log_file
        proc._prompt_file = prompt_file
        proc._model_info = model_info
        proc._ticket_ctx = {
            "ticket": ticket,
            "agent": agent,
            "info": info,
            "docs_context": docs_context,
        }

        # Store PID in lock for monitoring (preserve qa_cycle and agent from set_executing)
        existing = get_executing() or {}
        with open(LOCK_FILE, "w") as f:
            json.dump({
                "ticket_id": tid,
                "started": time.time(),
                "model": model_info,
                "copilot_pid": proc.pid,
                "log_file": log_file,
                "qa_cycle": existing.get("qa_cycle", 0),
                "agent": existing.get("agent", "copilot"),
            }, f)

        print(f"COPILOT_RUNNING | {tid} | PID={proc.pid}", flush=True)
        return proc

    except Exception as e:
        print(f"COPILOT_ERROR | {tid} | {e}", flush=True)
        _err_agent = (get_executing() or {}).get("agent", "copilot")
        add_comment(tid, f"[{_err_agent}] Błąd uruchamiania agenta: {e}")
        return None


# ── Auto-execute ─────────────────────────────────────────────────────────

def auto_start_ticket(ticket):
    """Start ticket: assign, move, select model by complexity, launch Copilot CLI."""
    tid = ticket["id"]
    title = ticket["title"]

    # Global attempt cap: prevent infinite retry loops
    allowed, attempt_count = _record_attempt(tid)
    if not allowed:
        print(f"SHELVED | {tid} | {attempt_count} total attempts (max {MAX_TOTAL_ATTEMPTS}) — moving to Review", flush=True)
        add_comment(tid,
            f"[system] Ticket automatycznie odłożony po {attempt_count} próbach. "
            f"Wymaga interwencji manualnej (limit: {MAX_TOTAL_ATTEMPTS} prób).")
        try:
            move_ticket(tid, "Review")
        except Exception:
            pass
        return tid, None

    agent = detect_agent(title, ticket.get("labels", []))
    info = AGENT_MAP.get(agent, AGENT_MAP["General"])
    model_info = select_model(ticket)
    complexity = ticket.get("complexity", "medium")

    # Assign and move
    assign_ticket(tid, "copilot")
    move_ticket(tid, "W trakcie")
    set_executing(tid, model_info)

    # Build context
    desc = ticket.get("description", "")
    lc = ticket.get("last_comment")
    docs_context = load_docs_context(agent)

    print(f"\n{'='*70}", flush=True)
    print(f"EXECUTE | {tid} | {agent}", flush=True)
    print(f"Title: {title}", flush=True)
    print(f"Priority: {ticket['priority']}", flush=True)
    print(f"Complexity: {complexity} → Model: {model_info['model']} ({model_info['label']})", flush=True)
    if desc: print(f"Description: {desc[:200]}", flush=True)
    print(f"Docs: {', '.join(info.get('docs', []))}", flush=True)
    print(f"{'='*70}", flush=True)

    comment = (
        f"[copilot] Rozpoczynam prace nad ticketem.\n"
        f"Agent: {agent} | Model: {model_info['model']} ({model_info['label']})\n"
        f"Złożoność: {complexity} | Docs: {', '.join(info.get('docs', []))}"
    )
    add_comment(tid, comment)

    # Launch Copilot CLI to actually solve the ticket
    proc = execute_via_copilot(ticket, agent, info, model_info, docs_context)
    if proc is None:
        print(f"LAUNCH_FAILED | {tid} | Copilot failed to start — returning ticket to queue", flush=True)
        clear_executing()
        try:
            move_ticket(tid, "Do zrobienia")
        except Exception as me:
            print(f"MOVE_ERROR | {tid} | {me}", flush=True)
    return tid, proc


def auto_rework_ticket(ticket, qa_reason, qa_cycle):
    """Re-launch dev agent to fix QA issues. Ticket stays in W trakcie."""
    tid = ticket["id"]
    title = ticket["title"]
    agent = detect_agent(title, ticket.get("labels", []))
    info = AGENT_MAP.get(agent, AGENT_MAP["General"])
    ticket_agent = ticket.get("agent", "copilot")
    if ticket_agent == "freemodel":
        complexity = ticket.get("complexity", "medium")
        model_info = FREE_MODEL_MAP.get(complexity, FREE_MODEL_MAP["medium"])
    else:
        model_info = select_model(ticket)
    complexity = ticket.get("complexity", "medium")
    # Rework prompt builds its own context — skip expensive docs reload
    docs_context = ""

    set_executing(tid, model_info, qa_cycle=qa_cycle, agent=ticket_agent)

    print(f"\n{'='*70}", flush=True)
    print(f"REWORK | {tid} | {agent} | QA cycle {qa_cycle}/{MAX_QA_CYCLES} | agent={ticket_agent}", flush=True)
    print(f"Title: {title}", flush=True)
    print(f"QA reason: {qa_reason[:200]}", flush=True)
    print(f"Model: {model_info['model']} ({model_info['label']})", flush=True)
    print(f"{'='*70}", flush=True)

    add_comment(tid,
        f"[{ticket_agent}] Rozpoczynam poprawki po QA (cykl {qa_cycle}/{MAX_QA_CYCLES}).\n"
        f"Agent: {agent} | Model: {model_info['model']}\n"
        f"QA feedback: {qa_reason[:500]}")

    rework_prompt = build_rework_prompt(ticket, agent, info, model_info, docs_context, qa_reason, qa_cycle)
    proc = execute_via_copilot(ticket, agent, info, model_info, docs_context, override_prompt=rework_prompt)
    if proc is None:
        print(f"REWORK_LAUNCH_FAILED | {tid} | Copilot failed to start", flush=True)
        clear_executing()
        try:
            move_ticket(tid, "Do zrobienia")
            add_comment(tid, f"[system] Nie udało się uruchomić agenta do poprawek. Ticket wraca do kolejki.")
        except Exception as me:
            print(f"MOVE_ERROR | {tid} | {me}", flush=True)
    return tid, proc


# ── Main loop ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    parser.add_argument("--auto", action="store_true", help="Auto-start tickets from Do zrobienia")
    args = parser.parse_args()

    mode = "AUTO" if args.auto else "WATCH"
    print(f"Ticket Watcher | poll={POLL_ACTIVE}s(active)/{POLL_IDLE}s(idle) | mode={mode}", flush=True)

    global _shutdown_event
    _shutdown_event = threading.Event()
    print(f"Monitoring copilot-enabled projects via /copilot/queue", flush=True)
    print(f"Connection pooling: enabled | Retry: 3x with backoff on 502/503/504", flush=True)
    print(f"Model mapping: complex→{COMPLEXITY_MODEL_MAP['complex']['model']}, medium→{COMPLEXITY_MODEL_MAP['medium']['model']}, simple→{COMPLEXITY_MODEL_MAP['simple']['model']}", flush=True)
    routing_str = ", ".join(f"{k}: {' → '.join(v)}" for k, v in MODEL_ROUTING.items())
    print(f"Fallback routing: {routing_str}", flush=True)
    print(f"QA agent: {QA_MODEL_CHAIN[0]} (free) | Flow: Dev→QA→Review (fail→Rework→QA, max {MAX_QA_CYCLES} cycles)", flush=True)
    free_str = ", ".join(f"{k}→{v['model']}" for k, v in FREE_MODEL_MAP.items())
    print(f"Free model mapping: {free_str}", flush=True)
    print(f"Copilot CLI: {COPILOT_BIN}", flush=True)

    # Clean up stale lock from previous watcher instance
    cleanup_stale_lock()

    # Re-queue any tickets left stranded in 'W trakcie' from a previous run
    if args.auto:
        resume_in_progress_tickets()

    prev_state = {}
    api_fail_count = 0
    # Dev process tracking
    active_proc = None
    active_ticket_id = None
    # QA process tracking (separate from dev)
    qa_proc = None
    qa_ticket_id = None
    # QA cycle tracking per ticket {ticket_id: cycle_count}
    qa_cycles = {}
    todo = []
    local_busy = False

    # ── Graceful shutdown on SIGTERM / SIGINT ─────────────────────────────
    _shutting_down = False

    def _graceful_shutdown(signum, frame):
        nonlocal _shutting_down, active_proc, active_ticket_id, qa_proc, qa_ticket_id
        if _shutting_down:
            return
        _shutting_down = True
        _shutdown_event.set()  # unblock any _interruptible_sleep()
        sig_name = signal.Signals(signum).name
        print(f"\n{'='*70}", flush=True)
        print(f"SHUTDOWN | signal={sig_name} | graceful teardown", flush=True)

        # Terminate DEV process
        if active_proc is not None and active_proc.poll() is None:
            pid = active_proc.pid
            print(f"SHUTDOWN | terminating DEV PID={pid} ({active_ticket_id})", flush=True)
            try:
                active_proc.terminate()
                try: active_proc.wait(timeout=10)
                except subprocess.TimeoutExpired: active_proc.kill()
            except OSError:
                pass
            if hasattr(active_proc, '_log_fh'):
                try: active_proc._log_fh.close()
                except: pass
            # Record interrupted state so next startup knows what happened
            try:
                executing = get_executing() or {}
                executing["interrupted_by"] = sig_name
                executing["interrupted_at"] = time.time()
                with open(LOCK_FILE, "w") as f:
                    json.dump(executing, f)
                print(f"SHUTDOWN | lock saved for {active_ticket_id} (will resume on restart)", flush=True)
            except Exception as e:
                print(f"SHUTDOWN | lock write failed: {e}", flush=True)
            try:
                add_comment(active_ticket_id,
                    f"[system] Watcher zatrzymany ({sig_name}). "
                    f"Ticket zostanie wznowiony po restarcie watchera.")
            except Exception:
                pass

        # Terminate QA process
        if qa_proc is not None and qa_proc.poll() is None:
            pid = qa_proc.pid
            print(f"SHUTDOWN | terminating QA PID={pid} ({qa_ticket_id})", flush=True)
            try:
                qa_proc.terminate()
                try: qa_proc.wait(timeout=10)
                except subprocess.TimeoutExpired: qa_proc.kill()
            except OSError:
                pass
            if hasattr(qa_proc, '_log_fh'):
                try: qa_proc._log_fh.close()
                except: pass

        print(f"SHUTDOWN | complete", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    while True:
        try:
            # Globals for pacing and live monitoring
            global _last_ticket_finished, _last_rate_limit_hit, _last_live_check, _last_log_size

            # --- Detect dead DEV process (PID gone but poll() wasn't called) ---
            if active_proc is not None and active_proc.poll() is None:
                if not pid_alive(active_proc.pid):
                    print(f"\nDEAD_PROCESS | {active_ticket_id} | PID {active_proc.pid} vanished", flush=True)
                    if hasattr(active_proc, '_log_fh'):
                        try: active_proc._log_fh.close()
                        except: pass
                    executing = get_executing()
                    log_file = executing.get("log_file", "?") if executing else "?"
                    _dead_agent = executing.get("agent", "copilot") if isinstance(executing, dict) else "copilot"
                    add_comment(active_ticket_id,
                        f"[{_dead_agent}] Proces agenta zniknął nieoczekiwanie (PID {active_proc.pid}). Log: {log_file}")
                    try:
                        move_ticket(active_ticket_id, "Do zrobienia")
                        print(f"DEAD_REQUEUE | {active_ticket_id} | moved back to Do zrobienia", flush=True)
                    except Exception as me:
                        print(f"MOVE_ERROR | {active_ticket_id} | {me}", flush=True)
                    clear_executing()
                    active_proc = None
                    active_ticket_id = None

            # --- Check if DEV Copilot process finished ---
            if active_proc is not None:
                retcode = active_proc.poll()
                if retcode is not None:
                    if hasattr(active_proc, '_log_fh'):
                        try: active_proc._log_fh.close()
                        except: pass
                    if hasattr(active_proc, '_prompt_file'):
                        try: os.remove(active_proc._prompt_file)
                        except: pass

                    executing = get_executing()
                    log_file = executing.get("log_file", "?") if executing else "?"
                    _agent_prefix = (executing.get("agent", "copilot") if isinstance(executing, dict) else "copilot")
                    if retcode == 0:
                        # Preserve qa_cycle from lock file before clearing
                        qa_cycle_for_ticket = 0
                        if isinstance(executing, dict):
                            qa_cycle_for_ticket = executing.get("qa_cycle", 0)
                        qa_cycles[active_ticket_id] = qa_cycle_for_ticket

                        _exec_duration = time.time() - (executing.get("started", time.time()) if isinstance(executing, dict) else time.time())
                        _done_model = (executing.get("model", {}) or {}).get("model", "?") if isinstance(executing, dict) else "?"

                        # Post-completion health check: if the run had many transient errors,
                        # set a soft cooldown so the next ticket picks a different model
                        transient_count = _count_transient_errors(log_file)
                        if transient_count >= TRANSIENT_SOFT_COOLDOWN_THRESHOLD:
                            soft_cd = 90  # 90s soft cooldown — enough to use a different model for 1-2 next tickets
                            _set_model_cooldown(_done_model, soft_cd)
                            print(f"\nCOPILOT_DONE | {active_ticket_id} | exit=0 | qa_cycle={qa_cycle_for_ticket} | log={log_file} | ⚠ {transient_count} transient errors → soft cooldown {soft_cd}s on {_done_model}", flush=True)
                            _record_metric(_done_model, "ok", _exec_duration)
                            add_comment(active_ticket_id,
                                f"[{_agent_prefix}] Zakończyłem pracę (exit 0), ale z {transient_count} transient API errors. "
                                f"Model {_done_model} w soft-cooldownie ({soft_cd}s). Log: {log_file}")
                        else:
                            print(f"\nCOPILOT_DONE | {active_ticket_id} | exit=0 | qa_cycle={qa_cycle_for_ticket} | log={log_file}", flush=True)
                            _record_metric(_done_model, "ok", _exec_duration)
                            add_comment(active_ticket_id,
                                f"[{_agent_prefix}] Zakończyłem pracę nad ticketem (exit 0). Log: {log_file}")
                        # ── Health gate: verify server survived before moving to QA ──
                        healthy, health_detail = _check_ethos_health()
                        if not healthy:
                            print(f"\nHEALTH_GATE_FAIL | {active_ticket_id} | {health_detail} — attempting auto-revert", flush=True)
                            reverted, revert_detail = _auto_revert_and_recover(active_ticket_id)
                            if reverted:
                                add_comment(active_ticket_id,
                                    f"[system] ⚠️ Serwer padł po commicie agenta. "
                                    f"Auto-revert wykonany ({revert_detail}). "
                                    f"Ticket wraca do kolejki.")
                                print(f"HEALTH_GATE_REVERTED | {active_ticket_id} | {revert_detail}", flush=True)
                            else:
                                add_comment(active_ticket_id,
                                    f"[system] ❌ Serwer padł po commicie agenta. "
                                    f"Auto-revert nie powiódł się: {revert_detail}. "
                                    f"Wymaga interwencji manualnej!")
                                print(f"HEALTH_GATE_REVERT_FAILED | {active_ticket_id} | {revert_detail}", flush=True)
                            try:
                                move_ticket(active_ticket_id, "Do zrobienia")
                            except Exception:
                                print(f"MOVE_ERROR | {active_ticket_id} | could not move back to queue after health gate fail", flush=True)
                            clear_executing()
                            _last_ticket_finished = time.time()
                            active_proc = None
                            active_ticket_id = None
                            continue

                        # Health OK — move to QA
                        try:
                            move_ticket(active_ticket_id, "QA")
                            print(f"MOVED_TO_QA | {active_ticket_id}", flush=True)
                        except Exception as me:
                            print(f"MOVE_ERROR | {active_ticket_id} | QA move failed: {me} — retrying...", flush=True)
                            time.sleep(2)
                            try:
                                move_ticket(active_ticket_id, "QA")
                                print(f"MOVED_TO_QA | {active_ticket_id} | retry OK", flush=True)
                            except Exception:
                                try:
                                    move_ticket(active_ticket_id, "Do zrobienia")
                                    print(f"MOVE_FALLBACK | {active_ticket_id} | back to Do zrobienia", flush=True)
                                except Exception:
                                    print(f"MOVE_CRITICAL | {active_ticket_id} | all move attempts failed — ticket stranded in W trakcie!", flush=True)
                    else:
                        model_info = {}
                        if isinstance(executing, dict):
                            model_info = executing.get("model") or {}
                        if (not model_info) and hasattr(active_proc, "_model_info"):
                            model_info = active_proc._model_info

                        retry_count = 0
                        tried_models = []
                        if isinstance(executing, dict):
                            retry_count = executing.get("retry_count", 0)
                            tried_models = executing.get("tried_models", [])

                        failure_type = _classify_failure(log_file)
                        # If live monitor already killed this process, use its diagnosis
                        if isinstance(executing, dict) and executing.get("killed_by_live_monitor"):
                            failure_type = executing["killed_by_live_monitor"]
                        ctx = getattr(active_proc, "_ticket_ctx", None)
                        complexity = ctx["ticket"].get("complexity", "medium") if ctx else "medium"
                        current_model = model_info.get("model", "?") if isinstance(model_info, dict) else "?"
                        retried = False

                        if failure_type == "rate_limit":
                            # Set global cooldown on this model so it's skipped everywhere
                            _set_model_cooldown(current_model)
                            _last_rate_limit_hit = time.time()
                            _record_metric(current_model, "rate_limit")

                            # 429: exponential backoff with BACKOFF_BASE_SECS, retry up to MAX_RETRIES_SAME_MODEL
                            if retry_count < MAX_RETRIES_SAME_MODEL:
                                backoff = min(BACKOFF_BASE_SECS * (2 ** retry_count), MAX_BACKOFF_SECS)
                                print(
                                    f"\nRATE_LIMIT_429 | {active_ticket_id} | {current_model} | "
                                    f"retry {retry_count + 1}/{MAX_RETRIES_SAME_MODEL} | backoff {backoff}s",
                                    flush=True,
                                )
                                add_comment(
                                    active_ticket_id,
                                    f"[system] Rate limit na modelu {current_model}. "
                                    f"Cooldown {backoff}s (próba {retry_count + 1}/{MAX_RETRIES_SAME_MODEL}).",
                                )
                                _interruptible_sleep(backoff)
                                if ctx:
                                    set_executing(active_ticket_id, model_info, retry_count + 1, tried_models)
                                    retry_prompt = build_retry_prompt(
                                        ctx["ticket"], ctx["agent"], ctx["info"],
                                        model_info, ctx["docs_context"],
                                        current_model, failure_type)
                                    retry_proc = execute_via_copilot(
                                        ctx["ticket"], ctx["agent"], ctx["info"],
                                        model_info, ctx["docs_context"],
                                        override_prompt=retry_prompt,
                                    )
                                    if retry_proc is not None:
                                        active_proc = retry_proc
                                        retried = True
                            if not retried:
                                # Exhausted retries on this model → switch provider
                                tried_models = list(set(tried_models + [current_model]))
                                fallback = _get_next_fallback(model_info, complexity, tried_models)
                                if fallback and ctx:
                                    # Wait before switching to avoid hammering new provider
                                    switch_wait = min(BACKOFF_BASE_SECS, 60)
                                    print(
                                        f"\nPROVIDER_SWITCH | {active_ticket_id} | "
                                        f"{current_model} -> {fallback['model']} "
                                        f"(rate limit, {MAX_RETRIES_SAME_MODEL}x exhausted) "
                                        f"| wait {switch_wait}s | tried: {tried_models}",
                                        flush=True,
                                    )
                                    add_comment(
                                        active_ticket_id,
                                        f"[system] Rate limit {current_model} ({MAX_RETRIES_SAME_MODEL}x). "
                                        f"Przełączam na {fallback['model']} za {switch_wait}s.",
                                    )
                                    _interruptible_sleep(switch_wait)
                                    set_executing(active_ticket_id, fallback, 0, tried_models)
                                    switch_prompt = build_retry_prompt(
                                        ctx["ticket"], ctx["agent"], ctx["info"],
                                        fallback, ctx["docs_context"],
                                        current_model, "rate_limit")
                                    retry_proc = execute_via_copilot(
                                        ctx["ticket"], ctx["agent"], ctx["info"],
                                        fallback, ctx["docs_context"],
                                        override_prompt=switch_prompt,
                                    )
                                    if retry_proc is not None:
                                        active_proc = retry_proc
                                        retried = True
                                    else:
                                        add_comment(active_ticket_id,
                                            f"[system] Fallback na {fallback['model']} nie powiódł się.")

                        elif failure_type == "quota_exhausted":
                            # 402: account quota exhausted — no point retrying ANY model
                            print(f"\nQUOTA_EXHAUSTED | {active_ticket_id} | {current_model} | no retry, returning to queue", flush=True)
                            _record_metric(current_model, "fail")
                            add_comment(active_ticket_id,
                                f"[system] Brak limitu (402 quota exhausted). "
                                f"Ticket wraca do kolejki — wymaga odnowienia limitu Copilot.")
                            try:
                                move_ticket(active_ticket_id, "Do zrobienia")
                            except Exception as me:
                                print(f"MOVE_ERROR | {active_ticket_id} | {me}", flush=True)
                            clear_executing()
                            _last_ticket_finished = time.time()
                            active_proc = None
                            active_ticket_id = None
                            # Pause watcher for 5 min — quota won't reset quickly
                            print("QUOTA_PAUSE | sleeping 300s before checking queue again", flush=True)
                            _interruptible_sleep(300)
                            continue

                        elif failure_type in ("server_error", "transient"):
                            # 5xx or transient: short cooldown + failover to next provider
                            _set_model_cooldown(current_model, 120)  # 2 min cooldown
                            _record_metric(current_model, failure_type if failure_type == "transient" else "server_error")
                            tried_models = list(set(tried_models + [current_model]))
                            fallback = _get_next_fallback(model_info, complexity, tried_models)
                            if fallback and ctx:
                                err_label = "transient API errors" if failure_type == "transient" else "5xx"
                                print(
                                    f"\n{'TRANSIENT' if failure_type == 'transient' else 'SERVER_ERROR'}_FAILOVER | {active_ticket_id} | "
                                    f"{current_model} -> {fallback['model']} "
                                    f"({err_label} failover, 15s wait) | tried: {tried_models}",
                                    flush=True,
                                )
                                add_comment(
                                    active_ticket_id,
                                    f"[system] Błąd modelu {current_model} ({err_label}). "
                                    f"Przełączam na {fallback['model']}.",
                                )
                                _interruptible_sleep(15)  # brief pause before hitting a new provider
                                set_executing(active_ticket_id, fallback, 0, tried_models)
                                err_prompt = build_retry_prompt(
                                    ctx["ticket"], ctx["agent"], ctx["info"],
                                    fallback, ctx["docs_context"],
                                    current_model, failure_type)
                                retry_proc = execute_via_copilot(
                                    ctx["ticket"], ctx["agent"], ctx["info"],
                                    fallback, ctx["docs_context"],
                                    override_prompt=err_prompt,
                                )
                                if retry_proc is not None:
                                    active_proc = retry_proc
                                    retried = True
                                else:
                                    add_comment(active_ticket_id,
                                        f"[system] Failover na {fallback['model']} nie powiódł się.")

                        if retried:
                            continue

                        # All retries/fallbacks exhausted or unknown failure
                        tried_str = ", ".join(tried_models) if tried_models else current_model
                        print(f"\nCOPILOT_FAILED | {active_ticket_id} | exit={retcode} | type={failure_type} | tried: [{tried_str}] | log={log_file}", flush=True)
                        _record_metric(current_model, "fail")
                        add_comment(active_ticket_id,
                            f"[{_agent_prefix}] Zakończono z błędem (exit {retcode}, {failure_type}). "
                            f"Wypróbowane modele: [{tried_str}]. Log: {log_file}")
                        try:
                            move_ticket(active_ticket_id, "Do zrobienia")
                            print(f"MOVED_TO_TODO | {active_ticket_id} (failed, needs rework)", flush=True)
                        except Exception as me:
                            print(f"MOVE_ERROR | {active_ticket_id} | {me}", flush=True)
                    clear_executing()
                    _last_ticket_finished = time.time()
                    _print_metrics_summary()
                    active_proc = None
                    active_ticket_id = None

            # --- Live log monitoring: detect rate limits / transient errors during execution ---
            if active_proc is not None and active_proc.poll() is None:
                executing = get_executing()
                log_file = executing.get("log_file", "") if executing else ""
                live_issue = check_running_process_for_rate_limit(active_proc, log_file)
                if live_issue in ("rate_limit", "transient"):
                    model_info = executing.get("model", {}) if executing else {}
                    current_model = model_info.get("model", "?") if isinstance(model_info, dict) else "?"
                    issue_label = "LIVE_RATE_LIMIT" if live_issue == "rate_limit" else "LIVE_TRANSIENT"
                    metric_key = "rate_limit" if live_issue == "rate_limit" else "transient"
                    print(f"\n{issue_label} | {active_ticket_id} | {current_model} | killing PID {active_proc.pid}", flush=True)
                    _set_model_cooldown(current_model)
                    _last_rate_limit_hit = time.time()
                    _record_metric(current_model, metric_key)
                    # Kill the process — it's stuck retrying anyway
                    try:
                        active_proc.terminate()
                        try: active_proc.wait(timeout=5)
                        except subprocess.TimeoutExpired: active_proc.kill()
                    except OSError:
                        pass
                    if hasattr(active_proc, '_log_fh'):
                        try: active_proc._log_fh.close()
                        except: pass
                    if live_issue == "rate_limit":
                        add_comment(active_ticket_id,
                            f"[system] Rate limit wykryty w trakcie wykonania ({current_model}). "
                            f"Proces zatrzymany, model w cooldownie na {MODEL_COOLDOWN_SECS}s.")
                    else:
                        add_comment(active_ticket_id,
                            f"[system] Zbyt wiele transient API errors ({_live_transient_count}x) na modelu {current_model}. "
                            f"Proces zatrzymany, przełączam na inny model.")
                    # Store kill reason in lock so the process-finished handler knows
                    try:
                        lock_data = get_executing() or {}
                        lock_data["killed_by_live_monitor"] = live_issue
                        with open(LOCK_FILE, "w") as f:
                            json.dump(lock_data, f)
                    except Exception:
                        pass
                    # Don't clear executing yet — let the normal "process finished" handler
                    # deal with retry/fallback on next loop iteration

            # --- Live health check: detect if ethos crashed during agent execution ---
            if active_proc is not None and active_proc.poll() is None:
                global _last_live_health_check, _live_health_failures
                now = time.time()
                if now - _last_live_health_check >= LIVE_HEALTH_CHECK_INTERVAL:
                    _last_live_health_check = now
                    healthy, detail = _check_ethos_health()
                    if not healthy:
                        _live_health_failures += 1
                        print(f"LIVE_HEALTH_FAIL | {active_ticket_id} | {detail} | failures={_live_health_failures}/3", flush=True)
                        if _live_health_failures >= 3:
                            print(f"LIVE_HEALTH_KILL | {active_ticket_id} | server down for 3 checks — killing agent and reverting", flush=True)
                            try:
                                active_proc.terminate()
                                try: active_proc.wait(timeout=5)
                                except subprocess.TimeoutExpired: active_proc.kill()
                            except OSError:
                                pass
                            if hasattr(active_proc, '_log_fh'):
                                try: active_proc._log_fh.close()
                                except: pass
                            reverted, revert_detail = _auto_revert_and_recover(active_ticket_id)
                            _lh_agent = (get_executing() or {}).get("agent", "copilot")
                            if reverted:
                                add_comment(active_ticket_id,
                                    f"[system] ⚠️ Serwer padł w trakcie pracy agenta. "
                                    f"Auto-revert: {revert_detail}. Ticket wraca do kolejki.")
                            else:
                                add_comment(active_ticket_id,
                                    f"[system] ❌ Serwer padł w trakcie pracy agenta. "
                                    f"Auto-revert nie powiódł się: {revert_detail}.")
                            try:
                                move_ticket(active_ticket_id, "Do zrobienia")
                            except Exception:
                                pass
                            clear_executing()
                            active_proc = None
                            active_ticket_id = None
                            _live_health_failures = 0
                    else:
                        if _live_health_failures > 0:
                            print(f"LIVE_HEALTH_RECOVERED | {active_ticket_id} | {detail} (was {_live_health_failures} failures)", flush=True)
                        _live_health_failures = 0

                        # --- Timeout check for DEV process ---
            if active_proc is not None and active_proc.poll() is None:
                executing = get_executing()
                if executing:
                    started = executing.get("started", 0)
                    ticket_ctx = getattr(active_proc, "_ticket_ctx", {})
                    complexity = ticket_ctx.get("ticket", {}).get("complexity", "medium") if ticket_ctx else "medium"
                    max_secs = MAX_EXECUTION_SECS.get(complexity, MAX_EXECUTION_SECS["medium"])
                    elapsed = time.time() - started
                    if elapsed > max_secs:
                        log_file = executing.get("log_file", "?")
                        print(f"\nTIMEOUT | {active_ticket_id} | {elapsed:.0f}s > {max_secs}s | killing PID {active_proc.pid}", flush=True)
                        try:
                            active_proc.terminate()
                            try: active_proc.wait(timeout=5)
                            except subprocess.TimeoutExpired: active_proc.kill()
                        except OSError:
                            pass
                        if hasattr(active_proc, '_log_fh'):
                            try: active_proc._log_fh.close()
                            except: pass
                        _to_agent = executing.get("agent", "copilot") if isinstance(executing, dict) else "copilot"
                        _timeout_counts[active_ticket_id] = _timeout_counts.get(active_ticket_id, 0) + 1
                        _tc = _timeout_counts[active_ticket_id]
                        if _tc >= MAX_TIMEOUT_RETRIES:
                            print(f"TIMEOUT_EXHAUSTED | {active_ticket_id} | {_tc}/{MAX_TIMEOUT_RETRIES} timeouts → shelving to Review", flush=True)
                            add_comment(active_ticket_id,
                                f"[{_to_agent}] Przekroczono limit czasu {_tc}x (po {max_secs}s każdy). "
                                f"Ticket wymaga interwencji manualnej. Log: {log_file}")
                            try:
                                move_ticket(active_ticket_id, "Review")
                            except Exception as me:
                                print(f"MOVE_ERROR | {active_ticket_id} | {me}", flush=True)
                        else:
                            print(f"TIMEOUT_RETRY | {active_ticket_id} | attempt {_tc}/{MAX_TIMEOUT_RETRIES} → back to queue", flush=True)
                            add_comment(active_ticket_id,
                                f"[{_to_agent}] Przekroczono limit czasu ({max_secs}s, próba {_tc}/{MAX_TIMEOUT_RETRIES}). "
                                f"Ticket wraca do kolejki. Log: {log_file}")
                            try:
                                move_ticket(active_ticket_id, "Do zrobienia")
                            except Exception as me:
                                print(f"MOVE_ERROR | {active_ticket_id} | {me}", flush=True)
                        clear_executing()
                        active_proc = None
                        active_ticket_id = None

            # --- Detect dead QA process (only check when DEV is idle) ---
            if active_proc is None and qa_proc is not None and qa_proc.poll() is None:
                if not pid_alive(qa_proc.pid):
                    print(f"\nDEAD_QA_PROCESS | {qa_ticket_id} | PID {qa_proc.pid} vanished", flush=True)
                    if hasattr(qa_proc, '_log_fh'):
                        try: qa_proc._log_fh.close()
                        except: pass
                    try:
                        move_ticket(qa_ticket_id, "Do zrobienia")
                        print(f"DEAD_QA_REQUEUE | {qa_ticket_id} | moved back to Do zrobienia", flush=True)
                    except Exception as me:
                        print(f"MOVE_ERROR | {qa_ticket_id} | {me}", flush=True)
                    qa_proc = None
                    qa_ticket_id = None

            # --- Check if QA Copilot process finished (only when DEV is idle) ---
            if active_proc is None and qa_proc is not None:
                retcode = qa_proc.poll()
                if retcode is not None:
                    if hasattr(qa_proc, '_log_fh'):
                        try: qa_proc._log_fh.close()
                        except: pass

                    log_file = qa_proc._log_file if hasattr(qa_proc, '_log_file') else "?"
                    verdict, reason = parse_qa_verdict(log_file) if log_file != "?" else ("error", "no log")

                    if verdict == "pass":
                        print(f"\nQA_PASS | {qa_ticket_id} | {reason}", flush=True)
                        add_comment(qa_ticket_id, f"[qa] ✅ QA PASSED: {reason}")
                        qa_cycles.pop(qa_ticket_id, None)
                        try:
                            move_ticket(qa_ticket_id, "Review")
                            print(f"MOVED_TO_REVIEW | {qa_ticket_id}", flush=True)
                        except Exception as me:
                            print(f"MOVE_ERROR | {qa_ticket_id} | {me}", flush=True)
                    elif verdict == "fail":
                        current_qa_cycle = qa_cycles.get(qa_ticket_id, 0) + 1
                        qa_cycles[qa_ticket_id] = current_qa_cycle

                        # Per-complexity max QA cycles
                        qa_ticket_data = next((t for t in queue if t["id"] == qa_ticket_id), None)
                        _qa_complexity = qa_ticket_data.get("complexity", "medium") if qa_ticket_data else "medium"
                        _max_cycles = QA_DEPTH.get(_qa_complexity, QA_DEPTH["medium"]).get("max_cycles", MAX_QA_CYCLES)

                        print(f"\nQA_FAIL | {qa_ticket_id} | cycle {current_qa_cycle}/{_max_cycles} | {reason}", flush=True)
                        add_comment(qa_ticket_id,
                            f"[qa] ❌ QA FAILED (cykl {current_qa_cycle}/{_max_cycles}): {reason}")

                        if current_qa_cycle < _max_cycles and active_proc is None:
                            # Move to W trakcie and launch rework agent
                            try:
                                move_ticket(qa_ticket_id, "W trakcie")
                                print(f"MOVED_TO_REWORK | {qa_ticket_id} | launching dev agent for fixes", flush=True)
                            except Exception as me:
                                print(f"MOVE_ERROR | {qa_ticket_id} | {me}", flush=True)

                            # Find full ticket data for rework
                            if qa_ticket_data:
                                active_ticket_id, active_proc = auto_rework_ticket(
                                    qa_ticket_data, reason, current_qa_cycle)
                            else:
                                print(f"REWORK_SKIP | {qa_ticket_id} | ticket data not found in queue", flush=True)
                        else:
                            # Max cycles reached or dev agent busy — back to Do zrobienia
                            if current_qa_cycle >= _max_cycles:
                                add_comment(qa_ticket_id,
                                    f"[system] Osiągnięto limit cykli QA ({_max_cycles}). "
                                    f"Ticket wymaga interwencji manualnej.")
                                print(f"QA_MAX_CYCLES | {qa_ticket_id} | {_max_cycles} cycles exhausted", flush=True)
                            try:
                                move_ticket(qa_ticket_id, "Do zrobienia")
                                print(f"MOVED_TO_TODO | {qa_ticket_id} (QA failed, needs manual rework)", flush=True)
                            except Exception as me:
                                print(f"MOVE_ERROR | {qa_ticket_id} | {me}", flush=True)
                    else:
                        print(f"\nQA_UNKNOWN | {qa_ticket_id} | exit={retcode} | verdict={verdict} | {reason}", flush=True)
                        add_comment(qa_ticket_id,
                            f"[qa] ⚠️ QA verdict unclear (exit {retcode}). Log: {log_file}. Moving to Review for manual check.")
                        try:
                            move_ticket(qa_ticket_id, "Review")
                            print(f"MOVED_TO_REVIEW | {qa_ticket_id} (manual QA needed)", flush=True)
                        except Exception as me:
                            print(f"MOVE_ERROR | {qa_ticket_id} | {me}", flush=True)

                    qa_proc = None
                    qa_ticket_id = None
                    _last_ticket_finished = time.time()

            # --- Poll queue (with API outage protection) ---
            try:
                data = poll_queue()
                queue = data.get("queue", [])
                if api_fail_count > 0:
                    print(f"API_RECOVERED | after {api_fail_count} failure(s)", flush=True)
                api_fail_count = 0  # reset on success
            except Exception as poll_err:
                api_fail_count += 1
                backoff = min(args.interval * (2 ** (api_fail_count - 1)), 120)
                if api_fail_count <= API_FAIL_THRESHOLD:
                    print(f"API_RETRY | attempt {api_fail_count}/{API_FAIL_THRESHOLD} | backoff {backoff}s | {poll_err}", flush=True)
                elif api_fail_count == API_FAIL_THRESHOLD + 1:
                    print(f"API_OUTAGE | backend unreachable, preserving active processes | {poll_err}", flush=True)
                try: login()
                except: pass
                _interruptible_sleep(backoff)
                continue  # skip queue processing — don't touch prev_state or active procs

            current_state = {t["id"]: t["column"] for t in queue}

            todo = [t for t in queue if t["column"] == "Do zrobienia"]
            in_progress = [t for t in queue if t["column"] == "W trakcie"]
            qa_tickets = [t for t in queue if t["column"] == "QA"]

            todo_local = [t for t in todo if t.get("agent") == "localai"]
            todo_free = [t for t in todo if t.get("agent") == "freemodel"]
            todo = [t for t in todo if t.get("agent", "copilot") not in ("localai", "freemodel")]

            # Lightweight local agent (plan + patch hints) — runs only when no other execution is active
            if args.auto and todo_local and not is_executing() and not local_busy and active_proc is None and qa_proc is None:
                local_busy = True
                try:
                    process_local_ticket(todo_local[0])
                    _last_ticket_finished = time.time()
                except Exception as local_err:
                    print(f"LOCAL_TICKET_ERROR | {todo_local[0].get('id','?')} | {local_err}", flush=True)
                    clear_executing()
                finally:
                    local_busy = False

            # Free-model agent — uses Copilot CLI with free/low-cost models
            if args.auto and todo_free and not is_executing() and active_proc is None and qa_proc is None and not local_busy:
                next_free = todo_free[0]
                # Global attempt cap for free-model tickets too
                if _is_ticket_shelved(next_free["id"]):
                    print(f"SHELVED_FREE | {next_free['id']} | max attempts reached", flush=True)
                    try:
                        add_comment(next_free["id"], f"[system] Ticket automatycznie odłożony po {MAX_TOTAL_ATTEMPTS} próbach.")
                        move_ticket(next_free["id"], "Review")
                    except Exception:
                        pass
                else:
                    _record_attempt(next_free["id"])
                    complexity = next_free.get("complexity", "medium")
                    model_info = FREE_MODEL_MAP.get(complexity, FREE_MODEL_MAP["medium"])
                    agent = detect_agent(next_free["title"], next_free.get("labels", []))
                    info = AGENT_MAP.get(agent, AGENT_MAP["General"])
                    assign_ticket(next_free["id"], "freemodel")
                    move_ticket(next_free["id"], "W trakcie")
                    set_executing(next_free["id"], model_info, agent="freemodel")
                    docs_context = load_docs_context(agent)
                    print(f"\n{'='*70}", flush=True)
                    print(f"FREE_EXECUTE | {next_free['id']} | {agent} | {model_info['model']} ({model_info['label']})", flush=True)
                    print(f"Title: {next_free['title']}", flush=True)
                    print(f"{'='*70}", flush=True)
                    add_comment(next_free["id"],
                        f"[freemodel] Rozpoczynam prace nad ticketem.\n"
                        f"Agent: {agent} | Model: {model_info['model']} ({model_info['label']})\n"
                        f"Złożoność: {complexity}")
                    active_proc = execute_via_copilot(next_free, agent, info, model_info, docs_context)
                    if active_proc:
                        active_ticket_id = next_free["id"]
                    else:
                        print(f"FREE_LAUNCH_FAILED | {next_free['id']}", flush=True)
                        clear_executing()
                        try: move_ticket(next_free["id"], "Do zrobienia")
                        except: pass

            for t in queue:
                tid, col = t["id"], t["column"]
                prev_col = prev_state.get(tid)
                agent = detect_agent(t["title"], t.get("labels", []))
                complexity = t.get("complexity", "medium")
                model = COMPLEXITY_MODEL_MAP.get(complexity, COMPLEXITY_MODEL_MAP["medium"])

                if prev_col is None and col == "Do zrobienia":
                    print(f"\nACTION_NEEDED | {tid} | {t['priority'].upper()} | {agent} | {complexity}→{model['label']} | {t['title']}", flush=True)
                    lc = t.get("last_comment")
                    if lc:
                        print(f"  comment [{lc['author']}]: {lc['text'][:150]}", flush=True)

                elif prev_col and prev_col != col and col == "Do zrobienia":
                    print(f"\nREWORK | {tid} | {agent} | {complexity}→{model['label']} | {t['title']}", flush=True)
                    print(f"  moved: {prev_col} -> {col}", flush=True)
                    lc = t.get("last_comment")
                    if lc:
                        print(f"  feedback [{lc['author']}]: {lc['text'][:150]}", flush=True)

                elif prev_col and prev_col != col:
                    print(f"\nMOVED | {tid} | {prev_col} -> {col} | {t['title']}", flush=True)

            removed = set(prev_state.keys()) - set(current_state.keys())
            for rid in removed:
                print(f"DONE | {rid} removed from queue", flush=True)

            # --- Stranded ticket recovery: detect tickets stuck in "W trakcie" with no active process ---
            if in_progress and active_proc is None and not is_executing():
                for stale in in_progress:
                    stale_id = stale['id']
                    _stranded_counts[stale_id] = _stranded_counts.get(stale_id, 0) + 1
                    stranded_n = _stranded_counts[stale_id]
                    if _is_ticket_shelved(stale_id) or stranded_n >= MAX_STRANDED_REQUEUES:
                        reason = (f"max attempts ({MAX_TOTAL_ATTEMPTS})" if _is_ticket_shelved(stale_id)
                                  else f"stranded {stranded_n}x consecutively (limit {MAX_STRANDED_REQUEUES})")
                        print(f"SHELVED_STRANDED | {stale_id} | {reason} — moving to Review", flush=True)
                        try:
                            add_comment(stale_id,
                                f"[system] Ticket odłożony — {reason}. "
                                f"Wykryto pętlę: ticket wielokrotnie utykał w 'W trakcie' bez aktywnego procesu. "
                                f"Wymaga interwencji manualnej.")
                            move_ticket(stale_id, "Review")
                            _stranded_counts.pop(stale_id, None)
                        except Exception as me:
                            print(f"MOVE_ERROR | {stale_id} | {me}", flush=True)
                    else:
                        print(f"STRANDED | {stale_id} | detection {stranded_n}/{MAX_STRANDED_REQUEUES} — requeuing", flush=True)
                        try:
                            move_ticket(stale_id, "Do zrobienia")
                        except Exception as me:
                            print(f"MOVE_ERROR | {stale_id} | {me}", flush=True)
            else:
                # Reset stranded counters for tickets no longer stuck
                for tid in list(_stranded_counts.keys()):
                    if not any(t['id'] == tid for t in in_progress):
                        _stranded_counts.pop(tid, None)

            # --- AUTO MODE: pick and start DEV ticket ---
            # Don't start a new ticket if anything is in "W trakcie" or "QA"
            # Only 1 ticket may be processed at a time (dev OR qa, never both)
            if args.auto and todo and not is_executing() and active_proc is None and not in_progress and not qa_tickets and qa_proc is None:
                # Inter-ticket pacing: check if the model we'd use is in cooldown
                prio_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                todo.sort(key=lambda t: prio_order.get(t["priority"], 99))
                next_ticket = todo[0]
                planned_model = select_model(next_ticket).get("model")
                wait_secs = _should_wait_before_next_ticket(planned_model)
                if wait_secs > 0:
                    print(f"PACING | waiting {wait_secs:.0f}s before starting next ticket (model={planned_model})", flush=True)
                    _interruptible_sleep(wait_secs)
                active_ticket_id, active_proc = auto_start_ticket(next_ticket)

            # --- AUTO MODE: pick and start QA ticket (only when DEV is idle) ---
            # Strict sequencing: never run QA alongside DEV to avoid git conflicts
            if args.auto and active_proc is None and qa_tickets and qa_proc is None:
                # Inter-ticket pacing for QA — check against QA model
                qa_model_info = _select_qa_model()
                planned_qa_model = qa_model_info.get("model") if qa_model_info else None
                wait_secs = _should_wait_before_next_ticket(planned_qa_model)
                if wait_secs > 0:
                    print(f"QA_PACING | waiting {wait_secs:.0f}s before starting QA (model={planned_qa_model})", flush=True)
                    _interruptible_sleep(wait_secs)
                qa_candidate = qa_tickets[0]
                if qa_candidate["id"] != active_ticket_id:
                    qa_ticket_id = qa_candidate["id"]
                    qa_proc = run_qa_check(qa_candidate)
                    if qa_proc is None:
                        qa_ticket_id = None

            # --- Check if executing ticket was moved out of queue manually ---
            executing = get_executing()
            if executing and executing["ticket_id"] not in current_state:
                print(f"COMPLETED | {executing['ticket_id']} left queue (manual)", flush=True)
                if active_proc and active_proc.poll() is None:
                    pid = active_proc.pid
                    active_proc.terminate()
                    try: active_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: active_proc.kill()
                    if hasattr(active_proc, '_log_fh'):
                        try: active_proc._log_fh.close()
                        except: pass
                    print(f"COPILOT_TERMINATED | PID={pid} (ticket removed from queue)", flush=True)
                clear_executing()
                active_proc = None
                active_ticket_id = None

            # Check if QA ticket was moved out manually
            if qa_ticket_id and qa_ticket_id not in current_state:
                print(f"QA_CANCELLED | {qa_ticket_id} left queue (manual)", flush=True)
                if qa_proc and qa_proc.poll() is None:
                    pid = qa_proc.pid
                    qa_proc.terminate()
                    try: qa_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: qa_proc.kill()
                    if hasattr(qa_proc, '_log_fh'):
                        try: qa_proc._log_fh.close()
                        except: pass
                    print(f"QA_TERMINATED | PID={pid}", flush=True)
                qa_proc = None
                qa_ticket_id = None

            prev_state = current_state

        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            try: login()
            except: pass

        # Adaptive polling: faster when busy, slower when idle
        is_busy = active_proc is not None or qa_proc is not None or bool(todo)
        _interruptible_sleep(POLL_ACTIVE if is_busy else POLL_IDLE)


if __name__ == "__main__":
    main()
