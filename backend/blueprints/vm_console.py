import os
import re
import json
import logging
import urllib.request
from flask import request, jsonify, send_from_directory, abort
from blueprints.admin_required import admin_required
from blueprints.vm_manager import (
    vm_bp,
    _load_vms, _check_vm_process,
    _NOVNC_DIR, host_run,
)

log = logging.getLogger('vm-manager')

# ─── noVNC static proxy (same-origin for iframe) ─────────

@vm_bp.route('/novnc/<path:filename>')
def novnc_static(filename):
    """Serve noVNC files through Flask so the console iframe stays same-origin.
    No auth required — these are static open-source UI files, not data."""
    novnc_dir = os.path.abspath(_NOVNC_DIR)
    if not os.path.isdir(novnc_dir):
        abort(404)
    return send_from_directory(novnc_dir, filename)


# ═══════════════════════════════════════════════════════════
#  USB PASSTHROUGH
# ═══════════════════════════════════════════════════════════

def _list_host_usb():
    """Return a list of USB devices attached to the host.

    Parses lsusb output, filters out hubs, then enriches each entry with
    block-device name and size by walking the sysfs device chain.
    Returns dicts: bus, device, vendorid, productid, name, block_dev, size.
    """
    try:
        out = host_run('lsusb', timeout=5).stdout
    except Exception:
        out = ''

    # Build a map of vid:pid -> {block_dev, size} from lsblk + sysfs
    block_map = {}
    try:
        r = host_run("lsblk -J -o NAME,TRAN,SIZE 2>/dev/null", timeout=5)
        bd = json.loads(r.stdout).get('blockdevices', [])
        for dev in bd:
            if dev.get('tran') != 'usb':
                continue
            name = dev.get('name', '')
            size = dev.get('size', '')
            # Walk sysfs chain to get idVendor / idProduct
            try:
                sysfs = os.path.realpath(f'/sys/block/{name}')
                cur = sysfs
                while cur and cur != '/':
                    vid_f = os.path.join(cur, 'idVendor')
                    pid_f = os.path.join(cur, 'idProduct')
                    if os.path.isfile(vid_f) and os.path.isfile(pid_f):
                        vid = open(vid_f).read().strip().lower()
                        pid = open(pid_f).read().strip().lower()
                        block_map[f'{vid}:{pid}'] = {'block_dev': name, 'size': size}
                        break
                    cur = os.path.dirname(cur)
            except Exception:
                pass
    except Exception:
        pass

    hub_keywords = ('root hub', 'hub class', 'usb hub')
    devices = []
    for line in out.splitlines():
        m = re.match(
            r'Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)',
            line.strip()
        )
        if not m:
            continue
        bus, dev, vid, pid, name = m.groups()
        name = name.strip()
        if any(kw in name.lower() for kw in hub_keywords):
            continue
        entry = {
            'bus': int(bus),
            'device': int(dev),
            'vendorid': vid.lower(),
            'productid': pid.lower(),
            'name': name or f'{vid}:{pid}',
        }
        entry.update(block_map.get(f'{vid.lower()}:{pid.lower()}', {}))
        devices.append(entry)
    return devices


@vm_bp.route('/usb-devices')
@admin_required
def list_usb_devices():
    """List USB devices on the host available for passthrough."""
    return jsonify(_list_host_usb())


@vm_bp.route('/machines/<vm_id>/usb', methods=['GET'])
@admin_required
def get_vm_usb(vm_id):
    """List USB passthrough devices configured for a VM."""
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404
    return jsonify({'usb_devices': vm.get('usb_devices', [])})


@vm_bp.route('/machines/<vm_id>/usb', methods=['POST'])
@admin_required
def attach_vm_usb(vm_id):
    """Attach a USB device to a VM (persisted in config, applied on next start)."""
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    vendorid = data.get('vendorid', '').strip().lower().lstrip('0x')
    productid = data.get('productid', '').strip().lower().lstrip('0x')
    name = data.get('name', f'{vendorid}:{productid}')

    if not re.fullmatch(r'[0-9a-f]{4}', vendorid) or not re.fullmatch(r'[0-9a-f]{4}', productid):
        return jsonify({'error': 'Invalid vendorid or productid (must be 4-digit hex)'}), 400

    usb_devices = vm.setdefault('usb_devices', [])
    # Prevent duplicates
    for ud in usb_devices:
        if ud.get('vendorid') == vendorid and ud.get('productid') == productid:
            return jsonify({'error': 'Device already attached'}), 409

    usb_devices.append({'vendorid': vendorid, 'productid': productid, 'name': name})
    _save_vms(vms)
    return jsonify({'ok': True, 'usb_devices': usb_devices})


@vm_bp.route('/machines/<vm_id>/usb/<int:index>', methods=['DELETE'])
@admin_required
def detach_vm_usb(vm_id, index):
    """Remove a USB passthrough device from VM config by index."""
    vms = _load_vms()
    vm = vms.get(vm_id)
    if not vm:
        return jsonify({'error': 'VM not found'}), 404

    usb_devices = vm.get('usb_devices', [])
    if index < 0 or index >= len(usb_devices):
        return jsonify({'error': 'Index out of range'}), 400

    removed = usb_devices.pop(index)
    vm['usb_devices'] = usb_devices
    _save_vms(vms)
    return jsonify({'ok': True, 'removed': removed})


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
