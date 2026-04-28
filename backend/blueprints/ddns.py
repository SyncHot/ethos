"""
EthOS – Dynamic DNS manager blueprint.

Supported providers:
  - DuckDNS   (duckdns.org)  – recommended, no registration forms
  - dynv6     (dynv6.com)    – free, IPv4+IPv6
  - No-IP     (noip.com)     – classic, free tier
  - Cloudflare               – own domain via API
  - Afraid.org / FreeDNS     – free subdomains
  - Custom URL               – any service with HTTP-GET/POST update API
"""

import json, os, time, threading, logging, secrets, re, sys
from datetime import datetime
from flask import Blueprint, jsonify, request
from blueprints.admin_required import admin_required

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from crypto_utils import encrypt_secret, decrypt_secret
from utils import load_json as _load_json, save_json as _save_json, register_pkg_routes
from host import data_path as _data_path

_DDNS_SECRET_KEYS = ('token', 'password', 'api_token', 'update_key')

log = logging.getLogger('ddns')


ddns_bp = Blueprint('ddns', __name__, url_prefix='/api/ddns')

# ── paths ──────────────────────────────────────────────────────
_CONFIG_FILE  = _data_path('ddns_config.json')
_HISTORY_FILE = _data_path('ddns_history.json')

# ── state ──────────────────────────────────────────────────────
_timer: threading.Timer | None = None
_current_ip: str = ''
_last_update: str = ''
_last_status: str = ''        # 'ok' | 'error' | ''
_last_error: str = ''

# ── provider templates ─────────────────────────────────────────
PROVIDERS = {
    'duckdns': {
        'name': 'DuckDNS',
        'description': 'Free DDNS — login via GitHub/Google/Reddit. No registration forms.',
        'website': 'https://www.duckdns.org',
        'fields': [
            {'key': 'domain',   'label': 'Subdomain (without .duckdns.org)', 'type': 'text',     'required': True, 'placeholder': 'my-nas'},
            {'key': 'token',    'label': 'Token',                        'type': 'password',  'required': True, 'placeholder': 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'},
        ],
        'no_registration': True,
    },
    'dynv6': {
        'name': 'dynv6',
        'description': 'Free DDNS with IPv4 and IPv6 support. Only an e-mail is needed to register.',
        'website': 'https://dynv6.com',
        'fields': [
            {'key': 'hostname', 'label': 'Hostname (e.g. mynas.dynv6.net)', 'type': 'text',     'required': True},
            {'key': 'token',    'label': 'HTTP Token',                      'type': 'password',  'required': True},
        ],
        'no_registration': False,
    },
    'noip': {
        'name': 'No-IP',
        'description': 'Popular DDNS provider with a free plan. Requires confirmation every 30 days.',
        'website': 'https://www.noip.com',
        'fields': [
            {'key': 'hostname', 'label': 'Hostname',   'type': 'text',     'required': True, 'placeholder': 'mynas.ddns.net'},
            {'key': 'username', 'label': 'Login',       'type': 'text',     'required': True},
            {'key': 'password', 'label': 'Password',    'type': 'password', 'required': True},
        ],
        'no_registration': False,
    },
    'cloudflare': {
        'name': 'Cloudflare',
        'description': 'Update a DNS A/AAAA record on your own domain managed by Cloudflare.',
        'website': 'https://dash.cloudflare.com',
        'fields': [
            {'key': 'zone_id',   'label': 'Zone ID',          'type': 'text',     'required': True},
            {'key': 'record_name','label': 'Record (e.g. nas.example.com)', 'type': 'text', 'required': True},
            {'key': 'api_token', 'label': 'API Token',        'type': 'password', 'required': True},
        ],
        'no_registration': False,
    },
    'freedns': {
        'name': 'FreeDNS (afraid.org)',
        'description': 'Free subdomains from a large pool of domains. Simple update URL.',
        'website': 'https://freedns.afraid.org',
        'fields': [
            {'key': 'update_key', 'label': 'Update Key / URL', 'type': 'text', 'required': True,
             'placeholder': 'https://freedns.afraid.org/dynamic/update.php?XXXX'},
        ],
        'no_registration': False,
    },
    'custom': {
        'name': 'Custom URL',
        'description': 'Any DDNS service that supports updates via HTTP GET/POST.',
        'website': '',
        'fields': [
            {'key': 'update_url', 'label': 'Update URL',       'type': 'text',     'required': True,
             'placeholder': 'https://example.com/update?ip={{IP}}&key=abc'},
            {'key': 'method',     'label': 'HTTP Method',       'type': 'select',   'required': True,
             'options': ['GET', 'POST'], 'default': 'GET'},
        ],
        'no_registration': False,
    },
}


# ═══════════════════════════════════════════════════════════════
#  Config helpers
# ═══════════════════════════════════════════════════════════════

def _load_config():
    """Return config dict with secrets decrypted transparently."""
    cfg = _load_json(_CONFIG_FILE, None)
    if cfg:
        for p in cfg.get('providers', []):
            for key in _DDNS_SECRET_KEYS:
                if p.get(key):
                    p[key] = decrypt_secret(p[key])
        return cfg
    return {'providers': [], 'interval_min': 5, 'enabled': False}


def _save_config(cfg):
    """Save config dict with secrets encrypted on disk."""
    to_save = {**cfg}
    encrypted_providers = []
    for p in cfg.get('providers', []):
        pp = dict(p)
        for key in _DDNS_SECRET_KEYS:
            if pp.get(key):
                pp[key] = encrypt_secret(pp[key])
        encrypted_providers.append(pp)
    to_save['providers'] = encrypted_providers
    _save_json(_CONFIG_FILE, to_save)


def _load_history():
    return _load_json(_HISTORY_FILE, [])


def _save_history(history):
    _save_json(_HISTORY_FILE, history[-200:])


def _add_history(provider_id, provider_name, ip, status, message=''):
    history = _load_history()
    history.append({
        'time': datetime.now().isoformat(),
        'provider_id': provider_id,
        'provider_name': provider_name,
        'ip': ip,
        'status': status,
        'message': message,
    })
    _save_history(history)


# ═══════════════════════════════════════════════════════════════
#  IP detection
# ═══════════════════════════════════════════════════════════════

def _get_public_ip():
    """Get public IPv4 address using multiple fallback services."""
    import urllib.request
    services = [
        'https://api.ipify.org',
        'https://ifconfig.me/ip',
        'https://ipinfo.io/ip',
        'https://checkip.amazonaws.com',
        'https://icanhazip.com',
    ]
    for url in services:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-DDNS/1.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                ip = resp.read().decode().strip()
                if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
                    return ip
        except Exception:
            continue
    return ''


# ═══════════════════════════════════════════════════════════════
#  Provider update functions
# ═══════════════════════════════════════════════════════════════

def _update_duckdns(cfg, ip):
    import urllib.request
    domain = cfg.get('domain', '').strip()
    token = cfg.get('token', '').strip()
    url = f'https://www.duckdns.org/update?domains={domain}&token={token}&ip={ip}'
    req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-DDNS/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode().strip()
    if body == 'OK':
        return True, 'OK'
    return False, f'DuckDNS returned: {body}'


def _update_dynv6(cfg, ip):
    import urllib.request
    hostname = cfg.get('hostname', '').strip()
    token = cfg.get('token', '').strip()
    url = f'https://ipv4.dynv6.com/api/update?hostname={hostname}&token={token}&ipv4={ip}'
    req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-DDNS/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode().strip()
    if 'updated' in body.lower() or 'unchanged' in body.lower():
        return True, body
    return False, body


def _update_noip(cfg, ip):
    import urllib.request, base64
    hostname = cfg.get('hostname', '').strip()
    username = cfg.get('username', '').strip()
    password = cfg.get('password', '')
    url = f'https://dynupdate.no-ip.com/nic/update?hostname={hostname}&myip={ip}'
    auth = base64.b64encode(f'{username}:{password}'.encode()).decode()
    req = urllib.request.Request(url, headers={
        'User-Agent': 'EthOS-DDNS/1.0',
        'Authorization': f'Basic {auth}',
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode().strip()
    if body.startswith('good') or body.startswith('nochg'):
        return True, body
    return False, body


def _update_cloudflare(cfg, ip):
    import urllib.request
    zone_id = cfg.get('zone_id', '').strip()
    record_name = cfg.get('record_name', '').strip()
    api_token = cfg.get('api_token', '').strip()

    headers = {
        'Authorization': f'Bearer {api_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'EthOS-DDNS/1.0',
    }

    # 1. Get record ID
    list_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=A&name={record_name}'
    req = urllib.request.Request(list_url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data.get('success') or not data.get('result'):
        return False, 'No A record found for ' + record_name

    record = data['result'][0]
    record_id = record['id']

    # 2. Update record
    update_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}'
    payload = json.dumps({
        'type': 'A',
        'name': record_name,
        'content': ip,
        'ttl': 120,
        'proxied': record.get('proxied', False),
    }).encode()
    req = urllib.request.Request(update_url, data=payload, method='PUT', headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get('success'):
        return True, 'Cloudflare OK'
    return False, str(data.get('errors', 'Unknown error'))


def _update_freedns(cfg, ip):
    import urllib.request
    update_key = cfg.get('update_key', '').strip()
    # If user pasted full URL
    if update_key.startswith('http'):
        url = update_key
    else:
        url = f'https://freedns.afraid.org/dynamic/update.php?{update_key}'
    if '?' in url and 'address=' not in url:
        url += f'&address={ip}'
    req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-DDNS/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode().strip()
    if 'Updated' in body or 'has not changed' in body:
        return True, body
    return False, body


def _update_custom(cfg, ip):
    import urllib.request
    url = cfg.get('update_url', '').strip().replace('{{IP}}', ip).replace('{IP}', ip)
    method = cfg.get('method', 'GET').upper()
    req = urllib.request.Request(url, method=method, headers={'User-Agent': 'EthOS-DDNS/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode().strip()[:500]
    return True, body


_UPDATERS = {
    'duckdns':    _update_duckdns,
    'dynv6':      _update_dynv6,
    'noip':       _update_noip,
    'cloudflare': _update_cloudflare,
    'freedns':    _update_freedns,
    'custom':     _update_custom,
}


# ═══════════════════════════════════════════════════════════════
#  Background updater
# ═══════════════════════════════════════════════════════════════

def _run_update(force=False):
    """Check public IP and update all enabled providers."""
    global _current_ip, _last_update, _last_status, _last_error

    ip = _get_public_ip()
    if not ip:
        _last_status = 'error'
        _last_error = 'Failed to get public IP'
        log.warning('[ddns] Cannot determine public IP')
        return {'ok': False, 'error': _last_error}

    old_ip = _current_ip
    _current_ip = ip

    cfg = _load_config()
    providers = cfg.get('providers', [])
    active = [p for p in providers if p.get('active', True)]

    if not active:
        _last_status = 'ok'
        _last_error = ''
        _last_update = datetime.now().isoformat()
        if old_ip and old_ip != ip:
            log.info('[ddns] IP changed: %s -> %s (no active providers to update)', old_ip, ip)
        return {'ok': True, 'ip': ip, 'message': 'No active providers'}

    # Skip if IP hasn't changed (unless forced)
    if ip == _current_ip and not force:
        _last_status = 'ok'
        return {'ok': True, 'ip': ip, 'message': 'IP unchanged', 'skipped': True}

    results = []
    all_ok = True
    for p in active:
        ptype = p.get('type', '')
        updater = _UPDATERS.get(ptype)
        if not updater:
            results.append({'id': p.get('id'), 'ok': False, 'error': f'Unknown provider: {ptype}'})
            all_ok = False
            continue
        try:
            ok, msg = updater(p, ip)
            results.append({'id': p.get('id'), 'name': p.get('name', ptype), 'ok': ok, 'message': msg})
            _add_history(p.get('id', ''), p.get('name', ptype), ip, 'ok' if ok else 'error', msg)
            if not ok:
                all_ok = False
        except Exception as e:
            errmsg = str(e)[:300]
            results.append({'id': p.get('id'), 'name': p.get('name', ptype), 'ok': False, 'error': errmsg})
            _add_history(p.get('id', ''), p.get('name', ptype), ip, 'error', errmsg)
            all_ok = False
            log.warning('[ddns] Update error for %s: %s', ptype, errmsg)

    _current_ip = ip
    _last_update = datetime.now().isoformat()
    _last_status = 'ok' if all_ok else 'error'
    _last_error = '' if all_ok else '; '.join(r.get('error', r.get('message', '')) for r in results if not r.get('ok'))

    return {'ok': all_ok, 'ip': ip, 'results': results}


def _schedule_next():
    """Schedule the next periodic update."""
    global _timer
    cfg = _load_config()
    if not cfg.get('enabled', False):
        return
    interval = max(1, cfg.get('interval_min', 5)) * 60

    def _tick():
        try:
            _run_update()
        except Exception as e:
            log.warning('[ddns] Periodic update error: %s', e)
        _schedule_next()

    _timer = threading.Timer(interval, _tick)
    _timer.daemon = True
    _timer.start()


def _stop_timer():
    global _timer
    if _timer:
        _timer.cancel()
        _timer = None


def start_ddns():
    """Called on app startup to begin periodic updates if enabled."""
    cfg = _load_config()
    if cfg.get('enabled', False):
        log.info('[ddns] Starting periodic updates (every %d min)', cfg.get('interval_min', 5))
        # Initial update — always runs to fetch IP, even without providers
        threading.Thread(target=_run_update, daemon=True).start()
        _schedule_next()


# ═══════════════════════════════════════════════════════════════
#  API routes
# ═══════════════════════════════════════════════════════════════

@ddns_bp.route('/providers')
def list_providers():
    """Return available DDNS provider templates."""
    result = []
    for pid, pinfo in PROVIDERS.items():
        result.append({
            'id': pid,
            'name': pinfo['name'],
            'description': pinfo['description'],
            'website': pinfo['website'],
            'fields': pinfo['fields'],
            'no_registration': pinfo.get('no_registration', False),
        })
    return jsonify(result)


@ddns_bp.route('/config')
def get_config():
    """Return current DDNS configuration (passwords masked)."""
    cfg = _load_config()
    # Mask sensitive fields
    safe = {**cfg}
    safe_providers = []
    for p in cfg.get('providers', []):
        sp = {**p}
        for key in ('token', 'password', 'api_token', 'update_key'):
            if key in sp and sp[key]:
                sp[key] = '••••••••'
        safe_providers.append(sp)
    safe['providers'] = safe_providers
    return jsonify(safe)


@ddns_bp.route('/config', methods=['POST'])
def save_config_route():
    """Save DDNS configuration."""
    data = request.json or {}
    old_cfg = _load_config()

    # Merge: if password field is masked, keep old value
    old_by_id = {p.get('id', ''): p for p in old_cfg.get('providers', [])}
    for p in data.get('providers', []):
        if not p.get('id'):
            p['id'] = secrets.token_hex(6)
        old = old_by_id.get(p['id'], {})
        for key in ('token', 'password', 'api_token', 'update_key'):
            if p.get(key) == '••••••••' or p.get(key) == '':
                if key in old:
                    p[key] = old[key]

    cfg = {
        'providers': data.get('providers', []),
        'interval_min': max(1, int(data.get('interval_min', 5))),
        'enabled': bool(data.get('enabled', False)),
    }
    _save_config(cfg)

    # Restart/stop timer
    _stop_timer()
    if cfg['enabled'] and cfg['providers']:
        _schedule_next()
        # Run initial update in background
        threading.Thread(target=lambda: _run_update(force=True), daemon=True).start()

    return jsonify({'ok': True})


@ddns_bp.route('/status')
def ddns_status_route():
    """Return current DDNS status."""
    global _current_ip
    cfg = _load_config()
    # If IP is not yet known, try fetching it now (quick)
    ip = _current_ip
    if not ip:
        ip = _get_public_ip()
        if ip:
            _current_ip = ip
    return jsonify({
        'enabled': cfg.get('enabled', False),
        'interval_min': cfg.get('interval_min', 5),
        'providers_count': len(cfg.get('providers', [])),
        'active_count': len([p for p in cfg.get('providers', []) if p.get('active', True)]),
        'current_ip': ip,
        'last_update': _last_update,
        'last_status': _last_status,
        'last_error': _last_error,
    })


@ddns_bp.route('/update', methods=['POST'])
def manual_update():
    """Trigger a manual DNS update now."""
    result = _run_update(force=True)
    return jsonify(result)


@ddns_bp.route('/check-ip')
def check_ip():
    """Just return current public IP without updating DNS."""
    global _current_ip
    ip = _get_public_ip()
    if ip:
        _current_ip = ip
    return jsonify({'ip': ip or '', 'ok': bool(ip)})


@ddns_bp.route('/history')
def get_history():
    """Return update history (last 200 entries)."""
    history = _load_history()
    history.reverse()  # newest first
    return jsonify(history)


@ddns_bp.route('/history', methods=['DELETE'])
def clear_history():
    """Clear update history."""
    _save_history([])
    return jsonify({'ok': True})


# ── Package routes (for App Store) ─────────────────────────────

def _ddns_on_uninstall(wipe):
    _stop_timer()
    log.info('[ddns] Uninstall triggered')

@ddns_bp.route('/install', methods=['POST'])
@admin_required
def install_ddns():
    return jsonify({'status': 'ok'})

@ddns_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall_ddns():
    _ddns_on_uninstall(wipe=True)
    return jsonify({'status': 'ok'})

register_pkg_routes(
    ddns_bp,
    install_message='Dynamic DNS ready.',
    wipe_files=[_CONFIG_FILE, _HISTORY_FILE],
    on_uninstall=_ddns_on_uninstall,
)
