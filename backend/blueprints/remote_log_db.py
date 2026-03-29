import sqlite3
import os
import json
import time
from datetime import datetime
import sys

# Path setup
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path

DB_PATH = os.environ.get('REMOTE_LOG_DB_PATH', data_path('remote_logs.db'))
LOG_DIR = data_path('device_logs')

def get_db():
    from blueprints.db_pool import get_pooled_db
    return get_pooled_db(DB_PATH)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS device_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            reason TEXT,
            filename TEXT,
            content TEXT,
            system_info TEXT,
            errors_count INTEGER DEFAULT 0
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_device_id ON device_logs(device_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON device_logs(timestamp)')
    conn.commit()

    # Check if empty and migrate from JSON files
    try:
        count = conn.execute('SELECT COUNT(*) FROM device_logs').fetchone()[0]
    except Exception:
        count = 0
    conn.close()

    if count == 0 and os.path.isdir(LOG_DIR):
        _migrate_from_json()

def _migrate_from_json():
    print(f"Migrating device logs from {LOG_DIR}...")
    try:
        conn = get_db()
        import glob
        files = glob.glob(os.path.join(LOG_DIR, '**/*.json'), recursive=True)
        for filepath in files:
            if filepath.endswith('latest.json'):
                continue
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                
                device_id = data.get('device_id', 'unknown')
                reason = data.get('reason', 'unknown')
                ts = data.get('timestamp')
                # Parse timestamp if needed, but storing as string is fine for now if ISO8601
                
                system_info = json.dumps(data.get('system', {}))
                errors_count = len(data.get('errors', []))
                
                filename = os.path.basename(filepath)
                
                conn.execute(
                    'INSERT INTO device_logs (device_id, timestamp, reason, filename, content, system_info, errors_count) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (device_id, ts, reason, filename, json.dumps(data), system_info, errors_count)
                )
            except Exception as e:
                print(f"Failed to migrate {filepath}: {e}")
        conn.commit()
        conn.close()
        print("Migration complete.")
    except Exception as e:
        print(f"Migration failed: {e}")

def save_log(data):
    device_id = data.get('device_id', 'unknown')
    reason = data.get('reason', 'unknown')
    ts = data.get('timestamp', datetime.utcnow().isoformat() + 'Z')
    system_info = json.dumps(data.get('system', {}))
    errors_count = len(data.get('errors', []))
    filename = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{reason}.json"

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO device_logs (device_id, timestamp, reason, filename, content, system_info, errors_count) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (device_id, ts, reason, filename, json.dumps(data), system_info, errors_count)
        )
        conn.commit()
        return filename
    finally:
        conn.close()

def get_devices():
    conn = get_db()
    try:
        # Get distinct devices with latest info
        query = '''
            SELECT device_id, MAX(timestamp) as last_seen, count(*) as reports_count,
                   (SELECT content FROM device_logs d2 WHERE d2.device_id = d1.device_id ORDER BY timestamp DESC LIMIT 1) as latest_content
            FROM device_logs d1
            GROUP BY device_id
            ORDER BY last_seen DESC
        '''
        rows = conn.execute(query).fetchall()
        devices = []
        for r in rows:
            latest = json.loads(r['latest_content']) if r['latest_content'] else {}
            si = latest.get('system', {})
            svcs = latest.get('services', {})
            failed = [s for s, v in svcs.items() if isinstance(v, dict) and v.get('active') == 'failed']
            
            devices.append({
                'device_id': r['device_id'],
                'reports': r['reports_count'],
                'hostname': si.get('hostname', '?'),
                'ip': si.get('ip', '?'),
                'version': si.get('ethos_version', '?'),
                'last_seen': r['last_seen'],
                'reason': latest.get('reason', '?'),
                'uptime_seconds': si.get('uptime_seconds', 0),
                'disk': si.get('disk_usage', ''),
                'ram_mb': f"{si.get('ram_used_mb', '?')}/{si.get('ram_total_mb', '?')}",
                'kernel': si.get('kernel', '?'),
                'arch': si.get('arch', '?'),
                'installed_at': si.get('installed_at', ''),
                'failed_services': failed,
                'error_count': len(latest.get('errors', []))
            })
        return devices
    finally:
        conn.close()

def get_device_logs(device_id):
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT filename, timestamp, reason, errors_count, length(content) as size FROM device_logs WHERE device_id = ? ORDER BY timestamp DESC',
            (device_id,)
        ).fetchall()
        reports = []
        for r in rows:
            reports.append({
                'filename': r['filename'],
                'timestamp': r['timestamp'],
                'reason': r['reason'],
                'error_count': r['errors_count'],
                'size': r['size']
            })
        return reports
    finally:
        conn.close()

def get_log_content(device_id, filename):
    conn = get_db()
    try:
        # Match by filename mainly for backward compat, but safer to use ID if we exposed it. 
        # Using filename is fine since it's unique enough per device.
        row = conn.execute(
            'SELECT content FROM device_logs WHERE device_id = ? AND filename = ?',
            (device_id, filename)
        ).fetchone()
        if row:
            return json.loads(row['content'])
        return None
    finally:
        conn.close()

def get_latest_log(device_id):
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT content FROM device_logs WHERE device_id = ? ORDER BY timestamp DESC LIMIT 1',
            (device_id,)
        ).fetchone()
        if row:
            return json.loads(row['content'])
        return None
    finally:
        conn.close()

def delete_device_logs(device_id):
    conn = get_db()
    try:
        conn.execute('DELETE FROM device_logs WHERE device_id = ?', (device_id,))
        conn.commit()
    finally:
        conn.close()

def delete_log(device_id, filename):
    conn = get_db()
    try:
        conn.execute('DELETE FROM device_logs WHERE device_id = ? AND filename = ?', (device_id, filename))
        conn.commit()
    finally:
        conn.close()
