"""
EthOS — Family Hub Blueprint
Family center: announcements board, shopping lists, chores, calendar.
All routes under /api/familyhub.
"""

import os
import uuid
import time
import sqlite3
import logging
from contextlib import contextmanager
from flask import Blueprint, jsonify, request, g

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path as _data_path

log = logging.getLogger('familyhub')

familyhub_bp = Blueprint('familyhub', __name__, url_prefix='/api/familyhub')

DB_PATH = _data_path('familyhub.db')

# ──────────────────────────── DB helpers ────────────────────────────

def _uid():
    return uuid.uuid4().hex[:12]

def _now():
    return time.time()

@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA journal_mode=WAL')  # Enable Write-Ahead Logging
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # Set WAL mode once at init (persists across connections)
    _wal_conn = sqlite3.connect(DB_PATH, timeout=10)
    _wal_conn.execute('PRAGMA journal_mode=WAL')
    _wal_conn.close()
    with _db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT DEFAULT '',
                color TEXT DEFAULT 'blue',
                pinned INTEGER DEFAULT 0,
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS post_reactions (
                post_id TEXT NOT NULL,
                username TEXT NOT NULL,
                emoji TEXT NOT NULL,
                PRIMARY KEY (post_id, username, emoji),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS shopping_lists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT DEFAULT '#3b82f6',
                created_by TEXT,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS shopping_items (
                id TEXT PRIMARY KEY,
                list_id TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT DEFAULT '',
                checked INTEGER DEFAULT 0,
                added_by TEXT,
                checked_by TEXT,
                created_at REAL,
                FOREIGN KEY (list_id) REFERENCES shopping_lists(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS chores (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                assigned_to TEXT DEFAULT '',
                created_by TEXT,
                recurrence TEXT DEFAULT 'once',
                status TEXT DEFAULT 'pending',
                streak INTEGER DEFAULT 0,
                due_date TEXT DEFAULT '',
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS chore_completions (
                id TEXT PRIMARY KEY,
                chore_id TEXT NOT NULL,
                completed_by TEXT,
                completed_at REAL,
                FOREIGN KEY (chore_id) REFERENCES chores(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                event_date TEXT NOT NULL,
                event_time TEXT DEFAULT '',
                end_date TEXT DEFAULT '',
                color TEXT DEFAULT '#3b82f6',
                author TEXT,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_pinned ON posts(pinned);
            CREATE INDEX IF NOT EXISTS idx_shopping_items_list ON shopping_items(list_id);
            CREATE INDEX IF NOT EXISTS idx_chores_status ON chores(status);
            CREATE INDEX IF NOT EXISTS idx_chores_assigned ON chores(assigned_to);
            CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);
            CREATE INDEX IF NOT EXISTS idx_chore_completions_chore ON chore_completions(chore_id);
        ''')

_init_db()

def _rows_to_list(rows):
    return [dict(r) for r in rows]

def _get_username():
    return getattr(g, 'username', 'unknown')

# ──────────────────────────── Helpers ────────────────────────────

def _get_system_users():
    """Get list of system usernames (uid >= 1000)."""
    try:
        import subprocess
        r = subprocess.run(
            "getent passwd | awk -F: '($3 >= 1000 && $3 < 65534){print $1}'",
            shell=True, capture_output=True, text=True, timeout=5
        )
        return sorted([u.strip() for u in r.stdout.strip().split('\n') if u.strip()])
    except Exception:
        return []

# ══════════════════════════════════════════════════════════════════
#  ANNOUNCEMENTS BOARD (Posts / Announcements)
# ══════════════════════════════════════════════════════════════════

@familyhub_bp.route('/posts', methods=['GET'])
def get_posts():
    with _db() as conn:
        posts = _rows_to_list(conn.execute(
            'SELECT * FROM posts ORDER BY pinned DESC, created_at DESC'
        ).fetchall())
        for p in posts:
            p['reactions'] = _rows_to_list(conn.execute(
                'SELECT emoji, GROUP_CONCAT(username) as users FROM post_reactions '
                'WHERE post_id=? GROUP BY emoji', (p['id'],)
            ).fetchall())
    return jsonify({'ok': True, 'posts': posts})

@familyhub_bp.route('/posts', methods=['POST'])
def create_post():
    body = request.json or {}
    title = str(body.get('title', '')).strip()[:200]
    if not title:
        return jsonify({'error': 'Title is required'}), 400
    post = {
        'id': _uid(), 'author': _get_username(),
        'title': title,
        'content': str(body.get('content', '')).strip()[:5000],
        'color': str(body.get('color', 'blue'))[:20],
        'pinned': 0, 'created_at': _now(), 'updated_at': _now()
    }
    with _db() as conn:
        conn.execute(
            'INSERT INTO posts (id,author,title,content,color,pinned,created_at,updated_at) '
            'VALUES (:id,:author,:title,:content,:color,:pinned,:created_at,:updated_at)', post
        )
    post['reactions'] = []
    return jsonify({'ok': True, 'post': post})

@familyhub_bp.route('/posts/<post_id>', methods=['PUT'])
def update_post(post_id):
    body = request.json or {}
    with _db() as conn:
        row = conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        sets, vals = [], []
        for field in ('title', 'content', 'color', 'pinned'):
            if field in body:
                v = body[field]
                if field == 'pinned':
                    v = 1 if v else 0
                elif field == 'title':
                    v = str(v).strip()[:200]
                elif field == 'content':
                    v = str(v).strip()[:5000]
                else:
                    v = str(v)[:20]
                sets.append(f'{field}=?')
                vals.append(v)
        if sets:
            sets.append('updated_at=?')
            vals.append(_now())
            vals.append(post_id)
            conn.execute(f'UPDATE posts SET {",".join(sets)} WHERE id=?', vals)
        post = dict(conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone())
        post['reactions'] = _rows_to_list(conn.execute(
            'SELECT emoji, GROUP_CONCAT(username) as users FROM post_reactions '
            'WHERE post_id=? GROUP BY emoji', (post_id,)
        ).fetchall())
    return jsonify({'ok': True, 'post': post})

@familyhub_bp.route('/posts/<post_id>', methods=['DELETE'])
def delete_post(post_id):
    with _db() as conn:
        conn.execute('DELETE FROM posts WHERE id=?', (post_id,))
    return jsonify({'ok': True})

@familyhub_bp.route('/posts/<post_id>/react', methods=['POST'])
def react_post(post_id):
    body = request.json or {}
    emoji = str(body.get('emoji', ''))[:4]
    if not emoji:
        return jsonify({'error': 'Emoji required'}), 400
    username = _get_username()
    with _db() as conn:
        existing = conn.execute(
            'SELECT 1 FROM post_reactions WHERE post_id=? AND username=? AND emoji=?',
            (post_id, username, emoji)
        ).fetchone()
        if existing:
            conn.execute(
                'DELETE FROM post_reactions WHERE post_id=? AND username=? AND emoji=?',
                (post_id, username, emoji)
            )
        else:
            conn.execute(
                'INSERT INTO post_reactions (post_id,username,emoji) VALUES (?,?,?)',
                (post_id, username, emoji)
            )
        reactions = _rows_to_list(conn.execute(
            'SELECT emoji, GROUP_CONCAT(username) as users FROM post_reactions '
            'WHERE post_id=? GROUP BY emoji', (post_id,)
        ).fetchall())
    return jsonify({'ok': True, 'reactions': reactions})

# ══════════════════════════════════════════════════════════════════
#  SHOPPING LISTS (Shopping Lists)
# ══════════════════════════════════════════════════════════════════

@familyhub_bp.route('/lists', methods=['GET'])
def get_lists():
    with _db() as conn:
        lists = _rows_to_list(conn.execute(
            'SELECT * FROM shopping_lists ORDER BY created_at DESC'
        ).fetchall())
        for lst in lists:
            lst['items'] = _rows_to_list(conn.execute(
                'SELECT * FROM shopping_items WHERE list_id=? ORDER BY checked ASC, created_at DESC',
                (lst['id'],)
            ).fetchall())
    return jsonify({'ok': True, 'lists': lists})

@familyhub_bp.route('/lists', methods=['POST'])
def create_list():
    body = request.json or {}
    name = str(body.get('name', '')).strip()[:100]
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    lst = {
        'id': _uid(), 'name': name,
        'color': str(body.get('color', '#3b82f6'))[:20],
        'created_by': _get_username(), 'created_at': _now()
    }
    with _db() as conn:
        conn.execute(
            'INSERT INTO shopping_lists (id,name,color,created_by,created_at) '
            'VALUES (:id,:name,:color,:created_by,:created_at)', lst
        )
    lst['items'] = []
    return jsonify({'ok': True, 'list': lst})

@familyhub_bp.route('/lists/<list_id>', methods=['PUT'])
def update_list(list_id):
    body = request.json or {}
    with _db() as conn:
        sets, vals = [], []
        if 'name' in body:
            sets.append('name=?')
            vals.append(str(body['name']).strip()[:100])
        if 'color' in body:
            sets.append('color=?')
            vals.append(str(body['color'])[:20])
        if sets:
            vals.append(list_id)
            conn.execute(f'UPDATE shopping_lists SET {",".join(sets)} WHERE id=?', vals)
    return jsonify({'ok': True})

@familyhub_bp.route('/lists/<list_id>', methods=['DELETE'])
def delete_list(list_id):
    with _db() as conn:
        conn.execute('DELETE FROM shopping_items WHERE list_id=?', (list_id,))
        conn.execute('DELETE FROM shopping_lists WHERE id=?', (list_id,))
    return jsonify({'ok': True})

@familyhub_bp.route('/lists/<list_id>/items', methods=['POST'])
def add_item(list_id):
    body = request.json or {}
    name = str(body.get('name', '')).strip()[:200]
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    item = {
        'id': _uid(), 'list_id': list_id, 'name': name,
        'category': str(body.get('category', '')).strip()[:50],
        'checked': 0, 'added_by': _get_username(),
        'checked_by': '', 'created_at': _now()
    }
    with _db() as conn:
        conn.execute(
            'INSERT INTO shopping_items (id,list_id,name,category,checked,added_by,checked_by,created_at) '
            'VALUES (:id,:list_id,:name,:category,:checked,:added_by,:checked_by,:created_at)', item
        )
    return jsonify({'ok': True, 'item': item})

@familyhub_bp.route('/lists/<list_id>/items/<item_id>', methods=['PUT'])
def update_item(list_id, item_id):
    body = request.json or {}
    with _db() as conn:
        sets, vals = [], []
        if 'name' in body:
            sets.append('name=?')
            vals.append(str(body['name']).strip()[:200])
        if 'category' in body:
            sets.append('category=?')
            vals.append(str(body['category']).strip()[:50])
        if 'checked' in body:
            checked = 1 if body['checked'] else 0
            sets.append('checked=?')
            vals.append(checked)
            sets.append('checked_by=?')
            vals.append(_get_username() if checked else '')
        if sets:
            vals.append(item_id)
            conn.execute(f'UPDATE shopping_items SET {",".join(sets)} WHERE id=?', vals)
        item = conn.execute('SELECT * FROM shopping_items WHERE id=?', (item_id,)).fetchone()
    return jsonify({'ok': True, 'item': dict(item) if item else None})

@familyhub_bp.route('/lists/<list_id>/items/<item_id>', methods=['DELETE'])
def delete_item(list_id, item_id):
    with _db() as conn:
        conn.execute('DELETE FROM shopping_items WHERE id=? AND list_id=?', (item_id, list_id))
    return jsonify({'ok': True})

# ══════════════════════════════════════════════════════════════════
#  CHORES (Chores)
# ══════════════════════════════════════════════════════════════════

@familyhub_bp.route('/chores', methods=['GET'])
def get_chores():
    with _db() as conn:
        chores = _rows_to_list(conn.execute(
            'SELECT * FROM chores ORDER BY '
            'CASE status WHEN "pending" THEN 0 WHEN "in_progress" THEN 1 ELSE 2 END, '
            'due_date ASC, created_at DESC'
        ).fetchall())
        for ch in chores:
            ch['completions'] = _rows_to_list(conn.execute(
                'SELECT * FROM chore_completions WHERE chore_id=? ORDER BY completed_at DESC LIMIT 10',
                (ch['id'],)
            ).fetchall())
    return jsonify({'ok': True, 'chores': chores})

@familyhub_bp.route('/chores', methods=['POST'])
def create_chore():
    body = request.json or {}
    title = str(body.get('title', '')).strip()[:200]
    if not title:
        return jsonify({'error': 'Title is required'}), 400
    chore = {
        'id': _uid(), 'title': title,
        'assigned_to': str(body.get('assigned_to', '')).strip()[:50],
        'created_by': _get_username(),
        'recurrence': str(body.get('recurrence', 'once'))[:20],
        'status': 'pending', 'streak': 0,
        'due_date': str(body.get('due_date', ''))[:10],
        'created_at': _now(), 'updated_at': _now()
    }
    with _db() as conn:
        conn.execute(
            'INSERT INTO chores (id,title,assigned_to,created_by,recurrence,status,streak,due_date,created_at,updated_at) '
            'VALUES (:id,:title,:assigned_to,:created_by,:recurrence,:status,:streak,:due_date,:created_at,:updated_at)', chore
        )
    chore['completions'] = []
    return jsonify({'ok': True, 'chore': chore})

@familyhub_bp.route('/chores/<chore_id>', methods=['PUT'])
def update_chore(chore_id):
    body = request.json or {}
    with _db() as conn:
        sets, vals = [], []
        for field in ('title', 'assigned_to', 'recurrence', 'status', 'due_date'):
            if field in body:
                v = str(body[field]).strip()
                if field == 'title':
                    v = v[:200]
                elif field == 'status' and v not in ('pending', 'in_progress', 'done'):
                    continue
                elif field == 'recurrence' and v not in ('once', 'daily', 'weekly', 'monthly'):
                    continue
                sets.append(f'{field}=?')
                vals.append(v)
        if sets:
            sets.append('updated_at=?')
            vals.append(_now())
            vals.append(chore_id)
            conn.execute(f'UPDATE chores SET {",".join(sets)} WHERE id=?', vals)
        chore = conn.execute('SELECT * FROM chores WHERE id=?', (chore_id,)).fetchone()
    return jsonify({'ok': True, 'chore': dict(chore) if chore else None})

@familyhub_bp.route('/chores/<chore_id>', methods=['DELETE'])
def delete_chore(chore_id):
    with _db() as conn:
        conn.execute('DELETE FROM chore_completions WHERE chore_id=?', (chore_id,))
        conn.execute('DELETE FROM chores WHERE id=?', (chore_id,))
    return jsonify({'ok': True})

@familyhub_bp.route('/chores/<chore_id>/done', methods=['POST'])
def complete_chore(chore_id):
    username = _get_username()
    with _db() as conn:
        chore = conn.execute('SELECT * FROM chores WHERE id=?', (chore_id,)).fetchone()
        if not chore:
            return jsonify({'error': 'Not found'}), 404
        completion_id = _uid()
        conn.execute(
            'INSERT INTO chore_completions (id,chore_id,completed_by,completed_at) VALUES (?,?,?,?)',
            (completion_id, chore_id, username, _now())
        )
        new_streak = dict(chore)['streak'] + 1
        if dict(chore)['recurrence'] == 'once':
            conn.execute('UPDATE chores SET status="done", streak=?, updated_at=? WHERE id=?',
                         (new_streak, _now(), chore_id))
        else:
            conn.execute('UPDATE chores SET status="pending", streak=?, updated_at=? WHERE id=?',
                         (new_streak, _now(), chore_id))
        chore = dict(conn.execute('SELECT * FROM chores WHERE id=?', (chore_id,)).fetchone())
        chore['completions'] = _rows_to_list(conn.execute(
            'SELECT * FROM chore_completions WHERE chore_id=? ORDER BY completed_at DESC LIMIT 10',
            (chore_id,)
        ).fetchall())
    return jsonify({'ok': True, 'chore': chore})

# ══════════════════════════════════════════════════════════════════
#  FAMILY CALENDAR (Events)
# ══════════════════════════════════════════════════════════════════

@familyhub_bp.route('/events', methods=['GET'])
def get_events():
    month = request.args.get('month', '')  # YYYY-MM
    with _db() as conn:
        if month and len(month) == 7:
            events = _rows_to_list(conn.execute(
                'SELECT * FROM events WHERE event_date LIKE ? ORDER BY event_date, event_time',
                (month + '%',)
            ).fetchall())
        else:
            events = _rows_to_list(conn.execute(
                'SELECT * FROM events ORDER BY event_date DESC, event_time LIMIT 100'
            ).fetchall())
    return jsonify({'ok': True, 'events': events})

@familyhub_bp.route('/events', methods=['POST'])
def create_event():
    body = request.json or {}
    title = str(body.get('title', '')).strip()[:200]
    event_date = str(body.get('event_date', ''))[:10]
    if not title or not event_date:
        return jsonify({'error': 'Title and date are required'}), 400
    ev = {
        'id': _uid(), 'title': title,
        'description': str(body.get('description', '')).strip()[:2000],
        'event_date': event_date,
        'event_time': str(body.get('event_time', ''))[:5],
        'end_date': str(body.get('end_date', ''))[:10],
        'color': str(body.get('color', '#3b82f6'))[:20],
        'author': _get_username(), 'created_at': _now()
    }
    with _db() as conn:
        conn.execute(
            'INSERT INTO events (id,title,description,event_date,event_time,end_date,color,author,created_at) '
            'VALUES (:id,:title,:description,:event_date,:event_time,:end_date,:color,:author,:created_at)', ev
        )
    return jsonify({'ok': True, 'event': ev})

@familyhub_bp.route('/events/<event_id>', methods=['PUT'])
def update_event(event_id):
    body = request.json or {}
    with _db() as conn:
        sets, vals = [], []
        for field in ('title', 'description', 'event_date', 'event_time', 'end_date', 'color'):
            if field in body:
                v = str(body[field]).strip()
                if field == 'title':
                    v = v[:200]
                elif field == 'description':
                    v = v[:2000]
                sets.append(f'{field}=?')
                vals.append(v)
        if sets:
            vals.append(event_id)
            conn.execute(f'UPDATE events SET {",".join(sets)} WHERE id=?', vals)
        ev = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    return jsonify({'ok': True, 'event': dict(ev) if ev else None})

@familyhub_bp.route('/events/<event_id>', methods=['DELETE'])
def delete_event(event_id):
    with _db() as conn:
        conn.execute('DELETE FROM events WHERE id=?', (event_id,))
    return jsonify({'ok': True})

# ══════════════════════════════════════════════════════════════════
#  META (users list for assignment dropdowns)
# ══════════════════════════════════════════════════════════════════

@familyhub_bp.route('/users', methods=['GET'])
def hub_users():
    return jsonify({'ok': True, 'users': _get_system_users()})
