/**
 * EthOS — Menedżer Domen / SSL / DDNS
 * Unified app with left sidebar navigation.
 */
AppRegistry['domains-manager'] = function (appDef, launchOpts) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('domains-manager', level, msg, details) : console.log('[domains-manager]', msg, details || '');

    const winId = 'domains-manager';
    if (WM.windows.has(winId)) return;

    createWindow(winId, {
        title: t('Domeny i SSL'),
        icon: appDef?.icon || 'fa-globe',
        iconColor: appDef?.color || '#059669',
        width: 900, height: 680,
        minWidth: 640, minHeight: 440,
        onRender: (body) => _domInit(body),
    });
};

function _domInit(body) {
    const API_D = '/domains-mgr';
    const API_DDNS = '/ddns';
    let tab = 'domains';

    /* ── CSS ── */
    const CSS = `<style>
    .dm-wrap { display:flex; height:100%; font-family:var(--font-family,Inter,system-ui,sans-serif); color:var(--text,#e2e8f0); }
    .dm-sidebar { width:190px; min-width:190px; background:var(--bg-sidebar,#0f172a); border-right:1px solid var(--border,#1e293b); display:flex; flex-direction:column; padding:8px 0; }
    .dm-nav { padding:10px 18px; cursor:pointer; display:flex; align-items:center; gap:10px; font-size:13px; color:var(--text-muted,#94a3b8); transition:.15s; border-left:3px solid transparent; }
    .dm-nav:hover { background:var(--bg-hover,rgba(255,255,255,.04)); color:var(--text,#e2e8f0); }
    .dm-nav.active { background:var(--bg-hover,rgba(255,255,255,.06)); color:#059669; border-left-color:#059669; font-weight:600; }
    .dm-nav i { width:16px; text-align:center; }
    .dm-content { flex:1; overflow-y:auto; padding:24px; }

    .dm-toolbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:18px; flex-wrap:wrap; gap:8px; }
    .dm-toolbar h2 { margin:0; font-size:17px; font-weight:700; display:flex; align-items:center; gap:10px; }

    .dm-group { background:var(--bg-card,#1e293b); border:1px solid var(--border,#334155); border-radius:12px; padding:18px; margin-bottom:16px; }
    .dm-group-title { font-size:13px; font-weight:700; margin:0 0 12px; display:flex; align-items:center; gap:8px; }

    .dm-row { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
    .dm-row label { width:140px; font-size:12px; font-weight:600; color:var(--text-muted,#94a3b8); flex-shrink:0; }
    .dm-row input[type="text"], .dm-row input[type="email"], .dm-row input[type="number"], .dm-row textarea, .dm-row select {
        flex:1; padding:8px 12px; background:var(--bg-input,#0f172a); border:1px solid var(--border,#334155);
        border-radius:8px; color:var(--text,#e2e8f0); font-size:13px; outline:none; }
    .dm-row input:focus, .dm-row select:focus, .dm-row textarea:focus { border-color:#059669; }
    .dm-hint { font-size:11px; color:var(--text-muted,#94a3b8); margin:-4px 0 10px 150px; }

    .dm-btn { padding:8px 16px; border:none; border-radius:8px; cursor:pointer; font-size:12px; font-weight:600;
              display:inline-flex; align-items:center; gap:6px; background:var(--bg-hover,#334155); color:var(--text,#e2e8f0); transition:.15s; }
    .dm-btn:hover { filter:brightness(1.15); }
    .dm-btn.primary { background:#059669; color:#fff; }
    .dm-btn.warn { background:#d97706; color:#fff; }
    .dm-btn.danger { background:#ef4444; color:#fff; }
    .dm-btn.info { background:#3b82f6; color:#fff; }
    .dm-btn.purple { background:#6366f1; color:#fff; }
    .dm-btn.sm { padding:5px 10px; font-size:11px; }
    .dm-btn:disabled { opacity:.5; cursor:not-allowed; }

    .dm-actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }

    .dm-msg { padding:10px 14px; border-radius:8px; font-size:12px; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
    .dm-msg-ok { background:rgba(34,197,94,.12); color:#22c55e; }
    .dm-msg-err { background:rgba(239,68,68,.12); color:#ef4444; }
    .dm-msg-warn { background:rgba(234,179,8,.1); color:#eab308; }

    .dm-info-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:10px; margin-bottom:12px; }
    .dm-info-card { background:var(--bg-card,#1e293b); border:1px solid var(--border,#334155); border-radius:10px; padding:12px 16px; }
    .dm-info-label { font-size:11px; color:var(--text-muted); text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }
    .dm-info-value { font-size:16px; font-weight:700; }

    .dm-domain-card { display:flex; align-items:center; gap:12px; padding:12px 16px;
        background:var(--bg-card,#1e293b); border:1px solid var(--border,#334155); border-radius:10px; margin-bottom:8px; transition:.15s; }
    .dm-domain-card:hover { border-color:#059669; }

    /* DDNS styles */
    .dm-switch { position:relative; width:42px; height:24px; cursor:pointer; display:inline-block; }
    .dm-switch input { display:none; }
    .dm-switch .slider { position:absolute; inset:0; background:#475569; border-radius:24px; transition:.2s; }
    .dm-switch .slider::before { content:''; position:absolute; left:3px; top:3px; width:18px; height:18px; border-radius:50%; background:#fff; transition:.2s; }
    .dm-switch input:checked + .slider { background:#059669; }
    .dm-switch input:checked + .slider::before { transform:translateX(18px); }

    .dm-prov-card { background:var(--bg-card,#1e293b); border:1px solid var(--border,#334155); border-radius:10px; padding:14px 18px; margin-bottom:10px; }
    .dm-prov-header { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
    .dm-prov-name { font-weight:600; font-size:14px; flex:1; }
    .dm-prov-badge { font-size:10px; padding:2px 8px; border-radius:10px; background:#059669; color:#fff; }
    .dm-prov-badge.inactive { background:#64748b; }
    .dm-prov-fields { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .dm-prov-field { display:flex; flex-direction:column; gap:3px; }
    .dm-prov-field.full { grid-column:1/-1; }
    .dm-prov-field label { font-size:11px; color:var(--text-muted); }
    .dm-prov-field input, .dm-prov-field select { padding:6px 10px; border:1px solid var(--border,#334155); border-radius:6px; background:var(--bg-input,#0f172a); color:var(--text); font-size:13px; }
    .dm-prov-actions { display:flex; gap:8px; margin-top:10px; justify-content:flex-end; }

    .dm-add-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; margin-top:10px; }
    .dm-add-card { background:var(--bg-card,#1e293b); border:1px dashed var(--border,#334155); border-radius:10px; padding:14px; cursor:pointer; text-align:center; transition:.15s; }
    .dm-add-card:hover { border-color:#059669; background:rgba(5,150,105,.08); }

    .dm-history table { width:100%; border-collapse:collapse; font-size:12px; }
    .dm-history th { text-align:left; padding:6px 10px; border-bottom:1px solid var(--border,#334155); color:var(--text-muted); font-size:11px; text-transform:uppercase; }
    .dm-history td { padding:6px 10px; border-bottom:1px solid var(--border,#1e293b); }

    .dm-empty { text-align:center; padding:48px 20px; color:var(--text-muted,#94a3b8); }
    .dm-empty i { font-size:42px; opacity:.3; display:block; margin-bottom:14px; }
    </style>`;

    body.innerHTML = CSS + `
    <div class="dm-wrap">
        <div class="dm-sidebar">
            <div class="dm-nav active" data-tab="domains"><i class="fas fa-globe"></i> Domeny</div>
            <div class="dm-nav" data-tab="ssl"><i class="fas fa-lock"></i> SSL / HTTPS</div>
            <div class="dm-nav" data-tab="ddns"><i class="fas fa-sync-alt"></i> Dynamic DNS</div>
            <div class="dm-nav" data-tab="nginx"><i class="fas fa-server"></i> Nginx</div>
        </div>
        <div class="dm-content" id="dm-content"></div>
    </div>`;

    const sidebar = body.querySelector('.dm-sidebar');
    const content = body.querySelector('#dm-content');

    sidebar.addEventListener('click', e => {
        const n = e.target.closest('.dm-nav');
        if (!n) return;
        tab = n.dataset.tab;
        sidebar.querySelectorAll('.dm-nav').forEach(x => x.classList.toggle('active', x.dataset.tab === tab));
        renderTab();
    });

    function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

    async function renderTab() {
        if (tab === 'domains') await renderDomains();
        else if (tab === 'ssl') await renderSsl();
        else if (tab === 'ddns') await renderDdns();
        else if (tab === 'nginx') await renderNginx();
    }

    // ═══════════════════════════════════════════════════════════
    //  DOMAINS TAB
    // ═══════════════════════════════════════════════════════════

    async function renderDomains() {
        content.innerHTML = `<div class="dm-msg dm-msg-warn"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
        let data;
        try {
            data = await api(API_D + '/domains');
        } catch (e) {
            content.innerHTML = `<div class="dm-msg dm-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(e.message)}</div>`;
            return;
        }
        _renderDomainsData(data);
    }

    function _renderDomainsData(data) {
        const installed = data.nginx_installed;
        const domains = data.domains || [];
        let html = '';

        html += `<div class="dm-toolbar"><h2><i class="fas fa-globe dom-icon-primary"></i> ${t('Zarządzanie domenami')}</h2></div>`;

        // Nginx status
        html += '<div class="dm-group">';
        if (!installed) {
            html += `<div class="dm-msg dm-msg-err"><i class="fas fa-times-circle"></i> Nginx nie jest zainstalowany. Reverse proxy wymaga Nginx.</div>
                <div class="dm-actions dom-mt-sm">
                    <button class="dm-btn primary" id="dm-install-nginx"><i class="fas fa-download"></i> Zainstaluj Nginx</button>
                </div>`;
        } else {
            html += `<div class="dm-msg dm-msg-ok"><i class="fas fa-check-circle"></i> Nginx zainstalowany</div>`;
        }
        html += '</div>';

        if (installed) {
            // Domain list
            html += '<div class="dm-group"><div class="dm-group-title"><i class="fas fa-list"></i> Skonfigurowane domeny</div>';
            if (domains.length === 0) {
                html += `<div class="dm-msg dm-msg-warn"><i class="fas fa-info-circle"></i> ${t('Brak skonfigurowanych domen. Dodaj pierwszą poniżej.')}</div>`;
            } else {
                for (const d of domains) {
                    const statusColor = d.enabled ? '#22c55e' : '#6b7280';
                    const statusIcon = d.enabled ? 'fa-check-circle' : 'fa-pause-circle';
                    const sslBadge = d.ssl ? '<span class="dom-badge dom-badge-ssl">SSL</span>' : '';
                    const wsBadge = d.websocket ? '<span class="dom-badge dom-badge-ws">WS</span>' : '';

                    html += `<div class="dm-domain-card">
                        <div class="dom-card-body">
                            <div class="dom-card-title">
                                <i class="fas ${statusIcon} dom-status-icon" style="color:${statusColor}"></i>
                                ${esc(d.domain)}${sslBadge}${wsBadge}
                            </div>
                            <div class="dom-card-sub">
                                → ${esc(d.target)}${d.description ? ' — ' + esc(d.description) : ''}
                            </div>
                        </div>
                        <div class="dom-card-actions">
                            <button class="dm-btn info sm dm-ssl-btn" data-id="${d.id}" title="Certyfikat SSL"${d.ssl ? ' disabled' : ''}><i class="fas fa-lock"></i></button>
                            <button class="dm-btn sm dm-toggle-btn" data-id="${d.id}" style="background:${d.enabled ? '#d97706' : '#22c55e'};color:#fff;" title="${d.enabled ? t('Wyłącz') : t('Włącz')}"><i class="fas ${d.enabled ? 'fa-pause' : 'fa-play'}"></i></button>
                            <button class="dm-btn info sm dm-edit-btn" data-id="${d.id}" title="Edytuj"><i class="fas fa-pen"></i></button>
                            <button class="dm-btn danger sm dm-del-btn" data-id="${d.id}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                        </div>
                    </div>`;
                }
            }
            html += '</div>';

            // Add domain form
            html += `<div class="dm-group">
                <div class="dm-group-title"><i class="fas fa-plus-circle"></i> ${t('Dodaj domenę / subdomenę')}</div>
                <div class="dm-row"><label>Domena</label><input type="text" id="dm-add-domain" placeholder="np. app.mojadomena.pl"></div>
                <div class="dm-hint">${t('Pełna domena lub subdomena (np. nas.example.com)')}</div>
                <div class="dm-row">
                    <label>Cel (target)</label>
                    <div class="dom-target-wrap">
                        <input type="text" id="dm-add-target" placeholder="127.0.0.1:8080" style="flex:1;">
                        <button class="dm-btn sm dom-nowrap" id="dm-scan-svc"><i class="fas fa-search"></i> Skanuj</button>
                    </div>
                </div>
                <div class="dm-hint">${t('Adres wewnętrzny usługi — np. 127.0.0.1:8080')}</div>
                <div id="dm-services-list" class="dom-mb-md" style="display:none"></div>
                <div class="dm-row"><label>Opis</label><input type="text" id="dm-add-desc" placeholder="np. Jellyfin, Grafana…" maxlength="100"></div>
                <div class="dm-row dom-cb-row">
                    <label class="dom-cb-label"><input type="checkbox" id="dm-add-ws" class="dom-cb-gap"> WebSocket</label>
                    <label class="dom-cb-label"><input type="checkbox" id="dm-add-ssl" class="dom-cb-gap"> SSL (HTTPS)</label>
                    <label class="dom-cb-label"><input type="checkbox" id="dm-add-redirect" class="dom-cb-gap" checked> Redirect HTTP→HTTPS</label>
                </div>
                <div id="dm-add-ssl-email-row" style="display:none;">
                    <div class="dm-row"><label>Email (SSL)</label><input type="email" id="dm-add-email" placeholder="admin@mojadomena.pl"></div>
                    <div class="dm-hint">Let's Encrypt wymaga emaila. Certyfikat zostanie uzyskany automatycznie.</div>
                </div>
                <div class="dm-hint dom-hint-flush">${t('WebSocket — dla usług real-time. SSL — certyfikat zostanie uzyskany automatycznie.')}</div>
                <div class="dm-actions">
                    <button class="dm-btn primary" id="dm-add-btn"><i class="fas fa-plus"></i> ${t('Dodaj domenę')}</button>
                </div>
            </div>`;
        }

        content.innerHTML = html;

        // ── Events ──
        content.querySelector('#dm-install-nginx')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget; btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Instalowanie…';
            try { const r = await api(API_D + '/domains/install-nginx', { method: 'POST' }); if (r.error) throw new Error(r.error); toast(r.message || 'Nginx zainstalowany', 'success'); }
            catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            renderDomains();
        });

        content.querySelector('#dm-scan-svc')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            const svcDiv = content.querySelector('#dm-services-list');
            try {
                const r = await api(API_D + '/domains/services');
                const svcs = r.services || [];
                if (!svcs.length) { svcDiv.innerHTML = `<div class="dm-msg dm-msg-warn"><i class="fas fa-info-circle"></i> ${t('Nie znaleziono usług')}</div>`; }
                else {
                    svcDiv.innerHTML = '<div class="dom-svc-wrap">' +
                        svcs.map(s => `<button class="dm-btn sm" data-target="${esc(s.target)}" title="Ustaw jako cel">${esc(s.name ? s.name + ' (:' + s.port + ')' : ':' + s.port)}</button>`).join('') +
                        '</div>';
                    svcDiv.querySelectorAll('button[data-target]').forEach(b => {
                        b.addEventListener('click', () => { content.querySelector('#dm-add-target').value = b.dataset.target; svcDiv.style.display = 'none'; });
                    });
                }
                svcDiv.style.display = 'block';
            } catch (err) { svcDiv.innerHTML = `<div class="dm-msg dm-msg-err">${esc(err.message)}</div>`; svcDiv.style.display = 'block'; }
            btn.disabled = false; btn.innerHTML = '<i class="fas fa-search"></i> Skanuj';
        });

        /* Show/hide email field when SSL checkbox toggles */
        content.querySelector('#dm-add-ssl')?.addEventListener('change', (e) => {
            const emailRow = content.querySelector('#dm-add-ssl-email-row');
            if (emailRow) emailRow.style.display = e.target.checked ? 'block' : 'none';
        });

        content.querySelector('#dm-add-btn')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const domain = content.querySelector('#dm-add-domain').value.trim();
            const target = content.querySelector('#dm-add-target').value.trim();
            const desc = content.querySelector('#dm-add-desc').value.trim();
            const websocket = content.querySelector('#dm-add-ws').checked;
            const ssl = content.querySelector('#dm-add-ssl').checked;
            const force_https = content.querySelector('#dm-add-redirect').checked;
            const email = content.querySelector('#dm-add-email')?.value.trim() || '';
            if (!domain) { toast(t('Podaj domenę'), 'error'); return; }
            if (!target) { toast('Podaj cel (target)', 'error'); return; }
            if (ssl && !email) { toast('Podaj email — wymagany do uzyskania certyfikatu SSL', 'error'); return; }
            btn.disabled = true;
            btn.innerHTML = ssl
                ? '<i class="fas fa-spinner fa-spin"></i> Uzyskiwanie certyfikatu i dodawanie…'
                : '<i class="fas fa-spinner fa-spin"></i> Dodawanie…';
            try {
                const r = await api(API_D + '/domains', { method: 'POST', body: { domain, target, description: desc, websocket, ssl, force_https, email } });
                if (r.error) throw new Error(r.error);
                toast(`Domena ${domain} dodana!` + (ssl ? ' Certyfikat SSL uzyskany!' : ''), 'success');
            } catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            renderDomains();
        });

        content.querySelectorAll('.dm-toggle-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                btn.disabled = true;
                try { const r = await api(`${API_D}/domains/${btn.dataset.id}/toggle`, { method: 'POST' }); if (r.error) throw new Error(r.error); toast(r.message, 'success'); }
                catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
                renderDomains();
            });
        });

        content.querySelectorAll('.dm-del-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                const d = domains.find(x => x.id === btn.dataset.id);
                if (!await confirmDialog(t('Usunąć konfigurację domeny') + ' ' + (d?.domain || btn.dataset.id) + '?')) return;
                btn.disabled = true;
                try { const r = await api(`${API_D}/domains/${btn.dataset.id}`, { method: 'DELETE' }); if (r.error) throw new Error(r.error); toast(r.message, 'success'); }
                catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
                renderDomains();
            });
        });

        content.querySelectorAll('.dm-ssl-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                const d = domains.find(x => x.id === btn.dataset.id);
                if (d?.ssl) return;
                /* Try to get a saved email first */
                let savedEmail = '';
                try { const st = await api(API_D + '/ssl/status'); savedEmail = st?.config?.email || ''; } catch {}
                let email = savedEmail;
                if (!email) {
                    email = prompt(t('Podaj email do certyfikatu SSL dla') + ' ' + d?.domain + ':\n' + t('(wymagany przez Let\'s Encrypt)'), '');
                    if (!email) return;
                }
                if (!await confirmDialog(t('Uzyskać certyfikat SSL dla') + ' ' + d?.domain + '?\nEmail: ' + email)) return;
                btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                try { const r = await api(`${API_D}/domains/${btn.dataset.id}/ssl`, { method: 'POST', body: { email } }); if (r.error) throw new Error(r.error); toast(r.message, 'success'); }
                catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
                renderDomains();
            });
        });

        content.querySelectorAll('.dm-edit-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const d = domains.find(x => x.id === btn.dataset.id);
                if (d) _showDomainEditor(d);
            });
        });
    }

    function _showDomainEditor(d) {
        content.innerHTML = `
            <div class="dm-toolbar"><h2><i class="fas fa-pen dom-icon-blue"></i> Edycja: ${esc(d.domain)}</h2></div>
            <div class="dm-group">
                <div class="dm-row"><label>Domena</label><input type="text" id="dme-domain" value="${esc(d.domain)}"></div>
                <div class="dm-row"><label>Cel (target)</label><input type="text" id="dme-target" value="${esc(d.target)}"></div>
                <div class="dm-row"><label>Opis</label><input type="text" id="dme-desc" value="${esc(d.description || '')}" maxlength="100"></div>
                <div class="dm-row dom-cb-row">
                    <label class="dom-cb-label"><input type="checkbox" id="dme-ws" ${d.websocket ? 'checked' : ''} class="dom-cb-gap"> WebSocket</label>
                    <label class="dom-cb-label"><input type="checkbox" id="dme-ssl" ${d.ssl ? 'checked' : ''} class="dom-cb-gap"> SSL</label>
                    <label class="dom-cb-label"><input type="checkbox" id="dme-redirect" ${d.force_https ? 'checked' : ''} class="dom-cb-gap"> Redirect HTTP→HTTPS</label>
                </div>
                <div class="dm-row"><label>Custom config</label>
                    <textarea id="dme-custom" rows="3" class="dom-textarea-code"
                        placeholder="Dodatkowe dyrektywy nginx (opcjonalne)">${esc(d.custom_config || '')}</textarea>
                </div>
                <div class="dm-actions">
                    <button class="dm-btn primary" id="dme-save"><i class="fas fa-save"></i> Zapisz</button>
                    <button class="dm-btn" id="dme-cancel"><i class="fas fa-times"></i> Anuluj</button>
                    <button class="dm-btn purple" id="dme-preview"><i class="fas fa-eye"></i> ${t('Podgląd nginx')}</button>
                </div>
                <pre id="dme-preview-out" class="dom-pre-output" style="display:none"></pre>
            </div>
        `;

        content.querySelector('#dme-save').addEventListener('click', async (e) => {
            const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Zapisywanie…';
            try {
                const r = await api(`${API_D}/domains/${d.id}`, { method: 'PUT', body: {
                    domain: content.querySelector('#dme-domain').value.trim(),
                    target: content.querySelector('#dme-target').value.trim(),
                    description: content.querySelector('#dme-desc').value.trim(),
                    websocket: content.querySelector('#dme-ws').checked,
                    ssl: content.querySelector('#dme-ssl').checked,
                    force_https: content.querySelector('#dme-redirect').checked,
                    custom_config: content.querySelector('#dme-custom').value,
                }});
                if (r.error) throw new Error(r.error);
                toast('Domena zaktualizowana', 'success');
            } catch (err) { toast(t('Błąd: ') + err.message, 'error'); btn.disabled = false; btn.innerHTML = '<i class="fas fa-save"></i> Zapisz'; return; }
            renderDomains();
        });

        content.querySelector('#dme-cancel').addEventListener('click', () => renderDomains());

        content.querySelector('#dme-preview').addEventListener('click', async () => {
            const pre = content.querySelector('#dme-preview-out');
            try { const r = await api(`${API_D}/domains/${d.id}/preview`); pre.textContent = r.config || '(pusty)'; pre.style.display = 'block'; }
            catch (err) { pre.textContent = t('Błąd: ') + err.message; pre.style.display = 'block'; }
        });
    }

    // ═══════════════════════════════════════════════════════════
    //  SSL TAB
    // ═══════════════════════════════════════════════════════════

    async function renderSsl() {
        content.innerHTML = `<div class="dm-msg dm-msg-warn"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie statusu SSL…')}</div>`;
        let status;
        try { status = await api(API_D + '/ssl/status'); }
        catch (e) { content.innerHTML = `<div class="dm-msg dm-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(e.message)}</div>`; return; }
        _renderSslData(status);
    }

    function _renderSslData(status) {
        const cfg = status.config || {};
        const allCerts = status.certs || [];
        const cert = status.cert;
        const installed = status.certbot_installed;

        let html = '<div class="dm-toolbar"><h2><i class="fas fa-lock dom-icon-primary"></i> Certyfikaty SSL / Let\'s Encrypt</h2></div>';

        // Step 1: Certbot
        html += `<div class="dm-group"><div class="dm-group-title"><i class="fas fa-box"></i> 1. Certbot</div>`;
        if (installed) {
            html += `<div class="dm-msg dm-msg-ok"><i class="fas fa-check-circle"></i> Certbot jest zainstalowany</div>`;
        } else {
            html += `<div class="dm-msg dm-msg-err"><i class="fas fa-times-circle"></i> Certbot nie jest zainstalowany</div>
                <div class="dm-actions"><button class="dm-btn primary" id="dm-ssl-install"><i class="fas fa-download"></i> Zainstaluj Certbot</button></div>`;
        }
        html += `</div>`;

        // Step 2: All certificates
        html += `<div class="dm-group"><div class="dm-group-title"><i class="fas fa-certificate"></i> 2. Certyfikaty</div>`;
        if (allCerts.length === 0) {
            html += `<div class="dm-msg dm-msg-warn"><i class="fas fa-info-circle"></i> ${t('Brak certyfikatów. Uzyskaj pierwszy certyfikat poniżej lub zaznacz SSL przy dodawaniu domeny.')}</div>`;
        } else {
            for (const c of allCerts) {
                const daysLeft = c.days_left !== undefined ? c.days_left : '?';
                const daysColor = daysLeft <= 7 ? '#ef4444' : daysLeft <= 30 ? '#eab308' : '#22c55e';
                const domBadge = c.has_domain
                    ? (c.domain_ssl_enabled
                        ? '<span class="dom-badge dom-badge-active">Aktywny w domenie</span>'
                        : '<span class="dom-badge dom-badge-warn">Domena bez SSL</span>')
                    : '<span class="dom-badge dom-badge-muted">Bez przypisanej domeny</span>';

                html += `<div class="dm-domain-card dom-cert-card">
                    <div class="dom-cert-header">
                        <div class="dom-cert-info">
                            <i class="fas fa-shield-alt dom-shield-icon" style="color:${daysColor}"></i>
                            <span class="dom-cert-domain">${esc(c.domain)}</span>
                            ${domBadge}
                        </div>
                        <div class="dom-btn-group">
                            <button class="dm-btn primary sm dm-renew-cert" data-domain="${esc(c.domain)}"><i class="fas fa-sync-alt"></i> ${t('Odnów')}</button>
                        </div>
                    </div>
                    <div class="dm-info-grid dom-m-0">
                        <div class="dm-info-card"><div class="dm-info-label">${t('Pozostało dni')}</div><div class="dm-info-value" style="color:${daysColor}">${daysLeft}</div></div>
                        <div class="dm-info-card"><div class="dm-info-label">${t('Ważny od')}</div><div class="dm-info-value dom-text-xs">${esc(c.not_before || '-')}</div></div>
                        <div class="dm-info-card"><div class="dm-info-label">${t('Ważny do')}</div><div class="dm-info-value dom-text-xs">${esc(c.not_after || '-')}</div></div>
                    </div>
                </div>`;
            }
        }
        // Obtain new cert form
        html += `
            <div class="dom-section-sep">
                <div class="dom-section-title"><i class="fas fa-plus-circle dom-icon-primary dom-mr-xs"></i>Uzyskaj nowy certyfikat</div>
                <div class="dm-row"><label>Domena</label><input type="text" id="dm-ssl-domain" value="${esc(cfg.domain)}" placeholder="np. nas.mojadomena.pl"></div>
                <div class="dm-hint">${t('Domena musi wskazywać (DNS A/AAAA) na publiczne IP tego serwera')}</div>
                <div class="dm-row"><label>Email</label><input type="email" id="dm-ssl-email" value="${esc(cfg.email)}" placeholder="admin@mojadomena.pl"></div>
                <div class="dm-hint">${t("Używany przez Let's Encrypt do powiadomień o wygasaniu")}</div>
            </div>
            <div class="dm-actions">
                <button class="dm-btn primary" id="dm-ssl-obtain" ${!installed?'disabled':''}><i class="fas fa-certificate"></i> Uzyskaj certyfikat</button>
                <button class="dm-btn warn" id="dm-ssl-test" ${!installed?'disabled':''}><i class="fas fa-vial"></i> ${t('Testuj połączenie')}</button>
            </div>
            <div id="dm-ssl-test-result" class="dom-mt-sm"></div>
        </div>`;

        // Step 3: Enable HTTPS for EthOS
        const anyCert = allCerts.length > 0;
        // Check if any domain with SSL is configured (nginx handles HTTPS)
        const domainsSslActive = allCerts.some(c => c.has_domain && c.domain_ssl_enabled);
        html += `<div class="dm-group"><div class="dm-group-title"><i class="fas fa-shield-alt"></i> 3. HTTPS dla EthOS</div>`;
        if (!anyCert) {
            html += `<div class="dm-msg dm-msg-warn"><i class="fas fa-info-circle"></i> Najpierw uzyskaj certyfikat</div>`;
        } else if (domainsSslActive) {
            html += `<div class="dm-msg dm-msg-ok dom-lh-relaxed">
                <i class="fas fa-check-circle"></i> <strong>HTTPS aktywny przez Nginx proxy</strong><br>
                <span class="dom-text-sm">${t('Domeny z SSL skonfigurowane w zakładce "Domeny" obsługują HTTPS automatycznie. Natywny HTTPS na serwerze EthOS nie jest potrzebny.')}</span>
            </div>`;
        } else {
            const active = status.ssl_active;
            const httpsPort = status.https_port || cfg.https_port || 443;
            html += `
                <div class="dm-hint dom-hint-flush-mb">${t('Włącz natywny HTTPS bezpośrednio na serwerze EthOS (gdy nie używasz nginx proxy)')}</div>
                <div class="dm-row"><label>Port HTTPS</label><input type="number" id="dm-ssl-port" value="${httpsPort}" min="1" max="65535"></div>
                <div class="dm-row dom-row-gap">
                    <label>Przekierowanie HTTP→HTTPS</label>
                    <input type="checkbox" id="dm-ssl-redirect" ${cfg.redirect_http !== false ? 'checked' : ''} style="flex:none;width:18px;height:18px;">
                </div>
                <div class="dm-actions">`;
            if (active) {
                html += `<div class="dm-msg dm-msg-ok dom-mr-md"><i class="fas fa-check-circle"></i> HTTPS aktywny</div>
                    <button class="dm-btn danger" id="dm-ssl-disable"><i class="fas fa-times"></i> ${t('Wyłącz HTTPS')}</button>`;
            } else {
                html += `<button class="dm-btn primary" id="dm-ssl-enable"><i class="fas fa-shield-alt"></i> ${t('Włącz HTTPS')}</button>`;
            }
            html += `</div>`;
        }
        html += `</div>`;

        // Step 4: Auto-renewal
        html += `<div class="dm-group"><div class="dm-group-title"><i class="fas fa-clock"></i> 4. Automatyczne odnawianie</div>`;
        if (anyCert) {
            const renewal = status.renewal;
            const autoRenew = cfg.auto_renew !== false;
            // Find nearest cert expiry
            let nearestExpiry = null;
            let nearestDomain = '';
            for (const c of allCerts) {
                if (c.days_left != null) {
                    if (nearestExpiry === null || c.days_left < nearestExpiry) {
                        nearestExpiry = c.days_left;
                        nearestDomain = c.domain;
                    }
                }
            }
            html += `
                <div class="dm-row dom-row-gap">
                    <label class="dm-switch"><input type="checkbox" id="dm-ssl-autorenew" ${autoRenew ? 'checked' : ''}><span class="slider"></span></label>
                    <span class="dom-label-text">${t('Automatyczne odnawianie certyfikatów')}</span>
                </div>
                <div class="dm-hint dom-mt-xs">${t('Certbot odnowi certyfikaty automatycznie (codziennie o 3:00) bez przerwy w działaniu Nginx.')}</div>`;
            if (renewal) {
                html += `<div class="dm-msg dm-msg-ok dom-mt-sm"><i class="fas fa-check-circle"></i> ${t('Mechanizm odnowień aktywny')} (${esc(renewal)})</div>`;
            } else if (autoRenew) {
                html += `<div class="dm-msg dm-msg-warn dom-mt-sm"><i class="fas fa-exclamation-triangle"></i> ${t('Timer nie wykryty — kliknij przełącznik aby go aktywować')}</div>`;
            }
            if (nearestExpiry !== null) {
                const color = nearestExpiry < 7 ? '#ef4444' : nearestExpiry < 30 ? '#d97706' : '#22c55e';
                html += `<div class="dom-expiry-box">
                    <i class="fas fa-calendar-alt dom-mr-xs" style="color:${color}"></i>
                    ${t('Najbliższe wygaśnięcie:')} <strong style="color:${color}">${nearestExpiry} ${t('dni')}</strong> (${esc(nearestDomain)})
                </div>`;
            }
        } else {
            html += `<div class="dm-msg dm-msg-warn"><i class="fas fa-info-circle"></i> Najpierw uzyskaj certyfikat</div>`;
        }
        html += `</div>`;

        content.innerHTML = html;

        // ── Events ──
        content.querySelector('#dm-ssl-install')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Instalowanie…';
            try { const r = await api(API_D + '/ssl/install-certbot', { method: 'POST' }); if (r.error) throw new Error(r.error); toast(r.message || 'Certbot zainstalowany', 'success'); }
            catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            renderSsl();
        });

        content.querySelector('#dm-ssl-test')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const domain = content.querySelector('#dm-ssl-domain').value.trim();
            btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Testuję…')}`;
            const rd = content.querySelector('#dm-ssl-test-result');
            try {
                const r = await api(API_D + '/ssl/test', { method: 'POST', body: { domain } });
                let msgs = [];
                if (r.port_80) msgs.push('<div class="dm-msg dm-msg-ok"><i class="fas fa-check-circle"></i> Port 80 OK' + (r.port_80_nginx ? ' (' + t('nginx obsługuje port 80') + ')' : '') + '</div>');
                else if (r.port_80_in_use) msgs.push(`<div class="dm-msg dm-msg-err"><i class="fas fa-times-circle"></i> ${t('Port 80 jest zajęty przez inną usługę')}</div>`);
                else msgs.push('<div class="dm-msg dm-msg-ok"><i class="fas fa-check-circle"></i> Port 80 wolny — OK</div>');
                if (domain) {
                    if (r.dns_ok) msgs.push(`<div class="dm-msg dm-msg-ok"><i class="fas fa-check-circle"></i> DNS OK: ${esc(domain)} → ${esc(r.dns_ip)}</div>`);
                    else {
                        const hint = r.dns_ip ? ` (DNS: ${esc(r.dns_ip)}, serwer: ${esc(r.server_ip)})` : '';
                        msgs.push(`<div class="dm-msg dm-msg-err"><i class="fas fa-times-circle"></i> DNS nie wskazuje na ten serwer${hint}</div>`);
                    }
                }
                rd.innerHTML = msgs.join('');
            } catch (err) { rd.innerHTML = `<div class="dm-msg dm-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(err.message)}</div>`; }
            btn.disabled = false; btn.innerHTML = `<i class="fas fa-vial"></i> ${t('Testuj połączenie')}`;
        });

        content.querySelector('#dm-ssl-obtain')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const domain = content.querySelector('#dm-ssl-domain').value.trim();
            const email = content.querySelector('#dm-ssl-email').value.trim();
            const httpsPort = parseInt(content.querySelector('#dm-ssl-port')?.value || '443');
            if (!domain) { toast(t('Podaj domenę'), 'error'); return; }
            if (!email) { toast(t('Podaj email'), 'error'); return; }
            if (!await confirmDialog(t('Certbot spróbuje uzyskać certyfikat dla:') + '\n\n' + domain + '\n\n' + t('Kontynuować?'))) return;
            btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Uzyskiwanie…')}`;
            try { const r = await api(API_D + '/ssl/obtain', { method: 'POST', body: { domain, email, https_port: httpsPort } }); if (r.error) throw new Error(r.error); toast(r.message || t('Certyfikat uzyskany!'), 'success'); }
            catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            renderSsl();
        });

        content.querySelectorAll('.dm-renew-cert').forEach(btn => {
            btn.addEventListener('click', async () => {
                const domain = btn.dataset.domain;
                if (!await confirmDialog(t('Odnowić certyfikat dla') + ' ' + domain + '?')) return;
                btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Odnawianie…')}`;
                try { const r = await api(API_D + '/ssl/renew', { method: 'POST', body: { domain } }); if (r.error) throw new Error(r.error); toast(r.message || 'Certyfikat odnowiony', 'success'); }
                catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
                renderSsl();
            });
        });

        content.querySelector('#dm-ssl-enable')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const httpsPort = parseInt(content.querySelector('#dm-ssl-port')?.value || '443');
            const redirect = content.querySelector('#dm-ssl-redirect')?.checked !== false;
            if (!await confirmDialog(t('Włączyć HTTPS na porcie') + ' ' + httpsPort + '?')) return;
            btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Włączanie…')}`;
            try {
                const r = await api(API_D + '/ssl/enable', { method: 'POST', body: { enabled: true, https_port: httpsPort, redirect_http: redirect } });
                if (r.error) throw new Error(r.error);
                toast(r.message || t('HTTPS włączony'), 'success');
                if (r.restart_needed && await confirmDialog(t('Restart wymagany. Zrestartować teraz?'))) {
                    await api('/settings/restart', { method: 'POST' });
                    setTimeout(() => { window.location.href = `https://${location.hostname}:${httpsPort}`; }, 10000);
                    return;
                }
            } catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            renderSsl();
        });

        content.querySelector('#dm-ssl-disable')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            if (!await confirmDialog(t('Wyłączyć HTTPS?'))) return;
            btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Wyłączanie…')}`;
            try {
                const r = await api(API_D + '/ssl/enable', { method: 'POST', body: { enabled: false } });
                if (r.error) throw new Error(r.error);
                toast(r.message || t('HTTPS wyłączony'), 'success');
                if (r.restart_needed && await confirmDialog(t('Restart wymagany. Zrestartować teraz?'))) {
                    await api('/settings/restart', { method: 'POST' });
                    setTimeout(() => { window.location.href = `http://${location.hostname}:9000`; }, 8000);
                    return;
                }
            } catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            renderSsl();
        });

        content.querySelector('#dm-ssl-autorenew')?.addEventListener('change', async (e) => {
            const enabled = e.target.checked;
            try { const r = await api(API_D + '/ssl/auto-renew', { method: 'POST', body: { enabled } }); if (r.error) throw new Error(r.error); toast(r.message || 'OK', 'success'); }
            catch (err) { toast(t('Błąd: ') + err.message, 'error'); e.target.checked = !enabled; }
        });
    }

    // ═══════════════════════════════════════════════════════════
    //  DDNS TAB
    // ═══════════════════════════════════════════════════════════

    let ddnsProviders = [];
    let ddnsConfig = {};
    let ddnsStatus = {};

    async function renderDdns() {
        content.innerHTML = `<div class="dm-msg dm-msg-warn"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
        try {
            [ddnsProviders, ddnsConfig, ddnsStatus] = await Promise.all([
                api(API_DDNS + '/providers'),
                api(API_DDNS + '/config'),
                api(API_DDNS + '/status'),
            ]);
        } catch (e) { content.innerHTML = `<div class="dm-msg dm-msg-err">${esc(e.message)}</div>`; return; }
        _renderDdnsData();
    }

    function _renderDdnsData() {
        const configProviders = ddnsConfig.providers || [];
        let html = '';

        html += '<div class="dm-toolbar"><h2><i class="fas fa-sync-alt dom-icon-primary"></i> Dynamic DNS</h2></div>';

        // Status cards
        html += `<div class="dm-info-grid">
            <div class="dm-info-card"><div class="dm-info-label">Publiczne IP</div><div class="dm-info-value">${ddnsStatus.current_ip || '—'}</div></div>
            <div class="dm-info-card"><div class="dm-info-label">Status</div><div class="dm-info-value" style="color:${!ddnsStatus.enabled ? 'var(--text-muted)' : ddnsStatus.last_status === 'ok' ? '#22c55e' : '#ef4444'}">
                ${!ddnsStatus.enabled ? `<i class="fas fa-pause-circle"></i> ${t('Wyłączony')}` : ddnsStatus.last_status === 'ok' ? '<i class="fas fa-check-circle"></i> OK' : ddnsStatus.last_status === 'error' ? `<i class="fas fa-exclamation-circle"></i> ${t('Błąd')}` : '<i class="fas fa-hourglass-half"></i> Oczekiwanie'}
            </div></div>
            <div class="dm-info-card"><div class="dm-info-label">Ostatnia aktualizacja</div><div class="dm-info-value dom-fz-13">${ddnsStatus.last_update ? new Date(ddnsStatus.last_update).toLocaleString('pl') : '—'}</div></div>
            <div class="dm-info-card"><div class="dm-info-label">Aktywne providery</div><div class="dm-info-value">${ddnsStatus.active_count || 0} / ${ddnsStatus.providers_count || 0}</div></div>
        </div>`;

        // Settings
        html += `<div class="dm-group">
            <div class="dm-group-title"><i class="fas fa-cog"></i> Ustawienia</div>
            <div class="dom-settings-row">
                <label class="dm-switch"><input type="checkbox" id="dd-enabled" ${ddnsConfig.enabled ? 'checked' : ''}><span class="slider"></span></label>
                <span class="dom-fz-13">Automatyczna aktualizacja DNS</span>
            </div>
            <div class="dom-settings-row-sm">
                <span class="dom-fz-13">Sprawdzaj IP co</span>
                <input type="number" id="dd-interval" min="1" max="1440" value="${ddnsConfig.interval_min || 5}" class="dom-input-compact" style="width:80px;">
                <span class="dom-fz-13">minut</span>
            </div>
            <div class="dm-actions dom-mt-0">
                <button class="dm-btn primary" id="dd-save"><i class="fas fa-save"></i> Zapisz</button>
                <button class="dm-btn info" id="dd-update-now"><i class="fas fa-sync-alt"></i> Aktualizuj teraz</button>
                <button class="dm-btn" id="dd-check-ip"><i class="fas fa-search"></i> ${t('Sprawdź IP')}</button>
                <button class="dm-btn" id="dd-history-btn"><i class="fas fa-history"></i> Historia</button>
            </div>
        </div>`;

        // Configured providers
        html += '<div class="dm-group"><div class="dm-group-title"><i class="fas fa-server"></i> Skonfigurowane providery</div>';
        if (!configProviders.length) {
            html += `<div class="dom-empty-text">${t('Brak skonfigurowanych providerów. Dodaj jednego poniżej.')}</div>`;
        } else {
            configProviders.forEach((p, idx) => {
                const tmpl = ddnsProviders.find(prov => prov.id === p.type) || {};
                const fields = tmpl.fields || [];
                html += `<div class="dm-prov-card" data-idx="${idx}">
                    <div class="dm-prov-header">
                        <span class="dm-prov-name">${esc(p.name || tmpl.name || p.type)}</span>
                        <span class="dm-prov-badge ${p.active === false ? 'inactive' : ''}">${p.active === false ? 'Nieaktywny' : 'Aktywny'}</span>
                    </div>
                    <div class="dm-prov-fields">
                        ${fields.map(f => `
                            <div class="dm-prov-field ${f.key === 'update_url' ? 'full' : ''}">
                                <label>${esc(f.label)}</label>
                                ${f.type === 'select' ?
                                    `<select data-key="${f.key}" data-idx="${idx}">${(f.options||[]).map(o => `<option ${p[f.key]===o?'selected':''}>${o}</option>`).join('')}</select>` :
                                    `<input type="${f.type||'text'}" data-key="${f.key}" data-idx="${idx}" value="${esc(p[f.key]||'')}" placeholder="${esc(f.placeholder||'')}">`
                                }
                            </div>
                        `).join('')}
                    </div>
                    <div class="dm-prov-actions">
                        <button class="dm-btn primary sm dd-save-prov" data-idx="${idx}"><i class="fas fa-save"></i> Zapisz</button>
                        <button class="dm-btn sm dd-toggle-active" data-idx="${idx}"><i class="fas ${p.active === false ? 'fa-play' : 'fa-pause'}"></i> ${p.active === false ? t('Włącz') : t('Wyłącz')}</button>
                        <button class="dm-btn danger sm dd-remove-prov" data-idx="${idx}"><i class="fas fa-trash"></i> ${t('Usuń')}</button>
                    </div>
                </div>`;
            });
        }
        html += '</div>';

        // Add provider grid
        html += '<div class="dm-group"><div class="dm-group-title"><i class="fas fa-plus-circle"></i> Dodaj provider</div><div class="dm-add-grid">';
        ddnsProviders.forEach(p => {
            html += `<div class="dm-add-card" data-type="${p.id}">
                <div class="dom-add-title">${esc(p.name)}</div>
                <div class="dom-add-desc">${esc(p.description)}</div>
                ${p.no_registration ? '<div class="dom-badge-free">Bez rejestracji</div>' : ''}
            </div>`;
        });
        html += '</div></div>';

        // History (hidden)
        html += '<div id="dd-history-area" style="display:none;" class="dm-group"><div class="dm-group-title"><i class="fas fa-history"></i> Historia aktualizacji</div><div class="dm-history" id="dd-history-list"></div></div>';

        content.innerHTML = html;

        // ── Events ──
        content.querySelector('#dd-save').addEventListener('click', () => _ddnsSaveConfig());

        content.querySelector('#dd-update-now').addEventListener('click', async (e) => {
            const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Aktualizowanie…';
            try {
                const r = await api(API_DDNS + '/update', { method: 'POST' });
                if (r.ok) toast('DNS zaktualizowany!', 'success'); else toast(t('Błąd: ') + (r.error || 'nieznany'), 'error');
                ddnsStatus = await api(API_DDNS + '/status');
                _renderDdnsData();
            } catch (e) { toast(t('Błąd: ') + e.message, 'error'); btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt"></i> Aktualizuj teraz'; }
        });

        content.querySelector('#dd-check-ip').addEventListener('click', async (e) => {
            const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            try {
                const r = await api(API_DDNS + '/check-ip');
                toast('Twoje IP: ' + (r.ip || 'nieznane'), r.ok ? 'info' : 'error');
                if (r.ip) ddnsStatus.current_ip = r.ip;
                _renderDdnsData();
            } catch (e) { toast(t('Błąd: ') + e.message, 'error'); }
            btn.disabled = false; btn.innerHTML = `<i class="fas fa-search"></i> ${t('Sprawdź IP')}`;
        });

        content.querySelector('#dd-history-btn').addEventListener('click', async () => {
            const area = content.querySelector('#dd-history-area');
            const list = content.querySelector('#dd-history-list');
            if (area.style.display === 'none') {
                area.style.display = '';
                list.innerHTML = '<div class="dom-loading"><i class="fas fa-spinner fa-spin"></i></div>';
                try {
                    const history = await api(API_DDNS + '/history');
                    if (!history.length) list.innerHTML = '<div class="dm-empty"><i class="fas fa-inbox"></i><p>Brak historii</p></div>';
                    else list.innerHTML = `<table><tr><th>${t('Data')}</th><th>Provider</th><th>IP</th><th>Status</th><th>${t('Wiadomość')}</th></tr>
                        ${history.slice(0,50).map(h => `<tr><td>${new Date(h.time).toLocaleString('pl')}</td><td>${esc(h.provider_name)}</td><td><code>${esc(h.ip)}</code></td>
                        <td style="color:${h.status==='ok'?'#22c55e':'#ef4444'}"><i class="fas ${h.status==='ok'?'fa-check':'fa-times'}"></i> ${h.status}</td>
                        <td class="dom-truncate" title="${esc(h.message)}">${esc(h.message)}</td></tr>`).join('')}</table>`;
                } catch (e) { list.innerHTML = `<div class="dm-msg dm-msg-err">${esc(e.message)}</div>`; }
            } else { area.style.display = 'none'; }
        });

        // Add provider
        content.querySelectorAll('.dm-add-card').forEach(card => {
            card.addEventListener('click', () => {
                const type = card.dataset.type;
                const tmpl = ddnsProviders.find(p => p.id === type);
                if (!tmpl) return;
                const newP = { id: Math.random().toString(36).slice(2,10), type, name: tmpl.name, active: true };
                tmpl.fields.forEach(f => { newP[f.key] = f.default || ''; });
                ddnsConfig.providers = ddnsConfig.providers || [];
                ddnsConfig.providers.push(newP);
                _renderDdnsData();
                toast(t('Dodano') + ' ' + tmpl.name + ' — ' + t('uzupełnij dane i zapisz'), 'info');
            });
        });

        // Save individual provider
        content.querySelectorAll('.dd-save-prov').forEach(btn => {
            btn.addEventListener('click', async () => {
                // Collect current field values from DOM before saving
                content.querySelectorAll(`.dm-prov-fields input[data-idx="${btn.dataset.idx}"], .dm-prov-fields select[data-idx="${btn.dataset.idx}"]`).forEach(el => {
                    const idx = parseInt(el.dataset.idx);
                    if (ddnsConfig.providers[idx]) ddnsConfig.providers[idx][el.dataset.key] = el.value;
                });
                btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Zapisuję…')}`;
                await _ddnsSaveConfig();
            });
        });

        // Toggle active
        content.querySelectorAll('.dd-toggle-active').forEach(btn => {
            btn.addEventListener('click', () => {
                const idx = parseInt(btn.dataset.idx);
                const p = ddnsConfig.providers[idx];
                if (p) { p.active = p.active === false ? true : false; _ddnsSaveConfig(); }
            });
        });

        // Remove provider
        content.querySelectorAll('.dd-remove-prov').forEach(btn => {
            btn.addEventListener('click', async () => {
                const idx = parseInt(btn.dataset.idx);
                const p = ddnsConfig.providers[idx];
                if (!await confirmDialog(t('Usunąć provider') + ' ' + (p?.name || '') + '?')) return;
                ddnsConfig.providers.splice(idx, 1);
                _ddnsSaveConfig();
            });
        });

        // Field changes
        content.querySelectorAll('.dm-prov-fields input, .dm-prov-fields select').forEach(el => {
            el.addEventListener('change', () => {
                const idx = parseInt(el.dataset.idx);
                if (ddnsConfig.providers[idx]) ddnsConfig.providers[idx][el.dataset.key] = el.value;
            });
        });
    }

    async function _ddnsSaveConfig() {
        content.querySelectorAll('.dm-prov-fields input, .dm-prov-fields select').forEach(el => {
            const idx = parseInt(el.dataset.idx);
            if (ddnsConfig.providers[idx]) ddnsConfig.providers[idx][el.dataset.key] = el.value;
        });
        const payload = {
            providers: ddnsConfig.providers || [],
            interval_min: parseInt(content.querySelector('#dd-interval')?.value || '5'),
            enabled: content.querySelector('#dd-enabled')?.checked || false,
        };
        try {
            await api(API_DDNS + '/config', { method: 'POST', body: payload });
            ddnsConfig = await api(API_DDNS + '/config');
            ddnsStatus = await api(API_DDNS + '/status');
            _renderDdnsData();
            toast('Konfiguracja zapisana', 'success');
        } catch (e) { toast(t('Błąd zapisu: ') + e.message, 'error'); }
    }

    // ═══════════════════════════════════════════════════════════
    //  NGINX TAB
    // ═══════════════════════════════════════════════════════════

    async function renderNginx() {
        content.innerHTML = `<div class="dm-msg dm-msg-warn"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
        let status;
        try { status = await api(API_D + '/domains/nginx-status'); }
        catch (e) { content.innerHTML = `<div class="dm-msg dm-msg-err">${esc(e.message)}</div>`; return; }

        let html = '<div class="dm-toolbar"><h2><i class="fas fa-server dom-icon-primary"></i> Status Nginx</h2></div>';

        if (!status.installed) {
            html += `<div class="dm-group"><div class="dm-msg dm-msg-err"><i class="fas fa-times-circle"></i> Nginx nie jest zainstalowany</div>
                <div class="dm-actions"><button class="dm-btn primary" id="dm-ng-install"><i class="fas fa-download"></i> Zainstaluj Nginx</button></div></div>`;
        } else {
            // Status cards
            let uptimeStr = '—';
            if (status.uptime_since) {
                try {
                    const since = new Date(status.uptime_since);
                    const diffMs = Date.now() - since.getTime();
                    if (diffMs > 0) {
                        const d = Math.floor(diffMs / 86400000);
                        const h = Math.floor((diffMs % 86400000) / 3600000);
                        const m = Math.floor((diffMs % 3600000) / 60000);
                        uptimeStr = (d > 0 ? `${d}d ` : '') + `${h}h ${m}m`;
                    }
                } catch(e) { uptimeStr = status.uptime_since; }
            }
            html += `<div class="dm-info-grid">
                <div class="dm-info-card">
                    <div class="dm-info-label">Status</div>
                    <div class="dm-info-value" style="color:${status.active ? '#22c55e' : '#ef4444'}">${status.active ? '<i class="fas fa-check-circle"></i> Aktywny' : '<i class="fas fa-times-circle"></i> Nieaktywny'}</div>
                </div>
                <div class="dm-info-card">
                    <div class="dm-info-label">Konfiguracja</div>
                    <div class="dm-info-value" style="color:${status.config_ok ? '#22c55e' : '#ef4444'}">${status.config_ok ? '<i class="fas fa-check"></i> OK' : `<i class="fas fa-times"></i> ${t('Błąd')}`}</div>
                </div>
                <div class="dm-info-card">
                    <div class="dm-info-label">Uptime</div>
                    <div class="dm-info-value">${uptimeStr}</div>
                </div>
            </div>`;

            // Action buttons
            html += `<div class="dm-group">
                <div class="dm-group-title"><i class="fas fa-tools"></i> ${t('Zarządzanie')}</div>
                <div class="dm-actions">
                    <button class="dm-btn primary dm-ng-action" data-action="reload"><i class="fas fa-sync-alt"></i> ${t('Przeładuj konfigurację')}</button>
                    <button class="dm-btn info dm-ng-action" data-action="restart"><i class="fas fa-redo"></i> Restart</button>
                    ${status.active
                        ? '<button class="dm-btn danger dm-ng-action" data-action="stop"><i class="fas fa-stop"></i> Zatrzymaj</button>'
                        : '<button class="dm-btn primary dm-ng-action" data-action="start"><i class="fas fa-play"></i> Uruchom</button>'
                    }
                </div>
                <div class="dm-hint dom-mt-sm">${t('Przeładowanie stosuje nową konfigurację bez przerwy w działaniu. Restart zatrzymuje i uruchamia ponownie cały serwer.')}</div>
            </div>`;

            // Config test
            if (status.config_test) {
                html += `<div class="dm-group"><div class="dm-group-title"><i class="fas fa-clipboard-check"></i> Wynik testu konfiguracji</div>
                    <pre class="dom-pre-block">${esc(status.config_test)}</pre></div>`;
            }
        }
        content.innerHTML = html;

        content.querySelector('#dm-ng-install')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Instalowanie…';
            try { const r = await api(API_D + '/domains/install-nginx', { method: 'POST' }); if (r.error) throw new Error(r.error); toast(r.message, 'success'); }
            catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
            renderNginx();
        });

        content.querySelectorAll('.dm-ng-action').forEach(btn => {
            btn.addEventListener('click', async () => {
                const action = btn.dataset.action;
                const labels = { reload: t('Przeładowuję…'), restart: t('Restartuję…'), stop: t('Zatrzymuję…'), start: 'Uruchamiam…' };
                btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${labels[action] || t('Wykonuję…')}`;
                try {
                    const r = await api(API_D + '/domains/nginx-action', { method: 'POST', body: { action } });
                    if (r.error) throw new Error(r.error);
                    toast(r.message || `Nginx ${action} OK`, 'success');
                } catch (err) { toast(t('Błąd: ') + err.message, 'error'); }
                renderNginx();
            });
        });
    }

    // ── Initial load ──
    renderTab();
}
