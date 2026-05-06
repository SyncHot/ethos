"""Podcast routes for the Radio & Music blueprint."""

import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from flask import jsonify, request

from blueprints.radio_music import (
    radio_music_bp, log, _safe_int, _user_file, _load_json, _save_json, _ITUNES_API,
)
from blueprints.radio_music_playlist import _migrate_old_subscriptions


# iTunes podcast genre IDs
_PODCAST_GENRES = {
    'all': 26,
    'arts': 1301, 'business': 1321, 'comedy': 1303, 'education': 1304,
    'fiction': 1483, 'health': 1512, 'history': 1487, 'kids': 1305,
    'leisure': 1502, 'music': 1310, 'news': 1489, 'religion': 1314,
    'science': 1533, 'society': 1324, 'sports': 1545, 'technology': 1318,
    'truecrime': 1488, 'tv': 1309,
}


def _parse_duration(s):
    """Parse iTunes duration string (HH:MM:SS or seconds) to total seconds."""
    if not s:
        return 0
    s = s.strip()
    if ':' in s:
        parts = s.split(':')
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return 0
    try:
        return int(s)
    except ValueError:
        return 0


def _autodownload_file():
    return _user_file('autodownload.json')


# ── Podcasts: search via iTunes ──────────────────────────────

@radio_music_bp.route('/podcasts/top', methods=['GET'])
def podcasts_top():
    """Top podcasts by genre and country via iTunes RSS."""
    country = request.args.get('country', 'pl').strip().lower()
    genre_key = request.args.get('genre', '').strip().lower()
    limit = _safe_int(request.args.get('limit', 30), 30, hi=100)
    genre_id = _PODCAST_GENRES.get(genre_key, 0)

    rss_url = f'https://itunes.apple.com/{country}/rss/toppodcasts/limit={limit}'
    if genre_id:
        rss_url += f'/genre={genre_id}'
    rss_url += '/json'

    try:
        req = urllib.request.Request(rss_url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log.debug('iTunes RSS top podcasts error: %s', e)
        return jsonify({'items': []})

    items = []
    for r in data.get('feed', {}).get('entry', []):
        apple_id = r.get('id', {}).get('attributes', {}).get('im:id', '')
        imgs = r.get('im:image', [])
        artwork = imgs[-1].get('label', '') if imgs else ''
        items.append({
            'id': apple_id,
            'name': r.get('im:name', {}).get('label', ''),
            'artist': r.get('im:artist', {}).get('label', ''),
            'artwork': artwork.replace('170x170', '600x600') if artwork else '',
            'genre': r.get('category', {}).get('attributes', {}).get('label', ''),
        })
    return jsonify({'items': items})


@radio_music_bp.route('/podcasts/lookup', methods=['GET'])
def podcasts_lookup():
    """Lookup a podcast by Apple ID to get its RSS feed URL (needed after top charts)."""
    apple_id = request.args.get('id', '').strip()
    if not apple_id:
        return jsonify({'error': 'Brak ID'}), 400
    lookup_url = f'https://itunes.apple.com/lookup?id={apple_id}&entity=podcast'
    try:
        req = urllib.request.Request(lookup_url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log.debug('iTunes lookup error: %s', e)
        return jsonify({'error': str(e)}), 502

    results = data.get('results', [])
    if not results:
        return jsonify({'error': 'Nie znaleziono'}), 404
    r = results[0]
    return jsonify({
        'id': r.get('collectionId', 0),
        'name': r.get('collectionName', ''),
        'artist': r.get('artistName', ''),
        'artwork': r.get('artworkUrl600') or r.get('artworkUrl100', ''),
        'feed_url': r.get('feedUrl', ''),
        'genre': r.get('primaryGenreName', ''),
        'count': r.get('trackCount', 0),
    })


@radio_music_bp.route('/podcasts/search', methods=['GET'])
def podcasts_search():
    q_str = request.args.get('q', '').strip()
    if not q_str:
        return jsonify({'items': []})

    params = {
        'term': q_str,
        'media': 'podcast',
        'limit': 30,
    }
    url = _ITUNES_API + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log.debug('iTunes search error: %s', e)
        return jsonify({'items': []})

    items = []
    for r in data.get('results', []):
        items.append({
            'id': r.get('collectionId', 0),
            'name': r.get('collectionName', ''),
            'artist': r.get('artistName', ''),
            'artwork': r.get('artworkUrl600') or r.get('artworkUrl100', ''),
            'feed_url': r.get('feedUrl', ''),
            'genre': r.get('primaryGenreName', ''),
            'count': r.get('trackCount', 0),
        })
    return jsonify({'items': items})


# ── Podcasts: parse RSS feed ─────────────────────────────────

@radio_music_bp.route('/podcasts/feed', methods=['GET'])
def podcasts_feed():
    feed_url = request.args.get('url', '').strip()
    if not feed_url:
        return jsonify({'error': 'Brak URL feedu.'}), 400

    req = urllib.request.Request(feed_url, headers={'User-Agent': 'EthOS-RadioMusic/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_text = resp.read(10 * 1024 * 1024).decode('utf-8', errors='replace')
    except Exception as e:
        return jsonify({'error': 'Nie udało się pobrać feedu: ' + str(e)}), 502

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return jsonify({'error': 'Błąd parsowania RSS: ' + str(e)}), 502

    ns = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}
    channel = root.find('channel')
    if channel is None:
        return jsonify({'error': 'Nieprawidłowy feed RSS.'}), 400

    podcast = {
        'title': (channel.findtext('title') or '').strip(),
        'description': (channel.findtext('description') or '').strip(),
        'author': (channel.findtext('itunes:author', namespaces=ns) or '').strip(),
        'image': '',
    }
    img_el = channel.find('itunes:image', ns)
    if img_el is not None:
        podcast['image'] = img_el.get('href', '')
    elif channel.find('image') is not None:
        podcast['image'] = channel.findtext('image/url', '') or ''

    episodes = []
    for item in channel.findall('item'):
        enc = item.find('enclosure')
        audio_url = ''
        audio_type = ''
        if enc is not None:
            audio_url = enc.get('url', '')
            audio_type = enc.get('type', '')

        dur_text = item.findtext('itunes:duration', namespaces=ns) or ''
        duration = _parse_duration(dur_text)

        ep_img = ''
        ep_img_el = item.find('itunes:image', ns)
        if ep_img_el is not None:
            ep_img = ep_img_el.get('href', '')

        episodes.append({
            'title': (item.findtext('title') or '').strip(),
            'description': (item.findtext('description') or item.findtext('itunes:summary', namespaces=ns) or '').strip(),
            'pub_date': (item.findtext('pubDate') or '').strip(),
            'audio_url': audio_url,
            'audio_type': audio_type,
            'duration': duration,
            'duration_fmt': dur_text,
            'image': ep_img,
            'guid': (item.findtext('guid') or audio_url).strip(),
        })

    return jsonify({'podcast': podcast, 'episodes': episodes})


# ── Podcasts: subscriptions ──────────────────────────────────

@radio_music_bp.route('/podcasts/subscriptions', methods=['GET'])
def podcasts_subs():
    """
    PHASE 2: Get podcast subscriptions.
    Automatically migrates old subscriptions.json to unified favorites.json.
    Returns items with source='podcast' from unified favorites.json.
    """
    _migrate_old_subscriptions()
    all_favs = _load_json(_user_file('favorites.json'), [])
    # Filter only podcast subscriptions (source='podcast')
    pod_subs = [f for f in all_favs if f.get('source') == 'podcast']
    return jsonify({'items': pod_subs})


@radio_music_bp.route('/podcasts/subscribe', methods=['POST'])
def podcasts_subscribe():
    """
    PHASE 2: Add/remove podcast subscription to unified favorites.json.
    Automatically adds source='podcast' and type='subscription' fields.
    """
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    podcast = body.get('podcast')
    if not podcast or not podcast.get('feed_url'):
        return jsonify({'error': 'Brak danych podcastu.'}), 400

    _migrate_old_subscriptions()
    favs = _load_json(_user_file('favorites.json'), [])

    if action == 'remove':
        favs = [f for f in favs if f.get('feed_url') != podcast['feed_url']]
    else:
        if not any(f.get('feed_url') == podcast['feed_url'] for f in favs):
            # Ensure source and type are set
            podcast = dict(podcast)
            podcast.setdefault('source', 'podcast')
            podcast.setdefault('type', 'subscription')
            podcast.setdefault('subscribed_at', time.time())
            # Normalize title field
            if 'title' not in podcast and 'name' in podcast:
                podcast['title'] = podcast['name']
            favs.insert(0, podcast)

    _save_json(_user_file('favorites.json'), favs)
    return jsonify({'ok': True, 'items': favs})


# ── Podcast auto-download ────────────────────────────────────

@radio_music_bp.route('/podcasts/autodownload', methods=['GET'])
def podcasts_autodownload_get():
    """Return auto-download settings: {feeds: {feed_url: {enabled, max_episodes, downloaded}}}"""
    return jsonify(_load_json(_autodownload_file(), {'feeds': {}}))


@radio_music_bp.route('/podcasts/autodownload', methods=['POST'])
def podcasts_autodownload_set():
    """Toggle auto-download for a feed_url. Body: {feed_url, enabled?, max_episodes?}"""
    body = request.get_json(force=True, silent=True) or {}
    feed_url = body.get('feed_url', '').strip()
    if not feed_url:
        return jsonify({'error': 'feed_url required'}), 400

    cfg = _load_json(_autodownload_file(), {'feeds': {}})
    feeds = cfg.setdefault('feeds', {})

    if 'enabled' in body:
        entry = feeds.setdefault(feed_url, {'enabled': False, 'max_episodes': 3, 'downloaded': []})
        entry['enabled'] = bool(body['enabled'])
    if 'max_episodes' in body:
        entry = feeds.setdefault(feed_url, {'enabled': False, 'max_episodes': 3, 'downloaded': []})
        entry['max_episodes'] = max(1, min(50, int(body['max_episodes'])))

    _save_json(_autodownload_file(), cfg)
    return jsonify({'ok': True, 'feeds': feeds})
