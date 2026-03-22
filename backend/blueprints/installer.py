"""
EthOS — System Installer Blueprint (v2 — Role-Based Drive Assignment)
=====================================================================
Handles first-boot system installation:
  • Device discovery with persistent IDs (/dev/disk/by-id/)
  • Role-based drive assignment: System Drive (OS) + Data Drive(s)
  • SMART pre-flight checks
  • Typed confirmation ("INSTALUJ") for destructive operations
  • Validation (size, RAID, overlap, boot-medium protection)
  • System installation (partitioning, cloning, GRUB)
  • installer_result.json handover contract for firstboot/setup

Architecture:
  The installer runs BEFORE the normal setup wizard.  Once the system
  disk is finalised the setup wizard handles user/hostname/network.
  Drive references use persistent IDs from /dev/disk/by-id/ to guard
  against hotplug device reordering during the wizard.
"""

import json
import os
import re
import shutil
import subprocess
import shlex
import threading
import time
from pathlib import Path
from flask import Blueprint, jsonify, request, Response, stream_with_context

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, data_path, ETHOS_ROOT
from utils import load_json as _load_json, save_json as _save_json

installer_bp = Blueprint('installer', __name__, url_prefix='/api/installer')

# ─────────────────────── State management ───────────────────────

_INSTALLER_STATE_FILE = data_path('installer_state.json')
_INSTALLER_RESULT_FILE = data_path('installer_result.json')
_MIN_SYSTEM_DISK_GB = 16
_MIN_DATA_DISK_GB = 1
_CONFIRMATION_TOKEN = 'INSTALUJ'

_install_state = {
    'status': 'idle',          # idle | running | done | error
    'phase': '',               # discovery | partitioning | cloning | grub | verify | complete
    'percent': 0,
    'message': '',
    'logs': [],
    'start_time': 0,
    'result': None,            # {success, message} on completion
    'speed': 0,                # MB/s during cloning
    'strategy': '',            # usb | internal
    'target_device': '',
    'data_disks': [],          # list of data disk devices
}
_install_lock = threading.Lock()
_install_thread = None


def _save_state():
    try:
        _save_json(_INSTALLER_STATE_FILE, _install_state)
    except Exception:
        pass


def _load_state():
    global _install_state
    try:
        saved = _load_json(_INSTALLER_STATE_FILE, None)
        if saved and isinstance(saved, dict):
            _install_state.update(saved)
    except Exception:
        pass
    # If installer_result.json exists (written by preboot-server or prior install),
    # treat installation as done even if installer_state.json is missing/idle.
    if _install_state['status'] == 'idle' and os.path.isfile(_INSTALLER_RESULT_FILE):
        _install_state['status'] = 'done'
        _install_state['phase'] = 'complete'
        _install_state['percent'] = 100
        _install_state['message'] = 'Instalacja zakończona (preboot)'


def _log(msg):
    ts = time.strftime('%H:%M:%S')
    entry = f'[{ts}] {msg}'
    _install_state['logs'].append(entry)
    if len(_install_state['logs']) > 500:
        _install_state['logs'] = _install_state['logs'][-400:]
    _install_state['message'] = msg
    _save_state()


def _set_phase(phase, percent, msg):
    _install_state['phase'] = phase
    _install_state['percent'] = percent
    _install_state['message'] = msg
    _log(msg)


def _sp_run(cmd, timeout=30, **kw):
    """Safe subprocess.run wrapper."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


# ─────────────────────── Persistent ID helpers ───────────────────────

def _get_persistent_id(dev_name):
    """Get a stable /dev/disk/by-id/ identifier for a block device.
    Falls back to serial-based or device name if no by-id link exists."""
    by_id_dir = '/dev/disk/by-id'
    if os.path.isdir(by_id_dir):
        try:
            for entry in os.listdir(by_id_dir):
                # Skip partition symlinks (contain -partN suffix)
                if re.search(r'-part\d+$', entry):
                    continue
                link_target = os.path.realpath(os.path.join(by_id_dir, entry))
                if link_target == f'/dev/{dev_name}':
                    return entry
        except OSError:
            pass
    return dev_name


def _resolve_persistent_id(persistent_id):
    """Resolve a persistent ID back to a current /dev/sdX path.
    If the ID is already a device name (fallback), returns /dev/<name>."""
    if persistent_id.startswith('/dev/'):
        return persistent_id
    by_id_path = f'/dev/disk/by-id/{persistent_id}'
    if os.path.exists(by_id_path):
        return os.path.realpath(by_id_path)
    # Fallback: try direct device name
    dev_path = f'/dev/{persistent_id}'
    if os.path.exists(dev_path):
        return dev_path
    return None


def _get_smart_status(dev_path):
    """Quick SMART health check. Returns 'PASSED', 'FAILED', or 'UNKNOWN'."""
    try:
        r = _sp_run(['smartctl', '-H', dev_path], timeout=10)
        if 'PASSED' in r.stdout:
            return 'PASSED'
        elif 'FAILED' in r.stdout:
            return 'FAILED'
        return 'UNKNOWN'
    except FileNotFoundError:
        return 'UNKNOWN'
    except Exception:
        return 'UNKNOWN'


def _get_smart_temp(dev_path):
    """Get drive temperature from SMART data. Returns int or None."""
    try:
        r = _sp_run(['smartctl', '-A', dev_path], timeout=10)
        # Look for Temperature_Celsius or Airflow_Temperature
        for line in r.stdout.splitlines():
            if 'Temperature' in line and ('Celsius' in line or 'Airflow' in line):
                parts = line.split()
                for p in reversed(parts):
                    try:
                        t = int(p)
                        if 0 < t < 120:
                            return t
                    except ValueError:
                        continue
        # NVME: look for "Temperature:" line
        for line in r.stdout.splitlines():
            m = re.search(r'Temperature:\s+(\d+)\s*Celsius', line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


# ─────────────────────── 1. Device Discovery ───────────────────────

def _discover_devices():
    """Return structured list of all block devices suitable for installation.

    Each device dict contains:
        id (persistent), device, name, model, size, size_gb, transport,
        connection_type, removable, is_boot_medium, is_raid_member,
        partitions[], fstype_summary, has_ethos_root, smart_status,
        temperature_c, eligible_roles[]
    """
    try:
        r = _sp_run(
            ['lsblk', '-J', '-b', '-o',
             'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,TRAN,RO,RM,LABEL,UUID,SERIAL'],
            timeout=10)
        data = json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        data = {}

    block_devs = data.get('blockdevices', [])

    # Identify the disk that has / mounted
    system_disk_names = set()

    def _find_system(devs, parent=None):
        for d in devs:
            mp = d.get('mountpoint') or ''
            if mp == '/':
                system_disk_names.add(parent or d['name'])
            for c in d.get('children', []):
                if (c.get('mountpoint') or '') == '/':
                    system_disk_names.add(d['name'])
            _find_system(d.get('children', []), parent or d.get('name'))

    _find_system(block_devs)

    # Check for RAID membership via mdadm
    raid_members = set()
    try:
        r = _sp_run(['mdadm', '--detail', '--scan'], timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            # Also check /proc/mdstat for component devices
            try:
                mdstat = Path('/proc/mdstat').read_text()
                for line in mdstat.splitlines():
                    if line.startswith('md'):
                        # e.g. md0 : active raid1 sdb1[1] sda1[0]
                        for part in line.split():
                            m = re.match(r'([a-z]+\d*)\[\d+\]', part)
                            if m:
                                # strip partition number to get parent disk
                                dev_name = re.sub(r'p?\d+$', '', m.group(1))
                                raid_members.add(dev_name)
            except Exception:
                pass
    except FileNotFoundError:
        pass  # mdadm not installed
    except Exception:
        pass

    # Detect USB boot source (the device currently running the system)
    boot_source_disk = ''
    try:
        r = _sp_run(['findmnt', '-no', 'SOURCE', '/'], timeout=5)
        root_src = r.stdout.strip()  # e.g. /dev/sda3
        boot_source_disk = re.sub(r'p?\d+$', '', root_src.replace('/dev/', ''))
    except Exception:
        pass

    # Detect if the system was booted from USB
    booted_from_usb = False
    if boot_source_disk:
        for d in block_devs:
            if d.get('name') == boot_source_disk:
                booted_from_usb = (d.get('tran') or '').lower() == 'usb' or bool(d.get('rm'))
                break

    devices = []
    for dev in block_devs:
        if dev.get('type') != 'disk':
            continue
        if dev.get('ro'):
            continue
        name = dev.get('name', '')
        if name.startswith(('loop', 'sr', 'fd', 'zram')):
            continue
        size = dev.get('size') or 0
        if size < 500_000_000:  # Skip < 500 MB
            continue

        is_system = name in system_disk_names
        is_boot_medium = name == boot_source_disk
        children = dev.get('children', [])
        tran = (dev.get('tran') or '').lower()
        removable = bool(dev.get('rm'))
        is_usb = tran == 'usb' or removable

        # Persistent ID
        persistent_id = _get_persistent_id(name)

        # Build partition list
        partitions = []
        has_ethos_root = False
        for child in children:
            if child.get('type') not in ('part', 'crypt'):
                continue
            label = child.get('label') or ''
            if label in ('ethos-root', 'EthOS-Root'):
                has_ethos_root = True
            partitions.append({
                'name': child.get('name', ''),
                'device': f"/dev/{child.get('name', '')}",
                'size': child.get('size') or 0,
                'fstype': child.get('fstype') or '',
                'mountpoint': child.get('mountpoint') or '',
                'label': label,
            })

        # FS summary
        fstypes = list({p['fstype'] for p in partitions if p['fstype']})
        fstype_summary = ', '.join(fstypes) if fstypes else 'brak partycji'

        # connection type
        if is_usb:
            connection_type = 'USB'
        elif tran == 'nvme':
            connection_type = 'NVMe'
        elif tran in ('sata', 'ata'):
            connection_type = 'SATA'
        elif tran == 'sas':
            connection_type = 'SAS'
        else:
            connection_type = tran.upper() if tran else 'Wewnętrzny'

        size_gb = round(size / 1e9, 1)

        # SMART health (non-blocking, best-effort)
        dev_path = f'/dev/{name}'
        smart_status = _get_smart_status(dev_path)
        temperature_c = _get_smart_temp(dev_path)

        # Compute eligible roles
        eligible_roles = []
        if not is_boot_medium:
            if size_gb >= _MIN_SYSTEM_DISK_GB:
                eligible_roles.append('system')
            if size_gb >= _MIN_DATA_DISK_GB:
                eligible_roles.append('data')

        devices.append({
            'id': persistent_id,
            'device': dev_path,
            'name': name,
            'model': (dev.get('model') or '').strip() or name,
            'serial': (dev.get('serial') or '').strip(),
            'size': size,
            'size_gb': size_gb,
            'transport': tran,
            'connection_type': connection_type,
            'removable': removable,
            'is_usb': is_usb,
            'is_system': is_system,
            'is_boot_medium': is_boot_medium,
            'is_boot_source': is_boot_medium,  # compat alias
            'is_raid_member': name in raid_members,
            'partitions': partitions,
            'fstype_summary': fstype_summary,
            'has_ethos_root': has_ethos_root,
            'smart_status': smart_status,
            'temperature_c': temperature_c,
            'eligible_roles': eligible_roles,
            'warnings': [],
        })

    # Add warnings
    for d in devices:
        if d['is_raid_member']:
            d['warnings'].append('raid')
        if d['size_gb'] < _MIN_SYSTEM_DISK_GB:
            d['warnings'].append('low_space')
        if d['smart_status'] == 'FAILED':
            d['warnings'].append('smart_failed')

    # Sort: internal first, then by size descending
    devices.sort(key=lambda d: (d['is_usb'], -d['size']))

    # Boot medium info
    boot_medium = None
    for d in devices:
        if d['is_boot_medium']:
            boot_medium = {'id': d['id'], 'device': d['device'], 'model': d['model']}
            break

    # Detect platform + EFI
    try:
        r = _sp_run(['uname', '-m'], timeout=5)
        platform = r.stdout.strip()
    except Exception:
        platform = 'unknown'
    has_efi = os.path.isdir('/sys/firmware/efi')

    return {
        'devices': devices,
        'boot_source_disk': f"/dev/{boot_source_disk}" if boot_source_disk else '',
        'booted_from_usb': booted_from_usb,
        'boot_medium': boot_medium,
        'platform': platform,
        'has_efi': has_efi,
    }


# ─────────────────────── 2. Validation ───────────────────────

def _validate_plan(strategy, target_device, data_disks, devices_map, boot_medium_id=''):
    """Validate an installation plan. Returns (warnings: list, errors: list).
    
    devices_map can be keyed by persistent ID or by device path.
    boot_medium_id is the persistent ID of the boot medium (USB that booted the system).
    """
    warnings = []
    errors = []

    if strategy not in ('usb', 'internal'):
        errors.append('Nieprawidłowa strategia bootowania')
        return warnings, errors

    if strategy == 'internal':
        if not target_device:
            errors.append('Nie wybrano dysku docelowego')
            return warnings, errors

        dev = devices_map.get(target_device)
        if not dev:
            errors.append(f'Dysk {target_device} nie znaleziony')
            return warnings, errors

        # Hard block: cannot install on boot medium
        if dev.get('is_boot_medium'):
            errors.append('Nie można zainstalować systemu na nośniku startowym (USB boot)')
            return warnings, errors

        # Hard block: system drive cannot also be a data drive
        target_id = dev.get('id', target_device)
        for dd_dev in data_disks:
            dd = devices_map.get(dd_dev, {})
            if dd.get('id', dd_dev) == target_id:
                errors.append('Dysk nie może być jednocześnie systemowy i danych')
                return warnings, errors

        if dev['is_usb']:
            warnings.append(f'{target_device} jest dyskiem USB — instalacja na dysku wewnętrznym jest zalecana')

        if dev['size_gb'] < _MIN_SYSTEM_DISK_GB:
            warnings.append(f'{target_device} ma tylko {dev["size_gb"]} GB — wymagane minimum {_MIN_SYSTEM_DISK_GB} GB')

        if dev['is_raid_member']:
            warnings.append(f'UWAGA: {target_device} jest członkiem macierzy RAID! Usunięcie danych może uszkodzić macierz.')

        if dev.get('smart_status') == 'FAILED':
            warnings.append(f'SMART wykrył problemy z {target_device} — ryzyko awarii dysku')

    # Validate data disks
    for dd_dev in data_disks:
        dd = devices_map.get(dd_dev)
        if not dd:
            errors.append(f'Dysk danych {dd_dev} nie znaleziony')
            continue
        if dd.get('is_boot_medium'):
            errors.append(f'Dysk danych {dd_dev} jest nośnikiem startowym (USB boot)')
            continue
        if strategy == 'internal' and dd_dev == target_device:
            # Can't use same disk for system and data (will be handled by data partition)
            continue
        if dd['is_raid_member']:
            warnings.append(f'Dysk danych {dd_dev} jest członkiem RAID')
        if dd['size_gb'] < _MIN_DATA_DISK_GB:
            warnings.append(f'Dysk danych {dd_dev} ma tylko {dd["size_gb"]} GB')
        if dd.get('smart_status') == 'FAILED':
            warnings.append(f'SMART wykrył problemy z dyskiem danych {dd_dev} — ryzyko awarii')

    # Overall warnings
    if not data_disks and strategy == 'internal':
        warnings.append('Brak dysku danych — pliki użytkownika będą na partycji danych dysku systemowego')

    return warnings, errors


def _generate_summary(strategy, target_device, data_disks, devices_map, booted_from_usb):
    """Generate human-readable summary of planned actions.
    Also returns structured plan for the frontend."""
    lines = []
    plan = {'system': None, 'data': []}

    if strategy == 'usb':
        boot_dev = devices_map.get(booted_from_usb, {})
        boot_label = boot_dev.get('model', 'USB') if boot_dev else 'USB'
        lines.append(f'System pozostanie na nośniku USB ({boot_label}).')
        lines.append('Partycja persistence zostanie utworzona/zachowana na USB dla konfiguracji.')
    else:
        dev = devices_map.get(target_device, {})
        label = dev.get('model', target_device)
        size = dev.get('size_gb', '?')
        lines.append(f'System zostanie zainstalowany na: {label} ({size} GB)')
        lines.append(f'Nośnik {target_device} zostanie CAŁKOWICIE wyczyszczony.')
        lines.append('Partycje: EFI (256 MB) + BIOS grub (1 MB) + rootfs (reszta)')
        plan['system'] = {
            'device': target_device,
            'id': dev.get('id', ''),
            'model': label,
            'action': 'WIPE + GPT + ESP + rootfs',
            'partitions': [
                {'label': 'ESP', 'size_mb': 256, 'fs': 'vfat'},
                {'label': 'BIOS-GRUB', 'size_mb': 1, 'fs': None},
                {'label': 'ethos-root', 'size_pct': 100, 'fs': 'ext4'},
            ]
        }

    if data_disks:
        dd_labels = []
        for i, dd_dev in enumerate(data_disks, 1):
            dd = devices_map.get(dd_dev, {})
            dd_model = dd.get('model', dd_dev)
            dd_size = dd.get('size_gb', '?')
            dd_labels.append(f'{dd_model} ({dd_size} GB)')
            plan['data'].append({
                'device': dd_dev,
                'id': dd.get('id', ''),
                'model': dd_model,
                'action': f'WIPE + GPT + single ext4 (EthOS-Data-{i})',
            })
        lines.append(f'Dyski danych: {", ".join(dd_labels)}')
        lines.append('UWAGA: Wszystkie dane na wybranych dyskach danych zostaną usunięte!')
    else:
        if strategy == 'internal':
            lines.append('Dane użytkownika na partycji danych dysku systemowego (wolne miejsce).')
        else:
            lines.append('Dane użytkownika na dysku systemowym (USB).')

    return '\n'.join(lines), plan


# ─────────────────────── 3. Installation Worker ───────────────────────

def _install_worker(strategy, target_device, data_disks, encrypt, passphrase,
                    plan_meta=None):
    """Background worker that performs the actual installation.

    All device paths here are already resolved /dev/sdX paths
    (persistent IDs were resolved in api_execute before launching).
    """
    global _install_state
    result_data = {
        'system_device': target_device,
        'data_devices': [],
        'strategy': strategy,
        'encrypt': bool(encrypt),
    }
    try:
        _install_state['status'] = 'running'
        _install_state['start_time'] = time.time()
        _install_state['strategy'] = strategy
        _install_state['target_device'] = target_device
        _install_state['data_disks'] = data_disks
        _save_state()

        if strategy == 'usb':
            _install_usb_persistent()
        else:
            _install_to_internal(target_device, encrypt, passphrase)

        # Prepare data disks with sequential labels
        for i, dd_dev in enumerate(data_disks):
            pct = 85 + int(15 * (i / max(len(data_disks), 1)))
            _set_phase('data_disks', pct,
                       f'Przygotowuję dysk danych {i+1}/{len(data_disks)}: {dd_dev}')
            label = f'EthOS-Data-{i+1}'
            _prepare_data_disk(dd_dev, label=label)
            result_data['data_devices'].append({
                'device': dd_dev,
                'label': label,
                'id': plan_meta['data_disk_ids'][i] if plan_meta else '',
            })

        # Write installer_result.json for handover to setup wizard / firstboot
        _write_installer_result(result_data, plan_meta)

        _install_state['status'] = 'done'
        _install_state['percent'] = 100
        _install_state['result'] = {'success': True, 'message': 'Instalacja zakończona pomyślnie'}
        _install_state['phase'] = 'complete'
        _log('Instalacja zakończona pomyślnie!')
        _save_state()

    except Exception as e:
        _install_state['status'] = 'error'
        _install_state['result'] = {'success': False, 'message': str(e)}
        _log(f'BŁĄD: {e}')
        _save_state()


def _install_usb_persistent():
    """Keep running from USB — ensure persistence partition exists."""
    _set_phase('partitioning', 5, 'Sprawdzam partycję persistence na USB...')

    # Find the boot source device
    r = _sp_run(['findmnt', '-no', 'SOURCE', '/'], timeout=5)
    root_src = r.stdout.strip()
    root_disk = re.sub(r'p?\d+$', '', root_src)

    # Check if there's already a persistence/data partition
    r = _sp_run(['lsblk', '-nlo', 'NAME,LABEL,FSTYPE', root_disk], timeout=5)
    has_persistence = False
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ('persistence', 'EthOS-Data', 'ethos-data'):
            has_persistence = True
            break

    if has_persistence:
        _set_phase('complete', 85, 'Partycja persistence już istnieje na USB.')
        _log('USB persistence OK — partycja znaleziona.')
        return

    # Check for free space on the USB drive
    _set_phase('partitioning', 15, 'Sprawdzam wolne miejsce na USB...')
    r = _sp_run(['parted', '-ms', root_disk, 'unit', 'B', 'print', 'free'], timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd odczytu partycji USB: {r.stderr.strip()}')

    free_start = free_end = None
    best_size = 0
    for line in r.stdout.splitlines():
        if ':free;' in line:
            fields = line.split(':')
            try:
                fs = int(fields[1].rstrip('B'))
                fe = int(fields[2].rstrip('B'))
                sz = fe - fs
                if sz > best_size:
                    best_size = sz
                    free_start, free_end = fs, fe
            except (ValueError, IndexError):
                pass

    if free_start is None or best_size < 500_000_000:
        _log('Brak wolnego miejsca na USB — dane będą przechowywane na rootfs.')
        _set_phase('complete', 85, 'USB gotowy (brak wolnego miejsca na persistence).')
        return

    # Create persistence partition
    _set_phase('partitioning', 25, 'Tworzę partycję persistence na USB...')

    # Find highest partition number
    r = _sp_run(['parted', '-ms', root_disk, 'unit', 'B', 'print'], timeout=10)
    max_num = 0
    for line in r.stdout.splitlines():
        if line and line[0].isdigit():
            try:
                max_num = max(max_num, int(line.split(':')[0]))
            except (ValueError, IndexError):
                pass
    new_num = max_num + 1

    r = _sp_run(
        ['parted', '-s', root_disk, 'mkpart', 'primary', 'ext4',
         f'{free_start}B', f'{free_end}B'],
        timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd tworzenia partycji: {r.stderr.strip()}')

    _sp_run(['udevadm', 'settle', '--timeout=10'], timeout=15)
    time.sleep(1)

    # Determine partition device
    if 'mmcblk' in root_disk or 'nvme' in root_disk:
        part_dev = f"{root_disk}p{new_num}"
    else:
        part_dev = f"{root_disk}{new_num}"

    if not os.path.exists(part_dev):
        _sp_run(['partprobe', root_disk], timeout=10)
        _sp_run(['udevadm', 'settle', '--timeout=5'], timeout=10)
        time.sleep(1)

    if not os.path.exists(part_dev):
        raise RuntimeError(f'Partycja {part_dev} nie pojawiła się')

    # Format
    _set_phase('partitioning', 40, f'Formatuję {part_dev} jako EthOS-Data...')
    r = _sp_run(['mkfs.ext4', '-F', '-L', 'EthOS-Data', part_dev], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd formatowania: {r.stderr.strip()}')

    # Mount
    os.makedirs('/mnt/data', mode=0o755, exist_ok=True)
    r = _sp_run(['mount', part_dev, '/mnt/data'], timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd montowania: {r.stderr.strip()}')

    _set_phase('complete', 85, 'USB persistence utworzone i zamontowane.')
    _log(f'Partycja {part_dev} (EthOS-Data) gotowa.')


def _install_to_internal(target_device, encrypt=False, passphrase=''):
    """Clone system from USB/current rootfs onto an internal disk."""
    _set_phase('partitioning', 5, f'Przygotowuję {target_device}...')

    # Safety: don't destroy current root if target IS the boot source
    r = _sp_run(['findmnt', '-no', 'SOURCE', '/'], timeout=5)
    root_src = r.stdout.strip()
    root_disk = re.sub(r'p?\d+$', '', root_src)
    if target_device == root_disk:
        raise RuntimeError('Nie można zainstalować na aktywnym dysku systemowym!')

    # 1. Unmount all partitions on target
    _log(f'Odmontowuję partycje na {target_device}...')
    r = _sp_run(['lsblk', '-nlo', 'NAME,MOUNTPOINT', target_device], timeout=5)
    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) >= 2 and parts[1].strip():
            _sp_run(['umount', '-f', parts[1].strip()], timeout=10)

    # 2. Wipe
    _set_phase('partitioning', 10, f'Czyszczę {target_device}...')
    _sp_run(['wipefs', '-af', target_device], timeout=30)

    # 3. Create partition table (GPT)
    # Layout: p1=ESP(256MB), p2=BIOS grub(1MB), p3=rootfs(rest)
    _set_phase('partitioning', 15, 'Tworzę tablicę partycji GPT...')
    cmds = [
        ['parted', '-s', target_device, 'mklabel', 'gpt'],
        ['parted', '-s', target_device, 'mkpart', 'ESP', 'fat32', '1MiB', '257MiB'],
        ['parted', '-s', target_device, 'set', '1', 'esp', 'on'],
        ['parted', '-s', target_device, 'mkpart', 'primary', '257MiB', '258MiB'],
        ['parted', '-s', target_device, 'set', '2', 'bios_grub', 'on'],
        ['parted', '-s', target_device, 'mkpart', 'primary', 'ext4', '258MiB', '100%'],
    ]
    for cmd in cmds:
        r = _sp_run(cmd, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f'Błąd partycjonowania: {" ".join(cmd)} → {r.stderr.strip()}')

    _sp_run(['udevadm', 'settle', '--timeout=10'], timeout=15)
    time.sleep(1)

    # Determine partition devices
    if 'mmcblk' in target_device or 'nvme' in target_device:
        p1 = f"{target_device}p1"
        p3 = f"{target_device}p3"
    else:
        p1 = f"{target_device}1"
        p3 = f"{target_device}3"

    if not os.path.exists(p1):
        _sp_run(['partprobe', target_device], timeout=10)
        _sp_run(['udevadm', 'settle', '--timeout=5'], timeout=10)
        time.sleep(1)

    for pdev in (p1, p3):
        if not os.path.exists(pdev):
            raise RuntimeError(f'Partycja {pdev} nie pojawiła się')

    # 4. Format partitions
    _set_phase('partitioning', 25, 'Formatuję partycje...')
    r = _sp_run(['mkfs.vfat', '-F32', p1], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd formatowania ESP: {r.stderr.strip()}')

    root_dev = p3
    if encrypt:
        if shutil.which('cryptsetup') is None:
            raise RuntimeError('Brak programu cryptsetup. Zainstaluj pakiet cryptsetup lub wyłącz szyfrowanie LUKS.')
        _set_phase('partitioning', 30, 'Szyfrowanie LUKS...')
        os.makedirs('/etc/ethos', mode=0o700, exist_ok=True)
        keyfile = '/etc/ethos/luks_system.key'
        _sp_run(['dd', 'if=/dev/urandom', f'of={keyfile}', 'bs=4096', 'count=1'], timeout=10)
        os.chmod(keyfile, 0o600)

        r = _sp_run(['cryptsetup', 'luksFormat', '--batch-mode', '--key-file', keyfile, p3], timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f'Błąd LUKS format: {r.stderr.strip()}')

        if passphrase:
            r = _sp_run(['cryptsetup', 'luksAddKey', '--key-file', keyfile, p3],
                        input=passphrase, timeout=60)
            if r.returncode != 0:
                _log(f'Ostrzeżenie: nie udało się dodać hasła awaryjnego: {r.stderr.strip()}')

        r = _sp_run(['cryptsetup', 'luksOpen', '--key-file', keyfile, p3, 'ethos_root'], timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f'Błąd LUKS open: {r.stderr.strip()}')

        root_dev = '/dev/mapper/ethos_root'

    r = _sp_run(['mkfs.ext4', '-F', '-L', 'ethos-root', root_dev], timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd formatowania rootfs: {r.stderr.strip()}')

    # 5. Mount target
    _set_phase('cloning', 35, 'Montuję partycje docelowe...')
    target_root = '/mnt/installer_target'
    os.makedirs(target_root, mode=0o755, exist_ok=True)
    r = _sp_run(['mount', root_dev, target_root], timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd montowania rootfs: {r.stderr.strip()}')

    efi_mount = os.path.join(target_root, 'boot/efi')
    os.makedirs(efi_mount, mode=0o755, exist_ok=True)
    r = _sp_run(['mount', p1, efi_mount], timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd montowania ESP: {r.stderr.strip()}')

    try:
        # 6. Clone rootfs using rsync
        _set_phase('cloning', 40, 'Kopiuję system (rsync)... to zajmie kilka minut')
        _log('rsync / → ' + target_root)

        exclude_list = [
            '/proc', '/sys', '/dev', '/run', '/tmp',
            '/mnt', '/media', '/lost+found',
            '/boot/efi/*',     # will be populated by grub
            '/swap*', '/swapfile',
            target_root,       # don't recurse into mount
        ]
        exclude_args = []
        for ex in exclude_list:
            exclude_args.extend(['--exclude', ex])

        rsync_cmd = [
            'rsync', '-aAXH', '--info=progress2', '--no-inc-recursive',
        ] + exclude_args + ['/', target_root + '/']

        # Run rsync and track progress
        proc = subprocess.Popen(
            rsync_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)

        last_pct = 40
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            # rsync progress2 output:   1,234,567  12%   45.67MB/s   0:01:23
            m = re.search(r'(\d+)%', line)
            if m:
                rsync_pct = int(m.group(1))
                # Map rsync 0-100% to our 40-75%
                cur_pct = 40 + int(rsync_pct * 0.35)
                if cur_pct > last_pct:
                    last_pct = cur_pct
                    _install_state['percent'] = cur_pct
                    speed_m = re.search(r'([\d.]+)\s*MB/s', line)
                    if speed_m:
                        _install_state['speed'] = float(speed_m.group(1))
                    _save_state()

        proc.wait(timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f'rsync zakończył się kodem {proc.returncode}')

        # Ensure critical dirs exist on target
        for d in ['proc', 'sys', 'dev', 'run', 'tmp', 'mnt', 'media']:
            os.makedirs(os.path.join(target_root, d), mode=0o755, exist_ok=True)

        # 7. Fix fstab on target
        _set_phase('grub', 76, 'Konfiguruję fstab...')
        root_uuid = _get_uuid(root_dev)
        efi_uuid = _get_uuid(p1)

        if not root_uuid:
            raise RuntimeError(f'Nie udało się odczytać UUID partycji root: {root_dev}')

        fstab_lines = [
            '# EthOS System Disk (auto-generated by installer)',
            f'UUID={root_uuid}  /         ext4  defaults,noatime,errors=remount-ro  0  1',
        ]
        if efi_uuid:
            fstab_lines.append(f'UUID={efi_uuid}   /boot/efi vfat  umask=0077                 0  1')
        fstab_lines.extend([
            'tmpfs        /tmp      tmpfs defaults,noatime,nosuid      0  0',
            '',
        ])

        # Defensive: keep a single entry per mountpoint in case future edits append lines.
        unique_lines = []
        seen_mounts = set()
        for line in fstab_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                unique_lines.append(line)
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                mnt = parts[1]
                if mnt in seen_mounts:
                    continue
                seen_mounts.add(mnt)
            unique_lines.append(line)

        fstab_path = os.path.join(target_root, 'etc/fstab')
        with open(fstab_path, 'w') as f:
            f.write('\n'.join(unique_lines))
        _log(f'fstab zapisany (root UUID={root_uuid})')

        # 8. Install GRUB
        _set_phase('grub', 80, 'Instaluję bootloader (GRUB)...')
        _install_grub(target_root, target_device, root_dev, p1)

    finally:
        # Cleanup mounts
        _sp_run(['umount', '-R', target_root], timeout=30)
        if encrypt:
            _sp_run(['cryptsetup', 'close', 'ethos_root'], timeout=15)

    _set_phase('verify', 84, 'Weryfikuję instalację...')
    # Quick verify: mount target, check for /etc/fstab and /sbin/init
    # Skip verify mount if encrypted — we closed LUKS above
    if not encrypt:
        r = _sp_run(['mount', root_dev, target_root], timeout=15)
        if r.returncode == 0:
            ok = os.path.exists(os.path.join(target_root, 'etc/fstab'))
            ok = ok and (os.path.exists(os.path.join(target_root, 'sbin/init'))
                         or os.path.exists(os.path.join(target_root, 'usr/sbin/init'))
                         or os.path.islink(os.path.join(target_root, 'sbin/init')))
            _sp_run(['umount', target_root], timeout=15)
            if ok:
                _log('Weryfikacja OK — system zainstalowany poprawnie')
            else:
                _log('OSTRZEŻENIE: weryfikacja niekompletna (brak init lub fstab)')
        else:
            _log('Pominięto weryfikację — nie udało się zamontować')
    else:
        _log('Pominięto weryfikację (LUKS zamknięty)')

    _set_phase('complete', 85, 'Instalacja systemu zakończona.')


def _get_uuid(dev_path):
    """Get UUID for a device using blkid."""
    try:
        r = _sp_run(['blkid', '-s', 'UUID', '-o', 'value', dev_path], timeout=5)
        return r.stdout.strip()
    except Exception:
        return ''


def _install_grub(target_root, target_device, root_dev, efi_part):
    """Install GRUB for both BIOS and UEFI boot."""
    # Bind-mount required filesystem trees
    for fs in ['proc', 'sys', 'dev', 'run']:
        src = f'/{fs}'
        dst = os.path.join(target_root, fs)
        os.makedirs(dst, exist_ok=True)
        _sp_run(['mount', '--bind', src, dst], timeout=10)

    try:
        # Detect architecture
        r = _sp_run(['uname', '-m'], timeout=5)
        arch = r.stdout.strip()

        root_uuid = _get_uuid(root_dev)

        if arch in ('x86_64', 'i686'):
            # BIOS (i386-pc) install to MBR/GPT
            _log('Instaluję GRUB (BIOS i386-pc)...')
            r = _sp_run(
                ['chroot', target_root, 'grub-install',
                 '--target=i386-pc', '--boot-directory=/boot',
                 target_device],
                timeout=60)
            if r.returncode != 0:
                _log(f'GRUB BIOS: {r.stderr.strip()}')

            # UEFI (x86_64-efi)
            _log('Instaluję GRUB (UEFI x86_64-efi)...')
            r = _sp_run(
                ['chroot', target_root, 'grub-install',
                 '--target=x86_64-efi', '--efi-directory=/boot/efi',
                 '--boot-directory=/boot', '--removable',
                 '--no-nvram'],
                timeout=60)
            if r.returncode != 0:
                _log(f'GRUB UEFI: {r.stderr.strip()}')

        elif arch.startswith('aarch64') or arch.startswith('arm'):
            _log('Architektura ARM — pomijam GRUB (U-Boot/DTB).')
            return

        # Generate grub.cfg
        _log('Generuję grub.cfg...')
        r = _sp_run(
            ['chroot', target_root, 'update-grub'],
            timeout=60)
        if r.returncode != 0:
            _log(f'update-grub: {r.stderr.strip()}')
            # Fallback: write minimal grub.cfg
            grub_cfg = os.path.join(target_root, 'boot/grub/grub.cfg')
            os.makedirs(os.path.dirname(grub_cfg), exist_ok=True)
            with open(grub_cfg, 'w') as f:
                f.write(f'''# EthOS minimal GRUB config
set default=0
set timeout=3

menuentry "EthOS" {{
    search --no-floppy --fs-uuid --set=root {root_uuid}
    linux /boot/vmlinuz root=UUID={root_uuid} ro quiet
    initrd /boot/initrd.img
}}
''')
            _log('Wygenerowano minimalny grub.cfg')

    finally:
        # Unmount bind mounts (reverse order)
        for fs in reversed(['proc', 'sys', 'dev', 'run']):
            _sp_run(['umount', '-l', os.path.join(target_root, fs)], timeout=10)


def _write_installer_result(result_data, plan_meta=None):
    """Write installer_result.json — the handover contract for firstboot/setup.

    This file is read by setup.js and firstboot.sh to know what
    the installer already did, so they can skip redundant steps.
    """
    import json as _json

    payload = {
        'version': 2,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'strategy': result_data.get('strategy', ''),
        'system_device': result_data.get('system_device', ''),
        'data_devices': result_data.get('data_devices', []),
        'encrypt': result_data.get('encrypt', False),
    }
    if plan_meta:
        payload['plan'] = {
            'target_device_id': plan_meta.get('target_device_id', ''),
            'data_disk_ids': plan_meta.get('data_disk_ids', []),
        }

    try:
        os.makedirs(os.path.dirname(_INSTALLER_RESULT_FILE), exist_ok=True)
        with open(_INSTALLER_RESULT_FILE, 'w') as f:
            _json.dump(payload, f, indent=2)
        _log(f'Zapisano installer_result.json → {_INSTALLER_RESULT_FILE}')
    except Exception as e:
        _log(f'OSTRZEŻENIE: nie udało się zapisać installer_result.json: {e}')


def _prepare_data_disk(device, label='EthOS-Data'):
    """Format and prepare a data disk with a given label (sequential)."""
    _log(f'Przygotowuję dysk danych: {device} (label={label})')

    # Unmount existing
    r = _sp_run(['lsblk', '-nlo', 'NAME,MOUNTPOINT', device], timeout=5)
    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) >= 2 and parts[1].strip():
            _sp_run(['umount', '-f', parts[1].strip()], timeout=10)

    # Wipe + partition
    _sp_run(['wipefs', '-af', device], timeout=30)
    r = _sp_run(['parted', '-s', device, 'mklabel', 'gpt'], timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd tablicy partycji na {device}: {r.stderr.strip()}')

    r = _sp_run(['parted', '-s', device, 'mkpart', 'primary', 'ext4', '1MiB', '100%'], timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd partycjonowania {device}: {r.stderr.strip()}')

    _sp_run(['udevadm', 'settle', '--timeout=10'], timeout=15)
    time.sleep(1)

    # Determine partition
    if 'mmcblk' in device or 'nvme' in device:
        part = f"{device}p1"
    else:
        part = f"{device}1"

    if not os.path.exists(part):
        _sp_run(['partprobe', device], timeout=10)
        _sp_run(['udevadm', 'settle', '--timeout=5'], timeout=10)
        time.sleep(1)

    if not os.path.exists(part):
        raise RuntimeError(f'Partycja {part} nie pojawiła się')

    r = _sp_run(['mkfs.ext4', '-F', '-L', label, part], timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f'Błąd formatowania {part}: {r.stderr.strip()}')

    _log(f'Dysk danych {device} → {part} ({label}) gotowy')


# ─────────────────────── API Endpoints ───────────────────────

@installer_bp.route('/discover')
def api_discover():
    """Discover all block devices. No auth required (pre-setup)."""
    try:
        result = _discover_devices()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@installer_bp.route('/validate', methods=['POST'])
def api_validate():
    """Validate installation plan before execution.

    Body: {strategy, target_device?, data_disks[], encrypt?, passphrase?}
    Returns: {valid, warnings[], errors[], summary, summary_text, plan}
    """
    data = request.json or {}
    strategy = data.get('strategy', '')
    target_device = data.get('target_device', '')
    data_disks = data.get('data_disks', [])
    encrypt = bool(data.get('encrypt', False))

    # Get current devices for validation
    disc = _discover_devices()
    devices_map = {d['device']: d for d in disc['devices']}
    boot_medium_id = disc.get('boot_medium', '')

    warnings, errors = _validate_plan(
        strategy, target_device, data_disks, devices_map, boot_medium_id)
    if encrypt and shutil.which('cryptsetup') is None:
        errors.append('Szyfrowanie LUKS wymaga zainstalowanego pakietu cryptsetup.')
    summary_text, plan = _generate_summary(
        strategy, target_device, data_disks, devices_map,
        disc.get('boot_source_disk', ''))

    return jsonify({
        'valid': len(errors) == 0,
        'warnings': warnings,
        'errors': errors,
        'summary': summary_text,
        'plan': plan,
    })


@installer_bp.route('/execute', methods=['POST'])
def api_execute():
    """Start installation. Returns immediately — poll /status for progress.

    Body: {strategy, target_device?, data_disks[], encrypt?, passphrase?,
           confirmation_token: 'INSTALUJ'}
    """
    global _install_thread

    with _install_lock:
        if _install_state['status'] == 'running':
            return jsonify({'error': 'Instalacja już trwa'}), 409

    data = request.json or {}
    strategy = data.get('strategy', '')
    target_device = data.get('target_device', '')
    data_disks = data.get('data_disks', [])
    encrypt = data.get('encrypt', False)
    passphrase = data.get('passphrase', '')
    token = data.get('confirmation_token', '')

    # Typed confirmation — user must type exact token
    if token != _CONFIRMATION_TOKEN:
        return jsonify({
            'error': f'Wymagane wpisanie "{_CONFIRMATION_TOKEN}" w polu potwierdzenia.'
        }), 400

    # Re-validate
    disc = _discover_devices()
    devices_map = {d['device']: d for d in disc['devices']}
    boot_medium_id = disc.get('boot_medium', '')
    warnings, errors = _validate_plan(
        strategy, target_device, data_disks, devices_map, boot_medium_id)

    if errors:
        return jsonify({'error': 'Walidacja nieudana', 'errors': errors}), 400

    # Resolve persistent IDs to /dev paths at execution time
    resolved_target = ''
    if strategy == 'internal' and target_device:
        resolved_target = _resolve_persistent_id(target_device)
        if not resolved_target:
            return jsonify({
                'error': f'Nie można rozwiązać urządzenia systemowego: {target_device}'
            }), 400

    resolved_data = []
    for dd_id in data_disks:
        dd_dev = _resolve_persistent_id(dd_id)
        if not dd_dev:
            return jsonify({
                'error': f'Nie można rozwiązać dysku danych: {dd_id}'
            }), 400
        resolved_data.append(dd_dev)

    # Check for RAID danger zone
    danger_devices = []
    all_targets = ([resolved_target] if resolved_target else []) + resolved_data
    for dev_path in all_targets:
        dev = devices_map.get(dev_path, {})
        if dev.get('is_raid_member'):
            danger_devices.append(dev_path)

    if danger_devices and not data.get('confirm_raid', False):
        return jsonify({
            'error': 'danger_zone',
            'message': f'Dyski {", ".join(danger_devices)} są członkami macierzy RAID. '
                       f'Wymagane dodatkowe potwierdzenie (confirm_raid: true).',
            'raid_devices': danger_devices,
        }), 409

    # Build installer plan metadata for result file
    plan_meta = {
        'strategy': strategy,
        'target_device_id': target_device,
        'target_device_resolved': resolved_target,
        'data_disk_ids': data_disks,
        'data_disks_resolved': resolved_data,
        'encrypt': bool(encrypt),
    }

    # Reset state
    _install_state.update({
        'status': 'running',
        'phase': 'discovery',
        'percent': 0,
        'message': 'Rozpoczynam instalację...',
        'logs': [],
        'start_time': time.time(),
        'result': None,
        'speed': 0,
        'strategy': strategy,
        'target_device': resolved_target or target_device,
        'data_disks': resolved_data,
    })
    _save_state()

    # Launch worker thread
    _install_thread = threading.Thread(
        target=_install_worker,
        args=(strategy, resolved_target, resolved_data, encrypt, passphrase, plan_meta),
        daemon=True)
    _install_thread.start()

    return jsonify({'status': 'ok'})


@installer_bp.route('/status')
def api_status():
    """Get current installation status (poll endpoint)."""
    elapsed = 0
    if _install_state['start_time'] and _install_state['status'] == 'running':
        elapsed = int(time.time() - _install_state['start_time'])

    return jsonify({
        'status': _install_state['status'],
        'phase': _install_state['phase'],
        'percent': _install_state['percent'],
        'message': _install_state['message'],
        'result': _install_state['result'],
        'speed': _install_state['speed'],
        'elapsed': elapsed,
        'strategy': _install_state['strategy'],
        'target_device': _install_state['target_device'],
        'data_disks': _install_state['data_disks'],
        'log_count': len(_install_state['logs']),
    })


@installer_bp.route('/logs')
def api_logs():
    """Get installation logs."""
    offset = request.args.get('offset', 0, type=int)
    logs = _install_state['logs'][offset:]
    return jsonify({
        'logs': logs,
        'total': len(_install_state['logs']),
        'offset': offset,
    })


@installer_bp.route('/reset', methods=['POST'])
def api_reset():
    """Reset installer state (for retrying after error)."""
    with _install_lock:
        if _install_state['status'] == 'running':
            return jsonify({'error': 'Instalacja trwa — nie można zresetować'}), 409

    _install_state.update({
        'status': 'idle',
        'phase': '',
        'percent': 0,
        'message': '',
        'logs': [],
        'start_time': 0,
        'result': None,
        'speed': 0,
        'strategy': '',
        'target_device': '',
        'data_disks': [],
    })
    _save_state()
    return jsonify({'success': True})


@installer_bp.route('/result')
def api_result():
    """Return installer_result.json handover contract (if exists)."""
    import json as _json
    if os.path.isfile(_INSTALLER_RESULT_FILE):
        try:
            with open(_INSTALLER_RESULT_FILE) as f:
                return jsonify(_json.load(f))
        except Exception:
            pass
    return jsonify({'error': 'No installer result'}), 404


# Load saved state on import
_load_state()
