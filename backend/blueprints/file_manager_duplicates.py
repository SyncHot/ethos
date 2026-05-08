"""EthOS — File Manager: duplicate finder, rename, move, copy, package management."""
import os
import sys
import sys as _sys
import json
import time
import re
import shutil
import hashlib
import secrets
import errno
import threading as _threading
import gevent
from PIL import Image
from flask import request, jsonify
from i18n import t
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from host import (
    data_path as _data_path,
    ETHOS_ROOT,
    host_run as _host_run,
    q,
    get_user_home as _get_user_home,
    user_data_path as _user_data_path,
    fs_call_with_timeout as _fs_call,
)
from utils import (
    load_json as _load_json,
    save_json as _save_json,
    DATA_ROOT,
    ALLOWED_ROOTS as _ALLOWED_ROOTS,
)
from blueprints.eventlog import log as elog
from blueprints.admin_required import admin_required
from blueprints.auth import require_auth, get_current_user


def _main():
    """Return the main file_manager module."""
    return _sys.modules['blueprints.file_manager']


files_bp = _sys.modules['blueprints.file_manager'].files_bp

class _SocketioProxy:
    """Lazy proxy for socketio – avoids import-lock issues under gevent."""
    def __getattr__(self, name):
        sio = getattr(_sys.modules.get('app'), 'socketio', None)
        if sio is None:
            raise AttributeError(f'socketio not yet available ({name!r})')
        return getattr(sio, name)
socketio = _SocketioProxy()

# ─────────────────── Duplicate Photo Finder ───────────────────────

_dup_scan = {
    'running': False,
    'cancel': False,
    'scan_id': None,
    'phase': '',
    'scanned': 0,
    'total': 0,
    'found_groups': 0,
    'results': [],         # list of groups — populated INCREMENTALLY
    'error': None,
}

# ── Hash cache — avoids re-computing SHA256 / dhash on unchanged files ──
DUP_HASH_CACHE_FILE = _data_path('dup_hash_cache.json')

def _load_hash_cache():
    return _load_json(DUP_HASH_CACHE_FILE, {})

def _save_hash_cache(cache):
    try:
        _save_json(DUP_HASH_CACHE_FILE, cache)
    except Exception:
        pass

_MAX_HASH_CACHE_ENTRIES = 50000

def _prune_hash_cache(cache):
    """Remove oldest entries if cache exceeds max size."""
    if len(cache) <= _MAX_HASH_CACHE_ENTRIES:
        return cache
    # Keep newest entries by mtime
    sorted_keys = sorted(cache.keys(), key=lambda k: cache[k].get('mtime', 0), reverse=True)
    pruned = {k: cache[k] for k in sorted_keys[:_MAX_HASH_CACHE_ENTRIES]}
    return pruned

# ── Ignored duplicate groups ──
DUP_IGNORED_FILE = _data_path('dup_ignored.json')


def _dup_group_key(group):
    """Stable key for a dup group based on sorted file paths."""
    paths = sorted(f['path'] for f in group.get('items', []))
    return hashlib.md5('|'.join(paths).encode()).hexdigest()


def _load_dup_ignored():
    return _load_json(DUP_IGNORED_FILE, {})

def _save_dup_ignored(data):
    _save_json(DUP_IGNORED_FILE, data)

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.heic', '.heif'}


def _dhash(img, hash_size=10):
    """Compute difference hash (dhash) — perceptual hash for visual similarity."""
    img = img.convert('L').resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    w = hash_size + 1
    bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            bits.append(1 if pixels[row * w + col] < pixels[row * w + col + 1] else 0)
    return int(''.join(str(b) for b in bits), 2)


def _hamming(h1, h2):
    """Hamming distance between two integer hashes."""
    return bin(h1 ^ h2).count('1')


def _emit_new_group(group):
    """Send a newly found group to the frontend in real-time."""
    socketio.emit('dup_new_group', {
        'group': group,
        'total_groups': _dup_scan['found_groups'],
    })


def _scan_duplicates(scan_paths, mode, threshold):
    """Background task: find duplicate images — emits groups incrementally."""
    global _dup_scan
    try:
        _dup_scan['phase'] = 'Searching for images…'
        _dup_scan['scanned'] = 0
        socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': 0})

        # Load hash cache for faster re-scans
        hash_cache = _load_hash_cache()
        cache_hits = 0

        # Collect all image files from all scan paths
        image_files = []
        seen_real = set()
        _file_count = 0
        for scan_path in scan_paths:
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            for dirpath, _dirs, filenames in os.walk(scan_path):
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                # Skip hidden dirs and trash
                if '/.trash' in dirpath or '/.' in dirpath.split(scan_path)[-1]:
                    continue
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in IMAGE_EXTS:
                        fp = os.path.join(dirpath, fn)
                        real = os.path.realpath(fp)
                        if real in seen_real:
                            continue
                        seen_real.add(real)
                        try:
                            st = os.stat(fp)
                            image_files.append({
                                'path': fp.replace(DATA_ROOT, '', 1),
                                'real_path': fp,
                                'size': st.st_size,
                                'modified': st.st_mtime,
                                'name': fn,
                            })
                        except OSError:
                            pass
                    _file_count += 1
                    if _file_count % 5 == 0:
                        gevent.sleep(0)
                gevent.sleep(0)  # yield per directory

        total = len(image_files)
        _dup_scan['total'] = total
        _dup_scan['phase'] = f'Found {total} images, analyzing…'
        socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

        if total == 0:
            _dup_scan['running'] = False
            _dup_scan['phase'] = 'Completed'
            socketio.emit('dup_complete', {'groups': 0, 'duplicates': 0, 'size': 0})
            return

        exact_path_sets = set()  # track exact groups to avoid duplication in similar phase

        if mode in ('exact', 'both'):
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            # ── Phase: exact duplicates by SHA256 ──
            _dup_scan['phase'] = 'Comparing checksums (SHA256)…'
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

            # Pre-filter: group by size
            by_size = {}
            for f in image_files:
                by_size.setdefault(f['size'], []).append(f)
            size_groups = {k: v for k, v in by_size.items() if len(v) > 1}

            # Hash only size-matched files
            hash_map = {}
            scanned = 0
            to_hash = [f for grp in size_groups.values() for f in grp]
            for f in to_hash:
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                try:
                    cache_key = f['real_path']
                    cached = hash_cache.get(cache_key)
                    if cached and cached.get('mtime') == f['modified'] and cached.get('size') == f['size'] and cached.get('sha256'):
                        digest = cached['sha256']
                        cache_hits += 1
                    else:
                        h = hashlib.sha256()
                        with open(f['real_path'], 'rb') as fh:
                            _chunks = 0
                            while True:
                                chunk = fh.read(65536)
                                if not chunk:
                                    break
                                h.update(chunk)
                                _chunks += 1
                                if _chunks % 16 == 0:  # yield every ~1MB
                                    gevent.sleep(0)
                        digest = h.hexdigest()
                        # Update cache
                        if cache_key not in hash_cache:
                            hash_cache[cache_key] = {}
                        hash_cache[cache_key]['sha256'] = digest
                        hash_cache[cache_key]['mtime'] = f['modified']
                        hash_cache[cache_key]['size'] = f['size']
                    hash_map.setdefault(digest, []).append(f)
                except Exception:
                    pass
                scanned += 1
                gevent.sleep(0)  # yield after every file hash
                if scanned % 20 == 0:
                    _dup_scan['scanned'] = scanned
                    socketio.emit('dup_progress', {
                        'phase': f'SHA256: {scanned}/{len(to_hash)}',
                        'scanned': scanned, 'total': len(to_hash)
                    })

            # Emit exact duplicate groups incrementally
            for digest, files in hash_map.items():
                if len(files) > 1:
                    items = sorted(files, key=lambda x: x['modified'])
                    # Strip real_path before sending
                    items_clean = [{k: v for k, v in it.items() if k != 'real_path'} for it in items]
                    group = {
                        'hash': digest[:16],
                        'type': 'exact',
                        'items': items_clean,
                    }
                    _dup_scan['results'].append(group)
                    _dup_scan['found_groups'] = len(_dup_scan['results'])
                    _emit_new_group(group)
                    exact_path_sets.add(tuple(sorted(f['path'] for f in items)))
                    gevent.sleep(0)

        if mode in ('similar', 'both'):
            if _dup_scan.get('cancel'):
                raise InterruptedError('Cancelled')
            # ── Phase: visually similar by dhash ──
            _dup_scan['phase'] = 'Analiza wizualna (perceptual hash)…'
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': total})

            phash_list = []  # [(file_info, hash_int)]
            scanned = 0
            for f in image_files:
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                try:
                    cache_key = f['real_path']
                    cached = hash_cache.get(cache_key)
                    if cached and cached.get('mtime') == f['modified'] and cached.get('size') == f['size'] and cached.get('dhash') is not None:
                        h = cached['dhash']
                        cache_hits += 1
                    else:
                        img = Image.open(f['real_path'])
                        h = _dhash(img)
                        # Update cache
                        if cache_key not in hash_cache:
                            hash_cache[cache_key] = {}
                        hash_cache[cache_key]['dhash'] = h
                        hash_cache[cache_key]['mtime'] = f['modified']
                        hash_cache[cache_key]['size'] = f['size']
                    phash_list.append((f, h))
                except Exception:
                    pass
                scanned += 1
                gevent.sleep(0)  # yield after every image
                if scanned % 20 == 0:
                    _dup_scan['scanned'] = scanned
                    socketio.emit('dup_progress', {
                        'phase': f'Perceptual hash: {scanned}/{total}',
                        'scanned': scanned, 'total': total
                    })

            # ── Phase: clustering ──
            # Use bucket approach to avoid full O(n²): bucket by coarse hash chunks
            _dup_scan['phase'] = 'Grouping similar images…'
            n = len(phash_list)
            socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': n})

            parent = list(range(n))

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

            # Bucket by portions of the hash to reduce comparisons
            HASH_BITS = 100  # 10x10 dhash = 100 bits
            BUCKET_BITS = 25  # split into 4 bands
            num_bands = HASH_BITS // BUCKET_BITS

            buckets = [{} for _ in range(num_bands)]
            for i, (f, h) in enumerate(phash_list):
                for band_idx in range(num_bands):
                    band_val = (h >> (band_idx * BUCKET_BITS)) & ((1 << BUCKET_BITS) - 1)
                    buckets[band_idx].setdefault(band_val, []).append(i)

            compared = set()
            comparisons_done = 0
            for band_idx in range(num_bands):
                if _dup_scan.get('cancel'):
                    raise InterruptedError('Cancelled')
                for candidates in buckets[band_idx].values():
                    if len(candidates) < 2:
                        continue
                    for ci in range(len(candidates)):
                        for cj in range(ci + 1, len(candidates)):
                            i, j = candidates[ci], candidates[cj]
                            pair = (min(i, j), max(i, j))
                            if pair in compared:
                                continue
                            compared.add(pair)
                            if _hamming(phash_list[i][1], phash_list[j][1]) <= threshold:
                                union(i, j)
                            comparisons_done += 1
                            if comparisons_done % 50 == 0:
                                gevent.sleep(0)  # yield frequently during comparisons
                            if comparisons_done % 500 == 0:
                                socketio.emit('dup_progress', {
                                    'phase': f'Comparing: {comparisons_done} pairs',
                                    'scanned': comparisons_done, 'total': comparisons_done
                                })

            # For high thresholds, also do a limited brute-force on remaining
            if threshold >= 10 and n <= 5000:
                _dup_scan['phase'] = 'Additional comparisons…'
                socketio.emit('dup_progress', {'phase': _dup_scan['phase'], 'scanned': 0, 'total': n})
                _bf_ops = 0
                for i in range(n):
                    if _dup_scan.get('cancel'):
                        raise InterruptedError('Cancelled')
                    for j in range(i + 1, n):
                        pair = (i, j)
                        if pair in compared:
                            continue
                        if _hamming(phash_list[i][1], phash_list[j][1]) <= threshold:
                            union(i, j)
                        _bf_ops += 1
                        if _bf_ops % 50 == 0:
                            gevent.sleep(0)
                    if i % 50 == 0:
                        socketio.emit('dup_progress', {
                            'phase': f'Brute-force: {i}/{n}',
                            'scanned': i, 'total': n
                        })
                        gevent.sleep(0)

            # Collect clusters & emit incrementally
            clusters = {}
            for i in range(n):
                root = find(i)
                clusters.setdefault(root, []).append(i)

            for idxs in clusters.values():
                if len(idxs) < 2:
                    continue
                items = [phash_list[i][0] for i in idxs]
                paths = tuple(sorted(f['path'] for f in items))
                if paths in exact_path_sets:
                    continue
                items_sorted = sorted(items, key=lambda x: x['modified'])
                items_clean = [{k: v for k, v in it.items() if k != 'real_path'} for it in items_sorted]
                group = {
                    'hash': f'phash_{idxs[0]}',
                    'type': 'similar',
                    'items': items_clean,
                }
                _dup_scan['results'].append(group)
                _dup_scan['found_groups'] = len(_dup_scan['results'])
                _emit_new_group(group)
                gevent.sleep(0)

        # Sort results by total size (descending)
        _dup_scan['results'].sort(key=lambda g: sum(f['size'] for f in g['items']), reverse=True)

        _dup_scan['running'] = False
        _dup_scan['phase'] = 'Completed'
        groups = _dup_scan['results']
        total_dups = sum(len(g['items']) - 1 for g in groups)
        dup_size = sum(sum(f['size'] for f in g['items'][1:]) for g in groups)
        socketio.emit('dup_complete', {
            'groups': len(groups),
            'duplicates': total_dups,
            'size': dup_size,
        })
        elog('files', 'info',
             f'Duplicate scan completed: {len(groups)} groups, '
             f'{total_dups} duplicates ({dup_size} bytes), '
             f'{cache_hits} cache hits')
        # Save hash cache for future scans
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)

    except InterruptedError:
        _dup_scan['running'] = False
        _dup_scan['cancel'] = False
        _dup_scan['phase'] = 'Cancelled'
        socketio.emit('dup_cancelled', {
            'groups': _dup_scan['found_groups']
        })
        elog('files', 'info', f'Duplicate scan cancelled ({_dup_scan["found_groups"]} groups found)')
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)
    except Exception as e:
        _dup_scan['running'] = False
        _dup_scan['error'] = str(e)
        _dup_scan['phase'] = 'Error'
        socketio.emit('dup_error', {'error': str(e)})
        elog('files', 'error', f'Duplicate scan error: {str(e)}')
        hash_cache = _prune_hash_cache(hash_cache)
        _save_hash_cache(hash_cache)


def get_dupscan_notifications():
    """Return notification items for the duplicate scan."""
    notifs = []
    if _dup_scan['running']:
        phase = _dup_scan.get('phase', 'Scanning…')
        found = _dup_scan.get('found_groups', 0)
        scanned = _dup_scan.get('scanned', 0)
        total = _dup_scan.get('total', 0)
        pct = (round(scanned / total * 100) if total > 0 else 0)
        notifs.append({
            'type': 'progress',
            'title': 'Finding duplicates',
            'message': f'{phase} — {found} grup, {pct}%',
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    elif _dup_scan.get('phase') == 'Completed' and _dup_scan.get('results'):
        groups = _dup_scan['results']
        total_dups = sum(len(g['items']) - 1 for g in groups)
        notifs.append({
            'type': 'success',
            'title': 'Duplicates found',
            'message': f'{len(groups)} groups, {total_dups} redundant files',
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    elif _dup_scan.get('phase') == 'Cancelled' and _dup_scan.get('results'):
        found = len(_dup_scan['results'])
        if found > 0:
            notifs.append({
                'type': 'warning',
                'title': 'Scan cancelled',
                'message': f'{found} groups found before cancellation',
                'time': time.time(),
                'action': {'app': 'duplicates'}
            })
    elif _dup_scan.get('error'):
        notifs.append({
            'type': 'error',
            'title': 'Duplicate scan error',
            'message': str(_dup_scan['error']),
            'time': time.time(),
            'action': {'app': 'duplicates'}
        })
    return notifs


@files_bp.route('/api/files/duplicates/scan', methods=['POST'])
@require_auth
def files_duplicates_scan():
    """Start a duplicate photo scan."""
    if _dup_scan['running']:
        return jsonify({'error': 'Scan already in progress'}), 409

    data = request.json or {}
    # Support both single path and multiple paths
    paths_rel = data.get('paths', [])
    if not paths_rel and data.get('path'):
        paths_rel = [data['path']]
    if not paths_rel:
        paths_rel = ['/home']
    mode = data.get('mode', 'both')        # exact | similar | both
    threshold = data.get('threshold', 8)     # hamming distance for similar

    scan_paths = []
    for p in paths_rel:
        rp = _main().safe_path(p)
        if rp and os.path.isdir(rp):
            scan_paths.append(rp)
    if not scan_paths:
        return jsonify({'error': 'No valid paths'}), 400

    _dup_scan['running'] = True
    _dup_scan['cancel'] = False
    _dup_scan['scan_id'] = secrets.token_hex(4)
    _dup_scan['phase'] = 'Starting…'
    _dup_scan['scanned'] = 0
    _dup_scan['total'] = 0
    _dup_scan['found_groups'] = 0
    _dup_scan['results'] = []
    _dup_scan['error'] = None

    socketio.start_background_task(_scan_duplicates, scan_paths, mode, threshold)
    return jsonify({'scan_id': _dup_scan['scan_id'], 'status': 'started'})


@files_bp.route('/api/files/duplicates/cancel', methods=['POST'])
@require_auth
def files_duplicates_cancel():
    """Cancel a running scan."""
    if not _dup_scan['running']:
        return jsonify({'error': 'No active scan'}), 400
    _dup_scan['cancel'] = True
    return jsonify({'ok': True, 'message': 'Cancelling scan…'})


@files_bp.route('/api/files/duplicates/status')
@require_auth
def files_duplicates_status():
    """Get current scan progress."""
    return jsonify({
        'running': _dup_scan['running'],
        'phase': _dup_scan['phase'],
        'scanned': _dup_scan['scanned'],
        'total': _dup_scan['total'],
        'found_groups': _dup_scan['found_groups'],
        'error': _dup_scan['error'],
    })


@files_bp.route('/api/files/duplicates/results')
@require_auth
def files_duplicates_results():
    """Get scan results — groups of duplicate images (works during scan too).
    Filters out ignored groups unless ?include_ignored=1."""
    results = _dup_scan.get('results', [])
    include_ignored = request.args.get('include_ignored', '0') == '1'
    if not include_ignored:
        ignored = _load_dup_ignored()
        results = [g for g in results if _dup_group_key(g) not in ignored]
    return jsonify({
        'groups': results,
        'ready': not _dup_scan['running'],
        'total_groups': len(results),
    })


@files_bp.route('/api/files/duplicates/ignore', methods=['POST'])
@require_auth
def files_duplicates_ignore():
    """Add groups to the ignore list."""
    data = request.json or {}
    groups = data.get('groups', [])  # list of group objects with items
    if not groups:
        return jsonify({'error': 'No groups provided'}), 400
    ignored = _load_dup_ignored()
    added = 0
    for g in groups:
        key = _dup_group_key(g)
        if key not in ignored:
            ignored[key] = {
                'key': key,
                'paths': sorted(f['path'] for f in g.get('items', [])),
                'type': g.get('type', 'unknown'),
                'ignored_at': time.time(),
            }
            added += 1
    _save_dup_ignored(ignored)
    return jsonify({'ok': True, 'added': added, 'total_ignored': len(ignored)})


@files_bp.route('/api/files/duplicates/unignore', methods=['POST'])
@require_auth
def files_duplicates_unignore():
    """Remove groups from the ignore list."""
    data = request.json or {}
    keys = data.get('keys', [])  # list of group keys
    if not keys:
        return jsonify({'error': 'No keys provided'}), 400
    ignored = _load_dup_ignored()
    removed = 0
    for k in keys:
        if k in ignored:
            del ignored[k]
            removed += 1
    _save_dup_ignored(ignored)
    return jsonify({'ok': True, 'removed': removed, 'total_ignored': len(ignored)})


@files_bp.route('/api/files/duplicates/ignored')
@require_auth
def files_duplicates_ignored():
    """List all ignored dup groups — returns stored info + matching scan results if available."""
    ignored = _load_dup_ignored()
    # Try to enrich with actual group data from last scan results
    results = _dup_scan.get('results', [])
    result_by_key = {_dup_group_key(g): g for g in results}
    items = []
    for key, info in ignored.items():
        entry = {**info}
        if key in result_by_key:
            entry['group'] = result_by_key[key]
        items.append(entry)
    items.sort(key=lambda x: x.get('ignored_at', 0), reverse=True)
    return jsonify({'items': items, 'total': len(items)})


# -- duplicates package routes (no blueprint, lives in app.py) --
@files_bp.route('/api/files/duplicates/install', methods=['POST'])
@require_auth
def duplicates_pkg_install():
    return jsonify({'ok': True})

@files_bp.route('/api/files/duplicates/uninstall', methods=['POST'])
@require_auth
def duplicates_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    if wipe:
        _dup_scan['results'] = []
        _dup_scan['progress'] = 0
        _hash_cache_path = _data_path('dup_hash_cache.json')
        if os.path.isfile(_hash_cache_path):
            os.remove(_hash_cache_path)
        _ignored_path = _data_path('dup_ignored.json')
        if os.path.isfile(_ignored_path):
            os.remove(_ignored_path)
    return jsonify({'ok': True})

@files_bp.route('/api/files/duplicates/pkg-status')
@require_auth
def duplicates_pkg_status():
    return jsonify({'status': 'ready'})


# -- code-editor package routes (frontend-only, noop backend) --
@files_bp.route('/api/code-editor/install', methods=['POST'])
@require_auth
@admin_required
def code_editor_pkg_install():
    return jsonify({'ok': True})

@files_bp.route('/api/code-editor/uninstall', methods=['POST'])
@require_auth
@admin_required
def code_editor_pkg_uninstall():
    return jsonify({'ok': True})

@files_bp.route('/api/code-editor/pkg-status')
@require_auth
def code_editor_pkg_status():
    return jsonify({'status': 'ready'})


@files_bp.route('/api/files/rename', methods=['POST'])
@require_auth
def files_rename():
    data = request.json or {}
    old = _main().safe_path(data.get('path', ''))
    new_name = data.get('new_name', '')
    if not old or not new_name or '/' in new_name:
        return jsonify({'error': 'Invalid parameters'}), 400

    blocked = _main()._require_folder_access(data.get('path', ''))
    if blocked is not None:
        return blocked

    new_path = os.path.join(os.path.dirname(old), new_name)
    try:
        # Compute user-visible paths for folder password migration
        old_user_path = data.get('path', '').rstrip('/')
        parent = os.path.dirname(old_user_path)
        new_user_path = (parent + '/' + new_name) if parent != '/' else ('/' + new_name)

        os.rename(old, new_path)

        # Migrate folder passwords
        _main()._migrate_folder_passwords(old_user_path, new_user_path)
        _main()._dirsize_cache_invalidate(os.path.dirname(old))
        _main()._listdir_cache_invalidate(data.get('path', ''))

        elog('files', 'info', f'Renamed: {os.path.basename(old)} → {new_name}')
        return jsonify({'ok': True})
    except Exception as e:
        elog('files', 'error', f'Rename error: {str(e)}')
        return jsonify({'error': str(e)}), 500


def _atomic_move(src, target, username=None):
    """Move *src* to *target* atomically.

    For same-filesystem moves, uses os.rename which is atomic.
    For cross-filesystem moves, copies to a temp file first, then atomically
    replaces the target, and finally removes the source.  This ensures that
    a crash between any step leaves data intact (source still exists or target
    is already written).

    *username* is used to chown the destination on cross-device moves.
    It must be captured from the request context before entering the thread pool.
    """
    try:
        os.rename(src, target)
    except OSError as e:
        import errno as _errno
        if e.errno != _errno.EXDEV:
            raise
        # Cross-device move: copy → atomic replace → remove source
        if os.path.isdir(src):
            tmp_target = target + '.ethos_mv_tmp_dir'
            try:
                shutil.copytree(src, tmp_target)
                _main()._chown_recursive(tmp_target, username)
                os.rename(tmp_target, target)
                shutil.rmtree(src)
            except Exception:
                shutil.rmtree(tmp_target, ignore_errors=True)
                raise
        else:
            tmp_target = target + '.ethos_mv_tmp'
            try:
                shutil.copy2(src, tmp_target)
                _main()._chown_to_user(tmp_target, username)
                os.replace(tmp_target, target)
                os.remove(src)
            except Exception:
                try:
                    os.remove(tmp_target)
                except OSError:
                    pass
                raise


@files_bp.route('/api/files/move', methods=['POST'])
@require_auth
def files_move():
    data = request.json or {}
    src = _main().safe_path(data.get('src', ''))
    dest = _main().safe_path(data.get('dest', ''))
    if not src or not dest:
        return jsonify({'error': 'Invalid parameters'}), 400

    blocked = _main()._require_folder_access(data.get('src', ''))
    if blocked is not None:
        return blocked
    blocked = _main()._require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked

    try:
        old_user_path = data.get('src', '').rstrip('/')
        dest_user = data.get('dest', '').rstrip('/')
        new_user_path = dest_user + '/' + os.path.basename(old_user_path)

        target = os.path.join(dest, os.path.basename(src))
        # Capture username in request context before entering thread pool
        _cur_user = get_current_user()
        _username = _cur_user['username'] if _cur_user else None
        # Run in thread pool to avoid blocking gevent on cross-device moves
        _fs_call(_main()._atomic_move, src, target, _username, timeout=300)

        # Migrate folder passwords for moved folder
        _main()._migrate_folder_passwords(old_user_path, new_user_path)
        _main()._dirsize_cache_invalidate(os.path.dirname(src))
        _main()._dirsize_cache_invalidate(dest)
        _main()._listdir_cache_invalidate(data.get('src', ''))
        _main()._listdir_cache_invalidate(data.get('dest', ''))

        elog('files', 'info', f'Moved: {os.path.basename(src)} → {data.get("dest", "")}')
        return jsonify({'ok': True})
    except Exception as e:
        elog('files', 'error', f'Move error: {str(e)}')
        return jsonify({'error': str(e)}), 500


@files_bp.route('/api/files/check-conflicts', methods=['POST'])
@require_auth
def files_check_conflicts():
    """Check which sources already exist in dest."""
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = _main().safe_path(data.get('dest', ''))
    if not sources or not dest_dir:
        return jsonify({'conflicts': []})
    conflicts = []
    for src_path in sources:
        real_src = _main().safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            continue
        base_name = os.path.basename(real_src)
        target = os.path.join(dest_dir, base_name)
        if os.path.exists(target):
            conflicts.append({
                'name': base_name,
                'source': src_path,
                'is_dir': os.path.isdir(real_src),
                'target_is_dir': os.path.isdir(target),
            })
    return jsonify({'conflicts': conflicts})


def _resolve_target(real_src, dest_dir, on_conflict):
    """Resolve target path based on conflict strategy. Returns target path or None to skip."""
    base_name = os.path.basename(real_src)
    target = os.path.join(dest_dir, base_name)
    if os.path.exists(target):
        if on_conflict == 'overwrite':
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            return target
        elif on_conflict == 'skip':
            return None
        else:  # rename (default)
            name, ext = os.path.splitext(base_name)
            counter = 1
            while os.path.exists(target):
                target = os.path.join(dest_dir, f"{name}_kopia{counter}{ext}")
                counter += 1
            return target
    return target


@files_bp.route('/api/files/copy', methods=['POST'])
@require_auth
def files_copy():
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = _main().safe_path(data.get('dest', ''))
    on_conflict = data.get('on_conflict', 'rename')  # overwrite | skip | rename
    if not sources or not dest_dir:
        return jsonify({'error': 'Invalid parameters'}), 400
    if not os.path.isdir(dest_dir):
        return jsonify({'error': 'Destination is not a folder'}), 400

    # Security check: check destination and all sources
    blocked = _main()._require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked
    for s in sources:
        blocked = _main()._require_folder_access(s)
        if blocked is not None:
            return blocked

    # Resolve sources first
    resolved = []
    for src_path in sources:
        real_src = _main().safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            continue
        resolved.append(real_src)

    total = _main()._count_items(resolved)
    use_bg = total > 2  # background task for >2 items

    if use_bg:
        _fm = _main()._fileop_channels['fm']
        with _main()._fileop_lock:
            if _fm['active']:
                return jsonify({'error': 'Another file operation is in progress'}), 400
            _fm['active'] = True
            _fm['operation'] = 'copy'
            _fm['progress'] = None
            _fm['cancel'] = False
            _fm['paused'] = False
        cur_user = get_current_user()
        _main()._save_copy_task(resolved, dest_dir, total, on_conflict, (cur_user or {}).get('username'))
        socketio.start_background_task(_bg_copy, resolved, dest_dir, total, on_conflict, cur_user)
        return jsonify({'async': True, 'message': f'Copying {len(resolved)} items ({total} files) in background'})

    # Small operation — synchronous but run I/O in thread pool
    # to avoid blocking gevent on slow disks (USB, cross-device)
    copied = []
    skipped = []
    errors = []
    for src_path in sources:
        real_src = _main().safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            errors.append(f'Not found: {src_path}')
            continue
        base_name = os.path.basename(real_src)
        target = _resolve_target(real_src, dest_dir, on_conflict)
        if target is None:
            skipped.append(base_name)
            continue
        try:
            if os.path.isdir(real_src):
                tmp_target = target + '.ethos_tmp_dir'
                def _do_copy_dir(_src=real_src, _tmp=tmp_target, _tgt=target):
                    try:
                        shutil.copytree(_src, _tmp)
                        os.rename(_tmp, _tgt)
                    except Exception:
                        shutil.rmtree(_tmp, ignore_errors=True)
                        raise
                _fs_call(_do_copy_dir, timeout=300)
                _main()._chown_recursive(target)
            else:
                tmp_target = target + '.ethos_tmp'
                def _do_copy_file(_src=real_src, _tmp=tmp_target, _tgt=target):
                    shutil.copy2(_src, _tmp)
                    os.replace(_tmp, _tgt)
                _fs_call(_do_copy_file, timeout=300)
                _main()._chown_to_user(target)
            copied.append(base_name)
        except Exception as e:
            errors.append(f'{base_name}: {str(e)}')

    _main()._listdir_cache_invalidate(data.get('dest', ''))
    _main()._dirsize_cache_invalidate(dest_dir)

    if copied:
        cur = get_current_user()
        username = cur['username'] if cur else 'unknown'
        elog('files', 'info', f'Copied {len(copied)} files to {data.get("dest", "")}', {'user': username, 'files': copied})

    return jsonify({'copied': copied, 'skipped': skipped, 'errors': errors})


def _bg_copy(resolved_sources, dest_dir, total, on_conflict='rename', cur_user=None):
    """Background copy with progress, cancel/pause support, and cleanup on abort."""
    _bg_username = cur_user['username'] if cur_user else None
    copied = []
    skipped = []
    errors = []
    done_ref = [0]
    partial_files = []  # tracks .ethos_tmp files written so far (for cleanup on cancel)
    cancel_ref = [False]
    _fm = _main()._fileop_channels['fm']

    def _check_cancel():
        with _main()._fileop_lock:
            cancel_ref[0] = _fm.get('cancel', False)
        return cancel_ref[0]

    def _is_paused():
        with _main()._fileop_lock:
            return _fm.get('paused', False)

    _main()._fileop_progress('copy', '', 0, total, 'fm')
    cancelled = False
    try:
        for real_src in resolved_sources:
            if _check_cancel():
                cancelled = True
                break
            if not real_src or not os.path.exists(real_src):
                errors.append(f'Not found: {real_src}')
                continue
            base_name = os.path.basename(real_src)
            target = _resolve_target(real_src, dest_dir, on_conflict)
            if target is None:
                skipped.append(base_name)
                done_ref[0] += _main()._count_items([real_src])
                _main()._fileop_progress('copy', f'{base_name} (skipped)', done_ref[0], total, 'fm')
                gevent.sleep(0)
                continue
            try:
                _main()._copy_with_progress(real_src, target, 'copy', done_ref, total, username=_bg_username,
                                    cancel_ref=cancel_ref, pause_fn=_is_paused, partial_files=partial_files)
                copied.append(base_name)
            except InterruptedError:
                cancelled = True
                break
            except Exception as e:
                errors.append(f'{base_name}: {str(e)}')

        if cancelled:
            # Clean up partial .ethos_tmp files left behind
            for pf in partial_files:
                try: os.remove(pf)
                except OSError: pass
            _main()._clear_copy_task()
            _main()._fileop_finish('copy', False, 'Cancelled', 'fm')
            return

        _main()._clear_copy_task()
        msg = f'Copied {len(copied)} items'
        if skipped:
            msg += f', skipped {len(skipped)}'
        if errors:
            msg += f' ({len(errors)} errors)'
        _main()._fileop_finish('copy', len(copied) > 0 or len(skipped) > 0, msg, 'fm')
    except Exception as e:
        # Clean up partial files on unexpected error
        for pf in partial_files:
            try: os.remove(pf)
            except OSError: pass
        _main()._clear_copy_task()
        _main()._fileop_finish('copy', False, str(e), 'fm')


@files_bp.route('/api/files/move-multi', methods=['POST'])
@require_auth
def files_move_multi():
    data = request.json or {}
    sources = data.get('sources', [])
    dest_dir = _main().safe_path(data.get('dest', ''))
    if not sources or not dest_dir:
        return jsonify({'error': 'Invalid parameters'}), 400
    if not os.path.isdir(dest_dir):
        return jsonify({'error': 'Destination is not a folder'}), 400

    # Security check: check destination and all sources
    blocked = _main()._require_folder_access(data.get('dest', ''))
    if blocked is not None:
        return blocked
    for s in sources:
        blocked = _main()._require_folder_access(s)
        if blocked is not None:
            return blocked

    # Resolve sources
    resolved = []
    for src_path in sources:
        real_src = _main().safe_path(src_path)
        if real_src and os.path.exists(real_src):
            resolved.append(real_src)

    total = _main()._count_items(resolved)
    use_bg = total > 2  # background task for >2 items

    on_conflict = data.get('on_conflict', 'rename')  # overwrite | skip | rename

    if use_bg:
        _fm = _main()._fileop_channels['fm']
        with _main()._fileop_lock:
            if _fm['active']:
                return jsonify({'error': 'Another file operation is in progress'}), 400
            _fm['active'] = True
            _fm['operation'] = 'move'
            _fm['progress'] = None
            _fm['cancel'] = False
            _fm['paused'] = False
        cur_user = get_current_user()
        _main()._save_move_task(resolved, dest_dir, total, on_conflict, data.get('dest', ''), (cur_user or {}).get('username'))
        socketio.start_background_task(_bg_move, resolved, dest_dir, total, on_conflict, data.get('dest', ''), cur_user)
        return jsonify({'async': True, 'message': f'Moving {len(resolved)} items in background'})

    # Small — synchronous
    moved = []
    skipped = []
    errors = []
    _cur_user = get_current_user()
    _username = (_cur_user or {}).get('username')
    for src_path in sources:
        real_src = _main().safe_path(src_path)
        if not real_src or not os.path.exists(real_src):
            errors.append(f'Not found: {src_path}')
            continue
        base_name = os.path.basename(real_src)
        target = _resolve_target(real_src, dest_dir, on_conflict)
        if target is None:
            skipped.append(base_name)
            continue
        try:
            # Run in thread pool to avoid blocking gevent on cross-device moves
            _fs_call(_main()._atomic_move, real_src, target, _username, timeout=300)
            _main()._chown_recursive(target, _username)
            moved.append(base_name)
            # Migrate folder passwords
            dest_user = data.get('dest', '').rstrip('/')
            _main()._migrate_folder_passwords(src_path.rstrip('/'), dest_user + '/' + base_name)
        except Exception as e:
            errors.append(f'{base_name}: {str(e)}')

    _main()._listdir_cache_invalidate(data.get('dest', ''))
    _main()._dirsize_cache_invalidate(dest_dir)
    for src_path in sources:
        _main()._listdir_cache_invalidate(src_path)
        _main()._dirsize_cache_invalidate(os.path.dirname(_main().safe_path(src_path) or ''))

    if moved:
        cur = get_current_user()
        username = cur['username'] if cur else 'unknown'
        elog('files', 'info', f'Moved {len(moved)} files to {data.get("dest", "")}', {'user': username, 'files': moved})

    return jsonify({'moved': moved, 'skipped': skipped, 'errors': errors})


def _bg_move(resolved_sources, dest_dir, total, on_conflict='rename', dest_user_path='', cur_user=None):
    """Background move with progress, cancel/pause support."""
    _bg_username = cur_user['username'] if cur_user else None
    moved = []
    skipped = []
    errors = []
    done = 0
    dest_user = dest_user_path.rstrip('/')
    cancel_ref = [False]
    _fm = _main()._fileop_channels['fm']

    def _check_cancel():
        with _main()._fileop_lock:
            cancel_ref[0] = _fm.get('cancel', False)
        return cancel_ref[0]

    def _is_paused():
        with _main()._fileop_lock:
            return _fm.get('paused', False)

    def _wait_if_paused():
        while _is_paused():
            if _check_cancel():
                return True
            gevent.sleep(0.2)
        return _check_cancel()

    _main()._fileop_progress('move', '', 0, total, 'fm')
    cancelled = False
    try:
        for real_src in resolved_sources:
            if _check_cancel():
                cancelled = True
                break
            # Honour pause between items
            if _wait_if_paused():
                cancelled = True
                break
            if not real_src or not os.path.exists(real_src):
                errors.append(f'Not found: {real_src}')
                continue
            base_name = os.path.basename(real_src)
            item_count = _main()._count_items([real_src])
            target = _resolve_target(real_src, dest_dir, on_conflict)
            if target is None:
                skipped.append(base_name)
                done += item_count
                _main()._fileop_progress('move', f'{base_name} (skipped)', done, total, 'fm')
                gevent.sleep(0)
                continue
            try:
                # Run in thread pool to avoid blocking gevent on cross-device moves
                _fs_call(_main()._atomic_move, real_src, target, _bg_username, timeout=600)
                _main()._chown_recursive(target, _bg_username)
                done += item_count
                moved.append(base_name)
                # Migrate folder passwords
                if dest_user:
                    _main()._migrate_folder_passwords(real_src.rstrip('/'), dest_user + '/' + base_name)
                _main()._fileop_progress('move', base_name, done, total, 'fm')
                gevent.sleep(0)
            except Exception as e:
                errors.append(f'{base_name}: {str(e)}')

        if cancelled:
            _main()._clear_move_task()
            _main()._fileop_finish('move', False, 'Cancelled', 'fm')
            return

        _main()._clear_move_task()
        msg = f'Moved {len(moved)} items'
        if skipped:
            msg += f', skipped {len(skipped)}'
        if errors:
            msg += f' ({len(errors)} errors)'
        _main()._fileop_finish('move', len(moved) > 0 or len(skipped) > 0, msg, 'fm')
    except Exception as e:
        _main()._clear_move_task()
        _main()._fileop_finish('move', False, str(e), 'fm')


