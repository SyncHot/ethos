"""
Radio & Music v2 — Radio Browser API integration.
Provides: search, top stations, countries, tags
"""
import os
import sys
import requests
from flask import jsonify

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Radio Browser API base URL
RADIO_API = 'https://de1.api.radio-browser.info/json'

def search_stations(query='', country='', tag='', limit=50):
    """Search radio stations via Radio Browser API."""
    try:
        params = {
            'name': query,
            'country': country,
            'tag': tag,
            'limit': limit,
            'order': 'votes',
            'reverse': 'true',
            'hidebroken': 'true'
        }
        # Remove empty params
        params = {k: v for k, v in params.items() if v}
        
        r = requests.get(f'{RADIO_API}/stations/search', params=params, timeout=10)
        if r.status_code != 200:
            return []
        
        stations = r.json()
        return [{
            'id': s['stationuuid'],
            'name': s['name'],
            'url': s['url_resolved'] or s['url'],
            'favicon': s['favicon'],
            'country': s['country'],
            'tags': s['tags'],
            'votes': s['votes'],
            'codec': s.get('codec', ''),
            'bitrate': s.get('bitrate', 0)
        } for s in stations]
    except Exception as e:
        print(f'Radio search error: {e}')
        return []

def get_top_stations(limit=50):
    """Get top voted stations."""
    try:
        params = {'limit': limit, 'order': 'votes', 'reverse': 'true', 'hidebroken': 'true'}
        r = requests.get(f'{RADIO_API}/stations/search', params=params, timeout=10)
        if r.status_code != 200:
            return []
        
        stations = r.json()
        return [{
            'id': s['stationuuid'],
            'name': s['name'],
            'url': s['url_resolved'] or s['url'],
            'favicon': s['favicon'],
            'country': s['country'],
            'tags': s['tags'],
            'votes': s['votes']
        } for s in stations]
    except Exception as e:
        print(f'Top stations error: {e}')
        return []

def get_countries():
    """Get list of countries with station counts."""
    try:
        r = requests.get(f'{RADIO_API}/countries', timeout=10)
        if r.status_code != 200:
            return []
        
        countries = r.json()
        # Sort by station count desc
        countries = sorted(countries, key=lambda x: x.get('stationcount', 0), reverse=True)
        return [{
            'name': c['name'],
            'code': c['iso_3166_1'] or c['name'],
            'count': c.get('stationcount', 0)
        } for c in countries if c.get('stationcount', 0) > 0][:100]  # Top 100
    except Exception as e:
        print(f'Countries error: {e}')
        return []

def get_tags():
    """Get popular genre tags."""
    try:
        r = requests.get(f'{RADIO_API}/tags?limit=100&order=stationcount&reverse=true', timeout=10)
        if r.status_code != 200:
            return []
        
        tags = r.json()
        return [{
            'name': t['name'],
            'count': t.get('stationcount', 0)
        } for t in tags if t.get('stationcount', 0) > 5]  # Filter noise
    except Exception as e:
        print(f'Tags error: {e}')
        return []
