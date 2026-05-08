"""
docker_manager_compose.py — part of the Docker Manager blueprint.
Compose project management: list, action, create, logs, compose file editing, sandbox policy.

Imported at the bottom of docker_manager.py.
"""

import os
import re
import json
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask import request, jsonify, g

from blueprints.docker_manager import (
    docker_bp,
    _compose_root,
    _run,
    _run_host,
    _sio,
    _require_docker,
    _require_admin,
    _sanitize_shell_arg,
    _get_sandbox_policy,
    _PROTECTED_PROJECTS,
    _SANDBOX_OVERRIDE_FILENAME,
    _DEFAULT_COMPOSE_OVERRIDES,
    _DESTRUCTIVE_PROJECT_ACTIONS,
)
from utils import find_compose_projects as _find_compose_projects_util


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


def _detect_legacy_mem_limit_services(project_path, main_filename):
    """Return set of service names that use the legacy top-level mem_limit key."""
    result = set()
    main_path = os.path.join(project_path, main_filename)
    try:
        import yaml  # type: ignore
        with open(main_path) as f:
            data = yaml.safe_load(f)
        for svc_name, svc_cfg in (data or {}).get('services', {}).items():
            if isinstance(svc_cfg, dict) and 'mem_limit' in svc_cfg:
                result.add(svc_name)
    except Exception:
        pass
    return result


def _policy_to_legacy_limits(policy):
    """Translate sandbox policy into legacy top-level service keys.

    Used instead of _policy_to_service_limits when the compose file already
    uses the legacy ``mem_limit`` key so that ``deploy.resources.limits`` is
    not mixed in — Docker Compose rejects that combination.
    """
    cfg: dict = {}

    mem_limit = str(policy.get('mem_limit', '')).strip()
    if mem_limit and mem_limit != '0':
        cfg['mem_limit'] = mem_limit

    mem_reservation = str(policy.get('mem_reservation', '')).strip()
    if mem_reservation and mem_reservation != '0':
        cfg['mem_reservation'] = mem_reservation

    cpu_quota = policy.get('cpu_quota', 0)
    try:
        cpu_quota = float(cpu_quota)
    except (TypeError, ValueError):
        cpu_quota = 0
    if cpu_quota > 0:
        cfg['cpus'] = round(cpu_quota / 100.0, 3)

    pids_limit = policy.get('pids_limit', 0)
    try:
        pids_limit = int(pids_limit)
    except (TypeError, ValueError):
        pids_limit = 0
    if pids_limit > 0:
        cfg['pids_limit'] = pids_limit

    if policy.get('read_only_root'):
        cfg['read_only'] = True

    if policy.get('no_new_privileges'):
        cfg['security_opt'] = ['no-new-privileges:true']

    cap_drop = policy.get('cap_drop') or []
    if cap_drop:
        cfg['cap_drop'] = cap_drop

    cap_add = policy.get('cap_add') or []
    if cap_add:
        cfg['cap_add'] = cap_add

    return cfg


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

    # Services using legacy mem_limit must not get any deploy.resources.limits
    # block — Docker Compose rejects the combination even when only cpus/pids
    # are in deploy.resources.limits (it internally maps mem_limit to
    # deploy.resources.limits.memory, causing a "distinct values" conflict).
    # Use legacy top-level keys (cpus, mem_limit, pids_limit …) for those.
    legacy_mem_svcs = _detect_legacy_mem_limit_services(project_path, main_filename)
    legacy_limits = _policy_to_legacy_limits(policy) if legacy_mem_svcs else {}

    override_services = {}
    for svc in services:
        override_services[svc] = dict(legacy_limits if svc in legacy_mem_svcs else limits)

    override = {'version': '3', 'services': override_services}
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


# ═══════════════════════════════════════════════════════════
#  DOCKER-COMPOSE PROJECTS
# ═══════════════════════════════════════════════════════════

@docker_bp.route('/projects')
@_require_docker
def list_projects():
    """List all docker-compose projects with status."""
    try:
        projects = _find_compose_projects()

        out, _, _ = _run([
            'docker', 'ps', '-a', '--format',
            '{{.Label "com.docker.compose.project"}}|{{.Names}}|{{.State}}|{{.Status}}|{{.Image}}|{{.Ports}}|{{.Label "com.docker.compose.service"}}'
        ])

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

    if project_name in _PROTECTED_PROJECTS and action in ('down', 'stop', 'remove'):
        return jsonify({'error': f'Project "{project_name}" is protected — cannot stop from interface'}), 403

    projects = _find_compose_projects()
    project = next((p for p in projects if p['name'] == project_name), None)
    if not project:
        return jsonify({'error': f'Project {project_name} not found'}), 404

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
    if project_name in _PROTECTED_PROJECTS:
        return jsonify({'error': f'Project "{project_name}" is protected and cannot be removed'}), 403

    projects = _find_compose_projects()
    project = next((p for p in projects if p['name'] == project_name), None)
    if not project:
        return jsonify({'error': f'Project {project_name} not found'}), 404

    project_path = os.path.join(_compose_root(), project_name)

    real_root = os.path.realpath(_compose_root())
    real_path = os.path.realpath(project_path)
    if not real_path.startswith(real_root + '/'):
        return jsonify({'error': 'Invalid project path'}), 400

    try:
        _run_host('docker compose down --remove-orphans', timeout=120, cwd=project_path)
    except Exception:
        pass

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

    try:
        import yaml
        yaml.safe_load(content)
    except yaml.YAMLError as ye:
        return jsonify({'error': f'YAML syntax error: {str(ye)}'}), 400
    except ImportError:
        pass

    try:
        with open(project['compose_file'], 'w') as f:
            f.write(content)
        return jsonify({'ok': True})
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

    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$', name):
        return jsonify({'error': 'Project name may only contain letters, numbers, hyphens, and underscores'}), 400

    if len(name) > 64:
        return jsonify({'error': 'Project name too long (max 64 characters)'}), 400

    root = _compose_root()
    project_path = os.path.join(root, name)

    if os.path.exists(project_path):
        return jsonify({'error': f'Project "{name}" already exists'}), 409

    if not content:
        content = f"""# {name} — docker-compose.yaml
version: '3'

services:
  app:
    image: hello-world
    restart: unless-stopped
"""

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
