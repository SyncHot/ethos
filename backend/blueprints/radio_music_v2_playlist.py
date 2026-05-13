"""
Radio & Music v2 — Playlist management.
Provides: CRUD operations, M3U8 export
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from blueprints.radio_music_v2_schema import get_db

def get_playlists():
    """Get all user playlists."""
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute('SELECT * FROM playlists ORDER BY updated_at DESC').fetchall()
    conn.close()
    
    playlists = []
    for row in rows:
        playlists.append({
            'id': row['id'],
            'name': row['name'],
            'tracks': json.loads(row['tracks']),
            'created_at': row['created_at'],
            'updated_at': row['updated_at']
        })
    
    return playlists

def get_playlist(playlist_id):
    """Get single playlist by ID."""
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute('SELECT * FROM playlists WHERE id = ?', (playlist_id,)).fetchone()
    conn.close()
    
    if not row:
        return None
    
    return {
        'id': row['id'],
        'name': row['name'],
        'tracks': json.loads(row['tracks']),
        'created_at': row['created_at'],
        'updated_at': row['updated_at']
    }

def create_playlist(name, tracks=None):
    """Create new playlist."""
    if tracks is None:
        tracks = []
    
    conn = get_db()
    cur = conn.cursor()
    now = int(time.time())
    
    cur.execute(
        'INSERT INTO playlists (name, tracks, created_at, updated_at) VALUES (?, ?, ?, ?)',
        (name, json.dumps(tracks), now, now)
    )
    playlist_id = cur.lastrowid
    conn.commit()
    conn.close()
    
    return playlist_id

def update_playlist(playlist_id, name=None, tracks=None):
    """Update playlist name or tracks."""
    conn = get_db()
    cur = conn.cursor()
    now = int(time.time())
    
    if name is not None and tracks is not None:
        cur.execute(
            'UPDATE playlists SET name = ?, tracks = ?, updated_at = ? WHERE id = ?',
            (name, json.dumps(tracks), now, playlist_id)
        )
    elif name is not None:
        cur.execute(
            'UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?',
            (name, now, playlist_id)
        )
    elif tracks is not None:
        cur.execute(
            'UPDATE playlists SET tracks = ?, updated_at = ? WHERE id = ?',
            (json.dumps(tracks), now, playlist_id)
        )
    
    conn.commit()
    conn.close()

def delete_playlist(playlist_id):
    """Delete playlist."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
    conn.commit()
    conn.close()

def add_track_to_playlist(playlist_id, track):
    """Add track to playlist."""
    playlist = get_playlist(playlist_id)
    if not playlist:
        return False
    
    tracks = playlist['tracks']
    tracks.append(track)
    update_playlist(playlist_id, tracks=tracks)
    return True

def remove_track_from_playlist(playlist_id, track_index):
    """Remove track from playlist by index."""
    playlist = get_playlist(playlist_id)
    if not playlist:
        return False
    
    tracks = playlist['tracks']
    if 0 <= track_index < len(tracks):
        tracks.pop(track_index)
        update_playlist(playlist_id, tracks=tracks)
        return True
    
    return False

def export_playlist_m3u(playlist):
    """Export playlist as M3U8 format."""
    lines = ['#EXTM3U']
    
    for track in playlist['tracks']:
        # #EXTINF:duration,Artist - Title
        duration = track.get('duration', -1)
        title = track.get('title', 'Unknown')
        artist = track.get('artist', '')
        
        if artist:
            display = f'{artist} - {title}'
        else:
            display = title
        
        lines.append(f'#EXTINF:{duration},{display}')
        lines.append(track.get('url', ''))
    
    return '\n'.join(lines)
