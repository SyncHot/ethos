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

import sys, time, json, os, argparse, requests, urllib3
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

def poll(project_id):
    data = api_get(f"/tickets/projects/{project_id}/tickets")
    tickets = data.get("tickets", [])
    return {t["id"]: t for t in tickets}

def diff_tickets(old_map, new_map):
    events = []
    for tid, ticket in new_map.items():
        if tid not in old_map:
            events.append({
                "type": "NEW",
                "id": tid,
                "priority": ticket.get("priority", "medium"),
                "title": ticket.get("title", ""),
                "column": ticket.get("column", ""),
            })
        else:
            old_col = old_map[tid].get("column", "")
            new_col = ticket.get("column", "")
            if old_col != new_col:
                ev_type = "REWORK" if new_col == "Do zrobienia" and old_col == "Review" else "MOVED"
                events.append({
                    "type": ev_type,
                    "id": tid,
                    "from": old_col,
                    "to": new_col,
                    "priority": ticket.get("priority", "medium"),
                    "title": ticket.get("title", ""),
                })
    return events

def format_event(ev):
    if ev["type"] == "NEW":
        return f"🆕 NEW | {ev['id']} | {ev['priority'].upper()} | {ev['column']} | {ev['title']}"
    elif ev["type"] == "REWORK":
        return f"🔄 REWORK | {ev['id']} | {ev['priority'].upper()} | {ev['title']}"
    elif ev["type"] == "MOVED":
        return f"➡️  MOVED | {ev['id']} | {ev['from']} → {ev['to']} | {ev['title']}"
    return str(ev)

def actionable(ev):
    """Return True if this event needs Copilot action."""
    if ev["type"] == "NEW" and ev.get("column") == "Do zrobienia":
        return True
    if ev["type"] == "REWORK":
        return True
    if ev["type"] == "MOVED" and ev.get("to") == "Do zrobienia":
        return True
    return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    args = parser.parse_args()

    projects = api_get("/tickets/projects").get("projects", [])
    proj = next((p for p in projects if p["name"] == PROJECT_NAME), None)
    if not proj:
        print(f"❌ Projekt '{PROJECT_NAME}' nie znaleziony.", flush=True)
        sys.exit(1)

    pid = proj["id"]
    print(f"👁️  Watcher started | Projekt: {proj['name']} | Interval: {args.interval}s", flush=True)
    print(f"   Monitoring columns: Do zrobienia, Review → Do zrobienia (rework)", flush=True)

    # Initial snapshot
    current = poll(pid)
    
    # Show initial state
    todo = [t for t in current.values() if t.get("column") == "Do zrobienia"]
    if todo:
        sorted_todo = sorted(todo, key=lambda t: {"critical":0,"high":1,"medium":2,"low":3}.get(t.get("priority","medium"),2))
        print(f"\n📋 Aktualnie w 'Do zrobienia': {len(todo)} ticketów", flush=True)
        for t in sorted_todo:
            print(f"   🎯 ACTION_NEEDED | {t['id']} | {t.get('priority','medium').upper()} | {t['title']}", flush=True)
    else:
        print(f"\n✅ 'Do zrobienia' jest puste — czekam na nowe tickety...", flush=True)

    save_state({"tickets": {tid: {"column": t.get("column","")} for tid,t in current.items()}})

    while True:
        time.sleep(args.interval)
        try:
            new_tickets = poll(pid)
            events = diff_tickets(current, new_tickets)
            
            for ev in events:
                print(f"\n{format_event(ev)}", flush=True)
                if actionable(ev):
                    print(f"   🎯 ACTION_NEEDED | {ev['id']} | {ev.get('priority','medium').upper()} | {ev.get('title','')}", flush=True)
            
            current = new_tickets
            save_state({"tickets": {tid: {"column": t.get("column","")} for tid,t in current.items()}})
            
        except Exception as e:
            print(f"⚠️  Poll error: {e}", flush=True)
            try:
                login()
            except:
                pass

if __name__ == "__main__":
    main()
