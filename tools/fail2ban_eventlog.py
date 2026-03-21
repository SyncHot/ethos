#!/usr/bin/env python3
import sqlite3
import time
import sys
import json
import os
import pwd
from datetime import datetime

DB_PATH = '/opt/ethos/logs/eventlog.db'

def drop_privileges():
    # Only drop privileges if we are root
    if os.geteuid() == 0:
        try:
            # Use the owner of the logs directory as the user to run as
            log_dir = os.path.dirname(DB_PATH)
            st = os.stat(log_dir)
            
            # Switch to that user
            os.setgid(st.st_gid)
            os.setuid(st.st_uid)
        except Exception as e:
            # Continue even if we can't drop privileges
            pass

def log_event(jail, ip, failures):
    try:
        drop_privileges()
        
        # Ensure DB directory exists
        if not os.path.exists(os.path.dirname(DB_PATH)):
            return

        conn = sqlite3.connect(DB_PATH)
        ts = time.time()
        t_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = f'Fail2Ban: Zbanowano IP {ip} w sekcji {jail}'
        details = json.dumps({'ip': ip, 'jail': jail, 'failures': failures})
        
        # Ensure table exists (might be created by app, but just in case)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                time TEXT,
                category TEXT,
                level TEXT,
                message TEXT,
                details TEXT
            )
        ''')

        conn.execute(
            'INSERT INTO events (ts, time, category, level, message, details) VALUES (?, ?, ?, ?, ?, ?)',
            (ts, t_str, 'security', 'warning', message, details)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Don't fail2ban if logging fails
        pass

if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit(0)
    
    jail = sys.argv[1]
    ip = sys.argv[2]
    failures = sys.argv[3] if len(sys.argv) > 3 else 'unknown'
    
    log_event(jail, ip, failures)
