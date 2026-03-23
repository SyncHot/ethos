"""
EthOS — Package Manager Blueprint
Apt package management on the host system.
"""

import subprocess
import re
import shlex
import os
import fcntl
import select
import time
import sys
from flask import Blueprint, jsonify, request, Response, stream_with_context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, q as _q, NATIVE_MODE

packages_bp = Blueprint('packages', __name__, url_prefix='/api/packages')

_NSENTER = 'nsenter --target 1 --mount --uts --ipc --net --pid --'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shell_quote(s):
    return shlex.quote(s)


def host_run(cmd, timeout=120):
    return _host_run(cmd, timeout=timeout)


def host_run_stream(cmd):
    """Stream command output line by line from host, with keepalive pings."""
    full_cmd = f"bash -c {_q(cmd)}"
    proc = subprocess.Popen(
        full_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    # Set stdout to non-blocking
    fd = proc.stdout.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    buf = b''
    while True:
        ready, _, _ = select.select([fd], [], [], 5.0)  # 5s timeout
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b''
            if not chunk:
                # EOF
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                yield line.decode('utf-8', errors='replace') + '\n'
        else:
            # No output for 5s — send keepalive
            yield '__KEEPALIVE__\n'

    # Flush remaining buffer
    if buf:
        yield buf.decode('utf-8', errors='replace') + '\n'
    proc.wait()
    yield f"__EXIT_CODE__:{proc.returncode}\n"


# ---------------------------------------------------------------------------
# API: System info
# ---------------------------------------------------------------------------

# Docker-related packages that must NOT be upgraded from inside a container
# (only relevant in Docker mode — in native mode no hold needed)
_DOCKER_PKGS = ()


@packages_bp.route('/stats')
def package_stats():
    """Get package counts and apt cache stats."""
    try:
        r = host_run("dpkg -l 2>/dev/null | grep '^ii' | wc -l")
        installed = int(r.stdout.strip()) if r.returncode == 0 else 0

        r2 = host_run("apt list --upgradable 2>/dev/null | grep -c upgradable || true")
        upgradable_raw = r2.stdout.strip().split('\n')[0].strip()
        upgradable = int(upgradable_raw) if upgradable_raw.isdigit() else 0

        r3 = host_run("du -sh /var/cache/apt/archives/ 2>/dev/null | cut -f1")
        cache_size = r3.stdout.strip() if r3.returncode == 0 else '0B'

        r4 = host_run("cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'\"' -f2")
        os_name = r4.stdout.strip() if r4.returncode == 0 else 'Ubuntu'

        r5 = host_run("apt-get update --print-uris 2>/dev/null | head -1; stat -c '%Y' /var/lib/apt/lists/lock 2>/dev/null")
        last_update_ts = r5.stdout.strip().split('\n')[-1] if r5.returncode == 0 else ''

        # Check dpkg state
        dpkg_ok = True
        rd = host_run("dpkg --audit 2>&1")
        if rd.returncode != 0 or 'dpkg was interrupted' in (rd.stderr or '') or rd.stdout.strip():
            dpkg_ok = False

        return jsonify({
            'installed': installed,
            'upgradable': upgradable,
            'cache_size': cache_size,
            'os_name': os_name,
            'last_update': last_update_ts,
            'dpkg_ok': dpkg_ok,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# API: List installed packages
# ---------------------------------------------------------------------------

@packages_bp.route('/installed')
def list_installed():
    """List installed packages with version and description."""
    try:
        r = host_run(
            "dpkg-query -W -f='${Package}\\t${Version}\\t${Installed-Size}\\t${Status}\\t${binary:Summary}\\n' 2>/dev/null"
            " | grep 'install ok installed'"
        )
        packages = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('\t')
            if len(parts) >= 5:
                packages.append({
                    'name': parts[0],
                    'version': parts[1],
                    'size': int(parts[2]) * 1024 if parts[2].isdigit() else 0,  # KB -> bytes
                    'description': parts[4]
                })
        return jsonify(packages)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# API: List upgradable
# ---------------------------------------------------------------------------

@packages_bp.route('/upgradable')
def list_upgradable():
    """List packages that can be upgraded."""
    try:
        r = host_run("apt list --upgradable 2>/dev/null")
        packages = []
        for line in r.stdout.strip().split('\n'):
            if 'upgradable' not in line:
                continue
            # Format: name/repo version arch [upgradable from: old_version]
            m = re.match(r'^(\S+)/\S+\s+(\S+)\s+\S+\s+\[upgradable from:\s+(\S+)\]', line)
            if m:
                packages.append({
                    'name': m.group(1),
                    'new_version': m.group(2),
                    'old_version': m.group(3)
                })
        return jsonify(packages)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# API: Search packages
# ---------------------------------------------------------------------------

@packages_bp.route('/search')
def search_packages():
    """Search apt cache for packages."""
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify([])
    try:
        safe_q = re.sub(r'[^a-zA-Z0-9\-_.+]', '', q)
        r = host_run(
            f"apt-cache search {_shell_quote(safe_q)} 2>/dev/null | head -100"
        )
        # Check which are installed
        r2 = host_run(f"dpkg -l 2>/dev/null | grep '^ii' | awk '{{print $2}}'")
        installed_set = set(r2.stdout.strip().split('\n')) if r2.returncode == 0 else set()

        packages = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split(' - ', 1)
            if len(parts) == 2:
                name = parts[0].strip()
                packages.append({
                    'name': name,
                    'description': parts[1].strip(),
                    'installed': name in installed_set
                })
        return jsonify(packages)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# API: Package details
# ---------------------------------------------------------------------------

@packages_bp.route('/info/<package_name>')
def package_info(package_name):
    """Get details about a specific package."""
    safe_name = re.sub(r'[^a-zA-Z0-9\-_.+:]', '', package_name)
    try:
        r = host_run(f"apt-cache show {_shell_quote(safe_name)} 2>/dev/null | head -60")
        if r.returncode != 0:
            return jsonify({'error': 'Package not found'}), 404

        info = {}
        current_key = None
        for line in r.stdout.split('\n'):
            if ': ' in line and not line.startswith(' '):
                key, val = line.split(': ', 1)
                info[key.strip()] = val.strip()
                current_key = key.strip()
            elif line.startswith(' ') and current_key == 'Description':
                info['Description'] = info.get('Description', '') + '\n' + line.strip()

        # Check if installed
        r2 = host_run(f"dpkg -l {_shell_quote(safe_name)} 2>/dev/null | grep '^ii'")
        info['is_installed'] = bool(r2.stdout.strip())

        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# API: Actions (streamed output via SSE)
# ---------------------------------------------------------------------------

@packages_bp.route('/update', methods=['POST'])
def apt_update():
    """Run apt-get update (streamed)."""
    def generate():
        for line in host_run_stream("DEBIAN_FRONTEND=noninteractive apt-get update 2>&1"):
            yield f"data: {line}\n\n"
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@packages_bp.route('/upgrade', methods=['POST'])
def apt_upgrade():
    """Run apt-get upgrade -y (streamed). In Docker mode, Docker packages are held."""
    if _DOCKER_PKGS:
        hold_cmds = ' && '.join(f'apt-mark hold {p} 2>/dev/null' for p in _DOCKER_PKGS)
        unhold_cmds = ' && '.join(f'apt-mark unhold {p} 2>/dev/null' for p in _DOCKER_PKGS)
        cmd = (
            f"{hold_cmds}; "
            f"DEBIAN_FRONTEND=noninteractive apt-get upgrade -y 2>&1; "
            f"RET=$?; {unhold_cmds}; exit $RET"
        )
    else:
        cmd = "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y 2>&1"
    def generate():
        for line in host_run_stream(cmd):
            yield f"data: {line}\n\n"
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@packages_bp.route('/install', methods=['POST'])
def apt_install():
    """Install one or more packages (streamed)."""
    data = request.json or {}
    names = data.get('packages', [])
    if not names:
        return jsonify({'error': 'No packages specified'}), 400
    # Sanitize
    safe = [re.sub(r'[^a-zA-Z0-9\-_.+:]', '', n) for n in names]
    safe = [n for n in safe if n]
    if not safe:
        return jsonify({'error': 'Invalid package names'}), 400

    pkg_str = ' '.join(_shell_quote(n) for n in safe)

    def generate():
        for line in host_run_stream(
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg_str} 2>&1"
        ):
            yield f"data: {line}\n\n"
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@packages_bp.route('/remove', methods=['POST'])
def apt_remove():
    """Remove a package (streamed)."""
    data = request.json or {}
    names = data.get('packages', [])
    if not names:
        return jsonify({'error': 'No packages specified'}), 400
    safe = [re.sub(r'[^a-zA-Z0-9\-_.+:]', '', n) for n in names]
    safe = [n for n in safe if n]
    if not safe:
        return jsonify({'error': 'Invalid package names'}), 400

    pkg_str = ' '.join(_shell_quote(n) for n in safe)

    def generate():
        for line in host_run_stream(
            f"DEBIAN_FRONTEND=noninteractive apt-get remove -y {pkg_str} 2>&1"
        ):
            yield f"data: {line}\n\n"
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@packages_bp.route('/clean', methods=['POST'])
def apt_clean():
    """Clean apt cache and autoremove."""
    def generate():
        for line in host_run_stream(
            "DEBIAN_FRONTEND=noninteractive apt-get autoremove -y 2>&1 && apt-get clean 2>&1 && echo '--- Cleanup complete ---'"
        ):
            yield f"data: {line}\n\n"
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@packages_bp.route('/fix-dpkg', methods=['POST'])
def fix_dpkg():
    """Run dpkg --configure -a (streamed)."""
    def generate():
        yield f"data: Running dpkg --configure -a...\n\n"
        for line in host_run_stream("dpkg --configure -a 2>&1"):
            yield f"data: {line}\n\n"
    return Response(stream_with_context(generate()), mimetype='text/event-stream')
