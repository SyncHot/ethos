"""
EthOS — VM Manager (Virtual Machine Manager)
Create and manage virtual machines using QEMU/KVM.
Supports booting ISO, IMG, QCOW2 and VDI images.
Supports multiple disks per VM (add, remove, resize).

Endpoints:
  GET    /api/vm/machines                               — list VMs
  POST   /api/vm/machines                               — create VM
  GET    /api/vm/machines/<id>                           — get VM
  PUT    /api/vm/machines/<id>                           — update VM config
  DELETE /api/vm/machines/<id>                           — delete VM
  POST   /api/vm/machines/<id>/start                    — start VM
  POST   /api/vm/machines/<id>/stop                     — stop VM
  GET    /api/vm/machines/<id>/disk-info                 — boot disk info (compat)
  POST   /api/vm/machines/<id>/resize-disk               — resize boot disk (compat)
  GET    /api/vm/machines/<id>/disks                     — list all disks
  POST   /api/vm/machines/<id>/disks                     — add disk
  DELETE /api/vm/machines/<id>/disks/<disk_id>           — remove disk
  POST   /api/vm/machines/<id>/disks/<disk_id>/resize   — resize specific disk
  GET    /api/vm/machines/<id>/snapshots                 — list snapshots
  POST   /api/vm/machines/<id>/snapshots                 — create snapshot
  POST   /api/vm/machines/<id>/snapshots/<tag>           — restore snapshot
  DELETE /api/vm/machines/<id>/snapshots/<tag>           — delete snapshot
  POST   /api/vm/quick-create-ethos                      — quick-create EthOS VM
  POST   /api/vm/import-disk                             — import disk image
  POST   /api/vm/convert                                 — convert disk format
  GET    /api/vm/machines/<id>/installer-logs            — proxy installer logs from running VM
"""

import os
import json
import re
import shutil
import signal
import subprocess
import sys
import time
import threading
import urllib.request
from functools import wraps
from flask import Blueprint, request, jsonify, send_from_directory, abort
from blueprints.admin_required import admin_required

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, check_dep, ensure_dep, get_data_disk as _get_data_disk, app_path as _app_path, q
from utils import register_pkg_routes, require_tools, check_tool

vm_bp = Blueprint('vm_mgr', __name__, url_prefix='/api/vm')

# ─── Paths ───────────────────────────────────────────────────

_DEFAULT_VM_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'vms')


def _vm_root():
    """Directory where VM configs and disks are stored."""
    dd = _get_data_disk()
    if dd:
        p = os.path.join(dd, 'vms')
    else:
        p = os.path.abspath(_DEFAULT_VM_DIR)
    os.makedirs(p, exist_ok=True)
    return p


def _iso_root():
    """Directory where ISO/IMG files are stored."""
    p = os.path.join(_vm_root(), '_images')
    os.makedirs(p, exist_ok=True)
    return p


# ─── State ───────────────────────────────────────────────────

_STATE_FILE = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'vm_state.json')
_running_vms = {}  # vm_id -> { 'proc': Popen, 'pid': int, 'started': float, 'vnc_port': int, 'serial_port': int }


def _load_vms():
    """Load VM definitions from the state file, auto-migrating legacy format."""
    try:
        with open(_STATE_FILE, 'r') as f:
            vms = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    migrated = False
    for vm_id, vm in vms.items():
        if 'disks' not in vm and vm.get('disk_file'):
            vm['disks'] = [{
                'id': 'disk0',
                'file': vm['disk_file'],
                'format': vm.get('disk_format', 'qcow2'),
                'size': vm.get('disk_size', ''),
                'bus': 'virtio',
            }]
            migrated = True
    if migrated:
        _save_vms(vms)
    return vms


def _save_vms(vms):
    """Save VM definitions to the state file."""
    os.makedirs(os.path.dirname(os.path.abspath(_STATE_FILE)), exist_ok=True)
    with open(_STATE_FILE, 'w') as f:
        json.dump(vms, f, indent=2)


def _next_disk_id(vm):
    """Return the next available disk ID (disk0, disk1, ...)."""
    existing = {d['id'] for d in vm.get('disks', [])}
    for i in range(100):
        did = f'disk{i}'
        if did not in existing:
            return did
    return f'disk{len(existing)}'


def _get_disk(vm, disk_id):
    """Find a disk entry by ID, or None."""
    for d in vm.get('disks', []):
        if d['id'] == disk_id:
            return d
    return None


def _is_ethos_image(boot_image):
    """Check if a boot image filename looks like an EthOS image."""
    if not boot_image:
        return False
    name = os.path.basename(boot_image).lower()
    return 'ethos' in name


def _used_host_ports():
    """Collect all host ports already mapped by existing VMs."""
    used = set()
    try:
        vms = _load_vms()
    except Exception:
        vms = {}
    for vm in vms.values():
        net = vm.get('network') or {}
        for pf in net.get('port_forwards', []):
            hp = int(pf.get('host', 0))
            if hp > 0:
                used.add(hp)
    return used


def _find_free_host_port(preferred, used_ports):
    """Find a free host port starting from preferred, skipping used and busy ports."""
    import socket
    candidate = preferred
    for _ in range(200):
        if candidate in used_ports:
            candidate += 1
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(('', candidate))
            s.close()
            return candidate
        except OSError:
            candidate += 1
        finally:
            s.close()
    return 0


def _default_network(os_type='linux', boot_image=''):
    """Return sensible default network config based on OS type.
    When boot_image looks like an EthOS image, auto-maps ports 9000, 9443 and 22
    to free host ports that don't conflict with the host or other VMs.
    """
    pf = []
    if _is_ethos_image(boot_image):
        used = _used_host_ports()
        ethos_port = _find_free_host_port(9000, used)
        used.add(ethos_port)
        https_port = _find_free_host_port(9443, used)
        used.add(https_port)
        ssh_port = _find_free_host_port(2222, used)
        pf.append({'proto': 'tcp', 'host': ethos_port, 'guest': 9000, 'label': 'EthOS Web'})
        pf.append({'proto': 'tcp', 'host': https_port, 'guest': 9443, 'label': 'EthOS HTTPS'})
        pf.append({'proto': 'tcp', 'host': ssh_port, 'guest': 22, 'label': 'SSH'})
    elif os_type == 'linux':
        pf.append({'proto': 'tcp', 'host': 0, 'guest': 22, 'label': 'SSH'})
    elif os_type == 'windows':
        pf.append({'proto': 'tcp', 'host': 0, 'guest': 3389, 'label': 'RDP'})
    return {'net_type': 'user', 'port_forwards': pf}


def _validate_port_forwards(forwards):
    """Validate and sanitize a list of port forward rules."""
    clean = []
    for rule in (forwards or []):
        proto = str(rule.get('proto', 'tcp')).lower()
        if proto not in ('tcp', 'udp'):
            proto = 'tcp'
        host = int(rule.get('host', 0))
        guest = int(rule.get('guest', 0))
        if guest < 1 or guest > 65535:
            continue
        if host < 0 or host > 65535:
            host = 0
        label = str(rule.get('label', ''))[:32]
        clean.append({'proto': proto, 'host': host, 'guest': guest, 'label': label})
    return clean


_INTERNAL_PORT_BASE = 19000


def _find_free_internal_port(host_port):
    """Find a free localhost port for QEMU's internal hostfwd binding."""
    import socket
    # Try deterministic offset first for debuggability
    candidate = _INTERNAL_PORT_BASE + (host_port % 1000)
    for _ in range(100):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(('127.0.0.1', candidate))
            s.close()
            return candidate
        except OSError:
            candidate += 1
        finally:
            s.close()
    raise RuntimeError(f'Cannot find free internal port for hostfwd (host_port={host_port})')


_SOCAT_BIN = '/usr/bin/socat'


def _start_socat_proxies(proxy_map):
    """Start socat TCP proxies for QEMU user-mode port forwards.
    proxy_map: {public_host_port: internal_localhost_port}.
    Returns list of Popen objects.
    """
    procs = []
    if not proxy_map or not os.path.isfile(_SOCAT_BIN):
        return procs
    for host_port, internal_port in proxy_map.items():
        try:
            proc = subprocess.Popen(
                [_SOCAT_BIN,
                 f'TCP-LISTEN:{host_port},fork,reuseaddr',
                 f'TCP:127.0.0.1:{internal_port}'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(0.3)
            if proc.poll() is not None:
                log.error("socat proxy :%s→:%s exited early", host_port, internal_port)
            else:
                log.info("socat proxy :%s → 127.0.0.1:%s (pid %s)",
                         host_port, internal_port, proc.pid)
                procs.append(proc)
        except Exception as e:
            log.error("Failed to start socat proxy :%s: %s", host_port, e)
    return procs


def _stop_socat_proxies(info):
    """Stop all socat proxy processes associated with a VM."""
    for proc in info.get('socat_procs', []):
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass


def _build_net_opts(vm):
    """Build QEMU -netdev options string from VM network config.
    Returns (netdev_args_list, tap_device_or_None, proxy_map).
    proxy_map is {host_port: internal_port} for TCP proxies (user-mode only).
    """
    net = vm.get('network') or _default_network(vm.get('os_type', 'linux'))
    net_type = net.get('net_type', 'user')

    if net_type == 'none':
        return None, None, {}

    if net_type == 'bridge':
        bridge = net.get('bridge', 'br0')
        if not _validate_bridge_name(bridge):
            log.warning("Invalid bridge name rejected: %s", bridge)
            bridge = _BRIDGE_NAME
        tap = _create_tap(bridge)
        if not tap:
            # Fallback to user mode if bridge setup fails
            log.warning("Bridge setup failed, falling back to user mode")
            net_type = 'user'
        else:
            return ['-netdev', f'tap,id=net0,ifname={tap},script=no,downscript=no',
                    '-device', 'virtio-net-pci,netdev=net0'], tap, {}

    # User-mode NAT — check port availability first
    opts = 'user,id=net0'
    proxy_map = {}  # {public_host_port: internal_localhost_port}
    for rule in net.get('port_forwards', []):
        proto = rule.get('proto', 'tcp')
        host = rule.get('host', 0)
        guest = rule.get('guest', 0)
        if guest and host:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('', int(host)))
                s.close()
            except OSError:
                raise RuntimeError(
                    f'Port {host} jest zajęty (inny proces go używa). '
                    f'Zmień port hosta w ustawieniach sieci VM lub zwolnij port.')
            # QEMU user-mode networking has a tiny TCP backlog (1) and poor
            # connection cleanup, causing CLOSE-WAIT accumulation that blocks
            # new external connections.  Bind QEMU to localhost on an internal
            # port and use a socat TCP proxy on the public port instead.
            internal = _find_free_internal_port(int(host))
            proxy_map[int(host)] = internal
            opts += f',hostfwd={proto}:127.0.0.1:{internal}-:{guest}'
    return ['-netdev', opts, '-device', 'virtio-net-pci,netdev=net0'], None, proxy_map


# ─── Bridge Networking ────────────────────────────────────────

_BRIDGE_NAME = 'br0'


def _validate_bridge_name(name):
    """Validate bridge interface name to prevent shell injection."""
    if not name or not isinstance(name, str):
        return False
    return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9\-]{0,14}$', name))

log = __import__('logging').getLogger('vm-manager')


def _get_primary_iface():
    """Detect the primary ethernet interface (carries default route)."""
    try:
        r = subprocess.run(
            "ip -4 route show default | awk '{print $5}' | head -1",
            shell=True, capture_output=True, text=True, timeout=5
        )
        iface = r.stdout.strip()
        if iface and not iface.startswith(('br', 'docker', 'veth', 'virbr')):
            return iface
        # If default route is already on a bridge, check for slave interfaces
        if iface and iface.startswith('br'):
            return iface  # bridge itself is fine
    except Exception:
        pass
    return None


def _bridge_exists(br='br0'):
    """Check if a bridge interface exists."""
    if not _validate_bridge_name(br):
        return False
    try:
        r = subprocess.run(
            f'ip link show {br} type bridge',
            shell=True, capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def _bridge_status():
    """Return bridge status info for the API."""
    br = _BRIDGE_NAME
    exists = _bridge_exists(br)
    primary = _get_primary_iface()
    br_ip = None
    if exists:
        try:
            r = subprocess.run(
                f"ip -4 -o addr show {br} scope global | awk '{{print $4}}' | cut -d/ -f1 | head -1",
                shell=True, capture_output=True, text=True, timeout=5
            )
            br_ip = r.stdout.strip() or None
        except Exception:
            pass
    return {
        'bridge': br,
        'exists': exists,
        'bridge_ip': br_ip,
        'primary_iface': primary,
        'ready': exists and br_ip is not None,
    }


def _setup_bridge():
    """Create br0 bridge and slave the primary ethernet interface to it via nmcli.

    Cleans up duplicate/stale br0 and br0-port connections before creating fresh ones.
    Properly disconnects the existing ethernet connection so eth0 can join the bridge.
    """
    br = _BRIDGE_NAME

    # Already working?
    st = _bridge_status()
    if st['ready']:
        return True, 'Bridge already configured'

    primary = _get_primary_iface()
    if not primary:
        return False, 'No primary ethernet interface found'
    if primary.startswith('br'):
        return True, f'Already using bridge {primary}'

    try:
        def nmcli(cmd):
            return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)

        # 1. Remove all stale br0 and br0-port connections to avoid duplicates
        show = nmcli('nmcli -t -f NAME,UUID connection show')
        for line in show.stdout.splitlines():
            parts = line.split(':')
            if len(parts) >= 2:
                name, uuid = parts[0], parts[1]
                if name in (br, f'{br}-port'):
                    nmcli(f'nmcli connection delete {uuid}')

        # 2. Get MAC of primary interface so bridge gets same DHCP lease
        mac_r = subprocess.run(
            f"ip link show {primary} | grep -o 'link/ether [^ ]*' | awk '{{print $2}}'",
            shell=True, capture_output=True, text=True, timeout=5
        )
        primary_mac = mac_r.stdout.strip()

        # 2. Create the bridge connection, cloning MAC so DHCP assigns the same IP
        mac_opt = f' 802-3-ethernet.cloned-mac-address {primary_mac}' if primary_mac else ''
        r = nmcli(f'nmcli connection add type bridge con-name {br} ifname {br} stp no autoconnect yes{mac_opt}')
        if r.returncode != 0:
            return False, f'create bridge: {r.stderr.strip()}'

        # 3. Create the bridge-slave for the primary ethernet interface
        r = nmcli(f'nmcli connection add type ethernet con-name {br}-port ifname {primary} master {br} slave-type bridge autoconnect yes')
        if r.returncode != 0:
            return False, f'create bridge-port: {r.stderr.strip()}'

        # 4. Find and disconnect the existing non-slave connection on primary iface
        show = nmcli('nmcli -t -f NAME,UUID,DEVICE connection show --active')
        for line in show.stdout.splitlines():
            parts = line.split(':')
            if len(parts) >= 3 and parts[2] == primary:
                nmcli(f'nmcli connection down {parts[1]}')

        # 5. Activate the slave so eth0 joins the bridge
        nmcli(f'nmcli connection up {br}-port')

        # 6. Bring up the bridge
        nmcli(f'nmcli connection up {br}')

        # Wait for bridge to get IP via DHCP
        for _ in range(15):
            time.sleep(1)
            st = _bridge_status()
            if st['ready']:
                return True, f'Bridge {br} active with IP {st["bridge_ip"]}'
        return False, 'Bridge created but did not get an IP (DHCP timeout — check router/DHCP)'
    except Exception as e:
        return False, str(e)


def _create_tap(bridge='br0'):
    """Create a TAP device and attach to bridge. Returns tap name or None."""
    if not _bridge_exists(bridge):
        ok, msg = _setup_bridge()
        if not ok:
            log.error("Cannot setup bridge: %s", msg)
            return None

    # Find next available tap name
    for i in range(100):
        tap = f'vmtap{i}'
        r = subprocess.run(
            f'ip link show {tap}', shell=True, capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            break
    else:
        return None

    try:
        cmds = [
            f'ip tuntap add dev {tap} mode tap',
            f'ip link set {tap} master {bridge}',
            f'ip link set {tap} up',
        ]
        for cmd in cmds:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                log.error("TAP setup failed: %s → %s", cmd, r.stderr.strip())
                subprocess.run(f'ip link del {tap} 2>/dev/null', shell=True, timeout=5)
                return None
        return tap
    except Exception as e:
        log.error("TAP creation error: %s", e)
        return None


def _destroy_tap(tap):
    """Remove a TAP device."""
    if tap:
        try:
            subprocess.run(f'ip link del {tap}', shell=True, capture_output=True, timeout=5)
        except Exception:
            pass


# ─── Helpers ─────────────────────────────────────────────────

def _allowed_image_roots():
    roots = [
        os.path.realpath(_iso_root()),
        os.path.realpath(_vm_root()),
    ]
    builder_images = _app_path('installer/images')
    if builder_images:
        roots.append(os.path.realpath(builder_images))
    # Allow ISOs from mounted drives
    for mnt in ('/media', '/mnt'):
        if os.path.isdir(mnt):
            roots.append(os.path.realpath(mnt))
    return [r.rstrip(os.sep) for r in roots if r]


def _is_allowed_image_path(path):
    """Check if a path stays within permitted VM image directories."""
    real = os.path.realpath(path or '')
    return any(real == root or real.startswith(root + os.sep) for root in _allowed_image_roots())


def _qemu_available():
    return check_dep('qemu-system-x86_64')


def _arm_qemu_available():
    """Check if qemu-system-aarch64 is available for ARM emulation."""
    try:
        r = host_run('which qemu-system-aarch64', timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _is_arm_image(boot_image, vm_name=''):
    """Detect if an image is ARM-based (rpi, arm, aarch64) by filename."""
    check = (os.path.basename(boot_image or '') + ' ' + vm_name).lower()
    return any(tag in check for tag in ('rpi', 'raspberry', 'arm64', 'aarch64', 'armhf', '-arm'))


def _is_rpi_image(boot_image, vm_name=''):
    """Detect if an image is specifically a Raspberry Pi image (needs -machine raspi3b)."""
    check = (os.path.basename(boot_image or '') + ' ' + vm_name).lower()
    return any(tag in check for tag in ('rpi', 'raspberry', 'raspios', 'raspi'))


def _raspi_machine_available():
    """Check if QEMU supports the raspi3b machine type."""
    try:
        r = host_run('qemu-system-aarch64 -machine help 2>/dev/null | grep -q raspi3b && echo yes', timeout=5)
        return r.stdout.strip() == 'yes'
    except Exception:
        return False


def _kvm_available():
    """Check if KVM hardware acceleration is available."""
    try:
        r = host_run('test -e /dev/kvm && echo yes || echo no', timeout=5)
        return r.stdout.strip() == 'yes'
    except Exception:
        return False


def _require_qemu(f):
    """Decorator: return 503 if QEMU is not installed."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _qemu_available():
            return jsonify({'error': 'QEMU is not installed. Install the VM Manager package.'}), 503
        return f(*args, **kwargs)
    return decorated


def _next_vnc_port():
    """Find the next available VNC display number (port = 5900 + display)."""
    used = {v.get('vnc_port', 0) for v in _running_vms.values()}
    for display in range(1, 100):
        port = 5900 + display
        if port not in used:
            return display, port
    return 99, 5999


def _next_ws_port():
    """Find the next available WebSocket port for noVNC (6080+)."""
    used = {v.get('ws_port', 0) for v in _running_vms.values()}
    for p in range(6080, 6180):
        if p not in used:
            return p
    return 6179


def _next_serial_port():
    """Find the next available TCP port for serial console (4000+)."""
    used = {v.get('serial_port', 0) for v in _running_vms.values()}
    for p in range(4000, 4100):
        if p not in used:
            return p
    return 4099


def _next_serial_ws_port():
    """Find the next available WebSocket port for serial console (6180+)."""
    used = {v.get('serial_ws_port', 0) for v in _running_vms.values()}
    for p in range(6180, 6280):
        if p not in used:
            return p
    return 6279


_NOVNC_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'novnc')
_WEBSOCKIFY_BIN = os.path.join(os.path.dirname(__file__), '..', '..', 'venv', 'bin', 'websockify')


def _start_websockify(vnc_port, ws_port):
    """Start websockify to proxy WebSocket→VNC for noVNC browser client."""
    novnc_dir = os.path.abspath(_NOVNC_DIR)
    ws_bin = os.path.abspath(_WEBSOCKIFY_BIN)
    if not os.path.isfile(ws_bin):
        return None
    try:
        proc = subprocess.Popen(
            [ws_bin, '--web', novnc_dir, str(ws_port), f'localhost:{vnc_port}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(1.0)
        if proc.poll() is not None:
            out = proc.stderr.read().decode('utf-8', errors='replace')[:300]
            log.error("websockify exited early: %s", out)
            return None
        return proc
    except Exception:
        return None


def _stop_websockify(info):
    """Stop the websockify process associated with a VM."""
    ws_proc = info.get('ws_proc')
    if ws_proc:
        try:
            ws_proc.terminate()
            try:
                ws_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                ws_proc.kill()
        except Exception:
            pass


def _start_serial_websockify(serial_port, serial_ws_port):
    """Start websockify to proxy WebSocket→serial TCP for xterm.js browser client."""
    ws_bin = os.path.abspath(_WEBSOCKIFY_BIN)
    if not os.path.isfile(ws_bin):
        return None
    try:
        proc = subprocess.Popen(
            [ws_bin, str(serial_ws_port), f'localhost:{serial_port}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(1.0)
        if proc.poll() is not None:
            out = proc.stderr.read().decode('utf-8', errors='replace')[:300]
            log.error("serial websockify exited early: %s", out)
            return None
        return proc
    except Exception:
        return None


def _stop_serial_websockify(info):
    """Stop the serial websockify process associated with a VM."""
    proc = info.get('serial_ws_proc')
    if proc:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass


def _sanitize_name(name):
    """Sanitize a VM name for use as a directory name."""
    name = re.sub(r'[^\w\s\-.]', '', name).strip()
    return name[:64] if name else 'unnamed-vm'


def _vm_dir(vm_id):
    """Get the directory for a specific VM."""
    return os.path.join(_vm_root(), vm_id)


def _human_size(size_bytes):
    """Format bytes to human-readable string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(size_bytes) < 1024.0:
            return f'{size_bytes:.1f} {unit}'
        size_bytes /= 1024.0
    return f'{size_bytes:.1f} PB'


def _disk_has_gpt(path):
    """Detect GPT partition table on a disk image (raw or qcow2).

    For raw images we read the GPT header directly (LBA 1, offset 512).
    For qcow2/vmdk/vdi we use `qemu-img dd` to extract the first 1024 bytes.
    Falls back to fdisk/sfdisk if available.
    """
    _GPT_MAGIC = b'EFI PART'

    ext = os.path.splitext(path)[1].lower()
    is_raw = ext in ('.img', '.raw', '.iso')

    # Raw images — read directly
    if is_raw:
        try:
            with open(path, 'rb') as f:
                f.seek(512)
                return f.read(8) == _GPT_MAGIC
        except Exception:
            return False

    # qcow2/vmdk/vdi — use qemu-img dd to extract the first 1024 bytes
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.bin') as tmp:
            subprocess.run(
                ['qemu-img', 'dd', f'if={path}', f'of={tmp.name}',
                 'bs=1024', 'count=1', 'skip=0'],
                capture_output=True, timeout=10)
            data = tmp.read()
            if len(data) >= 520:
                return data[512:520] == _GPT_MAGIC
    except Exception:
        pass

    # Fallback: try fdisk or sfdisk
    for tool in ('fdisk', 'sfdisk'):
        try:
            r = subprocess.run(
                [tool, '-l', path],
                capture_output=True, timeout=5)
            if b'gpt' in r.stdout.lower() or b'GPT' in r.stdout:
                return True
        except Exception:
            continue

    return False


def _check_vm_process(vm_id):
    """Check if a VM process is still running. Clean up if dead."""
    info = _running_vms.get(vm_id)
    if not info:
        return False
    proc = info.get('proc')
    if proc and proc.poll() is None:
        return True
    # Process is dead, clean up websockify and socat proxies too
    _stop_websockify(info)
    _stop_serial_websockify(info)
    _stop_socat_proxies(info)
    _running_vms.pop(vm_id, None)
    return False


# ═══════════════════════════════════════════════════════════
#  STATUS / CAPABILITIES
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/status')
@admin_required
def vm_status():
    """Check QEMU/KVM availability and capabilities."""
    qemu_ok = _qemu_available()
    kvm_ok = _kvm_available() if qemu_ok else False
    return jsonify({
        'available': qemu_ok,
        'kvm': kvm_ok,
        'arm': _arm_qemu_available(),
        'message': None if qemu_ok else 'QEMU is not installed.',
    })


# ═══════════════════════════════════════════════════════════
#  VM CRUD
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/machines')
@admin_required
@_require_qemu
def list_vms():
    """List all virtual machines with their status."""
    vms = _load_vms()
    result = []
    for vm_id, vm in vms.items():
        is_running = _check_vm_process(vm_id)
        info = _running_vms.get(vm_id, {})
        result.append({
            'id': vm_id,
            'name': vm.get('name', vm_id),
            'cpu': vm.get('cpu', 1),
            'ram': vm.get('ram', 1024),
            'disk_size': vm.get('disk_size', '10G'),
            'os_type': vm.get('os_type', 'linux'),
            'boot_image': vm.get('boot_image', ''),
            'autostart': vm.get('autostart', False),
            'status': 'running' if is_running else 'stopped',
            'vnc_port': info.get('vnc_port') if is_running else None,
            'vnc_display': info.get('vnc_display') if is_running else None,
            'ws_port': info.get('ws_port') if is_running else None,
            'serial_ws_port': info.get('serial_ws_port') if is_running else None,
            'pid': info.get('pid') if is_running else None,
            'started': info.get('started') if is_running else None,
            'created': vm.get('created', ''),
            'description': vm.get('description', ''),
            'disk_file': vm.get('disk_file', ''),
            'disks': vm.get('disks', []),
            'network': vm.get('network') or _default_network(vm.get('os_type', 'linux')),
            'arch': 'raspi' if _is_rpi_image(vm.get('boot_image', ''), vm.get('name', ''))
                    else 'aarch64' if _is_arm_image(vm.get('boot_image', ''), vm.get('name', ''))
                    else 'x86_64',
        })
    return jsonify(result)


@vm_bp.route('/machines', methods=['POST'])
@admin_required
@_require_qemu
def create_vm():
    """Create a new virtual machine."""
    err = require_tools('qemu-img')
    if err:
        return err
    data = request.get_json(force=True) if request.data else {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'VM name is required'}), 400

    cpu = int(data.get('cpu', 2))
    ram = int(data.get('ram', 1024))  # MB
    disk_size = data.get('disk_size', '20G')
    os_type = data.get('os_type', 'linux')  # linux, windows, other
    boot_image = data.get('boot_image', '')  # ISO/IMG file to boot from
    description = data.get('description', '')
    disk_format = data.get('disk_format', 'qcow2')  # qcow2, raw
    disk_bus = data.get('disk_bus', 'virtio')  # virtio, scsi, sata, ide

    # Network configuration
    network = data.get('network')
    if network is None:
        network = _default_network(os_type, boot_image)

    # Validate
    if cpu < 1 or cpu > 32:
        return jsonify({'error': 'CPU: 1-32 rdzeni'}), 400
    if ram < 256 or ram > 65536:
        return jsonify({'error': 'RAM: 256 MB - 64 GB'}), 400
    if not re.match(r'^\d+[GMK]?$', disk_size):
        return jsonify({'error': 'Invalid disk size (e.g. 20G, 512M)'}), 400
    if disk_bus not in ('virtio', 'scsi', 'sata', 'ide'):
        return jsonify({'error': 'Disk bus must be virtio, scsi, sata, or ide'}), 400
    if boot_image:
        boot_image_real = os.path.realpath(boot_image)
        if not _is_allowed_image_path(boot_image_real):
            return jsonify({'error': 'Image path not allowed'}), 403
        boot_image = boot_image_real

    vm_id = _sanitize_name(name).lower().replace(' ', '-')
    vm_id = re.sub(r'-+', '-', vm_id)
    ts = str(int(time.time()))[-6:]
    vm_id = f'{vm_id}-{ts}'

    vm_path = _vm_dir(vm_id)
    os.makedirs(vm_path, exist_ok=True)

    # Create virtual disk
    disk_file = os.path.join(vm_path, f'disk0.{disk_format}')
    try:
        r = host_run(
            f'qemu-img create -f {disk_format} "{disk_file}" {disk_size}',
            timeout=60
        )
        if r.returncode != 0:
            return jsonify({'error': f'Disk creation error: {r.stderr}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Save VM definition
    vms = _load_vms()
    vms[vm_id] = {
        'name': name,
        'cpu': cpu,
        'ram': ram,
        'disk_size': disk_size,
        'disk_format': disk_format,
        'disk_file': disk_file,
        'disks': [{
            'id': 'disk0',
            'file': disk_file,
            'format': disk_format,
            'size': disk_size,
            'bus': disk_bus,
        }],
        'os_type': os_type,
        'boot_image': boot_image,
        'description': description,
        'network': network,
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    _save_vms(vms)

    # Build a user-friendly message with port info for EthOS images
    msg = 'VM utworzona'
    pf_list = network.get('port_forwards', [])
    active_pf = [p for p in pf_list if p.get('host')]
    if active_pf:
        ports_str = ', '.join(f"{p['label']}: {p['host']}\u2192{p['guest']}" for p in active_pf)
        msg = f"VM utworzona. Porty: {ports_str}"

    return jsonify({'status': 'ok', 'id': vm_id, 'name': name, 'message': msg})


def _mark_image_installed(disk_file):
    """Mount a qcow2 disk image and prepare it for direct boot.

    Builder images boot into ethos-preboot.service (the disk installer)
    by default.  For quick-created VMs the system is already on disk,
    so the preboot must be skipped.  This function:
    1. Creates /opt/ethos/.installed — tells systemd's
       ConditionPathExists to NOT start preboot.
    2. Enables ethos.service — so the main app starts on boot.
    3. Disables ethos-preboot.service — belt-and-suspenders.
    """
    nbd_dev = '/dev/nbd0'
    mnt = '/tmp/_vm_mark_installed'
    try:
        host_run('modprobe nbd max_part=8', timeout=10)
        r = host_run(f'qemu-nbd --connect={nbd_dev} {q(disk_file)}', timeout=15)
        if r.returncode != 0:
            log.warning('Cannot attach nbd for .installed marker: %s', r.stderr)
            return
        import time as _time
        _time.sleep(1)  # let kernel discover partitions
        # Repair backup GPT header — qemu-img resize extends the disk but
        # leaves the backup GPT at the old end-of-disk position.  sgdisk -e
        # moves it to the actual end so OVMF and GRUB see a clean GPT.
        gpt_fix = host_run(f'sgdisk -e {nbd_dev} 2>/dev/null', timeout=15)
        if gpt_fix.returncode == 0:
            log.info('Relocated backup GPT to end of resized disk')
            _time.sleep(0.5)  # let nbd settle after GPT write
        else:
            log.warning('sgdisk -e failed (non-fatal): %s', gpt_fix.stderr)
        # Root is partition 2 in standard EthOS layout (1=ESP, 2=root)
        root_part = f'{nbd_dev}p2'
        os.makedirs(mnt, exist_ok=True)
        r = host_run(f'mount {root_part} {mnt}', timeout=15)
        if r.returncode != 0:
            log.warning('Cannot mount root partition for .installed marker: %s', r.stderr)
            return
        # 1. Create .installed marker
        marker = os.path.join(mnt, 'opt/ethos/.installed')
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, 'w') as f:
            f.write('installed\n')
        # 2. Enable ethos.service, disable preboot via systemd symlinks
        systemd_dir = os.path.join(mnt, 'etc/systemd/system')
        wants_dir = os.path.join(systemd_dir, 'multi-user.target.wants')
        os.makedirs(wants_dir, exist_ok=True)
        ethos_link = os.path.join(wants_dir, 'ethos.service')
        preboot_link = os.path.join(wants_dir, 'ethos-preboot.service')
        if not os.path.exists(ethos_link):
            try:
                os.symlink('/etc/systemd/system/ethos.service', ethos_link)
            except OSError:
                pass
        if os.path.islink(preboot_link):
            try:
                os.remove(preboot_link)
            except OSError:
                pass
        # 3. Create installer_result.json so the setup wizard skips the
        #    installer step (OS is already on disk — no need to repartition)
        import time as _time2
        result_file = os.path.join(mnt, 'opt/ethos/data/installer_result.json')
        os.makedirs(os.path.dirname(result_file), exist_ok=True)
        with open(result_file, 'w') as f:
            json.dump({
                'version': 2,
                'timestamp': _time2.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'strategy': 'usb',
                'system_device': '',
                'data_devices': [],
                'encrypt': False,
            }, f, indent=2)
        log.info('Marked image as installed and enabled ethos.service')
    except Exception as e:
        log.warning('Failed to mark image as installed: %s', e)
    finally:
        host_run(f'umount {mnt} 2>/dev/null', timeout=10)
        host_run(f'qemu-nbd --disconnect {nbd_dev} 2>/dev/null', timeout=10)
        try:
            os.rmdir(mnt)
        except OSError:
            pass


@vm_bp.route('/quick-create-ethos', methods=['POST'])
@admin_required
@_require_qemu
def quick_create_ethos():
    """Quick-create an EthOS VM with optimal defaults.

    Automatically finds the best EthOS image from VM images and builder
    images, generates a unique name, and creates the VM with EthOS-optimized
    settings (2 CPU, 2 GB RAM, 20 GB virtio/qcow2 disk, auto-mapped ports).
    Optionally auto-starts the VM.
    """
    err = require_tools('qemu-img')
    if err:
        return err

    data = request.get_json(force=True) if request.data else {}

    # ── Find best EthOS image (prefer VM images dir, then builder images) ──
    def _find_ethos_images():
        candidates = []
        valid_exts = {'.iso', '.img', '.raw', '.qcow2'}
        # VM images directory
        iso_dir = _iso_root()
        if os.path.isdir(iso_dir):
            for entry in os.listdir(iso_dir):
                if 'ethos' in entry.lower() and os.path.splitext(entry)[1].lower() in valid_exts:
                    fpath = os.path.join(iso_dir, entry)
                    candidates.append(('images', fpath, os.path.getmtime(fpath)))
        # Builder images directory
        builder_dir = _app_path('installer/images')
        if builder_dir and os.path.isdir(builder_dir):
            for entry in os.listdir(builder_dir):
                if 'ethos' in entry.lower() and os.path.splitext(entry)[1].lower() in valid_exts:
                    fpath = os.path.join(builder_dir, entry)
                    candidates.append(('builder', fpath, os.path.getmtime(fpath)))
        # Sort by modification time (newest first)
        candidates.sort(key=lambda c: c[2], reverse=True)
        return candidates

    candidates = _find_ethos_images()
    if not candidates:
        return jsonify({
            'error': 'Nie znaleziono obrazu EthOS. Zbuduj obraz w aplikacji Builder lub prześlij go w zakładce Obrazy.'
        }), 404

    source, boot_image, _ = candidates[0]

    # ── Generate unique name ──
    vms = _load_vms()
    existing_names = {vm.get('name', '').lower() for vm in vms.values()}
    name = 'EthOS VM'
    if name.lower() in existing_names:
        for i in range(2, 100):
            candidate_name = f'EthOS VM {i}'
            if candidate_name.lower() not in existing_names:
                name = candidate_name
                break

    # ── Create VM with optimal EthOS defaults ──
    cpu = 2
    ram = 2048
    disk_size = '20G'
    disk_format = 'qcow2'
    disk_bus = 'virtio'
    os_type = 'linux'

    network = _default_network(os_type, boot_image)

    vm_id = _sanitize_name(name).lower().replace(' ', '-')
    vm_id = re.sub(r'-+', '-', vm_id)
    ts = str(int(time.time()))[-6:]
    vm_id = f'{vm_id}-{ts}'

    vm_path = _vm_dir(vm_id)
    os.makedirs(vm_path, exist_ok=True)

    disk_file = os.path.join(vm_path, f'disk0.{disk_format}')

    # For EthOS builder images (.img/.raw): convert the image to qcow2 and
    # mark it as installed so it boots directly into the EthOS setup wizard
    # (skipping the preboot disk installer which is meant for real hardware).
    img_ext = os.path.splitext(boot_image)[1].lower()
    is_raw_image = img_ext in ('.img', '.raw')

    try:
        if is_raw_image:
            src_fmt = 'raw'
            r = host_run(f'qemu-img convert -f {src_fmt} -O {disk_format} {q(boot_image)} {q(disk_file)}', timeout=300)
        else:
            src_fmt = img_ext.lstrip('.')
            r = host_run(f'qemu-img convert -f {src_fmt} -O {disk_format} {q(boot_image)} {q(disk_file)}', timeout=300)
        if r.returncode != 0:
            return jsonify({'error': f'Disk conversion error: {r.stderr}'}), 500
        # Resize to target size if the converted image is smaller
        r2 = host_run(f'qemu-img resize {q(disk_file)} {disk_size}', timeout=60)
        if r2.returncode != 0:
            log.warning('Could not resize disk to %s: %s', disk_size, r2.stderr)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Mark image as installed so the VM boots into ethos.service (setup wizard)
    # instead of ethos-preboot.service (disk installer for real hardware).
    _mark_image_installed(disk_file)

    vms[vm_id] = {
        'name': name,
        'cpu': cpu,
        'ram': ram,
        'disk_size': disk_size,
        'disk_format': disk_format,
        'disk_file': disk_file,
        'disks': [{
            'id': 'disk0',
            'file': disk_file,
            'format': disk_format,
            'size': disk_size,
            'bus': disk_bus,
        }],
        'os_type': os_type,
        'boot_image': '',
        'description': f'Quick-created EthOS VM (image: {os.path.basename(boot_image)})',
        'network': network,
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    _save_vms(vms)

    # Port info for the response
    pf_list = network.get('port_forwards', [])
    active_pf = [p for p in pf_list if p.get('host')]
    ports_str = ', '.join(f"{p['label']}: {p['host']}\u2192{p['guest']}" for p in active_pf) if active_pf else ''

    msg = f'EthOS VM utworzona ({os.path.basename(boot_image)})'
    if ports_str:
        msg += f'. Porty: {ports_str}'

    return jsonify({
        'status': 'ok',
        'id': vm_id,
        'name': name,
        'image': os.path.basename(boot_image),
        'image_source': source,
        'ports': [{'label': p['label'], 'host': p['host'], 'guest': p['guest']} for p in active_pf],
        'message': msg,
    })


@vm_bp.route('/machines/<vm_id>', methods=['PUT'])
@admin_required
@_require_qemu
def update_vm(vm_id):
    """Update VM configuration (only when VM is stopped)."""
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before editing configuration'}), 409

    vms = _load_vms()
    if vm_id not in vms:
        return jsonify({'error': 'VM not found'}), 404

    data = request.get_json(force=True) if request.data else {}
    vm = vms[vm_id]

    if 'name' in data:
        vm['name'] = data['name'].strip()
    if 'cpu' in data:
        vm['cpu'] = max(1, min(32, int(data['cpu'])))
    if 'ram' in data:
        vm['ram'] = max(256, min(65536, int(data['ram'])))
    if 'os_type' in data:
        vm['os_type'] = data['os_type']
    if 'boot_image' in data:
        new_boot = data.get('boot_image', '')
        if new_boot:
            real_boot = os.path.realpath(new_boot)
            if not _is_allowed_image_path(real_boot):
                return jsonify({'error': 'Image path not allowed'}), 403
            new_boot = real_boot
        vm['boot_image'] = new_boot
    if 'description' in data:
        vm['description'] = data['description']
    if 'autostart' in data:
        vm['autostart'] = bool(data['autostart'])
    if 'network' in data:
        net = data['network']
        net_type = net.get('net_type', 'user') if isinstance(net, dict) else 'user'
        if net_type not in ('user', 'none', 'bridge'):
            net_type = 'user'
        pf = _validate_port_forwards(net.get('port_forwards', []) if isinstance(net, dict) else [])
        net_cfg = {'net_type': net_type, 'port_forwards': pf}
        if net_type == 'bridge':
            bridge = net.get('bridge', _BRIDGE_NAME) if isinstance(net, dict) else _BRIDGE_NAME
            if not _validate_bridge_name(bridge):
                return jsonify({'error': 'Invalid bridge name'}), 400
            net_cfg['bridge'] = bridge
        vm['network'] = net_cfg

    _save_vms(vms)
    return jsonify({'ok': True})


@vm_bp.route('/machines/<vm_id>/autostart', methods=['PUT'])
@admin_required
@_require_qemu
def set_vm_autostart(vm_id):
    """Toggle autostart for a VM (allowed even while running)."""
    vms = _load_vms()
    if vm_id not in vms:
        return jsonify({'error': 'VM not found'}), 404
    data = request.get_json(force=True) if request.data else {}
    vms[vm_id]['autostart'] = bool(data.get('autostart', False))
    _save_vms(vms)
    return jsonify({'ok': True, 'autostart': vms[vm_id]['autostart']})


def vm_autostart_boot(flask_app=None):
    """Start all VMs with autostart=True. Called on EthOS startup."""
    import logging
    from flask import g
    log = logging.getLogger('vm_autostart')
    try:
        vms = _load_vms()
    except Exception:
        return
    candidates = [(vid, v) for vid, v in vms.items() if v.get('autostart')]
    if not candidates:
        return
    if not _qemu_available():
        log.warning('[vm] Autostart: QEMU not installed, skipping')
        return
    log.info('[vm] Autostart: %d VM(s) queued', len(candidates))
    if not flask_app:
        try:
            from flask import current_app
            flask_app = current_app._get_current_object()
        except RuntimeError:
            log.warning('[vm] Autostart: no Flask app context available')
            return
    for vm_id, vm in candidates:
        if _check_vm_process(vm_id):
            log.info('[vm] Autostart: %s already running, skip', vm.get('name', vm_id))
            continue
        try:
            with flask_app.test_request_context():
                g.username = 'system'
                g.role = 'admin'
                g.groups = ['sudo']
                resp = start_vm(vm_id)
                status = resp[1] if isinstance(resp, tuple) else 200
                if status >= 400:
                    log.warning('[vm] Autostart: %s failed (HTTP %d)', vm.get('name', vm_id), status)
                else:
                    log.info('[vm] Autostart: %s started OK', vm.get('name', vm_id))
        except Exception as e:
            log.warning('[vm] Autostart: %s failed: %s', vm.get('name', vm_id), e)


@vm_bp.route('/machines/<vm_id>/network', methods=['PUT'])
@admin_required
@_require_qemu
def update_vm_network(vm_id):
    """Update VM network configuration (only when stopped)."""
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before changing network configuration'}), 409

    vms = _load_vms()
    if vm_id not in vms:
        return jsonify({'error': 'VM not found'}), 404

    data = request.get_json(force=True) if request.data else {}
    net_type = data.get('net_type', 'user')
    if net_type not in ('user', 'none', 'bridge'):
        return jsonify({'error': 'net_type must be "user", "bridge", or "none"'}), 400
    pf = _validate_port_forwards(data.get('port_forwards', []))
    net_cfg = {'net_type': net_type, 'port_forwards': pf}
    if net_type == 'bridge':
        bridge = data.get('bridge', _BRIDGE_NAME)
        if not _validate_bridge_name(bridge):
            return jsonify({'error': 'Invalid bridge name'}), 400
        net_cfg['bridge'] = bridge
    vms[vm_id]['network'] = net_cfg
    _save_vms(vms)
    return jsonify({'ok': True})


@vm_bp.route('/bridge', methods=['GET'])
@admin_required
@_require_qemu
def bridge_info():
    """Return bridge networking status."""
    return jsonify(_bridge_status())


@vm_bp.route('/bridge/setup', methods=['POST'])
@admin_required
@_require_qemu
def bridge_setup():
    """Set up bridge networking (creates br0 from primary ethernet)."""
    ok, msg = _setup_bridge()
    if ok:
        return jsonify({'ok': True, 'message': msg, **_bridge_status()})
    return jsonify({'error': msg}), 500


def _teardown_bridge():
    """Remove br0 bridge and restore direct ethernet connection via nmcli."""
    br = _BRIDGE_NAME
    try:
        def nmcli(cmd):
            return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)

        # Find the slave interface before destroying the bridge
        r = subprocess.run(
            f"ip link show master {br} | grep -oP '^\\d+: \\K[^@:]+'",
            shell=True, capture_output=True, text=True, timeout=5
        )
        slave = r.stdout.strip() or None

        # Delete br0 and br0-port nmcli connections
        show = nmcli('nmcli -t -f NAME,UUID connection show')
        for line in show.stdout.splitlines():
            parts = line.split(':')
            if len(parts) >= 2 and parts[0] in (br, f'{br}-port'):
                nmcli(f'nmcli connection delete {parts[1]}')

        # Restore a plain DHCP connection on the slave interface
        if slave:
            nmcli(f'nmcli connection add type ethernet con-name {slave} ifname {slave} autoconnect yes ipv4.method auto')
            nmcli(f'nmcli connection up {slave}')

        # Wait for IP on restored interface
        for _ in range(15):
            time.sleep(1)
            r = subprocess.run(
                f"ip -4 -o addr show {slave} scope global | awk '{{print $4}}' | cut -d/ -f1 | head -1",
                shell=True, capture_output=True, text=True, timeout=5
            )
            ip = r.stdout.strip()
            if ip:
                return True, f'Bridge removed, {slave} restored with IP {ip}'
        return True, f'Bridge removed, {slave} restored (waiting for DHCP)'
    except Exception as e:
        return False, str(e)


@vm_bp.route('/bridge/teardown', methods=['POST'])
@admin_required
@_require_qemu
def bridge_teardown():
    """Remove br0 bridge and restore direct ethernet connection."""
    ok, msg = _teardown_bridge()
    if ok:
        return jsonify({'ok': True, 'message': msg, **_bridge_status()})
    return jsonify({'error': msg}), 500


@vm_bp.route('/machines/<vm_id>', methods=['DELETE'])
@admin_required
@_require_qemu
def delete_vm(vm_id):
    """Delete a virtual machine and its disk files."""
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before deletion'}), 409

    vms = _load_vms()
    if vm_id not in vms:
        return jsonify({'error': 'VM not found'}), 404

    # Remove VM directory
    vm_path = _vm_dir(vm_id)
    if os.path.isdir(vm_path):
        import shutil
        shutil.rmtree(vm_path, ignore_errors=True)

    del vms[vm_id]
    _save_vms(vms)
    return jsonify({'status': 'ok'})


# ─── noVNC static proxy (same-origin for iframe) ─────────

@vm_bp.route('/novnc/<path:filename>')
def novnc_static(filename):
    """Serve noVNC files through Flask so the console iframe stays same-origin.
    No auth required — these are static open-source UI files, not data."""
    novnc_dir = os.path.abspath(_NOVNC_DIR)
    if not os.path.isdir(novnc_dir):
        abort(404)
    return send_from_directory(novnc_dir, filename)


# ─── WebSocket proxy (same-origin, no extra ports) ────────
#
# Browser connects WebSocket to Flask (/api/vm/ws/vnc/<id> or
# /api/vm/ws/serial/<id>). Flask relays raw bytes to QEMU's
# VNC/serial TCP port. No need for the browser to reach external
# ports — works with HTTPS, reverse proxies, and firewalls.

def _init_ws_proxy(app):
    """Wrap the Flask app with a WSGI middleware that proxies WebSocket
    connections for VNC and serial consoles directly to QEMU TCP ports.

    Uses gevent-websocket (already present for Flask-SocketIO).  The
    middleware intercepts ``/api/vm/ws/vnc/<id>`` and
    ``/api/vm/ws/serial/<id>`` *before* Flask routing, so there is no
    conflict with Werkzeug's WebSocket-aware URL map.
    """
    import re
    from http.cookies import SimpleCookie
    from urllib.parse import parse_qs

    _WS_RE = re.compile(r'^/api/vm/ws/(vnc|serial)/([A-Za-z0-9._-]+)$')

    def _parse_token(environ):
        """Extract auth token from cookie / query string / Authorization."""
        # Cookie
        cookie_str = environ.get('HTTP_COOKIE', '')
        if cookie_str:
            sc = SimpleCookie()
            sc.load(cookie_str)
            morsel = sc.get('nas_token')
            if morsel and morsel.value:
                return morsel.value
        # Query param
        qs = parse_qs(environ.get('QUERY_STRING', ''))
        qt = qs.get('token', [''])[0]
        if qt:
            return qt
        # Authorization header
        auth_h = environ.get('HTTP_AUTHORIZATION', '')
        if auth_h.startswith('Bearer '):
            return auth_h[7:]
        return ''

    def _validate_token(token):
        import sys
        from datetime import datetime
        # App runs as __main__ when started directly (python app.py)
        app_mod = sys.modules.get('__main__')
        if not app_mod:
            return False
        tok_store = getattr(app_mod, 'tokens', None)
        if not tok_store:
            return False
        info = tok_store.get(token)
        if not info:
            return False
        expires = info.get('expires', datetime.min)
        return expires > datetime.now()

    def _relay(ws, target_port):
        """Relay gevent-websocket frames ↔ raw TCP bytes."""
        import gevent
        from gevent import socket as gsock

        try:
            upstream = gsock.create_connection(('127.0.0.1', target_port), timeout=5)
        except Exception as exc:
            log.warning('WS proxy: cannot connect to port %s: %s', target_port, exc)
            ws.close()
            return

        closed = [False]

        def _ws_to_tcp():
            try:
                while not closed[0]:
                    msg = ws.receive()
                    if msg is None:
                        break
                    if isinstance(msg, str):
                        upstream.sendall(msg.encode('utf-8'))
                    else:
                        upstream.sendall(msg)
            except Exception:
                pass
            finally:
                closed[0] = True
                try:
                    upstream.close()
                except Exception:
                    pass

        def _tcp_to_ws():
            try:
                while not closed[0]:
                    data = upstream.recv(65536)
                    if not data:
                        break
                    ws.send(data)
            except Exception:
                pass
            finally:
                closed[0] = True
                try:
                    ws.close()
                except Exception:
                    pass

        g1 = gevent.spawn(_ws_to_tcp)
        g2 = gevent.spawn(_tcp_to_ws)
        gevent.joinall([g1, g2])

    inner = app.wsgi_app

    def _ws_middleware(environ, start_response):
        path = environ.get('PATH_INFO', '')
        ws = environ.get('wsgi.websocket')
        if ws:
            m = _WS_RE.match(path)
            if m:
                kind, vm_id = m.group(1), m.group(2)
                token = _parse_token(environ)
                if not token or not _validate_token(token):
                    log.info('WS proxy: auth failed for %s/%s', kind, vm_id)
                    ws.close()
                    start_response('401 Unauthorized', [])
                    return [b'']
                info = _running_vms.get(vm_id)
                port_key = 'vnc_port' if kind == 'vnc' else 'serial_port'
                port = info.get(port_key) if info else None
                if not port:
                    log.info('WS proxy: VM %s not found or no %s', vm_id, port_key)
                    ws.close()
                    start_response('404 Not Found', [])
                    return [b'']
                log.info('WS %s proxy: vm=%s → port %s', kind, vm_id, port)
                _relay(ws, port)
                return [b'']
        return inner(environ, start_response)

    app.wsgi_app = _ws_middleware


# ═══════════════════════════════════════════════════════════
#  VM POWER CONTROL
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/machines/<vm_id>/start', methods=['POST'])
@admin_required
@_require_qemu
def start_vm(vm_id):
    """Start a virtual machine."""
    if _check_vm_process(vm_id):
        return jsonify({'error': 'VM already running'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    vnc_display, vnc_port = _next_vnc_port()
    serial_port = _next_serial_port()
    kvm = _kvm_available()

    boot_image = vm.get('boot_image', '')
    boot_image_real = os.path.realpath(boot_image) if boot_image else ''
    if boot_image and not _is_allowed_image_path(boot_image_real):
        return jsonify({'error': 'Image path not allowed'}), 403
    boot_image = boot_image_real

    is_arm = _is_arm_image(boot_image, vm.get('name', ''))
    is_rpi = _is_rpi_image(boot_image, vm.get('name', ''))
    proxy_map = {}  # populated by _build_net_opts for user-mode networking

    # Validate that the required QEMU system binary is available
    if is_arm or is_rpi:
        err = require_tools('qemu-system-aarch64')
    else:
        err = require_tools('qemu-system-x86_64')
    if err:
        return err

    # ── Raspberry Pi VM (raspi3b machine) ─────────────────────
    tap_dev = None  # Track TAP device for cleanup on stop
    if is_rpi:
        if not _arm_qemu_available():
            return jsonify({'error': 'qemu-system-aarch64 is not installed. Install: apt install qemu-system-arm'}), 503
        if not _raspi_machine_available():
            return jsonify({'error': 'QEMU does not support raspi3b machine. Update QEMU to version >= 8.0'}), 503

        # The boot image (RPi OS .img) is the main SD card — must be writable.
        # We work on a copy so the original stays intact.
        if boot_image and os.path.exists(boot_image):
            vm_path = _vm_dir(vm_id)
            sd_copy = os.path.join(vm_path, 'sd-card.img')
            if not os.path.exists(sd_copy):
                _logger.info('Copying RPi image as SD card: %s → %s', boot_image, sd_copy)
                shutil.copy2(boot_image, sd_copy)
        else:
            return jsonify({'error': 'No RPi boot image (.img)'}), 400

        # ── Extract kernel + DTB from boot partition ──
        # QEMU raspi3b does NOT emulate GPU firmware (bootcode.bin/start.elf),
        # so we must extract and pass kernel + DTB explicitly.
        kernel_path = os.path.join(vm_path, 'kernel8.img')
        dtb_path = os.path.join(vm_path, 'bcm2710-rpi-3-b-plus.dtb')

        if not os.path.exists(kernel_path) or not os.path.exists(dtb_path):
            _logger.info('Extracting kernel + DTB from RPi image boot partition...')
            extract_script = (
                f'LOOP=$(losetup --find --show --partscan "{sd_copy}") && '
                f'BOOT_PART="${{LOOP}}p1" && '
                f'MNT=$(mktemp -d) && '
                f'mount -o ro "$BOOT_PART" "$MNT" && '
                f'cp "$MNT/kernel8.img" "{kernel_path}" 2>/dev/null || '
                f'  cp "$MNT/kernel_2712.img" "{kernel_path}" 2>/dev/null || '
                f'  cp "$MNT/kernel8.img" "{kernel_path}" && '
                f'DTB=$(ls "$MNT"/bcm2710-rpi-3-b*.dtb 2>/dev/null | head -1) && '
                f'[ -n "$DTB" ] && cp "$DTB" "{dtb_path}" && '
                f'umount "$MNT" && losetup -d "$LOOP" && rm -rf "$MNT" && echo OK'
            )
            r = host_run(extract_script, timeout=30)
            if 'OK' not in r.stdout:
                return jsonify({'error': f'Failed to extract kernel/DTB from RPi image: {r.stderr[-200:]}'}), 500

        if not os.path.exists(kernel_path):
            return jsonify({'error': 'No kernel8.img in RPi image'}), 400
        if not os.path.exists(dtb_path):
            return jsonify({'error': 'No DTB file (bcm2710-rpi-3-b*.dtb) in RPi image'}), 400

        cmd = ['qemu-system-aarch64']
        cmd += ['-machine', 'raspi3b']
        # raspi3b: fixed 1 GB RAM — QEMU ignores -m for this machine

        # Kernel + DTB (required — raspi3b has no GPU firmware emulation)
        cmd += ['-kernel', kernel_path]
        cmd += ['-dtb', dtb_path]
        cmd += ['-append', 'console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait']

        # SD card — main boot drive (RPi boots from SD)
        cmd += ['-drive', f'file={sd_copy},format=raw,if=sd']

        # Additional data disks — attach via USB mass-storage
        # NOTE: raspi3b USB emulation is limited; this may not work
        disks = vm.get('disks', [])
        if not disks and vm.get('disk_file'):
            disks = [{'id': 'disk0', 'file': vm['disk_file'],
                       'format': vm.get('disk_format', 'qcow2')}]
        for disk in disks:
            df = disk.get('file', '')
            if df and os.path.exists(df):
                dfmt = disk.get('format', 'qcow2')
                did = disk.get('id', 'usbdisk')
                cmd += ['-drive', f'file={df},format={dfmt},if=none,id={did}']
                cmd += ['-device', f'usb-storage,drive={did}']

        # Serial console (more reliable than VNC for raspi3b)
        cmd += ['-serial', f'mon:tcp:127.0.0.1:{vnc_port},server=on,wait=off']

        # VNC display (raspi3b framebuffer — may show nothing until kernel draws to fb)
        cmd += ['-vnc', f':{vnc_display}']

        # Network — no USB-net for raspi3b (DWC2 emulation is limited)
        # User can SSH via port-forwarded serial or VNC
        cmd += ['-monitor', 'none']

    # ── Generic ARM (aarch64) VM ──────────────────────────────
    elif is_arm:
        if not _arm_qemu_available():
            return jsonify({'error': 'qemu-system-aarch64 is not installed. Install: apt install qemu-system-arm qemu-efi-aarch64'}), 503

        cmd = ['qemu-system-aarch64']
        cmd += ['-machine', 'virt']
        cmd += ['-cpu', 'cortex-a72']
        cmd += ['-smp', str(vm.get('cpu', 2))]
        cmd += ['-m', str(vm.get('ram', 1024))]

        # UEFI firmware for aarch64
        aavmf_paths = [
            '/usr/share/AAVMF/AAVMF_CODE.fd',
            '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd',
        ]
        for fw in aavmf_paths:
            if os.path.exists(fw):
                cmd += ['-bios', fw]
                break

        # Disks
        disks = vm.get('disks', [])
        if not disks and vm.get('disk_file'):
            disks = [{'id': 'disk0', 'file': vm['disk_file'],
                       'format': vm.get('disk_format', 'qcow2')}]
        for disk in disks:
            df = disk.get('file', '')
            if df and os.path.exists(df):
                dfmt = disk.get('format', 'qcow2')
                cmd += ['-drive', f'file={df},format={dfmt},if=virtio']

        # Boot image — mount as second drive for generic ARM
        if boot_image and os.path.exists(boot_image):
            ext = os.path.splitext(boot_image)[1].lower()
            fmt = 'raw' if ext in ('.img', '.raw') else 'qcow2'
            cmd += ['-drive', f'file={boot_image},format={fmt},if=virtio']

        # Network — configurable per-VM
        try:
            net_args, tap_dev, proxy_map = _build_net_opts(vm)
        except RuntimeError as e:
            return jsonify({'error': str(e)}), 409
        if net_args:
            cmd += net_args

        # VNC and display
        cmd += ['-vnc', f':{vnc_display}']
        cmd += ['-device', 'virtio-gpu-pci']
        cmd += ['-device', 'usb-ehci', '-device', 'usb-tablet']
        cmd += ['-serial', f'tcp:127.0.0.1:{serial_port},server=on,wait=off']
        cmd += ['-monitor', 'none']

    # ── x86_64 VM ─────────────────────────────────────────────
    else:

        # Build QEMU command
        cmd = ['qemu-system-x86_64']

        # KVM acceleration
        if kvm:
            cmd += ['-enable-kvm']

        # Machine type
        cmd += ['-machine', 'q35']

        # CPU
        cpu_model = 'host' if kvm else 'qemu64'
        cmd += ['-cpu', cpu_model, '-smp', str(vm.get('cpu', 2))]

        # RAM
        cmd += ['-m', str(vm.get('ram', 1024))]

        # Boot image (ISO/IMG) — must be resolved before disk so we can
        # set boot priority when a disk image is used as installer media.
        has_disk_boot_image = False
        if boot_image and os.path.exists(boot_image):
            ext = os.path.splitext(boot_image)[1].lower()
            if ext in ('.iso',):
                pass  # handled below after disk
            else:
                has_disk_boot_image = True
                fmt_map = {
                    '.img': 'raw', '.raw': 'raw',
                    '.qcow2': 'qcow2', '.vdi': 'vdi', '.vmdk': 'vmdk',
                }
                img_fmt = fmt_map.get(ext, 'raw')
                # Boot image as primary drive (bootindex=0) — acts like a USB installer
                # snapshot=on: temp CoW overlay so guest can write without modifying the original
                cmd += ['-drive', f'file={boot_image},format={img_fmt},if=none,id=bootimg,snapshot=on']
                cmd += ['-device', f'virtio-blk-pci,drive=bootimg,bootindex=0']

        # Disks — loop over all VM disks
        disks = vm.get('disks', [])
        if not disks and vm.get('disk_file'):
            disks = [{'id': 'disk0', 'file': vm['disk_file'],
                       'format': vm.get('disk_format', 'qcow2')}]

        # Track which controller types are needed for non-virtio buses
        need_scsi_ctrl = any(d.get('bus') == 'scsi' for d in disks)
        need_sata_ctrl = any(d.get('bus') == 'sata' for d in disks)
        if need_scsi_ctrl:
            cmd += ['-device', 'virtio-scsi-pci,id=scsi0']
        if need_sata_ctrl:
            cmd += ['-device', 'ich9-ahci,id=sata0']

        scsi_idx = 0
        sata_idx = 0
        boot_offset = 1 if has_disk_boot_image else 0
        for i, disk in enumerate(disks):
            df = disk.get('file', '')
            if not df or not os.path.exists(df):
                continue
            dfmt = disk.get('format', 'qcow2')
            did = disk.get('id', f'disk{i}')
            bus = disk.get('bus', 'virtio')
            cmd += ['-drive', f'file={df},format={dfmt},if=none,id={did}']
            boot_str = f',bootindex={i + boot_offset}'
            if bus == 'scsi':
                cmd += ['-device', f'scsi-hd,bus=scsi0.0,drive={did},lun={scsi_idx}{boot_str}']
                scsi_idx += 1
            elif bus == 'sata':
                cmd += ['-device', f'ide-hd,bus=sata0.{sata_idx},drive={did}{boot_str}']
                sata_idx += 1
            elif bus == 'ide':
                cmd += ['-device', f'ide-hd,drive={did}{boot_str}']
            else:
                cmd += ['-device', f'virtio-blk-pci,drive={did}{boot_str}']

        # ISO boot image (CD-ROM) — no forced boot order; UEFI uses
        # bootindex (disks=0,1,… before CD) and NVRAM for boot priority.
        # On first boot empty disks are skipped so the ISO boots naturally.
        # After OS installation disk has EFI → boots before CD-ROM.
        if boot_image and os.path.exists(boot_image):
            ext = os.path.splitext(boot_image)[1].lower()
            if ext in ('.iso',):
                cmd += ['-cdrom', boot_image]

        # Network — configurable per-VM
        try:
            net_args, tap_dev, proxy_map = _build_net_opts(vm)
        except RuntimeError as e:
            return jsonify({'error': str(e)}), 409
        if net_args:
            cmd += net_args

        # VNC display (for remote access through browser)
        cmd += ['-vnc', f':{vnc_display}']

        # UEFI firmware — pflash mode with per-VM writable NVRAM
        # This preserves UEFI boot entries across reboots (required for
        # grub-install / efibootmgr to persist after installer finishes).
        _ovmf_pairs = [
            ('/usr/share/OVMF/OVMF_CODE_4M.fd', '/usr/share/OVMF/OVMF_VARS_4M.fd'),
            ('/usr/share/OVMF/OVMF_CODE.fd',    '/usr/share/OVMF/OVMF_VARS.fd'),
            ('/usr/share/ovmf/OVMF.fd',          None),
            ('/usr/share/qemu/OVMF.fd',          None),
        ]
        for ovmf_code, ovmf_vars_template in _ovmf_pairs:
            if not os.path.exists(ovmf_code):
                continue
            if ovmf_vars_template and os.path.exists(ovmf_vars_template):
                vm_vars = os.path.join(_vm_dir(vm_id), 'OVMF_VARS.fd')
                if not os.path.exists(vm_vars):
                    shutil.copy2(ovmf_vars_template, vm_vars)
                cmd += ['-drive', f'if=pflash,format=raw,unit=0,file={ovmf_code},readonly=on']
                cmd += ['-drive', f'if=pflash,format=raw,unit=1,file={vm_vars}']
            else:
                cmd += ['-bios', ovmf_code]
            break

        # USB tablet for better mouse tracking in VNC
        cmd += ['-device', 'usb-ehci', '-device', 'usb-tablet']

        # VGA adapter — virtio-gpu for best performance in VNC/noVNC
        cmd += ['-vga', 'virtio']

        # Serial console on TCP for browser-based terminal (xterm.js via websockify)
        cmd += ['-serial', f'tcp:127.0.0.1:{serial_port},server=on,wait=off']

        # Daemonize — no, we manage the process ourselves
        cmd += ['-monitor', 'none']

    try:
        # Use real subprocess.Popen (not gevent patched one for better control)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # Detach from our process group
        )

        # Wait briefly to check if QEMU started OK
        time.sleep(1)
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode('utf-8', errors='replace')
            _destroy_tap(tap_dev)
            return jsonify({'error': f'QEMU failed to start: {stderr[:500]}'}), 500

        _running_vms[vm_id] = {
            'proc': proc,
            'pid': proc.pid,
            'started': time.time(),
            'vnc_port': vnc_port,
            'vnc_display': vnc_display,
            'serial_port': serial_port,
            'ws_proc': None,
            'ws_port': None,
            'serial_ws_proc': None,
            'serial_ws_port': None,
            'tap_dev': tap_dev,
            'socat_procs': [],
        }

        # Start socat TCP proxies for user-mode port forwards
        if proxy_map:
            _running_vms[vm_id]['socat_procs'] = _start_socat_proxies(proxy_map)

        # Start websockify for browser-based console (noVNC)
        ws_port = _next_ws_port()
        ws_proc = _start_websockify(vnc_port, ws_port)
        if ws_proc:
            _running_vms[vm_id]['ws_proc'] = ws_proc
            _running_vms[vm_id]['ws_port'] = ws_port

        # Start websockify for serial console (xterm.js)
        serial_ws_port = _next_serial_ws_port()
        serial_ws_proc = _start_serial_websockify(serial_port, serial_ws_port)
        if serial_ws_proc:
            _running_vms[vm_id]['serial_ws_proc'] = serial_ws_proc
            _running_vms[vm_id]['serial_ws_port'] = serial_ws_port

        return jsonify({
            'ok': True,
            'pid': proc.pid,
            'vnc_port': vnc_port,
            'vnc_display': vnc_display,
            'ws_port': _running_vms[vm_id].get('ws_port'),
            'serial_ws_port': _running_vms[vm_id].get('serial_ws_port'),
            'kvm': kvm,
            'message': f'VM started (VNC: :{vnc_display})',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vm_bp.route('/machines/<vm_id>/stop', methods=['POST'])
@admin_required
@_require_qemu
def stop_vm(vm_id):
    """Stop (gracefully or forcefully) a virtual machine."""
    if not _check_vm_process(vm_id):
        return jsonify({'error': 'VM not running'}), 409

    data = request.get_json(force=True) if request.data else {}
    force = data.get('force', False)

    info = _running_vms.get(vm_id)
    if not info:
        return jsonify({'error': 'VM not found'}), 404

    proc = info['proc']
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
            # Wait up to 10s for graceful shutdown
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass

    _stop_websockify(info)
    _stop_serial_websockify(info)
    _stop_socat_proxies(info)
    _destroy_tap(info.get('tap_dev'))
    _running_vms.pop(vm_id, None)

    # Auto-eject boot media after stop — installation is assumed complete.
    # This ensures next boot goes to disk instead of re-booting the installer.
    # Also reset NVRAM so stale CD-ROM boot entries are cleared.
    vms = _load_vms()
    vm_def = vms.get(vm_id, {})
    boot_img = vm_def.get('boot_image', '')
    eject_exts = ('.iso', '.img', '.raw', '.qcow2', '.vdi', '.vmdk')

    if boot_img and os.path.splitext(boot_img)[1].lower() in eject_exts:
        vm_def['boot_image'] = ''
        vm_vars = os.path.join(_vm_dir(vm_id), 'OVMF_VARS.fd')
        try:
            if os.path.exists(vm_vars):
                os.remove(vm_vars)
        except OSError as e:
            log.warning('Failed to reset NVRAM for VM %s: %s', vm_id, e)
        _save_vms(vms)
        log.info('Auto-ejected boot media from VM %s and reset NVRAM', vm_id)

    return jsonify({'status': 'ok'})


@vm_bp.route('/machines/<vm_id>/restart', methods=['POST'])
@admin_required
@_require_qemu
def restart_vm(vm_id):
    """Restart a VM by stopping and starting it."""
    # Stop
    if _check_vm_process(vm_id):
        info = _running_vms.get(vm_id)
        if info:
            proc = info['proc']
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            _stop_websockify(info)
            _stop_serial_websockify(info)
            _stop_socat_proxies(info)
            _destroy_tap(info.get('tap_dev'))
        _running_vms.pop(vm_id, None)
        time.sleep(1)

    # Start — delegate to start_vm logic
    return start_vm(vm_id)


# ═══════════════════════════════════════════════════════════
#  ISO / IMAGE MANAGEMENT
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/images')
@admin_required
@_require_qemu
def list_images():
    """List available ISO/IMG/QCOW2 images for booting VMs."""
    iso_dir = _iso_root()
    images = []
    valid_exts = {'.iso', '.img', '.raw', '.qcow2', '.vdi', '.vmdk'}

    for entry in sorted(os.listdir(iso_dir)):
        fpath = os.path.join(iso_dir, entry)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(entry)[1].lower()
        if ext not in valid_exts:
            continue
        stat = os.stat(fpath)
        images.append({
            'name': entry,
            'path': fpath,
            'size': stat.st_size,
            'size_human': _human_size(stat.st_size),
            'type': ext.lstrip('.').upper(),
            'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)),
        })
    return jsonify(images)


@vm_bp.route('/builder-images')
@admin_required
def list_builder_images():
    """List images built by the EthOS Builder (installer/images/)."""
    images_dir = _app_path('installer/images')
    if not os.path.isdir(images_dir):
        return jsonify([])
    valid_exts = {'.iso', '.img', '.raw', '.qcow2'}
    result = []
    for entry in sorted(os.listdir(images_dir)):
        fpath = os.path.join(images_dir, entry)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(entry)[1].lower()
        if ext not in valid_exts:
            continue
        stat = os.stat(fpath)
        result.append({
            'name': entry,
            'path': fpath,
            'size': stat.st_size,
            'size_human': _human_size(stat.st_size),
            'type': ext.lstrip('.').upper(),
            'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)),
        })
    return jsonify(result)


@vm_bp.route('/builder-images/copy', methods=['POST'])
@admin_required
@_require_qemu
def copy_builder_image():
    """Copy a builder image into the VM images directory."""
    import shutil as _shutil
    data = request.get_json(force=True) if request.data else {}
    src = data.get('path', '')
    if not src or not os.path.isfile(src):
        return jsonify({'error': 'Source file not found'}), 404
    # Security: only allow files from the builder images directory
    images_dir = os.path.realpath(_app_path('installer/images'))
    real_src = os.path.realpath(src)
    if not real_src.startswith(images_dir + '/'):
        return jsonify({'error': 'Path not allowed'}), 403
    dest = os.path.join(_iso_root(), os.path.basename(src))
    if os.path.exists(dest):
        return jsonify({'error': f'File "{os.path.basename(src)}" already exists in VM images'}), 409
    try:
        _shutil.copy2(real_src, dest)
        return jsonify({'status': 'ok', 'name': os.path.basename(src)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vm_bp.route('/images', methods=['POST'])
@admin_required
@_require_qemu
def upload_image():
    """Upload an ISO/IMG/QCOW2 image."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No filename provided'}), 400

    valid_exts = {'.iso', '.img', '.raw', '.qcow2', '.vdi', '.vmdk'}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in valid_exts:
        return jsonify({'error': f'Unsupported format: {ext}. Allowed: {", ".join(valid_exts)}'}), 400

    safe_name = re.sub(r'[^\w\s\-.]', '', f.filename)
    dest = os.path.join(_iso_root(), safe_name)

    f.save(dest)
    return jsonify({'ok': True, 'name': safe_name, 'path': dest})


@vm_bp.route('/images/<path:filename>', methods=['DELETE'])
@admin_required
@_require_qemu
def delete_image(filename):
    """Delete an image file."""
    fpath = os.path.join(_iso_root(), os.path.basename(filename))
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404

    # Check if any VM uses this image
    vms = _load_vms()
    for vm_id, vm in vms.items():
        if vm.get('boot_image') == fpath:
            return jsonify({'error': f'Image in use by VM "{vm.get("name", vm_id)}"'}), 409

    os.remove(fpath)
    return jsonify({'ok': True})


@vm_bp.route('/import-disk', methods=['POST'])
@admin_required
@_require_qemu
def import_disk():
    """Create a VM from an uploaded or server-path disk image.

    Accepts multipart/form-data (file upload) or JSON with src_path.
    Converts vmdk/vdi/raw/img → qcow2 via qemu-img convert.
    """
    import shutil

    err = require_tools('qemu-img')
    if err:
        return err

    is_upload = 'file' in request.files

    if is_upload:
        f      = request.files['file']
        name   = (request.form.get('name') or '').strip()
        cpu    = int(request.form.get('cpu', 2))
        ram    = int(request.form.get('ram', 2048))
        os_type= request.form.get('os_type', 'linux')
        desc   = request.form.get('description', '')
        do_convert = request.form.get('convert', 'true') == 'true'
    else:
        data   = request.get_json(force=True) if request.data else {}
        name   = (data.get('name') or '').strip()
        cpu    = int(data.get('cpu', 2))
        ram    = int(data.get('ram', 2048))
        os_type= data.get('os_type', 'linux')
        desc   = data.get('description', '')
        do_convert = data.get('convert', True)
        src_path   = (data.get('src_path') or '').strip()

    if not name:
        return jsonify({'error': 'Podaj nazwę VM'}), 400

    # Build VM id/dir
    vm_id  = re.sub(r'-+', '-', _sanitize_name(name).lower().replace(' ', '-'))
    vm_id  = f'{vm_id}-{str(int(time.time()))[-6:]}'
    vm_path = _vm_dir(vm_id)
    os.makedirs(vm_path, exist_ok=True)

    try:
        if is_upload:
            fname = f.filename or 'disk'
            ext   = os.path.splitext(fname)[1].lower()
            valid = {'.qcow2', '.raw', '.vmdk', '.vdi', '.img', '.vhd', '.vhdx'}
            if ext not in valid:
                shutil.rmtree(vm_path, ignore_errors=True)
                return jsonify({'error': f'Nieobsługiwany format: {ext}. Dozwolone: {", ".join(sorted(valid))}'}), 400
            tmp = os.path.join(vm_path, f'import_tmp{ext}')
            f.save(tmp)
            src_path = tmp
        else:
            if not src_path:
                shutil.rmtree(vm_path, ignore_errors=True)
                return jsonify({'error': 'src_path wymagany'}), 400
            real = os.path.realpath(src_path)
            allowed_roots = _allowed_image_roots() + [os.path.realpath(_iso_root())]
            if not any(real.startswith(r + '/') or real == r for r in allowed_roots):
                shutil.rmtree(vm_path, ignore_errors=True)
                return jsonify({'error': 'Ścieżka niedozwolona'}), 403
            src_path = real

        ext = os.path.splitext(src_path)[1].lower()

        if do_convert and ext != '.qcow2':
            disk_file   = os.path.join(vm_path, 'disk.qcow2')
            disk_format = 'qcow2'
            r = host_run(f'qemu-img convert -O qcow2 "{src_path}" "{disk_file}"', timeout=7200)
            if r.returncode != 0:
                raise Exception(f'Konwersja nie powiodła się: {r.stderr[:300]}')
            if is_upload:
                os.remove(src_path)
        else:
            disk_format = {'img': 'raw', 'vhd': 'vpc', 'vhdx': 'vhdx'}.get(ext.lstrip('.'), ext.lstrip('.'))
            disk_file   = os.path.join(vm_path, f'disk{ext}')
            if is_upload:
                os.rename(src_path, disk_file)
            else:
                shutil.copy2(src_path, disk_file)

        # Read actual disk size from image metadata
        try:
            ir = host_run(f'qemu-img info --output=json "{disk_file}"', timeout=30)
            info = json.loads(ir.stdout) if ir.returncode == 0 else {}
            disk_size = _human_size(info.get('virtual-size', 0))
        except Exception:
            disk_size = 'imported'

        vms = _load_vms()
        vms[vm_id] = {
            'name':        name,
            'cpu':         max(1, min(32, cpu)),
            'ram':         max(256, min(65536, ram)),
            'disk_size':   disk_size,
            'disk_format': disk_format,
            'disk_file':   disk_file,
            'disks':       [{'id': 'disk0', 'file': disk_file,
                             'format': disk_format, 'size': disk_size,
                             'bus': 'virtio'}],
            'os_type':     os_type,
            'boot_image':  '',
            'description': desc,
            'network':     _default_network(os_type),
            'created':     time.strftime('%Y-%m-%d %H:%M:%S'),
            'imported':    True,
        }
        _save_vms(vms)
        return jsonify({'status': 'ok', 'id': vm_id, 'name': name, 'disk_size': disk_size})

    except Exception as e:
        shutil.rmtree(vm_path, ignore_errors=True)
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  DISK MANAGEMENT
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/machines/<vm_id>/disk-info')
@admin_required
@_require_qemu
def disk_info(vm_id):
    """Get info about a VM's disk file."""
    err = require_tools('qemu-img')
    if err:
        return err
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    disk_file = vm.get('disk_file', '')
    if not disk_file or not os.path.exists(disk_file):
        return jsonify({'error': 'Disk file not found'}), 404

    try:
        r = host_run(f'qemu-img info --output=json "{disk_file}"', timeout=10)
        if r.returncode == 0:
            info = json.loads(r.stdout)
            return jsonify({
                'filename': info.get('filename', ''),
                'format': info.get('format', ''),
                'virtual_size': info.get('virtual-size', 0),
                'virtual_size_human': _human_size(info.get('virtual-size', 0)),
                'actual_size': info.get('actual-size', 0),
                'actual_size_human': _human_size(info.get('actual-size', 0)),
            })
        return jsonify({'error': r.stderr}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vm_bp.route('/machines/<vm_id>/resize-disk', methods=['POST'])
@admin_required
@_require_qemu
def resize_disk(vm_id):
    """Resize a VM's disk (expand only, VM must be stopped)."""
    err = require_tools('qemu-img')
    if err:
        return err
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before resizing disk'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    data = request.get_json(force=True) if request.data else {}
    new_size = data.get('size', '')
    if not re.match(r'^\+?\d+[GMK]$', new_size):
        return jsonify({'error': 'Invalid size (e.g. +10G, +512M)'}), 400

    if not new_size.startswith('+'):
        new_size = '+' + new_size

    disk_file = vm.get('disk_file', '')
    if not disk_file or not os.path.exists(disk_file):
        return jsonify({'error': 'Disk file not found'}), 404

    try:
        r = host_run(f'qemu-img resize "{disk_file}" {new_size}', timeout=30)
        if r.returncode == 0:
            return jsonify({'status': 'ok', 'new_size': new_size})
        return jsonify({'error': r.stderr}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _disk_info_dict(disk_file):
    """Return qemu-img info as a dict for a single disk file."""
    r = host_run(f'qemu-img info --output=json "{disk_file}"', timeout=10)
    if r.returncode != 0:
        return None
    info = json.loads(r.stdout)
    return {
        'filename': info.get('filename', ''),
        'format': info.get('format', ''),
        'virtual_size': info.get('virtual-size', 0),
        'virtual_size_human': _human_size(info.get('virtual-size', 0)),
        'actual_size': info.get('actual-size', 0),
        'actual_size_human': _human_size(info.get('actual-size', 0)),
    }


@vm_bp.route('/machines/<vm_id>/disks')
@admin_required
@_require_qemu
def list_disks(vm_id):
    """List all disks attached to a VM with size info."""
    err = require_tools('qemu-img')
    if err:
        return err
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    result = []
    for disk in vm.get('disks', []):
        entry = {
            'id': disk.get('id', ''),
            'format': disk.get('format', 'qcow2'),
            'size': disk.get('size', ''),
            'bus': disk.get('bus', 'virtio'),
            'bootable': disk.get('id') == 'disk0',
        }
        df = disk.get('file', '')
        if df and os.path.exists(df):
            info = _disk_info_dict(df)
            if info:
                entry.update(info)
        result.append(entry)
    return jsonify({'disks': result})


@vm_bp.route('/machines/<vm_id>/disks', methods=['POST'])
@admin_required
@_require_qemu
def add_disk(vm_id):
    """Add a new disk to a VM (VM must be stopped)."""
    err = require_tools('qemu-img')
    if err:
        return err
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before adding a disk'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    data = request.get_json(force=True) if request.data else {}
    size = data.get('size', '20G')
    fmt = data.get('format', 'qcow2')
    bus = data.get('bus', 'virtio')
    if fmt not in ('qcow2', 'raw'):
        return jsonify({'error': 'Format must be qcow2 or raw'}), 400
    if bus not in ('virtio', 'scsi', 'sata', 'ide'):
        return jsonify({'error': 'Bus must be virtio, scsi, sata, or ide'}), 400
    if not re.match(r'^\d+[GMK]$', size):
        return jsonify({'error': 'Invalid size (e.g. 20G, 512M)'}), 400

    disk_id = _next_disk_id(vm)
    disk_file = os.path.join(_vm_dir(vm_id), f'{disk_id}.{fmt}')

    try:
        r = host_run(f'qemu-img create -f {fmt} "{disk_file}" {size}', timeout=60)
        if r.returncode != 0:
            return jsonify({'error': f'Disk creation failed: {r.stderr}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    new_disk = {'id': disk_id, 'file': disk_file, 'format': fmt,
                'size': size, 'bus': bus}
    vm.setdefault('disks', []).append(new_disk)
    _save_vms(vms)
    return jsonify({'status': 'ok', 'disk': new_disk})


@vm_bp.route('/machines/<vm_id>/disks/<disk_id>', methods=['DELETE'])
@admin_required
@_require_qemu
def remove_disk(vm_id, disk_id):
    """Remove a disk from a VM (VM must be stopped, cannot remove disk0)."""
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before removing a disk'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    if disk_id == 'disk0':
        return jsonify({'error': 'Cannot remove the boot disk'}), 400

    disk = _get_disk(vm, disk_id)
    if not disk:
        return jsonify({'error': f'Disk {disk_id} not found'}), 404

    disk_file = disk.get('file', '')
    if disk_file and os.path.isfile(disk_file):
        os.remove(disk_file)

    vm['disks'] = [d for d in vm['disks'] if d['id'] != disk_id]
    _save_vms(vms)
    return jsonify({'status': 'ok'})


@vm_bp.route('/machines/<vm_id>/disks/<disk_id>/resize', methods=['POST'])
@admin_required
@_require_qemu
def resize_specific_disk(vm_id, disk_id):
    """Resize a specific disk (expand only, VM must be stopped)."""
    err = require_tools('qemu-img')
    if err:
        return err
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before resizing disk'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    disk = _get_disk(vm, disk_id)
    if not disk:
        return jsonify({'error': f'Disk {disk_id} not found'}), 404

    data = request.get_json(force=True) if request.data else {}
    new_size = data.get('size', '')
    if not re.match(r'^\+?\d+[GMK]$', new_size):
        return jsonify({'error': 'Invalid size (e.g. +10G, +512M)'}), 400
    if not new_size.startswith('+'):
        new_size = '+' + new_size

    disk_file = disk.get('file', '')
    if not disk_file or not os.path.exists(disk_file):
        return jsonify({'error': 'Disk file not found'}), 404

    try:
        r = host_run(f'qemu-img resize "{disk_file}" {new_size}', timeout=30)
        if r.returncode == 0:
            return jsonify({'status': 'ok', 'new_size': new_size})
        return jsonify({'error': r.stderr}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  SNAPSHOTS
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/machines/<vm_id>/snapshots')
@admin_required
@_require_qemu
def list_snapshots(vm_id):
    """List disk snapshots for a QCOW2 VM."""
    err = require_tools('qemu-img')
    if err:
        return err
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    disk_file = vm.get('disk_file', '')
    if not disk_file or not os.path.exists(disk_file):
        return jsonify({'snapshots': []})

    if vm.get('disk_format') != 'qcow2':
        return jsonify({'error': 'Snapshots only available for QCOW2 disks'}), 400

    try:
        r = host_run(f'qemu-img snapshot -l "{disk_file}"', timeout=10)
        snapshots = []
        if r.returncode == 0 and r.stdout.strip():
            # Parse qemu-img snapshot -l output
            lines = r.stdout.strip().split('\n')
            for line in lines[2:]:  # Skip headers
                parts = line.split()
                if len(parts) >= 5:
                    snapshots.append({
                        'id': parts[0],
                        'tag': parts[1],
                        'vm_size': parts[2] if len(parts) > 2 else '',
                        'date': parts[3] if len(parts) > 3 else '',
                        'time': parts[4] if len(parts) > 4 else '',
                    })
        return jsonify({'snapshots': snapshots})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vm_bp.route('/machines/<vm_id>/snapshots', methods=['POST'])
@admin_required
@_require_qemu
def create_snapshot(vm_id):
    """Create a disk snapshot (VM must be stopped, disk must be QCOW2)."""
    err = require_tools('qemu-img')
    if err:
        return err
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before creating snapshot'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    if vm.get('disk_format') != 'qcow2':
        return jsonify({'error': 'Snapshots only available for QCOW2'}), 400

    data = request.get_json(force=True) if request.data else {}
    tag = _sanitize_name(data.get('name', f'snap-{int(time.time())}'))

    disk_file = vm.get('disk_file', '')
    try:
        r = host_run(f'qemu-img snapshot -c "{tag}" "{disk_file}"', timeout=30)
        if r.returncode == 0:
            return jsonify({'status': 'ok', 'snapshot': tag})
        return jsonify({'error': r.stderr}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vm_bp.route('/machines/<vm_id>/snapshots/<tag>', methods=['POST'])
@admin_required
@_require_qemu
def restore_snapshot(vm_id, tag):
    """Restore a disk snapshot (VM must be stopped)."""
    err = require_tools('qemu-img')
    if err:
        return err
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Stop VM before restoring snapshot'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    disk_file = vm.get('disk_file', '')
    safe_tag = _sanitize_name(tag)
    try:
        r = host_run(f'qemu-img snapshot -a "{safe_tag}" "{disk_file}"', timeout=30)
        if r.returncode == 0:
            return jsonify({'status': 'ok', 'snapshot': safe_tag})
        return jsonify({'error': r.stderr}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vm_bp.route('/machines/<vm_id>/snapshots/<tag>', methods=['DELETE'])
@admin_required
@_require_qemu
def delete_snapshot(vm_id, tag):
    """Delete a disk snapshot."""
    err = require_tools('qemu-img')
    if err:
        return err
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    disk_file = vm.get('disk_file', '')
    safe_tag = _sanitize_name(tag)
    try:
        r = host_run(f'qemu-img snapshot -d "{safe_tag}" "{disk_file}"', timeout=30)
        if r.returncode == 0:
            return jsonify({'ok': True})
        return jsonify({'error': r.stderr}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ═══════════════════════════════════════════════════════════
#  INSTALLER LOGS PROXY
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/machines/<vm_id>/installer-logs')
@admin_required
def get_installer_logs(vm_id):
    """Proxy installer log entries from a running VM's installer service.

    The EthOS installer inside the VM exposes GET /api/install/logs?since=N
    on port 9000 (the 'EthOS Web' port forward).  This endpoint fetches those
    logs from the host-side forwarded port so that Copilot and other tools
    can access them without needing a direct connection to the VM.

    Query params:
      since (int, default 0) — return only entries after this index
    """
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    if not _check_vm_process(vm_id):
        return jsonify({'error': 'VM is not running'}), 409

    # Find the host port mapped to guest port 9000 (EthOS Web / installer)
    network = vm.get('network') or {}
    host_port = None
    for pf in network.get('port_forwards', []):
        if int(pf.get('guest', 0)) == 9000 and pf.get('host'):
            host_port = int(pf['host'])
            break

    if not host_port:
        return jsonify({'error': 'No port forward found for guest port 9000'}), 404

    since = request.args.get('since', 0, type=int)
    url = f'http://127.0.0.1:{host_port}/api/install/logs?since={since}'
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        return jsonify(data)
    except urllib.error.URLError as e:
        return jsonify({'error': f'Cannot reach installer at port {host_port}: {e.reason}'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  CONVERT DISK IMAGES
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/convert', methods=['POST'])
@admin_required
@_require_qemu
def convert_image():
    """Convert a disk image between formats (raw, qcow2, vdi, vmdk)."""
    err = require_tools('qemu-img')
    if err:
        return err
    data = request.get_json(force=True) if request.data else {}
    source = data.get('source', '')
    target_format = data.get('format', 'qcow2')

    if target_format not in ('raw', 'qcow2', 'vdi', 'vmdk'):
        return jsonify({'error': 'Unsupported target format'}), 400

    if not source or not os.path.exists(source):
        return jsonify({'error': 'Source file not found'}), 404

    source_real = os.path.realpath(source)
    if not _is_allowed_image_path(source_real):
        return jsonify({'error': 'Source path not allowed'}), 403

    base, _ = os.path.splitext(source_real)
    dest = os.path.realpath(f'{base}.{target_format}')
    if not _is_allowed_image_path(dest):
        return jsonify({'error': 'Target path not allowed'}), 403

    try:
        r = host_run(f'qemu-img convert -O {target_format} "{source_real}" "{dest}"', timeout=600)
        if r.returncode == 0:
            return jsonify({'status': 'ok', 'output': dest, 'target_format': target_format})
        return jsonify({'error': r.stderr}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Conversion timed out (10 min)'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  PACKAGE INSTALL / UNINSTALL
# ═══════════════════════════════════════════════════════════

def _on_uninstall(wipe):
    """Cleanup when the VM Manager package is uninstalled."""
    # Stop all running VMs and their websockify/socat processes
    for vm_id in list(_running_vms.keys()):
        try:
            info = _running_vms[vm_id]
            _stop_websockify(info)
            _stop_serial_websockify(info)
            _stop_socat_proxies(info)
            proc = info.get('proc')
            if proc:
                proc.kill()
        except Exception:
            pass
    _running_vms.clear()


register_pkg_routes(
    vm_bp,
    install_message='VM Manager ready — QEMU/KVM installed.',
    install_deps=['qemu-system-x86_64'],
    status_extras=lambda: {
        'qemu_available': _qemu_available(),
        'kvm_available': _kvm_available(),
        'arm_available': _arm_qemu_available(),
        'raspi_available': _raspi_machine_available(),
    },
    on_uninstall=_on_uninstall,
    wipe_files=[_STATE_FILE],
    wipe_dirs=[os.path.abspath(_DEFAULT_VM_DIR)],
)
