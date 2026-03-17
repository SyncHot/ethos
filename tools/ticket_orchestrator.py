#!/usr/bin/env python3
"""
EthOS Ticket Orchestrator — CLI helper for Copilot agents.

Usage:
    python3 ticket_orchestrator.py login
    python3 ticket_orchestrator.py next          # Show next ticket to work on
    python3 ticket_orchestrator.py board          # Show full board state
    python3 ticket_orchestrator.py start <id>     # Move ticket to "W trakcie"
    python3 ticket_orchestrator.py review <id>    # Move ticket to "Review"
    python3 ticket_orchestrator.py done <id>      # Move ticket to "Gotowe"
    python3 ticket_orchestrator.py rework <id>    # Move ticket back to "W trakcie"
    python3 ticket_orchestrator.py comment <id> <text>  # Add comment
"""

import sys, json, os, requests

BASE = "http://localhost:9000/api"
TOKEN_FILE = "/tmp/.ethos_orchestrator_token"
PROJECT_NAME = "ETHOS"

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
COLUMN_FLOW = ["Backlog", "Do zrobienia", "W trakcie", "Review", "Gotowe"]

# ─── Auth ───

def login():
    r = requests.post(f"{BASE}/auth/login", json={"username": "marcin", "password": "pluton2303"}, verify=False)
    r.raise_for_status()
    token = r.json().get("token", "")
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    print(f"✅ Logged in. Token saved.")
    return token

def get_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    return login()

def headers():
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}

# ─── API ───

def get_projects():
    r = requests.get(f"{BASE}/tickets/projects", headers=headers(), verify=False)
    r.raise_for_status()
    return r.json().get("projects", [])

def get_tickets(project_id):
    r = requests.get(f"{BASE}/tickets/projects/{project_id}/tickets", headers=headers(), verify=False)
    r.raise_for_status()
    return r.json().get("tickets", [])

def move_ticket(ticket_id, column):
    r = requests.put(f"{BASE}/tickets/tickets/{ticket_id}/move",
                     json={"column": column, "order": 0}, headers=headers(), verify=False)
    r.raise_for_status()
    return r.json()

def add_comment(ticket_id, text):
    r = requests.post(f"{BASE}/tickets/tickets/{ticket_id}/comments",
                      json={"text": text}, headers=headers(), verify=False)
    r.raise_for_status()
    return r.json()

def get_project():
    projects = get_projects()
    proj = next((p for p in projects if p["name"] == PROJECT_NAME), None)
    if not proj:
        print(f"❌ Projekt '{PROJECT_NAME}' nie znaleziony.")
        sys.exit(1)
    return proj

# ─── Commands ───

def cmd_board():
    proj = get_project()
    tickets = get_tickets(proj["id"])
    print(f"\n📋 Projekt: {proj['name']} ({len(tickets)} ticketów)\n")
    for col in COLUMN_FLOW:
        col_tix = sorted(
            [t for t in tickets if t.get("column") == col],
            key=lambda t: PRIORITY_ORDER.get(t.get("priority", "medium"), 2)
        )
        print(f"  ┌── {col} ({len(col_tix)}) ──")
        if not col_tix:
            print(f"  │   (pusto)")
        for t in col_tix:
            prio = t.get("priority", "medium").upper()
            assignee = t.get("assignee", "")
            labels = ", ".join(t.get("labels", []))
            print(f"  │   [{prio}] {t['title']}")
            print(f"  │         id={t['id']}  assignee={assignee or '—'}  labels={labels or '—'}")
        print(f"  └{'─' * 40}")
    print()

def cmd_next():
    proj = get_project()
    tickets = get_tickets(proj["id"])
    todo = sorted(
        [t for t in tickets if t.get("column") == "Do zrobienia"],
        key=lambda t: PRIORITY_ORDER.get(t.get("priority", "medium"), 2)
    )
    in_progress = [t for t in tickets if t.get("column") == "W trakcie"]
    in_review = [t for t in tickets if t.get("column") == "Review"]

    print(f"\n📊 Status: {len(in_progress)} w trakcie, {len(in_review)} w review, {len(todo)} do zrobienia\n")

    if in_review:
        print("⏸️  CZEKAM NA REVIEW:")
        for t in in_review:
            print(f"   [{t['priority'].upper()}] {t['title']}  (id={t['id']})")
        print()

    if in_progress:
        print("🔨 W TRAKCIE:")
        for t in in_progress:
            print(f"   [{t['priority'].upper()}] {t['title']}  (id={t['id']})")
        print()

    if not todo:
        print("✅ Brak ticketów do zrobienia. Kolumna 'Do zrobienia' jest pusta.")
        return

    next_ticket = todo[0]
    title = next_ticket["title"]
    desc = next_ticket.get("description", "")
    prio = next_ticket.get("priority", "medium")
    labels = next_ticket.get("labels", [])
    tid = next_ticket["id"]

    # Determine agent type from title prefix or labels
    agent = _detect_agent(title, labels)

    print(f"🎯 NASTĘPNY TICKET:")
    print(f"   Tytuł:     {title}")
    print(f"   ID:        {tid}")
    print(f"   Priorytet: {prio.upper()}")
    print(f"   Opis:      {desc or '(brak)'}")
    print(f"   Labels:    {', '.join(labels) or '—'}")
    print(f"   Agent:     {agent}")
    print(f"\n   Aby rozpocząć: python3 ticket_orchestrator.py start {tid}")
    print()

def _detect_agent(title, labels):
    """Detect which agent type should handle this ticket."""
    title_lower = title.lower()
    labels_lower = [l.lower() for l in labels]

    if any(x in title_lower for x in ['[fe]', 'frontend', 'ui', 'ux', 'css', 'design']):
        return "FE/UX Agent (general-purpose)"
    if any(x in title_lower for x in ['[be]', 'backend', 'api', 'endpoint']):
        return "Backend Agent (general-purpose)"
    if any(x in title_lower for x in ['[devops]', 'deploy', 'build', 'ci', 'docker']):
        return "DevOps Agent (task)"
    if any(x in title_lower for x in ['[sec]', 'security', 'hardening']):
        return "Security Agent (general-purpose)"
    if any(x in title_lower for x in ['[docs]', 'dokumentacja', 'documentation']):
        return "Docs Agent (general-purpose)"
    if any(x in title_lower for x in ['[qa]', 'test', 'qa']):
        return "QA Agent (task)"

    # Check labels
    if any(x in labels_lower for x in ['frontend', 'fe', 'ui', 'ux']):
        return "FE/UX Agent (general-purpose)"
    if any(x in labels_lower for x in ['backend', 'be', 'api']):
        return "Backend Agent (general-purpose)"

    return "General-purpose Agent"

def cmd_move(ticket_id, target_column):
    if target_column not in COLUMN_FLOW:
        print(f"❌ Nieprawidłowa kolumna: {target_column}")
        print(f"   Dostępne: {', '.join(COLUMN_FLOW)}")
        return
    move_ticket(ticket_id, target_column)
    print(f"✅ Ticket {ticket_id} → {target_column}")

def cmd_comment(ticket_id, text):
    add_comment(ticket_id, text)
    print(f"💬 Komentarz dodany do {ticket_id}")

# ─── Main ───

if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "login":
        login()
    elif cmd == "board":
        cmd_board()
    elif cmd == "next":
        cmd_next()
    elif cmd == "start":
        if len(sys.argv) < 3:
            print("Użycie: start <ticket_id>")
            sys.exit(1)
        cmd_move(sys.argv[2], "W trakcie")
    elif cmd == "review":
        if len(sys.argv) < 3:
            print("Użycie: review <ticket_id>")
            sys.exit(1)
        cmd_move(sys.argv[2], "Review")
    elif cmd == "done":
        if len(sys.argv) < 3:
            print("Użycie: done <ticket_id>")
            sys.exit(1)
        cmd_move(sys.argv[2], "Gotowe")
    elif cmd == "rework":
        if len(sys.argv) < 3:
            print("Użycie: rework <ticket_id>")
            sys.exit(1)
        cmd_move(sys.argv[2], "W trakcie")
    elif cmd == "comment":
        if len(sys.argv) < 4:
            print("Użycie: comment <ticket_id> <text>")
            sys.exit(1)
        cmd_comment(sys.argv[2], " ".join(sys.argv[3:]))
    else:
        print(f"❌ Nieznane polecenie: {cmd}")
        print(__doc__)
