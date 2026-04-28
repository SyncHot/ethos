"""
EthOS — SSH Manager Blueprint
Centralised SSH key + known_hosts management.
All routes under /api/ssh.
"""

import os, re, time, subprocess, logging
from datetime import datetime
from flask import Blueprint, jsonify, request, g
import socket as _socket

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path as _data_path
from ssh_utils import get_ssh_client as _get_ssh_client, ssh_exec as _ssh_exec, HAS_SSH
from utils import require_tools, check_tool

log = logging.getLogger('ssh_manager')

ssh_bp = Blueprint('ssh_manager', __name__, url_prefix='/api/ssh')

# ── Directory ──

SSH_KEYS_DIR = _data_path('ssh_keys')


def _ensure_dir():
    os.makedirs(SSH_KEYS_DIR, mode=0o700, exist_ok=True)


def _ensure_default_key():
    """Generate a default ed25519 keypair if none exist yet."""
    _ensure_dir()
    # Check if any private key exists
    for fname in os.listdir(SSH_KEYS_DIR):
        fpath = os.path.join(SSH_KEYS_DIR, fname)
        if os.path.isfile(fpath) and not fname.endswith('.pub') and os.path.isfile(fpath + '.pub'):
            return  # at least one keypair exists
    # No keys — generate default
    hostname = _socket.gethostname()
    key_name = f'ethos_{hostname}'
    key_path = os.path.join(SSH_KEYS_DIR, key_name)
    if os.path.exists(key_path):
        return
    try:
        comment = f'ethos@{hostname}'
        subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-f', key_path, '-N', '', '-C', comment],
            capture_output=True, timeout=30, check=False,
        )
        if os.path.isfile(key_path):
            os.chmod(key_path, 0o600)
        if os.path.isfile(key_path + '.pub'):
            os.chmod(key_path + '.pub', 0o644)
        log.info('Auto-generated default SSH keypair: %s', key_name)
    except Exception as e:
        log.warning('Failed to auto-generate SSH key: %s', e)


# ══════════════════════════════════════════════════════════════════
#  Key listing (shared helper — also used by legacy settings routes)
# ══════════════════════════════════════════════════════════════════

def list_ssh_keys_data():
    """Return list of key dicts (used by both this blueprint and settings shim)."""
    _ensure_dir()
    keys = []
    seen = set()
    for fname in sorted(os.listdir(SSH_KEYS_DIR)):
        fpath = os.path.join(SSH_KEYS_DIR, fname)
        if not os.path.isfile(fpath) or fname.endswith('.pub'):
            continue
        pub_path = fpath + '.pub'
        if not os.path.isfile(pub_path):
            continue
        if fname in seen:
            continue
        seen.add(fname)
        try:
            with open(pub_path, 'r') as f:
                pub_content = f.read().strip()
        except Exception:
            pub_content = ''
        st = os.stat(fpath)
        key_type = 'unknown'
        comment = ''
        if pub_content:
            parts = pub_content.split()
            kt = parts[0] if parts else ''
            if 'ed25519' in kt:
                key_type = 'ed25519'
            elif 'ecdsa' in kt:
                key_type = 'ecdsa'
            elif 'rsa' in kt:
                key_type = 'rsa'
            if len(parts) >= 3:
                comment = ' '.join(parts[2:])
        keys.append({
            'name': fname,
            'type': key_type,
            'public_key': pub_content,
            'comment': comment,
            'private_path': fpath,
            'public_path': pub_path,
            'created': datetime.fromtimestamp(st.st_ctime).isoformat(),
            'size': st.st_size,
        })
    return keys


def _safe_keys(keys):
    return [{
        'name': k['name'], 'type': k['type'], 'public_key': k['public_key'],
        'comment': k['comment'], 'created': k['created'], 'private_path': k['private_path'],
    } for k in keys]


# ══════════════════════════════════════════════════════════════════
#  SSH Keys CRUD
# ══════════════════════════════════════════════════════════════════

@ssh_bp.route('/keys', methods=['GET'])
def api_list_keys():
    try:
        _ensure_default_key()
        return jsonify({'keys': _safe_keys(list_ssh_keys_data())})
    except Exception as e:
        log.exception('Error listing SSH keys')
        return jsonify({'error': str(e)}), 500


@ssh_bp.route('/keys/generate', methods=['POST'])
def api_generate_key():
    err = require_tools('ssh-keygen')
    if err:
        return err
    data = request.json or {}
    key_name = data.get('name', '').strip()
    key_type = data.get('type', 'ed25519')
    comment = data.get('comment', '').strip()
    bits = int(data.get('bits', 4096))

    if not key_name:
        key_name = f"ethos_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    key_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', key_name)
    _ensure_dir()
    key_path = os.path.join(SSH_KEYS_DIR, key_name)

    if os.path.exists(key_path):
        return jsonify({'error': f'Key named "{key_name}" already exists'}), 400
    if not comment:
        comment = f"ethos@{_socket.gethostname()}"
    if key_type not in ('ed25519', 'ecdsa', 'rsa'):
        key_type = 'ed25519'

    cmd = ['ssh-keygen', '-t', key_type, '-f', key_path, '-N', '', '-C', comment]
    if key_type == 'rsa':
        cmd.extend(['-b', str(bits)])
    elif key_type == 'ecdsa':
        cmd.extend(['-b', '521'])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({'error': f'ssh-keygen failed: {result.stderr}'}), 500
        os.chmod(key_path, 0o600)
        os.chmod(key_path + '.pub', 0o644)
        with open(key_path + '.pub', 'r') as f:
            pub = f.read().strip()
        return jsonify({
            'success': True,
            'key': {
                'name': key_name, 'type': key_type, 'public_key': pub,
                'comment': comment, 'private_path': key_path,
                'created': datetime.now().isoformat(),
            }
        })
    except Exception as e:
        log.exception('Error generating SSH key')
        return jsonify({'error': str(e)}), 500


@ssh_bp.route('/keys/<key_name>', methods=['DELETE'])
def api_delete_key(key_name):
    key_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', key_name)
    key_path = os.path.join(SSH_KEYS_DIR, key_name)
    if not os.path.exists(key_path):
        return jsonify({'error': 'Key not found'}), 404
    try:
        os.remove(key_path)
        pub = key_path + '.pub'
        if os.path.exists(pub):
            os.remove(pub)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@ssh_bp.route('/keys/<key_name>/public', methods=['GET'])
def api_get_public(key_name):
    key_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', key_name)
    pub_path = os.path.join(SSH_KEYS_DIR, key_name + '.pub')
    if not os.path.isfile(pub_path):
        return jsonify({'error': 'Key not found'}), 404
    with open(pub_path, 'r') as f:
        return jsonify({'public_key': f.read().strip()})


# ══════════════════════════════════════════════════════════════════
#  Deploy + Test
# ══════════════════════════════════════════════════════════════════

@ssh_bp.route('/keys/<key_name>/deploy', methods=['POST'])
def api_deploy_key(key_name):
    err = require_tools('ssh-keygen')
    if err:
        return err
    if not HAS_SSH:
        return jsonify({'error': 'paramiko not installed'}), 500

    key_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', key_name)
    pub_path = os.path.join(SSH_KEYS_DIR, key_name + '.pub')
    if not os.path.isfile(pub_path):
        return jsonify({'error': 'Key not found'}), 404

    with open(pub_path, 'r') as f:
        pub_key = f.read().strip()

    data = request.json or {}
    host = data.get('host', '')
    port = int(data.get('port', 22))
    username = data.get('username', '')
    password = data.get('password', '')
    if not host or not username:
        return jsonify({'error': 'host and username required'}), 400

    try:
        ssh = _get_ssh_client(host, port, username, password=password, timeout=15)
        for cmd in ['mkdir -p ~/.ssh', 'chmod 700 ~/.ssh',
                    'touch ~/.ssh/authorized_keys', 'chmod 600 ~/.ssh/authorized_keys']:
            ssh.exec_command(cmd)
            time.sleep(0.1)

        stdout, _, _ = _ssh_exec(ssh, 'cat ~/.ssh/authorized_keys')
        existing = stdout
        pub_parts = pub_key.split()
        fingerprint = pub_parts[1] if len(pub_parts) >= 2 else pub_key

        if fingerprint in existing:
            ssh.close()
            return jsonify({'status': 'ok', 'already_deployed': True})

        escaped = pub_key.replace("'", "'\\''")
        _, stderr, rc = _ssh_exec(ssh, f"echo '{escaped}' >> ~/.ssh/authorized_keys")
        if rc != 0:
            ssh.close()
            return jsonify({'error': f'Failed to add key: {stderr}'}), 500

        verify, _, _ = _ssh_exec(ssh, 'cat ~/.ssh/authorized_keys')
        ssh.close()

        priv_path = os.path.join(SSH_KEYS_DIR, key_name)
        if fingerprint in verify:
            return jsonify({'status': 'ok', 'key_path': priv_path})
        else:
            return jsonify({'error': 'Key deployment could not be verified'}), 500

    except Exception as e:
        log.exception('Error deploying SSH key')
        return jsonify({'error': str(e)}), 500


@ssh_bp.route('/keys/test-auth', methods=['POST'])
def api_test_auth():
    if not HAS_SSH:
        return jsonify({'error': 'paramiko not installed'}), 500

    data = request.json or {}
    key_name = data.get('key_name', '')
    host = data.get('host', '')
    port = int(data.get('port', 22))
    username = data.get('username', '')

    key_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', key_name)
    key_path = os.path.join(SSH_KEYS_DIR, key_name)
    if not os.path.isfile(key_path):
        return jsonify({'error': 'Key not found'}), 404

    try:
        ssh = _get_ssh_client(host, port, username, key_path=key_path, timeout=10)
        stdout, _, _ = _ssh_exec(ssh, 'whoami')
        ssh.close()
        return jsonify({'success': True, 'user': stdout.strip()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ══════════════════════════════════════════════════════════════════
#  Known Hosts
# ══════════════════════════════════════════════════════════════════

def _get_user_home(username=None):
    if not username:
        username = getattr(g, 'username', None) or 'root'
    try:
        result = subprocess.run(['getent', 'passwd', username],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split(':')[5]
    except Exception:
        pass
    return '/root' if username == 'root' else f'/home/{username}'


def _kh_path(username=None):
    return os.path.join(_get_user_home(username), '.ssh', 'known_hosts')


def _parse_known_hosts(username=None):
    kh = _kh_path(username)
    if not os.path.isfile(kh):
        return [], kh

    try:
        from blueprints.backup import load_ssh_configs
        servers = load_ssh_configs()
    except Exception:
        servers = []
    server_by_host = {s.get('host', ''): s.get('name', s.get('host', '')) for s in servers if s.get('host')}

    hosts_to_check = set(server_by_host.keys())
    try:
        arp = subprocess.run(['ip', 'neigh', 'show'], capture_output=True, text=True, timeout=5)
        if arp.returncode == 0:
            for line in arp.stdout.strip().split('\n'):
                parts = line.split()
                if parts:
                    hosts_to_check.add(parts[0])
    except Exception:
        pass

    line_to_host = {}
    for h in hosts_to_check:
        try:
            r = subprocess.run(['ssh-keygen', '-F', h, '-f', kh],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                for m in re.finditer(r'found: line (\d+)', r.stdout):
                    line_to_host[int(m.group(1))] = h
        except Exception:
            pass

    entries = []
    with open(kh, 'r') as f:
        lines = f.readlines()
    for idx, raw in enumerate(lines):
        raw = raw.strip()
        if not raw or raw.startswith('#'):
            continue
        parts = raw.split()
        if len(parts) < 3:
            continue
        host_field, key_type = parts[0], parts[1]
        line_num = idx + 1
        is_hashed = host_field.startswith('|1|')

        try:
            r = subprocess.run(['ssh-keygen', '-l', '-f', '-'],
                               input=raw, capture_output=True, text=True, timeout=5)
            fingerprint = r.stdout.strip().split()[1] if r.returncode == 0 else ''
        except Exception:
            fingerprint = ''

        matched_host = line_to_host.get(line_num)
        matched_server = server_by_host.get(matched_host) if matched_host else None
        if not matched_host and not is_hashed:
            matched_host = host_field.split(',')[0].strip('[]')
            matched_server = server_by_host.get(matched_host)

        entries.append({
            'line': line_num, 'host': matched_host or '(hashed)',
            'server_name': matched_server, 'key_type': key_type,
            'fingerprint': fingerprint, 'hashed': is_hashed,
        })
    return entries, kh


@ssh_bp.route('/known-hosts', methods=['GET'])
def api_list_known_hosts():
    err = require_tools('ssh-keygen')
    if err:
        return err
    try:
        username = getattr(g, 'username', None)
        entries, path = _parse_known_hosts(username)
        return jsonify({
            'entries': entries, 'path': path,
            'user': username or 'root', 'home': _get_user_home(username),
        })
    except Exception as e:
        log.exception('Error listing known_hosts')
        return jsonify({'error': str(e)}), 500


@ssh_bp.route('/known-hosts/lookup', methods=['POST'])
def api_lookup_host():
    err = require_tools('ssh-keygen')
    if err:
        return err
    data = request.json or {}
    host = data.get('host', '')
    if not host:
        return jsonify({'error': 'host required'}), 400
    username = getattr(g, 'username', None)
    kh = _kh_path(username)
    results = []
    try:
        r = subprocess.run(['ssh-keygen', '-F', host, '-f', kh],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and host in r.stdout:
            for l in r.stdout.strip().split('\n'):
                if not l.startswith('#'):
                    results.append(l.strip())
    except Exception:
        pass
    return jsonify({'found': len(results) > 0, 'host': host, 'entries': results})


@ssh_bp.route('/known-hosts/remove', methods=['POST'])
def api_remove_host():
    err = require_tools('ssh-keygen')
    if err:
        return err
    data = request.json or {}
    host = data.get('host', '')
    if not host:
        return jsonify({'error': 'host required'}), 400
    username = getattr(g, 'username', None)
    kh = _kh_path(username)
    try:
        r = subprocess.run(['ssh-keygen', '-R', host, '-f', kh],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            log.info('[%s] Removed known_hosts entry for %s', username, host)
            return jsonify({'status': 'ok', 'host': host})
        return jsonify({'error': r.stderr.strip() or 'Host not found'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@ssh_bp.route('/known-hosts/remove-line', methods=['POST'])
def api_remove_line():
    data = request.json or {}
    line_num = data.get('line')
    if not line_num or not isinstance(line_num, int):
        return jsonify({'error': 'line number required'}), 400
    username = getattr(g, 'username', None)
    kh = _kh_path(username)
    try:
        with open(kh, 'r') as f:
            lines = f.readlines()
        if line_num < 1 or line_num > len(lines):
            return jsonify({'error': 'Line number out of range'}), 400
        removed = lines[line_num - 1].strip()
        del lines[line_num - 1]
        with open(kh, 'w') as f:
            f.writelines(lines)
        log.info('[%s] Removed known_hosts line %d', username, line_num)
        return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
