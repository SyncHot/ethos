"""
EthOS — Docker Manager (Portainer-like)
Full Docker & Docker-Compose management: containers, projects, images, logs.

Backup & Restore endpoints:
  GET    /api/docker/backups                   — list all backups
  GET    /api/docker/backups/<id>              — backup details
  POST   /api/docker/backups                   — create backup (bg, SocketIO: docker_backup)
  DELETE /api/docker/backups/<id>              — delete backup
  POST   /api/docker/backups/<id>/restore      — restore from backup (bg, SocketIO: docker_restore)
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
    get_data_disk as _get_data_disk
from utils import docker_available as _docker_available_util, run_host, \
    find_compose_projects as _find_compose_projects_util, register_pkg_routes, get_ethos_user
from audit import audit_log

# Import sandbox policy helper — used to apply resource limits to containers
try:
    from blueprints.sandbox_policy import get_effective_policy as _get_sandbox_policy
except ImportError:
    def _get_sandbox_policy(_name):
        return {}

docker_bp = Blueprint('docker_mgr', __name__, url_prefix='/api/docker')

# Fallback defaults when no data disk is configured
_DEFAULT_COMPOSE_ROOT = f'/home/{get_ethos_user()}/docker'

# Projects that cannot be stopped/deleted via the UI (self-protection)
_PROTECTED_PROJECTS = {'nasos'}
_SANDBOX_OVERRIDE_FILENAME = 'docker-compose.ethos-sandbox.yml'
_DEFAULT_COMPOSE_OVERRIDES = ('docker-compose.override.yml', 'docker-compose.override.yaml')

# Where compose projects live
def _compose_root():
    dd = _get_data_disk()
    if dd:
        p = os.path.join(dd, 'apps', 'compose')
        os.makedirs(p, mode=0o755, exist_ok=True)
        return p
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
#  DOCKER-COMPOSE PROJECTS
# ═══════════════════════════════════════════════════════════

def _find_compose_projects():
    """Scan compose root for directories with docker-compose files."""
    return _find_compose_projects_util(_compose_root())


def _compose_files_for_project(project_path, main_filename, sandbox_override=None):
    """Return compose file sequence (main, user overrides, sandbox override)."""
    files = [main_filename]
    for override in _DEFAULT_COMPOSE_OVERRIDES:
        if os.path.isfile(os.path.join(project_path, override)):
            files.append(override)
    if sandbox_override:
        files.append(os.path.basename(sandbox_override))
    return files


def _list_compose_services(project_path, compose_files):
    """List services defined in compose files."""
    file_args = ' '.join(f'-f {f}' for f in compose_files)
    try:
        out, err, rc = _run_host(f'docker compose {file_args} config --services', timeout=60, cwd=project_path)
    except Exception as exc:  # noqa: BLE001
        return [], f'Compose services error: {exc}'
    if rc != 0:
        return [], err.strip() or 'docker compose config failed'
    services = [line.strip() for line in out.split('\n') if line.strip()]
    return services, None


def _policy_to_service_limits(policy):
    """Translate sandbox policy into docker-compose service options."""
    service_config = {}
    
    # Initialize deploy structure
    deploy = {'resources': {'limits': {}, 'reservations': {}}}
    has_deploy_limits = False
    has_deploy_reservations = False

    mem_limit = str(policy.get('mem_limit', '')).strip()
    if mem_limit and mem_limit != '0':
        deploy['resources']['limits']['memory'] = mem_limit
        has_deploy_limits = True

    mem_reservation = str(policy.get('mem_reservation', '')).strip()
    if mem_reservation and mem_reservation != '0':
        deploy['resources']['reservations']['memory'] = mem_reservation
        has_deploy_reservations = True

    cpu_quota = policy.get('cpu_quota', 0)
    try:
        cpu_quota = float(cpu_quota)
    except (TypeError, ValueError):
        cpu_quota = 0
    if cpu_quota > 0:
        deploy['resources']['limits']['cpus'] = round(cpu_quota / 100.0, 3)
        has_deploy_limits = True

    cpu_shares = policy.get('cpu_shares', 1024)
    try:
        cpu_shares = int(cpu_shares)
    except (TypeError, ValueError):
        cpu_shares = 1024
    if cpu_shares > 0 and cpu_shares != 1024:
        service_config['cpu_shares'] = cpu_shares

    pids_limit = policy.get('pids_limit', 0)
    try:
        pids_limit = int(pids_limit)
    except (TypeError, ValueError):
        pids_limit = 0
    if pids_limit > 0:
        deploy['resources']['limits']['pids'] = pids_limit
        has_deploy_limits = True

    # Clean up empty sections
    if not has_deploy_limits:
        if 'limits' in deploy['resources']:
            del deploy['resources']['limits']
    if not has_deploy_reservations:
        if 'reservations' in deploy['resources']:
            del deploy['resources']['reservations']
    if not deploy['resources']:
        del deploy['resources']
    
    if has_deploy_limits or has_deploy_reservations:
        service_config['deploy'] = deploy

    if policy.get('read_only_root'):
        service_config['read_only'] = True

    if policy.get('no_new_privileges'):
        service_config['security_opt'] = ['no-new-privileges:true']

    cap_drop = policy.get('cap_drop') or []
    if cap_drop:
        service_config['cap_drop'] = cap_drop

    cap_add = policy.get('cap_add') or []
    if cap_add:
        service_config['cap_add'] = cap_add

    return service_config


def _ensure_sandbox_override(project_name, project_path, main_filename):
    """Create/update sandbox override compose file with enforced limits."""
    policy = _get_sandbox_policy(project_name) or {}
    compose_files = _compose_files_for_project(project_path, main_filename)
    services, err = _list_compose_services(project_path, compose_files)
    if err:
        return None, f'Error reading compose services: {err}'
    if not services:
        return None, 'No services in docker-compose file'

    limits = _policy_to_service_limits(policy)
    if not limits:
        return None, None

    override = {'version': '3', 'services': {svc: dict(limits) for svc in services}}
    override_path = os.path.join(project_path, _SANDBOX_OVERRIDE_FILENAME)

    try:
        try:
            import yaml  # type: ignore
            with open(override_path, 'w') as f:
                yaml.safe_dump(override, f, sort_keys=False)
        except ImportError:
            with open(override_path, 'w') as f:
                json.dump(override, f, indent=2)
    except OSError as exc:
        return None, f'Cannot write sandbox policy file: {exc}'

    return override_path, None


@docker_bp.route('/projects')
@_require_docker
def list_projects():
    """List all docker-compose projects with status."""
    try:
        projects = _find_compose_projects()

        # Get running containers with their compose project label
        out, _, _ = _run([
            'docker', 'ps', '-a', '--format',
            '{{.Label "com.docker.compose.project"}}|{{.Names}}|{{.State}}|{{.Status}}|{{.Image}}|{{.Ports}}|{{.Label "com.docker.compose.service"}}'
        ])

        # Build per-project container map
        project_containers = {}
        for line in out.strip().split('\n'):
            if not line or '|' not in line:
                continue
            parts = line.split('|', 6)
            if len(parts) < 7:
                continue
            proj, name, state, status, image, ports, service = parts
            if proj:
                project_containers.setdefault(proj, []).append({
                    'name': name,
                    'state': state,
                    'status': status,
                    'image': image,
                    'ports': ports,
                    'service': service or name,
                })

        result = []
        for p in projects:
            name = p['name']
            containers = project_containers.get(name, [])
            running = sum(1 for c in containers if c['state'] == 'running')
            total = len(containers)

            if total == 0:
                status = 'stopped'
            elif running == total:
                status = 'running'
            elif running > 0:
                status = 'partial'
            else:
                status = 'stopped'

            result.append({
                'name': name,
                'path': p['path'],
                'compose_file': p['compose_filename'],
                'status': status,
                'containers': containers,
                'running': running,
                'total': total,
                'protected': name in _PROTECTED_PROJECTS,
            })

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/projects/<project_name>/action', methods=['POST'])
@_require_docker
def project_action(project_name):
    """Perform docker-compose action: up, down, restart, pull, build."""
    data = request.get_json(force=True) if request.data else {}
    action = data.get('action', '')
    if action not in ('up', 'down', 'restart', 'pull', 'build', 'stop', 'start'):
        return jsonify({'error': 'Invalid action'}), 400

    if action in _DESTRUCTIVE_PROJECT_ACTIONS and getattr(g, 'role', None) != 'admin':
        return jsonify({'error': 'Permission denied — only admin can perform this action'}), 403

    # Protect critical projects from destructive actions
    if project_name in _PROTECTED_PROJECTS and action in ('down', 'stop', 'remove'):
        return jsonify({'error': f'Project "{project_name}" is protected — cannot stop from interface'}), 403

    projects = _find_compose_projects()
    project = next((p for p in projects if p['name'] == project_name), None)
    if not project:
        return jsonify({'error': f'Project {project_name} not found'}), 404

    # Use the real host path for docker compose (runs via nsenter on host)
    host_path = os.path.join(_compose_root(), project_name)

    sandbox_override = None
    compose_files = None
    if action in ('up', 'start', 'restart'):
        sandbox_override, err = _ensure_sandbox_override(project_name, host_path, project['compose_filename'])
        if err:
            return jsonify({'error': err}), 500
        compose_files = _compose_files_for_project(host_path, project['compose_filename'], sandbox_override)

    cmd_map = {
        'up': 'docker compose up -d',
        'down': 'docker compose down',
        'restart': 'docker compose restart',
        'pull': 'docker compose pull',
        'build': 'docker compose build --no-cache',
        'stop': 'docker compose stop',
        'start': 'docker compose start',
    }

    cmd_str = cmd_map[action]
    if compose_files:
        files_arg = ' '.join(f'-f {f}' for f in compose_files)
        cmd_str = f'docker compose {files_arg} up -d --force-recreate --remove-orphans'
    try:
        out, err, rc = _run_host(cmd_str, timeout=120, cwd=host_path)
        combined = (out + '\n' + err).strip()
        if rc == 0:
            return jsonify({'ok': True, 'output': combined})
        return jsonify({'error': combined}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Operation timed out (120s)'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/projects/<project_name>', methods=['DELETE'])
@_require_docker
def delete_project(project_name):
    """Delete a docker-compose project: stop containers, remove directory."""
    # Protect critical projects
    if project_name in _PROTECTED_PROJECTS:
        return jsonify({'error': f'Project "{project_name}" is protected and cannot be removed'}), 403

    projects = _find_compose_projects()
    project = next((p for p in projects if p['name'] == project_name), None)
    if not project:
        return jsonify({'error': f'Project {project_name} not found'}), 404

    project_path = os.path.join(_compose_root(), project_name)

    # Validate the path is inside compose root (prevent traversal)
    real_root = os.path.realpath(_compose_root())
    real_path = os.path.realpath(project_path)
    if not real_path.startswith(real_root + '/'):
        return jsonify({'error': 'Invalid project path'}), 400

    # First, docker compose down (stop and remove containers/networks)
    try:
        _run_host('docker compose down --remove-orphans', timeout=120, cwd=project_path)
    except Exception:
        pass  # Continue even if down fails (project may not be running)

    # Remove the project directory
    try:
        if os.path.isdir(real_path):
            shutil.rmtree(real_path)
        return jsonify({'status': 'ok', 'project': project_name})
    except Exception as e:
        return jsonify({'error': f'Directory removal error: {str(e)}'}), 500


@docker_bp.route('/projects/<project_name>/logs')
@_require_docker
def project_logs(project_name):
    """Get combined logs for a docker-compose project. ?lines=200&since=1h&search=text&service=name"""
    lines = request.args.get('lines', '200')
    since = request.args.get('since', '')
    search = request.args.get('search', '').lower()
    service = request.args.get('service', '')

    # Sanitize shell-interpolated parameters to prevent injection
    lines = _sanitize_shell_arg(lines)
    if not lines.isdigit():
        lines = '200'
    since = _sanitize_shell_arg(since) if since else ''
    service = _sanitize_shell_arg(service) if service else ''

    projects = _find_compose_projects()
    project = next((p for p in projects if p['name'] == project_name), None)
    if not project:
        return jsonify({'error': f'Project {project_name} not found'}), 404

    host_path = os.path.join(_compose_root(), project_name)
    cmd = f'docker compose logs --tail {lines} --timestamps'
    if since:
        cmd += f' --since {since}'
    if service:
        cmd += f' {service}'

    try:
        out, err, rc = _run_host(cmd, timeout=30, cwd=host_path)
        combined = out + err
        log_lines = combined.strip().split('\n') if combined.strip() else []
        if search:
            log_lines = [l for l in log_lines if search in l.lower()]
        # Sort by timestamp (docker compose logs may interleave)
        log_lines.sort()
        return jsonify({'logs': log_lines[-int(lines):], 'total': len(log_lines)})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Timeout (30s)'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/projects/<project_name>/compose', methods=['GET'])
def project_compose(project_name):
    """Read the docker-compose file content."""
    projects = _find_compose_projects()
    project = next((p for p in projects if p['name'] == project_name), None)
    if not project:
        return jsonify({'error': f'Project {project_name} not found'}), 404

    try:
        with open(project['compose_file'], 'r') as f:
            content = f.read()
        return jsonify({'content': content, 'filename': project['compose_filename']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/projects/<project_name>/compose', methods=['PUT'])
def project_compose_save(project_name):
    """Save the docker-compose file content."""
    projects = _find_compose_projects()
    project = next((p for p in projects if p['name'] == project_name), None)
    if not project:
        return jsonify({'error': f'Project {project_name} not found'}), 404

    data = request.get_json(force=True)
    content = data.get('content', '')
    if not content:
        return jsonify({'error': 'No content'}), 400

    # Validate YAML syntax before saving
    try:
        import yaml
        yaml.safe_load(content)
    except yaml.YAMLError as ye:
        return jsonify({'error': f'YAML syntax error: {str(ye)}'}), 400
    except ImportError:
        pass  # If pyyaml not installed, skip validation

    try:
        with open(project['compose_file'], 'w') as f:
            f.write(content)
        return jsonify({'ok': True})
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
#  CREATE NEW PROJECT
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/projects', methods=['POST'])
@_require_docker
def create_project():
    """Create a new docker-compose project with an initial compose file."""
    data = request.get_json(force=True) if request.data else {}
    name = data.get('name', '').strip()
    content = data.get('content', '').strip()

    if not name:
        return jsonify({'error': 'Project name is required'}), 400

    # Validate name: only alphanumeric, dash, underscore
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$', name):
        return jsonify({'error': 'Project name may only contain letters, numbers, hyphens, and underscores'}), 400

    if len(name) > 64:
        return jsonify({'error': 'Project name too long (max 64 characters)'}), 400

    root = _compose_root()
    project_path = os.path.join(root, name)

    if os.path.exists(project_path):
        return jsonify({'error': f'Project "{name}" already exists'}), 409

    # Default compose content if none provided
    if not content:
        content = f"""# {name} — docker-compose.yaml
version: '3'

services:
  app:
    image: hello-world
    restart: unless-stopped
"""

    # Validate YAML
    try:
        import yaml
        yaml.safe_load(content)
    except yaml.YAMLError as ye:
        return jsonify({'error': f'YAML syntax error: {str(ye)}'}), 400
    except ImportError:
        pass

    try:
        os.makedirs(project_path, mode=0o755, exist_ok=True)
        compose_file = os.path.join(project_path, 'docker-compose.yaml')
        with open(compose_file, 'w') as f:
            f.write(content)
        return jsonify({'status': 'ok', 'name': name, 'path': project_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  DOCKER BACKUPS — snapshot & restore containers/projects
# ═══════════════════════════════════════════════════════════

import time as _time
import hashlib as _hashlib

_backup_running = {}  # guard: {backup_id: True} for in-progress ops


def _backup_root():
    """Return the backup storage directory (prefer data disk)."""
    dd = _get_data_disk()
    if dd:
        p = os.path.join(dd, 'docker-backups')
    else:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'data', 'docker-backups')
    os.makedirs(p, mode=0o755, exist_ok=True)
    return p


def _backup_id(backup_type, name):
    """Generate a unique backup directory name."""
    ts = _time.strftime('%Y%m%d_%H%M%S')
    short = _hashlib.md5(f'{ts}{name}'.encode()).hexdigest()[:6]
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)[:40]
    return f'{ts}_{backup_type}_{safe_name}_{short}'


def _read_backup_meta(backup_dir):
    """Read metadata.json from a backup directory."""
    meta_path = os.path.join(backup_dir, 'metadata.json')
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def _write_backup_meta(backup_dir, meta):
    """Write metadata.json to a backup directory."""
    with open(os.path.join(backup_dir, 'metadata.json'), 'w') as f:
        json.dump(meta, f, indent=2)


def _dir_size_bytes(path):
    """Calculate total size of a directory in bytes."""
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _fmt_size(nbytes):
    """Human-readable size string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(nbytes) < 1024:
            return f'{nbytes:.1f} {unit}'
        nbytes /= 1024
    return f'{nbytes:.1f} PB'


def _get_container_volumes(inspect_data):
    """Extract named volumes from container inspect data."""
    volumes = []
    for m in inspect_data.get('Mounts', []):
        if m.get('Type') == 'volume':
            volumes.append({
                'name': m.get('Name', ''),
                'destination': m.get('Destination', ''),
                'driver': m.get('Driver', 'local'),
            })
    return volumes


def _get_project_volumes(project_name):
    """Get named volumes used by all containers in a compose project."""
    volumes = []
    seen = set()
    try:
        out, _, rc = _run([
            'docker', 'ps', '-a', '--filter',
            f'label=com.docker.compose.project={project_name}',
            '--format', '{{.ID}}'
        ])
        if rc != 0:
            return volumes
        for cid in out.strip().split('\n'):
            cid = cid.strip()
            if not cid:
                continue
            iout, _, irc = _run(['docker', 'inspect', cid])
            if irc != 0:
                continue
            info = json.loads(iout)
            if info:
                for v in _get_container_volumes(info[0]):
                    if v['name'] not in seen:
                        seen.add(v['name'])
                        volumes.append(v)
    except Exception:
        pass
    return volumes


def _export_volume(vol_name, dest_dir, timeout=300):
    """Export a named Docker volume to a tar.gz file. Returns (path, error)."""
    tar_path = os.path.join(dest_dir, f'{vol_name}.tar.gz')
    try:
        r = subprocess.run(
            ['docker', 'run', '--rm',
             '-v', f'{vol_name}:/volume_data:ro',
             '-v', f'{dest_dir}:/backup',
             'alpine', 'tar', 'czf', f'/backup/{vol_name}.tar.gz', '-C', '/volume_data', '.'],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            return None, r.stderr.strip()
        return tar_path, None
    except subprocess.TimeoutExpired:
        return None, 'Volume export timed out'
    except Exception as e:
        return None, str(e)


def _import_volume(vol_name, tar_path, timeout=300):
    """Import a tar.gz file into a named Docker volume. Returns error or None."""
    tar_dir = os.path.dirname(tar_path)
    tar_file = os.path.basename(tar_path)
    try:
        # Create volume if it doesn't exist
        subprocess.run(['docker', 'volume', 'create', vol_name],
                       capture_output=True, text=True, timeout=30)
        r = subprocess.run(
            ['docker', 'run', '--rm',
             '-v', f'{vol_name}:/volume_data',
             '-v', f'{tar_dir}:/backup:ro',
             'alpine', 'sh', '-c',
             f'rm -rf /volume_data/* /volume_data/..?* /volume_data/.[!.]* 2>/dev/null; '
             f'tar xzf /backup/{tar_file} -C /volume_data'],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            return r.stderr.strip()
        return None
    except subprocess.TimeoutExpired:
        return 'Volume import timed out'
    except Exception as e:
        return str(e)


# ── Backup create worker ──

def _bg_create_backup(backup_dir, meta, backup_type, target_name, mode):
    """Background worker: create a Docker backup."""
    s = _sio()
    bid = os.path.basename(backup_dir)

    def emit(pct, msg, status='running'):
        if s:
            s.emit('docker_backup', {
                'id': bid, 'percent': pct, 'message': msg, 'status': status
            })

    try:
        os.makedirs(backup_dir, exist_ok=True)

        if backup_type == 'container':
            _backup_container(backup_dir, meta, target_name, mode, emit)
        else:
            _backup_project(backup_dir, meta, target_name, mode, emit)

        # Finalize metadata
        meta['size'] = _dir_size_bytes(backup_dir)
        meta['size_human'] = _fmt_size(meta['size'])
        meta['status'] = 'complete'
        _write_backup_meta(backup_dir, meta)
        emit(100, 'Backup zakończony pomyślnie.', 'done')
        audit_log('docker.backup.create', f'Backup created: {bid} ({backup_type} {target_name})')

    except Exception as e:
        meta['status'] = 'error'
        meta['error'] = str(e)
        try:
            _write_backup_meta(backup_dir, meta)
        except Exception:
            pass
        emit(0, f'Błąd backupu: {e}', 'error')
    finally:
        _backup_running.pop(bid, None)


def _backup_container(backup_dir, meta, container_id, mode, emit):
    """Backup a single container: inspect + volumes + optional image."""
    emit(5, 'Zapisywanie konfiguracji kontenera...')

    # 1. Save container inspect
    out, err, rc = _run(['docker', 'inspect', container_id])
    if rc != 0:
        raise RuntimeError(f'docker inspect failed: {err}')
    inspect_data = json.loads(out)
    if not inspect_data:
        raise RuntimeError('Container not found')

    info = inspect_data[0]
    config_path = os.path.join(backup_dir, 'config.json')
    with open(config_path, 'w') as f:
        json.dump(info, f, indent=2)

    container_name = info.get('Name', '').lstrip('/')
    image_name = info.get('Config', {}).get('Image', '')
    meta['container_name'] = container_name
    meta['image'] = image_name

    # 2. Export volumes
    volumes = _get_container_volumes(info)
    meta['volumes'] = [v['name'] for v in volumes]

    if volumes:
        emit(15, f'Eksportowanie {len(volumes)} wolumenów...')
        vol_dir = os.path.join(backup_dir, 'volumes')
        os.makedirs(vol_dir, exist_ok=True)
        for i, vol in enumerate(volumes):
            pct = 15 + int(45 * (i / len(volumes)))
            emit(pct, f'Wolumen: {vol["name"]}...')
            _, vol_err = _export_volume(vol['name'], vol_dir)
            if vol_err:
                emit(pct, f'Ostrzeżenie: wolumen {vol["name"]}: {vol_err}')

    # 3. Optionally save image
    if mode == 'full':
        emit(65, f'Zapisywanie obrazu {image_name}...')
        img_dir = os.path.join(backup_dir, 'images')
        os.makedirs(img_dir, exist_ok=True)
        safe_img = re.sub(r'[^a-zA-Z0-9_.-]', '_', image_name)
        img_path = os.path.join(img_dir, f'{safe_img}.tar')
        try:
            r = subprocess.run(
                ['docker', 'save', '-o', img_path, image_name],
                capture_output=True, text=True, timeout=600
            )
            if r.returncode != 0:
                emit(70, f'Ostrzeżenie: nie udało się zapisać obrazu: {r.stderr.strip()[:200]}')
            else:
                meta['image_saved'] = True
        except subprocess.TimeoutExpired:
            emit(70, 'Ostrzeżenie: zapis obrazu przekroczył limit czasu')

    emit(90, 'Finalizowanie...')
    _write_backup_meta(backup_dir, meta)


def _backup_project(backup_dir, meta, project_name, mode, emit):
    """Backup a Docker Compose project: compose files + volumes + optional images."""
    emit(5, 'Zapisywanie plików compose...')

    # 1. Copy compose files
    compose_dir = os.path.join(backup_dir, 'compose')
    os.makedirs(compose_dir, exist_ok=True)

    project_path = os.path.join(_compose_root(), project_name)
    if not os.path.isdir(project_path):
        raise RuntimeError(f'Project directory not found: {project_name}')

    copied_files = []
    for fname in os.listdir(project_path):
        fpath = os.path.join(project_path, fname)
        if os.path.isfile(fpath):
            shutil.copy2(fpath, os.path.join(compose_dir, fname))
            copied_files.append(fname)
    meta['compose_files'] = copied_files

    # 2. Collect project images
    emit(10, 'Pobieranie informacji o kontenerach projektu...')
    out, _, rc = _run([
        'docker', 'ps', '-a', '--filter',
        f'label=com.docker.compose.project={project_name}',
        '--format', '{{.Image}}'
    ])
    images = list(set(line.strip() for line in out.strip().split('\n') if line.strip())) if rc == 0 else []
    meta['images'] = images

    # 3. Export volumes
    volumes = _get_project_volumes(project_name)
    meta['volumes'] = [v['name'] for v in volumes]

    if volumes:
        emit(15, f'Eksportowanie {len(volumes)} wolumenów...')
        vol_dir = os.path.join(backup_dir, 'volumes')
        os.makedirs(vol_dir, exist_ok=True)
        for i, vol in enumerate(volumes):
            pct = 15 + int(35 * (i / len(volumes)))
            emit(pct, f'Wolumen: {vol["name"]}...')
            _, vol_err = _export_volume(vol['name'], vol_dir)
            if vol_err:
                emit(pct, f'Ostrzeżenie: wolumen {vol["name"]}: {vol_err}')

    # 4. Optionally save images
    if mode == 'full' and images:
        emit(55, f'Zapisywanie {len(images)} obrazów...')
        img_dir = os.path.join(backup_dir, 'images')
        os.makedirs(img_dir, exist_ok=True)
        for i, img in enumerate(images):
            pct = 55 + int(35 * (i / len(images)))
            emit(pct, f'Obraz: {img}...')
            safe_img = re.sub(r'[^a-zA-Z0-9_.-]', '_', img)
            img_path = os.path.join(img_dir, f'{safe_img}.tar')
            try:
                r = subprocess.run(
                    ['docker', 'save', '-o', img_path, img],
                    capture_output=True, text=True, timeout=600
                )
                if r.returncode != 0:
                    emit(pct, f'Ostrzeżenie: obraz {img}: {r.stderr.strip()[:200]}')
                else:
                    meta.setdefault('images_saved', []).append(img)
            except subprocess.TimeoutExpired:
                emit(pct, f'Ostrzeżenie: zapis obrazu {img} przekroczył limit czasu')

    emit(92, 'Finalizowanie...')
    _write_backup_meta(backup_dir, meta)


# ── Restore worker ──

def _bg_restore_backup(backup_dir, meta, options):
    """Background worker: restore a Docker backup."""
    s = _sio()
    bid = os.path.basename(backup_dir)
    backup_type = meta.get('type', 'container')

    def emit(pct, msg, status='running'):
        if s:
            s.emit('docker_restore', {
                'id': bid, 'percent': pct, 'message': msg, 'status': status
            })

    try:
        if backup_type == 'container':
            _restore_container(backup_dir, meta, options, emit)
        else:
            _restore_project(backup_dir, meta, options, emit)

        emit(100, 'Przywracanie zakończone pomyślnie.', 'done')
        audit_log('docker.backup.restore', f'Backup restored: {bid} ({backup_type})')
    except Exception as e:
        emit(0, f'Błąd przywracania: {e}', 'error')
    finally:
        _backup_running.pop(bid, None)


def _restore_container(backup_dir, meta, options, emit):
    """Restore a single container from backup."""
    config_path = os.path.join(backup_dir, 'config.json')
    if not os.path.isfile(config_path):
        raise RuntimeError('Brak pliku konfiguracji kontenera')

    with open(config_path, 'r') as f:
        info = json.load(f)

    image = info.get('Config', {}).get('Image', '')
    container_name = meta.get('container_name', '') or info.get('Name', '').lstrip('/')

    # 1. Ensure image is available
    emit(5, f'Przygotowywanie obrazu {image}...')
    img_dir = os.path.join(backup_dir, 'images')
    image_loaded = False
    if os.path.isdir(img_dir):
        for tar_file in os.listdir(img_dir):
            if tar_file.endswith('.tar'):
                emit(10, f'Ładowanie obrazu z backupu...')
                r = subprocess.run(
                    ['docker', 'load', '-i', os.path.join(img_dir, tar_file)],
                    capture_output=True, text=True, timeout=600
                )
                if r.returncode == 0:
                    image_loaded = True

    if not image_loaded:
        emit(15, f'Pobieranie obrazu {image}...')
        r = subprocess.run(
            ['docker', 'pull', image],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            raise RuntimeError(f'Nie udało się pobrać obrazu {image}: {r.stderr.strip()[:200]}')

    # 2. Restore volumes
    vol_dir = os.path.join(backup_dir, 'volumes')
    if os.path.isdir(vol_dir):
        tar_files = [f for f in os.listdir(vol_dir) if f.endswith('.tar.gz')]
        if tar_files:
            emit(25, f'Przywracanie {len(tar_files)} wolumenów...')
            for i, tf in enumerate(tar_files):
                vol_name = tf.replace('.tar.gz', '')
                pct = 25 + int(30 * (i / len(tar_files)))
                emit(pct, f'Wolumen: {vol_name}...')
                err = _import_volume(vol_name, os.path.join(vol_dir, tf))
                if err:
                    emit(pct, f'Ostrzeżenie: wolumen {vol_name}: {err}')

    # 3. Build docker create command from saved config
    emit(60, 'Tworzenie kontenera...')
    config = info.get('Config', {})
    host_config = info.get('HostConfig', {})

    # Remove old container with same name if exists
    if container_name:
        subprocess.run(['docker', 'rm', '-f', container_name],
                       capture_output=True, text=True, timeout=15)

    args = ['docker', 'create']

    # Name
    if container_name:
        args += ['--name', container_name]

    # Hostname
    hostname = config.get('Hostname', '')
    if hostname:
        args += ['--hostname', hostname]

    # Environment variables
    for env_var in (config.get('Env') or []):
        args += ['-e', env_var]

    # Port bindings
    port_bindings = host_config.get('PortBindings') or {}
    for container_port, bindings in port_bindings.items():
        if bindings:
            for b in bindings:
                host_ip = b.get('HostIp', '')
                host_port = b.get('HostPort', '')
                if host_ip and host_ip != '0.0.0.0':
                    args += ['-p', f'{host_ip}:{host_port}:{container_port}']
                elif host_port:
                    args += ['-p', f'{host_port}:{container_port}']

    # Volumes/mounts
    for m in info.get('Mounts', []):
        mtype = m.get('Type', '')
        src = m.get('Source', '')
        dst = m.get('Destination', '')
        rw = m.get('RW', True)
        mode_str = 'rw' if rw else 'ro'
        if mtype == 'volume':
            vol_name = m.get('Name', '')
            if vol_name and dst:
                args += ['-v', f'{vol_name}:{dst}:{mode_str}']
        elif mtype == 'bind' and src and dst:
            args += ['-v', f'{src}:{dst}:{mode_str}']

    # Network mode
    net_mode = host_config.get('NetworkMode', '')
    if net_mode and net_mode != 'default':
        args += ['--network', net_mode]

    # Restart policy
    restart_policy = host_config.get('RestartPolicy', {})
    rp_name = restart_policy.get('Name', '')
    if rp_name and rp_name != 'no':
        max_retry = restart_policy.get('MaximumRetryCount', 0)
        if rp_name == 'on-failure' and max_retry > 0:
            args += ['--restart', f'on-failure:{max_retry}']
        else:
            args += ['--restart', rp_name]

    # Privileged
    if host_config.get('Privileged'):
        args.append('--privileged')

    # Working dir
    working_dir = config.get('WorkingDir', '')
    if working_dir:
        args += ['-w', working_dir]

    # User
    user = config.get('User', '')
    if user:
        args += ['-u', user]

    # Labels
    labels = config.get('Labels') or {}
    for k, v in labels.items():
        # Skip compose-managed labels to avoid conflicts
        if k.startswith('com.docker.compose.'):
            continue
        args += ['--label', f'{k}={v}']

    # Image + command
    args.append(image)
    cmd = config.get('Cmd') or []
    entrypoint = config.get('Entrypoint') or []
    if entrypoint:
        args_before_image = args[:-1]
        args = args_before_image + ['--entrypoint', entrypoint[0]] + [image]
        if len(entrypoint) > 1:
            args += entrypoint[1:]
        if cmd:
            args += cmd
    elif cmd:
        args += cmd

    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f'docker create failed: {r.stderr.strip()[:300]}')

    new_container_id = r.stdout.strip()[:12]

    # 4. Start the container
    emit(85, 'Uruchamianie kontenera...')
    r = subprocess.run(['docker', 'start', new_container_id],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        emit(90, f'Ostrzeżenie: kontener utworzony ale nie uruchomiony: {r.stderr.strip()[:200]}')

    emit(95, f'Kontener {container_name or new_container_id} przywrócony.')


def _restore_project(backup_dir, meta, options, emit):
    """Restore a Docker Compose project from backup."""
    compose_bak = os.path.join(backup_dir, 'compose')
    if not os.path.isdir(compose_bak):
        raise RuntimeError('Brak plików compose w backupie')

    project_name = meta.get('name', '')
    if not project_name:
        raise RuntimeError('Brak nazwy projektu w metadanych')

    # 1. Load images if available
    img_dir = os.path.join(backup_dir, 'images')
    if os.path.isdir(img_dir):
        tar_files = [f for f in os.listdir(img_dir) if f.endswith('.tar')]
        if tar_files:
            emit(5, f'Ładowanie {len(tar_files)} obrazów...')
            for i, tf in enumerate(tar_files):
                pct = 5 + int(20 * (i / len(tar_files)))
                emit(pct, f'Obraz: {tf}...')
                subprocess.run(
                    ['docker', 'load', '-i', os.path.join(img_dir, tf)],
                    capture_output=True, text=True, timeout=600
                )

    # 2. Restore compose files
    emit(30, 'Przywracanie plików compose...')
    project_path = os.path.join(_compose_root(), project_name)
    os.makedirs(project_path, mode=0o755, exist_ok=True)

    for fname in os.listdir(compose_bak):
        src = os.path.join(compose_bak, fname)
        dst = os.path.join(project_path, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)

    # 3. Restore volumes
    vol_dir = os.path.join(backup_dir, 'volumes')
    if os.path.isdir(vol_dir):
        tar_files = [f for f in os.listdir(vol_dir) if f.endswith('.tar.gz')]
        if tar_files:
            emit(40, f'Przywracanie {len(tar_files)} wolumenów...')
            for i, tf in enumerate(tar_files):
                vol_name = tf.replace('.tar.gz', '')
                pct = 40 + int(25 * (i / len(tar_files)))
                emit(pct, f'Wolumen: {vol_name}...')
                err = _import_volume(vol_name, os.path.join(vol_dir, tf))
                if err:
                    emit(pct, f'Ostrzeżenie: wolumen {vol_name}: {err}')

    # 4. Pull images (if not saved in backup) + start project
    emit(70, 'Pobieranie obrazów i uruchamianie projektu...')
    try:
        _run_host('docker compose pull', timeout=300, cwd=project_path)
    except Exception:
        pass  # May fail if images were loaded from backup

    emit(85, 'Uruchamianie projektu...')
    out, err, rc = _run_host('docker compose up -d', timeout=120, cwd=project_path)
    if rc != 0:
        combined = (out + '\n' + err).strip()
        raise RuntimeError(f'docker compose up failed: {combined[:300]}')

    emit(95, f'Projekt {project_name} przywrócony.')


# ── Backup API endpoints ──

@docker_bp.route('/backups')
@_require_docker
def list_backups():
    """List all Docker backups."""
    try:
        root = _backup_root()
        backups = []
        for entry in sorted(os.listdir(root), reverse=True):
            entry_path = os.path.join(root, entry)
            if not os.path.isdir(entry_path):
                continue
            meta = _read_backup_meta(entry_path)
            if not meta:
                continue
            meta['id'] = entry
            backups.append(meta)
        return jsonify(backups)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/backups/<backup_id>')
@_require_docker
def get_backup(backup_id):
    """Get details of a specific backup."""
    backup_id = re.sub(r'[^a-zA-Z0-9_-]', '', backup_id)
    backup_dir = os.path.join(_backup_root(), backup_id)
    if not os.path.isdir(backup_dir):
        return jsonify({'error': 'Backup not found'}), 404
    meta = _read_backup_meta(backup_dir)
    if not meta:
        return jsonify({'error': 'Invalid backup metadata'}), 500
    meta['id'] = backup_id
    return jsonify(meta)


def _resolve_all_targets(backup_type):
    """Return list of (name, label) tuples for all containers or projects."""
    targets = []
    if backup_type == 'container':
        fmt = '{{json .}}'
        out, _, rc = _run(['docker', 'ps', '-a', '--format', fmt, '--no-trunc'])
        if rc == 0:
            for raw in _json_lines(out):
                cid = raw.get('ID', '')[:12]
                cname = raw.get('Names', '')
                cimage = raw.get('Image', '')
                if cid:
                    targets.append((cid, f'{cname} ({cimage})'))
    else:
        projects = _find_compose_projects()
        for p in projects:
            targets.append((p['name'], p['name']))
    return targets


def _bg_create_backup_all(targets, backup_type, mode, label_prefix, bulk_bid):
    """Background worker: create backups for all targets sequentially."""
    s = _sio()
    total = len(targets)

    def emit(pct, msg, status='running'):
        if s:
            s.emit('docker_backup', {
                'id': bulk_bid, 'percent': pct, 'message': msg, 'status': status
            })

    try:
        for idx, (name, default_label) in enumerate(targets):
            progress_base = int((idx / total) * 100)
            lbl = f'{label_prefix} — {default_label}' if label_prefix else default_label
            emit(progress_base, f'[{idx + 1}/{total}] {default_label}...')

            bid = _backup_id(backup_type, name)
            backup_dir = os.path.join(_backup_root(), bid)
            meta = {
                'type': backup_type,
                'name': name,
                'label': lbl,
                'mode': mode,
                'created': _time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'status': 'running',
                'size': 0,
                'size_human': '0 B',
                'volumes': [],
                'images': [],
            }
            try:
                os.makedirs(backup_dir, exist_ok=True)
                if backup_type == 'container':
                    _backup_container(backup_dir, meta, name, mode, lambda p, m, s='running': emit(
                        progress_base + int(p * (1 / total)), m, s))
                else:
                    _backup_project(backup_dir, meta, name, mode, lambda p, m, s='running': emit(
                        progress_base + int(p * (1 / total)), m, s))
                meta['size'] = _dir_size_bytes(backup_dir)
                meta['size_human'] = _fmt_size(meta['size'])
                meta['status'] = 'complete'
                _write_backup_meta(backup_dir, meta)
            except Exception as e:
                meta['status'] = 'error'
                meta['error'] = str(e)
                try:
                    _write_backup_meta(backup_dir, meta)
                except Exception:
                    pass
                emit(progress_base, f'Błąd: {default_label} — {e}')

        emit(100, f'Backup zakończony — {total} elementów.', 'done')
        audit_log('docker.backup.create', f'Bulk backup: {total} {backup_type}s ({mode})')
    except Exception as e:
        emit(0, f'Błąd backupu zbiorczego: {e}', 'error')
    finally:
        _backup_running.pop(bulk_bid, None)


@docker_bp.route('/backups', methods=['POST'])
@_require_docker
@_require_admin
def create_backup():
    """Create a new Docker backup (runs in background).

    Body: { type: "container"|"project", name: "id_or_name"|"__all__", label: "...", mode: "full"|"light" }
    Progress via SocketIO: docker_backup
    """
    data = request.get_json(force=True) if request.data else {}
    backup_type = data.get('type', '').strip()
    target_name = data.get('name', '').strip()
    label = data.get('label', '').strip()
    mode = data.get('mode', 'light').strip()

    if backup_type not in ('container', 'project'):
        return jsonify({'error': 'Invalid backup type — use "container" or "project"'}), 400
    if not target_name:
        return jsonify({'error': 'Target name is required'}), 400
    if mode not in ('full', 'light'):
        return jsonify({'error': 'Invalid mode — use "full" or "light"'}), 400

    # Bulk backup: resolve __all__ into individual targets
    if target_name == '__all__':
        targets = _resolve_all_targets(backup_type)
        if not targets:
            return jsonify({'error': 'No targets found for bulk backup'}), 404
        bid = _backup_id(backup_type, '__all__')
        if bid in _backup_running:
            return jsonify({'error': 'Bulk backup already in progress'}), 409
        _backup_running[bid] = True
        s = _sio()
        if s:
            s.start_background_task(_bg_create_backup_all, targets, backup_type, mode, label, bid)
        else:
            _bg_create_backup_all(targets, backup_type, mode, label, bid)
        return jsonify({'ok': True, 'id': bid, 'status': 'started', 'count': len(targets)})

    bid = _backup_id(backup_type, target_name)
    backup_dir = os.path.join(_backup_root(), bid)

    if bid in _backup_running:
        return jsonify({'error': 'Backup already in progress'}), 409

    meta = {
        'type': backup_type,
        'name': target_name,
        'label': label or target_name,
        'mode': mode,
        'created': _time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'status': 'running',
        'size': 0,
        'size_human': '0 B',
        'volumes': [],
        'images': [],
    }

    _backup_running[bid] = True
    s = _sio()
    if s:
        s.start_background_task(_bg_create_backup, backup_dir, meta, backup_type, target_name, mode)
    else:
        _bg_create_backup(backup_dir, meta, backup_type, target_name, mode)

    return jsonify({'ok': True, 'id': bid, 'status': 'started'})


@docker_bp.route('/backups/<backup_id>', methods=['DELETE'])
@_require_docker
@_require_admin
def delete_backup(backup_id):
    """Delete a Docker backup."""
    backup_id = re.sub(r'[^a-zA-Z0-9_-]', '', backup_id)
    backup_dir = os.path.join(_backup_root(), backup_id)

    # Validate path inside backup root
    real_root = os.path.realpath(_backup_root())
    real_path = os.path.realpath(backup_dir)
    if not real_path.startswith(real_root + '/'):
        return jsonify({'error': 'Invalid backup path'}), 400

    if not os.path.isdir(backup_dir):
        return jsonify({'error': 'Backup not found'}), 404

    if backup_id in _backup_running:
        return jsonify({'error': 'Cannot delete — backup in progress'}), 409

    try:
        shutil.rmtree(backup_dir)
        audit_log('docker.backup.delete', f'Backup deleted: {backup_id}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/backups/<backup_id>/restore', methods=['POST'])
@_require_docker
@_require_admin
def restore_backup(backup_id):
    """Restore from a Docker backup (runs in background).

    Progress via SocketIO: docker_restore
    """
    backup_id = re.sub(r'[^a-zA-Z0-9_-]', '', backup_id)
    backup_dir = os.path.join(_backup_root(), backup_id)

    if not os.path.isdir(backup_dir):
        return jsonify({'error': 'Backup not found'}), 404

    meta = _read_backup_meta(backup_dir)
    if not meta:
        return jsonify({'error': 'Invalid backup metadata'}), 500

    if meta.get('status') != 'complete':
        return jsonify({'error': 'Backup is incomplete or in error state'}), 400

    if backup_id in _backup_running:
        return jsonify({'error': 'Operation already in progress for this backup'}), 409

    data = request.get_json(force=True) if request.data else {}
    options = {
        'restore_volumes': data.get('restore_volumes', True),
    }

    _backup_running[backup_id] = True
    s = _sio()
    if s:
        s.start_background_task(_bg_restore_backup, backup_dir, meta, options)
    else:
        _bg_restore_backup(backup_dir, meta, options)

    return jsonify({'ok': True, 'id': backup_id, 'status': 'started'})


# ═══════════════════════════════════════════════════════════
#  SANDBOX POLICY — convenience proxy endpoints
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/projects/<project_name>/policy', methods=['GET'])
@_require_admin
def get_project_policy(project_name):
    """Return the effective sandbox policy for a compose project.

    Combines global defaults with any per-project override stored in
    sandbox_policies.json. The result describes the resource constraints
    that should be applied to containers belonging to this project.
    """
    policy = _get_sandbox_policy(project_name)
    return jsonify({'project': project_name, 'policy': policy})


# ── Package: uninstall / pkg-status ──

register_pkg_routes(
    docker_bp,
    install_message='Docker Manager ready.',
    status_extras=lambda: {'docker_available': check_dep('docker')},
)
