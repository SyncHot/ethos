import os
import re
import json
import time
from flask import request, jsonify
from blueprints.admin_required import admin_required
from blueprints.vm_manager import (
    vm_bp, _require_qemu,
    _load_vms, _save_vms, _check_vm_process,
    _get_disk, _next_disk_id, _vm_dir, _sanitize_name,
    _human_size, host_run, require_tools,
)

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
