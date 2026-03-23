"""
EthOS — VM Manager (Virtual Machine Manager)
Create and manage virtual machines using QEMU/KVM.
Supports booting ISO, IMG, QCOW2 and VDI images.
"""

import os
import json
import re
import signal
import subprocess
import sys
import time
import threading
from functools import wraps
from flask import Blueprint, request, jsonify
from blueprints.admin_required import admin_required

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, check_dep, ensure_dep, get_data_disk as _get_data_disk, app_path as _app_path
from utils import register_pkg_routes

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
_running_vms = {}  # vm_id -> { 'proc': Popen, 'pid': int, 'started': float, 'vnc_port': int }


def _load_vms():
    """Load VM definitions from the state file."""
    try:
        with open(_STATE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_vms(vms):
    """Save VM definitions to the state file."""
    os.makedirs(os.path.dirname(os.path.abspath(_STATE_FILE)), exist_ok=True)
    with open(_STATE_FILE, 'w') as f:
        json.dump(vms, f, indent=2)


# ─── Helpers ─────────────────────────────────────────────────

def _allowed_image_roots():
    roots = [
        os.path.realpath(_iso_root()),
        os.path.realpath(_vm_root()),
    ]
    builder_images = _app_path('installer/images')
    if builder_images:
        roots.append(os.path.realpath(builder_images))
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
            return jsonify({'error': 'QEMU nie jest zainstalowany. Zainstaluj paczkę VM Manager.'}), 503
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.3)
        if proc.poll() is not None:
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


def _check_vm_process(vm_id):
    """Check if a VM process is still running. Clean up if dead."""
    info = _running_vms.get(vm_id)
    if not info:
        return False
    proc = info.get('proc')
    if proc and proc.poll() is None:
        return True
    # Process is dead, clean up websockify too
    _stop_websockify(info)
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
        'message': None if qemu_ok else 'QEMU nie jest zainstalowany.',
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
            'status': 'running' if is_running else 'stopped',
            'vnc_port': info.get('vnc_port') if is_running else None,
            'vnc_display': info.get('vnc_display') if is_running else None,
            'ws_port': info.get('ws_port') if is_running else None,
            'pid': info.get('pid') if is_running else None,
            'started': info.get('started') if is_running else None,
            'created': vm.get('created', ''),
            'description': vm.get('description', ''),
            'disk_file': vm.get('disk_file', ''),
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
    data = request.get_json(force=True) if request.data else {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Nazwa VM jest wymagana'}), 400

    cpu = int(data.get('cpu', 2))
    ram = int(data.get('ram', 1024))  # MB
    disk_size = data.get('disk_size', '20G')
    os_type = data.get('os_type', 'linux')  # linux, windows, other
    boot_image = data.get('boot_image', '')  # ISO/IMG file to boot from
    description = data.get('description', '')
    disk_format = data.get('disk_format', 'qcow2')  # qcow2, raw

    # Validate
    if cpu < 1 or cpu > 32:
        return jsonify({'error': 'CPU: 1-32 rdzeni'}), 400
    if ram < 256 or ram > 65536:
        return jsonify({'error': 'RAM: 256 MB - 64 GB'}), 400
    if not re.match(r'^\d+[GMK]?$', disk_size):
        return jsonify({'error': 'Nieprawidłowy rozmiar dysku (np. 20G, 512M)'}), 400
    if boot_image:
        boot_image_real = os.path.realpath(boot_image)
        if not _is_allowed_image_path(boot_image_real):
            return jsonify({'error': 'Niedozwolona ścieżka obrazu'}), 403
        boot_image = boot_image_real

    vm_id = _sanitize_name(name).lower().replace(' ', '-')
    vm_id = re.sub(r'-+', '-', vm_id)
    ts = str(int(time.time()))[-6:]
    vm_id = f'{vm_id}-{ts}'

    vm_path = _vm_dir(vm_id)
    os.makedirs(vm_path, exist_ok=True)

    # Create virtual disk
    disk_file = os.path.join(vm_path, f'disk.{disk_format}')
    try:
        r = host_run(
            f'qemu-img create -f {disk_format} "{disk_file}" {disk_size}',
            timeout=60
        )
        if r.returncode != 0:
            return jsonify({'error': f'Błąd tworzenia dysku: {r.stderr}'}), 500
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
        'os_type': os_type,
        'boot_image': boot_image,
        'description': description,
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    _save_vms(vms)

    return jsonify({'status': 'ok', 'id': vm_id, 'name': name})


@vm_bp.route('/machines/<vm_id>', methods=['PUT'])
@admin_required
@_require_qemu
def update_vm(vm_id):
    """Update VM configuration (only when VM is stopped)."""
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Zatrzymaj VM przed edycją konfiguracji'}), 409

    vms = _load_vms()
    if vm_id not in vms:
        return jsonify({'error': 'VM nie znaleziona'}), 404

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
                return jsonify({'error': 'Niedozwolona ścieżka obrazu'}), 403
            new_boot = real_boot
        vm['boot_image'] = new_boot
    if 'description' in data:
        vm['description'] = data['description']

    _save_vms(vms)
    return jsonify({'ok': True})


@vm_bp.route('/machines/<vm_id>', methods=['DELETE'])
@admin_required
@_require_qemu
def delete_vm(vm_id):
    """Delete a virtual machine and its disk files."""
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Zatrzymaj VM przed usunięciem'}), 409

    vms = _load_vms()
    if vm_id not in vms:
        return jsonify({'error': 'VM nie znaleziona'}), 404

    # Remove VM directory
    vm_path = _vm_dir(vm_id)
    if os.path.isdir(vm_path):
        import shutil
        shutil.rmtree(vm_path, ignore_errors=True)

    del vms[vm_id]
    _save_vms(vms)
    return jsonify({'status': 'ok'})


# ═══════════════════════════════════════════════════════════
#  VM POWER CONTROL
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/machines/<vm_id>/start', methods=['POST'])
@admin_required
@_require_qemu
def start_vm(vm_id):
    """Start a virtual machine."""
    if _check_vm_process(vm_id):
        return jsonify({'error': 'VM już działa'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM nie znaleziona'}), 404

    vnc_display, vnc_port = _next_vnc_port()
    kvm = _kvm_available()

    boot_image = vm.get('boot_image', '')
    boot_image_real = os.path.realpath(boot_image) if boot_image else ''
    if boot_image and not _is_allowed_image_path(boot_image_real):
        return jsonify({'error': 'Niedozwolona ścieżka obrazu'}), 403
    boot_image = boot_image_real

    is_arm = _is_arm_image(boot_image, vm.get('name', ''))
    is_rpi = _is_rpi_image(boot_image, vm.get('name', ''))

    # ── Raspberry Pi VM (raspi3b machine) ─────────────────────
    if is_rpi:
        if not _arm_qemu_available():
            return jsonify({'error': 'qemu-system-aarch64 nie jest zainstalowany. Zainstaluj: apt install qemu-system-arm'}), 503
        if not _raspi_machine_available():
            return jsonify({'error': 'QEMU nie obsługuje maszyny raspi3b. Zaktualizuj QEMU do wersji >= 8.0'}), 503

        # The boot image (RPi OS .img) is the main SD card — must be writable.
        # We work on a copy so the original stays intact.
        if boot_image and os.path.exists(boot_image):
            vm_path = _vm_dir(vm_id)
            sd_copy = os.path.join(vm_path, 'sd-card.img')
            if not os.path.exists(sd_copy):
                import shutil
                _logger.info('Kopiowanie obrazu RPi jako SD card: %s → %s', boot_image, sd_copy)
                shutil.copy2(boot_image, sd_copy)
        else:
            return jsonify({'error': 'Brak obrazu boot RPi (.img)'}), 400

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
                return jsonify({'error': f'Nie udało się wyodrębnić kernela/DTB z obrazu RPi: {r.stderr[-200:]}'}), 500

        if not os.path.exists(kernel_path):
            return jsonify({'error': 'Brak kernel8.img w obrazie RPi'}), 400
        if not os.path.exists(dtb_path):
            return jsonify({'error': 'Brak pliku DTB (bcm2710-rpi-3-b*.dtb) w obrazie RPi'}), 400

        cmd = ['qemu-system-aarch64']
        cmd += ['-machine', 'raspi3b']
        # raspi3b: fixed 1 GB RAM — QEMU ignores -m for this machine

        # Kernel + DTB (required — raspi3b has no GPU firmware emulation)
        cmd += ['-kernel', kernel_path]
        cmd += ['-dtb', dtb_path]
        cmd += ['-append', 'console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait']

        # SD card — main boot drive (RPi boots from SD)
        cmd += ['-drive', f'file={sd_copy},format=raw,if=sd']

        # Additional data disk (qcow2) — attach via USB mass-storage
        # NOTE: raspi3b USB emulation is limited; this may not work
        disk_file = vm.get('disk_file', '')
        if disk_file and os.path.exists(disk_file):
            disk_format = vm.get('disk_format', 'qcow2')
            cmd += ['-drive', f'file={disk_file},format={disk_format},if=none,id=usbdisk']
            cmd += ['-device', 'usb-storage,drive=usbdisk']

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
            return jsonify({'error': 'qemu-system-aarch64 nie jest zainstalowany. Zainstaluj: apt install qemu-system-arm qemu-efi-aarch64'}), 503

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

        # Disk
        disk_file = vm.get('disk_file', '')
        if disk_file and os.path.exists(disk_file):
            disk_format = vm.get('disk_format', 'qcow2')
            cmd += ['-drive', f'file={disk_file},format={disk_format},if=virtio']

        # Boot image — mount as second drive for generic ARM
        if boot_image and os.path.exists(boot_image):
            ext = os.path.splitext(boot_image)[1].lower()
            fmt = 'raw' if ext in ('.img', '.raw') else 'qcow2'
            cmd += ['-drive', f'file={boot_image},format={fmt},if=virtio']

        # Network
        net_opts = 'user,id=net0'
        if vm.get('os_type') == 'linux':
            net_opts += ',hostfwd=tcp::0-:22'
        cmd += ['-netdev', net_opts, '-device', 'virtio-net-pci,netdev=net0']

        # VNC and display
        cmd += ['-vnc', f':{vnc_display}']
        cmd += ['-device', 'virtio-gpu-pci']
        cmd += ['-device', 'usb-ehci', '-device', 'usb-tablet']
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

        # Disk
        disk_file = vm.get('disk_file', '')
        if disk_file and os.path.exists(disk_file):
            disk_format = vm.get('disk_format', 'qcow2')
            cmd += ['-drive', f'file={disk_file},format={disk_format},if=virtio']

        # Boot image (ISO/IMG)
        if boot_image and os.path.exists(boot_image):
            ext = os.path.splitext(boot_image)[1].lower()
            if ext in ('.iso',):
                cmd += ['-cdrom', boot_image]
                cmd += ['-boot', 'd']  # Boot from CD
            elif ext in ('.img', '.raw'):
                cmd += ['-drive', f'file={boot_image},format=raw,if=virtio,readonly=on']
            elif ext in ('.qcow2',):
                cmd += ['-drive', f'file={boot_image},format=qcow2,if=virtio,readonly=on']
            elif ext in ('.vdi',):
                cmd += ['-drive', f'file={boot_image},format=vdi,if=virtio,readonly=on']
            elif ext in ('.vmdk',):
                cmd += ['-drive', f'file={boot_image},format=vmdk,if=virtio,readonly=on']

        # Network — user-mode NAT with port forwarding
        net_opts = 'user,id=net0'
        # Forward SSH for Linux VMs
        if vm.get('os_type') == 'linux':
            net_opts += ',hostfwd=tcp::0-:22'
        # Forward RDP for Windows VMs
        if vm.get('os_type') == 'windows':
            net_opts += ',hostfwd=tcp::0-:3389'
        cmd += ['-netdev', net_opts, '-device', 'virtio-net-pci,netdev=net0']

        # VNC display (for remote access through browser)
        cmd += ['-vnc', f':{vnc_display}']

        # UEFI if available (for Windows and modern Linux)
        ovmf_paths = [
            '/usr/share/OVMF/OVMF_CODE.fd',
            '/usr/share/ovmf/OVMF.fd',
            '/usr/share/qemu/OVMF.fd',
        ]
        if vm.get('os_type') in ('windows', 'uefi'):
            for ovmf in ovmf_paths:
                if os.path.exists(ovmf):
                    cmd += ['-bios', ovmf]
                    break

        # USB tablet for better mouse tracking in VNC
        cmd += ['-device', 'usb-ehci', '-device', 'usb-tablet']

        # VGA adapter — virtio-gpu for best performance in VNC/noVNC
        cmd += ['-vga', 'virtio']

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
            return jsonify({'error': f'QEMU nie uruchomił się: {stderr[:500]}'}), 500

        _running_vms[vm_id] = {
            'proc': proc,
            'pid': proc.pid,
            'started': time.time(),
            'vnc_port': vnc_port,
            'vnc_display': vnc_display,
            'ws_proc': None,
            'ws_port': None,
        }

        # Start websockify for browser-based console (noVNC)
        ws_port = _next_ws_port()
        ws_proc = _start_websockify(vnc_port, ws_port)
        if ws_proc:
            _running_vms[vm_id]['ws_proc'] = ws_proc
            _running_vms[vm_id]['ws_port'] = ws_port

        return jsonify({
            'ok': True,
            'pid': proc.pid,
            'vnc_port': vnc_port,
            'vnc_display': vnc_display,
            'ws_port': _running_vms[vm_id].get('ws_port'),
            'kvm': kvm,
            'message': f'VM uruchomiona (VNC: :{vnc_display})',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vm_bp.route('/machines/<vm_id>/stop', methods=['POST'])
@admin_required
@_require_qemu
def stop_vm(vm_id):
    """Stop (gracefully or forcefully) a virtual machine."""
    if not _check_vm_process(vm_id):
        return jsonify({'error': 'VM nie działa'}), 409

    data = request.get_json(force=True) if request.data else {}
    force = data.get('force', False)

    info = _running_vms.get(vm_id)
    if not info:
        return jsonify({'error': 'VM nie znaleziona'}), 404

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
    _running_vms.pop(vm_id, None)
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
        return jsonify({'error': 'Plik źródłowy nie istnieje'}), 404
    # Security: only allow files from the builder images directory
    images_dir = os.path.realpath(_app_path('installer/images'))
    real_src = os.path.realpath(src)
    if not real_src.startswith(images_dir + '/'):
        return jsonify({'error': 'Niedozwolona ścieżka'}), 403
    dest = os.path.join(_iso_root(), os.path.basename(src))
    if os.path.exists(dest):
        return jsonify({'error': f'Plik "{os.path.basename(src)}" już istnieje w obrazach VM'}), 409
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
        return jsonify({'error': 'Brak pliku'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Brak nazwy pliku'}), 400

    valid_exts = {'.iso', '.img', '.raw', '.qcow2', '.vdi', '.vmdk'}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in valid_exts:
        return jsonify({'error': f'Nieobsługiwany format: {ext}. Dozwolone: {", ".join(valid_exts)}'}), 400

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
        return jsonify({'error': 'Plik nie znaleziony'}), 404

    # Check if any VM uses this image
    vms = _load_vms()
    for vm_id, vm in vms.items():
        if vm.get('boot_image') == fpath:
            return jsonify({'error': f'Obraz używany przez VM "{vm.get("name", vm_id)}"'}), 409

    os.remove(fpath)
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════
#  DISK MANAGEMENT
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/machines/<vm_id>/disk-info')
@admin_required
@_require_qemu
def disk_info(vm_id):
    """Get info about a VM's disk file."""
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM nie znaleziona'}), 404

    disk_file = vm.get('disk_file', '')
    if not disk_file or not os.path.exists(disk_file):
        return jsonify({'error': 'Brak pliku dysku'}), 404

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
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Zatrzymaj VM przed zmianą rozmiaru dysku'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM nie znaleziona'}), 404

    data = request.get_json(force=True) if request.data else {}
    new_size = data.get('size', '')
    if not re.match(r'^\+?\d+[GMK]$', new_size):
        return jsonify({'error': 'Nieprawidłowy rozmiar (np. +10G, +512M)'}), 400

    if not new_size.startswith('+'):
        new_size = '+' + new_size

    disk_file = vm.get('disk_file', '')
    if not disk_file or not os.path.exists(disk_file):
        return jsonify({'error': 'Brak pliku dysku'}), 404

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
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM nie znaleziona'}), 404

    disk_file = vm.get('disk_file', '')
    if not disk_file or not os.path.exists(disk_file):
        return jsonify({'snapshots': []})

    if vm.get('disk_format') != 'qcow2':
        return jsonify({'error': 'Snapshoty dostępne tylko dla dysków QCOW2'}), 400

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
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Zatrzymaj VM przed tworzeniem snapshotu'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM nie znaleziona'}), 404

    if vm.get('disk_format') != 'qcow2':
        return jsonify({'error': 'Snapshoty dostępne tylko dla QCOW2'}), 400

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
    if _check_vm_process(vm_id):
        return jsonify({'error': 'Zatrzymaj VM przed przywracaniem snapshotu'}), 409

    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM nie znaleziona'}), 404

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
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM nie znaleziona'}), 404

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
#  CONVERT DISK IMAGES
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/convert', methods=['POST'])
@admin_required
@_require_qemu
def convert_image():
    """Convert a disk image between formats (raw, qcow2, vdi, vmdk)."""
    data = request.get_json(force=True) if request.data else {}
    source = data.get('source', '')
    target_format = data.get('format', 'qcow2')

    if target_format not in ('raw', 'qcow2', 'vdi', 'vmdk'):
        return jsonify({'error': 'Nieobsługiwany format docelowy'}), 400

    if not source or not os.path.exists(source):
        return jsonify({'error': 'Plik źródłowy nie istnieje'}), 404

    source_real = os.path.realpath(source)
    if not _is_allowed_image_path(source_real):
        return jsonify({'error': 'Niedozwolona ścieżka źródłowa'}), 403

    base, _ = os.path.splitext(source_real)
    dest = os.path.realpath(f'{base}.{target_format}')
    if not _is_allowed_image_path(dest):
        return jsonify({'error': 'Niedozwolona ścieżka docelowa'}), 403

    try:
        r = host_run(f'qemu-img convert -O {target_format} "{source_real}" "{dest}"', timeout=600)
        if r.returncode == 0:
            return jsonify({'status': 'ok', 'output': dest, 'target_format': target_format})
        return jsonify({'error': r.stderr}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Konwersja przekroczyła limit czasu (10 min)'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  PACKAGE INSTALL / UNINSTALL
# ═══════════════════════════════════════════════════════════

def _on_uninstall(wipe):
    """Cleanup when the VM Manager package is uninstalled."""
    # Stop all running VMs and their websockify processes
    for vm_id in list(_running_vms.keys()):
        try:
            info = _running_vms[vm_id]
            _stop_websockify(info)
            proc = info.get('proc')
            if proc:
                proc.kill()
        except Exception:
            pass
    _running_vms.clear()


register_pkg_routes(
    vm_bp,
    install_message='VM Manager gotowy — QEMU/KVM zainstalowane.',
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
