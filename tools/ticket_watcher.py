#!/usr/bin/env python3
"""
EthOS Ticket Watcher — polls copilot queue and auto-processes tickets.

Usage:
    python3 ticket_watcher.py [--interval 15] [--auto]

With --auto: picks highest priority ticket, assigns to copilot, moves to W trakcie,
outputs EXECUTE instruction for Copilot to act on.
"""

import sys, time, json, os, argparse, requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "http://localhost:9000/api"
TOKEN_FILE = "/tmp/.ethos_orchestrator_token"
LOCK_FILE = "/tmp/.ethos_watcher_executing"

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

def is_executing():
    return os.path.exists(LOCK_FILE)

def set_executing(tid):
    with open(LOCK_FILE, "w") as f:
        json.dump({"ticket_id": tid, "started": time.time()}, f)

def clear_executing():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

def get_executing():
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE) as f:
            return json.load(f)
    return None

AGENT_MAP = {
    "FE/UX": {"lessons": "FE_LESSONS_LEARNED.md", "skills": "frontend, CSS, JS, UX"},
    "Backend": {"lessons": "BE_LESSONS_LEARNED.md", "skills": "Python, Flask, API"},
    "DevOps": {"lessons": "DEVOPS_LESSONS_LEARNED.md", "skills": "systemd, Docker, deployment"},
    "Docs": {"lessons": "DEVOPS_LESSONS_LEARNED.md", "skills": "documentation"},
    "QA": {"lessons": "FE_LESSONS_LEARNED.md", "skills": "testing, QA"},
    "General": {"lessons": "FE_LESSONS_LEARNED.md", "skills": "general development"},
}

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

def auto_start_ticket(ticket):
    """Start ticket: assign to copilot, move to W trakcie, output EXECUTE instruction."""
    tid = ticket["id"]
    title = ticket["title"]
    agent = detect_agent(title, ticket.get("labels", []))
    info = AGENT_MAP.get(agent, AGENT_MAP["General"])

    # Assign and move
    assign_ticket(tid, "copilot")
    move_ticket(tid, "W trakcie")
    set_executing(tid)

    # Build context
    desc = ticket.get("description", "")
    lc = ticket.get("last_comment")
    feedback = f"\nFeedback: {lc['text']}" if lc else ""

    print(f"\n{'='*60}", flush=True)
    print(f"EXECUTE | {tid} | {agent}", flush=True)
    print(f"Title: {title}", flush=True)
    print(f"Priority: {ticket['priority']}", flush=True)
    if desc: print(f"Description: {desc}", flush=True)
    if feedback: print(f"Feedback: {lc['text']}", flush=True)
    print(f"Lessons: /opt/ethos/docs/{info['lessons']}", flush=True)
    print(f"Skills: {info['skills']}", flush=True)
    print(f"Status: ASSIGNED to copilot, moved to W trakcie", flush=True)
    print(f"{'='*60}", flush=True)

    add_comment(tid, f"[copilot] Rozpoczynam prace nad ticketem. Agent: {agent}")
    return tid

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    parser.add_argument("--auto", action="store_true", help="Auto-start tickets from Do zrobienia")
    args = parser.parse_args()

    mode = "AUTO" if args.auto else "WATCH"
    print(f"Ticket Watcher | interval={args.interval}s | mode={mode}", flush=True)
    print(f"Monitoring copilot-enabled projects via /copilot/queue", flush=True)

    prev_state = {}

    while True:
        try:
            data = poll_queue()
            queue = data.get("queue", [])
            current_state = {t["id"]: t["column"] for t in queue}

            todo = [t for t in queue if t["column"] == "Do zrobienia"]
            in_progress = [t for t in queue if t["column"] == "W trakcie"]

            for t in queue:
                tid, col = t["id"], t["column"]
                prev_col = prev_state.get(tid)
                agent = detect_agent(t["title"], t.get("labels", []))

                if prev_col is None and col == "Do zrobienia":
                    print(f"\nACTION_NEEDED | {tid} | {t['priority'].upper()} | {agent} | {t['title']}", flush=True)
                    lc = t.get("last_comment")
                    if lc:
                        print(f"  comment [{lc['author']}]: {lc['text'][:150]}", flush=True)

                elif prev_col and prev_col != col and col == "Do zrobienia":
                    print(f"\nREWORK | {tid} | {agent} | {t['title']}", flush=True)
                    print(f"  moved: {prev_col} -> {col}", flush=True)
                    lc = t.get("last_comment")
                    if lc:
                        print(f"  feedback [{lc['author']}]: {lc['text'][:150]}", flush=True)

                elif prev_col and prev_col != col:
                    print(f"\nMOVED | {tid} | {prev_col} -> {col} | {t['title']}", flush=True)

            removed = set(prev_state.keys()) - set(current_state.keys())
            for rid in removed:
                print(f"DONE | {rid} removed from queue", flush=True)

            # AUTO MODE: pick and start highest priority ticket
            if args.auto and todo and not is_executing():
                # Sort by priority
                prio_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                todo.sort(key=lambda t: prio_order.get(t["priority"], 99))
                auto_start_ticket(todo[0])

            # Check if executing ticket was moved out of queue (done/review)
            executing = get_executing()
            if executing and executing["ticket_id"] not in current_state:
                print(f"COMPLETED | {executing['ticket_id']} left queue", flush=True)
                clear_executing()

            prev_state = current_state

        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            try: login()
            except: pass

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
