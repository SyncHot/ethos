"""
EthOS — Network Manager Blueprint
Manage LAN / WiFi interfaces on the host via nmcli + ip.
"""

import subprocess
import re
import shlex
import json
import os
import sys
import time
from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run
from utils import require_tools, check_tool

network_bp = Blueprint('network', __name__, url_prefix='/api/network')

_HELPER = '/opt/ethos/tools/ethos-system-helper.sh'

_DOCKER_PREFIXES = ('docker', 'br-', 'veth', 'lo', 'p2p-dev')


def _host(cmd, timeout=30):
    return _host_run(cmd, timeout=timeout)


def _is_real_iface(name):
    return not any(name.startswith(p) for p in _DOCKER_PREFIXES)


# ---------------------------------------------------------------------------
# Interfaces list
# ---------------------------------------------------------------------------

_iface_cache = {'data': None, 'ts': 0}
_IFACE_CACHE_TTL = 5  # seconds

@network_bp.route('/interfaces')
def list_interfaces():
    """Return non-Docker network interfaces with addresses and stats."""
    now = time.time()
    if _iface_cache['data'] is not None and (now - _iface_cache['ts']) < _IFACE_CACHE_TTL:
        return jsonify(_iface_cache['data'])
    try:
        r = _host("ip -j addr show 2>/dev/null")
        if r.returncode != 0:
            return jsonify({'error': 'ip command failed'}), 500
        raw = json.loads(r.stdout)
        ifaces = []
        for iface in raw:
            name = iface.get('ifname', '')
            if not _is_real_iface(name):
                continue
            # Get type
            itype = 'ethernet'
            r2 = _host(f"test -d /sys/class/net/{shlex.quote(name)}/wireless && echo wifi")
            if 'wifi' in (r2.stdout or ''):
                itype = 'wifi'

            addrs = []
            for ai in iface.get('addr_info', []):
                addrs.append({
                    'family': ai.get('family'),
                    'address': ai.get('local'),
                    'prefixlen': ai.get('prefixlen'),
                    'scope': ai.get('scope'),
                })

            # Speed / duplex (ethernet)
            speed = None
            if itype == 'ethernet':
                rs = _host(f"cat /sys/class/net/{shlex.quote(name)}/speed 2>/dev/null")
                if rs.returncode == 0 and rs.stdout.strip().isdigit():
                    speed = int(rs.stdout.strip())

            # WiFi connection info
            wifi_info = None
            if itype == 'wifi':
                rw = _host(f"nmcli -t -f GENERAL.CONNECTION,GENERAL.STATE device show {shlex.quote(name)} 2>/dev/null")
                if rw.returncode == 0:
                    conn = ''
                    state = ''
                    for line in rw.stdout.strip().split('\n'):
                        if line.startswith('GENERAL.CONNECTION:'):
                            conn = line.split(':', 1)[1].strip()
                        elif line.startswith('GENERAL.STATE:'):
                            state = line.split(':', 1)[1].strip()
                    wifi_info = {'connection': conn if conn != '--' else '', 'state': state}

            # RX/TX bytes
            rx_bytes = tx_bytes = 0
            try:
                rxr = _host(f"cat /sys/class/net/{shlex.quote(name)}/statistics/rx_bytes 2>/dev/null")
                txr = _host(f"cat /sys/class/net/{shlex.quote(name)}/statistics/tx_bytes 2>/dev/null")
                rx_bytes = int(rxr.stdout.strip()) if rxr.returncode == 0 else 0
                tx_bytes = int(txr.stdout.strip()) if txr.returncode == 0 else 0
            except (ValueError, AttributeError):
                pass

            # operstate for wifi is DOWN when not connected (no carrier), use flags instead
            flags = iface.get('flags', [])
            operstate = iface.get('operstate', 'UNKNOWN').upper()
            if itype == 'wifi' and 'UP' in flags:
                effective_state = 'UP'
            else:
                effective_state = operstate

            ifaces.append({
                'name': name,
                'type': itype,
                'mac': iface.get('address', ''),
                'mtu': iface.get('mtu', 0),
                'state': effective_state,
                'flags': flags,
                'addresses': addrs,
                'speed': speed,
                'wifi': wifi_info,
                'rx_bytes': rx_bytes,
                'tx_bytes': tx_bytes,
            })

        # Default gateway
        rg = _host("ip -j route show default 2>/dev/null")
        gateway = None
        if rg.returncode == 0:
            try:
                routes = json.loads(rg.stdout)
                if routes:
                    gateway = {'ip': routes[0].get('gateway'), 'dev': routes[0].get('dev')}
            except (json.JSONDecodeError, IndexError):
                pass

        # DNS
        rd = _host("cat /etc/resolv.conf 2>/dev/null")
        dns_servers = []
        if rd.returncode == 0:
            for line in rd.stdout.split('\n'):
                if line.strip().startswith('nameserver'):
                    dns_servers.append(line.strip().split()[1])

        # Hostname
        rh = _host("hostname 2>/dev/null")
        hostname = rh.stdout.strip() if rh.returncode == 0 else ''

        result = {
            'interfaces': ifaces,
            'gateway': gateway,
            'dns': dns_servers,
            'hostname': hostname,
        }
        _iface_cache['data'] = result
        _iface_cache['ts'] = time.time()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Interface up / down
# ---------------------------------------------------------------------------

@network_bp.route('/interface/<name>/up', methods=['POST'])
def iface_up(name):
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '', name)
    # For WiFi, enable radio via NetworkManager
    r2 = _host(f"test -d /sys/class/net/{shlex.quote(safe)}/wireless && echo wifi")
    if 'wifi' in (r2.stdout or ''):
        _host(f"sudo {_HELPER} nmcli radio wifi on 2>&1")
    r = _host(f"sudo {_HELPER} ip-link {shlex.quote(safe)} up 2>&1")
    if r.returncode != 0:
        return jsonify({'error': r.stdout.strip() or r.stderr.strip()}), 400
    return jsonify({'success': True})


@network_bp.route('/interface/<name>/down', methods=['POST'])
def iface_down(name):
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '', name)
    # For WiFi, disable radio via NetworkManager
    r2 = _host(f"test -d /sys/class/net/{shlex.quote(safe)}/wireless && echo wifi")
    if 'wifi' in (r2.stdout or ''):
        _host(f"sudo {_HELPER} nmcli radio wifi off 2>&1")
        return jsonify({'success': True})
    r = _host(f"sudo {_HELPER} ip-link {shlex.quote(safe)} down 2>&1")
    if r.returncode != 0:
        return jsonify({'error': r.stdout.strip() or r.stderr.strip()}), 400
    return jsonify({'success': True})


def _nmcli_split(line):
    """Split nmcli terse-mode line on unescaped colons."""
    parts = []
    current = []
    i = 0
    while i < len(line):
        if line[i] == '\\' and i + 1 < len(line) and line[i + 1] == ':':
            current.append(':')
            i += 2
        elif line[i] == ':':
            parts.append(''.join(current))
            current = []
            i += 1
        else:
            current.append(line[i])
            i += 1
    parts.append(''.join(current))
    return parts


# ---------------------------------------------------------------------------
# WiFi scanning
# ---------------------------------------------------------------------------

@network_bp.route('/wifi/scan', methods=['GET', 'POST'])
def wifi_scan():
    """Trigger WiFi rescan and return results."""
    err = require_tools('nmcli')
    if err:
        return err
    try:
        # Find WiFi interface
        wifi_iface = _find_wifi_iface()
        if not wifi_iface:
            return jsonify({'error': 'No WiFi interface'}), 404

        # Bring up interface if down
        _host(f"sudo {_HELPER} ip-link {shlex.quote(wifi_iface)} up 2>/dev/null")

        # Rescan
        _host(f"sudo {_HELPER} nmcli device wifi rescan ifname {shlex.quote(wifi_iface)} 2>/dev/null")

        import time
        time.sleep(3)

        # Get results
        r = _host(
            f"nmcli -t -f SSID,SIGNAL,SECURITY,FREQ,BSSID,RATE,MODE "
            f"device wifi list ifname {shlex.quote(wifi_iface)} 2>/dev/null"
        )
        networks = []
        seen_ssids = set()
        if r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = _nmcli_split(line)
                if len(parts) >= 5:
                    ssid = parts[0].strip()
                    if not ssid or ssid in seen_ssids:
                        continue
                    # Skip the device's own hotspot AP
                    if ssid.lower() == 'ethos':
                        continue
                    seen_ssids.add(ssid)
                    signal = 0
                    try:
                        signal = int(parts[1].strip())
                    except ValueError:
                        pass
                    security = parts[2].strip()
                    freq = parts[3].strip()
                    bssid = parts[4].strip()
                    rate = parts[5].strip() if len(parts) > 5 else ''
                    mode = parts[6].strip() if len(parts) > 6 else ''

                    band = '5 GHz' if '5' in freq[:1] else '2.4 GHz'
                    networks.append({
                        'ssid': ssid,
                        'signal': signal,
                        'security': security,
                        'frequency': freq,
                        'band': band,
                        'bssid': bssid,
                        'rate': rate,
                        'mode': mode,
                    })

        # Sort by signal strength
        networks.sort(key=lambda n: n['signal'], reverse=True)

        return jsonify({'interface': wifi_iface, 'networks': networks})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Known WiFi connections
# ---------------------------------------------------------------------------

@network_bp.route('/wifi/saved')
def wifi_saved():
    """List saved WiFi connections."""
    err = require_tools('nmcli')
    if err:
        return err
    try:
        r = _host("nmcli -t -f NAME,TYPE connection show 2>/dev/null")
        connections = []
        if r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = _nmcli_split(line)
                if len(parts) >= 2 and parts[1].strip() in ('802-11-wireless', 'wifi'):
                    connections.append({'name': parts[0].strip()})
        return jsonify(connections)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# WiFi connect / disconnect / forget
# ---------------------------------------------------------------------------

@network_bp.route('/wifi/connect', methods=['POST'])
def wifi_connect():
    """Connect to a WiFi network."""
    err = require_tools('nmcli')
    if err:
        return err
    data = request.json or {}
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()

    if not ssid:
        return jsonify({'error': 'SSID required'}), 400

    wifi_iface = _find_wifi_iface()
    if not wifi_iface:
        return jsonify({'error': 'No WiFi interface'}), 404

    try:
        # Bring interface up
        _host(f"sudo {_HELPER} ip-link {shlex.quote(wifi_iface)} up 2>/dev/null")

        # Try connecting
        if password:
            r = _host(
                f"sudo {_HELPER} nmcli device wifi connect {shlex.quote(ssid)} "
                f"password {shlex.quote(password)} "
                f"ifname {shlex.quote(wifi_iface)} 2>&1",
                timeout=30
            )
        else:
            r = _host(
                f"sudo {_HELPER} nmcli device wifi connect {shlex.quote(ssid)} "
                f"ifname {shlex.quote(wifi_iface)} 2>&1",
                timeout=30
            )

        output = (r.stdout or '').strip()
        if r.returncode == 0 and 'successfully' in output.lower():
            # Wait for DHCP to assign an IP then return it
            import time
            new_ip = None
            for _ in range(10):
                time.sleep(1)
                r2 = _host(f"ip -4 -o addr show {shlex.quote(wifi_iface)} 2>/dev/null")
                if r2.returncode == 0:
                    for ln in r2.stdout.strip().split('\n'):
                        parts = ln.split()
                        for j, p in enumerate(parts):
                            if p == 'inet' and j + 1 < len(parts):
                                new_ip = parts[j + 1].split('/')[0]
                                break
                    if new_ip:
                        break
            # Also grab hostname for .local address
            hostname = ''
            try:
                r3 = _host("hostname 2>/dev/null")
                hostname = r3.stdout.strip() if r3.returncode == 0 else ''
            except Exception:
                pass
            # Stop AP hotspot if it was running (WiFi connected → AP no longer needed)
            try:
                _host(f"sudo {_HELPER} ap-control stop 2>/dev/null || true", timeout=10)
            except Exception:
                pass
            return jsonify({
                'success': True,
                'message': output,
                'new_ip': new_ip,
                'hostname': hostname,
            })

        # Check common errors
        if 'no network with SSID' in output.lower():
            return jsonify({'error': 'Network with specified name not found'}), 400
        if 'password' in output.lower() or 'secrets' in output.lower():
            return jsonify({'error': 'Invalid WiFi password'}), 401

        return jsonify({'error': output or 'Connection failed'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@network_bp.route('/wifi/disconnect', methods=['POST'])
def wifi_disconnect():
    """Disconnect WiFi."""
    err = require_tools('nmcli')
    if err:
        return err
    wifi_iface = _find_wifi_iface()
    if not wifi_iface:
        return jsonify({'error': 'No WiFi interface'}), 404
    try:
        r = _host(f"sudo {_HELPER} nmcli device disconnect {shlex.quote(wifi_iface)} 2>&1")
        return jsonify({'status': 'ok', 'output': (r.stdout or '').strip()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@network_bp.route('/wifi/forget', methods=['POST'])
def wifi_forget():
    """Delete a saved WiFi connection."""
    err = require_tools('nmcli')
    if err:
        return err
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Connection name required'}), 400
    try:
        r = _host(f"sudo {_HELPER} nmcli connection delete {shlex.quote(name)} 2>&1")
        if r.returncode == 0:
            return jsonify({'success': True})
        return jsonify({'error': (r.stdout or r.stderr or '').strip()}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# WiFi status
# ---------------------------------------------------------------------------

@network_bp.route('/wifi/status')
def wifi_status():
    """Get current WiFi connection details."""
    err = require_tools('nmcli')
    if err:
        return err
    wifi_iface = _find_wifi_iface()
    if not wifi_iface:
        return jsonify({'error': 'No WiFi interface'}), 404
    try:
        r = _host(f"nmcli -t -f active,ssid,signal,security,freq,bssid device wifi list ifname {shlex.quote(wifi_iface)} 2>/dev/null")
        connected = None
        if r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                parts = _nmcli_split(line)
                if len(parts) >= 2 and parts[0].strip().lower() == 'yes':
                    connected = {
                        'ssid': parts[1].strip(),
                        'signal': int(parts[2].strip()) if len(parts) > 2 and parts[2].strip().isdigit() else 0,
                        'security': parts[3].strip() if len(parts) > 3 else '',
                        'frequency': parts[4].strip() if len(parts) > 4 else '',
                        'bssid': parts[5].strip() if len(parts) > 5 else '',
                    }
                    break

        # IP info
        r2 = _host(f"ip -j addr show {shlex.quote(wifi_iface)} 2>/dev/null")
        ip_addr = None
        if r2.returncode == 0:
            try:
                data = json.loads(r2.stdout)
                if data:
                    for ai in data[0].get('addr_info', []):
                        if ai.get('family') == 'inet':
                            ip_addr = ai.get('local')
                            break
            except (json.JSONDecodeError, IndexError):
                pass

        return jsonify({
            'interface': wifi_iface,
            'connected': connected,
            'ip_address': ip_addr,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# WiFi AP (hotspot) management
# ---------------------------------------------------------------------------

AP_SCRIPT_PATHS = ['/usr/local/bin/ethos-ap', '/opt/ethos/ethos-ap.sh']

def _ap_script():
    """Find the AP script on the host."""
    for p in AP_SCRIPT_PATHS:
        r = _host(f"test -x {p} && echo ok", timeout=5)
        if (r.stdout or '').strip() == 'ok':
            return p
    # fallback
    return AP_SCRIPT_PATHS[0]

@network_bp.route('/ap/status')
def ap_status():
    """Check if the WiFi hotspot is active."""
    err = require_tools('nmcli')
    if err:
        return err
    try:
        script = _ap_script()
        r = _host(f"bash {script} status 2>/dev/null")
        lines = (r.stdout or '').strip().split('\n')
        active = lines[0].strip() == 'active' if lines else False
        info = {}
        for line in lines[1:]:
            if '=' in line:
                k, v = line.split('=', 1)
                info[k.strip()] = v.strip()
        return jsonify({
            'active': active,
            'ssid': info.get('ssid', 'ethos'),
            'interface': info.get('interface', ''),
            'ip': info.get('ip', '192.168.42.1'),
            'clients': int(info.get('clients', 0)),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@network_bp.route('/ap/start', methods=['POST'])
def ap_start():
    """Start the WiFi hotspot."""
    err = require_tools('nmcli')
    if err:
        return err
    try:
        script = _ap_script()
        r = _host(f"sudo {_HELPER} ap-control start 2>&1", timeout=30)
        output = (r.stdout or '').strip()
        if r.returncode == 0:
            return jsonify({'status': 'ok', 'output': output})
        return jsonify({'error': output or 'Failed to start hotspot'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@network_bp.route('/ap/stop', methods=['POST'])
def ap_stop():
    """Stop the WiFi hotspot and reconnect to normal WiFi."""
    err = require_tools('nmcli')
    if err:
        return err
    try:
        script = _ap_script()
        r = _host(f"sudo {_HELPER} ap-control stop 2>&1", timeout=30)
        output = (r.stdout or '').strip()
        return jsonify({'status': 'ok', 'output': output})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_wifi_iface():
    """Find the first WiFi interface name."""
    r = _host("nmcli -t -f DEVICE,TYPE device status 2>/dev/null")
    if r.returncode == 0:
        for line in r.stdout.strip().split('\n'):
            parts = _nmcli_split(line)
            if len(parts) >= 2 and parts[1].strip() == 'wifi':
                return parts[0].strip()
    return None
