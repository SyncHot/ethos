"""
EthOS — Event Log (Dziennik zdarzeń)
Centralized event logging system for debugging and monitoring.
Logs to file + exposes API for the frontend viewer app.

Endpoints:
  GET  /api/eventlog                   — list events (filterable)
  POST /api/eventlog                   — log event externally (agents, ticket_watcher)
  POST /api/eventlog/clear             — clear all events
  GET  /api/eventlog/stats             — counts by category/level
"""

import os
import json
import time
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify
from collections import deque

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import log_path

eventlog_bp = Blueprint('eventlog', __name__)

LOG_DIR = log_path()
LOG_FILE = os.path.join(LOG_DIR, 'eventlog.jsonl')
MAX_MEMORY_EVENTS = 1000
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB rotate

_lock = threading.Lock()
_events = deque(maxlen=MAX_MEMORY_EVENTS)
_socketio = None


LEVELS = ('debug', 'info', 'warning', 'error')
CATEGORIES = ('system', 'files', 'backup', 'docker', 'storage',
              'network', 'printer', 'security', 'error')


def init_eventlog(socketio_instance):
    global _socketio
    _socketio = socketio_instance
    os.makedirs(LOG_DIR, exist_ok=True)
    _load_recent()

    startup_details = {'pid': os.getpid()}

    # Find last shutdown event to calculate downtime
    with _lock:
        events_copy = list(_events)
    for ev in reversed(events_copy):
        if (ev.get('category') == 'system' and ev.get('level') == 'warning'
                and 'zatrzymany' in ev.get('message', '')):
            shutdown_ts = ev.get('ts', 0)
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
            break

    log('system', 'info', 'EthOS uruchomiony', details=startup_details)


def _load_recent():
    if not os.path.isfile(LOG_FILE):
        return
    try:
        lines = []
        with open(LOG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)
        for line in lines[-MAX_MEMORY_EVENTS:]:
            try:
                _events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except Exception:
        pass


def _rotate_if_needed():
    try:
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_FILE_SIZE:
            rotated = LOG_FILE + '.1'
            if os.path.isfile(rotated):
                os.remove(rotated)
            os.rename(LOG_FILE, rotated)
    except Exception:
        pass


def log(category, level, message, details=None):
    """
    Log an event.
    category: system|files|backup|docker|storage|network|printer|error
    level: debug|info|warning|error
    message: Human-readable description
    details: Optional dict with extra data
    """
    if level not in LEVELS:
        level = 'info'

    event = {
        'ts': time.time(),
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'category': category,
        'level': level,
        'message': message,
    }
    if details:
        event['details'] = details

    with _lock:
        _events.append(event)
        try:
            _rotate_if_needed()
            with open(LOG_FILE, 'a') as f:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')
        except Exception:
            pass

    if _socketio:
        try:
            _socketio.emit('eventlog_new', event)
        except Exception:
            pass

    # Trigger remote log on errors (throttled: max once per 5 min)
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


# ─── API ─────────────────────────────────────────────────────

@eventlog_bp.route('/api/eventlog', methods=['POST'])
def eventlog_create():
    """POST /api/eventlog — log an event from external callers (e.g. ticket_watcher, agents)
    Body: {"category": "system", "level": "warning", "message": "...", "detail": {...}}
    """
    data = request.get_json(silent=True) or {}
    category = data.get('category', 'system')
    level = data.get('level', 'info')
    message = data.get('message', '').strip()
    detail = data.get('detail') or data.get('details')

    if not message:
        return jsonify({'error': 'message is required'}), 400
    if category not in CATEGORIES:
        category = 'system'
    if level not in LEVELS:
        level = 'info'

    log(category, level, message, details=detail)
    return jsonify({'ok': True}), 201


@eventlog_bp.route('/api/eventlog')
def eventlog_list():
    """GET /api/eventlog?limit=100&offset=0&category=files&level=error&search=text"""
    limit = min(int(request.args.get('limit', 100)), 1000)
    offset = int(request.args.get('offset', 0))
    category = request.args.get('category', '')
    level = request.args.get('level', '')
    search = request.args.get('search', '').lower()

    with _lock:
        all_events = list(_events)

    if category:
        cats = set(category.split(','))
        all_events = [e for e in all_events if e.get('category') in cats]
    if level:
        lvls = set(level.split(','))
        all_events = [e for e in all_events if e.get('level') in lvls]
    if search:
        all_events = [e for e in all_events
                      if search in e.get('message', '').lower()
                      or search in json.dumps(e.get('details', {})).lower()]

    all_events.reverse()
    total = len(all_events)
    page = all_events[offset:offset + limit]

    return jsonify({'events': page, 'total': total, 'limit': limit, 'offset': offset})


@eventlog_bp.route('/api/eventlog/clear', methods=['POST'])
def eventlog_clear():
    with _lock:
        _events.clear()
        try:
            if os.path.isfile(LOG_FILE):
                os.remove(LOG_FILE)
            rotated = LOG_FILE + '.1'
            if os.path.isfile(rotated):
                os.remove(rotated)
        except Exception:
            pass
    log('system', 'info', 'Dziennik zdarzeń wyczyszczony')
    return jsonify({'ok': True})


@eventlog_bp.route('/api/eventlog/stats')
def eventlog_stats():
    with _lock:
        all_events = list(_events)

    by_category = {}
    by_level = {}
    for e in all_events:
        cat = e.get('category', 'unknown')
        lvl = e.get('level', 'info')
        by_category[cat] = by_category.get(cat, 0) + 1
        by_level[lvl] = by_level.get(lvl, 0) + 1

    return jsonify({'total': len(all_events), 'by_category': by_category, 'by_level': by_level})
