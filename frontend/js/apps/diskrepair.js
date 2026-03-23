/* ═══════════════════════════════════════════════════════════
   ${t('EthOS — Disk Repair  (Naprawa dysków)')}
   SMART diagnostics, fsck, badblocks scanner.
   ═══════════════════════════════════════════════════════════ */

AppRegistry['disk-repair'] = function (appDef) {
    createWindow('disk-repair', {
        title: t('Naprawa dysków'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1000,
        height: 700,
        onRender: (body) => renderDiskRepairApp(body),
    });
};

function renderDiskRepairApp(body) {
    const state = {
        disks: [],
        selectedDisk: null,
        activeTab: 'info',
        operation: null,
        pollTimer: null,
        logOffset: 0,
        smartData: null,
        fsDetail: null,
        history: [],
    };

    body.innerHTML = `
    <style>
        .dr-wrap { display:flex; height:100%; overflow:hidden; }

        /* ── Sidebar ── */
        .dr-sidebar { width:250px; min-width:250px; background:var(--bg-secondary); border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:hidden; }
        .dr-sidebar-header { display:flex; align-items:center; justify-content:space-between; padding:12px 14px; border-bottom:1px solid var(--border); }
        .dr-sidebar-header span { font-weight:600; font-size:13px; color:var(--text-primary); }
        .dr-disk-list { flex:1; overflow-y:auto; padding:6px; }
        .dr-disk-item { display:flex; align-items:center; gap:10px; padding:10px; border-radius:8px; cursor:pointer; margin-bottom:4px; transition:background .15s; }
        .dr-disk-item:hover { background:var(--bg-card); }
        .dr-disk-item.active { background:var(--accent); color:#fff; }
        .dr-disk-item.active .dr-disk-model { color:#fff; }
        .dr-disk-item.active .dr-disk-size { color:rgba(255,255,255,.7); }
        .dr-disk-item.active .dr-temp-badge { background:rgba(255,255,255,.2); color:#fff; }
        .dr-disk-icon { font-size:22px; width:28px; text-align:center; flex-shrink:0; }
        .dr-disk-meta { flex:1; min-width:0; }
        .dr-disk-model { font-size:12px; font-weight:600; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .dr-disk-size { font-size:11px; color:var(--text-muted); }
        .dr-health-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
        .dr-temp-badge { font-size:10px; padding:2px 6px; border-radius:4px; background:var(--bg-primary); color:var(--text-muted); flex-shrink:0; }

        /* ── Main ── */
        .dr-main { flex:1; display:flex; flex-direction:column; overflow:hidden; }
        .dr-banner { display:none; padding:10px 16px; background:var(--bg-card); border-bottom:1px solid var(--border); }
        .dr-banner.active { display:flex; align-items:center; gap:12px; }
        .dr-banner-info { flex:1; min-width:0; }
        .dr-banner-title { font-size:12px; font-weight:600; color:var(--text-primary); }
        .dr-banner-progress { height:8px; background:var(--bg-primary); border-radius:4px; overflow:hidden; margin-top:6px; }
        .dr-banner-bar { height:100%; background:linear-gradient(90deg, var(--accent), #6366f1); border-radius:4px; transition:width .3s; width:0%; }
        .dr-banner-detail { font-size:11px; color:var(--text-muted); margin-top:4px; }

        /* ── Tabs ── */
        .dr-tabs { display:flex; border-bottom:1px solid var(--border); padding:0 16px; background:var(--bg-secondary); flex-shrink:0; }
        .dr-tab { padding:10px 16px; font-size:12px; font-weight:500; color:var(--text-muted); cursor:pointer; border-bottom:2px solid transparent; transition:color .15s, border-color .15s; white-space:nowrap; }
        .dr-tab:hover { color:var(--text-primary); }
        .dr-tab.active { color:var(--accent); border-bottom-color:var(--accent); }

        /* ── Tab content ── */
        .dr-content { flex:1; overflow-y:auto; padding:16px; }
        .dr-empty { display:flex; align-items:center; justify-content:center; height:100%; color:var(--text-muted); font-size:14px; flex-direction:column; gap:8px; }
        .dr-empty i { font-size:40px; opacity:.4; }

        .dr-card { background:var(--bg-card); border-radius:10px; padding:16px; margin-bottom:14px; }
        .dr-card-title { font-weight:600; font-size:13px; color:var(--text-primary); margin-bottom:10px; display:flex; align-items:center; gap:8px; }
        .dr-card-title i { width:18px; text-align:center; }

        .dr-table { width:100%; border-collapse:collapse; font-size:12px; }
        .dr-table th { text-align:left; font-weight:600; padding:8px 10px; border-bottom:2px solid var(--border); color:var(--text-secondary); font-size:11px; text-transform:uppercase; letter-spacing:.3px; }
        .dr-table td { padding:7px 10px; border-bottom:1px solid var(--border); color:var(--text-primary); }
        .dr-table tr:last-child td { border-bottom:none; }

        .dr-kv { display:grid; grid-template-columns:140px 1fr; gap:6px 12px; font-size:12px; }
        .dr-kv-label { color:var(--text-muted); }
        .dr-kv-value { color:var(--text-primary); font-weight:500; }

        .dr-metrics { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
        .dr-metric { background:var(--bg-card); border-radius:10px; padding:14px 18px; flex:1; min-width:120px; text-align:center; }
        .dr-metric-value { font-size:22px; font-weight:700; color:var(--text-primary); }
        .dr-metric-label { font-size:11px; color:var(--text-muted); margin-top:2px; }

        .dr-health-big { display:flex; align-items:center; gap:12px; padding:14px; background:var(--bg-primary); border-radius:10px; margin-bottom:14px; }
        .dr-health-icon { font-size:32px; }
        .dr-health-text { font-size:15px; font-weight:600; }

        .dr-btn { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:8px 14px; cursor:pointer; font-size:12px; display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
        .dr-btn:hover { filter:brightness(1.1); }
        .dr-btn:disabled { opacity:.5; cursor:not-allowed; filter:none; }
        .dr-btn-sm { padding:5px 10px; font-size:11px; }
        .dr-btn-outline { background:transparent; border:1px solid var(--border); color:var(--text-secondary); }
        .dr-btn-outline:hover { border-color:var(--accent); color:var(--accent); }
        .dr-btn-danger { background:#ef4444; }
        .dr-btn-danger:hover { background:#dc2626; }
        .dr-btn-warn { background:#f59e0b; }
        .dr-btn-warn:hover { background:#d97706; }

        .dr-select { background:var(--bg-primary); border:1px solid var(--border); border-radius:6px; padding:8px 10px; color:var(--text-primary); font-size:12px; }
        .dr-select option { background:var(--bg-primary); color:var(--text-primary); }

        .dr-log { max-height:200px; overflow-y:auto; background:var(--bg-primary); border-radius:8px; padding:8px 10px; margin-top:10px; font-family:monospace; font-size:11px; color:var(--text-muted); }
        .dr-log-line { padding:2px 0; border-bottom:1px solid var(--border); }
        .dr-log-line:last-child { border:none; }
        .dr-log-line.error { color:#ef4444; }
        .dr-log-line.success { color:#10b981; }

        .dr-progress-outer { height:20px; background:var(--bg-primary); border-radius:10px; overflow:hidden; position:relative; margin-top:10px; }
        .dr-progress-inner { height:100%; background:linear-gradient(90deg, var(--accent), #6366f1); border-radius:10px; transition:width .3s; width:0%; }
        .dr-progress-text { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:600; color:#fff; text-shadow:0 1px 2px rgba(0,0,0,.4); }

        .dr-row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
        .dr-warning { background:#fef3c7; color:#92400e; border-radius:8px; padding:10px 14px; font-size:12px; display:flex; align-items:center; gap:8px; margin-bottom:12px; }
        .dr-warning i { color:#f59e0b; }

        .dr-smart-status { display:inline-block; width:8px; height:8px; border-radius:50%; }

        .dr-hist-item { display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px solid var(--border); font-size:12px; }
        .dr-hist-item:last-child { border:none; }
        .dr-hist-icon { font-size:16px; width:20px; text-align:center; }
        .dr-hist-meta { flex:1; min-width:0; }
        .dr-hist-time { font-size:10px; color:var(--text-muted); white-space:nowrap; }

        .dr-nodata { text-align:center; padding:30px; color:var(--text-muted); font-size:13px; }
    </style>

    <div class="dr-wrap">
        <div class="dr-sidebar">
            <div class="dr-sidebar-header">
                <span><i class="fas fa-hard-drive"></i> Dyski</span>
                <button class="dr-btn dr-btn-sm dr-btn-outline" id="dr-refresh-disks" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="dr-disk-list" id="dr-disk-list"></div>
        </div>
        <div class="dr-main">
            <div class="dr-banner" id="dr-banner">
                <i class="fas fa-cog fa-spin" style="color:var(--accent);font-size:18px"></i>
                <div class="dr-banner-info">
                    <div class="dr-banner-title" id="dr-banner-title"></div>
                    <div class="dr-banner-progress"><div class="dr-banner-bar" id="dr-banner-bar"></div></div>
                    <div class="dr-banner-detail" id="dr-banner-detail"></div>
                </div>
                <button class="dr-btn dr-btn-sm dr-btn-danger" id="dr-banner-cancel"><i class="fas fa-stop"></i> Anuluj</button>
            </div>
            <div class="dr-tabs" id="dr-tabs">
                <div class="dr-tab active" data-tab="info"><i class="fas fa-info-circle"></i> Info</div>
                <div class="dr-tab" data-tab="smart"><i class="fas fa-heartbeat"></i> SMART</div>
                <div class="dr-tab" data-tab="check"><i class="fas fa-search"></i> ${t('Sprawdź')}</div>
                <div class="dr-tab" data-tab="repair"><i class="fas fa-wrench"></i> Naprawa</div>
                <div class="dr-tab" data-tab="history"><i class="fas fa-clock-rotate-left"></i> Historia</div>
            </div>
            <div class="dr-content" id="dr-content">
                <div class="dr-empty"><i class="fas fa-hard-drive"></i><span>Wybierz dysk z listy</span></div>
            </div>
        </div>
    </div>`;

    const $ = s => body.querySelector(s);
    const $$ = s => body.querySelectorAll(s);

    /* ─── Helpers ─── */
    function humanSize(b) {
        if (b == null) return '—';
        const n = Number(b);
        if (isNaN(n)) return String(b);
        if (n >= 1e12) return (n / 1e12).toFixed(1) + ' TB';
        if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
        if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
        return (n / 1024).toFixed(0) + ' KB';
    }

    function healthColor(disk) {
        if (!disk.smart_available) return '#888';
        if (disk.pending_sectors > 0) return '#ef4444';
        if (disk.reallocated_sectors > 0) return '#f59e0b';
        if (disk.smart_healthy === false) return '#ef4444';
        return '#10b981';
    }

    function tempColor(t) {
        if (t == null) return '';
        if (t > 60) return '#ef4444';
        if (t > 50) return '#f59e0b';
        return '#10b981';
    }

    function isRunning() {
        return state.operation && (state.operation.status === 'running' || state.operation.status === 'starting');
    }

    function escHtml(s) {
        if (!s) return '';
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    /* ─── Load disks ─── */
    async function loadDisks() {
        try {
            const data = await api('/diskrepair/disks');
            state.disks = data || [];
            renderDiskList();
            if (state.selectedDisk) {
                const still = state.disks.find(d => d.name === state.selectedDisk.name);
                if (still) { state.selectedDisk = still; }
            }
        } catch (e) {
            toast(t('Błąd ładowania dysków: ') + e.message, 'error');
        }
    }

    function renderDiskList() {
        const list = $('#dr-disk-list');
        if (!state.disks.length) {
            list.innerHTML = `<div class="dr-nodata"><i class="fas fa-info-circle"></i> ${t('Nie znaleziono dysków')}</div>`;
            return;
        }
        list.innerHTML = state.disks.map(d => {
            const sel = state.selectedDisk && state.selectedDisk.name === d.name;
            const icon = (d.rotational === false || (d.model && /ssd|nvme/i.test(d.model))) ? 'fa-microchip' : 'fa-hard-drive';
            const hc = healthColor(d);
            const tc = tempColor(d.temperature);
            return `
                <div class="dr-disk-item ${sel ? 'active' : ''}" data-disk="${escHtml(d.name)}">
                    <i class="fas ${icon} dr-disk-icon"></i>
                    <div class="dr-disk-meta">
                        <div class="dr-disk-model">${escHtml(d.model || d.name)}</div>
                        <div class="dr-disk-size">${humanSize(d.size)}</div>
                    </div>
                    ${d.temperature != null ? `<span class="dr-temp-badge" style="color:${sel ? '#fff' : tc}">${d.temperature}°C</span>` : ''}
                    <span class="dr-health-dot" style="background:${hc}" title="${d.smart_healthy === false ? 'FAILING' : d.smart_healthy ? 'OK' : '?'}"></span>
                </div>`;
        }).join('');

        list.querySelectorAll('.dr-disk-item').forEach(el => {
            el.onclick = () => {
                const name = el.dataset.disk;
                const disk = state.disks.find(d => d.name === name);
                if (disk) selectDisk(disk);
            };
        });
    }

    function selectDisk(disk) {
        state.selectedDisk = disk;
        state.smartData = null;
        renderDiskList();
        renderTab();
    }

    /* ─── Tabs ─── */
    $$('.dr-tab').forEach(tab => {
        tab.onclick = () => {
            state.activeTab = tab.dataset.tab;
            $$('.dr-tab').forEach(t => t.classList.toggle('active', t === tab));
            renderTab();
        };
    });

    function renderTab() {
        const content = $('#dr-content');
        if (!state.selectedDisk) {
            content.innerHTML = '<div class="dr-empty"><i class="fas fa-hard-drive"></i><span>Wybierz dysk z listy</span></div>';
            return;
        }
        switch (state.activeTab) {
            case 'info': renderInfoTab(content); break;
            case 'smart': renderSmartTab(content); break;
            case 'check': renderCheckTab(content); break;
            case 'repair': renderRepairTab(content); break;
            case 'history': renderHistoryTab(content); break;
        }
    }

    /* ─── Tab: Info ─── */
    function renderInfoTab(el) {
        const d = state.selectedDisk;
        const noSmart = !d.smart_available;
        el.innerHTML = `
            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-hard-drive"></i> Informacje o dysku</div>
                <div class="dr-kv">
                    <span class="dr-kv-label">${t('Urządzenie')}</span><span class="dr-kv-value">/dev/${escHtml(d.name)}</span>
                    <span class="dr-kv-label">Model</span><span class="dr-kv-value">${escHtml(d.model || '—')}</span>
                    <span class="dr-kv-label">Rozmiar</span><span class="dr-kv-value">${humanSize(d.size)}</span>
                    <span class="dr-kv-label">SMART</span><span class="dr-kv-value">${noSmart ? `<span style="color:#f59e0b">${t('Niedostępny')}</span>` : (d.smart_healthy ? '<span style="color:#10b981">Zdrowy</span>' : '<span style="color:#ef4444">Problemy!</span>')}</span>
                    <span class="dr-kv-label">Temperatura</span><span class="dr-kv-value">${d.temperature != null ? d.temperature + '°C' : '—'}</span>
                    <span class="dr-kv-label">Godziny pracy</span><span class="dr-kv-value">${d.power_on_hours != null ? d.power_on_hours.toLocaleString() + ' h' : '—'}</span>
                    <span class="dr-kv-label">Realokowane sektory</span><span class="dr-kv-value">${d.reallocated_sectors != null ? `<span style="color:${d.reallocated_sectors > 0 ? '#f59e0b' : '#10b981'}">${d.reallocated_sectors}</span>` : '—'}</span>
                    <span class="dr-kv-label">${t('Oczekujące sektory')}</span><span class="dr-kv-value">${d.pending_sectors != null ? `<span style="color:${d.pending_sectors > 0 ? '#ef4444' : '#10b981'}">${d.pending_sectors}</span>` : '—'}</span>
                </div>
            </div>
            ${noSmart ? `<div class="dr-warning"><i class="fas fa-exclamation-triangle"></i> ${t('SMART niedostępny dla tego dysku (dyski USB mogą nie obsługiwać SMART).')}</div>` : ''}
            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-table-cells"></i> Partycje</div>
                ${d.partitions && d.partitions.length ? `
                <table class="dr-table">
                    <thead><tr><th>${t('Nazwa')}</th><th>${t('Rozmiar')}</th><th>${t('System plików')}</th><th>${t('Punkt montowania')}</th><th>Status</th><th></th></tr></thead>
                    <tbody id="dr-partitions-body">
                    ${d.partitions.map(p => `
                        <tr>
                            <td>/dev/${escHtml(p.name)}</td>
                            <td>${humanSize(p.size)}</td>
                            <td>${escHtml(p.fstype || '—')}</td>
                            <td>${escHtml(p.mountpoint || '—')}</td>
                            <td>${p.mounted ? '<span style="color:#10b981"><i class="fas fa-circle" style="font-size:8px"></i> Zamontowany</span>' : '<span style="color:var(--text-muted)"><i class="far fa-circle" style="font-size:8px"></i> Niezamontowany</span>'}</td>
                            <td><button class="dr-btn dr-btn-sm dr-btn-outline dr-part-check" data-part="${escHtml(p.name)}" ${isRunning() ? 'disabled' : ''}><i class="fas fa-search"></i> ${t('Sprawdź')}</button></td>
                        </tr>
                    `).join('')}
                    </tbody>
                </table>` : '<div class="dr-nodata">Brak partycji</div>'}
            </div>`;

        el.querySelectorAll('.dr-part-check').forEach(btn => {
            btn.onclick = () => startFsck(btn.dataset.part, false);
        });
    }

    /* ─── Tab: SMART ─── */
    async function renderSmartTab(el) {
        const d = state.selectedDisk;
        if (!d.smart_available) {
            el.innerHTML = `<div class="dr-warning"><i class="fas fa-exclamation-triangle"></i> ${t('SMART niedostępny dla tego dysku (dyski USB mogą nie obsługiwać SMART).')}</div>`;
            return;
        }

        el.innerHTML = `<div class="dr-nodata"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie danych SMART...')}</div>`;

        try {
            const smart = await api(`/diskrepair/smart/${d.name}`);
            state.smartData = smart;
            renderSmartContent(el, smart);
        } catch (e) {
            el.innerHTML = `<div class="dr-warning"><i class="fas fa-times-circle"></i> ${t('Błąd ładowania SMART:')} ${escHtml(e.message)}</div>`;
        }
    }

    function renderSmartContent(el, smart) {
        const healthy = smart.health && /passed|ok/i.test(smart.health);
        el.innerHTML = `
            <div class="dr-health-big">
                <i class="fas ${healthy ? 'fa-check-circle' : 'fa-exclamation-triangle'} dr-health-icon" style="color:${healthy ? '#10b981' : '#ef4444'}"></i>
                <div>
                    <div class="dr-health-text" style="color:${healthy ? '#10b981' : '#ef4444'}">${healthy ? 'SMART: Zdrowy' : 'SMART: Problemy wykryte!'}</div>
                    <div style="font-size:12px;color:var(--text-muted)">${escHtml(smart.health || '')}</div>
                </div>
            </div>
            <div class="dr-metrics">
                <div class="dr-metric">
                    <div class="dr-metric-value" style="color:${tempColor(smart.temperature)}">${smart.temperature != null ? smart.temperature + '°C' : '—'}</div>
                    <div class="dr-metric-label">Temperatura</div>
                </div>
                <div class="dr-metric">
                    <div class="dr-metric-value">${smart.power_on_hours != null ? smart.power_on_hours.toLocaleString() : '—'}</div>
                    <div class="dr-metric-label">Godziny pracy</div>
                </div>
                <div class="dr-metric">
                    <div class="dr-metric-value">${smart.power_cycle_count != null ? smart.power_cycle_count.toLocaleString() : '—'}</div>
                    <div class="dr-metric-label">Cykle zasilania</div>
                </div>
            </div>
            ${smart.attributes && smart.attributes.length ? `
            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-list"></i> Atrybuty SMART</div>
                <div style="overflow-x:auto">
                <table class="dr-table">
                    <thead><tr><th>ID</th><th>${t('Nazwa')}</th><th>${t('Wartość')}</th><th>${t('Najgorsza')}</th><th>${t('Próg')}</th><th>Raw</th><th>Status</th></tr></thead>
                    <tbody>
                    ${smart.attributes.map(a => {
                        const failing = a.thresh && a.value && Number(a.value) <= Number(a.thresh);
                        const warn = a.name && /reallocat|pending|uncorrect/i.test(a.name) && Number(a.raw) > 0;
                        const color = failing ? '#ef4444' : warn ? '#f59e0b' : '#10b981';
                        return `<tr>
                            <td>${escHtml(a.id)}</td>
                            <td>${escHtml(a.name)}</td>
                            <td>${escHtml(a.value)}</td>
                            <td>${escHtml(a.worst)}</td>
                            <td>${escHtml(a.thresh)}</td>
                            <td style="font-family:monospace">${escHtml(a.raw)}</td>
                            <td><span class="dr-smart-status" style="background:${color}"></span></td>
                        </tr>`;
                    }).join('')}
                    </tbody>
                </table>
                </div>
            </div>` : ''}
            ${smart.error_log ? `<div class="dr-card"><div class="dr-card-title"><i class="fas fa-exclamation-circle"></i> ${t('Log błędów')}</div><pre style="font-size:11px;color:var(--text-muted);white-space:pre-wrap;margin:0">${escHtml(smart.error_log)}</pre></div>` : ''}
            ${smart.self_test_log ? `<div class="dr-card"><div class="dr-card-title"><i class="fas fa-vial"></i> ${t('Log testów')}</div><pre style="font-size:11px;color:var(--text-muted);white-space:pre-wrap;margin:0">${escHtml(smart.self_test_log)}</pre></div>` : ''}
            <div class="dr-row" style="margin-top:8px">
                <button class="dr-btn" id="dr-smart-short" ${isRunning() ? 'disabled' : ''}><i class="fas fa-bolt"></i> ${t('Test krótki')}</button>
                <button class="dr-btn dr-btn-warn" id="dr-smart-long" ${isRunning() ? 'disabled' : ''}><i class="fas fa-clock"></i> ${t('Test długi')}</button>
            </div>`;

        const shortBtn = el.querySelector('#dr-smart-short');
        const longBtn = el.querySelector('#dr-smart-long');
        if (shortBtn) shortBtn.onclick = () => startSmartTest('short');
        if (longBtn) longBtn.onclick = () => startSmartTest('long');
    }

    async function startSmartTest(type) {
        if (isRunning()) { toast('Inna operacja w toku', 'warning'); return; }
        const d = state.selectedDisk;
        try {
            const r = await api('/diskrepair/smart-test', { method: 'POST', body: { disk: d.name, type } });
            toast(r.message || 'Test SMART uruchomiony', 'success');
        } catch (e) {
            toast(t('Błąd: ') + e.message, 'error');
        }
    }

    /* ─── Tab: Sprawdź (Check) ─── */
    function renderCheckTab(el) {
        const d = state.selectedDisk;
        const parts = (d.partitions || []).filter(p => p.fstype);
        el.innerHTML = `
            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-search"></i> ${t('Sprawdzanie systemu plików (fsck)')}</div>
                ${parts.length ? `
                <div class="dr-row" style="margin-bottom:12px">
                    <select class="dr-select" id="dr-check-part" style="flex:1">
                        <option value="">— ${t('Wybierz partycję')} —</option>
                        ${parts.map(p => `<option value="${escHtml(p.name)}">/dev/${escHtml(p.name)} (${escHtml(p.fstype)}, ${humanSize(p.size)})</option>`).join('')}
                    </select>
                    <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary);cursor:pointer">
                        <input type="checkbox" id="dr-check-repair"> ${t('Napraw (fsck -y)')}
                    </label>
                </div>
                <div class="dr-row">
                    <button class="dr-btn" id="dr-check-start" ${isRunning() ? 'disabled' : ''}><i class="fas fa-play"></i> Rozpocznij sprawdzanie</button>
                </div>
                ` : `<div class="dr-nodata">${t('Brak partycji z systemem plików')}</div>`}
            </div>
            <div id="dr-check-progress" style="display:none">
                <div class="dr-card">
                    <div class="dr-card-title"><i class="fas fa-cog fa-spin"></i> ${t('Postęp')}</div>
                    <div class="dr-progress-outer">
                        <div class="dr-progress-inner" id="dr-check-bar"></div>
                        <div class="dr-progress-text" id="dr-check-pct">0%</div>
                    </div>
                    <div class="dr-log" id="dr-check-log"></div>
                    <div style="margin-top:10px"><button class="dr-btn dr-btn-sm dr-btn-danger" id="dr-check-cancel"><i class="fas fa-stop"></i> Anuluj</button></div>
                </div>
            </div>`;

        const startBtn = el.querySelector('#dr-check-start');
        if (startBtn) {
            startBtn.onclick = () => {
                const part = el.querySelector('#dr-check-part').value;
                const repair = el.querySelector('#dr-check-repair').checked;
                if (!part) { toast(t('Wybierz partycję'), 'warning'); return; }
                startFsck(part, repair);
            };
        }
        const cancelBtn = el.querySelector('#dr-check-cancel');
        if (cancelBtn) cancelBtn.onclick = cancelOperation;

        if (isRunning() && state.operation && (state.operation.operation === 'fsck')) {
            showCheckProgress(el);
        }
    }

    function showCheckProgress(el) {
        const wrap = el.querySelector('#dr-check-progress');
        if (wrap) wrap.style.display = '';
    }

    async function startFsck(partition, repair) {
        if (isRunning()) { toast('Inna operacja w toku', 'warning'); return; }
        try {
            await api('/diskrepair/fsck', { method: 'POST', body: { partition, repair } });
            toast(t('Sprawdzanie rozpoczęte'), 'info');
            state.logOffset = 0;
            startPolling();
        } catch (e) {
            toast(t('Błąd: ') + e.message, 'error');
        }
    }

    /* ─── Tab: Naprawa (Repair) ─── */
    function renderRepairTab(el) {
        const d = state.selectedDisk;
        const parts = (d.partitions || []).filter(p => p.fstype);
        el.innerHTML = `
            <div class="dr-warning"><i class="fas fa-exclamation-triangle"></i> ${t('Operacje naprawcze mogą trwać bardzo długo i mogą uszkodzić dane. Upewnij się, że masz kopię zapasową!')}</div>

            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-search-plus"></i> ${t('Skanowanie uszkodzonych sektorów (badblocks)')}</div>
                <div class="dr-row" style="margin-bottom:10px">
                    <span style="font-size:12px;color:var(--text-secondary)">Dysk:</span>
                    <strong style="font-size:12px;color:var(--text-primary)">/dev/${escHtml(d.name)}</strong>
                    <select class="dr-select" id="dr-bb-mode">
                        <option value="readonly">Tylko odczyt (bezpieczny)</option>
                        <option value="nondestructive">Niedestrukcyjny zapis</option>
                    </select>
                </div>
                <div class="dr-warning"><i class="fas fa-clock"></i> ${t('Badblocks może trwać wiele godzin, w zależności od rozmiaru dysku.')}</div>
                <button class="dr-btn dr-btn-warn" id="dr-bb-start" ${isRunning() ? 'disabled' : ''}><i class="fas fa-play"></i> Rozpocznij skanowanie</button>
            </div>

            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-wrench"></i> ${t('Naprawa systemu plików')}</div>
                ${parts.length ? `
                <div class="dr-row" style="margin-bottom:10px">
                    <select class="dr-select" id="dr-repair-part" style="flex:1">
                        <option value="">— ${t('Wybierz partycję')} —</option>
                        ${parts.map(p => `<option value="${escHtml(p.name)}">/dev/${escHtml(p.name)} (${escHtml(p.fstype)}, ${humanSize(p.size)})${p.mounted ? ' [' + t('zamontowany') + ']' : ''}</option>`).join('')}
                    </select>
                    <button class="dr-btn dr-btn-sm dr-btn-outline" id="dr-repair-unmount"><i class="fas fa-eject"></i> Odmontuj</button>
                    <button class="dr-btn dr-btn-danger" id="dr-repair-start" ${isRunning() ? 'disabled' : ''}><i class="fas fa-wrench"></i> Napraw (fsck -y)</button>
                </div>
                ` : `<div class="dr-nodata">${t('Brak partycji z systemem plików')}</div>`}
            </div>

            <div id="dr-repair-progress" style="display:none">
                <div class="dr-card">
                    <div class="dr-card-title"><i class="fas fa-cog fa-spin"></i> ${t('Postęp operacji')}</div>
                    <div class="dr-progress-outer">
                        <div class="dr-progress-inner" id="dr-repair-bar"></div>
                        <div class="dr-progress-text" id="dr-repair-pct">0%</div>
                    </div>
                    <div class="dr-log" id="dr-repair-log"></div>
                    <div style="margin-top:10px"><button class="dr-btn dr-btn-sm dr-btn-danger" id="dr-repair-cancel"><i class="fas fa-stop"></i> Anuluj</button></div>
                </div>
            </div>`;

        const bbStartBtn = el.querySelector('#dr-bb-start');
        if (bbStartBtn) {
            bbStartBtn.onclick = () => {
                const mode = el.querySelector('#dr-bb-mode').value;
                startBadblocks(d.name, mode);
            };
        }

        const repairStartBtn = el.querySelector('#dr-repair-start');
        if (repairStartBtn) {
            repairStartBtn.onclick = () => {
                const part = el.querySelector('#dr-repair-part').value;
                if (!part) { toast(t('Wybierz partycję'), 'warning'); return; }
                startFsck(part, true);
            };
        }

        const unmountBtn = el.querySelector('#dr-repair-unmount');
        if (unmountBtn) {
            unmountBtn.onclick = async () => {
                const part = el.querySelector('#dr-repair-part').value;
                if (!part) { toast(t('Wybierz partycję'), 'warning'); return; }
                try {
                    await api('/diskrepair/unmount', { method: 'POST', body: { partition: part } });
                    toast('Odmontowano /dev/' + part, 'success');
                    await loadDisks();
                    renderTab();
                } catch (e) {
                    toast(t('Błąd odmontowywania: ') + e.message, 'error');
                }
            };
        }

        const repairCancel = el.querySelector('#dr-repair-cancel');
        if (repairCancel) repairCancel.onclick = cancelOperation;

        if (isRunning()) {
            const wrap = el.querySelector('#dr-repair-progress');
            if (wrap) wrap.style.display = '';
        }
    }

    async function startBadblocks(disk, mode) {
        if (isRunning()) { toast('Inna operacja w toku', 'warning'); return; }
        try {
            await api('/diskrepair/badblocks', { method: 'POST', body: { disk, mode } });
            toast(t('Skanowanie badblocks rozpoczęte'), 'info');
            state.logOffset = 0;
            startPolling();
        } catch (e) {
            toast(t('Błąd: ') + e.message, 'error');
        }
    }

    /* ─── Tab: Historia ─── */
    async function renderHistoryTab(el) {
        el.innerHTML = `<div class="dr-nodata"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>`;
        try {
            const data = await api('/diskrepair/history');
            state.history = data || [];
            if (!state.history.length) {
                el.innerHTML = '<div class="dr-nodata"><i class="fas fa-clock-rotate-left"></i> Brak historii operacji</div>';
                return;
            }
            el.innerHTML = `<div class="dr-card"><div class="dr-card-title"><i class="fas fa-clock-rotate-left"></i> Historia operacji</div>
                ${state.history.slice().reverse().map(h => {
                    const ok = h.success || h.result === 'success';
                    const icon = ok ? 'fa-check-circle' : 'fa-times-circle';
                    const color = ok ? '#10b981' : '#ef4444';
                    const ts = h.timestamp ? new Date(typeof h.timestamp === 'number' && h.timestamp < 1e12 ? h.timestamp * 1000 : h.timestamp).toLocaleString(getLocale()) : '';
                    return `<div class="dr-hist-item">
                        <i class="fas ${icon} dr-hist-icon" style="color:${color}"></i>
                        <div class="dr-hist-meta">
                            <div style="font-weight:500;color:var(--text-primary)">${escHtml(h.operation || h.type || '?')} — ${escHtml(h.disk || h.partition || '')}</div>
                            <div style="font-size:11px;color:var(--text-muted)">${escHtml(h.message || h.detail || '')}</div>
                        </div>
                        <div class="dr-hist-time">${ts}</div>
                    </div>`;
                }).join('')}
            </div>`;
        } catch (e) {
            el.innerHTML = `<div class="dr-warning"><i class="fas fa-times-circle"></i> ${t('Błąd:')} ${escHtml(e.message)}</div>`;
        }
    }

    /* ─── Operation banner / polling ─── */
    async function cancelOperation() {
        try {
            await api('/diskrepair/cancel', { method: 'POST' });
            toast('Operacja anulowana', 'warning');
        } catch (e) {
            toast(t('Błąd anulowania: ') + e.message, 'error');
        }
    }

    function startPolling() {
        stopPolling();
        state.pollTimer = setInterval(pollStatus, 2000);
        pollStatus();
    }

    function stopPolling() {
        if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    }

    async function pollStatus() {
        try {
            const st = await api(`/diskrepair/status?since=${state.logOffset}`);
            state.operation = st;

            const banner = $('#dr-banner');
            const bannerTitle = $('#dr-banner-title');
            const bannerBar = $('#dr-banner-bar');
            const bannerDetail = $('#dr-banner-detail');
            const bannerCancel = $('#dr-banner-cancel');

            if (st.status === 'running' || st.status === 'starting') {
                banner.classList.add('active');
                bannerTitle.textContent = `${st.operation || 'Operacja'} — ${st.disk || st.partition || ''}`;
                bannerBar.style.width = (st.percent || 0) + '%';

                let detail = st.message || '';
                if (st.percent != null) detail = st.percent + '% ' + detail;
                bannerDetail.textContent = detail;
                bannerCancel.style.display = '';

                updateTabProgress(st);

                for (const line of (st.logs || [])) appendLog(line);
                state.logOffset = st.log_total || state.logOffset;

                disableStartButtons(true);
            } else if (st.status === 'done' || st.status === 'error') {
                for (const line of (st.logs || [])) appendLog(line);
                state.logOffset = st.log_total || state.logOffset;

                const ok = st.status === 'done' || (st.result && st.result === 'success');
                banner.classList.add('active');
                bannerTitle.textContent = ok ? t('Operacja zakończona pomyślnie') : t('Operacja zakończona z błędem');
                bannerBar.style.width = ok ? '100%' : '0%';
                bannerDetail.textContent = st.message || '';
                bannerCancel.style.display = 'none';

                toast(ok ? t('Operacja zakończona') : t('Operacja nie powiodła się'), ok ? 'success' : 'error');
                stopPolling();
                disableStartButtons(false);

                // Dismiss after showing result
                try { await api('/diskrepair/dismiss', { method: 'POST' }); } catch (_) {}
                state.operation = null;

                // Auto-hide banner after 5s
                setTimeout(() => {
                    banner.classList.remove('active');
                    loadDisks();
                }, 5000);
            } else {
                banner.classList.remove('active');
                stopPolling();
                disableStartButtons(false);
                state.operation = null;
            }
        } catch (e) {
            // Keep polling on transient errors
        }
    }

    function updateTabProgress(st) {
        // Update inline progress bars in check/repair tabs
        const bar = body.querySelector('#dr-check-bar') || body.querySelector('#dr-repair-bar');
        const pct = body.querySelector('#dr-check-pct') || body.querySelector('#dr-repair-pct');
        const progressWrap = body.querySelector('#dr-check-progress') || body.querySelector('#dr-repair-progress');

        if (progressWrap) progressWrap.style.display = '';
        if (bar) bar.style.width = (st.percent || 0) + '%';
        if (pct) pct.textContent = (st.percent || 0) + '%';
    }

    function appendLog(line) {
        const logEls = body.querySelectorAll('.dr-log');
        logEls.forEach(logEl => {
            const cls = /error|fail/i.test(line) ? 'error' : /success|pass|ok/i.test(line) ? 'success' : '';
            logEl.innerHTML += `<div class="dr-log-line ${cls}">${escHtml(line)}</div>`;
            logEl.scrollTop = logEl.scrollHeight;
        });
    }

    function disableStartButtons(disabled) {
        body.querySelectorAll('#dr-check-start, #dr-bb-start, #dr-repair-start, #dr-smart-short, #dr-smart-long').forEach(btn => {
            btn.disabled = disabled;
        });
        body.querySelectorAll('.dr-part-check').forEach(btn => { btn.disabled = disabled; });
    }

    /* ─── Banner cancel ─── */
    $('#dr-banner-cancel').onclick = cancelOperation;

    /* ─── Refresh button ─── */
    $('#dr-refresh-disks').onclick = async () => {
        await loadDisks();
        renderTab();
        toast(t('Odświeżono'), 'info');
    };

    /* ─── Check for running operation ─── */
    async function checkRunningOp() {
        try {
            const st = await api('/diskrepair/status?since=0');
            if (st.status === 'running' || st.status === 'starting') {
                state.operation = st;
                state.logOffset = st.log_total || 0;
                startPolling();
            }
        } catch (_) {}
    }

    /* ─── Init ─── */
    loadDisks();
    checkRunningOp();
}
