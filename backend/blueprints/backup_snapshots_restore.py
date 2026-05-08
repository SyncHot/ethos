"""
EthOS — Backup Snapshots Restore Worker
Extracts snapshot restoration logic from backup_snapshots.py.
Contains _restore_snapshot_worker() function (lines 1667-1950).
"""

import os
import subprocess
import json
import time

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import get_ethos_user


def _get_main():
    """Get the main backup_snapshots module at runtime."""
    return sys.modules.get('blueprints.backup_snapshots')


def _restore_snapshot_worker(snap_dir, restore_docker, restore_volumes,
                              restore_ethos, restore_system):
    """Background task: restores system from a snapshot."""
    main = _get_main()
    if not main:
        return

    _snap_update = main._snap_update
    _docker_ok = main._docker_ok
    _docker_cmd = main._docker_cmd
    _read_file_safe = main._read_file_safe
    _containers_using_volume = main._containers_using_volume
    logger = main.logger

    try:
        meta_file = os.path.join(snap_dir, 'meta.json')
        if not os.path.isfile(meta_file):
            _snap_update(status='error', message='No meta.json in snapshot')
            return

        with open(meta_file) as f:
            meta = json.load(f)

        _snap_update(percent=5, message='Restore started...', log=f'Restoring: {meta.get("label", "?")}')

        steps_total = sum([restore_ethos, restore_system, restore_docker, restore_volumes])
        step_i = 0

        # ── 1. EthOS config & data ──
        if restore_ethos:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring EthOS...', log='Restoring EthOS configuration...')

            ethos_root = os.environ.get('ETHOS_ROOT', f'/home/{get_ethos_user()}/docker/nasos')
            ethos_bak = os.path.join(snap_dir, 'ethos')

            if os.path.isdir(ethos_bak):
                import shutil
                # ethos.env
                env_src = os.path.join(ethos_bak, 'ethos.env')
                if os.path.isfile(env_src):
                    shutil.copy2(env_src, os.path.join(ethos_root, 'ethos.env'))
                    _snap_update(log='  ethos.env restored')

                # data/
                data_tar = os.path.join(ethos_bak, 'data.tar.gz')
                if os.path.isfile(data_tar):
                    data_current = os.path.join(ethos_root, 'data')
                    if os.path.isdir(data_current):
                        pre_restore = os.path.join(ethos_root, 'data.pre-restore')
                        if os.path.isdir(pre_restore):
                            shutil.rmtree(pre_restore, ignore_errors=True)
                        shutil.copytree(data_current, pre_restore)
                        _snap_update(log='  Current data/ copied to data.pre-restore/')
                    subprocess.run(
                        ['tar', '-xzf', data_tar, '-C', ethos_root],
                        capture_output=True, timeout=600
                    )
                    _snap_update(log='  data/ restored')

                # install.conf
                for extra in ['install.conf']:
                    src = os.path.join(ethos_bak, extra)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(ethos_root, extra))

                _snap_update(log='EthOS — restored')
            else:
                _snap_update(log='No EthOS data in snapshot')

        # ── 2. System configs ──
        if restore_system:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring system configuration...', log='Restoring system...')

            sys_bak = os.path.join(snap_dir, 'system')
            if os.path.isdir(sys_bak):
                import shutil
                # Hostname
                hostname_f = os.path.join(sys_bak, 'hostname')
                if os.path.isfile(hostname_f):
                    shutil.copy2(hostname_f, '/etc/hostname')
                    hostname = _read_file_safe(hostname_f, '').strip()
                    if hostname:
                        subprocess.run(['hostnamectl', 'set-hostname', hostname],
                                       capture_output=True, timeout=10)
                    _snap_update(log=f'  Hostname: {hostname}')

                # hosts
                hosts_f = os.path.join(sys_bak, 'hosts')
                if os.path.isfile(hosts_f):
                    shutil.copy2(hosts_f, '/etc/hosts')

                # Timezone
                tz_link = os.path.join(sys_bak, 'timezone_link')
                if os.path.isfile(tz_link):
                    tz_path = _read_file_safe(tz_link, '').strip()
                    if tz_path and os.path.isfile(tz_path):
                        try:
                            os.remove('/etc/localtime')
                        except Exception:
                            pass
                        os.symlink(tz_path, '/etc/localtime')
                        _snap_update(log=f'  Timezone: {tz_path}')

                # NetworkManager connections
                nm_tar = os.path.join(sys_bak, 'nm-connections.tar.gz')
                if os.path.isfile(nm_tar):
                    subprocess.run(
                        ['tar', '-xzf', nm_tar, '-C', '/etc/NetworkManager'],
                        capture_output=True, timeout=30
                    )
                    subprocess.run(['nmcli', 'connection', 'reload'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  NetworkManager connections restored')

                # Nginx sites
                nginx_tar = os.path.join(sys_bak, 'nginx-sites.tar.gz')
                if os.path.isfile(nginx_tar):
                    subprocess.run(
                        ['tar', '-xzf', nginx_tar, '-C', '/etc/nginx'],
                        capture_output=True, timeout=30
                    )
                    subprocess.run(['systemctl', 'reload', 'nginx'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Nginx sites restored')

                # Let's Encrypt
                le_tar = os.path.join(sys_bak, 'letsencrypt.tar.gz')
                if os.path.isfile(le_tar):
                    subprocess.run(
                        ['tar', '-xzf', le_tar, '-C', '/etc'],
                        capture_output=True, timeout=60
                    )
                    _snap_update(log='  Let\'s Encrypt certs restored')

                # Samba
                smb_f = os.path.join(sys_bak, 'smb.conf')
                if os.path.isfile(smb_f):
                    shutil.copy2(smb_f, '/etc/samba/smb.conf')
                    subprocess.run(['systemctl', 'restart', 'smbd'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Samba config restored')

                # fstab
                fstab_f = os.path.join(sys_bak, 'fstab')
                if os.path.isfile(fstab_f):
                    shutil.copy2(fstab_f, '/etc/fstab')
                    _snap_update(log='  fstab restored (reboot required)')

                # Crontab
                cron_f = os.path.join(sys_bak, 'crontab')
                if os.path.isfile(cron_f):
                    subprocess.run(['crontab', cron_f],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Crontab restored')

                _snap_update(log='System config — restored')
            else:
                _snap_update(log='No system configuration in snapshot')

        # ── 2b. User home directories ──
        users_bak = os.path.join(snap_dir, 'userdirs')
        if os.path.isdir(users_bak):
            step_i += 1
            pct = int(step_i / steps_total * 85) if steps_total else 50
            _snap_update(percent=pct, message='Restoring user directories...', log='Restoring /home...')

            for tarfile_name in sorted(os.listdir(users_bak)):
                if not tarfile_name.endswith('.tar.gz'):
                    continue
                uname = tarfile_name.replace('.tar.gz', '')
                _snap_update(log=f'  Restoring user: {uname}')
                try:
                    subprocess.run(
                        ['tar', '-xzf', os.path.join(users_bak, tarfile_name), '-C', '/home'],
                        capture_output=True, timeout=3600
                    )
                    _snap_update(log=f'  {uname} — restored')
                except Exception as e:
                    _snap_update(log=f'  {uname} — error: {e}')

            _snap_update(log='User directories — restored')

        # ── 3. Docker compose projects ──
        if restore_docker:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring Docker...', log='Restoring Docker projects...')

            docker_bak = os.path.join(snap_dir, 'docker')
            proj_bak = os.path.join(docker_bak, 'projects')

            if os.path.isdir(proj_bak) and _docker_ok():
                import shutil
                compose_root = os.environ.get('COMPOSE_ROOT', f'/home/{get_ethos_user()}/docker')
                restored = 0

                for proj_name in sorted(os.listdir(proj_bak)):
                    proj_src = os.path.join(proj_bak, proj_name)
                    if not os.path.isdir(proj_src):
                        continue

                    proj_dest = os.path.join(compose_root, proj_name)
                    os.makedirs(proj_dest, exist_ok=True)

                    # Copy compose file + env files
                    for fn in os.listdir(proj_src):
                        src = os.path.join(proj_src, fn)
                        dst = os.path.join(proj_dest, fn)
                        if os.path.isfile(src):
                            shutil.copy2(src, dst)

                    # Pull images + start project
                    _snap_update(log=f'  Project {proj_name}: pulling images...')
                    subprocess.run(
                        ['docker', 'compose', 'pull'],
                        capture_output=True, timeout=300, cwd=proj_dest
                    )
                    r = subprocess.run(
                        ['docker', 'compose', 'up', '-d'],
                        capture_output=True, text=True, timeout=120, cwd=proj_dest
                    )
                    if r.returncode == 0:
                        _snap_update(log=f'  Project {proj_name}: started ✓')
                        restored += 1
                    else:
                        _snap_update(log=f'  Project {proj_name}: ERROR — {r.stderr[:200]}')

                _snap_update(log=f'Docker projects: {restored} restored')
            else:
                _snap_update(log='No Docker projects in snapshot or Docker unavailable')

        # ── 4. Docker volumes ──
        if restore_volumes and restore_docker:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Restoring Docker volumes...', log='Importing Docker volumes...')

            vol_bak = os.path.join(snap_dir, 'docker', 'volumes')
            if os.path.isdir(vol_bak) and _docker_ok():
                restored_vols = 0
                vol_archives = [f for f in os.listdir(vol_bak) if f.endswith('.tar.gz')]

                for vi, vf in enumerate(vol_archives):
                    vol_name = vf.replace('.tar.gz', '')
                    vol_pct = int(pct + (vi / max(len(vol_archives), 1)) * (85 - pct))
                    _snap_update(
                        percent=vol_pct,
                        message=f'Import: {vol_name}',
                        log=f'  Volume: {vol_name}'
                    )

                    _docker_cmd(['volume', 'create', vol_name])

                    containers_using = _containers_using_volume(vol_name)
                    stopped = []
                    for cid in containers_using:
                        _docker_cmd(['stop', cid], timeout=30)
                        stopped.append(cid)
                        _snap_update(log=f'    Stopped container {cid[:12]}')

                    archive_path = os.path.join(vol_bak, vf)
                    r = subprocess.run(
                        ['docker', 'run', '--rm',
                         '-v', f'{vol_name}:/volume_data',
                         '-v', f'{vol_bak}:/backup:ro',
                         'alpine', 'sh', '-c',
                         f'rm -rf /volume_data/* && tar -xzf /backup/{vf} -C /volume_data'],
                        capture_output=True, text=True, timeout=600
                    )
                    if r.returncode == 0:
                        restored_vols += 1
                    else:
                        _snap_update(log=f'    Import ERROR {vol_name}: {r.stderr[:200]}')

                    # Restart stopped containers
                    for cid in stopped:
                        _docker_cmd(['start', cid], timeout=30)
                        _snap_update(log=f'    Restarted {cid[:12]}')

                _snap_update(log=f'Volumes: {restored_vols}/{len(vol_archives)} restored')
            else:
                _snap_update(log='No volumes in snapshot or Docker unavailable')

        _snap_update(status='done', percent=100,
                     message=f'Restoring "{meta.get("label", "")}" completed!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Error: {e}', log=f'EXCEPTION: {e}')
        logger.exception("Snapshot restore failed")
