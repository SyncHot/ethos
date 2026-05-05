"""Download Manager — Archive Extraction routes."""

import time
import uuid

from flask import request, jsonify

from blueprints.downloads import (
    downloads_bp,
    _lock, _downloads, _packages,
    _enqueue_extraction, _emit, _sanitize_package, _save_state,
)
from utils import require_tools


@downloads_bp.route('/api/downloads/extract', methods=['POST'])
def extract_package():
    """Trigger deep extraction for a package or single download."""
    err = require_tools('7z')
    if err:
        return err
    data = request.get_json(force=True)
    package_id = data.get('package_id', '')
    password = data.get('password', '')
    delete_after = data.get('delete_after', False)

    if not package_id:
        # Single download extract — create ad-hoc "package"
        dl_id = data.get('id', '')
        with _lock:
            dl = _downloads.get(dl_id)
            if not dl:
                return jsonify({'error': 'Not found'}), 404
            if dl.get('status') != 'completed':
                return jsonify({'error': 'Download not completed'}), 400
            dest = dl.get('dest_dir', '')
            if not dest:
                return jsonify({'error': 'Target folder not found'}), 400
        # Create temporary package for this single download
        package_id = 'pkg_' + str(uuid.uuid4())[:8]
        pkg = {
            'id': package_id,
            'name': dl.get('filename', 'Extraction'),
            'dl_ids': [dl_id],
            'dest_dir': dest,
            'status': 'downloading',
            'auto_extract': False,
            'delete_after_extract': delete_after,
            'extract_password': password,
            'extract_error': '',
            'created_at': time.time(),
            'has_archives': True,
        }
        with _lock:
            dl['package_id'] = package_id
            _packages[package_id] = pkg
            _save_state()
        _emit('dl:package_update', _sanitize_package(pkg))

    with _lock:
        pkg = _packages.get(package_id)
        if not pkg:
            return jsonify({'error': 'Package not found'}), 404
        if pkg.get('status') == 'extracting':
            return jsonify({'error': 'Extraction already in progress'}), 400
        # Update password/delete if provided
        if password:
            pkg['extract_password'] = password
        if delete_after is not None:
            pkg['delete_after_extract'] = bool(delete_after)

    _enqueue_extraction(package_id)
    return jsonify({'ok': True, 'package_id': package_id})


@downloads_bp.route('/api/downloads/package/remove', methods=['POST'])
def remove_package():
    """Remove a package (not the downloads themselves)."""
    data = request.get_json(force=True)
    pkg_id = data.get('package_id', '')
    with _lock:
        pkg = _packages.pop(pkg_id, None)
        if not pkg:
            return jsonify({'error': 'Not found'}), 404
        # Clear package_id from all related downloads
        for dl_id in pkg.get('dl_ids', []):
            dl = _downloads.get(dl_id)
            if dl:
                dl['package_id'] = ''
        _save_state()
    _emit('dl:package_removed', {'id': pkg_id})
    return jsonify({'ok': True})
