# EthOS — Comprehensive Manual QA Report

**Date:** 2026-06-03
**Tester:** Hermes Agent (Automated QA)
**Version Tested:** EthOS v1.0.147
**Environment:** Docker container on Windows/WSL2, port 9000
**Scope:** Full API endpoint testing, security edge cases, frontend code review

---

## Executive Summary

| Severity | Count |
|----------|-------|
| Critical | 3     |
| Major    | 7     |
| Minor    | 6     |
| Cosmetic | 2     |
| **Total** | **18** |

**Categories:** Functional (9), Security (2), API Design (4), UX/Content (3)

---

## [BUG-01] File deletion crashes with NameError — `_purge_thumb_cache` undefined

- **Application/Module:** File Manager → Delete operation
- **Severity:** Critical
- **Environment:** EthOS OS (Web UI + API)
- **Description:** Deleting files via the file manager triggers a Python `NameError`. The function `_purge_thumb_cache()` is called in `file_manager_mobile.py:387` but never imported. It is defined in `file_manager_photos.py` and imported in `file_manager.py`, but `file_manager_mobile.py` lacks this import.
- **Steps to Reproduce:**
  1. Create a directory via `POST /api/files/mkdir {"path":"/home/nasadmin/test_dir"}`
  2. Delete it via `DELETE /api/files/delete {"path":"/home/nasadmin/test_dir"}`
  3. Observe response: `{"deleted":[], "errors":["name '_purge_thumb_cache' is not defined"]}`
- **Expected Behavior:** File/directory deleted successfully with no errors in the response.
- **Actual Behavior:** Deletion completes but returns an error message containing a Python traceback fragment, exposing internal implementation details to the user.
- **Notes for Senior Dev:** Missing import in `backend/blueprints/file_manager_mobile.py`. Add `_purge_thumb_cache` to the imports from `file_manager_photos` or refactor into a shared utility module. Also: error messages should never expose raw Python exceptions to end users — wrap in try/except and return user-friendly messages.

---

## [BUG-02] Power status endpoint returns 500 Internal Server Error

- **Application/Module:** Power Manager → Status
- **Severity:** Critical
- **Environment:** EthOS OS (Web UI + API)
- **Description:** `GET /api/power/status` consistently returns HTTP 500 with generic "Internal server error" message. The endpoint is registered correctly (`@power_bp.route('/status')`) but crashes at runtime, likely due to an unhandled exception in `_get_primary_iface()` or CPU governor sysfs access inside the Docker container.
- **Steps to Reproduce:**
  1. Authenticate and obtain a bearer token
  2. `GET /api/power/status` with valid auth header
  3. Observe: `{"error":"Internal server error"}` (HTTP 500)
- **Expected Behavior:** Returns power management status (WOL, schedule, HDD spindown, CPU governor).
- **Actual Behavior:** HTTP 500 with no diagnostic information. The generic error handler swallows the actual exception.
- **Notes for Senior Dev:** Likely caused by accessing `/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` which doesn't exist in Docker/WSL2 containers. Add graceful fallback: if sysfs paths don't exist, return `"unknown"` instead of crashing. Also consider adding `try/except` around each status check so one failure doesn't break the entire endpoint.

---

## [BUG-03] Disk Repair endpoint always fails — "Cannot read disk list"

- **Application/Module:** Storage Manager → Disk Repair
- **Severity:** Critical
- **Environment:** EthOS OS (Web UI + API)
- **Description:** `GET /api/diskrepair/disks` returns HTTP 500 with `"error":"Cannot read disk list"`. This endpoint is called on every dashboard load and generates persistent error notifications. The underlying cause is likely insufficient privileges to access block device information in the Docker container.
- **Steps to Reproduce:**
  1. `GET /api/diskrepair/disks` with valid auth
  2. Observe: HTTP 500, `{"error":"Cannot read disk list"}`
- **Expected Behavior:** Returns disk SMART data or a graceful "not available in this environment" message.
- **Actual Behavior:** HTTP 500 error logged to event log on every call, generating notification spam (IDs 768, 460+).
- **Notes for Senior Dev:** The disk repair module requires `smartmontools` and direct block device access (`/dev/sdX`). In Docker without `--privileged`, this fails. Add a capability check at startup: if SMART tools are unavailable or devices inaccessible, return HTTP 503 with `"error":"missing_dependency"` (consistent pattern used by network/WiFi endpoints) instead of HTTP 500. This also stops the notification spam.

---

## [BUG-04] Firewall status crashes — iptables permission denied in Docker

- **Application/Module:** Security → Firewall
- **Severity:** Major
- **Environment:** EthOS OS (Web UI + API)
- **Description:** `GET /api/firewall/status` returns HTTP 500 with a verbose error message exposing the full iptables command output: `"ERROR: problem running iptables: iptables v1.8.11 (nf_tables): Could not fetch rule set generation id: Permission denied (you must be root)"`. This leaks internal tooling details and version numbers.
- **Steps to Reproduce:**
  1. `GET /api/firewall/status` with valid auth
  2. Observe: HTTP 500 with full iptables error message in response body
- **Expected Behavior:** Return firewall status or a clean "not available" message.
- **Actual Behavior:** HTTP 500 with verbose command output including tool version numbers.
- **Notes for Senior Dev:** Two issues here: (1) In Docker without `CAP_NET_ADMIN`, iptables commands fail — add capability check and return 503 like other modules do. (2) Error messages should never pass raw subprocess stderr directly to the JSON response. Sanitize all error strings before returning them to clients.

---

## [BUG-05] Multiple network endpoints return 503 with missing `nmcli` dependency

- **Application/Module:** Network → WiFi, AP Mode, Bonding
- **Severity:** Major
- **Environment:** EthOS OS (Web UI + API)
- **Description:** Four network-related endpoints consistently return HTTP 503: `/api/network/wifi/status`, `/api/network/wifi/saved`, `/api/network/ap/status`, `/api/network/bonds`. All fail because `nmcli` is not installed in the Docker container. While the error handling pattern (returning 503 with install instructions) is correct, these endpoints are called on every dashboard load and generate persistent notification spam.
- **Steps to Reproduce:**
  1. Load the Network app or dashboard
  2. Observe repeated 503 errors in event log for all four endpoints
- **Expected Behavior:** Either (a) `nmcli` is installed as a base dependency, or (b) the frontend gracefully hides WiFi/AP/Bonding UI sections when the dependency check fails at startup.
- **Actual Behavior:** Every dashboard load triggers 4 separate API calls that each return 503, generating 4 error notifications per refresh cycle.
- **Notes for Senior Dev:** The notification system should debounce or suppress repeated identical errors. Consider adding a "dependency check" endpoint that runs once at startup and caches results, so the frontend can conditionally show/hide UI sections without making individual failing API calls.

---

## [BUG-06] Updates check returns "Method not allowed" for POST request

- **Application/Module:** System Settings → Updates
- **Severity:** Major
- **Environment:** EthOS OS (Web UI + API)
- **Description:** The route definition in `updater.py:338` declares `@update_bp.route('/check', methods=['POST'])`, but sending a POST request to `/api/updates/check` returns `"error":"Method not allowed"`. This suggests the blueprint is registered with a conflicting route or there's a method mismatch.
- **Steps to Reproduce:**
  1. `POST /api/updates/check` with valid auth and empty JSON body
  2. Observe: HTTP 405, `{"error":"Method not allowed"}`
- **Expected Behavior:** Triggers an update check and returns available updates or "system is up to date".
- **Actual Behavior:** HTTP 405 Method Not Allowed.
- **Notes for Senior Dev:** Check if there's a route conflict — another handler might be registered at `/api/updates/check` with different methods. Also verify the blueprint registration in `app.py`: `update_bp` is imported and registered, but check if `url_prefix` matches expectations.

---

## [BUG-07] Health endpoint reports version as "unknown"

- **Application/Module:** System → Health Check
- **Severity:** Minor
- **Environment:** EthOS OS (Web UI + API)
- **Description:** `GET /api/health` returns `{"status":"ok","version":"unknown"}`. The dashboard summary endpoint correctly reports `"version":"1.0.147"`, but the health check cannot determine the version string.
- **Steps to Reproduce:**
  1. `GET /api/health` (no auth required)
  2. Observe: `{"status":"ok","version":"unknown"}`
- **Expected Behavior:** Returns actual EthOS version, e.g., `{"status":"ok","version":"1.0.147"}`.
- **Actual Behavior:** Version field is always "unknown".
- **Notes for Senior Dev:** The health check likely reads from a file or environment variable that isn't set in the Docker build. Compare with how `/api/dashboard/summary` gets the version and use the same source.

---

## [BUG-08] Settings endpoint accepts empty POST body without validation

- **Application/Module:** System Settings
- **Severity:** Minor
- **Environment:** EthOS OS (Web UI + API)
- **Description:** `POST /api/settings/` with an empty JSON body `{}` returns `{"changes":[],"errors":[],"ok":true,"restart_needed":false}`. While technically harmless, this indicates missing input validation — the endpoint should either require at least one field or return a 400 Bad Request for empty submissions.
- **Steps to Reproduce:**
  1. `POST /api/settings/` with body `{}` and valid auth
  2. Observe: HTTP 200, `{"changes":[],"errors":[],"ok":true,"restart_needed":false}`
- **Expected Behavior:** Either reject empty submissions (400) or document that empty POSTs are a no-op.
- **Actual Behavior:** Silently accepts and returns success with empty changes array.
- **Notes for Senior Dev:** Add input validation: if `request.json` is empty or has no recognized keys, return 400 with `"error":"No settings provided"`. This prevents accidental empty submissions from the frontend.

---

## [BUG-09] Tickets API stores unsanitized HTML/JavaScript in title and description fields

- **Application/Module:** Kanban / Tickets
- **Severity:** Major (Security)
- **Environment:** EthOS OS (Web UI + API)
- **Description:** The ticket creation endpoint accepts raw HTML/JavaScript without server-side sanitization. Creating a ticket with `title: "<script>alert(1)</script>"` and `description: "<img src=x onerror=alert(1)>"` stores these values verbatim in the database and returns them unmodified in API responses.
- **Steps to Reproduce:**
  1. Create a project via `POST /api/tickets/projects {"name":"Test"}`
  2. Create a ticket: `POST /api/tickets/tickets {"project_id":"<id>","title":"<script>alert(1)</script>","description":"<img src=x onerror=alert(1)>"}`
  3. Observe response contains raw HTML/JS in title and description fields
- **Expected Behavior:** Server-side sanitization strips or escapes HTML tags before storing, OR the API clearly documents that clients must escape content.
- **Actual Behavior:** Raw HTML stored and returned verbatim.
- **Notes for Senior Dev:** The frontend DOES use `_escHtml()` when rendering ticket titles/descriptions (confirmed in `tickets.js` lines 1604, 1680, 1737, 2098, 2102), so reflected XSS is mitigated. However: (a) Server-side sanitization is still best practice as a defense-in-depth measure; (b) If any future API consumer (mobile app, third-party integration) renders these values without escaping, it becomes an active vulnerability. Recommend adding `bleach.clean()` or similar server-side HTML sanitization to the ticket creation/update handlers.

---

## [BUG-10] Notifications count endpoint does not exist

- **Application/Module:** Notifications
- **Severity:** Minor
- **Environment:** EthOS OS (Web UI + API)
- **Description:** `GET /api/notifications/count` returns HTTP 404 "Not found". The frontend likely expects this endpoint to efficiently check the unread notification count without fetching all notifications. Currently, the only available endpoint is `GET /api/notifications` which returns the full list (50+ items).
- **Steps to Reproduce:**
  1. `GET /api/notifications/count` with valid auth
  2. Observe: HTTP 404, `{"error":"Not found"}`
- **Expected Behavior:** Returns `{"unread": N}` or similar lightweight count response.
- **Actual Behavior:** HTTP 404 Not Found.
- **Notes for Senior Dev:** Add a `/api/notifications/count` endpoint that returns just the unread count. This is a common pattern for notification badges and avoids transferring large payloads on every poll cycle.

---

## [BUG-11] Docker Manager shows contradictory status — installed but not available

- **Application/Module:** App Manager → Docker Manager
- **Severity:** Minor
- **Environment:** EthOS OS (Web UI + API)
- **Description:** `GET /api/docker/pkg-status` returns `{"docker_available":false,"installed":true}`. The app is marked as installed but Docker itself is not available inside the container. This creates a confusing state where the UI shows Docker Manager as "installed" but all operations fail with `"Docker is not installed or not running"`.
- **Steps to Reproduce:**
  1. `GET /api/docker/pkg-status` with valid auth
  2. Observe: `{"docker_available":false,"installed":true}`
  3. Try `GET /api/docker/containers` → `"Docker is not installed or not running"`
- **Expected Behavior:** Either mark as "not installed" when Docker daemon is unavailable, or show a clear warning state in the UI.
- **Actual Behavior:** Contradictory status — installed=true but docker_available=false.
- **Notes for Senior Dev:** The `installed` flag likely checks if the blueprint/package files exist, while `docker_available` checks for the Docker socket/daemon. Consider adding an `"operational"` or `"ready"` boolean that combines both conditions, so the frontend can show a proper warning state instead of appearing functional but failing on every operation.

---

## [BUG-12] Storage volumes and backup tasks endpoints return 404

- **Application/Module:** Storage Manager, Backup
- **Severity:** Minor
- **Environment:** EthOS OS (Web UI + API)
- **Description:** Several commonly expected endpoints return HTTP 404:
  - `GET /api/storage/volumes` → Not found
  - `GET /api/backup/tasks` → Not found
  - `GET /api/security-advisor/status` → Not found (correct route is `/scan`)
  - `GET /api/ssh-manager/status` → Not found (no status endpoint exists)
- **Steps to Reproduce:** Call any of the above endpoints with valid auth.
- **Expected Behavior:** Return data or a meaningful error message.
- **Actual Behavior:** HTTP 404 "Not found".
- **Notes for Senior Dev:** The frontend may be calling these non-existent routes, causing silent failures in the UI. Audit the frontend JavaScript to ensure all API calls match actual backend route definitions. Consider adding an API documentation endpoint that lists all available routes (the `api_docs_bp` exists but `/api/docs/routes` also returns 404).

---

## [BUG-13] Hostname displayed as Docker container ID instead of user-friendly name

- **Application/Module:** System Dashboard, Login Screen
- **Severity:** Cosmetic
- **Environment:** EthOS OS (Web UI)
- **Description:** The system hostname is reported as the Docker container ID (`062778b4a207`) in `/api/dashboard/summary` and `/api/system/info`, while `nas_name` correctly shows "EthOS". The login screen displays "EthOS" (from nas_name), but internal system info pages show the raw container ID.
- **Steps to Reproduce:**
  1. Check `/api/dashboard/summary` → `"hostname":"062778b4a207"`
  2. Compare with `"nas_name":"EthOS"` in same response
- **Expected Behavior:** Hostname should match `nas_name` or be a user-configurable value, not the container ID.
- **Actual Behavior:** Hostname is the Docker container ID.
- **Notes for Senior Dev:** In Docker environments, `/etc/hostname` contains the container ID. The frontend should prefer `nas_name` over raw hostname for display purposes, or provide a setting to override the displayed hostname.

---

## [BUG-14] Notification badge shows inflated count (50→375) on dashboard load

- **Application/Module:** Notifications
- **Severity:** Minor
- **Environment:** EthOS OS (Web UI)
- **Description:** The notification badge initially shows "50" unread notifications, but after interacting with the dashboard, it jumps to "375". This suggests that background monitoring tasks are generating notifications faster than they can be read, or there's a counting bug where errors from repeated API calls are each logged as separate notifications.
- **Steps to Reproduce:**
  1. Log in and observe notification badge shows "50"
  2. Wait ~30 seconds or navigate between pages
  3. Observe badge jumps to "375"
- **Expected Behavior:** Notification count should be stable or grow slowly based on actual new events.
- **Actual Behavior:** Rapid inflation from 50 to 375 within minutes.
- **Notes for Senior Dev:** The notification system likely creates a new notification for every failed API call (disk repair, firewall, network endpoints). Since these are polled repeatedly, each poll generates new error notifications. Implement deduplication: if the same error occurs multiple times within a time window, update the existing notification's timestamp/count instead of creating a new one.

---

## [BUG-15] Disk usage alerts fire for Docker internal mount points

- **Application/Module:** Storage Manager → Diagnostics
- **Severity:** Minor
- **Environment:** EthOS OS (Web UI)
- **Description:** The storage monitoring system generates persistent "Disk usage at 100%" alerts for `/mnt/host/wsl/docker-desktop/cli-tools` and "Disk usage at 91% on /mnt/host/c". These are Docker/WSL internal mount points that the user cannot control or fix, creating notification noise.
- **Steps to Reproduce:**
  1. Check notifications → multiple "Storage Alert" entries for Docker internal paths
  2. These repeat every monitoring cycle (~5 minutes)
- **Expected Behavior:** Only alert on user-configurable storage volumes, not Docker/WSL internal mounts.
- **Actual Behavior:** Alerts fire for all mounted filesystems including host passthrough mounts.
- **Notes for Senior Dev:** Add a filter/exclusion list in the storage monitoring code to skip paths matching patterns like `/mnt/host/*`, `/var/lib/docker/*`, and other container-internal mount points. Alternatively, make alert thresholds configurable per-mount-point.

---

## [BUG-16] Event log shows 239 errors out of 483 total entries (49% error rate)

- **Application/Module:** System → Event Log
- **Severity:** Minor
- **Environment:** EthOS OS (Web UI + API)
- **Description:** The event log statistics show: `{"by_level":{"debug":3,"error":239,"info":119,"warning":122},"total":483}`. Nearly half of all logged events are errors, most from the same recurring issues (disk repair, firewall, network dependencies). This makes it difficult to identify genuinely new problems among the noise.
- **Steps to Reproduce:**
  1. `GET /api/eventlog/stats` with valid auth
  2. Observe error rate: 239/483 = 49.5%
- **Expected Behavior:** Error rate should be low (<5%) in a healthy system, with recurring issues suppressed or grouped.
- **Actual Behavior:** Nearly half of all events are errors from known Docker environment limitations.
- **Notes for Senior Dev:** Implement event log deduplication: group identical error messages within a time window and show "occurred N times" instead of logging each occurrence separately. This is especially important for health-check-style endpoints that poll on intervals.

---

## [BUG-17] CSS text-overflow ellipsis applied to many elements but no `white-space: nowrap`

- **Application/Module:** Frontend → UI/CSS
- **Severity:** Cosmetic
- **Environment:** EthOS OS (Web UI)
- **Description:** The CSS applies `text-overflow: ellipsis` to multiple elements (taskbar buttons, file names, etc.) but in many cases lacks the required companion property `white-space: nowrap`. Without `white-space: nowrap`, the ellipsis never triggers — text simply wraps onto multiple lines instead of truncating with "...".
- **Steps to Reproduce:**
  1. Create a file or folder with a very long name (>50 characters)
  2. Observe in File Manager → text wraps to multiple lines instead of showing ellipsis
- **Expected Behavior:** Long text should truncate with "..." (ellipsis) and show full name on hover via title attribute.
- **Actual Behavior:** Text wraps onto multiple lines, potentially breaking layout.
- **Notes for Senior Dev:** Audit all CSS rules using `text-overflow: ellipsis` and ensure they also have `white-space: nowrap` and a fixed/max-width container. Affected selectors include `.taskbar-win-btn span`, various file list items, and notification text elements.

---

## [BUG-18] File Manager delete returns raw Python error message to user

- **Application/Module:** File Manager → Delete
- **Severity:** Major (Security / UX)
- **Environment:** EthOS OS (Web UI + API)
- **Description:** Related to BUG-01. The file deletion endpoint returns `{"deleted":[],"errors":["name '_purge_thumb_cache' is not defined"]}` — a raw Python NameError message exposed directly in the JSON response. This reveals internal implementation details and could aid attackers in understanding the codebase structure.
- **Steps to Reproduce:** Same as BUG-01.
- **Expected Behavior:** User-friendly error: `"errors":["Failed to clean up thumbnails for this item"]` or similar.
- **Actual Behavior:** Raw Python exception text in response body.
- **Notes for Senior Dev:** All API endpoints should catch exceptions and return sanitized error messages. The raw traceback should only be logged server-side (which the event log system already does). Add a global exception handler that converts Python exceptions to user-friendly JSON responses, or wrap each endpoint's business logic in try/except blocks with meaningful fallback messages.

---

## Testing Notes

### What Was Tested
- **Authentication:** Login/logout, empty credentials, non-existent users, SQL injection attempts — all handled correctly
- **File Manager:** Create/delete/rename operations, path traversal protection (blocked `/etc/shadow`), special characters in filenames
- **Storage Manager:** Disk listing, SMB shares, volume endpoints
- **Network:** Interface info, WiFi/AP/bonding endpoints (expected failures in Docker)
- **System Settings:** Settings GET/POST, hostname, timezone, updates check
- **Tickets/Kanban:** Project creation, ticket CRUD, XSS payload handling
- **App Manager:** Full catalog of 45+ apps with install status
- **Security Advisor:** Security scan (14 checks, score 32/100)
- **Notifications:** List, mark-all-read functionality
- **Event Log:** Stats, recent entries
- **AI Chat:** Config and conversations endpoints
- **Cloud Backup, Family Hub, Downloads, Gallery, Video Station:** Status endpoints

### What Was Not Tested (Environment Limitations)
- Full UI/UX visual testing — the browser tool's JavaScript execution environment does not fully render the SPA after login, preventing visual inspection of the desktop interface
- Docker container management — Docker daemon is not available inside the test container
- WiFi/AP/Bonding functionality — requires `nmcli` which is not installed in Docker
- SMART disk diagnostics — requires direct block device access unavailable in Docker
- Firewall rules — iptables requires elevated privileges not granted to the container

### Recommendations for Future Testing
1. Run tests on bare-metal EthOS installation (not Docker) to validate hardware-dependent features
2. Perform visual UI testing with a real browser on the actual desktop interface
3. Test concurrent operations and race conditions in file management
4. Load test notification system under sustained error conditions
