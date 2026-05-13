"""
Radio & Music v2 — YouTube audio streaming via yt-dlp.
Provides: search, extract audio URL, HTTP 206 proxy streaming
"""
import os
import sys
import json
import requests
from flask import Response, request, jsonify

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from host import host_run, q

def check_ytdlp():
    """Check if yt-dlp is installed."""
    result = host_run('which yt-dlp')
    return result.returncode == 0

def search_youtube(query, limit=20):
    """Search YouTube for music videos."""
    try:
        cmd = f'yt-dlp --ignore-config --dump-json --flat-playlist --playlist-end {limit} {q(f"ytsearch{limit}:{query}")}'
        result = host_run(cmd, timeout=60)
        
        if result.returncode != 0:
            return []
        
        # Parse line-delimited JSON
        videos = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            try:
                data = json.loads(line)
                videos.append({
                    'id': data.get('id', ''),
                    'title': data.get('title', ''),
                    'url': f"https://youtube.com/watch?v={data.get('id', '')}",
                    'thumbnail': data.get('thumbnails', [{}])[0].get('url', ''),
                    'duration': data.get('duration', 0),
                    'channel': data.get('uploader', '')
                })
            except:
                continue
        
        return videos
    except Exception as e:
        print(f'YouTube search error: {e}')
        return []

def extract_audio_url(video_url):
    """Extract direct audio stream URL from YouTube video."""
    try:
        cmd = f'yt-dlp --ignore-config -f bestaudio[ext=webm]/bestaudio/best --get-url --no-playlist {q(video_url)}'
        result = host_run(cmd, timeout=30)
        
        if result.returncode != 0:
            return None
        
        url = result.stdout.strip()
        return url if url else None
    except Exception as e:
        print(f'Audio extraction error: {e}')
        return None

def get_video_metadata(video_url):
    """Get video metadata (title, artist, thumbnail)."""
    try:
        cmd = f'yt-dlp --ignore-config --dump-json --no-playlist {q(video_url)}'
        result = host_run(cmd, timeout=30)
        
        if result.returncode != 0:
            return {}
        
        data = json.loads(result.stdout)
        return {
            'title': data.get('title', ''),
            'artist': data.get('uploader', ''),
            'thumbnail': data.get('thumbnail', ''),
            'duration': data.get('duration', 0)
        }
    except Exception as e:
        print(f'Metadata error: {e}')
        return {}

def proxy_audio_stream(audio_url):
    """Proxy audio stream with HTTP 206 range support for seeking."""
    try:
        # Get range header from client
        range_header = request.headers.get('Range')
        headers = {}
        if range_header:
            headers['Range'] = range_header
        
        # Stream from YouTube
        r = requests.get(audio_url, headers=headers, stream=True, timeout=30)
        
        # Build response headers
        response_headers = {
            'Content-Type': r.headers.get('Content-Type', 'audio/webm'),
            'Accept-Ranges': 'bytes'
        }
        
        if 'Content-Length' in r.headers:
            response_headers['Content-Length'] = r.headers['Content-Length']
        
        if 'Content-Range' in r.headers:
            response_headers['Content-Range'] = r.headers['Content-Range']
        
        # Stream response
        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        
        return Response(
            generate(),
            status=r.status_code,
            headers=response_headers
        )
    except Exception as e:
        print(f'Stream proxy error: {e}')
        return jsonify({'error': str(e)}), 500
