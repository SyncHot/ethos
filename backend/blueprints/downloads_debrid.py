"""Download Manager — Debrid service test routes."""

import urllib.parse

from flask import request, jsonify

from blueprints.downloads import (
    downloads_bp,
    _load_config, _http_get_json,
)


@downloads_bp.route('/api/downloads/test-debrid', methods=['POST'])
def test_debrid():
    """Test debrid API key by checking account info."""
    data = request.get_json(force=True)
    service = data.get('service', '')
    api_key = data.get('api_key', '')

    if not api_key:
        return jsonify({'ok': False, 'error': 'API key missing'})

    return _do_test_debrid(service, api_key)


@downloads_bp.route('/api/downloads/test-saved-debrid', methods=['POST'])
def test_saved_debrid():
    """Test the already-saved debrid API key."""
    data = request.get_json(force=True)
    service = data.get('service', '')
    cfg = _load_config()
    api_key = cfg.get(f'{service}_api_key', '')
    if not api_key:
        return jsonify({'ok': False, 'error': 'No saved API key for this service'})
    return _do_test_debrid(service, api_key)


def _do_test_debrid(service, api_key):
    """Shared debrid test logic."""
    try:
        if service == 'alldebrid':
            result = _http_get_json(
                f"https://api.alldebrid.com/v4/user?agent=EthOS&apikey={urllib.parse.quote(api_key)}"
            )
            if result.get('status') == 'success':
                user = result.get('data', {}).get('user', {})
                return jsonify({'ok': True, 'info': f"User: {user.get('username', '?')}, Premium: {'Yes' if user.get('isPremium') else 'No'}"})
            return jsonify({'ok': False, 'error': result.get('error', {}).get('message', 'Error')})

        elif service == 'realdebrid':
            result = _http_get_json(
                "https://api.real-debrid.com/rest/1.0/user",
                headers={'Authorization': f'Bearer {api_key}'}
            )
            if result.get('username'):
                prem = 'Yes' if result.get('premium', 0) > 0 else 'No'
                return jsonify({'ok': True, 'info': f"User: {result['username']}, Premium: {prem}"})
            return jsonify({'ok': False, 'error': 'Invalid key'})

        elif service == 'premiumize':
            result = _http_get_json(
                f"https://www.premiumize.me/api/account/info?apikey={urllib.parse.quote(api_key)}"
            )
            if result.get('status') == 'success':
                return jsonify({'ok': True, 'info': f"User: {result.get('customer_id', '?')}, Premium: {'Yes' if result.get('premium_until') else 'No'}"})
            return jsonify({'ok': False, 'error': result.get('message', 'Error')})


        elif service == 'debridlink':
            result = _http_get_json(
                "https://debrid-link.com/api/v2/account/infos",
                headers={'Authorization': f'Bearer {api_key}'}
            )
            if result.get('success') and result.get('value'):
                val = result['value']
                prem = 'Yes' if val.get('premiumLeft', 0) > 0 else 'No'
                return jsonify({'ok': True, 'info': f"User: {val.get('pseudo', '?')}, Premium: {prem}"})
            return jsonify({'ok': False, 'error': result.get('error', 'Invalid key')})

        elif service == 'torbox':
            result = _http_get_json(
                "https://api.torbox.app/v1/api/user/me",
                headers={'Authorization': f'Bearer {api_key}'}
            )
            if result.get('success') and result.get('data'):
                d = result['data']
                prem = 'Yes' if d.get('plan', 0) > 0 else 'No'
                return jsonify({'ok': True, 'info': f"User: {d.get('email', '?')}, Premium: {prem}"})
            return jsonify({'ok': False, 'error': result.get('detail', 'Invalid key')})

        return jsonify({'ok': False, 'error': 'Unknown service'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]})
