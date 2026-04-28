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
OLLAMA_LOG_DIR = '/opt/ethos/logs/ollama_tickets'
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

def _norm_title(title: str) -> str:
    """Normalise a ticket title for deduplication (lowercase, collapse whitespace, strip punctuation)."""
    t = (title or '').lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def _build_existing_titles(project_id: str) -> set:
    """Return a set of normalised titles for all tickets in the project."""
    return {_norm_title(t.get('title', '')) for t in get_tickets(project_id)}

def _find_existing_epic(project_id: str, app_name: str):
    """Return the first EPIC ticket for this app if one already exists, else None."""
    target = _norm_title(f'[EPIC] Code Audit: {app_name}')
    for t in get_tickets(project_id):
        if _norm_title(t.get('title', '')) == target:
            return t
    return None

def _ai_dedup_filter(new_tickets: list, existing_tickets: list,
                     ollama_url: str, ollama_model: str,
                     progress_cb=None) -> list:
    """
    Use Ollama to remove tickets from new_tickets that are semantically
    equivalent to any ticket already in existing_tickets.

    Returns the subset of new_tickets that are genuinely new.
    Falls back to normalised-title matching if Ollama is unavailable.
    """
    import requests as _req

    if not existing_tickets:
        return new_tickets

    # --- fast pre-filter: exact normalised-title match ---
    existing_norms = {_norm_title(t.get('title', '')) for t in existing_tickets}
    candidates = [t for t in new_tickets
                  if _norm_title(t.get('title', '')) not in existing_norms]

    if not candidates or not ollama_url or not ollama_model:
        return candidates

    # --- AI pass: batch-check remaining candidates ---
    existing_summary = '\n'.join(
        f'- {t.get("title", "")}' for t in existing_tickets[:120]  # cap at 120
    )
    new_summary = '\n'.join(
        f'{i}. {t.get("title", "")}'
        for i, t in enumerate(candidates)
    )

    prompt = (
        "You are a ticket deduplication assistant.\n\n"
        "EXISTING TICKETS (already in the project):\n"
        f"{existing_summary}\n\n"
        "CANDIDATE NEW TICKETS (numbered list):\n"
        f"{new_summary}\n\n"
        "Task: identify which candidates are duplicates or near-duplicates of existing tickets.\n"
        "Two tickets are duplicates if they describe the same bug, risk, or task — even with different wording.\n\n"
        "Reply ONLY with a JSON array of the numbers (0-based) of tickets that are NOT duplicates.\n"
        "Example: [0, 2, 4]\n"
        "If all are duplicates reply: []\n"
        "If none are duplicates reply with all indices."
    )

    if progress_cb:
        progress_cb(f'🔎 AI dedup check: {len(candidates)} candidates vs {len(existing_tickets)} existing…')

    try:
        resp = _req.post(
            f'{ollama_url.rstrip("/")}/api/generate',
            json={'model': ollama_model, 'prompt': prompt, 'stream': False,
                  'options': {'temperature': 0, 'num_predict': 256}},
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get('response', '')
        # extract first JSON array from the response
        m = re.search(r'\[[\d,\s]*\]', raw)
        if m:
            keep_indices = json.loads(m.group())
            result = [candidates[i] for i in keep_indices if i < len(candidates)]
            skipped = len(candidates) - len(result)
            if progress_cb and skipped:
                progress_cb(f'🚫 AI removed {skipped} duplicate(s)')
            return result
    except Exception as e:
        # fallback: return all candidates (title-dedup already ran)
        if progress_cb:
            progress_cb(f'⚠️ AI dedup failed ({e}), using title match only')

    return candidates


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
        'ollama_enabled': bool(body.get('ollama_enabled', False)),
        'ollama_model': str(body.get('ollama_model', 'mistral')).strip(),
        'qa_model': str(body.get('qa_model', '')).strip(),
        'audit_apps': str(body.get('audit_apps', '')).strip(),
        'created': now,
        'updated': now,
    }

    # Mutual exclusivity logic
    enabled = [k for k in ('copilot_enabled', 'localai_enabled', 'freemodel_enabled', 'ollama_enabled') if project_data[k]]
    if len(enabled) > 1:
        # Priority: Copilot > LocalAI > Ollama > Free
        if project_data['copilot_enabled']:
            project_data['localai_enabled'] = False
            project_data['freemodel_enabled'] = False
            project_data['ollama_enabled'] = False
        elif project_data['localai_enabled']:
            project_data['freemodel_enabled'] = False
            project_data['ollama_enabled'] = False
        elif project_data['ollama_enabled']:
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
    if 'ollama_enabled' in body: updates['ollama_enabled'] = bool(body['ollama_enabled'])
    if 'ollama_model' in body: updates['ollama_model'] = str(body['ollama_model']).strip() or 'mistral'
    if 'qa_model' in body: updates['qa_model'] = str(body['qa_model']).strip()
    if 'audit_apps' in body: updates['audit_apps'] = str(body['audit_apps']).strip()

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
    current_state = {k: project.get(k, False) for k in ['copilot_enabled', 'localai_enabled', 'freemodel_enabled', 'ollama_enabled']}
    current_state.update({k: v for k, v in updates.items() if k in current_state})

    enabled = [k for k in current_state if current_state[k]]
    if len(enabled) > 1:
        # Priority: Copilot > LocalAI > Ollama > Free
        if current_state['copilot_enabled']:
            updates['localai_enabled'] = False
            updates['freemodel_enabled'] = False
            updates['ollama_enabled'] = False
        elif current_state['localai_enabled']:
            updates['freemodel_enabled'] = False
            updates['ollama_enabled'] = False
        elif current_state['ollama_enabled']:
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
        elif project.get('ollama_enabled', False):
            agent_type = 'ollama'
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

    if not (project.get('copilot_enabled') or project.get('localai_enabled') or project.get('ollama_enabled') or project.get('freemodel_enabled')):
        return jsonify({'by_day': {}, 'by_model': {}, 'by_month': {}, 'totals': _empty_totals()})

    cache_key = project_id
    now = time.time()
    cached = _ai_usage_cache.get(cache_key)
    if cached and now - cached['ts'] < 60:
        return jsonify(cached['data'])

    ticket_ids = set()
    for t in get_tickets(project_id):
        ticket_ids.add(t['id'])

    log_dirs = [COPILOT_LOG_DIR, LOCALAI_LOG_DIR, OLLAMA_LOG_DIR,
                os.path.join(COPILOT_LOG_DIR, 'archive'),
                os.path.join(LOCALAI_LOG_DIR, 'archive'),
                os.path.join(OLLAMA_LOG_DIR, 'archive')]

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
    if agent not in ('copilot', 'localai', 'ollama'):
        return jsonify({'error': 'Invalid agent'}), 400
    if agent == 'ollama':
        log_dir = OLLAMA_LOG_DIR
    elif agent == 'localai':
        log_dir = LOCALAI_LOG_DIR
    else:
        log_dir = COPILOT_LOG_DIR

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
        if '_local_' in filename:
            agent = 'localai'
        elif '_ollama_' in filename or os.path.exists(os.path.join(OLLAMA_LOG_DIR, filename)):
            agent = 'ollama'
        else:
            agent = 'copilot'
    if agent not in ('copilot', 'localai', 'ollama'):
        return jsonify({'error': 'Invalid agent'}), 400
    if agent == 'ollama':
        log_dir = OLLAMA_LOG_DIR
    elif agent == 'localai':
        log_dir = LOCALAI_LOG_DIR
    else:
        log_dir = COPILOT_LOG_DIR

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


@tickets_bp.route('/ollama-models', methods=['GET'])
def list_ollama_models():
    """Return list of models available on configured Ollama server"""
    import urllib.request
    # Try to get URL from AIChat settings first, fall back to env/default
    ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
    try:
        from blueprints.aichat import _load_config
        username = g.username or 'admin'
        cfg = _load_config(username)
        if cfg.get('ollama_url'):
            ollama_url = cfg['ollama_url'].rstrip('/')
    except Exception:
        pass
    try:
        req = urllib.request.Request(ollama_url + '/api/tags', headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        models = [m['name'] for m in data.get('models', [])]
        return jsonify({'models': models, 'url': ollama_url})
    except Exception as e:
        return jsonify({'models': [], 'error': str(e), 'url': ollama_url})

# ── Bug Hunt Job Store (in-memory, per-process) ─────────────────────────────
_bug_hunt_jobs: dict = {}  # job_id -> {status, progress, log, result, error}
_bug_hunt_lock = threading.Lock()


def _bug_hunt_worker(job_id: str, project_id: str, project: dict, target_app: dict,
                     ollama_url, ollama_model):
    """Background worker: runs SmartAppAuditor and stores result in _bug_hunt_jobs."""

    def progress(msg: str, pct: int = None):
        with _bug_hunt_lock:
            job = _bug_hunt_jobs.get(job_id)
            if job:
                job['log'].append(msg)
                if pct is not None:
                    job['progress'] = pct
        print(f'[bug-hunt {job_id}] {msg}')

    try:
        progress(f'🔍 Analysing: {target_app["name"]} ({target_app["id"]})', 5)

        from blueprints.smart_auditor import SmartAppAuditor
        auditor = SmartAppAuditor(
            target_app['id'],
            target_app['name'],
            target_app,
            ollama_url=ollama_url,
            ollama_model=ollama_model,
            large_context=True,
            progress_cb=progress,
        )

        progress('📂 Loading source files…', 10)
        findings = auditor.analyze()
        meta = findings.get('metadata', {})

        bp_found = meta.get('backend_file',  'not_found') not in ('not_found', None)
        js_found = meta.get('frontend_file', 'not_found') not in ('not_found', None)

        if not bp_found and not js_found:
            with _bug_hunt_lock:
                _bug_hunt_jobs[job_id].update({
                    'status': 'done',
                    'progress': 100,
                    'result': {
                        'warning': f'No source files found for "{target_app["name"]}"',
                        'app': target_app['name'],
                        'count': 0,
                    }
                })
            return

        progress(f'📊 Static analysis done. endpoints={meta.get("endpoints_found",0)} '
                 f'security={len(findings["security"])} perf={len(findings["performance"])}', 60)

        if ollama_model:
            progress(f'🤖 AI insights: {len(findings["ai_insights"])} found', 75)

        tasks_data = auditor.generate_tasks()
        real_findings = (len(findings['security']) + len(findings['performance']) +
                         len(findings['logic']) + len(findings['ai_insights']))

        if real_findings == 0 and len(tasks_data) <= 1:
            with _bug_hunt_lock:
                _bug_hunt_jobs[job_id].update({
                    'status': 'done',
                    'progress': 100,
                    'result': {
                        'warning': f'No issues found in "{target_app["name"]}". Clean audit.',
                        'app': target_app['name'],
                        'count': 0,
                    }
                })
            return

        now = _now()
        target_column = project['columns'][0] if project.get('columns') else 'Backlog'
        username = project.get('owner', 'admin')

        eps   = meta.get('endpoints_found', 0)
        sec   = len(findings['security'])
        perf  = len(findings['performance'])
        ai_i  = len(findings['ai_insights'])
        deps  = meta.get('dependency_count', 0)
        kb    = meta.get('bundle_size_kb', 0)

        epic_desc = (
            f"Comprehensive AI audit of **{target_app['name']}** application.\n"
            f"*{target_app.get('description','')}*\n\n"
            f"**Analysis Summary:**\n"
            f"- Endpoints found: {eps}\n"
            f"- Security issues: {sec}\n"
            f"- Performance concerns: {perf}\n"
            f"- AI insights: {ai_i}\n"
            f"- Dependencies: {deps}\n"
            f"- JS bundle size: {kb} KB\n\n"
            f"**Source files:**\n"
            f"- Backend: `{meta.get('backend_file', 'N/A')}`\n"
            f"- Frontend: `{meta.get('frontend_file', 'N/A')}`"
        )

        # ── Reuse existing EPIC for this app (never duplicate epics) ──
        existing_epic = _find_existing_epic(project_id, target_app['name'])
        if existing_epic:
            epic_id = existing_epic['id']
            update_ticket(epic_id, {'description': epic_desc, 'updated': now})
            progress(f'♻️ Reusing existing EPIC {epic_id}', 80)
            created = []
        else:
            epic_id = _gen_id('t_')
            epic = {
                'id': epic_id, 'project_id': project_id,
                'title': f'[EPIC] Code Audit: {target_app["name"]}',
                'description': epic_desc,
                'column': target_column, 'priority': 'high',
                'assignee': username, 'reporter': username,
                'labels': ['audit', 'auto-generated', 'smart-analysis', target_app['id']],
                'comments': [], 'attachments': [], 'order': 0,
                'created': now, 'updated': now,
            }
            created = [create_ticket(epic)]
            progress(f'🎟️ Created EPIC {epic_id}', 82)

        # ── AI dedup: filter out tasks already represented in the project ──
        existing_tickets = get_tickets(project_id)
        progress(f'🔎 Dedup check: {len(tasks_data)} candidate tasks vs {len(existing_tickets)} existing…', 84)
        unique_tasks = _ai_dedup_filter(
            new_tickets=tasks_data,
            existing_tickets=existing_tickets,
            ollama_url=ollama_url,
            ollama_model=ollama_model,
            progress_cb=progress,
        )

        if not unique_tasks and not created:
            with _bug_hunt_lock:
                _bug_hunt_jobs[job_id].update({
                    'status': 'done', 'progress': 100,
                    'result': {
                        'warning': f'All findings already tracked — no new tickets for "{target_app["name"]}".',
                        'app': target_app['name'], 'count': 0,
                    }
                })
            return

        progress(f'🎟️ Creating {len(unique_tasks)} new ticket(s)…', 88)
        for td in unique_tasks:
            tid = _gen_id('t_')
            created.append(create_ticket({
                'id': tid, 'project_id': project_id,
                'title': td['title'], 'description': td.get('description', ''),
                'column': target_column, 'priority': td.get('priority', 'medium'),
                'assignee': None, 'reporter': username,
                'labels': td.get('labels', []) + [f'epic:{epic_id}'],
                'comments': [], 'attachments': [], 'order': 0,
                'created': now, 'updated': now,
            }))

        _save_audit_history(target_app['id'], target_app['name'], project_id, findings, epic_id)

        try:
            from blueprints.tickets import _emit
            for t in created:
                _emit('ticket_created', project_id, {'ticket': t})
        except Exception:
            pass

        progress(f'✅ Done! Created {len(created)} ticket(s) ({len(tasks_data) - len(unique_tasks)} duplicate(s) skipped).', 100)

        with _bug_hunt_lock:
            _bug_hunt_jobs[job_id].update({
                'status': 'done',
                'progress': 100,
                'result': {
                    'ok': True, 'count': len(created),
                    'app': target_app['name'], 'epic_id': epic_id,
                    'analysis': {
                        'endpoints': eps, 'security_issues': sec,
                        'performance_issues': perf, 'ai_issues': ai_i,
                        'tasks_created': len(unique_tasks),
                        'duplicates_skipped': len(tasks_data) - len(unique_tasks),
                    }
                }
            })

    except Exception as e:
        import traceback as _tb
        msg = f'❌ Error: {str(e)}'
        with _bug_hunt_lock:
            job = _bug_hunt_jobs.get(job_id)
            if job:
                job['log'].append(msg)
                job.update({'status': 'error', 'error': str(e)})
        print(f'[bug-hunt {job_id}] EXCEPTION: {e}\n{_tb.format_exc()}')


@tickets_bp.route('/projects/<project_id>/bug-hunt', methods=['POST'])
def bug_hunt(project_id):
    """Start async audit job — returns job_id immediately."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401

    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not _is_member(project):
        return jsonify({'error': 'Access denied'}), 403

    candidates = [p for p in _ETHOS_PACKAGES if p['id'] not in ('docker-manager',)]
    if not candidates:
        return jsonify({'error': 'No suitable packages found'}), 500

    body = request.get_json(silent=True) or {}
    requested_app_id = body.get('app_id')

    if requested_app_id:
        target_app = next((p for p in candidates if p['id'] == requested_app_id), None)
        if not target_app:
            return jsonify({'error': f'App not found: {requested_app_id}'}), 404
    else:
        try:
            history_path = os.path.normpath(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                '..', 'data', 'audit_schedule.json'))
            schedule = json.load(open(history_path)) if os.path.exists(history_path) else {}
        except Exception:
            schedule = {}
        audited_ids = {k.split(':')[0] for k in schedule.keys()}
        unaudited = [p for p in candidates if p['id'] not in audited_ids]
        target_app = unaudited[0] if unaudited else random.choice(candidates)

    # Ollama config
    ollama_url = None
    ollama_model = None
    if project.get('ollama_enabled'):
        ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
        try:
            from blueprints.aichat import _load_config
            cfg = _load_config(g.username or 'admin')
            if cfg.get('ollama_url'):
                ollama_url = cfg['ollama_url'].rstrip('/')
        except Exception:
            pass
        ollama_model = project.get('ollama_model') or 'mistral'

    # Create job
    job_id = _gen_id('job_')
    with _bug_hunt_lock:
        _bug_hunt_jobs[job_id] = {
            'status': 'running',
            'progress': 0,
            'log': [f'🚀 Starting audit: {target_app["name"]}' +
                    (f' (🤖 {ollama_model})' if ollama_model else '')],
            'result': None,
            'error': None,
            'app': target_app['name'],
            'app_id': target_app['id'],
            'created': time.time(),
        }

    t = threading.Thread(
        target=_bug_hunt_worker,
        args=(job_id, project_id, project, target_app, ollama_url, ollama_model),
        daemon=True,
    )
    t.start()

    return jsonify({'job_id': job_id, 'app': target_app['name']}), 202


@tickets_bp.route('/projects/<project_id>/bug-hunt/status/<job_id>', methods=['GET'])
def bug_hunt_status(project_id, job_id):
    """Poll job status — returns progress, log lines, and final result."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401

    with _bug_hunt_lock:
        job = _bug_hunt_jobs.get(job_id)

    if not job:
        return jsonify({'error': 'Job not found'}), 404

    return jsonify({
        'status':   job['status'],
        'progress': job['progress'],
        'log':      job['log'],
        'result':   job['result'],
        'error':    job['error'],
        'app':      job.get('app'),
    })


@tickets_bp.route('/watcher/status', methods=['GET'])
def watcher_status():
    """Return ticket watcher health: running, last cycle, active job."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401

    import subprocess
    try:
        r = subprocess.run(['systemctl', 'is-active', 'ethos-ticket-watcher'],
                           capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() == 'active'
    except Exception:
        active = False

    lock_info = None
    lock_file = '/tmp/.ethos_watcher_executing'
    if os.path.exists(lock_file):
        try:
            lock_info = json.load(open(lock_file))
            pid = lock_info.get('copilot_pid')
            if pid:
                try:
                    os.kill(pid, 0)
                    lock_info['elapsed'] = round(time.time() - lock_info.get('started', time.time()))
                except (ProcessLookupError, OSError):
                    lock_info = None  # stale lock
        except Exception:
            pass

    status_info = None
    status_file = '/tmp/.ethos_watcher_status'
    if os.path.exists(status_file):
        try:
            status_info = json.load(open(status_file))
            pid = status_info.get('pid')
            age = time.time() - status_info.get('ts', 0)
            if age > 600:  # stale if older than 10 min
                status_info = None
        except Exception:
            pass

    schedule = {}
    sched_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              '..', 'data', 'audit_schedule.json')
    sched_path = os.path.normpath(sched_path)
    if os.path.exists(sched_path):
        try:
            schedule = json.load(open(sched_path))
        except Exception:
            pass

    return jsonify({
        'active': active,
        'lock': lock_info,
        'status': status_info,
        'schedule': schedule,
    })


@tickets_bp.route('/watcher/log', methods=['GET'])
def watcher_log():
    """Return the last N lines of the ticket watcher log, with optional byte offset for polling."""
    if not g.username:
        return jsonify({'error': 'Unauthorized'}), 401
    lines_n = min(int(request.args.get('lines', 200)), 2000)
    offset   = int(request.args.get('offset', 0))
    try:
        size = os.path.getsize(WATCHER_LOG_FILE)
        with open(WATCHER_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            if offset and offset < size:
                f.seek(offset)
                new_text = f.read()
                new_lines = new_text.splitlines()
            else:
                all_lines = f.readlines()
                new_lines = [l.rstrip('\n') for l in all_lines[-lines_n:]]
        return jsonify({'lines': new_lines, 'size': size})
    except FileNotFoundError:
        return jsonify({'lines': [], 'size': 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


    """Save audit results to history"""
    try:
        from blueprints.tickets_db import get_db
        audit_id = _gen_id('audit_')
        now = time.time()

        conn = get_db()
        conn.execute(
            '''INSERT INTO audit_history (id, app_id, app_name, project_id, timestamp, findings, epic_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (audit_id, app_id, app_name, project_id, now, json.dumps(findings), epic_id)
        )
        conn.commit()
    except Exception as e:
        print(f"Failed to save audit history: {e}")
