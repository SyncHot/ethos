---
name: Build Test and Deploy
globs: "**/*.{py,js,css,sh}"
---

# Build, Test, and Deploy

## Commands

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

Tests run against the live server at `http://localhost:9000` (override with `ETHOS_BASE_URL`). Default request timeout is 15 seconds.

## Deployment workflow

1. Edit files
2. Run `tools/agent_helpers/check_syntax.sh`
3. `sudo systemctl restart ethos`
4. Verify in browser
5. If frontend changed: `rsync -av --delete frontend/ frontend_dist/`
6. Commit and push

## Git conventions

- **Branches:** `feature/{desc}`, `fix/{desc}`, `hotfix/{desc}`, `refactor/{desc}`
- **Commits:** Prefix with `feat:`, `fix:`, `refactor:`, `docs:`, `style:`, `test:`, `chore:`, `perf:`, `security:`
- **Runtime directories not in git:** `data/`, `logs/`, `uploads/`, `venv/`
