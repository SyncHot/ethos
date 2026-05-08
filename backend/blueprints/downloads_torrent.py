"""
EthOS — Downloads: Torrent via Debrid
Handles torrent/magnet uploads to AllDebrid, Real-Debrid, Premiumize,
DebridLink, and TorBox.
All module-level state is accessed via _dl() to avoid gevent import-lock issues.
Worker helpers (e.g. _sanitize) are accessed via _dl_worker().
"""

import sys
import json
import time
import ssl
import urllib.request
import urllib.parse
import uuid


def _dl():
    """Return the blueprints.downloads module without importing it directly."""
    return sys.modules.get('blueprints.downloads')


def _dl_worker():
    """Return blueprints.downloads_worker without importing it directly."""
    return sys.modules.get('blueprints.downloads_worker')


# ─── Torrent via Debrid ───

def _torrent_alldebrid(magnet_or_url, api_key, torrent_file=None):
    """Add torrent/magnet to AllDebrid, wait for completion, return file links."""
    base = "https://api.alldebrid.com/v4.1"
    agent = "EthOS"

    if torrent_file:
        # Upload .torrent file — use magnet/upload/file with multipart
        boundary = uuid.uuid4().hex
        body = b''
        body += f'--{boundary}\r\n'.encode()
        body += b'Content-Disposition: form-data; name="files[]"; filename="upload.torrent"\r\n'
        body += b'Content-Type: application/x-bittorrent\r\n\r\n'
        body += torrent_file
        body += f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(
            f"{base}/magnet/upload/file?agent={agent}&apikey={urllib.parse.quote(api_key)}",
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            result = json.loads(resp.read().decode())

        if result.get('status') != 'success':
            err = result.get('error', {}).get('message', 'Upload failed')
            raise Exception(f"AllDebrid torrent: {err}")

        # /magnet/upload/file returns data.files[] with {id, name, ...}
        data = result.get('data', {})
        files = data.get('files', [])
        if files:
            magnet_id = files[0].get('id')
            if magnet_id:
                return {'service': 'alldebrid', 'torrent_id': magnet_id, 'api_key': api_key}
            err = files[0].get('error', {})
            if isinstance(err, dict):
                err = err.get('message', 'No magnet ID')
            raise Exception(f"AllDebrid: {err}")

        # Fallback: check if it returned magnets[] structure instead
        magnets = data.get('magnets', [])
        if magnets:
            magnet_id = magnets[0].get('id')
            if magnet_id:
                return {'service': 'alldebrid', 'torrent_id': magnet_id, 'api_key': api_key}

        raise Exception(f"AllDebrid: no data returned from file upload (keys: {list(data.keys())})")

    else:
        # Magnet link — use magnet/upload
        data_payload = urllib.parse.urlencode({'magnets[]': magnet_or_url}).encode()
        req = urllib.request.Request(
            f"{base}/magnet/upload?agent={agent}&apikey={urllib.parse.quote(api_key)}",
            data=data_payload,
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            result = json.loads(resp.read().decode())

        if result.get('status') != 'success':
            err = result.get('error', {}).get('message', 'Upload failed')
            raise Exception(f"AllDebrid torrent: {err}")

        magnets = result.get('data', {}).get('magnets', [])
        if not magnets:
            raise Exception(f"AllDebrid: no magnet data (keys: {list(result.get('data', {}).keys())})")

        magnet_id = magnets[0].get('id')
        if not magnet_id:
            err = magnets[0].get('error', {})
            if isinstance(err, dict):
                err = err.get('message', 'No magnet ID')
            raise Exception(f"AllDebrid: {err}")

        return {'service': 'alldebrid', 'torrent_id': magnet_id, 'api_key': api_key}


def _poll_alldebrid(torrent_id, api_key, dl):
    """Poll AllDebrid v4.1 until torrent is ready, return list of download links."""
    base = "https://api.alldebrid.com/v4.1"
    agent = "EthOS"

    for _ in range(600):  # up to ~30 min
        if dl.get('status') == 'cancelled':
            try:
                _dl()._http_get_json(f"{base}/magnet/delete?agent={agent}&apikey={urllib.parse.quote(api_key)}&id={torrent_id}")
            except Exception:
                pass
            return []

        result = _dl()._http_get_json(
            f"{base}/magnet/status?agent={agent}&apikey={urllib.parse.quote(api_key)}&id={torrent_id}"
        )
        if result.get('status') != 'success':
            time.sleep(3)
            continue

        # v4.1: data.magnets is an object (not a list)
        data = result.get('data', {}).get('magnets', {})
        if isinstance(data, list):
            data = data[0] if data else {}

        status_code = data.get('statusCode', 0)
        dl['torrent_status'] = data.get('status', '')
        dl['torrent_seeders'] = data.get('seeders', 0)
        dl['torrent_speed'] = data.get('downloadSpeed', 0)

        size = data.get('size', 0)
        downloaded = data.get('downloaded', 0)
        if size > 0:
            dl['progress'] = round(downloaded / size * 100, 1)
        dl['filesize'] = size
        _dl()._emit('dl:update', _dl_worker()._sanitize(dl))

        if status_code == 4:  # Ready
            links = []

            # v4.1 files[] can be nested: folders have {n, e:[...]}, files have {n, s, l}
            def _extract_ad_files(items):
                """Recursively extract all files from AllDebrid nested structure."""
                for obj in items:
                    link = obj.get('l', '')
                    entries = obj.get('e')
                    if link:
                        # This is a file with a direct link
                        try:
                            resolved = _dl()._resolve_alldebrid(link, api_key)
                            links.append({
                                'url': resolved['url'],
                                'filename': resolved.get('filename') or obj.get('n', ''),
                                'filesize': resolved.get('filesize') or obj.get('s', 0),
                            })
                        except Exception:
                            links.append({'url': link, 'filename': obj.get('n', ''), 'filesize': obj.get('s', 0)})
                    elif entries and isinstance(entries, list):
                        # This is a folder — recurse into entries
                        _extract_ad_files(entries)

            _extract_ad_files(data.get('files', []))

            # Fallback: check old-style 'links' field too
            if not links:
                for link_obj in data.get('links', []):
                    link_url = link_obj.get('link', '') or link_obj.get('l', '')
                    fname = link_obj.get('filename', '') or link_obj.get('n', '')
                    fsize = link_obj.get('size', 0) or link_obj.get('s', 0)
                    if link_url:
                        try:
                            resolved = _dl()._resolve_alldebrid(link_url, api_key)
                            links.append(resolved)
                        except Exception:
                            links.append({'url': link_url, 'filename': fname, 'filesize': fsize})
            return links

        if status_code >= 5:  # Error
            raise Exception(f"AllDebrid torrent error: {data.get('status', 'unknown')}")

        time.sleep(3)

    raise Exception("AllDebrid: timeout waiting for torrent")


def _torrent_realdebrid(magnet_or_url, api_key, torrent_file=None):
    """Add torrent/magnet to Real-Debrid."""
    headers = {'Authorization': f'Bearer {api_key}'}
    ctx = ssl.create_default_context()

    if torrent_file:
        req = urllib.request.Request(
            "https://api.real-debrid.com/rest/1.0/torrents/addTorrent",
            data=torrent_file,
            headers={**headers, 'Content-Type': 'application/x-bittorrent'}
        )
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            result = json.loads(resp.read().decode())
    else:
        data = urllib.parse.urlencode({'magnet': magnet_or_url}).encode()
        req = urllib.request.Request(
            "https://api.real-debrid.com/rest/1.0/torrents/addMagnet",
            data=data, headers=headers
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            result = json.loads(resp.read().decode())

    torrent_id = result.get('id')
    if not torrent_id:
        raise Exception("Real-Debrid: no torrent ID returned")

    # Select all files
    data = urllib.parse.urlencode({'files': 'all'}).encode()
    req = urllib.request.Request(
        f"https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{torrent_id}",
        data=data, headers=headers
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        pass  # 204 No Content

    return {'service': 'realdebrid', 'torrent_id': torrent_id, 'api_key': api_key}


def _poll_realdebrid(torrent_id, api_key, dl):
    """Poll Real-Debrid until torrent is ready."""
    headers = {'Authorization': f'Bearer {api_key}'}

    for _ in range(600):
        if dl.get('status') == 'cancelled':
            try:
                req = urllib.request.Request(
                    f"https://api.real-debrid.com/rest/1.0/torrents/delete/{torrent_id}",
                    method='DELETE', headers=headers
                )
                ctx = ssl.create_default_context()
                urllib.request.urlopen(req, context=ctx, timeout=10)
            except Exception:
                pass
            return []

        result = _dl()._http_get_json(
            f"https://api.real-debrid.com/rest/1.0/torrents/info/{torrent_id}",
            headers=headers
        )

        status = result.get('status', '')
        dl['torrent_status'] = status
        dl['torrent_seeders'] = result.get('seeders', 0)
        dl['torrent_speed'] = result.get('speed', 0)

        progress = result.get('progress', 0)
        dl['progress'] = round(progress, 1)
        dl['filesize'] = result.get('bytes', 0)
        _dl()._emit('dl:update', _dl_worker()._sanitize(dl))

        if status == 'downloaded':
            links = []
            for link_url in result.get('links', []):
                # Unrestrict each link
                try:
                    resolved = _dl()._resolve_realdebrid(link_url, api_key)
                    links.append(resolved)
                except Exception:
                    links.append({'url': link_url, 'filename': '', 'filesize': 0})
            return links

        if status in ('magnet_error', 'error', 'virus', 'dead'):
            raise Exception(f"Real-Debrid torrent: {status}")

        time.sleep(3)

    raise Exception("Real-Debrid: timeout waiting for torrent")


def _torrent_premiumize(magnet_or_url, api_key, torrent_file=None):
    """Add torrent/magnet to Premiumize."""
    if torrent_file:
        boundary = uuid.uuid4().hex
        body = b''
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="apikey"\r\n\r\n{api_key}\r\n'.encode()
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'.encode()
        body += torrent_file
        body += f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(
            "https://www.premiumize.me/api/transfer/create",
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            result = json.loads(resp.read().decode())
    else:
        result = _dl()._http_post_json(
            "https://www.premiumize.me/api/transfer/create",
            data={'apikey': api_key, 'src': magnet_or_url}
        )

    if result.get('status') != 'success':
        raise Exception(f"Premiumize: {result.get('message', 'failed')}")

    transfer_id = result.get('id')
    if not transfer_id:
        raise Exception("Premiumize: no transfer ID")

    return {'service': 'premiumize', 'torrent_id': transfer_id, 'api_key': api_key}


def _poll_premiumize(torrent_id, api_key, dl):
    """Poll Premiumize until torrent is ready."""
    for _ in range(600):
        if dl.get('status') == 'cancelled':
            try:
                _dl()._http_post_json("https://www.premiumize.me/api/transfer/delete",
                                data={'apikey': api_key, 'id': torrent_id})
            except Exception:
                pass
            return []

        result = _dl()._http_get_json(
            f"https://www.premiumize.me/api/transfer/list?apikey={urllib.parse.quote(api_key)}"
        )
        if result.get('status') != 'success':
            time.sleep(3)
            continue

        transfer = None
        for t in result.get('transfers', []):
            if str(t.get('id')) == str(torrent_id):
                transfer = t
                break

        if not transfer:
            # Transfer might be done, check folder
            # Try getting direct download links
            try:
                ddl = _dl()._http_post_json(
                    "https://www.premiumize.me/api/transfer/directdl",
                    data={'apikey': api_key, 'src': dl.get('url', '')}
                )
                if ddl.get('status') == 'success' and ddl.get('content'):
                    links = []
                    for item in ddl['content']:
                        links.append({
                            'url': item.get('link', ''),
                            'filename': item.get('path', '').split('/')[-1],
                            'filesize': item.get('size', 0),
                        })
                    return links
            except Exception:
                pass
            raise Exception("Premiumize: transfer disappeared")

        status = transfer.get('status', '')
        dl['torrent_status'] = transfer.get('message', status)
        progress = transfer.get('progress', 0)
        if isinstance(progress, (int, float)):
            dl['progress'] = round(progress * 100, 1)
        _dl()._emit('dl:update', _dl_worker()._sanitize(dl))

        if status == 'finished':
            # Get file links
            folder_id = transfer.get('folder_id') or transfer.get('target_folder_id')
            if folder_id:
                try:
                    folder = _dl()._http_get_json(
                        f"https://www.premiumize.me/api/folder/list?apikey={urllib.parse.quote(api_key)}&id={folder_id}"
                    )
                    links = []
                    for item in folder.get('content', []):
                        if item.get('link'):
                            links.append({
                                'url': item['link'],
                                'filename': item.get('name', ''),
                                'filesize': item.get('size', 0),
                            })
                    return links
                except Exception:
                    pass

            # Fallback: direct download
            try:
                ddl = _dl()._http_post_json(
                    "https://www.premiumize.me/api/transfer/directdl",
                    data={'apikey': api_key, 'src': dl.get('url', '')}
                )
                if ddl.get('status') == 'success' and ddl.get('content'):
                    return [{'url': c.get('link', ''), 'filename': c.get('path', '').split('/')[-1], 'filesize': c.get('size', 0)} for c in ddl['content']]
            except Exception:
                pass
            raise Exception("Premiumize: could not get download links")

        if status == 'error':
            raise Exception(f"Premiumize torrent: {transfer.get('message', 'error')}")

        time.sleep(3)

    raise Exception("Premiumize: timeout waiting for torrent")


def _add_torrent_to_debrid(url, config, torrent_file=None):
    """Submit magnet/torrent to configured debrid service. Returns torrent info dict."""
    service = config.get('debrid_service', 'none')
    if service == 'alldebrid' and config.get('alldebrid_api_key'):
        return _torrent_alldebrid(url, config['alldebrid_api_key'], torrent_file)
    elif service == 'realdebrid' and config.get('realdebrid_api_key'):
        return _torrent_realdebrid(url, config['realdebrid_api_key'], torrent_file)
    elif service == 'premiumize' and config.get('premiumize_api_key'):
        return _torrent_premiumize(url, config['premiumize_api_key'], torrent_file)
    raise Exception("No debrid service configured for torrent handling")


def _poll_torrent(torrent_info, dl):
    """Poll until torrent is downloaded by debrid, return list of file links."""
    service = torrent_info['service']
    tid = torrent_info['torrent_id']
    key = torrent_info['api_key']
    if service == 'alldebrid':
        return _poll_alldebrid(tid, key, dl)
    elif service == 'realdebrid':
        return _poll_realdebrid(tid, key, dl)
    elif service == 'premiumize':
        return _poll_premiumize(tid, key, dl)
    return []


def _guess_filename(url):
    """Extract filename from URL."""
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    name = path.split('/')[-1] if path else ''
    if not name or '.' not in name:
        name = 'download_' + str(int(time.time()))
    # Sanitize
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    return name[:200]


