"""
EthOS — Docker Manager (Portainer-like)
Full Docker & Docker-Compose management: containers, projects, images, logs.

Backup & Restore endpoints:
  GET    /api/docker/backups                   — list all backups
  GET    /api/docker/backups/<id>              — backup details
  POST   /api/docker/backups                   — create backup (bg, SocketIO: docker_backup)
  DELETE /api/docker/backups/<id>              — delete backup
  POST   /api/docker/backups/<id>/restore      — restore from backup (bg, SocketIO: docker_restore)

Settings endpoints:
  GET    /api/docker/settings                  — get current settings (compose_dir)
  POST   /api/docker/settings                  — save settings (compose_dir)
"""

import os
import json
import shutil
import subprocess
import re
import sys
from functools import wraps
from flask import Blueprint, request, jsonify, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_base, host_path, NATIVE_MODE, check_dep, ensure_dep, \
    get_data_disk as _get_data_disk, data_path as _data_path
from utils import docker_available as _docker_available_util, run_host, \
    find_compose_projects as _find_compose_projects_util, register_pkg_routes, get_ethos_user, \
    load_json as _load_json, save_json as _save_json
from audit import audit_log

# Import sandbox policy helper — used to apply resource limits to containers
try:
    from blueprints.sandbox_policy import get_effective_policy as _get_sandbox_policy
except ImportError:
    def _get_sandbox_policy(_name):
        return {}

docker_bp = Blueprint('docker_mgr', __name__, url_prefix='/api/docker')

# Default when no path is explicitly configured
_DEFAULT_COMPOSE_ROOT = f'/home/{get_ethos_user()}/docker'

# Projects that cannot be stopped/deleted via the UI (self-protection)
_PROTECTED_PROJECTS = {'nasos'}
_SANDBOX_OVERRIDE_FILENAME = 'docker-compose.ethos-sandbox.yml'
_DEFAULT_COMPOSE_OVERRIDES = ('docker-compose.override.yml', 'docker-compose.override.yaml')

_CONFIG_FILE = _data_path('docker_manager.json')


def _load_settings():
    return _load_json(_CONFIG_FILE, {})


def _save_settings(cfg):
    _save_json(_CONFIG_FILE, cfg)


def _compose_root():
    """Return compose projects directory — always from settings, never auto-detected."""
    cfg = _load_settings()
    custom = cfg.get('compose_dir', '').strip()
    if custom and os.path.isabs(custom):
        return custom
    return _DEFAULT_COMPOSE_ROOT


def _docker_available():
    """Check if Docker is installed and daemon is running."""
    return _docker_available_util()


def _require_docker(f):
    """Decorator: return 503 if Docker daemon is not reachable."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _docker_available():
            return jsonify({'error': 'Docker is not installed or not running'}), 503
        return f(*args, **kwargs)
    return decorated


def _require_admin(f):
    """Decorator: return 403 if the current user is not an admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if getattr(g, 'role', None) != 'admin':
            return jsonify({'error': 'Permission denied — admin role required'}), 403
        return f(*args, **kwargs)
    return decorated


# Container actions that mutate or destroy — require admin role
_DESTRUCTIVE_CONTAINER_ACTIONS = {'stop', 'kill', 'remove'}

# Project (compose) actions that stop or tear down services — require admin role
_DESTRUCTIVE_PROJECT_ACTIONS = {'down', 'stop'}


def _sanitize_shell_arg(value):
    """Sanitize a value for safe interpolation into a shell command string.
    Only allow alphanumeric, dash, underscore, dot, colon, slash."""
    return re.sub(r'[^a-zA-Z0-9_\-\.:/]', '', str(value))


def _sio():
    return getattr(docker_bp, '_socketio', None)


@docker_bp.route('/status')
def docker_status():
    """Check Docker availability. Returns install instructions if missing."""
    available = _docker_available()
    return jsonify({
        'available': available,
        'message': None if available else 'Docker is not installed. Install it to manage containers.'
    })


_docker_installing = False   # guard against concurrent installs


def _bg_install_docker():
    global _docker_installing
    s = _sio()
    def emit(stage, pct, msg, status='running'):
        if s:
            s.emit('docker_install', {'stage': stage, 'percent': pct, 'message': msg, 'status': status})

    try:
        # Pre-flight: check available disk space on root
        try:
            st = os.statvfs('/')
            free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
            if free_mb < 500:
                emit('error', 0,
                     f'Za mało miejsca na dysku: {free_mb} MB wolne, potrzeba minimum 500 MB. '
                     f'Zwolnij miejsce i spróbuj ponownie.', 'error')
                return
        except Exception:
            pass

        emit('start', 5, 'Pobieranie skryptu instalacyjnego...')
        r = _host_run_base('curl -fsSL https://get.docker.com | sh', timeout=360)
        if r.returncode != 0:
            emit('error', 0, f'Instalacja nie powiodła się: {r.stderr[-300:]}', 'error')
            return

        # Redirect Docker data to data partition if available (like Synology)
        # This prevents Docker images/containers from filling up the root partition
        data_root = '/mnt/data/docker'
        if os.path.ismount('/mnt/data'):
            emit('start_service', 70, 'Konfigurowanie magazynu Docker na dysku danych...')
            os.makedirs(data_root, exist_ok=True)
            daemon_cfg = '/etc/docker/daemon.json'
            try:
                import json as _json
                existing = {}
                if os.path.isfile(daemon_cfg):
                    with open(daemon_cfg) as _f:
                        existing = _json.load(_f)
                existing['data-root'] = data_root
                with open(daemon_cfg, 'w') as _f:
                    _json.dump(existing, _f, indent=2)
                emit('start_service', 75, f'Dane Docker będą przechowywane w {data_root}')
            except Exception as _e:
                emit('start_service', 75, f'Uwaga: nie udało się skonfigurować data-root: {_e}')

        emit('start_service', 80, 'Uruchamianie usługi Docker...')
        _host_run_base('systemctl enable docker && systemctl start docker', timeout=30)
        _host_run_base('apt-get clean 2>/dev/null', timeout=30)
        if _docker_available():
            emit('done', 100, 'Docker zainstalowany pomyślnie.', 'done')
        else:
            emit('error', 0, 'Instalacja zakończona, ale Docker niedostępny — sprawdź logi systemd.', 'error')
    except Exception as e:
        emit('error', 0, f'Błąd instalacji: {e}', 'error')
    finally:
        _docker_installing = False


@docker_bp.route('/install', methods=['POST'])
def docker_install():
    """Install Docker Engine via get.docker.com — runs in background, progress via SocketIO docker_install."""
    global _docker_installing
    if _docker_available():
        return jsonify({'status': 'ok', 'installed': True})
    if _docker_installing:
        return jsonify({'status': 'started', 'message': 'Instalacja już w toku…'})
    _docker_installing = True
    s = _sio()
    if s:
        s.start_background_task(_bg_install_docker)
        return jsonify({'status': 'started'})
    # Fallback: blocking install (no socketio)
    _docker_installing = False
    ok, msg = ensure_dep('docker', install=True)
    if ok:
        return jsonify({'status': 'ok'})
    return jsonify({'ok': False, 'error': msg}), 500

# ─── helpers ─────────────────────────────────────────────────

def _run(cmd, timeout=15, cwd=None):
    """Run a command, return (stdout, stderr, returncode)."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return r.stdout, r.stderr, r.returncode


def _run_host(cmd_str, timeout=120, cwd=None):
    """Run a command on the HOST."""
    return run_host(cmd_str, timeout=timeout, cwd=cwd)


def _json_lines(stdout):
    """Parse docker's JSON-per-line output."""
    items = []
    for line in stdout.strip().split('\n'):
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return items


# ═══════════════════════════════════════════════════════════
#  CONTAINERS
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/containers')
@_require_docker
def list_containers():
    """List all containers with detailed info."""
    try:
        fmt = '{{json .}}'
        out, err, rc = _run(['docker', 'ps', '-a', '--format', fmt, '--no-trunc'])
        if rc != 0:
            return jsonify({'error': err}), 500

        containers = []
        for raw in _json_lines(out):
            c = {
                'id': raw.get('ID', '')[:12],
                'id_full': raw.get('ID', ''),
                'name': raw.get('Names', ''),
                'image': raw.get('Image', ''),
                'status': raw.get('Status', ''),
                'state': raw.get('State', ''),
                'ports': raw.get('Ports', ''),
                'created': raw.get('CreatedAt', ''),
                'command': raw.get('Command', ''),
                'networks': raw.get('Networks', ''),
                'mounts': raw.get('Mounts', ''),
                'labels': raw.get('Labels', ''),
            }
            # Extract compose project from labels
            labels = c.get('labels', '')
            project = ''
            for lbl in labels.split(','):
                if lbl.startswith('com.docker.compose.project='):
                    project = lbl.split('=', 1)[1]
                    break
            c['project'] = project

            # Extract compose service
            service = ''
            for lbl in labels.split(','):
                if lbl.startswith('com.docker.compose.service='):
                    service = lbl.split('=', 1)[1]
                    break
            c['service'] = service
            containers.append(c)

        return jsonify(containers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/containers/<container_id>/action', methods=['POST'])
@_require_docker
def container_action(container_id):
    """Perform an action on a container."""
    data = request.get_json(force=True) if request.data else {}
    action = data.get('action', '')
    if action not in ('start', 'stop', 'restart', 'pause', 'unpause', 'remove', 'kill'):
        return jsonify({'error': 'Invalid action'}), 400

    if action in _DESTRUCTIVE_CONTAINER_ACTIONS and getattr(g, 'role', None) != 'admin':
        return jsonify({'error': 'Permission denied — only admin can perform this action'}), 403

    cmd_map = {'remove': 'rm'}
    cmd = cmd_map.get(action, action)
    args = ['docker', cmd]
    if action == 'remove':
        args.append('-f')
    args.append(container_id)

    try:
        out, err, rc = _run(args, timeout=30)
        if rc == 0:
            audit_log('docker.container.action', f'Container "{container_id}" action: {action}')
            return jsonify({'ok': True})
        return jsonify({'error': err.strip()}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/containers/<container_id>/logs')
@_require_docker
def container_logs(container_id):
    """Get container logs. ?lines=200&since=1h&search=text"""
    lines = request.args.get('lines', '200')
    since = request.args.get('since', '')
    search = request.args.get('search', '').lower()

    args = ['docker', 'logs', '--tail', lines, '--timestamps']
    if since:
        args += ['--since', since]
    args.append(container_id)

    try:
        out, err, rc = _run(args, timeout=15)
        # Docker logs go to both stdout and stderr
        combined = out + err
        log_lines = combined.strip().split('\n') if combined.strip() else []
        if search:
            log_lines = [l for l in log_lines if search in l.lower()]
        return jsonify({'logs': log_lines[-int(lines):], 'total': len(log_lines)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/containers/<container_id>/inspect')
@_require_docker
def container_inspect(container_id):
    """Get detailed container inspect data."""
    try:
        out, err, rc = _run(['docker', 'inspect', container_id])
        if rc != 0:
            return jsonify({'error': err.strip()}), 500
        data = json.loads(out)
        if not data:
            return jsonify({'error': 'Not found'}), 404

        info = data[0]
        config = info.get('Config', {})
        host_config = info.get('HostConfig', {})
        net_settings = info.get('NetworkSettings', {})
        state = info.get('State', {})

        # Parse ports
        ports = []
        port_bindings = host_config.get('PortBindings') or {}
        for container_port, bindings in port_bindings.items():
            if bindings:
                for b in bindings:
                    ports.append({
                        'container': container_port,
                        'host': f"{b.get('HostIp', '0.0.0.0')}:{b.get('HostPort', '')}",
                    })

        # Parse volumes/mounts
        mounts = []
        for m in info.get('Mounts', []):
            mounts.append({
                'type': m.get('Type', ''),
                'source': m.get('Source', ''),
                'destination': m.get('Destination', ''),
                'mode': m.get('Mode', ''),
                'rw': m.get('RW', True),
            })

        # Parse environment
        env = config.get('Env', [])

        # Parse networks
        networks = []
        for name, net in (net_settings.get('Networks') or {}).items():
            networks.append({
                'name': name,
                'ip': net.get('IPAddress', ''),
                'gateway': net.get('Gateway', ''),
                'mac': net.get('MacAddress', ''),
            })

        result = {
            'id': info.get('Id', '')[:12],
            'name': info.get('Name', '').lstrip('/'),
            'image': config.get('Image', ''),
            'created': info.get('Created', ''),
            'state': {
                'status': state.get('Status', ''),
                'running': state.get('Running', False),
                'paused': state.get('Paused', False),
                'started': state.get('StartedAt', ''),
                'finished': state.get('FinishedAt', ''),
                'exit_code': state.get('ExitCode', 0),
                'restart_count': host_config.get('RestartPolicy', {}).get('MaximumRetryCount', 0),
                'pid': state.get('Pid', 0),
            },
            'command': ' '.join(config.get('Cmd', []) or []),
            'entrypoint': ' '.join(config.get('Entrypoint', []) or []),
            'working_dir': config.get('WorkingDir', ''),
            'user': config.get('User', ''),
            'hostname': config.get('Hostname', ''),
            'restart_policy': host_config.get('RestartPolicy', {}),
            'network_mode': host_config.get('NetworkMode', ''),
            'privileged': host_config.get('Privileged', False),
            'ports': ports,
            'mounts': mounts,
            'env': env,
            'networks': networks,
            'labels': config.get('Labels', {}),
        }
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/containers/<container_id>/stats')
@_require_docker
def container_stats(container_id):
    """Get CPU/memory stats for a single container (one-shot)."""
    try:
        out, err, rc = _run([
            'docker', 'stats', '--no-stream', '--format',
            '{"cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}","mem_perc":"{{.MemPerc}}","net":"{{.NetIO}}","block":"{{.BlockIO}}","pids":"{{.PIDs}}"}',
            container_id
        ], timeout=10)
        if rc != 0:
            return jsonify({'error': err.strip()}), 500
        data = _json_lines(out)
        return jsonify(data[0] if data else {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════
#  IMAGES
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/images')
@_require_docker
def list_images():
    """List all Docker images."""
    try:
        out, err, rc = _run([
            'docker', 'images', '--format',
            '{"id":"{{.ID}}","repository":"{{.Repository}}","tag":"{{.Tag}}","size":"{{.Size}}","created":"{{.CreatedAt}}"}'
        ])
        if rc != 0:
            return jsonify({'error': err}), 500
        images = _json_lines(out)
        return jsonify(images)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/images/<image_id>', methods=['DELETE'])
@_require_docker
def delete_image(image_id):
    """Remove a Docker image."""
    force = request.args.get('force', 'false') == 'true'
    args = ['docker', 'rmi']
    if force:
        args.append('-f')
    args.append(image_id)
    try:
        out, err, rc = _run(args, timeout=30)
        if rc == 0:
            return jsonify({'ok': True})
        return jsonify({'error': err.strip()}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/images/prune', methods=['POST'])
@_require_docker
def prune_images():
    """Remove unused images."""
    try:
        out, err, rc = _run(['docker', 'image', 'prune', '-af'], timeout=60)
        if rc == 0:
            return jsonify({'ok': True, 'output': out.strip()})
        return jsonify({'error': err.strip()}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  NETWORKS
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/networks')
@_require_docker
def list_networks():
    """List Docker networks."""
    try:
        out, err, rc = _run([
            'docker', 'network', 'ls', '--format',
            '{"id":"{{.ID}}","name":"{{.Name}}","driver":"{{.Driver}}","scope":"{{.Scope}}"}'
        ])
        if rc != 0:
            return jsonify({'error': err}), 500
        return jsonify(_json_lines(out))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  VOLUMES
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/volumes')
@_require_docker
def list_volumes():
    """List Docker volumes."""
    try:
        out, err, rc = _run([
            'docker', 'volume', 'ls', '--format',
            '{"name":"{{.Name}}","driver":"{{.Driver}}","mountpoint":"{{.Mountpoint}}"}'
        ])
        if rc != 0:
            return jsonify({'error': err}), 500
        return jsonify(_json_lines(out))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/volumes/prune', methods=['POST'])
@_require_docker
def prune_volumes():
    """Remove unused volumes."""
    try:
        out, err, rc = _run(['docker', 'volume', 'prune', '-af'], timeout=60)
        if rc == 0:
            return jsonify({'ok': True, 'output': out.strip()})
        return jsonify({'error': err.strip()}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  SYSTEM
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/system')
@_require_docker
def docker_system_info():
    """Get Docker system info and disk usage summary."""
    try:
        # docker info
        out, err, rc = _run(['docker', 'info', '--format', '{{json .}}'], timeout=10)
        info = json.loads(out) if rc == 0 else {}

        # docker system df
        df_out, _, df_rc = _run([
            'docker', 'system', 'df', '--format',
            '{"type":"{{.Type}}","total":"{{.TotalCount}}","active":"{{.Active}}","size":"{{.Size}}","reclaimable":"{{.Reclaimable}}"}'
        ], timeout=10)
        disk = _json_lines(df_out) if df_rc == 0 else []

        return jsonify({
            'version': info.get('ServerVersion', ''),
            'os': info.get('OperatingSystem', ''),
            'kernel': info.get('KernelVersion', ''),
            'arch': info.get('Architecture', ''),
            'cpus': info.get('NCPU', 0),
            'memory': info.get('MemTotal', 0),
            'containers': info.get('Containers', 0),
            'containers_running': info.get('ContainersRunning', 0),
            'containers_stopped': info.get('ContainersStopped', 0),
            'containers_paused': info.get('ContainersPaused', 0),
            'images': info.get('Images', 0),
            'storage_driver': info.get('Driver', ''),
            'disk_usage': disk,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/settings')
def get_settings():
    """Return current docker manager settings."""
    cfg = _load_settings()
    return jsonify({
        'compose_dir': cfg.get('compose_dir', ''),
        'default_compose_dir': _DEFAULT_COMPOSE_ROOT,
        'effective_compose_dir': _compose_root(),
    })


@docker_bp.route('/settings', methods=['POST'])
@_require_docker
def save_settings():
    """Save docker manager settings. Admin only."""
    if getattr(g, 'role', None) != 'admin':
        return jsonify({'error': 'Permission denied — admin role required'}), 403
    data = request.get_json(force=True) if request.data else {}
    compose_dir = data.get('compose_dir', '').strip()
    if compose_dir and not os.path.isabs(compose_dir):
        return jsonify({'error': 'compose_dir must be an absolute path'}), 400
    if compose_dir and not os.path.isdir(compose_dir):
        return jsonify({'error': f'Directory does not exist: {compose_dir}'}), 400
    cfg = _load_settings()
    if compose_dir:
        cfg['compose_dir'] = compose_dir
    else:
        cfg.pop('compose_dir', None)
    _save_settings(cfg)
    audit_log('docker_settings_saved', {'compose_dir': compose_dir or '(default)'})
    return jsonify({'ok': True, 'effective_compose_dir': _compose_root()})



# ── Sub-module routes (compose projects + backup/restore) ──
from blueprints.docker_manager_compose import *  # noqa: F401, F403
from blueprints.docker_manager_backup import *  # noqa: F401, F403

# ── Package: uninstall / pkg-status ──

register_pkg_routes(
    docker_bp,
    install_message='Docker Manager ready.',
    status_extras=lambda: {'docker_available': check_dep('docker')},
)
