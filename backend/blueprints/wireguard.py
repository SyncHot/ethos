
import os
import subprocess
import json
import re
import tempfile
import time
from flask import Blueprint, jsonify, request, send_file, Response
from blueprints.admin_required import admin_required
from utils import require_tools, check_tool

# Host helpers
from host import host_run as _host_run, data_path as _data_path

wireguard_bp = Blueprint('wireguard', __name__, url_prefix='/api/wireguard')

WG_DIR = '/etc/wireguard'
WG_CONF = os.path.join(WG_DIR, 'wg0.conf')
DDNS_CONFIG = _data_path('ddns_config.json')
WG_PORT = 51820
WG_NET_PREFIX = "10.100.0"

def _run_cmd(cmd, timeout=10):
    """Run shell command with sudo."""
    if isinstance(cmd, list):
        cmd_str = ' '.join(cmd) # rudimentary, be careful with spaces
    else:
        cmd_str = cmd
    
    # Use host_run which handles sudo if needed (though host_run might not use sudo by default unless configured)
    # Actually host_run in this codebase usually runs as the user, so we need sudo.
    # But wait, host_run in host.py might be wrapping subprocess.
    
    # Let's use subprocess directly with sudo for local execution since backend runs as user but needs root for wg
    try:
        if isinstance(cmd, list):
             full_cmd = ['sudo', '-n'] + cmd
        else:
             full_cmd = f"sudo -n {cmd}"
             
        # Use shell=True if it's a string, but list is safer
        if isinstance(cmd, list):
            result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        else:
            result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception as e:
        return None, str(e), -1

def _get_ddns_hostname():
    try:
        if os.path.exists(DDNS_CONFIG):
            with open(DDNS_CONFIG, 'r') as f:
                data = json.load(f)
                if not data.get('enabled'):
                    return None
                provider = data.get('provider')
                if provider == 'duckdns':
                    domain = data.get('domain')
                    if domain:
                        return f"{domain}.duckdns.org"
                elif provider in ['dynv6', 'noip']:
                    return data.get('hostname')
                elif provider == 'cloudflare':
                    return data.get('record_name')
                elif provider == 'freedns':
                    # freedns config might not store hostname explicitly if only update_key is used
                    # check if we can parse it
                    pass
                elif provider == 'custom':
                     # parse from update_url if possible, or maybe a separate field?
                     # Custom might not have a reliable hostname field
                     pass
    except Exception:
        pass
    
    # Fallback: try to get public IP or hostname
    try:
        out, _, _ = _run_cmd(['curl', '-s', 'ifconfig.me'])
        if out and re.match(r'^\d+\.\d+\.\d+\.\d+$', out):
            return out
    except Exception:
        pass
        
    return None

def _get_default_interface():
    out, _, _ = _run_cmd("ip route show default | awk '/default/ {print $5}'")
    return out.strip() if out else 'eth0'

def _ensure_wg_installed():
    out, _, code = _run_cmd(['which', 'wg'])
    return code == 0

def _get_wg_keys():
    """Read private/public key from wg0.conf."""
    if not os.path.exists(WG_CONF):
        return None, None

    try:
        with open(WG_CONF, 'r') as f:
            content = f.read()
    except PermissionError:
        out, _, code = _run_cmd(['cat', WG_CONF])
        if code != 0:
            return None, None
        content = out

    priv_key = None
    pub_key = None
    
    if content:
        m = re.search(r'PrivateKey\s*=\s*(.*)', content)
        if m:
            priv_key = m.group(1).strip()
            # Derive public key
            # Use stdin to avoid exposing key in ps and shell expansion issues
            try:
                p = subprocess.run(['wg', 'pubkey'], input=priv_key.encode(), capture_output=True)
                if p.returncode == 0:
                    pub_key = p.stdout.decode().strip()
            except Exception:
                pass
    
    return priv_key, pub_key

def _generate_keys():
    priv = subprocess.run("wg genkey", shell=True, capture_output=True, text=True).stdout.strip()
    # Use pipe or explicit input for pubkey to be safe
    pub = subprocess.run(['wg', 'pubkey'], input=priv.encode(), capture_output=True).stdout.decode().strip()
    preshared = subprocess.run("wg genpsk", shell=True, capture_output=True, text=True).stdout.strip()
    return priv, pub, preshared

def _get_peers():
    """Parse wg0.conf to get peers."""
    out, err, code = _run_cmd(['cat', WG_CONF])
    if code != 0:
        return []

    peers = []
    current_peer = None
    
    for line in out.split('\n'):
        line = line.strip()
        if line == '[Peer]':
            if current_peer:
                peers.append(current_peer)
            current_peer = {'AllowedIPs': '', 'PublicKey': '', 'Comment': ''}
        elif current_peer is not None:
            if line.startswith('PublicKey'):
                current_peer['PublicKey'] = line.split('=', 1)[1].strip()
            elif line.startswith('AllowedIPs'):
                current_peer['AllowedIPs'] = line.split('=', 1)[1].strip()
            elif line.startswith('# Name:'):
                 current_peer['Name'] = line.split(':', 1)[1].strip()
            elif line.startswith('#'):
                 # Capture other comments if needed
                 pass
    
    if current_peer:
        peers.append(current_peer)
        
    # Enrich with status
    status_out, _, _ = _run_cmd(['wg', 'show', 'wg0', 'dump'])
    # dump format: peer_pubkey  preshared_key  endpoint  allowed_ips  latest_handshake  transfer_rx  transfer_tx  persistent_keepalive
    
    peer_status = {}
    if status_out:
        for line in status_out.split('\n'):
            parts = line.split('\t')
            if len(parts) > 1:
                # first line is interface info, verify if it's a peer
                # wg show dump lines:
                # interface public_key private_key listen_port fwmark
                # peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx
                if len(parts) >= 8: # It's a peer
                    pub = parts[0]
                    peer_status[pub] = {
                        'endpoint': parts[2],
                        'latest_handshake': int(parts[4]),
                        'transfer_rx': int(parts[5]),
                        'transfer_tx': int(parts[6])
                    }

    for p in peers:
        pub = p.get('PublicKey')
        if pub in peer_status:
            stat = peer_status[pub]
            p['latest_handshake'] = stat['latest_handshake']
            p['transfer_rx'] = stat['transfer_rx']
            p['transfer_tx'] = stat['transfer_tx']
            p['endpoint'] = stat['endpoint']
            p['status'] = 'active'
        else:
            p['status'] = 'inactive'
            p['latest_handshake'] = 0
            p['transfer_rx'] = 0
            p['transfer_tx'] = 0

    return peers

def _get_next_ip(peers):
    used_ips = set()
    for p in peers:
        ips = p.get('AllowedIPs', '').split(',')
        for ip in ips:
            ip = ip.strip()
            if ip.startswith(WG_NET_PREFIX):
                # Extract last octet
                try:
                    parts = ip.split('/')
                    addr = parts[0]
                    octet = int(addr.split('.')[-1])
                    used_ips.add(octet)
                except (ValueError, IndexError):
                    pass
    
    # Server is usually .1
    used_ips.add(1)
    
    for i in range(2, 255):
        if i not in used_ips:
            return f"{WG_NET_PREFIX}.{i}/32"
    return None

def _write_config(server_priv, port, peers):
    iface = _get_default_interface()
    
    # PostUp: Enable IP forwarding if not enabled, set up NAT
    # PostDown: revert
    
    config = f"""[Interface]
Address = {WG_NET_PREFIX}.1/24
SaveConfig = false
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o {iface} -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o {iface} -j MASQUERADE
ListenPort = {port}
PrivateKey = {server_priv}

"""
    for p in peers:
        config += f"\n# Name: {p.get('Name', 'Unknown')}\n[Peer]\nPublicKey = {p['PublicKey']}\nAllowedIPs = {p['AllowedIPs']}\n"
        if p.get('PresharedKey'):
             config += f"PresharedKey = {p['PresharedKey']}\n"

    # Write to temp file then move
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as tf:
        tf.write(config)
        temp_name = tf.name
        
    _run_cmd(['mv', temp_name, WG_CONF])
    _run_cmd(['chmod', '600', WG_CONF])

@wireguard_bp.route('/status', methods=['GET'])
@admin_required
def status():
    # Check if wg is installed
    if not _ensure_wg_installed():
         return jsonify({'installed': False, 'active': False, 'peers': []})

    # Check if interface is up
    out, _, _ = _run_cmd(['ip', 'link', 'show', 'wg0'])
    is_active = (out is not None and 'UP' in out)
    
    peers = _get_peers()
    
    # Get DDNS hostname
    hostname = _get_ddns_hostname()
    
    return jsonify({
        'installed': True,
        'active': is_active,
        'peers': peers,
        'hostname': hostname,
        'port': WG_PORT
    })

@wireguard_bp.route('/toggle', methods=['POST'])
@admin_required
def toggle():
    err = require_tools('wg', 'ufw')
    if err:
        return err
    data = request.json or {}
    enable = data.get('enable', False)
    
    if enable:
        # Check if config exists, if not create minimal
        if not os.path.exists(WG_CONF):
            priv, pub, _ = _generate_keys()
            _write_config(priv, WG_PORT, [])
            
        # Ensure firewall
        _run_cmd(['ufw', 'allow', f'{WG_PORT}/udp'])
            
        # Start
        out, err, code = _run_cmd(['wg-quick', 'up', 'wg0'])
        if code != 0 and "already exists" not in err:
            return jsonify({'error': err or 'Failed to start WireGuard'}), 500
        
        # Enable auto-start
        _run_cmd(['systemctl', 'enable', 'wg-quick@wg0'])
        
    else:
        # Stop
        out, err, code = _run_cmd(['wg-quick', 'down', 'wg0'])
        
        # Disable auto-start
        _run_cmd(['systemctl', 'disable', 'wg-quick@wg0'])

    return jsonify({'success': True})

@wireguard_bp.route('/peer', methods=['POST'])
@admin_required
def add_peer():
    err = require_tools('wg', 'qrencode')
    if err:
        return err
    data = request.json or {}
    name = data.get('name', 'Device')
    
    # Get current peers
    peers = _get_peers()
    
    # Generate keys for new peer
    priv, pub, psk = _generate_keys()
    
    # Get IP
    ip = _get_next_ip(peers)
    if not ip:
        return jsonify({'error': 'No IP addresses available'}), 500
        
    new_peer = {
        'Name': name,
        'PublicKey': pub,
        'PresharedKey': psk,
        'AllowedIPs': ip,
        'PrivateKey': priv # Only needed for generating config, don't store in wg0.conf usually? 
                           # Actually we don't store Peer's PrivateKey in Server config.
                           # But we need to return it to the user ONCE.
    }
    
    peers.append({
        'Name': name,
        'PublicKey': pub,
        'PresharedKey': psk,
        'AllowedIPs': ip
    })
    
    # Get server private key to write config
    server_priv, server_pub = _get_wg_keys()
    if not server_priv:
         # Should not happen if we are adding peers to active server
         # but if server config missing, generate new
         server_priv, server_pub, _ = _generate_keys()

    _write_config(server_priv, WG_PORT, peers)

    # Sync live interface without restart
    # Fix: avoid process substitution <(...) which fails in list context or sudo
    # Use pipe: wg-quick strip wg0 | wg syncconf wg0 /dev/stdin
    try:
        # Get config
        strip_proc = subprocess.run(['sudo', 'wg-quick', 'strip', 'wg0'], capture_output=True)
        if strip_proc.returncode == 0:
            config_data = strip_proc.stdout
            subprocess.run(['sudo', 'wg', 'syncconf', 'wg0', '/dev/stdin'], input=config_data)
    except Exception as e:
        print(f"Error syncing wg conf: {e}")
    
    # Construct peer config
    hostname = _get_ddns_hostname() or request.host.split(':')[0]
    
    peer_conf = f"""[Interface]
PrivateKey = {priv}
Address = {ip}
DNS = 1.1.1.1

[Peer]
PublicKey = {server_pub}
PresharedKey = {psk}
Endpoint = {hostname}:{WG_PORT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
    
    # Generate QR code
    import base64
    qr_b64 = ''
    try:
        # Input must be bytes if text is False (default)
        # qrencode expects input on stdin
        qr_proc = subprocess.run(['qrencode', '-o', '-', '-t', 'PNG'], input=peer_conf.encode('utf-8'), capture_output=True)
        if qr_proc.returncode == 0:
            qr_b64 = base64.b64encode(qr_proc.stdout).decode('utf-8')
    except Exception as e:
        print(f"QR code generation failed: {e}")

    return jsonify({
        'success': True,
        'peer': new_peer,
        'config': peer_conf,
        'qr_code': qr_b64
    })

@wireguard_bp.route('/peer/<public_key>', methods=['DELETE'])
@admin_required
def delete_peer(public_key):
    err = require_tools('wg')
    if err:
        return err
    # Normalize key (url encoded?)
    import urllib.parse
    public_key = urllib.parse.unquote(public_key)
    
    peers = _get_peers()
    new_peers = [p for p in peers if p['PublicKey'] != public_key]
    
    if len(peers) == len(new_peers):
        return jsonify({'error': 'Peer not found'}), 404
        
    server_priv, _ = _get_wg_keys()
    if not server_priv:
        return jsonify({'error': 'Cannot read server private key'}), 500
    _write_config(server_priv, WG_PORT, new_peers)
    
    # Reload — use pipe to avoid bash process substitution (same pattern as add_peer)
    try:
        strip_proc = subprocess.run(['sudo', 'wg-quick', 'strip', 'wg0'], capture_output=True)
        if strip_proc.returncode == 0:
            subprocess.run(['sudo', 'wg', 'syncconf', 'wg0', '/dev/stdin'], input=strip_proc.stdout)
    except Exception as e:
        print(f"Error syncing wg conf on delete: {e}")
    
    return jsonify({'success': True})



import threading as _wg_threading
import secrets as _wg_secrets

@wireguard_bp.route('/install', methods=['POST'])
@admin_required
def wireguard_install():
    """Install wireguard-tools via apt. Returns task_id for progress tracking."""
    task_id = _wg_secrets.token_hex(8)
    _socketio = getattr(wireguard_bp, '_socketio', None)

    def _bg():
        def _emit(stage, pct, msg):
            if _socketio:
                _socketio.emit('wireguard_install', {
                    'task_id': task_id, 'stage': stage, 'percent': pct, 'message': msg
                })

        _emit('start', 0, 'Installing wireguard-tools…')
        try:
            r = subprocess.run(
                ['apt-get', 'install', '-y', 'wireguard', 'wireguard-tools', 'qrencode'],
                capture_output=True, text=True, timeout=300
            )
            if r.returncode != 0:
                _emit('error', 0, f'Installation error: {r.stderr[:300]}')
                return
            _emit('done', 100, 'WireGuard installed!')
        except Exception as e:
            _emit('error', 0, str(e))

    _wg_threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


@wireguard_bp.route('/uninstall', methods=['POST'])
@admin_required
def wireguard_uninstall():
    """Stop WireGuard interface. Optionally wipe config."""
    wipe = (request.json or {}).get('wipe_data', False)
    # Stop interface
    try:
        subprocess.run(['wg-quick', 'down', 'wg0'], capture_output=True, timeout=15)
    except Exception:
        pass
    if wipe:
        import glob
        for f in glob.glob('/etc/wireguard/*.conf'):
            try:
                os.remove(f)
            except Exception:
                pass
    return jsonify({'ok': True})


@wireguard_bp.route('/pkg-status', methods=['GET'])
@admin_required
def wireguard_pkg_status():
    installed = _ensure_wg_installed()
    return jsonify({'installed': installed})
