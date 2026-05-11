"""Download Manager — History & Statistics routes."""

import json
import os
import time
import uuid

from flask import request, jsonify

from blueprints.downloads import (
    downloads_bp,
    _lock, _downloads, _packages,
    _history_lock, DOWNLOADS_HISTORY_FILE,
    _load_history, _get_username, _emit,
    _start_next, _save_state, _is_torrent, _sanitize,
    _atomic_write_json, _load_config, _normalize_url,
)


@downloads_bp.route('/api/downloads/stats')
def download_stats():
    """Return aggregate download statistics."""
    me = _get_username()
    with _lock:
        _my = [d for d in _downloads.values() if not me or d.get('user', '') == me or not d.get('user')]
        total = len(_my)
        active = sum(1 for d in _my
                     if d['status'] in ('downloading', 'resolving',
                                        'torrent_uploading', 'torrent_downloading'))
        completed = sum(1 for d in _my if d['status'] == 'completed')
        failed = sum(1 for d in _my if d['status'] == 'failed')
        paused = sum(1 for d in _my if d['status'] == 'paused')
        pending = sum(1 for d in _my if d['status'] == 'pending')
        total_bytes = sum(d.get('downloaded', 0) for d in _my
                         if d['status'] == 'completed')
        current_speed = 0
        for d in _my:
            if d['status'] == 'downloading':
                current_speed += d.get('speed', 0)
            elif d['status'] == 'torrent_downloading':
                current_speed += d.get('torrent_speed', 0)
        my_pkgs = sum(1 for p in _packages.values() if not me or p.get('user', '') == me or not p.get('user'))
    history = _load_history(username=me)
    now = time.time()
    periods = {
        'today': now - 86400,
        'week': now - 7 * 86400,
        'month': now - 30 * 86400,
    }
    bytes_by_period = {k: 0 for k in periods}
    counts = {'completed': 0, 'failed': 0, 'cancelled': 0}
    total_bytes_hist = 0
    total_duration = 0.0
    for h in history:
        event = h.get('event')
        ts = h.get('timestamp', 0) or 0
        size = h.get('filesize', 0) or 0
        duration = h.get('duration', 0) or 0
        if event == 'completed':
            counts['completed'] += 1
            total_bytes_hist += size
            if duration > 0:
                total_duration += duration
            for key, cutoff in periods.items():
                if ts >= cutoff:
                    bytes_by_period[key] += size
        elif event == 'failed':
            counts['failed'] += 1
        elif event == 'cancelled':
            counts['cancelled'] += 1
    avg_speed = int(total_bytes_hist / total_duration) if total_duration > 0 else 0
    return jsonify({
        'ok': True,
        'stats': {
            'total': total, 'active': active, 'completed': completed,
            'failed': failed, 'paused': paused, 'pending': pending,
            'total_bytes_downloaded': total_bytes,
            'current_speed': current_speed,
            'packages': my_pkgs,
        },
        'metrics': {
            'bytes': {**bytes_by_period, 'all_time': total_bytes_hist},
            'counts': counts,
            'average_speed': avg_speed,
            'history_entries': len(history),
        },
    })


@downloads_bp.route('/api/downloads/history')
def download_history():
    """Return download history log with search, filter, pagination."""
    me = _get_username()
    history = _load_history(username=me)
    # Return in reverse chronological order
    history.reverse()

    # Filtering
    q = request.args.get('q', '').lower()
    status = request.args.get('status', '')
    source = request.args.get('source', '')
    start_ts = request.args.get('start', type=float)
    end_ts = request.args.get('end', type=float)

    if q:
        history = [h for h in history if q in h.get('filename', '').lower() or q in h.get('url', '').lower()]

    if status:
        # status in history is 'event' (completed, failed, cancelled)
        history = [h for h in history if h.get('event') == status]

    if source:
        # source: torrent, direct. Debrid logic is complex (uses direct URL but originated from magnet/link)
        # simplistic check: is_torrent field
        if source == 'torrent':
            history = [h for h in history if h.get('is_torrent')]
        elif source == 'direct':
            history = [h for h in history if not h.get('is_torrent') and not h.get('use_debrid')]
        elif source == 'debrid':
            history = [h for h in history if h.get('use_debrid') and not h.get('is_torrent')]

    if start_ts:
        history = [h for h in history if h.get('timestamp', 0) >= start_ts]
    if end_ts:
        # end_ts is usually start of next day, so strictly less
        history = [h for h in history if h.get('timestamp', 0) < end_ts]

    total = len(history)
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    
    return jsonify({
        'ok': True, 
        'history': history[start_idx:end_idx],
        'total': total,
        'page': page,
        'limit': limit
    })


@downloads_bp.route('/api/downloads/history/clear', methods=['POST'])
def clear_history():
    """Clear download history for the current user."""
    me = _get_username()
    data = request.get_json(force=True)
    older_than_days = data.get('older_than_days')

    with _history_lock:
        if os.path.isfile(DOWNLOADS_HISTORY_FILE):
            try:
                with open(DOWNLOADS_HISTORY_FILE) as f:
                    history = json.load(f)
            except Exception:
                history = []
        else:
            history = []

        if older_than_days is not None:
            cutoff = time.time() - (int(older_than_days) * 86400)
            # Keep entries that belong to other users OR are newer than cutoff for current user
            history = [h for h in history if h.get('user') != me or h.get('timestamp', 0) > cutoff]
        else:
            # Remove only entries belonging to the current user
            history = [h for h in history if h.get('user') != me]

        _atomic_write_json(DOWNLOADS_HISTORY_FILE, history)

    return jsonify({'ok': True})


def _check_url_in_history(normalized_url):
    """Check if a normalized URL exists in history. Returns dict with status and filename if found, None otherwise."""
    hist = _load_history()
    # Search from newest to oldest
    for entry in reversed(hist):
        entry_normalized = _normalize_url(entry.get('url', ''))
        if entry_normalized == normalized_url:
            return {
                'status': entry.get('status'),
                'filename': entry.get('filename', ''),
                'completed_at': entry.get('completed_at', 0)
            }
    return None


@downloads_bp.route('/api/downloads/history/retry', methods=['POST'])
def retry_history_download():
    """Retry a download from history."""
    data = request.get_json(force=True)
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    # Handle legacy torrent:// URLs — not retryable without original magnet/torrent
    if url.startswith('torrent://'):
        return jsonify({'error': 'Cannot retry: original torrent data no longer available. '
                        'Please re-add the magnet link or .torrent file.'}), 400

    dl_id = str(uuid.uuid4())[:8]
    is_t = _is_torrent(url)
    dest_dir = data.get('dest_dir')

    if not dest_dir:
        _cfg = _load_config()
        _default_key = 'default_dir_torrent' if is_t else 'default_dir'
        dest_dir = _cfg.get(_default_key, '/home')

    # Respect original debrid setting from history; default True for torrents
    use_debrid = data.get('use_debrid', True) if is_t else data.get('use_debrid', True)

    dl = {
        'id': dl_id,
        'url': url,
        'filename': data.get('filename', ''),
        'filesize': 0,
        'downloaded': 0,
        'progress': 0,
        'speed': 0,
        'status': 'pending',
        'error': '',
        'debrid_error': '',
        'dest_dir': dest_dir,
        'dest_path': '',
        'use_debrid': use_debrid,
        'added_at': time.time(),
        'started_at': 0,
        'completed_at': 0,
        'is_torrent': is_t,
        'package_id': '',
        'user': _get_username() or '',
    }

    with _lock:
        _downloads[dl_id] = dl
        _save_state()

    _emit('dl:update', _sanitize(dl))
    _start_next()
    return jsonify({'ok': True, 'id': dl_id})
