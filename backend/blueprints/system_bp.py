"""
EthOS — System Management Blueprint
Extracted from app.py (Language, Power, System Info, Services Manager sections).

Exposes:
  system_bp — system-wide routes
    /api/language — get/set system language
    /api/power/* — shutdown/restart/sleep
    /api/system-info — system info
    /api/services/* — service management
"""

import os
import sys
import json
import time
import re
import subprocess
from flask import Blueprint, request, jsonify, g
from i18n import t

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from host import (
    data_path as _data_path,
    app_path as _app_path,
    ETHOS_ROOT,
    host_run as _host_run,
)
from utils import load_json as _load_json, save_json as _save_json
from blueprints.eventlog import log as elog
from blueprints.admin_required import admin_required
from blueprints.auth import require_auth

system_bp = Blueprint('system', __name__)

SETUP_DONE_FILE = _data_path('setup_done')

# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────── Language / i18n ───────────────────────────

SUPPORTED_LANGUAGES = [
    'pl', 'en', 'de', 'fr', 'es',
]

@system_bp.route('/api/language', methods=['GET'])
def get_language():
    """Get the system language (no auth required — needed during setup)."""
    env = _read_env_file()
    lang = env.get('LANGUAGE', 'pl')
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'pl'
    return jsonify({'language': lang, 'supported': SUPPORTED_LANGUAGES})


@system_bp.route('/api/language', methods=['POST'])
def set_language():
    """Set the system language. Persists to ethos.env."""
    data = request.get_json(silent=True) or {}
    lang = data.get('language', 'pl')
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'pl'
    _write_env_file_key('LANGUAGE', lang)
    return jsonify({'ok': True, 'language': lang})


def _read_env_file():
    """Read ethos.env key-value pairs."""
    env = {}
    env_path = os.path.join(ETHOS_ROOT, 'ethos.env')
    try:
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def _write_env_file_key(key, value):
    """Write a single key to ethos.env (pure Python, no shell)."""
    env_path = os.path.join(ETHOS_ROOT, 'ethos.env')
    lines = []
    found = False
    if os.path.isfile(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if line.rstrip('\n').split('=', 1)[0].strip() == key:
                    lines.append(f'{key}={value}\n')
                    found = True
                else:
                    lines.append(line if line.endswith('\n') else line + '\n')
    if not found:
        lines.append(f'{key}={value}\n')
    with open(env_path, 'w') as f:
        f.writelines(lines)


# ─────────────────────────── Setup Wizard ───────────────────────────

_SETUP_PROGRESS = {
    'active': False,
    'stage': '',
    'message': '',
    'elapsed': 0,
    'started_at': 0,
    'updated_at': 0,
}
_setup_lock = __import__('threading').Lock()


def _setup_progress_update(stage, message, active=True):
    now = time.time()
    with _setup_lock:
        if active and not _SETUP_PROGRESS.get('started_at'):
            _SETUP_PROGRESS['started_at'] = now
        _SETUP_PROGRESS['active'] = active
        _SETUP_PROGRESS['stage'] = stage
        _SETUP_PROGRESS['message'] = message
        _SETUP_PROGRESS['updated_at'] = now
        _SETUP_PROGRESS['elapsed'] = int(now - (_SETUP_PROGRESS.get('started_at') or now))


def _setup_progress_start(message='Starting configuration...'):
    now = time.time()
    with _setup_lock:
        _SETUP_PROGRESS.update({
            'active': True,
            'stage': 'start',
            'message': message,
            'elapsed': 0,
            'started_at': now,
            'updated_at': now,
        })


def _setup_progress_end(stage, message):
    _setup_progress_update(stage, message, active=False)

def _is_setup_done():
    return os.path.exists(SETUP_DONE_FILE)


@system_bp.route('/api/setup/status')
def setup_status():
    """Check if initial setup has been completed (no auth required)."""
    return jsonify({'needs_setup': not _is_setup_done()})


@system_bp.route('/api/setup/progress')
def setup_progress():
    """Return current progress of /api/setup/complete execution."""
    with _setup_lock:
        if _SETUP_PROGRESS.get('active') and _SETUP_PROGRESS.get('started_at'):
            _SETUP_PROGRESS['elapsed'] = int(time.time() - _SETUP_PROGRESS['started_at'])
        return jsonify({
            'active': bool(_SETUP_PROGRESS.get('active')),
            'stage': _SETUP_PROGRESS.get('stage', ''),
            'message': _SETUP_PROGRESS.get('message', ''),
            'elapsed': int(_SETUP_PROGRESS.get('elapsed') or 0),
            'updated_at': int(_SETUP_PROGRESS.get('updated_at') or 0),
        })


@system_bp.route('/api/setup/timezones')
def setup_timezones():
    """List available timezones (no auth for setup wizard)."""
    tz_dir = '/usr/share/zoneinfo'
    zones = []
    for region in sorted(os.listdir(tz_dir)):
        region_path = os.path.join(tz_dir, region)
        if not os.path.isdir(region_path) or region.startswith(('.', '+')) or region in ('posix', 'right', 'posixrules'):
            continue
        for city in sorted(os.listdir(region_path)):
            if os.path.isfile(os.path.join(region_path, city)):
                zones.append(f'{region}/{city}')
    return jsonify({'timezones': zones, 'default': 'Europe/Warsaw'})


@system_bp.route('/api/setup/locales')
def setup_locales():
    """List commonly used locales for setup wizard."""
    locales = [
        {'code': 'en_US.UTF-8', 'name': 'English (US)'},
        {'code': 'en_GB.UTF-8', 'name': 'English (UK)'},
        {'code': 'pl_PL.UTF-8', 'name': 'Polski'},
        {'code': 'de_DE.UTF-8', 'name': 'Deutsch'},
        {'code': 'fr_FR.UTF-8', 'name': 'Français'},
        {'code': 'es_ES.UTF-8', 'name': 'Español'},
        {'code': 'it_IT.UTF-8', 'name': 'Italiano'},
        {'code': 'pt_BR.UTF-8', 'name': 'Português (Brasil)'},
        {'code': 'nl_NL.UTF-8', 'name': 'Nederlands'},
        {'code': 'sv_SE.UTF-8', 'name': 'Svenska'},
        {'code': 'nb_NO.UTF-8', 'name': 'Norsk'},
        {'code': 'da_DK.UTF-8', 'name': 'Dansk'},
        {'code': 'fi_FI.UTF-8', 'name': 'Suomi'},
        {'code': 'ja_JP.UTF-8', 'name': '日本語'},
        {'code': 'zh_CN.UTF-8', 'name': '中文 (简体)'},
        {'code': 'ko_KR.UTF-8', 'name': '한국어'},
    ]
    return jsonify({'locales': locales, 'default': 'en_US.UTF-8'})


@system_bp.route('/api/setup/languages')
def setup_languages():
    """List available UI languages with their translations for firstboot i18n."""
    from i18n import SUPPORTED_LANGUAGES, _ensure_loaded, _translations, _I18N_DIR
    langs = []
    for code in SUPPORTED_LANGUAGES:
        _ensure_loaded(code)
        data = _translations.get(code, {})
        if data or os.path.exists(os.path.join(_I18N_DIR, f'{code}.json')):
            name = {'en': 'English', 'pl': 'Polski', 'de': 'Deutsch',
                    'fr': 'Français', 'es': 'Español'}.get(code, code)
            langs.append({'code': code, 'name': name})
    return jsonify({'languages': langs, 'default': 'en'})


@system_bp.route('/api/setup/translations/<lang>')
def setup_translations(lang):
    """Get full translation file for a language (preboot i18n)."""
    import re
    if not re.match(r'^[a-z]{2}$', lang):
        return jsonify({'error': 'Invalid language code'}), 400
    from i18n import _ensure_loaded, _translations
    _ensure_loaded(lang)
    return jsonify(_translations.get(lang, {}))


# ── EthOS identification (public, no auth) ──

@system_bp.route('/api/ethos/identify')
def ethos_identify():
    """Public endpoint for NAS-to-NAS discovery. Returns basic info about this instance."""
    import socket
    # Use sys.modules to avoid circular import
    app = sys.modules.get('app')
    NAS_NAME = getattr(app, 'NAS_NAME', 'EthOS') if app else 'EthOS'
    PORT = getattr(app, 'PORT', 9000) if app else 9000
    ETHOS_VERSION = getattr(app, 'ETHOS_VERSION', {}) if app else {}
    
    hostname = socket.gethostname()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = '127.0.0.1'
    return jsonify({
        'ethos': True,
        'name': NAS_NAME,
        'hostname': hostname,
        'ip': local_ip,
        'port': PORT,
        'version': ETHOS_VERSION.get('version', '0.0.0'),
    })


# ── Avahi mDNS service registration ──

def _register_avahi_service():
    """Register EthOS as an mDNS service so other NAS instances can discover it."""
    # Use sys.modules to avoid circular import
    app = sys.modules.get('app')
    if not app:
        return
    NAS_NAME = getattr(app, 'NAS_NAME', 'EthOS')
    PORT = getattr(app, 'PORT', 9000)
    ETHOS_VERSION = getattr(app, 'ETHOS_VERSION', {})
    
    service_xml = f"""<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">{NAS_NAME} (%h)</name>
  <service>
    <type>_ethos._tcp</type>
    <port>{PORT}</port>
    <txt-record>name={NAS_NAME}</txt-record>
    <txt-record>version={ETHOS_VERSION.get('version', '0.0.0')}</txt-record>
  </service>
</service-group>
"""
    try:
        avahi_dir = '/etc/avahi/services'
        if os.path.isdir(avahi_dir):
            svc_file = os.path.join(avahi_dir, 'ethos.service')
            with open(svc_file, 'w') as f:
                f.write(service_xml)
            # Reload avahi if running
            subprocess.run(['systemctl', 'reload', 'avahi-daemon'],
                           capture_output=True, timeout=5)
            print(f'  Avahi mDNS: _ethos._tcp registered on port {PORT}')
    except Exception as e:
        print(f'  [warn] Avahi mDNS registration skipped: {e}')


@system_bp.route('/api/setup/disks')
def setup_disks():
    """List block devices for data storage — includes raw/unpartitioned disks.

    Returns all disks with partition info.  For the system disk we also
    calculate unallocated (free) space so the wizard can offer to create
    a data partition there — just like Synology / QNAP.
    """
    if _is_setup_done():
        return jsonify({'error': 'Setup already completed'}), 400

    import subprocess as _sp

    # Use lsblk for comprehensive block device info (including raw disks)
    try:
        r = _sp.run(
            ['lsblk', '-J', '-b', '-o',
             'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,TRAN,RO,RM,LABEL,UUID'],
            capture_output=True, text=True, timeout=10)
        lsblk_data = json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        lsblk_data = {}

    block_devs = lsblk_data.get('blockdevices', [])

    # Find the system disk (the one that has / mounted)
    system_disk_names = set()

    def _find_system(devs, parent=None):
        for d in devs:
            if (d.get('mountpoint') or '') == '/':
                system_disk_names.add(parent or d['name'])
            for c in d.get('children', []):
                if (c.get('mountpoint') or '') == '/':
                    system_disk_names.add(d['name'])
            _find_system(d.get('children', []), parent or d.get('name'))

    _find_system(block_devs)

    def _get_free_space(disk_device):
        """Compute unallocated bytes on a disk using parted."""
        try:
            r = _sp.run(
                ['parted', '-ms', disk_device, 'unit', 'B', 'print', 'free'],
                capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return 0
            free_bytes = 0
            for line in r.stdout.splitlines():
                # free-space lines look like: 1:3276800B:64023257087B:64019980288B:free;
                if ':free;' in line:
                    parts = line.split(':')
                    if len(parts) >= 4:
                        size_str = parts[3].rstrip('B')
                        try:
                            free_bytes += int(size_str)
                        except ValueError:
                            pass
            return free_bytes
        except Exception:
            return 0

    disks = []
    for dev in block_devs:
        if dev.get('type') != 'disk':
            continue
        if dev.get('ro'):
            continue
        name = dev.get('name', '')
        if name.startswith(('loop', 'sr', 'fd', 'zram')):
            continue
        size = dev.get('size') or 0
        if size < 1_000_000_000:  # skip < 1 GB
            continue

        is_system = name in system_disk_names
        children = dev.get('children', [])

        partitions = []
        for child in children:
            ctype = child.get('type', '')
            if ctype not in ('part', 'crypt'):
                continue
            partitions.append({
                'name': child.get('name', ''),
                'device': f"/dev/{child.get('name', '')}",
                'size': child.get('size') or 0,
                'fstype': child.get('fstype') or '',
                'mountpoint': child.get('mountpoint') or '',
                'label': child.get('label') or '',
                'uuid': child.get('uuid') or '',
            })

        # Find first usable partition (ext4/xfs/btrfs and mounted, non-root)
        usable_mp = ''
        for p in partitions:
            if p['fstype'] in ('ext4', 'xfs', 'btrfs') and p['mountpoint'] \
                    and p['mountpoint'] != '/':
                usable_mp = p['mountpoint']
                break

        disk_info = {
            'name': name,
            'device': f"/dev/{name}",
            'size': size,
            'model': (dev.get('model') or '').strip(),
            'transport': dev.get('tran') or '',
            'removable': bool(dev.get('rm')),
            'is_system': is_system,
            'partitions': partitions,
            'usable_mountpoint': usable_mp,
        }

        # For system disk — compute free (unallocated) space
        if is_system:
            free = _get_free_space(f"/dev/{name}")
            disk_info['free_space'] = free
            # Already has a non-root EthOS-Data partition? (e.g. previous setup)
            for p in partitions:
                if p['label'] == 'EthOS-Data' and p['fstype'] in ('ext4', 'xfs', 'btrfs'):
                    disk_info['existing_data_partition'] = {
                        'device': p['device'],
                        'size': p['size'],
                        'fstype': p['fstype'],
                        'mountpoint': p['mountpoint'],
                    }
                    if p['mountpoint']:
                        disk_info['usable_mountpoint'] = p['mountpoint']
                    break

        disks.append(disk_info)

    # Sort: system disk first (primary option), then by size descending
    disks.sort(key=lambda d: (not d['is_system'], -d['size']))

    return jsonify({'disks': disks})


@system_bp.route('/api/setup/prepare-disk', methods=['POST'])
def setup_prepare_disk():
    """Prepare a disk for data storage.

    Modes (determined by ``mode`` field):
    * ``format``  — wipe & partition an entire separate disk (existing behaviour)
    * ``syspart`` — create a new data partition from free space on the system disk
    * ``mount``   — mount an existing partition (e.g. EthOS-Data from previous setup)
    """
    if _is_setup_done():
        return jsonify({'error': 'Setup already completed'}), 400

    data = request.json or {}
    mode = data.get('mode', 'format')
    device = data.get('device', '').strip()
    encrypt = data.get('encrypt', False)
    passphrase = data.get('passphrase', '')

    if not device or not device.startswith('/dev/'):
        return jsonify({'error': 'Invalid device'}), 400
    if not os.path.exists(device):
        return jsonify({'error': f'Device {device} does not exist'}), 400

    import subprocess as _sp

    # For encryption, require passphrase
    if encrypt and (not passphrase or len(passphrase) < 4):
        return jsonify({'error': 'Encryption password must be at least 4 characters'}), 400
    if encrypt and shutil.which('cryptsetup') is None:
        return jsonify({'error': 'LUKS encryption requires the cryptsetup package. Install it or disable encryption.'}), 400

    mountpoint = '/mnt/data'

    def _parted_kernel_sync_issue(stderr_text):
        s = (stderr_text or '').lower()
        return ('unable to inform the kernel' in s) or ('will remain in use' in s)

    def _wait_for_partition(candidates, wait_sec=20):
        """Wait for new partition node to appear (udev can be delayed on some disks)."""
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            for c in candidates:
                if os.path.exists(c):
                    return c
            try:
                _sp.run(['udevadm', 'settle', '--timeout=3'], capture_output=True, timeout=5)
            except Exception:
                pass
            time.sleep(0.5)
        return ''

    def _try_unmount(target):
        """Try force unmount, then lazy unmount if target is busy."""
        if not target:
            return
        try:
            _sp.run(['umount', '-f', target], capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            _sp.run(['umount', '-l', target], capture_output=True, timeout=10)
        except Exception:
            pass

    # ── Helper: format + optional LUKS + mount a partition ──────
    def _format_and_mount(part_dev):
        target_dev = part_dev
        is_luks = False
        udisks_was_active = False

        # Safety: partition can get auto-mounted by udev/udisks right after creation.
        # Ensure it's unmounted before mkfs.
        try:
            r_um = _sp.run(['findmnt', '-rn', '-S', part_dev, '-o', 'TARGET'],
                           capture_output=True, text=True, timeout=5)
            for mp in (r_um.stdout or '').splitlines():
                mp = mp.strip()
                if mp:
                    _try_unmount(mp)
        except Exception:
            pass

        # Temporarily stop udisks automounter to avoid immediate re-mount during mkfs.
        try:
            r_ud = _sp.run(['systemctl', 'is-active', 'udisks2'],
                           capture_output=True, text=True, timeout=5)
            if r_ud.returncode == 0:
                _sp.run(['systemctl', 'stop', 'udisks2'], capture_output=True, timeout=15)
                udisks_was_active = True
        except Exception:
            pass

        def _restore_udisks():
            if not udisks_was_active:
                return
            try:
                _sp.run(['systemctl', 'start', 'udisks2'], capture_output=True, timeout=15)
            except Exception:
                pass

        try:
            if encrypt:
                os.makedirs('/etc/ethos', mode=0o700, exist_ok=True)
                keyfile = '/etc/ethos/luks.key'
                _sp.run(['dd', 'if=/dev/urandom', f'of={keyfile}', 'bs=4096', 'count=1'],
                        capture_output=True, timeout=10)
                os.chmod(keyfile, 0o600)

                r = _sp.run(['cryptsetup', 'luksFormat', '--batch-mode',
                             '--key-file', keyfile, part_dev],
                            capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    return None, f'LUKS error: {r.stderr.strip()}'

                r = _sp.run(['cryptsetup', 'luksAddKey', '--key-file', keyfile, part_dev],
                            input=passphrase.encode(),
                            capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    return None, f'Error adding password: {r.stderr.strip()}'

                r = _sp.run(['cryptsetup', 'luksOpen', '--key-file', keyfile,
                             part_dev, 'ethos_data'],
                            capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    return None, f'Error opening LUKS: {r.stderr.strip()}'

                target_dev = '/dev/mapper/ethos_data'
                is_luks = True

                r2 = _sp.run(['blkid', '-s', 'UUID', '-o', 'value', part_dev],
                             capture_output=True, text=True, timeout=5)
                part_uuid = r2.stdout.strip()
                if part_uuid:
                    existing = ''
                    try:
                        with open('/etc/crypttab', 'r') as f:
                            existing = f.read()
                    except FileNotFoundError:
                        pass
                    if 'ethos_data' not in existing:
                        with open('/etc/crypttab', 'a') as f:
                            f.write(f'# EthOS encrypted data disk\n'
                                    f'ethos_data UUID={part_uuid} {keyfile} luks\n')

            fmt_err = ''
            for _ in range(4):
                try:
                    # Direct unmount attempts help when automounters re-attach device.
                    _try_unmount(part_dev)
                    if target_dev != part_dev:
                        _try_unmount(target_dev)
                except Exception:
                    pass
                r = _sp.run(['mkfs.ext4', '-F', '-F', '-L', 'EthOS-Data', target_dev],
                            capture_output=True, text=True, timeout=300)
                if r.returncode == 0:
                    fmt_err = ''
                    break
                fmt_err = (r.stderr or '').strip()
                if 'is mounted; will not make a filesystem' in fmt_err.lower():
                    time.sleep(1)
                    continue
                return None, f'Formatting error: {fmt_err}'
            if fmt_err:
                return None, f'Formatting error: {fmt_err}'

            os.makedirs(mountpoint, mode=0o755, exist_ok=True)
            r = _sp.run(['mount', target_dev, mountpoint],
                        capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return None, f'Mount error: {r.stderr.strip()}'

            return {'success': True, 'mountpoint': mountpoint,
                    'device': part_dev, 'encrypted': is_luks}, None
        finally:
            _restore_udisks()

    try:
        # ── Mode: mount existing partition ──────────────────────
        if mode == 'mount':
            part_dev = data.get('partition', device)
            if not os.path.exists(part_dev):
                return jsonify({'error': f'Partition {part_dev} does not exist'}), 400
            os.makedirs(mountpoint, mode=0o755, exist_ok=True)
            # Check if already mounted at mountpoint
            r = _sp.run(['findmnt', '-no', 'SOURCE', mountpoint],
                        capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                # Already mounted — return success
                return jsonify({'success': True, 'mountpoint': mountpoint,
                                'device': part_dev, 'encrypted': False})
            r = _sp.run(['mount', part_dev, mountpoint],
                        capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return jsonify({'error': f'Mount error: {r.stderr.strip()}'}), 500
            return jsonify({'success': True, 'mountpoint': mountpoint,
                            'device': part_dev, 'encrypted': False})

        # ── Mode: create data partition from system disk free space ─
        if mode == 'syspart':
            # device = system disk (e.g. /dev/sda or /dev/mmcblk0)
            # Find highest partition number
            r = _sp.run(['parted', '-ms', device, 'unit', 'B', 'print'],
                        capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return jsonify({'error': f'Partition read error: {r.stderr.strip()}'}), 500

            max_num = 0
            for line in r.stdout.splitlines():
                if line and line[0].isdigit():
                    try:
                        max_num = max(max_num, int(line.split(':')[0]))
                    except (ValueError, IndexError):
                        pass

            new_num = max_num + 1

            # Find the largest free region via 'print free'
            r2 = _sp.run(
                ['parted', '-ms', device, 'unit', 'B', 'print', 'free'],
                capture_output=True, text=True, timeout=10)
            free_start = free_end = None
            best_size = 0
            for line in r2.stdout.splitlines():
                if ':free;' in line:
                    parts = line.split(':')
                    try:
                        fs = int(parts[1].rstrip('B'))
                        fe = int(parts[2].rstrip('B'))
                        size = fe - fs
                        if size > best_size:
                            best_size = size
                            free_start, free_end = fs, fe
                    except (ValueError, IndexError):
                        pass

            if free_start is None or best_size < 500_000_000:
                return jsonify({'error': 'Not enough free disk space'}), 400

            r = _sp.run(
                ['parted', '-s', device, 'mkpart', 'primary', 'ext4',
                 f'{free_start}B', f'{free_end}B'],
                capture_output=True, text=True, timeout=15)
            if r.returncode != 0 and not _parted_kernel_sync_issue(r.stderr):
                return jsonify({'error': f'Partition creation error: {r.stderr.strip()}'}), 500
            if r.returncode != 0:
                # Parted sometimes writes metadata but returns non-zero when kernel
                # hasn't re-read partition table yet. Try to resync and continue.
                _sp.run(['partprobe', device], capture_output=True, timeout=10)
                _sp.run(['udevadm', 'settle', '--timeout=8'], capture_output=True, timeout=12)

            _sp.run(['udevadm', 'settle', '--timeout=10'], capture_output=True, timeout=15)
            time.sleep(1)

            # Determine new partition device path
            # For /dev/sda → /dev/sdaN, for /dev/mmcblk0 → /dev/mmcblk0pN
            if 'mmcblk' in device or 'nvme' in device:
                part_dev = f"{device}p{new_num}"
            else:
                part_dev = f"{device}{new_num}"

            if not os.path.exists(part_dev):
                # Ask kernel/udev to refresh partition map, then wait a bit longer.
                _sp.run(['partprobe', device], capture_output=True, timeout=10)
                _sp.run(['udevadm', 'settle', '--timeout=5'], capture_output=True, timeout=10)
                found = _wait_for_partition([part_dev], wait_sec=20)
                if found:
                    part_dev = found

            if not os.path.exists(part_dev):
                return jsonify({'error': f'Partition {part_dev} did not appear'}), 500

            result, err = _format_and_mount(part_dev)
            if err:
                return jsonify({'error': err}), 500
            return jsonify(result)

        # ── Mode: format entire separate disk (default / legacy) ──
        # Safety: prevent formatting the system disk
        r = _sp.run(['findmnt', '-no', 'SOURCE', '/'],
                    capture_output=True, text=True, timeout=5)
        root_source = r.stdout.strip()
        root_disk = re.sub(r'p?\d+$', '', root_source)
        if device == root_disk or device == root_source:
            return jsonify({'error': 'Cannot format system disk!'}), 400

        # 1. Unmount any existing partitions on this disk
        r = _sp.run(['lsblk', '-nlo', 'NAME,MOUNTPOINT', device],
                    capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split('\n'):
            parts = line.split(None, 1)
            if len(parts) >= 2 and parts[1].strip():
                _try_unmount(parts[1].strip())

        # Also detach setup mountpoint if still in use from previous runs.
        _try_unmount(mountpoint)

        # 2. Close any existing LUKS mappings
        _sp.run(['bash', '-c',
                 'for m in /dev/mapper/ethos_data*; do '
                 '[ -e "$m" ] && cryptsetup close "$m" 2>/dev/null; done'],
                capture_output=True, timeout=10)

        # 3. Wipe existing signatures
        _sp.run(['wipefs', '-af', device], capture_output=True, timeout=15)

        # 4. Create GPT partition table with single partition using full disk
        r = _sp.run(['parted', '-s', device, 'mklabel', 'gpt'],
                    capture_output=True, text=True, timeout=15)
        if r.returncode != 0 and not _parted_kernel_sync_issue(r.stderr):
            return jsonify({'error': f'Partition table error: {r.stderr.strip()}'}), 500
        if r.returncode != 0:
            _sp.run(['partprobe', device], capture_output=True, timeout=10)
            _sp.run(['udevadm', 'settle', '--timeout=8'], capture_output=True, timeout=12)

        r = _sp.run(['parted', '-s', device, 'mkpart', 'primary', '1MiB', '100%'],
                    capture_output=True, text=True, timeout=15)
        if r.returncode != 0 and not _parted_kernel_sync_issue(r.stderr):
            return jsonify({'error': f'Partition creation error: {r.stderr.strip()}'}), 500
        if r.returncode != 0:
            _sp.run(['partprobe', device], capture_output=True, timeout=10)
            _sp.run(['udevadm', 'settle', '--timeout=8'], capture_output=True, timeout=12)

        # 5. Wait for udev and discover partition node (sdb1 or nvme0n1p1)
        _sp.run(['udevadm', 'settle', '--timeout=10'], capture_output=True, timeout=15)
        time.sleep(1)
        part_dev = _wait_for_partition([f"{device}1", f"{device}p1"], wait_sec=20)
        if not part_dev:
            _sp.run(['partprobe', device], capture_output=True, timeout=10)
            part_dev = _wait_for_partition([f"{device}1", f"{device}p1"], wait_sec=10)
        if not part_dev:
            return jsonify({'error': 'Partition did not appear after creation'}), 500

        result, err = _format_and_mount(part_dev)
        if err:
            return jsonify({'error': err}), 500
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@system_bp.route('/api/setup/complete', methods=['POST'])
def setup_complete():
    """Complete the initial setup — create admin user, set hostname, set password."""
    global NAS_NAME
    if _is_setup_done():
        return jsonify({'error': 'Setup already completed'}), 400

    data = request.json or {}
    hostname = re.sub(r'[^a-zA-Z0-9\-]', '', data.get('hostname', '')).strip() or 'ethos'
    username = re.sub(r'[^a-zA-Z0-9_.\-]', '', data.get('username', '')).strip()
    password = data.get('password', '')
    nas_name = data.get('nas_name', '').strip() or hostname
    data_disk = data.get('data_disk', '').strip()  # mountpoint for user data
    language = data.get('language', 'pl').strip()   # system language from wizard
    timezone = data.get('timezone', '').strip()     # e.g. "Europe/Warsaw"
    locale = data.get('locale', '').strip()         # e.g. "en_US.UTF-8"

    if not username or len(username) < 2:
        return jsonify({'error': 'Username is required (min. 2 characters)'}), 400

    from blueprints.users import validate_password_strength
    pw_ok, pw_errors = validate_password_strength(password, username)
    if not pw_ok:
        return jsonify({'error': pw_errors[0], 'password_errors': pw_errors}), 400

    import shlex
    errors = []
    _setup_progress_start('Starting system configuration...')

    # 0. Set timezone & locale if provided
    if timezone and re.match(r'^[A-Za-z_]+/[A-Za-z_/]+$', timezone):
        tz_path = f'/usr/share/zoneinfo/{timezone}'
        if os.path.exists(tz_path):
            _host_run_base(f"ln -sf {tz_path} /etc/localtime && "
                           f"echo {shlex.quote(timezone)} > /etc/timezone", timeout=10)
    if locale and re.match(r'^[a-zA-Z_]+\.[A-Za-z0-9-]+$', locale):
        _host_run_base(f"echo {shlex.quote(locale + ' UTF-8')} >> /etc/locale.gen && "
                       f"locale-gen >/dev/null 2>&1 && "
                       f"echo {shlex.quote('LANG=' + locale)} > /etc/default/locale",
                       timeout=30)

    # 1. Set hostname on host
    _setup_progress_update('hostname', 'Setting system hostname...')
    _host_run_base(f"hostnamectl set-hostname {shlex.quote(hostname)}", timeout=10)

    # 2. Setup data disk — create directory structure and symlinks
    if data_disk and data_disk != '/' and os.path.isdir(data_disk):
        _setup_progress_update('data_disk', 'Configuring data disk and symlinks...')
        _setup_data_disk(data_disk, errors)

    # 3. Create or update system user
    _setup_progress_update('user', 'Creating/updating administrator account...')
    safe_user = shlex.quote(username)
    safe_pass = shlex.quote(username + ':' + password)
    groups = 'sudo,ethos-user,ethos-admin'

    # Determine user home directory — on data disk if available
    home_base = os.path.join(data_disk, 'home') if (data_disk and data_disk != '/') else '/home'
    home_dir = os.path.join(home_base, username)
    os.makedirs(home_base, mode=0o755, exist_ok=True)

    r = _host_run_base(f"id {safe_user} 2>/dev/null", timeout=10)
    if r.returncode != 0:
        # Create user with home on data disk
        r = _host_run_base(
            f"getent group ethos-user >/dev/null 2>&1 || groupadd ethos-user; "
            f"getent group ethos-admin >/dev/null 2>&1 || groupadd ethos-admin; "
            f"getent group ethos-family >/dev/null 2>&1 || groupadd ethos-family; "
            f"useradd -m -d {shlex.quote(home_dir)} -s /bin/bash -G {groups} {safe_user} && "
            f"echo {safe_pass} | chpasswd",
            timeout=15)
        if r.returncode != 0:
            errors.append(f'Error creating user: {r.stderr.strip()}')
    else:
        # User exists — update password and groups
        _host_run_base(
            f"echo {safe_pass} | chpasswd && "
            f"usermod -aG {groups} {safe_user}",
            timeout=10)

    # Grant passwordless sudo (like the default nasadmin user)
    sudoers_file = f"/etc/sudoers.d/010_{username}"
    _host_run_base(
        f"echo {shlex.quote(username + ' ALL=(ALL) NOPASSWD:ALL')} > {shlex.quote(sudoers_file)} && "
        f"chmod 440 {shlex.quote(sudoers_file)}",
        timeout=10)

    # 3b. Create default folder structure based on wizard language + ~/.ethos
    _setup_progress_update('home', 'Creating user directory structure...')
    _ensure_user_home_structure(username, lang=language)

    # 3c. Keep only setup-selected admin account
    _setup_progress_update('admin_cleanup', 'Disabling other administrative accounts...')
    _restrict_admin_users(username, errors)

    # 4. Update NAS_NAME in memory
    _setup_progress_update('settings', 'Saving system settings...')
    NAS_NAME = nas_name

    # 4b. Persist language setting
    if language in SUPPORTED_LANGUAGES:
        _write_env_file_key('LANGUAGE', language)

    # 5. Update env files on host for persistence across restarts
    _update_compose_env(username, password, nas_name, data_disk)

    # 6. Mark setup as done
    _setup_progress_update('finalize', 'Finalizing configuration...')
    setup_info = {
        'timestamp': time.time(),
        'hostname': hostname,
        'username': username,
        'nas_name': nas_name,
    }
    if data_disk and data_disk != '/':
        setup_info['data_disk'] = data_disk
    with open(SETUP_DONE_FILE, 'w') as f:
        json.dump(setup_info, f)

    # Create password changed marker
    with open(PASSWORD_CHANGED_MARKER, 'w') as f:
        f.write(str(time.time()))

    # Enable SSH now that setup is complete and password is set
    try:
        subprocess.run(['systemctl', 'enable', '--now', 'ssh'], check=False)
    except Exception:
        pass

    # Generate default SSH keypair for the admin user
    try:
        from blueprints.ssh_manager import SSH_KEYS_DIR
        os.makedirs(SSH_KEYS_DIR, mode=0o700, exist_ok=True)
        default_key = os.path.join(SSH_KEYS_DIR, f'ethos_{hostname}')
        if not os.path.exists(default_key):
            _comment = f'{username}@{hostname}'
            subprocess.run(
                ['ssh-keygen', '-t', 'ed25519', '-f', default_key, '-N', '', '-C', _comment],
                capture_output=True, timeout=30, check=False,
            )
            if os.path.isfile(default_key):
                os.chmod(default_key, 0o600)
            if os.path.isfile(default_key + '.pub'):
                os.chmod(default_key + '.pub', 0o644)
    except Exception as e:
        log.warning('Auto SSH keygen failed: %s', e)

    # Replace tty1 auto-login with read-only info console (Synology-style)
    try:
        console_svc = '/opt/ethos/tools/ethos-console@.service'
        if os.path.isfile(console_svc):
            _host_run_base(
                'cp /opt/ethos/tools/ethos-console@.service /etc/systemd/system/ && '
                'systemctl daemon-reload && '
                'systemctl disable --now getty@tty1.service 2>/dev/null; '
                'rm -rf /etc/systemd/system/getty@tty1.service.d 2>/dev/null; '
                'systemctl enable --now ethos-console@tty1.service',
                timeout=15
            )
    except Exception:
        pass  # non-critical

    if errors:
        _setup_progress_end('done', 'Configuration completed with warnings.')
        return jsonify({'success': True, 'warnings': errors})
    _setup_progress_end('done', 'Configuration completed successfully.')
    return jsonify({'success': True})


def _setup_data_disk(mountpoint, errors):
    """Create EthOS directory structure on the chosen data disk and symlink from ETHOS_ROOT."""
    import shutil
    ethos_on_disk = os.path.join(mountpoint, 'ethos')
    dirs_to_move = ['data', 'backups', 'uploads', 'logs', 'temp']

    # --- Ensure data disk is in fstab for auto-mount on boot ---
    try:
        # Find the device for this mountpoint
        import subprocess as _sp
        r = _sp.run(['findmnt', '-no', 'SOURCE', mountpoint],
                     capture_output=True, text=True, timeout=5)
        dev = r.stdout.strip()
        if dev:
            # Get UUID for stable identification
            r2 = _sp.run(['blkid', '-s', 'UUID', '-o', 'value', dev],
                          capture_output=True, text=True, timeout=5)
            uuid = r2.stdout.strip()
            # Get fstype
            r3 = _sp.run(['blkid', '-s', 'TYPE', '-o', 'value', dev],
                          capture_output=True, text=True, timeout=5)
            fstype = r3.stdout.strip() or 'auto'

            if uuid:
                fstab_line = f'UUID={uuid}  {mountpoint}  {fstype}  defaults,nofail,errors=continue  0  2'
                # Keep a single managed entry per mountpoint to avoid duplicates
                # when UUID changes after disk replacement/reformat.
                with open('/etc/fstab', 'r') as f:
                    raw_lines = f.read().splitlines()

                cleaned = []
                i = 0
                while i < len(raw_lines):
                    line = raw_lines[i]
                    stripped = line.strip()
                    # Remove old managed pair: comment + mountpoint line.
                    if stripped == '# EthOS data disk' and i + 1 < len(raw_lines):
                        nxt = raw_lines[i + 1].strip()
                        parts = nxt.split()
                        if len(parts) >= 2 and parts[1] == mountpoint:
                            i += 2
                            continue

                    # Remove any direct entry for this mountpoint.
                    if stripped and not stripped.startswith('#'):
                        parts = stripped.split()
                        if len(parts) >= 2 and parts[1] == mountpoint:
                            i += 1
                            continue

                    cleaned.append(line)
                    i += 1

                while cleaned and not cleaned[-1].strip():
                    cleaned.pop()
                cleaned.append('')
                cleaned.append('# EthOS data disk')
                cleaned.append(fstab_line)

                with open('/etc/fstab', 'w') as f:
                    f.write('\n'.join(cleaned).rstrip() + '\n')
    except Exception as e:
        errors.append(f'Failed to add disk to fstab: {e}')

    try:
        os.makedirs(ethos_on_disk, mode=0o755, exist_ok=True)

        for d in dirs_to_move:
            disk_dir = os.path.join(ethos_on_disk, d)
            local_dir = os.path.join(ETHOS_ROOT, d)

            os.makedirs(disk_dir, mode=0o755, exist_ok=True)

            # If local_dir exists and is NOT already a symlink, move its contents
            if os.path.isdir(local_dir) and not os.path.islink(local_dir):
                for item in os.listdir(local_dir):
                    src = os.path.join(local_dir, item)
                    dst = os.path.join(disk_dir, item)
                    if not os.path.exists(dst):
                        try:
                            shutil.move(src, dst)
                        except Exception as e:
                            errors.append(f'Error moving {item}: {e}')
                # Remove the now-empty directory
                try:
                    shutil.rmtree(local_dir)
                except Exception:
                    pass

            # Create symlink: ETHOS_ROOT/data → /mnt/disk/ethos/data
            if not os.path.islink(local_dir):
                try:
                    if os.path.exists(local_dir):
                        shutil.rmtree(local_dir)
                    os.symlink(disk_dir, local_dir)
                except Exception as e:
                    errors.append(f'Error creating symlink {d}: {e}')

        # Also create shared user folders on disk
        shared_dir = os.path.join(ethos_on_disk, 'shared')
        os.makedirs(shared_dir, mode=0o777, exist_ok=True)

    except Exception as e:
        errors.append(f'Error configuring data disk: {e}')


def _restrict_admin_users(primary_user, errors):
    """Keep only setup-selected user in sudo/ethos-admin admin groups.

    Legacy default account `nasadmin` is also locked when another user is selected.
    """
    import shlex
    try:
        r = _host_run_base("getent group sudo ethos-admin 2>/dev/null | cut -d: -f4 | tr ',' '\n' | sort -u", timeout=10)
        admin_users = [u.strip() for u in (r.stdout or '').split('\n') if u.strip()]
    except Exception as e:
        errors.append(f'Failed to read admin list: {e}')
        return

    for user in admin_users:
        if user in (primary_user, 'root'):
            continue
        su = shlex.quote(user)
        # Remove elevated groups but keep non-admin groups.
        _host_run_base(f'gpasswd -d {su} sudo 2>/dev/null || true', timeout=10)
        _host_run_base(f'gpasswd -d {su} ethos-admin 2>/dev/null || true', timeout=10)

        # Kill default-password path if legacy user remains on system.
        if user == 'nasadmin':
            _host_run_base(f'passwd -l {su} 2>/dev/null || true', timeout=10)
            _host_run_base(f'usermod -p "!" {su} 2>/dev/null || true', timeout=10)


def _update_compose_env(username, password, nas_name, data_disk=''):
    """Update ETHOS_USER, NAS_NAME, DATA_DISK in ethos.env."""
    import shlex
    try:
        ethos_root = os.environ.get('ETHOS_ROOT', '/opt/ethos')
        env_file = os.path.join(ethos_root, 'ethos.env')

        # Write ETHOS_USER
        _write_env_file_key('ETHOS_USER', username)

        safe_name = shlex.quote(nas_name)
        _host_run_base(
            f'sed -i "s|NAS_NAME=.*|NAS_NAME={safe_name}|" {shlex.quote(env_file)}',
            timeout=10)
        # Add or update DATA_DISK
        if data_disk and data_disk != '/':
            safe_disk = shlex.quote(data_disk)
            # Check if DATA_DISK line exists
            r = _host_run_base(
                f'grep -q "^DATA_DISK=" {shlex.quote(env_file)}',
                timeout=5)
            if r.returncode == 0:
                _host_run_base(
                    f'sed -i "s|DATA_DISK=.*|DATA_DISK={safe_disk}|" {shlex.quote(env_file)}',
                    timeout=10)
            else:
                _host_run_base(
                    f'echo "DATA_DISK={safe_disk}" >> {shlex.quote(env_file)}',
                    timeout=10)
    except Exception:
        pass


# ─────────────────────────── Power Management ───────────────────────────

@system_bp.route('/api/power/action', methods=['POST'])
@require_auth
def power_action():
    """Perform a power management action on the host or app."""
    data = request.json or {}
    action = data.get('action', '')

    allowed = ('shutdown', 'reboot', 'restart-app')
    if action not in allowed:
        return jsonify({'error': f'Unknown action: {action}'}), 400

    if action == 'restart-app':
        elog('system', 'warning', 'Power Manager: restarting application')
        gevent.spawn_later(1, _restart_self)
        return jsonify({'ok': True, 'message': 'Application restarting shortly...'})

    if action == 'shutdown':
        elog('system', 'warning', 'Power Manager: shutting down system')
        gevent.spawn_later(2, _host_power, 'poweroff')
        return jsonify({'ok': True, 'message': 'System will shut down...'})

    if action == 'reboot':
        elog('system', 'warning', 'Power Manager: rebooting system')
        gevent.spawn_later(2, _host_power, 'reboot')
        return jsonify({'ok': True, 'message': 'System will reboot shortly...'})


def _host_power(cmd):
    """Execute poweroff/reboot on the host."""
    try:
        _host_run_base(cmd, timeout=30)
    except Exception as e:
        app.logger.error(f'Power action {cmd} failed: {e}')


def _restart_self():
    """Restart EthOS via systemd."""
    try:
        subprocess.run(['systemctl', 'restart', 'ethos'], timeout=60)
    except Exception as e:
        app.logger.error(f'Restart failed: {e}')


@system_bp.route('/api/power/status')
@require_auth
def power_status():
    """Return uptime and load info."""
    sys_info = _mon_sys()
    cpu_info = _mon_cpu()
    return jsonify({
        'uptime': sys_info['uptime'],
        'load': [round(v, 2) for v in cpu_info['load_avg'][:3]]
    })


# ─────────────────────────── System Info ───────────────────────────

@system_bp.route('/api/system/info')
@require_auth
def system_info():
    cpu = _mon_cpu()
    ram = _mon_ram()
    sys_i = _mon_sys()
    disks = _mon_disk()
    net = psutil.net_io_counters()

    return jsonify({
        'hostname': NAS_NAME,
        'cpu_percent': cpu['usage_percent'],
        'cpu_count': cpu['core_count'],
        'memory': {
            'total': ram['total'],
            'used': ram['used'],
            'available': ram['available'],
            'percent': ram['percent']
        },
        'disks': disks,
        'network': {
            'bytes_sent': net.bytes_sent,
            'bytes_recv': net.bytes_recv
        },
        'uptime': sys_i['uptime'],
        'cpu_temp': cpu['temperature'],
        'ups': _ups_status
    })


@system_bp.route('/api/system/version')
@require_auth
def system_version():
    """Return EthOS version info and changelog."""
    return jsonify(ETHOS_VERSION)


@system_bp.route('/api/system/deps', methods=['POST'])
@require_auth
def system_install_dep():
    """Install a missing dependency on-demand. Body: {binary: "smbclient"}"""
    from host import ensure_dep
    data = request.get_json(silent=True) or {}
    binary = data.get('binary', '').strip()
    if not binary:
        return jsonify({'error': 'binary required'}), 400
    ok, msg = ensure_dep(binary, install=True)
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'ok': False, 'error': msg}), 500


# ─────────────────────────── Services Manager ───────────────────────────

# Known services with metadata for nicer display
_KNOWN_SERVICES = {
    'smbd':       {'name': 'Samba (smbd)',     'pkg': 'samba',             'icon': 'fa-windows',      'cat': 'Sharing'},
    'nmbd':       {'name': 'Samba NetBIOS',    'pkg': 'samba',             'icon': 'fa-windows',      'cat': 'Sharing'},
    'nfs-kernel-server':{'name': 'NFS Server', 'pkg': 'nfs-kernel-server', 'icon': 'fa-network-wired','cat': 'Sharing'},
    'minidlna':   {'name': 'MiniDLNA',         'pkg': 'minidlna',         'icon': 'fa-photo-video',  'cat': 'Sharing'},
    'lighttpd':   {'name': 'Lighttpd (WebDAV)','pkg': 'lighttpd',         'icon': 'fa-globe',        'cat': 'Sharing'},
    'vsftpd':     {'name': 'FTP (vsftpd)',     'pkg': 'vsftpd',           'icon': 'fa-upload',       'cat': 'Sharing'},
    'ssh':        {'name': 'SSH / SFTP',       'pkg': 'openssh-server',   'icon': 'fa-lock',         'cat': 'System'},
    'cups':       {'name': 'CUPS (drukarka)',   'pkg': 'cups',             'icon': 'fa-print',        'cat': 'System'},
    'nut-server': {'name': 'UPS Server (NUT)',  'pkg': 'nut',              'icon': 'fa-battery-full', 'cat': 'System'},
    'docker':     {'name': 'Docker',           'pkg': 'docker-ce',        'icon': 'fa-cubes',        'cat': 'System'},
    'ethos':    {'name': 'EthOS',          'pkg': None,               'icon': 'fa-server',       'cat': 'System'},
    'avahi-daemon':{'name': 'Avahi (mDNS)',    'pkg': 'avahi-daemon',     'icon': 'fa-broadcast-tower','cat': 'System'},
    'NetworkManager':{'name': 'NetworkManager','pkg': 'network-manager',  'icon': 'fa-wifi',         'cat': 'System'},
    'cron':       {'name': 'Cron (zaplanowane)','pkg': 'cron',            'icon': 'fa-clock',        'cat': 'System'},
    'transmission-daemon':{'name':'Transmission','pkg':'transmission-daemon','icon':'fa-magnet',      'cat': 'Apps'},
}

@system_bp.route('/api/services/list')
@require_auth
def services_list():
    """List only EthOS-relevant services with their status."""
    services = []
    # Only query known services — no need to list all systemd units
    for svc_id, meta in _KNOWN_SERVICES.items():
        r = _host_run_base(f"systemctl show {svc_id}.service --no-pager "
                           f"--property=LoadState,ActiveState,SubState,UnitFileState 2>/dev/null",
                           timeout=5)
        props = {}
        for line in r.stdout.strip().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                props[k] = v

        load_state = props.get('LoadState', 'not-found')
        if load_state == 'not-found':
            # Service not installed — show as installable if it has a package
            if meta.get('pkg'):
                services.append({
                    'id': svc_id,
                    'name': meta['name'],
                    'icon': meta.get('icon', 'fa-cog'),
                    'category': meta.get('cat', 'Inne'),
                    'pkg': meta.get('pkg'),
                    'active': False,
                    'state': 'not-installed',
                    'enabled': 'not-installed',
                    'masked': False,
                    'installed': False,
                })
            continue

        active = props.get('ActiveState', 'inactive')
        sub = props.get('SubState', 'dead')
        enabled = props.get('UnitFileState', 'unknown')

        services.append({
            'id': svc_id,
            'name': meta['name'],
            'icon': meta.get('icon', 'fa-cog'),
            'category': meta.get('cat', 'Inne'),
            'pkg': meta.get('pkg'),
            'active': active == 'active',
            'state': sub,
            'enabled': enabled,
            'masked': load_state == 'masked',
            'installed': True,
        })

    # Sort by category, then name
    cat_order = {'System': 0, 'Sharing': 1, 'Apps': 2, 'Inne': 3}
    services.sort(key=lambda s: (cat_order.get(s['category'], 9), s['name'].lower()))
    return jsonify({'services': services})


@system_bp.route('/api/services/action', methods=['POST'])
@require_auth
def services_action():
    """Perform action on a service. Body: {service, action}
    Actions: start, stop, restart, enable, disable, uninstall"""
    data = request.get_json(silent=True) or {}
    name = data.get('service', '').strip()
    action = data.get('action', '').strip()

    if not name or not action:
        return jsonify({'error': 'service and action required'}), 400

    # Sanitize service name
    import re as _re
    import shlex
    if not _re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
        return jsonify({'error': 'Invalid service name'}), 400

    # Protect critical services
    _PROTECTED = {'ethos', 'ssh', 'sshd', 'NetworkManager', 'systemd-journald', 'systemd-logind', 'dbus'}
    if name in _PROTECTED and action in ('stop', 'disable', 'uninstall'):
        return jsonify({'error': f'Service {name} is protected — cannot {action}'}), 403

    if action in ('start', 'stop', 'restart'):
        r = _host_run_base(f"systemctl {action} {shlex.quote(name)}", timeout=30)
        if r.returncode != 0:
            return jsonify({'error': f'{action} failed: {r.stderr.strip()[-200:]}'}), 500
        return jsonify({'ok': True, 'message': f'{name}: {action} OK'})

    elif action == 'enable':
        r = _host_run_base(f"systemctl enable {shlex.quote(name)}", timeout=15)
        if r.returncode != 0:
            return jsonify({'error': r.stderr.strip()[-200:]}), 500
        return jsonify({'ok': True, 'message': f'{name} enabled at startup'})

    elif action == 'disable':
        r = _host_run_base(f"systemctl disable {shlex.quote(name)}", timeout=15)
        if r.returncode != 0:
            return jsonify({'error': r.stderr.strip()[-200:]}), 500
        return jsonify({'ok': True, 'message': f'{name} disabled at startup'})

    elif action == 'uninstall':
        meta = _KNOWN_SERVICES.get(name, {})
        pkg = meta.get('pkg') or data.get('pkg', '').strip()
        if not pkg:
            return jsonify({'error': 'Package not found for uninstall'}), 400
        # Stop first
        _host_run_base(f"systemctl stop {shlex.quote(name)} 2>/dev/null", timeout=15)
        r = _host_run_base(f"apt-get remove -y {shlex.quote(pkg)}", timeout=120)
        if r.returncode != 0:
            return jsonify({'error': f'Uninstall failed: {r.stderr.strip()[-200:]}'}), 500
        return jsonify({'ok': True, 'message': f'{pkg} uninstalled'})

    elif action == 'install':
        meta = _KNOWN_SERVICES.get(name, {})
        pkg = meta.get('pkg') or data.get('pkg', '').strip()
        if not pkg:
            return jsonify({'error': 'Package not found for install'}), 400

        # Run install asynchronously with progress via SocketIO
        import gevent as _gev

        def _install_bg():
            svc_name = name
            svc_pkg = pkg
            try:
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'start', 'progress': 0,
                    'message': f'Preparing installation of {svc_pkg}…',
                })

                if svc_pkg == 'docker-ce':
                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': 'install', 'progress': 20,
                        'message': 'Installing Docker CE…',
                    })
                    from host import ensure_dep
                    ok, msg = ensure_dep('docker', install=True)
                    if not ok:
                        socketio.emit('service_install_progress', {
                            'service': svc_name, 'pkg': svc_pkg,
                            'phase': 'error', 'progress': 0,
                            'message': msg,
                        })
                        return
                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': 'done', 'progress': 100,
                        'message': 'Docker installed',
                    })
                    return

                # Phase 1: heal dpkg + apt-get update
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'update', 'progress': 5,
                    'message': 'Repairing package manager…',
                })
                _host_run_base("dpkg --configure -a 2>/dev/null", timeout=60)

                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'update', 'progress': 10,
                    'message': 'Updating package list…',
                })
                r_upd = _host_run_base("apt-get update -qq", timeout=120)
                if r_upd.returncode != 0:
                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': 'update', 'progress': 15,
                        'message': 'Updating package list (warning)…',
                        'detail': r_upd.stderr.strip()[-300:],
                    })

                # Phase 2: install via streaming
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'install', 'progress': 20,
                    'message': f'Installing {svc_pkg}…',
                })

                stream = _host_run_stream_base(
                    f"DEBIAN_FRONTEND=noninteractive apt-get install -y -o Dpkg::Use-Pty=0 {shlex.quote(svc_pkg)}"
                )
                lines_buf = []
                progress = 20
                for line in stream:
                    line_s = line.rstrip('\n')
                    if line_s.startswith('__EXIT_CODE__:'):
                        exit_code = int(line_s.split(':')[1])
                        break
                    lines_buf.append(line_s)
                    # Parse apt progress hints
                    if line_s.startswith('Get:') or line_s.startswith('Fetching'):
                        progress = min(progress + 3, 60)
                        phase_name = 'download'
                    elif 'Unpacking' in line_s:
                        progress = min(max(progress, 60), 75)
                        phase_name = 'unpack'
                    elif 'Setting up' in line_s:
                        progress = min(max(progress, 75), 90)
                        phase_name = 'configure'
                    elif 'Processing triggers' in line_s:
                        progress = 90
                        phase_name = 'triggers'
                    else:
                        phase_name = 'install'

                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': phase_name, 'progress': progress,
                        'message': line_s[:200],
                    })
                else:
                    exit_code = 1

                if exit_code != 0:
                    err_out = '\n'.join(lines_buf[-10:])
                    socketio.emit('service_install_progress', {
                        'service': svc_name, 'pkg': svc_pkg,
                        'phase': 'error', 'progress': 0,
                        'message': f'Installation failed (code {exit_code})',
                        'detail': err_out[-500:],
                    })
                    return

                # Phase 3: enable & start
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'enable', 'progress': 95,
                    'message': f'Starting {svc_name}…',
                })
                _host_run_base(f"systemctl enable {shlex.quote(svc_name)} 2>/dev/null", timeout=10)
                _host_run_base(f"systemctl start {shlex.quote(svc_name)} 2>/dev/null", timeout=10)

                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'done', 'progress': 100,
                    'message': f'{svc_pkg} installed and started',
                })

            except Exception as ex:
                socketio.emit('service_install_progress', {
                    'service': svc_name, 'pkg': svc_pkg,
                    'phase': 'error', 'progress': 0,
                    'message': f'Error: {ex}',
                })

        _gev.spawn(_install_bg)
        return jsonify({'ok': True, 'message': f'Installation of {pkg} started', 'async': True})

    else:
        return jsonify({'error': f'Unknown action: {action}'}), 400


# ── GPU Driver Installer ──────────────────────────────────────────────

@system_bp.route('/api/resources/gpu/install', methods=['POST'])
@require_auth
def gpu_driver_install():
    """Install GPU drivers detected by lspci. Streams progress via SocketIO."""
    from blueprints.monitor import detect_gpu_hardware
    data = request.get_json() or {}
    card_index = data.get('card_index', 0)

    hw = detect_gpu_hardware()
    if not hw['cards']:
        return jsonify({'error': 'No GPU card detected'}), 400
    if card_index >= len(hw['cards']):
        return jsonify({'error': 'Invalid card index'}), 400

    card = hw['cards'][card_index]
    if card.get('packages_installed') and not card.get('driver_installed'):
        socketio.emit('gpu_driver_progress', {
            'phase': 'done', 'progress': 100,
            'message': 'Drivers already installed. System restart required.',
            'vendor': card.get('vendor', 'unknown'),
            'reboot_required': True,
        })
        return jsonify({
            'ok': True,
            'message': 'Drivers already installed — restart required',
            'reboot_required': True,
        })

    if not card.get('install_cmd'):
        return jsonify({'error': f"No install instructions for {card.get('vendor', '?')}"}), 400

    import gevent as _gev

    def _gpu_install_bg():
        vendor = card['vendor']
        cmd = card['install_cmd']
        try:
            socketio.emit('gpu_driver_progress', {
                'phase': 'start', 'progress': 0,
                'message': f"Preparing {vendor.upper()} driver installation…",
                'vendor': vendor,
            })

            # Heal dpkg first
            socketio.emit('gpu_driver_progress', {
                'phase': 'update', 'progress': 5,
                'message': 'Repairing package manager…',
                'vendor': vendor,
            })
            _host_run_base("dpkg --configure -a 2>/dev/null", timeout=60)

            # Stream the install command
            socketio.emit('gpu_driver_progress', {
                'phase': 'install', 'progress': 10,
                'message': f"Installing {vendor.upper()} drivers…",
                'vendor': vendor,
            })

            full_cmd = cmd
            stream = _host_run_stream_base(full_cmd)
            lines_buf = []
            progress = 10
            exit_code = 1
            for line in stream:
                line_s = line.rstrip('\n')
                if line_s.startswith('__EXIT_CODE__:'):
                    exit_code = int(line_s.split(':')[1])
                    break
                lines_buf.append(line_s)
                if line_s.startswith('Get:') or line_s.startswith('Hit:'):
                    progress = min(progress + 2, 40)
                    phase = 'download'
                elif 'Unpacking' in line_s:
                    progress = min(max(progress, 40), 65)
                    phase = 'unpack'
                elif 'Setting up' in line_s:
                    progress = min(max(progress, 65), 85)
                    phase = 'configure'
                elif 'Processing triggers' in line_s:
                    progress = 88
                    phase = 'triggers'
                else:
                    phase = 'install'
                socketio.emit('gpu_driver_progress', {
                    'phase': phase, 'progress': progress,
                    'message': line_s[:200],
                    'vendor': vendor,
                })
            else:
                exit_code = 1

            if exit_code != 0:
                err_out = '\n'.join(lines_buf[-10:])
                socketio.emit('gpu_driver_progress', {
                    'phase': 'error', 'progress': 0,
                    'message': f'Installation failed (code {exit_code})',
                    'detail': err_out[-500:],
                    'vendor': vendor,
                })
                return

            socketio.emit('gpu_driver_progress', {
                'phase': 'done', 'progress': 100,
                'message': 'Drivers installed! System restart required.',
                'vendor': vendor,
                'reboot_required': True,
            })

        except Exception as ex:
            socketio.emit('gpu_driver_progress', {
                'phase': 'error', 'progress': 0,
                'message': f'Error: {ex}',
                'vendor': vendor,
            })

    _gev.spawn(_gpu_install_bg)
    return jsonify({'ok': True, 'message': 'Driver installation started', 'async': True})


@system_bp.route('/api/services/logs')
@require_auth
def services_logs():
    """Get recent journal logs for a service. Query: ?service=name&lines=50"""
    name = request.args.get('service', '').strip()
    try:
        lines = min(int(request.args.get('lines', '50')), 500)
    except (ValueError, TypeError):
        lines = 50
    if not name:
        return jsonify({'error': 'service required'}), 400
    import re as _re
    import shlex
    if not _re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
        return jsonify({'error': 'Invalid service name'}), 400
    r = _host_run_base(f"journalctl -u {shlex.quote(name)} --no-pager -n {lines} --output=short-iso", timeout=15)
    return jsonify({'logs': r.stdout, 'service': name})


