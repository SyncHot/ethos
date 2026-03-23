/* ═══════════════════════════════════════════════════════════
   EthOS — Websites (Kreator stron / Prosty CMS)
   ═══════════════════════════════════════════════════════════ */

AppRegistry['websites'] = function (appDef) {
    createWindow('websites', {
        title: t('Kreator stron'),
        icon: appDef.icon || 'fa-globe',
        iconColor: appDef.color || '#14b8a6',
        width: 1100,
        height: 720,
        onRender: (body) => renderWebsitesApp(body),
    });
};

function renderWebsitesApp(body) {

    /* ── helpers ── */
    const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

    /* ── state ── */
    let sites = [];
    let currentSite = null;    // full site object when editing
    let currentPage = null;    // current page slug being edited
    let templates = [];
    let themes = [];
    let view = 'list';         // 'list' | 'create' | 'edit'

    /* ── CSS ── */
    body.innerHTML = `
    <style>
        .ws-app { height:100%; display:flex; flex-direction:column; background:var(--bg-body,#0f172a); color:var(--text,#e2e8f0); font-family:'Segoe UI',system-ui,sans-serif; overflow:hidden; }

        /* Header bar */
        .ws-header { display:flex; align-items:center; gap:12px; padding:12px 18px; background:var(--bg-card,#1e293b); border-bottom:1px solid var(--border,#334155); flex-shrink:0; }
        .ws-header h2 { margin:0; font-size:16px; font-weight:600; flex:1; display:flex; align-items:center; gap:8px; }
        .ws-btn { padding:7px 16px; border:none; border-radius:8px; font-size:13px; cursor:pointer; display:inline-flex; align-items:center; gap:6px; transition:all .15s; font-weight:500; }
        .ws-btn-primary { background:#14b8a6; color:#fff; }
        .ws-btn-primary:hover { background:#0d9488; }
        .ws-btn-secondary { background:var(--bg-hover,#334155); color:var(--text,#e2e8f0); }
        .ws-btn-secondary:hover { background:var(--border,#475569); }
        .ws-btn-danger { background:#dc2626; color:#fff; }
        .ws-btn-danger:hover { background:#b91c1c; }
        .ws-btn-sm { padding:5px 10px; font-size:12px; }
        .ws-btn:disabled { opacity:.5; cursor:not-allowed; }

        /* Body area */
        .ws-body { flex:1; overflow-y:auto; padding:18px; }

        /* Site list */
        .ws-empty { text-align:center; padding:60px 20px; color:var(--text-muted,#64748b); }
        .ws-empty i { font-size:48px; display:block; margin-bottom:14px; opacity:.3; }
        .ws-site-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }
        .ws-site-card { background:var(--bg-card,#1e293b); border:1px solid var(--border,#334155); border-radius:12px; padding:18px; cursor:pointer; transition:all .15s; position:relative; }
        .ws-site-card:hover { border-color:#14b8a6; transform:translateY(-2px); box-shadow:0 4px 12px rgba(0,0,0,.2); }
        .ws-site-card h3 { margin:0 0 4px; font-size:15px; display:flex; align-items:center; gap:8px; }
        .ws-site-card .ws-meta { font-size:12px; color:var(--text-muted,#64748b); margin-top:6px; }
        .ws-badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; }
        .ws-badge-live { background:#059669; color:#fff; }
        .ws-badge-draft { background:#64748b; color:#fff; }
        .ws-site-actions { display:flex; gap:6px; margin-top:12px; }

        /* Create form */
        .ws-create { max-width:680px; margin:0 auto; }
        .ws-create h3 { margin:0 0 16px; font-size:16px; }
        .ws-form-row { margin-bottom:14px; }
        .ws-form-row label { display:block; font-size:13px; font-weight:500; margin-bottom:4px; color:var(--text-muted,#94a3b8); }
        .ws-form-row input, .ws-form-row select, .ws-form-row textarea { width:100%; padding:9px 12px; border:1px solid var(--border,#334155); border-radius:8px; background:var(--bg-body,#0f172a); color:var(--text,#e2e8f0); font-size:13px; }
        .ws-form-row textarea { min-height:60px; resize:vertical; }
        .ws-tpl-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; margin:10px 0; }
        .ws-tpl-card { background:var(--bg-card,#1e293b); border:2px solid var(--border,#334155); border-radius:10px; padding:16px; text-align:center; cursor:pointer; transition:all .15s; }
        .ws-tpl-card:hover { border-color:#14b8a6; }
        .ws-tpl-card.selected { border-color:#14b8a6; background:rgba(20,184,166,.1); }
        .ws-tpl-card i { font-size:28px; display:block; margin-bottom:8px; }
        .ws-tpl-card .tpl-name { font-weight:600; font-size:14px; }
        .ws-tpl-card .tpl-desc { font-size:11px; color:var(--text-muted,#64748b); margin-top:4px; }

        /* Editor layout */
        .ws-editor { display:flex; height:100%; overflow:hidden; }
        .ws-sidebar { width:220px; flex-shrink:0; background:var(--bg-card,#1e293b); border-right:1px solid var(--border,#334155); display:flex; flex-direction:column; overflow:hidden; }
        .ws-sidebar-header { padding:12px 14px; border-bottom:1px solid var(--border,#334155); font-size:13px; font-weight:600; display:flex; align-items:center; justify-content:space-between; }
        .ws-page-list { flex:1; overflow-y:auto; padding:6px; }
        .ws-page-item { padding:9px 12px; border-radius:8px; cursor:pointer; font-size:13px; display:flex; align-items:center; gap:8px; transition:background .1s; margin-bottom:2px; }
        .ws-page-item:hover { background:var(--bg-hover,#334155); }
        .ws-page-item.active { background:rgba(20,184,166,.15); color:#14b8a6; font-weight:600; }
        .ws-page-item i { font-size:11px; opacity:.6; }
        .ws-sidebar-section { padding:10px 14px; border-top:1px solid var(--border,#334155); }
        .ws-sidebar-section h4 { margin:0 0 8px; font-size:12px; text-transform:uppercase; color:var(--text-muted,#64748b); letter-spacing:.5px; }

        /* Main edit area */
        .ws-main { flex:1; display:flex; flex-direction:column; overflow:hidden; }
        .ws-toolbar { display:flex; align-items:center; gap:8px; padding:8px 14px; background:var(--bg-card,#1e293b); border-bottom:1px solid var(--border,#334155); flex-wrap:wrap; }
        .ws-toolbar-btn { padding:5px 8px; background:transparent; border:1px solid var(--border,#334155); border-radius:6px; color:var(--text,#e2e8f0); cursor:pointer; font-size:13px; transition:all .1s; }
        .ws-toolbar-btn:hover { background:var(--bg-hover,#334155); }
        .ws-toolbar-btn.active { background:#14b8a6; color:#fff; border-color:#14b8a6; }
        .ws-toolbar-sep { width:1px; height:20px; background:var(--border,#334155); margin:0 4px; }
        .ws-edit-area { flex:1; overflow:hidden; display:flex; }
        .ws-code-editor { flex:1; overflow:auto; resize:none; border:none; background:var(--bg-body,#0f172a); color:var(--text,#e2e8f0); font-family:'Fira Code','Consolas',monospace; font-size:13px; line-height:1.6; padding:16px; outline:none; white-space:pre-wrap; tab-size:2; }
        .ws-preview-frame { flex:1; border:none; background:#fff; }

        /* Settings panel */
        .ws-settings-panel { padding:16px; overflow-y:auto; flex:1; }
        .ws-settings-panel .ws-form-row { margin-bottom:12px; }

        /* Tabs in edit area */
        .ws-edit-tabs { display:flex; gap:0; border-bottom:1px solid var(--border,#334155); flex-shrink:0; }
        .ws-edit-tab { padding:8px 16px; font-size:13px; cursor:pointer; border-bottom:2px solid transparent; color:var(--text-muted,#64748b); transition:all .15s; background:transparent; border-top:none; border-left:none; border-right:none; }
        .ws-edit-tab:hover { color:var(--text,#e2e8f0); }
        .ws-edit-tab.active { color:#14b8a6; border-bottom-color:#14b8a6; font-weight:600; }

        /* Confirm modal */
        .ws-overlay { position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,.5); z-index:9999; display:flex; align-items:center; justify-content:center; }
        .ws-modal { background:var(--bg-card,#1e293b); border:1px solid var(--border,#334155); border-radius:12px; padding:24px; width:420px; max-width:95vw; }
        .ws-modal h3 { margin:0 0 12px; font-size:15px; }
        .ws-modal-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
    </style>
    <div class="ws-app">
        <div class="ws-header" id="ws-header"></div>
        <div class="ws-body" id="ws-body"></div>
    </div>`;

    const QS = sel => body.querySelector(sel);
    const headerEl = QS('#ws-header');
    const bodyEl = QS('#ws-body');

    /* ═══ Render: Site list ═══ */
    async function loadSites() {
        try {
            const r = await api('/websites/');
            sites = r.sites || [];
        } catch(e) { sites = []; }
        renderList();
    }

    function renderList() {
        view = 'list';
        headerEl.innerHTML = `
            <h2><i class="fas fa-globe" style="color:#14b8a6"></i> Kreator stron</h2>
            <button class="ws-btn ws-btn-primary" id="ws-new-btn"><i class="fas fa-plus"></i> Nowa strona</button>`;
        QS('#ws-new-btn').onclick = () => renderCreate();

        if (!sites.length) {
            bodyEl.innerHTML = `<div class="ws-empty">
                <i class="fas fa-globe"></i>
                <div style="font-size:16px;font-weight:600;margin-bottom:6px;">Brak stron</div>
                Kliknij „${t('Nowa strona')}" ${t('aby stworzyć swoją pierwszą witrynę.')}
            </div>`;
            return;
        }

        let html = '<div class="ws-site-grid">';
        sites.forEach(s => {
            const badge = s.published
                ? '<span class="ws-badge ws-badge-live">Opublikowana</span>'
                : '<span class="ws-badge ws-badge-draft">Szkic</span>';
            const pages = (s.pages||[]).length;
            const tpl = s.template || 'blank';
            const updatedAt = s.updated_at ? new Date(s.updated_at).toLocaleString(getLocale()) : '?';

            html += `<div class="ws-site-card" data-id="${esc(s.id)}">
                <h3><i class="fas fa-globe" style="color:#14b8a6;font-size:14px;"></i> ${esc(s.name)} ${badge}</h3>
                <div style="font-size:12px;color:var(--text-muted)">${esc(s.description || 'Brak opisu')}</div>
                <div class="ws-meta">
                    📄 ${pages} ${pages === 1 ? 'strona' : 'stron'} · 🎨 ${esc(s.theme || 'light')} · 📅 ${updatedAt}
                </div>
                <div class="ws-site-actions">
                    <button class="ws-btn ws-btn-primary ws-btn-sm ws-edit-site" data-id="${esc(s.id)}"><i class="fas fa-edit"></i> Edytuj</button>
                    <button class="ws-btn ws-btn-secondary ws-btn-sm ws-preview-site" data-id="${esc(s.id)}"><i class="fas fa-eye"></i> ${t('Podgląd')}</button>
                    <button class="ws-btn ws-btn-secondary ws-btn-sm ws-export-site" data-id="${esc(s.id)}"><i class="fas fa-download"></i></button>
                    <button class="ws-btn ws-btn-danger ws-btn-sm ws-delete-site" data-id="${esc(s.id)}"><i class="fas fa-trash"></i></button>
                </div>
            </div>`;
        });
        html += '</div>';
        bodyEl.innerHTML = html;

        /* Bind events */
        bodyEl.querySelectorAll('.ws-edit-site').forEach(btn => {
            btn.onclick = e => { e.stopPropagation(); openEditor(btn.dataset.id); };
        });
        bodyEl.querySelectorAll('.ws-preview-site').forEach(btn => {
            btn.onclick = e => { e.stopPropagation(); previewSite(btn.dataset.id); };
        });
        bodyEl.querySelectorAll('.ws-export-site').forEach(btn => {
            btn.onclick = e => { e.stopPropagation(); exportSite(btn.dataset.id); };
        });
        bodyEl.querySelectorAll('.ws-delete-site').forEach(btn => {
            btn.onclick = e => { e.stopPropagation(); deleteSite(btn.dataset.id); };
        });
        bodyEl.querySelectorAll('.ws-site-card').forEach(card => {
            card.onclick = () => openEditor(card.dataset.id);
        });
    }


    /* ═══ Render: Create site ═══ */
    async function renderCreate() {
        view = 'create';
        headerEl.innerHTML = `
            <h2><i class="fas fa-plus-circle" style="color:#14b8a6"></i> Nowa strona</h2>
            <button class="ws-btn ws-btn-secondary" id="ws-back-btn"><i class="fas fa-arrow-left"></i> ${t('Powrót')}</button>`;
        QS('#ws-back-btn').onclick = () => loadSites();

        // Load templates + themes in parallel
        if (!templates.length) {
            try {
                const [tr, thr] = await Promise.all([api('/websites/templates'), api('/websites/themes')]);
                templates = tr.templates || [];
                themes = thr.themes || [];
            } catch(e) { toast(t('Błąd ładowania szablonów: ') + e.message, 'error'); return; }
        }

        let selectedTemplate = 'blank';
        let selectedTheme = 'light';

        let tplHtml = '';
        templates.forEach(t => {
            tplHtml += `<div class="ws-tpl-card${t.id === selectedTemplate ? ' selected' : ''}" data-tpl="${esc(t.id)}">
                <i class="fas ${esc(t.icon)}" style="color:${esc(t.color)}"></i>
                <div class="tpl-name">${esc(t.name)}</div>
                <div class="tpl-desc">${esc(t.description)}</div>
            </div>`;
        });

        let themeOpts = themes.map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');

        bodyEl.innerHTML = `<div class="ws-create">
            <div class="ws-form-row">
                <label>Nazwa strony *</label>
                <input type="text" id="ws-c-name" placeholder="np. Moje portfolio" maxlength="100" autofocus>
            </div>
            <div class="ws-form-row">
                <label>Opis (opcjonalnie)</label>
                <input type="text" id="ws-c-desc" placeholder="${t('Krótki opis strony')}" maxlength="200">
            </div>
            <div class="ws-form-row">
                <label>Motyw</label>
                <select id="ws-c-theme">${themeOpts}</select>
            </div>
            <div class="ws-form-row">
                <label>Szablon</label>
                <div class="ws-tpl-grid" id="ws-c-tpl-grid">${tplHtml}</div>
            </div>
            <div style="margin-top:20px;">
                <button class="ws-btn ws-btn-primary" id="ws-c-submit" style="padding:10px 28px;font-size:14px;">
                    <i class="fas fa-plus-circle"></i> ${t('Stwórz stronę')}
                </button>
            </div>
        </div>`;

        /* Template selection */
        bodyEl.querySelectorAll('.ws-tpl-card').forEach(card => {
            card.onclick = () => {
                bodyEl.querySelectorAll('.ws-tpl-card').forEach(c => c.classList.remove('selected'));
                card.classList.add('selected');
                selectedTemplate = card.dataset.tpl;
            };
        });

        /* Theme selection */
        QS('#ws-c-theme').onchange = function() { selectedTheme = this.value; };

        /* Submit */
        QS('#ws-c-submit').onclick = async () => {
            const name = QS('#ws-c-name').value.trim();
            if (!name) { toast(t('Podaj nazwę strony'), 'warning'); return; }

            const btn = QS('#ws-c-submit');
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Tworzę…')}`;

            try {
                const r = await api('/websites/', {
                    method: 'POST',
                    body: {
                        name,
                        description: QS('#ws-c-desc').value.trim(),
                        template: selectedTemplate,
                        theme: selectedTheme
                    }
                });
                if (r.error) { toast(r.error, 'error'); btn.disabled = false; btn.innerHTML = `<i class="fas fa-plus-circle"></i> ${t('Stwórz stronę')}`; return; }
                toast(`Strona „${name}" utworzona!`, 'success');
                await loadSites();
                openEditor(r.site.id);
            } catch(e) {
                toast(t('Błąd: ') + e.message, 'error');
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-plus-circle"></i> ${t('Stwórz stronę')}`;
            }
        };
    }


    /* ═══ Editor ═══ */
    let editorTab = 'visual';  // 'visual' | 'code' | 'settings'

    async function openEditor(siteId) {
        try {
            const r = await api('/websites/' + siteId);
            currentSite = r.site;
        } catch(e) { toast(t('Błąd: ') + e.message, 'error'); return; }

        currentPage = (currentSite.pages && currentSite.pages.length) ? currentSite.pages[0].slug : null;
        editorTab = 'visual';
        renderEditor();
    }

    function renderEditor() {
        view = 'edit';
        const s = currentSite;

        headerEl.innerHTML = `
            <button class="ws-btn ws-btn-secondary ws-btn-sm" id="ws-ed-back"><i class="fas fa-arrow-left"></i></button>
            <h2 style="font-size:14px;"><i class="fas fa-globe" style="color:#14b8a6"></i> ${esc(s.name)}</h2>
            <div style="flex:1;"></div>
            <button class="ws-btn ws-btn-secondary ws-btn-sm" id="ws-ed-preview"><i class="fas fa-eye"></i> ${t('Podgląd')}</button>
            <button class="ws-btn ws-btn-primary ws-btn-sm" id="ws-ed-publish"><i class="fas fa-cloud-upload-alt"></i> Publikuj</button>`;

        QS('#ws-ed-back').onclick = () => loadSites();
        QS('#ws-ed-preview').onclick = () => previewSite(s.id);
        QS('#ws-ed-publish').onclick = async () => {
            try {
                await api('/websites/' + s.id + '/publish', { method: 'POST' });
                toast(t('Strona opublikowana!'), 'success');
            } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
        };

        bodyEl.style.padding = '0';
        bodyEl.style.overflow = 'hidden';

        bodyEl.innerHTML = `<div class="ws-editor">
            <div class="ws-sidebar">
                <div class="ws-sidebar-header">
                    <span>Podstrony</span>
                    <button class="ws-btn ws-btn-primary ws-btn-sm" id="ws-add-page" title="${t('Dodaj podstronę')}"><i class="fas fa-plus"></i></button>
                </div>
                <div class="ws-page-list" id="ws-page-list"></div>
                <div class="ws-sidebar-section">
                    <h4>Motyw</h4>
                    <select id="ws-theme-select" class="fm-input" style="width:100%">
                        ${(themes.length ? themes : [{id:'light',name:'Jasny'},{id:'dark',name:'Ciemny'},{id:'ocean',name:'Ocean'},{id:'forest',name:'Las'}])
                          .map(t => `<option value="${esc(t.id)}"${t.id === s.theme ? ' selected' : ''}>${esc(t.name)}</option>`).join('')}
                    </select>
                </div>
            </div>
            <div class="ws-main">
                <div class="ws-edit-tabs">
                    <button class="ws-edit-tab${editorTab==='visual'?' active':''}" data-tab="visual"><i class="fas fa-eye"></i> ${t('Podgląd')}</button>
                    <button class="ws-edit-tab${editorTab==='code'?' active':''}" data-tab="code"><i class="fas fa-code"></i> HTML</button>
                    <button class="ws-edit-tab${editorTab==='settings'?' active':''}" data-tab="settings"><i class="fas fa-cog"></i> Ustawienia</button>
                </div>
                <div class="ws-edit-area" id="ws-edit-area"></div>
            </div>
        </div>`;

        renderPageList();
        renderEditorContent();

        /* Bind tab switching */
        bodyEl.querySelectorAll('.ws-edit-tab').forEach(tab => {
            tab.onclick = () => {
                editorTab = tab.dataset.tab;
                bodyEl.querySelectorAll('.ws-edit-tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                renderEditorContent();
            };
        });

        /* Theme change */
        bodyEl.querySelector('#ws-theme-select').onchange = async function() {
            try {
                const r = await api('/websites/' + currentSite.id, {
                    method: 'PUT', body: { theme: this.value }
                });
                currentSite = r.site;
                toast('Motyw zmieniony', 'success');
                renderEditorContent();
            } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
        };

        /* Add page */
        bodyEl.querySelector('#ws-add-page').onclick = () => {
            const title = prompt(t('Tytuł nowej podstrony:'));
            if (!title || !title.trim()) return;
            addPage(title.trim());
        };
    }

    function renderPageList() {
        const list = bodyEl.querySelector('#ws-page-list');
        if (!list) return;
        const pages = currentSite.pages || [];

        let html = '';
        pages.forEach(p => {
            const isActive = p.slug === currentPage;
            const icon = p.slug === 'index' ? 'fa-home' : 'fa-file-alt';
            html += `<div class="ws-page-item${isActive ? ' active' : ''}" data-slug="${esc(p.slug)}">
                <i class="fas ${icon}"></i>
                <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(p.title)}</span>
                ${p.slug !== 'index' ? `<button class="ws-btn ws-btn-sm" style="padding:2px 5px;background:transparent;color:var(--text-muted);border:none;" data-del="${esc(p.slug)}" title="${t('Usuń')}"><i class="fas fa-times" style="font-size:10px;"></i></button>` : ''}
            </div>`;
        });
        list.innerHTML = html;

        /* Bind click */
        list.querySelectorAll('.ws-page-item').forEach(item => {
            item.onclick = (e) => {
                if (e.target.closest('[data-del]')) return;
                currentPage = item.dataset.slug;
                renderPageList();
                renderEditorContent();
            };
        });

        /* Bind delete */
        list.querySelectorAll('[data-del]').forEach(btn => {
            btn.onclick = async (e) => {
                e.stopPropagation();
                const slug = btn.dataset.del;
                const page = currentSite.pages.find(p => p.slug === slug);
                if (!confirm(t('Usunąć podstronę') + ` „${page ? page.title : slug}"?`)) return;
                try {
                    const r = await api('/websites/' + currentSite.id + '/pages/' + slug, { method: 'DELETE' });
                    currentSite = r.site;
                    if (currentPage === slug) currentPage = 'index';
                    renderPageList();
                    renderEditorContent();
                    toast(t('Podstrona usunięta'), 'success');
                } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
            };
        });
    }

    function renderEditorContent() {
        const area = bodyEl.querySelector('#ws-edit-area');
        if (!area) return;

        const page = (currentSite.pages || []).find(p => p.slug === currentPage);
        if (!page && editorTab !== 'settings') {
            area.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-muted)">${t('Wybierz podstronę z listy')}</div>`;
            return;
        }

        if (editorTab === 'visual') {
            area.innerHTML = `<iframe class="ws-preview-frame" id="ws-preview-iframe"></iframe>`;
            const iframe = area.querySelector('#ws-preview-iframe');
            iframe.onload = () => {}; // noop
            iframe.src = `/api/websites/${currentSite.id}/preview/${page.slug === 'index' ? 'index.html' : page.slug + '.html'}`;
        }
        else if (editorTab === 'code') {
            area.innerHTML = `
                <div style="display:flex;flex-direction:column;flex:1;overflow:hidden;">
                    <div style="padding:8px 14px;background:var(--bg-card,#1e293b);border-bottom:1px solid var(--border,#334155);display:flex;align-items:center;gap:8px;flex-shrink:0;">
                        <span style="font-size:12px;color:var(--text-muted);">Edytujesz: <b>${esc(page.title)}</b></span>
                        <div style="flex:1"></div>
                        <button class="ws-btn ws-btn-primary ws-btn-sm" id="ws-save-code"><i class="fas fa-save"></i> Zapisz</button>
                    </div>
                    <textarea class="ws-code-editor" id="ws-code-ta" spellcheck="false">${esc(page.content)}</textarea>
                </div>`;

            area.querySelector('#ws-save-code').onclick = () => savePage(page.slug);
        }
        else if (editorTab === 'settings') {
            area.innerHTML = `<div class="ws-settings-panel">
                <h3 style="margin:0 0 16px;font-size:15px;">Ustawienia strony</h3>
                <div class="ws-form-row">
                    <label>Nazwa strony</label>
                    <input type="text" id="ws-s-name" value="${esc(currentSite.name)}" maxlength="100">
                </div>
                <div class="ws-form-row">
                    <label>Opis</label>
                    <input type="text" id="ws-s-desc" value="${esc(currentSite.description || '')}" maxlength="200">
                </div>
                <div class="ws-form-row">
                    <label>Stopka</label>
                    <input type="text" id="ws-s-footer" value="${esc(currentSite.footer || '')}" maxlength="200">
                </div>
                <div class="ws-form-row">
                    <label>${t('Własny CSS')}</label>
                    <textarea id="ws-s-css" style="font-family:monospace;min-height:120px;">${esc(currentSite.custom_css || '')}</textarea>
                </div>
                <button class="ws-btn ws-btn-primary" id="ws-s-save"><i class="fas fa-save"></i> Zapisz ustawienia</button>
            </div>`;

            area.querySelector('#ws-s-save').onclick = async () => {
                const btn = area.querySelector('#ws-s-save');
                btn.disabled = true;
                try {
                    const r = await api('/websites/' + currentSite.id, {
                        method: 'PUT',
                        body: {
                            name: area.querySelector('#ws-s-name').value.trim(),
                            description: area.querySelector('#ws-s-desc').value.trim(),
                            footer: area.querySelector('#ws-s-footer').value.trim(),
                            custom_css: area.querySelector('#ws-s-css').value
                        }
                    });
                    currentSite = r.site;
                    toast(t('Ustawienia zapisane'), 'success');
                    // Update header
                    const h2 = headerEl.querySelector('h2');
                    if (h2) h2.innerHTML = `<i class="fas fa-globe" style="color:#14b8a6"></i> ${esc(currentSite.name)}`;
                } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
                btn.disabled = false;
            };
        }
    }


    /* ═══ Actions ═══ */
    async function savePage(slug) {
        const ta = bodyEl.querySelector('#ws-code-ta');
        if (!ta) return;

        const btn = bodyEl.querySelector('#ws-save-code');
        if (btn) { btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Zapisuję…')}`; }

        try {
            const r = await api('/websites/' + currentSite.id + '/pages/' + slug, {
                method: 'PUT',
                body: { content: ta.value }
            });
            currentSite = r.site;
            toast(t('Zapisano!'), 'success');
        } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }

        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-save"></i> Zapisz'; }
    }

    async function addPage(title) {
        try {
            const r = await api('/websites/' + currentSite.id + '/pages', {
                method: 'POST',
                body: { title }
            });
            currentSite = r.site;
            currentPage = r.page.slug;
            renderPageList();
            renderEditorContent();
            toast(t('Podstrona') + ` „${title}" ` + t('dodana'), 'success');
        } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
    }

    async function deleteSite(siteId) {
        const site = sites.find(s => s.id === siteId);
        if (!confirm(t('Usunąć stronę') + ` „${site ? site.name : siteId}" ` + t('i wszystkie jej pliki?'))) return;
        try {
            await api('/websites/' + siteId, { method: 'DELETE' });
            toast(t('Strona usunięta'), 'success');
            loadSites();
        } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
    }

    function previewSite(siteId) {
        window.open(`/api/websites/${siteId}/preview`, '_blank');
    }

    function exportSite(siteId) {
        const a = document.createElement('a');
        a.href = `/api/websites/${siteId}/export`;
        a.download = '';
        a.click();
    }


    /* ═══ Init ═══ */
    loadSites();

    /* Cleanup on window close */
    return () => {
        bodyEl.style.padding = '';
        bodyEl.style.overflow = '';
    };
}
