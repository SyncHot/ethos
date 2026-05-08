"""
EthOS — App Store Validation
Pre-install validation: checks port conflicts, policy compliance, and disk space.
"""

import os
import json
import yaml
import logging

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from flask import jsonify, request
from blueprints.admin_required import admin_required

log = logging.getLogger('appstore')

# Import from main appstore module
from appstore import (
    appstore_bp, _require_admin, _get_catalog, _safe_compose_dir,
    _adapt_compose, _validate_compose_policy, _check_port_conflicts,
    _get_installed_apps
)

_socketio = None

def init_appstore_validation(socketio):
    """Set SocketIO reference."""
    global _socketio
    _socketio = socketio


# ════════════════════════════════════════════════════════════
#  Validation Routes
# ════════════════════════════════════════════════════════════

@appstore_bp.route('/validate', methods=['POST'])
def validate_install():
    """Pre-install validation: check port conflicts, policy, disk space."""
    deny = _require_admin()
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
            warnings.append({'type': 'already_installed', 'message': f'{app_id} is already installed'})

    # Disk space check
    try:
        st = os.statvfs(_compose_root())
        free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
        if free_gb < 1.0:
            errors.append({'type': 'disk_space', 'message': f'Not enough disk space ({free_gb:.1f} GB free)'})
        elif free_gb < 5.0:
            warnings.append({'type': 'disk_space', 'message': f'Low disk space ({free_gb:.1f} GB)'})
    except Exception:
        pass

    return jsonify({
        'ok': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'adapted_compose': adapted,
    })


