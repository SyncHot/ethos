/* ═══════════════════════════════════════════════════════════
   EthOS — Desktop Engine
   Window Manager, Taskbar, Main Menu, Auth, Core
   ═══════════════════════════════════════════════════════════ */

const NAS = {
    token: null,
    nasName: 'EthOS',
    user: null,   // { username, role }
    sudoMode: false,  // true when admin (always-on for admins)
    apps: [],
    socket: null,
    stats: { cpu: 0, memory_percent: 0, net_up: 0, net_down: 0 },
    toast: null,  // set after toast() is defined
};

// Capture PWA install prompt for reuse
window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    window._ethosInstallPrompt = e;
});

// ─────────────────────────── API Helper ───────────────────────────

async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (NAS.token) headers['Authorization'] = `Bearer ${NAS.token}`;
    // CSRF Protection
    if (NAS.csrfToken) headers['X-CSRFToken'] = NAS.csrfToken;

    if (options.body && !(options.body instanceof FormData)) {
        headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(options.body);
    }
    const resp = await fetch(`/api${path}`, { ...options, headers });
    if (resp.status === 401) {
        showLogin(`API ${path} returned 401 (token invalid or expired)`);
        throw new Error('Unauthorized');
    }
    if (resp.status === 403) {
        const data = await resp.json();
        if (data.code === 'PASSWORD_CHANGE_REQUIRED') {
            showPasswordChangeModal();
            throw new Error('Password change required');
        }
        return data;
    }
    const ct = resp.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {
        return { error: 'API not available (non-JSON response)' };
    }
    const json = await resp.json();
    // Auto-log backend errors (5xx) that reach the frontend
    if (resp.status >= 500 && json.error) {
        logClient('frontend', 'error', `API error: ${options.method || 'GET'} ${path} → ${resp.status}: ${json.error}`,
            { path, method: options.method || 'GET', status: resp.status });
    }
    return json;
}

// ─────────────────────────── Toast ───────────────────────────

const TOAST_ICONS = {
    success: 'fa-check-circle',
    error: 'fa-exclamation-circle',
    warning: 'fa-exclamation-triangle',
    info: 'fa-info-circle',
};

function _dismissToastElement(el) {
    if (!el || el.classList.contains('removing')) return;
    el.classList.add('removing');
    setTimeout(() => el.remove(), 300);
}

function toast(message, type = 'info') {
    const iconClass = TOAST_ICONS[type] || TOAST_ICONS.info;
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<i class="fas ${iconClass}"></i><span>${message}</span>`;
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => _dismissToastElement(el), 3500);
    // Log errors/warnings to Event Log (only when logged in; 5xx already logged by backend)
    if ((type === 'error' || type === 'warning') && NAS.token && typeof logClient === 'function') {
        logClient('frontend', type === 'error' ? 'error' : 'warning', message, { source: 'toast' });
    }
}

function toastWithAction(message, type = 'info', actionLabel, actionFn) {
    const iconClass = TOAST_ICONS[type] || TOAST_ICONS.info;
    const el = document.createElement('div');
    el.className = `toast ${type} toast-action`;
    el.innerHTML = `<i class="fas ${iconClass}"></i><span>${message}</span>`;
    if (actionLabel) {
        const btn = document.createElement('button');
        btn.className = 'toast-action-btn';
        btn.type = 'button';
        btn.textContent = actionLabel;
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (typeof actionFn === 'function') {
                try { actionFn(); } catch (err) { console.error(err); }
            }
            _dismissToastElement(el);
        });
        el.appendChild(btn);
    }
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => _dismissToastElement(el), 3500);
}

// Expose toast under all aliases used across apps
NAS.toast = toast;
window.showToast = toast;
window.showNotification = toast;

// ───────────────── Client-side Error Logging ─────────────────
// Sends errors to /api/eventlog/client for remote diagnostics.
// Rate-limited to 20 events/min client-side; server enforces 30/min/IP.

const _logQueue = [];
let _logFlushTimer = null;
let _logCount = 0;
let _logWindowStart = Date.now();
const _LOG_CLIENT_LIMIT = 20;
const _LOG_CLIENT_WINDOW = 60000;
const _LOG_FLUSH_DELAY = 500;   // batch events within 500ms

/**
 * Log a frontend event to the server-side Event Log.
 * @param {string} app - App identifier (e.g. 'radio-music', 'file-manager')
 * @param {string} level - 'debug' | 'info' | 'warning' | 'error'
 * @param {string} message - Short description
 * @param {object} [details] - Additional context
 */
function logClient(app, level, message, details) {
    // Also log to console for dev
    const consoleFn = level === 'error' ? console.error : level === 'warning' ? console.warn : console.log;
    consoleFn(`[${app}]`, message, details || '');

    // Client-side rate limit
    const now = Date.now();
    if (now - _logWindowStart > _LOG_CLIENT_WINDOW) {
        _logCount = 0;
        _logWindowStart = now;
    }
    if (++_logCount > _LOG_CLIENT_LIMIT) return;

    _logQueue.push({ app, level, message, details });
    if (!_logFlushTimer) {
        _logFlushTimer = setTimeout(_flushLogQueue, _LOG_FLUSH_DELAY);
    }
}

function _flushLogQueue() {
    _logFlushTimer = null;
    if (!NAS.token) return;
    while (_logQueue.length) {
        const ev = _logQueue.shift();
        fetch('/api/eventlog/client', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${NAS.token}`,
                ...(NAS.csrfToken ? { 'X-CSRFToken': NAS.csrfToken } : {}),
            },
            body: JSON.stringify(ev),
        }).catch(() => {});
    }
}

NAS.logClient = logClient;

// ── Global unhandled error / rejection handlers ──
window.addEventListener('error', (event) => {
    const file = event.filename ? event.filename.split('/').pop() : '?';
    logClient('global', 'error', `${event.message} (${file}:${event.lineno})`, {
        file: event.filename, line: event.lineno, col: event.colno,
    });
});
window.addEventListener('unhandledrejection', (event) => {
    const msg = event.reason?.message || event.reason?.toString?.() || 'Unhandled promise rejection';
    const stack = event.reason?.stack?.split('\n').slice(0, 3).join(' | ') || '';
    logClient('global', 'error', msg, { stack });
});

// ───────────────────── Loading States ─────────────────────
function showLoading(container, message) {
    if (typeof container === 'string') container = document.querySelector(container);
    if (!container) return;
    const el = document.createElement('div');
    el.className = 'ethos-loading';
    el.innerHTML = '<div class="ethos-spinner"></div>' + (message ? '<span>' + message + '</span>' : '');
    container.innerHTML = '';
    container.appendChild(el);
    return el;
}

function hideLoading(container) {
    if (typeof container === 'string') container = document.querySelector(container);
    if (!container) return;
    const el = container.querySelector('.ethos-loading');
    if (el) el.remove();
}

function formatDate(dateStr, opts) {
    if (!dateStr) return '—';
    try {
        const d = new Date(dateStr);
        if (isNaN(d)) return dateStr;
        const o = opts || { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' };
        return d.toLocaleString(document.documentElement.lang || 'pl-PL', o);
    } catch { return dateStr; }
}

function formatRelativeTime(dateStr) {
    if (!dateStr) return '—';
    try {
        const d = new Date(dateStr);
        if (isNaN(d)) return dateStr;
        const diff = (Date.now() - d.getTime()) / 1000;
        if (diff < 60) return t('przed chwilą');
        if (diff < 3600) return Math.floor(diff / 60) + ' min ' + t('temu');
        if (diff < 86400) return Math.floor(diff / 3600) + 'h ' + t('temu');
        if (diff < 604800) return Math.floor(diff / 86400) + 'd ' + t('temu');
        return formatDate(dateStr, { day: '2-digit', month: '2-digit', year: 'numeric' });
    } catch { return dateStr; }
}

// ───────────────────── Global Task Progress Stack ─────────────────────

function _fmtEta(seconds) {
    if (!seconds || !isFinite(seconds) || seconds <= 0) return '';
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${sec}s`;
    return `${sec}s`;
}

function _ensureGlobalTaskPanel() {
    const panel = document.getElementById('notif-panel');
    if (!panel) return null;
    let stack = document.getElementById('global-task-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'global-task-stack';
        stack.className = 'global-task-stack hidden';
        const list = document.getElementById('notif-list');
        if (list) panel.insertBefore(stack, list);
    }
    return stack;
}

function _updateNotifBadge() {
    const badge = document.getElementById('notif-badge');
    if (!badge) return;
    const activeTasks = NAS.taskProgress ? NAS.taskProgress.getActiveCount() : 0;
    const notifCount = NAS._notifCount || 0;
    const total = notifCount + activeTasks;
    if (total > 0) {
        badge.textContent = total;
        badge.classList.remove('hidden');
    } else {
        badge.classList.add('hidden');
    }
}

function _runTaskAction(action) {
    if (!action || !action.app) return;
    const appDef = NAS.apps?.find(a => a.id === action.app);
    if (!appDef) return;

    // Close notifications panel before navigation.
    notifPanelOpen = false;
    document.getElementById('notif-panel')?.classList.add('hidden');
    document.getElementById('notifications-btn')?.classList.remove('active');

    openApp(appDef, action.launchOpts || undefined);

    if (action.tab) {
        setTimeout(() => {
            const tabBtn = document.querySelector(`#win-body-${action.app} [data-tab="${action.tab}"]`);
            if (tabBtn) tabBtn.click();
        }, 260);
    }
}

NAS.taskProgress = {
    tasks: new Map(),

    getActiveCount() {
        let c = 0;
        this.tasks.forEach(t => {
            if (t.status === 'running') c += 1;
        });
        return c;
    },

    upsert(task) {
        if (!task || !task.id) return;
        const now = Date.now();
        const prev = this.tasks.get(task.id) || {};
        const next = {
            id: task.id,
            source: task.source || prev.source || t('Zadanie'),
            title: task.title || prev.title || t('Operacja'),
            status: task.status || prev.status || 'running',
            percent: typeof task.percent === 'number' ? Math.max(0, Math.min(100, task.percent)) : (typeof prev.percent === 'number' ? prev.percent : null),
            message: task.message || prev.message || '',
            etaSeconds: typeof task.etaSeconds === 'number' ? task.etaSeconds : (typeof prev.etaSeconds === 'number' ? prev.etaSeconds : null),
            action: task.action || prev.action || null,
            startedAt: prev.startedAt || now,
            updatedAt: now,
        };

        // Estimate ETA from progress if not explicitly provided.
        if (next.status === 'running' && next.percent && !next.etaSeconds) {
            const elapsed = (now - next.startedAt) / 1000;
            const total = elapsed / (next.percent / 100);
            const eta = Math.max(0, total - elapsed);
            if (isFinite(eta) && eta > 0 && eta < 24 * 3600) next.etaSeconds = eta;
        }

        this.tasks.set(task.id, next);
        this.render();
        _updateNotifBadge();
    },

    finish(id, success, message) {
        const cur = this.tasks.get(id);
        if (!cur) return;
        cur.status = success ? 'done' : 'error';
        cur.percent = success ? 100 : (typeof cur.percent === 'number' ? cur.percent : null);
        cur.message = message || cur.message || (success ? t('Zakończono') : t('Błąd'));
        cur.updatedAt = Date.now();
        cur.etaSeconds = null;
        this.tasks.set(id, cur);
        this.render();
        _updateNotifBadge();
        setTimeout(() => {
            const x = this.tasks.get(id);
            if (x && x.status !== 'running') {
                this.tasks.delete(id);
                this.render();
                _updateNotifBadge();
            }
        }, success ? 6000 : 10000);
    },

    render() {
        const stack = _ensureGlobalTaskPanel();
        if (!stack) return;

        const items = [...this.tasks.values()]
            .sort((a, b) => {
                if (a.status === 'running' && b.status !== 'running') return -1;
                if (a.status !== 'running' && b.status === 'running') return 1;
                return (b.updatedAt || 0) - (a.updatedAt || 0);
            })
            .slice(0, 8);

        if (!items.length) {
            stack.classList.add('hidden');
            stack.innerHTML = '';
            return;
        }

        stack.classList.remove('hidden');
        stack.innerHTML = `
            <div class="gts-header">
                <span><i class="fas fa-layer-group"></i> ${t('Aktywne zadania')}</span>
                <span class="gts-count">${items.filter(x => x.status === 'running').length}</span>
            </div>
            <div class="gts-list">
                ${items.map(it => {
                    const icon = it.status === 'running' ? 'fa-spinner fa-spin' : (it.status === 'done' ? 'fa-check-circle' : 'fa-exclamation-triangle');
                    const eta = _fmtEta(it.etaSeconds);
                    const pctText = typeof it.percent === 'number' ? `${Math.round(it.percent)}%` : '…';
                    const pct = typeof it.percent === 'number' ? Math.max(0, Math.min(100, it.percent)) : 8;
                    const clickable = it.action && it.action.app ? ' clickable' : '';
                    return `
                        <div class="gts-item ${it.status}${clickable}" data-task-id="${it.id}">
                            <div class="gts-row">
                                <span class="gts-title"><i class="fas ${icon}"></i> ${it.title}</span>
                                <span class="gts-meta">${pctText}${eta ? ` · ETA ${eta}` : ''}</span>
                            </div>
                            <div class="gts-source">${it.source}${it.message ? ` · ${it.message}` : ''}</div>
                            <div class="gts-track"><div class="gts-fill" style="width:${pct}%"></div></div>
                        </div>
                    `;
                }).join('')}
            </div>
        `;

        stack.querySelectorAll('.gts-item.clickable').forEach(el => {
            el.addEventListener('click', () => {
                const id = el.dataset.taskId;
                if (!id) return;
                const task = this.tasks.get(id);
                if (task?.action) _runTaskAction(task.action);
            });
        });
    }
};

// ─────────────────────────── Auth ───────────────────────────

// ─────────────── Global Directory Picker ────────────────────
/**
 * Opens a modal directory browser. Used across Download Manager, Editor,
 * Backup, Sharing, etc. for consistent path selection UX.
 * @param {string} startPath   Initial directory to show (default '/home')
 * @param {string} title       Modal title (default 'Wybierz folder')
 * @param {function} onSelect  Callback receiving the chosen path
 */
function openDirPicker(startPath, title, onSelect) {
    let browsePath = startPath || '/home';
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" style="width:480px;">
            <div class="modal-header"><span>${title || t('Wybierz folder')}</span><button class="modal-close"><i class="fas fa-times"></i></button></div>
            <div class="modal-body" style="padding:0;">
                <div class="ux-dir-toolbar">
                    <button class="dlm-btn-sm" id="gdp-dir-up"><i class="fas fa-arrow-up"></i></button>
                    <span id="gdp-dir-path" class="ux-dir-path"></span>
                </div>
                <div id="gdp-dir-list" class="ux-dir-list"></div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" id="gdp-dir-cancel">${t('Anuluj')}</button>
                <button class="btn btn-primary" id="gdp-dir-select">${t('Wybierz')}</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);

    const pathEl = overlay.querySelector('#gdp-dir-path');
    const listEl = overlay.querySelector('#gdp-dir-list');
    const close = () => overlay.remove();

    overlay.querySelector('.modal-close').addEventListener('click', close);
    overlay.querySelector('#gdp-dir-cancel').addEventListener('click', close);
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    overlay.querySelector('#gdp-dir-up').addEventListener('click', () => {
        loadDir(browsePath.substring(0, browsePath.lastIndexOf('/')) || '/');
    });
    overlay.querySelector('#gdp-dir-select').addEventListener('click', () => {
        onSelect(browsePath);
        close();
    });

    async function loadDir(path) {
        browsePath = path;
        pathEl.textContent = path;
        listEl.innerHTML = '<div class="ux-placeholder"><i class="fas fa-spinner fa-spin"></i></div>';
        const data = await api(`/files/list?path=${encodeURIComponent(path)}`);
        if (!data || data.error || data.locked) {
            listEl.innerHTML = `<div class="ux-placeholder">${t('Brak dostępu')}</div>`;
            return;
        }
        const dirs = (data.items || []).filter(i => i.is_dir).sort((a, b) => a.name.localeCompare(b.name));
        listEl.innerHTML = '';
        if (!dirs.length) {
            listEl.innerHTML = `<div class="ux-placeholder">${t('Brak podfolderów')}</div>`;
        }
        dirs.forEach(d => {
            const row = document.createElement('div');
            row.className = 'dte-browse-item';
            row.innerHTML = `<i class="fas fa-folder ux-folder-icon"></i> ${d.name}`;
            row.addEventListener('click', () => loadDir(path + (path === '/' ? '' : '/') + d.name));
            listEl.appendChild(row);
        });
    }
    loadDir(browsePath);
}

// ─────────────────────────── Auth (cont.) ───────────────────

function showLogin(reason) {
    // Log forced logout to Event Log (skip manual logout and initial page load)
    if (reason) {
        const detail = { reason, user: NAS.user?.username || '?', time: new Date().toISOString() };
        if (typeof NAS !== 'undefined' && NAS.logClient) {
            NAS.logClient('security', 'warning', `Forced logout: ${reason}`, detail);
        }
        console.warn('[auth] Forced logout:', reason, detail);
    }
    NAS.token = null;
    NAS.user = null;
    NAS.sudoMode = false;
    desktopInitialized = false;
    localStorage.removeItem('nas_token');
    document.getElementById('login-screen').classList.remove('hidden', 'fade-out');
    document.getElementById('desktop').classList.add('hidden');
    const uEl = document.getElementById('login-username');
    if (uEl) uEl.value = '';
    document.getElementById('login-password').value = '';
    document.getElementById('login-error').textContent = '';
    document.getElementById('login-password').focus();
}

function showDesktop() {
    const loginEl = document.getElementById('login-screen');
    loginEl.classList.add('fade-out');
    setTimeout(() => {
        loginEl.classList.add('hidden');
        document.getElementById('desktop').classList.remove('hidden');
        initDesktop();
    }, 600);
}

async function tryAutoLogin() {
    const saved = localStorage.getItem('nas_token');
    if (!saved) return;
    NAS.token = saved;
    try {
        const data = await api('/auth/verify');
        if (data.valid) {
            NAS.nasName = data.nas_name || 'EthOS';
            NAS.user = data.user || { username: 'admin', role: 'admin' };
            NAS.sudoMode = NAS.user.role === 'admin';
            NAS.csrfToken = data.csrf_token; // Store CSRF token
            if (data.password_change_required) {
                showPasswordChangeModal();
            } else {
                showDesktop();
            }
        } else {
            showLogin('Token verification failed (token expired or revoked)');
        }
    } catch (e) {
        showLogin(`Server unreachable during auto-login: ${e.message || 'network error'}`);
    }
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = (document.getElementById('login-username')?.value || '').trim();
    const pw = document.getElementById('login-password').value;
    const errEl = document.getElementById('login-error');
    if (!pw) { errEl.textContent = t('Podaj hasło'); return; }
    try {
        const data = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password: pw })
        }).then(r => r.json());

        if (data.token) {
            NAS.token = data.token;
            NAS.nasName = data.nas_name || 'EthOS';
            NAS.user = data.user || { username: username || 'admin', role: 'admin' };
            NAS.sudoMode = NAS.user.role === 'admin';
            NAS.csrfToken = data.csrf_token; // Store CSRF token
            localStorage.setItem('nas_token', data.token);
            errEl.textContent = '';
            if (data.password_change_required) {
                showPasswordChangeModal();
            } else {
                showDesktop();
            }
        } else {
            errEl.textContent = data.error || t('Błąd logowania');
            document.getElementById('login-password').classList.add('shake');
            setTimeout(() => document.getElementById('login-password').classList.remove('shake'), 500);
        }
    } catch {
        errEl.textContent = t('Błąd połączenia z serwerem');
    }
});


// ─────────────────────────── Clock ───────────────────────────

function updateClocks() {
    const now = new Date();
    const time = now.toLocaleTimeString(getLocale(), { hour: '2-digit', minute: '2-digit' });
    const date = now.toLocaleDateString(getLocale(), { weekday: 'short', day: 'numeric', month: 'short' });
    const full = now.toLocaleString(getLocale(), { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit' });

    const taskbarClock = document.getElementById('taskbar-clock');
    if (taskbarClock) taskbarClock.textContent = `${date}  ${time}`;

    const loginTime = document.getElementById('login-time');
    if (loginTime) loginTime.textContent = full;
}
let _clockInterval = setInterval(updateClocks, 1000);
updateClocks();


// ─────────────────────────── Format Helpers ───────────────────────────

function formatBytes(bytes, decimals = 1) {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return (bytes / Math.pow(k, i)).toFixed(decimals) + ' ' + sizes[i];
}

function formatSpeed(bytesPerSec) {
    if (bytesPerSec < 1024) return Math.round(bytesPerSec) + ' B/s';
    if (bytesPerSec < 1024 * 1024) return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
    return (bytesPerSec / (1024 * 1024)).toFixed(1) + ' MB/s';
}

function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

function formatDate(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString(getLocale(), {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
    });
}


// ═══════════════════════════════════════════════════════════
//  WINDOW MANAGER
// ═══════════════════════════════════════════════════════════

const WM = {
    windows: new Map(),
    zIndex: 100,
    activeId: null,
    dragState: null,
    resizeState: null,
};

function createWindow(id, opts = {}) {
    // Defaults
    const defaults = {
        title: t('Okno'),
        icon: 'fa-window-maximize',
        iconColor: '#3b82f6',
        width: 900,
        height: 600,
        minWidth: 400,
        minHeight: 250,
        content: '',        // HTML string
        onRender: null,     // function(bodyEl)
        onClose: null,
        singleton: true,
    };
    const o = { ...defaults, ...opts };

    // Singleton — if already open, focus it
    if (o.singleton && WM.windows.has(id)) {
        focusWindow(id);
        const w = WM.windows.get(id);
        if (w.minimized) restoreWindow(id);
        return w;
    }

    // Position: cascade from center
    const area = document.getElementById('desktop-area');
    const areaW = area.clientWidth, areaH = area.clientHeight;
    const cascade = WM.windows.size * 30;
    const left = Math.max(40, Math.min((areaW - o.width) / 2 + cascade, areaW - o.width - 40));
    const top = Math.max(20, Math.min((areaH - o.height) / 2 + cascade, areaH - o.height - 40));

    const winEl = document.createElement('div');
    winEl.className = 'window opening';
    winEl.id = `win-${id}`;
    winEl.style.cssText = `left:${left}px;top:${top}px;width:${o.width}px;height:${o.height}px;z-index:${++WM.zIndex}`;

    winEl.innerHTML = `
        <div class="window-header" data-winid="${id}">
            <div class="window-title">
                <i class="fas ${o.icon}" style="color:${o.iconColor}"></i>
                <span>${o.title}</span>
            </div>
            <div class="window-controls">
                <button class="win-ctrl minimize" data-action="minimize"><i class="fas fa-minus"></i></button>
                <button class="win-ctrl maximize" data-action="maximize"><i class="fas fa-expand"></i></button>
                <button class="win-ctrl close" data-action="close"><i class="fas fa-times"></i></button>
            </div>
        </div>
        <div class="window-body" id="win-body-${id}">${o.content}</div>
        <div class="win-resize n" data-dir="n"></div>
        <div class="win-resize s" data-dir="s"></div>
        <div class="win-resize e" data-dir="e"></div>
        <div class="win-resize w" data-dir="w"></div>
        <div class="win-resize ne" data-dir="ne"></div>
        <div class="win-resize nw" data-dir="nw"></div>
        <div class="win-resize se" data-dir="se"></div>
        <div class="win-resize sw" data-dir="sw"></div>
    `;

    document.getElementById('window-layer').appendChild(winEl);

    setTimeout(() => winEl.classList.remove('opening'), 300);

    const winData = {
        id,
        el: winEl,
        body: document.getElementById(`win-body-${id}`),
        opts: o,
        minimized: false,
        maximized: false,
        prevBounds: null,
    };
    WM.windows.set(id, winData);

    // Event: header controls
    winEl.querySelector('.window-controls').addEventListener('click', e => {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const action = btn.dataset.action;
        if (action === 'close') closeWindow(id);
        else if (action === 'minimize') minimizeWindow(id);
        else if (action === 'maximize') toggleMaximize(id);
    });

    // Double-click header → maximize
    winEl.querySelector('.window-header').addEventListener('dblclick', (e) => {
        if (e.target.closest('.window-controls')) return;
        toggleMaximize(id);
    });

    // Click → focus
    winEl.addEventListener('mousedown', () => focusWindow(id));

    // Drag
    winEl.querySelector('.window-header').addEventListener('mousedown', (e) => {
        if (e.target.closest('.window-controls')) return;
        if (e.button !== 0) return;
        const w = WM.windows.get(id);
        if (w.maximized) return;
        e.preventDefault();
        WM.dragState = {
            id,
            startX: e.clientX,
            startY: e.clientY,
            origLeft: winEl.offsetLeft,
            origTop: winEl.offsetTop,
        };
    });

    // Resize
    winEl.querySelectorAll('.win-resize').forEach(handle => {
        handle.addEventListener('mousedown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            focusWindow(id);
            WM.resizeState = {
                id,
                dir: handle.dataset.dir,
                startX: e.clientX,
                startY: e.clientY,
                origLeft: winEl.offsetLeft,
                origTop: winEl.offsetTop,
                origW: winEl.offsetWidth,
                origH: winEl.offsetHeight,
                minW: o.minWidth,
                minH: o.minHeight,
            };
        });
    });

    focusWindow(id);
    updateTaskbarWindows();

    // Render callback
    if (o.onRender) {
        o.onRender(document.getElementById(`win-body-${id}`));
    }

    return winData;
}

function closeWindow(id) {
    const w = WM.windows.get(id);
    if (!w) return;
    // Side-panel apps – use their own close logic
    if (w.el.id === 'sn-panel') {
        if (typeof _stickyClose === 'function') _stickyClose(w.el, id);
        return;
    }
    w.el.classList.add('closing');
    if (w.opts.onClose) w.opts.onClose();
    setTimeout(() => {
        w.el.remove();
        WM.windows.delete(id);
        updateTaskbarWindows();
        // Focus next window
        if (WM.activeId === id) {
            WM.activeId = null;
            let topZ = 0, topId = null;
            WM.windows.forEach((ww, wid) => {
                if (!ww.minimized && ww.el.style.zIndex > topZ) {
                    topZ = parseInt(ww.el.style.zIndex);
                    topId = wid;
                }
            });
            if (topId) focusWindow(topId);
        }
    }, 200);
}

function minimizeWindow(id) {
    const w = WM.windows.get(id);
    if (!w) return;
    w.el.classList.add('minimizing');
    setTimeout(() => {
        w.el.style.display = 'none';
        w.el.classList.remove('minimizing');
        w.minimized = true;
        updateTaskbarWindows();
    }, 300);
}

function restoreWindow(id) {
    const w = WM.windows.get(id);
    if (!w) return;
    w.el.style.display = '';
    w.minimized = false;
    focusWindow(id);
    updateTaskbarWindows();
}

function toggleMaximize(id) {
    const w = WM.windows.get(id);
    if (!w) return;

    if (w.maximized) {
        // Restore
        if (w.prevBounds) {
            w.el.style.left = w.prevBounds.left + 'px';
            w.el.style.top = w.prevBounds.top + 'px';
            w.el.style.width = w.prevBounds.width + 'px';
            w.el.style.height = w.prevBounds.height + 'px';
        }
        w.el.classList.remove('maximized');
        w.maximized = false;
        w.el.querySelector('.maximize i').className = 'fas fa-expand';
    } else {
        // Maximize
        w.prevBounds = {
            left: w.el.offsetLeft,
            top: w.el.offsetTop,
            width: w.el.offsetWidth,
            height: w.el.offsetHeight,
        };
        w.el.style.left = '0';
        w.el.style.top = '0';
        const area = document.getElementById('desktop-area');
        w.el.style.width = area.clientWidth + 'px';
        w.el.style.height = area.clientHeight + 'px';
        w.el.classList.add('maximized');
        w.maximized = true;
        w.el.querySelector('.maximize i').className = 'fas fa-compress';
    }
}

function focusWindow(id) {
    if (WM.activeId === id) return;
    WM.windows.forEach((w) => w.el.classList.remove('focused'));
    const w = WM.windows.get(id);
    if (!w) return;
    w.el.style.zIndex = ++WM.zIndex;
    w.el.classList.add('focused');
    WM.activeId = id;
    updateTaskbarWindows();
}

// Global mouse handlers for drag & resize
document.addEventListener('mousemove', (e) => {
    // Drag
    if (WM.dragState) {
        const s = WM.dragState;
        const w = WM.windows.get(s.id);
        if (!w) return;
        w.el.style.left = (s.origLeft + e.clientX - s.startX) + 'px';
        w.el.style.top = Math.max(0, s.origTop + e.clientY - s.startY) + 'px';
    }
    // Resize
    if (WM.resizeState) {
        const s = WM.resizeState;
        const w = WM.windows.get(s.id);
        if (!w) return;
        const dx = e.clientX - s.startX;
        const dy = e.clientY - s.startY;
        const dir = s.dir;

        let newW = s.origW, newH = s.origH, newL = s.origLeft, newT = s.origTop;

        if (dir.includes('e')) newW = Math.max(s.minW, s.origW + dx);
        if (dir.includes('s')) newH = Math.max(s.minH, s.origH + dy);
        if (dir.includes('w')) {
            newW = Math.max(s.minW, s.origW - dx);
            if (newW > s.minW) newL = s.origLeft + dx;
        }
        if (dir.includes('n')) {
            newH = Math.max(s.minH, s.origH - dy);
            if (newH > s.minH) newT = s.origTop + dy;
        }

        w.el.style.width = newW + 'px';
        w.el.style.height = newH + 'px';
        w.el.style.left = newL + 'px';
        w.el.style.top = Math.max(0, newT) + 'px';
    }
});

document.addEventListener('mouseup', () => {
    WM.dragState = null;
    WM.resizeState = null;
});


// ─────────────────────────── Taskbar Windows ───────────────────────────

function updateTaskbarWindows() {
    const container = document.getElementById('taskbar-windows');
    container.innerHTML = '';
    WM.windows.forEach((w, id) => {
        const btn = document.createElement('button');
        btn.className = `taskbar-win-btn${WM.activeId === id && !w.minimized ? ' active' : ''}${w.minimized ? ' minimized' : ''}`;
        btn.innerHTML = `<i class="fas ${w.opts.icon}"></i><span>${w.opts.title}</span>`;
        btn.addEventListener('click', () => {
            // Side-panel apps (like sticky notes) – toggle close
            if (w.el.id === 'sn-panel') {
                if (typeof _stickyClose === 'function') _stickyClose(w.el, id);
                return;
            }
            if (w.minimized) {
                restoreWindow(id);
            } else if (WM.activeId === id) {
                minimizeWindow(id);
            } else {
                focusWindow(id);
            }
        });
        container.appendChild(btn);
    });
}


// ─────────────────────────── Main Menu ───────────────────────────

let menuOpen = false;

function toggleMainMenu() {
    const menu = document.getElementById('main-menu');
    const btn = document.getElementById('main-menu-btn');
    menuOpen = !menuOpen;
    menu.classList.toggle('hidden', !menuOpen);
    btn.classList.toggle('active', menuOpen);
    if (menuOpen) {
        document.getElementById('mm-search').value = '';
        document.getElementById('mm-search').focus();
        renderMenuGrid();
    }
}

function closeMainMenu() {
    menuOpen = false;
    document.getElementById('main-menu').classList.add('hidden');
    document.getElementById('main-menu-btn').classList.remove('active');
}

document.getElementById('main-menu-btn').addEventListener('click', toggleMainMenu);
document.querySelector('.main-menu-backdrop')?.addEventListener('click', closeMainMenu);

document.getElementById('mm-search').addEventListener('input', (e) => {
    renderMenuGrid(e.target.value.toLowerCase());
});

function renderMenuGrid(filter = '') {
    const grid = document.getElementById('mm-grid');
    grid.innerHTML = '';

    // Group by category
    const categories = {};
    NAS.apps.forEach(app => {
        if (filter && !t(app.name).toLowerCase().includes(filter) && !t(app.description || '').toLowerCase().includes(filter)) return;
        const cat = app.category || 'Inne';
        if (!categories[cat]) categories[cat] = [];
        categories[cat].push(app);
    });

    for (const [cat, apps] of Object.entries(categories)) {
        const catEl = document.createElement('div');
        catEl.className = 'mm-category';
        catEl.textContent = t(cat);
        grid.appendChild(catEl);

        apps.forEach(app => {
            const el = document.createElement('button');
            el.className = 'mm-app';
            const desc = app.domain ? `<div class="mm-app-domain">${app.domain}</div>` : '';
            el.innerHTML = `
                <div class="mm-app-icon" style="background:${app.color || '#3b82f6'}">
                    <i class="fas ${app.icon}"></i>
                </div>
                <div class="mm-app-name">${t(app.name)}</div>
                ${desc}
            `;
            el.addEventListener('click', () => {
                closeMainMenu();
                openApp(app);
            });
            grid.appendChild(el);
        });
    }
}


// ─────────────────────────── Desktop Icons ───────────────────────────

function renderDesktopIcons() {
    const container = document.getElementById('desktop-icons');
    container.innerHTML = '';

    let desktopApps;
    if (NAS.desktopAppIds && NAS.desktopAppIds.length) {
        // Ordered by saved preference
        desktopApps = NAS.desktopAppIds
            .map(id => NAS.apps.find(a => a.id === id))
            .filter(Boolean);
    } else {
        // Default: essential apps for a fresh system
        const defaultIds = ['dashboard', 'file-manager', 'storage-manager', 'network', 'terminal', 'backup'];
        const defaults = defaultIds.map(id => NAS.apps.find(a => a.id === id)).filter(Boolean);
        desktopApps = defaults.length ? defaults : NAS.apps.slice(0, 6);
    }

    desktopApps.forEach(app => {
        const el = document.createElement('div');
        el.className = 'desktop-icon';
        el.innerHTML = `
            <div class="desktop-icon-img" style="background:${app.color || '#3b82f6'}">
                <i class="fas ${app.icon}"></i>
            </div>
            <div class="desktop-icon-label">${t(app.name)}</div>
        `;
        el.addEventListener('dblclick', () => openApp(app));
        el.addEventListener('click', () => {
            container.querySelectorAll('.desktop-icon').forEach(i => i.classList.remove('selected'));
            el.classList.add('selected');
        });
        container.appendChild(el);
    });
}


/* ── Desktop right-click context menu ── */
(function initDesktopContextMenu() {
    const desktop = document.getElementById('desktop');
    if (!desktop) return;

    desktop.addEventListener('contextmenu', e => {
        // Only on empty desktop area (not on icons or windows)
        if (e.target.closest('.desktop-icon') || e.target.closest('.window') || e.target.closest('#taskbar')) return;
        e.preventDefault();

        // Remove any existing context menu
        document.querySelectorAll('.desktop-ctx-menu').forEach(m => m.remove());

        const menu = document.createElement('div');
        menu.className = 'desktop-ctx-menu';
        menu.innerHTML = `
            <button class="desktop-ctx-item" data-action="configure-icons">
                <i class="fas fa-th"></i> ${t('Konfiguruj ikony pulpitu')}
            </button>
            <button class="desktop-ctx-item" data-action="refresh-desktop">
                <i class="fas fa-sync-alt"></i> ${t('Odśwież')}
            </button>
        `;
        menu.style.left = e.clientX + 'px';
        menu.style.top = e.clientY + 'px';
        document.body.appendChild(menu);

        // Position adjustment if overflows
        const rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) menu.style.left = (e.clientX - rect.width) + 'px';
        if (rect.bottom > window.innerHeight) menu.style.top = (e.clientY - rect.height) + 'px';

        const close = () => menu.remove();
        setTimeout(() => document.addEventListener('click', close, { once: true }), 10);

        menu.querySelector('[data-action="configure-icons"]').addEventListener('click', () => {
            close();
            showDesktopIconsConfig();
        });
        menu.querySelector('[data-action="refresh-desktop"]').addEventListener('click', () => {
            close();
            renderDesktopIcons();
        });
    });
})();


/* ── Desktop icons configuration dialog ── */
async function showDesktopIconsConfig() {
    const allApps = NAS.apps || [];
    const currentIds = NAS.desktopAppIds && NAS.desktopAppIds.length
        ? [...NAS.desktopAppIds]
        : allApps.slice(0, 8).map(a => a.id);

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';

    const modal = document.createElement('div');
    modal.className = 'di-config-modal';
    modal.innerHTML = `
        <div class="di-config-header">
            <h3><i class="fas fa-th"></i> ${t('Ikony pulpitu')}</h3>
            <button class="di-config-close">&times;</button>
        </div>
        <div class="di-config-hint">${t('Wybierz aplikacje widoczne na pulpicie. Przeciągnij, aby zmienić kolejność.')}</div>
        <div class="di-config-list" id="di-config-list"></div>
        <div class="di-config-footer">
            <button class="di-config-btn di-btn-secondary" id="di-config-reset"><i class="fas fa-undo"></i> ${t('Domyślne')}</button>
            <button class="di-config-btn di-btn-primary" id="di-config-save"><i class="fas fa-check"></i> ${t('Zapisz')}</button>
        </div>
    `;
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    modal.querySelector('.di-config-close').addEventListener('click', () => overlay.remove());

    const listEl = modal.querySelector('#di-config-list');

    // Build a working ordered list: selected first (in order), then unselected
    let ordered = [];
    currentIds.forEach(id => {
        const app = allApps.find(a => a.id === id);
        if (app) ordered.push({ ...app, checked: true });
    });
    allApps.forEach(app => {
        if (!ordered.find(o => o.id === app.id)) {
            ordered.push({ ...app, checked: false });
        }
    });

    function renderList() {
        listEl.innerHTML = '';
        ordered.forEach((item, idx) => {
            const row = document.createElement('div');
            row.className = 'di-config-row' + (item.checked ? ' di-row-active' : '');
            row.draggable = true;
            row.dataset.idx = idx;
            row.innerHTML = `
                <span class="di-drag-handle"><i class="fas fa-grip-vertical"></i></span>
                <label class="di-config-check">
                    <input type="checkbox" ${item.checked ? 'checked' : ''} data-idx="${idx}">
                    <span class="di-check-box"></span>
                </label>
                <div class="di-config-icon" style="background:${item.color || '#3b82f6'}">
                    <i class="fas ${item.icon}"></i>
                </div>
                <span class="di-config-name">${t(item.name)}</span>
            `;
            listEl.appendChild(row);
        });

        /* Checkbox toggles */
        listEl.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.addEventListener('change', () => {
                const i = parseInt(cb.dataset.idx);
                ordered[i].checked = cb.checked;
                const row = cb.closest('.di-config-row');
                if (row) row.classList.toggle('di-row-active', cb.checked);
            });
        });

        /* Drag-and-drop reorder */
        let dragIdx = null;
        listEl.querySelectorAll('.di-config-row').forEach(row => {
            row.addEventListener('dragstart', e => {
                dragIdx = parseInt(row.dataset.idx);
                row.classList.add('di-dragging');
                e.dataTransfer.effectAllowed = 'move';
            });
            row.addEventListener('dragend', () => {
                row.classList.remove('di-dragging');
                dragIdx = null;
            });
            row.addEventListener('dragover', e => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                row.classList.add('di-drag-over');
            });
            row.addEventListener('dragleave', () => row.classList.remove('di-drag-over'));
            row.addEventListener('drop', e => {
                e.preventDefault();
                row.classList.remove('di-drag-over');
                const dropIdx = parseInt(row.dataset.idx);
                if (dragIdx !== null && dragIdx !== dropIdx) {
                    const [moved] = ordered.splice(dragIdx, 1);
                    ordered.splice(dropIdx, 0, moved);
                    renderList();
                }
            });
        });
    }

    renderList();

    /* Reset to defaults */
    modal.querySelector('#di-config-reset').addEventListener('click', () => {
        const defaultIds = allApps.slice(0, 8).map(a => a.id);
        ordered = [];
        allApps.forEach((app, i) => {
            ordered.push({ ...app, checked: i < 8 });
        });
        renderList();
    });

    /* Save */
    modal.querySelector('#di-config-save').addEventListener('click', async () => {
        const selectedIds = ordered.filter(o => o.checked).map(o => o.id);
        try {
            await api('/desktop-apps', { method: 'PUT', body: { app_ids: selectedIds } });
            NAS.desktopAppIds = selectedIds;
            renderDesktopIcons();
            overlay.remove();
            toast(t('Ikony pulpitu zapisane'), 'success');
        } catch (e) {
            toast(t('Błąd zapisu:') + ' ' + e.message, 'error');
        }
    });
}


// ─────────────────────────── Open App ───────────────────────────

function openApp(app, launchOpts) {
    if (app.type === 'builtin') {
        openBuiltinApp(app, launchOpts);
    } else if (app.type === 'iframe') {
        openIframeApp(app);
    } else if (app.type === 'external') {
        openExternalApp(app);
    }
}

function openIframeApp(app) {
    createWindow(app.id, {
        title: t(app.name),
        icon: app.icon,
        iconColor: app.color,
        width: 1100,
        height: 700,
        content: `<div class="iframe-container"><iframe src="${app.url}" sandbox="allow-same-origin allow-scripts allow-forms allow-popups"></iframe></div>`,
    });
}

function openExternalApp(app) {
    // External apps from NPM — open in new tab or as iframe window
    // Try iframe first, fall back to new tab for sites with X-Frame-Options
    createWindow(app.id, {
        title: t(app.name),
        icon: app.icon,
        iconColor: app.color,
        width: 1100,
        height: 700,
        content: `
            <div class="iframe-container" id="external-${app.id}">
                <iframe src="${app.url}"
                    referrerpolicy="no-referrer"
                    allow="fullscreen"
                    style="width:100%;height:100%;border:none;"
                    onerror="document.getElementById('external-${app.id}').innerHTML='<div style=\'padding:40px;text-align:center;color:var(--text-muted)\'><i class=\'fas fa-external-link-alt\' style=\'font-size:48px;margin-bottom:16px;\'></i><br>${t('Nie można załadować w ramce.')}<br><a href=${app.url} target=_blank style=\'color:var(--accent)\'>${t('Otwórz w nowej karcie')}</a></div>'"
                ></iframe>
            </div>
            <div style="position:absolute;bottom:0;left:0;right:0;padding:6px 16px;background:var(--bg-surface);border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;font-size:12px;">
                <span style="color:var(--text-muted)">
                    <i class="fas fa-globe" style="margin-right:4px"></i>${app.domain || app.url}
                </span>
                <a href="${app.url}" target="_blank" style="color:var(--accent);text-decoration:none;">
                    <i class="fas fa-external-link-alt"></i> ${t('Otwórz w nowej karcie')}
                </a>
            </div>
        `,
    });
}

function openBuiltinApp(app, launchOpts) {
    // Handled in apps.js via the global registry
    const registry = (typeof window !== 'undefined' && window.AppRegistry)
        ? window.AppRegistry
        : (typeof AppRegistry !== 'undefined' ? AppRegistry : null);
    if (registry && registry[app.id]) {
        registry[app.id](app, launchOpts);
    } else {
        toast(t('Aplikacja nie jest jeszcze dostępna') + `: ${t(app.name)}`, 'warning');
    }
}


// ─────────────────────────── Notifications ───────────────────────────

let notifPanelOpen = false;

document.getElementById('notifications-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    notifPanelOpen = !notifPanelOpen;
    document.getElementById('notif-panel').classList.toggle('hidden', !notifPanelOpen);
    document.getElementById('notifications-btn').classList.toggle('active', notifPanelOpen);
    // Close other panels
    closeUserMenu();
    closeUsbTray();
    if (notifPanelOpen) loadNotifications();
});

document.getElementById('notif-clear').addEventListener('click', async () => {
    try {
        await api('/notifications/clear', { method: 'POST' });
    } catch {}
    document.getElementById('notif-list').innerHTML = `<p class="notif-empty">${t('Brak powiadomień')}</p>`;
    document.getElementById('notif-badge').classList.add('hidden');
});

async function loadNotifications() {
    try {
        const data = await api('/notifications');
        NAS._notifCount = Array.isArray(data) ? data.length : 0;
        const list = document.getElementById('notif-list');
        const activeTasks = NAS.taskProgress.getActiveCount();
        if (!data.length) {
            list.innerHTML = `<p class="notif-empty">${t('Brak powiadomień')}</p>`;
            _updateNotifBadge();
            NAS.taskProgress.render();
            if (activeTasks > 0) list.innerHTML = '';
            return;
        }
        _updateNotifBadge();

        list.innerHTML = data.map(n => {
            const hasAction = n.action && n.action.app;
            const actionAttr = hasAction ? `data-action-app="${n.action.app}" data-action-tab="${n.action.tab || ''}"` : '';
            const clickClass = hasAction ? ' clickable' : '';
            const iconMap = { warning: 'fa-exclamation-triangle', info: 'fa-arrow-circle-up', error: 'fa-times-circle', success: 'fa-check-circle', progress: 'fa-spinner fa-spin' };
            const icon = iconMap[n.type] || 'fa-info-circle';
            let timeStr = '';
            if (n.time) {
                const ago = Math.round((Date.now() / 1000) - n.time);
                if (ago < 60) timeStr = t('teraz');
                else if (ago < 3600) timeStr = Math.round(ago / 60) + ' ' + t('min temu');
                else if (ago < 86400) timeStr = Math.round(ago / 3600) + 'h ' + t('temu');
                else timeStr = Math.round(ago / 86400) + 'd ' + t('temu');
            }
            return `
            <div class="notif-item${clickClass}" ${actionAttr}>
                <div class="notif-item-icon ${n.type}">
                    <i class="fas ${icon}"></i>
                </div>
                <div class="notif-item-content">
                    <div class="notif-item-title">${n.title}${timeStr ? '<span class="notif-time">' + timeStr + '</span>' : ''}</div>
                    <div class="notif-item-msg">${n.message}${hasAction ? ` <span class="notif-link">${t('Otwórz')} →</span>` : ''}</div>
                </div>
            </div>`;
        }).join('');

        // Handle clickable notifications
        list.querySelectorAll('.notif-item.clickable').forEach(el => {
            el.addEventListener('click', () => {
                const appId = el.dataset.actionApp;
                const tab = el.dataset.actionTab;
                // Close notif panel
                notifPanelOpen = false;
                document.getElementById('notif-panel').classList.add('hidden');
                document.getElementById('notifications-btn').classList.remove('active');
                // Open the app
                const appDef = NAS.apps.find(a => a.id === appId);
                if (appDef) {
                    openApp(appDef);
                    // Switch tab after a small delay for the window to render
                    if (tab) {
                        setTimeout(() => {
                            const tabBtn = document.querySelector(`#win-body-${appId} [data-tab="${tab}"]`);
                            if (tabBtn) tabBtn.click();
                        }, 300);
                    }
                }
            });
        });
        NAS.taskProgress.render();
    } catch {
        // ignore
    }
}


// ─────────────────────────── USB Tray ───────────────────────────

let usbTrayOpen = false;

function closeUsbTray() {
    usbTrayOpen = false;
    document.getElementById('usb-tray-panel').classList.add('hidden');
    document.getElementById('usb-tray-btn').classList.remove('active');
}

async function loadUsbTrayDrives() {
    try {
        // USB tray: admin-only (Synology default — non-admins don't see external devices)
        if (NAS.user?.role !== 'admin') {
            document.getElementById('usb-tray-btn').classList.add('hidden');
            return;
        }
        const data = await api('/resources/disks');
        const disks = data.disks || data || [];
        const usbDrives = disks.filter(d => d.is_usb && d.mountpoint);
        const btn = document.getElementById('usb-tray-btn');
        const badge = document.getElementById('usb-tray-badge');

        if (usbDrives.length === 0) {
            btn.classList.add('hidden');
            closeUsbTray();
            return;
        }
        btn.classList.remove('hidden');
        badge.textContent = usbDrives.length;
        badge.classList.remove('hidden');

        const list = document.getElementById('usb-tray-list');
        list.innerHTML = usbDrives.map(d => {
            const dev = (d.device || '').replace('/dev/', '').replace(/[0-9]+$/, '');
            const pct = d.percent || 0;
            const barColor = pct > 90 ? '#ef4444' : pct > 70 ? '#f59e0b' : 'var(--accent)';
            const totalNum = typeof d.total === 'number' ? d.total : parseInt(d.total) || 0;
            const usedNum = typeof d.used === 'number' ? d.used : parseInt(d.used) || 0;
            const usedStr = totalNum ? `${formatBytes(usedNum)} / ${formatBytes(totalNum)}` : '';
            return `<div class="usb-tray-item" data-dev="${dev}" data-mount="${d.mountpoint || ''}">
                <div class="usb-tray-icon"><i class="fas fa-hdd"></i></div>
                <div class="usb-tray-info">
                    <div class="usb-tray-name">${d.label || dev}</div>
                    <div class="usb-tray-sub">${usedStr}${usedStr && pct ? ` (${pct}%)` : ''}</div>
                    ${usedStr ? `<div class="usb-tray-bar"><div class="usb-tray-bar-fill" style="width:${pct}%;background:${barColor}"></div></div>` : ''}
                </div>
                <button class="usb-tray-browse" title="${t('Przeglądaj')}" data-action="browse"><i class="fas fa-folder-open"></i></button>
                <button class="usb-tray-eject" title="${t('Wysuń')}" data-action="eject"><i class="fas fa-eject"></i> ${t('Wysuń')}</button>
            </div>`;
        }).join('');

        list.querySelectorAll('[data-action]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const item = btn.closest('.usb-tray-item');
                const dev = item.dataset.dev;
                const mount = item.dataset.mount;
                if (btn.dataset.action === 'browse') {
                    closeUsbTray();
                    const fmApp = NAS.apps?.find(a => a.id === 'filemanager');
                    if (fmApp) openApp(fmApp, { path: mount });
                    else openApp('filemanager', { path: mount });
                } else if (btn.dataset.action === 'eject') {
                    btn.disabled = true;
                    btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Wysuwanie...')}`;
                    try {
                        const res = await api('/storage/eject', { method: 'POST', body: { disk: dev } });
                        if (res.success || res.ok) {
                            toast(`${t('Bezpiecznie wysunięto')} ${item.querySelector('.usb-tray-name').textContent}`, 'success');
                            setTimeout(() => loadUsbTrayDrives(), 1000);
                        } else {
                            toast(res.error || t('Błąd wysuwania'), 'error');
                            btn.disabled = false;
                            btn.innerHTML = `<i class="fas fa-eject"></i> ${t('Wysuń')}`;
                        }
                    } catch (err) {
                        toast(t('Błąd wysuwania: ') + (err.message || err), 'error');
                        btn.disabled = false;
                        btn.innerHTML = `<i class="fas fa-eject"></i> ${t('Wysuń')}`;
                    }
                }
            });
        });
    } catch {
        // ignore
    }
}

document.getElementById('usb-tray-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    usbTrayOpen = !usbTrayOpen;
    document.getElementById('usb-tray-panel').classList.toggle('hidden', !usbTrayOpen);
    document.getElementById('usb-tray-btn').classList.toggle('active', usbTrayOpen);
    // Close other panels
    closeUserMenu();
    notifPanelOpen = false;
    document.getElementById('notif-panel').classList.add('hidden');
    document.getElementById('notifications-btn').classList.remove('active');
    closePowerMenu();
    if (usbTrayOpen) loadUsbTrayDrives();
});


// ─────────────────────────── Power Menu ───────────────────────────

let powerMenuOpen = false;

function closePowerMenu() {
    powerMenuOpen = false;
    document.getElementById('power-menu').classList.add('hidden');
    document.getElementById('power-btn').classList.remove('active');
}

async function loadPowerUptime() {
    try {
        const data = await api('/power/status');
        const el = document.getElementById('pm-uptime');
        if (el && data.uptime) {
            el.innerHTML = `<i class="fas fa-clock"></i> Uptime: ${formatUptime(data.uptime)} &nbsp;|&nbsp; Load: ${data.load.join(', ')}`;
        }
    } catch {}
}

document.getElementById('power-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    powerMenuOpen = !powerMenuOpen;
    document.getElementById('power-menu').classList.toggle('hidden', !powerMenuOpen);
    document.getElementById('power-btn').classList.toggle('active', powerMenuOpen);
    // Close other panels
    closeUserMenu();
    closeUsbTray();
    notifPanelOpen = false;
    document.getElementById('notif-panel').classList.add('hidden');
    document.getElementById('notifications-btn').classList.remove('active');
    if (powerMenuOpen) loadPowerUptime();
});

document.querySelectorAll('.pm-action').forEach(btn => {
    btn.addEventListener('click', async () => {
        const action = btn.dataset.action;
        const labels = {
            'restart-app': t('Restart aplikacji (kontener nasos)'),
            'reboot': t('Restart całego systemu Linux'),
            'shutdown': t('Wyłączenie systemu Linux')
        };
        const icons = {
            'restart-app': 'fa-sync-alt',
            'reboot': 'fa-redo',
            'shutdown': 'fa-power-off'
        };
        const colors = {
            'restart-app': '#22c55e',
            'reboot': '#f59e0b',
            'shutdown': '#ef4444'
        };
        closePowerMenu();

        // Show confirmation modal
        const confirmed = await showPowerConfirm(labels[action], icons[action], colors[action]);
        if (!confirmed) return;

        try {
            const resp = await api('/power/action', {
                method: 'POST',
                body: { action }
            });
            if (action === 'reboot' || action === 'restart-app') {
                showRestartOverlay(action === 'reboot' ? t('Restart systemu…') : t('Restart aplikacji…'));
            } else if (action === 'shutdown') {
                showRestartOverlay(t('Wyłączanie systemu…'), true);
            } else {
                showToast(resp.message || t('Wykonano'), 'success');
            }
        } catch (err) {
            showToast(t('Błąd:') + ' ' + (err.message || t('nieznany')), 'error');
        }
    });
});

function showPowerConfirm(label, icon, color) {
    return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'power-confirm-overlay';
        overlay.innerHTML = `
            <div class="power-confirm-box">
                <div class="power-confirm-icon" style="color:${color}">
                    <i class="fas ${icon}"></i>
                </div>
                <div class="power-confirm-label">${label}</div>
                <div class="power-confirm-sub">${t('Czy na pewno chcesz kontynuować?')}</div>
                <div class="power-confirm-btns">
                    <button class="power-confirm-btn cancel">${t('Anuluj')}</button>
                    <button class="power-confirm-btn confirm" style="background:${color}">${t('Potwierdź')}</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        requestAnimationFrame(() => overlay.classList.add('visible'));

        overlay.querySelector('.cancel').addEventListener('click', () => {
            overlay.classList.remove('visible');
            setTimeout(() => overlay.remove(), 200);
            resolve(false);
        });
        overlay.querySelector('.confirm').addEventListener('click', () => {
            overlay.classList.remove('visible');
            setTimeout(() => overlay.remove(), 200);
            resolve(true);
        });
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) {
                overlay.classList.remove('visible');
                setTimeout(() => overlay.remove(), 200);
                resolve(false);
            }
        });
    });
}


function showRestartOverlay(msg, isShutdown = false) {
    const COUNTDOWN = isShutdown ? 30 : 20;
    let remaining = COUNTDOWN;

    const overlay = document.createElement('div');
    overlay.className = 'power-restart-overlay';
    overlay.innerHTML = `
        <div class="power-restart-spinner"></div>
        <div class="power-restart-msg">${msg}</div>
        <div class="power-restart-countdown">${t('System pojawi się za')} <span id="prc-sec">${remaining}</span> s</div>
        <div class="power-restart-status">${t('Oczekiwanie…')}</div>
    `;
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('visible'));

    const secEl = overlay.querySelector('#prc-sec');
    const statusEl = overlay.querySelector('.power-restart-status');
    const countdownEl = overlay.querySelector('.power-restart-countdown');

    const timer = setInterval(() => {
        remaining--;
        if (remaining >= 0) secEl.textContent = remaining;
        if (remaining <= 0) {
            clearInterval(timer);
            if (isShutdown) {
                countdownEl.textContent = t('System został wyłączony');
                statusEl.textContent = t('Możesz bezpiecznie odłączyć zasilanie.');
                overlay.querySelector('.power-restart-spinner').style.display = 'none';
                return;
            }
            countdownEl.textContent = t('Łączenie z systemem…');
            statusEl.textContent = '';
            pollServer();
        }
    }, 1000);

    function pollServer() {
        let attempts = 0;
        const maxAttempts = 60;
        const interval = setInterval(async () => {
            attempts++;
            try {
                const resp = await fetch('/api/auth/verify', { method: 'GET', cache: 'no-store' });
                if (resp.ok || resp.status === 401) {
                    clearInterval(interval);
                    overlay.classList.remove('visible');
                    setTimeout(() => {
                        overlay.remove();
                        window.location.reload();
                    }, 400);
                    return;
                }
            } catch {}
            if (attempts >= maxAttempts) {
                clearInterval(interval);
                statusEl.textContent = t('Nie udało się połączyć. Odśwież stronę ręcznie.');
            } else {
                statusEl.textContent = `${t('Próba połączenia…')} (${attempts}/${maxAttempts})`;
            }
        }, 2000);
    }
}


// ─────────────────────────── User Menu ───────────────────────────

let userMenuOpen = false;

function closeUserMenu() {
    userMenuOpen = false;
    document.getElementById('user-menu').classList.add('hidden');
    document.getElementById('user-btn').classList.remove('active');
}

document.getElementById('user-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    userMenuOpen = !userMenuOpen;
    document.getElementById('user-menu').classList.toggle('hidden', !userMenuOpen);
    document.getElementById('user-btn').classList.toggle('active', userMenuOpen);
    // Close other panels
    notifPanelOpen = false;
    document.getElementById('notif-panel').classList.add('hidden');
    document.getElementById('notifications-btn').classList.remove('active');
});

document.getElementById('btn-logout').addEventListener('click', async () => {
    try { await api('/auth/logout', { method: 'POST' }); } catch {}
    showLogin();
    closeUserMenu();
});

document.getElementById('btn-about').addEventListener('click', () => {
    closeUserMenu();
    openAbout();
});

// ── Sudo mode (always-on for admin) ──
function _updateSudoUI() {
    // Sudo is always active for admin users, no toggle needed
    NAS.sudoMode = !!(NAS.user && NAS.user.role === 'admin');
}

function openAbout() {
    const win = createWindow('about', {
        title: t('O systemie'),
        icon: 'fa-info-circle',
        iconColor: '#3b82f6',
        width: 520,
        height: 560,
        minWidth: 380,
        minHeight: 400,
        content: `
            <div class="about-content">
                <div class="about-header">
                    <div class="about-logo"><i class="fas fa-server"></i></div>
                    <div class="about-header-text">
                        <div class="about-name">${NAS.nasName}</div>
                        <div class="about-version" id="about-version-label">EthOS</div>
                        <div class="about-motto" style="font-size:11px;color:var(--text-muted);font-style:italic;margin-top:2px">Your Desktop, Anywhere</div>
                        <div class="about-build" id="about-build-date"></div>
                    </div>
                </div>
                <div class="about-tabs">
                    <button class="about-tab active" data-tab="info">${t('Informacje')}</button>
                    <button class="about-tab" data-tab="changelog">${t('Historia zmian')}</button>
                </div>
                <div class="about-tab-content" id="about-tab-info">
                    <div class="about-info">
                        <div class="dash-info-row">
                            <span class="dash-info-label">${t('Platforma')}</span>
                            <span class="dash-info-value">Docker + Flask</span>
                        </div>
                        <div class="dash-info-row">
                            <span class="dash-info-label">Frontend</span>
                            <span class="dash-info-value">Vanilla JS</span>
                        </div>
                        <div class="dash-info-row">
                            <span class="dash-info-label">Backend</span>
                            <span class="dash-info-value">Python 3.12 + gevent</span>
                        </div>
                        <div class="dash-info-row">
                            <span class="dash-info-label">${t('Komunikacja')}</span>
                            <span class="dash-info-value">Socket.IO (realtime)</span>
                        </div>
                        <div class="dash-info-row">
                            <span class="dash-info-label">${t('Licencja')}</span>
                            <span class="dash-info-value">MIT</span>
                        </div>
                    </div>
                    <div class="about-footer">
                        <span>${t('Zaprojektowane z')} <i class="fas fa-heart" style="color:#ef4444;font-size:11px"></i> ${t('przez Marcina')}</span>
                    </div>
                </div>
                <div class="about-tab-content hidden" id="about-tab-changelog">
                    <div class="about-changelog" id="about-changelog-list">
                        <div style="padding:24px;text-align:center;color:var(--text-muted)">
                            <i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}
                        </div>
                    </div>
                </div>
            </div>
        `
    });

    const body = document.getElementById('win-body-about');

    // Tab switching
    body.querySelectorAll('.about-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            body.querySelectorAll('.about-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            body.querySelectorAll('.about-tab-content').forEach(c => c.classList.add('hidden'));
            body.querySelector(`#about-tab-${tab.dataset.tab}`).classList.remove('hidden');
        });
    });

    // Load version info
    api('/system/version').then(data => {
        if (!data || data.error) return;

        const verLabel = body.querySelector('#about-version-label');
        if (verLabel) verLabel.textContent = `EthOS v${data.version}${data.codename ? ' "' + data.codename + '"' : ''}`;

        const buildDate = body.querySelector('#about-build-date');
        if (buildDate && data.build_date) {
            const d = new Date(data.build_date);
            buildDate.textContent = `Build ${d.toLocaleDateString(getLocale(), { year: 'numeric', month: 'long', day: 'numeric' })}`;
        }

        const clList = body.querySelector('#about-changelog-list');
        if (clList && data.changelog && data.changelog.length) {
            clList.innerHTML = data.changelog.map((entry, i) => `
                <div class="cl-entry${i === 0 ? ' cl-latest' : ''}">
                    <div class="cl-entry-header">
                        <div class="cl-version-badge${i === 0 ? ' cl-current' : ''}">v${entry.version}</div>
                        <div class="cl-entry-meta">
                            <div class="cl-entry-title">${entry.title}</div>
                            <div class="cl-entry-date">${entry.date}</div>
                        </div>
                    </div>
                    <ul class="cl-changes">
                        ${entry.changes.map(c => `<li>${c}</li>`).join('')}
                    </ul>
                </div>
            `).join('');
        }
    });
}

// Close panels on outside click
document.addEventListener('click', (e) => {
    if (notifPanelOpen && !e.target.closest('.notif-panel') && !e.target.closest('#notifications-btn')) {
        notifPanelOpen = false;
        document.getElementById('notif-panel').classList.add('hidden');
        document.getElementById('notifications-btn').classList.remove('active');
    }
    if (usbTrayOpen && !e.target.closest('.usb-tray-panel') && !e.target.closest('#usb-tray-btn')) {
        closeUsbTray();
    }
    if (userMenuOpen && !e.target.closest('.user-menu') && !e.target.closest('#user-btn')) {
        closeUserMenu();
    }
    if (powerMenuOpen && !e.target.closest('.power-menu') && !e.target.closest('#power-btn')) {
        closePowerMenu();
    }
    // Deselect desktop icons
    if (e.target.closest('.desktop-area') && !e.target.closest('.desktop-icon') && !e.target.closest('.window')) {
        document.querySelectorAll('.desktop-icon.selected').forEach(i => i.classList.remove('selected'));
    }
});

// ESC to close menu/panels
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        if (menuOpen) closeMainMenu();
        if (notifPanelOpen) {
            notifPanelOpen = false;
            document.getElementById('notif-panel').classList.add('hidden');
            document.getElementById('notifications-btn').classList.remove('active');
        }
        if (usbTrayOpen) closeUsbTray();
        if (userMenuOpen) closeUserMenu();
        if (powerMenuOpen) closePowerMenu();
        // Close modal
        document.querySelector('.modal-overlay')?.remove();
    }
});


// ─────────────── File Operation Floating Progress (multi-channel) ────────────────

// One progress bar per channel ('bg' = compress/download/transfer, 'fm' = copy/move/extract)
const _fileopBars = {};   // channel -> { el, timer }

function _ensureContainer() {
    let c = document.getElementById('fileop-progress-container');
    if (!c) {
        c = document.createElement('div');
        c.id = 'fileop-progress-container';
        c.style.cssText = 'position:fixed;bottom:24px;right:24px;display:flex;flex-direction:column-reverse;gap:10px;z-index:9999;pointer-events:none;';
        document.body.appendChild(c);
    }
    return c;
}

function _createBarEl(ch) {
    const el = document.createElement('div');
    el.className = 'fileop-progress-float';
    el.dataset.channel = ch;
    el.style.pointerEvents = 'auto';
    el.innerHTML = `
        <div class="fileop-header">
            <span class="fileop-title"><i class="fas fa-spinner fa-spin"></i><span class="fileop-label"></span></span>
            <div style="display:flex;align-items:center;gap:8px;">
                <span class="fileop-pct"></span>
                <button class="fileop-pause-btn fileop-action-btn fileop-action-warn" title="${t('Wstrzymaj / Wznów')}" aria-label="${t('Wstrzymaj lub wznów operację')}" style="display:none;"><i class="fas fa-pause"></i></button>
                <button class="fileop-cancel-btn fileop-action-btn fileop-action-danger" title="${t('Anuluj operację')}" aria-label="${t('Anuluj operację')}" style="display:none;"><i class="fas fa-times"></i></button>
            </div>
        </div>
        <div class="fileop-track"><div class="fileop-fill"></div></div>
        <div class="fileop-detail"></div>
    `;
    el.querySelector('.fileop-cancel-btn').addEventListener('click', async () => {
        try {
            // If there's an active upload XHR for this channel, abort it directly
            if (window._fileopUploadXhr && window._fileopUploadXhr[ch]) {
                const xhr = window._fileopUploadXhr[ch];
                delete window._fileopUploadXhr[ch];
                xhr.abort();
                return;
            }
            const r = await api('/files/cancel-operation', { method: 'POST', body: JSON.stringify({ channel: ch }), headers: { 'Content-Type': 'application/json' } });
            if (r.ok || r.cancelled) {
                const btn = el.querySelector('.fileop-cancel-btn');
                if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; }
            }
        } catch (e) { /* ignore */ }
    });
    el.querySelector('.fileop-pause-btn').addEventListener('click', async () => {
        try {
            const r = await api('/files/pause-operation', { method: 'POST', body: JSON.stringify({ channel: ch }), headers: { 'Content-Type': 'application/json' } });
            if (r && typeof r.paused !== 'undefined') {
                _updatePauseState(r.paused, ch);
            }
        } catch (e) { /* ignore */ }
    });
    return el;
}

function showFileOpProgress(data) {
    const ch = data.channel || 'bg';
    let bar = _fileopBars[ch];
    if (bar && bar.timer) { clearTimeout(bar.timer); bar.timer = null; }

    const container = _ensureContainer();
    if (!bar || !bar.el) {
        const el = _createBarEl(ch);
        container.appendChild(el);
        bar = { el, timer: null };
        _fileopBars[ch] = bar;
    }
    const el = bar.el;

    const opLabels = { copy: t('Kopiowanie'), move: t('Przenoszenie'), compress: t('Kompresja'), extract: t('Rozpakowywanie'), download: t('Pobieranie ZIP'), upload: t('Przesyłanie'), transfer: t('Transfer do NAS') };
    const pct = data.percent || 0;

    el.querySelector('.fileop-label').textContent = opLabels[data.operation] || t('Operacja');
    el.querySelector('.fileop-pct').textContent = Math.round(pct) + '%';
    el.querySelector('.fileop-fill').style.width = pct + '%';
    el.querySelector('.fileop-detail').textContent =
        data.operation === 'transfer' && data._transfer_detail
        ? data._transfer_detail
        : `${data.done}/${data.total}` + (data.current_file ? ` — ${data.current_file}` : '');

    // Show cancel + pause buttons for applicable operations
    const cancelableOps = ['transfer', 'download', 'compress', 'copy', 'move', 'extract', 'upload'];
    const pausableOps = ['transfer', 'download', 'compress', 'copy', 'move'];
    const cancelBtn = el.querySelector('.fileop-cancel-btn');
    const pauseBtn = el.querySelector('.fileop-pause-btn');
    if (cancelBtn) {
        cancelBtn.style.display = cancelableOps.includes(data.operation) ? '' : 'none';
        cancelBtn.disabled = false;
    }
    if (pauseBtn) {
        pauseBtn.style.display = pausableOps.includes(data.operation) ? '' : 'none';
    }

    el.classList.remove('fileop-done', 'fileop-error');
    el.classList.add('fileop-active');

    NAS.taskProgress.upsert({
        id: `fileop:${ch}`,
        source: t('Menedżer plików'),
        title: opLabels[data.operation] || t('Operacja plikowa'),
        percent: typeof pct === 'number' ? pct : null,
        message: data.current_file || '',
        action: { app: 'file-manager' },
        status: 'running',
    });
}

function _updatePauseState(paused, ch) {
    ch = ch || 'bg';
    const bar = _fileopBars[ch];
    if (!bar || !bar.el) return;
    const el = bar.el;
    const pauseBtn = el.querySelector('.fileop-pause-btn');
    const fill = el.querySelector('.fileop-fill');
    if (pauseBtn) {
        pauseBtn.innerHTML = paused ? '<i class="fas fa-play"></i>' : '<i class="fas fa-pause"></i>';
        pauseBtn.title = paused ? t('Wznów') : t('Wstrzymaj');
    }
    if (fill) {
        fill.style.background = paused ? '#eab308' : '';
    }
    const detail = el.querySelector('.fileop-detail');
    if (detail && paused) {
        detail.textContent = t('Wstrzymano');
    }
}

function finishFileOpProgress(success, data) {
    const ch = (data && data.channel) || 'bg';
    const bar = _fileopBars[ch];
    if (!bar || !bar.el) return;
    const el = bar.el;

    el.classList.remove('fileop-active');
    el.classList.add(success ? 'fileop-done' : 'fileop-error');

    const icon = success ? 'fa-check-circle' : 'fa-times-circle';
    const msg = data?.message || (success ? t('Zakończono') : t('Błąd'));
    el.querySelector('.fileop-header .fileop-title').innerHTML =
        `<i class="fas ${icon}"></i><span>${msg}</span>`;
    el.querySelector('.fileop-pct').textContent = success ? '100%' : '';
    el.querySelector('.fileop-fill').style.width = success ? '100%' : '0';
    el.querySelector('.fileop-fill').style.background = '';
    el.querySelector('.fileop-detail').textContent = '';

    // Hide control buttons when done
    const cancelBtn = el.querySelector('.fileop-cancel-btn');
    const pauseBtn = el.querySelector('.fileop-pause-btn');
    if (cancelBtn) cancelBtn.style.display = 'none';
    if (pauseBtn) pauseBtn.style.display = 'none';

    bar.timer = setTimeout(() => {
        if (bar.el) {
            bar.el.classList.add('fileop-removing');
            setTimeout(() => {
                if (bar.el) { bar.el.remove(); }
                delete _fileopBars[ch];
            }, 300);
        }
    }, 4000);

    NAS.taskProgress.finish(`fileop:${ch}`, success, msg);
}


// ─────────────── Restore active file-op progress on page load ────────────────

async function _checkActiveFileOp() {
    try {
        const st = await api('/files/operation-status');
        if (!st) return;

        // Multi-channel: check each channel for active progress
        const channels = st.channels || {};
        for (const [ch, info] of Object.entries(channels)) {
            if (info.active && info.progress) {
                const prog = { ...info.progress, channel: ch };
                showFileOpProgress(prog);
                if (info.paused) _updatePauseState(true, ch);
            }
        }
        // Fallback for old single-slot format
        if (!Object.keys(channels).length && st.active && st.progress) {
            showFileOpProgress(st.progress);
            if (st.paused) _updatePauseState(true, 'bg');
        }

        // If there are pending downloads ready (ZIP completed while page was away), trigger them
        if (st.pending_downloads && st.pending_downloads.length) {
            for (const pd of st.pending_downloads) {
                toast(`ZIP gotowy: ${pd.name}`, 'success');
                // Trigger browser download via hidden iframe
                const iframe = document.createElement('iframe');
                iframe.style.display = 'none';
                iframe.src = `/api/files/download-zip/${pd.download_id}`;
                document.body.appendChild(iframe);
                setTimeout(() => iframe.remove(), 30000);
            }
        }
    } catch {
        // ignore — server may still be starting
    }
}

// ─────────────────────────── Socket.IO ───────────────────────────

// Persistent DLM notification state — survives DLM window close/reopen
const _dlmNotifFailedIds = new Set();
const _dlmNotifPkgStatus = new Map();

function connectSocket() {
    try {
        // Clean up old socket listeners to prevent stacking on reconnect
        if (NAS.socket) {
            NAS.socket.off();
            NAS.socket.disconnect();
        }
        NAS.socket = io({ transports: ['websocket', 'polling'] });
        NAS.socket.on('system_stats', (data) => {
            NAS.stats = data;
            // Update taskbar stats
            document.querySelector('#stat-cpu span').textContent = Math.round(data.cpu) + '%';
            document.querySelector('#stat-ram span').textContent = Math.round(data.memory_percent) + '%';
            document.querySelector('#stat-net span').textContent = formatSpeed(data.net_down);

            // Color coding
            const cpuEl = document.getElementById('stat-cpu');
            cpuEl.style.color = data.cpu > 80 ? '#ef4444' : data.cpu > 50 ? '#eab308' : '';
            const ramEl = document.getElementById('stat-ram');
            ramEl.style.color = data.memory_percent > 80 ? '#ef4444' : data.memory_percent > 50 ? '#eab308' : '';
        });

        // Backup events → refresh notification badge
        NAS.socket.on('backup_complete', () => {
            loadNotifications();
        });
        NAS.socket.on('backup_error', () => {
            loadNotifications();
        });
        NAS.socket.on('backup_progress', () => {
            if (!NAS._lastBackupNotifRefresh || Date.now() - NAS._lastBackupNotifRefresh > 30000) {
                NAS._lastBackupNotifRefresh = Date.now();
                loadNotifications();
            }
        });

        // File operation events → floating progress bar + notification badge
        NAS.socket.on('fileop_progress', (data) => {
            showFileOpProgress(data);
            if (!NAS._lastFileopNotifRefresh || Date.now() - NAS._lastFileopNotifRefresh > 5000) {
                NAS._lastFileopNotifRefresh = Date.now();
                loadNotifications();
            }
        });
        NAS.socket.on('fileop_transfer_detail', (data) => {
            // Enhanced progress display for remote transfers with byte-level info
            showFileOpProgress({
                operation: 'transfer',
                current_file: data.current_file,
                done: data.files_sent,
                total: data.total_files,
                percent: data.percent,
                _transfer_detail: `${data.sent_fmt} / ${data.total_fmt} — ${data.current_file || ''}`
            });
        });
        NAS.socket.on('fileop_complete', (data) => {
            finishFileOpProgress(true, data);
            loadNotifications();
        });
        NAS.socket.on('fileop_error', (data) => {
            finishFileOpProgress(false, data);
            loadNotifications();
        });

        // App Store install progress -> global stacked tasks
        NAS.socket.on('appstore_install_progress', (data) => {
            if (!data || !data.task_id) return;
            const stageLabels = {
                prepare: t('Przygotowywanie'),
                pull: t('Pobieranie obrazów'),
                start: t('Uruchamianie'),
                verify: t('Weryfikacja'),
                done: t('Zakończono'),
                warning: t('Zakończono z ostrzeżeniami'),
                error: t('Błąd'),
            };
            const id = `appstore:${data.task_id}`;
            const title = data.app_id ? `App Store: ${data.app_id}` : 'App Store';
            const message = data.message || stageLabels[data.stage] || '';
            if (data.stage === 'done' || data.stage === 'warning') {
                NAS.taskProgress.upsert({ id, source: 'App Store', title, percent: 100, message, action: { app: 'app-store' }, status: 'running' });
                NAS.taskProgress.finish(id, true, message);
                return;
            }
            if (data.stage === 'error') {
                NAS.taskProgress.upsert({ id, source: 'App Store', title, percent: data.percent || null, message, action: { app: 'app-store' }, status: 'running' });
                NAS.taskProgress.finish(id, false, message);
                return;
            }
            NAS.taskProgress.upsert({
                id,
                source: 'App Store',
                title,
                percent: typeof data.percent === 'number' ? data.percent : null,
                message,
                action: { app: 'app-store' },
                status: 'running',
            });
        });
        NAS.socket.on('fileop_paused', (data) => {
            _updatePauseState(data.paused, data.channel || 'bg');
        });

        // ── USB hotplug events ──
        NAS.socket.on('usb_connected', (data) => {
            const label = data.label || data.dev;
            const size = data.size ? ` (${data.size})` : '';
            toast(`${t('USB podłączony:')} ${label}${size}`, 'success');
            loadNotifications();
            setTimeout(() => loadUsbTrayDrives(), 2000);
        });
        NAS.socket.on('usb_disconnected', (data) => {
            toast(`${t('USB odłączony:')} /dev/${data.dev}`, 'warning');
            loadNotifications();
            setTimeout(() => loadUsbTrayDrives(), 1000);
        });

        // ── Duplicate scan events → global tracking + notifications ──
        NAS._dupScan = NAS._dupScan || { running: false, phase: '', groups: 0, scanned: 0, total: 0 };

        NAS.socket.on('dup_progress', (data) => {
            NAS._dupScan.running = true;
            NAS._dupScan.phase = data.phase || '';
            NAS._dupScan.scanned = data.scanned || 0;
            NAS._dupScan.total = data.total || 0;
            // Throttled notif refresh
            if (!NAS._lastDupNotifRefresh || Date.now() - NAS._lastDupNotifRefresh > 5000) {
                NAS._lastDupNotifRefresh = Date.now();
                loadNotifications();
            }
        });
        NAS.socket.on('dup_new_group', (data) => {
            NAS._dupScan.groups = data.total_groups || (NAS._dupScan.groups + 1);
        });
        NAS.socket.on('dup_complete', (data) => {
            NAS._dupScan.running = false;
            NAS._dupScan.phase = 'done';
            NAS._dupScan.completeData = data;
            loadNotifications();
            toast(`${t('Duplikaty:')} ${data.groups} ${t('grup')} (${data.duplicates} ${t('plików')}, ${formatBytes(data.size)})`, 'success');
        });
        NAS.socket.on('dup_cancelled', (data) => {
            NAS._dupScan.running = false;
            NAS._dupScan.phase = 'cancelled';
            NAS._dupScan.cancelData = data;
            loadNotifications();
            toast(t('Skanowanie duplikatów anulowane'), 'info');
        });
        NAS.socket.on('dup_error', (data) => {
            NAS._dupScan.running = false;
            NAS._dupScan.phase = 'error';
            NAS._dupScan.errorData = data;
            loadNotifications();
            toast(t('Błąd skanowania duplikatów:') + ' ' + (data.error || ''), 'error');
        });

        // DLM — persistent notifications (work even when DLM window is closed)
        if ("Notification" in window && Notification.permission === "default") {
            Notification.requestPermission();
        }
        NAS.socket.on('dl:completed', (data) => {
            if (!data) return;
            const filename = data.filename || t('Pobrane pliki');
            const size = data.filesize ? _dlmFormatBytes(data.filesize) : '';
            const time = new Date().toLocaleTimeString();
            _dlmNotify(
                t('Pobieranie zakończone'),
                `${filename}${size ? '\n' + t('Rozmiar') + ': ' + size : ''}\n${t('Czas')}: ${time}`
            );
            const toastMessage = `${t('Pobieranie zakończone')}: ${filename}${size ? ` (${size})` : ''}`;
            const folder = data.folder || data.dest_path || data.dest_dir || '';
            if (folder) {
                toastWithAction(toastMessage, 'success', t('Otwórz folder'), () => {
                    openApp('file-manager', { path: folder });
                });
            } else {
                toast(toastMessage, 'success');
            }
        });
        NAS.socket.on('dl:update', (data) => {
            if (data?.status === 'failed' && data.id && !_dlmNotifFailedIds.has(data.id)) {
                _dlmNotifFailedIds.add(data.id);
                _dlmNotify(
                    t('Błąd pobierania'),
                    `${data.filename || ''}\n${data.error || t('Nieznany błąd')}`
                );
            }
        });
        NAS.socket.on('dl:package_update', (data) => {
            if (!data?.id) return;
            const prev = _dlmNotifPkgStatus.get(data.id);
            if (data.status === 'extracted' && prev !== 'extracted') {
                _dlmNotifPkgStatus.set(data.id, 'extracted');
                _dlmNotify(t('Ekstrakcja zakończona'), `${t('Pakiet')}: ${data.name || data.id}`);
            } else if (data.status === 'extract_failed' && prev !== 'extract_failed') {
                _dlmNotifPkgStatus.set(data.id, 'extract_failed');
                _dlmNotify(t('Błąd ekstrakcji'), `${t('Pakiet')}: ${data.name || data.id}\n${data.extract_error || ''}`);
            }
        });

        // Tickets real-time events — dispatch to tickets app if open
        NAS.socket.on('tickets_event', (data) => {
            if (typeof window._onTicketsEvent === 'function') {
                window._onTicketsEvent(data);
            }
        });

        NAS.socket.on('notification_new', (data) => {
            NAS._notifCount = (NAS._notifCount || 0) + 1;
            _updateNotifBadge();
            if (notifPanelOpen) loadNotifications();
        });

        NAS.socket.on('notification_count', (data) => {
            NAS._notifCount = data.count || 0;
            _updateNotifBadge();
        });

        // Reconnect handling — auto-reconnect on disconnect
        NAS.socket.on('disconnect', (reason) => {
            console.warn('[socket] disconnected:', reason);
            NAS._socketConnected = false;
        });
        NAS.socket.on('connect', () => {
            if (NAS._socketConnected === false) {
                console.log('[socket] reconnected');
            }
            NAS._socketConnected = true;
        });
        NAS.socket.on('connect_error', (err) => {
            console.warn('[socket] connect error:', err.message);
        });
    } catch {
        // Reconnect later
        setTimeout(connectSocket, 5000);
    }
}


// ─────────────────────────── Desktop Init ───────────────────────────

let desktopInitialized = false;

async function initDesktop() {
    if (desktopInitialized) return;
    desktopInitialized = true;

    document.getElementById('taskbar-hostname');
    document.getElementById('login-hostname').textContent = NAS.nasName;

    // Show logged-in username in user menu
    const umName = document.getElementById('um-username');
    if (umName && NAS.user) {
        const uname = NAS.user.username || 'admin';
        umName.textContent = uname.charAt(0).toUpperCase() + uname.slice(1);
    }

    // Show/hide sudo toggle for admin users
    _updateSudoUI();

    // Load apps
    try {
        NAS.apps = await api('/apps');
    } catch {
        NAS.apps = [];
    }

    // Load desktop icon preferences
    try {
        const dPrefs = await api('/desktop-apps');
        NAS.desktopAppIds = dPrefs.app_ids || null;
    } catch {
        NAS.desktopAppIds = null;
    }

    renderDesktopIcons();
    renderMenuGrid();
    connectSocket();
    loadNotifications();
    loadUsbTrayDrives();
    _checkActiveFileOp();

    // Autostart Sticky Notes if enabled
    if (localStorage.getItem('sn_autostart') === '1') {
        setTimeout(() => {
            const snApp = NAS.apps.find(a => a.id === 'sticky-notes');
            if (snApp && !document.getElementById('sn-panel')) openApp(snApp);
        }, 500);
    }

    // Auto-launch app from URL parameter (?app=radio-music etc.)
    try {
        const urlApp = new URLSearchParams(window.location.search).get('app');
        if (urlApp) {
            setTimeout(() => {
                const appDef = NAS.apps.find(a => a.id === urlApp);
                if (appDef) openApp(appDef);
            }, 600);
        }
    } catch {}

    // Periodic notification check
    let _notifInterval = setInterval(loadNotifications, 60000);

    // Pause timers when tab is hidden (save CPU/battery)
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            clearInterval(_clockInterval);
            clearInterval(_notifInterval);
        } else {
            _clockInterval = setInterval(updateClocks, 1000);
            _notifInterval = setInterval(loadNotifications, 60000);
            updateClocks();
            loadNotifications();
        }
    });

    // First-login Security Advisor — show once after first real login
    if (!localStorage.getItem('sa_welcome_shown')) {
        setTimeout(() => showSecurityWelcome(), 1500);
    }
}


// ─────────────────────────── Modal Dialog Utility ───────────────────────────

function showModal(title, bodyHtml, buttons = []) {
    return new Promise((resolve) => {
        let resolved = false;
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal">
                <div class="modal-header">${title}</div>
                <div class="modal-body">${bodyHtml}</div>
                <div class="modal-footer"></div>
            </div>
        `;
        const modal = overlay.querySelector('.modal');
        const footer = overlay.querySelector('.modal-footer');

        function cleanup(val) {
            if (resolved) return;
            resolved = true;
            document.removeEventListener('keydown', onKeyDown);
            if (overlay.parentNode) overlay.remove();
            resolve(val);
        }

        buttons.forEach((btnDef) => {
            const btn = document.createElement('button');
            btn.className = `btn ${btnDef.cls || btnDef.class || ''}`;
            btn.textContent = btnDef.label;
            btn.addEventListener('click', async () => {
                if (typeof btnDef.action === 'function') {
                    try { await btnDef.action(modal); } catch (_) {}
                }
                cleanup(btnDef.value !== undefined ? btnDef.value : (typeof btnDef.action === 'function' ? btnDef.label : null));
            });
            footer.appendChild(btn);
        });
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) cleanup(null);
        });

        const allBtns = footer.querySelectorAll('button');
        const confirmBtn = allBtns.length ? allBtns[allBtns.length - 1] : null;
        const input = overlay.querySelector('input');

        function onKeyDown(e) {
            if (e.key === 'Escape') { e.preventDefault(); cleanup(null); }
            else if (e.key === 'Enter' && confirmBtn) { e.preventDefault(); confirmBtn.click(); }
        }

        document.body.appendChild(overlay);
        document.addEventListener('keydown', onKeyDown);
        if (input) {
            input.focus();
        } else if (confirmBtn) {
            confirmBtn.focus();
        }
    });
}

async function promptDialog(title, label, defaultValue = '') {
    let inputValue = defaultValue;
    const result = await showModal(title, `
        <label class="modal-label">${label}</label>
        <input class="modal-input" id="prompt-input" value="${defaultValue}">
    `, [
        { label: t('Anuluj'), value: null },
        { label: t('OK'), cls: 'btn-primary', action: (modal) => {
            inputValue = modal.querySelector('#prompt-input')?.value || '';
        }, value: '__OK__' }
    ]);
    return result === '__OK__' ? inputValue : null;
}

async function confirmDialog(titleOrMsg, messageOrCallback) {
    // Signatures:
    //   confirmDialog(message)           -> modal with default title, returns bool
    //   confirmDialog(title, message)    -> modal with custom title, returns bool
    //   confirmDialog(message, callback) -> modal with default title, calls callback if confirmed
    const nl = s => String(s || '').replace(/\n/g, '<br>');
    if (messageOrCallback === undefined) {
        return confirmDialog(t('Potwierdzenie'), titleOrMsg);
    }
    if (typeof messageOrCallback === 'function') {
        const ok = await showModal(t('Potwierdzenie'), `<p style="color:var(--text-secondary)">${nl(titleOrMsg)}</p>`, [
            { label: t('Anuluj'), value: false },
            { label: t('Potwierdź'), cls: 'btn-danger', value: true }
        ]);
        if (ok) await messageOrCallback();
        return ok;
    }
    return await showModal(titleOrMsg, `<p style="color:var(--text-secondary)">${nl(messageOrCallback)}</p>`, [
        { label: t('Anuluj'), value: false },
        { label: t('Potwierdź'), cls: 'btn-danger', value: true }
    ]);
}


// ─────────────────────────── Init ───────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
    // Load saved language BEFORE any UI renders
    await initI18n();
    translateDOM(document);

    if (await checkSetupNeeded()) {
        showSetupWizard();
    } else {
        tryAutoLogin();
    }
});

// ───────────────────── DLM Helpers (Global) ─────────────────────

function _dlmNotify(title, body, icon) {
    if (!("Notification" in window)) return;

    // Default icon if not provided
    if (!icon) icon = '/favicon.ico'; // Fallback to favicon

    if (Notification.permission === "granted") {
        new Notification(title, { body, icon });
    } else if (Notification.permission !== "denied") {
        Notification.requestPermission().then(permission => {
            if (permission === "granted") {
                new Notification(title, { body, icon });
            }
        });
    }
}

function _dlmFormatBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
}

function showPasswordChangeModal() {
    const content = `
        <div style="padding:20px;text-align:center;">
            <div style="font-size:48px;color:var(--accent);margin-bottom:16px;"><i class="fas fa-shield-alt"></i></div>
            <h3 style="margin:0 0 10px;font-size:20px;">` + t('Wymagana zmiana hasła') + `</h3>
            <p style="margin:0 0 24px;color:var(--text-secondary);font-size:14px;line-height:1.5;">` + t('Ze względów bezpieczeństwa musisz zmienić domyślne hasło administratora.') + `</p>

            <div class="form-group" style="text-align:left;">
                <label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;display:block;">` + t('Obecne hasło') + `</label>
                <div class="input-icon"><i class="fas fa-key"></i><input type="password" id="pwd-change-current" class="form-control" placeholder="` + t('Obecne hasło') + `"></div>
            </div>
            <div class="form-group" style="text-align:left;margin-top:12px;">
                <label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;display:block;">` + t('Nowe hasło') + `</label>
                <div class="input-icon"><i class="fas fa-lock"></i><input type="password" id="pwd-change-new" class="form-control" placeholder="` + t('Min. 4 znaki') + `"></div>
            </div>
            <div class="form-group" style="text-align:left;margin-top:12px;">
                <label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;display:block;">` + t('Powtórz nowe hasło') + `</label>
                <div class="input-icon"><i class="fas fa-lock"></i><input type="password" id="pwd-change-confirm" class="form-control" placeholder="` + t('Powtórz hasło') + `"></div>
            </div>

            <div id="pwd-change-error" class="login-error" style="margin:16px 0 0;text-align:left;"></div>
            <button class="btn-login" id="pwd-change-submit" style="width:100%;margin-top:20px;"><span>` + t('Zmień hasło') + `</span> <i class="fas fa-arrow-right"></i></button>
        </div>
    `;

    // Ensure login screen is hidden
    document.getElementById('login-screen').classList.add('hidden');

    const win = createWindow('password-change-modal', {
        title: t('Bezpieczeństwo'),
        width: 420,
        height: 540,
        content: content,
        singleton: true,
        icon: 'fa-shield-alt'
    });

    // Disable close button
    const closeBtn = win.el.querySelector('.window-close');
    if (closeBtn) closeBtn.style.display = 'none';

    setTimeout(() => {
        const submitBtn = win.el.querySelector('#pwd-change-submit');
        const errEl = win.el.querySelector('#pwd-change-error');
        const currentInput = win.el.querySelector('#pwd-change-current');

        if (currentInput) currentInput.focus();

        submitBtn.onclick = async () => {
            const current = win.el.querySelector('#pwd-change-current').value;
            const newPw = win.el.querySelector('#pwd-change-new').value;
            const confirm = win.el.querySelector('#pwd-change-confirm').value;

            if (!current || !newPw) { errEl.textContent = t('Wypełnij wszystkie pola'); return; }
            if (newPw.length < 4) { errEl.textContent = t('Hasło za krótkie (min. 4 znaki)'); return; }
            if (newPw === 'ethos') { errEl.textContent = t('Hasło nie może być domyślne ("ethos")'); return; }
            if (newPw !== confirm) { errEl.textContent = t('Hasła nie są identyczne'); return; }

            submitBtn.disabled = true;
            submitBtn.style.opacity = '0.7';
            errEl.textContent = '';

            try {
                const r = await api('/settings/change-password', {
                    method: 'POST',
                    body: { current_password: current, new_password: newPw }
                });

                if (r.error) throw new Error(r.error);

                win.close();
                toast(t('Hasło zmienione pomyślnie'), 'success');
                showDesktop();
                // Show Security Advisor on first password change (new installation)
                setTimeout(() => showSecurityWelcome(), 1200);
            } catch (e) {
                errEl.textContent = e.message || t('Błąd zmiany hasła');
                submitBtn.disabled = false;
                submitBtn.style.opacity = '1';
            }
        };
    }, 100);
}


// ─────────────────── First-Login Security Advisor Welcome ───────────────────

async function showSecurityWelcome() {
    if (localStorage.getItem('sa_welcome_shown')) return;
    // Only for admins
    if (!NAS.user || NAS.user.role !== 'admin') return;

    localStorage.setItem('sa_welcome_shown', '1');

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay saw-overlay';
    overlay.innerHTML = `
    <div class="saw-card">
      <div class="saw-header">
        <div class="saw-icon"><i class="fas fa-shield-alt"></i></div>
        <h2>${t('Witaj w EthOS!')}</h2>
        <p>${t('Sprawdźmy bezpieczeństwo Twojego systemu')}</p>
      </div>
      <div class="saw-body">
        <div class="saw-loading">
          <i class="fas fa-spinner fa-spin"></i> ${t('Skanowanie systemu...')}
        </div>
      </div>
      <div class="saw-footer">
        <button class="btn btn-secondary saw-skip" id="saw-skip">${t('Pomiń')}</button>
        <button class="btn btn-primary saw-open" id="saw-open" style="display:none">
          <i class="fas fa-external-link-alt"></i> ${t('Otwórz Security Advisor')}
        </button>
      </div>
    </div>`;

    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('saw-visible'));

    const close = () => {
        overlay.classList.remove('saw-visible');
        setTimeout(() => overlay.remove(), 300);
    };

    overlay.querySelector('#saw-skip').onclick = close;

    // Run scan
    try {
        const data = await api('/security-advisor/scan');
        if (!data || data.error) throw new Error(data?.error || 'scan failed');

        const score = data.score || 0;
        const failed = (data.checks || []).filter(c => !c.passed);
        const fixable = failed.filter(c => c.fixable && c.fix_action);

        const scoreColor = score >= 80 ? '#22c55e' : score >= 50 ? '#f59e0b' : '#ef4444';
        const scoreLabel = score >= 80 ? t('Dobry poziom bezpieczeństwa')
                         : score >= 50 ? t('Wymaga poprawy')
                         : t('Niski poziom — zalecane działanie');

        const circumference = 2 * Math.PI * 42;
        const offset = circumference * (1 - score / 100);

        let checksHtml = '';
        const sevOrder = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
        const sorted = failed.sort((a, b) => (sevOrder[a.severity] || 4) - (sevOrder[b.severity] || 4));
        const shown = sorted.slice(0, 5);

        for (const c of shown) {
            const color = c.severity === 'critical' ? '#dc2626' : c.severity === 'high' ? '#ea580c' : '#d97706';
            const fixBtn = (c.fixable && c.fix_action)
                ? `<button class="btn btn-small btn-primary saw-fix" data-action="${c.fix_action}"><i class="fas fa-wrench"></i> ${t('Napraw')}</button>`
                : '';
            checksHtml += `
            <div class="saw-check">
              <i class="fas fa-exclamation-triangle" style="color:${color};flex-shrink:0;margin-top:2px"></i>
              <div style="flex:1;min-width:0">
                <div style="font-weight:500;font-size:13px">${c.title}</div>
                ${c.description ? `<div style="font-size:11px;color:var(--text-secondary);margin-top:2px">${c.description}</div>` : ''}
              </div>
              ${fixBtn}
            </div>`;
        }
        if (failed.length > 5) {
            checksHtml += `<div style="text-align:center;font-size:12px;color:var(--text-muted);padding:8px">
              ${t('...i {n} więcej', { n: failed.length - 5 })}
            </div>`;
        }

        const bodyEl = overlay.querySelector('.saw-body');
        bodyEl.innerHTML = `
          <div class="saw-score-row">
            <div class="saw-score-mini">
              <svg viewBox="0 0 100 100" style="width:90px;height:90px;transform:rotate(-90deg)">
                <circle cx="50" cy="50" r="42" fill="none" stroke="var(--bg-tertiary,#333)" stroke-width="6"/>
                <circle cx="50" cy="50" r="42" fill="none" stroke="${scoreColor}" stroke-width="6"
                  stroke-linecap="round" stroke-dasharray="${circumference}" stroke-dashoffset="${offset}"
                  style="transition:stroke-dashoffset .8s ease"/>
              </svg>
              <div class="saw-score-num" style="color:${scoreColor}">${score}</div>
            </div>
            <div>
              <div style="font-size:18px;font-weight:600;color:var(--text-primary)">${scoreLabel}</div>
              <div style="font-size:13px;color:var(--text-secondary);margin-top:4px">
                ${data.passed}/${data.total} ${t('testów zaliczonych')}
                ${fixable.length > 0 ? ` · <b>${fixable.length}</b> ${t('do naprawienia jednym kliknięciem')}` : ''}
              </div>
            </div>
          </div>
          ${checksHtml ? `<div class="saw-checks">${checksHtml}</div>` : `<div style="text-align:center;padding:16px;color:var(--success)"><i class="fas fa-check-circle"></i> ${t('Wszystko wygląda dobrze!')}</div>`}
        `;

        // Fix buttons
        bodyEl.querySelectorAll('.saw-fix').forEach(btn => {
            btn.onclick = async () => {
                const action = btn.dataset.action;
                btn.disabled = true;
                btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i>`;
                try {
                    const r = await api('/security-advisor/fix', { method: 'POST', body: JSON.stringify({ action }) });
                    if (r.ok) {
                        toast(r.message || t('Naprawiono'), 'success');
                        btn.closest('.saw-check').style.opacity = '0.4';
                        btn.innerHTML = `<i class="fas fa-check"></i>`;
                    } else {
                        toast(r.error || t('Błąd'), 'error');
                        btn.innerHTML = `<i class="fas fa-wrench"></i> ${t('Napraw')}`;
                        btn.disabled = false;
                    }
                } catch {
                    toast(t('Błąd naprawy'), 'error');
                    btn.innerHTML = `<i class="fas fa-wrench"></i> ${t('Napraw')}`;
                    btn.disabled = false;
                }
            };
        });

        // Show "Open full app" button
        overlay.querySelector('#saw-open').style.display = '';
        overlay.querySelector('#saw-open').onclick = () => {
            close();
            const appDef = (NAS.apps || []).find(a => a.id === 'security-advisor');
            if (appDef) openApp(appDef);
        };
        overlay.querySelector('#saw-skip').textContent = t('Zamknij');

    } catch (e) {
        // Security Advisor not available (optional blueprint) — just close
        overlay.querySelector('.saw-body').innerHTML = `
          <div style="text-align:center;padding:20px;color:var(--text-secondary)">
            <i class="fas fa-info-circle" style="font-size:24px;margin-bottom:8px"></i><br>
            ${t('Security Advisor nie jest dostępny. Możesz go zainstalować w Package Center.')}
          </div>`;
    }
}
