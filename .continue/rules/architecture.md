---
name: EthOS Architecture Overview
globs: "**/*.{py,js,css,html}"
---

# EthOS Architecture

EthOS is a NAS/home-server operating system with a desktop-metaphor web UI. It runs as a systemd service on port 9000.

## Stack

- **Backend:** Python 3 + Flask 3.1.0 + Gevent 24.11.1 (monkey-patched), served by Gunicorn
- **Frontend:** Vanilla JavaScript SPA (no framework, no build step) with a custom window manager
- **Database:** SQLite with WAL mode (connection pool in `backend/blueprints/db_pool.py`)
- **Real-time:** Flask-SocketIO over WebSocket/polling for live stats, progress, and notifications
- **Auth:** Custom token-based (64-char hex, 7-day expiry) stored in SQLite (`data/tokens.db`)

## Key directories

```
backend/app.py           — Main application file
backend/blueprints/      — ~50 Flask blueprint modules
backend/host.py          — Hardware Abstraction Layer (HAL)
backend/utils.py         — Shared utilities
frontend/js/apps/        — App JavaScript files
frontend/js/desktop.js   — Desktop shell, api() helper, NAS globals
frontend/css/style.css   — Base styles
frontend/css/apps.css    — App-specific styles
frontend/locales/        — i18n translations (pl, en, de, fr, es)
data/                    — Runtime data (SQLite DBs, JSON configs)
docs/                    — Developer documentation
```

## Concurrency model

Gevent monkey-patches the standard library so blocking I/O becomes non-blocking via greenlets (~4KB each). Each HTTP request and Socket.IO event runs in its own greenlet. Long operations use `socketio.start_background_task()`.

## Frontend serving

Flask serves static files from `frontend_dist/` if it exists, otherwise `frontend/`. They are identical copies — `frontend_dist/` is a production mirror for zero-downtime updates. After editing frontend files, sync with:

```bash
rsync -av --delete frontend/ frontend_dist/
```

There is no webpack/bundler. Script loading uses `?v=N` query params for cache busting.
