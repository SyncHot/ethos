#!/usr/bin/env python3
"""
EthOS Codebase Map Generator — dynamic architecture reference for AI agents.

Scans the codebase and generates a COMPACT map (~5KB) that gives AI models
instant orientation: which file to open for what, key entry points, line numbers.

Usage:
    python3 tools/codebase_map.py              # print to stdout
    python3 tools/codebase_map.py --json        # JSON output

Called by ticket_watcher.py before each ticket to inject into agent prompt.
"""

import os
import re
import json
import argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")
TOOLS = os.path.join(ROOT, "tools")
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(ROOT, "data")


def _lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.readlines()
    except Exception:
        return []


def _count(path):
    return len(_lines(path))


def _rel(path):
    return os.path.relpath(path, ROOT)


# ── Backend scan ──────────────────────────────────────────────────────────

def scan_backend():
    """Scan backend: blueprint prefix, route count, key functions."""
    results = []

    # Main app.py — just count routes, list major sections
    app_path = os.path.join(BACKEND, "app.py")
    if os.path.exists(app_path):
        lines = _lines(app_path)
        route_count = sum(1 for l in lines if "@app.route(" in l)
        results.append({
            "file": "backend/app.py",
            "lines": len(lines),
            "note": f"Main Flask app, {route_count} routes. Auth, setup, file-manager, apps, power, uploads, trash, duplicates, code-editor, phone-sync.",
        })

    # Blueprints — prefix + route count
    bp_dir = os.path.join(BACKEND, "blueprints")
    if os.path.isdir(bp_dir):
        for fname in sorted(os.listdir(bp_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            fpath = os.path.join(bp_dir, fname)
            lines = _lines(fpath)
            prefix = ""
            route_count = 0
            for line in lines:
                m = re.search(r"url_prefix=['\"]([^'\"]+)['\"]", line)
                if m:
                    prefix = m.group(1)
                if re.search(r"@\w+\.route\(", line):
                    route_count += 1
            results.append({
                "file": f"backend/blueprints/{fname}",
                "lines": len(lines),
                "prefix": prefix,
                "routes": route_count,
            })

    # Standalone modules
    for fname in sorted(os.listdir(BACKEND)):
        if not fname.endswith(".py") or fname.startswith("_") or fname == "app.py":
            continue
        fpath = os.path.join(BACKEND, fname)
        lines = _lines(fpath)
        pub_funcs = []
        for i, line in enumerate(lines, 1):
            m = re.match(r"^def\s+(\w+)\(", line)
            if m and not m.group(1).startswith("_"):
                pub_funcs.append(f"{m.group(1)}() L{i}")
        if pub_funcs:
            results.append({
                "file": f"backend/{fname}",
                "lines": len(lines),
                "functions": pub_funcs[:8],
            })

    return results


# ── Frontend scan ─────────────────────────────────────────────────────────

def scan_frontend():
    """Scan frontend: app registrations, key render functions."""
    results = []

    # Apps
    apps_dir = os.path.join(FRONTEND, "js", "apps")
    if os.path.isdir(apps_dir):
        for fname in sorted(os.listdir(apps_dir)):
            if not fname.endswith(".js"):
                continue
            fpath = os.path.join(apps_dir, fname)
            lines = _lines(fpath)
            app_ids = []
            renders = []
            for i, line in enumerate(lines, 1):
                m = re.search(r"AppRegistry\[['\"](\w+)['\"]\]", line)
                if m:
                    app_ids.append(f"{m.group(1)} L{i}")
                # Capture render/show/init-like functions
                m = re.match(r"^(?:function|const|let)\s+(render\w+|show\w+|init\w+|load\w+)\s*[=(]", line)
                if m:
                    renders.append(f"{m.group(1)}() L{i}")
            results.append({
                "file": f"frontend/js/apps/{fname}",
                "lines": len(lines),
                "apps": app_ids,
                "key_fns": renders[:10],
            })

    # Main JS
    js_dir = os.path.join(FRONTEND, "js")
    for fname in sorted(os.listdir(js_dir)):
        if not fname.endswith(".js"):
            continue
        fpath = os.path.join(js_dir, fname)
        lines = _lines(fpath)
        if len(lines) < 100:
            continue
        app_ids = []
        key_fns = []
        for i, line in enumerate(lines, 1):
            m = re.search(r"AppRegistry\[['\"](\w+)['\"]\]", line)
            if m:
                app_ids.append(f"{m.group(1)} L{i}")
            m = re.match(r"^(?:function|const|let)\s+(\w+)\s*[=(]", line)
            if m and not m.group(1).startswith("_"):
                key_fns.append(f"{m.group(1)}() L{i}")
        results.append({
            "file": f"frontend/js/{fname}",
            "lines": len(lines),
            "apps": app_ids[:10],
            "key_fns": key_fns[:15],
        })

    return results


# ── CSS scan ──────────────────────────────────────────────────────────────

def scan_css():
    results = []
    css_dir = os.path.join(FRONTEND, "css")
    if not os.path.isdir(css_dir):
        return results
    for fname in sorted(os.listdir(css_dir)):
        if not fname.endswith(".css"):
            continue
        fpath = os.path.join(css_dir, fname)
        lines = _lines(fpath)
        sections = []
        for i, line in enumerate(lines, 1):
            m = re.search(r"/\*[\s=\u2500*]+([A-Z][^*]{3,60})[\s=\u2500*]+\*/", line)
            if m:
                sections.append(f"L{i} {m.group(1).strip()}")
        results.append({
            "file": f"frontend/css/{fname}",
            "lines": len(lines),
            "sections": sections,
        })
    return results

# ── Agent-filtered maps ──────────────────────────────────────────────────
# Maps agent type to which sections to include (True = include)
_AGENT_SECTIONS = {
    "FE/UX":    {"backend": False, "frontend": True, "css": True,  "docs": True, "data": False, "tools": False},
    "Backend":  {"backend": True,  "frontend": False, "css": False, "docs": True, "data": True,  "tools": True},
    "DevOps":   {"backend": True,  "frontend": False, "css": False, "docs": True, "data": True,  "tools": True},
    "Security": {"backend": True,  "frontend": True,  "css": False, "docs": True, "data": False, "tools": True},
    "Docs":     {"backend": False, "frontend": False, "css": False, "docs": True, "data": False, "tools": False},
    "QA":       {"backend": True,  "frontend": True,  "css": False, "docs": True, "data": False, "tools": True},
}

# ── Format compact markdown ──────────────────────────────────────────────

def format_markdown(backend, frontend, css, agent=None):
    out = []
    out.append("# EthOS Codebase Map")
    out.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Root: /opt/ethos/")
    if agent:
        out.append(f"Filtered for: {agent} agent")
    out.append("Architecture: Flask backend + Vanilla JS frontend, no frameworks.\n")

    sections = _AGENT_SECTIONS.get(agent, {}) if agent else {}

    # Backend
    if backend:
        out.append("## Backend")
        for item in backend:
            f = item["file"]
            L = item["lines"]
            if "note" in item:
                out.append(f"  {f} ({L}L) \u2014 {item['note']}")
            elif "prefix" in item:
                out.append(f"  {f} ({L}L) [{item['prefix']}] {item['routes']}R")
            elif "functions" in item:
                out.append(f"  {f} ({L}L) \u2014 {', '.join(item['functions'][:6])}")

    # Frontend
    if frontend:
        out.append("\n## Frontend JS")
        for item in frontend:
            f = item["file"]
            L = item["lines"]
            apps = item.get("apps", [])
            fns = item.get("key_fns", [])
            parts = []
            if apps:
                parts.append("Apps: " + ", ".join(apps))
            if fns:
                parts.append("Fns: " + ", ".join(fns[:8]))
            detail = " | ".join(parts) if parts else ""
            out.append(f"  {f} ({L}L)")
            if detail:
                out.append(f"    {detail}")

    # CSS
    if css:
        out.append("\n## CSS")
        for item in css:
            out.append(f"  {item['file']} ({item['lines']}L)")
            if item.get("sections"):
                out.append(f"    Sections: {', '.join(item['sections'][:15])}")

    # Docs
    if not sections or sections.get("docs", True):
        out.append("\n## Docs")
        if os.path.isdir(os.path.join(ROOT, "docs")):
            for fname in sorted(os.listdir(os.path.join(ROOT, "docs"))):
                if fname.endswith(".md"):
                    fpath = os.path.join(ROOT, "docs", fname)
                    out.append(f"  docs/{fname} ({_count(fpath)}L)")

    # Data (>1KB only)
    if not sections or sections.get("data", True):
        out.append("\n## Data (>1KB)")
        if os.path.isdir(os.path.join(ROOT, "data")):
            for fname in sorted(os.listdir(os.path.join(ROOT, "data"))):
                fpath = os.path.join(ROOT, "data", fname)
                if os.path.isfile(fpath):
                    sz = os.path.getsize(fpath)
                    if sz >= 1024:
                        out.append(f"  data/{fname} ({round(sz/1024, 1)}KB)")

    # Tools
    if not sections or sections.get("tools", True):
        out.append("\n## Tools")
        if os.path.isdir(os.path.join(ROOT, "tools")):
            for fname in sorted(os.listdir(os.path.join(ROOT, "tools"))):
                fpath = os.path.join(ROOT, "tools", fname)
                if os.path.isfile(fpath):
                    out.append(f"  tools/{fname} ({_count(fpath)}L)")

    return "\n".join(out)


def generate(agent=None):
    """Generate codebase map, optionally filtered by agent type.
    When agent is provided, only relevant sections are included (40-50% smaller)."""
    backend = scan_backend()
    frontend = scan_frontend()
    css = scan_css()
    if agent and agent in _AGENT_SECTIONS:
        sections = _AGENT_SECTIONS[agent]
        if not sections.get("backend"):
            backend = []
        if not sections.get("frontend"):
            frontend = []
        if not sections.get("css"):
            css = []
    return format_markdown(backend, frontend, css, agent=agent)


def main():
    parser = argparse.ArgumentParser(description="EthOS Codebase Map Generator")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    if args.json:
        result = {
            "generated": datetime.now().isoformat(),
            "backend": scan_backend(),
            "frontend": scan_frontend(),
            "css": scan_css(),
        }
        print(json.dumps(result, indent=2))
    else:
        print(generate())


if __name__ == "__main__":
    main()
