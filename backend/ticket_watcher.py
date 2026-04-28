#!/usr/bin/env python3
"""
Ticket Watcher — analyzes tickets using Ollama and APPLIES fixes directly to source files.
Runs as a long-lived service (ethos-ticket-watcher.service).

Fix pipeline per ticket:
  1. _resolve_ticket_with_ai():
     a. Aider (primary) — reads file, applies changes using Qwen2.5-Coder-32B via Ollama
     b. _fix_with_ollama_strict() (fallback) — strict # OLD: / # NEW: re-prompt
  2. Retry after QA FAIL:
     a. Aider (primary) — includes QA feedback in the message
     b. _parse_and_apply_ollama_fix() — parse Ollama response for OLD/NEW blocks
     c. _fix_with_ollama_strict() — last-resort strict re-prompt
  3. Comment posted = actual unified diff of what changed
  4. Ticket moved to QA only if code was actually changed
"""

import os
import sys
import re
import json
import time
import signal
import logging
import difflib
import hashlib
import shutil
import sqlite3
import subprocess
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blueprints.tickets_db import get_projects, get_tickets, get_ticket, update_ticket, create_ticket
from host import data_path
import uuid as _uuid

# ── Config ──────────────────────────────────────────────────────────────────

LOG_DIR        = '/opt/ethos/logs/ollama_tickets'
WATCHER_LOG    = '/opt/ethos/logs/ticket_watcher_new.log'
LOCK_FILE      = '/tmp/.ethos_watcher_executing'
STATUS_FILE    = '/tmp/.ethos_watcher_status'
AICHAT_CONFIG  = '/opt/ethos/data/aichat_config_{}.json'
DEFAULT_OLLAMA = 'http://localhost:11434'
CLAUDE_BIN      = shutil.which('claude') or '/home/marcin/.local/bin/claude'
CLAUDE_TIMEOUT  = 600        # seconds for a single Claude CLI run
LOOP_INTERVAL  = 120        # seconds between full queue sweeps
STREAM_TIMEOUT = None       # no timeout — let Ollama run as long as needed
KEEP_ALIVE      = 600        # keep model loaded 10 min after last use (not forever — prevents OOM)
NUM_CTX_DEFAULT = 65536      # context for fix/QA prompts  (96 GB DDR5: ~56 GB free → ~229K possible)
NUM_CTX_AUDIT   = 131072     # full-file audit pass — full 128K (Qwen2.5-Coder-32B native max)
OLLAMA_AUTHOR  = 'ollama-watcher'
QA_AUTHOR      = 'ollama-qa'        # separate identity for QA verification comments
AUDIT_INTERVAL = 2 * 24 * 3600   # 2 days between audits per app
AUDIT_SCHEDULE = '/opt/ethos/data/audit_schedule.json'
EVENTLOG_DB           = '/opt/ethos/logs/eventlog.db'
EVENTLOG_WATCHER_STATE = '/opt/ethos/data/eventlog_watcher.json'
EVENTLOG_CHECK_INTERVAL = 6 * 3600   # check event log every 6 hours
EVENTLOG_LOOKBACK       = 48 * 3600  # consider errors from last 48 hours

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [watcher] %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(WATCHER_LOG, encoding='utf-8'),
    ]
)
log = logging.getLogger('watcher')

_running = True

def _handle_signal(sig, frame):
    global _running
    log.info(f'Got signal {sig}, shutting down gracefully…')
    _running = False

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_ollama_url():
    """Read Ollama URL from any existing aichat config file."""
    data_dir = os.path.dirname(data_path('x'))
    for fname in os.listdir(data_dir):
        if fname.startswith('aichat_config_') and fname.endswith('.json'):
            try:
                with open(os.path.join(data_dir, fname)) as f:
                    cfg = json.load(f)
                if cfg.get('ollama_url'):
                    return cfg['ollama_url'].rstrip('/')
            except Exception:
                pass
    return os.environ.get('OLLAMA_URL', DEFAULT_OLLAMA)

def _last_ollama_comment_ts(ticket):
    """Return unix timestamp of the last ollama-watcher OR ollama-qa comment, or 0."""
    comments = ticket.get('comments') or []
    ts = 0
    for c in comments:
        author = c.get('author') or c.get('user') or ''
        if author in (OLLAMA_AUTHOR, QA_AUTHOR):
            ts = max(ts, float(c.get('created') or c.get('created_at') or 0))
    return ts

def _was_returned_from_qa(ticket: dict, cols: list) -> bool:
    """True if a **human** moved the ticket back to In Progress after AI applied a fix.

    Guards against false positives:
    - Watcher's own QA-move (seconds after AI comment) → require gap > LOOP_INTERVAL
    - QA FAIL auto-return (Ollama-qa comment + move) → 'qa-failed' label is set;
      we skip this function's logic entirely when that label is present.
    """
    # QA FAIL is handled separately via qa-failed label — not a human return
    labels = ticket.get('labels') or []
    if 'qa-failed' in labels:
        return False

    last_ai = _last_ollama_comment_ts(ticket)
    if not last_ai:
        return False  # AI never touched it

    # Must currently be In Progress — only then can it have been "returned"
    in_progress_col = _col_in_progress(cols).lower()
    column = (ticket.get('column') or '').lower()
    if column != in_progress_col:
        return False

    updated = float(ticket.get('updated') or ticket.get('updated_at') or 0)
    # Require update to be at least one full loop interval after the last AI comment
    # to avoid treating the watcher's own QA-move (seconds later) as a return signal.
    return updated > last_ai + LOOP_INTERVAL


def _qa_model(project: dict) -> str:
    """Return the fast QA/dedup model. Falls back to the main fix model."""
    return project.get('qa_model', '').strip() or project.get('ollama_model', 'mistral')


def _build_retry_prompt(ticket, project) -> str:
    """Prompt for a ticket returned to In Progress — includes all QA feedback and human comments."""
    title    = ticket.get('title', '').strip()
    desc     = (ticket.get('description') or '').strip()
    column   = ticket.get('column', '')
    labels   = ', '.join(ticket.get('labels') or []) or 'none'
    model    = _qa_model(project)
    prev_ai      = []
    qa_feedback  = []
    human_after  = []
    last_ai_ts   = 0.0

    for c in (ticket.get('comments') or []):
        author = c.get('author') or c.get('user') or '?'
        ts     = float(c.get('created') or c.get('created_at') or 0)
        text   = (c.get('text') or '').strip()

        if author == OLLAMA_AUTHOR:
            prev_ai.append(text[:1500])
            last_ai_ts = max(last_ai_ts, ts)
        elif author == QA_AUTHOR:
            # Always include QA feedback — it's the primary rejection reason
            qa_feedback.append(text[:2000])
        elif ts > last_ai_ts:
            human_after.append(f'[{author}]: {text[:500]}')

    ai_block    = ('\n---\n'.join(prev_ai[-2:])) or '(none)'
    qa_block    = ('\n---\n'.join(qa_feedback[-3:])) or '(no QA feedback)'
    human_block = ('\n'.join(human_after)) or '(no human feedback)'

    return f"""You are a senior developer fixing a rejected ticket. Your previous fix was reviewed by QA and FAILED.

## Ticket: {title}
Labels: {labels} | Status: {column}

## Description
{desc[:2000]}

## Your previous fix attempt(s)
{ai_block}

## ❌ QA rejection reason(s) — READ THIS CAREFULLY
{qa_block}

## Human feedback (if any)
{human_block}

## Your task
QA rejected your fix. You MUST address every point QA raised above.
- Do NOT repeat the same approach.
- Explain specifically what was wrong and why the new approach is correct.
- If QA says the fix is incomplete, cover ALL affected places in the code.

## ⚠️ REQUIRED OUTPUT FORMAT — follow EXACTLY
For each change, wrap it in a fenced code block using `# OLD:` and `# NEW:` markers:

```python
# OLD:
<exact lines to replace — must match the file verbatim>

# NEW:
<replacement lines>
```

Multiple changes = multiple such blocks. Do NOT use unified diff (--- / +++ lines).

Model: {model}"""


def _get_source_snippet(app_id: str, max_bytes: int = 50000) -> tuple:
    """Return (backend_code, frontend_code, bp_path, js_path) for an app_id."""
    try:
        sys.path.insert(0, '/opt/ethos/backend')
        from blueprints.smart_auditor import SmartAppAuditor
        a = SmartAppAuditor(app_id, app_id, {})
        bp = a._find_blueprint_file()
        js = a._find_frontend_file()
        bp_code = open(bp, encoding='utf-8', errors='ignore').read()[:max_bytes] if bp else ''
        js_code = open(js, encoding='utf-8', errors='ignore').read()[:max_bytes] if js else ''
        return bp_code, js_code, bp or '', js or ''
    except Exception:
        return '', '', '', ''


def _build_prompt(ticket, project):
    """Build a detailed analysis prompt from a ticket, including source code when available."""
    title       = ticket.get('title', '').strip()
    description = (ticket.get('description') or '').strip()
    column      = ticket.get('column', '')
    priority    = ticket.get('priority', 'medium')
    labels_list = ticket.get('labels') or []
    labels      = ', '.join(labels_list) or 'brak'
    assignee    = ticket.get('assignee') or 'nieprzypisany'

    comments_text = ''
    for c in (ticket.get('comments') or [])[-5:]:
        author = c.get('author') or c.get('user') or '?'
        if author == OLLAMA_AUTHOR:
            continue
        text = (c.get('text') or '').strip()[:500]
        comments_text += f'\n  [{author}]: {text}'
    if not comments_text:
        comments_text = '\n  (brak)'

    project_name = project.get('name', '')
    ollama_model = project.get('ollama_model', 'mistral')

    # Extract app_id from labels (e.g. "app:antivirus")
    app_id = None
    for lbl in labels_list:
        if lbl.startswith('app:'):
            app_id = lbl[4:]
            break

    source_block = ''
    if app_id:
        bp_code, js_code, bp_path, js_path = _get_source_snippet(app_id)
        if bp_code:
            source_block += f'\n\n## Backend source: `{os.path.basename(bp_path)}`\n```python\n{bp_code}\n```'
        if js_code:
            source_block += f'\n\n## Frontend source: `{os.path.basename(js_path)}`\n```javascript\n{js_code}\n```'

    app_context = f' (application: **{app_id}**)' if app_id else ''

    prompt = f"""You are a senior developer working on the EthOS NAS project.
You are analysing a ticket{app_context} in project "{project_name}".

## Ticket
- Title: {title}
- Priority: {priority}
- Status: {column}
- Labels: {labels}
- Assignee: {assignee}

## Description
{description or '(no description)'}

## Human comments
{comments_text}
{source_block}

## Your task
1. Understand the exact problem described in the ticket.
2. Locate the issue in the source code above (quote the relevant lines).
3. Provide a CONCRETE fix — show exact old code → new code.
4. Explain WHY this fixes the issue in 2-3 sentences.
5. Note any related files that may also need changes.

Be specific. Do not give generic advice. Model: {ollama_model}"""
    return prompt

def _unload_other_models(ollama_url: str, keep_model: str):
    """Unload all Ollama models except keep_model to free RAM before loading a large model."""
    try:
        ps = requests.get(f"{ollama_url}/api/ps", timeout=10).json()
        for m in ps.get('models', []):
            name = m.get('name', '')
            if name and name != keep_model:
                requests.post(f"{ollama_url}/api/chat", json={
                    'model': name,
                    'messages': [{'role': 'user', 'content': '.'}],
                    'keep_alive': 0,
                }, timeout=15)
                log.info(f'Unloaded model from RAM: {name}')
    except Exception as e:
        log.debug(f'_unload_other_models error: {e}')


def _warmup_model(ollama_url: str, model: str):
    """Unload other models then load this one into memory."""
    _unload_other_models(ollama_url, model)
    try:
        requests.post(
            f"{ollama_url}/api/chat",
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': '.'}],
                'stream': False,
                'keep_alive': KEEP_ALIVE,
                'options': {'num_predict': 1},
            },
            timeout=180,
        )
        log.debug(f'Warmed up model: {model}')
    except Exception as e:
        log.debug(f'Warmup failed for {model}: {e}')


def _stream_ollama(ollama_url, model, prompt, log_path, num_ctx=NUM_CTX_DEFAULT):
    """
    Stream Ollama response, write to log file, return full response text.
    Returns (response_text, tokens_in, tokens_out).
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    ts_start = time.time()

    header = (
        f"=== Model: {model} ===\n"
        f"=== Started: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        f"=== Agent: ollama-watcher ===\n\n"
        f"--- Prompt ---\n{prompt}\n\n--- Response ---\n"
    )

    full_response = ''
    tokens_in = 0
    tokens_out = 0

    with open(log_path, 'w', encoding='utf-8') as lf:
        lf.write(header)
        lf.flush()

        try:
            resp = requests.post(
                f"{ollama_url}/api/chat",
                json={
                    'model': model,
                    'messages': [
                        {'role': 'system', 'content': 'Jesteś pomocnym asystentem programistycznym.'},
                        {'role': 'user',   'content': prompt},
                    ],
                    'stream': True,
                    'keep_alive': KEEP_ALIVE,
                    'options': {'temperature': 0.4, 'num_predict': 4096, 'num_ctx': num_ctx},
                },
                stream=True,
                timeout=STREAM_TIMEOUT,
            )
            resp.raise_for_status()

            for raw_line in resp.iter_lines():
                if not _running:
                    break
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                content = (chunk.get('message') or {}).get('content') or ''
                if content:
                    full_response += content
                    lf.write(content)
                    lf.flush()
                if chunk.get('done'):
                    usage = chunk.get('prompt_eval_count', 0), chunk.get('eval_count', 0)
                    tokens_in, tokens_out = usage

        except Exception as e:
            error_line = f'\n[ERROR] {e}\n'
            lf.write(error_line)
            lf.flush()
            log.error(f'Ollama stream error: {e}')

        elapsed = time.time() - ts_start
        elapsed_str = f'{int(elapsed // 60)}m {int(elapsed % 60)}s'
        footer = (
            f'\n\n=== Total session time: {elapsed_str} ===\n'
            f'=== Tokens in: {tokens_in} | Tokens out: {tokens_out} ===\n'
        )
        lf.write(footer)

    return full_response.strip(), tokens_in, tokens_out

def _emit_watcher_event(payload: dict):
    """Write status file so Flask /watcher/status picks it up on next poll."""
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump({**payload, 'pid': os.getpid(), 'ts': time.time()}, f)
    except Exception:
        pass


def _write_lock(ticket_id, model, project_name, log_file, phase='fix'):
    payload = {
        'ticket_id': ticket_id,
        'model': {'model': model, 'label': f'Ollama / {model}'},
        'agent': 'ollama',
        'started': time.time(),
        'copilot_pid': os.getpid(),
        'log_file': os.path.basename(log_file),
        'qa_cycle': 0,
        'project': project_name,
        'phase': phase,
    }
    try:
        with open(LOCK_FILE, 'w') as f:
            json.dump(payload, f)
    except Exception as e:
        log.warning(f'Lock write failed: {e}')
    _emit_watcher_event({'type': 'watcher_start', 'executing': True, **payload})


def _clear_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass
    _emit_watcher_event({'type': 'watcher_done', 'executing': False, 'phase': 'idle'})


def _write_audit_status(app_id: str, model: str, project_name: str):
    _emit_watcher_event({
        'type': 'watcher_audit',
        'executing': False,
        'phase': 'audit',
        'app_id': app_id,
        'model': model,
        'project': project_name,
    })

def _add_comment(ticket_id, text, model, author=None, prefix=None):
    ticket = get_ticket(ticket_id)
    if not ticket:
        return
    author  = author or OLLAMA_AUTHOR
    prefix  = prefix or f'🤖 **Ollama ({model}):**\n\n'
    comments = list(ticket.get('comments') or [])
    comments.append({
        'id': f'c_{_uuid.uuid4().hex[:8]}',
        'author': author,
        'user': author,
        'text': f'{prefix}{text}',
        'created': time.time(),
    })
    update_ticket(ticket_id, {'comments': comments})


# ── Column name helpers (support PL/EN column names) ────────────────────────

def _col_in_progress(cols):
    return next((c for c in cols
                 if 'progress' in c.lower() or 'trakcie' in c.lower()), 'In Progress')

def _col_qa(cols):
    """Return the QA column name (Ollama verification stage)."""
    return next((c for c in cols if c.lower() == 'qa'), None)

def _col_review(cols):
    """Return the Review column name (human review stage)."""
    return next((c for c in cols if 'review' in c.lower()), None)

# ── Audit scheduler helpers ─────────────────────────────────────────────────

def _load_schedule() -> dict:
    try:
        with open(AUDIT_SCHEDULE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_schedule(sched: dict):
    try:
        with open(AUDIT_SCHEDULE, 'w') as f:
            json.dump(sched, f, indent=2)
    except Exception as e:
        log.warning(f'Schedule save failed: {e}')

def _audit_due(sched: dict, key: str) -> bool:
    entry = sched.get(key, {})
    last  = entry.get('last_audit', 0)
    return time.time() - last > AUDIT_INTERVAL

def _get_all_app_ids() -> list:
    """Return all known app IDs by scanning blueprint filenames."""
    bp_dir = '/opt/ethos/backend/blueprints'
    skip = {'__init__', 'admin_required', 'db_pool', 'api_docs',
            'app_manager', 'appstore', 'builder', 'installer'}
    ids = []
    for fname in sorted(os.listdir(bp_dir)):
        if not fname.endswith('.py'):
            continue
        base = fname[:-3]
        if base in skip or base.startswith('builder_'):
            continue
        # Convert _ to - for app_id convention
        ids.append(base.replace('_', '-'))
    return ids

def _resolve_app_ids(spec: str) -> list:
    """Expand audit_apps spec ('*', or comma list) to actual app_id list."""
    spec = spec.strip()
    if not spec:
        return []
    if spec == '*':
        return _get_all_app_ids()
    return [s.strip() for s in spec.split(',') if s.strip()]

def _get_project_column(project: dict, col_name: str) -> str:
    """Return first column matching name, or fallback."""
    cols = project.get('columns') or ['To Do', 'In Progress', 'QA', 'Done']
    if isinstance(cols, list) and cols:
        for c in cols:
            if col_name.lower() in c.lower():
                return c
        return cols[0]
    return 'To Do'

def _audit_app_for_project(project: dict, app_id: str, ollama_url: str) -> int:
    """Run SmartAppAuditor for one app, create tickets. Returns tickets created count."""
    import sys, re as _re, json as _json
    sys.path.insert(0, '/opt/ethos/backend')
    from blueprints.smart_auditor import SmartAppAuditor

    model = project.get('ollama_model', 'mistral')
    dedup_model = _qa_model(project)
    log.info(f'Auditing app "{app_id}" for project "{project["name"]}" with {model}')
    _write_audit_status(app_id, model, project.get('name', ''))

    # Unload other models before loading the (large) audit model
    _unload_other_models(ollama_url, model)

    auditor = SmartAppAuditor(
        app_id=app_id,
        app_name=app_id.replace('-', ' ').title(),
        app_data={},
        ollama_url=ollama_url,
        ollama_model=model,
        large_context=True,
    )
    findings = auditor.analyze()
    tasks    = auditor.generate_tasks()

    if not tasks:
        log.info(f'No findings for {app_id}')
        return 0

    col_todo    = _get_project_column(project, 'to do')
    col_backlog = _get_project_column(project, 'backlog')
    now         = time.time()
    project_id  = project['id']

    existing_tickets = get_tickets(project_id)

    # ── fast normalised-title pre-filter ──────────────────────────────────
    def _norm(title):
        t = (title or '').lower().strip()
        t = _re.sub(r'[^\w\s]', ' ', t)
        t = _re.sub(r'\s+', ' ', t).strip()
        return t

    existing_norms = {_norm(t.get('title', '')) for t in existing_tickets}
    candidates = [t for t in tasks if _norm(t.get('title', '')) not in existing_norms]

    # ── AI dedup: semantic duplicate check via Ollama ─────────────────────
    if candidates and existing_tickets and ollama_url and dedup_model:
        existing_summary = '\n'.join(f'- {t.get("title","")}' for t in existing_tickets[:120])
        new_summary = '\n'.join(f'{i}. {t.get("title","")}' for i, t in enumerate(candidates))
        prompt = (
            "You are a ticket deduplication assistant.\n\n"
            "EXISTING TICKETS:\n" + existing_summary + "\n\n"
            "CANDIDATE NEW TICKETS:\n" + new_summary + "\n\n"
            "Which candidates are NOT duplicates of existing tickets?\n"
            "Reply ONLY with a JSON array of 0-based indices of non-duplicates.\n"
            "Example: [0, 2, 4]"
        )
        try:
            resp = requests.post(
                f'{ollama_url.rstrip("/")}/api/generate',
                json={'model': dedup_model, 'prompt': prompt, 'stream': False,
                      'options': {'temperature': 0, 'num_predict': 256}},
                timeout=None,  # no timeout on dedup check
            )
            raw = resp.json().get('response', '')
            m = _re.search(r'\[[\d,\s]*\]', raw)
            if m:
                keep = _json.loads(m.group())
                before = len(candidates)
                candidates = [candidates[i] for i in keep if i < len(candidates)]
                log.info(f'AI dedup: {before - len(candidates)} duplicate(s) removed for {app_id}')
        except Exception as e:
            log.warning(f'AI dedup failed for {app_id}: {e}')

    created = 0
    for task in candidates:
        title      = task.get('title', '')[:200]
        is_feature = task.get('is_feature', False)
        labels     = list(task.get('labels', []))
        labels.append(f'app:{app_id}')
        labels.append('auto-audit')

        # Feature suggestions → Backlog (human decides when to implement)
        # Bug/security/logic tickets → To Do (watcher picks them up)
        target_col = col_backlog if is_feature else col_todo

        ticket = create_ticket({
            'id':          f't_{_uuid.uuid4().hex[:12]}',
            'project_id':  project_id,
            'title':       title,
            'description': task.get('description', ''),
            'column':      target_col,
            'priority':    task.get('priority', 'medium'),
            'reporter':    OLLAMA_AUTHOR,
            'labels':      labels,
            'comments':    [],
            'created':     now,
            'updated':     now,
        })
        if ticket:
            created += 1

    log.info(f'Created {created} tickets from audit of {app_id} ({len(tasks) - created} duplicate(s) skipped)')
    return created


def _run_audits(projects: list, ollama_url: str) -> bool:
    """Audit at most ONE due app across all projects per call.

    Returns True if an audit was performed (Phase 2 should still run after).
    Spreading audits one-per-cycle ensures Phase 2 (ticket queue) gets
    Ollama attention every 2 minutes instead of waiting for all 54 apps.
    """
    sched = _load_schedule()

    for project in projects:
        if not _running:
            return False
        if not project.get('ollama_enabled'):
            continue
        audit_spec = (project.get('audit_apps') or '').strip()
        if not audit_spec:
            continue

        app_ids = _resolve_app_ids(audit_spec)
        for app_id in app_ids:
            if not _running:
                return False
            key = f"{project['id']}:{app_id}"
            if not _audit_due(sched, key):
                continue

            # Found one due audit — run it, then return so Phase 2 can run
            try:
                count = _audit_app_for_project(project, app_id, ollama_url)
                sched[key] = {
                    'last_audit':      time.time(),
                    'tickets_created': count,
                    'model':           project.get('ollama_model', ''),
                }
                log.info(f'Audit done: {key} → {count} tickets')
            except Exception as e:
                log.error(f'Audit failed for {key}: {e}', exc_info=True)
                # Mark as attempted so we don't retry immediately
                sched[key] = {
                    'last_audit':      time.time(),
                    'tickets_created': 0,
                    'model':           project.get('ollama_model', ''),
                    'error':           str(e),
                }

            _save_schedule(sched)
            return True  # one audit done — yield back to main loop

    return False  # nothing was due


# ── Resolution helpers ───────────────────────────────────────────────────────

def _get_source_for_ticket(ticket: dict) -> tuple:
    """
    Returns (file_path, label) for the source file relevant to this ticket.
    Looks at ticket labels for layer:* and app:* tags.
    If no layer:* label is present, falls back to parsing **File:** `filename`
    from the ticket description and inferring layer from the file extension.
    """
    labels = ticket.get('labels') or []
    layer  = next((l.split(':', 1)[1] for l in labels if l.startswith('layer:')), None)
    app_id = next((l.split(':', 1)[1] for l in labels if l.startswith('app:')), None)

    if not app_id:
        return None, None

    # If no layer label, infer from **File:** `filename` in the description
    if not layer:
        desc = ticket.get('description') or ''
        file_m = re.search(r'\*\*File:\*\*\s*`([^`]+)`', desc)
        if file_m:
            fname_hint = file_m.group(1).strip()
            ext = os.path.splitext(fname_hint)[1].lower()
            if ext == '.js':
                layer = 'frontend'
            elif ext == '.py':
                layer = 'backend'

    root = '/opt/ethos'

    # Use auditor's file discovery
    import sys
    sys.path.insert(0, '/opt/ethos/backend')
    from blueprints.smart_auditor import SmartAppAuditor
    a = SmartAppAuditor(app_id=app_id, app_name=app_id, app_data={})

    if layer == 'frontend':
        path = a._find_frontend_file()
        return path, 'frontend JS'
    elif layer == 'database':
        bp = a._find_blueprint_file()
        path = a._find_db_file(bp)
        return path, 'database layer'
    else:
        path = a._find_blueprint_file()
        return path, 'backend blueprint'


def _build_resolution_prompt(ticket: dict, source_path: str, source_label: str, project: dict) -> str:
    title   = ticket.get('title', '')
    desc    = ticket.get('description', '')
    labels  = ', '.join(ticket.get('labels') or [])
    finding = ''

    # Extract finding details from description if present
    loc_m = re.search(r'\*\*Location:\*\*\s*`([^`]+)`', desc)
    fix_m = re.search(r'\*\*Fix:\*\*\s*(.+?)(?:\n|$)', desc)
    if loc_m:
        finding += f'Location: {loc_m.group(1)}\n'
    if fix_m:
        finding += f'Suggested fix direction: {fix_m.group(1)}\n'

    try:
        code = open(source_path, encoding='utf-8', errors='ignore').read()
        # For large files, focus on the relevant section if location is known
        if loc_m and len(code) > 40000:
            loc_hint = loc_m.group(1)
            lines = code.splitlines(keepends=True)
            # Find lines around the location
            match_lines = [i for i, l in enumerate(lines) if loc_hint in l]
            if match_lines:
                start = max(0, match_lines[0] - 20)
                end   = min(len(lines), match_lines[0] + 120)
                code  = ''.join(lines[start:end])
    except Exception:
        code = '(could not read source file)'

    return f"""You are a senior developer fixing a bug in the EthOS NAS project.

## Ticket: {title}
Labels: {labels}

## Problem description
{desc[:2000]}

{finding}

## Source file: {os.path.basename(source_path)} ({source_label})
```
{code[:60000]}
```

## Your task
1. Identify the exact location of the bug in the code above.
2. Provide the EXACT fix — show the old code and the corrected replacement.
3. Explain WHY this fixes the issue.
4. If additional files need changes, mention them.

Format your response as:
**Root cause:** (one sentence)
**Fix in `{os.path.basename(source_path)}`:**
```python  (or js)
# OLD:
<problematic code>

# NEW:
<fixed code>
```
**Explanation:** (2-3 sentences)
**Additional changes needed:** (or "None")"""


def _parse_and_apply_ollama_fix(response: str, source_path: str) -> tuple:
    """Parse # OLD: / # NEW: code blocks from Ollama response and apply to source_path.

    Returns (success, patches_applied, diff_text).
    Supports fenced blocks:  ```python\\n# OLD:\\n...\\n# NEW:\\n...\\n```
    """
    if not source_path or not os.path.exists(source_path):
        return False, 0, ''

    try:
        with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
            original = f.read()
    except Exception as e:
        log.warning(f'_parse_and_apply: cannot read {source_path}: {e}')
        return False, 0, ''

    content = original
    applied = 0

    block_re = re.compile(
        r'```[a-zA-Z]*\s*\n'
        r'#\s*OLD:\s*\n(.*?)\n#\s*NEW:\s*\n(.*?)\n```',
        re.DOTALL | re.IGNORECASE,
    )
    for m in block_re.finditer(response):
        old_code = m.group(1).strip()
        new_code = m.group(2).strip()
        if old_code and old_code in content:
            content = content.replace(old_code, new_code, 1)
            applied += 1

    if applied == 0:
        return False, 0, ''

    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        content.splitlines(keepends=True),
        fromfile=f'a/{os.path.basename(source_path)}',
        tofile=f'b/{os.path.basename(source_path)}',
        n=3,
    ))
    diff_text = ''.join(diff_lines[:200])

    try:
        with open(source_path, 'w', encoding='utf-8') as f:
            f.write(content)
        log.info(f'_parse_and_apply: applied {applied} patch(es) to {source_path}')
        return True, applied, diff_text
    except Exception as e:
        log.error(f'_parse_and_apply: write failed for {source_path}: {e}')
        return False, 0, ''


def _restart_service_if_needed(source_path: str):
    """Restart ethos.service if the fixed file is part of the running backend.
    Frontend JS files don't need a restart (served statically).
    """
    if not source_path:
        return
    # Only restart for backend Python files
    if not source_path.endswith('.py'):
        return
    if '/opt/ethos/backend' not in source_path and '/opt/ethos/installer' not in source_path:
        return
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'restart', 'ethos'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info(f'ethos.service restarted after fix to {os.path.basename(source_path)}')
        else:
            log.warning(f'ethos.service restart failed: {result.stderr.strip()}')
    except Exception as e:
        log.warning(f'Could not restart ethos.service: {e}')


def _fix_with_aider(ticket: dict, source_path: str, model: str, ollama_url: str) -> tuple:
    """Use Aider to apply code fixes for a ticket.

    Runs Aider from the repo root (/opt/ethos) with full git repo map so it
    understands the project structure and cross-file dependencies.
    QA feedback from ticket comments is included in the message.
    Returns (success, summary, diff_text).
    """
    ticket_id = ticket.get('id', 'unknown')
    fname     = os.path.basename(source_path)
    title     = ticket.get('title', '').strip()
    desc      = (ticket.get('description') or '').strip()

    # Collect QA feedback from previous QA comments
    qa_feedback = []
    for c in (ticket.get('comments') or []):
        author = c.get('author') or c.get('user') or ''
        text   = (c.get('text') or '').strip()
        if author == QA_AUTHOR and text:
            qa_feedback.append(text[:2000])

    msg_parts = [f"Fix this issue in {fname}:", "", f"## Ticket: {title}"]
    if desc:
        msg_parts.append(f"\n{desc[:3000]}")
    if qa_feedback:
        msg_parts.append("\n\n## QA Feedback (previous fix was rejected — address these specific issues):")
        msg_parts.append('\n---\n'.join(qa_feedback[-3:]))
    message = '\n'.join(msg_parts)

    try:
        with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
            before = f.read()
    except Exception as e:
        return False, f'Cannot read source: {e}', ''

    # Capture git-tracked changes before aider runs so we can detect edits
    # to files other than source_path (aider may follow **File:** hints in desc)
    try:
        _git_before = set(subprocess.check_output(
            ['git', 'diff', '--name-only'], cwd=repo_root, text=True
        ).splitlines())
    except Exception:
        _git_before = set()

    tmp_dir = '/tmp/aider_sessions'
    os.makedirs(tmp_dir, exist_ok=True)
    safe_ticket = re.sub(r'[^a-zA-Z0-9_-]', '_', ticket_id)

    repo_root = '/opt/ethos'
    # Make source_path relative to repo root for Aider (it runs from repo_root)
    try:
        rel_source = os.path.relpath(source_path, repo_root)
    except ValueError:
        rel_source = source_path

    # Key shared files that give Aider project context (read-only)
    context_files = [
        'backend/utils.py',
        'backend/host.py',
        'backend/i18n.py',
    ]
    read_args = []
    for cf in context_files:
        cf_abs = os.path.join(repo_root, cf)
        if os.path.exists(cf_abs) and cf != rel_source:
            read_args += ['--read', cf]

    env = os.environ.copy()
    env['OLLAMA_API_BASE'] = ollama_url
    # Suppress aider analytics / update checks
    env.setdefault('AIDER_NO_AUTO_COMMITS', '1')
    env['OLLAMA_NUM_CTX'] = '131072'  # Qwen2.5-Coder-32B native max; 96 GB RAM handles KV-cache

    cmd = [
        '/opt/ethos/venv/bin/aider',
        '--model', f'ollama/{model}',
        '--message', message,
        '--yes-always',
        '--no-auto-commits',
        '--map-tokens', '32768',
        '--max-chat-history-tokens', '65536',
        '--model-metadata-file', '/opt/ethos/data/aider_model_metadata.json',
        '--chat-history-file', f'{tmp_dir}/{safe_ticket}_hist.md',
        '--input-history-file', f'{tmp_dir}/{safe_ticket}_input.md',
    ] + read_args + [rel_source]

    log.info(f'Aider fix for {fname} (ticket {ticket_id}) model={model} cwd={repo_root}')
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, cwd=repo_root,
        )
        aider_lines = []
        for line in proc.stdout:
            line = line.rstrip('\n')
            aider_lines.append(line)
            log.info(f'[aider] {line}')
        proc.wait()
        rc = proc.returncode
    except Exception as e:
        log.warning(f'Aider failed: {e}')
        return False, str(e), ''

    try:
        with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
            after = f.read()
    except Exception:
        return False, 'Cannot read source after aider', ''

    if after == before:
        # source_path unchanged — check if aider edited a different file
        try:
            _git_after = set(subprocess.check_output(
                ['git', 'diff', '--name-only'], cwd=repo_root, text=True
            ).splitlines())
        except Exception:
            _git_after = set()
        new_changes = _git_after - _git_before
        if new_changes:
            alt_path = os.path.join(repo_root, sorted(new_changes)[0])
            log.info(f'Aider: no change to {fname} but modified {sorted(new_changes)} — using first')
            try:
                alt_before = subprocess.check_output(
                    ['git', 'show', f'HEAD:{sorted(new_changes)[0]}'],
                    cwd=repo_root, text=True, errors='ignore'
                )
                with open(alt_path, encoding='utf-8', errors='ignore') as f:
                    alt_after = f.read()
                alt_fname = os.path.basename(alt_path)
                diff_lines = list(difflib.unified_diff(
                    alt_before.splitlines(keepends=True),
                    alt_after.splitlines(keepends=True),
                    fromfile=f'a/{alt_fname}',
                    tofile=f'b/{alt_fname}',
                    n=3,
                ))
                diff_text = ''.join(diff_lines[:200])
                log.info(f'Aider applied changes to {alt_fname} (via git diff)')
                return True, 'Aider fix applied', diff_text
            except Exception as e:
                log.warning(f'Could not build diff for {alt_path}: {e}')
                return True, 'Aider fix applied', ''
        log.info(f'Aider: no changes applied to {fname} (rc={rc})')
        return False, 'No changes applied', ''

    diff_lines = list(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f'a/{fname}',
        tofile=f'b/{fname}',
        n=3,
    ))
    diff_text = ''.join(diff_lines[:200])
    log.info(f'Aider applied changes to {fname}')
    return True, 'Aider fix applied', diff_text


def _fix_with_ollama_strict(ticket: dict, source_path: str, model: str, ollama_url: str,
                            prior_response: str = '') -> tuple:
    """Second-pass Ollama call that enforces strict # OLD: / # NEW: output format.

    Called when the first Ollama response couldn't be parsed/applied.
    Re-asks the model with the current file content and a format-enforcement prompt.

    Returns (success, summary, diff_text).
    """
    try:
        with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
            original = f.read()
    except Exception:
        return False, 'Cannot read source file', ''

    title  = ticket.get('title', '')
    desc   = (ticket.get('description') or '').strip()
    fname  = os.path.basename(source_path)
    ext    = os.path.splitext(fname)[1].lower()
    lang   = 'python' if ext == '.py' else ('javascript' if ext == '.js' else 'text')

    prior_block = f'\nYour previous attempt (unparseable):\n{prior_response[:2000]}\n' if prior_response else ''

    prompt = f"""You are a senior developer applying a precise code fix to a file.
{prior_block}
## Ticket: {title}
{desc[:1500]}

## Current content of {fname}:
```{lang}
{original[:60000]}
```

## STRICT INSTRUCTIONS
Output ONLY fenced code blocks in this EXACT format — nothing else:

```{lang}
# OLD:
<copy exact lines from the file that need replacing>

# NEW:
<replacement lines>
```

- You may emit multiple such blocks for multiple changes.
- The OLD section MUST be verbatim text that exists in the file above.
- Do NOT add explanations outside the code blocks.
- If no change is needed, output: NO_CHANGE"""

    log.info(f'Strict-format Ollama pass for {fname} (ticket {ticket.get("id")})')
    tmp_log = os.path.join(LOG_DIR, f'{ticket.get("id","x")}_strict_{int(time.time())}.log')
    try:
        response, _, _ = _stream_ollama(ollama_url, model, prompt, tmp_log)
    except Exception as e:
        log.warning(f'_fix_with_ollama_strict Ollama call failed: {e}')
        return False, str(e), ''

    if not response or 'NO_CHANGE' in response.upper()[:20]:
        log.info(f'_fix_with_ollama_strict: model says no change needed')
        return False, 'Model says no change needed', ''

    fixed, n, diff_text = _parse_and_apply_ollama_fix(response, source_path)
    if fixed:
        log.info(f'_fix_with_ollama_strict: applied {n} patch(es) to {fname}')
        return True, f'{n} patch(es) applied', diff_text

    log.warning(f'_fix_with_ollama_strict: still could not parse response for {fname}')
    return False, 'Could not parse strict-format response', ''


def _build_qa_prompt(ticket: dict, source_path: str, diff_text: str) -> str:
    """Build a deep QA verification prompt for Ollama."""
    title = ticket.get('title', '').strip()
    desc  = (ticket.get('description') or '').strip()

    try:
        with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
            code = f.read()[:40000]
    except Exception:
        code = '(could not read file)'

    ext = os.path.splitext(source_path)[1].lower()
    lang = 'python' if ext == '.py' else ('javascript' if ext == '.js' else 'text')

    return f"""You are a thorough QA engineer reviewing an automated code fix for the EthOS NAS project.

## Ticket: {title}
## Problem description:
{desc[:2000]}

## Applied diff:
```diff
{diff_text[:5000]}
```

## Full current state of the file after the fix:
```{lang}
{code}
```

## Your QA task — answer ALL of these:
1. Does the diff actually fix the described problem? Trace through the logic carefully.
2. Does the fix introduce any new bugs, regressions, or security vulnerabilities?
3. Is the code syntactically and logically correct?
4. Are there edge cases the fix misses?
5. Does the fix integrate correctly with the rest of the file?

**Respond starting with exactly PASS or FAIL on the first line:**
- `PASS` — fix is correct, complete, and safe
- `FAIL: <clear explanation of what is still wrong and what must be done differently>`

Be specific. If FAIL, describe exactly what the correct fix should look like."""


def _qa_verify_ticket(ticket: dict, project: dict, ollama_url: str) -> bool:
    """Run Ollama deep QA verification on a ticket sitting in the QA column.

    PASS → remove qa-failed label (if present), move to Review column.
    FAIL → add qa-failed label, post rejection comment, move back to In Progress.
    Returns True if verification ran (regardless of verdict), False if skipped.
    """
    ticket_id = ticket['id']
    title     = ticket.get('title', '')
    cols      = project.get('columns') or []
    model     = _qa_model(project)

    source_path, _ = _get_source_for_ticket(ticket)
    review_col     = _col_review(cols)
    in_prog_col    = _col_in_progress(cols)

    if not source_path or not os.path.exists(source_path):
        log.info(f'QA {ticket_id}: no source file — auto-advancing to Review')
        if review_col:
            update_ticket(ticket_id, {'column': review_col})
        return True

    # Extract last AI-applied diff from fix comments
    diff_text = ''
    for c in reversed(ticket.get('comments') or []):
        if c.get('author') == OLLAMA_AUTHOR:
            text = c.get('text') or ''
            m = re.search(r'```diff\n(.*?)```', text, re.DOTALL)
            if m:
                diff_text = m.group(1)
                break

    if not diff_text:
        log.info(f'QA {ticket_id}: no diff in comments — auto-advancing to Review')
        if review_col:
            update_ticket(ticket_id, {'column': review_col})
        return True

    ts       = int(time.time())
    log_file = os.path.join(LOG_DIR, f'{ticket_id}_qa_{ts}.log')

    log.info(f'QA verifying {ticket_id} "{title}" with {model}')
    prompt = _build_qa_prompt(ticket, source_path, diff_text)

    _write_lock(ticket_id, model, project.get('name', ''), log_file)
    try:
        response, tok_in, tok_out = _stream_ollama(ollama_url, model, prompt, log_file)
    finally:
        _clear_lock()

    if not response:
        log.warning(f'QA {ticket_id}: empty Ollama response — skipping this cycle')
        return False

    passed = response.strip().upper().startswith('PASS')
    labels = list(ticket.get('labels') or [])

    if passed:
        comment = f'✅ **QA PASS** (Ollama {model}) — fix verified, moving to Review.\n\n{response[:1500]}'
        _add_comment(ticket_id, comment, model, author=QA_AUTHOR, prefix='')
        labels = [l for l in labels if l != 'qa-failed']
        update_ticket(ticket_id, {'labels': labels})
        if review_col:
            update_ticket(ticket_id, {'column': review_col})
            log.info(f'QA PASS: {ticket_id} → "{review_col}"')
    else:
        comment = f'❌ **QA FAIL** (Ollama {model}) — fix rejected, needs rework.\n\n{response[:2000]}'
        _add_comment(ticket_id, comment, model, author=QA_AUTHOR, prefix='')
        if 'qa-failed' not in labels:
            labels.append('qa-failed')
        update_ticket(ticket_id, {'column': in_prog_col, 'labels': labels})
        log.info(f'QA FAIL: {ticket_id} → "{in_prog_col}" (qa-failed label added)')

    log.info(f'QA done: {ticket_id} passed={passed} {tok_in}/{tok_out} tokens')
    return True



def _resolve_ticket_with_ai(ticket: dict, project: dict, ollama_url: str) -> bool:
    """Fix a ticket using Aider (primary) with Ollama strict-format as fallback.

    Finds the source file, applies the fix, posts a diff comment, and moves to QA.
    Returns True if a fix was applied and the ticket moved to QA.
    Returns False if no source file is found (caller falls back to analysis-only flow).
    """
    ticket_id = ticket['id']

    source_path, source_label = _get_source_for_ticket(ticket)
    if not source_path or not os.path.exists(source_path):
        log.debug(f'No source file for ticket {ticket_id}, using standard analysis')
        return False

    model = _qa_model(project)
    log.info(f'Resolving ticket {ticket_id} "{ticket.get("title","")}" with Aider — {source_label}')

    # Primary: Aider handles file reading and editing directly
    fixed, summary, diff_text = _fix_with_aider(ticket, source_path, model, ollama_url)
    method = f'Aider {model}' if fixed else ''

    if not fixed:
        # Fallback: strict-format Ollama re-prompt
        fixed, summary, diff_text = _fix_with_ollama_strict(ticket, source_path, model, ollama_url)
        if fixed:
            method = f'Ollama strict-format {model}'

    if not fixed:
        log.info(f'Could not auto-apply fix for {ticket_id} — will post analysis comment')
        return False

    if diff_text:
        comment_text = f'🔧 **Fix applied by {method}:**\n\n```diff\n{diff_text}\n```'
    else:
        comment_text = f'🔧 **Fix applied by {method}** (no diff available)'

    _add_comment(ticket_id, comment_text, model)
    _restart_service_if_needed(source_path)

    cols   = project.get('columns') or []
    qa_col = _col_qa(cols) or next((c for c in cols if 'qa' in c.lower()), None)
    if not qa_col:
        qa_col = _col_review(cols)
    if qa_col and ticket.get('column') != qa_col:
        update_ticket(ticket_id, {'column': qa_col})
        log.info(f'Moved ticket {ticket_id} to "{qa_col}"')

    log.info(f'Resolution done: {ticket_id} fixed={fixed} method={method}')
    return True


# ── Event log → ticket integration ──────────────────────────────────────────

def _load_eventlog_state() -> dict:
    try:
        with open(EVENTLOG_WATCHER_STATE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_eventlog_state(state: dict):
    try:
        with open(EVENTLOG_WATCHER_STATE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f'eventlog state save failed: {e}')

def _normalize_event_msg(msg: str) -> str:
    """Strip dynamic parts (IPs, IDs, numbers, paths) for grouping."""
    msg = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '<ip>', msg)
    msg = re.sub(r'\b[0-9a-f-]{8,}\b', '<id>', msg)
    msg = re.sub(r'(?<!\w)\d+(?!\w)', '<n>', msg)
    msg = re.sub(r'["\']?/[\w./_-]+["\']?', '<path>', msg)
    return msg.strip()[:200]

def _simple_ollama(ollama_url: str, model: str, prompt: str, timeout: int = None) -> str:
    """Non-streaming Ollama call for short JSON responses."""
    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': 'You are a helpful assistant. Return only valid JSON when asked.'},
                    {'role': 'user', 'content': prompt},
                ],
                'stream': False,
                'keep_alive': KEEP_ALIVE,
                'options': {'temperature': 0.2, 'num_predict': 1024},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data.get('message') or {}).get('content', '').strip()
    except Exception as e:
        log.warning(f'_simple_ollama error: {e}')
        return ''

def _extract_json_obj(text: str) -> str:
    """Extract the first {...} JSON object from text."""
    start = text.find('{')
    end   = text.rfind('}')
    if start != -1 and end > start:
        return text[start:end + 1]
    return text

def _check_event_log_errors(projects: list, ollama_url: str, max_per_cycle: int = 3) -> int:
    """
    Scan event log for recent errors and create tickets for significant patterns.
    Processes at most max_per_cycle groups to avoid blocking QA/fix phases.
    Returns number of tickets created.
    """
    if not os.path.exists(EVENTLOG_DB):
        return 0

    # Only projects with Ollama enabled receive event-log tickets
    target_projects = [p for p in projects if p.get('ollama_enabled')]
    if not target_projects:
        return 0

    state = _load_eventlog_state()
    now   = time.time()
    if now - state.get('last_check', 0) < EVENTLOG_CHECK_INTERVAL:
        return 0

    processed_hashes = set(state.get('processed_hashes', []))
    since = now - EVENTLOG_LOOKBACK

    try:
        conn = sqlite3.connect(EVENTLOG_DB)
        c    = conn.cursor()
        c.execute(
            'SELECT ts, category, level, message, details FROM events '
            'WHERE level IN ("error", "warn") AND ts > ? ORDER BY ts DESC LIMIT 300',
            (since,)
        )
        rows = c.fetchall()
        conn.close()
    except Exception as e:
        log.warning(f'Event log read failed: {e}')
        state['last_check'] = now
        _save_eventlog_state(state)
        return 0

    # Group by normalised message
    groups: dict = {}
    for ts, category, level, message, details in rows:
        norm = _normalize_event_msg(message or '')
        key  = f'{category}:{norm}'
        h    = hashlib.md5(key.encode()).hexdigest()[:16]
        if h in processed_hashes:
            continue
        if key not in groups:
            groups[key] = {
                'category': category, 'level': level,
                'messages': [], 'details': [],
                'hash': h, 'ts': ts,
            }
        groups[key]['messages'].append(message or '')
        groups[key]['details'].append(details or '')
        groups[key]['ts'] = max(groups[key]['ts'], ts)

    if not groups:
        state['last_check'] = now
        _save_eventlog_state(state)
        return 0

    log.info(f'Event log: {len(groups)} unprocessed error group(s) to analyze')

    # Use the primary project (prefer one with audit_apps='*' or named ethos)
    primary = next(
        (p for p in target_projects
         if p.get('audit_apps') == '*' or 'ethos' in p.get('name', '').lower()),
        target_projects[0]
    )
    model       = _qa_model(primary)
    col_todo    = _get_project_column(primary, 'to do')
    project_id  = primary['id']

    def _norm_title(t):
        return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', (t or '').lower())).strip()

    existing_tickets = get_tickets(project_id)
    existing_norms   = {_norm_title(t.get('title', '')) for t in existing_tickets}

    created_total = 0
    processed_count = 0

    for key, group in groups.items():
        if not _running:
            break
        if processed_count >= max_per_cycle:
            log.debug(f'Event log: reached max_per_cycle={max_per_cycle}, deferring rest to next cycle')
            break

        cat     = group['category']
        level   = group['level']
        count   = len(group['messages'])
        h       = group['hash']

        # Skip single warnings — only act on errors or repeated warnings
        if count < 2 and level != 'error':
            processed_hashes.add(h)
            continue

        sample_msgs    = group['messages'][:5]
        sample_details = [d for d in group['details'][:3] if d]

        msgs_str    = '\n'.join(f'- {m}' for m in sample_msgs)
        details_str = '\n'.join(sample_details[:2]) if sample_details else 'N/A'

        prompt = (
            f"Analyze these EthOS system errors from the event log and create a bug ticket.\n\n"
            f"Category: {cat}\nLevel: {level}\nOccurrences: {count}\n"
            f"Error messages:\n{msgs_str}\n"
            f"Details (sample):\n{details_str}\n\n"
            f'Create a concise bug ticket. Respond ONLY with valid JSON:\n'
            f'{{"title": "short descriptive title (max 80 chars)", '
            f'"description": "what the bug is, possible cause, and suggested fix (3-5 sentences)", '
            f'"priority": "critical|high|medium|low", '
            f'"labels": ["array", "of", "relevant", "labels"]}}'
        )

        try:
            response = _simple_ollama(ollama_url, model, prompt)
            processed_count += 1  # count every Ollama call against the cycle budget
            if not response:
                processed_hashes.add(h)
                continue

            ticket_data = None
            for attempt in (response, _extract_json_obj(response)):
                try:
                    ticket_data = json.loads(attempt)
                    break
                except Exception:
                    pass

            if not ticket_data or not ticket_data.get('title'):
                processed_hashes.add(h)
                continue

            raw_title = ticket_data['title'].strip()
            title = f'🔴 {raw_title}'

            if _norm_title(title) in existing_norms:
                log.debug(f'Event log ticket already exists: {title}')
                processed_hashes.add(h)
                continue

            desc  = ticket_data.get('description', '')
            desc += (
                f'\n\n**Source:** Event Log | Category: `{cat}` | '
                f'Level: `{level}` | Occurrences: {count}\n'
                f'**Sample messages:**\n' +
                '\n'.join(f'- `{m[:200]}`' for m in sample_msgs[:3])
            )

            labels = (ticket_data.get('labels') or []) + [
                'event-log', 'auto-generated', f'category:{cat}',
            ]
            priority = ticket_data.get('priority', 'medium')

            new_ticket = create_ticket({
                'project_id': project_id,
                'title':      title,
                'description': desc,
                'column':     col_todo,
                'priority':   priority,
                'labels':     labels,
                'created':    time.time(),
                'updated':    time.time(),
            })
            if new_ticket:
                log.info(f'Created event-log ticket: {title}')
                existing_norms.add(_norm_title(title))
                created_total += 1

        except Exception as e:
            log.warning(f'Event log ticket error for group {key}: {e}')

        processed_hashes.add(h)

    state['last_check']        = now
    state['processed_hashes']  = list(processed_hashes)[-500:]
    _save_eventlog_state(state)

    if created_total:
        log.info(f'Event log scan: {created_total} new ticket(s) created')
    return created_total


# ── Main watcher loop ────────────────────────────────────────────────────────

def _process_ticket(ticket, project, ollama_url):
    """Analyze a single ticket with Ollama. Returns True on success."""
    ticket_id = ticket['id']
    model     = _qa_model(project)  # fast coder model for analysis
    ts        = int(time.time())
    log_file  = os.path.join(LOG_DIR, f'{ticket_id}_process_{ts}.log')

    log.info(f'Processing ticket {ticket_id} "{ticket.get("title","")}" with model {model}')

    prompt = _build_prompt(ticket, project)

    _write_lock(ticket_id, model, project.get('name', ''), log_file)

    try:
        response, tok_in, tok_out = _stream_ollama(ollama_url, model, prompt, log_file)
    finally:
        _clear_lock()

    if not response:
        log.warning(f'Empty response for ticket {ticket_id}')
        return False

    _add_comment(ticket_id, response, model)
    log.info(f'Done: {ticket_id} — {tok_in} in / {tok_out} out tokens, log: {os.path.basename(log_file)}')
    return True

def run():
    log.info('Ticket Watcher starting…')
    os.makedirs(LOG_DIR, exist_ok=True)
    _clear_lock()

    # Track which model was last loaded to avoid redundant warmups
    _last_warmed: dict = {}

    while _running:
        try:
            ollama_url = _get_ollama_url()
            projects   = get_projects()
            processed  = 0
            log.info('Cycle start')

            def _ensure_model(model: str):
                """Warm up model only if it changed since last cycle."""
                if _last_warmed.get('model') != model:
                    _warmup_model(ollama_url, model)
                    _last_warmed['model'] = model

            # ── Phase 1: QA verification (highest priority — already fixed, needs sign-off) ──
            # All QA tickets use qa_model (Qwen2.5-Coder-32B). Batch them first so the
            # QA model stays loaded the whole time — no mid-phase model switch.
            for project in projects:
                if not _running:
                    break
                if not project.get('ollama_enabled'):
                    continue

                cols    = project.get('columns') or ['To Do', 'In Progress', 'QA', 'Review', 'Done']
                qa_col  = _col_qa(cols)
                if not qa_col:
                    continue

                qa_col_name = qa_col.lower()
                qa_tickets  = [t for t in get_tickets(project['id'])
                               if (t.get('column') or '').lower() == qa_col_name]

                for ticket in qa_tickets:
                    if not _running:
                        break
                    _ensure_model(_qa_model(project))
                    ok = _qa_verify_ticket(ticket, project, ollama_url)
                    if ok:
                        processed += 1
                    break  # one ticket per project per cycle

                if processed > 0:
                    break  # one QA ticket total per cycle

            # ── Phase 2: Fix tickets ──────────────────────────────────────
            if not processed:
                for project in projects:
                    if not _running:
                        break
                    if not project.get('ollama_enabled'):
                        continue

                    cols        = project.get('columns') or ['To Do', 'In Progress', 'QA', 'Review', 'Done']
                    in_progress = _col_in_progress(cols)
                    review_col  = _col_review(cols)

                    tickets = get_tickets(project['id'])

                    in_progress_lc  = in_progress.lower()
                    in_prog_tickets = [t for t in tickets
                                       if (t.get('column') or '').lower() == in_progress_lc]

                    review_tickets  = [t for t in tickets
                                       if review_col and
                                          (t.get('column') or '').lower() == review_col.lower()]

                    try:
                        ip_idx = next(i for i, c in enumerate(cols)
                                      if 'progress' in c.lower() or 'trakcie' in c.lower())
                        intake_cols_lc = {c.lower() for c in cols[:ip_idx]}
                    except StopIteration:
                        intake_cols_lc = {'to do', 'backlog', 'do zrobienia'}

                    todo_tickets = [t for t in tickets
                                    if (t.get('column') or '').lower() in intake_cols_lc]

                    # Always process available work — Review tickets don't block new claims.
                    # Human can review/approve in the background at any time.
                    candidates = in_prog_tickets + todo_tickets

                    for ticket in candidates:
                        if not _running:
                            break

                        labels      = ticket.get('labels') or []
                        qa_failed   = 'qa-failed' in labels
                        returned    = _was_returned_from_qa(ticket, cols)
                        just_claimed = (ticket.get('column') or '').lower() in intake_cols_lc

                        if qa_failed:
                            # Count how many times QA already failed — stop after 15 attempts
                            comments = ticket.get('comments') or []
                            qa_fail_count = sum(
                                1 for c in comments
                                if (c.get('author') or c.get('user')) == QA_AUTHOR
                                and 'QA FAIL' in (c.get('text') or c.get('body') or '')
                            )
                            if qa_fail_count >= 15:
                                # Escalate to human: move to Review and mark blocked
                                blocked_labels = [l for l in labels if l != 'qa-failed'] + ['qa-blocked']
                                review_col = _col_review(cols)
                                if review_col:
                                    update_ticket(ticket['id'], {'column': review_col, 'labels': blocked_labels})
                                    _add_comment(ticket['id'],
                                        f'🚫 **QA BLOCKED** — failed QA {qa_fail_count} times in a row. '
                                        f'Needs human review before AI retries.',
                                        'system', author=QA_AUTHOR, prefix='')
                                    log.info(f'Escalated {ticket["id"]} to human review after {qa_fail_count} QA failures')
                                else:
                                    update_ticket(ticket['id'], {'labels': blocked_labels})
                                continue

                            # Cooldown between qa-failed retries: 5 min to avoid hammering Ollama
                            last_ts = _last_ollama_comment_ts(ticket)
                            if time.time() - last_ts < 300:
                                continue

                        if (ticket.get('column') or '').lower() in intake_cols_lc:
                            update_ticket(ticket['id'], {'column': in_progress})
                            ticket['column'] = in_progress
                            log.info(f'Claimed ticket {ticket["id"]} "{ticket.get("title","")}" → {in_progress}')

                        if returned or qa_failed:
                            reason = 'returned from Review' if returned else 'QA FAIL'
                            log.info(f'Retrying {ticket["id"]} "{ticket.get("title","")}" ({reason})')
                            ticket_id    = ticket['id']
                            model        = _qa_model(project)
                            ts           = int(time.time())
                            log_file     = os.path.join(LOG_DIR, f'{ticket_id}_retry_{ts}.log')
                            prompt       = _build_retry_prompt(ticket, project)
                            _write_lock(ticket_id, model, project.get('name', ''), log_file)
                            try:
                                response, tok_in, tok_out = _stream_ollama(ollama_url, model, prompt, log_file)
                            finally:
                                _clear_lock()

                            if response:
                                source_path, _ = _get_source_for_ticket(ticket)
                                fixed        = False
                                diff_text    = ''
                                retry_method = ''
                                if source_path and os.path.exists(source_path):
                                    # Primary: Aider applies the fix directly
                                    fixed, _, diff_text = _fix_with_aider(ticket, source_path, model, ollama_url)
                                    if fixed:
                                        retry_method = f'Aider {model}'
                                    if not fixed:
                                        # Fallback: parse Ollama response for OLD/NEW blocks
                                        fixed, n, diff_text = _parse_and_apply_ollama_fix(response, source_path)
                                        if fixed:
                                            retry_method = f'Ollama {model} ({n} patch(es))'
                                    if not fixed:
                                        fixed, _, diff_text = _fix_with_ollama_strict(
                                            ticket, source_path, model, ollama_url, response)
                                        if fixed:
                                            retry_method = f'Ollama strict-format {model}'

                                if fixed and diff_text:
                                    comment_text = (f'🔄 **Retry fix applied by {retry_method}:**\n\n'
                                                    f'```diff\n{diff_text}\n```')
                                elif fixed:
                                    comment_text = f'🔄 **Retry fix applied by {retry_method}**'
                                else:
                                    comment_text = (f'⚠️ **Retry — AI could not apply any changes automatically.**\n\n'
                                                    f'AI suggestion:\n{response[:2000]}\n\n'
                                                    f'Ticket stays in In Progress — manual intervention or next cycle needed.')
                                _add_comment(ticket_id, comment_text, model)

                                if fixed:
                                    _restart_service_if_needed(source_path)
                                    labels = [l for l in labels if l != 'qa-failed']
                                    qa_target = _col_qa(cols) or _col_review(cols)
                                    if qa_target:
                                        update_ticket(ticket_id, {'column': qa_target, 'labels': labels})
                                        log.info(f'Retry done — {ticket_id} → "{qa_target}"')
                                    else:
                                        update_ticket(ticket_id, {'labels': labels})
                                else:
                                    # No changes applied — stay in In Progress, don't send to QA
                                    log.warning(f'Retry {ticket_id} — no code changes applied, staying in In Progress')
                                ok = True
                            else:
                                ok = False
                        else:
                            ok = _resolve_ticket_with_ai(ticket, project, ollama_url)
                            if not ok:
                                # If ticket was just claimed from To Do, treat as fresh —
                                # user may have manually reset it for re-processing.
                                ai_comments = 0 if just_claimed else sum(
                                    1 for c in (ticket.get('comments') or [])
                                    if (c.get('author') or c.get('user')) == OLLAMA_AUTHOR
                                )
                                if ai_comments >= 3:
                                    # AI tried 3+ times without a code fix → escalate to human
                                    esc_col = _col_review(cols) or _col_qa(cols)
                                    esc_labels = list(set((ticket.get('labels') or []) + ['needs-manual']))
                                    if esc_col:
                                        update_ticket(ticket['id'], {'column': esc_col, 'labels': esc_labels})
                                    else:
                                        update_ticket(ticket['id'], {'labels': esc_labels})
                                    _add_comment(ticket['id'],
                                        f'🚦 **Escalated to human review** — AI analyzed this ticket '
                                        f'{ai_comments} times but could not apply any code fix automatically. '
                                        f'Manual intervention required.',
                                        _qa_model(project), author=OLLAMA_AUTHOR, prefix='')
                                    log.info(f'Escalated {ticket["id"]} → "{esc_col}" after {ai_comments} AI attempts without fix')
                                    ok = True
                                else:
                                    ok = _process_ticket(ticket, project, ollama_url)

                        if ok:
                            processed += 1
                        break

                    if processed > 0:
                        break

            if processed == 0:
                log.info('No tickets to process this cycle.')

            # ── Phase 3: app audit (low priority — every 2 days per app) ──
            # Runs AFTER QA and fix so slow AI models don't starve QA tickets.
            _run_audits(projects, ollama_url)

            # ── Phase 4: event log error → tickets (max 3 per cycle) ──
            _check_event_log_errors(projects, ollama_url)
        except Exception as e:
            log.error(f'Watcher cycle error: {e}', exc_info=True)

        # Wait before next sweep
        for _ in range(LOOP_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    log.info('Ticket Watcher stopped.')
    _clear_lock()
if __name__ == '__main__':
    run()
