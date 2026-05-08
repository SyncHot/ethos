"""
EthOS — Backup Snapshots Create Worker
Extracts snapshot creation logic from backup_snapshots.py.
Contains _create_snapshot_worker() function (lines 1274-1663).
"""

import os
import subprocess
import json
import time
from datetime import datetime

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import get_ethos_user


# Import shared helpers from main module at runtime to avoid circular imports
def _get_main():
    """Get the main backup_snapshots module at runtime."""
    return sys.modules.get('blueprints.backup_snapshots')


def _create_snapshot_worker(label, include_docker, include_volumes,
                             include_ethos, include_system, dest_type, dest_path,
                             include_vms=False, include_models=False, include_userdirs=True):
    """Background task: creates a full system snapshot."""
    main = _get_main()
    if not main:
        return

    _snap_update = main._snap_update
    SNAPSHOTS_DIR = main.SNAPSHOTS_DIR
    _docker_ok = main._docker_ok
    _docker_cmd = main._docker_cmd
    _read_file_safe = main._read_file_safe
    _find_env_files = main._find_env_files
    _emit = main._emit

    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        snap_name = f'snap_{ts}'
        snap_dir = os.path.join(SNAPSHOTS_DIR, snap_name)
        os.makedirs(snap_dir, exist_ok=True)

        meta = {
            'id': snap_name,
            'label': label or f'Snapshot {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            'created': datetime.now().isoformat(),
            'hostname': _read_file_safe('/etc/hostname', 'unknown').strip(),
            'includes': {
                'ethos': include_ethos,
                'docker': include_docker,
                'volumes': include_volumes,
                'system': include_system,
                'userdirs': include_userdirs,
                'vms': include_vms,
                'models': include_models,
            },
            'docker_projects': [],
            'docker_volumes': [],
            'docker_containers': 0,
        }

        steps_total = sum([include_ethos, include_system, include_userdirs, include_docker, include_volumes])
        step_i = 0

        # ── 1. EthOS config & data ──
        if include_ethos:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up EthOS...', log='Copying EthOS configuration...')

            ethos_root = os.environ.get('ETHOS_ROOT', f'/home/{get_ethos_user()}/docker/nasos')
            ethos_dir = os.path.join(snap_dir, 'ethos')
            os.makedirs(ethos_dir, exist_ok=True)

            # ethos.env
            env_file = os.path.join(ethos_root, 'ethos.env')
            if os.path.isfile(env_file):
                import shutil
                shutil.copy2(env_file, os.path.join(ethos_dir, 'ethos.env'))

            # data/ (settings, configs, profiles DB, etc.)
            data_src = os.path.join(ethos_root, 'data')
            if os.path.isdir(data_src):
                exclude_dirs = ['novnc', 'updates']
                if not include_vms:
                    exclude_dirs.append('vms')
                if not include_models:
                    exclude_dirs.append('models')
                tar_cmd = ['tar', '-czf', os.path.join(ethos_dir, 'data.tar.gz'),
                           '-C', ethos_root]
                for ed in exclude_dirs:
                    tar_cmd.extend(['--exclude', f'data/{ed}'])
                tar_cmd.append('data')
                subprocess.run(tar_cmd, capture_output=True, timeout=3600)
                excluded_note = ', '.join(exclude_dirs) if exclude_dirs else 'none'
                _snap_update(log=f'EthOS data/ — excluding: {excluded_note}')

            # install.conf
            for extra in ['install.conf']:
                src = os.path.join(ethos_root, extra)
                if os.path.isfile(src):
                    import shutil
                    shutil.copy2(src, os.path.join(ethos_dir, extra))

            _snap_update(log='EthOS — done')

        # ── 2. System configs ──
        if include_system:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up system configuration...', log='Copying system configuration...')

            sys_dir = os.path.join(snap_dir, 'system')
            os.makedirs(sys_dir, exist_ok=True)

            import shutil
            # Hostname, hosts, timezone, locale
            for f in ['/etc/hostname', '/etc/hosts', '/etc/timezone',
                       '/etc/default/locale', '/etc/resolv.conf']:
                if os.path.isfile(f):
                    dest = os.path.join(sys_dir, os.path.basename(f))
                    try:
                        shutil.copy2(f, dest)
                    except Exception:
                        pass

            # Timezone (symlink)
            try:
                tz = os.path.realpath('/etc/localtime')
                with open(os.path.join(sys_dir, 'timezone_link'), 'w') as tf:
                    tf.write(tz)
            except Exception:
                pass

            # NetworkManager connections
            nm_dir = '/etc/NetworkManager/system-connections'
            if os.path.isdir(nm_dir):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'nm-connections.tar.gz'),
                     '-C', '/etc/NetworkManager', 'system-connections'],
                    capture_output=True, timeout=30
                )
                _snap_update(log='NetworkManager connections saved')

            # Nginx sites
            nginx_avail = '/etc/nginx/sites-available'
            if os.path.isdir(nginx_avail):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'nginx-sites.tar.gz'),
                     '-C', '/etc/nginx', 'sites-available', 'sites-enabled'],
                    capture_output=True, timeout=30
                )
                _snap_update(log='Nginx sites saved')

            # SSL certs (Let's Encrypt)
            le_dir = '/etc/letsencrypt'
            if os.path.isdir(le_dir):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'letsencrypt.tar.gz'),
                     '-C', '/etc', 'letsencrypt'],
                    capture_output=True, timeout=60
                )
                _snap_update(log='Let\'s Encrypt certs saved')

            # Samba config
            smb_conf = '/etc/samba/smb.conf'
            if os.path.isfile(smb_conf):
                shutil.copy2(smb_conf, os.path.join(sys_dir, 'smb.conf'))
                _snap_update(log='Samba config saved')

            # fstab
            if os.path.isfile('/etc/fstab'):
                shutil.copy2('/etc/fstab', os.path.join(sys_dir, 'fstab'))

            # Crontab
            try:
                r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    with open(os.path.join(sys_dir, 'crontab'), 'w') as cf:
                        cf.write(r.stdout)
            except Exception:
                pass

            _snap_update(log='System config — done')

        # ── 2b. User home directories ──
        if include_userdirs:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up user directories...', log='Archiving /home...')

            users_dir = os.path.join(snap_dir, 'userdirs')
            os.makedirs(users_dir, exist_ok=True)

            try:
                home_entries = [d for d in os.listdir('/home')
                                if os.path.isdir(os.path.join('/home', d))
                                and not d.startswith('.')]
            except OSError:
                home_entries = []

            saved_users = []
            for uname in home_entries:
                user_home = os.path.join('/home', uname)
                _snap_update(log=f'  User: {uname}')
                tar_path = os.path.join(users_dir, f'{uname}.tar.gz')
                tar_cmd = [
                    'tar', '-czf', tar_path, '-C', '/home',
                    '--exclude', f'{uname}/.cache',
                    '--exclude', f'{uname}/.local/share/Trash',
                    '--exclude', f'{uname}/.npm',
                    '--exclude', f'{uname}/.venv',
                    '--exclude', f'{uname}/venv',
                    uname,
                ]
                try:
                    r = subprocess.run(tar_cmd, capture_output=True, timeout=3600)
                    if r.returncode == 0 or os.path.isfile(tar_path):
                        sz = os.path.getsize(tar_path) if os.path.isfile(tar_path) else 0
                        saved_users.append(uname)
                        _snap_update(log=f'  {uname}: {sz / 1048576:.1f} MB')
                except subprocess.TimeoutExpired:
                    _snap_update(log=f'  {uname}: TIMEOUT (skipped)')
                except Exception as e:
                    _snap_update(log=f'  {uname}: error — {e}')

            meta['user_dirs'] = saved_users
            _snap_update(log=f'User directories: {len(saved_users)} users archived')

        # ── 3. Docker compose projects ──
        if include_docker:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up Docker...', log='Saving Docker projects...')

            docker_dir = os.path.join(snap_dir, 'docker')
            os.makedirs(docker_dir, exist_ok=True)

            if _docker_ok():
                compose_root = os.environ.get('COMPOSE_ROOT', f'/home/{get_ethos_user()}/docker')
                projects = []
                if os.path.isdir(compose_root):
                    for entry in sorted(os.listdir(compose_root)):
                        proj_path = os.path.join(compose_root, entry)
                        if not os.path.isdir(proj_path):
                            continue
                        compose_file = None
                        for cf_name in ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml']:
                            candidate = os.path.join(proj_path, cf_name)
                            if os.path.isfile(candidate):
                                compose_file = candidate
                                break
                        if not compose_file:
                            continue

                        import shutil
                        proj_bak = os.path.join(docker_dir, 'projects', entry)
                        os.makedirs(proj_bak, exist_ok=True)
                        shutil.copy2(compose_file, os.path.join(proj_bak, os.path.basename(compose_file)))
                        # .env file
                        env_f = os.path.join(proj_path, '.env')
                        if os.path.isfile(env_f):
                            shutil.copy2(env_f, os.path.join(proj_bak, '.env'))
                        # Additional env files referenced in compose
                        for extra_env in _find_env_files(compose_file):
                            extra_path = os.path.join(proj_path, extra_env)
                            if os.path.isfile(extra_path):
                                shutil.copy2(extra_path, os.path.join(proj_bak, extra_env))

                        projects.append(entry)
                        _snap_update(log=f'  Project: {entry}')

                meta['docker_projects'] = projects

                # Container list (for reference — what was running)
                out, rc = _docker_cmd(['ps', '-a', '--format', '{{json .}}', '--no-trunc'])
                if rc == 0:
                    containers = []
                    for line in out.split('\n'):
                        line = line.strip()
                        if line:
                            try:
                                containers.append(json.loads(line))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'containers.json'), 'w') as cf:
                        json.dump(containers, cf, indent=2)
                    meta['docker_containers'] = len(containers)
                    _snap_update(log=f'Containers: {len(containers)}')

                # Docker images list
                out, rc = _docker_cmd(['images', '--format', '{{json .}}'])
                if rc == 0:
                    images = []
                    for line in out.split('\n'):
                        line = line.strip()
                        if line:
                            try:
                                images.append(json.loads(line))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'images.json'), 'w') as cf:
                        json.dump(images, cf, indent=2)
                    _snap_update(log=f'Docker images: {len(images)}')

                # Networks
                out, rc = _docker_cmd(['network', 'ls', '--format', '{{json .}}'])
                if rc == 0:
                    networks = []
                    for line in out.split('\n'):
                        if line.strip():
                            try:
                                networks.append(json.loads(line.strip()))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'networks.json'), 'w') as cf:
                        json.dump(networks, cf, indent=2)

            else:
                _snap_update(log='Docker unavailable — skipping')

            _snap_update(log='Docker projects — done')

        # ── 4. Docker volumes ──
        if include_volumes and include_docker:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backing up Docker volumes...', log='Exporting Docker volumes...')

            vol_dir = os.path.join(snap_dir, 'docker', 'volumes')
            os.makedirs(vol_dir, exist_ok=True)

            if _docker_ok():
                out, rc = _docker_cmd(['volume', 'ls', '-q'])
                vol_names = [v.strip() for v in out.split('\n') if v.strip()] if rc == 0 else []
                saved_vols = []

                for vi, vol in enumerate(vol_names):
                    inspect_out, irc = _docker_cmd(['volume', 'inspect', vol])
                    if irc != 0:
                        continue
                    try:
                        vinfo = json.loads(inspect_out)
                        if isinstance(vinfo, list) and vinfo:
                            vinfo = vinfo[0]
                        mount = vinfo.get('Mountpoint', '')
                    except Exception:
                        mount = ''

                    vol_size = 0
                    if mount and os.path.isdir(mount):
                        try:
                            r = subprocess.run(
                                ['du', '-sb', mount],
                                capture_output=True, text=True, timeout=10
                            )
                            if r.returncode == 0:
                                vol_size = int(r.stdout.split()[0])
                        except Exception:
                            pass

                    size_mb = vol_size / (1024 * 1024)
                    if vol_size > 5 * 1024 * 1024 * 1024:  # >5GB
                        _snap_update(log=f'  Skipping {vol} ({size_mb:.0f} MB — too large)')
                        continue

                    vol_pct = int(pct + (vi / max(len(vol_names), 1)) * (90 - pct))
                    _snap_update(
                        percent=vol_pct,
                        message=f'Eksport: {vol} ({size_mb:.1f} MB)',
                        log=f'  Volume: {vol} ({size_mb:.1f} MB)'
                    )

                    archive = os.path.join(vol_dir, f'{vol}.tar.gz')
                    result = subprocess.run(
                        ['docker', 'run', '--rm',
                         '-v', f'{vol}:/volume_data:ro',
                         '-v', f'{vol_dir}:/backup',
                         'alpine',
                         'tar', '-czf', f'/backup/{vol}.tar.gz', '-C', '/volume_data', '.'],
                        capture_output=True, text=True, timeout=600
                    )
                    if result.returncode == 0 and os.path.isfile(archive):
                        saved_vols.append(vol)
                    else:
                        _snap_update(log=f'  Export ERROR {vol}: {result.stderr[:200]}')

                meta['docker_volumes'] = saved_vols
                _snap_update(log=f'Volumes: {len(saved_vols)}/{len(vol_names)} exported')
            else:
                _snap_update(log='Docker unavailable — skipping volumes')

        # ── Save metadata ──
        _snap_update(percent=92, message='Saving metadata...')
        with open(os.path.join(snap_dir, 'meta.json'), 'w') as mf:
            json.dump(meta, mf, indent=2)

        # ── Copy to USB if requested ──
        if dest_type == 'usb' and dest_path:
            _snap_update(percent=93, message='Copying to USB...', log=f'USB transfer: {dest_path}')
            usb_snap_dir = os.path.join(dest_path, 'ethos-snapshots')
            os.makedirs(usb_snap_dir, exist_ok=True)
            archive_name = f'{snap_name}.tar.gz'
            archive_path = os.path.join(usb_snap_dir, archive_name)
            r = subprocess.run(
                ['tar', '-czf', archive_path, '-C', SNAPSHOTS_DIR, snap_name],
                capture_output=True, timeout=600
            )
            if r.returncode == 0:
                sz = os.path.getsize(archive_path) if os.path.isfile(archive_path) else 0
                _snap_update(log=f'USB: saved {archive_name} ({sz / 1048576:.1f} MB)')
            else:
                _snap_update(log=f'USB: write error — {r.stderr[:200]}')

        _snap_update(status='done', percent=100, message=f'Snapshot "{meta["label"]}" ready!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Error: {e}', log=f'EXCEPTION: {e}')
        import traceback
        main.logger.exception("Snapshot creation failed")
