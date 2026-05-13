"""
Radio & Music v2 — Main blueprint.
Lightweight rewrite: radio, podcasts, YouTube streaming, playlists, history.

Routes:
  GET  /api/radio-music-v2/radio/search       - search stations
  GET  /api/radio-music-v2/radio/top          - top stations
  GET  /api/radio-music-v2/radio/countries    - list countries
  GET  /api/radio-music-v2/radio/tags         - genre tags
  GET  /api/radio-music-v2/yt/check           - check yt-dlp
  GET  /api/radio-music-v2/yt/search          - search YouTube
  GET  /api/radio-music-v2/yt/stream          - stream audio
  GET  /api/radio-music-v2/yt/metadata        - get video metadata
  GET  /api/radio-music-v2/podcasts/search    - search podcasts
  GET  /api/radio-music-v2/podcasts/feed      - parse RSS feed
  GET  /api/radio-music-v2/playlists          - list playlists
  POST /api/radio-music-v2/playlists          - create playlist
  GET  /api/radio-music-v2/playlists/<id>     - get playlist
  PUT  /api/radio-music-v2/playlists/<id>     - update playlist
  DELETE /api/radio-music-v2/playlists/<id>   - delete playlist
  POST /api/radio-music-v2/playlists/<id>/tracks     - add track
  DELETE /api/radio-music-v2/playlists/<id>/tracks/<i> - remove track
  GET  /api/radio-music-v2/playlists/<id>/export - export M3U8
  GET  /api/radio-music-v2/favorites          - get favorites
  POST /api/radio-music-v2/favorites          - add/remove favorite
  GET  /api/radio-music-v2/history            - get history
  POST /api/radio-music-v2/history            - add to history
"""
import os
import sys
import json
import time
from flask import Blueprint, request, jsonify, Response

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from blueprints.admin_required import admin_required
from i18n import t

radio_music_v2_bp = Blueprint('radio_music_v2', __name__, url_prefix='/api/radio-music-v2')

# ── Radio Browser API ──────────────────────────────────────────────────

@radio_music_v2_bp.route('/radio/search', methods=['GET'])
def radio_search():
    """Search radio stations."""
    from blueprints.radio_music_v2_radio import search_stations
    
    query = request.args.get('q', '')
    country = request.args.get('country', '')
    tag = request.args.get('tag', '')
    limit = int(request.args.get('limit', 50))
    
    stations = search_stations(query, country, tag, limit)
    return jsonify(stations)

@radio_music_v2_bp.route('/radio/top', methods=['GET'])
def radio_top():
    """Get top voted stations."""
    from blueprints.radio_music_v2_radio import get_top_stations
    
    limit = int(request.args.get('limit', 50))
    stations = get_top_stations(limit)
    return jsonify(stations)

@radio_music_v2_bp.route('/radio/countries', methods=['GET'])
def radio_countries():
    """List countries with station counts."""
    from blueprints.radio_music_v2_radio import get_countries
    
    countries = get_countries()
    return jsonify(countries)

@radio_music_v2_bp.route('/radio/tags', methods=['GET'])
def radio_tags():
    """Get popular genre tags."""
    from blueprints.radio_music_v2_radio import get_tags
    
    tags = get_tags()
    return jsonify(tags)

# ── YouTube ────────────────────────────────────────────────────────────

@radio_music_v2_bp.route('/yt/check', methods=['GET'])
def yt_check():
    """Check if yt-dlp is installed."""
    from blueprints.radio_music_v2_youtube import check_ytdlp
    
    installed = check_ytdlp()
    return jsonify({'installed': installed})

@radio_music_v2_bp.route('/yt/search', methods=['GET'])
def yt_search():
    """Search YouTube for music."""
    from blueprints.radio_music_v2_youtube import search_youtube
    
    query = request.args.get('q', '')
    limit = int(request.args.get('limit', 20))
    
    if not query:
        return jsonify({'error': 'Missing query'}), 400
    
    videos = search_youtube(query, limit)
    return jsonify(videos)

@radio_music_v2_bp.route('/yt/stream', methods=['GET'])
def yt_stream():
    """Stream YouTube audio with HTTP 206 range support."""
    from blueprints.radio_music_v2_youtube import extract_audio_url, proxy_audio_stream
    
    video_url = request.args.get('url', '')
    if not video_url:
        return jsonify({'error': 'Missing url'}), 400
    
    # Extract audio stream URL
    audio_url = extract_audio_url(video_url)
    if not audio_url:
        return jsonify({'error': 'Failed to extract audio'}), 500
    
    # Proxy stream
    return proxy_audio_stream(audio_url)

@radio_music_v2_bp.route('/yt/metadata', methods=['GET'])
def yt_metadata():
    """Get video metadata."""
    from blueprints.radio_music_v2_youtube import get_video_metadata
    
    video_url = request.args.get('url', '')
    if not video_url:
        return jsonify({'error': 'Missing url'}), 400
    
    metadata = get_video_metadata(video_url)
    return jsonify(metadata)

# ── Podcasts ───────────────────────────────────────────────────────────

@radio_music_v2_bp.route('/podcasts/search', methods=['GET'])
def podcasts_search():
    """Search podcasts via iTunes."""
    from blueprints.radio_music_v2_podcasts import search_podcasts
    
    query = request.args.get('q', '')
    limit = int(request.args.get('limit', 20))
    
    if not query:
        return jsonify({'error': 'Missing query'}), 400
    
    podcasts = search_podcasts(query, limit)
    return jsonify(podcasts)

@radio_music_v2_bp.route('/podcasts/feed', methods=['GET'])
def podcasts_feed():
    """Parse podcast RSS feed."""
    from blueprints.radio_music_v2_podcasts import parse_podcast_feed
    
    feed_url = request.args.get('url', '')
    limit = int(request.args.get('limit', 50))
    
    if not feed_url:
        return jsonify({'error': 'Missing url'}), 400
    
    result = parse_podcast_feed(feed_url, limit)
    return jsonify(result)

# ── Playlists ──────────────────────────────────────────────────────────

@radio_music_v2_bp.route('/playlists', methods=['GET'])
def playlists_list():
    """Get all playlists."""
    from blueprints.radio_music_v2_playlist import get_playlists
    
    playlists = get_playlists()
    return jsonify(playlists)

@radio_music_v2_bp.route('/playlists', methods=['POST'])
def playlists_create():
    """Create new playlist."""
    from blueprints.radio_music_v2_playlist import create_playlist
    
    data = request.get_json()
    name = data.get('name', 'New Playlist')
    tracks = data.get('tracks', [])
    
    playlist_id = create_playlist(name, tracks)
    return jsonify({'id': playlist_id})

@radio_music_v2_bp.route('/playlists/<int:playlist_id>', methods=['GET'])
def playlists_get(playlist_id):
    """Get single playlist."""
    from blueprints.radio_music_v2_playlist import get_playlist
    
    playlist = get_playlist(playlist_id)
    if not playlist:
        return jsonify({'error': 'Playlist not found'}), 404
    
    return jsonify(playlist)

@radio_music_v2_bp.route('/playlists/<int:playlist_id>', methods=['PUT'])
def playlists_update(playlist_id):
    """Update playlist."""
    from blueprints.radio_music_v2_playlist import update_playlist
    
    data = request.get_json()
    name = data.get('name')
    tracks = data.get('tracks')
    
    update_playlist(playlist_id, name, tracks)
    return jsonify({'success': True})

@radio_music_v2_bp.route('/playlists/<int:playlist_id>', methods=['DELETE'])
def playlists_delete(playlist_id):
    """Delete playlist."""
    from blueprints.radio_music_v2_playlist import delete_playlist
    
    delete_playlist(playlist_id)
    return jsonify({'success': True})

@radio_music_v2_bp.route('/playlists/<int:playlist_id>/tracks', methods=['POST'])
def playlists_add_track(playlist_id):
    """Add track to playlist."""
    from blueprints.radio_music_v2_playlist import add_track_to_playlist
    
    data = request.get_json()
    track = data.get('track')
    
    if not track:
        return jsonify({'error': 'Missing track'}), 400
    
    success = add_track_to_playlist(playlist_id, track)
    if not success:
        return jsonify({'error': 'Playlist not found'}), 404
    
    return jsonify({'success': True})

@radio_music_v2_bp.route('/playlists/<int:playlist_id>/tracks/<int:track_index>', methods=['DELETE'])
def playlists_remove_track(playlist_id, track_index):
    """Remove track from playlist."""
    from blueprints.radio_music_v2_playlist import remove_track_from_playlist
    
    success = remove_track_from_playlist(playlist_id, track_index)
    if not success:
        return jsonify({'error': 'Track not found'}), 404
    
    return jsonify({'success': True})

@radio_music_v2_bp.route('/playlists/<int:playlist_id>/export', methods=['GET'])
def playlists_export(playlist_id):
    """Export playlist as M3U8."""
    from blueprints.radio_music_v2_playlist import get_playlist, export_playlist_m3u
    
    playlist = get_playlist(playlist_id)
    if not playlist:
        return jsonify({'error': 'Playlist not found'}), 404
    
    m3u_content = export_playlist_m3u(playlist)
    
    return Response(
        m3u_content,
        mimetype='audio/x-mpegurl',
        headers={
            'Content-Disposition': f'attachment; filename="{playlist["name"]}.m3u8"'
        }
    )

# ── Favorites ──────────────────────────────────────────────────────────

@radio_music_v2_bp.route('/favorites', methods=['GET'])
def favorites_list():
    """Get all favorites."""
    from blueprints.radio_music_v2_schema import get_db
    
    fav_type = request.args.get('type', '')  # 'radio', 'podcast', or empty for all
    
    conn = get_db()
    cur = conn.cursor()
    
    if fav_type:
        rows = cur.execute(
            'SELECT * FROM favorites WHERE type = ? ORDER BY created_at DESC',
            (fav_type,)
        ).fetchall()
    else:
        rows = cur.execute('SELECT * FROM favorites ORDER BY created_at DESC').fetchall()
    
    conn.close()
    
    favorites = []
    for row in rows:
        favorites.append({
            'id': row['id'],
            'type': row['type'],
            'data': json.loads(row['data']),
            'created_at': row['created_at']
        })
    
    return jsonify(favorites)

@radio_music_v2_bp.route('/favorites', methods=['POST'])
def favorites_toggle():
    """Add or remove favorite."""
    from blueprints.radio_music_v2_schema import get_db
    
    data = request.get_json()
    action = data.get('action', 'add')  # 'add' or 'remove'
    fav_type = data.get('type')  # 'radio' or 'podcast'
    fav_data = data.get('data')
    
    if not fav_type or not fav_data:
        return jsonify({'error': 'Missing type or data'}), 400
    
    conn = get_db()
    cur = conn.cursor()
    
    if action == 'add':
        # Check if already exists (by URL or ID)
        fav_data_str = json.dumps(fav_data)
        existing = cur.execute(
            'SELECT id FROM favorites WHERE type = ? AND data = ?',
            (fav_type, fav_data_str)
        ).fetchone()
        
        if not existing:
            cur.execute(
                'INSERT INTO favorites (type, data) VALUES (?, ?)',
                (fav_type, fav_data_str)
            )
            conn.commit()
    
    elif action == 'remove':
        fav_data_str = json.dumps(fav_data)
        cur.execute(
            'DELETE FROM favorites WHERE type = ? AND data = ?',
            (fav_type, fav_data_str)
        )
        conn.commit()
    
    conn.close()
    return jsonify({'success': True})

# ── History ────────────────────────────────────────────────────────────

@radio_music_v2_bp.route('/history', methods=['GET'])
def history_list():
    """Get playback history."""
    from blueprints.radio_music_v2_schema import get_db
    
    limit = int(request.args.get('limit', 50))
    
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute(
        'SELECT * FROM history ORDER BY played_at DESC LIMIT ?',
        (limit,)
    ).fetchall()
    conn.close()
    
    history = []
    for row in rows:
        history.append({
            'id': row['id'],
            'type': row['type'],
            'title': row['title'],
            'data': json.loads(row['data']),
            'played_at': row['played_at']
        })
    
    return jsonify(history)

@radio_music_v2_bp.route('/history', methods=['POST'])
def history_add():
    """Add item to history."""
    from blueprints.radio_music_v2_schema import get_db
    
    data = request.get_json()
    item_type = data.get('type')
    title = data.get('title', '')
    item_data = data.get('data')
    
    if not item_type or not item_data:
        return jsonify({'error': 'Missing type or data'}), 400
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO history (type, title, data) VALUES (?, ?, ?)',
        (item_type, title, json.dumps(item_data))
    )
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

# ── Bottom imports (avoid circular imports) ────────────────────────────
from blueprints import radio_music_v2_radio
from blueprints import radio_music_v2_youtube
from blueprints import radio_music_v2_podcasts
from blueprints import radio_music_v2_playlist
from blueprints import radio_music_v2_schema
