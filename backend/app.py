"""
EthOS — Synology DSM-inspired NAS Operating System
Backend API Server
"""

from gevent import monkey
monkey.patch_all()

from flask_compress import Compress
from flask import Flask, request, jsonify, send_from_directory, send_file, g, make_response
from flask_caching import Cache
from flask_socketio import SocketIO, emit
import os
import json
import re
import time
import secrets
import subprocess
import shutil
import psutil
import pty
import select
import struct
import fcntl
import termios
import signal
import errno
import hashlib
import pwd
import grp as _grp
import stat as _stat_mod
from datetime import datetime, timedelta
from functools import wraps
import gevent
import gevent.os
import gevent.select
from PIL import Image
import io
import logging
from logging.handlers import RotatingFileHandler
from i18n import t

# Setup Auth Logger for Fail2Ban
AUTH_LOG_FILE = '/opt/ethos/logs/auth.log'
auth_logger = logging.getLogger('ethos_auth')
auth_logger.setLevel(logging.INFO)
if not auth_logger.handlers:
    try:
        os.makedirs(os.path.dirname(AUTH_LOG_FILE), exist_ok=True)
        handler = RotatingFileHandler(AUTH_LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        auth_logger.addHandler(handler)
    except Exception:
        pass

# Setup Access Logger for Fail2Ban
ACCESS_LOG_FILE = '/opt/ethos/logs/access.log'
access_logger = logging.getLogger('ethos_access')
access_logger.setLevel(logging.INFO)
if not access_logger.handlers:
    try:
        os.makedirs(os.path.dirname(ACCESS_LOG_FILE), exist_ok=True)
        handler = RotatingFileHandler(ACCESS_LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        access_logger.addHandler(handler)
    except Exception:
        pass

from crypto_utils import hash_folder_password as _hash_folder_password_new, verify_folder_password as _verify_folder_password

# Host abstraction layer
import sys as _sys
_sys.path.insert(0, os.path.dirname(__file__))
from host import NATIVE_MODE, host_run as _host_run_base, host_run_stream as _host_run_stream_base, nsenter_args, \
    app_path as _app_path, data_path as _data_path, user_data_path as _user_data_path, ETHOS_ROOT, \
    get_data_disk as _get_data_disk, get_user_home as _get_user_home, ensure_user_home_structure as _ensure_user_home_structure, \
    get_photo_folders as _get_photo_folders, fs_call_with_timeout as _fs_call
from utils import load_json as _load_json, save_json as _save_json, \
    safe_path as _safe_path_util, fmt_bytes, DATA_ROOT, ALLOWED_ROOTS as _ALLOWED_ROOTS, \
    generate_thumbnail, THUMB_CACHE_DIR, THUMBS_DIR_NAME, \
    _thumb_cache_key, _local_thumb_path, list_directory as _list_dir, \
    systemd_notify_ready

from middleware.rate_limiter import RateLimiter

from blueprints.monitor import (
    get_cpu_info as _mon_cpu, get_ram_info as _mon_ram,
    get_disk_info as _mon_disk, get_system_info as _mon_sys,
)

# Blueprints
from blueprints.storage import storage_bp, init_storage, get_usb_notifications, usb_monitor_loop, keepalive_loop, try_wake_path
from blueprints.resources import resources_bp, resources_background_collector
from blueprints.resources_db import init_db as init_resources_db
from blueprints.backup import backup_bp, init_backup, get_backup_notifications
from blueprints.packages import packages_bp
from blueprints.users import users_bp, _load_privileges
from blueprints.network import network_bp
from blueprints.eventlog import eventlog_bp, init_eventlog, log as elog
from audit import audit_log
from blueprints.sandbox_policy import sandbox_bp
from blueprints.updater import update_bp, updates_public_bp, init_update, update_auto_check_loop
from blueprints.ddns import ddns_bp, start_ddns
from blueprints.settings import settings_bp
from blueprints.ssh_manager import ssh_bp
from blueprints.installer import installer_bp
try:
    from blueprints.ups import _ups_status
except ImportError:
    _ups_status = lambda: {}
from blueprints.power import power_bp
from blueprints.encryption import encryption_bp
from blueprints.ssd_cache import ssd_cache_bp
from blueprints.hardware import hardware_bp
from blueprints.notifications import notifications_bp, init_notifications
from blueprints.dashboard import dashboard_bp
from blueprints.admin_required import admin_required
from blueprints.totp import totp_bp, is_totp_enabled, verify_totp_code, verify_backup_code
from blueprints.security_advisor import security_advisor_bp
from blueprints.api_docs import api_docs_bp
from blueprints.app_manager import (
    app_manager_bp, init_app_manager, migrate_from_ethos_packages,
    CORE_APPS as _APP_MANAGER_CORE_APPS,
    BUILTIN_CATALOG as _BUILTIN_CATALOG,
    load_installed as _load_app_manager_installed,
    load_optional_blueprints as _load_optional_blueprints,
    OPTIONAL_BLUEPRINTS as _OPTIONAL_BLUEPRINTS,
)

# ── Shadow password verification (avoids crypt DeprecationWarning) ──
import warnings as _warnings
def _verify_shadow_hash(password: str, stored_hash: str) -> bool:
    """Verify password against /etc/shadow hash without DeprecationWarning."""
    with _warnings.catch_warnings():
        _warnings.filterwarnings('ignore', category=DeprecationWarning)
        import crypt as _crypt_mod
        return _crypt_mod.crypt(password, stored_hash) == stored_hash

# ─────────────────────────── App Setup ───────────────────────────

# Determine static folder (use dist if available)
_static_folder = '../frontend'
if os.path.exists(os.path.join(os.path.dirname(__file__), '../frontend_dist')):
    _static_folder = '../frontend_dist'

app = Flask(__name__, static_folder=_static_folder, static_url_path='/~static~')

# Unique SECRET_KEY per installation — generated once, persisted
_secret_key_path = os.path.join(os.path.dirname(__file__), '..', 'data', '.flask_secret')
if os.path.exists(_secret_key_path):
    with open(_secret_key_path) as _skf:
        app.secret_key = _skf.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(_secret_key_path), exist_ok=True)
        with open(_secret_key_path, 'w') as _skf:
            _skf.write(app.secret_key)
        os.chmod(_secret_key_path, 0o600)
    except OSError:
        pass

Compress(app)
app.config['COMPRESS_ALGORITHM'] = ['brotli', 'gzip', 'deflate']
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 year

PASSWORD_CHANGED_MARKER = '/opt/ethos/.password_changed'

@app.before_request
def check_password_change():
    """Enforce password change on first run."""
    # Skip for static files
    if request.path.startswith('/~static~') or request.endpoint == 'static':
        return

    # If setup is not done, allow setup-related endpoints
    if not _is_setup_done():
        return

    # If password changed marker exists, we are good
    if os.path.exists(PASSWORD_CHANGED_MARKER):
        return

    # Allowed endpoints for password change flow
    allowed = [
        '/api/auth/login',
        '/api/auth/logout',
        '/api/auth/verify',     # Used to check status
        '/api/auth/change-password', # The fix
        '/api/settings/change-password', # Alias? Check where it is
        '/api/setup/status',    # Needed for frontend logic
        '/api/setup/timezones', # Setup wizard data
        '/api/setup/locales',   # Setup wizard data
        '/api/setup/languages', # Firstboot i18n
        '/api/setup/translations/', # Firstboot i18n
        '/api/system/info',     # Often used by UI on load
        '/api/language',        # Needed for UI
    ]

    # Check if path starts with any allowed prefix
    if any(request.path.startswith(p) for p in allowed):
        return

    # Block everything else with specific code for frontend to catch
    return jsonify({'error': t('auth.password_change_required'), 'code': 'PASSWORD_CHANGE_REQUIRED'}), 403

@app.after_request
def add_header(response):
    # Add Cache-Control headers
    if request.path.startswith('/~static~') or request.path.endswith('.js') or request.path.endswith('.css') or request.path.endswith('.png') or request.path.endswith('.jpg') or request.path.endswith('.woff2'):
        # Assets: Cache for 1 year
        response.cache_control.max_age = 31536000
        response.cache_control.public = True
    elif request.path == '/' or request.path == '/index.html':
        # HTML: No cache (always revalidate)
        response.cache_control.no_cache = True
        response.cache_control.must_revalidate = True
        response.cache_control.max_age = 0

    # Log access for Fail2Ban (ethos-web jail)
    # Format: <HOST> - - [dd/MMM/yyyy:HH:mm:ss +0000] "GET /foo HTTP/1.1" 401 123 "-" "UserAgent"
    if not request.path.startswith('/~static~'):
        try:
            now = datetime.now().strftime('%d/%b/%Y:%H:%M:%S +0000') # Simplified UTC for now
            ip = request.remote_addr
            method = request.method
            path = request.full_path if request.query_string else request.path
            status = response.status_code
            length = response.content_length or 0
            ua = request.user_agent.string
            # Check if access_logger is defined (it should be)
            if 'access_logger' in globals():
                msg = f'{ip} - - [{now}] "{method} {path} HTTP/1.1" {status} {length} "-" "{ua}"'
                access_logger.info(msg)
        except Exception:
            pass

    return response
cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache'})
# Security: DDOS protection (5 req/sec per IP)
limiter = RateLimiter(app, limit=300, window=60)

# CSRF Protection
# 1. Generate CSRF token in auth module (done in login/verify)
# 2. Middleware checking token on state-changing requests
# 4. Exclude CSRF for API calls with Bearer token
@app.before_request
def csrf_check():
    # Skip safe methods
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return

    # Exclude Login (initial auth)
    if request.path == '/api/auth/login':
        return

    # Allow localhost (internal services like smartd, fail2ban)
    if request.remote_addr in ('127.0.0.1', '::1'):
        return

    # Exclude Bearer token requests (CLI, Watcher, Frontend with token)
    if request.headers.get('Authorization', '').startswith('Bearer '):
        return

    # Check for CSRF token in header and cookie (Double Submit Cookie)
    cookie_token = request.cookies.get('csrf_token')
    header_token = request.headers.get('X-CSRFToken')

    if not cookie_token or not header_token or cookie_token != header_token:
        # If authenticated via cookie (nas_token), this is a CSRF attempt
        if request.cookies.get('nas_token'):
            auth_logger.warning(f'CSRF mismatch from {request.remote_addr}: cookie={cookie_token}, header={header_token}')
            return jsonify({'error': t('auth.csrf_failed')}), 403

        # If not authenticated, we still enforce CSRF for consistency, unless it's a public endpoint.
        # But most endpoints are protected. If we block here, we return 403.
        # If we let it pass, the auth check will fail (401).
        # Better to fail with CSRF error (403).
        return jsonify({'error': t('auth.csrf_missing')}), 403

# Security Headers (SameSite=Strict, CSP, etc.)
@app.after_request
def add_security_headers(response):
    # 5. Add SameSite=Strict to cookie policy
    # We can't easily modify existing Set-Cookie headers here without parsing,
    # so we rely on setting samesite='Strict' when creating cookies (login/verify).
    # However, we can add other security headers here.

    # CSP from previous attempt (kept for security)
    csp_frame_ancestors = "frame-ancestors 'none';"
    x_frame_options = 'DENY'

    # Exception for Website Builder Preview
    if request.path.startswith('/api/websites/') and '/preview' in request.path:
        csp_frame_ancestors = "frame-ancestors 'self';"
        x_frame_options = 'SAMEORIGIN'

    # Exception for Document Anonymizer inline preview
    if request.path.startswith('/api/doc-anonymizer/preview/'):
        csp_frame_ancestors = "frame-ancestors 'self';"
        x_frame_options = 'SAMEORIGIN'

    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://www.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob: https:; "
        "connect-src 'self' ws: wss: https:; "
        "worker-src 'self' blob:; "
        "frame-src 'self'; "
        f"{csp_frame_ancestors} "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )

    response.headers['Content-Security-Policy'] = csp
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = x_frame_options
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    return response

app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 * 1024  # 50 GB upload limit
# No CORS — frontend served from same origin; no cross-origin access needed
socketio = SocketIO(app, async_mode='gevent')  # default: same-origin only


# ── Production error handlers — never leak internals ──
@app.errorhandler(404)
def _handle_404(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found'}), 404
    return _serve_index_response()

@app.errorhandler(405)
def _handle_405(e):
    return jsonify({'error': 'Method not allowed'}), 405

@app.errorhandler(413)
def _handle_413(e):
    return jsonify({'error': 'File too large'}), 413

@app.errorhandler(500)
def _handle_500(e):
    return jsonify({'error': 'Internal server error'}), 500


# Register blueprints
app.register_blueprint(storage_bp)
init_storage(socketio)
app.register_blueprint(resources_bp)
app.register_blueprint(backup_bp)
app.register_blueprint(packages_bp)
app.register_blueprint(users_bp)
app.register_blueprint(network_bp)
app.register_blueprint(eventlog_bp)
app.register_blueprint(sandbox_bp)
app.register_blueprint(update_bp)
app.register_blueprint(updates_public_bp)
app.register_blueprint(ddns_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(ssh_bp)
app.register_blueprint(installer_bp)
app.register_blueprint(power_bp, url_prefix='/api/power')
app.register_blueprint(encryption_bp)
app.register_blueprint(ssd_cache_bp)
app.register_blueprint(hardware_bp)
app.register_blueprint(notifications_bp)
app.register_blueprint(totp_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(api_docs_bp)
app.register_blueprint(app_manager_bp)
app.register_blueprint(security_advisor_bp)
init_app_manager(socketio)
_load_optional_blueprints(app, socketio)
migrate_from_ethos_packages()
init_update(socketio)

# ── Migrate data from app_path → data_path (one-time, for existing installs) ──
def _migrate_app_data():
    """Move stickynotes, ssh_keys, thumb_cache from ETHOS_ROOT/ to ETHOS_ROOT/data/.
    Also handles gabbyos→ethos env file rename for upgrades from GabbyOS."""
    import logging
    _log = logging.getLogger('migrate')
    # Rename legacy gabbyos.env → ethos.env
    old_env = _app_path('gabbyos.env')
    new_env = _app_path('ethos.env')
    if os.path.isfile(old_env) and not os.path.isfile(new_env):
        try:
            os.rename(old_env, new_env)
            _log.info('Renamed gabbyos.env → ethos.env')
        except OSError as exc:
            _log.warning('Could not rename gabbyos.env: %s', exc)
    migrations = [
        ('stickynotes.json', _app_path('stickynotes.json'), _data_path('stickynotes.json')),
        ('ssh_keys',         _app_path('ssh_keys'),          _data_path('ssh_keys')),
        ('.thumb_cache',     _app_path('.thumb_cache'),       _data_path('.thumb_cache')),
    ]
    for label, old, new in migrations:
        if not os.path.exists(old):
            continue
        try:
            if not os.path.exists(new):
                shutil.move(old, new)
                _log.info('Migrated %s → %s', label, new)
            elif os.path.isdir(old) and os.path.isdir(new):
                # Merge contents when new dir already exists (e.g. created at import time)
                for item in os.listdir(old):
                    src = os.path.join(old, item)
                    dst = os.path.join(new, item)
                    if not os.path.exists(dst):
                        shutil.move(src, dst)
                    elif os.path.isdir(src) and os.path.isdir(dst):
                        # Recursively merge subdirs
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                        shutil.rmtree(src)
                # Remove old dir if now empty
                try:
                    shutil.rmtree(old)
                except OSError:
                    pass
                _log.info('Merged %s → %s', label, new)
        except Exception as exc:
            _log.warning('Could not migrate %s: %s', label, exc)
_migrate_app_data()

NAS_NAME = os.environ.get('NAS_NAME', 'EthOS')
BRAND_NAME = os.environ.get('BRAND_NAME', 'EthOS')
PORT = int(os.environ.get('PORT', '9000'))
SETUP_DONE_FILE = _data_path('setup_done')

# ─── Version ───
VERSION_FILE = os.path.join(os.path.dirname(__file__), 'version.json')
def _load_version():
    try:
        with open(VERSION_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'version': '0.0.0', 'changelog': []}

ETHOS_VERSION = _load_version()


# ─── Per-user data migration ────────────────────────────────
def _migrate_global_to_per_user():
    """One-time migration: copy global data files to per-user files for the first admin.
    Detects admin user from sudo/ethos-admin group on the host system."""
    marker = _data_path('.per_user_migrated')
    if os.path.isfile(marker):
        return
    # Find the admin user
    try:
        import subprocess as _sp
        r = _sp.run('getent group sudo ethos-admin 2>/dev/null | cut -d: -f4 | tr "," "\\n" | sort -u',
                     shell=True, capture_output=True, text=True, timeout=5)
        admins = [u.strip() for u in (r.stdout or '').splitlines() if u.strip()]
        admin_user = admins[0] if admins else None
    except Exception:
        admin_user = None
    if not admin_user:
        # Fallback: check /home for the first directory
        try:
            admin_user = next(d for d in sorted(os.listdir('/home'))
                              if os.path.isdir(os.path.join('/home', d)) and not d.startswith('.'))
        except (StopIteration, OSError):
            admin_user = None
    if not admin_user:
        return
    # Files to migrate: global filename -> per-user filename
    _files_to_migrate = [
        'favorites.json',
        'dismissed_notifications.json',
        'gallery_favorites.json',
        'gallery_folders.json',
        'gallery_albums.json',
        'downloads_config.json',
    ]
    migrated = 0
    for fn in _files_to_migrate:
        src = _data_path(fn)
        dst = _user_data_path(fn, admin_user)
        if os.path.isfile(src) and not os.path.isfile(dst):
            try:
                import shutil
                shutil.copy2(src, dst)
                migrated += 1
            except Exception:
                pass
    # Write migration marker
    try:
        with open(marker, 'w') as f:
            f.write(f'migrated {migrated} files for user {admin_user}\n')
    except Exception:
        pass

_migrate_global_to_per_user()


@app.after_request
def _no_cache_api(response):
    """Prevent browser caching on all /api/ responses (except media streams and file downloads)."""
    if request.path.startswith('/api/'):
        # Allow caching for media preview (needed for video seeking / Range requests)
        is_media = request.path in ('/api/files/preview', '/api/files/trash/preview', '/api/gallery/stream') or \
                   request.path.endswith('/preview') and '/api/public/share/' in request.path or \
                   request.path.startswith('/api/video-station/stream/') or \
                   request.path.startswith('/api/video-station/transcode/') or \
                   request.path.startswith('/api/video-station/hls/') or \
                   request.path.startswith('/api/video-station/thumb/') or \
                   request.path.startswith('/api/video-station/poster/') or \
                   request.path.startswith('/api/video-station/backdrop/') or \
                   request.path.startswith('/api/video-station/thumbstrip/') or \
                   request.path == '/api/radio-music/radio/proxy' or \
                   request.path == '/api/radio-music/music/stream' or \
                   request.path == '/api/radio-music/local/stream'
        if is_media and response.status_code in (200, 206):
            ct = response.content_type or ''
            if ct.startswith(('video/', 'audio/', 'image/', 'application/vnd.apple.mpegurl')):
                # Transcode/HLS/radio proxy streams — don't advertise byte-range support
                _no_ranges = (request.path.startswith(('/api/video-station/transcode/', '/api/video-station/hls/'))
                              or request.path == '/api/radio-music/radio/proxy')
                if not _no_ranges:
                    response.headers['Accept-Ranges'] = 'bytes'
                response.headers.pop('Pragma', None)
                return response
        # Allow caching for file downloads so browsers can use Range requests to resume
        is_download = (request.path == '/api/files/download' or
                       request.path.startswith('/api/files/download-zip/') and
                       not request.path.endswith('/status'))
        if is_download and response.status_code in (200, 206):
            response.headers.pop('Pragma', None)
            return response
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response

# ─────────────────────────── Auth ───────────────────────────

TOKEN_EXPIRY = timedelta(days=7)
SESSION_IDLE_TIMEOUT = 1800  # 30 minutes idle → expire (configurable)
_tokens_lock = __import__('threading').Lock()


class _TokenStore:
    """SQLite-backed token store shared across gunicorn workers.

    Exposes dict-like .get(), .pop(), .items(), [] set/get so existing
    code works without changes.
    """

    def __init__(self):
        import sqlite3 as _sql
        self._db_path = os.path.join(
            os.path.dirname(__file__), '..', 'data', 'tokens.db'
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
                'expires': datetime.fromtimestamp(row[2]),
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
            (r[0], {'username': r[1], 'role': r[2], 'expires': datetime.fromtimestamp(r[3])})
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
    _sec_file = os.path.join(os.path.dirname(__file__), '..', 'data', 'security_settings.json')
    if os.path.exists(_sec_file):
        with open(_sec_file, 'r') as _sf:
            _sec_cfg = json.load(_sf)
        SESSION_IDLE_TIMEOUT = _sec_cfg.get('session_idle_timeout', SESSION_IDLE_TIMEOUT)
        _USER_MAX_ATTEMPTS = _sec_cfg.get('user_lockout_attempts', _USER_MAX_ATTEMPTS)
        _USER_LOCKOUT_TIME = _sec_cfg.get('user_lockout_duration', _USER_LOCKOUT_TIME)
except Exception:
    pass


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


# ── Auth guard for blueprint routes ──

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


@app.before_request
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
                        '/api/photos-ai/', '/api/video-station/')):
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
        # Allow network WiFi/AP endpoints during setup wizard (no auth yet)
        if not _is_setup_done() and path.startswith(('/api/network/wifi',
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


# ─── Endpoint rate limiter (in-memory, per-IP) ───
from collections import defaultdict as _defaultdict

class _EndpointRateLimiter:
    def __init__(self):
        self._attempts = _defaultdict(list)  # key -> [timestamps]

    def is_limited(self, key, max_attempts=5, window_secs=300):
        """Returns True if rate limited. Default: 5 attempts per 5 min."""
        now = time.time()
        self._attempts[key] = [t for t in self._attempts[key] if now - t < window_secs]
        if len(self._attempts[key]) >= max_attempts:
            return True
        self._attempts[key].append(now)
        return False

    def reset(self, key):
        self._attempts.pop(key, None)

_rate_limiter = _EndpointRateLimiter()


# ─── Brute-force protection ───
_login_attempts = {}  # ip -> {'count': int, 'first': float, 'locked_until': float}
_user_login_attempts = {}  # username -> {'count': int, 'first': float, 'locked_until': float}
_login_lock = __import__('threading').Lock()
_MAX_ATTEMPTS = 5
_ATTEMPT_WINDOW = 300   # 5 minutes
_LOCKOUT_TIME = 300     # 5 minute lockout after max attempts
_USER_MAX_ATTEMPTS = 5
_USER_LOCKOUT_TIME = 1800  # 30 minute lockout per account


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


# ── Login notification (new device/IP detection) ──
_KNOWN_IPS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'known_login_ips.json')


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

@app.route('/api/auth/login', methods=['POST'])
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


@app.route('/api/auth/verify')
def verify():
    token = get_token()
    info = tokens.get(token)
    if info and info['expires'] > datetime.now():
        home_path = _get_user_home(info['username'])
        pwd_change_required = _is_setup_done() and not os.path.exists(PASSWORD_CHANGED_MARKER)

        # Ensure CSRF token is present/refreshed
        csrf_token = request.cookies.get('csrf_token') or secrets.token_hex(32)

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


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    user = get_current_user()
    logout_user = user['username'] if user else 'unknown'
    tokens.pop(get_token(), None)
    audit_log('auth.logout', f'User "{logout_user}" logged out', username=logout_user)
    resp = jsonify({'ok': True})
    resp.delete_cookie('nas_token')
    resp.delete_cookie('csrf_token')
    return resp


def _is_sudo_mode():
    """Return True if current user is an admin (always-on sudo for admins)."""
    token = get_token()
    info = tokens.get(token)
    return bool(info and info.get('role') == 'admin')


@app.route('/api/auth/sudo', methods=['POST', 'GET'])
@require_auth
def get_sudo_status():
    """Sudo mode is always-on for admin users. Kept for API compat."""
    return jsonify({'ok': True, 'sudo_mode': _is_sudo_mode()})


# ── Security settings API (alert thresholds, session timeout, lockout) ──
_SECURITY_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'security_settings.json')


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


@app.route('/api/security/settings', methods=['GET'])
@require_auth
def get_security_settings():
    if not _is_sudo_mode():
        return jsonify({'error': 'Admin only'}), 403
    return jsonify(_load_security_settings())


@app.route('/api/security/settings', methods=['POST'])
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
    alert_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'alert_thresholds.json')
    with open(alert_path, 'w') as f:
        json.dump(settings.get('alert_thresholds', {}), f, indent=2)
    audit_log('security.settings', 'Security settings updated', username=get_current_user().get('username', 'admin'))
    return jsonify({'ok': True})


@app.route('/api/security/account-lockouts', methods=['GET'])
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


@app.route('/api/security/unlock-account', methods=['POST'])
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


# ─────────────────────────── Language / i18n ───────────────────────────

SUPPORTED_LANGUAGES = [
    'pl', 'en', 'de', 'fr', 'es',
]

@app.route('/api/language', methods=['GET'])
def get_language():
    """Get the system language (no auth required — needed during setup)."""
    env = _read_env_file()
    lang = env.get('LANGUAGE', 'pl')
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'pl'
    return jsonify({'language': lang, 'supported': SUPPORTED_LANGUAGES})


@app.route('/api/language', methods=['POST'])
def set_language():
    """Set the system language. Persists to ethos.env."""
    data = request.get_json(silent=True) or {}
    lang = data.get('language', 'pl')
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'pl'
    _write_env_file_key('LANGUAGE', lang)
    return jsonify({'ok': True, 'language': lang})


def _read_env_file():
    """Read ethos.env key-value pairs."""
    env = {}
    env_path = os.path.join(ETHOS_ROOT, 'ethos.env')
    try:
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def _write_env_file_key(key, value):
    """Write a single key to ethos.env (pure Python, no shell)."""
    env_path = os.path.join(ETHOS_ROOT, 'ethos.env')
    lines = []
    found = False
    if os.path.isfile(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if line.rstrip('\n').split('=', 1)[0].strip() == key:
                    lines.append(f'{key}={value}\n')
                    found = True
                else:
                    lines.append(line if line.endswith('\n') else line + '\n')
    if not found:
        lines.append(f'{key}={value}\n')
    with open(env_path, 'w') as f:
        f.writelines(lines)


# ─────────────────────────── Setup Wizard ───────────────────────────

_SETUP_PROGRESS = {
    'active': False,
    'stage': '',
    'message': '',
    'elapsed': 0,
    'started_at': 0,
    'updated_at': 0,
}
_setup_lock = __import__('threading').Lock()


def _setup_progress_update(stage, message, active=True):
    now = time.time()
    with _setup_lock:
        if active and not _SETUP_PROGRESS.get('started_at'):
            _SETUP_PROGRESS['started_at'] = now
        _SETUP_PROGRESS['active'] = active
        _SETUP_PROGRESS['stage'] = stage
        _SETUP_PROGRESS['message'] = message
        _SETUP_PROGRESS['updated_at'] = now
        _SETUP_PROGRESS['elapsed'] = int(now - (_SETUP_PROGRESS.get('started_at') or now))


def _setup_progress_start(message='Starting configuration...'):
    now = time.time()
    with _setup_lock:
        _SETUP_PROGRESS.update({
            'active': True,
            'stage': 'start',
            'message': message,
            'elapsed': 0,
            'started_at': now,
            'updated_at': now,
        })


def _setup_progress_end(stage, message):
    _setup_progress_update(stage, message, active=False)

def _is_setup_done():
    return os.path.exists(SETUP_DONE_FILE)


@app.route('/api/setup/status')
def setup_status():
    """Check if initial setup has been completed (no auth required)."""
    return jsonify({'needs_setup': not _is_setup_done()})


@app.route('/api/setup/progress')
def setup_progress():
    """Return current progress of /api/setup/complete execution."""
    with _setup_lock:
        if _SETUP_PROGRESS.get('active') and _SETUP_PROGRESS.get('started_at'):
            _SETUP_PROGRESS['elapsed'] = int(time.time() - _SETUP_PROGRESS['started_at'])
        return jsonify({
            'active': bool(_SETUP_PROGRESS.get('active')),
            'stage': _SETUP_PROGRESS.get('stage', ''),
            'message': _SETUP_PROGRESS.get('message', ''),
            'elapsed': int(_SETUP_PROGRESS.get('elapsed') or 0),
            'updated_at': int(_SETUP_PROGRESS.get('updated_at') or 0),
        })


@app.route('/api/setup/timezones')
def setup_timezones():
    """List available timezones (no auth for setup wizard)."""
    tz_dir = '/usr/share/zoneinfo'
    zones = []
    for region in sorted(os.listdir(tz_dir)):
        region_path = os.path.join(tz_dir, region)
        if not os.path.isdir(region_path) or region.startswith(('.', '+')) or region in ('posix', 'right', 'posixrules'):
            continue
        for city in sorted(os.listdir(region_path)):
            if os.path.isfile(os.path.join(region_path, city)):
                zones.append(f'{region}/{city}')
    return jsonify({'timezones': zones, 'default': 'Europe/Warsaw'})


@app.route('/api/setup/locales')
def setup_locales():
    """List commonly used locales for setup wizard."""
    locales = [
        {'code': 'en_US.UTF-8', 'name': 'English (US)'},
        {'code': 'en_GB.UTF-8', 'name': 'English (UK)'},
        {'code': 'pl_PL.UTF-8', 'name': 'Polski'},
        {'code': 'de_DE.UTF-8', 'name': 'Deutsch'},
        {'code': 'fr_FR.UTF-8', 'name': 'Français'},
        {'code': 'es_ES.UTF-8', 'name': 'Español'},
        {'code': 'it_IT.UTF-8', 'name': 'Italiano'},
        {'code': 'pt_BR.UTF-8', 'name': 'Português (Brasil)'},
        {'code': 'nl_NL.UTF-8', 'name': 'Nederlands'},
        {'code': 'sv_SE.UTF-8', 'name': 'Svenska'},
        {'code': 'nb_NO.UTF-8', 'name': 'Norsk'},
        {'code': 'da_DK.UTF-8', 'name': 'Dansk'},
        {'code': 'fi_FI.UTF-8', 'name': 'Suomi'},
        {'code': 'ja_JP.UTF-8', 'name': '日本語'},
        {'code': 'zh_CN.UTF-8', 'name': '中文 (简体)'},
        {'code': 'ko_KR.UTF-8', 'name': '한국어'},
    ]
    return jsonify({'locales': locales, 'default': 'en_US.UTF-8'})


@app.route('/api/setup/languages')
def setup_languages():
    """List available UI languages with their translations for firstboot i18n."""
    from i18n import SUPPORTED_LANGUAGES, _ensure_loaded, _translations, _I18N_DIR
    langs = []
    for code in SUPPORTED_LANGUAGES:
        _ensure_loaded(code)
        data = _translations.get(code, {})
        if data or os.path.exists(os.path.join(_I18N_DIR, f'{code}.json')):
            name = {'en': 'English', 'pl': 'Polski', 'de': 'Deutsch',
                    'fr': 'Français', 'es': 'Español'}.get(code, code)
            langs.append({'code': code, 'name': name})
    return jsonify({'languages': langs, 'default': 'en'})


@app.route('/api/setup/translations/<lang>')
def setup_translations(lang):
    """Get full translation file for a language (preboot i18n)."""
    import re
    if not re.match(r'^[a-z]{2}$', lang):
        return jsonify({'error': 'Invalid language code'}), 400
    from i18n import _ensure_loaded, _translations
    _ensure_loaded(lang)
    return jsonify(_translations.get(lang, {}))


# ── EthOS identification (public, no auth) ──

@app.route('/api/ethos/identify')
def ethos_identify():
    """Public endpoint for NAS-to-NAS discovery. Returns basic info about this instance."""
    import socket
    hostname = socket.gethostname()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = '127.0.0.1'
    return jsonify({
        'ethos': True,
        'name': NAS_NAME,
        'hostname': hostname,
        'ip': local_ip,
        'port': PORT,
        'version': ETHOS_VERSION.get('version', '0.0.0'),
    })


# ── Avahi mDNS service registration ──

def _register_avahi_service():
    """Register EthOS as an mDNS service so other NAS instances can discover it."""
    service_xml = f"""<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">{NAS_NAME} (%h)</name>
  <service>
    <type>_ethos._tcp</type>
    <port>{PORT}</port>
    <txt-record>name={NAS_NAME}</txt-record>
    <txt-record>version={ETHOS_VERSION.get('version', '0.0.0')}</txt-record>
  </service>
</service-group>
"""
    try:
        avahi_dir = '/etc/avahi/services'
        if os.path.isdir(avahi_dir):
            svc_file = os.path.join(avahi_dir, 'ethos.service')
            with open(svc_file, 'w') as f:
                f.write(service_xml)
            # Reload avahi if running
            subprocess.run(['systemctl', 'reload', 'avahi-daemon'],
                           capture_output=True, timeout=5)
            print(f'  Avahi mDNS: _ethos._tcp registered on port {PORT}')
    except Exception as e:
        print(f'  [warn] Avahi mDNS registration skipped: {e}')


@app.route('/api/setup/disks')
def setup_disks():
    """List block devices for data storage — includes raw/unpartitioned disks.

    Returns all disks with partition info.  For the system disk we also
    calculate unallocated (free) space so the wizard can offer to create
    a data partition there — just like Synology / QNAP.
    """
    if _is_setup_done():
        return jsonify({'error': 'Setup already completed'}), 400

    import subprocess as _sp

    # Use lsblk for comprehensive block device info (including raw disks)
    try:
        r = _sp.run(
            ['lsblk', '-J', '-b', '-o',
             'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,TRAN,RO,RM,LABEL,UUID'],
            capture_output=True, text=True, timeout=10)
        lsblk_data = json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        lsblk_data = {}

    block_devs = lsblk_data.get('blockdevices', [])

    # Find the system disk (the one that has / mounted)
    system_disk_names = set()

    def _find_system(devs, parent=None):
        for d in devs:
            if (d.get('mountpoint') or '') == '/':
                system_disk_names.add(parent or d['name'])
            for c in d.get('children', []):
                if (c.get('mountpoint') or '') == '/':
                    system_disk_names.add(d['name'])
            _find_system(d.get('children', []), parent or d.get('name'))

    _find_system(block_devs)

    def _get_free_space(disk_device):
        """Compute unallocated bytes on a disk using parted."""
        try:
            r = _sp.run(
                ['parted', '-ms', disk_device, 'unit', 'B', 'print', 'free'],
                capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return 0
            free_bytes = 0
            for line in r.stdout.splitlines():
                # free-space lines look like: 1:3276800B:64023257087B:64019980288B:free;
                if ':free;' in line:
                    parts = line.split(':')
                    if len(parts) >= 4:
                        size_str = parts[3].rstrip('B')
                        try:
                            free_bytes += int(size_str)
                        except ValueError:
                            pass
            return free_bytes
        except Exception:
            return 0

    disks = []
    for dev in block_devs:
        if dev.get('type') != 'disk':
            continue
        if dev.get('ro'):
            continue
        name = dev.get('name', '')
        if name.startswith(('loop', 'sr', 'fd', 'zram')):
            continue
        size = dev.get('size') or 0
        if size < 1_000_000_000:  # skip < 1 GB
            continue

        is_system = name in system_disk_names
        children = dev.get('children', [])

        partitions = []
        for child in children:
            ctype = child.get('type', '')
            if ctype not in ('part', 'crypt'):
                continue
            partitions.append({
                'name': child.get('name', ''),
                'device': f"/dev/{child.get('name', '')}",
                'size': child.get('size') or 0,
                'fstype': child.get('fstype') or '',
                'mountpoint': child.get('mountpoint') or '',
                'label': child.get('label') or '',
                'uuid': child.get('uuid') or '',
            })

        # Find first usable partition (ext4/xfs/btrfs and mounted, non-root)
        usable_mp = ''
        for p in partitions:
            if p['fstype'] in ('ext4', 'xfs', 'btrfs') and p['mountpoint'] \
                    and p['mountpoint'] != '/':
                usable_mp = p['mountpoint']
                break

        disk_info = {
            'name': name,
            'device': f"/dev/{name}",
            'size': size,
            'model': (dev.get('model') or '').strip(),
            'transport': dev.get('tran') or '',
            'removable': bool(dev.get('rm')),
            'is_system': is_system,
            'partitions': partitions,
            'usable_mountpoint': usable_mp,
        }

        # For system disk — compute free (unallocated) space
        if is_system:
            free = _get_free_space(f"/dev/{name}")
            disk_info['free_space'] = free
            # Already has a non-root EthOS-Data partition? (e.g. previous setup)
            for p in partitions:
                if p['label'] == 'EthOS-Data' and p['fstype'] in ('ext4', 'xfs', 'btrfs'):
                    disk_info['existing_data_partition'] = {
                        'device': p['device'],
                        'size': p['size'],
                        'fstype': p['fstype'],
                        'mountpoint': p['mountpoint'],
                    }
                    if p['mountpoint']:
                        disk_info['usable_mountpoint'] = p['mountpoint']
                    break

        disks.append(disk_info)

    # Sort: system disk first (primary option), then by size descending
    disks.sort(key=lambda d: (not d['is_system'], -d['size']))

    return jsonify({'disks': disks})


@app.route('/api/setup/prepare-disk', methods=['POST'])
def setup_prepare_disk():
    """Prepare a disk for data storage.

    Modes (determined by ``mode`` field):
    * ``format``  — wipe & partition an entire separate disk (existing behaviour)
    * ``syspart`` — create a new data partition from free space on the system disk
    * ``mount``   — mount an existing partition (e.g. EthOS-Data from previous setup)
    """
    if _is_setup_done():
        return jsonify({'error': 'Setup already completed'}), 400

    data = request.json or {}
    mode = data.get('mode', 'format')
    device = data.get('device', '').strip()
    encrypt = data.get('encrypt', False)
    passphrase = data.get('passphrase', '')

    if not device or not device.startswith('/dev/'):
        return jsonify({'error': 'Invalid device'}), 400
    if not os.path.exists(device):
        return jsonify({'error': f'Device {device} does not exist'}), 400

    import subprocess as _sp

    # For encryption, require passphrase
    if encrypt and (not passphrase or len(passphrase) < 4):
        return jsonify({'error': 'Encryption password must be at least 4 characters'}), 400
    if encrypt and shutil.which('cryptsetup') is None:
        return jsonify({'error': 'LUKS encryption requires the cryptsetup package. Install it or disable encryption.'}), 400

    mountpoint = '/mnt/data'

    def _parted_kernel_sync_issue(stderr_text):
        s = (stderr_text or '').lower()
        return ('unable to inform the kernel' in s) or ('will remain in use' in s)

    def _wait_for_partition(candidates, wait_sec=20):
        """Wait for new partition node to appear (udev can be delayed on some disks)."""
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            for c in candidates:
                if os.path.exists(c):
                    return c
            try:
                _sp.run(['udevadm', 'settle', '--timeout=3'], capture_output=True, timeout=5)
            except Exception:
                pass
            time.sleep(0.5)
        return ''

    def _try_unmount(target):
        """Try force unmount, then lazy unmount if target is busy."""
        if not target:
            return
        try:
            _sp.run(['umount', '-f', target], capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            _sp.run(['umount', '-l', target], capture_output=True, timeout=10)
        except Exception:
            pass

    # ── Helper: format + optional LUKS + mount a partition ──────
    def _format_and_mount(part_dev):
        target_dev = part_dev
        is_luks = False
        udisks_was_active = False

        # Safety: partition can get auto-mounted by udev/udisks right after creation.
        # Ensure it's unmounted before mkfs.
        try:
            r_um = _sp.run(['findmnt', '-rn', '-S', part_dev, '-o', 'TARGET'],
                           capture_output=True, text=True, timeout=5)
            for mp in (r_um.stdout or '').splitlines():
                mp = mp.strip()
                if mp:
                    _try_unmount(mp)
        except Exception:
            pass

        # Temporarily stop udisks automounter to avoid immediate re-mount during mkfs.
        try:
            r_ud = _sp.run(['systemctl', 'is-active', 'udisks2'],
                           capture_output=True, text=True, timeout=5)
            if r_ud.returncode == 0:
                _sp.run(['systemctl', 'stop', 'udisks2'], capture_output=True, timeout=15)
                udisks_was_active = True
        except Exception:
            pass

        def _restore_udisks():
            if not udisks_was_active:
                return
            try:
                _sp.run(['systemctl', 'start', 'udisks2'], capture_output=True, timeout=15)
            except Exception:
                pass

        try:
            if encrypt:
                os.makedirs('/etc/ethos', mode=0o700, exist_ok=True)
                keyfile = '/etc/ethos/luks.key'
                _sp.run(['dd', 'if=/dev/urandom', f'of={keyfile}', 'bs=4096', 'count=1'],
                        capture_output=True, timeout=10)
                os.chmod(keyfile, 0o600)

                r = _sp.run(['cryptsetup', 'luksFormat', '--batch-mode',
                             '--key-file', keyfile, part_dev],
                            capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    return None, f'LUKS error: {r.stderr.strip()}'

                r = _sp.run(['cryptsetup', 'luksAddKey', '--key-file', keyfile, part_dev],
                            input=passphrase.encode(),
                            capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    return None, f'Error adding password: {r.stderr.strip()}'

                r = _sp.run(['cryptsetup', 'luksOpen', '--key-file', keyfile,
                             part_dev, 'ethos_data'],
                            capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    return None, f'Error opening LUKS: {r.stderr.strip()}'

                target_dev = '/dev/mapper/ethos_data'
                is_luks = True

                r2 = _sp.run(['blkid', '-s', 'UUID', '-o', 'value', part_dev],
                             capture_output=True, text=True, timeout=5)
                part_uuid = r2.stdout.strip()
                if part_uuid:
                    existing = ''
                    try:
                        with open('/etc/crypttab', 'r') as f:
                            existing = f.read()
                    except FileNotFoundError:
                        pass
                    if 'ethos_data' not in existing:
                        with open('/etc/crypttab', 'a') as f:
                            f.write(f'# EthOS encrypted data disk\n'
                                    f'ethos_data UUID={part_uuid} {keyfile} luks\n')

            fmt_err = ''
            for _ in range(4):
                try:
                    # Direct unmount attempts help when automounters re-attach device.
                    _try_unmount(part_dev)
                    if target_dev != part_dev:
                        _try_unmount(target_dev)
                except Exception:
                    pass
                r = _sp.run(['mkfs.ext4', '-F', '-F', '-L', 'EthOS-Data', target_dev],
                            capture_output=True, text=True, timeout=300)
                if r.returncode == 0:
                    fmt_err = ''
                    break
                fmt_err = (r.stderr or '').strip()
                if 'is mounted; will not make a filesystem' in fmt_err.lower():
                    time.sleep(1)
                    continue
                return None, f'Formatting error: {fmt_err}'
            if fmt_err:
                return None, f'Formatting error: {fmt_err}'

            os.makedirs(mountpoint, mode=0o755, exist_ok=True)
            r = _sp.run(['mount', target_dev, mountpoint],
                        capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return None, f'Mount error: {r.stderr.strip()}'

            return {'success': True, 'mountpoint': mountpoint,
                    'device': part_dev, 'encrypted': is_luks}, None
        finally:
            _restore_udisks()

    try:
        # ── Mode: mount existing partition ──────────────────────
        if mode == 'mount':
            part_dev = data.get('partition', device)
            if not os.path.exists(part_dev):
                return jsonify({'error': f'Partition {part_dev} does not exist'}), 400
            os.makedirs(mountpoint, mode=0o755, exist_ok=True)
            # Check if already mounted at mountpoint
            r = _sp.run(['findmnt', '-no', 'SOURCE', mountpoint],
                        capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                # Already mounted — return success
                return jsonify({'success': True, 'mountpoint': mountpoint,
                                'device': part_dev, 'encrypted': False})
            r = _sp.run(['mount', part_dev, mountpoint],
                        capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return jsonify({'error': f'Mount error: {r.stderr.strip()}'}), 500
            return jsonify({'success': True, 'mountpoint': mountpoint,
                            'device': part_dev, 'encrypted': False})

        # ── Mode: create data partition from system disk free space ─
        if mode == 'syspart':
            # device = system disk (e.g. /dev/sda or /dev/mmcblk0)
            # Find highest partition number
            r = _sp.run(['parted', '-ms', device, 'unit', 'B', 'print'],
                        capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return jsonify({'error': f'Partition read error: {r.stderr.strip()}'}), 500

            max_num = 0
            for line in r.stdout.splitlines():
                if line and line[0].isdigit():
                    try:
                        max_num = max(max_num, int(line.split(':')[0]))
                    except (ValueError, IndexError):
                        pass

            new_num = max_num + 1

            # Find the largest free region via 'print free'
            r2 = _sp.run(
                ['parted', '-ms', device, 'unit', 'B', 'print', 'free'],
                capture_output=True, text=True, timeout=10)
            free_start = free_end = None
            best_size = 0
            for line in r2.stdout.splitlines():
                if ':free;' in line:
                    parts = line.split(':')
                    try:
                        fs = int(parts[1].rstrip('B'))
                        fe = int(parts[2].rstrip('B'))
                        size = fe - fs
                        if size > best_size:
                            best_size = size
                            free_start, free_end = fs, fe
                    except (ValueError, IndexError):
                        pass

            if free_start is None or best_size < 500_000_000:
                return jsonify({'error': 'Not enough free disk space'}), 400

            r = _sp.run(
                ['parted', '-s', device, 'mkpart', 'primary', 'ext4',
                 f'{free_start}B', f'{free_end}B'],
                capture_output=True, text=True, timeout=15)
            if r.returncode != 0 and not _parted_kernel_sync_issue(r.stderr):
                return jsonify({'error': f'Partition creation error: {r.stderr.strip()}'}), 500
            if r.returncode != 0:
                # Parted sometimes writes metadata but returns non-zero when kernel
                # hasn't re-read partition table yet. Try to resync and continue.
                _sp.run(['partprobe', device], capture_output=True, timeout=10)
                _sp.run(['udevadm', 'settle', '--timeout=8'], capture_output=True, timeout=12)

            _sp.run(['udevadm', 'settle', '--timeout=10'], capture_output=True, timeout=15)
            time.sleep(1)

            # Determine new partition device path
            # For /dev/sda → /dev/sdaN, for /dev/mmcblk0 → /dev/mmcblk0pN
            if 'mmcblk' in device or 'nvme' in device:
                part_dev = f"{device}p{new_num}"
            else:
                part_dev = f"{device}{new_num}"

            if not os.path.exists(part_dev):
                # Ask kernel/udev to refresh partition map, then wait a bit longer.
                _sp.run(['partprobe', device], capture_output=True, timeout=10)
                _sp.run(['udevadm', 'settle', '--timeout=5'], capture_output=True, timeout=10)
                found = _wait_for_partition([part_dev], wait_sec=20)
                if found:
                    part_dev = found

            if not os.path.exists(part_dev):
                return jsonify({'error': f'Partition {part_dev} did not appear'}), 500

            result, err = _format_and_mount(part_dev)
            if err:
                return jsonify({'error': err}), 500
            return jsonify(result)

        # ── Mode: format entire separate disk (default / legacy) ──
        # Safety: prevent formatting the system disk
        r = _sp.run(['findmnt', '-no', 'SOURCE', '/'],
                    capture_output=True, text=True, timeout=5)
        root_source = r.stdout.strip()
        root_disk = re.sub(r'p?\d+$', '', root_source)
        if device == root_disk or device == root_source:
            return jsonify({'error': 'Cannot format system disk!'}), 400

        # 1. Unmount any existing partitions on this disk
        r = _sp.run(['lsblk', '-nlo', 'NAME,MOUNTPOINT', device],
                    capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split('\n'):
            parts = line.split(None, 1)
            if len(parts) >= 2 and parts[1].strip():
                _try_unmount(parts[1].strip())

        # Also detach setup mountpoint if still in use from previous runs.
        _try_unmount(mountpoint)

        # 2. Close any existing LUKS mappings
        _sp.run(['bash', '-c',
                 'for m in /dev/mapper/ethos_data*; do '
                 '[ -e "$m" ] && cryptsetup close "$m" 2>/dev/null; done'],
                capture_output=True, timeout=10)

        # 3. Wipe existing signatures
        _sp.run(['wipefs', '-af', device], capture_output=True, timeout=15)

        # 4. Create GPT partition table with single partition using full disk
        r = _sp.run(['parted', '-s', device, 'mklabel', 'gpt'],
                    capture_output=True, text=True, timeout=15)
        if r.returncode != 0 and not _parted_kernel_sync_issue(r.stderr):
            return jsonify({'error': f'Partition table error: {r.stderr.strip()}'}), 500
        if r.returncode != 0:
            _sp.run(['partprobe', device], capture_output=True, timeout=10)
            _sp.run(['udevadm', 'settle', '--timeout=8'], capture_output=True, timeout=12)

        r = _sp.run(['parted', '-s', device, 'mkpart', 'primary', '1MiB', '100%'],
                    capture_output=True, text=True, timeout=15)
        if r.returncode != 0 and not _parted_kernel_sync_issue(r.stderr):
            return jsonify({'error': f'Partition creation error: {r.stderr.strip()}'}), 500
        if r.returncode != 0:
            _sp.run(['partprobe', device], capture_output=True, timeout=10)
            _sp.run(['udevadm', 'settle', '--timeout=8'], capture_output=True, timeout=12)

        # 5. Wait for udev and discover partition node (sdb1 or nvme0n1p1)
        _sp.run(['udevadm', 'settle', '--timeout=10'], capture_output=True, timeout=15)
        time.sleep(1)
        part_dev = _wait_for_partition([f"{device}1", f"{device}p1"], wait_sec=20)
        if not part_dev:
            _sp.run(['partprobe', device], capture_output=True, timeout=10)
            part_dev = _wait_for_partition([f"{device}1", f"{device}p1"], wait_sec=10)
        if not part_dev:
            return jsonify({'error': 'Partition did not appear after creation'}), 500

        result, err = _format_and_mount(part_dev)
        if err:
            return jsonify({'error': err}), 500
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/setup/complete', methods=['POST'])
def setup_complete():
    """Complete the initial setup — create admin user, set hostname, set password."""
    global NAS_NAME
    if _is_setup_done():
        return jsonify({'error': 'Setup already completed'}), 400

    data = request.json or {}
    hostname = re.sub(r'[^a-zA-Z0-9\-]', '', data.get('hostname', '')).strip() or 'ethos'
    username = re.sub(r'[^a-zA-Z0-9_.\-]', '', data.get('username', '')).strip()
    password = data.get('password', '')
    nas_name = data.get('nas_name', '').strip() or hostname
    data_disk = data.get('data_disk', '').strip()  # mountpoint for user data
    language = data.get('language', 'pl').strip()   # system language from wizard
    timezone = data.get('timezone', '').strip()     # e.g. "Europe/Warsaw"
    locale = data.get('locale', '').strip()         # e.g. "en_US.UTF-8"

    if not username or len(username) < 2:
        return jsonify({'error': 'Username is required (min. 2 characters)'}), 400

    from blueprints.users import validate_password_strength
    pw_ok, pw_errors = validate_password_strength(password, username)
    if not pw_ok:
        return jsonify({'error': pw_errors[0], 'password_errors': pw_errors}), 400

    import shlex
    errors = []
    _setup_progress_start('Starting system configuration...')

    # 0. Set timezone & locale if provided
    if timezone and re.match(r'^[A-Za-z_]+/[A-Za-z_/]+$', timezone):
        tz_path = f'/usr/share/zoneinfo/{timezone}'
        if os.path.exists(tz_path):
            _host_run_base(f"ln -sf {tz_path} /etc/localtime && "
                           f"echo {shlex.quote(timezone)} > /etc/timezone", timeout=10)
    if locale and re.match(r'^[a-zA-Z_]+\.[A-Za-z0-9-]+$', locale):
        _host_run_base(f"echo {shlex.quote(locale + ' UTF-8')} >> /etc/locale.gen && "
                       f"locale-gen >/dev/null 2>&1 && "
                       f"echo {shlex.quote('LANG=' + locale)} > /etc/default/locale",
                       timeout=30)

    # 1. Set hostname on host
    _setup_progress_update('hostname', 'Setting system hostname...')
    _host_run_base(f"hostnamectl set-hostname {shlex.quote(hostname)}", timeout=10)

    # 2. Setup data disk — create directory structure and symlinks
    if data_disk and data_disk != '/' and os.path.isdir(data_disk):
        _setup_progress_update('data_disk', 'Configuring data disk and symlinks...')
        _setup_data_disk(data_disk, errors)

    # 3. Create or update system user
    _setup_progress_update('user', 'Creating/updating administrator account...')
    safe_user = shlex.quote(username)
    safe_pass = shlex.quote(username + ':' + password)
    groups = 'sudo,ethos-user,ethos-admin'

    # Determine user home directory — on data disk if available
    home_base = os.path.join(data_disk, 'home') if (data_disk and data_disk != '/') else '/home'
    home_dir = os.path.join(home_base, username)
    os.makedirs(home_base, mode=0o755, exist_ok=True)

    r = _host_run_base(f"id {safe_user} 2>/dev/null", timeout=10)
    if r.returncode != 0:
        # Create user with home on data disk
        r = _host_run_base(
            f"getent group ethos-user >/dev/null 2>&1 || groupadd ethos-user; "
            f"getent group ethos-admin >/dev/null 2>&1 || groupadd ethos-admin; "
            f"getent group ethos-family >/dev/null 2>&1 || groupadd ethos-family; "
            f"useradd -m -d {shlex.quote(home_dir)} -s /bin/bash -G {groups} {safe_user} && "
            f"echo {safe_pass} | chpasswd",
            timeout=15)
        if r.returncode != 0:
            errors.append(f'Error creating user: {r.stderr.strip()}')
    else:
        # User exists — update password and groups
        _host_run_base(
            f"echo {safe_pass} | chpasswd && "
            f"usermod -aG {groups} {safe_user}",
            timeout=10)

    # Grant passwordless sudo (like the default nasadmin user)
    sudoers_file = f"/etc/sudoers.d/010_{username}"
    _host_run_base(
        f"echo {shlex.quote(username + ' ALL=(ALL) NOPASSWD:ALL')} > {shlex.quote(sudoers_file)} && "
        f"chmod 440 {shlex.quote(sudoers_file)}",
        timeout=10)

    # 3b. Create default folder structure based on wizard language + ~/.ethos
    _setup_progress_update('home', 'Creating user directory structure...')
    _ensure_user_home_structure(username, lang=language)

    # 3c. Keep only setup-selected admin account
    _setup_progress_update('admin_cleanup', 'Disabling other administrative accounts...')
    _restrict_admin_users(username, errors)

    # 4. Update NAS_NAME in memory
    _setup_progress_update('settings', 'Saving system settings...')
    NAS_NAME = nas_name

    # 4b. Persist language setting
    if language in SUPPORTED_LANGUAGES:
        _write_env_file_key('LANGUAGE', language)

    # 5. Update env files on host for persistence across restarts
    _update_compose_env(username, password, nas_name, data_disk)

    # 6. Mark setup as done
    _setup_progress_update('finalize', 'Finalizing configuration...')
    setup_info = {
        'timestamp': time.time(),
        'hostname': hostname,
        'username': username,
        'nas_name': nas_name,
    }
    if data_disk and data_disk != '/':
        setup_info['data_disk'] = data_disk
    with open(SETUP_DONE_FILE, 'w') as f:
        json.dump(setup_info, f)

    # Create password changed marker
    with open(PASSWORD_CHANGED_MARKER, 'w') as f:
        f.write(str(time.time()))

    # Enable SSH now that setup is complete and password is set
    try:
        subprocess.run(['systemctl', 'enable', '--now', 'ssh'], check=False)
    except Exception:
        pass

    # Remove tty1 auto-login override (no longer needed after setup)
    try:
        override_dir = '/etc/systemd/system/getty@tty1.service.d'
        override_file = os.path.join(override_dir, 'override.conf')
        if os.path.exists(override_file):
            _host_run_base(f'rm -f {override_file} && rmdir {override_dir} 2>/dev/null; systemctl daemon-reload', timeout=10)
    except Exception:
        pass  # non-critical

    if errors:
        _setup_progress_end('done', 'Configuration completed with warnings.')
        return jsonify({'success': True, 'warnings': errors})
    _setup_progress_end('done', 'Configuration completed successfully.')
    return jsonify({'success': True})


def _setup_data_disk(mountpoint, errors):
    """Create EthOS directory structure on the chosen data disk and symlink from ETHOS_ROOT."""
    import shutil
    ethos_on_disk = os.path.join(mountpoint, 'ethos')
    dirs_to_move = ['data', 'backups', 'uploads', 'logs', 'temp']

    # --- Ensure data disk is in fstab for auto-mount on boot ---
    try:
        # Find the device for this mountpoint
        import subprocess as _sp
        r = _sp.run(['findmnt', '-no', 'SOURCE', mountpoint],
                     capture_output=True, text=True, timeout=5)
        dev = r.stdout.strip()
        if dev:
            # Get UUID for stable identification
            r2 = _sp.run(['blkid', '-s', 'UUID', '-o', 'value', dev],
                          capture_output=True, text=True, timeout=5)
            uuid = r2.stdout.strip()
            # Get fstype
            r3 = _sp.run(['blkid', '-s', 'TYPE', '-o', 'value', dev],
                          capture_output=True, text=True, timeout=5)
            fstype = r3.stdout.strip() or 'auto'

            if uuid:
                fstab_line = f'UUID={uuid}  {mountpoint}  {fstype}  defaults,nofail,errors=continue  0  2'
                # Keep a single managed entry per mountpoint to avoid duplicates
                # when UUID changes after disk replacement/reformat.
                with open('/etc/fstab', 'r') as f:
                    raw_lines = f.read().splitlines()

                cleaned = []
                i = 0
                while i < len(raw_lines):
                    line = raw_lines[i]
                    stripped = line.strip()
                    # Remove old managed pair: comment + mountpoint line.
                    if stripped == '# EthOS data disk' and i + 1 < len(raw_lines):
                        nxt = raw_lines[i + 1].strip()
                        parts = nxt.split()
                        if len(parts) >= 2 and parts[1] == mountpoint:
                            i += 2
                            continue

                    # Remove any direct entry for this mountpoint.
                    if stripped and not stripped.startswith('#'):
                        parts = stripped.split()
                        if len(parts) >= 2 and parts[1] == mountpoint:
                            i += 1
                            continue

                    cleaned.append(line)
                    i += 1

                while cleaned and not cleaned[-1].strip():
                    cleaned.pop()
                cleaned.append('')
                cleaned.append('# EthOS data disk')
                cleaned.append(fstab_line)

                with open('/etc/fstab', 'w') as f:
                    f.write('\n'.join(cleaned).rstrip() + '\n')
    except Exception as e:
        errors.append(f'Failed to add disk to fstab: {e}')

    try:
        os.makedirs(ethos_on_disk, mode=0o755, exist_ok=True)

        for d in dirs_to_move:
            disk_dir = os.path.join(ethos_on_disk, d)
            local_dir = os.path.join(ETHOS_ROOT, d)

            os.makedirs(disk_dir, mode=0o755, exist_ok=True)

            # If local_dir exists and is NOT already a symlink, move its contents
            if os.path.isdir(local_dir) and not os.path.islink(local_dir):
                for item in os.listdir(local_dir):
                    src = os.path.join(local_dir, item)
                    dst = os.path.join(disk_dir, item)
                    if not os.path.exists(dst):
                        try:
                            shutil.move(src, dst)
                        except Exception as e:
                            errors.append(f'Error moving {item}: {e}')
                # Remove the now-empty directory
                try:
                    shutil.rmtree(local_dir)
                except Exception:
                    pass

            # Create symlink: ETHOS_ROOT/data → /mnt/disk/ethos/data
            if not os.path.islink(local_dir):
                try:
                    if os.path.exists(local_dir):
                        shutil.rmtree(local_dir)
                    os.symlink(disk_dir, local_dir)
                except Exception as e:
                    errors.append(f'Error creating symlink {d}: {e}')

        # Also create shared user folders on disk
        shared_dir = os.path.join(ethos_on_disk, 'shared')
        os.makedirs(shared_dir, mode=0o777, exist_ok=True)

    except Exception as e:
        errors.append(f'Error configuring data disk: {e}')


def _restrict_admin_users(primary_user, errors):
    """Keep only setup-selected user in sudo/ethos-admin admin groups.

    Legacy default account `nasadmin` is also locked when another user is selected.
    """
    import shlex
    try:
        r = _host_run_base("getent group sudo ethos-admin 2>/dev/null | cut -d: -f4 | tr ',' '\n' | sort -u", timeout=10)
        admin_users = [u.strip() for u in (r.stdout or '').split('\n') if u.strip()]
    except Exception as e:
        errors.append(f'Failed to read admin list: {e}')
        return

    for user in admin_users:
        if user in (primary_user, 'root'):
            continue
        su = shlex.quote(user)
        # Remove elevated groups but keep non-admin groups.
        _host_run_base(f'gpasswd -d {su} sudo 2>/dev/null || true', timeout=10)
        _host_run_base(f'gpasswd -d {su} ethos-admin 2>/dev/null || true', timeout=10)

        # Kill default-password path if legacy user remains on system.
        if user == 'nasadmin':
            _host_run_base(f'passwd -l {su} 2>/dev/null || true', timeout=10)
            _host_run_base(f'usermod -p "!" {su} 2>/dev/null || true', timeout=10)


def _update_compose_env(username, password, nas_name, data_disk=''):
    """Update ETHOS_USER, NAS_NAME, DATA_DISK in ethos.env."""
    import shlex
    try:
        ethos_root = os.environ.get('ETHOS_ROOT', '/opt/ethos')
        env_file = os.path.join(ethos_root, 'ethos.env')

        # Write ETHOS_USER
        _write_env_file_key('ETHOS_USER', username)

        safe_name = shlex.quote(nas_name)
        _host_run_base(
            f'sed -i "s|NAS_NAME=.*|NAS_NAME={safe_name}|" {shlex.quote(env_file)}',
            timeout=10)
        # Add or update DATA_DISK
        if data_disk and data_disk != '/':
            safe_disk = shlex.quote(data_disk)
            # Check if DATA_DISK line exists
            r = _host_run_base(
                f'grep -q "^DATA_DISK=" {shlex.quote(env_file)}',
                timeout=5)
            if r.returncode == 0:
                _host_run_base(
                    f'sed -i "s|DATA_DISK=.*|DATA_DISK={safe_disk}|" {shlex.quote(env_file)}',
                    timeout=10)
            else:
                _host_run_base(
                    f'echo "DATA_DISK={safe_disk}" >> {shlex.quote(env_file)}',
                    timeout=10)
    except Exception:
        pass


# ─────────────────────────── Power Management ───────────────────────────

@app.route('/api/power/action', methods=['POST'])
@require_auth
def power_action():
    """Perform a power management action on the host or app."""
    data = request.json or {}
    action = data.get('action', '')

    allowed = ('shutdown', 'reboot', 'restart-app')
    if action not in allowed:
        return jsonify({'error': f'Unknown action: {action}'}), 400

    if action == 'restart-app':
        elog('system', 'warning', 'Power Manager: restarting application')
        gevent.spawn_later(1, _restart_self)
        return jsonify({'ok': True, 'message': 'Application restarting shortly...'})

    if action == 'shutdown':
        elog('system', 'warning', 'Power Manager: shutting down system')
        gevent.spawn_later(2, _host_power, 'poweroff')
        return jsonify({'ok': True, 'message': 'System will shut down...'})

    if action == 'reboot':
        elog('system', 'warning', 'Power Manager: rebooting system')
        gevent.spawn_later(2, _host_power, 'reboot')
        return jsonify({'ok': True, 'message': 'System will reboot shortly...'})


def _host_power(cmd):
    """Execute poweroff/reboot on the host."""
    try:
        _host_run_base(cmd, timeout=30)
    except Exception as e:
        app.logger.error(f'Power action {cmd} failed: {e}')


def _restart_self():
    """Restart EthOS via systemd."""
    try:
        subprocess.run(['systemctl', 'restart', 'ethos'], timeout=60)
    except Exception as e:
        app.logger.error(f'Restart failed: {e}')


@app.route('/api/power/status')
@require_auth
def power_status():
    """Return uptime and load info."""
    sys_info = _mon_sys()
    cpu_info = _mon_cpu()
    return jsonify({
        'uptime': sys_info['uptime'],
        'load': [round(v, 2) for v in cpu_info['load_avg'][:3]]
    })


# ─────────────────────────── System Info ───────────────────────────

@app.route('/api/system/info')
@require_auth
@cache.cached(timeout=5)
def system_info():
    cpu = _mon_cpu()
    ram = _mon_ram()
    sys_i = _mon_sys()
    disks = _mon_disk()
    net = psutil.net_io_counters()

    return jsonify({
        'hostname': NAS_NAME,
        'cpu_percent': cpu['usage_percent'],
        'cpu_count': cpu['core_count'],
        'memory': {
            'total': ram['total'],
            'used': ram['used'],
            'available': ram['available'],
            'percent': ram['percent']
        },
        'disks': disks,
        'network': {
            'bytes_sent': net.bytes_sent,
            'bytes_recv': net.bytes_recv
        },
        'uptime': sys_i['uptime'],
        'cpu_temp': cpu['temperature'],
        'ups': _ups_status
    })


@app.route('/api/system/version')
@require_auth
def system_version():
    """Return EthOS version info and changelog."""
    return jsonify(ETHOS_VERSION)


@app.route('/api/system/deps', methods=['POST'])
@require_auth
def system_install_dep():
    """Install a missing dependency on-demand. Body: {binary: "smbclient"}"""
    from host import ensure_dep
    data = request.get_json(silent=True) or {}
    binary = data.get('binary', '').strip()
    if not binary:
        return jsonify({'error': 'binary required'}), 400
    ok, msg = ensure_dep(binary, install=True)
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'ok': False, 'error': msg}), 500


# ─────────────────────────── Services Manager ───────────────────────────

# Known services with metadata for nicer display
_KNOWN_SERVICES = {
    'smbd':       {'name': 'Samba (smbd)',     'pkg': 'samba',             'icon': 'fa-windows',      'cat': 'Sharing'},
    'nmbd':       {'name': 'Samba NetBIOS',    'pkg': 'samba',             'icon': 'fa-windows',      'cat': 'Sharing'},
    'nfs-kernel-server':{'name': 'NFS Server', 'pkg': 'nfs-kernel-server', 'icon': 'fa-network-wired','cat': 'Sharing'},
    'minidlna':   {'name': 'MiniDLNA',         'pkg': 'minidlna',         'icon': 'fa-photo-video',  'cat': 'Sharing'},
    'lighttpd':   {'name': 'Lighttpd (WebDAV)','pkg': 'lighttpd',         'icon': 'fa-globe',        'cat': 'Sharing'},
    'vsftpd':     {'name': 'FTP (vsftpd)',     'pkg': 'vsftpd',           'icon': 'fa-upload',       'cat': 'Sharing'},
    'ssh':        {'name': 'SSH / SFTP',       'pkg': 'openssh-server',   'icon': 'fa-lock',         'cat': 'System'},
    'cups':       {'name': 'CUPS (drukarka)',   'pkg': 'cups',             'icon': 'fa-print',        'cat': 'System'},
    'nut-server': {'name': 'UPS Server (NUT)',  'pkg': 'nut',              'icon': 'fa-battery-full', 'cat': 'System'},
    'docker':     {'name': 'Docker',           'pkg': 'docker-ce',        'icon': 'fa-cubes',        'cat': 'System'},
    'ethos':    {'name': 'EthOS',          'pkg': None,               'icon': 'fa-server',       'cat': 'System'},
    'avahi-daemon':{'name': 'Avahi (mDNS)',    'pkg': 'avahi-daemon',     'icon': 'fa-broadcast-tower','cat': 'System'},
    'NetworkManager':{'name': 'NetworkManager','pkg': 'network-manager',  'icon': 'fa-wifi',         'cat': 'System'},
    'cron':       {'name': 'Cron (zaplanowane)','pkg': 'cron',            'icon': 'fa-clock',        'cat': 'System'},
    'transmission-daemon':{'name':'Transmission','pkg':'transmission-daemon','icon':'fa-magnet',      'cat': 'Apps'},
}

@app.route('/api/services/list')
@require_auth
@cache.cached(timeout=10)
def services_list():
    """List only EthOS-relevant services with their status."""
    services = []
    # Only query known services — no need to list all systemd units
    for svc_id, meta in _KNOWN_SERVICES.items():
        r = _host_run_base(f"systemctl show {svc_id}.service --no-pager "
                           f"--property=LoadState,ActiveState,SubState,UnitFileState 2>/dev/null",
                           timeout=5)
        props = {}
        for line in r.stdout.strip().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                props[k] = v

        load_state = props.get('LoadState', 'not-found')
        if load_state == 'not-found':
            # Service not installed — show as installable if it has a package
            if meta.get('pkg'):
                services.append({
                    'id': svc_id,
                    'name': meta['name'],
                    'icon': meta.get('icon', 'fa-cog'),
                    'category': meta.get('cat', 'Inne'),
                    'pkg': meta.get('pkg'),
                    'active': False,
                    'state': 'not-installed',
                    'enabled': 'not-installed',
                    'masked': False,
                    'installed': False,
                })
            continue

        active = props.get('ActiveState', 'inactive')
        sub = props.get('SubState', 'dead')
        enabled = props.get('UnitFileState', 'unknown')

        services.append({
            'id': svc_id,
            'name': meta['name'],
            'icon': meta.get('icon', 'fa-cog'),
            'category': meta.get('cat', 'Inne'),
            'pkg': meta.get('pkg'),
            'active': active == 'active',
            'state': sub,
            'enabled': enabled,
            'masked': load_state == 'masked',
            'installed': True,
        })

    # Sort by category, then name
    cat_order = {'System': 0, 'Sharing': 1, 'Apps': 2, 'Inne': 3}
    services.sort(key=lambda s: (cat_order.get(s['category'], 9), s['name'].lower()))
    return jsonify({'services': services})


@app.route('/api/services/action', methods=['POST'])
@require_auth
def services_action():
    """Perform action on a service. Body: {service, action}
    Actions: start, stop, restart, enable, disable, uninstall"""
    data = request.get_json(silent=True) or {}
    name = data.get('service', '').strip()
    action = data.get('action', '').strip()

    if not name or not action:
        return jsonify({'error': 'service and action required'}), 400

    # Sanitize service name
    import re as _re
    import shlex
    if not _re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
        return jsonify({'error': 'Invalid service name'}), 400

    # Protect critical services
    _PROTECTED = {'ethos', 'ssh', 'sshd', 'NetworkManager', 'systemd-journald', 'systemd-logind', 'dbus'}
    if name in _PROTECTED and action in ('stop', 'disable', 'uninstall'):
        return jsonify({'error': f'Service {name} is protected — cannot {action}'}), 403

    if action in ('start', 'stop', 'restart'):
        r = _host_run_base(f"systemctl {action} {shlex.quote(name)}", timeout=30)
        if r.returncode != 0:
            return jsonify({'error': f'{action} failed: {r.stderr.strip()[-200:]}'}), 500
        return jsonify({'ok': True, 'message': f'{name}: {action} OK'})

    elif action == 'enable':
        r = _host_run_base(f"systemctl enable {shlex.quote(name)}", timeout=15)
        if r.returncode != 0:
            return jsonify({'error': r.stderr.strip()[-200:]}), 500
        return jsonify({'ok': True, 'message': f'{name} enabled at startup'})

    elif action == 'disable':
        r = _host_run_base(f"systemctl disable {shlex.quote(name)}", timeout=15)
        if r.returncode != 0:
            return jsonify({'error': r.stderr.strip()[-200:]}), 500
        return jsonify({'ok': True, 'message': f'{name} disabled at startup'})

    elif action == 'uninstall':
        meta = _KNOWN_SERVICES.get(name, {})
        pkg = meta.get('pkg') or data.get('pkg', '').strip()
        if not pkg:
            return jsonify({'error': 'Package not found for uninstall'}), 400
        # Stop first
        _host_run_base(f"systemctl stop {shlex.quote(name)} 2>/dev/null", timeout=15)
        r = _host_run_base(f"apt-get remove -y {shlex.quote(pkg)}", timeout=120)
        if r.returncode != 0:
            return jsonify({'error': f'Uninstall failed: {r.stderr.strip()[-200:]}'}), 500
        return jsonify({'ok': True, 'message': f'{pkg} uninstalled'})

    elif action == 'install':
        meta = _KNOWN_SERVICES.get(name, {})
        pkg = meta.get('pkg') or data.get('pkg', '').strip()
        if not pkg:
            return jsonify({'error': 'Package not found for install'}), 400

        # Run install asynchronously with progress via SocketIO
        import gevent as _gev

        def _install_bg():
            svc_name = name
            svc_pkg = pkg
            try:
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'start', 'progress': 0,
                    'message': f'Preparing installation of {svc_pkg}…',
                })

                if svc_pkg == 'docker-ce':
                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': 'install', 'progress': 20,
                        'message': 'Installing Docker CE…',
                    })
                    from host import ensure_dep
                    ok, msg = ensure_dep('docker', install=True)
                    if not ok:
                        socketio.emit('service_install_progress', {
                            'service': svc_name, 'pkg': svc_pkg,
                            'phase': 'error', 'progress': 0,
                            'message': msg,
                        })
                        return
                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': 'done', 'progress': 100,
                        'message': 'Docker installed',
                    })
                    return

                # Phase 1: heal dpkg + apt-get update
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'update', 'progress': 5,
                    'message': 'Repairing package manager…',
                })
                _host_run_base("dpkg --configure -a 2>/dev/null", timeout=60)

                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'update', 'progress': 10,
                    'message': 'Updating package list…',
                })
                r_upd = _host_run_base("apt-get update -qq", timeout=120)
                if r_upd.returncode != 0:
                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': 'update', 'progress': 15,
                        'message': 'Updating package list (warning)…',
                        'detail': r_upd.stderr.strip()[-300:],
                    })

                # Phase 2: install via streaming
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'install', 'progress': 20,
                    'message': f'Installing {svc_pkg}…',
                })

                stream = _host_run_stream_base(
                    f"DEBIAN_FRONTEND=noninteractive apt-get install -y -o Dpkg::Use-Pty=0 {shlex.quote(svc_pkg)}"
                )
                lines_buf = []
                progress = 20
                for line in stream:
                    line_s = line.rstrip('\n')
                    if line_s.startswith('__EXIT_CODE__:'):
                        exit_code = int(line_s.split(':')[1])
                        break
                    lines_buf.append(line_s)
                    # Parse apt progress hints
                    if line_s.startswith('Get:') or line_s.startswith('Fetching'):
                        progress = min(progress + 3, 60)
                        phase_name = 'download'
                    elif 'Unpacking' in line_s:
                        progress = min(max(progress, 60), 75)
                        phase_name = 'unpack'
                    elif 'Setting up' in line_s:
                        progress = min(max(progress, 75), 90)
                        phase_name = 'configure'
                    elif 'Processing triggers' in line_s:
                        progress = 90
                        phase_name = 'triggers'
                    else:
                        phase_name = 'install'

                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': phase_name, 'progress': progress,
                        'message': line_s[:200],
                    })
                else:
                    exit_code = 1

                if exit_code != 0:
                    err_out = '\n'.join(lines_buf[-10:])
                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': 'error', 'progress': 0,
                        'message': f'Installation failed (code {exit_code})',
                        'detail': err_out[-500:],
                    })
                    return

                # Phase 3: enable & start
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'enable', 'progress': 95,
                    'message': f'Starting {svc_name}…',
                })
                _host_run_base(f"systemctl enable {shlex.quote(svc_name)} 2>/dev/null", timeout=10)
                _host_run_base(f"systemctl start {shlex.quote(svc_name)} 2>/dev/null", timeout=10)

                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'done', 'progress': 100,
                    'message': f'{svc_pkg} installed and started',
                })

            except Exception as ex:
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'error', 'progress': 0,
                    'message': f'Error: {ex}',
                })

        _gev.spawn(_install_bg)
        return jsonify({'ok': True, 'message': f'Installation of {pkg} started', 'async': True})

    else:
        return jsonify({'error': f'Unknown action: {action}'}), 400


# ── GPU Driver Installer ──────────────────────────────────────────────

@app.route('/api/resources/gpu/install', methods=['POST'])
@require_auth
def gpu_driver_install():
    """Install GPU drivers detected by lspci. Streams progress via SocketIO."""
    from blueprints.monitor import detect_gpu_hardware
    data = request.get_json() or {}
    card_index = data.get('card_index', 0)

    hw = detect_gpu_hardware()
    if not hw['cards']:
        return jsonify({'error': 'No GPU card detected'}), 400
    if card_index >= len(hw['cards']):
        return jsonify({'error': 'Invalid card index'}), 400

    card = hw['cards'][card_index]
    if card.get('packages_installed') and not card.get('driver_installed'):
        socketio.emit('gpu_driver_progress', {
            'phase': 'done', 'progress': 100,
            'message': 'Drivers already installed. System restart required.',
            'vendor': card.get('vendor', 'unknown'),
            'reboot_required': True,
        })
        return jsonify({
            'ok': True,
            'message': 'Drivers already installed — restart required',
            'reboot_required': True,
        })

    if not card.get('install_cmd'):
        return jsonify({'error': f"No install instructions for {card.get('vendor', '?')}"}), 400

    import gevent as _gev

    def _gpu_install_bg():
        vendor = card['vendor']
        cmd = card['install_cmd']
        try:
            socketio.emit('gpu_driver_progress', {
                'phase': 'start', 'progress': 0,
                'message': f"Preparing {vendor.upper()} driver installation…",
                'vendor': vendor,
            })

            # Heal dpkg first
            socketio.emit('gpu_driver_progress', {
                'phase': 'update', 'progress': 5,
                'message': 'Repairing package manager…',
                'vendor': vendor,
            })
            _host_run_base("dpkg --configure -a 2>/dev/null", timeout=60)

            # Stream the install command
            socketio.emit('gpu_driver_progress', {
                'phase': 'install', 'progress': 10,
                'message': f"Installing {vendor.upper()} drivers…",
                'vendor': vendor,
            })

            full_cmd = cmd
            stream = _host_run_stream_base(full_cmd)
            lines_buf = []
            progress = 10
            exit_code = 1
            for line in stream:
                line_s = line.rstrip('\n')
                if line_s.startswith('__EXIT_CODE__:'):
                    exit_code = int(line_s.split(':')[1])
                    break
                lines_buf.append(line_s)
                if line_s.startswith('Get:') or line_s.startswith('Hit:'):
                    progress = min(progress + 2, 40)
                    phase = 'download'
                elif 'Unpacking' in line_s:
                    progress = min(max(progress, 40), 65)
                    phase = 'unpack'
                elif 'Setting up' in line_s:
                    progress = min(max(progress, 65), 85)
                    phase = 'configure'
                elif 'Processing triggers' in line_s:
                    progress = 88
                    phase = 'triggers'
                else:
                    phase = 'install'
                socketio.emit('gpu_driver_progress', {
                    'phase': phase, 'progress': progress,
                    'message': line_s[:200],
                    'vendor': vendor,
                })
            else:
                exit_code = 1

            if exit_code != 0:
                err_out = '\n'.join(lines_buf[-10:])
                socketio.emit('gpu_driver_progress', {
                    'phase': 'error', 'progress': 0,
                    'message': f'Installation failed (code {exit_code})',
                    'detail': err_out[-500:],
                    'vendor': vendor,
                })
                return

            socketio.emit('gpu_driver_progress', {
                'phase': 'done', 'progress': 100,
                'message': 'Drivers installed! System restart required.',
                'vendor': vendor,
                'reboot_required': True,
            })

        except Exception as ex:
            socketio.emit('gpu_driver_progress', {
                'phase': 'error', 'progress': 0,
                'message': f'Error: {ex}',
                'vendor': vendor,
            })

    _gev.spawn(_gpu_install_bg)
    return jsonify({'ok': True, 'message': 'Driver installation started', 'async': True})


@app.route('/api/services/logs')
@require_auth
def services_logs():
    """Get recent journal logs for a service. Query: ?service=name&lines=50"""
    name = request.args.get('service', '').strip()
    try:
        lines = min(int(request.args.get('lines', '50')), 500)
    except (ValueError, TypeError):
        lines = 50
    if not name:
        return jsonify({'error': 'service required'}), 400
    import re as _re
    import shlex
    if not _re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
        return jsonify({'error': 'Invalid service name'}), 400
    r = _host_run_base(f"journalctl -u {shlex.quote(name)} --no-pager -n {lines} --output=short-iso", timeout=15)
    return jsonify({'logs': r.stdout, 'service': name})


# ─────────────────────────── File Manager ───────────────────────────

import zipfile
import tarfile
import threading as _threading

_fileop_lock = _threading.RLock()

# ── Dir-size in-memory cache ────────────────────────────────
# Maps path → {'size': int, 'expires': monotonic_time}
# OrderedDict preserves insertion order for O(1) FIFO eviction.
from collections import OrderedDict as _ODict
_dirsize_cache = _ODict()
_dirsize_cache_lock = _threading.Lock()
_DIRSIZE_CACHE_TTL = 600   # seconds
_DIRSIZE_CACHE_MAX = 1000  # max entries — increased for better hit rate
_DIRSIZE_CACHE_PERSIST_FILE = _data_path('.dirsize_cache.json')
_DIRSIZE_CACHE_SAVE_INTERVAL = 60  # minimum seconds between disk saves
_dirsize_cache_last_save = 0.0

def _dirsize_cache_get(path):
    """Return cached size for *path* or None if missing/expired."""
    with _dirsize_cache_lock:
        entry = _dirsize_cache.get(path)
        if entry:
            if time.monotonic() < entry['expires']:
                return entry['size']
            del _dirsize_cache[path]  # evict expired entry eagerly
        return None

def _dirsize_cache_set(path, size):
    """Store *size* for *path* with TTL; O(1) FIFO eviction when over limit."""
    global _dirsize_cache_last_save
    with _dirsize_cache_lock:
        # Move-to-end on update keeps most-recently-set at the back
        if path in _dirsize_cache:
            _dirsize_cache.move_to_end(path)
        _dirsize_cache[path] = {'size': size, 'expires': time.monotonic() + _DIRSIZE_CACHE_TTL}
        if len(_dirsize_cache) > _DIRSIZE_CACHE_MAX:
            # Evict oldest insertion (O(1) with OrderedDict)
            _dirsize_cache.popitem(last=False)
    # Persist to disk (debounced — at most once per _DIRSIZE_CACHE_SAVE_INTERVAL)
    now = time.monotonic()
    if now - _dirsize_cache_last_save > _DIRSIZE_CACHE_SAVE_INTERVAL:
        _dirsize_cache_last_save = now
        gevent.spawn(_dirsize_cache_persist)

def _dirsize_cache_invalidate(path):
    """Evict *path* (and any sub-paths) from the dir-size cache."""
    with _dirsize_cache_lock:
        keys = [k for k in _dirsize_cache if k == path or k.startswith(path + '/')]
        for k in keys:
            del _dirsize_cache[k]

def _dirsize_cache_persist():
    """Write current in-memory dir-size cache to disk (runs in a background greenlet)."""
    now = time.monotonic()
    try:
        snapshot = {}
        with _dirsize_cache_lock:
            for path, entry in list(_dirsize_cache.items()):
                remaining = entry['expires'] - now
                if remaining > 0:
                    snapshot[path] = {'size': entry['size'], 'remaining': round(remaining, 1)}
        tmp = _DIRSIZE_CACHE_PERSIST_FILE + '.tmp'
        with open(tmp, 'w') as _f:
            json.dump(snapshot, _f)
        os.replace(tmp, _DIRSIZE_CACHE_PERSIST_FILE)
    except Exception:
        pass

def _dirsize_cache_restore():
    """Load persisted dir-size cache from disk at startup."""
    try:
        with open(_DIRSIZE_CACHE_PERSIST_FILE) as _f:
            raw = json.load(_f)
        now = time.monotonic()
        loaded = 0
        with _dirsize_cache_lock:
            for path, entry in raw.items():
                remaining = entry.get('remaining', 0)
                if remaining > 0 and loaded < _DIRSIZE_CACHE_MAX:
                    _dirsize_cache[path] = {'size': entry['size'], 'expires': now + remaining}
                    loaded += 1
    except Exception:
        pass

# Restore persisted cache at import time
_dirsize_cache_restore()

# ── Short-lived listing cache ────────────────────────────────
# Maps real_path → {'items': list, 'expires': monotonic_time}
# OrderedDict preserves insertion order for O(1) FIFO eviction.
_listdir_cache = _ODict()
_listdir_cache_lock = _threading.Lock()
_LISTDIR_CACHE_TTL = 45   # seconds — slightly longer for better hit rate
_LISTDIR_CACHE_MAX = 400  # max entries — doubled for large deployments

def _listdir_cache_get(real_path):
    """Return cached item list for *real_path* or None if missing/expired/stale.

    On every hit we do a single os.path.getmtime() syscall (~1-2 µs) so that
    any write to the directory immediately invalidates the cached listing
    instead of waiting up to the full TTL.
    """
    with _listdir_cache_lock:
        entry = _listdir_cache.get(real_path)
        if not entry:
            return None
        if time.monotonic() >= entry['expires']:
            del _listdir_cache[real_path]  # evict expired entry eagerly
            return None
        # Invalidate if directory mtime changed since we cached it
        _cached_mtime = entry.get('mtime')
        if _cached_mtime is not None:
            try:
                if os.path.getmtime(real_path) != _cached_mtime:
                    del _listdir_cache[real_path]
                    return None
            except OSError:
                del _listdir_cache[real_path]
                return None
        return entry['items']

def _listdir_cache_set(real_path, items, mtime=None):
    """Store *items* for *real_path* with short TTL; O(1) FIFO eviction.

    Pass *mtime* (os.path.getmtime result) so the cache can self-invalidate
    the moment the directory changes rather than waiting for TTL expiry.
    """
    with _listdir_cache_lock:
        if real_path in _listdir_cache:
            _listdir_cache.move_to_end(real_path)
        _listdir_cache[real_path] = {
            'items': items,
            'expires': time.monotonic() + _LISTDIR_CACHE_TTL,
            'mtime': mtime,
        }
        if len(_listdir_cache) > _LISTDIR_CACHE_MAX:
            _listdir_cache.popitem(last=False)  # O(1) FIFO eviction

def _listdir_cache_invalidate(path):
    """Evict *path* and its parent from the listing cache."""
    real = safe_path(path) if path else None
    parent_real = os.path.dirname(real) if real else None
    with _listdir_cache_lock:
        for key in list(_listdir_cache.keys()):
            if key == real or key == parent_real or (real and key.startswith(real + '/')):
                del _listdir_cache[key]


# ── Background dir-size jobs ────────────────────────────────
# job_id → { path: str, size: int|None, done: bool, error: bool, ts: float }
_dirsize_bg_jobs = {}
_dirsize_bg_lock = _threading.Lock()
_DIRSIZE_JOB_EXPIRY = 120  # seconds — stale completed jobs are cleaned up
# path → job_id for currently running (not-yet-done) jobs — prevents duplicate spawning
_dirsize_running_by_path = {}

def _dirsize_bg_jobs_prune():
    """Remove completed/stale jobs older than _DIRSIZE_JOB_EXPIRY seconds."""
    now = time.monotonic()
    with _dirsize_bg_lock:
        stale = [jid for jid, j in _dirsize_bg_jobs.items()
                 if j.get('done') and (now - j.get('ts', now)) > _DIRSIZE_JOB_EXPIRY]
        for jid in stale:
            del _dirsize_bg_jobs[jid]

# Two independent operation channels so e.g. compress doesn't block copy/move.
# Channel 'bg' = heavy background ops (compress, download-zip, transfer)
# Channel 'fm' = file-management ops (copy, move, extract)
_OP_CHANNEL = {
    'compress': 'bg', 'download': 'bg', 'transfer': 'bg',
    'copy': 'fm', 'move': 'fm', 'extract': 'fm',
}

def _new_channel_state():
    return {
        'active': False,
        'operation': None,
        'progress': None,
        'result': None,
        'cancel': False,
        'paused': False,
        '_proc': None,
        'meta': None,
    }

_fileop_channels = {
    'bg': _new_channel_state(),
    'fm': _new_channel_state(),
}

# ── Backward-compat alias ── (used in a few places that touch _proc / progress directly)
_fileop_state = _fileop_channels['bg']

# ── Persistent transfer resume ──
TRANSFER_RESUME_FILE = _data_path('transfer_resume.json')

def _save_transfer_task(resolved, server_id, remote_dest, requesting_user):
    """Persist transfer task to disk so it survives server restarts."""
    task = {
        'paths': resolved,
        'server_id': server_id,
        'remote_dest': remote_dest,
        'requesting_user': requesting_user,
        'started': time.time(),
    }
    _save_json(TRANSFER_RESUME_FILE, task)

def _clear_transfer_task():
    """Remove persisted transfer task (transfer completed or cancelled)."""
    try:
        if os.path.exists(TRANSFER_RESUME_FILE):
            os.remove(TRANSFER_RESUME_FILE)
    except OSError:
        pass

# ── Persistent ZIP download resume ──
ZIP_RESUME_FILE = _data_path('zip_resume.json')

def _save_zip_task(resolved, tmp_path, zip_name, download_id, total):
    """Persist ZIP download task to disk so it can restart after server reboot."""
    task = {
        'resolved': resolved,
        'tmp_path': tmp_path,
        'zip_name': zip_name,
        'download_id': download_id,
        'total': total,
        'started': time.time(),
    }
    _save_json(ZIP_RESUME_FILE, task)

def _clear_zip_task():
    """Remove persisted ZIP task (completed, cancelled, or failed)."""
    try:
        if os.path.exists(ZIP_RESUME_FILE):
            os.remove(ZIP_RESUME_FILE)
    except OSError:
        pass

# ── Persistent copy resume ──
COPY_RESUME_FILE = _data_path('copy_resume.json')

def _save_copy_task(resolved, dest_dir, total, on_conflict, username):
    """Persist copy task to disk so it can restart after server reboot."""
    task = {
        'resolved': resolved,
        'dest_dir': dest_dir,
        'total': total,
        'on_conflict': on_conflict,
        'username': username,
        'started': time.time(),
    }
    _save_json(COPY_RESUME_FILE, task)

def _clear_copy_task():
    """Remove persisted copy task (completed, cancelled, or failed)."""
    try:
        if os.path.exists(COPY_RESUME_FILE):
            os.remove(COPY_RESUME_FILE)
    except OSError:
        pass

def _safe_exists(path, timeout=3):
    """os.path.exists via thread pool – won't D-state on sleeping drives."""
    try:
        return _fs_call(os.path.exists, path, timeout=timeout)
    except (TimeoutError, Exception):
        return False

def _safe_isdir(path, timeout=3):
    """os.path.isdir via thread pool – won't D-state on sleeping drives."""
    try:
        return _fs_call(os.path.isdir, path, timeout=timeout)
    except (TimeoutError, Exception):
        return False

def _resume_interrupted_copy():
    """Check for a persisted copy task and restart it after server reboot."""
    task = _load_json(COPY_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('resolved', [])
    dest_dir = task.get('dest_dir', '')
    on_conflict = task.get('on_conflict', 'rename')
    username = task.get('username')
    started = task.get('started', 0)

    if time.time() - started > 86400:
        _clear_copy_task()
        return
    if not resolved or not dest_dir or not _safe_isdir(dest_dir):
        _clear_copy_task()
        return

    valid = [p for p in resolved if _safe_exists(p)]
    if not valid:
        _clear_copy_task()
        return

    total = _count_items(valid)
    if total == 0:
        _clear_copy_task()
        return

    _fm = _fileop_channels['fm']
    with _fileop_lock:
        if _fm['active']:
            return
        _fm['active'] = True
        _fm['operation'] = 'copy'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    cur_user = {'username': username} if username else None
    elog('files', 'info', f'Resuming copy after restart: {len(valid)} items → {dest_dir}')
    socketio.start_background_task(_bg_copy, valid, dest_dir, total, on_conflict, cur_user)


# ── Persistent move resume ──
MOVE_RESUME_FILE = _data_path('move_resume.json')

def _save_move_task(resolved, dest_dir, total, on_conflict, dest_user_path, username):
    """Persist move task to disk so it can restart after server reboot."""
    task = {
        'resolved': resolved,
        'dest_dir': dest_dir,
        'total': total,
        'on_conflict': on_conflict,
        'dest_user_path': dest_user_path,
        'username': username,
        'started': time.time(),
    }
    _save_json(MOVE_RESUME_FILE, task)

def _clear_move_task():
    """Remove persisted move task (completed, cancelled, or failed)."""
    try:
        if os.path.exists(MOVE_RESUME_FILE):
            os.remove(MOVE_RESUME_FILE)
    except OSError:
        pass

def _resume_interrupted_move():
    """Check for a persisted move task and restart it after server reboot."""
    task = _load_json(MOVE_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('resolved', [])
    dest_dir = task.get('dest_dir', '')
    on_conflict = task.get('on_conflict', 'rename')
    dest_user_path = task.get('dest_user_path', '')
    username = task.get('username')
    started = task.get('started', 0)

    if time.time() - started > 86400:
        _clear_move_task()
        return
    if not resolved or not dest_dir or not _safe_isdir(dest_dir):
        _clear_move_task()
        return

    # For moves, only resume sources that still exist (not yet moved)
    valid = [p for p in resolved if _safe_exists(p)]
    if not valid:
        _clear_move_task()
        return

    total = _count_items(valid)
    if total == 0:
        _clear_move_task()
        return

    _fm = _fileop_channels['fm']
    with _fileop_lock:
        if _fm['active']:
            return
        _fm['active'] = True
        _fm['operation'] = 'move'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    cur_user = {'username': username} if username else None
    elog('files', 'info', f'Resuming move after restart: {len(valid)} items → {dest_dir}')
    socketio.start_background_task(_bg_move, valid, dest_dir, total, on_conflict, dest_user_path, cur_user)


# ── Persistent compress (in-place) resume ──
COMPRESS_RESUME_FILE = _data_path('compress_resume.json')

def _save_compress_task(resolved, archive_path, fmt, total, username):
    """Persist compress task to disk so it can restart after server reboot."""
    task = {
        'resolved': resolved,
        'archive_path': archive_path,
        'format': fmt,
        'total': total,
        'username': username,
        'started': time.time(),
    }
    _save_json(COMPRESS_RESUME_FILE, task)

def _clear_compress_task():
    """Remove persisted compress task (completed, cancelled, or failed)."""
    try:
        if os.path.exists(COMPRESS_RESUME_FILE):
            os.remove(COMPRESS_RESUME_FILE)
    except OSError:
        pass

def _resume_interrupted_compress():
    """Check for a persisted compress task and restart it after server reboot."""
    task = _load_json(COMPRESS_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('resolved', [])
    archive_path = task.get('archive_path', '')
    fmt = task.get('format', 'zip')
    total = task.get('total', 0)
    username = task.get('username')
    started = task.get('started', 0)

    # Ignore tasks older than 1 day (stale)
    if time.time() - started > 86400:
        _clear_compress_task()
        return

    if not resolved or not archive_path:
        _clear_compress_task()
        return

    # Verify at least one source still exists
    valid = [p for p in resolved if _safe_exists(p)]
    if not valid:
        _clear_compress_task()
        return

    # Recount items (files may have changed)
    total = _count_items(valid)
    if total == 0:
        _clear_compress_task()
        return

    # Remove partial archives from previous attempt (both final and temp paths)
    for p in (archive_path, archive_path + '.ethos_archive_tmp'):
        if _safe_exists(p):
            try: os.remove(p)
            except OSError: pass

    cur_user = {'username': username} if username else None

    with _fileop_lock:
        if _fileop_state['active']:
            return  # something else already running
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'compress'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False

    elog('files', 'info', f'Resuming compression after restart: {os.path.basename(archive_path)} ({total} files)')
    socketio.start_background_task(_bg_compress, valid, archive_path, fmt, total, cur_user)

def _resume_interrupted_zip():
    """Check for a persisted ZIP task and restart it after server reboot."""
    task = _load_json(ZIP_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('resolved', [])
    zip_name = task.get('zip_name', 'download.zip')
    download_id = task.get('download_id')
    total = task.get('total', 0)
    started = task.get('started', 0)

    # Ignore tasks older than 1 day (stale)
    if time.time() - started > 86400:
        _clear_zip_task()
        return

    if not resolved or not download_id:
        _clear_zip_task()
        return

    # Verify at least one source still exists
    valid = [p for p in resolved if _safe_exists(p)]
    if not valid:
        _clear_zip_task()
        return

    # Recount items (files may have changed)
    total = _count_items(valid)
    if total == 0:
        _clear_zip_task()
        return

    # Generate fresh tmp_path (old partial file is gone after restart)
    tmp_path = os.path.join('/tmp', f'nasos_dl_{download_id}.zip')

    with _fileop_lock:
        if _fileop_state['active']:
            return  # something else already running
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'download'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False

    elog('files', 'info', f'Resuming ZIP preparation after restart: {zip_name} ({total} files)')
    socketio.start_background_task(_bg_download_zip, valid, tmp_path, zip_name, download_id, total)

def _cleanup_stale_ethos_tmp(data_root=None):
    """Remove leftover .ethos_tmp partial files from previous crash/abort.

    Scans user home directories for .ethos_tmp files older than 1 hour and
    removes them.  Also clears stale upload chunk directories.

    Only scans /home and data-disk home — external mounts (/media, /mnt)
    are skipped because walking large FUSE-mounted drives (e.g. NTFS)
    blocks the gevent event loop and .ethos_tmp files only exist on
    paths managed by EthOS file operations.
    """
    cutoff = time.time() - 3600  # 1 hour

    # Determine scan roots: user home directories only
    scan_roots = []
    try:
        scan_roots.append('/home')
        try:
            dd = _get_data_disk()
            if dd:
                home_on_dd = os.path.join(dd, 'home')
                if _safe_isdir(home_on_dd) and home_on_dd not in scan_roots:
                    scan_roots.append(home_on_dd)
        except Exception:
            pass
    except Exception:
        pass

    _SKIP_DIRS = {'.trash', '.thumbs', '.Trash-1000', '.Trash-0', 'preview', '.snapshots'}

    def _scan_dir(start_path):
        try:
            for dirpath, dirs, files in os.walk(start_path):
                # Prune directories that should never be walked for temp cleanup
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                for fn in files:
                    if fn.endswith('.ethos_tmp') or fn.endswith('.ethos_upload_tmp') or fn.endswith('.ethos_mv_tmp'):
                        fp = os.path.join(dirpath, fn)
                        try:
                            if os.path.getmtime(fp) < cutoff:
                                os.remove(fp)
                        except OSError:
                            pass
                # Clean up stale .ethos_tmp_dir and .ethos_mv_tmp_dir partial directory copies
                for dn in list(dirs):
                    if dn.endswith('.ethos_tmp_dir') or dn.endswith('.ethos_mv_tmp_dir'):
                        dp = os.path.join(dirpath, dn)
                        try:
                            if os.path.getmtime(dp) < cutoff:
                                shutil.rmtree(dp, ignore_errors=True)
                                dirs.remove(dn)
                        except OSError:
                            pass
        except Exception:
            pass

    for root in scan_roots:
        try:
            _fs_call(_scan_dir, root, timeout=30)
        except TimeoutError:
            print(f'[cleanup] Skipping hung path: {root}')
        except Exception:
            pass

    # Also prune stale upload chunk dirs
    try:
        if os.path.isdir(_UPLOAD_TMP_DIR):
            chunk_cutoff = time.time() - _UPLOAD_SESSION_TTL
            for entry in os.scandir(_UPLOAD_TMP_DIR):
                if entry.is_dir():
                    try:
                        if entry.stat().st_mtime < chunk_cutoff:
                            shutil.rmtree(entry.path, ignore_errors=True)
                    except OSError:
                        pass
    except Exception:
        pass


def _bg_prewarm_home_listing():
    """Pre-warm the listing cache for all user home directories at startup.

    Runs 12 s after server boot so the first FM open for any user is instant.
    Uses the preload semaphore to avoid competing with real requests.
    """
    try:
        home_roots = ['/home']
        dd = _get_data_disk()
        if dd:
            home_roots.append(os.path.join(dd, 'home'))

        dirs_to_warm = []
        for root in home_roots:
            if not os.path.isdir(root):
                continue
            try:
                for entry in os.scandir(root):
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'):
                        dirs_to_warm.append(entry.path)
            except (PermissionError, OSError):
                pass

        for rpath in dirs_to_warm:
            if _listdir_cache_get(rpath) is not None:
                continue  # already warm
            _preload_sem.acquire()
            try:
                if _listdir_cache_get(rpath) is not None:
                    continue
                items_pre = []
                _mtime = None
                try:
                    _mtime = os.path.getmtime(rpath)
                except OSError:
                    pass
                try:
                    for entry in sorted(os.scandir(rpath),
                                        key=lambda e: (not e.is_dir(), e.name.lower())):
                        if entry.name in ('.trash', '.thumbs') and entry.is_dir():
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                            items_pre.append({
                                'name': entry.name,
                                'is_dir': entry.is_dir(),
                                'is_link': entry.is_symlink(),
                                'size': st.st_size if not entry.is_dir() else 0,
                                'modified': st.st_mtime,
                                'permissions': oct(st.st_mode)[-3:],
                            })
                        except (PermissionError, OSError):
                            pass
                    _listdir_cache_set(rpath, items_pre, mtime=_mtime)
                except (PermissionError, OSError):
                    pass
            finally:
                _preload_sem.release()
            gevent.sleep(0.05)  # yield between home dirs to stay low-priority
    except Exception:
        pass


def _fileop_emit(event, data):
    socketio.emit(event, data)

def _ch_for(operation):
    """Return channel key ('bg' or 'fm') for a given operation name."""
    return _OP_CHANNEL.get(operation, 'bg')

def _fileop_progress(operation, current_file, done, total, ch=None):
    if ch is None:
        ch = _ch_for(operation)
    pct = round(done / total * 100, 1) if total > 0 else 0
    prog = {'operation': operation, 'current_file': current_file,
            'done': done, 'total': total, 'percent': pct, 'channel': ch}
    with _fileop_lock:
        _fileop_channels[ch]['progress'] = prog
    _fileop_emit('fileop_progress', prog)

def _fileop_cancelled(ch='bg'):
    """Check if current file operation on *ch* was cancelled."""
    with _fileop_lock:
        return _fileop_channels[ch].get('cancel', False)

def _fileop_is_paused(ch='bg'):
    """Check if current file operation on *ch* is paused."""
    with _fileop_lock:
        return _fileop_channels[ch].get('paused', False)

def _fileop_finish(operation, success, message, ch=None):
    if ch is None:
        ch = _ch_for(operation)
    result = {'status': 'completed' if success else 'failed',
              'message': message, 'time': time.time(), 'operation': operation, 'channel': ch}
    slot = _fileop_channels[ch]
    with _fileop_lock:
        slot['active'] = False
        slot['operation'] = None
        slot['progress'] = None
        slot['result'] = result
        slot['cancel'] = False
        slot['paused'] = False
        slot['_proc'] = None
    event = 'fileop_complete' if success else 'fileop_error'
    _fileop_emit(event, result)
    # Log to event log
    op_labels = {'copy': 'Copying', 'move': 'Moving',
                 'compress': 'Compressing', 'extract': 'Extracting',
                 'download': 'ZIP Download', 'transfer': 'NAS Transfer'}
    label = op_labels.get(operation, operation)
    lvl = 'info' if success else 'error'
    elog('files', lvl, f'{label}: {message}')

def get_fileop_notifications():
    notifs = []
    op_labels = {'copy': 'Copying', 'move': 'Moving',
                 'compress': 'Compressing', 'extract': 'Extracting',
                 'download': 'ZIP Download', 'transfer': 'NAS Transfer'}
    results = []
    with _fileop_lock:
        for _ch_key, slot in _fileop_channels.items():
            if slot['active'] and slot['progress']:
                p = slot['progress']
                label = op_labels.get(p.get('operation', ''), 'Operation')
                notifs.append({
                    'type': 'progress',
                    'title': f'{label} files',
                    'message': f'{p["done"]}/{p["total"]} — {p["percent"]}%',
                    'time': time.time(),
                    'action': {'app': 'file-manager', 'tab': ''}
                })
            r = slot.get('result')
            if r:
                results.append(r)
    for r in results:
        if r.get('time') and (time.time() - r['time']) < 300:
            label = op_labels.get(r.get('operation', ''), 'Operation')
            if r['status'] == 'completed':
                notifs.append({
                    'type': 'success', 'title': f'{label} completed',
                    'message': r['message'], 'time': r['time'],
                    'action': {'app': 'file-manager', 'tab': ''}
                })
            else:
                notifs.append({
                    'type': 'error', 'title': f'{label} — error',
                    'message': r['message'], 'time': r['time'],
                    'action': {'app': 'file-manager', 'tab': ''}
                })
    return notifs


@app.route('/api/files/operation-status', methods=['GET'])
@require_auth
def fileop_status():
    """Return current file operation status (both channels)."""
    channels = {}
    with _fileop_lock:
        for ch_key, slot in _fileop_channels.items():
            channels[ch_key] = {
                'active': slot['active'],
                'operation': slot['operation'],
                'progress': slot['progress'],
                'result': slot.get('result'),
                'meta': slot.get('meta'),
                'paused': slot.get('paused', False),
            }
    # Backward compat: top-level fields from first active channel (or bg)
    primary = channels.get('bg', {})
    for ch_key in ('bg', 'fm'):
        if channels.get(ch_key, {}).get('active'):
            primary = channels[ch_key]
            break
    st = dict(primary)
    st['channels'] = channels
    # Include pending downloads that haven't been picked up yet
    pending = []
    for did, info in list(_pending_downloads.items()):
        if os.path.isfile(info.get('path', '')):
            pending.append({'download_id': did, 'name': info.get('name', 'download.zip')})
    st['pending_downloads'] = pending
    return jsonify(st)


@app.route('/api/files/cancel-operation', methods=['POST'])
@require_auth
def cancel_fileop():
    """Cancel the currently active file operation on a specific channel."""
    data = request.get_json(silent=True) or {}
    ch = data.get('channel', None)
    # Auto-detect channel if not provided
    if not ch:
        with _fileop_lock:
            for ch_key in ('bg', 'fm'):
                if _fileop_channels[ch_key]['active']:
                    ch = ch_key
                    break
    if not ch:
        return jsonify({'error': 'No active operation'}), 400
    slot = _fileop_channels.get(ch)
    if not slot:
        return jsonify({'error': 'Unknown channel'}), 400
    with _fileop_lock:
        if not slot['active']:
            return jsonify({'error': 'No active operation on this channel'}), 400
        slot['cancel'] = True
        slot['paused'] = False
        proc = slot.get('_proc')
    # Resume first if paused, then kill subprocess (rsync)
    if proc:
        try:
            proc.send_signal(signal.SIGCONT)
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
    return jsonify({'cancelled': True, 'channel': ch, 'message': 'Cancellation in progress…'})


@app.route('/api/files/pause-operation', methods=['POST'])
@require_auth
def pause_fileop():
    """Pause or resume the currently active file operation on a specific channel."""
    data = request.get_json(silent=True) or {}
    ch = data.get('channel', None)
    # Auto-detect channel if not provided
    if not ch:
        with _fileop_lock:
            for ch_key in ('bg', 'fm'):
                if _fileop_channels[ch_key]['active']:
                    ch = ch_key
                    break
    if not ch:
        return jsonify({'error': 'No active operation'}), 400
    slot = _fileop_channels.get(ch)
    if not slot:
        return jsonify({'error': 'Unknown channel'}), 400
    with _fileop_lock:
        if not slot['active']:
            return jsonify({'error': 'No active operation on this channel'}), 400
        operation = slot.get('operation')
        if operation not in ('transfer', 'download', 'compress', 'copy', 'move'):
            return jsonify({'error': 'Pause is not supported for this operation'}), 400
        currently_paused = slot.get('paused', False)
        new_paused = not currently_paused
        slot['paused'] = new_paused
        proc = slot.get('_proc')

    # For transfer operations with rsync subprocess, use SIGSTOP/SIGCONT
    if proc:
        try:
            if new_paused:
                proc.send_signal(signal.SIGSTOP)
            else:
                proc.send_signal(signal.SIGCONT)
        except Exception:
            pass

    _fileop_emit('fileop_paused', {'paused': new_paused, 'channel': ch})
    return jsonify({'paused': new_paused, 'channel': ch, 'message': 'Wstrzymano' if new_paused else 'Wznowiono'})


def _count_items(paths):
    """Count total files/dirs recursively for progress."""
    total = 0
    for p in paths:
        if os.path.isdir(p):
            for _, dirs, files in os.walk(p):
                total += len(files) + len(dirs)
        else:
            total += 1
    return total

def _chown_to_user(path, username=None):
    """Change ownership of a single path to the given (or current) user."""
    if not username:
        user = get_current_user()
        username = user['username'] if user else None
    if not username:
        return
    try:
        pw = pwd.getpwnam(username)
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass

def _chown_recursive(path, username=None):
    """Recursively chown a path (file or dir) to the given (or current) user."""
    if not username:
        user = get_current_user()
        username = user['username'] if user else None
    if not username:
        return
    try:
        pw = pwd.getpwnam(username)
        uid, gid = pw.pw_uid, pw.pw_gid
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                os.chown(root, uid, gid)
                for d in dirs:
                    os.chown(os.path.join(root, d), uid, gid)
                for f in files:
                    os.chown(os.path.join(root, f), uid, gid)
        else:
            os.chown(path, uid, gid)
    except (KeyError, OSError):
        pass

def _copy_with_progress(real_src, target, op_label, done_ref, total, gevent_yield=True, username=None, cancel_ref=None, pause_fn=None, partial_files=None):
    """Copy file or directory tree with progress updates.

    cancel_ref: a mutable list [False] — set to True to abort.
    pause_fn:   callable returning True while paused.
    partial_files: list to track files being written (for cleanup on cancel).
    """
    def _check():
        """Return True if cancelled; honour pause."""
        if cancel_ref is not None and cancel_ref[0]:
            return True
        if pause_fn is not None:
            while pause_fn():
                if cancel_ref is not None and cancel_ref[0]:
                    return True
                gevent.sleep(0.2)
        return False

    if os.path.isdir(real_src):
        os.makedirs(target, exist_ok=True)
        _chown_to_user(target, username)
        for item in os.listdir(real_src):
            if _check():
                raise InterruptedError('Copy cancelled')
            s = os.path.join(real_src, item)
            d = os.path.join(target, item)
            if os.path.isdir(s):
                _copy_with_progress(s, d, op_label, done_ref, total, gevent_yield, username, cancel_ref, pause_fn, partial_files)
            else:
                tmp_d = d + '.ethos_tmp'
                if partial_files is not None:
                    partial_files.append(tmp_d)
                try:
                    _fs_call(shutil.copy2, s, tmp_d, timeout=300)
                    os.replace(tmp_d, d)
                    if partial_files is not None and tmp_d in partial_files:
                        partial_files.remove(tmp_d)
                except Exception:
                    try: os.remove(tmp_d)
                    except OSError: pass
                    raise
                _chown_to_user(d, username)
                done_ref[0] += 1
                if done_ref[0] % 3 == 0 or done_ref[0] == total:
                    _fileop_progress(op_label, item, done_ref[0], total)
                    if gevent_yield:
                        gevent.sleep(0)
        done_ref[0] += 1  # count the dir itself
    else:
        tmp_target = target + '.ethos_tmp'
        if partial_files is not None:
            partial_files.append(tmp_target)
        try:
            _fs_call(shutil.copy2, real_src, tmp_target, timeout=300)
            os.replace(tmp_target, target)
            if partial_files is not None and tmp_target in partial_files:
                partial_files.remove(tmp_target)
        except Exception:
            try: os.remove(tmp_target)
            except OSError: pass
            raise
        _chown_to_user(target, username)
        done_ref[0] += 1
        _fileop_progress(op_label, os.path.basename(real_src), done_ref[0], total)
        if gevent_yield:
            gevent.sleep(0)

def safe_path(user_path):
    """Resolve user path with role-aware sandboxing.

    - Normal mode: restricted to allowed roots and own home scope.
    - Sudo mode (admin): full filesystem access, except other users' home dirs.
    """
    cur = get_current_user()
    sudo_mode = bool(cur and cur.get('role') == 'admin' and _is_sudo_mode())
    result = _safe_path_util(
        user_path, isolate_home=True,
        current_user=cur['username'] if cur else None,
        sudo_mode=sudo_mode,
    )
    # Non-sudo users stay strictly in their own home directory.
    if result and cur and not sudo_mode:
        user_home = _get_user_home(cur['username'])
        if not (result == user_home or result.startswith(user_home + '/')):
            return None
    return result


# ─── Folder Passwords ────────────────────────────────────────
FOLDER_PASSWORDS_FILE = _data_path('folder_passwords.json')
_unlocked_folders = {}  # token -> set of unlocked folder paths
_uf_lock = _threading.Lock()

# Brute-force protection for folder unlock (per token, keyed by token+path)
_folder_unlock_attempts = {}  # key -> {'count': int, 'first': float, 'locked_until': float}
_folder_unlock_lock = _threading.Lock()
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


@app.route('/api/files/folder-password', methods=['GET'])
@require_auth
def folder_password_list():
    """List all password-protected folders."""
    passwords = _load_folder_passwords()
    return jsonify({'folders': list(passwords.keys())})


@app.route('/api/files/folder-password', methods=['POST'])
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
    real = safe_path(path)
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


@app.route('/api/files/folder-password', methods=['DELETE'])
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


@app.route('/api/files/folder-unlock', methods=['POST'])
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


@app.route('/api/files/folder-lock', methods=['POST'])
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

@app.route('/api/files/permissions')
@require_auth
def files_get_permissions():
    """GET /api/files/permissions?path=<path>
    Returns detailed permissions info: symbolic mode, octal, owner, group.
    """
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    real = safe_path(path)
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


@app.route('/api/files/chmod', methods=['POST'])
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
    real = safe_path(path)
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


@app.route('/api/files/chown', methods=['POST'])
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

    real = safe_path(path)
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


@app.route('/api/files/favorites')
@require_auth
def files_favorites_list():
    return jsonify(_load_favorites())


@app.route('/api/files/favorites', methods=['POST'])
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


@app.route('/api/files/favorites', methods=['DELETE'])
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


# ─── Photo Favorites (per-user, shared with Gallery) ─────────────────
_GALLERY_FAVS_GLOBAL = _data_path('gallery_favorites.json')  # legacy
_OLD_PHOTO_FAVS_FILE = _data_path('photo_favorites.json')

def _gallery_favs_file():
    cur = get_current_user()
    username = cur['username'] if cur else None
    if username:
        return _user_data_path('gallery_favorites.json', username)
    return _GALLERY_FAVS_GLOBAL

def _migrate_photo_favs():
    """One-time migration: merge old photo_favorites.json into gallery_favorites.json."""
    if not os.path.isfile(_OLD_PHOTO_FAVS_FILE):
        return
    try:
        with open(_OLD_PHOTO_FAVS_FILE, 'r') as f:
            old = json.load(f)
        if not isinstance(old, list) or not old:
            os.rename(_OLD_PHOTO_FAVS_FILE, _OLD_PHOTO_FAVS_FILE + '.bak')
            return
        # Migrate into global file (will be further migrated per-user on first access)
        favs = []
        if os.path.isfile(_GALLERY_FAVS_GLOBAL):
            try:
                with open(_GALLERY_FAVS_GLOBAL, 'r') as f:
                    favs = json.load(f)
            except Exception:
                pass
        existing = {f['path'] for f in favs}
        for path in old:
            if isinstance(path, str) and path not in existing:
                favs.append({'path': path, 'added': time.time()})
                existing.add(path)
        with open(_GALLERY_FAVS_GLOBAL, 'w') as f:
            json.dump(favs, f, ensure_ascii=False, indent=2)
        os.rename(_OLD_PHOTO_FAVS_FILE, _OLD_PHOTO_FAVS_FILE + '.bak')
    except Exception:
        pass

_migrate_photo_favs()

def _load_gallery_favs():
    return _load_json(_gallery_favs_file(), [])

def _save_gallery_favs(favs):
    _save_json(_gallery_favs_file(), favs)


@app.route('/api/photos/favorites')
@require_auth
def photo_favorites_list():
    """Return flat list of paths (compat with file manager)."""
    favs = _load_gallery_favs()
    return jsonify([f['path'] for f in favs if 'path' in f])


@app.route('/api/photos/favorites', methods=['POST'])
@require_auth
def photo_favorites_add():
    data = request.get_json(force=True)
    path = data.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    real = safe_path(path)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'File does not exist'}), 404
    favs = _load_gallery_favs()
    if not any(f['path'] == path for f in favs):
        favs.insert(0, {'path': path, 'added': time.time()})
        _save_gallery_favs(favs)
    paths = [f['path'] for f in favs]
    return jsonify({'ok': True, 'favorites': paths})


@app.route('/api/photos/favorites', methods=['DELETE'])
@require_auth
def photo_favorites_remove():
    data = request.get_json(force=True)
    path = data.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    favs = _load_gallery_favs()
    favs = [f for f in favs if f['path'] != path]
    _save_gallery_favs(favs)
    paths = [f['path'] for f in favs]
    return jsonify({'ok': True, 'favorites': paths})


@app.route('/api/photos/favorites/files')
@require_auth
def photo_favorites_files():
    """Return file info for all favorited photos (that still exist)."""
    favs = _load_gallery_favs()
    items = []
    cleaned = []
    for fav in favs:
        fpath = fav.get('path', '')
        real = safe_path(fpath)
        if not real or not os.path.isfile(real):
            continue  # skip deleted
        cleaned.append(fav)
        try:
            stat = os.stat(real)
            items.append({
                'name': os.path.basename(fpath),
                'path': fpath,
                'is_dir': False,
                'is_link': os.path.islink(real),
                'size': stat.st_size,
                'modified': stat.st_mtime,
                'permissions': oct(stat.st_mode)[-3:]
            })
        except Exception:
            pass
    # Clean up stale entries
    if len(cleaned) != len(favs):
        _save_gallery_favs(cleaned)
    return jsonify({'path': '/__photo_favorites__', 'items': items})


@app.route('/api/files/list')
@require_auth
@cache.cached(timeout=10, key_prefix=make_user_cache_key)
def files_list():
    path = request.args.get('path', '/')
    # Normalize double slashes
    while '//' in path:
        path = path.replace('//', '/')

    _cur = get_current_user()
    _sudo_mode = bool(_cur and _cur.get('role') == 'admin' and _is_sudo_mode())

    # Show virtual root with allowed directories (normal mode only).
    # In sudo mode, '/' is the real filesystem root.
    if (path == '/' or path == '') and not _sudo_mode:
        me = _cur['username'] if _cur else None

        # ── Normal mode: virtual root with allowed dirs ──
        items = []
        # Show user's own home directory (resolves to data disk if configured)
        if me:
            home_dir = _get_user_home(me)
            if os.path.isdir(home_dir):
                try:
                    st = os.stat(home_dir)
                    _owner, _group = _get_owner_group(st)
                    items.append({
                        'name': me,
                        'is_dir': True, 'is_link': False,
                        'size': 0, 'modified': st.st_mtime,
                        'permissions': oct(st.st_mode)[-3:],
                        'permissions_symbolic': _mode_to_symbolic(st.st_mode),
                        'can_read': os.access(home_dir, os.R_OK),
                        'can_write': os.access(home_dir, os.W_OK),
                        'owner': _owner,
                        'group': _group,
                        'home': True,
                        'home_path': home_dir,
                    })
                except OSError:
                    pass
        return jsonify({'items': items, 'path': '/'})

    real_path = safe_path(path)

    # If path looks like it's on a /media/devmon drive, try to wake / remount it
    if real_path and '/media/' in real_path:
        wake_path = real_path
        if wake_path:
            try:
                _fs_call(try_wake_path, wake_path, timeout=25)
            except TimeoutError:
                return jsonify({'error': 'Disk not responding — try again shortly'}), 504

    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    # Check if attempting to list inside a protected folder
    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    passwords = _load_folder_passwords()
    # ── Home isolation: when listing /home or {data_disk}/home, show only own dir ──
    _home_only_user = None
    _home_dirs = ['/home']
    _dd = _get_data_disk()
    if _dd:
        _home_dirs.append(os.path.join(_dd, 'home'))
    if real_path in _home_dirs:
        _cur = get_current_user()
        if _cur:
            _home_only_user = _cur['username']

    # ── Short-lived listing cache (skip for home-isolation and password-protected) ──
    _cache_key = real_path if not _home_only_user else None
    if _cache_key:
        cached_items = _listdir_cache_get(_cache_key)
        if cached_items is not None:
            # ETag based on directory mtime — allows 304 responses on repeat polls
            try:
                dir_mtime = os.path.getmtime(real_path)
                etag = hashlib.md5(f'{real_path}:{dir_mtime}'.encode()).hexdigest()[:16]
                if request.headers.get('If-None-Match') == etag:
                    return '', 304
                resp = jsonify({'path': path, 'items': cached_items})
                resp.headers['ETag'] = etag
                resp.headers['Cache-Control'] = 'no-cache'
                return resp
            except OSError:
                pass
            return jsonify({'path': path, 'items': cached_items})

    # ETag from directory mtime (computed once, reused below)
    _etag = None
    _dir_mtime = None
    if _cache_key:
        try:
            _dir_mtime = os.path.getmtime(real_path)
            _etag = hashlib.md5(f'{real_path}:{_dir_mtime}'.encode()).hexdigest()[:16]
        except OSError:
            pass

    items = []
    _on_external = '/media/' in real_path or '/mnt/' in real_path

    def _do_scandir():
        return sorted(os.scandir(real_path), key=lambda e: (not e.is_dir(), e.name.lower()))

    try:
        if _on_external:
            dir_entries = _fs_call(_do_scandir, timeout=10)
        else:
            dir_entries = _do_scandir()
        for entry in dir_entries:
            # Hide per-drive trash directories and .thumbs directories
            if entry.name in ('.trash', '.thumbs') and entry.is_dir():
                continue
            # Home isolation – skip other users' directories
            if _home_only_user and entry.name != _home_only_user:
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
                owner, group = _get_owner_group(stat)
                item_data = {
                    'name': entry.name,
                    'is_dir': entry.is_dir(),
                    'is_link': entry.is_symlink(),
                    'size': stat.st_size if not entry.is_dir() else 0,
                    'modified': stat.st_mtime,
                    'permissions': oct(stat.st_mode)[-3:],
                    'permissions_symbolic': _mode_to_symbolic(stat.st_mode),
                    'permissions_octal': '0' + oct(stat.st_mode & 0o7777)[2:],
                    'can_read': os.access(entry.path, os.R_OK),
                    'can_write': os.access(entry.path, os.W_OK),
                    'owner': owner,
                    'group': group,
                }
                # Mark protected dirs with locked flag
                if entry.is_dir():
                    child_path = (path.rstrip('/') + '/' + entry.name) if path != '/' else ('/' + entry.name)
                    if child_path in passwords:
                        item_data['locked'] = not _is_folder_unlocked(child_path)
                        item_data['protected'] = True
                items.append(item_data)
            except (PermissionError, OSError):
                items.append({
                    'name': entry.name,
                    'is_dir': entry.is_dir(),
                    'is_link': False,
                    'size': 0,
                    'modified': 0,
                    'permissions': '---',
                    'permissions_symbolic': '---------',
                })
    except PermissionError:
        return jsonify({'error': t('auth.no_permission')}), 403
    except TimeoutError:
        return jsonify({'error': 'Disk not responding — try again shortly'}), 504

    if _cache_key:
        _listdir_cache_set(_cache_key, items, mtime=_dir_mtime)

    resp = jsonify({'path': path, 'items': items})
    if _etag:
        resp.headers['ETag'] = _etag
        resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/api/files/search')
@require_auth
def files_search():
    path = request.args.get('path', '/')
    query = request.args.get('q', '').lower()
    real_path = safe_path(path)
    if not real_path or not query:
        return jsonify({'items': []})

    # Cache passwords once per request (not per os.walk iteration)
    pw_folders = _load_folder_passwords()
    data_real = os.path.realpath(DATA_ROOT)
    try:
        max_depth = int(request.args.get('depth', 6))
    except (ValueError, TypeError):
        max_depth = 6
    base_depth = real_path.rstrip('/').count('/')

    import time as _time
    deadline = _time.monotonic() + 5  # 5 second timeout

    # ── Home isolation for search: never descend into other users' homes ──
    _search_home_user = None
    _home_dirs = ['/home']
    _dd = _get_data_disk()
    if _dd:
        _home_dirs.append(os.path.join(_dd, 'home'))
    _scur = get_current_user()
    if _scur:
        _search_home_user = _scur['username']

    results = []
    truncated = False
    try:
        for root, dirs, files in os.walk(real_path):
            # Enforce depth limit
            cur_depth = root.rstrip('/').count('/') - base_depth
            if cur_depth >= max_depth:
                dirs[:] = []
                continue
            # Enforce time limit
            if _time.monotonic() > deadline:
                truncated = True
                break
            # Skip .trash, .thumbs, and password-protected directories
            def _dir_allowed(d):
                if d in ('.trash', '.thumbs'):
                    return False
                rel = '/' + os.path.relpath(os.path.join(root, d), data_real)
                if rel in pw_folders and not _is_folder_unlocked(rel):
                    return False
                # Home isolation – when walking a home dir, only descend into own dir
                if _search_home_user and root in _home_dirs and d != _search_home_user:
                    return False
                return True
            dirs[:] = [d for d in dirs if _dir_allowed(d)]
            for name in dirs + files:
                if query in name.lower():
                    rel = os.path.relpath(os.path.join(root, name), data_real)
                    full = os.path.join(root, name)
                    try:
                        stat = os.stat(full)
                        results.append({
                            'name': name,
                            'path': '/' + rel,
                            'is_dir': os.path.isdir(full),
                            'size': stat.st_size,
                            'modified': stat.st_mtime
                        })
                    except Exception:
                        pass
                    if len(results) >= 200:
                        truncated = True
                        break
            if truncated:
                break
    except Exception:
        pass
    return jsonify({'items': results, 'truncated': truncated})


@app.route('/api/files/dir-sizes', methods=['POST'])
@require_auth
def files_dir_sizes():
    """Calculate total size for a list of directories (with TTL cache)."""
    data = request.json or {}
    paths = data.get('paths', [])
    if not paths or not isinstance(paths, list):
        return jsonify({'error': 'Provide a list of paths'}), 400

    deadline = time.monotonic() + 10  # 10s total timeout

    results = {}
    for p in paths[:50]:  # limit to 50 dirs per request
        # Return cached result if available and fresh
        cached = _dirsize_cache_get(p)
        if cached is not None:
            results[p] = cached
            continue
        real = safe_path(p)
        if not real or not os.path.isdir(real):
            results[p] = 0
            continue
        total = 0
        timed_out = False
        try:
            stack = [real]
            while stack:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                cur = stack.pop()
                try:
                    for entry in os.scandir(cur):
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in ('.trash', '.thumbs'):
                                    stack.append(entry.path)
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
                except (PermissionError, OSError):
                    pass
        except (PermissionError, OSError):
            pass
        if not timed_out:
            _dirsize_cache_set(p, total)
        results[p] = total
        if timed_out:
            break
    return jsonify({'sizes': results})


def _calc_dir_size_worker(job_id, path, real):
    """Background greenlet: calculate dir size and store in job + cache.

    Uses an iterative os.scandir stack for better performance on large trees
    (avoids repeated string joins from os.walk and leverages DirEntry caching).
    """
    _PSEUDO_FS = {'/proc', '/sys', '/dev', '/run'}
    # Skip pseudo-filesystems early
    if real in _PSEUDO_FS or any(real.startswith(p + '/') for p in _PSEUDO_FS):
        _dirsize_cache_set(path, 0)
        with _dirsize_bg_lock:
            if job_id in _dirsize_bg_jobs:
                _dirsize_bg_jobs[job_id].update({'size': 0, 'done': True, 'error': False, 'ts': time.monotonic()})
            _dirsize_running_by_path.pop(path, None)
        return

    total = 0
    _scan_count = 0
    try:
        stack = [real]
        while stack:
            cur = stack.pop()
            try:
                for entry in os.scandir(cur):
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in ('.trash', '.thumbs'):
                                stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
                    _scan_count += 1
                    # Yield to gevent event loop every 500 entries to avoid blocking
                    if _scan_count % 500 == 0:
                        gevent.sleep(0)
            except (PermissionError, OSError):
                pass
        _dirsize_cache_set(path, total)
        with _dirsize_bg_lock:
            if job_id in _dirsize_bg_jobs:
                _dirsize_bg_jobs[job_id].update({'size': total, 'done': True, 'error': False, 'ts': time.monotonic()})
            _dirsize_running_by_path.pop(path, None)
    except Exception:
        with _dirsize_bg_lock:
            if job_id in _dirsize_bg_jobs:
                _dirsize_bg_jobs[job_id].update({'size': 0, 'done': True, 'error': True, 'ts': time.monotonic()})
            _dirsize_running_by_path.pop(path, None)


@app.route('/api/files/dir-sizes-start', methods=['POST'])
@require_auth
def files_dir_sizes_start():
    """Start background dir-size calculations, returning job IDs.

    Request body: { "paths": ["/path/to/dir", ...] }
    Response: { "jobs": { "/path": "job_id", ... }, "cached": { "/path": size, ... } }
    """
    data = request.json or {}
    paths = data.get('paths', [])
    if not paths or not isinstance(paths, list):
        return jsonify({'error': 'Provide a list of paths'}), 400

    jobs = {}
    cached = {}
    # Prune stale completed jobs to keep _dirsize_bg_jobs tidy
    _dirsize_bg_jobs_prune()
    # Pseudo-filesystems and virtual paths that block gevent event loop
    _PSEUDO_FS_PATHS = {'/proc', '/sys', '/dev', '/run', '/snap', '/snap/bin'}

    for p in paths[:50]:
        # Skip pseudo-filesystems — scanning them would block the gevent event loop
        if p in _PSEUDO_FS_PATHS or p.startswith('/proc/') or p.startswith('/sys/') or p.startswith('/dev/'):
            cached[p] = 0
            continue
        # Return from cache immediately if fresh
        cv = _dirsize_cache_get(p)
        if cv is not None:
            cached[p] = cv
            continue
        real = safe_path(p)
        if not real or not os.path.isdir(real):
            cached[p] = 0
            continue
        # Reuse an existing running job for this path — avoids duplicate workers
        with _dirsize_bg_lock:
            existing_jid = _dirsize_running_by_path.get(p)
            if (existing_jid and existing_jid in _dirsize_bg_jobs
                    and not _dirsize_bg_jobs[existing_jid].get('done')):
                jobs[p] = existing_jid
                continue
        job_id = hashlib.md5(f'{p}:{time.monotonic()}'.encode()).hexdigest()[:12]
        with _dirsize_bg_lock:
            _dirsize_bg_jobs[job_id] = {'path': p, 'size': None, 'done': False, 'error': False, 'ts': time.monotonic()}
            _dirsize_running_by_path[p] = job_id
        gevent.spawn(_calc_dir_size_worker, job_id, p, real)
        jobs[p] = job_id
    return jsonify({'jobs': jobs, 'cached': cached})


@app.route('/api/files/dir-sizes-result', methods=['POST'])
@require_auth
def files_dir_sizes_result():
    """Poll background dir-size jobs.

    Request body: { "jobs": { "/path": "job_id", ... } }
    Response: { "sizes": { "/path": size }, "pending": ["/path", ...] }
    """
    data = request.json or {}
    jobs = data.get('jobs', {})
    sizes = {}
    pending = []
    with _dirsize_bg_lock:
        for path, job_id in jobs.items():
            entry = _dirsize_bg_jobs.get(job_id)
            if entry is None:
                # Job expired/unknown — check cache
                cv = _dirsize_cache_get(path)
                if cv is not None:
                    sizes[path] = cv
                else:
                    pending.append(path)
            elif entry['done']:
                sizes[path] = entry.get('size') or 0
                del _dirsize_bg_jobs[job_id]
            else:
                pending.append(path)
    return jsonify({'sizes': sizes, 'pending': pending})


@app.route('/api/files/download')
@require_auth
def files_download():
    path = request.args.get('path', '')
    real_path = safe_path(path)
    if not real_path or not os.path.isfile(real_path):
        return jsonify({'error': 'File not found'}), 404
    # Enforce allowed roots even for admins — prevents reading /etc/shadow etc.
    if not any(real_path == r or real_path.startswith(r + '/') for r in _ALLOWED_ROOTS):
        return jsonify({'error': 'Access denied'}), 403
    # Block download from protected folders
    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked
    resp = send_file(real_path, as_attachment=True, conditional=True)
    # Advertise byte-range support so browsers can resume interrupted downloads (HTTP 206)
    resp.headers['Accept-Ranges'] = 'bytes'

    # Log download start (only for full files, not partial ranges to avoid spam)
    if 'Range' not in request.headers:
        cur = get_current_user()
        username = cur['username'] if cur else 'unknown'
        elog('files', 'info', f'File download: {path}', {'user': username, 'path': path, 'size': os.path.getsize(real_path)})

    return resp


@app.route('/api/files/download-zip', methods=['POST'])
@require_auth
def files_download_zip():
    """Create a ZIP from selected files/folders with progress, return download ID."""
    data = request.json or {}
    sources = data.get('sources', [])
    if not sources:
        return jsonify({'error': 'No files to download'}), 400

    resolved = []
    for s in sources:
        rp = safe_path(s)
        if rp and os.path.exists(rp):
            resolved.append(rp)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    total = _count_items(resolved)

    # Determine archive filename
    if len(resolved) == 1:
        zip_name = os.path.splitext(os.path.basename(resolved[0]))[0] + '.zip'
    else:
        zip_name = 'download.zip'

    download_id = secrets.token_hex(16)
    tmp_path = os.path.join('/tmp', f'nasos_dl_{download_id}.zip')

    with _fileop_lock:
        if _fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'download'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False

    socketio.start_background_task(_bg_download_zip, resolved, tmp_path, zip_name, download_id, total)
    _save_zip_task(resolved, tmp_path, zip_name, download_id, total)
    elog('files', 'info', f'Started ZIP preparation: {zip_name} ({total} files)')
    return jsonify({'async': True, 'download_id': download_id,
                    'message': f'Preparing {zip_name} ({total} files)…'})


# Pending download temp files: id -> {path, name, time}
_pending_downloads = {}


def _bg_download_zip(resolved, tmp_path, zip_name, download_id, total):
    """Background ZIP creation for download with progress."""
    done = 0
    cancelled = False
    CHUNK_SIZE = 1 * 1024 * 1024       # 1 MB read chunks
    LARGE_FILE_THRESHOLD = 50 * 1024 * 1024  # 50 MB — store without compression

    def _check_cancel_pause():
        """Check cancel flag & honour pause; return True if cancelled."""
        if _fileop_cancelled():
            return True
        while _fileop_is_paused():
            gevent.sleep(0.5)
            if _fileop_cancelled():
                return True
        return False

    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for src in resolved:
                if _check_cancel_pause():
                    cancelled = True; break
                base_dir = os.path.dirname(src)
                if os.path.isdir(src):
                    for root, dirs, files in os.walk(src):
                        if _check_cancel_pause():
                            cancelled = True; break
                        rel = os.path.relpath(root, base_dir)
                        if rel != '.':
                            try:
                                zf.write(root, rel)
                            except Exception:
                                pass
                        for fn in files:
                            if _check_cancel_pause():
                                cancelled = True; break
                            full = os.path.join(root, fn)
                            arcname = os.path.relpath(full, base_dir)
                            try:
                                fsize = os.path.getsize(full)
                                if fsize > LARGE_FILE_THRESHOLD:
                                    # Large file — store without compression, write in chunks
                                    info = zipfile.ZipInfo.from_file(full, arcname)
                                    info.compress_type = zipfile.ZIP_STORED
                                    with zf.open(info, 'w') as dest:
                                        with open(full, 'rb') as fsrc:
                                            while True:
                                                if _check_cancel_pause():
                                                    cancelled = True; break
                                                chunk = fsrc.read(CHUNK_SIZE)
                                                if not chunk:
                                                    break
                                                dest.write(chunk)
                                                gevent.sleep(0)
                                else:
                                    zf.write(full, arcname)
                            except (PermissionError, OSError):
                                pass  # skip unreadable files
                            if cancelled:
                                break
                            done += 1
                            _fileop_progress('download', fn, done, total)
                            gevent.sleep(0)
                        if cancelled:
                            break
                        done += len(dirs)
                        gevent.sleep(0)
                else:
                    try:
                        fsize = os.path.getsize(src)
                        bname = os.path.basename(src)
                        if fsize > LARGE_FILE_THRESHOLD:
                            info = zipfile.ZipInfo.from_file(src, bname)
                            info.compress_type = zipfile.ZIP_STORED
                            with zf.open(info, 'w') as dest:
                                with open(src, 'rb') as fsrc:
                                    while True:
                                        if _check_cancel_pause():
                                            cancelled = True; break
                                        chunk = fsrc.read(CHUNK_SIZE)
                                        if not chunk:
                                            break
                                        dest.write(chunk)
                                        gevent.sleep(0)
                        else:
                            zf.write(src, bname)
                    except (PermissionError, OSError):
                        pass
                    if cancelled:
                        break
                    done += 1
                    _fileop_progress('download', os.path.basename(src), done, total)
                    gevent.sleep(0)

        if cancelled:
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass
            _clear_zip_task()
            _fileop_finish('download', False, 'Cancelled')
            return

        size_mb = round(os.path.getsize(tmp_path) / (1024*1024), 1)
        _pending_downloads[download_id] = {
            'path': tmp_path, 'name': zip_name, 'time': time.time()
        }
        _clear_zip_task()
        _fileop_finish('download', True, f'{zip_name} ({size_mb} MB)')
        # Emit download_id so frontend can trigger browser download
        _fileop_emit('fileop_download_ready', {'download_id': download_id, 'name': zip_name})
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass
        _clear_zip_task()
        _fileop_finish('download', False, str(e))


@app.route('/api/files/download-zip/<download_id>/status')
@require_auth
def files_download_zip_status(download_id):
    """Check if a prepared ZIP is ready for download."""
    info = _pending_downloads.get(download_id)
    if info and os.path.isfile(info['path']):
        return jsonify({'ready': True, 'name': info.get('name', 'download.zip')})
    return jsonify({'ready': False})


@app.route('/api/files/download-zip/<download_id>')
@require_auth
def files_download_zip_file(download_id):
    """Serve a prepared ZIP and clean up."""
    info = _pending_downloads.pop(download_id, None)
    if not info or not os.path.isfile(info['path']):
        return jsonify({'error': 'File not found or expired'}), 404

    resp = send_file(info['path'], as_attachment=True,
                     download_name=info['name'], mimetype='application/zip',
                     conditional=False)
    @resp.call_on_close
    def _cleanup():
        try:
            if os.path.exists(info['path']):
                os.remove(info['path'])
        except OSError:
            pass
    return resp


# THUMB_CACHE_DIR, THUMBS_DIR_NAME, _thumb_cache_key, _local_thumb_path — imported from utils


def _purge_thumb_cache(real_path):
    """Remove cached thumbnails for a file or all files under a directory."""
    target = os.path.realpath(real_path)
    # Purge from centralized cache
    try:
        for fn in os.listdir(THUMB_CACHE_DIR):
            if not fn.endswith('.meta'):
                continue
            meta_fp = os.path.join(THUMB_CACHE_DIR, fn)
            try:
                with open(meta_fp) as f:
                    stored_path = f.read().strip()
            except Exception:
                continue
            if stored_path == target or stored_path.startswith(target + '/'):
                base = fn[:-5]  # remove .meta
                webp_fp = os.path.join(THUMB_CACHE_DIR, base + '.webp')
                for fp in (meta_fp, webp_fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
    except Exception:
        pass
    # Purge from local .thumbs/ directory
    try:
        if os.path.isfile(target):
            parent = os.path.dirname(target)
            basename = os.path.basename(target)
            name, _ = os.path.splitext(basename)
            thumbs_dir = os.path.join(parent, THUMBS_DIR_NAME)
            if os.path.isdir(thumbs_dir):
                for fn in os.listdir(thumbs_dir):
                    if fn.startswith(name + '_') and fn.endswith('.webp'):
                        try:
                            os.remove(os.path.join(thumbs_dir, fn))
                        except OSError:
                            pass
        elif os.path.isdir(target):
            thumbs_dir = os.path.join(target, THUMBS_DIR_NAME)
            if os.path.isdir(thumbs_dir):
                import shutil
                shutil.rmtree(thumbs_dir, ignore_errors=True)
    except Exception:
        pass


# Set of supported image extensions for thumbnail pre-generation
_THUMB_IMAGE_EXTS = frozenset(('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif'))

# Global semaphore: limits simultaneous thumbnail generation greenlets.
# Prevents memory overload when multiple users / large directories trigger
# pregeneration of hundreds of thumbnails concurrently.
import gevent.lock as _gevent_lock
_PREGENERATE_CONCURRENCY = 8
_pregenerate_sem = _gevent_lock.BoundedSemaphore(_PREGENERATE_CONCURRENCY)

# Global semaphore: limits simultaneous listing-preload greenlets.
_PRELOAD_CONCURRENCY = 12
_preload_sem = _gevent_lock.BoundedSemaphore(_PRELOAD_CONCURRENCY)


@app.route('/api/files/pregenerate-thumbs', methods=['POST'])
@require_auth
def files_pregenerate_thumbs():
    """Pre-generate WebP thumbnails for all images in a directory (background greenlets).

    Called by the frontend when entering thumb view so that thumbnails are warm
    before the user scrolls to them.  Returns immediately; generation runs async.
    """
    data = request.json or {}
    path = data.get('path', '')
    w = min(int(data.get('w', 120)), 400)
    h = min(int(data.get('h', 120)), 400)
    real_path = safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    def _generate_one(fpath):
        try:
            thumbs_dir, local_path = _local_thumb_path(fpath, w, h)
            if os.path.isfile(local_path):
                mtime = os.path.getmtime(fpath)
                if os.path.getmtime(local_path) >= mtime:
                    return  # already fresh — skip
            # Acquire global semaphore before generating to limit memory pressure
            _pregenerate_sem.acquire()
            try:
                generate_thumbnail(fpath, w, h)
            finally:
                _pregenerate_sem.release()
        except Exception:
            pass

    queued = 0
    try:
        for entry in os.scandir(real_path):
            if not entry.is_file(follow_symlinks=False):
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in _THUMB_IMAGE_EXTS:
                gevent.spawn(_generate_one, entry.path)
                queued += 1
                if queued >= 200:  # cap to avoid run-away greenlet creation
                    break
    except (PermissionError, OSError):
        pass
    return jsonify({'ok': True, 'queued': queued})


@app.route('/api/files/preload-cache', methods=['POST'])
@require_auth
def files_preload_cache():
    """Pre-populate the listing cache for a directory and its immediate subdirs.

    Called by the frontend on hover/prefetch so that navigating into a folder
    is instant (cache hit in files_list).  Returns immediately.
    """
    data = request.json or {}
    path = data.get('path', '')
    real_path = safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    def _preload_one(rpath):
        """Scan *rpath* and store result in the listing cache (semaphore-gated)."""
        if _listdir_cache_get(rpath) is not None:
            return  # already fresh
        _preload_sem.acquire()
        try:
            # Re-check under semaphore — another greenlet may have just populated it
            if _listdir_cache_get(rpath) is not None:
                return
            items_pre = []
            _mtime = None
            try:
                _mtime = os.path.getmtime(rpath)
            except OSError:
                pass
            for entry in sorted(os.scandir(rpath), key=lambda e: (not e.is_dir(), e.name.lower())):
                if entry.name in ('.trash', '.thumbs') and entry.is_dir():
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    items_pre.append({
                        'name': entry.name,
                        'is_dir': entry.is_dir(),
                        'is_link': entry.is_symlink(),
                        'size': st.st_size if not entry.is_dir() else 0,
                        'modified': st.st_mtime,
                        'permissions': oct(st.st_mode)[-3:],
                    })
                except (PermissionError, OSError):
                    pass
            _listdir_cache_set(rpath, items_pre, mtime=_mtime)
        except (PermissionError, OSError):
            pass
        finally:
            _preload_sem.release()

    # Preload the directory itself
    gevent.spawn(_preload_one, real_path)
    queued = 1
    # Preload immediate subdirectories (limit to 20 to avoid overload)
    try:
        for entry in os.scandir(real_path):
            if entry.is_dir(follow_symlinks=False) and entry.name not in ('.trash', '.thumbs'):
                gevent.spawn(_preload_one, entry.path)
                queued += 1
                if queued >= 21:
                    break
    except (PermissionError, OSError):
        pass
    return jsonify({'ok': True, 'queued': queued})


@app.route('/api/files/preview')
@require_auth
def files_preview():
    path = request.args.get('path', '')
    real_path = safe_path(path)
    if not real_path or not os.path.isfile(real_path):
        return jsonify({'error': 'File not found'}), 404

    # Block preview from protected folders
    blocked = _require_folder_access(path)
    if blocked is not None:
        return blocked

    w = request.args.get('w', type=int)
    h = request.args.get('h', type=int)

    # If thumbnail size requested, serve cached thumbnail with ETag for 304 support
    if w and h:
        try:
            mtime = os.path.getmtime(real_path)
            etag = _thumb_cache_key(real_path, mtime, w, h)
            if request.headers.get('If-None-Match') == etag:
                return '', 304
            resp = generate_thumbnail(real_path, w, h)
            resp.headers['ETag'] = etag
            return resp
        except Exception:
            return generate_thumbnail(real_path, w, h)
    return send_file(real_path)


def _sanitize_filename(filename):
    """Strip path separators and dangerous characters from an uploaded filename."""
    # Take only the basename — remove any directory components
    name = os.path.basename(filename)
    # Collapse .. and strip control chars / null bytes
    name = name.replace('\x00', '').replace('..', '_')
    # Remove shell-dangerous characters
    name = re.sub(r'[<>:"|?*\\]', '_', name)
    # Strip leading dots (hidden files attack) — optional, keep at least one char
    name = name.lstrip('.')
    if not name:
        name = 'upload_' + str(int(time.time()))
    return name[:255]


@app.route('/api/files/upload', methods=['POST'])
@require_auth
def files_upload():
    path = request.form.get('path', '/')
    real_path = safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    files = request.files.getlist('files')
    rel_paths = request.form.getlist('rel_paths')  # optional: relative paths for folder uploads

    uploaded = []
    errors = []
    for idx, f in enumerate(files):
        # Use relative path if provided (folder upload), else just filename
        if rel_paths and idx < len(rel_paths) and rel_paths[idx]:
            rel = rel_paths[idx]
            # Sanitize each path component
            parts = [_sanitize_filename(p) for p in rel.replace('\\', '/').split('/') if p and p != '..']
            if not parts:
                parts = [_sanitize_filename(f.filename)]
            # Create intermediate directories
            if len(parts) > 1:
                subdir = os.path.join(real_path, *parts[:-1])
                try:
                    os.makedirs(subdir, exist_ok=True)
                except PermissionError:
                    errors.append({'name': f.filename, 'error': 'No write permission'})
                    continue
                # chown intermediate dirs
                cur = real_path
                for p in parts[:-1]:
                    cur = os.path.join(cur, p)
                    _chown_to_user(cur)
            filepath = os.path.join(real_path, *parts)
        else:
            safe_name = _sanitize_filename(f.filename)
            filepath = os.path.join(real_path, safe_name)
        # Atomic write: save to temp then rename to avoid partial files
        tmp_filepath = filepath + '.ethos_upload_tmp'
        try:
            f.save(tmp_filepath)
            os.replace(tmp_filepath, filepath)
        except PermissionError:
            try: os.remove(tmp_filepath)
            except OSError: pass
            errors.append({'name': f.filename, 'error': 'No write permission'})
            continue
        except OSError as e:
            try: os.remove(tmp_filepath)
            except OSError: pass
            if e.errno == errno.ENOSPC:
                errors.append({'name': f.filename, 'error': 'No disk space available'})
            else:
                errors.append({'name': f.filename, 'error': f'Write error: {e.strerror}'})
            continue
        _chown_to_user(filepath)
        uploaded.append(os.path.relpath(filepath, real_path))
    if uploaded:
        elog('files', 'info', f'Uploaded {len(uploaded)} file(s) to {path}', {'files': uploaded[:20]})
        _listdir_cache_invalidate(path)
        _dirsize_cache_invalidate(real_path)
    if errors and not uploaded:
        # All files failed — return error status
        first_err = errors[0]['error']
        status = 403 if first_err == 'No write permission' else 507 if 'disk space' in first_err else 500
        return jsonify({'error': first_err, 'errors': errors}), status
    return jsonify({'uploaded': uploaded, 'errors': errors})


# ── Chunked / resumable upload ──────────────────────────────────────────────
# For large files: client splits into chunks, sends them one by one with session ID.
# Server assembles chunks and writes final file atomically.

_upload_sessions = {}          # session_id → session_dict
_upload_sessions_lock = _threading.Lock()
_UPLOAD_SESSION_TTL = 3600     # 1 hour — stale sessions are cleaned up
# Use persistent storage so sessions survive server restarts (/tmp is tmpfs and cleared on reboot)
_UPLOAD_TMP_DIR = '/opt/ethos/uploads/chunks'

def _upload_session_tmpdir(session_id):
    return os.path.join(_UPLOAD_TMP_DIR, session_id)

def _upload_sessions_prune():
    """Remove stale upload sessions and their temp dirs."""
    now = time.monotonic()
    with _upload_sessions_lock:
        stale = [sid for sid, s in _upload_sessions.items()
                 if now - s.get('created_at', now) > _UPLOAD_SESSION_TTL]
    for sid in stale:
        tmpdir = _upload_session_tmpdir(sid)
        try: shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception: pass
        with _upload_sessions_lock:
            _upload_sessions.pop(sid, None)

def _persist_upload_session(session):
    """Save upload session metadata to disk so it survives app restarts."""
    tmpdir = session.get('tmpdir', '')
    if not tmpdir:
        return
    meta = {k: v for k, v in session.items() if k != 'uploaded_chunks'}
    try:
        _save_json(os.path.join(tmpdir, 'session.json'), meta)
    except Exception:
        pass

def _restore_upload_sessions():
    """Scan upload tmpdir for persisted sessions and restore them into memory.

    Rebuilds uploaded_chunks by checking which chunk files actually exist on disk.
    Called once at startup so clients can resume interrupted chunked uploads.
    """
    if not os.path.isdir(_UPLOAD_TMP_DIR):
        return
    restored = 0
    now = time.monotonic()
    try:
        for entry in os.scandir(_UPLOAD_TMP_DIR):
            if not entry.is_dir():
                continue
            session_file = os.path.join(entry.path, 'session.json')
            if not os.path.isfile(session_file):
                continue
            try:
                session = _load_json(session_file, None)
                if not session or not isinstance(session, dict):
                    continue
                session_id = session.get('session_id', '')
                if not session_id:
                    continue
                # Skip sessions older than TTL (created_at is monotonic so use mtime as fallback)
                created_at = session.get('created_at', 0)
                mtime = entry.stat().st_mtime
                age = time.time() - mtime
                if age > _UPLOAD_SESSION_TTL:
                    continue
                # Rebuild uploaded_chunks from actual chunk files on disk
                num_chunks = session.get('num_chunks', 0)
                tmpdir = session.get('tmpdir', entry.path)
                existing_chunks = [
                    i for i in range(num_chunks)
                    if os.path.isfile(os.path.join(tmpdir, f'chunk_{i:06d}'))
                ]
                session['uploaded_chunks'] = existing_chunks
                session['tmpdir'] = tmpdir
                # created_at in restored sessions is wall-clock offset; set to now minus age
                session['created_at'] = now - age
                with _upload_sessions_lock:
                    if session_id not in _upload_sessions:
                        _upload_sessions[session_id] = session
                        restored += 1
            except Exception:
                pass
    except Exception:
        pass
    if restored:
        elog('files', 'info', f'Restored {restored} upload sessions after restart')


@app.route('/api/files/upload-chunk-init', methods=['POST'])
@require_auth
def files_upload_chunk_init():
    """Initialize a resumable chunked upload session.

    Body: {path, filename, size, chunk_size, create_dir}
    create_dir=true: create destination directory if it doesn't exist (needed for folder uploads).
    Returns: {session_id, uploaded_chunks: [], chunk_size}
    """
    data = request.json or {}
    dest_path = data.get('path', '/')
    filename = data.get('filename', '')
    total_size = int(data.get('size', 0))
    chunk_size = int(data.get('chunk_size', 5 * 1024 * 1024))  # default 5 MB
    create_dir = bool(data.get('create_dir', False))

    real_path = safe_path(dest_path)
    if not real_path:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.isdir(real_path):
        if create_dir:
            try:
                os.makedirs(real_path, exist_ok=True)
                _chown_to_user(real_path)
            except Exception as e:
                return jsonify({'error': f'Cannot create folder: {e}'}), 400
        else:
            return jsonify({'error': 'Invalid path'}), 400
    if not filename:
        return jsonify({'error': 'Filename is required'}), 400
    if total_size <= 0:
        return jsonify({'error': 'Invalid file size'}), 400

    safe_name = _sanitize_filename(filename)
    session_id = secrets.token_hex(16)
    tmpdir = _upload_session_tmpdir(session_id)
    os.makedirs(tmpdir, exist_ok=True)

    num_chunks = max(1, (total_size + chunk_size - 1) // chunk_size)
    session = {
        'session_id': session_id,
        'dest_path': dest_path,
        'real_path': real_path,
        'filename': safe_name,
        'total_size': total_size,
        'chunk_size': chunk_size,
        'num_chunks': num_chunks,
        'uploaded_chunks': [],
        'created_at': time.monotonic(),
        'tmpdir': tmpdir,
        'username': (get_current_user() or {}).get('username'),
    }
    with _upload_sessions_lock:
        _upload_sessions[session_id] = session

    # Persist session metadata to disk so it survives app restarts
    _persist_upload_session(session)

    return jsonify({'session_id': session_id, 'uploaded_chunks': [], 'num_chunks': num_chunks})


@app.route('/api/files/upload-chunk', methods=['POST'])
@require_auth
def files_upload_chunk():
    """Upload a single chunk for a resumable upload session.

    Form fields: session_id, chunk_index, checksum (optional SHA-256 hex of this chunk)
    File: 'chunk' — the binary data
    Returns: {ok: true, uploaded_chunks: [...]}
    """
    session_id = request.form.get('session_id', '')
    chunk_index = request.form.get('chunk_index', type=int)
    chunk_file = request.files.get('chunk')
    expected_checksum = request.form.get('checksum', '')  # optional SHA-256 of this chunk

    if chunk_index is None or not chunk_file:
        return jsonify({'error': 'Missing data'}), 400

    with _upload_sessions_lock:
        session = _upload_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'Unknown upload session', 'expired': True}), 404

    if chunk_index < 0 or chunk_index >= session['num_chunks']:
        return jsonify({'error': 'Invalid chunk index'}), 400

    chunk_path = os.path.join(session['tmpdir'], f'chunk_{chunk_index:06d}')
    tmp_chunk = chunk_path + '.tmp'
    try:
        chunk_file.save(tmp_chunk)
        # Verify SHA-256 checksum if provided
        if expected_checksum:
            import hashlib as _hashlib
            sha = _hashlib.sha256()
            with open(tmp_chunk, 'rb') as cf:
                for buf in iter(lambda: cf.read(65536), b''):
                    sha.update(buf)
            actual = sha.hexdigest()
            if actual != expected_checksum.lower():
                try: os.remove(tmp_chunk)
                except OSError: pass
                return jsonify({'error': f'Chunk {chunk_index} integrity error: checksum mismatch'}), 400
        os.replace(tmp_chunk, chunk_path)
    except Exception as e:
        try: os.remove(tmp_chunk)
        except OSError: pass
        return jsonify({'error': f'Chunk write error: {e}'}), 500

    with _upload_sessions_lock:
        if chunk_index not in session['uploaded_chunks']:
            session['uploaded_chunks'].append(chunk_index)
        uploaded = list(session['uploaded_chunks'])

    return jsonify({'ok': True, 'uploaded_chunks': uploaded})


@app.route('/api/files/upload-status/<session_id>', methods=['GET'])
@require_auth
def files_upload_status(session_id):
    """Return current upload session status."""
    with _upload_sessions_lock:
        session = _upload_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'Nieznana sesja', 'expired': True}), 404
    return jsonify({
        'session_id': session_id,
        'uploaded_chunks': list(session['uploaded_chunks']),
        'num_chunks': session['num_chunks'],
        'total_size': session['total_size'],
    })


@app.route('/api/files/upload-complete', methods=['POST'])
@require_auth
def files_upload_complete():
    """Finalize a chunked upload: assemble chunks into the destination file.

    Body: {session_id}
    Returns: {ok: true, filename, sha256 (if verify_checksum was requested)}
    """
    data = request.json or {}
    session_id = data.get('session_id', '')
    verify_checksum = bool(data.get('verify_checksum', False))  # optional: compute SHA-256 of assembled file

    with _upload_sessions_lock:
        session = _upload_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'Unknown upload session', 'expired': True}), 404

    num_chunks = session['num_chunks']
    uploaded = set(session['uploaded_chunks'])
    missing = [i for i in range(num_chunks) if i not in uploaded]
    if missing:
        return jsonify({'error': f'Missing chunks: {missing[:10]}', 'missing_chunks': missing}), 400

    real_path = session['real_path']
    filename = session['filename']
    tmpdir = session['tmpdir']
    username = session.get('username')

    if not os.path.isdir(real_path):
        return jsonify({'error': 'Destination folder does not exist'}), 400

    dest_file = os.path.join(real_path, filename)
    tmp_dest = dest_file + '.ethos_upload_tmp'
    file_sha256 = None

    try:
        import hashlib as _hashlib
        sha = _hashlib.sha256() if verify_checksum else None
        with open(tmp_dest, 'wb') as out:
            for i in range(num_chunks):
                chunk_path = os.path.join(tmpdir, f'chunk_{i:06d}')
                with open(chunk_path, 'rb') as cf:
                    while True:
                        buf = cf.read(65536)
                        if not buf:
                            break
                        out.write(buf)
                        if sha:
                            sha.update(buf)
        if sha:
            file_sha256 = sha.hexdigest()
        os.replace(tmp_dest, dest_file)
        _chown_to_user(dest_file, username)
    except Exception as e:
        try: os.remove(tmp_dest)
        except OSError: pass
        return jsonify({'error': f'File assembly error: {e}'}), 500
    finally:
        # Clean up temp chunks regardless of outcome
        try: shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception: pass
        with _upload_sessions_lock:
            _upload_sessions.pop(session_id, None)

    dest_path = session['dest_path']
    elog('files', 'info', f'Uploaded (chunked) {filename} to {dest_path}')
    _listdir_cache_invalidate(dest_path)
    _dirsize_cache_invalidate(real_path)
    result = {'ok': True, 'filename': filename}
    if file_sha256:
        result['sha256'] = file_sha256
    return jsonify(result)


@app.route('/api/files/upload-abort', methods=['POST'])
@require_auth
def files_upload_abort():
    """Abort a chunked upload session and clean up temp files.

    Body: {session_id}
    """
    data = request.json or {}
    session_id = data.get('session_id', '')

    with _upload_sessions_lock:
        session = _upload_sessions.pop(session_id, None)

    if session:
        tmpdir = session.get('tmpdir', '')
        if tmpdir:
            try: shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception: pass
    return jsonify({'ok': True})


@app.route('/api/files/mkdir', methods=['POST'])
@require_auth
def files_mkdir():
    data = request.json or {}
    real_path = safe_path(data.get('path', ''))
    if not real_path:
        return jsonify({'error': 'Invalid path'}), 400

    blocked = _require_folder_access(data.get('path', ''))
    if blocked is not None:
        return blocked

    try:
        os.makedirs(real_path, exist_ok=True)
        _chown_to_user(real_path)
        _dirsize_cache_invalidate(os.path.dirname(real_path))
        _listdir_cache_invalidate(data.get('path', ''))
        elog('files', 'info', f'Created folder: {data.get("path", "")}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────── Phone Sync ───────────────────────────

PHONE_SYNC_META_DIR = _data_path('phone_sync')
os.makedirs(PHONE_SYNC_META_DIR, exist_ok=True)

def _get_sync_username():
    """Get username from the current auth token."""
    user = get_current_user()
    return user['username'] if user else 'admin'

def _sync_meta_path(username=None):
    if not username:
        username = _get_sync_username()
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '', username)
    return os.path.join(PHONE_SYNC_META_DIR, f'{safe}.json')

def _default_dest(username=None):
    if not username:
        username = _get_sync_username()
    photos_folder, _ = _get_photo_folders()
    return f'home/{username}/{photos_folder}/Phone'

def _load_sync_meta(username=None):
    data = _load_json(_sync_meta_path(username), None)
    if data is not None:
        return data
    return {'synced': {}, 'dest': _default_dest(username)}

def _save_sync_meta(meta, username=None):
    _save_json(_sync_meta_path(username), meta)


@app.route('/api/sync/check', methods=['POST'])
@require_auth
def sync_check():
    """Check which files are already synced. Checks both metadata AND actual files on disk."""
    data = request.json or {}
    files = data.get('files', [])
    meta = _load_sync_meta()
    synced = meta.get('synced', {})
    dest_base = meta.get('dest', _default_dest())
    needed = []
    already = []
    for f in files:
        key = f'{f["name"]}_{f["size"]}'
        # Check metadata first
        if key in synced:
            already.append(f['name'])
            continue
        # Also check if file physically exists on disk with same name and size
        rel_path = f.get('rel_path', f['name'])
        dest_dir = safe_path(os.path.join(dest_base, os.path.dirname(rel_path)))
        if dest_dir:
            dest_file = os.path.join(dest_dir, os.path.basename(f['name']))
            if os.path.isfile(dest_file):
                try:
                    existing_size = os.path.getsize(dest_file)
                    if existing_size == int(f.get('size', 0)):
                        # File exists on disk with same size — mark as synced
                        synced[key] = {'ts': time.time(), 'path': rel_path}
                        already.append(f['name'])
                        continue
                except OSError:
                    pass
        needed.append(f['name'])
    # Save any newly discovered existing files
    if len(already) > len([k for k in synced if synced.get(k, {}).get('ts', 0) == 0]):
        meta['synced'] = synced
        _save_sync_meta(meta)
    return jsonify({'needed': needed, 'already': already})


@app.route('/api/sync/upload', methods=['POST'])
@require_auth
def sync_upload():
    """Upload a single photo/video from phone. Preserves subfolder structure."""
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file'}), 400

    rel_path = request.form.get('rel_path', _sanitize_filename(f.filename))
    try:
        file_size = int(request.form.get('size', 0))
    except (ValueError, TypeError):
        file_size = 0
    mtime_ms = request.form.get('mtime_ms', '')

    meta = _load_sync_meta()
    dest_base = meta.get('dest', _default_dest())

    # Build destination path
    dest_dir = safe_path(os.path.join(dest_base, os.path.dirname(rel_path)))
    if not dest_dir:
        return jsonify({'error': 'Invalid path'}), 400
    os.makedirs(dest_dir, exist_ok=True)
    _chown_to_user(dest_dir)

    dest_file = os.path.join(dest_dir, os.path.basename(rel_path))

    # If file exists with same size, skip
    if os.path.isfile(dest_file) and os.path.getsize(dest_file) == file_size and file_size > 0:
        key = f'{os.path.basename(rel_path)}_{file_size}'
        meta['synced'][key] = {'ts': time.time(), 'path': rel_path}
        _save_sync_meta(meta)
        return jsonify({'status': 'exists', 'name': os.path.basename(rel_path)})

    # Atomic write: save to temp then rename to avoid partial files on interrupt
    tmp_dest = dest_file + '.ethos_upload_tmp'
    try:
        f.save(tmp_dest)
        os.replace(tmp_dest, dest_file)
    except Exception:
        try: os.remove(tmp_dest)
        except OSError: pass
        raise
    _chown_to_user(dest_file)

    # Record in sync meta
    actual_size = os.path.getsize(dest_file)
    key = f'{os.path.basename(rel_path)}_{actual_size}'
    meta['synced'][key] = {'ts': time.time(), 'path': rel_path}
    _save_sync_meta(meta)

    elog('sync', 'info', f'Zsynchronizowano z telefonu: {rel_path}')
    return jsonify({'status': 'uploaded', 'name': os.path.basename(rel_path)})


@app.route('/api/sync/status')
@require_auth
def sync_status():
    """Return sync statistics."""
    meta = _load_sync_meta()
    dest = meta.get('dest', _default_dest())
    real_dest = safe_path(dest)
    total_files = 0
    total_size = 0
    if real_dest and os.path.isdir(real_dest):
        for root, dirs, files in os.walk(real_dest):
            for fn in files:
                fp = os.path.join(root, fn)
                total_files += 1
                try:
                    total_size += os.path.getsize(fp)
                except OSError:
                    pass
    return jsonify({
        'synced_count': len(meta.get('synced', {})),
        'dest': dest,
        'total_files': total_files,
        'total_size': total_size,
    })


@app.route('/api/sync/config', methods=['GET', 'POST'])
@require_auth
def sync_config():
    """Get or set sync destination folder."""
    meta = _load_sync_meta()
    if request.method == 'POST':
        data = request.json or {}
        new_dest = data.get('dest', '').strip('/')
        if new_dest:
            real = safe_path(new_dest)
            if not real:
                return jsonify({'error': 'Invalid path'}), 400
            os.makedirs(real, exist_ok=True)
            _chown_to_user(real)
            meta['dest'] = new_dest
            _save_sync_meta(meta)
    return jsonify({'dest': meta.get('dest', _default_dest())})


@app.route('/api/sync/reset', methods=['POST'])
@require_auth
def sync_reset():
    """Reset sync tracking (doesn't delete files)."""
    meta = _load_sync_meta()
    meta['synced'] = {}
    _save_sync_meta(meta)
    return jsonify({'ok': True})


@app.route('/api/sync/folders')
@require_auth
def sync_folders():
    """Browse folders for sync destination picker. Returns only directories."""
    path = request.args.get('path', '/')
    real_path = safe_path(path)
    if not real_path or not os.path.isdir(real_path):
        return jsonify({'error': 'Invalid path'}), 400

    items, err = _list_dir(real_path, dirs_only=True)
    if err:
        return jsonify({'error': err}), 403
    folders = [i['name'] for i in items]
    return jsonify({'path': path, 'folders': folders})


@app.route('/api/sync/qr-code')
def sync_qr_code():
    """Generate QR code PNG for the given URL."""
    import segno
    import io
    url = request.args.get('url', '')
    if not url:
        return 'Missing url parameter', 400
    qr = segno.make(url, error='L')
    buf = io.BytesIO()
    qr.save(buf, kind='png', scale=6, border=4)
    buf.seek(0)
    return send_file(buf, mimetype='image/png', download_name='qr.png')


# ─────────────────────────── Trash (Kosz) ───────────────────────────

TRASH_DIR = _data_path('.trash')               # centralized fallback
TRASH_META_FILE = _data_path('trash_meta.json')
TRASH_RETENTION_DAYS = 30

os.makedirs(TRASH_DIR, exist_ok=True)


def _get_trash_dir_for_path(real_path):
    """Return the .trash directory on the same filesystem as real_path.

    Finds the mount-point root by walking up until st_dev changes,
    then places .trash there.  Falls back to TRASH_DIR on error.
    """
    try:
        target_dev = os.stat(real_path).st_dev
        cur = os.path.dirname(os.path.realpath(real_path))
        while True:
            parent = os.path.dirname(cur)
            if parent == cur:
                # reached /
                break
            if os.stat(parent).st_dev != target_dev:
                # cur is the mount root
                break
            cur = parent
        trash = os.path.join(cur, '.trash')
        os.makedirs(trash, exist_ok=True)
        return trash
    except Exception:
        os.makedirs(TRASH_DIR, exist_ok=True)
        return TRASH_DIR


def _resolve_trash_path(item):
    """Return the full path to a trashed item, handling both old and new entries."""
    td = item.get('trash_dir', TRASH_DIR)
    return os.path.join(td, item['trash_id'])


def _load_trash_meta():
    return _load_json(TRASH_META_FILE, [])

def _save_trash_meta(meta):
    _save_json(TRASH_META_FILE, meta)


def _trash_cleanup():
    """Remove trash items older than TRASH_RETENTION_DAYS."""
    meta = _load_trash_meta()
    now = time.time()
    cutoff = now - TRASH_RETENTION_DAYS * 86400
    remaining = []
    removed = 0
    for item in meta:
        if item.get('deleted_at', 0) < cutoff:
            trash_path = _resolve_trash_path(item)
            try:
                if os.path.isdir(trash_path):
                    shutil.rmtree(trash_path)
                elif os.path.exists(trash_path):
                    os.remove(trash_path)
                removed += 1
            except Exception:
                pass
        else:
            remaining.append(item)
    if removed > 0:
        _save_trash_meta(remaining)
        elog('files', 'info', f'Trash: automatically removed {removed} items older than {TRASH_RETENTION_DAYS} days')


def _start_trash_scheduler():
    """Run trash cleanup every 6 hours."""
    def _loop():
        while True:
            gevent.sleep(6 * 3600)
            try:
                _trash_cleanup()
            except Exception:
                pass
    gevent.spawn(_loop)
    # Also run once on startup (delayed)
    gevent.spawn_later(30, _trash_cleanup)


@app.route('/api/files/delete', methods=['DELETE'])
@require_auth
def files_delete():
    """Move files to trash instead of permanent delete."""
    data = request.json or {}
    paths = data.get('paths', [])
    permanent = data.get('permanent', False)
    if isinstance(data.get('path'), str):
        paths = [data['path']]

    deleted = []
    errors = []
    meta = _load_trash_meta()

    # Track deleted items for logging
    deleted_log = []

    for p in paths:
        # Security check
        blocked = _require_folder_access(p)
        if blocked is not None:
            errors.append(f'Access denied: {p} (protected folder)')
            continue

        real_path = safe_path(p)
        if not real_path:
            errors.append(f'Invalid path: {p}')
            continue
        if not os.path.exists(real_path):
            errors.append(f'Does not exist: {p}')
            continue
        try:
            _purge_thumb_cache(real_path)
            _dirsize_cache_invalidate(os.path.dirname(real_path))
            _listdir_cache_invalidate(p)

            if permanent:
                # Permanent delete (from trash empty or explicit)
                if os.path.isdir(real_path):
                    shutil.rmtree(real_path)
                else:
                    os.remove(real_path)
            else:
                # Move to trash — on the same filesystem for instant rename
                local_trash = _get_trash_dir_for_path(real_path)
                trash_id = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}"
                trash_dest = os.path.join(local_trash, trash_id)
                shutil.move(real_path, trash_dest)

                # Compute size
                if os.path.isdir(trash_dest):
                    size = sum(
                        os.path.getsize(os.path.join(dp, f))
                        for dp, _, fns in os.walk(trash_dest)
                        for f in fns
                    )
                else:
                    size = os.path.getsize(trash_dest)

                meta.append({
                    'trash_id': trash_id,
                    'trash_dir': local_trash,
                    'original_path': p,
                    'name': os.path.basename(real_path),
                    'is_dir': os.path.isdir(trash_dest),
                    'size': size,
                    'deleted_at': time.time(),
                })
            deleted.append(p)
        except Exception as e:
            errors.append(str(e))

    if not permanent:
        _save_trash_meta(meta)

    if deleted:
        label = 'Permanently deleted' if permanent else 'Moved to trash'
        elog('files', 'info', f'{label} {len(deleted)} items', {'paths': deleted})
    if errors:
        elog('files', 'error', f'Deletion errors: {len(errors)}', {'errors': errors})
    return jsonify({'deleted': deleted, 'errors': errors})


@app.route('/api/files/trash')
@require_auth
def files_trash_list():
    """List items in trash."""
    meta = _load_trash_meta()
    now = time.time()
    items = []
    for item in meta:
        days_left = max(0, TRASH_RETENTION_DAYS - int((now - item.get('deleted_at', 0)) / 86400))
        items.append({
            **item,
            'days_left': days_left,
            'deleted_date': datetime.fromtimestamp(item.get('deleted_at', 0)).strftime('%Y-%m-%d %H:%M'),
        })
    items.sort(key=lambda x: x.get('deleted_at', 0), reverse=True)
    return jsonify({'items': items, 'retention_days': TRASH_RETENTION_DAYS})


@app.route('/api/files/trash/restore', methods=['POST'])
@require_auth
def files_trash_restore():
    """Restore items from trash to original location."""
    data = request.json or {}
    trash_ids = data.get('trash_ids', [])
    if isinstance(data.get('trash_id'), str):
        trash_ids = [data['trash_id']]

    meta = _load_trash_meta()
    restored = []
    errors = []

    for tid in trash_ids:
        item = next((m for m in meta if m['trash_id'] == tid), None)
        if not item:
            errors.append(f'Not found in trash: {tid}')
            continue

        trash_path = _resolve_trash_path(item)
        if not os.path.exists(trash_path):
            meta = [m for m in meta if m['trash_id'] != tid]
            errors.append(f'File disappeared from trash: {item["name"]}')
            continue

        original = safe_path(item['original_path'])
        if not original:
            errors.append(f'Invalid original path: {item["original_path"]}')
            continue

        try:
            # Ensure parent directory exists
            parent = os.path.dirname(original)
            os.makedirs(parent, exist_ok=True)

            # Handle name conflict
            dest = original
            if os.path.exists(dest):
                base, ext = os.path.splitext(dest)
                dest = f"{base}_restored{ext}"
                counter = 1
                while os.path.exists(dest):
                    dest = f"{base}_restored_{counter}{ext}"
                    counter += 1

            shutil.move(trash_path, dest)
            _chown_recursive(dest)
            meta = [m for m in meta if m['trash_id'] != tid]
            restored.append(item['name'])
        except Exception as e:
            errors.append(f'{item["name"]}: {str(e)}')

    _save_trash_meta(meta)
    if restored:
        elog('files', 'info', f'Restored {len(restored)} items from trash', {'names': restored})
    return jsonify({'restored': restored, 'errors': errors})


@app.route('/api/files/trash/empty', methods=['POST'])
@require_auth
def files_trash_empty():
    """Permanently delete all items in trash."""
    meta = _load_trash_meta()
    removed = 0
    for item in meta:
        trash_path = _resolve_trash_path(item)
        try:
            if os.path.isdir(trash_path):
                shutil.rmtree(trash_path)
            elif os.path.exists(trash_path):
                os.remove(trash_path)
            removed += 1
        except Exception:
            pass
    _save_trash_meta([])
    elog('files', 'info', f'Trash emptied: {removed} items removed')
    return jsonify({'removed': removed})


@app.route('/api/files/trash/delete', methods=['DELETE'])
@require_auth
def files_trash_delete_permanent():
    """Permanently delete specific items from trash."""
    data = request.json or {}
    trash_ids = data.get('trash_ids', [])
    if isinstance(data.get('trash_id'), str):
        trash_ids = [data['trash_id']]

    meta = _load_trash_meta()
    removed = []

    for tid in trash_ids:
        item = next((m for m in meta if m['trash_id'] == tid), None)
        if not item:
            continue
        trash_path = _resolve_trash_path(item)
        try:
            if os.path.isdir(trash_path):
                shutil.rmtree(trash_path)
            elif os.path.exists(trash_path):
                os.remove(trash_path)
        except Exception:
            pass
        meta = [m for m in meta if m['trash_id'] != tid]
        removed.append(item['name'])

    _save_trash_meta(meta)
    if removed:
        elog('files', 'info', f'Permanently deleted from trash: {len(removed)} items')
    return jsonify({'removed': removed})


@app.route('/api/files/trash/preview')
@require_auth
def files_trash_preview():
    """Serve preview / thumbnail for a file in trash."""
    trash_id = request.args.get('id', '')
    # Optional sub-path inside trashed directory
    sub = request.args.get('sub', '')
    w = request.args.get('w', type=int)
    h = request.args.get('h', type=int)

    if not trash_id:
        return jsonify({'error': 'ID is required'}), 400

    # Look up the item in meta to find its trash_dir
    meta = _load_trash_meta()
    item = next((m for m in meta if m['trash_id'] == trash_id), None)
    item_trash_dir = item.get('trash_dir', TRASH_DIR) if item else TRASH_DIR

    trash_path = os.path.join(item_trash_dir, trash_id)
    if sub:
        trash_path = os.path.join(trash_path, sub)
    # Security: ensure stays inside the item's trash dir
    trash_path = os.path.realpath(trash_path)
    if not trash_path.startswith(os.path.realpath(item_trash_dir)):
        return jsonify({'error': 'Invalid path'}), 403

    if not os.path.isfile(trash_path):
        return jsonify({'error': 'Not found'}), 404

    if w and h:
        try:
            img = Image.open(trash_path)
            img.thumbnail((w * 2, h * 2), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='WEBP', quality=75)
            buf.seek(0)
            return send_file(buf, mimetype='image/webp', max_age=3600)
        except Exception:
            return send_file(trash_path)
    return send_file(trash_path)


# ─────────────────── Duplicate Photo Finder ───────────────────────

_dup_scan = {
    'running': False,
    'cancel': False,
    'scan_id': None,
    'phase': '',
    'scanned': 0,
    'total': 0,
    'found_groups': 0,
    'results': [],         # list of groups — populated INCREMENTALLY
    'error': None,
}

# ── Hash cache — avoids re-computing SHA256 / dhash on unchanged files ──
DUP_HASH_CACHE_FILE = _data_path('dup_hash_cache.json')

def _load_hash_cache():
    return _load_json(DUP_HASH_CACHE_FILE, {})

def _save_hash_cache(cache):
    try:
        _save_json(DUP_HASH_CACHE_FILE, cache)
    except Exception:
        pass

_MAX_HASH_CACHE_ENTRIES = 50000

def _prune_hash_cache(cache):
    """Remove oldest entries if cache exceeds max size."""
    if len(cache) <= _MAX_HASH_CACHE_ENTRIES:
        return cache
    # Keep newest entries by mtime
    sorted_keys = sorted(cache.keys(), key=lambda k: cache[k].get('mtime', 0), reverse=True)
    pruned = {k: cache[k] for k in sorted_keys[:_MAX_HASH_CACHE_ENTRIES]}
    return pruned

# ── Ignored duplicate groups ──
DUP_IGNORED_FILE = _data_path('dup_ignored.json')


def _dup_group_key(group):
    """Stable key for a dup group based on sorted file paths."""
    paths = sorted(f['path'] for f in group.get('items', []))
    return hashlib.md5('|'.join(paths).encode()).hexdigest()


def _load_dup_ignored():
    return _load_json(DUP_IGNORED_FILE, {})

def _save_dup_ignored(data):
    _save_json(DUP_IGNORED_FILE, data)

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.heic', '.heif'}


def _dhash(img, hash_size=10):
    """Compute difference hash (dhash) — perceptual hash for visual similarity."""
    img = img.convert('L').resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    w = hash_size + 1
    bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            bits.append(1 if pixels[row * w + col] < pixels[row * w + col + 1] else 0)
    return int(''.join(str(b) for b in bits), 2)


def _hamming(h1, h2):
    """Hamming distance between two integer hashes."""
    return bin(h1 ^ h2).count('1')


def _emit_new_group(group):
    """Send a newly found group to the frontend in real-time."""
    socketio.emit('dup_new_group', {
        'group': group,
        'total_groups': _dup_scan['found_groups'],
    })


def _scan_duplicates(scan_paths, mode, threshold):
    """Background task: find duplicate images — emits groups incrementally."""
    global _dup_scan
    try:
        _dup_scan['phase'] = 'Searching for images…'
        _dup_scan['scanned'] = 0
        socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': 0})

        # Load hash cache for faster re-scans
        hash_cache = _load_hash_cache()
        cache_hits = 0

        # Collect all image files from all scan paths
        image_files = []
        seen_real = set()
        _file_count = 0
        for scan_path in scan_paths:
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            for dirpath, _dirs, filenames in os.walk(scan_path):
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                # Skip hidden dirs and trash
                if '/.trash' in dirpath or '/.' in dirpath.split(scan_path)[-1]:
                    continue
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in IMAGE_EXTS:
                        fp = os.path.join(dirpath, fn)
                        real = os.path.realpath(fp)
                        if real in seen_real:
                            continue
                        seen_real.add(real)
                        try:
                            st = os.stat(fp)
                            image_files.append({
                                'path': fp.replace(DATA_ROOT, '', 1),
                                'real_path': fp,
                                'size': st.st_size,
                                'modified': st.st_mtime,
                                'name': fn,
                            })
                        except OSError:
                            pass
                    _file_count += 1
                    if _file_count % 5 == 0:
                        gevent.sleep(0)
                gevent.sleep(0)  # yield per directory

        total = len(image_files)
        _dup_scan['total'] = total
        _dup_scan['phase'] = f'Found {total} images, analyzing…'
        socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

        if total == 0:
            _dup_scan['running'] = False
            _dup_scan['phase'] = 'Completed'
            socketio.emit('dup_complete', {'groups': 0, 'duplicates': 0, 'size': 0})
            return

        exact_path_sets = set()  # track exact groups to avoid duplication in similar phase

        if mode in ('exact', 'both'):
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            # ── Phase: exact duplicates by SHA256 ──
            _dup_scan['phase'] = 'Comparing checksums (SHA256)…'
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

            # Pre-filter: group by size
            by_size = {}
            for f in image_files:
                by_size.setdefault(f['size'], []).append(f)
            size_groups = {k: v for k, v in by_size.items() if len(v) > 1}

            # Hash only size-matched files
            hash_map = {}
            scanned = 0
            to_hash = [f for grp in size_groups.values() for f in grp]
            for f in to_hash:
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                try:
                    cache_key = f['real_path']
                    cached = hash_cache.get(cache_key)
                    if cached and cached.get('mtime') == f['modified'] and cached.get('size') == f['size'] and cached.get('sha256'):
                        digest = cached['sha256']
                        cache_hits += 1
                    else:
                        h = hashlib.sha256()
                        with open(f['real_path'], 'rb') as fh:
                            _chunks = 0
                            while True:
                                chunk = fh.read(65536)
                                if not chunk:
                                    break
                                h.update(chunk)
                                _chunks += 1
                                if _chunks % 16 == 0:  # yield every ~1MB
                                    gevent.sleep(0)
                        digest = h.hexdigest()
                        # Update cache
                        if cache_key not in hash_cache:
                            hash_cache[cache_key] = {}
                        hash_cache[cache_key]['sha256'] = digest
                        hash_cache[cache_key]['mtime'] = f['modified']
                        hash_cache[cache_key]['size'] = f['size']
                    hash_map.setdefault(digest, []).append(f)
                except Exception:
                    pass
                scanned += 1
                gevent.sleep(0)  # yield after every file hash
                if scanned % 20 == 0:
                    _dup_scan['scanned'] = scanned
                    socketio.emit('dup_progress', {
                        'phase': f'SHA256: {scanned}/{len(to_hash)}',
                        'scanned': scanned, 'total': len(to_hash)
                    })

            # Emit exact duplicate groups incrementally
            for digest, files in hash_map.items():
                if len(files) > 1:
                    items = sorted(files, key=lambda x: x['modified'])
                    # Strip real_path before sending
                    items_clean = [{k: v for k, v in it.items() if k != 'real_path'} for it in items]
                    group = {
                        'hash': digest[:16],
                        'type': 'exact',
                        'items': items_clean,
                    }
                    _dup_scan['results'].append(group)
                    _dup_scan['found_groups'] = len(_dup_scan['results'])
                    _emit_new_group(group)
                    exact_path_sets.add(tuple(sorted(f['path'] for f in items)))
                    gevent.sleep(0)

        if mode in ('similar', 'both'):
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            # ── Phase: visually similar by dhash ──
            _dup_scan['phase'] = 'Analiza wizualna (perceptual hash)…'
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

            phash_list = []  # [(file_info, hash_int)]
            scanned = 0
            for f in image_files:
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                try:
                    cache_key = f['real_path']
                    cached = hash_cache.get(cache_key)
                    if cached and cached.get('mtime') == f['modified'] and cached.get('size') == f['size'] and cached.get('dhash') is not None:
                        h = cached['dhash']
                        cache_hits += 1
                    else:
                        img = Image.open(f['real_path'])
                        h = _dhash(img)
                        # Update cache
                        if cache_key not in hash_cache:
                            hash_cache[cache_key] = {}
                        hash_cache[cache_key]['dhash'] = h
                        hash_cache[cache_key]['mtime'] = f['modified']
                        hash_cache[cache_key]['size'] = f['size']
                    phash_list.append((f, h))
                except Exception:
                    pass
                scanned += 1
                gevent.sleep(0)  # yield after every image
                if scanned % 20 == 0:
                    _dup_scan['scanned'] = scanned
                    socketio.emit('dup_progress', {
                        'phase': f'Perceptual hash: {scanned}/{total}',
                        'scanned': scanned, 'total': total
                    })

            # ── Phase: clustering ──
            # Use bucket approach to avoid full O(n²): bucket by coarse hash chunks
            _dup_scan['phase'] = 'Grouping similar images…'
            n = len(phash_list)
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': n})

            parent = list(range(n))

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

            # Bucket by portions of the hash to reduce comparisons
            HASH_BITS = 100  # 10x10 dhash = 100 bits
            BUCKET_BITS = 25  # split into 4 bands
            num_bands = HASH_BITS // BUCKET_BITS

            buckets = [{} for _ in range(num_bands)]
            for i, (f, h) in enumerate(phash_list):
                for band_idx in range(num_bands):
                    band_val = (h >> (band_idx * BUCKET_BITS)) & ((1 << BUCKET_BITS) - 1)
                    buckets[band_idx].setdefault(band_val, []).append(i)

            compared = set()
            comparisons_done = 0
            for band_idx in range(num_bands):
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                for candidates in buckets[band_idx].values():
                    if len(candidates) < 2:
                        continue
                    for ci in range(len(candidates)):
                        for cj in range(ci + 1, len(candidates)):
                            i, j = candidates[ci], candidates[cj]
                            pair = (min(i, j), max(i, j))
                            if pair in compared:
                                continue
                            compared.add(pair)
                            if _hamming(phash_list[i][1], phash_list[j][1]) <= threshold:
                                union(i, j)
                            comparisons_done += 1
                            if comparisons_done % 50 == 0:
                                gevent.sleep(0)  # yield frequently during comparisons
                            if comparisons_done % 500 == 0:
                                socketio.emit('dup_progress', {
                                    'phase': f'Comparing: {comparisons_done} pairs',
                                    'scanned': comparisons_done, 'total': comparisons_done
                                })

            # For high thresholds, also do a limited brute-force on remaining
            if threshold >= 10 and n <= 5000:
                _dup_scan['phase'] = 'Additional comparisons…'
                socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': n})
                _bf_ops = 0
                for i in range(n):
                    if _dup_scan.get('cancel'):
                        raise InterruptedError('Cancelled')
                    for j in range(i + 1, n):
                        pair = (i, j)
                        if pair in compared:
                            continue
                        if _hamming(phash_list[i][1], phash_list[j][1]) <= threshold:
                            union(i, j)
                        _bf_ops += 1
                        if _bf_ops % 50 == 0:
                            gevent.sleep(0)
                    if i % 50 == 0:
                        socketio.emit('dup_progress', {
                            'phase': f'Brute-force: {i}/{n}',
                            'scanned': i, 'total': n
                        })
                        gevent.sleep(0)

            # Collect clusters & emit incrementally
            clusters = {}
            for i in range(n):
                root = find(i)
                clusters.setdefault(root, []).append(i)

            for idxs in clusters.values():
                if len(idxs) < 2:
                    continue
                items = [phash_list[i][0] for i in idxs]
                paths = tuple(sorted(f['path'] for f in items))
                if paths in exact_path_sets:
                    continue
                items_sorted = sorted(items, key=lambda x: x['modified'])
                items_clean = [{k: v for k, v in it.items() if k != 'real_path'} for it in items_sorted]
                group = {
                    'hash': f'phash_{idxs[0]}',
                    'type': 'similar',
                    'items': items_clean,
                }
                _dup_scan['results'].append(group)
                _dup_scan['found_groups'] = len(_dup_scan['results'])
                _emit_new_group(group)
                gevent.sleep(0)

        # Sort results by total size (descending)
        _dup_scan['results'].sort(key=lambda g: sum(f['size'] for f in g['items']), reverse=True)

        _dup_scan['running'] = False
        _dup_scan['phase'] = 'Completed'
        groups = _dup_scan['results']
        total_dups = sum(len(g['items']) - 1 for g in groups)
        dup_size = sum(sum(f['size'] for f in g['items'][1:]) for g in groups)
        socketio.emit('dup_complete', {
            'groups': len(groups),
            'duplicates': total_dups,
            'size': dup_size,
        })
        elog('files', 'info',
             f'Duplicate scan completed: {len(groups)} groups, '
             f'{total_dups} duplicates ({dup_size} bytes), '
             f'{cache_hits} cache hits')
        # Save hash cache for future scans
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)

    except InterruptedError:
        _dup_scan['running'] = False
        _dup_scan['cancel'] = False
        _dup_scan['phase'] = 'Cancelled'
        socketio.emit('dup_cancelled', {
            'groups': _dup_scan['found_groups']
        })
        elog('files', 'info', f'Duplicate scan cancelled ({_dup_scan["found_groups"]} groups found)')
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)
    except Exception as e:
        _dup_scan['running'] = False
        _dup_scan['error'] = str(e)
        _dup_scan['phase'] = 'Error'
        socketio.emit('dup_error', {'error': str(e)})
        elog('files', 'error', f'Duplicate scan error: {str(e)}')
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)


def get_dupscan_notifications():
    """Return notification items for the duplicate scan."""
    notifs = []
    if _dup_scan['running']:
        phase = _dup_scan.get('phase', 'Scanning…')
        found = _dup_scan.get('found_groups', 0)
        scanned = _dup_scan.get('scanned', 0)
        total = _dup_scan.get('total', 0)
        pct = (round(scanned / total * 100) if total > 0 else 0)
        notifs.append({
            'type': 'progress',
            'title': 'Finding duplicates',
            'message': f'{phase} — {found} grup, {pct}%',
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    elif _dup_scan.get('phase') == 'Completed' and _dup_scan.get('results'):
        groups = _dup_scan['results']
        total_dups = sum(len(g['items']) - 1 for g in groups)
        notifs.append({
            'type': 'success',
            'title': 'Duplicates found',
            'message': f'{len(groups)} groups, {total_dups} redundant files',
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    elif _dup_scan.get('phase') == 'Cancelled' and _dup_scan.get('results'):
        found = len(_dup_scan['results'])
        if found > 0:
            notifs.append({
                'type': 'warning',
                'title': 'Scan cancelled',
                'message': f'{found} groups found before cancellation',
                'time': time.time(),
                'action': {'app': 'duplicates'}
            })
    elif _dup_scan.get('error'):
        notifs.append({
            'type': 'error',
            'title': 'Duplicate scan error',
            'message': str(_dup_scan['error']),
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    return notifs


@app.route('/api/files/duplicates/scan', methods=['POST'])
@require_auth
def files_duplicates_scan():
    """Start a duplicate photo scan."""
    if _dup_scan['running']:
        return jsonify({'error': 'Scan already in progress'}), 409

    data = request.json or {}
    # Support both single path and multiple paths
    paths_rel = data.get('paths', [])
    if not paths_rel and data.get('path'):
        paths_rel = [data['path']]
    if not paths_rel:
        paths_rel = ['/home']
    mode = data.get('mode', 'both')        # exact | similar | both
    threshold = data.get('threshold', 8)     # hamming distance for similar

    scan_paths = []
    for p in paths_rel:
        rp = safe_path(p)
        if rp and os.path.isdir(rp):
            scan_paths.append(rp)
    if not scan_paths:
        return jsonify({'error': 'No valid paths'}), 400

    _dup_scan['running'] = True
    _dup_scan['cancel'] = False
    _dup_scan['scan_id'] = secrets.token_hex(4)
    _dup_scan['phase'] = 'Starting…'
    _dup_scan['scanned'] = 0
    _dup_scan['total'] = 0
    _dup_scan['found_groups'] = 0
    _dup_scan['results'] = []
    _dup_scan['error'] = None

    socketio.start_background_task(_scan_duplicates, scan_paths, mode, threshold)
    return jsonify({'scan_id': _dup_scan['scan_id'], 'status': 'started'})


@app.route('/api/files/duplicates/cancel', methods=['POST'])
@require_auth
def files_duplicates_cancel():
    """Cancel a running scan."""
    if not _dup_scan['running']:
        return jsonify({'error': 'No active scan'}), 400
    _dup_scan['cancel'] = True
    return jsonify({'ok': True, 'message': 'Cancelling scan…'})


@app.route('/api/files/duplicates/status')
@require_auth
def files_duplicates_status():
    """Get current scan progress."""
    return jsonify({
        'running': _dup_scan['running'],
        'phase': _dup_scan['phase'],
        'scanned': _dup_scan['scanned'],
        'total': _dup_scan['total'],
        'found_groups': _dup_scan['found_groups'],
        'error': _dup_scan['error'],
    })


@app.route('/api/files/duplicates/results')
@require_auth
def files_duplicates_results():
    """Get scan results — groups of duplicate images (works during scan too).
    Filters out ignored groups unless ?include_ignored=1."""
    results = _dup_scan.get('results', [])
    include_ignored = request.args.get('include_ignored', '0') == '1'
    if not include_ignored:
        ignored = _load_dup_ignored()
        results = [g for g in results if _dup_group_key(g) not in ignored]
    return jsonify({
        'groups': results,
        'ready': not _dup_scan['running'],
        'total_groups': len(results),
    })


@app.route('/api/files/duplicates/ignore', methods=['POST'])
@require_auth
def files_duplicates_ignore():
    """Add groups to the ignore list."""
    data = request.json or {}
    groups = data.get('groups', [])  # list of group objects with items
    if not groups:
        return jsonify({'error': 'No groups provided'}), 400
    ignored = _load_dup_ignored()
    added = 0
    for g in groups:
        key = _dup_group_key(g)
        if key not in ignored:
            ignored[key] = {
                'key': key,
                'paths': sorted(f['path'] for f in g.get('items', [])),
                'type': g.get('type', 'unknown'),
                'ignored_at': time.time(),
            }
            added += 1
    _save_dup_ignored(ignored)
    return jsonify({'ok': True, 'added': added, 'total_ignored': len(ignored)})


@app.route('/api/files/duplicates/unignore', methods=['POST'])
@require_auth
def files_duplicates_unignore():
    """Remove groups from the ignore list."""
    data = request.json or {}
    keys = data.get('keys', [])  # list of group keys
    if not keys:
        return jsonify({'error': 'No keys provided'}), 400
    ignored = _load_dup_ignored()
    removed = 0
    for k in keys:
        if k in ignored:
            del ignored[k]
            removed += 1
    _save_dup_ignored(ignored)
    return jsonify({'ok': True, 'removed': removed, 'total_ignored': len(ignored)})


@app.route('/api/files/duplicates/ignored')
@require_auth
def files_duplicates_ignored():
    """List all ignored dup groups — returns stored info + matching scan results if available."""
    ignored = _load_dup_ignored()
    # Try to enrich with actual group data from last scan results
    results = _dup_scan.get('results', [])
    result_by_key = {_dup_group_key(g): g for g in results}
    items = []
    for key, info in ignored.items():
        entry = {**info}
        if key in result_by_key:
            entry['group'] = result_by_key[key]
        items.append(entry)
    items.sort(key=lambda x: x.get('ignored_at', 0), reverse=True)
    return jsonify({'items': items, 'total': len(items)})


# -- duplicates package routes (no blueprint, lives in app.py) --
@app.route('/api/files/duplicates/install', methods=['POST'])
@require_auth
def duplicates_pkg_install():
    return jsonify({'ok': True})

@app.route('/api/files/duplicates/uninstall', methods=['POST'])
@require_auth
def duplicates_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    if wipe:
        _dup_scan['results'] = []
        _dup_scan['progress'] = 0
        _hash_cache_path = _data_path('dup_hash_cache.json')
        if os.path.isfile(_hash_cache_path):
            os.remove(_hash_cache_path)
        _ignored_path = _data_path('dup_ignored.json')
        if os.path.isfile(_ignored_path):
            os.remove(_ignored_path)
    return jsonify({'ok': True})

@app.route('/api/files/duplicates/pkg-status')
@require_auth
def duplicates_pkg_status():
    return jsonify({'status': 'ready'})


# -- code-editor package routes (frontend-only, noop backend) --
@app.route('/api/code-editor/install', methods=['POST'])
@require_auth
@admin_required
def code_editor_pkg_install():
    return jsonify({'ok': True})

@app.route('/api/code-editor/uninstall', methods=['POST'])
@require_auth
@admin_required
def code_editor_pkg_uninstall():
    return jsonify({'ok': True})

@app.route('/api/code-editor/pkg-status')
@require_auth
def code_editor_pkg_status():
    return jsonify({'status': 'ready'})


@app.route('/api/files/rename', methods=['POST'])
@require_auth
def files_rename():
    data = request.json or {}
    old = safe_path(data.get('path', ''))
    new_name = data.get('new_name', '')
    if not old or not new_name or '/' in new_name:
        return jsonify({'error': 'Invalid parameters'}), 400

    blocked = _require_folder_access(data.get('path', ''))
    if blocked is not None:
        return blocked

    new_path = os.path.join(os.path.dirname(old), new_name)
    try:
        # Compute user-visible paths for folder password migration
        old_user_path = data.get('path', '').rstrip('/')
        parent = os.path.dirname(old_user_path)
        new_user_path = (parent + '/' + new_name) if parent != '/' else ('/' + new_name)

        os.rename(old, new_path)

        # Migrate folder passwords
        _migrate_folder_passwords(old_user_path, new_user_path)
        _dirsize_cache_invalidate(os.path.dirname(old))
        _listdir_cache_invalidate(data.get('path', ''))

        elog('files', 'info', f'Renamed: {os.path.basename(old)} → {new_name}')
        return jsonify({'ok': True})
    except Exception as e:
        elog('files', 'error', f'Rename error: {str(e)}')
        return jsonify({'error': str(e)}), 500


def _atomic_move(src, target):
    """Move *src* to *target* atomically.

    For same-filesystem moves, uses os.rename which is atomic.
    For cross-filesystem moves, copies to a temp file first, then atomically
    replaces the target, and finally removes the source.  This ensures that
    a crash between any step leaves data intact (source still exists or target
    is already written).
    """
    try:
        os.rename(src, target)
    except OSError as e:
        import errno as _errno
        if e.errno != _errno.EXDEV:
            raise
        # Cross-device move: copy → atomic replace → remove source
        if os.path.isdir(src):
            tmp_target = target + '.ethos_mv_tmp_dir'
            try:
                shutil.copytree(src, tmp_target)
                _chown_recursive(tmp_target)
                os.rename(tmp_target, target)
                shutil.rmtree(src)
            except Exception:
                shutil.rmtree(tmp_target, ignore_errors=True)
                raise
        else:
            tmp_target = target + '.ethos_mv_tmp'
            try:
                shutil.copy2(src, tmp_target)
                _chown_to_user(tmp_target)
                os.replace(tmp_target, target)
                os.remove(src)
            except Exception:
                try:
                    os.remove(tmp_target)
                except OSError:
                    pass
                raise


@app.route('/api/files/move', methods=['POST'])
@require_auth
def files_move():
    data = request.json or {}
    src = safe_path(data.get('src', ''))
    dest = safe_path(data.get('dest', ''))
    if not src or not dest:
        return jsonify({'error': 'Invalid parameters'}), 400

    blocked = _require_folder_access(data.get('src', ''))
    if blocked is not None:
        return blocked
    blocked = _require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked

    try:
        old_user_path = data.get('src', '').rstrip('/')
        dest_user = data.get('dest', '').rstrip('/')
        new_user_path = dest_user + '/' + os.path.basename(old_user_path)

        target = os.path.join(dest, os.path.basename(src))
        # Run in thread pool to avoid blocking gevent on cross-device moves
        _fs_call(_atomic_move, src, target, timeout=300)

        # Migrate folder passwords for moved folder
        _migrate_folder_passwords(old_user_path, new_user_path)
        _dirsize_cache_invalidate(os.path.dirname(src))
        _dirsize_cache_invalidate(dest)
        _listdir_cache_invalidate(data.get('src', ''))
        _listdir_cache_invalidate(data.get('dest', ''))

        elog('files', 'info', f'Moved: {os.path.basename(src)} → {data.get("dest", "")}')
        return jsonify({'ok': True})
    except Exception as e:
        elog('files', 'error', f'Move error: {str(e)}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/check-conflicts', methods=['POST'])
@require_auth
def files_check_conflicts():
    """Check which sources already exist in dest."""
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = safe_path(data.get('dest', ''))
    if not sources or not dest_dir:
        return jsonify({'conflicts': []})
    conflicts = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            continue
        base_name = os.path.basename(real_src)
        target = os.path.join(dest_dir, base_name)
        if os.path.exists(target):
            conflicts.append({
                'name': base_name,
                'source': src_path,
                'is_dir': os.path.isdir(real_src),
                'target_is_dir': os.path.isdir(target),
            })
    return jsonify({'conflicts': conflicts})


def _resolve_target(real_src, dest_dir, on_conflict):
    """Resolve target path based on conflict strategy. Returns target path or None to skip."""
    base_name = os.path.basename(real_src)
    target = os.path.join(dest_dir, base_name)
    if os.path.exists(target):
        if on_conflict == 'overwrite':
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            return target
        elif on_conflict == 'skip':
            return None
        else:  # rename (default)
            name, ext = os.path.splitext(base_name)
            counter = 1
            while os.path.exists(target):
                target = os.path.join(dest_dir, f"{name}_kopia{counter}{ext}")
                counter += 1
            return target
    return target


@app.route('/api/files/copy', methods=['POST'])
@require_auth
def files_copy():
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = safe_path(data.get('dest', ''))
    on_conflict = data.get('on_conflict', 'rename')  # overwrite | skip | rename
    if not sources or not dest_dir:
        return jsonify({'error': 'Invalid parameters'}), 400
    if not os.path.isdir(dest_dir):
        return jsonify({'error': 'Destination is not a folder'}), 400

    # Security check: check destination and all sources
    blocked = _require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked
    for s in sources:
        blocked = _require_folder_access(s)
        if blocked is not None:
            return blocked

    # Resolve sources first
    resolved = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            continue
        resolved.append(real_src)

    total = _count_items(resolved)
    use_bg = total > 2  # background task for >2 items

    if use_bg:
        _fm = _fileop_channels['fm']
        with _fileop_lock:
            if _fm['active']:
                return jsonify({'error': 'Another file operation is in progress'}), 400
            _fm['active'] = True
            _fm['operation'] = 'copy'
            _fm['progress'] = None
            _fm['cancel'] = False
            _fm['paused'] = False
        cur_user = get_current_user()
        _save_copy_task(resolved, dest_dir, total, on_conflict, (cur_user or {}).get('username'))
        socketio.start_background_task(_bg_copy, resolved, dest_dir, total, on_conflict, cur_user)
        return jsonify({'async': True, 'message': f'Copying {len(resolved)} items ({total} files) in background'})

    # Small operation — synchronous but run I/O in thread pool
    # to avoid blocking gevent on slow disks (USB, cross-device)
    copied = []
    skipped = []
    errors = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            errors.append(f'Not found: {src_path}')
            continue
        base_name = os.path.basename(real_src)
        target = _resolve_target(real_src, dest_dir, on_conflict)
        if target is None:
            skipped.append(base_name)
            continue
        try:
            if os.path.isdir(real_src):
                tmp_target = target + '.ethos_tmp_dir'
                def _do_copy_dir(_src=real_src, _tmp=tmp_target, _tgt=target):
                    try:
                        shutil.copytree(_src, _tmp)
                        os.rename(_tmp, _tgt)
                    except Exception:
                        shutil.rmtree(_tmp, ignore_errors=True)
                        raise
                _fs_call(_do_copy_dir, timeout=300)
                _chown_recursive(target)
            else:
                tmp_target = target + '.ethos_tmp'
                def _do_copy_file(_src=real_src, _tmp=tmp_target, _tgt=target):
                    shutil.copy2(_src, _tmp)
                    os.replace(_tmp, _tgt)
                _fs_call(_do_copy_file, timeout=300)
                _chown_to_user(target)
            copied.append(base_name)
        except Exception as e:
            errors.append(f'{base_name}: {str(e)}')

    _listdir_cache_invalidate(data.get('dest', ''))
    _dirsize_cache_invalidate(dest_dir)

    if copied:
        cur = get_current_user()
        username = cur['username'] if cur else 'unknown'
        elog('files', 'info', f'Copied {len(copied)} files to {data.get("dest", "")}', {'user': username, 'files': copied})

    return jsonify({'copied': copied, 'skipped': skipped, 'errors': errors})


def _bg_copy(resolved_sources, dest_dir, total, on_conflict='rename', cur_user=None):
    """Background copy with progress, cancel/pause support, and cleanup on abort."""
    _bg_username = cur_user['username'] if cur_user else None
    copied = []
    skipped = []
    errors = []
    done_ref = [0]
    partial_files = []  # tracks .ethos_tmp files written so far (for cleanup on cancel)
    cancel_ref = [False]
    _fm = _fileop_channels['fm']

    def _check_cancel():
        with _fileop_lock:
            cancel_ref[0] = _fm.get('cancel', False)
        return cancel_ref[0]

    def _is_paused():
        with _fileop_lock:
            return _fm.get('paused', False)

    _fileop_progress('copy', '', 0, total, 'fm')
    cancelled = False
    try:
        for real_src in resolved_sources:
            if _check_cancel():
                cancelled = True
                break
            if not real_src or not os.path.exists(real_src):
                errors.append(f'Not found: {real_src}')
                continue
            base_name = os.path.basename(real_src)
            target = _resolve_target(real_src, dest_dir, on_conflict)
            if target is None:
                skipped.append(base_name)
                done_ref[0] += _count_items([real_src])
                _fileop_progress('copy', f'{base_name} (skipped)', done_ref[0], total, 'fm')
                gevent.sleep(0)
                continue
            try:
                _copy_with_progress(real_src, target, 'copy', done_ref, total, username=_bg_username,
                                    cancel_ref=cancel_ref, pause_fn=_is_paused, partial_files=partial_files)
                copied.append(base_name)
            except InterruptedError:
                cancelled = True
                break
            except Exception as e:
                errors.append(f'{base_name}: {str(e)}')

        if cancelled:
            # Clean up partial .ethos_tmp files left behind
            for pf in partial_files:
                try: os.remove(pf)
                except OSError: pass
            _clear_copy_task()
            _fileop_finish('copy', False, 'Cancelled', 'fm')
            return

        _clear_copy_task()
        msg = f'Copied {len(copied)} items'
        if skipped:
            msg += f', skipped {len(skipped)}'
        if errors:
            msg += f' ({len(errors)} errors)'
        _fileop_finish('copy', len(copied) > 0 or len(skipped) > 0, msg, 'fm')
    except Exception as e:
        # Clean up partial files on unexpected error
        for pf in partial_files:
            try: os.remove(pf)
            except OSError: pass
        _clear_copy_task()
        _fileop_finish('copy', False, str(e), 'fm')


@app.route('/api/files/move-multi', methods=['POST'])
@require_auth
def files_move_multi():
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = safe_path(data.get('dest', ''))
    if not sources or not dest_dir:
        return jsonify({'error': 'Invalid parameters'}), 400
    if not os.path.isdir(dest_dir):
        return jsonify({'error': 'Destination is not a folder'}), 400

    # Security check: check destination and all sources
    blocked = _require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked
    for s in sources:
        blocked = _require_folder_access(s)
        if blocked is not None:
            return blocked

    # Resolve sources
    resolved = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if real_src and os.path.exists(real_src):
            resolved.append(real_src)

    total = _count_items(resolved)
    use_bg = total > 2  # background task for >2 items

    on_conflict = data.get('on_conflict', 'rename')  # overwrite | skip | rename

    if use_bg:
        _fm = _fileop_channels['fm']
        with _fileop_lock:
            if _fm['active']:
                return jsonify({'error': 'Another file operation is in progress'}), 400
            _fm['active'] = True
            _fm['operation'] = 'move'
            _fm['progress'] = None
            _fm['cancel'] = False
            _fm['paused'] = False
        cur_user = get_current_user()
        _save_move_task(resolved, dest_dir, total, on_conflict, data.get('dest', ''), (cur_user or {}).get('username'))
        socketio.start_background_task(_bg_move, resolved, dest_dir, total, on_conflict, data.get('dest', ''), cur_user)
        return jsonify({'async': True, 'message': f'Moving {len(resolved)} items in background'})

    # Small — synchronous
    moved = []
    skipped = []
    errors = []
    for src_path in sources:
        real_src = safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            errors.append(f'Not found: {src_path}')
            continue
        base_name = os.path.basename(real_src)
        target = _resolve_target(real_src, dest_dir, on_conflict)
        if target is None:
            skipped.append(base_name)
            continue
        try:
            # Run in thread pool to avoid blocking gevent on cross-device moves
            _fs_call(_atomic_move, real_src, target, timeout=300)
            _chown_recursive(target)
            moved.append(base_name)
            # Migrate folder passwords
            dest_user = data.get('dest', '').rstrip('/')
            _migrate_folder_passwords(src_path.rstrip('/'), dest_user + '/' + base_name)
        except Exception as e:
            errors.append(f'{base_name}: {str(e)}')

    _listdir_cache_invalidate(data.get('dest', ''))
    _dirsize_cache_invalidate(dest_dir)
    for src_path in sources:
        _listdir_cache_invalidate(src_path)
        _dirsize_cache_invalidate(os.path.dirname(safe_path(src_path) or ''))

    if moved:
        cur = get_current_user()
        username = cur['username'] if cur else 'unknown'
        elog('files', 'info', f'Moved {len(moved)} files to {data.get("dest", "")}', {'user': username, 'files': moved})

    return jsonify({'moved': moved, 'skipped': skipped, 'errors': errors})


def _bg_move(resolved_sources, dest_dir, total, on_conflict='rename', dest_user_path='', cur_user=None):
    """Background move with progress, cancel/pause support."""
    _bg_username = cur_user['username'] if cur_user else None
    moved = []
    skipped = []
    errors = []
    done = 0
    dest_user = dest_user_path.rstrip('/')
    cancel_ref = [False]
    _fm = _fileop_channels['fm']

    def _check_cancel():
        with _fileop_lock:
            cancel_ref[0] = _fm.get('cancel', False)
        return cancel_ref[0]

    def _is_paused():
        with _fileop_lock:
            return _fm.get('paused', False)

    def _wait_if_paused():
        while _is_paused():
            if _check_cancel():
                return True
            gevent.sleep(0.2)
        return _check_cancel()

    _fileop_progress('move', '', 0, total, 'fm')
    cancelled = False
    try:
        for real_src in resolved_sources:
            if _check_cancel():
                cancelled = True
                break
            # Honour pause between items
            if _wait_if_paused():
                cancelled = True
                break
            if not real_src or not os.path.exists(real_src):
                errors.append(f'Not found: {real_src}')
                continue
            base_name = os.path.basename(real_src)
            item_count = _count_items([real_src])
            target = _resolve_target(real_src, dest_dir, on_conflict)
            if target is None:
                skipped.append(base_name)
                done += item_count
                _fileop_progress('move', f'{base_name} (skipped)', done, total, 'fm')
                gevent.sleep(0)
                continue
            try:
                # Run in thread pool to avoid blocking gevent on cross-device moves
                _fs_call(_atomic_move, real_src, target, timeout=600)
                _chown_recursive(target, _bg_username)
                done += item_count
                moved.append(base_name)
                # Migrate folder passwords
                if dest_user:
                    _migrate_folder_passwords(real_src.rstrip('/'), dest_user + '/' + base_name)
                _fileop_progress('move', base_name, done, total, 'fm')
                gevent.sleep(0)
            except Exception as e:
                errors.append(f'{base_name}: {str(e)}')

        if cancelled:
            _clear_move_task()
            _fileop_finish('move', False, 'Cancelled', 'fm')
            return

        _clear_move_task()
        msg = f'Moved {len(moved)} items'
        if skipped:
            msg += f', skipped {len(skipped)}'
        if errors:
            msg += f' ({len(errors)} errors)'
        _fileop_finish('move', len(moved) > 0 or len(skipped) > 0, msg, 'fm')
    except Exception as e:
        _clear_move_task()
        _fileop_finish('move', False, str(e), 'fm')


# ── Archive / Extract ──

@app.route('/api/files/compress', methods=['POST'])
@require_auth
def files_compress():
    """Create a zip or tar.gz from selected files/folders."""
    data = request.json or {}
    sources = data.get('sources', [])
    fmt = data.get('format', 'zip')  # 'zip' or 'tar.gz'
    archive_name = data.get('name', '')
    if not sources:
        return jsonify({'error': 'No files to compress'}), 400

    # Determine output dir = same dir as first source
    first_src = safe_path(sources[0])
    if not first_src:
        return jsonify({'error': 'Invalid path'}), 400
    out_dir = os.path.dirname(first_src)

    if not archive_name:
        if len(sources) == 1:
            archive_name = os.path.splitext(os.path.basename(first_src))[0]
        else:
            archive_name = 'archive'

    ext = '.zip' if fmt == 'zip' else '.tar.gz'
    archive_path = os.path.join(out_dir, archive_name + ext)
    # Avoid overwrite
    counter = 1
    while os.path.exists(archive_path):
        archive_path = os.path.join(out_dir, f"{archive_name}_{counter}{ext}")
        counter += 1

    resolved = []
    for s in sources:
        rs = safe_path(s)
        if rs and os.path.exists(rs):
            resolved.append(rs)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    total = _count_items(resolved)

    with _fileop_lock:
        if _fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'compress'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False

    cur_user = get_current_user()
    _bg_username = cur_user['username'] if cur_user else None
    _save_compress_task(resolved, archive_path, fmt, total, _bg_username)
    socketio.start_background_task(_bg_compress, resolved, archive_path, fmt, total, cur_user)
    return jsonify({'async': True, 'message': f'Compressing {len(resolved)} items to {os.path.basename(archive_path)}'})


def _bg_compress(resolved, archive_path, fmt, total, cur_user=None):
    """Background archive creation with progress. Writes to a temp file first,
    then atomically renames to the final path on success (prevents corrupt archives)."""
    _bg_username = cur_user['username'] if cur_user else None
    done = 0
    cancelled = False
    tmp_archive_path = archive_path + '.ethos_archive_tmp'

    def _check_cancel_pause():
        if _fileop_cancelled():
            return True
        while _fileop_is_paused():
            gevent.sleep(0.5)
            if _fileop_cancelled():
                return True
        return False

    try:
        if fmt == 'zip':
            with zipfile.ZipFile(tmp_archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for src in resolved:
                    if _check_cancel_pause():
                        cancelled = True; break
                    base_dir = os.path.dirname(src)
                    if os.path.isdir(src):
                        for root, dirs, files in os.walk(src):
                            if _check_cancel_pause():
                                cancelled = True; break
                            for fn in files:
                                if _check_cancel_pause():
                                    cancelled = True; break
                                full = os.path.join(root, fn)
                                arcname = os.path.relpath(full, base_dir)
                                zf.write(full, arcname)
                                done += 1
                                _fileop_progress('compress', fn, done, total)
                                gevent.sleep(0)
                            if cancelled:
                                break
                            done += len(dirs)
                            gevent.sleep(0)
                    else:
                        zf.write(src, os.path.basename(src))
                        done += 1
                        _fileop_progress('compress', os.path.basename(src), done, total)
                        gevent.sleep(0)
        else:
            with tarfile.open(tmp_archive_path, 'w:gz') as tf:
                for src in resolved:
                    if _check_cancel_pause():
                        cancelled = True; break
                    base_dir = os.path.dirname(src)
                    if os.path.isdir(src):
                        for root, dirs, files in os.walk(src):
                            if _check_cancel_pause():
                                cancelled = True; break
                            for fn in files:
                                if _check_cancel_pause():
                                    cancelled = True; break
                                full = os.path.join(root, fn)
                                arcname = os.path.relpath(full, base_dir)
                                tf.add(full, arcname)
                                done += 1
                                _fileop_progress('compress', fn, done, total)
                                gevent.sleep(0)
                            if cancelled:
                                break
                            done += len(dirs)
                            gevent.sleep(0)
                    else:
                        tf.add(src, os.path.basename(src))
                        done += 1
                        _fileop_progress('compress', os.path.basename(src), done, total)
                        gevent.sleep(0)

        if cancelled:
            for p in (tmp_archive_path,):
                if os.path.exists(p):
                    try: os.remove(p)
                    except OSError: pass
            _clear_compress_task()
            _fileop_finish('compress', False, 'Cancelled')
            return

        # Atomically move temp archive to final path
        os.replace(tmp_archive_path, archive_path)
        _chown_to_user(archive_path, _bg_username)
        size_mb = round(os.path.getsize(archive_path) / (1024*1024), 1)
        _clear_compress_task()
        _fileop_finish('compress', True, f'{os.path.basename(archive_path)} ({size_mb} MB)')
    except Exception as e:
        for p in (tmp_archive_path, archive_path):
            if os.path.exists(p):
                try: os.remove(p)
                except OSError: pass
        _clear_compress_task()
        _fileop_finish('compress', False, str(e))


_EXTRACT_COMPOUND_EXTS = ('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')
_EXTRACT_SIMPLE_EXTS = ('.tar', '.zip')
_EXTRACT_7Z_EXTS = ('.gz', '.bz2', '.xz', '.rar', '.7z', '.cab', '.iso')
_EXTRACT_ALL_EXTS = _EXTRACT_COMPOUND_EXTS + _EXTRACT_SIMPLE_EXTS + _EXTRACT_7Z_EXTS


def _extract_folder_name(basename):
    """Derive output folder name from archive filename."""
    low = basename.lower()
    for ext in _EXTRACT_COMPOUND_EXTS:
        if low.endswith(ext):
            return basename[:len(basename) - len(ext)]
    for ext in _EXTRACT_SIMPLE_EXTS + _EXTRACT_7Z_EXTS:
        if low.endswith(ext):
            return basename[:len(basename) - len(ext)]
    return basename + '_extracted'


def _needs_7z(basename):
    """Return True if this archive format requires 7z CLI rather than Python stdlib."""
    low = basename.lower()
    return any(low.endswith(ext) for ext in _EXTRACT_7Z_EXTS)


@app.route('/api/files/extract', methods=['POST'])
@require_auth
def files_extract():
    """Extract an archive (zip, tar.*, gz, rar, 7z, bz2, xz, cab, iso)."""
    data = request.json or {}
    archive = safe_path(data.get('path', ''))
    if not archive or not os.path.isfile(archive):
        return jsonify({'error': 'Archive file does not exist'}), 400

    basename = os.path.basename(archive)
    low = basename.lower()

    if not any(low.endswith(ext) for ext in _EXTRACT_ALL_EXTS):
        return jsonify({'error': 'Unsupported archive format'}), 400

    folder_name = _extract_folder_name(basename)
    extract_to = os.path.join(os.path.dirname(archive), folder_name)
    counter = 1
    base_extract = extract_to
    while os.path.exists(extract_to):
        extract_to = f"{base_extract}_{counter}"
        counter += 1

    # For 7z-only formats, verify or install 7z first (before going async)
    use_7z = _needs_7z(basename)
    if use_7z:
        from host import ensure_dep
        ok, msg = ensure_dep('7z', install=True)
        if not ok:
            return jsonify({'error': f'Cannot extract: {msg}'}), 400

    os.makedirs(extract_to, exist_ok=True)

    # Count members (only for Python-handled formats)
    total = 0
    if not use_7z:
        try:
            if low.endswith('.zip'):
                with zipfile.ZipFile(archive, 'r') as zf:
                    total = len(zf.namelist())
            elif low.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar')):
                with tarfile.open(archive, 'r:*') as tf:
                    total = len(tf.getnames())
        except Exception:
            total = 0

    with _fileop_lock:
        _fm = _fileop_channels['fm']
        if _fm['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fm['active'] = True
        _fm['operation'] = 'extract'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    socketio.start_background_task(_bg_extract, archive, extract_to, total, use_7z, get_current_user())
    return jsonify({'async': True, 'message': f'Extracting {basename} to {folder_name}/'})


def _bg_extract(archive, extract_to, total, use_7z, cur_user=None):
    """Background archive extraction with progress and cancel support."""
    _bg_username = cur_user['username'] if cur_user else None
    done = 0
    basename = os.path.basename(archive)
    _fm = _fileop_channels['fm']

    def _check_cancel():
        with _fileop_lock:
            return _fm.get('cancel', False)

    try:
        if use_7z:
            # Use 7z CLI for formats Python can't handle (.gz, .rar, .7z, .bz2, .xz, .cab, .iso)
            import subprocess as _sp
            cmd = ['7z', 'x', '-y', f'-o{extract_to}', archive]
            _fileop_progress('extract', basename, 0, 1)
            proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, bufsize=1)
            for line in iter(proc.stdout.readline, ''):
                if _check_cancel():
                    proc.kill()
                    try: shutil.rmtree(extract_to, ignore_errors=True)
                    except Exception: pass
                    _fileop_finish('extract', False, 'Cancelled')
                    return
                line = line.strip()
                if line.startswith('- ') or line.startswith('Extracting '):
                    fname = line.split(' ', 1)[-1] if ' ' in line else line
                    done += 1
                    _fileop_progress('extract', fname[:80], done, max(done, 1))
                    gevent.sleep(0)
            proc.wait()
            if proc.returncode != 0:
                _fileop_finish('extract', False, f'7z exited with code {proc.returncode}')
                return
        elif basename.lower().endswith('.zip'):
            with zipfile.ZipFile(archive, 'r') as zf:
                for member in zf.namelist():
                    if _check_cancel():
                        try: shutil.rmtree(extract_to, ignore_errors=True)
                        except Exception: pass
                        _fileop_finish('extract', False, 'Cancelled')
                        return
                    zf.extract(member, extract_to)
                    done += 1
                    if done % 20 == 0 or done == total:
                        _fileop_progress('extract', member.split('/')[-1] or member, done, total)
                        gevent.sleep(0)
        else:
            # tar variants
            with tarfile.open(archive, 'r:*') as tf:
                for member in tf:
                    if _check_cancel():
                        try: shutil.rmtree(extract_to, ignore_errors=True)
                        except Exception: pass
                        _fileop_finish('extract', False, 'Cancelled')
                        return
                    tf.extract(member, extract_to, filter='data')
                    done += 1
                    if done % 20 == 0 or done == total:
                        name = member.name.split('/')[-1] or member.name
                        _fileop_progress('extract', name, done, total)
                        gevent.sleep(0)

        _chown_recursive(extract_to, _bg_username)
        _fileop_finish('extract', True, f'Extracted to {os.path.basename(extract_to)}/ ({done} files)')
    except Exception as e:
        _fileop_finish('extract', False, str(e))


# ── Transfer to remote NAS ──

@app.route('/api/files/remote-servers', methods=['GET'])
@require_auth
def files_remote_servers():
    """Return available SSH/NAS servers (from backup config) for file transfer."""
    try:
        from blueprints.backup import load_ssh_configs
        configs = load_ssh_configs()
        safe = []
        for c in configs:
            safe.append({
                'id': c.get('id'), 'name': c.get('name', ''),
                'host': c.get('host', ''), 'port': c.get('port', 22),
                'username': c.get('username', ''),
                'remote_path': c.get('remote_path', '~/'),
                'has_password': bool(c.get('password')),
                'has_key': bool(c.get('key_path'))
            })
        return jsonify({'servers': safe})
    except Exception as e:
        return jsonify({'servers': [], 'error': str(e)})


@app.route('/api/files/transfer-remote', methods=['POST'])
@require_auth
def files_transfer_remote():
    """Transfer files/folders to a remote NAS via SCP with progress."""
    data = request.json or {}
    server_id = data.get('server_id')
    paths = data.get('paths', [])
    remote_dest = data.get('remote_path', '')

    if not server_id:
        return jsonify({'error': 'No server selected'}), 400
    if not paths:
        return jsonify({'error': 'No files to transfer'}), 400

    # Get SSH config
    from blueprints.backup import load_ssh_configs
    configs = load_ssh_configs()
    server = next((c for c in configs if c.get('id') == server_id), None)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    # Quick connectivity check before starting background transfer
    import socket as _socket
    host = server.get('host', '')
    port = server.get('port', 22)
    try:
        s = _socket.create_connection((host, port), timeout=5)
        s.close()
    except Exception:
        return jsonify({'error': f'Server {server.get("name",host)} ({host}:{port}) is unreachable. Check that it is powered on and connected to the network.'}), 502

    # Resolve local paths
    resolved = []
    for p in paths:
        rp = safe_path(p)
        if rp and os.path.exists(rp):
            resolved.append(rp)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    # Compute total bytes
    total_bytes = 0
    total_files = 0
    for rp in resolved:
        if os.path.isdir(rp):
            for root, dirs, files in os.walk(rp):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except OSError:
                        pass
        else:
            try:
                total_bytes += os.path.getsize(rp)
                total_files += 1
            except OSError:
                pass

    with _fileop_lock:
        if _fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'transfer'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['paused'] = False
        _fileop_state['meta'] = {
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paths': [os.path.basename(p) for p in resolved[:10]],
            'total_bytes': total_bytes,
            'total_files': total_files,
            'started': time.time(),
        }

    dest = remote_dest or server.get('remote_path', '~/')
    # Capture requesting user for per-user known_hosts
    requesting_user = getattr(g, 'username', None) or 'root'

    # Persist transfer task so it can resume after server restart
    _save_transfer_task(resolved, server_id, dest, requesting_user)

    socketio.start_background_task(_bg_transfer_remote, resolved, server, dest, total_bytes, total_files, requesting_user)

    size_str = _fmt_bytes(total_bytes)
    return jsonify({
        'async': True,
        'cancellable': True,
        'message': f'Transferring {len(resolved)} items ({size_str}) to {server["name"]}'
    })


def _fmt_bytes(b):
    """Format bytes to human-readable — delegates to utils.fmt_bytes."""
    return fmt_bytes(b)


def _bg_transfer_remote(resolved, server, remote_dest, total_bytes, total_files, requesting_user='root'):
    """Background rsync transfer with real-time progress, resume and cancel support."""
    import subprocess, re as _re, shlex as _shlex, pty, select as _select

    host = server['host']
    port = server.get('port', 22)
    user = server['username']
    password = server.get('password', '')
    key_path = server.get('key_path', '')

    # Resolve the requesting user's home directory
    _user_home = _get_user_home(requesting_user)
    user_kh = os.path.join(_user_home, '.ssh', 'known_hosts')

    # Ensure .ssh dir exists for the user
    ssh_dir = os.path.join(_user_home, '.ssh')
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    try:
        import pwd
        pw = pwd.getpwnam(requesting_user)
        os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass

    # Build SSH command for rsync — use user's known_hosts file
    # Quote paths that may contain spaces
    ssh_cmd_parts = ['ssh', '-p', str(port), '-o', 'StrictHostKeyChecking=accept-new',
                     '-o', 'UserKnownHostsFile=' + _shlex.quote(user_kh),
                     '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=10',
                     '-o', 'ServerAliveCountMax=3']
    if key_path and os.path.exists(key_path):
        ssh_cmd_parts += ['-i', _shlex.quote(key_path)]
    ssh_cmd_str = ' '.join(ssh_cmd_parts)

    # Resolve remote dest (expand ~)
    dest = remote_dest
    if '~' in dest:
        try:
            from ssh_utils import get_ssh_client as _gsc, ssh_resolve_home as _srh
            ssh = _gsc(host, port, user, password=password, key_path=key_path, timeout=15)
            home = _srh(ssh)
            if home:
                dest = dest.replace('~', home)
            ssh.exec_command(f'mkdir -p {_shlex.quote(dest)}')
            gevent.sleep(0.3)
            ssh.close()
        except Exception:
            pass

    # Pre-compute per-item sizes for accurate progress
    item_sizes = []
    for rp in resolved:
        if os.path.isfile(rp):
            try:
                item_sizes.append(os.path.getsize(rp))
            except OSError:
                item_sizes.append(0)
        else:
            sz = 0
            for rt, _, fns in os.walk(rp):
                for fn in fns:
                    try:
                        sz += os.path.getsize(os.path.join(rt, fn))
                    except OSError:
                        pass
            item_sizes.append(sz)

    bytes_sent_total = [0]
    files_done = [0]
    current_file = ['']
    last_emit = [0]

    def emit_progress(forced=False):
        now = time.time()
        if not forced and now - last_emit[0] < 0.3:
            return
        last_emit[0] = now
        pct = round(bytes_sent_total[0] / total_bytes * 100, 1) if total_bytes > 0 else 0

        # Store byte-based progress in state (so /operation-status returns accurate %)
        detail = {
            'bytes_sent': bytes_sent_total[0], 'total_bytes': total_bytes,
            'files_sent': files_done[0], 'total_files': total_files,
            'current_file': current_file[0], 'percent': pct,
            'sent_fmt': _fmt_bytes(bytes_sent_total[0]), 'total_fmt': _fmt_bytes(total_bytes),
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paused': _fileop_is_paused(),
        }
        prog = {'operation': 'transfer', 'current_file': current_file[0],
                'done': files_done[0], 'total': total_files, 'percent': pct}
        with _fileop_lock:
            _fileop_state['progress'] = prog
        _fileop_emit('fileop_progress', prog)
        socketio.emit('fileop_transfer_detail', detail)

    def _run_rsync(rsync_cmd, env, idx, local_path, name):
        """Run rsync with PTY for real-time progress. Returns (returncode, stderr_text)."""
        master_fd, slave_fd = pty.openpty()
        err_r, err_w = os.pipe()
        proc = subprocess.Popen(
            rsync_cmd, stdout=slave_fd, stderr=err_w,
            env=env, close_fds=True
        )
        os.close(slave_fd)
        os.close(err_w)
        with _fileop_lock:
            _fileop_state['_proc'] = proc

        patt = _re.compile(r'([\d,]+)\s+(\d+)%\s+([\d.]+\w+/s)')
        buf = ''
        completed_bytes = sum(item_sizes[:idx])

        try:
            while True:
                if _fileop_cancelled():
                    # Resume first if paused (stopped process ignores SIGTERM)
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                    os.close(master_fd)
                    os.close(err_r)
                    with _fileop_lock:
                        _fileop_state['_proc'] = None
                    return (-999, 'cancelled')

                rlist = _select.select([master_fd], [], [], 0.5)[0]
                if rlist:
                    try:
                        data = os.read(master_fd, 4096).decode('utf-8', errors='replace')
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data
                    parts = _re.split(r'[\r\n]+', buf)
                    buf = parts[-1]
                    for p in parts[:-1]:
                        m = patt.search(p)
                        if m:
                            file_pct = int(m.group(2))
                            bytes_sent_total[0] = completed_bytes + int(item_sizes[idx] * file_pct / 100)
                            current_file[0] = name
                            emit_progress()
                    gevent.sleep(0)
                elif proc.poll() is not None:
                    break
        except (OSError, IOError) as exc:
            print(f'  [transfer] PTY read error for {name}: {exc}')
        except Exception as exc:
            print(f'  [transfer] Unexpected error in rsync read loop for {name}: {exc}')

        os.close(master_fd)
        # Use timeout to prevent permanent hang on stuck rsync
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print(f'  [transfer] rsync process for {name} did not exit in 30s, killing…')
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        stderr_text = ''
        try:
            stderr_text = os.read(err_r, 65536).decode('utf-8', errors='replace')
        except Exception:
            pass
        os.close(err_r)
        with _fileop_lock:
            _fileop_state['_proc'] = None
        return (proc.returncode, stderr_text)

    try:
        for idx, local_path in enumerate(resolved):
            if _fileop_cancelled():
                _clear_transfer_task()
                _fileop_finish('transfer', False, 'Transfer cancelled by user')
                return

            # Wait while paused (between items)
            while _fileop_is_paused():
                if _fileop_cancelled():
                    _clear_transfer_task()
                    _fileop_finish('transfer', False, 'Transfer cancelled by user')
                    return
                gevent.sleep(0.5)

            name = os.path.basename(local_path)
            current_file[0] = name
            remote_target = f'{user}@{host}:{_shlex.quote(dest)}/'

            rsync_cmd = [
                'rsync', '-rlt', '--partial', '--inplace',
                '--info=progress2', '--no-inc-recursive',
                '-e', ssh_cmd_str
            ]
            src = local_path.rstrip('/') + '/' if os.path.isdir(local_path) else local_path
            if os.path.isdir(local_path):
                remote_target = f'{user}@{host}:{_shlex.quote(dest + "/" + name)}/'
            rsync_cmd += [src, remote_target]

            env = os.environ.copy()
            if password and not (key_path and os.path.exists(key_path)):
                rsync_cmd = ['sshpass', '-e'] + rsync_cmd
                env['SSHPASS'] = password

            rc, stderr_out = _run_rsync(rsync_cmd, env, idx, local_path, name)

            if rc == -999:  # cancelled
                _clear_transfer_task()
                _fileop_finish('transfer', False, 'Transfer cancelled by user')
                return

            if rc not in (0, 24):
                # On network/IO error, retry up to 3 times (rsync resumes from partial)
                retryable_codes = (1, 5, 10, 11, 12, 23, 30, 35, 255)
                if rc in retryable_codes:
                    max_retries = 3
                    retry_ok = False
                    for attempt in range(1, max_retries + 1):
                        delay = min(5 * attempt, 15)  # 5s, 10s, 15s
                        _fileop_progress('transfer', f'Retrying ({attempt}/{max_retries}): {name}…', files_done[0], total_files)
                        print(f'  [transfer] rsync {name} failed (rc={rc}), retry {attempt}/{max_retries} in {delay}s')
                        gevent.sleep(delay)
                        if _fileop_cancelled():
                            _clear_transfer_task()
                            _fileop_finish('transfer', False, 'Transfer cancelled by user')
                            return
                        rc2, stderr2 = _run_rsync(rsync_cmd, env, idx, local_path, name)
                        if rc2 == -999:
                            _clear_transfer_task()
                            _fileop_finish('transfer', False, 'Transfer cancelled by user')
                            return
                        if rc2 in (0, 24):
                            retry_ok = True
                            break
                        rc = rc2
                        stderr_out = stderr2
                    if not retry_ok:
                        raise RuntimeError(f'rsync {name} (rc={rc}): {stderr_out.strip()}')
                else:
                    raise RuntimeError(f'rsync {name} (rc={rc}): {stderr_out.strip()}')

            files_done[0] = idx + 1
            bytes_sent_total[0] = sum(item_sizes[:idx + 1])
            emit_progress(forced=True)

        with _fileop_lock:
            _fileop_state['_proc'] = None
        _clear_transfer_task()
        size_str = _fmt_bytes(total_bytes)
        _fileop_finish('transfer', True,
                       f'Transferred {files_done[0]} files ({size_str}) to {server["name"]}')
    except Exception as e:
        import traceback
        print(f'  [transfer] EXCEPTION: {e}')
        traceback.print_exc()
        with _fileop_lock:
            _fileop_state['_proc'] = None
        # On crash / network error keep resume file so startup can retry.
        # Only clear on explicit cancel or permanent failure (we keep the file
        # for retryable errors so the next startup picks it up).
        err_msg = str(e)
        retryable_keywords = ('No route to host', 'Connection refused', 'timed out',
                              'Connection reset', 'Broken pipe', 'Network is unreachable',
                              'Connection timed out', 'Name or service not known')
        is_retryable = any(kw in err_msg for kw in retryable_keywords)
        if not is_retryable:
            _clear_transfer_task()
        # User-friendly error messages for common network errors
        for kw in ('No route to host', 'Connection refused', 'timed out', 'Connection reset',
                   'Network is unreachable', 'Name or service not known'):
            if kw.lower() in err_msg.lower():
                err_msg = f'Cannot connect to {server["name"]} ({server["host"]}). Check that the server is powered on and connected to the network.'
                break
        _fileop_finish('transfer', False, err_msg)


def _resume_interrupted_transfer():
    """Check for a persisted transfer task and resume it after server restart."""
    task = _load_json(TRANSFER_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('paths', [])
    server_id = task.get('server_id')
    remote_dest = task.get('remote_dest', '~/')
    requesting_user = task.get('requesting_user', 'root')
    started = task.get('started', 0)

    # Ignore tasks older than 7 days (stale)
    if time.time() - started > 7 * 86400:
        _clear_transfer_task()
        return

    if not resolved or not server_id:
        _clear_transfer_task()
        return

    # Verify at least one source still exists and validate paths
    valid = []
    for p in resolved:
        # Only allow absolute paths and ensure they actually exist
        rp = os.path.realpath(p)
        if rp and os.path.isabs(rp):
            try:
                exists = _fs_call(os.path.exists, rp, timeout=3)
            except TimeoutError:
                exists = False
            if exists:
                valid.append(rp)
    if not valid:
        _clear_transfer_task()
        return

    # Load server config
    try:
        from blueprints.backup import load_ssh_configs
        configs = load_ssh_configs()
        server = next((c for c in configs if c.get('id') == server_id), None)
    except Exception:
        server = None
    if not server:
        _clear_transfer_task()
        return

    # Quick connectivity check before resuming
    import socket as _socket
    try:
        s = _socket.create_connection((server.get('host', ''), server.get('port', 22)), timeout=10)
        s.close()
    except Exception:
        print(f'  [transfer-resume] Server {server.get("name", server.get("host", ""))} is unreachable, skipping resume for now')
        # Don't clear the task — it will be retried on next restart
        return

    # Compute totals
    total_bytes = 0
    total_files = 0
    for rp in valid:
        if os.path.isdir(rp):
            for root, dirs, files in os.walk(rp):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except OSError:
                        pass
        else:
            try:
                total_bytes += os.path.getsize(rp)
                total_files += 1
            except OSError:
                pass

    with _fileop_lock:
        if _fileop_state['active']:
            return  # something else already running
        _fileop_state['active'] = True
        _fileop_state['operation'] = 'transfer'
        _fileop_state['progress'] = None
        _fileop_state['cancel'] = False
        _fileop_state['meta'] = {
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paths': [os.path.basename(p) for p in valid[:10]],
            'total_bytes': total_bytes,
            'total_files': total_files,
            'started': started,
            'resumed': True,
        }

    dest = remote_dest or server.get('remote_path', '~/')
    print(f'  [transfer-resume] Resuming transfer of {len(valid)} items to {server.get("name", server.get("host", ""))}…')
    elog('files', 'info', f'Resuming interrupted transfer to {server.get("name", server.get("host", ""))} ({len(valid)} items)')
    socketio.start_background_task(_bg_transfer_remote, valid, server, dest, total_bytes, total_files, requesting_user)


# ─────────────────────────── Apps Registry ───────────────────────────

@app.route('/api/apps')
@require_auth
def get_apps():
    apps = [
        {
            'id': 'dashboard',
            'name': 'Dashboard',
            'icon': 'fa-tachometer-alt',
            'color': '#3b82f6',
            'type': 'builtin',
            'category': 'System',
            'description': 'System overview'
        },
        {
            'id': 'file-manager',
            'name': 'File Manager',
            'icon': 'fa-folder-open',
            'color': '#f59e0b',
            'type': 'builtin',
            'category': 'System',
            'description': 'Browse and manage files'
        },
        {
            'id': 'docker-manager',
            'name': 'Docker',
            'icon': 'fa-cubes',
            'color': '#2496ed',
            'type': 'builtin',
            'category': 'System',
            'description': 'Container management',
            'package': 'docker-manager'
        },
        {
            'id': 'vm-manager',
            'name': 'VM Manager',
            'icon': 'fa-desktop',
            'color': '#8b5cf6',
            'type': 'builtin',
            'category': 'System',
            'description': 'Virtual machines (QEMU/KVM)',
            'admin_only': True,
            'package': 'vm-manager'
        },
        {
            'id': 'storage-manager',
            'name': 'Storage Manager',
            'icon': 'fa-database',
            'color': '#10b981',
            'type': 'builtin',
            'category': 'Storage',
            'description': 'Disks, RAID, volumes, sharing and diagnostics',
            'package': 'storage-manager',
        },
        {
            'id': 'backup',
            'name': 'Backup',
            'icon': 'fa-shield-alt',
            'color': '#06b6d4',
            'type': 'builtin',
            'category': 'Storage',
            'description': 'Create and restore backups',
            'package': 'backup',
        },
        {
            'id': 'cloud-backup',
            'name': 'Cloud Backup',
            'icon': 'fa-cloud-upload-alt',
            'color': '#0ea5e9',
            'type': 'builtin',
            'category': 'Storage',
            'description': 'Cloud backup (S3, B2, Google Drive, WebDAV, SFTP)',
            'admin_only': True,
            'package': 'cloud-backup'
        },
        {
            'id': 'rollback',
            'name': 'Rollback',
            'icon': 'fa-history',
            'color': '#f97316',
            'type': 'builtin',
            'category': 'System',
            'description': 'System snapshots and version rollback',
            'admin_only': True,
            'package': 'rollback',
        },
        {
            'id': 'resource-monitor',
            'name': 'Resource Monitor',
            'icon': 'fa-chart-area',
            'color': '#8b5cf6',
            'type': 'builtin',
            'category': 'System',
            'description': 'Detailed monitoring',
            'package': 'resource-monitor',
        },
        {
            'id': 'printer',
            'name': 'Print Server',
            'icon': 'fa-print',
            'color': '#ef4444',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Document printing',
            'package': 'printer'
        },
        {
            'id': 'terminal',
            'name': 'Terminal',
            'icon': 'fa-terminal',
            'color': '#22c55e',
            'type': 'builtin',
            'category': 'System',
            'description': 'Command line (SSH-like)',
            'package': 'terminal',
        },
        {
            'id': 'packages',
            'name': 'Package Manager',
            'icon': 'fa-store',
            'color': '#a855f7',
            'type': 'builtin',
            'category': 'System',
            'description': 'Install and update software'
        },
        {
            'id': 'users',
            'name': 'Users',
            'icon': 'fa-users-cog',
            'color': '#ec4899',
            'type': 'builtin',
            'category': 'System',
            'description': 'User and group management',
            'admin_only': True
        },
        {
            'id': 'network',
            'name': 'Network',
            'icon': 'fa-network-wired',
            'color': '#0ea5e9',
            'type': 'builtin',
            'category': 'System',
            'description': 'Network interface and WiFi management',
            'package': 'network',
        },
        {
            'id': 'event-log',
            'name': 'Event Log',
            'icon': 'fa-scroll',
            'color': '#64748b',
            'type': 'builtin',
            'category': 'System',
            'description': 'Logs and operation history'
        },
        {
            'id': 'notifications',
            'name': 'Notifications',
            'icon': 'fa-bell',
            'color': '#f59e0b',
            'type': 'builtin',
            'category': 'System',
            'description': 'System notification channels',
            'package': 'notifications',
        },
        {
            'id': 'fail2ban',
            'name': 'Intrusion Protection',
            'icon': 'fa-shield-alt',
            'color': '#ef4444',
            'type': 'builtin',
            'category': 'System',
            'description': 'Fail2Ban — active bans, whitelist, SSH/Samba/Web protection',
            'admin_only': True,
            'package': 'fail2ban',
        },
        {
            'id': 'firewall',
            'name': 'Firewall (UFW)',
            'icon': 'fa-fire',
            'color': '#e05d44',
            'type': 'builtin',
            'category': 'System',
            'description': 'Firewall and rules management',
            'admin_only': True,
            'package': 'firewall',
        },
        {
            'id': 'cron',
            'name': 'Scheduler',
            'icon': 'fa-clock',
            'color': '#6366f1',
            'type': 'builtin',
            'category': 'System',
            'description': 'Cron job management (scheduler)',
            'admin_only': True,
            'package': 'cron',
        },
        {
            'id': 'app-store',
            'name': 'App Store',
            'icon': 'fa-th',
            'color': '#f97316',
            'type': 'builtin',
            'category': 'System',
            'description': 'EthOS packages and Docker containers',
            'admin_only': True
        },
        {
            'id': 'gallery',
            'name': 'Gallery',
            'icon': 'fa-images',
            'color': '#ec4899',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Photo and video gallery from selected folders',
            'package': 'gallery'
        },
        {
            'id': 'duplicates',
            'name': 'Photo Duplicates',
            'icon': 'fa-clone',
            'color': '#a78bfa',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Find identical and similar photos',
            'package': 'duplicates'
        },
        {
            'id': 'doc-editor',
            'name': 'Document Editor',
            'icon': 'fa-file-word',
            'color': '#2563eb',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Create and edit Word documents, export to PDF',
            'package': 'doc-editor'
        },
        {
            'id': 'code-editor',
            'name': 'Code Editor',
            'icon': 'fa-code',
            'color': '#22d3ee',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Simple code editor with line numbers and formatting',
            'package': 'code-editor'
        },
        {
            'id': 'download-manager',
            'name': 'Download Manager',
            'icon': 'fa-cloud-download-alt',
            'color': '#10b981',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Download files with premium services (AllDebrid, Real-Debrid, Premiumize)',
            'package': 'download-manager'
        },
        {
            'id': 'usb-flasher',
            'name': 'USB Flasher',
            'icon': 'fa-usb',
            'color': '#a855f7',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Flash ISO/IMG images to USB drives',
            'admin_only': True,
            'package': 'usb-flasher'
        },
        {
            'id': 'builder',
            'name': 'Builder',
            'icon': 'fa-hammer',
            'color': '#f97316',
            'type': 'builtin',
            'category': 'System',
            'description': 'Build EthOS releases and system images',
            'admin_only': True,
            'package': 'builder'
        },
        {
            'id': 'updates',
            'name': 'Updates',
            'icon': 'fa-cloud-download-alt',
            'color': '#8b5cf6',
            'type': 'builtin',
            'category': 'System',
            'description': 'Check and install system updates',
            'admin_only': True,
            'package': 'updates',
        },
        {
            'id': 'power',
            'name': 'Power Management',
            'icon': 'fa-power-off',
            'color': '#22c55e',
            'type': 'builtin',
            'category': 'System',
            'description': 'Schedule, WOL, power saving',
            'admin_only': True,
            'package': 'power',
        },
        {
            'id': 'ups',
            'name': 'UPS',
            'icon': 'fa-battery-full',
            'color': '#f59e0b',
            'type': 'builtin',
            'category': 'System',
            'description': 'UPS status and management',
            'admin_only': True,
            'package': 'ups',
        },
        {
            'id': 'services',
            'name': 'Services',
            'icon': 'fa-cogs',
            'color': '#64748b',
            'type': 'builtin',
            'category': 'System',
            'description': 'System service management',
            'admin_only': True,
            'package': 'services',
        },
        {
            'id': 'remote-log',
            'name': 'Remote Logs',
            'icon': 'fa-satellite-dish',
            'color': '#0891b2',
            'type': 'builtin',
            'category': 'System',
            'description': 'Send diagnostic logs to central server',
            'admin_only': True,
            'package': 'remote-log'
        },
        {
            'id': 'surveillance',
            'name': 'Monitoring',
            'icon': 'fa-video',
            'color': '#dc2626',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'IP camera monitoring with detection and recording',
            'admin_only': True,
            'package': 'surveillance'
        },
        {
            'id': 'ai-chat',
            'name': 'AI Chat',
            'icon': 'fa-robot',
            'color': '#8b5cf6',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'AI assistant — chat with GPT, Claude and other models',
            'admin_only': True,
            'package': 'ai-chat'
        },
        {
            'id': 'doc-anonymizer',
            'name': 'Document Anonymizer',
            'icon': 'fa-user-shield',
            'color': '#0ea5e9',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Anonymize medical PDF/DOCX documents using Bielik LLM',
            'package': 'doc-anonymizer'
        },
        {
            'id': 'med-assistant',
            'name': 'Medical Assistant',
            'icon': 'fa-user-md',
            'color': '#06b6d4',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Medical document analysis with Bielik LLM - timelines, drug interactions, ESC guidelines',
            'package': 'med-assistant'
        },
        {
            'id': 'system-settings',
            'name': 'Settings',
            'icon': 'fa-sliders-h',
            'color': '#64748b',
            'type': 'builtin',
            'category': 'System',
            'description': 'NAS name, server port, hostname, timezone, password change',
            'admin_only': True
        },
        {
            'id': 'domains-manager',
            'name': 'Domains & SSL',
            'icon': 'fa-globe',
            'color': '#059669',
            'type': 'builtin',
            'category': 'Network',
            'description': 'Domain, SSL certificate, reverse proxy and Dynamic DNS management',
            'admin_only': True,
            'package': 'ddns'
        },
        {
            'id': 'websites',
            'name': 'Websites',
            'icon': 'fa-globe-americas',
            'color': '#14b8a6',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Create and manage websites with a simple CMS',
            'package': 'websites'
        },
        {
            'id': 'naslink',
            'name': 'NASLink',
            'icon': 'fa-network-wired',
            'color': '#06b6d4',
            'type': 'builtin',
            'category': 'Network',
            'description': 'Connectivity and sync between NAS devices',
            'package': 'naslink',
        },
        {
            'id': 'ssh-manager',
            'name': 'SSH Manager',
            'icon': 'fa-key',
            'color': '#6366f1',
            'type': 'builtin',
            'category': 'Network',
            'description': 'SSH key and trusted host management',
            'package': 'ssh-manager',
        },
        {
            'id': 'sticky-notes',
            'name': 'Sticky Notes',
            'icon': 'fa-sticky-note',
            'color': '#eab308',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Quick notes — like sticky notes on a desktop',
            'package': 'sticky-notes',
        },
        {
            'id': 'tickets',
            'name': 'Tickets',
            'icon': 'fa-columns',
            'color': '#8b5cf6',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Project management — Jira/Trello-style Kanban board',
            'package': 'tickets',
        },
        {
            'id': 'family-hub',
            'name': 'Family Hub',
            'icon': 'fa-house-user',
            'color': '#f472b6',
            'type': 'builtin',
            'category': 'Tools',
            'description': 'Bulletin board, shopping lists, tasks and family calendar',
            'package': 'family-hub',
        },
        {
            'id': 'wireguard',
            'name': 'VPN (WireGuard)',
            'icon': 'fa-shield-halved',
            'color': '#7c3aed',
            'type': 'builtin',
            'category': 'Network',
            'description': 'WireGuard VPN server — manage peers, generate QR codes',
            'admin_only': True,
            'package': 'wireguard',
        },
        {
            'id': 'antivirus',
            'name': 'Antivirus (ClamAV)',
            'icon': 'fa-shield-virus',
            'color': '#16a34a',
            'type': 'builtin',
            'category': 'Security',
            'description': 'ClamAV antivirus — on-demand and scheduled scans',
            'admin_only': True,
            'package': 'antivirus',
        }
    ]

    # Mark package-based apps with install status and hide uninstalled ones
    pkg_state = _load_packages_state()
    # Surveillance is installable from App Store only.
    # If an old image marked it as auto-detected, clear that marker.
    if pkg_state.get('surveillance', {}).get('installed_at') == 'auto-detected':
        pkg_state['surveillance'] = {'installed': False, 'installed_at': ''}
        _save_packages_state(pkg_state)
    pkg_ids = {p['app_id']: p['id'] for p in _ETHOS_PACKAGES}
    # 1-to-many: one app_id may map to multiple packages (e.g. sharing → 6 protocols)
    from collections import defaultdict as _dtd
    _pkg_by_app = _dtd(list)
    for _p in _ETHOS_PACKAGES:
        _pkg_by_app[_p['app_id']].append(_p['id'])

    # Also check installed_apps.json (Package Center state)
    _pm_installed = _load_app_manager_installed()

    apps = [a for a in apps if a['id'] not in _pkg_by_app
            or a['id'] in _APP_MANAGER_CORE_APPS
            or a['id'] in _pm_installed
            or any(pkg_state.get(pid, {}).get('installed') for pid in _pkg_by_app[a['id']])]

    # Auto-discover installed catalog apps not in the hardcoded list
    _existing_ids = {a['id'] for a in apps}
    for _cat_app in _BUILTIN_CATALOG:
        _cid = _cat_app['id']
        if _cid not in _existing_ids and _cid in _pm_installed and not _cat_app.get('hidden'):
            apps.append({
                'id': _cid,
                'name': _cat_app.get('name', _cid),
                'icon': _cat_app.get('icon', 'fa-puzzle-piece'),
                'color': _cat_app.get('color', '#6b7280'),
                'type': 'builtin',
                'category': _cat_app.get('category', 'Tools'),
                'description': _cat_app.get('description', ''),
                'admin_only': _cat_app.get('admin_only', False),
                'package': _cid,
            })

    # Filter by privileges
    user = get_current_user()
    if user and user['role'] != 'admin':
        allowed = _user_allowed_apps(user['username'], user['role'])
        if allowed is not None:
            # Has restrictions — filter apps
            apps = [a for a in apps if not a.get('admin_only') and a['id'] in allowed]
        else:
            # No restrictions — show all except admin_only
            apps = [a for a in apps if not a.get('admin_only')]
    else:
        # Admin sees all
        pass

    return jsonify(apps)


# ─────────────────────────── EthOS Packages ───────────────────────────

PACKAGES_STATE_FILE = _data_path('ethos_packages.json')

# Registry of installable EthOS packages
# Each package must define: id, name, icon, color, description, app_id (matches get_apps id),
#   check_fn (returns bool — is it ready?), install_endpoint, uninstall_endpoint
_ETHOS_PACKAGES = [
    {
        'id': 'surveillance',
        'name': 'Surveillance',
        'icon': 'fa-video',
        'color': '#dc2626',
        'description': 'IP camera monitoring system with motion detection, continuous recording and live view.',
        'app_id': 'surveillance',
        'deps_label': 'ffmpeg, ffprobe, python-onvif',
        'install_endpoint': '/api/surveillance/install',
        'uninstall_endpoint': '/api/surveillance/uninstall',
        'status_endpoint': '/api/surveillance/status',
    },
    {
        'id': 'ai-chat',
        'name': 'AI Assistant',
        'icon': 'fa-robot',
        'color': '#8b5cf6',
        'description': 'AI assistant with developer tools supporting GPT, Claude, and local LLM models.',
        'app_id': 'ai-chat',
        'deps_label': 'local models: llama-cpp-python',
        'install_endpoint': '/api/aichat/install',
        'uninstall_endpoint': '/api/aichat/uninstall',
        'status_endpoint': '/api/aichat/status',
    },
    {
        'id': 'gallery',
        'name': 'Gallery',
        'icon': 'fa-images',
        'color': '#ec4899',
        'description': 'Photo and video gallery with EXIF data, thumbnails, favorites and folder passwords.',
        'app_id': 'gallery',
        'deps_label': 'no requirements',
        'install_endpoint': '/api/gallery/install',
        'uninstall_endpoint': '/api/gallery/uninstall',
        'status_endpoint': '/api/gallery/pkg-status',
    },
    {
        'id': 'download-manager',
        'name': 'Downloads',
        'icon': 'fa-cloud-download-alt',
        'color': '#10b981',
        'description': 'Download files with premium services supporting HTTP, torrent, and magnet links.',
        'app_id': 'download-manager',
        'deps_label': 'optional: debrid account',
        'install_endpoint': '/api/downloads/install',
        'uninstall_endpoint': '/api/downloads/uninstall',
        'status_endpoint': '/api/downloads/pkg-status',
    },
    {
        'id': 'printer',
        'name': 'Printing',
        'icon': 'fa-print',
        'color': '#ef4444',
        'description': 'Document printing service with automatic printer detection and PDF conversion.',
        'app_id': 'printer',
        'deps_label': 'cups, libreoffice',
        'install_endpoint': '/api/printer/install',
        'uninstall_endpoint': '/api/printer/uninstall',
        'status_endpoint': '/api/printer/pkg-status',
    },
    {
        'id': 'docker-manager',
        'name': 'Docker',
        'icon': 'fa-cubes',
        'color': '#2496ed',
        'description': 'Container management with Docker Compose projects, images and logs.',
        'app_id': 'docker-manager',
        'deps_label': 'docker, docker-compose',
        'install_endpoint': '/api/docker/install',
        'uninstall_endpoint': '/api/docker/uninstall',
        'status_endpoint': '/api/docker/pkg-status',
    },
    {
        'id': 'vm-manager',
        'name': 'VM Manager',
        'icon': 'fa-desktop',
        'color': '#8b5cf6',
        'description': 'Create and manage virtual machines with QEMU/KVM supporting snapshots and VNC access.',
        'app_id': 'vm-manager',
        'deps_label': 'qemu-system-x86, qemu-utils, ovmf',
        'install_endpoint': '/api/vm/install',
        'uninstall_endpoint': '/api/vm/uninstall',
        'status_endpoint': '/api/vm/pkg-status',
    },
    {
        'id': 'doc-editor',
        'name': 'Documents',
        'icon': 'fa-file-word',
        'color': '#2563eb',
        'description': 'Create and edit Word documents with PDF export using LibreOffice.',
        'app_id': 'doc-editor',
        'deps_label': 'libreoffice, mammoth',
        'install_endpoint': '/api/editor/install',
        'uninstall_endpoint': '/api/editor/uninstall',
        'status_endpoint': '/api/editor/pkg-status',
    },
    {
        'id': 'code-editor',
        'name': 'Code Editor',
        'icon': 'fa-code',
        'color': '#22d3ee',
        'description': 'Simple code editor with line numbers, syntax highlighting and formatting.',
        'app_id': 'code-editor',
        'deps_label': 'no requirements',
        'install_endpoint': '/api/code-editor/install',
        'uninstall_endpoint': '/api/code-editor/uninstall',
        'status_endpoint': '/api/code-editor/pkg-status',
    },
    {
        'id': 'duplicates',
        'name': 'Duplicates',
        'icon': 'fa-clone',
        'color': '#a78bfa',
        'description': 'Find identical and similar photos on disk using perceptual hashing.',
        'app_id': 'duplicates',
        'deps_label': 'no requirements',
        'install_endpoint': '/api/files/duplicates/install',
        'uninstall_endpoint': '/api/files/duplicates/uninstall',
        'status_endpoint': '/api/files/duplicates/pkg-status',
    },
    {
        'id': 'usb-flasher',
        'name': 'USB Creator',
        'icon': 'fa-usb',
        'color': '#a855f7',
        'description': 'Flash ISO/IMG images to USB drives with progress monitoring.',
        'app_id': 'usb-flasher',
        'deps_label': 'dd',
        'install_endpoint': '/api/flasher/install',
        'uninstall_endpoint': '/api/flasher/uninstall',
        'status_endpoint': '/api/flasher/pkg-status',
    },
    {
        'id': 'builder',
        'name': 'Builder',
        'icon': 'fa-hammer',
        'color': '#f97316',
        'description': 'Build EthOS releases and system images from the web interface.',
        'app_id': 'builder',
        'deps_label': 'squashfs-tools, genisoimage',
        'install_endpoint': '/api/builder/install',
        'uninstall_endpoint': '/api/builder/uninstall',
        'status_endpoint': '/api/builder/pkg-status',
    },
    {
        'id': 'disk-repair',
        'name': 'Disk Repair',
        'icon': 'fa-wrench',
        'color': '#ef4444',
        'description': 'SMART diagnostics and file system checking with repair tools.',
        'app_id': 'storage-manager',
        'deps_label': 'smartmontools, e2fsprogs',
        'install_endpoint': '/api/diskrepair/install',
        'uninstall_endpoint': '/api/diskrepair/uninstall',
        'status_endpoint': '/api/diskrepair/pkg-status',
    },
    {
        'id': 'remote-log',
        'name': 'Remote Logs',
        'icon': 'fa-satellite-dish',
        'color': '#0891b2',
        'description': 'Send diagnostic logs to central server and receive logs from devices.',
        'app_id': 'remote-log',
        'deps_label': 'no requirements',
        'install_endpoint': '/api/remote-log/install',
        'uninstall_endpoint': '/api/remote-log/uninstall',
        'status_endpoint': '/api/remote-log/pkg-status',
    },
    {
        'id': 'sharing-samba',
        'name': 'File Sharing',
        'icon': 'fa-windows',
        'color': '#6366f1',
        'description': 'Samba network shares visible across Windows, Mac and Linux systems.',
        'app_id': 'storage-manager',
        'deps_label': 'samba',
        'install_endpoint': '/api/storage/samba/pkg-install',
        'uninstall_endpoint': '/api/storage/samba/pkg-uninstall',
        'status_endpoint': '/api/storage/samba/pkg-status',
    },
    {
        'id': 'sharing-nfs',
        'name': 'NFS',
        'icon': 'fa-network-wired',
        'color': '#6366f1',
        'description': 'Fast file sharing for Linux/Unix systems using NFS protocol.',
        'app_id': 'storage-manager',
        'deps_label': 'nfs-kernel-server',
        'install_endpoint': '/api/storage/nfs/pkg-install',
        'uninstall_endpoint': '/api/storage/nfs/pkg-uninstall',
        'status_endpoint': '/api/storage/nfs/pkg-status',
    },
    {
        'id': 'sharing-dlna',
        'name': 'DLNA (MiniDLNA)',
        'icon': 'fa-photo-video',
        'color': '#6366f1',
        'description': 'DLNA media server for streaming music, videos and photos to TVs and players.',
        'app_id': 'storage-manager',
        'deps_label': 'minidlna',
        'install_endpoint': '/api/storage/dlna/pkg-install',
        'uninstall_endpoint': '/api/storage/dlna/pkg-uninstall',
        'status_endpoint': '/api/storage/dlna/pkg-status',
    },
    {
        'id': 'sharing-webdav',
        'name': 'WebDAV',
        'icon': 'fa-globe',
        'color': '#6366f1',
        'description': 'WebDAV server for HTTP file access with authentication.',
        'app_id': 'storage-manager',
        'deps_label': 'lighttpd',
        'install_endpoint': '/api/storage/webdav/pkg-install',
        'uninstall_endpoint': '/api/storage/webdav/pkg-uninstall',
        'status_endpoint': '/api/storage/webdav/pkg-status',
    },
    {
        'id': 'sharing-sftp',
        'name': 'SFTP',
        'icon': 'fa-lock',
        'color': '#6366f1',
        'description': 'Secure file transfer over SSH with encrypted connections.',
        'app_id': 'storage-manager',
        'deps_label': 'openssh-server',
        'install_endpoint': '/api/storage/sftp/pkg-install',
        'uninstall_endpoint': '/api/storage/sftp/pkg-uninstall',
        'status_endpoint': '/api/storage/sftp/pkg-status',
    },
    {
        'id': 'sharing-ftp',
        'name': 'FTP',
        'icon': 'fa-upload',
        'color': '#6366f1',
        'description': 'Classic FTP file transfer server using vsftpd.',
        'app_id': 'storage-manager',
        'deps_label': 'vsftpd',
        'install_endpoint': '/api/storage/ftp/pkg-install',
        'uninstall_endpoint': '/api/storage/ftp/pkg-uninstall',
        'status_endpoint': '/api/storage/ftp/pkg-status',
    },
    {
        'id': 'domains-manager',
        'name': 'Domains',
        'icon': 'fa-globe',
        'color': '#059669',
        'description': 'Domain management with SSL certificates, reverse proxy and Dynamic DNS support.',
        'app_id': 'domains-manager',
        'deps_label': 'optional: certbot',
        'install_endpoint': '/api/ddns/install',
        'uninstall_endpoint': '/api/ddns/uninstall',
        'status_endpoint': '/api/ddns/pkg-status',
    },
    {
        'id': 'websites',
        'name': 'Websites',
        'icon': 'fa-globe-americas',
        'color': '#14b8a6',
        'description': 'Website builder with simple CMS including templates and visual editor.',
        'app_id': 'websites',
        'deps_label': 'no requirements',
        'install_endpoint': '/api/websites/install',
        'uninstall_endpoint': '/api/websites/uninstall',
        'status_endpoint': '/api/websites/pkg-status',
    },
    {
        'id': 'cloud-backup',
        'name': 'Cloud Backup',
        'icon': 'fa-cloud-upload-alt',
        'color': '#0ea5e9',
        'description': 'Cloud backup to S3, Backblaze B2, Google Drive, WebDAV and SFTP with scheduling.',
        'app_id': 'cloud-backup',
        'deps_label': 'rclone',
        'install_endpoint': '/api/cloud-backup/install',
        'uninstall_endpoint': '/api/cloud-backup/uninstall',
        'status_endpoint': '/api/cloud-backup/pkg-status',
    },
    {
        'id': 'raid-lvm',
        'name': 'RAID / LVM',
        'icon': 'fa-layer-group',
        'color': '#f59e0b',
        'description': 'RAID array management with mdadm and logical volume management with LVM.',
        'app_id': 'storage-manager',
        'deps_label': 'mdadm, lvm2',
        'install_endpoint': '/api/raid/install',
        'uninstall_endpoint': '/api/raid/uninstall',
        'status_endpoint': '/api/raid/pkg-status',
    },
    # ─── Simple apps (no system deps — just toggle visibility) ───
    {
        'id': 'storage-manager',
        'name': 'Storage Manager',
        'icon': 'fa-database',
        'color': '#10b981',
        'description': 'Unified disk, RAID, volume, sharing and diagnostics management.',
        'app_id': 'storage-manager',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Storage',
    },
    {
        'id': 'backup',
        'name': 'Backup',
        'icon': 'fa-shield-alt',
        'color': '#06b6d4',
        'description': 'Create and restore local backups.',
        'app_id': 'backup',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Storage',
    },
    {
        'id': 'rollback',
        'name': 'Rollback',
        'icon': 'fa-history',
        'color': '#f97316',
        'description': 'System snapshots and version rollback.',
        'app_id': 'rollback',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'resource-monitor',
        'name': 'Resource Monitor',
        'icon': 'fa-chart-area',
        'color': '#8b5cf6',
        'description': 'CPU, memory, disk and network usage over time.',
        'app_id': 'resource-monitor',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'terminal',
        'name': 'Terminal',
        'icon': 'fa-terminal',
        'color': '#22c55e',
        'description': 'Command line (SSH-like) access from the browser.',
        'app_id': 'terminal',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'network',
        'name': 'Network',
        'icon': 'fa-network-wired',
        'color': '#0ea5e9',
        'description': 'Network interface and WiFi management.',
        'app_id': 'network',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Network',
    },
    {
        'id': 'notifications',
        'name': 'Notifications',
        'icon': 'fa-bell',
        'color': '#f59e0b',
        'description': 'System notification channels (email, Telegram, Pushbullet).',
        'app_id': 'notifications',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'fail2ban',
        'name': 'Intrusion Protection',
        'icon': 'fa-shield-alt',
        'color': '#ef4444',
        'description': 'Fail2Ban — active bans, whitelist, SSH/Samba/Web protection.',
        'app_id': 'fail2ban',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Security',
    },
    {
        'id': 'firewall',
        'name': 'Firewall (UFW)',
        'icon': 'fa-fire',
        'color': '#e05d44',
        'description': 'Firewall and port rules management.',
        'app_id': 'firewall',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Security',
    },
    {
        'id': 'cron',
        'name': 'Scheduler',
        'icon': 'fa-clock',
        'color': '#6366f1',
        'description': 'Task scheduler with cron job management.',
        'app_id': 'cron',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'updates',
        'name': 'Updates',
        'icon': 'fa-cloud-download-alt',
        'color': '#8b5cf6',
        'description': 'Check and install EthOS system updates.',
        'app_id': 'updates',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'power',
        'name': 'Power Management',
        'icon': 'fa-power-off',
        'color': '#22c55e',
        'description': 'Power schedule, Wake-on-LAN, HDD spindown and CPU governor.',
        'app_id': 'power',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'ups',
        'name': 'UPS',
        'icon': 'fa-battery-full',
        'color': '#f59e0b',
        'description': 'UPS battery status and safe shutdown management.',
        'app_id': 'ups',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'services',
        'name': 'Services',
        'icon': 'fa-cogs',
        'color': '#64748b',
        'description': 'System service management.',
        'app_id': 'services',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'System',
    },
    {
        'id': 'naslink',
        'name': 'NASLink',
        'icon': 'fa-network-wired',
        'color': '#06b6d4',
        'description': 'Connectivity and sync between NAS devices.',
        'app_id': 'naslink',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Network',
    },
    {
        'id': 'ssh-manager',
        'name': 'SSH Manager',
        'icon': 'fa-key',
        'color': '#6366f1',
        'description': 'SSH key and trusted host management.',
        'app_id': 'ssh-manager',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Network',
    },
    {
        'id': 'sticky-notes',
        'name': 'Sticky Notes',
        'icon': 'fa-sticky-note',
        'color': '#eab308',
        'description': 'Quick notes — like sticky notes on a desktop.',
        'app_id': 'sticky-notes',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Tools',
    },
    {
        'id': 'tickets',
        'name': 'Tickets',
        'icon': 'fa-columns',
        'color': '#8b5cf6',
        'description': 'Project management — Kanban board.',
        'app_id': 'tickets',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Tools',
    },
    {
        'id': 'family-hub',
        'name': 'Family Hub',
        'icon': 'fa-house-user',
        'color': '#f472b6',
        'description': 'Bulletin board, shopping lists, tasks and family calendar.',
        'app_id': 'family-hub',
        'deps_label': 'no requirements',
        'simple': True,
        'category': 'Tools',
    },
    {
        'id': 'wireguard',
        'name': 'VPN (WireGuard)',
        'icon': 'fa-shield-halved',
        'color': '#7c3aed',
        'description': 'WireGuard VPN server — manage peers, generate QR codes.',
        'app_id': 'wireguard',
        'deps_label': 'wireguard, wireguard-tools, qrencode',
        'install_endpoint': '/api/wireguard/install',
        'uninstall_endpoint': '/api/wireguard/uninstall',
        'status_endpoint': '/api/wireguard/pkg-status',
        'category': 'Network',
    },
    {
        'id': 'antivirus',
        'name': 'Antivirus (ClamAV)',
        'icon': 'fa-shield-virus',
        'color': '#16a34a',
        'description': 'ClamAV antivirus scanner with scheduler.',
        'app_id': 'antivirus',
        'deps_label': 'clamav, clamav-freshclam',
        'install_endpoint': '/api/antivirus/install',
        'uninstall_endpoint': '/api/antivirus/uninstall',
        'status_endpoint': '/api/antivirus/pkg-status',
        'category': 'Security',
    },

]

# Lock to prevent concurrent reads/writes corrupting the packages state under gevent
import gevent.lock as _gevent_lock
_PACKAGES_STATE_LOCK = _gevent_lock.RLock()

def _load_packages_state():
    """Load dict of package_id → { installed: bool, installed_at: str }"""
    with _PACKAGES_STATE_LOCK:
        return _load_json(PACKAGES_STATE_FILE, {})

def _save_packages_state(state):
    with _PACKAGES_STATE_LOCK:
        _save_json(PACKAGES_STATE_FILE, state)

@app.route('/api/ethos-packages')
@require_auth
def list_ethos_packages():
    """List all available EthOS packages with install status."""
    state = _load_packages_state()
    changed = False

    # Surveillance must not be auto-installed based on host deps.
    # Keep explicit installs; clear only legacy auto-detected marker.
    if state.get('surveillance', {}).get('installed_at') == 'auto-detected':
        state['surveillance'] = {'installed': False, 'installed_at': ''}
        changed = True

    # Auto-detect: packages with no external deps default to installed
    if 'surveillance' not in state:
        _no_deps_pkgs = ('gallery', 'duplicates', 'code-editor',
                         'usb-flasher', 'builder', 'remote-log', 'domains-manager', 'websites')
        for pid in _no_deps_pkgs:
            if pid not in state:
                state[pid] = {'installed': True, 'installed_at': 'auto-detected'}
                changed = True

    # Auto-detect docker
    if 'docker-manager' not in state:
        import shutil as _shutil_ad
        if _shutil_ad.which('docker'):
            state['docker-manager'] = {'installed': True, 'installed_at': 'auto-detected'}
            changed = True
    # Auto-detect QEMU / VM Manager
    if 'vm-manager' not in state:
        import shutil as _shutil_qm
        if _shutil_qm.which('qemu-system-x86_64'):
            state['vm-manager'] = {'installed': True, 'installed_at': 'auto-detected'}
            changed = True

    # Auto-detect wireguard (also correct migration/auto-detected state if binary missing)
    import shutil as _shutil_wg
    _wg_present = bool(_shutil_wg.which('wg') or _shutil_wg.which('wg-quick'))
    _wg_state = state.get('wireguard', {})
    if 'wireguard' not in state:
        if _wg_present:
            state['wireguard'] = {'installed': True, 'installed_at': 'auto-detected'}
            changed = True
    elif _wg_state.get('installed') and _wg_state.get('installed_at') in ('migration', 'auto-detected'):
        # Correct: binary check overrides migration guess for non-simple packages
        if not _wg_present:
            state['wireguard'] = {'installed': False, 'installed_at': ''}
            changed = True

    # Auto-detect dep-based packages
    if 'printer' not in state:
        import shutil as _shutil_pr
        if _shutil_pr.which('lpadmin') or _shutil_pr.which('cups'):
            state['printer'] = {'installed': True, 'installed_at': 'auto-detected'}
            changed = True
    if 'doc-editor' not in state:
        import shutil as _shutil_lo
        if _shutil_lo.which('libreoffice') or _shutil_lo.which('soffice'):
            state['doc-editor'] = {'installed': True, 'installed_at': 'auto-detected'}
            changed = True
    if 'disk-repair' not in state:
        import shutil as _shutil_sm
        if _shutil_sm.which('smartctl'):
            state['disk-repair'] = {'installed': True, 'installed_at': 'auto-detected'}
            changed = True

    # Auto-detect sharing protocols (6 separate packages)
    import shutil as _shutil_sh
    _sharing_detect = {
        'sharing-samba': lambda: bool(_shutil_sh.which('smbd')),
        'sharing-nfs': lambda: bool(_shutil_sh.which('exportfs')),
        'sharing-dlna': lambda: bool(_shutil_sh.which('minidlnad')),
        'sharing-webdav': lambda: bool(_shutil_sh.which('lighttpd')),
        'sharing-sftp': lambda: _host_run_base("grep -q '^Subsystem.*sftp' /etc/ssh/sshd_config 2>/dev/null", timeout=5).returncode == 0,
        'sharing-ftp': lambda: bool(_shutil_sh.which('vsftpd')),
    }
    for _pid, _detect in _sharing_detect.items():
        if _pid not in state:
            try:
                if _detect():
                    state[_pid] = {'installed': True, 'installed_at': 'auto-detected'}
                    changed = True
            except Exception:
                pass

    # Auto-install simple packages for existing users (migration)
    # Mark as installed unless the user explicitly uninstalled them (installed_at is non-empty)
    _simple_pkg_ids = [p['id'] for p in _ETHOS_PACKAGES if p.get('simple')]
    for pid in _simple_pkg_ids:
        current = state.get(pid, {})
        # Never in state, or in state with installed=False and no installed_at
        # (stale entry from before this system existed — not an intentional uninstall)
        if pid not in state or (not current.get('installed') and not current.get('installed_at')):
            state[pid] = {'installed': True, 'installed_at': 'migration'}
            changed = True

    # Migrate legacy 'sharing' state → sharing-samba (one-time upgrade)
    if 'sharing' in state and 'sharing-samba' not in state:
        state['sharing-samba'] = state['sharing']
        changed = True

    if changed:
        _save_packages_state(state)

    result = []
    for pkg in _ETHOS_PACKAGES:
        pkg_state = state.get(pkg['id'], {})
        item = {
            'id': pkg['id'],
            'name': pkg['name'],
            'icon': pkg['icon'],
            'color': pkg['color'],
            'description': pkg['description'],
            'app_id': pkg['app_id'],
            'deps_label': pkg.get('deps_label', ''),
            'simple': pkg.get('simple', False),
            'category': pkg.get('category', ''),
            'install_endpoint': pkg.get('install_endpoint'),
            'uninstall_endpoint': pkg.get('uninstall_endpoint'),
            'status_endpoint': pkg.get('status_endpoint'),
            'installed': pkg_state.get('installed', False),
            'installed_at': pkg_state.get('installed_at', ''),
        }
        result.append(item)
    return jsonify(result)

@app.route('/api/ethos-packages/<pkg_id>/install', methods=['POST'])
@require_auth


def install_ethos_package(pkg_id):
    """Mark package as installed. The actual install is triggered by the frontend
    calling the package's own install endpoint (e.g., /api/surveillance/install)."""
    if getattr(g, 'role', None) != 'admin':
        return jsonify({'error': t('auth.no_permission')}), 403

    pkg = next((p for p in _ETHOS_PACKAGES if p['id'] == pkg_id), None)
    if not pkg:
        return jsonify({'error': 'Package not found'}), 404

    state = _load_packages_state()
    state[pkg_id] = {
        'installed': True,
        'installed_at': datetime.now().isoformat(),
    }
    _save_packages_state(state)

    return jsonify({'ok': True, 'install_endpoint': pkg['install_endpoint']})


@app.route('/api/apps/set-installed', methods=['POST'])
@require_auth
def apps_set_installed():
    """Enable or disable a simple (no-deps) package."""
    if getattr(g, 'role', None) != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    data = request.get_json() or {}
    pkg_id = data.get('id', '').strip()
    installed = bool(data.get('installed', True))

    pkg = next((p for p in _ETHOS_PACKAGES if p['id'] == pkg_id), None)
    if not pkg:
        return jsonify({'error': f'Package {pkg_id!r} not found'}), 404
    if not pkg.get('simple'):
        return jsonify({'error': f'Package {pkg_id!r} is not a simple package'}), 400

    state = _load_packages_state()
    state[pkg_id] = {
        'installed': installed,
        'installed_at': datetime.utcnow().isoformat() if installed else '',
    }
    _save_packages_state(state)

    elog('packages', 'info',
         f'App {"enabled" if installed else "disabled"}: {pkg.get("name", pkg_id)}')
    return jsonify({'ok': True, 'id': pkg_id, 'installed': installed})


@app.route('/api/ethos-packages/<pkg_id>/uninstall', methods=['POST'])
@require_auth
def uninstall_ethos_package(pkg_id):
    """Uninstall a EthOS package — call its cleanup and mark as removed."""
    if getattr(g, 'role', None) != 'admin':
        return jsonify({'error': t('auth.no_permission')}), 403

    pkg = next((p for p in _ETHOS_PACKAGES if p['id'] == pkg_id), None)
    if not pkg:
        return jsonify({'error': 'Package not found'}), 404

    body = request.json or {}
    wipe = body.get('wipe_data', False)
    wipe_models = body.get('wipe_models', False)

    # Call the package's own uninstall endpoint (generic dispatch)
    uninstall_ep = pkg.get('uninstall_endpoint', '')
    if uninstall_ep:
        try:
            with app.test_client() as tc:
                headers = {}
                auth_hdr = request.headers.get('Authorization')
                if auth_hdr:
                    headers['Authorization'] = auth_hdr
                tc.post(uninstall_ep, json={'wipe_data': wipe, 'wipe_models': wipe_models}, headers=headers)
        except Exception as _e:
            log.warning('[packages] uninstall endpoint %s failed: %s', uninstall_ep, _e)

    state = _load_packages_state()
    state[pkg_id] = {
        'installed': False,
        'installed_at': '',
    }
    _save_packages_state(state)

    return jsonify({'ok': True})


# ─────────────────────────── Desktop Layout Prefs ───────────────────────────

DESKTOP_APPS_FILE = _data_path('desktop_apps.json')

def _load_desktop_apps(username='admin'):
    """Load per-user desktop app IDs. Returns list or None (=default)."""
    if os.path.isfile(DESKTOP_APPS_FILE):
        try:
            with open(DESKTOP_APPS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data.get(username)
            if isinstance(data, list):
                return data  # legacy: single list
        except Exception:
            pass
    return None

def _save_desktop_apps(app_ids, username='admin'):
    data = {}
    if os.path.isfile(DESKTOP_APPS_FILE):
        try:
            with open(DESKTOP_APPS_FILE) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
    data[username] = app_ids
    with open(DESKTOP_APPS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

@app.route('/api/desktop-apps')
@require_auth
def get_desktop_apps():
    user = get_current_user()
    uname = user['username'] if user else 'admin'
    ids = _load_desktop_apps(uname)
    return jsonify({'app_ids': ids})

@app.route('/api/desktop-apps', methods=['PUT'])
@require_auth
def set_desktop_apps():
    data = request.get_json(force=True)
    ids = data.get('app_ids', [])
    if not isinstance(ids, list):
        return jsonify({'error': 'app_ids must be a list'}), 400
    user = get_current_user()
    uname = user['username'] if user else 'admin'
    _save_desktop_apps(ids, uname)
    return jsonify({'ok': True})


# ─────────────────────────── Notifications (per-user) ────────────────
_DISMISSED_NOTIFS_GLOBAL = _data_path('dismissed_notifications.json')  # legacy
_PERSISTENT_NOTIFS_FILE = _data_path('persistent_notifications.json')
_MAX_PERSISTENT = 50

def _dismissed_file():
    cur = get_current_user()
    username = cur['username'] if cur else None
    if username:
        return _user_data_path('dismissed_notifications.json', username)
    return _DISMISSED_NOTIFS_GLOBAL

def _load_dismissed():
    return _load_json(_dismissed_file(), {})

def _save_dismissed(data):
    _save_json(_dismissed_file(), data)

def _notif_key(n):
    """Generate a stable key for a notification to track dismissals."""
    return n.get('title', '') + '::' + n.get('message', '')


def add_persistent_notification(title, message, ntype='info', action=None):
    """Store a notification that persists until the user clears it."""
    notifs = _load_json(_PERSISTENT_NOTIFS_FILE, [])
    entry = {
        'type': ntype,
        'title': title,
        'message': message,
        'time': time.time(),
    }
    if action:
        entry['action'] = action
    notifs.insert(0, entry)
    notifs = notifs[:_MAX_PERSISTENT]
    _save_json(_PERSISTENT_NOTIFS_FILE, notifs)

def _load_persistent_notifications():
    return _load_json(_PERSISTENT_NOTIFS_FILE, [])

def _clear_persistent_notifications():
    _save_json(_PERSISTENT_NOTIFS_FILE, [])




@app.route('/api/notifications')
@require_auth
def get_notifications():
    notifications = []

    # 1) Persistent notifications (stored by events — stay until cleared)
    try:
        notifications.extend(_load_persistent_notifications())
    except Exception:
        pass

    # 2) Live system warnings (ephemeral — recomputed each call)
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            if usage.percent > 90:
                notifications.append({
                    'type': 'warning',
                    'title': 'Low disk space',
                    'message': f'{part.mountpoint} — {usage.percent}% used',
                    'time': time.time()
                })
        except (PermissionError, OSError):
            pass

    mem = psutil.virtual_memory()
    if mem.percent > 90:
        notifications.append({
            'type': 'warning',
            'title': 'High RAM usage',
            'message': f'{mem.percent}% used',
            'time': time.time()
        })

    cpu = psutil.cpu_percent(interval=0.3)
    if cpu > 90:
        notifications.append({
            'type': 'warning',
            'title': 'High CPU load',
            'message': f'{cpu}%',
            'time': time.time()
        })

    # 3) Live in-progress tasks (backup, file ops, etc.)
    try:
        from blueprints.backup import get_backup_notifications
        notifications.extend(get_backup_notifications())
    except Exception:
        pass

    try:
        notifications.extend(get_fileop_notifications())
    except Exception:
        pass

    # Duplicate scan notifications
    try:
        notifications.extend(get_dupscan_notifications())
    except Exception:
        pass

    # USB hotplug notifications
    try:
        notifications.extend(get_usb_notifications())
    except Exception:
        pass

    # Filter out dismissed notifications
    dismissed = _load_dismissed()
    if dismissed:
        notifications = [n for n in notifications if _notif_key(n) not in dismissed]

    return jsonify(notifications)


@app.route('/api/notifications/clear', methods=['POST'])
@require_auth
def clear_notifications():
    """Clear all persistent and dismiss all ephemeral notifications."""
    _clear_persistent_notifications()
    # Also dismiss ephemeral (system warnings) so they don't reappear immediately
    all_notifs = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            if usage.percent > 90:
                all_notifs.append({'title': 'Low disk space',
                                   'message': f'{part.mountpoint} — {usage.percent}% used'})
        except Exception:
            pass
    mem = psutil.virtual_memory()
    if mem.percent > 90:
        all_notifs.append({'title': 'High RAM usage', 'message': f'{mem.percent}% used'})
    cpu = psutil.cpu_percent(interval=0.1)
    if cpu > 90:
        all_notifs.append({'title': 'High CPU load', 'message': f'{cpu}%'})

    dismissed = {}
    for n in all_notifs:
        key = _notif_key(n)
        dismissed[key] = time.time()
    _save_dismissed(dismissed)
    return jsonify({'ok': True})


# ─────────────────────────── Static ───────────────────────────

_INDEX_CACHE = {'html': None, 'mtime': 0, 'apps_files': None}
_APP_SCRIPT_RE = re.compile(r'\s*<script src="js/apps/([^?"]+)(?:\?[^"]*)?"[^>]*></script>')


def _get_index_html():
    """Return index.html with script tags filtered to only existing app JS files.

    On dev machines all JS files are present so nothing is stripped.
    On built images optional app JS is removed; the corresponding script
    tags are silently dropped so the browser never sees a 404.
    After installing an app via Package Center, its JS file appears on disk
    and the next page load will include it.
    """
    index_path = os.path.join(app.static_folder, 'index.html')
    apps_dir = os.path.join(app.static_folder, 'js', 'apps')

    try:
        idx_mtime = os.path.getmtime(index_path)
    except OSError:
        return send_from_directory(app.static_folder, 'index.html')

    try:
        existing = frozenset(f for f in os.listdir(apps_dir) if f.endswith('.js'))
    except OSError:
        existing = frozenset()

    if (_INDEX_CACHE['html']
            and _INDEX_CACHE['mtime'] == idx_mtime
            and _INDEX_CACHE['apps_files'] == existing):
        return _INDEX_CACHE['html']

    with open(index_path) as f:
        html = f.read()

    def _keep_if_exists(m):
        filename = m.group(1)
        return m.group(0) if filename in existing else ''

    html = _APP_SCRIPT_RE.sub(_keep_if_exists, html)

    _INDEX_CACHE['html'] = html
    _INDEX_CACHE['mtime'] = idx_mtime
    _INDEX_CACHE['apps_files'] = existing
    return html


def _serve_index_response():
    """Build an HTTP response for index.html."""
    html = _get_index_html()
    if isinstance(html, str):
        resp = make_response(html)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp
    return html


@app.route('/')
def serve_index():
    return _serve_index_response()


@app.route('/<path:path>')
def serve_static(path):
    # PWA files must be served from root scope
    if path in ['manifest.json', 'manifest-music.json', 'sw.js', 'offline.html']:
        resp = send_from_directory(app.static_folder, path)
        if path.endswith('manifest.json') or path.endswith('manifest-music.json'):
            resp.headers['Content-Type'] = 'application/manifest+json'
        return resp

    if os.path.isfile(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)

    # Static assets that don't exist → 404 (not index.html fallback)
    _ext = os.path.splitext(path)[1].lower()
    if _ext in ('.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
                '.woff', '.woff2', '.ttf', '.eot', '.map', '.json', '.webp'):
        return '', 404

    # SPA fallback: return index.html for navigation routes
    return _serve_index_response()


# ─────────────────────────── WebSocket ───────────────────────────

prev_net = {'sent': 0, 'recv': 0, 'time': time.time()}


def background_stats():
    global prev_net
    while True:
        try:
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            net = psutil.net_io_counters()
            now = time.time()
            dt = now - prev_net['time'] if prev_net['time'] else 1

            net_speed_up = (net.bytes_sent - prev_net['sent']) / dt if prev_net['sent'] else 0
            net_speed_down = (net.bytes_recv - prev_net['recv']) / dt if prev_net['recv'] else 0
            prev_net = {'sent': net.bytes_sent, 'recv': net.bytes_recv, 'time': now}

            socketio.emit('system_stats', {
                'cpu': cpu,
                'memory_percent': mem.percent,
                'memory_used': mem.used,
                'memory_total': mem.total,
                'net_up': net_speed_up,
                'net_down': net_speed_down,
                'time': now
            })
        except Exception:
            pass
        socketio.sleep(3)


# ─────────────────────────── Terminal (PTY over WebSocket) ───────────────────

terminal_sessions = {}  # sid -> { 'fd': master_fd, 'pid': child_pid, 'greenlet': g }


@app.route('/api/terminal/users')
@require_auth
def terminal_users():
    """List system users that can run a shell (from the host)."""
    try:
        # Check current user for filtering
        current_user = g.username
        role = g.role

        r = _host_run_base(
            "getent passwd | awk -F: '$7 ~ /bash|zsh|sh/ && $3 >= 0 {print $1 \":\" $6 \":\" $7}'",
            timeout=10
        )
        users = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split(':')
            if len(parts) >= 3:
                username = parts[0]
                # Non-admin users can only see themselves
                if role != 'admin' and username != current_user:
                    continue

                users.append({
                    'username': username,
                    'home': parts[1],
                    'shell': parts[2]
                })
        return jsonify(users)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _pty_reader(sid, master_fd):
    """Greenlet that reads PTY output and sends to the client."""
    # Make fd non-blocking for gevent compatibility
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        while sid in terminal_sessions:
            try:
                r, _, _ = gevent.select.select([master_fd], [], [], timeout=5.0)
            except (OSError, IOError, ValueError):
                break
            if not r:
                continue
            try:
                data = os.read(master_fd, 16384)
                if not data:
                    break
                socketio.emit('terminal_output',
                              {'data': data.decode('utf-8', errors='replace')},
                              to=sid)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    gevent.sleep(0.005)
                    continue
                break
    except Exception:
        pass
    finally:
        socketio.emit('terminal_output', {'data': '\r\n\x1b[31m[Session ended]\x1b[0m\r\n'}, to=sid)
        _cleanup_terminal(sid)


def _cleanup_terminal(sid):
    """Clean up a terminal session."""
    session = terminal_sessions.pop(sid, None)
    if session:
        try:
            os.close(session['fd'])
        except OSError:
            pass
        try:
            os.kill(session['pid'], signal.SIGTERM)
            os.waitpid(session['pid'], os.WNOHANG)
        except (OSError, ChildProcessError):
            pass


def _ws_get_current_user():
    """Authenticate a WebSocket request using cookie or query token."""
    from flask import request as _req
    token = _req.args.get('token', '') or _req.cookies.get('nas_token', '')
    info = tokens.get(token)
    if info and info['expires'] > datetime.now():
        return {'username': info['username'], 'role': info['role']}
    return None


@socketio.on('terminal_open')
def handle_terminal_open(data):
    """Open a new PTY session on the host as the requested user."""
    user = _ws_get_current_user()
    if not user:
        emit('terminal_output', {'data': '\r\n\x1b[31m[Unauthorized — please log in again]\x1b[0m\r\n'})
        return

    sid = request.sid
    # Clean up any existing session for this client
    _cleanup_terminal(sid)

    # Non-admin users can only open terminal as themselves
    requested = data.get('username', user['username'])
    if user['role'] != 'admin' and requested != user['username']:
        requested = user['username']
    # Never allow direct root shell — use su - within terminal instead
    if requested == 'root':
        requested = user['username']
    username = requested
    cols = data.get('cols', 80)
    rows = data.get('rows', 24)

    # Spawn PTY — in native mode run su directly, in Docker via nsenter
    master_fd, slave_fd = pty.openpty()

    pid = os.fork()
    if pid == 0:
        # Child process
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.environ['TERM'] = 'xterm-256color'
        os.environ['COLORTERM'] = 'truecolor'
        ns_args = nsenter_args()
        if ns_args:
            os.execvp('nsenter', ns_args + ['--', 'su', '-', username])
        else:
            os.execvp('su', ['su', '-', username])
        os._exit(1)
    else:
        # Parent process
        os.close(slave_fd)
        # Set initial terminal size
        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        terminal_sessions[sid] = {
            'fd': master_fd,
            'pid': pid,
        }

        # Start background reader greenlet
        terminal_sessions[sid]['greenlet'] = socketio.start_background_task(
            _pty_reader, sid, master_fd
        )
        emit('terminal_ready', {'username': username})


@socketio.on('terminal_input')
def handle_terminal_input(data):
    """Send input data to the PTY."""
    if not _ws_get_current_user():
        return
    sid = request.sid
    session = terminal_sessions.get(sid)
    if session:
        try:
            os.write(session['fd'], data['data'].encode('utf-8'))
        except (OSError, IOError):
            _cleanup_terminal(sid)


@socketio.on('terminal_resize')
def handle_terminal_resize(data):
    """Resize the PTY."""
    if not _ws_get_current_user():
        return
    sid = request.sid
    session = terminal_sessions.get(sid)
    if session:
        try:
            winsize = struct.pack('HHHH', data.get('rows', 24), data.get('cols', 80), 0, 0)
            fcntl.ioctl(session['fd'], termios.TIOCSWINSZ, winsize)
        except (OSError, IOError):
            pass


@socketio.on('terminal_close')
def handle_terminal_close():
    """Close the terminal session."""
    _cleanup_terminal(request.sid)


@socketio.on('disconnect')
def handle_disconnect():
    """Clean up terminal on socket disconnect."""
    _cleanup_terminal(request.sid)


# ─────────────────────────── Event-loop watchdog ────────────

import time as _time  # real (non-monkey-patched) time module for watchdog
_watchdog_last_tick = [0.0]  # mutable — updated by the event-loop greenlet

def _watchdog_ticker():
    """Lightweight greenlet that updates a timestamp every 5 s.
    If this stops advancing, the event loop is blocked."""
    while True:
        _watchdog_last_tick[0] = _time.monotonic()
        gevent.sleep(5)

def _watchdog_monitor():
    """Runs in a REAL OS thread (not gevent).  Checks if the event-loop
    ticker has advanced recently.  If it hasn't moved for >30 s a warning
    is logged; if it exceeds 90 s the process is killed so systemd can
    restart it cleanly."""
    import signal as _signal
    WARN_LIMIT = 30   # seconds — log a warning
    STALL_LIMIT = 90  # seconds — hard kill
    _warned = [False]
    while True:
        _time.sleep(10)  # real OS sleep, not gevent
        last = _watchdog_last_tick[0]
        if last == 0.0:
            continue  # ticker hasn't started yet
        stall = _time.monotonic() - last
        if stall > STALL_LIMIT:
            msg = f'[watchdog] Event loop blocked for {stall:.0f}s — forcing restart!'
            try:
                print(msg, flush=True)
                elog('system', 'error', msg)
            except Exception:
                pass
            os._exit(1)  # hard exit — systemd will restart
        elif stall > WARN_LIMIT and not _warned[0]:
            _warned[0] = True
            try:
                print(f'[watchdog] Event loop nie odpowiada od {stall:.0f}s', flush=True)
            except Exception:
                pass
        elif stall <= WARN_LIMIT:
            _warned[0] = False


# ─────────────────────────── Main ───────────────────────────


def _mark_boot_success():
    """Mark boot as successful in GRUB environment (A/B boot counter).

    Called after EthOS starts successfully. Sets boot_success=1 in grubenv
    so GRUB won't increment the failure counter on next reboot.
    Also detects current A/B slot from kernel cmdline.
    """
    try:
        # Detect current slot from kernel cmdline
        slot = 'a'
        try:
            with open('/proc/cmdline') as f:
                cmdline = f.read()
            for param in cmdline.split():
                if param.startswith('ethos.slot='):
                    slot = param.split('=', 1)[1].strip()
                    break
        except Exception:
            pass

        # Try all known grubenv locations — $prefix varies by UEFI firmware
        for grubenv in (
            '/boot/efi/EFI/BOOT/grubenv',
            '/boot/efi/EFI/debian/grubenv',
            '/boot/efi/boot/grub/grubenv',
            '/boot/grub/grubenv',
        ):
            if os.path.exists(grubenv):
                os.system(f'grub-editenv {grubenv} set boot_success=1')
                logging.getLogger('boot').info(
                    'Marked boot_success=1 in %s (slot=%s)', grubenv, slot)

        # Store slot info for updater/UI access
        _slot_file = os.path.join(ETHOS_ROOT, 'data', 'active_slot')
        try:
            with open(_slot_file, 'w') as f:
                f.write(slot)
        except Exception:
            pass

        # Sync ab_slots.json "active" field with actual boot slot
        _ab_file = os.path.join(ETHOS_ROOT, 'data', 'ab_slots.json')
        try:
            if os.path.isfile(_ab_file):
                with open(_ab_file) as f:
                    _ab = json.load(f)
                if _ab.get('active') != slot:
                    _ab['active'] = slot
                    with open(_ab_file, 'w') as f:
                        json.dump(_ab, f, indent=2)
                    logging.getLogger('boot').info(
                        'Updated ab_slots.json active=%s', slot)
        except Exception:
            pass

    except Exception as e:
        logging.getLogger('boot').warning('Failed to mark boot success: %s', e)


@app.route('/api/cache-test')
@cache.cached(timeout=60)
def cache_test():
    return jsonify({'time': time.time()})

# Notify systemd when we are ready (delayed slightly to allow socket bind)
if os.environ.get('NOTIFY_SOCKET'):
    gevent.spawn_later(0.1, systemd_notify_ready)

if __name__ == '__main__':
    # Initialize databases
    init_resources_db()
    init_backup(socketio)
    init_eventlog(socketio)
    init_notifications(socketio)

    # SIGTERM handler — log shutdown before dying (e.g. systemd restart)
    def _sigterm_handler(signum, frame):
        try:
            elog('system', 'warning', 'EthOS zatrzymany (SIGTERM/shutdown)',
                 details={'source': 'signal', 'pid': os.getpid()})
        except Exception:
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Start background tasks
    socketio.start_background_task(background_stats)
    socketio.start_background_task(resources_background_collector, socketio)
    socketio.start_background_task(usb_monitor_loop, socketio)
    socketio.start_background_task(keepalive_loop, socketio)
    socketio.start_background_task(update_auto_check_loop)
    _start_trash_scheduler()
    start_ddns()
    _register_avahi_service()

    # Periodic cleanup: expired tokens, stale unlocked folders, stale temp downloads
    def _janitor_loop():
        import gevent as _gv
        while True:
            _gv.sleep(3600)  # every hour
            now = datetime.now()
            # Expired tokens + idle sessions
            tokens.prune_expired()
            tokens.prune_idle(SESSION_IDLE_TIMEOUT)
            # Stale unlocked_folders entries for expired tokens
            with _uf_lock:
                stale_uf = [t for t in _unlocked_folders if t not in tokens]
                for t in stale_uf:
                    _unlocked_folders.pop(t, None)
            # Stale login attempts (older than 2x window)
            cutoff = time.time() - _ATTEMPT_WINDOW * 2
            with _login_lock:
                stale_la = [ip for ip, a in _login_attempts.items() if a.get('first', 0) < cutoff]
                for ip in stale_la:
                    _login_attempts.pop(ip, None)
                # Stale per-user lockouts
                stale_ua = [u for u, a in _user_login_attempts.items()
                            if a.get('first', 0) < cutoff and time.time() > a.get('locked_until', 0)]
                for u in stale_ua:
                    _user_login_attempts.pop(u, None)
            # Stale pending downloads (temp ZIP files older than 1 hour)
            now_ts = time.time()
            stale_dl = [did for did, info in _pending_downloads.items()
                        if now_ts - info.get('time', 0) > 3600]
            for did in stale_dl:
                info = _pending_downloads.pop(did, None)
                if info:
                    try:
                        os.remove(info['path'])
                    except OSError:
                        pass
            # Stale upload sessions (older than session TTL)
            _upload_sessions_prune()
    socketio.start_background_task(_janitor_loop)

    # ── Resource alerts — check thresholds every 60 seconds ──
    _ALERT_THRESHOLDS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'alert_thresholds.json')
    _alert_cooldowns = {}  # metric_key -> last_alert_ts

    def _resource_alert_loop():
        import gevent as _gv
        import psutil
        _gv.sleep(60)  # initial delay
        while True:
            try:
                thresholds = {'cpu': 90, 'ram': 90, 'disk': 90, 'temp': 80, 'enabled': True}
                if os.path.exists(_ALERT_THRESHOLDS_FILE):
                    with open(_ALERT_THRESHOLDS_FILE, 'r') as f:
                        thresholds.update(json.load(f))

                if not thresholds.get('enabled', True):
                    _gv.sleep(60)
                    continue

                now = time.time()
                cooldown = 3600  # 1 hour between alerts per metric
                alerts = []

                # CPU
                cpu_pct = psutil.cpu_percent(interval=1)
                if cpu_pct >= thresholds.get('cpu', 90):
                    if now - _alert_cooldowns.get('cpu', 0) > cooldown:
                        alerts.append(('cpu', f'CPU: {cpu_pct:.0f}% (próg: {thresholds["cpu"]}%)'))
                        _alert_cooldowns['cpu'] = now

                # RAM
                mem = psutil.virtual_memory()
                if mem.percent >= thresholds.get('ram', 90):
                    if now - _alert_cooldowns.get('ram', 0) > cooldown:
                        alerts.append(('ram', f'RAM: {mem.percent:.0f}% (próg: {thresholds["ram"]}%)'))
                        _alert_cooldowns['ram'] = now

                # Disk usage
                for part in psutil.disk_partitions():
                    if part.mountpoint in ('/', '/mnt/data') or part.mountpoint.startswith('/mnt/pool'):
                        try:
                            usage = psutil.disk_usage(part.mountpoint)
                            if usage.percent >= thresholds.get('disk', 90):
                                key = f'disk:{part.mountpoint}'
                                if now - _alert_cooldowns.get(key, 0) > cooldown:
                                    alerts.append((key, f'Dysk {part.mountpoint}: {usage.percent:.0f}% (próg: {thresholds["disk"]}%)'))
                                    _alert_cooldowns[key] = now
                        except OSError:
                            pass

                # Temperature
                temps = psutil.sensors_temperatures() if hasattr(psutil, 'sensors_temperatures') else {}
                for name, entries in temps.items():
                    for entry in entries:
                        if entry.current and entry.current >= thresholds.get('temp', 80):
                            key = f'temp:{name}'
                            if now - _alert_cooldowns.get(key, 0) > cooldown:
                                alerts.append((key, f'Temperatura {name}: {entry.current:.0f}°C (próg: {thresholds["temp"]}°C)'))
                                _alert_cooldowns[key] = now
                            break

                # Fire alerts
                for key, msg in alerts:
                    try:
                        from blueprints.notifications import send_notification, push_inbox
                        push_inbox('Alarm zasobów', msg, msg_type='warning', category='system')
                        send_notification('EthOS: Alarm zasobów', msg, 'storage', 'warning')
                        elog('system', 'warning', msg)
                    except Exception:
                        pass

            except Exception:
                pass
            _gv.sleep(60)

    socketio.start_background_task(_resource_alert_loop)

    # ── Service watchdog — monitor critical services ──
    _WATCHDOG_RESTART_COUNT = {}  # service -> count
    _WATCHDOG_MAX_RESTARTS = 3

    def _service_watchdog_loop():
        import gevent as _gv
        _gv.sleep(120)  # wait for system to fully start
        services = ['ethos']  # core service always monitored
        while True:
            try:
                # Check which services are installed
                for svc in ['nginx', 'smbd', 'docker']:
                    r = _host_run_base(f'systemctl list-unit-files {svc}.service 2>/dev/null | grep -c {svc}', timeout=5)
                    if r.returncode == 0 and r.stdout.strip() != '0' and svc not in services:
                        services.append(svc)

                for svc in services:
                    if svc == 'ethos':
                        continue  # don't restart ourselves
                    r = _host_run_base(f'systemctl is-active {svc} 2>/dev/null', timeout=5)
                    is_active = r.stdout.strip() == 'active'
                    if not is_active:
                        count = _WATCHDOG_RESTART_COUNT.get(svc, 0)
                        if count < _WATCHDOG_MAX_RESTARTS:
                            _host_run_base(f'systemctl restart {svc}', timeout=30)
                            _WATCHDOG_RESTART_COUNT[svc] = count + 1
                            elog('system', 'warning', f'Watchdog: usługa {svc} zrestartowana (próba {count + 1}/{_WATCHDOG_MAX_RESTARTS})')
                            try:
                                from blueprints.notifications import push_inbox
                                push_inbox(f'Watchdog: {svc}', f'Usługa {svc} była nieaktywna i została zrestartowana.',
                                           msg_type='warning', category='system')
                            except Exception:
                                pass
                        elif count == _WATCHDOG_MAX_RESTARTS:
                            _WATCHDOG_RESTART_COUNT[svc] = count + 1  # prevent repeated alerts
                            msg = f'Watchdog: usługa {svc} nie odpowiada po {_WATCHDOG_MAX_RESTARTS} próbach restartu!'
                            elog('system', 'error', msg)
                            try:
                                from blueprints.notifications import send_notification, push_inbox
                                push_inbox(f'Watchdog: {svc} AWARIA', msg, msg_type='error', category='system')
                                send_notification('EthOS: Awaria usługi', msg, 'docker', 'error')
                            except Exception:
                                pass
                    else:
                        # Service is healthy — reset restart counter
                        _WATCHDOG_RESTART_COUNT.pop(svc, None)

            except Exception:
                pass
            _gv.sleep(30)

    socketio.start_background_task(_service_watchdog_loop)

    # Resume interrupted NasLink transfer (if any)
    gevent.spawn_later(5, _resume_interrupted_transfer)
    # Resume interrupted ZIP download (if any)
    gevent.spawn_later(6, _resume_interrupted_zip)
    # Resume interrupted compress (if any)
    gevent.spawn_later(7, _resume_interrupted_compress)
    # Resume interrupted copy (if any)
    gevent.spawn_later(8, _resume_interrupted_copy)
    # Resume interrupted move (if any)
    gevent.spawn_later(9, _resume_interrupted_move)
    # Restore persisted chunked upload sessions so clients can resume after restart
    gevent.spawn_later(10, _restore_upload_sessions)
    # Clean up leftover partial files from previous crash/abort
    gevent.spawn_later(11, _cleanup_stale_ethos_tmp)
    # Pre-warm listing cache for all user home directories (background, low priority)
    gevent.spawn_later(12, _bg_prewarm_home_listing)

    # VM autostart — start VMs flagged with autostart=True
    def _vm_autostart():
        try:
            from blueprints.vm_manager import vm_autostart_boot
            vm_autostart_boot(flask_app=app)
        except ImportError:
            pass  # VM Manager not installed
        except Exception as e:
            logging.getLogger('vm_autostart').warning('[vm] Autostart error: %s', e)
    gevent.spawn_later(15, _vm_autostart)

    # Start event-loop watchdog (ticker in gevent, monitor in real thread)
    socketio.start_background_task(_watchdog_ticker)
    _wd_thread = _threading.Thread(target=_watchdog_monitor, daemon=True)
    _wd_thread.start()

    # Mark boot as successful for A/B boot counter (after all init is done)
    gevent.spawn_later(20, _mark_boot_success)

    # Resume interrupted Photos AI scan if any (wait for full init)
    try:
        from blueprints.photos_ai import resume_interrupted_scan
        gevent.spawn_later(30, resume_interrupted_scan)
    except Exception:
        pass

    # ── SSL configuration ──
    ssl_enabled = os.environ.get('SSL_ENABLED', '0') == '1'
    ssl_cert = os.environ.get('SSL_CERT', '')
    ssl_key = os.environ.get('SSL_KEY', '')
    https_port = int(os.environ.get('HTTPS_PORT', '443'))
    ssl_redirect = os.environ.get('SSL_REDIRECT', '0') == '1'

    run_kwargs = dict(host='0.0.0.0', debug=False, allow_unsafe_werkzeug=False)

    if ssl_enabled and ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        run_kwargs['port'] = https_port
        run_kwargs['certfile'] = ssl_cert
        run_kwargs['keyfile'] = ssl_key
        print(f'\n  EthOS running on https://0.0.0.0:{https_port}  (SSL enabled)\n')

        if ssl_redirect:
            # Start a simple HTTP->HTTPS redirect on the original port
            import threading
            from werkzeug.serving import WSGIRequestHandler

            def _http_redirect_app(environ, start_response):
                host = environ.get('HTTP_HOST', '').split(':')[0]
                path = environ.get('PATH_INFO', '/')
                qs = environ.get('QUERY_STRING', '')
                url = f'https://{host}:{https_port}{path}'
                if qs:
                    url += '?' + qs
                start_response('301 Moved Permanently', [('Location', url), ('Content-Length', '0')])
                return [b'']

            def _run_redirect():
                from werkzeug.serving import make_server
                try:
                    srv = make_server('0.0.0.0', PORT, _http_redirect_app)
                    print(f'  HTTP→HTTPS redirect on port {PORT}')
                    srv.serve_forever()
                except Exception as e:
                    print(f'  [warn] Could not start HTTP redirect: {e}')

            t = threading.Thread(target=_run_redirect, daemon=True)
            t.start()
    else:
        run_kwargs['port'] = PORT
        print(f'\n  EthOS running on http://0.0.0.0:{PORT}\n')

    socketio.run(app, **run_kwargs)
