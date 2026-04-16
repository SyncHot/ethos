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

SocketIO events emitted:
  mail_server_install  — { task_id, stage, percent, message }
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

# Userdb — virtual users all map to vmail
userdb {{
    driver = static
    args = uid={uid} gid={gid} home={mail_data}/%d/%n
}}

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
