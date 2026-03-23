"""
EthOS — Domains & SSL Manager Blueprint

Unified management of:
  - SSL certificates (Let's Encrypt / Certbot)
  - Domain / subdomain reverse proxy (nginx)

Migrated from settings.py to a dedicated app.
DDNS stays in its own blueprint (ddns.py) — only the frontend unifies all three.
"""

import os
import re
import json
import shlex
import uuid
import tempfile
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify, g
from datetime import datetime, timedelta

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, data_path as _data_path, apt_install as _apt_install

domains_mgr_bp = Blueprint('domains_mgr', __name__, url_prefix='/api/domains-mgr')

# Application-level lock for domain read-modify-write cycles
_domains_lock = threading.Lock()


# ── helpers shared with settings.py (env read/write) ──

def _read_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'ethos.env')
    env_path = os.path.normpath(env_path)
    env = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    return env


def _write_env_key(key, value):
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'ethos.env')
    env_path = os.path.normpath(env_path)
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f'{key}='):
                    lines.append(f'{key}={value}\n')
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f'{key}={value}\n')
    with open(env_path, 'w') as f:
        f.writelines(lines)


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
    base = os.path.join(_CERT_DIR, domain)
    return os.path.join(base, 'fullchain.pem'), os.path.join(base, 'privkey.pem')


def _cert_info(domain):
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
    r = _host_run('systemctl is-active certbot.timer 2>/dev/null', timeout=5)
    if r.returncode == 0 and 'active' in r.stdout.strip():
        return 'systemd'
    r2 = _host_run('crontab -l 2>/dev/null | grep -q certbot', timeout=5)
    if r2.returncode == 0:
        return 'cron'
    return None


def _scan_all_certs():
    """Scan /etc/letsencrypt/live/ for all certificate directories."""
    certs = []
    if not os.path.isdir(_CERT_DIR):
        return certs
    try:
        for name in sorted(os.listdir(_CERT_DIR)):
            if name.startswith('.') or name == 'README':
                continue
            fullchain = os.path.join(_CERT_DIR, name, 'fullchain.pem')
            if os.path.exists(fullchain):
                info = _cert_info(name)
                if info:
                    certs.append(info)
    except PermissionError:
        r = _host_run(f'ls -1 {shlex.quote(_CERT_DIR)}', timeout=5)
        if r.returncode == 0:
            for name in r.stdout.strip().split('\n'):
                name = name.strip()
                if not name or name == 'README':
                    continue
                info = _cert_info(name)
                if info:
                    certs.append(info)
    return certs


@domains_mgr_bp.route('/ssl/status', methods=['GET'])
def ssl_status():
    cfg = _load_ssl_config()
    installed = _certbot_installed()

    # Scan ALL certificates
    all_certs = _scan_all_certs()

    # Cross-reference with configured domains
    domains_data = _load_domains()
    domain_map = {d['domain']: d for d in domains_data.get('domains', [])}
    for c in all_certs:
        dom = c.get('domain', '')
        if dom in domain_map:
            c['has_domain'] = True
            c['domain_id'] = domain_map[dom].get('id', '')
            c['domain_ssl_enabled'] = domain_map[dom].get('ssl', False)

    result = {
        'certbot_installed': installed,
        'config': cfg,
        'certs': all_certs,
        'cert': None,  # kept for backwards compat
        'renewal': None,
    }
    if cfg.get('domain'):
        cert = _cert_info(cfg['domain'])
        if cert:
            result['cert'] = cert
    elif all_certs:
        result['cert'] = all_certs[0]
    result['renewal'] = _renewal_timer_exists()
    env = _read_env()
    result['ssl_active'] = env.get('SSL_ENABLED', '') == '1'
    result['https_port'] = int(env.get('HTTPS_PORT', cfg.get('https_port', 443)))
    return jsonify(result)


@domains_mgr_bp.route('/ssl/install-certbot', methods=['POST'])
def install_certbot():
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    if _certbot_installed():
        return jsonify({'status': 'ok', 'installed': True})
    r = _apt_install('certbot', timeout=120)
    if r.returncode != 0:
        return jsonify({'error': f'Installation failed: {r.stderr.strip()[-200:]}'}), 500
    return jsonify({'status': 'ok'})


@domains_mgr_bp.route('/ssl/obtain', methods=['POST'])
def ssl_obtain():
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot is not installed'}), 400

    data = request.json or {}
    domain = data.get('domain', '').strip().lower()
    email = data.get('email', '').strip()
    try:
        https_port = int(data.get('https_port', 443))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid HTTPS port'}), 400

    if not domain:
        return jsonify({'error': 'Domain is required'}), 400
    if not re.match(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$', domain):
        return jsonify({'error': 'Invalid domain'}), 400
    if not email or '@' not in email:
        return jsonify({'error': 'A valid email is required'}), 400

    ok, err = _certbot_obtain(domain, email)
    if not ok:
        code = 429 if 'limit' in err.lower() else (400 if 'does not point' in err else 500)
        return jsonify({'error': err}), code

    fullchain, privkey = _cert_paths(domain)

    cfg = _load_ssl_config()
    cfg['domain'] = cfg.get('domain') or domain   # keep first domain as primary
    cfg['email'] = email
    cfg['https_port'] = https_port
    _save_ssl_config(cfg)

    return jsonify({
        'ok': True,
        'message': f'Certificate for {domain} obtained successfully!',
        'cert': _cert_info(domain),
    })


@domains_mgr_bp.route('/ssl/enable', methods=['POST'])
def ssl_enable():
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    data = request.json or {}
    enabled = data.get('enabled', False)
    cfg = _load_ssl_config()

    if enabled:
        domain = cfg.get('domain', '')
        if not domain:
            return jsonify({'error': 'Obtain a certificate first'}), 400
        fullchain, privkey = _cert_paths(domain)
        if not os.path.exists(fullchain):
            return jsonify({'error': f'No certificate for {domain}'}), 400

        https_port = int(data.get('https_port', cfg.get('https_port', 443)))
        redirect_http = data.get('redirect_http', cfg.get('redirect_http', True))

        cfg['enabled'] = True
        cfg['https_port'] = https_port
        cfg['redirect_http'] = redirect_http
        _save_ssl_config(cfg)

        _write_env_key('SSL_ENABLED', '1')
        _write_env_key('SSL_CERT', fullchain)
        _write_env_key('SSL_KEY', privkey)
        _write_env_key('HTTPS_PORT', str(https_port))
        _write_env_key('SSL_REDIRECT', '1' if redirect_http else '0')

        return jsonify({
            'ok': True,
            'message': f'HTTPS enabled on port {https_port}. Server restart required.',
            'restart_needed': True,
        })
    else:
        cfg['enabled'] = False
        _save_ssl_config(cfg)
        _write_env_key('SSL_ENABLED', '0')
        return jsonify({
            'ok': True,
            'message': 'HTTPS disabled. Server restart required.',
            'restart_needed': True,
        })


@domains_mgr_bp.route('/ssl/renew', methods=['POST'])
def ssl_renew():
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot is not installed'}), 400

    data = request.json or {}
    domain = data.get('domain', '').strip()
    if not domain:
        cfg = _load_ssl_config()
        domain = cfg.get('domain', '')
    if not domain:
        return jsonify({'error': 'No domain configured'}), 400

    _ensure_acme_webroot()
    r = _host_run(
        f'certbot renew --cert-name {shlex.quote(domain)} '
        f'--webroot -w {shlex.quote(_ACME_WEBROOT)} --non-interactive',
        timeout=120)

    if r.returncode != 0:
        return jsonify({'error': f'Renewal failed:\n{(r.stderr.strip() or r.stdout.strip())[-300:]}'}), 500

    _nginx_reload()  # pick up renewed cert

    return jsonify({
        'ok': True,
        'message': 'Certificate renewed successfully!',
        'cert': _cert_info(domain),
    })


@domains_mgr_bp.route('/ssl/auto-renew', methods=['POST'])
def ssl_auto_renew():
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    data = request.json or {}
    enabled = data.get('enabled', True)
    cfg = _load_ssl_config()

    # Webroot renewal: nginx stays running, deploy hook reloads after success
    cron_line = f'0 3 * * * certbot renew --webroot -w {_ACME_WEBROOT} --deploy-hook "nginx -t && systemctl reload nginx" --quiet 2>/dev/null'

    if enabled:
        # Try systemd timer first, fallback to cron
        r = _host_run('systemctl enable certbot.timer && systemctl start certbot.timer 2>/dev/null', timeout=15)
        if r.returncode != 0:
            _host_run(
                f'(crontab -l 2>/dev/null | grep -v certbot; echo {shlex.quote(cron_line)}) | crontab -',
                timeout=10)
        else:
            # Also set deploy hook so nginx reloads after renewal
            _host_run(
                'mkdir -p /etc/letsencrypt/renewal-hooks/deploy && '
                'echo \'#!/bin/bash\nnginx -t && systemctl reload nginx\' '
                '> /etc/letsencrypt/renewal-hooks/deploy/ethos-nginx.sh && '
                'chmod +x /etc/letsencrypt/renewal-hooks/deploy/ethos-nginx.sh',
                timeout=5)
        cfg['auto_renew'] = True
        _save_ssl_config(cfg)
        return jsonify({'status': 'ok'})
    else:
        _host_run('systemctl disable certbot.timer 2>/dev/null; systemctl stop certbot.timer 2>/dev/null', timeout=10)
        _host_run('(crontab -l 2>/dev/null | grep -v certbot) | crontab -', timeout=10)
        cfg['auto_renew'] = False
        _save_ssl_config(cfg)
        return jsonify({'status': 'ok'})


@domains_mgr_bp.route('/ssl/test', methods=['POST'])
def ssl_test():
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    data = request.json or {}
    domain = data.get('domain', '').strip()

    results = {'port_80': False, 'dns_ok': False}

    r2 = _host_run('ss -tlnp | grep ":80 "', timeout=5)
    port80_output = r2.stdout.strip() if r2.returncode == 0 else ''
    results['port_80_in_use'] = r2.returncode == 0
    results['port_80_nginx'] = 'nginx' in port80_output

    r3 = _host_run(
        'python3 -c "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); '
        's.bind((\\\"0.0.0.0\\\",80)); s.close(); print(\\\"ok\\\")" 2>&1',
        timeout=5)
    results['port_80'] = 'ok' in r3.stdout or results.get('port_80_nginx', False)

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
_TARGET_RE = re.compile(
    r'^(https?://)?[a-zA-Z0-9._\-]+(:\d{1,5})?(/[a-zA-Z0-9._\-/]*)?$'
)
_DANGEROUS_DIRECTIVES_RE = re.compile(
    r'\b(load_module|include|lua_|perl_|ssl_certificate|ssl_certificate_key)\b',
    re.IGNORECASE,
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


_NGINX_MAP_CONF = '/etc/nginx/conf.d/ethos-websocket-map.conf'
_ACME_WEBROOT = '/var/www/letsencrypt'
_ACME_CATCHALL_CONF = '/etc/nginx/conf.d/ethos-acme-catchall.conf'


def _ensure_acme_webroot():
    """Set up ACME challenge webroot + catch-all nginx config.

    This allows certbot to use --webroot mode: nginx stays running,
    so users connected via HTTPS proxy don't lose their connection.
    """
    _host_run(f'mkdir -p {shlex.quote(_ACME_WEBROOT)}/.well-known/acme-challenge', timeout=5)
    if not os.path.exists(_ACME_CATCHALL_CONF):
        conf = (
            '# EthOS: ACME challenge catch-all for certbot webroot mode\n'
            'server {\n'
            '    listen 80 default_server;\n'
            '    listen [::]:80 default_server;\n'
            '    server_name _;\n'
            '\n'
            '    location /.well-known/acme-challenge/ {\n'
            '        root /var/www/letsencrypt;\n'
            '        allow all;\n'
            '    }\n'
            '\n'
            '    location / {\n'
            '        return 444;\n'
            '    }\n'
            '}\n'
        )
        _host_run(
            f"mkdir -p /etc/nginx/conf.d && cat > {shlex.quote(_ACME_CATCHALL_CONF)} << 'ETHOSEOF'\n{conf}ETHOSEOF",
            timeout=5,
        )
        # Reload nginx so the catch-all takes effect before certbot runs
        _host_run('nginx -t 2>&1 && systemctl reload nginx', timeout=10)

    # Migrate existing standalone renewal configs → webroot
    _host_run(
        'for f in /etc/letsencrypt/renewal/*.conf; do '
        '  grep -q "authenticator = standalone" "$f" 2>/dev/null || continue; '
        '  sed -i "s/authenticator = standalone/authenticator = webroot/" "$f"; '
        '  grep -q "webroot_path" "$f" || '
        '    sed -i "/^\\[renewalparams\\]/a webroot_path = /var/www/letsencrypt," "$f"; '
        'done',
        timeout=10,
    )


def _certbot_obtain(domain, email):
    """Obtain a cert for *domain* using webroot (preferred) or standalone.

    Returns (success: bool, error_msg: str).
    """
    nginx_running = _host_run('systemctl is-active nginx', timeout=5).returncode == 0

    if nginx_running:
        # ── Webroot mode — nginx keeps running ──
        _ensure_acme_webroot()
        cmd = (
            f'certbot certonly --webroot -w {shlex.quote(_ACME_WEBROOT)} '
            f'--non-interactive --agree-tos '
            f'--email {shlex.quote(email)} '
            f'-d {shlex.quote(domain)} '
        )
    else:
        # ── Standalone — nginx is not running anyway ──
        _host_run('fuser -k 80/tcp 2>/dev/null', timeout=10)
        cmd = (
            f'certbot certonly --standalone --non-interactive --agree-tos '
            f'--email {shlex.quote(email)} '
            f'-d {shlex.quote(domain)} '
            f'--preferred-challenges http '
            f'--http-01-port 80'
        )

    r = _host_run(cmd, timeout=120)

    if not nginx_running:
        _host_run('systemctl start nginx 2>/dev/null', timeout=10)

    if r.returncode != 0:
        msg = (r.stderr.strip() or r.stdout.strip())[-500:]
        if 'too many' in msg.lower():
            return False, 'Let\'s Encrypt rate limit exceeded. Try again in an hour.'
        if 'dns' in msg.lower() or 'resolve' in msg.lower():
            return False, f'Domain {domain} does not point to this server.'
        return False, f'Certbot failed:\n{msg}'

    fullchain, _ = _cert_paths(domain)
    if not os.path.exists(fullchain):
        return False, 'Certbot completed but the certificate was not created'

    return True, ''


def _ensure_nginx_map():
    """Ensure the $connection_upgrade map directive exists.

    Uses a file in /etc/nginx/conf.d/ which is auto-included by nginx.conf.
    This is safe on fresh installs — no sed hacking of nginx.conf needed.
    """
    if os.path.exists(_NGINX_MAP_CONF):
        return
    map_content = (
        '# EthOS: conditional WebSocket upgrade header\n'
        'map $http_upgrade $connection_upgrade {\n'
        '    default upgrade;\n'
        '    \'\' close;\n'
        '}\n'
    )
    _host_run(f"mkdir -p /etc/nginx/conf.d && cat > {shlex.quote(_NGINX_MAP_CONF)} << 'EOF'\n{map_content}EOF", timeout=5)
    # Also clean up any old inline map that was injected directly into nginx.conf
    _host_run("sed -i '/^\\s*map \$http_upgrade \$connection_upgrade/,/^\\s*}/d' /etc/nginx/nginx.conf 2>/dev/null", timeout=5)


def _nginx_reload():
    _ensure_nginx_map()
    r = _host_run('nginx -t 2>&1', timeout=10)
    if r.returncode != 0:
        return False, r.stderr.strip() or r.stdout.strip()
    _host_run('systemctl reload nginx', timeout=10)
    return True, ''


def _nginx_conf_name(domain_id):
    return f'ethos-{domain_id}'


def _generate_nginx_conf(entry):
    domain = entry['domain']
    target = entry.get('target', '').strip()
    ssl = entry.get('ssl', False)
    force_https = entry.get('force_https', False)
    custom_config = entry.get('custom_config', '').strip()
    websocket = entry.get('websocket', False)

    if not target:
        target = f'127.0.0.1:{int(os.environ.get("PORT", "9000"))}'
    if not target.startswith('http://') and not target.startswith('https://'):
        target = 'http://' + target

    lines = []

    if ssl and force_https:
        lines.append('server {')
        lines.append('    listen 80;')
        lines.append('    listen [::]:80;')
        lines.append(f'    server_name {domain};')
        lines.append('')
        lines.append('    # ACME challenge for certbot webroot renewal')
        lines.append('    location /.well-known/acme-challenge/ {')
        lines.append('        root /var/www/letsencrypt;')
        lines.append('        allow all;')
        lines.append('    }')
        lines.append('')
        lines.append('    location / {')
        lines.append(f'        return 301 https://$host$request_uri;')
        lines.append('    }')
        lines.append('}')
        lines.append('')

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
            lines.append('')
            lines.append('    listen 80;')
            lines.append('    listen [::]:80;')
    else:
        lines.append('    listen 80;')
        lines.append('    listen [::]:80;')
        lines.append(f'    server_name {domain};')

    # ACME challenge location — always present so certbot webroot works
    # before and after SSL is enabled
    lines.append('')
    lines.append('    # ACME challenge for certbot webroot')
    lines.append('    location /.well-known/acme-challenge/ {')
    lines.append('        root /var/www/letsencrypt;')
    lines.append('        allow all;')
    lines.append('    }')

    lines.append('')
    lines.append('    # Proxy settings')
    lines.append('    location / {')
    lines.append(f'        proxy_pass {target};')
    lines.append('        proxy_set_header Host $host;')
    lines.append('        proxy_set_header X-Real-IP $remote_addr;')
    lines.append('        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;')
    lines.append('        proxy_set_header X-Forwarded-Proto $scheme;')
    if websocket:
        lines.append('        proxy_http_version 1.1;')
        lines.append('        proxy_set_header Upgrade $http_upgrade;')
        lines.append('        proxy_set_header Connection $connection_upgrade;')
        lines.append('        proxy_read_timeout 86400s;')
        lines.append('        proxy_send_timeout 86400s;')
    lines.append('    }')

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
    conf_name = _nginx_conf_name(entry['id'])
    conf_content = _generate_nginx_conf(entry)
    avail = os.path.join(_NGINX_SITES_DIR, conf_name)
    enabled = os.path.join(_NGINX_ENABLED_DIR, conf_name)

    _host_run(f'mkdir -p {shlex.quote(_NGINX_SITES_DIR)} {shlex.quote(_NGINX_ENABLED_DIR)}', timeout=5)
    # Write via Python to avoid shell injection through heredoc terminators
    with open(avail, 'w') as _f:
        _f.write(conf_content)

    if entry.get('enabled', True):
        _host_run(f'ln -sf {shlex.quote(avail)} {shlex.quote(enabled)}', timeout=5)
    else:
        _host_run(f'rm -f {shlex.quote(enabled)}', timeout=5)

    return _nginx_reload()


def _remove_nginx_conf(entry_id):
    conf_name = _nginx_conf_name(entry_id)
    avail = os.path.join(_NGINX_SITES_DIR, conf_name)
    enabled = os.path.join(_NGINX_ENABLED_DIR, conf_name)
    _host_run(f'rm -f {shlex.quote(avail)} {shlex.quote(enabled)}', timeout=5)
    _nginx_reload()


def _scan_local_services():
    services = []
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


@domains_mgr_bp.route('/domains', methods=['GET'])
def list_domains():
    data = _load_domains()
    return jsonify({
        'domains': data.get('domains', []),
        'nginx_installed': _nginx_installed(),
    })


@domains_mgr_bp.route('/domains/install-nginx', methods=['POST'])
def install_nginx():
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    if _nginx_installed():
        return jsonify({'status': 'ok', 'installed': True})

    r = _apt_install('nginx', timeout=120)
    if r.returncode != 0:
        return jsonify({'error': f'Installation failed: {r.stderr.strip()[-200:]}'}), 500

    _host_run('rm -f /etc/nginx/sites-enabled/default', timeout=5)
    _host_run('mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled', timeout=5)
    _host_run('systemctl enable nginx 2>/dev/null', timeout=10)
    r2 = _host_run('systemctl start nginx 2>&1', timeout=15)
    if r2.returncode != 0:
        _host_run('systemctl restart nginx 2>/dev/null', timeout=15)

    r3 = _host_run('systemctl is-active nginx', timeout=5)
    if r3.returncode != 0 or 'active' not in r3.stdout.strip():
        return jsonify({
            'ok': True,
            'message': 'Nginx installed but failed to start. '
                       'It will start automatically when the first domain is added.',
        })
    return jsonify({'status': 'ok'})


@domains_mgr_bp.route('/domains/services', methods=['GET'])
def local_services():
    return jsonify({'services': _scan_local_services()})


@domains_mgr_bp.route('/domains', methods=['POST'])
def add_domain():
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    if not _nginx_installed():
        return jsonify({'error': 'Nginx is not installed'}), 400

    d = request.json or {}
    domain = d.get('domain', '').strip().lower()
    target = d.get('target', '').strip()
    ssl = d.get('ssl', False)
    force_https = d.get('force_https', False)
    websocket = d.get('websocket', False)
    custom_config = d.get('custom_config', '').strip()
    description = d.get('description', '').strip()

    if not domain:
        return jsonify({'error': 'Domain is required'}), 400
    if len(domain) > 253:
        return jsonify({'error': 'Domain too long (max 253 characters)'}), 400
    if not _DOMAIN_RE.match(domain):
        return jsonify({'error': 'Invalid domain (e.g. sub.example.com)'}), 400
    if not target:
        return jsonify({'error': 'Target is required — e.g. 127.0.0.1:8080'}), 400
    if not _TARGET_RE.match(target):
        return jsonify({'error': 'Invalid target — allowed: host:port or http(s)://host:port/path'}), 400
    if custom_config and _DANGEROUS_DIRECTIVES_RE.search(custom_config):
        return jsonify({'error': 'Custom config contains forbidden directives (load_module, include, lua_, perl_, ssl_certificate)'}), 400

    data = _load_domains()
    for existing in data.get('domains', []):
        if existing['domain'] == domain:
            return jsonify({'error': f'Domain {domain} is already configured'}), 409

    if ssl:
        fullchain, _ = _cert_paths(domain)
        if not os.path.exists(fullchain):
            # Auto-obtain certificate if email provided
            email = d.get('email', '').strip()
            if not email:
                # Try saved email
                ssl_cfg = _load_ssl_config()
                email = ssl_cfg.get('email', '')
            if not email:
                return jsonify({'error': 'SSL requires a certificate. Provide an email to obtain one automatically.'}), 400
            if not _certbot_installed():
                return jsonify({'error': 'Certbot is not installed. Install it in the SSL tab.'}), 400
            # Obtain certificate (webroot mode — nginx stays up)
            ok, err = _certbot_obtain(domain, email)
            if not ok:
                code = 429 if 'limit' in err.lower() else (400 if 'does not point' in err else 500)
                return jsonify({'error': err}), code
            # Save email and domain for future use
            ssl_cfg = _load_ssl_config()
            if not ssl_cfg.get('email'):
                ssl_cfg['email'] = email
            if not ssl_cfg.get('domain'):
                ssl_cfg['domain'] = domain
            _save_ssl_config(ssl_cfg)

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

    ok, err = _write_nginx_conf(entry)
    if not ok:
        return jsonify({'error': f'Nginx configuration error:\n{err[-300:]}'}), 500

    data.setdefault('domains', []).append(entry)
    _save_domains(data)
    return jsonify({'ok': True, 'domain': entry})


@domains_mgr_bp.route('/domains/<domain_id>', methods=['PUT'])
def update_domain(domain_id):
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    data = _load_domains()
    entry = None
    for e in data.get('domains', []):
        if e['id'] == domain_id:
            entry = e
            break
    if not entry:
        return jsonify({'error': 'Domain not found'}), 404

    d = request.json or {}
    for k in ('target', 'ssl', 'force_https', 'websocket', 'custom_config', 'description', 'enabled'):
        if k in d:
            entry[k] = d[k]

    # Validate target if provided
    new_target = d.get('target', '').strip() if 'target' in d else None
    if new_target is not None and new_target and not _TARGET_RE.match(new_target):
        return jsonify({'error': 'Invalid target — allowed: host:port or http(s)://host:port/path'}), 400
    # Validate custom_config if provided
    new_cc = d.get('custom_config', '').strip() if 'custom_config' in d else None
    if new_cc and _DANGEROUS_DIRECTIVES_RE.search(new_cc):
        return jsonify({'error': 'Custom config contains forbidden directives'}), 400

    if 'domain' in d:
        new_domain = d['domain'].strip().lower()
        if new_domain and new_domain != entry['domain']:
            if len(new_domain) > 253:
                return jsonify({'error': 'Domain too long (max 253 characters)'}), 400
            if not _DOMAIN_RE.match(new_domain):
                return jsonify({'error': 'Invalid domain'}), 400
            for other in data.get('domains', []):
                if other['id'] != domain_id and other['domain'] == new_domain:
                    return jsonify({'error': f'Domain {new_domain} is already configured'}), 409
            # If SSL is enabled, check if cert exists for the new domain name.
            # If not — auto-disable SSL instead of blocking the rename.
            if entry.get('ssl') or d.get('ssl'):
                fullchain, _ = _cert_paths(new_domain)
                if not os.path.exists(fullchain):
                    entry['ssl'] = False
                    entry['force_https'] = False
            _remove_nginx_conf(domain_id)
            entry['domain'] = new_domain

    if entry.get('ssl'):
        fullchain, _ = _cert_paths(entry['domain'])
        if not os.path.exists(fullchain):
            entry['ssl'] = False
            entry['force_https'] = False

    entry['updated'] = datetime.utcnow().isoformat()

    ok, err = _write_nginx_conf(entry)
    if not ok:
        return jsonify({'error': f'Nginx configuration error:\n{err[-300:]}'}), 500

    _save_domains(data)
    return jsonify({'ok': True, 'domain': entry})


@domains_mgr_bp.route('/domains/<domain_id>', methods=['DELETE'])
def delete_domain(domain_id):
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    data = _load_domains()
    domains = data.get('domains', [])
    found = None
    for i, e in enumerate(domains):
        if e['id'] == domain_id:
            found = i
            break
    if found is None:
        return jsonify({'error': 'Domain not found'}), 404

    entry = domains.pop(found)
    _remove_nginx_conf(domain_id)
    _save_domains(data)
    return jsonify({'status': 'ok', 'domain': entry["domain"]})


@domains_mgr_bp.route('/domains/<domain_id>/toggle', methods=['POST'])
def toggle_domain(domain_id):
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    data = _load_domains()
    entry = None
    for e in data.get('domains', []):
        if e['id'] == domain_id:
            entry = e
            break
    if not entry:
        return jsonify({'error': 'Domain not found'}), 404

    entry['enabled'] = not entry.get('enabled', True)
    ok, err = _write_nginx_conf(entry)
    if not ok:
        return jsonify({'error': f'Nginx error: {err[-200:]}'}), 500
    _save_domains(data)

    status_str = 'enabled' if entry['enabled'] else 'disabled'
    return jsonify({'status': 'ok', 'enabled': entry['enabled'], 'domain': entry["domain"]})


@domains_mgr_bp.route('/domains/<domain_id>/ssl', methods=['POST'])
def domain_ssl(domain_id):
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot is not installed.'}), 400

    data = _load_domains()
    entry = None
    for e in data.get('domains', []):
        if e['id'] == domain_id:
            entry = e
            break
    if not entry:
        return jsonify({'error': 'Domain not found'}), 404

    domain = entry['domain']
    d = request.json or {}
    email = d.get('email', '').strip()

    if not email:
        ssl_cfg = _load_ssl_config()
        email = ssl_cfg.get('email', '')
    if not email:
        return jsonify({'error': 'Email is required (provide it in the SSL section)'}), 400

    # Use webroot mode (nginx stays running) — same as add_domain
    ok, err = _certbot_obtain(domain, email)
    if not ok:
        code = 429 if 'limit' in err.lower() else (400 if 'does not point' in err else 500)
        return jsonify({'error': err}), code

    fullchain, _ = _cert_paths(domain)
    if not os.path.exists(fullchain):
        return jsonify({'error': 'Certbot completed but the certificate does not exist'}), 500

    entry['ssl'] = True
    entry['force_https'] = True
    _write_nginx_conf(entry)
    _save_domains(data)

    # Save email/domain to ssl_config for consistency
    ssl_cfg = _load_ssl_config()
    if not ssl_cfg.get('email'):
        ssl_cfg['email'] = email
    if not ssl_cfg.get('domain'):
        ssl_cfg['domain'] = domain
    _save_ssl_config(ssl_cfg)

    return jsonify({
        'ok': True,
        'message': f'SSL certificate for {domain} obtained!',
        'cert': _cert_info(domain),
    })


@domains_mgr_bp.route('/domains/<domain_id>/preview', methods=['GET'])
def domain_preview(domain_id):
    data = _load_domains()
    entry = None
    for e in data.get('domains', []):
        if e['id'] == domain_id:
            entry = e
            break
    if not entry:
        return jsonify({'error': 'Domain not found'}), 404
    return jsonify({'config': _generate_nginx_conf(entry)})


@domains_mgr_bp.route('/domains/nginx-status', methods=['GET'])
def nginx_status():
    if not _nginx_installed():
        return jsonify({'installed': False})
    r = _host_run('systemctl is-active nginx', timeout=5)
    active = r.returncode == 0 and 'active' in r.stdout.strip()
    r2 = _host_run('nginx -t 2>&1', timeout=10)
    config_ok = r2.returncode == 0
    # Get uptime
    r3 = _host_run("systemctl show nginx --property=ActiveEnterTimestamp --value 2>/dev/null", timeout=5)
    uptime_since = r3.stdout.strip() if r3.returncode == 0 else ''
    return jsonify({
        'installed': True,
        'active': active,
        'config_ok': config_ok,
        'config_test': r2.stderr.strip() or r2.stdout.strip(),
        'uptime_since': uptime_since,
    })


@domains_mgr_bp.route('/domains/nginx-action', methods=['POST'])
def nginx_action():
    """Restart / reload / start / stop nginx."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    data = request.json or {}
    action = data.get('action', 'reload')
    if action not in ('restart', 'reload', 'start', 'stop'):
        return jsonify({'error': 'Invalid action'}), 400
    if action in ('restart', 'reload'):
        _ensure_nginx_map()
        r_test = _host_run('nginx -t 2>&1', timeout=10)
        if r_test.returncode != 0:
            return jsonify({'error': f'Nginx configuration error:\n{(r_test.stderr or r_test.stdout).strip()[-400:]}'}), 400
    r = _host_run(f'systemctl {action} nginx', timeout=15)
    if r.returncode != 0:
        return jsonify({'error': f'Failed: {(r.stderr or r.stdout).strip()[-300:]}'}), 500
    return jsonify({'status': 'ok', 'action': action})
