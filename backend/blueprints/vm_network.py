import subprocess
import time
import logging
from flask import request, jsonify
from blueprints.admin_required import admin_required
from blueprints.vm_manager import (
    vm_bp, _require_qemu,
    _load_vms, _save_vms, _check_vm_process,
    _validate_port_forwards, _validate_bridge_name,
    _BRIDGE_NAME, _bridge_status, _setup_bridge,
)

log = logging.getLogger('vm-manager')

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
