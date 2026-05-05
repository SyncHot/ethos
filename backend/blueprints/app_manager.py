"""
EthOS - App Manager (Package Center)

Zarządza opcjonalnymi paczkami EthOS: install/uninstall/update z GitHub catalog.
Obsługuje wiele źródeł katalogu (GitHub repos + custom URLs).

Endpoints:
  GET  /api/app-manager/catalog            -> pelny katalog z statusem instalacji
  POST /api/app-manager/catalog/refresh    -> wymusz odswiazenie z GitHub
  GET  /api/app-manager/installed          -> tylko zainstalowane apki
  GET  /api/app-manager/core               -> lista core apps
  GET  /api/app-manager/check-updates      -> sprawdz aktualizacje (catalog/GitHub)
  GET  /api/app-manager/catalog-sources    -> lista zrodel katalogu
  POST /api/app-manager/catalog-sources    -> dodaj nowe zrodlo
  PUT  /api/app-manager/catalog-sources/<id> -> edytuj zrodlo
  DELETE /api/app-manager/catalog-sources/<id> -> usun zrodlo
  GET  /api/app-manager/app-update-config  -> pobierz konfiguracje zrodla aktualizacji apek
  PUT  /api/app-manager/app-update-config  -> zapisz konfiguracje (source, github_repo)
  POST /api/app-manager/check-app-updates  -> sprawdz aktualizacje (GitHub lub OTA)
  POST /api/app-manager/update-apps        -> batch update {app_ids:[...]}
  POST /api/app-manager/<id>/install       -> zainstaluj paczke (async)
  POST /api/app-manager/<id>/uninstall     -> odinstaluj paczke
  POST /api/app-manager/<id>/update        -> zaktualizuj do najnowszej wersji
  GET  /api/app-manager/<id>/status        -> status instalacji paczki
  GET  /api/app-manager/running-tasks      -> aktywne zadania (reconnect recovery)

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
import gevent

from flask import Blueprint, request, jsonify, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, host_run_stream, data_path, app_path, q, _apt_exec, apt_install as _host_apt_install

log = logging.getLogger('app_manager')
app_manager_bp = Blueprint('app_manager', __name__, url_prefix='/api/app-manager')

# ─── SocketIO ref ────────────────────────────────────────────

_socketio = None
_flask_app = None
# Maps task_id → last known event data for reconnect recovery
_running_task_state = {}
_running_task_lock = threading.Lock()


def init_app_manager(sio):
    global _socketio
    _socketio = sio
    # Auto-repair missing files for "installed" apps in background
    try:
        from gevent import spawn_later
        spawn_later(15, _repair_missing_app_files)
    except Exception:
        pass


@app_manager_bp.record_once
def _on_register(state):
    global _flask_app
    _flask_app = state.app


def _emit(event_data):
    if _socketio:
        try:
            _socketio.emit('app_manager_progress', event_data)
        except Exception as exc:
            log.error('[_emit] emit failed: %s', exc)
    # Track last known state per task for reconnect recovery
    task_id = event_data.get('task_id')
    if task_id:
        with _running_task_lock:
            if event_data.get('status') in ('done', 'error'):
                _running_task_state.pop(task_id, None)
            else:
                _running_task_state[task_id] = {**event_data, '_ts': time.time()}


def _stream_with_keepalive(cmd, emit_fn, stage, percent, interval=4):
    """Iterate host_run_stream lines, emitting a keepalive if no output for `interval` seconds.

    Used to prevent the progress bar getting stuck during silent operations
    (e.g. apt-get update reading package lists, pip resolving dependencies).
    Returns (exit_code, last_error_line).
    """
    exit_code = -1
    last_err = ''
    last_emit = time.time()
    keepalive_msg = None

    for line in host_run_stream(cmd):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            exit_code = int(stripped.split(':', 1)[1])
            break

        # Keep the last error line for reporting
        lower = stripped.lower()
        if lower and ('error' in lower or 'e:' in lower or 'err' in lower):
            last_err = stripped

        # Keepalive: if no emit in `interval` seconds, send a heartbeat
        now = time.time()
        if now - last_emit >= interval and stripped:
            emit_fn({'stage': stage, 'message': stripped[:80], 'percent': percent, 'status': 'running'})
            last_emit = now
            keepalive_msg = stripped

        yield stripped, last_err

    return exit_code


# ─── Paths ───────────────────────────────────────────────────

_ETHOS_ROOT = app_path()
_FRONTEND_APPS_DIR = os.path.join(_ETHOS_ROOT, 'frontend', 'js', 'apps')
_BLUEPRINTS_DIR = os.path.join(_ETHOS_ROOT, 'backend', 'blueprints')

INSTALLED_FILE = data_path('installed_apps.json')
APP_UPDATE_CONFIG_FILE = data_path('app_update_config.json')
CATALOG_SOURCES_FILE = data_path('catalog_sources.json')
CATALOG_CACHE_FILE = '/tmp/ethos_app_catalog.json'
CATALOG_CACHE_TTL = 3600 * 6

DEFAULT_GITHUB_REPO = 'SyncHot/ethos-os-ethos-apps'
GITHUB_CATALOG_URL = f'https://raw.githubusercontent.com/{DEFAULT_GITHUB_REPO}/main/catalog.json'
GITHUB_APP_BASE    = f'https://raw.githubusercontent.com/{DEFAULT_GITHUB_REPO}/main/apps'

# ─── Core Apps (wbudowane, nieusuwalne) ──────────────────────

CORE_APPS = frozenset({
    'dashboard', 'file-manager', 'storage-manager', 'terminal',
    'system-settings', 'users', 'updates', 'app-store',
    'event-log', 'network', 'resource-monitor', 'backup',
    'power', 'notifications', 'ssh-manager',
    'firewall', 'fail2ban', 'security-advisor',
})

# ─── Frontend filename map ────────────────────────────────────

_FRONTEND_FILENAME = {
    'ai-chat':          'aichat',
    'disk-repair':      'storage',
    'doc-anonymizer':   'doc-anonymizer',
    'med-assistant':    'med-assistant',
    'doc-editor':       'editor',
    'download-manager': 'downloads',
    'domains-manager':  'domains',
    'family-hub':       'familyhub',
    'raid-lvm':         'storage',
    'ssh-manager':      'ssh',
    'sticky-notes':     'stickynotes',
    'storage-manager':  'storage',
    'usb-flasher':      'flasher',
    'resource-monitor': 'resources',
    'cloud-backup':     'cloud-backup',
    'code-editor':      'code-editor',
    'sharing-samba':    'storage',
    'sharing-nfs':      'storage',
    'sharing-dlna':     'storage',
    'sharing-webdav':   'storage',
    'sharing-sftp':     'storage',
    'sharing-ftp':      'storage',
    # Rozdzielone z apps.js na osobne pliki
    'file-manager':     'file-manager',
    'docker-manager':   'docker-manager',
    'vm-manager':       'vm-manager',
    'event-log':        'event-log',
    'app-store':        'app-store',
    'remote-log':       'remote-log',
    'system-settings':  'system-settings',
    'security-advisor': 'security_advisor',
    'photos-ai':        'photos_ai',
    'video-station':    'video_station',
    'radio-music':      'radio_music',
    'packages':         'packages',
    'services':         'services',
    'naslink':          'naslink',
}

# Maps app_id → list of EXTRA JS filenames (without .js) beyond the primary.
# The primary is already determined by _FRONTEND_FILENAME / app_id convention.
# Extra files are published as frontend_2.js, frontend_3.js, … on GitHub,
# and downloaded alongside the primary during App Store install.
_FRONTEND_EXTRA_FILES: dict = {
    # 'gallery': ['gallery_lightbox', 'gallery_people'],  # example
}

# Maps app_id → list of extra backend module filenames (without .py).
# Primary backend is always bp_info[0] from _OPTIONAL_BLUEPRINTS.
# Extras are published as backend_2.py, backend_3.py … on GitHub,
# and downloaded/removed/updated alongside the primary during App Store ops.
_BACKEND_EXTRA_FILES: dict = {
    'video-station': ['video_station_library', 'video_station_streaming', 'video_station_thumbnails', 'video_station_tmdb', 'video_station_extras'],
    'download-manager': ['downloads_config', 'downloads_debrid', 'downloads_extract', 'downloads_history'],
    'vm-manager': ['vm_boot', 'vm_disks', 'vm_network', 'vm_console'],
    'radio-music': ['radio_music_radio', 'radio_music_podcasts', 'radio_music_youtube', 'radio_music_local', 'radio_music_playlist'],
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
    'med-assistant':   ('med_assistant',   'med_assistant_bp',  None,               True),
    'gallery':         ('gallery',         'gallery_bp',        None,                False),
    'download-manager':('downloads',       'downloads_bp',     'init_downloads',    True),
    'printer':         ('printer',         'printer_bp',        None,                False),
    'docker-manager':  ('docker_manager',  'docker_bp',         None,                True),
    'vm-manager':      ('vm_manager',      'vm_bp',             None,                True),
    'doc-editor':      ('editor',          'editor_bp',         None,                False),
    'usb-flasher':     ('flasher',         'flasher_bp',        None,                False),
    'builder':         ('builder',         'builder_bp',        'init_builder',      True),
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

    'cron':            ('cron_manager',    'cron_bp',           None,                False),
    'ups':             ('ups',             'ups_bp',           'init_ups',          False),
    'family-hub':      ('familyhub',       'familyhub_bp',      None,                False),
    'sticky-notes':    ('stickynotes',     'notes_bp',          None,                False),
    'photos-ai':       ('photos_ai',       'photos_ai_bp',      None,                True),
    'video-station':   ('video_station',   'video_station_bp',  '_start_hls_cleanup_loop', True),
    'radio-music':     ('radio_music',     'radio_music_bp',    None,                True),
    'packages':        ('packages',        'packages_bp',       None,                False),
    'ldap':            ('ldap_auth',       'ldap_bp',           None,                False),
    'sync-drive':      ('sync_drive',      'sync_drive_bp',    'init_sync_drive',   True),
    'mail-server':     ('mail_server',     'mail_bp',          'init_mail_server',  True),
}

# Public alias
OPTIONAL_BLUEPRINTS = _OPTIONAL_BLUEPRINTS

# ─── Built-in catalog (fallback gdy GitHub niedostepny) ──────

BUILTIN_CATALOG = [
    {
        'id': 'surveillance', 'name': 'Surveillance', 'version': '1.0.2',
        'icon': 'fa-video', 'color': '#dc2626', 'category': 'Security', 'admin_only': False,
        'description': 'Monitoring IP kamer z detekcja ruchu i podgladem na zywo.',
        'apt_deps': ['ffmpeg'], 'pip_deps': ['onvif-zeep'],
        'install_endpoint': '/api/surveillance/install',
        'uninstall_endpoint': '/api/surveillance/uninstall',
        'status_endpoint': '/api/surveillance/status',
    },
    {
        'id': 'ai-chat', 'name': 'AI Assistant', 'version': '1.0.3',
        'icon': 'fa-robot', 'color': '#8b5cf6', 'category': 'Tools', 'admin_only': False,
        'description': 'Asystent AI z obsługą GPT, Claude i lokalnych modeli LLM.',
        'apt_deps': [], 'pip_deps': ['openai', 'anthropic', 'huggingface_hub'],
        'install_endpoint': '/api/aichat/install',
        'uninstall_endpoint': '/api/aichat/uninstall',
        'status_endpoint': '/api/aichat/status',
    },
    {
        'id': 'doc-anonymizer', 'name': 'Document Anonymizer', 'version': '1.1.4',
        'icon': 'fa-user-shield', 'color': '#0ea5e9', 'category': 'Tools', 'admin_only': False,
        'description': 'Anonimizacja dokumentow medycznych PDF/DOCX przy uzyciu polskiego modelu Bielik LLM.',
        'apt_deps': ['poppler-utils', 'tesseract-ocr', 'tesseract-ocr-pol'],
        'pip_deps': ['PyMuPDF', 'PyPDF2', 'Pillow', 'pytesseract', 'python-docx', 'reportlab'],
        'depends_on': ['ai-chat'],
        'install_endpoint': '/api/doc-anonymizer/install',
        'uninstall_endpoint': '/api/doc-anonymizer/uninstall',
        'status_endpoint': '/api/doc-anonymizer/pkg-status',
    },
    {
        'id': 'med-assistant', 'name': 'Medical Assistant', 'version': '0.0.4',
        'icon': 'fa-user-md', 'color': '#06b6d4', 'category': 'Tools', 'admin_only': False,
        'description': 'Asystent medyczny -- analiza dokumentacji, interakcje lekowe, skierowania, wytyczne ESC.',
        'apt_deps': ['poppler-utils', 'tesseract-ocr', 'tesseract-ocr-pol'],
        'pip_deps': ['PyMuPDF', 'PyPDF2', 'Pillow', 'pytesseract', 'python-docx'],
        'depends_on': ['ai-chat'],
        'install_endpoint': '/api/med-assistant/install',
        'uninstall_endpoint': '/api/med-assistant/uninstall',
        'status_endpoint': '/api/med-assistant/pkg-status',
    },
    {
        'id': 'gallery', 'name': 'Gallery', 'version': '1.0.4',
        'icon': 'fa-images', 'color': '#ec4899', 'category': 'Media', 'admin_only': False,
        'description': 'Galeria zdjec i filmow z EXIF, miniaturkami i haslami folderow.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/gallery/install',
        'uninstall_endpoint': '/api/gallery/uninstall',
        'status_endpoint': '/api/gallery/pkg-status',
    },
    {
        'id': 'download-manager', 'name': 'Download Manager', 'version': '1.0.3',
        'icon': 'fa-cloud-download-alt', 'color': '#10b981', 'category': 'Tools', 'admin_only': False,
        'description': 'Pobieranie plikow z HTTP, torrent, magnet i serwisow premium.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/downloads/install',
        'uninstall_endpoint': '/api/downloads/uninstall',
        'status_endpoint': '/api/downloads/pkg-status',
    },
    {
        'id': 'printer', 'name': 'Print Server', 'version': '1.0.2',
        'icon': 'fa-print', 'color': '#ef4444', 'category': 'Tools', 'admin_only': True,
        'description': 'Serwer drukowania z automatycznym wykrywaniem drukarek i konwersja PDF.',
        'apt_deps': ['cups', 'cups-browsed', 'libreoffice'], 'pip_deps': [],
        'install_endpoint': '/api/printer/install',
        'uninstall_endpoint': '/api/printer/uninstall',
        'status_endpoint': '/api/printer/pkg-status',
    },
    {
        'id': 'docker-manager', 'name': 'Docker Manager', 'version': '1.0.8',
        'icon': 'fa-cubes', 'color': '#2496ed', 'category': 'System', 'admin_only': True,
        'description': 'Zarządzanie kontenerami Docker, projektami Compose, obrazami i logami.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/docker/install',
        'uninstall_endpoint': '/api/docker/uninstall',
        'status_endpoint': '/api/docker/pkg-status',
    },
    {
        'id': 'vm-manager', 'name': 'VM Manager', 'version': '1.0.10',
        'icon': 'fa-desktop', 'color': '#8b5cf6', 'category': 'System', 'admin_only': True,
        'description': 'Maszyny wirtualne QEMU/KVM z migawkami i dostepem VNC.',
        'apt_deps': ['qemu-system-x86', 'qemu-utils', 'ovmf'], 'pip_deps': [],
        'install_endpoint': '/api/vm/install',
        'uninstall_endpoint': '/api/vm/uninstall',
        'status_endpoint': '/api/vm/pkg-status',
    },
    {
        'id': 'doc-editor', 'name': 'Documents', 'version': '1.0.2',
        'icon': 'fa-file-word', 'color': '#2563eb', 'category': 'Tools', 'admin_only': False,
        'description': 'Tworzenie i edycja dokumentow Word z eksportem do PDF.',
        'apt_deps': ['libreoffice'], 'pip_deps': ['mammoth', 'python-docx'],
        'install_endpoint': '/api/editor/install',
        'uninstall_endpoint': '/api/editor/uninstall',
        'status_endpoint': '/api/editor/pkg-status',
    },
    {
        'id': 'code-editor', 'name': 'Code Editor', 'version': '1.0.1',
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
        'id': 'usb-flasher', 'name': 'USB Creator', 'version': '1.0.2',
        'icon': 'fa-usb', 'color': '#a855f7', 'category': 'Tools', 'admin_only': True,
        'description': 'Flashowanie obrazow ISO/IMG na pendrive z monitoringiem postepu.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/flasher/install',
        'uninstall_endpoint': '/api/flasher/uninstall',
        'status_endpoint': '/api/flasher/pkg-status',
    },
    {
        'id': 'builder', 'name': 'Builder', 'version': '1.0.21',
        'icon': 'fa-hammer', 'color': '#f97316', 'category': 'System', 'admin_only': True,
        'description': 'Budowanie wydan EthOS i obrazow systemowych przez interfejs webowy.',
        'apt_deps': ['squashfs-tools', 'genisoimage', 'rsync'], 'pip_deps': [],
        'install_endpoint': '/api/builder/install',
        'uninstall_endpoint': '/api/builder/uninstall',
        'status_endpoint': '/api/builder/pkg-status',
    },
    {
        'id': 'disk-repair', 'name': 'Disk Repair', 'version': '1.0.20',
        'icon': 'fa-wrench', 'color': '#ef4444', 'category': 'Storage', 'admin_only': True,
        'description': 'Diagnostyka SMART i sprawdzanie systemu plikow z narzedziami naprawczymi.',
        'apt_deps': ['smartmontools', 'e2fsprogs'], 'pip_deps': [],
        'install_endpoint': '/api/diskrepair/install',
        'uninstall_endpoint': '/api/diskrepair/uninstall',
        'status_endpoint': '/api/diskrepair/pkg-status',
        'hidden': True,
    },
    {
        'id': 'remote-log', 'name': 'Remote Logs', 'version': '1.0.2',
        'icon': 'fa-satellite-dish', 'color': '#0891b2', 'category': 'System', 'admin_only': True,
        'description': 'Wysylanie logow diagnostycznych na centralny serwer.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/remote-log/install',
        'uninstall_endpoint': '/api/remote-log/uninstall',
        'status_endpoint': '/api/remote-log/pkg-status',
    },
    {
        'id': 'sharing-samba', 'name': 'File Sharing (Samba)', 'version': '1.0.21',
        'icon': 'fa-windows', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Udostepnianie plikow przez siec (Windows, Mac, Linux).',
        'apt_deps': ['samba'], 'pip_deps': [],
        'install_endpoint': '/api/storage/samba/pkg-install',
        'uninstall_endpoint': '/api/storage/samba/pkg-uninstall',
        'status_endpoint': '/api/storage/samba/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-nfs', 'name': 'NFS', 'version': '1.0.20',
        'icon': 'fa-network-wired', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Szybkie udostepnianie plikow dla Linux/Unix przez NFS.',
        'apt_deps': ['nfs-kernel-server'], 'pip_deps': [],
        'install_endpoint': '/api/storage/nfs/pkg-install',
        'uninstall_endpoint': '/api/storage/nfs/pkg-uninstall',
        'status_endpoint': '/api/storage/nfs/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-dlna', 'name': 'DLNA (MiniDLNA)', 'version': '1.0.21',
        'icon': 'fa-photo-video', 'color': '#6366f1', 'category': 'Media', 'admin_only': True,
        'description': 'Serwer DLNA do strumieniowania multimediow na TV i odtwarzacze.',
        'apt_deps': ['minidlna'], 'pip_deps': [],
        'install_endpoint': '/api/storage/dlna/pkg-install',
        'uninstall_endpoint': '/api/storage/dlna/pkg-uninstall',
        'status_endpoint': '/api/storage/dlna/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-webdav', 'name': 'WebDAV', 'version': '1.0.20',
        'icon': 'fa-globe', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Serwer WebDAV z dostepem do plikow przez HTTP.',
        'apt_deps': ['lighttpd'], 'pip_deps': [],
        'install_endpoint': '/api/storage/webdav/pkg-install',
        'uninstall_endpoint': '/api/storage/webdav/pkg-uninstall',
        'status_endpoint': '/api/storage/webdav/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-sftp', 'name': 'SFTP', 'version': '1.0.20',
        'icon': 'fa-lock', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Bezpieczny transfer plikow przez SSH.',
        'apt_deps': ['openssh-server'], 'pip_deps': [],
        'install_endpoint': '/api/storage/sftp/pkg-install',
        'uninstall_endpoint': '/api/storage/sftp/pkg-uninstall',
        'status_endpoint': '/api/storage/sftp/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-ftp', 'name': 'FTP', 'version': '1.0.20',
        'icon': 'fa-upload', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Klasyczny serwer FTP z obsługa vsftpd.',
        'apt_deps': ['vsftpd'], 'pip_deps': [],
        'install_endpoint': '/api/storage/ftp/pkg-install',
        'uninstall_endpoint': '/api/storage/ftp/pkg-uninstall',
        'status_endpoint': '/api/storage/ftp/pkg-status',
        'hidden': True,
    },
    {
        'id': 'domains-manager', 'name': 'Domains & SSL', 'version': '1.0.2',
        'icon': 'fa-globe', 'color': '#059669', 'category': 'Network', 'admin_only': True,
        'description': 'Domeny z certyfikatami SSL, reverse proxy i Dynamic DNS.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/ddns/install',
        'uninstall_endpoint': '/api/ddns/uninstall',
        'status_endpoint': '/api/ddns/pkg-status',
    },
    {
        'id': 'websites', 'name': 'Websites', 'version': '1.0.2',
        'icon': 'fa-globe-americas', 'color': '#14b8a6', 'category': 'Tools', 'admin_only': False,
        'description': 'Kreator stron z CMS, szablonami i edytorem wizualnym.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/websites/install',
        'uninstall_endpoint': '/api/websites/uninstall',
        'status_endpoint': '/api/websites/pkg-status',
    },
    {
        'id': 'cloud-backup', 'name': 'Cloud Backup', 'version': '1.0.4',
        'icon': 'fa-cloud-upload-alt', 'color': '#0ea5e9', 'category': 'Storage', 'admin_only': True,
        'description': 'Backup do S3, Backblaze, Google Drive, WebDAV i SFTP z harmonogramem.',
        'apt_deps': ['rclone'], 'pip_deps': [],
        'install_endpoint': '/api/cloud-backup/install',
        'uninstall_endpoint': '/api/cloud-backup/uninstall',
        'status_endpoint': '/api/cloud-backup/pkg-status',
    },
    {
        'id': 'raid-lvm', 'name': 'RAID / LVM', 'version': '1.0.20',
        'icon': 'fa-layer-group', 'color': '#f59e0b', 'category': 'Storage', 'admin_only': True,
        'description': 'Macierze RAID z mdadm i wolumeny LVM.',
        'apt_deps': ['mdadm', 'lvm2'], 'pip_deps': [],
        'install_endpoint': '/api/raid/install',
        'uninstall_endpoint': '/api/raid/uninstall',
        'status_endpoint': '/api/raid/pkg-status',
        'hidden': True,
    },
    {
        'id': 'wireguard', 'name': 'VPN (WireGuard)', 'version': '1.0.2',
        'icon': 'fa-shield-halved', 'color': '#7c3aed', 'category': 'Network', 'admin_only': True,
        'description': 'Serwer VPN WireGuard z peerami i kodami QR.',
        'apt_deps': ['wireguard', 'wireguard-tools', 'qrencode'], 'pip_deps': [],
        'install_endpoint': '/api/wireguard/install',
        'uninstall_endpoint': '/api/wireguard/uninstall',
        'status_endpoint': '/api/wireguard/pkg-status',
    },
    {
        'id': 'antivirus', 'name': 'Antivirus (ClamAV)', 'version': '1.0.4',
        'icon': 'fa-shield-virus', 'color': '#16a34a', 'category': 'Security', 'admin_only': True,
        'description': 'ClamAV antywirus — skanowanie na zadanie i zaplanowane (tryb daemon).',
        'apt_deps': ['clamav', 'clamav-freshclam', 'clamav-daemon', 'clamdscan'], 'pip_deps': [],
        'install_endpoint': '/api/antivirus/install',
        'uninstall_endpoint': '/api/antivirus/uninstall',
        'status_endpoint': '/api/antivirus/pkg-status',
    },
    {
        'id': 'security-advisor', 'name': 'Security Advisor', 'version': '0.0.3',
        'icon': 'fa-user-shield', 'color': '#059669', 'category': 'Security', 'admin_only': True,
        'description': 'Skaner bezpieczenstwa systemu z wynikiem 0-100 i automatycznymi poprawkami.',
        'apt_deps': [], 'pip_deps': [], 'simple': True, 'core': True,
        'status_endpoint': '/api/security-advisor/pkg-status',
    },
    {
        'id': 'photos-ai', 'name': 'Photos AI', 'version': '0.0.9',
        'icon': 'fa-brain', 'color': '#8b5cf6', 'category': 'Media', 'admin_only': False,
        'description': 'Rozpoznawanie twarzy, wykrywanie obiektow i inteligentne albumy dla Galerii.',
        'apt_deps': ['cmake', 'libopenblas-dev'], 'pip_deps': ['face_recognition', 'onnxruntime', 'scipy'],
        'install_endpoint': '/api/photos-ai/install',
        'uninstall_endpoint': '/api/photos-ai/uninstall',
        'status_endpoint': '/api/photos-ai/pkg-status',
        'hidden': True,
    },
    {
        'id': 'rollback', 'name': 'Rollback', 'version': '1.0.2',
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
        'id': 'cron', 'name': 'Scheduler', 'version': '1.0.2',
        'icon': 'fa-clock', 'color': '#6366f1', 'category': 'System', 'admin_only': True,
        'description': 'Harmonogram zadan z zarządzaniem cron jobs.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'ups', 'name': 'UPS', 'version': '1.0.4',
        'icon': 'fa-battery-full', 'color': '#f59e0b', 'category': 'System', 'admin_only': True,
        'description': 'Status baterii UPS i zarządzanie bezpiecznym wyłączeniem.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'family-hub', 'name': 'Family Hub', 'version': '1.0.2',
        'icon': 'fa-house-user', 'color': '#f472b6', 'category': 'Tools', 'admin_only': False,
        'description': 'Tablica ogloszen, listy zakupow, zadania i kalendarz rodzinny.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'sticky-notes', 'name': 'Sticky Notes', 'version': '1.0.2',
        'icon': 'fa-sticky-note', 'color': '#fbbf24', 'category': 'Tools', 'admin_only': False,
        'description': 'Szybkie notatki przyklejane do pulpitu.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'tickets', 'name': 'Tickets', 'version': '1.0.2',
        'icon': 'fa-tasks', 'color': '#06b6d4', 'category': 'Tools', 'admin_only': False,
        'description': 'Kanban — zarządzanie projektami i zadaniami.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'video-station', 'name': 'Video Station', 'version': '0.0.12',
        'icon': 'fa-film', 'color': '#7c3aed', 'category': 'Media', 'admin_only': False,
        'description': 'Biblioteka filmow z miniaturkami, streamingiem i sledzeniem postepu.',
        'apt_deps': ['ffmpeg'], 'pip_deps': [],
        'install_endpoint': '/api/video-station/install',
        'uninstall_endpoint': '/api/video-station/uninstall',
        'status_endpoint': '/api/video-station/pkg-status',
    },
    {
        'id': 'radio-music', 'name': 'Radio & Music', 'version': '0.0.9',
        'icon': 'fa-broadcast-tower', 'color': '#10b981', 'category': 'Media', 'admin_only': False,
        'description': 'Radio internetowe z całego świata, podcasty i odtwarzacz muzyki.',
        'apt_deps': ['ffmpeg'], 'pip_deps': ['yt-dlp'],
        'install_endpoint': '/api/radio-music/install',
        'uninstall_endpoint': '/api/radio-music/uninstall',
        'status_endpoint': '/api/radio-music/pkg-status',
    },
    {
        'id': 'packages', 'name': 'Package Manager', 'version': '1.0.0',
        'icon': 'fa-store', 'color': '#a855f7', 'category': 'System', 'admin_only': True,
        'description': 'Menedzer pakietow systemowych (legacy).',
        'apt_deps': [], 'pip_deps': [],
    },
    {
        'id': 'services', 'name': 'Services', 'version': '1.0.0',
        'icon': 'fa-cogs', 'color': '#64748b', 'category': 'System', 'admin_only': True,
        'description': 'Zarzadzanie uslugami systemowymi.',
        'apt_deps': [], 'pip_deps': [],
    },
    {
        'id': 'naslink', 'name': 'NASLink', 'version': '1.0.0',
        'icon': 'fa-network-wired', 'color': '#06b6d4', 'category': 'Network', 'admin_only': False,
        'description': 'Lacznosc i synchronizacja miedzy urzadzeniami NAS.',
        'apt_deps': [], 'pip_deps': [],
    },
    {
        'id': 'ldap', 'name': 'LDAP / Active Directory', 'version': '1.0.0',
        'icon': 'fa-sitemap', 'color': '#7c3aed', 'category': 'System', 'admin_only': True,
        'description': 'Integracja z LDAP i Active Directory dla centralnego zarzadzania uzytkownikami.',
        'apt_deps': [], 'pip_deps': ['ldap3'],
    },
    {
        'id': 'mail-server', 'name': 'Mail Server', 'version': '0.0.5',
        'icon': 'fa-envelope', 'color': '#3b82f6', 'category': 'Network', 'admin_only': True,
        'description': 'Serwer poczty (Postfix + Dovecot) z obsluga IMAP/SMTP, DKIM, relay i certyfikatow SSL.',
        'apt_deps': ['postfix', 'postfix-sqlite', 'dovecot-core', 'dovecot-imapd', 'dovecot-pop3d', 'dovecot-lmtpd', 'dovecot-sqlite', 'opendkim', 'opendkim-tools'],
        'pip_deps': [],
    },
]

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


def _set_installed(app_id, version, source='bundled', apt_deps=None, pip_deps=None):
    from datetime import datetime
    state = _load_installed()
    entry = {
        'version': version,
        'source': source,
        'installed_at': datetime.utcnow().isoformat(),
    }
    if apt_deps:
        entry['apt_deps'] = list(apt_deps)
    if pip_deps:
        entry['pip_deps'] = list(pip_deps)
    state[app_id] = entry
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


def _get_frontend_filenames(app_id):
    """Return list of ALL JS filenames (without .js) for an app.
    First element is the primary file; remaining are extras defined in
    _FRONTEND_EXTRA_FILES.  Returns [] when the app lives in apps.js (fn=None)."""
    primary = _get_frontend_filename(app_id)
    if primary is None:
        return []
    return [primary] + list(_FRONTEND_EXTRA_FILES.get(app_id, []))


def _get_backend_filenames(app_id):
    """Return list of ALL backend module names (without .py) for an app.
    First element is the primary module from _OPTIONAL_BLUEPRINTS; remaining
    are extras defined in _BACKEND_EXTRA_FILES.  Returns [] when the app has
    no optional blueprint."""
    bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
    if not bp_info:
        return []
    return [bp_info[0]] + list(_BACKEND_EXTRA_FILES.get(app_id, []))


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


# ─── Multi-source catalog ────────────────────────────────────

_DEFAULT_CATALOG_SOURCE = {
    'id': 'official',
    'name': 'EthOS Official',
    'type': 'github',
    'repo': DEFAULT_GITHUB_REPO,
    'enabled': True,
}


def _load_catalog_sources():
    """Load catalog sources list. Auto-creates default if missing."""
    try:
        if os.path.isfile(CATALOG_SOURCES_FILE):
            with open(CATALOG_SOURCES_FILE) as f:
                sources = json.load(f)
            if isinstance(sources, list) and sources:
                for s in sources:
                    s.setdefault('id', s.get('name', 'src').lower().replace(' ', '-'))
                    s.setdefault('enabled', True)
                return sources
    except Exception as e:
        log.warning('[app_manager] Error loading catalog sources: %s', e)
    return [dict(_DEFAULT_CATALOG_SOURCE)]


def _save_catalog_sources(sources):
    """Save catalog sources list."""
    tmp = CATALOG_SOURCES_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(sources, f, indent=2)
    os.replace(tmp, CATALOG_SOURCES_FILE)


def _fetch_catalog_from_source(source):
    """Fetch catalog.json from a single source. Returns list of app dicts or None."""
    src_type = source.get('type', 'github')
    try:
        if src_type == 'github':
            repo = source.get('repo', DEFAULT_GITHUB_REPO)
            url = f'https://raw.githubusercontent.com/{repo}/main/catalog.json'
        else:
            url = source.get('url', '')
            if not url:
                return None
            # Ensure URL points to catalog.json
            if not url.endswith('/catalog.json') and not url.endswith('.json'):
                url = url.rstrip('/') + '/catalog.json'

        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        apps = data.get('apps', data) if isinstance(data, dict) else data
        if not isinstance(apps, list):
            return None

        # Tag each app with its source info for download routing
        src_id = source.get('id', 'unknown')
        for app in apps:
            if isinstance(app, dict):
                app['_source_id'] = src_id
                if src_type == 'github':
                    app['_source_type'] = 'github'
                    app['_source_repo'] = source.get('repo', DEFAULT_GITHUB_REPO)
                else:
                    app['_source_type'] = 'url'
                    app['_source_url'] = url.rsplit('/catalog.json', 1)[0] if url.endswith('/catalog.json') else url.rsplit('/', 1)[0]
        return apps
    except Exception as e:
        log.debug('[app_manager] Catalog source %s (%s) unavailable: %s', source.get('name', '?'), src_type, e)
        return None


def _get_app_base_for_source(app):
    """Return the base URL for downloading app files, based on per-app source tags."""
    src_type = app.get('_source_type', 'github')
    if src_type == 'github':
        repo = app.get('_source_repo', DEFAULT_GITHUB_REPO)
        return f'https://raw.githubusercontent.com/{repo}/main/apps'
    elif src_type == 'url':
        return app.get('_source_url', '').rstrip('/') + '/apps'
    return GITHUB_APP_BASE


def _get_catalog(force_refresh=False):
    with _catalog_lock:
        cached = None if force_refresh else _load_catalog_cache()
        if cached is not None:
            return cached.get('apps', BUILTIN_CATALOG)

        sources = _load_catalog_sources()
        enabled = [s for s in sources if s.get('enabled', True)]
        builtin_by_id = {a['id']: a for a in BUILTIN_CATALOG}
        merged_by_id = {}

        # Start with builtin catalog as baseline
        for app in BUILTIN_CATALOG:
            merged_by_id[app['id']] = dict(app)

        # Layer each enabled source on top — later sources override earlier ones
        any_fetched = False
        for src in enabled:
            apps = _fetch_catalog_from_source(src)
            if apps is not None:
                any_fetched = True
                for app in apps:
                    if not isinstance(app, dict) or 'id' not in app:
                        continue
                    base = merged_by_id.get(app['id'], {}).copy()
                    base.update(app)
                    merged_by_id[app['id']] = base

        merged = list(merged_by_id.values())
        source_label = 'multi-source' if any_fetched else 'builtin'
        _save_catalog_cache({'apps': merged, 'source': source_label, 'fetched_at': time.time()})
        return merged


# ─── Install helpers ─────────────────────────────────────────

def _is_bundled(app_id):
    fns = _get_frontend_filenames(app_id)
    if not fns:
        return True  # lives in apps.js monolith
    return all(
        os.path.isfile(os.path.join(_FRONTEND_APPS_DIR, fn + '.js')) and
        os.path.getsize(os.path.join(_FRONTEND_APPS_DIR, fn + '.js')) > 0
        for fn in fns
    )


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


def _repair_missing_app_files():
    """Auto-repair: re-download missing frontend/backend files for installed apps."""
    try:
        installed = _load_installed()
        if not installed:
            return
        catalog = _get_catalog()
        catalog_map = {a['id']: a for a in catalog}
        repaired = []

        for app_id in list(installed):
            if app_id in CORE_APPS:
                continue
            fns = _get_frontend_filenames(app_id)
            if not fns:
                continue
            app_def = catalog_map.get(app_id, {})
            base_url = _get_app_base_for_source(app_def)
            # Check each frontend JS file (primary = frontend.js, extras = frontend_2.js, …)
            for idx, fn in enumerate(fns):
                remote_name = 'frontend.js' if idx == 0 else f'frontend_{idx + 1}.js'
                js_path = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                if not os.path.isfile(js_path):
                    url = base_url + '/' + app_id + '/' + remote_name
                    if _download_file(url, js_path):
                        repaired.append(f'{app_id}/{remote_name}')
                        dist_path = os.path.join(_ETHOS_ROOT, 'frontend_dist', 'js', 'apps', fn + '.js')
                        if os.path.isdir(os.path.dirname(dist_path)):
                            try:
                                import shutil
                                shutil.copy2(js_path, dist_path)
                            except Exception:
                                pass
            # Check backend .py (primary + extras)
            for idx, module_name in enumerate(_get_backend_filenames(app_id)):
                remote_name = 'backend.py' if idx == 0 else f'backend_{idx + 1}.py'
                bp_path = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                if not os.path.isfile(bp_path):
                    url = base_url + '/' + app_id + '/' + remote_name
                    if _download_file(url, bp_path):
                        repaired.append(f'{app_id}/{remote_name}')

        if repaired:
            log.info('[app_manager] Auto-repaired %d missing files: %s', len(repaired), ', '.join(repaired))
        else:
            log.debug('[app_manager] Integrity check OK — no missing files')
    except Exception as e:
        log.warning('[app_manager] Auto-repair error: %s', e)


def _get_github_app_base():
    """Get the GitHub base URL, respecting custom repo config. Fallback for non-sourced apps."""
    try:
        cfg = json.load(open(APP_UPDATE_CONFIG_FILE))
        repo = cfg.get('github_repo', DEFAULT_GITHUB_REPO)
    except Exception:
        repo = DEFAULT_GITHUB_REPO
    return f'https://raw.githubusercontent.com/{repo}/main/apps'


_MIN_FREE_MB = 300  # minimum free space on root before apt/pip install


def _semver_key(ver):
    """Return a sortable tuple for semver comparison (handles 1.10.0 > 1.9.0 correctly)."""
    try:
        parts = str(ver).split('.')
        return tuple(int(p) for p in (parts + ['0', '0', '0'])[:3])
    except Exception:
        return (0, 0, 0)


def _ensure_root_space(emit_fn):
    """Check root partition free space; proactively clean caches before install."""
    _PROACTIVE_CLEAN_MB = 600  # always clean caches if less than this
    try:
        st = os.statvfs('/')
        free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)

        if free_mb < _PROACTIVE_CLEAN_MB:
            log.info('[app_manager] Root space %.0f MB < %d MB — proactive cache cleanup', free_mb, _PROACTIVE_CLEAN_MB)
            emit_fn({'stage': 'cleanup', 'percent': 23,
                     'message': f'Czyszczenie cache ({free_mb:.0f} MB wolne)...',
                     'status': 'running'})

            host_run('apt-get clean 2>/dev/null', timeout=30)
            host_run('apt-get autoremove -y 2>/dev/null', timeout=60)
            host_run('rm -rf /root/.cache/pip /tmp/pip-* 2>/dev/null', timeout=10)
            # Remove stale __pycache__ from venv (safe, regenerated on import)
            venv_dir = os.path.join(os.environ.get('ETHOS_ROOT', '/opt/ethos'), 'venv')
            host_run(f'find {q(venv_dir)} -name __pycache__ -type d -exec rm -rf {{}} + 2>/dev/null',
                     timeout=30)

            st = os.statvfs('/')
            free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
            log.info('[app_manager] After cleanup: %.0f MB free', free_mb)
            emit_fn({'stage': 'cleanup', 'percent': 24,
                     'message': f'Po czyszczeniu: {free_mb:.0f} MB wolne',
                     'status': 'running'})

        if free_mb < _MIN_FREE_MB:
            emit_fn({'stage': 'error', 'percent': 0,
                     'message': f'Brak miejsca na dysku ({free_mb:.0f} MB wolne, potrzeba {_MIN_FREE_MB} MB). '
                                'Zwolnij miejsce na partycji root.',
                     'status': 'error'})
            return False

    except Exception as e:
        log.warning('[app_manager] Space check error: %s', e)
    return True


def _install_apt_deps(deps, emit_fn):
    """Install APT dependencies with streaming progress updates."""
    if not deps:
        return True

    # Filter out already-installed packages to avoid unnecessary apt-get update
    missing = []
    for pkg in deps:
        check = host_run(f'dpkg -l {q(pkg)} 2>/dev/null | grep -q "^ii"', timeout=10)
        if check.returncode != 0:
            missing.append(pkg)
    if not missing:
        log.info('[app_manager] All apt deps already installed: %s', deps)
        emit_fn({'stage': 'deps_apt', 'message': 'Pakiety apt juz zainstalowane', 'percent': 42, 'status': 'running'})
        return True

    pkgs = ' '.join(q(d) for d in missing)
    emit_fn({'stage': 'deps_apt', 'message': 'apt-get update...', 'percent': 25, 'status': 'running'})

    # Auto-recover from interrupted dpkg (common after power loss or killed installs)
    cmd = (
        f'DEBIAN_FRONTEND=noninteractive dpkg --configure -a 2>/dev/null; '
        f'DEBIAN_FRONTEND=noninteractive apt-get update -y -qq 2>/dev/null; '
        f'DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs} 2>&1'
    )
    lock_file = '/tmp/ethos-apt.lock'
    wrapped = f"flock -w 180 {q(lock_file)} bash -lc {q(cmd)}"

    exit_code = -1
    last_err = ''
    count = 0
    last_emit = time.time()
    for line in host_run_stream(wrapped):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            exit_code = int(stripped.split(':', 1)[1])
            break
        if not stripped:
            continue
        lower = stripped.lower()
        if 'e:' in lower or 'err' in lower:
            last_err = stripped
        # Emit on keywords OR as keepalive every 4s to prevent stuck progress bar
        now = time.time()
        is_keyword = any(kw in lower for kw in ('unpacking', 'setting up', 'installing', 'get:', 'fetched', 'reading', 'building'))
        if is_keyword or (now - last_emit >= 4):
            count += 1
            pct = min(40, 28 + count)
            emit_fn({'stage': 'deps_apt', 'message': stripped[:80], 'percent': pct, 'status': 'running'})
            last_emit = now

    if exit_code != 0:
        detail = last_err[:120] if last_err else f'exit code {exit_code}'
        log.error('[app_manager] apt install failed (rc=%s): %s', exit_code, detail)
        emit_fn({'stage': 'error', 'percent': 0,
                 'message': f'apt: {detail}', 'status': 'error'})
        return False
    emit_fn({'stage': 'deps_apt', 'message': 'Pakiety apt zainstalowane', 'percent': 42, 'status': 'running'})
    host_run('apt-get clean 2>/dev/null && apt-get autoremove -y 2>/dev/null', timeout=60)
    return True


def _install_pip_deps(deps, emit_fn):
    """Install pip dependencies with streaming progress updates."""
    if not deps:
        return True

    # Filter out already-installed pip packages
    venv = os.path.join(_ETHOS_ROOT, 'venv')
    pip = os.path.join(venv, 'bin', 'pip') if os.path.isdir(venv) else 'pip3'
    python = os.path.join(venv, 'bin', 'python') if os.path.isdir(venv) else 'python3'

    missing = []
    for pkg in deps:
        pkg_name = pkg.split('==')[0].split('>=')[0].split('<=')[0].strip()
        check = host_run(f'{q(python)} -c "import importlib; importlib.import_module({q(pkg_name.replace("-","_"))})" 2>/dev/null', timeout=10)
        if check.returncode != 0:
            # Also try pip show as fallback
            check2 = host_run(f'{q(pip)} show {q(pkg_name)} 2>/dev/null | grep -q "^Name:"', timeout=10)
            if check2.returncode != 0:
                missing.append(pkg)
    if not missing:
        log.info('[app_manager] All pip deps already installed: %s', deps)
        emit_fn({'stage': 'deps_pip', 'message': 'Pakiety pip juz zainstalowane', 'percent': 57, 'status': 'running'})
        return True

    pkgs = ' '.join(q(d) for d in missing)
    emit_fn({'stage': 'deps_pip', 'message': 'pip install: ' + ', '.join(missing), 'percent': 45, 'status': 'running'})

    cmd = q(pip) + ' install --progress-bar off ' + pkgs + ' 2>&1'

    exit_code = -1
    last_err = ''
    count = 0
    last_emit = time.time()
    for line in host_run_stream(cmd):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            exit_code = int(stripped.split(':', 1)[1])
            break
        if not stripped:
            continue
        lower = stripped.lower()
        if 'error' in lower:
            last_err = stripped
        # Emit on keywords OR as keepalive every 4s to prevent stuck progress bar
        now = time.time()
        is_keyword = any(kw in lower for kw in ('collecting', 'downloading', 'installing', 'building', 'successfully', 'obtaining'))
        if is_keyword or (now - last_emit >= 4):
            count += 1
            pct = min(55, 47 + count)
            emit_fn({'stage': 'deps_pip', 'message': stripped[:80], 'percent': pct, 'status': 'running'})
            last_emit = now

    if exit_code != 0:
        detail = last_err[:120] if last_err else f'exit code {exit_code}'
        log.error('[app_manager] pip install failed (rc=%s): %s', exit_code, detail)
        emit_fn({'stage': 'error', 'percent': 0,
                 'message': f'pip: {detail}', 'status': 'error'})
        return False
    emit_fn({'stage': 'deps_pip', 'message': 'Pakiety pip zainstalowane', 'percent': 57, 'status': 'running'})
    host_run(q(pip) + ' cache purge 2>/dev/null', timeout=30)
    return True


def _sync_frontend_dist():
    frontend = os.path.join(_ETHOS_ROOT, 'frontend')
    dist = os.path.join(_ETHOS_ROOT, 'frontend_dist')
    if os.path.isdir(dist):
        host_run('rsync -a --delete ' + q(frontend + '/') + ' ' + q(dist + '/'), timeout=60)
    # Invalidate the index.html cache so new/removed scripts are picked up
    import sys
    app_mod = sys.modules.get('app')
    if app_mod:
        cache = getattr(app_mod, '_INDEX_CACHE', None)
        if cache:
            cache['html'] = None


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
    _downloaded_frontend = None   # track newly downloaded files for cleanup on failure
    _downloaded_backend = []      # list of newly downloaded backend .py paths
    _task_start()
    try:
        emit({'stage': 'start', 'percent': 5, 'message': 'Instalowanie ' + app_def['name'] + '...', 'status': 'running'})

        # Determine source before downloading — was the app already on disk?
        _was_bundled = _is_bundled(app_id)
        app_base_url = _get_app_base_for_source(app_def)

        # Pobierz pliki z GitHub jesli nie ma na dysku
        if not _was_bundled:
            emit({'stage': 'download', 'percent': 10, 'message': 'Pobieranie pliku frontend...', 'status': 'running'})
            fns = _get_frontend_filenames(app_id)
            if fns:
                _downloaded_frontend = []
                for idx, fn in enumerate(fns):
                    remote_name = 'frontend.js' if idx == 0 else f'frontend_{idx + 1}.js'
                    url = app_base_url + '/' + app_id + '/' + remote_name
                    dest = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                    if not os.path.isfile(dest):
                        if not _download_file(url, dest):
                            emit({'stage': 'error', 'percent': 0, 'message': 'Bląd pobierania frontend', 'status': 'error'})
                            return
                        _downloaded_frontend.append(dest)
        else:
            emit({'stage': 'download', 'percent': 15, 'message': 'Pliki juz dostepne (bundled)', 'status': 'running'})

        # Download backend .py (primary + extras) from GitHub if not on disk
        # (Builder images keep frontend JS but remove optional backend .py)
        backend_modules = _get_backend_filenames(app_id)
        if backend_modules:
            for idx, module_name in enumerate(backend_modules):
                remote_name = 'backend.py' if idx == 0 else f'backend_{idx + 1}.py'
                bp_dest = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                if not os.path.isfile(bp_dest):
                    bp_url = app_base_url + '/' + app_id + '/' + remote_name
                    emit({'stage': 'download_backend', 'percent': 20, 'message': 'Pobieranie backend...', 'status': 'running'})
                    if not _download_file(bp_url, bp_dest):
                        emit({'stage': 'error', 'percent': 0, 'message': 'Bład pobierania backend — sprawdz połaczenie z internetem', 'status': 'error'})
                        return
                    _downloaded_backend.append(bp_dest)

        # Instalacja zaleznosci (apt: 25-42%, pip: 45-57%)
        apt_deps = app_def.get('apt_deps', [])
        pip_deps = app_def.get('pip_deps', [])

        if (apt_deps or pip_deps) and not _ensure_root_space(emit):
            return

        if apt_deps and not _install_apt_deps(apt_deps, emit):
            # Clean up freshly downloaded files — app isn't usable without its deps
            for p in (_downloaded_backend +
                      (_downloaded_frontend if isinstance(_downloaded_frontend, list) else
                       [_downloaded_frontend] if _downloaded_frontend else [])):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return

        if pip_deps and not _install_pip_deps(pip_deps, emit):
            for p in (_downloaded_backend +
                      (_downloaded_frontend if isinstance(_downloaded_frontend, list) else
                       [_downloaded_frontend] if _downloaded_frontend else [])):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return

        # Hot-load blueprint so its routes are available immediately
        emit({'stage': 'load', 'percent': 65, 'message': 'Ładowanie modułu...', 'status': 'running'})
        hot_ok = _hot_load_blueprint(app_id)
        emit({'stage': 'load', 'percent': 70, 'message': 'Moduł załadowany', 'status': 'running'})

        # Call app's install endpoint with timeout
        install_ep = app_def.get('install_endpoint')
        if install_ep and not app_def.get('simple') and _flask_app:
            emit({'stage': 'configure', 'percent': 75, 'message': 'Konfigurowanie apki...', 'status': 'running'})
            try:
                with gevent.Timeout(60, False):
                    with _flask_app.app_context():
                        with _flask_app.test_client() as tc:
                            resp = _internal_post(tc, install_ep)
                            if resp and resp.status_code >= 400:
                                log.warning('[app_manager] install_endpoint %s returned %s', install_ep, resp.status_code)
            except Exception as e:
                log.warning('[app_manager] install_endpoint %s failed: %s', install_ep, e)
            emit({'stage': 'configure', 'percent': 80, 'message': 'Konfiguracja zakonczona', 'status': 'running'})

        # Mark installed BEFORE frontend sync — rsync can disrupt SocketIO
        version = app_def.get('version', 'bundled')
        source = 'bundled' if _was_bundled else 'github'
        _set_installed(app_id, version, source,
                       apt_deps=app_def.get('apt_deps', []),
                       pip_deps=app_def.get('pip_deps', []))

        emit({'stage': 'done', 'percent': 100, 'message': app_def['name'] + ' zainstalowano pomyslnie', 'status': 'done'})

        # Notify all clients — enables hot-load without page refresh
        if _socketio:
            fns = _get_frontend_filenames(app_id)
            _socketio.emit('app_installed', {
                'id': app_id,
                'name': app_def.get('name', app_id),
                'icon': app_def.get('icon', 'fa-puzzle-piece'),
                'color': app_def.get('color', '#6b7280'),
                'category': app_def.get('category', 'Tools'),
                'description': app_def.get('description', ''),
                'admin_only': app_def.get('admin_only', False),
                'js_file': (fns[0] + '.js') if fns else None,
                'js_files': [fn + '.js' for fn in fns],
            })

        # Sync frontend_dist — non-critical cache sync, done after completion events
        gevent.sleep(0.1)
        try:
            _sync_frontend_dist()
        except Exception as e:
            log.warning('[app_manager] frontend sync error: %s', e)

        if not hot_ok:
            log.warning('[app_manager] Hot-load failed for %s, falling back to restart', app_id)
            _needs_restart = True
        else:
            _needs_restart = False

    except Exception as e:
        log.exception('[app_manager] install error for %s', app_id)
        try:
            emit({'stage': 'error', 'percent': 0, 'message': 'Bład: ' + str(e), 'status': 'error'})
        except Exception:
            pass
        _needs_restart = False
    finally:
        _task_done()
        if _needs_restart:
            _restart_server()


def _get_orphan_deps(app_id, app_def):
    """Return (apt_orphans, pip_orphans) — deps not needed by any other installed app."""
    installed = _load_installed()
    app_apt = set(app_def.get('apt_deps', []))
    app_pip = set(app_def.get('pip_deps', []))
    # Also check deps stored in installed_apps.json from the app being uninstalled
    stored = installed.get(app_id, {})
    app_apt |= set(stored.get('apt_deps', []))
    app_pip |= set(stored.get('pip_deps', []))
    if not app_apt and not app_pip:
        return [], []

    # Collect deps needed by other installed apps (from catalog + stored state)
    catalog_by_id = {a['id']: a for a in BUILTIN_CATALOG}
    needed_apt = set()
    needed_pip = set()
    for aid in installed:
        if aid == app_id:
            continue
        cat_entry = catalog_by_id.get(aid, {})
        stored_entry = installed.get(aid, {})
        needed_apt |= set(cat_entry.get('apt_deps', []))
        needed_apt |= set(stored_entry.get('apt_deps', []))
        needed_pip |= set(cat_entry.get('pip_deps', []))
        needed_pip |= set(stored_entry.get('pip_deps', []))

    return list(app_apt - needed_apt), list(app_pip - needed_pip)


def _remove_apt_deps(deps, emit_fn):
    """Remove orphaned APT packages."""
    if not deps:
        return
    # Only remove packages that are actually installed
    to_remove = []
    for pkg in deps:
        check = host_run(f'dpkg -l {q(pkg)} 2>/dev/null | grep -q "^ii"', timeout=10)
        if check.returncode == 0:
            to_remove.append(pkg)
    if not to_remove:
        return
    pkgs = ' '.join(q(d) for d in to_remove)
    log.info('[app_manager] Removing orphan apt deps: %s', to_remove)
    emit_fn({'stage': 'deps_cleanup', 'message': f'Usuwanie apt: {", ".join(to_remove)}', 'percent': 45, 'status': 'running'})
    cmd = f'DEBIAN_FRONTEND=noninteractive apt-get remove -y {pkgs} 2>&1'
    lock_file = '/tmp/ethos-apt.lock'
    wrapped = f"flock -w 180 {q(lock_file)} bash -lc {q(cmd)}"
    for line in host_run_stream(wrapped):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            rc = int(stripped.split(':', 1)[1])
            if rc != 0:
                log.warning('[app_manager] apt remove failed (rc=%s)', rc)
            break


def _remove_pip_deps(deps, emit_fn):
    """Remove orphaned pip packages."""
    if not deps:
        return
    venv = os.path.join(_ETHOS_ROOT, 'venv')
    pip = os.path.join(venv, 'bin', 'pip') if os.path.isdir(venv) else 'pip3'
    # Only remove packages that are actually installed
    to_remove = []
    for pkg in deps:
        pkg_name = pkg.split('==')[0].split('>=')[0].split('<=')[0].strip()
        check = host_run(f'{q(pip)} show {q(pkg_name)} 2>/dev/null | grep -q "^Name:"', timeout=10)
        if check.returncode == 0:
            to_remove.append(pkg_name)
    if not to_remove:
        return
    pkgs = ' '.join(q(d) for d in to_remove)
    log.info('[app_manager] Removing orphan pip deps: %s', to_remove)
    emit_fn({'stage': 'deps_cleanup', 'message': f'Usuwanie pip: {", ".join(to_remove)}', 'percent': 50, 'status': 'running'})
    cmd = f'{q(pip)} uninstall -y {pkgs} 2>&1'
    for line in host_run_stream(cmd):
        stripped = line.strip()
        if stripped.startswith('__EXIT_CODE__:'):
            rc = int(stripped.split(':', 1)[1])
            if rc != 0:
                log.warning('[app_manager] pip uninstall failed (rc=%s)', rc)
            break


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
                with gevent.Timeout(60, False):
                    with _flask_app.app_context():
                        with _flask_app.test_client() as tc:
                            resp = _internal_post(tc, uninstall_ep, json={'wipe_data': wipe_data})
                            if resp and resp.status_code not in (200, 204):
                                log.warning('[app_manager] uninstall_endpoint %s returned %s', uninstall_ep, resp.status_code)
            except Exception as e:
                log.warning('[app_manager] uninstall_endpoint %s failed: %s', uninstall_ep, e)

        # Remove orphaned dependencies (apt/pip) not needed by other installed apps
        try:
            apt_orphans, pip_orphans = _get_orphan_deps(app_id, app_def)
            if apt_orphans or pip_orphans:
                emit({'stage': 'deps_cleanup', 'percent': 40, 'message': 'Usuwanie nieuzywanych zaleznosci...', 'status': 'running'})
                _remove_apt_deps(apt_orphans, emit)
                _remove_pip_deps(pip_orphans, emit)
        except Exception as e:
            log.warning('[app_manager] dep cleanup for %s failed: %s', app_id, e)

        # Remove files only for externally-downloaded apps, not bundled ones
        installed_info = _load_installed().get(app_id, {})
        was_external = installed_info.get('source') == 'github'

        emit({'stage': 'remove', 'percent': 60, 'message': 'Usuwanie plikow apki...', 'status': 'running'})
        if was_external:
            for fn in _get_frontend_filenames(app_id):
                # Don't remove shared frontend files used by core apps (e.g. storage.js)
                core_uses_same = any(
                    _get_frontend_filename(cid) == fn for cid in CORE_APPS
                )
                if not core_uses_same:
                    fp = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                    if os.path.isfile(fp):
                        os.remove(fp)
        # Remove backend files only for externally-downloaded apps
        if was_external:
            for idx, module_name in enumerate(_get_backend_filenames(app_id)):
                bp_file = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                # Primary file: don't remove if another installed app shares the same module
                if idx == 0:
                    other_using_same = [
                        aid for aid, bpi in _OPTIONAL_BLUEPRINTS.items()
                        if bpi[0] == module_name and aid != app_id
                        and aid in _load_installed()
                    ]
                    if other_using_same:
                        continue
                if os.path.isfile(bp_file):
                    os.remove(bp_file)
                    log.info('[app_manager] Removed backend file: %s', module_name)

        _set_uninstalled(app_id)

        emit({'stage': 'done', 'percent': 100, 'message': app_def['name'] + ' odinstalowano', 'status': 'done'})

        # Notify all clients — hot-remove from desktop without refresh
        if _socketio:
            _socketio.emit('app_uninstalled', {'id': app_id})

        # Sync frontend_dist — cache sync after events flushed
        gevent.sleep(0.1)
        try:
            _sync_frontend_dist()
        except Exception as e:
            log.warning('[app_manager] frontend sync error: %s', e)

    except Exception as e:
        log.exception('[app_manager] uninstall error for %s', app_id)
        try:
            emit({'stage': 'error', 'percent': 0, 'message': 'Bład: ' + str(e), 'status': 'error'})
        except Exception:
            pass
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
    'storage-manager':  ('Storage Manager',  'fa-database',          '#10b981', 'Storage', 'Dyski, RAID, wolumeny, udostepnianie, diagnostyka'),
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
    installed = _ensure_installed_apps()

    result = []
    for app in catalog:
        if app['id'] in CORE_APPS:
            continue
        inst = installed.get(app['id'], {})
        item = dict(app)
        item['installed'] = isinstance(inst, dict) and bool(inst)
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
            if _semver_key(app.get('version', '')) > _semver_key(inst.get('version', '')):
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

    bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
    if not bp_info:
        return jsonify({'error': 'Nieznana apka: ' + app_id}), 404

    cfg = _load_app_update_config()
    source = cfg.get('source', 'github')

    if source == 'github':
        repo = cfg.get('github_repo', DEFAULT_GITHUB_REPO)
        base_url = _github_raw_base(repo) + '/apps'
    else:
        raw_url = _get_update_url()
        base_url = _resolve_update_base(raw_url) if raw_url else ''
        if not base_url:
            return jsonify({'error': 'Serwer aktualizacji nie skonfigurowany'}), 400

    task_id = str(uuid.uuid4())[:8]
    from gevent import spawn
    spawn(_bg_update_apps, [app_id], base_url, task_id, source)
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


@app_manager_bp.route('/running-tasks')
def get_running_tasks():
    """Return currently running install/update task states for reconnect recovery."""
    if not getattr(g, 'role', None):
        return jsonify({'error': 'Unauthorized'}), 401
    with _running_task_lock:
        tasks = [v for v in _running_task_state.values()]
    return jsonify({'tasks': tasks})


# ═══════════════════════════════════════════════════════════
#  App update source — GitHub (default) or OTA server
# ═══════════════════════════════════════════════════════════

_DEFAULT_APP_UPDATE_CONFIG = {
    'source': 'github',
    'github_repo': DEFAULT_GITHUB_REPO,
}


def _load_app_update_config():
    try:
        if os.path.isfile(APP_UPDATE_CONFIG_FILE):
            with open(APP_UPDATE_CONFIG_FILE) as f:
                cfg = json.load(f)
            # Ensure defaults
            if 'source' not in cfg:
                cfg['source'] = 'github'
            if 'github_repo' not in cfg:
                cfg['github_repo'] = DEFAULT_GITHUB_REPO
            return cfg
    except Exception:
        pass
    return dict(_DEFAULT_APP_UPDATE_CONFIG)


def _save_app_update_config(cfg):
    tmp = APP_UPDATE_CONFIG_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, APP_UPDATE_CONFIG_FILE)


@app_manager_bp.route('/app-update-config', methods=['GET'])
def get_app_update_config():
    err = _require_admin()
    if err:
        return err
    cfg = _load_app_update_config()
    return jsonify({'ok': True, **cfg})


@app_manager_bp.route('/app-update-config', methods=['PUT'])
def set_app_update_config():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    source = body.get('source', 'github')
    if source not in ('github', 'ota'):
        return jsonify({'error': 'source musi być "github" lub "ota"'}), 400
    cfg = _load_app_update_config()
    cfg['source'] = source
    if 'github_repo' in body and body['github_repo']:
        repo = body['github_repo'].strip()
        if '/' not in repo:
            return jsonify({'error': 'Format repo: owner/name'}), 400
        cfg['github_repo'] = repo
    _save_app_update_config(cfg)
    return jsonify({'ok': True, **cfg})


# ═══════════════════════════════════════════════════════════
#  Catalog sources — multi-source app catalog
# ═══════════════════════════════════════════════════════════

@app_manager_bp.route('/catalog-sources', methods=['GET'])
def get_catalog_sources():
    err = _require_admin()
    if err:
        return err
    return jsonify({'ok': True, 'sources': _load_catalog_sources()})


@app_manager_bp.route('/catalog-sources', methods=['POST'])
def add_catalog_source():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    src_type = body.get('type', 'github')
    name = body.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Nazwa źródła jest wymagana'}), 400
    if src_type not in ('github', 'url'):
        return jsonify({'error': 'Typ musi być "github" lub "url"'}), 400

    if src_type == 'github':
        repo = body.get('repo', '').strip()
        if not repo or '/' not in repo:
            return jsonify({'error': 'Format repozytorium: owner/name'}), 400
    else:
        url = body.get('url', '').strip()
        if not url or not (url.startswith('http://') or url.startswith('https://')):
            return jsonify({'error': 'Podaj poprawny URL (http:// lub https://)'}), 400

    sources = _load_catalog_sources()
    src_id = name.lower().replace(' ', '-').replace('/', '-')[:32]
    # Ensure unique ID
    existing_ids = {s['id'] for s in sources}
    base_id = src_id
    counter = 2
    while src_id in existing_ids:
        src_id = f'{base_id}-{counter}'
        counter += 1

    new_src = {
        'id': src_id,
        'name': name,
        'type': src_type,
        'enabled': True,
    }
    if src_type == 'github':
        new_src['repo'] = body['repo'].strip()
    else:
        new_src['url'] = body['url'].strip()

    sources.append(new_src)
    _save_catalog_sources(sources)
    # Invalidate cache so next catalog load picks up new source
    try:
        os.unlink(CATALOG_CACHE_FILE)
    except OSError:
        pass
    return jsonify({'ok': True, 'source': new_src, 'sources': sources})


@app_manager_bp.route('/catalog-sources/<src_id>', methods=['PUT'])
def update_catalog_source(src_id):
    err = _require_admin()
    if err:
        return err
    sources = _load_catalog_sources()
    target = None
    for s in sources:
        if s['id'] == src_id:
            target = s
            break
    if not target:
        return jsonify({'error': 'Źródło nie znalezione'}), 404

    body = request.get_json(silent=True) or {}
    if 'name' in body and body['name'].strip():
        target['name'] = body['name'].strip()
    if 'enabled' in body:
        target['enabled'] = bool(body['enabled'])
    if target.get('type') == 'github' and 'repo' in body:
        repo = body['repo'].strip()
        if repo and '/' in repo:
            target['repo'] = repo
    if target.get('type') == 'url' and 'url' in body:
        url = body['url'].strip()
        if url and (url.startswith('http://') or url.startswith('https://')):
            target['url'] = url

    _save_catalog_sources(sources)
    try:
        os.unlink(CATALOG_CACHE_FILE)
    except OSError:
        pass
    return jsonify({'ok': True, 'sources': sources})


@app_manager_bp.route('/catalog-sources/<src_id>', methods=['DELETE'])
def delete_catalog_source(src_id):
    err = _require_admin()
    if err:
        return err
    sources = _load_catalog_sources()
    new_sources = [s for s in sources if s['id'] != src_id]
    if len(new_sources) == len(sources):
        return jsonify({'error': 'Źródło nie znalezione'}), 404
    if not new_sources:
        return jsonify({'error': 'Musi pozostać co najmniej jedno źródło'}), 400
    _save_catalog_sources(new_sources)
    try:
        os.unlink(CATALOG_CACHE_FILE)
    except OSError:
        pass
    return jsonify({'ok': True, 'sources': new_sources})


def _get_update_url():
    """Read update_url from updater config."""
    cfg_path = data_path('update_config.json')
    try:
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                return json.load(f).get('update_url', '')
    except Exception:
        pass
    return ''


def _resolve_update_base(raw):
    """Resolve user-friendly update source to base URL (same logic as updater)."""
    if not raw:
        return ''
    raw = raw.strip().rstrip('/')
    if raw.startswith('github:'):
        return ''
    if raw.startswith('http://') or raw.startswith('https://'):
        from urllib.parse import urlparse
        parsed = urlparse(raw)
        path = parsed.path.rstrip('/')
        if path == '' or path == '/':
            return raw.rstrip('/') + '/updates'
        return raw
    return f'http://{raw}:9000/updates'


def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _ensure_installed_apps():
    """Ensure installed_apps.json contains all on-disk optional apps.
    Detects apps deployed via file copy that were never registered.
    Also syncs version when local file was updated outside Package Center
    (e.g. via git pull or OTA) so false update badges don't appear."""
    from datetime import datetime
    installed = _load_installed()
    changed = False
    now = datetime.utcnow().isoformat()

    catalog_by_id = {a['id']: a for a in BUILTIN_CATALOG}

    for app_id, bp_info in _OPTIONAL_BLUEPRINTS.items():
        module_name = bp_info[0]
        local_py = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
        if not (os.path.isfile(local_py) and os.path.getsize(local_py) > 0):
            continue

        cat_entry = catalog_by_id.get(app_id, {})

        if app_id not in installed:
            # Skip auto-registration for apps that have pip/apt deps —
            # those must be explicitly installed so deps get satisfied.
            has_deps = bool(cat_entry.get('apt_deps') or cat_entry.get('pip_deps'))
            if has_deps:
                continue
            installed[app_id] = {
                'version': 'bundled',
                'source': 'bundled',
                'installed_at': now,
            }
            changed = True
            log.info('[app_manager] Auto-registered on-disk app: %s', app_id)

        # Sync version: if installed version is behind catalog and files are
        # on disk, the app was updated outside Package Center — bump version.
        cat_ver = catalog_by_id.get(app_id, {}).get('version', '')
        inst_ver = installed[app_id].get('version', '')
        if cat_ver and inst_ver in ('bundled', 'core') or (cat_ver and inst_ver and cat_ver > inst_ver):
            installed[app_id]['version'] = cat_ver
            changed = True

    if changed:
        _save_installed(installed)
    return installed


def _git_blob_sha(filepath):
    """Compute git blob SHA1 for a local file (matches GitHub's blob SHA)."""
    import hashlib
    with open(filepath, 'rb') as f:
        content = f.read()
    blob = b'blob ' + str(len(content)).encode() + b'\0' + content
    return hashlib.sha1(blob).hexdigest()


def _github_raw_base(repo):
    return f'https://raw.githubusercontent.com/{repo}/main'


def _fetch_github_tree(repo):
    """Fetch the full file tree from a public GitHub repo. Returns {path: sha} dict."""
    url = f'https://api.github.com/repos/{repo}/git/trees/main?recursive=1'
    req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return {item['path']: item['sha'] for item in data.get('tree', [])}


def _check_github_updates(repo):
    """Check GitHub repo for app files that differ from local (git blob SHA comparison).
    Returns list of dicts: [{id, name, local_version, remote_version, backend_changed, frontend_changed}]."""
    # Fetch catalog for app names/versions
    catalog_url = _github_raw_base(repo) + '/catalog.json'
    try:
        req = urllib.request.Request(catalog_url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        raise RuntimeError(f'Nie udało się pobrać katalogu GitHub: {e}')

    remote_apps = data.get('apps', data) if isinstance(data, dict) else data
    remote_by_id = {a['id']: a for a in remote_apps if isinstance(a, dict) and 'id' in a}

    # Fetch tree for SHA comparison
    try:
        remote_tree = _fetch_github_tree(repo)
    except Exception as e:
        raise RuntimeError(f'Nie udało się pobrać drzewa GitHub: {e}')

    local_by_id = {a['id']: a for a in BUILTIN_CATALOG}
    installed = _ensure_installed_apps()
    updates = []

    for app_id in remote_by_id:
        # Skip core apps — they are updated via system OTA, not Package Center
        if app_id in CORE_APPS:
            continue
        if app_id not in installed and app_id not in _OPTIONAL_BLUEPRINTS:
            continue

        bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
        if not bp_info:
            continue

        remote = remote_by_id[app_id]
        local = local_by_id.get(app_id, {})

        backend_changed = False
        frontend_changed = False

        # Compare backend .py files (primary + extras)
        for idx, module_name in enumerate(_get_backend_filenames(app_id)):
            remote_name = 'backend.py' if idx == 0 else f'backend_{idx + 1}.py'
            local_py = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
            remote_py_path = f'apps/{app_id}/{remote_name}'
            if os.path.isfile(local_py) and remote_py_path in remote_tree:
                local_sha = _git_blob_sha(local_py)
                if local_sha != remote_tree[remote_py_path]:
                    backend_changed = True
                    break

        # Compare frontend .js (primary + extras)
        for idx, fn in enumerate(_get_frontend_filenames(app_id)):
            remote_name = 'frontend.js' if idx == 0 else f'frontend_{idx + 1}.js'
            local_js = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
            remote_js_path = f'apps/{app_id}/{remote_name}'
            if os.path.isfile(local_js) and remote_js_path in remote_tree:
                local_sha = _git_blob_sha(local_js)
                if local_sha != remote_tree[remote_js_path]:
                    frontend_changed = True
                    break

        if backend_changed or frontend_changed:
            updates.append({
                'id': app_id,
                'name': remote.get('name', local.get('name', app_id)),
                'local_version': local.get('version', '?'),
                'remote_version': remote.get('version', '?'),
                'backend_changed': backend_changed,
                'frontend_changed': frontend_changed,
            })

    return updates


@app_manager_bp.route('/check-app-updates', methods=['POST'])
def check_app_updates():
    """Check for app updates — GitHub (version comparison) or OTA (SHA256 hash)."""
    err = _require_admin()
    if err:
        return err

    cfg = _load_app_update_config()
    source = cfg.get('source', 'github')

    if source == 'github':
        repo = cfg.get('github_repo', DEFAULT_GITHUB_REPO)
        try:
            updates = _check_github_updates(repo)
        except RuntimeError as e:
            return jsonify({'error': str(e)}), 502
        return jsonify({'ok': True, 'updates': updates, 'source': 'github', 'repo': repo})

    # OTA server mode
    raw_url = _get_update_url()
    if not raw_url:
        return jsonify({'error': 'Serwer aktualizacji nie skonfigurowany. Zmień źródło na GitHub lub skonfiguruj OTA.'}), 400

    base_url = _resolve_update_base(raw_url)
    if not base_url:
        return jsonify({'error': 'Nieobsługiwany format URL aktualizacji'}), 400

    manifest_url = base_url + '/apps.json'
    try:
        req = urllib.request.Request(manifest_url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            remote_apps = json.loads(resp.read().decode())
    except Exception as e:
        return jsonify({'error': f'Nie udało się pobrać manifestu: {e}'}), 502

    installed = _ensure_installed_apps()
    updates = []

    for app_id, remote in remote_apps.items():
        if app_id not in installed:
            continue
        bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
        if not bp_info:
            continue

        has_update = False
        detail = {'id': app_id, 'name': app_id}

        for a in BUILTIN_CATALOG:
            if a['id'] == app_id:
                detail['name'] = a.get('name', app_id)
                break

        module_name = bp_info[0]
        local_py = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
        if os.path.isfile(local_py) and remote.get('backend_sha256'):
            local_hash = _file_sha256(local_py)
            if local_hash != remote['backend_sha256']:
                has_update = True
                detail['backend_changed'] = True

        for idx, fn in enumerate(_get_frontend_filenames(app_id)):
            local_js = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
            sha_key = 'frontend_sha256' if idx == 0 else f'frontend_{idx + 1}_sha256'
            if os.path.isfile(local_js) and remote.get(sha_key):
                local_hash = _file_sha256(local_js)
                if local_hash != remote[sha_key]:
                    has_update = True
                    detail['frontend_changed'] = True
                    break

        if has_update:
            updates.append(detail)

    return jsonify({'ok': True, 'updates': updates, 'source': 'ota', 'update_server': raw_url})


@app_manager_bp.route('/cleanup-disk', methods=['POST'])
def cleanup_disk():
    """Free root partition space: apt cache, pip cache, __pycache__. Returns freed MB."""
    err = _require_admin()
    if err:
        return err

    def _free_mb():
        st = os.statvfs('/')
        return (st.f_bavail * st.f_frsize) / (1024 * 1024)

    before = _free_mb()
    log.info('[app_manager] cleanup-disk: %.0f MB free before', before)

    host_run('apt-get clean 2>/dev/null', timeout=30)
    host_run('apt-get autoremove -y 2>/dev/null', timeout=120)
    host_run('rm -rf /root/.cache/pip /tmp/pip-* 2>/dev/null', timeout=10)
    host_run('rm -rf /tmp/*.tmp /tmp/ethos-* 2>/dev/null', timeout=10)
    venv_dir = os.path.join(os.environ.get('ETHOS_ROOT', '/opt/ethos'), 'venv')
    host_run(
        f'find {q(venv_dir)} -name __pycache__ -type d -exec rm -rf {{}} + 2>/dev/null',
        timeout=30)

    after = _free_mb()
    freed = round(after - before)
    log.info('[app_manager] cleanup-disk: %.0f MB free after, freed %d MB', after, freed)
    return jsonify({'ok': True, 'freed_mb': freed, 'free_mb': round(after)})


@app_manager_bp.route('/update-apps', methods=['POST'])
def update_apps():
    """Batch-update apps. Body: { "app_ids": ["doc-anonymizer", ...] }
    Downloads from GitHub or OTA depending on configured source."""
    err = _require_admin()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    app_ids = body.get('app_ids', [])
    if not app_ids or not isinstance(app_ids, list):
        return jsonify({'error': 'Podaj listę app_ids'}), 400

    cfg = _load_app_update_config()
    source = cfg.get('source', 'github')

    if source == 'github':
        repo = cfg.get('github_repo', DEFAULT_GITHUB_REPO)
        base_url = _github_raw_base(repo) + '/apps'
        task_id = str(uuid.uuid4())[:8]
        from gevent import spawn
        spawn(_bg_update_apps, app_ids, base_url, task_id, 'github')
        return jsonify({'ok': True, 'task_id': task_id})

    # OTA
    raw_url = _get_update_url()
    if not raw_url:
        return jsonify({'error': 'Serwer aktualizacji nie skonfigurowany'}), 400
    base_url = _resolve_update_base(raw_url)
    if not base_url:
        return jsonify({'error': 'Nieobsługiwany format URL aktualizacji'}), 400

    task_id = str(uuid.uuid4())[:8]
    from gevent import spawn
    spawn(_bg_update_apps, app_ids, base_url, task_id, 'ota')
    return jsonify({'ok': True, 'task_id': task_id})


def _bg_update_apps(app_ids, base_url, task_id, source='ota'):
    """Background: download updated files and hot-reload."""
    def emit(extra):
        _emit({'task_id': task_id, **extra})

    _task_start()
    total = len(app_ids)
    updated = []
    failed = []

    try:
        emit({'stage': 'start', 'percent': 2, 'status': 'running',
              'message': f'Aktualizacja {total} aplikacji ({source})...'})

        for idx, app_id in enumerate(app_ids):
            pct_base = int(5 + (idx / total) * 85)
            emit({'stage': 'updating', 'percent': pct_base, 'app_id': app_id,
                  'status': 'running',
                  'message': f'Aktualizacja {app_id} ({idx+1}/{total})...'})

            bp_info = _OPTIONAL_BLUEPRINTS.get(app_id)
            if not bp_info:
                log.warning('[app_manager] Unknown app for update: %s', app_id)
                failed.append(app_id)
                continue

            ok = True

            # Download backend .py (primary + extras)
            emit({'stage': 'updating', 'percent': pct_base + 2, 'app_id': app_id,
                  'status': 'running', 'message': f'{app_id}: pobieranie backend...'})
            for b_idx, module_name in enumerate(_get_backend_filenames(app_id)):
                remote_name = 'backend.py' if b_idx == 0 else f'backend_{b_idx + 1}.py'
                bp_url = base_url + f'/{app_id}/{remote_name}'
                bp_dest = os.path.join(_BLUEPRINTS_DIR, module_name + '.py')
                if not _download_file(bp_url, bp_dest):
                    log.warning('[app_manager] Backend download failed: %s/%s', app_id, remote_name)
                    ok = False
                    break

            # Download frontend .js (primary + extras)
            for idx, fn in enumerate(_get_frontend_filenames(app_id)):
                remote_name = 'frontend.js' if idx == 0 else f'frontend_{idx + 1}.js'
                js_url = base_url + f'/{app_id}/{remote_name}'
                js_dest = os.path.join(_FRONTEND_APPS_DIR, fn + '.js')
                emit({'stage': 'updating', 'percent': pct_base + 4, 'app_id': app_id,
                      'status': 'running', 'message': f'{app_id}: pobieranie frontend...'})
                if not _download_file(js_url, js_dest):
                    log.warning('[app_manager] Frontend download failed: %s', app_id)
                    ok = False

            if ok:
                emit({'stage': 'updating', 'percent': pct_base + 6, 'app_id': app_id,
                      'status': 'running', 'message': f'{app_id}: ładowanie...'})
                _hot_load_blueprint(app_id)
                # Use catalog version if available, fall back to 'latest'
                cat_entry = next((a for a in BUILTIN_CATALOG if a['id'] == app_id), {})
                ver = cat_entry.get('version', 'latest')
                _set_installed(app_id, ver, source)
                updated.append(app_id)
            else:
                failed.append(app_id)

        msg = f'Zaktualizowano {len(updated)} aplikacji'
        if failed:
            msg += f', {len(failed)} błędów'
        emit({'stage': 'done', 'percent': 100, 'status': 'done',
              'message': msg, 'updated': updated, 'failed': failed})

        # Sync frontend_dist after events flushed
        if updated:
            gevent.sleep(0.1)
            try:
                _sync_frontend_dist()
            except Exception as e:
                log.warning('[app_manager] frontend sync error: %s', e)

    except Exception as e:
        log.exception('[app_manager] App update error')
        emit({'stage': 'error', 'percent': 0, 'status': 'error',
              'message': f'Błąd: {e}'})
    finally:
        _task_done()
