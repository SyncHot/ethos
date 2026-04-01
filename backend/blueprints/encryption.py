"""
EthOS — Encryption Manager Blueprint
LUKS2 volume encryption, key management, auto-unlock configuration.

Endpoints:
  GET  /api/encryption/status         — LUKS status for all or specific device
  POST /api/encryption/encrypt        — Encrypt an unmounted partition with LUKS2
  POST /api/encryption/unlock         — Unlock (open) a LUKS volume
  POST /api/encryption/lock           — Lock (close) a LUKS volume
  POST /api/encryption/change-key     — Change LUKS passphrase
  GET  /api/encryption/auto-unlock    — Get auto-unlock config
  PUT  /api/encryption/auto-unlock    — Set auto-unlock (crypttab + keyfile)
  DELETE /api/encryption/auto-unlock  — Remove auto-unlock for a device
  GET  /api/encryption/devices        — List encryptable (unmounted, non-root) devices
"""

import json
import os
import re
import sys

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, data_path, q, apt_install
from blueprints.admin_required import admin_required

encryption_bp = Blueprint('encryption', __name__, url_prefix='/api/encryption')

_ENCRYPTION_STATE_FILE = data_path('encryption_state.json')
_KEYFILE_DIR = data_path('luks-keys')


def _ensure_cryptsetup():
    r = host_run("command -v cryptsetup")
    if r.returncode != 0:
        apt_install('cryptsetup', timeout=120)
        r = host_run("command -v cryptsetup")
        if r.returncode != 0:
            return False
    return True


def _load_state():
    if os.path.isfile(_ENCRYPTION_STATE_FILE):
        try:
            with open(_ENCRYPTION_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(_ENCRYPTION_STATE_FILE), exist_ok=True)
    with open(_ENCRYPTION_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _is_luks(device):
    r = host_run(f"cryptsetup isLuks {q(device)} 2>/dev/null")
    return r.returncode == 0


def _luks_info(device):
    if not _is_luks(device):
        return None
    r = host_run(f"cryptsetup luksDump {q(device)} 2>/dev/null")
    if r.returncode != 0:
        return None
    info = {'device': device, 'encrypted': True}
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith('Version:'):
            info['version'] = line.split(':', 1)[-1].strip()
        elif line.startswith('Cipher name:') or line.startswith('Cipher:'):
            info['cipher'] = line.split(':', 1)[-1].strip()
        elif line.startswith('Cipher mode:'):
            info['cipher_mode'] = line.split(':', 1)[-1].strip()
        elif line.startswith('Hash spec:') or line.startswith('Hash:'):
            info['hash'] = line.split(':', 1)[-1].strip()
        elif line.startswith('UUID:'):
            info['uuid'] = line.split(':', 1)[-1].strip()
    info['unlocked'] = False
    r2 = host_run("ls /dev/mapper/ 2>/dev/null")
    for name in r2.stdout.strip().splitlines():
        name = name.strip()
        if name == 'control':
            continue
        r3 = host_run(f"cryptsetup status {q(name)} 2>/dev/null")
        if device in r3.stdout:
            info['mapper_name'] = name
            info['unlocked'] = True
            break
    return info


def _get_mapper_name(device):
    r = host_run("ls /dev/mapper/ 2>/dev/null")
    for name in r.stdout.strip().splitlines():
        name = name.strip()
        if name == 'control':
            continue
        r2 = host_run(f"cryptsetup status {q(name)} 2>/dev/null")
        if device in r2.stdout:
            return name
    return None


def _is_mounted(device):
    r = host_run(f"findmnt -rn -S {q(device)} 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        return True
    mapper = _get_mapper_name(device)
    if mapper:
        r2 = host_run(f"findmnt -rn -S /dev/mapper/{q(mapper)} 2>/dev/null")
        if r2.returncode == 0 and r2.stdout.strip():
            return True
    return False


def _safe_device(device):
    if not device:
        return None
    device = device.strip()
    if not re.match(r'^/dev/[a-zA-Z0-9/_-]+$', device):
        return None
    r = host_run(f"test -b {q(device)}")
    if r.returncode != 0:
        return None
    return device


def _write_secret(path, content):
    with open(path, 'w') as f:
        f.write(content)
    os.chmod(path, 0o600)


def _mount_mapper(mapper_dev, mount_point):
    host_run(f"mkdir -p {q(mount_point)}")
    r = host_run(f"findmnt -rn -S {q(mapper_dev)} 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        return
    host_run(f"mount {q(mapper_dev)} {q(mount_point)}")


def _update_crypttab(mapper_name, line):
    crypttab = '/etc/crypttab'
    try:
        content = open(crypttab).read()
    except FileNotFoundError:
        content = ''
    lines = [l for l in content.splitlines()
             if not l.startswith(mapper_name + ' ') and not l.startswith(mapper_name + '\t')]
    lines.append(line)
    host_run(f"echo {q(chr(10).join(lines))} | sudo tee {crypttab} > /dev/null")


def _remove_crypttab(mapper_name):
    crypttab = '/etc/crypttab'
    try:
        content = open(crypttab).read()
    except FileNotFoundError:
        return
    lines = [l for l in content.splitlines()
             if not l.startswith(mapper_name + ' ') and not l.startswith(mapper_name + '\t')]
    host_run(f"echo {q(chr(10).join(lines))} | sudo tee {crypttab} > /dev/null")


def _update_fstab(mapper_name, line):
    fstab = '/etc/fstab'
    try:
        content = open(fstab).read()
    except FileNotFoundError:
        content = ''
    pattern = f"/dev/mapper/{mapper_name}"
    lines = [l for l in content.splitlines() if pattern not in l]
    lines.append(line)
    host_run(f"echo {q(chr(10).join(lines))} | sudo tee {fstab} > /dev/null")


def _remove_fstab(mapper_name):
    fstab = '/etc/fstab'
    try:
        content = open(fstab).read()
    except FileNotFoundError:
        return
    pattern = f"/dev/mapper/{mapper_name}"
    lines = [l for l in content.splitlines() if pattern not in l]
    host_run(f"echo {q(chr(10).join(lines))} | sudo tee {fstab} > /dev/null")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@encryption_bp.route('/status', methods=['GET'])
@admin_required
def encryption_status():
    if not _ensure_cryptsetup():
        return jsonify({'ok': False, 'error': 'cryptsetup not available'}), 500
    device = request.args.get('device', '').strip()
    if device:
        dev = _safe_device(device)
        if not dev:
            return jsonify({'error': 'Invalid device path'}), 400
        info = _luks_info(dev)
        if not info:
            return jsonify({'ok': True, 'encrypted': False, 'device': dev})
        return jsonify({'ok': True, **info})
    state = _load_state()
    results = []
    for dev_path in state.get('devices', []):
        info = _luks_info(dev_path)
        if info:
            results.append(info)
    r = host_run("lsblk -rno NAME,TYPE 2>/dev/null")
    known = {d['device'] for d in results}
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ('part', 'disk'):
            dev_path = f"/dev/{parts[0]}"
            if dev_path not in known and _is_luks(dev_path):
                info = _luks_info(dev_path)
                if info:
                    results.append(info)
    return jsonify({'ok': True, 'devices': results})


@encryption_bp.route('/devices', methods=['GET'])
@admin_required
def list_encryptable():
    r = host_run("lsblk -rno NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,MODEL 2>/dev/null")
    if r.returncode != 0:
        return jsonify({'error': 'Failed to list devices'}), 500
    root_r = host_run("findmnt -rno SOURCE / 2>/dev/null")
    root_dev = root_r.stdout.strip().replace('/dev/', '').split('\n')[0] if root_r.returncode == 0 else ''
    root_disk = re.sub(r'p?\d+$', '', root_dev)
    devices = []
    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 5)
        if len(parts) < 3:
            continue
        name, size, dtype = parts[0], parts[1], parts[2]
        mountpoint = parts[3] if len(parts) > 3 else ''
        fstype = parts[4] if len(parts) > 4 else ''
        model = parts[5] if len(parts) > 5 else ''
        if dtype not in ('part', 'disk'):
            continue
        base = re.sub(r'p?\d+$', '', name)
        if base == root_disk:
            continue
        if mountpoint and mountpoint not in ('', '[SWAP]'):
            continue
        dev_path = f"/dev/{name}"
        devices.append({
            'device': dev_path, 'name': name, 'size': size, 'type': dtype,
            'fstype': fstype or ('crypto_LUKS' if _is_luks(dev_path) else ''),
            'model': model, 'encrypted': _is_luks(dev_path),
        })
    return jsonify({'ok': True, 'devices': devices})


@encryption_bp.route('/encrypt', methods=['POST'])
@admin_required
def encrypt_device():
    if not _ensure_cryptsetup():
        return jsonify({'ok': False, 'error': 'cryptsetup not available'}), 500
    data = request.json or {}
    device = _safe_device(data.get('device', ''))
    passphrase = data.get('passphrase', '')
    label = re.sub(r'[^a-zA-Z0-9_-]', '', (data.get('label', '') or 'luks-data').strip())[:32]
    filesystem = data.get('filesystem', 'btrfs')
    if not device:
        return jsonify({'error': 'Valid block device required'}), 400
    if not passphrase or len(passphrase) < 8:
        return jsonify({'error': 'Passphrase must be at least 8 characters'}), 400
    if filesystem not in ('btrfs', 'ext4', 'xfs'):
        return jsonify({'error': 'Filesystem must be btrfs, ext4, or xfs'}), 400
    if _is_mounted(device):
        return jsonify({'error': 'Device is mounted — unmount first'}), 400
    if _is_luks(device):
        return jsonify({'error': 'Device is already LUKS-encrypted'}), 400
    mapper_name = f"luks-{label}"
    _write_secret('/tmp/_luks_key.txt', passphrase)
    try:
        r = host_run(
            f"cryptsetup luksFormat --type luks2 --cipher aes-xts-plain64 "
            f"--key-size 512 --hash sha256 --iter-time 2000 "
            f"--key-file /tmp/_luks_key.txt {q(device)}", timeout=120)
        if r.returncode != 0:
            return jsonify({'error': f'LUKS format failed: {r.stderr.strip()[-200:]}'}), 500
        r = host_run(
            f"cryptsetup open --type luks --key-file /tmp/_luks_key.txt "
            f"{q(device)} {q(mapper_name)}", timeout=30)
        if r.returncode != 0:
            return jsonify({'error': f'LUKS open failed: {r.stderr.strip()[-200:]}'}), 500
        mapper_dev = f"/dev/mapper/{mapper_name}"
        mkfs = {'btrfs': f"mkfs.btrfs -f -L {q(label)}",
                 'ext4': f"mkfs.ext4 -F -L {q(label)}",
                 'xfs': f"mkfs.xfs -f -L {q(label)}"}
        r = host_run(f"{mkfs[filesystem]} {q(mapper_dev)}", timeout=60)
        if r.returncode != 0:
            host_run(f"cryptsetup close {q(mapper_name)} 2>/dev/null")
            return jsonify({'error': f'mkfs failed: {r.stderr.strip()[-200:]}'}), 500
        state = _load_state()
        devs = state.get('devices', [])
        if device not in devs:
            devs.append(device)
        state['devices'] = devs
        state.setdefault('labels', {})[device] = label
        _save_state(state)
        try:
            from blueprints.eventlog import log as elog
            elog('storage', 'info', f'Device {device} encrypted with LUKS2 ({filesystem})')
        except Exception:
            pass
        return jsonify({'ok': True, 'device': device, 'mapper': mapper_dev,
                        'label': label, 'filesystem': filesystem})
    finally:
        host_run("rm -f /tmp/_luks_key.txt")


@encryption_bp.route('/unlock', methods=['POST'])
@admin_required
def unlock_device():
    if not _ensure_cryptsetup():
        return jsonify({'ok': False, 'error': 'cryptsetup not available'}), 500
    data = request.json or {}
    device = _safe_device(data.get('device', ''))
    passphrase = data.get('passphrase', '')
    mount_point = data.get('mount', '').strip()
    if not device:
        return jsonify({'error': 'Valid block device required'}), 400
    if not passphrase:
        return jsonify({'error': 'Passphrase required'}), 400
    if not _is_luks(device):
        return jsonify({'error': 'Device is not LUKS-encrypted'}), 400
    existing = _get_mapper_name(device)
    if existing:
        mapper_dev = f"/dev/mapper/{existing}"
        if mount_point:
            _mount_mapper(mapper_dev, mount_point)
        return jsonify({'ok': True, 'device': device, 'mapper': mapper_dev, 'already_open': True})
    state = _load_state()
    label = state.get('labels', {}).get(device, 'luks-data')
    mapper_name = f"luks-{label}"
    _write_secret('/tmp/_luks_key.txt', passphrase)
    try:
        r = host_run(
            f"cryptsetup open --type luks --key-file /tmp/_luks_key.txt "
            f"{q(device)} {q(mapper_name)}", timeout=30)
        if r.returncode != 0:
            err = r.stderr.strip()
            if 'No key available' in err or 'key slot' in err.lower():
                return jsonify({'error': 'Wrong passphrase'}), 401
            return jsonify({'error': f'Unlock failed: {err[-200:]}'}), 500
        mapper_dev = f"/dev/mapper/{mapper_name}"
        if mount_point:
            _mount_mapper(mapper_dev, mount_point)
        return jsonify({'ok': True, 'device': device, 'mapper': mapper_dev})
    finally:
        host_run("rm -f /tmp/_luks_key.txt")


@encryption_bp.route('/lock', methods=['POST'])
@admin_required
def lock_device():
    data = request.json or {}
    device = _safe_device(data.get('device', ''))
    if not device:
        return jsonify({'error': 'Valid block device required'}), 400
    mapper = _get_mapper_name(device)
    if not mapper:
        return jsonify({'ok': True, 'already_locked': True})
    host_run(f"umount /dev/mapper/{q(mapper)} 2>/dev/null")
    r = host_run(f"cryptsetup close {q(mapper)}", timeout=30)
    if r.returncode != 0:
        return jsonify({'error': f'Lock failed (device busy?): {r.stderr.strip()[-200:]}'}), 500
    return jsonify({'ok': True, 'device': device})


@encryption_bp.route('/change-key', methods=['POST'])
@admin_required
def change_passphrase():
    if not _ensure_cryptsetup():
        return jsonify({'ok': False, 'error': 'cryptsetup not available'}), 500
    data = request.json or {}
    device = _safe_device(data.get('device', ''))
    old_pass = data.get('old_passphrase', '')
    new_pass = data.get('new_passphrase', '')
    if not device:
        return jsonify({'error': 'Valid block device required'}), 400
    if not old_pass or not new_pass:
        return jsonify({'error': 'Both old and new passphrase required'}), 400
    if len(new_pass) < 8:
        return jsonify({'error': 'New passphrase must be at least 8 characters'}), 400
    if not _is_luks(device):
        return jsonify({'error': 'Device is not LUKS-encrypted'}), 400
    _write_secret('/tmp/_luks_old.txt', old_pass)
    _write_secret('/tmp/_luks_new.txt', new_pass)
    try:
        r = host_run(
            f"cryptsetup luksChangeKey {q(device)} "
            f"--key-file /tmp/_luks_old.txt /tmp/_luks_new.txt", timeout=60)
        if r.returncode != 0:
            err = r.stderr.strip()
            if 'No key available' in err:
                return jsonify({'error': 'Wrong current passphrase'}), 401
            return jsonify({'error': f'Key change failed: {err[-200:]}'}), 500
        return jsonify({'ok': True})
    finally:
        host_run("rm -f /tmp/_luks_old.txt /tmp/_luks_new.txt")


@encryption_bp.route('/auto-unlock', methods=['GET'])
@admin_required
def get_auto_unlock():
    state = _load_state()
    return jsonify({'ok': True, 'auto_unlock': state.get('auto_unlock', {})})


@encryption_bp.route('/auto-unlock', methods=['PUT'])
@admin_required
def set_auto_unlock():
    if not _ensure_cryptsetup():
        return jsonify({'ok': False, 'error': 'cryptsetup not available'}), 500
    data = request.json or {}
    device = _safe_device(data.get('device', ''))
    passphrase = data.get('passphrase', '')
    mount_point = data.get('mount', '').strip()
    if not device:
        return jsonify({'error': 'Valid block device required'}), 400
    if not passphrase:
        return jsonify({'error': 'Passphrase required to create keyfile'}), 400
    if not _is_luks(device):
        return jsonify({'error': 'Device is not LUKS-encrypted'}), 400
    state = _load_state()
    label = state.get('labels', {}).get(device, 'luks-data')
    mapper_name = f"luks-{label}"
    os.makedirs(_KEYFILE_DIR, exist_ok=True)
    os.chmod(_KEYFILE_DIR, 0o700)
    keyfile = os.path.join(_KEYFILE_DIR, f"{mapper_name}.key")
    host_run(f"dd if=/dev/urandom of={q(keyfile)} bs=4096 count=1 2>/dev/null")
    host_run(f"chmod 400 {q(keyfile)}")
    _write_secret('/tmp/_luks_pass.txt', passphrase)
    try:
        r = host_run(
            f"cryptsetup luksAddKey {q(device)} {q(keyfile)} "
            f"--key-file /tmp/_luks_pass.txt", timeout=60)
        if r.returncode != 0:
            host_run(f"rm -f {q(keyfile)}")
            return jsonify({'error': f'Failed to add keyfile: {r.stderr.strip()[-200:]}'}), 500
    finally:
        host_run("rm -f /tmp/_luks_pass.txt")
    r = host_run(f"cryptsetup luksUUID {q(device)}")
    uuid = r.stdout.strip()
    _update_crypttab(mapper_name, f"{mapper_name}  UUID={uuid}  {keyfile}  luks")
    if mount_point:
        host_run(f"mkdir -p {q(mount_point)}")
        mapper_dev = f"/dev/mapper/{mapper_name}"
        r = host_run(f"blkid -o value -s TYPE {q(mapper_dev)} 2>/dev/null || "
                      f"blkid -o value -s TYPE {q(device)} 2>/dev/null")
        fstype = r.stdout.strip() or 'btrfs'
        _update_fstab(mapper_name, f"/dev/mapper/{mapper_name}  {mount_point}  {fstype}  defaults,nofail  0  2")
    auto = state.get('auto_unlock', {})
    auto[device] = {'mapper_name': mapper_name, 'keyfile': keyfile,
                     'mount': mount_point, 'uuid': uuid}
    state['auto_unlock'] = auto
    _save_state(state)
    return jsonify({'ok': True, 'mapper_name': mapper_name, 'keyfile': keyfile})


@encryption_bp.route('/auto-unlock', methods=['DELETE'])
@admin_required
def remove_auto_unlock():
    data = request.json or {}
    device = _safe_device(data.get('device', ''))
    if not device:
        return jsonify({'error': 'Valid block device required'}), 400
    state = _load_state()
    auto = state.get('auto_unlock', {})
    entry = auto.pop(device, None)
    if not entry:
        return jsonify({'ok': True, 'message': 'No auto-unlock configured'})
    mapper_name = entry.get('mapper_name', '')
    keyfile = entry.get('keyfile', '')
    if keyfile and os.path.isfile(keyfile):
        host_run(f"cryptsetup luksRemoveKey {q(device)} {q(keyfile)} 2>/dev/null")
        host_run(f"rm -f {q(keyfile)}")
    if mapper_name:
        _remove_crypttab(mapper_name)
        _remove_fstab(mapper_name)
    state['auto_unlock'] = auto
    _save_state(state)
    return jsonify({'ok': True})
