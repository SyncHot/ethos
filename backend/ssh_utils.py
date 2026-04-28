"""
EthOS — Shared SSH Utilities
Consolidates paramiko SSH connection logic used by backup.py and settings.py.
"""

import os

try:
    import paramiko
    HAS_SSH = True
except ImportError:
    paramiko = None
    HAS_SSH = False


def get_ssh_client(host, port=22, username='root', *,
                   password=None, key_path=None, timeout=10):
    """Create and return a connected ``paramiko.SSHClient``.

    Raises ``RuntimeError`` if paramiko is not installed.
    Raises ``paramiko.AuthenticationException`` etc. on failure.
    """
    if not HAS_SSH:
        raise RuntimeError('paramiko not installed')

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kw = {'hostname': host, 'port': port, 'username': username, 'timeout': timeout}
    if key_path and os.path.exists(key_path):
        kw['key_filename'] = key_path
    elif password:
        kw['password'] = password

    ssh.connect(**kw)
    return ssh


def ssh_resolve_home(ssh):
    """Resolve ``$HOME`` on the remote server, or ``None``."""
    try:
        _, so, _ = ssh.exec_command('echo $HOME')
        home = so.read().decode().strip()
        return home if home else None
    except Exception:
        return None


def ssh_ensure_writable_dir(ssh, path):
    """Create *path* on remote and verify write access.

    Returns ``(ok: bool, resolved_path: str)``.
    """
    try:
        _, so, _ = ssh.exec_command(
            f'mkdir -p {path} 2>&1 && test -w {path} && echo OK || echo FAIL'
        )
        result = so.read().decode().strip()
        return result == 'OK', path
    except Exception:
        return False, path


def ssh_exec(ssh, cmd, timeout=15):
    """Run *cmd* on the SSH session, return ``(stdout, stderr, exit_code)``."""
    _, so, se = ssh.exec_command(cmd, timeout=timeout)
    exit_code = so.channel.recv_exit_status()
    return so.read().decode(), se.read().decode(), exit_code


def ssh_remote_disk_info(ssh):
    """Get remote disk usage (total/used/free) via ``df``.

    Returns a dict or ``None``.
    """
    try:
        stdout, _, _ = ssh_exec(ssh, "df -B1 / | tail -1 | awk '{print $2,$3,$4}'")
        parts = stdout.strip().split()
        if len(parts) >= 3:
            total, used, free = int(parts[0]), int(parts[1]), int(parts[2])
            return {
                'total': total, 'used': used, 'free': free,
                'percent_used': round((used / total * 100) if total else 0, 1),
            }
    except Exception:
        pass
    return None
