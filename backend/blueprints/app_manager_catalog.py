"""
EthOS - App Manager Catalog Module

Handles catalog discovery, fetching, merging, and caching.
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
from host import data_path

log = logging.getLogger('app_manager')

# ─── Catalog paths and settings ──────────────────────────────

CATALOG_SOURCES_FILE = data_path('catalog_sources.json')
CATALOG_CACHE_FILE = data_path('app_catalog_cache.json')
CATALOG_CACHE_TTL = 3600  # 1 hour

DEFAULT_GITHUB_REPO = 'SyncHot/ethos-os-ethos-apps'
GITHUB_CATALOG_URL = f'https://raw.githubusercontent.com/{DEFAULT_GITHUB_REPO}/main/catalog.json'
GITHUB_APP_BASE    = f'https://raw.githubusercontent.com/{DEFAULT_GITHUB_REPO}/main/apps'

# ─── Frontend extra files ────────────────────────────────────

_FRONTEND_EXTRA_FILES: dict = {
    # 'gallery': ['gallery_lightbox', 'gallery_people'],  # example
}

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



_catalog_lock = threading.RLock()


def _load_catalog_cache():
    """Return cached catalog if valid and fresh."""
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


def _invalidate_catalog_cache():
    """Clear catalog cache."""
    try:
        os.remove(CATALOG_CACHE_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning('[app_manager_catalog] Cannot remove cache: %s', e)


def _save_catalog_cache(data):
    """Save catalog to cache."""
    try:
        os.makedirs(os.path.dirname(CATALOG_CACHE_FILE), exist_ok=True)
        tmp = CATALOG_CACHE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, CATALOG_CACHE_FILE)
    except Exception as e:
        log.warning('[app_manager_catalog] Cannot save catalog cache: %s', e)


# ─── Multi-source catalog functions ──────────────────────────

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



def _load_catalog_with_cache():
    """Load catalog from cache or fetch fresh from all sources.
    
    Returns cached version if valid (< CATALOG_CACHE_TTL old).
    Otherwise fetches fresh from GitHub and built-in catalogs.
    """
    with _catalog_lock:
        # Try cache first
        cached = _load_catalog_cache()
        if cached is not None:
            return cached
        
        # Fetch fresh from multiple sources
        catalog = _get_catalog(force_refresh=True)
        return catalog


def _load_builtin_catalog():
    """Return BUILTIN_CATALOG as a list."""
    return list(BUILTIN_CATALOG)

