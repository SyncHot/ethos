#!/usr/bin/env python3
"""
EthOS Ticket Watcher — polls the board every N seconds and reports new/changed tickets.
Outputs actionable lines that Copilot can parse.

Usage:
    python3 ticket_watcher.py [--interval 15]

Output format (one line per event):
    NEW|<ticket_id>|<priority>|<title>|<column>
    MOVED|<ticket_id>|<from_col>|<to_col>|<title>
    REWORK|<ticket_id>|<title>   (moved back from Review to Do zrobienia)
"""

import sys, time, json, os, argparse, requests, urllib3, subprocess
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "http://localhost:9000/api"
TOKEN_FILE = "/tmp/.ethos_orchestrator_token"
PROJECT_NAME = "ETHOS"
STATE_FILE = "/tmp/.ethos_watcher_state.json"

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

def api_get(path):
    r = requests.get(f"{BASE}{path}", headers=headers(), verify=False)
    if r.status_code == 401:
        login()
        r = requests.get(f"{BASE}{path}", headers=headers(), verify=False)
    r.raise_for_status()
    return r.json()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def poll_queue():
    r = requests.get(f"{BASE}/tickets/copilot/queue", headers=headers(), verify=False)
    if r.status_code == 401:
        login()
        r = requests.get(f"{BASE}/tickets/copilot/queue", headers=headers(), verify=False)
    r.raise_for_status()
    return r.json()

def assign_ticket(ticket_id, username="copilot"):
    requests.put(f"{BASE}/tickets/tickets/{ticket_id}",
                 json={"assignee": username}, headers=headers(), verify=False)

def move_ticket(ticket_id, column):
    requests.put(f"{BASE}/tickets/tickets/{ticket_id}/move",
                 json={"column": column, "order": 0}, headers=headers(), verify=False)

def add_comment(ticket_id, text):
    requests.post(f"{BASE}/tickets/tickets/{ticket_id}/comments",
                  json={"text": text}, headers=headers(), verify=False)

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

    print(f"👁️  Ticket Watcher | interval={args.interval}s | auto={'ON' if args.auto else 'OFF'}", flush=True)
    print(f"   Monitoring copilot-enabled projects via /copilot/queue", flush=True)

    seen_ids = set()

    while True:
        try:
            data = poll_queue()
            queue = data.get("queue", [])
            total = data.get("total", 0)

            # Check for new actionable tickets
            todo = [t for t in queue if t["column"] == "Do zrobienia"]
            new_todo = [t for t in todo if t["id"] not in seen_ids]

            for t in new_todo:
                agent = detect_agent(t["title"], t.get("labels", []))
                print(f"\n🎯 ACTION_NEEDED | {t['id']} | {t['priority'].upper()} | {agent} | {t['title']}", flush=True)
                if t.get("description"):
                    print(f"   �� {t['description'][:120]}", flush=True)

            # Track all seen IDs
            current_ids = {t["id"] for t in queue}
            done_ids = seen_ids - current_ids
            for did in done_ids:
                print(f"   ✅ {did} — usunięto z kolejki (przeniesiony)", flush=True)
            seen_ids = current_ids

            if not queue and total == 0:
                pass  # silent when empty
            elif new_todo:
                print(f"   📊 Kolejka: {len(todo)} do zrobienia, {total} łącznie", flush=True)

        except Exception as e:
            print(f"⚠️  Error: {e}", flush=True)
            try: login()
            except: pass

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
