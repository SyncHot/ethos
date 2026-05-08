"""EthOS - App Manager File Operations

Handles installed state persistence and file path resolution.
"""

import os
import json
import logging
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path, app_path

log = logging.getLogger('app_manager')

INSTALLED_FILE = data_path('installed_apps.json')

_ETHOS_ROOT = app_path()
_FRONTEND_APPS_DIR = os.path.join(_ETHOS_ROOT, 'frontend', 'js', 'apps')
_BLUEPRINTS_DIR = os.path.join(_ETHOS_ROOT, 'backend', 'blueprints')

_state_lock = threading.RLock()


def _main():
    return sys.modules.get('blueprints.app_manager')


def _load_installed():
    with _state_lock:
        try:
            if os.path.isfile(INSTALLED_FILE):
                with open(INSTALLED_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}


# Public alias so app.py can import without touching private names
load_installed = _load_installed


def _save_installed(state):
    with _state_lock:
        tmp = INSTALLED_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, INSTALLED_FILE)


def _set_installed(app_id, version, source='bundled', apt_deps=None, pip_deps=None):
    from datetime import datetime
    state = _load_installed()
    entry = {
        'version': version,
        'source': source,
        'installed_at': datetime.utcnow().isoformat(),
    }
    if apt_deps:
        entry['apt_deps'] = list(apt_deps)
    if pip_deps:
        entry['pip_deps'] = list(pip_deps)
    state[app_id] = entry
    _save_installed(state)


def _get_internal_token():
    """Return a valid admin token from tokens.db for internal test_client calls."""
    try:
        import sqlite3 as _sq
        with _sq.connect(data_path('tokens.db')) as _conn:
            _row = _conn.execute(
                "SELECT token FROM tokens WHERE role='admin' AND expires > strftime('%s','now') "
                "ORDER BY expires DESC LIMIT 1"
            ).fetchone()
        return _row[0] if _row else None
    except Exception as e:
        log.warning('[app_manager] Could not fetch internal token: %s', e)
        return None


def _internal_post(tc, endpoint, **kwargs):
    """POST to an internal endpoint with an admin token."""
    token = _get_internal_token()
    headers = kwargs.pop('headers', {})
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return tc.post(endpoint, headers=headers, **kwargs)


def _set_uninstalled(app_id):
    state = _load_installed()
    state.pop(app_id, None)
    _save_installed(state)
    _clear_legacy_pkg(app_id)


def _clear_legacy_pkg(app_id):
    """Remove app_id from ethos_packages.json (legacy state file)."""
    try:
        legacy = data_path('ethos_packages.json')
        if not os.path.isfile(legacy):
            return
        with open(legacy) as f:
            state = json.load(f)
        if app_id in state:
            state[app_id] = {'installed': False, 'installed_at': ''}
            with open(legacy, 'w') as f:
                json.dump(state, f, indent=2)
    except Exception as e:
        log.warning('[app_manager] Could not clear legacy pkg state for %s: %s', app_id, e)


def migrate_from_ethos_packages():
    """Jednorazowa migracja: wczytaj ethos_packages.json -> installed_apps.json.
    Wywolywana przy starcie serwera z app.py."""
    if os.path.isfile(INSTALLED_FILE):
        return

    from datetime import datetime
    now = datetime.utcnow().isoformat()

    old_file = data_path('ethos_packages.json')
    new_state = {}

    if os.path.isfile(old_file):
        try:
            with open(old_file) as f:
                old_state = json.load(f)
            for pkg_id, pkg_info in old_state.items():
                if isinstance(pkg_info, dict) and pkg_info.get('installed'):
                    new_state[pkg_id] = {
                        'version': 'bundled',
                        'source': 'bundled',
                        'installed_at': pkg_info.get('installed_at', now),
                    }
        except Exception as e:
            log.warning('Migration from ethos_packages.json failed: %s', e)

    m_cat = sys.modules.get('blueprints.app_manager_catalog')
    builtin = getattr(m_cat, 'BUILTIN_CATALOG', []) if m_cat else []

    for app in builtin:
        aid = app['id']
        if aid in new_state:
            continue
        fn = _get_frontend_filename(aid)
        if fn is None:
            new_state[aid] = {'version': 'bundled', 'source': 'bundled', 'installed_at': now}
            continue
        fp = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
            new_state[aid] = {'version': 'bundled', 'source': 'bundled', 'installed_at': now}

    _save_installed(new_state)
    log.info('[app_manager] Migrated %d packages to installed_apps.json', len(new_state))


def _get_frontend_filename(app_id):
    m = _main()
    fn_map = getattr(m, '_FRONTEND_FILENAME', {}) if m else {}
    if app_id in fn_map:
        return fn_map[app_id]
    return app_id


def _get_frontend_filenames(app_id):
    """Return list of ALL JS filenames (without .js) for an app.
    First element is the primary file; remaining are extras defined in
    _FRONTEND_EXTRA_FILES.  Returns [] when the app lives in apps.js (fn=None)."""
    m = _main()
    extra_map = getattr(m, '_FRONTEND_EXTRA_FILES', {}) if m else {}
    primary = _get_frontend_filename(app_id)
    if primary is None:
        return []
    return [primary] + list(extra_map.get(app_id, []))


def _get_backend_filenames(app_id):
    """Return list of ALL backend module names (without .py) for an app.
    First element is the primary module from _OPTIONAL_BLUEPRINTS; remaining
    are extras defined in _BACKEND_EXTRA_FILES.  Returns [] when the app has
    no optional blueprint."""
    m = _main()
    opt_bp = getattr(m, '_OPTIONAL_BLUEPRINTS', {}) if m else {}
    extra_map = getattr(m, '_BACKEND_EXTRA_FILES', {}) if m else {}
    bp_info = opt_bp.get(app_id)
    if not bp_info:
        return []
    return [bp_info[0]] + list(extra_map.get(app_id, []))
