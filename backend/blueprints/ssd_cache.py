"""
EthOS — SSD Cache Manager Blueprint
Uses bcache to attach SSD write-through/write-back caches to HDD pools.

Endpoints:
  GET  /api/cache/status           — Status of all bcache caches
  GET  /api/cache/devices          — List SSDs and HDDs available for caching
  POST /api/cache/create           — Attach SSD cache to HDD backing device
  POST /api/cache/detach           — Detach SSD cache (flush first)
  PUT  /api/cache/mode             — Switch cache mode (writethrough/writeback/writearound)
  GET  /api/cache/stats            — Live bcache statistics (hit rate, dirty data, etc.)
"""

import json
import os
import re
import sys
import time

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, data_path, q, apt_install
from blueprints.admin_required import admin_required

ssd_cache_bp = Blueprint('ssd_cache', __name__, url_prefix='/api/cache')

_STATE_FILE = data_path('bcache_state.json')
_DEV_RE = re.compile(r'^/dev/[a-zA-Z0-9/_-]+$')


# ─── Helpers ──────────────────────────────────────────────────────────────

def _ensure_bcache():
    """Install bcache-tools if not present, load kernel module."""
    r = host_run("command -v bcache-super-show")
    if r.returncode != 0:
        apt_install('bcache-tools', timeout=120)
        r = host_run("command -v bcache-super-show")
        if r.returncode != 0:
            return False
    host_run("modprobe -q bcache 2>/dev/null")
    return True


def _safe_device(device):
    if not device:
        return None
    device = device.strip()
    if not _DEV_RE.match(device) or '..' in device:
        return None
    r = host_run(f"test -b {q(device)}")
    return device if r.returncode == 0 else None


def _load_state():
    if os.path.isfile(_STATE_FILE):
        try:
            with open(_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {'caches': []}


def _save_state(state):
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _find_root_device():
    """Find the root disk to exclude from caching."""
    r = host_run("findmnt -n -o SOURCE /")
    if r.returncode != 0:
        return None
    src = r.stdout.strip()
    r2 = host_run(f"lsblk -no PKNAME {q(src)} 2>/dev/null")
    if r2.returncode == 0 and r2.stdout.strip():
        return r2.stdout.strip()
    return re.sub(r'[0-9]+$', '', src.split('/')[-1])


def _read_bcache_stat(bcache_path, stat_name):
    """Read a bcache sysfs stat."""
    fpath = os.path.join(bcache_path, stat_name)
    try:
        with open(fpath) as f:
            return f.read().strip()
    except Exception:
        return None


def _get_bcache_uuid(device):
    """Get bcache UUID for a device."""
    r = host_run(f"bcache-super-show {q(device)} 2>/dev/null")
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if 'cset.uuid' in line:
            parts = line.split()
            if len(parts) >= 2:
                return parts[-1]
    return None


def _live_bcache_info():
    """Gather live bcache info from sysfs."""
    caches = []
    bcache_base = '/sys/fs/bcache'
    if not os.path.isdir(bcache_base):
        return caches

    for uuid in os.listdir(bcache_base):
        uuid_path = os.path.join(bcache_base, uuid)
        if not os.path.isdir(uuid_path) or uuid.startswith('.'):
            continue

        info = {'uuid': uuid, 'cache_devices': [], 'backing_devices': []}

        cache_dir = os.path.join(uuid_path, 'cache0')
        if os.path.isdir(cache_dir):
            info['cache_available_percent'] = _read_bcache_stat(cache_dir, 'cache_available_percent')

        for stat in ('average_key_pop', 'btree_cache_size', 'cache_available_percent',
                      'congested', 'root_usage_percent', 'tree_depth'):
            val = _read_bcache_stat(uuid_path, stat)
            if val is not None:
                info[stat] = val

        for blk in os.listdir('/sys/block'):
            bpath = f'/sys/block/{blk}/bcache'
            if os.path.isdir(bpath):
                bdev_uuid_link = os.path.join(bpath, 'cache')
                if os.path.islink(bdev_uuid_link):
                    target = os.path.realpath(bdev_uuid_link)
                    if uuid in target:
                        mode = _read_bcache_stat(bpath, 'cache_mode') or 'unknown'
                        state = _read_bcache_stat(bpath, 'state') or 'unknown'
                        hit_ratio = None
                        stats_total = os.path.join(bpath, 'stats_total')
                        if os.path.isdir(stats_total):
                            hits = _read_bcache_stat(stats_total, 'cache_hits') or '0'
                            misses = _read_bcache_stat(stats_total, 'cache_misses') or '0'
                            total = int(hits) + int(misses)
                            hit_ratio = round(int(hits) / total * 100, 1) if total > 0 else 0

                        dirty = _read_bcache_stat(bpath, 'dirty_data')
                        info['backing_devices'].append({
                            'device': f'/dev/{blk}',
                            'mode': mode.split('[')[-1].rstrip(']') if '[' in mode else mode,
                            'state': state,
                            'hit_ratio': hit_ratio,
                            'dirty_data': dirty,
                        })

        caches.append(info)
    return caches


# ─── Routes ───────────────────────────────────────────────────────────────

@ssd_cache_bp.route('/status', methods=['GET'])
@admin_required
def cache_status():
    """Return live bcache status + saved state."""
    if not _ensure_bcache():
        return jsonify({'ok': False, 'error': 'bcache-tools not available'}), 500

    live = _live_bcache_info()
    state = _load_state()
    return jsonify({'ok': True, 'live': live, 'configured': state.get('caches', [])})


@ssd_cache_bp.route('/devices', methods=['GET'])
@admin_required
def list_devices():
    """List SSDs (potential cache) and HDDs (potential backing) devices."""
    r = host_run(
        "lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,TRAN,ROTA,HOTPLUG 2>/dev/null"
    )
    if r.returncode != 0:
        return jsonify({'error': 'Failed to query devices'}), 500

    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return jsonify({'error': 'Failed to parse device list'}), 500

    root_disk = _find_root_device()
    ssds, hdds = [], []

    for dev in data.get('blockdevices', []):
        if dev.get('type') != 'disk':
            continue
        name = dev.get('name', '')
        if name == root_disk:
            continue
        is_usb = dev.get('hotplug') in (True, '1', 1) or dev.get('tran') == 'usb'
        if is_usb:
            continue

        is_rotational = dev.get('rota') in (True, '1', 1)
        tran = dev.get('tran') or ''
        is_ssd = (not is_rotational) or tran == 'nvme'

        children = dev.get('children', [])
        has_mounted = any(c.get('mountpoint') for c in children)

        entry = {
            'name': name,
            'device': f'/dev/{name}',
            'size': dev.get('size'),
            'model': (dev.get('model') or '').strip(),
            'tran': tran,
            'has_mounted_parts': has_mounted,
            'children': [{
                'name': c.get('name'),
                'size': c.get('size'),
                'fstype': c.get('fstype'),
                'mountpoint': c.get('mountpoint'),
            } for c in children]
        }

        if is_ssd:
            ssds.append(entry)
        else:
            hdds.append(entry)

    return jsonify({'ok': True, 'ssds': ssds, 'hdds': hdds})


@ssd_cache_bp.route('/create', methods=['POST'])
@admin_required
def create_cache():
    """Attach SSD cache to HDD backing device via bcache."""
    if not _ensure_bcache():
        return jsonify({'error': 'bcache-tools not available'}), 500

    body = request.get_json(silent=True) or {}
    cache_dev = _safe_device(body.get('cache_device'))
    backing_dev = _safe_device(body.get('backing_device'))
    mode = body.get('mode', 'writethrough')

    if not cache_dev:
        return jsonify({'error': 'Invalid cache (SSD) device'}), 400
    if not backing_dev:
        return jsonify({'error': 'Invalid backing (HDD) device'}), 400
    if mode not in ('writethrough', 'writeback', 'writearound', 'none'):
        return jsonify({'error': 'Invalid cache mode'}), 400

    r = host_run(f"findmnt -n {q(cache_dev)} 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        return jsonify({'error': 'Cache device is mounted — unmount first'}), 400
    r = host_run(f"findmnt -n {q(backing_dev)} 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        return jsonify({'error': 'Backing device is mounted — unmount first'}), 400

    host_run(f"wipefs -a {q(cache_dev)} 2>/dev/null", timeout=30)
    host_run(f"wipefs -a {q(backing_dev)} 2>/dev/null", timeout=30)

    r = host_run(f"make-bcache -C {q(cache_dev)} --wipe-bcache 2>&1", timeout=60)
    if r.returncode != 0:
        return jsonify({'error': f'Failed to format cache device: {r.stdout.strip() or r.stderr.strip()}'}), 500

    cache_uuid = _get_bcache_uuid(cache_dev)

    r = host_run(f"make-bcache -B {q(backing_dev)} --wipe-bcache 2>&1", timeout=60)
    if r.returncode != 0:
        return jsonify({'error': f'Failed to format backing device: {r.stdout.strip() or r.stderr.strip()}'}), 500

    time.sleep(2)

    if cache_uuid:
        backing_name = backing_dev.split('/')[-1]
        attach_path = f'/sys/block/{backing_name}/bcache/attach'
        if os.path.exists(attach_path):
            try:
                with open(attach_path, 'w') as f:
                    f.write(cache_uuid)
            except Exception as e:
                return jsonify({'error': f'Failed to attach: {e}'}), 500
        else:
            host_run(f"echo {q(cache_uuid)} > /sys/block/{q(backing_name)}/bcache/attach 2>&1", timeout=10)

    backing_name = backing_dev.split('/')[-1]
    bcache_mode_path = f'/sys/block/{backing_name}/bcache/cache_mode'
    if os.path.exists(bcache_mode_path):
        try:
            with open(bcache_mode_path, 'w') as f:
                f.write(mode)
        except Exception:
            pass

    state = _load_state()
    state['caches'].append({
        'cache_device': cache_dev,
        'backing_device': backing_dev,
        'mode': mode,
        'uuid': cache_uuid,
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
    })
    _save_state(state)

    try:
        from blueprints.eventlog import log as elog
        elog('storage', 'info', f'SSD cache created: {cache_dev} -> {backing_dev} ({mode})')
    except Exception:
        pass

    return jsonify({'ok': True, 'uuid': cache_uuid, 'mode': mode})


@ssd_cache_bp.route('/detach', methods=['POST'])
@admin_required
def detach_cache():
    """Detach SSD cache from backing device, flushing dirty data first."""
    if not _ensure_bcache():
        return jsonify({'error': 'bcache-tools not available'}), 500

    body = request.get_json(silent=True) or {}
    backing_dev = _safe_device(body.get('backing_device'))
    if not backing_dev:
        return jsonify({'error': 'Invalid backing device'}), 400

    backing_name = backing_dev.split('/')[-1]
    bcache_path = f'/sys/block/{backing_name}/bcache'

    if not os.path.isdir(bcache_path):
        return jsonify({'error': 'Device has no bcache configuration'}), 400

    dirty = _read_bcache_stat(bcache_path, 'dirty_data')
    if dirty and dirty != '0.0k' and dirty != '0':
        mode_path = os.path.join(bcache_path, 'cache_mode')
        if os.path.exists(mode_path):
            try:
                with open(mode_path, 'w') as f:
                    f.write('writethrough')
            except Exception:
                pass
        time.sleep(3)

    detach_path = os.path.join(bcache_path, 'detach')
    if os.path.exists(detach_path):
        try:
            with open(detach_path, 'w') as f:
                f.write('1')
        except Exception as e:
            return jsonify({'error': f'Failed to detach: {e}'}), 500

    stop_path = os.path.join(bcache_path, 'stop')
    if os.path.exists(stop_path):
        try:
            with open(stop_path, 'w') as f:
                f.write('1')
        except Exception:
            pass

    state = _load_state()
    state['caches'] = [c for c in state.get('caches', [])
                       if c.get('backing_device') != backing_dev]
    _save_state(state)

    try:
        from blueprints.eventlog import log as elog
        elog('storage', 'info', f'SSD cache detached from {backing_dev}')
    except Exception:
        pass

    return jsonify({'ok': True})


@ssd_cache_bp.route('/mode', methods=['PUT'])
@admin_required
def set_mode():
    """Change bcache mode for a backing device."""
    body = request.get_json(silent=True) or {}
    backing_dev = _safe_device(body.get('backing_device'))
    mode = body.get('mode', 'writethrough')

    if not backing_dev:
        return jsonify({'error': 'Invalid backing device'}), 400
    if mode not in ('writethrough', 'writeback', 'writearound', 'none'):
        return jsonify({'error': 'Invalid mode'}), 400

    backing_name = backing_dev.split('/')[-1]
    mode_path = f'/sys/block/{backing_name}/bcache/cache_mode'

    if not os.path.exists(mode_path):
        return jsonify({'error': 'No bcache found for device'}), 400

    try:
        with open(mode_path, 'w') as f:
            f.write(mode)
    except Exception as e:
        return jsonify({'error': f'Failed to set mode: {e}'}), 500

    state = _load_state()
    for c in state.get('caches', []):
        if c.get('backing_device') == backing_dev:
            c['mode'] = mode
    _save_state(state)

    return jsonify({'ok': True, 'mode': mode})


@ssd_cache_bp.route('/stats', methods=['GET'])
@admin_required
def cache_stats():
    """Return detailed bcache statistics."""
    if not _ensure_bcache():
        return jsonify({'ok': False, 'error': 'bcache-tools not available'}), 500

    live = _live_bcache_info()
    return jsonify({'ok': True, 'caches': live})