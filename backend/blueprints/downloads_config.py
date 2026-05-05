"""Download Manager — Configuration routes."""

from flask import request, jsonify

from blueprints.downloads import (
    downloads_bp,
    _load_config, _save_config, _start_watch_folder,
)


@downloads_bp.route('/api/downloads/config')
def get_config():
    cfg = _load_config()
    # Mask API keys
    safe = dict(cfg)
    for k in ('alldebrid_api_key', 'realdebrid_api_key', 'premiumize_api_key', 'debridlink_api_key', 'torbox_api_key'):
        if safe.get(k):
            safe[k] = safe[k][:4] + '***' + safe[k][-4:]
    return jsonify({'ok': True, 'config': safe})


@downloads_bp.route('/api/downloads/config', methods=['PUT'])
def set_config():
    data = request.get_json(force=True)
    cfg = _load_config()

    if 'default_dir' in data:
        cfg['default_dir'] = data['default_dir']
    if 'default_dir_torrent' in data:
        cfg['default_dir_torrent'] = data['default_dir_torrent']
    if 'watch_folder' in data:
        cfg['watch_folder'] = data['watch_folder']
    if 'watch_folder_enabled' in data:
        cfg['watch_folder_enabled'] = bool(data['watch_folder_enabled'])
    if 'max_concurrent' in data:
        cfg['max_concurrent'] = max(1, min(10, int(data['max_concurrent'])))
    if 'overwrite_existing' in data:
        cfg['overwrite_existing'] = bool(data['overwrite_existing'])
    if 'speed_limit' in data:
        cfg['speed_limit'] = max(0, int(data['speed_limit']))
    if 'debrid_service' in data:
        cfg['debrid_service'] = data['debrid_service']
    if 'auto_categorize' in data:
        cfg['auto_categorize'] = bool(data['auto_categorize'])
    if 'categories' in data:
        cfg['categories'] = data['categories']

    # Only update API keys if new value provided (not masked)
    for key in ('alldebrid_api_key', 'realdebrid_api_key', 'premiumize_api_key', 'debridlink_api_key', 'torbox_api_key'):
        if key in data and data[key] and '***' not in data[key]:
            cfg[key] = data[key]

    _save_config(cfg)
    # Restart watch folder if settings changed
    if 'watch_folder' in data or 'watch_folder_enabled' in data:
        _start_watch_folder()
    return jsonify({'ok': True})
