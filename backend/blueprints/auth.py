"""
EthOS — Auth Blueprint
Extracted from app.py (lines 653–1555).

Exposes:
  auth_bp      — /api/auth/* routes (login, verify, logout, sudo)
  security_bp  — /api/security/* routes (settings, lockouts)
  require_auth — decorator for protected endpoints
  get_current_user — returns {username, role} or None
  get_token    — extracts Bearer / cookie token from request
  generate_token — creates a new token
  tokens       — the global _TokenStore instance
"""

import os
import sys
import time
import json
import re
import hashlib
import secrets
import warnings as _warnings
import logging
from collections import defaultdict as _defaultdict
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify, g, current_app
from i18n import t

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from host import (
    host_run as _host_run_base,
    app_path as _app_path,
    data_path as _data_path,
    ETHOS_ROOT,
    get_user_home as _get_user_home,
    ensure_user_home_structure as _ensure_user_home_structure,
)
from utils import load_json as _load_json, save_json as _save_json
from blueprints.eventlog import log as elog
from audit import audit_log

# ── Blueprints ──────────────────────────────────────────────────────────────

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')
security_bp = Blueprint('security', __name__, url_prefix='/api/security')

# ── Loggers (Fail2Ban compatible) ───────────────────────────────────────────

_AUTH_LOG_FILE = '/opt/ethos/logs/auth.log'
auth_logger = logging.getLogger('ethos_auth')
auth_logger.setLevel(logging.INFO)
if not auth_logger.handlers:
    try:
        from logging.handlers import RotatingFileHandler as _RFH
        os.makedirs(os.path.dirname(_AUTH_LOG_FILE), exist_ok=True)
        _h = _RFH(_AUTH_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
        _h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        auth_logger.addHandler(_h)
    except Exception:
        pass

_ACCESS_LOG_FILE = '/opt/ethos/logs/access.log'
access_logger = logging.getLogger('ethos_access')
access_logger.setLevel(logging.INFO)
if not access_logger.handlers:
    try:
        from logging.handlers import RotatingFileHandler as _RFH2
        os.makedirs(os.path.dirname(_ACCESS_LOG_FILE), exist_ok=True)
        _h2 = _RFH2(_ACCESS_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
        _h2.setFormatter(logging.Formatter('%(message)s'))
        access_logger.addHandler(_h2)
    except Exception:
        pass

# ── SocketIO helper (avoids import-lock under gevent) ───────────────────────

def _get_sio():
    app_mod = sys.modules.get('app')
    return getattr(app_mod, 'socketio', None) if app_mod else None

# ── App-level helpers (resolved at call-time to avoid circular imports) ──────

def _get_nas_name():
    return os.environ.get('NAS_NAME', 'EthOS')

def _get_brand_name():
    return os.environ.get('BRAND_NAME', 'EthOS')

PASSWORD_CHANGED_MARKER = '/opt/ethos/.password_changed'
SETUP_DONE_FILE = _data_path('setup_done')

def _is_setup_done():
    return os.path.exists(SETUP_DONE_FILE)

def _verify_shadow_hash(password: str, stored_hash: str) -> bool:
    """Verify password against /etc/shadow hash without DeprecationWarning."""
    with _warnings.catch_warnings():
        _warnings.filterwarnings('ignore', category=DeprecationWarning)
        import crypt as _crypt_mod
        return _crypt_mod.crypt(password, stored_hash) == stored_hash

# ─────────────────────────── Token store ────────────────────────────────────

TOKEN_EXPIRY = timedelta(days=7)
SESSION_IDLE_TIMEOUT = 1800  # 30 minutes idle → expire (configurable)
_tokens_lock = __import__('threading').Lock()

# ─── Brute-force protection state (defined before startup settings apply) ───

_login_attempts = {}        # ip -> {'count': int, 'first': float, 'locked_until': float}
_user_login_attempts = {}   # username -> {'count': int, 'first': float, 'locked_until': float}
_login_lock = __import__('threading').Lock()
_MAX_ATTEMPTS = 5
_ATTEMPT_WINDOW = 300        # 5 minutes
_LOCKOUT_TIME = 300          # 5 minute lockout after max attempts
_USER_MAX_ATTEMPTS = 5
_USER_LOCKOUT_TIME = 1800    # 30 minute lockout per account


class _TokenStore:
    """SQLite-backed token store shared across gunicorn workers.

    Exposes dict-like .get(), .pop(), .items(), [] set/get so existing
    code works without changes.
    """

    def __init__(self):
        import sqlite3 as _sql
        self._db_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'data', 'tokens.db'
        )
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = _sql.connect(self._db_path, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute(
            'CREATE TABLE IF NOT EXISTS tokens '
            '(token TEXT PRIMARY KEY, username TEXT, role TEXT, expires REAL, last_active REAL)'
        )
        # Add last_active column if missing (migration)
        try:
            conn.execute('ALTER TABLE tokens ADD COLUMN last_active REAL DEFAULT 0')
        except _sql.OperationalError:
            pass
        conn.commit()
        conn.close()

    def _conn(self):
        import sqlite3 as _sql
        return _sql.connect(self._db_path, timeout=5)

    def __setitem__(self, token, info):
        expires = info['expires']
        ts = expires.timestamp() if isinstance(expires, datetime) else float(expires)
        la = info.get('last_active', time.time())
        conn = self._conn()
        try:
            conn.execute(
                'INSERT OR REPLACE INTO tokens (token,username,role,expires,last_active) '
                'VALUES (?,?,?,?,?)',
                (token, info['username'], info['role'], ts, la),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, token, default=None):
        if not token:
            return default
        conn = self._conn()
        try:
            row = conn.execute(
                'SELECT username,role,expires FROM tokens WHERE token=?',
                (token,),
            ).fetchone()
        finally:
            conn.close()
        if row:
            return {
                'username': row[0],
                'role': row[1],
                'expires': datetime.fromtimestamp(float(row[2])) if not isinstance(row[2], str) or row[2].replace('.', '', 1).isdigit() else datetime.fromisoformat(row[2]),
            }
        return default

    def pop(self, token, *args):
        result = self.get(token)
        if result is not None:
            conn = self._conn()
            try:
                conn.execute('DELETE FROM tokens WHERE token=?', (token,))
                conn.commit()
            finally:
                conn.close()
            return result
        return args[0] if args else None

    def items(self):
        conn = self._conn()
        try:
            rows = conn.execute(
                'SELECT token,username,role,expires FROM tokens'
            ).fetchall()
        finally:
            conn.close()
        return [
            (r[0], {'username': r[1], 'role': r[2],
                    'expires': datetime.fromtimestamp(float(r[3])) if not isinstance(r[3], str) or r[3].replace('.', '', 1).isdigit() else datetime.fromisoformat(r[3])})
            for r in rows
        ]

    def __contains__(self, token):
        return self.get(token) is not None

    def prune_expired(self):
        conn = self._conn()
        try:
            conn.execute(
                'DELETE FROM tokens WHERE expires < ?',
                (datetime.now().timestamp(),),
            )
            conn.commit()
        finally:
            conn.close()

    def touch(self, token):
        """Update last_active timestamp for session idle tracking."""
        conn = self._conn()
        try:
            conn.execute(
                'UPDATE tokens SET last_active=? WHERE token=?',
                (time.time(), token),
            )
            conn.commit()
        finally:
            conn.close()

    def prune_idle(self, idle_seconds):
        """Remove tokens that have been idle longer than idle_seconds."""
        cutoff = time.time() - idle_seconds
        conn = self._conn()
        try:
            conn.execute(
                'DELETE FROM tokens WHERE last_active > 0 AND last_active < ?',
                (cutoff,),
            )
            conn.commit()
        finally:
            conn.close()


tokens = _TokenStore()

# Apply persisted security settings on startup
try:
    _sec_file = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'security_settings.json')
    if os.path.exists(_sec_file):
        with open(_sec_file, 'r') as _sf:
            _sec_cfg = json.load(_sf)
        SESSION_IDLE_TIMEOUT = _sec_cfg.get('session_idle_timeout', SESSION_IDLE_TIMEOUT)
        _USER_MAX_ATTEMPTS = _sec_cfg.get('user_lockout_attempts', _USER_MAX_ATTEMPTS)
        _USER_LOCKOUT_TIME = _sec_cfg.get('user_lockout_duration', _USER_LOCKOUT_TIME)
except Exception:
    pass

# ─────────────────────────── Core auth helpers ──────────────────────────────


def generate_token(username='admin', role='admin'):
    token = secrets.token_hex(32)
    tokens[token] = {
        'expires': datetime.now() + TOKEN_EXPIRY,
        'username': username,
        'role': role,
    }
    return token


def get_token():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    # Support token in query param (for direct downloads via <a> links)
    qt = request.args.get('token', '')
    if qt:
        return qt
    return request.cookies.get('nas_token', '')


def get_current_user():
    """Return { username, role } for the current token, or None."""
    token = get_token()
    info = tokens.get(token)
    if info and info['expires'] > datetime.now():
        return {'username': info['username'], 'role': info['role']}
    return None


def _is_sudo_mode():
    """Return True if current user is an admin (always-on sudo for admins)."""
    token = get_token()
    info = tokens.get(token)
    return bool(info and info.get('role') == 'admin')


def make_user_cache_key(*args, **kwargs):
    """Generate cache key based on path, query, user, and sudo mode."""
    path = request.full_path  # includes query string
    user = get_current_user()
    username = user['username'] if user else 'anon'
    sudo = 'sudo' if _is_sudo_mode() else 'nosudo'
    return f"{path}:{username}:{sudo}"


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_token()
        info = tokens.get(token)
        if not info or info['expires'] < datetime.now():
            tokens.pop(token, None)
            return jsonify({'error': 'Unauthorized'}), 401
        # Update last activity for session idle timeout
        tokens.touch(token)
        return f(*args, **kwargs)
    return decorated


# ── Auth guard for blueprint routes ─────────────────────────────────────────

# Map API path prefixes to app IDs for privilege enforcement
_API_TO_APP = {
    '/api/storage/samba/': 'storage-manager',
    '/api/storage/nfs/': 'storage-manager',
    '/api/storage/dlna/': 'storage-manager',
    '/api/storage/webdav/': 'storage-manager',
    '/api/storage/sftp/': 'storage-manager',
    '/api/storage/ftp/': 'storage-manager',
    '/api/storage/': 'storage-manager',
    '/api/printer/': 'printer',
    '/api/resources/': 'resource-monitor',
    '/api/backup/': 'backup',
    '/api/packages/': 'packages',
    '/api/users/': 'users',
    '/api/network/': 'network',
    '/api/eventlog': 'event-log',
    '/api/docker/': 'docker-manager',
    '/api/vm/': 'vm-manager',
    '/api/appstore/': 'app-store',
    '/api/app-manager/': 'app-store',
    '/api/gallery/': 'gallery',
    '/api/files/duplicates/': 'duplicates',
    '/api/files/transfer-remote': 'naslink',
    '/api/files/': 'file-manager',
    '/api/photos/': 'file-manager',
    '/api/terminal/': 'terminal',
    '/api/editor/': 'doc-editor',
    '/api/code-editor/': 'code-editor',
    '/api/downloads/': 'download-manager',
    '/api/flasher/': 'usb-flasher',
    '/api/builder/': 'builder',
    '/api/services/': 'services',
    '/api/diskrepair/': 'storage-manager',
    '/api/surveillance/': 'surveillance',
    '/api/aichat/': 'ai-chat',
    '/api/doc-anonymizer/': 'doc-anonymizer',
    '/api/med-assistant/': 'med-assistant',
    '/api/ddns/': 'domains-manager',
    '/api/domains-mgr/': 'domains-manager',
    '/api/settings/': 'system-settings',
    '/api/ssh/': 'ssh-manager',
    '/api/notes/': 'sticky-notes',
    '/api/tickets/': 'tickets',
    '/api/familyhub/': 'family-hub',
    '/api/sync/': 'naslink',
    '/api/update/': 'updates',
    '/api/remote-log/': 'remote-log',
    '/api/websites/': 'websites',
    '/api/sandbox/': 'docker-manager',
    '/api/fail2ban/': 'fail2ban',
    '/api/wireguard/': 'wireguard',
    '/api/antivirus/': 'antivirus',
    '/api/power/': 'power',
    '/api/ups/': 'ups',
    '/api/cloud-backup/': 'cloud-backup',
    '/api/raid/': 'storage-manager',
    '/api/cron/': 'cron',
    '/api/dashboard/': 'dashboard',
    '/api/dlna/': 'storage-manager',
    '/api/notifications/': 'notifications',
    '/api/rollback/': 'rollback',
    '/api/totp/': 'system-settings',
    '/api/firewall/': 'firewall',
    '/api/encryption/': 'storage-manager',
    '/api/cache/': 'storage-manager',
    '/api/hardware/': 'system-settings',
    '/api/security-advisor/': 'security-advisor',
    '/api/security/': 'security-advisor',
    '/api/photos-ai/': 'photos-ai',
    '/api/video-station/': 'video-station',
}

# Admin-only apps — only role='admin' can access (matches admin_only: True in get_apps)
_ADMIN_ONLY_APPS = {
    'users', 'usb-flasher', 'builder', 'updates', 'services',
    'disk-repair', 'remote-log', 'surveillance',
    'system-settings', 'domains-manager', 'vm-manager', 'app-store',
    'fail2ban', 'wireguard', 'antivirus', 'power', 'ups', 'cloud-backup', 'rollback',
    'cron', 'security-advisor',
}

# ─── Role-based app access (3 roles: admin / user / family) ───
# admin  → ALL apps (no filtering)
# user   → work/productivity apps (everything except admin tools)
# family → safe subset for kids/guests
_ROLE_APPS = {
    'user': {
        'dashboard', 'file-manager', 'docker-manager', 'storage-manager',
        'backup', 'resource-monitor', 'printer', 'terminal',
        'packages', 'network', 'event-log', 'notifications', 'gallery',
        'duplicates', 'doc-editor', 'code-editor', 'download-manager',
        'naslink', 'ssh-manager', 'sticky-notes', 'tickets', 'family-hub',
        'ai-chat', 'cron',
    },
    'family': {
        'dashboard', 'file-manager', 'gallery', 'doc-editor',
        'sticky-notes', 'download-manager', 'family-hub',
    },
}


def _user_allowed_apps(username, role):
    """Return set of allowed app IDs for a user, or None meaning ALL.

    Role hierarchy: admin → all, user → _ROLE_APPS['user'], family → _ROLE_APPS['family'].
    Custom privileges from privileges.json override role defaults if configured.
    """
    if role == 'admin':
        return None  # all access
    import shlex as _shlex
    gr = _host_run_base(f"id -Gn {_shlex.quote(username)}", timeout=5)
    user_groups = gr.stdout.strip().split() if gr.returncode == 0 else []
    if 'sudo' in user_groups or 'root' in user_groups or 'ethos-admin' in user_groups:
        return None  # all access

    # Check for custom privilege overrides (from privileges.json)
    from blueprints.users import _load_privileges
    privileges = _load_privileges()
    custom_allowed = set()
    has_custom = False
    for ug in user_groups:
        if ug in privileges and privileges[ug]:
            has_custom = True
            custom_allowed.update(privileges[ug])
    if has_custom:
        custom_allowed.add('dashboard')
        return custom_allowed

    # Determine role from groups: ethos-family → family, else user
    if 'ethos-family' in user_groups:
        base = set(_ROLE_APPS.get('family', set()))
    else:
        base = set(_ROLE_APPS.get('user', set()))
    base.add('dashboard')
    return base


@auth_bp.before_app_request
def _blueprint_auth_guard():
    """Require auth on all blueprint API routes (except auth endpoints).
    Also enforce app-level privileges for non-admin users."""
    # Set g.username and g.role for per-user data isolation in blueprints
    g.username = None
    g.role = None
    g.sudo_mode = False
    _token = get_token()
    _tinfo = tokens.get(_token)
    if _tinfo and _tinfo['expires'] > datetime.now():
        g.username = _tinfo['username']
        g.role = _tinfo.get('role', 'user')
        g.sudo_mode = g.role == 'admin'

    path = request.path
    if path.startswith(('/api/storage/', '/api/printer/', '/api/resources/',
                        '/api/backup/', '/api/packages/', '/api/users/',
                        '/api/network/', '/api/eventlog', '/api/docker/',
                        '/api/appstore/', '/api/gallery/', '/api/files/',
                        '/api/photos/', '/api/sync/', '/api/terminal/',
                        '/api/editor/', '/api/code-editor/', '/api/downloads/',
                        '/api/diskrepair/', '/api/aichat/', '/api/doc-anonymizer/', '/api/ddns/',
                        '/api/domains-mgr/', '/api/settings/', '/api/ssh/',
                        '/api/flasher/', '/api/builder/', '/api/services/',
                        '/api/surveillance/', '/api/notes/', '/api/familyhub/', '/api/update/',
                        '/api/remote-log/', '/api/websites/', '/api/sandbox/',
                        '/api/fail2ban/', '/api/firewall/', '/api/wireguard/', '/api/antivirus/',
                        '/api/power/', '/api/ups/', '/api/totp/',
                        '/api/cloud-backup/', '/api/cron/', '/api/dashboard/',
                        '/api/dlna/', '/api/notifications/', '/api/raid/',
                        '/api/rollback/', '/api/tickets/', '/api/vm/',
                        '/api/installer/', '/api/med-assistant/',
                        '/api/encryption/', '/api/cache/', '/api/hardware/',
                        '/api/security-advisor/', '/api/security/',
                        '/api/photos-ai/', '/api/video-station/',
                        '/api/sync-drive/', '/api/radio-music/')):
        # Allow unauthenticated access to user auth validation
        if path == '/api/users/auth/validate':
            return
        # Public gallery share links (no auth)
        if path.startswith('/api/gallery/shared/'):
            return
        # noVNC static files (open-source UI, no sensitive data)
        if path.startswith('/api/vm/novnc/'):
            return
        # Internal system events (localhost only, e.g. from ticket watcher)
        if path == '/api/eventlog' and request.method == 'POST' and request.remote_addr in ('127.0.0.1', '::1'):
            return
        # Internal backup trigger from smartd ethos-notify hook
        if path == '/api/backup/trigger-smart' and request.method == 'POST' and request.remote_addr in ('127.0.0.1', '::1'):
            return
        # Internal RAG indexing endpoint (localhost only, used by cron)
        if path == '/api/aichat/rag/index-internal':
            return
        # Boot beacon (no auth — freshly booted EthOS VM sends this before having a token)
        if path == '/api/builder/beacon' and request.method == 'POST':
            return
        # Allow installer + network endpoints during setup wizard (no auth yet)
        if not _is_setup_done() and path.startswith(('/api/installer/',
                                                      '/api/network/wifi',
                                                      '/api/network/ap',
                                                      '/api/network/interfaces')):
            return
        token = get_token()
        info = tokens.get(token)
        if not info or info['expires'] < datetime.now():
            tokens.pop(token, None)
            return jsonify({'error': 'Unauthorized'}), 401

        # Enforce app-level privileges
        role = info.get('role', 'user')
        username = info.get('username', '')
        # Find which app this path belongs to (longest prefix wins)
        target_app = None
        best_len = 0
        for prefix, app_id in _API_TO_APP.items():
            if path.startswith(prefix) and len(prefix) > best_len:
                target_app = app_id
                best_len = len(prefix)

        if target_app:
            # Admin-only apps block non-admins
            if target_app in _ADMIN_ONLY_APPS and role != 'admin':
                return jsonify({'error': t('auth.no_permission')}), 403
            # Check privilege-based access
            allowed = _user_allowed_apps(username, role)
            if allowed is not None and target_app not in allowed:
                return jsonify({'error': t('auth.no_app_permission')}), 403


# ─── Endpoint rate limiter (in-memory, per-IP) ──────────────────────────────

class _EndpointRateLimiter:
    def __init__(self):
        self._attempts = _defaultdict(list)  # key -> [timestamps]

    def is_limited(self, key, max_attempts=5, window_secs=300):
        """Returns True if rate limited. Default: 5 attempts per 5 min."""
        now = time.time()
        self._attempts[key] = [ts for ts in self._attempts[key] if now - ts < window_secs]
        if len(self._attempts[key]) >= max_attempts:
            return True
        self._attempts[key].append(now)
        return False

    def reset(self, key):
        self._attempts.pop(key, None)


_rate_limiter = _EndpointRateLimiter()


# ─── Brute-force protection ──────────────────────────────────────────────────

def _record_failed_login(client_ip, username=None):
    """Record a failed login attempt and lock out IP/user if needed."""
    now = time.time()
    with _login_lock:
        # IP-level lockout
        attempt = _login_attempts.get(client_ip, {'count': 0, 'first': now, 'locked_until': 0})
        attempt['count'] += 1
        if attempt['count'] >= _MAX_ATTEMPTS:
            attempt['locked_until'] = now + _LOCKOUT_TIME
            attempt['count'] = 0
        _login_attempts[client_ip] = attempt
        # Per-user lockout (prevents distributed brute-force from rotating IPs)
        if username:
            ua = _user_login_attempts.get(username, {'count': 0, 'first': now, 'locked_until': 0})
            if now - ua['first'] > _ATTEMPT_WINDOW:
                ua = {'count': 0, 'first': now, 'locked_until': 0}
            ua['count'] += 1
            if ua['count'] >= _USER_MAX_ATTEMPTS:
                ua['locked_until'] = now + _USER_LOCKOUT_TIME
                ua['count'] = 0
                try:
                    from blueprints.notifications import notify_event
                    notify_event('auth', 'warning',
                                 f'Konto "{username}" zablokowane na {_USER_LOCKOUT_TIME // 60} min po {_USER_MAX_ATTEMPTS} nieudanych próbach logowania')
                except Exception:
                    pass
            _user_login_attempts[username] = ua


def _is_user_locked(username):
    """Return remaining lockout seconds if user account is locked, else 0."""
    if not username:
        return 0
    with _login_lock:
        ua = _user_login_attempts.get(username)
        if ua and time.time() < ua.get('locked_until', 0):
            return int(ua['locked_until'] - time.time())
    return 0


def _log_auth_failure(username, ip):
    try:
        auth_logger.warning(f'Failed login attempt for user {username} from {ip}')
    except Exception:
        pass


# ── Login notification (new device/IP detection) ────────────────────────────

_KNOWN_IPS_FILE = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'known_login_ips.json')


def _check_login_notification(username, client_ip, user_agent):
    """Send notification if login is from a previously unseen IP."""
    try:
        known = {}
        if os.path.exists(_KNOWN_IPS_FILE):
            with open(_KNOWN_IPS_FILE, 'r') as f:
                known = json.load(f)

        user_ips = known.get(username, [])
        is_new = client_ip not in user_ips

        if is_new:
            # Record this IP
            user_ips.append(client_ip)
            # Keep last 50 IPs per user
            known[username] = user_ips[-50:]
            os.makedirs(os.path.dirname(_KNOWN_IPS_FILE), exist_ok=True)
            with open(_KNOWN_IPS_FILE, 'w') as f:
                json.dump(known, f, indent=2)

            # Send notification
            from blueprints.notifications import send_notification, push_inbox
            ts = datetime.now().strftime('%Y-%m-%d %H:%M')
            msg = f'Nowe logowanie: {username} z IP {client_ip} ({ts})'
            if user_agent:
                short_ua = user_agent[:80]
                msg += f'\nPrzeglądarka: {short_ua}'
            push_inbox(f'Nowe logowanie: {username}', msg,
                       msg_type='warning', category='security')
            send_notification('EthOS: Nowe logowanie', msg, 'auth', 'info')
    except Exception:
        pass


# ─────────────────────────── Routes ─────────────────────────────────────────

@auth_bp.route('/login', methods=['POST'])
def login():
    client_ip = request.remote_addr or '0.0.0.0'
    if _rate_limiter.is_limited(f'login:{client_ip}'):
        return jsonify({"error": "Too many login attempts. Try again in 5 minutes."}), 429
    now = time.time()
    with _login_lock:
        attempt = _login_attempts.get(client_ip)
        if attempt:
            # Check lockout
            if now < attempt.get('locked_until', 0):
                remaining = int(attempt['locked_until'] - now)
                return jsonify({'error': t('auth.too_many_attempts', remaining=remaining)}), 429
            # Reset window if expired
            if now - attempt['first'] > _ATTEMPT_WINDOW:
                _login_attempts.pop(client_ip, None)
                attempt = None

    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    import shlex

    # Authenticate against /etc/shadow
    # If no username, find admin users (sudo/ethos-admin groups) and try their passwords
    if not username:
            # Get all users in sudo or ethos-admin groups
            r = _host_run_base("getent group sudo ethos-admin 2>/dev/null | cut -d: -f4 | tr ',' '\\n' | sort -u", timeout=10)
            admin_users = [u.strip() for u in (r.stdout or '').split('\n') if u.strip()]
            if not admin_users:
                admin_users = ['root']
            for try_user in admin_users:
                r = _host_run_base(f"getent shadow {shlex.quote(try_user)}", timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    fields = r.stdout.strip().split(':')
                    h = fields[1] if len(fields) > 1 else ''
                    if h and not h.startswith('!') and h != '*':
                        if _verify_shadow_hash(password, h):
                            username = try_user
                            break
            if not username:
                _record_failed_login(client_ip)
                _log_auth_failure('admin', client_ip)
                elog('system', 'warning', 'Failed login (wrong password)')
                audit_log('auth.login.failure', f'Failed login (no matching admin user) from {client_ip}', username='admin')
                return jsonify({'error': t('auth.invalid_credentials')}), 401

    # User login — validate against host /etc/shadow
    safe_user = re.sub(r'[^a-zA-Z0-9_.-]', '', username)

    # Check per-user account lockout (distributed brute-force protection)
    user_locked = _is_user_locked(safe_user)
    if user_locked:
        return jsonify({'error': t('auth.too_many_attempts', remaining=user_locked)}), 429

    r = _host_run_base(f"getent shadow {shlex.quote(safe_user)}", timeout=10)
    if r.returncode != 0 or not r.stdout.strip():
        # User not in shadow — try LDAP/AD if configured
        _ldap_role = None
        try:
            from blueprints.ldap_auth import try_ldap_auth
            _ldap_role = try_ldap_auth(safe_user, password)
        except ImportError:
            pass
        except Exception:
            pass

        if _ldap_role:
            # LDAP auth succeeded — create token directly
            _login_attempts.pop(client_ip, None)
            with _login_lock:
                _user_login_attempts.pop(safe_user, None)
            _rate_limiter.reset(f'login:{client_ip}')
            token = generate_token(safe_user, _ldap_role)
            home_path = _get_user_home(safe_user)
            _ensure_user_home_structure(safe_user)
            elog('system', 'info', f'Logowanie LDAP: {safe_user} (rola: {_ldap_role})')
            audit_log('auth.login.success', f'LDAP user "{safe_user}" logged in (role: {_ldap_role}) from {client_ip}', username=safe_user)
            _check_login_notification(safe_user, client_ip, request.headers.get('User-Agent', ''))
            csrf_token = secrets.token_hex(32)
            gr = _host_run_base(f"id -Gn {shlex.quote(safe_user)}", timeout=5)
            groups = gr.stdout.strip().split() if gr.returncode == 0 else []
            NAS_NAME = _get_nas_name()
            BRAND_NAME = _get_brand_name()
            resp = jsonify({
                'token': token, 'nas_name': NAS_NAME, 'brand_name': BRAND_NAME,
                'user': {'username': safe_user, 'role': _ldap_role, 'groups': groups,
                         'home_path': home_path},
                'sudo_mode': _ldap_role == 'admin',
                'password_change_required': False,
                'csrf_token': csrf_token,
                'ldap_user': True,
            })
            _secure = request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure
            resp.set_cookie('nas_token', token, max_age=7 * 24 * 3600,
                            httponly=True, samesite='Strict', secure=_secure)
            resp.set_cookie('csrf_token', csrf_token, max_age=7 * 24 * 3600,
                            httponly=False, samesite='Strict', secure=_secure)
            return resp

        _record_failed_login(client_ip, safe_user)
        _log_auth_failure(safe_user, client_ip)
        audit_log('auth.login.failure', f'Unknown user "{safe_user}" from {client_ip}', username=safe_user)
        return jsonify({'error': 'Invalid username or password'}), 401

    shadow_fields = r.stdout.strip().split(':')
    stored_hash = shadow_fields[1] if len(shadow_fields) > 1 else ''
    if not stored_hash or stored_hash.startswith('!') or stored_hash == '*':
        audit_log('auth.login.failure', f'Locked account "{safe_user}" from {client_ip}', username=safe_user)
        return jsonify({'error': 'Account locked'}), 401

    if not _verify_shadow_hash(password, stored_hash):
        # Shadow auth failed — try LDAP/AD if configured
        _ldap_role = None
        try:
            from blueprints.ldap_auth import try_ldap_auth
            _ldap_role = try_ldap_auth(safe_user, password)
        except ImportError:
            pass
        except Exception:
            pass

        if not _ldap_role:
            _record_failed_login(client_ip, safe_user)
            _log_auth_failure(safe_user, client_ip)
            audit_log('auth.login.failure', f'Bad password for "{safe_user}" from {client_ip}', username=safe_user)
            return jsonify({'error': 'Invalid username or password'}), 401

    # Clear login attempts on success (both IP and per-user)
    _login_attempts.pop(client_ip, None)
    with _login_lock:
        _user_login_attempts.pop(safe_user, None)
    _rate_limiter.reset(f'login:{client_ip}')

    # ─── TOTP / 2FA check ───
    from blueprints.totp import is_totp_enabled, verify_totp_code, verify_backup_code
    if is_totp_enabled(safe_user):
        totp_code = str(data.get('totp_code', '')).strip()
        backup_code = str(data.get('backup_code', '')).strip()
        if not totp_code and not backup_code:
            return jsonify({'totp_required': True}), 200
        totp_ok = False
        if totp_code:
            totp_ok = verify_totp_code(safe_user, totp_code)
        if not totp_ok and backup_code:
            totp_ok = verify_backup_code(safe_user, backup_code)
        if not totp_ok:
            _log_auth_failure(safe_user, client_ip)
            audit_log('auth.login.failure', f'Invalid 2FA code for "{safe_user}" from {client_ip}', username=safe_user)
            return jsonify({'error': 'Invalid 2FA code'}), 401

    # Password OK — determine role
    gr = _host_run_base(f"id -Gn {shlex.quote(safe_user)}", timeout=5)
    groups = gr.stdout.strip().split() if gr.returncode == 0 else []
    role = 'admin' if ('sudo' in groups or 'root' in groups or safe_user == 'root' or 'ethos-admin' in groups) else 'user'
    token = generate_token(safe_user, role)
    home_path = _get_user_home(safe_user)
    # Ensure default folders exist in the user's home
    _ensure_user_home_structure(safe_user)
    elog('system', 'info', f'Logowanie: {safe_user} (rola: {role})')
    audit_log('auth.login.success', f'User "{safe_user}" logged in (role: {role}) from {client_ip}', username=safe_user)

    # Login notification — alert on new IP/device
    _check_login_notification(safe_user, client_ip, request.headers.get('User-Agent', ''))

    pwd_change_required = _is_setup_done() and not os.path.exists(PASSWORD_CHANGED_MARKER)

    # 1. Generate CSRF token
    csrf_token = secrets.token_hex(32)

    NAS_NAME = _get_nas_name()
    BRAND_NAME = _get_brand_name()
    resp = jsonify({
        'token': token, 'nas_name': NAS_NAME, 'brand_name': BRAND_NAME,
        'user': {'username': safe_user, 'role': role, 'groups': groups,
                 'home_path': home_path},
        'sudo_mode': role == 'admin',
        'password_change_required': pwd_change_required,
        'csrf_token': csrf_token  # Expose to frontend for API helper
    })

    # 5. Add SameSite=Strict to cookie policy
    # Set Secure flag when behind HTTPS reverse proxy
    _secure = request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure
    resp.set_cookie('nas_token', token, max_age=7 * 24 * 3600,
                    httponly=True, samesite='Strict', secure=_secure)

    # Set CSRF cookie (JS readable, Strict)
    resp.set_cookie('csrf_token', csrf_token, max_age=7 * 24 * 3600,
                    httponly=False, samesite='Strict', secure=_secure)

    return resp


@auth_bp.route('/verify')
def verify():
    token = get_token()
    info = tokens.get(token)
    if info and info['expires'] > datetime.now():
        home_path = _get_user_home(info['username'])
        pwd_change_required = _is_setup_done() and not os.path.exists(PASSWORD_CHANGED_MARKER)

        # Ensure CSRF token is present/refreshed
        csrf_token = request.cookies.get('csrf_token') or secrets.token_hex(32)

        NAS_NAME = _get_nas_name()
        BRAND_NAME = _get_brand_name()
        resp = jsonify({
            'valid': True,
            'nas_name': NAS_NAME, 'brand_name': BRAND_NAME,
            'user': {'username': info['username'], 'role': info['role'],
                     'home_path': home_path},
            'sudo_mode': info.get('role') == 'admin',
            'password_change_required': pwd_change_required,
            'csrf_token': csrf_token
        })

        # Refresh cookie if missing or just to be safe
        _secure = request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure
        resp.set_cookie('csrf_token', csrf_token, max_age=7 * 24 * 3600,
                        httponly=False, samesite='Strict', secure=_secure)
        return resp

    return jsonify({'valid': False}), 401


@auth_bp.route('/logout', methods=['POST'])
def logout():
    user = get_current_user()
    logout_user = user['username'] if user else 'unknown'
    tokens.pop(get_token(), None)
    audit_log('auth.logout', f'User "{logout_user}" logged out', username=logout_user)
    resp = jsonify({'ok': True})
    resp.delete_cookie('nas_token')
    resp.delete_cookie('csrf_token')
    return resp


@auth_bp.route('/sudo', methods=['POST', 'GET'])
@require_auth
def get_sudo_status():
    """Sudo mode is always-on for admin users. Kept for API compat."""
    return jsonify({'ok': True, 'sudo_mode': _is_sudo_mode()})


# ── Security settings API (alert thresholds, session timeout, lockout) ───────

_SECURITY_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'security_settings.json')


def _load_security_settings():
    defaults = {
        'session_idle_timeout': SESSION_IDLE_TIMEOUT,
        'user_lockout_attempts': _USER_MAX_ATTEMPTS,
        'user_lockout_duration': _USER_LOCKOUT_TIME,
        'login_notifications': True,
        'alert_thresholds': {
            'enabled': True, 'cpu': 90, 'ram': 90, 'disk': 90, 'temp': 80,
        },
    }
    try:
        if os.path.exists(_SECURITY_SETTINGS_FILE):
            with open(_SECURITY_SETTINGS_FILE, 'r') as f:
                defaults.update(json.load(f))
    except Exception:
        pass
    return defaults


@security_bp.route('/settings', methods=['GET'])
@require_auth
def get_security_settings():
    if not _is_sudo_mode():
        return jsonify({'error': 'Admin only'}), 403
    return jsonify(_load_security_settings())


@security_bp.route('/settings', methods=['POST'])
@require_auth
def set_security_settings():
    global SESSION_IDLE_TIMEOUT, _USER_MAX_ATTEMPTS, _USER_LOCKOUT_TIME
    if not _is_sudo_mode():
        return jsonify({'error': 'Admin only'}), 403
    data = request.get_json(silent=True) or {}
    settings = _load_security_settings()
    # Update allowed fields
    if 'session_idle_timeout' in data:
        val = max(300, min(86400, int(data['session_idle_timeout'])))  # 5min..24h
        settings['session_idle_timeout'] = val
        SESSION_IDLE_TIMEOUT = val
    if 'user_lockout_attempts' in data:
        settings['user_lockout_attempts'] = max(3, min(20, int(data['user_lockout_attempts'])))
        _USER_MAX_ATTEMPTS = settings['user_lockout_attempts']
    if 'user_lockout_duration' in data:
        val = max(60, min(86400, int(data['user_lockout_duration'])))
        settings['user_lockout_duration'] = val
        _USER_LOCKOUT_TIME = val
    if 'login_notifications' in data:
        settings['login_notifications'] = bool(data['login_notifications'])
    if 'alert_thresholds' in data:
        at = data['alert_thresholds']
        t_cfg = settings.get('alert_thresholds', {})
        for k in ('enabled', 'cpu', 'ram', 'disk', 'temp'):
            if k in at:
                t_cfg[k] = at[k] if k == 'enabled' else max(50, min(99, int(at[k])))
        settings['alert_thresholds'] = t_cfg
    os.makedirs(os.path.dirname(_SECURITY_SETTINGS_FILE), exist_ok=True)
    with open(_SECURITY_SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)
    # Also write alert thresholds to the file the resource alert loop reads
    alert_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'alert_thresholds.json')
    with open(alert_path, 'w') as f:
        json.dump(settings.get('alert_thresholds', {}), f, indent=2)
    audit_log('security.settings', 'Security settings updated', username=get_current_user().get('username', 'admin'))
    return jsonify({'ok': True})


@security_bp.route('/account-lockouts', methods=['GET'])
@require_auth
def get_account_lockouts():
    """List currently locked out accounts (admin only)."""
    if not _is_sudo_mode():
        return jsonify({'error': 'Admin only'}), 403
    now = time.time()
    locked = []
    with _login_lock:
        for username, ua in _user_login_attempts.items():
            remaining = ua.get('locked_until', 0) - now
            if remaining > 0:
                locked.append({'username': username, 'remaining': int(remaining)})
    return jsonify({'locked': locked})


@security_bp.route('/unlock-account', methods=['POST'])
@require_auth
def unlock_account():
    """Manually unlock a locked user account (admin only)."""
    if not _is_sudo_mode():
        return jsonify({'error': 'Admin only'}), 403
    data = request.get_json(silent=True) or {}
    username = data.get('username', '')
    if not username:
        return jsonify({'error': 'Username required'}), 400
    with _login_lock:
        _user_login_attempts.pop(username, None)
    audit_log('security.unlock', f'Account "{username}" unlocked by admin',
              username=get_current_user().get('username', 'admin'))
    return jsonify({'ok': True})
