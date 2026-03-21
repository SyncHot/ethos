"""
EthOS — Per-App Sandboxing Policies & Default Resource Limits

Stores and serves container resource constraints (CPU, memory, PIDs, privileges)
per compose project. Defaults apply when no per-app override exists.

Endpoints (all require admin):
  GET  /api/sandbox/defaults               — get global default limits
  PUT  /api/sandbox/defaults               — update global default limits
  GET  /api/sandbox/apps                   — list all per-app overrides
  GET  /api/sandbox/apps/<name>            — get override for one app
  PUT  /api/sandbox/apps/<name>            — create/update override for one app
  DELETE /api/sandbox/apps/<name>          — remove override (falls back to defaults)
  GET  /api/sandbox/apps/<name>/effective  — merged effective policy (defaults + override)
"""

import os
import json
import re
import sys
from functools import wraps
from flask import Blueprint, request, jsonify, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path

sandbox_bp = Blueprint('sandbox', __name__, url_prefix='/api/sandbox')

_POLICY_FILE = data_path('sandbox_policies.json')

# ─── Default container resource limits ───────────────────────────────────────
# These represent sensible safe defaults for untrusted/unknown compose apps.
# Values follow Docker resource constraint semantics.
DEFAULT_LIMITS = {
    'mem_limit': '512m',        # Max RAM (Docker memory string: 128m, 1g, …)
    'mem_reservation': '128m',  # Soft memory limit / reservation
    'cpu_quota': 50.0,          # % of a single CPU core (0 = unlimited)
    'cpu_shares': 1024,         # CPU shares (relative weight, default 1024)
    'pids_limit': 200,          # Max processes/threads inside container (0 = unlimited)
    'no_new_privileges': True,  # Prevent privilege escalation via setuid/setgid
    'read_only_root': False,    # Mount root filesystem read-only
    'cap_drop': ['NET_ADMIN', 'SYS_ADMIN', 'SYS_PTRACE'],  # Dropped Linux capabilities
    'cap_add': [],              # Explicitly granted capabilities (beyond defaults)
}

_APP_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')
_MEM_RE = re.compile(r'^\d+[bkmgBKMG]?$')


# ─── Storage helpers ──────────────────────────────────────────────────────────

def _load_policies():
    """Load policy store from disk; return dict with 'defaults' and 'apps'."""
    if os.path.exists(_POLICY_FILE):
        try:
            with open(_POLICY_FILE, 'r') as f:
                data = json.load(f)
            # Ensure both keys exist
            if 'defaults' not in data:
                data['defaults'] = {}
            if 'apps' not in data:
                data['apps'] = {}
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {'defaults': {}, 'apps': {}}


def _save_policies(data):
    os.makedirs(os.path.dirname(_POLICY_FILE), exist_ok=True)
    with open(_POLICY_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def _effective_defaults(stored_defaults):
    """Merge stored overrides onto the built-in DEFAULT_LIMITS."""
    result = dict(DEFAULT_LIMITS)
    result.update(stored_defaults)
    return result


def get_effective_policy(app_name):
    """Return the effective merged policy for a given app (public API).

    Callers (e.g. docker_manager) can import this to obtain the constraints
    that should be applied when starting a container belonging to *app_name*.
    """
    store = _load_policies()
    policy = _effective_defaults(store.get('defaults', {}))
    policy.update(store.get('apps', {}).get(app_name, {}))
    return policy


# ─── Input validation ─────────────────────────────────────────────────────────

def _validate_limits(data):
    """Validate a partial or full limits dict. Returns (cleaned, error_str)."""
    allowed_keys = {
        'mem_limit', 'mem_reservation', 'cpu_quota', 'cpu_shares', 'pids_limit',
        'no_new_privileges', 'read_only_root', 'cap_drop', 'cap_add',
    }
    unknown = set(data.keys()) - allowed_keys
    if unknown:
        return None, f'Nieznane pola: {", ".join(sorted(unknown))}'

    cleaned = {}

    if 'mem_limit' in data:
        v = str(data['mem_limit']).strip().lower()
        if v != '0' and not _MEM_RE.match(v):
            return None, 'mem_limit: nieprawidłowy format (np. 512m, 2g, 0)'
        cleaned['mem_limit'] = v

    if 'mem_reservation' in data:
        v = str(data['mem_reservation']).strip().lower()
        if v != '0' and not _MEM_RE.match(v):
            return None, 'mem_reservation: nieprawidłowy format (np. 128m, 1g, 0)'
        cleaned['mem_reservation'] = v

    if 'cpu_quota' in data:
        try:
            v = float(data['cpu_quota'])
            if v < 0 or v > 1000:
                raise ValueError
        except (TypeError, ValueError):
            return None, 'cpu_quota: oczekiwana liczba 0–1000 (% CPU, 0 = brak limitu)'
        cleaned['cpu_quota'] = v

    if 'cpu_shares' in data:
        try:
            v = int(data['cpu_shares'])
            if v < 2:  # Docker minimum is 2
                raise ValueError
        except (TypeError, ValueError):
            return None, 'cpu_shares: oczekiwana liczba całkowita >= 2 (domyślnie 1024)'
        cleaned['cpu_shares'] = v

    if 'pids_limit' in data:
        try:
            v = int(data['pids_limit'])
            if v < 0:
                raise ValueError
        except (TypeError, ValueError):
            return None, 'pids_limit: oczekiwana nieujemna liczba całkowita (0 = brak limitu)'
        cleaned['pids_limit'] = v

    if 'no_new_privileges' in data:
        if not isinstance(data['no_new_privileges'], bool):
            return None, 'no_new_privileges: oczekiwana wartość boolowska'
        cleaned['no_new_privileges'] = data['no_new_privileges']

    if 'read_only_root' in data:
        if not isinstance(data['read_only_root'], bool):
            return None, 'read_only_root: oczekiwana wartość boolowska'
        cleaned['read_only_root'] = data['read_only_root']

    if 'cap_drop' in data:
        if not isinstance(data['cap_drop'], list):
            return None, 'cap_drop: oczekiwana lista stringów'
        cleaned['cap_drop'] = [str(c).upper() for c in data['cap_drop']]

    if 'cap_add' in data:
        if not isinstance(data['cap_add'], list):
            return None, 'cap_add: oczekiwana lista stringów'
        cleaned['cap_add'] = [str(c).upper() for c in data['cap_add']]

    return cleaned, None


# ─── Auth decorator ───────────────────────────────────────────────────────────

def _require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if getattr(g, 'role', None) != 'admin':
            return jsonify({'error': 'Brak uprawnień — wymagana rola administratora'}), 403
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════
#  DEFAULT LIMITS
# ═══════════════════════════════════════════════════════════

@sandbox_bp.route('/defaults', methods=['GET'])
@_require_admin
def get_defaults():
    """Return the currently active default resource limits."""
    store = _load_policies()
    return jsonify(_effective_defaults(store.get('defaults', {})))


@sandbox_bp.route('/defaults', methods=['PUT'])
@_require_admin
def update_defaults():
    """Patch the global default resource limits (partial update)."""
    body = request.get_json(force=True, silent=True) or {}
    cleaned, err = _validate_limits(body)
    if err:
        return jsonify({'error': err}), 400

    store = _load_policies()
    store['defaults'].update(cleaned)
    _save_policies(store)
    return jsonify({'ok': True, 'defaults': _effective_defaults(store['defaults'])})


# ═══════════════════════════════════════════════════════════
#  PER-APP OVERRIDES
# ═══════════════════════════════════════════════════════════

@sandbox_bp.route('/apps', methods=['GET'])
@_require_admin
def list_apps():
    """List all per-app policy overrides."""
    store = _load_policies()
    return jsonify(store.get('apps', {}))


@sandbox_bp.route('/apps/<app_name>', methods=['GET'])
@_require_admin
def get_app_policy(app_name):
    """Get the stored override for *app_name* (does not include defaults)."""
    if not _APP_NAME_RE.match(app_name):
        return jsonify({'error': 'Nieprawidłowa nazwa aplikacji'}), 400

    store = _load_policies()
    override = store.get('apps', {}).get(app_name)
    if override is None:
        return jsonify({'error': f'Brak polityki dla aplikacji "{app_name}"'}), 404
    return jsonify(override)


@sandbox_bp.route('/apps/<app_name>', methods=['PUT'])
@_require_admin
def set_app_policy(app_name):
    """Create or update a per-app resource policy (partial update supported)."""
    if not _APP_NAME_RE.match(app_name):
        return jsonify({'error': 'Nieprawidłowa nazwa aplikacji'}), 400

    body = request.get_json(force=True, silent=True) or {}
    cleaned, err = _validate_limits(body)
    if err:
        return jsonify({'error': err}), 400

    store = _load_policies()
    existing = store.setdefault('apps', {}).get(app_name, {})
    existing.update(cleaned)
    store['apps'][app_name] = existing
    _save_policies(store)
    return jsonify({'ok': True, 'app': app_name, 'policy': existing})


@sandbox_bp.route('/apps/<app_name>', methods=['DELETE'])
@_require_admin
def delete_app_policy(app_name):
    """Remove the per-app override so the app falls back to defaults."""
    if not _APP_NAME_RE.match(app_name):
        return jsonify({'error': 'Nieprawidłowa nazwa aplikacji'}), 400

    store = _load_policies()
    if app_name not in store.get('apps', {}):
        return jsonify({'error': f'Brak polityki dla aplikacji "{app_name}"'}), 404

    del store['apps'][app_name]
    _save_policies(store)
    return jsonify({'ok': True, 'message': f'Polityka dla "{app_name}" usunięta'})


@sandbox_bp.route('/apps/<app_name>/effective', methods=['GET'])
@_require_admin
def get_app_effective(app_name):
    """Return the fully resolved policy for *app_name* (defaults + override)."""
    if not _APP_NAME_RE.match(app_name):
        return jsonify({'error': 'Nieprawidłowa nazwa aplikacji'}), 400

    return jsonify(get_effective_policy(app_name))
