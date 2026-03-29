import sqlite3
import os
import json
import time
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import log_path

eventlog_bp = Blueprint('eventlog', __name__)

LOG_DIR = log_path()
DB_PATH = os.environ.get('EVENTLOG_DB_PATH', os.path.join(LOG_DIR, 'eventlog.db'))
JSON_LOG_FILE = os.path.join(LOG_DIR, 'eventlog.jsonl')

_socketio = None

LEVELS = ('debug', 'info', 'warning', 'error')
CATEGORIES = ('system', 'files', 'backup', 'docker', 'storage',
              'network', 'printer', 'security', 'error')

def get_db():
    from blueprints.db_pool import get_pooled_db
    return get_pooled_db(DB_PATH)

def init_eventlog(socketio_instance):
    global _socketio
    _socketio = socketio_instance
    os.makedirs(LOG_DIR, exist_ok=True)

    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            time TEXT,
            category TEXT,
            level TEXT,
            message TEXT,
            details TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ts ON events(ts)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_category ON events(category)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_level ON events(level)')
    conn.commit()

    # Check if empty and migrate
    try:
        count = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    except Exception:
        count = 0
    conn.close()

    if count == 0 and os.path.isfile(JSON_LOG_FILE):
        _migrate_from_json()

    _log_startup()

def _migrate_from_json():
    print(f"Migrating eventlog from {JSON_LOG_FILE}...")
    try:
        conn = get_db()
        with open(JSON_LOG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    e = json.loads(line)
                    conn.execute(
                        'INSERT INTO events (ts, time, category, level, message, details) VALUES (?, ?, ?, ?, ?, ?)',
                        (
                            e.get('ts', time.time()),
                            e.get('time', ''),
                            e.get('category', 'system'),
                            e.get('level', 'info'),
                            e.get('message', ''),
                            json.dumps(e.get('details')) if e.get('details') else None
                        )
                    )
                except (json.JSONDecodeError, ValueError, KeyError):
                    pass
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Eventlog migration failed: {e}")

def _log_startup():
    startup_details = {'pid': os.getpid()}

    conn = get_db()
    # Find last shutdown
    # Assuming 'system' 'warning' and 'zatrzymany' in message
    try:
        rows = conn.execute('''
            SELECT ts, message FROM events
            WHERE category='system' AND level='warning'
            ORDER BY ts DESC LIMIT 100
        ''').fetchall()

        shutdown_ts = 0
        for r in rows:
            if 'zatrzymany' in r['message']:
                shutdown_ts = r['ts']
                break

        if shutdown_ts:
            elapsed = int(time.time() - shutdown_ts)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            if h:
                startup_details['downtime'] = f'{h}h {m}m {s}s'
            elif m:
                startup_details['downtime'] = f'{m}m {s}s'
            else:
                startup_details['downtime'] = f'{s}s'

        # Check for restart trigger (last 5 mins)
        now_ts = time.time()
        rows = conn.execute('''
            SELECT ts, message, details FROM events
            WHERE category='system' AND level='warning' AND ts > ?
            ORDER BY ts DESC
        ''', (now_ts - 300,)).fetchall()

        for r in rows:
            if r['message'] == 'Restart ethos z ticket watchera':
                details = json.loads(r['details']) if r['details'] else {}
                if 'reason' in details:
                    startup_details['reason'] = details['reason']
                if 'ticket_id' in details:
                    startup_details['ticket_id'] = details['ticket_id']
                break
    except Exception as e:
        print(f"Startup log error: {e}")
    finally:
        conn.close()

    log('system', 'info', 'EthOS started', details=startup_details)

def log(category, level, message, details=None):
    if level not in LEVELS: level = 'info'

    ts = time.time()
    t_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO events (ts, time, category, level, message, details) VALUES (?, ?, ?, ?, ?, ?)',
            (ts, t_str, category, level, message, json.dumps(details) if details else None)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

    event = {
        'ts': ts,
        'time': t_str,
        'category': category,
        'level': level,
        'message': message,
        'details': details
    }

    if _socketio:
        try:
            _socketio.emit('eventlog_new', event)
        except Exception:
            pass

    if level == 'error':
        try:
            from blueprints.remote_log import send_report, _config, _last_send_ts
            import time as _t
            if (_config.get('enabled') and _config.get('send_on_error')
                    and _t.time() - _last_send_ts > 300):
                threading.Thread(
                    target=send_report, args=('error',), daemon=True
                ).start()
        except Exception:
            pass

    # Notification channels hook
    try:
        from blueprints.notifications import notify_event
        notify_event(category, level, message)
    except Exception:
        pass

@eventlog_bp.route('/api/eventlog', methods=['POST'])
def eventlog_create():
    data = request.get_json(silent=True) or {}
    category = data.get('category', 'system')
    level = data.get('level', 'info')
    message = data.get('message', '').strip()
    detail = data.get('detail') or data.get('details')

    if not message:
        return jsonify({'error': 'message is required'}), 400
    if category not in CATEGORIES: category = 'system'
    if level not in LEVELS: level = 'info'

    log(category, level, message, details=detail)
    return jsonify({'ok': True}), 201

@eventlog_bp.route('/api/eventlog')
def eventlog_list():
    try:
        limit = min(int(request.args.get('limit', 100)), 1000)
    except (ValueError, TypeError):
        limit = 100
    try:
        offset = int(request.args.get('offset', 0))
    except (ValueError, TypeError):
        offset = 0
    category = request.args.get('category', '')
    level = request.args.get('level', '')
    search = request.args.get('search', '').lower()

    query = "SELECT * FROM events WHERE 1=1"
    params = []

    if category:
        cats = category.split(',')
        query += " AND category IN ({})".format(','.join(['?']*len(cats)))
        params.extend(cats)

    if level:
        lvls = level.split(',')
        query += " AND level IN ({})".format(','.join(['?']*len(lvls)))
        params.extend(lvls)

    if search:
        query += " AND (lower(message) LIKE ? OR lower(details) LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])

    # Count total first
    conn = get_db()
    try:
        # Use simple count for performance if no filters, else subquery
        if not (category or level or search):
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        else:
            count_query = f"SELECT COUNT(*) FROM ({query})"
            total = conn.execute(count_query, params).fetchone()[0]

        query += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()

        events = []
        for r in rows:
            d = dict(r)
            if d['details']:
                try:
                    d['details'] = json.loads(d['details'])
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
            events.append(d)
    finally:
        conn.close()

    return jsonify({'events': events, 'total': total, 'limit': limit, 'offset': offset})

@eventlog_bp.route('/api/eventlog/clear', methods=['POST'])
def eventlog_clear():
    conn = get_db()
    conn.execute('DELETE FROM events')
    conn.commit()
    conn.close()
    log('system', 'info', 'Event log cleared')
    return jsonify({'ok': True})

@eventlog_bp.route('/api/eventlog/stats')
def eventlog_stats():
    conn = get_db()
    try:
        by_category = {}
        rows = conn.execute('SELECT category, COUNT(*) as c FROM events GROUP BY category').fetchall()
        for r in rows:
            by_category[r['category']] = r['c']

        by_level = {}
        rows = conn.execute('SELECT level, COUNT(*) as c FROM events GROUP BY level').fetchall()
        for r in rows:
            by_level[r['level']] = r['c']

        total = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    finally:
        conn.close()

    return jsonify({'total': total, 'by_category': by_category, 'by_level': by_level})
