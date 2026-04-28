"""
EthOS — RAID / LVM Manager Blueprint
Create and manage mdadm arrays and LVM volume groups / logical volumes.
"""

import json
import os
import re
import sys
import time

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_base, q as _q
from blueprints.admin_required import admin_required

raid_bp = Blueprint('raid', __name__, url_prefix='/api/raid')

_VALID_LEVELS = {'0', '1', '5', '6', '10'}
_DEV_RE = re.compile(r'^/dev/[a-zA-Z0-9/_-]+$')


def host_run(cmd, **kw):
    return _host_run_base(cmd, **kw)


def _validate_dev(path):
    """Return True if *path* looks like a legitimate device path."""
    return bool(path and _DEV_RE.match(path) and '..' not in path)


# ──────────────────────────────────────────────────────────
#  RAID helpers
# ──────────────────────────────────────────────────────────

def _parse_mdstat():
    """Parse /proc/mdstat into a list of array dicts."""
    r = host_run('cat /proc/mdstat')
    if r.returncode != 0:
        return []
    arrays = []
    current = None
    for line in r.stdout.splitlines():
        m = re.match(r'^(md\d+)\s*:\s*(\w+)\s+(\w+)\s+(.*)', line)
        if m:
            current = {
                'name': m.group(1),
                'device': f'/dev/{m.group(1)}',
                'state': m.group(2),
                'level': m.group(3),
                'members': re.findall(r'(\w+)\[\d+\](?:\(S\))?', m.group(4)),
                'spares': re.findall(r'(\w+)\[\d+\]\(S\)', m.group(4)),
                'sync': None,
            }
            arrays.append(current)
            continue
        if current:
            # Resync / recovery progress line
            pm = re.search(r'(\w+)\s*=\s*([\d.]+)%', line)
            if pm:
                current['sync'] = {'action': pm.group(1), 'progress': float(pm.group(2))}
            bm = re.search(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]', line)
            if bm:
                current['total_disks'] = int(bm.group(1))
                current['active_disks'] = int(bm.group(2))
                current['bitmap'] = bm.group(3)
    return arrays


def _detail_scan():
    """Run mdadm --detail --scan and return list of array info dicts."""
    r = host_run('mdadm --detail --scan 2>/dev/null')
    if r.returncode != 0:
        return []
    results = []
    for line in r.stdout.splitlines():
        m = re.match(r'ARRAY\s+(\S+)\s+(.*)', line)
        if not m:
            continue
        info = {'device': m.group(1), 'name': os.path.basename(m.group(1))}
        for kv in re.findall(r'(\w+)=(\S+)', m.group(2)):
            info[kv[0].lower()] = kv[1]
        results.append(info)
    return results


def _array_detail(device):
    """Run mdadm --detail on a single array device."""
    if not _validate_dev(device):
        return None
    r = host_run(f'mdadm --detail {_q(device)}')
    if r.returncode != 0:
        return None
    info = {'device': device, 'disks': []}
    for line in r.stdout.splitlines():
        line = line.strip()
        kv = line.split(':', 1)
        if len(kv) == 2:
            key = kv[0].strip().lower().replace(' ', '_')
            val = kv[1].strip()
            if key in ('raid_level', 'array_size', 'state', 'active_devices',
                        'working_devices', 'failed_devices', 'spare_devices',
                        'uuid', 'name', 'creation_time', 'rebuild_status',
                        'total_devices', 'persistence'):
                info[key] = val
        dm = re.match(r'^\d+\s+\d+\s+\d+\s+\d+\s+(.+?)\s+(/dev/\S+)$', line)
        if dm:
            info['disks'].append({'state': dm.group(1).strip(), 'device': dm.group(2)})
    return info


def _available_disks():
    """List block devices that are not mounted, not in an array, and are whole disks or partitions."""
    r = host_run('lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,TRAN')
    if r.returncode != 0:
        return []

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []

    # Gather devices already in arrays
    in_array = set()
    for arr in _parse_mdstat():
        for m in arr.get('members', []):
            in_array.add(m)
        for s in arr.get('spares', []):
            in_array.add(s)

    # Gather devices used as LVM PVs
    pv_devs = set()
    pvr = host_run('pvs --noheadings -o pv_name 2>/dev/null')
    if pvr.returncode == 0:
        for line in pvr.stdout.splitlines():
            pv_devs.add(line.strip())

    available = []
    for bd in data.get('blockdevices', []):
        _collect_available(bd, '', in_array, pv_devs, available)
    return available


def _collect_available(node, parent_name, in_array, pv_devs, out):
    """Recursively collect available (unused) disks/partitions from lsblk JSON."""
    name = node.get('name', '')
    dtype = node.get('type', '')
    mount = node.get('mountpoint') or ''
    fstype = node.get('fstype') or ''
    children = node.get('children', [])
    dev = f'/dev/{name}'

    # Skip loop, rom, etc.
    if dtype not in ('disk', 'part'):
        return

    # If it has children (partitions), recurse into them but don't offer the whole disk
    if children:
        for child in children:
            _collect_available(child, name, in_array, pv_devs, out)
        return

    # Skip if mounted, in array, used as PV, or has a filesystem we shouldn't touch
    if mount:
        return
    if name in in_array:
        return
    if dev in pv_devs:
        return
    # Skip swap
    if fstype == 'swap':
        return

    out.append({
        'name': name,
        'device': dev,
        'size': node.get('size', ''),
        'type': dtype,
        'model': node.get('model') or '',
        'tran': node.get('tran') or '',
        'fstype': fstype,
    })


# ──────────────────────────────────────────────────────────
#  LVM helpers
# ──────────────────────────────────────────────────────────

def _list_vgs():
    r = host_run('vgs --reportformat json 2>/dev/null')
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
        return data.get('report', [{}])[0].get('vg', [])
    except (json.JSONDecodeError, IndexError):
        return []


def _list_lvs():
    r = host_run('lvs --reportformat json -o lv_name,vg_name,lv_size,lv_attr,lv_path 2>/dev/null')
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
        return data.get('report', [{}])[0].get('lv', [])
    except (json.JSONDecodeError, IndexError):
        return []


def _list_pvs():
    r = host_run('pvs --reportformat json 2>/dev/null')
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
        return data.get('report', [{}])[0].get('pv', [])
    except (json.JSONDecodeError, IndexError):
        return []


# ──────────────────────────────────────────────────────────
#  RAID endpoints
# ──────────────────────────────────────────────────────────

@raid_bp.route('/arrays')
@admin_required
def list_arrays():
    """List all mdadm arrays with merged info from mdstat and detail scan."""
    mdstat = {a['name']: a for a in _parse_mdstat()}
    scan = {a['name']: a for a in _detail_scan()}

    arrays = []
    seen = set()
    for name in list(mdstat.keys()) + list(scan.keys()):
        if name in seen:
            continue
        seen.add(name)
        merged = {'name': name, 'device': f'/dev/{name}'}
        if name in mdstat:
            merged.update(mdstat[name])
        if name in scan:
            for k, v in scan[name].items():
                if k not in merged or not merged[k]:
                    merged[k] = v
        detail = _array_detail(merged['device'])
        if detail:
            for k, v in detail.items():
                if k not in merged or not merged[k]:
                    merged[k] = v
        arrays.append(merged)

    return jsonify(arrays)


@raid_bp.route('/disks')
@admin_required
def list_available_disks():
    """List disks available for array creation."""
    return jsonify(_available_disks())


@raid_bp.route('/arrays', methods=['POST'])
@admin_required
def create_array():
    """Create a new mdadm array."""
    data = request.get_json(force=True)
    level = str(data.get('level', ''))
    devices = data.get('devices', [])
    spares = int(data.get('spares', 0))
    name = data.get('name', '').strip()

    if level not in _VALID_LEVELS:
        return jsonify({'error': f'Invalid RAID level: {level}'}), 400

    if not devices or len(devices) < 2:
        return jsonify({'error': 'At least 2 devices required'}), 400

    min_devs = {'0': 2, '1': 2, '5': 3, '6': 4, '10': 4}
    active_count = len(devices) - spares
    if active_count < min_devs.get(level, 2):
        return jsonify({'error': f'RAID {level} requires at least {min_devs[level]} active devices'}), 400

    if spares < 0 or spares >= len(devices):
        return jsonify({'error': 'Invalid spare count'}), 400

    for dev in devices:
        if not _validate_dev(dev):
            return jsonify({'error': f'Invalid device path: {dev}'}), 400

    # Find next free md device
    if name:
        if not re.match(r'^[a-zA-Z0-9_-]+$', name):
            return jsonify({'error': 'Invalid array name'}), 400
    else:
        existing = {a['name'] for a in _parse_mdstat()}
        for i in range(128):
            cand = f'md{i}'
            if cand not in existing:
                name = cand
                break
        else:
            return jsonify({'error': 'No free md device numbers'}), 500

    md_dev = f'/dev/{name}'
    dev_args = ' '.join(_q(d) for d in devices)

    cmd = (
        f'mdadm --create {_q(md_dev)} '
        f'--level={_q(level)} '
        f'--raid-devices={active_count} '
    )
    if spares > 0:
        cmd += f'--spare-devices={spares} '
    cmd += f'--run --force {dev_args}'

    r = host_run(cmd, timeout=60)
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or r.stdout.strip()}), 500

    # Save config
    host_run('mdadm --detail --scan >> /etc/mdadm/mdadm.conf 2>/dev/null || true')
    host_run('update-initramfs -u 2>/dev/null || true', timeout=120)

    return jsonify({'ok': True, 'device': md_dev, 'name': name})


@raid_bp.route('/arrays/<name>', methods=['DELETE'])
@admin_required
def delete_array(name):
    """Stop and remove an mdadm array."""
    if not re.match(r'^md\d+$', name):
        return jsonify({'error': 'Invalid array name'}), 400

    md_dev = f'/dev/{name}'

    # Get member devices before stopping
    detail = _array_detail(md_dev)
    member_devs = [d['device'] for d in (detail or {}).get('disks', []) if _validate_dev(d['device'])]

    r = host_run(f'mdadm --stop {_q(md_dev)}')
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to stop array'}), 500

    # Zero superblocks on member devices
    for dev in member_devs:
        host_run(f'mdadm --zero-superblock {_q(dev)} 2>/dev/null || true')

    # Remove from config
    host_run(f"sed -i '/{name}/d' /etc/mdadm/mdadm.conf 2>/dev/null || true")

    return jsonify({'ok': True})


@raid_bp.route('/arrays/<name>/add', methods=['POST'])
@admin_required
def add_disk_to_array(name):
    """Add a disk to an existing array (as active or spare)."""
    if not re.match(r'^md\d+$', name):
        return jsonify({'error': 'Invalid array name'}), 400

    data = request.get_json(force=True)
    device = data.get('device', '')
    if not _validate_dev(device):
        return jsonify({'error': f'Invalid device path: {device}'}), 400

    md_dev = f'/dev/{name}'
    r = host_run(f'mdadm --add {_q(md_dev)} {_q(device)}')
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to add device'}), 500

    return jsonify({'ok': True})


@raid_bp.route('/arrays/<name>/remove', methods=['POST'])
@admin_required
def remove_disk_from_array(name):
    """Mark a disk as failed and remove it from the array."""
    if not re.match(r'^md\d+$', name):
        return jsonify({'error': 'Invalid array name'}), 400

    data = request.get_json(force=True)
    device = data.get('device', '')
    if not _validate_dev(device):
        return jsonify({'error': f'Invalid device path: {device}'}), 400

    md_dev = f'/dev/{name}'
    # Mark failed first, then remove
    host_run(f'mdadm --fail {_q(md_dev)} {_q(device)}')
    r = host_run(f'mdadm --remove {_q(md_dev)} {_q(device)}')
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to remove device'}), 500

    return jsonify({'ok': True})


@raid_bp.route('/arrays/<name>/status')
@admin_required
def array_status(name):
    """Detailed status of a single array."""
    if not re.match(r'^md\d+$', name):
        return jsonify({'error': 'Invalid array name'}), 400

    md_dev = f'/dev/{name}'
    detail = _array_detail(md_dev)
    if not detail:
        return jsonify({'error': 'Array not found'}), 404

    # Merge sync progress from mdstat
    for arr in _parse_mdstat():
        if arr['name'] == name:
            detail['sync'] = arr.get('sync')
            detail['bitmap'] = arr.get('bitmap')
            break

    return jsonify(detail)


# ──────────────────────────────────────────────────────────
#  LVM endpoints
# ──────────────────────────────────────────────────────────

@raid_bp.route('/lvm/vgs')
@admin_required
def lvm_list_vgs():
    """List LVM volume groups with their PVs."""
    vgs = _list_vgs()
    pvs = _list_pvs()
    # Attach PV list to each VG
    for vg in vgs:
        vg['pvs'] = [p for p in pvs if p.get('vg_name') == vg.get('vg_name')]
    return jsonify(vgs)


@raid_bp.route('/lvm/lvs')
@admin_required
def lvm_list_lvs():
    """List LVM logical volumes."""
    return jsonify(_list_lvs())


@raid_bp.route('/lvm/pvs')
@admin_required
def lvm_list_pvs():
    """List LVM physical volumes."""
    return jsonify(_list_pvs())


@raid_bp.route('/lvm/vg', methods=['POST'])
@admin_required
def lvm_create_vg():
    """Create a volume group from physical volumes."""
    data = request.get_json(force=True)
    vg_name = data.get('name', '').strip()
    devices = data.get('devices', [])

    if not vg_name or not re.match(r'^[a-zA-Z0-9_.-]+$', vg_name):
        return jsonify({'error': 'Invalid VG name'}), 400

    if not devices:
        return jsonify({'error': 'At least one device required'}), 400

    for dev in devices:
        if not _validate_dev(dev):
            return jsonify({'error': f'Invalid device path: {dev}'}), 400

    # Initialize PVs
    for dev in devices:
        r = host_run(f'pvcreate -f {_q(dev)}', timeout=30)
        if r.returncode != 0:
            return jsonify({'error': f'pvcreate failed on {dev}: {r.stderr.strip()}'}), 500

    dev_args = ' '.join(_q(d) for d in devices)
    r = host_run(f'vgcreate {_q(vg_name)} {dev_args}', timeout=30)
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to create VG'}), 500

    return jsonify({'ok': True, 'vg_name': vg_name})


@raid_bp.route('/lvm/lv', methods=['POST'])
@admin_required
def lvm_create_lv():
    """Create a logical volume in a volume group."""
    data = request.get_json(force=True)
    vg_name = data.get('vg_name', '').strip()
    lv_name = data.get('name', '').strip()
    size = data.get('size', '').strip()
    use_all = data.get('use_all', False)

    if not vg_name or not re.match(r'^[a-zA-Z0-9_.-]+$', vg_name):
        return jsonify({'error': 'Invalid VG name'}), 400
    if not lv_name or not re.match(r'^[a-zA-Z0-9_.-]+$', lv_name):
        return jsonify({'error': 'Invalid LV name'}), 400

    if use_all:
        cmd = f'lvcreate -l 100%FREE -n {_q(lv_name)} {_q(vg_name)}'
    else:
        if not size or not re.match(r'^\d+(\.\d+)?[MGTmgt]?$', size):
            return jsonify({'error': 'Invalid size (e.g. 10G, 500M)'}), 400
        cmd = f'lvcreate -L {_q(size)} -n {_q(lv_name)} {_q(vg_name)}'

    r = host_run(cmd, timeout=30)
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to create LV'}), 500

    return jsonify({'ok': True, 'lv_path': f'/dev/{vg_name}/{lv_name}'})


@raid_bp.route('/lvm/lv/<vg>/<lv>', methods=['DELETE'])
@admin_required
def lvm_delete_lv(vg, lv):
    """Delete a logical volume."""
    if not re.match(r'^[a-zA-Z0-9_.-]+$', vg):
        return jsonify({'error': 'Invalid VG name'}), 400
    if not re.match(r'^[a-zA-Z0-9_.-]+$', lv):
        return jsonify({'error': 'Invalid LV name'}), 400

    lv_path = f'/dev/{vg}/{lv}'
    r = host_run(f'lvremove -f {_q(lv_path)}', timeout=30)
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to delete LV'}), 500

    return jsonify({'ok': True})


@raid_bp.route('/lvm/vg/<vg_name>', methods=['DELETE'])
@admin_required
def lvm_delete_vg(vg_name):
    """Delete a volume group (must have no LVs)."""
    if not re.match(r'^[a-zA-Z0-9_.-]+$', vg_name):
        return jsonify({'error': 'Invalid VG name'}), 400

    r = host_run(f'vgremove -f {_q(vg_name)}', timeout=30)
    if r.returncode != 0:
        return jsonify({'error': r.stderr.strip() or 'Failed to delete VG'}), 500

    return jsonify({'ok': True})


@raid_bp.route('/pkg-status')
@admin_required
def pkg_status():
    """Package status for AppStore integration."""
    import shutil
    mdadm = shutil.which('mdadm') is not None
    lvm = shutil.which('lvcreate') is not None
    return jsonify({'installed': mdadm or lvm, 'mdadm': mdadm, 'lvm': lvm,
                    'status': 'active' if (mdadm or lvm) else 'not_installed'})


@raid_bp.route('/install', methods=['POST'])
@admin_required
def install_raid_tools():
    """Install mdadm and LVM tools."""
    from host import host_run
    host_run('apt-get update -qq && apt-get install -y -qq mdadm lvm2 && apt-get clean', timeout=120)
    import shutil
    ok = shutil.which('mdadm') is not None
    return jsonify({'ok': ok, 'message': 'mdadm + lvm2 installed' if ok else 'Install failed'})


@raid_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall_raid_tools():
    """Uninstall mdadm and LVM tools."""
    from host import host_run
    host_run('apt-get remove -y mdadm lvm2 2>/dev/null || true', timeout=60)
    return jsonify({'ok': True})
