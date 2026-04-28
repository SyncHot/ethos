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
from collections import defaultdict
from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, data_path, get_data_disk, get_user_home, ensure_user_home_structure
from audit import audit_log

users_bp = Blueprint('users', __name__, url_prefix='/api/users')


# ─── Endpoint rate limiter (in-memory, per-IP) ───
class _EndpointRateLimiter:
    def __init__(self):
        self._attempts = defaultdict(list)

    def is_limited(self, key, max_attempts=10, window_secs=300):
        now = time.time()
        self._attempts[key] = [t for t in self._attempts[key] if now - t < window_secs]
        if len(self._attempts[key]) >= max_attempts:
            return True
        self._attempts[key].append(now)
        return False

    def reset(self, key):
        self._attempts.pop(key, None)

_rate_limiter = _EndpointRateLimiter()


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


NASOS_GROUP = 'ethos-user'
NASOS_FAMILY_GROUP = 'ethos-family'
ETHOS_ADMIN_GROUP = 'ethos-admin'

# Groups managed by EthOS — always shown in UI
_ETHOS_GROUPS = {ETHOS_ADMIN_GROUP, NASOS_GROUP, NASOS_FAMILY_GROUP}

# System groups hidden from UI (handled internally)
_HIDDEN_GROUPS = {
    'sudo', 'root', 'adm', 'www-data', 'samba', 'docker',
    'plugdev', 'netdev', 'audio', 'video', 'dialout', 'cdrom',
    'floppy', 'tape', 'input', 'kvm', 'render', 'sgx',
    'lp', 'tty', 'disk', 'kmem', 'staff', 'games', 'users',
    'nogroup', 'systemd-journal', 'systemd-network', 'systemd-resolve',
    'systemd-timesync', 'messagebus', 'syslog', 'crontab',
    'ssh', 'ssl-cert', 'utmp', 'shadow', 'man', 'mail',
    'daemon', 'bin', 'sys', 'operator', 'backup', 'sasl',
    'gnats', 'irc', 'list', 'src', 'proxy', 'lxd',
    'bluetooth', 'colord', 'scanner', 'avahi', 'ollama',
    # Legacy EthOS groups (pre-rename)
    'nasosadmin', 'nasos', 'nasos-family',
}

# Valid EthOS roles for user creation
_VALID_ROLES = {'admin', 'user', 'family'}


def ensure_nasos_group():
    """Create the 'ethos-admin', 'ethos-user' and 'ethos-family' groups on the host if they don't exist."""
    for grp in (ETHOS_ADMIN_GROUP, NASOS_GROUP, NASOS_FAMILY_GROUP):
        r = host_run(f"getent group {grp}")
        if r.returncode != 0:
            host_run(f"sudo {_HELPER} group-add {grp}")


def _detect_user_role(groups):
    """Detect EthOS role from system groups: admin, user, or family."""
    if 'sudo' in groups or 'root' in groups or ETHOS_ADMIN_GROUP in groups:
        return 'admin'
    if NASOS_FAMILY_GROUP in groups:
        return 'family'
    return 'user'


def _visible_groups(groups):
    """Filter group list to only EthOS-managed and user-created groups."""
    return [g for g in groups if g not in _HIDDEN_GROUPS]


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
# Password Policy
# ---------------------------------------------------------------------------

_DEFAULT_PASSWORD_POLICY = {
    'min_length': 8,
    'require_uppercase': True,
    'require_lowercase': True,
    'require_digit': True,
    'require_special': False,
    'max_length': 128,
    'blacklist': ['ethos', 'password', '12345678', 'qwerty123'],
}
_PASSWORD_POLICY_FILE = data_path('password_policy.json')


def _load_password_policy():
    """Load password policy from file, or return defaults."""
    if os.path.exists(_PASSWORD_POLICY_FILE):
        try:
            with open(_PASSWORD_POLICY_FILE) as f:
                saved = json.load(f)
            policy = dict(_DEFAULT_PASSWORD_POLICY)
            policy.update(saved)
            return policy
        except Exception:
            pass
    return dict(_DEFAULT_PASSWORD_POLICY)


def _save_password_policy(policy):
    os.makedirs(os.path.dirname(_PASSWORD_POLICY_FILE), exist_ok=True)
    with open(_PASSWORD_POLICY_FILE, 'w') as f:
        json.dump(policy, f, indent=2)


def validate_password_strength(password, username=None):
    """Validate password against the configured policy.

    Returns (ok: bool, errors: list[str]).
    """
    policy = _load_password_policy()
    errors = []

    if not password:
        return False, ['Password is required']

    if len(password) < policy.get('min_length', 8):
        errors.append(f'Minimum {policy["min_length"]} characters required')

    if len(password) > policy.get('max_length', 128):
        errors.append(f'Maximum {policy["max_length"]} characters allowed')

    if policy.get('require_uppercase') and not re.search(r'[A-Z]', password):
        errors.append('At least one uppercase letter required')

    if policy.get('require_lowercase') and not re.search(r'[a-z]', password):
        errors.append('At least one lowercase letter required')

    if policy.get('require_digit') and not re.search(r'[0-9]', password):
        errors.append('At least one digit required')

    if policy.get('require_special') and not re.search(r'[^A-Za-z0-9]', password):
        errors.append('At least one special character required')

    blacklist = [w.lower() for w in policy.get('blacklist', [])]
    if password.lower() in blacklist:
        errors.append('This password is not allowed (blacklisted)')

    if username and password.lower() == username.lower():
        errors.append('Password cannot be the same as username')

    return len(errors) == 0, errors


@users_bp.route('/password-policy', methods=['GET'])
def get_password_policy():
    """Return the current password policy."""
    policy = _load_password_policy()
    return jsonify({'ok': True, 'policy': policy})


@users_bp.route('/password-policy', methods=['PUT'])
def update_password_policy():
    """Update password policy settings."""
    data = request.json or {}
    policy = _load_password_policy()

    allowed_keys = {'min_length', 'require_uppercase', 'require_lowercase',
                    'require_digit', 'require_special', 'max_length', 'blacklist'}
    for key in allowed_keys:
        if key in data:
            policy[key] = data[key]

    # Sanity checks
    if policy.get('min_length', 8) < 4:
        policy['min_length'] = 4
    if policy.get('max_length', 128) < policy['min_length']:
        policy['max_length'] = policy['min_length'] + 10

    _save_password_policy(policy)
    return jsonify({'ok': True, 'policy': policy})


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

_users_cache = {'data': None, 'ts': 0}
_USERS_CACHE_TTL = 30  # seconds

_HELPER = '/opt/ethos/tools/ethos-system-helper.sh'

@users_bp.route('/list')
def list_users():
    """List all system users (uid >= 1000, excluding system accounts)."""
    now = time.time()
    if _users_cache['data'] is not None and (now - _users_cache['ts']) < _USERS_CACHE_TTL:
        return jsonify(_users_cache['data'])
    try:
        r = host_run(
            "getent passwd | awk -F: '($3 >= 1000 && $3 < 65534) "
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
                    'groups': [g for g in _visible_groups(groups) if g != username],
                    'nasos_user': NASOS_GROUP in groups,
                    'role': _detect_user_role(groups),
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

    Accepts optional ``role``: 'admin' | 'user' | 'family' (default: 'user').
    """
    client_ip = request.remote_addr or '0.0.0.0'
    if _rate_limiter.is_limited(f'create_user:{client_ip}'):
        return jsonify({"error": "Too many attempts. Try again in 5 minutes."}), 429
    data = request.json or {}
    username = _safe_name(data.get('username', ''))
    password = data.get('password', '')
    shell = data.get('shell', '/bin/bash')
    groups = data.get('groups', [])
    role = data.get('role', 'user')

    if role not in _VALID_ROLES:
        role = 'user'

    if not username or len(username) < 2:
        return jsonify({'error': 'Username is required (min. 2 characters)'}), 400
    pw_ok, pw_errors = validate_password_strength(password, username)
    if not pw_ok:
        return jsonify({'error': pw_errors[0], 'password_errors': pw_errors}), 400

    # Check if exists
    r = host_run(f"id {_sq(username)} 2>/dev/null")
    if r.returncode == 0:
        return jsonify({'error': f'User "{username}" already exists'}), 400

    # Ensure nasos group exists
    ensure_nasos_group()

    # Determine home directory — prefer data disk
    dd = get_data_disk()
    if dd:
        home_base = os.path.join(dd, 'home')
        home_dir = os.path.join(home_base, username)
        os.makedirs(home_base, mode=0o755, exist_ok=True)
        cmd = f"sudo {_HELPER} user-add {_sq(username)} {_sq(shell)} {_sq(home_dir)}"
    else:
        cmd = f"sudo {_HELPER} user-add {_sq(username)} {_sq(shell)}"

    r = host_run(cmd)
    if r.returncode != 0:
        return jsonify({'error': f'Creation error: {r.stderr.strip()}'}), 500

    # Set password
    r = host_run(f"echo {_sq(password)} | sudo {_HELPER} user-set-password {_sq(username)}")
    if r.returncode != 0:
        return jsonify({'error': f'Password setting error: {r.stderr.strip()}'}), 500

    # Add to nasos group (mark as EthOS-created user)
    host_run(f"sudo {_HELPER} user-mod {_sq(username)} group-append {_sq(NASOS_GROUP)}")

    # Assign role-based groups
    if role == 'admin':
        host_run(f"sudo {_HELPER} user-mod {_sq(username)} group-append sudo")
        host_run(f"sudo {_HELPER} user-mod {_sq(username)} group-append ethos-admin")
    elif role == 'family':
        host_run(f"sudo {_HELPER} user-mod {_sq(username)} group-append {_sq(NASOS_FAMILY_GROUP)}")

    # Add to additional groups
    for g in groups:
        g = _safe_name(g)
        if g:
            host_run(f"sudo {_HELPER} user-mod {_sq(username)} group-append {_sq(g)}")

    # Create default folder structure (Dokumenty, Pobrane, …) + ~/.ethos
    ensure_user_home_structure(username)

    _users_cache['data'] = None  # invalidate cache
    audit_log('user.create', f'User "{username}" created (role: {role})')
    return jsonify({'success': True, 'username': username})


@users_bp.route('/delete', methods=['POST'])
def delete_user():
    """Delete a system user."""
    data = request.json or {}
    username = _safe_name(data.get('username', ''))
    if not username:
        return jsonify({'error': 'Username is required'}), 400
    if username == 'root':
        return jsonify({'error': 'Cannot delete root'}), 400

    r = host_run(f"sudo {_HELPER} user-del {_sq(username)} 2>&1")
    if r.returncode != 0:
        return jsonify({'error': f'Error: {r.stdout.strip() or r.stderr.strip()}'}), 500

    _users_cache['data'] = None  # invalidate cache
    audit_log('user.delete', f'User "{username}" deleted')
    return jsonify({'success': True})


@users_bp.route('/update', methods=['POST'])
def update_user():
    """Update user properties (password, shell, groups)."""
    client_ip = request.remote_addr or '0.0.0.0'
    if _rate_limiter.is_limited(f'update_user:{client_ip}'):
        return jsonify({"error": "Too many attempts. Try again in 5 minutes."}), 429
    data = request.json or {}
    username = _safe_name(data.get('username', ''))
    if not username:
        return jsonify({'error': 'Username is required'}), 400

    errors = []

    # Change password
    password = data.get('password')
    if password:
        r = host_run(f"echo {_sq(password)} | sudo {_HELPER} user-set-password {_sq(username)}")
        if r.returncode != 0:
            errors.append(f'Password: {r.stderr.strip()}')
        else:
            audit_log('user.password.change', f'Password changed for user "{username}"')

    # Change shell
    shell = data.get('shell')
    if shell:
        r = host_run(f"sudo {_HELPER} user-mod {_sq(username)} shell {_sq(shell)}")
        if r.returncode != 0:
            errors.append(f'Shell: {r.stderr.strip()}')

    # Update groups (set exact group list)
    groups = data.get('groups')
    if groups is not None:
        safe_groups = [_safe_name(g) for g in groups if _safe_name(g)]
        if safe_groups:
            groups_str = ','.join(_sq(g) for g in safe_groups)
            r = host_run(f"sudo {_HELPER} user-mod {_sq(username)} groups-set {groups_str}")
            if r.returncode != 0:
                errors.append(f'Groups: {r.stderr.strip()}')

    if errors:
        return jsonify({'error': '; '.join(errors)}), 500

    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@users_bp.route('/groups')
def list_groups():
    """List EthOS groups and user-created groups."""
    try:
        # Get all groups and all usernames (to filter out primary user groups)
        r = host_run("getent group | sort -t: -k3 -n")
        r_users = host_run("getent passwd | awk -F: '{print $1}'")
        user_names = set(r_users.stdout.strip().split('\n')) if r_users.returncode == 0 else set()
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
                # Skip hidden system groups and per-user primary groups (username == groupname)
                if gname in _HIDDEN_GROUPS or gname in user_names:
                    continue
                # Show EthOS-managed groups and user-created groups (gid >= 1000)
                if gname in _ETHOS_GROUPS or gid >= 1000:
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
        return jsonify({'error': 'Group name is required (min. 2 characters)'}), 400

    r = host_run(f"sudo {_HELPER} group-add {_sq(name)} 2>&1")
    if r.returncode != 0:
        return jsonify({'error': r.stdout.strip() or r.stderr.strip() or 'Group creation error'}), 500

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
        return jsonify({'error': 'Group name is required'}), 400

    r = host_run(f"sudo {_HELPER} group-del {_sq(name)} 2>&1")
    if r.returncode != 0:
        return jsonify({'error': r.stdout.strip() or r.stderr.strip() or 'Error'}), 500

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
        return jsonify({'error': 'Group is required'}), 400

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
            r = host_run(f"sudo {_HELPER} user-mod {_sq(u)} group-append {_sq(group)}")
            if r.returncode != 0:
                errors.append(f'Adding {u}: {r.stderr.strip()}')

    for u in remove_list:
        u = _safe_name(u)
        if u:
            r = host_run(f"sudo {_HELPER} group-mod {_sq(group)} {_sq(u)} remove 2>&1")
            if r.returncode != 0:
                errors.append(f'Removing {u}: {r.stdout.strip()}')

    if errors:
        return jsonify({'error': '; '.join(errors)}), 500
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# App Privileges
# ---------------------------------------------------------------------------

@users_bp.route('/roles')
def get_roles():
    """Return available EthOS roles with their app access lists."""
    from importlib import import_module
    try:
        _app = import_module('app')
        role_apps = getattr(_app, '_ROLE_APPS', {})
    except Exception:
        role_apps = {}
    return jsonify({
        'roles': [
            {'id': 'admin', 'name': 'Administrator', 'description': 'Full system access', 'apps': None},
            {'id': 'user', 'name': 'User', 'description': 'Access to work tools', 'apps': sorted(role_apps.get('user', []))},
            {'id': 'family', 'name': 'Family / Guest', 'description': 'Safe mode with basic apps', 'apps': sorted(role_apps.get('family', []))},
        ]
    })


@users_bp.route('/set-role', methods=['POST'])
def set_user_role():
    """Change a user's role. Admin only."""
    data = request.json or {}
    username = _safe_name(data.get('username', ''))
    new_role = data.get('role', '')

    if not username:
        return jsonify({'error': 'Username is required'}), 400
    if new_role not in _VALID_ROLES:
        return jsonify({'error': f'Invalid role. Allowed: {", ".join(sorted(_VALID_ROLES))}'}), 400
    if username == 'root':
        return jsonify({'error': 'Cannot change root role'}), 400

    # Remove from all role groups first
    host_run(f"sudo gpasswd -d {_sq(username)} sudo 2>/dev/null")
    host_run(f"sudo gpasswd -d {_sq(username)} ethos-admin 2>/dev/null")
    host_run(f"sudo gpasswd -d {_sq(username)} {_sq(NASOS_FAMILY_GROUP)} 2>/dev/null")

    # Add to appropriate groups
    if new_role == 'admin':
        host_run(f"sudo {_HELPER} user-mod {_sq(username)} group-append sudo")
        host_run(f"sudo {_HELPER} user-mod {_sq(username)} group-append ethos-admin")
        # Grant passwordless sudo
        sudoers_file = f"/etc/sudoers.d/010_{username}"
        host_run(f"echo {_sq(username + ' ALL=(ALL) NOPASSWD:ALL')} | sudo tee {_sq(sudoers_file)} > /dev/null && sudo chmod 440 {_sq(sudoers_file)}")
    elif new_role == 'family':
        host_run(f"sudo {_HELPER} user-mod {_sq(username)} group-append {_sq(NASOS_FAMILY_GROUP)}")
        # Remove sudoers file if exists
        host_run(f"sudo rm -f /etc/sudoers.d/010_{_sq(username)}")
    else:
        # 'user' role — just nasos group (already added at creation)
        host_run(f"sudo rm -f /etc/sudoers.d/010_{_sq(username)}")

    _users_cache['data'] = None  # invalidate cache
    audit_log('user.role.change', f'Role changed for "{username}" to "{new_role}"')
    return jsonify({'success': True, 'username': username, 'role': new_role})


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
        return jsonify({'valid': False, 'error': 'Username and password required'}), 400

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
        return jsonify({'valid': False, 'error': 'Invalid username or password'}), 401


# ---------------------------------------------------------------------------
# User Storage Quotas (btrfs qgroups)
# ---------------------------------------------------------------------------

_QUOTAS_FILE = data_path('user_quotas.json')


def _load_quotas():
    """Load user quota config: { username: { limit_gb: N } }."""
    if os.path.isfile(_QUOTAS_FILE):
        try:
            with open(_QUOTAS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_quotas(quotas):
    os.makedirs(os.path.dirname(_QUOTAS_FILE), exist_ok=True)
    with open(_QUOTAS_FILE, 'w') as f:
        json.dump(quotas, f, indent=2)


def _btrfs_quota_available():
    """Check if /mnt/data is btrfs and quotas can work."""
    r = host_run("stat -f -c '%T' /mnt/data 2>/dev/null")
    return r.returncode == 0 and 'btrfs' in r.stdout.strip().lower()


def _ensure_btrfs_quotas():
    """Enable btrfs quota on data partition if not already."""
    r = host_run("btrfs quota status /mnt/data 2>/dev/null")
    if 'Quota disabled' in r.stdout or r.returncode != 0:
        host_run("btrfs quota enable /mnt/data 2>/dev/null")


def _get_qgroup_for_user(username):
    """Find or create a qgroup for a user's home directory."""
    home = get_user_home(username)
    if not home or not os.path.isdir(home):
        return None

    # Check if the home dir is a btrfs subvolume
    r = host_run(f"btrfs subvolume show {_sq(home)} 2>/dev/null")
    if r.returncode != 0:
        return None

    # Parse subvolume ID
    for line in r.stdout.splitlines():
        if 'Subvolume ID' in line:
            subvol_id = line.split(':')[-1].strip()
            return f'0/{subvol_id}'
    return None


@users_bp.route('/quotas', methods=['GET'])
def get_quotas():
    """Get quota configuration and usage for all users."""
    if not _btrfs_quota_available():
        return jsonify({'ok': True, 'available': False, 'message': 'Btrfs quotas not available (data partition is not btrfs)'})

    _ensure_btrfs_quotas()
    quotas = _load_quotas()

    # Get actual usage from btrfs
    r = host_run("btrfs qgroup show -reF /mnt/data 2>/dev/null")
    qgroup_usage = {}
    if r.returncode == 0:
        for line in r.stdout.strip().splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 3:
                qgroup_usage[parts[0]] = {
                    'used_bytes': int(parts[1]) if parts[1].isdigit() else 0,
                    'exclusive_bytes': int(parts[2]) if parts[2].isdigit() else 0,
                }

    # Build per-user quota info
    users_quota = {}
    for username, cfg in quotas.items():
        qg = _get_qgroup_for_user(username)
        usage = qgroup_usage.get(qg, {}) if qg else {}
        limit_bytes = int(cfg.get('limit_gb', 0) * 1073741824) if cfg.get('limit_gb') else 0
        used = usage.get('used_bytes', 0)
        users_quota[username] = {
            'limit_gb': cfg.get('limit_gb', 0),
            'limit_bytes': limit_bytes,
            'used_bytes': used,
            'used_gb': round(used / 1073741824, 2) if used else 0,
            'percent': round((used / limit_bytes * 100), 1) if limit_bytes > 0 else 0,
            'qgroup': qg,
        }

    return jsonify({'ok': True, 'available': True, 'quotas': users_quota})


@users_bp.route('/quotas', methods=['PUT'])
def set_quota():
    """Set storage quota for a user.

    Body: { username: "user", limit_gb: 50 }
    Set limit_gb to 0 or null to remove quota.
    """
    data = request.json or {}
    username = _safe_name(data.get('username', ''))
    limit_gb = data.get('limit_gb', 0)

    if not username:
        return jsonify({'error': 'username required'}), 400
    if not _btrfs_quota_available():
        return jsonify({'error': 'Btrfs quotas not available'}), 400

    _ensure_btrfs_quotas()

    quotas = _load_quotas()

    if limit_gb and float(limit_gb) > 0:
        limit_gb = float(limit_gb)
        quotas[username] = {'limit_gb': limit_gb}

        # Apply btrfs qgroup limit
        qg = _get_qgroup_for_user(username)
        if qg:
            limit_bytes = int(limit_gb * 1073741824)
            host_run(f"btrfs qgroup limit {limit_bytes} {_sq(qg)} /mnt/data 2>/dev/null")
    else:
        # Remove quota
        quotas.pop(username, None)
        qg = _get_qgroup_for_user(username)
        if qg:
            host_run(f"btrfs qgroup limit none {_sq(qg)} /mnt/data 2>/dev/null")

    _save_quotas(quotas)
    audit_log('user.quota.change', f'Quota for "{username}" set to {limit_gb} GB')
    return jsonify({'ok': True, 'username': username, 'limit_gb': limit_gb})
