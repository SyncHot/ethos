"""
EthOS — Cron Manager (Scheduled Tasks)
Manage crontab entries for the root user.
"""

import os
import sys
from flask import Blueprint, jsonify, request
from blueprints.admin_required import admin_required
import subprocess
import re
import shlex

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import require_tools, check_tool

cron_bp = Blueprint('cron', __name__, url_prefix='/api/cron')

# ────────────────────────── helpers ──────────────────────────

_CRON_FIELD = re.compile(
    r'^(\*|[0-9]{1,2}(?:-[0-9]{1,2})?(?:/[0-9]{1,2})?(?:,[0-9]{1,2}(?:-[0-9]{1,2})?(?:/[0-9]{1,2})?)*)$'
)

_FIELD_RANGES = {
    'minute': (0, 59),
    'hour':   (0, 23),
    'dom':    (1, 31),
    'month':  (1, 12),
    'dow':    (0, 7),
}


def _validate_field(value, field_name):
    """Validate a single cron schedule field."""
    value = str(value).strip()
    if not value:
        return None, f'{field_name} is required'
    if not _CRON_FIELD.match(value):
        return None, f'Invalid {field_name}: {value}'
    lo, hi = _FIELD_RANGES.get(field_name, (0, 59))
    for part in value.replace('/', ',').replace('-', ',').replace('*', '').split(','):
        part = part.strip()
        if part and part.isdigit():
            n = int(part)
            if n < lo or n > hi:
                return None, f'{field_name} value {n} out of range ({lo}-{hi})'
    return value, None


def _read_crontab():
    """Return list of raw crontab lines for root."""
    try:
        result = subprocess.run(
            ['sudo', '-n', 'crontab', '-l'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            if 'no crontab' in (result.stderr or '').lower():
                return []
            return []
        return result.stdout.splitlines()
    except Exception:
        return []


def _write_crontab(lines):
    """Write lines as root crontab. Returns (ok, error_msg)."""
    content = '\n'.join(lines)
    if content and not content.endswith('\n'):
        content += '\n'
    try:
        result = subprocess.run(
            ['sudo', '-n', 'crontab', '-'],
            input=content, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or 'Failed to write crontab'
        return True, None
    except Exception as e:
        return False, str(e)


def _parse_jobs(lines):
    """Parse crontab lines into structured job list.

    Each job is a dict with: index, minute, hour, dom, month, dow,
    command, description, enabled.
    Non-cron lines (env vars, blank lines) are preserved but not
    returned as jobs.
    """
    jobs = []
    pending_comment = ''
    cron_re = re.compile(
        r'^(#\s*)?'                          # optional disabled marker
        r'(@(?:reboot|yearly|annually|monthly|weekly|daily|hourly|midnight)|'
        r'([\d\*,/\-]+)\s+([\d\*,/\-]+)\s+([\d\*,/\-]+)\s+([\d\*,/\-]+)\s+([\d\*,/\-]+))'
        r'\s+(.+)$'
    )
    # Description comment pattern: "# DESC: some text"
    desc_re = re.compile(r'^#\s*DESC:\s*(.*)$')

    for idx, raw in enumerate(lines):
        stripped = raw.strip()

        # Collect description comments
        m_desc = desc_re.match(stripped)
        if m_desc:
            pending_comment = m_desc.group(1).strip()
            continue

        m = cron_re.match(stripped)
        if m:
            disabled_marker = m.group(1)  # '# ' or None
            enabled = disabled_marker is None

            special = m.group(2) if m.group(2) and m.group(2).startswith('@') else None
            if special:
                minute = hour = dom = month = dow = special
                command = m.group(8).strip() if m.group(8) else ''
            else:
                minute = m.group(3)
                hour = m.group(4)
                dom = m.group(5)
                month = m.group(6)
                dow = m.group(7)
                command = m.group(8).strip() if m.group(8) else ''

            jobs.append({
                'index': idx,
                'minute': minute,
                'hour': hour,
                'dom': dom,
                'month': month,
                'dow': dow,
                'command': command,
                'description': pending_comment,
                'enabled': enabled,
            })
            pending_comment = ''
        else:
            # Non-matching line (env var, blank, generic comment) — skip
            if not m_desc:
                pending_comment = ''

    return jobs


def _build_cron_line(minute, hour, dom, month, dow, command, description, enabled=True):
    """Build one or two crontab lines (description comment + cron entry)."""
    lines = []
    if description:
        lines.append(f'# DESC: {description}')
    prefix = '# ' if not enabled else ''
    lines.append(f'{prefix}{minute} {hour} {dom} {month} {dow} {command}')
    return lines


def _replace_job(lines, job_index, new_lines):
    """Replace a job at line index (and its preceding DESC comment if present)."""
    desc_re = re.compile(r'^#\s*DESC:\s*')
    start = job_index
    if start > 0 and desc_re.match(lines[start - 1]):
        start -= 1
    # Remove old lines (desc + job)
    count = job_index - start + 1
    return lines[:start] + new_lines + lines[start + count:]


def _delete_job(lines, job_index):
    """Remove a job at line index (and its preceding DESC comment if present)."""
    desc_re = re.compile(r'^#\s*DESC:\s*')
    start = job_index
    if start > 0 and desc_re.match(lines[start - 1]):
        start -= 1
    count = job_index - start + 1
    return lines[:start] + lines[start + count:]


# ────────────────────────── routes ───────────────────────────

@cron_bp.route('/jobs', methods=['GET'])
@admin_required
def list_jobs():
    """Return all cron jobs for root."""
    err = require_tools('crontab')
    if err:
        return err
    lines = _read_crontab()
    jobs = _parse_jobs(lines)
    return jsonify({'jobs': jobs})


@cron_bp.route('/jobs', methods=['POST'])
@admin_required
def create_job():
    """Add a new cron job."""
    err = require_tools('crontab')
    if err:
        return err
    data = request.json or {}
    minute = data.get('minute', '*')
    hour = data.get('hour', '*')
    dom = data.get('dom', '*')
    month = data.get('month', '*')
    dow = data.get('dow', '*')
    command = (data.get('command') or '').strip()
    description = (data.get('description') or '').strip()

    # Validate schedule fields
    for name, val in [('minute', minute), ('hour', hour), ('dom', dom),
                      ('month', month), ('dow', dow)]:
        _, err = _validate_field(val, name)
        if err:
            return jsonify({'error': err}), 400

    if not command:
        return jsonify({'error': 'Command is required'}), 400
    if len(command) > 2048:
        return jsonify({'error': 'Command too long (max 2048 chars)'}), 400
    # Sanitize description (single line, no control chars)
    description = re.sub(r'[\r\n]', ' ', description)[:256]

    lines = _read_crontab()
    new_lines = _build_cron_line(minute, hour, dom, month, dow, command, description)
    lines.extend(new_lines)

    ok, err = _write_crontab(lines)
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'status': 'ok'})


@cron_bp.route('/jobs/<int:index>', methods=['PUT'])
@admin_required
def update_job(index):
    """Update an existing cron job identified by its line index."""
    err = require_tools('crontab')
    if err:
        return err
    data = request.json or {}
    minute = data.get('minute', '*')
    hour = data.get('hour', '*')
    dom = data.get('dom', '*')
    month = data.get('month', '*')
    dow = data.get('dow', '*')
    command = (data.get('command') or '').strip()
    description = (data.get('description') or '').strip()
    enabled = data.get('enabled', True)

    for name, val in [('minute', minute), ('hour', hour), ('dom', dom),
                      ('month', month), ('dow', dow)]:
        _, err = _validate_field(val, name)
        if err:
            return jsonify({'error': err}), 400

    if not command:
        return jsonify({'error': 'Command is required'}), 400
    if len(command) > 2048:
        return jsonify({'error': 'Command too long (max 2048 chars)'}), 400
    description = re.sub(r'[\r\n]', ' ', description)[:256]

    lines = _read_crontab()
    if index < 0 or index >= len(lines):
        return jsonify({'error': 'Invalid job index'}), 404

    new_lines = _build_cron_line(minute, hour, dom, month, dow, command, description, enabled)
    lines = _replace_job(lines, index, new_lines)

    ok, err = _write_crontab(lines)
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'status': 'ok'})


@cron_bp.route('/jobs/<int:index>', methods=['DELETE'])
@admin_required
def delete_job(index):
    """Delete a cron job by its line index."""
    err = require_tools('crontab')
    if err:
        return err
    lines = _read_crontab()
    if index < 0 or index >= len(lines):
        return jsonify({'error': 'Invalid job index'}), 404

    lines = _delete_job(lines, index)
    ok, err = _write_crontab(lines)
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'status': 'ok'})


@cron_bp.route('/jobs/<int:index>/toggle', methods=['POST'])
@admin_required
def toggle_job(index):
    """Enable or disable a cron job by commenting/uncommenting."""
    err = require_tools('crontab')
    if err:
        return err
    lines = _read_crontab()
    if index < 0 or index >= len(lines):
        return jsonify({'error': 'Invalid job index'}), 404

    line = lines[index]
    stripped = line.lstrip()

    # Detect if currently disabled (commented out cron line)
    cron_re = re.compile(
        r'^#\s*'
        r'([\d\*,/\-]+\s+[\d\*,/\-]+\s+[\d\*,/\-]+\s+[\d\*,/\-]+\s+[\d\*,/\-]+\s+.+)$'
    )
    m = cron_re.match(stripped)
    if m:
        # Currently disabled → enable
        lines[index] = m.group(1)
        new_state = True
    else:
        # Currently enabled → disable
        lines[index] = '# ' + line
        new_state = False

    ok, err = _write_crontab(lines)
    if not ok:
        return jsonify({'error': err}), 500
    return jsonify({'success': True, 'enabled': new_state,
                    'message': f'Job {"enabled" if new_state else "disabled"}'})
