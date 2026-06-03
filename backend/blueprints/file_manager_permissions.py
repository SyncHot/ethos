"""EthOS — File Manager: folder passwords, file permissions, ACL, file favorites."""
import os
import sys
import sys as _sys
import re
import hashlib
import threading as _threading
import gevent.lock as _gevent_lock
import stat as _stat_mod
import grp as _grp
import pwd
import shlex as _shlex_acl
from flask import request, jsonify
from i18n import t
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from host import (
    data_path as _data_path,
    host_run as _host_run,
    host_run as _host_run_base,
    q,
    get_user_home as _get_user_home,
    user_data_path as _user_data_path,
)
from utils import (
    load_json as _load_json,
    save_json as _save_json,
    DATA_ROOT,
    ALLOWED_ROOTS as _ALLOWED_ROOTS,
)
from blueprints.eventlog import log as elog
from blueprints.admin_required import admin_required
from blueprints.auth import require_auth, get_current_user, get_token
from crypto_utils import (
    hash_folder_password as _hash_folder_password_new,
    verify_folder_password as _verify_folder_password,
)


def _main():
    """Return the main file_manager module."""
    return _sys.modules['blueprints.file_manager']


files_bp = _sys.modules['blueprints.file_manager'].files_bp

class _SocketioProxy:
    """Lazy proxy for socketio – avoids import-lock issues under gevent."""
    def __getattr__(self, name):
        sio = getattr(_sys.modules.get('app'), 'socketio', None)
        if sio is None:
            raise AttributeError(f'socketio not yet available ({name!r})')
        return getattr(sio, name)
socketio = _SocketioProxy()

# ─── Folder Passwords ────────────────────────────────────────
FOLDER_PASSWORDS_FILE = _data_path('folder_passwords.json')
_unlocked_folders = {}  # token -> set of unlocked folder paths
_uf_lock = _gevent_lock.RLock()

# Brute-force protection for folder unlock (per token, keyed by token+path)
_folder_unlock_attempts = {}  # key -> {'count': int, 'first': float, 'locked_until': float}
_folder_unlock_lock = _gevent_lock.RLock()
_FU_MAX_ATTEMPTS = 5
_FU_ATTEMPT_WINDOW = 120   # 2 minutes
_FU_LOCKOUT_TIME = 300     # 5 minute lockout

FOLDER_PASSWORD_MIN_LENGTH = 8


def _mode_to_symbolic(mode):
    """Convert numeric stat mode to symbolic string like 'rwxr-xr-x'."""
    flags = [
        (_stat_mod.S_IRUSR, 'r'), (_stat_mod.S_IWUSR, 'w'), (_stat_mod.S_IXUSR, 'x'),
        (_stat_mod.S_IRGRP, 'r'), (_stat_mod.S_IWGRP, 'w'), (_stat_mod.S_IXGRP, 'x'),
        (_stat_mod.S_IROTH, 'r'), (_stat_mod.S_IWOTH, 'w'), (_stat_mod.S_IXOTH, 'x'),
    ]
    return ''.join(c if mode & f else '-' for f, c in flags)


def _get_owner_group(st):
    """Return (owner_name, group_name) strings from a stat result."""
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, AttributeError):
        owner = str(st.st_uid)
    try:
        group = _grp.getgrgid(st.st_gid).gr_name
    except (KeyError, AttributeError):
        group = str(st.st_gid)
    return owner, group

def _load_folder_passwords():
    return _load_json(FOLDER_PASSWORDS_FILE, {})

def _save_folder_passwords(data):
    _save_json(FOLDER_PASSWORDS_FILE, data)

def _hash_folder_password(pw):
    return _hash_folder_password_new(pw)

def _is_folder_protected(user_path):
    """Check if user_path or any of its parents is password-protected."""
    passwords = _load_folder_passwords()
    if not passwords:
        return None
    # Normalize
    check = user_path.rstrip('/')
    while check and check != '/':
        if check in passwords:
            return check
        check = os.path.dirname(check)
    if '/' in passwords:
        return '/'
    return None

def _is_folder_unlocked(user_path):
    """Check if a protected folder is unlocked for the current session."""
    token = get_token()
    with _uf_lock:
        unlocked = _unlocked_folders.get(token, set())
    protected_path = _is_folder_protected(user_path)
    if protected_path is None:
        return True  # not protected
    return protected_path in unlocked

def _require_folder_access(user_path):
    """Returns None if access is allowed, or a Flask response tuple if blocked."""
    protected_path = _is_folder_protected(user_path)
    if protected_path is None:
        return None
    if _is_folder_unlocked(user_path):
        return None
    return jsonify({'error': 'Folder protected by password', 'locked': True, 'protected_path': protected_path}), 403


def _migrate_folder_passwords(old_user_path, new_user_path):
    """Update folder_passwords.json and _unlocked_folders when a folder is renamed/moved."""
    old_user_path = old_user_path.rstrip('/')
    new_user_path = new_user_path.rstrip('/')
    passwords = _load_folder_passwords()
    updated = {}
    changed = False
    for pw_path, pw_hash in passwords.items():
        if pw_path == old_user_path:
            updated[new_user_path] = pw_hash
            changed = True
        elif pw_path.startswith(old_user_path + '/'):
            updated[new_user_path + pw_path[len(old_user_path):]] = pw_hash
            changed = True
        else:
            updated[pw_path] = pw_hash
    if changed:
        _save_folder_passwords(updated)
        with _uf_lock:
            for token_set in list(_unlocked_folders.values()):
                to_add = set()
                to_remove = set()
                for p in token_set:
                    if p == old_user_path:
                        to_remove.add(p)
                        to_add.add(new_user_path)
                    elif p.startswith(old_user_path + '/'):
                        to_remove.add(p)
                        to_add.add(new_user_path + p[len(old_user_path):])
                token_set -= to_remove
                token_set |= to_add


@files_bp.route('/api/files/folder-password', methods=['GET'])
@require_auth
def folder_password_list():
    """List all password-protected folders."""
    passwords = _load_folder_passwords()
    return jsonify({'folders': list(passwords.keys())})


@files_bp.route('/api/files/folder-password', methods=['POST'])
@require_auth
def folder_password_set():
    """Set password for a folder."""
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/') or '/'
    password = data.get('password', '')
    if not password or len(password) < FOLDER_PASSWORD_MIN_LENGTH:
        return jsonify({'error': f'Password must be at least {FOLDER_PASSWORD_MIN_LENGTH} characters'}), 400
    # Complexity check: at least one letter and one number
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return jsonify({'error': 'Password must contain letters and numbers'}), 400
    real = _main().safe_path(path)
    if not real or not os.path.isdir(real):
        return jsonify({'error': 'Folder does not exist'}), 404
    cur = get_current_user()
    username = cur['username'] if cur else 'unknown'
    passwords = _load_folder_passwords()
    is_update = path in passwords
    passwords[path] = _hash_folder_password(password)
    _save_folder_passwords(passwords)
    action = 'Changed folder password' if is_update else 'Set folder password'
    elog('security', 'info', f'{action}: {path}', {'user': username, 'path': path})
    return jsonify({'ok': True})


@files_bp.route('/api/files/folder-password', methods=['DELETE'])
@require_auth
def folder_password_remove():
    """Remove password from a folder."""
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/') or '/'
    password = data.get('password', '')
    passwords = _load_folder_passwords()
    if path not in passwords:
        return jsonify({'error': 'Folder is not protected'}), 404
    cur = get_current_user()
    username = cur['username'] if cur else 'unknown'
    # Verify current password (unless admin override)
    is_admin = cur and cur.get('role') == 'admin'
    force = data.get('force', False)

    if not _verify_folder_password(password, passwords[path]):
        # Allow admin to force remove without correct password
        if is_admin and force:
            elog('security', 'warning', f'Forced removal of folder password by admin: {path}',
                 {'user': username, 'path': path})
        else:
            elog('security', 'warning', f'Failed attempt to remove folder password: {path}',
                 {'user': username, 'path': path})
            return jsonify({'error': t('auth.invalid_password')}), 403
    del passwords[path]
    _save_folder_passwords(passwords)
    # Remove from all unlock sessions
    with _uf_lock:
        for s in _unlocked_folders.values():
            s.discard(path)
    elog('security', 'info', f'Removed folder password: {path}', {'user': username, 'path': path})
    return jsonify({'ok': True})


@files_bp.route('/api/files/folder-unlock', methods=['POST'])
@require_auth
def folder_unlock():
    """Unlock a password-protected folder for the current session."""
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/') or '/'
    password = data.get('password', '')
    passwords = _load_folder_passwords()
    if path not in passwords:
        return jsonify({'error': 'Folder is not protected'}), 404

    token = get_token()
    cur = get_current_user()
    username = cur['username'] if cur else 'unknown'
    fu_key = f'{token}:{path}'
    now = time.time()

    # Check brute-force lockout
    with _folder_unlock_lock:
        attempt = _folder_unlock_attempts.get(fu_key)
        if attempt:
            if now < attempt.get('locked_until', 0):
                remaining = int(attempt['locked_until'] - now)
                elog('security', 'warning',
                     f'Folder locked against brute-force attack: {path}',
                     {'user': username, 'path': path, 'remaining_s': remaining})
                return jsonify({'error': f'Too many attempts. Wait {remaining}s'}), 429
            if now - attempt.get('first', now) > _FU_ATTEMPT_WINDOW:
                _folder_unlock_attempts.pop(fu_key, None)

    if not _verify_folder_password(password, passwords[path]):
        with _folder_unlock_lock:
            attempt = _folder_unlock_attempts.get(fu_key, {'count': 0, 'first': now, 'locked_until': 0})
            attempt['count'] += 1
            if attempt['count'] >= _FU_MAX_ATTEMPTS:
                attempt['locked_until'] = now + _FU_LOCKOUT_TIME
                attempt['count'] = 0
            _folder_unlock_attempts[fu_key] = attempt
        elog('security', 'warning', f'Nieudane odblokowanie folderu: {path}',
             {'user': username, 'path': path})
        return jsonify({'error': t('auth.invalid_password')}), 403

    # Successful unlock — clear attempts and record
    with _folder_unlock_lock:
        _folder_unlock_attempts.pop(fu_key, None)
    with _uf_lock:
        _unlocked_folders.setdefault(token, set()).add(path)
    elog('security', 'info', f'Odblokowano folder: {path}', {'user': username, 'path': path})
    return jsonify({'ok': True})


@files_bp.route('/api/files/folder-lock', methods=['POST'])
@require_auth
def folder_lock():
    """Re-lock a folder for the current session."""
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/') or '/'
    token = get_token()
    cur = get_current_user()
    username = cur['username'] if cur else 'unknown'
    with _uf_lock:
        if token in _unlocked_folders:
            _unlocked_folders[token].discard(path)
    elog('security', 'info', f'Zablokowano folder: {path}', {'user': username, 'path': path})
    return jsonify({'ok': True})


# ─── File Permissions ─────────────────────────────────────────

@files_bp.route('/api/files/permissions')
@require_auth
def files_get_permissions():
    """GET /api/files/permissions?path=<path>
    Returns detailed permissions info: symbolic mode, octal, owner, group.
    """
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    real = _main().safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404
    try:
        st = os.stat(real)
        owner, group = _get_owner_group(st)
        return jsonify({
            'path': path,
            'permissions': oct(st.st_mode)[-3:],
            'permissions_symbolic': _mode_to_symbolic(st.st_mode),
            'permissions_octal': '0' + oct(st.st_mode & 0o7777)[2:],
            'owner': owner,
            'group': group,
            'uid': st.st_uid,
            'gid': st.st_gid,
            'is_dir': os.path.isdir(real),
        })
    except OSError as e:
        return jsonify({'error': str(e)}), 500


@files_bp.route('/api/files/chmod', methods=['POST'])
@require_auth
def files_chmod():
    """POST /api/files/chmod — Change file/folder permissions.
    Body: { path, mode }  where mode is an octal string like '755' or '644'.
    Admin only.
    """
    cur = get_current_user()
    if not cur or cur.get('role') != 'admin':
        return jsonify({'error': 'Administrator privileges required'}), 403
    data = request.get_json(force=True)
    path = data.get('path', '')
    mode_str = data.get('mode', '')
    if not path or not mode_str:
        return jsonify({'error': 'Path and mode are required'}), 400

    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    # Validate mode: must be 3-4 octal digits
    if not re.match(r'^[0-7]{3,4}$', mode_str):
        return jsonify({'error': 'Invalid permissions mode (e.g. 755, 644)'}), 400
    real = _main().safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404
    try:
        new_mode = int(mode_str, 8)
        os.chmod(real, new_mode)
        st = os.stat(real)
        owner, group = _get_owner_group(st)
        username = cur['username']
        elog('security', 'info', f'Changed permissions: {path} → {mode_str}',
             {'user': username, 'path': path, 'mode': mode_str})
        return jsonify({
            'ok': True,
            'permissions': oct(st.st_mode)[-3:],
            'permissions_symbolic': _mode_to_symbolic(st.st_mode),
            'owner': owner,
            'group': group,
        })
    except PermissionError:
        return jsonify({'error': t('auth.no_permission')}), 403
    except OSError as e:
        return jsonify({'error': str(e)}), 500
_FAVORITES_GLOBAL = _data_path('favorites.json')  # legacy, used for migration

def _favorites_file():
    """Return per-user favorites file path."""
    cur = get_current_user()
    username = cur['username'] if cur else None
    if username:
        return _user_data_path('favorites.json', username)
    return _FAVORITES_GLOBAL

def _load_favorites():
    return _load_json(_favorites_file(), [])

def _save_favorites(favs):
    _save_json(_favorites_file(), favs)


@files_bp.route('/api/files/chown', methods=['POST'])
@require_auth
def files_chown():
    """POST /api/files/chown — Change file/folder owner/group.
    Body: { path, owner, group }
    Admin only.
    """
    cur = get_current_user()
    if not cur or cur.get('role') != 'admin':
        return jsonify({'error': 'Administrator privileges required'}), 403
    data = request.get_json(force=True)
    path = data.get('path', '')
    owner = data.get('owner', '')
    group = data.get('group', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    if not owner and not group:
        return jsonify({'error': 'Owner or group is required'}), 400

    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    real = _main().safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404

    try:
        uid = -1
        gid = -1
        if owner:
            try:
                uid = pwd.getpwnam(owner).pw_uid
            except KeyError:
                return jsonify({'error': f'User {owner} does not exist'}), 400
        if group:
            try:
                gid = _grp.getgrnam(group).gr_gid
            except KeyError:
                return jsonify({'error': f'Group {group} does not exist'}), 400

        os.chown(real, uid, gid)
        st = os.stat(real)
        new_owner, new_group = _get_owner_group(st)
        username = cur['username']
        elog('security', 'info', f'Changed owner: {path} → {new_owner}:{new_group}',
             {'user': username, 'path': path, 'owner': new_owner, 'group': new_group})
        return jsonify({
            'ok': True,
            'owner': new_owner,
            'group': new_group
        })
    except PermissionError:
        return jsonify({'error': t('auth.no_permission')}), 403
    except OSError as e:
        return jsonify({'error': str(e)}), 500


# ── Per-folder POSIX ACL management ──────────────────────────────────────────
# shlex imported in header

def _parse_getfacl(output):
    """Parse getfacl output into structured ACL dict.
    Returns: { 'users': { 'name': 'rwx', ... }, 'groups': { ... },
               'owner': 'rwx', 'group': 'rwx', 'other': 'rwx',
               'default_users': { ... }, 'default_groups': { ... },
               'default_owner': 'rwx', 'default_group': 'rwx', 'default_other': 'rwx' }
    """
    acl = {
        'users': {}, 'groups': {},
        'owner': '', 'group': '', 'other': '',
        'default_users': {}, 'default_groups': {},
        'default_owner': '', 'default_group': '', 'default_other': '',
        'mask': '', 'default_mask': '',
    }
    for line in output.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(':')
        if len(parts) < 3:
            continue
        # Standard entries: user::rwx, user:bob:r-x, group::r-x, etc.
        # Default entries: default:user::rwx, default:user:bob:r-x, etc.
        is_default = parts[0] == 'default'
        if is_default:
            parts = parts[1:]
        if len(parts) < 3:
            continue
        etype = parts[0]   # user, group, mask, other
        ename = parts[1]   # empty for owner/group/other, or username/groupname
        eperm = parts[2]   # rwx, r-x, etc.
        prefix = 'default_' if is_default else ''
        if etype == 'user':
            if ename:
                acl[prefix + 'users'][ename] = eperm
            else:
                acl[prefix + 'owner'] = eperm
        elif etype == 'group':
            if ename:
                acl[prefix + 'groups'][ename] = eperm
            else:
                acl[prefix + 'group'] = eperm
        elif etype == 'other':
            acl[prefix + 'other'] = eperm
        elif etype == 'mask':
            acl[prefix + 'mask'] = eperm
    return acl


def _perm_to_simple(perm_str):
    """Convert rwx string to simple label: 'rw', 'ro', or 'none'."""
    if not perm_str or perm_str == '---':
        return 'none'
    has_w = 'w' in perm_str
    has_r = 'r' in perm_str
    if has_w:
        return 'rw'
    if has_r:
        return 'ro'
    return 'none'


def _simple_to_perm(simple, is_dir=True):
    """Convert simple label to rwx string."""
    if simple == 'rw':
        return 'rwx' if is_dir else 'rw-'
    elif simple == 'ro':
        return 'r-x' if is_dir else 'r--'
    return '---'


@files_bp.route('/api/files/acl')
@require_auth
def files_get_acl():
    """GET /api/files/acl?path=<path>
    Returns POSIX ACLs for the given path using getfacl.
    """
    cur = get_current_user()
    if not cur or cur.get('role') != 'admin':
        return jsonify({'error': 'Administrator privileges required'}), 403
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    real = _main().safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404

    r = _host_run_base(f"getfacl -p {_shlex_acl.quote(real)} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({'error': 'Could not read ACL (filesystem may not support ACLs)'}), 500

    acl = _parse_getfacl(r.stdout)
    # Convert rwx strings to simple labels for the UI
    entries = []
    for user, perm in acl['users'].items():
        entries.append({'type': 'user', 'name': user, 'access': _perm_to_simple(perm)})
    for grp, perm in acl['groups'].items():
        entries.append({'type': 'group', 'name': grp, 'access': _perm_to_simple(perm)})

    defaults = []
    for user, perm in acl['default_users'].items():
        defaults.append({'type': 'user', 'name': user, 'access': _perm_to_simple(perm)})
    for grp, perm in acl['default_groups'].items():
        defaults.append({'type': 'group', 'name': grp, 'access': _perm_to_simple(perm)})

    return jsonify({
        'ok': True,
        'path': path,
        'is_dir': os.path.isdir(real),
        'entries': entries,
        'defaults': defaults,
        'base': {
            'owner': acl['owner'],
            'group': acl['group'],
            'other': acl['other'],
        }
    })


@files_bp.route('/api/files/acl', methods=['PUT'])
@require_auth
def files_set_acl():
    """PUT /api/files/acl — Set POSIX ACLs on a file/folder.
    Body: {
        path: "/some/folder",
        entries: [ { type: "user"|"group", name: "bob", access: "rw"|"ro"|"none" }, ... ],
        inherit: true|false,     // if true, set default ACLs too (dirs only)
        recursive: true|false    // if true, apply -R recursively
    }
    """
    cur = get_current_user()
    if not cur or cur.get('role') != 'admin':
        return jsonify({'error': 'Administrator privileges required'}), 403

    data = request.get_json(force=True)
    path = data.get('path', '')
    entries = data.get('entries', [])
    inherit = data.get('inherit', False)
    recursive = data.get('recursive', False)

    if not path:
        return jsonify({'error': 'Path is required'}), 400

    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    real = _main().safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'File does not exist'}), 404

    is_dir = os.path.isdir(real)
    rflag = '-R ' if recursive else ''
    username = cur['username']

    # Build setfacl commands
    cmds = []
    for entry in entries:
        etype = entry.get('type', '')
        ename = entry.get('name', '')
        access = entry.get('access', 'none')
        if etype not in ('user', 'group') or not ename:
            continue

        prefix = 'u' if etype == 'user' else 'g'
        perm = _simple_to_perm(access, is_dir)
        _sq = _shlex_acl.quote

        if access == 'none':
            cmds.append(f"setfacl {rflag}-x {prefix}:{_sq(ename)} {_sq(real)} 2>/dev/null; true")
            if inherit and is_dir:
                cmds.append(f"setfacl {rflag}-x d:{prefix}:{_sq(ename)} {_sq(real)} 2>/dev/null; true")
        else:
            cmds.append(f"setfacl {rflag}-m {prefix}:{_sq(ename)}:{perm} {_sq(real)}")
            if inherit and is_dir:
                cmds.append(f"setfacl {rflag}-m d:{prefix}:{_sq(ename)}:{perm} {_sq(real)}")

    if not cmds:
        return jsonify({'ok': True, 'message': 'No changes to apply'})

    errors = []
    for cmd in cmds:
        r = _host_run_base(cmd, timeout=60)
        if r.returncode != 0 and 'true' not in cmd:
            errors.append(r.stderr.strip() if r.stderr else f'Command failed: {cmd}')

    if errors:
        return jsonify({'error': '; '.join(errors)}), 500

    elog('security', 'info', f'ACL updated: {path}' + (' (recursive)' if recursive else ''),
         {'user': username, 'path': path, 'entries': len(entries), 'inherit': inherit, 'recursive': recursive})

    return jsonify({'ok': True})


@files_bp.route('/api/files/favorites')
@require_auth
def files_favorites_list():
    return jsonify(_load_favorites())


@files_bp.route('/api/files/favorites', methods=['POST'])
@require_auth
def files_favorites_add():
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/')
    label = data.get('label', '') or os.path.basename(path) or path
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    favs = _load_favorites()
    if any(f['path'] == path for f in favs):
        return jsonify({'error': 'Already in favorites'}), 409
    favs.append({'path': path, 'label': label})
    _save_favorites(favs)
    return jsonify({'ok': True, 'favorites': favs})


@files_bp.route('/api/files/favorites', methods=['DELETE'])
@require_auth
def files_favorites_remove():
    data = request.get_json(force=True)
    path = data.get('path', '').rstrip('/')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    favs = _load_favorites()
    favs = [f for f in favs if f['path'] != path]
    _save_favorites(favs)
    return jsonify({'ok': True, 'favorites': favs})


