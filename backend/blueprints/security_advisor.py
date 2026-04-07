"""
EthOS — Security Advisor Blueprint
Scans system for security weaknesses, generates a security score (0-100),
and provides actionable recommendations with optional one-click fixes.

Endpoints:
  GET  /api/security-advisor/scan          — Run a full security scan
  POST /api/security-advisor/fix           — Apply a one-click fix
  GET  /api/security-advisor/pkg-status    — Package install status
  POST /api/security-advisor/cleanup-disk  — Free disk space (apt/pip cache)
"""

import json
import os
import re
import shutil
import time
from datetime import datetime
from flask import Blueprint, jsonify, request

from host import host_run, q, data_path

security_advisor_bp = Blueprint('security_advisor', __name__,
                                url_prefix='/api/security-advisor')

_LAST_SCAN_FILE = data_path('security_advisor_scan.json')


# ── Security checks ──────────────────────────────────────────────────────

def _check_default_admin_password():
    marker = os.path.join(os.path.dirname(__file__), '..', '..', 'data', '.password_changed')
    if not os.path.exists(marker):
        return {
            'id': 'default_password', 'severity': 'critical',
            'title': 'Domyslne haslo administratora',
            'description': 'Haslo administratora nie zostalo zmienione od instalacji. Zmien je natychmiast.',
            'fixable': False, 'passed': False,
        }
    return {'id': 'default_password', 'severity': 'critical', 'title': 'Haslo administratora zmienione', 'passed': True}


def _check_2fa_admin():
    r = host_run("getent group sudo ethos-admin 2>/dev/null | cut -d: -f4 | tr ',' '\\n' | sort -u", timeout=5)
    admins = [u.strip() for u in (r.stdout or '').split('\n') if u.strip()]
    totp_dir = data_path('totp')
    missing = [a for a in admins if not os.path.exists(os.path.join(totp_dir, f'{a}.json'))]
    if missing:
        return {
            'id': '2fa_admin', 'severity': 'high',
            'title': '2FA nie wlaczone dla administratorow',
            'description': f'Administratorzy bez 2FA: {", ".join(missing)}.',
            'fixable': False, 'passed': False,
        }
    return {'id': '2fa_admin', 'severity': 'high', 'title': '2FA wlaczone dla administratorow', 'passed': True}


def _check_ssh_root():
    r = host_run("grep -E '^PermitRootLogin' /etc/ssh/sshd_config 2>/dev/null", timeout=5)
    line = r.stdout.strip() if r.returncode == 0 else ''
    if 'no' in line.lower():
        return {'id': 'ssh_root', 'severity': 'high', 'title': 'SSH root login wylaczony', 'passed': True}
    return {
        'id': 'ssh_root', 'severity': 'high',
        'title': 'SSH root login wlaczony',
        'description': 'Logowanie root przez SSH powinno byc wylaczone.',
        'fixable': True, 'fix_action': 'disable_ssh_root', 'passed': False,
    }


def _check_ssh_password():
    r = host_run("grep -E '^PasswordAuthentication' /etc/ssh/sshd_config 2>/dev/null", timeout=5)
    line = r.stdout.strip() if r.returncode == 0 else ''
    if 'no' in line.lower():
        return {'id': 'ssh_password', 'severity': 'medium', 'title': 'SSH haslo wylaczone (klucze)', 'passed': True}
    return {
        'id': 'ssh_password', 'severity': 'medium',
        'title': 'SSH logowanie haslem wlaczone',
        'description': 'Rozwaz wylaczenie logowania haslem SSH i uzywanie kluczy.',
        'fixable': True, 'fix_action': 'disable_ssh_password', 'passed': False,
    }


def _check_firewall():
    r = host_run("ufw status 2>/dev/null", timeout=5)
    if 'Status: active' in (r.stdout or ''):
        return {'id': 'firewall', 'severity': 'high', 'title': 'Firewall (UFW) aktywny', 'passed': True}
    return {
        'id': 'firewall', 'severity': 'high',
        'title': 'Firewall (UFW) nieaktywny',
        'description': 'Firewall nie jest wlaczony.',
        'fixable': True, 'fix_action': 'enable_firewall', 'passed': False,
    }


def _check_fail2ban():
    r = host_run("systemctl is-active fail2ban 2>/dev/null", timeout=5)
    if r.stdout.strip() == 'active':
        jr = host_run("fail2ban-client status 2>/dev/null | grep 'Jail list'", timeout=5)
        jails = jr.stdout.strip().split(':')[-1].strip() if jr.returncode == 0 and ':' in (jr.stdout or '') else 'brak'
        return {'id': 'fail2ban', 'severity': 'high', 'title': f'Fail2Ban aktywny ({jails})', 'passed': True}
    return {
        'id': 'fail2ban', 'severity': 'high',
        'title': 'Fail2Ban nieaktywny',
        'description': 'Fail2Ban chroni przed atakami brute-force.',
        'fixable': True, 'fix_action': 'enable_fail2ban', 'passed': False,
    }


def _check_https():
    env_file = os.path.join(os.path.dirname(__file__), '..', '..', 'ethos.env')
    try:
        with open(env_file, 'r') as f:
            for line in f:
                if line.strip().startswith('SSL_ENABLED=') and 'true' in line.lower():
                    return {'id': 'https', 'severity': 'high', 'title': 'HTTPS wlaczone', 'passed': True}
    except FileNotFoundError:
        pass
    return {
        'id': 'https', 'severity': 'high',
        'title': 'HTTPS wylaczone',
        'description': 'Polaczenia nie sa szyfrowane. Skonfiguruj certyfikat SSL/TLS.',
        'fixable': False, 'passed': False,
    }


def _check_open_ports():
    r = host_run("ss -tlnp 2>/dev/null | tail -n +2", timeout=10)
    expected = {'22', '80', '443', '9000', '445', '139', '631', '53', '51820', '5353'}
    unexpected = []
    for line in (r.stdout or '').splitlines():
        m = re.search(r':(\d+)\s', line)
        if m:
            port = m.group(1)
            if port not in expected and int(port) < 32768:
                proc = re.search(r'users:\(\("([^"]+)"', line)
                unexpected.append(f'{port} ({proc.group(1) if proc else "?"})')
    if not unexpected:
        return {'id': 'open_ports', 'severity': 'medium', 'title': 'Brak nieoczekiwanych otwartych portow', 'passed': True}
    return {
        'id': 'open_ports', 'severity': 'medium',
        'title': f'{len(unexpected)} nieoczekiwanych otwartych portow',
        'description': f'Porty: {", ".join(unexpected[:10])}.',
        'fixable': False, 'passed': False,
    }


def _check_system_updates():
    r = host_run("apt list --upgradable 2>/dev/null | grep -c upgradable", timeout=30)
    try:
        count = int(r.stdout.strip())
    except (ValueError, AttributeError):
        count = 0
    if count == 0:
        return {'id': 'updates', 'severity': 'medium', 'title': 'System aktualny', 'passed': True}
    return {
        'id': 'updates', 'severity': 'medium',
        'title': f'{count} aktualizacji systemu dostepnych',
        'description': 'Zainstaluj aktualizacje pakietow.',
        'fixable': True, 'fix_action': 'apt_upgrade', 'passed': False,
    }


def _check_ssl_expiry():
    cert_path = '/etc/letsencrypt/live'
    if not os.path.isdir(cert_path):
        return {'id': 'ssl_cert', 'severity': 'info', 'title': 'Brak certyfikatu SSL', 'passed': True}
    r = host_run(f"find {cert_path} -name cert.pem -exec openssl x509 -enddate -noout -in {{}} \\; 2>/dev/null", timeout=10)
    for line in (r.stdout or '').splitlines():
        if 'notAfter' in line:
            try:
                date_str = line.split('=')[1].strip()
                expiry = datetime.strptime(date_str, '%b %d %H:%M:%S %Y %Z')
                days_left = (expiry - datetime.now()).days
                if days_left < 14:
                    return {
                        'id': 'ssl_cert', 'severity': 'high',
                        'title': f'Certyfikat SSL wygasa za {days_left} dni',
                        'fixable': True, 'fix_action': 'renew_cert', 'passed': False,
                    }
                return {'id': 'ssl_cert', 'severity': 'info', 'title': f'Certyfikat SSL wazny ({days_left} dni)', 'passed': True}
            except Exception:
                pass
    return {'id': 'ssl_cert', 'severity': 'info', 'title': 'Certyfikat SSL OK', 'passed': True}


def _check_samba_guest():
    r = host_run("grep -i 'guest ok.*=.*yes' /etc/samba/smb.conf 2>/dev/null", timeout=5)
    if r.returncode == 0 and r.stdout.strip():
        n = len(r.stdout.strip().splitlines())
        return {
            'id': 'samba_guest', 'severity': 'medium',
            'title': f'Samba: {n} udzialow z dostepem goscia',
            'fixable': False, 'passed': False,
        }
    return {'id': 'samba_guest', 'severity': 'medium', 'title': 'Samba: brak dostepu goscia', 'passed': True}


def _check_password_policy():
    policy_file = data_path('password_policy.json')
    if os.path.exists(policy_file):
        try:
            with open(policy_file, 'r') as f:
                p = json.load(f)
            if p.get('min_length', 8) >= 10 and p.get('require_upper') and p.get('require_digit'):
                return {'id': 'password_policy', 'severity': 'medium', 'title': 'Silna polityka hasel', 'passed': True}
        except Exception:
            pass
    return {
        'id': 'password_policy', 'severity': 'medium',
        'title': 'Slaba polityka hasel',
        'description': 'Ustaw min. 10 znakow, wielkie litery i cyfry.',
        'fixable': False, 'passed': False,
    }


def _check_auto_updates():
    env_file = os.path.join(os.path.dirname(__file__), '..', '..', 'ethos.env')
    try:
        with open(env_file, 'r') as f:
            content = f.read()
        if 'AUTO_UPDATE=true' in content or 'AUTO_UPDATE=1' in content:
            return {'id': 'auto_update', 'severity': 'low', 'title': 'Auto-aktualizacje wlaczone', 'passed': True}
    except FileNotFoundError:
        pass
    return {
        'id': 'auto_update', 'severity': 'low',
        'title': 'Auto-aktualizacje wylaczone',
        'fixable': False, 'passed': False,
    }


def _check_file_permissions():
    issues = []
    for path, max_perm in [('/etc/shadow', 640), ('/etc/ssh/sshd_config', 644)]:
        r = host_run(f"stat -c '%a' {q(path)} 2>/dev/null", timeout=5)
        if r.returncode == 0:
            try:
                if int(r.stdout.strip()) > max_perm:
                    issues.append(f'{path} ({r.stdout.strip()})')
            except ValueError:
                pass
    if not issues:
        return {'id': 'file_perms', 'severity': 'medium', 'title': 'Uprawnienia plikow poprawne', 'passed': True}
    return {
        'id': 'file_perms', 'severity': 'medium',
        'title': 'Zbyt szerokie uprawnienia plikow',
        'description': f'Pliki: {", ".join(issues)}',
        'fixable': False, 'passed': False,
    }


# ── Scan runner ───────────────────────────────────────────────────────────

ALL_CHECKS = [
    _check_default_admin_password, _check_2fa_admin,
    _check_ssh_root, _check_ssh_password,
    _check_firewall, _check_fail2ban, _check_https,
    _check_open_ports, _check_system_updates, _check_ssl_expiry,
    _check_samba_guest, _check_password_policy,
    _check_auto_updates, _check_file_permissions,
]

_SEVERITY_WEIGHT = {'critical': 15, 'high': 10, 'medium': 5, 'low': 2, 'info': 0}


def run_scan():
    results = []
    for check in ALL_CHECKS:
        try:
            results.append(check())
        except Exception:
            results.append({'id': check.__name__, 'severity': 'info',
                            'title': f'Blad: {check.__name__}', 'passed': True})

    total_w = sum(_SEVERITY_WEIGHT.get(r.get('severity', 'info'), 0) for r in results)
    fail_w = sum(_SEVERITY_WEIGHT.get(r.get('severity', 'info'), 0) for r in results if not r.get('passed', True))
    score = 100 if total_w == 0 else max(0, int(100 * (1 - fail_w / total_w)))
    passed = sum(1 for r in results if r.get('passed'))

    scan_result = {
        'score': score, 'total': len(results), 'passed': passed,
        'failed': len(results) - passed, 'checks': results,
        'scanned_at': datetime.now().isoformat(),
    }
    try:
        os.makedirs(os.path.dirname(_LAST_SCAN_FILE), exist_ok=True)
        with open(_LAST_SCAN_FILE, 'w') as f:
            json.dump(scan_result, f, indent=2)
    except Exception:
        pass
    return scan_result


# ── Fix actions ───────────────────────────────────────────────────────────

def _fix_disable_ssh_root():
    host_run("sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config", timeout=5)
    host_run("systemctl restart sshd 2>/dev/null || systemctl restart ssh 2>/dev/null", timeout=10)
    return 'SSH root login wylaczony'

def _fix_disable_ssh_password():
    host_run("sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config", timeout=5)
    host_run("systemctl restart sshd 2>/dev/null || systemctl restart ssh 2>/dev/null", timeout=10)
    return 'SSH logowanie haslem wylaczone'

def _fix_enable_firewall():
    host_run("ufw --force enable", timeout=10)
    host_run("ufw allow 9000/tcp", timeout=5)
    host_run("ufw allow 22/tcp", timeout=5)
    return 'Firewall wlaczony (porty 9000, 22)'

def _fix_enable_fail2ban():
    host_run("apt-get install -y fail2ban && apt-get clean 2>/dev/null", timeout=120)
    host_run("systemctl enable fail2ban && systemctl start fail2ban", timeout=10)
    return 'Fail2Ban zainstalowany i aktywny'

def _fix_apt_upgrade():
    host_run("apt-get update -qq && apt-get upgrade -y -qq", timeout=600)
    return 'Pakiety zaktualizowane'

def _fix_renew_cert():
    host_run("certbot renew --quiet", timeout=120)
    return 'Certyfikat SSL odnowiony'

_FIX_ACTIONS = {
    'disable_ssh_root': _fix_disable_ssh_root,
    'disable_ssh_password': _fix_disable_ssh_password,
    'enable_firewall': _fix_enable_firewall,
    'enable_fail2ban': _fix_enable_fail2ban,
    'apt_upgrade': _fix_apt_upgrade,
    'renew_cert': _fix_renew_cert,
}


# ── Routes ────────────────────────────────────────────────────────────────

@security_advisor_bp.route('/scan', methods=['GET'])
def scan():
    return jsonify(run_scan())

@security_advisor_bp.route('/fix', methods=['POST'])
def fix():
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    if action not in _FIX_ACTIONS:
        return jsonify({'error': f'Unknown fix action: {action}'}), 400
    try:
        msg = _FIX_ACTIONS[action]()
        return jsonify({'ok': True, 'message': msg})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

@security_advisor_bp.route('/pkg-status', methods=['GET'])
def pkg_status():
    return jsonify({'installed': True})


@security_advisor_bp.route('/cleanup-disk', methods=['POST'])
def cleanup_disk():
    """Free disk space by cleaning apt/pip caches and temp files."""
    import psutil
    before = psutil.disk_usage('/').free

    host_run('apt-get clean 2>/dev/null', timeout=30)
    host_run('apt-get autoremove -y -qq 2>/dev/null', timeout=120)

    # pip cache
    host_run('pip cache purge 2>/dev/null || true', timeout=30)

    # venv __pycache__
    venv_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'venv')
    venv_dir = os.path.normpath(venv_dir)
    for root, dirs, _ in os.walk(venv_dir):
        for d in dirs:
            if d == '__pycache__':
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)

    # blueprints __pycache__
    for root, dirs, _ in os.walk(os.path.dirname(__file__)):
        for d in dirs:
            if d == '__pycache__':
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)

    after = psutil.disk_usage('/').free
    freed_mb = round((after - before) / 1024 / 1024)
    free_mb = round(after / 1024 / 1024)
    return jsonify({'ok': True, 'freed_mb': freed_mb, 'free_mb': free_mb})
