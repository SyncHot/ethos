"""
EthOS — Downloads: Core download worker, concurrent download manager,
and deep archive extraction.
All module-level state is accessed via _dl() to avoid gevent import-lock issues.
Functions from downloads_torrent.py are accessed via _dl_torrent().
"""

import sys
import os
import re
import time
import json
import ssl
import urllib.request
import urllib.parse
import shutil
import subprocess
import collections
import threading
import logging
import gevent
import gevent.threadpool


def _dl():
    """Return the blueprints.downloads module without importing it directly."""
    return sys.modules.get('blueprints.downloads')


def _dl_torrent():
    """Return blueprints.downloads_torrent without importing it directly."""
    return sys.modules.get('blueprints.downloads_torrent')


def _get_proxy_handler(config):
    """Build urllib proxy handler if proxy is enabled in config."""
    if not config.get('proxy_enabled'):
        return None
    
    proxy_type = config.get('proxy_type', 'http')
    host = config.get('proxy_host', '').strip()
    port = config.get('proxy_port', '').strip()
    
    if not host or not port:
        return None
    
    username = config.get('proxy_username', '').strip()
    password = config.get('proxy_password', '').strip()
    
    # Build proxy URL
    if username and password:
        proxy_url = f"{proxy_type}://{username}:{password}@{host}:{port}"
    else:
        proxy_url = f"{proxy_type}://{host}:{port}"
    
    # SOCKS5 requires PySocks library (optional)
    if proxy_type == 'socks5':
        try:
            import socks
            import socket
            # Configure socket globally for SOCKS5
            socks.set_default_proxy(socks.SOCKS5, host, int(port), username=username or None, password=password or None)
            socket.socket = socks.socksocket
            return None  # socket is already patched globally
        except ImportError:
            # PySocks not installed, fall back to no proxy
            return None
    
    # HTTP/HTTPS proxy
    proxies = {
        'http': proxy_url,
        'https': proxy_url,
    }
    return urllib.request.ProxyHandler(proxies)


def _check_range_support(url, config):
    """Check if URL supports HTTP Range requests. Returns (supports_range, filesize)."""
    try:
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('User-Agent', 'EthOS/1.0')
        ctx = ssl.create_default_context()
        
        proxy_handler = _get_proxy_handler(config)
        if proxy_handler:
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ctx))
            response = opener.open(req, timeout=10)
        else:
            response = urllib.request.urlopen(req, context=ctx, timeout=10)
        
        accept_ranges = response.headers.get('Accept-Ranges', '').lower()
        content_length = int(response.headers.get('Content-Length', 0))
        
        supports = accept_ranges == 'bytes'
        return supports, content_length
    except Exception:
        return False, 0


def _download_segment(url, start_byte, end_byte, segment_path, dl, config, segment_idx):
    """Download a single segment of a file. Returns True on success."""
    try:
        headers = {
            'User-Agent': 'EthOS/1.0',
            'Range': f'bytes={start_byte}-{end_byte}'
        }
        
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl.create_default_context()
        
        proxy_handler = _get_proxy_handler(config)
        if proxy_handler:
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ctx))
            response = opener.open(req, timeout=60)
        else:
            response = urllib.request.urlopen(req, context=ctx, timeout=60)
        
        chunk_size = 256 * 1024
        downloaded = 0
        segment_size = end_byte - start_byte + 1
        
        with open(segment_path, 'wb') as f:
            while True:
                if dl.get('status') in ('cancelled', 'paused'):
                    return False
                
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                
                _dl()._io_pool.apply(f.write, (chunk,))
                downloaded += len(chunk)
                
                # Update segment progress
                with _dl()._lock:
                    if 'segments' not in dl:
                        dl['segments'] = {}
                    dl['segments'][segment_idx] = {
                        'downloaded': downloaded,
                        'total': segment_size,
                        'progress': round(downloaded / segment_size * 100, 1) if segment_size > 0 else 0
                    }
        
        return downloaded == segment_size
    except Exception as e:
        return False


def _merge_segments(segment_paths, final_path, dl):
    """Merge all segments into final file. Returns True on success."""
    try:
        with open(final_path, 'wb') as outfile:
            for segment_path in segment_paths:
                if dl.get('status') == 'cancelled':
                    return False
                
                with open(segment_path, 'rb') as infile:
                    shutil.copyfileobj(infile, outfile, 1024 * 1024)  # 1MB buffer
                
                # Remove segment file after merging
                try:
                    os.remove(segment_path)
                except OSError:
                    pass
        
        return True
    except Exception as e:
        return False


def _download_multi_segment(dl, download_url, filename, filesize, dest_dir, config):
    """Download file in multiple parallel segments. Returns True on success."""
    segment_count = int(config.get('multi_segment_count', 4))
    segment_count = max(2, min(8, segment_count))  # Clamp to 2-8
    
    dest_path = os.path.join(dest_dir, filename)
    if not config.get('overwrite_existing', False):
        base, ext = os.path.splitext(dest_path)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = f"{base}_{counter}{ext}"
            counter += 1
    elif os.path.exists(dest_path):
        try:
            os.remove(dest_path)
        except OSError:
            pass
    
    filename = os.path.basename(dest_path)
    dl['filename'] = filename
    dl['filesize'] = filesize
    dl['dest_path'] = dest_path
    dl['multi_segment'] = True
    dl['segments'] = {}
    
    # Calculate segment sizes
    segment_size = filesize // segment_count
    segments = []
    for i in range(segment_count):
        start = i * segment_size
        end = ((i + 1) * segment_size - 1) if i < segment_count - 1 else (filesize - 1)
        segment_path = f"{dest_path}.part{i}"
        segments.append((start, end, segment_path, i))
    
    # Download all segments in parallel using thread pool
    def download_segment_wrapper(args):
        start, end, seg_path, idx = args
        return _download_segment(download_url, start, end, seg_path, dl, config, idx)
    
    # Use ThreadPool to download segments in parallel
    pool = threading.Thread
    results = []
    threads = []
    
    for seg_args in segments:
        t = threading.Thread(target=lambda a=seg_args: results.append(download_segment_wrapper(a)))
        t.start()
        threads.append(t)
    
    # Wait for all threads while monitoring progress
    while any(t.is_alive() for t in threads):
        time.sleep(0.5)
        
        # Calculate total progress from all segments
        with _dl()._lock:
            if 'segments' in dl:
                total_downloaded = sum(s.get('downloaded', 0) for s in dl['segments'].values())
                dl['downloaded'] = total_downloaded
                dl['progress'] = round(total_downloaded / filesize * 100, 1) if filesize > 0 else 0
                dl['speed'] = _calc_speed(dl)
                _dl()._emit('dl:update', _sanitize(dl))
        
        if dl.get('status') in ('cancelled', 'paused'):
            for t in threads:
                t.join(timeout=1)
            # Cleanup partial segments
            for _, _, seg_path, _ in segments:
                try:
                    os.remove(seg_path)
                except OSError:
                    pass
            return False
    
    # Join all threads
    for t in threads:
        t.join()
    
    # Check if cancelled/paused
    if dl.get('status') in ('cancelled', 'paused'):
        for _, _, seg_path, _ in segments:
            try:
                os.remove(seg_path)
            except OSError:
                pass
        return False
    
    # Check if all segments downloaded successfully
    if not all(results):
        raise Exception("One or more segments failed to download")
    
    # Merge segments
    dl['status'] = 'merging'
    _dl()._emit('dl:update', _sanitize(dl))
    
    segment_paths = [seg_path for _, _, seg_path, _ in segments]
    if not _merge_segments(segment_paths, dest_path, dl):
        raise Exception("Failed to merge segments")
    
    dl.pop('segments', None)
    dl.pop('multi_segment', None)
    return True



# ─── Download worker ───

def _download_single_url(dl, download_url, filename, filesize, dest_dir, config):
    """Download a single file URL to dest_dir. Returns True on success.
    Supports HTTP Range resume from partial files and multi-segment downloads.
    """
    # Check if multi-segment download is possible and enabled
    multi_segment_enabled = config.get('multi_segment_enabled', True)
    min_size_mb = int(config.get('multi_segment_min_size', 10))
    min_size_bytes = min_size_mb * 1024 * 1024
    
    # Only use multi-segment for fresh downloads (not resumes)
    resume_offset = 0
    resume_path = dl.get('_actual_dest')
    is_resume = resume_path and os.path.isfile(resume_path)
    
    # Try multi-segment if enabled and file is large enough
    if (multi_segment_enabled and not is_resume and filesize >= min_size_bytes):
        supports_range, detected_size = _check_range_support(download_url, config)
        if supports_range:
            # Use detected size if filesize is unknown
            if not filesize and detected_size:
                filesize = detected_size
            
            if filesize >= min_size_bytes:
                try:
                    return _download_multi_segment(dl, download_url, filename, filesize, dest_dir, config)
                except Exception as e:
                    # Fall back to single-threaded download on multi-segment failure
                    dl.pop('segments', None)
                    dl.pop('multi_segment', None)
                    # Continue to single-threaded download below
    
    # Single-threaded download (classic mode or fallback)

    if resume_path and os.path.isfile(resume_path):
        # Resume from existing partial file
        resume_offset = os.path.getsize(resume_path)
        dest_path = resume_path
        filename = os.path.basename(dest_path)
    else:
        # New download — handle overwrite/rename
        dest_path = os.path.join(dest_dir, filename)
        if not config.get('overwrite_existing', False):
            base, ext = os.path.splitext(dest_path)
            counter = 1
            while os.path.exists(dest_path):
                dest_path = f"{base}_{counter}{ext}"
                counter += 1
        elif os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
    filename = os.path.basename(dest_path)

    dl['filename'] = filename
    dl['filesize'] = filesize
    dl['dest_path'] = dest_path  # actual filesystem path
    dl['_actual_dest'] = dest_path  # save for resume

    # Disk space pre-check (skip for resume — already partly on disk)
    if filesize and resume_offset == 0:
        try:
            st = os.statvfs(dest_dir)
            free_bytes = st.f_bavail * st.f_frsize
            # Need at least filesize + 100MB buffer
            if free_bytes < filesize + 100 * 1024 * 1024:
                free_gb = free_bytes / (1024 ** 3)
                need_gb = filesize / (1024 ** 3)
                raise Exception(
                    f"Not enough disk space: {free_gb:.1f} GB free, {need_gb:.1f} GB needed"
                )
        except OSError:
            pass  # can't check — proceed anyway

    try:
        headers = {'User-Agent': 'EthOS/1.0'}
        if resume_offset > 0:
            headers['Range'] = f'bytes={resume_offset}-'

        req = urllib.request.Request(download_url, headers=headers)
        ctx = ssl.create_default_context()
        
        # Proxy support
        proxy_handler = _get_proxy_handler(config)
        if proxy_handler:
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ctx))
            response = opener.open(req, timeout=60)
        else:
            response = urllib.request.urlopen(req, context=ctx, timeout=60)
        
        with response as resp:
            status_code = getattr(resp, 'status', 200)
            content_length = int(resp.headers.get('content-length', 0))

            if resume_offset > 0 and status_code == 206:
                # Server supports Range — append to existing file
                total = resume_offset + content_length
                downloaded = resume_offset
                file_mode = 'ab'
            else:
                # No Range support or fresh download — start from scratch
                total = content_length or filesize
                downloaded = 0
                resume_offset = 0
                file_mode = 'wb'

            if total:
                dl['filesize'] = total

            chunk_size = 256 * 1024
            last_emit = 0
            # Speed limit (KB/s -> bytes/s), 0 = unlimited
            speed_limit = int(config.get('speed_limit', 0)) * 1024
            throttle_window = 0.25  # measure every 250ms
            window_bytes = 0
            window_start = time.time()

            with open(dest_path, file_mode) as f:
                while True:
                    if dl.get('status') == 'cancelled':
                        break
                    if dl.get('status') == 'paused':
                        # Break immediately — keep partial file for Range resume
                        break
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    # Write in a real OS thread so slow HDD I/O
                    # doesn't block gevent's event loop.
                    _dl()._io_pool.apply(f.write, (chunk,))
                    downloaded += len(chunk)
                    window_bytes += len(chunk)
                    dl['downloaded'] = downloaded
                    if total > 0:
                        dl['progress'] = round(downloaded / total * 100, 1)
                    else:
                        # Unknown total — use -1 to signal indeterminate progress
                        dl['progress'] = -1
                    dl['speed'] = _calc_speed(dl)
                    now = time.time()
                    if now - last_emit >= 0.5:
                        last_emit = now
                        _dl()._emit('dl:update', _sanitize(dl))
                    # Throttle if speed limit is set
                    if speed_limit > 0:
                        elapsed = now - window_start
                        if elapsed < throttle_window:
                            max_bytes = speed_limit * elapsed
                            if window_bytes >= max_bytes:
                                sleep_time = (window_bytes / speed_limit) - elapsed
                                if sleep_time > 0:
                                    time.sleep(sleep_time)
                        else:
                            window_bytes = 0
                            window_start = time.time()

        if dl.get('status') == 'cancelled':
            try:
                os.remove(dest_path)
            except OSError:
                pass
            dl.pop('_actual_dest', None)
            return False
        if dl.get('status') == 'paused':
            # Keep partial file, save progress for resume
            dl['downloaded'] = downloaded
            dl['_actual_dest'] = dest_path
            _dl()._save_state()
            return False
        dl['downloaded'] = os.path.getsize(dest_path) if os.path.exists(dest_path) else downloaded
        dl.pop('_actual_dest', None)  # cleanup — download complete
        return True
    except Exception as e:
        # Keep partial file for resume (store path)
        dl['_actual_dest'] = dest_path
        raise


def _wait_for_slot(dl):
    """Wait for a concurrency slot (limit from global config)."""
    while True:
        # Check if cancelled while waiting
        with _dl()._lock:
            if dl.get('status') in ('cancelled', 'failed', 'paused'):
                return False

            # Check global limit (system-wide)
            # We load global config to ensure we respect the AppStore setting
            config = _dl()._load_config(username=None)
            max_conc = config.get('max_concurrent', 3)

            # Count currently active downloads (excluding this one if it was already active,
            # but it shouldn't be as we are in 'torrent_downloading' or similar)
            active = sum(1 for d in _dl()._downloads.values()
                         if d['status'] in ('downloading', 'resolving'))

            if active < max_conc:
                # Slot available!
                return True

        # Wait before retrying
        time.sleep(2)


def _download_worker(dl_id):
    """Background thread that downloads a single file (or torrent)."""
    with _dl()._lock:
        dl = _dl()._downloads.get(dl_id)
        if not dl:
            return

    # Load per-user config for the download owner
    dl_owner = dl.get('user', '')
    config = _dl()._load_config(username=dl_owner or None)
    original_url = dl['url']
    is_torrent_dl = dl.get('is_torrent', False) or _dl()._is_torrent(dl.get('url', ''))
    default_key = 'default_dir_torrent' if is_torrent_dl else 'default_dir'
    dest_dir = _dl()._safe_path(dl.get('dest_dir') or config.get(default_key, '/home'))

    if not dest_dir or not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)

    is_torrent = dl.get('is_torrent', False) or _dl()._is_torrent(original_url)

    # For torrents: don't wait for slot now - wait only before local download
    # For direct downloads: wait for slot now
    if not is_torrent:
        if not _wait_for_slot(dl):
            return

    with _dl()._lock:
        dl['status'] = 'resolving'
        if is_torrent:
            dl['is_torrent'] = True
        _dl()._emit('dl:update', _sanitize(dl))

    # ─── Torrent/Magnet flow ───
    if is_torrent:
        try:
            # Get torrent file data if it was uploaded
            torrent_file_data = None
            torrent_cache = dl.get('torrent_cache_path')
            if torrent_cache and os.path.exists(torrent_cache):
                with open(torrent_cache, 'rb') as f:
                    torrent_file_data = f.read()

            # Submit to debrid
            with _dl()._lock:
                dl['status'] = 'torrent_uploading'
                _dl()._emit('dl:update', _sanitize(dl))

            torrent_info = _dl_torrent()._add_torrent_to_debrid(original_url, config, torrent_file_data)

            with _dl()._lock:
                dl['status'] = 'torrent_downloading'
                dl['torrent_id'] = torrent_info.get('torrent_id', '')
                dl['started_at'] = time.time()
                _dl()._save_state()
                _dl()._emit('dl:update', _sanitize(dl))

            # Poll until debrid has downloaded the torrent
            file_links = _dl_torrent()._poll_torrent(torrent_info, dl)

            if dl.get('status') == 'cancelled':
                _dl()._emit('dl:update', _sanitize(dl))
                return

            if not file_links:
                raise Exception("No files to download from torrent")

            # Try to auto-categorize torrent if using default path
            if config.get('auto_categorize', True):
                current_dest = dl.get('dest_dir')
                default_torrent = config.get('default_dir_torrent')
                # If current dest matches default, try to categorize
                if current_dest and default_torrent and os.path.normpath(current_dest) == os.path.normpath(default_torrent):
                    largest = max(file_links, key=lambda x: x.get('filesize', 0))
                    cat_id, cat_path = _get_category_for_file(largest.get('filename', ''), config)
                    if cat_id:
                        dest_dir = cat_path
                        with _dl()._lock:
                            dl['category_id'] = cat_id
                            dl['dest_dir'] = dest_dir
                            _dl()._save_state()
                        if not os.path.exists(dest_dir):
                            os.makedirs(dest_dir, exist_ok=True)

            # Download all resulting files
            # Wait for a slot before starting local download to respect global limit
            if not _wait_for_slot(dl):
                return

            with _dl()._lock:
                dl['status'] = 'downloading'
                dl['progress'] = 0
                dl['downloaded'] = 0
                dl['torrent_files_total'] = len(file_links)
                dl['torrent_files_done'] = 0
                _dl()._emit('dl:update', _sanitize(dl))

            # Create a subfolder for multi-file torrents
            if len(file_links) > 1:
                # Use magnet name or first file as folder name
                folder_name = dl.get('filename') or 'torrent_' + dl_id
                folder_name = re.sub(r'[<>:"/\\|?*]', '_', folder_name)[:100]
                torrent_dest = os.path.join(dest_dir, folder_name)
                os.makedirs(torrent_dest, exist_ok=True)
            else:
                torrent_dest = dest_dir

            total_size = sum(link.get('filesize', 0) for link in file_links)
            total_downloaded = 0

            for i, link in enumerate(file_links):
                if dl.get('status') in ('cancelled', 'paused'):
                    break

                link_url = link.get('url', '')
                link_filename = link.get('filename') or _dl_torrent()._guess_filename(link_url)
                link_filesize = link.get('filesize', 0)

                dl['torrent_files_done'] = i
                dl['filename'] = link_filename

                success = _download_single_url(dl, link_url, link_filename, link_filesize, torrent_dest, config)
                if not success:
                    break  # paused or cancelled
                total_downloaded += dl.get('downloaded', 0)

                dl['torrent_files_done'] = i + 1
                _dl()._emit('dl:update', _sanitize(dl))

            if dl.get('status') == 'cancelled':
                _dl()._emit('dl:update', _sanitize(dl))
                return

            if dl.get('status') == 'paused':
                _dl()._emit('dl:update', _sanitize(dl))
                return

            with _dl()._lock:
                dl['status'] = 'completed'
                dl['progress'] = 100
                dl['completed_at'] = time.time()
                dl['downloaded'] = total_downloaded
                dl['filesize'] = total_size or total_downloaded
                if len(file_links) > 1:
                    dl['filename'] = folder_name
                    dl['dest_path'] = torrent_dest
                _dl()._save_state()
            _dl()._emit('dl:update', _sanitize(dl))
            _dl()._emit('dl:completed', _dl_completed_payload(dl))
            _dl()._log_history(dl, 'completed')
            _dl()._flush_state()

            # Cleanup torrent cache
            if torrent_cache and os.path.exists(torrent_cache):
                try:
                    os.remove(torrent_cache)
                except OSError:
                    pass

            # Move watch torrent to processed/
            _dl()._move_torrent_on_finish(dl, True)

            return

        except Exception as e:
            with _dl()._lock:
                dl['status'] = 'failed'
                dl['error'] = str(e)[:500]
                _dl()._save_state()
            _dl()._emit('dl:update', _sanitize(dl))
            _dl()._log_history(dl, 'failed')
            _dl()._flush_state()
            # Move watch torrent to error/
            _dl()._move_torrent_on_finish(dl, False)
            return

    # ─── Regular download flow ───
    resolved = None
    debrid_was_requested = dl.get('use_debrid', True) and config.get('debrid_service', 'none') != 'none'
    if debrid_was_requested:
        try:
            resolved = _dl()._resolve_debrid(original_url, config)
        except Exception as e:
            dl['debrid_error'] = str(e)

    # If debrid was requested but failed and the URL doesn't look like a direct
    # download, fail immediately instead of downloading an HTML error page.
    if debrid_was_requested and not resolved:
        if not _dl()._looks_like_direct_url(original_url):
            with _dl()._lock:
                dl['status'] = 'failed'
                dl['error'] = f"Debrid resolution failed: {dl.get('debrid_error', 'unknown')}. " \
                              "Link is not a direct download URL."
                _dl()._save_state()
            _dl()._emit('dl:update', _sanitize(dl))
            _dl()._log_history(dl, 'failed')
            _dl()._flush_state()
            return

    download_url = resolved['url'] if resolved else original_url
    filename = dl.get('filename') or (resolved or {}).get('filename') or _dl_torrent()._guess_filename(download_url)
    filesize = (resolved or {}).get('filesize', 0)

    # Deduplicate: skip if same resolved URL or filename already downloaded in same package
    pkg_id = dl.get('package_id')
    if pkg_id and resolved:
        with _dl()._lock:
            for other in _dl()._downloads.values():
                if other.get('id') == dl_id or other.get('package_id') != pkg_id:
                    continue
                if other.get('status') != 'completed':
                    continue
                # Same debrid-resolved filename + similar size → duplicate
                other_fn = other.get('filename', '')
                other_sz = other.get('filesize', 0)
                if other_fn and other_fn == filename and (
                    not filesize or not other_sz or abs(filesize - other_sz) < 1024
                ):
                    dl['status'] = 'completed'
                    dl['progress'] = 100
                    dl['completed_at'] = time.time()
                    dl['filename'] = filename
                    dl['dest_path'] = other.get('dest_path', '')
                    dl['error'] = ''
                    dl['_dedup_of'] = other.get('id', '')
                    _dl()._save_state()
            if dl.get('_dedup_of'):
                logging.info('[downloads] Skipping duplicate %s (same as %s): %s',
                             dl_id, dl['_dedup_of'], filename)
                _dl()._emit('dl:update', _sanitize(dl))
                _dl()._log_history(dl, 'completed')
                _dl()._flush_state()
                return

    # Auto-categorize
    if config.get('auto_categorize', True):
        current_dest = dl.get('dest_dir')
        default_dir = config.get('default_dir')
        if current_dest and default_dir and os.path.normpath(current_dest) == os.path.normpath(default_dir):
            cat_id, cat_path = _get_category_for_file(filename, config)
            if cat_id:
                dest_dir = cat_path
                with _dl()._lock:
                    dl['category_id'] = cat_id
                    dl['dest_dir'] = dest_dir
                    _dl()._save_state()
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir, exist_ok=True)

    retries = dl.get('retry_count', 0)
    max_retries = _dl().MAX_RETRIES

    for attempt in range(max_retries + 1):
        with _dl()._lock:
            dl['status'] = 'downloading'
            dl['started_at'] = time.time()
            if attempt > 0:
                dl['retry_count'] = attempt
            _dl()._emit('dl:update', _sanitize(dl))

        try:
            success = _download_single_url(dl, download_url, filename, filesize, dest_dir, config)

            if dl.get('status') == 'cancelled':
                _dl()._emit('dl:update', _sanitize(dl))
                return
            elif dl.get('status') == 'paused':
                _dl()._emit('dl:update', _sanitize(dl))
                return
            elif success:
                # Validate downloaded file — detect bogus HTML error pages
                actual_size = dl.get('downloaded', 0)
                expected_size = filesize
                is_bogus = False
                if expected_size > 100_000 and actual_size < 50_000:
                    # Expected large file but got tiny → likely HTML error page
                    is_bogus = True
                elif actual_size < 1000 and not dl.get('is_torrent'):
                    # Sub-1KB "file" for a non-torrent download is suspicious
                    is_bogus = True

                if is_bogus:
                    # Remove the bogus file
                    dest = dl.get('dest_path', '')
                    if dest and os.path.isfile(dest):
                        try:
                            os.remove(dest)
                        except OSError:
                            pass
                    with _dl()._lock:
                        dl['status'] = 'failed'
                        dl['error'] = (
                            f'Downloaded file too small ({actual_size} bytes) — '
                            f'likely an error page, not the real file. '
                            f'Expected ~{expected_size} bytes.'
                            if expected_size
                            else f'Downloaded file too small ({actual_size} bytes) — '
                                 f'likely an error page.'
                        )
                        _dl()._save_state()
                    _dl()._emit('dl:update', _sanitize(dl))
                    _dl()._log_history(dl, 'failed')
                    _dl()._flush_state()
                    return

                with _dl()._lock:
                    dl['status'] = 'completed'
                    dl['progress'] = 100
                    dl['completed_at'] = time.time()
                    dl['retry_count'] = 0
                    _dl()._save_state()
                _dl()._emit('dl:update', _sanitize(dl))
                _dl()._emit('dl:completed', _dl_completed_payload(dl))
                _dl()._log_history(dl, 'completed')
                _dl()._flush_state()
                return

        except Exception as e:
            err_str = str(e)[:500]
            # Check if transient and retries remain
            if attempt < max_retries and _dl()._is_transient_error(err_str):
                delay = _dl().RETRY_BASE_DELAY * (2 ** attempt)
                with _dl()._lock:
                    dl['status'] = 'resolving'  # visual: "retrying"
                    dl['error'] = f'Retry {attempt + 1}/{max_retries} in {delay}s: {err_str}'
                    dl['retry_count'] = attempt + 1
                _dl()._emit('dl:update', _sanitize(dl))
                time.sleep(delay)
                if dl.get('status') == 'cancelled':
                    _dl()._emit('dl:update', _sanitize(dl))
                    return
                continue
            # Non-transient or retries exhausted
            with _dl()._lock:
                dl['status'] = 'failed'
                dl['error'] = err_str
                _dl()._save_state()
            _dl()._emit('dl:update', _sanitize(dl))
            _dl()._log_history(dl, 'failed')
            _dl()._flush_state()
            return


def _calc_speed(dl):
    """Calculate download speed using sliding window (last ~5s) for responsiveness."""
    now = time.time()
    downloaded = dl.get('downloaded', 0)
    samples = dl.get('_speed_samples', [])
    samples.append((now, downloaded))
    # Keep last 10s of samples
    cutoff = now - 10
    samples = [(t, d) for t, d in samples if t >= cutoff]
    dl['_speed_samples'] = samples
    if len(samples) < 2:
        return 0
    # Use 5s window for calculation
    window = 5
    oldest = None
    for t, d in samples:
        if t >= now - window:
            oldest = (t, d)
            break
    if not oldest:
        oldest = samples[0]
    elapsed = now - oldest[0]
    if elapsed < 0.5:
        return 0
    return int((downloaded - oldest[1]) / elapsed)


def _calc_eta(dl):
    """Calculate ETA in seconds, or 0 if unknown."""
    speed = dl.get('speed', 0)
    if speed <= 0:
        return 0
    total = dl.get('filesize', 0)
    downloaded = dl.get('downloaded', 0)
    remaining = total - downloaded
    if remaining <= 0:
        return 0
    return int(remaining / speed)


def _get_category_for_file(filename, config):
    """Determine category and destination path based on file extension."""
    if not config.get('auto_categorize', True):
        return None, None
    
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    if not ext:
        return None, None
    
    categories = config.get('categories', [])
    for cat in categories:
        if ext in cat.get('extensions', []):
            # Found matching category
            # If cat path is absolute, use it. Else relative to default_dir
            cat_path = cat.get('path')
            if not cat_path:
                base_dir = config.get('default_dir', '/home')
                cat_path = os.path.join(base_dir, cat['name'])
            return cat['id'], _dl()._safe_path(cat_path)
            
    # No match found - use 'other' category if defined
    other = next((c for c in categories if c['id'] == 'other'), None)
    if other:
         cat_path = other.get('path')
         if not cat_path:
             base_dir = config.get('default_dir', '/home')
             cat_path = os.path.join(base_dir, other['name'])
         return other['id'], _dl()._safe_path(cat_path)

    return None, None


def _sanitize(dl):
    """Return a safe copy for JSON serialization."""
    d = {
        'id': dl.get('id'),
        'url': dl.get('url', ''),
        'filename': dl.get('filename', ''),
        'filesize': dl.get('filesize', 0),
        'downloaded': dl.get('downloaded', 0),
        'progress': dl.get('progress', 0),
        'speed': dl.get('speed', 0),
        'status': dl.get('status', 'pending'),
        'error': dl.get('error', ''),
        'debrid_error': dl.get('debrid_error', ''),
        'dest_dir': dl.get('dest_dir', ''),
        'dest_path': dl.get('dest_path', ''),
        'use_debrid': dl.get('use_debrid', True),
        'added_at': dl.get('added_at', 0),
        'started_at': dl.get('started_at', 0),
        'completed_at': dl.get('completed_at', 0),
        'is_torrent': dl.get('is_torrent', False),
        'priority': dl.get('priority', 0),
        'package_id': dl.get('package_id', ''),
        'eta': _calc_eta(dl),
        'retry_count': dl.get('retry_count', 0),
        'category_id': dl.get('category_id', ''),
    }
    if d['is_torrent']:
        d['torrent_status'] = dl.get('torrent_status', '')
        d['torrent_seeders'] = dl.get('torrent_seeders', 0)
        d['torrent_speed'] = dl.get('torrent_speed', 0)
        d['torrent_files_total'] = dl.get('torrent_files_total', 0)
        d['torrent_files_done'] = dl.get('torrent_files_done', 0)
    return d


def _dl_completed_payload(dl):
    """Build the payload sent with dl:completed events."""
    folder = dl.get('dest_dir') or dl.get('dest_path') or ''
    return {
        'id': dl.get('id'),
        'filename': dl.get('filename', ''),
        'filesize': dl.get('downloaded', 0),
        'folder': folder,
        'dest_dir': dl.get('dest_dir', ''),
        'dest_path': dl.get('dest_path', ''),
    }


# ─── Concurrent download manager ───

_active_threads = {}


def _start_next():
    """Start next pending download if under concurrency limit.
    Thread-safe: holds _lock while checking counts and claiming pending downloads.
    Torrents start immediately (debrid handles them remotely).
    Direct downloads wait for local concurrency slots.
    """
    # Use global config for max_concurrent (system-wide limit)
    config = _dl()._load_config(username=None)
    max_conc = config.get('max_concurrent', 3)

    with _dl()._lock:
        # Only count LOCAL downloads toward the concurrency limit.
        # Remote debrid operations (torrent_uploading, torrent_downloading)
        # happen on the debrid server and use no local bandwidth/disk.
        active = sum(1 for d in _dl()._downloads.values()
                     if d['status'] in ('downloading', 'resolving', 'merging'))

        # Find next pending (highest priority first: high=2, normal=1, low=0, then earliest added)
        def _priority_value(d):
            p = d.get('priority', 'normal')
            if p == 'high': return 2
            if p == 'low': return 0
            return 1
        
        pending = sorted(
            [d for d in _dl()._downloads.values() if d['status'] == 'pending'],
            key=lambda d: (-_priority_value(d), d.get('added_at', 0))
        )
        to_start = []
        for dl in pending:
            # Torrents/magnets start immediately - no local slot needed
            if _dl()._is_torrent(dl.get('url', '')):
                dl['status'] = 'resolving'
                to_start.append(dl['id'])
            # Direct downloads only if slot available
            elif active < max_conc:
                dl['status'] = 'resolving'  # claim immediately to prevent double-start
                to_start.append(dl['id'])
                active += 1

    # Start threads outside lock
    for dl_id in to_start:
        t = threading.Thread(target=_download_then_next, args=(dl_id,), daemon=True)
        _active_threads[dl_id] = t
        t.start()


def _download_then_next(dl_id):
    """Download, then trigger next queued download."""
    try:
        _download_worker(dl_id)
    except Exception as exc:
        # Safety net: mark download failed on unhandled crash
        logging.exception('[downloads] Worker crashed for %s', dl_id)
        with _dl()._lock:
            dl = _dl()._downloads.get(dl_id)
            if dl and dl['status'] not in ('completed', 'cancelled', 'failed'):
                dl['status'] = 'failed'
                dl['error'] = f'Unexpected error: {str(exc)[:300]}'
                _dl()._save_state()
        _dl()._emit('dl:update', _sanitize(dl) if dl else {})
    finally:
        with _dl()._lock:
            _active_threads.pop(dl_id, None)
        # Check if this completes a package
        _check_package_completion(dl_id)
        _start_next()


def _check_package_completion(dl_id):
    """Check if the completed download finishes a package, trigger auto-extract if needed."""
    with _dl()._lock:
        dl = _dl()._downloads.get(dl_id)
        if not dl or dl.get('status') != 'completed':
            return
        pkg_id = dl.get('package_id')
        if not pkg_id:
            return
        pkg = _dl()._packages.get(pkg_id)
        if not pkg or pkg.get('status') in ('extracting', 'extracted'):
            return
        # Check if ALL downloads in the package are completed
        all_done = all(
            _dl()._downloads.get(did, {}).get('status') == 'completed'
            for did in pkg.get('dl_ids', [])
        )
        if not all_done:
            return
        pkg['status'] = 'completed'
        _dl()._save_state()
    _dl()._emit('dl:package_update', _sanitize_package(pkg))
    # Auto-extract if enabled
    if pkg.get('auto_extract'):
        _enqueue_extraction(pkg_id)


def _sanitize_package(pkg):
    """Return a safe copy of package for JSON."""
    return {
        'id': pkg.get('id', ''),
        'name': pkg.get('name', ''),
        'dl_ids': pkg.get('dl_ids', []),
        'dest_dir': pkg.get('dest_dir', ''),
        'status': pkg.get('status', 'downloading'),
        'auto_extract': pkg.get('auto_extract', False),
        'delete_after_extract': pkg.get('delete_after_extract', False),
        'extract_password': '***' if pkg.get('extract_password') else '',
        'extract_error': pkg.get('extract_error', ''),
        'created_at': pkg.get('created_at', 0),
        'has_archives': pkg.get('has_archives', False),
    }


# ─── Deep extract ───

def _is_archive_file(filename):
    """Check if a filename looks like an archive."""
    fn_lower = filename.lower()
    for ext in _dl().ARCHIVE_EXTENSIONS:
        if fn_lower.endswith(ext):
            return True
    if _dl().RAR_PART_RE.search(fn_lower):
        return True
    return False


def _is_first_part(filepath):
    """For multi-part archives, return True only for the first part."""
    fn = os.path.basename(filepath).lower()
    # .part2.rar, .part3.rar → skip (not first)
    m = re.search(r'\.part(\d+)\.rar$', fn, re.IGNORECASE)
    if m:
        return int(m.group(1)) == 1
    # .r00, .r01 → skip, only .rar is first
    if re.search(r'\.r\d+$', fn, re.IGNORECASE):
        return False
    return True


def _extract_single(archive_path, dest_dir, password=''):
    """Extract a single archive. Returns (success, error_msg)."""
    fn_lower = archive_path.lower()
    cmd = None

    if fn_lower.endswith(('.rar',)) or _dl().RAR_PART_RE.search(fn_lower):
        # Use unrar or 7z for rar
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}']
        if password:
            cmd.append(f'-p{password}')
        else:
            cmd.append('-p-')  # no password, skip prompts
        cmd.append(archive_path)
    elif fn_lower.endswith('.7z'):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}']
        if password:
            cmd.append(f'-p{password}')
        cmd.append(archive_path)
    elif fn_lower.endswith('.zip'):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}']
        if password:
            cmd.append(f'-p{password}')
        cmd.append(archive_path)
    elif fn_lower.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar')):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}', archive_path]
    elif fn_lower.endswith(('.gz', '.bz2', '.xz')):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}', archive_path]
    elif fn_lower.endswith(('.cab', '.iso')):
        cmd = ['7z', 'x', '-y', f'-o{dest_dir}', archive_path]
    else:
        return False, f'Unsupported format: {os.path.basename(archive_path)}'

    try:
        # Auto-install 7z if missing
        from host import ensure_dep
        ok, msg = ensure_dep('7z', install=True)
        if not ok:
            return False, f'Missing 7z: {msg}'

        logging.info('[extract] cmd=%s', ' '.join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        logging.info('[extract] returncode=%d stdout=%.300s stderr=%.300s',
                     result.returncode, result.stdout.strip(), result.stderr.strip())
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            # Check for password errors
            if 'Wrong password' in err or 'incorrect password' in err.lower():
                return False, 'Invalid archive password'
            return False, err[:500]
        return True, ''
    except subprocess.TimeoutExpired:
        logging.error('[extract] Timeout extracting %s', archive_path)
        return False, 'Timeout — extraction took too long'
    except Exception as e:
        logging.exception('[extract] Error extracting %s', archive_path)
        return False, str(e)[:500]


def _deep_extract_dir(directory, password='', delete_after=False, max_depth=5):
    """Recursively extract all archives in directory.
    Returns (total_extracted, errors_list).
    """
    total_extracted = 0
    errors = []

    for depth in range(max_depth):
        # Find all archive files in directory tree
        archives_found = []
        for root, dirs, files in os.walk(directory):
            for fname in files:
                fpath = os.path.join(root, fname)
                if _is_archive_file(fname) and _is_first_part(fpath):
                    archives_found.append(fpath)

        if not archives_found:
            break  # No more archives

        extracted_this_round = 0
        for archive_path in archives_found:
            if not os.path.exists(archive_path):
                continue
            extract_dest = os.path.dirname(archive_path)
            ok, err = _extract_single(archive_path, extract_dest, password)
            if ok:
                extracted_this_round += 1
                total_extracted += 1
                if delete_after:
                    _delete_archive_parts(archive_path)
            else:
                errors.append(f'{os.path.basename(archive_path)}: {err}')

        if extracted_this_round == 0:
            break  # Nothing new extracted, stop recursion

    return total_extracted, errors


def _delete_archive_parts(archive_path):
    """Delete an archive and all its parts (for multi-part rar/zip)."""
    try:
        base = archive_path.lower()
        directory = os.path.dirname(archive_path)
        basename = os.path.basename(archive_path)

        # For partN.rar multi-part: delete all .partN.rar files
        m = re.match(r'(.*)\.part\d+\.rar$', basename, re.IGNORECASE)
        if m:
            prefix = m.group(1)
            for f in os.listdir(directory):
                if re.match(re.escape(prefix) + r'\.part\d+\.rar$', f, re.IGNORECASE):
                    try:
                        os.remove(os.path.join(directory, f))
                    except OSError:
                        pass
            return

        # For .rar + .r00, .r01 etc.
        if basename.lower().endswith('.rar'):
            stem = basename[:-4]
            for f in os.listdir(directory):
                if f.lower().startswith(stem.lower()) and (
                    f.lower().endswith('.rar') or re.search(r'\.r\d+$', f, re.IGNORECASE)
                ):
                    try:
                        os.remove(os.path.join(directory, f))
                    except OSError:
                        pass
            return

        # Single file archive
        try:
            os.remove(archive_path)
        except OSError:
            pass
    except Exception:
        pass


def _scan_for_archives(directory):
    """Check if a directory contains any archive files."""
    if not directory or not os.path.isdir(directory):
        return False
    for root, dirs, files in os.walk(directory):
        for fname in files:
            if _is_archive_file(fname):
                return True
    return False


def _extract_package_files(archive_paths, dest_dir, password='', delete_after=False):
    """Extract only the specified archive files.
    Returns (total_extracted, errors_list).
    """
    total_extracted = 0
    errors = []

    for archive_path in archive_paths:
        if not os.path.exists(archive_path):
            continue
        extract_dest = os.path.dirname(archive_path) or dest_dir
        ok, err = _extract_single(archive_path, extract_dest, password)
        if ok:
            total_extracted += 1
            if delete_after:
                _delete_archive_parts(archive_path)
        else:
            errors.append(f'{os.path.basename(archive_path)}: {err}')

    return total_extracted, errors


def _enqueue_extraction(package_id):
    """Add a package to the extraction queue and start the worker if not running."""
    # (global removed — variable accessed via _dl() in split module)
    _dl()._extract_queue.append(package_id)
    logging.info('[extract] Enqueued %s (queue length: %d)', package_id, len(_dl()._extract_queue))
    # Start the extraction worker thread if not already running
    with _dl()._extract_start_lock:
        if _dl()._extract_thread is None or not _dl()._extract_thread.is_alive():
            _dl()._extract_thread = threading.Thread(target=_extraction_worker, daemon=True)
            _dl()._extract_thread.start()


def _extraction_worker():
    """Single worker thread that processes extraction queue sequentially."""
    while True:
        try:
            package_id = _dl()._extract_queue.popleft()
        except IndexError:
            logging.info('[extract] Queue empty, worker exiting')
            return
        logging.info('[extract] Starting extraction for %s', package_id)
        _dl()._extract_running.set()
        try:
            _run_package_extract(package_id)
        except Exception:
            logging.exception('[extract] Unhandled error extracting %s', package_id)
        finally:
            _dl()._extract_running.clear()


def _run_package_extract(package_id):
    """Run deep extraction for a package (called by _extraction_worker)."""
    with _dl()._lock:
        pkg = _dl()._packages.get(package_id)
        if not pkg:
            logging.warning('[extract] Package %s not found, skipping', package_id)
            return
        pkg['status'] = 'extracting'
        pkg['extract_error'] = ''
        _dl()._save_state()
        # Collect downloaded file paths belonging to this package
        pkg_files = []
        for did in pkg.get('dl_ids', []):
            dl = _dl()._downloads.get(did)
            if dl:
                fp = dl.get('dest_path') or dl.get('filepath') or ''
                if fp:
                    pkg_files.append(fp)
    _dl()._emit('dl:package_update', _sanitize_package(pkg))

    dest_dir = _dl()._safe_path(pkg.get('dest_dir', ''))
    password = pkg.get('extract_password', '')
    delete_after = pkg.get('delete_after_extract', False)

    logging.info('[extract] pkg=%s dest_dir=%s pkg_files=%s delete_after=%s',
                 package_id, dest_dir, pkg_files, delete_after)

    if not dest_dir or not os.path.isdir(dest_dir):
        err_msg = f'Target folder not found: {dest_dir}'
        logging.error('[extract] %s', err_msg)
        with _dl()._lock:
            pkg['status'] = 'extract_failed'
            pkg['extract_error'] = 'Target folder not found'
            _dl()._save_state()
        _dl()._emit('dl:package_update', _sanitize_package(pkg))
        return

    # Extract only archives from this package's files, not the whole directory
    pkg_archives = [f for f in pkg_files if os.path.isfile(f) and _is_archive_file(os.path.basename(f)) and _is_first_part(f)]
    logging.info('[extract] Found %d archives from pkg_files, fallback to deep=%s',
                 len(pkg_archives), not bool(pkg_archives))
    if pkg_archives:
        total, errors = _extract_package_files(pkg_archives, dest_dir, password, delete_after)
    else:
        total, errors = _deep_extract_dir(dest_dir, password, delete_after)

    logging.info('[extract] pkg=%s total=%d errors=%s', package_id, total, errors)

    with _dl()._lock:
        if errors and total == 0:
            pkg['status'] = 'extract_failed'
            pkg['extract_error'] = '; '.join(errors[:5])
        else:
            pkg['status'] = 'extracted'
            if errors:
                pkg['extract_error'] = f'Extracted {total}, errors: ' + '; '.join(errors[:3])
            else:
                pkg['extract_error'] = ''
        pkg['has_archives'] = _scan_for_archives(dest_dir)
        _dl()._save_state()
    _dl()._emit('dl:package_update', _sanitize_package(pkg))


