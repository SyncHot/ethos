"""
EthOS — Installer Handover Blueprint
=====================================
Provides read-only endpoints that the setup wizard uses to detect whether
the preboot installer has already prepared disks (installer_result.json).

The actual installation is handled by the separate preboot installer
(installer/preboot/).  This blueprint only exposes the handover contract.

Endpoints:
  GET /api/installer/status  — installation status (idle / done)
  GET /api/installer/result  — installer_result.json contents
"""

import json
import os

from flask import Blueprint, jsonify

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path

installer_bp = Blueprint('installer', __name__, url_prefix='/api/installer')

_INSTALLER_RESULT_FILE = data_path('installer_result.json')


@installer_bp.route('/status')
def api_status():
    """Check if preboot installer has completed."""
    if os.path.isfile(_INSTALLER_RESULT_FILE):
        return jsonify({
            'status': 'done',
            'phase': 'complete',
            'percent': 100,
            'message': 'Installation complete (preboot)',
            'data_disks': [],
            'elapsed': 0,
            'log_count': 0,
            'result': None,
            'speed': 0,
            'strategy': '',
            'target_device': '',
        })
    return jsonify({
        'status': 'idle',
        'phase': '',
        'percent': 0,
        'message': '',
        'data_disks': [],
        'elapsed': 0,
        'log_count': 0,
        'result': None,
        'speed': 0,
        'strategy': '',
        'target_device': '',
    })


@installer_bp.route('/result')
def api_result():
    """Return installer_result.json handover contract (if exists)."""
    if os.path.isfile(_INSTALLER_RESULT_FILE):
        try:
            with open(_INSTALLER_RESULT_FILE) as f:
                return jsonify(json.load(f))
        except Exception:
            pass
    return jsonify({'error': 'No installer result'}), 404
