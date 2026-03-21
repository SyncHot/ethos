"""
EthOS — User & Group Management Blueprint
Host system user/group CRUD + app privilege management.
"""

import json
import os
import re
import subprocess
import shlex
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, data_path, get_data_disk, get_user_home, ensure_user_home_structure

users_bp = Blueprint('users', __name__, url_prefix='/api/users')

PRIVILEGES_FILE = data_path('privileges.json')

# Setup Auth Logger for Fail2Ban
AUTH_LOG_FILE = '/opt/ethos/logs/auth.log'
auth_logger = logging.getLogger('ethos_auth')
auth_logger.setLevel(logging.INFO)
if not auth_logger.handlers:
    try:
        os.makedirs(os.path.dirname(AUTH_LOG_FILE), exist_ok=True)
        # 10MB log file, keep 5 backups
        handler = RotatingFileHandler(AUTH_LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        auth_logger.addHandler(handler)
    except Exception as e:
        print(f"Failed to setup auth logger: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sq(s):
    return shlex.quote(s)


def host_run(cmd, timeout=30):
    return _host_run(cmd, timeout=timeout)


def _safe_name(name):
    """Allow only safe username/group characters."""
    return re.sub(r'[^a-zA-Z0-9_.\-]', '', name)


NASOS_GROUP = 'nasos'


def ensure_nasos_group():
    """Create the 'nasos' group on the host if it doesn't exist."""
    r = host_run(f"getent group {NASOS_GROUP}")
    if r.returncode != 0:
        host_run(f"groupadd {NASOS_GROUP}")


def _load_privileges():
    """Load group→apps privilege map."""
    if os.path.exists(PRIVILEGES_FILE):
        try:
            with open(PRIVILEGES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_privileges(data):
    os.makedirs(os.path.dirname(PRIVILEGES_FILE), exist_ok=True)
    with open(PRIVILEGES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

_users_cache = {'data': None, 'ts': 0}
_USERS_CACHE_TTL = 30  # seconds

@users_bp.route('/list')
def list_users():
    """List all system users (uid >= 1000 + root)."""
    now = time.time()
    if _users_cache['data'] is not None and (now - _users_cache['ts']) < _USERS_CACHE_TTL:
        return jsonify(_users_cache['data'])
    try:
        r = host_run(
            "getent passwd | awk -F: '($3 >= 1000 && $3 < 65534) || $3 == 0 "
            "{print $1\"\\t\"$3\"\\t\"$4\"\\t\"$6\"\\t\"$7}'"
        )
        users = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('\t')
            if len(parts) >= 5:
                username = parts[0]
                # Get groups for this user
                gr = host_run(f"id -Gn {_sq(username)} 2>/dev/null")
                groups = gr.stdout.strip().split() if gr.returncode == 0 else []
                users.append({
                    'username': username,
                    'uid': int(parts[1]),
                    'gid': int(parts[2]),
                    'home': parts[3],
                    'shell': parts[4],
                    'groups': groups,
                    'nasos_user': NASOS_GROUP in groups,
                })
        _users_cache['data'] = users
        _users_cache['ts'] = time.time()
        return jsonify(users)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@users_bp.route('/create', methods=['POST'])
def create_user():
    """Create a new system user.

    If a data disk is configured, the user's home directory is placed
    on the data disk at ``{data_disk}/home/{username}`` so their files
    live on the chosen storage device.  Default folders (Dokumenty,
    Pobrane, …) and the ``~/.ethos`` config directory are also created.
    """
    data = request.json or {}
    username = _safe_name(data.get('username', ''))
    password = data.get('password', '')
    shell = data.get('shell', '/bin/bash')
    groups = data.get('groups', [])

    if not username or len(username) < 2:
        return jsonify({'error': 'Nazwa użytkownika jest wymagana (min. 2 znaki)'}), 400
    if not password or len(password) < 4:
        return jsonify({'error': 'Hasło jest wymagane (min. 4 znaki)'}), 400

    # Check if exists
    r = host_run(f"id {_sq(username)} 2>/dev/null")
    if r.returncode == 0:
        return jsonify({'error': f'Użytkownik „{username}" już istnieje'}), 400

    # Ensure nasos group exists
    ensure_nasos_group()

    # Determine home directory — prefer data disk
    dd = get_data_disk()
    if dd:
        home_base = os.path.join(dd, 'home')
        home_dir = os.path.join(home_base, username)
        os.makedirs(home_base, mode=0o755, exist_ok=True)
        cmd = f"useradd -m -d {_sq(home_dir)} -s {_sq(shell)} {_sq(username)}"
    else:
        cmd = f"useradd -m -s {_sq(shell)} {_sq(username)}"

    r = host_run(cmd)
    if r.returncode != 0:
        return jsonify({'error': f'Błąd tworzenia: {r.stderr.strip()}'}), 500

    # Set password
    r = host_run(f"echo {_sq(username + ':' + password)} | chpasswd")
    if r.returncode != 0:
        return jsonify({'error': f'Błąd ustawiania hasła: {r.stderr.strip()}'}), 500

    # Add to nasos group (mark as EthOS-created user)
    host_run(f"usermod -aG {_sq(NASOS_GROUP)} {_sq(username)}")

    # Add to additional groups
    for g in groups:
        g = _safe_name(g)
        if g:
            host_run(f"usermod -aG {_sq(g)} {_sq(username)}")

    # Create default folder structure (Dokumenty, Pobrane, …) + ~/.ethos
    ensure_user_home_structure(username)

    _users_cache['data'] = None  # invalidate cache
    return jsonify({'success': True, 'username': username})


@users_bp.route('/delete', methods=['POST'])
def delete_user():
    """Delete a system user."""
    data = request.json or {}
    username = _safe_name(data.get('username', ''))
    if not username:
        return jsonify({'error': 'Nazwa użytkownika jest wymagana'}), 400
    if username == 'root':
        return jsonify({'error': 'Nie można usunąć root'}), 400

    r = host_run(f"userdel -r {_sq(username)} 2>&1")
    if r.returncode != 0:
        return jsonify({'error': f'Błąd: {r.stdout.strip() or r.stderr.strip()}'}), 500

    _users_cache['data'] = None  # invalidate cache
    return jsonify({'success': True})


@users_bp.route('/update', methods=['POST'])
def update_user():
    """Update user properties (password, shell, groups)."""
    data = request.json or {}
    username = _safe_name(data.get('username', ''))
    if not username:
        return jsonify({'error': 'Nazwa użytkownika jest wymagana'}), 400

    errors = []

    # Change password
    password = data.get('password')
    if password:
        r = host_run(f"echo {_sq(username + ':' + password)} | chpasswd")
        if r.returncode != 0:
            errors.append(f'Hasło: {r.stderr.strip()}')

    # Change shell
    shell = data.get('shell')
    if shell:
        r = host_run(f"usermod -s {_sq(shell)} {_sq(username)}")
        if r.returncode != 0:
            errors.append(f'Shell: {r.stderr.strip()}')

    # Update groups (set exact group list)
    groups = data.get('groups')
    if groups is not None:
        safe_groups = [_safe_name(g) for g in groups if _safe_name(g)]
        if safe_groups:
            r = host_run(f"usermod -G {','.join(_sq(g) for g in safe_groups)} {_sq(username)}")
            if r.returncode != 0:
                errors.append(f'Grupy: {r.stderr.strip()}')

    if errors:
        return jsonify({'error': '; '.join(errors)}), 500

    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@users_bp.route('/groups')
def list_groups():
    """List system groups (gid >= 1000 + some system groups)."""
    try:
        # Get all groups
        r = host_run("getent group | sort -t: -k3 -n")
        groups = []
        privileges = _load_privileges()

        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split(':')
            if len(parts) >= 4:
                gname = parts[0]
                gid = int(parts[2])
                members = [m for m in parts[3].split(',') if m]
                # Show groups with gid >= 1000, or recognized system groups, or groups that have privileges
                if gid >= 1000 or gname in privileges or gname in ('sudo', 'docker', 'adm', 'www-data', 'samba', NASOS_GROUP):
                    groups.append({
                        'name': gname,
                        'gid': gid,
                        'members': members,
                        'app_privileges': privileges.get(gname, []),
                    })
        return jsonify(groups)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@users_bp.route('/groups/create', methods=['POST'])
def create_group():
    """Create a new group."""
    data = request.json or {}
    name = _safe_name(data.get('name', ''))
    if not name or len(name) < 2:
        return jsonify({'error': 'Nazwa grupy jest wymagana (min. 2 znaki)'}), 400

    r = host_run(f"groupadd {_sq(name)} 2>&1")
    if r.returncode != 0:
        return jsonify({'error': r.stdout.strip() or r.stderr.strip() or 'Błąd tworzenia grupy'}), 500

    # Set initial privileges if provided
    app_privileges = data.get('app_privileges', [])
    if app_privileges:
        privileges = _load_privileges()
        privileges[name] = app_privileges
        _save_privileges(privileges)

    return jsonify({'success': True, 'name': name})


@users_bp.route('/groups/delete', methods=['POST'])
def delete_group():
    """Delete a group."""
    data = request.json or {}
    name = _safe_name(data.get('name', ''))
    if not name:
        return jsonify({'error': 'Nazwa grupy jest wymagana'}), 400

    r = host_run(f"groupdel {_sq(name)} 2>&1")
    if r.returncode != 0:
        return jsonify({'error': r.stdout.strip() or r.stderr.strip() or 'Błąd'}), 500

    # Remove privileges
    privileges = _load_privileges()
    privileges.pop(name, None)
    _save_privileges(privileges)

    return jsonify({'success': True})


@users_bp.route('/groups/members', methods=['POST'])
def update_group_members():
    """Add or remove members from a group.
    Accepts two formats:
      - { group, add: [users], remove: [users] }   (batch)
      - { group, username, action: 'add'|'remove' } (single)
    """
    data = request.json or {}
    group = _safe_name(data.get('group', ''))

    if not group:
        return jsonify({'error': 'Grupa jest wymagana'}), 400

    # Support single-user format from frontend
    add_list = data.get('add', [])
    remove_list = data.get('remove', [])
    single_user = data.get('username', '')
    action = data.get('action', '')
    if single_user and action:
        if action == 'add':
            add_list = [single_user]
        elif action == 'remove':
            remove_list = [single_user]

    errors = []
    for u in add_list:
        u = _safe_name(u)
        if u:
            r = host_run(f"usermod -aG {_sq(group)} {_sq(u)}")
            if r.returncode != 0:
                errors.append(f'Dodawanie {u}: {r.stderr.strip()}')

    for u in remove_list:
        u = _safe_name(u)
        if u:
            r = host_run(f"gpasswd -d {_sq(u)} {_sq(group)} 2>&1")
            if r.returncode != 0:
                errors.append(f'Usuwanie {u}: {r.stdout.strip()}')

    if errors:
        return jsonify({'error': '; '.join(errors)}), 500
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# App Privileges
# ---------------------------------------------------------------------------

@users_bp.route('/privileges')
def get_privileges():
    """Get all group→app privilege mappings."""
    return jsonify(_load_privileges())


@users_bp.route('/privileges', methods=['POST'])
def set_privileges():
    """Set app privileges.
    Accepts two formats:
      - { group, apps: [...] }                     (single group)
      - { "group1": ["app1", ...], "group2": ... }  (full map)
    """
    data = request.json or {}
    group = data.get('group', '')
    apps = data.get('apps', [])

    if group:
        # Single-group format
        privileges = _load_privileges()
        if apps:
            privileges[group] = apps
        else:
            privileges.pop(group, None)
        _save_privileges(privileges)
    else:
        # Full map format — { groupName: [appIds], ... }
        # Validate: all keys should be strings, all values should be lists
        new_privileges = {}
        for k, v in data.items():
            if isinstance(v, list):
                new_privileges[k] = v
        _save_privileges(new_privileges)

    return jsonify({'success': True})


@users_bp.route('/privileges/for-user/<username>')
def get_user_privileges(username):
    """Get the list of allowed app IDs for a specific user.
    Returns all apps if user is admin or has no group restrictions."""
    username = _safe_name(username)
    if not username or username == 'admin':
        return jsonify({'all': True, 'apps': []})

    # Get user's groups
    r = host_run(f"id -Gn {_sq(username)} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({'all': True, 'apps': []})

    user_groups = r.stdout.strip().split()
    privileges = _load_privileges()

    # If user is in sudo/root group → all access
    if 'sudo' in user_groups or 'root' in user_groups:
        return jsonify({'all': True, 'apps': []})

    # Collect all allowed apps from user's groups
    allowed = set()
    has_restrictions = False
    for g in user_groups:
        if g in privileges:
            has_restrictions = True
            allowed.update(privileges[g])

    if not has_restrictions:
        # No groups have privilege config → full access
        return jsonify({'all': True, 'apps': []})

    return jsonify({'all': False, 'apps': list(allowed)})


# ---------------------------------------------------------------------------
# Auth helper — validate user credentials on host via PAM/su
# ---------------------------------------------------------------------------

@users_bp.route('/auth/validate', methods=['POST'])
def validate_user():
    """Validate username + password against host system (via su -c)."""
    data = request.json or {}
    username = data.get('username', '')
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'valid': False, 'error': 'Wymagane login i hasło'}), 400

    safe_user = _safe_name(username)
    # Use su to validate credentials
    r = host_run(
        f"echo {_sq(password)} | su -c 'echo OK' {_sq(safe_user)} 2>/dev/null",
        timeout=10
    )

    if r.returncode == 0 and 'OK' in r.stdout:
        return jsonify({'valid': True, 'username': safe_user})
    else:
        # Log failure for fail2ban
        try:
            ip = request.remote_addr
            if auth_logger.handlers:
                auth_logger.warning(f'Failed login attempt for user {safe_user} from {ip}')
        except Exception:
            pass
        return jsonify({'valid': False, 'error': 'Nieprawidłowy login lub hasło'}), 401
