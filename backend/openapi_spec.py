"""
Static OpenAPI 3.0 specification for EthOS NAS API.

Organised by tag with comprehensive endpoint coverage.
"""


def get_openapi_spec():
    """Return the complete OpenAPI 3.0 specification dict."""
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "EthOS NAS API",
            "description": (
                "REST API for EthOS Network Attached Storage. "
                "Provides endpoints for file management, storage, Docker, "
                "networking, user management, system administration, and more."
            ),
            "version": "1.0.0",
            "contact": {"name": "EthOS"},
        },
        "servers": [
            {
                "url": "/",
                "description": "This EthOS instance",
            }
        ],
        "tags": _tags(),
        "paths": _paths(),
        "components": _components(),
    }


# ── Tags ────────────────────────────────────────────────────────────────

def _tags():
    return [
        {"name": "Auth", "description": "Authentication, CSRF, session management"},
        {"name": "Users", "description": "System user and group management, privileges"},
        {"name": "TOTP", "description": "Two-factor authentication (TOTP / 2FA)"},
        {"name": "Files", "description": "File browsing, upload, download, trash, compression"},
        {"name": "Sharing", "description": "File and gallery sharing links"},
        {"name": "Storage", "description": "Drives, mount/unmount, SMART, disk analysis"},
        {"name": "Storage Services", "description": "Samba, NFS, DLNA, SFTP, WebDAV, FTP services"},
        {"name": "Docker", "description": "Containers, compose projects, images, volumes, networks"},
        {"name": "Network", "description": "Network interfaces, WiFi, hotspot"},
        {"name": "System", "description": "System info, version, services, language"},
        {"name": "Power", "description": "Power settings, reboot, shutdown"},
        {"name": "Settings", "description": "Hostname, SSL, SSH keys, domains, factory reset"},
        {"name": "Backup", "description": "Backup profiles, snapshots, restore, SSH servers"},
        {"name": "Gallery", "description": "Photo/video gallery, albums, EXIF, favourites"},
        {"name": "Tickets", "description": "Kanban project boards, tickets, comments, attachments"},
        {"name": "Notifications", "description": "Notification channels, triggers, history"},
        {"name": "Event Log", "description": "System event logging and statistics"},
        {"name": "Updates", "description": "System update check, apply, publish"},
        {"name": "Setup", "description": "Initial setup wizard"},
    ]


# ── Components ──────────────────────────────────────────────────────────

def _components():
    return {
        "securitySchemes": {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "JWT token obtained from POST /api/auth/login",
            },
            "CookieAuth": {
                "type": "apiKey",
                "in": "cookie",
                "name": "nas_token",
                "description": "Session cookie set on login",
            },
        },
        "schemas": _schemas(),
        "responses": {
            "Unauthorized": {
                "description": "Missing or invalid authentication",
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"error": {"type": "string", "example": "Unauthorized"}},
                }}},
            },
            "Forbidden": {
                "description": "Insufficient permissions",
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                }}},
            },
            "NotFound": {
                "description": "Resource not found",
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                }}},
            },
            "ServerError": {
                "description": "Internal server error",
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                }}},
            },
        },
    }


def _schemas():
    return {
        "User": {
            "type": "object",
            "properties": {
                "username": {"type": "string"},
                "role": {"type": "string", "enum": ["admin", "user"]},
                "groups": {"type": "array", "items": {"type": "string"}},
                "home_path": {"type": "string"},
            },
        },
        "SystemUser": {
            "type": "object",
            "properties": {
                "username": {"type": "string"},
                "uid": {"type": "integer"},
                "gid": {"type": "integer"},
                "home": {"type": "string"},
                "shell": {"type": "string"},
                "groups": {"type": "array", "items": {"type": "string"}},
                "nasos_user": {"type": "boolean"},
            },
        },
        "Group": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "gid": {"type": "integer"},
                "members": {"type": "array", "items": {"type": "string"}},
                "app_privileges": {"type": "array", "items": {"type": "string"}},
            },
        },
        "FileItem": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "is_dir": {"type": "boolean"},
                "size": {"type": "integer"},
                "modified": {"type": "number", "description": "Unix timestamp"},
                "permissions": {"type": "string"},
                "owner": {"type": "string"},
                "group": {"type": "string"},
                "locked": {"type": "boolean"},
                "protected": {"type": "boolean"},
            },
        },
        "Drive": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "size": {"type": "string"},
                "type": {"type": "string"},
                "fstype": {"type": "string"},
                "mountpoint": {"type": "string"},
                "label": {"type": "string"},
                "model": {"type": "string"},
                "transport": {"type": "string"},
                "uuid": {"type": "string"},
                "hotplug": {"type": "boolean"},
                "mountable": {"type": "boolean"},
                "temp_celsius": {"type": "number"},
                "health": {"type": "string"},
                "used": {"type": "integer"},
                "free": {"type": "integer"},
                "percent_used": {"type": "number"},
            },
        },
        "SambaShare": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "browseable": {"type": "boolean"},
                "writable": {"type": "boolean"},
                "guest_ok": {"type": "boolean"},
                "read_only": {"type": "boolean"},
            },
        },
        "Container": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "image": {"type": "string"},
                "status": {"type": "string"},
                "state": {"type": "string"},
                "ports": {"type": "string"},
                "created": {"type": "string"},
                "networks": {"type": "string"},
                "project": {"type": "string"},
                "service": {"type": "string"},
            },
        },
        "ComposeProject": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "compose_file": {"type": "string"},
                "status": {"type": "string"},
                "running": {"type": "integer"},
                "total": {"type": "integer"},
                "protected": {"type": "boolean"},
            },
        },
        "NetworkInterface": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "addresses": {"type": "array", "items": {"type": "object"}},
                "stats": {"type": "object"},
            },
        },
        "TicketProject": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "owner": {"type": "string"},
                "members": {"type": "array", "items": {"type": "string"}},
                "columns": {"type": "array", "items": {"type": "string"}},
                "color": {"type": "string"},
                "created": {"type": "string"},
                "updated": {"type": "string"},
                "ticket_count": {"type": "integer"},
            },
        },
        "Ticket": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "project_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "column": {"type": "string"},
                "priority": {"type": "string"},
                "assignee": {"type": "string"},
                "reporter": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "comments": {"type": "array", "items": {"type": "object"}},
                "attachments": {"type": "array", "items": {"type": "object"}},
                "order": {"type": "integer"},
                "created": {"type": "string"},
                "updated": {"type": "string"},
            },
        },
        "EventLogEntry": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "ts": {"type": "number"},
                "time": {"type": "string"},
                "category": {"type": "string", "enum": ["system", "files", "backup", "docker", "storage", "network", "printer", "security", "error"]},
                "level": {"type": "string", "enum": ["debug", "info", "warning", "error"]},
                "message": {"type": "string"},
                "details": {"type": "object"},
            },
        },
        "BackupProfile": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "source": {"type": "string"},
                "destination": {"type": "string"},
                "schedule": {"type": "object"},
                "encrypt": {"type": "boolean"},
            },
        },
        "NotificationConfig": {
            "type": "object",
            "properties": {
                "channels": {
                    "type": "object",
                    "description": "Channel configs: telegram, discord, gotify, ntfy, smtp, webhook",
                },
                "triggers": {
                    "type": "object",
                    "description": "Trigger flags: smart_warning, backup_failed, disk_full_90, etc.",
                },
            },
        },
        "SuccessResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "example": True},
            },
        },
        "ErrorResponse": {
            "type": "object",
            "properties": {
                "error": {"type": "string"},
            },
        },
    }


# ── Helpers ─────────────────────────────────────────────────────────────

_AUTH = [{"BearerAuth": []}, {"CookieAuth": []}]

_401 = {"$ref": "#/components/responses/Unauthorized"}
_403 = {"$ref": "#/components/responses/Forbidden"}
_404 = {"$ref": "#/components/responses/NotFound"}
_500 = {"$ref": "#/components/responses/ServerError"}


def _json(schema, desc=""):
    return {"content": {"application/json": {"schema": schema}}, "description": desc}


def _ok(props, desc="Success"):
    return _json({"type": "object", "properties": props}, desc)


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _arr(item):
    return {"type": "array", "items": item}


def _str(ex=None):
    s = {"type": "string"}
    if ex:
        s["example"] = ex
    return s


def _bool():
    return {"type": "boolean"}


def _int():
    return {"type": "integer"}


def _num():
    return {"type": "number"}


def _obj(props=None):
    o = {"type": "object"}
    if props:
        o["properties"] = props
    return o


# ── Paths ───────────────────────────────────────────────────────────────

def _paths():
    p = {}
    _auth_paths(p)
    _users_paths(p)
    _totp_paths(p)
    _files_paths(p)
    _sharing_paths(p)
    _storage_paths(p)
    _storage_services_paths(p)
    _docker_paths(p)
    _network_paths(p)
    _system_paths(p)
    _power_paths(p)
    _settings_paths(p)
    _backup_paths(p)
    _gallery_paths(p)
    _tickets_paths(p)
    _notifications_paths(p)
    _eventlog_paths(p)
    _updates_paths(p)
    _setup_paths(p)
    return p


# ── Auth ────────────────────────────────────────────────────────────────

def _auth_paths(p):
    p["/api/auth/login"] = {
        "post": {
            "tags": ["Auth"],
            "summary": "Log in",
            "description": "Authenticate with username/password. Returns JWT token and user info. Supports TOTP 2FA.",
            "requestBody": _json(_obj({
                "username": _str("admin"),
                "password": _str(),
                "totp_code": _str(),
                "backup_code": _str(),
            })),
            "responses": {
                "200": _ok({
                    "token": _str(),
                    "csrf_token": _str(),
                    "nas_name": _str(),
                    "user": _ref("User"),
                    "sudo_mode": _bool(),
                    "totp_required": _bool(),
                }, "Login successful or TOTP required"),
                "401": _401,
                "429": {"description": "Rate limited – too many attempts"},
            },
        }
    }
    p["/api/auth/verify"] = {
        "get": {
            "tags": ["Auth"],
            "summary": "Verify session",
            "description": "Check if the current token is valid and return user info.",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "valid": _bool(),
                    "nas_name": _str(),
                    "user": _ref("User"),
                    "sudo_mode": _bool(),
                    "csrf_token": _str(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/auth/logout"] = {
        "post": {
            "tags": ["Auth"],
            "summary": "Log out",
            "description": "Invalidate the current session token and clear cookies.",
            "security": _AUTH,
            "responses": {
                "200": _ok({"ok": _bool()}),
            },
        }
    }
    p["/api/auth/sudo"] = {
        "get": {
            "tags": ["Auth"],
            "summary": "Check sudo status",
            "description": "Return whether the current user has admin privileges.",
            "security": _AUTH,
            "responses": {
                "200": _ok({"ok": _bool(), "sudo_mode": _bool()}),
                "401": _401,
            },
        }
    }


# ── Users ───────────────────────────────────────────────────────────────

def _users_paths(p):
    p["/api/users/list"] = {
        "get": {
            "tags": ["Users"],
            "summary": "List users",
            "description": "List all system users (uid >= 1000 and root).",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_ref("SystemUser"))),
                "401": _401,
            },
        }
    }
    p["/api/users/create"] = {
        "post": {
            "tags": ["Users"],
            "summary": "Create user",
            "description": "Create a new system user with home directory.",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "username": _str("john"),
                "password": _str(),
                "shell": _str("/bin/bash"),
                "groups": _arr(_str()),
            })),
            "responses": {
                "200": _ok({"success": _bool(), "username": _str()}),
                "400": _json(_ref("ErrorResponse"), "Validation error"),
                "401": _401,
            },
        }
    }
    p["/api/users/update"] = {
        "post": {
            "tags": ["Users"],
            "summary": "Update user",
            "description": "Update password, shell, or groups for an existing user.",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "username": _str(),
                "password": _str(),
                "shell": _str(),
                "groups": _arr(_str()),
            })),
            "responses": {
                "200": _ok({"success": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/users/delete"] = {
        "post": {
            "tags": ["Users"],
            "summary": "Delete user",
            "security": _AUTH,
            "requestBody": _json(_obj({"username": _str()})),
            "responses": {
                "200": _ok({"success": _bool()}),
                "400": _json(_ref("ErrorResponse")),
                "401": _401,
            },
        }
    }
    p["/api/users/groups"] = {
        "get": {
            "tags": ["Users"],
            "summary": "List groups",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_ref("Group"))),
                "401": _401,
            },
        }
    }
    p["/api/users/groups/create"] = {
        "post": {
            "tags": ["Users"],
            "summary": "Create group",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "name": _str(),
                "app_privileges": _arr(_str()),
            })),
            "responses": {
                "200": _ok({"success": _bool(), "name": _str()}),
                "401": _401,
            },
        }
    }
    p["/api/users/groups/delete"] = {
        "post": {
            "tags": ["Users"],
            "summary": "Delete group",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str()})),
            "responses": {
                "200": _ok({"success": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/users/groups/members"] = {
        "post": {
            "tags": ["Users"],
            "summary": "Update group members",
            "description": "Add or remove members from a group.",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "group": _str(),
                "add": _arr(_str()),
                "remove": _arr(_str()),
            })),
            "responses": {
                "200": _ok({"success": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/users/privileges"] = {
        "get": {
            "tags": ["Users"],
            "summary": "Get privilege map",
            "description": "Return group-to-app privilege mappings.",
            "security": _AUTH,
            "responses": {
                "200": _json(_obj(), "Map of group name → array of app IDs"),
                "401": _401,
            },
        },
        "post": {
            "tags": ["Users"],
            "summary": "Set privileges",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "group": _str(),
                "apps": _arr(_str()),
            })),
            "responses": {
                "200": _ok({"success": _bool()}),
                "401": _401,
            },
        },
    }
    p["/api/users/privileges/for-user/{username}"] = {
        "get": {
            "tags": ["Users"],
            "summary": "Get user privileges",
            "parameters": [{"name": "username", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {
                "200": _ok({"all": _bool(), "apps": _arr(_str())}),
                "401": _401,
            },
        }
    }
    p["/api/users/auth/validate"] = {
        "post": {
            "tags": ["Users"],
            "summary": "Validate credentials",
            "description": "Validate username/password against the host system (PAM). No auth required.",
            "requestBody": _json(_obj({
                "username": _str(),
                "password": _str(),
            })),
            "responses": {
                "200": _ok({"valid": _bool(), "username": _str()}),
                "401": _json(_ref("ErrorResponse"), "Invalid credentials"),
            },
        }
    }


# ── TOTP ────────────────────────────────────────────────────────────────

def _totp_paths(p):
    p["/api/totp/status"] = {
        "get": {
            "tags": ["TOTP"],
            "summary": "TOTP status",
            "description": "Check whether TOTP 2FA is enabled for the current user.",
            "security": _AUTH,
            "responses": {
                "200": _ok({"enabled": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/totp/setup"] = {
        "post": {
            "tags": ["TOTP"],
            "summary": "Setup TOTP",
            "description": "Generate a TOTP secret, QR code, and backup codes.",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "secret": _str(),
                    "qr_code_base64": _str(),
                    "backup_codes": _arr(_str()),
                }),
                "401": _401,
            },
        }
    }
    p["/api/totp/verify"] = {
        "post": {
            "tags": ["TOTP"],
            "summary": "Verify and enable TOTP",
            "security": _AUTH,
            "requestBody": _json(_obj({"code": _str("123456")})),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "400": _json(_ref("ErrorResponse"), "Invalid code"),
                "401": _401,
            },
        }
    }
    p["/api/totp/disable"] = {
        "post": {
            "tags": ["TOTP"],
            "summary": "Disable TOTP",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "code": _str(),
                "backup_code": _str(),
            })),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        }
    }


# ── Files ───────────────────────────────────────────────────────────────

def _files_paths(p):
    p["/api/files/list"] = {
        "get": {
            "tags": ["Files"],
            "summary": "List directory",
            "parameters": [
                {"name": "path", "in": "query", "required": True, "schema": _str("/")},
            ],
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "path": _str(),
                    "items": _arr(_ref("FileItem")),
                }),
                "401": _401,
            },
        }
    }
    p["/api/files/search"] = {
        "get": {
            "tags": ["Files"],
            "summary": "Search files",
            "parameters": [
                {"name": "path", "in": "query", "required": True, "schema": _str("/")},
                {"name": "q", "in": "query", "required": True, "schema": _str()},
                {"name": "depth", "in": "query", "schema": _int()},
            ],
            "security": _AUTH,
            "responses": {
                "200": _ok({"items": _arr(_ref("FileItem")), "truncated": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/files/mkdir"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Create directory",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/files/rename"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Rename file or directory",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "new_name": _str()})),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/files/move"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Move file or directory",
            "security": _AUTH,
            "requestBody": _json(_obj({"src": _str(), "dest": _str()})),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/files/move-multi"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Move multiple items",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "sources": _arr(_str()),
                "dest": _str(),
                "on_conflict": _str("skip"),
            })),
            "responses": {
                "200": _ok({
                    "moved": _arr(_str()),
                    "skipped": _arr(_str()),
                    "errors": _arr(_str()),
                }),
                "401": _401,
            },
        }
    }
    p["/api/files/copy"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Copy files",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "sources": _arr(_str()),
                "dest": _str(),
                "on_conflict": _str("skip"),
            })),
            "responses": {
                "200": _ok({
                    "copied": _arr(_str()),
                    "skipped": _arr(_str()),
                    "errors": _arr(_str()),
                }),
                "401": _401,
            },
        }
    }
    p["/api/files/delete"] = {
        "delete": {
            "tags": ["Files"],
            "summary": "Delete files",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "paths": _arr(_str()),
                "permanent": _bool(),
            })),
            "responses": {
                "200": _ok({"deleted": _arr(_str()), "errors": _arr(_str())}),
                "401": _401,
            },
        }
    }
    p["/api/files/download"] = {
        "get": {
            "tags": ["Files"],
            "summary": "Download file",
            "parameters": [
                {"name": "path", "in": "query", "required": True, "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {
                "200": {"description": "File binary", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
                "401": _401,
                "404": _404,
            },
        }
    }
    p["/api/files/download-zip"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Create ZIP download",
            "description": "Start async ZIP creation of multiple files/folders.",
            "security": _AUTH,
            "requestBody": _json(_obj({"sources": _arr(_str())})),
            "responses": {
                "200": _ok({"async": _bool(), "download_id": _str()}),
                "401": _401,
            },
        }
    }
    p["/api/files/upload"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Upload files",
            "description": "Upload one or more files via multipart form data.",
            "security": _AUTH,
            "requestBody": {
                "content": {
                    "multipart/form-data": {
                        "schema": _obj({
                            "path": _str(),
                            "files": _arr({"type": "string", "format": "binary"}),
                        }),
                    }
                }
            },
            "responses": {
                "200": _ok({"uploaded": _arr(_str()), "errors": _arr(_str())}),
                "401": _401,
            },
        }
    }
    p["/api/files/compress"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Compress files",
            "description": "Create a ZIP or tar.gz archive (async).",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "sources": _arr(_str()),
                "format": _str("zip"),
                "name": _str(),
            })),
            "responses": {
                "200": _ok({"async": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/files/extract"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Extract archive",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {
                "200": _ok({"async": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/files/permissions"] = {
        "get": {
            "tags": ["Files"],
            "summary": "Get file permissions",
            "parameters": [{"name": "path", "in": "query", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "path": _str(),
                    "permissions_octal": _str("755"),
                    "permissions_symbolic": _str("rwxr-xr-x"),
                    "owner": _str(),
                    "group": _str(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/files/chmod"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Change file permissions",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "mode": _str("755")})),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/files/chown"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Change file ownership",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "owner": _str(), "group": _str()})),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        }
    }
    # Trash
    p["/api/files/trash"] = {
        "get": {
            "tags": ["Files"],
            "summary": "List trash",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "items": _arr(_obj({
                        "trash_id": _str(),
                        "original_path": _str(),
                        "name": _str(),
                        "size": _int(),
                        "deleted_date": _str(),
                        "days_left": _int(),
                    })),
                    "retention_days": _int(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/files/trash/restore"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Restore from trash",
            "security": _AUTH,
            "requestBody": _json(_obj({"trash_ids": _arr(_str())})),
            "responses": {
                "200": _ok({"restored": _arr(_str()), "errors": _arr(_str())}),
                "401": _401,
            },
        }
    }
    p["/api/files/trash/empty"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Empty trash",
            "security": _AUTH,
            "responses": {
                "200": _ok({"removed": _int()}),
                "401": _401,
            },
        }
    }
    # Favourites
    p["/api/files/favorites"] = {
        "get": {
            "tags": ["Files"],
            "summary": "List file favourites",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_obj({"path": _str(), "label": _str()}))),
                "401": _401,
            },
        },
        "post": {
            "tags": ["Files"],
            "summary": "Add favourite",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "label": _str()})),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        },
        "delete": {
            "tags": ["Files"],
            "summary": "Remove favourite",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        },
    }
    # Folder password
    p["/api/files/folder-password"] = {
        "get": {
            "tags": ["Files"],
            "summary": "List password-protected folders",
            "security": _AUTH,
            "responses": {
                "200": _ok({"folders": _arr(_str())}),
                "401": _401,
            },
        },
        "post": {
            "tags": ["Files"],
            "summary": "Set folder password",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "password": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
        "delete": {
            "tags": ["Files"],
            "summary": "Remove folder password",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "password": _str(), "force": _bool()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/files/folder-unlock"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Unlock folder",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "password": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/files/folder-lock"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Lock folder",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    # Operation status
    p["/api/files/operation-status"] = {
        "get": {
            "tags": ["Files"],
            "summary": "File operation status",
            "description": "Get status of running async file operations (copy, compress, extract).",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "active": _bool(),
                    "operation": _str(),
                    "progress": _obj(),
                    "result": _obj(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/files/cancel-operation"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Cancel file operation",
            "security": _AUTH,
            "responses": {"200": _ok({"cancelled": _bool()}), "401": _401},
        }
    }
    # Duplicates
    p["/api/files/duplicates/scan"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Start duplicate scan",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "paths": _arr(_str()),
                "mode": _str("hash"),
                "threshold": _int(),
            })),
            "responses": {
                "200": _ok({"scan_id": _str(), "status": _str("started")}),
                "401": _401,
            },
        }
    }
    p["/api/files/duplicates/status"] = {
        "get": {
            "tags": ["Files"],
            "summary": "Duplicate scan status",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "running": _bool(),
                    "phase": _str(),
                    "scanned": _int(),
                    "total": _int(),
                    "found_groups": _int(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/files/duplicates/results"] = {
        "get": {
            "tags": ["Files"],
            "summary": "Duplicate scan results",
            "security": _AUTH,
            "responses": {
                "200": _ok({"groups": _arr(_obj()), "ready": _bool(), "total_groups": _int()}),
                "401": _401,
            },
        }
    }
    # Preview
    p["/api/files/preview"] = {
        "get": {
            "tags": ["Files"],
            "summary": "Preview / thumbnail",
            "parameters": [
                {"name": "path", "in": "query", "required": True, "schema": _str()},
                {"name": "w", "in": "query", "schema": _int()},
                {"name": "h", "in": "query", "schema": _int()},
            ],
            "security": _AUTH,
            "responses": {
                "200": {"description": "Image data", "content": {"image/*": {"schema": {"type": "string", "format": "binary"}}}},
                "401": _401,
                "404": _404,
            },
        }
    }
    # Remote transfer
    p["/api/files/remote-servers"] = {
        "get": {
            "tags": ["Files"],
            "summary": "List remote transfer servers",
            "security": _AUTH,
            "responses": {
                "200": _ok({"servers": _arr(_obj({
                    "id": _str(), "name": _str(), "host": _str(),
                    "port": _int(), "username": _str(), "remote_path": _str(),
                }))}),
                "401": _401,
            },
        }
    }
    p["/api/files/transfer-remote"] = {
        "post": {
            "tags": ["Files"],
            "summary": "Transfer files to remote server",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "server_id": _str(),
                "paths": _arr(_str()),
                "remote_path": _str(),
            })),
            "responses": {
                "200": _ok({"async": _bool()}),
                "401": _401,
            },
        }
    }


# ── Sharing ─────────────────────────────────────────────────────────────

def _sharing_paths(p):
    p["/api/files/shares"] = {
        "get": {
            "tags": ["Sharing"],
            "summary": "List my shares",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_obj({
                    "token": _str(), "path": _str(), "name": _str(),
                    "is_dir": _bool(), "created": _str(), "expires": _str(),
                    "creator": _str(), "shared_with": _arr(_str()),
                }))),
                "401": _401,
            },
        },
        "post": {
            "tags": ["Sharing"],
            "summary": "Create share link",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "path": _str(),
                "expires_hours": _int(),
                "shared_with": _arr(_str()),
            })),
            "responses": {
                "200": _json(_obj({
                    "token": _str(), "path": _str(), "name": _str(),
                    "is_dir": _bool(), "created": _str(),
                })),
                "401": _401,
            },
        },
    }
    p["/api/files/shares/received"] = {
        "get": {
            "tags": ["Sharing"],
            "summary": "List received shares",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_obj())),
                "401": _401,
            },
        }
    }
    p["/api/files/shares/{token}"] = {
        "delete": {
            "tags": ["Sharing"],
            "summary": "Delete share",
            "parameters": [{"name": "token", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {
                "200": _ok({"ok": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/public/share/{token}"] = {
        "get": {
            "tags": ["Sharing"],
            "summary": "Access public share",
            "description": "Get share info and file listing. No auth required for public shares.",
            "parameters": [
                {"name": "token", "in": "path", "required": True, "schema": _str()},
                {"name": "path", "in": "query", "schema": _str()},
            ],
            "responses": {
                "200": _ok({"share": _obj(), "items": _arr(_ref("FileItem")), "path": _str()}),
                "404": _404,
            },
        }
    }
    p["/api/public/share/{token}/download"] = {
        "get": {
            "tags": ["Sharing"],
            "summary": "Download shared file",
            "parameters": [
                {"name": "token", "in": "path", "required": True, "schema": _str()},
                {"name": "path", "in": "query", "schema": _str()},
            ],
            "responses": {
                "200": {"description": "File or ZIP", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
                "404": _404,
            },
        }
    }


# ── Storage ─────────────────────────────────────────────────────────────

def _storage_paths(p):
    p["/api/storage/drives"] = {
        "get": {
            "tags": ["Storage"],
            "summary": "List drives",
            "description": "List all block devices with size, filesystem, mount status, SMART info, and usage.",
            "security": _AUTH,
            "responses": {
                "200": _ok({"blockdevices": _arr(_ref("Drive"))}),
                "401": _401,
            },
        }
    }
    p["/api/storage/mount"] = {
        "post": {
            "tags": ["Storage"],
            "summary": "Mount drive",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "drive": _str("sdb1"),
                "path": _str("/mnt/data"),
                "auto_mount": _bool(),
            })),
            "responses": {
                "200": _ok({"success": _bool(), "device": _str(), "mountpoint": _str(), "fstype": _str(), "auto_mount": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/storage/unmount"] = {
        "post": {
            "tags": ["Storage"],
            "summary": "Unmount drive",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {
                "200": _ok({"success": _bool(), "mountpoint": _str()}),
                "401": _401,
            },
        }
    }
    p["/api/storage/eject"] = {
        "post": {
            "tags": ["Storage"],
            "summary": "Eject USB drive",
            "security": _AUTH,
            "requestBody": _json(_obj({"disk": _str()})),
            "responses": {
                "200": _ok({"success": _bool(), "disk": _str(), "message": _str()}),
                "401": _401,
            },
        }
    }
    p["/api/storage/auto-mount"] = {
        "post": {
            "tags": ["Storage"],
            "summary": "Toggle auto-mount",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "enable": _bool()})),
            "responses": {
                "200": _ok({"ok": _bool(), "auto_mount": _bool()}),
                "401": _401,
            },
        }
    }
    p["/api/storage/smart"] = {
        "get": {
            "tags": ["Storage"],
            "summary": "SMART info",
            "parameters": [{"name": "disk", "in": "query", "required": True, "schema": _str("sda")}],
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "disk": _str(), "available": _bool(), "health": _str(),
                    "temperature": _int(), "power_on_hours": _int(),
                    "model": _str(), "serial": _str(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/storage/format"] = {
        "post": {
            "tags": ["Storage"],
            "summary": "Format drive",
            "description": "Format a drive with the specified filesystem. Returns SSE progress stream.",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "drive": _str(), "fstype": _str("ext4"), "label": _str(),
            })),
            "responses": {
                "200": {"description": "SSE event stream"},
                "401": _401,
            },
        }
    }
    p["/api/storage/format/options"] = {
        "get": {
            "tags": ["Storage"],
            "summary": "Supported filesystems",
            "security": _AUTH,
            "responses": {
                "200": _ok({"filesystems": _arr(_obj({"name": _str(), "fstype": _str()}))}),
                "401": _401,
            },
        }
    }
    p["/api/storage/relabel"] = {
        "post": {
            "tags": ["Storage"],
            "summary": "Change drive label",
            "security": _AUTH,
            "requestBody": _json(_obj({"drive": _str(), "label": _str()})),
            "responses": {
                "200": _ok({"success": _bool(), "device": _str(), "label": _str()}),
                "401": _401,
            },
        }
    }
    p["/api/storage/partition"] = {
        "post": {
            "tags": ["Storage"],
            "summary": "Partition disk",
            "description": "Repartition disk into multiple partitions. Returns SSE progress stream.",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "disk": _str(),
                "partitions": _arr(_obj({
                    "size_mb": _int(), "fstype": _str(), "label": _str(),
                })),
            })),
            "responses": {"200": {"description": "SSE event stream"}, "401": _401},
        }
    }
    p["/api/storage/merge"] = {
        "post": {
            "tags": ["Storage"],
            "summary": "Merge partitions",
            "description": "Merge all partitions into one. Returns SSE progress stream.",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "disk": _str(), "fstype": _str("ext4"), "label": _str(),
            })),
            "responses": {"200": {"description": "SSE event stream"}, "401": _401},
        }
    }
    p["/api/storage/analyze"] = {
        "get": {
            "tags": ["Storage"],
            "summary": "Analyse disk usage",
            "parameters": [
                {"name": "path", "in": "query", "required": True, "schema": _str()},
                {"name": "limit", "in": "query", "schema": _int()},
            ],
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "path": _str(), "total_size": _int(),
                    "entries": _arr(_obj({"path": _str(), "name": _str(), "size": _int(), "percent": _num()})),
                }),
                "401": _401,
            },
        }
    }
    p["/api/storage/keepalive"] = {
        "get": {
            "tags": ["Storage"],
            "summary": "List keep-alive drives",
            "security": _AUTH,
            "responses": {"200": _ok({"drives": _obj()}), "401": _401},
        },
        "post": {
            "tags": ["Storage"],
            "summary": "Toggle drive keep-alive",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "drive": _str(), "mountpoint": _str(), "enable": _bool(),
            })),
            "responses": {"200": _ok({"success": _bool(), "enabled": _bool(), "drive": _str()}), "401": _401},
        },
    }


# ── Storage Services ────────────────────────────────────────────────────

def _storage_services_paths(p):
    for svc, tag_label in [
        ("samba", "Samba (SMB)"), ("nfs", "NFS"), ("dlna", "DLNA"),
        ("sftp", "SFTP"), ("webdav", "WebDAV"), ("ftp", "FTP"),
    ]:
        prefix = f"/api/storage/{svc}"
        tag = "Storage Services"

        p[f"{prefix}/status"] = {
            "get": {
                "tags": [tag],
                "summary": f"{tag_label} status",
                "security": _AUTH,
                "responses": {"200": _ok({"installed": _bool(), "running": _bool()}), "401": _401},
            }
        }
        p[f"{prefix}/install"] = {
            "post": {
                "tags": [tag],
                "summary": f"Install {tag_label}",
                "security": _AUTH,
                "responses": {"200": _ok({"ok": _bool(), "message": _str()}), "401": _401},
            }
        }
        p[f"{prefix}/pkg-status"] = {
            "get": {
                "tags": [tag],
                "summary": f"{tag_label} package status",
                "security": _AUTH,
                "responses": {"200": _ok({"installed": _bool()}), "401": _401},
            }
        }
        p[f"{prefix}/pkg-install"] = {
            "post": {
                "tags": [tag],
                "summary": f"Install {tag_label} package",
                "security": _AUTH,
                "responses": {"200": _ok({"ok": _bool()}), "401": _401},
            }
        }
        p[f"{prefix}/pkg-uninstall"] = {
            "post": {
                "tags": [tag],
                "summary": f"Uninstall {tag_label} package",
                "security": _AUTH,
                "responses": {"200": _ok({"ok": _bool()}), "401": _401},
            }
        }

    # Samba-specific
    p["/api/storage/samba/shares"] = {
        "get": {
            "tags": ["Storage Services"],
            "summary": "List Samba shares",
            "security": _AUTH,
            "responses": {"200": _ok({"shares": _arr(_ref("SambaShare"))}), "401": _401},
        }
    }
    p["/api/storage/samba/share"] = {
        "post": {
            "tags": ["Storage Services"],
            "summary": "Add Samba share",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "name": _str(), "path": _str(),
                "guest_ok": _bool(), "writable": _bool(),
            })),
            "responses": {"200": _ok({"success": _bool(), "share_name": _str(), "path": _str()}), "401": _401},
        },
        "delete": {
            "tags": ["Storage Services"],
            "summary": "Remove Samba share",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str()})),
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        },
    }
    p["/api/storage/samba/password"] = {
        "post": {
            "tags": ["Storage Services"],
            "summary": "Set Samba password",
            "security": _AUTH,
            "requestBody": _json(_obj({"username": _str(), "password": _str()})),
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        }
    }
    # NFS-specific
    p["/api/storage/nfs/exports"] = {
        "get": {
            "tags": ["Storage Services"],
            "summary": "List NFS exports",
            "security": _AUTH,
            "responses": {"200": _ok({"exports": _arr(_obj({"path": _str(), "clients": _str(), "perms": _str()}))}), "401": _401},
        }
    }
    p["/api/storage/nfs/export"] = {
        "post": {
            "tags": ["Storage Services"],
            "summary": "Add NFS export",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "clients": _str(), "perms": _str()})),
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        },
        "delete": {
            "tags": ["Storage Services"],
            "summary": "Remove NFS export",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        },
    }
    # WebDAV shares
    p["/api/storage/webdav/shares"] = {
        "get": {
            "tags": ["Storage Services"],
            "summary": "List WebDAV shares",
            "security": _AUTH,
            "responses": {"200": _ok({"shares": _arr(_obj({"name": _str(), "path": _str(), "read_only": _bool()}))}), "401": _401},
        }
    }
    p["/api/storage/webdav/share"] = {
        "post": {
            "tags": ["Storage Services"],
            "summary": "Add WebDAV share",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str(), "path": _str(), "read_only": _bool()})),
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        },
        "delete": {
            "tags": ["Storage Services"],
            "summary": "Remove WebDAV share",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str()})),
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        },
    }
    # DLNA config
    p["/api/storage/dlna/config"] = {
        "get": {
            "tags": ["Storage Services"],
            "summary": "Get DLNA config",
            "security": _AUTH,
            "responses": {"200": _ok({"config": _obj()}), "401": _401},
        },
        "post": {
            "tags": ["Storage Services"],
            "summary": "Update DLNA config",
            "security": _AUTH,
            "requestBody": _json(_obj({"friendly_name": _str(), "media_dirs": _arr(_str())})),
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        },
    }
    p["/api/storage/dlna/rescan"] = {
        "post": {
            "tags": ["Storage Services"],
            "summary": "Rescan DLNA library",
            "security": _AUTH,
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        }
    }
    # SFTP toggle
    p["/api/storage/sftp/toggle"] = {
        "post": {
            "tags": ["Storage Services"],
            "summary": "Toggle SFTP",
            "security": _AUTH,
            "requestBody": _json(_obj({"enable": _bool()})),
            "responses": {"200": _ok({"ok": _bool(), "enabled": _bool()}), "401": _401},
        }
    }
    p["/api/storage/sftp/users"] = {
        "get": {
            "tags": ["Storage Services"],
            "summary": "List SFTP users",
            "security": _AUTH,
            "responses": {"200": _ok({"users": _arr(_str())}), "401": _401},
        }
    }
    # FTP toggle
    p["/api/storage/ftp/toggle"] = {
        "post": {
            "tags": ["Storage Services"],
            "summary": "Toggle FTP",
            "security": _AUTH,
            "requestBody": _json(_obj({"enable": _bool()})),
            "responses": {"200": _ok({"ok": _bool(), "enabled": _bool()}), "401": _401},
        }
    }


# ── Docker ──────────────────────────────────────────────────────────────

def _docker_paths(p):
    p["/api/docker/status"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "Docker availability",
            "security": _AUTH,
            "responses": {"200": _ok({"available": _bool(), "message": _str()}), "401": _401},
        }
    }
    p["/api/docker/install"] = {
        "post": {
            "tags": ["Docker"],
            "summary": "Install Docker",
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool(), "message": _str()}), "401": _401},
        }
    }
    p["/api/docker/containers"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "List containers",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_ref("Container"))), "401": _401},
        }
    }
    p["/api/docker/containers/{container_id}/action"] = {
        "post": {
            "tags": ["Docker"],
            "summary": "Container action",
            "description": "Perform action: start, stop, restart, pause, unpause, remove, kill.",
            "parameters": [{"name": "container_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({"action": _str("restart")})),
            "responses": {"200": _ok({"ok": _bool()}), "400": _json(_ref("ErrorResponse")), "401": _401},
        }
    }
    p["/api/docker/containers/{container_id}/logs"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "Container logs",
            "parameters": [
                {"name": "container_id", "in": "path", "required": True, "schema": _str()},
                {"name": "lines", "in": "query", "schema": _int()},
                {"name": "since", "in": "query", "schema": _str()},
                {"name": "search", "in": "query", "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {"200": _ok({"logs": _arr(_str()), "total": _int()}), "401": _401},
        }
    }
    p["/api/docker/containers/{container_id}/inspect"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "Inspect container",
            "parameters": [{"name": "container_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        }
    }
    p["/api/docker/containers/{container_id}/stats"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "Container stats",
            "parameters": [{"name": "container_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"cpu": _num(), "mem": _int(), "mem_perc": _num(), "pids": _int()}), "401": _401},
        }
    }
    p["/api/docker/projects"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "List compose projects",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_ref("ComposeProject"))), "401": _401},
        },
        "post": {
            "tags": ["Docker"],
            "summary": "Create compose project",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str(), "content": _str()})),
            "responses": {"200": _ok({"ok": _bool(), "path": _str()}), "401": _401},
        },
    }
    p["/api/docker/projects/{project_name}/action"] = {
        "post": {
            "tags": ["Docker"],
            "summary": "Compose project action",
            "description": "Actions: up, down, restart, pull, build, stop, start.",
            "parameters": [{"name": "project_name", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({"action": _str("up")})),
            "responses": {"200": _ok({"ok": _bool(), "output": _str()}), "401": _401},
        }
    }
    p["/api/docker/projects/{project_name}"] = {
        "delete": {
            "tags": ["Docker"],
            "summary": "Delete compose project",
            "parameters": [{"name": "project_name", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/docker/projects/{project_name}/logs"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "Compose project logs",
            "parameters": [
                {"name": "project_name", "in": "path", "required": True, "schema": _str()},
                {"name": "lines", "in": "query", "schema": _int()},
                {"name": "service", "in": "query", "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {"200": _ok({"logs": _arr(_str()), "total": _int()}), "401": _401},
        }
    }
    p["/api/docker/projects/{project_name}/compose"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "Read compose file",
            "parameters": [{"name": "project_name", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"content": _str(), "filename": _str()}), "401": _401},
        },
        "put": {
            "tags": ["Docker"],
            "summary": "Save compose file",
            "parameters": [{"name": "project_name", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({"content": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/docker/images"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "List images",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj({"id": _str(), "repository": _str(), "tag": _str(), "size": _int(), "created": _str()}))), "401": _401},
        }
    }
    p["/api/docker/images/{image_id}"] = {
        "delete": {
            "tags": ["Docker"],
            "summary": "Delete image",
            "parameters": [
                {"name": "image_id", "in": "path", "required": True, "schema": _str()},
                {"name": "force", "in": "query", "schema": _bool()},
            ],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/docker/images/prune"] = {
        "post": {
            "tags": ["Docker"],
            "summary": "Prune unused images",
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool(), "output": _str()}), "401": _401},
        }
    }
    p["/api/docker/networks"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "List networks",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj({"id": _str(), "name": _str(), "driver": _str(), "scope": _str()}))), "401": _401},
        }
    }
    p["/api/docker/volumes"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "List volumes",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj({"name": _str(), "driver": _str(), "mountpoint": _str()}))), "401": _401},
        }
    }
    p["/api/docker/volumes/prune"] = {
        "post": {
            "tags": ["Docker"],
            "summary": "Prune unused volumes",
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool(), "output": _str()}), "401": _401},
        }
    }
    p["/api/docker/system"] = {
        "get": {
            "tags": ["Docker"],
            "summary": "Docker system info",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "version": _str(), "containers": _int(),
                    "containers_running": _int(), "images": _int(),
                    "storage_driver": _str(), "disk_usage": _obj(),
                }),
                "401": _401,
            },
        }
    }


# ── Network ─────────────────────────────────────────────────────────────

def _network_paths(p):
    p["/api/network/interfaces"] = {
        "get": {
            "tags": ["Network"],
            "summary": "List network interfaces",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "interfaces": _arr(_ref("NetworkInterface")),
                    "gateway": _obj(),
                    "dns": _arr(_str()),
                    "hostname": _str(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/network/interface/{name}/up"] = {
        "post": {
            "tags": ["Network"],
            "summary": "Bring interface up",
            "parameters": [{"name": "name", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        }
    }
    p["/api/network/interface/{name}/down"] = {
        "post": {
            "tags": ["Network"],
            "summary": "Bring interface down",
            "parameters": [{"name": "name", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        }
    }
    p["/api/network/wifi/scan"] = {
        "get": {
            "tags": ["Network"],
            "summary": "Scan WiFi networks",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "interface": _str(),
                    "networks": _arr(_obj({
                        "ssid": _str(), "signal": _int(),
                        "security": _str(), "frequency": _str(), "band": _str(),
                    })),
                }),
                "401": _401,
            },
        }
    }
    p["/api/network/wifi/saved"] = {
        "get": {
            "tags": ["Network"],
            "summary": "Saved WiFi connections",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj({"name": _str()}))), "401": _401},
        }
    }
    p["/api/network/wifi/connect"] = {
        "post": {
            "tags": ["Network"],
            "summary": "Connect to WiFi",
            "security": _AUTH,
            "requestBody": _json(_obj({"ssid": _str(), "password": _str()})),
            "responses": {
                "200": _ok({"success": _bool(), "message": _str(), "new_ip": _str()}),
                "401": _401,
            },
        }
    }
    p["/api/network/wifi/disconnect"] = {
        "post": {
            "tags": ["Network"],
            "summary": "Disconnect WiFi",
            "security": _AUTH,
            "responses": {"200": _ok({"success": _bool(), "message": _str()}), "401": _401},
        }
    }
    p["/api/network/wifi/forget"] = {
        "post": {
            "tags": ["Network"],
            "summary": "Forget WiFi network",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str()})),
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        }
    }
    p["/api/network/wifi/status"] = {
        "get": {
            "tags": ["Network"],
            "summary": "WiFi connection status",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "interface": _str(),
                    "connected": _obj({"ssid": _str(), "signal": _int(), "security": _str()}),
                    "ip_address": _str(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/network/ap/status"] = {
        "get": {
            "tags": ["Network"],
            "summary": "Hotspot status",
            "security": _AUTH,
            "responses": {
                "200": _ok({"active": _bool(), "ssid": _str(), "interface": _str(), "ip": _str(), "clients": _int()}),
                "401": _401,
            },
        }
    }
    p["/api/network/ap/start"] = {
        "post": {
            "tags": ["Network"],
            "summary": "Start hotspot",
            "security": _AUTH,
            "responses": {"200": _ok({"success": _bool(), "message": _str()}), "401": _401},
        }
    }
    p["/api/network/ap/stop"] = {
        "post": {
            "tags": ["Network"],
            "summary": "Stop hotspot",
            "security": _AUTH,
            "responses": {"200": _ok({"success": _bool(), "message": _str()}), "401": _401},
        }
    }


# ── System ──────────────────────────────────────────────────────────────

def _system_paths(p):
    p["/api/system/info"] = {
        "get": {
            "tags": ["System"],
            "summary": "System information",
            "description": "CPU, memory, disk, network, uptime, temperature, UPS status.",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "hostname": _str(), "cpu_percent": _num(),
                    "cpu_count": _int(), "memory": _obj(),
                    "disks": _arr(_obj()), "uptime": _num(),
                    "cpu_temp": _num(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/system/version"] = {
        "get": {
            "tags": ["System"],
            "summary": "EthOS version",
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        }
    }
    p["/api/services/list"] = {
        "get": {
            "tags": ["System"],
            "summary": "List system services",
            "security": _AUTH,
            "responses": {
                "200": _ok({"services": _arr(_obj({
                    "id": _str(), "name": _str(), "active": _bool(),
                    "state": _str(), "enabled": _bool(), "installed": _bool(),
                }))}),
                "401": _401,
            },
        }
    }
    p["/api/services/action"] = {
        "post": {
            "tags": ["System"],
            "summary": "Service action",
            "description": "Actions: start, stop, restart, enable, disable, uninstall.",
            "security": _AUTH,
            "requestBody": _json(_obj({"service": _str(), "action": _str("restart")})),
            "responses": {"200": _ok({"ok": _bool(), "status": _str(), "message": _str()}), "401": _401},
        }
    }
    p["/api/services/logs"] = {
        "get": {
            "tags": ["System"],
            "summary": "Service logs",
            "parameters": [
                {"name": "service", "in": "query", "required": True, "schema": _str()},
                {"name": "lines", "in": "query", "schema": _int()},
            ],
            "security": _AUTH,
            "responses": {"200": _ok({"logs": _str(), "service": _str()}), "401": _401},
        }
    }
    p["/api/language"] = {
        "get": {
            "tags": ["System"],
            "summary": "Get language",
            "responses": {"200": _ok({"language": _str("en")})},
        },
        "post": {
            "tags": ["System"],
            "summary": "Set language",
            "requestBody": _json(_obj({"language": _str("en")})),
            "responses": {"200": _ok({"ok": _bool()})},
        },
    }
    p["/api/apps"] = {
        "get": {
            "tags": ["System"],
            "summary": "List apps",
            "description": "Return all available EthOS applications.",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        }
    }
    p["/api/ethos/identify"] = {
        "get": {
            "tags": ["System"],
            "summary": "Identify NAS",
            "description": "Discovery endpoint returning NAS name, version, and capabilities. No auth required.",
            "responses": {"200": _json(_obj())},
        }
    }


# ── Power ───────────────────────────────────────────────────────────────

def _power_paths(p):
    p["/api/power/status"] = {
        "get": {
            "tags": ["Power"],
            "summary": "Power settings",
            "description": "Get WOL, CPU governor, HDD spin-down, and schedule settings.",
            "security": _AUTH,
            "responses": {
                "200": _ok({"wol": _obj(), "schedule": _obj(), "hdd": _obj(), "cpu": _obj()}),
                "401": _401,
            },
        }
    }
    p["/api/power/save"] = {
        "post": {
            "tags": ["Power"],
            "summary": "Save power settings",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "wol_enabled": _bool(),
                "schedule": _obj(),
                "hdd_spindown": _int(),
                "cpu_governor": _str(),
            })),
            "responses": {"200": _ok({"status": _str("ok")}), "401": _401},
        }
    }


# ── Settings ────────────────────────────────────────────────────────────

def _settings_paths(p):
    p["/api/settings"] = {
        "get": {
            "tags": ["Settings"],
            "summary": "Get settings",
            "description": "Hostname, port, NAS name, uptime, and other system settings.",
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        },
        "post": {
            "tags": ["Settings"],
            "summary": "Update settings",
            "security": _AUTH,
            "requestBody": _json(_obj()),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/settings/change-password"] = {
        "post": {
            "tags": ["Settings"],
            "summary": "Change password",
            "security": _AUTH,
            "requestBody": _json(_obj({"current_password": _str(), "new_password": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/settings/timezones"] = {
        "get": {
            "tags": ["Settings"],
            "summary": "List time zones",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_str())), "401": _401},
        }
    }
    p["/api/settings/restart"] = {
        "post": {
            "tags": ["Settings"],
            "summary": "Restart EthOS service",
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    # SSL
    p["/api/settings/ssl/status"] = {
        "get": {
            "tags": ["Settings"],
            "summary": "SSL certificate status",
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        }
    }
    p["/api/settings/ssl/obtain"] = {
        "post": {
            "tags": ["Settings"],
            "summary": "Obtain Let's Encrypt certificate",
            "security": _AUTH,
            "requestBody": _json(_obj({"domain": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401, "500": _500},
        }
    }
    p["/api/settings/ssl/enable"] = {
        "post": {
            "tags": ["Settings"],
            "summary": "Enable or disable HTTPS",
            "security": _AUTH,
            "requestBody": _json(_obj({"enable": _bool()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    # SSH keys
    p["/api/settings/ssh-keys"] = {
        "get": {
            "tags": ["Settings"],
            "summary": "List SSH keys",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj({"name": _str()}))), "401": _401},
        }
    }
    p["/api/settings/ssh-keys/generate"] = {
        "post": {
            "tags": ["Settings"],
            "summary": "Generate SSH key pair",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/settings/ssh-keys/{name}/public"] = {
        "get": {
            "tags": ["Settings"],
            "summary": "Get public key",
            "parameters": [{"name": "name", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _json(_obj({"public_key": _str()})), "401": _401},
        }
    }
    # Domains / nginx proxy
    p["/api/settings/domains"] = {
        "get": {
            "tags": ["Settings"],
            "summary": "List reverse proxy domains",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        },
        "post": {
            "tags": ["Settings"],
            "summary": "Add domain",
            "security": _AUTH,
            "requestBody": _json(_obj()),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/settings/factory-reset"] = {
        "post": {
            "tags": ["Settings"],
            "summary": "Factory reset",
            "description": "⚠️ Destructive: resets system to defaults.",
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401, "403": _403},
        }
    }


# ── Backup ──────────────────────────────────────────────────────────────

def _backup_paths(p):
    p["/api/backup/profiles"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "List backup profiles",
            "security": _AUTH,
            "responses": {"200": _ok({"profiles": _arr(_ref("BackupProfile"))}), "401": _401},
        },
        "post": {
            "tags": ["Backup"],
            "summary": "Create backup profile",
            "security": _AUTH,
            "requestBody": _json(_ref("BackupProfile")),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/backup/profiles/{id}"] = {
        "put": {
            "tags": ["Backup"],
            "summary": "Update backup profile",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_ref("BackupProfile")),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
        "delete": {
            "tags": ["Backup"],
            "summary": "Delete backup profile",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/backup/profiles/{id}/run"] = {
        "post": {
            "tags": ["Backup"],
            "summary": "Run backup profile",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/backup/profiles/{id}/schedule"] = {
        "put": {
            "tags": ["Backup"],
            "summary": "Update profile schedule",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({"schedule": _obj()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/backup/backups"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "List backups",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        }
    }
    p["/api/backup/backups/{filename}"] = {
        "delete": {
            "tags": ["Backup"],
            "summary": "Delete backup",
            "parameters": [{"name": "filename", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/backup/backup"] = {
        "post": {
            "tags": ["Backup"],
            "summary": "Start backup",
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/backup/restore"] = {
        "post": {
            "tags": ["Backup"],
            "summary": "Start restore",
            "security": _AUTH,
            "requestBody": _json(_obj({"filename": _str(), "destination": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/backup/status"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "Backup operation status",
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        }
    }
    p["/api/backup/history"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "Backup history",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        }
    }
    p["/api/backup/ssh-servers"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "List SSH servers",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        },
        "post": {
            "tags": ["Backup"],
            "summary": "Add SSH server",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str(), "host": _str(), "port": _int(), "username": _str(), "password": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/backup/ssh-servers/{id}"] = {
        "delete": {
            "tags": ["Backup"],
            "summary": "Delete SSH server",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/backup/ssh-servers/test"] = {
        "post": {
            "tags": ["Backup"],
            "summary": "Test SSH connection",
            "security": _AUTH,
            "requestBody": _json(_obj({"host": _str(), "port": _int(), "username": _str(), "password": _str()})),
            "responses": {"200": _ok({"ok": _bool(), "message": _str()}), "401": _401},
        }
    }
    # Snapshots
    p["/api/backup/snapshots"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "List snapshots",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        },
        "post": {
            "tags": ["Backup"],
            "summary": "Create snapshot",
            "security": _AUTH,
            "requestBody": _json(_obj({"source": _str(), "name": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/backup/snapshots/{id}"] = {
        "delete": {
            "tags": ["Backup"],
            "summary": "Delete snapshot",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/backup/snapshots/{id}/restore"] = {
        "post": {
            "tags": ["Backup"],
            "summary": "Restore snapshot",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/backup/snapshots/{id}/browse"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "Browse snapshot files",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        }
    }
    p["/api/backup/snapshots/status"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "Snapshot operation status",
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        }
    }
    # Browse & paths
    p["/api/backup/browse/roots"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "List filesystem roots",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        }
    }
    p["/api/backup/browse"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "Browse directory",
            "parameters": [{"name": "path", "in": "query", "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        }
    }
    p["/api/backup/paths"] = {
        "get": {
            "tags": ["Backup"],
            "summary": "List backup paths",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_str())), "401": _401},
        },
        "post": {
            "tags": ["Backup"],
            "summary": "Add backup path",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
        "delete": {
            "tags": ["Backup"],
            "summary": "Remove backup path",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }


# ── Gallery ─────────────────────────────────────────────────────────────

def _gallery_paths(p):
    p["/api/gallery/folders"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "List gallery folders",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_obj({"path": _str(), "label": _str(), "media_count": _int(), "exists": _bool()}))),
                "401": _401,
            },
        },
        "post": {
            "tags": ["Gallery"],
            "summary": "Add gallery folder",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "label": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
        "delete": {
            "tags": ["Gallery"],
            "summary": "Remove gallery folder",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/gallery/scan"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "Scan media items",
            "description": "Paginated media listing with optional filters by folder, type, date, search.",
            "parameters": [
                {"name": "offset", "in": "query", "schema": _int()},
                {"name": "limit", "in": "query", "schema": _int()},
                {"name": "folder", "in": "query", "schema": _str()},
                {"name": "type", "in": "query", "schema": {"type": "string", "enum": ["image", "video", "all"]}},
                {"name": "sort", "in": "query", "schema": {"type": "string", "enum": ["date_desc", "date_asc", "name", "size"]}},
                {"name": "q", "in": "query", "schema": _str()},
                {"name": "month", "in": "query", "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {
                "200": _ok({"total": _int(), "offset": _int(), "limit": _int(), "items": _arr(_obj())}),
                "401": _401,
            },
        }
    }
    p["/api/gallery/albums"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "List albums",
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        }
    }
    p["/api/gallery/timeline"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "Media timeline",
            "description": "Media grouped by year-month.",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_obj({"key": _str(), "year": _int(), "month": _int(), "count": _int()}))),
                "401": _401,
            },
        }
    }
    p["/api/gallery/exif"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "Get EXIF metadata",
            "parameters": [{"name": "path", "in": "query", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        }
    }
    p["/api/gallery/stats"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "Gallery statistics",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "total_images": _int(), "total_videos": _int(),
                    "total_files": _int(), "total_size": _int(),
                    "formats": _obj(),
                }),
                "401": _401,
            },
        }
    }
    p["/api/gallery/favorites"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "List gallery favourites",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        },
        "post": {
            "tags": ["Gallery"],
            "summary": "Add to favourites",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
        "delete": {
            "tags": ["Gallery"],
            "summary": "Remove from favourites",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/gallery/upload"] = {
        "post": {
            "tags": ["Gallery"],
            "summary": "Upload to gallery",
            "security": _AUTH,
            "requestBody": {
                "content": {
                    "multipart/form-data": {
                        "schema": _obj({
                            "folder": _str(),
                            "files": _arr({"type": "string", "format": "binary"}),
                        }),
                    }
                }
            },
            "responses": {"200": _ok({"ok": _bool(), "uploaded": _int()}), "401": _401},
        }
    }
    p["/api/gallery/rotate"] = {
        "post": {
            "tags": ["Gallery"],
            "summary": "Rotate image",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "angle": _int()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/gallery/duplicates"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "Find duplicate media",
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        }
    }
    p["/api/gallery/map"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "Photos with GPS data",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_obj({"path": _str(), "lat": _num(), "lng": _num(), "name": _str()}))),
                "401": _401,
            },
        }
    }
    # Custom albums
    p["/api/gallery/custom-albums"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "List custom albums",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj({"id": _str(), "name": _str(), "description": _str(), "paths": _arr(_str())}))), "401": _401},
        },
        "post": {
            "tags": ["Gallery"],
            "summary": "Create custom album",
            "security": _AUTH,
            "requestBody": _json(_obj({"name": _str(), "description": _str()})),
            "responses": {"200": _ok({"ok": _bool(), "id": _str()}), "401": _401},
        },
        "delete": {
            "tags": ["Gallery"],
            "summary": "Delete custom album",
            "security": _AUTH,
            "requestBody": _json(_obj({"id": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/gallery/custom-albums/add"] = {
        "post": {
            "tags": ["Gallery"],
            "summary": "Add items to custom album",
            "security": _AUTH,
            "requestBody": _json(_obj({"album_id": _str(), "paths": _arr(_str())})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/gallery/custom-albums/remove"] = {
        "post": {
            "tags": ["Gallery"],
            "summary": "Remove items from custom album",
            "security": _AUTH,
            "requestBody": _json(_obj({"album_id": _str(), "paths": _arr(_str())})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    # Gallery shares
    p["/api/gallery/share"] = {
        "post": {
            "tags": ["Gallery"],
            "summary": "Create gallery share",
            "security": _AUTH,
            "requestBody": _json(_obj({"path": _str(), "paths": _arr(_str()), "shared_with": _arr(_str())})),
            "responses": {"200": _ok({"ok": _bool(), "token": _str(), "url": _str()}), "401": _401},
        },
        "delete": {
            "tags": ["Gallery"],
            "summary": "Delete gallery share",
            "security": _AUTH,
            "requestBody": _json(_obj({"token": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/gallery/shares"] = {
        "get": {
            "tags": ["Gallery"],
            "summary": "List gallery shares",
            "security": _AUTH,
            "responses": {"200": _json(_arr(_obj())), "401": _401},
        }
    }


# ── Tickets ─────────────────────────────────────────────────────────────

def _tickets_paths(p):
    p["/api/tickets/projects"] = {
        "get": {
            "tags": ["Tickets"],
            "summary": "List projects",
            "security": _AUTH,
            "responses": {"200": _ok({"projects": _arr(_ref("TicketProject"))}), "401": _401},
        },
        "post": {
            "tags": ["Tickets"],
            "summary": "Create project",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "name": _str(), "description": _str(), "color": _str(),
                "members": _arr(_str()),
            })),
            "responses": {"200": _ok({"ok": _bool(), "item": _ref("TicketProject")}), "401": _401},
        },
    }
    p["/api/tickets/projects/{project_id}"] = {
        "get": {
            "tags": ["Tickets"],
            "summary": "Get project",
            "parameters": [{"name": "project_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {
                "200": _ok({"project": _ref("TicketProject"), "tickets": _arr(_ref("Ticket"))}),
                "401": _401, "403": _403, "404": _404,
            },
        },
        "put": {
            "tags": ["Tickets"],
            "summary": "Update project",
            "parameters": [{"name": "project_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({
                "name": _str(), "description": _str(), "color": _str(),
                "members": _arr(_str()), "columns": _arr(_str()),
            })),
            "responses": {"200": _ok({"ok": _bool(), "item": _ref("TicketProject")}), "401": _401},
        },
        "delete": {
            "tags": ["Tickets"],
            "summary": "Delete project",
            "parameters": [{"name": "project_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401, "403": _403},
        },
    }
    p["/api/tickets/tickets"] = {
        "post": {
            "tags": ["Tickets"],
            "summary": "Create ticket",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "project_id": _str(), "title": _str(), "description": _str(),
                "column": _str(), "priority": _str(), "assignee": _str(),
                "labels": _arr(_str()),
            })),
            "responses": {"200": _ok({"ok": _bool(), "item": _ref("Ticket")}), "401": _401},
        }
    }
    p["/api/tickets/tickets/{ticket_id}"] = {
        "get": {
            "tags": ["Tickets"],
            "summary": "Get ticket",
            "parameters": [{"name": "ticket_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _json(_ref("Ticket")), "401": _401, "404": _404},
        },
        "put": {
            "tags": ["Tickets"],
            "summary": "Update ticket",
            "parameters": [{"name": "ticket_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({
                "title": _str(), "description": _str(), "priority": _str(),
                "assignee": _str(), "column": _str(),
            })),
            "responses": {"200": _ok({"ok": _bool(), "item": _ref("Ticket")}), "401": _401},
        },
        "delete": {
            "tags": ["Tickets"],
            "summary": "Delete ticket",
            "parameters": [{"name": "ticket_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/tickets/tickets/{ticket_id}/move"] = {
        "put": {
            "tags": ["Tickets"],
            "summary": "Move ticket",
            "description": "Move ticket to another column and/or reorder.",
            "parameters": [{"name": "ticket_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({"column": _str(), "order": _int()})),
            "responses": {"200": _ok({"ok": _bool(), "item": _ref("Ticket")}), "401": _401},
        }
    }
    p["/api/tickets/tickets/{ticket_id}/comments"] = {
        "post": {
            "tags": ["Tickets"],
            "summary": "Add comment",
            "parameters": [{"name": "ticket_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({"text": _str()})),
            "responses": {
                "200": _json(_obj({"id": _str(), "author": _str(), "text": _str(), "created": _str()})),
                "401": _401,
            },
        }
    }
    p["/api/tickets/tickets/{ticket_id}/comments/{comment_id}"] = {
        "delete": {
            "tags": ["Tickets"],
            "summary": "Delete comment",
            "parameters": [
                {"name": "ticket_id", "in": "path", "required": True, "schema": _str()},
                {"name": "comment_id", "in": "path", "required": True, "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/tickets/tickets/{ticket_id}/labels"] = {
        "post": {
            "tags": ["Tickets"],
            "summary": "Add label",
            "parameters": [{"name": "ticket_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": _json(_obj({"label": _str()})),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/tickets/tickets/{ticket_id}/labels/{label}"] = {
        "delete": {
            "tags": ["Tickets"],
            "summary": "Remove label",
            "parameters": [
                {"name": "ticket_id", "in": "path", "required": True, "schema": _str()},
                {"name": "label", "in": "path", "required": True, "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/tickets/tickets/{ticket_id}/attachments"] = {
        "post": {
            "tags": ["Tickets"],
            "summary": "Upload attachment",
            "parameters": [{"name": "ticket_id", "in": "path", "required": True, "schema": _str()}],
            "security": _AUTH,
            "requestBody": {
                "content": {
                    "multipart/form-data": {
                        "schema": _obj({"file": {"type": "string", "format": "binary"}}),
                    }
                }
            },
            "responses": {
                "200": _json(_obj({"attachment": _obj({"filename": _str(), "size": _int(), "mimetype": _str()})})),
                "401": _401,
            },
        }
    }
    p["/api/tickets/tickets/{ticket_id}/attachments/{filename}"] = {
        "get": {
            "tags": ["Tickets"],
            "summary": "Download attachment",
            "parameters": [
                {"name": "ticket_id", "in": "path", "required": True, "schema": _str()},
                {"name": "filename", "in": "path", "required": True, "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {
                "200": {"description": "File binary", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
                "401": _401, "404": _404,
            },
        },
        "delete": {
            "tags": ["Tickets"],
            "summary": "Delete attachment",
            "parameters": [
                {"name": "ticket_id", "in": "path", "required": True, "schema": _str()},
                {"name": "filename", "in": "path", "required": True, "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {"200": _ok({"status": _str("deleted")}), "401": _401},
        },
    }
    p["/api/tickets/watcher/status"] = {
        "get": {
            "tags": ["Tickets"],
            "summary": "Watcher service status",
            "security": _AUTH,
            "responses": {
                "200": _ok({"active": _bool(), "enabled": _bool(), "state": _str(), "since": _str()}),
                "401": _401,
            },
        }
    }
    p["/api/tickets/watcher/control"] = {
        "post": {
            "tags": ["Tickets"],
            "summary": "Control watcher service",
            "description": "Start, stop, or restart the ticket watcher.",
            "security": _AUTH,
            "requestBody": _json(_obj({"action": {"type": "string", "enum": ["start", "stop", "restart"]}})),
            "responses": {"200": _ok({"ok": _bool(), "action": _str(), "active": _bool()}), "401": _401},
        }
    }


# ── Notifications ───────────────────────────────────────────────────────

def _notifications_paths(p):
    p["/api/notifications/config"] = {
        "get": {
            "tags": ["Notifications"],
            "summary": "Get notification config",
            "description": "Channel settings (telegram, discord, gotify, ntfy, smtp, webhook) and trigger flags.",
            "security": _AUTH,
            "responses": {"200": _json(_ref("NotificationConfig")), "401": _401},
        },
        "put": {
            "tags": ["Notifications"],
            "summary": "Update notification config",
            "security": _AUTH,
            "requestBody": _json(_ref("NotificationConfig")),
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/notifications/test"] = {
        "post": {
            "tags": ["Notifications"],
            "summary": "Send test notification",
            "security": _AUTH,
            "requestBody": _json(_obj({"channel": _str("telegram")})),
            "responses": {"200": _ok({"ok": _bool(), "message": _str()}), "401": _401},
        }
    }
    p["/api/notifications/history"] = {
        "get": {
            "tags": ["Notifications"],
            "summary": "Notification history",
            "description": "Last 50 notification events.",
            "security": _AUTH,
            "responses": {
                "200": _json(_arr(_obj({
                    "time": _str(), "channel": _str(), "title": _str(),
                    "message": _str(), "success": _bool(),
                }))),
                "401": _401,
            },
        }
    }


# ── Event Log ───────────────────────────────────────────────────────────

def _eventlog_paths(p):
    p["/api/eventlog"] = {
        "get": {
            "tags": ["Event Log"],
            "summary": "List events",
            "parameters": [
                {"name": "limit", "in": "query", "schema": _int()},
                {"name": "offset", "in": "query", "schema": _int()},
                {"name": "category", "in": "query", "schema": _str()},
                {"name": "level", "in": "query", "schema": _str()},
                {"name": "search", "in": "query", "schema": _str()},
            ],
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "events": _arr(_ref("EventLogEntry")),
                    "total": _int(), "limit": _int(), "offset": _int(),
                }),
                "401": _401,
            },
        },
        "post": {
            "tags": ["Event Log"],
            "summary": "Create event",
            "security": _AUTH,
            "requestBody": _json(_obj({
                "category": _str("system"),
                "level": _str("info"),
                "message": _str(),
                "details": _obj(),
            })),
            "responses": {"201": _ok({"ok": _bool()}), "401": _401},
        },
    }
    p["/api/eventlog/clear"] = {
        "post": {
            "tags": ["Event Log"],
            "summary": "Clear all events",
            "security": _AUTH,
            "responses": {"200": _ok({"ok": _bool()}), "401": _401},
        }
    }
    p["/api/eventlog/stats"] = {
        "get": {
            "tags": ["Event Log"],
            "summary": "Event statistics",
            "security": _AUTH,
            "responses": {
                "200": _ok({
                    "total": _int(),
                    "by_category": _obj(),
                    "by_level": _obj(),
                }),
                "401": _401,
            },
        }
    }


# ── Updates ─────────────────────────────────────────────────────────────

def _updates_paths(p):
    p["/api/update/config"] = {
        "get": {
            "tags": ["Updates"],
            "summary": "Update configuration",
            "security": _AUTH,
            "responses": {"200": _json(_obj()), "401": _401},
        },
        "put": {
            "tags": ["Updates"],
            "summary": "Set update configuration",
            "security": _AUTH,
            "requestBody": _json(_obj({"update_url": _str(), "auto_check": _bool()})),
            "responses": {"200": _ok({"success": _bool(), "config": _obj()}), "401": _401},
        },
    }
    p["/api/update/check"] = {
        "post": {
            "tags": ["Updates"],
            "summary": "Check for updates",
            "security": _AUTH,
            "responses": {"200": _ok({"update_available": _bool(), "manifest": _obj()}), "401": _401},
        }
    }
    p["/api/update/apply"] = {
        "post": {
            "tags": ["Updates"],
            "summary": "Apply update",
            "description": "Download and apply the latest update package.",
            "security": _AUTH,
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        }
    }
    p["/api/update/upload"] = {
        "post": {
            "tags": ["Updates"],
            "summary": "Upload update package",
            "security": _AUTH,
            "requestBody": {
                "content": {
                    "multipart/form-data": {
                        "schema": _obj({"file": {"type": "string", "format": "binary"}}),
                    }
                }
            },
            "responses": {"200": _ok({"success": _bool()}), "401": _401},
        }
    }
    p["/api/update/status"] = {
        "get": {
            "tags": ["Updates"],
            "summary": "Update status",
            "security": _AUTH,
            "responses": {"200": _ok({"checking": _bool(), "available": _bool()}), "401": _401},
        }
    }


# ── Setup ───────────────────────────────────────────────────────────────

def _setup_paths(p):
    p["/api/setup/status"] = {
        "get": {
            "tags": ["Setup"],
            "summary": "Setup status",
            "description": "Check if initial setup is needed. No auth required.",
            "responses": {"200": _ok({"needs_setup": _bool()})},
        }
    }
    p["/api/setup/disks"] = {
        "get": {
            "tags": ["Setup"],
            "summary": "Available disks",
            "description": "List disks available for data storage during setup.",
            "responses": {"200": _json(_obj())},
        }
    }
    p["/api/setup/timezones"] = {
        "get": {
            "tags": ["Setup"],
            "summary": "Available time zones",
            "responses": {"200": _ok({"timezones": _arr(_str()), "default": _str()})},
        }
    }
    p["/api/setup/locales"] = {
        "get": {
            "tags": ["Setup"],
            "summary": "Available locales",
            "responses": {"200": _ok({"locales": _arr(_str())})},
        }
    }
    p["/api/setup/languages"] = {
        "get": {
            "tags": ["Setup"],
            "summary": "Available languages",
            "responses": {"200": _ok({"languages": _arr(_obj({"code": _str(), "name": _str()}))})},
        }
    }
    p["/api/setup/complete"] = {
        "post": {
            "tags": ["Setup"],
            "summary": "Complete setup",
            "requestBody": _json(_obj({
                "hostname": _str(), "username": _str(), "password": _str(),
                "nas_name": _str(), "data_disk": _str(), "language": _str(),
                "timezone": _str(), "locale": _str(),
            })),
            "responses": {"200": _ok({"ok": _bool(), "stage": _str(), "message": _str()})},
        }
    }
    p["/api/setup/progress"] = {
        "get": {
            "tags": ["Setup"],
            "summary": "Setup progress",
            "responses": {"200": _ok({"active": _bool(), "stage": _str(), "message": _str(), "elapsed": _num()})},
        }
    }
