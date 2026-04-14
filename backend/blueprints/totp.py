"""
EthOS — TOTP Two-Factor Authentication Blueprint
Manages TOTP setup, verification, and status for user accounts.
"""

import os
import json
import time
import random
import base64
import io
import threading
from flask import Blueprint, request, jsonify, g

totp_bp = Blueprint('totp', __name__, url_prefix='/api/totp')

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path as _data_path

import pyotp
import qrcode

_SECRETS_FILE = _data_path('totp_secrets.json')
_secrets_lock = threading.Lock()


def _load_secrets():
    """Load TOTP secrets from disk."""
    try:
        with open(_SECRETS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_secrets(data):
    """Save TOTP secrets to disk with restricted permissions."""
    tmp = _SECRETS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _SECRETS_FILE)


def is_totp_enabled(username):
    """Check if TOTP is enabled for a given username."""
    secrets = _load_secrets()
    entry = secrets.get(username)
    return bool(entry and entry.get('enabled'))


def get_totp_secret(username):
    """Return the TOTP secret for a username, or None."""
    secrets = _load_secrets()
    entry = secrets.get(username)
    if entry and entry.get('enabled'):
        return entry.get('secret')
    return None


def verify_totp_code(username, code):
    """Verify a TOTP code for a user. Returns True if valid."""
    secret = get_totp_secret(username)
    if not secret:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=2)


def verify_backup_code(username, code):
    """Verify and consume a backup code. Returns True if valid."""
    with _secrets_lock:
        secrets = _load_secrets()
        entry = secrets.get(username)
        if not entry or not entry.get('enabled'):
            return False
        backup_codes = entry.get('backup_codes', [])
        if code in backup_codes:
            backup_codes.remove(code)
            entry['backup_codes'] = backup_codes
            secrets[username] = entry
            _save_secrets(secrets)
            return True
    return False


def _get_current_user():
    """Get the current authenticated username from g context."""
    username = getattr(g, 'username', None)
    if not username:
        return None
    return username


def _generate_backup_codes(count=8):
    """Generate a list of random 8-digit backup codes."""
    return [f"{random.randint(10000000, 99999999)}" for _ in range(count)]


@totp_bp.route('/status', methods=['GET'])
def totp_status():
    username = _get_current_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'enabled': is_totp_enabled(username)})


@totp_bp.route('/setup', methods=['POST'])
def totp_setup():
    username = _get_current_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 401

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=username, issuer_name='EthOS NAS')

    # Generate QR code as base64 PNG
    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    qr_b64 = base64.b64encode(buf.getvalue()).decode('ascii')

    backup_codes = _generate_backup_codes()

    # Store the pending setup (not yet enabled)
    with _secrets_lock:
        secrets = _load_secrets()
        secrets[username] = {
            'secret': secret,
            'enabled': False,
            'backup_codes': backup_codes,
            'created': time.time(),
        }
        _save_secrets(secrets)

    return jsonify({
        'secret': secret,
        'qr_code_base64': f"data:image/png;base64,{qr_b64}",
        'backup_codes': backup_codes,
    })


@totp_bp.route('/verify', methods=['POST'])
def totp_verify():
    username = _get_current_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json or {}
    code = str(data.get('code', '')).strip()
    if not code:
        return jsonify({'error': 'Code required'}), 400

    with _secrets_lock:
        secrets = _load_secrets()
        entry = secrets.get(username)
        if not entry or not entry.get('secret'):
            return jsonify({'error': 'Run /api/totp/setup first'}), 400

        totp = pyotp.TOTP(entry['secret'])
        if not totp.verify(code, valid_window=2):
            return jsonify({'error': 'Invalid code'}), 401

        # Enable 2FA
        entry['enabled'] = True
        secrets[username] = entry
        _save_secrets(secrets)

    return jsonify({'ok': True})


@totp_bp.route('/disable', methods=['POST'])
def totp_disable():
    username = _get_current_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json or {}
    code = str(data.get('code', '')).strip()
    backup_code = str(data.get('backup_code', '')).strip()

    if not code and not backup_code:
        return jsonify({'error': 'Provide code or backup_code'}), 400

    with _secrets_lock:
        secrets = _load_secrets()
        entry = secrets.get(username)
        if not entry or not entry.get('enabled'):
            return jsonify({'error': '2FA is not enabled'}), 400

        valid = False
        if code:
            totp = pyotp.TOTP(entry['secret'])
            valid = totp.verify(code, valid_window=2)
        if not valid and backup_code:
            if backup_code in entry.get('backup_codes', []):
                entry['backup_codes'].remove(backup_code)
                valid = True

        if not valid:
            return jsonify({'error': 'Invalid code'}), 401

        # Remove TOTP entry entirely
        del secrets[username]
        _save_secrets(secrets)

    return jsonify({'ok': True})
