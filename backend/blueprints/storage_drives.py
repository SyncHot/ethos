"""EthOS Storage — Drive Management Routes

Routes:
  GET  /api/storage/keepalive
  POST /api/storage/keepalive
  GET  /api/storage/drives
  POST /api/storage/mount
  POST /api/storage/auto-mount
  POST /api/storage/unmount
  POST /api/storage/eject
  GET  /api/storage/smart
"""
import json
import os
import re
import time
import sys
from flask import jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import require_tools
from blueprints.admin_required import admin_required
from blueprints.storage import (
    storage_bp,
    host_run, Q,
    _validate_drive_name, _validate_disk_name, _sanitize_mount_path,
    _keepalive_drives, _save_keepalive, _disable_usb_autosuspend,
    _drives_cache, _DRIVES_CACHE_TTL, _invalidate_drives_cache,
)

# _drives_cache, _DRIVES_CACHE_TTL, _invalidate_drives_cache are defined in storage.py


# ---------------------------------------------------------------------------
# API – Keep-alive
# ---------------------------------------------------------------------------

@storage_bp.route('/keepalive')
def list_keepalive():
    """Return the set of drives with keep-alive enabled."""
    return jsonify({'drives': _keepalive_drives})


@storage_bp.route('/keepalive', methods=['POST'])
def toggle_keepalive():
    """Toggle keep-alive for a drive.  Body: {drive, mountpoint?, fstype?, label?, enable}."""
    data = request.json or {}
    dev_name = data.get('drive', '').strip()
    enable = data.get('enable', True)

    if not dev_name or not _validate_drive_name(dev_name):
        return jsonify({'error': 'Invalid device name'}), 400

    if enable:
        mp = data.get('mountpoint', '').strip()
        if not mp:
            return jsonify({'error': 'Mountpoint required'}), 400
        _keepalive_drives[dev_name] = {
            'mountpoint': mp,
            'fstype': data.get('fstype', 'auto'),
            'label': data.get('label', dev_name),
            'disk': data.get('disk', ''),
        }
        _save_keepalive()
        # Immediately disable USB autosuspend
        _disable_usb_autosuspend(dev_name)
        print(f'[keepalive] Enabled for /dev/{dev_name} → {mp}')
        return jsonify({'success': True, 'enabled': True, 'drive': dev_name})
    else:
        _keepalive_drives.pop(dev_name, None)
        _save_keepalive()
        print(f'[keepalive] Disabled for /dev/{dev_name}')
        return jsonify({'success': True, 'enabled': False, 'drive': dev_name})


@storage_bp.route('/drives')
def list_drives():
    """List block devices with useful info (including main system disk)."""
    now = time.time()
    if _drives_cache['data'] is not None and now - _drives_cache['ts'] < _DRIVES_CACHE_TTL:
        return jsonify(_drives_cache['data'])
    r = host_run(
        "lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL,MODEL,TRAN,UUID,HOTPLUG"
    )
    if r.returncode != 0:
        return jsonify({"error": r.stderr.strip()}), 500

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return jsonify({"error": "Failed to parse lsblk output"}), 500



    # Collect SMART temperature (quick check, cached per-disk)
    smart_temps = {}
    smart_health = {}
    smart_r = host_run(
        "for d in /dev/sd? /dev/nvme?n?; do "
        "[ -b \"$d\" ] && smartctl -A -H \"$d\" 2>/dev/null | "
        "awk -v dev=\"$d\" '"
        "/Temperature_Celsius|Airflow_Temperature/{print dev\"\ttemp\t\"$10} "
        "/^Temperature:/{for(i=1;i<=NF;i++){if($i+0==$i && $i>0){print dev\"\ttemp\t\"$i; break}}} "
        "/SMART overall-health/{print dev\"\thealth\t\"$NF} "
        "'; done",
        timeout=10
    )
    if smart_r.returncode == 0:
        for line in smart_r.stdout.strip().splitlines():
            cols = line.split('\t')
            if len(cols) >= 3:
                dname = cols[0].replace('/dev/', '')
                if cols[1] == 'temp':
                    try:
                        smart_temps[dname] = int(cols[2])
                    except ValueError:
                        pass
                elif cols[1] == 'health':
                    smart_health[dname] = cols[2]

    # Collect df info for disk usage
    df_map = {}
    dfr = host_run("df -B1 --output=source,size,used,avail,pcent,target 2>/dev/null")
    if dfr.returncode == 0:
        for line in dfr.stdout.strip().split('\n')[1:]:
            parts = line.split()
            if len(parts) >= 6:
                mp = parts[-1]
                try:
                    df_map[mp] = {
                        'total': int(parts[1]),
                        'used': int(parts[2]),
                        'free': int(parts[3]),
                        'percent': float(parts[4].replace('%', '')),
                    }
                except (ValueError, IndexError):
                    pass

    drives = []
    _SKIP = {'loop', 'rom'}

    def _collect(dev, parent_info=None):
        dtype = dev.get('type', '')
        name = dev.get('name', '')
        if dtype in _SKIP or name.startswith(('loop', 'nbd', 'zram')):
            return

        children = dev.get('children', [])
        # Recurse into children (partitions, LVM, etc.)
        for child in children:
            _collect(child, parent_info=parent_info or dev)

        # Only include partitions, LVMs, and whole-disk with fs
        if dtype not in ('part', 'lvm', 'disk'):
            return
        # For parent disks with children: include as a summary entry
        if dtype == 'disk' and children:
            child_names = [c.get('name', '') for c in children if c.get('type') in ('part', 'lvm')]
            p = parent_info or dev
            tran = p.get('tran') or dev.get('tran') or ''
            hotplug = p.get('hotplug') or dev.get('hotplug')
            is_usb = tran == 'usb' or hotplug == '1' or hotplug is True
            model = p.get('model') or dev.get('model') or ''
            mp = dev.get('mountpoint')
            category = 'usb' if is_usb else ('system' if mp == '/' or (mp and mp.startswith('/boot')) else 'storage')
            drives.append({
                'name': name,
                'size': dev.get('size'),
                'type': 'disk',
                'fstype': dev.get('fstype'),
                'mountpoint': mp,
                'label': dev.get('label'),
                'model': model.strip() if model else None,
                'uuid': dev.get('uuid'),
                'parent': None,
                'tran': tran,
                'category': category,
                'usage': None,
                'smart_temp': smart_temps.get(name),
                'smart_health': smart_health.get(name),
                'children': child_names,
                'children_count': len(child_names),
            })
            return

        fstype = dev.get('fstype')
        mountpoint = dev.get('mountpoint')

        # Determine transport / category
        p = parent_info or dev
        tran = p.get('tran') or dev.get('tran') or ''
        hotplug = p.get('hotplug') or dev.get('hotplug')
        is_usb = tran == 'usb' or hotplug == '1' or hotplug is True
        model = p.get('model') or dev.get('model') or ''

        # Determine category
        if is_usb:
            category = 'usb'
        elif tran in ('nvme', 'sata', 'ata', 'scsi', '') and dtype == 'lvm':
            category = 'system'
        elif mountpoint == '/':
            category = 'system'
        elif mountpoint and mountpoint.startswith('/boot'):
            category = 'system'
        else:
            category = 'storage'

        usage = df_map.get(mountpoint) if mountpoint else None

        dev_uuid = dev.get('uuid')

        # SMART temp from parent disk
        parent_name = (parent_info or {}).get('name', '')
        temp = smart_temps.get(parent_name) or smart_temps.get(name)
        health = smart_health.get(parent_name) or smart_health.get(name)

        drives.append({
            'name': name,
            'size': dev.get('size'),
            'type': dtype,
            'fstype': fstype,
            'mountpoint': mountpoint,
            'label': dev.get('label'),
            'model': model.strip() if model else None,
            'uuid': dev_uuid,
            'parent': parent_name or None,
            'tran': tran,
            'category': category,
            'usage': usage,
            'smart_temp': temp,
            'smart_health': health,
        })

    for dev in data.get('blockdevices', []):
        _collect(dev)

    result = {'drives': drives}
    _drives_cache['data'] = result
    _drives_cache['ts'] = time.time()
    return jsonify(result)


# USB auto-mount is handled by devmon; fstab entries are reserved for network mounts only


@storage_bp.route('/mount', methods=['POST'])
def mount_drive():
    """Mount a drive to a specified path."""
    data = request.json or {}
    drive_name = data.get("drive", "").strip()
    mount_path = data.get("path", "").strip()

    if not drive_name or not mount_path:
        return jsonify({"error": "drive and path are required"}), 400

    # Validate drive name to prevent command injection
    if not _validate_drive_name(drive_name):
        return jsonify({"error": "Invalid device name"}), 400
    if not mount_path.startswith('/'):
        return jsonify({"error": "path must be absolute"}), 400
    if '..' in mount_path:
        return jsonify({"error": "Path cannot contain '..'"}), 400

    mount_path = _sanitize_mount_path(mount_path)

    r = host_run(f"lsblk -no FSTYPE /dev/{drive_name}")
    fstype = r.stdout.strip() or "auto"

    if fstype in ("ntfs", "ntfs3"):
        fstype = "ntfs-3g"

    uid_r = host_run("id -u")
    gid_r = host_run("id -g")
    uid = uid_r.stdout.strip() or "1000"
    gid = gid_r.stdout.strip() or "1000"

    if fstype == "ntfs-3g":
        mount_opts = f"rw,uid={uid},gid={gid},umask=000,nofail,remove_hiberfile"
    elif fstype in ("vfat", "exfat"):
        mount_opts = f"rw,uid={uid},gid={gid},umask=000,nofail"
    elif fstype in ("ext4", "ext3", "ext2"):
        mount_opts = "defaults,nofail,noatime,commit=60"
    elif fstype in ("xfs", "btrfs"):
        mount_opts = "defaults,nofail,noatime"
    else:
        mount_opts = "rw,nofail"

    uuid_r = host_run(f"lsblk -no UUID /dev/{drive_name}")
    uuid = uuid_r.stdout.strip()

    host_run(f"umount /dev/{drive_name} 2>/dev/null || true")
    host_run(f"mkdir -p {Q(mount_path)}")

    if fstype == "ntfs-3g":
        r = host_run(f"mount -t ntfs-3g -o {mount_opts} /dev/{drive_name} {Q(mount_path)}")
    else:
        r = host_run(f"mount {Q(mount_path)}")
    if r.returncode != 0:
        r = host_run(f"mount -t {fstype} -o {mount_opts} /dev/{drive_name} {Q(mount_path)}")
        if r.returncode != 0:
            return jsonify({"error": f"Mount failed: {r.stderr.strip()}"}), 500

    if fstype in ("ext4", "ext3", "ext2", "xfs", "btrfs"):
        host_run(f"chmod 0777 {Q(mount_path)}")
        host_run(f"chown {uid}:{gid} {Q(mount_path)}")

    auto_mount = data.get("auto_mount", False)
    # USB auto-mount at boot is handled by devmon; fstab entries are reserved for network mounts

    _invalidate_drives_cache()
    return jsonify({
        "success": True,
        "device": f"/dev/{drive_name}",
        "mountpoint": mount_path,
        "fstype": fstype,
        "auto_mount": bool(auto_mount and uuid),
    })


def _fstab_add(uuid, mount_path, fstype, opts):
    """Add a UUID-based fstab entry with nofail. Idempotent."""
    fstab = host_run("cat /etc/fstab").stdout or ""
    marker = f"# ethos-auto:{mount_path}"
    if marker in fstab or f"UUID={uuid}" in fstab:
        return
    line = f"UUID={uuid}  {mount_path}  {fstype}  {opts}  0  2  {marker}\n"
    host_run(f"echo {Q(line)} >> /etc/fstab")


def _fstab_remove(mount_path):
    """Remove ethos-auto fstab entries for a mount point."""
    escaped = mount_path.replace('/', '\\/')
    marker = f"# ethos-auto:{escaped}"
    host_run(f"sed -i '/{marker}/d' /etc/fstab 2>/dev/null || true")


@storage_bp.route('/auto-mount', methods=['POST'])
def toggle_auto_mount():
    """Enable/disable auto-mount on boot for a drive."""
    data = request.json or {}
    mount_path = data.get("path", "").strip()
    enable = data.get("enable", True)
    if not mount_path:
        return jsonify({"error": "path required"}), 400

    # USB auto-mount at boot is handled by devmon; fstab is reserved for network mounts
    return jsonify({"ok": True, "auto_mount": enable, "note": "USB auto-mount is managed by devmon"})


@storage_bp.route('/unmount', methods=['POST'])
def unmount_drive():
    """Unmount a drive."""
    data = request.json or {}
    mount_path = data.get("path")
    if not mount_path:
        return jsonify({"error": "path is required"}), 400

    check = host_run(f"findmnt -n -o SOURCE {Q(mount_path)}")
    is_mounted = check.returncode == 0 and check.stdout.strip() != ""

    if is_mounted:
        loop_dev = None
        src = check.stdout.strip()
        if src.startswith("/dev/loop"):
            loop_dev = src

        r = host_run(f"umount {Q(mount_path)}")
        if r.returncode != 0:
            r = host_run(f"umount -l {Q(mount_path)}")
            if r.returncode != 0:
                return jsonify({"error": f"Unmount failed: {r.stderr.strip()}"}), 500

        if loop_dev:
            host_run(f"losetup -d {loop_dev} 2>/dev/null || true")

    return jsonify({"success": True, "mountpoint": mount_path})


@storage_bp.route('/eject', methods=['POST'])
def eject_drive():
    """Safely eject a USB drive: unmount all partitions, then power-off the device."""
    data = request.json or {}
    disk_name = data.get("disk", "").strip()

    if not disk_name:
        return jsonify({"error": "disk is required"}), 400
    if not _validate_disk_name(disk_name):
        return jsonify({"error": "Invalid disk name"}), 400

    dev_path = f"/dev/{disk_name}"

    # Verify it's a USB device
    r = host_run(f"lsblk -ndo TRAN,HOTPLUG {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": f"Device {dev_path} does not exist"}), 404
    parts = r.stdout.strip().split()
    tran = parts[0] if parts else ''
    hotplug = parts[1] if len(parts) > 1 else '0'
    if tran != 'usb' and hotplug != '1':
        return jsonify({"error": "Not a USB device"}), 400

    # Unmount all partitions
    r = host_run(f"lsblk -nlo NAME,MOUNTPOINT {dev_path} 2>/dev/null")
    unmounted = []
    if r.returncode == 0:
        for line in r.stdout.strip().splitlines():
            cols = line.split(None, 1)
            if len(cols) >= 2 and cols[1].strip():
                mp = cols[1].strip()
                ur = host_run(f"umount {Q(mp)}")
                if ur.returncode != 0:
                    host_run(f"umount -l {Q(mp)}")
                unmounted.append(mp)

    # Sync filesystem
    host_run("sync")

    # Power off the USB device via udisksctl or kernel sysfs
    r = host_run(f"udisksctl power-off -b {dev_path} 2>/dev/null")
    if r.returncode != 0:
        # Fallback: remove device via sysfs
        host_run(f"echo 1 > /sys/block/{disk_name}/device/delete 2>/dev/null")

    _invalidate_drives_cache()
    return jsonify({
        "success": True,
        "disk": disk_name,
        "unmounted": unmounted,
        "message": "Disk safely ejected",
    })


@storage_bp.route('/smart')
def smart_info():
    """Get SMART info for a specific disk."""
    err = require_tools('smartctl')
    if err:
        return err
    disk_name = request.args.get('disk', '').strip()
    if not disk_name or not _validate_disk_name(disk_name):
        return jsonify({"error": "Invalid disk name"}), 400

    dev_path = f"/dev/{disk_name}"
    r = host_run(f"smartctl -A -H -i {dev_path} 2>/dev/null", timeout=15)

    info = {'disk': disk_name, 'available': False}

    if r.returncode in (0, 4):  # 4 = some SMART data available but device has errors
        info['available'] = True
        info['raw'] = r.stdout

        # Parse health
        for line in r.stdout.splitlines():
            if 'SMART overall-health' in line:
                info['health'] = line.split(':')[-1].strip()
            elif 'Temperature_Celsius' in line or 'Airflow_Temperature' in line:
                parts = line.split()
                if len(parts) >= 10:
                    try:
                        info['temperature'] = int(parts[9])
                    except ValueError:
                        pass
            elif line.startswith('Temperature:') and 'temperature' not in info:
                # NVMe format: "Temperature:                        43 Celsius"
                parts = line.split()
                for p in parts[1:]:
                    try:
                        info['temperature'] = int(p)
                        break
                    except ValueError:
                        continue
            elif 'Power_On_Hours' in line:
                parts = line.split()
                if len(parts) >= 10:
                    try:
                        info['power_on_hours'] = int(parts[9])
                    except ValueError:
                        pass
            elif 'Reallocated_Sector_Ct' in line:
                parts = line.split()
                if len(parts) >= 10:
                    try:
                        info['reallocated_sectors'] = int(parts[9])
                    except ValueError:
                        pass
            elif 'Model Family' in line or 'Device Model' in line or 'Model Number' in line:
                info['model'] = line.split(':')[-1].strip()
            elif 'Serial Number' in line:
                info['serial'] = line.split(':')[-1].strip()
            elif 'Firmware Version' in line:
                info['firmware'] = line.split(':')[-1].strip()

    return jsonify(info)


