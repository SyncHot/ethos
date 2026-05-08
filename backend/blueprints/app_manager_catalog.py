"""EthOS - App Manager Catalog

Catalog discovery, fetching, caching, update checking.
"""

import os
import json
import time
import logging
import sys
import urllib.request
import urllib.error
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path, app_path

log = logging.getLogger('app_manager')

# Path constants
APP_UPDATE_CONFIG_FILE = data_path('app_update_config.json')
CATALOG_SOURCES_FILE = data_path('catalog_sources.json')
CATALOG_CACHE_FILE = data_path('app_catalog_cache.json')
CATALOG_CACHE_TTL = 3600 * 6

DEFAULT_GITHUB_REPO = 'SyncHot/ethos-os-ethos-apps'
GITHUB_CATALOG_URL = f'https://raw.githubusercontent.com/{DEFAULT_GITHUB_REPO}/main/catalog.json'
GITHUB_APP_BASE = f'https://raw.githubusercontent.com/{DEFAULT_GITHUB_REPO}/main/apps'

_ETHOS_ROOT = app_path()
_FRONTEND_APPS_DIR = os.path.join(_ETHOS_ROOT, 'frontend', 'js', 'apps')
_BLUEPRINTS_DIR = os.path.join(_ETHOS_ROOT, 'backend', 'blueprints')

_catalog_lock = threading.RLock()


def _main():
    return sys.modules.get('blueprints.app_manager')


def _fileops():
    return sys.modules.get('blueprints.app_manager_fileops')


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
        'id': 'gallery', 'name': 'Gallery', 'version': '1.0.5',
        'icon': 'fa-images', 'color': '#ec4899', 'category': 'Media', 'admin_only': False,
        'description': 'Galeria zdjec i filmow z EXIF, miniaturkami i haslami folderow.',
        'apt_deps': [], 'pip_deps': [],
        'install_endpoint': '/api/gallery/install',
        'uninstall_endpoint': '/api/gallery/uninstall',
        'status_endpoint': '/api/gallery/pkg-status',
    },
    {
        'id': 'download-manager', 'name': 'Download Manager', 'version': '1.0.4',
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
        'id': 'vm-manager', 'name': 'VM Manager', 'version': '1.0.12',
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
        'id': 'builder', 'name': 'Builder', 'version': '1.0.22',
        'icon': 'fa-hammer', 'color': '#f97316', 'category': 'System', 'admin_only': True,
        'description': 'Budowanie wydan EthOS i obrazow systemowych przez interfejs webowy.',
        'apt_deps': ['squashfs-tools', 'genisoimage', 'rsync'], 'pip_deps': [],
        'install_endpoint': '/api/builder/install',
        'uninstall_endpoint': '/api/builder/uninstall',
        'status_endpoint': '/api/builder/pkg-status',
    },
    {
        'id': 'disk-repair', 'name': 'Disk Repair', 'version': '1.0.21',
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
        'id': 'sharing-samba', 'name': 'File Sharing (Samba)', 'version': '1.0.22',
        'icon': 'fa-windows', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Udostepnianie plikow przez siec (Windows, Mac, Linux).',
        'apt_deps': ['samba'], 'pip_deps': [],
        'install_endpoint': '/api/storage/samba/pkg-install',
        'uninstall_endpoint': '/api/storage/samba/pkg-uninstall',
        'status_endpoint': '/api/storage/samba/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-nfs', 'name': 'NFS', 'version': '1.0.21',
        'icon': 'fa-network-wired', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Szybkie udostepnianie plikow dla Linux/Unix przez NFS.',
        'apt_deps': ['nfs-kernel-server'], 'pip_deps': [],
        'install_endpoint': '/api/storage/nfs/pkg-install',
        'uninstall_endpoint': '/api/storage/nfs/pkg-uninstall',
        'status_endpoint': '/api/storage/nfs/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-dlna', 'name': 'DLNA (MiniDLNA)', 'version': '1.0.22',
        'icon': 'fa-photo-video', 'color': '#6366f1', 'category': 'Media', 'admin_only': True,
        'description': 'Serwer DLNA do strumieniowania multimediow na TV i odtwarzacze.',
        'apt_deps': ['minidlna'], 'pip_deps': [],
        'install_endpoint': '/api/storage/dlna/pkg-install',
        'uninstall_endpoint': '/api/storage/dlna/pkg-uninstall',
        'status_endpoint': '/api/storage/dlna/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-webdav', 'name': 'WebDAV', 'version': '1.0.21',
        'icon': 'fa-globe', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Serwer WebDAV z dostepem do plikow przez HTTP.',
        'apt_deps': ['lighttpd'], 'pip_deps': [],
        'install_endpoint': '/api/storage/webdav/pkg-install',
        'uninstall_endpoint': '/api/storage/webdav/pkg-uninstall',
        'status_endpoint': '/api/storage/webdav/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-sftp', 'name': 'SFTP', 'version': '1.0.21',
        'icon': 'fa-lock', 'color': '#6366f1', 'category': 'Network', 'admin_only': True,
        'description': 'Bezpieczny transfer plikow przez SSH.',
        'apt_deps': ['openssh-server'], 'pip_deps': [],
        'install_endpoint': '/api/storage/sftp/pkg-install',
        'uninstall_endpoint': '/api/storage/sftp/pkg-uninstall',
        'status_endpoint': '/api/storage/sftp/pkg-status',
        'hidden': True,
    },
    {
        'id': 'sharing-ftp', 'name': 'FTP', 'version': '1.0.21',
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
        'id': 'raid-lvm', 'name': 'RAID / LVM', 'version': '1.0.21',
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
        'id': 'photos-ai', 'name': 'Photos AI', 'version': '0.0.10',
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
        'id': 'tickets', 'name': 'Tickets', 'version': '1.0.3',
        'icon': 'fa-tasks', 'color': '#06b6d4', 'category': 'Tools', 'admin_only': False,
        'description': 'Kanban — zarządzanie projektami i zadaniami.',
        'apt_deps': [], 'pip_deps': [], 'simple': True,
    },
    {
        'id': 'video-station', 'name': 'Video Station', 'version': '0.0.13',
        'icon': 'fa-film', 'color': '#7c3aed', 'category': 'Media', 'admin_only': False,
        'description': 'Biblioteka filmow z miniaturkami, streamingiem i sledzeniem postepu.',
        'apt_deps': ['ffmpeg'], 'pip_deps': [],
        'install_endpoint': '/api/video-station/install',
        'uninstall_endpoint': '/api/video-station/uninstall',
        'status_endpoint': '/api/video-station/pkg-status',
    },
    {
        'id': 'radio-music', 'name': 'Radio & Music', 'version': '0.0.10',
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

# ─── Catalog cache helpers ────────────────────────────────────

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


def _invalidate_catalog_cache():
    try:
        os.unlink(CATALOG_CACHE_FILE)
    except OSError:
        pass


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
            if not url.endswith('/catalog.json') and not url.endswith('.json'):
                url = url.rstrip('/') + '/catalog.json'

        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        apps = data.get('apps', data) if isinstance(data, dict) else data
        if not isinstance(apps, list):
            return None

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
        merged_by_id = {}

        for app in BUILTIN_CATALOG:
            merged_by_id[app['id']] = dict(app)

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


# ─── Is-bundled check ────────────────────────────────────────

def _is_bundled(app_id):
    m = sys.modules.get('blueprints.app_manager_fileops')
    if m:
        fns = m._get_frontend_filenames(app_id)
        apps_dir = m._FRONTEND_APPS_DIR
    else:
        fns = [app_id]
        apps_dir = _FRONTEND_APPS_DIR
    if not fns:
        return True
    return all(
        os.path.isfile(os.path.join(apps_dir, fn + '.js')) and
        os.path.getsize(os.path.join(apps_dir, fn + '.js')) > 0
        for fn in fns
    )


# ─── Semver + GitHub app base ────────────────────────────────

def _semver_key(ver):
    """Return a sortable tuple for semver comparison (handles 1.10.0 > 1.9.0 correctly)."""
    try:
        parts = str(ver).split('.')
        return tuple(int(p) for p in (parts + ['0', '0', '0'])[:3])
    except Exception:
        return (0, 0, 0)


def _get_github_app_base():
    """Get the GitHub base URL, respecting custom repo config. Fallback for non-sourced apps."""
    try:
        with open(APP_UPDATE_CONFIG_FILE) as f:
            cfg = json.load(f)
        repo = cfg.get('github_repo', DEFAULT_GITHUB_REPO)
    except Exception:
        repo = DEFAULT_GITHUB_REPO
    return f'https://raw.githubusercontent.com/{repo}/main/apps'


# ─── App update config ───────────────────────────────────────

_DEFAULT_APP_UPDATE_CONFIG = {
    'source': 'github',
    'github_repo': DEFAULT_GITHUB_REPO,
}


def _load_app_update_config():
    try:
        if os.path.isfile(APP_UPDATE_CONFIG_FILE):
            with open(APP_UPDATE_CONFIG_FILE) as f:
                cfg = json.load(f)
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


# ─── OTA update helpers ──────────────────────────────────────

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


# ─── SHA helpers ─────────────────────────────────────────────

def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


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
    catalog_url = _github_raw_base(repo) + '/catalog.json'
    try:
        req = urllib.request.Request(catalog_url, headers={'User-Agent': 'EthOS-AppManager/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        raise RuntimeError(f'Nie udało się pobrać katalogu GitHub: {e}')

    remote_apps = data.get('apps', data) if isinstance(data, dict) else data
    remote_by_id = {a['id']: a for a in remote_apps if isinstance(a, dict) and 'id' in a}

    try:
        remote_tree = _fetch_github_tree(repo)
    except Exception as e:
        raise RuntimeError(f'Nie udało się pobrać drzewa GitHub: {e}')

    local_by_id = {a['id']: a for a in BUILTIN_CATALOG}

    m_main = _main()
    CORE_APPS = getattr(m_main, 'CORE_APPS', frozenset()) if m_main else frozenset()
    _OPTIONAL_BLUEPRINTS = getattr(m_main, '_OPTIONAL_BLUEPRINTS', {}) if m_main else {}

    m_fo = _fileops()
    m_install = sys.modules.get('blueprints.app_manager_install')
    if m_install and hasattr(m_install, '_ensure_installed_apps'):
        installed = m_install._ensure_installed_apps()
    elif m_fo and hasattr(m_fo, '_load_installed'):
        installed = m_fo._load_installed()
    else:
        installed = {}

    updates = []

    for app_id in remote_by_id:
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

        if m_fo:
            backend_fns = m_fo._get_backend_filenames(app_id)
            frontend_fns = m_fo._get_frontend_filenames(app_id)
            fo_blueprints_dir = m_fo._BLUEPRINTS_DIR
            fo_frontend_dir = m_fo._FRONTEND_APPS_DIR
        else:
            backend_fns = [bp_info[0]]
            frontend_fns = [app_id]
            fo_blueprints_dir = _BLUEPRINTS_DIR
            fo_frontend_dir = _FRONTEND_APPS_DIR

        for idx, module_name in enumerate(backend_fns):
            remote_name = 'backend.py' if idx == 0 else f'backend_{idx + 1}.py'
            local_py = os.path.join(fo_blueprints_dir, module_name + '.py')
            remote_py_path = f'apps/{app_id}/{remote_name}'
            if os.path.isfile(local_py) and remote_py_path in remote_tree:
                local_sha = _git_blob_sha(local_py)
                if local_sha != remote_tree[remote_py_path]:
                    backend_changed = True
                    break

        for idx, fn in enumerate(frontend_fns):
            remote_name = 'frontend.js' if idx == 0 else f'frontend_{idx + 1}.js'
            local_js = os.path.join(fo_frontend_dir, fn + '.js')
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
