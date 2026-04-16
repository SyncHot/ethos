"""
Mail Server blueprint — Postfix + Dovecot management for EthOS.

Endpoints:
  GET   /api/mail-server/pkg-status         — check if packages installed
  GET   /api/mail-server/status              — detailed service status
  POST  /api/mail-server/install             — install packages (async, SocketIO)
  POST  /api/mail-server/uninstall           — remove packages and services
  POST  /api/mail-server/setup               — wizard: configure hostname, domain, first account
  GET   /api/mail-server/config              — get current server config
  PUT   /api/mail-server/config              — update hostname / relay settings
  GET   /api/mail-server/domains             — list domains
  POST  /api/mail-server/domains             — add domain
  DELETE /api/mail-server/domains/<domain>   — remove domain
  GET   /api/mail-server/domains/<d>/dns     — DNS records helper for domain
  GET   /api/mail-server/accounts            — list accounts
  POST  /api/mail-server/accounts            — create account
  PUT   /api/mail-server/accounts/<email>    — update account (password, quota, enabled)
  DELETE /api/mail-server/accounts/<email>   — delete account
  GET   /api/mail-server/aliases             — list aliases
  POST  /api/mail-server/aliases             — create alias
  DELETE /api/mail-server/aliases/<id>       — delete alias
  PUT   /api/mail-server/relay               — configure SMTP relay
  POST  /api/mail-server/test-send           — send test email
  POST  /api/mail-server/service/<action>    — start/stop/restart services
  GET   /api/mail-server/logs                — recent mail log entries
  GET   /api/mail-server/queue               — mail queue info

Webmail endpoints (IMAP proxy API):
  GET   /api/mail-server/webmail/folders                — list IMAP folders
  GET   /api/mail-server/webmail/messages               — paginated message list
  GET   /api/mail-server/webmail/message/<uid>          — full message with body
  GET   /api/mail-server/webmail/attachment/<uid>/<part> — download attachment
  POST  /api/mail-server/webmail/send                   — compose & send email
  POST  /api/mail-server/webmail/action                 — bulk actions

SnappyMail management:
  GET   /api/mail-server/webmail/status                 — SnappyMail install & config status
  POST  /api/mail-server/webmail/install                — install PHP-FPM + SnappyMail (async)
  POST  /api/mail-server/webmail/setup-domain           — create nginx vhost for mail.domain

SocketIO events emitted:
  mail_server_install   — { task_id, stage, percent, message }
  snappymail_install    — { stage, percent, message }
"""

import json
import logging
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time

from flask import Blueprint, jsonify, request

from blueprints.admin_required import admin_required
from host import (
    host_run, host_run_stream, apt_install, q,
    data_path, get_data_disk,
)

log = logging.getLogger(__name__)

mail_bp = Blueprint('mail_server', __name__, url_prefix='/api/mail-server')

# ─── Paths ────────────────────────────────────────────────────

_CERT_DIR = '/etc/letsencrypt/live'
_POSTFIX_DIR = '/etc/postfix'
_DOVECOT_DIR = '/etc/dovecot'
_OPENDKIM_DIR = '/etc/opendkim'

def _mail_data_dir():
    """Mail data root — prefers data partition."""
    dd = get_data_disk()
    if dd:
        p = os.path.join(dd, 'mail')
    else:
        p = data_path('mail')
    os.makedirs(p, exist_ok=True)
    return p

def _mail_vhosts_dir():
    """Virtual mailbox root (Maildir storage)."""
    p = os.path.join(_mail_data_dir(), 'vhosts')
    os.makedirs(p, exist_ok=True)
    return p

def _mail_db_path():
    """SQLite DB for mail accounts/domains/aliases."""
    return os.path.join(_mail_data_dir(), 'mail.db')

def _dkim_keys_dir():
    """DKIM key storage."""
    p = os.path.join(_mail_data_dir(), 'dkim-keys')
    os.makedirs(p, exist_ok=True)
    return p

def _config_path():
    """Persistent config JSON."""
    return os.path.join(_mail_data_dir(), 'config.json')


# ─── Config helpers ────────────────────────────────────────────

def _load_config():
    p = _config_path()
    if os.path.isfile(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_config(cfg):
    p = _config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w') as f:
        json.dump(cfg, f, indent=2)


# ─── DB helpers ────────────────────────────────────────────────

def _get_db():
    db = sqlite3.connect(_mail_db_path())
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute('PRAGMA foreign_keys=ON')
    return db

def _init_db():
    db = _get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS domains (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            domain      TEXT UNIQUE NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT UNIQUE NOT NULL,
            domain      TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            quota_mb    INTEGER DEFAULT 1024,
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (domain) REFERENCES domains(domain) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS aliases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            destination TEXT NOT NULL,
            domain      TEXT NOT NULL,
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (domain) REFERENCES domains(domain) ON DELETE CASCADE
        );
    ''')
    db.close()


# ─── Service helpers ───────────────────────────────────────────

def _is_installed():
    return bool(shutil.which('postfix') and shutil.which('dovecot'))

def _service_active(name):
    try:
        r = host_run(f'systemctl is-active {q(name)} 2>/dev/null', timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def _service_enabled(name):
    try:
        r = host_run(f'systemctl is-enabled {q(name)} 2>/dev/null', timeout=5)
        return r.returncode == 0
    except Exception:
        return False


# ─── Password hashing ─────────────────────────────────────────

def _hash_password(password):
    """Hash password using doveadm pw (SHA512-CRYPT)."""
    try:
        r = host_run(f'doveadm pw -s SHA512-CRYPT -p {q(password)}', timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    # Fallback: use Python hashlib
    import crypt
    return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))


# ─── Postfix config generation ─────────────────────────────────

def _generate_postfix_config(cfg):
    """Write main.cf for virtual mailbox setup."""
    hostname = cfg.get('hostname', 'mail.localhost')
    mail_data = _mail_vhosts_dir()
    db_path = _mail_db_path()

    # Virtual domain lookup via sqlite
    _write_postfix_sqlite_cf('virtual_domains.cf',
        "SELECT domain FROM domains WHERE domain = '%s'")
    _write_postfix_sqlite_cf('virtual_mailbox.cf',
        "SELECT email || '/Maildir/' FROM accounts WHERE email = '%s' AND enabled = 1")
    _write_postfix_sqlite_cf('virtual_alias.cf',
        "SELECT destination FROM aliases WHERE source = '%s' AND enabled = 1")

    relay = cfg.get('relay', {})

    main_cf_lines = [
        f'myhostname = {hostname}',
        f'mydomain = {hostname.split(".", 1)[-1] if "." in hostname else hostname}',
        'myorigin = $mydomain',
        'inet_interfaces = all',
        'inet_protocols = ipv4',
        '',
        '# Virtual mailbox configuration',
        f'virtual_mailbox_domains = sqlite:{_POSTFIX_DIR}/virtual_domains.cf',
        f'virtual_mailbox_maps = sqlite:{_POSTFIX_DIR}/virtual_mailbox.cf',
        f'virtual_alias_maps = sqlite:{_POSTFIX_DIR}/virtual_alias.cf',
        f'virtual_mailbox_base = {mail_data}',
        'virtual_minimum_uid = 100',
        f'virtual_uid_maps = static:{_get_vmail_uid()}',
        f'virtual_gid_maps = static:{_get_vmail_gid()}',
        '',
        '# LMTP delivery to Dovecot',
        'virtual_transport = lmtp:unix:private/dovecot-lmtp',
        '',
        '# TLS (SMTP inbound)',
        'smtpd_use_tls = yes',
        'smtpd_tls_security_level = may',
        'smtpd_tls_auth_only = yes',
    ]

    # TLS certs — try Let's Encrypt first
    cert_domain = hostname
    fullchain = os.path.join(_CERT_DIR, cert_domain, 'fullchain.pem')
    privkey = os.path.join(_CERT_DIR, cert_domain, 'privkey.pem')
    if os.path.isfile(fullchain) and os.path.isfile(privkey):
        main_cf_lines.append(f'smtpd_tls_cert_file = {fullchain}')
        main_cf_lines.append(f'smtpd_tls_key_file = {privkey}')
    else:
        # Try snakeoil as fallback
        if os.path.isfile('/etc/ssl/certs/ssl-cert-snakeoil.pem'):
            main_cf_lines.append('smtpd_tls_cert_file = /etc/ssl/certs/ssl-cert-snakeoil.pem')
            main_cf_lines.append('smtpd_tls_key_file = /etc/ssl/private/ssl-cert-snakeoil.key')

    main_cf_lines += [
        '',
        '# SMTP client TLS (outbound)',
        'smtp_tls_security_level = may',
        '',
        '# SASL authentication (Dovecot)',
        'smtpd_sasl_type = dovecot',
        'smtpd_sasl_path = private/auth',
        'smtpd_sasl_auth_enable = yes',
        'smtpd_sasl_security_options = noanonymous',
        'smtpd_sasl_local_domain = $myhostname',
        '',
        '# Restrictions',
        'smtpd_recipient_restrictions = '
            'permit_sasl_authenticated, '
            'permit_mynetworks, '
            'reject_unauth_destination',
        '',
        '# Limits',
        'message_size_limit = 52428800',
        'mailbox_size_limit = 0',
    ]

    # DKIM milter (socket inside Postfix chroot)
    if os.path.isfile('/var/spool/postfix/opendkim/opendkim.sock') or _service_active('opendkim'):
        main_cf_lines += [
            '',
            '# DKIM signing via OpenDKIM',
            'milter_default_action = accept',
            'milter_protocol = 6',
            'smtpd_milters = unix:opendkim/opendkim.sock',
            'non_smtpd_milters = $smtpd_milters',
        ]

    # Relay
    if relay.get('enabled') and relay.get('host'):
        rhost = relay['host']
        rport = relay.get('port', 587)
        main_cf_lines += [
            '',
            '# SMTP relay',
            f'relayhost = [{rhost}]:{rport}',
            'smtp_sasl_auth_enable = yes',
            'smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd',
            'smtp_sasl_security_options = noanonymous',
            'smtp_tls_security_level = encrypt',
        ]
        # Write sasl_passwd
        ruser = relay.get('username', '')
        rpass = relay.get('password', '')
        with open('/etc/postfix/sasl_passwd', 'w') as f:
            f.write(f'[{rhost}]:{rport} {ruser}:{rpass}\n')
        host_run('postmap /etc/postfix/sasl_passwd', timeout=10)
        host_run('chmod 600 /etc/postfix/sasl_passwd /etc/postfix/sasl_passwd.db', timeout=5)

    main_cf = '\n'.join(main_cf_lines) + '\n'

    with open(os.path.join(_POSTFIX_DIR, 'main.cf'), 'w') as f:
        f.write(main_cf)

    # Enable submission port (587) in master.cf
    _enable_submission_port()
    # Disable chroot for services that need access to SQLite DB on data partition
    _disable_postfix_chroot()


def _write_postfix_sqlite_cf(filename, query):
    """Write a Postfix sqlite lookup config file."""
    db_path = _mail_db_path()
    content = f'dbpath = {db_path}\nquery = {query}\n'
    with open(os.path.join(_POSTFIX_DIR, filename), 'w') as f:
        f.write(content)


def _enable_submission_port():
    """Enable port 587 (submission) in master.cf if not already enabled."""
    master_cf = os.path.join(_POSTFIX_DIR, 'master.cf')
    if not os.path.isfile(master_cf):
        return
    with open(master_cf) as f:
        content = f.read()

    if re.search(r'^submission\s+inet', content, re.MULTILINE):
        return  # already enabled

    submission_block = (
        '\n# Submission port (587) for authenticated clients\n'
        'submission inet n       -       y       -       -       smtpd\n'
        '  -o syslog_name=postfix/submission\n'
        '  -o smtpd_tls_security_level=encrypt\n'
        '  -o smtpd_sasl_auth_enable=yes\n'
        '  -o smtpd_tls_auth_only=yes\n'
        '  -o smtpd_reject_unlisted_recipient=no\n'
        '  -o smtpd_recipient_restrictions=permit_sasl_authenticated,reject\n'
    )
    with open(master_cf, 'a') as f:
        f.write(submission_block)


def _disable_postfix_chroot():
    """Disable chroot for Postfix services that access the SQLite DB on the data drive.

    The cleanup and smtpd processes need access to virtual_alias/mailbox SQLite
    lookups which reside on the data partition — unreachable from inside chroot.
    """
    master_cf = os.path.join(_POSTFIX_DIR, 'master.cf')
    if not os.path.isfile(master_cf):
        return
    with open(master_cf) as f:
        lines = f.readlines()

    changed = False
    new_lines = []
    for line in lines:
        # Match service lines like:  smtp      inet  n  -  y  -  -  smtpd
        # Fields: service type private unpriv chroot wakeup maxproc command
        if not line.startswith(' ') and not line.startswith('#') and not line.startswith('\t'):
            parts = line.split()
            if len(parts) >= 5 and parts[-1] in ('smtpd', 'cleanup') and parts[4] == 'y':
                parts[4] = 'n'
                line = '  '.join(parts[:5]) + '  ' + '  '.join(parts[5:]) + '\n'
                changed = True
        new_lines.append(line)

    if changed:
        with open(master_cf, 'w') as f:
            f.writelines(new_lines)


# ─── Dovecot config generation ─────────────────────────────────

def _generate_dovecot_config(cfg):
    """Write Dovecot config for virtual mailbox auth via SQLite."""
    hostname = cfg.get('hostname', 'mail.localhost')
    mail_data = _mail_vhosts_dir()
    db_path = _mail_db_path()
    uid = _get_vmail_uid()
    gid = _get_vmail_gid()

    # Main dovecot config
    dovecot_conf = f'''# EthOS Mail Server — Dovecot configuration
protocols = imap pop3 lmtp

# Logging
log_path = /var/log/dovecot.log
info_log_path = /var/log/dovecot-info.log

# Mail location — Maildir under virtual hosts
mail_location = maildir:{mail_data}/%d/%n/Maildir
mail_uid = {uid}
mail_gid = {gid}
first_valid_uid = {uid}
last_valid_uid = {uid}

# SSL
ssl = required
'''

    # TLS certs
    cert_domain = hostname
    fullchain = os.path.join(_CERT_DIR, cert_domain, 'fullchain.pem')
    privkey = os.path.join(_CERT_DIR, cert_domain, 'privkey.pem')
    if os.path.isfile(fullchain) and os.path.isfile(privkey):
        dovecot_conf += f'ssl_cert = <{fullchain}\nssl_key = <{privkey}\n'
    elif os.path.isfile('/etc/ssl/certs/ssl-cert-snakeoil.pem'):
        dovecot_conf += ('ssl_cert = </etc/ssl/certs/ssl-cert-snakeoil.pem\n'
                        'ssl_key = </etc/ssl/private/ssl-cert-snakeoil.key\n')
    else:
        dovecot_conf = dovecot_conf.replace('ssl = required', 'ssl = yes')

    dovecot_conf += f'''
# Authentication
auth_mechanisms = plain login

# Passdb — authenticate against SQLite
passdb {{
    driver = sql
    args = {_DOVECOT_DIR}/dovecot-sql.conf
}}

# Passdb — master user (for webmail backend access)
passdb {{
    driver = passwd-file
    args = {_DOVECOT_DIR}/master-users
    master = yes
}}

# Userdb — virtual users all map to vmail
userdb {{
    driver = static
    args = uid={uid} gid={gid} home={mail_data}/%d/%n
}}

# Master user separator (login as user@domain*ethos-webmail)
auth_master_user_separator = *

# LMTP service for Postfix
service lmtp {{
    unix_listener /var/spool/postfix/private/dovecot-lmtp {{
        mode = 0600
        user = postfix
        group = postfix
    }}
}}

# Auth service for Postfix SASL
service auth {{
    unix_listener /var/spool/postfix/private/auth {{
        mode = 0660
        user = postfix
        group = postfix
    }}
}}

# Quota plugin
mail_plugins = $mail_plugins quota

protocol imap {{
    mail_plugins = $mail_plugins imap_quota
}}

plugin {{
    quota = maildir:User quota
    quota_rule = *:storage=1G
    quota_grace = 10%%
    quota_status_success = DUNNO
    quota_status_nouser = DUNNO
    quota_status_overquota = "552 5.2.2 Mailbox is full"
}}
'''

    with open(os.path.join(_DOVECOT_DIR, 'dovecot.conf'), 'w') as f:
        f.write(dovecot_conf)

    # SQL auth config
    sql_conf = f'''driver = sqlite
connect = {db_path}
default_pass_scheme = SHA512-CRYPT

password_query = SELECT email AS user, password_hash AS password \\
    FROM accounts WHERE email = '%u' AND enabled = 1
user_query = SELECT '{mail_data}/%d/%n' AS home, \\
    {uid} AS uid, {gid} AS gid \\
    FROM accounts WHERE email = '%u' AND enabled = 1
'''

    sql_conf_path = os.path.join(_DOVECOT_DIR, 'dovecot-sql.conf')
    with open(sql_conf_path, 'w') as f:
        f.write(sql_conf)
    os.chmod(sql_conf_path, 0o600)

    # Master user for webmail backend access (no plaintext password storage needed)
    master_pw = cfg.get('dovecot_master_pw')
    if not master_pw:
        master_pw = secrets.token_hex(32)
        cfg['dovecot_master_pw'] = master_pw
        _save_config(cfg)

    master_hash = ''
    try:
        r = host_run(f'doveadm pw -s SHA512-CRYPT -p {q(master_pw)}', timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            master_hash = r.stdout.strip()
    except Exception:
        pass
    if not master_hash:
        import crypt as _crypt
        master_hash = _crypt.crypt(master_pw, _crypt.mksalt(_crypt.METHOD_SHA512))

    master_users_path = os.path.join(_DOVECOT_DIR, 'master-users')
    with open(master_users_path, 'w') as f:
        f.write(f'ethos-webmail:{master_hash}\n')
    os.chmod(master_users_path, 0o640)
    host_run(f'chown root:dovecot {q(master_users_path)}', timeout=5)


# ─── OpenDKIM ──────────────────────────────────────────────────

def _generate_dkim_key(domain):
    """Generate DKIM key pair for a domain. Returns public key TXT value."""
    keys_dir = _dkim_keys_dir()
    domain_dir = os.path.join(keys_dir, domain)
    os.makedirs(domain_dir, exist_ok=True)

    selector = 'ethos'
    privkey = os.path.join(domain_dir, f'{selector}.private')
    pubkey_txt = os.path.join(domain_dir, f'{selector}.txt')

    if not os.path.isfile(privkey):
        host_run(
            f'opendkim-genkey -b 2048 -d {q(domain)} -D {q(domain_dir)} '
            f'-s {selector} -v',
            timeout=30)
        # Fix ownership
        host_run(f'chown -R opendkim:opendkim {q(keys_dir)}', timeout=5)

    # Read public key TXT record
    if os.path.isfile(pubkey_txt):
        with open(pubkey_txt) as f:
            raw = f.read()
        # opendkim-genkey splits the value across multiple quoted strings;
        # extract all quoted fragments and join them into one clean value
        paren = re.search(r'\((.*?)\)', raw, re.DOTALL)
        if paren:
            parts = re.findall(r'"([^"]*)"', paren.group(1))
            return ''.join(parts).strip()
        return raw.strip()
    return ''


def _configure_opendkim(cfg):
    """Write OpenDKIM config for all registered domains."""
    if not shutil.which('opendkim'):
        return

    keys_dir = _dkim_keys_dir()
    selector = 'ethos'

    # Get domains from DB
    db = _get_db()
    domains = [r['domain'] for r in db.execute('SELECT domain FROM domains').fetchall()]
    db.close()

    if not domains:
        return

    # KeyTable
    key_table_lines = []
    signing_table_lines = []
    for d in domains:
        privkey = os.path.join(keys_dir, d, f'{selector}.private')
        if os.path.isfile(privkey):
            key_table_lines.append(f'{selector}._domainkey.{d} {d}:{selector}:{privkey}')
            signing_table_lines.append(f'*@{d} {selector}._domainkey.{d}')

    os.makedirs(_OPENDKIM_DIR, exist_ok=True)

    with open(os.path.join(_OPENDKIM_DIR, 'KeyTable'), 'w') as f:
        f.write('\n'.join(key_table_lines) + '\n')

    with open(os.path.join(_OPENDKIM_DIR, 'SigningTable'), 'w') as f:
        f.write('\n'.join(signing_table_lines) + '\n')

    with open(os.path.join(_OPENDKIM_DIR, 'TrustedHosts'), 'w') as f:
        f.write('127.0.0.1\nlocalhost\n')
        for d in domains:
            f.write(f'*.{d}\n')

    # Main opendkim.conf
    opendkim_conf = f'''AutoRestart             Yes
AutoRestartRate         10/1h
Syslog                  yes
SyslogSuccess           yes
LogWhy                  yes

Canonicalization        relaxed/simple
Mode                    sv
SubDomains              no

KeyTable                refile:{_OPENDKIM_DIR}/KeyTable
SigningTable            refile:{_OPENDKIM_DIR}/SigningTable
ExternalIgnoreList      {_OPENDKIM_DIR}/TrustedHosts
InternalHosts           {_OPENDKIM_DIR}/TrustedHosts

Socket                  local:/var/spool/postfix/opendkim/opendkim.sock
PidFile                 /var/run/opendkim/opendkim.pid

OversignHeaders         From
TrustAnchorFile         /usr/share/dns/root.key

UserID                  opendkim:opendkim
'''

    with open(os.path.join(_OPENDKIM_DIR, 'opendkim.conf'), 'w') as f:
        f.write(opendkim_conf)

    # Ensure socket dir inside Postfix chroot so milter is reachable
    host_run('mkdir -p /var/spool/postfix/opendkim && '
             'chown opendkim:postfix /var/spool/postfix/opendkim && '
             'chmod 750 /var/spool/postfix/opendkim', timeout=5)
    # Add postfix user to opendkim group for socket access
    host_run('usermod -aG opendkim postfix', timeout=5)
    # Fix permissions
    host_run(f'chown -R opendkim:opendkim {q(keys_dir)}', timeout=5)


# ─── vmail user ────────────────────────────────────────────────

def _ensure_vmail_user():
    """Ensure the vmail system user exists for virtual mailboxes."""
    r = host_run('id vmail 2>/dev/null', timeout=5)
    if r.returncode != 0:
        host_run(
            'groupadd -g 5000 vmail 2>/dev/null; '
            'useradd -g vmail -u 5000 -d /var/mail -s /usr/sbin/nologin -r vmail 2>/dev/null',
            timeout=10)

def _get_vmail_uid():
    try:
        r = host_run('id -u vmail 2>/dev/null', timeout=5)
        return r.stdout.strip() if r.returncode == 0 else '5000'
    except Exception:
        return '5000'

def _get_vmail_gid():
    try:
        r = host_run('id -g vmail 2>/dev/null', timeout=5)
        return r.stdout.strip() if r.returncode == 0 else '5000'
    except Exception:
        return '5000'


# ─── DNS record helpers ───────────────────────────────────────

def _dns_records_for_domain(domain, cfg):
    """Return list of DNS records needed for a domain.

    Each record includes:
      - name:     FQDN (e.g. '_dmarc.example.com')
      - dns_name: relative name to enter in the DNS panel (e.g. '_dmarc', '@')
      - dns_hint: human-readable explanation of what to type in the Name field
    """
    hostname = cfg.get('hostname', f'mail.{domain}')
    records = []

    # A record for mail hostname
    records.append({
        'type': 'A',
        'name': hostname,
        'dns_name': hostname.replace(f'.{domain}', '') if hostname.endswith(f'.{domain}') else hostname,
        'value': cfg.get('ip', _get_public_ip()),
        'description': 'Adres IP serwera pocztowego.',
        'description_en': 'IP address of the mail server.',
    })

    # MX record
    records.append({
        'type': 'MX',
        'name': domain,
        'dns_name': '@',
        'value': f'10 {hostname}.',
        'description': 'Kieruje pocztę do Twojego serwera.',
        'description_en': 'Routes incoming email to your server.',
    })

    # SPF record
    records.append({
        'type': 'TXT',
        'name': domain,
        'dns_name': '@',
        'value': f'v=spf1 mx a:{hostname} ~all',
        'description': 'Informuje inne serwery, że Twój serwer może wysyłać maile z tej domeny.',
        'description_en': 'Tells other servers your server is authorized to send email for this domain.',
    })

    # DKIM record
    dkim_pub = _generate_dkim_key(domain)
    if dkim_pub:
        records.append({
            'type': 'TXT',
            'name': f'ethos._domainkey.{domain}',
            'dns_name': 'ethos._domainkey',
            'value': dkim_pub,
            'description': 'Podpis cyfrowy — potwierdza, że maile nie zostały sfałszowane.',
            'description_en': 'Digital signature — proves emails were not forged.',
        })

    # DMARC record
    records.append({
        'type': 'TXT',
        'name': f'_dmarc.{domain}',
        'dns_name': '_dmarc',
        'value': f'v=DMARC1; p=quarantine; rua=mailto:postmaster@{domain}; pct=100',
        'description': 'Polityka co robić z mailami które nie przejdą SPF/DKIM.',
        'description_en': 'Policy for handling emails that fail SPF/DKIM checks.',
    })

    return records


def _get_public_ip():
    """Best-effort public IP detection."""
    try:
        r = host_run('curl -4s --max-time 5 ifconfig.me 2>/dev/null', timeout=8)
        ip = r.stdout.strip()
        if r.returncode == 0 and ip:
            return ip
    except Exception:
        pass
    return '???'


# ─── Certbot renewal hook ─────────────────────────────────────

def _install_certbot_deploy_hook():
    """Install a certbot renewal hook that reloads Postfix + Dovecot."""
    hook_dir = '/etc/letsencrypt/renewal-hooks/deploy'
    if not os.path.isdir(hook_dir):
        os.makedirs(hook_dir, exist_ok=True)

    hook_path = os.path.join(hook_dir, 'reload-mail-services.sh')
    hook_content = '''#!/bin/bash
# Reload mail services after certificate renewal
systemctl reload postfix 2>/dev/null || true
systemctl reload dovecot 2>/dev/null || true
'''
    with open(hook_path, 'w') as f:
        f.write(hook_content)
    os.chmod(hook_path, 0o755)


# ═══════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════

@mail_bp.route('/pkg-status', methods=['GET'])
@admin_required
def pkg_status():
    return jsonify({'installed': _is_installed()})


@mail_bp.route('/status', methods=['GET'])
@admin_required
def status():
    installed = _is_installed()
    cfg = _load_config()
    result = {
        'installed': installed,
        'configured': cfg.get('configured', False),
        'hostname': cfg.get('hostname', ''),
        'postfix_running': _service_active('postfix'),
        'dovecot_running': _service_active('dovecot'),
        'opendkim_running': _service_active('opendkim'),
    }

    if installed:
        # Count domains/accounts
        try:
            db = _get_db()
            result['domain_count'] = db.execute('SELECT COUNT(*) FROM domains').fetchone()[0]
            result['account_count'] = db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
            result['alias_count'] = db.execute('SELECT COUNT(*) FROM aliases').fetchone()[0]
            db.close()
        except Exception:
            result['domain_count'] = 0
            result['account_count'] = 0
            result['alias_count'] = 0

        # Relay info
        relay = cfg.get('relay', {})
        result['relay_enabled'] = relay.get('enabled', False)
        result['relay_host'] = relay.get('host', '')

        # Data directory size
        try:
            r = host_run(f'du -sh {q(_mail_data_dir())} 2>/dev/null', timeout=10)
            result['data_size'] = r.stdout.split()[0] if r.returncode == 0 else '0'
        except Exception:
            result['data_size'] = '0'

        # Mail queue
        try:
            r = host_run('postqueue -j 2>/dev/null | wc -l', timeout=5)
            result['queue_count'] = int(r.stdout.strip()) if r.returncode == 0 else 0
        except Exception:
            result['queue_count'] = 0

    return jsonify(result)


@mail_bp.route('/install', methods=['POST'])
@admin_required
def install():
    if _is_installed():
        return jsonify(ok=True, message='Already installed')

    task_id = secrets.token_hex(8)
    sio = getattr(mail_bp, '_socketio', None)

    def _bg():
        def _emit(stage, pct, msg):
            if sio:
                sio.emit('mail_server_install', {
                    'task_id': task_id, 'stage': stage,
                    'percent': pct, 'message': msg,
                })

        try:
            _emit('start', 5, 'Instalacja Postfix i Dovecot...')

            # Pre-configure postfix to avoid interactive prompt
            host_run('debconf-set-selections <<< '
                     '"postfix postfix/mailname string localhost"',
                     timeout=10)
            host_run('debconf-set-selections <<< '
                     '"postfix postfix/main_mailer_type string Internet Site"',
                     timeout=10)

            env = 'DEBIAN_FRONTEND=noninteractive'
            r = host_run(
                f'{env} apt-get install -y '
                'postfix dovecot-core dovecot-imapd dovecot-pop3d '
                'dovecot-lmtpd dovecot-sqlite '
                'opendkim opendkim-tools',
                timeout=600)

            if r.returncode != 0:
                _emit('error', 0, 'Instalacja nie powiodła się: '
                      + (r.stderr or r.stdout or '')[:300])
                return

            _emit('progress', 50, 'Tworzenie użytkownika vmail...')
            _ensure_vmail_user()

            _emit('progress', 60, 'Inicjalizacja bazy danych...')
            _init_db()

            _emit('progress', 70, 'Ustawianie uprawnień...')
            mail_data = _mail_data_dir()
            host_run(f'chown -R vmail:vmail {q(mail_data)}', timeout=30)

            # Stop services until wizard configures them
            host_run('systemctl stop postfix dovecot opendkim 2>/dev/null', timeout=15)

            _emit('progress', 90, 'Instalacja hook certbot...')
            _install_certbot_deploy_hook()

            _emit('done', 100, 'Pakiety zainstalowane. Przejdź do konfiguracji.')
        except Exception as e:
            log.exception('Mail server install failed')
            _emit('error', 0, str(e))

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify(ok=True, task_id=task_id)


@mail_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall():
    try:
        host_run('systemctl stop postfix dovecot opendkim 2>/dev/null', timeout=15)
        host_run('systemctl disable postfix dovecot opendkim 2>/dev/null', timeout=10)
    except Exception:
        pass
    try:
        host_run('DEBIAN_FRONTEND=noninteractive apt-get remove -y '
                 'postfix dovecot-core dovecot-imapd dovecot-pop3d '
                 'dovecot-lmtpd dovecot-sqlite opendkim opendkim-tools',
                 timeout=120)
    except Exception:
        pass

    cfg = _load_config()
    cfg['configured'] = False
    _save_config(cfg)
    return jsonify(ok=True)


@mail_bp.route('/setup', methods=['POST'])
@admin_required
def setup_wizard():
    """Wizard endpoint — configure hostname, first domain, first account."""
    data = request.get_json(force=True)
    hostname = data.get('hostname', '').strip()
    domain = data.get('domain', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')

    if not hostname or not domain or not email or not password:
        return jsonify(error='Wszystkie pola są wymagane.'), 400

    if '@' not in email:
        email = f'{email}@{domain}'

    if not re.match(r'^[a-zA-Z0-9.-]+$', hostname):
        return jsonify(error='Nieprawidłowa nazwa hosta.'), 400
    if not re.match(r'^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', domain):
        return jsonify(error='Nieprawidłowa domena.'), 400

    try:
        # Save config
        cfg = _load_config()
        cfg['hostname'] = hostname
        cfg['configured'] = True
        _save_config(cfg)

        # Init DB
        _init_db()

        # Add domain
        db = _get_db()
        db.execute('INSERT OR IGNORE INTO domains (domain) VALUES (?)', (domain,))

        # Create account
        pw_hash = _hash_password(password)
        db.execute(
            'INSERT OR REPLACE INTO accounts (email, domain, password_hash, quota_mb, enabled) '
            'VALUES (?, ?, ?, 2048, 1)',
            (email, domain, pw_hash))

        # Default aliases
        db.execute(
            'INSERT OR IGNORE INTO aliases (source, destination, domain) VALUES (?, ?, ?)',
            (f'postmaster@{domain}', email, domain))
        db.execute(
            'INSERT OR IGNORE INTO aliases (source, destination, domain) VALUES (?, ?, ?)',
            (f'abuse@{domain}', email, domain))

        db.commit()
        db.close()

        # Generate configs
        _generate_postfix_config(cfg)
        _generate_dovecot_config(cfg)

        # DKIM
        _generate_dkim_key(domain)
        _configure_opendkim(cfg)

        # Create maildir
        mail_data = _mail_vhosts_dir()
        user_part = email.split('@')[0]
        maildir = os.path.join(mail_data, domain, user_part, 'Maildir')
        os.makedirs(maildir, exist_ok=True)
        host_run(f'chown -R vmail:vmail {q(mail_data)}', timeout=30)

        # Start services
        host_run('systemctl enable --now postfix', timeout=30)
        host_run('systemctl enable --now dovecot', timeout=30)
        host_run('systemctl enable --now opendkim', timeout=30)

        # Reload
        time.sleep(1)
        host_run('systemctl reload postfix 2>/dev/null', timeout=10)

        return jsonify(ok=True, email=email)

    except Exception as e:
        log.exception('Mail setup wizard failed')
        return jsonify(error=str(e)), 500


@mail_bp.route('/config', methods=['GET'])
@admin_required
def get_config():
    cfg = _load_config()
    # Mask relay password
    relay = dict(cfg.get('relay', {}))
    if relay.get('password'):
        relay['password'] = '***'
    safe = dict(cfg)
    safe['relay'] = relay
    return jsonify(safe)


@mail_bp.route('/config', methods=['PUT'])
@admin_required
def update_config():
    data = request.get_json(force=True)
    cfg = _load_config()

    if 'hostname' in data:
        cfg['hostname'] = data['hostname'].strip()

    _save_config(cfg)

    # Regenerate configs
    _generate_postfix_config(cfg)
    _generate_dovecot_config(cfg)

    host_run('systemctl reload postfix 2>/dev/null', timeout=10)
    host_run('systemctl reload dovecot 2>/dev/null', timeout=10)
    return jsonify(ok=True)


# ─── Domains ──────────────────────────────────────────────────

@mail_bp.route('/domains', methods=['GET'])
@admin_required
def list_domains():
    db = _get_db()
    rows = db.execute(
        'SELECT d.*, '
        '(SELECT COUNT(*) FROM accounts WHERE domain=d.domain) AS account_count '
        'FROM domains d ORDER BY d.domain').fetchall()
    db.close()
    return jsonify(items=[dict(r) for r in rows])


@mail_bp.route('/domains', methods=['POST'])
@admin_required
def add_domain():
    data = request.get_json(force=True)
    domain = data.get('domain', '').strip().lower()
    if not domain or not re.match(r'^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', domain):
        return jsonify(error='Nieprawidłowa domena.'), 400

    db = _get_db()
    try:
        db.execute('INSERT INTO domains (domain) VALUES (?)', (domain,))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        return jsonify(error='Domena już istnieje.'), 409
    db.close()

    cfg = _load_config()
    _generate_dkim_key(domain)
    _configure_opendkim(cfg)
    _generate_postfix_config(cfg)

    host_run('systemctl reload postfix 2>/dev/null', timeout=10)
    host_run('systemctl restart opendkim 2>/dev/null', timeout=10)

    return jsonify(ok=True)


@mail_bp.route('/domains/<domain>', methods=['DELETE'])
@admin_required
def delete_domain(domain):
    db = _get_db()
    r = db.execute('DELETE FROM domains WHERE domain = ?', (domain,))
    db.commit()
    db.close()

    if r.rowcount == 0:
        return jsonify(error='Domena nie znaleziona.'), 404

    cfg = _load_config()
    _configure_opendkim(cfg)
    _generate_postfix_config(cfg)
    host_run('systemctl reload postfix 2>/dev/null', timeout=10)
    host_run('systemctl restart opendkim 2>/dev/null', timeout=10)

    return jsonify(ok=True)


@mail_bp.route('/domains/<domain>/dns', methods=['GET'])
@admin_required
def domain_dns(domain):
    cfg = _load_config()
    records = _dns_records_for_domain(domain, cfg)
    return jsonify(items=records)


# ─── Accounts ─────────────────────────────────────────────────

@mail_bp.route('/accounts', methods=['GET'])
@admin_required
def list_accounts():
    db = _get_db()
    rows = db.execute(
        'SELECT id, email, domain, quota_mb, enabled, created_at '
        'FROM accounts ORDER BY email').fetchall()
    db.close()

    items = []
    for r in rows:
        item = dict(r)
        # Calculate maildir size
        user_part = r['email'].split('@')[0]
        maildir = os.path.join(_mail_vhosts_dir(), r['domain'], user_part)
        try:
            out = host_run(f'du -sb {q(maildir)} 2>/dev/null', timeout=5)
            item['used_bytes'] = int(out.stdout.split()[0]) if out.returncode == 0 else 0
        except Exception:
            item['used_bytes'] = 0
        items.append(item)

    return jsonify(items=items)


@mail_bp.route('/accounts', methods=['POST'])
@admin_required
def create_account():
    data = request.get_json(force=True)
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    quota_mb = data.get('quota_mb', 1024)

    if not email or '@' not in email:
        return jsonify(error='Nieprawidłowy adres email.'), 400
    if not password or len(password) < 6:
        return jsonify(error='Hasło musi mieć co najmniej 6 znaków.'), 400

    domain = email.split('@')[1]

    db = _get_db()
    # Verify domain exists
    if not db.execute('SELECT 1 FROM domains WHERE domain = ?', (domain,)).fetchone():
        db.close()
        return jsonify(error=f'Domena {domain} nie jest zarejestrowana.'), 400

    pw_hash = _hash_password(password)
    try:
        db.execute(
            'INSERT INTO accounts (email, domain, password_hash, quota_mb) VALUES (?, ?, ?, ?)',
            (email, domain, pw_hash, quota_mb))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        return jsonify(error='Konto już istnieje.'), 409
    db.close()

    # Create maildir
    user_part = email.split('@')[0]
    maildir = os.path.join(_mail_vhosts_dir(), domain, user_part, 'Maildir')
    os.makedirs(maildir, exist_ok=True)
    host_run(f'chown -R vmail:vmail {q(os.path.join(_mail_vhosts_dir(), domain))}', timeout=10)

    return jsonify(ok=True)


@mail_bp.route('/accounts/<path:email>', methods=['PUT'])
@admin_required
def update_account(email):
    data = request.get_json(force=True)
    db = _get_db()

    row = db.execute('SELECT * FROM accounts WHERE email = ?', (email,)).fetchone()
    if not row:
        db.close()
        return jsonify(error='Konto nie znalezione.'), 404

    updates = []
    params = []

    if 'password' in data and data['password']:
        if len(data['password']) < 6:
            db.close()
            return jsonify(error='Hasło musi mieć co najmniej 6 znaków.'), 400
        updates.append('password_hash = ?')
        params.append(_hash_password(data['password']))

    if 'quota_mb' in data:
        updates.append('quota_mb = ?')
        params.append(int(data['quota_mb']))

    if 'enabled' in data:
        updates.append('enabled = ?')
        params.append(1 if data['enabled'] else 0)

    if updates:
        params.append(email)
        db.execute(f'UPDATE accounts SET {", ".join(updates)} WHERE email = ?', params)
        db.commit()

    db.close()
    return jsonify(ok=True)


@mail_bp.route('/accounts/<path:email>', methods=['DELETE'])
@admin_required
def delete_account(email):
    db = _get_db()
    r = db.execute('DELETE FROM accounts WHERE email = ?', (email,))
    db.commit()
    db.close()

    if r.rowcount == 0:
        return jsonify(error='Konto nie znalezione.'), 404

    return jsonify(ok=True)


# ─── Aliases ──────────────────────────────────────────────────

@mail_bp.route('/aliases', methods=['GET'])
@admin_required
def list_aliases():
    db = _get_db()
    rows = db.execute('SELECT * FROM aliases ORDER BY source').fetchall()
    db.close()
    return jsonify(items=[dict(r) for r in rows])


@mail_bp.route('/aliases', methods=['POST'])
@admin_required
def create_alias():
    data = request.get_json(force=True)
    source = data.get('source', '').strip().lower()
    destination = data.get('destination', '').strip().lower()

    if not source or not destination or '@' not in source:
        return jsonify(error='Źródło i cel są wymagane.'), 400

    domain = source.split('@')[1]
    db = _get_db()
    if not db.execute('SELECT 1 FROM domains WHERE domain = ?', (domain,)).fetchone():
        db.close()
        return jsonify(error=f'Domena {domain} nie jest zarejestrowana.'), 400

    db.execute(
        'INSERT INTO aliases (source, destination, domain) VALUES (?, ?, ?)',
        (source, destination, domain))
    db.commit()
    db.close()

    _generate_postfix_config(_load_config())
    host_run('systemctl reload postfix 2>/dev/null', timeout=10)
    return jsonify(ok=True)


@mail_bp.route('/aliases/<int:alias_id>', methods=['DELETE'])
@admin_required
def delete_alias(alias_id):
    db = _get_db()
    r = db.execute('DELETE FROM aliases WHERE id = ?', (alias_id,))
    db.commit()
    db.close()

    if r.rowcount == 0:
        return jsonify(error='Alias nie znaleziony.'), 404

    _generate_postfix_config(_load_config())
    host_run('systemctl reload postfix 2>/dev/null', timeout=10)
    return jsonify(ok=True)


# ─── Relay ────────────────────────────────────────────────────

@mail_bp.route('/relay', methods=['GET'])
@admin_required
def get_relay():
    cfg = _load_config()
    relay = dict(cfg.get('relay', {}))
    if relay.get('password'):
        relay['password'] = '***'
    return jsonify(relay)


@mail_bp.route('/relay', methods=['PUT'])
@admin_required
def update_relay():
    data = request.get_json(force=True)
    cfg = _load_config()

    relay = cfg.get('relay', {})
    relay['enabled'] = bool(data.get('enabled', False))
    relay['host'] = data.get('host', '').strip()
    relay['port'] = int(data.get('port', 587))
    relay['username'] = data.get('username', '').strip()

    # Only update password if not masked
    if data.get('password') and data['password'] != '***':
        relay['password'] = data['password']

    cfg['relay'] = relay
    _save_config(cfg)

    _generate_postfix_config(cfg)
    host_run('systemctl reload postfix 2>/dev/null', timeout=10)
    return jsonify(ok=True)


# ─── Service management ──────────────────────────────────────

@mail_bp.route('/service/<action>', methods=['POST'])
@admin_required
def service_action(action):
    if action not in ('start', 'stop', 'restart'):
        return jsonify(error='Nieprawidłowa akcja.'), 400

    services = ['postfix', 'dovecot', 'opendkim']
    errors = []
    for svc in services:
        r = host_run(f'systemctl {q(action)} {q(svc)} 2>&1', timeout=30)
        if r.returncode != 0:
            errors.append(f'{svc}: {r.stderr or r.stdout or "failed"}')

    if errors:
        return jsonify(ok=False, errors=errors), 500
    return jsonify(ok=True)


# ─── Test email ───────────────────────────────────────────────

@mail_bp.route('/test-send', methods=['POST'])
@admin_required
def test_send():
    data = request.get_json(force=True)
    from_email = data.get('from', '')
    to_email = data.get('to', '')
    subject = data.get('subject', 'EthOS Mail Server Test')

    if not from_email or not to_email:
        return jsonify(error='Nadawca i odbiorca są wymagani.'), 400

    msg = (
        f'From: {from_email}\n'
        f'To: {to_email}\n'
        f'Subject: {subject}\n'
        f'Date: {time.strftime("%a, %d %b %Y %H:%M:%S %z")}\n'
        f'Message-ID: <{secrets.token_hex(16)}@{from_email.split("@")[1]}>\n'
        f'Content-Type: text/plain; charset=UTF-8\n'
        f'\n'
        f'This is a test email from EthOS Mail Server.\n'
        f'If you received this message, your mail server is working correctly!\n'
        f'\n'
        f'Sent at: {time.strftime("%Y-%m-%d %H:%M:%S")}\n'
    )

    try:
        r = host_run(
            f'echo {q(msg)} | /usr/sbin/sendmail -t -f {q(from_email)}',
            timeout=15)
        if r.returncode == 0:
            return jsonify(ok=True, message='Wiadomość testowa wysłana.')
        else:
            return jsonify(error=f'Błąd wysyłki: {r.stderr or r.stdout}'), 500
    except Exception as e:
        return jsonify(error=str(e)), 500


# ─── Logs ─────────────────────────────────────────────────────

@mail_bp.route('/logs', methods=['GET'])
@admin_required
def get_logs():
    lines = int(request.args.get('lines', 100))
    lines = min(lines, 500)

    log_file = '/var/log/mail.log'
    if not os.path.isfile(log_file):
        log_file = '/var/log/syslog'

    try:
        r = host_run(f'tail -n {lines} {q(log_file)} 2>/dev/null '
                     f'| grep -iE "postfix|dovecot|opendkim" || true',
                     timeout=10)
        entries = r.stdout.strip().split('\n') if r.stdout.strip() else []
    except Exception:
        entries = []

    return jsonify(items=entries)


# ─── Queue ────────────────────────────────────────────────────

@mail_bp.route('/queue', methods=['GET'])
@admin_required
def get_queue():
    try:
        r = host_run('postqueue -j 2>/dev/null', timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            items = []
            for line in r.stdout.strip().split('\n'):
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            return jsonify(items=items)
    except Exception:
        pass
    return jsonify(items=[])


@mail_bp.route('/queue/flush', methods=['POST'])
@admin_required
def flush_queue():
    host_run('postqueue -f 2>/dev/null', timeout=10)
    return jsonify(ok=True)


# ─── Init ─────────────────────────────────────────────────────

def init_mail_server(socketio=None):
    """Called at app startup if mail-server is installed."""
    if not _is_installed():
        return
    try:
        _init_db()
        log.info('[mail-server] Initialized mail server DB')
    except Exception as e:
        log.error('[mail-server] Init failed: %s', e)


# ─── Webmail IMAP proxy ──────────────────────────────────────────────────
#
# Endpoints:
#   GET  /api/mail-server/webmail/folders       — list IMAP folders
#   GET  /api/mail-server/webmail/messages      — paginated message list
#   GET  /api/mail-server/webmail/message/<uid>  — full message with body
#   GET  /api/mail-server/webmail/attachment/<uid>/<part> — download attachment
#   POST /api/mail-server/webmail/send           — compose & send email
#   POST /api/mail-server/webmail/action         — bulk actions (read/delete/move/star)

import imaplib
import email
from email import policy
from email.header import decode_header as _decode_header
from email.mime.text import MIMEText as _MIMEText
from email.mime.multipart import MIMEMultipart as _MIMEMultipart
from email.mime.base import MIMEBase as _MIMEBase
from email import encoders as _encoders
from flask import Response


def _imap_connect(account):
    """Open IMAP connection to local Dovecot as master user."""
    cfg = _load_config()
    master_pw = cfg.get('dovecot_master_pw', '')
    if not master_pw:
        raise ValueError('Dovecot master user not configured. Re-save mail server config.')
    imap = imaplib.IMAP4('127.0.0.1', 143)
    imap.login(f'{account}*ethos-webmail', master_pw)
    return imap


def _decode_hdr(raw):
    """Decode RFC2047 encoded header into plain string."""
    if not raw:
        return ''
    parts = []
    for data, charset in _decode_header(raw):
        if isinstance(data, bytes):
            parts.append(data.decode(charset or 'utf-8', errors='replace'))
        else:
            parts.append(data)
    return ''.join(parts)


def _parse_addr(raw):
    """Parse email address header into {name, addr}."""
    if not raw:
        return {'name': '', 'addr': ''}
    decoded = _decode_hdr(raw)
    # "John Doe <john@example.com>" or just "john@example.com"
    m = re.match(r'^(.*?)\s*<(.+?)>\s*$', decoded)
    if m:
        return {'name': m.group(1).strip(' "\''), 'addr': m.group(2)}
    return {'name': '', 'addr': decoded.strip()}


def _parse_addr_list(raw):
    """Parse comma-separated address list."""
    if not raw:
        return []
    decoded = _decode_hdr(raw)
    # Split on commas not inside angle brackets
    addrs = re.split(r',\s*(?=[^<]*(?:<|$))', decoded)
    return [_parse_addr(a.strip()) for a in addrs if a.strip()]


def _sanitize_html(html_body):
    """Basic HTML sanitization for safe display in sandboxed iframe."""
    if not html_body:
        return ''
    # Remove script tags and event handlers
    html_body = re.sub(r'<script[^>]*>.*?</script>', '', html_body, flags=re.DOTALL | re.IGNORECASE)
    html_body = re.sub(r'\bon\w+\s*=\s*["\'][^"\']*["\']', '', html_body, flags=re.IGNORECASE)
    html_body = re.sub(r'\bon\w+\s*=\s*\S+', '', html_body, flags=re.IGNORECASE)
    # Remove iframe, object, embed
    for tag in ('iframe', 'object', 'embed', 'applet', 'form'):
        html_body = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', html_body, flags=re.DOTALL | re.IGNORECASE)
        html_body = re.sub(rf'<{tag}[^>]*/?\s*>', '', html_body, flags=re.IGNORECASE)
    return html_body


@mail_bp.route('/webmail/folders', methods=['GET'])
@admin_required
def webmail_folders():
    account = request.args.get('account', '')
    if not account:
        return jsonify(error='account parameter required'), 400

    try:
        imap = _imap_connect(account)
    except Exception as e:
        return jsonify(error=f'IMAP connection failed: {e}'), 500

    try:
        status, folder_data = imap.list()
        folders = []
        for item in (folder_data or []):
            if isinstance(item, bytes):
                # Parse: (\\Flags) "delimiter" "name"
                m = re.match(rb'\(([^)]*)\)\s+"([^"]+)"\s+"?([^"]+)"?', item)
                if m:
                    flags = m.group(1).decode('utf-8', errors='replace')
                    name = m.group(3).decode('utf-8', errors='replace').strip('"')
                else:
                    name = item.decode('utf-8', errors='replace').split('"')[-1].strip(' "')
                    flags = ''

                # Get message count + unseen
                try:
                    sel_status, sel_data = imap.select(f'"{name}"', readonly=True)
                    total = int(sel_data[0]) if sel_status == 'OK' else 0
                    unseen = 0
                    if total > 0:
                        s2, s2d = imap.search(None, 'UNSEEN')
                        unseen = len(s2d[0].split()) if s2d[0] else 0
                except Exception:
                    total = 0
                    unseen = 0

                folders.append({
                    'name': name,
                    'flags': flags,
                    'total': total,
                    'unseen': unseen,
                })
        imap.logout()
        return jsonify(items=folders)
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return jsonify(error=str(e)), 500


@mail_bp.route('/webmail/messages', methods=['GET'])
@admin_required
def webmail_messages():
    account = request.args.get('account', '')
    folder = request.args.get('folder', 'INBOX')
    page = max(1, int(request.args.get('page', 1)))
    per_page = min(100, max(10, int(request.args.get('per_page', 50))))

    if not account:
        return jsonify(error='account parameter required'), 400

    try:
        imap = _imap_connect(account)
    except Exception as e:
        return jsonify(error=f'IMAP connection failed: {e}'), 500

    try:
        status, data = imap.select(f'"{folder}"', readonly=True)
        if status != 'OK':
            imap.logout()
            return jsonify(error=f'Cannot select folder: {folder}'), 400

        total = int(data[0])
        if total == 0:
            imap.logout()
            return jsonify(items=[], total=0, page=page, pages=0)

        # Get UIDs sorted by date (newest first)
        try:
            status, uid_data = imap.uid('sort', '(REVERSE DATE)', 'UTF-8', 'ALL')
            all_uids = uid_data[0].split() if uid_data[0] else []
        except Exception:
            # Fallback: SEARCH ALL and reverse
            status, uid_data = imap.uid('search', None, 'ALL')
            all_uids = list(reversed(uid_data[0].split())) if uid_data[0] else []

        total_msgs = len(all_uids)
        pages = max(1, (total_msgs + per_page - 1) // per_page)
        start = (page - 1) * per_page
        page_uids = all_uids[start:start + per_page]

        if not page_uids:
            imap.logout()
            return jsonify(items=[], total=total_msgs, page=page, pages=pages)

        # Fetch headers for page UIDs
        uid_set = b','.join(page_uids)
        status, fetch_data = imap.uid('fetch', uid_set,
            '(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])')

        messages = []
        i = 0
        while i < len(fetch_data):
            item = fetch_data[i]
            if isinstance(item, tuple) and len(item) == 2:
                meta_line = item[0].decode('utf-8', errors='replace')
                header_bytes = item[1]

                # Parse UID
                uid_match = re.search(r'UID\s+(\d+)', meta_line)
                uid = uid_match.group(1) if uid_match else '0'

                # Parse flags
                flags_match = re.search(r'FLAGS\s+\(([^)]*)\)', meta_line)
                flags = flags_match.group(1) if flags_match else ''

                # Parse size
                size_match = re.search(r'RFC822\.SIZE\s+(\d+)', meta_line)
                size = int(size_match.group(1)) if size_match else 0

                # Parse headers
                msg_obj = email.message_from_bytes(header_bytes, policy=policy.default)
                messages.append({
                    'uid': uid,
                    'from': _parse_addr(msg_obj['From']),
                    'to': _parse_addr_list(msg_obj['To']),
                    'subject': _decode_hdr(msg_obj['Subject']) or '(no subject)',
                    'date': msg_obj['Date'] or '',
                    'flags': flags,
                    'unread': '\\Seen' not in flags,
                    'flagged': '\\Flagged' in flags,
                    'size': size,
                })
            i += 1

        imap.logout()
        return jsonify(items=messages, total=total_msgs, page=page, pages=pages)
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return jsonify(error=str(e)), 500


@mail_bp.route('/webmail/message/<uid>', methods=['GET'])
@admin_required
def webmail_message(uid):
    account = request.args.get('account', '')
    folder = request.args.get('folder', 'INBOX')

    if not account:
        return jsonify(error='account parameter required'), 400

    try:
        imap = _imap_connect(account)
    except Exception as e:
        return jsonify(error=f'IMAP connection failed: {e}'), 500

    try:
        imap.select(f'"{folder}"')

        # Mark as read
        imap.uid('store', uid.encode(), '+FLAGS', '(\\Seen)')

        # Fetch full message
        status, data = imap.uid('fetch', uid.encode(), '(RFC822)')
        if status != 'OK' or not data or not data[0]:
            imap.logout()
            return jsonify(error='Message not found'), 404

        raw = data[0][1] if isinstance(data[0], tuple) else data[0]
        msg = email.message_from_bytes(raw, policy=policy.default)

        # Extract body
        text_body = ''
        html_body = ''
        attachments = []
        part_idx = 0

        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get('Content-Disposition', ''))

            if content_type == 'multipart':
                continue

            if 'attachment' in disposition or part.get_filename():
                fname = part.get_filename() or f'attachment_{part_idx}'
                fname = _decode_hdr(fname)
                attachments.append({
                    'part': part_idx,
                    'filename': fname,
                    'content_type': content_type,
                    'size': len(part.get_payload(decode=True) or b''),
                })
            elif content_type == 'text/html' and not html_body:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                html_body = payload.decode(charset, errors='replace') if payload else ''
            elif content_type == 'text/plain' and not text_body:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                text_body = payload.decode(charset, errors='replace') if payload else ''

            part_idx += 1

        imap.logout()

        return jsonify(
            uid=uid,
            folder=folder,
            subject=_decode_hdr(msg['Subject']) or '(no subject)',
            from_addr=_parse_addr(msg['From']),
            to=_parse_addr_list(msg['To']),
            cc=_parse_addr_list(msg['Cc']),
            date=msg['Date'] or '',
            message_id=msg['Message-ID'] or '',
            in_reply_to=msg['In-Reply-To'] or '',
            text_body=text_body,
            html_body=_sanitize_html(html_body),
            attachments=attachments,
        )
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return jsonify(error=str(e)), 500


@mail_bp.route('/webmail/attachment/<uid>/<int:part>', methods=['GET'])
@admin_required
def webmail_attachment(uid, part):
    account = request.args.get('account', '')
    folder = request.args.get('folder', 'INBOX')

    if not account:
        return jsonify(error='account parameter required'), 400

    try:
        imap = _imap_connect(account)
        imap.select(f'"{folder}"', readonly=True)

        status, data = imap.uid('fetch', uid.encode(), '(RFC822)')
        if status != 'OK' or not data or not data[0]:
            imap.logout()
            return jsonify(error='Message not found'), 404

        raw = data[0][1] if isinstance(data[0], tuple) else data[0]
        msg = email.message_from_bytes(raw, policy=policy.default)

        part_idx = 0
        for msg_part in msg.walk():
            if msg_part.get_content_type() == 'multipart':
                continue
            if part_idx == part:
                payload = msg_part.get_payload(decode=True) or b''
                fname = _decode_hdr(msg_part.get_filename() or f'attachment_{part}')
                ctype = msg_part.get_content_type() or 'application/octet-stream'
                imap.logout()
                return Response(
                    payload,
                    mimetype=ctype,
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'},
                )
            part_idx += 1

        imap.logout()
        return jsonify(error='Attachment not found'), 404
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return jsonify(error=str(e)), 500


@mail_bp.route('/webmail/send', methods=['POST'])
@admin_required
def webmail_send():
    data = request.get_json(silent=True) or {}
    from_email = data.get('from', '').strip()
    to_addrs = data.get('to', '').strip()
    cc_addrs = data.get('cc', '').strip()
    bcc_addrs = data.get('bcc', '').strip()
    subject = data.get('subject', '').strip()
    body = data.get('body', '')
    is_html = data.get('html', False)

    if not from_email or not to_addrs:
        return jsonify(error='Nadawca i odbiorca są wymagani.'), 400

    # Verify sender is a valid account
    db = _get_db()
    if not db.execute('SELECT 1 FROM accounts WHERE email = ? AND enabled = 1',
                      (from_email,)).fetchone():
        db.close()
        return jsonify(error='Nadawca nie jest prawidłowym kontem.'), 400
    db.close()

    # Build message
    msg = _MIMEMultipart('mixed')
    msg['From'] = from_email
    msg['To'] = to_addrs
    if cc_addrs:
        msg['Cc'] = cc_addrs
    msg['Subject'] = subject
    msg['Date'] = email.utils.formatdate(localtime=True)
    msg['Message-ID'] = email.utils.make_msgid(domain=from_email.split('@')[1])
    in_reply_to = data.get('in_reply_to', '').strip()
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to

    # Body
    if is_html:
        msg.attach(_MIMEText(body, 'html', 'utf-8'))
    else:
        msg.attach(_MIMEText(body, 'plain', 'utf-8'))

    # All recipients
    all_rcpts = []
    for field in [to_addrs, cc_addrs, bcc_addrs]:
        if field:
            all_rcpts.extend(a.strip() for a in field.split(',') if a.strip())

    # Send via local SMTP (localhost:25, no auth needed for local delivery)
    try:
        import smtplib as _smtplib
        with _smtplib.SMTP('127.0.0.1', 25, timeout=15) as smtp:
            smtp.sendmail(from_email, all_rcpts, msg.as_string())
        return jsonify(ok=True, message='Wiadomość wysłana.')
    except Exception as e:
        return jsonify(error=f'Błąd wysyłki: {e}'), 500


@mail_bp.route('/webmail/action', methods=['POST'])
@admin_required
def webmail_action():
    data = request.get_json(silent=True) or {}
    account = data.get('account', '')
    folder = data.get('folder', 'INBOX')
    uids = data.get('uids', [])
    action = data.get('action', '')

    if not account or not uids or not action:
        return jsonify(error='account, uids, and action are required'), 400

    try:
        imap = _imap_connect(account)
        imap.select(f'"{folder}"')

        uid_set = ','.join(str(u) for u in uids).encode()

        if action in ('mark_read', 'read'):
            imap.uid('store', uid_set, '+FLAGS', '(\\Seen)')
        elif action in ('mark_unread', 'unread'):
            imap.uid('store', uid_set, '-FLAGS', '(\\Seen)')
        elif action == 'flag':
            imap.uid('store', uid_set, '+FLAGS', '(\\Flagged)')
        elif action == 'unflag':
            imap.uid('store', uid_set, '-FLAGS', '(\\Flagged)')
        elif action == 'delete':
            # Move to Trash if exists, otherwise mark deleted + expunge
            try:
                imap.uid('copy', uid_set, 'Trash')
                imap.uid('store', uid_set, '+FLAGS', '(\\Deleted)')
                imap.expunge()
            except Exception:
                imap.uid('store', uid_set, '+FLAGS', '(\\Deleted)')
                imap.expunge()
        elif action == 'move':
            target = data.get('target_folder', '')
            if not target:
                imap.logout()
                return jsonify(error='target_folder required for move'), 400
            imap.uid('copy', uid_set, target.encode())
            imap.uid('store', uid_set, '+FLAGS', '(\\Deleted)')
            imap.expunge()
        else:
            imap.logout()
            return jsonify(error=f'Unknown action: {action}'), 400

        imap.logout()
        return jsonify(ok=True)
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return jsonify(error=str(e)), 500


# ─── SnappyMail management ──────────────────────────────────────────────
#
# Endpoints:
#   GET  /api/mail-server/webmail/status       — install & config status
#   POST /api/mail-server/webmail/install       — install PHP-FPM + SnappyMail
#   POST /api/mail-server/webmail/setup-domain  — nginx vhost + auto-configure

_SNAPPYMAIL_VERSION = '2.38.2'

def _snappymail_dir():
    """SnappyMail install root — on data partition."""
    dd = get_data_disk()
    if dd:
        p = os.path.join(dd, 'snappymail')
    else:
        p = data_path('snappymail')
    return p

def _snappymail_installed():
    """Check if SnappyMail files are present."""
    sm_dir = _snappymail_dir()
    return os.path.isfile(os.path.join(sm_dir, 'index.php'))

def _php_fpm_installed():
    """Check if PHP-FPM is available."""
    return host_run('which php-fpm8.2 2>/dev/null || which php-fpm 2>/dev/null',
                     timeout=5).returncode == 0

def _php_fpm_sock():
    """Return the PHP-FPM unix socket path."""
    for sock in ['/run/php/php8.2-fpm.sock', '/run/php/php-fpm.sock',
                 '/var/run/php/php8.2-fpm.sock']:
        if os.path.exists(sock):
            return sock
    return '/run/php/php8.2-fpm.sock'

def _snappymail_nginx_conf_name(domain):
    """Nginx config file name for a webmail vhost."""
    return f'snappymail-{domain}'


@mail_bp.route('/webmail/status', methods=['GET'])
@admin_required
def webmail_status():
    """Return SnappyMail install status and configured domains."""
    cfg = _load_config()
    sm_installed = _snappymail_installed()
    php_installed = _php_fpm_installed()

    webmail_domains = cfg.get('webmail_domains', [])

    domains_status = []
    for wd in webmail_domains:
        domain = wd.get('domain', '')
        vhost_name = _snappymail_nginx_conf_name(domain)
        vhost_path = f'/etc/nginx/sites-enabled/{vhost_name}'
        has_ssl = wd.get('ssl', False)
        proto = 'https' if has_ssl else 'http'
        domains_status.append({
            'domain': domain,
            'mail_domain': wd.get('mail_domain', domain.replace('mail.', '', 1)),
            'url': f'{proto}://{domain}',
            'ssl': has_ssl,
            'active': os.path.exists(vhost_path),
        })

    # Read actual admin password from SnappyMail's file (authoritative source)
    admin_pw = None
    if sm_installed:
        pw_file = os.path.join(_snappymail_dir(), 'data', '_data_', '_default_',
                               'admin_password.txt')
        try:
            with open(pw_file) as f:
                admin_pw = f.read().strip()
        except Exception:
            admin_pw = cfg.get('snappymail_admin_pw', '')

    return jsonify(ok=True, data={
        'installed': sm_installed and php_installed,
        'php_installed': php_installed,
        'snappymail_installed': sm_installed,
        'version': _SNAPPYMAIL_VERSION if sm_installed else None,
        'admin_password': admin_pw,
        'domains': domains_status,
    })


@mail_bp.route('/webmail/install', methods=['POST'])
@admin_required
def webmail_install():
    """Install PHP-FPM + SnappyMail. Async with SocketIO progress."""
    if _snappymail_installed() and _php_fpm_installed():
        return jsonify(ok=True, message='Already installed')

    sio = getattr(mail_bp, '_socketio', None)

    def _emit(stage, pct, msg):
        if sio:
            sio.emit('snappymail_install', {
                'stage': stage, 'percent': pct, 'message': msg,
            })

    def _bg():
        try:
            _emit('start', 5, 'Instalacja PHP-FPM...')
            env = 'DEBIAN_FRONTEND=noninteractive'
            r = host_run(
                f'{env} apt-get install -y '
                'php-fpm php-imap php-mbstring php-xml php-curl php-intl php-json 2>&1',
                timeout=300)

            if r.returncode != 0:
                _emit('error', 0, 'Instalacja PHP nie powiodła się: '
                      + (r.stderr or r.stdout or '')[:300])
                return

            # Enable and start PHP-FPM
            host_run('systemctl enable php8.2-fpm 2>/dev/null; '
                     'systemctl start php8.2-fpm 2>/dev/null', timeout=30)

            _emit('progress', 40, 'Pobieranie SnappyMail...')
            sm_dir = _snappymail_dir()
            os.makedirs(sm_dir, exist_ok=True)

            tarball = os.path.join(sm_dir, 'snappymail.tar.gz')
            url = (f'https://github.com/the-djmaze/snappymail/releases/download/'
                   f'v{_SNAPPYMAIL_VERSION}/snappymail-{_SNAPPYMAIL_VERSION}.tar.gz')

            r = host_run(f'curl -fSL -o {q(tarball)} {q(url)}', timeout=120)
            if r.returncode != 0:
                _emit('error', 0, 'Pobieranie nie powiodło się: '
                      + (r.stderr or r.stdout or '')[:200])
                return

            _emit('progress', 70, 'Rozpakowywanie SnappyMail...')
            r = host_run(f'tar xzf {q(tarball)} -C {q(sm_dir)}', timeout=60)
            if r.returncode != 0:
                _emit('error', 0, 'Rozpakowywanie nie powiodło się')
                return
            os.remove(tarball)

            # www-data needs write access to data/
            sm_data = os.path.join(sm_dir, 'data')
            os.makedirs(sm_data, exist_ok=True)
            host_run(f'chown -R www-data:www-data {q(sm_dir)}', timeout=30)

            _emit('progress', 85, 'Konfiguracja SnappyMail...')
            admin_pw = secrets.token_hex(16)
            cfg = _load_config()
            cfg['snappymail_admin_pw'] = admin_pw
            _save_config(cfg)

            _write_snappymail_admin_pw(sm_dir, admin_pw)

            # Re-chown everything after config writes (makedirs runs as root)
            host_run(f'chown -R www-data:www-data {q(sm_dir)}', timeout=30)

            _emit('done', 100, 'SnappyMail zainstalowany pomyślnie.')
        except Exception as e:
            log.exception('SnappyMail install failed')
            _emit('error', 0, str(e))

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify(ok=True)


def _write_snappymail_admin_pw(sm_dir, password):
    """Write plaintext admin password to SnappyMail's admin_password.txt.

    SnappyMail reads this file as the plaintext admin password on first setup,
    then stores a bcrypt hash in application.ini.
    """
    data_dir = os.path.join(sm_dir, 'data', '_data_', '_default_')
    os.makedirs(data_dir, exist_ok=True)

    pw_file = os.path.join(data_dir, 'admin_password.txt')
    with open(pw_file, 'w') as f:
        f.write(password)
    host_run(f'chown www-data:www-data {q(pw_file)}', timeout=5)


def _write_snappymail_domain_config(sm_dir, mail_domain):
    """Pre-configure SnappyMail for a mail domain (IMAP + SMTP on localhost)."""
    domains_dir = os.path.join(sm_dir, 'data', '_data_', '_default_', 'domains')
    os.makedirs(domains_dir, exist_ok=True)

    ini_content = f"""[IMAP]
host = "127.0.0.1"
port = 143
secure = "None"
shortLogin = false
lowerLogin = true
ssl_verify_peer = false
ssl_allow_self_signed = true

[SMTP]
host = "127.0.0.1"
port = 25
secure = "None"
shortLogin = false
lowerLogin = true
auth = true
authPlainLine = false
ssl_verify_peer = false
ssl_allow_self_signed = true

[WHITE_LIST]
; Allow all users on this domain
list = ""
"""
    ini_path = os.path.join(domains_dir, f'{mail_domain}.ini')
    with open(ini_path, 'w') as f:
        f.write(ini_content)
    host_run(f'chown www-data:www-data {q(ini_path)}', timeout=5)


def _generate_snappymail_nginx_conf(webmail_domain, ssl=False):
    """Generate nginx vhost config for SnappyMail."""
    sm_dir = _snappymail_dir()
    sock = _php_fpm_sock()
    lines = []

    if ssl:
        lines.append('server {')
        lines.append('    listen 80;')
        lines.append('    listen [::]:80;')
        lines.append(f'    server_name {webmail_domain};')
        lines.append('')
        lines.append('    location /.well-known/acme-challenge/ {')
        lines.append('        root /var/www/letsencrypt;')
        lines.append('        allow all;')
        lines.append('    }')
        lines.append('')
        lines.append('    location / {')
        lines.append('        return 301 https://$host$request_uri;')
        lines.append('    }')
        lines.append('}')
        lines.append('')

    lines.append('server {')
    if ssl:
        cert_dir = f'/etc/letsencrypt/live/{webmail_domain}'
        lines.append('    listen 443 ssl http2;')
        lines.append('    listen [::]:443 ssl http2;')
        lines.append(f'    server_name {webmail_domain};')
        lines.append(f'    ssl_certificate {cert_dir}/fullchain.pem;')
        lines.append(f'    ssl_certificate_key {cert_dir}/privkey.pem;')
        lines.append('    ssl_protocols TLSv1.2 TLSv1.3;')
        lines.append('    ssl_ciphers HIGH:!aNULL:!MD5;')
        lines.append('    ssl_prefer_server_ciphers on;')
    else:
        lines.append('    listen 80;')
        lines.append('    listen [::]:80;')
        lines.append(f'    server_name {webmail_domain};')

    lines.append('')
    lines.append(f'    root {sm_dir};')
    lines.append('    index index.php;')
    lines.append('')
    lines.append('    # ACME challenge for certbot webroot')
    lines.append('    location /.well-known/acme-challenge/ {')
    lines.append('        root /var/www/letsencrypt;')
    lines.append('        allow all;')
    lines.append('    }')
    lines.append('')
    lines.append('    # Block access to sensitive data')
    lines.append('    location ^~ /data {')
    lines.append('        deny all;')
    lines.append('        return 403;')
    lines.append('    }')
    lines.append('')
    lines.append('    location ~ \\.php$ {')
    lines.append('        fastcgi_split_path_info ^(.+\\.php)(/.+)$;')
    lines.append(f'        fastcgi_pass unix:{sock};')
    lines.append('        fastcgi_index index.php;')
    lines.append('        include fastcgi_params;')
    lines.append('        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;')
    lines.append('        fastcgi_param PATH_INFO $fastcgi_path_info;')
    lines.append('    }')
    lines.append('')
    lines.append('    location / {')
    lines.append('        try_files $uri $uri/ /index.php?$query_string;')
    lines.append('    }')
    lines.append('}')

    return '\n'.join(lines) + '\n'


@mail_bp.route('/webmail/setup-domain', methods=['POST'])
@admin_required
def webmail_setup_domain():
    """Create nginx vhost for mail.DOMAIN, auto-configure SnappyMail domain.

    Body: { "mail_domain": "example.com", "ssl": true }
    Creates vhost for mail.example.com pointing to SnappyMail.
    """
    if not _snappymail_installed():
        return jsonify(error='SnappyMail nie jest zainstalowany'), 400

    data = request.get_json(silent=True) or {}
    mail_domain = data.get('mail_domain', '').strip().lower()
    use_ssl = data.get('ssl', False)

    if not mail_domain:
        return jsonify(error='mail_domain is required'), 400

    webmail_domain = f'mail.{mail_domain}'
    sm_dir = _snappymail_dir()

    # 1) Write SnappyMail domain config (IMAP/SMTP on localhost)
    _write_snappymail_domain_config(sm_dir, mail_domain)
    host_run(f'chown -R www-data:www-data {q(os.path.join(sm_dir, "data"))}', timeout=15)

    # 2) Obtain SSL cert if requested
    if use_ssl:
        host_run('mkdir -p /var/www/letsencrypt', timeout=5)

        # Temporary HTTP-only vhost so certbot can validate
        tmp_conf = _generate_snappymail_nginx_conf(webmail_domain, ssl=False)
        conf_name = _snappymail_nginx_conf_name(webmail_domain)
        avail = f'/etc/nginx/sites-available/{conf_name}'
        enabled = f'/etc/nginx/sites-enabled/{conf_name}'

        with open(avail, 'w') as f:
            f.write(tmp_conf)
        host_run(f'ln -sf {q(avail)} {q(enabled)}', timeout=5)
        host_run('nginx -t && systemctl reload nginx', timeout=15)

        cfg = _load_config()
        admin_email = cfg.get('admin_email', f'admin@{mail_domain}')
        r = host_run(
            f'certbot certonly --webroot -w /var/www/letsencrypt '
            f'--non-interactive --agree-tos '
            f'--email {q(admin_email)} '
            f'-d {q(webmail_domain)}',
            timeout=120)

        cert_ok = (r.returncode == 0 and
                   os.path.exists(f'/etc/letsencrypt/live/{webmail_domain}/fullchain.pem'))

        if not cert_ok:
            use_ssl = False
            log.warning('SSL cert failed for %s, using HTTP: %s',
                        webmail_domain, (r.stderr or r.stdout or '')[:200])

    # 3) Write final nginx vhost
    conf_content = _generate_snappymail_nginx_conf(webmail_domain, ssl=use_ssl)
    conf_name = _snappymail_nginx_conf_name(webmail_domain)
    avail = f'/etc/nginx/sites-available/{conf_name}'
    enabled = f'/etc/nginx/sites-enabled/{conf_name}'

    with open(avail, 'w') as f:
        f.write(conf_content)
    host_run(f'ln -sf {q(avail)} {q(enabled)}', timeout=5)

    r_test = host_run('nginx -t 2>&1', timeout=10)
    if r_test.returncode != 0:
        host_run(f'rm -f {q(avail)} {q(enabled)}', timeout=5)
        host_run('systemctl reload nginx 2>/dev/null', timeout=10)
        return jsonify(error='Konfiguracja nginx niepoprawna: ' +
                       (r_test.stderr or r_test.stdout or '')[:200]), 500

    host_run('systemctl reload nginx', timeout=10)

    # 4) Save domain mapping
    cfg = _load_config()
    webmail_domains = cfg.get('webmail_domains', [])
    webmail_domains = [d for d in webmail_domains
                       if d.get('domain') != webmail_domain]
    webmail_domains.append({
        'domain': webmail_domain,
        'mail_domain': mail_domain,
        'ssl': use_ssl,
    })
    cfg['webmail_domains'] = webmail_domains
    _save_config(cfg)

    proto = 'https' if use_ssl else 'http'
    return jsonify(ok=True, data={
        'url': f'{proto}://{webmail_domain}',
        'ssl': use_ssl,
        'domain': webmail_domain,
    })
