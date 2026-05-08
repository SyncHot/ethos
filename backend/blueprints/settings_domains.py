"""
EthOS — Domain & Subdomain Management Blueprint
Handles nginx reverse proxy configuration for domains.
"""

import os
import re
import json
import shlex
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, g

# Import blueprint from main settings module
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from blueprints.settings import settings_bp
from host import host_run as _host_run, data_path as _data_path, apt_install as _apt_install

# ── Domain Configuration Constants ──

_DOMAINS_FILE = _data_path('domains.json')
_NGINX_SITES_DIR = '/etc/nginx/sites-available'
_NGINX_ENABLED_DIR = '/etc/nginx/sites-enabled'
_DOMAIN_RE = re.compile(
    r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$'
)


# ── Domain Helpers ──

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


# Access helpers from main settings module
def _get_cert_paths(domain):
    """Get certificate paths. Access SSL helpers."""
    m = sys.modules.get('blueprints.settings_ssl')
    if m and hasattr(m, '_cert_paths'):
        return m._cert_paths(domain)
    # Fallback
    return f'/etc/letsencrypt/live/{domain}/fullchain.pem', f'/etc/letsencrypt/live/{domain}/privkey.pem'


def _get_cert_info(domain):
    """Get certificate info. Access SSL helpers."""
    m = sys.modules.get('blueprints.settings_ssl')
    if m and hasattr(m, '_cert_info'):
        return m._cert_info(domain)
    return None


def _certbot_installed():
    """Check if certbot is installed."""
    m = sys.modules.get('blueprints.settings_ssl')
    if m and hasattr(m, '_certbot_installed'):
        return m._certbot_installed()
    return False


# ── Routes ──

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
        return jsonify({'error': 'Admin only'}), 403
    if _nginx_installed():
        return jsonify({'status': 'ok', 'installed': True})

    r = _apt_install('nginx', timeout=120)
    if r.returncode != 0:
        return jsonify({'error': f'Installation failed: {r.stderr.strip()[-200:]}'}), 500

    # Remove default site to avoid port 80 conflict
    _host_run('rm -f /etc/nginx/sites-enabled/default', timeout=5)

    # Write a stub nginx.conf that doesn't listen on any port by default
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
            'message': 'Nginx installed but failed to start (port 80 may be in use). '
                       'Will start automatically when the first domain is added.',
        })

    return jsonify({'status': 'ok'})


@settings_bp.route('/domains/services', methods=['GET'])
def local_services():
    """Scan for running services on localhost."""
    return jsonify({'services': _scan_local_services()})


@settings_bp.route('/domains', methods=['POST'])
def add_domain():
    """Add a new domain/subdomain proxy entry."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403
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
    if not _DOMAIN_RE.match(domain):
        return jsonify({'error': 'Invalid domain (e.g. sub.example.com)'}), 400
    if not target:
        return jsonify({'error': 'Target is required — e.g. 127.0.0.1:8080'}), 400

    data = _load_domains()
    for existing in data.get('domains', []):
        if existing['domain'] == domain:
            return jsonify({'error': f'Domain {domain} is already configured'}), 409

    # If SSL requested, check cert
    if ssl:
        fullchain, _ = _get_cert_paths(domain)
        if not os.path.exists(fullchain):
            return jsonify({'error': f'No SSL certificate for {domain}. Obtain a certificate first in the SSL tab.'}), 400

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
        return jsonify({'error': f'Nginx configuration error:\n{err[-300:]}'}), 500

    data.setdefault('domains', []).append(entry)
    _save_domains(data)

    return jsonify({'ok': True, 'domain': entry})


@settings_bp.route('/domains/<domain_id>', methods=['PUT'])
def update_domain(domain_id):
    """Update an existing domain entry."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403

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

    # Allow changing domain itself
    if 'domain' in d:
        new_domain = d['domain'].strip().lower()
        if new_domain and new_domain != entry['domain']:
            if not _DOMAIN_RE.match(new_domain):
                return jsonify({'error': 'Invalid domain'}), 400
            for other in data.get('domains', []):
                if other['id'] != domain_id and other['domain'] == new_domain:
                    return jsonify({'error': f'Domain {new_domain} is already configured'}), 409
            # Remove old conf
            _remove_nginx_conf(domain_id)
            entry['domain'] = new_domain

    if entry.get('ssl'):
        fullchain, _ = _get_cert_paths(entry['domain'])
        if not os.path.exists(fullchain):
            return jsonify({'error': f'No SSL certificate for {entry["domain"]}'}), 400

    entry['updated'] = datetime.utcnow().isoformat()

    ok, err = _write_nginx_conf(entry)
    if not ok:
        return jsonify({'error': f'Nginx configuration error:\n{err[-300:]}'}), 500

    _save_domains(data)
    return jsonify({'ok': True, 'domain': entry})


@settings_bp.route('/domains/<domain_id>', methods=['DELETE'])
def delete_domain(domain_id):
    """Delete a domain entry and its nginx config."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403

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


@settings_bp.route('/domains/<domain_id>/toggle', methods=['POST'])
def toggle_domain(domain_id):
    """Enable or disable a domain."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403

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


@settings_bp.route('/domains/<domain_id>/ssl', methods=['POST'])
def domain_ssl(domain_id):
    """Obtain a Let's Encrypt certificate for this domain."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot is not installed. Install it in the SSL tab.'}), 400

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

    # Try to get email from SSL config
    if not email:
        # Load SSL config from settings_ssl module
        m = sys.modules.get('blueprints.settings_ssl')
        if m and hasattr(m, '_load_ssl_config'):
            ssl_cfg = m._load_ssl_config()
            email = ssl_cfg.get('email', '')
    if not email:
        return jsonify({'error': 'Email is required (provide it in the SSL tab)'}), 400

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
        return jsonify({'error': f'Certbot failed:\n{msg}'}), 500

    fullchain, _ = _get_cert_paths(domain)
    if not os.path.exists(fullchain):
        return jsonify({'error': 'Certbot completed but certificate does not exist'}), 500

    # Enable SSL on this entry
    entry['ssl'] = True
    entry['force_https'] = True
    _write_nginx_conf(entry)
    _save_domains(data)

    return jsonify({
        'ok': True,
        'message': f'SSL certificate for {domain} obtained!',
        'cert': _get_cert_info(domain),
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
        return jsonify({'error': 'Domain not found'}), 404
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
