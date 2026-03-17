"""
EthOS – Kanban / Project Management (Tickets) Blueprint
"""

import os
import sys
import time
import uuid
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask import Blueprint, request, jsonify, g
from host import data_path as _data_path
from utils import load_json as _load_json, save_json as _save_json

tickets_bp = Blueprint('tickets', __name__, url_prefix='/api/tickets')

TICKETS_FILE = _data_path('tickets.json')

_lock = threading.Lock()

VALID_PRIORITIES = ('critical', 'high', 'medium', 'low')
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
            'reporter': g.username,
            'labels': labels,
            'comments': [],
            'order': 0,
            'created': now,
            'updated': now,
        }

        data['tickets'].append(ticket)
        _save(data)

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
        if 'assignee' in body:
            ticket['assignee'] = _strip(body['assignee'], MAX_TITLE)
        if 'labels' in body:
            labels = body['labels']
            if isinstance(labels, list):
                ticket['labels'] = [_strip(l, 100) for l in labels if _strip(l, 100)][:MAX_LABELS]

        ticket['updated'] = _now()
        _save(data)

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

        data['tickets'] = [t for t in data['tickets'] if t['id'] != ticket_id]
        _save(data)

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

        # Collect tickets in target column (excluding the moved one)
        col_tickets = sorted(
            [t for t in data['tickets']
             if t['project_id'] == ticket['project_id']
             and t['column'] == column and t['id'] != ticket_id],
            key=lambda t: t.get('order', 0),
        )

        if order is None or not isinstance(order, int) or order < 0:
            order = 0
        if order > len(col_tickets):
            order = len(col_tickets)

        col_tickets.insert(order, ticket)
        for idx, t in enumerate(col_tickets):
            t['order'] = idx

        _save(data)

    return jsonify({'ok': True, 'item': ticket})


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
            if col in ('Do zrobienia', 'W trakcie'):
                queue.append({
                    'id': t['id'],
                    'title': t['title'],
                    'description': t.get('description', ''),
                    'priority': t.get('priority', 'medium'),
                    'column': col,
                    'assignee': t.get('assignee', ''),
                    'labels': t.get('labels', []),
                    'project_id': project['id'],
                    'project_name': project['name'],
                })

    queue.sort(key=lambda t: (
        0 if t['column'] == 'Do zrobienia' else 1,
        PRIORITY_ORDER.get(t['priority'], 2),
    ))

    return jsonify({'queue': queue, 'total': len(queue)})
