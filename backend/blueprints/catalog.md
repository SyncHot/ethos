# EthOS Blueprint Catalog

This document provides a comprehensive catalog of all blueprints in the EthOS system, organized by functionality to make it easier to understand the system architecture.

## System Core

### Authentication & Authorization
- **users_bp** (`users.py`) - User and group management with privilege system
- **auth_bp** - Authentication endpoints (login, logout, verification)
- **totp_bp** - Two-factor authentication (TOTP)
- **ldap_bp** - LDAP authentication integration

### System Management
- **settings_bp** - System settings management
- **hardware_bp** - Hardware information and management
- **power_bp** - Power management (suspend, hibernate, shutdown)
- **updater_bp** - System update management
- **installer_bp** - Installation and upgrade processes

### Security
- **firewall_bp** - Network firewall management
- **fail2ban_bp** - Intrusion prevention system
- **security_advisor_bp** - Security recommendations and monitoring
- **sandbox_bp** - Application sandboxing policies

## Storage & File Management

### Storage Management
- **storage_bp** - Drive mounting/unmounting, USB monitoring, network drives
- **raid_bp** - RAID management
- **diskrepair_bp** - Disk repair and maintenance
- **ssd_cache_bp** - SSD caching configuration

### File Sharing
- **sharing_bp** - File sharing configuration
- **dlna_bp** - DLNA media server
- **samba_bp** - Samba file sharing (not explicitly found, but likely integrated)
- **nfs_bp** - NFS file sharing (not explicitly found, but likely integrated)

### Backup & Recovery
- **backup_bp** - Backup and restore functionality
- **cloud_backup_bp** - Cloud backup integration
- **rollback_bp** - System rollback capabilities

## Applications & Services

### Media & Entertainment
- **video_station_bp** - Video station management
- **gallery_bp** - Photo gallery management
- **radio_music_bp** - Radio and music streaming
- **photos_ai_bp** - AI-powered photo analysis

### Development & Tools
- **editor_bp** - Code editor
- **packages_bp** - Package management
- **docker_bp** - Docker container management
- **vm_bp** - Virtual machine management
- **builder_bp** - Build system management

### Communication & Collaboration
- **mail_bp** - Mail server management
- **websites_bp** - Website hosting management
- **aichat_bp** - AI chat interface
- **familyhub_bp** - Family communication hub

### Monitoring & Logging
- **monitor_bp** - System monitoring
- **eventlog_bp** - Event logging
- **remote_log_bp** - Remote logging
- **notifications_bp** - Notification system

### Network Services
- **network_bp** - Network configuration and management
- **ddns_bp** - Dynamic DNS management
- **wireguard_bp** - WireGuard VPN management
- **domains_mgr_bp** - Domain management

## Utilities & Infrastructure

### Development Tools
- **api_docs_bp** - API documentation
- **app_manager_bp** - Application management
- **appstore_bp** - Application store integration
- **dashboard_bp** - System dashboard
- **resources_bp** - Resource usage monitoring

### System Utilities
- **printer_bp** - Printer management
- **flasher_bp** - Firmware flashing
- **ups_bp** - UPS management
- **encryption_bp** - File encryption
- **sync_drive_bp** - Drive synchronization
- **surveillance_bp** - Surveillance system
- **med_assistant_bp** - Medical assistant (AI assistant)
- **downloads_bp** - Download management
- **stickynotes_bp** - Digital sticky notes

### Integration & Specialized
- **antivirus_bp** - Antivirus scanning
- **doc_anonymizer_bp** - Document anonymization
- **surveillance_bp** - Surveillance system
- **pkg_registry_bp** - Package registry management

## Notes

- All blueprints follow the naming convention `{name}_bp` where `name` is the feature name
- Most blueprints use `url_prefix='/api/{name}'` for their API endpoints
- Blueprints are registered in `backend/app.py` in logical groups
- Common utilities are imported from `host.py` and `utils.py`
- Authentication decorators (`@require_auth`, `@admin_required`) are used across blueprints