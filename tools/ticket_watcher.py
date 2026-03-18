#!/usr/bin/env python3
"""
EthOS Ticket Watcher — polls copilot queue and auto-processes tickets.

Usage:
    python3 ticket_watcher.py [--interval 15] [--auto]

With --auto: picks highest priority ticket, assigns to copilot, moves to W trakcie,
outputs EXECUTE instruction with model selection based on ticket complexity.

Complexity → Model mapping:
    complex  → claude-opus-4-20250514 (premium, deep reasoning)
    medium   → claude-sonnet-4-20250514 (balanced quality/speed)
    simple   → claude-haiku-4-20250514 (fast, cost-effective)
"""

import sys, time, json, os, argparse, requests, urllib3, subprocess, shlex, signal
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "http://localhost:9000/api"
TOKEN_FILE = "/tmp/.ethos_orchestrator_token"
LOCK_FILE = "/tmp/.ethos_watcher_executing"
DOCS_DIR = "/opt/ethos/docs"
API_FAIL_THRESHOLD = 3  # consecutive API failures before considering outage

# ── Complexity → AI Model mapping ────────────────────────────────────────
# Maps to Copilot CLI --model flag values
COMPLEXITY_MODEL_MAP = {
    "complex": {
        "model": "gpt-5.1-codex-max",
        "label": "GPT-5.1 Codex Max (Agent Mode)",
        "reason": "Deep repo-wide reasoning and architectural autonomy",
    },
    "medium": {
        "model": "gemini-3.1-pro",
        "label": "Gemini 3.1 Pro (Reliability)",
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
    "complex": ["gpt-5.1-codex-max", "claude-opus-4.6", "gemini-3.1-pro"],
    "medium":  ["gemini-3.1-pro", "claude-sonnet-4.6", "gpt-5.1-codex-mini"],
    "simple":  ["gpt-5.1-codex-mini", "gemini-3-flash", "grok-code-fast"],
}

RATE_LIMIT_MARKERS = [
    "rate limit",
    "rate-limit",
    "too many requests",
    "429",
    "quota exceeded",
    "usage limit",
    "try again later",
]

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
    r = requests.post(f"{BASE}/auth/login",
                      json={"username": "marcin", "password": "pluton2303"}, verify=False)
    r.raise_for_status()
    token = r.json().get("token", "")
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token

def get_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t: return t
    return login()

def headers():
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}

_last_auth_refresh = 0
_AUTH_REFRESH_COOLDOWN = 300  # seconds between empty-queue auth retries

def api(method, path, body=None):
    global _last_auth_refresh
    fn = requests.get if method == "GET" else (requests.post if method == "POST" else requests.put)
    kw = {"headers": headers(), "verify": False}
    if body: kw["json"] = body
    r = fn(f"{BASE}{path}", **kw)
    if r.status_code == 401:
        login()
        kw["headers"] = headers()
        r = fn(f"{BASE}{path}", **kw)
    r.raise_for_status()
    data = r.json()
    # Detect silent auth failure: retry only if cooldown has elapsed
    if (path == "/tickets/copilot/queue" and data.get("total", -1) == 0
            and time.time() - _last_auth_refresh > _AUTH_REFRESH_COOLDOWN):
        _last_auth_refresh = time.time()
        login()
        kw["headers"] = headers()
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

def set_executing(tid, model_info=None):
    with open(LOCK_FILE, "w") as f:
        json.dump({
            "ticket_id": tid,
            "started": time.time(),
            "model": model_info,
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
    """Best-effort detection of provider/API rate limit from Copilot log output."""
    if not log_file or log_file == "?" or not os.path.isfile(log_file):
        return False
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()[-20000:].lower()
        return any(marker in content for marker in RATE_LIMIT_MARKERS)
    except Exception:
        return False


def _should_fallback_from_sonnet(model_info, log_file):
    """Check if the current model failed due to rate limit and a fallback exists."""
    if not isinstance(model_info, dict):
        return False
    return _log_indicates_rate_limit(log_file)


def _get_next_fallback(model_info, complexity):
    """Return the next model in the routing chain, or None if exhausted."""
    if not isinstance(model_info, dict):
        return None
    current = model_info.get("model", "")
    chain = MODEL_ROUTING.get(complexity, MODEL_ROUTING.get("medium", []))
    try:
        idx = chain.index(current)
    except ValueError:
        idx = -1
    next_idx = idx + 1
    if next_idx >= len(chain):
        return None
    next_model = chain[next_idx]
    return {
        "model": next_model,
        "label": f"{next_model} (fallback #{next_idx})",
        "reason": f"Fallback from {current} (rate limit / failure)",
    }

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

def select_model(ticket):
    """Select AI model based on ticket complexity; fall back to priority if missing."""
    complexity = ticket.get("complexity", "medium")
    if complexity not in COMPLEXITY_MODEL_MAP:
        complexity = "medium"
    return COMPLEXITY_MODEL_MAP[complexity]

# ── Load relevant docs for agent ─────────────────────────────────────────

def load_docs_context(agent):
    """Load and return concatenated content of docs relevant to the agent type."""
    info = AGENT_MAP.get(agent, AGENT_MAP["General"])
    doc_files = info.get("docs", [])
    context_parts = []

    for doc_name in doc_files:
        doc_path = os.path.join(DOCS_DIR, doc_name)
        if os.path.exists(doc_path):
            try:
                with open(doc_path, "r", encoding="utf-8") as f:
                    content = f.read()
                context_parts.append(f"--- {doc_name} ---\n{content}")
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

def build_qa_prompt(ticket):
    """Build a QA review prompt — Copilot checks if implementation meets requirements and docs."""
    tid = ticket["id"]
    title = ticket["title"]
    desc = ticket.get("description", "")
    comments = []
    # Gather all comments for context (implementation notes)
    lc = ticket.get("last_comment")
    if lc:
        comments.append(lc["text"])

    prompt = f"""You are an EthOS QA Expert agent. Your job is to verify that the ticket was implemented correctly.

TICKET: {tid}
Title: {title}
{f'Description: {desc}' if desc else ''}
{f'Latest comment: {lc["text"][:500]}' if lc else ''}

YOUR TASK:
1. Read the ticket requirements (title + description above)
2. Read the relevant EthOS docs to understand standards:
   - /opt/ethos/docs/DEV_STANDARDS.md
   - /opt/ethos/docs/QA_FAILOVER_PROTOCOLS.md
   - /opt/ethos/docs/BE_LESSONS_LEARNED.md
   - /opt/ethos/docs/FE_LESSONS_LEARNED.md
   - /opt/ethos/docs/UX_LESSONS_LEARNED.md
3. Check recent git commits to see what was changed: git --no-pager log --oneline -10
4. Review the changed files and verify they meet the requirements
5. Test the functionality if possible (curl API endpoints, check if server responds, etc.)
6. Check if the code follows EthOS coding standards from the docs

VERDICT — you MUST output exactly one of these lines at the END of your response:
  QA_PASS: <brief reason why it passes>
  QA_FAIL: <specific issues found that need fixing>

Be strict but fair. Check for real issues, not style nitpicks.
Focus on: correctness, requirements met, docs compliance, no regressions."""

    return prompt


def run_qa_check(ticket):
    """Launch Copilot CLI as QA agent (always Sonnet) to verify the ticket."""
    tid = ticket["id"]
    model = "claude-sonnet-4.6"

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

    prompt = f"""You are an EthOS {agent} agent. Execute this ticket efficiently.

TICKET: {tid}
Title: {title}
{f'Description: {desc}' if desc else ''}
{feedback}

PROJECT: /opt/ethos/ (Flask backend + vanilla JS frontend)
REFERENCE DOCS (consult only when relevant, do NOT read everything):
{doc_list}

WORKFLOW:
1. Understand what needs to change — explore the relevant source files
2. Implement the solution
3. Test if possible (restart ethos if backend changes: sudo systemctl restart ethos)
4. Commit: git add <files> && git commit -m "[{tid}] <description>"
5. Push: sudo -u marcin git push

Be focused and efficient. Do not read docs that aren't relevant to the task."""

    return prompt


def execute_via_copilot(ticket, agent, info, model_info, docs_context):
    """Launch Copilot CLI in non-interactive mode to solve the ticket."""
    tid = ticket["id"]
    model = model_info["model"]

    # Ensure log dir exists
    os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
    log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_{int(time.time())}.log")
    prompt_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_{int(time.time())}_prompt.txt")

    prompt = build_copilot_prompt(ticket, agent, info, model_info, docs_context)

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

        # Store PID in lock for monitoring
        with open(LOCK_FILE, "w") as f:
            json.dump({
                "ticket_id": tid,
                "started": time.time(),
                "model": model_info,
                "copilot_pid": proc.pid,
                "log_file": log_file,
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

# ── Main loop ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    parser.add_argument("--auto", action="store_true", help="Auto-start tickets from Do zrobienia")
    args = parser.parse_args()

    mode = "AUTO" if args.auto else "WATCH"
    print(f"Ticket Watcher | interval={args.interval}s | mode={mode}", flush=True)
    print(f"Monitoring copilot-enabled projects via /copilot/queue", flush=True)
    print(f"Model mapping: complex→{COMPLEXITY_MODEL_MAP['complex']['model']}, medium→{COMPLEXITY_MODEL_MAP['medium']['model']}, simple→{COMPLEXITY_MODEL_MAP['simple']['model']}", flush=True)
    routing_str = ", ".join(f"{k}: {' → '.join(v)}" for k, v in MODEL_ROUTING.items())
    print(f"Fallback routing: {routing_str}", flush=True)
    print(f"QA agent: Sonnet | Flow: Dev→QA→Review (fail→Do zrobienia)", flush=True)
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

    while True:
        try:
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
                        print(f"\nCOPILOT_DONE | {active_ticket_id} | exit=0 | log={log_file}", flush=True)
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

                        if _should_fallback_from_sonnet(model_info, log_file):
                            ctx = getattr(active_proc, "_ticket_ctx", None)
                            complexity = ctx["ticket"].get("complexity", "medium") if ctx else "medium"
                            fallback = _get_next_fallback(model_info, complexity)
                            if fallback:
                                print(
                                    f"\nRATE_LIMIT_FALLBACK | {active_ticket_id} | "
                                    f"{model_info.get('model', '?')} -> {fallback['model']}",
                                    flush=True,
                                )
                                add_comment(
                                    active_ticket_id,
                                    f"[copilot] Rate limit / błąd modelu {model_info.get('model', '?')}. "
                                    f"Automatyczny retry na {fallback['model']}.",
                                )
                                if ctx:
                                    retry_proc = execute_via_copilot(
                                        ctx["ticket"],
                                        ctx["agent"],
                                        ctx["info"],
                                        fallback,
                                        ctx["docs_context"],
                                    )
                                    if retry_proc is not None:
                                        active_proc = retry_proc
                                        continue
                                    add_comment(
                                        active_ticket_id,
                                        f"[copilot] Retry fallback na {fallback['model']} nie powiódł się.",
                                    )

                        print(f"\nCOPILOT_FAILED | {active_ticket_id} | exit={retcode} | log={log_file}", flush=True)
                        add_comment(active_ticket_id,
                            f"[copilot] Copilot zakończył z błędem (exit {retcode}). Log: {log_file}")
                        try:
                            move_ticket(active_ticket_id, "Do zrobienia")
                            print(f"MOVED_TO_TODO | {active_ticket_id} (failed, needs rework)", flush=True)
                        except Exception as me:
                            print(f"MOVE_ERROR | {active_ticket_id} | {me}", flush=True)
                    clear_executing()
                    active_proc = None
                    active_ticket_id = None

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

            # --- Detect dead QA process ---
            if qa_proc is not None and qa_proc.poll() is None:
                if not pid_alive(qa_proc.pid):
                    print(f"\nDEAD_QA_PROCESS | {qa_ticket_id} | PID {qa_proc.pid} vanished", flush=True)
                    if hasattr(qa_proc, '_log_fh'):
                        try: qa_proc._log_fh.close()
                        except: pass
                    qa_proc = None
                    qa_ticket_id = None

            # --- Check if QA Copilot process finished ---
            if qa_proc is not None:
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
                        try:
                            move_ticket(qa_ticket_id, "Review")
                            print(f"MOVED_TO_REVIEW | {qa_ticket_id}", flush=True)
                        except Exception as me:
                            print(f"MOVE_ERROR | {qa_ticket_id} | {me}", flush=True)
                    elif verdict == "fail":
                        print(f"\nQA_FAIL | {qa_ticket_id} | {reason}", flush=True)
                        add_comment(qa_ticket_id, f"[qa] ❌ QA FAILED: {reason}")
                        try:
                            move_ticket(qa_ticket_id, "Do zrobienia")
                            print(f"MOVED_TO_TODO | {qa_ticket_id} (QA failed, needs rework)", flush=True)
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

            # --- Poll queue (with API outage protection) ---
            try:
                data = poll_queue()
                queue = data.get("queue", [])
                api_fail_count = 0  # reset on success
            except Exception as poll_err:
                api_fail_count += 1
                if api_fail_count <= API_FAIL_THRESHOLD:
                    print(f"API_RETRY | attempt {api_fail_count}/{API_FAIL_THRESHOLD} | {poll_err}", flush=True)
                elif api_fail_count == API_FAIL_THRESHOLD + 1:
                    print(f"API_OUTAGE | backend unreachable, preserving active processes | {poll_err}", flush=True)
                try: login()
                except: pass
                time.sleep(args.interval)
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
            # Don't start a new ticket if anything is still in "W trakcie"
            if args.auto and todo and not is_executing() and active_proc is None and not in_progress:
                prio_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                todo.sort(key=lambda t: prio_order.get(t["priority"], 99))
                active_ticket_id, active_proc = auto_start_ticket(todo[0])

            # --- AUTO MODE: pick and start QA ticket (runs alongside dev) ---
            if args.auto and qa_tickets and qa_proc is None:
                # Pick first QA ticket (not the one we just moved there)
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

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
