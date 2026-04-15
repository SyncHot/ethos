"""LDAP / Active Directory Authentication Blueprint.

Endpoints:
  GET  /api/ldap/config         — Get LDAP configuration (masked)
  PUT  /api/ldap/config         — Save LDAP configuration
  POST /api/ldap/test           — Test LDAP connection
  POST /api/ldap/sync           — Sync LDAP users to local system
  GET  /api/ldap/status         — Get LDAP status (enabled, last sync, etc.)
  DELETE /api/ldap/config       — Disable and clear LDAP configuration

Socket.IO events:
  (none)
"""

import os
import json
import time
import logging
from flask import Blueprint, request, jsonify
from blueprints.admin_required import admin_required

ldap_bp = Blueprint('ldap', __name__)
_log = logging.getLogger('ethos.ldap')

# ── Config storage ───────────────────────────────────────────────────────────

def _data_path(*parts):
    base = os.environ.get('ETHOS_DATA', os.path.join(os.path.dirname(__file__), '..', '..', 'data'))
    return os.path.join(base, *parts)

_LDAP_CONFIG_FILE = _data_path('ldap_config.json')

_DEFAULT_CONFIG = {
    'enabled': False,
    'server': '',
    'port': 389,
    'use_ssl': False,
    'use_starttls': True,
    'bind_dn': '',
    'bind_password': '',
    'base_dn': '',
    'user_filter': '(&(objectClass=user)(sAMAccountName={username}))',
    'user_attr': 'sAMAccountName',
    'mail_attr': 'mail',
    'display_name_attr': 'displayName',
    'group_filter': '(&(objectClass=group)(member={user_dn}))',
    'admin_groups': [],
    'user_groups': [],
    'family_groups': [],
    'auto_create_users': True,
    'default_shell': '/bin/bash',
    'sync_interval_hours': 6,
    'last_sync': None,
    'last_sync_result': None,
}

_SENSITIVE_KEYS = {'bind_password'}


def _load_config():
    if os.path.isfile(_LDAP_CONFIG_FILE):
        try:
            with open(_LDAP_CONFIG_FILE) as f:
                cfg = json.load(f)
            merged = dict(_DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def _save_config(cfg):
    os.makedirs(os.path.dirname(_LDAP_CONFIG_FILE), exist_ok=True)
    with open(_LDAP_CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.chmod(_LDAP_CONFIG_FILE, 0o600)


def _mask_config(cfg):
    """Return config with sensitive fields masked."""
    out = dict(cfg)
    for k in _SENSITIVE_KEYS:
        if out.get(k):
            out[k] = '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022'
    return out


# ── LDAP connection helper ───────────────────────────────────────────────────

def _get_ldap_connection(cfg):
    """Create and return an LDAP connection using config."""
    import ldap3
    from ldap3 import Server, Connection, ALL, Tls
    import ssl

    server_kwargs = {
        'host': cfg['server'],
        'port': cfg.get('port', 389),
        'get_info': ALL,
        'connect_timeout': 10,
    }

    if cfg.get('use_ssl'):
        tls = Tls(validate=ssl.CERT_NONE)
        server_kwargs['use_ssl'] = True
        server_kwargs['tls'] = tls
        if not cfg.get('port') or cfg['port'] == 389:
            server_kwargs['port'] = 636

    server = Server(**server_kwargs)

    conn = Connection(
        server,
        user=cfg.get('bind_dn', ''),
        password=cfg.get('bind_password', ''),
        auto_bind=False,
        raise_exceptions=False,
    )

    if not conn.bind():
        raise ConnectionError(f"LDAP bind failed: {conn.result.get('description', 'Unknown error')}")

    if cfg.get('use_starttls') and not cfg.get('use_ssl'):
        tls = Tls(validate=ssl.CERT_NONE)
        conn.server.tls = tls
        if not conn.start_tls():
            _log.warning("StartTLS failed, continuing without TLS")

    return conn


def _ldap_authenticate(username, password, cfg=None):
    """Attempt to authenticate a user via LDAP.
    Returns dict with 'success', 'dn', 'attrs', 'role' keys.
    """
    if cfg is None:
        cfg = _load_config()
    if not cfg.get('enabled') or not cfg.get('server'):
        return {'success': False, 'error': 'LDAP not configured'}

    try:
        import ldap3
        from ldap3 import Connection, Server, Tls
        import ssl
    except ImportError:
        return {'success': False, 'error': 'ldap3 package not installed'}

    try:
        conn = _get_ldap_connection(cfg)
        user_filter = cfg.get('user_filter', '(&(objectClass=user)(sAMAccountName={username}))').replace(
            '{username}', ldap3.utils.conv.escape_filter_chars(username))
        user_attr = cfg.get('user_attr', 'sAMAccountName')
        mail_attr = cfg.get('mail_attr', 'mail')
        display_attr = cfg.get('display_name_attr', 'displayName')

        conn.search(
            search_base=cfg['base_dn'],
            search_filter=user_filter,
            attributes=[user_attr, mail_attr, display_attr, 'memberOf'],
        )

        if not conn.entries:
            conn.unbind()
            return {'success': False, 'error': 'User not found in LDAP'}

        user_entry = conn.entries[0]
        user_dn = str(user_entry.entry_dn)
        conn.unbind()

        # Bind with user credentials to verify password
        server_kwargs = {'host': cfg['server'], 'port': cfg.get('port', 389), 'connect_timeout': 10}
        if cfg.get('use_ssl'):
            tls = Tls(validate=ssl.CERT_NONE)
            server_kwargs['use_ssl'] = True
            server_kwargs['tls'] = tls
            if not cfg.get('port') or cfg['port'] == 389:
                server_kwargs['port'] = 636

        server = Server(**server_kwargs)
        user_conn = Connection(server, user=user_dn, password=password, auto_bind=False, raise_exceptions=False)

        if not user_conn.bind():
            return {'success': False, 'error': 'Invalid credentials'}
        user_conn.unbind()

        attrs = {
            'username': str(getattr(user_entry, user_attr, username)),
            'email': str(getattr(user_entry, mail_attr, '')) if hasattr(user_entry, mail_attr) else '',
            'display_name': str(getattr(user_entry, display_attr, '')) if hasattr(user_entry, display_attr) else '',
            'member_of': [str(g) for g in getattr(user_entry, 'memberOf', [])],
        }

        role = _determine_role(attrs.get('member_of', []), cfg)
        return {'success': True, 'dn': user_dn, 'attrs': attrs, 'role': role}

    except ConnectionError as e:
        return {'success': False, 'error': str(e)}
    except Exception as e:
        _log.exception("LDAP authentication error")
        return {'success': False, 'error': f'LDAP error: {str(e)}'}


def _determine_role(member_of_dns, cfg):
    """Determine EthOS role based on LDAP group membership."""
    def _cn_match(dn_list, group_names):
        for dn in dn_list:
            cn = dn.split(',')[0].replace('CN=', '').replace('cn=', '').strip()
            if cn.lower() in [g.lower() for g in group_names]:
                return True
        return False

    admin_groups = cfg.get('admin_groups', [])
    family_groups = cfg.get('family_groups', [])

    if admin_groups and _cn_match(member_of_dns, admin_groups):
        return 'admin'
    if family_groups and _cn_match(member_of_dns, family_groups):
        return 'family'
    return 'user'


def _ensure_local_user(username, role='user', shell='/bin/bash'):
    """Create a local system user if they don't exist yet."""
    import subprocess
    r = subprocess.run(['id', username], capture_output=True, timeout=5)
    if r.returncode == 0:
        return True

    try:
        from host import host_run, q, get_data_disk
        dd = get_data_disk()
        home_dir = os.path.join(dd, 'home', username) if dd else f'/home/{username}'
        os.makedirs(home_dir, exist_ok=True)

        host_run(f"useradd -m -d {q(home_dir)} -s {q(shell)} {q(username)}", timeout=15)
        host_run(f"usermod -aG ethos-user {q(username)}", timeout=10)
        if role == 'admin':
            host_run(f"usermod -aG sudo,ethos-admin {q(username)}", timeout=10)
        elif role == 'family':
            host_run(f"usermod -aG ethos-family {q(username)}", timeout=10)

        import secrets
        random_pw = secrets.token_hex(32)
        host_run(f"echo {q(username + ':' + random_pw)} | chpasswd", timeout=10)

        _log.info(f"Created local user {username} (role={role}) for LDAP sync")
        return True
    except Exception as e:
        _log.error(f"Failed to create local user {username}: {e}")
        return False


# ── Endpoints ────────────────────────────────────────────────────────────────

@ldap_bp.route('/api/ldap/config', methods=['GET'])
@admin_required
def get_ldap_config():
    cfg = _load_config()
    return jsonify({'ok': True, 'config': _mask_config(cfg)})


@ldap_bp.route('/api/ldap/config', methods=['PUT'])
@admin_required
def set_ldap_config():
    data = request.get_json(force=True)
    cfg = _load_config()
    for key in _DEFAULT_CONFIG:
        if key in data:
            if key in _SENSITIVE_KEYS and data[key] == '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022':
                continue
            cfg[key] = data[key]
    _save_config(cfg)
    return jsonify({'ok': True, 'config': _mask_config(cfg)})


@ldap_bp.route('/api/ldap/config', methods=['DELETE'])
@admin_required
def delete_ldap_config():
    cfg = dict(_DEFAULT_CONFIG)
    _save_config(cfg)
    return jsonify({'ok': True, 'message': 'LDAP configuration reset'})


@ldap_bp.route('/api/ldap/test', methods=['POST'])
@admin_required
def test_ldap():
    data = request.get_json(force=True) if request.is_json else {}
    cfg = _load_config()
    for k, v in data.items():
        if k in _SENSITIVE_KEYS and v == '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022':
            continue
        cfg[k] = v

    if not cfg.get('server'):
        return jsonify({'ok': False, 'error': 'Server address is required'}), 400

    try:
        import ldap3
    except ImportError:
        return jsonify({'ok': False, 'error': 'ldap3 package not installed. Install via App Manager.'}), 500

    try:
        conn = _get_ldap_connection(cfg)
        user_filter = cfg.get('user_filter', '(&(objectClass=user)(sAMAccountName=*))').replace('{username}', '*')
        conn.search(
            search_base=cfg['base_dn'],
            search_filter=user_filter,
            attributes=[cfg.get('user_attr', 'sAMAccountName')],
            size_limit=5,
        )
        user_count = len(conn.entries)
        users_found = [str(getattr(e, cfg.get('user_attr', 'sAMAccountName'), '?')) for e in conn.entries]
        conn.unbind()
        return jsonify({
            'ok': True,
            'user_count': user_count,
            'message': f'Connected successfully. Found {user_count} user(s).',
            'users_sample': users_found[:5],
        })
    except ConnectionError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        _log.exception("LDAP test failed")
        return jsonify({'ok': False, 'error': f'Connection failed: {str(e)}'}), 500


@ldap_bp.route('/api/ldap/sync', methods=['POST'])
@admin_required
def sync_ldap_users():
    cfg = _load_config()
    if not cfg.get('enabled') or not cfg.get('server'):
        return jsonify({'error': 'LDAP not enabled'}), 400

    try:
        import ldap3
    except ImportError:
        return jsonify({'error': 'ldap3 not installed'}), 500

    try:
        conn = _get_ldap_connection(cfg)
        user_filter = cfg.get('user_filter', '(&(objectClass=user)(sAMAccountName=*))').replace('{username}', '*')
        user_attr = cfg.get('user_attr', 'sAMAccountName')

        conn.search(
            search_base=cfg['base_dn'],
            search_filter=user_filter,
            attributes=[user_attr, 'memberOf', cfg.get('mail_attr', 'mail')],
            size_limit=500,
        )

        synced = 0
        errors = []
        import re
        for entry in conn.entries:
            uname = str(getattr(entry, user_attr, ''))
            if not uname or len(uname) > 32:
                continue
            if not re.match(r'^[a-zA-Z0-9_.\-]+$', uname):
                continue

            member_of = [str(g) for g in getattr(entry, 'memberOf', [])]
            role = _determine_role(member_of, cfg)

            if cfg.get('auto_create_users', True):
                if _ensure_local_user(uname, role, cfg.get('default_shell', '/bin/bash')):
                    synced += 1
                else:
                    errors.append(uname)

        conn.unbind()

        cfg['last_sync'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        cfg['last_sync_result'] = f'{synced} synced' + (f', {len(errors)} errors' if errors else '')
        _save_config(cfg)

        return jsonify({
            'ok': True,
            'synced': synced,
            'errors': errors,
            'message': f'Synced {synced} user(s)' + (f', {len(errors)} error(s)' if errors else ''),
        })

    except Exception as e:
        _log.exception("LDAP sync failed")
        cfg['last_sync'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        cfg['last_sync_result'] = f'Error: {str(e)}'
        _save_config(cfg)
        return jsonify({'error': f'Sync failed: {str(e)}'}), 500


@ldap_bp.route('/api/ldap/status', methods=['GET'])
@admin_required
def ldap_status():
    cfg = _load_config()
    ldap3_installed = False
    try:
        import ldap3
        ldap3_installed = True
    except ImportError:
        pass

    return jsonify({
        'ok': True,
        'enabled': cfg.get('enabled', False),
        'server': cfg.get('server', ''),
        'ldap3_installed': ldap3_installed,
        'last_sync': cfg.get('last_sync'),
        'last_sync_result': cfg.get('last_sync_result'),
        'auto_create_users': cfg.get('auto_create_users', True),
    })


# ── Public API for login integration ─────────────────────────────────────────

def try_ldap_auth(username, password):
    """Called from app.py login endpoint. Returns role string or None."""
    cfg = _load_config()
    if not cfg.get('enabled'):
        return None

    result = _ldap_authenticate(username, password, cfg)
    if not result.get('success'):
        return None

    role = result.get('role', 'user')

    if cfg.get('auto_create_users', True):
        _ensure_local_user(username, role, cfg.get('default_shell', '/bin/bash'))

    return role
