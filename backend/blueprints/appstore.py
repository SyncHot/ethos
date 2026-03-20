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
import tempfile
import logging
import sys
from flask import Blueprint, request, jsonify, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_base, NATIVE_MODE, data_path, check_dep, get_data_disk as _get_data_disk
from utils import load_json as _load_json, save_json as _save_json, run_host, \
    find_compose_project_names as _find_compose_project_names, \
    docker_available as _docker_available_util

# Optional: sandbox policy for compose resource limits
try:
    from blueprints.sandbox_policy import get_effective_policy as _get_sandbox_policy
except Exception:  # pragma: no cover - fallback when module not present
    def _get_sandbox_policy(_name):
        return {}

log = logging.getLogger('appstore')

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
SANDBOX_OVERRIDE_FILENAME = 'docker-compose.ethos-sandbox.yml'

HOST_COMPOSE_ROOT = '/home/marcin/docker'
CONTAINER_COMPOSE_ROOT = '/home/marcin/docker'
APPDATA_ROOT = '/home/marcin/docker/_appdata'

# Default resource limits applied if service has none
DEFAULT_MEM_LIMIT = '2g'
DEFAULT_RESTART_POLICY = 'unless-stopped'


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
_repo_locks = {}
_repo_locks_guard = threading.Lock()
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


def _is_safe_mount_path(path):
    """Check if host path is safe to mount."""
    if not path:
        return True, None
    rp = os.path.realpath(path)

    # Explicitly disallow sensitive system paths
    sensitive_roots = ['/', '/boot', '/dev', '/etc', '/lib', '/proc', '/sys', '/usr', '/var/lib/docker']
    for s_root in sensitive_roots:
        if rp == s_root or rp.startswith(s_root + os.sep):
            return False, f'Montowanie sciezki systemowej "{s_root}" jest zabronione'

    if rp in ['/var/run/docker.sock', '/run/docker.sock']:
        return False, 'Montowanie docker.sock jest niedozwolone'

    # Check allowed roots
    allowed_roots = [os.path.realpath(_apps_root()), os.path.realpath(_compose_root())]
    for root in allowed_roots:
        if rp == root or rp.startswith(root + os.sep):
            return True, None

    return False, f'Sciezka montowania "{path}" jest poza dozwolonym obszarem'


def _validate_compose_policy(compose_text):
    """Reject dangerous compose options and host bind mounts outside allowed roots.

    This is the final safety gate — it catches options that were not stripped by
    _adapt_compose (e.g., manually edited compose_override content).  Error
    messages are intentionally actionable so they can be shown directly to the
    user at install time.
    """
    try:
        data = yaml.safe_load(compose_text)
    except Exception:
        return 'Nieprawidlowa skladnia compose YAML — sprawdz formatowanie pliku'

    if not isinstance(data, dict):
        return 'Nieprawidlowy plik compose — brak struktury YAML'
    services = data.get('services', {})
    if not isinstance(services, dict) or not services:
        return 'Compose nie zawiera sekcji services'

    errors = []

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue

        # Check unsafe flags
        if svc.get('privileged') is True:
            errors.append(f'Serwis {svc_name}: privileged=true jest niedozwolone.')

        if svc.get('cap_add'):
            errors.append(f'Serwis {svc_name}: cap_add jest niedozwolone.')

        if svc.get('devices'):
            errors.append(f'Serwis {svc_name}: devices jest niedozwolone.')

        if svc.get('cgroup_parent'):
            errors.append(f'Serwis {svc_name}: cgroup_parent jest niedozwolone.')

        # Check unsafe namespaces
        for ns in ['network_mode', 'pid', 'ipc', 'userns_mode']:
            if str(svc.get(ns, '')).strip().lower() == 'host':
                errors.append(f'Serwis {svc_name}: {ns}=host jest niedozwolone.')

        # Check security_opt
        sec_opts = svc.get('security_opt')
        if sec_opts:
            if isinstance(sec_opts, str):
                sec_opts = [sec_opts]
            for opt in sec_opts:
                if str(opt).strip().lower() not in ('no-new-privileges', 'no-new-privileges:true'):
                    errors.append(f'Serwis {svc_name}: security_opt "{opt}" jest niedozwolone.')

        # Check volumes
        for vol in (svc.get('volumes') or []):
            host_path = None
            if isinstance(vol, str):
                left = vol.split(':', 1)[0].strip()
                # Named docker volume (no slash) is safe.
                if left and ('/' not in left) and not left.startswith('.'):
                    continue
                host_path = left if left.startswith('/') else os.path.join(_compose_root(), left)
            elif isinstance(vol, dict) and str(vol.get('type', '')).lower() == 'bind':
                host_path = vol.get('source', '')

            if host_path:
                is_safe, err_msg = _is_safe_mount_path(host_path)
                if not is_safe:
                    errors.append(f'Serwis {svc_name}: {err_msg}')

    if errors:
        return '\n'.join(errors)

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
    os.makedirs(os.path.join(CACHE_DIR, 'repos'), exist_ok=True)

    with _repo_locks_guard:
        lock = _repo_locks.setdefault(repo_id, threading.Lock())

    with lock:
        fd, zip_path = tempfile.mkstemp(dir=CACHE_DIR, prefix=f'{repo_id}-', suffix='.zip')
        os.close(fd)
        tmp_extract_dir = tempfile.mkdtemp(dir=os.path.join(CACHE_DIR, 'repos'), prefix=f'{repo_id}-tmp-')
        backup_dir = None
        replaced = False
        try:
            urllib.request.urlretrieve(repo['url'], zip_path)

            _safe_extract_zip(zip_path, tmp_extract_dir)
            os.remove(zip_path)

            entries = os.listdir(tmp_extract_dir)
            inner_dir = entries[0] if len(entries) == 1 and os.path.isdir(os.path.join(tmp_extract_dir, entries[0])) else None

            if os.path.exists(repo_dir):
                backup_dir = f"{repo_dir}.bak.{uuid.uuid4().hex[:8]}"
                os.rename(repo_dir, backup_dir)

            os.rename(tmp_extract_dir, repo_dir)
            replaced = True

            if backup_dir and os.path.exists(backup_dir):
                shutil.rmtree(backup_dir, ignore_errors=True)

            return os.path.join(repo_dir, inner_dir) if inner_dir else repo_dir
        finally:
            if not replaced and os.path.isdir(tmp_extract_dir):
                shutil.rmtree(tmp_extract_dir, ignore_errors=True)
            if os.path.isfile(zip_path):
                os.remove(zip_path)
            if not replaced and backup_dir and os.path.exists(backup_dir):
                os.rename(backup_dir, repo_dir)


def _get_system_tz():
    """Detect host timezone, default to UTC."""
    try:
        if os.path.islink('/etc/localtime'):
            target = os.readlink('/etc/localtime')
            if '/zoneinfo/' in target:
                return target.split('/zoneinfo/')[-1]
        if os.path.isfile('/etc/timezone'):
            with open('/etc/timezone') as f:
                tz = f.read().strip()
                if tz:
                    return tz
    except Exception:
        pass
    return 'UTC'


def _extract_ports_from_services(services):
    """Extract published host ports from compose services."""
    ports = set()
    if not isinstance(services, dict):
        return ports
    def _add_host_candidate(candidate):
        candidate_value = str(candidate or '')
        candidate_value = candidate_value.split('/')[0].strip()
        if not candidate_value:
            return
        if '-' in candidate_value:
            bounds = [segment.strip() for segment in candidate_value.split('-', 1)]
            if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                start, end = int(bounds[0]), int(bounds[1])
                low, high = min(start, end), max(start, end)
                ports.update(range(low, high + 1))
                return
        if candidate_value.isdigit():
            ports.add(int(candidate_value))
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for port_entry in (svc.get('ports') or []):
            if isinstance(port_entry, dict):
                host_candidate = (
                    port_entry.get('published')
                    or port_entry.get('published_port')
                )
                if host_candidate is not None:
                    try:
                        _add_host_candidate(host_candidate)
                    except (ValueError, IndexError):
                        pass
                continue
            p = str(port_entry)
            # Formats: "8080:80", "8080:80/tcp", "127.0.0.1:8080:80"
            parts = p.split(':')
            try:
                if len(parts) >= 2:
                    _add_host_candidate(parts[-2])
                elif len(parts) == 1:
                    _add_host_candidate(parts[0])
            except (ValueError, IndexError):
                pass
    return ports


def _parse_compose_metadata(compose_path):
    """Parse x-casaos metadata and service info from a docker-compose.yml."""
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

        tips_obj = casaos.get('tips', {})
        tips = ''
        if isinstance(tips_obj, dict):
            before = tips_obj.get('before_install', {})
            tips = before.get('en_us', '') if isinstance(before, dict) else str(before)
        elif tips_obj:
            tips = str(tips_obj)

        icon = casaos.get('icon', '')
        category = casaos.get('category', 'Uncategorized')
        port_map = casaos.get('port_map', '')
        archs = casaos.get('architectures', [])
        developer = casaos.get('developer', '')
        main_service = casaos.get('main', '')
        thumbnail = casaos.get('thumbnail', '')
        screenshots = casaos.get('screenshot_link', [])
        if isinstance(screenshots, str):
            screenshots = [screenshots] if screenshots else []
        store_app_id = casaos.get('store_app_id', '')

        image = ''
        services = data.get('services', {})
        if main_service and main_service in services:
            image = services[main_service].get('image', '')
        elif services:
            image = next(iter(services.values())).get('image', '')

        if '@sha256:' in image:
            image = image.split('@sha256:')[0]

        # Extract service-level metadata
        service_count = len(services) if isinstance(services, dict) else 0
        all_images = []
        for svc in (services.values() if isinstance(services, dict) else []):
            if isinstance(svc, dict) and svc.get('image'):
                img = svc['image']
                if '@sha256:' in img:
                    img = img.split('@sha256:')[0]
                all_images.append(img)

        # Extract host ports from service port mappings
        host_ports = sorted(_extract_ports_from_services(services))

        # Detect named volumes
        top_volumes = list((data.get('volumes') or {}).keys()) if isinstance(data.get('volumes'), dict) else []

        return {
            'title': title,
            'description': desc,
            'tagline': tagline,
            'icon': icon,
            'thumbnail': thumbnail,
            'category': category,
            'port_map': str(port_map),
            'architectures': archs,
            'developer': developer,
            'image': image,
            'main_service': main_service,
            'tips': tips,
            'screenshots': screenshots,
            'store_app_id': store_app_id,
            'service_count': service_count,
            'all_images': all_images,
            'host_ports': host_ports,
            'named_volumes': top_volumes,
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
            'thumbnail': meta.get('thumbnail', ''),
            'main_service': meta.get('main_service', ''),
            'version': version,
            'compose_path': compose_file,
            'repo_id': repo_id,
            'repo_name': repo_name,
            'store_app_id': meta.get('store_app_id', ''),
            'tips': meta.get('tips', ''),
            'screenshots': meta.get('screenshots', []),
            'service_count': meta.get('service_count', 1),
            'all_images': meta.get('all_images', []),
            'host_ports': meta.get('host_ports', []),
            'named_volumes': meta.get('named_volumes', []),
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


def _save_catalog_atomic(catalog):
    """Write catalog JSON atomically (tmp file + rename)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix='.json.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(catalog, f)
        os.replace(tmp_path, CATALOG_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _get_catalog(force_refresh=False):
    """Get (possibly cached) catalog. Implements stale-while-revalidate."""
    with _catalog_lock:
        if not force_refresh and os.path.isfile(CATALOG_FILE):
            mtime = os.path.getmtime(CATALOG_FILE)
            age = time.time() - mtime
            if age < CACHE_MAX_AGE:
                try:
                    with open(CATALOG_FILE) as f:
                        return json.load(f)
                except Exception:
                    pass
            # Stale but usable — serve stale, trigger background refresh
            if age < CACHE_MAX_AGE * 2:
                try:
                    with open(CATALOG_FILE) as f:
                        stale = json.load(f)
                    _trigger_background_refresh()
                    return stale
                except Exception:
                    pass

        try:
            catalog, _errors = _build_full_catalog()
            if _errors:
                log.warning('Catalog build errors: %s', _errors)
            _save_catalog_atomic(catalog)
            return catalog
        except Exception as e:
            log.error('Catalog build failed: %s', e)
            if os.path.isfile(CATALOG_FILE):
                with open(CATALOG_FILE) as f:
                    return json.load(f)
            return []


_bg_refresh_running = False

def _trigger_background_refresh():
    """Refresh catalog in a background thread (stale-while-revalidate)."""
    global _bg_refresh_running
    if _bg_refresh_running:
        return
    _bg_refresh_running = True

    def _do_refresh():
        global _bg_refresh_running
        try:
            catalog, _ = _build_full_catalog()
            with _catalog_lock:
                _save_catalog_atomic(catalog)
            log.info('Background catalog refresh complete: %d apps', len(catalog))
        except Exception as e:
            log.error('Background catalog refresh failed: %s', e)
        finally:
            _bg_refresh_running = False

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()


def _get_installed_apps():
    """Get set of installed app directory names on host."""
    return _find_compose_project_names(_compose_root())


def _extract_editable_config(compose_text):
    """Extract editable configuration (ports, volumes) from compose YAML."""
    config = {}
    try:
        data = yaml.safe_load(compose_text)
        if not data or not isinstance(data, dict):
            return {}

        services = data.get('services', {})
        for name, svc in services.items():
            if not isinstance(svc, dict):
                continue

            s_conf = {'ports': [], 'volumes': []}

            # Ports — support "host:container", "ip:host:container", and "/proto" suffix
            for p in (svc.get('ports') or []):
                try:
                    p_str = str(p)
                    parts = p_str.split(':')
                    if len(parts) >= 2:
                        # Extract container port and protocol
                        right = parts[-1]
                        if '/' in right:
                            container, proto = right.split('/', 1)
                        else:
                            container, proto = right, 'tcp'

                        # Extract host port
                        host = parts[-2]

                        # Preserve any IP binding prefix (e.g. "127.0.0.1" in "127.0.0.1:8080:80")
                        ip_binding = parts[-3] if len(parts) >= 3 else ''
                        if ip_binding:
                            host = f"{ip_binding}:{host}"

                        s_conf['ports'].append({
                            'host': host,
                            'container': container,
                            'protocol': proto,
                            'original': p_str
                        })
                except Exception:
                    pass

            # Volumes — editable entries are those with an explicit host path (bind mounts).
            # Standalone named-volume references (e.g. "mydata") have no host path to edit
            # and are preserved verbatim by _apply_editable_config.
            for v in (svc.get('volumes') or []):
                try:
                    v_str = ''
                    if isinstance(v, str):
                        v_str = v
                    elif isinstance(v, dict) and v.get('type') == 'bind':
                        v_str = f"{v.get('source')}:{v.get('target')}"

                    if v_str:
                        parts = v_str.split(':')
                        if len(parts) >= 2:
                            # host_path:container_path[:mode]
                            host_path = parts[0]
                            container_path = parts[1]
                            mode = parts[2] if len(parts) > 2 else 'rw'

                            s_conf['volumes'].append({
                                'host': host_path,
                                'container': container_path,
                                'mode': mode,
                                'original': v_str
                            })
                except Exception:
                    pass

            config[name] = s_conf

    except Exception:
        pass
    return config


def _apply_editable_config(compose_text, overrides):
    """Apply configuration overrides (ports, volumes) to compose YAML."""
    if not overrides or not isinstance(overrides, dict):
        return compose_text

    try:
        data = yaml.safe_load(compose_text)
        if not data or not isinstance(data, dict):
            return compose_text

        services = data.get('services', {})
        modified = False

        for name, conf in overrides.items():
            if name not in services:
                continue
            svc = services[name]
            if not isinstance(svc, dict):
                continue

            # Update ports — rebuild only the entries that came from the UI,
            # preserving the original IP binding if one was present.
            if 'ports' in conf and isinstance(conf['ports'], list):
                original_ports = svc.get('ports') or []
                preserved_ports = []
                for p in original_ports:
                    try:
                        p_str = str(p)
                        parts = p_str.split(':')
                        # If extraction logic would fail or skip it, we preserve it.
                        if len(parts) < 2:
                            preserved_ports.append(p)
                    except Exception:
                        preserved_ports.append(p)

                new_ports = list(preserved_ports)
                for p in conf['ports']:
                    host = p.get('host')
                    container = p.get('container')
                    proto = p.get('protocol', 'tcp')
                    if host and container:
                        entry = f"{host}:{container}"
                        if proto and proto != 'tcp':
                            entry += f"/{proto}"
                        new_ports.append(entry)
                svc['ports'] = new_ports
                modified = True

            # Update volumes — rebuild editable (bind-mount) entries from the UI
            # while preserving any standalone named-volume references that the UI
            # never saw (they have no host path and cannot be edited).
            if 'volumes' in conf and isinstance(conf['volumes'], list):
                # Collect the original entries that were NOT extractable (standalone named volumes)
                original_vols = svc.get('volumes') or []
                preserved = []
                for ov in original_vols:
                    if isinstance(ov, str) and ':' not in ov:
                        preserved.append(ov)
                    elif isinstance(ov, dict) and ov.get('type') not in (None, 'bind'):
                        preserved.append(ov)

                new_vols = list(preserved)
                for v in conf['volumes']:
                    host = v.get('host')
                    container = v.get('container')
                    mode = v.get('mode', 'rw')
                    if host and container:
                        entry = f"{host}:{container}"
                        if mode and mode != 'rw':
                            entry += f":{mode}"
                        new_vols.append(entry)
                svc['volumes'] = new_vols
                modified = True

        if modified:
            return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)

    except Exception:
        pass

    return compose_text


def _policy_to_service_limits(policy):
    """Translate sandbox policy into docker-compose service options."""
    limits = {}

    mem_limit = str(policy.get('mem_limit', '')).strip()
    if mem_limit and mem_limit != '0':
        limits['mem_limit'] = mem_limit

    mem_reservation = str(policy.get('mem_reservation', '')).strip()
    if mem_reservation and mem_reservation != '0':
        limits['mem_reservation'] = mem_reservation

    cpu_quota = policy.get('cpu_quota', 0)
    try:
        cpu_quota = float(cpu_quota)
    except (TypeError, ValueError):
        cpu_quota = 0
    if cpu_quota > 0:
        limits['cpus'] = round(cpu_quota / 100.0, 3)

    pids_limit = policy.get('pids_limit', 0)
    try:
        pids_limit = int(pids_limit)
    except (TypeError, ValueError):
        pids_limit = 0
    if pids_limit > 0:
        limits['pids_limit'] = pids_limit

    if policy.get('read_only_root'):
        limits['read_only'] = True

    if policy.get('no_new_privileges'):
        limits['security_opt'] = ['no-new-privileges:true']

    cap_drop = policy.get('cap_drop') or []
    if cap_drop:
        limits['cap_drop'] = cap_drop

    cap_add = policy.get('cap_add') or []
    if cap_add:
        limits['cap_add'] = cap_add

    return limits


def _compose_files_args(compose_files):
    return ' '.join(f'-f {f}' for f in compose_files if f)


def _list_compose_services(project_path, compose_files):
    """List services defined in compose files (host context)."""
    files_arg = _compose_files_args(compose_files)
    try:
        out, err, rc = _run_host(f'docker compose {files_arg} config --services', timeout=60, cwd=project_path)
    except Exception as exc:  # noqa: BLE001
        return [], f'Compose services error: {exc}'
    if rc != 0:
        return [], err.strip() or 'docker compose config failed'
    services = [line.strip() for line in out.split('\n') if line.strip()]
    return services, None


def _ensure_sandbox_override(app_name, project_path, compose_filename):
    """Create/update sandbox override compose file with enforced limits."""
    policy = _get_sandbox_policy(app_name) or {}
    if not policy:
        return None, None

    services, err = _list_compose_services(project_path, [compose_filename])
    if err:
        return None, err
    if not services:
        return None, 'Brak usług w pliku docker-compose'

    limits = _policy_to_service_limits(policy)
    if not limits:
        return None, None

    override = {'version': '3', 'services': {svc: dict(limits) for svc in services}}
    override_path = os.path.join(project_path, SANDBOX_OVERRIDE_FILENAME)

    try:
        with open(override_path, 'w') as f:
            yaml.safe_dump(override, f, sort_keys=False)
    except OSError as exc:
        return None, f'Nie można zapisać pliku polityki sandbox: {exc}'

    return override_path, None


def _adapt_compose(compose_text, app_id):
    """Adapt a CasaOS compose file for our environment.

    Handles CasaOS-specific variables, path rewrites, resource defaults,
    strips non-standard metadata keys, and removes unsafe options.

    Returns:
        (adapted_text, adapt_warnings) — adapted_text is the modified YAML
        string; adapt_warnings is a list of human-readable strings describing
        each unsafe option that was automatically removed.
    """
    adapt_warnings = []
    appdata = f'{_apps_root()}/{app_id}'

    # Replace CasaOS path/variable patterns
    text = compose_text.replace('/DATA/AppData/$AppID', appdata)
    text = text.replace('/DATA/AppData/${AppID}', appdata)
    text = re.sub(r'\$\{?AppID\}?', app_id, text)

    # System-level variable substitutions
    tz = _get_system_tz()
    text = re.sub(r'\$\{?TZ\}?', tz, text)
    uid = str(os.getuid())
    gid = str(os.getgid())
    text = re.sub(r'\$\{?PUID\}?', uid, text)
    text = re.sub(r'\$\{?PGID\}?', gid, text)
    def _normalize_webui_ports(raw_text):
        ports_block_re = re.compile(r'^\s*ports\s*:')
        host_port_re = re.compile(r'^(\s*-\s*)(["\']?)\s*\$\{?WEBUI_PORT\}?\s*:\s*(.*)$')

        result_lines = []
        in_ports = False
        ports_indent = 0

        for line in raw_text.splitlines(keepends=True):
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())

            if in_ports and stripped and indent <= ports_indent and not stripped.startswith('-'):
                in_ports = False

            if not in_ports and ports_block_re.match(line):
                in_ports = True
                ports_indent = indent
                result_lines.append(line)
                continue

            if in_ports and stripped.startswith('-'):
                core = line.rstrip('\r\n')
                newline = line[len(core):]
                match = host_port_re.match(core)
                if match:
                    prefix, quote, remainder = match.groups()
                    line = f'{prefix}{quote}{remainder}{newline}'

            result_lines.append(line)

        return ''.join(result_lines)

    text = _normalize_webui_ports(text)
    text = re.sub(r'\$\{?WEBUI_PORT\}?', '', text)

    # Unsafe per-service keys that grant excess host privileges
    _UNSAFE_KEYS = {
        'privileged':    'tryb uprzywilejowany (privileged)',
        'cap_add':       'dodatkowe uprawnienia linuksowe (cap_add)',
        'devices':       'bezposredni dostep do urzadzen (devices)',
        'cgroup_parent': 'nadrzedna grupa kontrolna (cgroup_parent)',
    }
    # Unsafe namespace-sharing modes (value must equal "host")
    _UNSAFE_NS = {
        'network_mode': 'network_mode=host',
        'pid':          'pid=host',
        'ipc':          'ipc=host',
        'userns_mode':  'userns_mode=host',
    }

    try:
        data = yaml.safe_load(text)
        if not data:
            return text, adapt_warnings

        # Strip CasaOS metadata extensions
        data.pop('x-casaos', None)

        services = data.get('services', {})
        for svc_name in list(services.keys()):
            svc = services[svc_name]
            if not isinstance(svc, dict):
                continue
            svc.pop('x-casaos', None)

            # Remove unsafe options — warn for each truthy value removed
            for key, label in _UNSAFE_KEYS.items():
                val = svc.pop(key, None)
                # privileged=false / empty cap_add=[] are no-ops; skip warning
                if val:
                    adapt_warnings.append(
                        f'Serwis "{svc_name}": automatycznie usunieto {label}'
                    )

            # Handle security_opt separately (allow safe values)
            raw_sec = svc.get('security_opt')
            if raw_sec:
                if isinstance(raw_sec, str):
                    raw_sec = [raw_sec]
                
                safe_opts = []
                removed_count = 0
                
                for opt in raw_sec:
                    if str(opt).strip().lower() in ('no-new-privileges', 'no-new-privileges:true'):
                        safe_opts.append(opt)
                    else:
                        removed_count += 1
                
                if safe_opts:
                    svc['security_opt'] = safe_opts
                else:
                    svc.pop('security_opt', None)
                
                if removed_count > 0:
                    adapt_warnings.append(
                        f'Serwis "{svc_name}": automatycznie usunieto niebezpieczne opcje security_opt'
                    )

            for key, label in _UNSAFE_NS.items():
                if str(svc.get(key, '')).strip().lower() == 'host':
                    svc.pop(key)
                    adapt_warnings.append(
                        f'Serwis "{svc_name}": automatycznie usunieto {label}'
                    )

            # Ensure restart policy is set
            if 'restart' not in svc:
                svc['restart'] = DEFAULT_RESTART_POLICY

            # Add default memory limit if none specified
            if not svc.get('mem_limit') and not svc.get('deploy'):
                svc['mem_limit'] = DEFAULT_MEM_LIMIT

            # Rewrite volume paths to safe locations
            volumes = svc.get('volumes', [])
            adapted_volumes = []
            for vol in volumes:
                host_path_to_check = None
                
                if isinstance(vol, str):
                    parts = vol.split(':', 1)
                    left = parts[0].strip()
                    rest = (':' + parts[1]) if len(parts) > 1 else ''
                    
                    if left.startswith('../'):
                        # Strip all leading ../ sequences and anchor to appdata
                        stripped = re.sub(r'^(\.\./)+', '', left)
                        left = os.path.join(appdata, stripped)
                        vol = left + rest
                    elif left.startswith('./'):
                        left = os.path.join(appdata, left[2:])
                        vol = left + rest
                    elif left.startswith('/DATA/'):
                        left = left.replace('/DATA/', appdata + '/', 1)
                        vol = left + rest
                    
                    # Determine path to check
                    if left and ('/' not in left) and not left.startswith('.'):
                        # Named volume, skip check
                        pass
                    else:
                        host_path_to_check = left

                elif isinstance(vol, dict):
                    src = vol.get('source', '')
                    if isinstance(src, str):
                        if src.startswith('../'):
                            stripped = re.sub(r'^(\.\./)+', '', src)
                            src = os.path.join(appdata, stripped)
                            vol = dict(vol, source=src)
                        elif src.startswith('./'):
                            src = os.path.join(appdata, src[2:])
                            vol = dict(vol, source=src)
                        elif src.startswith('/DATA/'):
                            src = src.replace('/DATA/', appdata + '/', 1)
                            vol = dict(vol, source=src)
                        
                        if str(vol.get('type', '')).lower() == 'bind':
                            host_path_to_check = src
                
                if host_path_to_check:
                    is_safe, err_msg = _is_safe_mount_path(host_path_to_check)
                    if not is_safe:
                        adapt_warnings.append(
                            f'Serwis "{svc_name}": automatycznie usunieto wolumen {host_path_to_check} ({err_msg})'
                        )
                        continue

                adapted_volumes.append(vol)
            
            if volumes:
                svc['volumes'] = adapted_volumes

            # Adapt env_file paths — anchor to appdata and drop unsafe locations
            env_file = svc.get('env_file')
            if env_file:
                env_list = env_file if isinstance(env_file, list) else [env_file]
                new_env_files = []
                for env_entry in env_list:
                    if not isinstance(env_entry, str):
                        continue
                    original_env = env_entry
                    host_path_to_check = None

                    if env_entry.startswith('../'):
                        stripped = re.sub(r'^(\.\./)+', '', env_entry)
                        env_entry = os.path.join(appdata, stripped)
                    elif env_entry.startswith('./'):
                        env_entry = os.path.join(appdata, env_entry[2:])
                    elif env_entry.startswith('/DATA/'):
                        env_entry = env_entry.replace('/DATA/', appdata + '/', 1)
                    elif env_entry.startswith('/'):
                        host_path_to_check = env_entry
                    else:
                        # Relative file — place it under appdata for safety
                        env_entry = os.path.join(appdata, env_entry)

                    if env_entry.startswith('/'):
                        host_path_to_check = host_path_to_check or env_entry

                    if host_path_to_check:
                        is_safe, err_msg = _is_safe_mount_path(host_path_to_check)
                        if not is_safe:
                            adapt_warnings.append(
                                f'Serwis "{svc_name}": env_file {original_env} usunięto ({err_msg})'
                            )
                            continue

                    new_env_files.append(env_entry)

                if new_env_files:
                    svc['env_file'] = new_env_files if len(new_env_files) > 1 else new_env_files[0]
                else:
                    svc.pop('env_file', None)

            # Remove hostname/domainname that may conflict
            svc.pop('hostname', None)
            svc.pop('domainname', None)

            # Add container name based on app_id for predictability
            if 'container_name' not in svc:
                if len(services) == 1:
                    svc['container_name'] = app_id
                else:
                    svc['container_name'] = f'{app_id}-{svc_name}'

        # Remove top-level 'name' key — Docker Compose will use
        # the directory name as the project name
        data.pop('name', None)

        return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True), adapt_warnings
    except Exception:
        return text, adapt_warnings


def _check_port_conflicts(compose_text):
    """Check if ports in compose file conflict with running containers.

    Returns list of conflict descriptions, empty if no conflicts.
    """
    conflicts = []
    try:
        data = yaml.safe_load(compose_text)
        if not data or not isinstance(data, dict):
            return conflicts

        services = data.get('services', {})
        needed_ports = _extract_ports_from_services(services)
        if not needed_ports:
            return conflicts

        # Get ports used by running containers
        out, _err, rc = _run_host(
            "docker ps --format '{{.Ports}}' 2>/dev/null",
            timeout=10
        )
        if rc != 0:
            return conflicts

        used_ports = set()
        for line in out.strip().split('\n'):
            for mapping in line.split(','):
                mapping = mapping.strip()
                # Format: 0.0.0.0:8080->80/tcp
                m = re.search(r':(\d+)->', mapping)
                if m:
                    used_ports.add(int(m.group(1)))

        for port in needed_ports:
            if port in used_ports:
                conflicts.append(f'Port {port} jest już zajęty przez inny kontener')
    except Exception:
        pass
    return conflicts


def _get_cache_stats():
    """Return cache statistics."""
    stats = {
        'catalog_exists': os.path.isfile(CATALOG_FILE),
        'catalog_age_seconds': None,
        'catalog_size_bytes': None,
        'catalog_app_count': None,
        'repo_cache_count': 0,
        'repo_cache_size_bytes': 0,
        'cache_max_age': CACHE_MAX_AGE,
    }
    if stats['catalog_exists']:
        try:
            stats['catalog_age_seconds'] = int(time.time() - os.path.getmtime(CATALOG_FILE))
            stats['catalog_size_bytes'] = os.path.getsize(CATALOG_FILE)
            with open(CATALOG_FILE) as f:
                stats['catalog_app_count'] = len(json.load(f))
        except Exception:
            pass

    repos_dir = os.path.join(CACHE_DIR, 'repos')
    if os.path.isdir(repos_dir):
        total_size = 0
        repo_count = 0
        for entry in os.scandir(repos_dir):
            if entry.is_dir():
                repo_count += 1
                for dirpath, _dirs, files in os.walk(entry.path):
                    for f in files:
                        try:
                            total_size += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
        stats['repo_cache_count'] = repo_count
        stats['repo_cache_size_bytes'] = total_size

    return stats


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


@appstore_bp.route('/cache/stats')
def cache_stats():
    """Return cache statistics for monitoring."""
    return jsonify(_get_cache_stats())


@appstore_bp.route('/cache/clear', methods=['POST'])
def cache_clear():
    """Clear all cached data and force re-download on next request."""
    deny = _require_admin()
    if deny:
        return deny
    try:
        if os.path.isfile(CATALOG_FILE):
            os.remove(CATALOG_FILE)
        repos_dir = os.path.join(CACHE_DIR, 'repos')
        if os.path.isdir(repos_dir):
            shutil.rmtree(repos_dir, ignore_errors=True)
        return jsonify({'ok': True, 'message': 'Cache wyczyszczony'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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


@appstore_bp.route('/validate', methods=['POST'])
def validate_install():
    """Pre-install validation: check port conflicts, policy, disk space."""
    deny = _require_admin()
    if deny:
        return deny
    if not _docker_available():
        return jsonify({'error': 'Docker nie jest zainstalowany.'}), 503

    data = request.json or {}
    app_id = data.get('app_id', '').strip()
    if not app_id:
        return jsonify({'error': 'app_id required'}), 400

    cat = _get_catalog()
    app = next((a for a in cat if a['id'] == app_id), None)
    if not app:
        return jsonify({'error': 'App not found'}), 404

    compose_override = data.get('compose_override', '').strip()
    options_override = data.get('options_override')

    adapt_warnings = []
    if compose_override:
        adapted = compose_override
    else:
        compose_path = app.get('compose_path', '')
        if not compose_path or not os.path.isfile(compose_path):
            return jsonify({'error': 'Compose file missing'}), 500
        with open(compose_path) as f:
            raw = f.read()
        adapted, adapt_warnings = _adapt_compose(raw, app_id)
    
    if options_override:
        adapted = _apply_editable_config(adapted, options_override)

    warnings = []
    errors = []

    # Surface any options that were automatically stripped during adaptation
    for w in adapt_warnings:
        warnings.append({'type': 'adapt_removed', 'message': w})

    # Policy check
    policy_err = _validate_compose_policy(adapted)
    if policy_err:
        errors.append({'type': 'policy', 'message': policy_err})

    # Port conflict check
    port_conflicts = _check_port_conflicts(adapted)
    for conflict in port_conflicts:
        warnings.append({'type': 'port_conflict', 'message': conflict})

    # Check if already installed
    dir_name, _ = _safe_compose_dir(app_id)
    if dir_name:
        installed = _get_installed_apps()
        if dir_name in installed:
            warnings.append({'type': 'already_installed', 'message': f'{app_id} jest już zainstalowana'})

    # Disk space check
    try:
        st = os.statvfs(_compose_root())
        free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
        if free_gb < 1.0:
            errors.append({'type': 'disk_space', 'message': f'Za mało miejsca na dysku ({free_gb:.1f} GB wolnego)'})
        elif free_gb < 5.0:
            warnings.append({'type': 'disk_space', 'message': f'Niski poziom wolnego miejsca ({free_gb:.1f} GB)'})
    except Exception:
        pass

    return jsonify({
        'ok': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'adapted_compose': adapted,
    })


def _bg_install(task_id, app_id, app_title, adapted, dir_name, host_app_dir, container_app_dir):
    """Background: write compose, pull, up — emit progress via SocketIO."""
    try:
        # Step 1: Write compose file
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'prepare',
                        'percent': 5, 'message': 'Przygotowywanie plików…'})
        os.makedirs(container_app_dir, exist_ok=True)

        # Atomic write of compose file
        compose_path = os.path.join(container_app_dir, 'docker-compose.yml')
        fd, tmp_compose = tempfile.mkstemp(dir=container_app_dir, suffix='.yml.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(adapted)
            os.replace(tmp_compose, compose_path)
        except Exception:
            try:
                os.unlink(tmp_compose)
            except OSError:
                pass
            raise

        _run_host(f'mkdir -p {_apps_root()}/{dir_name}')

        compose_filename = 'docker-compose.yml'
        compose_files = [compose_filename]

        sandbox_override, sb_err = _ensure_sandbox_override(dir_name, host_app_dir, compose_filename)
        if sb_err:
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                            'percent': 100, 'message': sb_err})
            return
        if sandbox_override:
            compose_files.append(os.path.basename(sandbox_override))
        files_arg = _compose_files_args(compose_files)

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
            f'docker compose {files_arg} pull', cwd=host_app_dir, on_line=on_pull_line, timeout=600
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
            f'docker compose {files_arg} up -d --remove-orphans', cwd=host_app_dir, on_line=on_up_line, timeout=300
        )
        if up_rc != 0:
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                            'percent': 100, 'message': f'Błąd uruchamiania: {up_output[-300:]}'})
            return

        # Step 4: Verify containers started
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'verify',
                        'percent': 95, 'message': 'Weryfikacja kontenerów…'})
        time.sleep(2)
        verify_out, _, verify_rc = _run_host(
            f'docker compose {files_arg} ps --format json', cwd=host_app_dir, timeout=15
        )
        running_ok = True
        if verify_rc == 0 and verify_out.strip():
            for line in verify_out.strip().split('\n'):
                try:
                    cinfo = json.loads(line)
                    state = cinfo.get('State', '').lower()
                    if state not in ('running', 'restarting'):
                        running_ok = False
                except (json.JSONDecodeError, AttributeError):
                    pass

        if not running_ok:
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'warning',
                            'percent': 100,
                            'message': f'{app_title} zainstalowana, ale niektóre kontenery mogą wymagać konfiguracji.'})
        else:
            # Done!
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'done',
                            'percent': 100, 'message': f'{app_title} zainstalowana!'})

    except Exception as e:
        log.error('Install %s failed: %s', app_id, e)
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
    options_override = data.get('options_override')

    if compose_override:
        adapted = compose_override
    else:
        compose_path = app.get('compose_path', '')
        if not compose_path or not os.path.isfile(compose_path):
            return jsonify({'error': 'Compose file missing'}), 500
        with open(compose_path) as f:
            raw = f.read()
        adapted, adapt_warnings = _adapt_compose(raw, app_id)
        if adapt_warnings:
            log.info('Adapted compose for %s — removed unsafe options: %s', app_id, adapt_warnings)

    if options_override:
        adapted = _apply_editable_config(adapted, options_override)

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


@appstore_bp.route('/reinstall', methods=['POST'])
def reinstall_app():
    """Reinstall/update an app: stop, pull new images, start."""
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

    compose_file = os.path.join(safe_dir, 'docker-compose.yml')
    if not os.path.isfile(compose_file):
        return jsonify({'error': 'Aplikacja nie jest zainstalowana'}), 404

    # If compose_override provided, update the compose file first
    compose_override = data.get('compose_override', '').strip()
    options_override = data.get('options_override')

    if compose_override or options_override:
        if compose_override:
            updated_compose = compose_override
        else:
            with open(compose_file) as f:
                updated_compose = f.read()
        
        if options_override:
            updated_compose = _apply_editable_config(updated_compose, options_override)

        policy_err = _validate_compose_policy(updated_compose)
        if policy_err:
            return jsonify({'error': policy_err}), 400
        fd, tmp_compose = tempfile.mkstemp(dir=safe_dir, suffix='.yml.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(updated_compose)
            os.replace(tmp_compose, compose_file)
        except Exception:
            try:
                os.unlink(tmp_compose)
            except OSError:
                pass
            raise

    cat = _get_catalog()
    app = next((a for a in cat if a['id'] == app_id), None)
    app_title = (app.get('title') if app else None) or app_id
    task_id = str(uuid.uuid4())[:8]

    def _bg_reinstall():
        try:
            compose_filename = os.path.basename(compose_file)
            compose_files = [compose_filename]

            sandbox_override, sb_err = _ensure_sandbox_override(dir_name, safe_dir, compose_filename)
            if sb_err:
                _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                                'percent': 100, 'message': sb_err})
                return
            if sandbox_override:
                compose_files.append(os.path.basename(sandbox_override))
            files_arg = _compose_files_args(compose_files)

            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'prepare',
                            'percent': 5, 'message': 'Zatrzymywanie kontenerów…'})
            _run_host_stream(f'docker compose {files_arg} down', cwd=safe_dir, timeout=120)

            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'pull',
                            'percent': 20, 'message': 'Pobieranie nowych obrazów…'})
            pull_out, pull_rc = _run_host_stream(f'docker compose {files_arg} pull', cwd=safe_dir, timeout=600)
            if pull_rc != 0:
                _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                                'percent': 100, 'message': f'Błąd: {pull_out[-300:]}'})
                return

            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'start',
                            'percent': 80, 'message': 'Uruchamianie kontenerów…'})
            up_out, up_rc = _run_host_stream(f'docker compose {files_arg} up -d --remove-orphans', cwd=safe_dir, timeout=300)
            if up_rc != 0:
                _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                                'percent': 100, 'message': f'Błąd: {up_out[-300:]}'})
                return

            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'done',
                            'percent': 100, 'message': f'{app_title} zaktualizowana!'})
        except Exception as e:
            log.error('Reinstall %s failed: %s', app_id, e)
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                            'percent': 100, 'message': f'Błąd: {str(e)}'})

    if _socketio:
        _socketio.start_background_task(_bg_reinstall)
    else:
        _bg_reinstall()

    return jsonify({'ok': True, 'task_id': task_id, 'message': 'Reinstall started'})


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
    adapted, adapt_warnings = _adapt_compose(raw, app_id)
    config = _extract_editable_config(adapted)
    return jsonify({'compose': adapted, 'compose_raw': raw, 'adapt_warnings': adapt_warnings, 'config': config})
