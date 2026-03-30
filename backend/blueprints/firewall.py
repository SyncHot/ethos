# blueprints/firewall.py — UFW firewall management and Fail2Ban integration
#
# Endpoints:
#   GET  /api/firewall/status   — UFW status, default policy, and numbered rules
#   POST /api/firewall/toggle   — Enable / disable UFW
#   POST /api/firewall/rules    — Add / delete / reset rules
#   GET  /api/firewall/banned   — Fail2Ban banned IPs per jail
#   POST /api/firewall/unban    — Unban IP from a Fail2Ban jail
#   GET  /api/firewall/subnet   — Auto-detect LAN subnet

import ipaddress
import re

from flask import Blueprint, jsonify, request
from blueprints.admin_required import admin_required
from host import host_run, q

firewall_bp = Blueprint('firewall', __name__, url_prefix='/api/firewall')

# ── helpers ──────────────────────────────────────────────────────────

_VALID_ACTIONS = {'allow', 'deny', 'reject', 'limit'}
_VALID_PROTOS  = {'tcp', 'udp'}

_PRIVATE_NETS = [
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
]


def _ufw(args, timeout=15):
    """Run a ufw command via HAL and return (stdout, stderr, returncode)."""
    r = host_run(f'ufw {args}', timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def _detect_subnet():
    """Return the LAN subnet (e.g. '192.168.50.0/24') from the default iface."""
    try:
        r = host_run("ip -4 route show default | head -1 | awk '{print $5}'", timeout=5)
        iface = r.stdout.strip()
        if not iface:
            return '192.168.0.0/24'
        r2 = host_run(
            f"ip -4 addr show {q(iface)} | grep 'inet ' | head -1 | awk '{{print $2}}'",
            timeout=5,
        )
        cidr = r2.stdout.strip()
        if cidr:
            return str(ipaddress.ip_network(cidr, strict=False))
    except Exception:
        pass
    return '192.168.0.0/24'


def _classify_source(src):
    """Classify a source string as 'lan', 'public', or 'custom'."""
    s = src.strip().lower()
    if s in ('anywhere', 'anywhere (v6)', ''):
        return 'public'
    try:
        net = ipaddress.ip_network(s, strict=False)
        for pn in _PRIVATE_NETS:
            if net.subnet_of(pn):
                return 'lan'
    except (ValueError, TypeError):
        pass
    return 'custom'


def _parse_rules(output):
    """Parse 'ufw status numbered' into a list of rule dicts.

    Groups IPv4 and IPv6 rules together so the UI shows each logical
    rule only once.
    """
    rules = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('Status:') or line.startswith('To') or line.startswith('--'):
            continue

        comment = ''
        if '#' in line:
            parts = line.split('#', 1)
            line = parts[0].rstrip()
            comment = parts[1].strip()

        m = re.match(
            r'\[\s*(\d+)\]\s+'
            r'(.+?)\s+'
            r'(ALLOW|DENY|REJECT|LIMIT)'
            r'(?:\s+(IN|OUT))?\s+'
            r'(.+)',
            line,
        )
        if not m:
            continue

        rule_id   = int(m.group(1))
        to_raw    = m.group(2).strip()
        action    = m.group(3).strip()
        direction = (m.group(4) or 'IN').strip()
        from_raw  = m.group(5).strip()

        is_v6      = '(v6)' in to_raw or '(v6)' in from_raw
        to_clean   = to_raw.replace('(v6)', '').strip()
        from_clean = from_raw.replace('(v6)', '').strip()

        rules.append({
            'id':        rule_id,
            'to':        to_clean,
            'action':    action,
            'direction': direction,
            'from':      from_clean,
            'comment':   comment,
            'v6':        is_v6,
            'access':    _classify_source(from_clean),
        })

    # Group v4 + v6: keep v4 as primary, attach v6 id
    grouped = []
    v6_map = {}
    for r in rules:
        if r['v6']:
            key = (r['to'], r['action'], r['direction'], r['from'])
            v6_map[key] = r['id']
        else:
            grouped.append(r)

    for r in grouped:
        key = (r['to'], r['action'], r['direction'], r['from'])
        r['v6_id'] = v6_map.get(key)
        del r['v6']

    return grouped


# ── endpoints ────────────────────────────────────────────────────────

@firewall_bp.route('/status', methods=['GET'])
@admin_required
def get_status():
    """Return UFW status, default policies, and parsed rules."""
    out, err, code = _ufw('status numbered')
    if code != 0:
        combined = (out + ' ' + err).lower()
        if 'inactive' in combined:
            return jsonify(ok=True, status='inactive', rules=[], defaults={})
        return jsonify(error=err or 'UFW error'), 500

    status = 'active' if 'Status: active' in out else 'inactive'

    defaults = {}
    vout, _, _ = _ufw('status verbose')
    dm = re.search(r'Default:\s*(.*)', vout)
    if dm:
        for part in dm.group(1).split(','):
            part = part.strip()
            if 'incoming' in part:
                defaults['incoming'] = 'deny' if 'deny' in part else ('allow' if 'allow' in part else 'reject')
            elif 'outgoing' in part:
                defaults['outgoing'] = 'deny' if 'deny' in part else ('allow' if 'allow' in part else 'reject')

    rules = _parse_rules(out) if status == 'active' else []
    return jsonify(ok=True, status=status, rules=rules, defaults=defaults)


@firewall_bp.route('/toggle', methods=['POST'])
@admin_required
def toggle_firewall():
    """Enable or disable UFW."""
    data = request.json or {}
    enable = bool(data.get('enable', False))

    args = '--force enable' if enable else 'disable'
    out, err, code = _ufw(args)
    if code != 0:
        return jsonify(error=err or f'Failed to {"enable" if enable else "disable"} UFW'), 500

    return jsonify(ok=True, enabled=enable,
                   message=out or ('Firewall enabled' if enable else 'Firewall disabled'))


@firewall_bp.route('/rules', methods=['POST'])
@admin_required
def manage_rules():
    """Add, delete, or reset UFW rules."""
    data = request.json or {}
    action = data.get('action')

    if action == 'delete':
        rule_id = data.get('id')
        v6_id   = data.get('v6_id')
        if not rule_id:
            return jsonify(error='Missing rule ID'), 400
        # Delete v6 first (higher ID) so v4 ID stays valid
        if v6_id:
            _ufw(f'--force delete {int(v6_id)}')
        out, err, code = _ufw(f'--force delete {int(rule_id)}')
        if code != 0:
            return jsonify(error=err or 'Failed to delete rule'), 500
        return jsonify(ok=True)

    elif action == 'add':
        port      = str(data.get('port', '')).strip()
        proto     = str(data.get('proto', '')).strip().lower()
        ufw_act   = str(data.get('ufw_action', 'allow')).strip().lower()
        access    = str(data.get('access', 'public')).strip().lower()
        from_ip   = str(data.get('from', '')).strip()
        comment   = str(data.get('comment', '')).strip()

        if not port:
            return jsonify(error='Missing port'), 400
        if not re.match(r'^[\d,:]+$', port):
            return jsonify(error='Invalid port format'), 400
        if ufw_act not in _VALID_ACTIONS:
            return jsonify(error=f'Invalid action: {ufw_act}'), 400
        if proto and proto not in _VALID_PROTOS:
            return jsonify(error=f'Invalid protocol: {proto}'), 400

        # Determine source from access type
        if access == 'lan':
            source = _detect_subnet()
        elif access == 'custom' and from_ip:
            try:
                ipaddress.ip_network(from_ip, strict=False)
            except ValueError:
                try:
                    ipaddress.ip_address(from_ip)
                except ValueError:
                    return jsonify(error='Invalid IP address or subnet'), 400
            source = from_ip
        else:
            source = 'any'

        cmd = f'{ufw_act} from {q(source)} to any port {q(port)}'
        if proto:
            cmd += f' proto {proto}'
        if comment:
            cmd += f' comment {q(comment)}'

        out, err, code = _ufw(cmd)
        if code != 0:
            return jsonify(error=err or 'Failed to add rule'), 500
        return jsonify(ok=True)

    elif action == 'reset_defaults':
        _ufw('--force reset', timeout=30)
        _ufw('logging on')
        _ufw('default deny incoming')
        _ufw('default allow outgoing')

        subnet = _detect_subnet()
        defaults = [
            f'allow from {subnet} to any port 22 proto tcp comment {q("SSH")}',
            f'allow from {subnet} to any port 9000 proto tcp comment {q("EthOS Web UI")}',
            f'allow from {subnet} to any port 9001 proto tcp comment {q("EthOS WebSocket")}',
        ]
        for rule in defaults:
            _ufw(rule)

        _ufw('--force enable')
        return jsonify(ok=True)

    return jsonify(error='Invalid action'), 400


@firewall_bp.route('/banned', methods=['GET'])
@admin_required
def get_banned():
    """Return banned IPs from all Fail2Ban jails."""
    r = host_run('fail2ban-client status', timeout=5)
    if r.returncode != 0:
        return jsonify(ok=True, jails=[], note='Fail2Ban not running')

    jails = []
    m = re.search(r'Jail list:\s+(.*)', r.stdout)
    if m:
        jails = [j.strip() for j in m.group(1).split(',') if j.strip()]

    jail_data = []
    for jail in jails:
        jr = host_run(f'fail2ban-client status {q(jail)}', timeout=5)
        if jr.returncode != 0:
            continue
        banned = []
        bm = re.search(r'Banned IP list:\s+(.*)', jr.stdout)
        if bm and bm.group(1).strip():
            banned = bm.group(1).strip().split()

        failed = 0
        fm = re.search(r'Currently failed:\s+(\d+)', jr.stdout)
        if fm:
            failed = int(fm.group(1))

        total_banned = 0
        tm = re.search(r'Total banned:\s+(\d+)', jr.stdout)
        if tm:
            total_banned = int(tm.group(1))

        jail_data.append({
            'name': jail,
            'banned_ips': banned,
            'failed': failed,
            'total_banned': total_banned,
        })

    return jsonify(ok=True, jails=jail_data)


@firewall_bp.route('/unban', methods=['POST'])
@admin_required
def unban_ip():
    """Unban an IP from a Fail2Ban jail."""
    data = request.json or {}
    jail = str(data.get('jail', '')).strip()
    ip   = str(data.get('ip', '')).strip()

    if not jail or not ip:
        return jsonify(error='Missing jail or IP'), 400
    if not re.match(r'^[a-zA-Z0-9_\-]+$', jail):
        return jsonify(error='Invalid jail name'), 400
    if not re.match(r'^[0-9a-fA-F.:]+$', ip):
        return jsonify(error='Invalid IP address'), 400

    r = host_run(f'fail2ban-client set {q(jail)} unbanip {q(ip)}', timeout=5)
    if r.returncode != 0:
        return jsonify(error=r.stderr.strip() or 'Failed to unban'), 500

    return jsonify(ok=True, ip=ip, jail=jail)


@firewall_bp.route('/subnet', methods=['GET'])
@admin_required
def get_subnet():
    """Return the auto-detected LAN subnet."""
    return jsonify(ok=True, subnet=_detect_subnet())
