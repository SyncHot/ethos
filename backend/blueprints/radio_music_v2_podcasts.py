"""
Radio & Music v2 — Podcast search and RSS parsing.
Provides: iTunes search, RSS feed parser
"""
import os
import sys
import requests
import feedparser
from flask import jsonify

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

ITUNES_SEARCH_API = 'https://itunes.apple.com/search'

def search_podcasts(query, limit=20):
    """Search podcasts via iTunes API."""
    try:
        params = {
            'term': query,
            'media': 'podcast',
            'limit': limit
        }
        r = requests.get(ITUNES_SEARCH_API, params=params, timeout=10)
        if r.status_code != 200:
            return []
        
        data = r.json()
        results = data.get('results', [])
        
        podcasts = []
        for p in results:
            podcasts.append({
                'id': p.get('collectionId', ''),
                'name': p.get('collectionName', ''),
                'artist': p.get('artistName', ''),
                'artwork': p.get('artworkUrl600', p.get('artworkUrl100', '')),
                'feedUrl': p.get('feedUrl', ''),
                'genres': p.get('genres', []),
                'trackCount': p.get('trackCount', 0)
            })
        
        return podcasts
    except Exception as e:
        print(f'Podcast search error: {e}')
        return []

def parse_podcast_feed(feed_url, limit=50):
    """Parse podcast RSS feed and return episodes."""
    try:
        # Fetch and parse feed
        feed = feedparser.parse(feed_url)
        
        if not feed.entries:
            return {'error': 'No episodes found'}
        
        # Extract podcast info
        podcast_info = {
            'title': feed.feed.get('title', 'Unknown Podcast'),
            'description': feed.feed.get('description', ''),
            'image': '',
            'link': feed.feed.get('link', '')
        }
        
        # Try to get podcast image
        if hasattr(feed.feed, 'image') and 'href' in feed.feed.image:
            podcast_info['image'] = feed.feed.image.href
        elif hasattr(feed.feed, 'itunes_image'):
            podcast_info['image'] = feed.feed.itunes_image.get('href', '')
        
        # Parse episodes
        episodes = []
        for entry in feed.entries[:limit]:
            # Find audio enclosure
            audio_url = ''
            duration = 0
            
            for enclosure in entry.get('enclosures', []):
                if 'audio' in enclosure.get('type', ''):
                    audio_url = enclosure.get('href', '')
                    break
            
            # Try itunes:duration
            if hasattr(entry, 'itunes_duration'):
                try:
                    dur_str = entry.itunes_duration
                    # Parse HH:MM:SS or MM:SS or seconds
                    parts = dur_str.split(':')
                    if len(parts) == 3:
                        duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2:
                        duration = int(parts[0]) * 60 + int(parts[1])
                    else:
                        duration = int(parts[0])
                except:
                    pass
            
            episodes.append({
                'title': entry.get('title', 'Untitled'),
                'description': entry.get('summary', ''),
                'url': audio_url,
                'pubDate': entry.get('published', ''),
                'duration': duration,
                'guid': entry.get('id', audio_url)
            })
        
        return {
            'podcast': podcast_info,
            'episodes': episodes
        }
    except Exception as e:
        print(f'Feed parse error: {e}')
        return {'error': str(e)}
