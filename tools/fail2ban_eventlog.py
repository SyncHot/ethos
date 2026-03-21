#!/usr/bin/env python3
import sqlite3
import time
import sys
import json
import os
import pwd
import smtplib
import socket
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

DB_PATH = '/opt/ethos/logs/eventlog.db'
ENV_FILE = '/opt/ethos/ethos.env'

# Load env immediately
ENV = {}
try:
    with open(ENV_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            ENV[k.strip()] = v.strip()
except Exception:
    pass


def drop_privileges():
    if os.geteuid() == 0:
        try:
            # Drop to the owner of the logs directory or 1000
            target_uid = 1000
            target_gid = 1000
            
            if os.path.exists(os.path.dirname(DB_PATH)):
                st = os.stat(os.path.dirname(DB_PATH))
                target_uid = st.st_uid
                target_gid = st.st_gid
            
            os.setgid(target_gid)
            os.setuid(target_uid)
        except Exception:
            pass


def send_email_notification(jail, ip, failures):
    """Send email alert if SMTP is configured."""
    try:
        smtp_host = ENV.get('SMTP_HOST', '').strip()
        if not smtp_host:
            return

        smtp_port = int(ENV.get('SMTP_PORT', '587'))
        smtp_user = ENV.get('SMTP_USER', '').strip()
        smtp_pass = ENV.get('SMTP_PASS', '').strip()
        smtp_to = ENV.get('SMTP_TO', smtp_user).strip()
        nas_name = ENV.get('NAS_NAME', 'EthOS').strip()

        if not smtp_to:
            return

        hostname = socket.gethostname()
        subject = f'[{nas_name}] Fail2Ban: Zbanowano IP {ip} ({jail})'
        body = (
            f'EthOS Fail2Ban Alert\n'
            f'====================\n\n'
            f'Serwer:       {hostname} ({nas_name})\n'
            f'Czas:         {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
            f'Sekcja (jail): {jail}\n'
            f'Zbanowany IP:  {ip}\n'
            f'Nieudane próby: {failures}\n\n'
            f'Aby odblokować adres IP, zaloguj się do panelu EthOS:\n'
            f'Settings -> Security -> Ochrona przed atakami (Fail2Ban)\n'
        )

        msg = MIMEMultipart()
        msg['From'] = smtp_user or f'ethos@{hostname}'
        msg['To'] = smtp_to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        use_tls = smtp_port in (465,)
        use_starttls = smtp_port in (587, 25) or ENV.get('SMTP_STARTTLS', '1') == '1'

        if use_tls:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            if use_starttls:
                server.starttls()

        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)

        server.sendmail(msg['From'], [smtp_to], msg.as_string())
        server.quit()
    except Exception:
        pass


def log_event(jail, ip, failures):
    try:
        # We drop privileges ONLY for database writing to avoid ownership issues
        drop_privileges()

        if not os.path.exists(os.path.dirname(DB_PATH)):
            return

        conn = sqlite3.connect(DB_PATH)
        ts = time.time()
        t_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = f'Fail2Ban: Zbanowano IP {ip} w sekcji {jail}'
        details = json.dumps({'ip': ip, 'jail': jail, 'failures': failures})

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
    except Exception:
        pass


if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit(0)

    jail = sys.argv[1]
    ip = sys.argv[2]
    failures = sys.argv[3] if len(sys.argv) > 3 else 'unknown'

    # 1. Send email (as root/invoker)
    send_email_notification(jail, ip, failures)
    
    # 2. Log to DB (drops privileges internally)
    log_event(jail, ip, failures)
