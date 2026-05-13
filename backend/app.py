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
import threading as _threading
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
from blueprints.users import users_bp, _load_privileges
from blueprints.network import network_bp
from blueprints.eventlog import eventlog_bp, init_eventlog, log as elog
from blueprints.auth import auth_bp, security_bp, require_auth, get_current_user, get_token, generate_token, tokens, SESSION_IDLE_TIMEOUT
from blueprints.system_bp import system_bp, _register_avahi_service
from blueprints.file_manager import files_bp
from blueprints.file_manager_mobile import _start_trash_scheduler
from blueprints.file_manager_archive import _resume_interrupted_transfer
from blueprints.file_manager_ops import (
    _resume_interrupted_copy, _resume_interrupted_compress, _resume_interrupted_zip,
    _resume_interrupted_move, _cleanup_stale_ethos_tmp, _bg_prewarm_home_listing
)
from blueprints.file_manager_photos import _restore_upload_sessions
from audit import audit_log
from blueprints.sandbox_policy import sandbox_bp
from blueprints.updater import update_bp, updates_public_bp, init_update
from blueprints.updater_apply import update_auto_check_loop
from blueprints.ddns import ddns_bp, start_ddns
from blueprints.settings import settings_bp
from blueprints.ssh_manager import ssh_bp
from blueprints.installer import installer_bp
try:
    from blueprints.ups import _ups_status
except ImportError:
    _ups_status = {}
from blueprints.power import power_bp
from blueprints.encryption import encryption_bp
from blueprints.ssd_cache import ssd_cache_bp
from blueprints.hardware import hardware_bp
from blueprints.notifications import notifications_bp, init_notifications
from blueprints.dashboard import dashboard_bp
from blueprints.admin_required import admin_required
from blueprints.totp import totp_bp, is_totp_enabled, verify_totp_code, verify_backup_code
from blueprints.api_docs import api_docs_bp
from blueprints.security_advisor import security_advisor_bp
from blueprints.firewall import firewall_bp
from blueprints.fail2ban import fail2ban_bp
from blueprints.app_manager import (
    app_manager_bp, init_app_manager, migrate_from_ethos_packages,
    BUILTIN_CATALOG as _BUILTIN_CATALOG,
    load_installed as _load_app_manager_installed,
    load_optional_blueprints as _load_optional_blueprints,
    OPTIONAL_BLUEPRINTS as _OPTIONAL_BLUEPRINTS,
)
from blueprints.tickets import tickets_bp, init_tickets

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
    # Guard against None response
    if response is None:
        return response
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

    # Boot beacon — VM has no session token or CSRF cookie
    if request.path == '/api/builder/beacon' and request.method == 'POST':
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
    # Guard against None response
    if response is None:
        return response
    
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

    # Exception for noVNC console iframe (VM Manager)
    if request.path.startswith('/api/vm/novnc/'):
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
        "frame-src 'self' https://www.gstatic.com https://*.google.com; "
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


# ── Auto-log API errors to Event Log ──────────────────────────────
#
# Maps /api/<prefix>/... to an eventlog category.  Anything not
# explicitly listed falls back to 'system'.
_API_CATEGORY_MAP = {
    'files': 'files', 'storage': 'storage', 'backup': 'backup',
    'cloud-backup': 'backup', 'docker': 'docker', 'network': 'network',
    'printer': 'printer', 'security': 'security', 'users': 'security',
    'radio-music': 'system', 'video-station': 'system',
    'gallery': 'system', 'photos-ai': 'system',
}

def _api_category():
    """Derive eventlog category from the request path."""
    p = request.path
    if not p.startswith('/api/'):
        return 'system'
    parts = p.split('/')       # ['', 'api', 'storage', ...]
    prefix = parts[2] if len(parts) > 2 else ''
    return _API_CATEGORY_MAP.get(prefix, 'system')

# Noisy endpoints that produce harmless 4xx — skip logging them
_SKIP_LOG_PREFIXES = ('/api/stats', '/api/eventlog', '/api/notifications')

@app.after_request
def _log_api_errors(response):
    """Auto-log 4xx/5xx API responses to Event Log (except 401/404 on non-API)."""
    if response is None:
        return response
    try:
        if not request.path.startswith('/api/'):
            return response
        code = response.status_code
        if code < 400:
            return response
        # Skip auth challenges and harmless misses
        if code in (401, 403, 404, 405):
            return response
        # Skip noisy endpoints
        if any(request.path.startswith(p) for p in _SKIP_LOG_PREFIXES):
            return response
        # Extract error message from JSON body if possible
        err_msg = ''
        try:
            body = response.get_json(silent=True)
            if body and isinstance(body, dict):
                err_msg = body.get('error', '')
        except Exception:
            pass

        from blueprints.eventlog import log as elog
        elog(
            _api_category(),
            'error',
            f'{request.method} {request.path} → {code}' + (f': {err_msg}' if err_msg else ''),
            {'status': code, 'method': request.method, 'path': request.path},
        )
    except Exception:
        pass
    return response


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
    import traceback
    tb = traceback.format_exc()
    try:
        from blueprints.eventlog import log as elog
        elog(
            _api_category(),
            'error',
            f'500 {request.method} {request.path}',
            {'traceback': tb[-2000:], 'method': request.method, 'path': request.path},
        )
    except Exception:
        pass
    return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(Exception)
def _handle_unhandled(e):
    import traceback
    tb = traceback.format_exc()
    app.logger.error(f'Unhandled exception on {request.method} {request.path}: {tb}')
    try:
        from blueprints.eventlog import log as elog
        elog(
            _api_category(),
            'error',
            f'Unhandled: {type(e).__name__}: {str(e)[:200]}',
            {'traceback': tb[-2000:], 'method': request.method, 'path': request.path},
        )
    except Exception:
        pass
    return jsonify({'error': 'Internal server error'}), 500


# Register blueprints
app.register_blueprint(storage_bp)
init_storage(socketio)
app.register_blueprint(resources_bp)
app.register_blueprint(backup_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(security_bp)
app.register_blueprint(system_bp)
app.register_blueprint(files_bp)
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
app.register_blueprint(security_advisor_bp)
app.register_blueprint(firewall_bp)
app.register_blueprint(fail2ban_bp)
app.register_blueprint(app_manager_bp)
app.register_blueprint(tickets_bp)
init_app_manager(socketio)
init_tickets(socketio)
_load_optional_blueprints(app, socketio)

# Init VM WebSocket proxy (must run after vm_manager blueprint is registered)
try:
    from blueprints.vm_manager import _init_ws_proxy
    _init_ws_proxy(app)
except ImportError:
    pass
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

def _is_setup_done():
    """Check if initial setup is complete."""
    return os.path.isfile(SETUP_DONE_FILE)


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
    if response is None:
        return response
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
                   request.path == '/api/radio-music/local/stream' or \
                   request.path.startswith('/api/radio-music/archive/file/')
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
        # For all other API requests, disable caching
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


# ─────────────────────────── Apps Registry ───────────────────────────

# Core apps — always visible on the desktop regardless of install state.
# Optional apps are managed entirely via App Store (BUILTIN_CATALOG + installed_apps.json).
_CORE_APP_DEFS = [
    {'id': 'dashboard',        'name': 'Dashboard',         'icon': 'fa-tachometer-alt',   'color': '#3b82f6', 'category': 'System',   'description': 'System overview'},
    {'id': 'file-manager',     'name': 'File Manager',      'icon': 'fa-folder-open',      'color': '#f59e0b', 'category': 'System',   'description': 'Browse and manage files'},
    {'id': 'storage-manager',  'name': 'Storage Manager',   'icon': 'fa-database',         'color': '#10b981', 'category': 'Storage',  'description': 'Disks, RAID, volumes, sharing and diagnostics'},
    {'id': 'backup',           'name': 'Backup',            'icon': 'fa-shield-alt',       'color': '#06b6d4', 'category': 'Storage',  'description': 'Create and restore backups'},
    {'id': 'resource-monitor', 'name': 'Resource Monitor',  'icon': 'fa-chart-area',       'color': '#8b5cf6', 'category': 'System',   'description': 'Detailed monitoring'},
    {'id': 'terminal',         'name': 'Terminal',          'icon': 'fa-terminal',         'color': '#22c55e', 'category': 'System',   'description': 'Command line (SSH-like)'},
    {'id': 'users',            'name': 'Users',             'icon': 'fa-users-cog',        'color': '#ec4899', 'category': 'System',   'description': 'User and group management', 'admin_only': True},
    {'id': 'network',          'name': 'Network',           'icon': 'fa-network-wired',    'color': '#0ea5e9', 'category': 'System',   'description': 'Network interface and WiFi management'},
    {'id': 'event-log',        'name': 'Event Log',         'icon': 'fa-scroll',           'color': '#64748b', 'category': 'System',   'description': 'Logs and operation history'},
    {'id': 'notifications',    'name': 'Notifications',     'icon': 'fa-bell',             'color': '#f59e0b', 'category': 'System',   'description': 'System notification channels'},
    {'id': 'fail2ban',         'name': 'Intrusion Protection', 'icon': 'fa-shield-alt',    'color': '#ef4444', 'category': 'System',   'description': 'Fail2Ban — active bans, whitelist, SSH/Samba/Web protection', 'admin_only': True},
    {'id': 'firewall',         'name': 'Firewall (UFW)',    'icon': 'fa-fire',             'color': '#e05d44', 'category': 'System',   'description': 'Firewall and rules management', 'admin_only': True},
    {'id': 'app-store',        'name': 'App Store',         'icon': 'fa-th',               'color': '#f97316', 'category': 'System',   'description': 'EthOS packages and Docker containers', 'admin_only': True},
    {'id': 'updates',          'name': 'Updates',           'icon': 'fa-cloud-download-alt', 'color': '#8b5cf6', 'category': 'System', 'description': 'Check and install system updates', 'admin_only': True},
    {'id': 'power',            'name': 'Power Management',  'icon': 'fa-power-off',        'color': '#22c55e', 'category': 'System',   'description': 'Schedule, WOL, power saving', 'admin_only': True},
    {'id': 'system-settings',  'name': 'Settings',          'icon': 'fa-sliders-h',        'color': '#64748b', 'category': 'System',   'description': 'NAS name, server port, hostname, timezone, password change', 'admin_only': True},
    {'id': 'ssh-manager',      'name': 'SSH Manager',       'icon': 'fa-key',              'color': '#6366f1', 'category': 'Network',  'description': 'SSH key and trusted host management'},
    {'id': 'security-advisor', 'name': 'Security Advisor',  'icon': 'fa-user-shield',      'color': '#10b981', 'category': 'Security', 'description': 'Security audit and recommendations', 'admin_only': True},
]


@app.route('/api/apps')
@require_auth
def get_apps():
    # Start with core apps (always visible)
    apps = [dict(a, type='builtin') for a in _CORE_APP_DEFS]

    # Add installed optional apps from Package Center (BUILTIN_CATALOG + installed_apps.json)
    _pm_installed = _load_app_manager_installed()
    _core_ids = {a['id'] for a in _CORE_APP_DEFS}
    for _cat_app in _BUILTIN_CATALOG:
        _cid = _cat_app['id']
        if _cid in _core_ids or _cat_app.get('hidden'):
            continue
        if _cid in _pm_installed:
            apps.append({
                'id': _cid,
                'name': _cat_app.get('name', _cid),
                'icon': _cat_app.get('icon', 'fa-puzzle-piece'),
                'color': _cat_app.get('color', '#6b7280'),
                'type': 'builtin',
                'category': _cat_app.get('category', 'Tools'),
                'description': _cat_app.get('description', ''),
                'admin_only': _cat_app.get('admin_only', False),
            })

    # Filter by privileges
    user = get_current_user()
    if user and user['role'] != 'admin':
        allowed = _user_allowed_apps(user['username'], user['role'])
        if allowed is not None:
            apps = [a for a in apps if not a.get('admin_only') and a['id'] in allowed]
        else:
            apps = [a for a in apps if not a.get('admin_only')]

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
    # Warm up cpu_percent so first non-blocking call returns a valid delta
    psutil.cpu_percent(interval=None)
    while True:
        try:
            cpu = psutil.cpu_percent(interval=None)  # non-blocking; uses delta since last call
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
        socketio.sleep(10)  # was 3s — 10s is sufficient for the header bar stats


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
            '/boot/efi/EFI/ubuntu/grubenv',
            '/boot/efi/boot/grub/grubenv',
            '/boot/grub/grubenv',
        ):
            if os.path.exists(grubenv):
                # Reset counter to 0 so consecutive-failure rollback restarts from scratch
                os.system(f'grub-editenv {grubenv} set boot_success=1 boot_counter=0')
                logging.getLogger('boot').info(
                    'Marked boot_success=1 boot_counter=0 in %s (slot=%s)', grubenv, slot)

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
    # DISABLED DUE TO BLOCKING urllib:     socketio.start_background_task(update_auto_check_loop)
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

    def _auto_clean_root():
        """Free space on root partition: apt cache, pip cache, old /tmp files, old logs.
        Returns human-readable string of freed space (e.g. '120 MB') or '' if nothing freed."""
        import shutil as _shutil
        before = _shutil.disk_usage('/').free
        try:
            _host_run_base('apt-get clean -y 2>/dev/null || true', timeout=60)
        except Exception:
            pass
        try:
            _host_run_base('find /var/cache/apt/archives -name "*.deb" -delete 2>/dev/null || true', timeout=30)
        except Exception:
            pass
        try:
            _host_run_base('find /root/.cache/pip /home/*/.cache/pip -maxdepth 0 -exec rm -rf {} + 2>/dev/null || true', timeout=30)
        except Exception:
            pass
        try:
            tmp = '/tmp'
            cutoff = time.time() - 3600
            for name in os.listdir(tmp):
                p = os.path.join(tmp, name)
                try:
                    if os.path.getmtime(p) < cutoff:
                        if os.path.isdir(p):
                            _shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.unlink(p)
                except OSError:
                    pass
        except Exception:
            pass
        try:
            log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
            cutoff = time.time() - 30 * 86400
            for name in os.listdir(log_dir):
                p = os.path.join(log_dir, name)
                try:
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.unlink(p)
                except OSError:
                    pass
        except Exception:
            pass
        after = _shutil.disk_usage('/').free
        freed = after - before
        if freed > 1024 * 1024:
            mb = freed // (1024 * 1024)
            return f'{mb} MB'
        return ''

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
                cpu_pct = psutil.cpu_percent(interval=None)  # non-blocking
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

                # Disk usage — alert and auto-clean root if critically full
                _ROOT_CLEAN_THRESHOLD = 85
                for part in psutil.disk_partitions():
                    if part.mountpoint in ('/', '/mnt/data') or part.mountpoint.startswith('/mnt/pool'):
                        try:
                            usage = psutil.disk_usage(part.mountpoint)
                            if usage.percent >= thresholds.get('disk', 90):
                                key = f'disk:{part.mountpoint}'
                                if now - _alert_cooldowns.get(key, 0) > cooldown:
                                    alerts.append((key, f'Dysk {part.mountpoint}: {usage.percent:.0f}% (próg: {thresholds["disk"]}%)'))
                                    _alert_cooldowns[key] = now
                            # Auto-clean root partition when above threshold
                            if part.mountpoint == '/' and usage.percent >= _ROOT_CLEAN_THRESHOLD:
                                clean_key = 'auto_clean:/'
                                if now - _alert_cooldowns.get(clean_key, 0) > 3600:
                                    _alert_cooldowns[clean_key] = now
                                    freed = _auto_clean_root()
                                    if freed:
                                        try:
                                            from blueprints.notifications import push_inbox
                                            push_inbox('Auto-cleanup', f'Root {usage.percent:.0f}% pełny — zwolniono {freed}', msg_type='info', category='system')
                                            elog('system', 'info', f'Auto-cleanup root: zwolniono {freed}')
                                        except Exception:
                                            pass
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
        _services_last_check = 0  # throttle installed-service discovery
        while True:
            try:
                now = time.time()
                # Discover installed services at most once every 10 minutes
                if now - _services_last_check > 600:
                    _services_last_check = now
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
            _gv.sleep(60)  # was 30s — 1-minute resolution sufficient for watchdog

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

    # Auto-detect self-signed cert from firstboot (data/ssl/ethos.crt)
    if not ssl_cert:
        _auto_cert = os.path.join(ETHOS_ROOT, 'data', 'ssl', 'ethos.crt')
        _auto_key = os.path.join(ETHOS_ROOT, 'data', 'ssl', 'ethos.key')
        if os.path.exists(_auto_cert) and os.path.exists(_auto_key):
            ssl_cert = _auto_cert
            ssl_key = _auto_key

    run_kwargs = dict(host='0.0.0.0', debug=False, allow_unsafe_werkzeug=False)

    if ssl_enabled and ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        # Full HTTPS mode (Let's Encrypt) — HTTPS as primary
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
        # HTTP primary mode
        run_kwargs['port'] = PORT
        print(f'\n  EthOS running on http://0.0.0.0:{PORT}')

        # If self-signed cert exists, also start HTTPS alongside on port 9443
        _https_side_port = int(os.environ.get('HTTPS_SIDE_PORT', '9443'))
        if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
            import ssl as _ssl_mod

            def _run_https_side():
                """Run a secondary HTTPS listener using eventlet/gevent WSGIServer."""
                try:
                    from gevent.pywsgi import WSGIServer
                    ctx = _ssl_mod.SSLContext(_ssl_mod.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(ssl_cert, ssl_key)
                    ctx.minimum_version = _ssl_mod.TLSVersion.TLSv1_2
                    server = WSGIServer(('0.0.0.0', _https_side_port), app, ssl_context=ctx, log=None)
                    print(f'  HTTPS also available on https://0.0.0.0:{_https_side_port}  (self-signed)\n')
                    server.serve_forever()
                except Exception as e:
                    print(f'  [warn] Could not start HTTPS side listener: {e}\n')

            import gevent as _gv
            _gv.spawn(_run_https_side)
        else:
            print()

    socketio.run(app, **run_kwargs)
