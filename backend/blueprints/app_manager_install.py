"""EthOS - App Manager Install/Uninstall

App lifecycle: install, uninstall, batch update, dependency management.
"""

import os
import json
import logging
import sys
import time
import threading

import gevent

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, host_run_stream, app_path, q

log = logging.getLogger('app_manager')

_ETHOS_ROOT = app_path()
_FRONTEND_APPS_DIR = os.path.join(_ETHOS_ROOT, 'frontend', 'js', 'apps')
_BLUEPRINTS_DIR = os.path.join(_ETHOS_ROOT, 'backend', 'blueprints')

_MIN_FREE_MB = 300
_active_tasks = 0
_active_tasks_lock = threading.Lock()


def _main():
    return sys.modules.get('blueprints.app_manager')


def _catalog():
    return sys.modules.get('blueprints.app_manager_catalog')


def _fileops():
    return sys.modules.get('blueprints.app_manager_fileops')


# ─── Runtime accessors ─────────────────────────────────────

def _emit(event_data):
    m = _main()
    fn = getattr(m, '_emit', None) if m else None
    if fn:
        fn(event_data)


def _get_socketio():
    m = _main()
    return getattr(m, '_socketio', None) if m else None


def _get_flask_app():
    m = _main()
    return getattr(m, '_flask_app', None) if m else None


def _hot_load_blueprint(app_id):
    m = _main()
    fn = getattr(m, '_hot_load_blueprint', None) if m else None
    return fn(app_id) if fn else True


def _load_installed():
    m = _fileops()
    return m._load_installed() if m else {}


def _save_installed(state):
    m = _fileops()
    if m:
        m._save_installed(state)


def _set_installed(app_id, version, source='bundled', apt_deps=None, pip_deps=None):
    m = _fileops()
    if m:
        m._set_installed(app_id, version, source, apt_deps=apt_deps, pip_deps=pip_deps)


def _set_uninstalled(app_id):
    m = _fileops()
    if m:
        m._set_uninstalled(app_id)


def _internal_post(tc, endpoint, **kwargs):
    m = _fileops()
    return m._internal_post(tc, endpoint, **kwargs) if m else None


def _get_frontend_filenames(app_id):
    m = _fileops()
    return m._get_frontend_filenames(app_id) if m else []


def _get_frontend_filename(app_id):
    m = _fileops()
    return m._get_frontend_filename(app_id) if m else app_id


def _get_backend_filenames(app_id):
    m = _fileops()
    return m._get_backend_filenames(app_id) if m else []


def _get_optional_blueprints():
    m = _main()
    return getattr(m, '_OPTIONAL_BLUEPRINTS', {}) if m else {}


def _get_core_apps():
    m = _main()
    return getattr(m, 'CORE_APPS', frozenset()) if m else frozenset()


def _get_builtin_catalog():
    m = _catalog()
    return getattr(m, 'BUILTIN_CATALOG', []) if m else []


def _get_catalog(force_refresh=False):
    m = _catalog()
    fn = getattr(m, '_get_catalog', None) if m else None
    return fn(force_refresh=force_refresh) if fn else []


def _get_app_base_for_source(app_def):
    m = _catalog()
    fn = getattr(m, '_get_app_base_for_source', None) if m else None
    return fn(app_def) if fn else ''


def _is_bundled(app_id):
    m = _catalog()
    fn = getattr(m, '_is_bundled', None) if m else None
    return fn(app_id) if fn else False


# ─── Download helper ──────────────────────────────────────────

def _download_file(url, dest_path):
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
        tmp = dest_path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(content)
        os.replace(tmp, dest_path)
        return True
    except Exception as e:
        log.error('[app_manager] Download failed %s: %s', url, e)
        return False


# ─── Disk space check ─────────────────────────────────────────

def _ensure_root_space(emit_fn):
    """Check root partition free space; proactively clean caches before install."""
    _PROACTIVE_CLEAN_MB = 600
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
            host_run('rm -rf /root/.cache/pip 2>/dev/null', timeout=10)
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


# ─── APT/pip dependency helpers ───────────────────────────────

def _install_apt_deps(deps, emit_fn):
    """Install APT dependencies with streaming progress updates."""
    if not deps:
        return True

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

    cmd = (
        f'DEBIAN_FRONTEND=noninteractive dpkg --configure -a 2>/dev/null; '
        f'DEBIAN_FRONTEND=noninteractive apt-get update -y -qq 2>/dev/null; '
        f'DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs} 2>&1'
    )
    lock_file = '/var/lock/ethos-apt.lock'
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

    venv = os.path.join(_ETHOS_ROOT, 'venv')
    pip = os.path.join(venv, 'bin', 'pip') if os.path.isdir(venv) else 'pip3'
    python = os.path.join(venv, 'bin', 'python') if os.path.isdir(venv) else 'python3'

    missing = []
    for pkg in deps:
        pkg_name = pkg.split('==')[0].split('>=')[0].split('<=')[0].strip()
        check = host_run(f'{q(python)} -c "import importlib; importlib.import_module({q(pkg_name.replace("-","_"))})" 2>/dev/null', timeout=10)
        if check.returncode != 0:
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


# ─── Frontend sync + task counter ────────────────────────────

def _sync_frontend_dist():
    frontend = os.path.join(_ETHOS_ROOT, 'frontend')
    dist = os.path.join(_ETHOS_ROOT, 'frontend_dist')
    if os.path.isdir(dist):
        host_run('rsync -a --delete ' + q(frontend + '/') + ' ' + q(dist + '/'), timeout=60)
    app_mod = sys.modules.get('app')
    if app_mod:
        cache = getattr(app_mod, '_INDEX_CACHE', None)
        if cache:
            cache['html'] = None


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


# ─── Background install ───────────────────────────────────────

def _bg_install(app_id, app_def, task_id):
    def emit(extra):
        _emit({'task_id': task_id, 'app_id': app_id, **extra})

    _needs_restart = False
    _downloaded_frontend = None
    _downloaded_backend = []
    _task_start()
    try:
        emit({'stage': 'start', 'percent': 5, 'message': 'Instalowanie ' + app_def['name'] + '...', 'status': 'running'})

        _was_bundled = _is_bundled(app_id)
        app_base_url = _get_app_base_for_source(app_def)

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

        apt_deps = app_def.get('apt_deps', [])
        pip_deps = app_def.get('pip_deps', [])

        if (apt_deps or pip_deps) and not _ensure_root_space(emit):
            return

        if apt_deps and not _install_apt_deps(apt_deps, emit):
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

        emit({'stage': 'load', 'percent': 65, 'message': 'Ładowanie modułu...', 'status': 'running'})
        hot_ok = _hot_load_blueprint(app_id)
        emit({'stage': 'load', 'percent': 70, 'message': 'Moduł załadowany', 'status': 'running'})

        install_ep = app_def.get('install_endpoint')
        flask_app = _get_flask_app()
        if install_ep and not app_def.get('simple') and flask_app:
            emit({'stage': 'configure', 'percent': 75, 'message': 'Konfigurowanie apki...', 'status': 'running'})
            try:
                with gevent.Timeout(60, False):
                    with flask_app.app_context():
                        with flask_app.test_client() as tc:
                            resp = _internal_post(tc, install_ep)
                            if resp and resp.status_code >= 400:
                                log.warning('[app_manager] install_endpoint %s returned %s', install_ep, resp.status_code)
            except Exception as e:
                log.warning('[app_manager] install_endpoint %s failed: %s', install_ep, e)
            emit({'stage': 'configure', 'percent': 80, 'message': 'Konfiguracja zakonczona', 'status': 'running'})

        version = app_def.get('version', 'bundled')
        source = 'bundled' if _was_bundled else 'github'
        _set_installed(app_id, version, source,
                       apt_deps=app_def.get('apt_deps', []),
                       pip_deps=app_def.get('pip_deps', []))

        emit({'stage': 'done', 'percent': 100, 'message': app_def['name'] + ' zainstalowano pomyslnie', 'status': 'done'})

        socketio = _get_socketio()
        if socketio:
            fns = _get_frontend_filenames(app_id)
            socketio.emit('app_installed', {
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


# ─── Orphan dependency detection ─────────────────────────────

def _get_orphan_deps(app_id, app_def):
    """Return (apt_orphans, pip_orphans) — deps not needed by any other installed app."""
    installed = _load_installed()
    app_apt = set(app_def.get('apt_deps', []))
    app_pip = set(app_def.get('pip_deps', []))
    stored = installed.get(app_id, {})
    app_apt |= set(stored.get('apt_deps', []))
    app_pip |= set(stored.get('pip_deps', []))
    if not app_apt and not app_pip:
        return [], []

    catalog_by_id = {a['id']: a for a in _get_builtin_catalog()}
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


# ─── Remove dep helpers ───────────────────────────────────────

def _remove_apt_deps(deps, emit_fn):
    """Remove orphaned APT packages."""
    if not deps:
        return
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
    lock_file = '/var/lock/ethos-apt.lock'
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


# ─── Background uninstall ─────────────────────────────────────

def _bg_uninstall(app_id, app_def, task_id, wipe_data=False):
    def emit(extra):
        _emit({'task_id': task_id, 'app_id': app_id, **extra})

    _task_start()
    try:
        emit({'stage': 'start', 'percent': 10, 'message': 'Odinstalowywanie ' + app_def['name'] + '...', 'status': 'running'})

        uninstall_ep = app_def.get('uninstall_endpoint')
        flask_app = _get_flask_app()
        if uninstall_ep and not app_def.get('simple') and flask_app:
            emit({'stage': 'cleanup', 'percent': 30, 'message': 'Czyszczenie danych apki...', 'status': 'running'})
            try:
                with gevent.Timeout(60, False):
                    with flask_app.app_context():
                        with flask_app.test_client() as tc:
                            resp = _internal_post(tc, uninstall_ep, json={'wipe_data': wipe_data})
                            if resp and resp.status_code not in (200, 204):
                                log.warning('[app_manager] uninstall_endpoint %s returned %s', uninstall_ep, resp.status_code)
            except Exception as e:
                log.warning('[app_manager] uninstall_endpoint %s failed: %s', uninstall_ep, e)

        try:
            apt_orphans, pip_orphans = _get_orphan_deps(app_id, app_def)
            if apt_orphans or pip_orphans:
                emit({'stage': 'deps_cleanup', 'percent': 40, 'message': 'Usuwanie nieuzywanych zaleznosci...', 'status': 'running'})
                _remove_apt_deps(apt_orphans, emit)
                _remove_pip_deps(pip_orphans, emit)
        except Exception as e:
            log.warning('[app_manager] dep cleanup for %s failed: %s', app_id, e)

        installed_info = _load_installed().get(app_id, {})
        was_external = installed_info.get('source') == 'github'

        emit({'stage': 'remove', 'percent': 60, 'message': 'Usuwanie plikow apki...', 'status': 'running'})
        CORE_APPS = _get_core_apps()
        if was_external:
            for fn in _get_frontend_filenames(app_id):
                core_uses_same = any(
                    _get_frontend_filename(cid) == fn for cid in CORE_APPS
                )
                if not core_uses_same:
                    fp = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                    if os.path.isfile(fp):
                        os.remove(fp)

        _OPTIONAL_BLUEPRINTS = _get_optional_blueprints()
        if was_external:
            for idx, module_name in enumerate(_get_backend_filenames(app_id)):
                bp_file = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
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

        socketio = _get_socketio()
        if socketio:
            socketio.emit('app_uninstalled', {'id': app_id})

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


# ─── Ensure installed + repair ────────────────────────────────

def _ensure_installed_apps():
    """Ensure installed_apps.json contains all on-disk optional apps."""
    from datetime import datetime
    installed = _load_installed()
    changed = False
    now = datetime.utcnow().isoformat()

    catalog_by_id = {a['id']: a for a in _get_builtin_catalog()}
    _OPTIONAL_BLUEPRINTS = _get_optional_blueprints()

    for app_id, bp_info in _OPTIONAL_BLUEPRINTS.items():
        module_name = bp_info[0]
        local_py = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
        if not (os.path.isfile(local_py) and os.path.getsize(local_py) > 0):
            continue

        cat_entry = catalog_by_id.get(app_id, {})

        if app_id not in installed:
            has_deps = bool(cat_entry.get('apt_deps') or cat_entry.get('pip_deps'))
            if has_deps:
                continue
            installed[app_id] = {
                'version': 'bundled',
                'source': 'bundled',
                'installed_at': now,
            }
            changed = True
            log.info('[app_manager] Auto-registered on-disk app: %s', app_id)

        cat_ver = catalog_by_id.get(app_id, {}).get('version', '')
        inst_ver = installed[app_id].get('version', '')
        if cat_ver and inst_ver in ('bundled', 'core') or (cat_ver and inst_ver and cat_ver > inst_ver):
            installed[app_id]['version'] = cat_ver
            changed = True

    if changed:
        _save_installed(installed)
    return installed


def _repair_missing_app_files():
    """Auto-repair: re-download missing frontend/backend files for installed apps."""
    try:
        installed = _load_installed()
        if not installed:
            return
        catalog = _get_catalog()
        catalog_map = {a['id']: a for a in catalog}
        repaired = []
        CORE_APPS = _get_core_apps()

        for app_id in list(installed):
            if app_id in CORE_APPS:
                continue
            fns = _get_frontend_filenames(app_id)
            if not fns:
                continue
            app_def = catalog_map.get(app_id, {})
            base_url = _get_app_base_for_source(app_def)
            for idx, fn in enumerate(fns):
                remote_name = 'frontend.js' if idx == 0 else f'frontend_{idx + 1}.js'
                js_path = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                if not os.path.isfile(js_path):
                    url = base_url + '/' + app_id + '/' + remote_name
                    if _download_file(url, js_path):
                        repaired.append(f'{app_id}/{remote_name}')
                        dist_path = os.path.join(_ETHOS_ROOT, 'frontend_dist', 'js', 'apps', fn + '.js')
                        if os.path.isdir(os.path.dirname(dist_path)):
                            try:
                                import shutil
                                shutil.copy2(js_path, dist_path)
                            except Exception:
                                pass
            for idx, module_name in enumerate(_get_backend_filenames(app_id)):
                remote_name = 'backend.py' if idx == 0 else f'backend_{idx + 1}.py'
                bp_path = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                if not os.path.isfile(bp_path):
                    url = base_url + '/' + app_id + '/' + remote_name
                    if _download_file(url, bp_path):
                        repaired.append(f'{app_id}/{remote_name}')

        if repaired:
            log.info('[app_manager] Auto-repaired %d missing files: %s', len(repaired), ', '.join(repaired))
        else:
            log.debug('[app_manager] Integrity check OK — no missing files')
    except Exception as e:
        log.warning('[app_manager] Auto-repair error: %s', e)


# ─── Background update ────────────────────────────────────────

def _bg_update_apps(app_ids, base_url, task_id, source='ota'):
    """Background: download updated files and hot-reload."""
    def emit(extra):
        _emit({'task_id': task_id, **extra})

    _task_start()
    total = len(app_ids)
    updated = []
    failed = []

    try:
        emit({'stage': 'start', 'percent': 2, 'status': 'running',
              'message': f'Aktualizacja {total} aplikacji ({source})...'})

        _OPTIONAL_BLUEPRINTS = _get_optional_blueprints()

        for idx, app_id in enumerate(app_ids):
            pct_base = int(5 + (idx / total) * 85)
            emit({'stage': 'updating', 'percent': pct_base, 'app_id': app_id,
                  'status': 'running',
                  'message': f'Aktualizacja {app_id} ({idx+1}/{total})...'})

            bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
            if not bp_info:
                log.warning('[app_manager] Unknown app for update: %s', app_id)
                failed.append(app_id)
                continue

            ok = True

            emit({'stage': 'updating', 'percent': pct_base + 2, 'app_id': app_id,
                  'status': 'running', 'message': f'{app_id}: pobieranie backend...'})
            for b_idx, module_name in enumerate(_get_backend_filenames(app_id)):
                remote_name = 'backend.py' if b_idx == 0 else f'backend_{b_idx + 1}.py'
                bp_url = base_url + f'/{app_id}/{remote_name}'
                bp_dest = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                if not _download_file(bp_url, bp_dest):
                    log.warning('[app_manager] Backend download failed: %s/%s', app_id, remote_name)
                    ok = False
                    break

            for f_idx, fn in enumerate(_get_frontend_filenames(app_id)):
                remote_name = 'frontend.js' if f_idx == 0 else f'frontend_{f_idx + 1}.js'
                js_url = base_url + f'/{app_id}/{remote_name}'
                js_dest = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                emit({'stage': 'updating', 'percent': pct_base + 4, 'app_id': app_id,
                      'status': 'running', 'message': f'{app_id}: pobieranie frontend...'})
                if not _download_file(js_url, js_dest):
                    log.warning('[app_manager] Frontend download failed: %s', app_id)
                    ok = False

            if ok:
                emit({'stage': 'updating', 'percent': pct_base + 6, 'app_id': app_id,
                      'status': 'running', 'message': f'{app_id}: ładowanie...'})
                _hot_load_blueprint(app_id)
                cat_entry = next((a for a in _get_builtin_catalog() if a['id'] == app_id), {})
                ver = cat_entry.get('version', 'latest')
                _set_installed(app_id, ver, source)
                updated.append(app_id)
            else:
                failed.append(app_id)

        msg = f'Zaktualizowano {len(updated)} aplikacji'
        if failed:
            msg += f', {len(failed)} błędów'
        emit({'stage': 'done', 'percent': 100, 'status': 'done',
              'message': msg, 'updated': updated, 'failed': failed})

        if updated:
            gevent.sleep(0.1)
            try:
                _sync_frontend_dist()
            except Exception as e:
                log.warning('[app_manager] frontend sync error: %s', e)

    except Exception as e:
        log.exception('[app_manager] App update error')
        emit({'stage': 'error', 'percent': 0, 'status': 'error',
              'message': f'Błąd: {e}'})
    finally:
        _task_done()
