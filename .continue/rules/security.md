---
name: Security Requirements
globs: "backend/**/*.py"
---

# Security — Critical Rules

These are non-negotiable security requirements for all backend code.

## Shell injection prevention

Wrap ALL user-supplied values with `q()` (shlex.quote) before passing to `host_run()`:

```python
from host import host_run, q

# ✅ Correct
host_run(f'ls -la {q(user_path)}', timeout=10)

# ❌ DANGEROUS — shell injection
host_run(f'ls -la {user_path}', timeout=10)
```

## Path traversal prevention

Validate ALL file paths with `safe_path()` before any filesystem operation:

```python
from utils import safe_path

# ✅ Correct
validated = safe_path(user_supplied_path)

# ❌ DANGEROUS — path traversal
open(user_supplied_path).read()
```

Note: `safe_path()` uses `lstrip()` not `strip()` — trailing whitespace is preserved because Linux filenames can have trailing spaces.

## Sensitive keys

Use the single `SENSITIVE_KEYS` constant for masking — never maintain separate lists of sensitive key names.

## Authentication

- `@require_auth` decorator is mandatory on all protected endpoints
- `@admin_required` for admin-only endpoints
- Auth tokens: 64-char hex, 7-day expiry, stored in `data/tokens.db`
- Tokens delivered via `nas_token` cookie, `Authorization: Bearer` header, or `?token=` query param
- CSRF uses double-submit cookie pattern
