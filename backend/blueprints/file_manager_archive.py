"""EthOS — File Manager: compress, extract, remote file transfer."""
import os
import sys
import sys as _sys
import json
import time
import re
import shutil
import zipfile
import tarfile
import threading as _threading
import signal
import pty
import select
import fcntl
import termios
import gevent
import pwd
from flask import request, jsonify, g
from i18n import t
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from host import (
    data_path as _data_path,
    ETHOS_ROOT,
    host_run as _host_run,
    q,
    get_user_home as _get_user_home,
    user_data_path as _user_data_path,
    fs_call_with_timeout as _fs_call,
)
from utils import (
    load_json as _load_json,
    save_json as _save_json,
    DATA_ROOT,
    ALLOWED_ROOTS as _ALLOWED_ROOTS,
    fmt_bytes,
)
from blueprints.eventlog import log as elog
from blueprints.admin_required import admin_required
from blueprints.auth import require_auth, get_current_user


def _get_transfer_resume_file():
    """Lazy import to avoid circular dependency."""
    from blueprints.file_manager_ops import TRANSFER_RESUME_FILE
    return TRANSFER_RESUME_FILE


def _main():
    """Return the main file_manager module."""
    return _sys.modules['blueprints.file_manager']


files_bp = _sys.modules['blueprints.file_manager'].files_bp

class _SocketioProxy:
    """Lazy proxy for socketio - avoids import-lock issues under gevent."""
    def __getattr__(self, name):
        sio = getattr(_sys.modules.get('app'), 'socketio', None)
        if sio is None:
            # Return a no-op function instead of raising an error
            def noop(*args, **kwargs):
                pass
            return noop
        return getattr(sio, name)
socketio = _SocketioProxy()

# ── Archive / Extract ──

@files_bp.route('/api/files/compress', methods=['POST'])
@require_auth
def files_compress():
    """Create a zip or tar.gz from selected files/folders."""
    data = request.json or {}
    sources = data.get('sources', [])
    fmt = data.get('format', 'zip')  # 'zip' or 'tar.gz'
    archive_name = data.get('name', '')
    if not sources:
        return jsonify({'error': 'No files to compress'}), 400

    # Determine output dir = same dir as first source
    first_src = _main().safe_path(sources[0])
    if not first_src:
        return jsonify({'error': 'Invalid path'}), 400
    out_dir = os.path.dirname(first_src)

    if not archive_name:
        if len(sources) == 1:
            archive_name = os.path.splitext(os.path.basename(first_src))[0]
        else:
            archive_name = 'archive'

    ext = '.zip' if fmt == 'zip' else '.tar.gz'
    archive_path = os.path.join(out_dir, archive_name + ext)
    # Avoid overwrite
    counter = 1
    while os.path.exists(archive_path):
        archive_path = os.path.join(out_dir, f"{archive_name}_{counter}{ext}")
        counter += 1

    resolved = []
    for s in sources:
        rs = _main().safe_path(s)
        if rs and os.path.exists(rs):
            resolved.append(rs)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    total = _main()._count_items(resolved)

    with _main()._fileop_lock:
        if _main()._fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _main()._fileop_state['active'] = True
        _main()._fileop_state['operation'] = 'compress'
        _main()._fileop_state['progress'] = None
        _main()._fileop_state['cancel'] = False
        _main()._fileop_state['paused'] = False

    cur_user = get_current_user()
    _bg_username = cur_user['username'] if cur_user else None
    _main()._save_compress_task(resolved, archive_path, fmt, total, _bg_username)
    socketio.start_background_task(_bg_compress, resolved, archive_path, fmt, total, cur_user)
    return jsonify({'async': True, 'message': f'Compressing {len(resolved)} items to {os.path.basename(archive_path)}'})


def _bg_compress(resolved, archive_path, fmt, total, cur_user=None):
    """Background archive creation with progress. Writes to a temp file first,
    then atomically renames to the final path on success (prevents corrupt archives)."""
    _bg_username = cur_user['username'] if cur_user else None
    done = 0
    cancelled = False
    tmp_archive_path = archive_path + '.ethos_archive_tmp'

    def _check_cancel_pause():
        if _main()._fileop_cancelled():
            return True
        while _main()._fileop_is_paused():
            gevent.sleep(0.5)
            if _main()._fileop_cancelled():
                return True
        return False

    try:
        if fmt == 'zip':
            with zipfile.ZipFile(tmp_archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for src in resolved:
                    if _check_cancel_pause():
                        cancelled = True; break
                    base_dir = os.path.dirname(src)
                    if os.path.isdir(src):
                        for root, dirs, files in os.walk(src):
                            if _check_cancel_pause():
                                cancelled = True; break
                            for fn in files:
                                if _check_cancel_pause():
                                    cancelled = True; break
                                full = os.path.join(root, fn)
                                arcname = os.path.relpath(full, base_dir)
                                zf.write(full, arcname)
                                done += 1
                                _main()._fileop_progress('compress', fn, done, total)
                                gevent.sleep(0)
                            if cancelled:
                                break
                            done += len(dirs)
                            gevent.sleep(0)
                    else:
                        zf.write(src, os.path.basename(src))
                        done += 1
                        _main()._fileop_progress('compress', os.path.basename(src), done, total)
                        gevent.sleep(0)
        else:
            with tarfile.open(tmp_archive_path, 'w:gz') as tf:
                for src in resolved:
                    if _check_cancel_pause():
                        cancelled = True; break
                    base_dir = os.path.dirname(src)
                    if os.path.isdir(src):
                        for root, dirs, files in os.walk(src):
                            if _check_cancel_pause():
                                cancelled = True; break
                            for fn in files:
                                if _check_cancel_pause():
                                    cancelled = True; break
                                full = os.path.join(root, fn)
                                arcname = os.path.relpath(full, base_dir)
                                tf.add(full, arcname)
                                done += 1
                                _main()._fileop_progress('compress', fn, done, total)
                                gevent.sleep(0)
                            if cancelled:
                                break
                            done += len(dirs)
                            gevent.sleep(0)
                    else:
                        tf.add(src, os.path.basename(src))
                        done += 1
                        _main()._fileop_progress('compress', os.path.basename(src), done, total)
                        gevent.sleep(0)

        if cancelled:
            for p in (tmp_archive_path,):
                if os.path.exists(p):
                    try: os.remove(p)
                    except OSError: pass
            _main()._clear_compress_task()
            _main()._fileop_finish('compress', False, 'Cancelled')
            return

        # Atomically move temp archive to final path
        os.replace(tmp_archive_path, archive_path)
        _main()._chown_to_user(archive_path, _bg_username)
        size_mb = round(os.path.getsize(archive_path) / (1024*1024), 1)
        _main()._clear_compress_task()
        _main()._fileop_finish('compress', True, f'{os.path.basename(archive_path)} ({size_mb} MB)')
    except Exception as e:
        for p in (tmp_archive_path, archive_path):
            if os.path.exists(p):
                try: os.remove(p)
                except OSError: pass
        _main()._clear_compress_task()
        _main()._fileop_finish('compress', False, str(e))


_EXTRACT_COMPOUND_EXTS = ('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')
_EXTRACT_SIMPLE_EXTS = ('.tar', '.zip')
_EXTRACT_7Z_EXTS = ('.gz', '.bz2', '.xz', '.rar', '.7z', '.cab', '.iso')
_EXTRACT_ALL_EXTS = _EXTRACT_COMPOUND_EXTS + _EXTRACT_SIMPLE_EXTS + _EXTRACT_7Z_EXTS


def _extract_folder_name(basename):
    """Derive output folder name from archive filename."""
    low = basename.lower()
    for ext in _EXTRACT_COMPOUND_EXTS:
        if low.endswith(ext):
            return basename[:len(basename) - len(ext)]
    for ext in _EXTRACT_SIMPLE_EXTS + _EXTRACT_7Z_EXTS:
        if low.endswith(ext):
            return basename[:len(basename) - len(ext)]
    return basename + '_extracted'


def _needs_7z(basename):
    """Return True if this archive format requires 7z CLI rather than Python stdlib."""
    low = basename.lower()
    return any(low.endswith(ext) for ext in _EXTRACT_7Z_EXTS)


@files_bp.route('/api/files/extract', methods=['POST'])
@require_auth
def files_extract():
    """Extract an archive (zip, tar.*, gz, rar, 7z, bz2, xz, cab, iso)."""
    data = request.json or {}
    archive = _main().safe_path(data.get('path', ''))
    if not archive or not os.path.isfile(archive):
        return jsonify({'error': 'Archive file does not exist'}), 400

    basename = os.path.basename(archive)
    low = basename.lower()

    if not any(low.endswith(ext) for ext in _EXTRACT_ALL_EXTS):
        return jsonify({'error': 'Unsupported archive format'}), 400

    folder_name = _extract_folder_name(basename)
    extract_to = os.path.join(os.path.dirname(archive), folder_name)
    counter = 1
    base_extract = extract_to
    while os.path.exists(extract_to):
        extract_to = f"{base_extract}_{counter}"
        counter += 1

    # For 7z-only formats, verify or install 7z first (before going async)
    use_7z = _needs_7z(basename)
    if use_7z:
        from host import ensure_dep
        ok, msg = ensure_dep('7z', install=True)
        if not ok:
            return jsonify({'error': f'Cannot extract: {msg}'}), 400

    os.makedirs(extract_to, exist_ok=True)

    # Count members (only for Python-handled formats)
    total = 0
    if not use_7z:
        try:
            if low.endswith('.zip'):
                with zipfile.ZipFile(archive, 'r') as zf:
                    total = len(zf.namelist())
            elif low.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar')):
                with tarfile.open(archive, 'r:*') as tf:
                    total = len(tf.getnames())
        except Exception:
            total = 0

    with _main()._fileop_lock:
        _fm = _main()._fileop_channels['fm']
        if _fm['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _fm['active'] = True
        _fm['operation'] = 'extract'
        _fm['progress'] = None
        _fm['cancel'] = False
        _fm['paused'] = False

    socketio.start_background_task(_bg_extract, archive, extract_to, total, use_7z, get_current_user())
    return jsonify({'async': True, 'message': f'Extracting {basename} to {folder_name}/'})


def _bg_extract(archive, extract_to, total, use_7z, cur_user=None):
    """Background archive extraction with progress and cancel support."""
    _bg_username = cur_user['username'] if cur_user else None
    done = 0
    basename = os.path.basename(archive)
    _fm = _main()._fileop_channels['fm']

    def _check_cancel():
        with _main()._fileop_lock:
            return _fm.get('cancel', False)

    try:
        if use_7z:
            # Use 7z CLI for formats Python can't handle (.gz, .rar, .7z, .bz2, .xz, .cab, .iso)
            import subprocess as _sp
            cmd = ['7z', 'x', '-y', f'-o{extract_to}', archive]
            _main()._fileop_progress('extract', basename, 0, 1)
            proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, bufsize=1)
            for line in iter(proc.stdout.readline, ''):
                if _check_cancel():
                    proc.kill()
                    try: shutil.rmtree(extract_to, ignore_errors=True)
                    except Exception: pass
                    _main()._fileop_finish('extract', False, 'Cancelled')
                    return
                line = line.strip()
                if line.startswith('- ') or line.startswith('Extracting '):
                    fname = line.split(' ', 1)[-1] if ' ' in line else line
                    done += 1
                    _main()._fileop_progress('extract', fname[:80], done, max(done, 1))
                    gevent.sleep(0)
            proc.wait()
            if proc.returncode != 0:
                _main()._fileop_finish('extract', False, f'7z exited with code {proc.returncode}')
                return
        elif basename.lower().endswith('.zip'):
            with zipfile.ZipFile(archive, 'r') as zf:
                for member in zf.namelist():
                    if _check_cancel():
                        try: shutil.rmtree(extract_to, ignore_errors=True)
                        except Exception: pass
                        _main()._fileop_finish('extract', False, 'Cancelled')
                        return
                    zf.extract(member, extract_to)
                    done += 1
                    if done % 20 == 0 or done == total:
                        _main()._fileop_progress('extract', member.split('/')[-1] or member, done, total)
                        gevent.sleep(0)
        else:
            # tar variants
            with tarfile.open(archive, 'r:*') as tf:
                for member in tf:
                    if _check_cancel():
                        try: shutil.rmtree(extract_to, ignore_errors=True)
                        except Exception: pass
                        _main()._fileop_finish('extract', False, 'Cancelled')
                        return
                    tf.extract(member, extract_to, filter='data')
                    done += 1
                    if done % 20 == 0 or done == total:
                        name = member.name.split('/')[-1] or member.name
                        _main()._fileop_progress('extract', name, done, total)
                        gevent.sleep(0)

        _chown_recursive(extract_to, _bg_username)
        _main()._fileop_finish('extract', True, f'Extracted to {os.path.basename(extract_to)}/ ({done} files)')
    except Exception as e:
        _main()._fileop_finish('extract', False, str(e))


# ── Transfer to remote NAS ──

@files_bp.route('/api/files/remote-servers', methods=['GET'])
@require_auth
def files_remote_servers():
    """Return available SSH/NAS servers (from backup config) for file transfer."""
    try:
        from blueprints.backup import load_ssh_configs
        configs = load_ssh_configs()
        safe = []
        for c in configs:
            safe.append({
                'id': c.get('id'), 'name': c.get('name', ''),
                'host': c.get('host', ''), 'port': c.get('port', 22),
                'username': c.get('username', ''),
                'remote_path': c.get('remote_path', '~/'),
                'has_password': bool(c.get('password')),
                'has_key': bool(c.get('key_path'))
            })
        return jsonify({'servers': safe})
    except Exception as e:
        return jsonify({'servers': [], 'error': str(e)})


@files_bp.route('/api/files/transfer-remote', methods=['POST'])
@require_auth
def files_transfer_remote():
    """Transfer files/folders to a remote NAS via SCP with progress."""
    data = request.json or {}
    server_id = data.get('server_id')
    paths = data.get('paths', [])
    remote_dest = data.get('remote_path', '')

    if not server_id:
        return jsonify({'error': 'No server selected'}), 400
    if not paths:
        return jsonify({'error': 'No files to transfer'}), 400

    # Get SSH config
    from blueprints.backup import load_ssh_configs
    configs = load_ssh_configs()
    server = next((c for c in configs if c.get('id') == server_id), None)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    # Quick connectivity check before starting background transfer
    import socket as _socket
    host = server.get('host', '')
    port = server.get('port', 22)
    try:
        s = _socket.create_connection((host, port), timeout=5)
        s.close()
    except Exception:
        return jsonify({'error': f'Server {server.get("name",host)} ({host}:{port}) is unreachable. Check that it is powered on and connected to the network.'}), 502

    # Resolve local paths
    resolved = []
    for p in paths:
        rp = _main().safe_path(p)
        if rp and os.path.exists(rp):
            resolved.append(rp)
    if not resolved:
        return jsonify({'error': 'None of the paths exist'}), 400

    # Compute total bytes
    total_bytes = 0
    total_files = 0
    for rp in resolved:
        if os.path.isdir(rp):
            for root, dirs, files in os.walk(rp):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except OSError:
                        pass
        else:
            try:
                total_bytes += os.path.getsize(rp)
                total_files += 1
            except OSError:
                pass

    with _main()._fileop_lock:
        if _main()._fileop_state['active']:
            return jsonify({'error': 'Another file operation is in progress'}), 400
        _main()._fileop_state['active'] = True
        _main()._fileop_state['operation'] = 'transfer'
        _main()._fileop_state['progress'] = None
        _main()._fileop_state['cancel'] = False
        _main()._fileop_state['paused'] = False
        _main()._fileop_state['meta'] = {
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paths': [os.path.basename(p) for p in resolved[:10]],
            'total_bytes': total_bytes,
            'total_files': total_files,
            'started': time.time(),
        }

    dest = remote_dest or server.get('remote_path', '~/')
    # Capture requesting user for per-user known_hosts
    requesting_user = getattr(g, 'username', None) or 'root'

    # Persist transfer task so it can resume after server restart
    _main()._save_transfer_task(resolved, server_id, dest, requesting_user)

    socketio.start_background_task(_bg_transfer_remote, resolved, server, dest, total_bytes, total_files, requesting_user)

    size_str = _fmt_bytes(total_bytes)
    return jsonify({
        'async': True,
        'cancellable': True,
        'message': f'Transferring {len(resolved)} items ({size_str}) to {server["name"]}'
    })


def _fmt_bytes(b):
    """Format bytes to human-readable — delegates to utils.fmt_bytes."""
    return fmt_bytes(b)


def _bg_transfer_remote(resolved, server, remote_dest, total_bytes, total_files, requesting_user='root'):
    """Background rsync transfer with real-time progress, resume and cancel support."""
    import subprocess, re as _re, shlex as _shlex, pty, select as _select

    host = server['host']
    port = server.get('port', 22)
    user = server['username']
    password = server.get('password', '')
    key_path = server.get('key_path', '')

    # Resolve the requesting user's home directory
    _user_home = _get_user_home(requesting_user)
    user_kh = os.path.join(_user_home, '.ssh', 'known_hosts')

    # Ensure .ssh dir exists for the user
    ssh_dir = os.path.join(_user_home, '.ssh')
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    try:
        import pwd
        pw = pwd.getpwnam(requesting_user)
        os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass

    # Build SSH command for rsync — use user's known_hosts file
    # Quote paths that may contain spaces
    ssh_cmd_parts = ['ssh', '-p', str(port), '-o', 'StrictHostKeyChecking=accept-new',
                     '-o', 'UserKnownHostsFile=' + _shlex.quote(user_kh),
                     '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=10',
                     '-o', 'ServerAliveCountMax=3']
    if key_path and os.path.exists(key_path):
        ssh_cmd_parts += ['-i', _shlex.quote(key_path)]
    ssh_cmd_str = ' '.join(ssh_cmd_parts)

    # Resolve remote dest (expand ~)
    dest = remote_dest
    if '~' in dest:
        try:
            from ssh_utils import get_ssh_client as _gsc, ssh_resolve_home as _srh
            ssh = _gsc(host, port, user, password=password, key_path=key_path, timeout=15)
            home = _srh(ssh)
            if home:
                dest = dest.replace('~', home)
            ssh.exec_command(f'mkdir -p {_shlex.quote(dest)}')
            gevent.sleep(0.3)
            ssh.close()
        except Exception:
            pass

    # Pre-compute per-item sizes for accurate progress
    item_sizes = []
    for rp in resolved:
        if os.path.isfile(rp):
            try:
                item_sizes.append(os.path.getsize(rp))
            except OSError:
                item_sizes.append(0)
        else:
            sz = 0
            for rt, _, fns in os.walk(rp):
                for fn in fns:
                    try:
                        sz += os.path.getsize(os.path.join(rt, fn))
                    except OSError:
                        pass
            item_sizes.append(sz)

    bytes_sent_total = [0]
    files_done = [0]
    current_file = ['']
    last_emit = [0]

    def emit_progress(forced=False):
        now = time.time()
        if not forced and now - last_emit[0] < 0.3:
            return
        last_emit[0] = now
        pct = round(bytes_sent_total[0] / total_bytes * 100, 1) if total_bytes > 0 else 0

        # Store byte-based progress in state (so /operation-status returns accurate %)
        detail = {
            'bytes_sent': bytes_sent_total[0], 'total_bytes': total_bytes,
            'files_sent': files_done[0], 'total_files': total_files,
            'current_file': current_file[0], 'percent': pct,
            'sent_fmt': _fmt_bytes(bytes_sent_total[0]), 'total_fmt': _fmt_bytes(total_bytes),
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paused': _main()._fileop_is_paused(),
        }
        prog = {'operation': 'transfer', 'current_file': current_file[0],
                'done': files_done[0], 'total': total_files, 'percent': pct}
        with _main()._fileop_lock:
            _main()._fileop_state['progress'] = prog
        _fileop_emit('fileop_progress', prog)
        socketio.emit('fileop_transfer_detail', detail)

    def _run_rsync(rsync_cmd, env, idx, local_path, name):
        """Run rsync with PTY for real-time progress. Returns (returncode, stderr_text)."""
        master_fd, slave_fd = pty.openpty()
        err_r, err_w = os.pipe()
        proc = subprocess.Popen(
            rsync_cmd, stdout=slave_fd, stderr=err_w,
            env=env, close_fds=True
        )
        os.close(slave_fd)
        os.close(err_w)
        with _main()._fileop_lock:
            _main()._fileop_state['_proc'] = proc

        patt = _re.compile(r'([\d,]+)\s+(\d+)%\s+([\d.]+\w+/s)')
        buf = ''
        completed_bytes = sum(item_sizes[:idx])

        try:
            while True:
                if _main()._fileop_cancelled():
                    # Resume first if paused (stopped process ignores SIGTERM)
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                    os.close(master_fd)
                    os.close(err_r)
                    with _main()._fileop_lock:
                        _main()._fileop_state['_proc'] = None
                    return (-999, 'cancelled')

                rlist = _select.select([master_fd], [], [], 0.5)[0]
                if rlist:
                    try:
                        data = os.read(master_fd, 4096).decode('utf-8', errors='replace')
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data
                    parts = _re.split(r'[\r\n]+', buf)
                    buf = parts[-1]
                    for p in parts[:-1]:
                        m = patt.search(p)
                        if m:
                            file_pct = int(m.group(2))
                            bytes_sent_total[0] = completed_bytes + int(item_sizes[idx] * file_pct / 100)
                            current_file[0] = name
                            emit_progress()
                    gevent.sleep(0)
                elif proc.poll() is not None:
                    break
        except (OSError, IOError) as exc:
            print(f'  [transfer] PTY read error for {name}: {exc}')
        except Exception as exc:
            print(f'  [transfer] Unexpected error in rsync read loop for {name}: {exc}')

        os.close(master_fd)
        # Use timeout to prevent permanent hang on stuck rsync
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print(f'  [transfer] rsync process for {name} did not exit in 30s, killing…')
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        stderr_text = ''
        try:
            stderr_text = os.read(err_r, 65536).decode('utf-8', errors='replace')
        except Exception:
            pass
        os.close(err_r)
        with _main()._fileop_lock:
            _main()._fileop_state['_proc'] = None
        return (proc.returncode, stderr_text)

    try:
        for idx, local_path in enumerate(resolved):
            if _main()._fileop_cancelled():
                _main()._clear_transfer_task()
                _main()._fileop_finish('transfer', False, 'Transfer cancelled by user')
                return

            # Wait while paused (between items)
            while _main()._fileop_is_paused():
                if _main()._fileop_cancelled():
                    _main()._clear_transfer_task()
                    _main()._fileop_finish('transfer', False, 'Transfer cancelled by user')
                    return
                gevent.sleep(0.5)

            name = os.path.basename(local_path)
            current_file[0] = name
            remote_target = f'{user}@{host}:{_shlex.quote(dest)}/'

            rsync_cmd = [
                'rsync', '-rlt', '--partial', '--inplace',
                '--info=progress2', '--no-inc-recursive',
                '-e', ssh_cmd_str
            ]
            src = local_path.rstrip('/') + '/' if os.path.isdir(local_path) else local_path
            if os.path.isdir(local_path):
                remote_target = f'{user}@{host}:{_shlex.quote(dest + "/" + name)}/'
            rsync_cmd += [src, remote_target]

            env = os.environ.copy()
            if password and not (key_path and os.path.exists(key_path)):
                rsync_cmd = ['sshpass', '-e'] + rsync_cmd
                env['SSHPASS'] = password

            rc, stderr_out = _run_rsync(rsync_cmd, env, idx, local_path, name)

            if rc == -999:  # cancelled
                _main()._clear_transfer_task()
                _main()._fileop_finish('transfer', False, 'Transfer cancelled by user')
                return

            if rc not in (0, 24):
                # On network/IO error, retry up to 3 times (rsync resumes from partial)
                retryable_codes = (1, 5, 10, 11, 12, 23, 30, 35, 255)
                if rc in retryable_codes:
                    max_retries = 3
                    retry_ok = False
                    for attempt in range(1, max_retries + 1):
                        delay = min(5 * attempt, 15)  # 5s, 10s, 15s
                        _main()._fileop_progress('transfer', f'Retrying ({attempt}/{max_retries}): {name}…', files_done[0], total_files)
                        print(f'  [transfer] rsync {name} failed (rc={rc}), retry {attempt}/{max_retries} in {delay}s')
                        gevent.sleep(delay)
                        if _main()._fileop_cancelled():
                            _main()._clear_transfer_task()
                            _main()._fileop_finish('transfer', False, 'Transfer cancelled by user')
                            return
                        rc2, stderr2 = _run_rsync(rsync_cmd, env, idx, local_path, name)
                        if rc2 == -999:
                            _main()._clear_transfer_task()
                            _main()._fileop_finish('transfer', False, 'Transfer cancelled by user')
                            return
                        if rc2 in (0, 24):
                            retry_ok = True
                            break
                        rc = rc2
                        stderr_out = stderr2
                    if not retry_ok:
                        raise RuntimeError(f'rsync {name} (rc={rc}): {stderr_out.strip()}')
                else:
                    raise RuntimeError(f'rsync {name} (rc={rc}): {stderr_out.strip()}')

            files_done[0] = idx + 1
            bytes_sent_total[0] = sum(item_sizes[:idx + 1])
            emit_progress(forced=True)

        with _main()._fileop_lock:
            _main()._fileop_state['_proc'] = None
        _main()._clear_transfer_task()
        size_str = _fmt_bytes(total_bytes)
        _main()._fileop_finish('transfer', True,
                       f'Transferred {files_done[0]} files ({size_str}) to {server["name"]}')
    except Exception as e:
        import traceback
        print(f'  [transfer] EXCEPTION: {e}')
        traceback.print_exc()
        with _main()._fileop_lock:
            _main()._fileop_state['_proc'] = None
        # On crash / network error keep resume file so startup can retry.
        # Only clear on explicit cancel or permanent failure (we keep the file
        # for retryable errors so the next startup picks it up).
        err_msg = str(e)
        retryable_keywords = ('No route to host', 'Connection refused', 'timed out',
                              'Connection reset', 'Broken pipe', 'Network is unreachable',
                              'Connection timed out', 'Name or service not known')
        is_retryable = any(kw in err_msg for kw in retryable_keywords)
        if not is_retryable:
            _main()._clear_transfer_task()
        # User-friendly error messages for common network errors
        for kw in ('No route to host', 'Connection refused', 'timed out', 'Connection reset',
                   'Network is unreachable', 'Name or service not known'):
            if kw.lower() in err_msg.lower():
                err_msg = f'Cannot connect to {server["name"]} ({server["host"]}). Check that the server is powered on and connected to the network.'
                break
        _main()._fileop_finish('transfer', False, err_msg)


def _resume_interrupted_transfer():
    """Check for a persisted transfer task and resume it after server restart."""
    TRANSFER_RESUME_FILE = _get_transfer_resume_file()
    task = _load_json(TRANSFER_RESUME_FILE, None)
    if not task or not isinstance(task, dict):
        return
    resolved = task.get('paths', [])
    server_id = task.get('server_id')
    remote_dest = task.get('remote_dest', '~/')
    requesting_user = task.get('requesting_user', 'root')
    started = task.get('started', 0)

    # Ignore tasks older than 7 days (stale)
    if time.time() - started > 7 * 86400:
        _main()._clear_transfer_task()
        return

    if not resolved or not server_id:
        _main()._clear_transfer_task()
        return

    # Verify at least one source still exists and validate paths
    valid = []
    for p in resolved:
        # Only allow absolute paths and ensure they actually exist
        rp = os.path.realpath(p)
        if rp and os.path.isabs(rp):
            try:
                exists = _fs_call(os.path.exists, rp, timeout=3)
            except TimeoutError:
                exists = False
            if exists:
                valid.append(rp)
    if not valid:
        _main()._clear_transfer_task()
        return

    # Load server config
    try:
        from blueprints.backup import load_ssh_configs
        configs = load_ssh_configs()
        server = next((c for c in configs if c.get('id') == server_id), None)
    except Exception:
        server = None
    if not server:
        _main()._clear_transfer_task()
        return

    # Quick connectivity check before resuming
    import socket as _socket
    try:
        s = _socket.create_connection((server.get('host', ''), server.get('port', 22)), timeout=10)
        s.close()
    except Exception:
        print(f'  [transfer-resume] Server {server.get("name", server.get("host", ""))} is unreachable, skipping resume for now')
        # Don't clear the task — it will be retried on next restart
        return

    # Compute totals
    total_bytes = 0
    total_files = 0
    for rp in valid:
        if os.path.isdir(rp):
            for root, dirs, files in os.walk(rp):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total_bytes += os.path.getsize(fp)
                        total_files += 1
                    except OSError:
                        pass
        else:
            try:
                total_bytes += os.path.getsize(rp)
                total_files += 1
            except OSError:
                pass

    with _main()._fileop_lock:
        if _main()._fileop_state['active']:
            return  # something else already running
        _main()._fileop_state['active'] = True
        _main()._fileop_state['operation'] = 'transfer'
        _main()._fileop_state['progress'] = None
        _main()._fileop_state['cancel'] = False
        _main()._fileop_state['meta'] = {
            'server_name': server.get('name', server.get('host', '')),
            'server_host': server.get('host', ''),
            'paths': [os.path.basename(p) for p in valid[:10]],
            'total_bytes': total_bytes,
            'total_files': total_files,
            'started': started,
            'resumed': True,
        }

    dest = remote_dest or server.get('remote_path', '~/')
    print(f'  [transfer-resume] Resuming transfer of {len(valid)} items to {server.get("name", server.get("host", ""))}…')
    elog('files', 'info', f'Resuming interrupted transfer to {server.get("name", server.get("host", ""))} ({len(valid)} items)')
    socketio.start_background_task(_bg_transfer_remote, valid, server, dest, total_bytes, total_files, requesting_user)


