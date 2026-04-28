"""
SmartAppAuditor — comprehensive multi-pass AI analysis with Ollama.
Analyzes backend blueprint, DB layer, and frontend JS in separate focused passes.
"""
import os
import re
import json
import requests
import time
from typing import Dict, List, Any, Optional, Tuple


# ── Constants ─────────────────────────────────────────────────────────────────

# Explicit app_id → file mappings where the name doesn't follow conventions
_BP_OVERRIDES = {
    'ai-chat':          'aichat.py',
    'download-manager': 'downloads.py',
    'docker-manager':   'docker_manager.py',
    'disk-repair':      'diskrepair.py',
    'mail-server':      'mail_server.py',
    'radio-music':      'radio_music.py',
    'video-station':    'video_station.py',
    'cloud-backup':     'cloud_backup.py',
    'usb-flasher':      'flasher.py',
    'sharing-samba':    'sharing.py',
    'sharing-nfs':      'sharing.py',
    'sharing-dlna':     'dlna.py',
    'sharing-webdav':   'sharing.py',
    'sharing-sftp':     'sharing.py',
    'sharing-ftp':      'sharing.py',
    'domains-manager':  'domains_manager.py',
    'websites':         'domains_manager.py',
    'surveillance':     'security_advisor.py',
}

_JS_OVERRIDES = {
    'ai-chat':          'aichat.js',
    'download-manager': 'downloads.js',
    'docker-manager':   'docker-manager.js',
    'code-editor':      'code-editor.js',
    'mail-server':      'mail-server.js',
    'radio-music':      'radio_music.js',
    'video-station':    'video_station.js',
    'cloud-backup':     'cloud-backup.js',
    'usb-flasher':      'flasher.js',
    'sharing-samba':    'storage.js',
    'sharing-nfs':      'storage.js',
    'sharing-dlna':     'dlna.js',
    'sharing-webdav':   'storage.js',
    'sharing-sftp':     'storage.js',
    'sharing-ftp':      'storage.js',
    'domains-manager':  'domains.js',
    'websites':         'domains.js',
    'surveillance':     'security_advisor.js',
    'disk-repair':      'storage.js',
    'remote-log':       'remote-log.js',
}

# Max chars sent to Ollama per pass
# With large_context=True the full file is sent (no truncation).
# num_ctx is set dynamically based on actual prompt length.
_CODE_CHUNK_DEFAULT = 20_000   # conservative for small-RAM setups
_CODE_CHUNK_LARGE   = 0        # 0 = no limit (send full file)


class SmartAppAuditor:
    """Analyzes real app code with multi-pass AI analysis via Ollama."""

    def __init__(self, app_id: str, app_name: str, app_data: Dict[str, Any],
                 ollama_url: Optional[str] = None, ollama_model: Optional[str] = None,
                 large_context: bool = False, progress_cb=None):
        self.app_id       = app_id
        self.app_name     = app_name
        self.app_data     = app_data
        self.root_dir     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.ollama_url   = ollama_url
        self.ollama_model = ollama_model
        self._chunk       = _CODE_CHUNK_LARGE if large_context else _CODE_CHUNK_DEFAULT
        self._progress    = progress_cb or (lambda msg, pct=None: None)
        self.findings: Dict[str, Any] = {
            'security':   [],
            'performance':[],
            'ux_ui':      [],
            'logic':      [],
            'ai_insights':[],
            'features':   [],
            'metadata':   {},
        }

    # ═══════════════════════════════════════════════════════════════
    # Public entry point
    # ═══════════════════════════════════════════════════════════════

    def analyze(self) -> Dict[str, Any]:
        """Run full analysis: static checks + multi-pass AI."""
        # --- static passes (always) ---
        bp_file = self._find_blueprint_file()
        db_file = self._find_db_file(bp_file)
        js_file = self._find_frontend_file()

        self._static_backend(bp_file)
        self._static_frontend(js_file)
        self._measure_sizes(bp_file, js_file)

        # --- AI passes (only if Ollama configured) ---
        if self.ollama_url and self.ollama_model:
            self._ai_full_analysis(bp_file, db_file, js_file)
            self._ai_features_pass(bp_file, js_file)

        return self.findings

    # ═══════════════════════════════════════════════════════════════
    # File discovery
    # ═══════════════════════════════════════════════════════════════

    def _find_blueprint_file(self) -> Optional[str]:
        bp_dir = os.path.join(self.root_dir, 'backend', 'blueprints')
        if self.app_id in _BP_OVERRIDES:
            p = os.path.join(bp_dir, _BP_OVERRIDES[self.app_id])
            if os.path.exists(p):
                return p
        for name in (
            self.app_id + '.py',
            self.app_id.replace('-', '_') + '.py',
            self.app_id.replace('-', '') + '.py',
        ):
            p = os.path.join(bp_dir, name)
            if os.path.exists(p):
                return p
        # Fuzzy: scan for Blueprint registration
        clean = self.app_id.replace('-', '_').lower()
        for fname in os.listdir(bp_dir):
            if not fname.endswith('.py'):
                continue
            if clean in fname.lower():
                return os.path.join(bp_dir, fname)
        return None

    def _find_db_file(self, bp_file: Optional[str]) -> Optional[str]:
        """Look for a companion _db.py file."""
        if not bp_file:
            return None
        base = os.path.splitext(os.path.basename(bp_file))[0]
        candidate = os.path.join(os.path.dirname(bp_file), base + '_db.py')
        return candidate if os.path.exists(candidate) else None

    def _find_frontend_file(self) -> Optional[str]:
        js_dir = os.path.join(self.root_dir, 'frontend', 'js', 'apps')
        if self.app_id in _JS_OVERRIDES:
            p = os.path.join(js_dir, _JS_OVERRIDES[self.app_id])
            if os.path.exists(p):
                return p
        for name in (
            self.app_id + '.js',
            self.app_id.replace('-', '_') + '.js',
            self.app_id.replace('-', '') + '.js',
        ):
            p = os.path.join(js_dir, name)
            if os.path.exists(p):
                return p
        clean = self.app_id.replace('-', '_').lower()
        for fname in os.listdir(js_dir):
            if not fname.endswith('.js'):
                continue
            if clean in fname.lower():
                return os.path.join(js_dir, fname)
        return None

    # ═══════════════════════════════════════════════════════════════
    # Static analysis helpers
    # ═══════════════════════════════════════════════════════════════

    def _static_backend(self, bp_file: Optional[str]) -> None:
        if not bp_file:
            self.findings['metadata']['backend_file'] = 'not_found'
            return
        self.findings['metadata']['backend_file'] = bp_file
        fname = os.path.basename(bp_file)
        try:
            src = open(bp_file, encoding='utf-8', errors='ignore').read()
            self.findings['metadata']['backend_lines'] = len(src.splitlines())
            endpoints = self._extract_endpoints(src)
            self.findings['metadata']['endpoints_found'] = len(endpoints)
            security_findings = self._check_endpoint_security(endpoints) + self._check_for_secrets(src)
            # Enrich each finding with file name and code snippet
            for f in security_findings:
                f.setdefault('file', fname)
                if not f.get('location') and not f.get('description'):
                    # Try to find a relevant snippet
                    keyword = (f.get('message', '') or '').split()[0].lower()
                    pat = None
                    if 'subprocess' in keyword or 'exec' in (f.get('type','') or ''):
                        pat = r'subprocess\.|os\.system|eval\('
                    elif 'secret' in (f.get('type','') or ''):
                        pat = r'password\s*=|secret\s*=|token\s*=|api_key\s*='
                    if pat:
                        snips = self._extract_snippets(src, pat, context=2)
                        if snips:
                            f['description'] = (
                                'Found in `' + fname + '`:\n```python\n' +
                                '\n---\n'.join(snips[:3]) + '\n```'
                            )
            self.findings['security'].extend(security_findings)
        except Exception as e:
            self.findings['security'].append({'type': 'analysis_error', 'severity': 'low',
                                               'message': str(e)})

    @staticmethod
    def _extract_snippets(src: str, pattern: str, context: int = 2) -> list:
        """Return code snippet strings (with line numbers) for each pattern match."""
        all_lines = src.splitlines()
        snippets, seen = [], set()
        for m in re.finditer(pattern, src):
            ln    = src[:m.start()].count('\n')
            start = max(0, ln - context)
            end   = min(len(all_lines), ln + context + 1)
            key   = (start, end)
            if key in seen:
                continue
            seen.add(key)
            block = '\n'.join(f'  {i+1}: {all_lines[i]}' for i in range(start, end))
            snippets.append(block)
            if len(snippets) >= 8:
                break
        return snippets

    def _static_frontend(self, js_file: Optional[str]) -> None:
        if not js_file:
            self.findings['metadata']['frontend_file'] = 'not_found'
            return
        self.findings['metadata']['frontend_file'] = js_file
        fname = os.path.basename(js_file)
        try:
            src   = open(js_file, encoding='utf-8', errors='ignore').read()
            lines = len(src.splitlines())
            self.findings['metadata']['frontend_lines'] = lines

            auth_count = sum(src.count(k) for k in
                             ['isAdmin', 'hasPermission', 'admin_required', 'authorized', 'permission'])
            if not auth_count:
                snips = self._extract_snippets(src, r'\bapi\(', context=1)
                desc  = 'None of the standard permission keywords found in this file.\n'
                if snips:
                    desc += f'\nExample unguarded call in `{fname}`:\n```js\n{snips[0]}\n```'
                self.findings['ux_ui'].append({
                    'type': 'missing_permission_guards', 'severity': 'medium',
                    'file': fname,
                    'message': 'Frontend lacks permission/admin checks — may expose UI to unauthorized users',
                    'description': desc,
                })

            try_ct   = len(re.findall(r'try\s*\{', src))
            api_ct   = len(re.findall(r'\bapi\(', src))
            catch_ct = len(re.findall(r'catch\s*\(', src))

            if try_ct == 0 and lines > 500:
                snips = self._extract_snippets(src, r'\bapi\(', context=2)
                snip_block = '\n---\n'.join(snips[:4]) if snips else '(none found)'
                self.findings['logic'].append({
                    'type': 'missing_error_handling', 'severity': 'medium',
                    'file': fname,
                    'message': 'No try/catch blocks found in large frontend file',
                    'description': (
                        f'`{fname}` has {lines} lines and {api_ct} api() calls but zero try/catch blocks.\n\n'
                        f'Unhandled api() calls (sample):\n```js\n{snip_block}\n```'
                    ),
                })

            if api_ct > catch_ct * 2 and api_ct > 5:
                snips = self._extract_snippets(src, r'\bapi\(', context=2)
                snip_block = '\n---\n'.join(snips[:5]) if snips else '(none found)'
                self.findings['logic'].append({
                    'type': 'inadequate_api_error_handling', 'severity': 'low',
                    'file': fname,
                    'message': f'{api_ct} API calls but only {catch_ct} catch handlers',
                    'description': (
                        f'`{fname}`: {api_ct} api() calls, {catch_ct} catch handlers, {try_ct} try blocks.\n'
                        f'Most api() calls have no error handling — network errors will be silently ignored.\n\n'
                        f'Sample unguarded calls:\n```js\n{snip_block}\n```'
                    ),
                })
        except Exception as e:
            self.findings['logic'].append({'type': 'analysis_error', 'severity': 'low',
                                            'message': str(e)})

    def _measure_sizes(self, bp_file, js_file) -> None:
        for attr, path, key in [
            ('backend_size_kb',  bp_file, 'backend'),
            ('frontend_size_kb', js_file, 'frontend'),
        ]:
            if path and os.path.exists(path):
                kb = round(os.path.getsize(path) / 1024, 1)
                self.findings['metadata'][attr] = kb
                if key == 'frontend' and kb > 500:
                    self.findings['performance'].append({
                        'type': 'large_bundle', 'severity': 'medium',
                        'message': f'Frontend bundle is {kb} KB — consider lazy loading',
                        'size_kb': kb,
                    })
        deps = [d.strip() for d in self.app_data.get('deps_label', '').split(',')
                if d.strip() and 'brak' not in d.lower()]
        self.findings['metadata']['dependencies'] = deps
        if len(deps) > 4:
            self.findings['performance'].append({
                'type': 'many_dependencies', 'severity': 'low',
                'message': f'{len(deps)} dependencies — verify each is necessary',
            })

    def _extract_endpoints(self, content: str) -> List[Dict]:
        endpoints = []
        lines = content.splitlines()
        for i, line in enumerate(lines):
            m = re.search(r"@\w+_bp\.route\('([^']+)'(?:.*methods=\[([^\]]+)\])?", line)
            if not m:
                m = re.search(r'@\w+_bp\.route\("([^"]+)"(?:.*methods=\[([^\]]+)\])?', line)
            if not m:
                continue
            path    = m.group(1)
            methods = (m.group(2) or 'GET').replace("'", '').replace('"', '')
            has_auth = any('@admin_required' in lines[j]
                           for j in range(max(0, i-2), min(len(lines), i+4)))
            endpoints.append({'path': path, 'methods': methods, 'line': i+1,
                               'has_auth': has_auth,
                               'sensitive': self._is_sensitive(path)})
        return endpoints

    def _is_sensitive(self, path: str) -> bool:
        return any(kw in path.lower() for kw in
                   ['install', 'uninstall', 'config', 'setting', 'delete', 'remove',
                    'admin', 'auth', 'permission', 'user', 'password', 'token'])

    def _check_endpoint_security(self, endpoints: List[Dict]) -> List[Dict]:
        issues = []
        unprotected = [e for e in endpoints if e['sensitive'] and not e['has_auth']]
        if unprotected:
            issues.append({
                'type': 'missing_auth', 'severity': 'critical',
                'count': len(unprotected),
                'message': f'{len(unprotected)} sensitive endpoint(s) without @admin_required',
                'endpoints': [f"{e['path']} (line {e['line']})" for e in unprotected],
            })
        return issues

    def _check_for_secrets(self, content: str) -> List[Dict]:
        found   = []
        located = {}
        pat_map = {
            'password': r"password\s*=\s*(?P<q>[\"'])(?P<v>.{4,})(?P=q)",
            'api_key':  r"api_key\s*=\s*(?P<q>[\"'])(?P<v>.{8,})(?P=q)",
            'secret':   r"secret\s*=\s*(?P<q>[\"'])(?P<v>.{8,})(?P=q)",
            'token':    r"token\s*=\s*(?P<q>[\"'])(?P<v>.{12,})(?P=q)",
        }
        for label, pat in pat_map.items():
            found_m = re.search(pat, content, re.I)
            if found_m:
                found.append(label)
                located[label] = content[:found_m.start()].count('\n') + 1
        if found:
            loc_str = ', '.join(f'{k} (line {v})' for k, v in located.items())
            combined = r"password\s*=|api_key\s*=|secret\s*=|token\s*="
            snips = self._extract_snippets(content, combined, context=1)
            desc  = f'Hardcoded credentials found: {loc_str}'
            if snips:
                desc += '\n\n```python\n' + '\n---\n'.join(snips[:3]) + '\n```'
            return [{'type': 'hardcoded_secrets', 'severity': 'critical',
                     'message': f'Potential hardcoded credential(s): {", ".join(found)}',
                     'description': desc}]
        return []

    # ═══════════════════════════════════════════════════════════════
    # Smart code extraction (avoids sending useless boilerplate)
    # ═══════════════════════════════════════════════════════════════

    def _extract_smart_backend(self, path: str) -> str:
        """
        Extract the most informative parts of a large backend file:
        - All route functions (decorator + full body)
        - All class definitions
        - All subprocess/exec/eval calls in context
        - Top-level SQL patterns
        When self._chunk == 0 the full file is returned without truncation.
        """
        try:
            content = open(path, encoding='utf-8', errors='ignore').read()
        except Exception:
            return ''

        # No limit — return whole file (large-RAM mode)
        if self._chunk == 0:
            return content

        lines = content.splitlines(keepends=True)
        selected: List[str] = []
        selected_set = set()
        total = 0

        def add_block(start: int, end: int):
            nonlocal total
            for li in range(start, min(end, len(lines))):
                if li not in selected_set:
                    selected_set.add(li)
                    selected.append(lines[li])
                    total += len(lines[li])
                    if total >= self._chunk:
                        return True
            return False

        # Priority 1: route decorators + their functions
        i = 0
        while i < len(lines) and total < self._chunk:
            line = lines[i]
            if re.search(r'@\w+_bp\.route\(', line):
                # find the end of the function (next def/class at same indent)
                indent = len(line) - len(line.lstrip())
                end = i + 1
                while end < len(lines):
                    nl = lines[end]
                    if nl.strip() and not nl[0].isspace() and end > i + 1:
                        break
                    stripped = nl.lstrip()
                    if stripped.startswith(('def ', 'class ', '@')) and \
                       (len(nl) - len(stripped)) <= indent and end > i + 2:
                        break
                    end += 1
                if add_block(i, min(i + 80, end)):
                    break
            i += 1

        # Priority 2: dangerous patterns in context
        danger_patterns = [
            r'\bsubprocess\b', r'\bos\.system\b', r'\beval\b', r'\bexec\b',
            r'shell=True', r'pickle\.', r'yaml\.load\b',
            r'execute\(.*%.*\)', r'cursor\.execute\(',
        ]
        for pi, line in enumerate(lines):
            if total >= self._chunk:
                break
            if any(re.search(p, line) for p in danger_patterns):
                add_block(max(0, pi - 3), pi + 10)

        result = ''.join(selected)
        if len(result) < 500 and len(content) > 500:
            # Fallback: just take the first chunk
            result = content[:self._chunk]
        return result[:self._chunk]

    def _extract_smart_frontend(self, path: str) -> str:
        """
        Extract interesting JS sections:
        - All function declarations
        - All api() calls with surrounding context
        - XSS-risky patterns (innerHTML, eval)
        When self._chunk == 0 the full file is returned without truncation.
        """
        try:
            content = open(path, encoding='utf-8', errors='ignore').read()
        except Exception:
            return ''

        # No limit — return whole file (large-RAM mode)
        if self._chunk == 0:
            return content

        lines = content.splitlines(keepends=True)
        selected_set: set = set()
        selected: List[str] = []
        total = 0

        def add(start: int, end: int):
            nonlocal total
            for li in range(start, min(end, len(lines))):
                if li not in selected_set:
                    selected_set.add(li)
                    selected.append(lines[li])
                    total += len(lines[li])
                    if total >= self._chunk:
                        return True
            return False

        risky = [r'innerHTML\s*[+]?=', r'\beval\s*\(', r'document\.write\(',
                 r'\.src\s*=', r'location\.href\s*=', r'api\s*\(']
        for pi, line in enumerate(lines):
            if total >= self._chunk:
                break
            if any(re.search(p, line) for p in risky):
                add(max(0, pi - 2), pi + 6)

        result = ''.join(selected)
        if len(result) < 300:
            result = content[:self._chunk]
        return result[:self._chunk]

    # ═══════════════════════════════════════════════════════════════
    # AI analysis — multi-pass
    # ═══════════════════════════════════════════════════════════════

    def _ai_full_analysis(self, bp_file, db_file, js_file) -> None:
        """Run multi-pass AI analysis: optional holistic pass + focused per-file passes."""
        passes_done = 0
        self.findings['metadata']['ai_analysis'] = 'in_progress'
        self.findings['metadata']['ai_passes'] = []

        try:
            # Pass 0 — holistic (only when full files are sent, i.e. large_context mode)
            # Gives the model a complete picture: blueprint + db + frontend + shared helpers.
            if self._chunk == 0 and bp_file:
                self._progress('🔭 AI Pass 0/5: holistic overview...', 5)
                parts = {}
                for label, path in [
                    ('blueprint', bp_file),
                    ('database',  db_file),
                    ('frontend',  js_file),
                ]:
                    if path and os.path.exists(path):
                        parts[label] = open(path, encoding='utf-8', errors='ignore').read()

                shared_dir = os.path.dirname(bp_file)
                for name in ('host.py', 'utils.py', 'db_pool.py', 'i18n.py'):
                    candidates = [
                        os.path.join(shared_dir, name),
                        os.path.join(os.path.dirname(shared_dir), name),
                    ]
                    for c in candidates:
                        if os.path.exists(c):
                            parts[name] = open(c, encoding='utf-8', errors='ignore').read()
                            break

                sections = '\n\n'.join(
                    f'=== {k} ===\n{v}' for k, v in parts.items()
                )
                holistic_result = self._ai_pass(
                    label='holistic',
                    filename=', '.join(parts.keys()),
                    code=sections,
                    focus=(
                        "You have the FULL source of this application — blueprint, database layer, "
                        "frontend JS, and shared utilities. Look for cross-cutting issues that only "
                        "appear when considering all files together: "
                        "- auth checks in backend not reflected in frontend (hidden endpoints), "
                        "- data written to DB but never validated on read-back, "
                        "- race conditions between concurrent API calls, "
                        "- inconsistent error handling across layers, "
                        "- config values used in JS that are never sanitized in backend, "
                        "- missing or wrong HTTP status codes returned to frontend, "
                        "- business logic split incorrectly between layers."
                    ),
                )
                if holistic_result:
                    for item in holistic_result:
                        item['source'] = 'ai_analysis:holistic'
                    self.findings['ai_insights'].extend(holistic_result)
                    self.findings['metadata']['ai_passes'].append('holistic')
                    passes_done += 1
                    self._progress(f'   ✓ holistic: {len(holistic_result)} issue(s)', 15)

            total_passes = 5 if self._chunk == 0 else 4

            # Pass 1 - backend blueprint
            if bp_file:
                self._progress('\U0001f4c2 Loading source files...', 10)
                self._progress(f'\U0001f916 AI Pass {passes_done+1}/{total_passes}: backend (' + os.path.basename(bp_file) + ')...', 20)
                code = self._extract_smart_backend(bp_file)
                result = self._ai_pass(
                    label='backend',
                    filename=os.path.basename(bp_file),
                    code=code,
                    focus=(
                        "Focus on: authentication/authorization gaps, input validation, "
                        "SQL injection, path traversal, command injection, insecure "
                        "deserialization, race conditions, logic errors in business flow."
                    ),
                )
                if result:
                    self.findings['ai_insights'].extend(result)
                    self.findings['metadata']['ai_passes'].append('backend')
                    passes_done += 1
                    self._progress('   \u2713 backend: ' + str(len(result)) + ' issue(s)', 35)

            # Pass 2 - DB layer
            if db_file:
                self._progress('\U0001f916 AI Pass 2/4: database (' + os.path.basename(db_file) + ')...', 40)
                code = open(db_file, encoding='utf-8', errors='ignore').read()[:self._chunk]
                result = self._ai_pass(
                    label='database',
                    filename=os.path.basename(db_file),
                    code=code,
                    focus=(
                        "Focus on: SQL injection via string concatenation, missing parameterized "
                        "queries, lack of transactions, data leaks in error messages, "
                        "unvalidated input stored in DB, missing indexes on frequently queried columns."
                    ),
                )
                if result:
                    self.findings['ai_insights'].extend(result)
                    self.findings['metadata']['ai_passes'].append('database')
                    passes_done += 1
                    self._progress('   \u2713 database: ' + str(len(result)) + ' issue(s)', 52)

            # Pass 3 - frontend JS
            if js_file:
                self._progress('\U0001f916 AI Pass 3/4: frontend (' + os.path.basename(js_file) + ')...', 55)
                code = self._extract_smart_frontend(js_file)
                result = self._ai_pass(
                    label='frontend',
                    filename=os.path.basename(js_file),
                    code=code,
                    focus=(
                        "Focus on: XSS via innerHTML/eval, missing input sanitization, "
                        "CSRF exposure, sensitive data in localStorage, broken auth checks, "
                        "API calls without error handling, insecure direct object references."
                    ),
                )
                if result:
                    self.findings['ai_insights'].extend(result)
                    self.findings['metadata']['ai_passes'].append('frontend')
                    passes_done += 1
                    self._progress('   \u2713 frontend: ' + str(len(result)) + ' issue(s)', 70)

            # Pass 4 - logic flow and error handling (backend)
            if bp_file:
                self._progress('\U0001f916 AI Pass 4/4: logic flow (' + os.path.basename(bp_file) + ')...', 75)
                code = self._extract_smart_backend(bp_file)
                result = self._ai_pass(
                    label='backend',
                    filename=os.path.basename(bp_file),
                    code=code,
                    focus=(
                        "Focus on LOGIC and FLOW issues only (not security or performance): "
                        "missing rollback on partial failures, inconsistent state after errors, "
                        "race conditions between concurrent requests, incorrect business logic "
                        "(wrong conditions, off-by-one, missing edge cases), broken error recovery "
                        "chains, unhandled None/empty returns, missing transaction boundaries, "
                        "incorrect status transitions, misleading error messages to the user."
                    ),
                )
                if result:
                    # tag as flow-analysis to distinguish from generic backend pass
                    for item in result:
                        item['source'] = 'ai_analysis:flow'
                    self.findings['ai_insights'].extend(result)
                    self.findings['metadata']['ai_passes'].append('flow')
                    passes_done += 1
                    self._progress('   \u2713 flow: ' + str(len(result)) + ' issue(s)', 90)

        except Exception as e:
            self.findings['metadata']['ai_analysis'] = 'error: ' + str(e)
            return

        self.findings['metadata']['ai_analysis'] = (
            'completed (' + str(passes_done) + ' pass' + ('es' if passes_done != 1 else '') + ')'
        )

    def _ai_features_pass(self, bp_file, js_file) -> None:
        """
        Suggest new features based on app capabilities + NAS ecosystem context
        (Synology DSM, QNAP QTS, TrueNAS).  Results go into findings['features'].
        """
        code_parts = []
        filenames  = []
        if bp_file and os.path.exists(bp_file):
            code_parts.append(self._extract_smart_backend(bp_file)[:6000])
            filenames.append(os.path.basename(bp_file))
        if js_file and os.path.exists(js_file):
            code_parts.append(self._extract_smart_frontend(js_file)[:4000])
            filenames.append(os.path.basename(js_file))

        if not code_parts:
            return

        code      = '\n\n'.join(code_parts)
        files_str = ' + '.join(filenames) if filenames else self.app_id

        self._progress(f'✨ Feature pass: {self.app_name}…', 95)

        prompt = f"""You are a NAS product manager reviewing the **{self.app_name}** application in EthOS — a Synology DSM-inspired NAS OS.

Source files: `{files_str}`

```
{code}
```

Your task: propose 3–5 genuinely useful NEW FEATURES for this app that don't yet exist in the code above.
Draw inspiration from Synology DSM, QNAP QTS, TrueNAS SCALE, or invent practical features that NAS users would value.
Each feature must be concrete and implementable in this codebase.

Respond with a JSON array. Each item must have:
- "title": feature name, max 70 chars, no emoji
- "description": what it does and why NAS users would find it useful (2–4 sentences)
- "inspiration": "synology" | "qnap" | "truenas" | "original"
- "priority": "high" | "medium" | "low"
- "effort": "small" | "medium" | "large"

Return ONLY the JSON array, no markdown fences."""

        raw = self._call_ollama(prompt)
        if not raw:
            return

        # Strip fences
        raw = re.sub(r'```(?:json)?\s*', '', raw)
        raw = re.sub(r'```\s*', '', raw).strip()

        features: List[Dict] = []
        for attempt in (raw, self._extract_json_array(raw)):
            try:
                data = json.loads(attempt)
                if isinstance(data, list):
                    features = [f for f in data if isinstance(f, dict) and f.get('title')]
                    break
            except (json.JSONDecodeError, TypeError):
                pass

        if features:
            self.findings['features'].extend(features)
            self._progress(f'   ✓ features: {len(features)} suggestion(s)', 98)

    def _ai_pass(self, label: str, filename: str, code: str, focus: str) -> List[Dict]:
        """
        Single focused analysis pass.
        Returns list of finding dicts.
        """
        if not code.strip():
            return []

        prompt = f"""You are a senior security & code quality engineer reviewing the EthOS NAS application.
Analyze the {label} file `{filename}` of the **{self.app_name}** app.

{focus}

```
{code}
```

Respond with a JSON array of findings. Each finding must have:
- "type": one of "security", "logic", "performance", "ux"
- "severity": "critical" | "high" | "medium" | "low"
- "title": short one-line title (max 80 chars)
- "description": clear explanation of the issue (2-4 sentences)
- "location": function name, route path, or line area
- "fix": concrete fix recommendation

If there are no real issues, return an empty array [].
Return ONLY the JSON array, no markdown fences, no preamble."""

        raw = self._call_ollama(prompt)
        return self._parse_findings(raw, label)

    # ═══════════════════════════════════════════════════════════════
    # Ollama client (streaming)
    # ═══════════════════════════════════════════════════════════════

    def _call_ollama(self, prompt: str, timeout: int = 600) -> str:
        """Stream from Ollama, return full response text."""
        chunks: List[str] = []
        # Set num_ctx dynamically: ~1.5 chars per token, +25% headroom, min 32k, max 65536
        # (65536 tokens = ~16 GB KV cache for 32B model — fits in hybrid VRAM+RAM)
        prompt_tokens = int(len(prompt) / 1.5)
        num_ctx = min(max(prompt_tokens * 5 // 4, 32768), 65536)
        try:
            resp = requests.post(
                f"{self.ollama_url.rstrip('/')}/api/chat",
                json={
                    'model': self.ollama_model,
                    'messages': [
                        {'role': 'system',
                         'content': 'You are a code security and quality expert. '
                                    'Return only valid JSON arrays when asked.'},
                        {'role': 'user', 'content': prompt},
                    ],
                    'stream': True,
                    'keep_alive': 600,
                    'options': {
                        'temperature': 0.2,
                        'num_predict': 8192,
                        'num_ctx': num_ctx,
                    },
                },
                stream=True,
                timeout=timeout,
            )
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode('utf-8') if isinstance(line, bytes) else line)
                    delta = (chunk.get('message') or {}).get('content') or ''
                    if delta:
                        chunks.append(delta)
                    if chunk.get('done'):
                        break
                except Exception:
                    continue
        except Exception as e:
            print(f'[SmartAuditor] Ollama error ({self.ollama_model}): {e}')
        return ''.join(chunks)

    # ═══════════════════════════════════════════════════════════════
    # Response parsing (robust — handles partial/fenced JSON)
    # ═══════════════════════════════════════════════════════════════

    def _parse_findings(self, raw: str, label: str) -> List[Dict]:
        if not raw:
            return []

        # Strip markdown fences
        raw = re.sub(r'```(?:json)?\s*', '', raw)
        raw = re.sub(r'```\s*', '', raw)
        raw = raw.strip()

        # Try full parse
        for attempt in (raw, self._extract_json_array(raw)):
            try:
                data = json.loads(attempt)
                if isinstance(data, list):
                    return [self._normalize(item, label) for item in data if isinstance(item, dict)]
            except (json.JSONDecodeError, TypeError):
                pass

        # Last resort: extract individual objects
        findings = []
        for m in re.finditer(r'\{[^{}]+\}', raw, re.DOTALL):
            try:
                obj = json.loads(m.group())
                if isinstance(obj, dict) and ('description' in obj or 'title' in obj):
                    findings.append(self._normalize(obj, label))
            except Exception:
                pass
        return findings

    def _extract_json_array(self, text: str) -> str:
        """Find the outermost [...] block."""
        start = text.find('[')
        end   = text.rfind(']')
        if start != -1 and end > start:
            return text[start:end+1]
        return text

    def _normalize(self, item: Dict, label: str) -> Dict:
        return {
            'type':        item.get('type', 'logic'),
            'severity':    item.get('severity', 'medium'),
            'message':     item.get('title') or item.get('description', ''),
            'description': item.get('description', ''),
            'location':    item.get('location', ''),
            'fix':         item.get('fix', ''),
            'source':      f'ai_analysis:{label}',
        }

    # ═══════════════════════════════════════════════════════════════
    # Task generation
    # ═══════════════════════════════════════════════════════════════

    def generate_tasks(self) -> List[Dict[str, Any]]:
        tasks = []

        priority_map = {'critical': 'critical', 'high': 'high',
                        'medium': 'medium', 'low': 'low'}
        emoji_map    = {'security': '🛡️', 'performance': '🚀',
                        'logic': '⚙️', 'ux_ui': '🎨', 'ai_insights': '🤖'}
        cat_map = {
            # AI type values (from _ai_pass JSON output)
            'security':                      'security',
            'performance':                   'performance',
            'logic':                         'logic',
            'ux':                            'ux_ui',
            'ui':                            'ux_ui',
            # Static analysis type values (legacy)
            'missing_auth':                  'security',
            'hardcoded_secrets':             'security',
            'public_endpoints':              'security',
            'missing_permission_guards':     'ux_ui',
            'large_bundle':                  'performance',
            'many_dependencies':             'performance',
            'missing_error_handling':        'logic',
            'inadequate_api_error_handling': 'logic',
        }

        for bucket, findings in [
            ('security',   self.findings['security']),
            ('performance',self.findings['performance']),
            ('ux_ui',      self.findings['ux_ui']),
            ('logic',      self.findings['logic']),
            ('ai_insights',self.findings['ai_insights']),
        ]:
            for f in findings:
                ftype    = f.get('type', 'logic')
                severity = f.get('severity', 'medium')
                category = cat_map.get(ftype, bucket)
                emoji    = emoji_map.get(category, '📋')
                source   = f.get('source', '')
                msg      = f.get('message', '')
                loc      = f.get('location', '')
                fix      = f.get('fix', '')
                desc_extra = f.get('description', '')

                file_hint = f.get('file', '')
                desc = f"**Finding:** {msg}\n\n"
                if desc_extra and desc_extra != msg:
                    desc += f"{desc_extra}\n\n"
                if loc:
                    desc += f"**Location:** `{loc}`\n\n"
                elif file_hint:
                    desc += f"**File:** `{file_hint}`\n\n"
                if fix:
                    desc += f"**Fix:** {fix}\n\n"
                if 'endpoints' in f:
                    desc += "**Affected endpoints:**\n" + \
                            ''.join(f"- `{ep}`\n" for ep in f['endpoints'][:12])
                    if len(f['endpoints']) > 12:
                        desc += f"- … and {len(f['endpoints'])-12} more\n"

                labels = [category, 'auto-generated']
                if 'ai_analysis' in source:
                    labels.append('ai-detected')
                    layer = source.split(':')[-1]  # backend/database/frontend
                    if layer in ('backend', 'database', 'frontend'):
                        labels.append(f'layer:{layer}')

                # Use AI-generated title directly; static findings use their message
                title = msg[:120] if msg else f'{category} issue'
                tasks.append({
                    'category':       category,
                    'emoji':          emoji,
                    'priority':       priority_map.get(severity, 'medium'),
                    'title':          f"{emoji} {title}",
                    'description':    desc.strip(),
                    'finding_details':f,
                    'labels':         labels,
                })

        # ── Feature suggestions (Backlog only — human decides when to implement) ──
        effort_priority = {'small': 'medium', 'medium': 'medium', 'large': 'low'}
        inspiration_emoji = {'synology': '🔵', 'qnap': '🟣', 'truenas': '🟤', 'original': '💡'}
        for feat in self.findings.get('features', []):
            title       = (feat.get('title') or '').strip()[:120]
            desc        = (feat.get('description') or '').strip()
            inspiration = feat.get('inspiration', 'original')
            priority    = feat.get('priority', 'medium')
            effort      = feat.get('effort', 'medium')
            ie          = inspiration_emoji.get(inspiration, '💡')
            insp_label  = inspiration if inspiration != 'original' else 'original-idea'

            full_desc = f"{desc}\n\n**Inspiration:** {inspiration.capitalize()}"
            if effort:
                full_desc += f" | **Effort:** {effort}"
            tasks.append({
                'category':        'feature',
                'emoji':           '✨',
                'priority':        priority_map.get(priority, 'medium'),
                'title':           f'✨ {title}',
                'description':     full_desc,
                'finding_details': feat,
                'labels':          ['feature', 'backlog', f'inspiration:{insp_label}', 'auto-generated'],
                'is_feature':      True,
            })

        return tasks
