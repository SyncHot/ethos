"""
EthOS AI Chat — Model Management
Extracts model catalog, download, installation, and calibration logic from aichat.py.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

from flask import request, jsonify

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path
from utils import load_json as _load_json, save_json as _save_json


# Import from main module at runtime to avoid circular imports
def _get_aichat_module():
    """Get the main aichat module at runtime."""
    return sys.modules.get('blueprints.aichat')


def _parse_params(params_str):
    """Parse '7B' → 7.0, '0.5B' → 0.5, '1.5B' → 1.5"""
    try:
        s = str(params_str).upper().replace('B', '').strip()
        return float(s)
    except (ValueError, TypeError):
        return 0


def _calibration_path(username='admin'):
    return data_path(f'aichat_calibration_{username}.json')


def _aichat_on_uninstall(wipe, wipe_models=False):
    """Clean up AI Chat: unload model, optionally remove configs and/or models."""
    from model_library import get_library as _get_ml

    try:
        lib = _get_ml()
        lib.unload_model()
    except Exception:
        pass

    if wipe:
        import glob
        for pattern in ('aichat_config_*.json', 'aichat_history_*.json', 'model_library.json'):
            for f in glob.glob(os.path.join(data_path(), pattern)):
                try:
                    os.remove(f)
                except Exception:
                    pass

    if wipe or wipe_models:
        try:
            lib = _get_ml()
            models_dir = lib.models_path
            if models_dir and os.path.isdir(models_dir):
                import shutil
                shutil.rmtree(models_dir, ignore_errors=True)
        except Exception:
            pass


def _check_llama_cpp():
    """Check if llama-cpp-python is importable."""
    try:
        import llama_cpp
        return True
    except ImportError:
        return False


def _check_hf_hub():
    """Check if huggingface_hub is importable."""
    try:
        import huggingface_hub  # noqa: F401
        return True
    except ImportError:
        return False


def _find_venv_pip():
    """Resolve pip path for the active EthOS runtime."""
    venv_pip = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'venv', 'bin', 'pip')
    if not os.path.isfile(venv_pip):
        venv_pip = os.path.join(os.environ.get('ETHOS_ROOT', '/opt/ethos'), 'venv', 'bin', 'pip')
    if not os.path.isfile(venv_pip):
        import shutil as _sh
        venv_pip = _sh.which('pip3') or _sh.which('pip') or 'pip'
    return venv_pip


def _install_py_pkg(pkg_name, timeout=900):
    """Install a Python package in the EthOS venv. Returns (ok, error_msg)."""
    try:
        pip_bin = _find_venv_pip()
        proc = subprocess.run(
            [pip_bin, 'install', '--no-cache-dir', pkg_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            tail = '\n'.join((proc.stdout or '').splitlines()[-12:])
            return (False, tail or f'Package {pkg_name} installation failed')
        return (True, '')
    except Exception as ex:
        return (False, str(ex))


def register_model_routes(blueprint):
    """Register model management routes on the given blueprint."""

    @blueprint.route('/models/catalog', methods=['GET'])
    def models_catalog():
        """Return model catalog with hardware recommendations."""
        from model_library import (
            get_library as _get_ml,
            get_hardware_info as _get_hw,
        )
        hw = _get_hw()
        lib = _get_ml()
        recs = lib.get_recommendations(hw)
        disk = lib.get_disk_space()
        return jsonify({
            'models': recs,
            'hardware': hw,
            'disk': disk,
            'models_path': lib.models_path,
            'download_status': lib.get_download_status(),
        })

    @blueprint.route('/models/hardware', methods=['GET'])
    def models_hardware():
        """Return hardware info for model fitting."""
        from model_library import (
            get_library as _get_ml,
            get_hardware_info as _get_hw,
        )
        hw = _get_hw()
        lib = _get_ml()
        disk = lib.get_disk_space()
        return jsonify({'hardware': hw, 'disk': disk, 'models_path': lib.models_path})

    @blueprint.route('/models/download', methods=['POST'])
    def models_download():
        """Start downloading a model."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        from model_library import get_library as _get_ml

        if not _is_admin():
            return jsonify({'error': 'Only admin can download models'}), 403
        body = request.get_json(silent=True) or {}
        model_id = body.get('model_id', '').strip()
        if not model_id:
            return jsonify({'error': 'model_id required'}), 400

        if not _check_hf_hub():
            ok_hf, err_hf = _install_py_pkg('huggingface_hub', timeout=300)
            if not ok_hf:
                return jsonify({'error': f'huggingface_hub library not found and installation failed: {err_hf[-300:]}' }), 500
            if not _check_hf_hub():
                return jsonify({'error': 'huggingface_hub library still unavailable after installation.'}), 500

        lib = _get_ml()
        sio = None
        try:
            from flask import current_app
            sio = current_app.extensions.get('socketio')
        except Exception:
            pass

        ok, err = lib.start_download(model_id, socketio=sio)
        if not ok:
            return jsonify({'error': err}), 400
        return jsonify({'status': 'ok', 'model_id': model_id})

    @blueprint.route('/models/download/status', methods=['GET'])
    def models_download_status():
        """Poll download progress."""
        from model_library import get_library as _get_ml
        lib = _get_ml()
        return jsonify(lib.get_download_status())

    @blueprint.route('/models/download/cancel', methods=['POST'])
    def models_download_cancel():
        """Cancel active download."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        from model_library import get_library as _get_ml

        if not _is_admin():
            return jsonify({'error': 'Admin only'}), 403
        lib = _get_ml()
        lib.cancel_download()
        return jsonify({'ok': True})

    @blueprint.route('/models/<model_id>', methods=['DELETE'])
    def models_delete(model_id):
        """Delete a downloaded model."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        from model_library import get_library as _get_ml

        if not _is_admin():
            return jsonify({'error': 'Admin only'}), 403
        lib = _get_ml()
        ok, err = lib.delete_model(model_id)
        if not ok:
            return jsonify({'error': err}), 400
        return jsonify({'ok': True})

    @blueprint.route('/models/active', methods=['GET', 'POST'])
    def models_active():
        """Get or set active model."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        _get_username = aichat._get_username
        _load_config = aichat._load_config
        _save_config = aichat._save_config
        from model_library import get_library as _get_ml

        lib = _get_ml()
        if request.method == 'GET':
            m = lib.get_active_model()
            return jsonify({'model': m})

        if not _is_admin():
            return jsonify({'error': 'Admin only'}), 403
        body = request.get_json(silent=True) or {}
        model_id = body.get('model_id')
        ok, err = lib.set_active_model(model_id)
        if not ok:
            return jsonify({'error': err}), 400

        username = _get_username()
        cfg = _load_config(username)
        if model_id is not None:
            cfg['provider'] = 'local'
        elif cfg.get('provider') == 'local':
            cfg['provider'] = 'openai'
        _save_config(cfg, username)

        return jsonify({'ok': True, 'provider': cfg['provider']})

    @blueprint.route('/models/unload', methods=['POST'])
    def models_unload():
        """Unload model from RAM to free memory."""
        from model_library import get_library as _get_ml
        lib = _get_ml()
        loaded, mid = lib.get_loaded_model()
        if loaded is None:
            return jsonify({'status': 'ok', 'loaded': False})
        lib.unload_model()
        return jsonify({'status': 'ok', 'model_id': mid})

    @blueprint.route('/models/path', methods=['GET', 'POST'])
    def models_path():
        """Get or set models storage path."""
        from model_library import get_library as _get_ml

        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        lib = _get_ml()
        if request.method == 'GET':
            return jsonify({'path': lib.models_path, 'disk': lib.get_disk_space()})

        if not _is_admin():
            return jsonify({'error': 'Admin only'}), 403
        body = request.get_json(silent=True) or {}
        new_path = body.get('path', '').strip()
        if not new_path:
            return jsonify({'error': 'Path required'}), 400
        ok, err = lib.set_models_path(new_path)
        if not ok:
            return jsonify({'error': err}), 400
        return jsonify({'ok': True, 'path': lib.models_path, 'disk': lib.get_disk_space()})

    @blueprint.route('/models/custom', methods=['POST'])
    def models_custom_add():
        """Add a custom model by HF URL."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        from model_library import get_library as _get_ml

        if not _is_admin():
            return jsonify({'error': 'Admin only'}), 403
        body = request.get_json(silent=True) or {}
        url = body.get('url', '').strip()
        if not url:
            return jsonify({'error': 'URL required'}), 400
        lib = _get_ml()
        entry, err = lib.add_custom_model(url)
        if not entry:
            return jsonify({'error': err}), 400
        return jsonify({'ok': True, 'model': entry})

    @blueprint.route('/models/custom/<model_id>', methods=['DELETE'])
    def models_custom_remove(model_id):
        """Remove a custom model."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        from model_library import get_library as _get_ml

        if not _is_admin():
            return jsonify({'error': 'Admin only'}), 403
        lib = _get_ml()
        ok, err = lib.remove_custom_model(model_id)
        if not ok:
            return jsonify({'error': err}), 400
        return jsonify({'ok': True})

    @blueprint.route('/models/benchmark', methods=['POST'])
    def models_benchmark():
        """Run inference benchmark on active model. Returns TPS, TTFT, tier."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        from model_library import get_library as _get_ml

        if not _is_admin():
            return jsonify({'error': 'Only admin can run benchmark'}), 403
        body = request.get_json(silent=True) or {}
        model_id = body.get('model_id')
        prompt = body.get('prompt')

        lib = _get_ml()
        active = lib.get_active_model()
        if not active and not model_id:
            return jsonify({'error': 'No active model — download and activate a model in the Model Library'}), 400

        result = lib.run_benchmark(model_id=model_id, prompt=prompt)
        if 'error' in result:
            return jsonify(result), 400
        return jsonify(result)

    @blueprint.route('/models/benchmark/auto', methods=['POST'])
    def models_benchmark_auto():
        """Auto-benchmark: ensure smallest model is available, benchmark it, return TPS scaling for all models."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _is_admin = aichat._is_admin
        from model_library import (
            get_library as _get_ml,
            get_hardware_info as _get_hw,
        )

        if not _is_admin():
            return jsonify({'error': 'Admin only'}), 403

        lib = _get_ml()
        hw = _get_hw()
        recs = lib.get_recommendations(hw)

        downloaded_rec = [m for m in recs if m.get('downloaded') and m['status'] in ('recommended', 'possible')]
        downloaded_rec.sort(key=lambda m: m.get('size_gb', 99))

        if downloaded_rec:
            bench_model = downloaded_rec[0]
        else:
            all_rec = [m for m in recs if m['status'] in ('recommended', 'possible')]
            all_rec.sort(key=lambda m: m.get('size_gb', 99))
            if not all_rec:
                return jsonify({'error': 'No models matching hardware'}), 400
            bench_model = all_rec[0]

            if not _check_hf_hub():
                ok_hf, err_hf = _install_py_pkg('huggingface_hub', timeout=300)
                if not ok_hf:
                    return jsonify({'error': f'huggingface_hub not found: {err_hf[-200:]}'}), 500

            ok, err = lib.download_sync(bench_model['id'])
            if not ok:
                return jsonify({'error': f'Failed to download test model: {err}'}), 500

        lib.set_active_model(bench_model['id'])
        result = lib.run_benchmark(model_id=bench_model['id'])
        if 'error' in result:
            return jsonify(result), 400

        ref_params_b = _parse_params(bench_model.get('params', '0'))
        ref_tps = result.get('tps', 1)
        model_estimates = {}
        for m in recs:
            m_params_b = _parse_params(m.get('params', '0'))
            if ref_params_b > 0 and m_params_b > 0:
                estimated_tps = round(ref_tps * (ref_params_b / m_params_b), 1)
            else:
                estimated_tps = 0
            model_estimates[m['id']] = estimated_tps

        result['ref_model_id'] = bench_model['id']
        result['ref_model_name'] = bench_model.get('name', bench_model['id'])
        result['ref_params'] = bench_model.get('params', '?')
        result['model_estimates'] = model_estimates
        return jsonify(result)

    @blueprint.route('/models/benchmark', methods=['GET'])
    def models_benchmark_results():
        """Get last benchmark results."""
        from model_library import (
            get_library as _get_ml,
            get_tier as _get_tier,
        )
        lib = _get_ml()
        model_id = request.args.get('model_id')
        bench = lib.get_last_benchmark(model_id)
        if not bench:
            return jsonify({'benchmark': None})
        bench_copy = dict(bench)
        bench_copy['tier'] = _get_tier(bench.get('tps', 0))
        return jsonify({'benchmark': bench_copy})

    @blueprint.route('/calibration', methods=['GET'])
    def get_calibration():
        """Get calibration state for current user."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _get_username = aichat._get_username
        username = _get_username()
        cal = _load_json(_calibration_path(username), None)
        if cal is None:
            return jsonify({'calibrated': False})
        return jsonify({**cal, 'calibrated': True})

    @blueprint.route('/calibration', methods=['POST'])
    def save_calibration():
        """Save calibration results after setup wizard."""
        aichat = _get_aichat_module()
        if not aichat:
            return jsonify({'error': 'aichat module not loaded'}), 500

        _get_username = aichat._get_username
        username = _get_username()
        data = request.json or {}
        cal = {
            'calibrated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'tier_id': data.get('tier_id', 'balanced'),
            'model_id': data.get('model_id'),
            'benchmark': data.get('benchmark'),
            'hardware_snapshot': data.get('hardware'),
            'wizard_completed': True,
        }
        _save_json(_calibration_path(username), cal)
        return jsonify({'ok': True, 'calibration': cal})
