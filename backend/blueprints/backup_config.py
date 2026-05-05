"""
EthOS — Backup Config Sub-module
Routes: /browse/roots, /browse, /browse/mkdir, /paths, /usb-drives, /usb-browse,
        /usb-mkdir, /ssh-servers, /ssh-servers/<id>, /ssh-servers/test
"""

import os

from flask import jsonify, request, g

from blueprints.backup import (
    backup_bp, _effective_browse_roots, BROWSE_ROOTS,
    load_paths, save_paths, detect_usb_drives,
    load_ssh_configs, save_ssh_configs, test_ssh_connection, HAS_SSH,
    _list_dir, _fs_call_with_timeout,
)
from datetime import datetime


@backup_bp.route('/browse/roots')
def browse_roots():
    roots = []
    for r in _effective_browse_roots():
        try:
            stat = os.statvfs(r)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bfree * stat.f_frsize
            roots.append({'path': r, 'name': r, 'total': total, 'free': free})
        except OSError:
            roots.append({'path': r, 'name': r, 'total': None, 'free': None})
    return jsonify({'roots': roots})

@backup_bp.route('/browse')
def browse_directory():
    allowed = _effective_browse_roots()
    if not allowed:
        return jsonify({'error': 'Access denied'}), 403
    path = request.args.get('path', allowed[0])
    if not any(path.startswith(r) for r in allowed):
        path = allowed[0]
    items, err = _list_dir(path, allowed_prefix=allowed, timeout=5)
    if err:
        code = 404 if 'Not a directory' in err else 400 if 'outside' in err else 403
        return jsonify({'error': err}), code
    # Convert to camelCase keys + add sizeDisplay
    out = []
    for i in items:
        sz = i['size']
        if i['is_dir']:
            sd = 'folder'
        elif sz < 1024:
            sd = f'{sz} B'
        elif sz < 1048576:
            sd = f'{sz / 1024:.1f} KB'
        elif sz < 1073741824:
            sd = f'{sz / 1048576:.1f} MB'
        else:
            sd = f'{sz / 1073741824:.2f} GB'
        out.append({
            'name': i['name'], 'path': i['path'], 'isDir': i['is_dir'],
            'size': i['size'] if not i['is_dir'] else None,
            'sizeDisplay': sd, 'modified': i['modified'],
        })
    parent_path = os.path.dirname(path) if path not in allowed else None
    if parent_path and not any(parent_path.startswith(r) for r in allowed):
        parent_path = None
    return jsonify({'currentPath': path, 'parentPath': parent_path, 'items': out})


@backup_bp.route('/browse/mkdir', methods=['POST'])
def browse_mkdir():
    allowed = _effective_browse_roots()
    data = request.json or {}
    parent = data.get('path', '')
    name = data.get('name', '').strip()
    if not parent or not name:
        return jsonify({'error': 'Path and name required'}), 400
    if not any(parent.startswith(r) for r in allowed):
        return jsonify({'error': 'Invalid path'}), 400
    if '/' in name or '..' in name:
        return jsonify({'error': 'Invalid name'}), 400
    new_path = os.path.join(parent, name)
    if os.path.exists(new_path):
        return jsonify({'error': 'Folder already exists'}), 400
    try:
        os.makedirs(new_path)
        return jsonify({'success': True, 'path': new_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/paths', methods=['GET'])
def get_paths():
    return jsonify({'paths': load_paths()})

@backup_bp.route('/paths', methods=['POST'])
def add_path():
    data = request.json or {}
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    if not os.path.exists(path):
        return jsonify({'error': f'Path does not exist: {path}'}), 400
    paths = load_paths()
    if path not in paths:
        paths.append(path)
        save_paths(paths)
    return jsonify({'success': True, 'paths': paths})

@backup_bp.route('/paths', methods=['DELETE'])
def remove_path():
    data = request.json or {}
    path = data.get('path', '').strip()
    paths = load_paths()
    if path in paths:
        paths.remove(path)
        save_paths(paths)
    return jsonify({'success': True, 'paths': paths})


@backup_bp.route('/usb-drives')
def get_usb_drives():
    return jsonify({'drives': detect_usb_drives()})

@backup_bp.route('/usb-browse')
def browse_usb():
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    allowed = ['/data/media', '/data/run_media', '/data/mnt', '/media', '/run/media', '/mnt']
    items, err = _list_dir(path, allowed_prefix=allowed, dirs_only=True, timeout=5)
    if err:
        code = 400 if 'outside' in err else 404 if 'Not a dir' in err else 403
        return jsonify({'error': err}), code
    # Convert to camelCase
    out = [{'name': i['name'], 'path': i['path'], 'isDir': True} for i in items]
    parent = os.path.dirname(path)
    if not any(parent.startswith(p) for p in allowed):
        parent = None
    usb_drives = detect_usb_drives()
    usb_roots = set(d['path'] for d in usb_drives)
    if path in usb_roots:
        parent = None
    return jsonify({'currentPath': path, 'parentPath': parent, 'items': out})

@backup_bp.route('/usb-mkdir', methods=['POST'])
def create_usb_folder():
    data = request.json or {}
    parent = data.get('path', '')
    name = data.get('name', '').strip()
    if not parent or not name:
        return jsonify({'error': 'Path and name required'}), 400
    allowed = ['/data/media', '/data/run_media', '/data/mnt', '/media', '/run/media', '/mnt']
    if not any(parent.startswith(p) for p in allowed):
        return jsonify({'error': 'Invalid path'}), 400
    if '/' in name or '..' in name:
        return jsonify({'error': 'Invalid name'}), 400
    new_path = os.path.join(parent, name)
    if os.path.exists(new_path):
        return jsonify({'error': 'Folder already exists'}), 400
    try:
        os.makedirs(new_path)
        return jsonify({'success': True, 'path': new_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/ssh-servers', methods=['GET'])
def get_ssh_servers():
    configs = load_ssh_configs()
    safe = [{k: v for k, v in c.items() if k != 'password'} | {'has_password': bool(c.get('password'))} for c in configs]
    return jsonify({'servers': safe})

@backup_bp.route('/ssh-servers', methods=['POST'])
def add_ssh_server():
    data = request.json or {}
    for field in ['name', 'host', 'username']:
        if not data.get(field):
            return jsonify({'error': f'{field} required'}), 400
    configs = load_ssh_configs()
    # Prevent duplicates (same host + username)
    for existing in configs:
        if existing.get('host') == data['host'] and existing.get('username') == data['username']:
            # Update existing instead of adding duplicate
            existing['name'] = data['name']
            existing['port'] = int(data.get('port', 22))
            existing['password'] = data.get('password', '')
            existing['key_path'] = data.get('key_path', '')
            existing['remote_path'] = data.get('remote_path', '~/backups')
            save_ssh_configs(configs)
            return jsonify({'success': True, 'server': {k: v for k, v in existing.items() if k != 'password'}, 'updated': True})
    new = {
        'id': datetime.now().strftime("%Y%m%d%H%M%S"),
        'name': data['name'], 'host': data['host'],
        'port': int(data.get('port', 22)), 'username': data['username'],
        'password': data.get('password', ''), 'key_path': data.get('key_path', ''),
        'remote_path': data.get('remote_path', '~/backups')
    }
    configs.append(new)
    save_ssh_configs(configs)
    return jsonify({'success': True, 'server': {k: v for k, v in new.items() if k != 'password'}})

@backup_bp.route('/ssh-servers/<server_id>', methods=['DELETE'])
def delete_ssh_server(server_id):
    configs = [c for c in load_ssh_configs() if c.get('id') != server_id]
    save_ssh_configs(configs)
    return jsonify({'success': True})

@backup_bp.route('/ssh-servers/test', methods=['POST'])
def test_ssh():
    data = request.json or {}
    # If called with just 'id', load full server config
    server_id = data.get('id')
    if server_id:
        configs = load_ssh_configs()
        srv = next((c for c in configs if c.get('id') == server_id), None)
        if not srv:
            return jsonify({'success': False, 'error': 'Server not found'}), 404
        host = srv.get('host')
        port = int(srv.get('port', 22))
        username = srv.get('username')
        password = srv.get('password')
        key_path = srv.get('key_path')
        remote_path = srv.get('remote_path')
    else:
        host = data.get('host')
        port = int(data.get('port', 22))
        username = data.get('username')
        password = data.get('password')
        key_path = data.get('key_path')
        remote_path = data.get('remote_path')

    if not host or not username:
        return jsonify({'success': False, 'error': 'Host or username missing'}), 400

    ok, result = test_ssh_connection(host, port, username, password, key_path, remote_path)
    if ok:
        return jsonify({'success': True, 'name': data.get('name') or (srv.get('name') if server_id else ''), 'disk_info': result})
    return jsonify({'success': False, 'error': result}), 400
