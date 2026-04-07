"""
EthOS — [App Name] Blueprint
[One-line description of what this app does]

Endpoints:
  GET  /api/myapp/status              — check install status and current config
  POST /api/myapp/install             — install dependencies (SocketIO: myapp_install)
  POST /api/myapp/uninstall           — remove app and clean up
  GET  /api/myapp/config              — get current configuration
  PUT  /api/myapp/config              — update configuration
  POST /api/myapp/migrate-db          — migrate data from root to data partition
  GET  /api/myapp/pkg-status          — App Store integration status

SocketIO events emitted:
  myapp_install   {task_id, stage, percent, message}
  myapp_progress  {task_id, percent, message}
"""

# ─── Standard imports ─────────────────────────────────────────────────────────
import os
import re
import json
import shutil
import threading
import time
from flask import Blueprint, jsonify, request

# ─── EthOS HAL ────────────────────────────────────────────────────────────────
from host import host_run, data_path, q, apt_install, get_data_disk

# ─── Auth ─────────────────────────────────────────────────────────────────────
from blueprints.admin_required import admin_required

# ─── Blueprint registration ───────────────────────────────────────────────────
myapp_bp = Blueprint('myapp', __name__, url_prefix='/api/myapp')

# ─── Constants ────────────────────────────────────────────────────────────────
# Small config/state files → data_path() (symlinked to data partition)
_CONFIG_FILE = data_path('myapp_config.json')
_STATE_FILE  = data_path('myapp_state.json')

# ─── Data directory ───────────────────────────────────────────────────────────
# RULE: Large data (databases, downloads, indexes) MUST go to data partition.
# Use get_data_disk() for paths outside the ethos/ subdir (e.g. /mnt/data/myapp).
# Use data_path() for small state files (they are on the data partition via symlink).
_DATA_DIR_DEFAULT = '/var/lib/myapp'  # only if apt pkg uses this


def _myapp_data_dir():
    """Return data directory — prefers /mnt/data/myapp, falls back to /var/lib/myapp."""
    dd = get_data_disk()
    if dd:
        p = os.path.join(dd, 'myapp')
        os.makedirs(p, exist_ok=True)
        return p
    return _DATA_DIR_DEFAULT


# ─── Migration (existing installs) ────────────────────────────────────────────
_migrated = False


def _do_migration():
    """Move existing data from /var/lib/myapp to data partition if needed.
    Called automatically on import and via POST /api/myapp/migrate-db.
    Safe to call multiple times."""
    global _migrated
    if _migrated:
        return {'skipped': True}
    _migrated = True

    target = _myapp_data_dir()
    if target == _DATA_DIR_DEFAULT:
        return {'skipped': True, 'reason': 'no data disk'}

    src = _DATA_DIR_DEFAULT
    if not os.path.isdir(src):
        return {'skipped': True, 'reason': 'source missing'}

    # Check if already migrated (conf points to target)
    # Replace this check with the actual config file check for your app
    conf = '/etc/myapp/myapp.conf'
    if os.path.isfile(conf) and target in open(conf).read():
        return {'skipped': True, 'reason': 'already configured'}

    moved, errors = 0, []
    try:
        host_run('systemctl stop myapp 2>/dev/null', timeout=10)
    except Exception:
        pass
    try:
        for fname in os.listdir(src):
            src_f = os.path.join(src, fname)
            dst_f = os.path.join(target, fname)
            if os.path.isfile(src_f) and not os.path.exists(dst_f):
                shutil.move(src_f, dst_f)
                moved += 1
    except Exception as e:
        errors.append(str(e))

    # Patch config to use new dir — adapt regex to your app's conf format
    if os.path.isfile(conf):
        try:
            txt = open(conf).read()
            txt = re.sub(r'^DataDirectory\s.*$', f'DataDirectory {target}',
                         txt, flags=re.MULTILINE)
            if 'DataDirectory' not in txt:
                txt += f'\nDataDirectory {target}\n'
            open(conf, 'w').write(txt)
        except Exception as e:
            errors.append(f'conf patch: {e}')

    try:
        host_run(f'chown -R myapp:myapp {q(target)} 2>/dev/null', timeout=10)
        host_run('systemctl start myapp 2>/dev/null', timeout=10)
    except Exception:
        pass

    return {'moved': moved, 'errors': errors, 'target': target}


# Auto-migrate on import (best-effort, won't crash if app not installed)
try:
    _do_migration()
except Exception:
    pass


# ─── Config helpers ───────────────────────────────────────────────────────────
_DEFAULT_CONFIG = {
    'enabled': False,
    'some_setting': 'default_value',
}


def _load_config():
    try:
        if os.path.isfile(_CONFIG_FILE):
            with open(_CONFIG_FILE) as f:
                return {**_DEFAULT_CONFIG, **json.load(f)}
    except Exception:
        pass
    return dict(_DEFAULT_CONFIG)


def _save_config(cfg):
    with open(_CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


# ─── Install check ────────────────────────────────────────────────────────────
def _is_installed():
    """Return True if the apt package is installed."""
    r = host_run("dpkg -s myapp 2>/dev/null | grep -q 'Status: install ok installed'", timeout=10)
    return r.returncode == 0


# ─── Routes ──────────────────────────────────────────────────────────────────

@myapp_bp.route('/pkg-status')
@admin_required
def pkg_status():
    """App Store integration: installed status."""
    installed = _is_installed()
    return jsonify({'installed': installed, 'status': 'active' if installed else 'not_installed'})


@myapp_bp.route('/status')
@admin_required
def status():
    """Current install status and configuration."""
    installed = _is_installed()
    cfg = _load_config()
    data_dir = _myapp_data_dir()
    return jsonify({
        'installed': installed,
        'config': cfg,
        'data_dir': data_dir,
        'data_on_data_partition': data_dir != _DATA_DIR_DEFAULT,
    })


@myapp_bp.route('/install', methods=['POST'])
@admin_required
def install():
    """Install the app. Emits SocketIO events: myapp_install."""
    task_id  = __import__('secrets').token_hex(8)
    _socketio = getattr(myapp_bp, '_socketio', None)

    def _emit(stage, pct, msg):
        if _socketio:
            _socketio.emit('myapp_install', {
                'task_id': task_id, 'stage': stage, 'percent': pct, 'message': msg,
            })

    def _bg():
        _emit('start', 5, 'Instalacja...')
        try:
            r = apt_install('myapp', timeout=300)
            if r.returncode != 0:
                _emit('error', 0, 'Błąd instalacji: ' + (r.stderr or '')[:300])
                return

            # Redirect data to data partition
            dd = get_data_disk()
            if dd:
                _emit('progress', 60, 'Konfigurowanie katalogu danych...')
                data_dir = _myapp_data_dir()
                conf = '/etc/myapp/myapp.conf'
                if os.path.isfile(conf):
                    txt = open(conf).read()
                    txt = re.sub(r'^DataDirectory\s.*$', f'DataDirectory {data_dir}',
                                 txt, flags=re.MULTILINE)
                    if 'DataDirectory' not in txt:
                        txt += f'\nDataDirectory {data_dir}\n'
                    open(conf, 'w').write(txt)
                host_run(f'chown -R myapp:myapp {q(data_dir)} 2>/dev/null', timeout=10)

            _emit('progress', 80, 'Uruchamianie usługi...')
            host_run('systemctl enable --now myapp', timeout=15)
            _emit('done', 100, 'Zainstalowano!')
        except Exception as e:
            _emit('error', 0, str(e))

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


@myapp_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall():
    """Uninstall the app."""
    host_run('systemctl stop myapp 2>/dev/null; apt-get remove -y myapp 2>/dev/null || true', timeout=60)
    return jsonify({'ok': True})


@myapp_bp.route('/migrate-db', methods=['POST'])
@admin_required
def migrate_db():
    """Manually trigger migration of data from root to data partition."""
    global _migrated
    _migrated = False  # force re-run
    result = _do_migration()
    return jsonify({'ok': True, **result})


@myapp_bp.route('/config')
@admin_required
def get_config():
    return jsonify(_load_config())


@myapp_bp.route('/config', methods=['PUT'])
@admin_required
def update_config():
    data = request.get_json(force=True, silent=True) or {}
    cfg = _load_config()
    for key in _DEFAULT_CONFIG:
        if key in data:
            cfg[key] = data[key]
    _save_config(cfg)
    return jsonify({'ok': True, 'config': cfg})
