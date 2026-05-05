"""
EthOS — Storage Manager Blueprint
Drive mounting/unmounting, file sharing (Samba, NFS, DLNA, WebDAV, SFTP),
WS-Discovery (wsdd), USB hotplug monitoring, network drive mounts.
"""

import json
import logging
import os
import re
import subprocess
import base64
import time
import threading
import sys
from flask import Blueprint, jsonify, request, Response, stream_with_context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_base, host_run_stream as _host_run_stream_base, data_path, q as _q_imported, apt_install as _apt_install, claim_dep, release_dep, ufw_allow, ufw_delete, get_data_disk as _get_data_disk
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
    try:
        with open(_KEEPALIVE_FILE, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    _keepalive_drives.clear()
    _keepalive_drives.update(data)


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
                    # Mounted correctly — keep USB awake AND prevent APM spindown
                    # by doing a lightweight real disk read (stat alone uses VFS cache).
                    _disable_usb_autosuspend(dev_name)
                    host_run(f"ls {Q(mp)} >/dev/null 2>&1", timeout=10)
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

    # Run quick fsck on ext filesystems before remounting
    if fstype in ('ext4', 'ext3', 'ext2'):
        print(f'[keepalive] Running e2fsck -p on /dev/{dev_name}')
        fsck_r = host_run(f'e2fsck -p /dev/{dev_name} 2>&1', timeout=300)
        if fsck_r.returncode in (0, 1):
            print(f'[keepalive] e2fsck OK (code {fsck_r.returncode})')
        else:
            print(f'[keepalive] e2fsck warning (code {fsck_r.returncode}): {fsck_r.stdout.strip()[:200]}')

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
            # First: force a real directory read to wake the disk from APM standby
            # (stat on a mountpoint may use VFS cache and not touch the spindle)
            host_run(f"ls {Q(mp)} >/dev/null 2>&1", timeout=20)
            # Then check if still actually mounted
            mnt_check = host_run(f"findmnt -n -o TARGET {Q(mp)} 2>/dev/null", timeout=10)
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
    from blueprints import storage_health as _sh
    sio.start_background_task(_sh._health_monitor_loop)
    sio.start_background_task(_sh._auto_mount_network_drives)


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
        gevent.sleep(10)  # was 3s — 10s sufficient for USB hot-plug detection
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
# Drives cache (shared across storage_drives, storage_filesystem, storage_health)
# ---------------------------------------------------------------------------

_drives_cache = {'data': None, 'ts': 0}
_DRIVES_CACHE_TTL = 10  # seconds


def _invalidate_drives_cache():
    _drives_cache['ts'] = 0


# ---------------------------------------------------------------------------
# Load sub-modules to register routes on storage_bp
# ---------------------------------------------------------------------------

from blueprints import storage_drives, storage_sharing, storage_filesystem, storage_health  # noqa: E402, F401
