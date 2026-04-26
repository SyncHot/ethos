# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EthOS is a Linux-based NAS (Network Attached Storage) operating system with a web-based desktop interface. It's built using Python 3 with Flask, Gevent for concurrency, and a custom frontend implementation.

## Key Architecture Components

- **Backend**: Python 3 + Flask 3.1.0 + Gevent 24.11.1 (monkey-patched for non-blocking I/O)
- **Frontend**: Vanilla JavaScript SPA with no build step, using a custom window manager
- **Database**: SQLite with WAL mode and connection pooling
- **Real-time Communication**: Flask-SocketIO over WebSocket/polling
- **Authentication**: Token-based system (64-char hex tokens, 7-day expiry)

## Directory Structure

```
backend/
  ├── app.py              # Main application file
  ├── blueprints/         # ~50 Flask blueprint modules for different features
  ├── host.py             # Hardware Abstraction Layer (HAL)
  └── utils.py            # Shared utilities

frontend/
  ├── js/apps/            # App JavaScript files
  ├── js/desktop.js       # Desktop shell, api() helper, NAS globals
  ├── css/                # Stylesheets
  └── locales/            # i18n translations (pl, en, de, fr, es)

data/                     # Runtime data (SQLite DBs, JSON configs)
docs/                     # Developer documentation
tests/                    # Test suite
```

## Development Commands

```bash
# Run full test suite
sudo /opt/ethos/tests/run_tests.sh

# Run a single test file
pytest tests/test_auth.py -v

# Run a single test function
pytest tests/test_auth.py::TestLogin::test_login_success -v

# Validate Python & JS syntax across codebase
tools/agent_helpers/check_syntax.sh

# Restart the server after backend changes
sudo systemctl restart ethos

# Sync frontend files for zero-downtime updates
rsync -av --delete frontend/ frontend_dist/
```

## Key Conventions

- All blueprint files are named `{name}_bp` and register routes under `/api/{blueprint}/...`
- All protected endpoints must use `@require_auth` decorator
- Admin-only endpoints use `@admin_required` decorator
- Hardware commands must go through the HAL (`backend/host.py`) - never use `subprocess` directly
- API response format: `jsonify({'ok': True, 'data': result})` for success, `jsonify({'error': 'Description'})` for errors
- All paths must use `safe_path()` for validation and `data_path()` for resolving data directories
- No webpack/bundler - all frontend code is vanilla JavaScript with cache busting via query params

## Testing

Tests run against the live server at `http://localhost:9000`. Default request timeout is 15 seconds. Tests use pytest framework with custom test runner script.

## Deployment Workflow

1. Edit files
2. Run syntax validation: `tools/agent_helpers/check_syntax.sh`
3. Restart server: `sudo systemctl restart ethos`
4. If frontend changed: `rsync -av --delete frontend/ frontend_dist/`
5. Commit and push