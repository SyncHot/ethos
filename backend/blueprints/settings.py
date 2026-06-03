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
from audit import audit_log

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
        'auto_update': env.get('AUTO_UPDATE', '') in ('true', '1'),
    })


@settings_bp.route('/', methods=['POST'])
def update_settings():
    """Update system settings. Returns which settings changed and if restart is needed."""
    if g.role != 'admin':
        return jsonify({'error': 'Only admin can change settings'}), 403

    data = request.json or {}
    changes = []
    restart_needed = False
    errors = []

    # Reject completely empty submissions
    recognized_keys = {'nas_name', 'port', 'hostname', 'timezone', 'auto_update',
                       'backup_dir', 'ethos_root'}
    if not data.keys() & recognized_keys:
        return jsonify({'error': 'Brak rozpoznanych ustawień do zmiany'}), 400

    env = _read_env()
    current_port = int(env.get('PORT', '9000'))
    current_name = env.get('NAS_NAME', 'EthOS')

    # ── NAS Name ──
    new_name = data.get('nas_name', '').strip()
    if new_name and new_name != current_name:
        _write_env_key('NAS_NAME', new_name)
        # Update in-memory value in main app
        os.environ['NAS_NAME'] = new_name
        changes.append(f'NAS name changed to: {new_name}')

    # ── Port ──
    new_port = data.get('port')
    if new_port is not None:
        try:
            new_port = int(new_port)
            if new_port < 1 or new_port > 65535:
                errors.append('Port must be in range 1-65535')
            elif new_port != current_port:
                _write_env_key('PORT', str(new_port))
                changes.append(f'Port changed to: {new_port}')
                restart_needed = True
        except (ValueError, TypeError):
            errors.append('Invalid port number')

    # ── Hostname ──
    new_hostname = data.get('hostname', '').strip()
    if new_hostname:
        # Sanitize
        new_hostname = re.sub(r'[^a-zA-Z0-9\-]', '', new_hostname)
        if new_hostname and new_hostname != _get_hostname():
            r = _host_run(f'hostnamectl set-hostname {shlex.quote(new_hostname)}', timeout=10)
            if r.returncode == 0:
                changes.append(f'Hostname changed to: {new_hostname}')
            else:
                errors.append(f'Hostname change error: {r.stderr.strip()}')

    # ── Timezone ──
    new_tz = data.get('timezone', '').strip()
    if new_tz and new_tz != _get_timezone():
        r = _host_run(f'timedatectl set-timezone {shlex.quote(new_tz)}', timeout=10)
        if r.returncode == 0:
            changes.append(f'Timezone changed to: {new_tz}')
        else:
            errors.append(f'Timezone change error: {r.stderr.strip()}')

    if errors and not changes:
        return jsonify({'ok': False, 'errors': errors}), 400

    if changes:
        audit_log('system.settings.change', f'Settings changed: {"; ".join(changes)}')

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
        return jsonify({'error': 'Both password fields are required'}), 400

    from blueprints.users import validate_password_strength
    pw_ok, pw_errors = validate_password_strength(new_pw, g.username)
    if not pw_ok:
        return jsonify({'error': pw_errors[0], 'password_errors': pw_errors}), 400

    username = g.username
    if not username:
        return jsonify({'error': 'User not recognized'}), 401

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
                return jsonify({'error': 'User not found in system'}), 404

        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=DeprecationWarning)
            import crypt
            if crypt.crypt(current_pw, stored_hash) != stored_hash:
                return jsonify({'error': 'Invalid current password'}), 403
    except PermissionError:
        return jsonify({'error': 'No permission to verify password'}), 500

    # Change password
    safe = shlex.quote(f'{username}:{new_pw}')
    r = _host_run(f'echo {safe} | chpasswd', timeout=10)
    if r.returncode != 0:
        return jsonify({'error': f'Password change error: {r.stderr.strip()}'}), 500

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

    audit_log('user.password.change', f'User "{username}" changed own password')
    return jsonify({'status': 'ok'})


@settings_bp.route('/timezones', methods=['GET'])
def list_tz():
    """Return list of available timezones."""
    return jsonify(_list_timezones())


@settings_bp.route('/restart', methods=['POST'])
def restart_app():
    """Restart EthOS service (after settings change)."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403

    import gevent
    def _do_restart():
        try:
            subprocess.run(['systemctl', 'restart', 'ethos'], timeout=60)
        except Exception:
            pass

    gevent.spawn_later(1, _do_restart)
    return jsonify({'status': 'ok'})


@settings_bp.route('/auto-update', methods=['POST'])
def toggle_auto_update():
    """Toggle automatic system updates."""
    if g.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    data = request.json or {}
    enabled = bool(data.get('enabled', False))
    _write_env_key('AUTO_UPDATE', 'true' if enabled else 'false')
    audit_log('system.settings.change', f'Auto-update {"enabled" if enabled else "disabled"}')
    return jsonify({'ok': True, 'auto_update': enabled})


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
        return jsonify({'error': 'Admin only'}), 403

    if _FACTORY_RESET_LOCK:
        return jsonify({'error': 'Reset is already in progress'}), 409

    data = request.json or {}
    confirm_text = (data.get('confirm') or '').strip()
    if confirm_text != 'RESET':
        return jsonify({'error': 'Type RESET to confirm'}), 400

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
            'message': 'Factory reset complete. Server will restart with the setup wizard.',
            'steps': steps,
        })

    except Exception as e:
        return jsonify({'error': f'Reset error: {e}'}), 500
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
        return jsonify({'error': 'Only admin can view Fail2Ban status'}), 403
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
        return jsonify({'error': 'Only admin can unban IP addresses'}), 403
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
            return jsonify({'status': 'ok'})
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


# ── Config Export / Import ──────────────────────────────────────────────

import zipfile
import io
import tempfile
from flask import send_file

_CONFIG_EXPORT_FILES = [
    'notifications_config.json',
    'shares.json',
    'folder_passwords.json',
    'power_config.json',
    'update_config.json',
    'ssl_config.json',
    'domains.json',
    'ssh_configs.json',
    'ddns_config.json',
    'ethos_packages.json',
    'installed_apps.json',
    'privileges.json',
    'sandbox_policies.json',
    'surveillance_settings.json',
    'surveillance_cameras.json',
    'rollback_auto.json',
    'desktop_apps.json',
    'antivirus_schedules.json',
    'cloud_backup.json',
]

_SYSTEM_CONFIG_FILES = [
    ('/etc/samba/smb.conf', 'system/smb.conf'),
    ('/etc/exports', 'system/exports'),
]


@settings_bp.route('/config/export', methods=['POST'])
def export_config():
    """Export system config as a ZIP bundle."""

    buf = io.BytesIO()
    data_dir = _data_path()
    manifest = {
        'version': 1,
        'exported': datetime.now().isoformat(),
        'files': [],
    }

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in _CONFIG_EXPORT_FILES:
            fpath = os.path.join(data_dir, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, f'data/{fname}')
                manifest['files'].append(f'data/{fname}')

        env_path = os.path.join(ETHOS_ROOT, 'ethos.env')
        if os.path.isfile(env_path):
            zf.write(env_path, 'ethos.env')
            manifest['files'].append('ethos.env')

        for src, dst in _SYSTEM_CONFIG_FILES:
            if os.path.isfile(src):
                zf.write(src, dst)
                manifest['files'].append(dst)

        r = _host_run("awk -F: '$3 >= 1000 && $3 < 65534 {print $1\":\"$6\":\"$7}' /etc/passwd", timeout=5)
        if r.returncode == 0:
            zf.writestr('system/users.txt', r.stdout)
            manifest['files'].append('system/users.txt')

        r = _host_run("awk -F: '$3 >= 1000 {print}' /etc/group", timeout=5)
        if r.returncode == 0:
            zf.writestr('system/groups.txt', r.stdout)
            manifest['files'].append('system/groups.txt')

        r = _host_run("crontab -l 2>/dev/null", timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            zf.writestr('system/crontab.txt', r.stdout)
            manifest['files'].append('system/crontab.txt')

        zf.writestr('manifest.json', json.dumps(manifest, indent=2))

    buf.seek(0)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True,
                     download_name=f'ethos_config_{ts}.zip')


@settings_bp.route('/config/import', methods=['POST'])
def import_config():
    """Import system config from uploaded ZIP bundle."""

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    f = request.files['file']
    if not f.filename.endswith('.zip'):
        return jsonify({'error': 'File must be a .zip archive'}), 400

    try:
        zdata = io.BytesIO(f.read())
        zf = zipfile.ZipFile(zdata)
    except Exception:
        return jsonify({'error': 'Invalid ZIP file'}), 400

    try:
        manifest = json.loads(zf.read('manifest.json'))
    except Exception:
        return jsonify({'error': 'Missing or invalid manifest.json'}), 400

    if manifest.get('version') != 1:
        return jsonify({'error': 'Unsupported config version'}), 400

    imported = []
    errors = []
    data_dir = _data_path()

    for name in zf.namelist():
        if name == 'manifest.json':
            continue

        try:
            content = zf.read(name)

            if name.startswith('data/'):
                fname = name[5:]
                if fname in _CONFIG_EXPORT_FILES:
                    dst = os.path.join(data_dir, fname)
                    with open(dst, 'wb') as out:
                        out.write(content)
                    imported.append(fname)

            elif name == 'ethos.env':
                dst = os.path.join(ETHOS_ROOT, 'ethos.env')
                with open(dst, 'wb') as out:
                    out.write(content)
                imported.append('ethos.env')

            elif name == 'system/smb.conf':
                with open('/etc/samba/smb.conf', 'wb') as out:
                    out.write(content)
                _host_run("systemctl restart smbd 2>/dev/null", timeout=10)
                imported.append('smb.conf')

            elif name == 'system/exports':
                with open('/etc/exports', 'wb') as out:
                    out.write(content)
                _host_run("exportfs -ra 2>/dev/null", timeout=10)
                imported.append('exports')

            elif name == 'system/crontab.txt':
                tmp = tempfile.NamedTemporaryFile(mode='wb', suffix='.cron', delete=False)
                tmp.write(content)
                tmp.close()
                _host_run(f"crontab {tmp.name}", timeout=5)
                os.unlink(tmp.name)
                imported.append('crontab')

        except Exception as e:
            errors.append(f'{name}: {e}')

    zf.close()

    try:
        audit_log('Config imported', details={'imported': imported, 'errors': errors})
    except Exception:
        pass

    return jsonify({
        'ok': True,
        'imported': imported,
        'errors': errors,
        'message': f'Imported {len(imported)} items' + (f', {len(errors)} errors' if errors else '')
    })


# ── Import sub-modules to register routes ──
# These imports must come after blueprint definition to avoid import lock issues
try:
    from blueprints import settings_ssl
except ImportError:
    pass
try:
    from blueprints import settings_domains
except ImportError:
    pass
try:
    from blueprints import settings_ssh
except ImportError:
    pass
