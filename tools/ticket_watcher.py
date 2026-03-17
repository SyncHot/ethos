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

import sys, time, json, os, argparse, requests, urllib3, subprocess, shlex
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "http://localhost:9000/api"
TOKEN_FILE = "/tmp/.ethos_orchestrator_token"
LOCK_FILE = "/tmp/.ethos_watcher_executing"
DOCS_DIR = "/opt/ethos/docs"

# ── Complexity → AI Model mapping ────────────────────────────────────────
# Maps to Copilot CLI --model flag values
COMPLEXITY_MODEL_MAP = {
    "complex": {
        "model": "claude-opus-4.6",
        "label": "Opus (premium)",
        "reason": "Deep reasoning required for complex tasks",
    },
    "medium": {
        "model": "claude-sonnet-4.6",
        "label": "Sonnet (balanced)",
        "reason": "Good balance of quality and speed",
    },
    "simple": {
        "model": "claude-haiku-4.5",
        "label": "Haiku (fast)",
        "reason": "Fast execution for straightforward tasks",
    },
}

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
    with open(TOKEN_FILE, "w") as f:
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

def api(method, path, body=None):
    fn = requests.get if method == "GET" else (requests.post if method == "POST" else requests.put)
    kw = {"headers": headers(), "verify": False}
    if body: kw["json"] = body
    r = fn(f"{BASE}{path}", **kw)
    if r.status_code == 401:
        login()
        kw["headers"] = headers()
        r = fn(f"{BASE}{path}", **kw)
    r.raise_for_status()
    return r.json()

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
        with open(LOCK_FILE) as f:
            return json.load(f)
    return None

# ── Agent detection ──────────────────────────────────────────────────────

def detect_agent(title, labels):
    t = title.lower()
    l = [x.lower() for x in labels]
    if any(x in t for x in ['[fe]','frontend','ui','ux','css','design']): return "FE/UX"
    if any(x in t for x in ['[be]','backend','api','endpoint']): return "Backend"
    if any(x in t for x in ['[devops]','deploy','build','docker']): return "DevOps"
    if any(x in t for x in ['[sec]','security']): return "Security"
    if any(x in t for x in ['[docs]','dokumentacja']): return "Docs"
    if any(x in t for x in ['[qa]','test']): return "QA"
    if any(x in l for x in ['frontend','fe','ui']): return "FE/UX"
    if any(x in l for x in ['backend','be','api']): return "Backend"
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
MAX_AUTOPILOT = 25

def build_copilot_prompt(ticket, agent, info, model_info, docs_context):
    """Build a comprehensive prompt for Copilot CLI to execute a ticket."""
    tid = ticket["id"]
    title = ticket["title"]
    desc = ticket.get("description", "")
    priority = ticket.get("priority", "medium")
    complexity = ticket.get("complexity", "medium")
    lc = ticket.get("last_comment")
    feedback = f"\nUser feedback: {lc['text']}" if lc else ""

    prompt = f"""You are an EthOS developer agent. Execute the following ticket.

TICKET: {tid}
Title: {title}
Priority: {priority}
Complexity: {complexity}
Agent type: {agent}
Skills needed: {info['skills']}
{f'Description: {desc}' if desc else ''}
{feedback}

RULES (follow strictly):
1. Read the relevant docs before making changes: {', '.join(info.get('docs', []))}
   Docs are in /opt/ethos/docs/
2. Follow the lessons learned in /opt/ethos/docs/{info['lessons']}
3. Work in /opt/ethos/ — this is the project root
4. After making changes, test them (restart ethos if backend changes: sudo systemctl restart ethos)
5. Commit changes with a descriptive message including ticket ID [{tid}]
6. If the task is done, report completion

{f'DOCS CONTEXT:{chr(10)}{docs_context[:4000]}' if docs_context else ''}

Start by reading the relevant files, then implement the solution. Be thorough."""

    return prompt


def execute_via_copilot(ticket, agent, info, model_info, docs_context):
    """Launch Copilot CLI in non-interactive mode to solve the ticket."""
    tid = ticket["id"]
    model = model_info["model"]

    # Ensure log dir exists
    os.makedirs(COPILOT_LOG_DIR, exist_ok=True)
    log_file = os.path.join(COPILOT_LOG_DIR, f"{tid}_{int(time.time())}.log")

    prompt = build_copilot_prompt(ticket, agent, info, model_info, docs_context)

    cmd = [
        COPILOT_BIN,
        "-p", prompt,
        "--model", model,
        "--autopilot",
        "--allow-all",
        "--max-autopilot-continues", str(MAX_AUTOPILOT),
    ]

    print(f"COPILOT_START | {tid} | model={model} | log={log_file}", flush=True)

    try:
        with open(log_file, "w") as lf:
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
    print(f"Model mapping: complex→Opus, medium→Sonnet, simple→Haiku", flush=True)
    print(f"Copilot CLI: {COPILOT_BIN}", flush=True)

    prev_state = {}
    active_proc = None  # Track running Copilot process
    active_ticket_id = None

    while True:
        try:
            # Check if Copilot process finished
            if active_proc is not None:
                retcode = active_proc.poll()
                if retcode is not None:
                    executing = get_executing()
                    log_file = executing.get("log_file", "?") if executing else "?"
                    if retcode == 0:
                        print(f"\nCOPILOT_DONE | {active_ticket_id} | exit=0 | log={log_file}", flush=True)
                        add_comment(active_ticket_id,
                            f"[copilot] Zakończyłem pracę nad ticketem (exit 0). Log: {log_file}")
                        # Move to Review
                        try:
                            move_ticket(active_ticket_id, "Review")
                            print(f"MOVED_TO_REVIEW | {active_ticket_id}", flush=True)
                        except Exception as me:
                            print(f"MOVE_ERROR | {active_ticket_id} | {me}", flush=True)
                    else:
                        print(f"\nCOPILOT_FAILED | {active_ticket_id} | exit={retcode} | log={log_file}", flush=True)
                        add_comment(active_ticket_id,
                            f"[copilot] Copilot zakończył z błędem (exit {retcode}). Log: {log_file}")
                    clear_executing()
                    active_proc = None
                    active_ticket_id = None

            data = poll_queue()
            queue = data.get("queue", [])
            current_state = {t["id"]: t["column"] for t in queue}

            todo = [t for t in queue if t["column"] == "Do zrobienia"]
            in_progress = [t for t in queue if t["column"] == "W trakcie"]

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

            # AUTO MODE: pick and start highest priority ticket (only if no copilot running)
            if args.auto and todo and not is_executing() and active_proc is None:
                prio_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                todo.sort(key=lambda t: prio_order.get(t["priority"], 99))
                active_ticket_id, active_proc = auto_start_ticket(todo[0])

            # Check if executing ticket was moved out of queue manually (done/review by user)
            executing = get_executing()
            if executing and executing["ticket_id"] not in current_state:
                print(f"COMPLETED | {executing['ticket_id']} left queue (manual)", flush=True)
                # Kill copilot if still running
                if active_proc and active_proc.poll() is None:
                    pid = active_proc.pid
                    active_proc.terminate()
                    print(f"COPILOT_TERMINATED | PID={pid} (ticket removed from queue)", flush=True)
                clear_executing()
                active_proc = None
                active_ticket_id = None

            prev_state = current_state

        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            try: login()
            except: pass

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
