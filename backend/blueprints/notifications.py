"""
EthOS — Notification Channels Blueprint
Send alerts via Telegram, Discord, Gotify, Ntfy, SMTP, Webhook.
In-app notification inbox with read/unread state and SocketIO push.

Inbox endpoints:
  GET  /api/notifications           — List inbox notifications (paginated)
  POST /api/notifications/clear     — Clear all inbox notifications
  POST /api/notifications/read      — Mark notification(s) as read
  POST /api/notifications/read-all  — Mark all as read
  POST /api/notifications/subscribe — Accept Web Push subscription (stub)
  GET  /api/notifications/local-smtp — Auto-detect local mail server for SMTP relay
"""

import json
import os
import sqlite3
import time
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from flask import Blueprint, jsonify, request

try:
    import requests as _requests
except ImportError:
    _requests = None

try:
    import gevent
    _HAS_GEVENT = True
except ImportError:
    _HAS_GEVENT = False

notifications_bp = Blueprint('notifications', __name__, url_prefix='/api/notifications')

DATA_DIR = os.environ.get('ETHOS_DATA', '/opt/ethos/data')
CONFIG_PATH = os.path.join(DATA_DIR, 'notifications_config.json')
HISTORY_PATH = os.path.join(DATA_DIR, 'notifications_history.json')
MAX_HISTORY = 200
_INBOX_DB = os.path.join(os.environ.get('ETHOS_LOG_DIR', '/opt/ethos/logs'), 'inbox.db')

_socketio = None


def init_notifications(socketio_instance):
    """Store SocketIO reference and create inbox DB table."""
    global _socketio
    _socketio = socketio_instance
    _init_inbox_db()

DATA_DIR = os.environ.get('ETHOS_DATA', '/opt/ethos/data')
CONFIG_PATH = os.path.join(DATA_DIR, 'notifications_config.json')
HISTORY_PATH = os.path.join(DATA_DIR, 'notifications_history.json')
MAX_HISTORY = 200

# ── Default config ────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "channels": {
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "discord":  {"enabled": False, "webhook_url": ""},
        "gotify":   {"enabled": False, "server_url": "", "app_token": ""},
        "ntfy":     {"enabled": False, "server_url": "https://ntfy.sh", "topic": ""},
        "smtp":     {"enabled": False, "host": "", "port": 587, "username": "",
                     "password": "", "from_addr": "", "to_addr": "", "use_tls": True},
        "webhook":  {"enabled": False, "url": "", "method": "POST", "headers": {}},
    },
    "triggers": {
        "smart_warning":    True,
        "backup_failed":    True,
        "backup_completed": False,
        "disk_full_90":     True,
        "login_failed":     True,
        "container_crash":  True,
        "update_available": True,
        "raid_degraded":    True,
    },
}

SENSITIVE_KEYS = {"bot_token", "app_token", "password", "webhook_url", "url"}

# ── Helpers ───────────────────────────────────────────────────────────────

def _get_inbox_db():
    os.makedirs(os.path.dirname(_INBOX_DB), exist_ok=True)
    conn = sqlite3.connect(_INBOX_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_inbox_db():
    conn = _get_inbox_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            type TEXT DEFAULT 'info',
            category TEXT DEFAULT 'system',
            read INTEGER DEFAULT 0,
            action_app TEXT,
            action_tab TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_inbox_ts ON inbox(ts)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_inbox_read ON inbox(read)')
    conn.commit()
    conn.close()


def push_inbox(title, message, msg_type='info', category='system', action_app=None, action_tab=None):
    """Add a notification to the in-app inbox and push via SocketIO."""
    ts = time.time()
    try:
        conn = _get_inbox_db()
        conn.execute(
            'INSERT INTO inbox (ts, title, message, type, category, action_app, action_tab) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (ts, title, message, msg_type, category, action_app, action_tab)
        )
        conn.commit()
        # Get unread count
        row = conn.execute('SELECT COUNT(*) FROM inbox WHERE read = 0').fetchone()
        unread = row[0] if row else 0
        conn.close()
    except Exception:
        unread = 0

    notif = {
        'title': title,
        'message': message,
        'type': msg_type,
        'category': category,
        'time': ts,
        'action': {'app': action_app, 'tab': action_tab} if action_app else None,
    }

    if _socketio:
        try:
            _socketio.emit('notification_new', notif)
            _socketio.emit('notification_count', {'count': unread})
        except Exception:
            pass


# ── Type mapping from event levels ────────────────────────────────────────

_LEVEL_TO_TYPE = {
    'debug': 'info',
    'info': 'info',
    'warning': 'warning',
    'error': 'error',
}

def _load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
            merged = json.loads(json.dumps(_DEFAULT_CONFIG))
            for section in ('channels', 'triggers'):
                if section in cfg:
                    for k, v in cfg[section].items():
                        if section == 'channels' and k in merged['channels']:
                            merged['channels'][k].update(v)
                        else:
                            merged[section][k] = v
            return merged
        except Exception:
            pass
    return json.loads(json.dumps(_DEFAULT_CONFIG))


def _save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)


def _mask(value):
    """Mask sensitive string, showing only last 4 chars."""
    s = str(value)
    if len(s) <= 4:
        return '****'
    return '*' * (len(s) - 4) + s[-4:]


def _mask_config(cfg):
    """Return a copy with sensitive values masked."""
    out = json.loads(json.dumps(cfg))
    for ch_name, ch_cfg in out.get('channels', {}).items():
        for key in SENSITIVE_KEYS:
            if key in ch_cfg and ch_cfg[key] and isinstance(ch_cfg[key], str):
                ch_cfg[key] = _mask(ch_cfg[key])
    return out


def _load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_history(history):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, 'w') as f:
        json.dump(history[-MAX_HISTORY:], f, indent=2)


def _append_history(channel, title, message, success, error_msg=None):
    history = _load_history()
    history.append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'channel': channel,
        'title': title,
        'message': message,
        'success': success,
        'error': error_msg,
    })
    _save_history(history)

# ── Channel senders ───────────────────────────────────────────────────────

def _send_telegram(cfg, title, message):
    token = cfg.get('bot_token', '')
    chat_id = cfg.get('chat_id', '')
    if not token or not chat_id:
        raise ValueError('bot_token and chat_id are required')
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    resp = _requests.post(url, json={
        'chat_id': chat_id,
        'text': f'*{title}*\n{message}',
        'parse_mode': 'Markdown',
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _send_discord(cfg, title, message):
    webhook_url = cfg.get('webhook_url', '')
    if not webhook_url:
        raise ValueError('webhook_url is required')
    resp = _requests.post(webhook_url, json={
        'content': f'**{title}**\n{message}',
    }, timeout=15)
    resp.raise_for_status()


def _send_gotify(cfg, title, message):
    server_url = cfg.get('server_url', '').rstrip('/')
    token = cfg.get('app_token', '')
    if not server_url or not token:
        raise ValueError('server_url and app_token are required')
    resp = _requests.post(f'{server_url}/message', params={'token': token}, json={
        'title': title,
        'message': message,
        'priority': 5,
    }, timeout=15)
    resp.raise_for_status()


def _send_ntfy(cfg, title, message):
    server_url = cfg.get('server_url', 'https://ntfy.sh').rstrip('/')
    topic = cfg.get('topic', '')
    if not topic:
        raise ValueError('topic is required')
    resp = _requests.post(f'{server_url}/{topic}', data=message.encode('utf-8'),
                          headers={'Title': title}, timeout=15)
    resp.raise_for_status()


def _send_smtp(cfg, title, message):
    host = cfg.get('host', '')
    port = int(cfg.get('port', 587))
    username = cfg.get('username', '')
    password = cfg.get('password', '')
    from_addr = cfg.get('from_addr', '')
    to_addr = cfg.get('to_addr', '')
    use_tls = cfg.get('use_tls', True)

    if not host or not from_addr or not to_addr:
        raise ValueError('host, from_addr and to_addr are required')

    msg = MIMEText(message)
    msg['Subject'] = f'[EthOS] {title}'
    msg['From'] = from_addr
    msg['To'] = to_addr

    with smtplib.SMTP(host, port, timeout=15) as srv:
        if use_tls:
            srv.starttls()
        if username and password:
            srv.login(username, password)
        srv.sendmail(from_addr, [to_addr], msg.as_string())


def _send_webhook(cfg, title, message):
    url = cfg.get('url', '')
    method = cfg.get('method', 'POST').upper()
    headers = cfg.get('headers', {})
    if not url:
        raise ValueError('url is required')
    payload = {'title': title, 'message': message, 'timestamp': datetime.now().isoformat()}
    headers.setdefault('Content-Type', 'application/json')
    resp = _requests.request(method, url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()


_SENDERS = {
    'telegram': _send_telegram,
    'discord':  _send_discord,
    'gotify':   _send_gotify,
    'ntfy':     _send_ntfy,
    'smtp':     _send_smtp,
    'webhook':  _send_webhook,
}

# ── Trigger mapping ──────────────────────────────────────────────────────

_EVENT_TRIGGER_MAP = {
    ('smart',    'warning'): 'smart_warning',
    ('smart',    'error'):   'smart_warning',
    ('backup',   'error'):   'backup_failed',
    ('backup',   'info'):    'backup_completed',
    ('storage',  'warning'): 'disk_full_90',
    ('storage',  'error'):   'disk_full_90',
    ('auth',     'warning'): 'login_failed',
    ('auth',     'error'):   'login_failed',
    ('docker',   'error'):   'container_crash',
    ('update',   'info'):    'update_available',
    ('raid',     'error'):   'raid_degraded',
    ('raid',     'warning'): 'raid_degraded',
}

# ── Public API ────────────────────────────────────────────────────────────

def send_notification(title, message, category='system', level='info'):
    """Send notification to all enabled channels (non-blocking)."""
    cfg = _load_config()

    trigger_key = _EVENT_TRIGGER_MAP.get((category, level))
    if trigger_key and not cfg['triggers'].get(trigger_key, False):
        return

    channels = cfg.get('channels', {})
    enabled = {k: v for k, v in channels.items() if v.get('enabled')}
    if not enabled:
        return

    def _do_send(ch_name, ch_cfg):
        sender = _SENDERS.get(ch_name)
        if not sender:
            return
        try:
            sender(ch_cfg, title, message)
            _append_history(ch_name, title, message, True)
        except Exception as exc:
            _append_history(ch_name, title, message, False, str(exc))

    for name, ch_cfg in enabled.items():
        if _HAS_GEVENT:
            gevent.spawn(_do_send, name, ch_cfg)
        else:
            try:
                _do_send(name, ch_cfg)
            except Exception:
                pass


def notify_event(category, level, message):
    """Hook callable from eventlog — maps event to trigger, sends to channels + inbox."""
    # Always push to inbox for warning/error events
    if level in ('warning', 'error'):
        msg_type = _LEVEL_TO_TYPE.get(level, 'info')
        title = f'{category.title()}: {level.title()}'
        push_inbox(title, message, msg_type=msg_type, category=category)

    trigger_key = _EVENT_TRIGGER_MAP.get((category, level))
    if not trigger_key:
        return
    cfg = _load_config()
    if not cfg['triggers'].get(trigger_key, False):
        return
    title = f'EthOS: {trigger_key.replace("_", " ").title()}'

    # Push to inbox for triggered events too (if not already pushed above)
    if level not in ('warning', 'error'):
        push_inbox(title, message, msg_type=_LEVEL_TO_TYPE.get(level, 'info'), category=category)

    send_notification(title, message, category, level)

# ── Routes ────────────────────────────────────────────────────────────────

@notifications_bp.route('/config', methods=['GET'])
def get_config():
    cfg = _load_config()
    return jsonify(_mask_config(cfg))


@notifications_bp.route('/config', methods=['PUT'])
def put_config():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'invalid JSON'}), 400

    cfg = _load_config()

    if 'channels' in data:
        for ch_name, ch_new in data['channels'].items():
            if ch_name not in cfg['channels']:
                continue
            for key, val in ch_new.items():
                if key in SENSITIVE_KEYS and isinstance(val, str) and val.startswith('*'):
                    continue
                cfg['channels'][ch_name][key] = val

    if 'triggers' in data:
        for k, v in data['triggers'].items():
            if k in cfg['triggers']:
                cfg['triggers'][k] = bool(v)

    _save_config(cfg)
    return jsonify({'ok': True})


@notifications_bp.route('/test', methods=['POST'])
def test_channel():
    data = request.get_json(silent=True) or {}
    ch_name = data.get('channel', '')
    if ch_name not in _SENDERS:
        return jsonify({'error': f'Unknown channel: {ch_name}'}), 400

    cfg = _load_config()
    ch_cfg = cfg['channels'].get(ch_name, {})
    if not ch_cfg.get('enabled'):
        return jsonify({'error': 'Channel is not enabled'}), 400

    sender = _SENDERS[ch_name]
    try:
        sender(ch_cfg, 'EthOS Test', 'This is a test notification from your EthOS NAS.')
        _append_history(ch_name, 'EthOS Test', 'Test notification', True)
        return jsonify({'status': 'ok'})
    except Exception as exc:
        _append_history(ch_name, 'EthOS Test', 'Test notification', False, str(exc))
        return jsonify({'error': str(exc)}), 500


@notifications_bp.route('/history', methods=['GET'])
def get_history():
    history = _load_history()
    history.reverse()
    return jsonify(history[:50])


# ── Inbox routes (in-app notification center) ────────────────────────────

@notifications_bp.route('', methods=['GET'])
@notifications_bp.route('/', methods=['GET'])
def get_inbox():
    """Return inbox notifications for the bell dropdown. Most recent first."""
    limit = min(int(request.args.get('limit', 50)), 200)
    try:
        conn = _get_inbox_db()
        rows = conn.execute(
            'SELECT id, ts, title, message, type, category, read, action_app, action_tab '
            'FROM inbox ORDER BY ts DESC LIMIT ?', (limit,)
        ).fetchall()
        conn.close()
    except Exception:
        return jsonify([])

    result = []
    for r in rows:
        item = {
            'id': r['id'],
            'title': r['title'],
            'message': r['message'],
            'type': r['type'],
            'category': r['category'],
            'time': r['ts'],
            'read': bool(r['read']),
        }
        if r['action_app']:
            item['action'] = {'app': r['action_app'], 'tab': r['action_tab'] or ''}
        result.append(item)
    return jsonify(result)


@notifications_bp.route('/clear', methods=['POST'])
def clear_inbox():
    """Delete all inbox notifications."""
    try:
        conn = _get_inbox_db()
        conn.execute('DELETE FROM inbox')
        conn.commit()
        conn.close()
    except Exception:
        pass

    if _socketio:
        try:
            _socketio.emit('notification_count', {'count': 0})
        except Exception:
            pass

    return jsonify({'ok': True})


@notifications_bp.route('/read', methods=['POST'])
def mark_read():
    """Mark specific notification(s) as read. Body: {ids: [1,2,3]}"""
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({'error': 'ids array required'}), 400

    try:
        conn = _get_inbox_db()
        placeholders = ','.join('?' for _ in ids)
        conn.execute(f'UPDATE inbox SET read = 1 WHERE id IN ({placeholders})', ids)
        conn.commit()
        row = conn.execute('SELECT COUNT(*) FROM inbox WHERE read = 0').fetchone()
        unread = row[0] if row else 0
        conn.close()
    except Exception:
        unread = 0

    if _socketio:
        try:
            _socketio.emit('notification_count', {'count': unread})
        except Exception:
            pass

    return jsonify({'ok': True, 'unread': unread})


@notifications_bp.route('/read-all', methods=['POST'])
def mark_all_read():
    """Mark all inbox notifications as read."""
    try:
        conn = _get_inbox_db()
        conn.execute('UPDATE inbox SET read = 1')
        conn.commit()
        conn.close()
    except Exception:
        pass

    if _socketio:
        try:
            _socketio.emit('notification_count', {'count': 0})
        except Exception:
            pass

    return jsonify({'ok': True, 'unread': 0})


@notifications_bp.route('/unread-count', methods=['GET'])
def unread_count():
    """Return unread notification count."""
    try:
        conn = _get_inbox_db()
        row = conn.execute('SELECT COUNT(*) FROM inbox WHERE read = 0').fetchone()
        count = row[0] if row else 0
        conn.close()
    except Exception:
        count = 0
    return jsonify({'count': count})


@notifications_bp.route('/subscribe', methods=['POST'])
def subscribe_push():
    """Accept Web Push subscription from browser (stub).

    Stores the subscription JSON for potential future use with Web Push
    notifications.  Currently a no-op — notifications use SocketIO.
    """
    return jsonify({'ok': True})


# ── Local mail server auto-detect ─────────────────────────────────────────

def _detect_local_mail_server():
    """Check if local Postfix mail server is running with accounts configured.

    Returns dict with suggested SMTP config or None if unavailable.
    """
    import shutil
    import subprocess
    import sqlite3 as _sqlite3

    if not shutil.which('postfix'):
        return None

    # Check if Postfix is active
    try:
        r = subprocess.run(
            ['systemctl', 'is-active', 'postfix'],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
    except Exception:
        return None

    # Find mail DB and get first account + hostname
    mail_data_dir = os.environ.get('ETHOS_DATA', '/opt/ethos/data')
    # Check data partition first, then local data/
    from host import get_data_disk
    dd = get_data_disk()
    db_path = None
    for candidate in [
        os.path.join(dd, 'mail', 'mail.db') if dd else None,
        os.path.join(mail_data_dir, 'mail', 'mail.db'),
    ]:
        if candidate and os.path.isfile(candidate):
            db_path = candidate
            break

    if not db_path:
        return None

    try:
        conn = _sqlite3.connect(db_path, timeout=3)
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            'SELECT email FROM accounts WHERE enabled = 1 ORDER BY id LIMIT 1'
        ).fetchone()
        conn.close()
        if not row:
            return None
        first_email = row['email']
    except Exception:
        return None

    # Read hostname from mail config
    config_path = os.path.join(os.path.dirname(db_path), 'config.json')
    hostname = 'localhost'
    try:
        with open(config_path) as f:
            cfg = json.load(f)
            hostname = cfg.get('hostname', hostname)
    except Exception:
        pass

    return {
        'available': True,
        'host': '127.0.0.1',
        'port': 25,
        'username': '',
        'password': '',
        'from_addr': f'noreply@{hostname.replace("mail.", "", 1) if hostname.startswith("mail.") else hostname}',
        'to_addr': first_email,
        'use_tls': False,
    }


@notifications_bp.route('/local-smtp', methods=['GET'])
def detect_local_smtp():
    """Auto-detect local Postfix mail server for notification relay."""
    result = _detect_local_mail_server()
    if result:
        return jsonify(result)
    return jsonify({'available': False})
