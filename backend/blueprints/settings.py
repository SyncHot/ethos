"""
EthOS — System Settings Blueprint
Manages NAS name, port, hostname, password changes, SSL/Let's Encrypt,
domain/subdomain management with nginx reverse proxy, and system information.
"""

import os
import re
import json
import time
import shlex
import subprocess
import uuid
import psutil
from flask import Blueprint, request, jsonify, g
from datetime import datetime

settings_bp = Blueprint('settings', __name__, url_prefix='/api/settings')

# ── Imports from host module ──
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, ETHOS_ROOT, data_path as _data_path, apt_install as _apt_install, app_path as _app_path

try:
    import paramiko
    _HAS_SSH = True
except ImportError:
    _HAS_SSH = False

from ssh_utils import get_ssh_client as _get_ssh_client, ssh_exec as _ssh_exec


# ── Helpers ──

def _env_file():
    return os.path.join(ETHOS_ROOT, 'ethos.env')


def _read_env():
    """Read current ethos.env values."""
    env = {}
    try:
        with open(_env_file(), 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def _write_env_key(key, value):
    """Update or add a key in ethos.env."""
    ef = shlex.quote(_env_file())
    safe_val = shlex.quote(value)
    # Check if key exists
    r = _host_run(f'grep -q "^{key}=" {ef}', timeout=5)
    if r.returncode == 0:
        _host_run(f'sed -i "s|^{key}=.*|{key}={safe_val}|" {ef}', timeout=10)
    else:
        _host_run(f'echo "{key}={safe_val}" >> {ef}', timeout=10)


def _get_hostname():
    try:
        r = _host_run('hostname', timeout=5)
        return r.stdout.strip() if r.returncode == 0 else 'unknown'
    except Exception:
        return 'unknown'


def _get_timezone():
    try:
        r = _host_run('timedatectl show --property=Timezone --value', timeout=5)
        return r.stdout.strip() if r.returncode == 0 else 'UTC'
    except Exception:
        return 'UTC'


def _list_timezones():
    try:
        r = _host_run('timedatectl list-timezones', timeout=10)
        if r.returncode == 0:
            return [tz.strip() for tz in r.stdout.strip().split('\n') if tz.strip()]
    except Exception:
        pass
    return ['UTC', 'Europe/Warsaw', 'Europe/London', 'America/New_York', 'America/Los_Angeles', 'Asia/Tokyo']


# ──────────────────────── Routes ────────────────────────

@settings_bp.route('/', methods=['GET'])
def get_settings():
    """Return current system settings."""
    env = _read_env()
    hostname = _get_hostname()
    timezone = _get_timezone()

    # System info
    boot_time = psutil.boot_time()
    uptime = time.time() - boot_time

    # Kernel
    try:
        kernel = _host_run('uname -r', timeout=5).stdout.strip()
    except Exception:
        kernel = 'unknown'

    # Architecture
    try:
        arch = _host_run('uname -m', timeout=5).stdout.strip()
    except Exception:
        arch = 'unknown'

    return jsonify({
        'nas_name': env.get('NAS_NAME', 'EthOS'),
        'port': int(env.get('PORT', '9000')),
        'hostname': hostname,
        'timezone': timezone,
        'ethos_root': env.get('ETHOS_ROOT', ETHOS_ROOT),
        'backup_dir': env.get('BACKUP_DIR', ''),
        'uptime': uptime,
        'kernel': kernel,
        'arch': arch,
    })


@settings_bp.route('/', methods=['POST'])
def update_settings():
    """Update system settings. Returns which settings changed and if restart is needed."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator może zmieniać ustawienia'}), 403

    data = request.json or {}
    changes = []
    restart_needed = False
    errors = []

    env = _read_env()
    current_port = int(env.get('PORT', '9000'))
    current_name = env.get('NAS_NAME', 'EthOS')

    # ── NAS Name ──
    new_name = data.get('nas_name', '').strip()
    if new_name and new_name != current_name:
        _write_env_key('NAS_NAME', new_name)
        # Update in-memory value in main app
        os.environ['NAS_NAME'] = new_name
        changes.append(f'Nazwa NAS zmieniona na: {new_name}')

    # ── Port ──
    new_port = data.get('port')
    if new_port is not None:
        try:
            new_port = int(new_port)
            if new_port < 1 or new_port > 65535:
                errors.append('Port musi być w zakresie 1-65535')
            elif new_port != current_port:
                _write_env_key('PORT', str(new_port))
                changes.append(f'Port zmieniony na: {new_port}')
                restart_needed = True
        except (ValueError, TypeError):
            errors.append('Nieprawidłowy numer portu')

    # ── Hostname ──
    new_hostname = data.get('hostname', '').strip()
    if new_hostname:
        # Sanitize
        new_hostname = re.sub(r'[^a-zA-Z0-9\-]', '', new_hostname)
        if new_hostname and new_hostname != _get_hostname():
            r = _host_run(f'hostnamectl set-hostname {shlex.quote(new_hostname)}', timeout=10)
            if r.returncode == 0:
                changes.append(f'Hostname zmieniony na: {new_hostname}')
            else:
                errors.append(f'Błąd zmiany hostname: {r.stderr.strip()}')

    # ── Timezone ──
    new_tz = data.get('timezone', '').strip()
    if new_tz and new_tz != _get_timezone():
        r = _host_run(f'timedatectl set-timezone {shlex.quote(new_tz)}', timeout=10)
        if r.returncode == 0:
            changes.append(f'Strefa czasowa zmieniona na: {new_tz}')
        else:
            errors.append(f'Błąd zmiany strefy czasowej: {r.stderr.strip()}')

    if errors and not changes:
        return jsonify({'ok': False, 'errors': errors}), 400

    return jsonify({
        'ok': True,
        'changes': changes,
        'errors': errors,
        'restart_needed': restart_needed,
    })


@settings_bp.route('/change-password', methods=['POST'])
def change_password():
    """Change the current user's Linux password."""
    data = request.json or {}
    current_pw = data.get('current_password', '')
    new_pw = data.get('new_password', '')

    if not current_pw or not new_pw:
        return jsonify({'error': 'Oba pola hasła są wymagane'}), 400
    if len(new_pw) < 4:
        return jsonify({'error': 'Nowe hasło musi mieć minimum 4 znaki'}), 400
    if new_pw == 'ethos':
        return jsonify({'error': 'Hasło nie może być domyślne ("ethos")'}), 400

    username = g.username
    if not username:
        return jsonify({'error': 'Nie rozpoznano użytkownika'}), 401

    # Verify current password via shadow
    import warnings
    try:
        with open('/etc/shadow', 'r') as f:
            for line in f:
                parts = line.strip().split(':')
                if parts[0] == username:
                    stored_hash = parts[1]
                    break
            else:
                return jsonify({'error': 'Nie znaleziono użytkownika w systemie'}), 404

        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=DeprecationWarning)
            import crypt
            if crypt.crypt(current_pw, stored_hash) != stored_hash:
                return jsonify({'error': 'Nieprawidłowe obecne hasło'}), 403
    except PermissionError:
        return jsonify({'error': 'Brak uprawnień do weryfikacji hasła'}), 500

    # Change password
    safe = shlex.quote(f'{username}:{new_pw}')
    r = _host_run(f'echo {safe} | chpasswd', timeout=10)
    if r.returncode != 0:
        return jsonify({'error': f'Błąd zmiany hasła: {r.stderr.strip()}'}), 500

    # Ensure password changed marker exists (if this was the first run)
    try:
        marker = '/opt/ethos/.password_changed'
        if not os.path.exists(marker):
            with open(marker, 'w') as f:
                f.write(str(time.time()))
            # Also enable SSH if it was disabled
            subprocess.run(['systemctl', 'enable', '--now', 'ssh'], check=False)
    except Exception:
        pass

    return jsonify({'ok': True, 'message': 'Hasło zostało zmienione'})


@settings_bp.route('/timezones', methods=['GET'])
def list_tz():
    """Return list of available timezones."""
    return jsonify(_list_timezones())


@settings_bp.route('/restart', methods=['POST'])
def restart_app():
    """Restart EthOS service (after settings change)."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403

    import gevent
    def _do_restart():
        try:
            subprocess.run(['systemctl', 'restart', 'ethos'], timeout=60)
        except Exception:
            pass

    gevent.spawn_later(1, _do_restart)
    return jsonify({'ok': True, 'message': 'Restart za chwilę…'})


# ═══════════════════════════════════════════════════════════════
#  SSL / Let's Encrypt
# ═══════════════════════════════════════════════════════════════

_SSL_CONFIG_FILE = _data_path('ssl_config.json')
_CERT_DIR = '/etc/letsencrypt/live'


def _load_ssl_config():
    try:
        with open(_SSL_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {
            'enabled': False,
            'domain': '',
            'email': '',
            'https_port': 443,
            'redirect_http': True,
            'auto_renew': True,
        }


def _save_ssl_config(cfg):
    os.makedirs(os.path.dirname(_SSL_CONFIG_FILE), exist_ok=True)
    with open(_SSL_CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


def _certbot_installed():
    r = _host_run('command -v certbot', timeout=5)
    return r.returncode == 0


def _cert_paths(domain):
    """Return (fullchain, privkey) paths for a domain."""
    base = os.path.join(_CERT_DIR, domain)
    return os.path.join(base, 'fullchain.pem'), os.path.join(base, 'privkey.pem')


def _cert_info(domain):
    """Get certificate info for a domain."""
    fullchain, privkey = _cert_paths(domain)
    if not os.path.exists(fullchain):
        return None
    try:
        r = _host_run(
            f'openssl x509 -noout -subject -issuer -dates -serial '
            f'-in {shlex.quote(fullchain)}',
            timeout=10)
        if r.returncode != 0:
            return None
        info = {}
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if '=' in line:
                k, v = line.split('=', 1)
                k = k.strip().lower()
                if k == 'subject':
                    info['subject'] = v.strip()
                elif k == 'issuer':
                    info['issuer'] = v.strip()
                elif k.startswith('notbefore'):
                    info['not_before'] = v.strip()
                elif k.startswith('notafter'):
                    info['not_after'] = v.strip()
                elif k == 'serial':
                    info['serial'] = v.strip()
        # Parse dates
        for dk in ('not_before', 'not_after'):
            if dk in info:
                try:
                    dt = datetime.strptime(info[dk], '%b %d %H:%M:%S %Y %Z')
                    info[dk + '_iso'] = dt.isoformat()
                    if dk == 'not_after':
                        info['days_left'] = (dt - datetime.utcnow()).days
                except Exception:
                    pass
        info['fullchain'] = fullchain
        info['privkey'] = privkey
        info['domain'] = domain
        return info
    except Exception:
        return None


def _renewal_timer_exists():
    """Check if certbot auto-renewal systemd timer or cron is active."""
    r = _host_run('systemctl is-active certbot.timer 2>/dev/null', timeout=5)
    if r.returncode == 0 and 'active' in r.stdout.strip():
        return 'systemd'
    r2 = _host_run('crontab -l 2>/dev/null | grep -q certbot', timeout=5)
    if r2.returncode == 0:
        return 'cron'
    return None


@settings_bp.route('/ssl/status', methods=['GET'])
def ssl_status():
    """Return SSL/Let's Encrypt status."""
    cfg = _load_ssl_config()
    installed = _certbot_installed()

    result = {
        'certbot_installed': installed,
        'config': cfg,
        'cert': None,
        'renewal': None,
    }

    if cfg.get('domain'):
        cert = _cert_info(cfg['domain'])
        if cert:
            result['cert'] = cert
    result['renewal'] = _renewal_timer_exists()

    # Check if SSL is currently active (env var)
    env = _read_env()
    result['ssl_active'] = env.get('SSL_ENABLED', '') == '1'
    result['https_port'] = int(env.get('HTTPS_PORT', cfg.get('https_port', 443)))

    return jsonify(result)


@settings_bp.route('/ssl/install-certbot', methods=['POST'])
def install_certbot():
    """Install certbot package."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403
    if _certbot_installed():
        return jsonify({'ok': True, 'message': 'Certbot jest już zainstalowany'})

    r = _apt_install('certbot', timeout=120)
    if r.returncode != 0:
        return jsonify({'error': f'Instalacja nie powiodła się: {r.stderr.strip()[-200:]}'}), 500

    return jsonify({'ok': True, 'message': 'Certbot zainstalowany'})


@settings_bp.route('/ssl/obtain', methods=['POST'])
def ssl_obtain():
    """Obtain a Let's Encrypt certificate using standalone mode."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot nie jest zainstalowany'}), 400

    data = request.json or {}
    domain = data.get('domain', '').strip().lower()
    email = data.get('email', '').strip()
    https_port = int(data.get('https_port', 443))

    if not domain:
        return jsonify({'error': 'Domena jest wymagana'}), 400
    if not re.match(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$', domain):
        return jsonify({'error': 'Nieprawidłowa domena'}), 400
    if not email or '@' not in email:
        return jsonify({'error': 'Prawidłowy email jest wymagany'}), 400

    # Stop anything on port 80 temporarily
    _host_run('systemctl stop nginx apache2 2>/dev/null; fuser -k 80/tcp 2>/dev/null', timeout=10)

    # Run certbot
    cmd = (
        f'certbot certonly --standalone --non-interactive --agree-tos '
        f'--email {shlex.quote(email)} '
        f'-d {shlex.quote(domain)} '
        f'--preferred-challenges http '
        f'--http-01-port 80'
    )
    r = _host_run(cmd, timeout=120)

    if r.returncode != 0:
        stderr = r.stderr.strip()
        stdout = r.stdout.strip()
        msg = stderr or stdout
        # Common errors
        if 'too many' in msg.lower():
            return jsonify({'error': 'Przekroczono limit żądań Let\'s Encrypt. Spróbuj za godzinę.'}), 429
        if 'dns' in msg.lower() or 'resolve' in msg.lower():
            return jsonify({'error': f'Domena {domain} nie wskazuje na ten serwer. Upewnij się, że DNS A/AAAA wskazuje na publiczne IP tego serwera i port 80 jest otwarty.'}), 400
        return jsonify({'error': f'Certbot nie powiódł się:\n{msg[-500:]}'}), 500

    # Verify cert was created
    fullchain, privkey = _cert_paths(domain)
    if not os.path.exists(fullchain):
        return jsonify({'error': 'Certbot zakończył się, ale certyfikat nie został utworzony'}), 500

    # Save config
    cfg = _load_ssl_config()
    cfg['domain'] = domain
    cfg['email'] = email
    cfg['https_port'] = https_port
    _save_ssl_config(cfg)

    return jsonify({
        'ok': True,
        'message': f'Certyfikat dla {domain} uzyskany pomyślnie!',
        'cert': _cert_info(domain),
    })


@settings_bp.route('/ssl/enable', methods=['POST'])
def ssl_enable():
    """Enable or disable HTTPS on EthOS."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403

    data = request.json or {}
    enabled = data.get('enabled', False)

    cfg = _load_ssl_config()

    if enabled:
        # Verify cert exists
        domain = cfg.get('domain', '')
        if not domain:
            return jsonify({'error': 'Najpierw uzyskaj certyfikat'}), 400
        fullchain, privkey = _cert_paths(domain)
        if not os.path.exists(fullchain):
            return jsonify({'error': f'Brak certyfikatu dla {domain}'}), 400

        https_port = int(data.get('https_port', cfg.get('https_port', 443)))
        redirect_http = data.get('redirect_http', cfg.get('redirect_http', True))

        cfg['enabled'] = True
        cfg['https_port'] = https_port
        cfg['redirect_http'] = redirect_http
        _save_ssl_config(cfg)

        # Set env vars for the server
        _write_env_key('SSL_ENABLED', '1')
        _write_env_key('SSL_CERT', fullchain)
        _write_env_key('SSL_KEY', privkey)
        _write_env_key('HTTPS_PORT', str(https_port))
        if redirect_http:
            _write_env_key('SSL_REDIRECT', '1')
        else:
            _write_env_key('SSL_REDIRECT', '0')

        return jsonify({
            'ok': True,
            'message': f'HTTPS włączony na porcie {https_port}. Wymagany restart serwera.',
            'restart_needed': True,
        })
    else:
        cfg['enabled'] = False
        _save_ssl_config(cfg)
        _write_env_key('SSL_ENABLED', '0')

        return jsonify({
            'ok': True,
            'message': 'HTTPS wyłączony. Wymagany restart serwera.',
            'restart_needed': True,
        })


@settings_bp.route('/ssl/renew', methods=['POST'])
def ssl_renew():
    """Manually renew the certificate."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot nie jest zainstalowany'}), 400

    cfg = _load_ssl_config()
    domain = cfg.get('domain', '')
    if not domain:
        return jsonify({'error': 'Brak skonfigurowanej domeny'}), 400

    # Stop anything on port 80
    _host_run('fuser -k 80/tcp 2>/dev/null', timeout=10)

    r = _host_run(
        f'certbot renew --cert-name {shlex.quote(domain)} --standalone --non-interactive',
        timeout=120)

    if r.returncode != 0:
        return jsonify({'error': f'Odnowienie nie powiodło się:\n{r.stderr.strip()[-300:]}'}), 500

    cert = _cert_info(domain)
    return jsonify({
        'ok': True,
        'message': 'Certyfikat odnowiony pomyślnie!',
        'cert': cert,
    })


@settings_bp.route('/ssl/auto-renew', methods=['POST'])
def ssl_auto_renew():
    """Enable or disable automatic certificate renewal."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403

    data = request.json or {}
    enabled = data.get('enabled', True)

    cfg = _load_ssl_config()
    domain = cfg.get('domain', '')

    if enabled:
        # Enable certbot.timer (if available from package) or create a cron job
        r = _host_run('systemctl enable certbot.timer && systemctl start certbot.timer', timeout=15)
        if r.returncode != 0:
            # Fallback: add to cron — renew at 3 AM daily, restart ethos after
            cron_line = '0 3 * * * certbot renew --standalone --pre-hook "fuser -k 80/tcp 2>/dev/null" --quiet'
            _host_run(
                f'(crontab -l 2>/dev/null | grep -v certbot; echo {shlex.quote(cron_line)}) | crontab -',
                timeout=10)
        cfg['auto_renew'] = True
        _save_ssl_config(cfg)
        return jsonify({'ok': True, 'message': 'Automatyczne odnawianie włączone'})
    else:
        _host_run('systemctl disable certbot.timer 2>/dev/null; systemctl stop certbot.timer 2>/dev/null', timeout=10)
        _host_run('(crontab -l 2>/dev/null | grep -v certbot) | crontab -', timeout=10)
        cfg['auto_renew'] = False
        _save_ssl_config(cfg)
        return jsonify({'ok': True, 'message': 'Automatyczne odnawianie wyłączone'})


@settings_bp.route('/ssl/test', methods=['POST'])
def ssl_test():
    """Test if port 80 is reachable from the outside (needed for Let's Encrypt)."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403

    data = request.json or {}
    domain = data.get('domain', '').strip()

    results = {'port_80': False, 'dns_ok': False}

    # Check port 80
    r = _host_run('timeout 3 bash -c "echo ok | nc -l -p 80 &" 2>/dev/null; sleep 0.5; '
                  'curl -s --max-time 3 http://127.0.0.1:80 2>/dev/null; '
                  'fuser -k 80/tcp 2>/dev/null', timeout=10)
    # Simpler: just check if port 80 is not blocked by firewall
    r2 = _host_run('ss -tlnp | grep ":80 "', timeout=5)
    results['port_80_in_use'] = r2.returncode == 0

    # Check if we can bind to port 80
    r3 = _host_run(
        'python3 -c "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); '
        's.bind((\\\"0.0.0.0\\\",80)); s.close(); print(\\\"ok\\\")" 2>&1',
        timeout=5)
    results['port_80'] = 'ok' in r3.stdout

    # DNS check
    if domain:
        import urllib.request
        try:
            my_ip = urllib.request.urlopen('https://api.ipify.org', timeout=5).read().decode().strip()
            results['server_ip'] = my_ip
        except Exception:
            my_ip = None
            results['server_ip'] = None

        r4 = _host_run(f'dig +short {shlex.quote(domain)} A 2>/dev/null || nslookup {shlex.quote(domain)} 2>/dev/null | grep -oP "Address: \\K.*"', timeout=10)
        resolved_ip = r4.stdout.strip().split('\n')[0].strip() if r4.returncode == 0 else ''
        results['dns_ip'] = resolved_ip
        results['dns_ok'] = bool(my_ip and resolved_ip and resolved_ip == my_ip)

    return jsonify(results)


# ═══════════════════════════════════════════════════════════════
#  Domain & Subdomain Management (nginx reverse proxy)
# ═══════════════════════════════════════════════════════════════

_DOMAINS_FILE = _data_path('domains.json')
_NGINX_SITES_DIR = '/etc/nginx/sites-available'
_NGINX_ENABLED_DIR = '/etc/nginx/sites-enabled'
_DOMAIN_RE = re.compile(
    r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$'
)


def _load_domains():
    try:
        with open(_DOMAINS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'domains': []}


def _save_domains(data):
    os.makedirs(os.path.dirname(_DOMAINS_FILE), exist_ok=True)
    with open(_DOMAINS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def _nginx_installed():
    r = _host_run('command -v nginx', timeout=5)
    return r.returncode == 0


def _nginx_reload():
    """Test and reload nginx."""
    r = _host_run('nginx -t 2>&1', timeout=10)
    if r.returncode != 0:
        return False, r.stderr.strip() or r.stdout.strip()
    _host_run('systemctl reload nginx', timeout=10)
    return True, ''


def _nginx_conf_name(domain_id):
    return f'ethos-{domain_id}'


def _generate_nginx_conf(entry):
    """Generate nginx server block for a domain entry."""
    domain = entry['domain']
    target = entry.get('target', '').strip()
    ssl = entry.get('ssl', False)
    force_https = entry.get('force_https', False)
    custom_config = entry.get('custom_config', '').strip()
    websocket = entry.get('websocket', False)

    if not target:
        target = f'127.0.0.1:{int(os.environ.get("PORT", "9000"))}'

    # Ensure target has http scheme
    if not target.startswith('http://') and not target.startswith('https://'):
        target = 'http://' + target

    lines = []

    # HTTP → HTTPS redirect block (if SSL + force)
    if ssl and force_https:
        lines.append('server {')
        lines.append('    listen 80;')
        lines.append('    listen [::]:80;')
        lines.append(f'    server_name {domain};')
        lines.append(f'    return 301 https://$host$request_uri;')
        lines.append('}')
        lines.append('')

    # Main server block
    lines.append('server {')
    if ssl:
        cert_dir = f'/etc/letsencrypt/live/{domain}'
        lines.append('    listen 443 ssl http2;')
        lines.append('    listen [::]:443 ssl http2;')
        lines.append(f'    server_name {domain};')
        lines.append('')
        lines.append(f'    ssl_certificate {cert_dir}/fullchain.pem;')
        lines.append(f'    ssl_certificate_key {cert_dir}/privkey.pem;')
        lines.append('    ssl_protocols TLSv1.2 TLSv1.3;')
        lines.append('    ssl_ciphers HIGH:!aNULL:!MD5;')
        lines.append('    ssl_prefer_server_ciphers on;')
        if not force_https:
            # Also listen on 80
            lines.append('')
            lines.append('    listen 80;')
            lines.append('    listen [::]:80;')
    else:
        lines.append('    listen 80;')
        lines.append('    listen [::]:80;')
        lines.append(f'    server_name {domain};')

    lines.append('')
    lines.append('    # Proxy settings')
    lines.append('    location / {')
    lines.append(f'        proxy_pass {target};')
    lines.append('        proxy_set_header Host $host;')
    lines.append('        proxy_set_header X-Real-IP $remote_addr;')
    lines.append('        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;')
    lines.append('        proxy_set_header X-Forwarded-Proto $scheme;')
    lines.append('        proxy_http_version 1.1;')
    if websocket:
        lines.append('        proxy_set_header Upgrade $http_upgrade;')
        lines.append('        proxy_set_header Connection "upgrade";')
    lines.append('        proxy_read_timeout 86400s;')
    lines.append('        proxy_send_timeout 86400s;')
    lines.append('    }')

    # Custom config
    if custom_config:
        lines.append('')
        lines.append('    # Custom config')
        for cl in custom_config.split('\n'):
            cl = cl.rstrip()
            if cl:
                lines.append('    ' + cl)

    lines.append('}')
    return '\n'.join(lines) + '\n'


def _write_nginx_conf(entry):
    """Write nginx config for a domain and reload."""
    conf_name = _nginx_conf_name(entry['id'])
    conf_content = _generate_nginx_conf(entry)
    avail = os.path.join(_NGINX_SITES_DIR, conf_name)
    enabled = os.path.join(_NGINX_ENABLED_DIR, conf_name)

    # Write config
    _host_run(f'mkdir -p {shlex.quote(_NGINX_SITES_DIR)} {shlex.quote(_NGINX_ENABLED_DIR)}', timeout=5)
    _host_run(f'cat > {shlex.quote(avail)} << \'ETHOSEOF\'\n{conf_content}\nETHOSEOF', timeout=5)

    if entry.get('enabled', True):
        _host_run(f'ln -sf {shlex.quote(avail)} {shlex.quote(enabled)}', timeout=5)
    else:
        _host_run(f'rm -f {shlex.quote(enabled)}', timeout=5)

    return _nginx_reload()


def _remove_nginx_conf(entry_id):
    """Remove nginx config for a domain."""
    conf_name = _nginx_conf_name(entry_id)
    avail = os.path.join(_NGINX_SITES_DIR, conf_name)
    enabled = os.path.join(_NGINX_ENABLED_DIR, conf_name)
    _host_run(f'rm -f {shlex.quote(avail)} {shlex.quote(enabled)}', timeout=5)
    _nginx_reload()


def _scan_local_services():
    """Scan localhost for common running services and return them."""
    services = []
    # Well-known local ports
    known = {
        80: 'HTTP', 443: 'HTTPS', 631: 'CUPS', 1883: 'MQTT',
        3000: 'Grafana / Dev', 5000: 'Flask / Docker Registry',
        5432: 'PostgreSQL', 5672: 'RabbitMQ', 6379: 'Redis',
        7878: 'Radarr', 8080: 'HTTP Alt', 8081: 'HTTP Alt',
        8086: 'InfluxDB', 8096: 'Jellyfin', 8123: 'Home Assistant',
        8686: 'Lidarr', 8920: 'Jellyfin HTTPS', 8989: 'Sonarr',
        9000: 'EthOS', 9090: 'Prometheus', 9117: 'Jackett',
        19006: 'Dev', 32400: 'Plex', 51821: 'WireGuard',
    }
    r = _host_run("ss -tlnp 2>/dev/null | awk 'NR>1 {print $4}' | sed 's/.*://'", timeout=5)
    if r.returncode == 0:
        seen = set()
        for line in r.stdout.strip().split('\n'):
            port_str = line.strip()
            if not port_str or not port_str.isdigit():
                continue
            port = int(port_str)
            if port in seen or port < 80:
                continue
            seen.add(port)
            name = known.get(port, '')
            services.append({'port': port, 'name': name, 'target': f'127.0.0.1:{port}'})
        services.sort(key=lambda x: x['port'])
    return services


@settings_bp.route('/domains', methods=['GET'])
def list_domains():
    """List all configured domains."""
    data = _load_domains()
    return jsonify({
        'domains': data.get('domains', []),
        'nginx_installed': _nginx_installed(),
    })


@settings_bp.route('/domains/install-nginx', methods=['POST'])
def install_nginx():
    """Install nginx."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403
    if _nginx_installed():
        return jsonify({'ok': True, 'message': 'Nginx jest już zainstalowany'})

    r = _apt_install('nginx', timeout=120)
    if r.returncode != 0:
        return jsonify({'error': f'Instalacja nie powiodła się: {r.stderr.strip()[-200:]}'}), 500

    # Remove default site to avoid port 80 conflict
    _host_run('rm -f /etc/nginx/sites-enabled/default', timeout=5)

    # Write a stub nginx.conf that doesn't listen on any port by default
    # Only our per-domain server blocks will create listeners
    _host_run('mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled', timeout=5)

    # Enable and start nginx (with no default site it won't bind to 80)
    _host_run('systemctl enable nginx 2>/dev/null', timeout=10)
    r2 = _host_run('systemctl start nginx 2>&1', timeout=15)
    if r2.returncode != 0:
        # If it still fails, try a restart (maybe the config is now ok)
        _host_run('systemctl restart nginx 2>/dev/null', timeout=15)

    # Verify
    r3 = _host_run('systemctl is-active nginx', timeout=5)
    if r3.returncode != 0 or 'active' not in r3.stdout.strip():
        # Last resort: check if conf is ok but something else blocks
        return jsonify({
            'ok': True,
            'message': 'Nginx zainstalowany, ale nie udało się uruchomić (port 80 może być zajęty). '
                       'Uruchomi się automatycznie po dodaniu pierwszej domeny.',
        })

    return jsonify({'ok': True, 'message': 'Nginx zainstalowany i uruchomiony'})


@settings_bp.route('/domains/services', methods=['GET'])
def local_services():
    """Scan for running services on localhost."""
    return jsonify({'services': _scan_local_services()})


@settings_bp.route('/domains', methods=['POST'])
def add_domain():
    """Add a new domain/subdomain proxy entry."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403
    if not _nginx_installed():
        return jsonify({'error': 'Nginx nie jest zainstalowany'}), 400

    d = request.json or {}
    domain = d.get('domain', '').strip().lower()
    target = d.get('target', '').strip()
    ssl = d.get('ssl', False)
    force_https = d.get('force_https', False)
    websocket = d.get('websocket', False)
    custom_config = d.get('custom_config', '').strip()
    description = d.get('description', '').strip()

    if not domain:
        return jsonify({'error': 'Domena jest wymagana'}), 400
    if not _DOMAIN_RE.match(domain):
        return jsonify({'error': 'Nieprawidłowa domena (np. sub.example.com)'}), 400
    if not target:
        return jsonify({'error': 'Cel (target) jest wymagany — np. 127.0.0.1:8080'}), 400

    data = _load_domains()
    # Check uniqueness
    for existing in data.get('domains', []):
        if existing['domain'] == domain:
            return jsonify({'error': f'Domena {domain} jest już skonfigurowana'}), 409

    # If SSL requested, check cert
    if ssl:
        fullchain, _ = _cert_paths(domain)
        if not os.path.exists(fullchain):
            return jsonify({'error': f'Brak certyfikatu SSL dla {domain}. Najpierw uzyskaj certyfikat w zakładce SSL.'}), 400

    entry = {
        'id': uuid.uuid4().hex[:12],
        'domain': domain,
        'target': target,
        'ssl': ssl,
        'force_https': force_https,
        'websocket': websocket,
        'custom_config': custom_config,
        'description': description,
        'enabled': True,
        'created': datetime.utcnow().isoformat(),
    }

    # Write nginx config
    ok, err = _write_nginx_conf(entry)
    if not ok:
        return jsonify({'error': f'Błąd konfiguracji nginx:\n{err[-300:]}'}), 500

    data.setdefault('domains', []).append(entry)
    _save_domains(data)

    return jsonify({'ok': True, 'domain': entry})


@settings_bp.route('/domains/<domain_id>', methods=['PUT'])
def update_domain(domain_id):
    """Update an existing domain entry."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403

    data = _load_domains()
    entry = None
    for e in data.get('domains', []):
        if e['id'] == domain_id:
            entry = e
            break
    if not entry:
        return jsonify({'error': 'Nie znaleziono domeny'}), 404

    d = request.json or {}
    for k in ('target', 'ssl', 'force_https', 'websocket', 'custom_config', 'description', 'enabled'):
        if k in d:
            entry[k] = d[k]

    # Allow changing domain itself
    if 'domain' in d:
        new_domain = d['domain'].strip().lower()
        if new_domain and new_domain != entry['domain']:
            if not _DOMAIN_RE.match(new_domain):
                return jsonify({'error': 'Nieprawidłowa domena'}), 400
            for other in data.get('domains', []):
                if other['id'] != domain_id and other['domain'] == new_domain:
                    return jsonify({'error': f'Domena {new_domain} jest już skonfigurowana'}), 409
            # Remove old conf
            _remove_nginx_conf(domain_id)
            entry['domain'] = new_domain

    if entry.get('ssl'):
        fullchain, _ = _cert_paths(entry['domain'])
        if not os.path.exists(fullchain):
            return jsonify({'error': f'Brak certyfikatu SSL dla {entry["domain"]}'}), 400

    entry['updated'] = datetime.utcnow().isoformat()

    ok, err = _write_nginx_conf(entry)
    if not ok:
        return jsonify({'error': f'Błąd konfiguracji nginx:\n{err[-300:]}'}), 500

    _save_domains(data)
    return jsonify({'ok': True, 'domain': entry})


@settings_bp.route('/domains/<domain_id>', methods=['DELETE'])
def delete_domain(domain_id):
    """Delete a domain entry and its nginx config."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403

    data = _load_domains()
    domains = data.get('domains', [])
    found = None
    for i, e in enumerate(domains):
        if e['id'] == domain_id:
            found = i
            break
    if found is None:
        return jsonify({'error': 'Nie znaleziono domeny'}), 404

    entry = domains.pop(found)
    _remove_nginx_conf(domain_id)
    _save_domains(data)

    return jsonify({'ok': True, 'message': f'Domena {entry["domain"]} usunięta'})


@settings_bp.route('/domains/<domain_id>/toggle', methods=['POST'])
def toggle_domain(domain_id):
    """Enable or disable a domain."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403

    data = _load_domains()
    entry = None
    for e in data.get('domains', []):
        if e['id'] == domain_id:
            entry = e
            break
    if not entry:
        return jsonify({'error': 'Nie znaleziono domeny'}), 404

    entry['enabled'] = not entry.get('enabled', True)
    ok, err = _write_nginx_conf(entry)
    if not ok:
        return jsonify({'error': f'Błąd nginx: {err[-200:]}'}), 500
    _save_domains(data)

    status_str = 'włączona' if entry['enabled'] else 'wyłączona'
    return jsonify({'ok': True, 'enabled': entry['enabled'], 'message': f'Domena {entry["domain"]} {status_str}'})


@settings_bp.route('/domains/<domain_id>/ssl', methods=['POST'])
def domain_ssl(domain_id):
    """Obtain a Let's Encrypt certificate for this domain."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot nie jest zainstalowany. Zainstaluj go w zakładce SSL.'}), 400

    data = _load_domains()
    entry = None
    for e in data.get('domains', []):
        if e['id'] == domain_id:
            entry = e
            break
    if not entry:
        return jsonify({'error': 'Nie znaleziono domeny'}), 404

    domain = entry['domain']
    d = request.json or {}
    email = d.get('email', '').strip()

    # Try to get email from SSL config
    if not email:
        ssl_cfg = _load_ssl_config()
        email = ssl_cfg.get('email', '')
    if not email:
        return jsonify({'error': 'Email jest wymagany (podaj go w zakładce SSL)'}), 400

    # Temporarily disable this domain's nginx conf to free port 80
    conf_name = _nginx_conf_name(entry['id'])
    enabled_path = os.path.join(_NGINX_ENABLED_DIR, conf_name)
    was_enabled = os.path.exists(enabled_path)

    # Use nginx plugin if nginx is running, otherwise standalone
    # Try standalone first (stop nginx temporarily)
    _host_run('systemctl stop nginx 2>/dev/null; fuser -k 80/tcp 2>/dev/null', timeout=10)

    cmd = (
        f'certbot certonly --standalone --non-interactive --agree-tos '
        f'--email {shlex.quote(email)} '
        f'-d {shlex.quote(domain)} '
        f'--preferred-challenges http '
        f'--http-01-port 80'
    )
    r = _host_run(cmd, timeout=120)

    # Restart nginx
    _host_run('systemctl start nginx 2>/dev/null', timeout=10)

    if r.returncode != 0:
        msg = (r.stderr.strip() or r.stdout.strip())[-400:]
        return jsonify({'error': f'Certbot nie powiódł się:\n{msg}'}), 500

    fullchain, _ = _cert_paths(domain)
    if not os.path.exists(fullchain):
        return jsonify({'error': 'Certbot zakończył się, lecz certyfikat nie istnieje'}), 500

    # Enable SSL on this entry
    entry['ssl'] = True
    entry['force_https'] = True
    _write_nginx_conf(entry)
    _save_domains(data)

    return jsonify({
        'ok': True,
        'message': f'Certyfikat SSL dla {domain} uzyskany!',
        'cert': _cert_info(domain),
    })


@settings_bp.route('/domains/<domain_id>/preview', methods=['GET'])
def domain_preview(domain_id):
    """Preview the nginx config that would be generated."""
    data = _load_domains()
    entry = None
    for e in data.get('domains', []):
        if e['id'] == domain_id:
            entry = e
            break
    if not entry:
        return jsonify({'error': 'Nie znaleziono domeny'}), 404
    return jsonify({'config': _generate_nginx_conf(entry)})


@settings_bp.route('/domains/nginx-status', methods=['GET'])
def nginx_status():
    """Get nginx status info."""
    if not _nginx_installed():
        return jsonify({'installed': False})

    r = _host_run('systemctl is-active nginx', timeout=5)
    active = r.returncode == 0 and 'active' in r.stdout.strip()
    r2 = _host_run('nginx -t 2>&1', timeout=10)
    config_ok = r2.returncode == 0

    return jsonify({
        'installed': True,
        'active': active,
        'config_ok': config_ok,
        'config_test': r2.stderr.strip() or r2.stdout.strip(),
    })


# ══════════════════════════════════════════════════════════════════
#  SSH Key Management — MIGRATED to ssh_manager.py (/api/ssh/*)
#  These thin shims keep the old /settings/ssh-keys/* URLs working
#  for any consumers that haven't been updated yet.
# ══════════════════════════════════════════════════════════════════

from blueprints.ssh_manager import (
    list_ssh_keys_data as _ssh_list_keys_data,
    _safe_keys as _ssh_safe_keys,
    SSH_KEYS_DIR,
)

import logging as _logging
_ssh_logger = _logging.getLogger(__name__)


@settings_bp.route('/ssh-keys', methods=['GET'])
def list_ssh_keys():
    try:
        return jsonify({'keys': _ssh_safe_keys(_ssh_list_keys_data())})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/ssh-keys/generate', methods=['POST'])
def generate_ssh_key():
    from blueprints.ssh_manager import api_generate_key
    return api_generate_key()


@settings_bp.route('/ssh-keys/<key_name>', methods=['DELETE'])
def delete_ssh_key(key_name):
    from blueprints.ssh_manager import api_delete_key
    return api_delete_key(key_name)


@settings_bp.route('/ssh-keys/<key_name>/public', methods=['GET'])
def get_public_key(key_name):
    from blueprints.ssh_manager import api_get_public
    return api_get_public(key_name)


@settings_bp.route('/ssh-keys/<key_name>/deploy', methods=['POST'])
def deploy_ssh_key(key_name):
    from blueprints.ssh_manager import api_deploy_key
    return api_deploy_key(key_name)


@settings_bp.route('/ssh-keys/test-auth', methods=['POST'])
def test_key_auth():
    from blueprints.ssh_manager import api_test_auth
    return api_test_auth()


@settings_bp.route('/known-hosts', methods=['GET'])
def list_known_hosts():
    from blueprints.ssh_manager import api_list_known_hosts
    return api_list_known_hosts()


# ═══════════════════════════════════════════════════════════════
#  Factory Reset
# ═══════════════════════════════════════════════════════════════

_FACTORY_RESET_LOCK = False

@settings_bp.route('/factory-reset', methods=['POST'])
def factory_reset():
    """Reset EthOS to factory state — removes all user data, configs, Docker
    containers and brings back the initial setup wizard."""
    global _FACTORY_RESET_LOCK

    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator'}), 403

    if _FACTORY_RESET_LOCK:
        return jsonify({'error': 'Reset jest już w toku'}), 409

    data = request.json or {}
    confirm_text = (data.get('confirm') or '').strip()
    if confirm_text != 'RESET':
        return jsonify({'error': 'Wpisz RESET aby potwierdzić'}), 400

    keep_docker = data.get('keep_docker', False)

    _FACTORY_RESET_LOCK = True
    steps = []

    try:
        data_dir = _data_path()
        ethos_root = ETHOS_ROOT

        # ── 1. Stop and remove app services & apt dependencies ──
        steps.append('packages')
        # Stop all app services first
        _host_run('systemctl stop smbd nmbd 2>/dev/null || true', timeout=15)
        _host_run('systemctl disable smbd nmbd 2>/dev/null || true', timeout=10)
        _host_run('systemctl stop nfs-server 2>/dev/null || true', timeout=15)
        _host_run('systemctl disable nfs-server 2>/dev/null || true', timeout=10)
        _host_run('systemctl stop minidlna 2>/dev/null || true', timeout=15)
        _host_run('systemctl disable minidlna 2>/dev/null || true', timeout=10)
        _host_run('systemctl stop lighttpd 2>/dev/null || true', timeout=15)
        _host_run('systemctl disable lighttpd 2>/dev/null || true', timeout=10)
        _host_run('systemctl stop vsftpd 2>/dev/null || true', timeout=15)
        _host_run('systemctl disable vsftpd 2>/dev/null || true', timeout=10)
        _host_run('systemctl stop cups 2>/dev/null || true', timeout=15)
        _host_run('systemctl disable cups 2>/dev/null || true', timeout=10)

        # Remove apt packages installed by EthOS apps
        # Protect core first-boot/setup dependencies from global autoremove.
        _PROTECT_PKGS = [
            'network-manager', 'wpasupplicant', 'rfkill',
            'parted', 'e2fsprogs', 'dosfstools', 'util-linux', 'udev',
            'cryptsetup',
        ]
        _host_run(
            f"apt-mark manual {' '.join(_PROTECT_PKGS)} 2>/dev/null || true",
            timeout=30)

        _APP_APT_PKGS = [
            'samba', 'samba-common-bin', 'smbclient',
            'nfs-kernel-server',
            'minidlna',
            'lighttpd',
            'vsftpd',
            'cups',
            'libreoffice-writer',
            'smartmontools',
            'qemu-system-x86', 'qemu-utils', 'ovmf',
        ]
        pkg_str = ' '.join(_APP_APT_PKGS)
        _host_run(
            f'DEBIAN_FRONTEND=noninteractive apt-get remove --purge -y {pkg_str} 2>/dev/null || true',
            timeout=300)
        _host_run(
            'DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y 2>/dev/null || true',
            timeout=300)

        # Remove Docker if not keeping it
        if not keep_docker:
            steps.append('docker')
            try:
                # Stop all running containers
                _host_run('docker stop $(docker ps -q) 2>/dev/null || true', timeout=120)
                # Remove all containers
                _host_run('docker rm $(docker ps -aq) 2>/dev/null || true', timeout=60)
                # Prune networks, volumes
                _host_run('docker network prune -f 2>/dev/null || true', timeout=30)
                _host_run('docker volume prune -f 2>/dev/null || true', timeout=30)
                # Remove Docker itself
                _host_run(
                    'DEBIAN_FRONTEND=noninteractive apt-get remove --purge -y '
                    'docker-ce docker-ce-cli containerd.io docker-compose-plugin 2>/dev/null || true',
                    timeout=300)
                _host_run(
                    'DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y 2>/dev/null || true',
                    timeout=300)
            except Exception:
                pass

        # ── 2. Remove all data files ──
        steps.append('data')
        # Files to preserve (critical for system operation)
        _KEEP = {'version.json'}  # needed by backend to start

        # Remove everything in data directory
        if os.path.isdir(data_dir):
            for entry in os.listdir(data_dir):
                if entry in _KEEP:
                    continue
                full = os.path.join(data_dir, entry)
                try:
                    if os.path.isdir(full):
                        import shutil
                        shutil.rmtree(full, ignore_errors=True)
                    else:
                        os.remove(full)
                except Exception:
                    pass

        # ── 3. Remove backend per-user data ──
        backend_data = _app_path('data')
        if os.path.isdir(backend_data):
            import shutil
            shutil.rmtree(backend_data, ignore_errors=True)
            os.makedirs(backend_data, exist_ok=True)

        # ── 4. Remove logs ──
        steps.append('logs')
        log_dir = os.path.join(ethos_root, 'logs')
        if os.path.isdir(log_dir):
            for f in os.listdir(log_dir):
                try:
                    fp = os.path.join(log_dir, f)
                    if os.path.isfile(fp):
                        os.remove(fp)
                except Exception:
                    pass

        # ── 5. Remove uploads, thumbnails, cache ──
        steps.append('cache')
        for subdir in ['uploads', '.thumb_cache', 'backups']:
            p = os.path.join(ethos_root, subdir)
            if os.path.isdir(p):
                import shutil
                shutil.rmtree(p, ignore_errors=True)
                os.makedirs(p, exist_ok=True)

        # ── 6. Reset ethos.env to defaults ──
        steps.append('env')
        env_file = _env_file()
        try:
            with open(env_file, 'w') as f:
                f.write('NAS_NAME=EthOS\n')
                f.write('PORT=9000\n')
                f.write(f'ETHOS_ROOT={ethos_root}\n')
        except Exception:
            pass

        # ── 7. Remove .installed marker if present ──
        installed_marker = os.path.join(ethos_root, '.installed')
        if os.path.exists(installed_marker):
            try:
                os.remove(installed_marker)
            except Exception:
                pass

        # ── 8. Remove service config files left by apps ──
        steps.append('service_configs')
        for cfg in ['/etc/samba/smb.conf', '/etc/exports', '/etc/minidlna.conf',
                     '/etc/vsftpd.conf']:
            try:
                if os.path.isfile(cfg):
                    os.remove(cfg)
            except Exception:
                pass
        _host_run('rm -f /etc/lighttpd/conf-enabled/90-webdav.conf 2>/dev/null || true')
        _host_run('rm -f /etc/lighttpd/webdav_*.htpasswd 2>/dev/null || true')

        # ── 9. Recreate required directories ──
        for d in ['data', 'logs', 'uploads', 'backups', '.thumb_cache']:
            p = os.path.join(ethos_root, d)
            try:
                if os.path.islink(p):
                    os.unlink(p)
                elif os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
            os.makedirs(p, exist_ok=True)

        steps.append('done')

        # ── 9. Schedule restart ──
        import gevent
        def _do_restart():
            try:
                subprocess.run(['systemctl', 'restart', 'ethos'], timeout=60)
            except Exception:
                pass
        gevent.spawn_later(2, _do_restart)

        return jsonify({
            'ok': True,
            'message': 'Factory reset zakończony. Serwer uruchomi się ponownie z kreatorem konfiguracji.',
            'steps': steps,
        })

    except Exception as e:
        return jsonify({'error': f'Błąd resetu: {e}'}), 500
    finally:
        _FACTORY_RESET_LOCK = False


@settings_bp.route('/known-hosts/lookup', methods=['POST'])
def lookup_known_host():
    from blueprints.ssh_manager import api_lookup_host
    return api_lookup_host()


@settings_bp.route('/known-hosts/remove', methods=['POST'])
def remove_known_host():
    from blueprints.ssh_manager import api_remove_host
    return api_remove_host()


@settings_bp.route('/known-hosts/remove-line', methods=['POST'])
def remove_known_host_line():
    from blueprints.ssh_manager import api_remove_line
    return api_remove_line()


# ── Fail2Ban Routes ──

@settings_bp.route('/fail2ban/status')
def fail2ban_status():
    """Get status of Fail2Ban jails."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator może przeglądać status Fail2Ban'}), 403
    status = {}

    # Check if fail2ban is running
    r = _host_run('sudo /opt/ethos/tools/ethos-system-helper.sh systemctl is-active fail2ban')
    if r.returncode != 0:
        return jsonify({'running': False, 'jails': {}})

    # Get list of jails
    r = _host_run('sudo /opt/ethos/tools/ethos-system-helper.sh fail2ban-client status')
    if r.returncode != 0:
        return jsonify({'running': False, 'error': 'Failed to query fail2ban-client', 'jails': {}})

    # Parse output: "Jail list: sshd, samba, ethos-web"
    jail_list_match = re.search(r'Jail list:\s+(.*)', r.stdout)
    if jail_list_match:
        jail_names = [j.strip() for j in jail_list_match.group(1).split(',') if j.strip()]
        for jail in jail_names:
             r2 = _host_run(f'sudo /opt/ethos/tools/ethos-system-helper.sh fail2ban-client status {jail}')
             # Parse banned IPs
             # "Banned IP list: 1.2.3.4 5.6.7.8"
             banned_match = re.search(r'Banned IP list:\s+(.*)', r2.stdout)
             banned_ips = banned_match.group(1).split() if banned_match and banned_match.group(1).strip() else []
             status[jail] = banned_ips

    return jsonify({'running': True, 'jails': status})


@settings_bp.route('/fail2ban/unban', methods=['POST'])
def fail2ban_unban():
    """Unban an IP from a jail."""
    if g.role != 'admin':
        return jsonify({'error': 'Tylko administrator może odblokowywać adresy IP'}), 403
    data = request.json or {}
    jail = data.get('jail')
    ip = data.get('ip')

    if not jail or not ip:
         return jsonify({'error': 'Missing jail or ip'}), 400

    safe_jail = shlex.quote(jail)
    safe_ip = shlex.quote(ip)

    r = _host_run(f'sudo /opt/ethos/tools/ethos-system-helper.sh fail2ban-client set {safe_jail} unbanip {safe_ip}')
    if r.returncode == 0:
        return jsonify({'success': True})
    else:
        return jsonify({'error': r.stderr.strip() or r.stdout.strip()}), 500

# ── Power / Performance ──

@settings_bp.route('/sysctl/restart', methods=['POST'])
def restart_sysctl():
    """Reload sysctl settings."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin required'}), 403

    try:
        # Reload sysctl settings from all system files
        r = _host_run('sudo /opt/ethos/tools/ethos-system-helper.sh sysctl --system', timeout=30)
        if r.returncode == 0:
            return jsonify({'ok': True, 'message': 'Ustawienia kernela (sysctl) przeładowane pomyślnie.'})
        else:
            return jsonify({'ok': False, 'error': r.stderr.strip()}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@settings_bp.route('/sysctl', methods=['GET'])
def get_sysctl_params():
    """Return key NAS tuning parameters."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin required'}), 403

    params = [
        'vm.swappiness',
        'vm.dirty_ratio',
        'vm.dirty_background_ratio',
        'vm.vfs_cache_pressure',
        'net.core.rmem_max',
        'net.core.wmem_max',
        'net.ipv4.tcp_rmem',
        'vm.min_free_kbytes'
    ]

    result = {}
    for p in params:
        try:
            r = _host_run(f'sysctl -n {p}', timeout=5)
            if r.returncode == 0:
                result[p] = r.stdout.strip()
        except Exception as e:
            pass

    return jsonify(result)
