"""
EthOS — USB Flasher Blueprint
Create bootable USB drives from ISO/IMG images.
Uses dd with progress tracking, streamed via SSE.
"""

import json
import os
import re
import signal
import subprocess
import threading
_HELPER = '/opt/ethos/tools/ethos-system-helper.sh'
import time
from flask import Blueprint, jsonify, request, Response, stream_with_context

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, host_run_stream_raw, host_path, browse_roots, data_path, q, app_path
from utils import is_pid_alive, load_json as _load_json, save_json as _save_json

flasher_bp = Blueprint('flasher', __name__, url_prefix='/api/flasher')

# ---------------------------------------------------------------------------
# Flash State (persistent across reconnects / logouts)
# ---------------------------------------------------------------------------
_FLASH_STATE_FILE = data_path('flasher_state.json')
_MAX_STATE_LOGS = 200

_flash_state = {
    'status': 'idle',       # idle | flashing | done | error
    'percent': 0,
    'message': '',
    'image': '',
    'disk': '',
    'logs': [],
    'start_time': 0,
    'pid': 0,
    'result': None,         # {success, message} on completion
    'speed': 0,             # MB/s
    'eta': None,            # seconds remaining
    'elapsed': 0,           # seconds elapsed
    'bytes_written': 0,
    'total_bytes': 0,
    'verify': False,        # whether to verify after write
}
_flash_lock = threading.Lock()
_flash_thread = None  # reference to the active worker thread


def _save_flash_state():
    try:
        _save_json(_FLASH_STATE_FILE, _flash_state)
    except Exception:
        pass


def _load_flash_state():
    global _flash_state
    try:
        saved = _load_json(_FLASH_STATE_FILE, None)
        if saved is None:
            return
        _flash_state.update(saved)
        # If was flashing but process died or service restarted → mark as error
        if _flash_state['status'] == 'flashing':
            pid = _flash_state.get('pid', 0)
            if not pid or not is_pid_alive(pid):
                _flash_state['status'] = 'error'
                _flash_state['message'] = 'Flash process interrupted (service restart)'
                _flash_state['result'] = {'success': False, 'message': _flash_state['message']}
                _save_flash_state()
    except Exception:
        pass


def _is_pid_alive(pid):
    return is_pid_alive(pid)


def _update_flash(**kw):
    with _flash_lock:
        _flash_state.update(kw)
        if 'log_line' in kw:
            line = kw.pop('log_line')
            _flash_state.pop('log_line', None)
            _flash_state['logs'].append(line)
            if len(_flash_state['logs']) > _MAX_STATE_LOGS:
                _flash_state['logs'] = _flash_state['logs'][-_MAX_STATE_LOGS:]
        _save_flash_state()


def _reset_flash():
    with _flash_lock:
        _flash_state.update({
            'status': 'idle', 'percent': 0, 'message': '', 'image': '',
            'disk': '', 'logs': [], 'start_time': 0, 'pid': 0, 'result': None,
            'speed': 0, 'eta': None, 'elapsed': 0, 'bytes_written': 0,
            'total_bytes': 0, 'verify': False,
        })
        _save_flash_state()


_load_flash_state()

# ---------------------------------------------------------------------------
# Flash History
# ---------------------------------------------------------------------------
_FLASH_HISTORY_FILE = data_path('flasher_history.json')


def _save_flash_history(entry):
    """Append an entry to flash history (keep last 50)."""
    try:
        history = []
        if os.path.exists(_FLASH_HISTORY_FILE):
            with open(_FLASH_HISTORY_FILE) as f:
                history = json.load(f)
        history.append(entry)
        history = history[-50:]
        with open(_FLASH_HISTORY_FILE, 'w') as f:
            json.dump(history, f)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_host_run = host_run
_host_run_stream = host_run_stream_raw
_q = q


# ---------------------------------------------------------------------------
# API — Flash status (reconnect after logout / refresh)
# ---------------------------------------------------------------------------

@flasher_bp.route('/status')
def flash_status():
    """Return current flash state for reconnect after logout."""
    # Detect dead worker thread while status still says 'flashing'
    with _flash_lock:
        if _flash_state['status'] == 'flashing':
            thread_dead = _flash_thread is None or not _flash_thread.is_alive()
            pid_dead = not _is_pid_alive(_flash_state.get('pid', 0))
            if thread_dead and pid_dead:
                _flash_state['status'] = 'error'
                _flash_state['message'] = 'Flash process unexpectedly terminated'
                _flash_state['result'] = {'success': False, 'message': _flash_state['message']}
                _save_flash_state()

    try:
        since = int(request.args.get('since', 0))
    except (ValueError, TypeError):
        since = 0
    with _flash_lock:
        out = dict(_flash_state)
        out['logs'] = _flash_state['logs'][since:]
        out['log_offset'] = since
        out['log_total'] = len(_flash_state['logs'])
    return jsonify(out)


# ---------------------------------------------------------------------------
# API — List USB drives suitable for flashing
# ---------------------------------------------------------------------------

@flasher_bp.route('/drives')
def list_usb_drives():
    """List removable USB drives (whole disks, not partitions)."""
    r = _host_run(
        "sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -J -o NAME,SIZE,TYPE,TRAN,HOTPLUG,MODEL,LABEL,MOUNTPOINT,RM 2>/dev/null"
    )
    if r.returncode != 0:
        return jsonify({'error': 'Cannot read disk list'}), 500

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return jsonify({'error': 'lsblk parse error'}), 500

    drives = []
    for dev in data.get('blockdevices', []):
        dtype = dev.get('type', '')
        tran = dev.get('tran', '') or ''
        hotplug = dev.get('hotplug') or dev.get('rm')
        is_usb = tran == 'usb' or hotplug in (True, '1', 1)

        if dtype != 'disk' or not is_usb:
            continue

        # Check size in bytes for display
        size_r = _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh blockdev --getsize64 /dev/{dev['name']} 2>/dev/null", timeout=5)
        size_bytes = int(size_r.stdout.strip()) if size_r.returncode == 0 and size_r.stdout.strip().isdigit() else 0

        # Gather partition info
        children = dev.get('children', [])
        partitions = []
        has_mounted = False
        for ch in children:
            mp = ch.get('mountpoint', '')
            if mp:
                has_mounted = True
            partitions.append({
                'name': ch.get('name', ''),
                'label': ch.get('label', ''),
                'mountpoint': mp or None,
                'size': ch.get('size', ''),
            })

        drives.append({
            'name': dev['name'],
            'size': dev.get('size', ''),
            'size_bytes': size_bytes,
            'model': (dev.get('model') or '').strip(),
            'label': dev.get('label') or (children[0].get('label', '') if children else ''),
            'has_mounted': has_mounted,
            'partitions': partitions,
        })

    return jsonify({'drives': drives})


# ---------------------------------------------------------------------------
# API — List ISO/IMG files found on the system
# ---------------------------------------------------------------------------

@flasher_bp.route('/images')
def list_images():
    """Search for .iso and .img files in common locations."""
    search_paths = browse_roots()
    
    # Add EthOS builder output directories
    search_paths.append(app_path('installer/images'))
    results = []
    seen = set()

    for base in search_paths:
        if not os.path.isdir(base):
            continue
        try:
            for root, dirs, files in os.walk(base, followlinks=False):
                # Skip hidden and system directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                # Limit depth to 6 levels
                depth = root.replace(base, '').count(os.sep)
                if depth > 6:
                    dirs.clear()
                    continue
                for f in files:
                    low = f.lower()
                    if any(low.endswith(ext) for ext in (
                        '.iso', '.img', '.iso.gz', '.img.gz',
                        '.iso.xz', '.img.xz', '.img.zst',
                    )):
                        full = os.path.join(root, f)
                        if full in seen:
                            continue
                        seen.add(full)
                        try:
                            stat = os.stat(full)
                            # Convert path to user-visible form
                            user_path = host_path(full)
                            results.append({
                                'path': full,         # container path (for server use)
                                'display': user_path,  # user-visible path
                                'name': f,
                                'size': stat.st_size,
                                'modified': stat.st_mtime,
                            })
                        except OSError:
                            pass
        except PermissionError:
            pass

    results.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify({'images': results})


# ---------------------------------------------------------------------------
# API — Verify image file
# ---------------------------------------------------------------------------

@flasher_bp.route('/verify')
def verify_image():
    """Quick check: does the image file exist and is it readable?"""
    path = request.args.get('path', '')
    if not path or not os.path.isfile(path):
        return jsonify({'valid': False, 'error': 'File not found'}), 404

    size = os.path.getsize(path)
    # Check if it looks like a valid image (> 1MB)
    if size < 1024 * 1024:
        return jsonify({'valid': False, 'error': 'File too small — probably not an image'}), 400

    # Detect compression
    low = path.lower()
    compressed = None
    if low.endswith('.gz'):
        compressed = 'gzip'
    elif low.endswith('.xz'):
        compressed = 'xz'
    elif low.endswith('.zst'):
        compressed = 'zstd'

    # Try to detect image type
    img_type = 'unknown'
    try:
        with open(path, 'rb') as f:
            header = f.read(32768)
            if compressed == 'gzip' and header[:2] == b'\x1f\x8b':
                img_type = 'gzip-compressed'
            elif compressed == 'xz' and header[:6] == b'\xfd7zXZ\x00':
                img_type = 'xz-compressed'
            elif compressed == 'zstd' and header[:4] == b'\x28\xb5\x2f\xfd':
                img_type = 'zstd-compressed'
            elif b'CD001' in header:
                img_type = 'iso9660'
            elif header[:2] == b'MZ':
                img_type = 'uefi-img'
            elif header[510:512] == b'\x55\xaa':
                img_type = 'mbr-img'
            elif header[:8] == b'\x00\x00\x00\x00\x00\x00\x00\x00':
                img_type = 'raw-img'
            else:
                img_type = 'raw-img'
    except Exception:
        pass

    return jsonify({
        'valid': True,
        'path': path,
        'size': size,
        'type': img_type,
        'compressed': compressed,
    })


# ---------------------------------------------------------------------------
# API — Flash image to USB (SSE stream)
# ---------------------------------------------------------------------------

@flasher_bp.route('/flash', methods=['POST'])
def flash_drive():
    """Flash an ISO/IMG to a USB drive. Runs in background thread."""
    data = request.json or {}
    image_path = data.get('image', '').strip()
    target_disk = data.get('disk', '').strip()

    if not image_path:
        return jsonify({'error': 'Image path not specified'}), 400
    if not target_disk:
        return jsonify({'error': 'Target disk not selected'}), 400

    # Validate disk name
    if not re.match(r'^[a-zA-Z0-9]+$', target_disk):
        return jsonify({'error': 'Invalid disk name'}), 400

    # Verify image exists
    if not os.path.isfile(image_path):
        return jsonify({'error': f'Image does not exist: {image_path}'}), 404

    image_size = os.path.getsize(image_path)

    # Verify target is a USB disk
    r = _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -ndo TRAN,HOTPLUG,TYPE /dev/{target_disk} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({'error': f'/dev/{target_disk} does not exist'}), 404

    parts = r.stdout.strip().split()
    tran = parts[0] if parts else ''
    hotplug = parts[1] if len(parts) > 1 else '0'
    dtype = parts[2] if len(parts) > 2 else ''

    if dtype != 'disk':
        return jsonify({'error': 'Select the whole disk, not a partition'}), 400
    if tran != 'usb' and hotplug != '1':
        return jsonify({'error': 'Not a USB device — write refused'}), 400

    # Protect system disk
    mount_check = _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -nlo MOUNTPOINT /dev/{target_disk} 2>/dev/null")
    if mount_check.returncode == 0:
        mounts = [m.strip() for m in mount_check.stdout.strip().splitlines() if m.strip()]
        for mp in mounts:
            if mp in ('/', '/boot', '/boot/efi', '/home'):
                return jsonify({'error': f'Disk contains system partition ({mp}) — write refused!'}), 400

    # Check disk size
    size_r = _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh blockdev --getsize64 /dev/{target_disk} 2>/dev/null")
    disk_size = int(size_r.stdout.strip()) if size_r.returncode == 0 and size_r.stdout.strip().isdigit() else 0

    if disk_size > 0 and image_size > disk_size:
        return jsonify({'error': f'Image ({image_size // (1024**2)} MB) is larger than disk ({disk_size // (1024**2)} MB)'}), 400

    # Reject if already flashing
    with _flash_lock:
        if _flash_state['status'] == 'flashing':
            return jsonify({'error': 'Flash already in progress'}), 409

    # Translate container path to host path for dd
    host_image_path = host_path(image_path)

    verify_after = data.get('verify', False)
    compressed = data.get('compressed', None)

    _update_flash(status='flashing', percent=0, message='Rozpoczynanie...', image=image_path,
                  disk=target_disk, logs=[], start_time=time.time(), pid=0, result=None,
                  verify=verify_after, speed=0, eta=None, elapsed=0, bytes_written=0,
                  total_bytes=image_size)

    # Run in background thread
    global _flash_thread
    t = threading.Thread(target=_flash_worker,
                         args=(host_image_path, target_disk, image_path, image_size, verify_after, compressed),
                         daemon=True)
    _flash_thread = t
    t.start()

    return jsonify({'ok': True})


def _flash_worker(host_image_path, target_disk, image_path, image_size, verify_after=False, compressed=None):
    """Background worker that performs the actual flash operation."""
    try:
        # Step 1: Unmount all partitions on the target disk
        msg = f'Unmounting partitions on /dev/{target_disk}...'
        _update_flash(percent=0, message=msg, log_line=msg)

        umount_r = _host_run(
            f"sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -nlo NAME,MOUNTPOINT /dev/{target_disk} 2>/dev/null"
        )
        if umount_r.returncode == 0:
            for line in umount_r.stdout.strip().splitlines():
                cols = line.split(None, 1)
                if len(cols) >= 2 and cols[1].strip():
                    mp = cols[1].strip()
                    _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh umount {_q(mp)} 2>/dev/null")
                    _update_flash(log_line=f'Unmounted {mp}')

        # Step 2: Write image with dd (no status=progress — we track via /proc)
        msg2 = 'Writing image to USB drive...'
        _update_flash(percent=1, message=msg2, log_line=msg2)

        bs = '4M'
        # Use oflag=direct to bypass OS page cache — writes go straight to device
        if compressed == 'gzip':
            dd_cmd = f"sudo {_HELPER} write-image {_q(host_image_path)} /dev/{target_disk} 2>&1"
        elif compressed == 'xz':
            dd_cmd = f"sudo {_HELPER} write-image {_q(host_image_path)} /dev/{target_disk} 2>&1"
        elif compressed == 'zstd':
            dd_cmd = f"sudo {_HELPER} write-image {_q(host_image_path)} /dev/{target_disk} 2>&1"
        else:
            dd_cmd = f"sudo {_HELPER} write-image {_q(host_image_path)} /dev/{target_disk} 2>&1"

        start_time = time.time()

        # Launch dd as a subprocess (don't use streaming — pipe buffering issues)
        full_cmd = f"bash -c {_q(dd_cmd)}"
        proc = subprocess.Popen(full_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _update_flash(pid=proc.pid)

        # Find the actual dd child PID for /proc monitoring
        dd_pid = _find_dd_child(proc.pid)

        # Monitor progress by polling /proc/<dd_pid>/fdinfo/0 (read position)
        last_percent = 0
        while proc.poll() is None:
            time.sleep(1)
            bytes_written = _get_dd_progress(dd_pid, target_disk)
            elapsed = time.time() - start_time
            effective_size = image_size if not compressed else max(image_size, bytes_written * 2)

            if bytes_written > 0 and effective_size > 0:
                pct = min(94, int(bytes_written / effective_size * 94))
                speed = bytes_written / (1024 * 1024 * max(elapsed, 0.1))
                eta = (effective_size - bytes_written) / (bytes_written / max(elapsed, 0.1)) if bytes_written > 0 else None
                if pct > last_percent:
                    last_percent = pct
                    prog_msg = f'{bytes_written // (1024**2)} / {image_size // (1024**2)} MB  ({speed:.1f} MB/s)'
                    _update_flash(percent=pct, message=prog_msg, speed=round(speed, 1),
                                  eta=round(eta) if eta else None, elapsed=round(elapsed),
                                  bytes_written=bytes_written, total_bytes=image_size)
            elif elapsed > 2:
                _update_flash(elapsed=round(elapsed))

        # dd finished — read remaining output
        remaining = proc.stdout.read().decode('utf-8', errors='replace').strip() if proc.stdout else ''
        if remaining:
            for out_line in remaining.splitlines()[-5:]:
                if out_line.strip():
                    _update_flash(log_line=out_line.strip())

        code = proc.returncode
        if code == 0:
            _update_flash(percent=95, message='Synchronizacja...', log_line='Synchronizacja...')
            _host_run('sync', timeout=60)
            # Flush device write cache (critical for USB card readers)
            _host_run(f'sudo /opt/ethos/tools/ethos-system-helper.sh blockdev --flushbufs /dev/{target_disk} 2>/dev/null', timeout=30)
            _host_run(f'hdparm -F /dev/{target_disk} 2>/dev/null', timeout=10)

            # Re-unmount: automounters (devmon/udisks) may have mounted partitions after dd
            _host_run(f"for mp in $(sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -nlo MOUNTPOINT /dev/{target_disk} 2>/dev/null | grep .); do sudo /opt/ethos/tools/ethos-system-helper.sh umount \"$mp\" 2>/dev/null; done", timeout=15)

            # Step 3: Optional data verification (BEFORE partprobe which may alter GPT)
            if verify_after and not compressed:
                _update_flash(percent=96, message='Verifying write...', log_line='Verifying write...')
                # Drop page cache so reads get fresh data from disk (not stale pre-dd cache)
                _host_run('echo 1 > /proc/sys/vm/drop_caches 2>/dev/null', timeout=10)
                # Compare checksums of a chunk from the middle of the image
                chunk_offset = min(32 * 1024 * 1024, image_size // 4)  # 32MB or 25% in
                chunk_size = min(64 * 1024 * 1024, image_size - chunk_offset)  # 64MB chunk
                if chunk_size > 0:
                    skip_mb = chunk_offset // (1024 * 1024)
                    count_mb = chunk_size // (1024 * 1024)
                    img_hash_cmd = f"dd if={_q(host_image_path)} bs=1M skip={skip_mb} count={count_mb} 2>/dev/null | sha256sum"
                    disk_hash_cmd = f"dd if=/dev/{target_disk} bs=1M skip={skip_mb} count={count_mb} iflag=direct 2>/dev/null | sha256sum"
                    img_r = _host_run(img_hash_cmd, timeout=120)
                    disk_r = _host_run(disk_hash_cmd, timeout=120)
                    img_hash = img_r.stdout.strip().split()[0] if img_r.returncode == 0 and img_r.stdout else ''
                    disk_hash = disk_r.stdout.strip().split()[0] if disk_r.returncode == 0 and disk_r.stdout else ''
                    if img_hash and img_hash == disk_hash:
                        _update_flash(log_line=f'Verification OK — SHA256 {count_mb} MB @ offset {skip_mb} MB match')
                    elif not img_hash or not disk_hash:
                        _update_flash(log_line='Verification skipped — failed to calculate checksum')
                    else:
                        err = f'SHA256 mismatch @ offset {skip_mb} MB: image={img_hash[:16]}… disk={disk_hash[:16]}…'
                        _update_flash(status='error', message=f'Verification FAILED: {err}',
                                      result={'success': False, 'message': f'Verification failed: {err}'})
                        elapsed = time.time() - start_time
                        _save_flash_history({
                            'image': image_path, 'disk': target_disk, 'success': False,
                            'message': f'Verification failed: {err}',
                            'timestamp': time.time(), 'size': image_size, 'elapsed': round(elapsed),
                        })
                        return

            # Step 4: Verify partition table
            _update_flash(percent=98, message='Verifying partition table...', log_line='Verifying...')
            verify_r = _host_run(f"partprobe /dev/{target_disk} 2>&1; fdisk -l /dev/{target_disk} 2>&1 | head -5")
            verify_msg = verify_r.stdout.strip() if verify_r.stdout else ''
            if verify_msg:
                _update_flash(log_line=verify_msg)

            elapsed = time.time() - start_time
            speed = (image_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            done_msg = f'Done! {image_size // (1024**2)} MB written in {elapsed:.0f}s ({speed:.1f} MB/s)'
            _update_flash(log_line=done_msg)

            # Safe eject — ensure USB controller flushes internal cache
            _update_flash(percent=99, message='Safely ejecting...', log_line='Safely ejecting device...')
            _host_run(f'udisksctl power-off -b /dev/{target_disk} 2>/dev/null', timeout=15)
            _host_run(f'eject /dev/{target_disk} 2>/dev/null', timeout=10)

            _update_flash(status='done', percent=100, message=done_msg,
                          elapsed=round(elapsed), speed=round(speed, 1),
                          result={'success': True, 'message': done_msg})
            _save_flash_history({
                'image': image_path, 'disk': target_disk, 'success': True,
                'message': done_msg, 'timestamp': time.time(),
                'size': image_size, 'elapsed': round(elapsed),
            })
        else:
            err_msg = f'dd failed (code: {code})'
            if remaining:
                err_msg += f' — {remaining[:200]}'
            elapsed = time.time() - start_time
            _update_flash(status='error', percent=0, message=err_msg,
                          result={'success': False, 'message': err_msg})
            _save_flash_history({
                'image': image_path, 'disk': target_disk, 'success': False,
                'message': err_msg, 'timestamp': time.time(),
                'size': image_size, 'elapsed': round(elapsed),
            })
    except Exception as exc:
        err_msg = f'Unexpected error: {exc}'
        elapsed = time.time() - start_time if 'start_time' in dir() else 0
        _update_flash(status='error', percent=0, message=err_msg,
                      result={'success': False, 'message': err_msg})
        _save_flash_history({
            'image': image_path, 'disk': target_disk, 'success': False,
            'message': err_msg, 'timestamp': time.time(),
            'size': image_size, 'elapsed': round(elapsed) if isinstance(elapsed, float) else 0,
        })


def _find_dd_child(parent_pid):
    """Find the actual dd process PID among children of parent_pid."""
    for _ in range(20):  # retry for up to 2 seconds
        try:
            children_dir = f'/proc/{parent_pid}/task/{parent_pid}/children'
            if os.path.exists(children_dir):
                with open(children_dir) as f:
                    child_pids = f.read().strip().split()
                for cpid in child_pids:
                    try:
                        with open(f'/proc/{cpid}/comm') as f:
                            comm = f.read().strip()
                        if comm == 'dd':
                            return int(cpid)
                        # Check grandchildren (dd may be under bash)
                        gc_path = f'/proc/{cpid}/task/{cpid}/children'
                        if os.path.exists(gc_path):
                            with open(gc_path) as f:
                                for gcpid in f.read().strip().split():
                                    with open(f'/proc/{gcpid}/comm') as f2:
                                        if f2.read().strip() == 'dd':
                                            return int(gcpid)
                    except (OSError, ValueError):
                        continue
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    return 0


def _get_dd_progress(dd_pid, target_disk):
    """Get bytes written by dd via /proc fdinfo or disk write stats."""
    if dd_pid:
        try:
            # Read position of output fd (fd 1 for stdout-of-dd = the disk)
            with open(f'/proc/{dd_pid}/fdinfo/1') as f:
                for line in f:
                    if line.startswith('pos:'):
                        return int(line.split(':')[1].strip())
        except (OSError, ValueError):
            pass
    # Fallback: check disk write stats via /sys
    try:
        with open(f'/sys/block/{target_disk}/stat') as f:
            parts = f.read().split()
            # Field index 6 = sectors written, sector = 512 bytes
            if len(parts) > 6:
                return int(parts[6]) * 512
    except (OSError, ValueError, IndexError):
        pass
    return 0


@flasher_bp.route('/dismiss', methods=['POST'])
def dismiss_flash():
    """Clear flash state (dismiss error/done)."""
    _reset_flash()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# API — Cancel flash
# ---------------------------------------------------------------------------

@flasher_bp.route('/cancel', methods=['POST'])
def cancel_flash():
    """Kill the running dd process."""
    with _flash_lock:
        if _flash_state['status'] != 'flashing':
            return jsonify({'error': 'No active flash in progress'}), 400
        pid = _flash_state.get('pid', 0)
    if pid and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        except OSError:
            pass
    _update_flash(status='error', message='Flash cancelled by user',
                  result={'success': False, 'message': 'Cancelled'})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# API — SHA256 checksum
# ---------------------------------------------------------------------------

@flasher_bp.route('/checksum', methods=['POST'])
def start_checksum():
    """Compute SHA256 of an image file using system sha256sum."""
    data = request.json or {}
    path = data.get('path', '')
    if not path or not os.path.isfile(path):
        return jsonify({'error': 'File not found'}), 404
    # Security: only allow files under allowed browse roots
    real = os.path.realpath(path)
    roots = browse_roots()
    if not any(real.startswith(os.path.realpath(r) + '/') or real == os.path.realpath(r) for r in roots):
        return jsonify({'error': 'Path not allowed'}), 403
    size = os.path.getsize(real)
    if size > 10 * 1024**3:
        return jsonify({'error': 'File too large to calculate checksum'}), 400
    r = _host_run(f"sha256sum {_q(real)} 2>/dev/null", timeout=600)
    if r.returncode == 0 and r.stdout.strip():
        sha = r.stdout.strip().split()[0]
        return jsonify({'sha256': sha, 'path': path})
    return jsonify({'error': 'Failed to calculate checksum'}), 500


# ---------------------------------------------------------------------------
# API — Format USB drive
# ---------------------------------------------------------------------------

@flasher_bp.route('/format', methods=['POST'])
def format_drive():
    """Format a USB drive with specified filesystem."""
    data = request.json or {}
    disk = data.get('disk', '').strip()
    fs_type = data.get('fs_type', 'exfat').strip()
    label = data.get('label', 'USB').strip()[:16]

    if not disk or not re.match(r'^[a-zA-Z0-9]+$', disk):
        return jsonify({'error': 'Invalid disk name'}), 400
    if fs_type not in ('fat32', 'exfat', 'ext4', 'ntfs', 'wipe'):
        return jsonify({'error': 'Unsupported filesystem'}), 400

    # Verify USB
    r = _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -ndo TRAN,HOTPLUG,TYPE /dev/{disk} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({'error': f'/dev/{disk} does not exist'}), 404
    parts = r.stdout.strip().split()
    tran = parts[0] if parts else ''
    hotplug = parts[1] if len(parts) > 1 else '0'
    if tran != 'usb' and hotplug != '1':
        return jsonify({'error': 'Not a USB device'}), 400

    # System disk protection
    mount_check = _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -nlo MOUNTPOINT /dev/{disk} 2>/dev/null")
    if mount_check.returncode == 0:
        mounts = [m.strip() for m in mount_check.stdout.strip().splitlines() if m.strip()]
        for mp in mounts:
            if mp in ('/', '/boot', '/boot/efi', '/home'):
                return jsonify({'error': f'Disk contains system partition ({mp})!'}), 400

    # Unmount all partitions
    umount_r = _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -nlo NAME,MOUNTPOINT /dev/{disk} 2>/dev/null")
    if umount_r.returncode == 0:
        for line in umount_r.stdout.strip().splitlines():
            cols = line.split(None, 1)
            if len(cols) >= 2 and cols[1].strip():
                _host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh umount {_q(cols[1].strip())} 2>/dev/null")

    # Wipe and create new partition table + single partition
    part_name = f"{disk}1"
    cmds = [
        f"wipefs -a /dev/{disk} 2>&1",
        f"parted -s /dev/{disk} mklabel gpt mkpart primary 1MiB 100% 2>&1",
        "sleep 1",
        f"partprobe /dev/{disk} 2>&1",
        "sleep 2",
        # Re-unmount: automounters may have mounted the new partition
        f"for mp in $(sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -nlo MOUNTPOINT /dev/{disk} 2>/dev/null | grep .); do sudo /opt/ethos/tools/ethos-system-helper.sh umount \"$mp\" 2>/dev/null; done",
        "sleep 1",
    ]
    if fs_type == 'fat32':
        cmds.append(f"mkfs.vfat -F 32 -n {_q(label)} /dev/{part_name} 2>&1")
    elif fs_type == 'exfat':
        cmds.append(f"mkfs.exfat -n {_q(label)} /dev/{part_name} 2>&1")
    elif fs_type == 'ext4':
        cmds.append(f"mkfs.ext4 -L {_q(label)} -F /dev/{part_name} 2>&1")
    elif fs_type == 'ntfs':
        cmds.append(f"mkfs.ntfs -f -L {_q(label)} /dev/{part_name} 2>&1")
    elif fs_type == 'wipe':
        # Remove keepalive entries for this disk (prevent interference)
        try:
            ka_path = data_path('keepalive.json')
            if os.path.exists(ka_path):
                with open(ka_path) as f:
                    ka = json.load(f)
                changed = False
                for key in list(ka.keys()):
                    if ka[key].get('disk') == disk or key.startswith(disk):
                        del ka[key]
                        changed = True
                if changed:
                    with open(ka_path, 'w') as f:
                        json.dump(ka, f, indent=2)
        except Exception:
            pass

        # Full wipe — stop automounter, zero partition table, verify
        wipe_script = (
            f"exec 2>&1; "
            # Stop automounter to prevent race conditions
            f"systemctl stop devmon@devmon.service 2>/dev/null || true; "
            f"sudo /opt/ethos/tools/ethos-system-helper.sh umount /dev/{disk}?* 2>/dev/null || true; "
            f"sudo /opt/ethos/tools/ethos-system-helper.sh umount -l /dev/{disk}?* 2>/dev/null || true; "
            f"sleep 1; "
            # Wipe filesystem signatures
            f"for p in /dev/{disk}[0-9]*; do wipefs -af \"$p\" 2>/dev/null; done; "
            f"wipefs -af /dev/{disk} 2>/dev/null; "
            # Zero first and last 10MB with O_DIRECT+O_SYNC
            f"dd if=/dev/zero of=/dev/{disk} bs=1M count=10 oflag=direct,sync 2>&1; "
            f"SZ=$(sudo /opt/ethos/tools/ethos-system-helper.sh blockdev --getsize64 /dev/{disk} 2>/dev/null); "
            f"dd if=/dev/zero of=/dev/{disk} bs=1M seek=$(( $SZ / 1048576 - 10 )) count=10 oflag=direct,sync 2>&1; "
            f"sync; sudo /opt/ethos/tools/ethos-system-helper.sh blockdev --flushbufs /dev/{disk} 2>/dev/null || true; "
            # Drop all caches, verify write reached physical media
            f"echo 3 > /proc/sys/vm/drop_caches; "
            f"FIRST=$(dd if=/dev/{disk} bs=512 count=1 iflag=direct 2>/dev/null | od -A n -t x1 -N 4 | tr -d ' \\n'); "
            f"echo \"verify:$FIRST\"; "
            # Check dmesg for I/O errors on this device
            f"IOERR=$(dmesg | tail -50 | grep -c 'I/O error, dev {disk}' 2>/dev/null || echo 0); "
            f"echo \"ioerr:$IOERR\"; "
            # Remove partitions from kernel
            f"partx -d /dev/{disk} 2>/dev/null || true; "
            f"sudo /opt/ethos/tools/ethos-system-helper.sh blockdev --rereadpt /dev/{disk} 2>/dev/null || true; "
            # Restart automounter
            f"systemctl start devmon@devmon.service 2>/dev/null || true; "
            f"echo WIPE_DONE"
        )
        result = _host_run(wipe_script, timeout=120)
        output = (result.stdout or '') + (result.stderr or '')
        if 'WIPE_DONE' not in output:
            return jsonify({'error': f'Wipe error: {output.strip()[-500:]}'}), 500

        # Check if writes actually reached the physical media
        import re as _re
        verify_m = _re.search(r'verify:(\S+)', output)
        ioerr_m = _re.search(r'ioerr:(\d+)', output)
        first_bytes = verify_m.group(1) if verify_m else ''
        io_errors = int(ioerr_m.group(1)) if ioerr_m else 0

        if first_bytes != '00000000' or io_errors > 0:
            msg = 'Disk rejects writes (I/O errors in dmesg). '
            if io_errors > 0:
                msg += f'Found {io_errors} I/O errors. '
            msg += 'Check: (1) SD card write lock, (2) damaged card, (3) faulty USB reader.'
            return jsonify({'error': msg}), 500

        return jsonify({'status': 'ok'})

    full_cmd = ' && '.join(cmds)
    result = _host_run(full_cmd, timeout=120)

    if result.returncode == 0:
        msg = 'Disk wiped (no partitions)' if fs_type == 'wipe' else f'Disk formatted as {fs_type.upper()} ({label})'
        return jsonify({'status': 'ok', 'disk_type': fs_type, 'label': label if fs_type != 'wipe' else None})
    else:
        return jsonify({'error': f'Format error: {(result.stdout or "").strip()[-200:]}'}), 500


# ---------------------------------------------------------------------------
# API — Flash history
# ---------------------------------------------------------------------------

@flasher_bp.route('/history')
def flash_history():
    """Return flash history."""
    try:
        if os.path.exists(_FLASH_HISTORY_FILE):
            with open(_FLASH_HISTORY_FILE) as f:
                return jsonify(json.load(f))
    except Exception:
        pass
    return jsonify([])


# ── Package: install / uninstall / status ──

@flasher_bp.route('/install', methods=['POST'])
def install_flasher():
    return jsonify({'status': 'ok'})


@flasher_bp.route('/uninstall', methods=['POST'])
def uninstall_flasher():
    wipe = (request.json or {}).get('wipe_data', False)

    # Kill running flash process
    with _flash_lock:
        if _flash_state['status'] == 'flashing':
            pid = _flash_state.get('pid', 0)
            if pid and pid > 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(1)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                except OSError:
                    pass
            _reset_flash()

    if wipe:
        try:
            if os.path.isfile(_FLASH_STATE_FILE):
                os.remove(_FLASH_STATE_FILE)
        except Exception:
            pass
    return jsonify({'ok': True})


@flasher_bp.route('/pkg-status', methods=['GET'])
def flasher_pkg_status():
    return jsonify({'installed': True})
