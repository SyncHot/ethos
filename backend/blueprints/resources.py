"""
EthOS — Resources Monitor Blueprint
System monitoring with WebSocket real-time updates and history.
Migrated from standalone resources app.
"""

import os
import time
import logging
from flask import Blueprint, jsonify, request

from utils import require_tools, check_tool

from blueprints.monitor import (
    get_cpu_info, get_ram_info, get_gpu_info, get_disk_info,
    get_network_info, get_processes, kill_process, get_usb_devices,
    get_system_info, get_docker_containers, docker_action,
    detect_gpu_hardware, get_smart_info
)
from blueprints.resources_db import (
    init_db as init_resources_db, save_cpu_data, save_ram_data,
    save_gpu_data, save_disk_data, save_network_data, save_process_data,
    save_usb_data, save_docker_data, get_history, cleanup_old_data
)

# Setup logging for the blueprint
logger = logging.getLogger(__name__)

resources_bp = Blueprint('resources', __name__, url_prefix='/api/resources')

COLLECT_INTERVAL = int(os.environ.get('COLLECT_INTERVAL', 3))            # fast: CPU, RAM, network (WebSocket only)
COLLECT_INTERVAL_SLOW = int(os.environ.get('COLLECT_INTERVAL_SLOW', 30)) # medium: disks, processes, GPU
COLLECT_INTERVAL_VSLOW = int(os.environ.get('COLLECT_INTERVAL_VSLOW', 60)) # expensive: docker stats, SMART
SAVE_TO_DB_INTERVAL = int(os.environ.get('SAVE_TO_DB_INTERVAL', 30))      # DB write cadence (independent of emit)
CLEANUP_INTERVAL = int(os.environ.get('CLEANUP_INTERVAL', 3600))
DATA_RETENTION_DAYS = int(os.environ.get('DATA_RETENTION_DAYS', 7))

# ---- In-memory cache fed by background collector ----
_snapshot = {}
_snapshot_ts = 0  


def _cached(key, fallback_fn, *args):
    """Return cached data from collector snapshot, fall back to live call."""
    if _snapshot_ts and key in _snapshot:
        return _snapshot[key]
    return fallback_fn(*args)


# ---- REST API ----

@resources_bp.route('/system')
def api_system():
    return jsonify(_cached('system', get_system_info))


@resources_bp.route('/cpu')
def api_cpu():
    return jsonify(_cached('cpu', get_cpu_info))


@resources_bp.route('/ram')
def api_ram():
    return jsonify(_cached('ram', get_ram_info))


@resources_bp.route('/gpu')
def api_gpu():
    return jsonify(_cached('gpu', get_gpu_info))


@resources_bp.route('/gpu/detect')
def api_gpu_detect():
    return jsonify(detect_gpu_hardware())


@resources_bp.route('/disks')
def api_disks():
    return jsonify(_cached('disks', get_disk_info))


@resources_bp.route('/network')
def api_network():
    return jsonify(_cached('network', get_network_info))


@resources_bp.route('/processes')
def api_processes():
    sort_by = request.args.get('sort', 'cpu')
    try:
        limit = int(request.args.get('limit', 30))
    except (ValueError, TypeError):
        limit = 30
    return jsonify(_cached('processes', get_processes, sort_by, limit))


@resources_bp.route('/processes/kill', methods=['POST'])
def api_kill_process():
    data = request.get_json()
    pid = data.get('pid')
    signal = data.get('signal', 'TERM')
    if pid is None:
        return jsonify({'success': False, 'message': 'PID required'}), 400
    result = kill_process(int(pid), signal)
    return jsonify(result)


@resources_bp.route('/usb')
def api_usb():
    return jsonify(_cached('usb', get_usb_devices))


@resources_bp.route('/smart')
def api_smart():
    return jsonify(_cached('smart', get_smart_info))


@resources_bp.route('/docker')
def api_docker():
    err = require_tools('docker')
    if err:
        return err
    return jsonify(_cached('docker', get_docker_containers))


@resources_bp.route('/docker/action', methods=['POST'])
def api_docker_action():
    err = require_tools('docker')
    if err:
        return err
    data = request.get_json()
    container_id = data.get('container_id')
    action = data.get('action', 'stop')
    if not container_id:
        return jsonify({'success': False, 'message': 'container_id required'}), 400
    result = docker_action(container_id, action)
    return jsonify(result)


@resources_bp.route('/history/<table>')
def api_history(table):
    allowed = ['cpu_history', 'ram_history', 'gpu_history', 'disk_history', 'network_history', 'process_history', 'docker_history']
    if table not in allowed:
        return jsonify({'error': 'Invalid table'}), 400
    try:
        hours = int(request.args.get('hours', 1))
    except (ValueError, TypeError):
        hours = 1
    try:
        limit = int(request.args.get('limit', 500))
    except (ValueError, TypeError):
        limit = 500
    return jsonify(get_history(table, hours, limit))


@resources_bp.route('/all')
def api_all():
    data = {
        'system': _cached('system', get_system_info),
        'cpu': _cached('cpu', get_cpu_info),
        'ram': _cached('ram', get_ram_info),
        'gpu': _cached('gpu', get_gpu_info),
        'disks': _cached('disks', get_disk_info),
        'smart': _cached('smart', get_smart_info),
        'network': _cached('network', get_network_info),
        'processes': _cached('processes', get_processes, 'cpu', 30),
        'usb': _cached('usb', get_usb_devices),
        'docker': _cached('docker', get_docker_containers) if check_tool('docker') else []
    }
    return jsonify(data)


# ---- Background collector (called from main app) ----

def resources_background_collector(socketio):
    """Collect and broadcast data periodically. Called as a socketio background task."""
    global _snapshot, _snapshot_ts
    last_cleanup = time.time()
    last_slow = 0     
    last_vslow = 0    
    last_db_save = 0  

    # Cached slow-changing data
    _disks = []
    _smart = []
    _gpu = []
    _processes = []
    _usb = []
    _docker = []

    while True:
        try:
            now = time.time()
            do_slow = (now - last_slow) >= COLLECT_INTERVAL_SLOW
            do_vslow = (now - last_vslow) >= COLLECT_INTERVAL_VSLOW
            do_db = (now - last_db_save) >= SAVE_TO_DB_INTERVAL

            # Fast — always collected
            cpu = get_cpu_info()
            ram = get_ram_info()
            network = get_network_info()

            # Medium collection
            if do_slow:
                last_slow = now
                try:
                    _gpu = get_gpu_info()
                    _disks = get_disk_info()
                    _processes = get_processes('cpu', 20)
                    _usb = get_usb_devices()
                except Exception as e:
                    logger.error(f"Medium collection error: {e}")

            # Very slow collection
            if do_vslow:
                last_vslow = now
                try:
                    _smart = get_smart_info()
                    _docker = get_docker_containers()
                except Exception as e:
                    logger.error(f"Very slow collection error: {e}")

            # Save to DB
            if do_db:
                last_db_save = now
                try:
                    save_cpu_data(cpu)
                    save_ram_data(ram)
                    save_network_data(network)
                    if _gpu: save_gpu_data(_gpu)
                    save_disk_data(_disks)
                    save_process_data(_processes)
                    save_usb_data(_usb)
                    if _docker: save_docker_data(_docker)
                except Exception as e:
                    logger.error(f"Resources DB save error: {e}")

            # Prepare broadcast data
            data = {
                'cpu': cpu,
                'ram': ram,
                'gpu': _gpu,
                'disks': _disks,
                'smart': _smart,
                'network': network,
                'processes': _processes,
                'usb': _usb,
                'docker': _docker,
                'timestamp': now
            }
            
            # Atomic update of snapshot
            _snapshot = data
            _snapshot_ts = now

            socketio.emit('resources_update', data)

            # Cleanup
            if now - last_cleanup > CLEANUP_INTERVAL:
                try:
                    cleanup_old_data(DATA_RETENTION_DAYS)
                    last_cleanup = now
                except Exception as e:
                    logger.error(f"Cleanup error: {e}")

        except Exception as e:
            logger.critical(f"Resources collector loop failure: {e}", exc_info=True)

        socketio.sleep(COLLECT_INTERVAL)
