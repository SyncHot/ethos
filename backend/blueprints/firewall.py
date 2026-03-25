# blueprints/firewall.py — UFW firewall management and Fail2Ban integration
from flask import Blueprint, jsonify, request
import subprocess
import re
from blueprints.admin_required import admin_required
from utils import require_tools, check_tool

firewall_bp = Blueprint('firewall', __name__, url_prefix='/api/firewall')

def run_ufw(args):
    """Run ufw command with sudo -n (non-interactive)"""
    # Use sudo -n to prevent interactive password prompt if not configured
    cmd = ['sudo', '-n', 'ufw'] + args
    try:
        # timeout increased for slow operations
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception as e:
        return None, str(e), -1

@firewall_bp.route('/status', methods=['GET'])
@admin_required
def get_status():
    """Return UFW status (active/inactive) and list of numbered rules."""
    err = require_tools('ufw')
    if err:
        return err
    out, err, code = run_ufw(['status', 'numbered'])

    # Check if UFW is installed or other error
    if code != 0:
        if "command not found" in (err or ""):
            return jsonify({'error': 'UFW not installed. Please install ufw package.', 'status': 'unknown'}), 500
        # If inactive, ufw status numbered returns "Status: inactive" and code 0 usually
        if "inactive" in (out or ""):
             return jsonify({'status': 'inactive', 'rules': []})
        # If code is non-zero but output says inactive (happens on some versions)
        if "inactive" in (err or ""):
             return jsonify({'status': 'inactive', 'rules': []})

        return jsonify({'error': err or 'Unknown error'}), 500

    # Parse output
    lines = out.split('\n')
    status = 'inactive'
    if lines and 'Status: active' in lines[0]:
        status = 'active'

    rules = []
    if status == 'active':
        # Regex for rule line: [ 1] 22/tcp ALLOW IN Anywhere
        # Captures: id, to, action, direction (optional), from
        rule_pattern = re.compile(r'\[\s*(\d+)\]\s+(.*?)\s+(ALLOW|DENY|REJECT|LIMIT)(?:\s+(IN|OUT))?\s+(.*)')

        for line in lines:
            line = line.strip()
            if not line: continue
            if line.startswith('To') or line.startswith('--'): continue

            match = rule_pattern.match(line)
            if match:
                rule_id = int(match.group(1))
                to_port = match.group(2).strip()
                action = match.group(3).strip()
                direction = match.group(4) or "IN"
                from_ip = match.group(5).strip()

                # Check for comment (v6 is often in parens, comments might be #)
                comment = ""
                # ufw output doesn't always show comments in 'status numbered'.
                # 'ufw show added' shows commands with comments.
                # For now, let's just parse what we see.

                rules.append({
                    'id': rule_id,
                    'to': to_port,
                    'action': action,
                    'direction': direction,
                    'from': from_ip,
                    'comment': comment
                })

    return jsonify({'status': status, 'rules': rules})

@firewall_bp.route('/toggle', methods=['POST'])
@admin_required
def toggle_firewall():
    """Enable or disable UFW. Body: {"enable": true|false}."""
    err = require_tools('ufw')
    if err:
        return err
    data = request.json or {}
    enable = data.get('enable', False)

    # We need to force yes because 'ufw enable' prompts for confirmation
    args = ['--force', 'enable'] if enable else ['disable']

    out, err, code = run_ufw(args)
    if code != 0:
        return jsonify({'error': err or f'Failed to {"enable" if enable else "disable"} firewall'}), 500

    return jsonify({'status': 'ok', 'enabled': enable, 'output': out})

@firewall_bp.route('/rules', methods=['POST'])
@admin_required
def manage_rules():
    """Add, delete, or reset UFW rules. Body: {"action": "add"|"delete"|"reset_defaults", ...}."""
    err = require_tools('ufw')
    if err:
        return err
    data = request.json or {}
    action = data.get('action') # add, delete

    if action == 'delete':
        rule_id = data.get('id')
        if not rule_id:
            return jsonify({'error': 'Missing rule ID'}), 400

        # Delete by ID: sudo ufw --force delete <id>
        # We use --force to avoid confirmation prompt "Delete rule X (y|n)?"
        out, err, code = run_ufw(['--force', 'delete', str(rule_id)])
        if code != 0:
            return jsonify({'error': err or 'Failed to delete rule'}), 500
        return jsonify({'status': 'ok'})

    elif action == 'add':
        # Expected: proto (tcp/udp), port (22), from_ip (any/1.2.3.4)
        proto = data.get('proto') # tcp, udp, or None (both)
        port = data.get('port')
        from_ip = data.get('from', 'any')

        if not port:
             return jsonify({'error': 'Missing port'}), 400

        # Construct command: ufw allow [proto] from [from] to any port [port]
        # Example: ufw allow 22/tcp
        # Example: ufw allow from 192.168.1.5 to any port 22 proto tcp

        cmd_args = ['allow']

        # Order matters for ufw syntax somewhat, but 'allow <port>/<proto>' is simplest
        # 'allow from <ip> to any port <port> proto <proto>' is most flexible

        if from_ip and from_ip.lower() != 'any':
             cmd_args.extend(['from', from_ip])
        else:
             # If from is any, we can skip 'from any' unless we want to be explicit,
             # but 'ufw allow <port>' implies from any.
             # However to keep structure consistent:
             cmd_args.extend(['from', 'any'])

        cmd_args.extend(['to', 'any', 'port', str(port)])

        if proto and proto.lower() in ['tcp', 'udp']:
            cmd_args.extend(['proto', proto.lower()])

        out, err, code = run_ufw(cmd_args)
        if code != 0:
             return jsonify({'error': err or 'Failed to add rule'}), 500
        return jsonify({'status': 'ok'})

    elif action == 'reset_defaults':
        # Apply default EthOS rules
        # 0. Reset everything
        run_ufw(['--force', 'reset'])

        # 1. Enable logging
        run_ufw(['logging', 'on'])

        # 2. Set defaults
        run_ufw(['default', 'deny', 'incoming'])
        run_ufw(['default', 'allow', 'outgoing'])

        # 3. Allow specific services
        defaults = [
            # SSH
            ['allow', '22/tcp'],
            # EthOS Web
            ['allow', '9000/tcp'],
            # Samba (TCP+UDP)
            ['allow', '139,445/tcp'],
            ['allow', '137,138/udp'],
            # Plex
            ['allow', '32400/tcp'],
            # DNS/DHCP (optional, but good for local net)
            ['allow', '53'],
            ['allow', '67,68/udp']
        ]

        for rule in defaults:
            run_ufw(rule)

        # Ensure enabled
        run_ufw(['--force', 'enable'])

        return jsonify({'status': 'ok'})

    return jsonify({'error': 'Invalid action'}), 400


@firewall_bp.route('/banned', methods=['GET'])
@admin_required
def get_banned_ips():
    """Return banned IPs from Fail2Ban jails."""
    err = require_tools('fail2ban-client')
    if err:
        return err
    try:
        result = subprocess.run(
            ['sudo', '-n', 'fail2ban-client', 'status'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return jsonify({'error': result.stderr.strip() or 'fail2ban not running', 'jails': []})

        jails = []
        match = re.search(r'Jail list:\s+(.*)', result.stdout)
        if match:
            jails = [j.strip() for j in match.group(1).split(',') if j.strip()]

        jail_data = []
        for jail in jails:
            jr = subprocess.run(
                ['sudo', '-n', 'fail2ban-client', 'status', jail],
                capture_output=True, text=True, timeout=5
            )
            if jr.returncode != 0:
                continue
            banned_ips = []
            m = re.search(r'Banned IP list:\s+(.*)', jr.stdout)
            if m and m.group(1).strip():
                banned_ips = m.group(1).strip().split()
            jail_data.append({'name': jail, 'banned_ips': banned_ips})

        return jsonify({'jails': jail_data})
    except Exception as e:
        return jsonify({'error': str(e), 'jails': []})

@firewall_bp.route('/unban', methods=['POST'])
@admin_required
def unban_ip():
    """Unban an IP from a Fail2Ban jail. Body: {"jail": "sshd", "ip": "1.2.3.4"}."""
    err = require_tools('fail2ban-client')
    if err:
        return err
    data = request.json or {}
    jail = data.get('jail')
    ip = data.get('ip')

    if not jail or not ip:
        return jsonify({'error': 'Missing jail or IP'}), 400

    # validate jail/ip to prevent injection (simple alphanumeric/dot/colon check)
    if not re.match(r'^[a-zA-Z0-9_\-]+$', jail):
        return jsonify({'error': 'Invalid jail name'}), 400
    if not re.match(r'^[0-9a-fA-F\.:]+$', ip):
        return jsonify({'error': 'Invalid IP address'}), 400

    # sudo fail2ban-client set <jail> unbanip <ip>
    # out, err, code = run_ufw(['unban-placeholder']) # dummy, use subprocess directly

    try:
        cmd = ['sudo', '-n', 'fail2ban-client', 'set', jail, 'unbanip', ip]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

        if result.returncode != 0:
            # If IP not banned, fail2ban returns 0 or 1?
            # Usually it says "0" if nothing unbanned, but return code is 0.
            # If error, return code non-zero.
            return jsonify({'error': result.stderr.strip() or 'Failed to unban'}), 500

        return jsonify({'status': 'ok', 'ip': ip, 'jail': jail, 'output': result.stdout.strip()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
