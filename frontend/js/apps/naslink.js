/**
 * EthOS — NASLink
 * Centrum zarządzania powiązanymi NAS-ami: serwery, transfery, snapshoty, klucze SSH
 */

AppRegistry['naslink'] = function (appDef, launchOpts) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('naslink', level, msg, details) : console.log('[naslink]', msg, details || '');

    const winId = 'naslink';
    if (WM.windows.has(winId)) return;

    createWindow(winId, {
        title: 'NASLink',
        icon: appDef?.icon || 'fa-network-wired',
        iconColor: appDef?.color || '#06b6d4',
        width: 960,
        height: 620,
        minWidth: 700,
        minHeight: 460,
        onRender: (body) => _nlRender(body, launchOpts),
        onClose: () => _nlCleanup(),
    });
};

/* ═══════════════════════════════════════════════════════════════ */
/*  NASLink cleanup on window close                              */
/* ═══════════════════════════════════════════════════════════════ */

function _nlCleanup() {
    if (window._nlSocketBound && window.NAS?.socket) {
        if (window._nlOnDetail) NAS.socket.off('fileop_transfer_detail', window._nlOnDetail);
        if (window._nlOnComplete) NAS.socket.off('fileop_complete', window._nlOnComplete);
        if (window._nlOnError) NAS.socket.off('fileop_error', window._nlOnError);
        if (window._nlOnPaused) NAS.socket.off('fileop_paused', window._nlOnPaused);
        window._nlSocketBound = false;
        window._nlOnDetail = null;
        window._nlOnComplete = null;
        window._nlOnError = null;
        window._nlOnPaused = null;
    }
}

/* ═══════════════════════════════════════════════════════════════ */
/*  NASLink main render                                          */
/* ═══════════════════════════════════════════════════════════════ */

function _nlRender(body, launchOpts) {
    let tab = launchOpts?.tab || 'dashboard';
    let initPaths = launchOpts?.paths || [];
    let servers = [];
    let transferHistory = [];
    let snapshots = [];
    let receivedSnaps = [];
    let remoteSnaps = [];
    let _discoverResults = [];
    let _transferActive = null; // current active transfer info

    const CSS = `
    <style>
    .nl { display:flex; height:100%; font-family:var(--font-sans); color:var(--text-primary); background:var(--bg-primary); overflow:hidden; }
    .nl-sidebar { width:200px; min-width:200px; background:var(--bg-secondary); border-right:1px solid var(--border); display:flex; flex-direction:column; padding:10px 0; }
    .nl-nav-item { display:flex; align-items:center; gap:10px; padding:10px 18px; cursor:pointer; font-size:13px; color:var(--text-secondary); transition:all .15s; border-left:3px solid transparent; }
    .nl-nav-item:hover { background:var(--bg-hover); color:var(--text-primary); }
    .nl-nav-item.active { color:var(--accent); border-left-color:var(--accent); background:rgba(var(--accent-rgb),.08); font-weight:600; }
    .nl-nav-item i { width:18px; text-align:center; font-size:14px; }
    .nl-nav-item .nl-badge { background:var(--accent); color:#fff; font-size:10px; padding:1px 6px; border-radius:10px; margin-left:auto; font-weight:600; }
    .nl-content { flex:1; overflow-y:auto; padding:24px; }

    /* Dashboard */
    .nl-dash-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px; margin-bottom:24px; }
    .nl-dash-card { background:var(--bg-secondary); border:1px solid var(--border); border-radius:var(--r-md); padding:20px; }
    .nl-dash-card h3 { margin:0 0 4px; font-size:14px; font-weight:600; display:flex; align-items:center; gap:8px; }
    .nl-dash-card h3 i { color:var(--accent); }
    .nl-dash-card .nl-stat { font-size:28px; font-weight:700; color:var(--text-primary); margin:12px 0 4px; }
    .nl-dash-card .nl-stat-label { font-size:12px; color:var(--text-muted); }
    .nl-dash-card .nl-status { display:inline-flex; align-items:center; gap:6px; font-size:12px; padding:3px 10px; border-radius:12px; }
    .nl-dash-card .nl-status.online { background:rgba(34,197,94,.12); color:#22c55e; }
    .nl-dash-card .nl-status.offline { background:rgba(239,68,68,.12); color:#ef4444; }
    .nl-dash-card .nl-status.unknown { background:rgba(100,116,139,.12); color:#64748b; }

    /* Servers list */
    .nl-server-list { display:flex; flex-direction:column; gap:10px; }
    .nl-server-card { background:var(--bg-secondary); border:1px solid var(--border); border-radius:var(--r-md); padding:16px 20px; display:flex; align-items:center; gap:16px; transition:border-color .15s; }
    .nl-server-card:hover { border-color:var(--accent); }
    .nl-server-card .nl-srv-icon { width:44px; height:44px; border-radius:var(--r-sm); background:rgba(var(--accent-rgb),.1); display:flex; align-items:center; justify-content:center; font-size:20px; color:var(--accent); }
    .nl-server-card .nl-srv-info { flex:1; }
    .nl-server-card .nl-srv-name { font-weight:600; font-size:14px; }
    .nl-server-card .nl-srv-host { font-size:12px; color:var(--text-muted); margin-top:2px; }
    .nl-server-card .nl-srv-actions { display:flex; gap:6px; }
    .nl-server-card .nl-srv-dot { width:8px; height:8px; border-radius:50%; }
    .nl-server-card .nl-srv-dot.online { background:#22c55e; }
    .nl-server-card .nl-srv-dot.offline { background:#ef4444; }
    .nl-server-card .nl-srv-dot.checking { background:#eab308; animation:pulse 1s infinite; }

    /* Buttons */
    .nl-btn { padding:7px 14px; border:1px solid var(--border); border-radius:var(--r-sm); background:var(--bg-secondary); color:var(--text-primary); cursor:pointer; font-size:12px; transition:all .15s; display:inline-flex; align-items:center; gap:6px; }
    .nl-btn:hover { background:var(--bg-hover); border-color:var(--text-muted); }
    .nl-btn.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
    .nl-btn.primary:hover { filter:brightness(1.1); }
    .nl-btn.danger { color:#ef4444; border-color:rgba(239,68,68,.3); }
    .nl-btn.danger:hover { background:rgba(239,68,68,.08); }
    .nl-btn.sm { padding:4px 10px; font-size:11px; }
    .nl-btn:disabled { opacity:.5; cursor:default; }

    /* Toolbar */
    .nl-toolbar { display:flex; align-items:center; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
    .nl-toolbar h2 { font-size:18px; font-weight:700; margin:0; flex:1; display:flex; align-items:center; gap:10px; }
    .nl-toolbar h2 i { color:var(--accent); }

    /* Transfer section */
    .nl-transfer-card { background:var(--bg-secondary); border:1px solid var(--border); border-radius:var(--r-md); padding:16px 20px; margin-bottom:10px; }
    .nl-transfer-active { border-color:var(--accent); }
    .nl-transfer-card .nl-tf-header { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
    .nl-transfer-card .nl-tf-header i { color:var(--accent); }
    .nl-transfer-card .nl-tf-header .nl-tf-title { font-weight:600; font-size:13px; flex:1; }
    .nl-transfer-card .nl-tf-bar { height:6px; background:var(--bg-hover); border-radius:3px; overflow:hidden; margin:8px 0; }
    .nl-transfer-card .nl-tf-fill { height:100%; background:var(--accent); border-radius:3px; transition:width .3s; }
    .nl-transfer-card .nl-tf-detail { font-size:12px; color:var(--text-muted); }

    /* Snapshots */
    .nl-snap-list { display:flex; flex-direction:column; gap:8px; }
    .nl-snap-card { background:var(--bg-secondary); border:1px solid var(--border); border-radius:var(--r-md); padding:14px 18px; display:flex; align-items:center; gap:14px; }
    .nl-snap-card.received { border-left:3px solid #f97316; }
    .nl-snap-card.remote { border-left:3px solid #8b5cf6; }
    .nl-snap-icon { width:36px; height:36px; border-radius:var(--r-sm); display:flex; align-items:center; justify-content:center; font-size:16px; }
    .nl-snap-info { flex:1; }
    .nl-snap-name { font-weight:600; font-size:13px; }
    .nl-snap-meta { font-size:11px; color:var(--text-muted); margin-top:2px; }
    .nl-snap-badge { font-size:10px; padding:2px 8px; border-radius:10px; font-weight:600; }
    .nl-snap-badge.local { background:rgba(6,182,212,.12); color:#06b6d4; }
    .nl-snap-badge.received { background:rgba(249,115,22,.12); color:#f97316; }
    .nl-snap-badge.remote { background:rgba(139,92,246,.12); color:#8b5cf6; }

    /* Form */
    .nl-form-row { margin-bottom:12px; }
    .nl-form-row label { display:block; font-size:12px; font-weight:600; color:var(--text-secondary); margin-bottom:4px; }
    .nl-form-row input, .nl-form-row select { width:100%; padding:8px 12px; border:1px solid var(--border); border-radius:var(--r-sm); background:var(--bg-primary); color:var(--text-primary); font-size:13px; box-sizing:border-box; }
    .nl-form-row input:focus, .nl-form-row select:focus { outline:none; border-color:var(--accent); }
    .nl-form-grid { display:grid; grid-template-columns:1fr 1fr; gap:0 16px; }
    .nl-form-full { grid-column:1/-1; }

    /* Empty state */
    .nl-empty { text-align:center; padding:48px 20px; color:var(--text-muted); }
    .nl-empty i { font-size:40px; margin-bottom:12px; display:block; opacity:.4; }
    .nl-empty p { margin:0 0 16px; font-size:13px; }

    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }
    </style>
    `;

    body.innerHTML = CSS + `
    <div class="nl">
        <div class="nl-sidebar">
            <div class="nl-nav-item" data-tab="dashboard"><i class="fas fa-tachometer-alt"></i> Dashboard</div>
            <div class="nl-nav-item" data-tab="servers"><i class="fas fa-server"></i> Serwery <span class="nl-badge nl-badge-servers" style="display:none"></span></div>
            <div class="nl-nav-item" data-tab="transfer"><i class="fas fa-exchange-alt"></i> Transfer</div>
            <div class="nl-nav-item" data-tab="snapshots"><i class="fas fa-camera"></i> Snapshoty</div>
        </div>
        <div class="nl-content" id="nl-content"></div>
    </div>
    `;

    const sidebar = body.querySelector('.nl-sidebar');
    const content = body.querySelector('#nl-content');

    sidebar.addEventListener('click', (e) => {
        const item = e.target.closest('.nl-nav-item');
        if (!item) return;
        tab = item.dataset.tab;
        sidebar.querySelectorAll('.nl-nav-item').forEach(n => n.classList.toggle('active', n.dataset.tab === tab));
        renderTab();
    });

    function setActiveTab() {
        sidebar.querySelectorAll('.nl-nav-item').forEach(n => n.classList.toggle('active', n.dataset.tab === tab));
    }

    // ─── Data loading ────────────────────────────────────────────

    async function loadServers() {
        try {
            const r = await api('/backup/ssh-servers');
            servers = r.servers || r || [];
        } catch { servers = []; }
    }

    async function loadSnapshots() {
        try {
            const r = await api('/backup/snapshots');
            snapshots = r.snapshots || [];
        } catch { snapshots = []; }
    }

    async function loadReceivedSnaps() {
        try {
            const r = await api('/backup/snapshots/received');
            receivedSnaps = r.snapshots || [];
        } catch { receivedSnaps = []; }
    }

    // ─── Tab Router ──────────────────────────────────────────────

    async function renderTab() {
        switch (tab) {
            case 'dashboard': await renderDashboard(); break;
            case 'servers': await renderServers(); break;
            case 'transfer': await renderTransfer(); break;
            case 'snapshots': await renderSnapshots(); break;
        }
    }

    // ═══════════════════════════════════════════════════════════════
    //  DASHBOARD
    // ═══════════════════════════════════════════════════════════════

    async function renderDashboard() {
        content.innerHTML = `<div class="nl-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
        await loadServers();
        await loadReceivedSnaps();

        const serverCount = servers.length;
        const receivedCount = receivedSnaps.length;

        content.innerHTML = `
            <div class="nl-toolbar">
                <h2><i class="fas fa-tachometer-alt"></i> NASLink — Dashboard</h2>
            </div>
            <div class="nl-dash-grid">
                <div class="nl-dash-card">
                    <h3><i class="fas fa-server"></i> ${t('Połączone serwery')}</h3>
                    <div class="nl-stat">${serverCount}</div>
                    <div class="nl-stat-label">${t('skonfigurowanych serwerów SSH')}</div>
                </div>
                <div class="nl-dash-card">
                    <h3><i class="fas fa-exchange-alt"></i> Transfer</h3>
                    <div class="nl-stat" id="nl-dash-transfer">—</div>
                    <div class="nl-stat-label">aktywny transfer</div>
                </div>
                <div class="nl-dash-card">
                    <h3><i class="fas fa-inbox"></i> Odebrane snapshoty</h3>
                    <div class="nl-stat">${receivedCount}</div>
                    <div class="nl-stat-label">${t('oczekujących na ten NAS')}</div>
                </div>
            </div>

            <h3 class="nl-section-title">
                <i class="fas fa-server nl-icon-accent"></i> ${t('Status serwerów')}
            </h3>
            <div class="nl-server-list" id="nl-dash-servers">
                ${servers.length === 0 ? `<div class="nl-empty"><i class="fas fa-plug"></i><p>${t('Brak skonfigurowanych serwerów')}</p><button class="nl-btn primary" id="nl-dash-add-server"><i class="fas fa-plus"></i> ${t('Dodaj serwer')}</button></div>` : ''}
                ${servers.map(s => `
                    <div class="nl-server-card" data-id="${s.id}">
                        <div class="nl-srv-icon"><i class="fas fa-server"></i></div>
                        <div class="nl-srv-info">
                            <div class="nl-srv-name">${_nlEsc(s.name || s.host)}</div>
                            <div class="nl-srv-host">${_nlEsc(s.host)}:${s.port || 22} — ${_nlEsc(s.username || '')}</div>
                        </div>
                        <div class="nl-srv-dot checking" data-host="${_nlEsc(s.host)}" data-port="${s.port || 22}" title="Sprawdzanie…"></div>
                    </div>
                `).join('')}
            </div>
        `;

        const addBtn = content.querySelector('#nl-dash-add-server');
        if (addBtn) addBtn.addEventListener('click', () => { tab = 'servers'; setActiveTab(); renderTab(); });

        // Check server availability in parallel
        _nlCheckServersStatus(content);

        // Check active transfer
        _nlUpdateDashTransfer();
    }

    async function _nlUpdateDashTransfer() {
        try {
            const r = await api('/notifications');
            const notifs = r.notifications || [];
            const tfNotif = notifs.find(n => n.type === 'progress' && n.title?.includes('Transfer'));
            const el = content.querySelector('#nl-dash-transfer');
            if (!el) return;
            if (tfNotif) {
                el.textContent = tfNotif.message || 'W toku…';
                el.style.color = 'var(--accent)';
            } else {
                el.textContent = 'Brak';
                el.style.color = '';
            }
        } catch { /* ignore */ }
    }

    async function _nlCheckServersStatus(container) {
        for (const s of servers) {
            const dot = container.querySelector(`.nl-srv-dot[data-host="${s.host}"]`);
            if (!dot) continue;
            try {
                const r = await api('/backup/ssh-servers/test', { method: 'POST', body: { id: s.id } });
                if (r.success) {
                    dot.className = 'nl-srv-dot online';
                    dot.title = `Online — ${r.disk_info || ''}`;
                } else {
                    dot.className = 'nl-srv-dot offline';
                    dot.title = r.error || 'Offline';
                }
            } catch {
                dot.className = 'nl-srv-dot offline';
                dot.title = t('Nieosiągalny');
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════
    //  SERVERS
    // ═══════════════════════════════════════════════════════════════

    async function renderServers() {
        content.innerHTML = '<div class="nl-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        await loadServers();

        content.innerHTML = `
            <div class="nl-toolbar">
                <h2><i class="fas fa-server"></i> Serwery NAS</h2>
                <button class="nl-btn" id="nl-discover"><i class="fas fa-search"></i> Wykryj w sieci</button>
                <button class="nl-btn primary" id="nl-add-server"><i class="fas fa-plus"></i> Dodaj serwer</button>
            </div>
            <div class="nl-server-list" id="nl-servers-list">
                ${servers.length === 0 ? `<div class="nl-empty"><i class="fas fa-plug"></i><p>${t('Brak skonfigurowanych serwerów NAS.')}<br>${t('Dodaj serwer SSH lub wykryj automatycznie w sieci.')}</p></div>` : ''}
                ${servers.map(s => _nlServerCard(s)).join('')}
            </div>
            <div id="nl-server-form-area"></div>
            <div id="nl-discover-area" class="nl-section-mt-sm"></div>
        `;

        content.querySelector('#nl-add-server').addEventListener('click', () => _nlShowServerForm());
        content.querySelector('#nl-discover').addEventListener('click', () => _nlRunDiscover());

        // Server card actions
        content.querySelector('#nl-servers-list').addEventListener('click', (e) => {
            const card = e.target.closest('.nl-server-card');
            if (!card) return;
            const id = card.dataset.id;
            const btn = e.target.closest('.nl-btn');
            if (!btn) return;
            if (btn.classList.contains('nl-srv-test')) _nlTestServer(id);
            else if (btn.classList.contains('nl-srv-edit')) _nlEditServer(id);
            else if (btn.classList.contains('nl-srv-delete')) _nlDeleteServer(id);
        });

        _nlCheckServersStatus(content);
    }

    function _nlServerCard(s) {
        return `
            <div class="nl-server-card" data-id="${s.id}">
                <div class="nl-srv-icon"><i class="fas fa-server"></i></div>
                <div class="nl-srv-info">
                    <div class="nl-srv-name">${_nlEsc(s.name || s.host)}</div>
                    <div class="nl-srv-host">${_nlEsc(s.host)}:${s.port || 22} — ${_nlEsc(s.username || '')}${s.has_key ? ' <i class="fas fa-key nl-key-icon" title="Klucz SSH"></i>' : ''}${s.has_password ? ` <i class="fas fa-lock nl-lock-icon" title="${t('Hasło')}"></i>` : ''}</div>
                    <div class="nl-sub-info">${t('Ścieżka:')} ${_nlEsc(s.remote_path || '~/')}</div>
                </div>
                <div class="nl-srv-dot checking" data-host="${_nlEsc(s.host)}" data-port="${s.port || 22}" title="Sprawdzanie…"></div>
                <div class="nl-srv-actions">
                    <button class="nl-btn sm nl-srv-test" title="${t('Test połączenia')}"><i class="fas fa-plug"></i></button>
                    <button class="nl-btn sm nl-srv-edit" title="Edytuj"><i class="fas fa-edit"></i></button>
                    <button class="nl-btn sm danger nl-srv-delete" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                </div>
            </div>
        `;
    }

    async function _nlTestServer(id) {
        const card = content.querySelector(`.nl-server-card[data-id="${id}"]`);
        if (!card) return;
        const dot = card.querySelector('.nl-srv-dot');
        dot.className = 'nl-srv-dot checking';
        dot.title = 'Testowanie…';
        try {
            const r = await api('/backup/ssh-servers/test', { method: 'POST', body: { id } });
            if (r.success) {
                dot.className = 'nl-srv-dot online';
                dot.title = `Online — ${r.disk_info || ''}`;
                toast(`${r.name || 'Serwer'}${t(': połączenie OK')}`, 'success');
            } else {
                dot.className = 'nl-srv-dot offline';
                dot.title = r.error || 'Offline';
                toast(r.error || t('Błąd połączenia'), 'error');
            }
        } catch (e) {
            dot.className = 'nl-srv-dot offline';
            toast(t('Błąd: ') + (e.message || e), 'error');
        }
    }

    async function _nlDeleteServer(id) {
        const s = servers.find(x => x.id === id);
        if (!s) return;
        const ok = await confirmDialog(t('Usunąć serwer') + ' "' + (s.name || s.host) + '"?');
        if (!ok) return;
        try {
            await api(`/backup/ssh-servers/${id}`, { method: 'DELETE' });
            toast(t('Serwer usunięty'), 'info');
            renderServers();
        } catch (e) {
            toast(t('Błąd: ') + (e.message || e), 'error');
        }
    }

    function _nlEditServer(id) {
        const s = servers.find(x => x.id === id);
        if (s) _nlShowServerForm(s);
    }

    function _nlShowServerForm(existing) {
        const area = content.querySelector('#nl-server-form-area');
        area.innerHTML = `
            <div class="nl-panel-accent">
                <h3 class="nl-form-title">${existing ? t('Edytuj serwer') : t('Nowy serwer SSH')}</h3>
                <div class="nl-form-grid">
                    <div class="nl-form-row"><label>${t('Nazwa')}</label><input id="nl-sf-name" value="${_nlEsc(existing?.name || '')}" placeholder="np. NAS Salon"></div>
                    <div class="nl-form-row"><label>${t('Host (IP)')}</label><input id="nl-sf-host" value="${_nlEsc(existing?.host || '')}" placeholder="192.168.50.xxx"></div>
                    <div class="nl-form-row"><label>Port</label><input id="nl-sf-port" type="number" value="${existing?.port || 22}"></div>
                    <div class="nl-form-row"><label>${t('Użytkownik')}</label><input id="nl-sf-user" value="${_nlEsc(existing?.username || '')}"></div>
                    <div class="nl-form-row"><label>${t('Hasło')}</label><input id="nl-sf-pass" type="password" value="" placeholder="${existing ? t('(bez zmian)') : ''}"></div>
                    <div class="nl-form-row"><label>${t('Klucz SSH (ścieżka)')}</label><input id="nl-sf-key" value="${_nlEsc(existing?.key_path || '')}" placeholder="${t('opcjonalnie')}"></div>
                    <div class="nl-form-row nl-form-full"><label>${t('Ścieżka zdalna')}</label><input id="nl-sf-path" value="${_nlEsc(existing?.remote_path || '~/backups')}" placeholder="~/backups"></div>
                </div>
                <div class="nl-form-actions">
                    <button class="nl-btn primary" id="nl-sf-save"><i class="fas fa-save"></i> Zapisz</button>
                    <button class="nl-btn" id="nl-sf-test"><i class="fas fa-plug"></i> Testuj</button>
                    <button class="nl-btn" id="nl-sf-cancel"><i class="fas fa-times"></i> Anuluj</button>
                </div>
                <div id="nl-sf-result" class="nl-result-msg"></div>
            </div>
        `;

        area.querySelector('#nl-sf-cancel').addEventListener('click', () => { area.innerHTML = ''; });

        area.querySelector('#nl-sf-save').addEventListener('click', async () => {
            const data = _nlGetFormData();
            if (existing) data.id = existing.id;
            try {
                const r = await api('/backup/ssh-servers', { method: 'POST', body: data });
                if (r.error) { toast(r.error, 'error'); return; }
                toast(existing ? t('Serwer zaktualizowany') : t('Serwer dodany'), 'success');
                area.innerHTML = '';
                renderServers();
            } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
        });

        area.querySelector('#nl-sf-test').addEventListener('click', async () => {
            const res = area.querySelector('#nl-sf-result');
            res.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Testowanie…';
            const data = _nlGetFormData();
            try {
                const r = await api('/backup/ssh-servers/test', { method: 'POST', body: data });
                if (r.success) {
                    res.innerHTML = `<span class="nl-success"><i class="fas fa-check-circle"></i> ${t('Połączono!')} ${r.disk_info || ''}</span>`;
                } else {
                    res.innerHTML = `<span class="nl-error"><i class="fas fa-times-circle"></i> ${_nlEsc(r.error || t('Błąd'))}</span>`;
                }
            } catch (e) {
                res.innerHTML = `<span class="nl-error"><i class="fas fa-times-circle"></i> ${_nlEsc(e.message || e)}</span>`;
            }
        });
    }

    function _nlGetFormData() {
        return {
            name: content.querySelector('#nl-sf-name').value.trim(),
            host: content.querySelector('#nl-sf-host').value.trim(),
            port: parseInt(content.querySelector('#nl-sf-port').value) || 22,
            username: content.querySelector('#nl-sf-user').value.trim(),
            password: content.querySelector('#nl-sf-pass').value,
            key_path: content.querySelector('#nl-sf-key').value.trim(),
            remote_path: content.querySelector('#nl-sf-path').value.trim() || '~/backups',
        };
    }

    // ─── Discovery ───────────────────────────────────────────────

    async function _nlRunDiscover() {
        const area = content.querySelector('#nl-discover-area');
        area.innerHTML = `<div class="nl-panel">
            <i class="fas fa-spinner fa-spin"></i> ${t('Szukam urządzeń EthOS w sieci…')}
        </div>`;
        try {
            const r = await api('/backup/discover-nas', { method: 'POST' });
            _discoverResults = r.devices || [];
            if (_discoverResults.length === 0) {
                area.innerHTML = `<div class="nl-panel nl-text-muted">
                    <i class="fas fa-info-circle"></i> ${t('Nie znaleziono innych urządzeń EthOS w sieci.')}
                </div>`;
                return;
            }
            area.innerHTML = `<div class="nl-panel">
                <h4 class="nl-discover-title"><i class="fas fa-broadcast-tower nl-icon-accent"></i> ${t('Znalezione urządzenia')} (${_discoverResults.length})</h4>
                ${_discoverResults.map(d => `
                    <div class="nl-discover-row">
                        <i class="fas ${d.source === 'vm' ? 'fa-desktop' : 'fa-server'} nl-icon-accent"></i>
                        <div class="nl-flex-1">
                            <div class="nl-discover-name">${_nlEsc(d.name || d.hostname || d.ip)}${d.source === 'vm' ? ' <span class="nl-snap-badge local">VM</span>' : ''}</div>
                            <div class="nl-muted-sm">${_nlEsc(d.ip)}:${d.port || 9000} • v${_nlEsc(d.version || '?')}</div>
                        </div>
                        <button class="nl-btn sm primary nl-discover-add" data-ip="${_nlEsc(d.ip)}" data-port="${d.port || 9000}" data-name="${_nlEsc(d.name || d.hostname || '')}"><i class="fas fa-plus"></i> ${t('Dodaj')}</button>
                    </div>
                `).join('')}
            </div>`;

            area.querySelectorAll('.nl-discover-add').forEach(btn => {
                btn.addEventListener('click', () => {
                    _nlShowServerForm({
                        name: btn.dataset.name || 'NAS',
                        host: btn.dataset.ip,
                        port: 22,
                        username: '',
                        remote_path: '~/backups',
                    });
                });
            });
        } catch (e) {
            area.innerHTML = `<div class="nl-panel nl-error">
                <i class="fas fa-exclamation-triangle"></i> ${t('Błąd wykrywania:')} ${_nlEsc(e.message || e)}
            </div>`;
        }
    }

    // ═══════════════════════════════════════════════════════════════
    //  TRANSFER
    // ═══════════════════════════════════════════════════════════════

    async function renderTransfer() {
        content.innerHTML = '<div class="nl-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        await loadServers();

        // Check current operation status
        let opStatus = null;
        try { opStatus = await api('/files/operation-status'); } catch { /* ignore */ }

        const hasActive = opStatus?.active && opStatus?.operation === 'transfer';
        const lastResult = opStatus?.result;
        const hasRecent = lastResult && lastResult.time && (Date.now()/1000 - lastResult.time) < 300;
        const meta = opStatus?.meta || {};
        const srvLabel = meta.server_name ? `${meta.server_name} (${meta.server_host || ''})` : '';
        const filesLabel = meta.paths?.length ? meta.paths.join(', ') : '';
        const resumedBadge = meta.resumed ? ' <span class="nl-badge-resumed">wznowiono</span>' : '';

        let statusHtml;
        const isPaused = opStatus?.paused || false;
        if (hasActive && opStatus.progress) {
            const p = opStatus.progress;
            statusHtml = `
                <div class="nl-transfer-card nl-transfer-active" id="nl-tf-status">
                    <div class="nl-tf-header"><i class="fas ${isPaused ? 'fa-pause-circle' : 'fa-spinner fa-spin'}" style="color:${isPaused ? '#eab308' : 'var(--accent)'}"></i>
                        <div class="nl-tf-title" id="nl-tf-title">${isPaused ? t('Transfer wstrzymany') : t('Transfer w toku')} — ${Math.round(p.percent || 0)}%${resumedBadge}</div></div>
                    ${srvLabel ? `<div class="nl-tf-srv-label"><i class="fas fa-arrow-right nl-tf-arrow"></i> <strong>${_nlEsc(srvLabel)}</strong>${filesLabel ? ' &mdash; ' + _nlEsc(filesLabel) : ''}</div>` : ''}
                    <div class="nl-tf-bar" id="nl-tf-bar-wrap"><div class="nl-tf-fill" id="nl-tf-fill" style="width:${p.percent||0}%;${isPaused ? 'background:#eab308;' : ''}"></div></div>
                    <div class="nl-tf-detail" id="nl-tf-detail">${p.done||0}/${p.total||0} ${t('plików')} — ${Math.round(p.percent||0)}%</div>
                    <div id="nl-tf-actions" class="nl-tf-actions-row">
                        <button class="nl-btn sm" id="nl-tf-pause" title="${isPaused ? t('Wznów') : t('Wstrzymaj')}"><i class="fas ${isPaused ? 'fa-play' : 'fa-pause'}"></i> ${isPaused ? t('Wznów') : t('Wstrzymaj')}</button>
                        <button class="nl-btn sm danger" id="nl-tf-cancel"><i class="fas fa-times"></i> ${t('Anuluj')}</button>
                    </div>
                </div>`;
        } else if (hasActive) {
            statusHtml = `
                <div class="nl-transfer-card nl-transfer-active" id="nl-tf-status">
                    <div class="nl-tf-header"><i class="fas fa-spinner fa-spin nl-icon-accent"></i>
                        <div class="nl-tf-title" id="nl-tf-title">Transfer w toku…</div></div>
                    ${srvLabel ? `<div class="nl-tf-srv-label"><i class="fas fa-arrow-right nl-tf-arrow"></i> <strong>${_nlEsc(srvLabel)}</strong>${filesLabel ? ' &mdash; ' + _nlEsc(filesLabel) : ''}</div>` : ''}
                    <div class="nl-tf-bar" style="display:none" id="nl-tf-bar-wrap"><div class="nl-tf-fill" id="nl-tf-fill" style="width:0%"></div></div>
                    <div class="nl-tf-detail" id="nl-tf-detail"></div>
                    <div id="nl-tf-actions" class="nl-tf-actions-row">
                        <button class="nl-btn sm" id="nl-tf-pause"><i class="fas fa-pause"></i> Wstrzymaj</button>
                        <button class="nl-btn sm danger" id="nl-tf-cancel"><i class="fas fa-times"></i> Anuluj</button>
                    </div>
                </div>`;
        } else if (hasRecent && lastResult.operation === 'transfer') {
            const isOk = lastResult.status === 'completed';
            statusHtml = `
                <div class="nl-transfer-card" id="nl-tf-status">
                    <div class="nl-tf-header">
                        <i class="fas ${isOk ? 'fa-check-circle' : 'fa-times-circle'}" style="color:${isOk ? '#22c55e' : '#ef4444'}"></i>
                        <div class="nl-tf-title" id="nl-tf-title">${_nlEsc(lastResult.message || (isOk ? t('Zakończono') : t('Błąd')))}</div></div>
                    <div class="nl-tf-bar" ${isOk ? '' : 'style="display:none"'} id="nl-tf-bar-wrap"><div class="nl-tf-fill" id="nl-tf-fill" style="width:${isOk ? '100' : '0'}%"></div></div>
                    <div class="nl-tf-detail" id="nl-tf-detail"></div>
                    <div id="nl-tf-actions" style="display:none"><button class="nl-btn sm" id="nl-tf-pause" style="display:none"><i class="fas fa-pause"></i></button><button class="nl-btn sm danger" id="nl-tf-cancel" style="display:none"><i class="fas fa-times"></i> Anuluj</button></div>
                </div>`;
        } else {
            statusHtml = `
                <div class="nl-transfer-card" id="nl-tf-status">
                    <div class="nl-tf-header"><i class="fas fa-info-circle nl-icon-muted"></i>
                        <div class="nl-tf-title" id="nl-tf-title">Brak aktywnego transferu</div></div>
                    <div class="nl-tf-bar" style="display:none" id="nl-tf-bar-wrap"><div class="nl-tf-fill" id="nl-tf-fill" style="width:0%"></div></div>
                    <div class="nl-tf-detail" id="nl-tf-detail"></div>
                    <div id="nl-tf-actions" style="display:none"><button class="nl-btn sm" id="nl-tf-pause" style="display:none"><i class="fas fa-pause"></i></button><button class="nl-btn sm danger" id="nl-tf-cancel" style="display:none"><i class="fas fa-times"></i> Anuluj</button></div>
                </div>`;
        }

        content.innerHTML = `
            <div class="nl-toolbar">
                <h2><i class="fas fa-exchange-alt"></i> ${t('Transfer plików')}</h2>
            </div>

            ${statusHtml}

            <div class="nl-section-mt">
                <h3 class="nl-section-title"><i class="fas fa-paper-plane nl-icon-accent"></i> Nowy transfer</h3>
                ${servers.length === 0 ? `<div class="nl-empty"><i class="fas fa-plug"></i><p>${t('Brak skonfigurowanych serwerów.')}<br>${t('Dodaj serwer w zakładce')} <strong>${t('Serwery')}</strong>${t('.')}</p></div>` : `
                <div class="nl-panel-form">
                    <div class="nl-form-grid">
                        <div class="nl-form-row nl-form-full">
                            <label>${t('Pliki / foldery źródłowe')}</label>
                            <div class="nl-row">
                                <input id="nl-tf-paths" value="${initPaths.length ? _nlEsc(initPaths.join(', ')) : ''}" style="flex:1;" placeholder="${t('/ścieżka/do/pliku, /inna/ścieżka (oddziel przecinkiem)')}">
                                <button class="nl-btn" id="nl-tf-browse" title="${t('Przeglądaj')}"><i class="fas fa-folder-open"></i></button>
                            </div>
                            <div class="nl-hint">${t('Podaj ścieżki do plików/folderów oddzielone przecinkiem, lub kliknij')} <i class="fas fa-folder-open"></i> ${t('aby wybrać z dysku.')}</div>
                        </div>
                        <div class="nl-form-row">
                            <label>${t('Serwer docelowy')}</label>
                            <select id="nl-tf-server">
                                ${servers.map(s => `<option value="${s.id}">${_nlEsc(s.name || s.host)} (${_nlEsc(s.host)})</option>`).join('')}
                            </select>
                        </div>
                        <div class="nl-form-row">
                            <label>${t('Ścieżka zdalna')}</label>
                            <input id="nl-tf-dest" value="${_nlEsc(servers[0]?.remote_path || '~/')}" placeholder="~/received">
                        </div>
                    </div>
                    <div class="nl-form-actions">
                        <button class="nl-btn primary" id="nl-tf-start" ${hasActive ? 'disabled' : ''}>
                            <i class="fas fa-paper-plane"></i> ${t('Rozpocznij transfer')}
                        </button>
                        <span class="nl-muted-sm">${t('rsync — automatyczne wznawianie po utracie połączenia')}</span>
                    </div>
                    <div id="nl-tf-msg" class="nl-result-msg"></div>
                </div>`}
            </div>

            <div class="nl-info-footer">
                <i class="fas fa-info-circle"></i> ${t('Możesz też zaznaczać pliki w')} <strong>${t('Menedżerze plików')}</strong> ${t('i wybrać')} <strong>${t('Transferuj do NAS')}</strong> ${t('z menu kontekstowego.')}
            </div>
        `;

        // Server change → update remote path
        const serverSelect = content.querySelector('#nl-tf-server');
        const destInput = content.querySelector('#nl-tf-dest');
        const pathsInput = content.querySelector('#nl-tf-paths');

        if (pathsInput) {
            pathsInput.addEventListener('input', () => {
                initPaths = pathsInput.value.split(',').map(s => s.trim()).filter(s => s);
            });
        }

        if (serverSelect && destInput) {
            serverSelect.addEventListener('change', () => {
                const srv = servers.find(s => s.id === serverSelect.value);
                if (srv) destInput.value = srv.remote_path || '~/';
            });
        }

        // Browse button → open navigable file picker
        const browseBtn = content.querySelector('#nl-tf-browse');
        if (browseBtn) {
            browseBtn.addEventListener('click', () => {
                const pathInput = content.querySelector('#nl-tf-paths');
                _nlFilePicker(pathInput);
            });
        }

        // Start transfer
        const startBtn = content.querySelector('#nl-tf-start');
        if (startBtn) {
            startBtn.addEventListener('click', async () => {
                const pathInput = content.querySelector('#nl-tf-paths');
                const rawPaths = pathInput.value.trim();
                if (!rawPaths) { toast(t('Podaj ścieżki do plików'), 'warning'); pathInput.focus(); return; }
                const paths = rawPaths.split(',').map(s => s.trim()).filter(Boolean);
                const serverId = serverSelect?.value;
                const remotePath = destInput?.value?.trim() || '~/';
                if (!serverId) { toast('Wybierz serwer docelowy', 'warning'); return; }

                const msgEl = content.querySelector('#nl-tf-msg');
                startBtn.disabled = true;
                startBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Rozpoczynanie…';
                if (msgEl) msgEl.innerHTML = '';

                try {
                    const r = await api('/files/transfer-remote', {
                        method: 'POST',
                        body: { server_id: serverId, paths, remote_path: remotePath }
                    });
                    if (r.error) {
                        toast(r.error, 'error');
                        if (msgEl) msgEl.innerHTML = `<span class="nl-error"><i class="fas fa-times-circle"></i> ${_nlEsc(r.error)}</span>`;
                        startBtn.disabled = false;
                        startBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Rozpocznij transfer';
                        return;
                    }
                    toast(r.message || t('Transfer rozpoczęty'), 'success');
                    if (msgEl) msgEl.innerHTML = `<span class="nl-success"><i class="fas fa-check-circle"></i> ${_nlEsc(r.message || 'Transfer w toku…')}</span>`;
                    // Refresh status card after short delay
                    setTimeout(() => renderTransfer(), 1500);
                } catch (e) {
                    toast(t('Błąd: ') + (e.message || e), 'error');
                    if (msgEl) msgEl.innerHTML = `<span class="nl-error"><i class="fas fa-times-circle"></i> ${_nlEsc(e.message || e)}</span>`;
                    startBtn.disabled = false;
                    startBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Rozpocznij transfer';
                }
            });
        }

        // Cancel button
        const cancelBtn = content.querySelector('#nl-tf-cancel');
        if (cancelBtn) {
            cancelBtn.addEventListener('click', async () => {
                try {
                    await api('/files/cancel-operation', { method: 'POST' });
                    toast('Anulowanie transferu…', 'info');
                    setTimeout(() => renderTransfer(), 2000);
                } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
            });
        }

        // Pause/Resume button
        const pauseBtn = content.querySelector('#nl-tf-pause');
        if (pauseBtn) {
            pauseBtn.addEventListener('click', async () => {
                pauseBtn.disabled = true;
                try {
                    const r = await api('/files/pause-operation', { method: 'POST' });
                    if (r.error) { toast(r.error, 'warning'); }
                    else { toast(r.message, 'info'); }
                } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
                pauseBtn.disabled = false;
            });
        }

        // Listen to SocketIO for real-time transfer updates
        _nlSetupTransferSocket();
    }

    function _nlFmtSize(bytes) {
        if (!bytes || bytes <= 0) return '';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0;
        let b = bytes;
        while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
        return b.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
    }

    function _nlSetupTransferSocket() {
        if (!window.NAS?.socket) return;

        // Use named handlers so we can properly remove them
        if (window._nlSocketBound) {
            NAS.socket.off('fileop_transfer_detail', window._nlOnDetail);
            NAS.socket.off('fileop_complete', window._nlOnComplete);
            NAS.socket.off('fileop_error', window._nlOnError);
            NAS.socket.off('fileop_paused', window._nlOnPaused);
        }

        window._nlOnDetail = (data) => {
            const title = content.querySelector('#nl-tf-title');
            const barWrap = content.querySelector('#nl-tf-bar-wrap');
            const fill = content.querySelector('#nl-tf-fill');
            const detail = content.querySelector('#nl-tf-detail');
            const actionsWrap = content.querySelector('#nl-tf-actions');
            const pauseBtn = content.querySelector('#nl-tf-pause');
            const statusCard = content.querySelector('#nl-tf-status');
            const startBtn = content.querySelector('#nl-tf-start');
            if (!title) return;

            const paused = !!data.paused;
            const srv = data.server_name ? ` → ${data.server_name}` : '';
            const icon = paused ? '<i class="fas fa-pause-circle nl-icon-paused"></i>'
                                : '<i class="fas fa-spinner fa-spin nl-icon-spin-mr"></i>';
            const label = paused ? 'Transfer wstrzymany' : 'Transfer';
            title.innerHTML = `${icon} ${label}${srv} — ${Math.round(data.percent || 0)}%`;
            barWrap.style.display = '';
            fill.style.width = (data.percent || 0) + '%';
            fill.style.background = paused ? '#eab308' : '';
            detail.textContent = `${data.sent_fmt || '?'} / ${data.total_fmt || '?'} — ${data.current_file || ''}`;
            if (actionsWrap) actionsWrap.style.display = 'flex';
            if (pauseBtn) {
                pauseBtn.innerHTML = paused ? `<i class="fas fa-play"></i> ${t('Wznów')}` : '<i class="fas fa-pause"></i> Wstrzymaj';
                pauseBtn.title = paused ? t('Wznów') : 'Wstrzymaj';
            }
            if (startBtn) startBtn.disabled = true;
            statusCard.classList.add('nl-transfer-active');
        };

        window._nlOnComplete = (data) => {
            if (data.operation !== 'transfer') return;
            const title = content.querySelector('#nl-tf-title');
            const fill = content.querySelector('#nl-tf-fill');
            const detail = content.querySelector('#nl-tf-detail');
            const actionsWrap = content.querySelector('#nl-tf-actions');
            const statusCard = content.querySelector('#nl-tf-status');
            const startBtn = content.querySelector('#nl-tf-start');
            if (!title) return;
            title.innerHTML = `<i class="fas fa-check-circle nl-icon-success-mr"></i> ${_nlEsc(data.message || t('Zakończono'))}`;
            fill.style.width = '100%';
            fill.style.background = '';
            detail.textContent = '';
            if (actionsWrap) actionsWrap.style.display = 'none';
            if (startBtn) startBtn.disabled = false;
            statusCard.classList.remove('nl-transfer-active');
        };

        window._nlOnError = (data) => {
            if (data.operation !== 'transfer') return;
            const title = content.querySelector('#nl-tf-title');
            const fill = content.querySelector('#nl-tf-fill');
            const actionsWrap = content.querySelector('#nl-tf-actions');
            const statusCard = content.querySelector('#nl-tf-status');
            const startBtn = content.querySelector('#nl-tf-start');
            if (!title) return;
            title.innerHTML = `<i class="fas fa-times-circle nl-icon-error-mr"></i> ${_nlEsc(data.message || t('Błąd'))}`;
            fill.style.width = '0%';
            fill.style.background = '';
            if (actionsWrap) actionsWrap.style.display = 'none';
            if (startBtn) startBtn.disabled = false;
            statusCard.classList.remove('nl-transfer-active');
        };

        window._nlOnPaused = (data) => {
            const title = content.querySelector('#nl-tf-title');
            const fill = content.querySelector('#nl-tf-fill');
            const pauseBtn = content.querySelector('#nl-tf-pause');
            if (!title) return;
            const paused = !!data.paused;
            if (paused) {
                title.innerHTML = title.innerHTML.replace(/fa-spinner fa-spin/g, 'fa-pause-circle').replace('Transfer ', 'Transfer wstrzymany ');
                title.querySelector('i')?.setAttribute('style', 'margin-right:6px;color:#eab308;');
                if (fill) fill.style.background = '#eab308';
            } else {
                title.innerHTML = title.innerHTML.replace(/fa-pause-circle/g, 'fa-spinner fa-spin').replace('Transfer wstrzymany', 'Transfer');
                title.querySelector('i')?.setAttribute('style', 'margin-right:6px;');
                if (fill) fill.style.background = '';
            }
            if (pauseBtn) {
                pauseBtn.innerHTML = paused ? `<i class="fas fa-play"></i> ${t('Wznów')}` : '<i class="fas fa-pause"></i> Wstrzymaj';
                pauseBtn.title = paused ? t('Wznów') : 'Wstrzymaj';
            }
        };

        NAS.socket.on('fileop_transfer_detail', window._nlOnDetail);
        NAS.socket.on('fileop_complete', window._nlOnComplete);
        NAS.socket.on('fileop_error', window._nlOnError);
        NAS.socket.on('fileop_paused', window._nlOnPaused);
        window._nlSocketBound = true;
    }

    // ═══════════════════════════════════════════════════════════════
    //  SNAPSHOTS
    // ═══════════════════════════════════════════════════════════════

    async function renderSnapshots() {
        content.innerHTML = '<div class="nl-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        await Promise.all([loadServers(), loadSnapshots(), loadReceivedSnaps()]);

        const localSnaps = snapshots.filter(s => !s.received);
        const rcvdSnaps = snapshots.filter(s => s.received);

        content.innerHTML = `
            <div class="nl-toolbar">
                <h2><i class="fas fa-camera"></i> Snapshoty</h2>
                <button class="nl-btn" id="nl-snap-remote"><i class="fas fa-cloud-download-alt"></i> Pobierz ze zdalnego NAS</button>
            </div>

            ${rcvdSnaps.length > 0 ? `
                <h3 class="nl-section-title-sm nl-heading-received">
                    <i class="fas fa-inbox"></i> Otrzymane (${rcvdSnaps.length})
                </h3>
                <div class="nl-snap-list nl-snap-list-mb">
                    ${rcvdSnaps.map(s => `
                        <div class="nl-snap-card received" data-path="${_nlEsc(s.path || s.dir || '')}">
                            <div class="nl-snap-icon nl-snap-icon-received"><i class="fas fa-inbox"></i></div>
                            <div class="nl-snap-info">
                                <div class="nl-snap-name">${_nlEsc(s.label || s.name || s.id || 'Snapshot')}</div>
                                <div class="nl-snap-meta">${_nlEsc(s.source_name || s.hostname || '?')} — ${_nlFmtDate(s.date || s.created)}</div>
                            </div>
                            <span class="nl-snap-badge received">Otrzymany</span>
                            <button class="nl-btn sm primary nl-snap-adopt" data-path="${_nlEsc(s.path || s.dir || '')}"><i class="fas fa-download"></i> Adoptuj</button>
                            <button class="nl-btn sm nl-snap-restore" data-path="${_nlEsc(s.path || s.dir || '')}"><i class="fas fa-undo"></i> ${t('Przywróć')}</button>
                        </div>
                    `).join('')}
                </div>
            ` : ''}

            <h3 class="nl-section-title-sm">
                <i class="fas fa-camera nl-icon-accent"></i> Lokalne (${localSnaps.length})
            </h3>
            <div class="nl-snap-list" id="nl-snap-local">
                ${localSnaps.length === 0 ? `<div class="nl-empty"><i class="fas fa-camera"></i><p>${t('Brak lokalnych snapshotów.')}<br>${t('Utwórz snapshot w aplikacji')} <strong>${t('Kopia zapasowa')}</strong>${t('.')}</p></div>` : ''}
                ${localSnaps.map(s => `
                    <div class="nl-snap-card" data-id="${_nlEsc(s.id || '')}">
                        <div class="nl-snap-icon nl-snap-icon-local"><i class="fas fa-camera"></i></div>
                        <div class="nl-snap-info">
                            <div class="nl-snap-name">${_nlEsc(s.label || s.name || s.id || 'Snapshot')}</div>
                            <div class="nl-snap-meta">${_nlFmtDate(s.date || s.created)} — ${_nlEsc(s.hostname || 'local')}</div>
                        </div>
                        <span class="nl-snap-badge local">Lokalny</span>
                        ${servers.length > 0 ? `<button class="nl-btn sm nl-snap-push" data-id="${_nlEsc(s.id || '')}"><i class="fas fa-upload"></i> ${t('Wyślij do NAS')}</button>` : ''}
                    </div>
                `).join('')}
            </div>

            <div id="nl-snap-remote-area" class="nl-section-mt-sm"></div>
        `;

        // Push snapshot
        content.querySelectorAll('.nl-snap-push').forEach(btn => {
            btn.addEventListener('click', () => _nlPushSnapshot(btn.dataset.id));
        });

        // Adopt received snapshot
        content.querySelectorAll('.nl-snap-adopt').forEach(btn => {
            btn.addEventListener('click', () => _nlAdoptSnapshot(btn.dataset.path));
        });

        // Restore received snapshot
        content.querySelectorAll('.nl-snap-restore').forEach(btn => {
            btn.addEventListener('click', () => _nlRestoreReceivedSnapshot(btn.dataset.path));
        });

        // Remote snapshots
        content.querySelector('#nl-snap-remote').addEventListener('click', () => _nlLoadRemoteSnaps());
    }

    async function _nlPushSnapshot(snapId) {
        if (servers.length === 0) { toast(t('Brak skonfigurowanych serwerów'), 'warning'); return; }

        let serverOptions = servers.map(s => `<option value="${s.id}">${_nlEsc(s.name || s.host)}</option>`).join('');
        const result = await new Promise(resolve => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal nl-modal-sm">
                    <div class="modal-header"><i class="fas fa-upload nl-icon-mr"></i>${t('Wyślij snapshot')}</div>
                    <div class="modal-body">
                        <label class="modal-label">Serwer docelowy:</label>
                        <select class="modal-input" id="nl-push-server">${serverOptions}</select>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" id="nl-push-cancel">${t('Anuluj')}</button>
                        <button class="btn btn-primary" id="nl-push-go"><i class="fas fa-paper-plane"></i> ${t('Wyślij')}</button>
                    </div>
                </div>`;
            document.body.appendChild(overlay);
            overlay.querySelector('#nl-push-cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
            overlay.querySelector('#nl-push-go').addEventListener('click', () => {
                const val = overlay.querySelector('#nl-push-server').value;
                overlay.remove();
                resolve(val);
            });
            overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });
        });

        if (!result) return;
        try {
            const r = await api(`/backup/snapshots/${snapId}/transfer`, { method: 'POST', body: { server_id: result } });
            toast(r.message || t('Transfer rozpoczęty'), 'info');
        } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
    }

    async function _nlAdoptSnapshot(path) {
        try {
            const r = await api('/backup/snapshots/received/adopt', { method: 'POST', body: { path } });
            toast(r.message || 'Snapshot adoptowany', 'success');
            renderSnapshots();
        } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
    }

    async function _nlRestoreReceivedSnapshot(path) {
        const ok = await confirmDialog(t('Przywrócić konfigurację z otrzymanego snapshotu? Obecne ustawienia zostaną zastąpione.'));
        if (!ok) return;
        try {
            const r = await api('/backup/snapshots/received/restore', { method: 'POST', body: { path } });
            toast(r.message || t('Przywrócono'), 'success');
        } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
    }

    async function _nlLoadRemoteSnaps() {
        if (servers.length === 0) { toast(t('Brak serwerów'), 'warning'); return; }
        const area = content.querySelector('#nl-snap-remote-area');

        // Ask which server
        const serverOptions = servers.map(s => `<option value="${s.id}">${_nlEsc(s.name || s.host)}</option>`).join('');
        const server_id = await new Promise(resolve => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal nl-modal-sm">
                    <div class="modal-header"><i class="fas fa-cloud-download-alt nl-icon-mr"></i>Pobierz snapshoty zdalnie</div>
                    <div class="modal-body">
                        <label class="modal-label">${t('Serwer źródłowy:')}</label>
                        <select class="modal-input" id="nl-remote-srv">${serverOptions}</select>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" id="nl-remote-cancel">Anuluj</button>
                        <button class="btn btn-primary" id="nl-remote-go"><i class="fas fa-search"></i> Szukaj</button>
                    </div>
                </div>`;
            document.body.appendChild(overlay);
            overlay.querySelector('#nl-remote-cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
            overlay.querySelector('#nl-remote-go').addEventListener('click', () => {
                const v = overlay.querySelector('#nl-remote-srv').value;
                overlay.remove();
                resolve(v);
            });
            overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });
        });

        if (!server_id) return;

        area.innerHTML = `<div class="nl-status-msg"><i class="fas fa-spinner fa-spin"></i> ${t('Szukam snapshotów na zdalnym NAS…')}</div>`;
        try {
            const r = await api('/backup/snapshots/remote', { method: 'POST', body: { server_id } });
            remoteSnaps = r.snapshots || [];
            if (remoteSnaps.length === 0) {
                area.innerHTML = `<div class="nl-status-msg"><i class="fas fa-info-circle"></i> ${t('Brak snapshotów na zdalnym serwerze.')}</div>`;
                return;
            }
            area.innerHTML = `
                <h3 class="nl-section-title-sm nl-heading-remote">
                    <i class="fas fa-cloud"></i> Zdalne snapshoty (${remoteSnaps.length})
                </h3>
                <div class="nl-snap-list">
                    ${remoteSnaps.map(s => `
                        <div class="nl-snap-card remote">
                            <div class="nl-snap-icon nl-snap-icon-remote"><i class="fas fa-cloud"></i></div>
                            <div class="nl-snap-info">
                                <div class="nl-snap-name">${_nlEsc(s.label || s.name || s.id || 'Snapshot')}</div>
                                <div class="nl-snap-meta">${_nlFmtDate(s.date || s.created)} — ${_nlEsc(s.hostname || '?')}</div>
                            </div>
                            <span class="nl-snap-badge remote">Zdalny</span>
                            <button class="nl-btn sm primary nl-snap-pull" data-id="${_nlEsc(s.id || s.name || '')}" data-server="${_nlEsc(server_id)}"><i class="fas fa-download"></i> Pobierz</button>
                        </div>
                    `).join('')}
                </div>`;

            area.querySelectorAll('.nl-snap-pull').forEach(btn => {
                btn.addEventListener('click', async () => {
                    btn.disabled = true;
                    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                    try {
                        const r = await api('/backup/snapshots/pull', { method: 'POST', body: { server_id: btn.dataset.server, snapshot_id: btn.dataset.id } });
                        toast(r.message || t('Pobieranie rozpoczęte'), 'info');
                    } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); btn.disabled = false; btn.innerHTML = '<i class="fas fa-download"></i> Pobierz'; }
                });
            });
        } catch (e) {
            area.innerHTML = `<div class="nl-status-msg nl-error"><i class="fas fa-exclamation-triangle"></i> ${t('Błąd:')} ${_nlEsc(e.message || e)}</div>`;
        }
    }

    // ─── Helpers ─────────────────────────────────────────────────

    function _nlFmtDate(d) {
        if (!d) return '—';
        try {
            const dt = new Date(typeof d === 'number' ? d * 1000 : d);
            return dt.toLocaleString(getLocale(), { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
        } catch { return String(d); }
    }

    // ─── Navigable File/Folder Picker ────────────────────────────

    function _nlFilePicker(pathInput) {
        let browseDir = '/';
        const existing = pathInput.value.trim();
        if (existing) {
            const first = existing.split(',')[0].trim();
            browseDir = first.replace(/\/[^/]*$/, '') || '/';
        }
        let selected = new Set();

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal nl-modal-picker">
                <div class="modal-header nl-shrink-0">
                    <i class="fas fa-folder-open nl-icon-mr"></i>Wybierz pliki/foldery
                </div>
                <div class="nl-picker-bar">
                    <button class="nl-btn sm" id="nlp-up" title="${t('W górę')}"><i class="fas fa-arrow-up"></i></button>
                    <input id="nlp-path" class="nl-picker-input">
                    <button class="nl-btn sm" id="nlp-go" title="${t('Przejdź')}"><i class="fas fa-arrow-right"></i></button>
                </div>
                <div id="nlp-list" class="nl-picker-list">
                    <div class="nl-loading-sm"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>
                </div>
                <div id="nlp-selected" class="nl-picker-selected">
                    Nic nie zaznaczono
                </div>
                <div class="modal-footer nl-shrink-0">
                    <button class="btn" id="nlp-cancel">Anuluj</button>
                    <button class="btn" id="nlp-seldir" title="${t('Dodaj bieżący folder')}"><i class="fas fa-folder-plus nl-icon-mr-xs"></i>Dodaj ten folder</button>
                    <button class="btn btn-primary" id="nlp-add"><i class="fas fa-plus"></i> Dodaj zaznaczone (0)</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        // Static events — bound once
        overlay.querySelector('#nlp-cancel').addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
        overlay.querySelector('#nlp-up').addEventListener('click', () => {
            const parent = browseDir.replace(/\/[^/]+\/?$/, '') || '/';
            renderPicker(parent);
        });
        overlay.querySelector('#nlp-go').addEventListener('click', () => {
            const v = overlay.querySelector('#nlp-path').value.trim();
            if (v) renderPicker(v);
        });
        overlay.querySelector('#nlp-path').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const v = overlay.querySelector('#nlp-path').value.trim();
                if (v) renderPicker(v);
            }
        });
        overlay.querySelector('#nlp-seldir').addEventListener('click', () => {
            if (browseDir && browseDir !== '/') {
                pathInput.value = browseDir;
                overlay.remove();
            } else {
                toast(t('Nie można dodać katalogu głównego'), 'warning');
            }
        });
        overlay.querySelector('#nlp-add').addEventListener('click', () => {
            _applySelected();
            overlay.remove();
        });

        function updateSelectedCount() {
            const selEl = overlay.querySelector('#nlp-selected');
            const addBtn = overlay.querySelector('#nlp-add');
            if (selEl) {
                selEl.textContent = selected.size === 0 ? 'Nic nie zaznaczono' : [...selected].join(', ');
            }
            if (addBtn) addBtn.innerHTML = `<i class="fas fa-plus"></i> Dodaj zaznaczone (${selected.size})`;
        }

        async function renderPicker(dir) {
            browseDir = dir;
            // Update path bar and up-button — no modal rebuild
            const pathInput2 = overlay.querySelector('#nlp-path');
            const upBtn = overlay.querySelector('#nlp-up');
            pathInput2.value = dir;
            upBtn.disabled = (dir === '/');

            const listEl = overlay.querySelector('#nlp-list');
            listEl.innerHTML = `<div class="nl-loading-sm"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;

            try {
                const r = await api('/files/list?path=' + encodeURIComponent(dir));
                const items = r.items || [];
                if (items.length === 0) {
                    listEl.innerHTML = '<div class="nl-loading-sm"><i class="fas fa-folder-open"></i> Pusty katalog</div>';
                } else {
                    items.sort((a,b) => (b.is_dir?1:0) - (a.is_dir?1:0) || a.name.localeCompare(b.name));
                    listEl.innerHTML = items.map(it => {
                        const fullPath = it.path || ((dir === '/' ? '' : dir) + '/' + it.name);
                        const checked = selected.has(fullPath) ? 'checked' : '';
                        return `
                            <div class="nlp-item" data-path="${_nlEsc(fullPath)}" data-isdir="${it.is_dir ? '1' : '0'}">
                                <input type="checkbox" class="nlp-cb" value="${_nlEsc(fullPath)}" ${checked}>
                                <i class="fas ${it.is_dir ? 'fa-folder' : 'fa-file'} nl-file-icon" style="color:${it.is_dir ? '#eab308' : 'var(--text-muted)'}"></i>
                                <span class="nlp-name">${_nlEsc(it.name)}</span>
                                ${it.is_dir ? `<i class="fas fa-chevron-right nlp-enter" title="${t('Wejdź do folderu')}"></i>` : ''}
                                ${!it.is_dir && it.size ? '<span class="nlp-file-size">' + _nlFmtSize(it.size) + '</span>' : ''}
                            </div>`;
                    }).join('');

                    // Hover effect
                    listEl.querySelectorAll('.nlp-item').forEach(row => {
                        row.addEventListener('mouseenter', () => row.style.background = 'var(--bg-hover)');
                        row.addEventListener('mouseleave', () => row.style.background = '');
                    });

                    // Checkbox toggle
                    listEl.querySelectorAll('.nlp-cb').forEach(cb => {
                        cb.addEventListener('change', () => {
                            if (cb.checked) selected.add(cb.value);
                            else selected.delete(cb.value);
                            updateSelectedCount();
                        });
                    });

                    // Click on row → navigate into dir (single click) or toggle file checkbox
                    listEl.querySelectorAll('.nlp-item').forEach(row => {
                        row.addEventListener('click', (e) => {
                            if (e.target.classList.contains('nlp-cb')) return;
                            const isDir = row.dataset.isdir === '1';
                            if (isDir) {
                                renderPicker(row.dataset.path);
                                return;
                            }
                            const cb = row.querySelector('.nlp-cb');
                            cb.checked = !cb.checked;
                            if (cb.checked) selected.add(cb.value);
                            else selected.delete(cb.value);
                            updateSelectedCount();
                        });
                    });
                }
            } catch (e) {
                listEl.innerHTML = `<div class="nl-loading-xs nl-error"><i class="fas fa-exclamation-triangle"></i> ${_nlEsc(e.message || e)}</div>`;
            }
        }

        function _applySelected() {
            if (selected.size === 0) return;
            const all = [...selected];
            const deduped = all.filter(p => !all.some(q => q !== p && p.startsWith(q + '/')));
            pathInput.value = deduped.join(', ');
        }

        renderPicker(browseDir);
    }

    // ─── Init ────────────────────────────────────────────────────
    setActiveTab();
    renderTab();
}

/* ═══ Shared helpers ═══ */

function _nlEsc(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
