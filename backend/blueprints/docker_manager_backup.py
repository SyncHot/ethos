"""
docker_manager_backup.py — part of the Docker Manager blueprint.
Backup & restore: containers and compose projects (volumes, images, config).

Imported at the bottom of docker_manager.py.
"""

import os
import re
import json
import shutil
import subprocess
import time as _time
import hashlib as _hashlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask import request, jsonify

from blueprints.docker_manager import (
    docker_bp,
    _compose_root,
    _run,
    _run_host,
    _json_lines,
    _sio,
    _require_docker,
    _require_admin,
)
from blueprints.docker_manager_compose import _find_compose_projects
from host import get_data_disk as _get_data_disk
from audit import audit_log

_backup_running = {}  # guard: {backup_id: True} for in-progress ops


def _backup_root():
    """Return the backup storage directory (prefer data disk)."""
    dd = _get_data_disk()
    if dd:
        p = os.path.join(dd, 'docker-backups')
    else:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'data', 'docker-backups')
    os.makedirs(p, mode=0o755, exist_ok=True)
    return p


def _backup_id(backup_type, name):
    """Generate a unique backup directory name."""
    ts = _time.strftime('%Y%m%d_%H%M%S')
    short = _hashlib.md5(f'{ts}{name}'.encode()).hexdigest()[:6]
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)[:40]
    return f'{ts}_{backup_type}_{safe_name}_{short}'


def _read_backup_meta(backup_dir):
    """Read metadata.json from a backup directory."""
    meta_path = os.path.join(backup_dir, 'metadata.json')
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def _write_backup_meta(backup_dir, meta):
    """Write metadata.json to a backup directory."""
    with open(os.path.join(backup_dir, 'metadata.json'), 'w') as f:
        json.dump(meta, f, indent=2)


def _dir_size_bytes(path):
    """Calculate total size of a directory in bytes."""
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _fmt_size(nbytes):
    """Human-readable size string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(nbytes) < 1024:
            return f'{nbytes:.1f} {unit}'
        nbytes /= 1024
    return f'{nbytes:.1f} PB'


def _get_container_volumes(inspect_data):
    """Extract named volumes from container inspect data."""
    volumes = []
    for m in inspect_data.get('Mounts', []):
        if m.get('Type') == 'volume':
            volumes.append({
                'name': m.get('Name', ''),
                'destination': m.get('Destination', ''),
                'driver': m.get('Driver', 'local'),
            })
    return volumes


def _get_project_volumes(project_name):
    """Get named volumes used by all containers in a compose project."""
    volumes = []
    seen = set()
    try:
        out, _, rc = _run([
            'docker', 'ps', '-a', '--filter',
            f'label=com.docker.compose.project={project_name}',
            '--format', '{{.ID}}'
        ])
        if rc != 0:
            return volumes
        for cid in out.strip().split('\n'):
            cid = cid.strip()
            if not cid:
                continue
            iout, _, irc = _run(['docker', 'inspect', cid])
            if irc != 0:
                continue
            info = json.loads(iout)
            if info:
                for v in _get_container_volumes(info[0]):
                    if v['name'] not in seen:
                        seen.add(v['name'])
                        volumes.append(v)
    except Exception:
        pass
    return volumes


def _export_volume(vol_name, dest_dir, timeout=300):
    """Export a named Docker volume to a tar.gz file. Returns (path, error)."""
    tar_path = os.path.join(dest_dir, f'{vol_name}.tar.gz')
    try:
        r = subprocess.run(
            ['docker', 'run', '--rm',
             '-v', f'{vol_name}:/volume_data:ro',
             '-v', f'{dest_dir}:/backup',
             'alpine', 'tar', 'czf', f'/backup/{vol_name}.tar.gz', '-C', '/volume_data', '.'],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            return None, r.stderr.strip()
        return tar_path, None
    except subprocess.TimeoutExpired:
        return None, 'Volume export timed out'
    except Exception as e:
        return None, str(e)


def _import_volume(vol_name, tar_path, timeout=300):
    """Import a tar.gz file into a named Docker volume. Returns error or None."""
    tar_dir = os.path.dirname(tar_path)
    tar_file = os.path.basename(tar_path)
    try:
        subprocess.run(['docker', 'volume', 'create', vol_name],
                       capture_output=True, text=True, timeout=30)
        r = subprocess.run(
            ['docker', 'run', '--rm',
             '-v', f'{vol_name}:/volume_data',
             '-v', f'{tar_dir}:/backup:ro',
             'alpine', 'sh', '-c',
             f'rm -rf /volume_data/* /volume_data/..?* /volume_data/.[!.]* 2>/dev/null; '
             f'tar xzf /backup/{tar_file} -C /volume_data'],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            return r.stderr.strip()
        return None
    except subprocess.TimeoutExpired:
        return 'Volume import timed out'
    except Exception as e:
        return str(e)


# ── Backup create worker ──

def _bg_create_backup(backup_dir, meta, backup_type, target_name, mode):
    """Background worker: create a Docker backup."""
    s = _sio()
    bid = os.path.basename(backup_dir)

    def emit(pct, msg, status='running'):
        if s:
            s.emit('docker_backup', {
                'id': bid, 'percent': pct, 'message': msg, 'status': status
            })

    try:
        os.makedirs(backup_dir, exist_ok=True)

        if backup_type == 'container':
            _backup_container(backup_dir, meta, target_name, mode, emit)
        else:
            _backup_project(backup_dir, meta, target_name, mode, emit)

        meta['size'] = _dir_size_bytes(backup_dir)
        meta['size_human'] = _fmt_size(meta['size'])
        meta['status'] = 'complete'
        _write_backup_meta(backup_dir, meta)
        emit(100, 'Backup zakończony pomyślnie.', 'done')
        audit_log('docker.backup.create', f'Backup created: {bid} ({backup_type} {target_name})')

    except Exception as e:
        meta['status'] = 'error'
        meta['error'] = str(e)
        try:
            _write_backup_meta(backup_dir, meta)
        except Exception:
            pass
        emit(0, f'Błąd backupu: {e}', 'error')
    finally:
        _backup_running.pop(bid, None)


def _backup_container(backup_dir, meta, container_id, mode, emit):
    """Backup a single container: inspect + volumes + optional image."""
    emit(5, 'Zapisywanie konfiguracji kontenera...')

    out, err, rc = _run(['docker', 'inspect', container_id])
    if rc != 0:
        raise RuntimeError(f'docker inspect failed: {err}')
    inspect_data = json.loads(out)
    if not inspect_data:
        raise RuntimeError('Container not found')

    info = inspect_data[0]
    config_path = os.path.join(backup_dir, 'config.json')
    with open(config_path, 'w') as f:
        json.dump(info, f, indent=2)

    container_name = info.get('Name', '').lstrip('/')
    image_name = info.get('Config', {}).get('Image', '')
    meta['container_name'] = container_name
    meta['image'] = image_name

    volumes = _get_container_volumes(info)
    meta['volumes'] = [v['name'] for v in volumes]

    if volumes:
        emit(15, f'Eksportowanie {len(volumes)} wolumenów...')
        vol_dir = os.path.join(backup_dir, 'volumes')
        os.makedirs(vol_dir, exist_ok=True)
        for i, vol in enumerate(volumes):
            pct = 15 + int(45 * (i / len(volumes)))
            emit(pct, f'Wolumen: {vol["name"]}...')
            _, vol_err = _export_volume(vol['name'], vol_dir)
            if vol_err:
                emit(pct, f'Ostrzeżenie: wolumen {vol["name"]}: {vol_err}')

    if mode == 'full':
        emit(65, f'Zapisywanie obrazu {image_name}...')
        img_dir = os.path.join(backup_dir, 'images')
        os.makedirs(img_dir, exist_ok=True)
        safe_img = re.sub(r'[^a-zA-Z0-9_.-]', '_', image_name)
        img_path = os.path.join(img_dir, f'{safe_img}.tar')
        try:
            r = subprocess.run(
                ['docker', 'save', '-o', img_path, image_name],
                capture_output=True, text=True, timeout=600
            )
            if r.returncode != 0:
                emit(70, f'Ostrzeżenie: nie udało się zapisać obrazu: {r.stderr.strip()[:200]}')
            else:
                meta['image_saved'] = True
        except subprocess.TimeoutExpired:
            emit(70, 'Ostrzeżenie: zapis obrazu przekroczył limit czasu')

    emit(90, 'Finalizowanie...')
    _write_backup_meta(backup_dir, meta)


def _backup_project(backup_dir, meta, project_name, mode, emit):
    """Backup a Docker Compose project: compose files + volumes + optional images."""
    emit(5, 'Zapisywanie plików compose...')

    compose_dir = os.path.join(backup_dir, 'compose')
    os.makedirs(compose_dir, exist_ok=True)

    project_path = os.path.join(_compose_root(), project_name)
    if not os.path.isdir(project_path):
        raise RuntimeError(f'Project directory not found: {project_name}')

    copied_files = []
    for fname in os.listdir(project_path):
        fpath = os.path.join(project_path, fname)
        if os.path.isfile(fpath):
            shutil.copy2(fpath, os.path.join(compose_dir, fname))
            copied_files.append(fname)
    meta['compose_files'] = copied_files

    emit(10, 'Pobieranie informacji o kontenerach projektu...')
    out, _, rc = _run([
        'docker', 'ps', '-a', '--filter',
        f'label=com.docker.compose.project={project_name}',
        '--format', '{{.Image}}'
    ])
    images = list(set(line.strip() for line in out.strip().split('\n') if line.strip())) if rc == 0 else []
    meta['images'] = images

    volumes = _get_project_volumes(project_name)
    meta['volumes'] = [v['name'] for v in volumes]

    if volumes:
        emit(15, f'Eksportowanie {len(volumes)} wolumenów...')
        vol_dir = os.path.join(backup_dir, 'volumes')
        os.makedirs(vol_dir, exist_ok=True)
        for i, vol in enumerate(volumes):
            pct = 15 + int(35 * (i / len(volumes)))
            emit(pct, f'Wolumen: {vol["name"]}...')
            _, vol_err = _export_volume(vol['name'], vol_dir)
            if vol_err:
                emit(pct, f'Ostrzeżenie: wolumen {vol["name"]}: {vol_err}')

    if mode == 'full' and images:
        emit(55, f'Zapisywanie {len(images)} obrazów...')
        img_dir = os.path.join(backup_dir, 'images')
        os.makedirs(img_dir, exist_ok=True)
        for i, img in enumerate(images):
            pct = 55 + int(35 * (i / len(images)))
            emit(pct, f'Obraz: {img}...')
            safe_img = re.sub(r'[^a-zA-Z0-9_.-]', '_', img)
            img_path = os.path.join(img_dir, f'{safe_img}.tar')
            try:
                r = subprocess.run(
                    ['docker', 'save', '-o', img_path, img],
                    capture_output=True, text=True, timeout=600
                )
                if r.returncode != 0:
                    emit(pct, f'Ostrzeżenie: obraz {img}: {r.stderr.strip()[:200]}')
                else:
                    meta.setdefault('images_saved', []).append(img)
            except subprocess.TimeoutExpired:
                emit(pct, f'Ostrzeżenie: zapis obrazu {img} przekroczył limit czasu')

    emit(92, 'Finalizowanie...')
    _write_backup_meta(backup_dir, meta)


# ── Restore worker ──

def _bg_restore_backup(backup_dir, meta, options):
    """Background worker: restore a Docker backup."""
    s = _sio()
    bid = os.path.basename(backup_dir)
    backup_type = meta.get('type', 'container')

    def emit(pct, msg, status='running'):
        if s:
            s.emit('docker_restore', {
                'id': bid, 'percent': pct, 'message': msg, 'status': status
            })

    try:
        if backup_type == 'container':
            _restore_container(backup_dir, meta, options, emit)
        else:
            _restore_project(backup_dir, meta, options, emit)

        emit(100, 'Przywracanie zakończone pomyślnie.', 'done')
        audit_log('docker.backup.restore', f'Backup restored: {bid} ({backup_type})')
    except Exception as e:
        emit(0, f'Błąd przywracania: {e}', 'error')
    finally:
        _backup_running.pop(bid, None)


def _restore_container(backup_dir, meta, options, emit):
    """Restore a single container from backup."""
    config_path = os.path.join(backup_dir, 'config.json')
    if not os.path.isfile(config_path):
        raise RuntimeError('Brak pliku konfiguracji kontenera')

    with open(config_path, 'r') as f:
        info = json.load(f)

    image = info.get('Config', {}).get('Image', '')
    container_name = meta.get('container_name', '') or info.get('Name', '').lstrip('/')

    emit(5, f'Przygotowywanie obrazu {image}...')
    img_dir = os.path.join(backup_dir, 'images')
    image_loaded = False
    if os.path.isdir(img_dir):
        for tar_file in os.listdir(img_dir):
            if tar_file.endswith('.tar'):
                emit(10, f'Ładowanie obrazu z backupu...')
                r = subprocess.run(
                    ['docker', 'load', '-i', os.path.join(img_dir, tar_file)],
                    capture_output=True, text=True, timeout=600
                )
                if r.returncode == 0:
                    image_loaded = True

    if not image_loaded:
        emit(15, f'Pobieranie obrazu {image}...')
        r = subprocess.run(
            ['docker', 'pull', image],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            raise RuntimeError(f'Nie udało się pobrać obrazu {image}: {r.stderr.strip()[:200]}')

    vol_dir = os.path.join(backup_dir, 'volumes')
    if os.path.isdir(vol_dir):
        tar_files = [f for f in os.listdir(vol_dir) if f.endswith('.tar.gz')]
        if tar_files:
            emit(25, f'Przywracanie {len(tar_files)} wolumenów...')
            for i, tf in enumerate(tar_files):
                vol_name = tf.replace('.tar.gz', '')
                pct = 25 + int(30 * (i / len(tar_files)))
                emit(pct, f'Wolumen: {vol_name}...')
                err = _import_volume(vol_name, os.path.join(vol_dir, tf))
                if err:
                    emit(pct, f'Ostrzeżenie: wolumen {vol_name}: {err}')

    emit(60, 'Tworzenie kontenera...')
    config = info.get('Config', {})
    host_config = info.get('HostConfig', {})

    if container_name:
        subprocess.run(['docker', 'rm', '-f', container_name],
                       capture_output=True, text=True, timeout=15)

    args = ['docker', 'create']

    if container_name:
        args += ['--name', container_name]

    hostname = config.get('Hostname', '')
    if hostname:
        args += ['--hostname', hostname]

    for env_var in (config.get('Env') or []):
        args += ['-e', env_var]

    port_bindings = host_config.get('PortBindings') or {}
    for container_port, bindings in port_bindings.items():
        if bindings:
            for b in bindings:
                host_ip = b.get('HostIp', '')
                host_port = b.get('HostPort', '')
                if host_ip and host_ip != '0.0.0.0':
                    args += ['-p', f'{host_ip}:{host_port}:{container_port}']
                elif host_port:
                    args += ['-p', f'{host_port}:{container_port}']

    for m in info.get('Mounts', []):
        mtype = m.get('Type', '')
        src = m.get('Source', '')
        dst = m.get('Destination', '')
        rw = m.get('RW', True)
        mode_str = 'rw' if rw else 'ro'
        if mtype == 'volume':
            vol_name = m.get('Name', '')
            if vol_name and dst:
                args += ['-v', f'{vol_name}:{dst}:{mode_str}']
        elif mtype == 'bind' and src and dst:
            args += ['-v', f'{src}:{dst}:{mode_str}']

    net_mode = host_config.get('NetworkMode', '')
    if net_mode and net_mode != 'default':
        args += ['--network', net_mode]

    restart_policy = host_config.get('RestartPolicy', {})
    rp_name = restart_policy.get('Name', '')
    if rp_name and rp_name != 'no':
        max_retry = restart_policy.get('MaximumRetryCount', 0)
        if rp_name == 'on-failure' and max_retry > 0:
            args += ['--restart', f'on-failure:{max_retry}']
        else:
            args += ['--restart', rp_name]

    if host_config.get('Privileged'):
        args.append('--privileged')

    working_dir = config.get('WorkingDir', '')
    if working_dir:
        args += ['-w', working_dir]

    user = config.get('User', '')
    if user:
        args += ['-u', user]

    labels = config.get('Labels') or {}
    for k, v in labels.items():
        if k.startswith('com.docker.compose.'):
            continue
        args += ['--label', f'{k}={v}']

    args.append(image)
    cmd = config.get('Cmd') or []
    entrypoint = config.get('Entrypoint') or []
    if entrypoint:
        args_before_image = args[:-1]
        args = args_before_image + ['--entrypoint', entrypoint[0]] + [image]
        if len(entrypoint) > 1:
            args += entrypoint[1:]
        if cmd:
            args += cmd
    elif cmd:
        args += cmd

    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f'docker create failed: {r.stderr.strip()[:300]}')

    new_container_id = r.stdout.strip()[:12]

    emit(85, 'Uruchamianie kontenera...')
    r = subprocess.run(['docker', 'start', new_container_id],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        emit(90, f'Ostrzeżenie: kontener utworzony ale nie uruchomiony: {r.stderr.strip()[:200]}')

    emit(95, f'Kontener {container_name or new_container_id} przywrócony.')


def _restore_project(backup_dir, meta, options, emit):
    """Restore a Docker Compose project from backup."""
    compose_bak = os.path.join(backup_dir, 'compose')
    if not os.path.isdir(compose_bak):
        raise RuntimeError('Brak plików compose w backupie')

    project_name = meta.get('name', '')
    if not project_name:
        raise RuntimeError('Brak nazwy projektu w metadanych')

    img_dir = os.path.join(backup_dir, 'images')
    if os.path.isdir(img_dir):
        tar_files = [f for f in os.listdir(img_dir) if f.endswith('.tar')]
        if tar_files:
            emit(5, f'Ładowanie {len(tar_files)} obrazów...')
            for i, tf in enumerate(tar_files):
                pct = 5 + int(20 * (i / len(tar_files)))
                emit(pct, f'Obraz: {tf}...')
                subprocess.run(
                    ['docker', 'load', '-i', os.path.join(img_dir, tf)],
                    capture_output=True, text=True, timeout=600
                )

    emit(30, 'Przywracanie plików compose...')
    project_path = os.path.join(_compose_root(), project_name)
    os.makedirs(project_path, mode=0o755, exist_ok=True)

    for fname in os.listdir(compose_bak):
        src = os.path.join(compose_bak, fname)
        dst = os.path.join(project_path, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)

    vol_dir = os.path.join(backup_dir, 'volumes')
    if os.path.isdir(vol_dir):
        tar_files = [f for f in os.listdir(vol_dir) if f.endswith('.tar.gz')]
        if tar_files:
            emit(40, f'Przywracanie {len(tar_files)} wolumenów...')
            for i, tf in enumerate(tar_files):
                vol_name = tf.replace('.tar.gz', '')
                pct = 40 + int(25 * (i / len(tar_files)))
                emit(pct, f'Wolumen: {vol_name}...')
                err = _import_volume(vol_name, os.path.join(vol_dir, tf))
                if err:
                    emit(pct, f'Ostrzeżenie: wolumen {vol_name}: {err}')

    emit(70, 'Pobieranie obrazów i uruchamianie projektu...')
    try:
        _run_host('docker compose pull', timeout=300, cwd=project_path)
    except Exception:
        pass

    emit(85, 'Uruchamianie projektu...')
    out, err, rc = _run_host('docker compose up -d', timeout=120, cwd=project_path)
    if rc != 0:
        combined = (out + '\n' + err).strip()
        raise RuntimeError(f'docker compose up failed: {combined[:300]}')

    emit(95, f'Projekt {project_name} przywrócony.')


# ── Backup API endpoints ──

@docker_bp.route('/backups')
@_require_docker
def list_backups():
    """List all Docker backups."""
    try:
        root = _backup_root()
        backups = []
        for entry in sorted(os.listdir(root), reverse=True):
            entry_path = os.path.join(root, entry)
            if not os.path.isdir(entry_path):
                continue
            meta = _read_backup_meta(entry_path)
            if not meta:
                continue
            meta['id'] = entry
            backups.append(meta)
        return jsonify(backups)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/backups/<backup_id>')
@_require_docker
def get_backup(backup_id):
    """Get details of a specific backup."""
    backup_id = re.sub(r'[^a-zA-Z0-9_-]', '', backup_id)
    backup_dir = os.path.join(_backup_root(), backup_id)
    if not os.path.isdir(backup_dir):
        return jsonify({'error': 'Backup not found'}), 404
    meta = _read_backup_meta(backup_dir)
    if not meta:
        return jsonify({'error': 'Invalid backup metadata'}), 500
    meta['id'] = backup_id
    return jsonify(meta)


def _resolve_all_targets(backup_type):
    """Return list of (name, label) tuples for all containers or projects."""
    targets = []
    if backup_type == 'container':
        fmt = '{{json .}}'
        out, _, rc = _run(['docker', 'ps', '-a', '--format', fmt, '--no-trunc'])
        if rc == 0:
            for raw in _json_lines(out):
                cid = raw.get('ID', '')[:12]
                cname = raw.get('Names', '')
                cimage = raw.get('Image', '')
                if cid:
                    targets.append((cid, f'{cname} ({cimage})'))
    else:
        projects = _find_compose_projects()
        for p in projects:
            targets.append((p['name'], p['name']))
    return targets


def _bg_create_backup_all(targets, backup_type, mode, label_prefix, bulk_bid):
    """Background worker: create backups for all targets sequentially."""
    s = _sio()
    total = len(targets)

    def emit(pct, msg, status='running'):
        if s:
            s.emit('docker_backup', {
                'id': bulk_bid, 'percent': pct, 'message': msg, 'status': status
            })

    try:
        for idx, (name, default_label) in enumerate(targets):
            progress_base = int((idx / total) * 100)
            lbl = f'{label_prefix} — {default_label}' if label_prefix else default_label
            emit(progress_base, f'[{idx + 1}/{total}] {default_label}...')

            bid = _backup_id(backup_type, name)
            backup_dir = os.path.join(_backup_root(), bid)
            meta = {
                'type': backup_type,
                'name': name,
                'label': lbl,
                'mode': mode,
                'created': _time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'status': 'running',
                'size': 0,
                'size_human': '0 B',
                'volumes': [],
                'images': [],
            }
            try:
                os.makedirs(backup_dir, exist_ok=True)
                if backup_type == 'container':
                    _backup_container(backup_dir, meta, name, mode, lambda p, m, s='running': emit(
                        progress_base + int(p * (1 / total)), m, s))
                else:
                    _backup_project(backup_dir, meta, name, mode, lambda p, m, s='running': emit(
                        progress_base + int(p * (1 / total)), m, s))
                meta['size'] = _dir_size_bytes(backup_dir)
                meta['size_human'] = _fmt_size(meta['size'])
                meta['status'] = 'complete'
                _write_backup_meta(backup_dir, meta)
            except Exception as e:
                meta['status'] = 'error'
                meta['error'] = str(e)
                try:
                    _write_backup_meta(backup_dir, meta)
                except Exception:
                    pass
                emit(progress_base, f'Błąd: {default_label} — {e}')

        emit(100, f'Backup zakończony — {total} elementów.', 'done')
        audit_log('docker.backup.create', f'Bulk backup: {total} {backup_type}s ({mode})')
    except Exception as e:
        emit(0, f'Błąd backupu zbiorczego: {e}', 'error')
    finally:
        _backup_running.pop(bulk_bid, None)


@docker_bp.route('/backups', methods=['POST'])
@_require_docker
@_require_admin
def create_backup():
    """Create a new Docker backup (runs in background).

    Body: { type: "container"|"project", name: "id_or_name"|"__all__", label: "...", mode: "full"|"light" }
    Progress via SocketIO: docker_backup
    """
    data = request.get_json(force=True) if request.data else {}
    backup_type = data.get('type', '').strip()
    target_name = data.get('name', '').strip()
    label = data.get('label', '').strip()
    mode = data.get('mode', 'light').strip()

    if backup_type not in ('container', 'project'):
        return jsonify({'error': 'Invalid backup type — use "container" or "project"'}), 400
    if not target_name:
        return jsonify({'error': 'Target name is required'}), 400
    if mode not in ('full', 'light'):
        return jsonify({'error': 'Invalid mode — use "full" or "light"'}), 400

    if target_name == '__all__':
        targets = _resolve_all_targets(backup_type)
        if not targets:
            return jsonify({'error': 'No targets found for bulk backup'}), 404
        bid = _backup_id(backup_type, '__all__')
        if bid in _backup_running:
            return jsonify({'error': 'Bulk backup already in progress'}), 409
        _backup_running[bid] = True
        s = _sio()
        if s:
            s.start_background_task(_bg_create_backup_all, targets, backup_type, mode, label, bid)
        else:
            _bg_create_backup_all(targets, backup_type, mode, label, bid)
        return jsonify({'ok': True, 'id': bid, 'status': 'started', 'count': len(targets)})

    bid = _backup_id(backup_type, target_name)
    backup_dir = os.path.join(_backup_root(), bid)

    if bid in _backup_running:
        return jsonify({'error': 'Backup already in progress'}), 409

    meta = {
        'type': backup_type,
        'name': target_name,
        'label': label or target_name,
        'mode': mode,
        'created': _time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'status': 'running',
        'size': 0,
        'size_human': '0 B',
        'volumes': [],
        'images': [],
    }

    _backup_running[bid] = True
    s = _sio()
    if s:
        s.start_background_task(_bg_create_backup, backup_dir, meta, backup_type, target_name, mode)
    else:
        _bg_create_backup(backup_dir, meta, backup_type, target_name, mode)

    return jsonify({'ok': True, 'id': bid, 'status': 'started'})


@docker_bp.route('/backups/<backup_id>', methods=['DELETE'])
@_require_docker
@_require_admin
def delete_backup(backup_id):
    """Delete a Docker backup."""
    backup_id = re.sub(r'[^a-zA-Z0-9_-]', '', backup_id)
    backup_dir = os.path.join(_backup_root(), backup_id)

    real_root = os.path.realpath(_backup_root())
    real_path = os.path.realpath(backup_dir)
    if not real_path.startswith(real_root + '/'):
        return jsonify({'error': 'Invalid backup path'}), 400

    if not os.path.isdir(backup_dir):
        return jsonify({'error': 'Backup not found'}), 404

    if backup_id in _backup_running:
        return jsonify({'error': 'Cannot delete — backup in progress'}), 409

    try:
        shutil.rmtree(backup_dir)
        audit_log('docker.backup.delete', f'Backup deleted: {backup_id}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/backups/<backup_id>/restore', methods=['POST'])
@_require_docker
@_require_admin
def restore_backup(backup_id):
    """Restore from a Docker backup (runs in background).

    Progress via SocketIO: docker_restore
    """
    backup_id = re.sub(r'[^a-zA-Z0-9_-]', '', backup_id)
    backup_dir = os.path.join(_backup_root(), backup_id)

    if not os.path.isdir(backup_dir):
        return jsonify({'error': 'Backup not found'}), 404

    meta = _read_backup_meta(backup_dir)
    if not meta:
        return jsonify({'error': 'Invalid backup metadata'}), 500

    if meta.get('status') != 'complete':
        return jsonify({'error': 'Backup is incomplete or in error state'}), 400

    if backup_id in _backup_running:
        return jsonify({'error': 'Operation already in progress for this backup'}), 409

    data = request.get_json(force=True) if request.data else {}
    options = {
        'restore_volumes': data.get('restore_volumes', True),
    }

    _backup_running[backup_id] = True
    s = _sio()
    if s:
        s.start_background_task(_bg_restore_backup, backup_dir, meta, options)
    else:
        _bg_restore_backup(backup_dir, meta, options)

    return jsonify({'ok': True, 'id': backup_id, 'status': 'started'})
