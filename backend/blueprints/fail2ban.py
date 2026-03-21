from flask import Blueprint, jsonify, request
import subprocess
import re
import os

fail2ban_bp = Blueprint('fail2ban', __name__, url_prefix='/api/fail2ban')

def run_command(cmd):
    try:
        full_cmd = ['sudo', '-n'] + cmd
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None, result.stderr.strip()
        return result.stdout.strip(), None
    except Exception as e:
        return None, str(e)

@fail2ban_bp.route('/status', methods=['GET'])
def get_status():
    # Get list of jails
    out, err = run_command(['fail2ban-client', 'status'])
    if err:
        return jsonify({'error': str(err)}), 500

    jails = []
    # Output: Jail list: sshd, ethos-web
    match = re.search(r'Jail list:\s+(.*)', out)
    if match:
        raw_jails = match.group(1).split(',')
        jails = [j.strip() for j in raw_jails if j.strip()]

    jail_stats = []
    for jail in jails:
        # Get status for each jail
        j_out, j_err = run_command(['fail2ban-client', 'status', jail])
        if j_out:
            curr_banned = 0
            total_banned = 0
            banned_ips = []

            m_curr = re.search(r'Currently banned:\s+(\d+)', j_out)
            if m_curr: curr_banned = int(m_curr.group(1))

            m_total = re.search(r'Total banned:\s+(\d+)', j_out)
            if m_total: total_banned = int(m_total.group(1))

            m_ips = re.search(r'Banned IP list:\s+(.*)', j_out)
            if m_ips:
                ips_str = m_ips.group(1).strip()
                if ips_str:
                    banned_ips = ips_str.split()

            jail_stats.append({
                'name': jail,
                'currently_banned': curr_banned,
                'total_banned': total_banned,
                'banned_ips': banned_ips
            })

    return jsonify({'jails': jail_stats})

@fail2ban_bp.route('/unban', methods=['POST'])
def unban_ip():
    data = request.json
    jail = data.get('jail')
    ip = data.get('ip')

    if not jail or not ip:
        return jsonify({'error': 'Missing jail or IP'}), 400

    out, err = run_command(['fail2ban-client', 'set', jail, 'unbanip', ip])
    if err:
        return jsonify({'error': err}), 500

    return jsonify({'success': True, 'message': f'Unbanned {ip} from {jail}'})

@fail2ban_bp.route('/whitelist', methods=['GET'])
def get_whitelist():
    # Get global ignoreip from sshd jail (which inherits default)
    out, err = run_command(['fail2ban-client', 'get', 'sshd', 'ignoreip'])

    ips = []
    if out:
        # The output format is typically space-separated list of IPs
        ips = out.split()
    elif err:
        # Fallback to defaults if sshd jail is down
        return jsonify({'whitelist': ['127.0.0.1/8', '::1', '192.168.0.0/16', '10.0.0.0/8']})

    return jsonify({'whitelist': ips})
