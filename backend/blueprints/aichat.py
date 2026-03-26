"""
EthOS AI Chat — Personal NAS Intelligence (RAG)
OpenAI-compatible chat with Retrieval-Augmented Generation.
Auto-searches user's documents and gallery for relevant context.
Supports local LLM, OpenAI, Azure OpenAI, or any compatible endpoint.
"""

import json
import os
import sys
import time
import subprocess
import urllib.request
import urllib.error
import ssl
import re
from functools import wraps

import psutil

from flask import Blueprint, request, jsonify, Response, stream_with_context, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import load_json as _load_json, save_json as _save_json, get_username_or, register_pkg_routes, list_directory
from host import data_path
from model_library import (
    get_library as _get_ml,
    get_hardware_info as _get_hw,
    get_full_hardware_info as _get_full_hw,
    get_cpu_features as _get_cpu,
    get_npu_info as _get_npu,
    get_tier as _get_tier,
    get_tier_for_hardware as _get_tier_hw,
)
from rag_engine import (
    get_indexer as _get_rag,
    classify_query as _classify_query,
    build_rag_context as _build_rag_context,
)
import threading as _threading
import collections as _collections

# gevent: grab the REAL start_new_thread to spawn genuine OS threads.
# After monkey.patch_all(), both threading.Thread and _thread.start_new_thread
# are replaced with gevent greenlet wrappers — even "original" Thread class
# still uses the patched _thread internally.  The ONLY way to get a real OS
# thread is via the saved original _thread.start_new_thread.
try:
    import gevent as _gevent
    import gevent.monkey
    _HAS_GEVENT = True
    _native_start_new_thread = gevent.monkey.get_original('_thread', 'start_new_thread')
except Exception:
    _gevent = None
    _HAS_GEVENT = False
    import _thread
    _native_start_new_thread = _thread.start_new_thread

aichat_bp = Blueprint('aichat', __name__, url_prefix='/api/aichat')

# ── Paths ─────────────────────────────────────────────────────────────

def _config_path(username='admin'):
    return data_path(f'aichat_config_{username}.json')

def _history_path(username='admin'):
    return data_path(f'aichat_history_{username}.json')

# ── Auth helpers — use g.username/g.role set by before_request guard ─

def _get_username():
    return get_username_or('admin')

def _is_admin():
    return getattr(g, 'role', None) == 'admin'

def _is_authenticated():
    return getattr(g, 'username', None) is not None


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _is_admin():
            try:
                # Local import to avoid circular dependency
                from blueprints.eventlog import log
                log('security', 'warning', f'Unauthorized access attempt to {request.path}', 
                    details={'user': _get_username(), 'ip': request.remote_addr})
            except Exception:
                pass
            return jsonify({'error': 'Admin only'}), 403
        return f(*args, **kwargs)
    return decorated_function


def _user_sandbox_root():
    """Return the sandbox root for the current user.
    Admins get unrestricted access; non-admins are limited to /home/<username>."""
    if _is_admin():
        return None  # None = no restriction
    username = _get_username()
    return f'/home/{username}'


def _path_in_sandbox(path, sandbox_root=None):
    """Check if a realpath is within the user's sandbox.
    Returns (ok, error_msg or None)."""
    if sandbox_root is None:
        return (True, None)  # admin — no restriction
    rpath = os.path.realpath(path)
    if rpath.startswith(sandbox_root + '/') or rpath == sandbox_root:
        return (True, None)
    return (False, 'Access restricted to home directory')

# ── Default config ─────────────────────────────────────────────────

# N150-aware defaults: detect if local benchmark passed → prefer local provider
def _smart_default_provider():
    """Auto-select provider: 'local' if model is active + deps ok, else 'openai'."""
    try:
        lib = _get_ml()
        active = lib.get_active_model()
        if active:
            try:
                import llama_cpp  # noqa: F401
                return 'local'
            except ImportError:
                pass
    except Exception:
        pass
    return 'openai'


def _smart_default_max_tokens():
    """N150-aware max_tokens: 768 for ≤4 cores, 1024 for ≤8, else 2048."""
    try:
        cores = psutil.cpu_count(logical=False) or 2
        if cores <= 4:
            return 768
        elif cores <= 8:
            return 1024
        return 2048
    except Exception:
        return 1024


_CONFIG_DEFAULTS = {
    'provider': 'local',            # openai | azure | custom | local
    'api_key': '',
    'endpoint': 'https://api.openai.com/v1/chat/completions',
    'model': 'gpt-4o',
    'max_tokens': 768,              # N150-safe default
    'temperature': 0.7,
    'system_prompt': 'You are a home NAS assistant for EthOS. Respond briefly and to the point. '
                     'Your task is to help the user manage the NAS system, '
                     'browse the photo gallery, search files, and manage projects. '
                     'You have access to tools: create_ticket (creating tasks in Kanban), '
                     'list_tickets (checking board status). '
                     'When the user reports a problem or requests a task — use create_ticket. '
                     'You have access to the user\'s knowledge base (RAG). '
                     'Respond in English unless the user writes in another language.',
    'workspace': '',                # default workspace path for file browsing
    'rag_enabled': True,            # auto RAG context injection
    'rag_top_k': 3,                 # max context fragments (keep low for N150 prefill speed)
}

# ── Token estimation & context management ─────────────────────────

# Model context windows (tokens)
_MODEL_CONTEXT = {
    'gpt-4o': 128000, 'gpt-4o-mini': 128000,
    'gpt-4-turbo': 128000, 'gpt-4': 8192,
    'gpt-3.5-turbo': 16385,
}
_DEFAULT_CONTEXT_WINDOW = 4096  # conservative for local models


def _estimate_tokens(text):
    """Rough token estimate: ~4 chars per token for English, ~3 for Polish."""
    if not text:
        return 0
    return max(1, len(text) // 3)


def _trim_messages_to_fit(messages, max_context_tokens, max_response_tokens):
    """Trim conversation history to fit within context window.

    Always keeps: system prompt (first msg) + last user message.
    Removes oldest messages first until total fits.
    Returns trimmed list + whether trimming occurred.
    """
    budget = max_context_tokens - max_response_tokens - 200  # 200 token safety margin
    if budget < 500:
        budget = 500

    # Always keep system (idx 0) and last message
    if len(messages) <= 2:
        return messages, False

    system_msg = messages[0]
    last_msg = messages[-1]
    middle = messages[1:-1]

    system_tokens = _estimate_tokens(system_msg.get('content', ''))
    last_tokens = _estimate_tokens(last_msg.get('content', ''))
    fixed_tokens = system_tokens + last_tokens

    if fixed_tokens >= budget:
        # Even system + last don't fit — truncate last message content
        avail = budget - system_tokens
        if avail < 200:
            avail = 200
        content = last_msg.get('content', '')
        # Rough: 3 chars per token
        max_chars = avail * 3
        if len(content) > max_chars:
            last_msg = dict(last_msg)
            last_msg['content'] = content[:max_chars] + '\n\n[...message truncated due to context limit]'
        return [system_msg, last_msg], True

    # Fill from newest to oldest
    remaining_budget = budget - fixed_tokens
    kept = []
    for msg in reversed(middle):
        msg_tokens = _estimate_tokens(msg.get('content', ''))
        if msg_tokens <= remaining_budget:
            kept.append(msg)
            remaining_budget -= msg_tokens
        else:
            break  # stop including older messages

    kept.reverse()
    trimmed = len(kept) < len(middle)
    result = [system_msg] + kept + [last_msg]
    return result, trimmed


# ── AI Tools (function calling) ──────────────────────────────────

TICKET_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Creates a new ticket in the EthOS Kanban system. Use when the user asks to create a task, reports a problem, or suggests an improvement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short ticket title, prefixed with a tag e.g. [FE], [BE], [DevOps]"},
                    "description": {"type": "string", "description": "Detailed description of the problem or task"},
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"], "description": "Ticket priority"},
                    "column": {"type": "string", "enum": ["Backlog", "To Do"], "description": "Target column, defaults to Backlog"},
                },
                "required": ["title", "description", "priority"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_tickets",
            "description": "Retrieves a list of tickets from the Kanban board. Use to check project status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "column": {"type": "string", "description": "Filter by column (optional)"},
                },
            }
        }
    },
]


def _execute_tool(tool_name, args, username):
    """Execute an AI tool and return result string."""
    from blueprints.tickets import _load, _save, _gen_id, _now, _emit

    if tool_name == 'create_ticket':
        data = _load()
        # Find first copilot-enabled project or first project the user owns
        project = None
        for p in data.get('projects', []):
            if p.get('copilot_enabled') and username in p.get('members', []):
                project = p
                break
        if not project:
            for p in data.get('projects', []):
                if username in p.get('members', []):
                    project = p
                    break
        if not project:
            return "No available projects. Create a project in the ticket system."

        now = _now()
        ticket = {
            'id': _gen_id('t_'),
            'title': args.get('title', 'New ticket'),
            'description': args.get('description', ''),
            'project_id': project['id'],
            'column': args.get('column', 'Backlog'),
            'priority': args.get('priority', 'medium'),
            'assignee': '',
            'reporter': username,
            'labels': [],
            'comments': [],
            'order': 0,
            'created': now,
            'updated': now,
        }
        data['tickets'].append(ticket)
        _save(data)

        # Emit socket event if available
        _emit('ticket_created', project['id'], {'ticket': ticket})

        return f"Ticket created: [{ticket['priority'].upper()}] {ticket['title']} (id: {ticket['id']}, project: {project['name']}, column: {ticket['column']})"

    elif tool_name == 'list_tickets':
        data = _load()
        col_filter = args.get('column', '')
        results = []
        for p in data.get('projects', []):
            if username not in p.get('members', []):
                continue
            tickets = [t for t in data.get('tickets', []) if t['project_id'] == p['id']]
            if col_filter:
                tickets = [t for t in tickets if t['column'] == col_filter]
            for t in tickets:
                results.append(f"[{t['column']}] [{t['priority'].upper()}] {t['title']} (id: {t['id']})")
        if not results:
            return "No tickets" + (f" in column '{col_filter}'" if col_filter else "")
        return f"Found {len(results)} tickets:\n" + "\n".join(results)

    return f"Unknown tool: {tool_name}"


# ── Config helpers ─────────────────────────────────────────────────

def _load_config(username='admin'):
    cfg = dict(_CONFIG_DEFAULTS)
    saved = _load_json(_config_path(username), None)
    if saved is not None:
        cfg.update(saved)
    # Auto-detect provider for fresh configs
    if saved is None or 'provider' not in (saved or {}):
        cfg['provider'] = _smart_default_provider()
        cfg['max_tokens'] = _smart_default_max_tokens()
    return cfg

def _save_config(cfg, username='admin'):
    _save_json(_config_path(username), cfg)

# ── History helpers ────────────────────────────────────────────────

MAX_CONVERSATIONS = 50
MAX_MESSAGES_PER_CONV = 200

def _load_history(username='admin'):
    return _load_json(_history_path(username), [])

def _save_history(history, username='admin'):
    history = history[-MAX_CONVERSATIONS:]
    _save_json(_history_path(username), history)

# ── File helpers ───────────────────────────────────────────────────

_TEXT_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.htm', '.css', '.scss',
    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
    '.md', '.txt', '.rst', '.log', '.csv',
    '.sh', '.bash', '.zsh', '.fish',
    '.c', '.h', '.cpp', '.hpp', '.java', '.go', '.rs', '.rb', '.php',
    '.sql', '.xml', '.env', '.gitignore', '.dockerignore',
    '.dockerfile', '.makefile', '.cmake',
    '.vue', '.svelte', '.astro',
}

_MAX_FILE_SIZE = 512 * 1024  # 512 KB

def _is_text_file(path):
    _, ext = os.path.splitext(path.lower())
    if ext in _TEXT_EXTENSIONS:
        return True
    basename = os.path.basename(path).lower()
    if basename in ('makefile', 'dockerfile', 'vagrantfile', 'gemfile',
                    'rakefile', 'procfile', '.env', '.gitignore',
                    'docker-compose.yml', 'docker-compose.yaml',
                    'requirements.txt', 'package.json', 'tsconfig.json'):
        return True
    return False


# ══════════════════════════════════════════════════════════════════
#  Routes: Config
# ══════════════════════════════════════════════════════════════════

@aichat_bp.route('/config', methods=['GET'])
def get_config():
    username = _get_username()
    cfg = _load_config(username)
    safe = dict(cfg)
    if safe.get('api_key'):
        k = safe['api_key']
        safe['api_key_masked'] = k[:8] + '…' + k[-4:] if len(k) > 12 else '••••••••'
        safe['api_key_set'] = True
    else:
        safe['api_key_masked'] = ''
        safe['api_key_set'] = False
    del safe['api_key']
    return jsonify(safe)


@aichat_bp.route('/config', methods=['POST'])
def save_config():
    username = _get_username()
    data = request.json or {}
    cfg = _load_config(username)
    for key in ('provider', 'endpoint', 'model', 'max_tokens', 'temperature',
                'system_prompt', 'workspace', 'rag_enabled', 'rag_top_k'):
        if key in data:
            cfg[key] = data[key]
    if 'api_key' in data:
        cfg['api_key'] = data['api_key']
    _save_config(cfg, username)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
#  Routes: Conversations
# ══════════════════════════════════════════════════════════════════

@aichat_bp.route('/conversations', methods=['GET'])
def list_conversations():
    username = _get_username()
    history = _load_history(username)
    result = []
    for conv in reversed(history):
        result.append({
            'id': conv['id'],
            'title': conv.get('title', 'New conversation'),
            'created': conv.get('created', ''),
            'updated': conv.get('updated', ''),
            'message_count': len(conv.get('messages', [])),
        })
    return jsonify(result)


@aichat_bp.route('/conversations', methods=['POST'])
def create_conversation():
    username = _get_username()
    history = _load_history(username)
    conv_id = f"conv_{int(time.time() * 1000)}"
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    conv = {
        'id': conv_id,
        'title': 'New conversation',
        'messages': [],
        'created': now,
        'updated': now,
    }
    history.append(conv)
    _save_history(history, username)
    return jsonify(conv)


@aichat_bp.route('/conversations/<conv_id>', methods=['GET'])
def get_conversation(conv_id):
    username = _get_username()
    history = _load_history(username)
    conv = next((c for c in history if c['id'] == conv_id), None)
    if not conv:
        return jsonify({'error': 'Conversation not found'}), 404
    return jsonify(conv)


@aichat_bp.route('/conversations/<conv_id>', methods=['DELETE'])
def delete_conversation(conv_id):
    username = _get_username()
    history = _load_history(username)
    history = [c for c in history if c['id'] != conv_id]
    _save_history(history, username)
    return jsonify({'ok': True})


@aichat_bp.route('/conversations/<conv_id>/title', methods=['PUT'])
def rename_conversation(conv_id):
    username = _get_username()
    data = request.json or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'No title'}), 400
    history = _load_history(username)
    conv = next((c for c in history if c['id'] == conv_id), None)
    if not conv:
        return jsonify({'error': 'Conversation not found'}), 404
    conv['title'] = title[:120]
    _save_history(history, username)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
#  Routes: Dev tools — file browser / read / write
# ══════════════════════════════════════════════════════════════════

@aichat_bp.route('/files/browse', methods=['GET'])
def browse_files():
    """List directory contents for the file picker. Admin: unrestricted. Non-admin: sandboxed to /home/<user>."""
    if not _is_authenticated():
        return jsonify({'error': 'Permission denied'}), 403

    sandbox = _user_sandbox_root()
    default_path = sandbox if sandbox else '/home'
    path = request.args.get('path', default_path)
    show_hidden = request.args.get('hidden', '0') == '1'
    _DOTFILE_WHITELIST = {'.env', '.gitignore', '.dockerignore'}

    # Sandbox check for non-admins
    ok, err = _path_in_sandbox(path, sandbox)
    if not ok:
        return jsonify({'error': err}), 403

    try:
        path = os.path.realpath(path)
        items, err = list_directory(path, show_hidden=True, include_size=True)
        if err:
            code = 404 if 'Not a directory' in err else 403
            return jsonify({'error': err}), code
        # Apply hidden-file filter with whitelist
        if not show_hidden:
            items = [i for i in items if not i['name'].startswith('.') or i['name'] in _DOTFILE_WHITELIST]
        # Add is_text flag
        for i in items:
            i['is_text'] = _is_text_file(i['path']) if not i['is_dir'] else False
        return jsonify({'path': path, 'items': items})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@aichat_bp.route('/files/read', methods=['POST'])
def read_file_content():
    """Read file contents for AI context. Accepts multiple files. Sandboxed for non-admins."""
    if not _is_authenticated():
        return jsonify({'error': 'Permission denied'}), 403

    sandbox = _user_sandbox_root()
    data = request.json or {}
    paths = data.get('paths', [])
    if not paths:
        return jsonify({'error': 'No files'}), 400

    # Sensitive paths that must never be exposed via AI chat
    _SENSITIVE_PREFIXES = ('/proc/', '/sys/', '/dev/')
    _SENSITIVE_FILES = ('/etc/shadow', '/etc/gshadow', '/etc/sudoers')

    results = []
    for p in paths[:20]:
        p = os.path.realpath(p)
        # Sandbox check
        ok, err = _path_in_sandbox(p, sandbox)
        if not ok:
            results.append({'path': p, 'error': err})
            continue
        if any(p.startswith(pfx) for pfx in _SENSITIVE_PREFIXES) or p in _SENSITIVE_FILES:
            results.append({'path': p, 'error': 'Access denied to system file'})
            continue
        if not os.path.isfile(p):
            results.append({'path': p, 'error': 'Not found'})
            continue
        try:
            size = os.path.getsize(p)
            if size > _MAX_FILE_SIZE:
                results.append({'path': p, 'error': f'Too large ({size // 1024} KB > {_MAX_FILE_SIZE // 1024} KB)',
                                'size': size})
                continue
            with open(p, 'r', errors='replace') as f:
                content = f.read()
            results.append({'path': p, 'content': content, 'size': size})
        except Exception as e:
            results.append({'path': p, 'error': str(e)})

    return jsonify({'files': results})


@aichat_bp.route('/files/write', methods=['POST'])
def write_file_content():
    """Write content to a file (for applying AI-suggested code). Sandboxed for non-admins."""
    if not _is_authenticated():
        return jsonify({'error': 'Permission denied'}), 403

    sandbox = _user_sandbox_root()
    data = request.json or {}
    path = data.get('path', '').strip()
    content = data.get('content', '')

    if not path:
        return jsonify({'error': 'Path required'}), 400

    path = os.path.realpath(path)

    # Sandbox check for non-admins
    ok, err = _path_in_sandbox(path, sandbox)
    if not ok:
        return jsonify({'error': err}), 403

    # Allowlist: only permit writes under user homes, /tmp, or the NAS data area
    _ALLOWED_PREFIXES = ('/home/', '/tmp/', data_path(''))
    if not any(path.startswith(pfx) for pfx in _ALLOWED_PREFIXES):
        return jsonify({'error': 'Writing allowed only in home directories and NAS data'}), 403

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        return jsonify({'ok': True, 'path': path, 'size': len(content)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#  Routes: Dev tools — command execution
# ══════════════════════════════════════════════════════════════════

@aichat_bp.route('/exec', methods=['POST'])
def exec_command():
    """Execute a shell command and return output. Admin only."""
    if not _is_admin():
        return jsonify({'error': 'Permission denied'}), 403

    data = request.json or {}
    cmd = data.get('command', '').strip()
    cwd = data.get('cwd', '/home')
    timeout = min(data.get('timeout', 30), 120)

    if not cmd:
        return jsonify({'error': 'No command'}), 400

    if cwd and not os.path.isdir(cwd):
        return jsonify({'error': 'Working directory does not exist'}), 400

    # Block destructive system-level commands
    import re as _re
    _DANGEROUS = _re.compile(
        r'\b(rm\s+-rf\s+/|mkfs\.|dd\s+.*of=/dev/|:(){ :|shutdown|reboot|halt|poweroff|init\s+[06])\b',
        _re.IGNORECASE,
    )
    if _DANGEROUS.search(cmd):
        return jsonify({'error': 'Command blocked for security reasons'}), 403

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=timeout
        )
        return jsonify({
            'ok': True,
            'stdout': result.stdout[-50000:] if len(result.stdout) > 50000 else result.stdout,
            'stderr': result.stderr[-10000:] if len(result.stderr) > 10000 else result.stderr,
            'exit_code': result.returncode,
        })
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'error': f'Timeout exceeded ({timeout}s)',
                        'stdout': '', 'stderr': '', 'exit_code': -1})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e),
                        'stdout': '', 'stderr': '', 'exit_code': -1})


# ══════════════════════════════════════════════════════════════════
#  Routes: Chat with file context
# ══════════════════════════════════════════════════════════════════

@aichat_bp.route('/chat', methods=['POST'])
def chat():
    """Send a message with optional file context, RAG context, and stream AI response via SSE."""
    username = _get_username()
    data = request.json or {}
    conv_id = data.get('conversation_id')
    user_message = (data.get('message') or '').strip()
    attached_files = data.get('files', [])  # [{ path, content }]

    if not user_message:
        return jsonify({'error': 'No message'}), 400

    cfg = _load_config(username)
    provider = cfg.get('provider')
    req_provider = data.get('provider_override')
    if isinstance(req_provider, str):
        rp = req_provider.strip()
        if rp in ('local', 'openai', 'azure', 'custom'):
            provider = rp
    # Allow per-request override of RAG usage (without changing saved config)
    req_rag_enabled = data.get('rag_enabled')
    rag_enabled = cfg.get('rag_enabled', True) if req_rag_enabled is None else bool(req_rag_enabled)
    is_local = provider == 'local'
    if not is_local and not cfg.get('api_key'):
        return jsonify({'error': 'API key not configured. Open settings (⚙) and enter your key.'}), 400

    # Load/create conversation
    history = _load_history(username)
    conv = None
    if conv_id:
        conv = next((c for c in history if c['id'] == conv_id), None)
    if not conv:
        conv_id = f"conv_{int(time.time() * 1000)}"
        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        conv = {'id': conv_id, 'title': 'New conversation', 'messages': [], 'created': now, 'updated': now}
        history.append(conv)

    # ── RAG: auto-inject context if enabled and no manual files attached ──
    rag_context = ''
    rag_sources = []
    if rag_enabled and not attached_files:
        try:
            sandbox = _user_sandbox_root()
            indexer = _get_rag(username, sandbox)
            stats = indexer.store.get_stats()
            if stats.get('total_chunks', 0) > 0:
                # Classify query → gallery / documents / both
                idx_type = _classify_query(user_message)
                top_k = int(cfg.get('rag_top_k', 5))
                results = indexer.search(user_message, index_type=idx_type, top_k=top_k)
                if results:
                    rag_context, rag_sources = _build_rag_context(results, user_message)
        except Exception:
            pass  # RAG failure should never block chat

    # Build full user message with file context for the API
    full_user_msg = user_message
    if attached_files:
        file_ctx_parts = []
        for af in attached_files[:10]:
            fpath = af.get('path', '?')
            fcontent = af.get('content', '')
            if fcontent:
                file_ctx_parts.append(f"--- {fpath} ---\n{fcontent}\n--- end {fpath} ---")
        if file_ctx_parts:
            full_user_msg = "Attached files:\n\n" + "\n\n".join(file_ctx_parts) + "\n\n" + user_message
    elif rag_context:
        # Inject RAG context before the user question
        full_user_msg = rag_context + "\n\nUser question: " + user_message

    # Store compact display version (without file dump or RAG context)
    display_msg = user_message
    if attached_files:
        file_names = [af.get('path', '?').split('/')[-1] for af in attached_files]
        display_msg = '\U0001f4ce ' + ', '.join(file_names) + '\n\n' + user_message

    conv['messages'].append({
        'role': 'user',
        'content': display_msg,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    if len(conv['messages']) > MAX_MESSAGES_PER_CONV:
        conv['messages'] = conv['messages'][-MAX_MESSAGES_PER_CONV:]

    if conv['title'] == 'New conversation' and user_message:
        conv['title'] = user_message[:80] + ('\u2026' if len(user_message) > 80 else '')

    conv['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    _save_history(history, username)

    # Build API messages — use full_user_msg only for the LAST user message
    api_messages = []
    api_messages.append({'role': 'system', 'content': _CONFIG_DEFAULTS['system_prompt']})
    for i, m in enumerate(conv['messages']):
        if i == len(conv['messages']) - 1 and m['role'] == 'user':
            api_messages.append({'role': 'user', 'content': full_user_msg})
        else:
            api_messages.append({'role': m['role'], 'content': m['content']})

    # ── Smart context trimming — prevent token overflow ──
    req_model = data.get('model')
    model_name = cfg.get('model', 'gpt-4o')
    if isinstance(req_model, str) and req_model.strip():
        model_name = req_model.strip()
    context_window = _MODEL_CONTEXT.get(model_name, _DEFAULT_CONTEXT_WINDOW)
    if is_local:
        context_window = min(context_window, 4096)  # local models typically small
    max_resp_tokens = int(cfg.get('max_tokens', 4096))
    api_messages, was_trimmed = _trim_messages_to_fit(api_messages, context_window, max_resp_tokens)

    def generate():
        full_response = ''
        try:
            meta = {'type': 'meta', 'conversation_id': conv_id, 'rag_sources': rag_sources}
            if was_trimmed:
                meta['context_trimmed'] = True
            yield f"data: {json.dumps(meta)}\n\n"

            if is_local:
                # ── LOCAL MODEL via llama-cpp-python ──
                lib = _get_ml()
                active = lib.get_active_model()
                if not active:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'No active model. Download and activate a model in the Model Library.'})}\n\n"
                    return

                # Check if model needs loading — tell user to wait
                _cur_loaded = lib.get_loaded_model()
                if _cur_loaded[0] is None or _cur_loaded[1] != active.get('id'):
                    model_name = active.get('name', active.get('id', 'model'))
                    yield f"data: {json.dumps({'type': 'token', 'content': f'⏳ Loading model {model_name}… '})}\n\n"

                llm, err = lib.load_model()
                if err:
                    yield f"data: {json.dumps({'type': 'error', 'error': err})}\n\n"
                    return

                lib.touch_model()  # reset idle unload timer

                # ── Run inference in a REAL OS thread (pre-monkey-patch) ──
                # Token buffer: collections.deque is CPython GIL-safe for
                # single-producer (NativeThread) single-consumer (greenlet).
                # The greenlet polls non-blockingly and calls gevent.sleep()
                # to yield — NO native locks that would block the gevent hub.
                _token_buf  = _collections.deque()
                _done_flag  = [False]
                _error_ref  = [None]

                _inf_max_tokens  = int(cfg.get('max_tokens', 4096))
                _inf_temperature = float(cfg.get('temperature', 0.7))

                def _inference_worker():
                    try:
                        try:
                            os.nice(15)
                        except OSError:
                            pass
                        # NOTE: do NOT call lib.touch_model() here — it creates
                        # gevent Timer objects, which is unsafe from a NativeThread.
                        # touch_model() was already called in the greenlet above.
                        resp = llm.create_chat_completion(
                            messages=api_messages,
                            max_tokens=_inf_max_tokens,
                            temperature=_inf_temperature,
                            stream=True,
                        )
                        for chunk in resp:
                            delta = chunk.get('choices', [{}])[0].get('delta', {})
                            tok = delta.get('content', '')
                            if tok:
                                _token_buf.append(tok)
                    except Exception as ex:
                        _error_ref[0] = ex
                    finally:
                        _done_flag[0] = True

                _native_start_new_thread(_inference_worker, ())

                # ── SSE polling loop (non-blocking) ──
                # deque.popleft() is atomic under CPython GIL.
                # gevent.sleep() cooperatively yields so the hub can serve
                # other HTTP connections while we wait for tokens.
                while True:
                    # drain all available tokens first
                    while _token_buf:
                        tok = _token_buf.popleft()
                        full_response += tok
                        yield f"data: {json.dumps({'type': 'token', 'content': tok})}\n\n"

                    if _error_ref[0]:
                        yield f"data: {json.dumps({'type': 'error', 'error': f'Inference error: {_error_ref[0]}'})}\n\n"
                        return

                    if _done_flag[0]:
                        # worker finished — drain any last tokens
                        while _token_buf:
                            tok = _token_buf.popleft()
                            full_response += tok
                            yield f"data: {json.dumps({'type': 'token', 'content': tok})}\n\n"
                        break

                    # nothing ready yet — yield to gevent hub
                    if _HAS_GEVENT:
                        _gevent.sleep(0.05)
                    else:
                        time.sleep(0.05)

            else:
                # ── REMOTE API (OpenAI / Azure / Custom) ──
                api_body = {
                    'model': model_name,
                    'messages': api_messages,
                    'max_tokens': int(cfg.get('max_tokens', 4096)),
                    'temperature': float(cfg.get('temperature', 0.7)),
                    'stream': True,
                    'tools': TICKET_TOOLS,
                }
                body = json.dumps(api_body).encode('utf-8')

                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f"Bearer {cfg['api_key']}",
                }

                endpoint = cfg.get('endpoint', _CONFIG_DEFAULTS['endpoint'])

                # Tool call loop — may need multiple rounds
                max_tool_rounds = 5
                for _round in range(max_tool_rounds):
                    req = urllib.request.Request(endpoint, data=json.dumps(api_body).encode('utf-8'),
                                                headers=headers, method='POST')
                    ctx = ssl.create_default_context()
                    resp = urllib.request.urlopen(req, context=ctx, timeout=120)

                    buf = b''
                    tool_calls_acc = {}  # {index: {id, name, arguments_str}}
                    round_content = ''

                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b'\n' in buf:
                            line_bytes, buf = buf.split(b'\n', 1)
                            line = line_bytes.decode('utf-8', errors='replace').strip()
                            if not line:
                                continue
                            if line == 'data: [DONE]':
                                break
                            if line.startswith('data: '):
                                json_str = line[6:]
                                try:
                                    obj = json.loads(json_str)
                                    delta = obj.get('choices', [{}])[0].get('delta', {})
                                    content = delta.get('content', '')
                                    if content:
                                        round_content += content
                                        full_response += content
                                        yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"

                                    # Accumulate tool calls
                                    for tc in delta.get('tool_calls', []):
                                        idx = tc.get('index', 0)
                                        if idx not in tool_calls_acc:
                                            tool_calls_acc[idx] = {'id': '', 'name': '', 'arguments': ''}
                                        if tc.get('id'):
                                            tool_calls_acc[idx]['id'] = tc['id']
                                        fn = tc.get('function', {})
                                        if fn.get('name'):
                                            tool_calls_acc[idx]['name'] = fn['name']
                                        if fn.get('arguments'):
                                            tool_calls_acc[idx]['arguments'] += fn['arguments']
                                except (json.JSONDecodeError, IndexError, KeyError):
                                    pass

                    resp.close()

                    if not tool_calls_acc:
                        break  # No tool calls — normal response, done

                    # Execute tool calls
                    # Add assistant message with tool_calls to conversation
                    assistant_tc_msg = {'role': 'assistant', 'content': round_content or None, 'tool_calls': []}
                    for idx in sorted(tool_calls_acc.keys()):
                        tc = tool_calls_acc[idx]
                        assistant_tc_msg['tool_calls'].append({
                            'id': tc['id'],
                            'type': 'function',
                            'function': {'name': tc['name'], 'arguments': tc['arguments']}
                        })
                    api_body['messages'].append(assistant_tc_msg)

                    for idx in sorted(tool_calls_acc.keys()):
                        tc = tool_calls_acc[idx]
                        try:
                            tool_args = json.loads(tc['arguments'])
                        except json.JSONDecodeError:
                            tool_args = {}

                        tool_name = tc['name']
                        tool_msg = '\n🔧 *' + tool_name + '*...'
                        yield f"data: {json.dumps({'type': 'token', 'content': tool_msg})}\n\n"
                        full_response += tool_msg

                        result = _execute_tool(tool_name, tool_args, username)

                        result_msg = ' ✅\n> ' + result + '\n\n'
                        yield f"data: {json.dumps({'type': 'token', 'content': result_msg})}\n\n"
                        full_response += result_msg

                        api_body['messages'].append({
                            'role': 'tool',
                            'tool_call_id': tc['id'],
                            'content': result,
                        })

                    # Continue loop — model will generate final response after tool results
                    api_body['stream'] = True

            # ── Save assistant response ──
            if full_response:
                hist = _load_history(username)
                c = next((x for x in hist if x['id'] == conv_id), None)
                if c:
                    c['messages'].append({
                        'role': 'assistant',
                        'content': full_response,
                        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    })
                    c['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S')
                    _save_history(hist, username)

            yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id})}\n\n"

        except urllib.error.HTTPError as e:
            err_body = ''
            try:
                err_body = e.read().decode('utf-8', errors='replace')[:500]
            except Exception:
                pass
            msg = f"API error ({e.code}): {err_body}" if err_body else f"API error: HTTP {e.code}"
            yield f"data: {json.dumps({'type': 'error', 'error': msg})}\n\n"
        except urllib.error.URLError as e:
            yield f"data: {json.dumps({'type': 'error', 'error': f'Cannot connect to API: {e.reason}'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': f'Error: {str(e)}'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


# ══════════════════════════════════════════════════════════════════
#  Routes: Model Library
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
#  Routes: RAG — Retrieval-Augmented Generation
# ══════════════════════════════════════════════════════════════════

# ── Scheduler helpers ──────────────────────────────────────────

_TIMER_UNIT = 'rag_index_cron.timer'
_TIMER_FILE = '/etc/systemd/system/rag_index_cron.timer'


def _scheduler_status():
    """Get systemd timer status."""
    try:
        active = subprocess.run(
            ['systemctl', 'is-active', _TIMER_UNIT],
            capture_output=True, text=True, timeout=5
        ).stdout.strip() == 'active'

        enabled = subprocess.run(
            ['systemctl', 'is-enabled', _TIMER_UNIT],
            capture_output=True, text=True, timeout=5
        ).stdout.strip() == 'enabled'

        # Read interval from timer file
        interval = 'hourly'
        if os.path.isfile(_TIMER_FILE):
            with open(_TIMER_FILE) as f:
                for line in f:
                    if line.strip().startswith('OnCalendar='):
                        interval = line.strip().split('=', 1)[1]
                        break

        return {'active': active, 'enabled': enabled, 'interval': interval}
    except Exception:
        return {'active': False, 'enabled': False, 'interval': 'hourly'}


@aichat_bp.route('/rag/status', methods=['GET'])
def rag_status():
    """Get RAG index status for current user."""
    username = _get_username()
    sandbox = _user_sandbox_root()
    indexer = _get_rag(username, sandbox)
    status = indexer.get_status()
    cfg = _load_config(username)
    status['rag_enabled'] = cfg.get('rag_enabled', True)
    # Add progress info if indexing
    if hasattr(indexer, '_progress') and indexer._progress:
        status['progress'] = dict(indexer._progress)
    else:
        status['progress'] = None
    # Scheduler info
    status['scheduler'] = _scheduler_status()
    return jsonify(status)


@aichat_bp.route('/rag/index', methods=['POST'])
def rag_index():
    """Start indexing a directory. Runs in background thread."""
    if not _is_authenticated():
        return jsonify({'error': 'Permission denied'}), 403

    username = _get_username()
    sandbox = _user_sandbox_root()
    body = request.get_json(silent=True) or {}
    directory = body.get('directory', '').strip()

    if not directory:
        # Default: index user's home directory
        directory = sandbox or f'/home/{username}'

    # Validate directory: must exist and be within user's sandbox
    if not os.path.isdir(directory):
        return jsonify({'error': 'Specified path is not a directory'}), 400
    ok, err = _path_in_sandbox(directory, sandbox)
    if not ok:
        return jsonify({'error': err}), 403

    indexer = _get_rag(username, sandbox)
    if indexer._indexing:
        return jsonify({'error': 'Indexing already in progress'}), 409

    sio = None
    try:
        from flask import current_app
        sio = current_app.extensions.get('socketio')
    except Exception:
        pass

    def _bg_index():
        indexer.index_directory(directory, recursive=True, socketio=sio)

    if _HAS_GEVENT:
        _gevent.spawn(_bg_index)
    else:
        _threading.Thread(target=_bg_index, daemon=True).start()
    return jsonify({'status': 'ok', 'directory': directory})


@aichat_bp.route('/rag/index-internal', methods=['POST'])
def rag_index_internal():
    """Internal endpoint for cron/systemd — localhost only, no session auth."""
    remote = request.remote_addr or ''
    if remote not in ('127.0.0.1', '::1', 'localhost'):
        return jsonify({'error': 'Localhost only'}), 403

    body = request.get_json(silent=True) or {}
    username = body.get('username', '').strip()
    if not username:
        return jsonify({'error': 'Username required'}), 400
    directory = body.get('directory', '').strip() or f'/home/{username}'

    # Non-admin sandbox: force to /home/<username>
    sandbox = f'/home/{username}'

    # Validate directory: must exist and be within user's sandbox
    if not os.path.isdir(directory):
        return jsonify({'error': 'Specified path is not a directory'}), 400
    ok, err = _path_in_sandbox(directory, sandbox)
    if not ok:
        return jsonify({'error': err}), 403

    indexer = _get_rag(username, sandbox)
    if indexer._indexing:
        return jsonify({'error': 'Indexing already in progress'}), 409

    def _bg_index():
        indexer.index_directory(directory, recursive=True)

    if _HAS_GEVENT:
        _gevent.spawn(_bg_index)
    else:
        _threading.Thread(target=_bg_index, daemon=True).start()
    return jsonify({'status': 'ok', 'directory': directory})


@aichat_bp.route('/rag/search', methods=['POST'])
def rag_search():
    """Manual RAG search. Returns matching chunks with scores."""
    if not _is_authenticated():
        return jsonify({'error': 'Permission denied'}), 403

    username = _get_username()
    sandbox = _user_sandbox_root()
    body = request.get_json(silent=True) or {}
    query = body.get('query', '').strip()
    index_type = body.get('index_type')  # 'documents', 'gallery', or null
    top_k = min(int(body.get('top_k', 5)), 20)

    if not query:
        return jsonify({'error': 'Query required'}), 400

    indexer = _get_rag(username, sandbox)
    results = indexer.search(query, index_type=index_type, top_k=top_k)
    return jsonify({'results': results, 'query': query, 'index_type': index_type})


@aichat_bp.route('/rag/clear', methods=['POST'])
def rag_clear():
    """Clear the RAG index for current user."""
    if not _is_authenticated():
        return jsonify({'error': 'Permission denied'}), 403

    username = _get_username()
    sandbox = _user_sandbox_root()
    indexer = _get_rag(username, sandbox)
    indexer.clear()
    return jsonify({'status': 'ok'})


@aichat_bp.route('/rag/scheduler', methods=['GET'])
def rag_scheduler_status():
    """Get scheduler status."""
    return jsonify(_scheduler_status())


@aichat_bp.route('/rag/scheduler', methods=['POST'])
def rag_scheduler_toggle():
    """Enable/disable scheduler, change interval. Admin only."""
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403

    body = request.get_json(silent=True) or {}
    action = body.get('action', '').strip()  # 'enable', 'disable', 'set_interval'
    interval = body.get('interval', '').strip()  # e.g. 'hourly', 'daily', '*:0/30'

    _VALID_INTERVALS = {
        'hourly': 'hourly',
        'daily': 'daily',
        'every_6h': '*-*-* 0/6:00:00',
        'every_12h': '*-*-* 0/12:00:00',
        'every_30min': '*:0/30',
    }

    try:
        if action == 'enable':
            subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', 'enable', '--now', _TIMER_UNIT],
                           capture_output=True, timeout=10)
            return jsonify({'status': 'ok'})

        elif action == 'disable':
            subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', 'disable', '--now', _TIMER_UNIT],
                           capture_output=True, timeout=10)
            return jsonify({'status': 'ok'})

        elif action == 'set_interval':
            cal_value = _VALID_INTERVALS.get(interval, interval)
            if not cal_value:
                return jsonify({'error': 'Interval required'}), 400

            # Security check: validate systemd time format to prevent injection
            # Allow: alnum, space, *, /, -, :, comma. No newlines or control chars.
            if not re.match(r'^[a-zA-Z0-9\s*/:,\-]+$', cal_value):
                return jsonify({'error': 'Invalid interval format (allowed: a-z 0-9 * / : - , spaces)'}), 400

            # Rewrite timer file (needs root — use sudo tee)
            timer_content = (
                '[Unit]\n'
                'Description=Periodic RAG index update for EthOS\n'
                '\n'
                '[Timer]\n'
                f'OnCalendar={cal_value}\n'
                'Persistent=true\n'
                '\n'
                '[Install]\n'
                'WantedBy=timers.target\n'
            )
            import tempfile
            tmp = tempfile.NamedTemporaryFile('w', suffix='.timer', delete=False)
            tmp.write(timer_content)
            tmp.close()
            subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'copy-timer', tmp.name],
                           capture_output=True, timeout=10)
            os.unlink(tmp.name)

            subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', 'daemon-reload'], capture_output=True, timeout=10)
            subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', 'restart', _TIMER_UNIT], capture_output=True, timeout=10)
            return jsonify({'status': 'ok', 'interval': cal_value})

        else:
            return jsonify({'error': 'Unknown action'}), 400

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#  Routes: Model Library
# ══════════════════════════════════════════════════════════════════

@aichat_bp.route('/models/catalog', methods=['GET'])
def models_catalog():
    """Return model catalog with hardware recommendations."""
    hw = _get_hw()
    lib = _get_ml()
    recs = lib.get_recommendations(hw)
    disk = lib.get_disk_space()
    return jsonify({
        'models': recs,
        'hardware': hw,
        'disk': disk,
        'models_path': lib.models_path,
        'download_status': lib.get_download_status(),
    })

@aichat_bp.route('/models/hardware', methods=['GET'])
def models_hardware():
    """Return hardware info for model fitting."""
    hw = _get_hw()
    lib = _get_ml()
    disk = lib.get_disk_space()
    return jsonify({'hardware': hw, 'disk': disk, 'models_path': lib.models_path})

@aichat_bp.route('/models/download', methods=['POST'])
def models_download():
    """Start downloading a model."""
    if not _is_admin():
        return jsonify({'error': 'Only admin can download models'}), 403
    body = request.get_json(silent=True) or {}
    model_id = body.get('model_id', '').strip()
    if not model_id:
        return jsonify({'error': 'model_id required'}), 400

    # Fresh installs often miss huggingface_hub required for model downloads.
    if not _check_hf_hub():
        ok_hf, err_hf = _install_py_pkg('huggingface_hub', timeout=300)
        if not ok_hf:
            return jsonify({'error': f'huggingface_hub library not found and installation failed: {err_hf[-300:]}' }), 500
        if not _check_hf_hub():
            return jsonify({'error': 'huggingface_hub library still unavailable after installation.'}), 500

    lib = _get_ml()
    # Pass socketio if available
    sio = None
    try:
        from flask import current_app
        sio = current_app.extensions.get('socketio')
    except Exception:
        pass

    ok, err = lib.start_download(model_id, socketio=sio)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'status': 'ok', 'model_id': model_id})

@aichat_bp.route('/models/download/status', methods=['GET'])
def models_download_status():
    """Poll download progress."""
    lib = _get_ml()
    return jsonify(lib.get_download_status())

@aichat_bp.route('/models/download/cancel', methods=['POST'])
def models_download_cancel():
    """Cancel active download."""
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    lib = _get_ml()
    lib.cancel_download()
    return jsonify({'ok': True})

@aichat_bp.route('/models/<model_id>', methods=['DELETE'])
def models_delete(model_id):
    """Delete a downloaded model."""
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    lib = _get_ml()
    ok, err = lib.delete_model(model_id)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'ok': True})

@aichat_bp.route('/models/active', methods=['GET', 'POST'])
def models_active():
    """Get or set active model."""
    lib = _get_ml()
    if request.method == 'GET':
        m = lib.get_active_model()
        return jsonify({'model': m})

    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    body = request.get_json(silent=True) or {}
    model_id = body.get('model_id')  # None = deactivate
    ok, err = lib.set_active_model(model_id)
    if not ok:
        return jsonify({'error': err}), 400

    # Auto-switch provider to local when activating a model
    username = _get_username()
    cfg = _load_config(username)
    if model_id is not None:
        cfg['provider'] = 'local'
    elif cfg.get('provider') == 'local':
        # Deactivating — switch back to openai if was local
        cfg['provider'] = 'openai'
    _save_config(cfg, username)

    return jsonify({'ok': True, 'provider': cfg['provider']})

@aichat_bp.route('/models/unload', methods=['POST'])
def models_unload():
    """Unload model from RAM to free memory."""
    lib = _get_ml()
    loaded, mid = lib.get_loaded_model()
    if loaded is None:
        return jsonify({'status': 'ok', 'loaded': False})
    lib.unload_model()
    return jsonify({'status': 'ok', 'model_id': mid})

@aichat_bp.route('/models/path', methods=['GET', 'POST'])
def models_path():
    """Get or set models storage path."""
    lib = _get_ml()
    if request.method == 'GET':
        return jsonify({'path': lib.models_path, 'disk': lib.get_disk_space()})

    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    body = request.get_json(silent=True) or {}
    new_path = body.get('path', '').strip()
    if not new_path:
        return jsonify({'error': 'Path required'}), 400
    ok, err = lib.set_models_path(new_path)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'ok': True, 'path': lib.models_path, 'disk': lib.get_disk_space()})

@aichat_bp.route('/models/custom', methods=['POST'])
def models_custom_add():
    """Add a custom model by HF URL."""
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    body = request.get_json(silent=True) or {}
    url = body.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    lib = _get_ml()
    entry, err = lib.add_custom_model(url)
    if not entry:
        return jsonify({'error': err}), 400
    return jsonify({'ok': True, 'model': entry})

@aichat_bp.route('/models/custom/<model_id>', methods=['DELETE'])
def models_custom_remove(model_id):
    """Remove a custom model."""
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    lib = _get_ml()
    ok, err = lib.remove_custom_model(model_id)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════
#  Routes: Hardware discovery, benchmark, health, calibration
# ══════════════════════════════════════════════════════════════════

@aichat_bp.route('/hardware', methods=['GET'])
def full_hardware():
    """Full hardware discovery: CPU features, GPU, NPU, RAM, disk."""
    hw = _get_full_hw()
    lib = _get_ml()
    disk = lib.get_disk_space()
    tier = _get_tier_hw(hw)
    return jsonify({
        'hardware': hw,
        'disk': disk,
        'tier': tier,
    })


@aichat_bp.route('/models/benchmark', methods=['POST'])
def models_benchmark():
    """Run inference benchmark on active model. Returns TPS, TTFT, tier."""
    if not _is_admin():
        return jsonify({'error': 'Only admin can run benchmark'}), 403
    body = request.get_json(silent=True) or {}
    model_id = body.get('model_id')
    prompt = body.get('prompt')

    lib = _get_ml()
    active = lib.get_active_model()
    if not active and not model_id:
        return jsonify({'error': 'No active model — download and activate a model in the Model Library'}), 400

    result = lib.run_benchmark(model_id=model_id, prompt=prompt)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@aichat_bp.route('/models/benchmark/auto', methods=['POST'])
def models_benchmark_auto():
    """Auto-benchmark: ensure smallest model is available, benchmark it, return TPS scaling for all models."""
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403

    lib = _get_ml()
    hw = _get_hw()
    recs = lib.get_recommendations(hw)

    # Find smallest downloaded recommended model, or the smallest recommended overall
    downloaded_rec = [m for m in recs if m.get('downloaded') and m['status'] in ('recommended', 'possible')]
    downloaded_rec.sort(key=lambda m: m.get('size_gb', 99))

    if downloaded_rec:
        bench_model = downloaded_rec[0]
    else:
        # Need to download the smallest recommended model first
        all_rec = [m for m in recs if m['status'] in ('recommended', 'possible')]
        all_rec.sort(key=lambda m: m.get('size_gb', 99))
        if not all_rec:
            return jsonify({'error': 'No models matching hardware'}), 400
        bench_model = all_rec[0]

        # Trigger synchronous download of the smallest model
        if not _check_hf_hub():
            ok_hf, err_hf = _install_py_pkg('huggingface_hub', timeout=300)
            if not ok_hf:
                return jsonify({'error': f'huggingface_hub not found: {err_hf[-200:]}'}), 500

        ok, err = lib.download_sync(bench_model['id'])
        if not ok:
            return jsonify({'error': f'Failed to download test model: {err}'}), 500

    # Activate and benchmark
    lib.set_active_model(bench_model['id'])
    result = lib.run_benchmark(model_id=bench_model['id'])
    if 'error' in result:
        return jsonify(result), 400

    # Calculate TPS scaling for all models based on benchmark of reference model
    ref_params_b = _parse_params(bench_model.get('params', '0'))
    ref_tps = result.get('tps', 1)
    model_estimates = {}
    for m in recs:
        m_params_b = _parse_params(m.get('params', '0'))
        if ref_params_b > 0 and m_params_b > 0:
            # TPS scales roughly inversely with parameter count
            estimated_tps = round(ref_tps * (ref_params_b / m_params_b), 1)
        else:
            estimated_tps = 0
        model_estimates[m['id']] = estimated_tps

    result['ref_model_id'] = bench_model['id']
    result['ref_model_name'] = bench_model.get('name', bench_model['id'])
    result['ref_params'] = bench_model.get('params', '?')
    result['model_estimates'] = model_estimates
    return jsonify(result)


def _parse_params(params_str):
    """Parse '7B' → 7.0, '0.5B' → 0.5, '1.5B' → 1.5"""
    try:
        s = str(params_str).upper().replace('B', '').strip()
        return float(s)
    except (ValueError, TypeError):
        return 0


@aichat_bp.route('/models/benchmark', methods=['GET'])
def models_benchmark_results():
    """Get last benchmark results."""
    lib = _get_ml()
    model_id = request.args.get('model_id')
    bench = lib.get_last_benchmark(model_id)
    if not bench:
        return jsonify({'benchmark': None})
    # Enrich with tier
    bench_copy = dict(bench)
    bench_copy['tier'] = _get_tier(bench.get('tps', 0))
    return jsonify({'benchmark': bench_copy})


@aichat_bp.route('/health', methods=['GET'])
def ai_health():
    """AI Health Score — overall system readiness."""
    lib = _get_ml()
    health = lib.get_health_score()
    return jsonify(health)


def _calibration_path(username='admin'):
    return data_path(f'aichat_calibration_{username}.json')


@aichat_bp.route('/calibration', methods=['GET'])
def get_calibration():
    """Get calibration state for current user."""
    username = _get_username()
    cal = _load_json(_calibration_path(username), None)
    if cal is None:
        return jsonify({'calibrated': False})
    return jsonify({**cal, 'calibrated': True})


@aichat_bp.route('/calibration', methods=['POST'])
def save_calibration():
    """Save calibration results after setup wizard."""
    username = _get_username()
    data = request.json or {}
    cal = {
        'calibrated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'tier_id': data.get('tier_id', 'balanced'),
        'model_id': data.get('model_id'),
        'benchmark': data.get('benchmark'),
        'hardware_snapshot': data.get('hardware'),
        'wizard_completed': True,
    }
    _save_json(_calibration_path(username), cal)
    return jsonify({'ok': True, 'calibration': cal})


# ══════════════════════════════════════════════════════════════════
#  Routes: install / uninstall / status
# ══════════════════════════════════════════════════════════════════

def _aichat_on_uninstall(wipe, wipe_models=False):
    """Clean up AI Chat: unload model, optionally remove configs and/or models."""
    # Unload any loaded model from RAM
    try:
        lib = _get_ml()
        lib.unload_model()
    except Exception:
        pass

    if wipe:
        import glob
        for pattern in ('aichat_config_*.json', 'aichat_history_*.json', 'model_library.json'):
            for f in glob.glob(os.path.join(data_path(), pattern)):
                try:
                    os.remove(f)
                except Exception:
                    pass

    if wipe or wipe_models:
        # Remove downloaded models
        try:
            lib = _get_ml()
            models_dir = lib.models_path
            if models_dir and os.path.isdir(models_dir):
                import shutil
                shutil.rmtree(models_dir, ignore_errors=True)
        except Exception:
            pass


def _check_llama_cpp():
    """Check if llama-cpp-python is importable."""
    try:
        import llama_cpp
        return True
    except ImportError:
        return False


def _check_hf_hub():
    """Check if huggingface_hub is importable."""
    try:
        import huggingface_hub  # noqa: F401
        return True
    except ImportError:
        return False


def _find_venv_pip():
    """Resolve pip path for the active EthOS runtime."""
    venv_pip = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'venv', 'bin', 'pip')
    if not os.path.isfile(venv_pip):
        venv_pip = os.path.join(os.environ.get('ETHOS_ROOT', '/opt/ethos'), 'venv', 'bin', 'pip')
    if not os.path.isfile(venv_pip):
        import shutil as _sh
        venv_pip = _sh.which('pip3') or _sh.which('pip') or 'pip'
    return venv_pip


def _install_py_pkg(pkg_name, timeout=900):
    """Install a Python package in the EthOS venv. Returns (ok, error_msg)."""
    import subprocess
    try:
        pip_bin = _find_venv_pip()
        proc = subprocess.run(
            [pip_bin, 'install', '--no-cache-dir', pkg_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            tail = '\n'.join((proc.stdout or '').splitlines()[-12:])
            return (False, tail or f'Package {pkg_name} installation failed')
        return (True, '')
    except Exception as ex:
        return (False, str(ex))


@aichat_bp.route('/install', methods=['POST'])
@admin_required
def aichat_install():
    """Install AI Chat dependencies (llama-cpp-python). Streams progress via SocketIO."""
    import threading
    _sio = getattr(aichat_bp, '_socketio', None)

    task_id = os.urandom(8).hex()

    def _emit(stage, pct, msg):
        if _sio:
            _sio.emit('aichat_install', {
                'task_id': task_id, 'stage': stage, 'percent': pct, 'message': msg
            })

    def _bg():
        try:
            _emit('start', 0, 'Checking dependencies…')

            if _check_llama_cpp():
                _emit('done', 100, 'AI Chat ready — dependencies already installed.')
                return

            # Find venv pip
            venv_pip = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'venv', 'bin', 'pip')
            if not os.path.isfile(venv_pip):
                # Try ETHOS_ROOT
                venv_pip = os.path.join(os.environ.get('ETHOS_ROOT', '/opt/ethos'), 'venv', 'bin', 'pip')
            if not os.path.isfile(venv_pip):
                import shutil as _sh
                venv_pip = _sh.which('pip3') or _sh.which('pip') or 'pip'

            _emit('progress', 10, 'Installing llama-cpp-python (C++ compilation — this may take a few minutes)…')

            import subprocess
            proc = subprocess.Popen(
                [venv_pip, 'install', '--no-cache-dir', 'llama-cpp-python'],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            lines = []
            progress = 10
            for line in iter(proc.stdout.readline, ''):
                line_s = line.rstrip()
                lines.append(line_s)
                if 'Collecting' in line_s:
                    progress = min(progress + 5, 30)
                    _emit('progress', progress, line_s[:150])
                elif 'Downloading' in line_s:
                    progress = min(progress + 3, 40)
                    _emit('progress', progress, line_s[:150])
                elif 'Building' in line_s:
                    progress = min(progress + 2, 80)
                    _emit('progress', progress, 'Compiling llama.cpp…')
                elif 'Installing' in line_s or 'Successfully' in line_s:
                    progress = min(progress + 5, 95)
                    _emit('progress', progress, line_s[:150])

            proc.wait()
            if proc.returncode != 0:
                err = '\n'.join(lines[-10:])
                _emit('error', 0, f'Installation failed:\n{err[-400:]}')
                return

            # Verify import
            try:
                import importlib
                importlib.invalidate_caches()
                import llama_cpp  # noqa: F401
            except ImportError:
                _emit('error', 0, 'Package installed but cannot be imported. Server restart may be required.')
                return

            _emit('done', 100, 'AI Chat with local model support ready!')

        except Exception as ex:
            _emit('error', 0, f'Error: {ex}')

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return jsonify({'status': 'ok', 'task_id': task_id})


@aichat_bp.route('/uninstall', methods=['POST'])
@admin_required
def aichat_uninstall():
    """Uninstall AI Chat: unload model, optionally wipe data."""
    body = request.json or {}
    wipe = body.get('wipe_data', False)
    wipe_models = body.get('wipe_models', False)
    _aichat_on_uninstall(wipe, wipe_models=wipe_models)
    return jsonify({'ok': True})


@aichat_bp.route('/status', methods=['GET'])
def status_aichat():
    username = _get_username()
    cfg = _load_config(username)
    lib = _get_ml()
    active = lib.get_active_model()
    is_local = cfg.get('provider') == 'local'
    llama_ok = _check_llama_cpp()
    hf_ok = _check_hf_hub()

    # RAM info
    import psutil
    mem = psutil.virtual_memory()
    loaded_llm, loaded_id = lib.get_loaded_model()

    # Health score
    health = lib.get_health_score()

    # Calibration
    cal = _load_json(_calibration_path(username), None)

    # RAG status
    try:
        sandbox = _user_sandbox_root()
        rag_indexer = _get_rag(username, sandbox)
        rag_stats = rag_indexer.get_status()
        rag_stats['scheduler'] = _scheduler_status()
    except Exception:
        rag_stats = {'stats': {}, 'indexing': False, 'scheduler': _scheduler_status()}

    result = {
        'installed': True,
        'configured': is_local or bool(cfg.get('api_key')),
        'provider': cfg.get('provider', 'openai'),
        'model': cfg.get('model', ''),
        'local_model': active.get('name', '') if active else None,
        'local_model_loaded': loaded_llm is not None,
        'deps': {
            'llama_cpp': llama_ok,
            'huggingface_hub': hf_ok,
        },
        'ram': {
            'total_gb': round(mem.total / (1024**3), 1),
            'available_gb': round(mem.available / (1024**3), 1),
            'percent_used': mem.percent,
        },
        'health': health,
        'calibrated': cal is not None and cal.get('wizard_completed', False),
        'calibration': cal,
        'rag': rag_stats,
        'rag_enabled': cfg.get('rag_enabled', True),
    }
    return jsonify(result)
