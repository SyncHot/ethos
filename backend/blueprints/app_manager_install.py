"""
EthOS - App Manager Install/Uninstall Module

Handles app lifecycle: install, uninstall, repair operations.
"""

import os
import json
import logging
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, host_run_stream, data_path, q, _apt_exec, apt_install as _host_apt_install

log = logging.getLogger('app_manager')

# ─── Module state - set by main app_manager ──────────────────

_emit = None
_flask_app = None
_socketio = None
_ETHOS_ROOT = None
_BLUEPRINTS_DIR = None
_FRONTEND_APPS_DIR = None

# Constants needed
APP_UPDATE_CONFIG_FILE = None
DEFAULT_GITHUB_REPO = 'SyncHot/ethos-os-ethos-apps'


def set_emit_fn(fn):
    """Called from app_manager.py to set the emit callback."""
    global _emit
    _emit = fn


def set_flask_app(app):
    """Called from app_manager.py."""
    global _flask_app
    _flask_app = app


def set_socketio(sio):
    """Called from app_manager.py."""
    global _socketio
    _socketio = sio


def set_constants(ethos_root, blueprints_dir, frontend_apps_dir, app_update_cfg_file):
    """Called from app_manager to set paths and constants."""
    global _ETHOS_ROOT, _BLUEPRINTS_DIR, _FRONTEND_APPS_DIR, APP_UPDATE_CONFIG_FILE
    _ETHOS_ROOT = ethos_root
    _BLUEPRINTS_DIR = blueprints_dir
    _FRONTEND_APPS_DIR = frontend_apps_dir
    APP_UPDATE_CONFIG_FILE = app_update_cfg_file


# ─── Helper functions ────────────────────────────────────────

def _get_github_app_base():
    """Get the GitHub base URL, respecting custom repo config. Fallback for non-sourced apps."""
    try:
        cfg = json.load(open(APP_UPDATE_CONFIG_FILE))
        repo = cfg.get('github_repo', DEFAULT_GITHUB_REPO)
    except Exception:
        repo = DEFAULT_GITHUB_REPO
    return f'https://raw.githubusercontent.com/{repo}/main/apps'


_MIN_FREE_MB = 300  # minimum free space on root before apt/pip install



def _semver_key(ver):
    """Return a sortable tuple for semver comparison (handles 1.10.0 > 1.9.0 correctly)."""
    try:
        parts = str(ver).split('.')
        return tuple(int(p) for p in (parts + ['0', '0', '0'])[:3])
    except Exception:
        return (0, 0, 0)



def _ensure_root_space(emit_fn):
    """Check root partition free space; proactively clean caches before install."""
    _PROACTIVE_CLEAN_MB = 600  # always clean caches if less than this
    try:
        st = os.statvfs('/')
        free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)

        if free_mb < _PROACTIVE_CLEAN_MB:
            log.info('[app_manager] Root space %.0f MB < %d MB — proactive cache cleanup', free_mb, _PROACTIVE_CLEAN_MB)
            emit_fn({'stage': 'cleanup', 'percent': 23,
                     'message': f'Czyszczenie cache ({free_mb:.0f} MB wolne)...',
                     'status': 'running'})

            host_run('apt-get clean 2>/dev/null', timeout=30)
            host_run('apt-get autoremove -y 2>/dev/null', timeout=60)
            host_run('rm -rf /root/.cache/pip /tmp/pip-* 2>/dev/null', timeout=10)
            # Remove stale __pycache__ from venv (safe, regenerated on import)
            venv_dir = os.path.join(os.environ.get('ETHOS_ROOT', '/opt/ethos'), 'venv')
            host_run(f'find {q(venv_dir)} -name __pycache__ -type d -exec rm -rf {{}} + 2>/dev/null',
                     timeout=30)

            st = os.statvfs('/')
            free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
            log.info('[app_manager] After cleanup: %.0f MB free', free_mb)
            emit_fn({'stage': 'cleanup', 'percent': 24,
                     'message': f'Po czyszczeniu: {free_mb:.0f} MB wolne',
                     'status': 'running'})

        if free_mb < _MIN_FREE_MB:
            emit_fn({'stage': 'error', 'percent': 0,
                     'message': f'Brak miejsca na dysku ({free_mb:.0f} MB wolne, potrzeba {_MIN_FREE_MB} MB). '
                                'Zwolnij miejsce na partycji root.',
                     'status': 'error'})
            return False

    except Exception as e:
        log.warning('[app_manager] Space check error: %s', e)
    return True



def _install_apt_deps(deps, emit_fn):
    """Install APT dependencies with streaming progress updates."""
    if not deps:
        return True

    # Filter out already-installed packages to avoid unnecessary apt-get update
    missing = []
    for pkg in deps:
        check = host_run(f'dpkg -l {q(pkg)} 2>/dev/null | grep -q "^ii"', timeout=10)
        if check.returncode != 0:
            missing.append(pkg)
    if not missing:
        log.info('[app_manager] All apt deps already installed: %s', deps)
        emit_fn({'stage': 'deps_apt', 'message': 'Pakiety apt juz zainstalowane', 'percent': 42, 'status': 'running'})
        return True

    pkgs = ' '.join(q(d) for d in missing)
    emit_fn({'stage': 'deps_apt', 'message': 'apt-get update...', 'percent': 25, 'status': 'running'})

    # Auto-recover from interrupted dpkg (common after power loss or killed installs)
    cmd = (
        f'DEBIAN_FRONTEND=noninteractive dpkg --configure -a 2>/dev/null; '
        f'DEBIAN_FRONTEND=noninteractive apt-get update -y -qq 2>/dev/null; '
        f'DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs} 2>&1'
    )
    lock_file = '/tmp/ethos-apt.lock'
    wrapped = f"flock -w 180 {q(lock_file)} bash -lc {q(cmd)}"

    exit_code = -1
    last_err = ''
    count = 0
    last_emit = time.time()
    for line in host_run_stream(wrapped):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            exit_code = int(stripped.split(':', 1)[1])
            break
        if not stripped:
            continue
        lower = stripped.lower()
        if 'e:' in lower or 'err' in lower:
            last_err = stripped
        # Emit on keywords OR as keepalive every 4s to prevent stuck progress bar
        now = time.time()
        is_keyword = any(kw in lower for kw in ('unpacking', 'setting up', 'installing', 'get:', 'fetched', 'reading', 'building'))
        if is_keyword or (now - last_emit >= 4):
            count += 1
            pct = min(40, 28 + count)
            emit_fn({'stage': 'deps_apt', 'message': stripped[:80], 'percent': pct, 'status': 'running'})
            last_emit = now

    if exit_code != 0:
        detail = last_err[:120] if last_err else f'exit code {exit_code}'
        log.error('[app_manager] apt install failed (rc=%s): %s', exit_code, detail)
        emit_fn({'stage': 'error', 'percent': 0,
                 'message': f'apt: {detail}', 'status': 'error'})
        return False
    emit_fn({'stage': 'deps_apt', 'message': 'Pakiety apt zainstalowane', 'percent': 42, 'status': 'running'})
    host_run('apt-get clean 2>/dev/null && apt-get autoremove -y 2>/dev/null', timeout=60)
    return True



def _install_pip_deps(deps, emit_fn):
    """Install pip dependencies with streaming progress updates."""
    if not deps:
        return True

    # Filter out already-installed pip packages
    venv = os.path.join(_ETHOS_ROOT, 'venv')
    pip = os.path.join(venv, 'bin', 'pip') if os.path.isdir(venv) else 'pip3'
    python = os.path.join(venv, 'bin', 'python') if os.path.isdir(venv) else 'python3'

    missing = []
    for pkg in deps:
        pkg_name = pkg.split('==')[0].split('>=')[0].split('<=')[0].strip()
        check = host_run(f'{q(python)} -c "import importlib; importlib.import_module({q(pkg_name.replace("-","_"))})" 2>/dev/null', timeout=10)
        if check.returncode != 0:
            # Also try pip show as fallback
            check2 = host_run(f'{q(pip)} show {q(pkg_name)} 2>/dev/null | grep -q "^Name:"', timeout=10)
            if check2.returncode != 0:
                missing.append(pkg)
    if not missing:
        log.info('[app_manager] All pip deps already installed: %s', deps)
        emit_fn({'stage': 'deps_pip', 'message': 'Pakiety pip juz zainstalowane', 'percent': 57, 'status': 'running'})
        return True

    pkgs = ' '.join(q(d) for d in missing)
    emit_fn({'stage': 'deps_pip', 'message': 'pip install: ' + ', '.join(missing), 'percent': 45, 'status': 'running'})

    cmd = q(pip) + ' install --progress-bar off ' + pkgs + ' 2>&1'

    exit_code = -1
    last_err = ''
    count = 0
    last_emit = time.time()
    for line in host_run_stream(cmd):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            exit_code = int(stripped.split(':', 1)[1])
            break
        if not stripped:
            continue
        lower = stripped.lower()
        if 'error' in lower:
            last_err = stripped
        # Emit on keywords OR as keepalive every 4s to prevent stuck progress bar
        now = time.time()
        is_keyword = any(kw in lower for kw in ('collecting', 'downloading', 'installing', 'building', 'successfully', 'obtaining'))
        if is_keyword or (now - last_emit >= 4):
            count += 1
            pct = min(55, 47 + count)
            emit_fn({'stage': 'deps_pip', 'message': stripped[:80], 'percent': pct, 'status': 'running'})
            last_emit = now

    if exit_code != 0:
        detail = last_err[:120] if last_err else f'exit code {exit_code}'
        log.error('[app_manager] pip install failed (rc=%s): %s', exit_code, detail)
        emit_fn({'stage': 'error', 'percent': 0,
                 'message': f'pip: {detail}', 'status': 'error'})
        return False
    emit_fn({'stage': 'deps_pip', 'message': 'Pakiety pip zainstalowane', 'percent': 57, 'status': 'running'})
    host_run(q(pip) + ' cache purge 2>/dev/null', timeout=30)
    return True



def _sync_frontend_dist():
    frontend = os.path.join(_ETHOS_ROOT, 'frontend')
    dist = os.path.join(_ETHOS_ROOT, 'frontend_dist')
    if os.path.isdir(dist):
        host_run('rsync -a --delete ' + q(frontend + '/') + ' ' + q(dist + '/'), timeout=60)
    # Invalidate the index.html cache so new/removed scripts are picked up
    import sys
    app_mod = sys.modules.get('app')
    if app_mod:
        cache = getattr(app_mod, '_INDEX_CACHE', None)
        if cache:
            cache['html'] = None


_active_tasks = 0
_active_tasks_lock = threading.Lock()



def _task_start():
    """Increment active background task counter."""
    global _active_tasks
    with _active_tasks_lock:
        _active_tasks += 1



def _task_done():
    """Decrement active background task counter."""
    global _active_tasks
    with _active_tasks_lock:
        _active_tasks = max(0, _active_tasks - 1)



def _restart_server():
    """Full server restart — only used as fallback when hot-load fails."""
    def _do():
        import time as _t
        _t.sleep(1.5)
        host_run('systemctl restart ethos', timeout=10)
    threading.Thread(target=_do, daemon=True).start()



def _hot_load_blueprint(app_id):
    """Load an optional blueprint at runtime without server restart.
    Returns True if blueprint is ready (loaded or no backend needed).
    Returns False if loading failed (caller should fall back to restart).
    """
    import importlib
    import inspect

    bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
    if not bp_info:
        return True  # No backend needed (frontend-only / simple app)

    module_name, bp_var, init_fn, needs_sio = bp_info

    bp_file = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
    if not os.path.isfile(bp_file):
        return True  # No backend file on disk — frontend-only app

    if not _flask_app:
        log.warning('[app_manager] Flask app not available for hot-load')
        return False

    # Check if already loaded (module in cache + blueprint registered)
    mod_key = 'blueprints.' + module_name
    if mod_key in sys.modules:
        mod = sys.modules[mod_key]
        bp = getattr(mod, bp_var, None)
        if bp and bp.name in _flask_app.blueprints:
            log.info('[app_manager] Blueprint %s already loaded, skipping hot-load', bp.name)
            return True

    try:
        if mod_key in sys.modules:
            mod = importlib.reload(sys.modules[mod_key])
        else:
            mod = importlib.import_module(mod_key)

        bp = getattr(mod, bp_var)

        if needs_sio and _socketio:
            bp._socketio = _socketio

        # Skip registration if blueprint name already in app (e.g. bundled reload)
        if bp.name in _flask_app.blueprints:
            log.info('[app_manager] Blueprint %s already registered', bp.name)
            return True

        # Temporarily bypass Flask's first-request assertion to allow
        # runtime blueprint registration.  Gevent is cooperative so no
        # other greenlet can interleave between the flag flip.
        _flask_app._got_first_request = False
        try:
            _flask_app.register_blueprint(bp)
        finally:
            _flask_app._got_first_request = True

        if init_fn:
            fn = getattr(mod, init_fn, None)
            if fn:
                sig = inspect.signature(fn)
                if sig.parameters and _socketio:
                    fn(_socketio)
                else:
                    fn()

        log.info('[app_manager] Hot-loaded blueprint: %s', module_name)
        return True
    except Exception as e:
        log.error('[app_manager] Hot-load failed for %s: %s', module_name, e)
        return False



def _bg_install(app_id, app_def, task_id):
    def emit(extra):
        _emit({'task_id': task_id, 'app_id': app_id, **extra})

    _needs_restart = False
    _downloaded_frontend = None   # track newly downloaded files for cleanup on failure
    _downloaded_backend = []      # list of newly downloaded backend .py paths
    _task_start()
    try:
        emit({'stage': 'start', 'percent': 5, 'message': 'Instalowanie ' + app_def['name'] + '...', 'status': 'running'})

        # Determine source before downloading — was the app already on disk?
        _was_bundled = _is_bundled(app_id)
        app_base_url = _get_app_base_for_source(app_def)

        # Pobierz pliki z GitHub jesli nie ma na dysku
        if not _was_bundled:
            emit({'stage': 'download', 'percent': 10, 'message': 'Pobieranie pliku frontend...', 'status': 'running'})
            fns = _get_frontend_filenames(app_id)
            if fns:
                _downloaded_frontend = []
                for idx, fn in enumerate(fns):
                    remote_name = 'frontend.js' if idx == 0 else f'frontend_{idx + 1}.js'
                    url = app_base_url + '/' + app_id + '/' + remote_name
                    dest = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                    if not os.path.isfile(dest):
                        if not _download_file(url, dest):
                            emit({'stage': 'error', 'percent': 0, 'message': 'Bląd pobierania frontend', 'status': 'error'})
                            return
                        _downloaded_frontend.append(dest)
        else:
            emit({'stage': 'download', 'percent': 15, 'message': 'Pliki juz dostepne (bundled)', 'status': 'running'})

        # Download backend .py (primary + extras) from GitHub if not on disk
        # (Builder images keep frontend JS but remove optional backend .py)
        backend_modules = _get_backend_filenames(app_id)
        if backend_modules:
            for idx, module_name in enumerate(backend_modules):
                remote_name = 'backend.py' if idx == 0 else f'backend_{idx + 1}.py'
                bp_dest = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                if not os.path.isfile(bp_dest):
                    bp_url = app_base_url + '/' + app_id + '/' + remote_name
                    emit({'stage': 'download_backend', 'percent': 20, 'message': 'Pobieranie backend...', 'status': 'running'})
                    if not _download_file(bp_url, bp_dest):
                        emit({'stage': 'error', 'percent': 0, 'message': 'Bład pobierania backend — sprawdz połaczenie z internetem', 'status': 'error'})
                        return
                    _downloaded_backend.append(bp_dest)

        # Instalacja zaleznosci (apt: 25-42%, pip: 45-57%)
        apt_deps = app_def.get('apt_deps', [])
        pip_deps = app_def.get('pip_deps', [])

        if (apt_deps or pip_deps) and not _ensure_root_space(emit):
            return

        if apt_deps and not _install_apt_deps(apt_deps, emit):
            # Clean up freshly downloaded files — app isn't usable without its deps
            for p in (_downloaded_backend +
                      (_downloaded_frontend if isinstance(_downloaded_frontend, list) else
                       [_downloaded_frontend] if _downloaded_frontend else [])):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return

        if pip_deps and not _install_pip_deps(pip_deps, emit):
            for p in (_downloaded_backend +
                      (_downloaded_frontend if isinstance(_downloaded_frontend, list) else
                       [_downloaded_frontend] if _downloaded_frontend else [])):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return

        # Hot-load blueprint so its routes are available immediately
        emit({'stage': 'load', 'percent': 65, 'message': 'Ładowanie modułu...', 'status': 'running'})
        hot_ok = _hot_load_blueprint(app_id)
        emit({'stage': 'load', 'percent': 70, 'message': 'Moduł załadowany', 'status': 'running'})

        # Call app's install endpoint with timeout
        install_ep = app_def.get('install_endpoint')
        if install_ep and not app_def.get('simple') and _flask_app:
            emit({'stage': 'configure', 'percent': 75, 'message': 'Konfigurowanie apki...', 'status': 'running'})
            try:
                with gevent.Timeout(60, False):
                    with _flask_app.app_context():
                        with _flask_app.test_client() as tc:
                            resp = _internal_post(tc, install_ep)
                            if resp and resp.status_code >= 400:
                                log.warning('[app_manager] install_endpoint %s returned %s', install_ep, resp.status_code)
            except Exception as e:
                log.warning('[app_manager] install_endpoint %s failed: %s', install_ep, e)
            emit({'stage': 'configure', 'percent': 80, 'message': 'Konfiguracja zakonczona', 'status': 'running'})

        # Mark installed BEFORE frontend sync — rsync can disrupt SocketIO
        version = app_def.get('version', 'bundled')
        source = 'bundled' if _was_bundled else 'github'
        _set_installed(app_id, version, source,
                       apt_deps=app_def.get('apt_deps', []),
                       pip_deps=app_def.get('pip_deps', []))

        emit({'stage': 'done', 'percent': 100, 'message': app_def['name'] + ' zainstalowano pomyslnie', 'status': 'done'})

        # Notify all clients — enables hot-load without page refresh
        if _socketio:
            fns = _get_frontend_filenames(app_id)
            _socketio.emit('app_installed', {
                'id': app_id,
                'name': app_def.get('name', app_id),
                'icon': app_def.get('icon', 'fa-puzzle-piece'),
                'color': app_def.get('color', '#6b7280'),
                'category': app_def.get('category', 'Tools'),
                'description': app_def.get('description', ''),
                'admin_only': app_def.get('admin_only', False),
                'js_file': (fns[0] + '.js') if fns else None,
                'js_files': [fn + '.js' for fn in fns],
            })

        # Sync frontend_dist — non-critical cache sync, done after completion events
        gevent.sleep(0.1)
        try:
            _sync_frontend_dist()
        except Exception as e:
            log.warning('[app_manager] frontend sync error: %s', e)

        if not hot_ok:
            log.warning('[app_manager] Hot-load failed for %s, falling back to restart', app_id)
            _needs_restart = True
        else:
            _needs_restart = False

    except Exception as e:
        log.exception('[app_manager] install error for %s', app_id)
        try:
            emit({'stage': 'error', 'percent': 0, 'message': 'Bład: ' + str(e), 'status': 'error'})
        except Exception:
            pass
        _needs_restart = False
    finally:
        _task_done()
        if _needs_restart:
            _restart_server()



def _get_orphan_deps(app_id, app_def):
    """Return (apt_orphans, pip_orphans) — deps not needed by any other installed app."""
    installed = _load_installed()
    app_apt = set(app_def.get('apt_deps', []))
    app_pip = set(app_def.get('pip_deps', []))
    # Also check deps stored in installed_apps.json from the app being uninstalled
    stored = installed.get(app_id, {})
    app_apt |= set(stored.get('apt_deps', []))
    app_pip |= set(stored.get('pip_deps', []))
    if not app_apt and not app_pip:
        return [], []

    # Collect deps needed by other installed apps (from catalog + stored state)
    catalog_by_id = {a['id']: a for a in BUILTIN_CATALOG}
    needed_apt = set()
    needed_pip = set()
    for aid in installed:
        if aid == app_id:
            continue
        cat_entry = catalog_by_id.get(aid, {})
        stored_entry = installed.get(aid, {})
        needed_apt |= set(cat_entry.get('apt_deps', []))
        needed_apt |= set(stored_entry.get('apt_deps', []))
        needed_pip |= set(cat_entry.get('pip_deps', []))
        needed_pip |= set(stored_entry.get('pip_deps', []))

    return list(app_apt - needed_apt), list(app_pip - needed_pip)



def _remove_apt_deps(deps, emit_fn):
    """Remove orphaned APT packages."""
    if not deps:
        return
    # Only remove packages that are actually installed
    to_remove = []
    for pkg in deps:
        check = host_run(f'dpkg -l {q(pkg)} 2>/dev/null | grep -q "^ii"', timeout=10)
        if check.returncode == 0:
            to_remove.append(pkg)
    if not to_remove:
        return
    pkgs = ' '.join(q(d) for d in to_remove)
    log.info('[app_manager] Removing orphan apt deps: %s', to_remove)
    emit_fn({'stage': 'deps_cleanup', 'message': f'Usuwanie apt: {", ".join(to_remove)}', 'percent': 45, 'status': 'running'})
    cmd = f'DEBIAN_FRONTEND=noninteractive apt-get remove -y {pkgs} 2>&1'
    lock_file = '/tmp/ethos-apt.lock'
    wrapped = f"flock -w 180 {q(lock_file)} bash -lc {q(cmd)}"
    for line in host_run_stream(wrapped):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            rc = int(stripped.split(':', 1)[1])
            if rc != 0:
                log.warning('[app_manager] apt remove failed (rc=%s)', rc)
            break



def _remove_pip_deps(deps, emit_fn):
    """Remove orphaned pip packages."""
    if not deps:
        return
    venv = os.path.join(_ETHOS_ROOT, 'venv')
    pip = os.path.join(venv, 'bin', 'pip') if os.path.isdir(venv) else 'pip3'
    # Only remove packages that are actually installed
    to_remove = []
    for pkg in deps:
        pkg_name = pkg.split('==')[0].split('>=')[0].split('<=')[0].strip()
        check = host_run(f'{q(pip)} show {q(pkg_name)} 2>/dev/null | grep -q "^Name:"', timeout=10)
        if check.returncode == 0:
            to_remove.append(pkg_name)
    if not to_remove:
        return
    pkgs = ' '.join(q(d) for d in to_remove)
    log.info('[app_manager] Removing orphan pip deps: %s', to_remove)
    emit_fn({'stage': 'deps_cleanup', 'message': f'Usuwanie pip: {", ".join(to_remove)}', 'percent': 50, 'status': 'running'})
    cmd = f'{q(pip)} uninstall -y {pkgs} 2>&1'
    for line in host_run_stream(cmd):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            rc = int(stripped.split(':', 1)[1])
            if rc != 0:
                log.warning('[app_manager] pip uninstall failed (rc=%s)', rc)
            break



def _bg_uninstall(app_id, app_def, task_id, wipe_data=False):
    def emit(extra):
        _emit({'task_id': task_id, 'app_id': app_id, **extra})

    _task_start()
    try:
        emit({'stage': 'start', 'percent': 10, 'message': 'Odinstalowywanie ' + app_def['name'] + '...', 'status': 'running'})

        # Wywolaj wlasny endpoint uninstall
        uninstall_ep = app_def.get('uninstall_endpoint')
        if uninstall_ep and not app_def.get('simple') and _flask_app:
            emit({'stage': 'cleanup', 'percent': 30, 'message': 'Czyszczenie danych apki...', 'status': 'running'})
            try:
                with gevent.Timeout(60, False):
                    with _flask_app.app_context():
                        with _flask_app.test_client() as tc:
                            resp = _internal_post(tc, uninstall_ep, json={'wipe_data': wipe_data})
                            if resp and resp.status_code not in (200, 204):
                                log.warning('[app_manager] uninstall_endpoint %s returned %s', uninstall_ep, resp.status_code)
            except Exception as e:
                log.warning('[app_manager] uninstall_endpoint %s failed: %s', uninstall_ep, e)

        # Remove orphaned dependencies (apt/pip) not needed by other installed apps
        try:
            apt_orphans, pip_orphans = _get_orphan_deps(app_id, app_def)
            if apt_orphans or pip_orphans:
                emit({'stage': 'deps_cleanup', 'percent': 40, 'message': 'Usuwanie nieuzywanych zaleznosci...', 'status': 'running'})
                _remove_apt_deps(apt_orphans, emit)
                _remove_pip_deps(pip_orphans, emit)
        except Exception as e:
            log.warning('[app_manager] dep cleanup for %s failed: %s', app_id, e)

        # Remove files only for externally-downloaded apps, not bundled ones
        installed_info = _load_installed().get(app_id, {})
        was_external = installed_info.get('source') == 'github'

        emit({'stage': 'remove', 'percent': 60, 'message': 'Usuwanie plikow apki...', 'status': 'running'})
        if was_external:
            for fn in _get_frontend_filenames(app_id):
                # Don't remove shared frontend files used by core apps (e.g. storage.js)
                core_uses_same = any(
                    _get_frontend_filename(cid) == fn for cid in CORE_APPS
                )
                if not core_uses_same:
                    fp = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                    if os.path.isfile(fp):
                        os.remove(fp)
        # Remove backend files only for externally-downloaded apps
        if was_external:
            for idx, module_name in enumerate(_get_backend_filenames(app_id)):
                bp_file = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                # Primary file: don't remove if another installed app shares the same module
                if idx == 0:
                    other_using_same = [
                        aid for aid, bpi in _OPTIONAL_BLUEPRINTS.items()
                        if bpi[0] == module_name and aid != app_id
                        and aid in _load_installed()
                    ]
                    if other_using_same:
                        continue
                if os.path.isfile(bp_file):
                    os.remove(bp_file)
                    log.info('[app_manager] Removed backend file: %s', module_name)

        _set_uninstalled(app_id)

        emit({'stage': 'done', 'percent': 100, 'message': app_def['name'] + ' odinstalowano', 'status': 'done'})

        # Notify all clients — hot-remove from desktop without refresh
        if _socketio:
            _socketio.emit('app_uninstalled', {'id': app_id})

        # Sync frontend_dist — cache sync after events flushed
        gevent.sleep(0.1)
        try:
            _sync_frontend_dist()
        except Exception as e:
            log.warning('[app_manager] frontend sync error: %s', e)

    except Exception as e:
        log.exception('[app_manager] uninstall error for %s', app_id)
        try:
            emit({'stage': 'error', 'percent': 0, 'message': 'Bład: ' + str(e), 'status': 'error'})
        except Exception:
            pass
    finally:
        _task_done()


# ─── Auth helper ──────────────────────────────────────────────

