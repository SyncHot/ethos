#!/usr/bin/env python3
"""
EthOS Ticket Watcher — polls the board every N seconds and reports new/changed tickets.
Outputs actionable lines that Copilot can parse.

Usage:
    python3 ticket_watcher.py [--interval 15]

Output format (one line per event):
    ACTION_NEEDED|<ticket_id>|<priority>|<agent>|<title>
    REWORK|<ticket_id>|<agent>|<title> + feedback comment
    MOVED|<ticket_id>|<from_col>|<to_col>|<title>
"""

import sys, time, json, os, argparse, requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "http://localhost:9000/api"
TOKEN_FILE = "/tmp/.ethos_orchestrator_token"

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
            if t:
                return t
    return login()

def headers():
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}

def poll_queue():
    r = requests.get(f"{BASE}/tickets/copilot/queue", headers=headers(), verify=False)
    if r.status_code == 401:
        login()
        r = requests.get(f"{BASE}/tickets/copilot/queue", headers=headers(), verify=False)
    r.raise_for_status()
    return r.json()

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

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

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    parser.add_argument("--auto", action="store_true", help="Auto-process tickets (not just watch)")
    args = parser.parse_args()

    print(f"Ticket Watcher | interval={args.interval}s | auto={'ON' if args.auto else 'OFF'}", flush=True)
    print(f"Monitoring copilot-enabled projects via /copilot/queue", flush=True)

    # Track {ticket_id: column} to detect rework
    prev_state = {}

    while True:
        try:
            data = poll_queue()
            queue = data.get("queue", [])
            total = data.get("total", 0)
            current_state = {t["id"]: t["column"] for t in queue}

            for t in queue:
                tid, col = t["id"], t["column"]
                prev_col = prev_state.get(tid)
                agent = detect_agent(t["title"], t.get("labels", []))

                if prev_col is None and col == "Do zrobienia":
                    # New ticket in Do zrobienia
                    print(f"\nACTION_NEEDED | {tid} | {t['priority'].upper()} | {agent} | {t['title']}", flush=True)
                    if t.get("description"):
                        print(f"  desc: {t['description'][:120]}", flush=True)
                    lc = t.get("last_comment")
                    if lc:
                        print(f"  comment [{lc['author']}]: {lc['text'][:150]}", flush=True)
                    print(f"  queue: {sum(1 for c in current_state.values() if c=='Do zrobienia')} do zrobienia, {total} total", flush=True)

                elif prev_col and prev_col != col and col == "Do zrobienia":
                    # REWORK — moved back to Do zrobienia
                    print(f"\nREWORK | {tid} | {agent} | {t['title']}", flush=True)
                    print(f"  moved: {prev_col} -> {col}", flush=True)
                    lc = t.get("last_comment")
                    if lc:
                        print(f"  feedback [{lc['author']}]: {lc['text'][:150]}", flush=True)
                    else:
                        print(f"  WARNING: no feedback comment found", flush=True)

                elif prev_col and prev_col != col:
                    # Moved between other columns
                    print(f"\nMOVED | {tid} | {prev_col} -> {col} | {t['title']}", flush=True)

            # Detect tickets removed from queue
            removed = set(prev_state.keys()) - set(current_state.keys())
            for rid in removed:
                print(f"DONE | {rid} removed from queue", flush=True)

            prev_state = current_state

        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            try: login()
            except: pass

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
