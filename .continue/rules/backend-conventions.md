---
name: Backend Conventions
globs: "backend/**/*.py"
---

# Backend Conventions

## Blueprint structure

Each blueprint file in `backend/blueprints/`:
- Is named `{name}_bp` (the Blueprint object)
- Has a header comment listing all endpoints and Socket.IO events
- Registers routes under `/api/{blueprint}/...`

## Route pattern

```python
@blueprint.route('/api/module/action', methods=['POST'])
@require_auth
def action():
    # ... logic ...
    return jsonify(ok=True, data=result)
```

- `@require_auth` is mandatory on all protected endpoints
- `@admin_required` for admin-only endpoints (from `blueprints/admin_required.py`)
- Rate limiting: 300 req/60s per IP (middleware in `backend/middleware/rate_limiter.py`)

## API response format

```python
# Success (mutation)
return jsonify({'ok': True, 'item': new_item}), 201

# Success (list)
return jsonify({'projects': projects_list})

# Success (delete)
return jsonify({'ok': True}), 200

# Error
return jsonify({'error': 'Description'}), 400  # or 403, 404, 500
```

HTTP codes: 200 (ok), 400 (validation), 401 (unauth), 403 (forbidden), 404 (not found), 500 (server), 504 (timeout).

## Hardware Abstraction Layer (backend/host.py)

All shell commands MUST go through the HAL — never use `subprocess` directly:

```python
host_run(cmd, timeout=30)       # Sync execution via bash -c — returns CompletedProcess
host_run_stream(cmd)            # Line-by-line streaming (for long ops)
q(string)                       # shlex.quote — REQUIRED on all user input
safe_path(path)                 # Path traversal validation — REQUIRED on all file paths
app_path(*parts)                # Resolve relative to ETHOS_ROOT
data_path(rel='')               # Resolve relative to DATA_DIR (takes 0 or 1 arg only)
get_data_disk()                 # Returns '/mnt/data' or '' — for large data storage
```

- `host_run()` returns `subprocess.CompletedProcess` with `.stdout`, `.stderr`, `.returncode` — NOT a string. Always use `result.stdout` to get output.
- `data_path()` takes only 0-1 args. For nested paths use `os.path.join(data_path('models'), 'subdir')`.

## Naming

- Python: `snake_case` for files, functions, variables; `UPPER_SNAKE` for constants
- Blueprint files: `backend/blueprints/{feature}.py`

## Common pitfalls

- Do NOT manipulate paths with string slicing (`path[len(prefix):]`). Use `os.path.relpath()` or `pathlib`.
- Do NOT maintain separate lists of sensitive keys. Use the single `SENSITIVE_KEYS` constant.
- Do NOT use `conn.close()` on pooled DB connections. Return them to the pool.
- Do NOT store app data in `/var/lib/<pkg>`. Use `get_data_disk()` with fallback.
- Document ALL endpoints in the header comment of each blueprint.
