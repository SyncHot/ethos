"""
EthOS — SSL / Let's Encrypt Management Blueprint
Handles certificate management, renewal, and HTTPS configuration.
"""

import os
import json
import shlex
from datetime import datetime
from flask import Blueprint, request, jsonify, g

# Import blueprint from main settings module
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from blueprints.settings import settings_bp
from host import host_run as _host_run, data_path as _data_path, apt_install as _apt_install
from audit import audit_log

# Import helpers from main settings
def _get_shared_helpers():
    """Access helpers from main settings module."""
    m = sys.modules.get('blueprints.settings')
    return {
        'read_env': getattr(m, '_read_env', None),
        'write_env_key': getattr(m, '_write_env_key', None),
    }


# ── SSL Configuration Constants ──

_SSL_CONFIG_FILE = _data_path('ssl_config.json')
_CERT_DIR = '/etc/letsencrypt/live'


# ── SSL Helpers ──

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
    return _cert_info_file(fullchain, privkey=privkey, domain=domain)


def _cert_info_file(cert_path, privkey=None, domain=None):
    """Get certificate info from a file path."""
    if not os.path.exists(cert_path):
        return None
    try:
        r = _host_run(
            f'openssl x509 -noout -subject -issuer -dates -serial '
            f'-in {shlex.quote(cert_path)}',
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
        info['fullchain'] = cert_path
        if privkey:
            info['privkey'] = privkey
        if domain:
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


# ── Routes ──

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
    helpers = _get_shared_helpers()
    if helpers['read_env']:
        env = helpers['read_env']()
        result['ssl_active'] = env.get('SSL_ENABLED', '') == '1'
        result['https_port'] = int(env.get('HTTPS_PORT', cfg.get('https_port', 443)))
    else:
        result['ssl_active'] = False
        result['https_port'] = cfg.get('https_port', 443)

    # Check for self-signed cert (auto-generated at firstboot)
    ssl_dir = _data_path('ssl')
    self_signed_crt = os.path.join(ssl_dir, 'ethos.crt')
    self_signed_key = os.path.join(ssl_dir, 'ethos.key')
    result['self_signed'] = os.path.exists(self_signed_crt) and os.path.exists(self_signed_key)
    if result['self_signed'] and not result['cert']:
        result['self_signed_cert'] = _cert_info_file(self_signed_crt)
    result['https_side_port'] = int(os.environ.get('HTTPS_SIDE_PORT', '9443'))

    return jsonify(result)


@settings_bp.route('/ssl/install-certbot', methods=['POST'])
def install_certbot():
    """Install certbot package."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    if _certbot_installed():
        return jsonify({'status': 'ok', 'installed': True})

    r = _apt_install('certbot', timeout=120)
    if r.returncode != 0:
        return jsonify({'error': f'Installation failed: {r.stderr.strip()[-200:]}'}), 500

    return jsonify({'status': 'ok'})


@settings_bp.route('/ssl/obtain', methods=['POST'])
def ssl_obtain():
    """Obtain a Let's Encrypt certificate using standalone mode."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot is not installed'}), 400

    data = request.json or {}
    domain = data.get('domain', '').strip().lower()
    email = data.get('email', '').strip()
    https_port = int(data.get('https_port', 443))

    if not domain:
        return jsonify({'error': 'Domain is required'}), 400
    if not __import__('re').match(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$', domain):
        return jsonify({'error': 'Invalid domain'}), 400
    if not email or '@' not in email:
        return jsonify({'error': 'A valid email is required'}), 400

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
            return jsonify({'error': "Let's Encrypt rate limit exceeded. Try again in an hour."}), 429
        if 'dns' in msg.lower() or 'resolve' in msg.lower():
            return jsonify({'error': f'Domain {domain} does not point to this server. Ensure DNS A/AAAA points to this server\'s public IP and port 80 is open.'}), 400
        return jsonify({'error': f'Certbot failed:\n{msg[-500:]}'}), 500

    # Verify cert was created
    fullchain, privkey = _cert_paths(domain)
    if not os.path.exists(fullchain):
        return jsonify({'error': 'Certbot completed but certificate was not created'}), 500

    # Save config
    cfg = _load_ssl_config()
    cfg['domain'] = domain
    cfg['email'] = email
    cfg['https_port'] = https_port
    _save_ssl_config(cfg)

    return jsonify({
        'ok': True,
        'message': f'Certificate for {domain} obtained successfully!',
        'cert': _cert_info(domain),
    })


@settings_bp.route('/ssl/enable', methods=['POST'])
def ssl_enable():
    """Enable or disable HTTPS on EthOS."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403

    data = request.json or {}
    enabled = data.get('enabled', False)

    cfg = _load_ssl_config()

    if enabled:
        # Verify cert exists
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

        # Set env vars for the server
        helpers = _get_shared_helpers()
        if helpers['write_env_key']:
            write_env_key = helpers['write_env_key']
            write_env_key('SSL_ENABLED', '1')
            write_env_key('SSL_CERT', fullchain)
            write_env_key('SSL_KEY', privkey)
            write_env_key('HTTPS_PORT', str(https_port))
            if redirect_http:
                write_env_key('SSL_REDIRECT', '1')
            else:
                write_env_key('SSL_REDIRECT', '0')

        return jsonify({
            'ok': True,
            'message': f'HTTPS enabled on port {https_port}. Server restart required.',
            'restart_needed': True,
        })
    else:
        cfg['enabled'] = False
        _save_ssl_config(cfg)
        helpers = _get_shared_helpers()
        if helpers['write_env_key']:
            helpers['write_env_key']('SSL_ENABLED', '0')

        return jsonify({
            'ok': True,
            'message': 'HTTPS disabled. Server restart required.',
            'restart_needed': True,
        })


@settings_bp.route('/ssl/renew', methods=['POST'])
def ssl_renew():
    """Manually renew the certificate."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    if not _certbot_installed():
        return jsonify({'error': 'Certbot is not installed'}), 400

    cfg = _load_ssl_config()
    domain = cfg.get('domain', '')
    if not domain:
        return jsonify({'error': 'No domain configured'}), 400

    # Stop anything on port 80
    _host_run('fuser -k 80/tcp 2>/dev/null', timeout=10)

    r = _host_run(
        f'certbot renew --cert-name {shlex.quote(domain)} --standalone --non-interactive',
        timeout=120)

    if r.returncode != 0:
        return jsonify({'error': f'Renewal failed:\n{r.stderr.strip()[-300:]}'}), 500

    cert = _cert_info(domain)
    return jsonify({
        'ok': True,
        'message': 'Certificate renewed successfully!',
        'cert': cert,
    })


@settings_bp.route('/ssl/auto-renew', methods=['POST'])
def ssl_auto_renew():
    """Enable or disable automatic certificate renewal."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403

    data = request.json or {}
    enabled = data.get('enabled', True)

    cfg = _load_ssl_config()
    domain = cfg.get('domain', '')

    if enabled:
        # Enable certbot.timer (if available from package) or create a cron job
        r = _host_run('systemctl enable certbot.timer && systemctl start certbot.timer', timeout=15)
        if r.returncode != 0:
            # Fallback: add to cron — renew at 3 AM daily
            cron_line = '0 3 * * * certbot renew --standalone --pre-hook "fuser -k 80/tcp 2>/dev/null" --quiet'
            _host_run(
                f'(crontab -l 2>/dev/null | grep -v certbot; echo {shlex.quote(cron_line)}) | crontab -',
                timeout=10)
        cfg['auto_renew'] = True
        _save_ssl_config(cfg)
        return jsonify({'status': 'ok'})
    else:
        _host_run('systemctl disable certbot.timer 2>/dev/null; systemctl stop certbot.timer 2>/dev/null', timeout=10)
        _host_run('(crontab -l 2>/dev/null | grep -v certbot) | crontab -', timeout=10)
        cfg['auto_renew'] = False
        _save_ssl_config(cfg)
        return jsonify({'status': 'ok'})


@settings_bp.route('/ssl/test', methods=['POST'])
def ssl_test():
    """Test if port 80 is reachable from the outside (needed for Let's Encrypt)."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403

    data = request.json or {}
    domain = data.get('domain', '').strip()

    results = {'port_80': False, 'dns_ok': False}

    # Check if port 80 is in use
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
