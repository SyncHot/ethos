"""
EthOS — Backup Scheduling Sub-module
Routes: /trigger-smart, /profiles, /profiles/export, /profiles/import,
        /profiles/<id>/schedule, /profiles/<id> (GET/PUT/DELETE),
        /profiles/<id>/run, /profiles/<id>/key, /scheduled-backups
"""

import json
from datetime import datetime

from flask import jsonify, request

import blueprints.backup as _backup_mod
from blueprints.backup import (
    backup_bp, load_profiles, load_schedule_state,
    load_ssh_configs, emit_log, _calc_next_run, _run_scheduled_backup,
    _prepare_encryption, _resolve_encryption_passphrase,
    run_backup,
)
from blueprints.profiles_db import get_db_connection


@backup_bp.route('/trigger-smart', methods=['POST'])
def trigger_smart_backup():
    """Trigger a backup due to SMART warning."""
    # Check if a backup is already running
    with _backup_mod.operation_lock:
        if _backup_mod.current_operation is not None:
             return jsonify({'status': 'busy', 'message': 'Backup already in progress'}), 200

    # Find a suitable profile
    profiles = load_profiles()
    target_profile = None

    # 1. Look for explicit "SMART" profile
    for p in profiles:
        if 'smart' in p['name'].lower():
            target_profile = p
            break

    # 2. Look for "System" profile
    if not target_profile:
        for p in profiles:
            if 'system' in p['name'].lower():
                target_profile = p
                break

    # 3. Fallback to any profile
    if not target_profile and profiles:
        target_profile = profiles[0]

    if target_profile:
        destination = target_profile.get('destination')
        # Load SSH config if needed
        if destination and destination.get('type') == 'ssh':
             ssh_id = destination.get('server_id')
             if ssh_id:
                 configs = load_ssh_configs()
                 ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
                 if ssh_cfg:
                     destination['config'] = ssh_cfg

        retention = target_profile.get('retention', 0)
        incremental = target_profile.get('incremental', False)

        emit_log(f"SMART Alert triggered backup: {target_profile['name']}", 'warning')

        with _backup_mod.operation_lock:
             _backup_mod.current_operation = 'backup'

        _backup_mod._socketio.start_background_task(_run_scheduled_backup, target_profile, destination, retention, incremental)
        return jsonify({'status': 'started', 'profile': target_profile['name']})
    else:
        return jsonify({'status': 'no_profile', 'message': 'No backup profiles configured'}), 400


@backup_bp.route('/profiles', methods=['GET'])
def get_profiles():
    return jsonify({'profiles': load_profiles()})

@backup_bp.route('/profiles', methods=['POST'])
def create_profile():
    data = request.json or {}
    if not data.get('name'):
        return jsonify({'error': 'Name required'}), 400
    if not data.get('paths'):
        return jsonify({'error': 'Paths required'}), 400
    enc_input = data.get('encryption')
    enc, generated_key = _prepare_encryption(enc_input)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO profiles (name, paths, destination, schedule, options, retention, incremental, encryption) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (
        data['name'], json.dumps(data['paths']),
        json.dumps(data.get('destination')) if data.get('destination') else None,
        json.dumps(data['schedule']) if data.get('schedule') else None,
        data.get('options'), data.get('retention', 0),
        1 if data.get('incremental') else 0,
        json.dumps(enc) if enc else None,
    ))
    conn.commit()
    pid = c.lastrowid
    conn.close()
    enc_public = {k: v for k, v in enc.items() if k != 'stored_key'} if enc else None
    resp = {'success': True, 'profile': {'id': str(pid), 'name': data['name'], 'paths': data['paths'], 'destination': data.get('destination'), 'schedule': data.get('schedule'), 'retention': data.get('retention', 0), 'incremental': bool(data.get('incremental')), 'encryption': enc_public}}
    if generated_key:
        resp['generated_key'] = generated_key
    return jsonify(resp)


@backup_bp.route('/profiles/export', methods=['GET'])
def export_profiles():
    """Export all profiles as JSON for download."""
    profiles = load_profiles()
    export_data = {
        'version': 1,
        'exported_at': datetime.now().isoformat(),
        'profiles': []
    }
    for p in profiles:
        export_data['profiles'].append({
            'name': p['name'],
            'paths': p['paths'],
            'destination': p['destination'],
            'schedule': p['schedule'],
            'options': p.get('options'),
            'retention': p.get('retention', 0),
            'incremental': p.get('incremental', False),
            'encryption': p.get('encryption'),
        })
    return jsonify(export_data)


@backup_bp.route('/profiles/import', methods=['POST'])
def import_profiles():
    """Import profiles from JSON. Supports both file upload and JSON body."""
    try:
        # Support file upload
        if request.files and 'file' in request.files:
            file = request.files['file']
            import_data = json.loads(file.read().decode('utf-8'))
        else:
            import_data = request.json or {}

        if not import_data:
            return jsonify({'error': 'No data to import'}), 400

        # Support both wrapped format (with 'profiles' key) and raw array
        if isinstance(import_data, list):
            profiles_to_import = import_data
        elif isinstance(import_data, dict):
            profiles_to_import = import_data.get('profiles', [])
        else:
            return jsonify({'error': 'Invalid data format'}), 400

        if not profiles_to_import:
            return jsonify({'error': 'No profiles to import'}), 400

        imported = 0
        skipped = 0
        errors = []
        existing = load_profiles()
        existing_names = {p['name'] for p in existing}

        conn = get_db_connection()
        c = conn.cursor()
        for idx, p in enumerate(profiles_to_import):
            try:
                name = p.get('name', '').strip()
                paths = p.get('paths', [])
                if not name:
                    errors.append(f'Profil #{idx+1}: brak nazwy')
                    skipped += 1
                    continue
                if not paths:
                    errors.append(f'Profile "{name}": no paths')
                    skipped += 1
                    continue

                # Auto-rename duplicates
                original_name = name
                counter = 1
                while name in existing_names:
                    name = f"{original_name} ({counter})"
                    counter += 1

                destination = p.get('destination')
                schedule = p.get('schedule')
                enc_import = p.get('encryption')
                # When importing, strip any stored_key (it was encrypted on the exporting machine)
                # and regenerate a new key if needed
                enc, generated_key = _prepare_encryption(
                    {k: v for k, v in enc_import.items() if k != 'stored_key'} if enc_import else None
                )
                c.execute(
                    'INSERT INTO profiles (name, paths, destination, schedule, options, retention, incremental, encryption) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        name,
                        json.dumps(paths) if isinstance(paths, list) else paths,
                        json.dumps(destination) if destination else None,
                        json.dumps(schedule) if schedule else None,
                        p.get('options'),
                        p.get('retention', 0),
                        1 if p.get('incremental') else 0,
                        json.dumps(enc) if enc else None,
                    )
                )
                existing_names.add(name)
                imported += 1
            except Exception as e:
                errors.append(f'Profil "{p.get("name", "?")}": {str(e)}')
                skipped += 1

        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'imported': imported,
            'skipped': skipped,
            'errors': errors,
            'message': f'Imported {imported} profiles' + (f', skipped {skipped}' if skipped else '')
        })
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid JSON format'}), 400
    except Exception as e:
        return jsonify({'error': f'Import error: {str(e)}'}), 500


@backup_bp.route('/profiles/<profile_id>/schedule', methods=['PUT'])
def update_profile_schedule(profile_id):
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404
    c.execute('UPDATE profiles SET schedule=? WHERE id=?', (json.dumps(data.get('schedule')) if data.get('schedule') else None, profile_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>', methods=['PUT'])
def update_profile(profile_id):
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404
    enc_input = data.get('encryption')
    existing_enc = None
    try:
        existing_enc = json.loads(row['encryption']) if row['encryption'] else None
    except Exception:
        pass
    enc, generated_key = _prepare_encryption(enc_input, existing_enc)
    c.execute('UPDATE profiles SET name=?, paths=?, destination=?, schedule=?, options=?, retention=?, incremental=?, encryption=? WHERE id=?', (
        data.get('name', row['name']),
        json.dumps(data.get('paths', json.loads(row['paths']))),
        json.dumps(data.get('destination')) if data.get('destination') else row['destination'],
        json.dumps(data.get('schedule')) if data.get('schedule') else row['schedule'],
        data.get('options', row['options']),
        data.get('retention', row['retention'] or 0),
        1 if data.get('incremental') else 0,
        json.dumps(enc) if enc is not None else None,
        profile_id
    ))
    conn.commit()
    updated = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    conn.close()
    if updated:
        enc_val = None
        try:
            enc_val = json.loads(updated['encryption']) if updated['encryption'] else None
        except Exception:
            pass
        enc_public = {k: v for k, v in enc_val.items() if k != 'stored_key'} if enc_val else None
        resp = {'success': True, 'profile': {
            'id': str(updated['id']), 'name': updated['name'],
            'paths': json.loads(updated['paths']),
            'destination': json.loads(updated['destination']) if updated['destination'] else None,
            'schedule': json.loads(updated['schedule']) if updated['schedule'] else None,
            'retention': updated['retention'] or 0,
            'incremental': bool(updated['incremental']),
            'encryption': enc_public,
        }}
        if generated_key:
            resp['generated_key'] = generated_key
        return jsonify(resp)
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>', methods=['DELETE'])
def delete_profile(profile_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM profiles WHERE id = ?', (profile_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>/run', methods=['POST'])
def run_profile(profile_id):
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404
    enc = None
    try:
        enc = json.loads(row['encryption']) if row['encryption'] else None
    except Exception:
        pass
    profile = {
        'id': str(row['id']), 'name': row['name'],
        'paths': json.loads(row['paths']),
        'destination': json.loads(row['destination']) if row['destination'] else None,
        'retention': row['retention'] or 0,
        'incremental': bool(row['incremental']) if row['incremental'] else False,
        'encryption': enc,
    }
    conn.close()

    # Resolve encryption passphrase
    data = request.json or {}
    encrypt_passphrase = data.get('encrypt_passphrase') or None
    if enc and enc.get('enabled'):
        if enc.get('mode') == 'key':
            # Key mode: resolve from stored key automatically
            try:
                encrypt_passphrase = _resolve_encryption_passphrase(enc)
            except Exception as e:
                return jsonify({'error': f'Encryption key error: {e}'}), 500
        elif not encrypt_passphrase:
            return jsonify({'error': 'Profile has encryption enabled — enter password', 'needs_passphrase': True}), 400

    with _backup_mod.operation_lock:
        if _backup_mod.current_operation is not None:
            return jsonify({'error': 'Inna operacja jest w toku'}), 400
        _backup_mod.current_operation = 'backup'

    destination = profile.get('destination')
    if destination and destination.get('type') == 'ssh':
        ssh_id = destination.get('server_id')
        if ssh_id:
            configs = load_ssh_configs()
            ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
            if ssh_cfg:
                destination['config'] = ssh_cfg

    _backup_mod._socketio.start_background_task(run_backup, profile['paths'], destination, profile['name'], profile['retention'], profile['incremental'], encrypt_passphrase)
    return jsonify({'status': 'ok', 'profile': profile["name"]})


@backup_bp.route('/profiles/<profile_id>/key', methods=['GET'])
def get_profile_key(profile_id):
    """Return the plaintext encryption key for a key-mode profile (for user backup)."""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Profile not found'}), 404
    enc = None
    try:
        enc = json.loads(row['encryption']) if row['encryption'] else None
    except Exception:
        pass
    if not enc or not enc.get('enabled') or enc.get('mode') != 'key':
        return jsonify({'error': 'Profile does not use key mode'}), 400
    try:
        key = _resolve_encryption_passphrase(enc)
        return jsonify({'key': key})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@backup_bp.route('/scheduled-backups')
def get_scheduled_backups():
    profiles = load_profiles()
    state = load_schedule_state()
    scheduled = []
    for p in profiles:
        schedule = p.get('schedule')
        if isinstance(schedule, str):
            try:
                schedule = json.loads(schedule)
            except Exception:
                continue
        if schedule and schedule.get('type') != 'manual':
            last_run = state.get(str(p['id']), {}).get('last_run')
            scheduled.append({
                'profile_id': p['id'], 'profile_name': p['name'],
                'schedule': schedule, 'last_run': last_run,
                'next_run': _calc_next_run(schedule, last_run)
            })
    return jsonify({'scheduled': scheduled})
