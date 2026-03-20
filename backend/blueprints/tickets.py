"""
EthOS – Kanban / Project Management (Tickets) Blueprint
"""

import os
import sys
import re
import glob
import time
import uuid
import subprocess
import threading
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask import Blueprint, request, jsonify, g, send_file
from werkzeug.utils import secure_filename
from host import data_path as _data_path
from utils import load_json as _load_json, save_json as _save_json
from ethos_packages_data import _ETHOS_PACKAGES

tickets_bp = Blueprint('tickets', __name__, url_prefix='/api/tickets')

TICKETS_FILE = _data_path('tickets.json')
ATTACHMENTS_DIR = _data_path('ticket_attachments')
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

_lock = threading.Lock()
_socketio = None

def init_tickets(sio):
    global _socketio
    _socketio = sio

def _emit(event_type, project_id, payload=None):
    if _socketio:
        _socketio.emit('tickets_event', {
            'type': event_type,
            'project_id': project_id,
            **(payload or {}),
            'ts': time.time()
        })

VALID_PRIORITIES = ('critical', 'high', 'medium', 'low')
VALID_TYPES = ('task', 'bug', 'epic', 'subtask')
VALID_COMPLEXITIES = ('simple', 'medium', 'complex')
DEFAULT_COLUMNS = ["Backlog", "Do zrobienia", "W trakcie", "Review", "Gotowe"]

MAX_TITLE = 200
MAX_DESCRIPTION = 10000
MAX_COMMENT = 5000
MAX_LABELS = 10
MAX_COMMENTS = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gen_id(prefix=''):
    return prefix + uuid.uuid4().hex[:12]


def _now():
    return time.time()


def _load():
    return _load_json(TICKETS_FILE, {'projects': [], 'tickets': []})


def _save(data):
    _save_json(TICKETS_FILE, data)


def _strip(val, maxlen):
    if not isinstance(val, str):
        return ''
    return val.strip()[:maxlen]


def _is_member(project):
    """Return True if current user may access the project."""
    if g.role == 'admin':
        return True
    return g.username in project.get('members', []) or g.username == project.get('owner')


def _find_project(data, project_id):
    for p in data['projects']:
        if p['id'] == project_id:
            return p
    return None


def _find_ticket(data, ticket_id):
    for t in data['tickets']:
        if t['id'] == ticket_id:
            return t
    return None


def _can_manage_project(project):
    """Owner or admin."""
    return g.role == 'admin' or g.username == project.get('owner')


# ---------------------------------------------------------------------------
# Projects CRUD
# ---------------------------------------------------------------------------

@tickets_bp.route('/projects', methods=['GET'])
def list_projects():
    with _lock:
        data = _load()
    projects = [p for p in data['projects'] if _is_member(p)]
    all_tickets = data.get('tickets', [])
    for p in projects:
        pt = [t for t in all_tickets if t['project_id'] == p['id']]
        p['ticket_count'] = len(pt)
        p['in_progress_count'] = sum(1 for t in pt if t.get('column') == 'W trakcie')
        p['done_count'] = sum(1 for t in pt if t.get('column') == 'Gotowe')
    return jsonify({'projects': projects})


@tickets_bp.route('/projects', methods=['POST'])
def create_project():
    body = request.get_json(silent=True) or {}
    name = _strip(body.get('name', ''), MAX_TITLE)
    if not name:
        return jsonify({'error': 'Project name is required'}), 400

    description = _strip(body.get('description', ''), MAX_DESCRIPTION)
    color = _strip(body.get('color', '#3b82f6'), 20)
    members = body.get('members', [])
    if not isinstance(members, list):
        members = []
    members = [str(m).strip() for m in members if str(m).strip()]
    if g.username not in members:
        members.insert(0, g.username)

    now = _now()
    project = {
        'id': _gen_id(),
        'name': name,
        'description': description,
        'owner': g.username,
        'members': members,
        'columns': list(DEFAULT_COLUMNS),
        'color': color,
        'copilot_enabled': bool(body.get('copilot_enabled', False)),
        'created': now,
        'updated': now,
    }

    with _lock:
        data = _load()
        data['projects'].append(project)
        _save(data)

    _emit('project_created', project['id'], {'project': project})
    return jsonify({'ok': True, 'item': project}), 201


@tickets_bp.route('/projects/<project_id>', methods=['GET'])
def get_project(project_id):
    with _lock:
        data = _load()

    project = _find_project(data, project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    tickets = [t for t in data['tickets'] if t['project_id'] == project_id]
    tickets.sort(key=lambda t: t.get('order', 0))
    return jsonify({'project': project, 'tickets': tickets})


@tickets_bp.route('/projects/<project_id>', methods=['PUT'])
def update_project(project_id):
    body = request.get_json(silent=True) or {}

    with _lock:
        data = _load()
        project = _find_project(data, project_id)
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        if not _can_manage_project(project):
            return jsonify({'error': 'Access denied'}), 403

        if 'name' in body:
            name = _strip(body['name'], MAX_TITLE)
            if not name:
                return jsonify({'error': 'Project name cannot be empty'}), 400
            project['name'] = name
        if 'description' in body:
            project['description'] = _strip(body['description'], MAX_DESCRIPTION)
        if 'color' in body:
            project['color'] = _strip(body['color'], 20)
        if 'columns' in body:
            cols = body['columns']
            if isinstance(cols, list) and cols:
                project['columns'] = [_strip(c, MAX_TITLE) for c in cols if _strip(c, MAX_TITLE)]
        if 'members' in body:
            members = body['members']
            if isinstance(members, list):
                members = [str(m).strip() for m in members if str(m).strip()]
                if project['owner'] not in members:
                    members.insert(0, project['owner'])
                project['members'] = members

        if 'copilot_enabled' in body:
            project['copilot_enabled'] = bool(body['copilot_enabled'])

        project['updated'] = _now()
        _save(data)

    _emit('project_updated', project_id, {'project': project})
    return jsonify({'ok': True, 'item': project})


@tickets_bp.route('/projects/<project_id>', methods=['DELETE'])
def delete_project(project_id):
    with _lock:
        data = _load()
        project = _find_project(data, project_id)
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        if not _can_manage_project(project):
            return jsonify({'error': 'Access denied'}), 403

        data['projects'] = [p for p in data['projects'] if p['id'] != project_id]
        data['tickets'] = [t for t in data['tickets'] if t['project_id'] != project_id]
        _save(data)

    _emit('project_deleted', project_id)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Tickets CRUD
# ---------------------------------------------------------------------------

@tickets_bp.route('/projects/<project_id>/tickets', methods=['GET'])
def list_tickets(project_id):
    with _lock:
        data = _load()

    project = _find_project(data, project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    tickets = [t for t in data['tickets'] if t['project_id'] == project_id]

    # Filters
    col = request.args.get('column')
    assignee = request.args.get('assignee')
    priority = request.args.get('priority')
    label = request.args.get('label')
    search = request.args.get('search', '').strip().lower()

    if col:
        tickets = [t for t in tickets if t.get('column') == col]
    if assignee:
        tickets = [t for t in tickets if t.get('assignee') == assignee]
    if priority:
        tickets = [t for t in tickets if t.get('priority') == priority]
    if label:
        tickets = [t for t in tickets if label in t.get('labels', [])]
    if search:
        tickets = [t for t in tickets if search in t.get('title', '').lower()
                    or search in t.get('description', '').lower()]

    tickets.sort(key=lambda t: t.get('order', 0))
    return jsonify({'tickets': tickets})


@tickets_bp.route('/projects/<project_id>/tickets', methods=['POST'])
def create_ticket(project_id):
    body = request.get_json(silent=True) or {}

    title = _strip(body.get('title', ''), MAX_TITLE)
    if not title:
        return jsonify({'error': 'Ticket title is required'}), 400

    description = _strip(body.get('description', ''), MAX_DESCRIPTION)
    priority = body.get('priority', 'medium')
    if priority not in VALID_PRIORITIES:
        priority = 'medium'

    ticket_type = body.get('type', 'task')
    if ticket_type not in VALID_TYPES:
        ticket_type = 'task'

    complexity = body.get('complexity', 'medium')
    if complexity not in VALID_COMPLEXITIES:
        complexity = 'medium'

    assignee = _strip(body.get('assignee', ''), MAX_TITLE)
    labels = body.get('labels', [])
    if not isinstance(labels, list):
        labels = []
    labels = [_strip(l, 100) for l in labels if _strip(l, 100)][:MAX_LABELS]

    with _lock:
        data = _load()
        project = _find_project(data, project_id)
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        if not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403

        column = body.get('column', '')
        if not column or column not in project.get('columns', []):
            column = project['columns'][0] if project.get('columns') else 'Backlog'

        # Shift existing tickets in the target column down
        for t in data['tickets']:
            if t['project_id'] == project_id and t['column'] == column:
                t['order'] = t.get('order', 0) + 1

        now = _now()
        ticket = {
            'id': _gen_id('t_'),
            'project_id': project_id,
            'title': title,
            'description': description,
            'column': column,
            'priority': priority,
            'assignee': assignee,
            'type': ticket_type,
            'complexity': complexity,
            'reporter': g.username,
            'labels': labels,
            'comments': [],
            'order': 0,
            'created': now,
            'updated': now,
        }

        data['tickets'].append(ticket)
        _save(data)

    _emit('ticket_created', ticket['project_id'], {'ticket': ticket})
    return jsonify({'ok': True, 'item': ticket}), 201


@tickets_bp.route('/tickets/<ticket_id>', methods=['PUT'])
def update_ticket(ticket_id):
    body = request.get_json(silent=True) or {}

    with _lock:
        data = _load()
        ticket = _find_ticket(data, ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        project = _find_project(data, ticket['project_id'])
        if not project or not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403

        if 'title' in body:
            title = _strip(body['title'], MAX_TITLE)
            if not title:
                return jsonify({'error': 'Title cannot be empty'}), 400
            ticket['title'] = title
        if 'description' in body:
            ticket['description'] = _strip(body['description'], MAX_DESCRIPTION)
        if 'column' in body:
            col = body['column']
            if col in project.get('columns', []):
                ticket['column'] = col
        if 'priority' in body:
            pri = body['priority']
            if pri in VALID_PRIORITIES:
                ticket['priority'] = pri
        if 'type' in body:
            ttype = body['type']
            if ttype in VALID_TYPES:
                ticket['type'] = ttype
        if 'complexity' in body:
            comp = body['complexity']
            if comp in VALID_COMPLEXITIES:
                ticket['complexity'] = comp
        if 'assignee' in body:
            ticket['assignee'] = _strip(body['assignee'], MAX_TITLE)
        if 'labels' in body:
            labels = body['labels']
            if isinstance(labels, list):
                ticket['labels'] = [_strip(l, 100) for l in labels if _strip(l, 100)][:MAX_LABELS]

        ticket['updated'] = _now()
        _save(data)

    _emit('ticket_updated', ticket['project_id'], {'ticket': ticket})
    return jsonify({'ok': True, 'item': ticket})


@tickets_bp.route('/tickets/<ticket_id>', methods=['DELETE'])
def delete_ticket(ticket_id):
    with _lock:
        data = _load()
        ticket = _find_ticket(data, ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        project = _find_project(data, ticket['project_id'])
        if not project or not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403

        pid = ticket['project_id']
        data['tickets'] = [t for t in data['tickets'] if t['id'] != ticket_id]
        _save(data)

    _emit('ticket_deleted', pid, {'ticket_id': ticket_id})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Ticket Actions
# ---------------------------------------------------------------------------

@tickets_bp.route('/tickets/<ticket_id>/move', methods=['PUT'])
def move_ticket(ticket_id):
    body = request.get_json(silent=True) or {}
    column = body.get('column', '')
    order = body.get('order')

    if not column:
        return jsonify({'error': 'Target column is required'}), 400

    with _lock:
        data = _load()
        ticket = _find_ticket(data, ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        project = _find_project(data, ticket['project_id'])
        if not project or not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403
        if column not in project.get('columns', []):
            return jsonify({'error': 'Invalid column'}), 400

        ticket['column'] = column
        ticket['updated'] = _now()

        # Cascade: if this ticket is an epic, move all children too
        epic_label = 'epic:' + ticket_id
        children_moved = []
        for t in data['tickets']:
            if t['id'] == ticket_id:
                continue
            if epic_label in t.get('labels', []):
                t['column'] = column
                t['updated'] = _now()
                children_moved.append(t)

        # Collect tickets in target column (excluding the moved one)
        moved_ids = {ticket_id} | {c['id'] for c in children_moved}
        col_tickets = sorted(
            [t for t in data['tickets']
             if t['project_id'] == ticket['project_id']
             and t['column'] == column and t['id'] not in moved_ids],
            key=lambda t: t.get('order', 0),
        )

        if order is None or not isinstance(order, int) or order < 0:
            order = 0
        if order > len(col_tickets):
            order = len(col_tickets)

        col_tickets.insert(order, ticket)
        # Place children right after the epic
        for ci, child in enumerate(children_moved):
            col_tickets.insert(order + 1 + ci, child)

        for idx, t in enumerate(col_tickets):
            t['order'] = idx

        _save(data)

    _emit('ticket_moved', ticket['project_id'], {'ticket': ticket, 'children': [c['id'] for c in children_moved]})
    return jsonify({'ok': True, 'item': ticket, 'children_moved': len(children_moved)})


@tickets_bp.route('/tickets/<ticket_id>/comments', methods=['POST'])
def add_comment(ticket_id):
    body = request.get_json(silent=True) or {}
    text = _strip(body.get('text', ''), MAX_COMMENT)
    if not text:
        return jsonify({'error': 'Comment text is required'}), 400

    with _lock:
        data = _load()
        ticket = _find_ticket(data, ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        project = _find_project(data, ticket['project_id'])
        if not project or not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403

        if len(ticket.get('comments', [])) >= MAX_COMMENTS:
            return jsonify({'error': 'Comment limit reached'}), 400

        comment = {
            'id': _gen_id('c_'),
            'author': g.username,
            'text': text,
            'created': _now(),
        }
        ticket.setdefault('comments', []).append(comment)
        ticket['updated'] = _now()
        _save(data)

    _emit('comment_added', ticket['project_id'], {'ticket_id': ticket_id, 'comment': comment})
    return jsonify({'ok': True, 'item': comment}), 201


@tickets_bp.route('/tickets/<ticket_id>/comments/<comment_id>', methods=['DELETE'])
def delete_comment(ticket_id, comment_id):
    with _lock:
        data = _load()
        ticket = _find_ticket(data, ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        project = _find_project(data, ticket['project_id'])
        if not project or not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403

        comments = ticket.get('comments', [])
        comment = next((c for c in comments if c['id'] == comment_id), None)
        if not comment:
            return jsonify({'error': 'Comment not found'}), 404

        if comment.get('author') != g.username and g.role != 'admin':
            return jsonify({'error': 'Access denied'}), 403

        ticket['comments'] = [c for c in comments if c['id'] != comment_id]
        ticket['updated'] = _now()
        _save(data)

    _emit('comment_deleted', ticket['project_id'], {'ticket_id': ticket_id, 'comment_id': comment_id})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Bulk / Utility
# ---------------------------------------------------------------------------

@tickets_bp.route('/projects/<project_id>/reorder', methods=['PUT'])
def reorder_tickets(project_id):
    body = request.get_json(silent=True) or {}
    column = body.get('column', '')
    ticket_ids = body.get('ticket_ids', [])

    if not column or not isinstance(ticket_ids, list):
        return jsonify({'error': 'Column and ticket_ids are required'}), 400

    with _lock:
        data = _load()
        project = _find_project(data, project_id)
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        if not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403
        if column not in project.get('columns', []):
            return jsonify({'error': 'Invalid column'}), 400

        id_set = set(ticket_ids)
        for idx, tid in enumerate(ticket_ids):
            ticket = _find_ticket(data, tid)
            if ticket and ticket['project_id'] == project_id and ticket['column'] == column:
                ticket['order'] = idx

        # Tickets in the column that weren't in the list keep order after the listed ones
        max_order = len(ticket_ids)
        for t in data['tickets']:
            if (t['project_id'] == project_id and t['column'] == column
                    and t['id'] not in id_set):
                t['order'] = max_order
                max_order += 1

        _save(data)

    return jsonify({'ok': True})


@tickets_bp.route('/stats/<project_id>', methods=['GET'])
def project_stats(project_id):
    with _lock:
        data = _load()

    project = _find_project(data, project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    tickets = [t for t in data['tickets'] if t['project_id'] == project_id]

    by_column = {}
    by_priority = {}
    by_assignee = {}

    for t in tickets:
        col = t.get('column', '')
        by_column[col] = by_column.get(col, 0) + 1

        pri = t.get('priority', 'medium')
        by_priority[pri] = by_priority.get(pri, 0) + 1

        assignee = t.get('assignee', '') or 'unassigned'
        by_assignee[assignee] = by_assignee.get(assignee, 0) + 1

    return jsonify({
        'total': len(tickets),
        'by_column': by_column,
        'by_priority': by_priority,
        'by_assignee': by_assignee,
    })

# ---------------------------------------------------------------------------
# Copilot Integration
# ---------------------------------------------------------------------------

PRIORITY_ORDER = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}

@tickets_bp.route('/copilot/queue', methods=['GET'])
def copilot_queue():
    """Returns actionable tickets from copilot-enabled projects."""
    with _lock:
        data = _load()

    queue = []
    for project in data['projects']:
        if not project.get('copilot_enabled', False):
            continue
        if not _is_member(project):
            continue

        proj_tickets = [t for t in data['tickets'] if t['project_id'] == project['id']]

        for t in proj_tickets:
            col = t.get('column', '')
            if col in ('Do zrobienia', 'W trakcie', 'QA'):
                comments = t.get('comments', [])
                last_comment = None
                if comments:
                    lc = comments[-1]
                    last_comment = {
                        'author': lc.get('author', ''),
                        'text': lc.get('text', ''),
                        'created': lc.get('created', 0),
                    }
                queue.append({
                    'id': t['id'],
                    'title': t['title'],
                    'description': t.get('description', ''),
                    'priority': t.get('priority', 'medium'),
                    'type': t.get('type', 'task'),
                    'complexity': t.get('complexity', 'medium'),
                    'column': col,
                    'assignee': t.get('assignee', ''),
                    'labels': t.get('labels', []),
                    'project_id': project['id'],
                    'project_name': project['name'],
                    'last_comment': last_comment,
                })

    queue.sort(key=lambda t: (
        0 if t['column'] == 'Do zrobienia' else (1 if t['column'] == 'W trakcie' else 2),
        PRIORITY_ORDER.get(t['priority'], 2),
    ))

    return jsonify({'queue': queue, 'total': len(queue)})


# ---------------------------------------------------------------------------
# Copilot logs
# ---------------------------------------------------------------------------

COPILOT_LOG_DIR = '/opt/ethos/logs/copilot_tickets'
WATCHER_LOG_FILE = '/opt/ethos/logs/ticket_watcher_new.log'
WATCHER_LOCK_FILE = '/tmp/.ethos_watcher_executing'


def _parse_log_model(filepath):
    """Extract model name from a copilot log header line (=== Model: xxx ===)."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith('=== Model:'):
                    m = re.search(r'Model:\s*(\S+)', line)
                    if m:
                        return m.group(1)
                if not line.startswith('==='):
                    break
    except Exception:
        pass
    return None

@tickets_bp.route('/tickets/<ticket_id>/copilot-logs', methods=['GET'])
def copilot_logs(ticket_id):
    """List copilot execution logs for a ticket."""
    with _lock:
        data = _load()
    ticket = _find_ticket(data, ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = _find_project(data, ticket.get('project_id', ''))
    if project and not _is_member(project):
        return jsonify({'error': 'Forbidden'}), 403

    safe_id = re.sub(r'[^a-zA-Z0-9_]', '', ticket_id)
    pattern = os.path.join(COPILOT_LOG_DIR, f'{safe_id}_*.log')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    logs = []
    for f in files:
        name = os.path.basename(f)
        is_qa = '_qa_' in name
        is_prompt = '_prompt' in name
        if is_prompt:
            continue  # skip prompt dump files
        try:
            ts = int(re.search(r'_(\d{10,})', name).group(1))
        except (AttributeError, ValueError):
            ts = int(os.path.getmtime(f))
        model = _parse_log_model(f)
        logs.append({
            'filename': name,
            'type': 'qa' if is_qa else 'dev',
            'timestamp': ts,
            'size': os.path.getsize(f),
            'model': model,
        })

    return jsonify({'logs': logs})


@tickets_bp.route('/tickets/<ticket_id>/copilot-logs/<filename>', methods=['GET'])
def copilot_log_content(ticket_id, filename):
    """Return content of a specific copilot log file."""
    with _lock:
        data = _load()
    ticket = _find_ticket(data, ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = _find_project(data, ticket.get('project_id', ''))
    if project and not _is_member(project):
        return jsonify({'error': 'Forbidden'}), 403

    safe_id = re.sub(r'[^a-zA-Z0-9_]', '', ticket_id)
    safe_name = re.sub(r'[^a-zA-Z0-9_.\-]', '', filename)
    if not safe_name.startswith(safe_id) or '..' in safe_name:
        return jsonify({'error': 'Invalid filename'}), 400

    path = os.path.join(COPILOT_LOG_DIR, safe_name)
    if not os.path.isfile(path):
        return jsonify({'error': 'Log not found'}), 404

    tail = request.args.get('tail', type=int)
    offset = request.args.get('offset', 0, type=int)
    try:
        file_size = os.path.getsize(path)
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            if offset > 0:
                f.seek(offset)
            content = f.read()
        if tail and tail > 0 and offset == 0:
            lines = content.splitlines()
            content = '\n'.join(lines[-tail:])
        return jsonify({
            'content': content,
            'filename': safe_name,
            'size': file_size,
            'offset': file_size,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Ticket Watcher service control ───────────────────────────────────────

_WATCHER_UNIT = 'ethos-ticket-watcher.service'

@tickets_bp.route('/watcher/status', methods=['GET'])
def watcher_status():
    """Return current status of the ticket watcher systemd service."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        active = subprocess.run(
            ['systemctl', 'is-active', _WATCHER_UNIT],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        enabled = subprocess.run(
            ['systemctl', 'is-enabled', _WATCHER_UNIT],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        # Get uptime / last status line
        result = subprocess.run(
            ['systemctl', 'show', _WATCHER_UNIT,
             '--property=ActiveState,SubState,ActiveEnterTimestamp,MainPID'],
            capture_output=True, text=True, timeout=5
        )
        props = {}
        for line in result.stdout.strip().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                props[k] = v
        return jsonify({
            'active': active,
            'enabled': enabled,
            'pid': props.get('MainPID', ''),
            'state': props.get('ActiveState', ''),
            'substate': props.get('SubState', ''),
            'since': props.get('ActiveEnterTimestamp', ''),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@tickets_bp.route('/watcher/executing', methods=['GET'])
def watcher_executing():
    """Return current execution state from the watcher lock file."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    if not os.path.isfile(WATCHER_LOCK_FILE):
        return jsonify({'executing': False})
    try:
        import json as _json
        with open(WATCHER_LOCK_FILE, 'r') as f:
            lock = _json.load(f)
        tid = lock.get('ticket_id', '')
        model_info = lock.get('model') or {}
        started = lock.get('started', 0)
        elapsed = time.time() - started if started else 0
        pid = lock.get('copilot_pid')
        # Check if the process is actually alive
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, OSError):
                pass
        return jsonify({
            'executing': alive,
            'ticket_id': tid,
            'model': model_info.get('model', '') if isinstance(model_info, dict) else '',
            'model_label': model_info.get('label', '') if isinstance(model_info, dict) else '',
            'started': started,
            'elapsed': round(elapsed),
            'pid': pid,
            'qa_cycle': lock.get('qa_cycle', 0),
            'log_file': os.path.basename(lock.get('log_file', '')),
        })
    except Exception:
        return jsonify({'executing': False})


@tickets_bp.route('/watcher/control', methods=['POST'])
def watcher_control():
    """Start, stop, or restart the ticket watcher service."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    if action not in ('start', 'stop', 'restart'):
        return jsonify({'error': 'Invalid action. Use start, stop, or restart.'}), 400
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', action, _WATCHER_UNIT],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return jsonify({'error': result.stderr.strip() or f'{action} failed'}), 500
        # Brief wait for state to settle
        time.sleep(0.5)
        # Return fresh status
        active = subprocess.run(
            ['systemctl', 'is-active', _WATCHER_UNIT],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return jsonify({'ok': True, 'action': action, 'active': active})
    except subprocess.TimeoutExpired:
        return jsonify({'error': f'{action} timed out'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


_PREFLIGHT_SCRIPT = '/opt/ethos/tools/preflight_check.py'

@tickets_bp.route('/preflight', methods=['GET'])
def preflight_check():
    """Run preflight validation before service restart."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    quick = request.args.get('quick', '').lower() in ('1', 'true', 'yes')
    cmd = [sys.executable, _PREFLIGHT_SCRIPT, '--json']
    if quick:
        cmd.append('--quick')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        import json as _json
        data = _json.loads(r.stdout)
        return jsonify(data), 200 if data.get('passed') else 422
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Preflight timed out'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@tickets_bp.route('/projects/<project_id>/bug-hunt', methods=['POST'])
def bug_hunt(project_id):
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Filter candidates: non-docker Ethos packages
    candidates = [p for p in _ETHOS_PACKAGES if p['id'] != 'docker-manager']
    if not candidates:
        return jsonify({'error': 'No suitable packages found'}), 500

    target_app = random.choice(candidates)
    
    with _lock:
        data = _load()
        project = _find_project(data, project_id)
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        if not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403

        target_column = project['columns'][0] if project.get('columns') else 'Backlog'
        
        created_tickets = []
        now = _now()
        
        # 1. Create Epic
        epic_id = _gen_id('t_')
        epic_title = f"Audyt bezpieczeństwa i UX: {target_app['name']}"
        epic_desc = (f"Kompleksowy audyt aplikacji {target_app['name']} (v1.0).\n"
                     f"Opis aplikacji: {target_app['description']}\n\n"
                     f"Cele audytu:\n"
                     f"1. Weryfikacja bezpieczeństwa (RBAC, sanitizacja)\n"
                     f"2. Analiza UI/UX (responsywność, komunikaty)\n"
                     f"3. Spójność z systemem EthOS\n\n"
                     f"Wymagane zależności: {target_app.get('deps_label', 'N/A')}")
        
        epic_ticket = {
            'id': epic_id,
            'project_id': project_id,
            'title': epic_title,
            'description': epic_desc,
            'column': target_column,
            'priority': 'high',
            'assignee': g.username,
            'type': 'epic',
            'complexity': 'complex',
            'reporter': g.username,
            'labels': ['audit', 'auto-generated', target_app['id']],
            'comments': [],
            'order': 0, # Put at top
            'created': now,
            'updated': now,
        }
        
        created_tickets.append(epic_ticket)
        
        # 2. Create Tasks linked to Epic
        tasks = [
            {
                'title': f"Analiza RBAC i uprawnień: {target_app['name']}",
                'desc': (f"Sprawdź czy endpointy API aplikacji {target_app['name']} są poprawnie zabezpieczone.\n\n"
                         f"**Endpointy do sprawdzenia:**\n"
                         f"- Install: `{target_app.get('install_endpoint', 'N/A')}`\n"
                         f"- Uninstall: `{target_app.get('uninstall_endpoint', 'N/A')}`\n"
                         f"- Status: `{target_app.get('status_endpoint', 'N/A')}`\n\n"
                         f"**Scenariusze testowe (QA):**\n"
                         f"1. Próba wywołania endpointów bez tokenu (oczekiwane 401/403).\n"
                         f"2. Próba wywołania endpointów jako użytkownik bez uprawnień admina (oczekiwane 403).\n"
                         f"3. Weryfikacja czy status jest widoczny dla zwykłego użytkownika (zgodnie z polityką).\n\n"
                         f"**Zadania deweloperskie:**\n"
                         f"- Dodać dekorator `@admin_required` do endpointów instalacji/dezinstalacji.\n"
                         f"- Sprawdzić logowanie prób nieautoryzowanego dostępu."),
                'labels': ['security', 'RBAC', f"epic:{epic_id}"],
                'priority': 'critical',
                'complexity': 'medium'
            },
            {
                'title': f"Weryfikacja sanitizacji danych wejściowych: {target_app['name']}",
                'desc': (f"Przeprowadzić audyt pod kątem podatności Injection (XSS, Command Injection) w {target_app['name']}.\n\n"
                         f"**Obszary do sprawdzenia:**\n"
                         f"- Pola formularzy konfiguracyjnych.\n"
                         f"- Parametry URL w endpointach API.\n"
                         f"- Nazwy plików/folderów przetwarzane przez aplikację.\n\n"
                         f"**Scenariusze testowe (QA):**\n"
                         f"1. Wprowadzenie znaków specjalnych (`<script>`, `../`, `;`) w polach tekstowych.\n"
                         f"2. Próba wykonania komendy systemowej w polach ścieżek.\n\n"
                         f"**Zadania deweloperskie:**\n"
                         f"- Użyć funkcji `secure_filename` dla operacji na plikach.\n"
                         f"- Escapować dane wyjściowe w widokach (jeśli renderowane po stronie serwera)."),
                'labels': ['security', 'input-validation', f"epic:{epic_id}"],
                'priority': 'high',
                'complexity': 'medium'
            },
            {
                'title': f"Audyt UI/UX - Responsywność: {target_app['name']}",
                'desc': (f"Zweryfikować działanie interfejsu aplikacji {target_app['name']} na urządzeniach mobilnych i tabletach.\n\n"
                         f"**Scenariusze testowe (QA):**\n"
                         f"1. Sprawdzenie widoku na szerokości 375px (iPhone SE) i 768px (iPad).\n"
                         f"2. Czy przyciski są wystarczająco duże (min. 44x44px)?\n"
                         f"3. Czy tabele przewijają się horyzontalnie bez psucia układu?\n"
                         f"4. Czy modale mieszczą się na ekranie?\n\n"
                         f"**Zadania deweloperskie:**\n"
                         f"- Dodać media queries dla małych ekranów.\n"
                         f"- Dostosować Grid/Flexbox do układu jednokolumnowego na mobile."),
                'labels': ['ui-ux', 'mobile', f"epic:{epic_id}"],
                'priority': 'medium',
                'complexity': 'medium'
            },
            {
                'title': f"Audyt UI/UX - Komunikaty błędów: {target_app['name']}",
                'desc': (f"Poprawić jakość komunikatów błędów zwracanych do użytkownika w {target_app['name']}.\n\n"
                         f"**Problem:**\n"
                         f"Często błędy backendu są zwracane jako surowy JSON lub 'Internal Server Error'.\n\n"
                         f"**Oczekiwane zachowanie (QA):**\n"
                         f"1. Użytkownik widzi zrozumiały komunikat (np. 'Brak połączenia z dyskiem' zamiast 'IOError: [Errno 2]').\n"
                         f"2. Komunikaty sukcesu (Toast) znikają po 3-5 sekundach.\n"
                         f"3. Błędy krytyczne wymagają potwierdzenia zamknięcia.\n\n"
                         f"**Zadania deweloperskie:**\n"
                         f"- Przechwytywać wyjątki w endpointach i zwracać `jsonify({{ 'error': 'Human readable message' }})`.\n"
                         f"- W frontendzie używać `toast()` z odpowiednim typem ('error', 'success')."),
                'labels': ['ui-ux', 'error-handling', f"epic:{epic_id}"],
                'priority': 'medium',
                'complexity': 'simple'
            },
            {
                'title': f"Spójność wizualna (Style Guide): {target_app['name']}",
                'desc': (f"Dostosować wygląd aplikacji {target_app['name']} do standardów EthOS Design System.\n\n"
                         f"**Elementy do weryfikacji:**\n"
                         f"- Kolor wiodący: `{target_app['color']}` (czy jest używany w nagłówkach/przyciskach?)\n"
                         f"- Ikona: `{target_app['icon']}` (czy jest spójna w menu i nagłówku?)\n"
                         f"- Spójność fontów (Inter/Roboto) i odstępów (spacing scale).\n\n"
                         f"**Zadania deweloperskie:**\n"
                         f"- Usunąć inline style CSS.\n"
                         f"- Używać klas z `style.css` (np. `.btn-primary`, `.card`, `.text-lg`)."),
                'labels': ['ui-ux', 'consistency', f"epic:{epic_id}"],
                'priority': 'low',
                'complexity': 'simple'
            }
        ]
        
        for task in tasks:
            tid = _gen_id('t_')
            t_ticket = {
                'id': tid,
                'project_id': project_id,
                'title': task['title'],
                'description': task['desc'],
                'column': target_column,
                'priority': task['priority'],
                'assignee': None,
                'type': 'task',
                'complexity': task.get('complexity', 'medium'),
                'reporter': g.username,
                'labels': task['labels'],
                'comments': [],
                'order': 0, # Placeholder, will update later
                'created': now,
                'updated': now,
            }
            created_tickets.append(t_ticket)
            
        # Existing tickets in this column
        existing_tickets = [t for t in data['tickets'] if t['project_id'] == project_id and t['column'] == target_column]
        existing_tickets.sort(key=lambda x: x.get('order', 0))
        
        # New order: Created tickets first, then existing tickets
        data['tickets'].extend(created_tickets)
        
        all_col_tickets = created_tickets + existing_tickets
        for idx, t in enumerate(all_col_tickets):
            t['order'] = idx
            
        _save(data)

    for t in created_tickets:
        _emit('ticket_created', project_id, {'ticket': t})

    return jsonify({'ok': True, 'count': len(created_tickets), 'app': target_app['name'], 'epic_id': epic_id}), 201


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@tickets_bp.route('/tickets/<ticket_id>/attachments', methods=['POST'])
def upload_attachment(ticket_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    with _lock:
        data = _load()
        ticket = _find_ticket(data, ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404
        
        # Ensure user has access (via project)
        project = _find_project(data, ticket['project_id'])
        if not project or not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403
            
        tdir = os.path.join(ATTACHMENTS_DIR, ticket_id)
        os.makedirs(tdir, exist_ok=True)
        
        filename = secure_filename(file.filename)
        # Avoid overwrite
        base, ext = os.path.splitext(filename)
        if os.path.exists(os.path.join(tdir, filename)):
             filename = f"{base}_{int(time.time())}{ext}"
             
        filepath = os.path.join(tdir, filename)
        file.save(filepath)
        
        if 'attachments' not in ticket:
            ticket['attachments'] = []
            
        attachment = {
            'filename': filename,
            'size': os.path.getsize(filepath),
            'mimetype': file.mimetype,
            'created': time.time(),
            'uploader': g.username
        }
        ticket['attachments'].append(attachment)
        ticket['updated'] = time.time()
        
        _save(data)
        _emit('ticket_updated', ticket['project_id'], {'ticket': ticket})
        
        return jsonify({'attachment': attachment})

@tickets_bp.route('/tickets/<ticket_id>/attachments/<filename>', methods=['GET'])
def get_attachment(ticket_id, filename):
    data = _load() 
    ticket = _find_ticket(data, ticket_id)
    if not ticket:
         return jsonify({'error': 'Ticket not found'}), 404
         
    project = _find_project(data, ticket['project_id'])
    if not project or not _is_member(project):
         return jsonify({'error': 'Access denied'}), 403

    tdir = os.path.join(ATTACHMENTS_DIR, ticket_id)
    filepath = os.path.join(tdir, filename)
    if not os.path.abspath(filepath).startswith(os.path.abspath(tdir)):
         return jsonify({'error': 'Invalid path'}), 403
         
    if not os.path.exists(filepath):
         return jsonify({'error': 'File not found'}), 404
         
    return send_file(filepath)

@tickets_bp.route('/tickets/<ticket_id>/attachments/<filename>', methods=['DELETE'])
def delete_attachment(ticket_id, filename):
    with _lock:
        data = _load()
        ticket = _find_ticket(data, ticket_id)
        if not ticket:
             return jsonify({'error': 'Ticket not found'}), 404
             
        project = _find_project(data, ticket['project_id'])
        if not project or not _is_member(project):
             return jsonify({'error': 'Access denied'}), 403
             
        tdir = os.path.join(ATTACHMENTS_DIR, ticket_id)
        filepath = os.path.join(tdir, filename)
        
        if 'attachments' in ticket:
            ticket['attachments'] = [a for a in ticket['attachments'] if a['filename'] != filename]
            
        if os.path.exists(filepath):
            os.remove(filepath)
            
        ticket['updated'] = time.time()
        _save(data)
        _emit('ticket_updated', ticket['project_id'], {'ticket': ticket})
        
        return jsonify({'status': 'deleted'})

