/**
 * EthOS — Family Hub (Centrum Rodzinne)
 * Tablica ogłoszeń, listy zakupów, zadania domowe, kalendarz rodzinny.
 */

AppRegistry['family-hub'] = function (appDef) {
    const winId = 'family-hub';
    createWindow(winId, {
        title: t('Centrum Rodzinne'),
        icon: 'fa-house-user',
        iconColor: '#f472b6',
        width: 900, height: 620,
        onRender: (body) => _fhInit(body, winId),
    });
};

/* ═══════════════════════════════════════════════════════════════
   MAIN INIT
   ═══════════════════════════════════════════════════════════════ */
function _fhInit(root, winId) {
    const API = '/familyhub';
    let currentTab = 'board';
    let hubUsers = [];

    // ── Utility ──
    const esc = s => { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; };
    const timeAgo = ts => {
        const s = Math.floor(Date.now() / 1000 - ts);
        if (s < 60) return t('przed chwilą');
        if (s < 3600) return Math.floor(s / 60) + ' min temu';
        if (s < 86400) return Math.floor(s / 3600) + ' godz. temu';
        return new Date(ts * 1000).toLocaleDateString('pl', { day: 'numeric', month: 'short' });
    };
    const COLORS = [
        { id: 'blue',   bg: '#1e3a5f', text: '#93c5fd', accent: '#3b82f6' },
        { id: 'green',  bg: '#14532d', text: '#86efac', accent: '#22c55e' },
        { id: 'purple', bg: '#3b0764', text: '#c4b5fd', accent: '#8b5cf6' },
        { id: 'pink',   bg: '#500724', text: '#f9a8d4', accent: '#ec4899' },
        { id: 'orange', bg: '#431407', text: '#fed7aa', accent: '#f97316' },
        { id: 'yellow', bg: '#422006', text: '#fde68a', accent: '#eab308' },
    ];
    const getColor = id => COLORS.find(c => c.id === id) || COLORS[0];
    const EMOJIS = ['👍', '❤️', '😂', '🎉', '😮', '😢'];
    const LIST_COLORS = ['#3b82f6','#22c55e','#f97316','#ec4899','#8b5cf6','#eab308'];
    const CATEGORIES = ['', t('Owoce'), t('Warzywa'), t('Nabiał'), t('Mięso'), t('Pieczywo'), t('Napoje'), t('Chemia'), t('Inne')];
    const RECURRENCE = { once: t('Jednorazowe'), daily: t('Codziennie'), weekly: t('Co tydzień'), monthly: t('Co miesiąc') };
    const USER_COLORS = ['#3b82f6','#22c55e','#f97316','#ec4899','#8b5cf6','#eab308','#ef4444','#14b8a6'];
    const getUserColor = u => USER_COLORS[Math.abs([...u].reduce((a,c) => a + c.charCodeAt(0), 0)) % USER_COLORS.length];

    // ── CSS ──
    const CSS = `<style>
    .fh-wrap { display:flex; flex-direction:column; height:100%; background:var(--bg-primary,#0f172a); color:var(--text-primary,#e2e8f0); font-family:inherit; position:relative; overflow:hidden; }
    .fh-tabs { display:flex; border-bottom:1px solid var(--border,#1e293b); background:var(--bg-secondary,#1e293b); flex-shrink:0; }
    .fh-tab { flex:1; padding:10px 8px; text-align:center; cursor:pointer; border-bottom:3px solid transparent; transition:all .2s; font-size:13px; opacity:.6; position:relative; }
    .fh-tab:hover { opacity:.85; background:rgba(255,255,255,.03); }
    .fh-tab.active { opacity:1; border-bottom-color:#f472b6; }
    .fh-tab i { display:block; font-size:18px; margin-bottom:3px; }
    .fh-tab .fh-badge { position:absolute; top:4px; right:calc(50% - 24px); background:#ef4444; color:#fff; font-size:10px; min-width:16px; height:16px; line-height:16px; border-radius:8px; text-align:center; padding:0 4px; }
    .fh-body { flex:1; overflow-y:auto; padding:16px; }
    .fh-empty { text-align:center; padding:48px 16px; opacity:.4; }
    .fh-empty i { font-size:48px; margin-bottom:12px; display:block; }

    /* FAB */
    .fh-fab { position:absolute; bottom:16px; right:16px; width:48px; height:48px; border-radius:50%; background:#f472b6; color:#fff; border:none; font-size:20px; cursor:pointer; box-shadow:0 4px 12px rgba(244,114,182,.4); z-index:10; transition:transform .2s; }
    .fh-fab:hover { transform:scale(1.1); }

    /* Cards */
    .fh-card { background:var(--bg-secondary,#1e293b); border:1px solid var(--border,#334155); border-radius:10px; padding:14px; margin-bottom:10px; position:relative; transition:border-color .2s; }
    .fh-card:hover { border-color:rgba(244,114,182,.3); }
    .fh-card-header { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
    .fh-card-header .fh-avatar { width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:700; color:#fff; flex-shrink:0; }
    .fh-card-header .fh-meta { font-size:11px; opacity:.5; }
    .fh-card-title { font-weight:600; font-size:15px; margin-bottom:4px; }
    .fh-card-content { font-size:13px; opacity:.8; white-space:pre-wrap; line-height:1.5; }
    .fh-card-actions { display:flex; gap:4px; margin-top:8px; flex-wrap:wrap; align-items:center; }
    .fh-card .fh-pin { position:absolute; top:8px; right:8px; opacity:.4; font-size:12px; }
    .fh-card .fh-pin.pinned { opacity:1; color:#eab308; }

    /* Reactions */
    .fh-reactions { display:flex; gap:4px; flex-wrap:wrap; margin-top:8px; }
    .fh-react-btn { background:rgba(255,255,255,.06); border:1px solid transparent; border-radius:16px; padding:2px 8px; font-size:13px; cursor:pointer; transition:all .15s; }
    .fh-react-btn:hover { background:rgba(255,255,255,.12); }
    .fh-react-btn.mine { border-color:rgba(244,114,182,.4); background:rgba(244,114,182,.1); }
    .fh-react-add { opacity:.3; font-size:16px; cursor:pointer; padding:2px 6px; }
    .fh-react-add:hover { opacity:.7; }
    .fh-react-picker { display:flex; gap:2px; background:var(--bg-secondary,#1e293b); border:1px solid var(--border,#334155); border-radius:8px; padding:4px; position:absolute; bottom:100%; left:0; box-shadow:0 4px 16px rgba(0,0,0,.4); z-index:20; max-width:280px; }
    .fh-react-picker span { cursor:pointer; padding:4px 6px; border-radius:4px; font-size:18px; }
    .fh-react-picker span:hover { background:rgba(255,255,255,.1); }

    /* Forms / Modals */
    .fh-modal-overlay { position:absolute; inset:0; background:rgba(0,0,0,.6); z-index:50; display:flex; align-items:center; justify-content:center; }
    .fh-modal { background:var(--bg-secondary,#1e293b); border:1px solid var(--border,#334155); border-radius:12px; padding:20px; width:420px; max-width:90%; max-height:80%; overflow-y:auto; }
    .fh-modal h3 { margin:0 0 16px 0; font-size:16px; display:flex; align-items:center; gap:8px; }
    .fh-field { margin-bottom:12px; }
    .fh-field label { display:block; font-size:12px; opacity:.6; margin-bottom:4px; }
    .fh-field input, .fh-field textarea, .fh-field select { width:100%; background:var(--bg-primary,#0f172a); border:1px solid var(--border,#334155); color:inherit; padding:8px 10px; border-radius:6px; font-size:13px; font-family:inherit; box-sizing:border-box; }
    .fh-field textarea { min-height:80px; resize:vertical; }
    .fh-field select { cursor:pointer; }
    .fh-btn-row { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
    .fh-btn { padding:8px 16px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-family:inherit; transition:opacity .15s; }
    .fh-btn:hover { opacity:.85; }
    .fh-btn.primary { background:#f472b6; color:#fff; }
    .fh-btn.danger { background:#ef4444; color:#fff; }
    .fh-btn.ghost { background:transparent; color:var(--text-primary,#e2e8f0); border:1px solid var(--border,#334155); }
    .fh-btn.sm { padding:4px 10px; font-size:12px; }

    /* Shopping */
    .fh-list-card { background:var(--bg-secondary,#1e293b); border:1px solid var(--border,#334155); border-radius:10px; margin-bottom:12px; overflow:hidden; }
    .fh-list-header { padding:10px 14px; display:flex; align-items:center; gap:8px; cursor:pointer; }
    .fh-list-header .fh-list-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
    .fh-list-header .fh-list-name { font-weight:600; font-size:14px; flex:1; }
    .fh-list-header .fh-list-count { font-size:12px; opacity:.5; }
    .fh-list-header .fh-list-del { opacity:.3; cursor:pointer; padding:4px; }
    .fh-list-header .fh-list-del:hover { opacity:.8; color:#ef4444; }
    .fh-list-items { padding:0 14px 10px; }
    .fh-item { display:flex; align-items:center; gap:8px; padding:5px 0; border-bottom:1px solid rgba(255,255,255,.04); font-size:13px; }
    .fh-item:last-child { border-bottom:none; }
    .fh-item input[type=checkbox] { accent-color:#f472b6; cursor:pointer; flex-shrink:0; width:16px; height:16px; }
    .fh-item .fh-item-name { flex:1; }
    .fh-item .fh-item-name.checked { text-decoration:line-through; opacity:.4; }
    .fh-item .fh-item-cat { font-size:10px; opacity:.4; background:rgba(255,255,255,.05); padding:1px 6px; border-radius:4px; }
    .fh-item .fh-item-who { font-size:10px; opacity:.3; }
    .fh-item .fh-item-del { opacity:.2; cursor:pointer; font-size:12px; }
    .fh-item .fh-item-del:hover { opacity:.7; color:#ef4444; }
    .fh-add-input { display:flex; gap:6px; padding:6px 0; }
    .fh-add-input input { flex:1; background:var(--bg-primary,#0f172a); border:1px solid var(--border,#334155); color:inherit; padding:6px 10px; border-radius:6px; font-size:13px; font-family:inherit; }
    .fh-add-input select { background:var(--bg-primary,#0f172a); border:1px solid var(--border,#334155); color:inherit; padding:6px; border-radius:6px; font-size:11px; cursor:pointer; }

    /* Chores */
    .fh-chore { display:flex; align-items:center; gap:10px; padding:10px 14px; background:var(--bg-secondary,#1e293b); border:1px solid var(--border,#334155); border-radius:8px; margin-bottom:8px; }
    .fh-chore .fh-chore-done { width:32px; height:32px; border-radius:50%; border:2px solid var(--border,#334155); display:flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; transition:all .2s; font-size:14px; }
    .fh-chore .fh-chore-done:hover { border-color:#22c55e; color:#22c55e; }
    .fh-chore .fh-chore-done.is-done { border-color:#22c55e; background:#22c55e; color:#fff; }
    .fh-chore .fh-chore-info { flex:1; min-width:0; }
    .fh-chore .fh-chore-title { font-weight:600; font-size:14px; }
    .fh-chore .fh-chore-sub { font-size:11px; opacity:.5; display:flex; gap:8px; flex-wrap:wrap; margin-top:2px; }
    .fh-chore .fh-streak { background:rgba(234,179,8,.15); color:#eab308; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
    .fh-chore .fh-chore-actions { display:flex; gap:4px; }
    .fh-chore .fh-chore-actions button { background:none; border:none; color:inherit; opacity:.3; cursor:pointer; padding:4px; font-size:13px; }
    .fh-chore .fh-chore-actions button:hover { opacity:.8; }

    /* Calendar */
    .fh-cal-nav { display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }
    .fh-cal-nav button { background:none; border:1px solid var(--border,#334155); color:inherit; padding:6px 12px; border-radius:6px; cursor:pointer; }
    .fh-cal-nav button:hover { background:rgba(255,255,255,.05); }
    .fh-cal-nav .fh-cal-title { font-size:16px; font-weight:600; }
    .fh-cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:2px; }
    .fh-cal-hdr { text-align:center; font-size:11px; opacity:.4; padding:6px 0; font-weight:600; }
    .fh-cal-day { min-height:72px; background:var(--bg-secondary,#1e293b); border:1px solid var(--border,#334155); border-radius:6px; padding:4px; font-size:11px; cursor:pointer; position:relative; transition:border-color .15s; }
    .fh-cal-day:hover { border-color:rgba(244,114,182,.3); }
    .fh-cal-day.other { opacity:.25; }
    .fh-cal-day.today { border-color:#f472b6; }
    .fh-cal-day .fh-day-num { font-weight:600; margin-bottom:2px; }
    .fh-cal-day .fh-day-ev { font-size:10px; padding:1px 4px; border-radius:3px; margin-bottom:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#fff; }

    /* Color picker row */
    .fh-colors { display:flex; gap:6px; }
    .fh-colors .fh-cdot { width:24px; height:24px; border-radius:50%; cursor:pointer; border:2px solid transparent; transition:border-color .15s; }
    .fh-colors .fh-cdot.active { border-color:#fff; }
    </style>`;

    // ── Render shell ──
    const body = root;
    body.style.position = 'relative';
    body.innerHTML = CSS + `
        <div class="fh-wrap">
            <div class="fh-tabs">
                <div class="fh-tab active" data-tab="board"><i class="fas fa-clipboard-list"></i>${t('Tablica')}</div>
                <div class="fh-tab" data-tab="shopping"><i class="fas fa-cart-shopping"></i>${t('Zakupy')}<span class="fh-badge" id="fh-badge-shopping" style="display:none"></span></div>
                <div class="fh-tab" data-tab="chores"><i class="fas fa-tasks"></i>${t('Zadania')}<span class="fh-badge" id="fh-badge-chores" style="display:none"></span></div>
                <div class="fh-tab" data-tab="calendar"><i class="fas fa-calendar-alt"></i>${t('Kalendarz')}</div>
            </div>
            <div class="fh-body" id="fh-content"></div>
            <button class="fh-fab" id="fh-fab" title="${t('Dodaj nowy')}"><i class="fas fa-plus"></i></button>
        </div>`;

    const content = body.querySelector('#fh-content');
    const fab = body.querySelector('#fh-fab');
    const tabs = body.querySelectorAll('.fh-tab');

    // ── Tab switching ──
    tabs.forEach(tab => tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentTab = tab.dataset.tab;
        loadTab();
    }));

    fab.addEventListener('click', () => {
        if (currentTab === 'board') boardAdd();
        else if (currentTab === 'shopping') shoppingAddList();
        else if (currentTab === 'chores') choresAdd();
        else if (currentTab === 'calendar') calendarAdd();
    });

    // Load users + first tab
    api(API + '/users').then(r => { hubUsers = r.users || []; }).catch(() => {}).finally(() => loadTab());

    function loadTab() {
        if (currentTab === 'board') boardLoad();
        else if (currentTab === 'shopping') shoppingLoad();
        else if (currentTab === 'chores') choresLoad();
        else if (currentTab === 'calendar') calendarLoad();
    }

    // ── Modal helper ──
    function showModal(title, fields, onSave, existing) {
        const ov = document.createElement('div');
        ov.className = 'fh-modal-overlay';
        let html = `<div class="fh-modal"><h3><i class="fas fa-pen" style="color:#f472b6"></i> ${esc(title)}</h3>`;
        for (const f of fields) {
            html += `<div class="fh-field"><label>${esc(f.label)}</label>`;
            if (f.type === 'textarea') {
                html += `<textarea id="fh-m-${f.key}" placeholder="${esc(f.placeholder || '')}" maxlength="${f.max || 5000}">${esc(f.value || '')}</textarea>`;
            } else if (f.type === 'select') {
                html += `<select id="fh-m-${f.key}">${f.options.map(o => `<option value="${esc(o.value)}" ${o.value === f.value ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select>`;
            } else if (f.type === 'colors') {
                html += `<div class="fh-colors" id="fh-m-${f.key}">${f.options.map(c => `<div class="fh-cdot ${c === f.value ? 'active' : ''}" data-val="${c}" style="background:${c}"></div>`).join('')}</div>`;
            } else {
                html += `<input type="${f.type || 'text'}" id="fh-m-${f.key}" value="${esc(f.value || '')}" placeholder="${esc(f.placeholder || '')}" maxlength="${f.max || 200}">`;
            }
            html += `</div>`;
        }
        html += `<div class="fh-btn-row">`;
        if (existing) html += `<button class="fh-btn danger" id="fh-m-del"><i class="fas fa-trash"></i> ${t('Usuń')}</button><div style="flex:1"></div>`;
        html += `<button class="fh-btn ghost" id="fh-m-cancel">${t('Anuluj')}</button>`;
        html += `<button class="fh-btn primary" id="fh-m-save"><i class="fas fa-check"></i> ${t('Zapisz')}</button>`;
        html += `</div></div>`;
        ov.innerHTML = html;
        body.appendChild(ov);

        // Color picker clicks
        ov.querySelectorAll('.fh-colors .fh-cdot').forEach(dot => {
            dot.addEventListener('click', () => {
                dot.parentElement.querySelectorAll('.fh-cdot').forEach(d => d.classList.remove('active'));
                dot.classList.add('active');
            });
        });

        ov.addEventListener('click', e => { if (e.target === ov) ov.remove(); });
        ov.querySelector('#fh-m-cancel').addEventListener('click', () => ov.remove());
        ov.querySelector('#fh-m-save').addEventListener('click', async () => {
            const data = {};
            for (const f of fields) {
                if (f.type === 'colors') {
                    data[f.key] = ov.querySelector(`#fh-m-${f.key} .fh-cdot.active`)?.dataset.val || f.value;
                } else {
                    data[f.key] = ov.querySelector(`#fh-m-${f.key}`)?.value || '';
                }
            }
            ov.querySelector('#fh-m-save').disabled = true;
            try {
                await onSave(data);
                ov.remove();
            } catch (e) {
                toast(t('Błąd: ') + e.message, 'error');
                ov.querySelector('#fh-m-save').disabled = false;
            }
        });
        if (existing && ov.querySelector('#fh-m-del')) {
            ov.querySelector('#fh-m-del').addEventListener('click', async () => {
                if (!await confirmDialog(t('Na pewno usunąć?'))) return;
                try {
                    await existing.onDelete();
                    ov.remove();
                } catch (e) { toast(t('Błąd: ') + e.message, 'error'); }
            });
        }
        setTimeout(() => { const first = ov.querySelector('input,textarea'); if (first) first.focus(); }, 50);
        return ov;
    }

    /* ═══════════════════════════════════════════════════════════
       ${t('TABLICA OGŁOSZEŃ')}
       ═══════════════════════════════════════════════════════════ */
    let posts = [];
    let _boardClickHandler = null;

    async function boardLoad() {
        content.innerHTML = '<div class="fh-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        try {
            const r = await api(API + '/posts');
            posts = r.posts || [];
            boardRender();
        } catch (e) {
            content.innerHTML = `<div class="fh-empty"><i class="fas fa-exclamation-triangle"></i><p>${esc(e.message)}</p></div>`;
        }
    }

    function boardRender() {
        if (!posts.length) {
            content.innerHTML = `<div class="fh-empty"><i class="fas fa-clipboard-list"></i><p>${t('Brak ogłoszeń')}</p><p style="font-size:13px">${t('Kliknij + aby dodać pierwsze ogłoszenie')}</p></div>`;
            return;
        }
        let html = '';
        for (const p of posts) {
            const col = getColor(p.color);
            const initials = (p.author || '?').slice(0, 2).toUpperCase();
            const uc = getUserColor(p.author || '');
            html += `<div class="fh-card" data-id="${p.id}" style="border-left:3px solid ${col.accent}">
                <div class="fh-pin ${p.pinned ? 'pinned' : ''}" data-action="pin" data-id="${p.id}" title="${p.pinned ? 'Odepnij' : 'Przypnij'}"><i class="fas fa-thumbtack"></i></div>
                <div class="fh-card-header">
                    <div class="fh-avatar" style="background:${uc}">${initials}</div>
                    <div><strong>${esc(p.author)}</strong> <span class="fh-meta">${timeAgo(p.created_at)}</span></div>
                </div>
                <div class="fh-card-title">${esc(p.title)}</div>
                ${p.content ? `<div class="fh-card-content">${esc(p.content)}</div>` : ''}
                <div class="fh-reactions">
                    ${(p.reactions || []).map(r => {
                        const users = (r.users || '').split(',');
                        const isMine = users.includes(NAS?.username || '');
                        return `<button class="fh-react-btn ${isMine ? 'mine' : ''}" data-action="react" data-id="${p.id}" data-emoji="${r.emoji}" title="${users.join(', ')}">${r.emoji} ${users.length}</button>`;
                    }).join('')}
                    <span class="fh-react-add" data-action="react-pick" data-id="${p.id}" title="${t('Dodaj reakcję')}">➕</span>
                </div>
                <div class="fh-card-actions">
                    <button class="fh-btn sm ghost" data-action="edit-post" data-id="${p.id}"><i class="fas fa-pen"></i></button>
                </div>
            </div>`;
        }
        content.innerHTML = html;

        // Event delegation — remove old handler to prevent stacking
        if (_boardClickHandler) content.removeEventListener('click', _boardClickHandler);
        _boardClickHandler = function(e) {
            const btn = e.target.closest('[data-action]');
            if (!btn) return;
            const action = btn.dataset.action;
            const id = btn.dataset.id;
            if (action === 'pin') boardPin(id);
            else if (action === 'react') boardReact(id, btn.dataset.emoji);
            else if (action === 'react-pick') boardReactPicker(btn, id);
            else if (action === 'edit-post') boardEdit(id);
        };
        content.addEventListener('click', _boardClickHandler);
    }

    function boardAdd() {
        showModal(t('Nowe ogłoszenie'), [
            { key: 'title', label: t('Tytuł'), placeholder: t('np. Spotkanie rodzinne w sobotę'), max: 200 },
            { key: 'content', label: t('Treść'), type: 'textarea', placeholder: t('Szczegóły…') },
            { key: 'color', label: t('Kolor'), type: 'select', value: 'blue', options: COLORS.map(c => ({ value: c.id, label: c.id.charAt(0).toUpperCase() + c.id.slice(1) })) },
        ], async (data) => {
            await api(API + '/posts', { method: 'POST', body: data });
            boardLoad();
            toast(t('Ogłoszenie dodane!'), 'success');
        });
    }

    function boardEdit(id) {
        const p = posts.find(x => x.id === id);
        if (!p) return;
        showModal(t('Edytuj ogłoszenie'), [
            { key: 'title', label: t('Tytuł'), value: p.title, max: 200 },
            { key: 'content', label: t('Treść'), type: 'textarea', value: p.content },
            { key: 'color', label: t('Kolor'), type: 'select', value: p.color, options: COLORS.map(c => ({ value: c.id, label: c.id.charAt(0).toUpperCase() + c.id.slice(1) })) },
        ], async (data) => {
            await api(API + '/posts/' + id, { method: 'PUT', body: data });
            boardLoad();
            toast(t('Zapisano'), 'success');
        }, {
            onDelete: async () => {
                await api(API + '/posts/' + id, { method: 'DELETE' });
                boardLoad();
                toast(t('Ogłoszenie usunięte'), 'success');
            }
        });
    }

    async function boardPin(id) {
        const p = posts.find(x => x.id === id);
        if (!p) return;
        try {
            await api(API + '/posts/' + id, { method: 'PUT', body: { pinned: !p.pinned } });
            boardLoad();
        } catch (e) { toast(e.message, 'error'); }
    }

    async function boardReact(id, emoji) {
        try {
            const r = await api(API + '/posts/' + id + '/react', { method: 'POST', body: { emoji } });
            const p = posts.find(x => x.id === id);
            if (p) p.reactions = r.reactions;
            boardRender();
        } catch (e) { toast(e.message, 'error'); }
    }

    function boardReactPicker(btn, id) {
        const existing = content.querySelector('.fh-react-picker');
        if (existing) existing.remove();
        const picker = document.createElement('div');
        picker.className = 'fh-react-picker';
        picker.innerHTML = EMOJIS.map(e => `<span data-emoji="${e}">${e}</span>`).join('');
        btn.style.position = 'relative';
        btn.appendChild(picker);
        picker.addEventListener('click', e => {
            const em = e.target.dataset.emoji;
            if (em) { boardReact(id, em); picker.remove(); cleanup(); }
        });
        function cleanup() { document.removeEventListener('click', dismissHandler); }
        function dismissHandler(e) {
            if (!picker.contains(e.target) && e.target !== btn) { picker.remove(); cleanup(); }
        }
        setTimeout(() => document.addEventListener('click', dismissHandler), 10);
    }

    /* ═══════════════════════════════════════════════════════════
       ${t('LISTY ZAKUPÓW')}
       ═══════════════════════════════════════════════════════════ */
    let shoppingLists = [];

    async function shoppingLoad() {
        content.innerHTML = '<div class="fh-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        try {
            const r = await api(API + '/lists');
            shoppingLists = r.lists || [];
            shoppingRender();
            _updateBadge('shopping', shoppingLists.reduce((n, l) => n + (l.items || []).filter(i => !i.checked).length, 0));
        } catch (e) {
            content.innerHTML = `<div class="fh-empty"><i class="fas fa-exclamation-triangle"></i><p>${esc(e.message)}</p></div>`;
        }
    }

    function shoppingRender() {
        if (!shoppingLists.length) {
            content.innerHTML = `<div class="fh-empty"><i class="fas fa-cart-shopping"></i><p>${t('Brak list zakupów')}</p><p style="font-size:13px">${t('Kliknij + aby utworzyć pierwszą listę')}</p></div>`;
            return;
        }
        let html = '';
        for (const lst of shoppingLists) {
            const unchecked = (lst.items || []).filter(i => !i.checked).length;
            const total = (lst.items || []).length;
            html += `<div class="fh-list-card">
                <div class="fh-list-header">
                    <div class="fh-list-dot" style="background:${lst.color}"></div>
                    <div class="fh-list-name">${esc(lst.name)}</div>
                    <div class="fh-list-count">${unchecked}/${total}</div>
                    <span class="fh-list-del" data-action="del-list" data-id="${lst.id}" title="${t('Usuń listę')}"><i class="fas fa-trash"></i></span>
                </div>
                <div class="fh-list-items">`;
            for (const item of (lst.items || [])) {
                html += `<div class="fh-item">
                    <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-item" data-list="${lst.id}" data-id="${item.id}">
                    <span class="fh-item-name ${item.checked ? 'checked' : ''}">${esc(item.name)}</span>
                    ${item.category ? `<span class="fh-item-cat">${esc(item.category)}</span>` : ''}
                    <span class="fh-item-who">${esc(item.checked ? (item.checked_by || '') : (item.added_by || ''))}</span>
                    <span class="fh-item-del" data-action="del-item" data-list="${lst.id}" data-id="${item.id}"><i class="fas fa-times"></i></span>
                </div>`;
            }
            html += `<div class="fh-add-input">
                    <input type="text" placeholder="${t('Dodaj produkt… (Enter)')}" data-action="add-item-input" data-list="${lst.id}">
                    <select data-action="add-item-cat" data-list="${lst.id}">${CATEGORIES.map(c => `<option value="${c}">${c || '—'}</option>`).join('')}</select>
                </div>`;
            html += `</div></div>`;
        }
        content.innerHTML = html;

        // Events
        content.querySelectorAll('[data-action="toggle-item"]').forEach(cb => {
            cb.addEventListener('change', () => shoppingToggle(cb.dataset.list, cb.dataset.id, cb.checked));
        });
        content.querySelectorAll('[data-action="del-item"]').forEach(btn => {
            btn.addEventListener('click', () => shoppingDelItem(btn.dataset.list, btn.dataset.id));
        });
        content.querySelectorAll('[data-action="del-list"]').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (await confirmDialog(t('Usunąć całą listę?'))) shoppingDelList(btn.dataset.id);
            });
        });
        content.querySelectorAll('[data-action="add-item-input"]').forEach(inp => {
            inp.addEventListener('keydown', e => {
                if (e.key === 'Enter' && inp.value.trim()) {
                    const cat = content.querySelector(`select[data-list="${inp.dataset.list}"]`)?.value || '';
                    shoppingAddItem(inp.dataset.list, inp.value.trim(), cat);
                    inp.value = '';
                }
            });
        });
    }

    function shoppingAddList() {
        showModal(t('Nowa lista zakupów'), [
            { key: 'name', label: t('Nazwa listy'), placeholder: t('np. Biedronka, Castorama…') },
            { key: 'color', label: t('Kolor'), type: 'colors', value: '#3b82f6', options: LIST_COLORS },
        ], async (data) => {
            await api(API + '/lists', { method: 'POST', body: data });
            shoppingLoad();
            toast(t('Lista utworzona!'), 'success');
        });
    }

    async function shoppingAddItem(listId, name, category) {
        try {
            await api(API + '/lists/' + listId + '/items', { method: 'POST', body: { name, category } });
            shoppingLoad();
        } catch (e) { toast(e.message, 'error'); }
    }

    async function shoppingToggle(listId, itemId, checked) {
        try {
            await api(API + '/lists/' + listId + '/items/' + itemId, { method: 'PUT', body: { checked } });
            shoppingLoad();
        } catch (e) { toast(e.message, 'error'); }
    }

    async function shoppingDelItem(listId, itemId) {
        try {
            await api(API + '/lists/' + listId + '/items/' + itemId, { method: 'DELETE' });
            shoppingLoad();
        } catch (e) { toast(e.message, 'error'); }
    }

    async function shoppingDelList(listId) {
        try {
            await api(API + '/lists/' + listId, { method: 'DELETE' });
            shoppingLoad();
            toast(t('Lista usunięta'), 'success');
        } catch (e) { toast(e.message, 'error'); }
    }

    /* ═══════════════════════════════════════════════════════════
       ZADANIA DOMOWE
       ═══════════════════════════════════════════════════════════ */
    let chores = [];

    async function choresLoad() {
        content.innerHTML = '<div class="fh-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        try {
            const r = await api(API + '/chores');
            chores = r.chores || [];
            choresRender();
            _updateBadge('chores', chores.filter(c => c.status === 'pending').length);
        } catch (e) {
            content.innerHTML = `<div class="fh-empty"><i class="fas fa-exclamation-triangle"></i><p>${esc(e.message)}</p></div>`;
        }
    }

    function choresRender() {
        if (!chores.length) {
            content.innerHTML = `<div class="fh-empty"><i class="fas fa-tasks"></i><p>${t('Brak zadań')}</p><p style="font-size:13px">${t('Kliknij + aby dodać pierwsze zadanie')}</p></div>`;
            return;
        }
        let html = '';
        for (const ch of chores) {
            const isDone = ch.status === 'done';
            const uc = ch.assigned_to ? getUserColor(ch.assigned_to) : 'transparent';
            html += `<div class="fh-chore">
                <div class="fh-chore-done ${isDone ? 'is-done' : ''}" data-action="chore-done" data-id="${ch.id}" title="${isDone ? 'Zrobione' : 'Oznacz jako zrobione'}">
                    ${isDone ? '<i class="fas fa-check"></i>' : ''}
                </div>
                <div class="fh-chore-info">
                    <div class="fh-chore-title" style="${isDone ? 'text-decoration:line-through;opacity:.4' : ''}">${esc(ch.title)}</div>
                    <div class="fh-chore-sub">
                        ${ch.assigned_to ? `<span><i class="fas fa-user" style="color:${uc}"></i> ${esc(ch.assigned_to)}</span>` : ''}
                        <span><i class="fas fa-redo"></i> ${RECURRENCE[ch.recurrence] || ch.recurrence}</span>
                        ${ch.due_date ? `<span><i class="fas fa-calendar"></i> ${ch.due_date}</span>` : ''}
                    </div>
                </div>
                ${ch.streak > 0 ? `<span class="fh-streak">🔥 ${ch.streak}</span>` : ''}
                <div class="fh-chore-actions">
                    <button data-action="edit-chore" data-id="${ch.id}"><i class="fas fa-pen"></i></button>
                </div>
            </div>`;
        }
        content.innerHTML = html;

        content.querySelectorAll('[data-action="chore-done"]').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    await api(API + '/chores/' + btn.dataset.id + '/done', { method: 'POST' });
                    choresLoad();
                    toast('✅ ' + t('Zrobione!'), 'success');
                } catch (e) { toast(e.message, 'error'); }
            });
        });
        content.querySelectorAll('[data-action="edit-chore"]').forEach(btn => {
            btn.addEventListener('click', () => choresEdit(btn.dataset.id));
        });
    }

    function choresAdd() {
        showModal(t('Nowe zadanie'), [
            { key: 'title', label: t('Zadanie'), placeholder: t('np. Wynieść śmieci') },
            { key: 'assigned_to', label: t('Przypisz do'), type: 'select', value: '', options: [{ value: '', label: '— nikogo —' }, ...hubUsers.map(u => ({ value: u, label: u }))] },
            { key: 'recurrence', label: t('Powtarzalność'), type: 'select', value: 'once', options: Object.entries(RECURRENCE).map(([v, l]) => ({ value: v, label: l })) },
            { key: 'due_date', label: t('Termin'), type: 'date' },
        ], async (data) => {
            await api(API + '/chores', { method: 'POST', body: data });
            choresLoad();
            toast(t('Zadanie dodane!'), 'success');
        });
    }

    function choresEdit(id) {
        const ch = chores.find(x => x.id === id);
        if (!ch) return;
        showModal(t('Edytuj zadanie'), [
            { key: 'title', label: t('Zadanie'), value: ch.title },
            { key: 'assigned_to', label: t('Przypisz do'), type: 'select', value: ch.assigned_to, options: [{ value: '', label: '— nikogo —' }, ...hubUsers.map(u => ({ value: u, label: u }))] },
            { key: 'recurrence', label: t('Powtarzalność'), type: 'select', value: ch.recurrence, options: Object.entries(RECURRENCE).map(([v, l]) => ({ value: v, label: l })) },
            { key: 'status', label: t('Status'), type: 'select', value: ch.status, options: [{ value: 'pending', label: 'Do zrobienia' }, { value: 'in_progress', label: 'W trakcie' }, { value: 'done', label: 'Zrobione' }] },
            { key: 'due_date', label: t('Termin'), type: 'date', value: ch.due_date },
        ], async (data) => {
            await api(API + '/chores/' + id, { method: 'PUT', body: data });
            choresLoad();
            toast(t('Zapisano'), 'success');
        }, {
            onDelete: async () => {
                await api(API + '/chores/' + id, { method: 'DELETE' });
                choresLoad();
                toast(t('Zadanie usunięte'), 'success');
            }
        });
    }

    /* ═══════════════════════════════════════════════════════════
       KALENDARZ RODZINNY
       ═══════════════════════════════════════════════════════════ */
    let calEvents = [];
    let calYear = new Date().getFullYear();
    let calMonth = new Date().getMonth(); // 0-based

    async function calendarLoad() {
        const monthStr = `${calYear}-${String(calMonth + 1).padStart(2, '0')}`;
        content.innerHTML = '<div class="fh-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        try {
            const r = await api(API + '/events?month=' + monthStr);
            calEvents = r.events || [];
            calendarRender();
        } catch (e) {
            content.innerHTML = `<div class="fh-empty"><i class="fas fa-exclamation-triangle"></i><p>${esc(e.message)}</p></div>`;
        }
    }

    function calendarRender() {
        const MONTHS_PL = [t('Styczeń'),t('Luty'),t('Marzec'),t('Kwiecień'),t('Maj'),t('Czerwiec'),t('Lipiec'),t('Sierpień'),t('Wrzesień'),t('Październik'),t('Listopad'),t('Grudzień')];
        const DAYS_PL = [t('Pon'),t('Wto'),t('Śro'),t('Czw'),t('Pią'),t('Sob'),t('Nie')];
        const today = new Date();
        const todayStr = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;

        const firstDay = new Date(calYear, calMonth, 1);
        let startDow = firstDay.getDay() - 1; // Monday=0
        if (startDow < 0) startDow = 6;
        const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();

        let html = `<div class="fh-cal-nav">
            <button id="fh-cal-prev"><i class="fas fa-chevron-left"></i></button>
            <span class="fh-cal-title">${MONTHS_PL[calMonth]} ${calYear}</span>
            <button id="fh-cal-next"><i class="fas fa-chevron-right"></i></button>
        </div>`;
        html += `<div class="fh-cal-grid">`;
        for (const d of DAYS_PL) html += `<div class="fh-cal-hdr">${d}</div>`;

        // Previous month fill
        const prevDays = new Date(calYear, calMonth, 0).getDate();
        for (let i = startDow - 1; i >= 0; i--) {
            html += `<div class="fh-cal-day other"><div class="fh-day-num">${prevDays - i}</div></div>`;
        }

        // Current month
        for (let d = 1; d <= daysInMonth; d++) {
            const dateStr = `${calYear}-${String(calMonth + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
            const isToday = dateStr === todayStr;
            const dayEvents = calEvents.filter(e => e.event_date === dateStr);
            html += `<div class="fh-cal-day ${isToday ? 'today' : ''}" data-date="${dateStr}">
                <div class="fh-day-num">${d}</div>
                ${dayEvents.slice(0, 3).map(ev => `<div class="fh-day-ev" style="background:${ev.color}" title="${esc(ev.title)}">${esc(ev.title)}</div>`).join('')}
                ${dayEvents.length > 3 ? `<div style="font-size:10px;opacity:.4">+${dayEvents.length - 3}</div>` : ''}
            </div>`;
        }

        // Next month fill
        const totalCells = startDow + daysInMonth;
        const remaining = (7 - (totalCells % 7)) % 7;
        for (let i = 1; i <= remaining; i++) {
            html += `<div class="fh-cal-day other"><div class="fh-day-num">${i}</div></div>`;
        }
        html += `</div>`;
        content.innerHTML = html;

        content.querySelector('#fh-cal-prev').addEventListener('click', () => {
            calMonth--;
            if (calMonth < 0) { calMonth = 11; calYear--; }
            calendarLoad();
        });
        content.querySelector('#fh-cal-next').addEventListener('click', () => {
            calMonth++;
            if (calMonth > 11) { calMonth = 0; calYear++; }
            calendarLoad();
        });
        content.querySelectorAll('.fh-cal-day[data-date]').forEach(cell => {
            cell.addEventListener('click', () => {
                const date = cell.dataset.date;
                const dayEvs = calEvents.filter(e => e.event_date === date);
                if (dayEvs.length) calendarDayView(date, dayEvs);
                else calendarAdd(date);
            });
        });
    }

    function calendarAdd(prefillDate) {
        showModal(t('Nowe wydarzenie'), [
            { key: 'title', label: t('Tytuł'), placeholder: t('np. Urodziny Ani') },
            { key: 'event_date', label: t('Data'), type: 'date', value: prefillDate || '' },
            { key: 'event_time', label: t('Godzina (opcjonalnie)'), type: 'time' },
            { key: 'description', label: t('Opis'), type: 'textarea', placeholder: t('Szczegóły…') },
            { key: 'color', label: t('Kolor'), type: 'colors', value: '#f472b6', options: LIST_COLORS },
        ], async (data) => {
            await api(API + '/events', { method: 'POST', body: data });
            calendarLoad();
            toast(t('Wydarzenie dodane!'), 'success');
        });
    }

    function calendarDayView(date, events) {
        const ov = document.createElement('div');
        ov.className = 'fh-modal-overlay';
        let html = `<div class="fh-modal"><h3><i class="fas fa-calendar-day" style="color:#f472b6"></i> ${date}</h3>`;
        for (const ev of events) {
            html += `<div class="fh-card" style="border-left:3px solid ${ev.color}">
                <div class="fh-card-title">${esc(ev.title)}</div>
                ${ev.event_time ? `<div style="font-size:12px;opacity:.6"><i class="fas fa-clock"></i> ${esc(ev.event_time)}</div>` : ''}
                ${ev.description ? `<div class="fh-card-content" style="margin-top:6px">${esc(ev.description)}</div>` : ''}
                <div class="fh-card-actions" style="margin-top:8px">
                    <button class="fh-btn sm ghost" data-action="edit-ev" data-id="${ev.id}"><i class="fas fa-pen"></i> ${t('Edytuj')}</button>
                    <button class="fh-btn sm danger" data-action="del-ev" data-id="${ev.id}"><i class="fas fa-trash"></i></button>
                </div>
            </div>`;
        }
        html += `<div class="fh-btn-row"><button class="fh-btn ghost" id="fh-dv-close">${t('Zamknij')}</button>
            <button class="fh-btn primary" id="fh-dv-add"><i class="fas fa-plus"></i> ${t('Dodaj')}</button></div></div>`;
        ov.innerHTML = html;
        body.appendChild(ov);
        ov.addEventListener('click', e => { if (e.target === ov) ov.remove(); });
        ov.querySelector('#fh-dv-close').addEventListener('click', () => ov.remove());
        ov.querySelector('#fh-dv-add').addEventListener('click', () => { ov.remove(); calendarAdd(date); });
        ov.querySelectorAll('[data-action="edit-ev"]').forEach(btn => {
            btn.addEventListener('click', () => { ov.remove(); calendarEdit(btn.dataset.id); });
        });
        ov.querySelectorAll('[data-action="del-ev"]').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!await confirmDialog(t('Usunąć wydarzenie?'))) return;
                try {
                    await api(API + '/events/' + btn.dataset.id, { method: 'DELETE' });
                    ov.remove();
                    calendarLoad();
                    toast(t('Wydarzenie usunięte'), 'success');
                } catch (e) { toast(e.message, 'error'); }
            });
        });
    }

    function calendarEdit(id) {
        const ev = calEvents.find(x => x.id === id);
        if (!ev) return;
        showModal(t('Edytuj wydarzenie'), [
            { key: 'title', label: t('Tytuł'), value: ev.title },
            { key: 'event_date', label: t('Data'), type: 'date', value: ev.event_date },
            { key: 'event_time', label: t('Godzina'), type: 'time', value: ev.event_time },
            { key: 'description', label: t('Opis'), type: 'textarea', value: ev.description },
            { key: 'color', label: t('Kolor'), type: 'colors', value: ev.color, options: LIST_COLORS },
        ], async (data) => {
            await api(API + '/events/' + id, { method: 'PUT', body: data });
            calendarLoad();
            toast(t('Zapisano'), 'success');
        }, {
            onDelete: async () => {
                await api(API + '/events/' + id, { method: 'DELETE' });
                calendarLoad();
                toast(t('Wydarzenie usunięte'), 'success');
            }
        });
    }

    /* ═══════════════════════════════════════════════════════════
       BADGE HELPER
       ═══════════════════════════════════════════════════════════ */
    function _updateBadge(tab, count) {
        const badge = body.querySelector(`#fh-badge-${tab}`);
        if (!badge) return;
        if (count > 0) { badge.textContent = count; badge.style.display = ''; }
        else { badge.style.display = 'none'; }
    }
}
