"""
EthOS — Storage Manager Blueprint
Drive mounting/unmounting, file sharing (Samba, NFS, DLNA, WebDAV, SFTP),
USB hotplug monitoring.
"""

import json
import os
import re
import subprocess
import base64
import time
import threading
import sys
from flask import Blueprint, jsonify, request, Response, stream_with_context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_base, host_run_stream as _host_run_stream_base, data_path, q as _q_imported, apt_install as _apt_install, claim_dep, release_dep
from utils import fmt_bytes, require_tools, check_tool
from blueprints.admin_required import admin_required

storage_bp = Blueprint('storage', __name__, url_prefix='/api/storage')

# ---------------------------------------------------------------------------
# USB hotplug monitor
# ---------------------------------------------------------------------------

_socketio = None
_usb_events = []          # recent USB plug/unplug events for notifications
_USB_EVENT_TTL = 300       # keep events for 5 minutes

# ---------------------------------------------------------------------------
# Keep-alive: auto-remount & USB anti-suspend
# ---------------------------------------------------------------------------

_KEEPALIVE_FILE = data_path('keepalive.json')
_keepalive_drives = {}     # dev_name → {mountpoint, fstype, label, disk}


def _load_keepalive():
    """Load keep-alive config from disk."""
    global _keepalive_drives
    try:
        with open(_KEEPALIVE_FILE, 'r') as f:
            _keepalive_drives = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _keepalive_drives = {}


def _save_keepalive():
    """Persist keep-alive config to disk."""
    try:
        os.makedirs(os.path.dirname(_KEEPALIVE_FILE), exist_ok=True)
        with open(_KEEPALIVE_FILE, 'w') as f:
            json.dump(_keepalive_drives, f, indent=2)
    except Exception as e:
        print(f'[keepalive] save error: {e}')


def _disable_usb_autosuspend(dev_name):
    """Disable USB autosuspend for the physical disk so the kernel won't power it down."""
    # Find the parent disk (e.g. sda for sda1)
    r = host_run(f"lsblk -ndo PKNAME /dev/{dev_name} 2>/dev/null")
    disk = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else dev_name
    # Walk sysfs to find the usb device node
    cmds = (
        # Method 1: set autosuspend via the block device's usb ancestor
        f"devpath=$(readlink -f /sys/block/{disk}/device); "
        f"while [ \"$devpath\" != \"/sys\" ] && [ -n \"$devpath\" ]; do "
        f"  if [ -f \"$devpath/power/autosuspend\" ]; then "
        f"    echo -1 > \"$devpath/power/autosuspend\" 2>/dev/null; "
        f"    echo -1 > \"$devpath/power/autosuspend_delay_ms\" 2>/dev/null; "
        f"    echo on > \"$devpath/power/control\" 2>/dev/null; "
        f"    echo on > \"$devpath/power/level\" 2>/dev/null; "
        f"    break; "
        f"  fi; "
        f"  devpath=$(dirname \"$devpath\"); "
        f"done"
    )
    host_run(cmds, timeout=10)


def keepalive_loop(sio):
    """Background greenlet: periodically check pinned drives and re-mount if needed."""
    import gevent
    _load_keepalive()
    print(f'[keepalive] Started — watching {len(_keepalive_drives)} drive(s)')

    while True:
        gevent.sleep(30)
        if not _keepalive_drives:
            continue
        try:
            for dev_name, info in list(_keepalive_drives.items()):
                mp = info.get('mountpoint', '')
                if not mp:
                    continue

                # Check if device exists
                dev_check = host_run(f"test -b /dev/{dev_name} && echo ok", timeout=5)
                if 'ok' not in (dev_check.stdout or ''):
                    # Device not present (physically disconnected) — skip
                    continue

                # Check if already mounted at correct path
                mnt_check = host_run(
                    f"findmnt -n -o TARGET /dev/{dev_name} 2>/dev/null", timeout=5
                )
                current_mp = mnt_check.stdout.strip() if mnt_check.returncode == 0 else ''

                if current_mp == mp:
                    # Mounted correctly — just keep USB awake
                    _disable_usb_autosuspend(dev_name)
                    continue

                # Drive present but not mounted at expected path → re-mount
                ok = _remount_keepalive_drive(dev_name, info)
                if ok and sio:
                    sio.emit('drive_remounted', {
                        'dev': dev_name, 'mountpoint': mp,
                        'label': info.get('label', dev_name),
                    })
        except Exception as e:
            print(f'[keepalive] error: {e}')


def _remount_keepalive_drive(dev_name, info):
    """Try to remount a keepalive drive.  Returns True on success."""
    mp = info.get('mountpoint', '')
    if not mp:
        return False

    # Check if device exists
    dev_check = host_run(f"test -b /dev/{dev_name} && echo ok", timeout=5)
    if 'ok' not in (dev_check.stdout or ''):
        return False

    # Already mounted at correct path?
    mnt_check = host_run(f"findmnt -n -o TARGET /dev/{dev_name} 2>/dev/null", timeout=5)
    current_mp = mnt_check.stdout.strip() if mnt_check.returncode == 0 else ''
    if current_mp == mp:
        _disable_usb_autosuspend(dev_name)
        return True

    print(f'[keepalive] Re-mounting /dev/{dev_name} → {mp}')
    fstype = info.get('fstype', 'auto')
    if fstype in ('ntfs', 'ntfs3'):
        fstype = 'ntfs-3g'

    uid_r = host_run('id -u', timeout=5)
    gid_r = host_run('id -g', timeout=5)
    uid = uid_r.stdout.strip() or '1000'
    gid = gid_r.stdout.strip() or '1000'

    if fstype == 'ntfs-3g':
        opts = f'rw,uid={uid},gid={gid},umask=000,nofail,remove_hiberfile'
    elif fstype in ('vfat', 'exfat'):
        opts = f'rw,uid={uid},gid={gid},umask=000,nofail'
    elif fstype in ('ext4', 'ext3', 'ext2', 'xfs', 'btrfs'):
        opts = 'defaults,nofail'
    else:
        opts = 'rw,nofail'

    host_run(f'umount /dev/{dev_name} 2>/dev/null || true', timeout=10)
    host_run(f'mkdir -p {Q(mp)}', timeout=5)

    if fstype == 'ntfs-3g':
        r = host_run(f'mount -t ntfs-3g -o {opts} /dev/{dev_name} {Q(mp)}', timeout=15)
    else:
        r = host_run(f'mount -t {fstype} -o {opts} /dev/{dev_name} {Q(mp)}', timeout=15)

    if r.returncode == 0:
        print(f'[keepalive] ✓ Remounted /dev/{dev_name} → {mp}')
        _disable_usb_autosuspend(dev_name)
        return True
    else:
        print(f'[keepalive] ✗ Failed to remount /dev/{dev_name}: {r.stderr.strip()}')
        return False


def try_wake_path(host_path):
    """If *host_path* lives under a keepalive drive that is currently
    unmounted, try to remount it.  Called from the file-manager listing.
    Also sends a lightweight read to wake a sleeping USB disk.
    *host_path* is the real host path (e.g. /media/devmon/4TB-external/…).
    Returns True if a remount was performed (or not needed), False on failure."""
    if not _keepalive_drives:
        return False

    for dev_name, info in _keepalive_drives.items():
        mp = info.get('mountpoint', '')
        if not mp:
            continue
        # Check if requested path is under this mountpoint
        if host_path == mp or host_path.startswith(mp + '/'):
            # First: poke the disk with a lightweight stat to wake from standby
            host_run(f"stat {Q(mp)} >/dev/null 2>&1", timeout=5)
            # Then check if still actually mounted
            mnt_check = host_run(f"findmnt -n -o TARGET {Q(mp)} 2>/dev/null", timeout=5)
            if mnt_check.returncode == 0 and mnt_check.stdout.strip() == mp:
                _disable_usb_autosuspend(dev_name)
                return True
            # Not mounted → remount
            return _remount_keepalive_drive(dev_name, info)
    return False


def init_storage(sio):
    """Initialize the storage blueprint with Socket.IO for USB monitoring."""
    global _socketio
    _socketio = sio
    _load_keepalive()


def get_usb_notifications():
    """Return active USB hotplug notification items."""
    now = time.time()
    active = [e for e in _usb_events if now - e['time'] < _USB_EVENT_TTL]
    _usb_events[:] = active
    return list(active)


def _usb_label_for_device(dev_name):
    """Get filesystem label for a device like 'sdc2' via lsblk on the host."""
    try:
        r = host_run(f"lsblk -no LABEL,SIZE,FSTYPE /dev/{dev_name} 2>/dev/null", timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split()
            label = parts[0] if parts else dev_name
            size = parts[1] if len(parts) > 1 else ''
            fstype = parts[2] if len(parts) > 2 else ''
            return label or dev_name, size, fstype
    except Exception:
        pass
    return dev_name, '', ''


def usb_monitor_loop(sio):
    """Background greenlet: watch /proc/partitions for USB block device changes."""
    import gevent
    known = set()
    # Track dev→mountpoint so we can clean up stale dirs on removal
    _dev_mounts = {}   # dev_name → mountpoint path

    def _current_usb_parts():
        """Return set of (dev_name,) for USB partitions from lsblk."""
        parts = set()
        try:
            r = host_run(
                "lsblk -Jno NAME,TRAN,TYPE,RM,MOUNTPOINT 2>/dev/null",
                timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                for dev in data.get('blockdevices', []):
                    is_usb = dev.get('tran') == 'usb' or dev.get('rm')
                    if is_usb:
                        children = dev.get('children', [])
                        if children:
                            for ch in children:
                                if ch.get('type') == 'part':
                                    parts.add(ch['name'])
                                    mp = ch.get('mountpoint')
                                    if mp:
                                        _dev_mounts[ch['name']] = mp
                        else:
                            if dev.get('type') in ('disk', 'part'):
                                parts.add(dev['name'])
                                mp = dev.get('mountpoint')
                                if mp:
                                    _dev_mounts[dev['name']] = mp
        except Exception:
            pass
        return parts

    def _cleanup_stale_mountpoint(dev_name):
        """Force-unmount stale mountpoint and remove the empty directory."""
        mp = _dev_mounts.pop(dev_name, None)
        if not mp:
            return
        # Lazy unmount in case kernel still holds a ref
        host_run(f"umount -l {_shell_quote(mp)} 2>/dev/null", timeout=5)
        # Remove the empty directory so devmon doesn't create -2 suffixes
        host_run(f"rmdir {_shell_quote(mp)} 2>/dev/null", timeout=5)

    def _cleanup_all_stale_devmon():
        """Remove any /media/devmon/* dirs that are not active mountpoints."""
        try:
            r = host_run(
                "for d in /media/devmon/*/; do "
                "  mountpoint -q \"$d\" 2>/dev/null || "
                "  { rmdir \"$d\" 2>/dev/null && echo \"removed:$d\"; }; "
                "done",
                timeout=10
            )
        except Exception:
            pass

    # Initial snapshot
    known = _current_usb_parts()
    # Clean any stale dirs from before we started
    _cleanup_all_stale_devmon()

    while True:
        gevent.sleep(3)
        try:
            current = _current_usb_parts()
            added = current - known
            removed = known - current

            for dev_name in added:
                label, size, fstype = _usb_label_for_device(dev_name)
                size_str = f" ({size})" if size else ""
                evt = {
                    'type': 'success',
                    'title': 'USB connected',
                    'message': f'{label}{size_str} — /dev/{dev_name}',
                    'time': time.time(),
                    'action': {'app': 'storage'},
                    'dev': dev_name,
                    'label': label,
                }
                _usb_events.append(evt)
                sio.emit('usb_connected', {
                    'dev': dev_name, 'label': label,
                    'size': size, 'fstype': fstype,
                })

            for dev_name in removed:
                label = _dev_mounts.get(dev_name, '').split('/')[-1] or dev_name
                # Clean up stale mountpoint BEFORE notifying
                _cleanup_stale_mountpoint(dev_name)
                evt = {
                    'type': 'warning',
                    'title': 'USB disconnected',
                    'message': f'{label} (/dev/{dev_name})',
                    'time': time.time(),
                    'dev': dev_name,
                }
                _usb_events.append(evt)
                sio.emit('usb_disconnected', {'dev': dev_name, 'label': label})

            # Periodic cleanup of any stale dirs (safety net)
            if removed:
                gevent.sleep(2)
                _cleanup_all_stale_devmon()

            known = current
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers – execute on the HOST via nsenter
# ---------------------------------------------------------------------------

def host_run(cmd, timeout=30):
    return _host_run_base(cmd, timeout=timeout)


def host_run_stream(cmd):
    return _host_run_stream_base(cmd)


def _shell_quote(s):
    return _q_imported(s)

Q = _shell_quote


def _host_write_json(filepath, data):
    """Safely write a JSON object to a file on the host.
    Uses base64 encoding to avoid any shell/quote injection."""
    raw = json.dumps(data).encode('utf-8')
    b64 = base64.b64encode(raw).decode('ascii')
    host_run(f"echo '{b64}' | base64 -d > {_shell_quote(filepath)}")


def _host_write_script(filepath, script_text):
    """Safely write a Python script to the host using heredoc with single-quoted delimiter."""
    host_run(f"cat > {_shell_quote(filepath)} << 'PYSCRIPT_EOF_MARKER'\n" + script_text + "\nPYSCRIPT_EOF_MARKER")


def _host_write_file(filepath, content):
    """Safely write text content to a file on the host using base64."""
    raw = content.encode('utf-8')
    b64 = base64.b64encode(raw).decode('ascii')
    host_run(f"echo '{b64}' | base64 -d > {_shell_quote(filepath)}")


def _sanitize_mount_path(path):
    parts = path.rsplit('/', 1)
    if len(parts) == 2:
        dirname, basename = parts
        basename = re.sub(r'[^\w.\-]', '_', basename)
        basename = re.sub(r'_+', '_', basename).strip('_')
        return f"{dirname}/{basename}"
    return path


def _validate_drive_name(name):
    """Validate drive name to prevent command injection.
    Accepts: sda, sda1, nvme0n1, nvme0n1p1, mmcblk0p1, etc."""
    if not name:
        return False
    return bool(re.match(r'^[a-zA-Z0-9]+$', name))


def _validate_disk_name(name):
    """Validate base disk name (no partition number).
    Accepts: sda, sdb, nvme0n1, mmcblk0, etc."""
    if not name:
        return False
    return bool(re.match(r'^[a-zA-Z]+$', name) or re.match(r'^nvme\d+n\d+$', name) or re.match(r'^mmcblk\d+$', name))


def _is_system_disk(disk_name):
    """Robust check whether a disk is used by the system.
    Checks root, /boot, /home, swap, and LVM PVs."""
    # 1. Check root device
    r = host_run("findmnt -n -o SOURCE / 2>/dev/null")
    root_dev = r.stdout.strip() if r.returncode == 0 else ""

    # For LVM: root_dev might be /dev/mapper/vg-root or /dev/dm-0
    # Resolve to physical disk via lsblk
    if root_dev:
        # Direct match: /dev/sda1 on root, checking sda
        if f"/dev/{disk_name}" in root_dev or root_dev.startswith(f"/dev/{disk_name}"):
            return True
        # LVM/dm resolution: find which physical disk backs root
        pkdev_r = host_run(f"lsblk -nso NAME,TYPE /dev/{disk_name} 2>/dev/null")
        if pkdev_r.returncode == 0:
            for line in pkdev_r.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 1:
                    child_dev = f"/dev/{parts[0]}"
                    if child_dev in root_dev or root_dev.startswith(child_dev):
                        return True

    # 2. Check critical mount points
    for mp in ('/', '/boot', '/boot/efi', '/home', '/var', '/usr'):
        r2 = host_run(f"findmnt -n -o SOURCE {Q(mp)} 2>/dev/null")
        if r2.returncode == 0 and r2.stdout.strip():
            src = r2.stdout.strip()
            if f"/dev/{disk_name}" in src or src.startswith(f"/dev/{disk_name}"):
                return True

    # 3. Check swap
    r3 = host_run("cat /proc/swaps 2>/dev/null")
    if r3.returncode == 0:
        for line in r3.stdout.strip().splitlines()[1:]:
            if f"/dev/{disk_name}" in line or f"/{disk_name}" in line:
                return True

    return False


# fstab management removed — USB drives are auto-mounted by devmon


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


# ---------------------------------------------------------------------------
# API – Drives
# ---------------------------------------------------------------------------

_drives_cache = {'data': None, 'ts': 0}
_DRIVES_CACHE_TTL = 10  # seconds

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
        if dtype in _SKIP or name.startswith('loop'):
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


# /mounts endpoint removed — fstab no longer managed for USB drives


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
    if auto_mount and uuid:
        _fstab_add(uuid, mount_path, fstype, mount_opts)

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

    if enable:
        r = host_run(f"findmnt -n -o SOURCE {Q(mount_path)} 2>/dev/null")
        device = r.stdout.strip()
        if not device:
            return jsonify({"error": "Not mounted"}), 400
        uuid_r = host_run(f"lsblk -no UUID {Q(device)}")
        uuid = uuid_r.stdout.strip()
        fs_r = host_run(f"lsblk -no FSTYPE {Q(device)}")
        fstype = fs_r.stdout.strip() or "auto"
        if not uuid:
            return jsonify({"error": "No UUID found"}), 400
        opts = "defaults,nofail,noatime"
        if fstype in ("ntfs", "ntfs3", "ntfs-3g"):
            opts = "rw,uid=1000,gid=1000,umask=000,nofail"
        _fstab_add(uuid, mount_path, fstype, opts)
    else:
        _fstab_remove(mount_path)

    return jsonify({"ok": True, "auto_mount": enable})


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


# ---------------------------------------------------------------------------
# API – Samba
# ---------------------------------------------------------------------------

@storage_bp.route('/samba/status')
def samba_status():
    r = host_run("command -v smbd")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active smbd 2>/dev/null || systemctl is-active smb 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running})


@storage_bp.route('/samba/install', methods=['POST'])
def samba_install():
    r = host_run("command -v smbd")
    if r.returncode == 0:
        return jsonify({"status": "ok", "installed": True}), 200

    def generate():
        install_script = """
export DEBIAN_FRONTEND=noninteractive
echo '::STEP::Repairing package manager...'
dpkg --configure -a 2>/dev/null || true
echo '::STEP::Updating package lists...'
apt-get update -y 2>&1
echo '::STEP::Installing Samba...'
apt-get install -y samba samba-common-bin smbclient 2>&1
echo '::STEP::Configuring Samba...'
if [ ! -f /etc/samba/smb.conf ] || ! grep -q 'map to guest' /etc/samba/smb.conf 2>/dev/null; then
  cat > /etc/samba/smb.conf << 'SMBEOF'
[global]
    workgroup = WORKGROUP
    server string = EthOS NAS
    security = user
    map to guest = Bad User
    guest account = nobody
    server min protocol = SMB2
    log file = /var/log/samba/log.%m
    max log size = 1000
    logging = file
    dns proxy = no
    unix extensions = yes
    wide links = no
    follow symlinks = yes

    # Performance tuning
    socket options = TCP_NODELAY IPTOS_LOWDELAY SO_RCVBUF=131072 SO_SNDBUF=131072
    read raw = yes
    write raw = yes
    max xmit = 65535
    dead time = 15
    getwd cache = yes
    use sendfile = yes
    aio read size = 16384
    aio write size = 16384
SMBEOF
fi
echo '::STEP::Enabling Samba services...'
systemctl unmask smbd nmbd 2>/dev/null || true
systemctl enable smbd nmbd 2>/dev/null || true
systemctl start smbd nmbd 2>/dev/null || true
echo '::STEP::Samba installation complete!'
echo '::DONE::'
"""
        for line in host_run_stream(install_script):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = line.split(":")[1]
                yield f"data: {json.dumps({'type': 'exit', 'code': int(code)})}\n\n"
            elif line.startswith("::STEP::"):
                yield f"data: {json.dumps({'type': 'step', 'message': line[8:]})}\n\n"
            elif line.startswith("::DONE::"):
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@storage_bp.route('/samba/shares')
def samba_shares():
    r = host_run("cat /etc/samba/smb.conf 2>/dev/null || echo ''")
    shares = []
    current_share = None

    for line in r.stdout.splitlines():
        line = line.strip()
        match = re.match(r"^\[(.+)\]$", line)
        if match:
            name = match.group(1)
            if name.lower() != "global":
                current_share = {"name": name, "path": "", "writable": False, "guest_ok": False}
                shares.append(current_share)
            else:
                current_share = None
            continue
        if current_share and "=" in line:
            key, val = line.split("=", 1)
            key = key.strip().lower().replace(" ", "_")
            val = val.strip()
            if key == "path":
                current_share["path"] = val
            elif key in ("writable", "writeable"):
                current_share["writable"] = val.lower() == "yes"
            elif key == "guest_ok":
                current_share["guest_ok"] = val.lower() == "yes"

    return jsonify(shares)


@storage_bp.route('/samba/share', methods=['POST'])
def samba_share_add():
    # Check if Samba is installed first
    r_smb = host_run("command -v smbd")
    if r_smb.returncode != 0:
        return jsonify({"error": "Samba is not installed. Install it first in Disks → Samba."}), 400

    data = request.json or {}
    share_name = data.get("name", "").strip()
    share_path = data.get("path", "").strip()
    guest_ok = data.get("guest_ok", True)

    if not share_name or not share_path:
        return jsonify({"error": "name and path are required"}), 400

    # Validate share name: alphanumeric, hyphens, underscores, spaces only
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9 _\-]{0,63}$', share_name):
        return jsonify({"error": "Share name may only contain letters, numbers, spaces, - and _"}), 400

    # Validate share path: must be absolute, no traversal, no suspicious chars
    if not share_path.startswith('/'):
        return jsonify({"error": "Path must be absolute (start with /)"}), 400
    if '..' in share_path:
        return jsonify({"error": "Path cannot contain '..'"}), 400
    # Block sharing critical system directories
    _BLOCKED_PATHS = ('/', '/etc', '/proc', '/sys', '/dev', '/boot', '/root',
                       '/bin', '/sbin', '/usr', '/lib', '/lib64', '/var')
    norm_share = share_path.rstrip('/')
    if norm_share in _BLOCKED_PATHS or not norm_share:
        return jsonify({"error": f"Cannot share system path: {share_path}"}), 403

    uid_r = host_run("id -un")
    gid_r = host_run("id -gn")
    user = uid_r.stdout.strip() or "nobody"
    group = gid_r.stdout.strip() or "nogroup"

    writable = data.get("writable", True)
    guest_str = "yes" if guest_ok else "no"
    writable_str = "yes" if writable else "no"

    # Use safe temp-file approach to avoid shell/Python injection
    share_conf = {
        "name": share_name,
        "path": share_path,
        "guest_ok": guest_str,
        "writable": writable_str,
        "user": user,
        "group": group,
    }
    script = """import json, re
NL = chr(10)
params = json.loads(open('/tmp/_samba_params.json').read())
name = params['name']
path = params['path']
guest = params['guest_ok']
user = params['user']
group = params['group']
try:
    conf = open('/etc/samba/smb.conf').read()
except FileNotFoundError:
    conf = ''

# Ensure [global] section has required settings
GLOBAL_DEFAULTS = {
    'workgroup': 'WORKGROUP',
    'server string': 'EthOS NAS',
    'security': 'user',
    'map to guest': 'Bad User',
    'guest account': 'nobody',
    'server min protocol': 'SMB2',
    'dns proxy': 'no',
}
if '[global]' not in conf:
    header = '[global]' + NL
    for k, v in GLOBAL_DEFAULTS.items():
        header += f'    {k} = {v}' + NL
    conf = header + NL + conf
else:
    # Inject missing keys into existing [global]
    gstart = conf.index('[global]')
    # Find end of global: next section or end of file
    next_bracket = conf.find(NL + '[', gstart + 8)
    gend = next_bracket + 1 if next_bracket != -1 else len(conf)
    global_block = conf[gstart:gend]
    additions = ''
    for k, v in GLOBAL_DEFAULTS.items():
        if k not in global_block.lower():
            additions += f'    {k} = {v}' + NL
    if additions:
        insert_pos = conf.index(NL, gstart) + 1
        conf = conf[:insert_pos] + additions + conf[insert_pos:]

pattern = r'\\[' + re.escape(name) + r'\\][^\\[]*'
conf = re.sub(pattern, '', conf, flags=re.IGNORECASE)
conf = conf.rstrip() + NL + NL
block = f'[{name}]' + NL
block += f'    path = {path}' + NL
writable = params['writable']
read_only = 'no' if writable == 'yes' else 'yes'
block += '    browseable = yes' + NL
block += f'    writable = {writable}' + NL
block += f'    read only = {read_only}' + NL
block += f'    guest ok = {guest}' + NL
block += f'    public = {guest}' + NL
block += f'    force user = {user}' + NL
block += f'    force group = {group}' + NL
block += '    create mask = 0777' + NL
block += '    directory mask = 0777' + NL
open('/etc/samba/smb.conf', 'w').write(conf + block)

# Ensure share path exists
import os
os.makedirs(path, mode=0o777, exist_ok=True)
"""
    _host_write_json('/tmp/_samba_params.json', share_conf)
    _host_write_script('/tmp/_samba_edit.py', script)
    r = host_run("python3 /tmp/_samba_edit.py")
    host_run("rm -f /tmp/_samba_edit.py /tmp/_samba_params.json 2>/dev/null")

    if r.returncode != 0:
        return jsonify({"error": f"Failed to add share: {r.stderr.strip()}"}), 500

    host_run("systemctl restart smbd nmbd 2>/dev/null || systemctl restart smb nmb 2>/dev/null || true")
    return jsonify({"success": True, "share_name": share_name, "path": share_path})


@storage_bp.route('/samba/share', methods=['DELETE'])
def samba_share_remove():
    data = request.json or {}
    share_name = data.get("name", "").strip()
    if not share_name:
        return jsonify({"error": "name is required"}), 400

    # Validate share name
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9 _\-]{0,63}$', share_name):
        return jsonify({"error": "Invalid share name"}), 400

    script = """import json, re
NL = chr(10)
params = json.loads(open('/tmp/_samba_del_params.json').read())
name = params['name']
try:
    conf = open('/etc/samba/smb.conf').read()
except FileNotFoundError:
    exit(0)
pattern = r'\\[' + re.escape(name) + r'\\][^\\[]*'
conf = re.sub(pattern, '', conf, flags=re.IGNORECASE)
open('/etc/samba/smb.conf', 'w').write(conf.strip() + NL)
"""
    _host_write_json('/tmp/_samba_del_params.json', {"name": share_name})
    _host_write_script('/tmp/_samba_del.py', script)
    host_run("python3 /tmp/_samba_del.py")
    host_run("rm -f /tmp/_samba_del.py /tmp/_samba_del_params.json 2>/dev/null")
    host_run("systemctl restart smbd nmbd 2>/dev/null || systemctl restart smb nmb 2>/dev/null || true")
    return jsonify({"success": True})


@storage_bp.route('/samba/password', methods=['POST'])
def samba_password():
    """Set Samba password for a user (creates the user if needed)."""
    err = require_tools('smbpasswd')
    if err:
        return err
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    # Validate username: alphanumeric + underscores
    if not re.match(r'^[a-zA-Z0-9_]{1,32}$', username):
        return jsonify({"error": "Username may only contain letters, numbers, and _"}), 400

    # Validate password length
    if len(password) < 1 or len(password) > 128:
        return jsonify({"error": "Password must be 1-128 characters"}), 400

    # Ensure user exists on the host
    host_run(f"id {Q(username)} || useradd -M -s /usr/sbin/nologin {Q(username)}")

    # Set samba password safely — pass password via base64-encoded JSON file
    # This avoids any shell injection via password characters
    _host_write_json('/tmp/_smb_pw.json', {'pw': password, 'user': username})
    script = """import json, subprocess, sys
params = json.loads(open('/tmp/_smb_pw.json').read())
pw = params['pw']
user = params['user']
p = subprocess.Popen(['smbpasswd', '-s', '-a', user],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
out, err = p.communicate((pw + chr(10) + pw + chr(10)).encode())
sys.exit(p.returncode)
"""
    _host_write_script('/tmp/_smb_pw_set.py', script)
    r = host_run("python3 /tmp/_smb_pw_set.py")
    host_run("rm -f /tmp/_smb_pw.json /tmp/_smb_pw_set.py 2>/dev/null")

    if r.returncode != 0:
        return jsonify({"error": f"smbpasswd failed: {r.stderr.strip()}"}), 500

    return jsonify({"success": True})


# -- Samba package routes (for App Store install/uninstall) --

@storage_bp.route('/samba/pkg-install', methods=['POST'])
@admin_required
def samba_pkg_install():
    """Install Samba via apt — delegates to the existing /samba/install SSE endpoint logic."""
    r = host_run("command -v smbd")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('smbd', 'sharing-samba')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'Samba installation failed'}), 500
    host_run("systemctl unmask smbd nmbd 2>/dev/null; systemctl enable smbd nmbd 2>/dev/null; systemctl start smbd nmbd 2>/dev/null || true")
    return jsonify({'status': 'ok'})


@storage_bp.route('/samba/pkg-uninstall', methods=['POST'])
@admin_required
def samba_pkg_uninstall():
    """Stop Samba services and disable them. Optionally wipe shares config."""
    wipe = (request.json or {}).get('wipe_data', False)

    # 1. Stop services
    host_run("systemctl stop smbd nmbd 2>/dev/null || true")
    # 2. Disable so they don't restart on reboot
    host_run("systemctl disable smbd nmbd 2>/dev/null || true")

    ok, dep_msg = release_dep('smbd', 'sharing-samba')

    if wipe:
        # 3. Remove Samba configuration
        host_run("rm -f /etc/samba/smb.conf 2>/dev/null || true")

    # 4. Log event
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'Samba uninstalled (wipe={wipe})')
    except Exception:
        pass

    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove Samba dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/samba/pkg-status', methods=['GET'])
def samba_pkg_status():
    r = host_run("command -v smbd")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'samba_installed': installed})


# ═══════════════════════════════════════════════════════════
#  NFS Sharing
# ═══════════════════════════════════════════════════════════

@storage_bp.route('/nfs/status')
def nfs_status():
    r = host_run("command -v exportfs")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active nfs-server 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running})


@storage_bp.route('/nfs/install', methods=['POST'])
def nfs_install():
    from host import ensure_dep
    ok, msg = ensure_dep('exportfs', install=True)
    if not ok:
        # Try direct package name
        r = _apt_install('nfs-kernel-server', timeout=120)
        if r.returncode != 0:
            return jsonify({"error": f"Installation failed: {r.stderr[-200:]}"}), 500
    host_run("systemctl enable nfs-server && systemctl start nfs-server", timeout=15)
    # Set optimal NFS thread count
    host_run("sed -i 's/^RPCNFSDCOUNT=.*/RPCNFSDCOUNT=16/' /etc/default/nfs-kernel-server 2>/dev/null || echo 'RPCNFSDCOUNT=16' >> /etc/default/nfs-kernel-server")
    host_run("systemctl restart nfs-server", timeout=15)
    return jsonify({"status": "ok"})


@storage_bp.route('/nfs/exports')
def nfs_exports():
    """List current NFS exports."""
    r = host_run("cat /etc/exports 2>/dev/null || echo ''")
    exports = []
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            exports.append({
                "path": parts[0],
                "clients": ' '.join(parts[1:]),
                "raw": line,
            })
    return jsonify({"exports": exports})


@storage_bp.route('/nfs/export', methods=['POST'])
def nfs_export_add():
    """Add an NFS export. Body: {path, network, options}"""
    data = request.json or {}
    path = data.get("path", "").strip()
    network = data.get("network", "*").strip() or "*"
    options = data.get("options", "rw,async,no_subtree_check,no_root_squash,insecure").strip()
    if not path or not os.path.isdir(path):
        return jsonify({"error": "Path not found"}), 400

    export_line = f'{path} {network}({options})'
    # Check for duplicates
    r = host_run("cat /etc/exports 2>/dev/null || echo ''")
    for line in r.stdout.splitlines():
        if line.strip().startswith(path + ' '):
            return jsonify({"error": "This directory is already exported"}), 409

    host_run(f"echo {Q(export_line)} >> /etc/exports")
    host_run("exportfs -ra", timeout=10)
    return jsonify({"ok": True})


@storage_bp.route('/nfs/export', methods=['DELETE'])
def nfs_export_remove():
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "Path required"}), 400
    # Remove line from /etc/exports
    host_run(f"sed -i '\\|^{path} |d' /etc/exports")
    host_run("exportfs -ra", timeout=10)
    return jsonify({"ok": True})


# -- NFS package routes (for EthOS Package Store) --

@storage_bp.route('/nfs/pkg-install', methods=['POST'])
def nfs_pkg_install():
    r = host_run("command -v exportfs")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('exportfs', 'sharing-nfs')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'NFS installation failed'}), 500
    host_run("systemctl enable nfs-server && systemctl start nfs-server", timeout=15)
    return jsonify({'status': 'ok'})


@storage_bp.route('/nfs/pkg-uninstall', methods=['POST'])
def nfs_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    host_run("systemctl stop nfs-server 2>/dev/null || true")
    host_run("systemctl disable nfs-server 2>/dev/null || true")
    ok, dep_msg = release_dep('exportfs', 'sharing-nfs')
    if wipe:
        host_run("rm -f /etc/exports 2>/dev/null || true")
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'NFS uninstalled (wipe={wipe})')
    except Exception:
        pass
    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove NFS dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/nfs/pkg-status', methods=['GET'])
def nfs_pkg_status():
    r = host_run("command -v exportfs")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'nfs_installed': installed})


# ═══════════════════════════════════════════════════════════
#  DLNA / MiniDLNA
# ═══════════════════════════════════════════════════════════

@storage_bp.route('/dlna/status')
def dlna_status():
    r = host_run("command -v minidlnad")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active minidlna 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running})


@storage_bp.route('/dlna/install', methods=['POST'])
def dlna_install():
    r = _apt_install('minidlna', timeout=120)
    if r.returncode != 0:
        return jsonify({"error": f"Installation failed: {r.stderr[-200:]}"}), 500
    host_run("systemctl enable minidlna", timeout=10)
    return jsonify({"status": "ok"})


@storage_bp.route('/dlna/config')
def dlna_config():
    """Get DLNA media directories."""
    r = host_run("cat /etc/minidlna.conf 2>/dev/null || echo ''")
    dirs = []
    friendly_name = "EthOS"
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith('media_dir='):
            dirs.append(line.split('=', 1)[1])
        elif line.startswith('friendly_name='):
            friendly_name = line.split('=', 1)[1]
    return jsonify({"dirs": dirs, "friendly_name": friendly_name})


@storage_bp.route('/dlna/config', methods=['POST'])
def dlna_config_set():
    """Set DLNA media directories. Body: {dirs: ["/home/media", ...], friendly_name: "..."}"""
    data = request.json or {}
    dirs = data.get("dirs", [])
    friendly_name = data.get("friendly_name", "EthOS").strip()

    config = f"""# EthOS MiniDLNA config
friendly_name={friendly_name}
db_dir=/var/cache/minidlna
log_dir=/var/log
inotify=yes
"""
    for d in dirs:
        d = d.strip()
        if d and os.path.isdir(d):
            config += f"media_dir={d}\n"

    _host_write_file('/etc/minidlna.conf', config)
    host_run("systemctl restart minidlna 2>/dev/null || systemctl start minidlna", timeout=10)
    return jsonify({"ok": True})


@storage_bp.route('/dlna/rescan', methods=['POST'])
def dlna_rescan():
    host_run("systemctl restart minidlna", timeout=10)
    return jsonify({"status": "ok"})


# -- DLNA package routes (for EthOS Package Store) --

@storage_bp.route('/dlna/pkg-install', methods=['POST'])
def dlna_pkg_install():
    r = host_run("command -v minidlnad")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('minidlnad', 'sharing-dlna')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'MiniDLNA installation failed'}), 500
    host_run("systemctl enable minidlna", timeout=10)
    return jsonify({'status': 'ok'})


@storage_bp.route('/dlna/pkg-uninstall', methods=['POST'])
def dlna_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    host_run("systemctl stop minidlna 2>/dev/null || true")
    host_run("systemctl disable minidlna 2>/dev/null || true")
    ok, dep_msg = release_dep('minidlnad', 'sharing-dlna')
    if wipe:
        host_run("rm -f /etc/minidlna.conf 2>/dev/null || true")
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'MiniDLNA uninstalled (wipe={wipe})')
    except Exception:
        pass
    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove MiniDLNA dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/dlna/pkg-status', methods=['GET'])
def dlna_pkg_status():
    r = host_run("command -v minidlnad")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'dlna_installed': installed})


# ═══════════════════════════════════════════════════════════
#  SFTP
# ═══════════════════════════════════════════════════════════

@storage_bp.route('/sftp/status')
def sftp_status():
    r = host_run("systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null")
    running = "active" in r.stdout.strip()
    # Check if SFTP subsystem is enabled
    r2 = host_run("grep -q 'Subsystem.*sftp' /etc/ssh/sshd_config 2>/dev/null")
    sftp_enabled = r2.returncode == 0
    return jsonify({"installed": True, "running": running, "sftp_enabled": sftp_enabled})


@storage_bp.route('/sftp/toggle', methods=['POST'])
def sftp_toggle():
    """Enable or disable SFTP subsystem."""
    data = request.json or {}
    enable = data.get("enable", True)

    if enable:
        # Ensure Subsystem sftp line exists and is not commented
        host_run("grep -q '^Subsystem.*sftp' /etc/ssh/sshd_config || "
                 "echo 'Subsystem sftp /usr/lib/openssh/sftp-server' >> /etc/ssh/sshd_config")
        host_run("sed -i 's/^#Subsystem.*sftp/Subsystem sftp \\//g' /etc/ssh/sshd_config")
    else:
        host_run("sed -i 's/^Subsystem.*sftp/#Subsystem sftp/g' /etc/ssh/sshd_config")

    host_run("systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null", timeout=10)
    return jsonify({"ok": True, "enabled": enable})


@storage_bp.route('/sftp/users')
def sftp_users():
    """List system users that can use SFTP."""
    r = host_run("awk -F: '$3>=1000 && $3<65534{print $1\":\"$6}' /etc/passwd")
    users = []
    for line in r.stdout.strip().splitlines():
        parts = line.split(':', 1)
        if len(parts) == 2:
            users.append({"username": parts[0], "home": parts[1]})
    return jsonify({"users": users})


# -- SFTP package routes (for EthOS Package Store) --

@storage_bp.route('/sftp/pkg-install', methods=['POST'])
def sftp_pkg_install():
    """SFTP uses OpenSSH which is usually pre-installed. Enable the subsystem."""
    r = host_run("command -v sshd")
    if r.returncode != 0:
        _apt_install('openssh-server', timeout=120)
    # Enable SFTP subsystem
    host_run("grep -q '^Subsystem.*sftp' /etc/ssh/sshd_config || "
             "echo 'Subsystem sftp /usr/lib/openssh/sftp-server' >> /etc/ssh/sshd_config")
    host_run("sed -i 's/^#Subsystem.*sftp/Subsystem sftp \\/usr\\/lib\\/openssh\\/sftp-server/g' /etc/ssh/sshd_config")
    host_run("systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null", timeout=10)
    return jsonify({'status': 'ok'})


@storage_bp.route('/sftp/pkg-uninstall', methods=['POST'])
def sftp_pkg_uninstall():
    """Disable SFTP subsystem (don't remove sshd — that would kill SSH access)."""
    host_run("sed -i 's/^Subsystem.*sftp/#Subsystem sftp/g' /etc/ssh/sshd_config")
    host_run("systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null", timeout=10)
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', 'SFTP disabled (subsystem disabled)')
    except Exception:
        pass
    return jsonify({'ok': True})


@storage_bp.route('/sftp/pkg-status', methods=['GET'])
def sftp_pkg_status():
    r = host_run("grep -q '^Subsystem.*sftp' /etc/ssh/sshd_config 2>/dev/null")
    enabled = r.returncode == 0
    return jsonify({'status': 'ready' if enabled else 'missing', 'sftp_enabled': enabled})


# ═══════════════════════════════════════════════════════════
#  WebDAV (via lighttpd or built-in)
# ═══════════════════════════════════════════════════════════

_WEBDAV_CONF = '/etc/lighttpd/conf-enabled/90-webdav.conf'
_WEBDAV_PORT = 8888
_WEBDAV_SHARES_FILE = data_path('webdav_shares.json')


def _load_webdav_shares():
    try:
        with open(_WEBDAV_SHARES_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_webdav_shares(shares):
    with open(_WEBDAV_SHARES_FILE, 'w') as f:
        json.dump(shares, f, indent=2)


def _rebuild_webdav_conf(shares):
    """Regenerate lighttpd config from JSON share list."""
    if not shares:
        host_run(f"rm -f {_WEBDAV_CONF}")
        host_run("systemctl restart lighttpd 2>/dev/null || true", timeout=10)
        return

    blocks = []
    for s in shares:
        auth = ""
        if s.get('username'):
            htpasswd = f"/etc/lighttpd/webdav_{s['url_path'].strip('/').replace('/', '_')}.htpasswd"
            auth = f"""
    auth.backend = "htdigest"
    auth.backend.htdigest.userfile = "{htpasswd}"
    auth.require = ( "{s['url_path']}" => ( "method" => "digest", "realm" => "WebDAV", "require" => "valid-user" ) )
"""
        blocks.append(f"""    alias.url += ( "{s['url_path']}" => "{s['fs_path']}" )
    webdav.activate = "enable"
    webdav.is-readonly = "disable"
    webdav.sqlite-db-name = "/var/cache/lighttpd/webdav.db"
{auth}""")

    conf = f"""# EthOS WebDAV — auto-generated, do not edit manually
server.modules += ( "mod_webdav", "mod_alias" )
$SERVER["socket"] == ":{_WEBDAV_PORT}" {{
{chr(10).join(blocks)}
}}
"""
    _host_write_file(_WEBDAV_CONF, conf)
    host_run("mkdir -p /var/cache/lighttpd && systemctl restart lighttpd", timeout=10)


@storage_bp.route('/webdav/status')
def webdav_status():
    r = host_run("command -v lighttpd")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active lighttpd 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running, "port": _WEBDAV_PORT})


@storage_bp.route('/webdav/install', methods=['POST'])
def webdav_install():
    r = _apt_install('lighttpd', timeout=120)
    if r.returncode != 0:
        return jsonify({"error": f"Installation failed: {r.stderr[-200:]}"}), 500
    # Enable WebDAV module
    host_run("lighttpd-enable-mod webdav 2>/dev/null || true")
    return jsonify({"status": "ok"})


@storage_bp.route('/webdav/shares')
def webdav_shares():
    """Get current WebDAV shares from JSON store."""
    shares = _load_webdav_shares()
    return jsonify({"shares": shares, "port": _WEBDAV_PORT})


@storage_bp.route('/webdav/share', methods=['POST'])
def webdav_add():
    """Add a WebDAV share. Body: {path, url_path, username, password}"""
    data = request.json or {}
    fs_path = data.get("path", "").strip()
    url_path = data.get("url_path", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not fs_path or not os.path.isdir(fs_path):
        return jsonify({"error": "Path not found"}), 400
    if not url_path:
        url_path = '/' + os.path.basename(fs_path)

    # Validate username/password — reject shell-dangerous characters
    import re as _re_val
    if username and not _re_val.match(r'^[a-zA-Z0-9._@-]+$', username):
        return jsonify({"error": "Invalid characters in username"}), 400
    if password and any(c in password for c in "'\"\\`$"):
        return jsonify({"error": "Invalid characters in password"}), 400

    # Set up htpasswd for this share if auth requested
    if username and password:
        htpasswd = f"/etc/lighttpd/webdav_{url_path.strip('/').replace('/', '_')}.htpasswd"
        host_run(f"printf '%s\\n' {Q(password)} | htpasswd -i -c {htpasswd} {Q(username)} 2>/dev/null || "
                 f"printf '%s' {Q(username + ':WebDAV:' + password)} | md5sum | cut -d' ' -f1 | "
                 f"xargs -I{{}} printf '%s\\n' {Q(username)}':WebDAV:'{{}}'\\n' > {htpasswd}")

    # Save to JSON and rebuild config
    shares = _load_webdav_shares()
    # Remove existing share with same url_path (update)
    shares = [s for s in shares if s.get('url_path') != url_path]
    shares.append({
        "url_path": url_path,
        "fs_path": fs_path,
        "username": username or None,
    })
    _save_webdav_shares(shares)
    _rebuild_webdav_conf(shares)

    return jsonify({"ok": True, "port": _WEBDAV_PORT})


@storage_bp.route('/webdav/share', methods=['DELETE'])
def webdav_remove():
    """Remove a single WebDAV share by url_path, or all if no url_path given."""
    data = request.json or {}
    url_path = data.get("url_path", "").strip()

    shares = _load_webdav_shares()
    if url_path:
        shares = [s for s in shares if s.get('url_path') != url_path]
    else:
        shares = []

    _save_webdav_shares(shares)
    _rebuild_webdav_conf(shares)
    return jsonify({"ok": True})


# -- WebDAV package routes (for EthOS Package Store) --

@storage_bp.route('/webdav/pkg-install', methods=['POST'])
@admin_required
def webdav_pkg_install():
    r = host_run("command -v lighttpd")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('lighttpd', 'sharing-webdav')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'lighttpd installation failed'}), 500
    host_run("lighttpd-enable-mod webdav 2>/dev/null || true")
    return jsonify({'status': 'ok'})


@storage_bp.route('/webdav/pkg-uninstall', methods=['POST'])
@admin_required
def webdav_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    host_run("systemctl stop lighttpd 2>/dev/null || true")
    host_run("systemctl disable lighttpd 2>/dev/null || true")
    ok, dep_msg = release_dep('lighttpd', 'sharing-webdav')
    if wipe:
        host_run(f"rm -f {_WEBDAV_CONF} 2>/dev/null || true")
        host_run("rm -f /etc/lighttpd/webdav_*.htpasswd 2>/dev/null || true")
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'WebDAV uninstalled (wipe={wipe})')
    except Exception:
        pass
    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove WebDAV dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/webdav/pkg-status', methods=['GET'])
def webdav_pkg_status():
    r = host_run("command -v lighttpd")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'webdav_installed': installed})


# ═══════════════════════════════════════════════════════════
#  FTP (vsftpd)
# ═══════════════════════════════════════════════════════════

@storage_bp.route('/ftp/status')
def ftp_status():
    r = host_run("command -v vsftpd")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active vsftpd 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running})


@storage_bp.route('/ftp/install', methods=['POST'])
def ftp_install():
    r = _apt_install('vsftpd', timeout=120)
    if r.returncode != 0:
        return jsonify({"error": f"Installation failed: {r.stderr[-200:]}"}), 500
    # Sensible defaults
    config = """# EthOS vsftpd config
listen=YES
listen_ipv6=NO
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=022
chroot_local_user=YES
allow_writeable_chroot=YES
pasv_enable=YES
pasv_min_port=40000
pasv_max_port=40100
"""
    _host_write_file('/etc/vsftpd.conf', config)
    host_run("systemctl enable vsftpd && systemctl restart vsftpd", timeout=10)
    return jsonify({"status": "ok"})


@storage_bp.route('/ftp/toggle', methods=['POST'])
def ftp_toggle():
    """Start or stop vsftpd. Body: {enable: bool}"""
    data = request.json or {}
    enable = data.get("enable", True)
    if enable:
        host_run("systemctl start vsftpd", timeout=10)
    else:
        host_run("systemctl stop vsftpd", timeout=10)
    return jsonify({"ok": True, "enabled": enable})


# -- FTP package routes (for EthOS Package Store) --

@storage_bp.route('/ftp/pkg-install', methods=['POST'])
def ftp_pkg_install():
    r = host_run("command -v vsftpd")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('vsftpd', 'sharing-ftp')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'vsftpd installation failed'}), 500
    host_run("systemctl enable vsftpd && systemctl restart vsftpd", timeout=10)
    return jsonify({'status': 'ok'})


@storage_bp.route('/ftp/pkg-uninstall', methods=['POST'])
def ftp_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    host_run("systemctl stop vsftpd 2>/dev/null || true")
    host_run("systemctl disable vsftpd 2>/dev/null || true")
    ok, dep_msg = release_dep('vsftpd', 'sharing-ftp')
    if wipe:
        host_run("rm -f /etc/vsftpd.conf 2>/dev/null || true")
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'FTP (vsftpd) uninstalled (wipe={wipe})')
    except Exception:
        pass
    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove FTP dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/ftp/pkg-status', methods=['GET'])
def ftp_pkg_status():
    r = host_run("command -v vsftpd")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'ftp_installed': installed})


# ═══════════════════════════════════════════════════════════
#  Disk Formatting
# ═══════════════════════════════════════════════════════════

FORMAT_FS_OPTIONS = [
    {"value": "ext4",  "label": "ext4",  "desc": "Best for Linux — fast, reliable, journaling", "linux": True,  "windows": False, "mac": False},
    {"value": "btrfs", "label": "Btrfs", "desc": "Modern Linux — snapshots, compression, RAID", "linux": True,  "windows": False, "mac": False},
    {"value": "xfs",   "label": "XFS",   "desc": "High-performance Linux — large files, servers", "linux": True,  "windows": False, "mac": False},
    {"value": "ntfs",  "label": "NTFS",  "desc": "Windows — full Windows compatibility", "linux": True,  "windows": True,  "mac": True},
    {"value": "exfat", "label": "exFAT", "desc": "Universal — Windows, Mac, Linux, large files", "linux": True,  "windows": True,  "mac": True},
    {"value": "vfat",  "label": "FAT32", "desc": "Maximum compatibility — 4 GB file size limit", "linux": True,  "windows": True,  "mac": True},
    {"value": "ext3",  "label": "ext3",  "desc": "Legacy Linux — journaling, compatibility", "linux": True,  "windows": False, "mac": False},
    {"value": "f2fs",  "label": "F2FS",  "desc": "Flash-Friendly — SSD, USB drives, SD cards", "linux": True,  "windows": False, "mac": False},
]

# Mapping from fs value to mkfs command
MKFS_CMDS = {
    "ext4":  "mkfs.ext4 -F",
    "ext3":  "mkfs.ext3 -F",
    "btrfs": "mkfs.btrfs -f",
    "xfs":   "mkfs.xfs -f",
    "ntfs":  "mkfs.ntfs -f",
    "exfat": "mkfs.exfat",
    "vfat":  "mkfs.vfat -F 32",
    "f2fs":  "mkfs.f2fs -f",
}


@storage_bp.route('/relabel', methods=['POST'])
def relabel_drive():
    """Change the filesystem label of a partition.
    Expects JSON: { drive: 'sdb2', label: 'NewLabel' }
    For ext2/3/4 uses e2label, for ntfs uses ntfslabel, for fat uses fatlabel, for exfat uses exfatlabel.
    Ext can be relabeled while mounted; NTFS/FAT must be unmounted first.
    """
    data = request.json or {}
    drive = data.get('drive', '').strip()
    new_label = data.get('label', '').strip()

    if not drive or not re.match(r'^[a-zA-Z0-9]+$', drive):
        return jsonify({'error': 'Invalid drive name'}), 400
    if not new_label:
        return jsonify({'error': 'Label is required'}), 400
    # Sanitize label: no slashes, max 32 chars
    if '/' in new_label or len(new_label) > 32:
        return jsonify({'error': 'Label must be max 32 chars, no slashes'}), 400

    dev_path = f'/dev/{drive}'

    # Detect filesystem type
    r = host_run(f"lsblk -no FSTYPE {Q(dev_path)} 2>/dev/null")
    if r.returncode != 0 or not r.stdout.strip():
        return jsonify({'error': 'Cannot detect filesystem type'}), 400
    fstype = r.stdout.strip().lower()

    # Check if mounted
    r_mp = host_run(f"findmnt -n -o TARGET {Q(dev_path)} 2>/dev/null")
    is_mounted = r_mp.returncode == 0 and r_mp.stdout.strip() != ''
    mountpoint = r_mp.stdout.strip() if is_mounted else None

    # Build the label command based on filesystem
    # For filesystems that require unmount, handle it automatically
    needs_remount = False
    if fstype in ('ext2', 'ext3', 'ext4'):
        cmd = f"e2label {Q(dev_path)} {Q(new_label)}"
    elif fstype in ('ntfs', 'fuseblk'):
        if is_mounted:
            r_um = host_run(f"umount {Q(mountpoint)}")
            if r_um.returncode != 0:
                r_um = host_run(f"umount -l {Q(mountpoint)}")
                if r_um.returncode != 0:
                    return jsonify({'error': 'Failed to unmount NTFS disk'}), 500
            needs_remount = True
        cmd = f"ntfslabel --force {Q(dev_path)} {Q(new_label)}"
    elif fstype in ('vfat', 'fat32', 'fat16'):
        if is_mounted:
            r_um = host_run(f"umount {Q(mountpoint)}")
            if r_um.returncode != 0:
                return jsonify({'error': 'Failed to unmount FAT disk'}), 500
            needs_remount = True
        cmd = f"fatlabel {Q(dev_path)} {Q(new_label)}"
    elif fstype == 'exfat':
        if is_mounted:
            r_um = host_run(f"umount {Q(mountpoint)}")
            if r_um.returncode != 0:
                return jsonify({'error': 'Failed to unmount exFAT disk'}), 500
            needs_remount = True
        cmd = f"exfatlabel {Q(dev_path)} {Q(new_label)}"
    elif fstype == 'btrfs':
        cmd = f"btrfs filesystem label {Q(dev_path)} {Q(new_label)}"
    elif fstype == 'xfs':
        if is_mounted:
            r_um = host_run(f"umount {Q(mountpoint)}")
            if r_um.returncode != 0:
                return jsonify({'error': 'Failed to unmount XFS disk'}), 500
            needs_remount = True
        cmd = f"xfs_admin -L {Q(new_label)} {Q(dev_path)}"
    else:
        return jsonify({'error': f'Label change is not supported for {fstype}'}), 400

    r = host_run(cmd, timeout=15)
    if r.returncode != 0:
        # If we unmounted, try to remount before returning error
        if needs_remount and mountpoint:
            host_run(f"mount {Q(dev_path)} {Q(mountpoint)}", timeout=10)
        return jsonify({'error': f'Error: {r.stderr.strip() or r.stdout.strip()}'}), 500

    # Remount at new label path if it was auto-unmounted
    if needs_remount and mountpoint:
        # Mount to a path based on new label
        new_mp = f"/media/devmon/{new_label}"
        host_run(f"mkdir -p {Q(new_mp)}", timeout=5)
        r_rm = host_run(f"mount {Q(dev_path)} {Q(new_mp)}", timeout=15)
        if r_rm.returncode != 0:
            # Fallback: remount at old path
            host_run(f"mount {Q(dev_path)} {Q(mountpoint)}", timeout=10)
        else:
            # Clean up old empty mountpoint dir
            host_run(f"rmdir {Q(mountpoint)} 2>/dev/null", timeout=5)

    return jsonify({'success': True, 'drive': drive, 'label': new_label, 'fstype': fstype})


@storage_bp.route('/format/options')
def format_options():
    """Return available filesystem options with system recommendation."""
    # Detect current system filesystem
    r = host_run("findmnt -n -o FSTYPE / 2>/dev/null")
    system_fs = r.stdout.strip() if r.returncode == 0 else "ext4"

    # Check which mkfs tools are available
    options = []
    for fs in FORMAT_FS_OPTIONS:
        cmd = MKFS_CMDS.get(fs["value"], "").split()[0]
        if cmd:
            avail_r = host_run(f"command -v {cmd}")
            available = avail_r.returncode == 0
        else:
            available = False
        options.append({
            **fs,
            "available": available,
            "recommended": fs["value"] == system_fs,
        })

    return jsonify({
        "system_fs": system_fs,
        "options": options,
    })


@storage_bp.route('/format', methods=['POST'])
def format_drive():
    """Format a drive with a specified filesystem.
    Expects JSON: { drive: 'sda1', fstype: 'ext4', label: 'MyDisk' }
    The drive MUST be unmounted first.
    """
    data = request.json or {}
    drive_name = data.get("drive", "").strip()
    fstype = data.get("fstype", "").strip()
    label = data.get("label", "").strip()

    if not drive_name or not fstype:
        return jsonify({"error": "drive and fstype are required"}), 400

    # Validate drive name (prevent injection)
    if not re.match(r'^[a-zA-Z0-9]+$', drive_name):
        return jsonify({"error": "Invalid device name"}), 400

    # Validate fstype
    if fstype not in MKFS_CMDS:
        return jsonify({"error": f"Unsupported filesystem: {fstype}"}), 400

    dev_path = f"/dev/{drive_name}"

    # Check device exists
    r = host_run(f"lsblk -no NAME {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": f"Device {dev_path} does not exist"}), 404

    # Check device is not mounted (including child partitions)
    r = host_run(f"findmnt -n -o TARGET {dev_path} 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        return jsonify({"error": f"Device is mounted on {r.stdout.strip()}. Unmount first."}), 400

    # Also check child devices (e.g. sda has sda1 mounted)
    r = host_run(f"lsblk -no MOUNTPOINT {dev_path} 2>/dev/null")
    if r.returncode == 0:
        mounted_children = [mp.strip() for mp in r.stdout.strip().split('\n') if mp.strip()]
        if mounted_children:
            # Check for system mounts first
            for mp in mounted_children:
                if mp in ('/', '/boot', '/boot/efi', '/home'):
                    return jsonify({"error": f"Device is in use by system ({mp})!"}), 403
            # Any other mount — must unmount first
            return jsonify({"error": f"Partition is mounted on {mounted_children[0]}. Unmount first."}), 400

    # Safety: refuse to format system disk (robust check incl. LVM)
    # Extract base disk name for system check
    _base_disk = re.sub(r'p?\d+$', '', drive_name)
    if _is_system_disk(drive_name) or (_base_disk != drive_name and _is_system_disk(_base_disk)):
        return jsonify({"error": "Cannot format system disk!"}), 403

    # Build mkfs command
    mkfs_base = MKFS_CMDS[fstype]
    label_flag = ""
    if label:
        safe_label = re.sub(r'[^a-zA-Z0-9_\-.]', '_', label)[:16]
        if fstype in ("ext4", "ext3", "btrfs", "f2fs"):
            label_flag = f"-L {Q(safe_label)}"
        elif fstype == "xfs":
            label_flag = f"-L {Q(safe_label)}"
        elif fstype == "ntfs":
            label_flag = f"-L {Q(safe_label)}"
        elif fstype in ("exfat", "vfat"):
            label_flag = f"-n {Q(safe_label[:11])}"

    cmd = f"{mkfs_base} {label_flag} {dev_path} 2>&1"

    def generate():
        yield f"data: {json.dumps({'type': 'step', 'message': f'Formatting {dev_path} as {fstype}...'})}\n\n"
        if label:
            yield f"data: {json.dumps({'type': 'step', 'message': f'Label: {label}'})}\n\n"

        for line in host_run_stream(cmd):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = int(line.split(":")[1])
                if code == 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': 'Format completed successfully!'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': True})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'step', 'message': f'Format error (code: {code})'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Merge Partitions (repartition to single partition + format)
# ---------------------------------------------------------------------------

@storage_bp.route('/merge', methods=['POST'])
def merge_partitions():
    """Merge all partitions on a disk into one and format it.
    Expects JSON: { disk: 'sdb', fstype: 'ext4', label: 'MyDisk' }
    ALL partitions on disk MUST be unmounted.
    """
    data = request.json or {}
    disk_name = data.get("disk", "").strip()
    fstype = data.get("fstype", "").strip()
    label = data.get("label", "").strip()

    if not disk_name or not fstype:
        return jsonify({"error": "disk and fstype are required"}), 400

    # Validate disk name (only base disk, no partition numbers)
    if not _validate_disk_name(disk_name):
        return jsonify({"error": "Invalid disk name (provide base disk e.g. sdb, not a partition)"}), 400

    if fstype not in MKFS_CMDS:
        return jsonify({"error": f"Unsupported filesystem: {fstype}"}), 400

    dev_path = f"/dev/{disk_name}"

    # Check device exists and is a disk
    r = host_run(f"lsblk -ndo TYPE {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": f"Device {dev_path} does not exist"}), 404
    if r.stdout.strip() != 'disk':
        return jsonify({"error": f"{dev_path} is not a base disk"}), 400

    # Get all partitions on this disk
    r = host_run(f"lsblk -nlo NAME,MOUNTPOINT {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": "Cannot read partitions"}), 500

    parts_mounted = []
    for line in r.stdout.strip().split('\n'):
        parts = line.split(None, 1)
        if len(parts) >= 2 and parts[1].strip():
            pname = parts[0].strip()
            mp = parts[1].strip()
            if pname != disk_name:  # Skip the disk itself
                parts_mounted.append(f"{pname} → {mp}")

    if parts_mounted:
        return jsonify({"error": f"Partitions are mounted: {', '.join(parts_mounted)}. Unmount them first."}), 400

    # Safety: refuse system disk (robust check incl. LVM)
    if _is_system_disk(disk_name):
        return jsonify({"error": "Cannot merge partitions of system disk!"}), 403

    # Build label flags
    safe_label = ""
    if label:
        safe_label = re.sub(r'[^a-zA-Z0-9_\-.]', '_', label)[:16]

    def generate():
        yield f"data: {json.dumps({'type': 'step', 'message': f'Merging partitions on {dev_path}...'})}\n\n"

        # Step 1: Wipe existing partition signatures
        yield f"data: {json.dumps({'type': 'step', 'message': 'Wiping partition table...'})}\n\n"
        for line in host_run_stream(f"wipefs -a {dev_path} 2>&1"):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = int(line.split(":")[1])
                if code != 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': f'wipefs error (code: {code})'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                    return
            else:
                if line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Step 2: Create new GPT partition table with single partition
        yield f"data: {json.dumps({'type': 'step', 'message': 'Creating new GPT partition table...'})}\n\n"
        parted_cmd = f"parted -s {dev_path} mklabel gpt mkpart primary 0% 100% 2>&1"
        for line in host_run_stream(parted_cmd):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = int(line.split(":")[1])
                if code != 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': f'parted error (code: {code})'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                    return
            else:
                if line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Wait for kernel to re-read partition table
        yield f"data: {json.dumps({'type': 'step', 'message': 'Refreshing partition table...'})}\n\n"
        host_run(f"partprobe {dev_path} 2>/dev/null")
        host_run("sleep 2")

        # Determine new partition name (sdb1, nvme0n1p1, etc.)
        if 'nvme' in disk_name:
            new_part = f"{dev_path}p1"
            new_part_name = f"{disk_name}p1"
        else:
            new_part = f"{dev_path}1"
            new_part_name = f"{disk_name}1"

        # Auto-unmount in case devmon/udisks auto-mounted the new partition
        host_run(f"umount {new_part} 2>/dev/null")
        host_run("sleep 1")

        # Verify new partition exists
        r2 = host_run(f"lsblk -no NAME {new_part} 2>/dev/null")
        if r2.returncode != 0:
            yield f"data: {json.dumps({'type': 'step', 'message': f'Partition {new_part} was not created'})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'step', 'message': f'Created partition {new_part_name}'})}\n\n"

        # Step 3: Format the new partition
        mkfs_base = MKFS_CMDS[fstype]
        label_flag = ""
        if safe_label:
            if fstype in ("ext4", "ext3", "btrfs", "f2fs", "xfs"):
                label_flag = f"-L {Q(safe_label)}"
            elif fstype == "ntfs":
                label_flag = f"-L {Q(safe_label)}"
            elif fstype in ("exfat", "vfat"):
                label_flag = f"-n {Q(safe_label[:11])}"

        fmt_cmd = f"{mkfs_base} {label_flag} {new_part} 2>&1"
        yield f"data: {json.dumps({'type': 'step', 'message': f'Formatting as {fstype}...'})}\n\n"

        for line in host_run_stream(fmt_cmd):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = int(line.split(":")[1])
                if code == 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': 'Format completed successfully!'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': True, 'partition': new_part_name})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'step', 'message': f'Format error (code: {code})'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
            else:
                if line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Split / Partition a Disk
# ---------------------------------------------------------------------------

@storage_bp.route('/partition', methods=['POST'])
def partition_disk():
    """Repartition a disk into multiple partitions and format each.
    Expects JSON: {
        disk: 'sdb',
        partitions: [
            { size_mb: 50000, fstype: 'ext4', label: 'Dane' },
            { size_mb: 0, fstype: 'ntfs', label: 'Media' },   // 0 = remaining space
        ]
    }
    ALL existing partitions MUST be unmounted.
    """
    data = request.json or {}
    disk_name = data.get("disk", "").strip()
    partitions = data.get("partitions", [])

    if not disk_name:
        return jsonify({"error": "Disk name is required"}), 400
    if not partitions or len(partitions) < 1:
        return jsonify({"error": "Provide at least one partition"}), 400
    if len(partitions) > 8:
        return jsonify({"error": "Maximum 8 partitions"}), 400

    # Validate disk name
    if not _validate_disk_name(disk_name):
        return jsonify({"error": "Invalid disk name"}), 400

    # Validate each partition
    for i, p in enumerate(partitions):
        fs = p.get("fstype", "").strip()
        if not fs:
            return jsonify({"error": f"Partition {i+1}: no filesystem specified"}), 400
        if fs not in MKFS_CMDS:
            return jsonify({"error": f"Partition {i+1}: unsupported filesystem '{fs}'"}), 400

    # Count how many use size_mb=0 (remaining space)
    remaining_count = sum(1 for p in partitions if not p.get("size_mb"))
    if remaining_count > 1:
        return jsonify({"error": "Only one partition can use 'remaining space'"}), 400

    dev_path = f"/dev/{disk_name}"

    # Check device exists and is a disk
    r = host_run(f"lsblk -ndo TYPE {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": f"Device {dev_path} does not exist"}), 404
    if r.stdout.strip() != 'disk':
        return jsonify({"error": f"{dev_path} is not a base disk"}), 400

    # Get disk size in bytes
    r = host_run(f"lsblk -ndbo SIZE {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": "Cannot read disk size"}), 500
    disk_bytes = int(r.stdout.strip())
    disk_mb = disk_bytes // (1024 * 1024)

    # Validate total size
    total_requested_mb = sum(p.get("size_mb", 0) for p in partitions)
    if total_requested_mb > disk_mb:
        return jsonify({"error": f"Total partition size ({total_requested_mb} MB) exceeds disk size ({disk_mb} MB)"}), 400

    # Check all partitions unmounted
    r = host_run(f"lsblk -nlo NAME,MOUNTPOINT {dev_path} 2>/dev/null")
    if r.returncode == 0:
        for line in r.stdout.strip().split('\n'):
            cols = line.split(None, 1)
            if len(cols) >= 2 and cols[1].strip() and cols[0].strip() != disk_name:
                return jsonify({"error": f"Partition {cols[0].strip()} is mounted on {cols[1].strip()}. Unmount first."}), 400

    # Safety: refuse system disk (robust check incl. LVM)
    if _is_system_disk(disk_name):
        return jsonify({"error": "Cannot partition system disk!"}), 403

    def _label_flag(fstype, label):
        if not label:
            return ""
        safe = re.sub(r'[^a-zA-Z0-9_\-.]', '_', label)[:16]
        if fstype in ("ext4", "ext3", "btrfs", "f2fs", "xfs", "ntfs"):
            return f"-L {Q(safe)}"
        elif fstype in ("exfat", "vfat"):
            return f"-n {Q(safe[:11])}"
        return ""

    def generate():
        yield f"data: {json.dumps({'type': 'step', 'message': f'Partitioning {dev_path} into {len(partitions)} partitions...'})}\n\n"

        # Step 1: Wipe
        yield f"data: {json.dumps({'type': 'step', 'message': 'Wiping partition table...'})}\n\n"
        for line in host_run_stream(f"wipefs -a {dev_path} 2>&1"):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                if int(line.split(":")[1]) != 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': 'wipefs error'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                    return
            elif line.strip():
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Step 2: Create GPT label
        yield f"data: {json.dumps({'type': 'step', 'message': 'Creating GPT table...'})}\n\n"
        for line in host_run_stream(f"parted -s {dev_path} mklabel gpt 2>&1"):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                if int(line.split(":")[1]) != 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': 'parted mklabel error'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                    return
            elif line.strip():
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Step 3: Create partitions
        # Build parted mkpart commands with MB offsets
        offset_mb = 1  # Start at 1 MiB for alignment
        part_specs = []
        for i, p in enumerate(partitions):
            size_mb = p.get("size_mb", 0)
            if size_mb > 0:
                end_mb = offset_mb + size_mb
                part_specs.append((offset_mb, end_mb, p))
                offset_mb = end_mb
            else:
                # Remaining space — use 100%
                part_specs.append((offset_mb, -1, p))  # -1 means "100%"

        for idx, (start, end, p) in enumerate(part_specs):
            pnum = idx + 1
            end_str = "100%" if end == -1 else f"{end}MiB"
            parted_cmd = f"parted -s {dev_path} mkpart primary {start}MiB {end_str} 2>&1"
            yield f"data: {json.dumps({'type': 'step', 'message': f'Creating partition {pnum}/{len(partitions)}...'})}\n\n"
            for line in host_run_stream(parted_cmd):
                line = line.rstrip("\n")
                if line.startswith("__EXIT_CODE__:"):
                    if int(line.split(":")[1]) != 0:
                        yield f"data: {json.dumps({'type': 'step', 'message': f'Error creating partition {pnum}'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                        return
                elif line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Step 4: Re-read partition table
        yield f"data: {json.dumps({'type': 'step', 'message': 'Refreshing partition table...'})}\n\n"
        host_run(f"partprobe {dev_path} 2>/dev/null")
        host_run("sleep 2")

        # Auto-unmount any auto-mounted partitions (devmon/udisks)
        for idx in range(len(partitions)):
            pnum = idx + 1
            if 'nvme' in disk_name:
                pdev = f"{dev_path}p{pnum}"
            else:
                pdev = f"{dev_path}{pnum}"
            host_run(f"umount {pdev} 2>/dev/null")
        host_run("sleep 1")

        # Step 5: Format each partition
        created = []
        for idx, (_, _, p) in enumerate(part_specs):
            pnum = idx + 1
            if 'nvme' in disk_name:
                pdev = f"{dev_path}p{pnum}"
                pname = f"{disk_name}p{pnum}"
            else:
                pdev = f"{dev_path}{pnum}"
                pname = f"{disk_name}{pnum}"

            fstype = p.get("fstype", "ext4")
            label = p.get("label", "").strip()
            mkfs_base = MKFS_CMDS[fstype]
            lbl = _label_flag(fstype, label)

            yield f"data: {json.dumps({'type': 'step', 'message': f'Formatting {pname} as {fstype}...'})}\n\n"

            fmt_ok = False
            for line in host_run_stream(f"{mkfs_base} {lbl} {pdev} 2>&1"):
                line = line.rstrip("\n")
                if line.startswith("__EXIT_CODE__:"):
                    code = int(line.split(":")[1])
                    if code == 0:
                        fmt_ok = True
                        yield f"data: {json.dumps({'type': 'step', 'message': f'{pname} formatted ✓'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'step', 'message': f'Format error for {pname} (code: {code})'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                        return
                elif line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

            created.append(pname)

        parts_str = ", ".join(created)
        yield f"data: {json.dumps({'type': 'step', 'message': f'Partitioning complete! Created: {parts_str}'})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'success': True, 'partitions': created})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Filesystem Dependencies
# ---------------------------------------------------------------------------

FS_DEPS = {
    "ntfs": {"label": "NTFS (ntfs-3g)", "check": "command -v ntfs-3g", "packages": "ntfs-3g"},
    "ntfs3": {"label": "NTFS3 (kernel)", "check": "modprobe -n ntfs3 2>/dev/null", "packages": "linux-modules-extra-$(uname -r)"},
    "exfat": {"label": "exFAT", "check": "command -v mount.exfat || command -v mount.exfat-fuse", "packages": "exfat-fuse exfatprogs"},
    "btrfs": {"label": "Btrfs", "check": "command -v btrfs", "packages": "btrfs-progs"},
    "xfs": {"label": "XFS", "check": "command -v xfs_repair", "packages": "xfsprogs"},
    "hfsplus": {"label": "HFS+", "check": "command -v fsck.hfsplus", "packages": "hfsplus hfsutils hfsprogs"},
    "f2fs": {"label": "F2FS", "check": "command -v fsck.f2fs", "packages": "f2fs-tools"},
}


@storage_bp.route('/deps')
def check_deps():
    results = []
    for fs, info in FS_DEPS.items():
        r = host_run(info["check"])
        results.append({
            "fs": fs,
            "label": info["label"],
            "installed": r.returncode == 0,
            "packages": info["packages"],
        })
    return jsonify(results)


@storage_bp.route('/deps/install', methods=['POST'])
def install_deps():
    data = request.json or {}
    packages_str = data.get("packages", "")
    if not packages_str:
        return jsonify({"error": "packages is required"}), 400

    # Validate package names to prevent command injection
    # Only allow alphanumeric, hyphens, dots, colons, plus signs (valid dpkg chars)
    packages = packages_str.split()
    for pkg in packages:
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.+\-:~]+$', pkg):
            return jsonify({"error": f"Invalid package name: {pkg}"}), 400
    safe_packages = ' '.join(packages)

    def generate():
        install_script = f"""
export DEBIAN_FRONTEND=noninteractive
echo '::STEP::Repairing package manager...'
dpkg --configure -a 2>/dev/null || true
echo '::STEP::Updating package lists...'
apt-get update -y 2>&1
echo '::STEP::Installing {safe_packages}...'
apt-get install -y {safe_packages} 2>&1
echo '::STEP::Installation complete!'
echo '::DONE::'
"""
        for line in host_run_stream(install_script):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = line.split(":")[1]
                yield f"data: {json.dumps({'type': 'exit', 'code': int(code)})}\n\n"
            elif line.startswith("::STEP::"):
                yield f"data: {json.dumps({'type': 'step', 'message': line[8:]})}\n\n"
            elif line.startswith("::DONE::"):
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# API – Disk Usage Analytics
# ---------------------------------------------------------------------------

def _fmt_bytes(n):
    """Human-readable bytes."""
    return fmt_bytes(n)


@storage_bp.route('/analyze')
def analyze_path():
    """Analyze disk usage of a directory. Returns top-level children with sizes.
    Query params: path (required), limit (default 50)
    Uses du -k (1K blocks) which is much faster than du -b (apparent size).
    """
    path = request.args.get('path', '/')
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
    except (ValueError, TypeError):
        limit = 50

    # Validate path is absolute
    if not path.startswith('/'):
        return jsonify({"error": "Path must be absolute"}), 400

    # du -k --max-depth=1: list direct children sizes in KB
    # Use -x (stay on same fs) for root-level paths that might have mounts under them
    # Don't use -x for specific mount points (user wants to scan that drive)
    use_x = path in ('/', '/home', '/var', '/usr', '/opt', '/tmp', '/root')
    x_flag = 'x' if use_x else ''
    cmd = f"du -k{x_flag} --max-depth=1 {Q(path)} 2>/dev/null | sort -rn | head -n {limit + 1}"
    r = host_run(cmd, timeout=90)
    if r.returncode != 0 and not r.stdout.strip():
        return jsonify({"error": "Cannot analyze path", "detail": r.stderr.strip()}), 400

    entries = []
    total_size = 0
    norm_path = path.rstrip('/')
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t', 1)
        if len(parts) != 2:
            continue
        try:
            size_kb = int(parts[0])
        except ValueError:
            continue
        entry_path = parts[1].strip()
        size_bytes = size_kb * 1024

        ep_norm = entry_path.rstrip('/')
        if ep_norm == norm_path or ep_norm == '':
            total_size = size_bytes
            continue

        name = ep_norm[len(norm_path):].lstrip('/')
        if not name or '/' in name:
            continue

        entries.append({
            'path': entry_path,
            'name': name,
            'size': size_bytes,
            'size_human': _fmt_bytes(size_bytes),
        })

    # Compute percentages
    for e in entries:
        e['percent'] = round((e['size'] / total_size * 100) if total_size > 0 else 0, 1)

    return jsonify({
        'path': path,
        'total_size': total_size,
        'total_size_human': _fmt_bytes(total_size),
        'entries': entries[:limit],
    })


@storage_bp.route('/analyze/files')
def analyze_files():
    """Find the largest files in a directory.
    Query params: path (required), limit (default 50)
    """
    path = request.args.get('path', '/')
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
    except (ValueError, TypeError):
        limit = 50

    if not path.startswith('/'):
        return jsonify({"error": "Path must be absolute"}), 400

    cmd = (
        f"find {Q(path)} -xdev -maxdepth 8 -type f -printf '%s\\t%T@\\t%p\\n' 2>/dev/null | "
        f"sort -rn | head -n {limit}"
    )
    r = host_run(cmd, timeout=90)
    if r.returncode != 0 and not r.stdout.strip():
        return jsonify({"error": "Cannot scan files", "detail": r.stderr.strip()}), 400

    files = []
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t', 2)
        if len(parts) < 3:
            continue
        try:
            size = int(parts[0])
            mtime = float(parts[1])
        except ValueError:
            continue
        fpath = parts[2].strip()
        name = os.path.basename(fpath)
        ext = os.path.splitext(name)[1].lower()

        files.append({
            'path': fpath,
            'name': name,
            'ext': ext,
            'size': size,
            'size_human': _fmt_bytes(size),
            'modified': mtime,
        })

    return jsonify({
        'path': path,
        'files': files,
    })
