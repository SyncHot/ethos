"""
EthOS — Backup History Sub-module
Routes: /backups, /backups/<filename>, /history, /history/<id>
"""

import os
from datetime import datetime

from flask import jsonify, request

from blueprints.backup import (
    backup_bp, BACKUP_DIR, load_history, save_history, load_profiles,
    _fs_call_with_timeout,
)


@backup_bp.route('/backups')
def list_backups():
    backups = []
    scanned_dirs = set()

    # Build mapping: destination_path -> profile info
    profiles = []
    dir_to_profile = {}
    try:
        profiles = load_profiles()
        for profile in profiles:
            dest = profile.get('destination')
            if not dest:
                continue
            if dest.get('type') == 'usb' and dest.get('path'):
                dir_to_profile[dest['path']] = {
                    'profile_id': profile.get('id'),
                    'profile_name': profile.get('name', '')
                }
            elif dest.get('type') == 'local' or not dest.get('type'):
                dir_to_profile[BACKUP_DIR] = {
                    'profile_id': profile.get('id'),
                    'profile_name': profile.get('name', '')
                }
    except Exception:
        pass

    def scan_dir(directory, location_label):
        if directory in scanned_dirs:
            return
        scanned_dirs.add(directory)
        if not os.path.isdir(directory):
            return
        prof = dir_to_profile.get(directory, {})
        try:
            for item in _fs_call_with_timeout(os.listdir, directory, timeout=5):
                is_backup = item.startswith('backup_') and (item.endswith('.tar.gz') or item.endswith('.tar.gz.gpg'))
                if is_backup:
                    fpath = os.path.join(directory, item)
                    try:
                        st = _fs_call_with_timeout(os.stat, fpath, timeout=3)
                        entry = {
                            'name': item, 'size': st.st_size,
                            'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
                            'location': location_label, 'path': fpath,
                            'encrypted': item.endswith('.gpg'),
                        }
                        if prof:
                            entry['profile_id'] = prof['profile_id']
                            entry['profile_name'] = prof['profile_name']
                        backups.append(entry)
                    except Exception:
                        pass
        except Exception:
            pass

    # 1. Local backups
    scan_dir(BACKUP_DIR, 'Lokalnie')

    # 2. Scan destinations from all profiles
    for profile in profiles:
        dest = profile.get('destination')
        if not dest:
            continue
        if dest.get('type') == 'usb' and dest.get('path'):
            scan_dir(dest['path'], f"USB: {dest['path']}")

    backups.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify({'backups': backups})

@backup_bp.route('/backups/<filename>', methods=['DELETE'])
def delete_backup(filename):
    # Support deleting from a specific path (passed as query param)
    custom_path = request.args.get('path')
    if custom_path and os.path.exists(custom_path) and (custom_path.endswith('.tar.gz') or custom_path.endswith('.tar.gz.gpg')):
        os.remove(custom_path)
        return jsonify({'success': True})
    bp = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(bp):
        os.remove(bp)
        return jsonify({'success': True})
    return jsonify({'error': 'Backup does not exist'}), 404


@backup_bp.route('/history')
def get_history_route():
    return jsonify({'history': load_history()})

@backup_bp.route('/history/<entry_id>', methods=['DELETE'])
def delete_history_entry(entry_id):
    history = [h for h in load_history() if h.get('id') != entry_id]
    save_history(history)
    return jsonify({'success': True})
