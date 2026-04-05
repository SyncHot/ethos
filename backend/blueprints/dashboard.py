"""
EthOS Dashboard Blueprint — system summary API.
"""

import os
import json
import time
import socket
from datetime import timedelta

from flask import Blueprint, jsonify

import psutil

from host import host_run, q
from utils import require_tools, check_tool

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/api/dashboard')

INSTALL_CONF = '/opt/ethos/install.conf'
VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'version.json')


# ── helpers ──────────────────────────────────────────────────────

def _run(cmd, timeout=5):
    r = host_run(cmd, timeout=timeout)
    return r.stdout.strip() if r.returncode >= 0 else ''


def _read_install_conf():
    """Parse install.conf into a dict."""
    data = {}
    try:
        with open(INSTALL_CONF, 'r') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, _, val = line.partition('=')
                    data[key.strip()] = val.strip().strip('"').strip("'")
    except Exception:
        pass
    return data


def _get_version():
    try:
        with open(VERSION_FILE, 'r') as f:
            v = json.load(f)
        return v.get('version', 'unknown')
    except Exception:
        return 'unknown'


def _format_uptime(seconds):
    td = timedelta(seconds=int(seconds))
    days = td.days
    hours, rem = divmod(td.seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return ' '.join(parts)


def _get_temperatures():
    """Read thermal zone temperatures."""
    temps = []
    base = '/sys/class/thermal'
    try:
        for zone in sorted(os.listdir(base)):
            if not zone.startswith('thermal_zone'):
                continue
            temp_path = os.path.join(base, zone, 'temp')
            type_path = os.path.join(base, zone, 'type')
            try:
                with open(temp_path) as f:
                    val = int(f.read().strip()) / 1000.0
                label = zone
                try:
                    with open(type_path) as f:
                        label = f.read().strip()
                except Exception:
                    pass
                temps.append({'label': label, 'celsius': round(val, 1)})
            except Exception:
                continue
    except Exception:
        pass
    return temps


def _get_docker_info():
    """Get running/total docker container counts."""
    if not check_tool('docker'):
        return None
    try:
        out = _run("docker ps -a --format '{{.Status}}'", timeout=4)
        if not out:
            return None
        lines = [l for l in out.splitlines() if l.strip()]
        total = len(lines)
        running = sum(1 for l in lines if l.lower().startswith('up'))
        return {'running': running, 'total': total}
    except Exception:
        return None


def _get_shares_count():
    """Count active Samba and NFS shares."""
    samba = 0
    nfs = 0
    try:
        out = _run("grep -c '^\\[' /etc/samba/smb.conf 2>/dev/null || echo 0")
        raw = int(out) if out.isdigit() else 0
        samba = max(0, raw - 1)  # subtract [global]
    except Exception:
        pass
    try:
        out = _run("grep -cv '^#\\|^$' /etc/exports 2>/dev/null || echo 0")
        nfs = int(out) if out.isdigit() else 0
    except Exception:
        pass
    return {'samba': samba, 'nfs': nfs}


def _service_active(name):
    """Check if a systemd service is active."""
    code = _run(f"systemctl is-active {q(name)} 2>/dev/null")
    return code == 'active'


# ── route ────────────────────────────────────────────────────────

@dashboard_bp.route('/summary', methods=['GET'])
def summary():
    conf = _read_install_conf()

    # System info
    hostname = socket.gethostname()
    uptime_sec = time.time() - psutil.boot_time()
    version = _get_version()

    # CPU
    cpu_percent = psutil.cpu_percent(interval=0.3)
    cpu_count = psutil.cpu_count(logical=True)
    freq = psutil.cpu_freq()

    # Memory
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disks — only real mounted filesystems
    disks = []
    seen_mounts = set()
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen_mounts:
            continue
        if part.fstype in ('squashfs', 'overlay', 'tmpfs', 'devtmpfs'):
            continue
        seen_mounts.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                'device': part.device,
                'mountpoint': part.mountpoint,
                'fstype': part.fstype,
                'total': usage.total,
                'used': usage.used,
                'free': usage.free,
                'percent': usage.percent,
            })
        except (PermissionError, OSError):
            continue

    # Network interfaces with IPs
    interfaces = []
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    for iface, addr_list in sorted(addrs.items()):
        if iface == 'lo':
            continue
        ips = [a.address for a in addr_list if a.family == socket.AF_INET]
        is_up = stats.get(iface, None)
        interfaces.append({
            'name': iface,
            'ips': ips,
            'is_up': is_up.isup if is_up else False,
            'speed': is_up.speed if is_up else 0,
        })

    # Docker
    docker = _get_docker_info()

    # Shares
    shares = _get_shares_count()

    # Services
    services = {
        'samba': _service_active('smbd'),
        'nfs': _service_active('nfs-kernel-server'),
        'docker': _service_active('docker'),
        'ssh': _service_active('sshd') or _service_active('ssh'),
    }

    # Temperature
    temperatures = _get_temperatures()

    return jsonify({
        'hostname': hostname,
        'nas_name': conf.get('ETHOS_NAS_NAME', 'EthOS'),
        'uptime': _format_uptime(uptime_sec),
        'uptime_seconds': int(uptime_sec),
        'version': version,
        'cpu': {
            'percent': cpu_percent,
            'cores': cpu_count,
            'freq_mhz': round(freq.current, 0) if freq else None,
        },
        'ram': {
            'total': mem.total,
            'used': mem.used,
            'percent': mem.percent,
        },
        'swap': {
            'total': swap.total,
            'used': swap.used,
            'percent': swap.percent,
        },
        'disks': disks,
        'network': interfaces,
        'docker': docker,
        'shares': shares,
        'services': services,
        'temperatures': temperatures,
    })
