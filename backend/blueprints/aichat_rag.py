"""
EthOS AI Chat — RAG (Retrieval-Augmented Generation)
Extracts RAG-specific routes and helpers from aichat.py.
"""

import json
import os
import subprocess
import sys
import time

from flask import Blueprint, request, jsonify

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path

# Import from main module at runtime to avoid circular imports
def _get_aichat_module():
    """Get the main aichat module at runtime."""
    return sys.modules.get('blueprints.aichat')


# ── Scheduler helpers ──────────────────────────────────────────

_TIMER_UNIT = 'rag_index_cron.timer'
_TIMER_FILE = '/etc/systemd/system/rag_index_cron.timer'


def _scheduler_status():
    """Get systemd timer status."""
    try:
        active = subprocess.run(
            ['systemctl', 'is-active', _TIMER_UNIT],
            capture_output=True, text=True, timeout=5
        ).stdout.strip() == 'active'

        enabled = subprocess.run(
            ['systemctl', 'is-enabled', _TIMER_UNIT],
            capture_output=True, text=True, timeout=5
        ).stdout.strip() == 'enabled'

        # Read interval from timer file
        interval = 'hourly'
        if os.path.isfile(_TIMER_FILE):
            with open(_TIMER_FILE) as f:
                for line in f:
                    if line.strip().startswith('OnCalendar='):
                        interval = line.strip().split('=', 1)[1]
                        break

        return {'active': active, 'enabled': enabled, 'interval': interval}
    except Exception:
        return {'active': False, 'enabled': False, 'interval': 'hourly'}


def register_rag_routes(blueprint):
    """Register RAG routes on the given blueprint."""
    
    @blueprint.route('/rag/status', methods=['GET'])
    def rag_status():
        """Get RAG index status for current user."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _get_rag = aichat._get_rag
        _get_username = aichat._get_username
        _user_sandbox_root = aichat._user_sandbox_root
        _load_config = aichat._load_config

        username = _get_username()
        sandbox = _user_sandbox_root()
        indexer = _get_rag(username, sandbox)
        status = indexer.get_status()
        cfg = _load_config(username)
        status['rag_enabled'] = cfg.get('rag_enabled', True)
        if hasattr(indexer, '_progress') and indexer._progress:
            status['progress'] = dict(indexer._progress)
        else:
            status['progress'] = None
        status['scheduler'] = _scheduler_status()
        return jsonify(status)

    @blueprint.route('/rag/index', methods=['POST'])
    def rag_index():
        """Start indexing a directory. Runs in background thread."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_authenticated = aichat._is_authenticated
        _get_rag = aichat._get_rag
        _get_username = aichat._get_username
        _user_sandbox_root = aichat._user_sandbox_root
        _path_in_sandbox = aichat._path_in_sandbox
        _HAS_GEVENT = aichat._HAS_GEVENT
        _gevent = aichat._gevent
        _threading = aichat._threading

        if not _is_authenticated():
            return jsonify({'error': 'Permission denied'}), 403

        username = _get_username()
        sandbox = _user_sandbox_root()
        body = request.get_json(silent=True) or {}
        directory = body.get('directory', '').strip()

        if not directory:
            directory = sandbox or f'/home/{username}'

        if not os.path.isdir(directory):
            return jsonify({'error': 'Specified path is not a directory'}), 400
        ok, err = _path_in_sandbox(directory, sandbox)
        if not ok:
            return jsonify({'error': err}), 403

        indexer = _get_rag(username, sandbox)
        if indexer._indexing:
            return jsonify({'error': 'Indexing already in progress'}), 409

        sio = None
        try:
            from flask import current_app
            sio = current_app.extensions.get('socketio')
        except Exception:
            pass

        def _bg_index():
            indexer.index_directory(directory, recursive=True, socketio=sio)

        if _HAS_GEVENT:
            _gevent.spawn(_bg_index)
        else:
            _threading.Thread(target=_bg_index, daemon=True).start()
        return jsonify({'status': 'ok', 'directory': directory})

    @blueprint.route('/rag/index-internal', methods=['POST'])
    def rag_index_internal():
        """Internal endpoint for cron/systemd — localhost only, no session auth."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _get_rag = aichat._get_rag
        _path_in_sandbox = aichat._path_in_sandbox
        _HAS_GEVENT = aichat._HAS_GEVENT
        _gevent = aichat._gevent
        _threading = aichat._threading

        remote = request.remote_addr or ''
        if remote not in ('127.0.0.1', '::1', 'localhost'):
            return jsonify({'error': 'Localhost only'}), 403

        body = request.get_json(silent=True) or {}
        username = body.get('username', '').strip()
        if not username:
            return jsonify({'error': 'Username required'}), 400
        directory = body.get('directory', '').strip() or f'/home/{username}'

        sandbox = f'/home/{username}'

        if not os.path.isdir(directory):
            return jsonify({'error': 'Specified path is not a directory'}), 400
        ok, err = _path_in_sandbox(directory, sandbox)
        if not ok:
            return jsonify({'error': err}), 403

        indexer = _get_rag(username, sandbox)
        if indexer._indexing:
            return jsonify({'error': 'Indexing already in progress'}), 409

        def _bg_index():
            indexer.index_directory(directory, recursive=True)

        if _HAS_GEVENT:
            _gevent.spawn(_bg_index)
        else:
            _threading.Thread(target=_bg_index, daemon=True).start()
        return jsonify({'status': 'ok', 'directory': directory})

    @blueprint.route('/rag/search', methods=['POST'])
    def rag_search():
        """Manual RAG search. Returns matching chunks with scores."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_authenticated = aichat._is_authenticated
        _get_rag = aichat._get_rag
        _get_username = aichat._get_username
        _user_sandbox_root = aichat._user_sandbox_root

        if not _is_authenticated():
            return jsonify({'error': 'Permission denied'}), 403

        username = _get_username()
        sandbox = _user_sandbox_root()
        body = request.get_json(silent=True) or {}
        query = body.get('query', '').strip()
        index_type = body.get('index_type')
        top_k = min(int(body.get('top_k', 5)), 20)

        if not query:
            return jsonify({'error': 'Query required'}), 400

        indexer = _get_rag(username, sandbox)
        results = indexer.search(query, index_type=index_type, top_k=top_k)
        return jsonify({'results': results, 'query': query, 'index_type': index_type})

    @blueprint.route('/rag/clear', methods=['POST'])
    def rag_clear():
        """Clear the RAG index for current user."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_authenticated = aichat._is_authenticated
        _get_rag = aichat._get_rag
        _get_username = aichat._get_username
        _user_sandbox_root = aichat._user_sandbox_root

        if not _is_authenticated():
            return jsonify({'error': 'Permission denied'}), 403

        username = _get_username()
        sandbox = _user_sandbox_root()
        indexer = _get_rag(username, sandbox)
        indexer.clear()
        return jsonify({'status': 'ok'})

    @blueprint.route('/rag/scheduler', methods=['GET'])
    def rag_scheduler_status():
        """Get scheduler status."""
        return jsonify(_scheduler_status())

    @blueprint.route('/rag/scheduler', methods=['POST'])
    def rag_scheduler_toggle():
        """Enable/disable scheduler, change interval. Admin only."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        import re

        if not _is_admin():
            return jsonify({'error': 'Admin only'}), 403

        body = request.get_json(silent=True) or {}
        action = body.get('action', '').strip()
        interval = body.get('interval', '').strip()

        _VALID_INTERVALS = {
            'hourly': 'hourly',
            'daily': 'daily',
            'every_6h': '*-*-* 0/6:00:00',
            'every_12h': '*-*-* 0/12:00:00',
            'every_30min': '*:0/30',
        }

        try:
            if action == 'enable':
                subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', 'enable', '--now', _TIMER_UNIT],
                               capture_output=True, timeout=10)
                return jsonify({'status': 'ok'})

            elif action == 'disable':
                subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', 'disable', '--now', _TIMER_UNIT],
                               capture_output=True, timeout=10)
                return jsonify({'status': 'ok'})

            elif action == 'set_interval':
                cal_value = _VALID_INTERVALS.get(interval, interval)
                if not cal_value:
                    return jsonify({'error': 'Interval required'}), 400

                if not re.match(r'^[a-zA-Z0-9\s*/:,\-]+$', cal_value):
                    return jsonify({'error': 'Invalid interval format (allowed: a-z 0-9 * / : - , spaces)'}), 400

                timer_content = (
                    '[Unit]\n'
                    'Description=Periodic RAG index update for EthOS\n'
                    '\n'
                    '[Timer]\n'
                    f'OnCalendar={cal_value}\n'
                    'Persistent=true\n'
                    '\n'
                    '[Install]\n'
                    'WantedBy=timers.target\n'
                )
                import tempfile
                tmp = tempfile.NamedTemporaryFile('w', suffix='.timer', delete=False)
                tmp.write(timer_content)
                tmp.close()
                subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'copy-timer', tmp.name],
                               capture_output=True, timeout=10)
                os.unlink(tmp.name)

                subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', 'daemon-reload'], capture_output=True, timeout=10)
                subprocess.run(['sudo', '/opt/ethos/tools/ethos-system-helper.sh', 'systemctl', 'restart', _TIMER_UNIT], capture_output=True, timeout=10)
                return jsonify({'status': 'ok', 'interval': cal_value})

            else:
                return jsonify({'error': 'Unknown action'}), 400

        except Exception as e:
            return jsonify({'error': str(e)}), 500
