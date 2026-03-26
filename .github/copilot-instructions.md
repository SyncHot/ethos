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
