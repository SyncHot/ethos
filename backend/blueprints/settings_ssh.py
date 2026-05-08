"""
EthOS — SSH Key Management Shims Blueprint
Thin wrappers around ssh_manager module to maintain backward compatibility
with the old /api/settings/ssh-keys/* URLs.
"""

import os
import sys
import logging
from flask import jsonify

# Import blueprint from main settings module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from blueprints.settings import settings_bp

# Import from ssh_manager
from blueprints.ssh_manager import (
    list_ssh_keys_data as _ssh_list_keys_data,
    _safe_keys as _ssh_safe_keys,
    SSH_KEYS_DIR,
)

_ssh_logger = logging.getLogger(__name__)


# ── Routes (thin shims) ──

@settings_bp.route('/ssh-keys', methods=['GET'])
def list_ssh_keys():
    try:
        return jsonify({'keys': _ssh_safe_keys(_ssh_list_keys_data())})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/ssh-keys/generate', methods=['POST'])
def generate_ssh_key():
    from blueprints.ssh_manager import api_generate_key
    return api_generate_key()


@settings_bp.route('/ssh-keys/<key_name>', methods=['DELETE'])
def delete_ssh_key(key_name):
    from blueprints.ssh_manager import api_delete_key
    return api_delete_key(key_name)


@settings_bp.route('/ssh-keys/<key_name>/public', methods=['GET'])
def get_public_key(key_name):
    from blueprints.ssh_manager import api_get_public
    return api_get_public(key_name)


@settings_bp.route('/ssh-keys/<key_name>/deploy', methods=['POST'])
def deploy_ssh_key(key_name):
    from blueprints.ssh_manager import api_deploy_key
    return api_deploy_key(key_name)


@settings_bp.route('/ssh-keys/test-auth', methods=['POST'])
def test_key_auth():
    from blueprints.ssh_manager import api_test_auth
    return api_test_auth()


@settings_bp.route('/known-hosts', methods=['GET'])
def list_known_hosts():
    from blueprints.ssh_manager import api_list_known_hosts
    return api_list_known_hosts()
