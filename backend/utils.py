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
import stat as stat_lib
import subprocess
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
        The user-visible path (e.g. ``/home/marcin/docs``).
    isolate_home : bool
        If True, blocks access to ``/home/<other_user>``.
    current_user : str | None
        Username for home isolation.  Falls back to ``g.username``.
    sudo_mode : bool
        If True, skip ALLOWED_ROOTS restriction (admin full-system access).
    """
    if not user_path:
        return None
    clean = user_path.strip().replace('\\', '/')
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
    """
    from PIL import Image

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

        # 3) Generate thumbnail
        img = Image.open(real_path)
        img.thumbnail((w * 2, h * 2), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='WEBP', quality=75)
        thumb_bytes = buf.getvalue()

        # Store in local .thumbs/ directory
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
                        install_message='Gotowe.',
                        install_deps=None,
                        wipe_files=None,
                        wipe_dirs=None,
                        status_extras=None,
                        on_uninstall=None,
                        url_prefix=None,
                        dep_owner=None,
                        require_admin_install=True,
                        require_sudo_install=True):
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
    """
    from host import claim_dep, release_dep, check_dep  # avoid circular at module level
    import shutil as _shutil

    pfx = url_prefix or ''
    owner_id = dep_owner or bp.name

    @bp.route(f'{pfx}/install', methods=['POST'],
              endpoint=f'{bp.name}_pkg_install')
    def _pkg_install():
        if require_admin_install and getattr(g, 'role', None) != 'admin':
            return jsonify({'ok': False, 'error': 'Brak uprawnień'}), 403
        errors = []
        if install_deps:
            for dep in install_deps:
                try:
                    ok, msg = claim_dep(dep, owner_id)
                    if not ok:
                        errors.append(msg or f'Instalacja {dep} nie powiodła się')
                except Exception as exc:
                    errors.append(f'Błąd instalacji {dep}: {exc}')
        if errors:
            return jsonify({'ok': False, 'errors': errors}), 500
        return jsonify({'ok': True, 'message': install_message})

    @bp.route(f'{pfx}/uninstall', methods=['POST'],
              endpoint=f'{bp.name}_pkg_uninstall')
    def _pkg_uninstall():
        if require_admin_install and getattr(g, 'role', None) != 'admin':
            return jsonify({'ok': False, 'error': 'Brak uprawnień'}), 403
        wipe = (request.json or {}).get('wipe_data', False)
        dep_errors = []
        if install_deps:
            for dep in install_deps:
                try:
                    ok, msg = release_dep(dep, owner_id)
                    if not ok:
                        dep_errors.append(msg or f'Odinstalowanie zależności {dep} nie powiodło się')
                except Exception as exc:
                    dep_errors.append(f'Błąd odinstalowania {dep}: {exc}')
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
