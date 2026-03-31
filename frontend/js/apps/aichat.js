/* ─────────────────── AI Chat (EthOS) — Advanced Edition ─────────────────── */
/* globals AppRegistry, createWindow, NAS, showToast, t, WM */

AppRegistry['ai-chat'] = function (appDef, launchOpts) {
    var alreadyOpen = typeof WM !== 'undefined' && WM.windows && WM.windows.has('ai-chat');
    createWindow('ai-chat', {
        title: t('AI Chat'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1050,
        height: 700,
        onRender: function (body) { renderAIChat(body, launchOpts); },
    });
    // createWindow returns early (focusing the existing window) when singleton is already open,
    // so onRender is never called. Apply launchOpts directly to the running instance instead.
    if (alreadyOpen && launchOpts) {
        _aic.pendingLaunch = launchOpts;
        _aicMaybeHandleLaunch();
    }
};

/* ═══════════════════════════ STATE ═══════════════════════════ */
var _aic = {
    cfg: null,
    convs: [],
    activeConv: null,
    streaming: false,
    view: 'chat',            // 'chat' | 'settings' | 'filepicker' | 'models' | 'wizard' | 'dashboard'
    attachedFiles: [],        // [{ path, name, content, size }]
    fpPath: '/home',          // file picker cwd
    fpItems: [],
    fpLoading: false,
    fpSelected: {},           // path → true
    // Wizard & calibration
    wizardStep: 0,
    wizardHw: null,
    wizardBench: null,
    wizardRecModel: null,
    calibration: null,
    health: null,
    // RAG
    rag: null,           // {stats, indexing, progress, rag_enabled}
    ragSources: [],      // sources from last RAG response
    deps: null,
    pendingLaunch: null, // optional deep-link launch options
    convOverrides: {},   // conversation_id -> { providerOverride, ragEnabled, modelOverride }
    pendingOverrides: null, // overrides to apply once new conversation id is known
};

function _aicTierClass(id) {
    if (!id) return 'aic-tier-light';
    return 'aic-tier-' + id.toString().toLowerCase();
}

function _aicTierClassByScore(score) {
    if (typeof score !== 'number') return 'aic-tier-light';
    if (score >= 85) return 'aic-tier-ultra';
    if (score >= 65) return 'aic-tier-balanced';
    return 'aic-tier-light';
}

function _aicTierClassFromRecord(tier) {
    if (tier) {
        var id = tier.id || tier.tier_id;
        if (id) return _aicTierClass(id);
        if (typeof tier.score === 'number') return _aicTierClassByScore(tier.score);
    }
    return 'aic-tier-light';
}

function _aicTierTextClass(tier) {
    var id = tier && (tier.id || tier.tier_id);
    if (id) return 'aic-tier-text-' + id.toString().toLowerCase();
    return 'aic-tier-text-light';
}

function _aicStatusClass(status) {
    if (status === 'recommended') return 'aic-status-good';
    if (status === 'possible') return 'aic-status-medium';
    return 'aic-status-bad';
}

function _aicTpsClass(value) {
    if (value >= 15) return 'aic-status-good';
    if (value >= 5) return 'aic-status-medium';
    return 'aic-status-bad';
}

function _aicFamilyKey(name) {
    var raw = (name || 'custom').toString().toLowerCase();
    var cleaned = raw.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    return cleaned || 'custom';
}

function _aicSetHidden(el, hidden) {
    if (!el) return;
    if (hidden) el.classList.add('aic-hidden');
    else el.classList.remove('aic-hidden');
}

function _aicUpdateWizardDlBar(ds) {
    if (!ds) return;
    var bar = document.getElementById('wizDlBar');
    var pctEl = document.querySelector('.aic-wiz-dl-pct');
    var statusEl = document.getElementById('wizDlStatus');
    var speedEl = document.getElementById('wizDlSpeed');
    var pct = Math.round(ds.progress || 0);
    if (bar) bar.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';
    if (statusEl) statusEl.textContent = ds.status || '';
    if (ds.speed) {
        if (!speedEl) {
            var detailEl = document.querySelector('.aic-wiz-dl-detail');
            if (detailEl) {
                speedEl = document.createElement('span');
                speedEl.id = 'wizDlSpeed';
                detailEl.appendChild(speedEl);
            }
        }
        if (speedEl) speedEl.textContent = ds.speed;
    } else if (speedEl) {
        speedEl.textContent = '';
    }
}

/* ═══════════════════════════ HELPERS ═══════════════════════════ */
function _aicFetch(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (NAS.token) opts.headers['Authorization'] = 'Bearer ' + NAS.token;
    if (NAS.csrfToken) opts.headers['X-CSRFToken'] = NAS.csrfToken;
    return fetch(url, opts);
}

function _aicEsc(s) {
    var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML;
}

/* ── markdown rendering with code-block actions ── */
var _aicCodeBlockId = 0;

function _aicMd(text) {
    _aicCodeBlockId = 0;
    var s = _aicEsc(text);

    // fenced code blocks → with copy/apply buttons
    s = s.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
        var id = '_aicCB' + (++_aicCodeBlockId);
        var langLabel = lang ? '<span class="aic-cb-lang">' + lang + '</span>' : '';
        return '<div class="aic-codeblock" id="' + id + '">' +
            '<div class="aic-cb-header">' + langLabel +
                '<div class="aic-cb-actions">' +
                    '<button class="aic-cb-btn" onclick="window._aicCopyCode(\'' + id + '\')" title="Kopiuj"><i class="fas fa-copy"></i></button>' +
                    '<button class="aic-cb-btn" onclick="window._aicApplyCode(\'' + id + '\')" title="Zapisz do pliku"><i class="fas fa-file-export"></i></button>' +
                    '<button class="aic-cb-btn" onclick="window._aicRunCode(\'' + id + '\')" title="Uruchom w terminalu"><i class="fas fa-terminal"></i></button>' +
                '</div>' +
            '</div>' +
            '<pre class="aic-code"><code>' + code + '</code></pre>' +
        '</div>';
    });
    // inline code
    s = s.replace(/`([^`]+)`/g, '<code class="aic-inline-code">$1</code>');
    // bold
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // italic
    s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // newlines
    s = s.replace(/\n/g, '<br>');
    return s;
}

/* ═══════════════════════════ MAIN RENDER ═══════════════════════════ */
function renderAIChat(body, launchOpts) {
    _aic.pendingLaunch = launchOpts || null;
    body.innerHTML = `<div class="aic-root"><div class="aic-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div></div>`;
    _aicLoadConfig(function () {
        _aicLoadStatus(function () {
            _aicLoadConversations(function () {
                _aicRender(body);
                _aicMaybeHandleLaunch();
            });
        });
    });
}

function _aicMaybeHandleLaunch() {
    var opts = _aic.pendingLaunch;
    if (!opts || opts._handled) return;
    opts._handled = true;
    if (opts.ticketContext) {
        var tk = opts.ticketContext;
        var preferredModel = opts.preferredModel || null;
        var ragOff = true;
        if (Object.prototype.hasOwnProperty.call(opts, 'disableRag')) {
            ragOff = !!opts.disableRag;
        }
        var modelOverride = null;
        if (preferredModel && typeof preferredModel.id === 'string') {
            modelOverride = preferredModel.id;
        }
        var summary = [
            t('Jesteś lokalnym agentem kodowania EthOS (bez RAG).'),
            'Ticket: ' + (tk.id || ''),
            t('Tytuł: ') + (tk.title || ''),
        ];
        if (tk.description) summary.push('Opis: ' + tk.description);
        if (tk.priority) summary.push('Priorytet: ' + tk.priority);
        if (tk.complexity) summary.push(t('Złożoność: ') + tk.complexity);
        if (tk.column) summary.push('Kolumna: ' + tk.column);
        if (tk.labels && tk.labels.length) summary.push('Etykiety: ' + tk.labels.join(', '));
        if (preferredModel) {
            summary.push('Preferowany model: ' + (preferredModel.label || preferredModel.name || preferredModel.id || 'lokalny'));
        }
        summary.push(t('Używaj lokalnego modelu (np. Qwen 2.5 Coder 7B) i nie korzystaj z RAG.'));
        _aicSendMessage(summary.join('\n'), {
            ragEnabled: !ragOff,
            forceNewConv: true,
            providerOverride: 'local',
            modelOverride: modelOverride
        });
    }
}

function _aicLoadConfig(cb) {
    _aicFetch('/api/aichat/config').then(function (r) { return r.json(); }).then(function (d) {
        _aic.cfg = d; cb();
    }).catch(function () { _aic.cfg = {}; cb(); });
}

function _aicLoadStatus(cb) {
    _aicFetch('/api/aichat/status').then(function (r) { return r.json(); }).then(function (d) {
        _aic.health = d.health || null;
        _aic.deps = d.deps || null;
        _aic.calibration = d.calibration || null;
        _aic.rag = d.rag || null;
        // Always show wizard on first launch if not completed
        if (!d.calibrated) {
            _aic.view = 'wizard';
            _aic.wizardStep = 0;
        }
        cb();
    }).catch(function () { cb(); });
}

function _aicLoadConversations(cb) {
    _aicFetch('/api/aichat/conversations').then(function (r) { return r.json(); }).then(function (d) {
        _aic.convs = d; cb();
    }).catch(function () { _aic.convs = []; cb(); });
}

function _aicRender(body) {
    var root = body.querySelector('.aic-root');
    if (!root) return;

    if (_aic.view === 'settings') { _aicRenderSettings(root); return; }
    if (_aic.view === 'models') { _aicRenderModels(root); return; }
    if (_aic.view === 'filepicker') { _aicRenderFilePicker(root); return; }
    if (_aic.view === 'wizard') { _aicRenderWizard(root); return; }
    if (_aic.view === 'dashboard') { _aicRenderDashboard(root); return; }

    /* ── chat view ── */
    var healthBadge = '';
    if (_aic.health) {
        var healthClass = _aicTierClassByScore(_aic.health.score || 0);
        healthBadge = '<span class="aic-health-badge ' + healthClass + ' aic-health-badge--header" onclick="window._aicOpenDashboard()" title="AI Health Score: ' + _aic.health.score + '/100">' + _aic.health.grade + '</span>';
    }
    var healthActions = healthBadge ? '<div class="aic-main-header-actions">' + healthBadge + '</div>' : '';
    root.innerHTML =
        '<div class="aic-sidebar">' +
            '<div class="aic-sidebar-brand"><i class="fas fa-robot"></i> ' + t('AI Chat') + '</div>' +
            '<div class="aic-sidebar-header">' +
                '<button class="aic-btn-new" onclick="window._aicNewConv()" title="' + t('Nowa rozmowa') + '"><i class="fas fa-plus"></i> ' + t('Nowa') + '</button>' +
                '<button class="aic-btn-icon" onclick="window._aicOpenDashboard()" title="' + t('Dashboard AI') + '"><i class="fas fa-heartbeat"></i></button>' +
                '<button class="aic-btn-icon" onclick="window._aicOpenSettings()" title="' + t('Ustawienia') + '"><i class="fas fa-cog"></i></button>' +
                '<button class="aic-btn-icon" onclick="window._aicOpenModels()" title="' + t('Biblioteka modeli') + '"><i class="fas fa-cube"></i></button>' +
                '<button class="aic-btn-icon aic-mobile-close" onclick="window._aicToggleSidebar()"><i class="fas fa-times"></i></button>' +
            '</div>' +
            '<div class="aic-conv-list" id="aicConvList"></div>' +
        '</div>' +
        '<div class="aic-sidebar-overlay" onclick="window._aicToggleSidebar()"></div>' +
        '<div class="aic-main">' +
            '<div class="aic-main-header">' +
                '<button class="aic-btn-icon aic-mobile-menu" onclick="window._aicToggleSidebar()"><i class="fas fa-bars"></i></button>' +
                '<span class="aic-main-title text-lg"><i class="fas fa-robot"></i> ' + t('AI Chat') + '</span>' +
                healthActions +
            '</div>' +
            '<div class="aic-messages" id="aicMessages"></div>' +
            '<div id="aicAttachBar" class="aic-attach-bar aic-hidden"></div>' +
            '<div class="aic-input-bar">' +
                `<button class="aic-btn-icon aic-btn-attach" onclick="window._aicOpenFilePicker()" title="${t('Dołącz pliki')}"><i class="fas fa-paperclip"></i></button>` +
                `<textarea id="aicInput" class="aic-input" rows="1" placeholder="${t('Napisz wiadomość… (Shift+Enter = nowa linia)')}"></textarea>` +
                '<button class="aic-btn-send" id="aicSendBtn" onclick="window._aicSend()"><i class="fas fa-paper-plane"></i></button>' +
            '</div>' +
        '</div>';

    _aicRenderConvList(root);
    _aicRenderMessages(root);
    _aicRenderAttachBar();

    var ta = root.querySelector('#aicInput');
    if (ta) {
        ta.addEventListener('input', function () {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 200) + 'px';
        });
        ta.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); window._aicSend(); }
        });
        ta.focus();
    }

    // hints
    if (!_aic.cfg.api_key_set && _aic.cfg.provider !== 'local') {
        var msgs = root.querySelector('#aicMessages');
        if (msgs) msgs.innerHTML =
            '<div class="aic-empty">' +
                '<i class="fas fa-robot aic-empty-icon"></i>' +
                '<div class="aic-empty-title">' + t('Witaj w AI Chat') + '</div>' +
                '<div class="aic-empty-sub">' + t('Skonfiguruj klucz API aby rozpocząć.') + '</div>' +
                '<div class="aic-empty-actions">' +
                    '<button class="aic-btn-primary" onclick="window._aicOpenSettings()">' + t('Konfiguracja') + '</button>' +
                '</div>' +
            '</div>';
    } else if (!_aic.activeConv) {
        var msgs2 = root.querySelector('#aicMessages');
        if (msgs2) {
            var localBadge = _aic.cfg.provider === 'local'
                ? '<div class="aic-local-badge"><i class="fas fa-shield-alt"></i> ' + t('100% lokalne przetwarzanie — Twoje dane nie opuszczają serwera') + '</div>'
                : '';
            var ragBadge = '';
            if (_aic.rag && _aic.rag.stats) {
                var rs = _aic.rag.stats;
                if (rs.total_chunks > 0) {
                    ragBadge = '<div class="aic-rag-badge"><i class="fas fa-brain"></i> ' +
                        t('Baza wiedzy') + ': ' + rs.document_files + ' ' + t('dokumentów') + ', ' + rs.gallery_files + ' ' + t('zdjęć') + '</div>';
                } else {
                    ragBadge = '<div class="aic-rag-badge aic-rag-empty"><i class="fas fa-brain"></i> ' +
                        t('Baza wiedzy pusta') + ' — <a href="#" onclick="window._aicStartIndexing();return false">' + t('Zindeksuj pliki') + '</a></div>';
                }
            }
            msgs2.innerHTML =
            '<div class="aic-empty">' +
                '<i class="fas fa-robot aic-empty-icon"></i>' +
                '<div class="aic-empty-title">' + t('Osobisty Asystent EthOS') + '</div>' +
                localBadge +
                ragBadge +
                '<div class="aic-empty-sub">' + t('Zarządzanie plikami, galeria zdjęć, pytania o dokumenty.') + '<br>' +
                    '<span class="aic-empty-hint"><i class="fas fa-search"></i> ' + t('szukaj w dokumentach') + ' &nbsp; ' +
                    '<i class="fas fa-images"></i> ' + t('przeglądaj galerię') + ' &nbsp; ' +
                    '<i class="fas fa-paperclip"></i> ' + t('dołącz pliki') + ' &nbsp; ' +
                    '<i class="fas fa-terminal"></i> ' + t('uruchom') + '</span></div>' +
            '</div>';
        }
    }
}

window._aicToggleSidebar = function () {
    var root = document.querySelector('.aic-root');
    if (!root) return;
    var sidebar = root.querySelector('.aic-sidebar');
    var overlay = root.querySelector('.aic-sidebar-overlay');
    if (sidebar) sidebar.classList.toggle('active');
    if (overlay) overlay.classList.toggle('active');
};

/* ═══════════════════════════ SIDEBAR ═══════════════════════════ */
function _aicRenderConvList(root) {
    var list = root.querySelector('#aicConvList');
    if (!list) return;
    if (!_aic.convs.length) { list.innerHTML = `<div class="aic-conv-empty">${t('Brak rozmów')}</div>`; return; }
    list.innerHTML = _aic.convs.map(function (c) {
        var active = _aic.activeConv && _aic.activeConv.id === c.id ? ' aic-conv-active' : '';
        return '<div class="aic-conv-item' + active + '" onclick="window._aicSelectConv(\'' + c.id + '\')">' +
            '<div class="aic-conv-title">' + _aicEsc(c.title) + '</div>' +
            '<div class="aic-conv-meta">' + (c.message_count || 0) + ' wiad.</div>' +
            '<button class="aic-conv-del" onclick="event.stopPropagation();window._aicDeleteConv(\'' + c.id + '\')" title="' + t('Usuń') + '"><i class="fas fa-trash"></i></button>' +
        '</div>';
    }).join('');
}

/* ═══════════════════════════ MESSAGES ═══════════════════════════ */
function _aicRenderMessages(root) {
    var container = root.querySelector('#aicMessages');
    if (!container || !_aic.activeConv) return;
    var msgs = _aic.activeConv.messages || [];
    if (!msgs.length) {
        container.innerHTML =
            '<div class="aic-empty"><i class="fas fa-robot aic-empty-icon"></i>' +
                '<div class="aic-empty-sub">' + t('Napisz wiadomość, aby rozpocząć rozmowę.') + '</div></div>';
        return;
    }
    container.innerHTML = msgs.map(function (m) {
        var cls = m.role === 'user' ? 'aic-msg-user' : 'aic-msg-ai';
        var icon = m.role === 'user' ? 'fa-user' : 'fa-robot';
        return '<div class="aic-msg ' + cls + '">' +
            '<div class="aic-msg-avatar"><i class="fas ' + icon + '"></i></div>' +
            '<div class="aic-msg-bubble">' + _aicMd(m.content) + '</div>' +
        '</div>';
    }).join('');
    container.scrollTop = container.scrollHeight;
}

/* ═══════════════════════════ ATTACH BAR ═══════════════════════════ */
function _aicRenderAttachBar() {
    var bar = document.querySelector('#aicAttachBar');
    if (!bar) return;
    if (!_aic.attachedFiles.length) {
        bar.classList.add('aic-hidden');
        bar.innerHTML = '';
        return;
    }
    bar.classList.remove('aic-hidden');
    bar.innerHTML = _aic.attachedFiles.map(function (f, i) {
        var sizeKB = Math.round((f.size || 0) / 1024);
        return '<div class="aic-attach-chip">' +
            '<i class="fas fa-file-code"></i> ' +
            '<span class="aic-attach-name">' + _aicEsc(f.name) + '</span>' +
            '<span class="aic-attach-size">' + sizeKB + ' KB</span>' +
            '<button class="aic-attach-remove" onclick="window._aicRemoveFile(' + i + ')"><i class="fas fa-times"></i></button>' +
        '</div>';
    }).join('');
}

window._aicRemoveFile = function (idx) {
    _aic.attachedFiles.splice(idx, 1);
    _aicRenderAttachBar();
};

/* ═══════════════════════════ RAG INDEXING ═══════════════════════════════════ */
window._aicStartIndexing = function (dir) {
    _aicFetch('/api/aichat/rag/index', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory: dir || '' })
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.error) { showToast(d.error, 'error'); return; }
        showToast(t('Indeksowanie rozpoczęte'), 'success');
        _aicPollRagIndexing();
    }).catch(function () { showToast(t('Błąd indeksowania'), 'error'); });
};

function _aicPollRagIndexing() {
    var poll = setInterval(function () {
        _aicFetch('/api/aichat/rag/status').then(function (r) { return r.json(); }).then(function (d) {
            _aic.rag = d;
            var s = d.stats || {};
            var docs = s.document_files || 0;
            var photos = s.gallery_files || 0;
            var prog = d.progress || {};

            if (!d.indexing) {
                clearInterval(poll);
                showToast(t('Indeksowanie zakończone') + ' (' + docs + ' ' + t('dokumentów') + ', ' + photos + ' ' + t('zdjęć') + ')', 'success');
                // Refresh empty state badge if visible
                var badge = document.querySelector('.aic-rag-badge');
                if (badge) {
                    badge.innerHTML = '<i class="fas fa-brain"></i> ' + t('Baza wiedzy') + ': ' + docs + ' ' + t('dokumentów') + ', ' + photos + ' ' + t('zdjęć');
                    badge.className = 'aic-rag-badge';
                }
                var statsEl = document.querySelector('#aicRagStats');
                if (statsEl) statsEl.textContent = docs + ' ' + t('dokumentów') + ', ' + photos + ' ' + t('zdjęć');
                // Hide progress bar
                var wrap = document.querySelector('#aicRagProgressWrap');
                _aicSetHidden(wrap, true);
            } else {
                // Show and update progress bar
                var wrap = document.querySelector('#aicRagProgressWrap');
                _aicSetHidden(wrap, false);
                var pbar = document.querySelector('#aicRagProgress');
                var total = prog.total || 1;
                var indexed = prog.indexed || 0;
                var pct = Math.round(100 * indexed / total);
                if (pbar) {
                    pbar.style.width = pct + '%';
                    pbar.textContent = pct + '% (' + indexed + '/' + total + ')';
                }
                var statsEl = document.querySelector('#aicRagStats');
                if (statsEl) statsEl.textContent = t('Indeksowanie') + '... ' + indexed + '/' + total + ' — ' + (prog.current || '');
            }
        });
    }, 2000);
}

window._aicClearIndex = async function () {
    if (!await confirmDialog(t('Czy na pewno wyczyścić indeks RAG?'))) return;
    _aicFetch('/api/aichat/rag/clear', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            showToast(t('Indeks wyczyszczony'), 'success');
            _aic.rag = { documents: 0, photos: 0, stats: { document_files: 0, gallery_files: 0 } };
            var badge = document.querySelector('.aic-rag-badge');
            if (badge) {
                badge.className = 'aic-rag-badge aic-rag-empty';
                badge.innerHTML = '<i class="fas fa-brain"></i> ' +
                    t('Baza wiedzy pusta') + ' — <a href="#" onclick="window._aicStartIndexing();return false">' + t('Zindeksuj pliki') + '</a>';
            }
            var statsEl = document.querySelector('#aicRagStats');
            if (statsEl) statsEl.textContent = '0 ' + t('dokumentów') + ', 0 ' + t('zdjęć');
        });
};

window._aicToggleScheduler = function (enabled) {
    var action = enabled ? 'enable' : 'disable';
    _aicFetch('/api/aichat/rag/scheduler', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action })
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.error) { showToast(d.error, 'error'); return; }
        showToast(d.message, 'success');
        var intField = document.getElementById('aicSchedulerIntervalField');
        if (intField) intField.style.display = enabled ? '' : 'none';
        // Update rag state
        if (_aic.rag && _aic.rag.scheduler) _aic.rag.scheduler.active = enabled;
    }).catch(function () { showToast(t('Błąd'), 'error'); });
};

window._aicSetSchedulerInterval = function (interval) {
    _aicFetch('/api/aichat/rag/scheduler', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_interval', interval: interval })
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.error) { showToast(d.error, 'error'); return; }
        showToast(d.message, 'success');
    }).catch(function () { showToast(t('Błąd'), 'error'); });
};

/* ═══════════════════════════ CONVERSATION ACTIONS ═══════════════════════════ */
window._aicNewConv = function () {
    if (_aic.streaming) return;
    _aicFetch('/api/aichat/conversations', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (conv) {
            _aic.activeConv = conv;
            _aic.attachedFiles = [];
            _aic.convs.unshift({ id: conv.id, title: conv.title, message_count: 0 });
            var root = document.querySelector('.aic-root');
            if (root) _aicRender(root.parentElement);
        });
};

window._aicSelectConv = function (id) {
    if (_aic.streaming) return;
    _aicFetch('/api/aichat/conversations/' + id)
        .then(function (r) { return r.json(); })
        .then(function (conv) {
            _aic.activeConv = conv;
            _aic.attachedFiles = [];
            var root = document.querySelector('.aic-root');
            if (root) _aicRender(root.parentElement);
        });
};

window._aicDeleteConv = async function (id) {
    if (_aic.streaming) return;
    if (!await confirmDialog(t('Usunąć tę rozmowę?'))) return;
    _aicFetch('/api/aichat/conversations/' + id, { method: 'DELETE' })
        .then(function () {
            _aic.convs = _aic.convs.filter(function (c) { return c.id !== id; });
            if (_aic.activeConv && _aic.activeConv.id === id) _aic.activeConv = null;
            if (_aic.convOverrides && _aic.convOverrides[id]) delete _aic.convOverrides[id];
            var root = document.querySelector('.aic-root');
            if (root) _aicRender(root.parentElement);
        });
};

/* ═══════════════════════════ SEND MESSAGE ═══════════════════════════ */
function _aicSendMessage(msg, opts) {
    if (_aic.streaming) return;
    var input = document.querySelector('#aicInput');
    var text = (msg || (input ? input.value : '') || '').trim();
    if (!text) return;
    if (input && (!opts || !opts.keepInput)) {
        input.value = '';
        input.style.height = 'auto';
    }

    _aic.streaming = true;
    var sendBtn = document.querySelector('#aicSendBtn');
    if (sendBtn) sendBtn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i>';

    var convId = _aic.activeConv ? _aic.activeConv.id : null;
    if (opts && opts.forceNewConv) {
        convId = null;
        _aic.activeConv = null;
    }
    var overrides = {};
    var activeId = convId || (_aic.activeConv && _aic.activeConv.id) || null;
    if (activeId && _aic.convOverrides[activeId]) {
        Object.assign(overrides, _aic.convOverrides[activeId]);
    } else if (!activeId && _aic.pendingOverrides) {
        Object.assign(overrides, _aic.pendingOverrides);
    }
    if (opts && Object.prototype.hasOwnProperty.call(opts, 'ragEnabled')) {
        overrides.ragEnabled = opts.ragEnabled;
    }
    if (opts && typeof opts.providerOverride === 'string') {
        overrides.providerOverride = opts.providerOverride;
    }
    if (opts && typeof opts.modelOverride === 'string') {
        overrides.modelOverride = opts.modelOverride;
    }
    var filesPayload = _aic.attachedFiles.map(function (f) { return { path: f.path, content: f.content }; });
    if (opts && Array.isArray(opts.files)) filesPayload = opts.files;
    _aic.attachedFiles = [];
    _aicRenderAttachBar();

    // Optimistic UI
    if (!_aic.activeConv) {
        _aic.activeConv = { id: null, title: text.substring(0, 80), messages: [] };
    }
    var displayMsg = text;
    if (filesPayload.length) {
        var fnames = filesPayload.map(function (f) { return f.path.split('/').pop(); });
        displayMsg = '\u{1f4ce} ' + fnames.join(', ') + '\n\n' + text;
    }
    _aic.activeConv.messages.push({ role: 'user', content: displayMsg, timestamp: new Date().toISOString() });

    var container = document.querySelector('#aicMessages');
    _aicRenderMessages(document.querySelector('.aic-root'));

    // AI placeholder
    _aic.activeConv.messages.push({ role: 'assistant', content: '', timestamp: '' });
    var aiIdx = _aic.activeConv.messages.length - 1;

    if (container) {
        var aiDiv = document.createElement('div');
        aiDiv.className = 'aic-msg aic-msg-ai';
        aiDiv.id = 'aicStreamMsg';
        aiDiv.innerHTML = '<div class="aic-msg-avatar"><i class="fas fa-robot"></i></div>' +
            '<div class="aic-msg-bubble"><span class="aic-typing"><i class="fas fa-circle fa-xs"></i><i class="fas fa-circle fa-xs"></i><i class="fas fa-circle fa-xs"></i></span></div>';
        container.appendChild(aiDiv);
        container.scrollTop = container.scrollHeight;
    }

    var body = { conversation_id: convId, message: text, files: filesPayload };
    if (Object.prototype.hasOwnProperty.call(overrides, 'ragEnabled')) {
        body.rag_enabled = overrides.ragEnabled;
    }
    if (typeof overrides.providerOverride === 'string') {
        body.provider_override = overrides.providerOverride;
    }
    if (typeof overrides.modelOverride === 'string') {
        body.model = overrides.modelOverride;
    }
    var hasOverrides = Object.keys(overrides).length > 0;
    if (hasOverrides) {
        if (convId) {
            _aic.convOverrides[convId] = overrides;
        } else {
            _aic.pendingOverrides = overrides;
        }
    } else if (opts && opts.forceNewConv) {
        _aic.pendingOverrides = null;
    }

    _aicFetch('/api/aichat/chat', {
        method: 'POST',
        body: JSON.stringify(body),
    }).then(function (response) {
        if (!response.ok) {
            return response.json().then(function (d) { throw new Error(d.error || 'HTTP ' + response.status); });
        }
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var fullText = '';
        var sseBuffer = '';

        function processChunk(result) {
            if (result.done) { _aicStreamDone(fullText, aiIdx); return; }
            sseBuffer += decoder.decode(result.value, { stream: true });
            var lines = sseBuffer.split('\n');
            sseBuffer = lines.pop();
            lines.forEach(function (line) {
                line = line.trim();
                if (!line || !line.startsWith('data: ')) return;
                try {
                    var ev = JSON.parse(line.substring(6));
                    if (ev.type === 'meta' && ev.conversation_id) {
                        if (!convId) {
                            convId = ev.conversation_id;
                            _aic.activeConv.id = convId;
                            if (_aic.pendingOverrides) {
                                _aic.convOverrides[convId] = _aic.pendingOverrides;
                                _aic.pendingOverrides = null;
                            }
                        }
                        // Capture RAG sources for display
                        if (ev.rag_sources && ev.rag_sources.length) {
                            _aic.ragSources = ev.rag_sources;
                        } else {
                            _aic.ragSources = [];
                        }
                        // Show context trimmed warning
                        if (ev.context_trimmed) {
                            var trimNote = document.createElement('div');
                            trimNote.className = 'aic-context-trim';
                            trimNote.innerHTML = '<i class="fas fa-compress-alt"></i> ' + t('Kontekst skrócony — starsze wiadomości pominięte');
                            if (container) container.appendChild(trimNote);
                        }
                    } else if (ev.type === 'token') {
                        fullText += ev.content;
                        var bubble = document.querySelector('#aicStreamMsg .aic-msg-bubble');
                        if (bubble) { bubble.innerHTML = _aicMd(fullText); container.scrollTop = container.scrollHeight; }
                    } else if (ev.type === 'error') {
                        var bubble2 = document.querySelector('#aicStreamMsg .aic-msg-bubble');
                        if (bubble2) bubble2.innerHTML = '<span class="aic-error">' + _aicEsc(ev.error) + '</span>';
                    } else if (ev.type === 'done') {
                        _aicStreamDone(fullText, aiIdx);
                    }
                } catch (e) { /* skip */ }
            });
            return reader.read().then(processChunk);
        }
        return reader.read().then(processChunk);
    }).catch(function (err) {
        var bubble = document.querySelector('#aicStreamMsg .aic-msg-bubble');
        if (bubble) bubble.innerHTML = '<span class="aic-error">' + _aicEsc(err.message) + '</span>';
        _aicStreamDone('', aiIdx);
    });
}

window._aicSend = function () {
    _aicSendMessage();
};

function _aicStreamDone(fullText, aiIdx) {
    _aic.streaming = false;
    var sendBtn = document.querySelector('#aicSendBtn');
    if (sendBtn) sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i>';
    if (_aic.activeConv && _aic.activeConv.messages[aiIdx]) {
        _aic.activeConv.messages[aiIdx].content = fullText;
    }

    // Show RAG sources if any
    if (_aic.ragSources && _aic.ragSources.length) {
        var streamMsg = document.querySelector('#aicStreamMsg');
        if (streamMsg) {
            var srcHtml = '<div class="aic-rag-sources"><i class="fas fa-brain"></i> ' + t('Źródła') + ': ';
            srcHtml += _aic.ragSources.map(function (s) {
                var icon = s.type === 'gallery' ? 'fa-image' : 'fa-file-alt';
                return '<span class="aic-rag-source-chip"><i class="fas ' + icon + '"></i> ' + _aicEsc(s.name) + '</span>';
            }).join(' ');
            srcHtml += '</div>';
            streamMsg.querySelector('.aic-msg-bubble').insertAdjacentHTML('afterend', srcHtml);
        }
        _aic.ragSources = [];
    }

    _aicFetch('/api/aichat/conversations').then(function (r) { return r.json(); }).then(function (d) {
        _aic.convs = d;
        var root = document.querySelector('.aic-root');
        if (root) _aicRenderConvList(root);
    });
    var input = document.querySelector('#aicInput');
    if (input) input.focus();
}

/* ═══════════════════════════ CODE BLOCK ACTIONS ═══════════════════════════ */
window._aicCopyCode = function (blockId) {
    var block = document.getElementById(blockId);
    if (!block) return;
    var code = block.querySelector('code');
    if (!code) return;
    var text = code.textContent;
    navigator.clipboard.writeText(text).then(function () {
        if (typeof showToast === 'function') showToast('Skopiowano do schowka', 'success');
    });
};

window._aicApplyCode = function (blockId) {
    var block = document.getElementById(blockId);
    if (!block) return;
    var code = block.querySelector('code').textContent;
    var path = prompt(t('Podaj pełną ścieżkę pliku do zapisania:'), '');
    if (!path) return;
    _aicFetch('/api/aichat/files/write', {
        method: 'POST',
        body: JSON.stringify({ path: path, content: code }),
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) {
            if (typeof showToast === 'function') showToast('Zapisano: ' + path, 'success');
        } else {
            if (typeof showToast === 'function') showToast(t('Błąd: ') + (d.error || '?'), 'error');
        }
    }).catch(function () {
        if (typeof showToast === 'function') showToast(t('Błąd zapisu'), 'error');
    });
};

window._aicRunCode = function (blockId) {
    var block = document.getElementById(blockId);
    if (!block) return;
    var code = block.querySelector('code').textContent;
    var cmd = prompt('Komenda do uruchomienia:', code.split('\n')[0]);
    if (!cmd) return;

    block.querySelector('.aic-cb-actions').insertAdjacentHTML('beforeend',
        '<span class="aic-cb-running"><i class="fas fa-spinner fa-spin"></i></span>');

    _aicFetch('/api/aichat/exec', {
        method: 'POST',
        body: JSON.stringify({ command: cmd, cwd: (_aic.cfg && _aic.cfg.workspace) || '/home' }),
    }).then(function (r) { return r.json(); }).then(function (d) {
        var running = block.querySelector('.aic-cb-running');
        if (running) running.remove();

        var output = '';
        if (d.stdout) output += d.stdout;
        if (d.stderr) output += (output ? '\n' : '') + d.stderr;
        if (d.error) output += (output ? '\n' : '') + 'Error: ' + d.error;
        if (!output) output = t('(brak wyjścia)');

        var exitBadge = d.exit_code === 0
            ? '<span class="aic-term-ok">✓ exit 0</span>'
            : '<span class="aic-term-err">✗ exit ' + d.exit_code + '</span>';

        // Insert terminal output after the code block
        var termDiv = document.createElement('div');
        termDiv.className = 'aic-term-output';
        termDiv.innerHTML = '<div class="aic-term-header"><i class="fas fa-terminal"></i> Wynik ' + exitBadge + '</div>' +
            '<pre class="aic-term-pre">' + _aicEsc(output) + '</pre>';
        block.parentNode.insertBefore(termDiv, block.nextSibling);
    }).catch(function (err) {
        var running = block.querySelector('.aic-cb-running');
        if (running) running.remove();
        if (typeof showToast === 'function') showToast(t('Błąd: ') + err.message, 'error');
    });
};

/* ═══════════════════════════ FILE PICKER ═══════════════════════════ */
window._aicOpenFilePicker = function () {
    _aic.view = 'filepicker';
    _aic.fpPath = (_aic.cfg && _aic.cfg.workspace) || '/home';
    _aic.fpSelected = {};
    var root = document.querySelector('.aic-root');
    if (root) { _aicBrowse(_aic.fpPath); }
};

function _aicBrowse(path) {
    _aic.fpPath = path;
    _aic.fpLoading = true;
    var root = document.querySelector('.aic-root');
    if (root) _aicRenderFilePicker(root);

    _aicFetch('/api/aichat/files/browse?path=' + encodeURIComponent(path))
        .then(function (r) { return r.json(); })
        .then(function (d) {
            _aic.fpItems = d.items || [];
            _aic.fpPath = d.path || path;
            _aic.fpLoading = false;
            var root2 = document.querySelector('.aic-root');
            if (root2) _aicRenderFilePicker(root2);
        }).catch(function () {
            _aic.fpItems = [];
            _aic.fpLoading = false;
            var root2 = document.querySelector('.aic-root');
            if (root2) _aicRenderFilePicker(root2);
        });
}

function _aicRenderFilePicker(root) {
    var selCount = Object.keys(_aic.fpSelected).length;
    root.innerHTML =
        '<div class="aic-fp">' +
            '<div class="aic-fp-header">' +
                '<button class="aic-btn-icon" onclick="window._aicCloseFilePicker()"><i class="fas fa-arrow-left"></i></button>' +
                '<span class="aic-fp-title">' + t('Dołącz pliki') + '</span>' +
                '<span class="aic-fp-count">' + (selCount ? selCount + ' wybranych' : '') + '</span>' +
                '<button class="aic-btn-primary aic-fp-confirm" onclick="window._aicConfirmFiles()"' +
                    (selCount ? '' : ' disabled') + `><i class="fas fa-check"></i> ${t('Dołącz')}</button>` +
            '</div>' +
            '<div class="aic-fp-path">' +
                _aicBreadcrumb(_aic.fpPath) +
            '</div>' +
            '<div class="aic-fp-list">' +
                (_aic.fpLoading ? `<div class="aic-fp-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>` : _aicFileList()) +
            '</div>' +
        '</div>';
}

function _aicBreadcrumb(path) {
    var parts = path.split('/').filter(Boolean);
    var html = '<span class="aic-fp-crumb" onclick="window._aicBrowse(\'/\')">/</span>';
    var acc = '';
    parts.forEach(function (p) {
        acc += '/' + p;
        var full = acc;
        html += ' <span class="aic-fp-crumb" onclick="window._aicBrowse(\'' + _aicEsc(full) + '\')">' + _aicEsc(p) + '</span> /';
    });
    return html;
}

function _aicFileList() {
    if (!_aic.fpItems.length) return '<div class="aic-fp-empty">Pusty katalog</div>';
    // Parent dir link
    var parentPath = _aic.fpPath.replace(/\/[^/]+$/, '') || '/';
    var html = '';
    if (_aic.fpPath !== '/') {
        html += '<div class="aic-fp-item aic-fp-dir" onclick="window._aicBrowse(\'' + _aicEsc(parentPath) + '\')">' +
            '<i class="fas fa-level-up-alt"></i> <span>..</span></div>';
    }
    _aic.fpItems.forEach(function (item) {
        if (item.is_dir) {
            html += '<div class="aic-fp-item aic-fp-dir" onclick="window._aicBrowse(\'' + _aicEsc(item.path) + '\')">' +
                '<i class="fas fa-folder"></i> <span>' + _aicEsc(item.name) + '</span></div>';
        } else {
            var checked = _aic.fpSelected[item.path] ? ' checked' : '';
            var sizeKB = Math.round(item.size / 1024);
            var textClass = item.is_text ? '' : ' aic-fp-binary';
            html += '<div class="aic-fp-item aic-fp-file' + textClass + '">' +
                '<label><input type="checkbox" onchange="window._aicToggleFile(\'' + _aicEsc(item.path) + '\', this.checked)"' + checked +
                (item.is_text ? '' : ' disabled') + '>' +
                ' <i class="fas ' + (item.is_text ? 'fa-file-code' : 'fa-file') + '"></i> ' +
                _aicEsc(item.name) + '</label>' +
                '<span class="aic-fp-size">' + sizeKB + ' KB</span>' +
            '</div>';
        }
    });
    return html;
}

window._aicBrowse = function (path) { _aicBrowse(path); };

window._aicToggleFile = function (path, checked) {
    if (checked) _aic.fpSelected[path] = true;
    else delete _aic.fpSelected[path];
    // Update header count
    var cnt = document.querySelector('.aic-fp-count');
    var btn = document.querySelector('.aic-fp-confirm');
    var n = Object.keys(_aic.fpSelected).length;
    if (cnt) cnt.textContent = n ? n + ' wybranych' : '';
    if (btn) btn.disabled = !n;
};

window._aicConfirmFiles = function () {
    var paths = Object.keys(_aic.fpSelected);
    if (!paths.length) return;

    // Read selected files
    _aicFetch('/api/aichat/files/read', {
        method: 'POST',
        body: JSON.stringify({ paths: paths }),
    }).then(function (r) { return r.json(); }).then(function (d) {
        (d.files || []).forEach(function (f) {
            if (f.content !== undefined) {
                _aic.attachedFiles.push({
                    path: f.path,
                    name: f.path.split('/').pop(),
                    content: f.content,
                    size: f.size || f.content.length,
                });
            } else if (f.error) {
                if (typeof showToast === 'function') showToast(f.path.split('/').pop() + ': ' + f.error, 'error');
            }
        });
        _aic.view = 'chat';
        _aic.fpSelected = {};
        var root = document.querySelector('.aic-root');
        if (root) _aicRender(root.parentElement);
    }).catch(function () {
        if (typeof showToast === 'function') showToast(t('Błąd odczytu plików'), 'error');
    });
};

window._aicCloseFilePicker = function () {
    _aic.view = 'chat';
    _aic.fpSelected = {};
    var root = document.querySelector('.aic-root');
    if (root) _aicRender(root.parentElement);
};

/* ═══════════════════════════ MODEL LIBRARY ═══════════════════════════ */
var _mlData = { models: [], hardware: {}, disk: {}, models_path: '', download_status: {} };
var _mlPollTimer = null;

window._aicOpenModels = function () {
    _aic.view = 'models';
    var root = document.querySelector('.aic-root');
    if (root) {
        root.innerHTML = `<div class="aic-ml"><div class="aic-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie katalogu…')}</div></div>`;
        _mlLoadCatalog(function () { _aicRenderModels(root); });
    }
};

window._aicCloseModels = function () {
    _mlStopPoll();
    _aic.view = 'chat';
    var root = document.querySelector('.aic-root');
    if (root) _aicRender(root.parentElement);
};

function _mlLoadCatalog(cb) {
    _aicFetch('/api/aichat/models/catalog').then(function (r) { return r.json(); }).then(function (d) {
        _mlData = d;
        if (cb) cb();
    }).catch(function () {
        _mlData = { models: [], hardware: {}, disk: {}, models_path: '', download_status: {} };
        if (cb) cb();
    });
}

function _mlStartPoll() {
    _mlStopPoll();
    _mlPollTimer = setInterval(function () {
        _aicFetch('/api/aichat/models/download/status').then(function (r) { return r.json(); }).then(function (ds) {
            _mlData.download_status = ds;
            _mlUpdateProgress(ds);
            if (!ds.active) {
                if (ds.progress === 100 || ds.error) {
                    _mlStopPoll();
                    _mlLoadCatalog(function () {
                        var root = document.querySelector('.aic-root');
                        if (root && _aic.view === 'models') _aicRenderModels(root);
                    });
                }
            }
        });
    }, 1000);
}

function _mlStopPoll() {
    if (_mlPollTimer) { clearInterval(_mlPollTimer); _mlPollTimer = null; }
}

function _mlUpdateProgress(ds) {
    var bar = document.getElementById('mlProgressBar');
    var txt = document.getElementById('mlProgressTxt');
    var pctEl = document.getElementById('mlProgressPct');
    var pct = Math.round(ds.progress || 0);
    if (bar) bar.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';
    if (txt) {
        var parts = [];
        if (ds.status) parts.push(ds.status);
        if (ds.speed) parts.push(ds.speed);
        txt.textContent = parts.join('  ');
    }
    if (ds.error) {
        if (txt) txt.textContent = ds.error;
        if (bar) bar.style.background = '#ef4444';
        if (pctEl) pctEl.textContent = t('Błąd');
    }
}

function _aicRenderModels(root) {
    var hw = _mlData.hardware || {};
    var disk = _mlData.disk || {};
    var ds = _mlData.download_status || {};
    var models = _mlData.models || [];
    var deps = _aic.deps || null;

    var hwHtml =
        '<div class="ml-hw">' +
            '<div class="ml-hw-badge"><i class="fas fa-memory"></i> RAM: ' + (hw.ram_total_gb || '?') + ' GB' +
                '<span class="ml-hw-sub"> (' + (hw.ram_available_gb || '?') + ' GB wolne)</span></div>' +
            (hw.has_gpu
                ? '<div class="ml-hw-badge ml-hw-gpu"><i class="fas fa-microchip"></i> GPU: ' +
                    (hw.gpus && hw.gpus[0]
                        ? hw.gpus[0].name + (hw.gpus[0].vram_total_gb > 0 ? ' — ' + hw.gpus[0].vram_total_gb + ' GB VRAM' : t(' (RAM współdzielony)'))
                        : (hw.vram_total_gb > 0 ? hw.vram_total_gb + ' GB' : 'wykryty')) + '</div>'
                : '<div class="ml-hw-badge ml-hw-nogpu"><i class="fas fa-microchip"></i> Brak GPU — tylko CPU</div>') +
            '<div class="ml-hw-badge"><i class="fas fa-hdd"></i> Dysk: ' + (disk.free_gb || '?') + ' GB wolne z ' + (disk.total_gb || '?') + ' GB</div>' +
        '</div>';

    var progressHtml = '';
    if (ds.active) {
        var pct = Math.round(ds.progress || 0);
        progressHtml =
            '<div class="ml-dl-progress">' +
                '<div class="ml-dl-bar"><div class="ml-dl-fill" id="mlProgressBar"></div></div>' +
                '<div class="ml-dl-info">' +
                    '<span class="ml-dl-pct" id="mlProgressPct">' + pct + '%</span>' +
                    '<span id="mlProgressTxt">' + _aicEsc(ds.status || '') + (ds.speed ? '  ' + _aicEsc(ds.speed) : '') + '</span>' +
                    '<button class="aic-btn-icon ml-dl-cancel" onclick="window._mlCancelDownload()" title="Anuluj"><i class="fas fa-times"></i></button>' +
                '</div>' +
            '</div>';
        _mlStartPoll();
    }

    var pathHtml =
        '<div class="ml-path-row">' +
            '<label><i class="fas fa-folder-open"></i> ' + t('Ścieżka modeli:') + '</label>' +
            '<input type="text" id="mlModelsPath" value="' + _aicEsc(_mlData.models_path || '') + '" class="ml-path-input">' +
            '<button class="aic-btn-icon" onclick="window._mlSetPath()" title="' + t('Zmień') + '"><i class="fas fa-check"></i></button>' +
        '</div>';

    var depsWarnHtml = '';
    if (deps && deps.huggingface_hub === false) {
        depsWarnHtml =
            '<div class="aic-field-inline-hint aic-warn">' +
                '<i class="fas fa-exclamation-triangle"></i> ' + t('Brak biblioteki huggingface_hub - pobieranie modeli może nie działać.') +
                '<button class="aic-btn-secondary ml-deps-action" onclick="window._mlInstallDeps()">' +
                    '<i class="fas fa-wrench"></i> ' + t('Napraw zależności') +
                '</button>' +
            '</div>';
    }

    var customHtml =
        '<div class="ml-custom-row">' +
            '<input type="text" id="mlCustomUrl" placeholder="URL lub USER/REPO/PLIK.gguf z Hugging Face" class="ml-custom-input">' +
            '<button class="aic-btn-primary ml-custom-btn" onclick="window._mlAddCustom()"><i class="fas fa-plus"></i> Dodaj model</button>' +
        '</div>';

    var cardsHtml = '';
    if (!models.length) {
        cardsHtml = '<div class="ml-empty"><i class="fas fa-box-open"></i><p>Katalog modeli jest pusty</p></div>';
    } else {
        cardsHtml = '<div class="ml-grid">';
        models.forEach(function (m) {
        var statusCls = m.status === 'recommended' ? 'ml-st-rec' : m.status === 'possible' ? 'ml-st-pos' : m.status === 'unsupported' ? 'ml-st-heavy' : 'ml-st-heavy';
        var statusIcon = m.status === 'recommended' ? 'fa-check-circle' : m.status === 'possible' ? 'fa-exclamation-circle' : m.status === 'unsupported' ? 'fa-ban' : 'fa-times-circle';
        var statusTone = m.status === 'recommended' ? 'good' : m.status === 'possible' ? 'medium' : 'bad';
        var familyClass = 'ml-family-' + _aicFamilyKey(m.family);

            var badges = '';
            if (m.use_cases) {
                m.use_cases.forEach(function (uc) {
                    var ucLabel = { chat: 'Chat', code: 'Kod', reasoning: 'Reasoning', math: 'Matematyka', multilingual: t('Wielojęz.'), 'long-context': t('Długi kontekst') }[uc] || uc;
                    badges += '<span class="ml-uc-badge">' + ucLabel + '</span>';
                });
            }

            var actionBtn = '';
            if (m.unsupported) {
                actionBtn = '<button class="ml-btn ml-btn-dl" disabled title="' + _aicEsc(m.unsupported_reason || '') + '"><i class="fas fa-ban"></i> ' + t('Nieobsługiwany') + '</button>';
                if (m.downloaded) {
                    actionBtn += '<button class="ml-btn ml-btn-del" onclick="window._mlDeleteModel(\'' + m.id + '\')"><i class="fas fa-trash"></i></button>';
                }
            } else if (m.downloaded) {
                if (m.active) {
                    actionBtn =
                        '<button class="ml-btn ml-btn-active" disabled><i class="fas fa-check"></i> Aktywny</button>' +
                        '<button class="ml-btn ml-btn-del" onclick="window._mlDeleteModel(\'' + m.id + '\')"><i class="fas fa-trash"></i></button>';
                } else {
                    actionBtn =
                        '<button class="ml-btn ml-btn-activate" onclick="window._mlActivateModel(\'' + m.id + '\')"><i class="fas fa-play"></i> Aktywuj</button>' +
                        '<button class="ml-btn ml-btn-del" onclick="window._mlDeleteModel(\'' + m.id + '\')"><i class="fas fa-trash"></i></button>';
                }
            } else if (ds.active && ds.model_id === m.id) {
                actionBtn = '<button class="ml-btn ml-btn-dl" disabled><i class="fas fa-spinner fa-spin"></i> Pobieranie…</button>';
            } else {
                actionBtn = '<button class="ml-btn ml-btn-dl" onclick="window._mlDownloadModel(\'' + m.id + '\')"' +
                    (ds.active ? ' disabled' : '') + '><i class="fas fa-download"></i> Pobierz (' + (m.size_gb || '?') + ' GB)</button>';
            }

            var customBadge = m.custom
                ? '<button class="ml-btn ml-btn-custom-rm" onclick="event.stopPropagation();window._mlRemoveCustom(\'' + m.id + '\')" title="' + t('Usuń z katalogu') + '"><i class="fas fa-times"></i></button>'
                : '';

            cardsHtml +=
                '<div class="ml-card ' + statusCls + (m.downloaded ? ' ml-card-dl' : '') + (m.active ? ' ml-card-active' : '') + '">' +
                    '<div class="ml-card-head">' +
            '<div class="ml-card-family ' + familyClass + '">' + _aicEsc(m.family) + '</div>' +
            '<div class="ml-card-status ' + statusCls + ' aic-status-' + statusTone + '"><i class="fas ' + statusIcon + '"></i> ' + _aicEsc(m.status_label || '') + '</div>' +
                        customBadge +
                    '</div>' +
                    '<div class="ml-card-name">' + _aicEsc(m.name) + '</div>' +
                    '<div class="ml-card-meta">' +
                        '<span class="ml-meta-item"><i class="fas fa-microchip"></i> ' + _aicEsc(m.params) + '</span>' +
                        '<span class="ml-meta-item"><i class="fas fa-compress-alt"></i> ' + _aicEsc(m.quant) + '</span>' +
                        '<span class="ml-meta-item"><i class="fas fa-hdd"></i> ' + (m.size_gb || '?') + ' GB</span>' +
                        '<span class="ml-meta-item"><i class="fas fa-memory"></i> ' + (m.ram_required_gb || '?') + ' GB RAM</span>' +
                        (m.context_length ? '<span class="ml-meta-item"><i class="fas fa-text-width"></i> ' + (m.context_length >= 1024 ? Math.round(m.context_length / 1024) + 'K' : m.context_length) + ' ctx</span>' : '') +
                    '</div>' +
                    '<div class="ml-card-desc">' + _aicEsc(m.description) + '</div>' +
                    '<div class="ml-card-badges">' + badges + '</div>' +
                    '<div class="ml-card-gpu"><i class="fas fa-tv"></i> ' + _aicEsc(m.gpu_label || '') + '</div>' +
                    '<div class="ml-card-actions">' + actionBtn + '</div>' +
                '</div>';
        });
        cardsHtml += '</div>';
    }

    root.innerHTML =
        '<div class="aic-ml">' +
            '<div class="ml-header">' +
                '<button class="aic-btn-icon" onclick="window._aicCloseModels()"><i class="fas fa-arrow-left"></i></button>' +
                '<span class="ml-title"><i class="fas fa-cube"></i> Biblioteka modeli</span>' +
                '<button class="aic-btn-icon" onclick="window._mlRefresh()" title="' + t('Odśwież') + '"><i class="fas fa-sync-alt"></i></button>' +
            '</div>' +
            hwHtml +
            progressHtml +
            depsWarnHtml +
            pathHtml +
            customHtml +
            cardsHtml +
        '</div>';
}

window._mlRefresh = function () {
    var root = document.querySelector('.aic-root');
    if (root) root.innerHTML = `<div class="aic-ml"><div class="aic-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Odświeżanie…')}</div></div>`;
    _mlLoadCatalog(function () {
        var root2 = document.querySelector('.aic-root');
        if (root2 && _aic.view === 'models') _aicRenderModels(root2);
    });
};

window._mlDownloadModel = function (modelId) {
    _aicFetch('/api/aichat/models/download', {
        method: 'POST',
        body: JSON.stringify({ model_id: modelId }),
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.error) {
            if ((d.error || '').indexOf('huggingface_hub') !== -1) {
                if (typeof showToast === 'function') showToast(t('Brakuje zależności do pobierania modeli. Uruchamiam naprawę...'), 'warning');
                window._mlInstallDeps();
                return;
            }
            if (typeof showToast === 'function') showToast(d.error, 'error');
        } else {
            if (typeof showToast === 'function') showToast(t('Pobieranie rozpoczęte'), 'success');
            _mlData.download_status = { active: true, model_id: modelId, progress: 0, status: 'Rozpoczynanie…' };
            _mlStartPoll();
            var root = document.querySelector('.aic-root');
            if (root && _aic.view === 'models') _aicRenderModels(root);
        }
    }).catch(function (err) {
        if (typeof showToast === 'function') showToast(t('Błąd: ') + err.message, 'error');
    });
};

window._mlInstallDeps = function () {
    _aicFetch('/api/aichat/install', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (d && d.error) {
                if (typeof showToast === 'function') showToast(d.error, 'error');
                return;
            }
            if (typeof showToast === 'function') showToast(t('Naprawa zależności uruchomiona. Spróbuj pobrać model ponownie za chwilę.'), 'success');
        })
        .catch(function (err) {
            if (typeof showToast === 'function') showToast(t('Błąd naprawy zależności: ') + err.message, 'error');
        });
};

window._mlCancelDownload = function () {
    _aicFetch('/api/aichat/models/download/cancel', { method: 'POST' }).then(function () {
        _mlStopPoll();
        if (typeof showToast === 'function') showToast('Anulowano pobieranie', 'info');
        window._mlRefresh();
    });
};

window._mlDeleteModel = async function (modelId) {
    if (!await confirmDialog(t('Usunąć pobrany model? Plik GGUF zostanie usunięty z dysku.'))) return;
    _aicFetch('/api/aichat/models/' + encodeURIComponent(modelId), { method: 'DELETE' })
        .then(function (r) { return r.json(); }).then(function (d) {
            if (d.error) {
                if (typeof showToast === 'function') showToast(d.error, 'error');
            } else {
                if (typeof showToast === 'function') showToast(t('Model usunięty'), 'success');
                window._mlRefresh();
            }
        });
};

window._mlActivateModel = function (modelId) {
    _aicFetch('/api/aichat/models/active', {
        method: 'POST',
        body: JSON.stringify({ model_id: modelId }),
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.error) {
            if (typeof showToast === 'function') showToast(d.error, 'error');
        } else {
            var provMsg = d.provider === 'local'
                ? t('Model aktywowany — czat przełączony na lokalny model')
                : 'Model aktywowany';
            if (typeof showToast === 'function') showToast(provMsg, 'success');
            window._mlRefresh();
        }
    });
};

window._mlSetPath = function () {
    var input = document.getElementById('mlModelsPath');
    if (!input) return;
    var path = input.value.trim();
    if (!path) return;
    _aicFetch('/api/aichat/models/path', {
        method: 'POST',
        body: JSON.stringify({ path: path }),
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.error) {
            if (typeof showToast === 'function') showToast(d.error, 'error');
        } else {
            if (typeof showToast === 'function') showToast(t('Ścieżka modeli zmieniona'), 'success');
            window._mlRefresh();
        }
    });
};

window._mlAddCustom = function () {
    var input = document.getElementById('mlCustomUrl');
    if (!input) return;
    var url = input.value.trim();
    if (!url) return;
    _aicFetch('/api/aichat/models/custom', {
        method: 'POST',
        body: JSON.stringify({ url: url }),
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.error) {
            if (typeof showToast === 'function') showToast(d.error, 'error');
        } else {
            input.value = '';
            if (typeof showToast === 'function') showToast('Model dodany do katalogu', 'success');
            window._mlRefresh();
        }
    });
};

window._mlRemoveCustom = async function (modelId) {
    if (!await confirmDialog(t('Usunąć ten model z katalogu?'))) return;
    _aicFetch('/api/aichat/models/custom/' + encodeURIComponent(modelId), { method: 'DELETE' })
        .then(function (r) { return r.json(); }).then(function (d) {
            if (d.error) {
                if (typeof showToast === 'function') showToast(d.error, 'error');
            } else {
                if (typeof showToast === 'function') showToast(t('Model usunięty z katalogu'), 'success');
                window._mlRefresh();
            }
        });
};



/* ═══════════════════════════ SETUP WIZARD ═══════════════════════════ */

window._aicOpenWizard = function () {
    _aic.view = 'wizard';
    _aic.wizardStep = 0;
    _aic.wizardHw = null;
    _aic.wizardBench = null;
    _aic.wizardRecModel = null;
    var root = document.querySelector('.aic-root');
    if (root) _aicRenderWizard(root);
};

window._aicCloseWizard = function () {
    _aic.view = 'chat';
    _aic.wizardStep = 0;
    var root = document.querySelector('.aic-root');
    if (root) _aicRender(root.parentElement);
};

function _aicRenderWizard(root) {
    var step = _aic.wizardStep;
    var steps = [
        { icon: 'fa-microchip', label: t('Sprzęt') },
        { icon: 'fa-tachometer-alt', label: t('Benchmark') },
        { icon: 'fa-cube', label: t('Model') },
        { icon: 'fa-check-circle', label: t('Gotowe') },
    ];
    var stepsHtml = '<div class="aic-wiz-steps">';
    steps.forEach(function (s, i) {
        var cls = i < step ? 'aic-wiz-step-done' : i === step ? 'aic-wiz-step-active' : 'aic-wiz-step-pending';
        stepsHtml += '<div class="aic-wiz-step ' + cls + '"><i class="fas ' + s.icon + '"></i><span>' + s.label + '</span></div>';
        if (i < steps.length - 1) stepsHtml += '<div class="aic-wiz-connector"></div>';
    });
    stepsHtml += '</div>';

    var bodyHtml = '';
    if (step === 0) bodyHtml = _aicWizStep0();
    else if (step === 1) bodyHtml = _aicWizStepBench();
    else if (step === 2) bodyHtml = _aicWizStepModel();
    else bodyHtml = _aicWizStep3();

    root.innerHTML =
        '<div class="aic-wizard">' +
            '<div class="aic-wiz-header">' +
                '<i class="fas fa-magic"></i> ' + t('Kreator konfiguracji AI') +
                '<button class="aic-btn-icon aic-wiz-close" onclick="window._aicCloseWizard()" title="' + t('Zamknij') + '"><i class="fas fa-times"></i></button>' +
            '</div>' +
            stepsHtml +
            '<div class="aic-wiz-body">' + bodyHtml + '</div>' +
        '</div>';
    var downloadStatus = (_aic.wizardRecModel && _aic.wizardRecModel.download_status) || null;
    if (downloadStatus && downloadStatus.active) {
        _aicUpdateWizardDlBar(downloadStatus);
    }
}

/* Step 0: Hardware Discovery */
function _aicWizStep0() {
    if (!_aic.wizardHw) {
        // Start hardware discovery
        _aicFetch('/api/aichat/hardware').then(function (r) { return r.json(); }).then(function (d) {
            _aic.wizardHw = d;
            var root = document.querySelector('.aic-root');
            if (root) _aicRenderWizard(root);
        }).catch(function () {
            _aic.wizardHw = { error: true };
            var root = document.querySelector('.aic-root');
            if (root) _aicRenderWizard(root);
        });
        return '<div class="aic-wiz-loading"><i class="fas fa-spinner fa-spin fa-2x"></i><p>' + t('Skanowanie sprzętu…') + '</p></div>';
    }

    if (_aic.wizardHw.error) {
        return '<div class="aic-wiz-error"><i class="fas fa-exclamation-triangle"></i> ' + t('Nie udało się wykryć sprzętu') + '</div>' +
            '<button class="aic-btn-primary" onclick="_aic.wizardHw=null;_aicRenderWizard(document.querySelector(\'.aic-root\'))"><i class="fas fa-redo"></i> ' + t('Ponów') + '</button>';
    }

    var hw = _aic.wizardHw.hardware || {};
    var cpu = hw.cpu || {};
    var disk = _aic.wizardHw.disk || {};
    var tier = _aic.wizardHw.tier || {};
    var tierClass = _aicTierClassFromRecord(tier);

    var gpuHtml = '';
    if (hw.has_gpu && hw.gpus && hw.gpus.length) {
        hw.gpus.forEach(function (g) {
            gpuHtml += '<div class="aic-wiz-hw-item"><i class="fas fa-tv"></i> <strong>GPU:</strong> ' + _aicEsc(g.name) +
                (g.vram_total_gb > 0 ? ' — ' + g.vram_total_gb + ' GB VRAM' : ' (' + t('RAM współdzielony') + ')') + '</div>';
        });
    } else {
        gpuHtml = '<div class="aic-wiz-hw-item aic-wiz-hw-na"><i class="fas fa-tv"></i> <strong>GPU:</strong> ' + t('Brak dedykowanego GPU — tylko CPU') + '</div>';
    }

    var npuHtml = '';
    if (hw.npus && hw.npus.length) {
        hw.npus.forEach(function (n) {
            npuHtml += '<div class="aic-wiz-hw-item"><i class="fas fa-brain"></i> <strong>NPU:</strong> ' + _aicEsc(n.name) + '</div>';
        });
    }

    var featHtml = '';
    if (cpu.avx2) featHtml += '<span class="aic-wiz-feat aic-wiz-feat-ok">AVX2</span>';
    if (cpu.avx512) featHtml += '<span class="aic-wiz-feat aic-wiz-feat-ok">AVX-512</span>';
    if (cpu.vnni) featHtml += '<span class="aic-wiz-feat aic-wiz-feat-ok">VNNI</span>';
    if (cpu.sse42) featHtml += '<span class="aic-wiz-feat aic-wiz-feat-ok">SSE4.2</span>';
    if (cpu.neon) featHtml += '<span class="aic-wiz-feat aic-wiz-feat-ok">NEON</span>';
    if (!featHtml) featHtml = '<span class="aic-wiz-feat">' + t('Podstawowe') + '</span>';

    return '<h3><i class="fas fa-server"></i> ' + t('Wykryty sprzęt') + '</h3>' +
        '<div class="aic-wiz-hw">' +
            '<div class="aic-wiz-hw-item"><i class="fas fa-microchip"></i> <strong>CPU:</strong> ' + _aicEsc(cpu.cpu_name || t('Nieznany')) +
                ' (' + (cpu.cores_physical || '?') + ' ' + t('rdzeni') + ' / ' + (cpu.cores_logical || '?') + ' ' + t('wątków') + ')</div>' +
            '<div class="aic-wiz-hw-item"><i class="fas fa-memory"></i> <strong>RAM:</strong> ' + (hw.ram_total_gb || '?') + ' GB' +
                ' <span class="aic-wiz-hw-sub">(' + (hw.ram_available_gb || '?') + ' GB ' + t('wolne') + ')</span></div>' +
            gpuHtml + npuHtml +
            '<div class="aic-wiz-hw-item"><i class="fas fa-hdd"></i> <strong>' + t('Dysk') + ':</strong> ' + (disk.free_gb || '?') + ' GB ' + t('wolne') +
                ' / ' + (disk.total_gb || '?') + ' GB</div>' +
            '<div class="aic-wiz-hw-item"><i class="fas fa-cogs"></i> <strong>' + t('Instrukcje CPU') + ':</strong> ' + featHtml + '</div>' +
            '<div class="aic-wiz-hw-item"><i class="fas fa-compress-alt"></i> <strong>' + t('Zalecana kwantyzacja') + ':</strong> ' +
                _aicEsc(cpu.recommended_quant || 'Q4_K_M') + '</div>' +
        '</div>' +
        '<div class="aic-wiz-tier ' + tierClass + '">' +
            '<i class="fas ' + (tier.icon || 'fa-circle') + '"></i> ' +
            '<strong>' + t('Szacowany profil') + ': ' + _aicEsc(tier.name || '?') + '</strong>' +
            ' — ' + _aicEsc(tier.description || '') +
            ' (' + t('Zalecane modele') + ': ' + _aicEsc(tier.recommended_params || '?') + ')' +
        '</div>' +
        '<div class="aic-wiz-hw-item"><i class="fas fa-users-cog"></i> <strong>' + t('Wątki inferencji') + ':</strong> ' +
            (cpu.optimal_threads || '?') + ' (' + t('reguła N-1 — 1 rdzeń zarezerwowany dla systemu') + ')</div>' +
        '<div class="aic-wiz-actions">' +
            '<button class="aic-btn-primary" onclick="window._aicWizNext()"><i class="fas fa-arrow-right"></i> ' + t('Dalej — benchmark') + '</button>' +
        '</div>';
}

/* Step 1: Auto-Benchmark — downloads smallest model if needed, runs benchmark, estimates TPS for all models */
function _aicWizStepBench() {
    // Auto-start benchmark on entering this step
    if (!_aic.wizardBench) {
        _aic.wizardBench = { running: true, phase: 'starting' };
        var root = document.querySelector('.aic-root');

        _aicFetch('/api/aichat/models/benchmark/auto', { method: 'POST', body: JSON.stringify({}) })
            .then(function (r) { return r.json(); }).then(function (d) {
                if (d.error) {
                    _aic.wizardBench = { error: d.error, running: false };
                } else {
                    _aic.wizardBench = d;
                    _aic.wizardBench.running = false;
                }
                var root2 = document.querySelector('.aic-root');
                if (root2) _aicRenderWizard(root2);
            }).catch(function (err) {
                _aic.wizardBench = { error: err.message || t('Błąd połączenia'), running: false };
                var root2 = document.querySelector('.aic-root');
                if (root2) _aicRenderWizard(root2);
            });

        return '<h3><i class="fas fa-tachometer-alt"></i> ' + t('Benchmark wydajności') + '</h3>' +
            '<div class="aic-wiz-loading">' +
                '<i class="fas fa-spinner fa-spin fa-2x"></i>' +
                '<p>' + t('Automatyczny benchmark — pobieranie modelu testowego i pomiar wydajności…') + '</p>' +
                '<p class="aic-wiz-hint">' + t('To może potrwać 1-2 minuty.') + '</p>' +
            '</div>';
    }

    if (_aic.wizardBench.running) {
        return '<h3><i class="fas fa-tachometer-alt"></i> ' + t('Benchmark wydajności') + '</h3>' +
            '<div class="aic-wiz-loading">' +
                '<i class="fas fa-spinner fa-spin fa-2x"></i>' +
                '<p>' + t('Trwa benchmark — pomiar szybkości inferencji…') + '</p>' +
            '</div>';
    }

    var b = _aic.wizardBench;
    if (b.error) {
        return '<h3><i class="fas fa-tachometer-alt"></i> ' + t('Benchmark') + '</h3>' +
            '<div class="aic-wiz-error"><i class="fas fa-exclamation-triangle"></i> ' + _aicEsc(b.error) + '</div>' +
            '<div class="aic-wiz-actions">' +
                '<button class="aic-btn-secondary" onclick="window._aicWizPrev()"><i class="fas fa-arrow-left"></i> ' + t('Wstecz') + '</button>' +
                '<button class="aic-btn-primary" onclick="_aic.wizardBench=null;_aicRenderWizard(document.querySelector(\'.aic-root\'))"><i class="fas fa-redo"></i> ' + t('Ponów') + '</button>' +
                '<button class="aic-btn-secondary" onclick="_aic.wizardBench={skipped:true};window._aicWizNext()">' + t('Pomiń') + ' <i class="fas fa-forward"></i></button>' +
            '</div>';
    }

    // Benchmark results
    var tier = b.tier || {};
    var tierClass = _aicTierClassFromRecord(tier);
    return '<h3><i class="fas fa-tachometer-alt"></i> ' + t('Wyniki benchmarku') + '</h3>' +
        '<p class="aic-wiz-hint">' + t('Testowano na modelu') + ': <strong>' + _aicEsc(b.ref_model_name || b.model_id || '?') + '</strong> (' + _aicEsc(b.ref_params || '?') + ')</p>' +
        '<div class="aic-wiz-bench-results">' +
            '<div class="aic-wiz-bench-metric">' +
                '<div class="aic-wiz-bench-value">' + (b.tps || 0) + '</div>' +
                '<div class="aic-wiz-bench-label">' + t('tok/s (TPS)') + '</div>' +
            '</div>' +
            '<div class="aic-wiz-bench-metric">' +
                '<div class="aic-wiz-bench-value">' + ((b.ttft || 0) * 1000).toFixed(0) + ' ms</div>' +
                '<div class="aic-wiz-bench-label">' + t('Time to First Token') + '</div>' +
            '</div>' +
            '<div class="aic-wiz-bench-metric">' +
                '<div class="aic-wiz-bench-value">' + (b.tokens_generated || 0) + '</div>' +
                '<div class="aic-wiz-bench-label">' + t('Tokenów') + '</div>' +
            '</div>' +
        '</div>' +
        '<div class="aic-wiz-tier ' + tierClass + '">' +
            '<i class="fas ' + (tier.icon || 'fa-circle') + '"></i> ' +
            '<strong>' + t('Profil wydajności') + ': ' + _aicEsc(tier.name || '?') + '</strong>' +
            ' — ' + _aicEsc(tier.description || '') +
        '</div>' +
        '<div class="aic-wiz-actions">' +
            '<button class="aic-btn-secondary" onclick="window._aicWizPrev()"><i class="fas fa-arrow-left"></i> ' + t('Wstecz') + '</button>' +
            '<button class="aic-btn-primary" onclick="window._aicWizNext()"><i class="fas fa-arrow-right"></i> ' + t('Dalej — wybór modelu') + '</button>' +
        '</div>';
}

/* Step 2: Model selection with path picker and download progress */
function _aicWizStepModel() {
    if (!_aic.wizardRecModel) {
        Promise.all([
            _aicFetch('/api/aichat/models/catalog').then(function (r) { return r.json(); }),
            _aicFetch('/api/aichat/models/path').then(function (r) { return r.json(); }),
        ]).then(function (results) {
            _aic.wizardRecModel = results[0];
            _aic.wizardModelsPath = (results[1] || {}).path || '/opt/ethos/data/models';
            _aic.wizardDisk = (results[1] || {}).disk || {};
            var root = document.querySelector('.aic-root');
            if (root) _aicRenderWizard(root);
        });
        return '<div class="aic-wiz-loading"><i class="fas fa-spinner fa-spin fa-2x"></i><p>' + t('Ładowanie katalogu modeli…') + '</p></div>';
    }

    // Path picker
    var disk = _aic.wizardDisk || {};
    var pathHtml =
        '<div class="aic-wiz-path">' +
            '<h4><i class="fas fa-folder-open"></i> ' + t('Gdzie przechowywać modele?') + '</h4>' +
            '<div class="aic-wiz-path-row">' +
                '<input type="text" id="wizModelsPath" class="ml-path-input" value="' + _aicEsc(_aic.wizardModelsPath || '') + '">' +
                '<button class="aic-btn-primary" onclick="window._aicWizSetPath()"><i class="fas fa-check"></i></button>' +
            '</div>' +
            '<div class="aic-field-hint">' +
                '<i class="fas fa-hdd"></i> ' + (disk.free_gb || '?') + ' GB ' + t('wolne') + ' / ' + (disk.total_gb || '?') + ' GB ' + t('łącznie') +
            '</div>' +
        '</div>';

    // Sort: use estimated TPS from benchmark if available, else RAM descending
    var benchEstimates = (_aic.wizardBench && _aic.wizardBench.model_estimates) || {};
    var models = (_aic.wizardRecModel.models || []).filter(function (m) { return m.status === 'recommended' || m.status === 'possible'; });
    if (!models.length) models = _aic.wizardRecModel.models || [];
    models.sort(function (a, b) {
        if (a.status === 'recommended' && b.status !== 'recommended') return -1;
        if (b.status === 'recommended' && a.status !== 'recommended') return 1;
        // If we have TPS estimates, sort by estimated TPS (models with ≥5 TPS first, then by size desc)
        var aEst = benchEstimates[a.id] || 0;
        var bEst = benchEstimates[b.id] || 0;
        var aUsable = aEst >= 5 ? 1 : 0;
        var bUsable = bEst >= 5 ? 1 : 0;
        if (aUsable !== bUsable) return bUsable - aUsable;
        return (b.ram_required_gb || 0) - (a.ram_required_gb || 0);
    });

    // Auto-select: biggest model with ≥5 estimated TPS, or first recommended non-downloaded
    var topRecId = null;
    for (var mi = 0; mi < models.length; mi++) {
        var est = benchEstimates[models[mi].id] || 0;
        if (models[mi].status === 'recommended' && !models[mi].downloaded && est >= 5) {
            topRecId = models[mi].id; break;
        }
    }
    if (!topRecId) {
        for (var mi2 = 0; mi2 < models.length; mi2++) {
            if (models[mi2].status === 'recommended' && !models[mi2].downloaded) { topRecId = models[mi2].id; break; }
        }
    }

    var ds = _aic.wizardRecModel.download_status || {};

    // Download progress bar (shown during download)
    var progressHtml = '';
    if (ds.active) {
        var pct = Math.round(ds.progress || 0);
        progressHtml =
            '<div class="aic-wiz-dl-progress">' +
                '<div class="aic-wiz-dl-info">' +
                    '<span><i class="fas fa-download"></i> ' + t('Pobieranie modelu…') + '</span>' +
                    '<span class="aic-wiz-dl-pct">' + pct + '%</span>' +
                '</div>' +
                '<div class="ml-dl-bar"><div class="ml-dl-fill" id="wizDlBar" style="width:' + pct + '%"></div></div>' +
                '<div class="aic-wiz-dl-detail">' +
                    '<span id="wizDlStatus">' + _aicEsc(ds.status || '') + '</span>' +
                    (ds.speed ? '<span id="wizDlSpeed">' + _aicEsc(ds.speed) + '</span>' : '') +
                '</div>' +
                '<button class="aic-btn-secondary" onclick="window._aicWizCancelDl()"><i class="fas fa-times"></i> ' + t('Anuluj') + '</button>' +
            '</div>';
    }

    var cardsHtml = '';
    models.slice(0, 12).forEach(function (m) {
        var statusIcon = m.status === 'recommended' ? 'fa-check-circle' : m.status === 'possible' ? 'fa-exclamation-circle' : 'fa-times-circle';
        var statusTone = m.status === 'recommended' ? 'good' : m.status === 'possible' ? 'medium' : 'bad';
        var dlBadge = m.downloaded ? '<span class="aic-wiz-dl-badge"><i class="fas fa-check"></i> ' + t('Pobrany') + '</span>' : '';
        var activeBadge = m.active ? '<span class="aic-wiz-active-badge"><i class="fas fa-bolt"></i> ' + t('Aktywny') + '</span>' : '';
        var autoTag = (!m.downloaded && m.id === topRecId) ? '<span class="aic-wiz-auto-badge"><i class="fas fa-star"></i> ' + t('Rekomendowany na podstawie benchmarku') + '</span>' : '';

        // Show estimated TPS from benchmark
        var estTps = benchEstimates[m.id] || 0;
        var tpsHtml = '';
        if (estTps > 0) {
            var tpsTone = estTps >= 15 ? 'good' : estTps >= 5 ? 'medium' : 'bad';
            var tpsLabel = estTps >= 15 ? t('szybki') : estTps >= 5 ? t('OK') : t('wolny');
            tpsHtml = '<span class="aic-wiz-model-tps aic-status-' + tpsTone + '"><i class="fas fa-tachometer-alt"></i> ~' + estTps + ' tok/s (' + tpsLabel + ')</span>';
        }

        var actionBtn = '';
        if (m.downloaded && m.active) {
            actionBtn = '<button class="aic-btn-primary" disabled><i class="fas fa-check"></i> ' + t('Aktywny') + '</button>';
        } else if (m.downloaded) {
            actionBtn = '<button class="aic-btn-primary" onclick="window._aicWizActivate(\'' + m.id + '\')"><i class="fas fa-play"></i> ' + t('Aktywuj') + '</button>';
        } else if (ds.active && ds.model_id === m.id) {
            actionBtn = '<button class="aic-btn-primary" disabled><i class="fas fa-spinner fa-spin"></i> ' + t('Pobieranie…') + '</button>';
        } else {
            actionBtn = '<button class="aic-btn-primary" onclick="window._aicWizDownload(\'' + m.id + '\')"' + (ds.active ? ' disabled' : '') + '><i class="fas fa-download"></i> ' + t('Pobierz') + ' (' + (m.size_gb || '?') + ' GB)</button>';
        }

        cardsHtml +=
            '<div class="aic-wiz-model-card' + (m.id === topRecId ? ' aic-wiz-model-top' : '') + '">' +
                '<div class="aic-wiz-model-head">' +
                    '<span class="aic-wiz-model-name">' + _aicEsc(m.name) + '</span>' +
                    '<span class="aic-wiz-model-status aic-status-' + statusTone + '"><i class="fas ' + statusIcon + '"></i> ' + _aicEsc(m.status_label || '') + '</span>' +
                '</div>' +
                autoTag +
                '<div class="aic-wiz-model-meta">' +
                    '<span>' + _aicEsc(m.params) + '</span> · <span>' + _aicEsc(m.quant) + '</span> · <span>' + (m.size_gb || '?') + ' GB</span> · <span>' + (m.ram_required_gb || '?') + ' GB RAM</span>' +
                '</div>' +
                tpsHtml +
                '<div class="aic-wiz-model-desc">' + _aicEsc(m.description || '') + '</div>' +
                '<div class="aic-wiz-model-actions">' + dlBadge + activeBadge + actionBtn + '</div>' +
            '</div>';
    });

    return '<h3><i class="fas fa-cube"></i> ' + t('Wybierz model') + '</h3>' +
        '<p class="aic-wiz-hint">' + t('Modele posortowane wg benchmarku — na górze te z najlepszą wydajnością. Szukaj ≥5 tok/s dla płynnego czatu.') + '</p>' +
        pathHtml +
        progressHtml +
        '<div class="aic-wiz-models">' + cardsHtml + '</div>' +
        '<div class="aic-wiz-actions">' +
            '<button class="aic-btn-secondary" onclick="window._aicWizPrev()"><i class="fas fa-arrow-left"></i> ' + t('Wstecz') + '</button>' +
            '<button class="aic-btn-primary" onclick="window._aicWizNext()"><i class="fas fa-check"></i> ' + t('Zakończ konfigurację') + '</button>' +
        '</div>';
}

window._aicWizSetPath = function () {
    var input = document.getElementById('wizModelsPath');
    if (!input) return;
    var path = input.value.trim();
    if (!path) return;
    _aicFetch('/api/aichat/models/path', {
        method: 'POST',
        body: JSON.stringify({ path: path }),
    }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.error) {
            showToast(d.error, 'error');
        } else {
            _aic.wizardModelsPath = d.path;
            _aic.wizardDisk = d.disk || {};
            showToast(t('Ścieżka modeli zmieniona'), 'success');
            _aic.wizardRecModel = null; // force reload
            var root = document.querySelector('.aic-root');
            if (root) _aicRenderWizard(root);
        }
    });
};

window._aicWizCancelDl = function () {
    _aicFetch('/api/aichat/models/download/cancel', { method: 'POST' }).then(function () {
        if (_aic._wizDlPoll) { clearInterval(_aic._wizDlPoll); _aic._wizDlPoll = null; }
        showToast(t('Anulowano pobieranie'), 'info');
        _aic.wizardRecModel = null;
        var root = document.querySelector('.aic-root');
        if (root) _aicRenderWizard(root);
    });
};

window._aicWizDownload = function (modelId) {
    _aicFetch('/api/aichat/models/download', { method: 'POST', body: JSON.stringify({ model_id: modelId }) })
        .then(function (r) { return r.json(); }).then(function (d) {
            if (d.error) {
                showToast(d.error, 'error');
            } else {
                showToast(t('Pobieranie rozpoczęte'), 'success');
                // Update download_status to show progress immediately
                if (_aic.wizardRecModel) {
                    _aic.wizardRecModel.download_status = { active: true, model_id: modelId, progress: 0, status: 'Rozpoczynanie…' };
                }
                var root = document.querySelector('.aic-root');
                if (root) _aicRenderWizard(root);
                // Poll for download progress with inline updates
                if (_aic._wizDlPoll) clearInterval(_aic._wizDlPoll);
                _aic._wizDlPoll = setInterval(function () {
                    _aicFetch('/api/aichat/models/download/status').then(function (r) { return r.json(); }).then(function (ds) {
                        // Inline update progress bar elements
                        var bar = document.getElementById('wizDlBar');
                        var pctEl = document.querySelector('.aic-wiz-dl-pct');
                        var statusEl = document.getElementById('wizDlStatus');
                        var speedEl = document.getElementById('wizDlSpeed');
                        var pct = Math.round(ds.progress || 0);
                        if (bar) bar.style.width = pct + '%';
                        if (pctEl) pctEl.textContent = pct + '%';
                        if (statusEl) statusEl.textContent = ds.status || '';
                        // Speed element may not exist yet — create it dynamically
                        if (speedEl) {
                            speedEl.textContent = ds.speed || '';
                        } else if (ds.speed) {
                            var detailEl = document.querySelector('.aic-wiz-dl-detail');
                            if (detailEl) {
                                var sp = document.createElement('span');
                                sp.id = 'wizDlSpeed';
                                sp.textContent = ds.speed;
                                detailEl.appendChild(sp);
                            }
                        }
                        if (_aic.wizardRecModel) _aic.wizardRecModel.download_status = ds;
                        if (!ds.active) {
                            clearInterval(_aic._wizDlPoll);
                            _aic._wizDlPoll = null;
                            _aic.wizardRecModel = null; // force reload catalog
                            var root2 = document.querySelector('.aic-root');
                            if (root2) _aicRenderWizard(root2);
                            if (ds.error) {
                                showToast(ds.error, 'error');
                            } else {
                                showToast(t('Model pobrany!'), 'success');
                            }
                        }
                    }).catch(function () { /* network hiccup — retry next interval */ });
                }, 1000);
            }
        });
};

window._aicWizActivate = function (modelId) {
    _aicFetch('/api/aichat/models/active', { method: 'POST', body: JSON.stringify({ model_id: modelId }) })
        .then(function (r) { return r.json(); }).then(function (d) {
            if (d.error) {
                if (typeof showToast === 'function') showToast(d.error, 'error');
            } else {
                if (typeof showToast === 'function') showToast(t('Model aktywowany'), 'success');
                _aic.wizardRecModel = null;
                var root = document.querySelector('.aic-root');
                if (root) _aicRenderWizard(root);
            }
        });
};

/* Step 3: Complete */
function _aicWizStep3() {
    var hw = _aic.wizardHw || {};
    var bench = _aic.wizardBench || {};
    var tier = bench.tier || (hw.tier || {});

    return '<h3><i class="fas fa-check-circle" style="color:#10b981"></i> ' + t('Konfiguracja zakończona!') + '</h3>' +
        '<div class="aic-wiz-summary">' +
            '<p>' + t('Twój system AI jest skonfigurowany i gotowy do użycia.') + '</p>' +
            (tier.name ? '<p><strong>' + t('Profil') + ':</strong> ' + _aicEsc(tier.name) + ' — ' + _aicEsc(tier.description || '') + '</p>' : '') +
            (bench.tps ? '<p><strong>' + t('Wydajność') + ':</strong> ' + bench.tps + ' tok/s</p>' : '') +
            '<p><i class="fas fa-shield-alt" style="color:#10b981"></i> ' + t('100% lokalne przetwarzanie — Twoje dane nie opuszczają serwera') + '</p>' +
        '</div>' +
        '<div class="aic-wiz-actions">' +
            '<button class="aic-btn-primary aic-btn-large" onclick="window._aicWizFinish()"><i class="fas fa-comments"></i> ' + t('Rozpocznij czat') + '</button>' +
        '</div>';
}

window._aicWizNext = function () {
    _aic.wizardStep = Math.min(_aic.wizardStep + 1, 3);
    var root = document.querySelector('.aic-root');
    if (root) _aicRenderWizard(root);
};

window._aicWizPrev = function () {
    _aic.wizardStep = Math.max(_aic.wizardStep - 1, 0);
    var root = document.querySelector('.aic-root');
    if (root) _aicRenderWizard(root);
};

window._aicWizFinish = function () {
    // Save calibration + force local provider
    var hw = _aic.wizardHw || {};
    var bench = _aic.wizardBench || {};
    var tier = bench.tier || hw.tier || {};
    Promise.all([
        _aicFetch('/api/aichat/calibration', {
            method: 'POST',
            body: JSON.stringify({
                tier_id: tier.id || 'balanced',
                benchmark: bench.tps ? { tps: bench.tps, ttft: bench.ttft } : null,
                hardware: hw.hardware || null,
            }),
        }),
        _aicFetch('/api/aichat/config', {
            method: 'POST',
            body: JSON.stringify({ provider: 'local' }),
        }),
    ]).then(function () {
        _aic.view = 'chat';
        _aic.wizardStep = 0;
        showToast(t('Konfiguracja AI zakończona!'), 'success');
        var root = document.querySelector('.aic-root');
        if (root) {
            _aicLoadConfig(function () {
                _aicLoadStatus(function () { _aicRender(root.parentElement); });
            });
        }
    });
};


/* ═══════════════════════════ PERFORMANCE DASHBOARD ═══════════════════════════ */

window._aicOpenDashboard = function () {
    _aic.view = 'dashboard';
    var root = document.querySelector('.aic-root');
    if (root) {
        root.innerHTML = '<div class="aic-loading"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie…') + '</div>';
        // Load fresh health + hardware data
        Promise.all([
            _aicFetch('/api/aichat/health').then(function (r) { return r.json(); }).catch(function () { return { score: 0, grade: '?', details: [] }; }),
            _aicFetch('/api/aichat/hardware').then(function (r) { return r.json(); }).catch(function () { return {}; }),
            _aicFetch('/api/aichat/models/benchmark').then(function (r) { return r.json(); }).catch(function () { return { benchmark: null }; }),
        ]).then(function (results) {
            _aic.health = results[0];
            _aic.dashHw = results[1];
            _aic.dashBench = results[2].benchmark || null;
            _aicRenderDashboard(root);
        }).catch(function () {
            _aicRenderDashboard(root);
        });
    }
};

window._aicCloseDashboard = function () {
    _aic.view = 'chat';
    var root = document.querySelector('.aic-root');
    if (root) _aicRender(root.parentElement);
};

function _aicRenderDashboard(root) {
    var health = _aic.health || { score: 0, grade: '?', details: [] };
    var hw = (_aic.dashHw || {}).hardware || {};
    var cpu = hw.cpu || {};
    var bench = _aic.dashBench;

    // Health score ring color
    var scoreColor = health.score >= 85 ? '#10b981' : health.score >= 65 ? '#f59e0b' : health.score >= 40 ? '#ef4444' : '#6b7280';

    // Health details
    var detailsHtml = '';
    (health.details || []).forEach(function (d) {
        var icon = d.ok ? 'fa-check-circle' : 'fa-exclamation-circle';
        var color = d.ok ? '#10b981' : '#f59e0b';
        detailsHtml += '<div class="aic-dash-detail">' +
            '<i class="fas ' + icon + '" style="color:' + color + '"></i> ' +
            '<span class="aic-dash-detail-label">' + _aicEsc(d.label) + '</span>' +
            '<span class="aic-dash-detail-score">' + d.score + '/' + d.max + '</span>' +
            (d.hint ? '<span class="aic-dash-detail-hint">' + _aicEsc(d.hint) + '</span>' : '') +
        '</div>';
    });

    // TPS & TTFT cards
    var benchHtml = '';
    if (bench) {
        var tier = bench.tier || {};
        benchHtml = '<div class="aic-dash-bench">' +
            '<div class="aic-dash-metric">' +
                '<div class="aic-dash-metric-value">' + (bench.tps || 0) + '</div>' +
                '<div class="aic-dash-metric-label">' + t('tok/s') + '</div>' +
            '</div>' +
            '<div class="aic-dash-metric">' +
                '<div class="aic-dash-metric-value">' + ((bench.ttft || 0) * 1000).toFixed(0) + '</div>' +
                '<div class="aic-dash-metric-label">' + t('TTFT (ms)') + '</div>' +
            '</div>' +
            '<div class="aic-dash-metric">' +
                '<div class="aic-dash-metric-value" style="color:' + (tier.color || '#6b7280') + '">' + _aicEsc(tier.name || '?') + '</div>' +
                '<div class="aic-dash-metric-label">' + t('Profil') + '</div>' +
            '</div>' +
        '</div>';
    } else {
        var isRemote = _aic.cfg && _aic.cfg.provider && _aic.cfg.provider !== 'local';
        if (isRemote) {
            benchHtml = '<div class="aic-dash-no-bench">' +
                '<p><i class="fas fa-cloud"></i> ' + t('Benchmark niedostępny dla zdalnego API') + ' (' + _aicEsc((_aic.cfg || {}).provider || '') + ')</p>' +
                '<p class="aic-field-hint">' + t('Benchmark mierzy lokalny model. Przełącz na lokalny model, aby uruchomić benchmark.') + '</p>' +
            '</div>';
        } else {
            benchHtml = '<div class="aic-dash-no-bench">' +
                '<p>' + t('Brak wyników benchmarku') + '</p>' +
                '<button class="aic-btn-primary" onclick="window._aicDashRunBench()"><i class="fas fa-play"></i> ' + t('Uruchom benchmark') + '</button>' +
            '</div>';
        }
    }

    // Hardware summary
    var hwHtml = '';
    if (cpu.cpu_name) {
        var featBadges = '';
        if (cpu.avx2) featBadges += '<span class="aic-dash-feat">AVX2</span>';
        if (cpu.avx512) featBadges += '<span class="aic-dash-feat">AVX-512</span>';
        if (cpu.vnni) featBadges += '<span class="aic-dash-feat">VNNI</span>';
        if (cpu.neon) featBadges += '<span class="aic-dash-feat">NEON</span>';

        hwHtml = '<div class="aic-dash-hw">' +
            '<div class="aic-dash-hw-row"><i class="fas fa-microchip"></i> ' + _aicEsc(cpu.cpu_name) +
                ' <span>(' + (cpu.cores_physical || '?') + 'C/' + (cpu.cores_logical || '?') + 'T)</span></div>' +
            '<div class="aic-dash-hw-row"><i class="fas fa-memory"></i> ' + (hw.ram_total_gb || '?') + ' GB RAM</div>' +
            '<div class="aic-dash-hw-row"><i class="fas fa-cogs"></i> ' + t('Wątki AI') + ': ' + (cpu.optimal_threads || '?') + ' ' + featBadges + '</div>' +
        '</div>';
    }

    root.innerHTML =
        '<div class="aic-dashboard">' +
            '<div class="aic-dash-header">' +
                '<button class="aic-btn-icon" onclick="window._aicCloseDashboard()"><i class="fas fa-arrow-left"></i></button>' +
                '<span class="aic-dash-title"><i class="fas fa-heartbeat"></i> ' + t('AI Performance Dashboard') + '</span>' +
                '<button class="aic-btn-icon" onclick="window._aicOpenWizard()" title="' + t('Kreator') + '"><i class="fas fa-magic"></i></button>' +
            '</div>' +
            '<div class="aic-dash-body">' +
                '<div class="aic-dash-score-section">' +
                    '<div class="aic-dash-score-ring" style="border-color:' + scoreColor + '">' +
                        '<div class="aic-dash-score-num">' + health.score + '</div>' +
                        '<div class="aic-dash-score-grade" style="color:' + scoreColor + '">' + health.grade + '</div>' +
                    '</div>' +
                    '<div class="aic-dash-score-label">' + t('AI Health Score') + '</div>' +
                    '<div class="aic-dash-local"><i class="fas fa-shield-alt"></i> ' + t('100% lokalne przetwarzanie') + '</div>' +
                '</div>' +
                '<div class="aic-dash-details">' +
                    '<h4>' + t('Szczegóły') + '</h4>' +
                    detailsHtml +
                '</div>' +
                '<div class="aic-dash-perf-section">' +
                    '<h4>' + t('Wydajność') + '</h4>' +
                    benchHtml +
                '</div>' +
                hwHtml +
            '</div>' +
        '</div>';
}

window._aicDashRunBench = function () {
    var root = document.querySelector('.aic-root');
    var noBench = root ? root.querySelector('.aic-dash-no-bench') : null;
    if (noBench) noBench.innerHTML = '<div class="aic-wiz-loading"><i class="fas fa-spinner fa-spin"></i> ' + t('Benchmark…') + '</div>';
    _aicFetch('/api/aichat/models/benchmark', { method: 'POST', body: JSON.stringify({}) })
        .then(function (r) { return r.json(); }).then(function (d) {
            if (d.error) {
                showToast(d.error, 'error');
            } else {
                _aic.dashBench = d;
                showToast(t('Benchmark zakończony') + ': ' + d.tps + ' tok/s', 'success');
            }
            var root2 = document.querySelector('.aic-root');
            if (root2) _aicRenderDashboard(root2);
        }).catch(function (err) {
            showToast(t('Błąd benchmarku: ') + err.message, 'error');
            var root2 = document.querySelector('.aic-root');
            if (root2) _aicRenderDashboard(root2);
        });
};


/* ═══════════════════════════ SETTINGS ═══════════════════════════ */
window._aicOpenSettings = function () {
    _aic.view = 'settings';
    var root = document.querySelector('.aic-root');
    if (root) _aicRender(root.parentElement);
};

window._aicCloseSettings = function () {
    _aic.view = 'chat';
    var root = document.querySelector('.aic-root');
    if (root) _aicRender(root.parentElement);
};

function _aicRenderSettings(root) {
    var c = _aic.cfg || {};
    root.innerHTML =
        '<div class="aic-settings">' +
            '<div class="aic-settings-header">' +
                '<button class="aic-btn-icon" onclick="window._aicCloseSettings()"><i class="fas fa-arrow-left"></i></button>' +
                '<span class="aic-settings-title">' + t('Ustawienia AI Chat') + '</span>' +
            '</div>' +
            '<div class="aic-settings-body">' +
                '<div class="aic-field">' +
                    '<label>' + t('Dostawca') + '</label>' +
                    '<div class="aic-field-hint aic-ok"><i class="fas fa-shield-alt"></i> ' + t('100% lokalny model — Twoje dane nie opuszczają serwera') + '</div>' +
                '</div>' +
                '<div class="aic-field-row">' +
                    '<div class="aic-field">' +
                        '<label>Max tokens</label>' +
                        '<input type="number" id="aicMaxTokens" value="' + (c.max_tokens || 4096) + '" min="256" max="128000">' +
                    '</div>' +
                '</div>' +
                '<div class="aic-field">' +
                    '<label>' + t('Workspace (domyślna ścieżka dla przeglądarki plików)') + '</label>' +
                    '<input type="text" id="aicWorkspace" value="' + _aicEsc(c.workspace || '') + '" placeholder="/home/nasadmin/projekt">' +
                '</div>' +
                /* ── RAG section ── */
                '<div class="aic-settings-section">' +
                    '<h3><i class="fas fa-brain"></i> ' + t('Baza wiedzy (RAG)') + '</h3>' +
                    '<div class="aic-field">' +
                        '<label class="aic-toggle-label">' +
                            '<input type="checkbox" id="aicRagEnabled"' + (c.rag_enabled !== false ? ' checked' : '') + '> ' +
                            t('Automatyczne wyszukiwanie (RAG)') +
                        '</label>' +
                        '<div class="aic-field-hint">' + t('Asystent automatycznie przeszuka Twoje pliki przed odpowiedzią') + '</div>' +
                    '</div>' +
                    '<div class="aic-field">' +
                        '<label>' + t('Maks. fragmentów kontekstu') + '</label>' +
                        '<input type="number" id="aicRagTopK" value="' + (c.rag_top_k || 5) + '" min="1" max="20">' +
                    '</div>' +
                    '<div class="aic-field">' +
                        '<label>' + t('Stan indeksu') + '</label>' +
                        '<div class="aic-rag-stats-row">' +
                            '<span id="aicRagStats">' +
                                (_aic.rag && _aic.rag.stats ? (_aic.rag.stats.document_files + ' ' + t('dokumentów') + ', ' + _aic.rag.stats.gallery_files + ' ' + t('zdjęć')) : '…') +
                            '</span>' +
                        '</div>' +
                        '<div class="aic-rag-progress-bar" id="aicRagProgressWrap" style="display:none"><div id="aicRagProgress" class="aic-rag-progress-fill"></div></div>' +
                    '</div>' +
                    '<div class="aic-field-row aic-rag-actions">' +
                        '<button class="aic-btn-secondary" onclick="window._aicStartIndexing()"><i class="fas fa-sync"></i> ' + t('Indeksuj katalog domowy') + '</button>' +
                        '<button class="aic-btn-danger-sm" onclick="window._aicClearIndex()"><i class="fas fa-trash"></i> ' + t('Wyczyść indeks') + '</button>' +
                    '</div>' +
                '</div>' +
                /* ── Scheduler section ── */
                '<div class="aic-settings-section">' +
                    '<h3><i class="fas fa-clock"></i> ' + t('Harmonogram indeksowania') + '</h3>' +
                    '<div class="aic-field">' +
                        '<label class="aic-toggle-label">' +
                            '<input type="checkbox" id="aicSchedulerEnabled" onchange="window._aicToggleScheduler(this.checked)"' + (_aic.rag && _aic.rag.scheduler && _aic.rag.scheduler.active ? ' checked' : '') + '> ' +
                            t('Automatyczne indeksowanie') +
                        '</label>' +
                        '<div class="aic-field-hint">' + t('Cyklicznie indeksuj pliki w tle') + '</div>' +
                    '</div>' +
                    '<div class="aic-field" id="aicSchedulerIntervalField"' + (_aic.rag && _aic.rag.scheduler && _aic.rag.scheduler.active ? '' : ' style="display:none"') + '>' +
                        '<label>' + t('Częstotliwość') + '</label>' +
                        '<select id="aicSchedulerInterval" onchange="window._aicSetSchedulerInterval(this.value)">' +
                            '<option value="every_30min"' + (_aic.rag && _aic.rag.scheduler && _aic.rag.scheduler.interval === '*:0/30' ? ' selected' : '') + '>' + t('Co 30 minut') + '</option>' +
                            '<option value="hourly"' + (_aic.rag && _aic.rag.scheduler && (_aic.rag.scheduler.interval === 'hourly' || !_aic.rag.scheduler.interval) ? ' selected' : '') + '>' + t('Co godzinę') + '</option>' +
                            '<option value="every_6h"' + (_aic.rag && _aic.rag.scheduler && _aic.rag.scheduler.interval === '*-*-* 0/6:00:00' ? ' selected' : '') + '>' + t('Co 6 godzin') + '</option>' +
                            '<option value="every_12h"' + (_aic.rag && _aic.rag.scheduler && _aic.rag.scheduler.interval === '*-*-* 0/12:00:00' ? ' selected' : '') + '>' + t('Co 12 godzin') + '</option>' +
                            '<option value="daily"' + (_aic.rag && _aic.rag.scheduler && _aic.rag.scheduler.interval === 'daily' ? ' selected' : '') + '>' + t('Raz dziennie') + '</option>' +
                        '</select>' +
                    '</div>' +
                '</div>' +
                '<div class="aic-settings-actions">' +
                    '<button class="aic-btn-primary" onclick="window._aicSaveSettings()"><i class="fas fa-save"></i> ' + t('Zapisz') + '</button>' +
                    '<button class="aic-btn-secondary" onclick="window._aicCloseSettings()">' + t('Anuluj') + '</button>' +
                '</div>' +
            '</div>' +
        '</div>';
}

window._aicSaveSettings = function () {
    var data = {
        provider: 'local',
        max_tokens: parseInt(document.getElementById('aicMaxTokens').value) || 4096,
        workspace: document.getElementById('aicWorkspace').value.trim(),
        rag_enabled: document.getElementById('aicRagEnabled') ? document.getElementById('aicRagEnabled').checked : true,
        rag_top_k: parseInt((document.getElementById('aicRagTopK') || {}).value) || 5,
    };

    _aicFetch('/api/aichat/config', {
        method: 'POST',
        body: JSON.stringify(data),
    }).then(function (r) { return r.json(); }).then(function () {
        if (typeof showToast === 'function') showToast(t('Ustawienia zapisane'), 'success');
        _aicLoadConfig(function () { window._aicCloseSettings(); });
    }).catch(function () {
        if (typeof showToast === 'function') showToast(t('Błąd zapisu ustawień'), 'error');
    });
};
