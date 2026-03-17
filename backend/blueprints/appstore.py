"""
EthOS — App Store (Container Library)
Browse, search, and install Docker apps from multiple CasaOS-compatible repositories.
Supports Big Bear, IceWhale CasaOS, LinuxServer, and custom repos.
"""

import os
import json
import subprocess
import re
import zipfile
import shutil
import yaml
import time
import threading
import uuid
import urllib.request
import sys
from flask import Blueprint, request, jsonify, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_base, NATIVE_MODE, data_path, check_dep, get_data_disk as _get_data_disk
from utils import load_json as _load_json, save_json as _save_json, run_host, \
    find_compose_project_names as _find_compose_project_names, \
    docker_available as _docker_available_util

appstore_bp = Blueprint('appstore', __name__, url_prefix='/api/appstore')


def _docker_available():
    """Check if Docker is installed and daemon is running."""
    if not check_dep('docker'):
        return False
    return _docker_available_util()

_socketio = None

def init_appstore(sio):
    """Set SocketIO reference for progress events."""
    global _socketio
    _socketio = sio

def _emit_install(event_data):
    if _socketio:
        _socketio.emit('appstore_install_progress', event_data)

# ─── Config ──────────────────────────────────────────────────

CACHE_DIR = '/tmp/appstore_cache'
CATALOG_FILE = os.path.join(CACHE_DIR, 'catalog.json')
REPOS_FILE = data_path('appstore_repos.json')
CACHE_MAX_AGE = 3600 * 6  # 6 hours

HOST_COMPOSE_ROOT = '/home/marcin/docker'
CONTAINER_COMPOSE_ROOT = '/home/marcin/docker'
APPDATA_ROOT = '/home/marcin/docker/_appdata'


def _apps_root():
    """Return the base directory for Docker app data on the data disk.
    Falls back to APPDATA_ROOT if no data disk is configured."""
    dd = _get_data_disk()
    if dd:
        p = os.path.join(dd, 'apps', '_appdata')
        os.makedirs(p, mode=0o755, exist_ok=True)
        return p
    return APPDATA_ROOT


def _compose_root():
    """Return the base directory for docker-compose project files.
    Falls back to HOST_COMPOSE_ROOT if no data disk is configured."""
    dd = _get_data_disk()
    if dd:
        p = os.path.join(dd, 'apps', 'compose')
        os.makedirs(p, mode=0o755, exist_ok=True)
        return p
    return HOST_COMPOSE_ROOT

_catalog_lock = threading.Lock()
_REPO_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,29}$')
_APP_DIR_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')


def _require_admin(require_sudo=False):
    if getattr(g, 'role', None) != 'admin':
        return jsonify({'error': 'Brak uprawnień'}), 403
    return None


def _normalize_repo_id(repo_id):
    rid = (repo_id or '').strip().lower()
    return rid if _REPO_ID_RE.match(rid) else None


def _safe_extract_zip(zip_path, target_dir):
    with zipfile.ZipFile(zip_path, 'r') as zf:
        base = os.path.realpath(target_dir)
        for info in zf.infolist():
            dest = os.path.realpath(os.path.join(base, info.filename))
            if not (dest == base or dest.startswith(base + os.sep)):
                raise ValueError('Invalid zip path traversal detected')
        zf.extractall(target_dir)


def _safe_compose_dir(app_id):
    dir_name = app_id.split('--')[0] if '--' in app_id else app_id
    if not _APP_DIR_RE.match(dir_name):
        return None, None
    base = os.path.realpath(_compose_root())
    target = os.path.realpath(os.path.join(base, dir_name))
    if not (target.startswith(base + os.sep)):
        return None, None
    return dir_name, target


def _validate_compose_policy(compose_text):
    """Reject dangerous compose options and host bind mounts outside allowed roots."""
    try:
        data = yaml.safe_load(compose_text)
    except Exception:
        return 'Nieprawidłowa składnia compose YAML'

    if not isinstance(data, dict):
        return 'Nieprawidłowy plik compose'
    services = data.get('services', {})
    if not isinstance(services, dict) or not services:
        return 'Compose nie zawiera sekcji services'

    allowed_roots = [os.path.realpath(_apps_root()), os.path.realpath(_compose_root())]

    def _path_allowed(p):
        rp = os.path.realpath(p)
        for root in allowed_roots:
            if rp == root or rp.startswith(root + os.sep):
                return True
        return False

    def _check_host_path(path):
        if not path:
            return None
        rp = os.path.realpath(path)
        if rp == '/var/run/docker.sock':
            return 'Bind mount docker.sock jest zabroniony'
        if not _path_allowed(path):
            return f'Bind mount poza dozwolonym zakresem: {path}'
        return None

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        if svc.get('privileged') is True:
            return f'Serwis {svc_name}: privileged=true jest zabronione'
        if svc.get('cap_add'):
            return f'Serwis {svc_name}: cap_add jest zabronione'
        if svc.get('devices'):
            return f'Serwis {svc_name}: devices jest zabronione'
        if str(svc.get('network_mode', '')).strip().lower() == 'host':
            return f'Serwis {svc_name}: network_mode=host jest zabronione'

        for vol in (svc.get('volumes') or []):
            if isinstance(vol, str):
                left = vol.split(':', 1)[0].strip()
                # Named docker volume (no slash) is safe.
                if left and ('/' not in left) and not left.startswith('.'):
                    continue
                host_path = left if left.startswith('/') else os.path.join(_compose_root(), left)
                err = _check_host_path(host_path)
                if err:
                    return f'Serwis {svc_name}: {err}'
            elif isinstance(vol, dict) and str(vol.get('type', '')).lower() == 'bind':
                err = _check_host_path(vol.get('source', ''))
                if err:
                    return f'Serwis {svc_name}: {err}'

    return None

# Default repositories
DEFAULT_REPOS = [
    {
        'id': 'big-bear',
        'name': 'Big Bear CasaOS',
        'url': 'https://github.com/bigbeartechworld/big-bear-casaos/archive/refs/heads/master.zip',
        'enabled': True,
        'icon': 'fa-paw',
        'color': '#f59e0b',
    },
    {
        'id': 'casaos-official',
        'name': 'CasaOS Official',
        'url': 'https://github.com/IceWhaleTech/CasaOS-AppStore/archive/refs/heads/main.zip',
        'enabled': False,
        'icon': 'fa-house-signal',
        'color': '#0ea5e9',
    },
    {
        'id': 'linuxserver',
        'name': 'LinuxServer.io',
        'url': 'https://github.com/WisdomSky/CasaOS-LinuxServer-AppStore/archive/refs/heads/main.zip',
        'enabled': False,
        'icon': 'fa-linux',
        'color': '#22c55e',
    },
    {
        'id': 'coolstore',
        'name': 'CasaOS Coolstore',
        'url': 'https://github.com/WisdomSky/CasaOS-Coolstore/archive/refs/heads/main.zip',
        'enabled': False,
        'icon': 'fa-snowflake',
        'color': '#06b6d4',
    },
    {
        'id': 'homeautomation',
        'name': 'Home Automation',
        'url': 'https://github.com/mr-manuel/CasaOS-HomeAutomation-AppStore/archive/refs/tags/latest.zip',
        'enabled': False,
        'icon': 'fa-house-chimney',
        'color': '#8b5cf6',
    },
]


# ─── Repo Management ────────────────────────────────────────

def _load_repos():
    """Load repo list from persistent file or return defaults."""
    repos = _load_json(REPOS_FILE, [])
    return repos if repos else [dict(r) for r in DEFAULT_REPOS]


def _save_repos(repos):
    """Save repos to persistent file."""
    _save_json(REPOS_FILE, repos)


def _get_enabled_repos():
    """Get only enabled repos."""
    return [r for r in _load_repos() if r.get('enabled')]


# ─── Helpers ─────────────────────────────────────────────────

def _run_host(cmd_str, timeout=120, cwd=None):
    """Run a command on the HOST."""
    return run_host(cmd_str, timeout=timeout, cwd=cwd)


def _run_host_stream(cmd_str, cwd=None, on_line=None, timeout=600):
    """Run a host command streaming stdout+stderr line by line."""
    from host import host_run_stream as _stream, q as _q
    if cwd:
        cmd_str = f"cd {_q(cwd)} && {cmd_str} 2>&1"
    else:
        cmd_str = f"{cmd_str} 2>&1"
    output_lines = []
    try:
        for line in _stream(cmd_str):
            line = line.rstrip('\n')
            if line.startswith('__EXIT_CODE__:'):
                rc = int(line.split(':')[1])
                return '\n'.join(output_lines), rc
            output_lines.append(line)
            if on_line:
                on_line(line)
    except Exception:
        output_lines.append('[ERROR]')
    return '\n'.join(output_lines), 1


def _download_repo(repo):
    """Download and extract a repo, return extracted root path."""
    repo_id = _normalize_repo_id(repo.get('id', ''))
    if not repo_id:
        raise ValueError('Invalid repo id')
    repo_dir = os.path.join(CACHE_DIR, 'repos', repo_id)
    zip_path = os.path.join(CACHE_DIR, f'{repo_id}.zip')

    os.makedirs(os.path.join(CACHE_DIR, 'repos'), exist_ok=True)

    urllib.request.urlretrieve(repo['url'], zip_path)

    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir)
    os.makedirs(repo_dir, exist_ok=True)

    _safe_extract_zip(zip_path, repo_dir)
    os.remove(zip_path)

    entries = os.listdir(repo_dir)
    if len(entries) == 1 and os.path.isdir(os.path.join(repo_dir, entries[0])):
        return os.path.join(repo_dir, entries[0])
    return repo_dir


def _parse_compose_metadata(compose_path):
    """Parse x-casaos metadata from a docker-compose.yml."""
    try:
        with open(compose_path, 'r') as f:
            data = yaml.safe_load(f)
        if not data:
            return None

        casaos = data.get('x-casaos', {})
        if not casaos:
            return None

        title_obj = casaos.get('title', {})
        title = title_obj.get('en_us', '') if isinstance(title_obj, dict) else str(title_obj)

        desc_obj = casaos.get('description', {})
        desc = desc_obj.get('en_us', '') if isinstance(desc_obj, dict) else str(desc_obj)

        tag_obj = casaos.get('tagline', {})
        tagline = tag_obj.get('en_us', '') if isinstance(tag_obj, dict) else str(tag_obj)

        icon = casaos.get('icon', '')
        category = casaos.get('category', 'Uncategorized')
        port_map = casaos.get('port_map', '')
        archs = casaos.get('architectures', [])
        developer = casaos.get('developer', '')
        main_service = casaos.get('main', '')
        thumbnail = casaos.get('thumbnail', '')

        image = ''
        services = data.get('services', {})
        if main_service and main_service in services:
            image = services[main_service].get('image', '')
        elif services:
            image = next(iter(services.values())).get('image', '')

        if '@sha256:' in image:
            image = image.split('@sha256:')[0]

        return {
            'title': title,
            'description': desc,
            'tagline': tagline,
            'icon': icon,
            'category': category,
            'port_map': str(port_map),
            'architectures': archs,
            'developer': developer,
            'image': image,
            'thumbnail': thumbnail,
        }
    except Exception:
        return None


def _build_catalog_for_repo(repo_root, repo_id, repo_name):
    """Build catalog entries from an extracted repo directory."""
    apps_dir = None
    for root, dirs, _files in os.walk(repo_root):
        if 'Apps' in dirs:
            apps_dir = os.path.join(root, 'Apps')
            break
    if not apps_dir:
        return []

    catalog = []
    for app_name in sorted(os.listdir(apps_dir)):
        app_path = os.path.join(apps_dir, app_name)
        if not os.path.isdir(app_path):
            continue

        compose_file = None
        for fname in ('docker-compose.yml', 'docker-compose.yaml'):
            p = os.path.join(app_path, fname)
            if os.path.isfile(p):
                compose_file = p
                break
        if not compose_file:
            continue

        meta = _parse_compose_metadata(compose_file)
        if not meta:
            continue

        config_path = os.path.join(app_path, 'config.json')
        version = ''
        if os.path.isfile(config_path):
            try:
                with open(config_path) as f:
                    version = json.load(f).get('version', '')
            except Exception:
                pass

        app_id = app_name.lower().replace(' ', '-')

        catalog.append({
            'id': app_id,
            'title': meta['title'] or app_name.replace('-', ' ').title(),
            'description': meta['description'],
            'tagline': meta['tagline'],
            'icon': meta['icon'],
            'category': meta['category'],
            'port_map': meta['port_map'],
            'architectures': meta['architectures'],
            'developer': meta['developer'],
            'image': meta['image'],
            'version': version,
            'compose_path': compose_file,
            'repo_id': repo_id,
            'repo_name': repo_name,
        })

    return catalog


def _build_full_catalog():
    """Build catalog from all enabled repos."""
    repos = _get_enabled_repos()
    all_apps = []
    errors = []

    for repo in repos:
        try:
            repo_root = _download_repo(repo)
            apps = _build_catalog_for_repo(repo_root, repo['id'], repo['name'])
            all_apps.extend(apps)
        except Exception as e:
            errors.append({'repo': repo['id'], 'error': str(e)})

    # Deduplicate — keep first, suffix duplicates with repo id
    seen = {}
    deduped = []
    for app in all_apps:
        key = app['id']
        if key not in seen:
            seen[key] = True
            deduped.append(app)
        else:
            suffixed = f"{app['id']}--{app['repo_id']}"
            if suffixed not in seen:
                seen[suffixed] = True
                app['id'] = suffixed
                deduped.append(app)

    return deduped, errors


def _get_catalog(force_refresh=False):
    """Get (possibly cached) catalog."""
    with _catalog_lock:
        if not force_refresh and os.path.isfile(CATALOG_FILE):
            mtime = os.path.getmtime(CATALOG_FILE)
            if time.time() - mtime < CACHE_MAX_AGE:
                try:
                    with open(CATALOG_FILE) as f:
                        return json.load(f)
                except Exception:
                    pass

        try:
            catalog, _errors = _build_full_catalog()
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(CATALOG_FILE, 'w') as f:
                json.dump(catalog, f)
            return catalog
        except Exception:
            if os.path.isfile(CATALOG_FILE):
                with open(CATALOG_FILE) as f:
                    return json.load(f)
            return []


def _get_installed_apps():
    """Get set of installed app directory names on host."""
    return _find_compose_project_names(_compose_root())


def _adapt_compose(compose_text, app_id):
    """Adapt a CasaOS compose file for our environment."""
    appdata = f'{_apps_root()}/{app_id}'
    text = compose_text.replace('/DATA/AppData/$AppID', appdata)
    text = re.sub(r'\$AppID', app_id, text)

    try:
        data = yaml.safe_load(text)
        if not data:
            return text
        data.pop('x-casaos', None)
        services = data.get('services', {})
        for svc_name in list(services.keys()):
            services[svc_name].pop('x-casaos', None)
        # Always remove top-level 'name' key — Docker Compose will use
        # the directory name as the project name, which is what the
        # project listing matches against.  Keeping a foreign name
        # causes the project to show as "Zatrzymany" (0/0 containers).
        data.pop('name', None)
        return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception:
        return text


# ═══════════════════════════════════════════════════════════
#  API Routes: Repos
# ═══════════════════════════════════════════════════════════

@appstore_bp.route('/repos')
def list_repos():
    """List all configured repositories."""
    return jsonify(_load_repos())


@appstore_bp.route('/repos', methods=['PUT'])
def update_repos():
    """Update the full repos list (reorder, enable/disable)."""
    deny = _require_admin()
    if deny:
        return deny
    data = request.get_json(force=True)
    repos = data if isinstance(data, list) else data.get('repos', [])
    if not repos:
        return jsonify({'error': 'repos list required'}), 400
    for r in repos:
        rid = _normalize_repo_id((r or {}).get('id', ''))
        if not rid:
            return jsonify({'error': 'Nieprawidłowe repo id'}), 400
        r['id'] = rid
    _save_repos(repos)
    return jsonify({'ok': True})


@appstore_bp.route('/repos', methods=['POST'])
def add_repo():
    """Add a new custom repository."""
    deny = _require_admin()
    if deny:
        return deny
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    name = data.get('name', '').strip()
    if not url:
        return jsonify({'error': 'url required'}), 400

    repo_id = data.get('id', '').strip()
    if not repo_id:
        repo_id = re.sub(r'[^a-z0-9]+', '-', (name or url.split('/')[-1]).lower()).strip('-')[:30]
    repo_id = _normalize_repo_id(repo_id)
    if not repo_id:
        return jsonify({'error': 'Nieprawidłowe repo id (dozwolone: a-z, 0-9, -)'}), 400
    if not name:
        name = repo_id.replace('-', ' ').title()

    repos = _load_repos()
    if any(r['id'] == repo_id for r in repos):
        return jsonify({'error': f'Repozytorium "{repo_id}" już istnieje'}), 409

    new_repo = {
        'id': repo_id,
        'name': name,
        'url': url,
        'enabled': True,
        'icon': data.get('icon', 'fa-box'),
        'color': data.get('color', '#94a3b8'),
    }

    # Test download before saving
    try:
        _download_repo(new_repo)
        repo_dir = os.path.join(CACHE_DIR, 'repos', repo_id)
        found_apps = False
        for root, dirs, _f in os.walk(repo_dir):
            if 'Apps' in dirs:
                found_apps = True
                break
        if not found_apps:
            return jsonify({'error': 'Repozytorium nie zawiera katalogu Apps/'}), 400
    except Exception as e:
        return jsonify({'error': f'Błąd pobierania: {str(e)}'}), 400

    repos.append(new_repo)
    _save_repos(repos)

    if os.path.isfile(CATALOG_FILE):
        os.remove(CATALOG_FILE)

    return jsonify({'ok': True, 'repo': new_repo})


@appstore_bp.route('/repos/<repo_id>', methods=['DELETE'])
def delete_repo(repo_id):
    """Remove a repository."""
    deny = _require_admin()
    if deny:
        return deny
    repo_id = _normalize_repo_id(repo_id)
    if not repo_id:
        return jsonify({'error': 'Nieprawidłowe repo id'}), 400
    repos = _load_repos()
    new_repos = [r for r in repos if r['id'] != repo_id]
    if len(new_repos) == len(repos):
        return jsonify({'error': 'Nie znaleziono'}), 404
    _save_repos(new_repos)

    repo_dir = os.path.join(CACHE_DIR, 'repos', repo_id)
    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir, ignore_errors=True)
    if os.path.isfile(CATALOG_FILE):
        os.remove(CATALOG_FILE)

    return jsonify({'ok': True})


@appstore_bp.route('/repos/<repo_id>/toggle', methods=['POST'])
def toggle_repo(repo_id):
    """Enable or disable a repository."""
    deny = _require_admin()
    if deny:
        return deny
    repo_id = _normalize_repo_id(repo_id)
    if not repo_id:
        return jsonify({'error': 'Nieprawidłowe repo id'}), 400
    repos = _load_repos()
    repo = next((r for r in repos if r['id'] == repo_id), None)
    if not repo:
        return jsonify({'error': 'Nie znaleziono'}), 404

    data = request.get_json(force=True) if request.data else {}
    repo['enabled'] = data.get('enabled', not repo.get('enabled'))
    _save_repos(repos)

    if os.path.isfile(CATALOG_FILE):
        os.remove(CATALOG_FILE)

    return jsonify({'ok': True, 'enabled': repo['enabled']})


# ═══════════════════════════════════════════════════════════
#  API Routes: Catalog & Install
# ═══════════════════════════════════════════════════════════

@appstore_bp.route('/catalog')
def catalog():
    """Return full app catalog from all enabled repos."""
    force = request.args.get('refresh', '').lower() in ('1', 'true')
    cat = _get_catalog(force_refresh=force)
    installed = _get_installed_apps()

    for app in cat:
        dir_name = app['id'].split('--')[0] if '--' in app['id'] else app['id']
        app['installed'] = dir_name in installed
        app.pop('compose_path', None)

    return jsonify(cat)


@appstore_bp.route('/catalog/refresh', methods=['POST'])
def refresh_catalog():
    """Force refresh the catalog from all enabled repos."""
    cat = _get_catalog(force_refresh=True)
    installed = _get_installed_apps()
    repos_used = set()
    for app in cat:
        app['installed'] = app['id'] in installed
        repos_used.add(app.get('repo_id', ''))
        app.pop('compose_path', None)
    return jsonify({'ok': True, 'count': len(cat), 'repos': len(repos_used)})


@appstore_bp.route('/app/<path:app_id>')
def app_detail(app_id):
    """Get details for a single app."""
    cat = _get_catalog()
    app = next((a for a in cat if a['id'] == app_id), None)
    if not app:
        return jsonify({'error': 'App not found'}), 404

    compose_path = app.get('compose_path', '')
    compose_content = ''
    if compose_path and os.path.isfile(compose_path):
        with open(compose_path) as f:
            compose_content = f.read()

    installed = _get_installed_apps()
    dir_name = app['id'].split('--')[0] if '--' in app['id'] else app['id']
    is_installed = dir_name in installed
    result = {**app, 'installed': is_installed, 'compose_raw': compose_content}
    result.pop('compose_path', None)
    return jsonify(result)


def _bg_install(task_id, app_id, app_title, adapted, dir_name, host_app_dir, container_app_dir):
    """Background: write compose, pull, up — emit progress via SocketIO."""
    try:
        # Step 1: Write compose file
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'prepare',
                        'percent': 5, 'message': 'Przygotowywanie plików…'})
        os.makedirs(container_app_dir, exist_ok=True)
        with open(os.path.join(container_app_dir, 'docker-compose.yml'), 'w') as f:
            f.write(adapted)
        _run_host(f'mkdir -p {_apps_root()}/{dir_name}')

        # Step 2: Pull images (streaming)
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'pull',
                        'percent': 10, 'message': 'Pobieranie obrazów Docker…'})

        pull_lines = []
        pull_pct = [10]   # mutable for closure

        def on_pull_line(line):
            pull_lines.append(line)
            # Advance progress from 10→80 based on docker pull output
            low = line.lower()
            if 'pulling' in low or 'download' in low or 'extract' in low or 'pull complete' in low:
                pull_pct[0] = min(pull_pct[0] + 2, 80)
            elif 'already exists' in low or 'digest' in low:
                pull_pct[0] = min(pull_pct[0] + 5, 80)
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'pull',
                            'percent': pull_pct[0], 'message': line[:120]})

        pull_output, pull_rc = _run_host_stream(
            'docker compose pull', cwd=host_app_dir, on_line=on_pull_line, timeout=600
        )
        if pull_rc != 0:
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                            'percent': 100, 'message': f'Błąd pobierania: {pull_output[-300:]}'})
            return

        # Step 3: Start containers
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'start',
                        'percent': 85, 'message': 'Uruchamianie kontenerów…'})

        def on_up_line(line):
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'start',
                            'percent': 90, 'message': line[:120]})

        up_output, up_rc = _run_host_stream(
            'docker compose up -d', cwd=host_app_dir, on_line=on_up_line, timeout=300
        )
        if up_rc != 0:
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                            'percent': 100, 'message': f'Błąd uruchamiania: {up_output[-300:]}'})
            return

        # Done!
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'done',
                        'percent': 100, 'message': f'{app_title} zainstalowana!'})

    except Exception as e:
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                        'percent': 100, 'message': f'Błąd: {str(e)}'})


@appstore_bp.route('/install', methods=['POST'])
def install_app():
    """Install an app: returns task_id immediately, work happens in background."""
    deny = _require_admin(require_sudo=True)
    if deny:
        return deny
    if not _docker_available():
        return jsonify({'error': 'Docker nie jest zainstalowany. Zainstaluj Docker w Menedżerze Docker.'}), 503
    data = request.json or {}
    app_id = data.get('app_id', '').strip()
    if not app_id:
        return jsonify({'error': 'app_id required'}), 400

    cat = _get_catalog()
    app = next((a for a in cat if a['id'] == app_id), None)
    if not app:
        return jsonify({'error': 'App not found'}), 404

    compose_override = data.get('compose_override', '').strip()

    if compose_override:
        adapted = compose_override
    else:
        compose_path = app.get('compose_path', '')
        if not compose_path or not os.path.isfile(compose_path):
            return jsonify({'error': 'Compose file missing'}), 500
        with open(compose_path) as f:
            raw = f.read()
        adapted = _adapt_compose(raw, app_id)

    dir_name, safe_dir = _safe_compose_dir(app_id)
    if not dir_name:
        return jsonify({'error': 'Nieprawidłowe app_id'}), 400
    host_app_dir = safe_dir
    container_app_dir = safe_dir
    app_title = app.get('title') or app_id
    task_id = str(uuid.uuid4())[:8]

    compose_error = _validate_compose_policy(adapted)
    if compose_error:
        return jsonify({'error': compose_error}), 400

    if _socketio:
        _socketio.start_background_task(
            _bg_install, task_id, app_id, app_title,
            adapted, dir_name, host_app_dir, container_app_dir
        )
    else:
        # Fallback: synchronous
        _bg_install(task_id, app_id, app_title,
                    adapted, dir_name, host_app_dir, container_app_dir)

    return jsonify({'ok': True, 'task_id': task_id, 'message': 'Installation started'})


@appstore_bp.route('/uninstall', methods=['POST'])
def uninstall_app():
    """Stop and remove an installed app."""
    deny = _require_admin(require_sudo=True)
    if deny:
        return deny
    if not _docker_available():
        return jsonify({'error': 'Docker nie jest zainstalowany.'}), 503
    data = request.json or {}
    app_id = data.get('app_id', '').strip()
    if not app_id:
        return jsonify({'error': 'app_id required'}), 400

    dir_name, safe_dir = _safe_compose_dir(app_id)
    if not dir_name:
        return jsonify({'error': 'Nieprawidłowe app_id'}), 400
    host_app_dir = safe_dir
    container_app_dir = safe_dir

    if not os.path.isdir(container_app_dir):
        return jsonify({'error': 'App not installed'}), 404

    out, err, rc = _run_host('docker compose down --remove-orphans', timeout=120, cwd=host_app_dir)

    try:
        shutil.rmtree(container_app_dir)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Compose down ok but cleanup failed: {e}'}), 500

    return jsonify({'ok': True, 'message': f'{app_id} uninstalled', 'stdout': out})


@appstore_bp.route('/compose/<path:app_id>')
def get_compose(app_id):
    """Get adapted compose file for preview."""
    cat = _get_catalog()
    app = next((a for a in cat if a['id'] == app_id), None)
    if not app:
        return jsonify({'error': 'App not found'}), 404

    compose_path = app.get('compose_path', '')
    if not compose_path or not os.path.isfile(compose_path):
        return jsonify({'error': 'Compose file missing'}), 500

    with open(compose_path) as f:
        raw = f.read()
    adapted = _adapt_compose(raw, app_id)
    return jsonify({'compose': adapted, 'compose_raw': raw})
