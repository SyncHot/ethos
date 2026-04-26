# EthOS Blueprint Initialization
# This module handles the initialization of blueprints in logical groups

from blueprints import (
    # Storage & File Management
    storage_bp, init_storage, resources_bp, backup_bp, cloud_backup_bp, raid_bp,
    diskrepair_bp, ssd_cache_bp,

    # System & User Management
    users_bp, settings_bp, hardware_bp, power_bp, encryption_bp, totp_bp, sandbox_bp,

    # Network & Security
    network_bp, ddns_bp, firewall_bp, fail2ban_bp, wireguard_bp, ups_bp,

    # Applications & Services
    dashboard_bp, notifications_bp, eventlog_bp, init_eventlog, ssh_bp, installer_bp,
    app_manager_bp, init_app_manager, _load_optional_blueprints, api_docs_bp,
    security_advisor_bp, antivirus_bp, printer_bp, downloads_bp, aichat_bp, ldap_bp,
    appstore_bp, builder_bp, familyhub_bp, packages_bp, docker_bp, vm_bp,
    doc_anonymizer_bp, gallery_bp, cron_bp, photos_ai_bp, flasher_bp, video_station_bp,
    domains_mgr_bp, pkg_registry_bp, editor_bp, websites_bp, sharing_bp, rollback_bp,
    remote_log_bp, radio_music_bp, surveillance_bp, sync_drive_bp, tickets_bp,
    med_assistant_bp, mail_bp, notes_bp,

    # Update-related
    update_bp, updates_public_bp, init_update, update_auto_check_loop,

    # Special cases
    _init_ws_proxy
)

def initialize_blueprints(app, socketio):
    """Initialize all blueprints in logical groups."""

    # Storage & File Management
    app.register_blueprint(storage_bp)
    init_storage(socketio)
    app.register_blueprint(resources_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(cloud_backup_bp)
    app.register_blueprint(raid_bp)
    app.register_blueprint(diskrepair_bp)
    app.register_blueprint(ssd_cache_bp)

    # System & User Management
    app.register_blueprint(users_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(hardware_bp)
    app.register_blueprint(power_bp, url_prefix='/api/power')
    app.register_blueprint(encryption_bp)
    app.register_blueprint(totp_bp)
    app.register_blueprint(sandbox_bp)

    # Network & Security
    app.register_blueprint(network_bp)
    app.register_blueprint(ddns_bp)
    app.register_blueprint(firewall_bp)
    app.register_blueprint(fail2ban_bp)
    app.register_blueprint(wireguard_bp)
    app.register_blueprint(ups_bp)

    # Applications & Services
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(eventlog_bp)
    init_eventlog()
    app.register_blueprint(ssh_bp)
    app.register_blueprint(installer_bp)
    app.register_blueprint(app_manager_bp)
    init_app_manager(socketio)
    _load_optional_blueprints(app, socketio)
    app.register_blueprint(api_docs_bp)
    app.register_blueprint(security_advisor_bp)
    app.register_blueprint(antivirus_bp)
    app.register_blueprint(printer_bp)
    app.register_blueprint(downloads_bp)
    app.register_blueprint(aichat_bp)
    app.register_blueprint(ldap_bp)
    app.register_blueprint(appstore_bp)
    app.register_blueprint(builder_bp)
    app.register_blueprint(familyhub_bp)
    app.register_blueprint(packages_bp)
    app.register_blueprint(docker_bp)
    app.register_blueprint(vm_bp)
    app.register_blueprint(doc_anonymizer_bp)
    app.register_blueprint(gallery_bp)
    app.register_blueprint(cron_bp)
    app.register_blueprint(photos_ai_bp)
    app.register_blueprint(flasher_bp)
    app.register_blueprint(video_station_bp)
    app.register_blueprint(domains_mgr_bp)
    app.register_blueprint(pkg_registry_bp)
    app.register_blueprint(editor_bp)
    app.register_blueprint(websites_bp)
    app.register_blueprint(sharing_bp)
    app.register_blueprint(rollback_bp)
    app.register_blueprint(remote_log_bp)
    app.register_blueprint(radio_music_bp)
    app.register_blueprint(surveillance_bp)
    app.register_blueprint(sync_drive_bp)
    app.register_blueprint(tickets_bp)
    app.register_blueprint(med_assistant_bp)
    app.register_blueprint(mail_bp)
    app.register_blueprint(notes_bp)

    # Update-related
    app.register_blueprint(update_bp)
    app.register_blueprint(updates_public_bp)
    init_update(socketio)

    # Final initialization
    try:
        _init_ws_proxy(app)
    except ImportError:
        pass

    # Migrate data from app_path → data_path (one-time, for existing installs)
    migrate_from_ethos_packages()