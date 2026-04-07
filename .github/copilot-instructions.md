# EthOS — Copilot Instructions

## Architecture Overview

EthOS is a NAS/home-server operating system with a **desktop-metaphor web UI**. It runs as a systemd service on port 9000.

- **Backend:** Python 3 + Flask 3.1.0 + Gevent 24.11.1 (monkey-patched), served by Gunicorn
- **Frontend:** Vanilla JavaScript SPA (no framework, no build step) with a custom window manager
- **Database:** SQLite with WAL mode (connection pool in `backend/blueprints/db_pool.py`)
- **Real-time:** Flask-SocketIO over WebSocket/polling for live stats, progress, and notifications
- **Auth:** Custom token-based (64-char hex, 7-day expiry) stored in SQLite (`data/tokens.db`). Tokens delivered via `nas_token` cookie, `Authorization: Bearer` header, or `?token=` query param. CSRF uses double-submit cookie pattern.

### How the frontend is served

Flask serves static files from `frontend_dist/` if it exists, otherwise `frontend/`. They are identical copies — `frontend_dist/` is a production mirror enabling zero-downtime updates. After editing frontend files, sync with:

```bash
rsync -av --delete frontend/ frontend_dist/
```

Script loading uses `?v=N` query params for cache busting. There is no webpack/bundler.

### Hardware Abstraction Layer (`backend/host.py`)

All shell commands MUST go through the HAL — never use `subprocess` directly:

```python
host_run(cmd, timeout=30)       # Sync execution via bash -c
host_run_stream(cmd)            # Line-by-line streaming (for long ops)
q(string)                       # shlex.quote — REQUIRED on all user input
safe_path(path)                 # Path traversal validation — REQUIRED on all file paths
app_path(*parts)                # Resolve relative to ETHOS_ROOT
data_path(*parts)               # Resolve relative to DATA_DIR
```

### Concurrency model

Gevent monkey-patches the standard library so blocking I/O becomes non-blocking via greenlets (~4KB each). Each HTTP request and Socket.IO event runs in its own greenlet. Long operations (builds, backups, downloads) use `socketio.start_background_task()`.

## Build, Test, and Lint

```bash
# Run full test suite (restarts server, waits for readiness, then runs pytest)
sudo /opt/ethos/tests/run_tests.sh

# Run a single test file
pytest tests/test_auth.py -v

# Run a single test function
pytest tests/test_auth.py::TestLogin::test_login_success -v

# Custom credentials
ETHOS_USER=myuser ETHOS_PASS=mypass pytest tests/test_auth.py -v

# Validate Python & JS syntax across codebase
tools/agent_helpers/check_syntax.sh

# Restart the server after backend changes
sudo systemctl restart ethos
```

Tests run against the live server at `http://localhost:9000` (override with `ETHOS_BASE_URL`). The test session caches a Bearer token across all tests. Default request timeout is 15 seconds.

There is no frontend build step. After changing JS/CSS, sync to `frontend_dist/` and hard-refresh the browser.

## Backend Conventions

### Blueprint structure

~50 blueprint files live in `backend/blueprints/`. Each blueprint:
- Is named `{name}_bp`
- Has a header comment listing all endpoints and Socket.IO events
- Registers routes under `/api/{blueprint}/...`

### Route pattern

```python
@blueprint.route('/api/module/action', methods=['POST'])
@require_auth
def action():
    # ... logic ...
    return jsonify(ok=True, data=result)
```

- `@require_auth` is mandatory on all protected endpoints
- `@admin_required` for admin-only endpoints (decorators in `blueprints/admin_required.py`)
- Rate limiting: 300 req/60s per IP (middleware in `backend/middleware/rate_limiter.py`)

### API response format

```python
# Success
{"ok": True, "data": {...}}      # or {"ok": True, "item": {...}}

# Error
{"error": "description"}

# Collections
{"items": [...]}                  # with optional pagination
```

HTTP codes: 200 (ok), 400 (validation), 401 (unauth), 403 (forbidden), 404 (not found), 500 (server), 504 (timeout).

### Security — critical rules

1. **Shell injection:** Wrap ALL user-supplied values with `q()` (shlex.quote) before passing to `host_run()`
2. **Path traversal:** Validate ALL file paths with `safe_path()` before any filesystem operation
3. **Sensitive keys:** Use the single `SENSITIVE_KEYS` constant for masking — don't maintain separate lists
4. **Path manipulation:** Use `os.path.relpath()` or `pathlib` — never slice paths with string indexing

### Naming

- Python: `snake_case` for files, functions, variables; `UPPER_SNAKE` for constants
- Blueprint files: `backend/blueprints/{feature}.py`

## Frontend Conventions

### App registration pattern

Every app follows this structure in `frontend/js/apps/{name}.js`:

```javascript
AppRegistry['app-id'] = function(appDef, launchOpts) {
    createWindow('app-id', {
        title: t('App Name'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 900, height: 600,
        onRender: (body) => { /* build UI */ },
    });
};
```

### API calls

```javascript
// api() returns PARSED JSON — never call .json() on the result
const data = await api('/storage/disks');
if (data.error) { toast(data.error, 'error'); return; }
```

The `api()` helper (defined in `desktop.js`) automatically injects the Bearer token and CSRF header. It handles 401 → login redirect and 403 → password change modal.

### Key globals (`NAS` object in `desktop.js`)

- `NAS.token` — current auth token
- `NAS.user.username` — current username (NOT `NAS.username`)
- `NAS.socket` — Socket.IO instance
- `NAS.stats` — live system stats

### i18n

Use `t('Key text')` for all user-facing strings. Polish is the source language. Translations live in `frontend/locales/{en,pl,de,fr,es}.json`. Supports interpolation: `t('Hello {name}', { name: 'World' })`.

### CSS rules

- **Variables only:** Use CSS custom properties from `:root` — never hardcode colors
- **App prefixes:** Each app's CSS classes use a prefix (`fm-` File Manager, `dl-` Downloads, `dkr-` Docker, `cam-` Surveillance, `bkp-` Backup, `set-` Settings, `vm-` VM Manager, `mon-` Monitor, `trm-` Terminal, `dte-` Document Editor)
- **Theme support:** Dark (default) + light via `[data-theme="light"]` overrides
- **Editor exception:** Text editors/code editors MUST use fixed `background: #fff; color: #000` independent of theme
- **Every CSS class used in HTML must have a definition** in `style.css` or `apps.css`
- **Typography:** Inter font family, base 14px
- **Breakpoints:** Mobile-first at 768px, desktop at 1200px

### Naming

- JavaScript: `camelCase` for functions/variables; `UPPER_SNAKE` for constants
- CSS: `kebab-case` with app prefix

## New Application Requirements

Every new app must work on **remotely deployed images built by the Builder app** — not just the local dev server. This means:

- **Never assume dependencies are pre-installed.** If an app requires pip packages or apt packages not in the base image, it must either auto-install them on first use or provide a clearly visible "Install dependencies" button in the UI.
- The install flow should check whether the dependency is present first, and only prompt/install if missing.
- Test on a fresh EthOS install (`data/` empty, no prior setup) to verify the dependency install path works end-to-end.

Example pattern:

```python
# In the blueprint, check and install on demand
def _ensure_deps():
    try:
        import some_package
    except ImportError:
        host_run('pip install some_package', timeout=120)
```

```javascript
// In the app JS, surface install status to the user
const data = await api('/myapp/check-deps');
if (!data.ready) {
    body.innerHTML = `<button onclick="installDeps()">Install dependencies</button>`;
    return;
}
```

## App Data Storage Rules

EthOS runs on a **4 GB SquashFS root partition** (A/B slots). The root fills instantly if apps store large data there. All user data, databases, and large files **MUST** go to the data partition (`/mnt/data`).

### The golden rule

| Stored on root ✅ | Stored on data partition ✅ |
|-------------------|----------------------------|
| apt binaries (`/usr`, `/lib`) | Virus/ML/AI model databases |
| System configs (`/etc/*`) | App index/cache databases |
| pip packages (`venv/`) | Docker images & containers |
| Small state configs | VM disk images |
| Systemd units | Downloaded media files |
| | Recording files |

### Pattern: `get_data_disk()` with fallback

Always check if the data partition is available before using it. Fall back to root for same-disk installs (dev machine, minimal setup):

```python
from host import get_data_disk, data_path, q
import os

def _my_app_data_dir():
    """Return data dir — prefers /mnt/data, falls back to data partition."""
    dd = get_data_disk()          # returns '/mnt/data' or ''
    if dd:
        p = os.path.join(dd, 'myapp')
        os.makedirs(p, exist_ok=True)
        return p
    return data_path('myapp')     # fallback: /opt/ethos/data/myapp (symlinked to data disk on sep-disk installs)
```

> `data_path()` is **also safe** — on separate-disk installs `data/` is a symlink to `/mnt/data/ethos/data/`. Use it for small configs and state files. Use `get_data_disk()` directly only when you need to escape the ethos subdirectory (e.g., Docker needs `/mnt/data/docker`, not `/mnt/data/ethos/data/docker`).

### Pattern: redirect third-party app data directory after apt install

When an apt package stores large data in `/var/lib/<pkg>`, redirect it after install:

```python
def _bg_install():
    r = apt_install('somepkg', timeout=300)
    if r.returncode != 0:
        return

    # Redirect data dir to data partition
    dd = get_data_disk()
    if dd:
        data_dir = os.path.join(dd, 'somepkg')
        os.makedirs(data_dir, exist_ok=True)
        host_run(f'chown -R somepkg:somepkg {q(data_dir)}', timeout=10)

        # Patch app config to use new dir
        conf = '/etc/somepkg/somepkg.conf'
        if os.path.isfile(conf):
            import re
            txt = open(conf).read()
            txt = re.sub(r'^DataDirectory\s.*$', f'DataDirectory {data_dir}',
                         txt, flags=re.MULTILINE)
            if 'DataDirectory' not in txt:
                txt += f'\nDataDirectory {data_dir}\n'
            open(conf, 'w').write(txt)
```

### Checklist for every new app

Before writing a single line of blueprint code, answer these questions:

1. **Does the app install apt packages?**
   - Binaries only (e.g., `ffmpeg`, `mdadm`) → root is fine
   - Packages with data dirs in `/var/lib/<pkg>` → redirect to `get_data_disk()` after install

2. **Does the app download large files?** (models, virus defs, media, ISOs)
   - Always use `data_path('myapp/models')` or `get_data_disk()` path

3. **Does the app run a containerized service?** (Docker, LXC)
   - Set `data-root` / working dir to data partition

4. **Does the app write a database/index?**
   - Store in `data_path('myapp/myapp.db')` — never in `/var/lib` or `/tmp`

5. **Does the app have a configurable data directory?**
   - Expose it via conf file; patch it to `get_data_disk()` path at install time

### Real examples

```python
# Docker — /etc/docker/daemon.json
dd = get_data_disk()
if dd:
    json.dump({'data-root': os.path.join(dd, 'docker')}, open('/etc/docker/daemon.json','w'))

# ClamAV — /etc/clamav/freshclam.conf + clamd.conf
def _clamav_db_dir():
    dd = get_data_disk()
    if dd:
        p = os.path.join(dd, 'clamav')
        os.makedirs(p, exist_ok=True)
        return p
    return '/var/lib/clamav'

# MiniDLNA — /etc/minidlna.conf  db_dir=...
def _minidlna_db_dir():
    dd = get_data_disk()
    if dd:
        p = os.path.join(dd, 'minidlna')
        os.makedirs(p, exist_ok=True)
        return p
    return '/var/lib/minidlna'

# VM Manager — already correct
def _vm_root():
    dd = get_data_disk()
    return os.path.join(dd, 'vms') if dd else os.path.abspath('data/vms')
```

## Common Pitfalls

These are real bugs that have occurred — check for them in every change:

| Mistake | Fix |
|---------|-----|
| Calling `.json()` on `api()` result | `api()` already returns parsed JSON |
| Using `NAS.username` | Correct: `NAS.user?.username` |
| Duplicate `class` attribute on HTML element | Merge into single `class="a b"` |
| Sending masked API key (`XXX***XXX`) back to backend | Read original from server-side config |
| Creating GET endpoint for data embedded in parent object | Read from local state instead |
| Slicing paths with `string[len(prefix):]` | Use `os.path.relpath()` or `pathlib` |
| Using `conn.close()` on pooled DB connections | Return connection to pool instead |
| Storing app data in `/var/lib/<pkg>` | Use `get_data_disk()` and patch app config after install |
| Hardcoding `/var/lib/...` path in blueprint | Make it a function returning `get_data_disk()`-based path with fallback |

## Git Conventions

- **Branches:** `feature/{desc}`, `fix/{desc}`, `hotfix/{desc}`, `refactor/{desc}`
- **Commits:** Prefix with `feat:`, `fix:`, `refactor:`, `docs:`, `style:`, `test:`, `chore:`, `perf:`, `security:`
- **Runtime directories not in git:** `data/`, `logs/`, `uploads/`, `venv/`

## Deployment Workflow

1. Edit files
2. Run `tools/agent_helpers/check_syntax.sh`
3. `sudo systemctl restart ethos`
4. Verify in browser
5. If frontend changed: `rsync -av --delete frontend/ frontend_dist/`
6. Commit and push

## Reference Documentation

Detailed guides live in `docs/`:
- `DEV_STANDARDS.md` — Complete coding standards (naming, patterns, security)
- `ARCH_CORE_SYSTEM.md` — System architecture deep-dive (HAL, gevent, blueprints)
- `FE_LESSONS_LEARNED.md` — Frontend pitfalls with solutions
- `BE_LESSONS_LEARNED.md` — Backend pitfalls with solutions
- `UX_UI_DESIGN_SYSTEM.md` — Design tokens, component library, accessibility
