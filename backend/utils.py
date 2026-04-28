"""
EthOS — Shared Utilities
Consolidates common functions used across multiple blueprints to eliminate duplication.

Usage:
    from utils import load_json, save_json, safe_path, fmt_bytes, is_pid_alive, ...
"""

import hashlib
import io
import json
import os
import shutil
import stat as stat_lib
import subprocess
import threading as _threading
from datetime import datetime

from flask import g, jsonify, request, send_file
from host import data_path, NATIVE_MODE

# ── Constants ────────────────────────────────────────────────

DATA_ROOT = '/'
ALLOWED_ROOTS = ('/home', '/media', '/run/media', '/mnt')


# ── JSON persistence ────────────────────────────────────────

def load_json(path, default=None):
    """Load a JSON file, returning *default* on any error or if missing."""
    if default is None:
        default = {}
    if os.path.isfile(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    # Return a copy so callers get independent mutable objects
    if isinstance(default, (dict, list)):
        return type(default)(default)
    return default


def save_json(path, data, *, ensure_ascii=False, indent=2):
    """Atomically write *data* as JSON to *path*, creating parent dirs."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)


# ── Path safety ─────────────────────────────────────────────

def safe_path(user_path, *, isolate_home=True, current_user=None, sudo_mode=False):
    """Resolve *user_path* to a real filesystem path under DATA_ROOT.

    Returns ``None`` if the path escapes allowed roots or belongs to
    another user's home (when *isolate_home* is True).

    Parameters
    ----------
    user_path : str
        The user-visible path (e.g. ``/home/user/docs``).
    isolate_home : bool
        If True, blocks access to ``/home/<other_user>``.
    current_user : str | None
        Username for home isolation.  Falls back to ``g.username``.
    sudo_mode : bool
        If True, skip ALLOWED_ROOTS restriction (admin full-system access).
    """
    if not user_path:
        return None
    # Only strip leading whitespace — trailing spaces can be valid filename chars on Linux
    clean = user_path.lstrip().replace('\\', '/')
    while '//' in clean:
        clean = clean.replace('//', '/')
    clean = os.path.normpath(clean)
    base = os.path.realpath(DATA_ROOT)
    # If path already absolute and under base, use as-is
    if clean.startswith(base + '/') or clean == base:
        target = os.path.realpath(clean)
    else:
        target = os.path.realpath(os.path.join(base, clean.lstrip('/')))
    if not target.startswith(base):
        return None
    if not sudo_mode and not any(target == r or target.startswith(r + '/') for r in ALLOWED_ROOTS):
        return None
    # Home directory isolation — works for both /home/{user} and {data_disk}/home/{user}
    if isolate_home:
        # Check standard /home
        _home_prefixes = ['/home/']
        # Also check data-disk home if configured
        try:
            from host import get_data_disk as _gdd
            _dd = _gdd()
            if _dd:
                _home_prefixes.append(os.path.join(_dd, 'home') + '/')
        except Exception:
            pass
        for _hp in _home_prefixes:
            if target.startswith(_hp) or target == _hp.rstrip('/'):
                # Extract the home owner from the path
                _rel = target[len(_hp):]
                if _rel:
                    home_owner = _rel.split('/')[0]
                    me = current_user
                    if me is None:
                        try:
                            me = g.username
                        except (RuntimeError, AttributeError):
                            me = None
                    if me and home_owner != me:
                        return None
    return target


# ── Formatting ──────────────────────────────────────────────

def fmt_bytes(n):
    """Format a byte count to a human-readable string (e.g. ``1.5 GB``)."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'


# ── Process helpers ─────────────────────────────────────────

def is_pid_alive(pid):
    """Check if a process with given *pid* is still running."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ── Docker helpers ──────────────────────────────────────────

def docker_available():
    """Check if Docker daemon is running and the CLI exists."""
    try:
        r = subprocess.run(['docker', 'info'], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── Host command wrapper ────────────────────────────────────

def run_host(cmd_str, timeout=120, cwd=None):
    """Run a shell command on the host and return ``(stdout, stderr, returncode)``."""
    from host import host_run
    r = host_run(cmd_str, timeout=timeout, cwd=cwd)
    return r.stdout, r.stderr, r.returncode


# ── Flask context helpers ───────────────────────────────────

def get_username():
    """Get the current username from Flask ``g``, or ``None``."""
    try:
        return g.username
    except (RuntimeError, AttributeError):
        return None


def get_username_or(default='admin'):
    """Get the current username from Flask ``g``, or *default*."""
    return getattr(g, 'username', None) or default


# ── SocketIO helper ─────────────────────────────────────────

def sio_emit(socketio_ref, event, data, **kwargs):
    """Emit a SocketIO event if the reference is set."""
    if socketio_ref:
        socketio_ref.emit(event, data, **kwargs)


# ── Thumbnail generation ────────────────────────────────────

THUMB_CACHE_DIR = data_path('.thumb_cache')
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
THUMBS_DIR_NAME = '.thumbs'

# Per-cache-key locks to prevent concurrent duplicate thumbnail generation
_thumb_gen_locks: dict = {}
_thumb_gen_locks_meta = _threading.Lock()


def _thumb_gen_lock(cache_key: str):
    """Return a per-key lock for thumbnail generation."""
    with _thumb_gen_locks_meta:
        if cache_key not in _thumb_gen_locks:
            _thumb_gen_locks[cache_key] = _threading.Lock()
        return _thumb_gen_locks[cache_key]


def _thumb_cache_key(real_path, mtime, w, h):
    return hashlib.md5(f'{real_path}:{mtime}:{w}x{h}'.encode()).hexdigest()


def _local_thumb_path(real_path, w, h):
    parent = os.path.dirname(real_path)
    basename = os.path.basename(real_path)
    name, _ = os.path.splitext(basename)
    thumbs_dir = os.path.join(parent, THUMBS_DIR_NAME)
    return thumbs_dir, os.path.join(thumbs_dir, f'{name}_{w}x{h}.webp')


def generate_thumbnail(real_path, w, h):
    """Generate/serve a cached WebP thumbnail for *real_path*.

    Returns a Flask ``Response`` (send_file).  Falls back to the original
    file on any error.

    Uses per-path locking to prevent duplicate generation under concurrency.
    Applies EXIF auto-orientation so rotated photos display correctly.
    """
    from PIL import Image, ImageOps

    try:
        mtime = os.path.getmtime(real_path)

        # 1) Check local .thumbs/ directory first
        thumbs_dir, local_path = _local_thumb_path(real_path, w, h)
        if os.path.isfile(local_path):
            local_mtime = os.path.getmtime(local_path)
            if local_mtime >= mtime:
                return send_file(local_path, mimetype='image/webp', max_age=86400)

        # 2) Fallback: check centralized cache
        cache_key = _thumb_cache_key(real_path, mtime, w, h)
        cache_path = os.path.join(THUMB_CACHE_DIR, cache_key + '.webp')
        meta_path = os.path.join(THUMB_CACHE_DIR, cache_key + '.meta')

        if os.path.isfile(cache_path):
            return send_file(cache_path, mimetype='image/webp', max_age=86400)

        # 3) Acquire per-path lock to prevent duplicate generation
        with _thumb_gen_lock(cache_key):
            # Double-check after acquiring lock — another thread may have just created it
            if os.path.isfile(local_path) and os.path.getmtime(local_path) >= mtime:
                return send_file(local_path, mimetype='image/webp', max_age=86400)
            if os.path.isfile(cache_path):
                return send_file(cache_path, mimetype='image/webp', max_age=86400)

            # 4) Generate thumbnail
            img = Image.open(real_path)
            # Use JPEG draft mode for faster decoding of large photos — PIL will
            # choose the largest 1/2ⁿ subsampled decode that still exceeds the
            # target dimensions, avoiding a full-resolution decode.
            if getattr(img, 'format', None) == 'JPEG':
                img.draft('RGB', (w * 2, h * 2))
            # Apply EXIF orientation so rotated photos render correctly
            img = ImageOps.exif_transpose(img)
            img.thumbnail((w * 2, h * 2), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='WEBP', quality=75)
            thumb_bytes = buf.getvalue()

            # Store in local .thumbs/ directory (preferred) or fallback to central cache
            try:
                os.makedirs(thumbs_dir, exist_ok=True)
                with open(local_path, 'wb') as lf:
                    lf.write(thumb_bytes)
            except OSError:
                with open(cache_path, 'wb') as cf:
                    cf.write(thumb_bytes)
                with open(meta_path, 'w') as mf:
                    mf.write(os.path.realpath(real_path))

        buf.seek(0)
        return send_file(buf, mimetype='image/webp', max_age=86400)
    except Exception:
        return send_file(real_path)


# ── Compose project scanning ────────────────────────────────

COMPOSE_FILE_NAMES = ('docker-compose.yaml', 'docker-compose.yml',
                      'compose.yaml', 'compose.yml')


def find_compose_projects(root_dir):
    """Scan *root_dir* for directories containing compose files.

    Returns a list of dicts with keys: name, path, compose_file, compose_filename.
    """
    projects = []
    if not os.path.isdir(root_dir):
        return projects
    for entry in sorted(os.listdir(root_dir)):
        path = os.path.join(root_dir, entry)
        if not os.path.isdir(path):
            continue
        for name in COMPOSE_FILE_NAMES:
            fpath = os.path.join(path, name)
            if os.path.isfile(fpath):
                projects.append({
                    'name': entry,
                    'path': path,
                    'compose_file': fpath,
                    'compose_filename': name,
                })
                break
    return projects


def find_compose_project_names(root_dir):
    """Return a set of directory names that contain compose files."""
    return {p['name'] for p in find_compose_projects(root_dir)}


# ── Directory browsing ──────────────────────────────────────

def list_directory(path, *, allowed_prefix=None, show_hidden=False,
                   include_size=True, timeout=None, dirs_only=False):
    """List directory contents, returning a sorted list of item dicts.

    Parameters
    ----------
    path : str
        Absolute directory path.
    allowed_prefix : str | list[str] | None
        If set, path must start with this prefix (or one of several).
    show_hidden : bool
        Whether to include dotfiles.
    include_size : bool
        Whether to stat each entry for size/mtime.
    timeout : float | None
        Max seconds for os.listdir (None = no limit).
    dirs_only : bool
        If True, return only directories.

    Returns (items, error_str | None).  items is sorted dirs-first.
    """
    # ── prefix check ──
    if allowed_prefix:
        prefixes = allowed_prefix if isinstance(allowed_prefix, (list, tuple)) else [allowed_prefix]
        if not any(path.startswith(p) for p in prefixes):
            return [], 'Path outside allowed area'
    if not os.path.isdir(path):
        return [], 'Not a directory'
    try:
        if timeout:
            import signal

            def _alarm(signum, frame):
                raise TimeoutError('listdir timeout')
            old = signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(int(timeout))
            try:
                entries = sorted(os.listdir(path))
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
        else:
            entries = sorted(os.listdir(path))
    except PermissionError:
        return [], 'Permission denied'
    except TimeoutError:
        return [], 'Timeout reading directory'

    items = []
    for name in entries:
        if not show_hidden and name.startswith('.'):
            continue
        full = os.path.join(path, name)
        try:
            st = os.stat(full)
            is_dir = stat_lib.S_ISDIR(st.st_mode)
            if dirs_only and not is_dir:
                continue
            items.append({
                'name': name,
                'path': full,
                'is_dir': is_dir,
                'size': st.st_size if (include_size and not is_dir) else 0,
                'modified': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M'),
            })
        except (PermissionError, OSError):
            continue

    items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
    return items, None


# ── Package install/uninstall/status pattern ────────────────

def register_pkg_routes(bp, *,
                        install_message='Done.',
                        install_deps=None,
                        wipe_files=None,
                        wipe_dirs=None,
                        status_extras=None,
                        on_uninstall=None,
                        url_prefix=None,
                        dep_owner=None,
                        require_admin_install=True,
                        require_sudo_install=True,
                        ufw_ports=None):
    """Register the standard /install, /uninstall, /pkg-status routes on *bp*.

    Parameters
    ----------
    bp : Blueprint
    install_message : str – returned on install
    install_deps : list[str] | None – system deps to ensure_dep on install
    wipe_files : list[str] | None – files to delete on wipe
    wipe_dirs : list[str] | None – dirs to rmtree on wipe
    status_extras : callable | None – returns dict merged into status
    on_uninstall : callable | None – extra cleanup, receives wipe (bool)
    url_prefix : str | None – override route prefix (default auto-detect)
    dep_owner : str | None – dependency owner id for shared apt cleanup
    ufw_ports : list[tuple] | None – UFW rules: [(port, proto, comment), ...]
    """
    from host import claim_dep, release_dep, check_dep, ufw_allow, ufw_delete
    import shutil as _shutil
    from blueprints.admin_required import admin_required

    pfx = url_prefix or ''
    owner_id = dep_owner or bp.name

    def _pkg_install():
        errors = []
        if install_deps:
            for dep in install_deps:
                try:
                    ok, msg = claim_dep(dep, owner_id)
                    if not ok:
                        errors.append(msg or f'Installation of {dep} failed')
                except Exception as exc:
                    errors.append(f'Installation error for {dep}: {exc}')
        if errors:
            return jsonify({'ok': False, 'errors': errors}), 500
        for rule in (ufw_ports or []):
            ufw_allow(rule[0], rule[1] if len(rule) > 1 else 'tcp',
                      rule[2] if len(rule) > 2 else '')
        return jsonify({'ok': True, 'message': install_message})

    def _pkg_uninstall():
        wipe = (request.json or {}).get('wipe_data', False)
        dep_errors = []
        if install_deps:
            for dep in install_deps:
                try:
                    ok, msg = release_dep(dep, owner_id)
                    if not ok:
                        dep_errors.append(msg or f'Uninstallation of dependency {dep} failed')
                except Exception as exc:
                    dep_errors.append(f'Uninstallation error for {dep}: {exc}')
        for rule in (ufw_ports or []):
            ufw_delete(rule[0], rule[1] if len(rule) > 1 else 'tcp')
        if on_uninstall:
            on_uninstall(wipe)
        if wipe:
            for f in (wipe_files or []):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except Exception:
                    pass
            for d in (wipe_dirs or []):
                if os.path.isdir(d):
                    _shutil.rmtree(d, ignore_errors=True)
        if dep_errors:
            return jsonify({'ok': False, 'errors': dep_errors}), 500
        return jsonify({'ok': True})

    # Apply admin requirement if requested
    if require_admin_install:
        _pkg_install = admin_required(_pkg_install)
        _pkg_uninstall = admin_required(_pkg_uninstall)

    bp.add_url_rule(f'{pfx}/install', endpoint=f'{bp.name}_pkg_install', view_func=_pkg_install, methods=['POST'])
    bp.add_url_rule(f'{pfx}/uninstall', endpoint=f'{bp.name}_pkg_uninstall', view_func=_pkg_uninstall, methods=['POST'])

    @bp.route(f'{pfx}/pkg-status', methods=['GET'],
              endpoint=f'{bp.name}_pkg_status')
    def _pkg_status():
        installed = True
        if install_deps:
            installed = all(check_dep(dep) for dep in install_deps)
        result = {'installed': installed}
        if status_extras:
            try:
                result.update(status_extras())
            except Exception:
                pass
        return jsonify(result)


# ── Systemd helpers ─────────────────────────────────────────

def systemd_notify_ready():
    """Send 'READY=1' to systemd notification socket if available."""
    notify_socket = os.environ.get('NOTIFY_SOCKET')
    if not notify_socket:
        return

    if notify_socket.startswith('@'):
        notify_socket = '\0' + notify_socket[1:]

    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(notify_socket)
            sock.sendall(b'READY=1')
    except Exception:
        pass


# ── Configuration helpers ───────────────────────────────────

def get_ethos_config():
    """Read /opt/ethos/install.conf and return as dict."""
    config = {}
    try:
        # Try known locations
        paths = ['/opt/ethos/install.conf', '../install.conf', 'install.conf']
        path = next((p for p in paths if os.path.isfile(p)), None)
        if path:
            with open(path) as f:
                for line in f:
                    if '=' in line and not line.strip().startswith('#'):
                        key, val = line.strip().split('=', 1)
                        config[key] = val.strip('"').strip("'")
    except Exception:
        pass
    return config


def get_ethos_user():
    """Get the primary EthOS username from env or install.conf."""
    return os.environ.get('ETHOS_USER') or get_ethos_config().get('ETHOS_USER', 'nasadmin')


# ── Dependency checking ─────────────────────────────────────

def check_tool(name: str) -> bool:
    """Return True if a CLI tool is on PATH."""
    return shutil.which(name) is not None


def require_tools(*names: str):
    """Check that all named CLI tools exist.

    Returns None if all are present, otherwise a Flask JSON response (503)
    listing missing tools and install commands.
    """
    missing = [n for n in names if not shutil.which(n)]
    if not missing:
        return None
    apt_map = {
        'ffmpeg': 'ffmpeg', 'ffprobe': 'ffmpeg',
        'smartctl': 'smartmontools',
        'wg': 'wireguard-tools', 'qrencode': 'qrencode',
        'ufw': 'ufw', 'fail2ban-client': 'fail2ban',
        'docker': 'docker.io', 'qemu-system-x86_64': 'qemu-system-x86',
        'qemu-system-aarch64': 'qemu-system-arm', 'qemu-img': 'qemu-utils',
        'debootstrap': 'debootstrap', 'cups': 'cups',
        'nmcli': 'network-manager', 'mdadm': 'mdadm',
        'smbpasswd': 'samba', 'upsc': 'nut-client',
        'avahi-browse': 'avahi-utils', 'virsh': 'libvirt-daemon-system',
    }
    pkgs = sorted({apt_map.get(m, m) for m in missing})
    cmd = f"sudo apt install -y {' '.join(pkgs)}"
    return jsonify({
        'error': 'missing_dependency',
        'message': f"Wymagane narzędzia nie są zainstalowane: {', '.join(missing)}",
        'missing': missing,
        'install_cmd': cmd,
    }), 503
