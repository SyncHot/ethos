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


def _read_env():
    env = {}
    try:
        with open(ENV_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def drop_privileges():
    if os.geteuid() == 0:
        try:
            log_dir = os.path.dirname(DB_PATH)
            st = os.stat(log_dir)
            os.setgid(st.st_gid)
            os.setuid(st.st_uid)
        except Exception:
            pass


def send_email_notification(jail, ip, failures):
    """Send email alert if SMTP is configured in ethos.env."""
    try:
        env = _read_env()
        smtp_host = env.get('SMTP_HOST', '').strip()
        if not smtp_host:
            return

        smtp_port = int(env.get('SMTP_PORT', '587'))
        smtp_user = env.get('SMTP_USER', '').strip()
        smtp_pass = env.get('SMTP_PASS', '').strip()
        smtp_to = env.get('SMTP_TO', smtp_user).strip()
        nas_name = env.get('NAS_NAME', 'EthOS').strip()

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
            f'Settings → Security → Ochrona przed atakami (Fail2Ban)\n'
        )

        msg = MIMEMultipart()
        msg['From'] = smtp_user or f'ethos@{hostname}'
        msg['To'] = smtp_to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        use_tls = smtp_port in (465,)
        use_starttls = smtp_port in (587, 25) or env.get('SMTP_STARTTLS', '1') == '1'

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

    log_event(jail, ip, failures)
    send_email_notification(jail, ip, failures)
