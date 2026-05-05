"""Local music library routes for the Radio & Music blueprint."""

import mimetypes
import os
import shutil
import subprocess

from flask import after_this_request, jsonify, request, Response, send_file

from host import safe_path
from blueprints.radio_music import (
    radio_music_bp, log,
    _probe_audio_cached, _ensure_meta_cache, _save_meta_cache,
    _music_folders_file, _default_music_dir, _get_music_folders,
    _get_audiobook_folders, _get_all_local_folders,
    _user_file, _load_json, _save_json, _AUDIO_EXTS,
)


# ── Music folders config (per-user) ─────────────────────────

@radio_music_bp.route('/local/folders', methods=['GET'])
def local_folders_list():
    folders = _get_music_folders()
    result = []
    for f in folders:
        result.append({
            'path': f,
            'exists': os.path.isdir(f),
            'removable': f != _default_music_dir(),
        })
    return jsonify({'items': result})


@radio_music_bp.route('/local/folders', methods=['POST'])
def local_folders_update():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get('action', 'add')
    folder = body.get('path', '').strip()
    if not folder:
        return jsonify({'error': 'Brak ścieżki.'}), 400
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return jsonify({'error': 'Folder nie istnieje.'}), 400

    folders = _load_json(_music_folders_file(), [])
    home_music = _default_music_dir()
    if action == 'add':
        if folder not in folders and folder != home_music:
            folders.append(folder)
    elif action == 'remove':
        if folder == home_music:
            return jsonify({'error': 'Nie można usunąć domyślnego folderu muzyki.'}), 400
        folders = [f for f in folders if f != folder]
    _save_json(_music_folders_file(), folders)
    return jsonify({'ok': True})


@radio_music_bp.route('/local/scan', methods=['GET'])
def local_scan():
    """Scan configured music/audiobook folders for audio files with metadata.
    ?scope=audiobooks → scan Audiobooks folder; default → Music folders."""
    scope = request.args.get('scope', 'music').strip()
    if scope == 'audiobooks':
        folders = _get_audiobook_folders()
    else:
        folders = _get_music_folders()
    _ensure_meta_cache()
    items = []
    for base in folders:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _AUDIO_EXTS:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    stat = os.stat(fpath)
                except OSError:
                    continue
                rel = os.path.relpath(fpath, base)
                meta = _probe_audio_cached(fpath, stat.st_mtime)
                display_name = meta.get('title') or os.path.splitext(fname)[0]
                items.append({
                    'name': display_name,
                    'artist': meta.get('artist', ''),
                    'album': meta.get('album', ''),
                    'genre': meta.get('genre', ''),
                    'year': meta.get('year', ''),
                    'track': meta.get('track', ''),
                    'duration': meta.get('duration', 0),
                    'has_art': meta.get('has_art', False),
                    'filename': fname,
                    'path': fpath,
                    'folder': base,
                    'relative': rel,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'type': 'local',
                })
    _save_meta_cache()
    items.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify({'items': items, 'folders': folders})


@radio_music_bp.route('/local/file', methods=['DELETE'])
def local_delete_file():
    """Delete a single local audio file."""
    body = request.get_json(force=True, silent=True) or {}
    fpath = (body.get('path') or '').strip()
    if not fpath:
        return jsonify({'error': 'Brak ścieżki'}), 400
    # Validate path is within a configured music folder
    music_folders = _get_music_folders() + _get_audiobook_folders()
    try:
        resolved = safe_path(fpath, '/')
    except ValueError:
        return jsonify({'error': 'Nieprawidłowa ścieżka'}), 400
    if not any(resolved.startswith(os.path.realpath(f) + os.sep) or resolved == os.path.realpath(f)
               for f in music_folders):
        return jsonify({'error': 'Plik poza folderem muzyki'}), 403
    if not os.path.isfile(resolved):
        return jsonify({'error': 'Plik nie istnieje'}), 404
    try:
        os.remove(resolved)
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@radio_music_bp.route('/local/folder', methods=['DELETE'])
def local_delete_folder():
    """Delete a local folder and all its audio contents."""
    body = request.get_json(force=True, silent=True) or {}
    fpath = (body.get('path') or '').strip()
    if not fpath:
        return jsonify({'error': 'Brak ścieżki'}), 400
    music_folders = _get_music_folders() + _get_audiobook_folders()
    try:
        resolved = safe_path(fpath, '/')
    except ValueError:
        return jsonify({'error': 'Nieprawidłowa ścieżka'}), 400
    real_music_folders = [os.path.realpath(f) for f in music_folders]
    # Must be within (but not equal to) a configured music folder
    if not any(resolved.startswith(rf + os.sep) for rf in real_music_folders):
        return jsonify({'error': 'Folder poza folderem muzyki lub jest głównym folderem'}), 403
    if not os.path.isdir(resolved):
        return jsonify({'error': 'Folder nie istnieje'}), 404
    try:
        shutil.rmtree(resolved)
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@radio_music_bp.route('/local/stream', methods=['GET'])
def local_stream():
    """Stream a local audio file."""
    fpath = request.args.get('path', '').lstrip()
    if not fpath:
        return jsonify({'error': 'Brak ścieżki'}), 400

    # Validate path is within one of the configured music or audiobook folders
    fpath = os.path.realpath(fpath)
    folders = _get_all_local_folders()
    allowed = False
    for base in folders:
        try:
            if fpath.startswith(os.path.realpath(base) + os.sep):
                allowed = True
                break
        except Exception:
            continue
    if not allowed:
        return jsonify({'error': 'Ścieżka poza dozwolonymi folderami'}), 403
    if not os.path.isfile(fpath):
        return jsonify({'error': 'Plik nie istnieje'}), 404

    mime = mimetypes.guess_type(fpath)[0] or 'audio/mpeg'
    @after_this_request
    def _add_cors(resp):
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Range, Authorization'
        return resp
    return send_file(fpath, mimetype=mime, conditional=True)


@radio_music_bp.route('/local/artwork', methods=['GET'])
def local_artwork():
    """Extract embedded cover art from an audio file via ffmpeg."""
    fpath = request.args.get('path', '').lstrip()
    if not fpath:
        return jsonify({'error': 'Brak ścieżki'}), 400

    fpath = os.path.realpath(fpath)
    folders = _get_all_local_folders()
    allowed = False
    for base in folders:
        try:
            if fpath.startswith(os.path.realpath(base) + os.sep):
                allowed = True
                break
        except Exception:
            continue
    if not allowed:
        return jsonify({'error': 'Ścieżka poza dozwolonymi folderami'}), 403
    if not os.path.isfile(fpath):
        return jsonify({'error': 'Plik nie istnieje'}), 404

    try:
        r = subprocess.run(
            ['ffmpeg', '-i', fpath, '-an', '-vcodec', 'copy', '-f', 'image2pipe', '-'],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0 or not r.stdout:
            return Response(b'', status=204)
    except Exception:
        return Response(b'', status=204)

    # Detect MIME from magic bytes
    hdr = r.stdout[:4]
    if hdr[:2] == b'\xff\xd8':
        mime = 'image/jpeg'
    elif hdr[:4] == b'\x89PNG':
        mime = 'image/png'
    elif hdr[:4] == b'RIFF':
        mime = 'image/webp'
    else:
        mime = 'image/jpeg'

    return Response(r.stdout, mimetype=mime, headers={
        'Cache-Control': 'public, max-age=86400',
        'Access-Control-Allow-Origin': '*',
    })
