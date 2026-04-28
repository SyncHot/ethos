AppRegistry['docker-manager'] = function (appDef) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('docker-manager', level, msg, details) : console.log('[docker-manager]', msg, details || '');

    createWindow('docker-manager', {
        title: t('Docker'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1100,
        height: 700,
        onRender: (body) => renderDockerManager(body),
    });
};

function renderDockerManager(body) {
    const isAdmin = NAS.user?.role === 'admin';
    const S = {
        tab: 'containers',
        containers: [],
        projects: [],
        images: [],
        systemInfo: null,
        filter: '',
        selectedContainer: null,
        detailTab: 'logs',
        _intervals: [],
    };

    // Helper: track intervals and clear stale ones
    function addInterval(id) { S._intervals.push(id); }
    function clearAllIntervals() { S._intervals.forEach(clearInterval); S._intervals.length = 0; }

    // Helper: escape HTML to prevent XSS from Docker names
    function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

    // Helper: check if env var name looks sensitive
    function isSensitiveEnv(name) {
        return /password|secret|key|token|api_key|apikey|private|credential/i.test(name);
    }

    // Helper: basic YAML syntax validation (checks structure, not full parse)
    function validateYaml(text) {
        if (!text || !text.trim()) return { valid: false, error: t('Pusta treść') };
        const lines = text.split('\n');
        let hasServices = false;
        let inBlock = false;
        const warnings = [];
        for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed.startsWith('#') || !trimmed) continue;
            // Check for tabs (YAML forbids tabs for indentation)
            if (/^\t/.test(line)) return { valid: false, error: t('YAML nie może używać tabulatorów — użyj spacji') };
            // Detect services key
            if (/^services\s*:/.test(trimmed)) hasServices = true;
            // Detect dangerous directives
            if (/^\s*privileged\s*:\s*true/i.test(line)) warnings.push(t('Tryb privileged — pełny dostęp do hosta'));
            if (/^\s*network_mode\s*:\s*["']?host["']?/i.test(line)) warnings.push(t('network_mode: host — kontener widzi sieć hosta'));
            if (/^\s*pid\s*:\s*["']?host["']?/i.test(line)) warnings.push(t('pid: host — kontener widzi procesy hosta'));
            if (/^\s*cap_add\s*:/i.test(line)) inBlock = true;
            if (inBlock && /SYS_ADMIN|NET_ADMIN|ALL/i.test(trimmed)) warnings.push(t('Niebezpieczna capability: ') + trimmed.replace(/^-\s*/, ''));
            if (inBlock && /^\S/.test(line)) inBlock = false;
        }
        if (!hasServices) return { valid: false, error: t('Brak sekcji "services" — wymagana w docker-compose') };
        return { valid: true, warnings };
    }

    body.innerHTML = `
        <div class="dkr">
            <div class="dkr-sidebar">
                <div class="dkr-nav-item active" data-tab="containers"><i class="fas fa-box"></i> ${t('Kontenery')}</div>
                <div class="dkr-nav-item" data-tab="projects"><i class="fas fa-layer-group"></i> ${t('Projekty')}</div>
                <div class="dkr-nav-item" data-tab="images"><i class="fas fa-clone"></i> ${t('Obrazy')}</div>
                <div class="dkr-nav-item" data-tab="backups"><i class="fas fa-shield-alt"></i> ${t('Backupy')}</div>
                <div class="dkr-nav-item" data-tab="system"><i class="fas fa-server"></i> ${t('System')}</div>
            </div>
            <div class="dkr-main" id="dkr-main"></div>
        </div>
    `;

    // Tab navigation
    body.querySelectorAll('.dkr-nav-item').forEach(nav => {
        nav.addEventListener('click', () => {
            body.querySelectorAll('.dkr-nav-item').forEach(n => n.classList.remove('active'));
            nav.classList.add('active');
            S.tab = nav.dataset.tab;
            S.selectedContainer = null;
            S._inspectCache = null;
            clearAllIntervals();
            renderTab();
        });
    });

    const main = body.querySelector('#dkr-main');

    function renderTab() {
        switch (S.tab) {
            case 'containers': renderContainersTab(); break;
            case 'projects': renderProjectsTab(); break;
            case 'images': renderImagesTab(); break;
            case 'backups': renderBackupsTab(); break;
            case 'system': renderSystemTab(); break;
        }
    }

    // ─── CONTAINERS TAB ───
    async function loadContainers() {
        try {
            const res = await api('/docker/containers');
            if (!Array.isArray(res)) {
                if (res?.error) toast(res.error, 'error');
                S.containers = [];
                return;
            }
            // Pre-compute search string for performance
            S.containers = res.map(c => {
                c._search = (c.name + ' ' + c.image + ' ' + (c.project||'')).toLowerCase();
                return c;
            });
        } catch { toast(t('Błąd pobierania kontenerów'), 'error'); }
    }

    function renderContainersTab() {
        if (S.selectedContainer) { renderContainerDetail(); return; }
        main.innerHTML = `
            ${!isAdmin ? '<div class="dkr-readonly-notice"><i class="fas fa-info-circle"></i> ' + t('Tryb tylko do odczytu — wymagane uprawnienia administratora') + '</div>' : ''}
            <div class="dkr-toolbar">
                <span class="dkr-toolbar-title"><i class="fas fa-box"></i> ${t('Kontenery')} <span class="dkr-badge" id="dkr-cnt-count">0</span></span>
                <input class="dkr-filter" id="dkr-filter" placeholder="${t('Filtruj...')}" value="${esc(S.filter)}">
                <button class="dkr-btn" id="dkr-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="dkr-table-wrap">
                <table class="dkr-table">
                    <thead><tr>
                        <th class="app-col-icon"></th>
                        <th>${t('Nazwa')}</th>
                        <th>${t('Obraz')}</th>
                        <th>${t('Projekt')}</th>
                        <th>${t('Status')}</th>
                        ${isAdmin ? `<th class="app-col-actions">${t('Akcje')}</th>` : ''}
                    </tr></thead>
                    <tbody id="dkr-ct-body"></tbody>
                </table>
            </div>
        `;

        // Virtual scroll initialization
        S.virtual = { rowH: 45, padTop: 0, padBot: 0 };
        S.filtered = []; // Initialize to empty array to prevent TypeError in renderVirtualChunk
        const wrap = main.querySelector('.dkr-table-wrap');
        let ticking = false;
        wrap.addEventListener('scroll', () => {
            if (!ticking) {
                window.requestAnimationFrame(() => {
                    renderVirtualChunk();
                    ticking = false;
                });
                ticking = true;
            }
        });

        let filterDebounce;
        main.querySelector('#dkr-filter').addEventListener('input', e => {
            S.filter = e.target.value.toLowerCase();
            clearTimeout(filterDebounce);
            filterDebounce = setTimeout(fillContainersTable, 200);
        });
        main.querySelector('#dkr-refresh').addEventListener('click', async () => {
            await loadContainers();
            fillContainersTable();
        });
        loadContainers().then(fillContainersTable);
    }

    function fillContainersTable() {
        const tbody = main.querySelector('#dkr-ct-body');
        const badge = main.querySelector('#dkr-cnt-count');
        const wrap = main.querySelector('.dkr-table-wrap');
        if (!tbody) return;

        const f = S.filter;
        // Optimized filter using pre-computed _search
        S.filtered = S.containers.filter(c => !f || c._search.includes(f));
        badge.textContent = S.filtered.length;

        // Reset scroll on filter change if needed, but only if triggered by filter input
        if (wrap) wrap.scrollTop = 0;
        renderVirtualChunk();
    }

    function renderVirtualChunk() {
        const tbody = main.querySelector('#dkr-ct-body');
        const wrap = main.querySelector('.dkr-table-wrap');
        if (!tbody || !wrap) return;
        if (!S.filtered) S.filtered = []; // Safety check

        const rowH = 45; // Estimated row height
        const total = S.filtered.length;
        const viewH = wrap.clientHeight || 500;
        const scrollT = wrap.scrollTop;

        // Calculate visible range
        let start = Math.floor(scrollT / rowH);
        let end = Math.ceil((scrollT + viewH) / rowH);

        // Add buffer
        start = Math.max(0, start - 5);
        end = Math.min(total, end + 5);

        const padTop = start * rowH;
        const padBot = Math.max(0, (total - end) * rowH);

        const visible = S.filtered.slice(start, end);

        tbody.innerHTML = `
            <tr style="height:${padTop}px; border:0;"><td colspan="100" style="padding:0; border:0;"></td></tr>
            ${visible.map(c => {
                const st = c.state || 'exited';
                const isRun = st === 'running';
                const isPaused = st === 'paused';
                return `<tr class="dkr-row" data-id="${esc(c.id)}" data-name="${esc(c.name)}">
                    <td><span class="dkr-dot ${st}"></span></td>
                    <td><a class="dkr-link" data-cid="${esc(c.id)}">${esc(c.name)}</a></td>
                    <td class="dkr-muted dkr-ellipsis" title="${esc(c.image)}">${esc(c.image)}</td>
                    <td>${c.project ? `<span class="dkr-project-badge">${esc(c.project)}</span>` : '<span class="dkr-muted">—</span>'}</td>
                    <td class="dkr-status-text">${esc(c.status)}</td>
                    ${isAdmin ? `<td class="dkr-actions">
                        ${!isRun ? btn('start','fa-play',t('Uruchom'),'success') : ''}
                        ${isRun ? btn('stop','fa-stop',t('Zatrzymaj'),'warning') : ''}
                        ${isRun ? btn('restart','fa-redo',t('Restartuj'),'info') : ''}
                        ${isRun && !isPaused ? btn('pause','fa-pause',t('Wstrzymaj'),'') : ''}
                        ${isPaused ? btn('unpause','fa-play',t('Wznów'),'') : ''}
                        ${btn('remove','fa-trash',t('Usuń'),'danger')}
                    </td>` : ''}
                </tr>`;
            }).join('')}
            <tr style="height:${padBot}px; border:0;"><td colspan="100" style="padding:0; border:0;"></td></tr>
        `;

        // Re-attach listeners
        tbody.querySelectorAll('.dkr-act-btn').forEach(b => {
            b.addEventListener('click', async (e) => {
                e.stopPropagation();
                const row = b.closest('tr');
                const id = row.dataset.id;
                const name = row.dataset.name;
                const action = b.dataset.action;
                if (action === 'remove' && !await confirmDialog(t('Usunąć kontener') + ` ${name}?`)) return;
                try {
                    await api(`/docker/containers/${id}/action`, { method: 'POST', body: { action } });
                    toast(`${name}: ${action}`, 'success');
                    setTimeout(async () => { await loadContainers(); fillContainersTable(); }, 1000);
                } catch (err) {
                    toast(`${t('Błąd:')} ${action} ${name}`, 'error');
                }
            });
        });

        tbody.querySelectorAll('.dkr-link').forEach(a => {
            a.addEventListener('click', () => {
                S.selectedContainer = a.dataset.cid;
                renderContainerDetail();
            });
        });
    }

    function btn(action, icon, title, color) {
        return `<button class="dkr-act-btn ${color}" data-action="${action}" title="${title}"><i class="fas ${icon}"></i></button>`;
    }

    // ─── CONTAINER DETAIL ───
    async function renderContainerDetail() {
        clearAllIntervals();
        const cid = S.selectedContainer;
        main.innerHTML = `
            <div class="dkr-toolbar">
                <button class="dkr-btn" id="dkr-back"><i class="fas fa-arrow-left"></i> ${t('Powrót')}</button>
                <span class="dkr-toolbar-title" id="dkr-detail-title">${t('Ładowanie...')}</span>
                <div class="dkr-detail-tabs">
                    <button class="dkr-tab-btn active" data-dt="logs">${t('Logi')}</button>
                    <button class="dkr-tab-btn" data-dt="inspect">${t('Szczegóły')}</button>
                    <button class="dkr-tab-btn" data-dt="stats">${t('Zasoby')}</button>
                </div>
            </div>
            <div class="dkr-detail-body" id="dkr-detail-body"><div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div></div>
        `;
        main.querySelector('#dkr-back').addEventListener('click', () => {
            S.selectedContainer = null;
            S._inspectCache = null;
            clearAllIntervals();
            renderContainersTab();
        });
        main.querySelectorAll('.dkr-tab-btn').forEach(t => {
            t.addEventListener('click', () => {
                main.querySelectorAll('.dkr-tab-btn').forEach(x => x.classList.remove('active'));
                t.classList.add('active');
                S.detailTab = t.dataset.dt;
                clearAllIntervals(); // stop previous sub-tab timers
                renderDetailContent(cid);
            });
        });
        // Load inspect for title
        try {
            const info = await api(`/docker/containers/${cid}/inspect`);
            main.querySelector('#dkr-detail-title').innerHTML = `<span class="dkr-dot ${info.state.status}"></span> ${esc(info.name)}`;
            S._inspectCache = info;
        } catch { }
        S.detailTab = 'logs';
        renderDetailContent(cid);
    }

    async function renderDetailContent(cid) {
        const db = main.querySelector('#dkr-detail-body');
        if (!db) return;
        switch (S.detailTab) {
            case 'logs': await renderLogsPanel(db, cid); break;
            case 'inspect': await renderInspectPanel(db, cid); break;
            case 'stats': await renderStatsPanel(db, cid); break;
        }
    }

    async function renderLogsPanel(db, cid) {
        db.innerHTML = `
            <div class="dkr-logs-toolbar">
                <select class="dkr-select" id="dkr-log-lines">
                    <option value="100">100 ${t('linii')}</option>
                    <option value="200" selected>200 ${t('linii')}</option>
                    <option value="500">500 ${t('linii')}</option>
                    <option value="1000">1000 ${t('linii')}</option>
                    <option value="5000">5000 ${t('linii')}</option>
                </select>
                <input class="dkr-filter" id="dkr-log-search" placeholder="${t('Szukaj w logach...')}">
                <button class="dkr-btn" id="dkr-log-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
                <label class="dkr-check"><input type="checkbox" id="dkr-log-follow" checked> ${t('Auto-scroll')}</label>
            </div>
            <pre class="dkr-logs" id="dkr-logs"></pre>
        `;
        async function loadLogs() {
            const lines = db.querySelector('#dkr-log-lines').value;
            const search = db.querySelector('#dkr-log-search').value;
            try {
                const r = await api(`/docker/containers/${cid}/logs?lines=${lines}&search=${encodeURIComponent(search)}`);
                const pre = db.querySelector('#dkr-logs');
                if (pre) {
                    pre.textContent = (r.logs || []).join('\n');
                    if (db.querySelector('#dkr-log-follow')?.checked) pre.scrollTop = pre.scrollHeight;
                }
            } catch { }
        }
        loadLogs();
        db.querySelector('#dkr-log-refresh').addEventListener('click', loadLogs);
        db.querySelector('#dkr-log-lines').addEventListener('change', loadLogs);
        let debounce;
        db.querySelector('#dkr-log-search').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(loadLogs, 400); });
        // Auto-refresh logs every 5s
        const iv = setInterval(() => {
            if (!WM.windows.has('docker-manager') || S.detailTab !== 'logs') { clearInterval(iv); return; }
            loadLogs();
        }, 5000);
        addInterval(iv);
    }

    async function renderInspectPanel(db, cid) {
        db.innerHTML = '<div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        let info = S._inspectCache;
        if (!info || info.id !== cid.substring(0,12)) {
            try { info = await api(`/docker/containers/${cid}/inspect`); S._inspectCache = info; } catch { db.innerHTML = `<div class="dkr-empty">${t('Błąd')}</div>`; return; }
        }
        const s = info.state || {};
        db.innerHTML = `
            <div class="dkr-inspect">
                <div class="dkr-inspect-section">
                    <h3>${t('Ogólne')}</h3>
                    <div class="dkr-kv"><span>ID</span><span>${info.id}</span></div>
                    <div class="dkr-kv"><span>${t('Obraz')}</span><span>${info.image}</span></div>
                    <div class="dkr-kv"><span>${t('Polecenie')}</span><span><code>${info.command || info.entrypoint || '—'}</code></span></div>
                    <div class="dkr-kv"><span>${t('Utworzony')}</span><span>${info.created ? new Date(info.created).toLocaleString('pl') : '—'}</span></div>
                    <div class="dkr-kv"><span>${t('Status')}</span><span><span class="dkr-dot ${s.status}"></span> ${s.status} (PID: ${s.pid})</span></div>
                    <div class="dkr-kv"><span>${t('Polityka restartu')}</span><span>${info.restart_policy?.Name || '—'}</span></div>
                    <div class="dkr-kv"><span>${t('Tryb sieci')}</span><span>${info.network_mode}</span></div>
                    <div class="dkr-kv"><span>${t('Uprzywilejowany')}</span><span>${info.privileged ? t('Tak') : t('Nie')}</span></div>
                </div>
                ${info.ports.length ? `<div class="dkr-inspect-section"><h3>${t('Porty')}</h3>${info.ports.map(p =>
                    `<div class="dkr-kv"><span>${p.host}</span><span>→ ${p.container}</span></div>`
                ).join('')}</div>` : ''}
                ${info.mounts.length ? `<div class="dkr-inspect-section"><h3>${t('Wolumeny')}</h3>${info.mounts.map(m =>
                    `<div class="dkr-kv"><span>${m.source}</span><span>→ ${m.destination} ${m.rw ? '' : '(RO)'}</span></div>`
                ).join('')}</div>` : ''}
                ${info.networks.length ? `<div class="dkr-inspect-section"><h3>${t('Sieci')}</h3>${info.networks.map(n =>
                    `<div class="dkr-kv"><span>${n.name}</span><span>${n.ip || '—'}</span></div>`
                ).join('')}</div>` : ''}
                <div class="dkr-inspect-section">
                    <h3>${t('Zmienne środowiskowe')} <small>(${info.env.length})</small></h3>
                    <div class="dkr-env-list">${info.env.map(e => {
                        const [k,...v] = e.split('=');
                        const val = v.join('=');
                        const sensitive = isSensitiveEnv(k);
                        return `<div class="dkr-kv"><span>${esc(k)}</span><span>${sensitive
                            ? `<span class="dkr-env-masked" data-val="${esc(val)}" title="${t('Kliknij aby odsłonić')}">••••••••</span>`
                            : esc(val)
                        }</span></div>`;
                    }).join('')}</div>
                </div>
            </div>
        `;
        // Click to reveal masked env vars
        db.querySelectorAll('.dkr-env-masked').forEach(el => {
            el.addEventListener('click', () => {
                if (el.dataset.revealed === 'true') {
                    el.textContent = '••••••••';
                    el.dataset.revealed = 'false';
                } else {
                    el.textContent = el.dataset.val;
                    el.dataset.revealed = 'true';
                }
            });
        });
    }

    async function renderStatsPanel(db, cid) {
        db.innerHTML = '<div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        async function loadStats() {
            try {
                const s = await api(`/docker/containers/${cid}/stats`);
                db.innerHTML = `
                    <div class="dkr-stats-grid">
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-microchip"></i></div><div class="dkr-stat-label">CPU</div><div class="dkr-stat-value">${s.cpu || '—'}</div></div>
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-memory"></i></div><div class="dkr-stat-label">${t('Pamięć')}</div><div class="dkr-stat-value">${s.mem || '—'}</div><div class="dkr-stat-sub">${s.mem_perc || ''}</div></div>
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-network-wired"></i></div><div class="dkr-stat-label">${t('Sieć I/O')}</div><div class="dkr-stat-value">${s.net || '—'}</div></div>
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-hdd"></i></div><div class="dkr-stat-label">Dysk I/O</div><div class="dkr-stat-value">${s.block || '—'}</div></div>
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-stream"></i></div><div class="dkr-stat-label">PID-y</div><div class="dkr-stat-value">${s.pids || '—'}</div></div>
                    </div>
                `;
            } catch { db.innerHTML = '<div class="dkr-empty">Brak danych</div>'; }
        }
        loadStats();
        const iv = setInterval(() => {
            if (!WM.windows.has('docker-manager') || S.detailTab !== 'stats') { clearInterval(iv); return; }
            loadStats();
        }, 3000);
        addInterval(iv);
    }

    // ─── PROJECTS TAB ───
    async function loadProjects() {
        try {
            const res = await api('/docker/projects');
            if (!Array.isArray(res)) {
                if (res?.error) toast(res.error, 'error');
                S.projects = [];
                return;
            }
            S.projects = res.map(p => {
                const srv = (p.containers||[]).map(c=>c.name + ' ' + (c.image||'')).join(' ');
                p._search = (p.name + ' ' + srv).toLowerCase();
                return p;
            });
        } catch { toast(t('Błąd pobierania projektów'), 'error'); }
    }

    function renderProjectsTab() {
        S.projLimit = 10;
        main.innerHTML = `
            <div class="dkr-toolbar">
                <span class="dkr-toolbar-title"><i class="fas fa-layer-group"></i> Projekty Docker Compose</span>
                <span class="dkr-compose-dir-label" id="dkr-compose-dir-label" title="${t('Katalog projektów')}"></span>
                <input class="dkr-filter" id="dkr-proj-filter" placeholder="Filtruj...">
                ${isAdmin ? `<button class="dkr-btn primary" id="dkr-proj-create"><i class="fas fa-plus"></i> Nowy projekt</button>` : ''}
                ${isAdmin ? `<button class="dkr-btn" id="dkr-proj-dir-btn" title="${t('Zmień katalog projektów')}"><i class="fas fa-folder-open"></i></button>` : ''}
                <button class="dkr-btn" id="dkr-proj-refresh"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="dkr-projects" id="dkr-projects"><div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div></div>
        `;
        const wrap = main.querySelector('#dkr-projects');

        // Load and display the current compose dir
        api('/docker/settings').then(s => {
            const lbl = main.querySelector('#dkr-compose-dir-label');
            if (lbl && s?.effective_compose_dir) lbl.textContent = s.effective_compose_dir;
        }).catch(() => {});

        // Infinite scroll
        let ticking = false;
        wrap.addEventListener('scroll', () => {
             if (!ticking) {
                 window.requestAnimationFrame(() => {
                     if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 100) {
                         if (S.projLimit < (S.filteredProjects||[]).length) {
                             S.projLimit += 10;
                             fillProjects();
                         }
                     }
                     ticking = false;
                 });
                 ticking = true;
             }
        });

        main.querySelector('#dkr-proj-refresh').addEventListener('click', async () => { await loadProjects(); fillProjects(); });
        if (isAdmin) {
            main.querySelector('#dkr-proj-create').addEventListener('click', () => openCreateProjectModal());
            main.querySelector('#dkr-proj-dir-btn').addEventListener('click', () => openComposeDirModal());
        }
        main.querySelector('#dkr-proj-filter').addEventListener('input', e => {
            S.projLimit = 10;
            if (wrap) wrap.scrollTop = 0;
            fillProjects();
        });
        loadProjects().then(() => fillProjects());

        async function openComposeDirModal() {
            const settings = await api('/docker/settings').catch(() => ({}));
            const currentDir = settings?.compose_dir || '';
            const defaultDir = settings?.default_compose_dir || '';
            const overlay = document.createElement('div');
            overlay.className = 'dkr-modal-overlay';
            overlay.innerHTML = `
                <div class="dkr-modal" style="max-width:520px">
                    <div class="dkr-modal-header">
                        <span><i class="fas fa-folder-open"></i> ${t('Katalog projektów Docker Compose')}</span>
                        <button class="dkr-modal-close" id="dkr-dir-close"><i class="fas fa-times"></i></button>
                    </div>
                    <div class="dkr-modal-body" style="padding:18px 20px">
                        <p class="dkr-muted" style="margin:0 0 12px">${t('Wskaż katalog, w którym przechowywane są projekty Docker Compose. Każdy podkatalog z plikiem docker-compose.yaml staje się projektem.')}</p>
                        <label class="app-form-label">${t('Ścieżka katalogu')}</label>
                        <input class="app-input" id="dkr-dir-input" type="text" value="${esc(currentDir)}" placeholder="${esc(defaultDir)}" style="width:100%;box-sizing:border-box">
                        <p class="dkr-muted" style="margin:8px 0 0;font-size:12px">${t('Domyślny')}: <code>${esc(defaultDir)}</code></p>
                        <p class="dkr-muted" style="margin:4px 0 0;font-size:12px">${t('Zostaw puste, aby użyć domyślnego katalogu.')}</p>
                    </div>
                    <div class="dkr-modal-footer">
                        <button class="dkr-btn" id="dkr-dir-cancel">${t('Anuluj')}</button>
                        <button class="dkr-btn" id="dkr-dir-reset" style="margin-right:auto">${t('Przywróć domyślny')}</button>
                        <button class="dkr-btn primary" id="dkr-dir-save"><i class="fas fa-save"></i> ${t('Zapisz')}</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            const close = () => overlay.remove();
            overlay.querySelector('#dkr-dir-close').addEventListener('click', close);
            overlay.querySelector('#dkr-dir-cancel').addEventListener('click', close);
            overlay.querySelector('#dkr-dir-reset').addEventListener('click', () => {
                overlay.querySelector('#dkr-dir-input').value = '';
            });
            overlay.querySelector('#dkr-dir-save').addEventListener('click', async () => {
                const val = overlay.querySelector('#dkr-dir-input').value.trim();
                try {
                    const r = await api('/docker/settings', { method: 'POST', body: { compose_dir: val } });
                    if (r?.error) { toast(r.error, 'error'); return; }
                    toast(t('Zapisano katalog projektów'), 'success');
                    const lbl = main.querySelector('#dkr-compose-dir-label');
                    if (lbl && r?.effective_compose_dir) lbl.textContent = r.effective_compose_dir;
                    close();
                    await loadProjects();
                    fillProjects();
                } catch { toast(t('Błąd zapisu ustawień'), 'error'); }
            });
        }

        function fillProjects() {
            const wrap = main.querySelector('#dkr-projects');
            if (!wrap) return;

            const pf = (main.querySelector('#dkr-proj-filter').value || '').toLowerCase();
            S.filteredProjects = S.projects.filter(p => !pf || p._search.includes(pf));

            if (!S.filteredProjects.length) { wrap.innerHTML = `<div class="dkr-empty">${t('Brak projektów')}</div>`; return; }

            const visible = S.filteredProjects.slice(0, S.projLimit);

            wrap.innerHTML = visible.map(p => {
                const statusCls = p.status === 'running' ? 'success' : p.status === 'partial' ? 'warning' : 'muted';
                const statusLabel = p.status === 'running' ? t('Działa') : p.status === 'partial' ? t('Częściowo') : 'Zatrzymany';
                const isProt = p.protected;
                const servicesData = btoa(JSON.stringify(p.containers.map(c=>({name:c.name, service:c.service||c.name}))));
                return `
                    <div class="dkr-project-card" data-project="${esc(p.name)}">
                        <div class="dkr-project-header">
                            <div class="dkr-project-info">
                                <span class="dkr-project-name"><i class="fas fa-layer-group"></i> ${esc(p.name)}${isProt ? ' <i class="fas fa-shield-alt app-shield-icon" title="Projekt chroniony"></i>' : ''}</span>
                                <span class="dkr-project-status ${statusCls}">${statusLabel}</span>
                                <span class="dkr-muted">${p.running}/${p.total} ${t('kontenerów')}</span>
                            </div>
                            <div class="dkr-project-actions">
                                ${p.status !== 'running' ? `<button class="dkr-act-btn success" data-paction="up" title="Uruchom"><i class="fas fa-play"></i></button>` : ''}
                                ${!isProt && p.status !== 'stopped' ? `<button class="dkr-act-btn warning" data-paction="stop" title="Zatrzymaj"><i class="fas fa-stop"></i></button>` : ''}
                                <button class="dkr-act-btn info" data-paction="restart" title="Restartuj"><i class="fas fa-redo"></i></button>
                                <button class="dkr-act-btn" data-paction="pull" title="Pobierz obrazy"><i class="fas fa-download"></i></button>
                                ${!isProt ? `<button class="dkr-act-btn danger" data-paction="down" title="Down"><i class="fas fa-power-off"></i></button>` : ''}
                                <button class="dkr-compose-btn" data-project="${esc(p.name)}" title="docker-compose.yaml"><i class="fas fa-file-code"></i></button>
                                <button class="dkr-logs-btn" data-project="${esc(p.name)}" data-services="${servicesData}" title="${t('Logi kontenerów')}"><i class="fas fa-rectangle-list"></i></button>
                                ${!isProt ? `<button class="dkr-act-btn danger dkr-delete-proj-btn" data-project="${esc(p.name)}" title="${t('Usuń projekt')}"><i class="fas fa-trash-alt"></i></button>` : ''}
                            </div>
                        </div>
                        ${p.containers.length ? `<div class="dkr-project-containers">${p.containers.map(c => {
                            const ports = (c.ports || '').split(',').map(p => p.trim()).filter(p => p && p.includes('->')).map(p => {
                                const m = p.match(/(\d+\.\d+\.\d+\.\d+:)?(\d+)->(\d+)\/\w+/);
                                return m ? { host: m[2], container: m[3], label: (m[1] || '') + m[2] + ':' + m[3] } : { host: null, label: p };
                            });
                            return `<div class="dkr-project-ct">
                                <span class="dkr-dot ${c.state}"></span>
                                <span class="dkr-ct-name">${esc(c.name)}</span>
                                ${ports.length ? `<span class="dkr-ct-ports">${ports.map(p => p.host ? `<a class="dkr-port-badge dkr-port-link" href="http://${location.hostname}:${p.host}" target="_blank" rel="noopener" title="${t('Otwórz')} :${p.host}">${esc(p.label)}</a>` : `<span class="dkr-port-badge">${esc(p.label)}</span>`).join('')}</span>` : ''}
                                <span class="dkr-muted">${esc(c.image)}</span>
                                <span class="dkr-status-text">${esc(c.status)}</span>
                            </div>`;
                        }).join('')}</div>` : ''}
                    </div>
                `;
            }).join('');

            // Project actions
            wrap.querySelectorAll('.dkr-act-btn[data-paction]').forEach(b => {
                b.addEventListener('click', async () => {
                    const card = b.closest('.dkr-project-card');
                    const project = card.dataset.project;
                    const action = b.dataset.paction;
                    if (action === 'down' && !(await confirmDialog(`Docker Compose Down ${t('dla projektu')} ${project}?`, ''))) return;
                    // Note: Original code had complex handling here (loading state etc).
                    // I will replicate it simplified or assume it's fine.
                    // The view showed: b.disabled = true; ... toast ...
                    // I'll try to include it.
                    b.disabled = true;
                    b.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                    try {
                        await api(`/docker/projects/${project}/action`, { method: 'POST', body: { action } });
                        toast(`${project}: ${action} OK`, 'success');
                    } catch (err) { toast(`${project}: ${t('błąd')} ${action}`, 'error'); }
                    setTimeout(async () => { await loadProjects(); fillProjects(); }, 2000);
                });
            });

            // Compose file viewer
            wrap.querySelectorAll('.dkr-compose-btn').forEach(b => {
                b.addEventListener('click', () => {
                    openComposeEditor(b.dataset.project);
                });
            });

            // Delete project
            wrap.querySelectorAll('.dkr-delete-proj-btn').forEach(b => {
                b.addEventListener('click', async () => {
                    const project = b.dataset.project;
                    if (!await confirmDialog(t('Usunąć projekt'), t('Czy na pewno usunąć projekt') + ` <b>${project}</b>? ` + t('Tej operacji nie można cofnąć.'))) return;

                    b.disabled = true;
                    b.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                    try {
                        await api(`/docker/projects/${project}`, { method: 'DELETE' });
                        toast(`${t('Projekt')} ${project} ${t('usunięty')}`, 'success');
                        await loadProjects();
                        fillProjects();
                    } catch (err) {
                        toast(`${t('Błąd usuwania projektu')} ${project}`, 'error');
                        b.disabled = false;
                        b.innerHTML = '<i class="fas fa-trash-alt"></i>';
                    }
                });
            });

            // Project logs viewer
            wrap.querySelectorAll('.dkr-logs-btn').forEach(b => {
                b.addEventListener('click', () => {
                    let services = [];
                    try { services = JSON.parse(atob(b.dataset.services || '')); } catch {}
                    openProjectLogs(b.dataset.project, services);
                });
            });
        }
    }

    function openProjectLogs(projectName, services) {
        const overlay = document.createElement('div');
        overlay.className = 'dkr-modal-overlay';
        const serviceOpts = services.length
            ? services.map(s => `<option value="${s.service}">${s.name} (${s.service})</option>`).join('')
            : '';
        overlay.innerHTML = `
            <div class="dkr-modal dkr-modal-logs">
                <div class="dkr-modal-header">
                    <span><i class="fas fa-rectangle-list"></i> Logi — ${projectName}</span>
                    <button class="dkr-modal-close" id="dkr-plogs-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="dkr-modal-body dkr-modal-body-flex">
                    <div class="dkr-logs-toolbar">
                        <select class="dkr-select" id="dkr-plogs-service">
                            <option value="">Wszystkie kontenery</option>
                            ${serviceOpts}
                        </select>
                        <select class="dkr-select" id="dkr-plogs-lines">
                            <option value="100">100 linii</option>
                            <option value="200" selected>200 linii</option>
                            <option value="500">500 linii</option>
                            <option value="1000">1000 linii</option>
                            <option value="5000">5000 linii</option>
                        </select>
                        <input class="dkr-filter" id="dkr-plogs-search" placeholder="Szukaj w logach...">
                        <button class="dkr-btn" id="dkr-plogs-refresh"><i class="fas fa-sync-alt"></i></button>
                        <label class="dkr-check"><input type="checkbox" id="dkr-plogs-follow" checked> Auto-scroll</label>
                    </div>
                    <pre class="dkr-logs app-flex-fill" id="dkr-plogs"></pre>
                </div>
            </div>
        `;
        body.appendChild(overlay);

        async function loadProjectLogs() {
            const lines = overlay.querySelector('#dkr-plogs-lines').value;
            const search = overlay.querySelector('#dkr-plogs-search').value;
            const service = overlay.querySelector('#dkr-plogs-service').value;
            try {
                const r = await api(`/docker/projects/${projectName}/logs?lines=${lines}&search=${encodeURIComponent(search)}&service=${encodeURIComponent(service)}`);
                const pre = overlay.querySelector('#dkr-plogs');
                if (pre) {
                    pre.textContent = (r.logs || []).join('\n');
                    if (overlay.querySelector('#dkr-plogs-follow')?.checked) pre.scrollTop = pre.scrollHeight;
                }
            } catch (err) {
                const pre = overlay.querySelector('#dkr-plogs');
                if (pre) pre.textContent = t('Błąd pobierania logów');
            }
        }

        loadProjectLogs();
        overlay.querySelector('#dkr-plogs-close').addEventListener('click', () => { clearInterval(autoIv); overlay.remove(); });
        overlay.addEventListener('click', e => { if (e.target === overlay) { clearInterval(autoIv); overlay.remove(); } });
        overlay.querySelector('#dkr-plogs-refresh').addEventListener('click', loadProjectLogs);
        overlay.querySelector('#dkr-plogs-lines').addEventListener('change', loadProjectLogs);
        overlay.querySelector('#dkr-plogs-service').addEventListener('change', loadProjectLogs);
        let debounce;
        overlay.querySelector('#dkr-plogs-search').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(loadProjectLogs, 400); });
        // Auto-refresh every 5s
        const autoIv = setInterval(() => {
            if (!document.body.contains(overlay)) { clearInterval(autoIv); return; }
            loadProjectLogs();
        }, 5000);
    }

    function openComposeEditor(projectName) {
        // Open a modal to view/edit compose file
        const overlay = document.createElement('div');
        overlay.className = 'dkr-modal-overlay';
        overlay.innerHTML = `
            <div class="dkr-modal">
                <div class="dkr-modal-header">
                    <span><i class="fas fa-file-code"></i> ${projectName}/docker-compose.yaml</span>
                    <button class="dkr-modal-close" id="dkr-compose-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="dkr-modal-body">
                    <textarea class="dkr-compose-editor" id="dkr-compose-text" spellcheck="false">${t('Ładowanie...')}</textarea>
                </div>
                <div class="dkr-modal-footer">
                    <button class="dkr-btn" id="dkr-compose-cancel">Anuluj</button>
                    <button class="dkr-btn primary" id="dkr-compose-save"><i class="fas fa-save"></i> Zapisz</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);
        const textarea = overlay.querySelector('#dkr-compose-text');
        overlay.querySelector('#dkr-compose-close').addEventListener('click', () => overlay.remove());
        overlay.querySelector('#dkr-compose-cancel').addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

        api(`/docker/projects/${projectName}/compose`).then(r => {
            textarea.value = r.content || '';
        }).catch(() => { textarea.value = t('# Błąd odczytu'); });

        overlay.querySelector('#dkr-compose-save').addEventListener('click', async () => {
            try {
                await api(`/docker/projects/${projectName}/compose`, {
                    method: 'PUT',
                    body: { content: textarea.value }
                });
                toast('Zapisano docker-compose.yaml', 'success');
                overlay.remove();
            } catch (err) {
                toast(err?.error || t('Błąd zapisu'), 'error');
            }
        });
    }

    function openCreateProjectModal() {
        const overlay = document.createElement('div');
        overlay.className = 'dkr-modal-overlay';
        overlay.innerHTML = `
            <div class="dkr-modal">
                <div class="dkr-modal-header">
                    <span><i class="fas fa-plus"></i> Nowy projekt Docker Compose</span>
                    <button class="dkr-modal-close" id="dkr-create-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="dkr-modal-body dkr-modal-body-form">
                    <div>
                        <label class="app-form-label">${t('Nazwa projektu')}</label>
                        <input class="dkr-filter app-input-full" id="dkr-create-name" placeholder="moj-projekt">
                    </div>
                    <div class="app-flex-col">
                        <div class="app-row-between">
                            <label class="app-form-label-inline">docker-compose.yaml</label>
                            <label class="dkr-btn dkr-btn-file" id="dkr-create-file-label">
                                <i class="fas fa-file-import"></i> ${t('Wczytaj z pliku')}
                                <input type="file" id="dkr-create-file" accept=".yml,.yaml" class="hidden">
                            </label>
                        </div>
                        <textarea class="dkr-compose-editor dkr-compose-flex" id="dkr-create-content" spellcheck="false">version: '3'

services:
  app:
    image: hello-world
    restart: unless-stopped
</textarea>
                    </div>
                </div>
                <div class="dkr-modal-footer">
                    <button class="dkr-btn" id="dkr-create-cancel">${t('Anuluj')}</button>
                    <button class="dkr-btn primary" id="dkr-create-save"><i class="fas fa-plus"></i> ${t('Utwórz')}</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);
        overlay.querySelector('#dkr-create-close').addEventListener('click', () => overlay.remove());
        overlay.querySelector('#dkr-create-cancel').addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

        overlay.querySelector('#dkr-create-file').addEventListener('change', e => {
            const file = e.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = () => {
                overlay.querySelector('#dkr-create-content').value = reader.result;
                // Auto-fill project name from filename (e.g. "nextcloud-compose.yml" → "nextcloud-compose")
                const nameInput = overlay.querySelector('#dkr-create-name');
                if (!nameInput.value.trim()) {
                    const baseName = file.name.replace(/\.(ya?ml)$/i, '').replace(/[-_]?(docker[-_]?)?compose/i, '').replace(/^[-_]+|[-_]+$/g, '');
                    if (baseName) nameInput.value = baseName;
                }
                toast(t('Plik wczytany: ') + file.name, 'success');
            };
            reader.onerror = () => toast(t('Błąd odczytu pliku'), 'error');
            reader.readAsText(file);
        });

        overlay.querySelector('#dkr-create-save').addEventListener('click', async () => {
            const name = overlay.querySelector('#dkr-create-name').value.trim();
            const content = overlay.querySelector('#dkr-create-content').value.trim();
            if (!name) { toast(t('Podaj nazwę projektu'), 'warning'); return; }
            try {
                await api('/docker/projects', {
                    method: 'POST',
                    body: { name, content }
                });
                toast(`Projekt "${name}" utworzony`, 'success');
                overlay.remove();
                await loadProjects();
                renderProjectsTab(); // re-render full tab to pick up new project
            } catch (err) {
                toast(err?.error || t('Błąd tworzenia projektu'), 'error');
            }
        });
    }

    // ─── IMAGES TAB ───
    async function loadImages() {
        try {
            const res = await api('/docker/images');
            S.images = Array.isArray(res) ? res : [];
            if (!Array.isArray(res) && res?.error) toast(res.error, 'error');
        } catch { toast(t('Błąd pobierania obrazów'), 'error'); }
    }

    function renderImagesTab() {
        main.innerHTML = `
            <div class="dkr-toolbar">
                <span class="dkr-toolbar-title"><i class="fas fa-clone"></i> Obrazy <span class="dkr-badge" id="dkr-img-count">0</span></span>
                <input class="dkr-filter" id="dkr-img-filter" placeholder="Filtruj...">
                <button class="dkr-btn danger" id="dkr-img-prune" title="${t('Usuń nieużywane')}"><i class="fas fa-broom"></i> ${t('Wyczyść')}</button>
                <button class="dkr-btn" id="dkr-img-refresh"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="dkr-table-wrap">
                <table class="dkr-table">
                    <thead><tr>
                        <th>Repozytorium</th>
                        <th>Tag</th>
                        <th>ID</th>
                        <th>Rozmiar</th>
                        <th>Utworzony</th>
                        <th class="app-col-sm"></th>
                    </tr></thead>
                    <tbody id="dkr-img-body"></tbody>
                </table>
            </div>
        `;
        let imgFilter = '';
        main.querySelector('#dkr-img-filter').addEventListener('input', e => { imgFilter = e.target.value.toLowerCase(); fillImages(); });
        main.querySelector('#dkr-img-refresh').addEventListener('click', async () => { await loadImages(); fillImages(); });
        main.querySelector('#dkr-img-prune').addEventListener('click', async () => {
            if (!await confirmDialog(t('Usunąć wszystkie nieużywane obrazy?'))) return;
            try {
                const r = await api('/docker/images/prune', { method: 'POST' });
                toast(t('Wyczyszczono nieużywane obrazy'), 'success');
                await loadImages(); fillImages();
            } catch { toast(t('Błąd czyszczenia'), 'error'); }
        });
        loadImages().then(() => fillImages());

        function fillImages() {
            const tbody = main.querySelector('#dkr-img-body');
            const badge = main.querySelector('#dkr-img-count');
            if (!tbody) return;
            const filtered = S.images.filter(i =>
                !imgFilter || (i.repository||'').toLowerCase().includes(imgFilter) || (i.tag||'').toLowerCase().includes(imgFilter)
            );
            badge.textContent = filtered.length;
            tbody.innerHTML = filtered.map(i => `
                <tr>
                    <td class="dkr-ellipsis" title="${esc(i.repository)}">${esc(i.repository)}</td>
                    <td><span class="dkr-tag">${esc(i.tag)}</span></td>
                    <td class="dkr-muted">${esc((i.id||'').replace('sha256:','').substring(0,12))}</td>
                    <td>${esc(i.size)}</td>
                    <td class="dkr-muted dkr-ellipsis">${esc(i.created)}</td>
                    <td><button class="dkr-act-btn danger" data-imgdel="${esc(i.id)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button></td>
                </tr>
            `).join('');
            tbody.querySelectorAll('[data-imgdel]').forEach(b => {
                b.addEventListener('click', async () => {
                    if (!await confirmDialog(t('Usunąć ten obraz?'))) return;
                    try {
                        await api(`/docker/images/${encodeURIComponent(b.dataset.imgdel)}?force=true`, { method: 'DELETE' });
                        toast(t('Obraz usunięty'), 'success');
                        await loadImages(); fillImages();
                    } catch { toast(t('Błąd usuwania obrazu'), 'error'); }
                });
            });
        }
    }

    // ─── BACKUPS TAB ───
    async function loadBackups() {
        try {
            const res = await api('/docker/backups');
            if (!Array.isArray(res)) {
                if (res?.error) toast(res.error, 'error');
                return [];
            }
            return res;
        } catch { return []; }
    }

    function renderBackupsTab() {
        main.innerHTML = '<div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        loadBackups().then(backups => {
            main.innerHTML = `
                <div class="dkr-toolbar">
                    <span class="dkr-toolbar-title"><i class="fas fa-shield-alt"></i> ${t('Backupy Docker')}
                        <span class="dkr-badge">${backups.length}</span></span>
                    ${isAdmin ? `<button class="dkr-btn primary" id="dkr-bkp-create"><i class="fas fa-plus"></i> ${t('Nowy backup')}</button>` : ''}
                    <button class="dkr-btn" id="dkr-bkp-refresh"><i class="fas fa-sync-alt"></i></button>
                </div>
                <div id="dkr-bkp-progress" style="display:none" class="dkr-bkp-progress-bar"></div>
                <div class="dkr-table-wrap">
                    ${backups.length ? `
                    <table class="dkr-table">
                        <thead><tr>
                            <th>${t('Etykieta')}</th><th>${t('Typ')}</th><th>${t('Tryb')}</th>
                            <th>${t('Data')}</th><th>${t('Rozmiar')}</th><th>${t('Status')}</th>
                            <th>${t('Wolumeny')}</th><th></th>
                        </tr></thead>
                        <tbody>${backups.map(b => {
                            const typeIcon = b.type === 'project' ? 'fa-layer-group' : 'fa-box';
                            const typeName = b.type === 'project' ? t('Projekt') : t('Kontener');
                            const modeBadge = b.mode === 'full'
                                ? `<span class="dkr-bkp-mode full">${t('Pełny')}</span>`
                                : `<span class="dkr-bkp-mode light">${t('Lekki')}</span>`;
                            const statusCls = b.status === 'complete' ? 'success' : b.status === 'running' ? 'info' : 'danger';
                            const statusText = b.status === 'complete' ? t('Gotowy') : b.status === 'running' ? t('W toku...') : t('Błąd');
                            const date = b.created ? new Date(b.created).toLocaleString() : '—';
                            const volCount = (b.volumes || []).length;
                            return `<tr class="dkr-row" data-id="${esc(b.id)}">
                                <td><strong>${esc(b.label || b.name)}</strong><br><span class="dkr-muted">${esc(b.name)}</span></td>
                                <td><i class="fas ${typeIcon}"></i> ${typeName}</td>
                                <td>${modeBadge}</td>
                                <td class="dkr-muted">${date}</td>
                                <td>${b.size_human || '—'}</td>
                                <td><span class="dkr-project-status ${statusCls}">${statusText}</span></td>
                                <td>${volCount > 0 ? volCount : '—'}</td>
                                <td class="dkr-actions">${isAdmin && b.status === 'complete' ? `
                                    <button class="dkr-act-btn success dkr-bkp-restore" data-id="${esc(b.id)}" title="${t('Przywróć')}"><i class="fas fa-undo"></i></button>
                                    <button class="dkr-act-btn danger dkr-bkp-delete" data-id="${esc(b.id)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                                ` : ''}</td>
                            </tr>`;
                        }).join('')}</tbody>
                    </table>` : `<div class="dkr-empty"><i class="fas fa-shield-alt" style="font-size:2em;margin-bottom:8px;opacity:.3"></i><br>${t('Brak backupów')}</div>`}
                </div>
            `;

            // Refresh
            main.querySelector('#dkr-bkp-refresh')?.addEventListener('click', () => renderBackupsTab());

            // Create backup button
            main.querySelector('#dkr-bkp-create')?.addEventListener('click', () => showCreateBackupModal());

            // Restore buttons
            main.querySelectorAll('.dkr-bkp-restore').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const bid = btn.dataset.id;
                    if (!await confirmDialog(t('Przywrócić ten backup?'), t('Kontenery z tą samą nazwą zostaną zastąpione. Wolumeny zostaną nadpisane.'))) return;
                    btn.disabled = true;
                    showBackupProgress('restore');
                    const res = await api(`/docker/backups/${bid}/restore`, { method: 'POST', body: { restore_volumes: true } });
                    if (res?.error) { toast(res.error, 'error'); hideBackupProgress(); btn.disabled = false; }
                });
            });

            // Delete buttons
            main.querySelectorAll('.dkr-bkp-delete').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const bid = btn.dataset.id;
                    if (!await confirmDialog(t('Usunąć ten backup?'))) return;
                    const res = await api(`/docker/backups/${bid}`, { method: 'DELETE' });
                    if (res?.ok) { toast(t('Backup usunięty'), 'success'); renderBackupsTab(); }
                    else toast(res?.error || t('Błąd'), 'error');
                });
            });

            // SocketIO progress listeners
            _setupBackupSocketListeners();
        });
    }

    function showBackupProgress(type) {
        const bar = main.querySelector('#dkr-bkp-progress');
        if (!bar) return;
        const label = type === 'restore' ? t('Przywracanie...') : t('Tworzenie backupu...');
        bar.style.display = 'block';
        bar.innerHTML = `
            <div class="dkr-bkp-progress-inner">
                <div class="dkr-bkp-progress-track">
                    <div class="dkr-bkp-progress-fill" id="dkr-bkp-pbar" style="width:0%"></div>
                </div>
                <span class="dkr-bkp-progress-text" id="dkr-bkp-pmsg">${label}</span>
            </div>
        `;
    }

    function updateBackupProgress(pct, msg) {
        const bar = main.querySelector('#dkr-bkp-pbar');
        const msgEl = main.querySelector('#dkr-bkp-pmsg');
        if (bar) bar.style.width = `${pct}%`;
        if (msgEl) msgEl.textContent = msg || '';
    }

    function hideBackupProgress() {
        const bar = main.querySelector('#dkr-bkp-progress');
        if (bar) bar.style.display = 'none';
    }

    function _setupBackupSocketListeners() {
        if (!NAS.socket) return;
        NAS.socket.off('docker_backup');
        NAS.socket.off('docker_restore');
        NAS.socket.on('docker_backup', (d) => {
            if (S.tab !== 'backups') return;
            if (d.status === 'done') {
                hideBackupProgress();
                toast(d.message || t('Backup gotowy'), 'success');
                renderBackupsTab();
            } else if (d.status === 'error') {
                hideBackupProgress();
                toast(d.message || t('Błąd backupu'), 'error');
                renderBackupsTab();
            } else {
                updateBackupProgress(d.percent || 0, d.message || '');
            }
        });
        NAS.socket.on('docker_restore', (d) => {
            if (S.tab !== 'backups') return;
            if (d.status === 'done') {
                hideBackupProgress();
                toast(d.message || t('Przywracanie zakończone'), 'success');
                renderBackupsTab();
            } else if (d.status === 'error') {
                hideBackupProgress();
                toast(d.message || t('Błąd przywracania'), 'error');
                renderBackupsTab();
            } else {
                updateBackupProgress(d.percent || 0, d.message || '');
            }
        });
    }

    async function showCreateBackupModal() {
        // Load containers + projects in parallel
        const [containers, projects] = await Promise.all([
            api('/docker/containers').catch(() => []),
            api('/docker/projects').catch(() => []),
        ]);

        const overlay = document.createElement('div');
        overlay.className = 'dkr-modal-overlay';
        overlay.innerHTML = `
            <div class="dkr-modal" style="max-width:520px">
                <div class="dkr-modal-header">
                    <span>${t('Nowy backup Docker')}</span>
                    <button class="dkr-modal-close">&times;</button>
                </div>
                <div class="dkr-modal-body dkr-modal-body-form">
                    <label style="font-size:13px;font-weight:600">${t('Typ')}</label>
                    <div style="display:flex;gap:8px">
                        <button class="dkr-btn primary dkr-bkp-type-btn" data-type="container"><i class="fas fa-box"></i> ${t('Kontener')}</button>
                        <button class="dkr-btn dkr-bkp-type-btn" data-type="project"><i class="fas fa-layer-group"></i> ${t('Projekt')}</button>
                    </div>
                    <label style="font-size:13px;font-weight:600">${t('Cel')}</label>
                    <select id="dkr-bkp-target" class="dkr-select" style="width:100%;padding:8px;background:var(--bg-elevated);color:var(--text-primary)">
                    </select>
                    <label style="font-size:13px;font-weight:600">${t('Tryb')}</label>
                    <div style="display:flex;gap:8px">
                        <button class="dkr-btn dkr-bkp-mode-btn" data-mode="light">
                            <i class="fas fa-feather"></i> ${t('Lekki')}
                            <span class="dkr-muted" style="margin-left:4px">${t('(bez obrazów)')}</span>
                        </button>
                        <button class="dkr-btn primary dkr-bkp-mode-btn" data-mode="full">
                            <i class="fas fa-archive"></i> ${t('Pełny')}
                            <span class="dkr-muted" style="margin-left:4px;color:inherit">${t('(z obrazami)')}</span>
                        </button>
                    </div>
                    <label style="font-size:13px;font-weight:600">${t('Etykieta')} <span class="dkr-muted">(${t('opcjonalna')})</span></label>
                    <input id="dkr-bkp-label" class="dkr-filter" style="width:100%;box-sizing:border-box" placeholder="${t('np. Przed aktualizacją')}" />
                </div>
                <div class="dkr-modal-footer">
                    <button class="dkr-btn" id="dkr-bkp-cancel">${t('Anuluj')}</button>
                    <button class="dkr-btn primary" id="dkr-bkp-submit"><i class="fas fa-shield-alt"></i> ${t('Utwórz backup')}</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);

        let selectedType = 'container';
        let selectedMode = 'full';

        function updateTargetOptions() {
            const select = overlay.querySelector('#dkr-bkp-target');
            const optStyle = 'style="background:var(--bg-elevated);color:var(--text-primary)"';
            if (selectedType === 'container') {
                const cList = Array.isArray(containers) ? containers : [];
                select.innerHTML = `<option value="__all__" ${optStyle}>⚡ ${t('Wszystkie kontenery')} (${cList.length})</option>`
                    + cList.map(c =>
                        `<option value="${esc(c.id)}" ${optStyle}>${esc(c.name)} (${esc(c.image)})</option>`
                    ).join('');
            } else {
                const pList = Array.isArray(projects) ? projects : [];
                select.innerHTML = `<option value="__all__" ${optStyle}>⚡ ${t('Wszystkie projekty')} (${pList.length})</option>`
                    + pList.map(p =>
                        `<option value="${esc(p.name)}" ${optStyle}>${esc(p.name)} — ${p.running}/${p.total} ${t('kontenerów')}</option>`
                    ).join('');
            }
        }

        // Type toggle
        overlay.querySelectorAll('.dkr-bkp-type-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                overlay.querySelectorAll('.dkr-bkp-type-btn').forEach(b => b.classList.remove('primary'));
                btn.classList.add('primary');
                selectedType = btn.dataset.type;
                updateTargetOptions();
            });
        });

        // Mode toggle
        overlay.querySelectorAll('.dkr-bkp-mode-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                overlay.querySelectorAll('.dkr-bkp-mode-btn').forEach(b => b.classList.remove('primary'));
                btn.classList.add('primary');
                selectedMode = btn.dataset.mode;
            });
        });

        // Populate initial targets
        updateTargetOptions();

        const close = () => overlay.remove();
        overlay.querySelector('.dkr-modal-close').addEventListener('click', close);
        overlay.querySelector('#dkr-bkp-cancel').addEventListener('click', close);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

        overlay.querySelector('#dkr-bkp-submit').addEventListener('click', async () => {
            const target = overlay.querySelector('#dkr-bkp-target').value;
            const label = overlay.querySelector('#dkr-bkp-label').value.trim();
            if (!target) { toast(t('Wybierz cel backupu'), 'error'); return; }
            close();
            showBackupProgress('backup');
            _setupBackupSocketListeners();
            const res = await api('/docker/backups', {
                method: 'POST',
                body: { type: selectedType, name: target, label, mode: selectedMode }
            });
            if (res?.error) { toast(res.error, 'error'); hideBackupProgress(); }
        });
    }

    // ─── SYSTEM TAB ───
    function renderSystemTab() {
        main.innerHTML = '<div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        api('/docker/system').then(info => {
            S.systemInfo = info;
            main.innerHTML = `
                <div class="dkr-toolbar">
                    <span class="dkr-toolbar-title"><i class="fas fa-server"></i> Docker System</span>
                    <button class="dkr-btn danger" id="dkr-vol-prune"><i class="fas fa-broom"></i> ${t('Wyczyść wolumeny')}</button>
                </div>
                <div class="dkr-system">
                    <div class="dkr-sys-grid">
                        <div class="dkr-sys-card">
                            <div class="dkr-sys-card-title">Docker</div>
                            <div class="dkr-kv"><span>Wersja</span><span>${info.version}</span></div>
                            <div class="dkr-kv"><span>System</span><span>${info.os}</span></div>
                            <div class="dkr-kv"><span>Kernel</span><span>${info.kernel}</span></div>
                            <div class="dkr-kv"><span>Architektura</span><span>${info.arch}</span></div>
                            <div class="dkr-kv"><span>Sterownik</span><span>${info.storage_driver}</span></div>
                        </div>
                        <div class="dkr-sys-card">
                            <div class="dkr-sys-card-title">Kontenery</div>
                            <div class="dkr-sys-big-num">${info.containers}</div>
                            <div class="dkr-sys-row">
                                <span class="dkr-sys-label success"><i class="fas fa-play"></i> ${info.containers_running} ${t('działa')}</span>
                                <span class="dkr-sys-label muted"><i class="fas fa-stop"></i> ${info.containers_stopped} zatrzym.</span>
                                <span class="dkr-sys-label warning"><i class="fas fa-pause"></i> ${info.containers_paused} wstrzym.</span>
                            </div>
                        </div>
                        <div class="dkr-sys-card">
                            <div class="dkr-sys-card-title">Obrazy</div>
                            <div class="dkr-sys-big-num">${info.images}</div>
                        </div>
                    </div>
                    ${info.disk_usage && info.disk_usage.length ? `
                        <div class="dkr-inspect-section"><h3>Wykorzystanie dysku</h3>
                        <table class="dkr-table dkr-table-compact">
                            <thead><tr><th>${t('Typ')}</th><th>${t('Całkowity')}</th><th>${t('Aktywne')}</th><th>${t('Rozmiar')}</th><th>${t('Do odzyskania')}</th></tr></thead>
                            <tbody>${info.disk_usage.map(d => `
                                <tr><td>${d.type}</td><td>${d.total}</td><td>${d.active}</td><td>${d.size}</td><td>${d.reclaimable}</td></tr>
                            `).join('')}</tbody>
                        </table></div>
                    ` : ''}
                </div>
            `;
            main.querySelector('#dkr-vol-prune')?.addEventListener('click', async () => {
                if (!await confirmDialog(t('Usunąć nieużywane wolumeny?'))) return;
                try {
                    await api('/docker/volumes/prune', { method: 'POST' });
                    toast('Wyczyszczono wolumeny', 'success');
                } catch { toast(t('Błąd'), 'error'); }
            });
        }).catch(() => { main.innerHTML = `<div class="dkr-empty">${t('Błąd połączenia z Dockerem')}</div>`; });
    }

    // Check if Docker is available before rendering; show install screen if not
    async function checkDockerAndRender() {
        main.innerHTML = `<div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Sprawdzanie Dockera...')}</div>`;
        const status = await api('/docker/status').catch(() => ({ available: false }));
        if (!status.available) {
            renderInstallScreen();
            return;
        }
        renderTab();
        const refreshInterval = setInterval(() => {
            if (!WM.windows.has('docker-manager')) { clearInterval(refreshInterval); clearAllIntervals(); return; }
            if (S.tab === 'containers' && !S.selectedContainer) { loadContainers().then(fillContainersTable); }
        }, 10000);
        addInterval(refreshInterval);
    }

    function renderInstallScreen() {
        main.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:20px;padding:40px;text-align:center">
                <i class="fab fa-docker" style="font-size:4em;color:var(--text-muted)"></i>
                <div style="font-size:1.3em;font-weight:600">${t('Docker nie jest zainstalowany')}</div>
                <div style="color:var(--text-muted);max-width:380px">${t('Zainstaluj Docker Engine, aby zarządzać kontenerami i projektami Compose.')}</div>
                <div id="dkr-disk-warn" style="display:none;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:10px 16px;color:#ef4444;font-size:0.85em;max-width:420px"></div>
                <button class="dkr-btn success" id="dkr-install-btn" style="padding:10px 28px;font-size:1em">
                    <i class="fas fa-download"></i> ${t('Zainstaluj Docker')}
                </button>
                <div id="dkr-install-progress" style="display:none;width:100%;max-width:420px">
                    <div style="background:var(--bg-secondary);border-radius:4px;height:8px;overflow:hidden;margin-bottom:8px">
                        <div id="dkr-install-bar" style="height:100%;background:var(--accent);width:0%;transition:width 0.3s"></div>
                    </div>
                    <div id="dkr-install-msg" style="font-size:0.85em;color:var(--text-muted)"></div>
                    <div id="dkr-install-poll-msg" style="font-size:0.8em;color:var(--text-muted);margin-top:4px;opacity:.7"></div>
                </div>
            </div>`;

        // Check disk space and warn if low
        api('/system/info').then(info => {
            const rootDisk = (info.disks || []).find(d => d.label && (d.label.includes('Root') || d.label.includes('root')));
            if (rootDisk) {
                const freeMB = rootDisk.free / (1024 * 1024);
                const warn = main.querySelector('#dkr-disk-warn');
                if (warn) {
                    if (freeMB < 500) {
                        warn.style.display = 'block';
                        warn.innerHTML = `<i class="fas fa-exclamation-triangle"></i> ${t('Mało miejsca na dysku')}: ${(freeMB/1024).toFixed(1)} GB ${t('wolne na partycji root. Docker wymaga ok. 500 MB — instalacja może się nie powieść.')}`;
                        main.querySelector('#dkr-install-btn')?.setAttribute('data-low-disk', '1');
                    } else if (freeMB < 1024) {
                        warn.style.display = 'block';
                        warn.style.background = 'rgba(234,179,8,.1)';
                        warn.style.borderColor = 'rgba(234,179,8,.3)';
                        warn.style.color = '#eab308';
                        warn.innerHTML = `<i class="fas fa-info-circle"></i> ${t('Uwaga')}: ${(freeMB/1024).toFixed(1)} GB ${t('wolne. Docker używa ok. 400 MB podczas instalacji.')}`;
                    }
                }
            }
        }).catch(() => {});

        main.querySelector('#dkr-install-btn').addEventListener('click', async () => {
            const btn = main.querySelector('#dkr-install-btn');
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Instalowanie...')}`;
            main.querySelector('#dkr-install-progress').style.display = 'block';

            let installDone = false;

            if (NAS.socket) {
                NAS.socket.on('docker_install', (d) => {
                    const bar = main.querySelector('#dkr-install-bar');
                    const msg = main.querySelector('#dkr-install-msg');
                    if (bar) bar.style.width = (d.percent || 0) + '%';
                    if (msg) msg.textContent = d.message || '';
                    if (d.status === 'done') {
                        installDone = true;
                        NAS.socket.off('docker_install');
                        toast(t('Docker zainstalowany!'), 'success');
                        checkDockerAndRender();
                    } else if (d.status === 'error') {
                        installDone = true;
                        NAS.socket.off('docker_install');
                        toast(d.message || t('Instalacja nie powiodła się'), 'error');
                        btn.disabled = false;
                        btn.innerHTML = `<i class="fas fa-download"></i> ${t('Spróbuj ponownie')}`;
                        _cl('error', 'docker install failed', d.message);
                    }
                });
            }

            // Fallback poll: if SocketIO loses connection during long install, poll status every 15s
            const pollInterval = setInterval(async () => {
                if (installDone) { clearInterval(pollInterval); return; }
                try {
                    const st = await api('/docker/status').catch(() => null);
                    const pollMsg = main.querySelector('#dkr-install-poll-msg');
                    if (st?.available) {
                        installDone = true;
                        clearInterval(pollInterval);
                        if (NAS.socket) NAS.socket.off('docker_install');
                        toast(t('Docker zainstalowany!'), 'success');
                        checkDockerAndRender();
                    } else if (pollMsg) {
                        pollMsg.textContent = t('Instalacja w toku… (może trwać 3–8 minut)');
                    }
                } catch { /* ignore */ }
            }, 15000);
            addInterval(pollInterval);

            const res = await api('/docker/install', { method: 'POST' }).catch(e => ({ error: e.message }));
            if (res.error) {
                installDone = true;
                clearInterval(pollInterval);
                toast(res.error, 'error');
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-download"></i> ${t('Spróbuj ponownie')}`;
            } else if (res.installed) {
                installDone = true;
                clearInterval(pollInterval);
                checkDockerAndRender();
            }
            // If status=started, progress comes via socketio + poll fallback
        });
    }

    // Initial render
    checkDockerAndRender();
}


// ═══════════════════════════════════════════════════════════
//  VM MANAGER (QEMU/KVM)
// ═══════════════════════════════════════════════════════════

