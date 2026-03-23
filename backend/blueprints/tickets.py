import os
import time
import json
import re
import glob
import subprocess
import threading
import random
import shutil
import sys
from datetime import datetime
from flask import Blueprint, jsonify, request, g, send_file
from werkzeug.utils import secure_filename

# Fix path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import database functions
from blueprints.tickets_db import (
    init_db, get_projects, get_projects_with_stats, get_project, create_project,
    update_project, delete_project, get_tickets, get_ticket,
    create_ticket, update_ticket, delete_ticket
)

# Helpers
from ethos_packages_data import _ETHOS_PACKAGES
from host import log_path, data_path

tickets_bp = Blueprint('tickets', __name__, url_prefix='/api/tickets')

# Constants
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
DEFAULT_COLUMNS = ['Backlog', 'To Do', 'In Progress', 'QA', 'Review', 'Done']
ATTACHMENTS_DIR = data_path('ticket_attachments')

# AI/Watcher Config
COPILOT_LOG_DIR = '/opt/ethos/logs/copilot_tickets'
LOCALAI_LOG_DIR = '/opt/ethos/logs/localai_tickets'
WATCHER_LOG_FILE = '/opt/ethos/logs/ticket_watcher_new.log'
WATCHER_LOCK_FILE = '/tmp/.ethos_watcher_executing'
_PREFLIGHT_SCRIPT = '/opt/ethos/tools/preflight_check.py'
_WATCHER_UNIT = 'ethos-ticket-watcher.service'

_socketio = None

# Initialize DB
init_db()
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def init_tickets(socketio):
    global _socketio
    _socketio = socketio

def _emit(event_type, project_id, payload=None):
    if _socketio:
        try:
            _socketio.emit('tickets_event', {
                'type': event_type,
                'project_id': project_id,
                **(payload or {}),
                'ts': time.time()
            })
        except Exception:
            pass

def _gen_id(prefix=''):
    return prefix + os.urandom(6).hex()

def _now():
    return time.time()

def _strip(text, length=None):
    if not text:
        return ''
    text = str(text).strip()
    if length and len(text) > length:
        return text[:length]
    return text

def _is_member(project):
    # If no project, access denied
    if not project:
        return False
    # Admin always access
    if g.role == 'admin':
        return True
    # Owner always access
    if project.get('owner') == g.username:
        return True
    # Check members list
    members = project.get('members', [])
    return g.username in members

def _can_manage_project(project):
    """Owner or admin."""
    return g.role == 'admin' or g.username == project.get('owner')

# ---------------------------------------------------------------------------
# Projects CRUD
# ---------------------------------------------------------------------------

@tickets_bp.route('/projects', methods=['GET'])
def api_list_projects():
    projects = get_projects_with_stats()
    # Filter by membership
    projects = [p for p in projects if _is_member(p)]
    return jsonify({'projects': projects})

@tickets_bp.route('/projects', methods=['POST'])
def api_create_project():
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
    project_data = {
        'id': _gen_id(),
        'name': name,
        'description': description,
        'owner': g.username,
        'members': members,
        'columns': list(DEFAULT_COLUMNS),
        'color': color,
        'copilot_enabled': bool(body.get('copilot_enabled', False)),
        'localai_enabled': bool(body.get('localai_enabled', False)),
        'freemodel_enabled': bool(body.get('freemodel_enabled', False)),
        'created': now,
        'updated': now,
    }

    # Mutual exclusivity logic
    enabled = [k for k in ('copilot_enabled', 'localai_enabled', 'freemodel_enabled') if project_data[k]]
    if len(enabled) > 1:
        # If multiple enabled, disable others (simple logic: keep the first one encountered or just reset)
        # Replicating original logic: keep 1st found in enabled list?
        # Original: if k != enabled[0]: project[k] = False
        pass # Already handled by only setting 1 if carefully sent, but tickets_db handles storage
        # Logic in tickets_db doesn't enforce this, so we should enforce in create
        if project_data['copilot_enabled']:
            project_data['localai_enabled'] = False
            project_data['freemodel_enabled'] = False
        elif project_data['localai_enabled']:
            project_data['freemodel_enabled'] = False

    project = create_project(project_data)
    _emit('project_created', project['id'], {'project': project})
    return jsonify({'ok': True, 'item': project}), 201

@tickets_bp.route('/projects/<project_id>', methods=['GET'])
def api_get_project(project_id):
    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    tickets = get_tickets(project_id)
    return jsonify({'project': project, 'tickets': tickets})

@tickets_bp.route('/projects/<project_id>', methods=['PUT'])
def api_update_project(project_id):
    body = request.get_json(silent=True) or {}
    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _can_manage_project(project):
        return jsonify({'error': 'Access denied'}), 403

    updates = {}
    if 'name' in body: updates['name'] = _strip(body['name'], MAX_TITLE)
    if 'description' in body: updates['description'] = _strip(body['description'], MAX_DESCRIPTION)
    if 'color' in body: updates['color'] = _strip(body['color'], 20)

    if 'copilot_enabled' in body: updates['copilot_enabled'] = bool(body['copilot_enabled'])
    if 'localai_enabled' in body: updates['localai_enabled'] = bool(body['localai_enabled'])
    if 'freemodel_enabled' in body: updates['freemodel_enabled'] = bool(body['freemodel_enabled'])

    if 'members' in body:
        members = body['members']
        if isinstance(members, list):
            updates['members'] = [str(m).strip() for m in members if str(m).strip()]
            if g.username not in updates['members']:
                updates['members'].insert(0, g.username)

    if 'columns' in body:
        cols = body['columns']
        if isinstance(cols, list):
            updates['columns'] = [str(c).strip() for c in cols if str(c).strip()]

    # Mutual exclusivity
    # We need to merge with existing state to check
    current_state = {k: project.get(k, False) for k in ['copilot_enabled', 'localai_enabled', 'freemodel_enabled']}
    current_state.update({k: v for k, v in updates.items() if k in current_state})

    enabled = [k for k in current_state if current_state[k]]
    if len(enabled) > 1:
        # Priority: Copilot > LocalAI > Free
        if current_state['copilot_enabled']:
            updates['localai_enabled'] = False
            updates['freemodel_enabled'] = False
        elif current_state['localai_enabled']:
            updates['freemodel_enabled'] = False

    updated_project = update_project(project_id, updates)
    _emit('project_updated', project_id, {'project': updated_project})
    return jsonify({'ok': True, 'item': updated_project})

@tickets_bp.route('/projects/<project_id>', methods=['DELETE'])
def api_delete_project(project_id):
    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _can_manage_project(project):
        return jsonify({'error': 'Access denied'}), 403

    delete_project(project_id)
    _emit('project_deleted', project_id, {'id': project_id})
    return jsonify({'ok': True})

# ---------------------------------------------------------------------------
# Tickets CRUD
# ---------------------------------------------------------------------------

@tickets_bp.route('/tickets', methods=['POST'])
def api_create_ticket():
    body = request.get_json(silent=True) or {}
    project_id = body.get('project_id')
    if not project_id:
        return jsonify({'error': 'Project ID required'}), 400

    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    title = _strip(body.get('title', ''), MAX_TITLE)
    if not title:
        return jsonify({'error': 'Title required'}), 400

    column = body.get('column')
    if column not in project.get('columns', []):
        column = project.get('columns', [])[0] if project.get('columns') else 'Backlog'

    now = _now()
    ticket_data = {
        'id': _gen_id('t_'),
        'project_id': project_id,
        'title': title,
        'description': _strip(body.get('description', ''), MAX_DESCRIPTION),
        'column': column,
        'priority': body.get('priority', 'medium'),
        'assignee': body.get('assignee'),
        'reporter': g.username,
        'labels': body.get('labels', []),
        'comments': [],
        'attachments': [],
        'manual_tests': body.get('manual_tests', []),
        'order': 0, # Should calculate max order + 1? Or just 0 and let UI handle?
                    # Original logic added to end? No, order=0 usually top.
        'created': now,
        'updated': now
    }

    # Logic for order: find max order in column?
    # Original code: didn't seem to calc order explicitly in create_project example, but check list
    # Let's just use 0 or time?
    # Original tickets.py: `data['tickets'].append(ticket)` -> usually creates at end of list?
    # But filtering by project -> list.
    # We can just let it be 0.

    ticket = create_ticket(ticket_data)
    _emit('ticket_created', project_id, {'ticket': ticket})
    return jsonify({'ok': True, 'item': ticket}), 201

@tickets_bp.route('/tickets/<ticket_id>', methods=['GET'])
def api_get_ticket(ticket_id):
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404

    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    return jsonify(ticket)

@tickets_bp.route('/tickets/<ticket_id>', methods=['PUT'])
def api_update_ticket(ticket_id):
    body = request.get_json(silent=True) or {}
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404

    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    updates = {}
    if 'title' in body: updates['title'] = _strip(body['title'], MAX_TITLE)
    if 'description' in body: updates['description'] = _strip(body['description'], MAX_DESCRIPTION)
    if 'priority' in body: updates['priority'] = body['priority']
    if 'assignee' in body: updates['assignee'] = body['assignee']
    if 'column' in body and body['column'] in project.get('columns', []):
        updates['column'] = body['column']
    if 'manual_tests' in body and isinstance(body['manual_tests'], list):
        updates['manual_tests'] = body['manual_tests']

    updated_ticket = update_ticket(ticket_id, updates)
    _emit('ticket_updated', ticket['project_id'], {'ticket': updated_ticket})
    return jsonify({'ok': True, 'item': updated_ticket})

@tickets_bp.route('/tickets/<ticket_id>', methods=['DELETE'])
def api_delete_ticket(ticket_id):
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404

    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    delete_ticket(ticket_id)

    # Also delete attachments
    tdir = os.path.join(ATTACHMENTS_DIR, ticket_id)
    if os.path.exists(tdir):
        shutil.rmtree(tdir)

    _emit('ticket_deleted', ticket['project_id'], {'id': ticket_id})
    return jsonify({'ok': True})

@tickets_bp.route('/tickets/<ticket_id>/move', methods=['PUT'])
def api_move_ticket(ticket_id):
    body = request.get_json(silent=True) or {}
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404

    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    updates = {}
    if 'column' in body:
        col = body['column']
        if col in project.get('columns', []):
            updates['column'] = col
        else:
            return jsonify({'error': f'Column not found: {col}'}), 400

    if 'order' in body:
        updates['order'] = int(body['order'])

    updated_ticket = update_ticket(ticket_id, updates)
    _emit('ticket_updated', ticket['project_id'], {'ticket': updated_ticket})
    return jsonify({'ok': True, 'item': updated_ticket})

# ---------------------------------------------------------------------------
# Comments & Labels
# ---------------------------------------------------------------------------

@tickets_bp.route('/tickets/<ticket_id>/comments', methods=['POST'])
def add_comment(ticket_id):
    body = request.get_json(silent=True) or {}
    text = _strip(body.get('text', ''), 10000)
    if not text:
        return jsonify({'error': 'Text required'}), 400

    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    comment = {
        'id': _gen_id('c_'),
        'author': g.username,
        'text': text,
        'created': _now()
    }

    comments = ticket.get('comments', [])
    comments.append(comment)

    updated_ticket = update_ticket(ticket_id, {'comments': comments})
    _emit('ticket_updated', ticket['project_id'], {'ticket': updated_ticket})
    return jsonify(comment), 201

@tickets_bp.route('/tickets/<ticket_id>/comments/<comment_id>', methods=['DELETE'])
def delete_comment(ticket_id, comment_id):
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    comments = ticket.get('comments', [])
    # Only author or admin/owner can delete?
    # Original logic: "if c['id'] == comment_id" - assume check passes if found?
    # Actually need to check permission logic from original code.
    # Original code: if g.username != c['author'] and not _can_manage_project(project): error

    target_comment = next((c for c in comments if c['id'] == comment_id), None)
    if not target_comment:
        return jsonify({'error': 'Comment not found'}), 404

    if g.username != target_comment['author'] and not _can_manage_project(project):
        return jsonify({'error': 'Access denied'}), 403

    comments = [c for c in comments if c['id'] != comment_id]
    updated_ticket = update_ticket(ticket_id, {'comments': comments})
    _emit('ticket_updated', ticket['project_id'], {'ticket': updated_ticket})
    return jsonify({'ok': True})

@tickets_bp.route('/tickets/<ticket_id>/labels', methods=['POST'])
def add_label(ticket_id):
    body = request.get_json(silent=True) or {}
    label = _strip(body.get('label', ''), 30)
    if not label:
        return jsonify({'error': 'Label required'}), 400

    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    labels = ticket.get('labels', [])
    if label not in labels:
        labels.append(label)
        updated_ticket = update_ticket(ticket_id, {'labels': labels})
        _emit('ticket_updated', ticket['project_id'], {'ticket': updated_ticket})

    return jsonify({'ok': True})

@tickets_bp.route('/tickets/<ticket_id>/labels/<label>', methods=['DELETE'])
def remove_label(ticket_id, label):
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    labels = ticket.get('labels', [])
    if label in labels:
        labels.remove(label)
        updated_ticket = update_ticket(ticket_id, {'labels': labels})
        _emit('ticket_updated', ticket['project_id'], {'ticket': updated_ticket})

    return jsonify({'ok': True})

# ---------------------------------------------------------------------------
# Manual Tests — AI Generation
# ---------------------------------------------------------------------------

# Background AI test generation tasks
import threading
_gen_tasks = {}  # task_id -> {status, tests, error}

@tickets_bp.route('/tickets/<ticket_id>/generate-tests', methods=['POST'])
def generate_tests(ticket_id):
    """Start async generation of manual test steps via Ollama (llama3.2:3b)."""
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    body = request.get_json(silent=True) or {}
    title = body.get('title', ticket.get('title', ''))
    description = body.get('description', ticket.get('description', ''))

    import uuid
    task_id = uuid.uuid4().hex[:12]
    _gen_tasks[task_id] = {'status': 'running', 'tests': [], 'error': None}

    def _generate():
        prompt = (
            "Jesteś testerem QA dla systemu EthOS (web UI, SPA, desktop-like z oknami apek).\n"
            "Na podstawie ticketu wygeneruj kroki testów manualnych.\n\n"
            f"Tytuł: {title}\nOpis: {description}\n\n"
            "Każdy krok to JSON z polami: action (co zrobić), expected (oczekiwany wynik), screenshot (bool).\n"
            "Akcje po polsku. Dostępne komendy:\n"
            "- 'Otwórz apkę X' — otwiera okno aplikacji\n"
            "- 'Kliknij X' — klika element\n"
            "- 'Wpisz \"tekst\" w pole X' — wypełnia pole\n"
            "- 'Czekaj N sekund' — czeka\n"
            "- 'Sprawdź: X jest widoczny' — weryfikacja DOM\n"
            "- 'Przewiń w dół' — scroll\n"
            "- 'Screenshot: opis' — zrób screenshot\n\n"
            "Odpowiedz WYŁĄCZNIE jako JSON array, bez markdown, np:\n"
            '[{"action":"Otwórz apkę Dashboard","expected":"Dashboard widoczny z widgetami","screenshot":true}]'
        )
        try:
            import requests as req
            resp = req.post('http://127.0.0.1:11434/api/generate', json={
                'model': 'llama3.2:3b',
                'prompt': prompt,
                'stream': False,
                'options': {'temperature': 0.3, 'num_predict': 1024},
            }, timeout=600)
            resp.raise_for_status()
            raw = resp.json().get('response', '').strip()
            start = raw.find('[')
            end = raw.rfind(']')
            if start >= 0 and end > start:
                tests = json.loads(raw[start:end + 1])
                for i, t_step in enumerate(tests):
                    t_step['step'] = i + 1
                    t_step.setdefault('screenshot', True)
                _gen_tasks[task_id] = {'status': 'done', 'tests': tests, 'error': None}
            else:
                _gen_tasks[task_id] = {'status': 'done', 'tests': [], 'error': 'AI nie zwróciło JSON'}
        except Exception as e:
            _gen_tasks[task_id] = {'status': 'error', 'tests': [], 'error': str(e)}

    threading.Thread(target=_generate, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id}), 202


@tickets_bp.route('/gen-tests-poll/<task_id>', methods=['GET'])
def poll_generate_tests(task_id):
    """Poll status of async test generation task."""
    task = _gen_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    resp = jsonify(task)
    if task['status'] in ('done', 'error'):
        _gen_tasks.pop(task_id, None)
    return resp

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

    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    tdir = os.path.join(ATTACHMENTS_DIR, ticket_id)
    os.makedirs(tdir, exist_ok=True)

    filename = secure_filename(file.filename)
    base, ext = os.path.splitext(filename)
    if os.path.exists(os.path.join(tdir, filename)):
            filename = f"{base}_{int(time.time())}{ext}"

    filepath = os.path.join(tdir, filename)
    file.save(filepath)

    attachment = {
        'filename': filename,
        'size': os.path.getsize(filepath),
        'mimetype': file.mimetype,
        'created': time.time(),
        'uploader': g.username
    }

    attachments = ticket.get('attachments', [])
    attachments.append(attachment)

    updated_ticket = update_ticket(ticket_id, {'attachments': attachments})
    _emit('ticket_updated', ticket['project_id'], {'ticket': updated_ticket})

    return jsonify({'attachment': attachment})

@tickets_bp.route('/tickets/<ticket_id>/attachments/<filename>', methods=['GET'])
def get_attachment(ticket_id, filename):
    ticket = get_ticket(ticket_id)
    if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

    project = get_project(ticket['project_id'])
    if not _is_member(project):
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
    ticket = get_ticket(ticket_id)
    if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

    project = get_project(ticket['project_id'])
    if not _is_member(project):
            return jsonify({'error': 'Access denied'}), 403

    tdir = os.path.join(ATTACHMENTS_DIR, ticket_id)
    filepath = os.path.join(tdir, filename)

    attachments = ticket.get('attachments', [])
    attachments = [a for a in attachments if a['filename'] != filename]

    if os.path.exists(filepath):
        os.remove(filepath)

    updated_ticket = update_ticket(ticket_id, {'attachments': attachments})
    _emit('ticket_updated', ticket['project_id'], {'ticket': updated_ticket})

    return jsonify({'status': 'deleted'})

# ---------------------------------------------------------------------------
# Copilot Integration
# ---------------------------------------------------------------------------

PRIORITY_ORDER = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}

@tickets_bp.route('/copilot/queue', methods=['GET'])
def copilot_queue():
    """Returns actionable tickets from copilot-enabled projects."""
    projects = get_projects()
    queue = []
    for project in projects:
        agent_type = None
        if project.get('copilot_enabled', False):
            agent_type = 'copilot'
        elif project.get('localai_enabled', False):
            agent_type = 'localai'
        elif project.get('freemodel_enabled', False):
            agent_type = 'freemodel'
        if not agent_type:
            continue
        if not _is_member(project):
            continue

        proj_tickets = get_tickets(project['id'])
        for t in proj_tickets:
            col = t.get('column', '')
            if col in ('To Do', 'In Progress', 'QA'):
                comments = t.get('comments', [])
                last_comment = None
                if comments:
                    lc = comments[-1]
                    last_comment = {
                        'author': lc.get('author', ''),
                        'text': lc.get('text', ''),
                        'created': lc.get('created', 0),
                    }
                # Extract type/complexity from labels if stored there
                labels = t.get('labels', [])
                t_type = t.get('type', 'task')
                t_complexity = t.get('complexity', 'medium')
                for lbl in labels:
                    if isinstance(lbl, str) and lbl.startswith('complexity:'):
                        t_complexity = lbl.split(':', 1)[1]
                    elif isinstance(lbl, str) and lbl.startswith('type:'):
                        t_type = lbl.split(':', 1)[1]
                queue.append({
                    'id': t['id'],
                    'title': t['title'],
                    'description': t.get('description', ''),
                    'priority': t.get('priority', 'medium'),
                    'type': t_type,
                    'complexity': t_complexity,
                    'column': col,
                    'assignee': t.get('assignee', ''),
                    'labels': labels,
                    'project_id': project['id'],
                    'project_name': project['name'],
                    'agent': agent_type,
                    'last_comment': last_comment,
                })

    queue.sort(key=lambda t: (
        0 if t['column'] == 'To Do' else (1 if t['column'] == 'In Progress' else 2),
        PRIORITY_ORDER.get(t['priority'], 2),
    ))

    return jsonify({'queue': queue, 'total': len(queue)})


# ---------------------------------------------------------------------------
# AI Usage
# ---------------------------------------------------------------------------

_ai_usage_cache = {}

def _parse_token_val(s):
    """Parse token strings like '1.9m', '9.5k', '120' into integers."""
    s = s.strip().lower().replace(',', '')
    try:
        if s.endswith('b'):
            return int(float(s[:-1]) * 1e9)
        if s.endswith('m'):
            return int(float(s[:-1]) * 1e6)
        if s.endswith('k'):
            return int(float(s[:-1]) * 1e3)
        return int(float(s))
    except (ValueError, IndexError):
        return 0

def _empty_totals():
    return {'premium_requests': 0, 'runs': 0, 'qa_runs': 0,
            'session_time_s': 0, 'code_added': 0, 'code_removed': 0,
            'tokens_in': 0, 'tokens_out': 0}

def _parse_log_usage(filepath):
    """Extract usage metrics from a single log file footer."""
    result = _empty_totals()
    result['model'] = None
    is_qa = '_qa_' in os.path.basename(filepath)
    result['is_qa'] = is_qa
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return result

    for line in lines[:5]:
        if line.startswith('=== Model:'):
            m = re.search(r'Model:\s*(\S+)', line)
            if m:
                result['model'] = m.group(1)
        elif line.startswith('Model:'):
            m = re.search(r'Model:\s*(\S+)', line)
            if m:
                result['model'] = m.group(1)

    tail = lines[-30:] if len(lines) > 30 else lines
    for line in tail:
        line_s = line.strip()
        m = re.match(r'Total usage est:\s*([\d.]+)\s*Premium', line_s, re.I)
        if m:
            result['premium_requests'] = float(m.group(1))
            continue
        m = re.match(r'Total session time:\s*(.*)', line_s)
        if m:
            ts = m.group(1).strip()
            secs = 0
            hm = re.search(r'(\d+)h', ts)
            mm = re.search(r'(\d+)m', ts)
            sm = re.search(r'(\d+)s', ts)
            if hm: secs += int(hm.group(1)) * 3600
            if mm: secs += int(mm.group(1)) * 60
            if sm: secs += int(sm.group(1))
            result['session_time_s'] = secs
            continue
        m = re.match(r'Total code changes:\s*\+(\d+)\s+-(\d+)', line_s)
        if m:
            result['code_added'] = int(m.group(1))
            result['code_removed'] = int(m.group(2))
            continue
        m = re.match(r'^\s*(\S+)\s+([\d.]+[kmb]?)\s*in,\s*([\d.]+[kmb]?)\s*out', line_s, re.I)
        if m:
            result['tokens_in'] += _parse_token_val(m.group(2))
            result['tokens_out'] += _parse_token_val(m.group(3))

    return result


@tickets_bp.route('/ai-usage/<project_id>', methods=['GET'])
def ai_usage(project_id):
    """Aggregate AI usage stats for a project."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    if not (project.get('copilot_enabled') or project.get('localai_enabled') or project.get('freemodel_enabled')):
        return jsonify({'by_day': {}, 'by_model': {}, 'by_month': {}, 'totals': _empty_totals()})

    cache_key = project_id
    now = time.time()
    cached = _ai_usage_cache.get(cache_key)
    if cached and now - cached['ts'] < 60:
        return jsonify(cached['data'])

    ticket_ids = set()
    for t in get_tickets(project_id):
        ticket_ids.add(t['id'])

    log_dirs = [COPILOT_LOG_DIR, LOCALAI_LOG_DIR,
                os.path.join(COPILOT_LOG_DIR, 'archive'),
                os.path.join(LOCALAI_LOG_DIR, 'archive')]

    by_day = {}
    by_model = {}
    by_month = {}
    totals = _empty_totals()

    for log_dir in log_dirs:
        if not os.path.isdir(log_dir):
            continue
        for fname in os.listdir(log_dir):
            if not fname.endswith('.log') or '_prompt' in fname:
                continue
            m = re.match(r'(t_[a-f0-9]+)', fname)
            if not m or m.group(1) not in ticket_ids:
                continue

            fpath = os.path.join(log_dir, fname)
            usage = _parse_log_usage(fpath)
            model = usage.get('model') or 'unknown'

            ts_m = re.search(r'_(\d{10,})', fname)
            if ts_m:
                day_str = datetime.fromtimestamp(int(ts_m.group(1))).strftime('%Y-%m-%d')
                month_str = day_str[:7]
            else:
                try:
                    mt = os.path.getmtime(fpath)
                    day_str = datetime.fromtimestamp(mt).strftime('%Y-%m-%d')
                    month_str = day_str[:7]
                except Exception:
                    day_str = 'unknown'
                    month_str = 'unknown'

            for bucket_map, key in [(by_day, day_str), (by_model, model), (by_month, month_str)]:
                if key not in bucket_map:
                    bucket_map[key] = _empty_totals()
                b = bucket_map[key]
                b['premium_requests'] += usage['premium_requests']
                b['runs'] += 1
                if usage['is_qa']:
                    b['qa_runs'] += 1
                b['session_time_s'] += usage['session_time_s']
                b['code_added'] += usage['code_added']
                b['code_removed'] += usage['code_removed']
                b['tokens_in'] += usage['tokens_in']
                b['tokens_out'] += usage['tokens_out']

            totals['premium_requests'] += usage['premium_requests']
            totals['runs'] += 1
            if usage['is_qa']:
                totals['qa_runs'] += 1
            totals['session_time_s'] += usage['session_time_s']
            totals['code_added'] += usage['code_added']
            totals['code_removed'] += usage['code_removed']
            totals['tokens_in'] += usage['tokens_in']
            totals['tokens_out'] += usage['tokens_out']

    by_day = dict(sorted(by_day.items()))
    by_month = dict(sorted(by_month.items()))

    result = {'by_day': by_day, 'by_model': by_model, 'by_month': by_month, 'totals': totals}
    _ai_usage_cache[cache_key] = {'ts': now, 'data': result}
    return jsonify(result)


# ---------------------------------------------------------------------------
# AI & Watcher
# ---------------------------------------------------------------------------

def _parse_log_model(filepath):
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
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Forbidden'}), 403

    agent = (request.args.get('agent', 'copilot') or 'copilot').strip().lower()
    if agent not in ('copilot', 'localai'):
        return jsonify({'error': 'Invalid agent'}), 400
    log_dir = COPILOT_LOG_DIR if agent == 'copilot' else LOCALAI_LOG_DIR

    safe_id = re.sub(r'[^a-zA-Z0-9_]', '', ticket_id)
    pattern = os.path.join(log_dir, f'{safe_id}_*.log')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True) if os.path.isdir(log_dir) else []

    logs = []
    for f in files:
        name = os.path.basename(f)
        is_qa = '_qa_' in name
        is_prompt = '_prompt' in name
        if is_prompt:
            continue
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
            'agent': agent,
        })

    return jsonify({'logs': logs})

@tickets_bp.route('/tickets/<ticket_id>/copilot-logs/<filename>', methods=['GET'])
def copilot_log_content(ticket_id, filename):
    ticket = get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    project = get_project(ticket['project_id'])
    if not _is_member(project):
        return jsonify({'error': 'Forbidden'}), 403

    agent = (request.args.get('agent') or '').strip().lower()
    if not agent:
        agent = 'localai' if '_local_' in filename else 'copilot'
    if agent not in ('copilot', 'localai'):
        return jsonify({'error': 'Invalid agent'}), 400
    log_dir = COPILOT_LOG_DIR if agent == 'copilot' else LOCALAI_LOG_DIR

    safe_id = re.sub(r'[^a-zA-Z0-9_]', '', ticket_id)
    safe_name = re.sub(r'[^a-zA-Z0-9_.\-]', '', filename)
    if not safe_name.startswith(safe_id) or '..' in safe_name:
        return jsonify({'error': 'Invalid filename'}), 400

    path = os.path.join(log_dir, safe_name)
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

@tickets_bp.route('/watcher/status', methods=['GET'])
def watcher_status():
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
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    if not os.path.isfile(WATCHER_LOCK_FILE):
        return jsonify({'executing': False})
    try:
        with open(WATCHER_LOCK_FILE, 'r') as f:
            lock = json.load(f)
        tid = lock.get('ticket_id', '')
        model_info = lock.get('model') or {}
        started = lock.get('started', 0)
        elapsed = time.time() - started if started else 0
        pid = lock.get('copilot_pid')
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
            'agent': lock.get('agent', 'copilot') if isinstance(lock, dict) else 'copilot',
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
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    if action not in ('start', 'stop', 'restart'):
        return jsonify({'error': 'Invalid action'}), 400
    try:
        result = subprocess.run(
            ['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', action, _WATCHER_UNIT],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return jsonify({'error': result.stderr.strip() or f'{action} failed'}), 500
        time.sleep(0.5)
        active = subprocess.run(
            ['systemctl', 'is-active', _WATCHER_UNIT],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return jsonify({'ok': True, 'action': action, 'active': active})
    except subprocess.TimeoutExpired:
        return jsonify({'error': f'{action} timed out'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@tickets_bp.route('/preflight', methods=['GET'])
def preflight_check():
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    quick = request.args.get('quick', '').lower() in ('1', 'true', 'yes')
    cmd = [os.sys.executable, _PREFLIGHT_SCRIPT, '--json']
    if quick:
        cmd.append('--quick')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout)
        return jsonify(data), 200 if data.get('passed') else 422
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Preflight timed out'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@tickets_bp.route('/projects/<project_id>/bug-hunt', methods=['POST'])
def bug_hunt(project_id):
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401

    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    candidates = [p for p in _ETHOS_PACKAGES if p['id'] != 'docker-manager']
    if not candidates:
        return jsonify({'error': 'No suitable packages found'}), 500

    target_app = random.choice(candidates)

    # 1. Analyze Complexity
    deps_str = target_app.get('deps_label', '')
    deps = [d.strip() for d in deps_str.split(',') if d.strip() and 'brak' not in d.lower()]
    is_complex_auth = len(deps) > 1

    # 2. Analyze Performance
    js_size_kb = 0
    try:
        js_map = {
            'ai-chat': 'aichat.js',
            'code-editor': 'code_editor.js',
            'disk-repair': 'diskrepair.js',
            'download-manager': 'downloads.js',
            'domains-manager': 'domains.js',
            'doc-editor': 'editor.js',
            'usb-flasher': 'flasher.js',
            'sharing': 'sharing.js',
        }

        app_key = target_app.get('app_id', target_app['id'])
        candidates_filenames = [
            js_map.get(app_key),
            js_map.get(target_app['id']),
            f"{app_key}.js",
            f"{app_key.replace('-', '')}.js",
            f"{app_key.replace('-', '_')}.js"
        ]

        root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for fname in candidates_filenames:
            if not fname:
                continue
            js_path = os.path.join(root_dir, 'frontend', 'js', 'apps', fname)
            if os.path.exists(js_path):
                size_bytes = os.path.getsize(js_path)
                js_size_kb = round(size_bytes / 1024, 1)
                break
    except Exception:
        pass

    target_column = project['columns'][0] if project.get('columns') else 'Backlog'
    created_tickets = []
    now = _now()

    # 3. Create Epic
    epic_id = _gen_id('t_')
    epic_title = f"[EPIC] EthOS Package Optimization: {target_app['name']}"
    epic_desc = (f"Comprehensive audit and optimization of the {target_app['name']} application (v1.0).\n"
                 f"Application description: {target_app['description']}\n\n"
                 f"**Audit objectives:**\n"
                 f"1. 🛡️ [Security] RBAC verification and endpoint validation.\n"
                 f"2. ⚙️ [Logic] Session continuity and functional path flow.\n"
                 f"3. 🎨 [UX/UI] Alignment with the EthOS Design System.\n"
                 f"4. 🚀 [Perf] Asset optimization and API response time.\n\n"
                 f"**Preliminary analysis:**\n"
                 f"- Dependencies: {len(deps)} ({', '.join(deps) if deps else 'none'})\n"
                 f"- JS bundle size: {js_size_kb} KB\n"
                 f"- Security complexity: {'High (Smart Slicing active)' if is_complex_auth else 'Standard'}")

    epic_ticket = {
        'id': epic_id,
        'project_id': project_id,
        'title': epic_title,
        'description': epic_desc,
        'column': target_column,
        'priority': 'high',
        'assignee': g.username,
        'reporter': g.username,
        'labels': ['audit', 'auto-generated', target_app['id']],
        'comments': [],
        'attachments': [],
        'order': 0,
        'created': now,
        'updated': now,
    }
    # Note: 'type' and 'complexity' fields from original are not in DB schema but were stored in JSON
    # If they are important, we should add columns. Assuming they are less critical or can go in description/labels for now.
    # Original logic had them in the dict.
    # Let's add them as labels or part of description if schema doesn't support.
    # Or just ignore if frontend doesn't strictly need them.
    # To be safe, I'll add them to description or ignore.
    # Actually, I can store extra fields in a 'meta' JSON column if I had one.
    # For now, I'll proceed without them or add complexity as label.
    if is_complex_auth: epic_ticket['labels'].append('complexity:complex')
    else: epic_ticket['labels'].append('complexity:medium')
    epic_ticket['labels'].append('type:epic')

    created_tickets.append(create_ticket(epic_ticket))

    # 4. Create Tasks
    tasks = []
    if is_complex_auth:
        tasks.append({
            'title': f"🛡️ [Security] [RBAC] Backend Auth: {target_app['name']}",
            'desc': (f"Backend authorization verification (Smart Slicing: High Complexity).\n\n"
                     f"**Scope:**\n"
                     f"- Audit `@admin_required` decorators.\n"
                     f"- Verify access to system files.\n"
                     f"- Endpoints: {target_app.get('install_endpoint', 'N/A')}"),
            'labels': ['security', 'RBAC', 'backend', f"epic:{epic_id}", 'complexity:complex'],
            'priority': 'critical'
        })
        tasks.append({
            'title': f"🛡️ [Security] [RBAC] Frontend Guard: {target_app['name']}",
            'desc': (f"Client-side security verification.\n\n"
                     f"**Scope:**\n"
                     f"- Hide UI elements for users without permissions.\n"
                     f"- Handle 403 Forbidden responses in the view."),
            'labels': ['security', 'RBAC', 'frontend', f"epic:{epic_id}", 'complexity:medium'],
            'priority': 'high'
        })
        tasks.append({
            'title': f"🛡️ [Security] Manifest Config & Deps: {target_app['name']}",
            'desc': (f"Manifest and dependency audit ({len(deps)}).\n"
                     f"Ensure dependencies do not introduce security vulnerabilities."),
            'labels': ['security', 'config', f"epic:{epic_id}", 'complexity:medium'],
            'priority': 'medium'
        })
    else:
        tasks.append({
            'title': f"🛡️ [Security] RBAC verification and endpoint validation: {target_app['name']}",
            'desc': (f"Check if API endpoints of {target_app['name']} are properly secured.\n\n"
                     f"**Endpoints to check:**\n"
                     f"- Install: `{target_app.get('install_endpoint', 'N/A')}`\n"
                     f"- Uninstall: `{target_app.get('uninstall_endpoint', 'N/A')}`\n\n"
                     f"**Developer tasks:**\n"
                     f"- Add `@admin_required` decorator.\n"
                     f"- Check logging of unauthorized access attempts."),
            'labels': ['security', 'RBAC', f"epic:{epic_id}", 'complexity:medium'],
            'priority': 'critical'
        })

    tasks.append({
        'title': f"⚙️ [Logic] Session continuity and functional path flow: {target_app['name']}",
        'desc': (f"Business logic and application state management analysis.\n\n"
                 f"**Context Aware:**\n"
                 f"Focus on EthOS application logic, not Docker isolation.\n"
                 f"- Is the application state preserved after refresh?\n"
                 f"- Are network errors handled with graceful degradation?"),
        'labels': ['logic', 'flow', f"epic:{epic_id}", 'complexity:medium'],
        'priority': 'medium'
    })

    tasks.append({
        'title': f"🎨 [UX/UI] Alignment with EthOS Design System: {target_app['name']}",
        'desc': (f"Align the appearance of {target_app['name']} with EthOS Design System standards.\n\n"
                 f"**Elements to verify:**\n"
                 f"- Primary color: `{target_app.get('color', 'N/A')}`\n"
                 f"- Icon: `{target_app.get('icon', 'N/A')}`\n"
                 f"- Font and spacing consistency."),
        'labels': ['ui-ux', 'design-system', f"epic:{epic_id}", 'complexity:simple'],
        'priority': 'low'
    })

    perf_details = (f"Analysis: Resource loading time and memory usage.\n\n"
                    f"**Scan results:**\n"
                    f"- JS file size: **{js_size_kb} KB**\n"
                    f"- Dependencies: {len(deps)}\n\n")
    perf_tasks = (f"**Implementation (Dev):**\n"
                  f"- Implement Lazy Loading for modules (if > 200KB).\n"
                  f"- Optimize bundle size.\n"
                  f"- Check for memory leaks when switching features.\n\n"
                  f"**Tests (QA):**\n"
                  f"- Measure TTFB (Time to First Byte).\n"
                  f"- Test behavior under limited bandwidth (3G Throttling).")
    if js_size_kb > 500:
        perf_details += f"⚠️ **WARNING:** Assets > 500KB may slow loading on weak connections.\n\n"

    tasks.append({
        'title': f"🚀 [Perf] Asset optimization and API response time: {target_app['name']}",
        'desc': perf_details + perf_tasks,
        'labels': ['performance', 'optimization', f"epic:{epic_id}", 'complexity:medium'],
        'priority': 'medium'
    })

    for task in tasks:
        t_ticket = {
            'id': _gen_id('t_'),
            'project_id': project_id,
            'title': task['title'],
            'description': task['desc'],
            'column': target_column,
            'priority': task['priority'],
            'assignee': None,
            'reporter': g.username,
            'labels': task['labels'],
            'comments': [],
            'attachments': [],
            'order': 0,
            'created': now,
            'updated': now,
        }
        # Add 'type' label if missing
        t_ticket['labels'].append('type:task')
        created_tickets.append(create_ticket(t_ticket))

    for t in created_tickets:
        _emit('ticket_created', project_id, {'ticket': t})

    return jsonify({'ok': True, 'count': len(created_tickets), 'app': target_app['name'], 'epic_id': epic_id}), 201
