/**
 * EthOS — Sticky Notes (Karteczki)
 * Slide-to-reveal panel at the right edge of the screen.
 */
AppRegistry['sticky-notes'] = function (appDef) {
    const panelId = 'sticky-notes';

    // Toggle: if already open, remove it
    const existing = document.getElementById('sn-panel');
    if (existing) {
        _stickyClose(existing, panelId);
        return;
    }

    _stickyOpenPanel(panelId);
};

function _stickyClose(panel, panelId) {
    panel.classList.remove('sn-editing');
    panel.classList.add('sn-removing');
    setTimeout(() => {
        panel.remove();
        WM.windows.delete(panelId);
        updateTaskbarWindows();
    }, 350);
}

function _stickyOpenPanel(panelId) {
    const panel = document.createElement('div');
    panel.id = 'sn-panel';
    document.body.appendChild(panel);

    // Position above taskbar
    const taskbar = document.getElementById('taskbar');
    if (taskbar) panel.style.bottom = taskbar.offsetHeight + 'px';

    // Register in WM for taskbar / cleanup
    WM.windows.set(panelId, {
        id: panelId,
        el: panel,
        opts: { title: t('Karteczki'), icon: 'fa-sticky-note', iconColor: '#eab308', onClose: null },
        minimized: false,
        maximized: false,
        prevBounds: null,
    });
    updateTaskbarWindows();

    // Entrance: slide tab in from off-screen
    panel.classList.add('sn-entering');
    requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.remove('sn-entering')));

    _stickyInit(panel, panelId);
}

function _stickyInit(body, panelId) {
    const API = '/notes';
    let notes = [];
    let order = [];
    let editingId = null;

    const COLORS = [
        { id: 'yellow',  bg: '#fef08a', text: '#713f12', header: '#fde047' },
        { id: 'green',   bg: '#bbf7d0', text: '#14532d', header: '#86efac' },
        { id: 'blue',    bg: '#bfdbfe', text: '#1e3a5f', header: '#93c5fd' },
        { id: 'pink',    bg: '#fecdd3', text: '#881337', header: '#fda4af' },
        { id: 'purple',  bg: '#e9d5ff', text: '#581c87', header: '#d8b4fe' },
        { id: 'orange',  bg: '#fed7aa', text: '#7c2d12', header: '#fdba74' },
        { id: 'gray',    bg: '#e2e8f0', text: '#1e293b', header: '#cbd5e1' },
    ];

    function getColor(id) { return COLORS.find(c => c.id === id) || COLORS[0]; }
    function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
    function timeAgo(ts) {
        const s = Math.floor((Date.now() / 1000) - ts);
        if (s < 60) return t('przed chwilą');
        if (s < 3600) return Math.floor(s / 60) + ' min temu';
        if (s < 86400) return Math.floor(s / 3600) + ' godz. temu';
        return new Date(ts * 1000).toLocaleDateString('pl', { day: 'numeric', month: 'short' });
    }

    const CSS = `<style>
    /* ── Panel: fixed, right edge, slide-to-reveal ── */
    #sn-panel {
        position: fixed; top: 0; right: 0; bottom: 48px;
        width: 380px; max-width: 95vw; z-index: 900;
        display: flex; flex-direction: row;
        transform: translateX(calc(100% - 28px));
        transition: transform 0.3s ease-in-out;
        pointer-events: none;
    }
    #sn-panel.sn-entering { transform: translateX(100%); }
    #sn-panel:hover,
    #sn-panel.sn-editing  { transform: translateX(0); pointer-events: auto; }
    #sn-panel.sn-removing { transform: translateX(100%) !important; pointer-events: none !important;
                            transition: transform 0.3s ease-in !important; }

    /* ── Tab handle (visible strip when collapsed) ── */
    .sn-tab {
        width: 28px; flex-shrink: 0;
        display: flex; align-items: center; justify-content: center;
        pointer-events: auto; cursor: pointer; position: relative;
    }
    .sn-tab-handle {
        width: 26px; padding: 10px 0; border: 1px solid var(--border,#1e293b); border-right: none;
        border-radius: 10px 0 0 10px; background: var(--bg,#0f172a);
        display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
        color: #eab308; font-size: 14px; box-shadow: -2px 0 10px rgba(0,0,0,.25);
        transition: background .2s, box-shadow .2s;
    }
    .sn-tab-handle .sn-tab-label {
        writing-mode: vertical-rl; text-orientation: mixed;
        font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
        color: var(--text-muted,#94a3b8); line-height: 1;
    }
    .sn-tab:hover .sn-tab-handle {
        background: rgba(234,179,8,.08); box-shadow: -2px 0 16px rgba(234,179,8,.18);
    }
    .sn-tab-badge {
        position: absolute; top: 50%; left: 50%; transform: translate(-50%, 20px);
        background: #eab308; color: #1e293b; font-size: 9px; font-weight: 800;
        min-width: 16px; height: 16px; line-height: 16px; border-radius: 8px;
        text-align: center; padding: 0 3px; box-sizing: border-box; display: none;
    }

    /* ── Content area ── */
    .sn-content {
        flex: 1; display: flex; flex-direction: column; overflow: hidden;
        background: var(--bg,#0f172a); border-left: 1px solid var(--border,#1e293b);
        box-shadow: -4px 0 24px rgba(0,0,0,.4);
    }

    .sn-header { display:flex; align-items:center; gap:10px; padding:14px 18px;
                 border-bottom:1px solid var(--border,#1e293b); flex-shrink:0; }
    .sn-header h2 { margin:0; font-size:15px; font-weight:700; color:var(--text,#e2e8f0);
                    display:flex; align-items:center; gap:8px; flex:1; }
    .sn-btn { padding:7px 14px; border:none; border-radius:8px; cursor:pointer; font-size:12px; font-weight:600;
              display:inline-flex; align-items:center; gap:6px; transition:.15s; }
    .sn-btn.primary { background:#eab308; color:#1e293b; }
    .sn-btn.primary:hover { background:#facc15; }
    .sn-btn.sm { padding:4px 8px; font-size:11px; border-radius:6px; }
    .sn-btn.ghost { background:transparent; color:var(--text-muted,#94a3b8); }
    .sn-btn.ghost:hover { background:var(--bg-hover,rgba(255,255,255,.06)); color:var(--text,#e2e8f0); }
    .sn-btn:disabled { opacity:.5; cursor:not-allowed; }
    .sn-close-btn { width:30px; height:30px; border:none; border-radius:8px; background:transparent;
                    color:var(--text-muted,#94a3b8); cursor:pointer; display:flex; align-items:center;
                    justify-content:center; font-size:14px; transition:.15s; flex-shrink:0; }
    .sn-close-btn:hover { background:rgba(255,255,255,.08); color:var(--text,#e2e8f0); }

    .sn-search { padding:7px 12px; border:1px solid var(--border,#334155); border-radius:8px;
                 background:var(--bg-input,#0f172a); color:var(--text,#e2e8f0); font-size:12px;
                 width:100%; outline:none; box-sizing:border-box; }
    .sn-search:focus { border-color:#eab308; }
    .sn-search-wrap { padding:0 18px 12px; flex-shrink:0; }

    .sn-list { flex:1; overflow-y:auto; padding:8px 14px 18px; display:flex; flex-direction:column; gap:10px; }

    .sn-card { border-radius:12px; display:flex; flex-direction:column; min-height:80px;
               box-shadow:0 2px 8px rgba(0,0,0,.15); cursor:pointer; transition:transform .15s, box-shadow .15s;
               position:relative; overflow:hidden; flex-shrink:0; }
    .sn-card:hover { transform:translateY(-1px); box-shadow:0 4px 14px rgba(0,0,0,.25); }
    .sn-card-header { padding:8px 12px; display:flex; align-items:center; gap:6px; font-size:12px;
                      font-weight:700; min-height:32px; }
    .sn-card-header .pin { font-size:10px; opacity:.6; }
    .sn-card-title { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .sn-card-body { flex:1; padding:4px 14px 8px; font-size:12px; line-height:1.5; overflow:hidden;
                    white-space:pre-wrap; word-break:break-word; opacity:.85; max-height:120px; }
    .sn-card-footer { padding:4px 12px 8px; font-size:10px; opacity:.5; text-align:right; }
    .sn-card-actions { position:absolute; top:6px; right:6px; display:flex; gap:2px; opacity:0; transition:opacity .15s; }
    .sn-card:hover .sn-card-actions { opacity:1; }
    .sn-card-actions button { width:24px; height:24px; border:none; border-radius:6px; cursor:pointer;
                               display:flex; align-items:center; justify-content:center; font-size:11px;
                               background:rgba(0,0,0,.15); color:inherit; transition:.15s; }
    .sn-card-actions button:hover { background:rgba(0,0,0,.3); }

    .sn-empty { display:flex; flex-direction:column; align-items:center; justify-content:center; flex:1;
                color:var(--text-muted,#94a3b8); gap:12px; padding:40px; }
    .sn-empty i { font-size:40px; opacity:.2; }
    .sn-empty p { font-size:13px; }

    /* Editor overlay */
    .sn-editor-overlay { position:absolute; inset:0; background:rgba(0,0,0,.55); z-index:10;
                          display:flex; align-items:center; justify-content:center; backdrop-filter:blur(2px); }
    .sn-editor { width:360px; max-width:95%; border-radius:14px; box-shadow:0 12px 40px rgba(0,0,0,.4);
                 display:flex; flex-direction:column; max-height:80%; }
    .sn-editor-header { padding:14px 18px; border-radius:14px 14px 0 0; display:flex; align-items:center; gap:10px; }
    .sn-editor-header input { flex:1; background:transparent; border:none; font-size:15px; font-weight:700;
                               color:inherit; outline:none; }
    .sn-editor-header input::placeholder { color:inherit; opacity:.5; }
    .sn-editor-body textarea { width:100%; border:none; background:transparent; color:inherit; font-size:13px;
                                line-height:1.7; padding:14px 18px; resize:none; outline:none; min-height:160px; flex:1;
                                font-family:inherit; box-sizing:border-box; }
    .sn-editor-body textarea::placeholder { color:inherit; opacity:.4; }
    .sn-editor-footer { padding:10px 18px; display:flex; align-items:center; gap:6px;
                        border-top:1px solid rgba(0,0,0,.1); flex-wrap:wrap; }
    .sn-color-dot { width:22px; height:22px; border-radius:50%; cursor:pointer; border:2px solid transparent;
                    transition:.15s; flex-shrink:0; }
    .sn-color-dot:hover { transform:scale(1.15); }
    .sn-color-dot.active { border-color:rgba(0,0,0,.4); box-shadow:0 0 0 2px rgba(255,255,255,.3); }
    .sn-editor-footer .spacer { flex:1; }

    .sn-add-card { border:2px dashed var(--border,#334155); border-radius:12px; min-height:60px;
                   display:flex; align-items:center; justify-content:center; gap:8px;
                   cursor:pointer; color:var(--text-muted,#94a3b8); transition:.15s; flex-shrink:0; }
    .sn-add-card:hover { border-color:#eab308; color:#eab308; background:rgba(234,179,8,.04); }
    .sn-add-card i { font-size:20px; }
    .sn-add-card span { font-size:12px; font-weight:600; }

    /* Autostart toggle */
    .sn-autostart { display:flex; align-items:center; gap:6px; padding:6px 18px 10px; flex-shrink:0;
                    font-size:11px; color:var(--text-muted,#94a3b8); cursor:pointer; user-select:none; }
    .sn-autostart:hover { color:var(--text,#e2e8f0); }
    .sn-autostart input[type=checkbox] { accent-color:#eab308; width:14px; height:14px; cursor:pointer; margin:0; }
    </style>`;

    body.innerHTML = CSS + `
        <div class="sn-tab">
            <div class="sn-tab-handle">
                <i class="fas fa-sticky-note"></i>
                <span class="sn-tab-label">Notatki</span>
            </div>
            <span class="sn-tab-badge" id="sn-badge"></span>
        </div>
        <div class="sn-content">
            <div class="sn-header">
                <h2><i class="fas fa-sticky-note" style="color:#eab308"></i> Karteczki</h2>
                <button class="sn-btn primary" id="sn-add"><i class="fas fa-plus"></i> Nowa</button>
                <button class="sn-close-btn" id="sn-close-panel" title="Zamknij"><i class="fas fa-times"></i></button>
            </div>
            <div class="sn-search-wrap">
                <input class="sn-search" id="sn-search" placeholder="Szukaj…" type="text">
            </div>
            <label class="sn-autostart">
                <input type="checkbox" id="sn-autostart-cb" ${localStorage.getItem('sn_autostart') === '1' ? 'checked' : ''}>
                Otwieraj automatycznie po zalogowaniu
            </label>
            <div class="sn-list" id="sn-grid"></div>
        </div>
    `;

    const grid = body.querySelector('#sn-grid');
    const searchInput = body.querySelector('#sn-search');
    const badge = body.querySelector('#sn-badge');

    function updateBadge() {
        if (badge) {
            if (notes.length > 0) {
                badge.textContent = notes.length;
                badge.style.display = '';
            } else {
                badge.style.display = 'none';
            }
        }
    }

    // ── Load ──
    async function load() {
        try {
            const data = await api(API);
            notes = data.notes || [];
            order = data.order || notes.map(n => n.id);
            render();
            updateBadge();
        } catch (e) {
            grid.innerHTML = `<div class="sn-empty"><i class="fas fa-exclamation-circle"></i><p>${t('Błąd:')} ${esc(e.message)}</p></div>`;
        }
    }

    // ── Render ──
    function render(filter) {
        const q = (filter || searchInput.value).toLowerCase().trim();
        // Sort: pinned first, then by order
        let sorted = [...notes].sort((a, b) => {
            if (a.pinned && !b.pinned) return -1;
            if (!a.pinned && b.pinned) return 1;
            return order.indexOf(a.id) - order.indexOf(b.id);
        });
        if (q) {
            sorted = sorted.filter(n =>
                (n.title || '').toLowerCase().includes(q) ||
                (n.content || '').toLowerCase().includes(q)
            );
        }

        let html = '';
        // "Add" card always first
        html += `<div class="sn-add-card" id="sn-add-card"><i class="fas fa-plus"></i><span>Nowa karteczka</span></div>`;

        for (const n of sorted) {
            const c = getColor(n.color);
            const title = n.title || t('Bez tytułu');
            const preview = (n.content || '').slice(0, 200);
            html += `<div class="sn-card" data-id="${n.id}" style="background:${c.bg};color:${c.text};">
                <div class="sn-card-header" style="background:${c.header};">
                    ${n.pinned ? '<i class="fas fa-thumbtack pin"></i>' : ''}
                    <span class="sn-card-title">${esc(title)}</span>
                </div>
                <div class="sn-card-body">${esc(preview)}</div>
                <div class="sn-card-footer">${timeAgo(n.updated)}</div>
                <div class="sn-card-actions">
                    <button class="sn-pin-btn" data-id="${n.id}" title="${n.pinned ? 'Odepnij' : 'Przypnij'}"><i class="fas fa-thumbtack"></i></button>
                    <button class="sn-del-btn" data-id="${n.id}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                </div>
            </div>`;
        }

        grid.innerHTML = html;

        // Events
        grid.querySelector('#sn-add-card')?.addEventListener('click', () => openEditor(null));

        grid.querySelectorAll('.sn-card[data-id]').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('.sn-card-actions')) return;
                openEditor(card.dataset.id);
            });
        });

        grid.querySelectorAll('.sn-pin-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const n = notes.find(x => x.id === btn.dataset.id);
                if (!n) return;
                try {
                    await api(`${API}/${n.id}`, { method: 'PUT', body: { pinned: !n.pinned } });
                    n.pinned = !n.pinned;
                    render();
                } catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            });
        });

        grid.querySelectorAll('.sn-del-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const n = notes.find(x => x.id === btn.dataset.id);
                if (!confirm(`${t('Usunąć karteczkę "')}${esc(n?.title || t('Bez tytułu'))}"?`)) return;
                try {
                    await api(`${API}/${btn.dataset.id}`, { method: 'DELETE' });
                    notes = notes.filter(x => x.id !== btn.dataset.id);
                    order = order.filter(x => x !== btn.dataset.id);
                    render();
                    updateBadge();
                    toast(t('Karteczka usunięta'), 'success');
                } catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            });
        });
    }

    // ── Search ──
    searchInput.addEventListener('input', () => render());

    // ── Autostart toggle ──
    const autostartCb = body.querySelector('#sn-autostart-cb');
    if (autostartCb) {
        autostartCb.addEventListener('change', () => {
            localStorage.setItem('sn_autostart', autostartCb.checked ? '1' : '0');
            toast(autostartCb.checked ? t('Karteczki będą otwierane automatycznie') : t('Autostart wyłączony'), 'success');
        });
    }

    // ── Close panel button ──
    body.querySelector('#sn-close-panel').addEventListener('click', () => {
        _stickyClose(body, panelId);
    });

    // ── Add button ──
    body.querySelector('#sn-add').addEventListener('click', () => openEditor(null));

    // ── Editor ──
    function openEditor(noteId) {
        const existing = noteId ? notes.find(n => n.id === noteId) : null;
        const color = existing ? existing.color : 'yellow';
        const c = getColor(color);

        // Lock panel open while editing
        body.classList.add('sn-editing');

        const overlay = document.createElement('div');
        overlay.className = 'sn-editor-overlay';

        let currentColor = color;

        function closeEditor() {
            overlay.remove();
            body.classList.remove('sn-editing');
        }

        function editorColor() {
            const cc = getColor(currentColor);
            const editor = overlay.querySelector('.sn-editor');
            if (editor) {
                editor.style.background = cc.bg;
                editor.style.color = cc.text;
                const header = editor.querySelector('.sn-editor-header');
                if (header) header.style.background = cc.header;
            }
            overlay.querySelectorAll('.sn-color-dot').forEach(dot => {
                dot.classList.toggle('active', dot.dataset.color === currentColor);
            });
        }

        overlay.innerHTML = `
            <div class="sn-editor" style="background:${c.bg};color:${c.text};">
                <div class="sn-editor-header" style="background:${c.header};">
                    <i class="fas fa-sticky-note" style="font-size:16px;opacity:.6;"></i>
                    <input type="text" id="sn-ed-title" placeholder="${t('Tytuł…')}" value="${esc(existing?.title || '')}" maxlength="100">
                </div>
                <div class="sn-editor-body">
                    <textarea id="sn-ed-content" placeholder="${t('Wpisz treść notatki…')}" maxlength="5000">${esc(existing?.content || '')}</textarea>
                </div>
                <div class="sn-editor-footer">
                    ${COLORS.map(cc => `<div class="sn-color-dot ${cc.id === currentColor ? 'active' : ''}" data-color="${cc.id}" style="background:${cc.bg};"></div>`).join('')}
                    <div class="spacer"></div>
                    <button class="sn-btn sm ghost" id="sn-ed-cancel"><i class="fas fa-times"></i> Anuluj</button>
                    <button class="sn-btn sm primary" id="sn-ed-save"><i class="fas fa-check"></i> Zapisz</button>
                </div>
            </div>
        `;

        body.appendChild(overlay);

        // Color picker
        overlay.querySelectorAll('.sn-color-dot').forEach(dot => {
            dot.addEventListener('click', () => {
                currentColor = dot.dataset.color;
                editorColor();
            });
        });

        // Close on overlay click
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) closeEditor();
        });

        // Cancel
        overlay.querySelector('#sn-ed-cancel').addEventListener('click', () => closeEditor());

        // Save
        overlay.querySelector('#sn-ed-save').addEventListener('click', async () => {
            const title = overlay.querySelector('#sn-ed-title').value.trim();
            const content = overlay.querySelector('#sn-ed-content').value.trim();
            if (!title && !content) { toast(t('Wpisz tytuł lub treść'), 'error'); return; }

            const saveBtn = overlay.querySelector('#sn-ed-save');
            saveBtn.disabled = true;
            saveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

            try {
                if (existing) {
                    const r = await api(`${API}/${existing.id}`, { method: 'PUT', body: { title, content, color: currentColor } });
                    if (r.error) throw new Error(r.error);
                    Object.assign(existing, r.note);
                } else {
                    const r = await api(API, { method: 'POST', body: { title, content, color: currentColor } });
                    if (r.error) throw new Error(r.error);
                    notes.unshift(r.note);
                    order.unshift(r.note.id);
                }
                closeEditor();
                render();
                updateBadge();
                toast(existing ? 'Zapisano' : 'Karteczka dodana!', 'success');
            } catch (err) {
                toast(t('Błąd: ') + err.message, 'error');
                saveBtn.disabled = false;
                saveBtn.innerHTML = '<i class="fas fa-check"></i> Zapisz';
            }
        });

        // Auto-focus content
        setTimeout(() => {
            const el = existing ? overlay.querySelector('#sn-ed-content') : overlay.querySelector('#sn-ed-title');
            el?.focus();
        }, 100);
    }

    // ── Initial load ──
    load();
}
