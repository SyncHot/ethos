"""
EthOS - App Manager (Package Center)

Zarządza opcjonalnymi paczkami EthOS: install/uninstall/update z GitHub catalog.
Pobiera katalog z: https://raw.githubusercontent.com/SyncHot/ethos-os-ethos-apps/main/catalog.json

Endpoints:
  GET  /api/app-manager/catalog            -> pelny katalog z statusem instalacji
  POST /api/app-manager/catalog/refresh    -> wymusz odswiazenie z GitHub
  GET  /api/app-manager/installed          -> tylko zainstalowane apki
  GET  /api/app-manager/core               -> lista core apps
  GET  /api/app-manager/check-updates      -> sprawdz aktualizacje
  POST /api/app-manager/<id>/install       -> zainstaluj paczke (async)
  POST /api/app-manager/<id>/uninstall     -> odinstaluj paczke
  POST /api/app-manager/<id>/update        -> zaktualizuj do najnowszej wersji
  GET  /api/app-manager/<id>/status        -> status instalacji paczki

SocketIO events:
  app_manager_progress  ->  { task_id, stage, percent, message, app_id, status }
"""

import os
import json
import time
import threading
import logging
import sys
import uuid
import urllib.request
import urllib.error

from flask import Blueprint, request, jsonify, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, host_run_stream, data_path, app_path, q, _apt_exec, apt_install as _host_apt_install

log = logging.getLogger('app_manager')
app_manager_bp = Blueprint('app_manager', __name__, url_prefix='/api/app-manager')

# ─── SocketIO ref ────────────────────────────────────────────

_socketio = None
_flask_app = None


def init_app_manager(sio):
    global _socketio
    _socketio = sio


@app_manager_bp.record_once
def _on_register(state):
    global _flask_app
    _flask_app = state.app


def _emit(event_data):
    if _socketio:
        _socketio.emit('app_manager_progress', event_data)


# ─── Paths ───────────────────────────────────────────────────

_ETHOS_ROOT = app_path()
_FRONTEND_APPS_DIR = os.path.join(_ETHOS_ROOT, 'frontend', 'js', 'apps')
_BLUEPRINTS_DIR = os.path.join(_ETHOS_ROOT, 'backend', 'blueprints')

INSTALLED_FILE = data_path('installed_apps.json')
CATALOG_CACHE_FILE = '/tmp/ethos_app_catalog.json'
CATALOG_CACHE_TTL = 3600 * 6

GITHUB_CATALOG_URL = 'https://raw.githubusercontent.com/SyncHot/ethos-os-ethos-apps/main/catalog.json'
GITHUB_APP_BASE    = 'https://raw.githubusercontent.com/SyncHot/ethos-os-ethos-apps/main/apps'

# ─── Core Apps (wbudowane, nieusuwalne) ──────────────────────

CORE_APPS = frozenset({
    'dashboard', 'file-manager', 'storage-manager', 'terminal',
    'system-settings', 'users', 'updates', 'app-store', 'packages',
    'event-log', 'network', 'services', 'resource-monitor', 'backup',
    'power', 'notifications', 'ssh-manager', 'naslink',
    'firewall', 'fail2ban',
})

# ─── Frontend filename map ────────────────────────────────────

_FRONTEND_FILENAME = {
    'ai-chat':          'aichat',
    'disk-repair':      'diskrepair',
    'doc-anonymizer':   'doc-anonymizer',
    'doc-editor':       'editor',
    'download-manager': 'downloads',
    'domains-manager':  'domains',
    'family-hub':       'familyhub',
    'raid-lvm':         'raid',
    'ssh-manager':      'ssh',
    'sticky-notes':     'stickynotes',
    'storage-manager':  'storage',
    'usb-flasher':      'flasher',
    'resource-monitor': 'resources',
    'cloud-backup':     'cloud-backup',
    'code-editor':      'code-editor',
    'sharing-samba':    'sharing',
    'sharing-nfs':      'sharing',
    'sharing-dlna':     'dlna',
    'sharing-webdav':   'sharing',
    'sharing-sftp':     'sharing',
    'sharing-ftp':      'sharing',
    # W apps.js monolicie
    'file-manager':     None,
    'docker-manager':   'docker-manager',
    'vm-manager':       'vm-manager',
    'event-log':        None,
    'app-store':        None,
    'remote-log':       'remote-log',
    'system-settings':  None,
}

# Maps app_id → (module_filename, blueprint_var, init_func_or_None, socketio_attr_needed)
# module_filename: the .py filename without extension in backend/blueprints/
# blueprint_var: the variable name of the Blueprint object in that module
# init_func_or_None: function name to call with (socketio) after registering, or None
# socketio_attr_needed: if True, set bp._socketio = socketio before registering
_OPTIONAL_BLUEPRINTS = {
    'surveillance':    ('surveillance',    'surveillance_bp',  'init_surveillance', True),
    'ai-chat':         ('aichat',          'aichat_bp',        None,                True),
    'doc-anonymizer':  ('doc_anonymizer',  'doc_anonymizer_bp', None,               True),
    'gallery':         ('gallery',         'gallery_bp',        None,                False),
    'download-manager':('downloads',       'downloads_bp',     'init_downloads',    True),
    'printer':         ('printer',         'printer_bp',        None,                False),
    'docker-manager':  ('docker_manager',  'docker_bp',         None,                True),
    'vm-manager':      ('vm_manager',      'vm_bp',             None,                True),
    'doc-editor':      ('editor',          'editor_bp',         None,                False),
    'usb-flasher':     ('flasher',         'flasher_bp',        None,                False),
    'builder':         ('builder',         'builder_bp',        None,                False),
    'disk-repair':     ('diskrepair',      'diskrepair_bp',     None,                False),
    'remote-log':      ('remote_log',      'remote_log_bp',    'init_remote_log',   False),
    'sharing-samba':   ('sharing',         'sharing_bp',        None,                False),
    'sharing-dlna':    ('dlna',            'dlna_bp',           None,                False),
    'domains-manager': ('domains_manager', 'domains_mgr_bp',    None,                False),
    'websites':        ('websites',        'websites_bp',       None,                False),
    'cloud-backup':    ('cloud_backup',    'cloud_backup_bp',   None,                False),
    'raid-lvm':        ('raid_manager',    'raid_bp',           None,                False),
    'wireguard':       ('wireguard',       'wireguard_bp',      None,                True),
    'antivirus':       ('antivirus',       'antivirus_bp',      None,                True),
    'rollback':        ('rollback',        'rollback_bp',       None,                False),
    'firewall':        ('firewall',        'firewall_bp',       None,                True),
    'fail2ban':        ('fail2ban',        'fail2ban_bp',       None,                True),
    'cron':            ('cron_manager',    'cron_bp',           None,                False),
    'ups':             ('ups',             'ups_bp',           'init_ups',          False),
    'family-hub':      ('familyhub',       'familyhub_bp',      None,                False),
    'sticky-notes':    ('stickynotes',     'notes_bp',          None,                False),
    'tickets':         ('tickets',         'tickets_bp',       'init_tickets',      True),
}

# Public alias
OPTIONAL_BLUEPRINTS = _OPTIONAL_BLUEPRINTS

# ─── Built-in catalog (fallback gdy GitHub niedostepny) ──────

BUILTIN_CATALOG = [
    {
        'id': 'surveillance', 'name': 'Surveillance', 'version': '1.0.0',
        'icon': 'fa-video', 'color': '#dc2626', 'category': 'Security', 'admin_only': False,
        'description': 'Monitoring IP kamer z detekcja ruchu i podgladem na zywo.',
        'apt_deps': ['ffmpeg'], 'pip_deps': ['onvif-zeep'],
        'install_endpoint': '/api/surveillance/install',
        'uninstall_endpoint': '/api/surveillance/uninstall',
        'status_endpoint': '/api/surveillance/status',
    },
    {
        'id': 'ai-chat', 'name': 'AI Assistant', 'version': '1.0.0',
        'icon': 'fa-robot', 'color': '#8b5cf6', 'category': 'Tools', 'admin_only': False,
        'description': 'Asystent AI z obsługą GPT, Claude i lokalnych modeli LLM.',
        'apt_deps': [], 'pip_deps': ['openai', 'anthropic', 'huggingface_hub'],
        'install_endpoint': '/api/aichat/install',
        'uninstall_endpoint': '/api/aichat/uninstall',
        'status_endpoint': '/api/aichat/status',
    },
    {
        'id': 'doc-anonymizer', 'name': 'Document Anonymizer', 'version': '1.0.0',
        'icon': 'fa-user-shield', 'color': '#0ea5e9', 'category': 'Tools', 'admin_only': False,
        'description': 'Anonimizacja dokumentow medycznych PDF/DOCX przy uzyciu polskiego modelu Bielik LLM.',
        'apt_deps': [], 'pip_deps': ['PyPDF2', 'python-docx', 'reportlab'],
        'depends_on': ['ai-chat'],
        'install_endpoint': '/api/doc-anonymizer/install',
        'uninstall_endpoint': '/api/doc-anonymizer/uninstall',
        'status_endpoint': '/api/doc-anonymizer/pkg-status',
    },
    {
        'id': 'gallery', 'name': 'Gallery', 'version': '1.0.0',
        'icon': 'fa-images', 'color': '#ec4899', 'category': 'Media', 'admin_only': False,
        'description': 'Galeria zdjec i filmow z EXIF, miniaturkami i haslami folderow.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/gallery/install',
        'uninstall_endpoint': '/api/gallery/uninstall',
        'status_endpoint': '/api/gallery/pkg-status',
    },
    {
        'id': 'download-manager', 'name': 'Download Manager', 'version': '1.0.0',
        'icon': 'fa-cloud-download-alt', 'color': '#10b981', 'category': 'Tools', 'admin_only': False,
        'description': 'Pobieranie plikow z HTTP, torrent, magnet i serwisow premium.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/downloads/install',
        'uninstall_endpoint': '/api/downloads/uninstall',
        'status_endpoint': '/api/downloads/pkg-status',
    },
    {
        'id': 'printer', 'name': 'Print Server', 'version': '1.0.0',
        'icon': 'fa-print', 'color': '#ef4444', 'category': 'Tools', 'admin_only': True,
        'description': 'Serwer drukowania z automatycznym wykrywaniem drukarek i konwersja PDF.',
        'apt_deps': ['cups', 'cups-browsed', 'libreoffice'], 'pip_deps': [],
        'install_endpoint': '/api/printer/install',
        'uninstall_endpoint': '/api/printer/uninstall',
        'status_endpoint': '/api/printer/pkg-status',
    },
    {
        'id': 'docker-manager', 'name': 'Docker Manager', 'version': '1.0.0',
        'icon': 'fa-cubes', 'color': '#2496ed', 'category': 'System', 'admin_only': True,
        'description': 'Zarządzanie kontenerami Docker, projektami Compose, obrazami i logami.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/docker/install',
        'uninstall_endpoint': '/api/docker/uninstall',
        'status_endpoint': '/api/docker/pkg-status',
    },
    {
        'id': 'vm-manager', 'name': 'VM Manager', 'version': '1.0.0',
        'icon': 'fa-desktop', 'color': '#8b5cf6', 'category': 'System', 'admin_only': True,
        'description': 'Maszyny wirtualne QEMU/KVM z migawkami i dostepem VNC.',
        'apt_deps': ['qemu-system-x86', 'qemu-utils', 'ovmf'], 'pip_deps': [],
        'install_endpoint': '/api/vm/install',
        'uninstall_endpoint': '/api/vm/uninstall',
        'status_endpoint': '/api/vm/pkg-status',
    },
    {
        'id': 'doc-editor', 'name': 'Documents', 'version': '1.0.0',
        'icon': 'fa-file-word', 'color': '#2563eb', 'category': 'Tools', 'admin_only': False,
        'description': 'Tworzenie i edycja dokumentow Word z eksportem do PDF.',
        'apt_deps': ['libreoffice'], 'pip_deps': ['mammoth', 'python-docx'],
        'install_endpoint': '/api/editor/install',
        'uninstall_endpoint': '/api/editor/uninstall',
        'status_endpoint': '/api/editor/pkg-status',
    },
    {
        'id': 'code-editor', 'name': 'Code Editor', 'version': '1.0.0',
        'icon': 'fa-code', 'color': '#22d3ee', 'category': 'Tools', 'admin_only': False,
        'description': 'Edytor kodu z podswietlaniem skladni i numerami linii.',
        'apt_deps': [], 'pip_deps': [],
        'simple': True,
    },
    {
        'id': 'duplicates', 'name': 'Duplicates', 'version': '1.0.0',
        'icon': 'fa-clone', 'color': '#a78bfa', 'category': 'Tools', 'admin_only': False,
        'description': 'Znajdz identyczne i podobne zdjecia uzywajac perceptual hashing.',
        'apt_deps': [], 'pip_deps': [],
        'simple': True,
    },
    {
        'id': 'usb-flasher', 'name': 'USB Creator', 'version': '1.0.0',
        'icon': 'fa-usb', 'color': '#a855f7', 'category': 'Tools', 'admin_only': True,
        'description': 'Flashowanie obrazow ISO/IMG na pendrive z monitoringiem postepu.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/flasher/install',
        'uninstall_endpoint': '/api/flasher/uninstall',
        'status_endpoint': '/api/flasher/pkg-status',
    },
    {
        'id': 'builder', 'name': 'Builder', 'version': '1.0.0',
        'icon': 'fa-hammer', 'color': '#f97316', 'category': 'System', 'admin_only': True,
        'description': 'Budowanie wydan EthOS i obrazow systemowych przez interfejs webowy.',
        'apt_deps': ['squashfs-tools', 'genisoimage', 'rsync'], 'pip_deps': [],
        'install_endpoint': '/api/builder/install',
        'uninstall_endpoint': '/api/builder/uninstall',
        'status_endpoint': '/api/builder/pkg-status',
    },
    {
        'id': 'disk-repair', 'name': 'Disk Repair', 'version': '1.0.0',
        'icon': 'fa-wrench', 'color': '#ef4444', 'category': 'Storage', 'admin_only': True,
        'description': 'Diagnostyka SMART i sprawdzanie systemu plikow z narzedziami naprawczymi.',
        'apt_deps': ['smartmontools', 'e2fsprogs'], 'pip_deps': [],
        'install_endpoint': '/api/diskrepair/install',
        'uninstall_endpoint': '/api/diskrepair/uninstall',
        'status_endpoint': '/api/diskrepair/pkg-status',
    },
    {
        'id': 'remote-log', 'name': 'Remote Logs', 'version': '1.0.0',
        'icon': 'fa-satellite-dish', 'color': '#0891b2', 'category': 'System', 'admin_only': True,
        'description': 'Wysylanie logow diagnostycznych na centralny serwer.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/remote-log/install',
        'uninstall_endpoint': '/api/remote-log/uninstall',
        'status_endpoint': '/api/remote-log/pkg-status',
    },
    {
        'id': 'sharing-samba', 'name': 'File Sharing (Samba)', 'version': '1.0.0',
        'icon': 'fa-windows', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Udostepnianie plikow przez siec (Windows, Mac, Linux).',
        'apt_deps': ['samba'], 'pip_deps': [],
        'install_endpoint': '/api/storage/samba/pkg-install',
        'uninstall_endpoint': '/api/storage/samba/pkg-uninstall',
        'status_endpoint': '/api/storage/samba/pkg-status',
    },
    {
        'id': 'sharing-nfs', 'name': 'NFS', 'version': '1.0.0',
        'icon': 'fa-network-wired', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Szybkie udostepnianie plikow dla Linux/Unix przez NFS.',
        'apt_deps': ['nfs-kernel-server'], 'pip_deps': [],
        'install_endpoint': '/api/storage/nfs/pkg-install',
        'uninstall_endpoint': '/api/storage/nfs/pkg-uninstall',
        'status_endpoint': '/api/storage/nfs/pkg-status',
    },
    {
        'id': 'sharing-dlna', 'name': 'DLNA (MiniDLNA)', 'version': '1.0.0',
        'icon': 'fa-photo-video', 'color': '#6366f1', 'category': 'Media', 'admin_only': True,
        'description': 'Serwer DLNA do strumieniowania multimediow na TV i odtwarzacze.',
        'apt_deps': ['minidlna'], 'pip_deps': [],
        'install_endpoint': '/api/storage/dlna/pkg-install',
        'uninstall_endpoint': '/api/storage/dlna/pkg-uninstall',
        'status_endpoint': '/api/storage/dlna/pkg-status',
    },
    {
        'id': 'sharing-webdav', 'name': 'WebDAV', 'version': '1.0.0',
        'icon': 'fa-globe', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Serwer WebDAV z dostepem do plikow przez HTTP.',
        'apt_deps': ['lighttpd'], 'pip_deps': [],
        'install_endpoint': '/api/storage/webdav/pkg-install',
        'uninstall_endpoint': '/api/storage/webdav/pkg-uninstall',
        'status_endpoint': '/api/storage/webdav/pkg-status',
    },
    {
        'id': 'sharing-sftp', 'name': 'SFTP', 'version': '1.0.0',
        'icon': 'fa-lock', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Bezpieczny transfer plikow przez SSH.',
        'apt_deps': ['openssh-server'], 'pip_deps': [],
        'install_endpoint': '/api/storage/sftp/pkg-install',
        'uninstall_endpoint': '/api/storage/sftp/pkg-uninstall',
        'status_endpoint': '/api/storage/sftp/pkg-status',
    },
    {
        'id': 'sharing-ftp', 'name': 'FTP', 'version': '1.0.0',
        'icon': 'fa-upload', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Klasyczny serwer FTP z obsługa vsftpd.',
        'apt_deps': ['vsftpd'], 'pip_deps': [],
        'install_endpoint': '/api/storage/ftp/pkg-install',
        'uninstall_endpoint': '/api/storage/ftp/pkg-uninstall',
        'status_endpoint': '/api/storage/ftp/pkg-status',
    },
    {
        'id': 'domains-manager', 'name': 'Domains & SSL', 'version': '1.0.0',
        'icon': 'fa-globe', 'color': '#059669', 'category': 'Network', 'admin_only': True,
        'description': 'Domeny z certyfikatami SSL, reverse proxy i Dynamic DNS.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/ddns/install',
        'uninstall_endpoint': '/api/ddns/uninstall',
        'status_endpoint': '/api/ddns/pkg-status',
    },
    {
        'id': 'websites', 'name': 'Websites', 'version': '1.0.0',
        'icon': 'fa-globe-americas', 'color': '#14b8a6', 'category': 'Tools', 'admin_only': False,
        'description': 'Kreator stron z CMS, szablonami i edytorem wizualnym.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/websites/install',
        'uninstall_endpoint': '/api/websites/uninstall',
        'status_endpoint': '/api/websites/pkg-status',
    },
    {
        'id': 'cloud-backup', 'name': 'Cloud Backup', 'version': '1.0.0',
        'icon': 'fa-cloud-upload-alt', 'color': '#0ea5e9', 'category': 'Storage', 'admin_only': True,
        'description': 'Backup do S3, Backblaze, Google Drive, WebDAV i SFTP z harmonogramem.',
        'apt_deps': ['rclone'], 'pip_deps': [],
        'install_endpoint': '/api/cloud-backup/install',
        'uninstall_endpoint': '/api/cloud-backup/uninstall',
        'status_endpoint': '/api/cloud-backup/pkg-status',
    },
    {
        'id': 'raid-lvm', 'name': 'RAID / LVM', 'version': '1.0.0',
        'icon': 'fa-layer-group', 'color': '#f59e0b', 'category': 'Storage', 'admin_only': True,
        'description': 'Macierze RAID z mdadm i wolumeny LVM.',
        'apt_deps': ['mdadm', 'lvm2'], 'pip_deps': [],
        'install_endpoint': '/api/raid/install',
        'uninstall_endpoint': '/api/raid/uninstall',
        'status_endpoint': '/api/raid/pkg-status',
    },
    {
        'id': 'wireguard', 'name': 'VPN (WireGuard)', 'version': '1.0.0',
        'icon': 'fa-shield-halved', 'color': '#7c3aed', 'category': 'Network', 'admin_only': True,
        'description': 'Serwer VPN WireGuard z peerami i kodami QR.',
        'apt_deps': ['wireguard', 'wireguard-tools', 'qrencode'], 'pip_deps': [],
        'install_endpoint': '/api/wireguard/install',
        'uninstall_endpoint': '/api/wireguard/uninstall',
        'status_endpoint': '/api/wireguard/pkg-status',
    },
    {
        'id': 'antivirus', 'name': 'Antivirus (ClamAV)', 'version': '1.0.0',
        'icon': 'fa-shield-virus', 'color': '#16a34a', 'category': 'Security', 'admin_only': True,
        'description': 'ClamAV antywirus — skanowanie na zadanie i zaplanowane.',
        'apt_deps': ['clamav', 'clamav-freshclam'], 'pip_deps': [],
        'install_endpoint': '/api/antivirus/install',
        'uninstall_endpoint': '/api/antivirus/uninstall',
        'status_endpoint': '/api/antivirus/pkg-status',
    },
    {
        'id': 'rollback', 'name': 'Rollback', 'version': '1.0.0',
        'icon': 'fa-history', 'color': '#f97316', 'category': 'System', 'admin_only': True,
        'description': 'Migawki systemu i przywracanie poprzednich wersji.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'firewall', 'name': 'Firewall (UFW)', 'version': '1.0.0',
        'icon': 'fa-fire', 'color': '#e05d44', 'category': 'Security', 'admin_only': True,
        'description': 'Zarządzanie regułami zapory i portami.',
        'apt_deps': [], 'pip_deps': [], 'simple': True, 'core': True,
    },
    {
        'id': 'fail2ban', 'name': 'Intrusion Protection', 'version': '1.0.0',
        'icon': 'fa-shield-alt', 'color': '#ef4444', 'category': 'Security', 'admin_only': True,
        'description': 'Fail2Ban — aktywne bany, whitelist, ochrona SSH/Samba/Web.',
        'apt_deps': [], 'pip_deps': [], 'simple': True, 'core': True,
    },
    {
        'id': 'cron', 'name': 'Scheduler', 'version': '1.0.0',
        'icon': 'fa-clock', 'color': '#6366f1', 'category': 'System', 'admin_only': True,
        'description': 'Harmonogram zadan z zarządzaniem cron jobs.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'ups', 'name': 'UPS', 'version': '1.0.0',
        'icon': 'fa-battery-full', 'color': '#f59e0b', 'category': 'System', 'admin_only': True,
        'description': 'Status baterii UPS i zarządzanie bezpiecznym wyłączeniem.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'family-hub', 'name': 'Family Hub', 'version': '1.0.0',
        'icon': 'fa-house-user', 'color': '#f472b6', 'category': 'Tools', 'admin_only': False,
        'description': 'Tablica ogloszen, listy zakupow, zadania i kalendarz rodzinny.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'sticky-notes', 'name': 'Sticky Notes', 'version': '1.0.0',
        'icon': 'fa-sticky-note', 'color': '#fbbf24', 'category': 'Tools', 'admin_only': False,
        'description': 'Szybkie notatki przyklejane do pulpitu.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'tickets', 'name': 'Tickets', 'version': '1.0.0',
        'icon': 'fa-tasks', 'color': '#06b6d4', 'category': 'Tools', 'admin_only': False,
        'description': 'Kanban — zarządzanie projektami i zadaniami.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
]

# ─── State lock ───────────────────────────────────────────────

_state_lock = threading.RLock()
_catalog_lock = threading.RLock()

# ─── Installed state helpers ──────────────────────────────────

def _load_installed():
    with _state_lock:
        try:
            if os.path.isfile(INSTALLED_FILE):
                with open(INSTALLED_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}


# Public alias so app.py can import without touching private names
load_installed = _load_installed


def _save_installed(state):
    with _state_lock:
        tmp = INSTALLED_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, INSTALLED_FILE)


def _set_installed(app_id, version, source='bundled'):
    from datetime import datetime
    state = _load_installed()
    state[app_id] = {
        'version': version,
        'source': source,
        'installed_at': datetime.utcnow().isoformat(),
    }
    _save_installed(state)


def _get_internal_token():
    """Return a valid admin token from tokens.db for internal test_client calls."""
    try:
        import sqlite3 as _sq
        with _sq.connect(data_path('tokens.db')) as _conn:
            _row = _conn.execute(
                "SELECT token FROM tokens WHERE role='admin' AND expires > strftime('%s','now') "
                "ORDER BY expires DESC LIMIT 1"
            ).fetchone()
        return _row[0] if _row else None
    except Exception as e:
        log.warning('[app_manager] Could not fetch internal token: %s', e)
        return None


def _internal_post(tc, endpoint, **kwargs):
    """POST to an internal endpoint with an admin token."""
    token = _get_internal_token()
    headers = kwargs.pop('headers', {})
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return tc.post(endpoint, headers=headers, **kwargs)


def _set_uninstalled(app_id):
    state = _load_installed()
    state.pop(app_id, None)
    _save_installed(state)
    # Also clear legacy ethos_packages.json so get_apps() doesn't show the app
    _clear_legacy_pkg(app_id)


def _clear_legacy_pkg(app_id):
    """Remove app_id from ethos_packages.json (legacy state file)."""
    try:
        legacy = data_path('ethos_packages.json')
        if not os.path.isfile(legacy):
            return
        with open(legacy) as f:
            state = json.load(f)
        if app_id in state:
            state[app_id] = {'installed': False, 'installed_at': ''}
            with open(legacy, 'w') as f:
                json.dump(state, f, indent=2)
    except Exception as e:
        log.warning('[app_manager] Could not clear legacy pkg state for %s: %s', app_id, e)


# ─── Migration from ethos_packages.json ──────────────────────

def migrate_from_ethos_packages():
    """Jednorazowa migracja: wczytaj ethos_packages.json -> installed_apps.json.
    Wywolywana przy starcie serwera z app.py."""
    if os.path.isfile(INSTALLED_FILE):
        return

    from datetime import datetime
    now = datetime.utcnow().isoformat()

    old_file = data_path('ethos_packages.json')
    new_state = {}

    if os.path.isfile(old_file):
        try:
            with open(old_file) as f:
                old_state = json.load(f)
            for pkg_id, pkg_info in old_state.items():
                if isinstance(pkg_info, dict) and pkg_info.get('installed'):
                    new_state[pkg_id] = {
                        'version': 'bundled',
                        'source': 'bundled',
                        'installed_at': pkg_info.get('installed_at', now),
                    }
        except Exception as e:
            log.warning('Migration from ethos_packages.json failed: %s', e)

    # Wykryj apki bundled na podstawie plikow na dysku
    for app in BUILTIN_CATALOG:
        aid = app['id']
        if aid in new_state:
            continue
        fn = _get_frontend_filename(aid)
        if fn is None:
            # W monolicie apps.js — traktuj jako zainstalowane
            new_state[aid] = {'version': 'bundled', 'source': 'bundled', 'installed_at': now}
            continue
        fp = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
            new_state[aid] = {'version': 'bundled', 'source': 'bundled', 'installed_at': now}

    _save_installed(new_state)
    log.info('[app_manager] Migrated %d packages to installed_apps.json', len(new_state))


# ─── Catalog helpers ─────────────────────────────────────────

def _get_frontend_filename(app_id):
    if app_id in _FRONTEND_FILENAME:
        return _FRONTEND_FILENAME[app_id]
    return app_id


def _load_catalog_cache():
    try:
        if not os.path.isfile(CATALOG_CACHE_FILE):
            return None
        age = time.time() - os.path.getmtime(CATALOG_CACHE_FILE)
        if age > CATALOG_CACHE_TTL:
            return None
        with open(CATALOG_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _save_catalog_cache(data):
    try:
        tmp = CATALOG_CACHE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, CATALOG_CACHE_FILE)
    except Exception as e:
        log.warning('[app_manager] Cannot save catalog cache: %s', e)


def _fetch_github_catalog():
    try:
        req = urllib.request.Request(
            GITHUB_CATALOG_URL,
            headers={'User-Agent': 'EthOS-AppManager/1.0'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, dict) and 'apps' in data:
            return data['apps']
        if isinstance(data, list):
            return data
    except Exception as e:
        log.debug('[app_manager] GitHub catalog unavailable: %s', e)
    return None


def _get_catalog(force_refresh=False):
    with _catalog_lock:
        cached = None if force_refresh else _load_catalog_cache()
        if cached is not None:
            return cached.get('apps', BUILTIN_CATALOG)

        github_apps = _fetch_github_catalog()
        if github_apps is not None:
            builtin_by_id = {a['id']: a for a in BUILTIN_CATALOG}
            merged = []
            for app in github_apps:
                base = builtin_by_id.get(app['id'], {}).copy()
                base.update(app)
                merged.append(base)
            github_ids = {a['id'] for a in github_apps}
            for app in BUILTIN_CATALOG:
                if app['id'] not in github_ids:
                    merged.append(app)
            _save_catalog_cache({'apps': merged, 'source': 'github', 'fetched_at': time.time()})
            return merged

        _save_catalog_cache({'apps': BUILTIN_CATALOG, 'source': 'builtin', 'fetched_at': time.time()})
        return BUILTIN_CATALOG


# ─── Install helpers ─────────────────────────────────────────

def _is_bundled(app_id):
    fn = _get_frontend_filename(app_id)
    if fn is None:
        return True
    fp = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
    return os.path.isfile(fp) and os.path.getsize(fp) > 0


def _download_file(url, dest_path):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
        tmp = dest_path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(content)
        os.replace(tmp, dest_path)
        return True
    except Exception as e:
        log.error('[app_manager] Download failed %s: %s', url, e)
        return False


def _install_apt_deps(deps, emit_fn):
    """Install APT dependencies with streaming progress updates."""
    if not deps:
        return True
    pkgs = ' '.join(q(d) for d in deps)
    emit_fn({'stage': 'deps_apt', 'message': 'apt-get update...', 'percent': 25, 'status': 'running'})

    cmd = (
        f'DEBIAN_FRONTEND=noninteractive apt-get update -y -qq 2>/dev/null; '
        f'DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs} 2>&1'
    )
    lock_file = '/tmp/ethos-apt.lock'
    wrapped = f"flock -w 180 {q(lock_file)} bash -lc {q(cmd)}"

    exit_code = -1
    last_err = ''
    count = 0
    for line in host_run_stream(wrapped):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            exit_code = int(stripped.split(':', 1)[1])
            break
        if not stripped:
            continue
        # Track apt progress — emit every few meaningful lines
        lower = stripped.lower()
        if any(kw in lower for kw in ('unpacking', 'setting up', 'installing', 'get:', 'fetched')):
            count += 1
            pct = min(40, 28 + count)
            short = stripped[:80]
            emit_fn({'stage': 'deps_apt', 'message': short, 'percent': pct, 'status': 'running'})
        if 'e:' in lower or 'err' in lower:
            last_err = stripped

    if exit_code != 0:
        detail = last_err[:120] if last_err else f'exit code {exit_code}'
        log.error('[app_manager] apt install failed (rc=%s): %s', exit_code, detail)
        emit_fn({'stage': 'error', 'percent': 0,
                 'message': f'apt: {detail}', 'status': 'error'})
        return False
    emit_fn({'stage': 'deps_apt', 'message': 'Pakiety apt zainstalowane', 'percent': 42, 'status': 'running'})
    return True


def _install_pip_deps(deps, emit_fn):
    """Install pip dependencies with streaming progress updates."""
    if not deps:
        return True
    pkgs = ' '.join(q(d) for d in deps)
    venv = os.path.join(_ETHOS_ROOT, 'venv')
    pip = os.path.join(venv, 'bin', 'pip') if os.path.isdir(venv) else 'pip3'
    emit_fn({'stage': 'deps_pip', 'message': 'pip install: ' + ', '.join(deps), 'percent': 45, 'status': 'running'})

    cmd = q(pip) + ' install --progress-bar off ' + pkgs + ' 2>&1'

    exit_code = -1
    last_err = ''
    count = 0
    for line in host_run_stream(cmd):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            exit_code = int(stripped.split(':', 1)[1])
            break
        if not stripped:
            continue
        lower = stripped.lower()
        if any(kw in lower for kw in ('collecting', 'downloading', 'installing', 'building', 'successfully')):
            count += 1
            pct = min(55, 47 + count)
            short = stripped[:80]
            emit_fn({'stage': 'deps_pip', 'message': short, 'percent': pct, 'status': 'running'})
        if 'error' in lower:
            last_err = stripped

    if exit_code != 0:
        detail = last_err[:120] if last_err else f'exit code {exit_code}'
        log.error('[app_manager] pip install failed (rc=%s): %s', exit_code, detail)
        emit_fn({'stage': 'error', 'percent': 0,
                 'message': f'pip: {detail}', 'status': 'error'})
        return False
    emit_fn({'stage': 'deps_pip', 'message': 'Pakiety pip zainstalowane', 'percent': 57, 'status': 'running'})
    return True


def _sync_frontend_dist():
    frontend = os.path.join(_ETHOS_ROOT, 'frontend')
    dist = os.path.join(_ETHOS_ROOT, 'frontend_dist')
    if os.path.isdir(dist):
        host_run('rsync -av --delete ' + q(frontend + '/') + ' ' + q(dist + '/'), timeout=60)


_active_tasks = 0
_active_tasks_lock = threading.Lock()


def _task_start():
    """Increment active background task counter."""
    global _active_tasks
    with _active_tasks_lock:
        _active_tasks += 1


def _task_done():
    """Decrement active background task counter."""
    global _active_tasks
    with _active_tasks_lock:
        _active_tasks = max(0, _active_tasks - 1)


def _restart_server():
    """Full server restart — only used as fallback when hot-load fails."""
    def _do():
        import time as _t
        _t.sleep(1.5)
        host_run('systemctl restart ethos', timeout=10)
    threading.Thread(target=_do, daemon=True).start()


def _hot_load_blueprint(app_id):
    """Load an optional blueprint at runtime without server restart.
    Returns True if blueprint is ready (loaded or no backend needed).
    Returns False if loading failed (caller should fall back to restart).
    """
    import importlib
    import inspect

    bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
    if not bp_info:
        return True  # No backend needed (frontend-only / simple app)

    module_name, bp_var, init_fn, needs_sio = bp_info

    bp_file = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
    if not os.path.isfile(bp_file):
        return True  # No backend file on disk — frontend-only app

    if not _flask_app:
        log.warning('[app_manager] Flask app not available for hot-load')
        return False

    # Check if already loaded (module in cache + blueprint registered)
    mod_key = 'blueprints.' + module_name
    if mod_key in sys.modules:
        mod = sys.modules[mod_key]
        bp = getattr(mod, bp_var, None)
        if bp and bp.name in _flask_app.blueprints:
            log.info('[app_manager] Blueprint %s already loaded, skipping hot-load', bp.name)
            return True

    try:
        if mod_key in sys.modules:
            mod = importlib.reload(sys.modules[mod_key])
        else:
            mod = importlib.import_module(mod_key)

        bp = getattr(mod, bp_var)

        if needs_sio and _socketio:
            bp._socketio = _socketio

        # Skip registration if blueprint name already in app (e.g. bundled reload)
        if bp.name in _flask_app.blueprints:
            log.info('[app_manager] Blueprint %s already registered', bp.name)
            return True

        # Temporarily bypass Flask's first-request assertion to allow
        # runtime blueprint registration.  Gevent is cooperative so no
        # other greenlet can interleave between the flag flip.
        _flask_app._got_first_request = False
        try:
            _flask_app.register_blueprint(bp)
        finally:
            _flask_app._got_first_request = True

        if init_fn:
            fn = getattr(mod, init_fn, None)
            if fn:
                sig = inspect.signature(fn)
                if sig.parameters and _socketio:
                    fn(_socketio)
                else:
                    fn()

        log.info('[app_manager] Hot-loaded blueprint: %s', module_name)
        return True
    except Exception as e:
        log.error('[app_manager] Hot-load failed for %s: %s', module_name, e)
        return False


def load_optional_blueprints(flask_app, socketio_instance):
    """Dynamically load optional blueprints that are present on disk.
    Called from app.py after Flask app and SocketIO are initialized.
    Blueprints missing from disk (not yet installed) are silently skipped.
    """
    import importlib
    import inspect
    blueprints_dir = os.path.join(os.path.dirname(__file__))
    loaded_modules = set()

    for app_id, (module_name, bp_var, init_fn, needs_sio) in _OPTIONAL_BLUEPRINTS.items():
        bp_file = os.path.join(blueprints_dir, module_name + '.py')
        if not os.path.isfile(bp_file):
            log.debug('[app_manager] Optional blueprint not found, skipping: %s', module_name)
            continue
        if module_name in loaded_modules:
            continue
        loaded_modules.add(module_name)
        try:
            mod = importlib.import_module('blueprints.' + module_name)
            bp = getattr(mod, bp_var)
            if needs_sio and socketio_instance:
                bp._socketio = socketio_instance
            flask_app.register_blueprint(bp)
            if init_fn:
                fn = getattr(mod, init_fn, None)
                if fn:
                    sig = inspect.signature(fn)
                    if sig.parameters and socketio_instance:
                        fn(socketio_instance)
                    else:
                        fn()
            log.info('[app_manager] Loaded optional blueprint: %s', module_name)
        except Exception as e:
            log.error('[app_manager] Failed to load optional blueprint %s: %s', module_name, e)


# ─── Background tasks ─────────────────────────────────────────

def _bg_install(app_id, app_def, task_id):
    def emit(extra):
        _emit({'task_id': task_id, 'app_id': app_id, **extra})

    _needs_restart = False
    _task_start()
    try:
        emit({'stage': 'start', 'percent': 5, 'message': 'Instalowanie ' + app_def['name'] + '...', 'status': 'running'})

        # Pobierz pliki z GitHub jesli nie ma na dysku
        if not _is_bundled(app_id):
            emit({'stage': 'download', 'percent': 10, 'message': 'Pobieranie pliku frontend...', 'status': 'running'})
            fn = _get_frontend_filename(app_id)
            if fn:
                url = GITHUB_APP_BASE + '/' + app_id + '/frontend.js'
                dest = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                if not _download_file(url, dest):
                    emit({'stage': 'error', 'percent': 0, 'message': 'Bląd pobierania frontend', 'status': 'error'})
                    return
        else:
            emit({'stage': 'download', 'percent': 15, 'message': 'Pliki juz dostepne (bundled)', 'status': 'running'})

        # Download backend.py from GitHub if not on disk
        # (Builder images keep frontend JS but remove optional backend .py)
        bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
        if bp_info:
            module_name = bp_info[0]
            bp_dest = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
            if not os.path.isfile(bp_dest):
                bp_url = GITHUB_APP_BASE + '/' + app_id + '/backend.py'
                emit({'stage': 'download_backend', 'percent': 20, 'message': 'Pobieranie backend...', 'status': 'running'})
                if not _download_file(bp_url, bp_dest):
                    emit({'stage': 'error', 'percent': 0, 'message': 'Bład pobierania backend — sprawdz połaczenie z internetem', 'status': 'error'})
                    return

        # Instalacja zaleznosci (apt: 25-42%, pip: 45-57%)
        apt_deps = app_def.get('apt_deps', [])
        if apt_deps and not _install_apt_deps(apt_deps, emit):
            return

        pip_deps = app_def.get('pip_deps', [])
        if pip_deps and not _install_pip_deps(pip_deps, emit):
            return

        # Hot-load blueprint so its routes are available immediately
        emit({'stage': 'load', 'percent': 65, 'message': 'Ładowanie modułu...', 'status': 'running'})
        hot_ok = _hot_load_blueprint(app_id)

        # Call app's install endpoint (now works even for first install)
        install_ep = app_def.get('install_endpoint')
        if install_ep and not app_def.get('simple') and _flask_app:
            emit({'stage': 'configure', 'percent': 75, 'message': 'Konfigurowanie apki...', 'status': 'running'})
            try:
                with _flask_app.app_context():
                    with _flask_app.test_client() as tc:
                        _internal_post(tc, install_ep)
            except Exception as e:
                log.warning('[app_manager] install_endpoint %s failed: %s', install_ep, e)

        # Synchronizacja frontend_dist
        emit({'stage': 'sync', 'percent': 85, 'message': 'Synchronizacja plikow frontend...', 'status': 'running'})
        _sync_frontend_dist()

        version = app_def.get('version', 'bundled')
        source = 'bundled' if _is_bundled(app_id) else 'github'
        _set_installed(app_id, version, source)

        emit({'stage': 'done', 'percent': 100, 'message': app_def['name'] + ' zainstalowano pomyslnie', 'status': 'done'})

        if not hot_ok:
            log.warning('[app_manager] Hot-load failed for %s, falling back to restart', app_id)
            _needs_restart = True
        else:
            _needs_restart = False

    except Exception as e:
        log.exception('[app_manager] install error for %s', app_id)
        emit({'stage': 'error', 'percent': 0, 'message': 'Bład: ' + str(e), 'status': 'error'})
        _needs_restart = False
    finally:
        _task_done()
        if _needs_restart:
            _restart_server()


def _bg_uninstall(app_id, app_def, task_id, wipe_data=False):
    def emit(extra):
        _emit({'task_id': task_id, 'app_id': app_id, **extra})

    _task_start()
    try:
        emit({'stage': 'start', 'percent': 10, 'message': 'Odinstalowywanie ' + app_def['name'] + '...', 'status': 'running'})

        # Wywolaj wlasny endpoint uninstall
        uninstall_ep = app_def.get('uninstall_endpoint')
        if uninstall_ep and not app_def.get('simple') and _flask_app:
            emit({'stage': 'cleanup', 'percent': 30, 'message': 'Czyszczenie danych apki...', 'status': 'running'})
            try:
                with _flask_app.app_context():
                    with _flask_app.test_client() as tc:
                        resp = _internal_post(tc, uninstall_ep, json={'wipe_data': wipe_data})
                        if resp.status_code not in (200, 204):
                            log.warning('[app_manager] uninstall_endpoint %s returned %s', uninstall_ep, resp.status_code)
            except Exception as e:
                log.warning('[app_manager] uninstall_endpoint %s failed: %s', uninstall_ep, e)

        # Usun pliki jesli pobrane z GitHub
        inst = _load_installed().get(app_id, {})
        if inst.get('source') == 'github':
            emit({'stage': 'remove', 'percent': 60, 'message': 'Usuwanie plikow apki...', 'status': 'running'})
            fn = _get_frontend_filename(app_id)
            if fn:
                fp = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                if os.path.isfile(fp):
                    os.remove(fp)
            # Remove backend blueprint
            bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
            if bp_info:
                module_name = bp_info[0]
                bp_file = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                # Only delete if no other installed app uses same blueprint
                other_using_same = [
                    aid for aid, bpi in _OPTIONAL_BLUEPRINTS.items()
                    if bpi[0] == module_name and aid != app_id
                    and aid in _load_installed()
                ]
                if not other_using_same and os.path.isfile(bp_file):
                    os.remove(bp_file)
                    log.info('[app_manager] Removed backend blueprint: %s', module_name)
        else:
            emit({'stage': 'remove', 'percent': 60, 'message': 'Apka bundled - oznaczam jako odinstalowana', 'status': 'running'})

        _set_uninstalled(app_id)

        emit({'stage': 'sync', 'percent': 85, 'message': 'Synchronizacja...', 'status': 'running'})
        _sync_frontend_dist()

        emit({'stage': 'done', 'percent': 100, 'message': app_def['name'] + ' odinstalowano', 'status': 'done'})

    except Exception as e:
        log.exception('[app_manager] uninstall error for %s', app_id)
        emit({'stage': 'error', 'percent': 0, 'message': 'Bład: ' + str(e), 'status': 'error'})
    finally:
        _task_done()


# ─── Auth helper ──────────────────────────────────────────────

def _require_admin():
    if getattr(g, 'role', None) != 'admin':
        return jsonify({'error': 'Brak uprawnien (wymagane admin)'}), 403
    return None


# ─── Core apps info ───────────────────────────────────────────

_CORE_META = {
    'dashboard':        ('Dashboard',        'fa-tachometer-alt',    '#3b82f6', 'System',  'Przeglad systemu'),
    'file-manager':     ('File Manager',     'fa-folder-open',       '#f59e0b', 'System',  'Przegladanie i zarzadzanie plikami'),
    'storage-manager':  ('Storage Manager',  'fa-hdd',               '#10b981', 'Storage', 'Dyski, partycje, montowanie'),
    'terminal':         ('Terminal',         'fa-terminal',          '#22c55e', 'System',  'Terminal przez przegladarke'),
    'system-settings':  ('Settings',         'fa-cog',               '#6b7280', 'System',  'Ustawienia systemowe NAS'),
    'users':            ('Users',            'fa-users',             '#6366f1', 'System',  'Zarzadzanie uzytkownikami'),
    'updates':          ('Updates',          'fa-cloud-download-alt','#8b5cf6', 'System',  'Aktualizacje systemu EthOS'),
    'app-store':        ('App Manager',      'fa-th',                '#f97316', 'System',  'Zarzadzanie paczkami EthOS'),
    'packages':         ('Package Manager',  'fa-box',               '#0ea5e9', 'System',  'Zarzadzanie pakietami apt'),
    'event-log':        ('Event Log',        'fa-list-alt',          '#94a3b8', 'System',  'Logi i historia operacji'),
    'network':          ('Network',          'fa-network-wired',     '#0ea5e9', 'Network', 'Interfejsy sieciowe i WiFi'),
    'services':         ('Services',         'fa-server',            '#64748b', 'System',  'Zarzadzanie serwisami systemowymi'),
    'resource-monitor': ('Resource Monitor', 'fa-chart-area',        '#8b5cf6', 'System',  'CPU, RAM, dysk, siec - wykresy'),
    'backup':           ('Backup',           'fa-shield-alt',        '#06b6d4', 'Storage', 'Tworzenie i przywracanie backupow'),
    'power':            ('Power',            'fa-power-off',         '#22c55e', 'System',  'Harmonogram, WOL, spindown, governor'),
    'notifications':    ('Notifications',    'fa-bell',              '#f59e0b', 'System',  'Kanaly powiadomien'),
    'ssh-manager':      ('SSH Manager',      'fa-key',               '#10b981', 'Network', 'Zarzadzanie kluczami SSH'),
    'naslink':          ('NASLink',          'fa-link',              '#a78bfa', 'Network', 'Polaczenia miedzy urzadzeniami NAS'),
}


def get_core_apps_info():
    result = []
    for aid in sorted(CORE_APPS):
        meta = _CORE_META.get(aid, (aid, 'fa-cube', '#6b7280', 'System', ''))
        result.append({
            'id': aid,
            'name': meta[0],
            'icon': meta[1],
            'color': meta[2],
            'category': meta[3],
            'description': meta[4],
            'core': True,
            'installed': True,
            'installed_version': 'core',
            'installed_source': 'core',
        })
    return result


# ─── Endpoints ────────────────────────────────────────────────

@app_manager_bp.route('/catalog')
def get_catalog_endpoint():
    if not getattr(g, 'role', None):
        return jsonify({'error': 'Unauthorized'}), 401

    force = request.args.get('refresh') == '1'
    catalog = _get_catalog(force_refresh=force)
    installed = _load_installed()

    result = []
    for app in catalog:
        if app['id'] in CORE_APPS:
            continue
        inst = installed.get(app['id'], {})
        item = dict(app)
        item['installed'] = app['id'] in installed
        item['installed_version'] = inst.get('version', '')
        item['installed_source'] = inst.get('source', '')
        item['installed_at'] = inst.get('installed_at', '')
        item['core'] = False
        item['update_available'] = (
            item['installed']
            and item['installed_version'] not in ('bundled', 'core', app.get('version', ''))
            and app.get('version', '') > item['installed_version']
        )
        result.append(item)

    return jsonify({'optional': result, 'core': get_core_apps_info()})


@app_manager_bp.route('/catalog/refresh', methods=['POST'])
def refresh_catalog():
    err = _require_admin()
    if err:
        return err
    apps = _get_catalog(force_refresh=True)
    return jsonify({'ok': True, 'count': len(apps)})


@app_manager_bp.route('/installed')
def get_installed():
    if not getattr(g, 'role', None):
        return jsonify({'error': 'Unauthorized'}), 401
    installed = _load_installed()
    catalog = _get_catalog()
    cat_by_id = {a['id']: a for a in catalog}
    result = []
    for app_id, inst in installed.items():
        app = cat_by_id.get(app_id, {'id': app_id, 'name': app_id})
        result.append({
            **app,
            'installed': True,
            'installed_version': inst.get('version', 'bundled'),
            'installed_source': inst.get('source', 'bundled'),
            'installed_at': inst.get('installed_at', ''),
            'core': app_id in CORE_APPS,
        })
    return jsonify(result)


@app_manager_bp.route('/core')
def get_core():
    if not getattr(g, 'role', None):
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(get_core_apps_info())


@app_manager_bp.route('/check-updates')
def check_updates():
    if not getattr(g, 'role', None):
        return jsonify({'error': 'Unauthorized'}), 401
    catalog = _get_catalog(force_refresh=True)
    installed = _load_installed()
    updates = []
    for app in catalog:
        inst = installed.get(app['id'])
        if inst and inst.get('version') not in ('bundled', 'core', app.get('version', '')):
            if app.get('version', '') > inst.get('version', ''):
                updates.append({
                    'id': app['id'],
                    'name': app.get('name', app['id']),
                    'current_version': inst['version'],
                    'latest_version': app['version'],
                })
    return jsonify(updates)


@app_manager_bp.route('/<app_id>/install', methods=['POST'])
def install_app(app_id):
    err = _require_admin()
    if err:
        return err
    if app_id in CORE_APPS:
        # Allow install if backend .py is missing (stripped by builder)
        bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
        if bp_info:
            bp_file = os.path.join(_BLUEPRINTS_DIR, bp_info[0] + '.py')
            if os.path.isfile(bp_file):
                return jsonify({'error': 'Core apps nie wymagaja instalacji'}), 400
        else:
            return jsonify({'error': 'Core apps nie wymagaja instalacji'}), 400

    catalog = _get_catalog()
    app_def = next((a for a in catalog if a['id'] == app_id), None)
    if not app_def:
        return jsonify({'error': 'Nieznana apka: ' + app_id}), 404

    task_id = str(uuid.uuid4())[:8]
    from gevent import spawn
    spawn(_bg_install, app_id, app_def, task_id)
    return jsonify({'ok': True, 'task_id': task_id})


@app_manager_bp.route('/<app_id>/uninstall', methods=['POST'])
def uninstall_app(app_id):
    err = _require_admin()
    if err:
        return err
    if app_id in CORE_APPS:
        return jsonify({'error': 'Nie mozna odinstalowac core app'}), 400

    catalog = _get_catalog()
    app_def = next((a for a in catalog if a['id'] == app_id), None)
    if not app_def:
        return jsonify({'error': 'Nieznana apka: ' + app_id}), 404

    body = request.get_json(silent=True) or {}
    wipe_data = bool(body.get('wipe_data', False))

    task_id = str(uuid.uuid4())[:8]
    from gevent import spawn
    spawn(_bg_uninstall, app_id, app_def, task_id, wipe_data)
    return jsonify({'ok': True, 'task_id': task_id})


@app_manager_bp.route('/<app_id>/update', methods=['POST'])
def update_app(app_id):
    err = _require_admin()
    if err:
        return err
    if app_id in CORE_APPS:
        return jsonify({'error': 'Aktualizacje core apps przez OTA'}), 400

    catalog = _get_catalog(force_refresh=True)
    app_def = next((a for a in catalog if a['id'] == app_id), None)
    if not app_def:
        return jsonify({'error': 'Nieznana apka: ' + app_id}), 404

    task_id = str(uuid.uuid4())[:8]
    from gevent import spawn
    spawn(_bg_install, app_id, app_def, task_id)
    return jsonify({'ok': True, 'task_id': task_id})


@app_manager_bp.route('/<app_id>/status')
def app_status(app_id):
    if not getattr(g, 'role', None):
        return jsonify({'error': 'Unauthorized'}), 401
    installed = _load_installed()
    inst = installed.get(app_id)
    if inst:
        return jsonify({'installed': True, **inst, 'core': app_id in CORE_APPS})
    return jsonify({'installed': False, 'core': app_id in CORE_APPS})
