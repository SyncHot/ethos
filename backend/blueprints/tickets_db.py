import sqlite3
import os
import json
import time
from datetime import datetime
import sys

# Path setup
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path

DB_PATH = os.environ.get('TICKETS_DB_PATH', data_path('tickets.db'))
JSON_PATH = data_path('tickets.json')

def get_db():
    from blueprints.db_pool import get_pooled_db
    return get_pooled_db(DB_PATH)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    # Check if DB exists before connecting to know if we need migration
    db_exists = os.path.exists(DB_PATH)
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Projects table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            owner TEXT,
            members TEXT,
            columns TEXT,
            color TEXT,
            created REAL,
            updated REAL,
            copilot_enabled INTEGER DEFAULT 0,
            localai_enabled INTEGER DEFAULT 0,
            freemodel_enabled INTEGER DEFAULT 0
        )
    ''')

    # Tickets table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            title TEXT NOT NULL,
            description TEXT,
            column TEXT,
            priority TEXT,
            assignee TEXT,
            reporter TEXT,
            labels TEXT,
            comments TEXT,
            attachments TEXT,
            "order" INTEGER,
            created REAL,
            updated REAL,
            FOREIGN KEY(project_id) REFERENCES projects(id)
        )
    ''')
    
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_tickets_project_id ON tickets(project_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_tickets_updated ON tickets(updated)')

    # Migration: add manual_tests column if missing
    cols = [r[1] for r in cursor.execute("PRAGMA table_info(tickets)").fetchall()]
    if 'manual_tests' not in cols:
        cursor.execute("ALTER TABLE tickets ADD COLUMN manual_tests TEXT DEFAULT '[]'")

    conn.commit()
    conn.close()
    
    # Migrate if DB was just created and JSON exists
    if not db_exists and os.path.exists(JSON_PATH):
        migrate_from_json()

def migrate_from_json():
    print(f"Migrating tickets from {JSON_PATH} to SQLite...")
    try:
        with open(JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        projects = data.get('projects', [])
        tickets = data.get('tickets', [])
        
        conn = get_db()
        
        for p in projects:
            conn.execute(
                '''INSERT OR IGNORE INTO projects (
                    id, name, description, owner, members, columns, color, 
                    created, updated, copilot_enabled, localai_enabled, freemodel_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    p.get('id'), p.get('name', ''), p.get('description', ''),
                    p.get('owner', ''), json.dumps(p.get('members', [])),
                    json.dumps(p.get('columns', [])), p.get('color', ''),
                    p.get('created', time.time()), p.get('updated', time.time()),
                    1 if p.get('copilot_enabled') else 0,
                    1 if p.get('localai_enabled') else 0,
                    1 if p.get('freemodel_enabled') else 0
                )
            )
            
        for t in tickets:
            conn.execute(
                '''INSERT OR IGNORE INTO tickets (
                    id, project_id, title, description, column, priority, 
                    assignee, reporter, labels, comments, attachments, "order", created, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    t.get('id'), t.get('project_id'), t.get('title', ''),
                    t.get('description', ''), t.get('column', ''),
                    t.get('priority', 'medium'), t.get('assignee', ''),
                    t.get('reporter', ''), json.dumps(t.get('labels', [])),
                    json.dumps(t.get('comments', [])), json.dumps(t.get('attachments', [])), t.get('order', 0),
                    t.get('created', time.time()), t.get('updated', time.time())
                )
            )
            
        conn.commit()
        conn.close()
        print("Migration complete.")
        
        # Rename JSON file to .migrated to avoid confusion, or keep as backup
        # os.rename(JSON_PATH, JSON_PATH + '.migrated')
        
    except Exception as e:
        print(f"Migration failed: {e}")

def _row_to_dict(row):
    d = dict(row)
    # Parse JSON fields
    for field in ['members', 'columns', 'labels', 'comments', 'attachments', 'manual_tests']:
        if field in d and d[field]:
            try:
                d[field] = json.loads(d[field])
            except:
                d[field] = []
    
    # Convert booleans
    for field in ['copilot_enabled', 'localai_enabled', 'freemodel_enabled']:
        if field in d:
            d[field] = bool(d[field])
            
    return d

def get_projects_with_stats():
    conn = get_db()
    
    # Get all projects
    projects_rows = conn.execute('SELECT * FROM projects ORDER BY updated DESC').fetchall()
    projects = [_row_to_dict(r) for r in projects_rows]
    
    # Get stats for each project
    # This could be done with a JOIN but keeping it simple for now to avoid complex SQL
    # Or separate query for counts
    for p in projects:
        stats = conn.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN column = 'W trakcie' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN column = 'Gotowe' THEN 1 ELSE 0 END) as done
            FROM tickets 
            WHERE project_id = ?
        ''', (p['id'],)).fetchone()
        
        p['ticket_count'] = stats['total']
        p['in_progress_count'] = stats['in_progress'] or 0
        p['done_count'] = stats['done'] or 0
        
    conn.close()
    return projects

def get_projects():
    conn = get_db()
    rows = conn.execute('SELECT * FROM projects ORDER BY updated DESC').fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]

def get_project(project_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
    conn.close()
    if row:
        return _row_to_dict(row)
    return None

def create_project(data):
    conn = get_db()
    now = time.time()
    try:
        conn.execute(
            '''INSERT INTO projects (
                id, name, description, owner, members, columns, color, 
                created, updated, copilot_enabled, localai_enabled, freemodel_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                data['id'], data.get('name', ''), data.get('description', ''),
                data.get('owner', ''), json.dumps(data.get('members', [])),
                json.dumps(data.get('columns', [])), data.get('color', ''),
                data.get('created', now), data.get('updated', now),
                1 if data.get('copilot_enabled') else 0,
                1 if data.get('localai_enabled') else 0,
                1 if data.get('freemodel_enabled') else 0
            )
        )
        conn.commit()
    finally:
        conn.close()
    return get_project(data['id'])

def update_project(project_id, data):
    conn = get_db()
    
    fields = []
    values = []
    
    mappings = {
        'name': 'name', 'description': 'description', 'owner': 'owner',
        'color': 'color'
    }
    
    json_mappings = {'members': 'members', 'columns': 'columns'}
    bool_mappings = {
        'copilot_enabled': 'copilot_enabled', 
        'localai_enabled': 'localai_enabled',
        'freemodel_enabled': 'freemodel_enabled'
    }

    for k, v in mappings.items():
        if k in data:
            fields.append(f"{v} = ?")
            values.append(data[k])
            
    for k, v in json_mappings.items():
        if k in data:
            fields.append(f"{v} = ?")
            values.append(json.dumps(data[k]))
            
    for k, v in bool_mappings.items():
        if k in data:
            fields.append(f"{v} = ?")
            values.append(1 if data[k] else 0)
    
    fields.append("updated = ?")
    values.append(time.time())
    
    values.append(project_id)
    
    try:
        if fields:
            query = f"UPDATE projects SET {', '.join(fields)} WHERE id = ?"
            conn.execute(query, tuple(values))
            conn.commit()
    finally:
        conn.close()
    return get_project(project_id)

def delete_project(project_id):
    conn = get_db()
    try:
        conn.execute('DELETE FROM tickets WHERE project_id = ?', (project_id,))
        conn.execute('DELETE FROM projects WHERE id = ?', (project_id,))
        conn.commit()
    finally:
        conn.close()

def get_tickets(project_id=None):
    conn = get_db()
    try:
        if project_id:
            rows = conn.execute('SELECT * FROM tickets WHERE project_id = ? ORDER BY "order" ASC, updated DESC', (project_id,)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM tickets ORDER BY updated DESC').fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]

def get_ticket(ticket_id):
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM tickets WHERE id = ?', (ticket_id,)).fetchone()
    finally:
        conn.close()
    if row:
        return _row_to_dict(row)
    return None

def create_ticket(data):
    conn = get_db()
    now = time.time()
    try:
        conn.execute(
            '''INSERT INTO tickets (
                id, project_id, title, description, column, priority, 
                assignee, reporter, labels, comments, attachments, "order",
                created, updated, manual_tests
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                data['id'], data.get('project_id'), data.get('title', ''),
                data.get('description', ''), data.get('column', ''),
                data.get('priority', 'medium'), data.get('assignee', ''),
                data.get('reporter', ''), json.dumps(data.get('labels', [])),
                json.dumps(data.get('comments', [])), json.dumps(data.get('attachments', [])), data.get('order', 0),
                data.get('created', now), data.get('updated', now),
                json.dumps(data.get('manual_tests', []))
            )
        )
        conn.commit()
    finally:
        conn.close()
    return get_ticket(data['id'])

def update_ticket(ticket_id, data):
    conn = get_db()
    
    fields = []
    values = []
    
    mappings = {
        'project_id': 'project_id', 'title': 'title', 'description': 'description',
        'column': 'column', 'priority': 'priority', 'assignee': 'assignee',
        'reporter': 'reporter', 'order': '"order"'
    }
    json_mappings = {'labels': 'labels', 'comments': 'comments', 'attachments': 'attachments', 'manual_tests': 'manual_tests'}

    for k, v in mappings.items():
        if k in data:
            fields.append(f"{v} = ?")
            values.append(data[k])

    for k, v in json_mappings.items():
        if k in data:
            fields.append(f"{v} = ?")
            values.append(json.dumps(data[k]))
    
    fields.append("updated = ?")
    values.append(time.time())
    
    values.append(ticket_id)
    
    try:
        if fields:
            query = f"UPDATE tickets SET {', '.join(fields)} WHERE id = ?"
            conn.execute(query, tuple(values))
            conn.commit()
    finally:
        conn.close()
    return get_ticket(ticket_id)

def delete_ticket(ticket_id):
    conn = get_db()
    try:
        conn.execute('DELETE FROM tickets WHERE id = ?', (ticket_id,))
        conn.commit()
    finally:
        conn.close()
