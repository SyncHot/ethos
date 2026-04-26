# EthOS Blueprints

# Import all blueprints for use in app.py
from blueprints.storage import storage_bp, init_storage
from blueprints.resources import resources_bp, resources_background_collector
from blueprints.resources_db import init_db as init_resources_db
from blueprints.backup import backup_bp, init_backup, get_backup_notifications
from blueprints.users import users_bp, _load_privileges
from blueprints.network import network_bp
from blueprints.eventlog import eventlog_bp, init_eventlog, log as elog
from blueprints.sandbox_policy import sandbox_bp
from blueprints.updater import update_bp, updates_public_bp, init_update, update_auto_check_loop
from blueprints.ddns import ddns_bp, start_ddns
from blueprints.settings import settings_bp
from blueprints.ssh_manager import ssh_bp
from blueprints.installer import installer_bp
from blueprints.power import power_bp
from blueprints.encryption import encryption_bp
from blueprints.ssd_cache import ssd_cache_bp
from blueprints.hardware import hardware_bp
from blueprints.notifications import notifications_bp, init_notifications
from blueprints.dashboard import dashboard_bp
from blueprints.admin_required import admin_required
from blueprints.totp import totp_bp, is_totp_enabled, verify_totp_code, verify_backup_code
from blueprints.api_docs import api_docs_bp
from blueprints.security_advisor import security_advisor_bp
from blueprints.firewall import firewall_bp
from blueprints.fail2ban import fail2ban_bp
from blueprints.app_manager import (
    app_manager_bp, init_app_manager, migrate_from_ethos_packages,
    BUILTIN_CATALOG as _BUILTIN_CATALOG,
    load_installed as _load_app_manager_installed,
    load_optional_blueprints as _load_optional_blueprints,
    OPTIONAL_BLUEPRINTS as _OPTIONAL_BLUEPRINTS,
)
from blueprints.antivirus import antivirus_bp
from blueprints.wireguard import wireguard_bp
from blueprints.printer import printer_bp
from blueprints.downloads import downloads_bp
from blueprints.aichat import aichat_bp
from blueprints.ldap_auth import ldap_bp
from blueprints.appstore import appstore_bp
from blueprints.builder import builder_bp
from blueprints.familyhub import familyhub_bp
from blueprints.packages import packages_bp
from blueprints.docker_manager import docker_bp
from blueprints.vm_manager import vm_bp
from blueprints.doc_anonymizer import doc_anonymizer_bp
from blueprints.gallery import gallery_bp
from blueprints.cron_manager import cron_bp
from blueprints.photos_ai import photos_ai_bp
from blueprints.flasher import flasher_bp
from blueprints.video_station import video_station_bp
from blueprints.domains_manager import domains_mgr_bp
from blueprints.cloud_backup import cloud_backup_bp
from blueprints.pkg_registry import pkg_registry_bp
from blueprints.editor import editor_bp
from blueprints.diskrepair import diskrepair_bp
from blueprints.websites import websites_bp
from blueprints.sharing import sharing_bp
from blueprints.rollback import rollback_bp
from blueprints.remote_log import remote_log_bp
from blueprints.radio_music import radio_music_bp
from blueprints.surveillance import surveillance_bp
from blueprints.ups import ups_bp
from blueprints.sync_drive import sync_drive_bp
from blueprints.tickets import tickets_bp
from blueprints.med_assistant import med_assistant_bp
from blueprints.mail_server import mail_bp
from blueprints.stickynotes import notes_bp
from blueprints.raid_manager import raid_bp

# For backward compatibility
from blueprints.app_manager import init_app_manager as init_app_manager_func