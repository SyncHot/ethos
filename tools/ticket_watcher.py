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

import sys, time, json, os, argparse, requests, urllib3, subprocess, shlex, signal, threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Codebase map generator — dynamic architecture reference for AI agents
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codebase_map import generate as generate_codebase_map

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

# ── Complexity → AI Model mapping ────────────────────────────────────────
# Maps to Copilot CLI --model flag values
COMPLEXITY_MODEL_MAP = {
    "complex": {
        "model": "gpt-5.1-codex-max",
        "label": "GPT-5.1 Codex Max (Agent Mode)",
        "reason": "Deep repo-wide reasoning and architectural autonomy",
    },
    "medium": {
        "model": "gemini-3-pro-preview",
        "label": "Gemini 3 Pro (Reliability)",
        "reason": "Best handling of system tools and QA protocols",
    },
    "simple": {
        "model": "gpt-5.1-codex-mini",
        "label": "Codex Mini (Efficiency)",
        "reason": "Fast and cheap for straightforward logic",
    },
}

# ── Model routing: ordered fallback chains per complexity ─────────────────
MODEL_ROUTING = {
    "complex": ["gpt-5.1-codex-max", "claude-opus-4.6", "gemini-3-pro-preview"],
    "medium":  ["gemini-3-pro-preview", "claude-sonnet-4.6", "gpt-5.1-codex-mini"],
    "simple":  ["gpt-5.1-codex-mini", "gpt-5-mini", "gpt-4.1"],
}

# All available Copilot CLI models, ranked by capability (best first).
# Used as last-resort fallback pool when the routing chain is exhausted.
ALL_AVAILABLE_MODELS = [
    "gpt-5.1-codex-max",
    "claude-opus-4.6",
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

MAX_RETRIES_SAME_MODEL = 2      # retry same model N times with backoff before switching
MAX_BACKOFF_SECS = 180          # cap exponential backoff at 3 min
BACKOFF_BASE_SECS = 15          # starting backoff for rate limits (15→30→60→120→180)
INTER_TICKET_DELAY = 5          # base gap between tickets (only cosmetic, real protection is per-model cooldown)
MODEL_COOLDOWN_SECS = 120      # don't retry a model that got 429 for 2 min (providers reset in ~60s)
LIVE_LOG_CHECK_INTERVAL = 10   # seconds between checking running process log for rate limits
QA_RATE_LIMIT_COOLDOWN = 30    # extra pause only if QA would use same provider that just got 429
TRANSIENT_ERROR_KILL_THRESHOLD = 5  # kill process after N transient API errors (they waste autopilot turns)
TRANSIENT_SOFT_COOLDOWN_THRESHOLD = 3  # after exit 0, if this many transient errors → soft cooldown on model

# ── Global model cooldown tracker ─────────────────────────────────────────
# {model_name: timestamp_when_cooldown_expires}
_model_cooldowns = {}

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
    username = os.environ.get("ETHOS_COPILOT_USER", "marcin")
    password = os.environ.get("ETHOS_COPILOT_PASS", "pluton2303")
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

def set_executing(tid, model_info=None, retry_count=0, tried_models=None, qa_cycle=0):
    with open(LOCK_FILE, "w") as f:
        json.dump({
            "ticket_id": tid,
            "started": time.time(),
            "model": model_info,
            "retry_count": retry_count,
            "tried_models": tried_models or [],
            "qa_cycle": qa_cycle,
        }, f)

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
                add_comment(tid, "[copilot] Watcher zrestartowany — ticket wraca do kolejki do ponownego wykonania.")
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


def _classify_failure(log_file):
    """Classify failure type from log output. Returns 'rate_limit', 'server_error', 'transient', or 'unknown'."""
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

def select_model(ticket, tried_models=None):
    """Select AI model based on ticket complexity, respecting cooldowns."""
    complexity = ticket.get("complexity", "medium")
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
_codebase_map_cache = ""
_codebase_map_time = 0

def get_codebase_map():
    """Get the codebase map, regenerating if cache is stale (>10min)."""
    global _codebase_map_cache, _codebase_map_time
    now = time.time()
    if now - _codebase_map_time > _DOCS_CACHE_TTL or not _codebase_map_cache:
        try:
            _codebase_map_cache = generate_codebase_map()
            _codebase_map_time = now
        except Exception as e:
            print(f"CODEBASE_MAP_ERROR | {e}", flush=True)
            _codebase_map_cache = ""
    return _codebase_map_cache

def load_docs_context(agent):
    """Load and return concatenated content of docs relevant to the agent type. Cached."""
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
                entry = f"--- {doc_name} ---\n{content}"
                _docs_cache[doc_name] = entry
                context_parts.append(entry)
            except Exception:
                pass

    return "\n\n".join(context_parts)

# ── Execute ticket via Copilot CLI ────────────────────────────────────────

COPILOT_BIN = "/home/marcin/.local/bin/copilot"
COPILOT_LOG_DIR = "/opt/ethos/logs/copilot_tickets"
MAX_AUTOPILOT = {
    "complex": 25,
    "medium": 15,
    "simple": 8,
}
MAX_EXECUTION_SECS = {
    "complex": 2400,   # 40 min
    "medium": 1200,    # 20 min
    "simple": 600,     # 10 min
}
MAX_QA_CYCLES = 3  # max dev→QA round-trips before giving up

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


# ── QA model selection with fallback ─────────────────────────────────────
QA_MODEL_CHAIN = ["claude-sonnet-4.6", "gemini-3-pro-preview", "gpt-5.1-codex-mini"]

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
    """Launch Copilot CLI as QA agent with fallback model selection."""
    tid = ticket["id"]
    model_info = _select_qa_model()
    model = model_info["model"]

    os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
    log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_qa_{int(time.time())}.log")

    prompt = build_qa_prompt(ticket)

    cmd = [
        COPILOT_BIN,
        "-p", prompt,
        "--model", model,
        "--autopilot",
        "--allow-all",
        "--max-autopilot-continues", str(15),
    ]

    print(f"QA_START | {tid} | model={model} | log={log_file}", flush=True)

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

    # Generate dynamic codebase map so agent knows where everything is
    codebase_map = get_codebase_map()

    map_section = ""
    if codebase_map:
        map_section = f"\nCODEBASE MAP (use this to locate files — do NOT explore from scratch):\n{codebase_map}\n"

    prompt = f"""You are an EthOS {agent} agent. Execute this ticket efficiently.

TICKET: {tid}
Title: {title}
{f'Description: {desc}' if desc else ''}
{feedback}

PROJECT: /opt/ethos/ (Flask backend + vanilla JS frontend)
{map_section}REFERENCE DOCS (consult only when relevant, do NOT read everything):
{doc_list}

WORKFLOW:
1. Use the codebase map above to locate the relevant files — open them directly
2. Implement the solution
3. Test if possible (restart ethos if backend changes):
   - First log the restart: curl -s -X POST http://localhost:9000/api/eventlog -H 'Content-Type: application/json' -d '{{"category":"system","level":"warning","message":"Restart ethos z ticket watchera","detail":{{"ticket_id":"{tid}","reason":"backend changes"}}}}'
   - Run preflight before restart: python3 /opt/ethos/tools/preflight_check.py
   - If preflight passed, restart: sudo systemctl restart ethos
   - If preflight FAILED, fix the errors before restarting
4. Commit: git add <files> && git commit -m "[{tid}] <description>"
5. Push: sudo -u marcin git push

Be focused and efficient. Do not read docs that aren't relevant to the task."""

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
PROJECT: /opt/ethos/ (Flask backend + vanilla JS frontend)
REFERENCE DOCS (consult only when relevant):
{doc_list}

WORKFLOW:
1. Read the QA failure reason and full QA analysis above — they tell you EXACTLY what's wrong
2. The diff above shows your previous changes — locate the specific issues QA reported
3. Fix ONLY the issues identified by QA — do not restructure or rewrite working code
4. Test if possible (restart ethos if backend changes):
   - First log the restart: curl -s -X POST http://localhost:9000/api/eventlog -H 'Content-Type: application/json' -d '{{"category":"system","level":"warning","message":"Restart ethos z ticket watchera","detail":{{"ticket_id":"{tid}","reason":"backend changes"}}}}'
   - Run preflight before restart: python3 /opt/ethos/tools/preflight_check.py
   - If preflight passed, restart: sudo systemctl restart ethos
   - If preflight FAILED, fix the errors before restarting
5. Commit: git add <files> && git commit -m "[{tid}] fix: QA cycle {qa_cycle} — <description>"
6. Push: sudo -u marcin git push

IMPORTANT: You have the diff and QA analysis above. Go directly to fixing the issues.
Do NOT explore the codebase from scratch — start from the specific files mentioned in the QA feedback.
The ticket will go through QA again after your fixes."""

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


def build_retry_prompt(ticket, agent, info, model_info, docs_context, prev_model, failure_type):
    """Build a prompt for retry/failover that includes rich context from the previous attempt."""
    base_prompt = build_copilot_prompt(ticket, agent, info, model_info, docs_context)
    tid = ticket["id"]

    context_parts = []

    # Git state: commits + uncommitted work
    git_context = _get_recent_git_context(tid)
    if git_context:
        context_parts.append(git_context)

    # Previous agent log summary
    log_summary = _get_previous_log_summary(tid)
    if log_summary:
        context_parts.append(log_summary)

    if context_parts:
        return base_prompt + "\n".join(context_parts)

    # Fallback: at least mention there was a prior attempt
    return base_prompt + (
        f"\n\nNOTE: A previous attempt with model {prev_model} was interrupted ({failure_type}). "
        f"Run: git --no-pager log --oneline -10 and git status to see if any partial work exists."
    )


def execute_via_copilot(ticket, agent, info, model_info, docs_context, override_prompt=None):
    """Launch Copilot CLI in non-interactive mode to solve the ticket."""
    tid = ticket["id"]
    model = model_info["model"]

    # Ensure log dir exists
    os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
    log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_{int(time.time())}.log")
    prompt_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_{int(time.time())}_prompt.txt")

    prompt = override_prompt or build_copilot_prompt(ticket, agent, info, model_info, docs_context)

    # Write prompt to temp file to avoid shell escaping issues
    with open(prompt_file, "w") as pf:
        pf.write(prompt)

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

        # Store PID in lock for monitoring (preserve qa_cycle from set_executing)
        existing = get_executing() or {}
        with open(LOCK_FILE, "w") as f:
            json.dump({
                "ticket_id": tid,
                "started": time.time(),
                "model": model_info,
                "copilot_pid": proc.pid,
                "log_file": log_file,
                "qa_cycle": existing.get("qa_cycle", 0),
            }, f)

        print(f"COPILOT_RUNNING | {tid} | PID={proc.pid}", flush=True)
        return proc

    except Exception as e:
        print(f"COPILOT_ERROR | {tid} | {e}", flush=True)
        add_comment(tid, f"[copilot] Błąd uruchamiania Copilot: {e}")
        return None


# ── Auto-execute ─────────────────────────────────────────────────────────

def auto_start_ticket(ticket):
    """Start ticket: assign, move, select model by complexity, launch Copilot CLI."""
    tid = ticket["id"]
    title = ticket["title"]
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
    model_info = select_model(ticket)
    complexity = ticket.get("complexity", "medium")
    docs_context = load_docs_context(agent)

    set_executing(tid, model_info, qa_cycle=qa_cycle)

    print(f"\n{'='*70}", flush=True)
    print(f"REWORK | {tid} | {agent} | QA cycle {qa_cycle}/{MAX_QA_CYCLES}", flush=True)
    print(f"Title: {title}", flush=True)
    print(f"QA reason: {qa_reason[:200]}", flush=True)
    print(f"Model: {model_info['model']} ({model_info['label']})", flush=True)
    print(f"{'='*70}", flush=True)

    add_comment(tid,
        f"[copilot] Rozpoczynam poprawki po QA (cykl {qa_cycle}/{MAX_QA_CYCLES}).\n"
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
    print(f"QA agent: Sonnet | Flow: Dev→QA→Review (fail→Rework→QA, max {MAX_QA_CYCLES} cycles)", flush=True)
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
                    add_comment(active_ticket_id,
                        f"[copilot] Proces Copilot zniknął nieoczekiwanie (PID {active_proc.pid}). Log: {log_file}")
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
                                f"[copilot] Zakończyłem pracę (exit 0), ale z {transient_count} transient API errors. "
                                f"Model {_done_model} w soft-cooldownie ({soft_cd}s). Log: {log_file}")
                        else:
                            print(f"\nCOPILOT_DONE | {active_ticket_id} | exit=0 | qa_cycle={qa_cycle_for_ticket} | log={log_file}", flush=True)
                            _record_metric(_done_model, "ok", _exec_duration)
                            add_comment(active_ticket_id,
                                f"[copilot] Zakończyłem pracę nad ticketem (exit 0). Log: {log_file}")
                        # Move to QA (not Review — QA agent will verify first)
                        try:
                            move_ticket(active_ticket_id, "QA")
                            print(f"MOVED_TO_QA | {active_ticket_id}", flush=True)
                        except Exception as me:
                            print(f"MOVE_ERROR | {active_ticket_id} | {me}", flush=True)
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
                            f"[copilot] Copilot zakończył z błędem (exit {retcode}, {failure_type}). "
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
                        add_comment(active_ticket_id,
                            f"[copilot] Przekroczono limit czasu ({max_secs}s). Ticket wraca do kolejki. Log: {log_file}")
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

                        print(f"\nQA_FAIL | {qa_ticket_id} | cycle {current_qa_cycle}/{MAX_QA_CYCLES} | {reason}", flush=True)
                        add_comment(qa_ticket_id,
                            f"[qa] ❌ QA FAILED (cykl {current_qa_cycle}/{MAX_QA_CYCLES}): {reason}")

                        if current_qa_cycle < MAX_QA_CYCLES and active_proc is None:
                            # Move to W trakcie and launch rework agent
                            try:
                                move_ticket(qa_ticket_id, "W trakcie")
                                print(f"MOVED_TO_REWORK | {qa_ticket_id} | launching dev agent for fixes", flush=True)
                            except Exception as me:
                                print(f"MOVE_ERROR | {qa_ticket_id} | {me}", flush=True)

                            # Find full ticket data for rework
                            qa_ticket_data = next((t for t in queue if t["id"] == qa_ticket_id), None)
                            if qa_ticket_data:
                                active_ticket_id, active_proc = auto_rework_ticket(
                                    qa_ticket_data, reason, current_qa_cycle)
                            else:
                                print(f"REWORK_SKIP | {qa_ticket_id} | ticket data not found in queue", flush=True)
                        else:
                            # Max cycles reached or dev agent busy — back to Do zrobienia
                            if current_qa_cycle >= MAX_QA_CYCLES:
                                add_comment(qa_ticket_id,
                                    f"[system] Osiągnięto limit cykli QA ({MAX_QA_CYCLES}). "
                                    f"Ticket wymaga interwencji manualnej.")
                                print(f"QA_MAX_CYCLES | {qa_ticket_id} | {MAX_QA_CYCLES} cycles exhausted", flush=True)
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
