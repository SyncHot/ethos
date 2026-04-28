# EthOS — Copilot Instructions

EthOS is a Synology DSM-inspired NAS Operating System: a Flask + gevent backend serving a vanilla JS desktop-style SPA frontend.

## Build & Run

```bash
# Install Python dependencies
./venv/bin/pip install -r backend/requirements.txt

# Start / stop (systemd service)
./start.sh          # sudo systemctl start ethos
./stop.sh           # sudo systemctl stop ethos
./rebuild.sh        # pip install + systemctl restart

# Run tests
cd backend && python -m pytest tests/
python backend/tests/test_validation.py   # run a single test file
```

## Architecture

### Backend (`backend/`)

- **`app.py`** — Flask app entry point. Registers all blueprints, sets up SocketIO (`async_mode='gevent'`), CSRF, rate limiting, and security headers.
- **`blueprints/`** — One `.py` file per feature. Two tiers:
  - **Core blueprints**: imported and registered directly in `app.py`.
  - **Optional blueprints**: registered dynamically via `load_optional_blueprints()` in `app_manager.py`. Never hard-import optional blueprints in `app.py`. The `_OPTIONAL_BLUEPRINTS` dict in `app_manager.py` maps `app_id → (module, blueprint_var, init_fn, needs_socketio)`.
- **`host.py`** — Host abstraction layer. All shell commands go through `host_run(cmd)` or `host_run_stream(cmd)` — never raw `subprocess`. Use `q(s)` from `host` to shell-quote strings.
- **`utils.py`** — Shared helpers: `load_json()`, `save_json()`, `safe_path()`, `fmt_bytes()`, etc.
- **`i18n.py`** — Backend translations. Use `from i18n import t` and call `t('key')` or `t('key', param=val)` in all user-facing strings.
- **`audit.py`** — Security audit trail via `audit_log()`.

### Frontend (`frontend/`)

- No build step — plain HTML + vanilla JS.
- **`js/desktop.js`** — Global state in `NAS` object (token, user, socket, stats). Defines `api()`, `toast()`, `createWindow()`, window manager (`WM`), and the `logClient()` function.
- **`js/apps.js`** — Built-in apps (File Manager, Event Log, App Store, System Settings).
- **`js/apps/*.js`** — One file per optional app. Each registers itself via `AppRegistry['app-id'] = function(appDef, launchOpts) { ... }`.
- **`js/i18n.js`** — Frontend translations. Use the global `t('key')` function.

### Installer (`installer/preboot/`)

Separate Flask app that runs before first boot to handle disk partitioning/formatting and OS installation. Shares no code with the main backend.

## Key Conventions

### API calls (frontend)
`api()` in `desktop.js` **automatically** calls `JSON.stringify` on `body` and sets `Content-Type: application/json`. **Never pre-stringify** when using `api()` — pass raw objects:
```js
// Correct
api('/storage/mount', { method: 'POST', body: { path: '/dev/sda1' } });

// Wrong — double-serializes
api('/storage/mount', { method: 'POST', body: JSON.stringify({ path: '/dev/sda1' }) });
```
Use raw `fetch()` with manual `JSON.stringify` only when not going through `api()`.

### Blueprint pattern
Every blueprint follows the same structure:
1. `Blueprint('name', __name__, url_prefix='/api/name')`
2. `sys.path.insert(0, ...)` + import from `host`, `utils`, `blueprints.admin_required`
3. A module-level `_socketio = None` set via an `init_*()` function called from `app.py`
4. Use `@admin_required` decorator for admin-only endpoints

### Path helpers
```python
from host import data_path, app_path, q
data_path('config.json')   # → /opt/ethos/data/config.json
app_path()                  # → /opt/ethos
q('/some/path with spaces') # → safe shell-quoted string
```

### JSON persistence
Use `load_json(path, default)` and `save_json(path, data)` from `utils` — not raw `open()`. `save_json` writes atomically and creates parent dirs.

### Event logging
```python
from blueprints.eventlog import log as elog
elog('storage', 'info', 'Drive mounted', {'dev': '/dev/sda1'})
# categories: system, files, backup, docker, storage, network, printer, security, error, frontend
```

### gevent import lock
Under gevent, `from app import X` acquires the import lock and permanently disrupts SocketIO WebSocket delivery. Use `sys.modules.get('app')` + `getattr()` instead when a blueprint needs the app object at runtime.

### Service independence
Docker, nginx/domains, Samba, certbot, and all other services **must remain independent** of the `ethos` systemd service. Stopping EthOS (the management UI) must never affect running services.

### Translations
- Backend: `from i18n import t` — supported languages: `en`, `pl`, `de`, `fr`, `es`
- Frontend: global `t('key')` — translation files in `frontend/locales/`
- Backend translation files: `backend/i18n/{lang}.json`

### Data storage
- SQLite for event log, resources history, tickets (via `blueprints/db_pool.py`)
- JSON files via `save_json()`/`load_json()` for configuration
- Persistent data goes under `data_path(...)` (`/opt/ethos/data/`), never inside the app root
