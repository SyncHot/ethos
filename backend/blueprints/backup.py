"""
EthOS — Backup & Restore Blueprint
Full and incremental backups with USB/SSH destinations, profiles, scheduling.
Migrated from standalone backuprestore app (eventlet → gevent).
"""

import os
import subprocess
import threading
import time
import json
import logging
import fcntl
import gevent
from datetime import datetime, timedelta
import shutil
from pathlib import Path
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from flask import Blueprint, jsonify, request, g

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import get_ethos_user

try:
    import paramiko
    from scp import SCPClient
    HAS_SSH = True
except ImportError:
    HAS_SSH = False

from ssh_utils import (
    get_ssh_client as _get_ssh_client,
    ssh_resolve_home as _ssh_resolve_home,
    ssh_ensure_writable_dir as _ssh_ensure_writable_dir,
    ssh_remote_disk_info as _ssh_remote_disk_info,
    ssh_exec as _ssh_exec,
)

from blueprints.profiles_db import get_db_connection, init_profiles_db

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import app_path, data_path, log_path, q, host_run
from crypto_utils import encrypt_secret, decrypt_secret

backup_bp = Blueprint('backup', __name__, url_prefix='/api/backup')

# Thread pool for filesystem calls that may hang on stale mounts
_fs_executor = ThreadPoolExecutor(max_workers=4)


# ── Encryption helpers ──

def encrypt_backup_gpg(file_path, passphrase):
    """Encrypt a backup file using GPG symmetric AES-256.
    Returns the path of the encrypted file (.gpg)."""
    encrypted_path = file_path + '.gpg'
    result = subprocess.run(
        ['gpg', '--symmetric', '--cipher-algo', 'AES256',
         '--batch', '--yes', '--passphrase-fd', '0',
         '--output', encrypted_path, file_path],
        input=passphrase,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Exception(f"GPG encryption error: {result.stderr.strip()[:300]}")
    return encrypted_path


def decrypt_backup_gpg(encrypted_path, passphrase, output_path):
    """Decrypt a GPG-encrypted backup file.
    Returns output_path on success, raises on failure."""
    result = subprocess.run(
        ['gpg', '--decrypt', '--batch', '--yes', '--passphrase-fd', '0',
         '--output', output_path, encrypted_path],
        input=passphrase,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Exception(f"GPG decryption error (wrong password?): {result.stderr.strip()[:300]}")
    return output_path


def _resolve_encryption_passphrase(enc):
    """Return the encryption passphrase for the given encryption config dict.

    For mode='key': decrypts and returns the stored key (scheduled backups work).
    For mode='passphrase': returns None — caller must supply the passphrase.
    Returns None if encryption is not enabled."""
    if not enc or not enc.get('enabled'):
        return None
    if enc.get('mode') == 'key':
        stored = enc.get('stored_key')
        if not stored:
            raise Exception("Encryption key was not generated for this profile.")
        return decrypt_secret(stored)
    return None  # passphrase mode — must be provided by caller


def _prepare_encryption(enc_input, existing_enc=None):
    """Normalise encryption config from API input.

    - Preserves stored_key when mode='key' and client didn't send one.
    - Generates a new stored_key when switching to mode='key' for the first time.
    Returns (enc_dict_to_store, plaintext_key_for_user_or_None).
    """
    if not enc_input or not enc_input.get('enabled'):
        return None, None

    enc = dict(enc_input)
    enc.setdefault('mode', 'passphrase')
    generated_key = None

    if enc['mode'] == 'key':
        if enc.get('stored_key'):
            pass  # Client re-sent an (unusable) stored_key — ignore, use existing
        if existing_enc and existing_enc.get('mode') == 'key' and existing_enc.get('stored_key'):
            enc['stored_key'] = existing_enc['stored_key']
        else:
            # First time switching to key mode — generate a fresh key
            generated_key = secrets.token_hex(32)
            enc['stored_key'] = encrypt_secret(generated_key)
    else:
        enc.pop('stored_key', None)

    return enc, generated_key

logger = logging.getLogger(__name__)

# ── Config ──
DATA_DIR = '/data'
# Allowed root paths for backup source browsing. Includes legacy /data for
# Docker deployments, plus native host paths for direct installations.
_DEFAULT_BROWSE_ROOTS = ['/data', '/home', '/media', '/run/media', '/mnt', '/opt', '/srv', '/var', '/etc']
BROWSE_ROOTS = [r for r in _DEFAULT_BROWSE_ROOTS if os.path.isdir(r)]

def _effective_browse_roots():
    """Return allowed browse roots for the current request.
    Admin users get all BROWSE_ROOTS; regular users get only their own home."""
    if getattr(g, 'role', None) == 'admin':
        return BROWSE_ROOTS
    username = getattr(g, 'username', None)
    if username:
        home = f'/home/{username}'
        if os.path.isdir(home):
            return [home]
    return []
BACKUP_DIR = os.environ.get('BACKUP_DIR', app_path('backups'))
PATHS_FILE = data_path('paths.json')
HISTORY_FILE = data_path('history.json')
SSH_CONFIG_FILE = data_path('ssh_configs.json')
SCHEDULE_FILE = data_path('schedule_state.json')
SCHEDULER_LOG = log_path('scheduler.log')

# ── State ──
operation_lock = threading.Lock()
current_operation = None
progress_state = {'last': None, 'timestamp': 0}
_progress_lock = threading.Lock()

# Reference to the SocketIO instance — set by init_backup()
_socketio = None

# Ensure dirs
os.makedirs(data_path(), exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(os.path.dirname(SCHEDULER_LOG), exist_ok=True)


def init_backup(socketio):
    """Initialize backup module — called from main app after socketio is created."""
    global _socketio
    _socketio = socketio
    init_profiles_db()
    # Start progress worker
    threading.Thread(target=_progress_worker, daemon=True).start()
    # Start scheduler as socketio background task
    socketio.start_background_task(_scheduler_worker)


def get_backup_notifications():
    """Return live in-progress backup notification for the global panel."""
    notifs = []
    with operation_lock:
        op = current_operation
    with _progress_lock:
        prog = progress_state.get('last')
        prog_ts = progress_state.get('timestamp', time.time())
    if op and prog:
        pct = prog.get('overall_percent', prog.get('percent', 0))
        stage = prog.get('stage', 'archive')
        stage_label = 'Transfer' if stage == 'transfer' else 'Archiving'
        notifs.append({
            'type': 'progress',
            'title': 'Backup w toku',
            'message': f'{stage_label}: {round(pct)}%',
            'time': prog_ts,
            'action': {'app': 'backup', 'tab': 'backup'}
        })
    return notifs


# ── Helpers ──

def _fs_call_with_timeout(func, *args, timeout=3):
    future = _fs_executor.submit(func, *args)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(f"Filesystem operation timed out after {timeout}s")


def _emit(event, data):
    sio_emit(_socketio, event, data)


def emit_log(message, log_type='info'):
    _emit('backup_log', {'type': log_type, 'message': message})
    logger.info(f"[backup][{log_type}] {message}")


def emit_progress(operation, percent, current_file, files_done, total_files, bytes_done, total_bytes, eta=None, stage='archive', stage_percent=0, overall_percent=None):
    if overall_percent is None:
        overall_percent = percent
    progress = {
        'operation': operation,
        'percent': round(overall_percent, 1),
        'current_file': current_file,
        'files_done': files_done,
        'total_files': total_files,
        'bytes_done': bytes_done,
        'total_bytes': total_bytes,
        'eta': eta,
        'stage': stage,
        'stage_percent': round(stage_percent, 1),
        'overall_percent': round(overall_percent, 1)
    }
    _emit('backup_progress', progress)
    with _progress_lock:
        progress_state['last'] = progress
        progress_state['timestamp'] = time.time()


def _progress_worker():
    """Periodically re-emit last progress so reconnecting clients see it."""
    while True:
        time.sleep(10)
        with operation_lock:
            op = current_operation
        with _progress_lock:
            last = progress_state['last']
        if op and last:
            _emit('backup_progress', last)


# ── File helpers ──

from utils import load_json, save_json, sio_emit, list_directory as _list_dir, fmt_bytes


def load_paths():
    return load_json(PATHS_FILE, [])

def save_paths(paths):
    save_json(PATHS_FILE, paths)

def load_history():
    return load_json(HISTORY_FILE, [])

def _atomic_save_json(filepath, data):
    """Atomic JSON write with file locking."""
    tmp = filepath + '.tmp'
    with open(tmp, 'w') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, filepath)

def save_history(history):
    _atomic_save_json(HISTORY_FILE, history)

def _strip_secrets_from_entry(entry):
    """Remove passwords/secrets from a history entry before persisting."""
    entry = dict(entry)
    if isinstance(entry.get('destination'), dict):
        dest = dict(entry['destination'])
        if isinstance(dest.get('config'), dict):
            cfg = {k: v for k, v in dest['config'].items() if k != 'password'}
            cfg['has_password'] = bool(dest['config'].get('password'))
            dest['config'] = cfg
        entry['destination'] = dest
    return entry

def add_to_history(entry):
    history = load_history()
    history.insert(0, _strip_secrets_from_entry(entry))
    history = history[:100]
    save_history(history)

def load_ssh_configs():
    """Load SSH configs, decrypting passwords transparently."""
    configs = load_json(SSH_CONFIG_FILE, [])
    for c in configs:
        if c.get('password'):
            c['password'] = decrypt_secret(c['password'])
    return configs

def save_ssh_configs(configs):
    """Save SSH configs, encrypting passwords before writing to disk."""
    encrypted = []
    for c in configs:
        cc = dict(c)
        if cc.get('password'):
            cc['password'] = encrypt_secret(cc['password'])
        encrypted.append(cc)
    _atomic_save_json(SSH_CONFIG_FILE, encrypted)

def load_schedule_state():
    return load_json(SCHEDULE_FILE, {})

def save_schedule_state(state):
    save_json(SCHEDULE_FILE, state)


def load_profiles():
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM profiles').fetchall()
    profiles = []
    for row in rows:
        sched = row['schedule']
        try:
            sched = json.loads(sched) if sched else None
        except Exception:
            pass
        enc_raw = None
        try:
            enc_raw = row['encryption']
        except Exception:
            pass
        enc = None
        if enc_raw:
            try:
                enc = json.loads(enc_raw)
            except Exception:
                pass
        # Strip stored_key before returning to clients (sensitive — never expose)
        enc_public = None
        if enc:
            enc_public = {k: v for k, v in enc.items() if k != 'stored_key'}
        profiles.append({
            'id': str(row['id']),
            'name': row['name'],
            'paths': json.loads(row['paths']),
            'destination': json.loads(row['destination']) if row['destination'] else None,
            'schedule': sched,
            'options': row['options'],
            'retention': row['retention'] if row['retention'] else 0,
            'incremental': bool(row['incremental']) if row['incremental'] else False,
            'encryption': enc_public,
        })
    conn.close()
    return profiles


# ── USB detection ──

BACKUP_EXCLUDE_BASENAMES = {'backups', 'backup', 'cache', '.cache'}

def _is_excluded(dirpath):
    return os.path.basename(dirpath).lower() in BACKUP_EXCLUDE_BASENAMES

def _add_drive(drives_list, name, path):
    if any(d['path'] == path for d in drives_list):
        return
    try:
        stat = _fs_call_with_timeout(os.statvfs, path, timeout=3)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bfree * stat.f_frsize
        used = total - free
        if total < 1024 * 1024:
            return
        drives_list.append({
            'name': f"USB: {name}",
            'path': path,
            'total': total,
            'used': used,
            'free': free,
            'percent_used': round((used / total * 100) if total > 0 else 0, 1)
        })
    except (OSError, PermissionError, TimeoutError):
        pass


def detect_usb_drives():
    usb_drives = []
    mount_bases = {
        '/data/media': True,
        '/data/run_media': True,
        '/data/mnt': False,
        '/media': True,
        '/run/media': True,
        '/mnt': False,
    }
    for base_path, has_user_dirs in mount_bases.items():
        try:
            if not _fs_call_with_timeout(os.path.exists, base_path, timeout=2):
                continue
            entries = _fs_call_with_timeout(os.listdir, base_path, timeout=3)
            for item in entries:
                item_path = os.path.join(base_path, item)
                try:
                    if not _fs_call_with_timeout(os.path.isdir, item_path, timeout=2):
                        continue
                except (TimeoutError, OSError):
                    continue
                if has_user_dirs:
                    try:
                        for drive in _fs_call_with_timeout(os.listdir, item_path, timeout=3):
                            drive_path = os.path.join(item_path, drive)
                            try:
                                if _fs_call_with_timeout(os.path.isdir, drive_path, timeout=2):
                                    _add_drive(usb_drives, drive, drive_path)
                            except (TimeoutError, OSError):
                                continue
                    except (PermissionError, TimeoutError, OSError):
                        continue
                else:
                    _add_drive(usb_drives, item, item_path)
        except (PermissionError, TimeoutError, OSError):
            continue

    # Try lsblk
    try:
        result = subprocess.run(
            ['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,MOUNTPOINT,TRAN'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for device in data.get('blockdevices', []):
                if device.get('tran') == 'usb':
                    for child in device.get('children', []):
                        mp = child.get('mountpoint')
                        if mp:
                            cp = None
                            if mp.startswith('/media/'):
                                cp = '/data/media/' + mp[len('/media/'):]
                            elif mp.startswith('/run/media/'):
                                cp = '/data/run_media/' + mp[len('/run/media/'):]
                            elif mp.startswith('/mnt/'):
                                cp = '/data/mnt/' + mp[len('/mnt/'):] # legacy
                            if cp and os.path.exists(cp):
                                _add_drive(usb_drives, child.get('name', mp.split('/')[-1]), cp)
                            elif not cp and os.path.exists(mp):
                                _add_drive(usb_drives, mp.split('/')[-1], mp)
                            elif cp and not os.path.exists(cp) and os.path.exists(mp):
                                _add_drive(usb_drives, mp.split('/')[-1], mp)
    except Exception:
        pass

    return usb_drives


# ── SSH ── (uses shared ssh_utils)

def test_ssh_connection(host, port, username, password=None, key_path=None, remote_path=None):
    if not HAS_SSH:
        return False, "paramiko not installed"
    try:
        ssh = _get_ssh_client(host, port, username, password=password, key_path=key_path)
        disk_info = _ssh_remote_disk_info(ssh) or {}
        remote_home = _ssh_resolve_home(ssh)
        if remote_home:
            disk_info['remote_home'] = remote_home
        if remote_path:
            resolved = remote_path.replace('~', remote_home or '/tmp').replace('$HOME', remote_home or '/tmp')
            writable, _ = _ssh_ensure_writable_dir(ssh, resolved)
            disk_info['path_writable'] = writable
            disk_info['resolved_path'] = resolved
            if not writable and remote_home:
                fallback = f'{remote_home}/backups'
                fb_ok, _ = _ssh_ensure_writable_dir(ssh, fallback)
                if fb_ok:
                    disk_info['suggested_path'] = fallback
        ssh.close()
        return True, disk_info
    except Exception as e:
        return False, str(e)


# ── Size helpers ──

def get_size(path, exclude=False):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    root_path = os.path.normpath(path)
    for dirpath, dirnames, filenames in os.walk(path):
        if exclude and os.path.normpath(dirpath) != root_path and _is_excluded(dirpath):
            dirnames.clear()
            continue
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except Exception:
                pass
    return total


def count_files(path, exclude=False):
    if os.path.isfile(path):
        return 1
    count = 0
    root_path = os.path.normpath(path)
    for dirpath, dirnames, filenames in os.walk(path):
        if exclude and os.path.normpath(dirpath) != root_path and _is_excluded(dirpath):
            dirnames.clear()
            continue
        count += len(filenames)
    return count


# ── Transfer ──


def transfer_to_ssh(backup_path, ssh_config, backup_filename):
    if not HAS_SSH:
        return False, "paramiko not installed"
    try:
        emit_log(f"Copying to SSH: {ssh_config['host']}", 'info')
        source_size = os.path.getsize(backup_path)

        def progress_callback(filename, size, sent):
            percent = (sent / size * 100) if size > 0 else 0
            emit_progress(
                operation=f"Copying to SSH: {ssh_config['host']}",
                percent=50 + percent * 0.5,
                current_file=filename.decode() if isinstance(filename, bytes) else filename,
                files_done=1 if sent >= size else 0,
                total_files=1, bytes_done=sent, total_bytes=size,
                stage='transfer', stage_percent=percent,
                overall_percent=50 + percent * 0.5
            )

        ssh = _get_ssh_client(ssh_config['host'], ssh_config.get('port', 22),
                              ssh_config['username'],
                              password=ssh_config.get('password'),
                              key_path=ssh_config.get('key_path'), timeout=30)
        remote_path = ssh_config.get('remote_path', '/tmp')
        remote_file = f"{remote_path}/{backup_filename}"
        ssh.exec_command(f"mkdir -p {q(remote_path)}")
        with SCPClient(ssh.get_transport(), progress=progress_callback) as scp:
            scp.put(backup_path, remote_file)
        ssh.close()
        emit_log(f"Skopiowano do SSH: {remote_file}", 'success')
        return True, remote_file
    except Exception as e:
        return False, str(e)


# ── Retention ──

def _is_backup_file(f):
    return f.startswith('backup_') and (f.endswith('.tar.gz') or f.endswith('.tar.gz.gpg'))


def apply_retention(retention, backup_dir, destination=None, profile_name=None):
    if not retention or retention <= 0:
        return
    try:
        local_backups = sorted(
            [f for f in os.listdir(backup_dir) if _is_backup_file(f)],
            key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)), reverse=True
        )
        # Never delete the latest level-0 (full) backup — incrementals depend on it
        protected_full = None
        for f in local_backups:
            if '_incr' not in f:
                protected_full = f
                break
        if len(local_backups) > retention:
            for f in local_backups[retention:]:
                if f == protected_full:
                    continue
                try:
                    os.remove(os.path.join(backup_dir, f))
                    emit_log(f"Rotation: removed {f}", 'info')
                except Exception as e:
                    emit_log(f"Rotation: deletion error {f}: {e}", 'warning')

        if destination and destination.get('type') == 'usb' and destination.get('path'):
            usb_path = destination['path']
            if os.path.isdir(usb_path):
                usb_backups = sorted(
                    [f for f in os.listdir(usb_path) if _is_backup_file(f)],
                    key=lambda f: os.path.getmtime(os.path.join(usb_path, f)), reverse=True
                )
                # Protect the latest full backup on USB too
                usb_protected_full = None
                for f in usb_backups:
                    if '_incr' not in f:
                        usb_protected_full = f
                        break
                if len(usb_backups) > retention:
                    for f in usb_backups[retention:]:
                        if f == usb_protected_full:
                            continue
                        try:
                            os.remove(os.path.join(usb_path, f))
                        except Exception:
                            pass
    except Exception as e:
        emit_log(f"Rotation error: {e}", 'warning')


# ── Incremental chain detection ──

def detect_incremental_chain(selected_file, search_dir=None):
    if '_incr' not in selected_file:
        return [selected_file]
    d = search_dir or BACKUP_DIR
    all_backups = sorted(
        [f for f in os.listdir(d) if _is_backup_file(f)]
    )
    full_backup = None
    for f in all_backups:
        if f >= selected_file:
            break
        if '_incr' not in f:
            full_backup = f
    if not full_backup:
        return [selected_file]
    chain = [full_backup]
    for f in all_backups:
        if f <= full_backup:
            continue
        if f > selected_file:
            break
        if '_incr' in f:
            chain.append(f)
    return chain


# ── Backup ──

def run_backup(paths, destination=None, profile_name=None, retention=0, incremental=False, encrypt_passphrase=None):
    global current_operation
    # USB: write tar directly to destination (no local copy needed)
    # SSH: still needs local copy + transfer
    direct_to_usb = destination and destination.get('type') == 'usb' and destination.get('path')
    has_transfer = destination and destination.get('type') == 'ssh'
    archive_weight = 50 if has_transfer else 100
    history_entry = {
        'id': datetime.now().strftime("%Y%m%d%H%M%S"),
        'timestamp': datetime.now().isoformat(),
        'paths': paths, 'destination': destination,
        'status': 'in_progress', 'archive_file': None,
        'final_location': None, 'size': 0,
        'files_count': 0, 'duration': 0, 'error': None
    }
    start_time = time.time()
    try:
        mode_label = "incremental" if incremental else "full"
        emit_log(f"Backup {mode_label} — {len(paths)} paths...", 'info')

        # Validate paths before starting
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            raise Exception(f"Source paths do not exist: {', '.join(missing)}")

        if direct_to_usb:
            dest_dir = destination['path']
            if not os.path.isdir(dest_dir):
                raise Exception(f"Destination directory does not exist: {dest_dir}")

        total_bytes = 0
        total_files = 0
        for path in paths:
            if os.path.exists(path):
                total_bytes += get_size(path, exclude=True)
                total_files += count_files(path, exclude=True)
        history_entry['files_count'] = total_files
        emit_log(f"Size: {total_bytes / (1024**3):.2f} GB, files: {total_files}", 'info')

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # ── Incremental snapshot handling ──
        # Determine snapshot path first so we know if this will be a true
        # incremental or a level-0 (full) backup before naming the file.
        snapshot_path = None
        is_true_incremental = False
        if incremental and profile_name:
            safe_name = profile_name.replace('/', '_').replace(' ', '_')
            snapshot_path = os.path.join(BACKUP_DIR, f".snapshot_{safe_name}.snar")

            # Safety check: if snapshot exists but no full backup (level-0,
            # without '_incr') is found at the destination, delete the snapshot
            # so tar creates a fresh level-0 backup.  This prevents orphaned
            # incrementals after old archives are cleaned up.
            # NOTE: When a destination (USB/SSH) is configured, we ONLY check
            # the destination — local copies are temporary and deleted after
            # transfer, so their presence is meaningless.
            if os.path.exists(snapshot_path):
                has_full_backup = False
                dest_dir = None
                if destination and destination.get('type') == 'usb' and destination.get('path'):
                    dest_dir = destination['path']
                if dest_dir and os.path.isdir(dest_dir):
                    try:
                        has_full_backup = any(
                            _is_backup_file(f) and '_incr' not in f
                            for f in os.listdir(dest_dir)
                        )
                    except Exception:
                        pass
                # Only check local backup dir if there is NO destination configured
                if not has_full_backup and not dest_dir:
                    try:
                        has_full_backup = any(
                            _is_backup_file(f) and '_incr' not in f
                            for f in os.listdir(BACKUP_DIR)
                        )
                    except Exception:
                        pass

                if not has_full_backup:
                    os.remove(snapshot_path)
                    emit_log("No full base backup — resetting snapshot and creating new full backup", 'warning')

            # After the safety check, if snapshot still exists → true incremental
            is_true_incremental = os.path.exists(snapshot_path)

        # Suffix: only add _incr for actual incremental backups, NOT for level-0 (full)
        suffix = "_incr" if is_true_incremental else ""
        backup_filename = f"backup_{timestamp}{suffix}.tar.gz"

        # When USB destination is set, create tar directly on USB (no local copy)
        if direct_to_usb:
            usb_dir = destination['path']
            os.makedirs(usb_dir, exist_ok=True)
            backup_path = os.path.join(usb_dir, backup_filename)
            emit_log(f"Archive being created directly on USB: {usb_dir}", 'info')
        else:
            backup_path = os.path.join(BACKUP_DIR, backup_filename)
        history_entry['archive_file'] = backup_filename

        cmd = ['tar', '-cvzf', backup_path]
        if snapshot_path is not None:
            cmd.append(f'--listed-incremental={snapshot_path}')
            if is_true_incremental:
                emit_log("Incremental backup — archiving only changed files", 'info')
            else:
                emit_log("First backup — creating full archive with snapshot file", 'info')

        for path in paths:
            if os.path.exists(path):
                stripped = path.lstrip('/')
                for excl in ['backups', 'Backups', 'backup', '.cache', 'cache']:
                    cmd.append(f'--exclude={stripped}/{excl}')
                    cmd.append(f'--exclude={stripped}/*/{excl}')

        for path in paths:
            if os.path.exists(path):
                cmd.extend(['-C', '/', path.lstrip('/')])

        emit_log("Step 1: Archiving...", 'info')
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        files_done = 0
        bytes_done = 0
        archive_start = time.time()
        last_progress_time = 0
        tar_errors = []

        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if line:
                files_done += 1
                try:
                    if os.path.exists('/' + line):
                        bytes_done += os.path.getsize('/' + line)
                except Exception:
                    pass
                if files_done % 50 == 0:
                    gevent.sleep(0)
                percent = (files_done / total_files * 100) if total_files > 0 else (files_done / max(files_done, 1) * 50)
                elapsed = time.time() - archive_start
                eta = (elapsed / percent) * (100 - percent) if percent > 0 else None
                now = time.time()
                if total_files <= 100 or files_done % 10 == 0 or files_done == total_files or (now - last_progress_time) >= 0.5:
                    last_progress_time = now
                    overall = percent * archive_weight / 100
                    emit_progress(
                        operation=f"Archiving: {backup_filename}",
                        percent=overall, current_file=line[-50:] if len(line) > 50 else line,
                        files_done=files_done, total_files=total_files,
                        bytes_done=bytes_done, total_bytes=total_bytes,
                        eta=eta, stage='archive', stage_percent=percent,
                        overall_percent=overall
                    )

        # Capture stderr for error diagnostics
        stderr_output = ''
        try:
            stderr_output = process.stderr.read() or ''
        except Exception:
            pass

        process.wait()
        if process.returncode >= 2:
            err_detail = stderr_output.strip()[:500] if stderr_output.strip() else 'no details'
            emit_log(f"tar stderr: {err_detail}", 'error')
            raise Exception(f"Archive error (code: {process.returncode}): {err_detail}")
        elif process.returncode == 1:
            if stderr_output.strip():
                emit_log(f"tar warnings: {stderr_output.strip()[:300]}", 'warning')
            emit_log("Archive completed with warnings", 'warning')

        final_size = os.path.getsize(backup_path) if os.path.exists(backup_path) else 0
        history_entry['size'] = final_size
        emit_log(f"Archiwum: {backup_filename} ({final_size / (1024*1024):.2f} MB)", 'success')

        # ── Encryption step ──
        if encrypt_passphrase:
            emit_log("Step: Encrypting archive (AES-256)...", 'info')
            encrypted_path = encrypt_backup_gpg(backup_path, encrypt_passphrase)
            os.remove(backup_path)
            backup_path = encrypted_path
            backup_filename = backup_filename + '.gpg'
            history_entry['archive_file'] = backup_filename
            enc_size = os.path.getsize(backup_path)
            history_entry['size'] = enc_size
            history_entry['encrypted'] = True
            emit_log(f"Encrypted: {backup_filename} ({enc_size / (1024*1024):.2f} MB)", 'success')

        final_location = backup_path
        if destination:
            if destination['type'] == 'usb':
                # Archive already created directly on USB — no transfer needed
                final_location = backup_path
                emit_log(f"Archive saved to USB: {backup_path}", 'success')
            elif destination['type'] == 'ssh':
                emit_log("Step 2: Copying to SSH...", 'info')
                ok, result = transfer_to_ssh(backup_path, destination['config'], backup_filename)
                if ok:
                    final_location = result
                else:
                    raise Exception(f"SSH: {result}")

        history_entry['final_location'] = final_location
        history_entry['status'] = 'completed'
        history_entry['duration'] = round(time.time() - start_time, 1)
        add_to_history(history_entry)
        with _progress_lock:
            progress_state['last'] = None
        emit_log(f"Backup completed: {final_location}", 'success')
        _emit('backup_complete', {'message': f'Backup completed: {backup_filename}'})
        # Persistent notification — stays until user clears
        try:
            from app import add_persistent_notification
            size_mb = round(history_entry.get('size', 0) / (1024 * 1024), 1)
            add_persistent_notification(
                'Backup completed',
                f'{backup_filename} — {size_mb} MB, {round(history_entry["duration"])}s',
                ntype='success',
                action={'app': 'backup', 'tab': 'history'}
            )
        except Exception:
            pass
        if retention and retention > 0:
            # For USB: apply retention on USB directory (archives are there)
            retention_dir = destination['path'] if direct_to_usb else BACKUP_DIR
            apply_retention(retention, retention_dir, destination, profile_name)

        # If transferred to SSH, remove ALL local copies (no local retention)
        # For USB: archive was created directly there, no local copies to clean
        if has_transfer:
            try:
                for f in os.listdir(BACKUP_DIR):
                    if f.startswith('backup_') and (f.endswith('.tar.gz') or f.endswith('.tar.gz.gpg')):
                        try:
                            os.remove(os.path.join(BACKUP_DIR, f))
                        except Exception:
                            pass
                emit_log("Local copies removed (backup on external drive)", 'info')
            except Exception:
                pass

    except Exception as e:
        history_entry['status'] = 'failed'
        history_entry['error'] = str(e)
        history_entry['duration'] = round(time.time() - start_time, 1)
        add_to_history(history_entry)
        with _progress_lock:
            progress_state['last'] = None
        emit_log(f"Backup error: {e}", 'error')
        _emit('backup_error', {'message': str(e)})
        try:
            from app import add_persistent_notification
            add_persistent_notification(
                'Backup failed',
                str(e)[:200],
                ntype='error',
                action={'app': 'backup', 'tab': 'history'}
            )
        except Exception:
            pass
    finally:
        with operation_lock:
            current_operation = None


# ── Restore ──

def run_restore(backup_file, target_path=None, archive_dir=None, decrypt_passphrase=None):
    global current_operation
    d = archive_dir or BACKUP_DIR
    temp_decrypted_files = []
    try:
        # ── Decryption step for encrypted backups ──
        if backup_file.endswith('.tar.gz.gpg'):
            if not decrypt_passphrase:
                raise Exception("Backup is encrypted. Enter the decryption password.")
            decrypted_name = backup_file[:-4]  # strip .gpg
            decrypted_path = os.path.join(d, decrypted_name)
            temp_decrypted_files.append(decrypted_path)
            emit_log("Decrypting archive (AES-256)...", 'info')
            decrypt_backup_gpg(os.path.join(d, backup_file), decrypt_passphrase, decrypted_path)
            emit_log("Decrypted successfully", 'success')
            backup_file = decrypted_name

        chain = detect_incremental_chain(backup_file, search_dir=d)

        # ── Decrypt any remaining encrypted chain members (e.g. full base of an incremental chain) ──
        for i, cf in enumerate(chain):
            if cf.endswith('.tar.gz.gpg'):
                if not decrypt_passphrase:
                    raise Exception(f"Chain file {cf} is encrypted — enter password.")
                cf_decrypted_name = cf[:-4]
                cf_decrypted_path = os.path.join(d, cf_decrypted_name)
                emit_log(f"Decrypting: {cf}...", 'info')
                decrypt_backup_gpg(os.path.join(d, cf), decrypt_passphrase, cf_decrypted_path)
                temp_decrypted_files.append(cf_decrypted_path)
                chain[i] = cf_decrypted_name
        extract_to = target_path if target_path else '/'
        if target_path:
            os.makedirs(target_path, exist_ok=True)

        if len(chain) > 1:
            emit_log(f"Incremental chain — {len(chain)} archives", 'info')
        else:
            emit_log(f"Restoring from {backup_file}...", 'info')

        if target_path:
            emit_log(f"Target: {target_path}", 'info')

        total_files = 0
        total_bytes = 0
        for f in chain:
            fpath = os.path.join(d, f)
            if not os.path.exists(fpath):
                raise Exception(f"File not found: {f}")
            total_bytes += os.path.getsize(fpath)
            result = subprocess.run(['tar', '-tzf', fpath], capture_output=True, text=True)
            if result.returncode == 0:
                total_files += len([l for l in result.stdout.strip().split('\n') if l])

        files_done = 0
        start_time = time.time()
        last_progress_time = 0

        for chain_idx, archive_file in enumerate(chain):
            archive_path = os.path.join(d, archive_file)
            cmd = ['tar', '-xvzf', archive_path, '-C', extract_to]
            if '_incr' in archive_file or len(chain) > 1:
                cmd.insert(1, '--listed-incremental=/dev/null')

            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if line:
                    files_done += 1
                    percent = (files_done / total_files * 100) if total_files > 0 else 0
                    elapsed = time.time() - start_time
                    eta = (elapsed / percent) * (100 - percent) if percent > 0 else None
                    now = time.time()
                    if total_files <= 100 or files_done % 10 == 0 or files_done == total_files or (now - last_progress_time) >= 0.5:
                        last_progress_time = now
                        emit_progress(
                            operation=f"Restoring: {archive_file}",
                            percent=percent, current_file=line[-60:] if len(line) > 60 else line,
                            files_done=files_done, total_files=total_files,
                            bytes_done=int(total_bytes * percent / 100),
                            total_bytes=total_bytes, eta=eta
                        )

            process.wait()
            if process.returncode >= 2:
                raise Exception(f"Extraction error for {archive_file} (code: {process.returncode})")
            elif process.returncode == 1:
                emit_log(f"Warnings during extraction of {archive_file}", 'warning')

        emit_log(f"Restore completed successfully! → {target_path or 'original locations'}", 'success')
        with _progress_lock:
            progress_state['last'] = None
        _emit('backup_complete', {'message': f'Restore completed: {backup_file}'})
        try:
            from app import add_persistent_notification
            add_persistent_notification(
                'Restore completed',
                f'{backup_file} → {target_path or "original locations"}',
                ntype='success',
                action={'app': 'backup', 'tab': 'history'}
            )
        except Exception:
            pass

    except Exception as e:
        emit_log(f"Restore error: {e}", 'error')
        with _progress_lock:
            progress_state['last'] = None
        _emit('backup_error', {'message': str(e)})
        try:
            from app import add_persistent_notification
            add_persistent_notification(
                'Restore failed',
                str(e)[:200],
                ntype='error',
                action={'app': 'backup', 'tab': 'history'}
            )
        except Exception:
            pass
    finally:
        # Clean up all temporary decrypted files (sensitive data)
        for tmp in temp_decrypted_files:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        with operation_lock:
            current_operation = None


# ── Scheduler ──

def log_scheduler(message, level='INFO'):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    logger.info(f"Scheduler: {message}")
    try:
        with open(SCHEDULER_LOG, 'a') as f:
            f.write(f"[{timestamp}] [{level}] {message}\n")
    except Exception:
        pass


def _calc_next_run(schedule, last_run_iso=None):
    try:
        now = datetime.now()
        sched_time = schedule.get('time', '03:00')
        hour, minute = int(sched_time.split(':')[0]), int(sched_time.split(':')[1])
        if schedule.get('type') == 'daily':
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate.isoformat()
        elif schedule.get('type') == 'weekly':
            days = schedule.get('days', [])
            if not days:
                return None
            for offset in range(8):
                candidate = now + timedelta(days=offset)
                candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate.weekday() in days and candidate > now:
                    return candidate.isoformat()
        return None
    except Exception:
        return None


def _scheduler_worker():
    """Background scheduler — runs as socketio background task (gevent-friendly)."""
    global current_operation
    log_scheduler("Scheduler started")
    while True:
        try:
            _socketio.sleep(30)
            with operation_lock:
                if current_operation is not None:
                    continue
            profiles = load_profiles()
            state = load_schedule_state()
            now = datetime.now()

            for profile in profiles:
                schedule = None
                if profile.get('schedule'):
                    try:
                        schedule = json.loads(profile['schedule']) if isinstance(profile['schedule'], str) else profile['schedule']
                    except Exception:
                        continue
                if not schedule or schedule.get('type') == 'manual':
                    continue

                profile_id = str(profile['id'])
                last_run_iso = state.get(profile_id, {}).get('last_run')
                sched_time = schedule.get('time', '03:00')
                try:
                    hour, minute = int(sched_time.split(':')[0]), int(sched_time.split(':')[1])
                except Exception:
                    continue

                should_run = False
                if schedule.get('type') == 'daily':
                    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if now >= target:
                        if not last_run_iso:
                            should_run = True
                        else:
                            last_run = datetime.fromisoformat(last_run_iso)
                            if last_run.date() < now.date() or (last_run.date() == now.date() and last_run.replace(hour=hour, minute=minute, second=0) < target):
                                should_run = True
                elif schedule.get('type') == 'weekly':
                    days = schedule.get('days', [])
                    if now.weekday() in days:
                        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if now >= target:
                            if not last_run_iso:
                                should_run = True
                            else:
                                last_run = datetime.fromisoformat(last_run_iso)
                                if last_run < target:
                                    should_run = True

                if should_run:
                    with operation_lock:
                        if current_operation is not None:
                            break
                        current_operation = 'backup'
                    log_scheduler(f"BACKUP STARTED — '{profile['name']}'")
                    state[profile_id] = {'last_run': now.isoformat()}
                    save_schedule_state(state)

                    destination = profile.get('destination')
                    if destination and destination.get('type') == 'ssh':
                        ssh_id = destination.get('server_id')
                        if ssh_id:
                            configs = load_ssh_configs()
                            ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
                            if ssh_cfg:
                                destination['config'] = ssh_cfg

                    retention = profile.get('retention', 0)
                    incremental = profile.get('incremental', False)
                    emit_log(f"Zaplanowany backup: {profile['name']}", 'info')
                    _socketio.start_background_task(_run_scheduled_backup, profile, destination, retention, incremental)
                    break
        except Exception as e:
            log_scheduler(f"Error: {e}", 'ERROR')


def _run_scheduled_backup(profile, destination, retention, incremental):
    """Wrapper for run_backup that logs scheduler outcome."""
    profile_name = profile['name']
    encryption = profile.get('encryption')

    # Resolve encryption passphrase for this scheduled run
    encrypt_passphrase = None
    if encryption and encryption.get('enabled'):
        enc_mode = encryption.get('mode', 'passphrase')
        if enc_mode == 'key':
            # Key mode: load stored_key from DB (load_profiles strips it for security)
            try:
                conn = get_db_connection()
                row = conn.execute('SELECT encryption FROM profiles WHERE id = ?', (profile['id'],)).fetchone()
                conn.close()
                if row and row['encryption']:
                    enc_full = json.loads(row['encryption'])
                    encrypt_passphrase = _resolve_encryption_passphrase(enc_full)
            except Exception as e:
                log_scheduler(f"BACKUP FAILED - profile '{profile_name}': encryption key error: {e}", 'ERROR')
                with operation_lock:
                    if current_operation == 'backup':
                        current_operation = None
                return
        else:
            log_scheduler(f"BACKUP SKIPPED - profile '{profile_name}' requires passphrase encryption — scheduled backups do not support passphrase encryption. Use 'Auto key' mode.", 'WARNING')
            emit_log(f"Scheduled backup '{profile_name}' skipped — profile uses passphrase encryption. Switch to 'Auto key' or run manually.", 'warning')
            with operation_lock:
                if current_operation == 'backup':
                    current_operation = None
            return

    start = time.time()
    try:
        run_backup(profile['paths'], destination, profile_name, retention, incremental, encrypt_passphrase)
        duration = round(time.time() - start, 1)
        log_scheduler(f"BACKUP COMPLETED - profile '{profile_name}' in {duration}s")
    except Exception as e:
        duration = round(time.time() - start, 1)
        log_scheduler(f"BACKUP FAILED - profile '{profile_name}' after {duration}s: {e}", 'ERROR')


# ═══════════════════════════════════════════════════════════
#  API Routes
# ═══════════════════════════════════════════════════════════

@backup_bp.route('/browse/roots')
def browse_roots():
    roots = []
    for r in _effective_browse_roots():
        try:
            stat = os.statvfs(r)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bfree * stat.f_frsize
            roots.append({'path': r, 'name': r, 'total': total, 'free': free})
        except OSError:
            roots.append({'path': r, 'name': r, 'total': None, 'free': None})
    return jsonify({'roots': roots})

@backup_bp.route('/browse')
def browse_directory():
    allowed = _effective_browse_roots()
    if not allowed:
        return jsonify({'error': 'Access denied'}), 403
    path = request.args.get('path', allowed[0])
    if not any(path.startswith(r) for r in allowed):
        path = allowed[0]
    items, err = _list_dir(path, allowed_prefix=allowed, timeout=5)
    if err:
        code = 404 if 'Not a directory' in err else 400 if 'outside' in err else 403
        return jsonify({'error': err}), code
    # Convert to camelCase keys + add sizeDisplay
    out = []
    for i in items:
        sz = i['size']
        if i['is_dir']:
            sd = 'folder'
        elif sz < 1024:
            sd = f'{sz} B'
        elif sz < 1048576:
            sd = f'{sz / 1024:.1f} KB'
        elif sz < 1073741824:
            sd = f'{sz / 1048576:.1f} MB'
        else:
            sd = f'{sz / 1073741824:.2f} GB'
        out.append({
            'name': i['name'], 'path': i['path'], 'isDir': i['is_dir'],
            'size': i['size'] if not i['is_dir'] else None,
            'sizeDisplay': sd, 'modified': i['modified'],
        })
    parent_path = os.path.dirname(path) if path not in allowed else None
    if parent_path and not any(parent_path.startswith(r) for r in allowed):
        parent_path = None
    return jsonify({'currentPath': path, 'parentPath': parent_path, 'items': out})


@backup_bp.route('/browse/mkdir', methods=['POST'])
def browse_mkdir():
    allowed = _effective_browse_roots()
    data = request.json or {}
    parent = data.get('path', '')
    name = data.get('name', '').strip()
    if not parent or not name:
        return jsonify({'error': 'Path and name required'}), 400
    if not any(parent.startswith(r) for r in allowed):
        return jsonify({'error': 'Invalid path'}), 400
    if '/' in name or '..' in name:
        return jsonify({'error': 'Invalid name'}), 400
    new_path = os.path.join(parent, name)
    if os.path.exists(new_path):
        return jsonify({'error': 'Folder already exists'}), 400
    try:
        os.makedirs(new_path)
        return jsonify({'success': True, 'path': new_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/paths', methods=['GET'])
def get_paths():
    return jsonify({'paths': load_paths()})

@backup_bp.route('/paths', methods=['POST'])
def add_path():
    data = request.json or {}
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    if not os.path.exists(path):
        return jsonify({'error': f'Path does not exist: {path}'}), 400
    paths = load_paths()
    if path not in paths:
        paths.append(path)
        save_paths(paths)
    return jsonify({'success': True, 'paths': paths})

@backup_bp.route('/paths', methods=['DELETE'])
def remove_path():
    data = request.json or {}
    path = data.get('path', '').strip()
    paths = load_paths()
    if path in paths:
        paths.remove(path)
        save_paths(paths)
    return jsonify({'success': True, 'paths': paths})


@backup_bp.route('/backups')
def list_backups():
    backups = []
    scanned_dirs = set()

    # Build mapping: destination_path -> profile info
    profiles = []
    dir_to_profile = {}
    try:
        profiles = load_profiles()
        for profile in profiles:
            dest = profile.get('destination')
            if not dest:
                continue
            if dest.get('type') == 'usb' and dest.get('path'):
                dir_to_profile[dest['path']] = {
                    'profile_id': profile.get('id'),
                    'profile_name': profile.get('name', '')
                }
            elif dest.get('type') == 'local' or not dest.get('type'):
                dir_to_profile[BACKUP_DIR] = {
                    'profile_id': profile.get('id'),
                    'profile_name': profile.get('name', '')
                }
    except Exception:
        pass

    def scan_dir(directory, location_label):
        if directory in scanned_dirs:
            return
        scanned_dirs.add(directory)
        if not os.path.isdir(directory):
            return
        prof = dir_to_profile.get(directory, {})
        try:
            for item in _fs_call_with_timeout(os.listdir, directory, timeout=5):
                is_backup = item.startswith('backup_') and (item.endswith('.tar.gz') or item.endswith('.tar.gz.gpg'))
                if is_backup:
                    fpath = os.path.join(directory, item)
                    try:
                        st = _fs_call_with_timeout(os.stat, fpath, timeout=3)
                        entry = {
                            'name': item, 'size': st.st_size,
                            'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
                            'location': location_label, 'path': fpath,
                            'encrypted': item.endswith('.gpg'),
                        }
                        if prof:
                            entry['profile_id'] = prof['profile_id']
                            entry['profile_name'] = prof['profile_name']
                        backups.append(entry)
                    except Exception:
                        pass
        except Exception:
            pass

    # 1. Local backups
    scan_dir(BACKUP_DIR, 'Lokalnie')

    # 2. Scan destinations from all profiles
    for profile in profiles:
        dest = profile.get('destination')
        if not dest:
            continue
        if dest.get('type') == 'usb' and dest.get('path'):
            scan_dir(dest['path'], f"USB: {dest['path']}")

    backups.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify({'backups': backups})

@backup_bp.route('/backups/<filename>', methods=['DELETE'])
def delete_backup(filename):
    # Support deleting from a specific path (passed as query param)
    custom_path = request.args.get('path')
    if custom_path and os.path.exists(custom_path) and (custom_path.endswith('.tar.gz') or custom_path.endswith('.tar.gz.gpg')):
        os.remove(custom_path)
        return jsonify({'success': True})
    bp = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(bp):
        os.remove(bp)
        return jsonify({'success': True})
    return jsonify({'error': 'Backup does not exist'}), 404


@backup_bp.route('/backup', methods=['POST'])
def start_backup():
    global current_operation
    with operation_lock:
        if current_operation is not None:
            return jsonify({'error': 'Inna operacja jest w toku'}), 400
        current_operation = 'backup'

    data = request.json or {}
    paths = data.get('paths', [])
    destination = data.get('destination')
    retention = data.get('retention', 0)
    incremental = data.get('incremental', False)
    encrypt_passphrase = data.get('encrypt_passphrase') or None

    if not paths:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Select at least one path'}), 400

    valid_paths = [p for p in paths if os.path.exists(p)]
    if not valid_paths:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'No paths exist'}), 400

    if destination:
        if destination['type'] == 'usb':
            if not os.path.exists(destination.get('path', '')):
                with operation_lock:
                    current_operation = None
                return jsonify({'error': 'USB is not available'}), 400
        elif destination['type'] == 'ssh':
            ssh_id = destination.get('server_id')
            if ssh_id:
                configs = load_ssh_configs()
                ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
                if not ssh_cfg:
                    with operation_lock:
                        current_operation = None
                    return jsonify({'error': 'SSH server not found'}), 400
                destination['config'] = ssh_cfg

    _socketio.start_background_task(run_backup, valid_paths, destination, retention=retention, incremental=incremental, encrypt_passphrase=encrypt_passphrase)
    return jsonify({'status': 'ok'})


@backup_bp.route('/backup-preview/<filename>')
def backup_preview(filename):
    custom_path = request.args.get('path')
    if custom_path and os.path.isfile(custom_path) and (custom_path.endswith('.tar.gz') or custom_path.endswith('.tar.gz.gpg')):
        bp = custom_path
        archive_dir = os.path.dirname(custom_path)
    else:
        bp = os.path.join(BACKUP_DIR, filename)
        archive_dir = BACKUP_DIR
    if not os.path.exists(bp):
        return jsonify({'error': 'File does not exist'}), 404
    st = os.stat(bp)
    # Encrypted backups cannot be previewed without passphrase
    if filename.endswith('.tar.gz.gpg') or bp.endswith('.tar.gz.gpg'):
        return jsonify({
            'filename': filename, 'size': st.st_size,
            'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
            'total_files': 0, 'is_incremental': '_incr' in filename,
            'encrypted': True, 'chain': [], 'top_dirs': {},
            'files': [], 'truncated': False
        })
    result = subprocess.run(['tar', '-tzf', bp], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return jsonify({'error': 'Cannot read archive'}), 500
    all_files = [l for l in result.stdout.strip().split('\n') if l]
    top_dirs = {}
    for f in all_files:
        top = f.split('/')[0]
        top_dirs[top] = top_dirs.get(top, 0) + 1
    chain = detect_incremental_chain(filename, search_dir=archive_dir)
    chain_info = []
    for cf in chain:
        cp = os.path.join(archive_dir, cf)
        if os.path.exists(cp):
            chain_info.append({'name': cf, 'size': os.path.getsize(cp), 'is_full': '_incr' not in cf})
    return jsonify({
        'filename': filename, 'size': st.st_size,
        'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
        'total_files': len(all_files), 'is_incremental': '_incr' in filename,
        'chain': chain_info, 'top_dirs': top_dirs,
        'files': all_files[:200], 'truncated': len(all_files) > 200
    })


@backup_bp.route('/restore', methods=['POST'])
def start_restore():
    global current_operation
    with operation_lock:
        if current_operation is not None:
            return jsonify({'error': 'Inna operacja jest w toku'}), 400
        current_operation = 'restore'

    data = request.json or {}
    backup_file = data.get('backup_file', '')
    backup_path = data.get('backup_path', '')
    target_path = data.get('target_path', '')
    decrypt_passphrase = data.get('decrypt_passphrase') or None
    if not backup_file:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Wybierz plik backupu'}), 400
    # Resolve archive location: prefer explicit path, fallback to BACKUP_DIR
    if backup_path and os.path.isfile(backup_path) and (backup_path.endswith('.tar.gz') or backup_path.endswith('.tar.gz.gpg')):
        bp = backup_path
        archive_dir = os.path.dirname(backup_path)
    else:
        bp = os.path.join(BACKUP_DIR, backup_file)
        archive_dir = BACKUP_DIR
    if not os.path.exists(bp):
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'File does not exist'}), 400
    # Validate passphrase is provided for encrypted backups
    if backup_file.endswith('.tar.gz.gpg') and not decrypt_passphrase:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Backup is encrypted — enter decryption password', 'encrypted': True}), 400
    restore_target = target_path or None
    if restore_target:
        try:
            os.makedirs(restore_target, exist_ok=True)
        except Exception as e:
            with operation_lock:
                current_operation = None
            return jsonify({'error': f'Cannot create directory: {e}'}), 400

    _socketio.start_background_task(run_restore, backup_file, restore_target, archive_dir, decrypt_passphrase)
    return jsonify({'status': 'ok'})


@backup_bp.route('/status')
def get_status():
    with operation_lock:
        return jsonify({'busy': current_operation is not None, 'operation': current_operation})

@backup_bp.route('/progress')
def get_progress():
    with _progress_lock:
        return jsonify({'progress': progress_state.get('last')})

@backup_bp.route('/usb-drives')
def get_usb_drives():
    return jsonify({'drives': detect_usb_drives()})

@backup_bp.route('/usb-browse')
def browse_usb():
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    allowed = ['/data/media', '/data/run_media', '/data/mnt', '/media', '/run/media', '/mnt']
    items, err = _list_dir(path, allowed_prefix=allowed, dirs_only=True, timeout=5)
    if err:
        code = 400 if 'outside' in err else 404 if 'Not a dir' in err else 403
        return jsonify({'error': err}), code
    # Convert to camelCase
    out = [{'name': i['name'], 'path': i['path'], 'isDir': True} for i in items]
    parent = os.path.dirname(path)
    if not any(parent.startswith(p) for p in allowed):
        parent = None
    usb_drives = detect_usb_drives()
    usb_roots = set(d['path'] for d in usb_drives)
    if path in usb_roots:
        parent = None
    return jsonify({'currentPath': path, 'parentPath': parent, 'items': out})

@backup_bp.route('/usb-mkdir', methods=['POST'])
def create_usb_folder():
    data = request.json or {}
    parent = data.get('path', '')
    name = data.get('name', '').strip()
    if not parent or not name:
        return jsonify({'error': 'Path and name required'}), 400
    allowed = ['/data/media', '/data/run_media', '/data/mnt', '/media', '/run/media', '/mnt']
    if not any(parent.startswith(p) for p in allowed):
        return jsonify({'error': 'Invalid path'}), 400
    if '/' in name or '..' in name:
        return jsonify({'error': 'Invalid name'}), 400
    new_path = os.path.join(parent, name)
    if os.path.exists(new_path):
        return jsonify({'error': 'Folder already exists'}), 400
    try:
        os.makedirs(new_path)
        return jsonify({'success': True, 'path': new_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/trigger-smart', methods=['POST'])
def trigger_smart_backup():
    """Trigger a backup due to SMART warning."""
    global current_operation

    # Check if a backup is already running
    with operation_lock:
        if current_operation is not None:
             return jsonify({'status': 'busy', 'message': 'Backup already in progress'}), 200

    # Find a suitable profile
    profiles = load_profiles()
    target_profile = None

    # 1. Look for explicit "SMART" profile
    for p in profiles:
        if 'smart' in p['name'].lower():
            target_profile = p
            break

    # 2. Look for "System" profile
    if not target_profile:
        for p in profiles:
            if 'system' in p['name'].lower():
                target_profile = p
                break

    # 3. Fallback to any profile
    if not target_profile and profiles:
        target_profile = profiles[0]

    if target_profile:
        destination = target_profile.get('destination')
        # Load SSH config if needed
        if destination and destination.get('type') == 'ssh':
             ssh_id = destination.get('server_id')
             if ssh_id:
                 configs = load_ssh_configs()
                 ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
                 if ssh_cfg:
                     destination['config'] = ssh_cfg

        retention = target_profile.get('retention', 0)
        incremental = target_profile.get('incremental', False)

        emit_log(f"SMART Alert triggered backup: {target_profile['name']}", 'warning')

        with operation_lock:
             current_operation = 'backup'

        _socketio.start_background_task(_run_scheduled_backup, target_profile, destination, retention, incremental)
        return jsonify({'status': 'started', 'profile': target_profile['name']})
    else:
        return jsonify({'status': 'no_profile', 'message': 'No backup profiles configured'}), 400


@backup_bp.route('/ssh-servers', methods=['GET'])
def get_ssh_servers():
    configs = load_ssh_configs()
    safe = [{k: v for k, v in c.items() if k != 'password'} | {'has_password': bool(c.get('password'))} for c in configs]
    return jsonify({'servers': safe})

@backup_bp.route('/ssh-servers', methods=['POST'])
def add_ssh_server():
    data = request.json or {}
    for field in ['name', 'host', 'username']:
        if not data.get(field):
            return jsonify({'error': f'{field} required'}), 400
    configs = load_ssh_configs()
    # Prevent duplicates (same host + username)
    for existing in configs:
        if existing.get('host') == data['host'] and existing.get('username') == data['username']:
            # Update existing instead of adding duplicate
            existing['name'] = data['name']
            existing['port'] = int(data.get('port', 22))
            existing['password'] = data.get('password', '')
            existing['key_path'] = data.get('key_path', '')
            existing['remote_path'] = data.get('remote_path', '~/backups')
            save_ssh_configs(configs)
            return jsonify({'success': True, 'server': {k: v for k, v in existing.items() if k != 'password'}, 'updated': True})
    new = {
        'id': datetime.now().strftime("%Y%m%d%H%M%S"),
        'name': data['name'], 'host': data['host'],
        'port': int(data.get('port', 22)), 'username': data['username'],
        'password': data.get('password', ''), 'key_path': data.get('key_path', ''),
        'remote_path': data.get('remote_path', '~/backups')
    }
    configs.append(new)
    save_ssh_configs(configs)
    return jsonify({'success': True, 'server': {k: v for k, v in new.items() if k != 'password'}})

@backup_bp.route('/ssh-servers/<server_id>', methods=['DELETE'])
def delete_ssh_server(server_id):
    configs = [c for c in load_ssh_configs() if c.get('id') != server_id]
    save_ssh_configs(configs)
    return jsonify({'success': True})

@backup_bp.route('/ssh-servers/test', methods=['POST'])
def test_ssh():
    data = request.json or {}
    # If called with just 'id', load full server config
    server_id = data.get('id')
    if server_id:
        configs = load_ssh_configs()
        srv = next((c for c in configs if c.get('id') == server_id), None)
        if not srv:
            return jsonify({'success': False, 'error': 'Server not found'}), 404
        host = srv.get('host')
        port = int(srv.get('port', 22))
        username = srv.get('username')
        password = srv.get('password')
        key_path = srv.get('key_path')
        remote_path = srv.get('remote_path')
    else:
        host = data.get('host')
        port = int(data.get('port', 22))
        username = data.get('username')
        password = data.get('password')
        key_path = data.get('key_path')
        remote_path = data.get('remote_path')

    if not host or not username:
        return jsonify({'success': False, 'error': 'Host or username missing'}), 400

    ok, result = test_ssh_connection(host, port, username, password, key_path, remote_path)
    if ok:
        return jsonify({'success': True, 'name': data.get('name') or (srv.get('name') if server_id else ''), 'disk_info': result})
    return jsonify({'success': False, 'error': result}), 400


@backup_bp.route('/history')
def get_history_route():
    return jsonify({'history': load_history()})

@backup_bp.route('/history/<entry_id>', methods=['DELETE'])
def delete_history_entry(entry_id):
    history = [h for h in load_history() if h.get('id') != entry_id]
    save_history(history)
    return jsonify({'success': True})


@backup_bp.route('/profiles', methods=['GET'])
def get_profiles():
    return jsonify({'profiles': load_profiles()})

@backup_bp.route('/profiles', methods=['POST'])
def create_profile():
    data = request.json or {}
    if not data.get('name'):
        return jsonify({'error': 'Name required'}), 400
    if not data.get('paths'):
        return jsonify({'error': 'Paths required'}), 400
    enc_input = data.get('encryption')
    enc, generated_key = _prepare_encryption(enc_input)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO profiles (name, paths, destination, schedule, options, retention, incremental, encryption) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (
        data['name'], json.dumps(data['paths']),
        json.dumps(data.get('destination')) if data.get('destination') else None,
        json.dumps(data['schedule']) if data.get('schedule') else None,
        data.get('options'), data.get('retention', 0),
        1 if data.get('incremental') else 0,
        json.dumps(enc) if enc else None,
    ))
    conn.commit()
    pid = c.lastrowid
    conn.close()
    enc_public = {k: v for k, v in enc.items() if k != 'stored_key'} if enc else None
    resp = {'success': True, 'profile': {'id': str(pid), 'name': data['name'], 'paths': data['paths'], 'destination': data.get('destination'), 'schedule': data.get('schedule'), 'retention': data.get('retention', 0), 'incremental': bool(data.get('incremental')), 'encryption': enc_public}}
    if generated_key:
        resp['generated_key'] = generated_key
    return jsonify(resp)


@backup_bp.route('/profiles/export', methods=['GET'])
def export_profiles():
    """Export all profiles as JSON for download."""
    profiles = load_profiles()
    export_data = {
        'version': 1,
        'exported_at': datetime.now().isoformat(),
        'profiles': []
    }
    for p in profiles:
        export_data['profiles'].append({
            'name': p['name'],
            'paths': p['paths'],
            'destination': p['destination'],
            'schedule': p['schedule'],
            'options': p.get('options'),
            'retention': p.get('retention', 0),
            'incremental': p.get('incremental', False),
            'encryption': p.get('encryption'),
        })
    return jsonify(export_data)


@backup_bp.route('/profiles/import', methods=['POST'])
def import_profiles():
    """Import profiles from JSON. Supports both file upload and JSON body."""
    try:
        # Support file upload
        if request.files and 'file' in request.files:
            file = request.files['file']
            import_data = json.loads(file.read().decode('utf-8'))
        else:
            import_data = request.json or {}

        if not import_data:
            return jsonify({'error': 'No data to import'}), 400

        # Support both wrapped format (with 'profiles' key) and raw array
        if isinstance(import_data, list):
            profiles_to_import = import_data
        elif isinstance(import_data, dict):
            profiles_to_import = import_data.get('profiles', [])
        else:
            return jsonify({'error': 'Invalid data format'}), 400

        if not profiles_to_import:
            return jsonify({'error': 'No profiles to import'}), 400

        imported = 0
        skipped = 0
        errors = []
        existing = load_profiles()
        existing_names = {p['name'] for p in existing}

        conn = get_db_connection()
        c = conn.cursor()
        for idx, p in enumerate(profiles_to_import):
            try:
                name = p.get('name', '').strip()
                paths = p.get('paths', [])
                if not name:
                    errors.append(f'Profil #{idx+1}: brak nazwy')
                    skipped += 1
                    continue
                if not paths:
                    errors.append(f'Profile "{name}": no paths')
                    skipped += 1
                    continue

                # Auto-rename duplicates
                original_name = name
                counter = 1
                while name in existing_names:
                    name = f"{original_name} ({counter})"
                    counter += 1

                destination = p.get('destination')
                schedule = p.get('schedule')
                enc_import = p.get('encryption')
                # When importing, strip any stored_key (it was encrypted on the exporting machine)
                # and regenerate a new key if needed
                enc, generated_key = _prepare_encryption(
                    {k: v for k, v in enc_import.items() if k != 'stored_key'} if enc_import else None
                )
                c.execute(
                    'INSERT INTO profiles (name, paths, destination, schedule, options, retention, incremental, encryption) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        name,
                        json.dumps(paths) if isinstance(paths, list) else paths,
                        json.dumps(destination) if destination else None,
                        json.dumps(schedule) if schedule else None,
                        p.get('options'),
                        p.get('retention', 0),
                        1 if p.get('incremental') else 0,
                        json.dumps(enc) if enc else None,
                    )
                )
                existing_names.add(name)
                imported += 1
            except Exception as e:
                errors.append(f'Profil "{p.get("name", "?")}": {str(e)}')
                skipped += 1

        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'imported': imported,
            'skipped': skipped,
            'errors': errors,
            'message': f'Imported {imported} profiles' + (f', skipped {skipped}' if skipped else '')
        })
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid JSON format'}), 400
    except Exception as e:
        return jsonify({'error': f'Import error: {str(e)}'}), 500


@backup_bp.route('/profiles/<profile_id>/schedule', methods=['PUT'])
def update_profile_schedule(profile_id):
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404
    c.execute('UPDATE profiles SET schedule=? WHERE id=?', (json.dumps(data.get('schedule')) if data.get('schedule') else None, profile_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>', methods=['PUT'])
def update_profile(profile_id):
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404
    enc_input = data.get('encryption')
    existing_enc = None
    try:
        existing_enc = json.loads(row['encryption']) if row['encryption'] else None
    except Exception:
        pass
    enc, generated_key = _prepare_encryption(enc_input, existing_enc)
    c.execute('UPDATE profiles SET name=?, paths=?, destination=?, schedule=?, options=?, retention=?, incremental=?, encryption=? WHERE id=?', (
        data.get('name', row['name']),
        json.dumps(data.get('paths', json.loads(row['paths']))),
        json.dumps(data.get('destination')) if data.get('destination') else row['destination'],
        json.dumps(data.get('schedule')) if data.get('schedule') else row['schedule'],
        data.get('options', row['options']),
        data.get('retention', row['retention'] or 0),
        1 if data.get('incremental') else 0,
        json.dumps(enc) if enc is not None else None,
        profile_id
    ))
    conn.commit()
    updated = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    conn.close()
    if updated:
        enc_val = None
        try:
            enc_val = json.loads(updated['encryption']) if updated['encryption'] else None
        except Exception:
            pass
        enc_public = {k: v for k, v in enc_val.items() if k != 'stored_key'} if enc_val else None
        resp = {'success': True, 'profile': {
            'id': str(updated['id']), 'name': updated['name'],
            'paths': json.loads(updated['paths']),
            'destination': json.loads(updated['destination']) if updated['destination'] else None,
            'schedule': json.loads(updated['schedule']) if updated['schedule'] else None,
            'retention': updated['retention'] or 0,
            'incremental': bool(updated['incremental']),
            'encryption': enc_public,
        }}
        if generated_key:
            resp['generated_key'] = generated_key
        return jsonify(resp)
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>', methods=['DELETE'])
def delete_profile(profile_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM profiles WHERE id = ?', (profile_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>/run', methods=['POST'])
def run_profile(profile_id):
    global current_operation
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404
    enc = None
    try:
        enc = json.loads(row['encryption']) if row['encryption'] else None
    except Exception:
        pass
    profile = {
        'id': str(row['id']), 'name': row['name'],
        'paths': json.loads(row['paths']),
        'destination': json.loads(row['destination']) if row['destination'] else None,
        'retention': row['retention'] or 0,
        'incremental': bool(row['incremental']) if row['incremental'] else False,
        'encryption': enc,
    }
    conn.close()

    # Resolve encryption passphrase
    data = request.json or {}
    encrypt_passphrase = data.get('encrypt_passphrase') or None
    if enc and enc.get('enabled'):
        if enc.get('mode') == 'key':
            # Key mode: resolve from stored key automatically
            try:
                encrypt_passphrase = _resolve_encryption_passphrase(enc)
            except Exception as e:
                return jsonify({'error': f'Encryption key error: {e}'}), 500
        elif not encrypt_passphrase:
            return jsonify({'error': 'Profile has encryption enabled — enter password', 'needs_passphrase': True}), 400

    with operation_lock:
        if current_operation is not None:
            return jsonify({'error': 'Inna operacja jest w toku'}), 400
        current_operation = 'backup'

    destination = profile.get('destination')
    if destination and destination.get('type') == 'ssh':
        ssh_id = destination.get('server_id')
        if ssh_id:
            configs = load_ssh_configs()
            ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
            if ssh_cfg:
                destination['config'] = ssh_cfg

    _socketio.start_background_task(run_backup, profile['paths'], destination, profile['name'], profile['retention'], profile['incremental'], encrypt_passphrase)
    return jsonify({'status': 'ok', 'profile': profile["name"]})


@backup_bp.route('/profiles/<profile_id>/key', methods=['GET'])
def get_profile_key(profile_id):
    """Return the plaintext encryption key for a key-mode profile (for user backup)."""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Profile not found'}), 404
    enc = None
    try:
        enc = json.loads(row['encryption']) if row['encryption'] else None
    except Exception:
        pass
    if not enc or not enc.get('enabled') or enc.get('mode') != 'key':
        return jsonify({'error': 'Profile does not use key mode'}), 400
    try:
        key = _resolve_encryption_passphrase(enc)
        return jsonify({'key': key})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@backup_bp.route('/scheduled-backups')
def get_scheduled_backups():
    profiles = load_profiles()
    state = load_schedule_state()
    scheduled = []
    for p in profiles:
        schedule = p.get('schedule')
        if isinstance(schedule, str):
            try:
                schedule = json.loads(schedule)
            except Exception:
                continue
        if schedule and schedule.get('type') != 'manual':
            last_run = state.get(str(p['id']), {}).get('last_run')
            scheduled.append({
                'profile_id': p['id'], 'profile_name': p['name'],
                'schedule': schedule, 'last_run': last_run,
                'next_run': _calc_next_run(schedule, last_run)
            })
    return jsonify({'scheduled': scheduled})


# ═══════════════════════════════════════════════════════════
#  SYSTEM SNAPSHOTS  — Punkty przywracania systemu
#  Backs up: EthOS config/data, Docker compose projects,
#  Docker volumes, container list, system configs.
# ═══════════════════════════════════════════════════════════

SNAPSHOTS_DIR = os.path.join(BACKUP_DIR, 'snapshots')
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

_snapshot_state = {
    'status': 'idle',   # idle | creating | restoring | done | error
    'percent': 0,
    'message': '',
    'log': [],
    '_started': 0,      # timestamp when operation started (for stuck detection)
}


def _snap_update(status=None, percent=None, message=None, log=None):
    if status:
        _snapshot_state['status'] = status
    if percent is not None:
        _snapshot_state['percent'] = percent
    if message:
        _snapshot_state['message'] = message
    if log:
        _snapshot_state['log'].append(log)
        _emit('snapshot_log', {'message': log})
    _emit('snapshot_progress', {
        'status': _snapshot_state['status'],
        'percent': _snapshot_state['percent'],
        'message': _snapshot_state['message'],
    })


def _docker_cmd(args, timeout=120):
    """Run a docker command, return (stdout, returncode)."""
    try:
        r = subprocess.run(
            ['docker'] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return '', 1


def _docker_ok():
    _, rc = _docker_cmd(['info'], timeout=5)
    return rc == 0


# ── Btrfs native snapshots (instant CoW) ──
BTRFS_SNAPSHOTS_MOUNT = '/mnt/snapshots'
BTRFS_DATA_MOUNT = '/mnt/data'


def _btrfs_available():
    """Check if /mnt/data is a btrfs filesystem with snapshot support."""
    if not os.path.ismount(BTRFS_DATA_MOUNT):
        return False
    r = host_run(f'stat -f -c %T {q(BTRFS_DATA_MOUNT)}', timeout=5)
    return r.returncode == 0 and 'btrfs' in r.stdout.lower()


def _ensure_snapshots_mounted():
    """Mount @snapshots subvolume if not already mounted."""
    if os.path.ismount(BTRFS_SNAPSHOTS_MOUNT):
        return True
    os.makedirs(BTRFS_SNAPSHOTS_MOUNT, exist_ok=True)
    # Find the device for /mnt/data
    r = host_run(f"findmnt -n -o SOURCE {q(BTRFS_DATA_MOUNT)}", timeout=5)
    if r.returncode != 0 or not r.stdout.strip():
        return False
    dev = r.stdout.strip().split('[')[0]  # strip subvol suffix like [/@data]
    r = host_run(
        f"mount -o subvol=@snapshots,noatime,compress=zstd:3 {q(dev)} {q(BTRFS_SNAPSHOTS_MOUNT)}",
        timeout=15
    )
    return r.returncode == 0


@backup_bp.route('/btrfs-snapshots', methods=['GET'])
def list_btrfs_snapshots():
    """List native btrfs snapshots of the data partition."""
    if not _btrfs_available():
        return jsonify({'ok': False, 'error': 'Btrfs not available', 'snapshots': []})
    if not _ensure_snapshots_mounted():
        return jsonify({'ok': False, 'error': 'Cannot mount @snapshots', 'snapshots': []})

    snapshots = []
    try:
        for name in sorted(os.listdir(BTRFS_SNAPSHOTS_MOUNT), reverse=True):
            snap_path = os.path.join(BTRFS_SNAPSHOTS_MOUNT, name)
            if not os.path.isdir(snap_path):
                continue
            # Read metadata if available
            meta_file = os.path.join(snap_path, '.snap_meta.json')
            meta = {'id': name, 'type': 'btrfs'}
            if os.path.isfile(meta_file):
                try:
                    with open(meta_file) as f:
                        meta.update(json.load(f))
                except Exception:
                    pass
            # Get btrfs subvolume info for creation time
            r = host_run(f'btrfs subvolume show {q(snap_path)}', timeout=10)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if line.startswith('Creation time:'):
                        meta['btrfs_created'] = line.split(':', 1)[1].strip()
            if 'created' not in meta:
                meta['created'] = meta.get('btrfs_created', name)
            snapshots.append(meta)
    except Exception as e:
        logger.exception("Error listing btrfs snapshots")
        return jsonify({'ok': False, 'error': str(e), 'snapshots': []})

    return jsonify({'ok': True, 'snapshots': snapshots})


@backup_bp.route('/btrfs-snapshot', methods=['POST'])
def create_btrfs_snapshot():
    """Create an instant btrfs snapshot of the data partition (@data → @snapshots)."""
    if not _btrfs_available():
        return jsonify({'error': 'Btrfs not available on data partition'}), 400
    if not _ensure_snapshots_mounted():
        return jsonify({'error': 'Cannot mount @snapshots subvolume'}), 500

    data = request.json or {}
    label = data.get('label', '').strip()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    snap_name = f'snap_{ts}'
    snap_path = os.path.join(BTRFS_SNAPSHOTS_MOUNT, snap_name)

    # Create read-only btrfs snapshot (instant, CoW — O(1) operation)
    r = host_run(
        f'btrfs subvolume snapshot -r {q(BTRFS_DATA_MOUNT)} {q(snap_path)}',
        timeout=30
    )
    if r.returncode != 0:
        return jsonify({'error': f'Snapshot failed: {r.stderr[:200]}'}), 500

    # Write metadata
    meta = {
        'id': snap_name,
        'label': label or f'Snapshot {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        'created': datetime.now().isoformat(),
        'type': 'btrfs',
        'hostname': _read_file_safe('/etc/hostname', 'unknown').strip(),
    }
    try:
        with open(os.path.join(snap_path, '.snap_meta.json'), 'w') as f:
            json.dump(meta, f, indent=2)
    except OSError:
        pass  # read-only snapshot — metadata write may fail, that's OK

    _emit('snapshot_created', meta)
    logger.info("Btrfs snapshot created: %s", snap_name)
    return jsonify({'ok': True, 'snapshot': meta})


@backup_bp.route('/btrfs-snapshot/<snap_id>', methods=['DELETE'])
def delete_btrfs_snapshot(snap_id):
    """Delete a btrfs snapshot."""
    if not re.match(r'^snap_\d{8}_\d{6}$', snap_id):
        return jsonify({'error': 'Invalid snapshot ID'}), 400
    snap_path = os.path.join(BTRFS_SNAPSHOTS_MOUNT, snap_id)
    if not os.path.isdir(snap_path):
        return jsonify({'error': 'Snapshot not found'}), 404

    r = host_run(f'btrfs subvolume delete {q(snap_path)}', timeout=30)
    if r.returncode != 0:
        return jsonify({'error': f'Delete failed: {r.stderr[:200]}'}), 500

    logger.info("Btrfs snapshot deleted: %s", snap_id)
    return jsonify({'ok': True})


@backup_bp.route('/btrfs-snapshot/<snap_id>/rollback', methods=['POST'])
def rollback_btrfs_snapshot(snap_id):
    """Rollback data partition to a btrfs snapshot.

    This replaces @data with the snapshot content. The current @data
    is renamed to @data.replaced. Requires a service restart.
    """
    if not re.match(r'^snap_\d{8}_\d{6}$', snap_id):
        return jsonify({'error': 'Invalid snapshot ID'}), 400
    if not _ensure_snapshots_mounted():
        return jsonify({'error': 'Cannot mount @snapshots'}), 500

    snap_path = os.path.join(BTRFS_SNAPSHOTS_MOUNT, snap_id)
    if not os.path.isdir(snap_path):
        return jsonify({'error': 'Snapshot not found'}), 404

    # Find the btrfs device
    r = host_run(f"findmnt -n -o SOURCE {q(BTRFS_DATA_MOUNT)}", timeout=5)
    if r.returncode != 0:
        return jsonify({'error': 'Cannot find data device'}), 500
    dev = r.stdout.strip().split('[')[0]

    # Mount top-level btrfs (subvolid=5) to manipulate subvolumes
    top_mount = '/tmp/btrfs-rollback'
    os.makedirs(top_mount, exist_ok=True)
    try:
        host_run(f'umount {q(top_mount)} 2>/dev/null', timeout=10)
        r = host_run(f'mount -o subvolid=5 {q(dev)} {q(top_mount)}', timeout=15)
        if r.returncode != 0:
            return jsonify({'error': f'Cannot mount top-level btrfs: {r.stderr[:200]}'}), 500

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        data_old = os.path.join(top_mount, f'@data.replaced.{ts}')
        data_new = os.path.join(top_mount, '@data')
        snap_src = os.path.join(top_mount, f'@snapshots/{snap_id}')

        # Unmount @data before rename
        host_run(f'umount {q(BTRFS_DATA_MOUNT)} 2>/dev/null', timeout=15)

        # Rename current @data
        r = host_run(f'mv {q(data_new)} {q(data_old)}', timeout=15)
        if r.returncode != 0:
            # Try to remount and bail
            host_run(f'mount -o subvol=@data,noatime,compress=zstd:3 {q(dev)} {q(BTRFS_DATA_MOUNT)}', timeout=15)
            return jsonify({'error': f'Cannot rename @data: {r.stderr[:200]}'}), 500

        # Create writable snapshot from the read-only backup
        r = host_run(f'btrfs subvolume snapshot {q(snap_src)} {q(data_new)}', timeout=30)
        if r.returncode != 0:
            # Restore original
            host_run(f'mv {q(data_old)} {q(data_new)}', timeout=15)
            host_run(f'mount -o subvol=@data,noatime,compress=zstd:3 {q(dev)} {q(BTRFS_DATA_MOUNT)}', timeout=15)
            return jsonify({'error': f'Snapshot restore failed: {r.stderr[:200]}'}), 500

        # Remount the new @data
        r = host_run(f'mount -o subvol=@data,noatime,compress=zstd:3 {q(dev)} {q(BTRFS_DATA_MOUNT)}', timeout=15)

        # Clean up old @data (in background — may take time)
        host_run(f'btrfs subvolume delete {q(data_old)} 2>/dev/null', timeout=60)

        logger.info("Btrfs rollback to %s complete", snap_id)
        _emit('snapshot_rollback', {'snapshot': snap_id})

    finally:
        host_run(f'umount {q(top_mount)} 2>/dev/null', timeout=10)

    return jsonify({'ok': True, 'message': f'Rolled back to {snap_id}. Restart EthOS to apply.'})


@backup_bp.route('/btrfs-info', methods=['GET'])
def btrfs_info():
    """Return btrfs filesystem info for the data partition."""
    if not _btrfs_available():
        return jsonify({'ok': False, 'btrfs': False})
    r = host_run(f'btrfs filesystem usage -b {q(BTRFS_DATA_MOUNT)}', timeout=10)
    info = {'ok': True, 'btrfs': True, 'raw': r.stdout if r.returncode == 0 else ''}
    # Parse key metrics
    for line in (r.stdout or '').splitlines():
        line = line.strip()
        if line.startswith('Device size:'):
            info['device_size'] = int(''.join(c for c in line.split(':')[1] if c.isdigit()) or 0)
        elif line.startswith('Used:'):
            info['used'] = int(''.join(c for c in line.split(':')[1] if c.isdigit()) or 0)
        elif line.startswith('Free (estimated):'):
            val = line.split(':')[1].strip().split()[0]
            info['free'] = int(''.join(c for c in val if c.isdigit()) or 0)
    return jsonify(info)


@backup_bp.route('/snapshots', methods=['GET'])
def list_snapshots():
    """List all system snapshots (local + received from other NAS devices)."""
    snapshots = []
    if os.path.isdir(SNAPSHOTS_DIR):
        for name in sorted(os.listdir(SNAPSHOTS_DIR), reverse=True):
            snap_dir = os.path.join(SNAPSHOTS_DIR, name)
            if not os.path.isdir(snap_dir):
                continue
            meta_file = os.path.join(snap_dir, 'meta.json')
            if os.path.isfile(meta_file):
                try:
                    with open(meta_file, 'r') as f:
                        meta = json.load(f)
                    # Calculate total size
                    total_size = 0
                    for root, dirs, files in os.walk(snap_dir):
                        for fn in files:
                            total_size += os.path.getsize(os.path.join(root, fn))
                    meta['size'] = total_size
                    meta['dir'] = name
                    meta['received'] = False
                    snapshots.append(meta)
                except Exception:
                    pass

    # Also include received snapshots from other NAS devices
    try:
        received = _find_received_snapshot_dirs()
        for r in received:
            r['received'] = True
            snapshots.append(r)
    except Exception:
        logger.exception("Error scanning received snapshots for main list")

    # Sort all by creation date, newest first
    snapshots.sort(key=lambda s: s.get('created', ''), reverse=True)
    return jsonify({'snapshots': snapshots})


@backup_bp.route('/snapshots/status', methods=['GET'])
def snapshot_status():
    """Current snapshot operation status."""
    return jsonify(_snapshot_state)


@backup_bp.route('/snapshots/reset', methods=['POST'])
def reset_snapshot_state():
    """Manually reset a stuck snapshot operation state."""
    old_status = _snapshot_state['status']
    _snapshot_state['status'] = 'idle'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = ''
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = 0
    logger.info(f"Snapshot state manually reset from '{old_status}' to 'idle'")
    return jsonify({'ok': True, 'previous_status': old_status})


@backup_bp.route('/snapshots', methods=['POST'])
def create_snapshot():
    """Create a new system snapshot."""
    # Detect stuck state: if operation running for more than 30 minutes, auto-reset
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        elapsed = time.time() - _snapshot_state.get('_started', 0)
        if elapsed > 1800:  # 30 minutes
            logger.warning(f"Snapshot state '{_snapshot_state['status']}' stuck for {elapsed:.0f}s — auto-resetting")
            _snapshot_state['status'] = 'idle'
        else:
            return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    label = data.get('label', '').strip()
    include_docker = data.get('include_docker', True)
    include_volumes = data.get('include_volumes', True)
    include_ethos = data.get('include_ethos', True)
    include_system = data.get('include_system', True)
    include_userdirs = data.get('include_userdirs', True)
    include_vms = data.get('include_vms', False)
    include_models = data.get('include_models', False)
    dest_type = data.get('dest_type', 'local')  # 'local' or 'usb'
    dest_path = data.get('dest_path', '')

    _snapshot_state['status'] = 'creating'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam...'
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = time.time()

    if _socketio:
        _socketio.start_background_task(
            _create_snapshot_worker,
            label, include_docker, include_volumes, include_ethos, include_system,
            dest_type, dest_path, include_vms, include_models, include_userdirs
        )
    else:
        logger.error("SocketIO not initialized — cannot start snapshot worker")
        _snapshot_state['status'] = 'error'
        _snapshot_state['message'] = 'Internal error: SocketIO not initialized'
    return jsonify({'status': 'ok'})


@backup_bp.route('/snapshots/<snap_id>/browse', methods=['GET'])
def browse_snapshot(snap_id):
    """Browse the contents of a snapshot directory."""
    if not re.match(r'^snap_\d{8}_\d{6}$', snap_id):
        return jsonify({'error': 'Invalid snapshot identifier'}), 400
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404
    tree = []
    for root, dirs, files in os.walk(snap_dir):
        rel = os.path.relpath(root, snap_dir)
        if rel == '.':
            rel = ''
        for d in sorted(dirs):
            tree.append({'name': os.path.join(rel, d) if rel else d, 'type': 'dir', 'size': 0})
        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            tree.append({'name': os.path.join(rel, f) if rel else f, 'type': 'file', 'size': sz})
    return jsonify({'tree': tree, 'snap_id': snap_id})


@backup_bp.route('/snapshots/space', methods=['GET'])
def snapshot_space():
    """Get total space used by snapshots and disk free space."""
    total_size = 0
    count = 0
    if os.path.isdir(SNAPSHOTS_DIR):
        for name in os.listdir(SNAPSHOTS_DIR):
            d = os.path.join(SNAPSHOTS_DIR, name)
            if os.path.isdir(d):
                count += 1
                for root, dirs, files in os.walk(d):
                    for f in files:
                        try:
                            total_size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
    try:
        st = os.statvfs(SNAPSHOTS_DIR)
        disk_free = st.f_bavail * st.f_frsize
        disk_total = st.f_blocks * st.f_frsize
    except Exception:
        disk_free = 0
        disk_total = 0
    return jsonify({
        'snapshot_bytes': total_size,
        'snapshot_count': count,
        'disk_free': disk_free,
        'disk_total': disk_total,
    })


@backup_bp.route('/snapshots/<snap_id>', methods=['DELETE'])
def delete_snapshot(snap_id):
    """Delete a snapshot."""
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404
    shutil.rmtree(snap_dir, ignore_errors=True)
    return jsonify({'ok': True})


@backup_bp.route('/snapshots/<snap_id>/restore', methods=['POST'])
def restore_snapshot(snap_id):
    """Restore system from a snapshot."""
    if _snapshot_state['status'] in ('creating', 'restoring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404

    data = request.json or {}
    restore_docker = data.get('restore_docker', True)
    restore_volumes = data.get('restore_volumes', True)
    restore_ethos = data.get('restore_ethos', True)
    restore_system = data.get('restore_system', True)

    _snapshot_state['status'] = 'restoring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam przywracanie...'
    _snapshot_state['log'] = []

    if _socketio:
        _socketio.start_background_task(
            _restore_snapshot_worker,
            snap_dir, restore_docker, restore_volumes, restore_ethos, restore_system
        )
    return jsonify({'status': 'ok'})


@backup_bp.route('/snapshots/<snap_id>/download')
def download_snapshot(snap_id):
    """Download snapshot as tar.gz."""
    from flask import send_file as _send
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404
    archive = os.path.join(SNAPSHOTS_DIR, f'{snap_id}.tar.gz')
    if not os.path.isfile(archive):
        subprocess.run(['tar', '-czf', archive, '-C', SNAPSHOTS_DIR, snap_id],
                       capture_output=True, timeout=600)
    if os.path.isfile(archive):
        return _send(archive, as_attachment=True, download_name=f'snapshot-{snap_id}.tar.gz')
    return jsonify({'error': 'Failed to compress'}), 500


# ── NAS-to-NAS snapshot transfer ──

@backup_bp.route('/snapshots/<snap_id>/transfer', methods=['POST'])
def transfer_snapshot(snap_id):
    """Transfer (push) a snapshot to a remote NAS via SSH/SCP."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko/scp not installed'}), 500
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot does not exist'}), 404

    data = request.json or {}
    server_id = data.get('server_id')
    if not server_id:
        return jsonify({'error': 'No server selected'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'SSH server not found'}), 404

    _snapshot_state['status'] = 'transferring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam transfer...'
    _snapshot_state['log'] = []

    if _socketio:
        _socketio.start_background_task(
            _transfer_snapshot_worker, snap_id, snap_dir, srv
        )
    return jsonify({'status': 'ok'})


def _ssh_resolve_remote_path(ssh, raw_path):
    """Resolve ~, $HOME, etc. in remote path and return absolute path."""
    try:
        # Let the remote shell expand ~ and $HOME
        _, so, _ = ssh.exec_command(f'eval echo {raw_path}')
        resolved = so.read().decode().strip()
        if resolved and resolved != '' and not resolved.startswith('$'):
            return resolved
    except Exception:
        pass
    # Fallback: resolve $HOME manually
    home = _ssh_resolve_home(ssh)
    if home:
        return raw_path.replace('~', home).replace('$HOME', home)
    return raw_path


def _transfer_snapshot_worker(snap_id, snap_dir, ssh_config):
    """Background: tar snapshot, SCP to remote, optionally extract."""
    try:
        _snap_update(percent=5, message='Packing snapshot...', log=f'Archiving {snap_id}...')

        archive = os.path.join(SNAPSHOTS_DIR, f'{snap_id}.tar.gz')
        if not os.path.isfile(archive):
            r = subprocess.run(
                ['tar', '-czf', archive, '-C', SNAPSHOTS_DIR, snap_id],
                capture_output=True, text=True, timeout=600
            )
            if r.returncode != 0:
                _snap_update(status='error', message=f'Archive error: {r.stderr[:200]}')
                return

        arc_size = os.path.getsize(archive)
        _snap_update(percent=15, message='Connecting to server...',
                     log=f'Archiwum: {arc_size / 1048576:.1f} MB')

        ssh = _get_ssh_client(ssh_config['host'], ssh_config.get('port', 22),
                              ssh_config['username'],
                              password=ssh_config.get('password'),
                              key_path=ssh_config.get('key_path'), timeout=30)
        _snap_update(percent=20, message='Connected, preparing...', log=f'SSH → {ssh_config["host"]}')

        # Resolve the remote path (handle ~, $HOME, relative paths)
        raw_remote_path = ssh_config.get('remote_path', '~/backups')
        remote_path = _ssh_resolve_remote_path(ssh, raw_remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'

        # Create remote directory and verify it's writable
        writable, _ = _ssh_ensure_writable_dir(ssh, remote_snap_dir)

        if not writable:
            _snap_update(log=f'No write permissions for {remote_snap_dir} — searching for alternative path...')
            # Try $HOME/backups/snapshots as fallback
            remote_home = _ssh_resolve_home(ssh)
            fallback_paths = []
            if remote_home:
                fallback_paths.append(f'{remote_home}/backups/snapshots')
            fallback_paths.append('/tmp/ethos-snapshots')

            found_writable = False
            for fb_path in fallback_paths:
                fb_ok, _ = _ssh_ensure_writable_dir(ssh, fb_path)
                if fb_ok:
                    remote_snap_dir = fb_path
                    _snap_update(log=f'Using path: {remote_snap_dir}')
                    found_writable = True
                    break

            if not found_writable:
                _snap_update(status='error',
                             message=f'No write permissions on remote server. Check path: {raw_remote_path}',
                             log=f'ERROR: Cannot write to {remote_snap_dir} or fallback paths')
                ssh.close()
                return

        _snap_update(percent=22, message='Uploading...', log=f'Target directory: {remote_snap_dir}')

        remote_file = f'{remote_snap_dir}/{snap_id}.tar.gz'

        def scp_progress(filename, size, sent):
            pct = int(22 + (sent / max(size, 1)) * 58)
            _snap_update(
                percent=pct,
                message=f'Uploading... {sent / 1048576:.0f}/{size / 1048576:.0f} MB'
            )

        with SCPClient(ssh.get_transport(), progress=scp_progress) as scp:
            scp.put(archive, remote_file)

        _snap_update(percent=85, message='Extracting on remote server...',
                     log='Transfer complete, extracting...')

        # Extract on remote
        cmd = f'cd {remote_snap_dir} && tar -xzf {snap_id}.tar.gz && rm -f {snap_id}.tar.gz'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=300)
        exit_code = stdout_.channel.recv_exit_status()
        if exit_code == 0:
            _snap_update(log='Extracted on remote server ✓')
        else:
            err_txt = stderr_.read().decode()[:200]
            _snap_update(log=f'Extraction: code {exit_code} — {err_txt}')

        ssh.close()

        _snap_update(status='done', percent=100,
                     message=f'Snapshot transferred to {ssh_config["host"]}:{remote_snap_dir}')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Transfer error: {e}',
                     log=f'EXCEPTION: {e}')
        logger.exception("Snapshot transfer failed")


@backup_bp.route('/snapshots/import', methods=['POST'])
def import_snapshot():
    """Import snapshot from uploaded tar.gz file."""
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file'}), 400

    if not f.filename.endswith('.tar.gz'):
        return jsonify({'error': 'Wymagany plik .tar.gz'}), 400

    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

    # Save uploaded file to temp location
    tmp_archive = os.path.join(SNAPSHOTS_DIR, f'_import_{secrets.token_hex(4)}.tar.gz')
    try:
        f.save(tmp_archive)
        _snap_update(status='creating', percent=30,
                     message='Extracting import...', log=['File received, extracting...'])

        # Extract
        r = subprocess.run(
            ['tar', '-xzf', tmp_archive, '-C', SNAPSHOTS_DIR],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            _snap_update(status='error', message=f'Extraction error: {r.stderr[:200]}')
            return jsonify({'error': f'Extraction error: {r.stderr[:200]}'}), 500

        # Find extracted snapshot (directory with meta.json)
        imported_name = None
        for entry in os.listdir(SNAPSHOTS_DIR):
            entry_path = os.path.join(SNAPSHOTS_DIR, entry)
            if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, 'meta.json')):
                # Check if this is new (not in our known list before import)
                meta_file = os.path.join(entry_path, 'meta.json')
                try:
                    with open(meta_file, 'r') as mf:
                        m = json.load(mf)
                    imported_name = entry
                except Exception:
                    pass

        if not imported_name:
            _snap_update(status='error', message='Snapshot not found in archive')
            return jsonify({'error': 'Snapshot not found in archive'}), 400

        _snap_update(status='done', percent=100,
                     message=f'Snapshot "{imported_name}" imported!')
        return jsonify({'ok': True, 'snapshot': imported_name})

    except Exception as e:
        _snap_update(status='error', message=f'Import error: {e}')
        return jsonify({'error': str(e)}), 500
    finally:
        # Cleanup temp archive
        if os.path.isfile(tmp_archive):
            os.remove(tmp_archive)


# ── Received snapshots (from other NAS devices) ──

def _find_received_snapshot_dirs():
    """
    Scan common directories where snapshots from other NAS devices
    might have been received via SSH transfer.
    Returns list of (base_dir, snap_name, meta) for directories
    that contain valid snapshots and are NOT in the local SNAPSHOTS_DIR.
    """
    local_real = os.path.realpath(SNAPSHOTS_DIR)
    scan_paths = set()

    # 1) User home + /backups/snapshots (default ~/backups target)
    home = os.path.expanduser('~')
    scan_paths.add(os.path.join(home, 'backups', 'snapshots'))

    # 2) /backups/snapshots (if someone used /backups as remote_path)
    scan_paths.add('/backups/snapshots')

    # 3) /tmp/ethos-snapshots (fallback path used by transfer code)
    scan_paths.add('/tmp/ethos-snapshots')

    # 4) Check all home directories for /backups/snapshots
    try:
        for entry in os.scandir('/home'):
            if entry.is_dir():
                p = os.path.join(entry.path, 'backups', 'snapshots')
                scan_paths.add(p)
    except Exception:
        pass

    # 5) Check EthOS data paths (common on EthOS installs)
    for base in ['/data', '/media']:
        try:
            for root, dirs, files in os.walk(base, topdown=True):
                # Look max 4 levels deep
                depth = root.replace(base, '').count(os.sep)
                if depth > 4:
                    dirs.clear()
                    continue
                if os.path.basename(root) == 'snapshots':
                    parent = os.path.dirname(root)
                    if os.path.basename(parent) == 'backups':
                        scan_paths.add(root)
                    dirs.clear()
        except Exception:
            pass

    results = []
    seen_snap_ids = set()

    for scan_dir in scan_paths:
        if not os.path.isdir(scan_dir):
            continue
        # Skip if this IS the local SNAPSHOTS_DIR
        if os.path.realpath(scan_dir) == local_real:
            continue

        try:
            for name in os.listdir(scan_dir):
                snap_path = os.path.join(scan_dir, name)
                if not os.path.isdir(snap_path):
                    continue
                meta_file = os.path.join(snap_path, 'meta.json')
                if not os.path.isfile(meta_file):
                    continue
                # Skip if same name already exists in local SNAPSHOTS_DIR
                if os.path.isdir(os.path.join(SNAPSHOTS_DIR, name)):
                    continue
                # Avoid duplicates across scan paths
                if name in seen_snap_ids:
                    continue
                seen_snap_ids.add(name)
                try:
                    with open(meta_file, 'r') as f:
                        meta = json.load(f)
                    # Calculate size
                    total_size = 0
                    for root, dirs, files in os.walk(snap_path):
                        for fn in files:
                            try:
                                total_size += os.path.getsize(os.path.join(root, fn))
                            except OSError:
                                pass
                    meta['size'] = total_size
                    meta['dir'] = name
                    meta['source_path'] = snap_path
                    meta['received'] = True
                    results.append(meta)
                except Exception:
                    pass
        except PermissionError:
            pass

    results.sort(key=lambda s: s.get('created', ''), reverse=True)
    return results


@backup_bp.route('/snapshots/received', methods=['GET'])
def list_received_snapshots():
    """List snapshots received from other NAS devices (not in local SNAPSHOTS_DIR)."""
    try:
        snaps = _find_received_snapshot_dirs()
        return jsonify({'snapshots': snaps})
    except Exception as e:
        logger.exception("Error scanning received snapshots")
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/received/adopt', methods=['POST'])
def adopt_received_snapshot():
    """Copy/move a received snapshot into local SNAPSHOTS_DIR so it appears in the normal list."""
    data = request.json or {}
    source_path = data.get('source_path')
    snap_dir_name = data.get('snap_id')
    if not source_path or not snap_dir_name:
        return jsonify({'error': 'source_path and snap_id required'}), 400

    # Validate the source path contains a valid snapshot
    meta_file = os.path.join(source_path, 'meta.json')
    if not os.path.isfile(meta_file):
        return jsonify({'error': 'Invalid snapshot path'}), 404

    local_dest = os.path.join(SNAPSHOTS_DIR, snap_dir_name)
    if os.path.exists(local_dest):
        return jsonify({'error': 'Snapshot with this name already exists locally'}), 409

    try:
        # Copy the snapshot directory into local SNAPSHOTS_DIR
        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        shutil.copytree(source_path, local_dest)
        return jsonify({'status': 'ok', 'snapshot': snap_dir_name})
    except Exception as e:
        logger.exception("Error adopting received snapshot")
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/received/restore', methods=['POST'])
def restore_received_snapshot():
    """Restore from a received snapshot (in-place, without copying to SNAPSHOTS_DIR first)."""
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    source_path = data.get('source_path')
    if not source_path:
        return jsonify({'error': 'source_path required'}), 400

    meta_file = os.path.join(source_path, 'meta.json')
    if not os.path.isfile(meta_file):
        return jsonify({'error': 'Invalid snapshot path'}), 404

    restore_ethos = data.get('restore_ethos', True)
    restore_system = data.get('restore_system', True)
    restore_docker = data.get('restore_docker', True)
    restore_volumes = data.get('restore_volumes', True)

    _snapshot_state['status'] = 'restoring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Restoring from received snapshot...'
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = time.time()

    if _socketio:
        _socketio.start_background_task(
            _restore_snapshot_worker, source_path,
            restore_docker, restore_volumes, restore_ethos, restore_system
        )
    return jsonify({'status': 'ok'})


@backup_bp.route('/snapshots/remote', methods=['POST'])
def list_remote_snapshots():
    """List snapshots on a remote NAS (via SSH)."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko not installed'}), 500

    data = request.json or {}
    server_id = data.get('server_id')
    if not server_id:
        return jsonify({'error': 'No server selected'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'SSH server not found'}), 404

    try:
        ssh = _get_ssh_client(srv['host'], srv.get('port', 22),
                              srv['username'],
                              password=srv.get('password'),
                              key_path=srv.get('key_path'), timeout=15)

        remote_path = srv.get('remote_path', '~/backups')
        # Resolve ~ and $HOME on the remote
        remote_path = _ssh_resolve_remote_path(ssh, remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'

        # Read meta.json from each snapshot directory
        cmd = f'for d in {remote_snap_dir}/snap_*/; do [ -f "$d/meta.json" ] && echo "---" && cat "$d/meta.json" && echo "|||$(basename $d)|||$(du -sb $d | cut -f1)"; done 2>/dev/null'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=30)
        stdout_.channel.recv_exit_status()
        output = stdout_.read().decode()
        ssh.close()

        snapshots = []
        for block in output.split('---'):
            block = block.strip()
            if not block:
                continue
            # Parse: JSON content then |||dirname|||size
            parts = block.rsplit('|||', 2)
            if len(parts) >= 3:
                json_str = parts[0].strip()
                snap_dirname = parts[1]
                snap_size = int(parts[2]) if parts[2].isdigit() else 0
                try:
                    meta = json.loads(json_str)
                    meta['dir'] = snap_dirname
                    meta['size'] = snap_size
                    meta['remote'] = True
                    snapshots.append(meta)
                except Exception:
                    pass

        snapshots.sort(key=lambda s: s.get('created', ''), reverse=True)
        return jsonify({'snapshots': snapshots, 'host': srv['host']})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/pull', methods=['POST'])
def pull_remote_snapshot():
    """Pull (download) a snapshot from remote NAS to local."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko/scp not installed'}), 500
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    server_id = data.get('server_id')
    remote_snap_id = data.get('snap_id')
    if not server_id or not remote_snap_id:
        return jsonify({'error': 'server_id and snap_id required'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'SSH server not found'}), 404

    _snapshot_state['status'] = 'transferring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Downloading from remote NAS...'
    _snapshot_state['log'] = []

    if _socketio:
        _socketio.start_background_task(
            _pull_snapshot_worker, remote_snap_id, srv
        )
    return jsonify({'status': 'ok'})


def _pull_snapshot_worker(snap_id, ssh_config):
    """Background: SCP snapshot from remote NAS to local."""
    tmp_archive = None
    try:
        _snap_update(percent=5, message='Connecting to server...', log=f'Connecting to {ssh_config["host"]}...')

        ssh = _get_ssh_client(ssh_config['host'], ssh_config.get('port', 22),
                              ssh_config['username'],
                              password=ssh_config.get('password'),
                              key_path=ssh_config.get('key_path'), timeout=30)

        remote_path = ssh_config.get('remote_path', '~/backups')
        # Resolve ~ and $HOME on the remote
        remote_path = _ssh_resolve_remote_path(ssh, remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'
        remote_archive = f'/tmp/_ethos_pull_{snap_id}.tar.gz'

        # Archive snapshot on remote
        _snap_update(percent=10, message='Packing on remote server...',
                     log='Archiving on remote...')
        cmd = f'tar -czf {remote_archive} -C {remote_snap_dir} {snap_id}'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=600)
        exit_code = stdout_.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr_.read().decode()[:200]
            _snap_update(status='error', message=f'Packing error: {err}')
            ssh.close()
            return

        _snap_update(percent=20, message='Downloading...',
                     log='Transfer from remote server...')

        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        tmp_archive = os.path.join(SNAPSHOTS_DIR, f'_pull_{snap_id}.tar.gz')

        def scp_progress(filename, size, sent):
            pct = int(20 + (sent / max(size, 1)) * 60)
            _snap_update(
                percent=pct,
                message=f'Downloading... {sent / 1048576:.0f}/{size / 1048576:.0f} MB'
            )

        with SCPClient(ssh.get_transport(), progress=scp_progress) as scp:
            scp.get(remote_archive, tmp_archive)

        # Clean up remote archive
        ssh.exec_command(f'rm -f {q(remote_archive)}')
        ssh.close()

        _snap_update(percent=85, message='Extracting...',
                     log='Extracting snapshot...')

        r = subprocess.run(
            ['tar', '-xzf', tmp_archive, '-C', SNAPSHOTS_DIR],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            _snap_update(status='error', message=f'Extraction error: {r.stderr[:200]}')
            return

        _snap_update(status='done', percent=100,
                     message=f'Snapshot {snap_id} downloaded from remote NAS!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Error: {e}',
                     log=f'EXCEPTION: {e}')
        logger.exception("Snapshot pull failed")
    finally:
        if tmp_archive and os.path.isfile(tmp_archive):
            os.remove(tmp_archive)


# ── NAS discovery on LAN ──

def _get_local_ip():
    """Best-effort: get this machine's LAN IP."""
    import socket as _sock
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _discover_via_avahi():
    """Use avahi-browse to find _ethos._tcp services on LAN."""
    devices = []
    try:
        r = subprocess.run(
            ['avahi-browse', '-ptr', '_ethos._tcp', 'local', '--resolve', '-l'],
            capture_output=True, text=True, timeout=8
        )
        # Parse avahi-browse output:
        # =;eth0;IPv4;NAS Name;_ethos._tcp;local;hostname.local;192.168.50.100;9000;"name=X" "version=Y"
        for line in r.stdout.splitlines():
            if not line.startswith('='):
                continue
            parts = line.split(';')
            if len(parts) < 9:
                continue
            name = parts[3]
            ip = parts[7]
            port = int(parts[8]) if parts[8].isdigit() else 9000
            # Parse TXT records
            txt = {}
            for tp in parts[9:]:
                tp = tp.strip().strip('"')
                if '=' in tp:
                    k, v = tp.split('=', 1)
                    txt[k] = v
            devices.append({
                'name': txt.get('name', name),
                'hostname': parts[6].rstrip('.'),
                'ip': ip,
                'port': port,
                'version': txt.get('version', '?'),
                'source': 'mdns',
            })
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass
    return devices


def _discover_via_scan(port=9000, timeout=2.0):
    """Scan local /24 subnet on given port for EthOS identify endpoint."""
    import socket as _sock
    from concurrent.futures import ThreadPoolExecutor, as_completed

    my_ip = _get_local_ip()
    if my_ip == '127.0.0.1':
        return []

    prefix = '.'.join(my_ip.split('.')[:3])
    devices = []

    def _probe(ip):
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(timeout)
            result = s.connect_ex((ip, port))
            s.close()
            if result != 0:
                return None
            # Port open — try identify endpoint
            import urllib.request
            url = f'http://{ip}:{port}/api/ethos/identify'
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read().decode())
            if data.get('ethos'):
                return {
                    'name': data.get('name', '?'),
                    'hostname': data.get('hostname', '?'),
                    'ip': ip,
                    'port': data.get('port', port),
                    'version': data.get('version', '?'),
                    'source': 'scan',
                }
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(_probe, f'{prefix}.{i}'): i for i in range(1, 255)}
        try:
            for fut in as_completed(futures, timeout=timeout + 5):
                try:
                    res = fut.result(timeout=0.5)
                    if res:
                        devices.append(res)
                except Exception:
                    pass
        except (TimeoutError, FuturesTimeoutError):
            # Some hosts didn't respond in time — that's OK
            pass

    return devices


def _discover_via_vms():
    """Discover EthOS instances running inside local QEMU VMs.

    Reads VM Manager state to find VMs with port forwards mapping to
    guest port 9000 (EthOS). Probes localhost:{host_port} to verify
    the instance is live.
    """
    import urllib.request
    vm_state_file = os.path.join(
        os.path.dirname(__file__), '..', '..', 'data', 'vm_state.json'
    )
    if not os.path.isfile(vm_state_file):
        return []

    try:
        with open(vm_state_file, 'r') as f:
            vms = json.load(f)
    except Exception:
        return []

    devices = []
    for vm_id, vm in vms.items():
        net = vm.get('network') or {}
        if net.get('net_type') not in ('user', None):
            continue
        for rule in net.get('port_forwards', []):
            if rule.get('guest') != 9000 or rule.get('proto', 'tcp') != 'tcp':
                continue
            host_port = rule.get('host', 0)
            if not host_port:
                continue
            try:
                url = f'http://127.0.0.1:{host_port}/api/ethos/identify'
                req = urllib.request.Request(url, headers={'Accept': 'application/json'})
                resp = urllib.request.urlopen(req, timeout=2)
                data = json.loads(resp.read().decode())
                if data.get('ethos'):
                    devices.append({
                        'name': data.get('name', vm.get('name', vm_id)),
                        'hostname': data.get('hostname', vm.get('name', vm_id)),
                        'ip': '127.0.0.1',
                        'port': host_port,
                        'version': data.get('version', '?'),
                        'source': 'vm',
                        'vm_id': vm_id,
                        'vm_name': vm.get('name', vm_id),
                    })
            except Exception:
                pass
    return devices


@backup_bp.route('/discover-nas', methods=['POST'])
def discover_nas():
    """Discover other EthOS instances on the local network."""
    my_ip = _get_local_ip()

    # Method 1: Avahi mDNS
    devices = _discover_via_avahi()

    # Method 2: Subnet scan fallback / supplement
    scanned = _discover_via_scan()

    # Method 3: Local QEMU VMs with port-forwarded EthOS
    vm_devices = _discover_via_vms()

    # Merge: deduplicate by IP:port
    known = {(d['ip'], d.get('port', 9000)) for d in devices}
    for sd in scanned:
        key = (sd['ip'], sd.get('port', 9000))
        if key not in known:
            devices.append(sd)
            known.add(key)
    for vd in vm_devices:
        key = (vd['ip'], vd.get('port', 9000))
        if key not in known:
            devices.append(vd)
            known.add(key)

    # Filter out self
    my_port = int(os.environ.get('ETHOS_PORT', 9000))
    devices = [d for d in devices
               if not (d['ip'] == my_ip and d.get('port', 9000) == my_port)]

    return jsonify({'devices': devices, 'my_ip': my_ip})


# ── Snapshot creation worker ──

def _create_snapshot_worker(label, include_docker, include_volumes,
                             include_ethos, include_system, dest_type, dest_path,
                             include_vms=False, include_models=False, include_userdirs=True):
    """Background task: creates a full system snapshot."""
    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        snap_name = f'snap_{ts}'
        snap_dir = os.path.join(SNAPSHOTS_DIR, snap_name)
        os.makedirs(snap_dir, exist_ok=True)

        meta = {
            'id': snap_name,
            'label': label or f'Snapshot {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            'created': datetime.now().isoformat(),
            'hostname': _read_file_safe('/etc/hostname', 'unknown').strip(),
            'includes': {
                'ethos': include_ethos,
                'docker': include_docker,
                'volumes': include_volumes,
                'system': include_system,
                'userdirs': include_userdirs,
                'vms': include_vms,
                'models': include_models,
            },
            'docker_projects': [],
            'docker_volumes': [],
            'docker_containers': 0,
        }

        steps_total = sum([include_ethos, include_system, include_userdirs, include_docker, include_volumes])
        step_i = 0

        # ── 1. EthOS config & data ──
        if include_ethos:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up EthOS...', log='Copying EthOS configuration...')

            ethos_root = os.environ.get('ETHOS_ROOT', f'/home/{get_ethos_user()}/docker/nasos')
            ethos_dir = os.path.join(snap_dir, 'ethos')
            os.makedirs(ethos_dir, exist_ok=True)

            # ethos.env
            env_file = os.path.join(ethos_root, 'ethos.env')
            if os.path.isfile(env_file):
                shutil.copy2(env_file, os.path.join(ethos_dir, 'ethos.env'))

            # data/ (settings, configs, profiles DB, etc.)
            # Optionally exclude large dirs based on user choices
            data_src = os.path.join(ethos_root, 'data')
            if os.path.isdir(data_src):
                exclude_dirs = ['novnc', 'updates']
                if not include_vms:
                    exclude_dirs.append('vms')
                if not include_models:
                    exclude_dirs.append('models')
                tar_cmd = ['tar', '-czf', os.path.join(ethos_dir, 'data.tar.gz'),
                           '-C', ethos_root]
                for ed in exclude_dirs:
                    tar_cmd.extend(['--exclude', f'data/{ed}'])
                tar_cmd.append('data')
                subprocess.run(tar_cmd, capture_output=True, timeout=3600)
                excluded_note = ', '.join(exclude_dirs) if exclude_dirs else 'none'
                _snap_update(log=f'EthOS data/ — excluding: {excluded_note}')

            # install.conf
            for extra in ['install.conf']:
                src = os.path.join(ethos_root, extra)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(ethos_dir, extra))

            _snap_update(log='EthOS — done')

        # ── 2. System configs ──
        if include_system:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up system configuration...', log='Copying system configuration...')

            sys_dir = os.path.join(snap_dir, 'system')
            os.makedirs(sys_dir, exist_ok=True)

            # Hostname, hosts, timezone, locale
            for f in ['/etc/hostname', '/etc/hosts', '/etc/timezone',
                       '/etc/default/locale', '/etc/resolv.conf']:
                if os.path.isfile(f):
                    dest = os.path.join(sys_dir, os.path.basename(f))
                    try:
                        shutil.copy2(f, dest)
                    except Exception:
                        pass

            # Timezone (symlink)
            try:
                tz = os.path.realpath('/etc/localtime')
                with open(os.path.join(sys_dir, 'timezone_link'), 'w') as tf:
                    tf.write(tz)
            except Exception:
                pass

            # NetworkManager connections
            nm_dir = '/etc/NetworkManager/system-connections'
            if os.path.isdir(nm_dir):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'nm-connections.tar.gz'),
                     '-C', '/etc/NetworkManager', 'system-connections'],
                    capture_output=True, timeout=30
                )
                _snap_update(log='NetworkManager connections saved')

            # Nginx sites
            nginx_avail = '/etc/nginx/sites-available'
            if os.path.isdir(nginx_avail):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'nginx-sites.tar.gz'),
                     '-C', '/etc/nginx', 'sites-available', 'sites-enabled'],
                    capture_output=True, timeout=30
                )
                _snap_update(log='Nginx sites saved')

            # SSL certs (Let's Encrypt)
            le_dir = '/etc/letsencrypt'
            if os.path.isdir(le_dir):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'letsencrypt.tar.gz'),
                     '-C', '/etc', 'letsencrypt'],
                    capture_output=True, timeout=60
                )
                _snap_update(log='Let\'s Encrypt certs saved')

            # Samba config
            smb_conf = '/etc/samba/smb.conf'
            if os.path.isfile(smb_conf):
                shutil.copy2(smb_conf, os.path.join(sys_dir, 'smb.conf'))
                _snap_update(log='Samba config saved')

            # fstab
            if os.path.isfile('/etc/fstab'):
                shutil.copy2('/etc/fstab', os.path.join(sys_dir, 'fstab'))

            # Crontab
            try:
                r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    with open(os.path.join(sys_dir, 'crontab'), 'w') as cf:
                        cf.write(r.stdout)
            except Exception:
                pass

            _snap_update(log='System config — done')

        # ── 2b. User home directories ──
        if include_userdirs:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up user directories...', log='Archiving /home...')

            users_dir = os.path.join(snap_dir, 'userdirs')
            os.makedirs(users_dir, exist_ok=True)

            try:
                home_entries = [d for d in os.listdir('/home')
                                if os.path.isdir(os.path.join('/home', d))
                                and not d.startswith('.')]
            except OSError:
                home_entries = []

            saved_users = []
            for uname in home_entries:
                user_home = os.path.join('/home', uname)
                _snap_update(log=f'  User: {uname}')
                tar_path = os.path.join(users_dir, f'{uname}.tar.gz')
                # Exclude caches and large temp dirs
                tar_cmd = [
                    'tar', '-czf', tar_path, '-C', '/home',
                    '--exclude', f'{uname}/.cache',
                    '--exclude', f'{uname}/.local/share/Trash',
                    '--exclude', f'{uname}/.npm',
                    '--exclude', f'{uname}/.venv',
                    '--exclude', f'{uname}/venv',
                    uname,
                ]
                try:
                    r = subprocess.run(tar_cmd, capture_output=True, timeout=3600)
                    if r.returncode == 0 or os.path.isfile(tar_path):
                        sz = os.path.getsize(tar_path) if os.path.isfile(tar_path) else 0
                        saved_users.append(uname)
                        _snap_update(log=f'  {uname}: {sz / 1048576:.1f} MB')
                except subprocess.TimeoutExpired:
                    _snap_update(log=f'  {uname}: TIMEOUT (skipped)')
                except Exception as e:
                    _snap_update(log=f'  {uname}: error — {e}')

            meta['user_dirs'] = saved_users
            _snap_update(log=f'User directories: {len(saved_users)} users archived')

        # ── 3. Docker compose projects ──
        if include_docker:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up Docker...', log='Saving Docker projects...')

            docker_dir = os.path.join(snap_dir, 'docker')
            os.makedirs(docker_dir, exist_ok=True)

            if _docker_ok():
                # Compose projects — save compose files + .env
                compose_root = os.environ.get('COMPOSE_ROOT', f'/home/{get_ethos_user()}/docker')
                projects = []
                if os.path.isdir(compose_root):
                    for entry in sorted(os.listdir(compose_root)):
                        proj_path = os.path.join(compose_root, entry)
                        if not os.path.isdir(proj_path):
                            continue
                        compose_file = None
                        for cf_name in ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml']:
                            candidate = os.path.join(proj_path, cf_name)
                            if os.path.isfile(candidate):
                                compose_file = candidate
                                break
                        if not compose_file:
                            continue

                        proj_bak = os.path.join(docker_dir, 'projects', entry)
                        os.makedirs(proj_bak, exist_ok=True)
                        shutil.copy2(compose_file, os.path.join(proj_bak, os.path.basename(compose_file)))
                        # .env file
                        env_f = os.path.join(proj_path, '.env')
                        if os.path.isfile(env_f):
                            shutil.copy2(env_f, os.path.join(proj_bak, '.env'))
                        # Additional env files referenced in compose
                        for extra_env in _find_env_files(compose_file):
                            extra_path = os.path.join(proj_path, extra_env)
                            if os.path.isfile(extra_path):
                                shutil.copy2(extra_path, os.path.join(proj_bak, extra_env))

                        projects.append(entry)
                        _snap_update(log=f'  Project: {entry}')

                meta['docker_projects'] = projects

                # Container list (for reference — what was running)
                out, rc = _docker_cmd(['ps', '-a', '--format', '{{json .}}', '--no-trunc'])
                if rc == 0:
                    containers = []
                    for line in out.split('\n'):
                        line = line.strip()
                        if line:
                            try:
                                containers.append(json.loads(line))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'containers.json'), 'w') as cf:
                        json.dump(containers, cf, indent=2)
                    meta['docker_containers'] = len(containers)
                    _snap_update(log=f'Containers: {len(containers)}')

                # Docker images list
                out, rc = _docker_cmd(['images', '--format', '{{json .}}'])
                if rc == 0:
                    images = []
                    for line in out.split('\n'):
                        line = line.strip()
                        if line:
                            try:
                                images.append(json.loads(line))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'images.json'), 'w') as cf:
                        json.dump(images, cf, indent=2)
                    _snap_update(log=f'Docker images: {len(images)}')

                # Networks
                out, rc = _docker_cmd(['network', 'ls', '--format', '{{json .}}'])
                if rc == 0:
                    networks = []
                    for line in out.split('\n'):
                        if line.strip():
                            try:
                                networks.append(json.loads(line.strip()))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'networks.json'), 'w') as cf:
                        json.dump(networks, cf, indent=2)

            else:
                _snap_update(log='Docker unavailable — skipping')

            _snap_update(log='Docker projects — done')

        # ── 4. Docker volumes ──
        if include_volumes and include_docker:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up Docker volumes...', log='Exporting Docker volumes...')

            vol_dir = os.path.join(snap_dir, 'docker', 'volumes')
            os.makedirs(vol_dir, exist_ok=True)

            if _docker_ok():
                out, rc = _docker_cmd(['volume', 'ls', '-q'])
                vol_names = [v.strip() for v in out.split('\n') if v.strip()] if rc == 0 else []
                saved_vols = []

                for vi, vol in enumerate(vol_names):
                    # Skip very large or internal volumes
                    inspect_out, irc = _docker_cmd(['volume', 'inspect', vol])
                    if irc != 0:
                        continue
                    try:
                        vinfo = json.loads(inspect_out)
                        if isinstance(vinfo, list) and vinfo:
                            vinfo = vinfo[0]
                        mount = vinfo.get('Mountpoint', '')
                    except Exception:
                        mount = ''

                    # Check size — skip if > 5GB
                    vol_size = 0
                    if mount and os.path.isdir(mount):
                        try:
                            r = subprocess.run(
                                ['du', '-sb', mount],
                                capture_output=True, text=True, timeout=10
                            )
                            if r.returncode == 0:
                                vol_size = int(r.stdout.split()[0])
                        except Exception:
                            pass

                    size_mb = vol_size / (1024 * 1024)
                    if vol_size > 5 * 1024 * 1024 * 1024:  # >5GB
                        _snap_update(log=f'  Skipping {vol} ({size_mb:.0f} MB — too large)')
                        continue

                    vol_pct = int(pct + (vi / max(len(vol_names), 1)) * (90 - pct))
                    _snap_update(
                        percent=vol_pct,
                        message=f'Eksport: {vol} ({size_mb:.1f} MB)',
                        log=f'  Volume: {vol} ({size_mb:.1f} MB)'
                    )

                    archive = os.path.join(vol_dir, f'{vol}.tar.gz')
                    # Export volume via docker run
                    result = subprocess.run(
                        ['docker', 'run', '--rm',
                         '-v', f'{vol}:/volume_data:ro',
                         '-v', f'{vol_dir}:/backup',
                         'alpine',
                         'tar', '-czf', f'/backup/{vol}.tar.gz', '-C', '/volume_data', '.'],
                        capture_output=True, text=True, timeout=600
                    )
                    if result.returncode == 0 and os.path.isfile(archive):
                        saved_vols.append(vol)
                    else:
                        _snap_update(log=f'  Export ERROR {vol}: {result.stderr[:200]}')

                meta['docker_volumes'] = saved_vols
                _snap_update(log=f'Volumes: {len(saved_vols)}/{len(vol_names)} exported')
            else:
                _snap_update(log='Docker unavailable — skipping volumes')

        # ── Save metadata ──
        _snap_update(percent=92, message='Saving metadata...')
        with open(os.path.join(snap_dir, 'meta.json'), 'w') as mf:
            json.dump(meta, mf, indent=2)

        # ── Copy to USB if requested ──
        if dest_type == 'usb' and dest_path:
            _snap_update(percent=93, message='Copying to USB...', log=f'USB transfer: {dest_path}')
            usb_snap_dir = os.path.join(dest_path, 'ethos-snapshots')
            os.makedirs(usb_snap_dir, exist_ok=True)
            archive_name = f'{snap_name}.tar.gz'
            archive_path = os.path.join(usb_snap_dir, archive_name)
            r = subprocess.run(
                ['tar', '-czf', archive_path, '-C', SNAPSHOTS_DIR, snap_name],
                capture_output=True, timeout=600
            )
            if r.returncode == 0:
                sz = os.path.getsize(archive_path) if os.path.isfile(archive_path) else 0
                _snap_update(log=f'USB: saved {archive_name} ({sz / 1048576:.1f} MB)')
            else:
                _snap_update(log=f'USB: write error — {r.stderr[:200]}')

        _snap_update(status='done', percent=100, message=f'Snapshot "{meta["label"]}" ready!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Error: {e}', log=f'EXCEPTION: {e}')
        logger.exception("Snapshot creation failed")


# ── Snapshot restore worker ──

def _restore_snapshot_worker(snap_dir, restore_docker, restore_volumes,
                              restore_ethos, restore_system):
    """Background task: restores system from a snapshot."""
    try:
        meta_file = os.path.join(snap_dir, 'meta.json')
        if not os.path.isfile(meta_file):
            _snap_update(status='error', message='No meta.json in snapshot')
            return

        with open(meta_file) as f:
            meta = json.load(f)

        _snap_update(percent=5, message='Restore started...', log=f'Restoring: {meta.get("label", "?")}')

        steps_total = sum([restore_ethos, restore_system, restore_docker, restore_volumes])
        step_i = 0

        # ── 1. EthOS config & data ──
        if restore_ethos:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring EthOS...', log='Restoring EthOS configuration...')

            ethos_root = os.environ.get('ETHOS_ROOT', f'/home/{get_ethos_user()}/docker/nasos')
            ethos_bak = os.path.join(snap_dir, 'ethos')

            if os.path.isdir(ethos_bak):
                # ethos.env
                env_src = os.path.join(ethos_bak, 'ethos.env')
                if os.path.isfile(env_src):
                    shutil.copy2(env_src, os.path.join(ethos_root, 'ethos.env'))
                    _snap_update(log='  ethos.env restored')

                # data/
                data_tar = os.path.join(ethos_bak, 'data.tar.gz')
                if os.path.isfile(data_tar):
                    # Backup current data before overwriting
                    data_current = os.path.join(ethos_root, 'data')
                    if os.path.isdir(data_current):
                        pre_restore = os.path.join(ethos_root, 'data.pre-restore')
                        if os.path.isdir(pre_restore):
                            shutil.rmtree(pre_restore, ignore_errors=True)
                        shutil.copytree(data_current, pre_restore)
                        _snap_update(log='  Current data/ copied to data.pre-restore/')
                    subprocess.run(
                        ['tar', '-xzf', data_tar, '-C', ethos_root],
                        capture_output=True, timeout=600
                    )
                    _snap_update(log='  data/ restored')

                # install.conf
                for extra in ['install.conf']:
                    src = os.path.join(ethos_bak, extra)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(ethos_root, extra))

                _snap_update(log='EthOS — restored')
            else:
                _snap_update(log='No EthOS data in snapshot')

        # ── 2. System configs ──
        if restore_system:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring system configuration...', log='Restoring system...')

            sys_bak = os.path.join(snap_dir, 'system')
            if os.path.isdir(sys_bak):
                # Hostname
                hostname_f = os.path.join(sys_bak, 'hostname')
                if os.path.isfile(hostname_f):
                    shutil.copy2(hostname_f, '/etc/hostname')
                    hostname = _read_file_safe(hostname_f, '').strip()
                    if hostname:
                        subprocess.run(['hostnamectl', 'set-hostname', hostname],
                                       capture_output=True, timeout=10)
                    _snap_update(log=f'  Hostname: {hostname}')

                # hosts
                hosts_f = os.path.join(sys_bak, 'hosts')
                if os.path.isfile(hosts_f):
                    shutil.copy2(hosts_f, '/etc/hosts')

                # Timezone
                tz_link = os.path.join(sys_bak, 'timezone_link')
                if os.path.isfile(tz_link):
                    tz_path = _read_file_safe(tz_link, '').strip()
                    if tz_path and os.path.isfile(tz_path):
                        try:
                            os.remove('/etc/localtime')
                        except Exception:
                            pass
                        os.symlink(tz_path, '/etc/localtime')
                        _snap_update(log=f'  Timezone: {tz_path}')

                # NetworkManager connections
                nm_tar = os.path.join(sys_bak, 'nm-connections.tar.gz')
                if os.path.isfile(nm_tar):
                    subprocess.run(
                        ['tar', '-xzf', nm_tar, '-C', '/etc/NetworkManager'],
                        capture_output=True, timeout=30
                    )
                    subprocess.run(['nmcli', 'connection', 'reload'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  NetworkManager connections restored')

                # Nginx sites
                nginx_tar = os.path.join(sys_bak, 'nginx-sites.tar.gz')
                if os.path.isfile(nginx_tar):
                    subprocess.run(
                        ['tar', '-xzf', nginx_tar, '-C', '/etc/nginx'],
                        capture_output=True, timeout=30
                    )
                    subprocess.run(['systemctl', 'reload', 'nginx'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Nginx sites restored')

                # Let's Encrypt
                le_tar = os.path.join(sys_bak, 'letsencrypt.tar.gz')
                if os.path.isfile(le_tar):
                    subprocess.run(
                        ['tar', '-xzf', le_tar, '-C', '/etc'],
                        capture_output=True, timeout=60
                    )
                    _snap_update(log='  Let\'s Encrypt certs restored')

                # Samba
                smb_f = os.path.join(sys_bak, 'smb.conf')
                if os.path.isfile(smb_f):
                    shutil.copy2(smb_f, '/etc/samba/smb.conf')
                    subprocess.run(['systemctl', 'restart', 'smbd'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Samba config restored')

                # fstab (copy but don't apply — user must reboot)
                fstab_f = os.path.join(sys_bak, 'fstab')
                if os.path.isfile(fstab_f):
                    shutil.copy2(fstab_f, '/etc/fstab')
                    _snap_update(log='  fstab restored (reboot required)')

                # Crontab
                cron_f = os.path.join(sys_bak, 'crontab')
                if os.path.isfile(cron_f):
                    subprocess.run(['crontab', cron_f],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Crontab restored')

                _snap_update(log='System config — restored')
            else:
                _snap_update(log='No system configuration in snapshot')

        # ── 2b. User home directories ──
        users_bak = os.path.join(snap_dir, 'userdirs')
        if os.path.isdir(users_bak):
            step_i += 1
            pct = int(step_i / steps_total * 85) if steps_total else 50
            _snap_update(percent=pct, message='Restoring user directories...', log='Restoring /home...')

            for tarfile_name in sorted(os.listdir(users_bak)):
                if not tarfile_name.endswith('.tar.gz'):
                    continue
                uname = tarfile_name.replace('.tar.gz', '')
                _snap_update(log=f'  Restoring user: {uname}')
                try:
                    subprocess.run(
                        ['tar', '-xzf', os.path.join(users_bak, tarfile_name), '-C', '/home'],
                        capture_output=True, timeout=3600
                    )
                    _snap_update(log=f'  {uname} — restored')
                except Exception as e:
                    _snap_update(log=f'  {uname} — error: {e}')

            _snap_update(log='User directories — restored')

        # ── 3. Docker compose projects ──
        if restore_docker:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring Docker...', log='Restoring Docker projects...')

            docker_bak = os.path.join(snap_dir, 'docker')
            proj_bak = os.path.join(docker_bak, 'projects')

            if os.path.isdir(proj_bak) and _docker_ok():
                compose_root = os.environ.get('COMPOSE_ROOT', f'/home/{get_ethos_user()}/docker')
                restored = 0

                for proj_name in sorted(os.listdir(proj_bak)):
                    proj_src = os.path.join(proj_bak, proj_name)
                    if not os.path.isdir(proj_src):
                        continue

                    proj_dest = os.path.join(compose_root, proj_name)
                    os.makedirs(proj_dest, exist_ok=True)

                    # Copy compose file + env files
                    for fn in os.listdir(proj_src):
                        src = os.path.join(proj_src, fn)
                        dst = os.path.join(proj_dest, fn)
                        if os.path.isfile(src):
                            shutil.copy2(src, dst)

                    # Pull images + start project
                    _snap_update(log=f'  Project {proj_name}: pulling images...')
                    subprocess.run(
                        ['docker', 'compose', 'pull'],
                        capture_output=True, timeout=300, cwd=proj_dest
                    )
                    r = subprocess.run(
                        ['docker', 'compose', 'up', '-d'],
                        capture_output=True, text=True, timeout=120, cwd=proj_dest
                    )
                    if r.returncode == 0:
                        _snap_update(log=f'  Project {proj_name}: started ✓')
                        restored += 1
                    else:
                        _snap_update(log=f'  Project {proj_name}: ERROR — {r.stderr[:200]}')

                _snap_update(log=f'Docker projects: {restored} restored')
            else:
                _snap_update(log='No Docker projects in snapshot or Docker unavailable')

        # ── 4. Docker volumes ──
        if restore_volumes and restore_docker:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring Docker volumes...', log='Importing Docker volumes...')

            vol_bak = os.path.join(snap_dir, 'docker', 'volumes')
            if os.path.isdir(vol_bak) and _docker_ok():
                restored_vols = 0
                vol_archives = [f for f in os.listdir(vol_bak) if f.endswith('.tar.gz')]

                for vi, vf in enumerate(vol_archives):
                    vol_name = vf.replace('.tar.gz', '')
                    vol_pct = int(pct + (vi / max(len(vol_archives), 1)) * (85 - pct))
                    _snap_update(
                        percent=vol_pct,
                        message=f'Import: {vol_name}',
                        log=f'  Volume: {vol_name}'
                    )

                    # Create volume if needed
                    _docker_cmd(['volume', 'create', vol_name])

                    # Stop containers using this volume before restoring
                    containers_using = _containers_using_volume(vol_name)
                    stopped = []
                    for cid in containers_using:
                        _docker_cmd(['stop', cid], timeout=30)
                        stopped.append(cid)
                        _snap_update(log=f'    Stopped container {cid[:12]}')

                    # Import volume data
                    archive_path = os.path.join(vol_bak, vf)
                    r = subprocess.run(
                        ['docker', 'run', '--rm',
                         '-v', f'{vol_name}:/volume_data',
                         '-v', f'{vol_bak}:/backup:ro',
                         'alpine', 'sh', '-c',
                         f'rm -rf /volume_data/* && tar -xzf /backup/{vf} -C /volume_data'],
                        capture_output=True, text=True, timeout=600
                    )
                    if r.returncode == 0:
                        restored_vols += 1
                    else:
                        _snap_update(log=f'    Import ERROR {vol_name}: {r.stderr[:200]}')

                    # Restart stopped containers
                    for cid in stopped:
                        _docker_cmd(['start', cid], timeout=30)
                        _snap_update(log=f'    Restarted {cid[:12]}')

                _snap_update(log=f'Volumes: {restored_vols}/{len(vol_archives)} restored')
            else:
                _snap_update(log='No volumes in snapshot or Docker unavailable')

        _snap_update(status='done', percent=100,
                     message=f'Restoring "{meta.get("label", "")}" completed!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Error: {e}', log=f'EXCEPTION: {e}')
        logger.exception("Snapshot restore failed")


# ── Helpers for snapshots ──

def _read_file_safe(path, default=''):
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return default


def _dir_size_str(path):
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for fn in files:
                total += os.path.getsize(os.path.join(root, fn))
    except Exception:
        pass
    if total > 1073741824:
        return f'{total / 1073741824:.1f} GB'
    if total > 1048576:
        return f'{total / 1048576:.1f} MB'
    return f'{total / 1024:.0f} KB'


def _find_env_files(compose_file):
    """Find extra .env files referenced in a compose file."""
    envs = []
    try:
        with open(compose_file, 'r') as f:
            content = f.read()
        for m in re.finditer(r'env_file:\s*\n\s*-\s*(.+)', content):
            fn = m.group(1).strip().strip('"').strip("'")
            if fn and fn != '.env':
                envs.append(fn)
        for m in re.finditer(r'env_file:\s+(.+)', content):
            fn = m.group(1).strip().strip('"').strip("'")
            if fn and fn != '.env' and not fn.startswith('-'):
                envs.append(fn)
    except Exception:
        pass
    return envs


def _containers_using_volume(vol_name):
    """Return list of container IDs using a given volume."""
    out, rc = _docker_cmd(['ps', '-a', '-q', '--filter', f'volume={vol_name}'])
    if rc == 0 and out:
        return [c.strip() for c in out.split('\n') if c.strip()]
    return []
