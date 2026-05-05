"""
EthOS — File Sharing Blueprint (Public Links)
Manage share links: create, list, delete. Public access to shared files.
Extracted from app.py for clean separation.
"""
import os
import secrets
import io
import zipfile as _zf
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, send_file, send_from_directory, g
from werkzeug.security import generate_password_hash, check_password_hash
from host import data_path
from utils import load_json, save_json, DATA_ROOT, generate_thumbnail
from audit import audit_log

sharing_bp = Blueprint('sharing', __name__)

# ── Data ──

SHARES_FILE = data_path('shares.json')


def _load_shares():
    return load_json(SHARES_FILE, [])


def _save_shares(shares):
    save_json(SHARES_FILE, shares)


def _find_share(token):
    """Find a share by token. Returns None if expired or not found."""
    shares = _load_shares()
    for s in shares:
        if s['token'] == token:
            if s.get('expires'):
                try:
                    if datetime.fromisoformat(s['expires']) < datetime.now():
                        return None
                except Exception:
                    pass
            return s
    return None


def _share_real_path(share, sub=''):
    """Get the real filesystem path for a share + optional subpath."""
    base = os.path.realpath(DATA_ROOT)
    share_root = os.path.realpath(os.path.join(base, share['path'].lstrip('/')))
    if not share_root.startswith(base):
        return None
    if sub:
        target = os.path.realpath(os.path.join(share_root, sub.lstrip('/')))
        if not target.startswith(share_root):
            return None
        return target
    return share_root


def _verify_shared_with_auth(share):
    """Verify auth for shares restricted to specific users (shared_with).
    Returns (ok: bool, error_response | None).
    Shares without shared_with are public — always returns (True, None)."""
    if not share.get('shared_with'):
        return True, None

    auth_token = request.headers.get('Authorization', '')
    if auth_token.startswith('Bearer '):
        auth_token = auth_token[7:]
    else:
        auth_token = request.args.get('token', '')

    # Lazy import to avoid circular dependency — tokens dict lives in app.py
    from app import tokens
    tinfo = tokens.get(auth_token)
    if not tinfo or tinfo['expires'] < datetime.now():
        return False, (jsonify({'error': 'Login required'}), 401)
    if tinfo['username'] not in share['shared_with'] and tinfo['username'] != share.get('creator', ''):
        return False, (jsonify({'error': 'No access to this share'}), 403)
    return True, None


def _verify_share_password(share):
    """Verify password for password-protected shares.
    Returns (ok: bool, error_response | None).
    If share has no password, returns (True, None) immediately."""
    pw_hash = share.get('password')
    if not pw_hash:
        return True, None

    # Accept password via query param or Authorization header
    pw = request.args.get('password', '')
    if not pw:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            pw = auth[7:]

    if not pw or not check_password_hash(pw_hash, pw):
        return False, (jsonify({'error': 'Password required', 'password_protected': True}), 401)
    return True, None


# ── Authenticated share management (under /api/files/shares) ──
# These routes are protected by _blueprint_auth_guard via /api/files/ prefix.

def _sanitize_share(s):
    """Strip password hash, add has_password boolean for the frontend."""
    out = {k: v for k, v in s.items() if k != 'password'}
    out['has_password'] = bool(s.get('password'))
    return out


@sharing_bp.route('/api/files/shares')
def list_shares():
    me = g.username
    shares = _load_shares()
    if me:
        shares = [s for s in shares if s.get('creator', '') == me or not s.get('creator')]
    return jsonify([_sanitize_share(s) for s in shares])


@sharing_bp.route('/api/files/shares/received')
def received_shares():
    """List shares that other users have shared with the current user."""
    me = g.username
    if not me:
        return jsonify([]), 200
    shares = _load_shares()
    now = datetime.now()
    result = []
    for s in shares:
        if me not in s.get('shared_with', []):
            continue
        if s.get('creator', '') == me:
            continue
        if s.get('expires'):
            try:
                if datetime.fromisoformat(s['expires']) < now:
                    continue
            except Exception:
                pass
        result.append(_sanitize_share(s))
    return jsonify(result)


@sharing_bp.route('/api/files/shares', methods=['POST'])
def create_share():
    data = request.json or {}
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': 'path required'}), 400

    # safe_path with auth context — import from app to get the wrapper
    from app import safe_path
    real = safe_path(path)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'Path not found'}), 404

    is_dir = os.path.isdir(real)
    name = os.path.basename(real) or path
    token = secrets.token_urlsafe(24)

    expires_hours = data.get('expires_hours', 0)
    expires_iso = None
    if expires_hours and int(expires_hours) > 0:
        expires_iso = (datetime.now() + timedelta(hours=int(expires_hours))).isoformat()

    shared_with = data.get('shared_with', [])
    if isinstance(shared_with, list):
        shared_with = [u.strip() for u in shared_with if isinstance(u, str) and u.strip()]
    else:
        shared_with = []

    password = data.get('password', '').strip()
    password_hash = generate_password_hash(password) if password else None

    share = {
        'token': token,
        'path': path,
        'name': name,
        'is_dir': is_dir,
        'created': datetime.now().isoformat(),
        'expires': expires_iso,
        'creator': g.username or '',
        'shared_with': shared_with,
        'password': password_hash,
    }

    shares = _load_shares()
    shares.append(share)
    _save_shares(shares)

    audit_log('file.share.create', f'Shared "{path}" (token: {token})')
    return jsonify(_sanitize_share(share))


@sharing_bp.route('/api/files/shares/<token>', methods=['DELETE'])
def delete_share(token):
    me = g.username
    role = g.role
    shares = _load_shares()
    target = next((s for s in shares if s['token'] == token), None)
    if not target:
        return jsonify({'error': 'Share not found'}), 404
    if me and target.get('creator') and target['creator'] != me and role != 'admin':
        return jsonify({'error': 'Permission denied'}), 403
    new = [s for s in shares if s['token'] != token]
    _save_shares(new)
    audit_log('file.share.delete', f'Unshared "{target.get("path", "")}" (token: {token})')
    return jsonify({'ok': True})


# ── Public share access (NO AUTH — not under /api/ guard prefix) ──

@sharing_bp.route('/api/public/share/<token>')
def public_share_info(token):
    """Get share info + file listing for directory shares."""
    share = _find_share(token)
    if not share:
        return jsonify({'error': 'Link expired or not found'}), 404

    ok, err = _verify_shared_with_auth(share)
    if not ok:
        return err

    ok, err = _verify_share_password(share)
    if not ok:
        return err

    sub = request.args.get('path', '')
    real = _share_real_path(share, sub)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'Not found'}), 404

    if os.path.isfile(real):
        return jsonify({
            'share': {'name': share['name'], 'is_dir': False, 'token': token},
            'items': [],
            'path': sub or '/',
        })

    # Directory listing
    items = []
    try:
        for entry in sorted(os.scandir(real), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat(follow_symlinks=False)
                items.append({
                    'name': entry.name,
                    'is_dir': entry.is_dir(),
                    'size': stat.st_size if not entry.is_dir() else 0,
                    'modified': stat.st_mtime,
                })
            except (PermissionError, OSError):
                pass
    except PermissionError:
        return jsonify({'error': 'Permission denied'}), 403

    return jsonify({
        'share': {'name': share['name'], 'is_dir': True, 'token': token},
        'items': items,
        'path': sub or '/',
    })


@sharing_bp.route('/api/public/share/<token>/download')
def public_share_download(token):
    """Download a file from a share (no auth for public, auth for user-targeted)."""
    share = _find_share(token)
    if not share:
        return jsonify({'error': 'Link expired or not found'}), 404

    ok, err = _verify_shared_with_auth(share)
    if not ok:
        return err

    ok, err = _verify_share_password(share)
    if not ok:
        return err

    sub = request.args.get('path', '')
    real = _share_real_path(share, sub)
    if not real or not os.path.exists(real):
        return jsonify({'error': 'Not found'}), 404

    if os.path.isfile(real):
        return send_file(real, as_attachment=True)

    # Directory — stream as ZIP (limit 2 GB to prevent OOM)
    _MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
    total_size = 0
    for root, dirs, files in os.walk(real):
        for fname in files:
            try:
                total_size += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
            if total_size > _MAX_ZIP_BYTES:
                return jsonify({'error': 'Folder too large to download as ZIP (2 GB limit)'}), 400
    buf = io.BytesIO()
    with _zf.ZipFile(buf, 'w', _zf.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(real):
            for fname in files:
                fpath = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, real)
                try:
                    zf.write(fpath, arcname)
                except Exception:
                    pass
    buf.seek(0)
    dl_name = os.path.basename(real) + '.zip'
    return send_file(buf, as_attachment=True, download_name=dl_name, mimetype='application/zip')


@sharing_bp.route('/api/public/share/<token>/preview')
def public_share_preview(token):
    """Preview / stream a file from a share. Supports thumbnails and Range."""
    share = _find_share(token)
    if not share:
        return jsonify({'error': 'Link expired or not found'}), 404

    ok, err = _verify_shared_with_auth(share)
    if not ok:
        return err

    ok, err = _verify_share_password(share)
    if not ok:
        return err

    sub = request.args.get('path', '')
    real = _share_real_path(share, sub)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'Not found'}), 404

    w = request.args.get('w', type=int)
    h = request.args.get('h', type=int)

    if w and h:
        return generate_thumbnail(real, w, h)
    return send_file(real)


@sharing_bp.route('/share/<token>')
def public_share_page(token):
    """Serve a self-contained HTML page for browsing a public share."""
    from flask import current_app
    share = _find_share(token)
    if not share:
        return '<h2 style="font-family:sans-serif;color:#888;text-align:center;margin-top:80px">Link expired or not found</h2>', 404
    return send_from_directory(current_app.static_folder, 'share.html')
