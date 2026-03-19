#!/usr/bin/env python3
"""
EthOS Preflight Check — validates code before service restart.

Usage:
    python3 /opt/ethos/tools/preflight_check.py          # check all
    python3 /opt/ethos/tools/preflight_check.py --quick   # syntax only (fast)
    python3 /opt/ethos/tools/preflight_check.py --json    # machine-readable output

Exit codes:  0 = safe to restart,  1 = errors found — do NOT restart
"""

import ast, json, os, subprocess, sys, time, argparse

ETHOS_ROOT = "/opt/ethos"
BACKEND_DIR = os.path.join(ETHOS_ROOT, "backend")
TOOLS_DIR = os.path.join(ETHOS_ROOT, "tools")
FRONTEND_JS_DIR = os.path.join(ETHOS_ROOT, "frontend", "js")
VENV_PYTHON = os.path.join(ETHOS_ROOT, "venv", "bin", "python")
DATA_DIR = os.path.join(ETHOS_ROOT, "data")

CRITICAL_CONFIGS = ["paths.json", "shares.json", "ethos_packages.json"]


def find_py_files():
    files = []
    for d in [BACKEND_DIR, TOOLS_DIR]:
        for root, _, fns in os.walk(d):
            if "__pycache__" in root:
                continue
            for fn in fns:
                if fn.endswith(".py"):
                    files.append(os.path.join(root, fn))
    return sorted(files)


def find_js_files():
    files = []
    for root, _, fns in os.walk(FRONTEND_JS_DIR):
        for fn in fns:
            if fn.endswith(".js"):
                files.append(os.path.join(root, fn))
    return sorted(files)


def check_python_syntax(py_files):
    errors = []
    for fpath in py_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                source = f.read()
            ast.parse(source, filename=fpath)
        except SyntaxError as e:
            rel = os.path.relpath(fpath, ETHOS_ROOT)
            errors.append((rel, f"line {e.lineno}: {e.msg}"))
        except Exception as e:
            rel = os.path.relpath(fpath, ETHOS_ROOT)
            errors.append((rel, str(e)))
    return errors


def check_python_imports():
    python = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable
    script = (
        "import sys, os; sys.path.insert(0, '{bd}'); "
        "import ast; "
        "[ast.parse(open(os.path.join('{bd}', 'blueprints', f)).read(), "
        "filename=f) for f in sorted(os.listdir('{bd}/blueprints')) "
        "if f.endswith('.py') and f != '__init__.py']; "
        "print('IMPORT_OK')"
    ).format(bd=BACKEND_DIR)
    try:
        r = subprocess.run(
            [python, "-c", script],
            capture_output=True, text=True, timeout=30,
            cwd=BACKEND_DIR,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if "IMPORT_OK" in r.stdout:
            return None
        stderr = r.stderr.strip()
        if stderr:
            return "\n".join(stderr.splitlines()[-3:])
        return f"exit code {r.returncode}"
    except subprocess.TimeoutExpired:
        return "import check timed out (30s)"
    except Exception as e:
        return str(e)


def check_js_syntax(js_files):
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    errors = []
    for fpath in js_files:
        try:
            r = subprocess.run(["node", "--check", fpath],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                rel = os.path.relpath(fpath, ETHOS_ROOT)
                err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"exit {r.returncode}"
                errors.append((rel, err))
        except Exception as e:
            rel = os.path.relpath(fpath, ETHOS_ROOT)
            errors.append((rel, str(e)))
    return errors


def check_configs():
    errors = []
    for cfg in CRITICAL_CONFIGS:
        p = os.path.join(DATA_DIR, cfg)
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r") as f:
                json.load(f)
        except json.JSONDecodeError as e:
            errors.append((f"data/{cfg}", f"invalid JSON: {e}"))
        except Exception as e:
            errors.append((f"data/{cfg}", str(e)))
    return errors


def run_preflight(quick=False):
    start = time.time()
    results = {"checks": {}, "errors": [], "passed": True, "duration_ms": 0}

    py_files = find_py_files()
    py_errors = check_python_syntax(py_files)
    results["checks"]["python_syntax"] = {
        "files": len(py_files), "errors": len(py_errors),
        "passed": len(py_errors) == 0,
    }
    for f, e in py_errors:
        results["errors"].append({"check": "python_syntax", "file": f, "error": e})

    if not quick:
        imp_err = check_python_imports()
        results["checks"]["python_imports"] = {"passed": imp_err is None, "error": imp_err}
        if imp_err:
            results["errors"].append({"check": "python_imports", "file": "backend/app.py", "error": imp_err})

        js_files = find_js_files()
        js_errors = check_js_syntax(js_files)
        results["checks"]["js_syntax"] = {
            "files": len(js_files), "errors": len(js_errors),
            "passed": len(js_errors) == 0, "skipped": len(js_files) == 0,
        }
        for f, e in js_errors:
            results["errors"].append({"check": "js_syntax", "file": f, "error": e})

        cfg_errors = check_configs()
        results["checks"]["config_json"] = {
            "files": len(CRITICAL_CONFIGS), "errors": len(cfg_errors),
            "passed": len(cfg_errors) == 0,
        }
        for f, e in cfg_errors:
            results["errors"].append({"check": "config_json", "file": f, "error": e})

    results["duration_ms"] = int((time.time() - start) * 1000)
    results["passed"] = len(results["errors"]) == 0
    return results


def main():
    parser = argparse.ArgumentParser(description="EthOS preflight check")
    parser.add_argument("--quick", action="store_true", help="Syntax only (fast)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    results = run_preflight(quick=args.quick)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for name, data in results["checks"].items():
            s = "\u2713" if data.get("passed") else "\u2717"
            extra = f" ({data['files']} files)" if "files" in data else ""
            if data.get("skipped"):
                s = "\u2298"
                extra += " [skipped]"
            print(f"  {s} {name}{extra}")

        if results["errors"]:
            print(f"\n\u2717 PREFLIGHT FAILED \u2014 {len(results['errors'])} error(s):\n")
            for err in results["errors"]:
                print(f"  {err['file']}: {err['error']}")
            print(f"\n  Do NOT restart ethos until these are fixed.")
        else:
            print(f"\n\u2713 PREFLIGHT PASSED \u2014 safe to restart ({results['duration_ms']}ms)")

    sys.exit(0 if results["passed"] else 1)


if __name__ == "__main__":
    main()
