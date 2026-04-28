"""
EthOS — Package Registry Blueprint
Git-based package manager (like Synology Package Center)

Endpoints:
  GET  /api/pkg-registry/repos               — list configured repos
  POST /api/pkg-registry/repos               — add repo
  DEL  /api/pkg-registry/repos/<id>          — remove repo
  PUT  /api/pkg-registry/repos/<id>/toggle   — enable/disable repo
  GET  /api/pkg-registry/catalog             — merged catalog from all active repos
  POST /api/pkg-registry/catalog/refresh     — git pull all repos
  GET  /api/pkg-registry/script/<rid>/<pid>  — get install.sh content for review
  POST /api/pkg-registry/install             — install a package (run install.sh)
  POST /api/pkg-registry/uninstall           — uninstall a package

SocketIO events:
  pkg_registry_install   {task_id, pkg_id, stage, percent, message}
  pkg_registry_uninstall {task_id, pkg_id, stage, percent, message}
  pkg_registry_refresh   {stage, message, repo_id}
"""

import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time

from flask import Blueprint, g, jsonify, request

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path, q, safe_path
from auth_helpers import require_auth
from blueprints.admin_required import admin_required

log = logging.getLogger('pkg_registry')

pkg_registry_bp = Blueprint('pkg_registry', __name__, url_prefix='/api/pkg-registry')

# ── Constants ─────────────────────────────────────────────────────────────────

OFFICIAL_REPO_ID  = 'ethos-official'
OFFICIAL_REPO_URL = 'https://github.com/SyncHot/ethos-packages'
OFFICIAL_REPO_NAME = 'EthOS Official Packages'

REPOS_FILE   = data_path('pkg_registry_repos.json')
CACHE_DIR    = data_path('pkg_repos')
CATALOG_FILE = data_path('pkg_registry_catalog.json')

_lock = threading.Lock()

# ── Repo persistence ──────────────────────────────────────────────────────────

def _load_repos():
    if os.path.exists(REPOS_FILE):
        try:
            with open(REPOS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    # Default: official repo
    repos = [
        {
            'id': OFFICIAL_REPO_ID,
            'name': OFFICIAL_REPO_NAME,
            'url': OFFICIAL_REPO_URL,
            'official': True,
            'enabled': True,
            'added_at': '',
        }
    ]
    _save_repos(repos)
    return repos


def _save_repos(repos):
    os.makedirs(os.path.dirname(REPOS_FILE), exist_ok=True)
    with open(REPOS_FILE, 'w') as f:
        json.dump(repos, f, indent=2)


# ── Catalog cache ─────────────────────────────────────────────────────────────

def _load_catalog_cache():
    if os.path.exists(CATALOG_FILE):
        try:
            with open(CATALOG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_catalog_cache(catalog):
    with open(CATALOG_FILE, 'w') as f:
        json.dump(catalog, f, indent=2)


# ── Git operations ────────────────────────────────────────────────────────────

def _repo_dir(repo_id):
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', repo_id)
    return os.path.join(CACHE_DIR, safe_id)


def _git_sync(repo_id, url, emit=None):
    """Clone or pull a git repo. Returns (ok, error_msg)."""
    rdir = _repo_dir(repo_id)
    os.makedirs(CACHE_DIR, exist_ok=True)

    def _e(msg):
        if emit:
            emit(msg)

    if not os.path.exists(os.path.join(rdir, '.git')):
        _e(f'Klonowanie {url}…')
        r = subprocess.run(
            ['git', 'clone', '--depth=1', url, rdir],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode != 0:
            return False, r.stderr.strip() or r.stdout.strip() or 'git clone failed'
        _e('Sklonowano.')
    else:
        _e(f'Aktualizowanie {repo_id}…')
        r = subprocess.run(
            ['git', '-C', rdir, 'pull', '--ff-only'],
            capture_output=True, text=True, timeout=60
        )
        if r.returncode != 0:
            # Pull failed — try re-clone
            import shutil
            shutil.rmtree(rdir, ignore_errors=True)
            return _git_sync(repo_id, url, emit)
        _e('Zaktualizowano.')
    return True, ''


def _read_manifests(repo_id):
    """Read all manifest.json files from a cloned repo. Returns list of dicts."""
    rdir = _repo_dir(repo_id)
    pkgs_dir = os.path.join(rdir, 'packages')
    if not os.path.isdir(pkgs_dir):
        return []
    manifests = []
    for pkg_name in sorted(os.listdir(pkgs_dir)):
        manifest_path = os.path.join(pkgs_dir, pkg_name, 'manifest.json')
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path) as f:
                m = json.load(f)
            m['_repo_id']   = repo_id
            m['_pkg_dir']   = os.path.join(pkgs_dir, pkg_name)
            m['_has_install']   = os.path.isfile(os.path.join(pkgs_dir, pkg_name, 'install.sh'))
            m['_has_uninstall'] = os.path.isfile(os.path.join(pkgs_dir, pkg_name, 'uninstall.sh'))
            manifests.append(m)
        except Exception as e:
            log.warning('pkg_registry: bad manifest %s: %s', manifest_path, e)
    return manifests


def _build_catalog(repos):
    """Rebuild catalog cache from all enabled repos."""
    catalog = {}
    for repo in repos:
        if not repo.get('enabled'):
            continue
        for m in _read_manifests(repo['id']):
            pkg_id = m.get('id')
            if not pkg_id:
                continue
            catalog[pkg_id] = {
                **m,
                'repo_id':   repo['id'],
                'repo_name': repo.get('name', repo['id']),
                'official':  repo.get('official', False),
            }
    _save_catalog_cache(catalog)
    return catalog


# ── API: Repos ────────────────────────────────────────────────────────────────

@pkg_registry_bp.route('/repos', methods=['GET'])
@require_auth
def list_repos():
    return jsonify({'items': _load_repos()})


@pkg_registry_bp.route('/repos', methods=['POST'])
@admin_required
def add_repo():
    data = request.json or {}
    url  = data.get('url', '').strip()
    name = data.get('name', '').strip()
    if not url:
        return jsonify({'error': 'URL jest wymagany'}), 400
    if not url.startswith(('https://', 'http://', 'git@')):
        return jsonify({'error': 'Nieprawidłowy URL repozytorium'}), 400

    repos = _load_repos()
    # Prevent duplicates
    if any(r['url'] == url for r in repos):
        return jsonify({'error': 'Repozytorium już istnieje'}), 409

    repo_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', url.rstrip('/').split('/')[-1])
    if any(r['id'] == repo_id for r in repos):
        repo_id = repo_id + '_' + secrets.token_hex(4)

    repo = {
        'id': repo_id,
        'name': name or repo_id,
        'url': url,
        'official': False,
        'enabled': True,
        'added_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    repos.append(repo)
    _save_repos(repos)

    # Kick off background clone
    _socketio = getattr(pkg_registry_bp, '_socketio', None)
    def _bg():
        ok, err = _git_sync(repo_id, url)
        if ok:
            _build_catalog(repos)
        if _socketio:
            _socketio.emit('pkg_registry_refresh', {
                'stage': 'done' if ok else 'error',
                'repo_id': repo_id,
                'message': '' if ok else err,
            })
    threading.Thread(target=_bg, daemon=True).start()

    return jsonify({'ok': True, 'item': repo})


@pkg_registry_bp.route('/repos/<repo_id>', methods=['DELETE'])
@admin_required
def remove_repo(repo_id):
    repos = _load_repos()
    repo  = next((r for r in repos if r['id'] == repo_id), None)
    if not repo:
        return jsonify({'error': 'Repozytorium nie znalezione'}), 404
    if repo.get('official'):
        return jsonify({'error': 'Nie można usunąć oficjalnego repozytorium'}), 400

    repos = [r for r in repos if r['id'] != repo_id]
    _save_repos(repos)

    # Remove cached dir
    import shutil
    shutil.rmtree(_repo_dir(repo_id), ignore_errors=True)
    _build_catalog(repos)

    return jsonify({'ok': True})


@pkg_registry_bp.route('/repos/<repo_id>/toggle', methods=['POST'])
@admin_required
def toggle_repo(repo_id):
    repos = _load_repos()
    repo  = next((r for r in repos if r['id'] == repo_id), None)
    if not repo:
        return jsonify({'error': 'Repozytorium nie znalezione'}), 404
    repo['enabled'] = not repo.get('enabled', True)
    _save_repos(repos)
    _build_catalog(repos)
    return jsonify({'ok': True, 'enabled': repo['enabled']})


# ── API: Catalog ──────────────────────────────────────────────────────────────

@pkg_registry_bp.route('/catalog', methods=['GET'])
@require_auth
def get_catalog():
    repos = _load_repos()
    catalog = _load_catalog_cache()
    # If cache empty, try to build (repos may not be cloned yet)
    if not catalog:
        catalog = _build_catalog(repos)
    return jsonify({'items': list(catalog.values())})


@pkg_registry_bp.route('/catalog/refresh', methods=['POST'])
@admin_required
def refresh_catalog():
    repos    = _load_repos()
    task_id  = secrets.token_hex(8)
    _socketio = getattr(pkg_registry_bp, '_socketio', None)

    def _bg():
        for repo in repos:
            if not repo.get('enabled'):
                continue
            def _emit(msg, rid=repo['id']):
                if _socketio:
                    _socketio.emit('pkg_registry_refresh', {
                        'task_id': task_id, 'stage': 'progress',
                        'repo_id': rid, 'message': msg,
                    })
            ok, err = _git_sync(repo['id'], repo['url'], emit=_emit)
            if not ok:
                log.warning('pkg_registry: sync failed for %s: %s', repo['id'], err)
        _build_catalog(repos)
        if _socketio:
            _socketio.emit('pkg_registry_refresh', {
                'task_id': task_id, 'stage': 'done', 'message': 'Katalog zaktualizowany'
            })

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


# ── API: Script preview ───────────────────────────────────────────────────────

@pkg_registry_bp.route('/script/<repo_id>/<pkg_id>/install', methods=['GET'])
@admin_required
def get_install_script(repo_id, pkg_id):
    rdir   = _repo_dir(re.sub(r'[^a-zA-Z0-9_\-]', '_', repo_id))
    script = os.path.join(rdir, 'packages', pkg_id, 'install.sh')
    try:
        script = safe_path(script)
    except Exception:
        return jsonify({'error': 'Nieprawidłowa ścieżka'}), 400
    if not os.path.isfile(script):
        return jsonify({'error': 'Skrypt nie istnieje'}), 404
    with open(script) as f:
        content = f.read()
    return jsonify({'content': content, 'pkg_id': pkg_id, 'repo_id': repo_id})


# ── API: Install ──────────────────────────────────────────────────────────────

@pkg_registry_bp.route('/install', methods=['POST'])
@admin_required
def install_package():
    data   = request.json or {}
    pkg_id = data.get('pkg_id', '').strip()
    if not pkg_id:
        return jsonify({'error': 'pkg_id jest wymagany'}), 400

    catalog = _load_catalog_cache()
    pkg = catalog.get(pkg_id)
    if not pkg:
        return jsonify({'error': f'Paczka {pkg_id!r} nie znaleziona w katalogu'}), 404

    task_id  = secrets.token_hex(8)
    _socketio = getattr(pkg_registry_bp, '_socketio', None)

    def _emit(stage, pct, msg):
        if _socketio:
            _socketio.emit('pkg_registry_install', {
                'task_id': task_id, 'pkg_id': pkg_id,
                'stage': stage, 'percent': pct, 'message': msg,
            })

    def _bg():
        _emit('start', 5, f'Instalowanie {pkg.get("name", pkg_id)}…')

        # 1. Install apt dependencies from manifest
        apt_deps = pkg.get('apt_deps', [])
        if apt_deps:
            _emit('progress', 15, 'Instalowanie zależności systemowych: ' + ', '.join(apt_deps))
            r = subprocess.run(
                ['apt-get', 'install', '-y'] + apt_deps,
                capture_output=True, text=True, timeout=300
            )
            if r.returncode != 0:
                _emit('error', 0, 'apt error: ' + (r.stderr or r.stdout)[:300])
                return

        # 2. Run install.sh if present
        pkg_dir = pkg.get('_pkg_dir', '')
        install_sh = os.path.join(pkg_dir, 'install.sh') if pkg_dir else ''
        if install_sh and os.path.isfile(install_sh):
            _emit('progress', 40, 'Uruchamianie skryptu instalacyjnego…')
            r = subprocess.run(
                ['bash', install_sh],
                capture_output=True, text=True, timeout=600,
                env={**os.environ, 'ETHOS_PKG_ID': pkg_id, 'ETHOS_DATA': data_path('')}
            )
            if r.returncode != 0:
                _emit('error', 0, 'Błąd install.sh: ' + (r.stderr or r.stdout)[:400])
                return

        # 3. Call EthOS hook if defined (e.g. /api/wireguard/install for further config)
        ethos_hook = pkg.get('ethos_hook', '')
        if ethos_hook:
            _emit('progress', 70, 'Uruchamianie konfiguracji EthOS…')
            try:
                import urllib.request as _ur
                req = _ur.Request(
                    f'http://127.0.0.1:9000{ethos_hook}',
                    data=b'{}',
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                _ur.urlopen(req, timeout=120)
            except Exception as _he:
                log.warning('pkg_registry: ethos_hook %s failed: %s', ethos_hook, _he)

        _emit('done', 100, f'{pkg.get("name", pkg_id)} zainstalowany!')

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


# ── API: Uninstall ────────────────────────────────────────────────────────────

@pkg_registry_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall_package():
    data   = request.json or {}
    pkg_id = data.get('pkg_id', '').strip()
    wipe   = data.get('wipe_data', False)

    catalog = _load_catalog_cache()
    pkg = catalog.get(pkg_id)
    if not pkg:
        return jsonify({'error': f'Paczka {pkg_id!r} nie znaleziona'}), 404

    task_id  = secrets.token_hex(8)
    _socketio = getattr(pkg_registry_bp, '_socketio', None)

    def _emit(stage, pct, msg):
        if _socketio:
            _socketio.emit('pkg_registry_uninstall', {
                'task_id': task_id, 'pkg_id': pkg_id,
                'stage': stage, 'percent': pct, 'message': msg,
            })

    def _bg():
        _emit('start', 5, f'Odinstalowywanie {pkg.get("name", pkg_id)}…')

        # 1. Run uninstall.sh if present
        pkg_dir = pkg.get('_pkg_dir', '')
        uninstall_sh = os.path.join(pkg_dir, 'uninstall.sh') if pkg_dir else ''
        if uninstall_sh and os.path.isfile(uninstall_sh):
            _emit('progress', 30, 'Uruchamianie skryptu deinstalacyjnego…')
            r = subprocess.run(
                ['bash', uninstall_sh],
                capture_output=True, text=True, timeout=300,
                env={**os.environ, 'ETHOS_PKG_ID': pkg_id, 'ETHOS_DATA': data_path(''),
                     'ETHOS_WIPE': '1' if wipe else '0'}
            )
            if r.returncode != 0:
                log.warning('pkg_registry: uninstall.sh error: %s', r.stderr[:200])

        # 2. Call EthOS uninstall hook
        uninstall_hook = pkg.get('ethos_uninstall_hook', '')
        if uninstall_hook:
            _emit('progress', 70, 'Usuwanie konfiguracji…')
            try:
                import urllib.request as _ur
                req = _ur.Request(
                    f'http://127.0.0.1:9000{uninstall_hook}',
                    data=json.dumps({'wipe_data': wipe}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                _ur.urlopen(req, timeout=60)
            except Exception as _he:
                log.warning('pkg_registry: uninstall_hook %s failed: %s', uninstall_hook, _he)

        _emit('done', 100, f'{pkg.get("name", pkg_id)} odinstalowany.')

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})
