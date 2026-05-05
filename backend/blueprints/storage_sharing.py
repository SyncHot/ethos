"""EthOS Storage — File Sharing Routes

Routes: /api/storage/samba/*, /api/storage/nfs/*, /api/storage/dlna/*,
        /api/storage/sftp/*, /api/storage/webdav/*, /api/storage/ftp/*
"""
import json
import os
import re
import sys
from flask import jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path, q as _q_imported, apt_install as _apt_install, claim_dep, release_dep, ufw_allow, ufw_delete
from blueprints.admin_required import admin_required
from blueprints.storage import (
    storage_bp,
    host_run, host_run_stream, Q,
    _host_write_json, _host_write_script, _host_write_file,
)

# ---------------------------------------------------------------------------
# API – Samba
# ---------------------------------------------------------------------------

def _detect_lan_subnet():
    """Detect the primary LAN subnet (e.g. '192.168.1.0/24') from the default route interface."""
    try:
        r = host_run("ip -4 route show default 2>/dev/null | awk '{print $5}' | head -1")
        iface = r.stdout.strip()
        if not iface:
            return '192.168.0.0/16'
        r2 = host_run(f"ip -4 -o addr show {Q(iface)} 2>/dev/null | awk '{{print $4}}' | head -1")
        cidr = r2.stdout.strip()
        if '/' not in cidr:
            return '192.168.0.0/16'
        import ipaddress
        net = ipaddress.ip_network(cidr, strict=False)
        return str(net)
    except Exception:
        return '192.168.0.0/16'


def _detect_lan_interfaces():
    """Return space-separated list of physical LAN interface names (excluding docker/veth)."""
    try:
        r = host_run("ip -4 -o addr show scope global | awk '{print $2}' | grep -v '^docker\\|^br-\\|^veth' | sort -u")
        ifaces = [i.strip() for i in r.stdout.strip().split('\n') if i.strip()]
        return ' '.join(ifaces) if ifaces else 'eth0'
    except Exception:
        return 'eth0'


def _detect_primary_user():
    """Return the primary non-root user and group for Samba force user/group."""
    try:
        r = host_run("awk -F: '$3 >= 1000 && $3 < 60000 {print $1; exit}' /etc/passwd")
        user = r.stdout.strip()
        if user:
            r2 = host_run(f"id -gn {Q(user)}")
            group = r2.stdout.strip() or user
            return user, group
    except Exception:
        pass
    return 'nobody', 'nogroup'


@storage_bp.route('/samba/status')
def samba_status():
    r = host_run("command -v smbd")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active smbd 2>/dev/null || systemctl is-active smb 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running})


@storage_bp.route('/samba/install', methods=['POST'])
@admin_required
def samba_install():
    r = host_run("command -v smbd")
    if r.returncode == 0:
        return jsonify({"status": "ok", "installed": True}), 200

    subnet = _detect_lan_subnet()
    ifaces = _detect_lan_interfaces()

    def generate():
        install_script = f"""
export DEBIAN_FRONTEND=noninteractive
echo '::STEP::Repairing package manager...'
dpkg --configure -a 2>/dev/null || true
echo '::STEP::Updating package lists...'
apt-get update -y 2>&1
echo '::STEP::Installing Samba...'
apt-get install -y samba samba-common-bin smbclient 2>&1
echo '::STEP::Configuring Samba...'
if [ ! -f /etc/samba/smb.conf ] || ! grep -q 'map to guest' /etc/samba/smb.conf 2>/dev/null; then
  cat > /etc/samba/smb.conf << 'SMBEOF'
[global]
    workgroup = WORKGROUP
    server string = EthOS NAS
    security = user
    map to guest = Bad User
    guest account = nobody
    server min protocol = SMB3
    server signing = mandatory
    smb encrypt = if_required
    interfaces = 127.0.0.0/8 {ifaces}
    bind interfaces only = yes
    hosts allow = 127.0.0.1 {subnet}
    hosts deny = 0.0.0.0/0
    log file = /var/log/samba/log.%m
    max log size = 1000
    logging = file
    log level = 2 auth:3
    dns proxy = no
    unix extensions = yes
    wide links = no
    follow symlinks = no
    load printers = no
    printing = bsd
    printcap name = /dev/null
    disable spoolss = yes

    # Performance tuning
    socket options = TCP_NODELAY IPTOS_LOWDELAY
    read raw = yes
    write raw = yes
    use sendfile = yes
    aio read size = 16384
    aio write size = 16384
    dead time = 15
SMBEOF
fi
echo '::STEP::Enabling Samba services...'
systemctl unmask smbd nmbd 2>/dev/null || true
systemctl enable smbd nmbd 2>/dev/null || true
systemctl start smbd nmbd 2>/dev/null || true
echo '::STEP::Samba installation complete!'
echo '::DONE::'
"""
        for line in host_run_stream(install_script):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = line.split(":")[1]
                yield f"data: {json.dumps({'type': 'exit', 'code': int(code)})}\n\n"
            elif line.startswith("::STEP::"):
                yield f"data: {json.dumps({'type': 'step', 'message': line[8:]})}\n\n"
            elif line.startswith("::DONE::"):
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@storage_bp.route('/samba/shares')
def samba_shares():
    r = host_run("cat /etc/samba/smb.conf 2>/dev/null || echo ''")
    shares = []
    current_share = None

    for line in r.stdout.splitlines():
        line = line.strip()
        match = re.match(r"^\[(.+)\]$", line)
        if match:
            name = match.group(1)
            if name.lower() != "global":
                current_share = {"name": name, "path": "", "writable": False, "guest_ok": False}
                shares.append(current_share)
            else:
                current_share = None
            continue
        if current_share and "=" in line:
            key, val = line.split("=", 1)
            key = key.strip().lower().replace(" ", "_")
            val = val.strip()
            if key == "path":
                current_share["path"] = val
            elif key in ("writable", "writeable"):
                current_share["writable"] = val.lower() == "yes"
            elif key == "guest_ok":
                current_share["guest_ok"] = val.lower() == "yes"

    return jsonify(shares)


@storage_bp.route('/samba/share', methods=['POST'])
@admin_required
def samba_share_add():
    # Check if Samba is installed first
    r_smb = host_run("command -v smbd")
    if r_smb.returncode != 0:
        return jsonify({"error": "Samba is not installed. Install it first in Disks → Samba."}), 400

    data = request.json or {}
    share_name = data.get("name", "").strip()
    share_path = data.get("path", "").strip()
    guest_ok = data.get("guest_ok", False)

    if not share_name or not share_path:
        return jsonify({"error": "name and path are required"}), 400

    # Validate share name: alphanumeric, hyphens, underscores, spaces only
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9 _\-]{0,63}$', share_name):
        return jsonify({"error": "Share name may only contain letters, numbers, spaces, - and _"}), 400

    # Validate share path: must be absolute, no traversal, no suspicious chars
    if not share_path.startswith('/'):
        return jsonify({"error": "Path must be absolute (start with /)"}), 400
    if '..' in share_path:
        return jsonify({"error": "Path cannot contain '..'"}), 400
    # Block sharing critical system directories
    _BLOCKED_PATHS = ('/', '/etc', '/proc', '/sys', '/dev', '/boot', '/root',
                       '/bin', '/sbin', '/usr', '/lib', '/lib64', '/var')
    norm_share = share_path.rstrip('/')
    if norm_share in _BLOCKED_PATHS or not norm_share:
        return jsonify({"error": f"Cannot share system path: {share_path}"}), 403

    uid_r = host_run("id -un")
    user = uid_r.stdout.strip() or "nobody"
    gid_r = host_run("id -gn")
    group = gid_r.stdout.strip() or "nogroup"
    # Service runs as root — detect the actual primary user
    if user == "root":
        user, group = _detect_primary_user()

    writable = data.get("writable", True)
    guest_str = "yes" if guest_ok else "no"
    writable_str = "yes" if writable else "no"

    # Use safe temp-file approach to avoid shell/Python injection
    subnet = _detect_lan_subnet()
    ifaces = _detect_lan_interfaces()
    share_conf = {
        "name": share_name,
        "path": share_path,
        "guest_ok": guest_str,
        "writable": writable_str,
        "user": user,
        "group": group,
        "subnet": subnet,
        "ifaces": ifaces,
    }
    script = """import json, re
NL = chr(10)
params = json.loads(open('/tmp/_samba_params.json').read())
name = params['name']
path = params['path']
guest = params['guest_ok']
user = params['user']
group = params['group']
subnet = params['subnet']
ifaces = params.get('ifaces', 'eth0')
try:
    conf = open('/etc/samba/smb.conf').read()
except FileNotFoundError:
    conf = ''

# Ensure [global] section has required settings
GLOBAL_DEFAULTS = {
    'workgroup': 'WORKGROUP',
    'server string': 'EthOS NAS',
    'security': 'user',
    'map to guest': 'Bad User',
    'guest account': 'nobody',
    'server min protocol': 'SMB3',
    'server signing': 'mandatory',
    'smb encrypt': 'if_required',
    'dns proxy': 'no',
    'interfaces': f'127.0.0.0/8 {ifaces}',
    'bind interfaces only': 'yes',
    'hosts allow': f'127.0.0.1 {subnet}',
    'hosts deny': '0.0.0.0/0',
}
if '[global]' not in conf:
    header = '[global]' + NL
    for k, v in GLOBAL_DEFAULTS.items():
        header += f'    {k} = {v}' + NL
    conf = header + NL + conf
else:
    # Inject missing keys into existing [global]
    gstart = conf.index('[global]')
    # Find end of global: next section or end of file
    next_bracket = conf.find(NL + '[', gstart + 8)
    gend = next_bracket + 1 if next_bracket != -1 else len(conf)
    global_block = conf[gstart:gend]
    additions = ''
    for k, v in GLOBAL_DEFAULTS.items():
        if k not in global_block.lower():
            additions += f'    {k} = {v}' + NL
    if additions:
        insert_pos = conf.index(NL, gstart) + 1
        conf = conf[:insert_pos] + additions + conf[insert_pos:]

# Update guest/interface settings in existing global (use [^\\n]+ to match full value)
conf = re.sub(r'(?im)^(\\s*map to guest\\s*=\\s*).*$', r'\\1Bad User', conf)
conf = re.sub(r'(?im)^(\\s*interfaces\\s*=\\s*).*$', f'\\\\1127.0.0.0/8 {ifaces}', conf)
conf = re.sub(r'(?im)^(\\s*smb encrypt\\s*=\\s*).*$', r'\\1if_required', conf)

pattern = r'\\[' + re.escape(name) + r'\\][^\\[]*'
conf = re.sub(pattern, '', conf, flags=re.IGNORECASE)
conf = conf.rstrip() + NL + NL
block = f'[{name}]' + NL
block += f'    path = {path}' + NL
writable = params['writable']
read_only = 'no' if writable == 'yes' else 'yes'
block += '    browseable = yes' + NL
block += f'    writable = {writable}' + NL
block += f'    read only = {read_only}' + NL
block += f'    guest ok = {guest}' + NL
block += f'    force user = {user}' + NL
block += f'    force group = {group}' + NL
block += '    create mask = 0664' + NL
block += '    directory mask = 0775' + NL
open('/etc/samba/smb.conf', 'w').write(conf + block)

# Ensure share path exists
import os
os.makedirs(path, mode=0o775, exist_ok=True)
"""
    _host_write_json('/tmp/_samba_params.json', share_conf)
    _host_write_script('/tmp/_samba_edit.py', script)
    r = host_run("python3 /tmp/_samba_edit.py")
    host_run("rm -f /tmp/_samba_edit.py /tmp/_samba_params.json 2>/dev/null")

    if r.returncode != 0:
        return jsonify({"error": f"Failed to add share: {r.stderr.strip()}"}), 500

    host_run("systemctl restart smbd nmbd 2>/dev/null || systemctl restart smb nmb 2>/dev/null || true")
    return jsonify({"success": True, "share_name": share_name, "path": share_path})


@storage_bp.route('/samba/share', methods=['DELETE'])
@admin_required
def samba_share_remove():
    data = request.json or {}
    share_name = data.get("name", "").strip()
    if not share_name:
        return jsonify({"error": "name is required"}), 400

    # Validate share name
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9 _\-]{0,63}$', share_name):
        return jsonify({"error": "Invalid share name"}), 400

    script = """import json, re
NL = chr(10)
params = json.loads(open('/tmp/_samba_del_params.json').read())
name = params['name']
try:
    conf = open('/etc/samba/smb.conf').read()
except FileNotFoundError:
    exit(0)
pattern = r'\\[' + re.escape(name) + r'\\][^\\[]*'
conf = re.sub(pattern, '', conf, flags=re.IGNORECASE)
open('/etc/samba/smb.conf', 'w').write(conf.strip() + NL)
"""
    _host_write_json('/tmp/_samba_del_params.json', {"name": share_name})
    _host_write_script('/tmp/_samba_del.py', script)
    host_run("python3 /tmp/_samba_del.py")
    host_run("rm -f /tmp/_samba_del.py /tmp/_samba_del_params.json 2>/dev/null")
    host_run("systemctl restart smbd nmbd 2>/dev/null || systemctl restart smb nmb 2>/dev/null || true")
    return jsonify({"success": True})


@storage_bp.route('/samba/password', methods=['POST'])
@admin_required
def samba_password():
    """Set Samba password for a user (creates the user if needed)."""
    err = require_tools('smbpasswd')
    if err:
        return err
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    # Validate username: alphanumeric + underscores
    if not re.match(r'^[a-zA-Z0-9_]{1,32}$', username):
        return jsonify({"error": "Username may only contain letters, numbers, and _"}), 400

    # Validate password length
    if len(password) < 1 or len(password) > 128:
        return jsonify({"error": "Password must be 1-128 characters"}), 400

    # Ensure user exists on the host
    host_run(f"id {Q(username)} || useradd -M -s /usr/sbin/nologin {Q(username)}")

    # Set samba password safely — pass password via base64-encoded JSON file
    # This avoids any shell injection via password characters
    _host_write_json('/tmp/_smb_pw.json', {'pw': password, 'user': username})
    script = """import json, subprocess, sys
params = json.loads(open('/tmp/_smb_pw.json').read())
pw = params['pw']
user = params['user']
p = subprocess.Popen(['smbpasswd', '-s', '-a', user],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
out, err = p.communicate((pw + chr(10) + pw + chr(10)).encode())
sys.exit(p.returncode)
"""
    _host_write_script('/tmp/_smb_pw_set.py', script)
    r = host_run("python3 /tmp/_smb_pw_set.py")
    host_run("rm -f /tmp/_smb_pw.json /tmp/_smb_pw_set.py 2>/dev/null")

    if r.returncode != 0:
        return jsonify({"error": f"smbpasswd failed: {r.stderr.strip()}"}), 500

    return jsonify({"success": True})


# -- Samba package routes (for App Store install/uninstall) --

@storage_bp.route('/samba/pkg-install', methods=['POST'])
@admin_required
def samba_pkg_install():
    """Install Samba via apt — delegates to the existing /samba/install SSE endpoint logic."""
    r = host_run("command -v smbd")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('smbd', 'sharing-samba')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'Samba installation failed'}), 500
    host_run("systemctl unmask smbd nmbd 2>/dev/null; systemctl enable smbd nmbd 2>/dev/null; systemctl start smbd nmbd 2>/dev/null || true")
    host_run("ufw allow from 192.168.0.0/16 to any port 139 proto tcp comment 'Samba NetBIOS' 2>/dev/null || true")
    host_run("ufw allow from 192.168.0.0/16 to any port 137 proto udp comment 'Samba NetBIOS NS' 2>/dev/null || true")
    host_run("ufw allow from 192.168.0.0/16 to any port 138 proto udp comment 'Samba NetBIOS DGM' 2>/dev/null || true")
    host_run("ufw allow from 192.168.0.0/16 to any port 445 proto tcp comment 'Samba SMB' 2>/dev/null || true")
    return jsonify({'status': 'ok'})


@storage_bp.route('/samba/pkg-uninstall', methods=['POST'])
@admin_required
def samba_pkg_uninstall():
    """Stop Samba services and disable them. Optionally wipe shares config."""
    wipe = (request.json or {}).get('wipe_data', False)

    # 1. Stop wsdd (WS-Discovery) — no purpose without Samba
    host_run("systemctl stop wsdd 2>/dev/null; systemctl disable wsdd 2>/dev/null || true")
    ufw_delete(3702, 'udp')
    ufw_delete(5357, 'tcp')

    # 2. Stop Samba services
    host_run("systemctl stop smbd nmbd 2>/dev/null || true")
    # 3. Disable so they don't restart on reboot
    host_run("systemctl disable smbd nmbd 2>/dev/null || true")

    ok, dep_msg = release_dep('smbd', 'sharing-samba')
    ufw_delete(139, 'tcp')
    ufw_delete(445, 'tcp')

    if wipe:
        # 3. Remove Samba configuration
        host_run("rm -f /etc/samba/smb.conf 2>/dev/null || true")

    # 4. Log event
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'Samba uninstalled (wipe={wipe})')
    except Exception:
        pass

    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove Samba dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/samba/pkg-status', methods=['GET'])
def samba_pkg_status():
    r = host_run("command -v smbd")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'samba_installed': installed})


# ═══════════════════════════════════════════════════════════
#  WS-Discovery (wsdd) — Windows 10+ Network browsing
# ═══════════════════════════════════════════════════════════

def _wsdd_workgroup():
    """Read workgroup from smb.conf, default to WORKGROUP."""
    try:
        with open('/etc/samba/smb.conf') as f:
            for line in f:
                m = re.match(r'\s*workgroup\s*=\s*(\S+)', line, re.IGNORECASE)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return 'WORKGROUP'


@storage_bp.route('/samba/wsdd/status', methods=['GET'])
@admin_required
def wsdd_status():
    """Check if wsdd is installed and running."""
    r = host_run("command -v wsdd")
    installed = r.returncode == 0
    running = False
    enabled = False
    if installed:
        r2 = host_run("systemctl is-active wsdd 2>/dev/null")
        running = r2.stdout.strip() == 'active'
        r3 = host_run("systemctl is-enabled wsdd 2>/dev/null")
        enabled = r3.stdout.strip() == 'enabled'
    return jsonify({'installed': installed, 'running': running, 'enabled': enabled})


@storage_bp.route('/samba/wsdd/toggle', methods=['POST'])
@admin_required
def wsdd_toggle():
    """Enable or disable WS-Discovery for Windows network browsing."""
    enable = (request.json or {}).get('enable', True)

    if enable:
        # Install wsdd if not present
        r = host_run("command -v wsdd")
        if r.returncode != 0:
            r = _apt_install('wsdd', timeout=120)
            if r.returncode != 0:
                return jsonify({'ok': False, 'error': 'Failed to install wsdd: ' + r.stderr.strip()}), 500

        # Configure workgroup via /etc/default/wsdd
        workgroup = _wsdd_workgroup()
        hostname = host_run("hostname -s").stdout.strip() or 'ethos'
        # Detect LAN interface (first non-loopback, non-docker, non-bridge)
        iface_r = host_run(
            "ip -4 route get 8.8.8.8 2>/dev/null | grep -oP 'dev \\K\\S+' | head -1"
        )
        iface = iface_r.stdout.strip() or 'eth0'
        # Plain values (no shell-quoting) — env file is sourced by systemd, not bash
        wsdd_conf = f'WSDD_PARAMS="-n {hostname} -w {workgroup} -i {iface}"\n'
        try:
            os.makedirs('/etc/default', exist_ok=True)
            with open('/etc/default/wsdd', 'w') as f:
                f.write(wsdd_conf)
        except Exception as e:
            logging.warning('wsdd: failed to write /etc/default/wsdd: %s', e)

        # Ensure the systemd unit file exists (Debian wsdd package ships only the binary)
        unit_path = '/etc/systemd/system/wsdd.service'
        if not os.path.exists(unit_path):
            unit = (
                '[Unit]\n'
                'Description=Web Services Dynamic Discovery host daemon\n'
                'Documentation=man:wsdd(1)\n'
                'After=network-online.target smbd.service\n'
                'Wants=network-online.target\n\n'
                '[Service]\n'
                'Type=simple\n'
                'EnvironmentFile=-/etc/default/wsdd\n'
                'ExecStart=/usr/bin/wsdd $WSDD_PARAMS\n'
                'Restart=on-failure\n'
                'RestartSec=5\n\n'
                '[Install]\n'
                'WantedBy=multi-user.target\n'
            )
            try:
                with open(unit_path, 'w') as f:
                    f.write(unit)
                host_run("systemctl daemon-reload")
            except Exception as e:
                logging.warning('wsdd: failed to write unit file: %s', e)

        # Open firewall ports (WS-Discovery multicast + HTTP)
        host_run("ufw allow from 192.168.0.0/16 to any port 3702 proto udp comment 'WS-Discovery' 2>/dev/null || true")
        host_run("ufw allow from 192.168.0.0/16 to any port 5357 proto tcp comment 'WS-Discovery HTTP' 2>/dev/null || true")

        host_run("systemctl unmask wsdd 2>/dev/null; "
                 "systemctl enable wsdd 2>/dev/null; "
                 "systemctl restart wsdd 2>/dev/null || true")
        return jsonify({'ok': True, 'enabled': True})
    else:
        host_run("systemctl stop wsdd 2>/dev/null; "
                 "systemctl disable wsdd 2>/dev/null || true")
        ufw_delete(3702, 'udp')
        ufw_delete(5357, 'tcp')
        return jsonify({'ok': True, 'enabled': False})


# ═══════════════════════════════════════════════════════════
#  Share ACLs (per-user/group access control on Samba shares)
# ═══════════════════════════════════════════════════════════

_SHARE_ACLS_FILE = data_path('share_acls.json')


def _load_share_acls():
    """Load share ACL map: { share_name: { users: {user: perm}, groups: {group: perm} } }."""
    if os.path.isfile(_SHARE_ACLS_FILE):
        try:
            with open(_SHARE_ACLS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_share_acls(acls):
    os.makedirs(os.path.dirname(_SHARE_ACLS_FILE), exist_ok=True)
    with open(_SHARE_ACLS_FILE, 'w') as f:
        json.dump(acls, f, indent=2)


def _apply_share_acl(share_name):
    """Rewrite the Samba share config and apply POSIX ACLs for a single share."""
    acls = _load_share_acls()
    share_acl = acls.get(share_name, {})
    users_acl = share_acl.get('users', {})
    groups_acl = share_acl.get('groups', {})

    # Build Samba directive lists
    valid_users = []
    write_list = []
    read_list = []
    for user, perm in users_acl.items():
        if perm in ('rw', 'ro'):
            valid_users.append(user)
        if perm == 'rw':
            write_list.append(user)
        elif perm == 'ro':
            read_list.append(user)
    for group, perm in groups_acl.items():
        g = f'@{group}'
        if perm in ('rw', 'ro'):
            valid_users.append(g)
        if perm == 'rw':
            write_list.append(g)
        elif perm == 'ro':
            read_list.append(g)

    # If no ACLs defined, skip (share remains open to all authenticated users)
    if not valid_users:
        return

    acl_params = {
        'name': share_name,
        'valid_users': ' '.join(valid_users),
        'write_list': ' '.join(write_list),
        'read_list': ' '.join(read_list),
    }

    # Inject ACL directives into smb.conf for this share
    script = """import json, re
NL = chr(10)
params = json.loads(open('/tmp/_samba_acl_params.json').read())
name = params['name']
try:
    conf = open('/etc/samba/smb.conf').read()
except FileNotFoundError:
    exit(0)

# Find the share block
pattern = r'\\[' + re.escape(name) + r'\\]([^\\[]*)'
m = re.search(pattern, conf, flags=re.IGNORECASE)
if not m:
    exit(0)

block = m.group(0)
# Remove old ACL directives
for directive in ('valid users', 'write list', 'read list', 'force user', 'force group'):
    block = re.sub(r'\\n\\s*' + directive + r'\\s*=.*', '', block, flags=re.IGNORECASE)

# Add new ACL directives
if params['valid_users']:
    block = block.rstrip() + NL + '    valid users = ' + params['valid_users'] + NL
if params['write_list']:
    block = block.rstrip() + NL + '    write list = ' + params['write_list'] + NL
if params['read_list']:
    block = block.rstrip() + NL + '    read list = ' + params['read_list'] + NL

conf = re.sub(pattern, block, conf, count=1, flags=re.IGNORECASE)
open('/etc/samba/smb.conf', 'w').write(conf)
"""
    _host_write_json('/tmp/_samba_acl_params.json', acl_params)
    _host_write_script('/tmp/_samba_acl.py', script)
    host_run("python3 /tmp/_samba_acl.py")
    host_run("rm -f /tmp/_samba_acl.py /tmp/_samba_acl_params.json 2>/dev/null")

    # Apply POSIX ACLs on the directory
    share_path = _get_share_path(share_name)
    if share_path:
        _apply_posix_acls(share_path, users_acl, groups_acl)

    host_run("systemctl restart smbd 2>/dev/null || true")


def _get_share_path(share_name):
    """Read the path for a share from smb.conf."""
    r = host_run(f"grep -A5 '\\[{_q_imported(share_name)}\\]' /etc/samba/smb.conf 2>/dev/null | grep 'path =' | head -1")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split('=', 1)[-1].strip()
    return None


def _apply_posix_acls(path, users_acl, groups_acl):
    """Apply POSIX ACLs on the share directory using setfacl."""
    # Check if setfacl is available
    r = host_run("command -v setfacl")
    if r.returncode != 0:
        return

    # Reset ACLs
    host_run(f"setfacl -b {_q_imported(path)} 2>/dev/null")

    # Apply user ACLs
    for user, perm in users_acl.items():
        if perm == 'rw':
            host_run(f"setfacl -m u:{_q_imported(user)}:rwx {_q_imported(path)}")
        elif perm == 'ro':
            host_run(f"setfacl -m u:{_q_imported(user)}:r-x {_q_imported(path)}")
        elif perm == 'none':
            host_run(f"setfacl -m u:{_q_imported(user)}:--- {_q_imported(path)}")

    # Apply group ACLs
    for group, perm in groups_acl.items():
        if perm == 'rw':
            host_run(f"setfacl -m g:{_q_imported(group)}:rwx {_q_imported(path)}")
        elif perm == 'ro':
            host_run(f"setfacl -m g:{_q_imported(group)}:r-x {_q_imported(path)}")
        elif perm == 'none':
            host_run(f"setfacl -m g:{_q_imported(group)}:--- {_q_imported(path)}")

    # Set default ACLs (for new files/dirs)
    for user, perm in users_acl.items():
        if perm == 'rw':
            host_run(f"setfacl -dm u:{_q_imported(user)}:rwx {_q_imported(path)}")
        elif perm == 'ro':
            host_run(f"setfacl -dm u:{_q_imported(user)}:r-x {_q_imported(path)}")

    for group, perm in groups_acl.items():
        if perm == 'rw':
            host_run(f"setfacl -dm g:{_q_imported(group)}:rwx {_q_imported(path)}")
        elif perm == 'ro':
            host_run(f"setfacl -dm g:{_q_imported(group)}:r-x {_q_imported(path)}")


@storage_bp.route('/samba/share/acl', methods=['GET'])
@admin_required
def get_share_acl():
    """Get ACLs for a specific share."""
    share_name = request.args.get('name', '').strip()
    if not share_name:
        return jsonify({'error': 'name parameter required'}), 400
    acls = _load_share_acls()
    share_acl = acls.get(share_name, {'users': {}, 'groups': {}})
    return jsonify({'ok': True, 'share': share_name, 'acl': share_acl})


@storage_bp.route('/samba/share/acl', methods=['PUT'])
@admin_required
def set_share_acl():
    """Set ACLs for a specific share.

    Body: { name: "share_name", users: { "user1": "rw", "user2": "ro" },
            groups: { "group1": "rw" } }
    Permission values: "rw" (read-write), "ro" (read-only), "none" (no access)
    """
    data = request.json or {}
    share_name = data.get('name', '').strip()
    if not share_name:
        return jsonify({'error': 'name required'}), 400

    users_acl = data.get('users', {})
    groups_acl = data.get('groups', {})

    # Validate permission values
    valid_perms = {'rw', 'ro', 'none'}
    for perm in list(users_acl.values()) + list(groups_acl.values()):
        if perm not in valid_perms:
            return jsonify({'error': f'Invalid permission "{perm}". Use: rw, ro, none'}), 400

    # Remove 'none' entries (deny handled by exclusion from valid users)
    clean_users = {u: p for u, p in users_acl.items() if p != 'none'}
    clean_groups = {g: p for g, p in groups_acl.items() if p != 'none'}

    acls = _load_share_acls()
    acls[share_name] = {'users': users_acl, 'groups': groups_acl}
    _save_share_acls(acls)

    # Apply to Samba + filesystem
    _apply_share_acl(share_name)

    return jsonify({'ok': True, 'acl': acls[share_name]})


@storage_bp.route('/samba/share/acl', methods=['DELETE'])
@admin_required
def delete_share_acl():
    """Remove all ACLs for a share (revert to open access)."""
    data = request.json or {}
    share_name = data.get('name', '').strip()
    if not share_name:
        return jsonify({'error': 'name required'}), 400

    acls = _load_share_acls()
    if share_name in acls:
        del acls[share_name]
        _save_share_acls(acls)

    # Remove POSIX ACLs
    share_path = _get_share_path(share_name)
    if share_path:
        host_run(f"setfacl -b {_q_imported(share_path)} 2>/dev/null")

    # Remove ACL directives from smb.conf (restore default force user/group)
    host_run("systemctl restart smbd 2>/dev/null || true")
    return jsonify({'ok': True})


@storage_bp.route('/samba/shares/acls', methods=['GET'])
@admin_required
def get_all_share_acls():
    """Get ACLs for all shares."""
    return jsonify({'ok': True, 'acls': _load_share_acls()})


# ═══════════════════════════════════════════════════════════
#  NFS Sharing
# ═══════════════════════════════════════════════════════════

@storage_bp.route('/nfs/status')
def nfs_status():
    r = host_run("command -v exportfs")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active nfs-server 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running})


@storage_bp.route('/nfs/install', methods=['POST'])
def nfs_install():
    from host import ensure_dep
    ok, msg = ensure_dep('exportfs', install=True)
    if not ok:
        # Try direct package name
        r = _apt_install('nfs-kernel-server', timeout=120)
        if r.returncode != 0:
            return jsonify({"error": f"Installation failed: {r.stderr[-200:]}"}), 500
    host_run("systemctl enable nfs-server && systemctl start nfs-server", timeout=15)
    # Set optimal NFS thread count
    host_run("sed -i 's/^RPCNFSDCOUNT=.*/RPCNFSDCOUNT=16/' /etc/default/nfs-kernel-server 2>/dev/null || echo 'RPCNFSDCOUNT=16' >> /etc/default/nfs-kernel-server")
    host_run("systemctl restart nfs-server", timeout=15)
    return jsonify({"status": "ok"})


@storage_bp.route('/nfs/exports')
def nfs_exports():
    """List current NFS exports."""
    r = host_run("cat /etc/exports 2>/dev/null || echo ''")
    exports = []
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            exports.append({
                "path": parts[0],
                "clients": ' '.join(parts[1:]),
                "raw": line,
            })
    return jsonify({"exports": exports})


@storage_bp.route('/nfs/export', methods=['POST'])
def nfs_export_add():
    """Add an NFS export. Body: {path, network, options}"""
    data = request.json or {}
    path = data.get("path", "").strip()
    network = data.get("network", "*").strip() or "*"
    options = data.get("options", "rw,async,no_subtree_check,no_root_squash,insecure").strip()
    if not path or not os.path.isdir(path):
        return jsonify({"error": "Path not found"}), 400

    export_line = f'{path} {network}({options})'
    # Check for duplicates
    r = host_run("cat /etc/exports 2>/dev/null || echo ''")
    for line in r.stdout.splitlines():
        if line.strip().startswith(path + ' '):
            return jsonify({"error": "This directory is already exported"}), 409

    host_run(f"echo {Q(export_line)} >> /etc/exports")
    host_run("exportfs -ra", timeout=10)
    return jsonify({"ok": True})


@storage_bp.route('/nfs/export', methods=['DELETE'])
def nfs_export_remove():
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "Path required"}), 400
    # Remove line from /etc/exports
    host_run(f"sed -i '\\|^{path} |d' /etc/exports")
    host_run("exportfs -ra", timeout=10)
    return jsonify({"ok": True})


# -- NFS package routes (for EthOS Package Store) --

@storage_bp.route('/nfs/pkg-install', methods=['POST'])
def nfs_pkg_install():
    r = host_run("command -v exportfs")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('exportfs', 'sharing-nfs')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'NFS installation failed'}), 500
    host_run("systemctl enable nfs-server && systemctl start nfs-server", timeout=15)
    ufw_allow(2049, 'tcp', 'NFS')
    return jsonify({'status': 'ok'})


@storage_bp.route('/nfs/pkg-uninstall', methods=['POST'])
def nfs_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    host_run("systemctl stop nfs-server 2>/dev/null || true")
    host_run("systemctl disable nfs-server 2>/dev/null || true")
    ok, dep_msg = release_dep('exportfs', 'sharing-nfs')
    ufw_delete(2049, 'tcp')
    if wipe:
        host_run("rm -f /etc/exports 2>/dev/null || true")
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'NFS uninstalled (wipe={wipe})')
    except Exception:
        pass
    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove NFS dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/nfs/pkg-status', methods=['GET'])
def nfs_pkg_status():
    r = host_run("command -v exportfs")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'nfs_installed': installed})


# ═══════════════════════════════════════════════════════════
#  DLNA / MiniDLNA
# ═══════════════════════════════════════════════════════════

@storage_bp.route('/dlna/status')
def dlna_status():
    r = host_run("command -v minidlnad")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active minidlna 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running})


@storage_bp.route('/dlna/install', methods=['POST'])
def dlna_install():
    r = _apt_install('minidlna', timeout=120)
    if r.returncode != 0:
        return jsonify({"error": f"Installation failed: {r.stderr[-200:]}"}), 500
    host_run("systemctl enable minidlna", timeout=10)
    return jsonify({"status": "ok"})


@storage_bp.route('/dlna/config')
def dlna_config():
    """Get DLNA media directories."""
    r = host_run("cat /etc/minidlna.conf 2>/dev/null || echo ''")
    dirs = []
    friendly_name = "EthOS"
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith('media_dir='):
            dirs.append(line.split('=', 1)[1])
        elif line.startswith('friendly_name='):
            friendly_name = line.split('=', 1)[1]
    return jsonify({"dirs": dirs, "friendly_name": friendly_name})


@storage_bp.route('/dlna/config', methods=['POST'])
def dlna_config_set():
    """Set DLNA media directories. Body: {dirs: ["/home/media", ...], friendly_name: "..."}"""
    data = request.json or {}
    dirs = data.get("dirs", [])
    friendly_name = data.get("friendly_name", "EthOS").strip()

    config = f"""# EthOS MiniDLNA config
friendly_name={friendly_name}
db_dir=/var/cache/minidlna
log_dir=/var/log
inotify=yes
"""
    for d in dirs:
        d = d.strip()
        if d and os.path.isdir(d):
            config += f"media_dir={d}\n"

    _host_write_file('/etc/minidlna.conf', config)
    host_run("systemctl restart minidlna 2>/dev/null || systemctl start minidlna", timeout=10)
    return jsonify({"ok": True})


@storage_bp.route('/dlna/rescan', methods=['POST'])
def dlna_rescan():
    host_run("systemctl restart minidlna", timeout=10)
    return jsonify({"status": "ok"})


# -- DLNA package routes (for EthOS Package Store) --

@storage_bp.route('/dlna/pkg-install', methods=['POST'])
def dlna_pkg_install():
    r = host_run("command -v minidlnad")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('minidlnad', 'sharing-dlna')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'MiniDLNA installation failed'}), 500
    host_run("systemctl enable minidlna", timeout=10)
    ufw_allow(8200, 'tcp', 'DLNA')
    return jsonify({'status': 'ok'})


@storage_bp.route('/dlna/pkg-uninstall', methods=['POST'])
def dlna_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    host_run("systemctl stop minidlna 2>/dev/null || true")
    host_run("systemctl disable minidlna 2>/dev/null || true")
    ok, dep_msg = release_dep('minidlnad', 'sharing-dlna')
    ufw_delete(8200, 'tcp')
    if wipe:
        host_run("rm -f /etc/minidlna.conf 2>/dev/null || true")
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'MiniDLNA uninstalled (wipe={wipe})')
    except Exception:
        pass
    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove MiniDLNA dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/dlna/pkg-status', methods=['GET'])
def dlna_pkg_status():
    r = host_run("command -v minidlnad")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'dlna_installed': installed})


# ═══════════════════════════════════════════════════════════
#  SFTP
# ═══════════════════════════════════════════════════════════

@storage_bp.route('/sftp/status')
def sftp_status():
    r = host_run("systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null")
    running = "active" in r.stdout.strip()
    # Check if SFTP subsystem is enabled
    r2 = host_run("grep -q 'Subsystem.*sftp' /etc/ssh/sshd_config 2>/dev/null")
    sftp_enabled = r2.returncode == 0
    return jsonify({"installed": True, "running": running, "sftp_enabled": sftp_enabled})


@storage_bp.route('/sftp/toggle', methods=['POST'])
def sftp_toggle():
    """Enable or disable SFTP subsystem."""
    data = request.json or {}
    enable = data.get("enable", True)

    if enable:
        # Ensure Subsystem sftp line exists and is not commented
        host_run("grep -q '^Subsystem.*sftp' /etc/ssh/sshd_config || "
                 "echo 'Subsystem sftp /usr/lib/openssh/sftp-server' >> /etc/ssh/sshd_config")
        host_run("sed -i 's/^#Subsystem.*sftp/Subsystem sftp \\//g' /etc/ssh/sshd_config")
    else:
        host_run("sed -i 's/^Subsystem.*sftp/#Subsystem sftp/g' /etc/ssh/sshd_config")

    host_run("systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null", timeout=10)
    return jsonify({"ok": True, "enabled": enable})


@storage_bp.route('/sftp/users')
def sftp_users():
    """List system users that can use SFTP."""
    r = host_run("awk -F: '$3>=1000 && $3<65534{print $1\":\"$6}' /etc/passwd")
    users = []
    for line in r.stdout.strip().splitlines():
        parts = line.split(':', 1)
        if len(parts) == 2:
            users.append({"username": parts[0], "home": parts[1]})
    return jsonify({"users": users})


# -- SFTP package routes (for EthOS Package Store) --

@storage_bp.route('/sftp/pkg-install', methods=['POST'])
def sftp_pkg_install():
    """SFTP uses OpenSSH which is usually pre-installed. Enable the subsystem."""
    r = host_run("command -v sshd")
    if r.returncode != 0:
        _apt_install('openssh-server', timeout=120)
    # Enable SFTP subsystem
    host_run("grep -q '^Subsystem.*sftp' /etc/ssh/sshd_config || "
             "echo 'Subsystem sftp /usr/lib/openssh/sftp-server' >> /etc/ssh/sshd_config")
    host_run("sed -i 's/^#Subsystem.*sftp/Subsystem sftp \\/usr\\/lib\\/openssh\\/sftp-server/g' /etc/ssh/sshd_config")
    host_run("systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null", timeout=10)
    return jsonify({'status': 'ok'})


@storage_bp.route('/sftp/pkg-uninstall', methods=['POST'])
def sftp_pkg_uninstall():
    """Disable SFTP subsystem (don't remove sshd — that would kill SSH access)."""
    host_run("sed -i 's/^Subsystem.*sftp/#Subsystem sftp/g' /etc/ssh/sshd_config")
    host_run("systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null", timeout=10)
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', 'SFTP disabled (subsystem disabled)')
    except Exception:
        pass
    return jsonify({'ok': True})


@storage_bp.route('/sftp/pkg-status', methods=['GET'])
def sftp_pkg_status():
    r = host_run("grep -q '^Subsystem.*sftp' /etc/ssh/sshd_config 2>/dev/null")
    enabled = r.returncode == 0
    return jsonify({'status': 'ready' if enabled else 'missing', 'sftp_enabled': enabled})


# ═══════════════════════════════════════════════════════════
#  WebDAV (via lighttpd or built-in)
# ═══════════════════════════════════════════════════════════

_WEBDAV_CONF = '/etc/lighttpd/conf-enabled/90-webdav.conf'
_WEBDAV_PORT = 8888
_WEBDAV_SHARES_FILE = data_path('webdav_shares.json')


def _load_webdav_shares():
    try:
        with open(_WEBDAV_SHARES_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_webdav_shares(shares):
    with open(_WEBDAV_SHARES_FILE, 'w') as f:
        json.dump(shares, f, indent=2)


def _rebuild_webdav_conf(shares):
    """Regenerate lighttpd config from JSON share list."""
    if not shares:
        host_run(f"rm -f {_WEBDAV_CONF}")
        host_run("systemctl restart lighttpd 2>/dev/null || true", timeout=10)
        return

    blocks = []
    for s in shares:
        auth = ""
        if s.get('username'):
            htpasswd = f"/etc/lighttpd/webdav_{s['url_path'].strip('/').replace('/', '_')}.htpasswd"
            auth = f"""
    auth.backend = "htdigest"
    auth.backend.htdigest.userfile = "{htpasswd}"
    auth.require = ( "{s['url_path']}" => ( "method" => "digest", "realm" => "WebDAV", "require" => "valid-user" ) )
"""
        blocks.append(f"""    alias.url += ( "{s['url_path']}" => "{s['fs_path']}" )
    webdav.activate = "enable"
    webdav.is-readonly = "disable"
    webdav.sqlite-db-name = "/var/cache/lighttpd/webdav.db"
{auth}""")

    conf = f"""# EthOS WebDAV — auto-generated, do not edit manually
server.modules += ( "mod_webdav", "mod_alias" )
$SERVER["socket"] == ":{_WEBDAV_PORT}" {{
{chr(10).join(blocks)}
}}
"""
    _host_write_file(_WEBDAV_CONF, conf)
    host_run("mkdir -p /var/cache/lighttpd && systemctl restart lighttpd", timeout=10)


@storage_bp.route('/webdav/status')
def webdav_status():
    r = host_run("command -v lighttpd")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active lighttpd 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running, "port": _WEBDAV_PORT})


@storage_bp.route('/webdav/install', methods=['POST'])
def webdav_install():
    r = _apt_install('lighttpd', timeout=120)
    if r.returncode != 0:
        return jsonify({"error": f"Installation failed: {r.stderr[-200:]}"}), 500
    # Enable WebDAV module
    host_run("lighttpd-enable-mod webdav 2>/dev/null || true")
    return jsonify({"status": "ok"})


@storage_bp.route('/webdav/shares')
def webdav_shares():
    """Get current WebDAV shares from JSON store."""
    shares = _load_webdav_shares()
    return jsonify({"shares": shares, "port": _WEBDAV_PORT})


@storage_bp.route('/webdav/share', methods=['POST'])
def webdav_add():
    """Add a WebDAV share. Body: {path, url_path, username, password}"""
    data = request.json or {}
    fs_path = data.get("path", "").strip()
    url_path = data.get("url_path", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not fs_path or not os.path.isdir(fs_path):
        return jsonify({"error": "Path not found"}), 400
    if not url_path:
        url_path = '/' + os.path.basename(fs_path)

    # Validate username/password — reject shell-dangerous characters
    import re as _re_val
    if username and not _re_val.match(r'^[a-zA-Z0-9._@-]+$', username):
        return jsonify({"error": "Invalid characters in username"}), 400
    if password and any(c in password for c in "'\"\\`$"):
        return jsonify({"error": "Invalid characters in password"}), 400

    # Set up htpasswd for this share if auth requested
    if username and password:
        htpasswd = f"/etc/lighttpd/webdav_{url_path.strip('/').replace('/', '_')}.htpasswd"
        host_run(f"printf '%s\\n' {Q(password)} | htpasswd -i -c {htpasswd} {Q(username)} 2>/dev/null || "
                 f"printf '%s' {Q(username + ':WebDAV:' + password)} | md5sum | cut -d' ' -f1 | "
                 f"xargs -I{{}} printf '%s\\n' {Q(username)}':WebDAV:'{{}}'\\n' > {htpasswd}")

    # Save to JSON and rebuild config
    shares = _load_webdav_shares()
    # Remove existing share with same url_path (update)
    shares = [s for s in shares if s.get('url_path') != url_path]
    shares.append({
        "url_path": url_path,
        "fs_path": fs_path,
        "username": username or None,
    })
    _save_webdav_shares(shares)
    _rebuild_webdav_conf(shares)

    return jsonify({"ok": True, "port": _WEBDAV_PORT})


@storage_bp.route('/webdav/share', methods=['DELETE'])
def webdav_remove():
    """Remove a single WebDAV share by url_path, or all if no url_path given."""
    data = request.json or {}
    url_path = data.get("url_path", "").strip()

    shares = _load_webdav_shares()
    if url_path:
        shares = [s for s in shares if s.get('url_path') != url_path]
    else:
        shares = []

    _save_webdav_shares(shares)
    _rebuild_webdav_conf(shares)
    return jsonify({"ok": True})


# -- WebDAV package routes (for EthOS Package Store) --

@storage_bp.route('/webdav/pkg-install', methods=['POST'])
@admin_required
def webdav_pkg_install():
    r = host_run("command -v lighttpd")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('lighttpd', 'sharing-webdav')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'lighttpd installation failed'}), 500
    host_run("lighttpd-enable-mod webdav 2>/dev/null || true")
    ufw_allow(8888, 'tcp', 'WebDAV')
    return jsonify({'status': 'ok'})


@storage_bp.route('/webdav/pkg-uninstall', methods=['POST'])
@admin_required
def webdav_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    host_run("systemctl stop lighttpd 2>/dev/null || true")
    host_run("systemctl disable lighttpd 2>/dev/null || true")
    ok, dep_msg = release_dep('lighttpd', 'sharing-webdav')
    ufw_delete(8888, 'tcp')
    if wipe:
        host_run(f"rm -f {_WEBDAV_CONF} 2>/dev/null || true")
        host_run("rm -f /etc/lighttpd/webdav_*.htpasswd 2>/dev/null || true")
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'WebDAV uninstalled (wipe={wipe})')
    except Exception:
        pass
    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove WebDAV dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/webdav/pkg-status', methods=['GET'])
def webdav_pkg_status():
    r = host_run("command -v lighttpd")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'webdav_installed': installed})


# ═══════════════════════════════════════════════════════════
#  FTP (vsftpd)
# ═══════════════════════════════════════════════════════════

@storage_bp.route('/ftp/status')
def ftp_status():
    r = host_run("command -v vsftpd")
    installed = r.returncode == 0
    running = False
    if installed:
        r2 = host_run("systemctl is-active vsftpd 2>/dev/null")
        running = "active" in r2.stdout.strip()
    return jsonify({"installed": installed, "running": running})


@storage_bp.route('/ftp/install', methods=['POST'])
def ftp_install():
    r = _apt_install('vsftpd', timeout=120)
    if r.returncode != 0:
        return jsonify({"error": f"Installation failed: {r.stderr[-200:]}"}), 500
    # Sensible defaults
    config = """# EthOS vsftpd config
listen=YES
listen_ipv6=NO
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=022
chroot_local_user=YES
allow_writeable_chroot=YES
pasv_enable=YES
pasv_min_port=40000
pasv_max_port=40100
"""
    _host_write_file('/etc/vsftpd.conf', config)
    host_run("systemctl enable vsftpd && systemctl restart vsftpd", timeout=10)
    return jsonify({"status": "ok"})


@storage_bp.route('/ftp/toggle', methods=['POST'])
def ftp_toggle():
    """Start or stop vsftpd. Body: {enable: bool}"""
    data = request.json or {}
    enable = data.get("enable", True)
    if enable:
        host_run("systemctl start vsftpd", timeout=10)
    else:
        host_run("systemctl stop vsftpd", timeout=10)
    return jsonify({"ok": True, "enabled": enable})


# -- FTP package routes (for EthOS Package Store) --

@storage_bp.route('/ftp/pkg-install', methods=['POST'])
def ftp_pkg_install():
    r = host_run("command -v vsftpd")
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = claim_dep('vsftpd', 'sharing-ftp')
    if not ok:
        return jsonify({'ok': False, 'error': msg or 'vsftpd installation failed'}), 500
    host_run("systemctl enable vsftpd && systemctl restart vsftpd", timeout=10)
    ufw_allow(21, 'tcp', 'FTP')
    return jsonify({'status': 'ok'})


@storage_bp.route('/ftp/pkg-uninstall', methods=['POST'])
def ftp_pkg_uninstall():
    wipe = (request.json or {}).get('wipe_data', False)
    host_run("systemctl stop vsftpd 2>/dev/null || true")
    host_run("systemctl disable vsftpd 2>/dev/null || true")
    ok, dep_msg = release_dep('vsftpd', 'sharing-ftp')
    ufw_delete(21, 'tcp')
    if wipe:
        host_run("rm -f /etc/vsftpd.conf 2>/dev/null || true")
    try:
        from blueprints.eventlog import log as elog
        elog('packages', 'info', f'FTP (vsftpd) uninstalled (wipe={wipe})')
    except Exception:
        pass
    if not ok:
        return jsonify({'ok': False, 'error': dep_msg or 'Failed to remove FTP dependencies'}), 500
    return jsonify({'ok': True})


@storage_bp.route('/ftp/pkg-status', methods=['GET'])
def ftp_pkg_status():
    r = host_run("command -v vsftpd")
    installed = r.returncode == 0
    return jsonify({'status': 'ready' if installed else 'missing', 'ftp_installed': installed})


