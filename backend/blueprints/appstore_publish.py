"""
EthOS — App Store Install/Publish
Install, reinstall, and uninstall Docker applications.
"""

import os
import json
import subprocess
import tempfile
import uuid
import logging

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from flask import jsonify, request
from blueprints.admin_required import admin_required

log = logging.getLogger('appstore')

# Import from main appstore module
from appstore import (
    appstore_bp, _socketio, _require_admin, _docker_available,
    _get_catalog, _safe_compose_dir, _run_host, _run_host_stream,
    _adapt_compose, _validate_compose_policy, _extract_editable_config,
    _apply_editable_config, _emit_install
)

_app_socketio = None

def init_appstore_publish(socketio):
    """Set SocketIO reference."""
    global _app_socketio
    _app_socketio = socketio


# ════════════════════════════════════════════════════════════
#  Install/Publish Routes
# ════════════════════════════════════════════════════════════

def _bg_install(task_id, app_id, app_title, adapted, dir_name, host_app_dir, container_app_dir):
    """Background: write compose, pull, up — emit progress via SocketIO."""
    try:
        # Step 1: Write compose file
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'prepare',
                        'percent': 5, 'message': 'Preparing files…'})
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
                        'percent': 10, 'message': 'Pulling Docker images…'})

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
                            'percent': 100, 'message': f'Pull error: {pull_output[-300:]}'})
            return

        # Step 3: Start containers
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'start',
                        'percent': 85, 'message': 'Starting containers…'})

        def on_up_line(line):
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'start',
                            'percent': 90, 'message': line[:120]})

        up_output, up_rc = _run_host_stream(
            f'docker compose {files_arg} up -d --remove-orphans', cwd=host_app_dir, on_line=on_up_line, timeout=300
        )
        if up_rc != 0:
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                            'percent': 100, 'message': f'Start error: {up_output[-300:]}'})
            return

        # Step 4: Verify containers started
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'verify',
                        'percent': 95, 'message': 'Verifying containers…'})
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
                            'message': f'{app_title} installed, but some containers may require configuration.'})
        else:
            # Done!
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'done',
                            'percent': 100, 'message': f'{app_title} installed!'})

    except Exception as e:
        log.error('Install %s failed: %s', app_id, e)
        _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                        'percent': 100, 'message': f'Error: {str(e)}'})





@appstore_bp.route('/install', methods=['POST'])
def install_app():
    """Install an app: returns task_id immediately, work happens in background."""
    deny = _require_admin(require_sudo=True)
    if deny:
        return deny
    err = require_tools('docker')
    if err:
        return err
    if not _docker_available():
        return jsonify({'error': 'Docker daemon is not running. Start Docker in Docker Manager.'}), 503
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
        return jsonify({'error': 'Invalid app_id'}), 400
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

    return jsonify({'status': 'ok', 'task_id': task_id})


@appstore_bp.route('/reinstall', methods=['POST'])
def reinstall_app():
    """Reinstall/update an app: stop, pull new images, start."""
    deny = _require_admin(require_sudo=True)
    if deny:
        return deny
    err = require_tools('docker')
    if err:
        return err
    if not _docker_available():
        return jsonify({'error': 'Docker daemon is not running. Start Docker in Docker Manager.'}), 503
    data = request.json or {}
    app_id = data.get('app_id', '').strip()
    if not app_id:
        return jsonify({'error': 'app_id required'}), 400

    dir_name, safe_dir = _safe_compose_dir(app_id)
    if not dir_name:
        return jsonify({'error': 'Invalid app_id'}), 400

    compose_file = os.path.join(safe_dir, 'docker-compose.yml')
    if not os.path.isfile(compose_file):
        return jsonify({'error': 'Application is not installed'}), 404

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
                            'percent': 5, 'message': 'Stopping containers…'})
            _run_host_stream(f'docker compose {files_arg} down', cwd=safe_dir, timeout=120)

            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'pull',
                            'percent': 20, 'message': 'Pulling new images…'})
            pull_out, pull_rc = _run_host_stream(f'docker compose {files_arg} pull', cwd=safe_dir, timeout=600)
            if pull_rc != 0:
                _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                                'percent': 100, 'message': f'Error: {pull_out[-300:]}'})
                return

            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'start',
                            'percent': 80, 'message': 'Starting containers…'})
            up_out, up_rc = _run_host_stream(f'docker compose {files_arg} up -d --remove-orphans', cwd=safe_dir, timeout=300)
            if up_rc != 0:
                _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                                'percent': 100, 'message': f'Error: {up_out[-300:]}'})
                return

            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'done',
                            'percent': 100, 'message': f'{app_title} updated!'})
        except Exception as e:
            log.error('Reinstall %s failed: %s', app_id, e)
            _emit_install({'task_id': task_id, 'app_id': app_id, 'stage': 'error',
                            'percent': 100, 'message': f'Error: {str(e)}'})

    if _socketio:
        _socketio.start_background_task(_bg_reinstall)
    else:
        _bg_reinstall()

    return jsonify({'status': 'ok', 'task_id': task_id})


@appstore_bp.route('/uninstall', methods=['POST'])
def uninstall_app():
    """Stop and remove an installed app."""
    deny = _require_admin(require_sudo=True)
    if deny:
        return deny
    err = require_tools('docker')
    if err:
        return err
    if not _docker_available():
        return jsonify({'error': 'Docker daemon is not running. Start Docker in Docker Manager.'}), 503
    data = request.json or {}
    app_id = data.get('app_id', '').strip()
    if not app_id:
        return jsonify({'error': 'app_id required'}), 400

    dir_name, safe_dir = _safe_compose_dir(app_id)
    if not dir_name:
        return jsonify({'error': 'Invalid app_id'}), 400
    host_app_dir = safe_dir
    container_app_dir = safe_dir

    if not os.path.isdir(container_app_dir):
        return jsonify({'error': 'App not installed'}), 404

    out, err, rc = _run_host('docker compose down --remove-orphans', timeout=120, cwd=host_app_dir)

    try:
        shutil.rmtree(container_app_dir)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Compose down ok but cleanup failed: {e}'}), 500

    return jsonify({'status': 'ok', 'app_id': app_id, 'stdout': out})


